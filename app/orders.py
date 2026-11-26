"""Aufträge: was ein Kunde bestellt hat, mit Positionen und Stückzahl – von der Bestellung bis zur Abrechnung.

* **Auftrag** = Kunde + Titel + optional „bis“ (Datum) und vereinbarter Preis. **Positionen** sind Modelle
  (siehe app/models.py) oder Freitext für etwas, das noch nie gedruckt wurde.
* **Drucke** gehören über ``jobs.order_item_id`` zu einer Position. Fertige Drucke zählen ihre Stückzahl zur
  Position, Fehldrucke zählen nicht, laufende Drucke machen die Position „in Arbeit“. Beim Zuordnen bekommt der
  Druck den Kunden des Auftrags.
* **Stand**: offen → in Arbeit → fertig (alle Positionen gedruckt) → abgerechnet („Zur Abrechnung“ fasst die Drucke
  zu einem Projekt zusammen, der vereinbarte Preis wird Endpreis) → Archiv, sobald bezahlt und übergeben.
* **Schätzung** aus den letzten Drucken eines Modells: Stück pro Platte, Druckzeit und Gramm pro Platte, Kosten
  pro Stück. Daraus „noch zu drucken“ und ein Preis mit Kundenmarge.
* **Favoriten** (★ am Modell) und „Wieder drucken für …“ legen schnell einen Auftrag an.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import date, datetime
from typing import Any

from . import extras as _extras
from . import plates as _plates
from .db import Store, code_label
from .i18n import N_, t, tn
from .ui import e, fmt_date, fmt_hours, fmt_input, fmt_num, layout

log = logging.getLogger(__name__)

STATES = {"open": N_("Offen"), "running": N_("In Arbeit"), "done": N_("Fertig · abrechnen"), "billed": N_("Abgerechnet"),
          "archived": N_("Archiv")}
STATE_SHORT = {"open": N_("offen"), "running": N_("in Arbeit"), "done": N_("fertig"), "billed": N_("abgerechnet"),
               "archived": N_("erledigt")}
OFTEN = 2        # „Bestellt oft“ ab so vielen Drucken für einen Kunden
STATS_N = 6      # Schätzung aus den letzten N guten Drucken eines Modells


# ---- Lesen ------------------------------------------------------------------------

def order(store: Store, oid: int) -> dict[str, Any] | None:
    r = store._conn.execute(
        "SELECT o.*, c.name AS customer_name, c.margin_pct AS customer_margin, c.work_margin AS customer_work, "
        "COALESCE(c.no_billing, 0) AS customer_nobill FROM orders o LEFT JOIN customers c ON c.id = o.customer_id WHERE o.id = ?",
        (oid,)).fetchone()
    return _full(store, [dict(r)])[0] if r else None


def all_orders(store: Store, customer_id: int | None = None) -> list[dict[str, Any]]:
    """Alle Aufträge mit Positionen, Fortschritt und Stand (fällig zuerst, ohne Termin danach)."""
    sql = ("SELECT o.*, c.name AS customer_name, c.margin_pct AS customer_margin, c.work_margin AS customer_work, "
           "COALESCE(c.no_billing, 0) AS customer_nobill FROM orders o LEFT JOIN customers c ON c.id = o.customer_id")
    args: list[Any] = []
    if customer_id is not None:
        sql += " WHERE o.customer_id = ?"
        args.append(customer_id)
    sql += " ORDER BY o.due IS NULL, o.due, o.created_at"
    return _full(store, [dict(r) for r in store._conn.execute(sql, args)])


def _full(store: Store, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not orders:
        return orders
    ids = [o["id"] for o in orders]
    marks = ", ".join("?" for _ in ids)
    its = [dict(r) for r in store._conn.execute(
        "SELECT i.*, m.name AS model_name, m.fav AS model_fav, "
        "(SELECT image FROM jobs j WHERE j.model_id = i.model_id AND j.image IS NOT NULL AND j.kind IS NULL ORDER BY j.start_ts DESC LIMIT 1) AS image "
        f"FROM order_items i LEFT JOIN models m ON m.id = i.model_id WHERE i.order_id IN ({marks}) ORDER BY i.id", ids)]
    jobs = linked_jobs(store, [i["id"] for i in its])
    stats = model_stats(store, [i["model_id"] for i in its if i["model_id"]])
    by_order: dict[int, list[dict[str, Any]]] = {}
    for i in its:
        js = jobs.get(i["id"], [])
        i["jobs"] = js
        i["done"] = max(0, sum((j.get("quantity") or 1) for j in js if j["state"] == "done" and not j["is_scrap"]) + (i["adjust"] or 0))
        i["running"] = [j for j in js if j["state"] != "done"]
        i["label"] = i["model_name"] or i["name"] or t("Position")
        i["stats"] = stats.get(i["model_id"]) if i["model_id"] else None
        i["pl"] = _plates.summary(store, i)
        if i["pl"]:  # pro Platte: Sätze statt Stück
            i["done"] = i["pl"]["sets_done"]
        by_order.setdefault(i["order_id"], []).append(i)
    for o in orders:
        o["items"] = by_order.get(o["id"], [])
        o["state"] = _state(store, o)
    return orders


def linked_jobs(store: Store, item_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    if not item_ids:
        return out
    marks = ", ".join("?" for _ in item_ids)
    for r in store._conn.execute(
            f"SELECT jobs.*, {Store.SCRAP_SQL} AS is_scrap FROM jobs WHERE jobs.kind IS NULL AND jobs.order_item_id IN ({marks}) "
            "ORDER BY jobs.start_ts DESC, jobs.id DESC", item_ids):
        out.setdefault(r["order_item_id"], []).append(dict(r))
    return out


def _settled(store: Store, job: dict[str, Any], customer_id: int | None = None) -> bool:
    """Abgerechneter Druck/Projekt bezahlt und übergeben (oder keine Abrechnung nötig)? Bei aufgeteilten
    Drucken zählt der Anteil des Auftragskunden (der Rest liegt im Vorrat)."""
    from .pages import amount_due, has_billing

    if job.get("project_id"):
        job = store.get(job["project_id"]) or job
    if job.get("split"):
        mine = [x for x in store.shares([job["id"]]).get(job["id"], [])
                if customer_id and x.get("customer_id") == customer_id and x.get("share_kind") != "out"]
        return bool(mine) and all(_settled(store, x) for x in mine)
    if not has_billing(job):
        return True
    due = amount_due(job)
    paid = job.get("gift") or due is None or (job.get("paid") or 0) >= due - 0.005
    return bool(paid) and (job.get("delivered") or 0) >= (job.get("quantity") or 1)


def _state(store: Store, o: dict[str, Any]) -> str:
    if o.get("closed"):
        return "archived"
    if o.get("billed_job"):
        bj = store.get(o["billed_job"])
        if bj:
            o["billed"] = bj
            return "archived" if _settled(store, bj, o.get("customer_id")) else "billed"
    its = o["items"]
    if its and all(i["done"] >= i["qty"] for i in its):
        return "done"
    if any(i["done"] > 0 or i["running"] for i in its):
        return "running"
    return "open"


def model_stats(store: Store, model_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Je Modell aus den letzten guten Drucken: Stück pro Platte, Minuten und Gramm pro Platte, Kosten pro Stück."""
    out: dict[int, dict[str, Any]] = {}
    for mid in dict.fromkeys(model_ids):
        rows = [dict(r) for r in store._conn.execute(
            f"SELECT quantity, start_ts, end_ts, filament_g, weight_g, cost_total, cost_work FROM jobs "
            f"WHERE model_id = ? AND kind IS NULL AND state = 'done' AND NOT {Store.SCRAP_SQL} "
            "ORDER BY start_ts DESC LIMIT ?", (mid, STATS_N))]
        if not rows:
            continue
        mins = [(r["end_ts"] - r["start_ts"]) / 60 for r in rows if r["start_ts"] and r["end_ts"] and r["end_ts"] > r["start_ts"]]
        grams = [g for g in ((r["filament_g"] if r["filament_g"] is not None else r["weight_g"]) for r in rows) if g]
        cost = [r["cost_total"] / (r["quantity"] or 1) for r in rows if r["cost_total"] is not None]
        work = [(r["cost_work"] or 0) / (r["quantity"] or 1) for r in rows if r["cost_total"] is not None]
        out[mid] = {
            "per_plate": max(1, int(rows[0]["quantity"] or 1)),
            "min": sum(mins) / len(mins) if mins else None,
            "g": sum(grams) / len(grams) if grams else None,
            "piece_cost": sum(cost) / len(cost) if cost else None,
            "piece_work": sum(work) / len(work) if work else 0.0,
            "n": len(rows),
        }
    return out


def estimate(o: dict[str, Any]) -> dict[str, Any]:
    """Stück, Restdruckzeit, Platten und Preis eines Auftrags (geschätzt aus den Modellen)."""
    qty = sum(i["qty"] for i in o["items"])
    done = sum(min(i["done"], i["qty"]) for i in o["items"])
    rest_min, plates, cost, work, missing = 0.0, 0, 0.0, 0.0, 0
    for i in o["items"]:
        if i.get("pl"):
            sm = i["pl"]
            plates += sm["n_open"]
            rest_min += sm["rest_min"]
            missing += sm["missing"] + (1 if sm["cost_missing"] else 0)
            cost += sm["cost"]
            work += sm["work"]
            continue
        s = i.get("stats") or {}
        pp = int(s.get("per_plate") or i.get("per_plate") or 1)
        rest = max(i["qty"] - i["done"], 0)
        p = math.ceil(rest / pp) if rest else 0
        plates += p
        m = s.get("min") or i.get("est_min")
        if p and m:
            rest_min += p * m
        elif p:
            missing += 1
        if s.get("piece_cost") is not None:
            cost += i["qty"] * s["piece_cost"]
            work += i["qty"] * (s.get("piece_work") or 0)
        else:
            missing += 0 if i["qty"] <= i["done"] else 1
    price = None
    if cost or work:
        price = _extras.sell_price(cost, work, o.get("customer_margin") or 0, bool(o.get("customer_work")))
    spent = sum((j.get("cost_total") or 0) + (j.get("cost_work") or 0) for i in o["items"] for j in i["jobs"])
    hours = sum((j["end_ts"] - j["start_ts"]) / 3600 for i in o["items"] for j in i["jobs"]
                if j.get("start_ts") and j.get("end_ts") and j["end_ts"] > j["start_ts"])
    n_jobs = sum(len(i["jobs"]) for i in o["items"])
    return {"qty": qty, "done": done, "rest_min": rest_min, "plates": plates, "price": price, "missing": missing,
            "spent": spent, "hours": hours, "n_jobs": n_jobs}


def top_customers(store: Store) -> dict[int, tuple[int, str, int]]:
    """Je Modell der Kunde, für den es am häufigsten gedruckt wurde → (Kunden-ID, Name, Anzahl)."""
    out: dict[int, tuple[int, str, int]] = {}
    for r in store._conn.execute(
            "SELECT j.model_id, j.customer_id, c.name, COUNT(*) n, MAX(j.start_ts) last FROM jobs j JOIN customers c ON c.id = j.customer_id "
            "WHERE j.kind IS NULL AND j.model_id IS NOT NULL GROUP BY j.model_id, j.customer_id ORDER BY n DESC, last DESC"):
        out.setdefault(r["model_id"], (r["customer_id"], r["name"], r["n"]))
    return out


def often(store: Store, cid: int) -> list[dict[str, Any]]:
    """„Bestellt oft“: Modelle, die für den Kunden mindestens OFTEN-mal gedruckt wurden, und Favoriten, die er schon hatte."""
    rows = [dict(r) for r in store._conn.execute(
        "SELECT m.id, m.name, m.fav, COUNT(j.id) n, MAX(j.start_ts) last, SUM(COALESCE(j.cost_total, 0)) cost, "
        "(SELECT j2.quantity FROM jobs j2 WHERE j2.model_id = m.id AND j2.customer_id = ? AND j2.kind IS NULL ORDER BY j2.start_ts DESC LIMIT 1) last_qty, "
        "(SELECT image FROM jobs j3 WHERE j3.model_id = m.id AND j3.image IS NOT NULL AND j3.kind IS NULL ORDER BY j3.start_ts DESC LIMIT 1) image "
        "FROM models m JOIN jobs j ON j.model_id = m.id AND j.kind IS NULL AND j.customer_id = ? "
        "GROUP BY m.id HAVING n >= ? OR m.fav = 1 ORDER BY m.fav DESC, n DESC, last DESC", (cid, cid, OFTEN))]
    return rows


def open_orders(store: Store) -> list[dict[str, Any]]:
    """Aufträge, die noch nicht abgerechnet sind (für „Wieder drucken“ und das Zuordnen)."""
    return [o for o in all_orders(store) if o["state"] in ("open", "running", "done")]


def item_of_job(store: Store, job: dict[str, Any]) -> dict[str, Any] | None:
    if not job.get("order_item_id"):
        return None
    r = store._conn.execute(
        "SELECT i.*, o.title, o.customer_id, c.name AS customer_name FROM order_items i JOIN orders o ON o.id = i.order_id "
        "LEFT JOIN customers c ON c.id = o.customer_id WHERE i.id = ?", (job["order_item_id"],)).fetchone()
    return dict(r) if r else None


def counts(store: Store) -> dict[str, int]:
    out = {k: 0 for k in STATES}
    for o in all_orders(store):
        out[o["state"]] += 1
    return out


# ---- Ändern ------------------------------------------------------------------------

def _due(text: Any) -> str | None:
    s = str(text or "").strip()
    try:
        return date.fromisoformat(s).isoformat() if s else None
    except ValueError:
        return None


def create_order(store: Store, customer_id: int | None, title: str | None = None, due: Any = None,
                 agreed_price: float | None = None, note: str | None = None) -> int:
    with store._lock, store._conn:
        cur = store._conn.execute(
            "INSERT INTO orders (customer_id, title, due, agreed_price, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (customer_id, (title or "").strip()[:120] or None, _due(due), agreed_price, (note or "").strip() or None, time.time()))
        return int(cur.lastrowid)


def save_order(store: Store, oid: int, **f: Any) -> None:
    allowed = {k: v for k, v in f.items() if k in ("customer_id", "title", "due", "agreed_price", "note", "closed")}
    if "due" in allowed:
        allowed["due"] = _due(allowed["due"])
    if "title" in allowed:
        allowed["title"] = (allowed["title"] or "").strip()[:120] or None
    if not allowed:
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with store._lock, store._conn:
        store._conn.execute(f"UPDATE orders SET {sets} WHERE id = ?", [*allowed.values(), oid])
    if "customer_id" in allowed and allowed["customer_id"]:
        ids = [r[0] for r in store._conn.execute(
            "SELECT j.id FROM jobs j JOIN order_items i ON i.id = j.order_item_id WHERE i.order_id = ?", (oid,))]
        store.assign_customer(ids, allowed["customer_id"])


def add_item(store: Store, oid: int, model_id: int | None, name: str | None, qty: int) -> int:
    if model_id and not store._conn.execute("SELECT 1 FROM models WHERE id = ?", (model_id,)).fetchone():
        model_id = None
    qty = max(1, min(int(qty or 1), 9999))
    if model_id:  # gleiches Modell schon im Auftrag: Stückzahl erhöhen
        r = store._conn.execute("SELECT id, qty FROM order_items WHERE order_id = ? AND model_id = ?", (oid, model_id)).fetchone()
        if r:
            set_item(store, r["id"], qty=r["qty"] + qty)
            return int(r["id"])
    by_plate = 1 if _plates.has_plates(store, model_id) else None
    with store._lock, store._conn:
        cur = store._conn.execute(
            "INSERT INTO order_items (order_id, model_id, name, qty, by_plate, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (oid, model_id, None if model_id else (name or "").strip()[:120] or t("Position"), qty, by_plate, time.time()))
        return int(cur.lastrowid)


def set_item(store: Store, item_id: int, **f: Any) -> None:
    allowed = {k: v for k, v in f.items() if k in ("qty", "adjust", "name", "model_id", "est_min", "est_g", "per_plate", "by_plate", "n_plates")}
    if "qty" in allowed:
        allowed["qty"] = max(1, min(int(allowed["qty"] or 1), 9999))
    if not allowed:
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with store._lock, store._conn:
        store._conn.execute(f"UPDATE order_items SET {sets} WHERE id = ?", [*allowed.values(), item_id])


def delete_item(store: Store, item_id: int) -> None:
    _plates.forget_item(store, item_id)
    with store._lock, store._conn:
        store._conn.execute("UPDATE jobs SET order_item_id = NULL WHERE order_item_id = ?", (item_id,))
        store._conn.execute("DELETE FROM order_items WHERE id = ?", (item_id,))


def delete_order(store: Store, oid: int) -> None:
    """Auftrag löschen: Drucke bleiben, sind danach nur keinem Auftrag mehr zugeordnet."""
    for r in store._conn.execute("SELECT id FROM order_items WHERE order_id = ?", (oid,)).fetchall():
        _plates.forget_item(store, r["id"])
    with store._lock, store._conn:
        store._conn.execute("UPDATE jobs SET order_item_id = NULL WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id = ?)", (oid,))
        store._conn.execute("DELETE FROM order_items WHERE order_id = ?", (oid,))
        store._conn.execute("DELETE FROM orders WHERE id = ?", (oid,))


def link(store: Store, job_id: int, item_id: int | None, state: str = "confirmed") -> bool:
    """Druck einer Position zuordnen (None = lösen). Der Druck bekommt den Kunden des Auftrags."""
    job = store._conn.execute("SELECT id, kind FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job or job["kind"]:
        return False
    cid = None
    if item_id is not None:
        r = store._conn.execute("SELECT o.customer_id FROM order_items i JOIN orders o ON o.id = i.order_id WHERE i.id = ?",
                                (item_id,)).fetchone()
        if not r:
            return False
        cid = r[0]
    with store._lock, store._conn:
        store._conn.execute("UPDATE jobs SET order_item_id = ?, order_state = ?, order_cands = NULL WHERE id = ?",
                            (item_id, state if item_id is not None else "none", job_id))
    if cid:
        store.assign_customer([job_id], cid)
    if item_id is not None:
        _prefill(store, job_id, item_id)
        adopt_models(store)
    return True


def _prefill(store: Store, job_id: int, item_id: int) -> None:
    """Stück pro Platte vorbelegen, wenn am Druck noch keine Stückzahl steht."""
    j = store._conn.execute("SELECT quantity, model_id, gcode_plate FROM jobs WHERE id = ?", (job_id,)).fetchone()
    it = store._conn.execute("SELECT model_id, per_plate FROM order_items WHERE id = ?", (item_id,)).fetchone()
    if not j or j["quantity"] is not None:
        return
    mid = j["model_id"] or (it["model_id"] if it else None)
    pp = per_plate(store, mid, j["gcode_plate"]) if mid else (it["per_plate"] if it else None)
    if pp and pp > 1:
        store.update(job_id, quantity=int(pp))


def per_plate(store: Store, model_id: int | None, plate: int | None = None) -> int | None:
    """Stück pro Platte vom letzten guten Druck des Modells, bei bekannter Plattennummer von dieser Platte."""
    if not model_id:
        return None
    if plate:
        ps = _plates.plate_stats(store, model_id).get(int(plate))
        if ps:
            return ps["pieces"]
    return (model_stats(store, [model_id]).get(model_id) or {}).get("per_plate")


def adopt_models(store: Store) -> None:
    """Freitext-Position bekommt das Modell, sobald ihr erster Druck einem Modell zugeordnet ist."""
    rows = store._conn.execute(
        "SELECT i.id, (SELECT j.model_id FROM jobs j WHERE j.order_item_id = i.id AND j.model_id IS NOT NULL AND j.kind IS NULL "
        "AND j.model_state IN ('auto', 'confirmed', 'new') ORDER BY j.start_ts LIMIT 1) AS mid FROM order_items i "
        "WHERE i.model_id IS NULL").fetchall()
    for r in rows:
        if not r["mid"]:
            continue
        with store._lock, store._conn:
            store._conn.execute("UPDATE order_items SET model_id = ? WHERE id = ?", (r["mid"], r["id"]))
        _plates.adopt(store, r["id"], r["mid"])


def candidates(store: Store, o: dict[str, Any], limit: int = 12) -> list[dict[str, Any]]:
    """Drucke ohne Auftrag zum Zuordnen: passendes Modell zuerst, dann gleicher Kunde, dann die neuesten."""
    mids = [i["model_id"] for i in o["items"] if i["model_id"]] or [-1]
    marks = ", ".join("?" for _ in mids)
    rows = store._conn.execute(
        f"SELECT jobs.id, jobs.code, jobs.task_name, jobs.start_ts, jobs.state, jobs.quantity, jobs.printer, jobs.printer_name, "
        f"jobs.model_id, jobs.image, jobs.customer_id, {Store.SCRAP_SQL} AS is_scrap FROM jobs "
        "WHERE jobs.kind IS NULL AND jobs.order_item_id IS NULL AND jobs.start_ts >= ? "
        f"ORDER BY (jobs.model_id IN ({marks})) DESC, (jobs.customer_id IS NOT NULL AND jobs.customer_id = ?) DESC, jobs.start_ts DESC LIMIT ?",
        [o["created_at"] - 60 * 86400, *mids, o.get("customer_id") or -1, limit]).fetchall()
    return [dict(r) for r in rows]


def surplus(o: dict[str, Any]) -> int:
    """Mehr gedruckt als bestellt (nur Aufträge mit einer Position und Kunde): so viele Stück gehen in den Vorrat."""
    if len(o["items"]) != 1 or not o.get("customer_id") or o["items"][0].get("pl"):
        return 0
    i = o["items"][0]
    printed = sum((j.get("quantity") or 1) for j in i["jobs"] if j["state"] == "done" and not j["is_scrap"])
    return max(0, printed - i["qty"])


def bill(store: Store, oid: int, stock: bool = False) -> tuple[int | None, str | None]:
    """„Zur Abrechnung“: Drucke zum Projekt zusammenfassen (ein einzelner Druck bleibt einzeln), vereinbarter
    Preis wird Endpreis. Mit ``stock`` geht der Überschuss in den Vorrat: Der Kunde bekommt einen Anteil mit der
    bestellten Stückzahl. Gibt (Projekt-/Druck-ID, Fehlermeldung) zurück."""
    o = order(store, oid)
    if not o:
        return None, None
    jobs = [j for i in o["items"] for j in i["jobs"]]
    if not jobs:
        return None, t("Noch kein Druck zugeordnet.")
    if any(j["state"] != "done" for j in jobs):
        return None, t("Ein Druck läuft noch.")
    ids = sorted({j["id"] for j in jobs})
    projects = {j.get("project_id") for j in jobs}
    if len(ids) == 1 and not jobs[0].get("project_id"):
        target: int | None = ids[0]
    elif len(projects) == 1 and None not in projects:
        target = projects.pop()
    else:
        target = store.create_project(ids, name=o.get("title") or t("Auftrag"), customer_id=o.get("customer_id"))
    if not target:
        return None, t("Abrechnen hat nicht geklappt.")
    if o.get("customer_id"):
        store.assign_customer([target], o["customer_id"])
    if o.get("agreed_price") is not None:
        store.update(target, final_price=o["agreed_price"])
    notes: list[str] = []
    for i in o["items"]:
        if i.get("pl") and i["pl"]["n_stock"]:
            notes += _plates.book_stock(store, i, o.get("customer_id"), free=o.get("agreed_price") is not None)
    extra = surplus(o) if stock else 0
    if extra:
        tj = store.get(target) or {}
        if tj.get("kind") == "project":
            store.update(target, quantity=o["items"][0]["qty"] + extra)
        err = store.set_split(target, [{"sid": None, "customer_id": o["customer_id"], "qty": o["items"][0]["qty"]}])
        if err:
            log.warning("Überschuss in den Vorrat: %s", err)
    with store._lock, store._conn:
        store._conn.execute("UPDATE orders SET billed_job = ?, done_at = COALESCE(done_at, ?) WHERE id = ?", (target, time.time(), oid))
    return target, ("; ".join(notes) or None)


def reopen(store: Store, oid: int) -> None:
    """Wieder öffnen: nicht mehr erledigt, Abrechnung gelöst (Projekt und Preis bleiben bestehen)."""
    with store._lock, store._conn:
        store._conn.execute("UPDATE orders SET closed = NULL, billed_job = NULL WHERE id = ?", (oid,))


def again(store: Store, model_id: int, customer_id: int | None, qty: int, into: int | None = None,
          due: Any = None) -> int:
    """„Wieder drucken für …“: Modell als Position in einen offenen Auftrag oder einen neuen Auftrag."""
    if into and store._conn.execute("SELECT 1 FROM orders WHERE id = ?", (into,)).fetchone():
        oid = into
    else:
        m = store._conn.execute("SELECT name FROM models WHERE id = ?", (model_id,)).fetchone()
        oid = create_order(store, customer_id, m["name"] if m else None, due)
    add_item(store, oid, model_id, None, qty)
    return oid


# ---- Automatik: Druckstart/-ende einem Auftrag zuordnen --------------------------------
#
# Läuft im Sweeper (jede Minute). Ein Druck ohne Auftrag wird mit den offenen Positionen verglichen: gleiches Modell
# (beim Start über Name, Druckdatei und Platte, nach dem Ende über die Modell-Erkennung) oder bei Freitext ein gleicher
# bzw. ähnlicher Name. Passt genau eine Position, ordnet printlog den Druck zu („auto“); passen mehrere oder ist
# „immer fragen“ eingestellt, fragt die Glocke. Nur Drucke ab dem Einschalten der Funktion (``orders_since``).

AUTO_MODES = {"auto": N_("Automatisch, bei Zweifel fragen"), "ask": N_("Immer fragen"), "off": N_("Aus (nur von Hand)")}


def auto_mode(store: Store) -> str:
    m = store.settings().get("order_auto") or "auto"
    return m if m in AUTO_MODES else "auto"


def _open_items(store: Store) -> list[dict[str, Any]]:
    out = []
    for o in all_orders(store):
        if o["state"] not in ("open", "running"):
            continue
        for i in o["items"]:
            if i.get("pl"):
                if i["pl"]["n_open"]:
                    out.append({**i, "order": o})
            elif i["done"] + sum((j.get("quantity") or 1) for j in i["running"]) < i["qty"]:
                out.append({**i, "order": o})
    return out


def matches(store: Store, job: dict[str, Any], items: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Offene Positionen, zu denen der Druck passt (fällig zuerst)."""
    from . import models as mdl

    items = _open_items(store) if items is None else items
    if not items:
        return []
    mid = job.get("model_id") if job.get("model_state") in ("auto", "confirmed", "tentative", "new") else None
    if not mid:
        cands = mdl.candidates(store, job)
        if cands and cands[0][1] >= mdl.TENTATIVE:
            mid = cands[0][0]
    name = mdl.normalize(job.get("task_name"))
    out = []
    for i in items:
        if i["model_id"] and i["model_id"] == mid:
            pass
        elif not i["model_id"] and name and (mdl.normalize(i["name"]) == name or mdl.similar(mdl.normalize(i["name"]), name)):
            pass
        else:
            continue
        if i.get("pl"):  # pro Platte: nur, wenn diese Platte noch gebraucht wird
            nums = {p["plate"] for p in i["pl"]["plates"]}
            plate = job.get("gcode_plate")
            if plate in nums and not any(p == plate for _s, p in i["pl"]["grid"]["open"]):
                continue
        out.append(i)
    out.sort(key=lambda i: (i["order"].get("due") is None, i["order"].get("due") or "", i["order"]["created_at"]))
    return out


def match_pending(store: Store) -> list[str]:
    """Neue Drucke zuordnen oder nachfragen. Gibt Hinweistexte für neue Fragen zurück."""
    mode = auto_mode(store)
    if mode == "off":
        return []
    cfg = store.settings()
    if not cfg.get("orders_since"):
        store.set_setting("orders_since", f"{time.time():.0f}")
        return []
    rows = [dict(r) for r in store._conn.execute(
        f"SELECT jobs.*, {Store.SCRAP_SQL} AS is_scrap FROM jobs WHERE kind IS NULL AND order_item_id IS NULL "
        "AND order_state IS NULL AND start_ts >= ? ORDER BY start_ts", (float(cfg["orders_since"]),))]
    if not rows:
        return []
    items = _open_items(store)
    hints = []
    for j in rows:
        if j["state"] == "done" and j["is_scrap"]:
            store.update(j["id"], order_state="none")
            continue
        found = matches(store, j, items)
        if not found:
            if j["state"] == "done" and j.get("model_state"):  # nach der Modell-Erkennung: endgültig nichts
                store.update(j["id"], order_state="none")
            continue
        if len(found) == 1 and mode == "auto":
            link(store, j["id"], found[0]["id"], state="auto")
            items = _open_items(store)
            continue
        store.update(j["id"], order_state="ask", order_cands=json.dumps([i["id"] for i in found[:4]]))
        hints.append(t("„{job}“ – für welchen Auftrag?", job=j.get("task_name") or "?"))
    adopt_models(store)
    return hints


def answer(store: Store, job_id: int, item_id: int | None) -> None:
    """Antwort auf die Frage der Glocke: Position oder kein Auftrag."""
    if item_id:
        link(store, job_id, item_id)
    else:
        with store._lock, store._conn:
            store._conn.execute("UPDATE jobs SET order_state = 'none', order_cands = NULL WHERE id = ?", (job_id,))


def item_exists(store: Store, item_id: int) -> bool:
    return store._conn.execute("SELECT 1 FROM order_items WHERE id = ?", (item_id,)).fetchone() is not None


def open_questions(store: Store) -> list[dict[str, Any]]:
    return [dict(r) for r in store._conn.execute(
        "SELECT * FROM jobs WHERE kind IS NULL AND order_state = 'ask' AND order_item_id IS NULL ORDER BY start_ts DESC")]


def question_count(store: Store) -> int:
    return int(store._conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind IS NULL AND order_state = 'ask' AND order_item_id IS NULL").fetchone()[0])


def hint_cards(store: Store, back: str = "hints") -> str:
    """Karten für die Seite „Hinweise“: „Druck gestartet – für welchen Auftrag?“."""
    from .ui import printer_names

    names = printer_names(store)
    out = ""
    for j in open_questions(store):
        try:
            ids = [int(x) for x in json.loads(j.get("order_cands") or "[]")]
        except (ValueError, TypeError):
            ids = []
        its = []
        for iid in ids:
            r = store._conn.execute(
                "SELECT i.id, i.qty, i.order_id, COALESCE(m.name, i.name) AS label, o.title, c.name AS customer FROM order_items i "
                "JOIN orders o ON o.id = i.order_id LEFT JOIN models m ON m.id = i.model_id LEFT JOIN customers c ON c.id = o.customer_id "
                "WHERE i.id = ?", (iid,)).fetchone()
            if r:
                its.append(dict(r))
        prog = {i["id"]: i for o in all_orders(store) for i in o["items"]}
        opts = ""
        for k, i in enumerate(its):
            p = prog.get(i["id"])
            done = f'{p["done"]}/{p["qty"]}' if p else str(i["qty"])
            opts += (f'<form method="post" action="orders/answer"><input type="hidden" name="job" value="{j["id"]}">'
                     f'<input type="hidden" name="item" value="{i["id"]}"><input type="hidden" name="back" value="{e(back)}">'
                     f'<button class="btn{" primary" if k == 0 else ""}">{e(i.get("customer") or t("ohne Kunde"))} · {e(i.get("title") or i["label"])} <i>{done}</i></button>'
                     f'{"<small>" + e(t("fällig zuerst")) + "</small>" if k == 0 and len(its) > 1 else ""}</form>')
        opts += (f'<form method="post" action="orders/answer"><input type="hidden" name="job" value="{j["id"]}">'
                 f'<input type="hidden" name="back" value="{e(back)}"><button class="btn">{e(t("Kein Auftrag (Vorrat / eigen)"))}</button></form>')
        img = f'<img src="images/{e(j["image"])}" alt="" loading="lazy">' if j.get("image") else '<span class="ph"></span>'
        running = j["state"] != "done"
        sub = (t("gestartet {d}", d=fmt_date(j.get("start_ts"), "dm_hm")) if running else fmt_date(j.get("start_ts"), "dm_hm"))
        sub += " · " + names.get(j["printer"], j.get("printer_name") or j["printer"])
        if j.get("quantity"):
            sub += " · " + t("{n} Stk.", n=j["quantity"])
        q = t("Druck gestartet – für welchen Auftrag?") if running else t("Für welchen Auftrag war dieser Druck?")
        out += (f'<div class="mhint ord"><a class="mhj" href="job?id={j["id"]}">{img}<span><b>{e(j.get("task_name") or t("Druck"))}</b>'
                f'<small>{e(sub)}</small></span></a><div class="mhq"><b>📋 {e(q)}</b><div class="mhopts">{opts}</div></div></div>')
    return out


# ---- Anzeige: Bausteine ------------------------------------------------------------

def _due_chip(o: dict[str, Any]) -> str:
    if not o.get("due") or o["state"] in ("billed", "archived"):
        return ""
    try:
        d = date.fromisoformat(o["due"])
    except ValueError:
        return ""
    days = (d - date.today()).days
    cls = "late" if days < 0 else "soon" if days <= 7 else ""
    txt = t("überfällig seit {d}", d=fmt_date(datetime(d.year, d.month, d.day), "dm")) if days < 0 \
        else t("bis {d}", d=fmt_date(datetime(d.year, d.month, d.day), "dm"))
    return f'<span class="odue {cls}">{e(txt)}</span>'


def _img(item: dict[str, Any], cls: str = "") -> str:
    if item.get("image"):
        return f'<img class="{cls}" src="images/{e(item["image"])}" alt="" loading="lazy">'
    return f'<span class="{cls} oph">{"🧩" if item.get("model_id") else "💡"}</span>'


def _bar(done: int, qty: int, running: bool) -> str:
    pct = min(100, done / qty * 100) if qty else 0
    cls = "ok" if done >= qty else "run" if running else ""
    return f'<span class="obar"><i class="{cls}" style="width:{pct:.0f}%"></i></span>'


def _live(item: dict[str, Any], names: dict[str, str]) -> str:
    j = item["running"][0]
    who = names.get(j["printer"], j.get("printer_name") or j["printer"])
    return f'<span class="olive">{e(t("läuft auf {p}", p=who))}</span>'


def _status_chip(state: str) -> str:
    return f'<span class="ost {state}">{e(t(STATE_SHORT[state]))}</span>'


def _card(o: dict[str, Any], names: dict[str, str]) -> str:
    pos = ""
    live = ""
    for i in o["items"]:
        run = bool(i["running"])
        if i.get("pl"):
            sm = i["pl"]
            pos += (f'<div class="opos">{_img(i)}<div class="n"><s>{e(i["label"])}</s>{_bar(sm["n_done"], sm["n_print"] or 1, run)}</div>'
                    f'<b class="c" title="{e(t("Sätze fertig · Platten gedruckt"))}">{i["done"]}/{i["qty"]} <small>▦ {sm["n_done"]}/{sm["n_print"]}</small></b></div>')
            if run and not live:
                live = _live(i, names)
            continue
        pos += (f'<div class="opos">{_img(i)}<div class="n"><s>{e(i["label"])}</s>{_bar(i["done"], i["qty"], run)}</div>'
                f'<b class="c">{i["done"]}/{i["qty"]}</b></div>')
        if run and not live:
            live = _live(i, names)
    if not o["items"]:
        pos = f'<p class="muted ofoot">{e(t("Noch keine Positionen."))}</p>'
    foot = ""
    if live:
        foot += f'<div class="ofoot">{live}</div>'
    if o["state"] in ("done", "billed"):
        est = estimate(o)
        price = o.get("agreed_price") if o.get("agreed_price") is not None else est["price"]
        bits = []
        if o["state"] == "billed":
            bits.append(e(t("abgerechnet")))
        if price is not None:
            bits.append(f'<b class="calc">{fmt_num(price, 2, "€")}</b>')
        foot += f'<div class="ofoot">{" · ".join(bits)}</div>'
    title = e(o.get("title") or "")
    return (f'<a class="ocard {o["state"]}" href="order?id={o["id"]}"><div class="oh"><b>{e(o.get("customer_name") or t("ohne Kunde"))}</b>'
            f'<span class="sp"></span>{_due_chip(o)}</div>{f"<div class=ot>{title}</div>" if title else ""}{pos}{foot}</a>')


def _custlist(store: Store) -> str:
    opts = "".join(f'<option value="{e(c["name"])}">' for c in store.customers())
    return f'<datalist id="custlist">{opts}</datalist>'


def _modellist(store: Store) -> str:
    rows = store._conn.execute(
        "SELECT m.id, m.name, m.fav FROM models m LEFT JOIN jobs j ON j.model_id = m.id AND j.kind IS NULL "
        "GROUP BY m.id ORDER BY m.fav DESC, MAX(j.start_ts) DESC NULLS LAST").fetchall()
    opts = "".join(f'<option value="{e(r["name"])}">{"★" if r["fav"] else ""}</option>' for r in rows)
    return f'<datalist id="modellist">{opts}</datalist>'


def model_by_name(store: Store, name: str) -> int | None:
    r = store._conn.execute("SELECT id FROM models WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchone()
    return int(r[0]) if r else None


# ---- Seite „Aufträge“ (Übersicht) ---------------------------------------------------

def page_orders(store: Store, customer_id: int | None = None, archive: bool = False) -> str:
    from .ui import printer_names

    names = printer_names(store)
    orders = all_orders(store, customer_id)
    cust = store.customer(customer_id) if customer_id else None
    chips = (f'<a href="orders" class="{"" if cust else "on"}">{e(t("Alle Kunden"))}</a>'
             + (f'<a class="on" href="orders?c={customer_id}">{e(cust["name"])} ✕</a>' if cust else ""))
    head = (f'<div class="oph1"><h2>📋 {e(t("Aufträge"))}</h2><span class="sp"></span><span class="ochips">{chips}</span>'
            f'<a class="btn primary" href="order?new=1{f"&c={customer_id}" if customer_id else ""}">＋ {e(t("Auftrag"))}</a></div>')
    if archive:
        rows = [o for o in orders if o["state"] == "archived"]
        rows.sort(key=lambda o: -(o.get("done_at") or o["created_at"]))
        lst = "".join(_arch_row(o) for o in rows) or f'<p class="muted">{e(t("Noch nichts im Archiv."))}</p>'
        body = (f'{head}<p><a href="orders{f"?c={customer_id}" if customer_id else ""}">← {e(t("Zur Übersicht"))}</a></p>'
                f'<h3>{e(t("Archiv"))}</h3><div class="oarch">{lst}</div>')
        return layout(t("Aufträge"), "orders", f'<style>{CSS}</style>{body}')
    cols = {"open": [], "running": [], "done": []}
    for o in orders:
        if o["state"] in ("open", "running"):
            cols[o["state"]].append(o)
        elif o["state"] in ("done", "billed"):
            cols["done"].append(o)
    n_arch = sum(1 for o in orders if o["state"] == "archived")
    first = "running" if cols["running"] else "open" if cols["open"] else "done"
    colors = {"open": "var(--muted)", "running": "var(--run)", "done": "var(--ok)"}
    seg_l = {"open": t("Offen"), "running": t("In Arbeit"), "done": t("Fertig")}
    seg = "".join(f'<button type="button" data-k="{k}" class="{"on" if k == first else ""}">{e(seg_l[k])} {len(v)}</button>'
                  for k, v in cols.items())
    board = ""
    for k, v in cols.items():
        cards = "".join(_card(o, names) for o in v)
        extra = ""
        if k == "open":
            extra = f'<a class="oadd" href="order?new=1{f"&c={customer_id}" if customer_id else ""}">＋ {e(t("Auftrag"))}</a>'
        if k == "done":
            extra = (f'<a class="oarchl" href="orders?archiv=1{f"&c={customer_id}" if customer_id else ""}">'
                     f'{e(t("Archiv ({n})", n=n_arch))} →</a>')
        if not cards and k != "open":
            cards = f'<p class="muted oempty">{e(t("Nichts hier."))}</p>'
        board += (f'<div class="ocol{" on" if k == first else ""}" data-k="{k}"><h3><span class="d" style="background:{colors[k]}"></span>'
                  f'{e(t(STATES[k]))}<i>{len(v)}</i></h3>{cards}{extra}</div>')
    intro = e(t("Ein Auftrag ist das, was ein Kunde bestellt hat, mit einer oder mehreren Positionen. Ordne Drucke zu, dann zählt printlog die Stück mit; sind alle gedruckt, geht es mit einem Tipp in die Abrechnung."))
    body = (f'{head}<p class="muted ointro">{intro}</p><div class="oseg">{seg}</div><div class="oboard">{board}</div>')
    return layout(t("Aufträge"), "orders", f'<style>{CSS}</style>{body}', BOARD_JS)


def _arch_row(o: dict[str, Any]) -> str:
    its = ", ".join(f'{i["label"]} {i["done"]}/{i["qty"]}' for i in o["items"])
    when = fmt_date(o.get("done_at") or o["created_at"], "date_s")
    return (f'<a class="oarow" href="order?id={o["id"]}"><span class="d">{when}</span><span><b>{e(o.get("title") or "–")}</b>'
            f'<small>{e(o.get("customer_name") or t("ohne Kunde"))} · {e(its)}</small></span>{_status_chip(o["state"])}</a>')


BOARD_JS = r"""
document.querySelectorAll('.oseg button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.oseg button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('.ocol').forEach(c=>c.classList.toggle('on',c.dataset.k===b.dataset.k))});
"""


# ---- Seite „Auftrag“ ----------------------------------------------------------------

def page_new(store: Store, customer_id: int | None = None) -> str:
    c = store.customer(customer_id) if customer_id else None
    form = (f'<form method="post" action="orders" class="oform card">'
            f'<label>{e(t("Kunde"))}<input name="customer" list="custlist" value="{e(c["name"]) if c else ""}" autocomplete="off" '
            f'placeholder="{e(t("Name (neu oder vorhanden)"))}"></label>'
            f'<label>{e(t("Titel"))}<input name="title" maxlength="120" placeholder="{e(t("z. B. Nachbestellung Herbst"))}"></label>'
            f'<label>{e(t("Bis"))}<input type="date" name="due"></label>'
            f'<label>{e(t("Vereinbarter Preis"))}<input name="agreed" inputmode="decimal" placeholder="{e(t("optional, €"))}"></label>'
            f'<label class="w">{e(t("Erste Position"))}<span class="orow2"><input name="item" list="modellist" autocomplete="off" '
            f'placeholder="{e(t("Modell oder Freitext …"))}"><input name="qty" inputmode="numeric" value="1" class="num" aria-label="{e(t("Stück"))}"></span>'
            f'<small class="muted">{e(t("Ein bekanntes Modell aus der Liste oder Freitext für etwas, das du noch nie gedruckt hast."))}</small></label>'
            f'<label class="w">{e(t("Notiz"))}<textarea name="note" rows="2"></textarea></label>'
            f'<div class="w obtns"><a class="btn" href="orders">{e(t("Abbrechen"))}</a><button class="btn primary">{e(t("Auftrag anlegen"))}</button></div>'
            f'</form>{_custlist(store)}{_modellist(store)}')
    body = f'<a class="back" href="orders">← {e(t("alle Aufträge"))}</a><h2>＋ {e(t("Neuer Auftrag"))}</h2>{form}'
    return layout(t("Neuer Auftrag"), "orders", f'<style>{CSS}</style>{body}')


def page_order(store: Store, o: dict[str, Any], saved: str | None = None, show_link: bool = False) -> str:
    from .ui import printer_names

    names = printer_names(store)
    oid = o["id"]
    act = f"orders/{oid}"
    est = estimate(o)
    st = o["state"]
    meta = []
    if o.get("customer_id"):
        meta.append(f'<a href="customer?id={o["customer_id"]}">{e(o.get("customer_name") or "")}</a>')
        if o.get("customer_margin"):
            meta.append(f'<span class="calc">{e(t("+{p} %", p=fmt_input(o["customer_margin"])))}</span>')
    else:
        meta.append(e(t("ohne Kunde")))
    meta.append(e(t("angelegt {d}", d=fmt_date(o["created_at"], "date_s"))))
    head = (f'<a class="back" href="orders">← {e(t("alle Aufträge"))}</a>'
            f'<div class="odh"><div><h2>{e(o.get("title") or t("Auftrag"))}</h2><div class="meta">{" · ".join(meta)} {_due_chip(o)} {_status_chip(st)}</div></div>'
            f'<span class="sp"></span><button type="button" class="btn" data-tgl="oedit">✏️ {e(t("Bearbeiten"))}</button></div>')
    if saved:
        head += f'<p class="osaved">{e(saved if saved != "1" else t("Gespeichert."))}</p>'

    # Bearbeiten (eingeklappt)
    c_name = o.get("customer_name") or ""
    edit = (f'<form method="post" action="{act}" id="oedit" class="oform card" hidden><input type="hidden" name="do" value="save">'
            f'<label>{e(t("Kunde"))}<input name="customer" list="custlist" value="{e(c_name)}" autocomplete="off"></label>'
            f'<label>{e(t("Titel"))}<input name="title" maxlength="120" value="{e(o.get("title") or "")}"></label>'
            f'<label>{e(t("Bis"))}<input type="date" name="due" value="{e(o.get("due") or "")}"></label>'
            f'<label>{e(t("Vereinbarter Preis"))}<input name="agreed" inputmode="decimal" value="{fmt_input(o.get("agreed_price")) if o.get("agreed_price") is not None else ""}" placeholder="{e(t("optional, €"))}"></label>'
            f'<label class="w">{e(t("Notiz"))}<textarea name="note" rows="2">{e(o.get("note") or "")}</textarea></label>'
            f'<div class="w obtns"><button class="btn primary">{e(t("Speichern"))}</button></div></form>')

    # Kennzahlen
    price = o.get("agreed_price")
    price_sub = t("vereinbart") if price is not None else (t("geschätzt, mit Kundenmarge") if est["price"] is not None else t("noch keine Daten"))
    if price is None:
        price = est["price"]
    if st == "billed" and o.get("billed"):
        from .pages import amount_due
        price, price_sub = amount_due(o["billed"]), t("Endpreis")
    pct = est["done"] / est["qty"] * 100 if est["qty"] else 0
    rest = (f'~ {fmt_hours(est["rest_min"] / 60)}' if est["rest_min"] else ("–" if est["plates"] else "✓"))
    rest_sub = (tn(est["plates"], "{n} Platte", "{n} Platten") if est["plates"] else t("alles gedruckt"))
    if est["missing"] and est["plates"]:
        rest_sub += " · " + t("ohne Schätzung: {n}", n=est["missing"])
    all_sets = bool(o["items"]) and all(i.get("pl") for i in o["items"])
    kp = (f'<div class="okp"><div><small>{e(t("Sätze") if all_sets else t("Stück"))}</small><b>{est["done"]} / {est["qty"]}</b><em>{pct:.0f} %</em></div>'
          f'<div><small>{e(t("Druckzeit bisher"))}</small><b>{fmt_hours(est["hours"]) if est["hours"] else "–"}</b><em>{e(tn(est["n_jobs"], "{n} Druck", "{n} Drucke"))}</em></div>'
          f'<div><small>{e(t("Noch zu drucken"))}</small><b>{rest}</b><em>{e(rest_sub)}</em></div>'
          f'<div class="calc"><small>{e(t("Kosten bisher"))}</small><b>{fmt_num(est["spent"], 2, "€")}</b><em>{e(t("Druck, Zubehör, Arbeit"))}</em></div>'
          f'<div class="calc"><small>{e(t("Preis"))}</small><b class="acc">{fmt_num(price, 2, "€") if price is not None else "–"}</b><em>{e(price_sub)}</em></div></div>')

    # Positionen
    rows = ""
    for i in o["items"]:
        s = i.get("stats") or {}
        sub = []
        if i.get("pl"):
            sub.append(tn(len(i["pl"]["plates"]), "{n} Platte pro Satz", "{n} Platten pro Satz"))
            if i.get("model_fav"):
                sub.append("★ " + t("Favorit"))
        elif i["model_id"]:
            pp = s.get("per_plate")
            if pp:
                sub.append(tn(pp, "{n} Stk. pro Platte", "{n} Stk. pro Platte"))
            if s.get("min"):
                sub.append("~" + fmt_hours(s["min"] / 60))
            if i.get("model_fav"):
                sub.append("★ " + t("Favorit"))
        else:
            sub.append(t("Freitext, noch kein Modell"))
        run = bool(i["running"])
        state_txt = _live(i, names) if run else (f'<span class="muted">{e(t("fertig"))}</span>' if i["done"] >= i["qty"]
                                                  else f'<span class="muted">{e(tn(i["pl"]["n_open"], "noch {n} Platte", "noch {n} Platten"))}</span>' if i.get("pl")
                                                  else f'<span class="muted">{e(t("noch nicht gedruckt") if not i["done"] else t("{n} fehlen", n=i["qty"] - i["done"]))}</span>')
        model_link = (f'<a href="./?model={i["model_id"]}">{e(i["label"])}</a>' if i["model_id"] else e(i["label"]))
        rows += (f'<div class="oprow">{_img(i, "big")}<div class="nm"><b>{model_link}</b><small>{e(" · ".join(sub))}</small></div>'
                 f'<div class="pg">{_bar(i["pl"]["n_done"] if i.get("pl") else i["done"], (i["pl"]["n_print"] or 1) if i.get("pl") else i["qty"], run)}<small>{state_txt}</small></div>'
                 f'<b class="c">{i["done"]} / {i["qty"]}{"<small> " + e(t("Sätze")) + "</small>" if i.get("pl") else ""}</b>'
                 f'<form method="post" action="{act}" class="oqty"><input type="hidden" name="do" value="qty"><input type="hidden" name="item" value="{i["id"]}">'
                 f'<button name="d" value="-1" class="btn" title="{e(t("Stückzahl verringern"))}">−</button>'
                 f'<button name="d" value="1" class="btn" title="{e(t("Stückzahl erhöhen"))}">＋</button></form>'
                 f'<form method="post" action="{act}" class="odel" data-q="{e(t("Position entfernen? Zugeordnete Drucke bleiben erhalten."))}">'
                 f'<input type="hidden" name="do" value="del_item"><input type="hidden" name="item" value="{i["id"]}">'
                 f'<button class="btn" title="{e(t("Position entfernen"))}">✕</button></form></div>')
        rows += _plate_box(store, o, i, act)
    if not rows:
        rows = f'<p class="muted">{e(t("Noch keine Positionen. Füge ein Modell oder Freitext hinzu."))}</p>'
    add = (f'<form method="post" action="{act}" class="oaddf"><input type="hidden" name="do" value="add">'
           f'<input name="item" list="modellist" autocomplete="off" placeholder="{e(t("Modell oder Freitext …"))}" required>'
           f'<input name="qty" inputmode="numeric" value="1" class="num" aria-label="{e(t("Stück"))}">'
           f'<button class="btn primary">＋ {e(t("Position"))}</button></form>')
    items = f'<div class="obox"><h3>{e(t("Positionen"))}</h3>{rows}{add}</div>'

    # Zugeordnete Drucke
    jl = ""
    for i in o["items"]:
        for j in i["jobs"]:
            stj = (f'<span class="ost running">{e(t("läuft"))}</span>' if j["state"] != "done" else
                   f'<span class="ost bad">{e(t("Fehldruck"))}</span>' if j["is_scrap"] else f'<span class="ost done">{e(t("fertig"))}</span>')
            jl += (f'<div class="ojl"><span class="d">{fmt_date(j.get("start_ts"), "dm_hm")}</span>'
                   f'<span><a href="job?id={j["id"]}">{e(j.get("task_name") or t("Druck"))}</a> <small class="muted">{e(code_label(j.get("code")))}{" · " + e(i["label"]) if i["label"] != j.get("task_name") else ""}{" · " + e(t("Platte {n}", n=j["gcode_plate"])) if j.get("gcode_plate") else ""}</small></span>'
                   f'<span>{e(names.get(j["printer"], j.get("printer_name") or j["printer"]))}</span>'
                   f'<span class="n">{e(t("{n} Stk.", n=j.get("quantity") or 1))}</span>{stj}'
                   f'<form method="post" action="{act}"><input type="hidden" name="do" value="unlink"><input type="hidden" name="job" value="{j["id"]}">'
                   f'<button class="btn lnk" title="{e(t("Zuordnung lösen"))}">✕</button></form></div>')
    if not jl:
        jl = f'<p class="muted">{e(t("Noch keine Drucke zugeordnet."))}</p>'
    cand = ""
    if show_link and o["items"]:
        opts_items = [(i["id"], i["label"], i["model_id"]) for i in o["items"]]
        for c in candidates(store, o):
            pre = next((iid for iid, _l, mid in opts_items if mid and mid == c["model_id"]), opts_items[0][0])
            sel = "".join(f'<option value="{iid}"{" selected" if iid == pre else ""}>{e(lbl)}</option>' for iid, lbl, _m in opts_items)
            img = f'<img src="images/{e(c["image"])}" alt="" loading="lazy">' if c.get("image") else '<span class="oph"></span>'
            cand += (f'<form method="post" action="{act}" class="ocand"><input type="hidden" name="do" value="link"><input type="hidden" name="job" value="{c["id"]}">'
                     f'{img}<span><b>{e(c.get("task_name") or t("Druck"))}</b><small>{fmt_date(c.get("start_ts"), "dm_hm")} · '
                     f'{e(names.get(c["printer"], c.get("printer_name") or c["printer"]))} · {e(t("{n} Stk.", n=c.get("quantity") or 1))}'
                     f'{" · " + e(t("Fehldruck")) if c["is_scrap"] else ""}</small></span>'
                     f'<select name="item" aria-label="{e(t("Position"))}">{sel}</select><button class="btn">{e(t("Zuordnen"))}</button></form>')
        cand = (f'<div class="ocands"><h4>{e(t("Drucke zum Zuordnen"))}</h4>{cand or "<p class=muted>" + e(t("Keine passenden Drucke gefunden.")) + "</p>"}'
                f'<a href="order?id={oid}">{e(t("Fertig"))}</a></div>')
    link_btn = (f'<a class="btn" href="order?id={oid}&link=1#od">＋ {e(t("Druck zuordnen"))}</a>' if o["items"] and not show_link else "")
    jobs_box = f'<div class="obox" id="od"><h3>{e(t("Zugeordnete Drucke"))}<span class="sp"></span>{link_btn}</h3>{jl}{cand}</div>'

    # Abschluss
    if st in ("billed", "archived") and o.get("billed"):
        b = o["billed"]
        fin = (f'<div class="ofin ok"><span class="ic">🧾</span><div>{e(t("Abgerechnet über"))} '
               f'<a href="job?id={b["id"]}">{e(b.get("task_name") or "")} · {e(code_label(b.get("code")))}</a>. '
               f'{e(t("Zahlung und Übergabe trägst du dort ein."))}</div><span class="sp"></span>'
               f'<form method="post" action="{act}"><input type="hidden" name="do" value="reopen"><button class="btn">{e(t("Wieder öffnen"))}</button></form></div>')
    elif st == "archived":
        fin = (f'<div class="ofin"><span class="ic">✓</span><div>{e(t("Als erledigt markiert."))}</div><span class="sp"></span>'
               f'<form method="post" action="{act}"><input type="hidden" name="do" value="reopen"><button class="btn">{e(t("Wieder öffnen"))}</button></form></div>')
    else:
        ready = st == "done"
        txt = (t("Alles gedruckt. „Zur Abrechnung“ fasst die Drucke zum Projekt zusammen, danach Endpreis, Zahlung und Übergabe wie gewohnt.")
               if ready else t("Sind alle Positionen gedruckt, rückt der Auftrag in „Fertig · abrechnen“. Abrechnen geht auch vorher."))
        if o.get("agreed_price") is not None:
            txt += " " + t("Der vereinbarte Preis {p} wird Endpreis.", p=fmt_num(o["agreed_price"], 2, "€"))
        n_extra = surplus(o)
        extra = (f'<label class="ostock"><input type="checkbox" name="stock" value="1" checked> '
                 f'{e(t("{n} Stück mehr gedruckt – in den Vorrat", n=n_extra))}</label>'
                 if n_extra else "")
        fin = (f'<div class="ofin{" ok" if ready else ""}"><span class="ic">🧾</span><div>{e(txt)}</div><span class="sp"></span>'
               f'<form method="post" action="{act}"><input type="hidden" name="do" value="close"><button class="btn">{e(t("Ohne Abrechnung erledigt"))}</button></form>'
               f'<form method="post" action="{act}" class="obill"><input type="hidden" name="do" value="bill">{extra}'
               f'<button class="btn{" primary" if ready else ""}">💶 {e(t("Zur Abrechnung"))}</button></form></div>')
    note = f'<p class="onote">📝 {e(o["note"])}</p>' if o.get("note") else ""
    danger = (f'<form method="post" action="{act}" class="odel odanger" data-q="{e(t("Auftrag löschen? Die Drucke bleiben erhalten."))}">'
              f'<input type="hidden" name="do" value="delete"><button class="btn lnk">🗑 {e(t("Auftrag löschen"))}</button></form>')
    body = (f'{head}{edit}{note}{kp}{items}{jobs_box}{fin}{danger}{_custlist(store)}{_modellist(store)}')
    return layout(o.get("title") or t("Auftrag"), "orders", f'<style>{CSS}</style>{body}', ORDER_JS)



# ---- Platten pro Position (siehe app/plates.py) ------------------------------------------

def _swatches(colors: list[str]) -> str:
    return "".join(f'<i class="osw" style="background:{e(c)}" title="{e(c)}"></i>' for c in colors[:4])


def _plate_box(store: Store, o: dict[str, Any], i: dict[str, Any], act: str) -> str:
    """Unter einer Position: Tabelle Platte × Satz, oder der Knopf zum Einschalten."""
    hid = f'<input type="hidden" name="item" value="{i["id"]}">'
    sm = i.get("pl")
    if not sm:
        if i["model_id"] and _plates.has_plates(store, i["model_id"]):
            n = len(_plates.model_plate_numbers(store, i["model_id"]))
            return (f'<form method="post" action="{act}" class="opon"><input type="hidden" name="do" value="by_plate">{hid}'
                    f'<input type="hidden" name="on" value="1"><span class="muted">{e(tn(n, "Dieses Modell hat {n} Platte.", "Dieses Modell hat {n} Platten."))}</span>'
                    f'<button class="btn sm">▦ {e(t("Pro Platte verfolgen"))}</button></form>')
        if i["model_id"]:
            return ""
        return (f'<form method="post" action="{act}" class="opon"><input type="hidden" name="do" value="plates">{hid}'
                f'<input type="hidden" name="on" value="1"><span class="muted">{e(t("Mehrere Platten pro Satz?"))}</span>'
                f'<input name="n_plates" inputmode="numeric" class="num" placeholder="{e(t("Anzahl"))}" aria-label="{e(t("Anzahl Platten"))}" '
                f'value="{i.get("n_plates") or ""}"><button class="btn sm">▦ {e(t("Pro Platte verfolgen"))}</button></form>')
    plates, g, qty = sm["plates"], sm["grid"]["cells"], int(i["qty"])
    names = {}
    from .ui import printer_names
    names = printer_names(store)
    head = "".join(f'<th class="c">{e(t("Satz {n}", n=s))}</th>' for s in range(1, qty + 1))
    order = [p for p in plates if not p["grp"]]
    groups: dict[str, list[dict[str, Any]]] = {}
    for p in plates:
        if p["grp"]:
            groups.setdefault(p["grp"], []).append(p)
    body = ""

    def row(p: dict[str, Any]) -> str:
        meta = []
        if p["min"]:
            meta.append("~" + fmt_hours(p["min"] / 60))
        if p["g"]:
            meta.append(fmt_num(p["g"], 0, "g"))
        if (p["pieces"] or 1) > 1:
            meta.append(t("{n} Stk.", n=p["pieces"]))
        if not p["n"]:
            meta.append(t("noch nie gedruckt"))
        tds = ""
        need = done = stock = 0
        for s in range(1, qty + 1):
            c = g[(s, p["plate"])]
            st = c["state"]
            if st in ("done", "running"):
                j = c["job"]
                lab = (f'✓ {e(t("fertig"))}' if st == "done" else
                       f'⏳ {e(t("läuft"))} <small>{e(names.get(j["printer"], j.get("printer_name") or j["printer"]))}</small>')
                tds += f'<td class="c"><a class="pc {st}" href="job?id={j["id"]}" title="{e(fmt_date(j.get("start_ts"), "dm_hm"))}">{lab}</a></td>'
            else:
                lab = {"open": f'○ {e(t("offen"))}', "stock": f'📦 {e(t("aus Vorrat"))}',
                       "skip": f'— {e(t("nicht gewählt") if p["grp"] else t("nicht nötig"))}'}[st]
                tds += (f'<td class="c"><button class="pc {st}" name="cell" value="{s}:{p["plate"]}" '
                        f'title="{e(t("Tippen: drucken → aus Vorrat → nicht nötig"))}">{lab}</button></td>')
            need += st in ("done", "running", "open")
            done += st == "done"
            stock += st == "stock"
        right = f'{done} / {need}' if need else ""
        if stock:
            st_info = _plates.stock_for(store, i.get("model_id"), p)
            have = st_info["pieces"] // (p["pieces"] or 1)
            right += (" · " if right else "") + f'<span class="{"bad" if have < stock else ""}">📦 {e(t("{a} von {b} im Vorrat", a=stock, b=have))}</span>'
        part = ""
        if p.get("part_model_id"):
            pm = store._conn.execute("SELECT name FROM models WHERE id = ?", (p["part_model_id"],)).fetchone()
            if pm:
                part = f' <span class="ptag">🔗 {e(pm["name"])}</span>'
        return (f'<tr><td><div class="pl"><span class="pnr">{p["plate"]}</span><div><b>{e(p["name"])}</b>{part}'
                f'<small>{e(" · ".join(meta))} {_swatches(p["colors"])}</small></div></div></td>{tds}<td class="pr">{right}</td></tr>')

    for p in order:
        body += row(p)
    for gname, ps in groups.items():
        body += f'<tr class="pgrp"><td colspan="{qty + 2}">{e(t("Auswahl „{g}“ · eine pro Satz", g=gname))}</td></tr>'
        for p in ps:
            body += row(p)
    tot = [t("Sätze fertig {a} / {b}", a=sm["sets_done"], b=qty), t("Platten gedruckt {a} / {b}", a=sm["n_done"], b=sm["n_print"])]
    if sm["n_open"]:
        tot.append(t("noch zu drucken: {n}", n=tn(sm["n_open"], "{n} Platte", "{n} Platten")) +
                   (f' · ~{fmt_hours(sm["rest_min"] / 60)}' if sm["rest_min"] else ""))
    if sm["n_stock"]:
        tot.append(t("aus Vorrat: {n}", n=sm["n_stock"]))
    table = (f'<form method="post" action="{act}" class="ptab"><input type="hidden" name="do" value="cell">{hid}'
             f'<table class="pm"><tr><th>{e(t("Platte"))}</th>{head}<th>{e(t("je Platte"))}</th></tr>{body}</table></form>')
    # Platten bearbeiten
    grps = sorted({p["grp"] for p in plates if p["grp"]})
    glist = f'<datalist id="pg{i["id"]}">' + "".join(f'<option value="{e(x)}">' for x in grps) + "</datalist>"
    erows = ""
    for p in plates:
        erows += (f'<div class="perow"><span class="pnr">{p["plate"]}</span><input type="hidden" name="plate" value="{p["plate"]}">'
                  f'<input name="pname_{p["plate"]}" maxlength="80" value="{e(p["name"]) if p["named"] else ""}" placeholder="{e(t("Platte {n}", n=p["plate"]))}" aria-label="{e(t("Name"))}">'
                  f'<input name="pgrp_{p["plate"]}" maxlength="40" value="{e(p["grp"] or "")}" list="pg{i["id"]}" placeholder="{e(t("Auswahlgruppe (leer = immer)"))}" aria-label="{e(t("Auswahlgruppe"))}">'
                  f'{_part_input(store, i, p)}</div>')
    n_in = ""
    if not i["model_id"] or i.get("n_plates"):
        n_in = (f'<label class="pn">{e(t("Anzahl Platten"))} <input name="n_plates" inputmode="numeric" class="num" value="{i.get("n_plates") or len(plates)}"></label>')
    scope = t("Gilt für alle Aufträge mit diesem Modell.") if i["model_id"] else t("Gilt für diese Position.")
    edit = (f'<form method="post" action="{act}" class="pedit" id="pe{i["id"]}" hidden><input type="hidden" name="do" value="plates">{hid}'
            f'<p class="muted">{e(t("Platten mit gleicher Auswahlgruppe (z. B. „Design“) stehen zur Wahl: pro Satz wird eine davon gedruckt."))} {e(scope)}</p>'
            f'{erows}{glist}{n_in}<div class="obtns"><button class="btn primary">{e(t("Speichern"))}</button></div></form>'
            f'<form method="post" action="{act}" class="poff" id="po{i["id"]}" hidden><input type="hidden" name="do" value="by_plate">{hid}'
            f'<input type="hidden" name="on" value="0"><button class="btn lnk">{e(t("Nicht mehr pro Platte verfolgen"))}</button></form>')
    return (f'<div class="pbox" id="pb{i["id"]}"><div class="ptot">{" · ".join(e(x) for x in tot)}<span class="sp"></span>'
            f'<button type="button" class="btn sm" data-tgl="pe{i["id"]}" data-tgl2="po{i["id"]}">✏️ {e(t("Platten bearbeiten"))}</button></div>'
            f'{_stock_hint(store, i)}<div class="pscroll">{table}</div>{edit}</div>')


def _part_input(store: Store, item: dict[str, Any], p: dict[str, Any]) -> str:
    """Gemeinsames Teil: Die Platte entspricht einem eigenen Modell (z. B. „Kopf + Hände“), dessen Vorrat alle nutzen."""
    name = ""
    if p.get("part_model_id"):
        r = store._conn.execute("SELECT name FROM models WHERE id = ?", (p["part_model_id"],)).fetchone()
        name = r["name"] if r else ""
    return (f'<input name="ppart_{p["plate"]}" maxlength="120" value="{e(name)}" list="modellist" '
            f'placeholder="🔗 {e(t("Gemeinsames Teil (optional)"))}" aria-label="{e(t("Gemeinsames Teil"))}">')


def _stock_hint(store: Store, item: dict[str, Any]) -> str:
    """Vorschlag „aus Vorrat“ für Platten mit offenen Feldern, deren Teile schon im Vorrat liegen."""
    out = ""
    act = f"orders/{item['order_id']}"
    for sg in _plates.suggestions(store, item, item.get("pl")):
        hid = (f'<input type="hidden" name="do" value="use_stock"><input type="hidden" name="item" value="{item["id"]}">'
               f'<input type="hidden" name="plate" value="{sg["plate"]}">')
        per = sg["per"]
        good, all_ = sg["match"] // per, sg["pieces"] // per
        what = f'<b>{e(sg["name"])}</b>' + (f' <span class="ptag">🔗 {e(t("gemeinsam"))}</span>' if sg["shared"] else "")
        if sg["n_sets"]:
            txt = t("liegt {n}× im Vorrat.", n=good) + " " + tn(sg["n_sets"], "Für {n} Satz aus dem Vorrat nehmen?",
                                                                  "Für {n} Sätze aus dem Vorrat nehmen?")
            btn = f'<button class="btn sm">📦 {e(t("Ja, {n} nehmen", n=sg["n_sets"]))}</button>'
            other = (f' <span class="muted">{e(t("Dazu {n}× in anderer Farbe", n=all_ - good))} {_swatches(sg["other_colors"])}</span>'
                     if all_ > good else "")
            out += f'<form method="post" action="{act}" class="psug">{hid}💡 {what} {_swatches(sg["colors"])} {e(txt)}{other}{btn}</form>'
        elif all_:
            k = min(all_, sg["n_open"])
            txt = t("liegt nur in anderer Farbe im Vorrat ({n}×).", n=all_)
            btn = (f'<input type="hidden" name="n" value="{k}"><button class="btn sm">📦 '
                   f'{e(t("Trotzdem {n} nehmen", n=k))}</button>')
            out += f'<form method="post" action="{act}" class="psug">{hid}🎨 {what} {e(txt)} {_swatches(sg["other_colors"])}{btn}</form>'
    return out

ORDER_JS = r"""
document.querySelectorAll('[data-tgl]').forEach(b=>b.onclick=()=>{const el=document.getElementById(b.dataset.tgl);el.hidden=!el.hidden;
 const o=b.dataset.tgl2&&document.getElementById(b.dataset.tgl2);if(o)o.hidden=el.hidden});
document.querySelectorAll('form.odel').forEach(f=>f.addEventListener('submit',ev=>{if(!confirm(f.dataset.q))ev.preventDefault()}));
"""


# ---- Kundenseite: Karte „Aufträge“ ---------------------------------------------------

def customer_card(store: Store, cid: int) -> str:
    orders = all_orders(store, cid)
    live = [o for o in orders if o["state"] != "archived"]
    arch = sorted((o for o in orders if o["state"] == "archived"), key=lambda o: -(o.get("done_at") or o["created_at"]))[:2]
    rows = ""
    for o in live + arch:
        est = estimate(o)
        its = " · ".join(f'{i["label"]} {i["done"]}/{i["qty"]}' for i in o["items"][:3])
        run = any(i["running"] for i in o["items"])
        rows += (f'<a class="corow" href="order?id={o["id"]}"><span><b>{e(o.get("title") or t("Auftrag"))}</b><small>{e(its)}</small></span>'
                 f'{_bar(est["done"], est["qty"] or 1, run)}<span>{_due_chip(o)}</span>{_status_chip(o["state"])}</a>')
    if not rows:
        rows = f'<p class="muted" style="margin:0">{e(t("Noch keine Aufträge."))}</p>'
    more = (f'<a class="more" href="orders?c={cid}">{e(t("Alle {n} Aufträge →", n=len(orders)))}</a>' if len(orders) > len(live) + len(arch) else "")
    return (f'<div class="card"><h3 class="coh">📋 {e(t("Aufträge"))}<span class="sp"></span>'
            f'<a class="btn" href="order?new=1&c={cid}">＋ {e(t("Auftrag"))}</a></h3>{rows}{more}</div>')


def often_card(store: Store, cid: int) -> str:
    rows = ""
    for m in often(store, cid)[:6]:
        img = f'<img src="images/{e(m["image"])}" alt="" loading="lazy">' if m.get("image") else '<span class="oph">🧩</span>'
        rows += (f'<div class="cfv">{img}<span><b>{"<i class=ostar>★</i> " if m["fav"] else ""}<a href="./?model={m["id"]}">{e(m["name"])}</a></b>'
                 f'<small>{e(t("zuletzt {d}", d=fmt_date(m["last"], "date_s")))}'
                 f'{" · " + e(t("{n} Stk.", n=m["last_qty"])) if m.get("last_qty") else ""}</small></span>'
                 f'<span class="n">{m["n"]}×<small class="calc">{fmt_num(m["cost"], 2, "€")}</small></span>'
                 f'<button type="button" class="btn oagain" data-model="{m["id"]}" data-customer="{cid}">↻ {e(t("Nochmal"))}</button></div>')
    if not rows:
        return ""
    return (f'<div class="card"><h3>★ {e(t("Bestellt oft"))}</h3>{rows}'
            f'<p class="muted" style="font-size:12.5px;margin:8px 0 0">{e(t("Ab {n} Drucken für diesen Kunden oder als Favorit markiert (★ auf der Seite Modelle).", n=OFTEN))}</p></div>')


# ---- Druckseite: Chip ----------------------------------------------------------------

def job_chip(store: Store, job: dict[str, Any]) -> str:
    it = item_of_job(store, job)
    if not it:
        if job.get("order_state") == "ask":
            return f'<a class="bdg" href="hints" title="{e(t("Passt zu mehreren Aufträgen"))}">📋 {e(t("Auftrag?"))}</a>'
        return ""
    return (f'<a class="bdg" href="order?id={it["order_id"]}" title="{e(t("Gehört zu diesem Auftrag"))}">'
            f'📋 {e(it.get("title") or t("Auftrag"))}{" · " + e(t("automatisch")) if job.get("order_state") == "auto" else ""}</a>')


# ---- „Wieder drucken für …“ (Dialog auf Modelle- und Kundenseite) --------------------

def again_dialog(store: Store) -> str:
    """Dialog + Daten. Knöpfe mit ``class="oagain" data-model=… [data-customer=…]`` öffnen ihn."""
    mids = [r[0] for r in store._conn.execute("SELECT id FROM models")]
    stats = model_stats(store, mids)
    tops = top_customers(store)
    mrows = {r["id"]: dict(r) for r in store._conn.execute(
        "SELECT m.id, m.name, (SELECT image FROM jobs j WHERE j.model_id = m.id AND j.image IS NOT NULL AND j.kind IS NULL "
        "ORDER BY j.start_ts DESC LIMIT 1) image, (SELECT MAX(start_ts) FROM jobs j WHERE j.model_id = m.id AND j.kind IS NULL) last FROM models m")}
    last_qty: dict[str, int] = {}
    for r in store._conn.execute(
            "SELECT model_id, customer_id, quantity FROM jobs WHERE kind IS NULL AND model_id IS NOT NULL AND customer_id IS NOT NULL "
            "ORDER BY start_ts"):
        last_qty[f'{r["model_id"]}:{r["customer_id"]}'] = r["quantity"] or 1
    models = {}
    for mid, m in mrows.items():
        s = stats.get(mid) or {}
        top = tops.get(mid)
        models[mid] = {"name": m["name"], "img": m.get("image") or "", "pp": s.get("per_plate") or 1, "min": s.get("min"),
                       "g": s.get("g"), "c": s.get("piece_cost"), "w": s.get("piece_work") or 0,
                       "last": fmt_date(m["last"], "date_s") if m.get("last") else "", "top": top[0] if top else None}
    custs = {c["id"]: {"name": c["name"], "m": c.get("margin_pct") or 0, "wm": bool(c.get("work_margin"))} for c in store.customers()}
    opens = [{"id": o["id"], "c": o.get("customer_id"), "t": o.get("title") or t("Auftrag"),
              "s": ", ".join(f'{i["label"]} {i["done"]}/{i["qty"]}' for i in o["items"][:2])} for o in open_orders(store)]
    data = json.dumps({"models": models, "custs": custs, "orders": opens, "last": last_qty}, ensure_ascii=False).replace("</", "<\\/")
    return (f'<script type="application/json" id="againdata">{data}</script>'
            f'<dialog id="againdlg" class="odlg"><form method="post" action="orders/again">'
            f'<input type="hidden" name="model"><div class="hd"><img alt=""><span><b></b><small></small></span></div>'
            f'<label>{e(t("Für wen?"))}<input name="customer" list="custlist" autocomplete="off" placeholder="{e(t("Kunde (leer = Vorrat/eigen)"))}"></label>'
            f'<label>{e(t("Wie viele?"))}<span class="ostep"><button type="button" data-d="-1">−</button><input name="qty" inputmode="numeric" value="1">'
            f'<button type="button" data-d="1">＋</button></span><small class="oest"></small></label>'
            f'<label>{e(t("In welchen Auftrag?"))}<select name="into"><option value="">{e(t("Neuer Auftrag"))}</option></select></label>'
            f'<label class="duel">{e(t("Bis"))}<input type="date" name="due"></label>'
            f'<div class="obtns"><button type="button" class="btn" value="cancel" formmethod="dialog" data-close>{e(t("Abbrechen"))}</button>'
            f'<button class="btn primary">＋ {e(t("Zum Auftrag"))}</button></div></form></dialog>{_custlist(store)}')


AGAIN_JS = r"""
(()=>{const dlg=document.getElementById('againdlg');if(!dlg)return;let D={};try{D=JSON.parse(document.getElementById('againdata').textContent)}catch(_){}
  const f=dlg.querySelector('form'),cu=f.customer,q=f.qty,into=f.into,est=dlg.querySelector('.oest');let M=null,mid=null;
  const eur=v=>v.toLocaleString(document.documentElement.lang,{style:'currency',currency:'EUR'});
  const hm=m=>{const h=Math.floor(m/60),mm=Math.round(m%60);return h+':'+String(mm).padStart(2,'0')+' h'};
  const cid=()=>{const n=cu.value.trim().toLowerCase();for(const k in D.custs)if(D.custs[k].name.toLowerCase()===n)return +k;return null};
  function calc(){if(!M)return;const n=Math.max(1,parseInt(q.value)||1),pl=Math.ceil(n/M.pp),c=cid(),C=c?D.custs[c]:null;const bits=[];
    bits.push(pl+' '+(pl===1?_t("Platte"):_t("Platten")));if(M.min)bits.push('~'+hm(pl*M.min));if(M.g)bits.push('~'+Math.round(pl*M.g)+' g');
    if(M.c!=null){const pct=C?C.m:0,base=n*M.c,w=n*M.w;bits.push('~'+eur(C&&C.wm?(base+w)*(1+pct/100):base*(1+pct/100)+w))}
    est.textContent=bits.join(' · ');
    const keep=into.value;into.length=1;(D.orders||[]).filter(o=>!c||o.c===c).forEach(o=>{const op=document.createElement('option');op.value=o.id;
      op.textContent=o.t+(o.s?' ('+o.s+')':'');into.append(op)});into.value=[...into.options].some(o=>o.value===keep)?keep:'';
    dlg.querySelector('.duel').hidden=!!into.value}
  function open(b){mid=b.dataset.model;M=D.models[mid];if(!M)return;f.model.value=mid;
    const hd=dlg.querySelector('.hd'),im=hd.querySelector('img');im.hidden=!M.img;if(M.img)im.src='images/'+M.img;
    hd.querySelector('b').textContent='↻ '+M.name;
    hd.querySelector('small').textContent=_t("{n} Stk. pro Platte").replace('{n}',M.pp)+(M.last?' · '+_t("zuletzt {d}").replace('{d}',M.last):'');
    const c=b.dataset.customer||M.top;cu.value=c&&D.custs[c]?D.custs[c].name:'';
    q.value=(c&&D.last[mid+':'+c])||M.pp||1;into.value='';calc();dlg.showModal()}
  document.querySelectorAll('.oagain').forEach(b=>b.addEventListener('click',()=>open(b)));
  dlg.querySelectorAll('[data-d]').forEach(b=>b.onclick=()=>{q.value=Math.max(1,(parseInt(q.value)||1)+ +b.dataset.d*(M?M.pp:1));calc()});
  dlg.querySelector('[data-close]').onclick=()=>dlg.close();
  [cu,q,into].forEach(x=>x.addEventListener('input',calc));into.addEventListener('change',calc)})();
"""


CSS = """
.oph1{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:4px 0 2px}.oph1 h2{margin:0}.oph1 .sp,.odh .sp,.ofin .sp,.obox h3 .sp,.coh .sp{flex:1}
.ochips{display:flex;gap:6px;flex-wrap:wrap}.ochips a{border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:13.5px;font-weight:600;color:var(--muted);text-decoration:none;background:var(--card)}
.ochips a.on{background:color-mix(in srgb,var(--accent) 20%,var(--card));color:var(--fg);border-color:color-mix(in srgb,var(--accent) 55%,var(--line))}
.ointro{margin:4px 0 14px;font-size:14px}
.oboard{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;align-items:start}
.ocol{background:color-mix(in srgb,var(--card) 55%,var(--bg));border:1px solid var(--line);border-radius:16px;padding:10px}
.ocol>h3{margin:2px 4px 10px;font-size:13px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);display:flex;gap:8px;align-items:center}
.ocol>h3 i{font-style:normal;background:var(--line);color:var(--fg);border-radius:999px;padding:1px 8px;font-size:12px;letter-spacing:0}
.ocol>h3 .d{width:9px;height:9px;border-radius:50%}
.ocard{display:block;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:11px 13px;margin-bottom:9px;color:var(--fg);text-decoration:none}
.ocard:hover{border-color:color-mix(in srgb,var(--accent) 50%,var(--line))}
.ocard.running{box-shadow:inset 3px 0 0 var(--run)}.ocard.done{box-shadow:0 0 0 2px color-mix(in srgb,var(--ok) 55%,transparent)}
.ocard .oh{display:flex;gap:8px;align-items:baseline}.ocard .oh b{font-size:15px}.ocard .oh .sp{flex:1}
.ocard .ot{color:var(--muted);font-size:13px;margin:1px 0 6px}
.opos{display:grid;grid-template-columns:30px minmax(0,1fr) auto;gap:8px;align-items:center;padding:4px 0}
.opos img,.opos .oph{width:30px;height:30px;border-radius:7px;object-fit:cover;background:var(--bg)}
.oph{display:grid;place-items:center;font-size:15px;border:1px dashed var(--line);box-sizing:border-box}
.opos .n{min-width:0;font-size:13.5px}.opos s{text-decoration:none;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.opos .c{font-variant-numeric:tabular-nums;font-size:13px;padding-left:6px}
.obar{display:block;height:6px;border-radius:4px;background:var(--line);overflow:hidden;margin-top:3px}.obar i{display:block;height:100%;background:var(--accent)}
.obar i.run{background:var(--run)}.obar i.ok{background:var(--ok)}
.ofoot{margin-top:7px;font-size:12.5px;color:var(--muted)}
.olive{color:var(--run);font-weight:700}.olive:before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--run);margin-right:6px;
 box-shadow:0 0 0 3px color-mix(in srgb,var(--run) 30%,transparent)}
.odue{font-size:12px;font-weight:700;border-radius:999px;padding:2px 8px;background:var(--line);color:var(--fg);white-space:nowrap}
.odue.soon{background:color-mix(in srgb,var(--warn) 22%,var(--card));color:var(--warn)}.odue.late{background:color-mix(in srgb,var(--bad) 20%,var(--card));color:var(--bad)}
.ost{font-size:12px;font-weight:700;border-radius:999px;padding:2px 9px;white-space:nowrap;background:var(--line);color:var(--fg)}
.ost.running{background:color-mix(in srgb,var(--run) 20%,var(--card));color:var(--run)}
.obill{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.ostock{font-size:13px;display:flex;gap:6px;align-items:center}
.ost.done{background:color-mix(in srgb,var(--ok) 18%,var(--card));color:var(--ok)}.ost.billed{background:color-mix(in srgb,#b07cf0 20%,var(--card));color:#b07cf0}
.ost.archived{color:var(--muted)}.ost.bad{background:color-mix(in srgb,var(--bad) 18%,var(--card));color:var(--bad)}
.oadd{display:block;border:1px dashed var(--line);border-radius:12px;padding:9px;text-align:center;color:var(--muted);font-weight:600;font-size:14px;text-decoration:none}
.oarchl{display:block;margin:6px 4px 2px;font-size:13px}.oempty{margin:4px 4px 8px;font-size:13.5px}
.oseg{display:none}
.oarch{display:flex;flex-direction:column;gap:6px}.oarow{display:grid;grid-template-columns:80px minmax(0,1fr) auto;gap:12px;align-items:center;background:var(--card);
 border:1px solid var(--line);border-radius:12px;padding:8px 12px;color:var(--fg);text-decoration:none}.oarow small{display:block;color:var(--muted)}.oarow .d{color:var(--muted);font-size:13px}
.odh{display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap;margin:6px 0 12px}.odh h2{margin:0;font-size:26px}
.odh .meta{color:var(--muted);font-size:14px;margin-top:4px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.osaved{color:var(--ok);margin:0 0 10px}.onote{margin:0 0 12px;color:var(--muted)}
.okp{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:14px}
.okp>div{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:9px 13px}.okp small{color:var(--muted);font-size:12.5px;display:block}
.okp b{font-size:20px;display:block;font-variant-numeric:tabular-nums}.okp b.acc{color:var(--accent)}.okp em{font-style:normal;color:var(--muted);font-size:12.5px}
.obox{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 16px;margin-bottom:14px}
.obox>h3{margin:0 0 8px;font-size:16px;display:flex;gap:10px;align-items:center}
.oprow{display:grid;grid-template-columns:52px minmax(0,1.5fr) minmax(0,1.3fr) 76px auto auto;gap:12px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}
.oprow:first-of-type{border-top:0}
.oprow .big{width:52px;height:52px;border-radius:10px;object-fit:cover;background:var(--bg);font-size:22px}
.oprow .nm b{display:block;font-size:15.5px}.oprow .nm a{color:var(--fg);text-decoration:none}.oprow small{display:block;color:var(--muted);font-size:12.5px;margin-top:3px}
.oprow .obar{height:8px}.oprow .c{font-variant-numeric:tabular-nums;text-align:right;font-size:15px}
.oqty,.odel{display:flex;gap:4px;margin:0}.oqty .btn,.odel .btn{min-height:32px;padding:0 10px}
.oaddf{display:grid;grid-template-columns:minmax(0,1fr) 70px auto;gap:8px;margin-top:10px}.oaddf .num{text-align:center}
.ojl{display:grid;grid-template-columns:92px minmax(0,1fr) 110px 70px 80px 34px;gap:10px;padding:7px 0;border-top:1px solid var(--line);font-size:14px;align-items:center}
.ojl:first-of-type{border-top:0}.ojl .n{text-align:right;font-variant-numeric:tabular-nums}.ojl .d{color:var(--muted)}.ojl form{margin:0}
.ojl .ost{justify-self:end}.btn.lnk{border:0;background:none;color:var(--muted);min-height:0;padding:4px 6px}
.ocands{margin-top:10px;border-top:1px dashed var(--line);padding-top:10px}.ocands h4{margin:0 0 6px;font-size:14px}
.ocand{display:grid;grid-template-columns:38px minmax(0,1fr) minmax(0,200px) auto;gap:10px;align-items:center;padding:5px 0;margin:0}
.ocand img,.ocand .oph{width:38px;height:38px;border-radius:8px;object-fit:cover}.ocand small{display:block;color:var(--muted);font-size:12.5px}
.ofin{display:flex;gap:12px;align-items:center;flex-wrap:wrap;border:1px dashed var(--line);border-radius:14px;padding:12px 16px;font-size:14px;margin-bottom:14px}
.ofin.ok{border-color:color-mix(in srgb,var(--ok) 55%,var(--line))}.ofin .ic{font-size:22px}.ofin>div{flex:1 1 320px}.ofin form{margin:0}
.odanger{justify-content:flex-end}
.oform{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 16px;margin:8px 0 14px;padding:14px 16px}
.oform[hidden]{display:none}.oform label{display:flex;flex-direction:column;gap:5px;font-size:13px;color:var(--muted);font-weight:600}.oform .w{grid-column:1/-1}
.oform input,.oform textarea{font:inherit;color:var(--fg)}.orow2{display:grid;grid-template-columns:minmax(0,1fr) 80px;gap:8px}.orow2 .num{text-align:center}
.orow2 input,.oaddf input{min-width:0;width:100%;box-sizing:border-box}
.obtns{display:flex;gap:8px;justify-content:flex-end}
.coh{display:flex;align-items:center;gap:8px}.coh .btn{min-height:32px;font-size:13px;padding:0 10px}
.corow{display:grid;grid-template-columns:minmax(0,1fr) 120px auto auto;gap:10px;align-items:center;padding:8px 0;border-top:1px solid var(--line);color:var(--fg);text-decoration:none;font-size:14px}
.corow:first-of-type{border-top:0}.corow small{display:block;color:var(--muted);font-size:12.5px}.corow .obar{margin:0}
.cfv{display:grid;grid-template-columns:38px minmax(0,1fr) auto auto;gap:10px;align-items:center;padding:6px 0;border-top:1px solid var(--line)}
.cfv:first-of-type{border-top:0}.cfv img,.cfv .oph{width:38px;height:38px;border-radius:8px;object-fit:cover}.cfv b a{color:var(--fg);text-decoration:none}
.cfv small{display:block;color:var(--muted);font-size:12.5px}.cfv .n{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.cfv .btn{min-height:32px;padding:0 10px;font-size:13px;white-space:nowrap}.ostar{font-style:normal;color:#e3b505}
.odlg{border:1px solid var(--line);border-radius:18px;background:var(--card);color:var(--fg);padding:16px 18px;width:min(520px,94vw);box-shadow:0 24px 70px rgba(0,0,0,.45)}
.odlg::backdrop{background:rgba(0,0,0,.55)}.odlg form{display:flex;flex-direction:column;gap:12px;margin:0}
.odlg .hd{display:flex;gap:12px;align-items:center}.odlg .hd img{width:46px;height:46px;border-radius:10px;object-fit:cover}.odlg .hd b{display:block;font-size:17px}
.odlg .hd small{color:var(--muted);font-size:13px}
.odlg label{display:flex;flex-direction:column;gap:5px;font-size:12.5px;color:var(--muted);font-weight:700}.odlg label[hidden]{display:none}
.ostep{display:flex;align-items:center;gap:0;border:1px solid var(--line);border-radius:10px;overflow:hidden;width:max-content;background:var(--bg)}
.ostep button{width:42px;height:40px;border:0;background:none;color:var(--fg);font-size:20px;cursor:pointer}.ostep input{width:64px;border:0;text-align:center;font-weight:700;font-size:17px;background:none;color:var(--fg)}
.oest{font-weight:500;color:var(--muted);font-size:13px}
@media (max-width:1000px){.okp{grid-template-columns:repeat(3,minmax(0,1fr))}.oprow{grid-template-columns:48px minmax(0,1fr) auto auto;row-gap:8px}
 .oprow .big{grid-row:1/3}.oprow .nm{grid-column:2;grid-row:1}.oprow .c{grid-column:3;grid-row:1}.oprow .odel{grid-column:4;grid-row:1}
 .oprow .pg{grid-column:2;grid-row:2}.oprow .oqty{grid-column:3/5;grid-row:2;justify-content:flex-end}
 .ojl{grid-template-columns:84px minmax(0,1fr) 64px 30px}.ojl>span:nth-child(3),.ojl .ost{display:none}}
@media (max-width:700px){.oseg{display:flex;margin:0 0 10px;border:1px solid var(--line);border-radius:12px;overflow:hidden}
 .oseg button{flex:1;border:0;background:none;color:var(--muted);font:inherit;font-weight:600;padding:9px 4px;font-size:14px}
 .oseg button.on{background:color-mix(in srgb,var(--accent) 22%,var(--card));color:var(--fg)}
 .oboard{grid-template-columns:1fr}.ocol{display:none;padding:6px}.ocol.on{display:block}.ocol>h3{display:none}
 .okp{grid-template-columns:repeat(2,minmax(0,1fr))}.oform{grid-template-columns:1fr}.ocand{grid-template-columns:38px minmax(0,1fr) auto}.ocand select{grid-column:2}
 .corow{grid-template-columns:minmax(0,1fr) auto}.corow .obar,.corow>span:nth-child(3){display:none}}

.pbox{margin:-4px 0 12px 62px;border:1px solid var(--line);border-radius:12px;padding:10px 12px;background:color-mix(in srgb,var(--fg) 2%,var(--card))}
.ptot{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px;margin-bottom:6px}
.pscroll{overflow-x:auto}.pm{width:100%;border-collapse:separate;border-spacing:0 5px;font-size:13.5px}
.pm th{font-size:11.5px;color:var(--muted);font-weight:600;text-align:left;padding:0 6px;text-transform:uppercase;letter-spacing:.03em;white-space:nowrap}
.pm th.c,.pm td.c{text-align:center}.pm td{padding:6px;background:color-mix(in srgb,var(--fg) 4%,transparent)}
.pm td:first-child{border-radius:9px 0 0 9px;min-width:180px}.pm td:last-child{border-radius:0 9px 9px 0}
.pm td.pr{white-space:nowrap;font-variant-numeric:tabular-nums;font-size:13px}.pm td.pr .bad{color:var(--bad)}
.pl{display:flex;gap:9px;align-items:center}.pl small{display:block;color:var(--muted);font-size:12px}
.pnr{display:inline-grid;place-items:center;min-width:26px;height:26px;border-radius:7px;background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent);font-weight:700;font-size:12.5px}
.ptag{font-size:11.5px;border:1px solid var(--line);border-radius:999px;padding:1px 7px;margin-left:4px;white-space:nowrap}
.osw{display:inline-block;width:10px;height:10px;border-radius:50%;border:1px solid var(--line);vertical-align:-1px;margin-left:2px}
.pgrp td{background:none!important;padding:8px 6px 0;font-size:12px;color:var(--muted);font-weight:600}
.pc{display:inline-flex;gap:4px;align-items:center;padding:4px 10px;border-radius:999px;font-size:12.5px;font-weight:600;border:1px solid transparent;
white-space:nowrap;background:none;color:var(--fg);cursor:pointer;font-family:inherit;text-decoration:none}
.pc small{font-weight:500;opacity:.8}.pc.done{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok)}
.pc.running{background:color-mix(in srgb,var(--run) 18%,transparent);color:var(--run)}.pc.open{border-color:var(--line)}
.pc.stock{background:color-mix(in srgb,#b07cf0 20%,transparent);color:#b07cf0}.pc.skip{color:var(--muted);opacity:.6}
button.pc:hover{border-color:var(--accent)}
.opon{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:-4px 0 12px 62px;font-size:13px}.opon .num{width:70px}
.pedit{margin-top:8px;display:grid;gap:6px}.pedit[hidden],.poff[hidden]{display:none}.perow{display:flex;gap:8px;align-items:center}.perow input{flex:1}
.perow input{min-width:0;width:100%}.pedit .pn{display:flex;gap:8px;align-items:center;font-size:13px}.pedit .pn .num{width:70px}.poff{margin-top:4px}
.psug{display:flex;gap:8px;align-items:center;flex-wrap:wrap;border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin:4px 0 6px;font-size:13px}
@media (max-width:700px){.pbox,.opon{margin-left:0}.pm td:first-child{min-width:130px}.perow{grid-template-columns:26px minmax(0,1fr)}.perow input{grid-column:2}}
"""
