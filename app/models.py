"""Modelle: ein Design, das wiederholt gedruckt wird – mit Erkennung, Designkosten und Hinweisen.

* **Modell** = das Design (Name + gelernte Aliase), nicht die Platte. Platten werden zwischen Druckern
  neu zusammengestellt, deshalb zählt die Plattennummer nur wenig.
* **Erkennung** nach Druckende (``recognize_pending``, im Sweeper) mit Punkten:
  Name gleich (auch Alias) +60, ähnlich +35, Cover-Bild sehr ähnlich +25, gleiche Druckdatei +10,
  gleiche Platte +5, Gramm pro Stück ±15 % +5. Stufen: ≥85 automatisch, 60–84 vorläufig mit Hinweis,
  35–59 nur Vorschlag, <35 neues Modell. Kritisch (zwei Modelle fast gleich gut) fragt immer.
  Abgelehnte Paare (Name ↔ Modell) werden nie wieder vorgeschlagen.
* **Design** (Minuten am Modell) wird einmal beim ersten berechneten Druck berechnet oder auf die
  ersten N berechneten Drucke verteilt (``apply_design`` schreibt dafür ``jobs.design_min``).
* **Hinweise**: offene Erkennungsfragen (``jobs.model_hint``) und Zubehör, das bald nachgekauft werden
  muss. Glocke im Kopf, Seite „Hinweise“, Punkte in der Liste, Banner auf der Druckseite.
"""

from __future__ import annotations

import json
import logging
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .db import Store, code_label
from .i18n import t
from .ui import e, fmt_date, fmt_input, fmt_num

log = logging.getLogger("printlog.models")

AUTO, TENTATIVE, SUGGEST = 85, 60, 35
STAGES = {"auto": "g", "confirmed": "g", "new": "w", "tentative": "y", "suggest": "o", "critical": "r"}
REASONS = {
    "name": ("Name gleich", 60), "alias": ("Name bekannt", 60), "similar": ("Name ähnlich", 35),
    "img": ("Bild sehr ähnlich", 25), "file": ("gleiche Druckdatei", 10), "plate": ("gleiche Platte", 5),
    "gpp": ("Gewicht pro Stück passt", 5),
}


# ---- Erkennen -------------------------------------------------------------------

def clean_name(name: str | None) -> str:
    """Anzeigename aus dem Aufgabennamen: ohne Upload-Nummer, Endung und Platte."""
    s = (name or "").strip()
    s = re.sub(r"^\d{4,}-", "", s)
    s = re.sub(r"(\.gcode|\.3mf)+$", "", s, flags=re.I)
    s = re.sub(r"[\s_-]*plate[\s_-]*\d+$", "", s, flags=re.I)
    return s.replace("_", " ").strip() or (name or "").strip()


def normalize(name: str | None) -> str:
    """Vergleichsform: klein, ohne Upload-Nummer, Endung, Platte, Kopie-Zusätze und Satzzeichen."""
    s = clean_name(name).lower()
    s = re.sub(r"\((\d+|copy|kopie)\)$", "", s)
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())


def similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if SequenceMatcher(None, a, b).ratio() >= 0.8:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 6 and short in long_:
        return True
    ta, tb = set(a.split()), set(b.split())
    return len(ta | tb) >= 3 and len(ta & tb) / len(ta | tb) >= 0.6


def dhash(path: Path) -> str | None:
    """64-Bit-Differenz-Hash eines Bilds als Hex (gleiche Cover → gleiche oder sehr ähnliche Hashes)."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            g = im.convert("L").resize((9, 8), Image.LANCZOS)
            px = list(g.getdata())
    except Exception:  # noqa: BLE001
        return None
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (px[y * 9 + x] > px[y * 9 + x + 1])
    return f"{bits:016x}"


def hamming(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _gpp(job: dict[str, Any]) -> float | None:
    g = job.get("weight_g") or job.get("filament_g")
    return g / (job.get("quantity") or 1) if g else None


def _index(store: Store, skip_job: int | None = None) -> list[dict[str, Any]]:
    """Alle Modelle mit Namen, Aliasen, Ablehnungen und den Merkmalen ihrer Drucke."""
    models = {r["id"]: {**dict(r), "aliases": set(), "rejects": set(), "hashes": [], "files": set(),
                        "plates": set(), "gpp": []}
              for r in store._conn.execute("SELECT * FROM models")}
    for r in store._conn.execute("SELECT * FROM model_aliases"):
        if r["model_id"] in models:
            models[r["model_id"]]["aliases"].add(r["alias"])
    for r in store._conn.execute("SELECT * FROM model_rejects"):
        if r["model_id"] in models:
            models[r["model_id"]]["rejects"].add(r["alias"])
    for r in store._conn.execute(
            "SELECT id, model_id, img_hash, gcode_file, gcode_plate, weight_g, filament_g, quantity FROM jobs "
            "WHERE kind IS NULL AND model_id IS NOT NULL AND model_state IN ('auto', 'confirmed', 'new', 'tentative')"):
        m = models.get(r["model_id"])
        if not m or r["id"] == skip_job:
            continue
        if r["img_hash"]:
            m["hashes"].append(r["img_hash"])
        if r["gcode_file"]:
            m["files"].add(r["gcode_file"])
        if r["gcode_plate"]:
            m["plates"].add(r["gcode_plate"])
        g = _gpp(dict(r))
        if g:
            m["gpp"].append(g)
    for m in models.values():
        m["norm"] = normalize(m["name"])
    return list(models.values())


def score(job: dict[str, Any], m: dict[str, Any]) -> tuple[int, list[str]]:
    """Punkte (0–100) und Gründe, warum der Druck zu Modell ``m`` gehört."""
    n = normalize(job.get("task_name"))
    if not n or n in m["rejects"]:
        return 0, []
    pts, why = 0, []
    if n == m["norm"]:
        pts, why = 60, ["name"]
    elif n in m["aliases"]:
        pts, why = 60, ["alias"]
    elif similar(n, m["norm"]) or any(similar(n, a) for a in m["aliases"]):
        pts, why = 35, ["similar"]
    h = job.get("img_hash")
    if h and any(hamming(h, x) <= 6 for x in m["hashes"]):
        pts += 25
        why.append("img")
    if job.get("gcode_file") and job["gcode_file"] in m["files"]:
        pts += 10
        why.append("file")
    if job.get("gcode_plate") and job["gcode_plate"] in m["plates"]:
        pts += 5
        why.append("plate")
    g = _gpp(job)
    if g and m["gpp"]:
        avg = sum(m["gpp"]) / len(m["gpp"])
        if avg and abs(g - avg) / avg <= 0.15:
            pts += 5
            why.append("gpp")
    if not pts or (why and why[0] not in ("name", "alias", "similar") and pts < 35):
        return 0, []
    return min(pts, 100), why


def candidates(store: Store, job: dict[str, Any]) -> list[tuple[int, int, list[str]]]:
    """[(Modell-ID, Punkte, Gründe), …] absteigend, nur mit Punkten."""
    out = []
    for m in _index(store, skip_job=job["id"]):
        s, why = score(job, m)
        if s:
            out.append((m["id"], s, why))
    out.sort(key=lambda c: -c[1])
    return out[:4]


def recognize(store: Store, job_id: int, image_dir: Path, silent: bool = False) -> str | None:
    """Modell für einen fertigen Druck bestimmen. Gibt einen Hinweistext zurück, wenn nachgefragt wird."""
    job = _job(store, job_id)
    if not job or job.get("kind") is not None:
        return None
    fields: dict[str, Any] = {}
    if not job.get("img_hash") and job.get("image"):
        h = dhash(image_dir / job["image"])
        if h:
            fields["img_hash"] = job["img_hash"] = h
    if not normalize(job.get("task_name")):
        store.update(job_id, model_state="none", **fields)
        return None
    cands = candidates(store, job)
    best = cands[0] if cands else None
    second = cands[1] if len(cands) > 1 else None
    state, model_id, hint = "new", None, None
    if best and best[1] >= TENTATIVE and second and second[1] >= TENTATIVE and best[1] - second[1] < 10:
        state = "critical"
    elif best and best[1] >= AUTO:
        state, model_id = "auto", best[0]
    elif best and best[1] >= TENTATIVE:
        state, model_id = "tentative", best[0]
    elif best and best[1] >= SUGGEST:
        state = "suggest"
    if silent and state in ("tentative", "critical"):
        state, model_id = "auto", best[0]
    elif silent and state == "suggest":
        state = "new"
    if state == "new":
        model_id = create_model(store, clean_name(job["task_name"]))
    fields.update(model_id=model_id, model_state=state, model_score=best[1] if best and state != "new" else None,
                  model_cands=json.dumps([[c[0], c[1], c[2]] for c in cands]) if cands else None,
                  model_hint=1 if state in ("tentative", "suggest", "critical") else None)
    store.update(job_id, **fields)
    if model_id:
        _adopt_design(store, model_id, job)
        apply_design(store, model_id)
    if fields["model_hint"]:
        m = model(store, best[0]) if best else None
        hint = t("„{job}“ – Modell „{m}“? ({p} %)", job=job.get("task_name") or "?", m=(m or {}).get("name") or "?",
                 p=best[1] if best else 0)
    return hint


def recognize_pending(store: Store, image_dir: Path) -> list[str]:
    """Alle fertigen Drucke ohne Modell. Beim ersten Lauf (vorhandene Historie) still, ohne Hinweise."""
    silent = store.settings().get("models_ready") != "1"
    rows = store._conn.execute(
        "SELECT id FROM jobs WHERE kind IS NULL AND state = 'done' AND model_state IS NULL "
        "ORDER BY start_ts, id").fetchall()
    hints = []
    for r in rows:
        try:
            h = recognize(store, r["id"], image_dir, silent=silent)
        except Exception:  # noqa: BLE001
            log.exception("Modell-Erkennung für Job %s fehlgeschlagen", r["id"])
            store.update(r["id"], model_state="none")
            continue
        if h:
            hints.append(h)
    if silent:
        store.set_setting("models_ready", "1")
        if rows:
            log.info("Modelle für %s vorhandene Drucke angelegt", len(rows))
    return hints


# ---- Ändern ---------------------------------------------------------------------

def _job(store: Store, job_id: int) -> dict[str, Any] | None:
    r = store._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(r) if r else None


def model(store: Store, model_id: int | None) -> dict[str, Any] | None:
    if not model_id:
        return None
    r = store._conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
    return dict(r) if r else None


def create_model(store: Store, name: str, design_min: float | None = None) -> int:
    with store._lock, store._conn:
        cur = store._conn.execute("INSERT INTO models (name, design_min, created_at) VALUES (?, ?, ?)",
                                  ((name or "?")[:120], design_min, time.time()))
        return int(cur.lastrowid)


def _learn(store: Store, model_id: int, job: dict[str, Any]) -> None:
    n = normalize(job.get("task_name"))
    m = model(store, model_id)
    if n and m and n != normalize(m["name"]):
        with store._lock, store._conn:
            store._conn.execute("INSERT OR IGNORE INTO model_aliases (model_id, alias) VALUES (?, ?)", (model_id, n))
            store._conn.execute("DELETE FROM model_rejects WHERE model_id = ? AND alias = ?", (model_id, n))


def _reject(store: Store, model_id: int, job: dict[str, Any]) -> None:
    n = normalize(job.get("task_name"))
    if n:
        with store._lock, store._conn:
            store._conn.execute("INSERT OR IGNORE INTO model_rejects (model_id, alias) VALUES (?, ?)", (model_id, n))
            store._conn.execute("DELETE FROM model_aliases WHERE model_id = ? AND alias = ?", (model_id, n))


def _adopt_design(store: Store, model_id: int, job: dict[str, Any]) -> None:
    """Designzeit, die direkt am Druck stand (vor den Modellen), wandert ans Modell."""
    m = model(store, model_id)
    if m and not m.get("design_min") and job.get("design_min"):
        with store._lock, store._conn:
            store._conn.execute("UPDATE models SET design_min = ? WHERE id = ?", (job["design_min"], model_id))


def assign(store: Store, job_id: int, model_id: int) -> None:
    """Bestätigen bzw. von Hand zuordnen: Name wird als Alias gelernt."""
    job = _job(store, job_id)
    if not job or not model(store, model_id):
        return
    old = job.get("model_id")
    store.update(job_id, model_id=model_id, model_state="confirmed", model_hint=None)
    _learn(store, model_id, job)
    _adopt_design(store, model_id, job)
    _drop_if_empty(store, old, keep=model_id)
    for mid in {old, model_id} - {None}:
        apply_design(store, mid)


def make_new(store: Store, job_id: int) -> int | None:
    """„Neues Modell“: vorgeschlagene/vorläufige Modelle für diesen Namen ablehnen, eigenes Modell anlegen."""
    job = _job(store, job_id)
    if not job:
        return None
    old = job.get("model_id")
    for c in json.loads(job.get("model_cands") or "[]"):
        _reject(store, c[0], job)
    if old and old_only(store, old, job_id):
        store.update(job_id, model_state="confirmed", model_hint=None)
        return old
    mid = create_model(store, clean_name(job.get("task_name")))
    store.update(job_id, model_id=mid, model_state="confirmed", model_hint=None)
    _drop_if_empty(store, old, keep=mid)
    for m in {old, mid} - {None}:
        apply_design(store, m)
    return mid


def old_only(store: Store, model_id: int, job_id: int) -> bool:
    """Ist der Druck der einzige seines Modells (dann reicht Bestätigen)?"""
    r = store._conn.execute("SELECT COUNT(*) FROM jobs WHERE model_id = ? AND id <> ?", (model_id, job_id)).fetchone()
    return not r[0]


def dismiss(store: Store, job_id: int) -> None:
    """Vorläufige Zuordnung so lassen (bzw. beim Vorschlag: eigenes Modell behalten)."""
    job = _job(store, job_id)
    if not job:
        return
    if job.get("model_id"):
        store.update(job_id, model_state="confirmed", model_hint=None)
        _learn(store, job["model_id"], job)
    else:
        make_new(store, job_id)


def _drop_if_empty(store: Store, model_id: int | None, keep: int | None = None) -> None:
    """Modell ohne Drucke und ohne Designzeit entfernen."""
    if not model_id or model_id == keep:
        return
    m = model(store, model_id)
    used = store._conn.execute("SELECT 1 FROM jobs WHERE model_id = ? LIMIT 1", (model_id,)).fetchone()
    if m and not used and not m.get("design_min"):
        delete_model(store, model_id)


def delete_model(store: Store, model_id: int) -> None:
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM models WHERE id = ?", (model_id,))
        store._conn.execute("DELETE FROM model_aliases WHERE model_id = ?", (model_id,))
        store._conn.execute("DELETE FROM model_rejects WHERE model_id = ?", (model_id,))
        store._conn.execute("UPDATE jobs SET model_id = NULL, model_state = NULL WHERE model_id = ?", (model_id,))


def merge(store: Store, src: int, dst: int) -> None:
    """Modell ``src`` in ``dst`` aufgehen lassen (Drucke, Aliase; Designzeit, falls dst keine hat)."""
    a, b = model(store, src), model(store, dst)
    if not a or not b or src == dst:
        return
    with store._lock, store._conn:
        store._conn.execute("UPDATE jobs SET model_id = ?, model_hint = NULL, "
                            "model_state = CASE WHEN model_state IN ('tentative', 'suggest', 'critical') THEN 'confirmed' ELSE model_state END "
                            "WHERE model_id = ?", (dst, src))
        store._conn.execute("INSERT OR IGNORE INTO model_aliases (model_id, alias) SELECT ?, alias FROM model_aliases WHERE model_id = ?",
                            (dst, src))
        store._conn.execute("INSERT OR IGNORE INTO model_aliases (model_id, alias) VALUES (?, ?)", (dst, normalize(a["name"])))
        if not b.get("design_min") and a.get("design_min"):
            store._conn.execute("UPDATE models SET design_min = ?, design_mode = ?, spread_n = ? WHERE id = ?",
                                (a["design_min"], a.get("design_mode"), a.get("spread_n"), dst))
    delete_model(store, src)
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM model_aliases WHERE model_id = ? AND alias = ?", (dst, normalize(b["name"])))
    apply_design(store, dst)


def save_model(store: Store, model_id: int, **f: Any) -> None:
    allowed = {k: v for k, v in f.items() if k in ("name", "design_min", "design_mode", "spread_n", "note")}
    if not allowed:
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with store._lock, store._conn:
        store._conn.execute(f"UPDATE models SET {sets} WHERE id = ?", [*allowed.values(), model_id])
    apply_design(store, model_id)


def remove_alias(store: Store, model_id: int, alias: str) -> None:
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM model_aliases WHERE model_id = ? AND alias = ?", (model_id, alias))


# ---- Designkosten ---------------------------------------------------------------

def _billed(store: Store, job: dict[str, Any]) -> bool:
    """Wird dieser Druck einem Kunden berechnet (direkt, über ein Projekt oder über Anteile)?"""
    if job.get("is_scrap") and job.get("defect_charge") not in ("pct", "amount", "full"):
        return False
    q = ("SELECT 1 FROM customers WHERE id = ? AND COALESCE(no_billing, 0) = 0")
    if job.get("project_id"):
        p = store._conn.execute("SELECT customer_id, gift FROM jobs WHERE id = ?", (job["project_id"],)).fetchone()
        return bool(p and p["customer_id"] and not p["gift"] and store._conn.execute(q, (p["customer_id"],)).fetchone())
    if job.get("split"):
        for sh in store._conn.execute("SELECT customer_id, gift FROM jobs WHERE kind = 'share' AND share_of = ?", (job["id"],)):
            if sh["customer_id"] and not sh["gift"] and store._conn.execute(q, (sh["customer_id"],)).fetchone():
                return True
        return False
    return bool(job.get("customer_id") and not job.get("gift") and store._conn.execute(q, (job["customer_id"],)).fetchone())


def design_plan(store: Store, model_id: int) -> dict[int, float | None]:
    """Designminuten pro Druck des Modells: einmal beim ersten berechneten Druck oder verteilt."""
    m = model(store, model_id)
    jobs = [dict(r) for r in store._conn.execute(
        f"SELECT *, {Store.SCRAP_SQL} AS is_scrap FROM jobs WHERE kind IS NULL AND model_id = ? ORDER BY start_ts, id", (model_id,))]
    plan: dict[int, float | None] = {j["id"]: None for j in jobs}
    dm = (m or {}).get("design_min") or 0
    if not dm:
        return plan
    n = max(1, int((m or {}).get("spread_n") or 3)) if (m or {}).get("design_mode") == "spread" else 1
    billed = [j for j in jobs if _billed(store, j)][:n]
    for j in billed:
        plan[j["id"]] = round(dm / n, 2)
    return plan


def apply_design(store: Store, model_id: int) -> None:
    from . import extras  # Kreisimport vermeiden

    plan = design_plan(store, model_id)
    for jid, dm in plan.items():
        row = store._conn.execute("SELECT design_min, project_id, split FROM jobs WHERE id = ?", (jid,)).fetchone()
        if row and (row["design_min"] or None) != dm:
            store.update(jid, design_min=dm)
            extras.refresh(store, jid)
            if row["project_id"]:
                store.refresh_project(row["project_id"])
            if row["split"]:
                store.refresh_shares(jid)


def apply_all(store: Store) -> None:
    """Nach Änderungen an Kunden, Geschenken, Projekten …: Designkosten aller betroffenen Modelle neu verteilen."""
    ids = [r[0] for r in store._conn.execute(
        "SELECT id FROM models WHERE COALESCE(design_min, 0) > 0 UNION "
        "SELECT DISTINCT model_id FROM jobs WHERE model_id IS NOT NULL AND COALESCE(design_min, 0) > 0")]
    for mid in ids:
        apply_design(store, mid)


def design_note(store: Store, job: dict[str, Any]) -> str:
    """Erklärung im Kasten „Arbeit & Zubehör“: wo die Designzeit des Modells berechnet wird."""
    m = model(store, job.get("model_id"))
    if not m or not m.get("design_min"):
        return t("einmal pro Modell · eigener Stundensatz")
    plan = design_plan(store, m["id"])
    here = plan.get(job["id"])
    others = [j for j, v in plan.items() if v and j != job["id"]]
    spread = m.get("design_mode") == "spread"
    if here:
        return (t("verteilt: {v} min bei diesem Druck", v=fmt_input(here)) if spread
                else t("wird bei diesem Druck berechnet (erster berechneter)"))
    if others:
        r = store._conn.execute("SELECT code FROM jobs WHERE id = ?", (others[0],)).fetchone()
        return t("schon bei {c} berechnet", c=code_label(r["code"]) if r else "?")
    return t("noch bei keinem berechneten Druck")


# ---- Hinweise -------------------------------------------------------------------

def open_hints(store: Store) -> list[dict[str, Any]]:
    return [dict(r) for r in store._conn.execute(
        "SELECT * FROM jobs WHERE kind IS NULL AND model_hint = 1 ORDER BY "
        "CASE model_state WHEN 'critical' THEN 0 WHEN 'suggest' THEN 1 ELSE 2 END, start_ts DESC")]


def hint_count(store: Store) -> int:
    from . import extras

    n = store._conn.execute("SELECT COUNT(*) FROM jobs WHERE kind IS NULL AND model_hint = 1").fetchone()[0]
    return int(n) + len(extras.low_stock(store))


def names(store: Store) -> dict[int, str]:
    return {r["id"]: r["name"] for r in store._conn.execute("SELECT id, name FROM models")}


def dot(job: dict[str, Any]) -> str:
    """Farbiger Punkt in der Liste für offene Erkennungsfragen."""
    if not job.get("model_hint"):
        return ""
    c = STAGES.get(job.get("model_state") or "", "y")
    return f'<a class="mdot {c}" href="job?id={job["id"]}#mh" title="{e(t("Modell prüfen"))}"></a>'


def chip(store: Store, job: dict[str, Any]) -> str:
    """Schild „🧩 Modell“ im Kopf der Druckseite."""
    m = model(store, job.get("model_id"))
    if not m:
        return ""
    c = STAGES.get(job.get("model_state") or "", "g")
    sc = f' <i>{job["model_score"]} %</i>' if job.get("model_score") and job.get("model_state") in ("auto", "tentative") else ""
    return (f'<a class="bdg mchip {c}" href="models#m{m["id"]}" title="{e(t("Modell"))}">'
            f'🧩 {e(m["name"])}{sc}</a>')


def _why(codes: list[str]) -> str:
    return " · ".join(f"{t(REASONS[c][0])} +{REASONS[c][1]}" for c in codes if c in REASONS)


def hint_card(store: Store, job: dict[str, Any], back: str, all_models: dict[int, str] | None = None,
              with_job: bool = False) -> str:
    """Frage zur Modell-Zuordnung mit Antworten (Banner auf der Druckseite und Seite „Hinweise“)."""
    if not job.get("model_hint"):
        return ""
    all_models = all_models if all_models is not None else names(store)
    st = job.get("model_state") or "tentative"
    cands = [c for c in json.loads(job.get("model_cands") or "[]") if c[0] in all_models]
    act = f"api/jobs/{job['id']}/model"
    hid = f'<input type="hidden" name="back" value="{e(back)}">'
    head = {
        "tentative": t("Vorläufig zugeordnet – stimmt das?"),
        "suggest": t("Ähnliches Modell gefunden – gehört der Druck dazu?"),
        "critical": t("Zwei Modelle passen fast gleich gut – welches ist es?"),
    }.get(st, "")
    opts = ""
    for c in cands[:3] if st != "tentative" else cands[:1]:
        lab = t("Ja, „{m}“", m=all_models[c[0]]) if st != "critical" else f"„{all_models[c[0]]}“"
        opts += (f'<form method="post" action="{act}">{hid}<input type="hidden" name="model" value="{c[0]}">'
                 f'<button class="btn {"primary" if c is cands[0] and st != "critical" else ""}">✓ {e(lab)} <i>{c[1]} %</i></button>'
                 f'<small>{e(_why(c[2]))}</small></form>')
    other = "".join(f'<option value="{mid}">{e(n)}</option>' for mid, n in sorted(all_models.items(), key=lambda x: x[1].casefold())
                    if mid != job.get("model_id"))
    pick = (f'<form method="post" action="{act}" class="mpick">{hid}<select name="model" onchange="this.form.requestSubmit()">'
            f'<option value="">{e(t("Anderes Modell …"))}</option>{other}</select></form>') if other else ""
    new = (f'<form method="post" action="{act}">{hid}<input type="hidden" name="do" value="new">'
           f'<button class="btn">＋ {e(t("Neues Modell"))}</button></form>')
    who = ""
    if with_job:
        who = (f'<a class="mhj" href="job?id={job["id"]}#mh">{_img(job)}<span><b>{e(job.get("task_name") or "–")}</b>'
               f'<small>{e(code_label(job.get("code")))} · {e(fmt_date(job.get("start_ts")))}'
               + (f' · {t("Platte {n}", n=job["gcode_plate"])}' if job.get("gcode_plate") else "") + '</small></span></a>')
    return (f'<div class="mhint {STAGES.get(st, "y")}" id="mh">{who}<div class="mhq"><b>🧩 {e(head)}</b>'
            f'<div class="mhopts">{opts}{pick}{new}</div></div></div>')


def _img(job: dict[str, Any]) -> str:
    return (f'<img src="images/{e(job["image"])}" alt="" loading="lazy">' if job.get("image")
            else '<span class="ph"></span>')


def page_hints(store: Store) -> str:
    from . import extras
    from .ui import layout

    allm = names(store)
    cards = "".join(hint_card(store, j, "hints", allm, with_job=True) for j in open_hints(store))
    low = extras.low_stock(store)
    stock = ""
    if low:
        rows = ""
        for a in low:
            buy = (f' <a href="{e(a["url"])}" target="_blank" rel="noopener">🛒 {e(t("Kaufen"))}</a>'
                   if a.get("url") and str(a["url"]).startswith(("http://", "https://")) else "")
            free = (a.get("stock") or 0) - (a.get("reserved") or 0)
            rows += (f'<li><span>{e(a.get("icon") or "🔹")} <b>{e(a["name"])}</b></span>'
                     f'<span class="muted">{e(t("{n} frei", n=fmt_input(round(free, 2))))}</span>{buy}</li>')
        stock = f'<h2>🧰 {e(t("Zubehör bald nachkaufen"))}</h2><ul class="lowl">{rows}</ul><p><a href="settings#zubehoer">{e(t("Zum Inventar"))} →</a></p>'
    empty = "" if cards or stock else f'<p class="muted">✓ {e(t("Keine offenen Hinweise."))}</p>'
    intro = (f'<p class="muted">{e(t("printlog ordnet jeden fertigen Druck einem Modell (Design) zu. Ist es nicht sicher, fragt es hier nach. Deine Antwort merkt es sich."))}'
             f' <a href="models">{e(t("Alle Modelle"))} →</a></p>')
    body = (f'<h2 style="margin-top:4px">🔔 {e(t("Hinweise"))}</h2>{intro}'
            f'{"<h2>🧩 " + e(t("Modell prüfen")) + "</h2>" if cards else ""}<div class="mhl">{cards}</div>{stock}{empty}')
    return layout(t("Hinweise"), "", f'<style>{CSS}</style>{body}')


def page_models(store: Store, q: str | None = None) -> str:
    from .ui import layout

    rows = [dict(r) for r in store._conn.execute(
        "SELECT models.*, COUNT(jobs.id) AS n, MAX(jobs.start_ts) AS last, "
        "(SELECT image FROM jobs j2 WHERE j2.model_id = models.id AND j2.image IS NOT NULL ORDER BY j2.start_ts DESC LIMIT 1) AS image, "
        "SUM(CASE WHEN jobs.model_hint = 1 THEN 1 ELSE 0 END) AS open "
        "FROM models LEFT JOIN jobs ON jobs.model_id = models.id AND jobs.kind IS NULL "
        "GROUP BY models.id ORDER BY last DESC NULLS LAST, models.name COLLATE NOCASE")]
    aliases: dict[int, list[str]] = {}
    for r in store._conn.execute("SELECT * FROM model_aliases ORDER BY alias"):
        aliases.setdefault(r["model_id"], []).append(r["alias"])
    if q:
        qn = normalize(q)
        rows = [r for r in rows if qn in normalize(r["name"]) or any(qn in a for a in aliases.get(r["id"], []))]
    allm = sorted(((r["id"], r["name"]) for r in rows), key=lambda x: x[1].casefold())
    items = ""
    for r in rows:
        mid = r["id"]
        act = f"api/models/{mid}"
        al = "".join(f'<form method="post" action="{act}" class="al"><input type="hidden" name="unalias" value="{e(a)}">'
                     f'<span>{e(a)}</span><button title="{e(t("Alias entfernen"))}">×</button></form>' for a in aliases.get(mid, []))
        spread = r.get("design_mode") == "spread"
        merge_opts = "".join(f'<option value="{i}">{e(n)}</option>' for i, n in allm if i != mid)
        dot_ = f' <a class="mdot y" href="hints" title="{e(t("offene Hinweise"))}"></a>' if r.get("open") else ""
        items += (
            f'<div class="mdrow" id="m{mid}"><div class="mimg">{_img(r)}</div>'
            f'<form method="post" action="{act}" class="mdf"><div class="mdname"><input name="name" value="{e(r["name"])}" maxlength="120" aria-label="{e(t("Name"))}">'
            f'<small class="muted">{dot_.strip()}<a href="./?model={mid}">{e(t("{n} Drucke", n=r["n"]))}</a>'
            + (f' · {e(t("zuletzt {d}", d=fmt_date(r["last"])))}' if r.get("last") else "") + f'</small>'
            f'<div class="als">{al}</div></div>'
            f'<label class="mdes">✏️ {e(t("Design"))}<span><input class="num" name="design_min" value="{fmt_input(r.get("design_min")) if r.get("design_min") else ""}" inputmode="decimal" placeholder="0"> min</span></label>'
            f'<label class="mmode">{e(t("Berechnen"))}<select name="design_mode"><option value="first">{e(t("einmal beim ersten Druck"))}</option>'
            f'<option value="spread"{" selected" if spread else ""}>{e(t("verteilt auf"))}</option></select>'
            f'<span class="spn"{"" if spread else " hidden"}><input class="num" name="spread_n" value="{r.get("spread_n") or 3}" inputmode="numeric"> {e(t("Drucke"))}</span></label>'
            f'<button class="btn">{e(t("Speichern"))}</button></form>'
            f'<form method="post" action="{act}" class="mmerge"><select name="merge" onchange="if(this.value&&confirm(this.dataset.q))this.form.requestSubmit()" data-q="{e(t("Zusammenlegen? Drucke und Namen wandern in das gewählte Modell."))}">'
            f'<option value="">{e(t("Zusammenlegen mit …"))}</option>{merge_opts}</select></form></div>')
    if not items:
        items = f'<p class="muted">{e(t("Noch keine Modelle. Sie entstehen automatisch, sobald ein Druck fertig ist."))}</p>'
    intro = e(t("Ein Modell ist ein Design, das du öfter druckst, egal auf welchem Drucker oder welcher Platte. printlog erkennt es am Namen, am Vorschaubild und an der Druckdatei. Die Designzeit wird einmal beim ersten berechneten Druck berechnet oder auf mehrere verteilt."))
    search = (f'<form class="msrch" method="get"><input type="search" name="q" value="{e(q or "")}" placeholder="{e(t("Modell suchen …"))}"></form>')
    js = ("document.querySelectorAll('.mmode select').forEach(s=>s.onchange=()=>{s.parentElement.querySelector('.spn').hidden=s.value!=='spread'});"
          "if(location.hash){const el=document.querySelector(location.hash);if(el)el.classList.add('mdhl')}")
    body = f'<h2 style="margin-top:4px">🧩 {e(t("Modelle"))}</h2><p class="muted">{intro} <a href="hints">🔔 {e(t("Hinweise"))}</a></p>{search}<div class="ml">{items}</div>'
    return layout(t("Modelle"), "", f'<style>{CSS}</style>{body}', js)


def notify_text(hints: list[str]) -> str:
    return "\n".join(hints[:5]) + (f"\n… +{len(hints) - 5}" if len(hints) > 5 else "")


CSS = """
.mdot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-left:6px;vertical-align:middle;background:var(--warn)}
.mdot.o{background:#e8873a}.mdot.r{background:var(--bad)}.mdot.y{background:#e3b505}
.mchip{text-decoration:none;color:var(--fg)}.mchip i{font-style:normal;color:var(--muted);font-size:12px;margin-left:2px}
.mchip.y{border-color:#e3b505}.mchip.o{border-color:#e8873a}.mchip.r{border-color:var(--bad)}
.mhint{display:flex;gap:14px;align-items:flex-start;border:1px solid var(--line);border-left:5px solid #e3b505;border-radius:14px;padding:12px 16px;
 background:var(--card);margin-bottom:14px}
.mhint.o{border-left-color:#e8873a}.mhint.r{border-left-color:var(--bad)}
.mhq{flex:1;min-width:0}.mhq>b{display:block;margin-bottom:8px}
.mhopts{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-start}
.mhopts form{display:flex;flex-direction:column;gap:3px;margin:0}.mhopts small{color:var(--muted);font-size:11.5px;max-width:260px}
.mhopts .btn i{font-style:normal;opacity:.7;font-size:12px;margin-left:4px}.mhopts select{min-height:38px}
.mhj{display:flex;gap:10px;align-items:center;text-decoration:none;color:var(--fg);width:250px;flex:none}
.mhj img,.mhj .ph{width:54px;height:54px;border-radius:10px;object-fit:cover;background:var(--bg);flex:none}
.mhj b{display:block;font-size:14.5px;overflow-wrap:anywhere}.mhj small{color:var(--muted);font-size:12px}
.mhl{display:flex;flex-direction:column}.lowl{list-style:none;padding:0;margin:0 0 8px}
.lowl li{display:flex;gap:12px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line);flex-wrap:wrap}
.msrch input{width:100%;max-width:420px;margin:4px 0 14px}
.ml{display:flex;flex-direction:column;gap:10px}
.mdrow{display:grid;grid-template-columns:64px minmax(0,1fr) 200px;gap:14px;align-items:start;background:var(--card);border:1px solid var(--line);
 border-radius:14px;padding:12px 14px}
.mdrow.mdhl{box-shadow:0 0 0 2px var(--accent)}
.mimg img,.mimg .ph{width:64px;height:64px;border-radius:10px;object-fit:cover;display:block;background:var(--bg)}
.mdf{display:grid;grid-template-columns:minmax(0,1fr) auto auto auto;gap:12px;align-items:start;margin:0}
.mdname input{font-weight:700;width:100%;box-sizing:border-box}.mdname small{display:block;margin-top:4px}
.als{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.al{display:inline-flex;align-items:center;gap:2px;border:1px solid var(--line);border-radius:999px;padding:0 2px 0 9px;font-size:12px;color:var(--muted);margin:0}
.al button{border:0;background:none;color:var(--muted);cursor:pointer;padding:2px 6px;min-height:0}
.mdes,.mmode{display:flex;flex-direction:column;gap:4px;font-size:12.5px;color:var(--muted);font-weight:600}
.mdes span,.mmode .spn{display:flex;align-items:center;gap:6px;color:var(--fg)}.mdes input,.mmode input{width:64px;text-align:right}
.mmerge select{width:100%}.mmode .spn[hidden]{display:none}
@media (max-width:760px){.mhint{flex-direction:column}.mhj{width:auto}
 .mdrow{grid-template-columns:52px minmax(0,1fr)}.mimg img,.mimg .ph{width:52px;height:52px}.mmerge{grid-column:1/-1}
 .mdf{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.mmode select{width:100%;min-width:0}.mdf>*{min-width:0}.mdf .mdname{grid-column:1/-1}.mdf .btn{grid-column:1/-1}}
"""
