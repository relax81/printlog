"""Kundenseite: Kennzahlen, Umsatz pro Monat, „Auf einen Blick“, letzte Aufträge, Filament und öfter gedruckte Modelle."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from . import bulkpay as _bulkpay
from . import extras as _extras
from . import orders as _orders
from .costing import parse_materials
from .db import Store, code_label
from .defects import no_charge
from .i18n import N_, t, tn
from .pages import amount_due, overpaid, price_base
from .stats import REVENUE_CSS, _buckets, _fmt_eur, bar_chart
from .ui import e, fmt_date, fmt_hours, fmt_input, fmt_num, layout

PERIODS = {"": N_("Gesamt"), "jahr": N_("Dieses Jahr"), "12m": N_("Letzte 12 Monate")}
LIST_N = 8
MODELS_N = 5
MATS_N = 6

CSS = """
.kh{display:flex;gap:12px 16px;align-items:center;flex-wrap:wrap;margin:6px 0 14px}.kh h1{margin:0;font-size:28px}.kh .sp{flex:1}
.kh .tag{padding:4px 12px;border-radius:999px;border:1px solid var(--line);font-size:13px;font-weight:600}.kh small{color:var(--muted);display:block}
.kh .btns{display:flex;gap:8px;flex-wrap:wrap}.kh .bpay{font-weight:700;color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,transparent)}
.lt .n.pos{color:var(--ok)}.lt .lnk{border:0;background:none;color:var(--muted);cursor:pointer;padding:2px 6px;min-height:0}.back{color:var(--accent);text-decoration:none}
.cg{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:12px;margin-top:12px}@media(max-width:900px){.cg{grid-template-columns:1fr}}
.cg .card h3{margin:0 0 10px}.cg .card h3+.muted{margin-top:-4px}
.mrow{display:grid;grid-template-columns:minmax(90px,150px) 1fr 64px;gap:10px;align-items:center;font-size:14px;margin:6px 0}
.mrow>span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mrow .b{height:12px;border-radius:6px;background:color-mix(in srgb,var(--fg) 7%,transparent);overflow:hidden}.mrow .b i{display:block;height:100%;border-radius:6px;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--fg) 18%,transparent)}
.mrow .n{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle;border:1px solid color-mix(in srgb,var(--fg) 25%,transparent)}
.facts{display:grid;grid-template-columns:1fr 1fr;gap:10px}.facts>div{border:1px solid var(--line);border-radius:10px;padding:8px 12px}
.facts span{display:block;font-size:12px;color:var(--muted)}.facts b{font-size:17px}.facts small{display:block;font-size:12px;color:var(--muted)}
table.lt{width:100%;border-collapse:collapse;font-size:14px}.lt td{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
.lt tr:last-child td{border-bottom:0}.lt .n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}.lt .d{white-space:nowrap;color:var(--muted)}
.lt a{color:var(--fg);text-decoration:none;font-weight:600}.lt a:hover{color:var(--accent)}.lt small{display:block;color:var(--muted);font-size:12px}
.cb{font-size:12px;padding:2px 8px;border-radius:999px;font-weight:600;white-space:nowrap}
.cb.ok{color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,transparent)}.cb.warn{color:var(--warn);background:color-mix(in srgb,var(--warn) 12%,transparent)}
.cb.run{color:var(--run);background:color-mix(in srgb,var(--run) 12%,transparent)}.cb.gift{color:#b07cf0;background:color-mix(in srgb,#b07cf0 12%,transparent)}
.cb.none{color:var(--muted);background:color-mix(in srgb,var(--fg) 6%,transparent)}
.more{display:inline-block;margin-top:8px;color:var(--accent);text-decoration:none;font-size:14px}
@media(max-width:700px){.kh .btns{width:100%}.kh .btns .btn{flex:1;text-align:center}.kh .seg{width:100%;display:flex}.kh .seg a{flex:1;text-align:center}
 .facts{gap:8px}.lt td{padding:8px 4px}}
"""


def _period(key: str) -> tuple[float, float]:
    now = datetime.now()
    if key == "jahr":
        return datetime(now.year, 1, 1).timestamp(), datetime(now.year + 1, 1, 1).timestamp()
    if key == "12m":
        _k, _l, since, until = _buckets("month", date.today())
        return since.timestamp(), until.timestamp()
    return 0.0, 4102444800.0  # bis 2100


def summarize(store: Store, cid: int, period: str = "") -> dict[str, Any]:
    """Alle Zahlen für die Kundenseite im Zeitraum (nach Druckstart)."""
    since, until = _period(period)
    rows = store.billing_rows("month", since, until, cid, nobill=True)
    prints = store.customer_prints(cid, since, until)
    tot: dict[str, float] = defaultdict(float)
    orders: list[dict[str, Any]] = []
    pay_days: list[float] = []
    oldest_open: float | None = None
    for j in rows:
        cost = (price_base(j) or 0) + _extras.work_total(j)
        qty = j.get("quantity") or 1
        tot["n"] += 1
        tot["pieces"] += qty
        o = {"job": j, "price": None, "state": "none"}
        orders.append(o)
        if j.get("customer_nobill"):
            tot["own_cost"] += cost
            o["state"] = "own"
            continue
        if j.get("state") == "done" and not no_charge(j):
            tot["qty"] += qty
            tot["delivered"] += min(j.get("delivered") or 0, qty)
        if j.get("gift"):
            tot["gift"] += cost
            tot["gift_n"] += 1
            o["state"] = "gift"
            continue
        if no_charge(j):
            o["state"] = "nocharge"
            continue
        due = amount_due(j)
        if due is None:
            continue
        paid = j.get("paid") or 0
        over = overpaid(j)
        price = due + over
        tot["revenue"] += price
        tot["paid"] += paid
        tot["open"] += due - paid if paid < due - 0.005 else 0
        tot["cost"] += cost
        tot["profit"] += price - cost
        tot["billed"] += 1
        o.update(price=price, over=over, open=max(due - paid, 0), paid=paid)
        if paid >= due - 0.005:
            o["state"] = "paid"
            ref = j.get("end_ts") or j.get("start_ts")
            if j.get("last_pay_ts") and ref:
                pay_days.append(max(0.0, (j["last_pay_ts"] - ref) / 86400))
        else:
            o["state"] = "part" if paid > 0 else "open"
            ts = j.get("end_ts") or j.get("start_ts")
            if ts and (oldest_open is None or ts < oldest_open):
                oldest_open = ts

    # Drucke: Stunden, Filament, Fehldrucke, Modelle
    mats: dict[tuple[str, str | None], float] = defaultdict(float)
    models: dict[str, dict[str, Any]] = {}
    for p in prints:
        if p.get("start_ts") and p.get("end_ts") and p["end_ts"] > p["start_ts"]:
            tot["hours"] += (p["end_ts"] - p["start_ts"]) / 3600
        g = p["filament_g"] if p.get("filament_g") is not None else p.get("weight_g")
        tot["grams"] += g or 0
        tot["prints"] += 1
        if p.get("is_scrap"):
            tot["scrap"] += 1
            if p.get("defect") == "customer":
                tot["scrap_customer"] += 1
        for m in parse_materials(p.get("materials")):
            mats[(m.get("name") or m.get("type") or "?", m.get("color"))] += m.get("grams") or 0
        key = f'm{p["model_id"]}' if p.get("model_id") else (p.get("task_name") or "").strip().lower()
        if not key:
            continue
        md = models.setdefault(key, {"name": p.get("model_name") or p.get("task_name") or "–", "n": 0, "cost": 0.0, "last": 0.0,
                                     "model_id": p.get("model_id")})
        md["n"] += 1
        md["cost"] += p.get("cost_total") or 0
        md["last"] = max(md["last"], p.get("start_ts") or 0)
    top_models = sorted((m for m in models.values() if m["n"] >= 2), key=lambda m: (-m["n"], -m["last"]))[:MODELS_N]
    top_mats = sorted(mats.items(), key=lambda kv: -kv[1])
    first = min((j["start_ts"] for j in rows if j.get("start_ts")), default=None)
    last = max((j["start_ts"] for j in rows if j.get("start_ts")), default=None)
    return {"tot": tot, "orders": orders, "pay_days": sum(pay_days) / len(pay_days) if pay_days else None,
            "oldest_open": oldest_open, "mats": top_mats, "models": top_models, "first": first, "last": last}


def _chart(store: Store, cid: int) -> str:
    """Umsatz pro Monat (letzte 12 Monate): Selbstkosten + Gewinn, wie in der Statistik."""
    keys, labels, since, until = _buckets("month", date.today())
    vals: dict[tuple[str, str], float] = defaultdict(float)
    for j in store.billing_rows("month", since.timestamp(), until.timestamp(), cid):
        if j.get("gift") or no_charge(j):
            continue
        due = amount_due(j)
        if due is None:
            continue
        price = due + overpaid(j)
        cost = (price_base(j) or 0) + _extras.work_total(j)
        vals[(j["bucket"], "cost")] += min(cost, price)
        vals[(j["bucket"], "profit")] += max(price - cost, 0)
        if price < cost:
            vals[(j["bucket"], "loss")] += cost - price
    series = [("cost", t("Selbstkosten"), "color-mix(in srgb,var(--fg) 28%,transparent)"), ("profit", t("Gewinn"), "var(--ok)"),
              ("loss", t("Verlust"), "var(--bad)")]
    if not any(v for (_, k), v in vals.items() if k == "loss"):
        series = series[:2]
    if not any(vals.values()):
        return f'<p class="muted" style="margin:0">{t("In den letzten 12 Monaten nichts berechnet.")}</p>'
    return bar_chart(keys, labels, series, vals, _fmt_eur, 180)


def _badge(o: dict[str, Any]) -> str:
    st = o["state"]
    if st == "paid":
        txt = t("bezahlt") + (f' +{fmt_num(o["over"], 2)}' if o.get("over") else "")
        return f'<span class="cb ok">{e(txt)}</span>'
    if st == "part":
        return f'<span class="cb run">{e(t("{v} offen", v=fmt_num(o["open"], 2, "€")))}</span>'
    if st == "open":
        return f'<span class="cb warn">{t("offen")}</span>'
    if st == "gift":
        return f'<span class="cb gift">{t("🎁 verschenkt")}</span>'
    if st == "nocharge":
        return f'<span class="cb none">{t("nicht berechnet")}</span>'
    if st == "own":
        return f'<span class="cb none">{t("Eigenbedarf")}</span>'
    return f'<span class="cb none">{t("Preis fehlt noch")}</span>'


def page_customer(store: Store, cid: int, period: str | None = None) -> str | None:
    c = store.customer(cid)
    if not c:
        return None
    period = period if period in PERIODS else ""
    d = summarize(store, cid, period)
    tot = d["tot"]
    nobill = bool(c.get("no_billing"))

    sub = [x for x in (c.get("contact"), c.get("note")) if x]
    if d["first"]:
        sub.append(t("erster Druck {d}", d=fmt_date(d["first"], "date_s")))
    if d["last"]:
        sub.append(t("letzter Druck {d}", d=fmt_date(d["last"], "date_s")))
    tags = ""
    if c.get("margin_pct"):
        tags += f'<span class="tag calc">{t("+{p} % Marge", p=fmt_input(c["margin_pct"]))}</span>'
    if nobill:
        tags += f'<span class="tag">{t("Keine Abrechnung")}</span>'
    seg = "".join(f'<a href="customer?id={cid}{"&period=" + k if k else ""}" class="{"on" if k == period else ""}">{t(v)}</a>'
                  for k, v in PERIODS.items())
    n_open = 0 if nobill else len(_bulkpay.open_items(store, cid))
    credits = [] if nobill else store.credits(cid)
    balance = round(sum(x["amount"] for x in credits), 2)
    pay_btn = _bulkpay.open_button(cid, "btn bpay") if n_open or balance > 0.005 else ""
    head = (f'<a class="back" href="customers">← {t("alle Kunden")}</a>'
            f'<div class="kh"><div><h1>{e(c["name"])}</h1><small>{" · ".join(e(x) for x in sub)}</small></div>{tags}<span class="sp"></span>'
            f'<span class="seg">{seg}</span><span class="btns">{pay_btn}<a class="btn" href="./?customer={cid}">{t("Drucke")}</a>'
            f'<a class="btn" href="customers?edit={cid}">{t("Bearbeiten")}</a></span></div>')

    n = int(tot["n"])
    pieces = int(tot["pieces"])
    kp = (f'<div><span>{t("Drucke")}</span><b>{n}</b><small>{tn(pieces, "{n} Stück", "{n} Stück")}</small></div>')
    if nobill:
        kp += f'<div><span>{t("Kosten")}</span><b>{fmt_num(tot["own_cost"], 2, "€")}</b><small>{t("Eigenbedarf, keine Abrechnung")}</small></div>'
    else:
        avg = tot["revenue"] / tot["billed"] if tot["billed"] else None
        pct = t("{p} % auf die Selbstkosten", p=fmt_num(tot["profit"] / tot["cost"] * 100, 0)) if tot["cost"] > 0 else "–"
        kp += (f'<div><span>{t("Umsatz")}</span><b>{fmt_num(tot["revenue"], 2, "€")}</b>'
               f'<small>{t("Ø {v} pro Druck", v=fmt_num(avg, 2, "€")) if avg is not None else "–"}</small></div>'
               f'<div><span>{t("Bezahlt")}</span><b>{fmt_num(tot["paid"], 2, "€")}</b>'
               f'<small>{t("{v} offen", v=fmt_num(tot["open"], 2, "€")) if tot["open"] >= 0.005 else t("nichts offen")}</small></div>'
               f'<div class="calc"><span>{t("Selbstkosten")}</span><b>{fmt_num(tot["cost"], 2, "€")}</b><small>{t("Druck, Zubehör, Arbeit")}</small></div>'
               f'<div class="calc {"pos" if tot["profit"] >= 0 else "neg"}"><span>{t("Gewinn")}</span><b>{fmt_num(tot["profit"], 2, "€")}</b><small>{pct}</small></div>'
               + (f'<div class="pos"><span>{t("Guthaben")}</span><b>{fmt_num(balance, 2, "€")}</b>'
                  f'<small><a href="#guthaben">{t("aus zu viel Gezahltem")}</a></small></div>' if credits else "") +
               f'<div><span>{t("Verschenkt")}</span><b>{tn(int(tot["gift_n"]), "{n} Druck", "{n} Drucke") if tot["gift_n"] else "–"}</b>'
               f'<small class="calc">{t("Kosten {v}", v=fmt_num(tot["gift"], 2, "€")) if tot["gift_n"] else ""}</small></div>')

    facts = [(t("Druckstunden"), fmt_hours(tot["hours"]), tn(int(tot["prints"]), "{n} Druck", "{n} Drucke")),
             (t("Filament"), fmt_num(tot["grams"] / 1000, 2, "kg") if tot["grams"] >= 1000 else fmt_num(tot["grams"], 0, "g"), "")]
    if not nobill:
        facts.append((t("Ø Tage bis Zahlung"), tn(round(d["pay_days"]), "{n} Tag", "{n} Tage") if d["pay_days"] is not None else "–",
                      t("ab Druckende bis zur letzten Zahlung")))
        if d["oldest_open"]:
            days = max(0, int((datetime.now().timestamp() - d["oldest_open"]) / 86400))
            facts.append((t("Ältester offener Betrag"), tn(days, "seit {n} Tag", "seit {n} Tagen"), ""))
        else:
            facts.append((t("Ältester offener Betrag"), "–", t("nichts offen")))
    sc = int(tot["scrap"])
    facts.append((t("Fehldrucke"), str(sc), t("davon {n} Kundenwunsch", n=int(tot["scrap_customer"])) if tot["scrap_customer"] else ""))
    if not nobill:
        facts.append((t("Übergeben"), t("{d} von {n} Stück", d=int(tot["delivered"]), n=int(tot["qty"])), ""))
    fc = "".join(f'<div><span>{e(a)}</span><b>{e(b)}</b>{f"<small>{e(s)}</small>" if s else ""}</div>' for a, b, s in facts)

    lrows = ""
    for o in d["orders"][:LIST_N]:
        j = o["job"]
        ref = code_label(j.get("code"))
        lrows += (f'<tr><td class="d">{fmt_date(j.get("start_ts"), "dm")}</td>'
                  f'<td><a href="job?id={j["share_of"] if j.get("kind") == "share" and j.get("share_of") else j["id"]}">{e(j.get("task_name") or t("Druck"))}</a>'
                  f'{f"<small>{e(ref)}</small>" if ref else ""}</td>'
                  f'<td class="n">{fmt_num(o["price"], 2, "€") if o["price"] is not None else "–"}</td><td class="n">{_badge(o)}</td></tr>')
    more = (f'<a class="more" href="./?customer={cid}">{t("Alle {n} Drucke in der Druckliste →", n=n)}</a>' if n > LIST_N else "")
    orders = (f'<table class="lt"><tbody>{lrows}</tbody></table>{more}' if lrows
              else f'<p class="muted" style="margin:0">{t("Im Zeitraum keine Drucke.")}</p>')

    mats = d["mats"][:MATS_N]
    mx = max((g for _, g in mats), default=0) or 1
    mat = "".join(f'<div class="mrow"><span><span class="dot" style="background:{e(col or "var(--muted)")}"></span>{e(name)}</span>'
                  f'<span class="b"><i style="width:{g / mx * 100:.0f}%;background:{e(col or "var(--muted)")}"></i></span>'
                  f'<span class="n">{fmt_num(g, 0, "g")}</span></div>' for (name, col), g in mats) \
        or f'<p class="muted" style="margin:0">{t("Keine Filamentdaten.")}</p>'
    mrows = "".join(f'<tr><td>{f"<a href=./?model={m["model_id"]}>{e(m["name"])}</a>" if m["model_id"] else e(m["name"])}'
                    f'<small>{t("zuletzt {d}", d=fmt_date(m["last"], "date_s"))}</small></td>'
                    f'<td class="n">{m["n"]}×</td><td class="n calc">{fmt_num(m["cost"], 2, "€")}</td></tr>' for m in d["models"])
    models = (f'<table class="lt"><tbody>{mrows}</tbody></table>' if mrows
              else f'<p class="muted" style="margin:0">{t("Noch nichts mehrfach gedruckt.")}</p>')

    chart_card = ("" if nobill else f'<div class="card calc"><h3>{t("Umsatz pro Monat")}</h3>'
                  f'<p class="muted" style="font-size:13px">{t("letzte 12 Monate")}</p>{_chart(store, cid)}</div>')
    oft = _orders.often_card(store, cid) or (f'<div class="card"><h3>{t("Öfter gedruckt")}</h3>'
                                              f'<p class="muted" style="font-size:13px">{t("gleiches Modell oder gleicher Name, mit Druckkosten")}</p>{models}</div>')
    credit_card = ""
    if credits:
        crows = "".join(
            f'<tr><td class="d">{fmt_date(x["ts"], "dm")}</td><td>{e(x.get("note") or "")}</td>'
            f'<td class="n {"pos" if x["amount"] > 0 else ""}">{"+" if x["amount"] > 0 else ""}{fmt_num(x["amount"], 2, "€")}</td>'
            f'<td class="n"><form method="post" action="api/credits/{x["id"]}/delete" style="margin:0" '
            f'onsubmit="return confirm({e(json.dumps(t("Diesen Eintrag löschen?")))})"><button class="lnk" title="{e(t("Löschen"))}">✕</button></form></td></tr>'
            for x in credits)
        credit_card = (f'<div class="card" id="guthaben"><h3>{t("Guthaben")}: {fmt_num(balance, 2, "€")}</h3>'
                       f'<p class="muted" style="font-size:13px">{t("Zu viel Gezahltes, das bei der nächsten Zahlung mitverwendet werden kann.")}</p>'
                       f'<table class="lt"><tbody>{crows}</tbody></table></div>')
    body = (f'<style>{REVENUE_CSS}{CSS}{_orders.CSS}{_bulkpay.CSS}</style>{head}<div class="rvk">{kp}</div>'
            f'<div class="cg" style="align-items:start">{_orders.customer_card(store, cid)}{oft}</div><div class="cg">'
            f'{chart_card}<div class="card"><h3>{t("Auf einen Blick")}</h3><div class="facts">{fc}</div></div>'
            f'<div class="card"><h3>{t("Letzte Drucke")}</h3>{orders}</div>'
            f'<div class="card"><h3>{t("Filament")}</h3>{mat}</div>{credit_card}</div>'
            f'<p class="muted" style="font-size:13px;margin:12px 0 0">{t("Nach Druckstart. Preis = Endpreis oder Kosten + Kundenmarge; wurde mehr bezahlt, zählt das Gezahlte.")}</p>')
    return layout(c["name"], "customers", body + _orders.again_dialog(store) + _bulkpay.dialog(), _orders.AGAIN_JS + _bulkpay.BULKPAY_JS)
