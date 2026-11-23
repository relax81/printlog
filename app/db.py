"""SQLite-Speicher für Druckaufträge."""

from __future__ import annotations

import json
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
    extra_per_hour REAL                   -- Wartung/Verschleiß pro Stunde
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
        "cost_printer": "REAL",       # Abschreibung + Verschleiß
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
    }

    # Übergebene Stück (Summe der Übergaben, auch Teilabholungen) und Soll-Stückzahl
    DELIVERED_SQL = "(SELECT COALESCE(SUM(h.qty),0) FROM handovers h WHERE h.job_id = jobs.id)"
    QTY_SQL = "COALESCE(jobs.quantity, 1)"
    # Kunde mit Häkchen „Keine Abrechnung“ (z. B. Eigenbedarf): Kosten zählen, aber nie offen/unbezahlt/nicht übergeben.
    NOBILL_SQL = "COALESCE((SELECT cb.no_billing FROM customers cb WHERE cb.id = jobs.customer_id), 0) = 1"
    DELIVERABLE_SQL = ("(jobs.customer_id IS NOT NULL AND COALESCE(jobs.scrap, jobs.status = 'abgebrochen') = 0 AND jobs.state = 'done' "
                       f"AND NOT {NOBILL_SQL})")

    # Zu zahlen: Endpreis, sonst Kosten + Kundenmarge. Gezahlt: Summe der Zahlungen.
    DUE_SQL = ("COALESCE(jobs.final_price, jobs.cost_total * (1 + COALESCE("
               "(SELECT c.margin_pct FROM customers c WHERE c.id = jobs.customer_id), 0) / 100.0))")
    PAID_SQL = "(SELECT COALESCE(SUM(p.amount),0) FROM payments p WHERE p.job_id = jobs.id)"
    GIFT_SQL = "COALESCE(jobs.gift, 0) = 1"
    # Druck mit Kunde (mit Abrechnung), der kein Ausschuss, nicht verschenkt ist und einen Preis hat: dafür wird Geld erwartet.
    PAYABLE_SQL = (f"(jobs.customer_id IS NOT NULL AND {DUE_SQL} IS NOT NULL "
                   f"AND COALESCE(jobs.scrap, jobs.status = 'abgebrochen') = 0 AND COALESCE(jobs.gift, 0) = 0 AND NOT {NOBILL_SQL})")

    # Ausschuss, wenn so markiert oder (ohne Markierung) abgebrochen.
    SCRAP_SQL = "COALESCE(jobs.scrap, jobs.status = 'abgebrochen')"

    def _migrate(self) -> None:
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        for col, typ in self.NEW_COLUMNS.items():
            if col not in have:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(printer_rates)")}
        for col in ("maint_per_year", "hours_per_year"):
            if col not in have:
                self._conn.execute(f"ALTER TABLE printer_rates ADD COLUMN {col} REAL")
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(customers)")}
        if "margin_pct" not in have:
            self._conn.execute("ALTER TABLE customers ADD COLUMN margin_pct REAL")
        if "no_billing" not in have:
            # 1 = keine Abrechnung (z. B. Eigenbedarf): keine Zahlungen/Übergaben, Kosten zählen weiter
            self._conn.execute("ALTER TABLE customers ADD COLUMN no_billing INTEGER")
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_code ON jobs(code)")
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

    # ---- Papierkorb -------------------------------------------------------

    def trash_job(self, job_id: int) -> dict[str, Any] | None:
        """Druck in den Papierkorb legen (aus jobs raus, wiederherstellbar)."""
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        job = dict(row)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO trash (id, printer, start_ts, end_ts, deleted_ts, data) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, job["printer"], job["start_ts"], job["end_ts"], time.time(), json.dumps(job)))
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
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
        job = {k: v for k, v in job.items() if k in have}
        if job.get("code") and self._conn.execute(
                "SELECT 1 FROM jobs WHERE code = ? AND id != ?", (job["code"], job_id)).fetchone():
            job["code"] = None  # sehr unwahrscheinlich, aber dann neu vergeben
        if job.get("customer_id") and not self._conn.execute(
                "SELECT 1 FROM customers WHERE id = ?", (job["customer_id"],)).fetchone():
            job["customer_id"] = None  # Kunde inzwischen gelöscht
        cols = ", ".join(job)
        with self._lock, self._conn:
            self._conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({', '.join('?' for _ in job)})", list(job.values()))
            self._conn.execute("DELETE FROM trash WHERE id = ?", (job_id,))
        self.ensure_codes()
        return True

    def purge_job(self, job_id: int) -> list[str]:
        """Endgültig löschen; gibt die Bilddateien zurück, die weg können."""
        row = self._conn.execute("SELECT data FROM trash WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return []
        image = json.loads(row["data"]).get("image")
        files = [p["file"] for p in self.photos(job_id)] + ([image] if image else [])
        with self._lock, self._conn:
            for table in ("handovers", "payments", "photos"):
                self._conn.execute(f"DELETE FROM {table} WHERE job_id = ?", (job_id,))
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
        where, args = self._filters(**filters)
        total = self._conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(kwh),0) kwh, COALESCE(SUM(cost),0) cost, "
            f"COALESCE(SUM(COALESCE(filament_g, weight_g)),0) weight, "
            f"COALESCE(SUM(cost_printer),0) cost_printer, COALESCE(SUM(cost_filament),0) cost_filament, "
            f"COALESCE(SUM(cost_total),0) cost_total, "
            f"COALESCE(SUM(CASE WHEN {Store.SCRAP_SQL} THEN 1 ELSE 0 END),0) scrap_n, "
            f"COALESCE(SUM(CASE WHEN {Store.SCRAP_SQL} THEN cost_total ELSE 0 END),0) scrap_cost FROM jobs {where}",
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
            "SELECT printer, MAX(printer_name) printer_name FROM jobs GROUP BY printer ORDER BY printer"
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
        since: float | None = None,
        until: float | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
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
            clauses.append("jobs.customer_id IS NULL")
        elif customer:
            clauses.append("jobs.customer_id = ?")
            args.append(int(customer))
        if paid == "offen":
            clauses.append(f"{Store.PAYABLE_SQL} AND {Store.PAID_SQL} < {Store.DUE_SQL} - 0.005")
        elif paid == "teilweise":
            clauses.append(f"{Store.PAYABLE_SQL} AND {Store.PAID_SQL} > 0 AND {Store.PAID_SQL} < {Store.DUE_SQL} - 0.005")
        elif paid == "bezahlt":
            clauses.append(f"{Store.PAYABLE_SQL} AND {Store.PAID_SQL} >= {Store.DUE_SQL} - 0.005")
        elif paid == "verschenkt":
            clauses.append(Store.GIFT_SQL)
        if handover == "offen":
            clauses.append(f"{Store.DELIVERABLE_SQL} AND {Store.DELIVERED_SQL} < {Store.QTY_SQL}")
        elif handover == "teilweise":
            clauses.append(f"{Store.DELIVERABLE_SQL} AND {Store.DELIVERED_SQL} BETWEEN 1 AND {Store.QTY_SQL} - 1")
        elif handover == "übergeben":
            clauses.append(f"{Store.DELIVERED_SQL} >= {Store.QTY_SQL}")
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
                "COALESCE((SELECT name FROM customers c WHERE c.id = jobs.customer_id),'')) LIKE ?"
            )
            args.append("%" + norm(token) + "%")
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", args

    # ---- Kunden -----------------------------------------------------------

    def customers(self) -> list[dict[str, Any]]:
        P, DUE, PAID = (x.replace("jobs.", "j.") for x in (self.PAYABLE_SQL, self.DUE_SQL, self.PAID_SQL))
        rows = self._conn.execute(
            "SELECT c.*, COUNT(j.id) n, COALESCE(SUM(j.cost_total),0) cost_total, MAX(j.start_ts) last_ts, "
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
        DUE = self.DUE_SQL
        q = (f"SELECT customer_id, COUNT(*) n, COALESCE(SUM({self.QTY_SQL}),0) pieces, COALESCE(SUM(cost_total),0) cost, "
             f"COALESCE(SUM({DUE}),0) value FROM jobs WHERE {self.GIFT_SQL}")
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
            f"SUM(NOT {Store.SCRAP_SQL}) ok, SUM({Store.SCRAP_SQL}) failed, "
            f"COALESCE(SUM(CASE WHEN {Store.SCRAP_SQL} THEN cost_total ELSE 0 END),0) scrap_cost "
            "FROM jobs WHERE start_ts >= ? AND start_ts < ? GROUP BY bucket, printer ORDER BY bucket",
            (since, until),
        ).fetchall()
        return [dict(r) for r in rows]

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
        rows = self._conn.execute("SELECT * FROM jobs WHERE state = 'done'").fetchall()
        return [dict(r) for r in rows]

    # ---- Einstellungen --------------------------------------------------

    def printer_rates(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM printer_rates").fetchall()
        return {r["printer"]: dict(r) for r in rows}

    def printer_rates_set(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM printer_rates WHERE (purchase_eur IS NOT NULL AND life_hours IS NOT NULL) "
            "OR extra_per_hour IS NOT NULL OR (maint_per_year IS NOT NULL AND hours_per_year IS NOT NULL) LIMIT 1"
        ).fetchone()
        return row is not None

    RATE_FIELDS = ("purchase_eur", "life_hours", "extra_per_hour", "maint_per_year", "hours_per_year")

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

    def printed_hours_last_year(self) -> dict[str, float]:
        rows = self._conn.execute(
            "SELECT printer, SUM(end_ts - start_ts) / 3600.0 h FROM jobs "
            "WHERE end_ts > start_ts AND start_ts > strftime('%s','now','-365 days') GROUP BY printer"
        ).fetchall()
        return {r["printer"]: r["h"] for r in rows}

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
