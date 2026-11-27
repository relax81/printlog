"""Platten einer Auftragsposition: ein Satz besteht aus mehreren Druckplatten.

Ein Modell (z. B. ein MakerWorld-Projekt) kann mehrere Platten haben. printlog lernt sie aus der Plattennummer der
Druckdatei (``jobs.gcode_plate``), bei Freitext-Positionen trägst du die Anzahl ein. Eine Position „pro Platte“ zählt
Sätze statt Stück: Die Tabelle Platte × Satz zeigt für jedes Feld *fertig*, *läuft*, *offen*, *aus Vorrat* oder
*nicht nötig*.

- **Auswahlgruppen** (``plate_info.grp``, z. B. „Design“): pro Satz wird nur eine Platte der Gruppe gedruckt,
  standardmäßig die erste; ein Tipp auf eine andere wählt sie für diesen Satz.
- **Aus Vorrat**: Das Feld muss nicht gedruckt werden. Bei „Zur Abrechnung“ bucht printlog die Teile aus dem Vorrat
  (aufgeteilte Drucke desselben Modells und derselben Platte bzw. desselben gemeinsamen Teils) für den Kunden aus.
- **Gemeinsame Teile** (``plate_info.part_model_id``): Eine Platte entspricht einem eigenen Modell (z. B. „Kopf +
  Hände“, das mehrere Figuren nutzen). Ihr Vorrat wird über alle Modelle geteilt, die diese Platte haben.

Abweichungen vom Standard stehen in ``plate_cells`` (pro Position, Satz und Platte).
"""

from __future__ import annotations

import json
import time
from typing import Any

from .db import Store
from .i18n import N_, t

STATES = ("print", "stock", "skip")
CELL_LABELS = {"done": N_("fertig"), "running": N_("läuft"), "open": N_("offen"), "stock": N_("aus Vorrat"),
               "skip": N_("nicht nötig"), "unpicked": N_("nicht gewählt")}
STATS_N = 6
MAX_PLATES = 30


# ---- Platten eines Modells -------------------------------------------------------------

def _colors(materials: Any) -> list[str]:
    """Farben eines Drucks (#RRGGBB, ohne Doppelte) aus ``jobs.materials``."""
    try:
        mats = json.loads(materials) if isinstance(materials, str) else (materials or [])
    except (ValueError, TypeError):
        return []
    out: list[str] = []
    for m in mats if isinstance(mats, list) else []:
        c = str((m or {}).get("color") or "").strip().lstrip("#").upper()[:6]
        if len(c) == 6 and all(ch in "0123456789ABCDEF" for ch in c) and f"#{c}" not in out:
            out.append(f"#{c}")
    return out


def plate_stats(store: Store, model_id: int) -> dict[int, dict[str, Any]]:
    """Je Plattennummer eines Modells aus den letzten guten Drucken: Minuten, Gramm, Stück, Kosten, Farben."""
    rows = [dict(r) for r in store._conn.execute(
        f"SELECT gcode_plate, quantity, start_ts, end_ts, filament_g, weight_g, cost_total, cost_work, materials FROM jobs "
        f"WHERE model_id = ? AND kind IS NULL AND gcode_plate IS NOT NULL AND state = 'done' AND NOT {Store.SCRAP_SQL} "
        "ORDER BY start_ts DESC", (model_id,))]
    by: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        lst = by.setdefault(int(r["gcode_plate"]), [])
        if len(lst) < STATS_N:
            lst.append(r)
    out: dict[int, dict[str, Any]] = {}
    for p, rs in by.items():
        mins = [(r["end_ts"] - r["start_ts"]) / 60 for r in rs if r["start_ts"] and r["end_ts"] and r["end_ts"] > r["start_ts"]]
        grams = [g for g in ((r["filament_g"] if r["filament_g"] is not None else r["weight_g"]) for r in rs) if g]
        cost = [r["cost_total"] for r in rs if r["cost_total"] is not None]
        work = [r["cost_work"] or 0 for r in rs if r["cost_total"] is not None]
        out[p] = {"min": sum(mins) / len(mins) if mins else None, "g": sum(grams) / len(grams) if grams else None,
                  "pieces": max(1, int(rs[0]["quantity"] or 1)), "cost": sum(cost) / len(cost) if cost else None,
                  "work": sum(work) / len(work) if work else 0.0, "colors": _colors(rs[0].get("materials")), "n": len(rs)}
    return out


def _info(store: Store, model_id: int | None, item_id: int | None) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if model_id:
        for r in store._conn.execute("SELECT * FROM plate_info WHERE model_id = ? ORDER BY id", (model_id,)):
            out[int(r["plate"])] = dict(r)
    if item_id:
        for r in store._conn.execute("SELECT * FROM plate_info WHERE item_id = ? ORDER BY id", (item_id,)):
            base = out.get(int(r["plate"]), {})
            out[int(r["plate"])] = {**base, **{k: v for k, v in dict(r).items() if v is not None}}
    return out


def model_plate_numbers(store: Store, model_id: int) -> list[int]:
    """Bekannte Plattennummern eines Modells (aus Drucken und Einträgen)."""
    nums = {int(r[0]) for r in store._conn.execute(
        "SELECT DISTINCT gcode_plate FROM jobs WHERE model_id = ? AND kind IS NULL AND gcode_plate IS NOT NULL", (model_id,))}
    nums |= {int(r[0]) for r in store._conn.execute("SELECT plate FROM plate_info WHERE model_id = ?", (model_id,))}
    return sorted(nums)


def plates_of(store: Store, item: dict[str, Any]) -> list[dict[str, Any]]:
    """Platten einer Position: [{plate, name, grp, part_model_id, min, g, pieces, cost, work, colors, n}] nach Nummer."""
    mid, iid = item.get("model_id"), item.get("id")
    stats = plate_stats(store, mid) if mid else {}
    info = _info(store, mid, iid)
    nums = set(stats) | set(info)
    if mid:
        nums |= set(model_plate_numbers(store, mid))
    if item.get("n_plates"):
        nums |= set(range(1, int(item["n_plates"]) + 1))
    out = []
    part_stats: dict[int, dict[str, Any]] = {}
    for p in sorted(n for n in nums if 0 < n <= 999)[:MAX_PLATES]:
        i, s = info.get(p, {}), stats.get(p, {})
        if not s and i.get("part_model_id"):  # gemeinsames Teil, an diesem Modell noch nie gedruckt: Werte vom Teil
            pid = i["part_model_id"]
            if pid not in part_stats:
                ps = plate_stats(store, pid)
                ps0 = ps[min(ps)] if ps else {}
                n = ps0.get("pieces") or 1  # Werte pro Stück: ein Satz braucht ein Teil
                part_stats[pid] = {**ps0, "pieces": 1, "min": ps0["min"] / n if ps0.get("min") else None,
                                   "g": ps0["g"] / n if ps0.get("g") else None,
                                   "cost": ps0["cost"] / n if ps0.get("cost") is not None else None,
                                   "work": (ps0.get("work") or 0) / n} if ps0 else {}
            s = {**part_stats[pid], "n": 0}
        out.append({"plate": p, "name": i.get("name") or t("Platte {n}", n=p), "named": bool(i.get("name")),
                    "grp": (i.get("grp") or "").strip() or None, "part_model_id": i.get("part_model_id"),
                    "min": s.get("min"), "g": s.get("g"), "pieces": s.get("pieces") or 1, "cost": s.get("cost"),
                    "work": s.get("work") or 0.0, "colors": s.get("colors") or [], "n": s.get("n") or 0})
    return out


def has_plates(store: Store, model_id: int | None) -> bool:
    return bool(model_id) and len(model_plate_numbers(store, model_id)) >= 2


# ---- Felder: Platte × Satz -------------------------------------------------------------

def cells(store: Store, item: dict[str, Any], plates: list[dict[str, Any]]) -> dict[tuple[int, int], str]:
    """Soll-Zustand je (Satz, Platte): print | stock | skip."""
    first: dict[str, int] = {}
    for p in plates:
        if p["grp"]:
            first.setdefault(p["grp"], p["plate"])
    out: dict[tuple[int, int], str] = {}
    for s in range(1, int(item["qty"]) + 1):
        for p in plates:
            out[(s, p["plate"])] = "print" if not p["grp"] or first[p["grp"]] == p["plate"] else "skip"
    for r in store._conn.execute("SELECT set_no, plate, state FROM plate_cells WHERE item_id = ?", (item["id"],)):
        if (r["set_no"], r["plate"]) in out and r["state"] in STATES:
            out[(r["set_no"], r["plate"])] = r["state"]
    return out


def grid(store: Store, item: dict[str, Any], plates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Fortschritt pro Feld aus den zugeordneten Drucken.

    Drucke zählen bei ihrer Plattennummer, der Reihe nach für die Sätze, die diese Platte drucken. Drucke ohne
    (passende) Plattennummer füllen die ersten offenen Felder. Fehldrucke zählen nicht.
    """
    plates = plates_of(store, item) if plates is None else plates
    want = cells(store, item, plates)
    nums = [p["plate"] for p in plates]
    jobs = [j for j in item.get("jobs") or [] if not j.get("is_scrap")]
    done = {p: [j for j in jobs if j["state"] == "done" and j.get("gcode_plate") == p] for p in nums}
    run = {p: [j for j in jobs if j["state"] != "done" and j.get("gcode_plate") == p] for p in nums}
    loose = [j for j in jobs if j.get("gcode_plate") not in nums]
    loose.sort(key=lambda j: (j["state"] != "done", j.get("start_ts") or 0))
    qty = int(item["qty"])
    g: dict[tuple[int, int], dict[str, Any]] = {}
    extra: dict[int, int] = {}
    for p in nums:
        need = [s for s in range(1, qty + 1) if want[(s, p)] == "print"]
        d, r = done[p], run[p]
        for k, s in enumerate(need):
            if k < len(d):
                g[(s, p)] = {"state": "done", "job": d[k]}
            elif k < len(d) + len(r):
                g[(s, p)] = {"state": "running", "job": r[k - len(d)]}
            else:
                g[(s, p)] = {"state": "open"}
        extra[p] = max(0, len(d) - len(need))
        for s in range(1, qty + 1):
            if want[(s, p)] != "print":
                g[(s, p)] = {"state": want[(s, p)]}
    for j in loose:  # ohne Plattennummer: erstes offenes Feld
        cell = next(((s, p) for p in nums for s in range(1, qty + 1) if g[(s, p)]["state"] == "open"), None)
        if not cell:
            break
        g[cell] = {"state": "done" if j["state"] == "done" else "running", "job": j, "loose": True}
    sets_done = sum(1 for s in range(1, qty + 1) if all(g[(s, p)]["state"] in ("done", "stock", "skip") for p in nums))
    open_cells = [(s, p) for (s, p), c in g.items() if c["state"] == "open"]
    return {"cells": g, "want": want, "sets_done": sets_done, "open": open_cells, "extra": extra,
            "running": [c["job"] for c in g.values() if c["state"] == "running"]}


def summary(store: Store, item: dict[str, Any]) -> dict[str, Any] | None:
    """Für Positionen pro Platte: Platten, Felder, Sätze fertig, Restzeit und Preis-Basis. Sonst None."""
    if not item.get("by_plate"):
        return None
    plates = plates_of(store, item)
    if not plates:
        return None
    gr = grid(store, item, plates)
    by_nr = {p["plate"]: p for p in plates}
    rest_min, missing = 0.0, 0
    for _s, p in gr["open"]:
        m = by_nr[p]["min"] or item.get("est_min")
        if m:
            rest_min += m
        else:
            missing += 1
    printed = [c for c in gr["cells"].values() if c["state"] in ("done", "running", "open")]
    cost = work = 0.0
    cost_missing = 0
    for (s, p), c in gr["cells"].items():
        if c["state"] in ("done", "running", "open"):
            pl = by_nr[p]
            if pl["cost"] is not None:
                cost += pl["cost"]
                work += pl["work"]
            else:
                cost_missing += 1
    return {"plates": plates, "grid": gr, "sets_done": gr["sets_done"], "n_print": len(printed),
            "n_done": sum(1 for c in printed if c["state"] == "done"), "n_open": len(gr["open"]),
            "n_stock": sum(1 for c in gr["cells"].values() if c["state"] == "stock"),
            "rest_min": rest_min, "missing": missing, "cost": cost, "work": work, "cost_missing": cost_missing}


def needs_plate(store: Store, item: dict[str, Any], plate: int | None) -> bool:
    """Braucht die Position noch einen Druck dieser Platte (offenes Feld)? Ohne Nummer: irgendein offenes Feld."""
    sm = summary(store, item)
    if not sm:
        return True
    nums = {p["plate"] for p in sm["plates"]}
    if plate is None or plate not in nums:
        return bool(sm["grid"]["open"])
    return any(p == plate for _s, p in sm["grid"]["open"])


# ---- Ändern ---------------------------------------------------------------------------

def set_cell(store: Store, item: dict[str, Any], set_no: int, plate: int, state: str | None = None) -> None:
    """Feld setzen (print, stock, skip); ohne state wird weitergeschaltet. In einer Auswahlgruppe wählt „print“ die
    Platte für den Satz (die anderen der Gruppe werden „nicht gewählt“)."""
    plates = plates_of(store, item)
    p = next((x for x in plates if x["plate"] == plate), None)
    if not p or not 1 <= set_no <= int(item["qty"]):
        return
    cur = cells(store, item, plates)[(set_no, plate)]
    if state not in STATES:
        state = {"print": "stock", "stock": "skip", "skip": "print"}[cur]
    rows = [(set_no, plate, state)]
    if p["grp"] and state == "print":
        busy = grid(store, item, plates)["cells"]
        if any(busy[(set_no, o["plate"])]["state"] in ("done", "running") for o in plates
               if o["grp"] == p["grp"] and o["plate"] != plate):
            return  # für diesen Satz ist schon eine andere Platte der Auswahl gedruckt
        rows += [(set_no, o["plate"], "skip") for o in plates if o["grp"] == p["grp"] and o["plate"] != plate]
    with store._lock, store._conn:
        for s, pl, st in rows:
            store._conn.execute("INSERT OR REPLACE INTO plate_cells (item_id, set_no, plate, state) VALUES (?, ?, ?, ?)",
                                (item["id"], s, pl, st))


def save_plates(store: Store, item: dict[str, Any], rows: list[dict[str, Any]], n_plates: int | None = None) -> None:
    """Namen, Auswahlgruppen und gemeinsame Teile speichern. Bei einem Modell gelten sie für alle Aufträge mit
    diesem Modell, bei Freitext nur für die Position."""
    mid = item.get("model_id")
    with store._lock, store._conn:
        if n_plates is not None:
            store._conn.execute("UPDATE order_items SET n_plates = ? WHERE id = ?",
                                (max(1, min(int(n_plates), MAX_PLATES)) if n_plates else None, item["id"]))
        for r in rows:
            p = int(r["plate"])
            name = (r.get("name") or "").strip()[:80] or None
            grp = (r.get("grp") or "").strip()[:40] or None
            part = r.get("part_model_id")
            key, val = ("model_id", mid) if mid else ("item_id", item["id"])
            old = store._conn.execute(f"SELECT id FROM plate_info WHERE {key} = ? AND plate = ?", (val, p)).fetchone()
            if old:
                store._conn.execute("UPDATE plate_info SET name = ?, grp = ?, part_model_id = ? WHERE id = ?", (name, grp, part, old["id"]))
            elif name or grp or part:
                store._conn.execute(f"INSERT INTO plate_info ({key}, plate, name, grp, part_model_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                    (val, p, name, grp, part, time.time()))


def adopt(store: Store, item_id: int, model_id: int) -> None:
    """Freitext-Position bekommt ein Modell: ihre Platten-Einträge gehen ans Modell, wenn es dort noch keine gibt."""
    with store._lock, store._conn:
        store._conn.execute("UPDATE plate_info SET model_id = ?, item_id = NULL WHERE item_id = ? AND plate NOT IN "
                            "(SELECT plate FROM plate_info WHERE model_id = ?)", (model_id, item_id, model_id))
        store._conn.execute("DELETE FROM plate_info WHERE item_id = ?", (item_id,))


def forget_item(store: Store, item_id: int) -> None:
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM plate_cells WHERE item_id = ?", (item_id,))
        store._conn.execute("DELETE FROM plate_info WHERE item_id = ?", (item_id,))


# ---- Vorrat ---------------------------------------------------------------------------

def _sources(store: Store, model_id: int | None, plate: dict[str, Any]) -> list[tuple[str, Any]]:
    """Woher Teile dieser Platte kommen dürfen: dasselbe Modell mit gleicher Platte, und bei einem gemeinsamen Teil
    dessen Modell sowie alle Platten anderer Modelle, die dasselbe Teil sind."""
    src: list[tuple[str, Any]] = []
    if model_id:
        src.append(("mp", (model_id, plate["plate"])))
    part = plate.get("part_model_id")
    if part:
        src.append(("m", part))
        for r in store._conn.execute("SELECT model_id, plate FROM plate_info WHERE part_model_id = ? AND model_id IS NOT NULL", (part,)):
            if (r["model_id"], r["plate"]) != (model_id, plate["plate"]):
                src.append(("mp", (r["model_id"], r["plate"])))
    return src


def part_colors(store: Store, part_model_id: int | None) -> list[str]:
    """Farben des letzten guten Drucks eines gemeinsamen Teils (für den Farbabgleich im Vorrat)."""
    if not part_model_id:
        return []
    r = store._conn.execute(f"SELECT materials FROM jobs WHERE model_id = ? AND kind IS NULL AND state = 'done' AND NOT {Store.SCRAP_SQL} "
                            "ORDER BY start_ts DESC LIMIT 1", (part_model_id,)).fetchone()
    return _colors(r["materials"]) if r else []


def stock_for(store: Store, model_id: int | None, plate: dict[str, Any], want_colors: list[str] | None = None) -> dict[str, Any]:
    """Vorrat für eine Platte: Stück gesamt, davon in passender Farbe, und die Quellen (aufgeteilte Drucke)."""
    src = _sources(store, model_id, plate)
    if not src:
        return {"pieces": 0, "match": 0, "parents": []}
    conds, args = [], []
    for kind, v in src:
        if kind == "m":
            conds.append("model_id = ?")
            args.append(v)
        else:
            conds.append("(model_id = ? AND gcode_plate = ?)")
            args += [v[0], v[1]]
    rows = [dict(r) for r in store._conn.execute(
        f"SELECT jobs.*, {Store.SCRAP_SQL} AS is_scrap FROM jobs WHERE split = 1 AND kind IS NULL AND ({' OR '.join(conds)}) "
        "ORDER BY start_ts", args)]
    want = set(want_colors or plate.get("colors") or part_colors(store, plate.get("part_model_id")))
    out, pieces, match = [], 0, 0
    for r in rows:
        units = store.stock(r)["units"]
        if units <= 0:
            continue
        cols = _colors(r.get("materials"))
        same = not want or not cols or set(cols) == want
        pieces += units
        match += units if same else 0
        out.append({"id": r["id"], "units": units, "colors": cols, "same": same, "name": r.get("task_name"), "code": r.get("code")})
    out.sort(key=lambda x: not x["same"])  # passende Farbe zuerst
    return {"pieces": pieces, "match": match, "parents": out}


def book_stock(store: Store, item: dict[str, Any], customer_id: int | None, free: bool) -> list[str]:
    """Bei „Zur Abrechnung“: Felder „aus Vorrat“ ausbuchen (Anteil für den Kunden, bei vereinbartem Preis zu 0 €).
    Gibt Hinweise zurück, wo der Vorrat nicht gereicht hat."""
    sm = summary(store, item)
    if not sm:
        return []
    notes = []
    for p in sm["plates"]:
        n = sum(1 for (s, pl), c in sm["grid"]["cells"].items() if pl == p["plate"] and c["state"] == "stock")
        if not n:
            continue
        need = n * (p["pieces"] or 1)
        st = stock_for(store, item.get("model_id"), p)
        for par in st["parents"]:
            if need <= 0:
                break
            take = min(need, par["units"])
            res = store.give(par["id"], customer_id, int(take), final_price=0.0 if free else None,
                             note=t("Auftrag: {p}", p=p["name"]))
            if isinstance(res, int):
                need -= take
        if need > 0:
            notes.append(t("{p}: {n} Stk. nicht im Vorrat", p=p["name"], n=need))
    return notes


def suggestions(store: Store, item: dict[str, Any], sm: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Platten mit offenen Feldern, die (teilweise) im Vorrat liegen: {plate, name, sets, pieces, match, colors}."""
    sm = summary(store, item) if sm is None else sm
    if not sm:
        return []
    out = []
    for p in sm["plates"]:
        open_sets = [s for (s, pl) in sm["grid"]["open"] if pl == p["plate"]]
        if not open_sets:
            continue
        st = stock_for(store, item.get("model_id"), p)
        reserved = sum(1 for (s, pl), c in sm["grid"]["cells"].items() if pl == p["plate"] and c["state"] == "stock") * (p["pieces"] or 1)
        free_same = st["match"] - reserved
        free_all = st["pieces"] - reserved
        per = p["pieces"] or 1
        k = min(len(open_sets), free_same // per)
        if k <= 0 and free_all // per <= 0:
            continue
        other = sorted({c for par in st["parents"] if not par["same"] for c in par["colors"]})
        same_cols = sorted({c for par in st["parents"] if par["same"] for c in par["colors"]})
        out.append({"plate": p["plate"], "name": p["name"], "sets": sorted(open_sets)[:max(k, 0)], "n_sets": k,
                    "n_open": len(open_sets), "pieces": free_all, "match": free_same, "other_colors": other, "colors": same_cols, "per": per,
                    "shared": bool(p.get("part_model_id"))})
    return out


def use_stock(store: Store, item: dict[str, Any], plate: int, n: int | None = None) -> int:
    """Vorschlag annehmen: bis zu ``n`` offene Felder dieser Platte auf „aus Vorrat“ (ohne n: so viele wie passen)."""
    sm = summary(store, item)
    if not sm:
        return 0
    sug = next((x for x in suggestions(store, item, sm) if x["plate"] == plate), None)
    if not sug:
        return 0
    k = sug["n_sets"] if n is None else max(0, min(n, sug["n_open"], sug["pieces"] // sug["per"]))
    sets = sorted(s for (s, pl) in sm["grid"]["open"] if pl == plate)[:k]
    with store._lock, store._conn:
        for s in sets:
            store._conn.execute("INSERT OR REPLACE INTO plate_cells (item_id, set_no, plate, state) VALUES (?, ?, ?, 'stock')",
                                (item["id"], s, plate))
    return len(sets)
