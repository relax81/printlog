"""Stückzahl aufteilen: Anteile je Kunde, Vorrat, Abgeben und Ausbuchen (Oberfläche).

Daten und Regeln stehen in ``Store`` (db.py, Abschnitt „Aufteilen“). Dieses Modul wird am Ende von pages.py
geladen und nutzt dessen Helfer (Preise, Badges, Layout).
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from .db import Store, code_label
from .i18n import N_, t, tn
from .pages import (
    ACT_CSS,
    ACT_JS,
    DETAIL_CSS,
    DETAIL_JS,
    KPICK_CSS,
    MOS_CSS,
    PROJ_CSS,
    SHARE_JS,
    _deal_box,
    _pcs,
    _pid,
    _share_button,
    _thumb,
    amount_due,
    billable,
    customer_badges,
    customer_picker,
    mosaic,
)
from .ui import e, fmt_date, fmt_input, fmt_num, layout

# Farben wie in der Kunden-Schnellauswahl (KPICK_JS), damit ein Kunde überall gleich aussieht
COLORS = ("#7dd3fc", "#fda4af", "#fcd34d", "#a5b4fc", "#fdba74", "#86efac", "#f0abfc", "#5eead4", "#fca5a5", "#c4b5fd")

# Preis beim Abgeben aus dem Vorrat (Einstellung „stock_price“)
STOCK_PRICE = {
    "cost": N_("Kosten pro Stück + Kundenmarge"),
    "first": N_("Gleicher Stückpreis wie beim ersten Kunden-Anteil"),
    "ask": N_("Jedes Mal selbst eingeben (Vorschlag vorausgefüllt)"),
}
OUT_REASONS = (N_("selbst verwendet"), N_("kaputt"), N_("verloren"), N_("Sonstiges"))


def color(name: str | None) -> str:
    h = 0
    for ch in name or "?":
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return COLORS[h % len(COLORS)]


def initials(name: str | None) -> str:
    parts = [p for p in "".join(c if c.isalnum() or c.isspace() or c == "-" else "" for c in (name or "?")).replace("-", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def attach(store: Store, jobs: list[dict[str, Any]], kids: dict[int, list[dict[str, Any]]] | None = None) -> None:
    """Hängt an aufgeteilte Drucke/Projekte ihre Anteile (_shares) und den Vorrat (_stock)."""
    split = [j for j in jobs if j.get("split") and j.get("kind") != "share"]
    if not split:
        return
    shares = store.shares([j["id"] for j in split])
    kids = kids or {}
    missing = [j["id"] for j in split if j.get("kind") == "project" and j["id"] not in kids]
    if missing:
        kids = {**kids, **store.children(missing)}
    for j in split:
        j["_shares"] = shares.get(j["id"], [])
        j["_kids"] = kids.get(j["id"], []) if j.get("kind") == "project" else []
        j["_stock"] = store.stock(j, j["_shares"], j["_kids"] if j.get("kind") == "project" else None)


def _ensure(store: Store | None, j: dict[str, Any]) -> None:
    if "_shares" not in j and store is not None:
        attach(store, [j])


def unit_word(j: dict[str, Any], n: float) -> str:
    """„4 Stück“ bzw. beim Projekt „2 Sätze“."""
    n = int(n) if float(n).is_integer() else n
    if j.get("kind") == "project":
        return tn(n, "{n} Satz", "{n} Sätze") if isinstance(n, int) else t("{n} Sätze", n=fmt_input(n))
    return _pcs(n)


def stock_text(j: dict[str, Any], kids: list[dict[str, Any]] | None = None, short: bool = False) -> str:
    """Vorrat als Text: „4 Stück“, „2 Sätze + 1× Kopf“ (kurz: „2 Sätze + 1 Teil“)."""
    st = j.get("_stock") or {"units": 0, "loose": {}}
    names = {k["id"]: k.get("task_name") or "?" for k in kids or j.get("_kids") or []}
    parts = []
    if st["units"] or not st["loose"]:
        parts.append(unit_word(j, max(st["units"], 0)))
    if short:
        n = round(sum(v for v in st["loose"].values() if v > 0), 2)
        if n:
            parts.append(tn(int(n), "{n} Teil", "{n} Teile") if float(n).is_integer() else t("{n} Teile", n=fmt_input(n)))
        return " + ".join(parts)
    for kid, n in st["loose"].items():
        if n > 0:
            parts.append(f'{fmt_input(round(n, 2))}× {names.get(kid, "?")}')
    return " + ".join(parts)


def _groups(j: dict[str, Any]) -> list[tuple[str, int | None, float]]:
    """Anteile mit Kunde zusammengefasst: (Name, Kunden-ID, Stück) in der Reihenfolge der Aufteilung."""
    out: dict[int, list[Any]] = {}
    for s in j.get("_shares") or []:
        if s.get("customer_id") and not s.get("share_part") and s.get("share_kind") != "project":
            g = out.setdefault(s["customer_id"], [s.get("customer_name") or "?", s["customer_id"], 0])
            g[2] += s.get("quantity") or 0
    return [tuple(g) for g in out.values()]  # type: ignore[misc]


def split_chips(j: dict[str, Any], compact: bool = False) -> str:
    """Kunden-Chips eines aufgeteilten Drucks für Liste und Karten: „● Tom 2 · ● Anna 4 · 📦 Vorrat 4“."""
    chips = "".join(f'<span class="shc" title="{e(t("Aufteilung ansehen"))}"><i style="background:{color(n)}"></i>'
                    f'{e(n)} <b>{fmt_input(q)}</b></span>' for n, _, q in _groups(j))
    chips += "".join(f'<span class="shc" title="{e(t("Im Projekt „{name}“", name=s.get("project_name") or ""))}">📦 '
                     f'{e(s.get("project_name") or t("Projekt"))} <b>{fmt_input(s.get("quantity"))}</b></span>'
                     for s in j.get("_shares") or [] if s.get("share_kind") == "project")
    parts = sum(1 for s in j.get("_shares") or [] if s.get("customer_id") and s.get("share_part"))
    if parts:
        chips += f'<span class="shc">{tn(parts, "+{n} Einzelteil", "+{n} Einzelteile")}</span>'
    st = j.get("_stock") or {}
    if st.get("units", 0) > 0 or st.get("loose"):
        chips += f'<span class="shc v">📦 {t("Vorrat")} <b>{e(stock_text(j, short=True))}</b></span>'
    elif st.get("units", 0) < 0:
        chips += f'<span class="shc bad">⚠ {t("mehr vergeben als gedruckt")}</span>'
    if not chips:
        chips = f'<span class="shc">✂ {t("aufgeteilt")}</span>'
    return f'<span class="shcs">{chips}</span>'


def edit_field() -> str:
    """Bearbeiten-Tab eines aufgeteilten Drucks: Kunden stehen in der Aufteilung."""
    return (f'<label class="c3">{t("Kunde")}<span class="ro">✂ {t("aufgeteilt")} · '
            f'<a href="#split" class="spjump">{t("Aufteilung ändern")}</a></span></label>')


def split_bar(j: dict[str, Any], cls: str = "spbar") -> str:
    total = (j.get("_stock") or {}).get("total") or j.get("quantity") or 1
    segs = []
    for s in j.get("_shares") or []:
        q = s.get("quantity") or 0
        if s.get("share_part"):
            per = (j.get("_stock") or {}).get("per", {}).get(s["share_part"]) or 1
            q = q / per / max(len((j.get("_stock") or {}).get("per") or {}), 1)
        if q <= 0:
            continue
        who = s.get("project_name") if s.get("share_kind") == "project" else s.get("customer_name") if s.get("customer_id") else None
        style = f"background:{color(who)}" if who else ""
        segs.append(f'<i class="{"" if who else "out"}" style="flex:{q:.4f};{style}"></i>')
    st = j.get("_stock") or {}
    rest = max(st.get("units", 0), 0)
    if rest:
        segs.append(f'<i class="v" style="flex:{rest:.4f}"></i>')
    if not segs:
        return ""
    return f'<div class="{cls}" title="{e(t("{n} insgesamt", n=unit_word(j, total)))}">{"".join(segs)}</div>'


def split_prices(j: dict[str, Any]) -> str:
    """Spalte „Verkaufspreise“ der Liste: eine Zeile je Kunde, dazu der Vorrat zum Kostenwert."""
    out = ""
    for s in j.get("_shares") or []:
        if not s.get("customer_id") or s.get("customer_nobill") or s.get("share_kind") == "project":
            continue
        due = 0.0 if s.get("gift") else amount_due(s)
        q = s.get("quantity") or 1
        small = (t("🎁 verschenkt") if s.get("gift") else t("{n}× Teil", n=fmt_input(q)) if s.get("share_part")
                 else unit_word(j, q))
        out += (f'<span class="cu">👤 {e(s.get("customer_name") or "?")}</span>'
                f'<b class="cu">{fmt_num(due, 2, "€")}</b><small>{e(small)}</small>')
    st = j.get("_stock") or {}
    value = stock_value(j)
    if st.get("units", 0) > 0 or st.get("loose"):
        out += (f'<span class="calc">📦 {t("Vorrat")}</span><b class="calc">{fmt_num(value, 2, "€")}</b>'
                f'<small class="calc">{e(stock_text(j, short=True))}</small>')
    return f'<div class="jp sp">{out}</div>' if out else '<div class="jp muted">–</div>'


def unit_cost(j: dict[str, Any], part: int | None = None, kids: list[dict[str, Any]] | None = None) -> float | None:
    """Kosten für ein Stück (Druck), einen Satz (Projekt) oder ein Einzelteil eines Projekts."""
    if part:
        k = next((k for k in kids or j.get("_kids") or [] if k["id"] == part), None)
        if not k or k.get("cost_total") is None:
            return None
        return k["cost_total"] / (k.get("quantity") or 1)
    base = j.get("bill_base") if j.get("kind") == "project" and j.get("bill_base") is not None else j.get("cost_total")
    return None if base is None else base / (j.get("quantity") or 1)


def stock_value(j: dict[str, Any], kids: list[dict[str, Any]] | None = None) -> float | None:
    st = j.get("_stock") or {}
    u = unit_cost(j)
    if u is None:
        return None
    v = max(st.get("units", 0), 0) * u
    for kid, n in (st.get("loose") or {}).items():
        v += n * (unit_cost(j, kid, kids) or 0)
    return v


def split_badges(j: dict[str, Any], cls: str = "bdg") -> str:
    """Zahlung und Übergabe aller Anteile zusammengefasst (antippen führt zur Aufteilung)."""
    pay = [s for s in j.get("_shares") or [] if billable(s) and not s.get("gift") and amount_due(s) is not None]
    deliver = [s for s in j.get("_shares") or [] if billable(s)]
    gifts = sum(1 for s in j.get("_shares") or [] if s.get("gift"))
    out = ""
    if pay:
        ok = sum(1 for s in pay if (s.get("paid") or 0) >= (amount_due(s) or 0) - 0.005)
        tone = "ok" if ok == len(pay) else "run" if ok else "warn"
        label = t("bezahlt") if ok == len(pay) else t("{a} von {b} bezahlt", a=ok, b=len(pay))
        out += f'<span class="{cls} st-{tone}">💶 {label}</span>'
    if deliver and j.get("state") == "done":
        n = sum(s.get("quantity") or 0 for s in deliver)
        d = sum(min(s.get("delivered") or 0, s.get("quantity") or 0) for s in deliver)
        tone = "ok" if d >= n else "run" if d else "warn"
        label = t("übergeben") if d >= n else t("{d} von {n} übergeben", d=fmt_input(d), n=fmt_input(n))
        out += f'<span class="{cls} st-{tone}">📦 {label}</span>'
    if gifts:
        out += f'<span class="{cls} st-gift">🎁 {gifts}</span>'
    return out


# ---- Detailseite: Kasten „Aufteilung“ und die beiden Fenster ---------------------------

def _price_src(s: dict[str, Any]) -> str:
    if s.get("gift"):
        return t("🎁 verschenkt")
    if s.get("customer_nobill"):
        return t("keine Abrechnung")
    if s.get("final_price") is not None:
        return t("Endpreis")
    q = s.get("quantity") or 1
    due = amount_due(s)
    return t("{v}/Stk.", v=fmt_num(due / q, 2)) if due is not None and q > 1 else t("Kosten + Marge")


def _margins(store: Store) -> dict[str, float]:
    return {str(c["id"]): c.get("margin_pct") or 0 for c in store.customers()}


def first_unit_price(j: dict[str, Any], part: int | None) -> float | None:
    """Stückpreis des ersten Kunden-Anteils (gleiches Teil), für die Einstellung „first“."""
    for s in j.get("_shares") or []:
        if s.get("customer_id") and (s.get("share_part") or None) == (part or None) and not s.get("gift"):
            due = amount_due(s)
            if due is not None:
                return due / (s.get("quantity") or 1)
    return None


def give_config(store: Store, j: dict[str, Any], kids: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Daten für das Fenster „Aus Vorrat abgeben“."""
    st = j.get("_stock") or {}
    kids = kids if kids is not None else j.get("_kids") or []
    items = []
    if max(st.get("units", 0), 0) or j.get("kind") != "project":
        items.append({"part": None, "label": t("Ganzer Satz") if j.get("kind") == "project" else t("Stück"),
                      "max": max(st.get("units", 0), 0), "unit": unit_cost(j), "first": first_unit_price(j, None)})
    if j.get("kind") == "project":
        for k in kids:
            if k["id"] not in (st.get("per") or {}):
                continue
            n = (st.get("loose") or {}).get(k["id"], 0) + max(st.get("units", 0), 0) * st["per"][k["id"]]
            if n >= 1 - 1e-6:
                items.append({"part": k["id"], "label": t("Einzelteil: {name}", name=k.get("task_name") or "?"),
                              "max": math.floor(n + 1e-6), "unit": unit_cost(j, k["id"], kids), "first": first_unit_price(j, k["id"])})
    return {"id": j["id"], "title": j.get("task_name") or "", "items": items, "sets": j.get("kind") == "project",
            "mode": store.settings().get("stock_price") or "cost", "margins": _margins(store)}


def split_config(store: Store, j: dict[str, Any]) -> dict[str, Any]:
    """Daten für das Fenster „Aufteilen“: bearbeitbare Anteile, was fest vergeben ist, Kosten pro Stück."""
    shares = j.get("_shares") or []
    rows, fixed = [], 0.0
    busy = store.busy_ids([s["id"] for s in shares])
    for s in shares:
        if s.get("share_kind") == "project":
            rows.append({"sid": s["id"], "pid": s.get("project_id"), "name": s.get("project_name") or t("Projekt"),
                         "qty": s.get("quantity") or 1, "min": 1, "locked": False})
        elif s.get("customer_id") and not s.get("share_part") and s.get("share_kind") != "out":
            due = 0.0 if s.get("gift") else amount_due(s)
            rows.append({"sid": s["id"], "cid": s["customer_id"], "name": s.get("customer_name") or "?",
                         "qty": s.get("quantity") or 1, "min": max(1, s.get("delivered") or 0), "locked": s["id"] in busy,
                         "price": due, "fixed": s.get("final_price") is not None or bool(s.get("gift")),
                         "src": _price_src(s), "from": t("aus dem Vorrat") if s.get("share_kind") == "stock" else ""})
    st = store.stock(j, [s for s in shares if not any(r["sid"] == s["id"] for r in rows)])
    fixed = st["total"] - st["units"]
    if not j.get("split") and j.get("customer_id"):
        rows.append({"sid": None, "cid": j["customer_id"], "name": j.get("customer_name") or "?",
                     "qty": j.get("quantity") or 1, "min": 1, "locked": False})
    projects = [] if j.get("kind") == "project" else [
        {"id": p["id"], "name": p.get("task_name") or "–", "code": code_label(p.get("code")), "customer": p.get("customer_name") or ""}
        for p in store.projects(200)]
    return {"id": j["id"], "title": j.get("task_name") or "", "total": st["total"], "fixed": fixed, "projects": projects,
            "unit": unit_cost(j), "project": j.get("kind") == "project", "rows": rows,
            "split": bool(j.get("split")), "margins": _margins(store)}


def split_button(j: dict[str, Any]) -> str:
    """„✂ Aufteilen“ im Kopf eines noch nicht aufgeteilten Drucks/Projekts."""
    if j.get("split") or j.get("project_id") or (j.get("is_scrap") and j.get("kind") != "project"):
        return ""
    return (f'<button type="button" class="bdg spopen" title="{e(t("Stückzahl auf mehrere Kunden und den Vorrat aufteilen"))}">'
            f'✂ {t("Aufteilen")}</button>')


def dialogs(store: Store, j: dict[str, Any], kids: list[dict[str, Any]] | None = None) -> str:
    """Daten für beide Fenster (das Markup baut SPLIT_JS)."""
    data = {"split": split_config(store, j), "give": give_config(store, j, kids) if j.get("split") else None}
    raw = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/json" id="spdata">{raw}</script>'


def split_box(store: Store, j: dict[str, Any], kids: list[dict[str, Any]] | None = None) -> str:
    """Kasten „Aufteilung“ in der Übersicht eines aufgeteilten Drucks/Projekts."""
    _ensure(store, j)
    kids = kids if kids is not None else j.get("_kids") or []
    st = j["_stock"]
    names = {k["id"]: k.get("task_name") or "?" for k in kids}
    u = unit_cost(j)
    word = t("pro Satz") if j.get("kind") == "project" else t("pro Stück")
    head = (f'<div class="boxh"><h3 id="split">{t("Aufteilung")}</h3><span class="muted">{unit_word(j, st["total"])}'
            f'{" · " + fmt_num(u, 2, "€") + " " + t("Kosten") + " " + word if u is not None else ""}</span><span class="sp"></span>'
            f'<button type="button" class="btn sm spopen">✂ {t("Aufteilung ändern")}</button></div>')
    rows = []
    for s in j["_shares"]:
        q = s.get("quantity") or 0
        what = unit_word(j, q) if not s.get("share_part") else f'{fmt_input(q)}× {e(names.get(s["share_part"], "?"))}'
        when = (f'<small>{t("aus dem Vorrat")} · {fmt_date(s["start_ts"], "date_s")}</small>' if s.get("share_kind") == "stock" else "")
        if s.get("share_kind") == "project":
            pn = s.get("project_name") or t("Projekt")
            who = (f'<span class="av" style="background:{color(pn)}">📦</span><span class="nm"><b>{e(pn)}</b>'
                   f'<small>{t("im Projekt")} · {e(code_label(s.get("project_code")))}</small></span>')
            price = f'<b>–</b><small>{t("über das Projekt")}</small>'
            state = ""
            link = f'<a class="btn sm" href="job?id={s["project_id"]}">{t("Zum Projekt →")}</a>'
        elif s.get("customer_id"):
            who = (f'<span class="av" style="background:{color(s.get("customer_name"))}">{e(initials(s.get("customer_name")))}</span>'
                   f'<span class="nm"><b>{e(s.get("customer_name") or "?")}</b>{when}</span>')
            due = 0.0 if s.get("gift") else amount_due(s)
            price = (f'<b>{fmt_num(due, 2, "€")}</b><small class="calc">{e(_price_src(s))}</small>' if not s.get("customer_nobill")
                     else f'<b>–</b><small>{t("keine Abrechnung")}</small>')
            state = customer_badges(s, "bdg st") or ""
            link = f'<a class="btn sm" href="job?id={s["id"]}" title="{e(t("Preis, Zahlungen und Übergaben dieses Kunden"))}">{t("Abrechnung →")}</a>'
        else:
            who = (f'<span class="av out">↘</span><span class="nm"><b>{t("Ausgebucht")}</b>'
                   f'<small>{e(t(s.get("job_note") or "") or t("ohne Kunde"))} · {fmt_date(s["start_ts"], "date_s")}</small></span>')
            price = f'<b class="calc">{fmt_num(s.get("cost_total"), 2, "€")}</b><small class="calc">{t("Kosten")}</small>'
            state = ""
            link = (f'<form method="post" action="api/shares/{s["id"]}/delete" class="spdel"><button class="x" title="{e(t("Zurück in den Vorrat"))}">×</button></form>')
        rows.append(f'<div class="spr"><div class="who">{who}</div><div class="q">{what}</div>'
                    f'<div class="c calc">{fmt_num(s.get("cost_total"), 2, "€")}</div><div class="p">{price}</div>'
                    f'<div class="st">{state}</div><div class="l">{link}</div></div>')
    value = stock_value(j, kids)
    give = (f'<button type="button" class="btn sm primary spgive">↗ {t("Aus Vorrat abgeben …")}</button>'
            if st["units"] > 0 or any(n >= 1 - 1e-6 for n in st["loose"].values()) else "")
    loose = "".join(f'<small>+ {fmt_input(round(n, 2))}× {e(names.get(k, "?"))}</small>' for k, n in st["loose"].items() if n > 0)
    warn = (f'<div class="spwarn">⚠ {t("Es ist mehr vergeben, als gedruckt wurde. Bitte die Stückzahl oder die Aufteilung prüfen.")}</div>'
            if st["units"] < 0 else "")
    rows.append(f'<div class="spr v"><div class="who"><span class="av v">📦</span><span class="nm"><b>{t("Vorrat")}</b>{loose}</span></div>'
                f'<div class="q">{unit_word(j, max(st["units"], 0))}</div><div class="c calc">{fmt_num(value, 2, "€")}</div>'
                f'<div class="p"><b class="calc">{fmt_num(value, 2, "€")}</b><small class="calc">{t("Wert (Kosten)")}</small></div>'
                f'<div class="st">{give}</div><div class="l"></div></div>')
    hd = (f'<div class="spr h"><div>{t("An")}</div><div class="q">{t("Sätze") if j.get("kind") == "project" else t("Stück")}</div>'
          f'<div class="c calc">{t("Kosten")}</div><div class="p">{t("Preis")}</div><div class="st">{t("Zahlung · Übergabe")}</div><div></div></div>')
    note = t("Jeder Kunde hat seinen eigenen Preis, eigene Zahlungen und Übergaben. Stück für ein Projekt zählen dort mit "
             "(anteilige Kosten), abgerechnet wird über das Projekt. Was übrig bleibt, liegt im Vorrat.")
    return (f'<div class="box spbox">{head}{split_bar(j, "spbar big")}{warn}<div class="sptab">{hd}{"".join(rows)}</div>'
            f'<p class="muted spnote">{note}</p></div>')


def todo_note(store: Store, j: dict[str, Any], cache: dict[int, dict[str, Any] | None]) -> str:
    """Untertitel in „Offen“ für einen Anteil: „✂ 4 von 10 Stück“."""
    pid = j.get("share_of")
    if pid not in cache:
        cache[pid] = store.get(pid) if pid else None
    p = cache[pid]
    if not p:
        return ""
    q = j.get("quantity") or 1
    if j.get("share_part"):
        return f'<span class="spflag">✂ {t("Einzelteil")} · {fmt_input(q)}×</span>'
    return f'<span class="spflag">✂ {t("{q} von {n}", q=fmt_input(q), n=unit_word(p, p.get("quantity") or 1))}</span>'


# ---- Anteil (eigene Seite: Preis, Zahlungen, Übergaben eines Kunden) -------------------

def page_share(store: Store, s: dict[str, Any], saved: str | None) -> str:
    parent = store.get(s["share_of"]) if s.get("share_of") else None
    customers = store.customers()
    q = s.get("quantity") or 1
    title = s.get("task_name") or t("Anteil")
    if parent:
        attach(store, [parent])
        what = unit_word(parent, q) if not s.get("share_part") else _pcs(q)
        of = t("{q} von {n}", q=what, n=unit_word(parent, parent.get("quantity") or 1)) if not s.get("share_part") else what
        back = (f'<div class="pjbanner"><span class="spico">✂</span><div class="t2"><b>{t("Anteil von „{name}“", name=e(parent.get("task_name") or "–"))} · '
                f'{e(code_label(parent.get("code")))}</b><small>{e(of)} · {t("Kosten anteilig")}</small></div>'
                f'<a class="btn primary" href="job?id={parent["id"]}#split">{t("Zur Aufteilung →")}</a></div>')
    else:
        back = ""
    big = (f'<img class="big" src="images/{e(s["image"])}" alt="">' if s.get("image")
           else f'<div class="big noimg">{t("kein Vorschaubild")}</div>')
    badges = ""
    if s.get("customer_id"):
        badges += f'<span class="bdg">👤 {e(s.get("customer_name") or "?")}</span>'
    if s.get("code"):
        badges += f'<span class="bdg"># {e(code_label(s["code"]))}</span>'
    badges += f'<span class="bdg">{_pcs(q)}</span>'
    if s.get("share_kind") == "stock":
        badges += f'<span class="bdg">📦 {t("aus dem Vorrat")} · {fmt_date(s["start_ts"], "date_s")}</span>'
    total = s.get("cost_total")
    kpis = (f'<div class="kp main"><span>{t("Kosten anteilig")}</span><b>{fmt_num(total, 2, "€")}</b>'
            f'<small>{t("{v} pro Stück", v=fmt_num(total / q, 2, "€")) if total is not None and q else ""}</small></div>'
            f'<div class="kp"><span>{t("🧵 Filament")}</span><b>{fmt_num(s.get("filament_g"), 1, "g")}</b><small>{t("anteilig")}</small></div>')
    if s.get("project_id"):
        pj = store.get(s["project_id"]) or {}
        deal = (f'<div class="box"><p style="margin:0">{t("Diese Stück gehören zum Projekt „{name}“. Preis, Zahlung und Übergabe laufen über das Projekt.", name=e(pj.get("task_name") or ""))} '
                f'<a href="job?id={s["project_id"]}">{t("Zum Projekt →")}</a></p></div>')
    elif billable(s):
        deal = _deal_box(s, store.handovers(s["id"]), store.payments(s["id"]))
    else:
        deal = f'<div class="box"><p class="muted" style="margin:0">{t("Ohne Abrechnung (kein Kunde oder „Keine Abrechnung“).")}</p></div>'
    banner = f'<div class="banner">{t("Gespeichert.")}</div>' if saved else ""
    body = f"""<div class="jd">{banner}<div class="jtop"><a class="back" href="{f'job?id={parent["id"]}#split' if parent else './'}">{t("← zurück")}</a>
<div class="jact">{_share_button(s)}</div></div>
{back}<section class="hero sh2"><div>{big}</div>
<div><h2 class="jtitle">{e(title)}</h2><div class="badges">{badges}</div><div class="kps calc">{kpis}</div></div></section>
<div class="tabs" role="tablist"><button type="button" data-tab="ab" class="on">{t("Abrechnung")}</button></div>
<section class="pane on" data-pane="ab">{deal}</section></div>"""
    css = f"<style>{MOS_CSS}{DETAIL_CSS}{PROJ_CSS}{ACT_CSS}{KPICK_CSS}{SPLIT_CSS}.hero.sh2{{grid-template-columns:220px minmax(0,1fr)}}" \
          f"@media (max-width:760px){{.hero.sh2{{grid-template-columns:1fr}}}}</style>"
    return layout(title, "jobs", css + body + customer_picker(customers), ACT_JS + DETAIL_JS + SHARE_JS)


# ---- Ansicht „Vorrat“ in der Druckliste ---------------------------------------------

def stock_count(store: Store) -> int:
    return len(store.stock_items())


def stock_view(store: Store, f: dict[str, str | None]) -> str:
    items = store.stock_items()
    q = (f.get("q") or "").strip().lower()
    if q:
        items = [j for j in items if q in (j.get("task_name") or "").lower() or q in code_label(j.get("code")).lower()]
    if not items:
        empty = t("Nichts im Vorrat. Teile einen Druck mit mehreren Stück über „✂ Aufteilen“ auf, dann landet der Rest hier.")
        return f'<div class="oempty">{empty}</div>'
    pieces = sets = parts = 0.0
    value = 0.0
    for j in items:
        st = j["_stock"]
        if j.get("kind") == "project":
            sets += max(st["units"], 0)
            parts += sum(n for n in st["loose"].values() if n > 0)
        else:
            pieces += max(st["units"], 0)
        value += stock_value(j, j["_kids"]) or 0
    mix = [x for x in (_pcs(int(pieces)) if pieces else "", tn(int(sets), "{n} Satz", "{n} Sätze") if sets else "",
                       tn(int(round(parts)), "{n} Einzelteil", "{n} Einzelteile") if parts else "") if x]
    oldest = min(items, key=lambda j: j.get("start_ts") or 0)
    days = (datetime.now().date() - datetime.fromtimestamp(oldest.get("start_ts") or 0).date()).days if oldest.get("start_ts") else 0
    given = store.stock_given(datetime.now().timestamp() - 90 * 86400)
    kpis = (f'<div class="kpis okpis"><div class="kpi"><span>📦 {t("Im Vorrat")}</span><b>{tn(len(items), "{n} Modell", "{n} Modelle")}</b>'
            f'<small>{e(" · ".join(mix))}</small></div>'
            f'<div class="kpi run"><span>{t("Wert (Kosten)")}</span><b class="calc">{fmt_num(value, 2, "€")}</b>'
            f'<small>{t("zum Selbstkostenpreis")}</small></div>'
            f'<div class="kpi"><span>{t("Aus dem Vorrat abgegeben")}</span><b>{tn(given, "{n}×", "{n}×")}</b><small>{t("letzte 90 Tage")}</small></div>'
            f'<div class="kpi"><span>{t("Ältester Vorrat")}</span><b>{tn(days, "{n} Tag", "{n} Tage")}</b><small>{e(oldest.get("task_name") or "")}</small></div></div>')
    rows = []
    for j in items:
        st = j["_stock"]
        given_to = ", ".join(f'{fmt_input(q2)} {n}' for n, _, q2 in _groups(j))
        sub = [fmt_date(j["start_ts"], "dm") if j.get("start_ts") else "–",
               t("{n} gedruckt", n=unit_word(j, st["total"]))]
        if given_to:
            sub.append(t("an {who}", who=given_to))
        if st["loose"]:
            sub.append(t("lose: {x}", x=", ".join(f'{fmt_input(round(n, 2))}× {k.get("task_name") or "?"}'
                                                    for k in j["_kids"] for kid, n in st["loose"].items() if kid == k["id"] and n > 0)))
        pic = mosaic(j["_kids"]) if j.get("kind") == "project" else _thumb(j)
        u = unit_cost(j)
        cfg = json.dumps(give_config(store, j, j["_kids"]), ensure_ascii=False)
        rows.append(f'<div class="or vr">{pic}<div class="oname"><b><a href="job?id={j["id"]}#split">{e(j.get("task_name") or "–")}</a>{_pid(j)}</b>'
                    f'<span>{e(" · ".join(sub))}</span></div>'
                    f'<div class="vc"><b>{e(stock_text(j, short=True))}</b><span>{t("im Vorrat")}</span></div>'
                    f'<div class="vc calc"><b>{fmt_num(u, 2, "€")}</b><span>{t("Kosten pro Satz") if j.get("kind") == "project" else t("Kosten/Stk.")}</span></div>'
                    f'<div class="vc calc"><b>{fmt_num(stock_value(j, j["_kids"]), 2, "€")}</b><span>{t("Wert")}</span></div>'
                    f'<div class="oact"><button type="button" class="btn primary spgive" data-give="{e(cfg)}">↗ {t("Abgeben …")}</button></div></div>')
    hint = t("Im Vorrat liegt, was beim Aufteilen keinem Kunden zugeordnet wurde. „Abgeben“ legt einen neuen Anteil für den Kunden an "
             "(mit heutigem Datum), „Ausbuchen“ ohne Kunde für selbst Verwendetes oder Kaputtes.")
    return f'{kpis}<section class="og vg">{"".join(rows)}</section><p class="ohint">{hint}</p>'


SPLIT_CSS = """
.shc{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:999px;font-size:13px;font-weight:700;white-space:nowrap;
 border:1px solid var(--line);background:color-mix(in srgb,var(--fg) 5%,transparent);color:var(--fg);text-decoration:none;max-width:100%}
.shc i{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}.shc b{color:var(--muted);font-weight:700}
.shcs{display:contents}.mcard .shcs,.gcard .shcs{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.shc.v{border-style:dashed}a.bdg{text-decoration:none}.shc.bad{color:var(--bad);border-color:var(--bad)}
.spbar{display:flex;gap:2px;height:8px;border-radius:5px;overflow:hidden;margin:6px 0 0;max-width:320px}
.spbar.big{height:14px;max-width:none;margin:2px 0 14px}.spbar i{display:block;min-width:3px}
.spbar i.out{background:color-mix(in srgb,var(--fg) 22%,transparent)}
.spbar i.v,.av.v{background:repeating-linear-gradient(135deg,#94a3b8 0 5px,color-mix(in srgb,#94a3b8 50%,transparent) 5px 10px)}
.jp.sp .cu{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bdg.spb,.bdg.spopen{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 55%,var(--line));cursor:pointer;font:inherit;font-size:14px;font-weight:600}
.spbox{margin-bottom:16px}.spbox .boxh h3{scroll-margin-top:80px}
.sptab{display:grid}
.spr{display:grid;grid-template-columns:minmax(0,1.6fr) 90px 80px 110px minmax(0,1.6fr) 124px;gap:12px;align-items:center;padding:10px 4px;border-bottom:1px solid var(--line);font-size:15px}
.spr:last-child{border-bottom:0}
.spr.h{font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:700;padding-top:0}
.spr .q,.spr .c,.spr .p{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.spr .q{font-weight:700}
.spr .p b{display:block}.spr small{display:block;color:var(--muted);font-size:12.5px;font-weight:500}
.spr .who{display:flex;align-items:center;gap:10px;min-width:0}.spr .nm{min-width:0}.spr .nm b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.spr .av{width:32px;height:32px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#111;flex:none}
.spr .av.out{background:color-mix(in srgb,var(--fg) 14%,transparent);color:var(--fg)}
.spr .st{display:flex;flex-wrap:wrap;gap:6px}.spr .st .bdg{font-size:13px;padding:3px 10px}
.spr .l{display:flex;justify-content:flex-end}.spr.v{background:color-mix(in srgb,#94a3b8 7%,transparent);border-radius:10px}
.spr form{margin:0}.spr .x{border:0;background:none;color:var(--muted);font-size:18px;cursor:pointer;padding:4px 8px;min-height:0}
.spwarn{color:var(--bad);font-weight:600;margin:0 0 10px}.spnote{font-size:13px;margin:10px 0 0}
.spflag{display:inline-block;margin-left:6px;font-size:12px;font-weight:700;color:var(--accent);border:1px solid color-mix(in srgb,var(--accent) 50%,var(--line));border-radius:999px;padding:0 8px}
.spico{font-size:26px;width:54px;text-align:center}
.vg .or{grid-template-columns:48px minmax(0,1fr) 150px 120px 110px 150px}.vc b{display:block;font-size:16px}.vc span{font-size:12.5px;color:var(--muted)}
.dlgbg.sp{position:fixed;inset:0;height:100%;height:100dvh;box-sizing:border-box;background:#0009;z-index:60;display:flex;align-items:center;justify-content:center;padding:16px}
.dlgbg.sp[hidden]{display:none}
.spdlg{background:var(--card);border:1px solid var(--line);border-radius:18px;box-shadow:0 30px 80px #000a;padding:18px 20px;width:min(560px,100%);max-height:calc(100vh - 32px);max-height:calc(100dvh - 32px);overflow:auto;overscroll-behavior:contain;box-sizing:border-box;font-size:15px}
.spdlg h3{margin:0 0 2px;font-size:19px;text-transform:none;letter-spacing:0;color:var(--fg)}.spdlg .sub{color:var(--muted);font-size:13.5px;margin:0 0 12px}
.spdlg .row{display:flex;align-items:center;gap:10px;padding:9px 2px;border-bottom:1px solid var(--line)}
.spdlg .row .nm{flex:1;min-width:0;font-weight:700}.spdlg .row .nm small{display:block;color:var(--muted);font-weight:500;font-size:12.5px}
.spdlg .av{width:30px;height:30px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#111;flex:none}
.spdlg .spp{width:112px;text-align:right;font-weight:700;white-space:nowrap;font-variant-numeric:tabular-nums}.spdlg .spp small{display:block;color:var(--muted);font-weight:500;font-size:12px}
.spdlg .x{border:0;background:none;color:var(--muted);font-size:18px;cursor:pointer;padding:4px 6px;min-height:0;width:28px}.spdlg .x:disabled{opacity:.3;cursor:default}
.spdlg .add{display:block;width:100%;text-align:left;border:0;background:none;color:var(--accent);font-weight:700;padding:12px 2px;border-bottom:1px solid var(--line);cursor:pointer}
.spdlg .row.v .nm b{color:var(--fg)}.spdlg .pform{display:flex;gap:8px;flex-wrap:wrap;padding:10px 2px;border-bottom:1px solid var(--line)}
.spdlg .pform select,.spdlg .pform input{flex:1 1 220px;min-width:0}.spdlg .row.v.bad .spp,.spdlg .row.v.bad .vq{color:var(--bad)}
.spdlg .vq{min-width:56px;text-align:center;font-weight:800}
.spdlg .stp{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:10px;overflow:hidden;flex:none}
.spdlg .stp button{border:0;background:none;color:var(--accent);font-weight:800;font-size:18px;width:34px;height:34px;min-height:0;padding:0;cursor:pointer}
.spdlg .stp button:disabled{opacity:.35;cursor:default}
.spdlg .stp input{width:46px;text-align:center;border:0;border-left:1px solid var(--line);border-right:1px solid var(--line);border-radius:0;background:var(--bg);font-weight:800;font-size:16px;height:34px;padding:0;-moz-appearance:textfield}
.spdlg .stp input::-webkit-inner-spin-button{-webkit-appearance:none}
.spdlg .ft{display:flex;gap:10px;align-items:center;margin-top:16px;flex-wrap:wrap}.spdlg .ft .sp{flex:1}
.spdlg .err{color:var(--bad);font-weight:600;margin:10px 0 0}
.spdlg .lnk{border:0;background:none;color:var(--muted);text-decoration:underline;cursor:pointer;padding:0;min-height:0;font-size:13.5px}
.spdlg label.f{display:grid;gap:5px;font-size:13px;font-weight:700;color:var(--muted);margin:12px 0 0}
.spdlg label.f select,.spdlg label.f input{font-size:15px}
.spdlg .who{display:flex;gap:8px;flex-wrap:wrap}.spdlg .who button{flex:1}
.spdlg .who button.on{border-color:var(--accent);color:var(--accent);background:color-mix(in srgb,var(--accent) 10%,transparent)}
.spdlg .ck{display:flex;align-items:center;gap:8px;margin:12px 0 0;font-size:14px;font-weight:600;cursor:pointer}.spdlg .ck input{width:18px;height:18px;margin:0}
.spdlg .hint{font-size:12.5px;color:var(--muted);font-weight:500}
.spdlg [hidden]{display:none!important}.spdlg .ln{display:flex;gap:12px;align-items:start;flex-wrap:wrap}.spdlg .ln>*{flex:1}
@media (max-width:760px){
 .spr{grid-template-columns:minmax(0,1fr) auto auto;gap:4px 10px;padding:10px 2px}
 .spr.h{display:none}.spr .c{display:none}.spr .st{grid-column:1/-1;padding-left:42px}.spr .l{grid-column:1/-1;justify-content:flex-start;padding-left:42px}
 .spr .l:empty,.spr .st:empty{display:none}.spr.v .st{padding-left:0}.spr.v .st .btn{width:100%;justify-content:center}
 .vg .or{grid-template-columns:40px minmax(0,1fr) auto;gap:6px 10px}.vg .vc:nth-of-type(2),.vg .vc:nth-of-type(3){display:none}.vg .oact{grid-column:1/-1}
 .spdlg .row{flex-wrap:wrap;row-gap:8px}.spdlg .row .nm{flex:1 1 calc(100% - 44px)}.spdlg .row .stp{margin-left:40px}.spdlg .row .spp{margin-left:auto}
 .spdlg .row.v .vq{margin-left:40px;text-align:left}
 .dlgbg.sp{align-items:flex-end;padding:0}.spdlg{border-radius:20px 20px 0 0;width:100%;max-height:92vh;max-height:92dvh;padding-bottom:calc(18px + env(safe-area-inset-bottom))}
}
"""

SPLIT_JS = r"""
// Aufteilen und Abgeben: zwei kleine Fenster, Kunden kommen aus der Schnellauswahl (KP)
(function(){
 const raw=document.getElementById('spdata');const D=raw?JSON.parse(raw.textContent):{};
 const COL=['#7dd3fc','#fda4af','#fcd34d','#a5b4fc','#fdba74','#86efac','#f0abfc','#5eead4','#fca5a5','#c4b5fd'];
 const col=n=>{let h=0;for(const ch of n)h=(h*31+ch.charCodeAt(0))>>>0;return COL[h%COL.length]};
 const ini=n=>n.replace(/[^\p{L}\p{N}\s-]/gu,'').split(/[\s-]+/).filter(Boolean).slice(0,2).map(x=>x[0]).join('').toUpperCase()||'?';
 const eur=v=>v==null||isNaN(v)?'–':v.toLocaleString(document.documentElement.lang||'de',{minimumFractionDigits:2,maximumFractionDigits:2})+' €';
 const el=(tag,cls,txt)=>{const x=document.createElement(tag);if(cls)x.className=cls;if(txt!=null)x.textContent=txt;return x};
 function shell(){const bg=el('div','dlgbg sp');bg.hidden=true;const d=el('div','spdlg');d.setAttribute('role','dialog');bg.append(d);document.body.append(bg);
   bg.addEventListener('click',ev=>{if(ev.target===bg)bg.hidden=true});document.addEventListener('keydown',ev=>{if(ev.key==='Escape'&&!document.getElementById('kpop')?.offsetParent)bg.hidden=true});return [bg,d]}
 function stepper(val,min,max,on){const w=el('span','stp');const m=el('button',null,'−'),p=el('button',null,'+');m.type=p.type='button';
   const i=el('input');i.type='number';i.inputMode='numeric';i.value=val;
   const sync=fire=>{const old=parseInt(i.value);let v=old||min;v=Math.max(min,max!=null?Math.min(max,v):v);i.value=v;
     m.disabled=v<=min;p.disabled=max!=null&&v>=max;if(fire!==false||v!==old)on(v)};
   m.onclick=()=>{i.value=(parseInt(i.value)||min)-1;sync()};p.onclick=()=>{i.value=(parseInt(i.value)||min)+1;sync()};i.onchange=()=>sync();
   w.append(m,i,p);w.set=mx=>{max=mx;sync(false)};sync(false);return w}
 function avatar(name){const a=el('span','av',ini(name));a.style.background=col(name);return a}
 async function post(url,fd,err){try{const r=await fetch(url,{method:'POST',body:fd});const d=await r.json().catch(()=>({}));
   if(r.ok&&d.ok){location.hash='';location.reload();return}err.textContent=d.error||_t("Das hat nicht geklappt.")}catch(_){err.textContent=_t("Das hat nicht geklappt.")}}

 // ---- Aufteilen ----
 const S=D.split;let sBg,sD;
 function openSplit(){if(!S)return;if(!sBg)[sBg,sD]=shell();sD.textContent='';
   const rows=S.rows.map(r=>({...r}));const project=S.project;const word=project?_t("Sätze"):_t("Stück");
   sD.append(el('h3',null,_t("„{name}“ aufteilen").replace('{name}',S.title)));
   sD.append(el('p','sub',(project?_t("{n} Sätze"):_t("{n} Stück")).replace('{n}',S.total)+(S.unit!=null?' · '+eur(S.unit)+' '+(project?_t("Kosten pro Satz"):_t("Kosten pro Stück")):'')));
   const list=el('div');sD.append(list);const add=el('button','add','👤 '+_t("＋ Kunde hinzufügen"));add.type='button';sD.append(add);
   const padd=el('button','add','📦 '+_t("＋ Projekt hinzufügen"));padd.type='button';padd.hidden=!S.projects;sD.append(padd);
   const pf=el('div','pform');pf.hidden=true;const psel=el('select');const pnew=el('input');pnew.placeholder=_t("Name des neuen Projekts");pnew.maxLength=120;pnew.hidden=true;
   const pok=el('button','btn sm primary',_t("Hinzufügen"));pok.type='button';pf.append(psel,pnew,pok);sD.append(pf);
   (S.projects||[]).forEach(p=>{const o=el('option',null,[p.code,p.name,p.customer].filter(Boolean).join(' · '));o.value=p.id;psel.append(o)});
   {const o=el('option',null,_t("＋ Neues Projekt …"));o.value='new';psel.append(o)}
   psel.onchange=()=>{pnew.hidden=psel.value!=='new';if(!pnew.hidden)pnew.focus()};
   padd.onclick=()=>{pf.hidden=!pf.hidden;psel.onchange()};
   pok.onclick=()=>{if(psel.value==='new'){const n=pnew.value.trim();if(!n){pnew.focus();return}rows.push({sid:null,pid:null,newProject:n,name:n,qty:1,min:1})}
     else{const p=(S.projects||[]).find(x=>String(x.id)===psel.value);if(!p)return;const have=rows.find(r=>String(r.pid)===psel.value);
       if(have)have.qty++;else rows.push({sid:null,pid:p.id,name:p.name,qty:1,min:1})}
     pf.hidden=true;pnew.value='';draw()};
   const vrow=el('div','row v');const vav=el('span','av v','📦');const vnm=el('span','nm');vnm.append(el('b',null,_t("Vorrat (Rest)")),el('small',null,_t("wird automatisch berechnet")));
   const vq=el('span','vq'),vpr=el('span','spp');vrow.append(vav,vnm,vq,vpr,el('span','x'));sD.append(vrow);
   if(S.fixed>0)sD.append(el('p','hint',_t("Schon fest vergeben (Einzelteile, Ausbuchungen): {n}").replace('{n}',S.fixed)));
   const err=el('p','err');sD.append(err);
   const ft=el('div','ft');const off=el('button','lnk',_t("Aufteilung aufheben"));off.type='button';off.hidden=!S.split;
   const cancel=el('button','btn',_t("Abbrechen")),save=el('button','btn primary',S.split?_t("Speichern"):_t("Aufteilen"));cancel.type=save.type='button';
   ft.append(off,el('span','sp'),cancel,save);sD.append(ft);
   const isP=r=>r.pid!=null||!!r.newProject;
   function price(r){if(S.unit==null)return null;const m=r.cid!=null?(S.margins[String(r.cid)]||0):0;return S.unit*r.qty*(1+m/100)}
   function rest(){return S.total-S.fixed-rows.reduce((a,r)=>a+r.qty,0)}
   function upd(){const v=rest();vq.textContent=v;vpr.textContent='';if(S.unit!=null){vpr.append(document.createTextNode(eur(Math.max(v,0)*S.unit)),el('small',null,_t("Kosten")))}
     vrow.classList.toggle('bad',v<0);save.disabled=v<0;err.textContent=v<0?_t("Mehr vergeben als vorhanden."):'';
     rows.forEach(r=>{if(r.prEl&&isP(r)){r.prEl.textContent='';r.prEl.append(document.createTextNode(S.unit!=null?eur(S.unit*r.qty):'–'),el('small',null,_t("Kosten ins Projekt")))}
       else if(r.prEl){r.prEl.textContent='';const fx=r.sid&&r.fixed;
         r.prEl.append(document.createTextNode(eur(fx?r.price:price(r))),el('small',null,r.sid?r.src:_t("Vorschlag")))}
       if(r.st)r.st.set(r.qty+Math.max(v,0))})}
   function draw(){list.textContent='';rows.forEach((r,i)=>{const row=el('div','row');const nm=el('span','nm');nm.append(el('b',null,r.name));
       const m=r.cid!=null?S.margins[String(r.cid)]:null;
       nm.append(el('small',null,isP(r)?(r.newProject?_t("neues Projekt"):_t("Projekt")):r.cid==null?_t("neuer Kunde"):(r.from?r.from+' · ':'')+(m?'+'+m+' % '+_t("Kundenmarge"):_t("ohne Marge"))));
       r.st=stepper(r.qty,r.min||1,null,v=>{r.qty=v;upd()});r.prEl=el('span','spp');
       const x=el('button','x','✕');x.type='button';x.disabled=!!r.locked;x.title=r.locked?_t("Hat schon Zahlungen oder Übergaben"):_t("Entfernen");
       x.onclick=()=>{rows.splice(i,1);draw()};const av=avatar(r.name);if(isP(r))av.textContent='📦';row.append(av,nm,r.st,r.prEl,x);list.append(row)});upd()}
   add.onclick=()=>KP.open(add,{ids:[],title:S.title,prefix:_t("Anteil für")+' ',onpick:fd=>{const id=fd.get('customer_id'),nn=fd.get('new_name');
       if(nn){rows.push({sid:null,cid:null,name:nn,newName:nn,qty:1,min:1});draw();return}
       if(!id)return;const c=(KP.custs||[]).find(x=>String(x.id)===String(id));
       const have=rows.find(r=>String(r.cid)===String(id)&&!r.sid&&!isP(r));if(have){have.qty++;draw();return}
       rows.push({sid:null,cid:+id,name:c?c.name:'?',qty:1,min:1});draw()}});
   cancel.onclick=()=>sBg.hidden=true;
   save.onclick=()=>{const fd=new FormData();fd.append('rows',JSON.stringify(rows.map(r=>isP(r)?{sid:r.sid,project_id:r.pid,new_project:r.newProject||null,qty:r.qty}:{sid:r.sid,customer_id:r.cid,new_name:r.newName||null,qty:r.qty})));
     save.disabled=true;post('api/jobs/'+S.id+'/split',fd,err).finally(()=>save.disabled=false)};
   off.onclick=()=>{if(!confirm(_t("Aufteilung aufheben? Der erste Kunde bekommt den Druck wieder ganz, die anderen Anteile werden entfernt.")))return;
     const fd=new FormData();fd.append('off','1');post('api/jobs/'+S.id+'/split',fd,err)};
   draw();sBg.hidden=false}
 document.querySelectorAll('.spopen').forEach(b=>b.onclick=ev=>{ev.preventDefault();openSplit()});
 function fromHash(){if(location.hash!=='#split'||!S)return;if(!S.split){openSplit();return}
   if(typeof tab==='function')tab('ov',false);const h=document.getElementById('split');if(h)setTimeout(()=>h.scrollIntoView({block:'start'}),30)}
 fromHash();addEventListener('hashchange',fromHash);
 document.querySelectorAll('.badges .shc').forEach(c=>{c.style.cursor='pointer';c.onclick=()=>{history.replaceState(null,'','#split');fromHash()}});
 document.querySelectorAll('.spjump').forEach(a=>a.onclick=ev=>{ev.preventDefault();history.replaceState(null,'','#split');fromHash()});
 document.querySelectorAll('form.spdel').forEach(f=>f.onsubmit=()=>confirm(_t("Ausbuchung zurücknehmen? Die Stück gehen zurück in den Vorrat.")));

 // ---- Aus dem Vorrat abgeben / ausbuchen ----
 let gBg,gD;
 function openGive(G){if(!G||!G.items.length)return;if(!gBg)[gBg,gD]=shell();gD.textContent='';
   let who=null,out=false,item=G.items[0],qty=1,touched=false;
   gD.append(el('h3',null,_t("„{name}“ aus dem Vorrat").replace('{name}',G.title)));
   const sub=el('p','sub');gD.append(sub);
   const sw=el('div','who');const bCust=el('button','btn on','👤 '+_t("Kunde wählen …")),bOut=el('button','btn','↘ '+_t("Ohne Kunde ausbuchen"));
   bCust.type=bOut.type='button';sw.append(bCust,bOut);gD.append(sw);
   const reason=el('select');[_t("selbst verwendet"),_t("kaputt"),_t("verloren"),_t("Sonstiges")].forEach((r,i)=>{const o=el('option',null,r);o.value=['selbst verwendet','kaputt','verloren','Sonstiges'][i];reason.append(o)});
   const lr=el('label','f',_t("Grund"));lr.append(reason);lr.hidden=true;gD.append(lr);
   let sel=null;if(G.items.length>1){sel=el('select');G.items.forEach((it,i)=>{const o=el('option',null,it.label+' ('+_t("{n} da").replace('{n}',it.max)+')');o.value=i;sel.append(o)});
     const ls=el('label','f',_t("Was"));ls.append(sel);gD.append(ls)}
   const ln=el('div','ln');const lq=el('label','f',_t("Anzahl"));const st=stepper(1,1,item.max,v=>{qty=v;calc()});lq.append(st);
   const lp=el('label','f',_t("Preis €"));const pin=el('input');pin.inputMode='decimal';lp.append(pin);const ph=el('span','hint');lp.append(ph);
   ln.append(lq,lp);gD.append(ln);
   const ck=el('label','ck');const cb=el('input');cb.type='checkbox';cb.checked=true;ck.append(cb,document.createTextNode(' '+_t("gleich als übergeben eintragen")));gD.append(ck);
   const err=el('p','err');gD.append(err);
   const ft=el('div','ft');const cancel=el('button','btn',_t("Abbrechen")),go=el('button','btn primary',_t("Abgeben"));cancel.type=go.type='button';
   ft.append(el('span','sp'),cancel,go);gD.append(ft);
   function suggest(){if(item.unit==null)return null;const m=who&&who.id!=null?(G.margins[String(who.id)]||0):0;
     if(G.mode==='first'&&item.first!=null)return item.first*qty;return item.unit*qty*(1+m/100)}
   function calc(){sub.textContent=_t("{n} da").replace('{n}',item.max)+(item.unit!=null?' · '+eur(item.unit)+' '+(item.part?_t("Kosten pro Teil"):G.sets?_t("Kosten pro Satz"):_t("Kosten pro Stück")):'');
     const s=suggest();if(out){lp.hidden=true}else{lp.hidden=false;
       if(G.mode==='cost'){pin.placeholder=s!=null?s.toFixed(2).replace('.',','):'';if(!touched)pin.value='';ph.textContent=_t("leer = Kosten + Kundenmarge")}
       else{if(!touched)pin.value=s!=null?s.toFixed(2).replace('.',','):'';ph.textContent=G.mode==='first'&&item.first!=null?_t("wie beim ersten Kunden"):_t("Vorschlag, änderbar")}}
     go.textContent=out?_t("Ausbuchen"):_t("Abgeben");go.disabled=!out&&!who;ck.hidden=out}
   pin.oninput=()=>touched=pin.value!=='';
   if(sel)sel.onchange=()=>{item=G.items[+sel.value];st.set(item.max);touched=false;calc()};
   bCust.onclick=()=>{out=false;bCust.classList.add('on');bOut.classList.remove('on');lr.hidden=true;
     KP.open(bCust,{ids:[],title:G.title,prefix:_t("Abgeben an")+' ',onpick:fd=>{const id=fd.get('customer_id'),nn=fd.get('new_name');
       if(nn)who={id:null,name:nn,newName:nn};else if(id){const c=(KP.custs||[]).find(x=>String(x.id)===String(id));who={id:+id,name:c?c.name:'?'}}
       if(who)bCust.textContent='👤 '+who.name;calc()}});calc()};
   bOut.onclick=()=>{out=true;bOut.classList.add('on');bCust.classList.remove('on');lr.hidden=false;calc()};
   cancel.onclick=()=>gBg.hidden=true;
   go.onclick=()=>{const fd=new FormData();fd.append('qty',qty);if(item.part)fd.append('part',item.part);
     if(out)fd.append('note',reason.value);else{if(who.id!=null)fd.append('customer_id',who.id);else fd.append('new_name',who.newName);
       if(pin.value.trim())fd.append('price',pin.value.trim());if(cb.checked)fd.append('handed','1')}
     go.disabled=true;post('api/jobs/'+G.id+'/give',fd,err).finally(()=>go.disabled=false)};
   calc();gBg.hidden=false}
 document.addEventListener('click',ev=>{const b=ev.target.closest('.spgive');if(!b)return;ev.preventDefault();ev.stopPropagation();
   openGive(b.dataset.give?JSON.parse(b.dataset.give):D.give)});
})();
"""
