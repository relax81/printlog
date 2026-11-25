"""Arbeit und Zubehör: Arbeitszeiten, Zubehör-Inventar und Zusatzposten pro Druck/Projekt.

* **Arbeitszeit** am Druck: Vorbereitung (pro Platte/Druckjob), Nachbearbeitung (pro Stück oder einmalig)
  und Design (eigener Stundensatz). Stundensätze in den Einstellungen (``work_rate``, ``design_rate``).
  Arbeit steckt *nicht* in ``cost_total``, sondern in ``cost_work`` (davon ``cost_design``) und kommt nach
  der Marge zum Preis dazu. Ausnahme: Marge „auch auf Arbeitszeit“ (pro allgemeiner Marge bzw. pro Kunde).
* **Zubehör** aus dem Inventar (Tabelle ``accessories``) oder als einmaliger Posten (Freitext + Betrag),
  Tabelle ``job_extras``. Zubehör pro Stück wird mit der Stückzahl multipliziert. Die Kosten stecken in
  ``cost_extras`` und damit in ``cost_total`` (bekommen also die Marge wie Filament).
* **Bestand**: Zugeordnetes Zubehör ist reserviert. Verbraucht (vom Bestand abgebucht) wird es, wenn Stück
  montiert sind (``jobs.mounted``). Eine Übergabe zählt automatisch als montiert.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from .db import Store, code_label
from .i18n import is_en, t
from .ui import e, fmt_input, fmt_num, parse_num

ICONS = ["🔑", "⛓️", "🪝", "🧲", "🔩", "🔧", "🟤", "💡", "🎀", "📦", "🏷️", "🧷", "🪛", "🔋", "✨"]


# ---- Rechnen --------------------------------------------------------------------

def rates(settings: dict[str, str]) -> tuple[float | None, float | None]:
    """(Arbeit €/h, Design €/h) aus den Einstellungen."""
    return parse_num(settings.get("work_rate")), parse_num(settings.get("design_rate"))


def work_minutes(job: dict[str, Any]) -> tuple[float, float, float]:
    """(Vorbereitung, Nachbearbeitung gesamt, Design) in Minuten."""
    q = job.get("quantity") or 1
    post = job.get("post_min") or 0.0
    return (job.get("prep_min") or 0.0, post if job.get("post_once") else post * q, job.get("design_min") or 0.0)


def work_cost(job: dict[str, Any], settings: dict[str, str]) -> tuple[float | None, float | None]:
    """(Arbeit + Design, davon Design) in €; None, wenn nichts eingetragen ist."""
    prep, post, design = work_minutes(job)
    if not (prep or post or design):
        return None, None
    wr, dr = rates(settings)
    w = (prep + post) / 60 * (wr or 0)
    d = design / 60 * (dr or 0)
    return round(w + d, 4), round(d, 4)


def need(x: dict[str, Any], pieces: int) -> float:
    """Menge eines Zubehörs für ``pieces`` Stück."""
    if x.get("per_piece"):
        return (x.get("qty") or 0) * pieces
    return (x.get("qty") or 0) if pieces > 0 else 0.0


def extras_cost(extras: list[dict[str, Any]], qty: int) -> float | None:
    if not extras:
        return None
    return round(sum(need(x, qty) * (x.get("unit_price") or 0) for x in extras), 4)


# ---- Datenbank ------------------------------------------------------------------

def accessories(store: Store) -> list[dict[str, Any]]:
    """Inventar mit reserviertem Bestand (zugeordnet, aber noch nicht montiert)."""
    rows = [dict(r) for r in store._conn.execute("SELECT * FROM accessories ORDER BY grp COLLATE NOCASE, name COLLATE NOCASE")]
    res = reserved(store)
    for a in rows:
        a["reserved"] = res.get(a["id"], 0.0)
    return rows


def accessory(store: Store, aid: int) -> dict[str, Any] | None:
    r = store._conn.execute("SELECT * FROM accessories WHERE id = ?", (aid,)).fetchone()
    return dict(r) if r else None


def reserved(store: Store) -> dict[int, float]:
    out: dict[int, float] = {}
    rows = store._conn.execute(
        "SELECT x.*, COALESCE(j.quantity, 1) AS q FROM job_extras x JOIN jobs j ON j.id = x.job_id "
        "WHERE x.accessory_id IS NOT NULL").fetchall()
    for r in rows:
        x = dict(r)
        left = need(x, x["q"]) - (x.get("used") or 0)
        if left > 0:
            out[x["accessory_id"]] = out.get(x["accessory_id"], 0.0) + left
    return out


def job_extras(store: Store, job_id: int) -> list[dict[str, Any]]:
    rows = store._conn.execute(
        "SELECT x.*, a.icon, a.stock, a.warn_at, a.name AS acc_name FROM job_extras x "
        "LEFT JOIN accessories a ON a.id = x.accessory_id WHERE x.job_id = ? ORDER BY x.id", (job_id,)).fetchall()
    return [dict(r) for r in rows]


def extras_by_job(store: Store, ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    if not ids:
        return out
    marks = ", ".join("?" for _ in ids)
    for r in store._conn.execute(f"SELECT * FROM job_extras WHERE job_id IN ({marks}) ORDER BY id", ids):
        out.setdefault(r["job_id"], []).append(dict(r))
    return out


def save_accessory(store: Store, aid: int | None, **f: Any) -> int:
    with store._lock, store._conn:
        if aid:
            sets = ", ".join(f"{k} = ?" for k in f)
            store._conn.execute(f"UPDATE accessories SET {sets} WHERE id = ?", [*f.values(), aid])
            return aid
        f.setdefault("created_at", time.time())
        f.setdefault("stock", 0)
        cols = ", ".join(f)
        cur = store._conn.execute(f"INSERT INTO accessories ({cols}) VALUES ({', '.join('?' for _ in f)})", list(f.values()))
        return int(cur.lastrowid)


def restock(store: Store, aid: int, qty: float, price: float | None) -> None:
    """Zugang (neue Packung): Bestand rauf, Stückpreis gemittelt."""
    a = accessory(store, aid)
    if not a or qty <= 0:
        return
    stock = max(a.get("stock") or 0, 0)
    unit = a.get("unit_price")
    if price is not None:
        unit = price / qty if not unit or stock <= 0 else (stock * unit + price) / (stock + qty)
    save_accessory(store, aid, stock=(a.get("stock") or 0) + qty, unit_price=None if unit is None else round(unit, 4),
                   **({"pack_qty": qty, "pack_price": price} if price is not None else {}))


def delete_accessory(store: Store, aid: int) -> None:
    """Artikel aus dem Inventar löschen. Zuordnungen bleiben als einmalige Posten mit ihrem Preis."""
    with store._lock, store._conn:
        store._conn.execute("UPDATE job_extras SET accessory_id = NULL, per_piece = per_piece WHERE accessory_id = ?", (aid,))
        store._conn.execute("DELETE FROM accessories WHERE id = ?", (aid,))


def _job_row(store: Store, job_id: int) -> dict[str, Any] | None:
    r = store._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(r) if r else None


def sync_stock(store: Store, job_id: int) -> None:
    """Bestand an die montierten Stück anpassen (abbuchen oder zurückbuchen)."""
    job = _job_row(store, job_id)
    if not job:
        return
    q = job.get("quantity") or 1
    m = max(0, min(job.get("mounted") or 0, q))
    with store._lock, store._conn:
        for r in store._conn.execute("SELECT * FROM job_extras WHERE job_id = ? AND accessory_id IS NOT NULL", (job_id,)).fetchall():
            x = dict(r)
            target = need(x, m)
            delta = target - (x.get("used") or 0)
            if abs(delta) > 1e-9:
                store._conn.execute("UPDATE accessories SET stock = stock - ? WHERE id = ?", (delta, x["accessory_id"]))
                store._conn.execute("UPDATE job_extras SET used = ? WHERE id = ?", (target, x["id"]))


def refresh(store: Store, job_id: int) -> None:
    """Zubehör- und Arbeitskosten eines Drucks/Projekts neu berechnen (und die Summen darüber)."""
    from .costing import compute  # Kreisimport vermeiden

    job = _job_row(store, job_id)
    if not job:
        return
    xs = job_extras(store, job_id)
    q = job.get("quantity") or 1
    work, design = work_cost(job, store.settings())
    fields: dict[str, Any] = {"cost_extras": extras_cost(xs, q), "cost_work": work, "cost_design": design}
    if job.get("kind") is None and job.get("state") == "done":
        job.update(fields)
        fields.update(compute(job, store))
    store.update(job_id, **fields)
    if job.get("kind") == "project":
        store.refresh_project(job_id)


def refresh_all(store: Store) -> None:
    """Nach geänderten Stundensätzen: Arbeitskosten aller Drucke mit Arbeitszeit neu."""
    settings = store.settings()
    rows = store._conn.execute(
        "SELECT * FROM jobs WHERE prep_min IS NOT NULL OR post_min IS NOT NULL OR design_min IS NOT NULL").fetchall()
    with store._lock, store._conn:
        for r in rows:
            w, d = work_cost(dict(r), settings)
            store._conn.execute("UPDATE jobs SET cost_work = ?, cost_design = ? WHERE id = ?", (w, d, r["id"]))


def add_extra(store: Store, job_id: int, accessory_id: int | None, qty: float, per_piece: bool,
              name: str | None = None, unit_price: float | None = None) -> None:
    if accessory_id:
        a = accessory(store, accessory_id)
        if not a:
            return
        same = store._conn.execute("SELECT id, qty FROM job_extras WHERE job_id = ? AND accessory_id = ? AND per_piece = ?",
                                   (job_id, accessory_id, 1 if per_piece else 0)).fetchone()
        with store._lock, store._conn:
            if same:
                store._conn.execute("UPDATE job_extras SET qty = ? WHERE id = ?", (same["qty"] + qty, same["id"]))
            else:
                store._conn.execute(
                    "INSERT INTO job_extras (job_id, accessory_id, name, qty, per_piece, unit_price) VALUES (?, ?, ?, ?, ?, ?)",
                    (job_id, accessory_id, a["name"], qty, 1 if per_piece else 0, a.get("unit_price")))
    else:
        with store._lock, store._conn:
            store._conn.execute(
                "INSERT INTO job_extras (job_id, name, qty, per_piece, unit_price) VALUES (?, ?, ?, ?, ?)",
                (job_id, (name or "").strip()[:120] or t("Posten"), qty, 1 if per_piece else 0, unit_price))
    sync_stock(store, job_id)
    refresh(store, job_id)


def set_extra_qty(store: Store, extra_id: int, qty: float) -> int | None:
    r = store._conn.execute("SELECT job_id FROM job_extras WHERE id = ?", (extra_id,)).fetchone()
    if not r:
        return None
    if qty <= 0:
        return remove_extra(store, extra_id)
    with store._lock, store._conn:
        store._conn.execute("UPDATE job_extras SET qty = ? WHERE id = ?", (qty, extra_id))
    sync_stock(store, r["job_id"])
    refresh(store, r["job_id"])
    return r["job_id"]


def remove_extra(store: Store, extra_id: int) -> int | None:
    """Posten entfernen; schon abgebuchtes Zubehör kommt zurück in den Bestand."""
    r = store._conn.execute("SELECT * FROM job_extras WHERE id = ?", (extra_id,)).fetchone()
    if not r:
        return None
    with store._lock, store._conn:
        if r["accessory_id"] and r["used"]:
            store._conn.execute("UPDATE accessories SET stock = stock + ? WHERE id = ?", (r["used"], r["accessory_id"]))
        store._conn.execute("DELETE FROM job_extras WHERE id = ?", (extra_id,))
    refresh(store, r["job_id"])
    return r["job_id"]


def set_mounted(store: Store, job_id: int, mounted: int) -> None:
    job = _job_row(store, job_id)
    if not job:
        return
    q = job.get("quantity") or 1
    store.update(job_id, mounted=max(0, min(int(mounted), q)) or None)
    sync_stock(store, job_id)


def on_delivered(store: Store, job_id: int) -> None:
    """Übergeben heißt montiert: montierte Stück mindestens auf die übergebenen Stück anheben."""
    job = _job_row(store, job_id)
    if not job:
        return
    root = _job_row(store, job["share_of"]) if job.get("kind") == "share" and job.get("share_of") else job
    if not root:
        return
    ids = [root["id"]] + [r[0] for r in store._conn.execute("SELECT id FROM jobs WHERE kind = 'share' AND share_of = ?", (root["id"],))]
    marks = ", ".join("?" for _ in ids)
    delivered = store._conn.execute(f"SELECT COALESCE(SUM(qty), 0) FROM handovers WHERE job_id IN ({marks})", ids).fetchone()[0]
    rq = root.get("quantity") or 1
    targets = [(root, min(delivered, rq))]
    if root.get("kind") == "project":
        for k in store._conn.execute("SELECT * FROM jobs WHERE project_id = ?", (root["id"],)).fetchall():
            k = dict(k)
            targets.append((k, math.floor((k.get("quantity") or 1) * min(delivered, rq) / rq)))
    for j, d in targets:
        if d > (j.get("mounted") or 0) and store._conn.execute("SELECT 1 FROM job_extras WHERE job_id = ? LIMIT 1", (j["id"],)).fetchone():
            set_mounted(store, j["id"], d)


def last_like(store: Store, job: dict[str, Any]) -> dict[str, Any] | None:
    """Letzter anderer Druck mit gleichem Namen, der Arbeit oder Zubehör hat (Vorschlag „Übernehmen“)."""
    if not job.get("task_name"):
        return None
    r = store._conn.execute(
        "SELECT * FROM jobs WHERE id <> ? AND task_name = ? AND COALESCE(kind, '') <> 'share' AND "
        "(prep_min IS NOT NULL OR post_min IS NOT NULL OR EXISTS (SELECT 1 FROM job_extras x WHERE x.job_id = jobs.id)) "
        "ORDER BY start_ts DESC LIMIT 1", (job["id"], job["task_name"])).fetchone()
    return dict(r) if r else None


def copy_from(store: Store, job_id: int, src_id: int) -> None:
    """Arbeitszeiten (ohne Design, das ist schon gemacht) und Zubehör von einem anderen Druck übernehmen."""
    src = _job_row(store, src_id)
    if not src:
        return
    store.update(job_id, prep_min=src.get("prep_min"), post_min=src.get("post_min"), post_once=src.get("post_once"))
    for x in job_extras(store, src_id):
        add_extra(store, job_id, x.get("accessory_id"), x["qty"], bool(x["per_piece"]), x.get("name"),
                  None if x.get("accessory_id") else x.get("unit_price"))
    refresh(store, job_id)


def purge(store: Store, job_id: int) -> None:
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM job_extras WHERE job_id = ?", (job_id,))


# ---- Anzeige --------------------------------------------------------------------

def work_total(job: dict[str, Any]) -> float:
    """Arbeit + Design in € für Kopf und Preise (Projekt/Anteil: inkl. der Drucke)."""
    w = job.get("bill_work") if job.get("kind") in ("project", "share") and job.get("bill_work") is not None else job.get("cost_work")
    return w or 0.0


def sell_price(base: float | None, work: float, pct: float, work_on: bool) -> float | None:
    """Preis = (Kosten [+ Arbeit]) × (1 + Marge) [+ Arbeit]."""
    if base is None:
        return None if not work else work * (1 + pct / 100 if work_on else 1)
    if work_on:
        return (base + work) * (1 + pct / 100)
    return base * (1 + pct / 100) + work


def _min(v: float) -> str:
    return fmt_input(round(v, 1)) + " min"


def _stepper(act: str, field: str, value: float | None, step: float, label: str) -> str:
    v = value or 0
    return (f'<form class="zstp" method="post" action="{act}"><input type="hidden" name="field" value="{field}">'
            f'<button name="delta" value="{-step:g}" aria-label="{e(t("weniger"))}"{" disabled" if v <= 0 else ""}>−</button>'
            f'<label><input name="value" inputmode="decimal" value="{fmt_input(value) if value else ""}" placeholder="0" '
            f'aria-label="{e(label)}" onchange="this.form.requestSubmit()"><i>min</i></label>'
            f'<button name="delta" value="{step:g}" aria-label="{e(t("mehr"))}">+</button></form>')


def box(store: Store, job: dict[str, Any], picker: bool = True) -> str:
    """Kasten „Arbeit & Zubehör“ in der Übersicht eines Drucks oder Projekts."""
    jid = job["id"]
    xs = job_extras(store, jid)
    settings = store.settings()
    wr, dr = rates(settings)
    q = job.get("quantity") or 1
    project = job.get("kind") == "project"
    unit = t("Satz") if project else ("piece" if is_en() else "Stück")
    prep, post, design = work_minutes(job)
    has_any = bool(xs or prep or post or design)
    act = f"api/jobs/{jid}/work"
    like = None if has_any else last_like(store, job)
    sug = ""
    if like:
        lx = job_extras(store, like["id"])
        parts = [x.get("name") or "?" for x in lx][:3]
        if like.get("post_min"):
            parts.append(t("{m} Nachbearbeitung", m=_min(like["post_min"])))
        sug = (f'<form class="zsug" method="post" action="api/jobs/{jid}/extras"><input type="hidden" name="copy" value="{like["id"]}">'
               f'💡 <span>{t("Beim letzten Druck mit diesem Namen ({ref}):", ref=e(code_label(like.get("code"))))} <b>{e(", ".join(parts) or t("Arbeitszeit"))}</b></span>'
               f'<button class="btn sm">{t("Übernehmen")}</button></form>')
    if not has_any and not sug:
        return (f'<div class="zkmin calc"><button type="button" class="btn" data-zk-open>🧰 {t("＋ Arbeit & Zubehör")}</button>'
                f'<span class="muted">{t("Vorbereitung, Nachbearbeitung, Design und Zubehör wie Ringe oder Magnete")}</span></div>'
                f'<section class="zk calc" id="zk" hidden>{_inner(store, job, xs, act, wr, dr, q, unit, settings)}</section>')
    return f'<section class="zk calc" id="zk">{sug}{_inner(store, job, xs, act, wr, dr, q, unit, settings)}</section>'


def _inner(store: Store, job: dict[str, Any], xs: list[dict[str, Any]], act: str, wr: float | None, dr: float | None,
           q: int, unit: str, settings: dict[str, str]) -> str:
    jid = job["id"]
    prep, post, design = work_minutes(job)
    eur = lambda m, r: fmt_num(m / 60 * r, 2, "€") if r else "–"  # noqa: E731
    once = bool(job.get("post_once"))
    rate_hint = (t("Arbeit {a} · Design {d} (Einstellungen)", a=fmt_num(wr, 2, "€/h") if wr else "–", d=fmt_num(dr, 2, "€/h") if dr else "–")
                 if wr or dr else t("Noch kein Stundensatz: in den Einstellungen unter „Arbeit & Zubehör“ eintragen."))
    work = (f'<div><h4>{t("Arbeitszeit")}</h4>'
            f'<div class="ar"><div class="l"><b>{t("Vorbereitung")}</b><small>{t("pro Platte/Druckjob · Slicen, Platte, Druck starten")}</small></div>'
            f'{_stepper(act, "prep_min", job.get("prep_min"), 5, t("Vorbereitung"))}<div class="v">{eur(prep, wr)}</div></div>'
            f'<div class="ar"><div class="l"><b>{t("Nachbearbeitung")}</b><small>'
            + (t("einmalig für alles") if once else t("{m} × {n} {u} = {s}", m=_min(job.get("post_min") or 0), n=q, u=t("Stk.") if is_en() and unit == "piece" else unit, s=_min(post)))
            + f'</small><form class="mini" method="post" action="{act}"><input type="hidden" name="field" value="post_once">'
            f'<button name="value" value="0" class="{"" if once else "on"}">{t("pro {u}", u=unit)}</button>'
            f'<button name="value" value="1" class="{"on" if once else ""}">{t("einmalig")}</button></form></div>'
            f'{_stepper(act, "post_min", job.get("post_min"), 0.5 if not once else 5, t("Nachbearbeitung"))}<div class="v">{eur(post, wr)}</div></div>'
            f'<div class="ar"><div class="l"><b>✏️ {t("Design")}</b><small>{t("einmalig · eigener Stundensatz")}</small></div>'
            f'{_stepper(act, "design_min", job.get("design_min"), 15, t("Design"))}<div class="v">{eur(design, dr)}</div></div>'
            f'<div class="muted zh2">{e(rate_hint)}</div></div>')

    rows = ""
    for x in xs:
        n = need(x, q)
        cost = n * (x.get("unit_price") or 0)
        if x.get("accessory_id"):
            st = x.get("stock") or 0
            left = need(x, q) - (x.get("used") or 0)
            stock_txt = t("Bestand {s}", s=fmt_input(round(st, 2)))
            if left > 0:
                after = st - left
                cls = "bad" if after < 0 else "warn" if x.get("warn_at") is not None and after <= x["warn_at"] else "ok"
                stock_txt += f' · <em class="{cls}">{t("nach Montage {a}", a=fmt_input(round(after, 2)))}</em>'
            sub = (t("{q} pro {u} × {n} = {t}", q=fmt_input(x["qty"]), u=unit, n=q, t=fmt_input(round(n, 2))) if x["per_piece"]
                   else t("{q} einmalig", q=fmt_input(x["qty"]))) + f' · {t("je {p}", p=fmt_num(x.get("unit_price"), 2, "€"))} · {stock_txt}'
            step = 1
            ic = e(x.get("icon") or "🔹")
        else:
            sub = t("einmaliger Posten · nicht im Inventar")
            step = 0
            ic = "✎"
        stp = ""
        if step:
            stp = (f'<form class="zstp sm" method="post" action="api/jobs/{jid}/extras"><input type="hidden" name="extra" value="{x["id"]}">'
                   f'<button name="qty" value="{x["qty"] - 1:g}" aria-label="{e(t("weniger"))}">−</button>'
                   f'<i>{fmt_input(x["qty"])}{"/" + e(t("Stk.")) if x["per_piece"] else ""}</i>'
                   f'<button name="qty" value="{x["qty"] + 1:g}" aria-label="{e(t("mehr"))}">+</button></form>')
        rows += (f'<div class="zr{"" if x.get("accessory_id") else " fr"}"><div class="ic">{ic}</div>'
                 f'<div class="l"><b>{e(x.get("name") or "?")}</b><small>{sub}</small></div>{stp}'
                 f'<div class="v">{fmt_num(cost, 2, "€")}</div>'
                 f'<form method="post" action="api/jobs/{jid}/extras"><input type="hidden" name="extra" value="{x["id"]}">'
                 f'<button class="x" name="qty" value="0" title="{e(t("Entfernen"))}">✕</button></form></div>')
    if not rows:
        rows = f'<p class="muted" style="margin:4px 0 0">{t("Noch kein Zubehör.")}</p>'
    mount = ""
    if any(x.get("accessory_id") for x in xs):
        m = min(job.get("mounted") or 0, q)
        mount = (f'<form class="zmount" method="post" action="api/jobs/{jid}/mounted">'
                 f'<span>🔧 {t("Montiert")}</span><button name="mounted" value="{max(m - 1, 0)}"{" disabled" if m <= 0 else ""}>−</button>'
                 f'<b>{t("{m} von {n}", m=m, n=q)}</b><button name="mounted" value="{min(m + 1, q)}"{" disabled" if m >= q else ""}>+</button>'
                 + (f'<button class="btn sm" name="mounted" value="{q}">{t("alle")}</button>' if m < q else "")
                 + f'<small>{t("Erst montierte Stück gehen vom Bestand ab, der Rest ist reserviert. Übergeben zählt als montiert.")}</small></form>')
    acc = (f'<div><h4>{t("Zubehör & Material")}</h4>{rows}'
           f'<div class="zadd"><button type="button" class="btn" data-zk-pick>＋ {t("Aus Inventar")}</button>'
           f'<button type="button" class="btn" data-zk-free>✎ {t("Einmaliger Posten")}</button></div>'
           f'<form class="zfree" method="post" action="api/jobs/{jid}/extras" hidden><input name="name" placeholder="{e(t("z. B. Geschenkbeutel"))}" maxlength="120" required>'
           f'<input name="amount" inputmode="decimal" placeholder="€" required style="width:90px"><button class="btn primary">{t("Hinzufügen")}</button></form>'
           f'{mount}</div>')

    druck = (job.get("cost_total") or 0) - (job.get("cost_extras") or 0)
    if job.get("kind") == "project":
        druck = (job.get("cost_total") or 0) - (job.get("cost_extras") or 0)
    zub = job.get("cost_extras") or 0
    wd = job.get("cost_design") or 0
    wa = (job.get("cost_work") or 0) - wd
    kids_work = work_total(job) - (job.get("cost_work") or 0) if job.get("kind") == "project" else 0
    total = druck + zub + wa + wd + kids_work
    parts = [(t("🖨 Druck"), druck, "var(--s1)"), (t("🔗 Zubehör"), zub, "var(--s5)"),
             (t("⏱ Arbeit ({m})", m=_min(prep + post)), wa, "var(--s2)"), (t("✏️ Design ({m})", m=_min(design)), wd, "var(--s8)")]
    if kids_work:
        parts.insert(2, (t("⏱ Arbeit in den Drucken"), kids_work, "var(--s2)"))
    bar = "".join(f'<i style="flex:{max(v, 0):.4f};background:{c}"></i>' for _, v, c in parts if v > 0)
    srows = "".join(f'<div class="sr"><span>{e(n)}</span><b>{fmt_num(v, 2, "€")}</b></div>' for n, v, _ in parts if v or n.startswith("🖨"))
    per = f'<div class="hint">{t("{v} pro {u}", v=fmt_num(total / q, 2, "€"), u=unit)}</div>' if q > 1 else ""
    summe = (f'<div class="sum"><h4>{t("Selbstkosten")}</h4><div class="bar">{bar}</div>{srows}'
             f'<div class="sr t"><span>{t("Gesamt")}</span><b>{fmt_num(total, 2, "€")}</b></div>{per}</div>')
    head = (f'<div class="zh"><b>🧰 {t("Arbeit & Zubehör")}</b><span class="muted">'
            f'{t("Zubehör pro Stück wird mit der Stückzahl ({n}) multipliziert", n=q)}</span></div>')
    return head + f'<div class="zkg">{work}{acc}{summe}</div>'


def picker(store: Store, job: dict[str, Any]) -> str:
    """Dialog „Zubehör hinzufügen“: Kacheln aus dem Inventar."""
    items = accessories(store)
    q = job.get("quantity") or 1
    groups = sorted({a["grp"] for a in items if a.get("grp")}, key=str.casefold)
    chips = f'<button type="button" class="on" data-g="">{t("Alle")}</button>' + "".join(
        f'<button type="button" data-g="{e(g)}">{e(g)}</button>' for g in groups)
    tiles = ""
    for a in items:
        free = (a.get("stock") or 0) - (a.get("reserved") or 0)
        cls = "out" if free <= 0 else "low" if a.get("warn_at") is not None and free <= a["warn_at"] else ""
        st = (t("leer") if (a.get("stock") or 0) <= 0 else
              t("{n} frei · bald nachkaufen", n=fmt_input(round(free, 2))) if cls == "low" else
              t("{n} frei", n=fmt_input(round(free, 2))) if free > 0 else t("{n} auf Lager, alles reserviert", n=fmt_input(round(a.get("stock") or 0, 2))))
        tiles += (f'<button type="button" class="tl {cls}" data-id="{a["id"]}" data-g="{e(a.get("grp") or "")}" data-n="{e((a.get("name") or "").casefold())}" '
                  f'data-p="{a.get("unit_price") or 0}"><span class="q"><span data-d="-1">−</span><em>1</em><span data-d="1">+</span></span>'
                  f'<i>{e(a.get("icon") or "🔹")}</i><b>{e(a["name"])}</b><small>{t("{p}/Stk.", p=fmt_num(a.get("unit_price"), 2, "€"))}</small>'
                  f'<span class="st {cls}">{e(st)}</span></button>')
    if not tiles:
        tiles = f'<p class="muted">{t("Das Inventar ist noch leer.")} <a href="settings#zubehoer">{t("Zubehör in den Einstellungen anlegen →")}</a></p>'
    return (f'<div class="inv" id="zkpick" hidden><form class="d" method="post" action="api/jobs/{job["id"]}/extras">'
            f'<h3>{t("Zubehör hinzufügen")}</h3><p class="sub">{t("Menge gilt pro Stück und wird mit {n} multipliziert. Tippen wählt, ± ändert die Menge.", n=q)}</p>'
            f'<input type="search" class="srch" placeholder="{e(t("Suchen …"))}" autocomplete="off">'
            + (f'<div class="chs">{chips}</div>' if groups else "")
            + f'<div class="tiles">{tiles}</div><input type="hidden" name="add" value="">'
            f'<div class="ft"><span class="tot"></span><span class="sp"></span>'
            f'<a class="btn" href="settings#zubehoer">＋ {t("Neu im Inventar")}</a>'
            f'<button type="button" class="btn" data-zk-close>{t("Abbrechen")}</button>'
            f'<button class="btn primary">{t("Übernehmen")}</button></div></form></div>')


BOX_CSS = """
.zk{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin-bottom:16px}
.zkmin{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}.zkmin .muted{font-size:13.5px}
.zk .zh{display:flex;align-items:center;gap:10px;margin:0 0 12px;flex-wrap:wrap}.zk .zh b{font-size:17px}.zk .zh .muted{font-size:13px}
.zkg{display:grid;grid-template-columns:1.05fr 1.25fr .8fr;gap:22px}
.zkg h4{margin:0 0 8px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.zk .ar{display:grid;grid-template-columns:minmax(0,1fr) auto 70px;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}
.zk .ar .l b{display:block;font-size:15px}.zk .ar .l small{display:block;color:var(--muted);font-size:12.5px}
.zk .v{text-align:right;font-variant-numeric:tabular-nums;font-weight:700}
.zk .zh2{font-size:12.5px;margin-top:8px}
.zstp{display:flex;align-items:center;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--bg)}
.zstp button{width:36px;height:38px;border:0;background:none;color:var(--muted);font-weight:700;font-size:18px;cursor:pointer;padding:0}
.zstp button:disabled{opacity:.35;cursor:default}
.zstp label{display:flex;align-items:center;gap:3px;padding:0 2px}.zstp input{width:44px;text-align:right;border:0;background:none;padding:0;font-weight:700;font-size:15px;min-height:0;color:var(--fg)}
.zstp label i{font-style:normal;color:var(--muted);font-size:13px}
.zstp.sm button{width:30px;height:32px}.zstp i{font-style:normal;min-width:44px;text-align:center;font-weight:700;font-size:14px}
.zk .mini{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-top:4px;width:max-content}
.zk .mini button{padding:2px 9px;border:0;background:none;color:var(--muted);font-weight:600;font-size:12px;cursor:pointer;min-height:0}
.zk .mini button.on{background:color-mix(in srgb,var(--accent) 25%,var(--card));color:var(--fg)}
.zr{display:grid;grid-template-columns:38px minmax(0,1fr) auto 64px 26px;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}
.zr .ic{width:38px;height:38px;border-radius:10px;background:var(--bg);border:1px solid var(--line);display:grid;place-items:center;font-size:19px}
.zr.fr .ic{border-style:dashed;color:var(--muted)}
.zr .l b{display:block;font-size:15px}.zr .l small{display:block;color:var(--muted);font-size:12.5px}
.zr em{font-style:normal}.zr em.ok{color:var(--ok)}.zr em.warn{color:var(--warn)}.zr em.bad{color:var(--bad)}
.zr .x{border:0;background:none;color:var(--muted);cursor:pointer;font-size:15px;padding:4px;min-height:0}
.zr.fr .zstp{visibility:hidden}
.zadd{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}.zadd .btn{min-height:40px;padding:0 14px;font-weight:600}
.zfree{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}.zfree input{flex:1;min-width:120px}
.zmount{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
.zmount>span{font-weight:700}.zmount button:not(.btn){width:32px;height:32px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font-weight:700;cursor:pointer;padding:0;min-height:0}
.zmount button:disabled{opacity:.35}.zmount small{flex-basis:100%;color:var(--muted);font-size:12.5px}
.zk .sum .sr{display:flex;justify-content:space-between;padding:6px 0;font-size:14.5px;font-variant-numeric:tabular-nums}
.zk .sum .sr span{color:var(--muted)}.zk .sum .sr.t{border-top:1px solid var(--line);margin-top:6px;padding-top:10px;font-size:17px}
.zk .sum .sr.t b{color:var(--accent)}.zk .sum .hint{font-size:12.5px;color:var(--muted);margin-top:8px}
.zk .sum .bar{display:flex;gap:2px;height:9px;border-radius:5px;overflow:hidden;margin:4px 0 10px;background:color-mix(in srgb,var(--fg) 7%,transparent)}
.zk .sum .bar i{display:block;height:100%}
.zsug{display:flex;align-items:center;gap:12px;border:1px dashed color-mix(in srgb,var(--accent) 60%,var(--line));border-radius:12px;padding:10px 14px;margin-bottom:14px;font-size:14px}
.zsug span{flex:1}.zsug b{color:var(--accent)}
.inv{position:fixed;inset:0;background:#000a;z-index:90;display:grid;place-items:center;padding:12px}
.inv[hidden]{display:none}
.inv .d{width:min(760px,96vw);max-height:92vh;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 20px 60px #000c}
.inv h3{margin:0 0 4px;font-size:20px}.inv .sub{color:var(--muted);font-size:14px;margin:0 0 12px}
.inv .srch{width:100%;margin-bottom:12px;box-sizing:border-box}
.inv .chs{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
.inv .chs button{border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:13px;color:var(--muted);font-weight:600;background:none;cursor:pointer;min-height:0}
.inv .chs button.on{background:var(--accent);color:var(--bg);border-color:var(--accent)}
.inv .tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.inv .tl{border:1px solid var(--line);border-radius:14px;padding:12px;background:var(--bg);display:flex;flex-direction:column;align-items:flex-start;gap:4px;min-height:118px;
 position:relative;text-align:left;color:var(--fg);cursor:pointer;font:inherit}
.inv .tl i{font-style:normal;font-size:24px}.inv .tl b{font-size:14px;line-height:1.25}.inv .tl small{color:var(--muted);font-size:12.5px}
.inv .tl .st{margin-top:auto;font-size:12.5px;font-weight:600;color:var(--muted)}.inv .tl .st.low{color:var(--warn)}.inv .tl .st.out{color:var(--bad)}
.inv .tl.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent);background:color-mix(in srgb,var(--accent) 10%,var(--bg))}
.inv .tl .q{position:absolute;top:8px;right:8px;display:none;align-items:center;border:1px solid var(--accent);border-radius:999px;font-size:13px;font-weight:700;background:var(--card)}
.inv .tl.on .q{display:flex}.inv .tl .q span{padding:2px 9px;color:var(--accent)}.inv .tl .q em{font-style:normal;padding:0 2px}
.inv .tl.out:not(.on){opacity:.6}
.inv .ft{display:flex;align-items:center;gap:10px;margin-top:14px;border-top:1px solid var(--line);padding-top:12px;flex-wrap:wrap}
.inv .ft .sp{flex:1}.inv .ft .tot{font-size:14px;color:var(--muted)}.inv .ft .tot b{color:var(--fg)}
.kps.six{grid-template-columns:repeat(3,minmax(0,1fr))}
.pr.gw b.pos{color:var(--ok)}.pr.gw b.neg{color:var(--bad)}
@media (max-width:760px){.zkg{grid-template-columns:1fr;gap:18px}.zk .zh .muted{display:none}.kps.six{grid-template-columns:repeat(2,minmax(0,1fr))}
 .zr{grid-template-columns:38px minmax(0,1fr) 64px 26px}.zr .zstp{grid-column:2;grid-row:2;width:max-content}.zr .v{grid-column:3;grid-row:1}.zr>form:last-child{grid-column:4;grid-row:1}
 .zk .ar{grid-template-columns:minmax(0,1fr) auto;row-gap:6px}.zk .ar .l{grid-column:1/-1}.zk .ar .zstp{width:max-content}
 .zk .ar .l small{display:inline}.zk .ar .l .mini{display:flex}
 .inv .tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.inv .d{padding:14px}}
"""

BOX_JS = """
(()=>{const pick=document.getElementById('zkpick');
document.addEventListener('click',ev=>{const o=ev.target.closest('[data-zk-open]');if(o){const z=document.getElementById('zk');z.hidden=false;o.closest('.zkmin').remove();z.scrollIntoView({behavior:'smooth',block:'start'});return}
 if(ev.target.closest('[data-zk-free]')){const f=ev.target.closest('.zk').querySelector('.zfree');f.hidden=!f.hidden;if(!f.hidden)f.querySelector('input').focus();return}
 if(ev.target.closest('[data-zk-pick]')&&pick){pick.hidden=false;return}
 if(ev.target.closest('[data-zk-close]')||ev.target===pick){pick.hidden=true;return}});
if(!pick)return;
const tot=pick.querySelector('.tot'),add=pick.querySelector('input[name=add]'),eur=v=>v.toFixed(2).replace('.',',')+' €';
const upd=()=>{const sel=[...pick.querySelectorAll('.tl.on')];const per=+(pick.dataset.q||1);let s=0,n=0;
 add.value=JSON.stringify(sel.map(t=>{const q=+t.querySelector('em').textContent;s+=q*per*(+t.dataset.p);n+=q*per;return[+t.dataset.id,q]}));
 tot.innerHTML=sel.length?`${sel.length} ${sel.length==1?'%ONE%':'%MANY%'} · ${n} %PCS% · <b>${eur(s)}</b>`:''};
pick.querySelectorAll('.tl').forEach(t=>t.addEventListener('click',ev=>{const d=ev.target.closest('[data-d]');const em=t.querySelector('em');
 if(d){const v=Math.max(0,+em.textContent+(+d.dataset.d));em.textContent=v;if(!v){t.classList.remove('on');em.textContent=1}}else if(!t.classList.contains('on'))t.classList.add('on');else t.classList.remove('on');upd()}));
const srch=pick.querySelector('.srch');let grp='';
const filt=()=>{const q=srch.value.trim().toLowerCase();pick.querySelectorAll('.tl').forEach(t=>{t.hidden=(grp&&t.dataset.g!==grp)||(q&&!t.dataset.n.includes(q))})};
srch.addEventListener('input',filt);
pick.querySelectorAll('.chs button').forEach(b=>b.addEventListener('click',()=>{grp=b.dataset.g;pick.querySelectorAll('.chs button').forEach(x=>x.classList.toggle('on',x===b));filt()}));
pick.querySelector('form').addEventListener('submit',ev=>{upd();if(add.value==='[]'){ev.preventDefault();pick.hidden=true}});
if(location.hash==='#zubehoer-neu')pick.hidden=false;
})();
"""


def box_js(job: dict[str, Any]) -> str:
    js = BOX_JS.replace("%ONE%", t("Artikel")).replace("%MANY%", t("Artikel")).replace("%PCS%", t("Teile"))
    return js.replace("const per=+(pick.dataset.q||1)", f"const per={job.get('quantity') or 1}")


# ---- Einstellungen --------------------------------------------------------------

def settings_section(store: Store) -> str:
    """Tab „Arbeit & Zubehör“: Stundensätze und Inventar (Teil des großen Einstellungen-Formulars)."""
    cfg = store.settings()
    rows = ""
    for a in accessories(store):
        aid = a["id"]
        stock = a.get("stock") or 0
        cls = "outb" if stock <= 0 else "lowb" if a.get("warn_at") is not None and stock - (a.get("reserved") or 0) <= a["warn_at"] else ""
        res = f'<small class="muted">{t("{n} reserviert", n=fmt_input(round(a["reserved"], 2)))}</small>' if a.get("reserved") else ""
        buy = (f'<a class="buy" href="{e(a["url"])}" target="_blank" rel="noopener" title="{e(t("Kauflink öffnen"))}">🛒</a>'
               if a.get("url") and str(a["url"]).startswith(("http://", "https://")) else "")
        rows += (f'<tr><td data-l="{e(t("Symbol"))}"><input class="ico" name="acc_icon__{aid}" value="{e(a.get("icon") or "")}" maxlength="4" list="zk_icons"></td>'
                 f'<td class="nm" data-l="{e(t("Name"))}"><input name="acc_name__{aid}" value="{e(a["name"])}" maxlength="80">'
                 f'<span class="lk"><input name="acc_url__{aid}" value="{e(a.get("url") or "")}" type="url" placeholder="{e(t("Kauflink"))} https://…">{buy}</span></td>'
                 f'<td data-l="{e(t("Gruppe"))}"><input name="acc_grp__{aid}" value="{e(a.get("grp") or "")}" maxlength="40" list="zk_groups"></td>'
                 f'<td data-l="{e(t("Packung"))}"><span class="pk"><input class="num" name="acc_pq__{aid}" value="{fmt_input(a.get("pack_qty"))}" inputmode="decimal" title="{e(t("Stück pro Packung"))}"><i>{t("für")}</i>'
                 f'<input class="num" name="acc_pp__{aid}" value="{fmt_input(a.get("pack_price"))}" inputmode="decimal" title="{e(t("Preis €"))}"><i>€</i></span>'
                 f'<small class="muted">= {fmt_num(a.get("unit_price"), 3, "€")} {t("pro Stück")}</small></td>'
                 f'<td class="n {cls}" data-l="{e(t("Bestand"))}"><input class="num" name="acc_st__{aid}" value="{fmt_input(round(stock, 2))}" inputmode="decimal">{res}</td>'
                 f'<td class="n" data-l="{e(t("Warnen ab"))}"><input class="num" name="acc_wa__{aid}" value="{fmt_input(a.get("warn_at"))}" inputmode="decimal"></td>'
                 f'<td data-l="{e(t("Zugang"))}"><span class="zg"><input class="num" name="acc_aq__{aid}" placeholder="{e(t("Stk."))}" inputmode="decimal">'
                 f'<input class="num" name="acc_ap__{aid}" placeholder="€" inputmode="decimal"></span></td>'
                 f'<td data-l="{e(t("Löschen"))}"><label class="del" title="{e(t("Löschen"))}"><input type="checkbox" name="acc_del__{aid}" value="1"> 🗑</label></td></tr>')
    new = (f'<tr class="new"><td data-l="{e(t("Symbol"))}"><input class="ico" name="acc_icon__new" maxlength="4" list="zk_icons" placeholder="🔹"></td>'
           f'<td class="nm" data-l="{e(t("Name"))}"><input name="acc_name__new" maxlength="80" placeholder="{e(t("Neues Zubehör, z. B. Schlüsselring 25 mm"))}">'
           f'<span class="lk"><input name="acc_url__new" type="url" placeholder="{e(t("Kauflink"))} https://…"></span></td>'
           f'<td data-l="{e(t("Gruppe"))}"><input name="acc_grp__new" maxlength="40" list="zk_groups" placeholder="{e(t("z. B. Magnete"))}"></td>'
           f'<td data-l="{e(t("Packung"))}"><span class="pk"><input class="num" name="acc_pq__new" inputmode="decimal" placeholder="100"><i>{t("für")}</i>'
           f'<input class="num" name="acc_pp__new" inputmode="decimal" placeholder="6,99"><i>€</i></span></td>'
           f'<td class="n" data-l="{e(t("Bestand"))}"><input class="num" name="acc_st__new" inputmode="decimal" placeholder="{e(t("= Packung"))}"></td>'
           f'<td class="n" data-l="{e(t("Warnen ab"))}"><input class="num" name="acc_wa__new" inputmode="decimal"></td><td></td><td></td></tr>')
    groups = sorted({a["grp"] for a in accessories(store) if a.get("grp")}, key=str.casefold)
    lists = ('<datalist id="zk_icons">' + "".join(f'<option value="{i}">' for i in ICONS) + '</datalist>'
             '<datalist id="zk_groups">' + "".join(f'<option value="{e(g)}">' for g in groups) + '</datalist>')
    wr, dr = cfg.get("work_rate") or "", cfg.get("design_rate") or ""
    return f"""<h2 style="margin-top:0">{t("Arbeitszeit")}</h2>
<p class="muted">{t("Stundensätze für den Kasten „Arbeit & Zubehör“ in der Übersicht eines Drucks. Leer = Zeit wird nur notiert, nicht berechnet. Ob eine Marge auch auf die Arbeitszeit kommt, stellst du pro Marge (Tab „Margen“) und pro Kunde ein. Standard: nein, der Stundensatz ist schon dein Preis.")}</p>
<div class="zrates"><label>⏱ {t("Arbeit (Vorbereitung, Nachbearbeitung)")}<span><input class="num" name="work_rate" value="{e(fmt_input(parse_num(wr)))}" inputmode="decimal" placeholder="20"> €/h</span></label>
<label>✏️ {t("Design")}<span><input class="num" name="design_rate" value="{e(fmt_input(parse_num(dr)))}" inputmode="decimal" placeholder="30"> €/h</span></label></div>
<h2>{t("Zubehör-Inventar")}</h2>
<p class="muted">{t("Preis pro Stück = Packungspreis ÷ Menge. Zugeordnetes Zubehör ist reserviert, vom Bestand abgebucht wird es beim Montieren (oder bei der Übergabe). Zugang = neue Packung gekauft: Menge und Preis eintragen, der Stückpreis wird gemittelt.")}</p>
<div class="wrap"><table class="stack acc"><thead><tr><th></th><th>{t("Name")} · {t("Kauflink")}</th><th>{t("Gruppe")}</th><th>{t("Packung")}</th>
<th class="n">{t("Bestand")}</th><th class="n">{t("Warnen ab")}</th><th>{t("Zugang")}</th><th></th></tr></thead>
<tbody>{rows}{new}</tbody></table></div>{lists}"""


SETTINGS_CSS = """
.zrates{display:grid;grid-template-columns:repeat(2,minmax(0,280px));gap:12px;margin:10px 0 22px}
.zrates label{border:1px solid var(--line);border-radius:14px;padding:12px 14px;background:var(--card);display:flex;flex-direction:column;gap:8px;font-size:13px;font-weight:600;color:var(--muted)}
.zrates span{display:flex;align-items:center;gap:8px;color:var(--fg)}.zrates input{width:90px;text-align:right;font-size:17px}
table.acc{width:100%}table.acc input{width:100%;box-sizing:border-box;min-width:0}
table.acc input.ico{width:42px;padding:6px 0;text-align:center;font-size:18px}
table.acc input.num{width:64px;text-align:right}table.acc td{padding:6px;vertical-align:top}
table.acc td.nm{min-width:220px}table.acc td.nm>input{font-weight:600}
table.acc .lk{display:flex;gap:6px;align-items:center;margin-top:4px}table.acc td.nm .lk input{font-size:12.5px;padding:4px 10px;height:30px;min-height:0}
table.acc .pk{display:flex;gap:4px;align-items:center}table.acc .pk input{width:58px}table.acc .pk i{font-style:normal;font-size:12px;color:var(--muted)}
table.acc .zg{display:flex;gap:4px}table.acc .zg input{width:54px}table.acc td small{display:block;font-size:11.5px;margin-top:3px}
table.acc .lowb input{color:var(--warn);font-weight:700}table.acc .outb input{color:var(--bad);font-weight:700}
table.acc .buy{text-decoration:none;font-size:18px}table.acc .del{white-space:nowrap;font-size:15px;color:var(--muted);display:inline-flex;gap:4px;align-items:center;padding-top:8px}
table.acc .del input{width:auto}
table.acc tr.new td{background:color-mix(in srgb,var(--accent) 5%,transparent)}
"""


def save_settings(store: Store, form: Any) -> None:
    """Teil von POST /settings: Stundensätze und Inventar."""
    for key in ("work_rate", "design_rate"):
        if key in form:
            v = parse_num(form.get(key))
            store.set_setting(key, None if v is None or v < 0 else f"{v:g}")
    ids = {k.split("__", 1)[1] for k in form if str(k).startswith("acc_name__")}
    for sid in ids:
        g = lambda f: form.get(f"acc_{f}__{sid}")  # noqa: E731
        name = str(g("name") or "").strip()
        if sid == "new":
            if not name:
                continue
            pq, pp = parse_num(g("pq")), parse_num(g("pp"))
            st = parse_num(g("st"))
            save_accessory(store, None, name=name[:80], icon=str(g("icon") or "").strip()[:4] or None,
                           grp=str(g("grp") or "").strip()[:40] or None, pack_qty=pq, pack_price=pp,
                           unit_price=round(pp / pq, 4) if pq and pp is not None else None,
                           stock=st if st is not None else (pq or 0), warn_at=parse_num(g("wa")),
                           url=str(g("url") or "").strip()[:500] or None)
            continue
        if not sid.isdigit():
            continue
        aid = int(sid)
        old = accessory(store, aid)
        if not old:
            continue
        if g("del"):
            delete_accessory(store, aid)
            continue
        pq, pp = parse_num(g("pq")), parse_num(g("pp"))
        f: dict[str, Any] = {"name": name[:80] or old["name"], "icon": str(g("icon") or "").strip()[:4] or None,
                             "grp": str(g("grp") or "").strip()[:40] or None, "pack_qty": pq, "pack_price": pp,
                             "warn_at": parse_num(g("wa")), "url": str(g("url") or "").strip()[:500] or None}
        if (pq, pp) != (old.get("pack_qty"), old.get("pack_price")) and pq and pp is not None:
            f["unit_price"] = round(pp / pq, 4)
        st = parse_num(g("st"))
        if st is not None and abs(st - round(old.get("stock") or 0, 2)) > 1e-9:
            f["stock"] = st
        save_accessory(store, aid, **f)
        aq = parse_num(g("aq"))
        if aq and aq > 0:
            restock(store, aid, aq, parse_num(g("ap")))


def low_stock(store: Store) -> list[dict[str, Any]]:
    """Zubehör, das bald leer ist (frei ≤ Warnschwelle) oder leer."""
    out = []
    for a in accessories(store):
        free = (a.get("stock") or 0) - (a.get("reserved") or 0)
        if (a.get("stock") or 0) <= 0 or (a.get("warn_at") is not None and free <= a["warn_at"]):
            out.append(a)
    return out


def csv_extras(store: Store, job: dict[str, Any]) -> str:
    xs = job_extras(store, job["id"])
    q = job.get("quantity") or 1
    return "; ".join(f'{x.get("name")} {fmt_input(round(need(x, q), 2))}' for x in xs)


def export_rows(store: Store, job: dict[str, Any]) -> list[tuple[str, float]]:
    """Positionen für den Bon: (Text, Betrag) für Zubehör und Arbeit."""
    q = job.get("quantity") or 1
    rows = []
    for x in job_extras(store, job["id"]):
        n = need(x, q)
        rows.append((f'{x.get("name")} × {fmt_input(round(n, 2))}', n * (x.get("unit_price") or 0)))
    return rows


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)
