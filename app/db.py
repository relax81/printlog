"""SQLite-Speicher für Druckaufträge."""

from __future__ import annotations

import json
import math
import re
import secrets
import sqlite3
import threading
import unicodedata
import time
from pathlib import Path
from typing import Any

def norm(value: Any) -> str:
    """Kleinschreibung ohne Akzente, damit "tur" auch "Türmagnet" findet."""
    if value is None:
        return ""
    s = str(value).casefold().replace("ß", "ss")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


# Kurze Projekt-ID wie "PL-7K3Q": 4 Zeichen ohne Verwechsler (0/O, 1/I/L), 31^4 ≈ 920 000 Kombinationen.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 4
CODE_PREFIX = "PL-"
_CODE_RE = re.compile(rf"(?:PL-?)?([{CODE_ALPHABET}]{{{CODE_LEN}}})")


def code_label(code: str | None) -> str:
    """Anzeige mit Präfix, z. B. "PL-7K3Q"."""
    return f"{CODE_PREFIX}{code}" if code else ""


def parse_code(text: str | None) -> str | None:
    """Eingabe wie "pl-7k3q", "PL7K3Q", "PL 7K3Q" oder "7k3q" → "7K3Q"; sonst None."""
    t = re.sub(r"[\s_.]", "", (text or "").upper())
    m = _CODE_RE.fullmatch(t)
    return m.group(1) if m else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    printer       TEXT NOT NULL,          -- Kurzname, z. B. h2s
    printer_name  TEXT,                   -- Anzeigename, z. B. Bamboss
    task_name     TEXT,
    state         TEXT NOT NULL,          -- running | closing | done
    status        TEXT,                   -- fertig | abgebrochen | unvollständig
    start_ts      REAL,                   -- Unix-Zeit (UTC)
    end_ts        REAL,                   -- Druckende laut Drucker
    final_ts      REAL,                   -- Ende der Messung (nach Abkühlen)
    start_kwh     REAL,
    end_kwh       REAL,
    kwh           REAL,
    price         REAL,                   -- €/kWh zum Druckende
    cost          REAL,
    weight_g      REAL,                   -- geplantes Gewicht laut Slicer
    progress      REAL,                   -- Fortschritt beim Ende in %
    image         TEXT,                   -- Dateiname im Bilderordner
    note          TEXT,                   -- Grund für "unvollständig"
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_printer_state ON jobs(printer, state);
CREATE TABLE IF NOT EXISTS printer_rates (
    printer        TEXT PRIMARY KEY,
    purchase_eur   REAL,                  -- Anschaffungspreis
    life_hours     REAL,                  -- erwartete Lebensdauer in Druckstunden
    extra_per_hour REAL                   -- alt: Verschleiß pro Stunde (steckt jetzt in maint_per_1000h)
);
CREATE TABLE IF NOT EXISTS photos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    file       TEXT NOT NULL,             -- Dateiname im Bilderordner
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_job ON photos(job_id);
CREATE TABLE IF NOT EXISTS handovers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    qty        INTEGER NOT NULL,          -- übergebene Stück (Teilabholung möglich)
    ts         REAL NOT NULL,             -- Zeitpunkt der Übergabe
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_handovers_job ON handovers(job_id);
CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    amount     REAL NOT NULL,             -- gezahlter Betrag in € (Teilzahlung möglich)
    ts         REAL NOT NULL,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_payments_job ON payments(job_id);
CREATE TABLE IF NOT EXISTS customers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    contact    TEXT,
    note       TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS material_prices (
    key        TEXT PRIMARY KEY,          -- Filamentname (z. B. "Bambu PLA Matte") oder Typ ("PLA")
    kind       TEXT NOT NULL,             -- name | type
    eur_per_kg REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_start ON jobs(start_ts);
-- Papierkorb: gelöschte Drucke (ganze Zeile als JSON), Zahlungen/Übergaben/Fotos bleiben bis zum endgültigen Löschen liegen
CREATE TABLE IF NOT EXISTS trash (
    id          INTEGER PRIMARY KEY,      -- ursprüngliche Job-ID
    printer     TEXT,
    start_ts    REAL,
    end_ts      REAL,
    deleted_ts  REAL NOT NULL,
    data        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _scaled_materials(raw: Any, factor: float) -> str | None:
    """Materialliste (JSON) mit anteiligen Gramm, z. B. für 4 von 10 Stück."""
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    out = []
    for m in items:
        if isinstance(m, dict):
            m = dict(m)
            try:
                m["grams"] = round(float(m.get("grams") or 0) * factor, 2)
            except (TypeError, ValueError):
                pass
            out.append(m)
    return json.dumps(out)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("norm", 1, norm, deterministic=True)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._lock = threading.Lock()

    # Spalten, die nach der ersten Version dazugekommen sind.
    NEW_COLUMNS = {
        "materials": "TEXT",          # JSON-Liste [{slot,name,type,color,grams}]
        "cost_printer": "REAL",       # Abschreibung + Wartung
        "cost_filament": "REAL",
        "cost_total": "REAL",
        "filament_g": "REAL",         # tatsächlich verbraucht (bei Abbruch anteilig)
        "customer_id": "INTEGER",
        "quantity": "INTEGER",        # Stückzahl auf der Platte
        "job_note": "TEXT",           # eigene Notiz
        "scrap": "INTEGER",           # Ausschuss: 1 ja, 0 nein, NULL = automatisch (abgebrochen)
        "project_url": "TEXT",        # z. B. MakerWorld-Link
        "project_title": "TEXT",      # Titel des Modells, durchsuchbar
        "local_path": "TEXT",         # Projektordner auf dem Windows-PC
        "final_price": "REAL",        # Endpreis für den Kunden (leer = Kundenpreis aus Kalkulation)
        "gift": "INTEGER",            # 1 = verschenkt: zählt in Kosten/Statistik, wird aber nicht berechnet
        "material_notes": "TEXT",     # JSON {"0": "Ivory White", …}: eigene Notiz je Material-Zeile (Index in materials)
        "code": "TEXT",               # kurze Projekt-ID ohne Präfix, z. B. "7K3Q" (Anzeige "PL-7K3Q")
        "printer_start_ts": "REAL",   # Startzeit laut Drucker: erkennt Fortsetzen nach Pause als gleichen Druck
        "defect": "TEXT",             # Fehldruck-Ursache (Schlüssel aus defects.DEFECTS), leer = nicht angegeben
        "defect_charge": "TEXT",      # Fehldruck berechnen: leer/none = nichts, pct, amount, full
        "defect_value": "REAL",       # Prozent (pct) oder Betrag (amount)
        "reprint_of": "INTEGER",      # Nachdruck: ID des Fehldrucks, den dieser Druck ersetzt
        "kind": "TEXT",               # 'project' = Projekt aus mehreren Drucken (kein echter Druck), sonst leer
        "project_id": "INTEGER",      # Druck gehört zu diesem Projekt (ID der Projekt-Zeile)
        "bill_base": "REAL",          # Projekt: Kostenbasis für den Preis (ohne unberechnete Fehldrucke)
        "split": "INTEGER",           # 1 = Stückzahl aufgeteilt (Anteile je Kunde, Rest im Vorrat)
        "share_of": "INTEGER",        # Anteil (kind='share'): ID des aufgeteilten Drucks bzw. Projekts
        "share_part": "INTEGER",      # Anteil mit Einzelteilen eines Projekts: ID des Drucks im Projekt
        "share_kind": "TEXT",         # Anteil: split (beim Aufteilen) | stock (aus dem Vorrat) | out (ausgebucht)
    }

    # Echter Druck (keine Projekt-Zeile): nur diese zählen in Statistik, Kosten-Kacheln, Kalender.
    REAL_SQL = "jobs.kind IS NULL"

    # Übergebene Stück (Summe der Übergaben, auch Teilabholungen) und Soll-Stückzahl
    DELIVERED_SQL = "(SELECT COALESCE(SUM(h.qty),0) FROM handovers h WHERE h.job_id = jobs.id)"
    QTY_SQL = "COALESCE(jobs.quantity, 1)"
    # Kunde mit Häkchen „Keine Abrechnung“ (z. B. Eigenbedarf): Kosten zählen, aber nie offen/unbezahlt/nicht übergeben.
    NOBILL_SQL = "COALESCE((SELECT cb.no_billing FROM customers cb WHERE cb.id = jobs.customer_id), 0) = 1"
    # Fehldruck („Ausschuss“), wenn so markiert oder (ohne Markierung) abgebrochen.
    SCRAP_SQL = "COALESCE(jobs.scrap, jobs.status = 'abgebrochen')"
    # Fehldruck, für den der Kunde nichts zahlt: fällt aus Offen und Abrechnung heraus.
    NOCHARGE_SQL = f"({SCRAP_SQL} = 1 AND COALESCE(jobs.defect_charge, '') NOT IN ('pct', 'amount', 'full'))"
    # Drucke in einem Projekt werden über das Projekt abgerechnet und übergeben.
    # Aufgeteilte Drucke/Projekte rechnen über ihre Anteile ab (kind='share'), nicht selbst.
    UNSPLIT_SQL = "COALESCE(jobs.split, 0) = 0"
    DELIVERABLE_SQL = (f"(jobs.customer_id IS NOT NULL AND NOT {NOCHARGE_SQL} AND jobs.state = 'done' "
                       f"AND NOT {NOBILL_SQL} AND jobs.project_id IS NULL AND {UNSPLIT_SQL})")

    # Normaler Preis: Endpreis, sonst Kosten + Kundenmarge.
    BASE_DUE_SQL = ("COALESCE(jobs.final_price, COALESCE(jobs.bill_base, jobs.cost_total) * (1 + COALESCE("
                    "(SELECT c.margin_pct FROM customers c WHERE c.id = jobs.customer_id), 0) / 100.0))")
    # Zu zahlen: normaler Preis, bei einem berechneten Fehldruck anteilig bzw. fester Betrag. Gezahlt: Summe der Zahlungen.
    DUE_SQL = (f"(CASE WHEN {SCRAP_SQL} = 1 AND jobs.defect_charge = 'pct' THEN {BASE_DUE_SQL} * COALESCE(jobs.defect_value, 0) / 100.0 "
               f"WHEN {SCRAP_SQL} = 1 AND jobs.defect_charge = 'amount' THEN COALESCE(jobs.defect_value, 0) ELSE {BASE_DUE_SQL} END)")
    PAID_SQL = "(SELECT COALESCE(SUM(p.amount),0) FROM payments p WHERE p.job_id = jobs.id)"
    GIFT_SQL = "COALESCE(jobs.gift, 0) = 1"
    # Druck mit Kunde (mit Abrechnung), kein unberechneter Fehldruck, nicht verschenkt, mit Preis: dafür wird Geld erwartet.
    PAYABLE_SQL = (f"(jobs.customer_id IS NOT NULL AND {DUE_SQL} IS NOT NULL "
                   f"AND NOT {NOCHARGE_SQL} AND COALESCE(jobs.gift, 0) = 0 AND NOT {NOBILL_SQL} AND jobs.project_id IS NULL "
                   f"AND {UNSPLIT_SQL})")
    NOSHARE_SQL = "COALESCE(jobs.kind, '') <> 'share'"

    def _migrate(self) -> None:
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        for col, typ in self.NEW_COLUMNS.items():
            if col not in have:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(printer_rates)")}
        for col in ("maint_per_year", "hours_per_year"):
            if col not in have:
                self._conn.execute(f"ALTER TABLE printer_rates ADD COLUMN {col} REAL")
        if "maint_per_1000h" not in have:
            # Wartung jetzt pro 1000 Druckstunden. Alte Werte (€/Jahr ÷ Druckstunden/Jahr und
            # Verschleiß €/h) so umrechnen, dass der Stundensatz gleich bleibt. Die alten Spalten
            # bleiben unverändert stehen, werden aber nicht mehr gelesen.
            self._conn.execute("ALTER TABLE printer_rates ADD COLUMN maint_per_1000h REAL")
            self._conn.execute(
                "UPDATE printer_rates SET maint_per_1000h = ROUND("
                "CASE WHEN maint_per_year > 0 AND hours_per_year > 0 THEN maint_per_year * 1000.0 / hours_per_year ELSE 0 END"
                " + CASE WHEN extra_per_hour > 0 THEN extra_per_hour * 1000.0 ELSE 0 END, 2) "
                "WHERE (maint_per_year > 0 AND hours_per_year > 0) OR extra_per_hour > 0")
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(customers)")}
        if "margin_pct" not in have:
            self._conn.execute("ALTER TABLE customers ADD COLUMN margin_pct REAL")
        if "no_billing" not in have:
            # 1 = keine Abrechnung (z. B. Eigenbedarf): keine Zahlungen/Übergaben, Kosten zählen weiter
            self._conn.execute("ALTER TABLE customers ADD COLUMN no_billing INTEGER")
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_code ON jobs(code)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_share ON jobs(share_of)")
        self._conn.commit()
        self.ensure_codes()

    # ---- Projekt-ID ---------------------------------------------------------

    def _code_taken(self, code: str) -> bool:
        return bool(self._conn.execute("SELECT 1 FROM jobs WHERE code = ?", (code,)).fetchone()
                    or self._conn.execute("SELECT 1 FROM trash WHERE json_extract(data, '$.code') = ?", (code,)).fetchone())

    def new_code(self) -> str:
        """Zufällige, noch freie Projekt-ID (auch nicht im Papierkorb vergeben)."""
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
            if not self._code_taken(code):
                return code

    def ensure_codes(self) -> int:
        """Drucke ohne Projekt-ID (ältere oder aus dem Papierkorb) bekommen eine."""
        ids = [r[0] for r in self._conn.execute("SELECT id FROM jobs WHERE code IS NULL ORDER BY id")]
        for job_id in ids:
            self._conn.execute("UPDATE jobs SET code = ? WHERE id = ?", (self.new_code(), job_id))
        if ids:
            self._conn.commit()
        return len(ids)

    def find_code(self, text: str | None) -> int | None:
        """Job-ID zu einer eingegebenen Projekt-ID (mit/ohne "PL-", Groß/klein egal)."""
        code = parse_code(text)
        if not code:
            return None
        row = self._conn.execute("SELECT id FROM jobs WHERE code = ?", (code,)).fetchone()
        return row[0] if row else None

    # ---- Schreiben -------------------------------------------------------

    def insert(self, **fields: Any) -> int:
        fields.setdefault("created_at", time.time())
        with self._lock:
            fields.setdefault("code", self.new_code())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"INSERT INTO jobs ({cols}) VALUES ({marks})", list(fields.values())
            )
            return int(cur.lastrowid)

    def update(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE jobs SET {sets} WHERE id = ?", [*fields.values(), job_id]
            )
        row = self._conn.execute("SELECT kind, project_id, split FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return
        if row["kind"] == "project":
            if "customer_id" in fields:
                # Kunde des Projekts gilt für alle seine Drucke (Filter „Kunde“ findet sie weiter).
                with self._lock, self._conn:
                    self._conn.execute("UPDATE jobs SET customer_id = ? WHERE project_id = ?", (fields["customer_id"], job_id))
            if {"customer_id", "gift", "quantity", "task_name"} & set(fields):
                self.refresh_project(job_id)
        elif row["project_id"]:
            self.refresh_project(row["project_id"])
        elif row["split"] and row["kind"] != "share":
            self.refresh_shares(job_id)

    # ---- Papierkorb -------------------------------------------------------

    def trash_job(self, job_id: int) -> dict[str, Any] | None:
        """Druck in den Papierkorb legen (aus jobs raus, wiederherstellbar). Ein Projekt wird nur aufgelöst."""
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row and row["kind"] == "share":
            self.remove_share(job_id)
            return None
        if row and row["kind"] == "project":
            self.dissolve_project(job_id)
            return None
        if row and row["project_id"]:
            pid = row["project_id"]
            out = self._trash_row(job_id)
            if not self._conn.execute("SELECT 1 FROM jobs WHERE project_id = ?", (pid,)).fetchone():
                self.dissolve_project(pid)
            else:
                self.refresh_project(pid)
            return out
        return self._trash_row(job_id)

    def _trash_row(self, job_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        job = dict(row)
        if job.get("split"):
            # Anteile kommen mit in den Papierkorb (Zahlungen/Übergaben bleiben bis zum endgültigen Löschen liegen)
            job["_shares"] = [dict(r) for r in self._conn.execute("SELECT * FROM jobs WHERE kind = 'share' AND share_of = ?", (job_id,))]
        with self._lock, self._conn:
            for sh in job.get("_shares", []):
                self._conn.execute("DELETE FROM jobs WHERE id = ?", (sh["id"],))
            self._conn.execute(
                "INSERT OR REPLACE INTO trash (id, printer, start_ts, end_ts, deleted_ts, data) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, job["printer"], job["start_ts"], job["end_ts"], time.time(), json.dumps(job)))
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        self._tidy_projects({sh["project_id"] for sh in job.get("_shares", []) if sh.get("project_id")})
        return job

    def trashed(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM trash ORDER BY deleted_ts DESC, id DESC").fetchall()
        out = []
        for r in rows:
            job = json.loads(r["data"])
            job["deleted_ts"] = r["deleted_ts"]
            out.append(job)
        return out

    def trash_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM trash").fetchone()[0]

    def restore_job(self, job_id: int) -> bool:
        row = self._conn.execute("SELECT data FROM trash WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return False
        job = json.loads(row["data"])
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        shares = [{k: v for k, v in sh.items() if k in have} for sh in job.get("_shares") or []]
        job = {k: v for k, v in job.items() if k in have}
        if job.get("code") and self._conn.execute(
                "SELECT 1 FROM jobs WHERE code = ? AND id != ?", (job["code"], job_id)).fetchone():
            job["code"] = None  # sehr unwahrscheinlich, aber dann neu vergeben
        if job.get("customer_id") and not self._conn.execute(
                "SELECT 1 FROM customers WHERE id = ?", (job["customer_id"],)).fetchone():
            job["customer_id"] = None  # Kunde inzwischen gelöscht
        pid = job.get("project_id")
        if pid and not self._conn.execute("SELECT 1 FROM jobs WHERE id = ? AND kind = 'project'", (pid,)).fetchone():
            job["project_id"] = pid = None  # Projekt gibt es nicht mehr: Druck kommt einzeln zurück
        cols = ", ".join(job)
        with self._lock, self._conn:
            self._conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({', '.join('?' for _ in job)})", list(job.values()))
            for sh in shares:
                if sh.get("project_id") and not self._conn.execute(
                        "SELECT 1 FROM jobs WHERE id = ? AND kind = 'project'", (sh["project_id"],)).fetchone():
                    continue  # Projekt gibt es nicht mehr: die Stück liegen wieder im Vorrat
                if sh.get("customer_id") and not self._conn.execute("SELECT 1 FROM customers WHERE id = ?", (sh["customer_id"],)).fetchone():
                    sh["customer_id"] = None
                if sh.get("code") and self._conn.execute("SELECT 1 FROM jobs WHERE code = ?", (sh["code"],)).fetchone():
                    sh["code"] = None
                self._conn.execute(f"INSERT OR REPLACE INTO jobs ({', '.join(sh)}) VALUES ({', '.join('?' for _ in sh)})", list(sh.values()))
            self._conn.execute("DELETE FROM trash WHERE id = ?", (job_id,))
        self.ensure_codes()
        for p in {pid} | {sh["project_id"] for sh in shares if sh.get("project_id")}:
            if p:
                self.refresh_project(p)
        return True

    def purge_job(self, job_id: int) -> list[str]:
        """Endgültig löschen; gibt die Bilddateien zurück, die weg können."""
        row = self._conn.execute("SELECT data FROM trash WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return []
        data = json.loads(row["data"])
        image = data.get("image")
        files = [p["file"] for p in self.photos(job_id)] + ([image] if image else [])
        with self._lock, self._conn:
            for jid in [job_id] + [sh["id"] for sh in data.get("_shares") or []]:
                for table in ("handovers", "payments", "photos"):
                    self._conn.execute(f"DELETE FROM {table} WHERE job_id = ?", (jid,))
            self._conn.execute("DELETE FROM trash WHERE id = ?", (job_id,))
        return files

    def expired_trash(self, older_than_ts: float) -> list[int]:
        return [r[0] for r in self._conn.execute("SELECT id FROM trash WHERE deleted_ts < ?", (older_than_ts,))]

    def delete(self, job_id: int) -> dict[str, Any] | None:
        job = self.get(job_id)
        if job:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM handovers WHERE job_id = ?", (job_id,))
                self._conn.execute("DELETE FROM payments WHERE job_id = ?", (job_id,))
                self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return job

    # ---- Projekte (mehrere Drucke als ein Auftrag) -------------------------------

    def projects(self, limit: int = 50) -> list[dict[str, Any]]:
        """Projekte, neueste zuerst (für „Zu bestehendem Projekt“)."""
        rows = self._conn.execute(
            "SELECT jobs.id, jobs.code, jobs.task_name, jobs.start_ts, jobs.customer_id, customers.name AS customer_name, "
            "(SELECT COUNT(*) FROM jobs k WHERE k.project_id = jobs.id) n FROM jobs "
            "LEFT JOIN customers ON customers.id = jobs.customer_id WHERE jobs.kind = 'project' "
            "ORDER BY jobs.start_ts DESC, jobs.id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def children(self, project_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """Drucke je Projekt (gleiche Felder wie list_jobs), nach Startzeit."""
        out: dict[int, list[dict[str, Any]]] = {}
        if not project_ids:
            return out
        marks = ", ".join("?" for _ in project_ids)
        rows = self._conn.execute(
            f"SELECT jobs.*, customers.name AS customer_name, customers.margin_pct AS customer_margin, COALESCE(customers.no_billing, 0) AS customer_nobill, "
            f"{Store.SCRAP_SQL} AS is_scrap, {Store.DELIVERED_SQL} AS delivered, {Store.PAID_SQL} AS paid FROM jobs "
            f"LEFT JOIN customers ON customers.id = jobs.customer_id WHERE jobs.project_id IN ({marks}) "
            "ORDER BY jobs.start_ts, jobs.id", project_ids).fetchall()
        for r in rows:
            out.setdefault(r["project_id"], []).append(dict(r))
        return out

    def project_candidates(self, project: dict[str, Any], limit: int = 40) -> list[dict[str, Any]]:
        """Drucke, die zu einem Projekt dazu passen: noch in keinem Projekt, gleicher Kunde und zeitlich nah zuerst."""
        rows = self._conn.execute(
            "SELECT id, code, task_name, start_ts, printer, printer_name, customer_id FROM jobs "
            "WHERE kind IS NULL AND project_id IS NULL "
            "ORDER BY (customer_id IS NOT NULL AND customer_id = ?) DESC, ABS(COALESCE(start_ts, 0) - ?) LIMIT ?",
            (project.get("customer_id"), project.get("start_ts") or time.time(), limit)).fetchall()
        return [dict(r) for r in rows]

    def _flatten(self, ids: list[int]) -> tuple[list[int], list[int]]:
        """(Drucke, Projekte) aus einer Auswahl; ausgewählte Projekte bringen ihre Drucke mit."""
        jobs: list[int] = []
        projects: list[int] = []
        for i in dict.fromkeys(ids):
            row = self._conn.execute("SELECT id, kind FROM jobs WHERE id = ?", (i,)).fetchone()
            if not row:
                continue
            if row["kind"] == "project":
                projects.append(i)
                jobs += [r[0] for r in self._conn.execute("SELECT id FROM jobs WHERE project_id = ?", (i,))]
            else:
                jobs.append(i)
        return list(dict.fromkeys(jobs)), projects

    def create_project(self, ids: list[int], name: str | None = None, customer_id: int | None = None,
                       quantity: int | None = None, into: int | None = None) -> int | None:
        """Drucke zu einem Projekt zusammenfassen (``into`` = vorhandenes Projekt, sonst neues).

        Zahlungen der Drucke wandern ins Projekt. Ausgewählte andere Projekte werden aufgelöst und
        ihre Drucke (samt Zahlungen) übernommen.
        """
        jobs, projects = self._flatten(ids)
        jobs = [j for j in jobs if (self._conn.execute("SELECT kind FROM jobs WHERE id = ?", (j,)).fetchone() or [None])[0] != "share"]
        for j in jobs + projects:
            if into is None or j != into:
                self.unsplit(j)  # aufgeteilte Drucke: Abrechnung läuft danach über das Projekt
        if into is None and len(projects) == 1:
            into, projects = projects[0], []
        if into is not None:
            projects = [p for p in projects if p != into]
            if not self._conn.execute("SELECT 1 FROM jobs WHERE id = ? AND kind = 'project'", (into,)).fetchone():
                return None
        if not jobs and into is None:
            return None
        rows = [dict(r) for r in self._conn.execute(
            f"SELECT *, {Store.DELIVERED_SQL} AS delivered FROM jobs WHERE id IN ({', '.join('?' for _ in jobs)}) ORDER BY start_ts, id", jobs)] if jobs else []
        if into is None:
            first = rows[0]
            if customer_id is None:
                cids = [r["customer_id"] for r in rows if r["customer_id"]]
                customer_id = max(set(cids), key=cids.count) if cids else None
            all_gift = all(r.get("gift") for r in rows)
            into = self.insert(kind="project", printer="", state="done", status="fertig",
                               task_name=(name or first.get("task_name") or "Projekt").strip(), customer_id=customer_id,
                               quantity=quantity, gift=1 if all_gift else None, start_ts=first.get("start_ts"))
            done = [r for r in rows if r["state"] == "done"]
            if done and all((r["delivered"] or 0) >= (r.get("quantity") or 1) for r in done) and len(done) == len(rows) \
                    and self._conn.execute("SELECT 1 FROM handovers WHERE job_id IN (%s)" % ", ".join("?" for _ in jobs), jobs).fetchone():
                self.add_handover(into, quantity or 1)  # alles schon übergeben: Projekt gilt als übergeben
        else:
            customer_id = self._conn.execute("SELECT customer_id FROM jobs WHERE id = ?", (into,)).fetchone()[0]
        with self._lock, self._conn:
            for p in projects:
                self._conn.execute("UPDATE jobs SET project_id = ? WHERE project_id = ? AND kind = 'share'", (into, p))
                self._conn.execute("UPDATE payments SET job_id = ? WHERE job_id = ?", (into, p))
                self._conn.execute("UPDATE photos SET job_id = ? WHERE job_id = ?", (into, p))
                self._conn.execute("DELETE FROM handovers WHERE job_id = ?", (p,))
                self._conn.execute("UPDATE jobs SET project_id = NULL WHERE project_id = ?", (p,))
                self._conn.execute("DELETE FROM jobs WHERE id = ?", (p,))
            for j in jobs:
                self._conn.execute("UPDATE payments SET job_id = ? WHERE job_id = ?", (into, j))
                self._conn.execute("UPDATE jobs SET project_id = ?, customer_id = ? WHERE id = ?", (into, customer_id, j))
        self.refresh_project(into)
        return into

    def remove_from_project(self, job_id: int) -> int | None:
        """Druck aus seinem Projekt lösen; ein leeres Projekt wird aufgelöst. Gibt die Projekt-ID zurück."""
        row = self._conn.execute("SELECT project_id, kind FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row or not row[0]:
            return None
        pid = row[0]
        if row["kind"] == "share":  # Stück aus einem aufgeteilten Druck: gehen zurück in dessen Vorrat
            self.remove_share(job_id)
            return pid
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET project_id = NULL WHERE id = ?", (job_id,))
        if self._conn.execute("SELECT 1 FROM jobs WHERE project_id = ?", (pid,)).fetchone():
            self.refresh_project(pid)
        else:
            self._drop_project(pid, job_id)
        return pid

    def dissolve_project(self, project_id: int) -> list[int]:
        """Projekt auflösen: Drucke werden wieder einzeln. Zahlungen und Fotos gehen an den ersten Druck,
        eine vollständige Übergabe gilt für alle Drucke."""
        self.unsplit(project_id)
        with self._lock, self._conn:  # Stück aus aufgeteilten Drucken gehen zurück in deren Vorrat
            self._conn.execute("DELETE FROM jobs WHERE project_id = ? AND kind = 'share'", (project_id,))
        kids = [r[0] for r in self._conn.execute(
            f"SELECT id FROM jobs WHERE project_id = ? ORDER BY {Store.SCRAP_SQL}, start_ts, id", (project_id,))]
        proj = self.get(project_id)
        if not proj or proj.get("kind") != "project":
            return []
        handed = (proj.get("delivered") or 0) >= (proj.get("quantity") or 1)
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET project_id = NULL WHERE project_id = ?", (project_id,))
            if handed:
                for k in kids:
                    self._conn.execute(
                        "INSERT INTO handovers (job_id, qty, ts, note) SELECT id, MAX(COALESCE(quantity,1) - "
                        "(SELECT COALESCE(SUM(qty),0) FROM handovers h WHERE h.job_id = jobs.id), 0), ?, NULL FROM jobs WHERE id = ? "
                        "AND COALESCE(quantity,1) > (SELECT COALESCE(SUM(qty),0) FROM handovers h WHERE h.job_id = jobs.id)",
                        (time.time(), k))
        self._drop_project(project_id, kids[0] if kids else None)
        return kids

    def _drop_project(self, project_id: int, heir: int | None) -> None:
        with self._lock, self._conn:
            if heir:
                self._conn.execute("UPDATE payments SET job_id = ? WHERE job_id = ?", (heir, project_id))
                self._conn.execute("UPDATE photos SET job_id = ? WHERE job_id = ?", (heir, project_id))
            self._conn.execute("DELETE FROM handovers WHERE job_id = ?", (project_id,))
            if not heir:
                self._conn.execute("DELETE FROM payments WHERE job_id = ?", (project_id,))
            self._conn.execute("DELETE FROM jobs WHERE id = ? AND kind = 'project'", (project_id,))

    def refresh_project(self, project_id: int) -> None:
        """Summen eines Projekts aus seinen Drucken neu berechnen (Kosten, Zeitraum, Bild, Preisbasis)."""
        proj = self._conn.execute(
            "SELECT jobs.*, customers.margin_pct AS customer_margin FROM jobs LEFT JOIN customers ON customers.id = jobs.customer_id "
            "WHERE jobs.id = ? AND jobs.kind = 'project'", (project_id,)).fetchone()
        if not proj:
            return
        kids = [dict(r) for r in self._conn.execute(
            f"SELECT *, {Store.SCRAP_SQL} AS is_scrap FROM jobs WHERE project_id = ? ORDER BY start_ts, id", (project_id,))]
        if not kids:
            return

        def total(col: str) -> float | None:
            vals = [k[col] for k in kids if k[col] is not None]
            return round(sum(vals), 4) if vals else None

        margin = 1 + (proj["customer_margin"] or 0) / 100
        base = 0.0
        for k in kids:
            c = k["cost_total"] or 0.0
            if not k["is_scrap"] or k.get("defect_charge") == "full":
                base += c
            elif k.get("defect_charge") == "pct":
                base += c * (k.get("defect_value") or 0) / 100
            elif k.get("defect_charge") == "amount":
                base += (k.get("defect_value") or 0) / margin
        running = any(k["state"] != "done" for k in kids)
        ends = [k["end_ts"] for k in kids if k["end_ts"]]
        fields = {
            "start_ts": min((k["start_ts"] for k in kids if k["start_ts"]), default=proj["start_ts"]),
            "end_ts": None if running or not ends else max(ends),
            "status": "läuft" if running else "fertig",
            "image": next((k["image"] for k in kids if k["image"] and not k["is_scrap"]), None)
                     or next((k["image"] for k in kids if k["image"]), None),
            "kwh": total("kwh"), "cost": total("cost"), "cost_filament": total("cost_filament"),
            "cost_printer": total("cost_printer"), "cost_total": total("cost_total"),
            "filament_g": round(sum((k["filament_g"] if k["filament_g"] is not None else k["weight_g"] or 0) for k in kids), 2),
            "bill_base": round(base, 4) if total("cost_total") is not None else None,
        }
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", [*fields.values(), project_id])
        if proj["split"]:
            self.refresh_shares(project_id)

    def refresh_projects(self) -> None:
        for (pid,) in self._conn.execute("SELECT id FROM jobs WHERE kind = 'project'").fetchall():
            self.refresh_project(pid)
        for (jid,) in self._conn.execute("SELECT id FROM jobs WHERE split = 1 AND kind IS NULL").fetchall():
            self.refresh_shares(jid)

    # ---- Aufteilen: Stückzahl auf Kunden und Vorrat ---------------------------
    #
    # Ein aufgeteilter Druck (oder ein Projekt) hat split = 1. Jeder Kunde bekommt einen Anteil: eine eigene
    # jobs-Zeile mit kind = 'share', share_of = Druck/Projekt, quantity = Stück (beim Projekt: Sätze) und
    # anteiligen Kosten in bill_base/cost_total. Preis, Zahlungen, Übergaben und „verschenkt“ laufen dann pro
    # Anteil über die üblichen Spalten und Tabellen. Was keinem Anteil gehört, liegt im Vorrat.
    # share_part = ein einzelner Druck aus einem Projekt (Einzelteile, z. B. nur ein Kopf).

    SHARE_KINDS = ("split", "stock", "out")

    def shares(self, parent_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """Anteile je aufgeteiltem Druck/Projekt (gleiche Felder wie get), älteste zuerst."""
        out: dict[int, list[dict[str, Any]]] = {}
        if not parent_ids:
            return out
        marks = ", ".join("?" for _ in parent_ids)
        rows = self._conn.execute(
            f"SELECT jobs.*, customers.name AS customer_name, customers.margin_pct AS customer_margin, COALESCE(customers.no_billing, 0) AS customer_nobill, "
            f"{Store.SCRAP_SQL} AS is_scrap, {Store.DELIVERED_SQL} AS delivered, {Store.PAID_SQL} AS paid, "
            "(SELECT p.task_name FROM jobs p WHERE p.id = jobs.project_id) AS project_name, "
            "(SELECT p.code FROM jobs p WHERE p.id = jobs.project_id) AS project_code FROM jobs "
            f"LEFT JOIN customers ON customers.id = jobs.customer_id WHERE jobs.kind = 'share' AND jobs.share_of IN ({marks}) "
            "ORDER BY jobs.start_ts, jobs.id", parent_ids).fetchall()
        for r in rows:
            out.setdefault(r["share_of"], []).append(dict(r))
        return out

    def _unit(self, src: dict[str, Any], sets: bool) -> tuple[float | None, float | None]:
        """(Kosten, Gramm) für ein Stück eines Drucks bzw. einen Satz eines Projekts."""
        n = src.get("quantity") or 1
        base = src.get("bill_base") if sets and src.get("bill_base") is not None else src.get("cost_total")
        grams = src.get("filament_g") if src.get("filament_g") is not None else src.get("weight_g")
        return (None if base is None else base / n), (None if grams is None else grams / n)

    def refresh_shares(self, parent_id: int) -> None:
        """Kosten, Name und Bild der Anteile aus dem aufgeteilten Druck/Projekt neu berechnen."""
        parent = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (parent_id,)).fetchone()
        if not parent:
            return
        parent = dict(parent)
        rows = self._conn.execute("SELECT id, quantity, share_part, share_kind, project_id FROM jobs WHERE kind = 'share' AND share_of = ?",
                                  (parent_id,)).fetchall()
        cache: dict[int, dict[str, Any] | None] = {}
        with self._lock, self._conn:
            for r in rows:
                src, name, image = parent, parent.get("task_name"), parent.get("image")
                if r["share_part"]:
                    if r["share_part"] not in cache:
                        k = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (r["share_part"],)).fetchone()
                        cache[r["share_part"]] = dict(k) if k else None
                    k = cache[r["share_part"]]
                    if k:
                        src, image = k, k.get("image") or image
                        name = f'{parent.get("task_name") or ""} · {k.get("task_name") or ""}'
                unit, grams = self._unit(src, sets=src is parent and parent.get("kind") == "project")
                q = r["quantity"] or 1
                cost = None if unit is None else round(unit * q, 4)
                self._conn.execute(
                    "UPDATE jobs SET task_name = ?, image = ?, cost_total = ?, bill_base = ?, filament_g = ? WHERE id = ?",
                    (name, image, cost, cost, None if grams is None else round(grams * q, 2), r["id"]))
                if r["share_kind"] == "project":
                    # Stück für ein Projekt: wie ein eigener Druck mit anteiligen Werten (Projekt summiert sie)
                    self._conn.execute(
                        "UPDATE jobs SET printer = ?, printer_name = ?, end_ts = ?, kwh = ?, cost = ?, cost_filament = ?, "
                        "cost_printer = ?, materials = ? WHERE id = ?",
                        (src.get("printer") or "", src.get("printer_name"), src.get("end_ts"),
                         *(None if src.get(c) is None else round(src[c] * q / (src.get("quantity") or 1), 4)
                           for c in ("kwh", "cost", "cost_filament", "cost_printer")),
                         _scaled_materials(src.get("materials"), q / (src.get("quantity") or 1)), r["id"]))
        for pid in {r["project_id"] for r in rows if r["share_kind"] == "project" and r["project_id"]}:
            self.refresh_project(pid)

    def stock(self, parent: dict[str, Any], shares: list[dict[str, Any]] | None = None,
              kids: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Vorrat eines aufgeteilten Drucks/Projekts.

        ``units`` = Stück (beim Projekt: ganze Sätze) im Vorrat, ``total`` = Stückzahl bzw. Sätze,
        ``loose`` = {Druck-ID: einzelne Teile} beim Projekt, ``per`` = Teile je Satz.
        """
        shares = self.shares([parent["id"]]).get(parent["id"], []) if shares is None else shares
        total = parent.get("quantity") or 1
        if parent.get("kind") != "project":
            return {"units": total - sum(s.get("quantity") or 0 for s in shares), "total": total, "loose": {}, "per": {}}
        if kids is None:
            kids = [dict(r) for r in self._conn.execute(
                f"SELECT *, {Store.SCRAP_SQL} AS is_scrap FROM jobs WHERE project_id = ? ORDER BY start_ts, id", (parent["id"],))]
        kids = [k for k in kids if not k.get("is_scrap")]  # Fehldrucke liefern keine Teile
        per = {k["id"]: (k.get("quantity") or 1) / total for k in kids}
        sets_used = sum(s.get("quantity") or 0 for s in shares if not s.get("share_part"))
        left = {k["id"]: (k.get("quantity") or 1) - sets_used * per[k["id"]] for k in kids}
        for s in shares:
            if s.get("share_part") in left:
                left[s["share_part"]] -= s.get("quantity") or 0
        if kids:
            units = math.floor(min(left[i] / per[i] for i in left) + 1e-6)
        else:
            units = total - sets_used
        full = max(units, 0)
        loose = {i: round(left[i] - full * per[i], 4) for i in left}
        return {"units": units, "total": total, "loose": {i: n for i, n in loose.items() if abs(n) > 1e-6}, "per": per}

    def _new_share(self, parent: dict[str, Any], customer_id: int | None, qty: int, kind: str,
                   part: int | None = None, ts: float | None = None, **extra: Any) -> int:
        sid = self.insert(kind="share", printer="", state="done", status="fertig", share_of=parent["id"], share_part=part,
                          share_kind=kind, customer_id=customer_id, quantity=qty, start_ts=ts or time.time(),
                          task_name=parent.get("task_name"), image=parent.get("image"), **extra)
        return sid

    def set_split(self, parent_id: int, rows: list[dict[str, Any]]) -> str | None:
        """Aufteilung speichern. ``rows`` = [{"sid": Anteil oder None, "customer_id", "qty"}] für die Anteile
        ganzer Stücke/Sätze mit Kunde; fehlende Anteile werden entfernt. Gibt einen Fehlertext oder None zurück.

        Beim ersten Aufteilen bekommt der Anteil des bisherigen Kunden dessen Zahlungen, Übergaben und Endpreis.
        """
        parent = self.get(parent_id)
        if not parent or parent.get("kind") == "share" or parent.get("project_id"):
            return "nicht aufteilbar"
        if parent.get("kind") != "project" and parent.get("is_scrap"):
            return "Fehldrucke lassen sich nicht aufteilen"
        old = self.shares([parent_id]).get(parent_id, [])
        editable = {s["id"]: s for s in old if s.get("share_kind") == "project"
                    or (s.get("customer_id") and not s.get("share_part") and s.get("share_kind") != "out")}
        for r in rows:
            if r.get("project_id") and not (r.get("sid") and int(r["sid"]) in editable):
                if parent.get("kind") == "project":
                    return "Ein Projekt lässt sich nicht in ein anderes Projekt aufteilen"
                if not self._conn.execute("SELECT 1 FROM jobs WHERE id = ? AND kind = 'project'", (int(r["project_id"]),)).fetchone():
                    return "Projekt nicht gefunden"
            if r.get("new_project") and parent.get("kind") == "project":
                return "Ein Projekt lässt sich nicht in ein anderes Projekt aufteilen"
        keep = {int(r["sid"]) for r in rows if r.get("sid")}
        drop = [s for sid, s in editable.items() if sid not in keep]
        for s in drop:
            if self._conn.execute("SELECT 1 FROM payments WHERE job_id = ? UNION SELECT 1 FROM handovers WHERE job_id = ?",
                                  (s["id"], s["id"])).fetchone():
                return "Anteil mit Zahlungen oder Übergaben kann nicht entfernt werden"
        # Vorrat prüfen: neue Stückzahlen dürfen nicht mehr als vorhanden vergeben
        trial = [s for s in old if s["id"] not in editable]
        for r in rows:
            q = int(r.get("qty") or 0)
            if q < 1:
                return "Stückzahl fehlt"
            trial.append({"quantity": q, "share_part": None})
            if r.get("sid") and int(r["sid"]) in editable:
                s = editable[int(r["sid"])]
                if (s.get("delivered") or 0) > q:
                    return "Schon mehr Stück übergeben"
        if self.stock(parent, trial)["units"] < 0:
            return "Mehr vergeben als vorhanden"
        first = not parent.get("split")
        if first and parent.get("customer_id") and not any(int(r.get("customer_id") or 0) == parent["customer_id"] for r in rows) \
                and self._conn.execute("SELECT 1 FROM payments WHERE job_id = ? UNION SELECT 1 FROM handovers WHERE job_id = ?",
                                       (parent_id, parent_id)).fetchone():
            return "Der bisherige Kunde hat schon Zahlungen oder Übergaben, bitte in der Aufteilung lassen"
        heir = None
        with self._lock, self._conn:
            for s in drop:
                self._conn.execute("DELETE FROM jobs WHERE id = ? AND kind = 'share'", (s["id"],))
            self._conn.execute("UPDATE jobs SET split = 1 WHERE id = ?", (parent_id,))
        self._tidy_projects({s["project_id"] for s in drop if s.get("project_id")})
        for r in rows:
            q = int(r["qty"])
            if r.get("sid") and int(r["sid"]) in editable and editable[int(r["sid"])].get("share_kind") == "project":
                self.update(int(r["sid"]), quantity=q)
            elif r.get("project_id") or r.get("new_project"):
                pid = int(r["project_id"]) if r.get("project_id") else self.new_project(str(r["new_project"]), parent)
                cid = self._conn.execute("SELECT customer_id FROM jobs WHERE id = ?", (pid,)).fetchone()[0]
                self._new_share(parent, cid, q, "project", ts=parent.get("start_ts"), project_id=pid)
            elif r.get("sid") and int(r["sid"]) in editable:
                cid = r.get("customer_id") or editable[int(r["sid"])]["customer_id"]
                self.update(int(r["sid"]), quantity=q, customer_id=cid)
            elif r.get("customer_id"):
                sid = self._new_share(parent, int(r["customer_id"]), q, "split", ts=parent.get("start_ts"))
                if first and heir is None and parent.get("customer_id") == int(r["customer_id"]):
                    heir = sid
        if first and parent.get("customer_id"):
            # Bisheriger Kunde: Zahlungen, Übergaben, Endpreis und „verschenkt“ gehen auf seinen Anteil
            fields: dict[str, Any] = {"customer_id": None, "final_price": None, "gift": None}
            with self._lock, self._conn:
                if heir:
                    self._conn.execute("UPDATE payments SET job_id = ? WHERE job_id = ?", (heir, parent_id))
                    self._conn.execute("UPDATE handovers SET job_id = ? WHERE job_id = ?", (heir, parent_id))
            if heir:
                hq = self.get(heir)["quantity"] or 1
                fp = parent.get("final_price")
                self.update(heir, gift=parent.get("gift"),
                            final_price=None if fp is None else round(fp * hq / (parent.get("quantity") or 1), 2))
            self.update(parent_id, **fields)
        self.refresh_shares(parent_id)
        return None

    def new_project(self, name: str, like: dict[str, Any] | None = None) -> int:
        """Leeres Projekt anlegen (für Stück aus einem aufgeteilten Druck)."""
        return self.insert(kind="project", printer="", state="done", status="fertig", task_name=name.strip() or "Projekt",
                           quantity=1, start_ts=(like or {}).get("start_ts") or time.time())

    def _tidy_projects(self, pids: set[int]) -> None:
        """Nach dem Entfernen von Stück: Projekte neu rechnen, leere Projekte auflösen."""
        for pid in pids:
            if self._conn.execute("SELECT 1 FROM jobs WHERE project_id = ?", (pid,)).fetchone():
                self.refresh_project(pid)
            else:
                self._drop_project(pid, None)

    def unsplit(self, parent_id: int) -> None:
        """Aufteilung aufheben: Kunde, Zahlungen und Übergaben des ersten Anteils mit Kunde gehen zurück an den Druck."""
        parent = self.get(parent_id)
        if not parent or not parent.get("split"):
            return
        old = self.shares([parent_id]).get(parent_id, [])
        heir = next((s for s in old if s.get("customer_id") and s.get("share_kind") != "project"), None)
        with self._lock, self._conn:
            for s in old:
                self._conn.execute("UPDATE payments SET job_id = ? WHERE job_id = ?", (parent_id, s["id"]))
                self._conn.execute("DELETE FROM handovers WHERE job_id = ?", (s["id"],))
                self._conn.execute("DELETE FROM jobs WHERE id = ?", (s["id"],))
            self._conn.execute("UPDATE jobs SET split = NULL WHERE id = ?", (parent_id,))
        self._tidy_projects({s["project_id"] for s in old if s.get("project_id")})
        if heir:
            self.update(parent_id, customer_id=heir["customer_id"], gift=heir.get("gift"))

    def give(self, parent_id: int, customer_id: int | None, qty: int, part: int | None = None,
             final_price: float | None = None, handed: bool = False, note: str | None = None) -> int | str:
        """Aus dem Vorrat abgeben (mit Kunde) oder ausbuchen (ohne Kunde). Gibt die Anteil-ID oder einen Fehlertext zurück."""
        parent = self.get(parent_id)
        if not parent or not parent.get("split"):
            return "nicht aufgeteilt"
        if qty < 1:
            return "Stückzahl fehlt"
        st = self.stock(parent)
        if part:
            if parent.get("kind") != "project" or part not in st["per"]:
                return "Teil gehört nicht zum Projekt"
            avail = st["loose"].get(part, 0) + max(st["units"], 0) * st["per"][part]
        else:
            avail = st["units"]
        if qty > avail + 1e-6:
            return "Nicht genug im Vorrat"
        sid = self._new_share(parent, customer_id, qty, "stock" if customer_id else "out", part=part,
                              final_price=final_price, job_note=note)
        if handed and customer_id:
            self.add_handover(sid, qty)
        self.refresh_shares(parent_id)
        return sid

    def busy_ids(self, ids: list[int]) -> set[int]:
        """IDs mit Zahlungen oder Übergaben (lassen sich nicht einfach entfernen)."""
        if not ids:
            return set()
        marks = ", ".join("?" for _ in ids)
        rows = self._conn.execute(f"SELECT job_id FROM payments WHERE job_id IN ({marks}) UNION "
                                  f"SELECT job_id FROM handovers WHERE job_id IN ({marks})", [*ids, *ids]).fetchall()
        return {r[0] for r in rows}

    def remove_share(self, share_id: int) -> bool:
        """Anteil löschen (nur ohne Zahlungen und Übergaben); die Stück gehen zurück in den Vorrat."""
        row = self._conn.execute("SELECT id, project_id FROM jobs WHERE id = ? AND kind = 'share'", (share_id,)).fetchone()
        if not row or self._conn.execute("SELECT 1 FROM payments WHERE job_id = ? UNION SELECT 1 FROM handovers WHERE job_id = ?",
                                         (share_id, share_id)).fetchone():
            return False
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (share_id,))
        if row["project_id"]:
            self._tidy_projects({row["project_id"]})
        return True

    def stock_items(self) -> list[dict[str, Any]]:
        """Alle aufgeteilten Drucke/Projekte mit Vorrat (Stück, Sätze oder Einzelteile)."""
        rows = [dict(r) for r in self._conn.execute(
            f"SELECT jobs.*, {Store.SCRAP_SQL} AS is_scrap FROM jobs WHERE split = 1 AND kind IS NOT 'share' "
            "ORDER BY start_ts DESC, id DESC")]
        shares = self.shares([r["id"] for r in rows])
        kids = self.children([r["id"] for r in rows if r.get("kind") == "project"])
        out = []
        for r in rows:
            st = self.stock(r, shares.get(r["id"], []), kids.get(r["id"], []))
            if st["units"] > 0 or any(n > 1e-6 for n in st["loose"].values()):
                r["_shares"], r["_stock"], r["_kids"] = shares.get(r["id"], []), st, kids.get(r["id"], [])
                out.append(r)
        return out

    def stock_given(self, since: float) -> int:
        """Wie oft seit ``since`` aus dem Vorrat abgegeben oder ausgebucht wurde."""
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind = 'share' AND share_kind IN ('stock', 'out') AND start_ts >= ?",
            (since,)).fetchone()[0])

    # ---- Übergaben ---------------------------------------------------------

    def handovers(self, job_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM handovers WHERE job_id = ? ORDER BY ts, id", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_handover(self, job_id: int, qty: int, ts: float | None = None, note: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO handovers (job_id, qty, ts, note) VALUES (?, ?, ?, ?)",
                               (job_id, qty, ts or time.time(), note))

    def delete_handover(self, handover_id: int) -> int | None:
        row = self._conn.execute("SELECT job_id FROM handovers WHERE id = ?", (handover_id,)).fetchone()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM handovers WHERE id = ?", (handover_id,))
        return row["job_id"] if row else None

    def payments(self, job_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM payments WHERE job_id = ? ORDER BY ts, id", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_payment(self, job_id: int, amount: float, ts: float | None = None, note: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO payments (job_id, amount, ts, note) VALUES (?, ?, ?, ?)",
                               (job_id, round(amount, 2), ts or time.time(), note))

    def delete_payment(self, payment_id: int) -> int | None:
        row = self._conn.execute("SELECT job_id FROM payments WHERE id = ?", (payment_id,)).fetchone()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
        return row["job_id"] if row else None

    def clear_payments(self, job_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM payments WHERE job_id = ?", (job_id,))

    def clear_handovers(self, job_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM handovers WHERE job_id = ?", (job_id,))

    # ---- Lesen -----------------------------------------------------------

    def get(self, job_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"SELECT jobs.*, customers.name AS customer_name, customers.margin_pct AS customer_margin, COALESCE(customers.no_billing, 0) AS customer_nobill, "
            f"{Store.SCRAP_SQL} AS is_scrap, {Store.DELIVERED_SQL} AS delivered, {Store.PAID_SQL} AS paid FROM jobs LEFT JOIN customers ON customers.id = jobs.customer_id "
            "WHERE jobs.id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def open_job(self, printer: str, state: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE printer = ? AND state = ? ORDER BY id DESC LIMIT 1",
            (printer, state),
        ).fetchone()
        return dict(row) if row else None

    def job_during(self, printer: str, ts: float) -> dict[str, Any] | None:
        """Job dieses Druckers, der zum Zeitpunkt ``ts`` lief (Start ≤ ts ≤ Ende bzw. noch offen)."""
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE printer = ? AND start_ts <= ? "
            "AND (end_ts IS NULL AND state = 'running' OR end_ts >= ?) "
            "ORDER BY id DESC LIMIT 1",
            (printer, ts, ts),
        ).fetchone()
        return dict(row) if row else None

    def job_near(self, printer: str, column: str, ts: float, tolerance_s: float) -> dict[str, Any] | None:
        """Job dieses Druckers, dessen start_ts/end_ts nahe an ``ts`` liegt."""
        if column not in ("start_ts", "end_ts"):
            raise ValueError(column)
        row = self._conn.execute(
            f"SELECT * FROM jobs WHERE printer = ? AND {column} BETWEEN ? AND ? "
            "ORDER BY id DESC LIMIT 1",
            (printer, ts - tolerance_s, ts + tolerance_s),
        ).fetchone()
        if not row:
            # Auch Drucke im Papierkorb zählen, sonst holt die Wiederherstellung nach Redeploy sie zurück.
            row = self._conn.execute(
                f"SELECT data FROM trash WHERE printer = ? AND {column} BETWEEN ? AND ? LIMIT 1",
                (printer, ts - tolerance_s, ts + tolerance_s)).fetchone()
            return json.loads(row["data"]) if row else None
        return dict(row) if row else None

    def unfinished_jobs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE state IN ('running', 'closing') ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def missing_start_jobs(self, since_ts: float) -> list[dict[str, Any]]:
        """Abgeschlossene Drucke, bei denen nur der Startwert des Zählers fehlt (Druckstart nicht empfangen)."""
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE state = 'done' AND start_kwh IS NULL AND end_kwh IS NOT NULL "
            "AND start_ts >= ? AND note LIKE '%Druckstart nicht empfangen%' ORDER BY id",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]

    def stale_closing(self, older_than_ts: float) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE state = 'closing' AND end_ts < ?", (older_than_ts,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_jobs(self, limit: int = 500, offset: int = 0, **filters: Any) -> list[dict[str, Any]]:
        where, args = self._filters(**filters)
        rows = self._conn.execute(
            f"SELECT jobs.*, customers.name AS customer_name, customers.margin_pct AS customer_margin, COALESCE(customers.no_billing, 0) AS customer_nobill, "
            f"{Store.SCRAP_SQL} AS is_scrap, {Store.DELIVERED_SQL} AS delivered, {Store.PAID_SQL} AS paid FROM jobs "
            f"LEFT JOIN customers ON customers.id = jobs.customer_id {where} "
            "ORDER BY start_ts DESC, jobs.id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        return [dict(r) for r in rows]

    def count_jobs(self, **filters: Any) -> int:
        where, args = self._filters(**filters)
        return self._conn.execute(f"SELECT COUNT(*) FROM jobs {where}", args).fetchone()[0]

    def summary(self, **filters: Any) -> dict[str, Any]:
        where, args = self._filters(**{**filters, "real": "1"})
        total = self._conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(kwh),0) kwh, COALESCE(SUM(cost),0) cost, "
            f"COALESCE(SUM(COALESCE(filament_g, weight_g)),0) weight, "
            f"COALESCE(SUM(cost_printer),0) cost_printer, COALESCE(SUM(cost_filament),0) cost_filament, "
            f"COALESCE(SUM(cost_total),0) cost_total, "
            f"COALESCE(SUM(CASE WHEN {Store.SCRAP_SQL} THEN 1 ELSE 0 END),0) scrap_n, "
            f"COALESCE(SUM(CASE WHEN {Store.SCRAP_SQL} THEN cost_total ELSE 0 END),0) scrap_cost, "
            f"COALESCE(SUM(CASE WHEN {Store.SCRAP_SQL} AND NOT {Store.NOCHARGE_SQL} AND jobs.customer_id IS NOT NULL "
            f"AND NOT {Store.NOBILL_SQL} AND COALESCE(jobs.gift, 0) = 0 AND jobs.project_id IS NULL THEN {Store.DUE_SQL} ELSE 0 END),0) scrap_charged FROM jobs {where}",
            args,
        ).fetchone()
        per_printer = self._conn.execute(
            f"SELECT printer, MAX(printer_name) printer_name, COUNT(*) n, "
            f"COALESCE(SUM(kwh),0) kwh, COALESCE(SUM(cost),0) cost, COALESCE(SUM(cost_total),0) cost_total "
            f"FROM jobs {where} GROUP BY printer ORDER BY cost_total DESC, cost DESC",
            args,
        ).fetchall()
        return {"total": dict(total), "per_printer": [dict(r) for r in per_printer]}

    def months(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT strftime('%Y-%m', start_ts, 'unixepoch', 'localtime') m "
            "FROM jobs WHERE start_ts IS NOT NULL ORDER BY m DESC"
        ).fetchall()
        return [r["m"] for r in rows]

    def printers(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT printer, MAX(printer_name) printer_name FROM jobs WHERE kind IS NULL GROUP BY printer ORDER BY printer"
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _filters(
        printer: str | None = None,
        month: str | None = None,
        status: str | None = None,
        material: str | None = None,
        customer: str | None = None,
        day: str | None = None,
        q: str | None = None,
        paid: str | None = None,
        handover: str | None = None,
        pending: str | None = None,
        since: float | None = None,
        until: float | None = None,
        real: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if real:
            clauses.append(Store.REAL_SQL)
        # Anteile aufgeteilter Drucke stehen nur in „Offen“ einzeln, sonst zählt der aufgeteilte Druck.
        listing = not pending
        if listing:
            clauses.append(Store.NOSHARE_SQL)

        def any_share(cond: str) -> str:
            """In der Liste passt ein aufgeteilter Druck, wenn einer seiner Anteile passt."""
            if not listing:
                return cond
            return (f"({cond} OR (COALESCE(jobs.split, 0) = 1 AND EXISTS (SELECT 1 FROM jobs s WHERE s.share_of = jobs.id "
                    f"AND {cond.replace('jobs.', 's.')})))")
        local_day = "strftime('%Y-%m-%d', jobs.start_ts, 'unixepoch', 'localtime')"
        if printer:
            clauses.append("jobs.printer = ?")
            args.append(printer)
        if month:
            clauses.append("strftime('%Y-%m', jobs.start_ts, 'unixepoch', 'localtime') = ?")
            args.append(month)
        if day:
            clauses.append(f"{local_day} = ?")
            args.append(day)
        if status == "ausschuss":
            clauses.append(f"{Store.SCRAP_SQL} = 1")
        elif status and status.startswith("fd:"):
            # Fehldrucke einer Ursache; fd:none = ohne Angabe
            clauses.append(f"{Store.SCRAP_SQL} = 1")
            if status == "fd:none":
                clauses.append("jobs.defect IS NULL")
            else:
                clauses.append("jobs.defect = ?")
                args.append(status[3:])
        elif status == "gut":
            clauses.append(f"{Store.SCRAP_SQL} = 0")
        elif status:
            clauses.append("jobs.status = ?")
            args.append(status)
        if material:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(jobs.materials) m "
                "WHERE json_extract(m.value, '$.type') = ?)"
            )
            args.append(material)
        if customer == "none":
            clauses.append(f"jobs.customer_id IS NULL AND {Store.UNSPLIT_SQL}")
        elif customer:
            clauses.append("(jobs.customer_id = ? OR EXISTS (SELECT 1 FROM jobs s WHERE s.share_of = jobs.id AND s.customer_id = ?))")
            args += [int(customer), int(customer)]
        if paid == "offen":
            clauses.append(any_share(f"{Store.PAYABLE_SQL} AND {Store.PAID_SQL} < {Store.DUE_SQL} - 0.005"))
        elif paid == "teilweise":
            clauses.append(any_share(f"{Store.PAYABLE_SQL} AND {Store.PAID_SQL} > 0 AND {Store.PAID_SQL} < {Store.DUE_SQL} - 0.005"))
        elif paid == "bezahlt":
            clauses.append(any_share(f"{Store.PAYABLE_SQL} AND {Store.PAID_SQL} >= {Store.DUE_SQL} - 0.005"))
        elif paid == "verschenkt":
            clauses.append(any_share(Store.GIFT_SQL))
        if handover == "offen":
            clauses.append(any_share(f"{Store.DELIVERABLE_SQL} AND {Store.DELIVERED_SQL} < {Store.QTY_SQL}"))
        elif handover == "teilweise":
            clauses.append(any_share(f"{Store.DELIVERABLE_SQL} AND {Store.DELIVERED_SQL} BETWEEN 1 AND {Store.QTY_SQL} - 1"))
        elif handover == "übergeben":
            clauses.append(any_share(f"{Store.DELIVERED_SQL} >= {Store.QTY_SQL}"))
        if pending:
            # Offene Posten: noch Geld zu bekommen oder noch Stück zu übergeben
            clauses.append(f"(({Store.PAYABLE_SQL} AND {Store.PAID_SQL} < {Store.DUE_SQL} - 0.005) OR "
                           f"({Store.DELIVERABLE_SQL} AND {Store.DELIVERED_SQL} < {Store.QTY_SQL}))")
        if since is not None:
            clauses.append("jobs.start_ts >= ?")
            args.append(since)
        if until is not None:
            clauses.append("jobs.start_ts < ?")
            args.append(until)
        for token in (q or "").split():
            # Jedes Wort muss irgendwo vorkommen, auch als Teilwort.
            clauses.append(
                "norm(COALESCE(jobs.task_name,'') || ' ' || COALESCE(jobs.printer_name,'') || ' ' || "
                "jobs.printer || ' ' || COALESCE(jobs.note,'') || ' ' || COALESCE(jobs.status,'') || ' ' || "
                "COALESCE(jobs.materials,'') || ' ' || COALESCE(jobs.job_note,'') || ' ' || COALESCE(jobs.material_notes,'') || ' ' || "
                "COALESCE(jobs.project_title,'') || ' ' || COALESCE(jobs.project_url,'') || ' ' || "
                "COALESCE(jobs.local_path,'') || ' pl-' || COALESCE(jobs.code,'') || ' ' || "
                "COALESCE((SELECT name FROM customers c WHERE c.id = jobs.customer_id),'') || ' ' || "
                "COALESCE((SELECT group_concat(c.name, ' ') FROM jobs s JOIN customers c ON c.id = s.customer_id "
                "WHERE s.share_of = jobs.id),'')) LIKE ?"
            )
            args.append("%" + norm(token) + "%")
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", args

    # ---- Kunden -----------------------------------------------------------

    def customers(self) -> list[dict[str, Any]]:
        P, DUE, PAID = (x.replace("jobs.", "j.") for x in (self.PAYABLE_SQL, self.DUE_SQL, self.PAID_SQL))
        rows = self._conn.execute(
            "SELECT c.*, COUNT(CASE WHEN j.kind IS NULL OR j.kind = 'share' THEN j.id END) n, "
            "COALESCE(SUM(CASE WHEN j.kind IS NULL OR j.kind = 'share' THEN j.cost_total END),0) cost_total, MAX(j.start_ts) last_ts, "
            f"COALESCE(SUM(CASE WHEN {P} THEN MAX({DUE} - {PAID}, 0) END),0) open_amount, "
            f"COALESCE(SUM(CASE WHEN {P} AND {PAID} < {DUE} - 0.005 THEN 1 END),0) open_n, "
            f"COALESCE(SUM({PAID}),0) paid_amount, "
            f"COALESCE(SUM(CASE WHEN {self.DELIVERABLE_SQL.replace('jobs.', 'j.')} AND "
            "(SELECT COALESCE(SUM(h.qty),0) FROM handovers h WHERE h.job_id = j.id) < COALESCE(j.quantity,1) "
            "THEN 1 END),0) undelivered_n "
            "FROM customers c LEFT JOIN jobs j ON j.customer_id = c.id "
            "GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def gifts_by_customer(self, since: float | None = None, until: float | None = None) -> dict[int | None, dict[str, Any]]:
        """Verschenktes pro Kunde (None = ohne Kunde) im Zeitraum: Anzahl, Stück, Kosten, Preis."""
        DUE = self.BASE_DUE_SQL
        q = (f"SELECT customer_id, COUNT(*) n, COALESCE(SUM({self.QTY_SQL}),0) pieces, COALESCE(SUM(cost_total),0) cost, "
             f"COALESCE(SUM({DUE}),0) value FROM jobs WHERE {self.GIFT_SQL} AND project_id IS NULL")
        args: list[Any] = []
        if since is not None:
            q += " AND start_ts >= ?"; args.append(since)
        if until is not None:
            q += " AND start_ts < ?"; args.append(until)
        rows = self._conn.execute(q + " GROUP BY customer_id", args).fetchall()
        return {r["customer_id"]: dict(r) for r in rows}

    def save_customer(self, customer_id: int | None, name: str, contact: str | None,
                      note: str | None, margin_pct: float | None = None, no_billing: bool = False) -> int:
        nb = 1 if no_billing else None
        with self._lock, self._conn:
            if customer_id:
                self._conn.execute(
                    "UPDATE customers SET name = ?, contact = ?, note = ?, margin_pct = ?, no_billing = ? WHERE id = ?",
                    (name, contact, note, margin_pct, nb, customer_id),
                )
                return customer_id
            cur = self._conn.execute(
                "INSERT INTO customers (name, contact, note, margin_pct, no_billing, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, contact, note, margin_pct, nb, time.time()),
            )
            return int(cur.lastrowid)

    def customer_by_name(self, name: str) -> int:
        """Kunde mit diesem Namen (Groß-/Kleinschreibung egal) oder neu angelegt, nur mit Namen."""
        name = name.strip()[:120]
        row = self._conn.execute("SELECT id FROM customers WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        return int(row["id"]) if row else self.save_customer(None, name, None, None)

    def assign_customer(self, ids: list[int], customer_id: int | None) -> dict[int, int | None]:
        """Kunde für mehrere Drucke/Projekte setzen. Ein Druck in einem Projekt ändert den Kunden des Projekts.

        Gibt {geänderte ID: bisheriger Kunde} zurück (für „Rückgängig“).
        """
        if customer_id is not None and not self._conn.execute("SELECT 1 FROM customers WHERE id = ?", (customer_id,)).fetchone():
            return {}
        prev: dict[int, int | None] = {}
        for i in ids:
            row = self._conn.execute("SELECT id, project_id, customer_id, split, kind FROM jobs WHERE id = ?", (i,)).fetchone()
            if not row:
                continue
            if row["project_id"]:
                row = self._conn.execute("SELECT id, project_id, customer_id, split, kind FROM jobs WHERE id = ?", (row["project_id"],)).fetchone()
                if not row:
                    continue
            if row["split"] or row["kind"] == "share":
                continue  # aufgeteilt: Kunden stehen in den Anteilen
            if row["id"] not in prev:
                prev[row["id"]] = row["customer_id"]
        for i in prev:
            self.update(i, customer_id=customer_id)
        return prev

    def customer_suggestions(self, names: list[str]) -> dict[str, tuple[int, str, int]]:
        """Vorschlag je Druckname: Kunde, für den das gleiche Modell am häufigsten gedruckt wurde → (id, Name, Anzahl)."""
        names = sorted({n for n in names if n})
        out: dict[str, tuple[int, str, int] | None] = {}
        for i in range(0, len(names), 400):
            part = names[i:i + 400]
            marks = ", ".join("?" for _ in part)
            rows = self._conn.execute(
                "SELECT jobs.task_name, jobs.customer_id, customers.name, COUNT(*) n, MAX(jobs.start_ts) last FROM jobs "
                "JOIN customers ON customers.id = jobs.customer_id "
                f"WHERE jobs.kind IS NULL AND jobs.task_name IN ({marks}) GROUP BY jobs.task_name, jobs.customer_id "
                "ORDER BY n DESC, last DESC", part).fetchall()
            # Modelle, die meist ohne Kunde gedruckt werden (Eigenbedarf), bekommen keinen Vorschlag
            none = dict(self._conn.execute(
                f"SELECT task_name, COUNT(*) FROM jobs WHERE kind IS NULL AND customer_id IS NULL AND task_name IN ({marks}) "
                "GROUP BY task_name", part).fetchall())
            for r in rows:
                if r["task_name"] not in out and r["n"] >= none.get(r["task_name"], 0):
                    out[r["task_name"]] = (r["customer_id"], r["name"], r["n"])
                out.setdefault(r["task_name"], None)  # nur der häufigste Kunde zählt
        return {k: v for k, v in out.items() if v}

    def delete_customer(self, customer_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET customer_id = NULL WHERE customer_id = ?", (customer_id,))
            self._conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))

    # ---- Eigene Fotos ---------------------------------------------------------

    def photos(self, job_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM photos WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def first_photos(self, job_ids: list[int]) -> dict[int, str]:
        if not job_ids:
            return {}
        marks = ",".join("?" for _ in job_ids)
        rows = self._conn.execute(
            "SELECT job_id, file FROM photos WHERE id IN "
            f"(SELECT MAX(id) FROM photos WHERE job_id IN ({marks}) GROUP BY job_id)", job_ids
        ).fetchall()
        return {r["job_id"]: r["file"] for r in rows}

    def photo_counts(self, job_ids: list[int]) -> dict[int, int]:
        if not job_ids:
            return {}
        marks = ",".join("?" for _ in job_ids)
        rows = self._conn.execute(
            f"SELECT job_id, COUNT(*) n FROM photos WHERE job_id IN ({marks}) GROUP BY job_id", job_ids
        ).fetchall()
        return {r["job_id"]: r["n"] for r in rows}

    def add_photo(self, job_id: int, file: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO photos (job_id, file, created_at) VALUES (?, ?, ?)",
                (job_id, file, time.time()),
            )
            return int(cur.lastrowid)

    def delete_photo(self, photo_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if row:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        return dict(row) if row else None

    def delete_job_photos(self, job_id: int) -> list[str]:
        files = [p["file"] for p in self.photos(job_id)]
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM photos WHERE job_id = ?", (job_id,))
        return files

    # ---- Statistik ----------------------------------------------------------

    def buckets(self, period: str, since: float, until: float) -> list[dict[str, Any]]:
        """Summen pro Zeitraum (day/week/month) und Drucker."""
        fmt = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m"}[period]
        rows = self._conn.execute(
            f"SELECT strftime('{fmt}', start_ts, 'unixepoch', 'localtime') bucket, printer, "
            "COUNT(*) n, SUM(CASE WHEN end_ts > start_ts THEN end_ts - start_ts ELSE 0 END) / 3600.0 hours, "
            "COALESCE(SUM(COALESCE(filament_g, weight_g)),0) grams, COALESCE(SUM(kwh),0) kwh, "
            "COALESCE(SUM(cost),0) cost_energy, COALESCE(SUM(cost_filament),0) cost_filament, "
            "COALESCE(SUM(cost_printer),0) cost_printer, COALESCE(SUM(cost_total),0) cost_total, "
            # „Kundenwunsch geändert“ liegt nicht am Druck: zählt nicht als Fehlschlag
            f"SUM(NOT {Store.SCRAP_SQL} OR COALESCE(jobs.defect, '') = 'customer') ok, "
            f"SUM({Store.SCRAP_SQL} AND COALESCE(jobs.defect, '') <> 'customer') failed, "
            f"COALESCE(SUM(CASE WHEN {Store.SCRAP_SQL} THEN cost_total ELSE 0 END),0) scrap_cost, "
            f"COALESCE(SUM(CASE WHEN {Store.SCRAP_SQL} THEN 0 ELSE cost END),0) good_energy, "
            f"COALESCE(SUM(CASE WHEN {Store.SCRAP_SQL} THEN 0 ELSE cost_filament END),0) good_filament, "
            f"COALESCE(SUM(CASE WHEN {Store.SCRAP_SQL} THEN 0 ELSE cost_printer END),0) good_printer "
            "FROM jobs WHERE kind IS NULL AND start_ts >= ? AND start_ts < ? GROUP BY bucket, printer ORDER BY bucket",
            (since, until),
        ).fetchall()
        return [dict(r) for r in rows]

    def defect_stats(self, since: float, until: float) -> dict[str, Any]:
        """Fehldrucke im Zeitraum: pro Ursache (None = nicht angegeben) Anzahl und Kosten, dazu berechnet und Nachdrucke."""
        charged = (f"CASE WHEN NOT {Store.NOCHARGE_SQL} AND jobs.customer_id IS NOT NULL AND NOT {Store.NOBILL_SQL} "
                   f"AND COALESCE(jobs.gift, 0) = 0 THEN {Store.DUE_SQL} ELSE 0 END")
        rows = self._conn.execute(
            f"SELECT jobs.defect, COUNT(*) n, COALESCE(SUM(cost_total),0) cost, COALESCE(SUM({charged}),0) charged, "
            f"SUM(CASE WHEN {charged} > 0 THEN 1 ELSE 0 END) n_charged, "
            "SUM(CASE WHEN jobs.defect_charge = 'full' THEN 1 ELSE 0 END) n_full "
            f"FROM jobs WHERE {Store.SCRAP_SQL} = 1 AND start_ts >= ? AND start_ts < ? GROUP BY jobs.defect",
            (since, until)).fetchall()
        rp = self._conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(cost_total),0) cost FROM jobs WHERE reprint_of IS NOT NULL "
            "AND start_ts >= ? AND start_ts < ?", (since, until)).fetchone()
        return {"rows": [dict(r) for r in rows], "reprints": dict(rp)}

    def reprints(self, job_id: int) -> list[dict[str, Any]]:
        """Nachdrucke eines Fehldrucks."""
        rows = self._conn.execute(
            f"SELECT jobs.*, {Store.SCRAP_SQL} AS is_scrap FROM jobs WHERE reprint_of = ? ORDER BY start_ts", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def reprint_candidates(self, job: dict[str, Any], limit: int = 15) -> list[dict[str, Any]]:
        """Drucke, die als Nachdruck in Frage kommen: später gestartet, gleicher Kunde oder gleicher Name, passende zuerst."""
        rows = self._conn.execute(
            "SELECT id, code, task_name, start_ts, status, customer_id, reprint_of FROM jobs "
            "WHERE id <> ? AND kind IS NULL AND start_ts >= ? AND (reprint_of IS NULL OR reprint_of = ?) "
            "ORDER BY (task_name = ?) + (customer_id IS NOT NULL AND customer_id = ?) DESC, start_ts LIMIT ?",
            (job["id"], job.get("start_ts") or 0, job["id"], job.get("task_name"), job.get("customer_id"), limit)).fetchall()
        return [dict(r) for r in rows]

    def set_reprint(self, job_id: int, reprint_id: int | None) -> None:
        """Nachdruck zuordnen (``None`` löst die Zuordnung)."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET reprint_of = NULL WHERE reprint_of = ?", (job_id,))
            if reprint_id and reprint_id != job_id:
                self._conn.execute("UPDATE jobs SET reprint_of = ? WHERE id = ?", (job_id, reprint_id))
        # Nachdruck eines Drucks aus einem Projekt gehört ins selbe Projekt (wenn er noch in keinem ist).
        if reprint_id and reprint_id != job_id:
            pid = self._conn.execute("SELECT project_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
            other = self._conn.execute("SELECT project_id, kind FROM jobs WHERE id = ?", (reprint_id,)).fetchone()
            if pid and pid[0] and other and not other["project_id"] and not other["kind"]:
                self.create_project([reprint_id], into=pid[0])

    def material_buckets(self, period: str, since: float, until: float) -> list[dict[str, Any]]:
        fmt = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m"}[period]
        rows = self._conn.execute(
            f"SELECT strftime('{fmt}', start_ts, 'unixepoch', 'localtime') bucket, "
            "COALESCE(json_extract(m.value, '$.type'), '?') mtype, "
            "SUM(json_extract(m.value, '$.grams') * CASE WHEN jobs.status = 'abgebrochen' "
            "AND jobs.progress IS NOT NULL THEN jobs.progress / 100.0 ELSE 1 END) grams "
            "FROM jobs, json_each(jobs.materials) m WHERE start_ts >= ? AND start_ts < ? "
            "GROUP BY bucket, mtype ORDER BY bucket",
            (since, until),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- Materialauswertung ---------------------------------------------

    def material_summary(self, **filters: Any) -> list[dict[str, Any]]:
        where, args = self._filters(**filters)
        rows = self._conn.execute(
            "SELECT COALESCE(json_extract(m.value, '$.type'), '?') mtype, "
            "SUM(json_extract(m.value, '$.grams') * CASE WHEN jobs.status = 'abgebrochen' "
            "AND jobs.progress IS NOT NULL THEN jobs.progress / 100.0 ELSE 1 END) grams "
            f"FROM jobs, json_each(jobs.materials) m {where} GROUP BY 1 ORDER BY grams DESC",
            args,
        ).fetchall()
        return [dict(r) for r in rows]

    def material_types(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT json_extract(m.value, '$.type') t FROM jobs, json_each(jobs.materials) m "
            "WHERE t IS NOT NULL ORDER BY t"
        ).fetchall()
        return [r["t"] for r in rows]

    def material_names(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT json_extract(m.value, '$.name') name, MAX(json_extract(m.value, '$.type')) type "
            "FROM jobs, json_each(jobs.materials) m WHERE name IS NOT NULL GROUP BY name ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def done_jobs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM jobs WHERE state = 'done' AND kind IS NULL").fetchall()
        return [dict(r) for r in rows]

    # ---- Einstellungen --------------------------------------------------

    def printer_rates(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM printer_rates").fetchall()
        return {r["printer"]: dict(r) for r in rows}

    def printer_rates_set(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM printer_rates WHERE (purchase_eur IS NOT NULL AND life_hours IS NOT NULL) "
            "OR maint_per_1000h IS NOT NULL LIMIT 1"
        ).fetchone()
        return row is not None

    RATE_FIELDS = ("purchase_eur", "life_hours", "maint_per_1000h")

    def set_printer_rate(self, printer: str, **values: float | None) -> None:
        fields = {k: values.get(k) for k in self.RATE_FIELDS}
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{k} = excluded.{k}" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO printer_rates (printer, {cols}) VALUES (?, {marks}) "
                f"ON CONFLICT(printer) DO UPDATE SET {updates}",
                (printer, *fields.values()),
            )

    def settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self._conn.execute("SELECT * FROM settings")}

    def set_setting(self, key: str, value: str | None) -> None:
        with self._lock, self._conn:
            if value is None or value == "":
                self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def backup_to(self, target: Path) -> None:
        """Konsistente Kopie der Datenbank (auch während Schreibzugriffen)."""
        dst = sqlite3.connect(target)
        try:
            with self._lock:
                self._conn.backup(dst)
        finally:
            dst.close()

    # ---- Datensicherung: Einspielen und Zurücksetzen ------------------------

    HISTORY_TABLES = ("jobs", "photos", "handovers", "payments", "trash")
    SETTINGS_TABLES = ("customers", "printer_rates", "material_prices", "settings")
    # Bleiben beim Zurücksetzen/Einspielen erhalten: Sprache der Oberfläche und der
    # Zeitpunkt der letzten HA-Verbindung (sonst holt printlog alte Drucke aus dem Verlauf nach).
    KEEP_SETTINGS = ("language", "ha_seen_ts")

    def counts(self) -> dict[str, Any]:
        """Kennzahlen für Vorschau und Zurücksetzen."""
        c = self._conn
        one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "jobs": one("SELECT COUNT(*) FROM jobs"),
            "customers": one("SELECT COUNT(*) FROM customers"),
            "photos": one("SELECT COUNT(*) FROM photos"),
            "trash": one("SELECT COUNT(*) FROM trash"),
            "payments": one("SELECT COUNT(*) FROM payments"),
            "first_ts": one("SELECT MIN(start_ts) FROM jobs"),
            "last_ts": one("SELECT MAX(start_ts) FROM jobs"),
            "running": one("SELECT COUNT(*) FROM jobs WHERE state IN ('running', 'closing')"),
        }

    def image_files(self) -> set[str]:
        """Alle Bilddateien, auf die die Datenbank verweist (Cover, Fotos, Papierkorb, Bon-Logo)."""
        c = self._conn
        names = {r[0] for r in c.execute("SELECT image FROM jobs WHERE image IS NOT NULL")}
        names |= {r[0] for r in c.execute("SELECT file FROM photos")}
        names |= {r[0] for r in c.execute("SELECT json_extract(data, '$.image') FROM trash") if r[0]}
        logo = self.settings().get("bon_logo")
        return names | ({logo} if logo else set())

    def restore_from(self, source: Path) -> None:
        """Ersetzt den kompletten Inhalt durch die Datenbank ``source`` (SQLite-Backup-API).

        Die Verbindung bleibt dieselbe, die App muss also nicht neu starten.
        """
        keep = {k: v for k, v in self.settings().items() if k in self.KEEP_SETTINGS}
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            with self._lock:
                src.backup(self._conn)
        finally:
            src.close()
        self._conn.executescript(SCHEMA)  # ältere Sicherungen: fehlende Tabellen anlegen
        self._migrate()
        for k, v in keep.items():
            self.set_setting(k, v)

    def reset(self, everything: bool) -> None:
        """Löscht alle Drucke samt Historie; mit ``everything`` auch Kunden, Preise und Einstellungen."""
        tables = self.HISTORY_TABLES + (self.SETTINGS_TABLES if everything else ())
        with self._lock, self._conn:
            for table in tables:
                if table == "settings":
                    marks = ", ".join("?" for _ in self.KEEP_SETTINGS)
                    self._conn.execute(f"DELETE FROM settings WHERE key NOT IN ({marks})", self.KEEP_SETTINGS)
                else:
                    self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute(
                f"DELETE FROM sqlite_sequence WHERE name IN ({', '.join('?' for _ in tables)})", tables)
        self._conn.execute("VACUUM")

    def material_prices(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM material_prices").fetchall()
        return {r["key"]: dict(r) for r in rows}

    def set_material_price(self, key: str, kind: str, eur_per_kg: float | None) -> None:
        with self._lock, self._conn:
            if eur_per_kg is None:
                self._conn.execute("DELETE FROM material_prices WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO material_prices (key, kind, eur_per_kg) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET kind = excluded.kind, eur_per_kg = excluded.eur_per_kg",
                    (key, kind, eur_per_kg),
                )
