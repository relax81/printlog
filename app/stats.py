"""Statistikseite (Tag/Woche/Monat) und Monatskalender."""

from __future__ import annotations

import calendar as cal
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from .costing import parse_materials
from .db import Store
from .defects import DEFECTS, no_charge
from .i18n import N_, t, tn
from .ui import e, fmt_date, fmt_hours, fmt_num, layout, printer_names

PERIODS = {
    "day": (N_("Tag"), 30),      # letzte 30 Tage
    "week": (N_("Woche"), 12),   # letzte 12 Wochen
    "month": (N_("Monat"), 12),  # letzte 12 Monate
}
MATERIAL_ORDER = ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC"]
# Kosten-Grafik: gute Drucke nach Art, Fehldrucke als eigener (roter) Anteil
COST_SERIES = [("good_energy", N_("Strom")), ("good_filament", N_("Filament")), ("good_printer", N_("Drucker")),
               ("scrap_cost", N_("Fehldrucke"))]
WEEKDAYS = [N_("Mo"), N_("Di"), N_("Mi"), N_("Do"), N_("Fr"), N_("Sa"), N_("So")]
MONTHS = [N_("Januar"), N_("Februar"), N_("März"), N_("April"), N_("Mai"), N_("Juni"), N_("Juli"), N_("August"),
          N_("September"), N_("Oktober"), N_("November"), N_("Dezember")]


def _series_color(i: int) -> str:
    return f"var(--s{i % 8 + 1})"


def _buckets(period: str, today: date) -> tuple[list[str], list[str], datetime, datetime]:
    """Schlüssel (wie SQLite strftime), Beschriftungen und Zeitraum."""
    n = PERIODS[period][1]
    keys: list[str] = []
    labels: list[str] = []
    if period == "day":
        start = today - timedelta(days=n - 1)
        for i in range(n):
            d = start + timedelta(days=i)
            keys.append(d.strftime("%Y-%m-%d"))
            labels.append(fmt_date(datetime(d.year, d.month, d.day), "dm"))
        since = datetime.combine(start, datetime.min.time())
        until = datetime.combine(today + timedelta(days=1), datetime.min.time())
    elif period == "week":
        monday = today - timedelta(days=today.weekday())
        start = monday - timedelta(weeks=n - 1)
        for i in range(n):
            d = start + timedelta(weeks=i)
            keys.append(d.strftime("%Y-W%W"))
            labels.append(fmt_date(datetime(d.year, d.month, d.day), "dm"))
        since = datetime.combine(start, datetime.min.time())
        until = datetime.combine(monday + timedelta(weeks=1), datetime.min.time())
    else:
        y, m = today.year, today.month
        months = []
        for _ in range(n):
            months.append((y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        months.reverse()
        for y, m in months:
            keys.append(f"{y}-{m:02d}")
            labels.append(f"{m:02d}/{str(y)[2:]}")
        since = datetime(months[0][0], months[0][1], 1)
        ny, nm = (today.year + (today.month == 12), today.month % 12 + 1)
        until = datetime(ny, nm, 1)
    return keys, labels, since, until


def _nice_step(v: float) -> float:
    """Runder Achsenschritt, sodass etwa vier Teilstriche den Höchstwert abdecken."""
    import math

    if v <= 0:
        return 0.25
    raw = v / 4
    exp = 10 ** math.floor(math.log10(raw))
    for f in (1, 2, 5, 10):
        if raw <= f * exp:
            return f * exp
    return 10 * exp


def bar_chart(keys: list[str], labels: list[str], series: list[tuple[str, str, str]],
              values: dict[tuple[str, str], float], fmt: Any, height: int = 200) -> str:
    """Gestapeltes Säulendiagramm als SVG mit Tooltip pro Säule."""
    totals = [sum(values.get((k, s[0]), 0) for s in series) for k in keys]
    step_v = _nice_step(max(totals) if totals else 0)
    ticks = max(1, math.ceil((max(totals) if totals else 0) / step_v - 1e-9))
    vmax = step_v * ticks
    left, bottom, top = 50, 22, 8
    width = 640
    plot_w = width - left - 6
    plot_h = height - bottom - top
    n = len(keys)
    slot = plot_w / max(n, 1)
    bar_w = max(3.0, min(28.0, slot * 0.62))
    parts = []
    for i in range(ticks + 1):
        v = step_v * i
        y = top + plot_h - plot_h * i / ticks
        parts.append(f'<line x1="{left}" x2="{width - 6}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--grid)"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{e(fmt(v, True))}</text>')
    step = max(1, round(n / 8))
    for i, k in enumerate(keys):
        x = left + slot * i + (slot - bar_w) / 2
        y = top + plot_h
        tip_lines = [f"<b>{e(labels[i])}</b>"]
        for key, label, color in series:
            v = values.get((k, key), 0)
            if v <= 0:
                continue
            h = plot_h * v / vmax
            seg_h = max(h - 2, 0.8)  # 2px Abstand zwischen Segmenten
            parts.append(f'<rect x="{x:.1f}" y="{y - h + (2 if h > 3 else 0):.1f}" width="{bar_w:.1f}" '
                         f'height="{seg_h:.1f}" rx="2" fill="{color}"/>')
            y -= h
            tip_lines.append(f'<span class="sw" style="background:{color}"></span>{e(label)}: {e(fmt(v, False))}')
        if totals[i] > 0 and len(series) > 1:
            tip_lines.append(f"{e(t('Summe'))}: {e(fmt(totals[i], False))}")
        parts.append(f'<rect x="{left + slot * i:.1f}" y="{top}" width="{slot:.1f}" height="{plot_h}" '
                     f'fill="transparent" data-tip="{e("<br>".join(tip_lines))}"/>')
        if i % step == 0:
            parts.append(f'<text x="{left + slot * i + slot / 2:.1f}" y="{height - 6}" text-anchor="middle">{e(labels[i])}</text>')
    parts.append(f'<line x1="{left}" x2="{width - 6}" y1="{top + plot_h}" y2="{top + plot_h}" stroke="var(--line)"/>')
    legend = ""
    if len(series) > 1:
        legend = '<div class="legend">' + "".join(
            f'<span><span class="sw" style="background:{c}"></span>{e(lbl)}</span>' for _, lbl, c in series
        ) + "</div>"
    return (f'{legend}<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
            f'style="display:block;max-height:{height + 40}px">{"".join(parts)}</svg>')


def _fmt_h(v: float, axis: bool) -> str:
    return fmt_num(v, 0 if (axis and v == int(v)) or v >= 10 else 1, "h")


def _fmt_g(v: float, axis: bool) -> str:
    if v >= 1000 or (axis and v > 0 and v % 1000 == 0):
        return fmt_num(v / 1000, 0 if axis and v % 1000 == 0 else 2, "kg")
    return fmt_num(v, 0, "g")


def _fmt_eur(v: float, axis: bool) -> str:
    return fmt_num(v, 0 if axis and v == int(v) else 2, "€")


DEFECT_CSS = """
.fdcard .rows{display:grid;gap:9px}.fdcard .r{display:grid;grid-template-columns:minmax(120px,200px) minmax(0,1fr) 36px 80px;gap:10px;align-items:center;
 font-size:14px;text-decoration:none;color:var(--fg)}
.fdcard .r .b{height:12px;border-radius:6px;background:color-mix(in srgb,var(--fg) 7%,transparent);overflow:hidden}
.fdcard .r .b i{display:block;height:100%;background:var(--bad);border-radius:6px}.fdcard .r.na .b i{background:var(--muted)}
.fdcard .r .n{text-align:right;color:var(--muted)}.fdcard .r .c{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.fdk{display:grid;grid-template-columns:1fr 1fr;gap:10px}.fdk>div{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.fdk span{display:block;font-size:13px;color:var(--muted)}.fdk b{font-size:22px;font-variant-numeric:tabular-nums}.fdk small{display:block;font-size:12px;color:var(--muted)}
.fdk .bad b{color:var(--bad)}.fdk .ok b{color:var(--ok)}.fdhint{font-size:13px;color:var(--muted);margin:12px 0 0}
@media (max-width:700px){.fdcard .r{grid-template-columns:minmax(0,1fr) 30px 70px}.fdcard .r .b{grid-column:1/-1;grid-row:2}}
"""


def _defect_cards(store: Store, since: float, until: float, cost_all: float) -> str:
    """Zwei Karten: Fehldrucke nach Ursache und was sie gekostet haben (gleicher Zeitraum wie oben)."""
    st = store.defect_stats(since, until)
    if not st["rows"]:
        return (f'<div class="card" style="margin-top:12px"><h3>{t("Fehldrucke")}</h3>'
                f'<p class="muted" style="margin:0">{t("Im Zeitraum keine Fehldrucke. 🎉")}</p></div>')
    merged: dict[str | None, dict[str, float]] = {}
    for r in st["rows"]:
        k = r["defect"] if r["defect"] in DEFECTS else None
        m = merged.setdefault(k, {"n": 0, "cost": 0.0, "charged": 0.0, "n_charged": 0, "n_full": 0})
        for f in m:
            m[f] += r[f] or 0
    order = [k for k in DEFECTS if k in merged] + ([None] if None in merged else [])
    top = max(merged[k]["cost"] for k in order) or 1
    lines = ""
    for k in order:
        m = merged[k]
        lbl = f"{DEFECTS[k][0]} {t(DEFECTS[k][1])}" if k else t("Ursache nicht angegeben")
        lines += (f'<a class="r{"" if k else " na"}" href="./?status=fd:{k or "none"}"><span>{e(lbl)}</span>'
                  f'<span class="b"><i style="width:{m["cost"] / top * 100:.1f}%"></i></span><span class="n">{int(m["n"])}×</span>'
                  f'<span class="c">{fmt_num(m["cost"], 2, "€")}</span></a>')
    n = sum(m["n"] for m in merged.values())
    cost = sum(m["cost"] for m in merged.values())
    chg = sum(m["charged"] for m in merged.values())
    n_chg = int(sum(m["n_charged"] for m in merged.values()))
    n_full = int(sum(m["n_full"] for m in merged.values()))
    share = f' · {fmt_num(cost / cost_all * 100, 1)} % {t("aller Kosten")}' if cost_all else ""
    rp = st["reprints"]
    kp = (f'<div class="bad"><span>{t("Fehldruck-Kosten")}</span><b>{fmt_num(cost, 2, "€")}</b><small>{tn(int(n), "{n} Druck", "{n} Drucke")}{share}</small></div>'
          f'<div class="ok"><span>{t("davon berechnet")}</span><b>{fmt_num(chg, 2, "€")}</b>'
          f'<small>{", ".join(x for x in (t("{n} anteilig", n=n_chg - n_full) if n_chg > n_full else "", t("{n} voll", n=n_full) if n_full else "") if x) or t("nichts berechnet")}</small></div>'
          f'<div><span>{t("Ungedeckt")}</span><b>{fmt_num(max(cost - chg, 0), 2, "€")}</b><small>{t("trägst du selbst")}</small></div>'
          f'<div><span>{t("Nachdrucke")}</span><b>{int(rp["n"] or 0)}</b><small>{t("{v} zusätzlich", v=fmt_num(rp["cost"], 2, "€"))}</small></div>')
    return (f'<style>{DEFECT_CSS}</style><div class="grid2" style="margin-top:12px"><div class="card fdcard"><h3>{t("Fehldrucke nach Ursache")}</h3><div class="rows">{lines}</div>'
            f'<p class="fdhint">{t("„Kundenwunsch geändert“ zählt nicht gegen die Erfolgsquote, es lag ja nicht am Druck.")}</p></div>'
            f'<div class="card"><h3>{t("Was Fehldrucke gekostet haben")}</h3><div class="fdk">{kp}</div></div></div>')


REVENUE_CSS = """
.rvk{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:0 0 12px}
.rvk>div{border:1px solid var(--line);border-radius:10px;padding:10px 12px}.rvk span{display:block;font-size:12px;color:var(--muted)}
.rvk b{display:block;font-size:20px;font-variant-numeric:tabular-nums}.rvk small{font-size:12px;color:var(--muted)}
.rvk .pos b{color:var(--ok)}.rvk .neg b{color:var(--bad)}
.rvt td.pos{color:var(--ok);font-weight:600}.rvt td.neg{color:var(--bad);font-weight:600}.rvt a{color:var(--fg);font-weight:600;text-decoration:none}
"""


def revenue_data(store: Store, period: str, since: float, until: float) -> dict[str, Any]:
    """Einnahmen und Gewinn der abgerechneten Aufträge im Zeitraum (nach Druckstart).

    Preis = was der Kunde zahlen soll (Endpreis bzw. Kosten + Kundenmarge, Fehldruck anteilig);
    wurde mehr bezahlt, zählt das Gezahlte. Gewinn = Preis − Selbstkosten (Druck, Zubehör, Arbeit).
    Verschenktes zählt mit seinen Kosten extra, Fehldrucke ohne Berechnung bringen nichts ein.
    """
    from . import extras as _extras
    from .pages import amount_due, overpaid, price_base

    tot: dict[str, float] = defaultdict(float)
    per_bucket: dict[tuple[str, str], float] = defaultdict(float)
    per_cust: dict[int, dict[str, Any]] = {}
    for j in store.billing_rows(period, since, until):
        cost = (price_base(j) or 0) + _extras.work_total(j)
        c = per_cust.setdefault(j["customer_id"], {"id": j["customer_id"], "name": j.get("customer_name") or "", "n": 0,
                                                   "revenue": 0.0, "paid": 0.0, "open": 0.0, "profit": 0.0, "gift": 0.0})
        c["n"] += 1
        if j.get("gift"):
            tot["gift"] += cost
            c["gift"] += cost
            continue
        if no_charge(j):
            continue
        due = amount_due(j)
        if due is None:
            continue
        paid = j.get("paid") or 0
        price = due + overpaid(j)
        gain = price - cost
        for d in (tot, c):
            d["revenue"] += price
            d["paid"] += paid
            d["open"] += max(due - paid, 0)
            d["profit"] += gain
        tot["cost"] += cost
        tot["n"] += 1
        per_bucket[(j["bucket"], "cost")] += min(cost, price)
        per_bucket[(j["bucket"], "profit")] += max(gain, 0)
        if gain < 0:
            per_bucket[(j["bucket"], "loss")] += -gain
    custs = sorted((c for c in per_cust.values() if c["revenue"] or c["gift"]), key=lambda c: -c["revenue"])
    return {"tot": tot, "buckets": per_bucket, "customers": custs}


def _revenue_card(keys: list[str], labels: list[str], rv: dict[str, Any]) -> str:
    tot = rv["tot"]
    if not tot["n"] and not tot["gift"]:
        return (f'<div class="card" style="margin-top:12px"><h3>{t("Einnahmen und Gewinn")}</h3>'
                f'<p class="muted" style="margin:0">{t("Im Zeitraum keine abgerechneten Drucke.")}</p></div>')
    pct = f' · {fmt_num(tot["profit"] / tot["cost"] * 100, 0, "%")} {t("auf die Selbstkosten")}' if tot["cost"] > 0 else ""
    kp = (f'<div><span>{t("Umsatz")}</span><b>{fmt_num(tot["revenue"], 2, "€")}</b>'
          f'<small>{tn(int(tot["n"]), "{n} Auftrag", "{n} Aufträge")}</small></div>'
          f'<div><span>{t("Bezahlt")}</span><b>{fmt_num(tot["paid"], 2, "€")}</b>'
          f'<small>{t("{v} offen", v=fmt_num(tot["open"], 2, "€"))}</small></div>'
          f'<div><span>{t("Selbstkosten")}</span><b>{fmt_num(tot["cost"], 2, "€")}</b><small>{t("Druck, Zubehör, Arbeit")}</small></div>'
          f'<div class="{"pos" if tot["profit"] >= 0 else "neg"}"><span>{t("Gewinn")}</span><b>{fmt_num(tot["profit"], 2, "€")}</b>'
          f'<small>{pct.lstrip(" ·") or "–"}</small></div>'
          + (f'<div><span>{t("Verschenkt")}</span><b>{fmt_num(tot["gift"], 2, "€")}</b><small>{t("Kosten, nicht im Gewinn")}</small></div>'
             if tot["gift"] else ""))
    series = [("cost", t("Selbstkosten"), "color-mix(in srgb,var(--fg) 28%,transparent)"), ("profit", t("Gewinn"), "var(--ok)"), ("loss", t("Verlust"), "var(--bad)")]
    if not any(v for (_, k), v in rv["buckets"].items() if k == "loss"):
        series = series[:2]
    rows = "".join(
        f'<tr><td><a href="customer?id={c["id"]}">{e(c["name"])}</a></td>'
        f'<td class="n" data-l="{e(t("Aufträge"))}">{c["n"]}</td>'
        f'<td class="n" data-l="{e(t("Umsatz"))}">{fmt_num(c["revenue"], 2, "€")}</td>'
        f'<td class="n" data-l="{e(t("Bezahlt"))}">{fmt_num(c["paid"], 2, "€")}</td>'
        f'<td class="n" data-l="{e(t("Offen"))}">{fmt_num(c["open"], 2, "€") if c["open"] >= 0.005 else "–"}</td>'
        f'<td class="n {"pos" if c["profit"] >= 0 else "neg"}" data-l="{e(t("Gewinn"))}">{fmt_num(c["profit"], 2, "€")}</td>'
        f'<td class="n" data-l="{e(t("Verschenkt"))}">{fmt_num(c["gift"], 2, "€") if c["gift"] else "–"}</td></tr>'
        for c in rv["customers"])
    return (f'<style>{REVENUE_CSS}</style><h2>{t("Einnahmen und Gewinn")}</h2><div class="rvk">{kp}</div>'
            f'<div class="grid2"><div class="card"><h3>{t("Umsatz = Selbstkosten + Gewinn")}</h3>'
            f'{bar_chart(keys, labels, series, rv["buckets"], _fmt_eur)}</div>'
            f'<div class="card"><h3>{t("Kunden im Zeitraum")}</h3><div class="wrap" style="border:0"><table class="stack rvt">'
            f'<thead><tr><th>{t("Kunde")}</th><th class="n">{t("Aufträge")}</th><th class="n">{t("Umsatz")}</th><th class="n">{t("Bezahlt")}</th>'
            f'<th class="n">{t("Offen")}</th><th class="n">{t("Gewinn")}</th><th class="n">{t("Verschenkt")}</th></tr></thead><tbody>{rows}</tbody></table></div></div></div>'
            f'<p class="muted" style="font-size:13px;margin:8px 0 0">{t("Nach Druckstart. Preis = Endpreis oder Kosten + Kundenmarge; wurde mehr bezahlt, zählt das Gezahlte. Ohne Kunden mit „Keine Abrechnung“ und ohne Fehldrucke, die nicht berechnet werden.")}</p>')


def page_stats(store: Store, period: str) -> str:
    if period not in PERIODS:
        period = "month"
    names = printer_names(store)
    keys, labels, since, until = _buckets(period, date.today())
    rows = store.buckets(period, since.timestamp(), until.timestamp())
    mrows = store.material_buckets(period, since.timestamp(), until.timestamp())

    order = list(names) + sorted({r["printer"] for r in rows} - set(names))
    printer_series = [(p, names.get(p, p), _series_color(i)) for i, p in enumerate(order)]
    used_printers = {r["printer"] for r in rows}
    printer_series = [s for s in printer_series if s[0] in used_printers] or printer_series[:1]

    hours = {(r["bucket"], r["printer"]): r["hours"] or 0 for r in rows}
    costs: dict[tuple[str, str], float] = defaultdict(float)
    for r in rows:
        for key, _ in COST_SERIES:
            costs[(r["bucket"], key)] += r[key] or 0
    cost_series = [(k, t(lbl), "var(--bad)" if k == "scrap_cost" else _series_color(i)) for i, (k, lbl) in enumerate(COST_SERIES)]

    types_seen = {r["mtype"] for r in mrows}
    mat_order = [t for t in MATERIAL_ORDER if t in types_seen] + sorted(types_seen - set(MATERIAL_ORDER))
    if len(mat_order) > 7:  # mehr als 7 Typen: Rest unter "Andere"
        keep = set(mat_order[:6])
        mat_order = mat_order[:6] + ["Andere"]
    else:
        keep = set(mat_order)
    grams: dict[tuple[str, str], float] = defaultdict(float)
    for r in mrows:
        mt = r["mtype"] if r["mtype"] in keep else "Andere"
        grams[(r["bucket"], mt)] += r["grams"] or 0
    mat_series = ([(mt, t(mt) if mt == "Andere" else mt, _series_color(i)) for i, mt in enumerate(mat_order)]
                  or [("PLA", "PLA", "var(--s1)")])

    # Kennzahlen für den Zeitraum
    tot: dict[str, float] = defaultdict(float)
    per_printer: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        for k in ("n", "hours", "grams", "kwh", "cost_total", "ok", "failed"):
            tot[k] += r[k] or 0
            per_printer[r["printer"]][k] += r[k] or 0
    rate = f'{tot["ok"] / (tot["ok"] + tot["failed"]) * 100:.0f} %' if tot["ok"] + tot["failed"] else "–"
    kpis = [
        (t("Druckstunden"), fmt_hours(tot["hours"])),
        (t("Drucke"), str(int(tot["n"]))),
        (t("Erfolgsquote"), rate),
        (t("Filament"), fmt_num(tot["grams"] / 1000, 2, "kg")),
        (t("Energie"), fmt_num(tot["kwh"], 1, "kWh")),
        (t("Gesamtkosten"), fmt_num(tot["cost_total"], 2, "€")),
    ]
    kpi_html = "".join(f'<div class="kpi"><span class="muted">{e(k)}</span><b>{e(v)}</b></div>' for k, v in kpis)

    printer_rows = "".join(
        f'<tr><td><span class="sw" style="background:{c}"></span>{e(lbl)}</td>'
        f'<td class="n" data-l="{e(t("Drucke"))}">{int(per_printer[p]["n"])}</td><td class="n" data-l="{e(t("Stunden"))}">{fmt_hours(per_printer[p]["hours"])}</td>'
        f'<td class="n" data-l="{e(t("Erfolg"))}">{(per_printer[p]["ok"] / (per_printer[p]["ok"] + per_printer[p]["failed"]) * 100) if per_printer[p]["ok"] + per_printer[p]["failed"] else 0:.0f} %</td>'
        f'<td class="n" data-l="{e(t("Filament"))}">{fmt_num(per_printer[p]["grams"] / 1000, 2, "kg")}</td>'
        f'<td class="n" data-l="{e(t("Energie"))}">{fmt_num(per_printer[p]["kwh"], 2, "kWh")}</td>'
        f'<td class="n" data-l="{e(t("Kosten"))}">{fmt_num(per_printer[p]["cost_total"], 2, "€")}</td></tr>'
        for p, lbl, c in printer_series if p in per_printer
    ) or f'<tr><td colspan="7" class="muted">{t("Im Zeitraum noch keine Drucke.")}</td></tr>'

    seg = "".join(
        f'<a href="stats?period={k}" class="{"on" if k == period else ""}">{t(lbl)}</a>' for k, (lbl, _) in PERIODS.items()
    )
    range_txt = {"day": t("letzte 30 Tage"), "week": t("letzte 12 Wochen"), "month": t("letzte 12 Monate")}[period]

    body = f"""<div class="f" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
<span class="seg">{seg}</span><span class="muted">{range_txt}</span></div>
<div class="kpis">{kpi_html}</div>
<div class="grid2">
<div class="card"><h3>{t("Druckstunden pro Drucker")}</h3>{bar_chart(keys, labels, printer_series, hours, _fmt_h)}</div>
<div class="card"><h3>{t("Kosten")}</h3>{bar_chart(keys, labels, cost_series, costs, _fmt_eur)}</div>
<div class="card"><h3>{t("Verdrucktes Filament nach Material")}</h3>{bar_chart(keys, labels, mat_series, grams, _fmt_g)}</div>
<div class="card"><h3>{t("Drucker im Zeitraum")}</h3><div class="wrap" style="border:0">
<table class="stack"><thead><tr><th>{t("Drucker")}</th><th class="n">{t("Drucke")}</th><th class="n">{t("Stunden")}</th><th class="n">{t("Erfolg")}</th>
<th class="n">{t("Filament")}</th><th class="n">{t("Energie")}</th><th class="n">{t("Kosten")}</th></tr></thead><tbody>{printer_rows}</tbody></table></div></div>
</div>
{_defect_cards(store, since.timestamp(), until.timestamp(), tot["cost_total"])}
{_revenue_card(keys, labels, revenue_data(store, period, since.timestamp(), until.timestamp()))}
<h2>{t("Druckstunden pro Tag, letzte 12 Monate")}</h2>
<div class="card">{heatmap(store)}</div>"""
    return layout(t("Statistik"), "stats", f'<div class="calc">{body}</div>')


def heatmap(store: Store) -> str:
    today = date.today()
    start = today - timedelta(days=364)
    start -= timedelta(days=start.weekday())  # mit Montag beginnen
    since = datetime.combine(start, datetime.min.time()).timestamp()
    until = datetime.combine(today + timedelta(days=1), datetime.min.time()).timestamp()
    per_day: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    for r in store.buckets("day", since, until):
        per_day[r["bucket"]] += r["hours"] or 0
        count[r["bucket"]] += r["n"]
    vmax = max(per_day.values(), default=0)
    cells = []
    d = start
    while d <= today:
        k = d.strftime("%Y-%m-%d")
        h = per_day.get(k, 0)
        if h > 0:
            level = 0.25 + 0.75 * min(h / vmax, 1) if vmax else 0.25
            style = f'background:color-mix(in srgb, var(--s1) {level * 100:.0f}%, var(--grid))'
        else:
            style = ""
        tip = (f'{fmt_date(datetime(d.year, d.month, d.day), "date")}: {fmt_hours(h) if h else t("kein Druck")}'
               + (" · " + tn(count[k], "{n} Druck", "{n} Drucke") if count.get(k) else ""))
        link = f'<a href="./?day={k}" title=""><i style="{style}" data-tip="{e(tip)}"></i></a>' if h else f'<i data-tip="{e(tip)}"></i>'
        cells.append(link)
        d += timedelta(days=1)
    return (f'<div class="heat">{"".join(cells)}</div>'
            f'<div class="legend" style="margin-top:6px"><span>{t("weniger")}</span>'
            + "".join(f'<i style="display:inline-block;width:12px;height:12px;border-radius:3px;'
                      f'background:color-mix(in srgb, var(--s1) {p}%, var(--grid))"></i>' for p in (0, 25, 50, 75, 100))
            + f'<span>{t("mehr")}</span></div>')


# ---- Kalender -------------------------------------------------------------------

def page_calendar(store: Store, month: str | None) -> str:
    today = date.today()
    try:
        first = datetime.strptime(month, "%Y-%m").date() if month else today.replace(day=1)
    except ValueError:
        first = today.replace(day=1)
    days_in = cal.monthrange(first.year, first.month)[1]
    grid_start = first - timedelta(days=first.weekday())
    last = first.replace(day=days_in)
    grid_end = last + timedelta(days=6 - last.weekday())

    since = datetime.combine(grid_start, datetime.min.time()).timestamp()
    until = datetime.combine(grid_end + timedelta(days=1), datetime.min.time()).timestamp()
    jobs = store.list_jobs(limit=5000, since=since, until=until, real="1")
    photos = store.first_photos([j["id"] for j in jobs])
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for j in jobs:
        if j["start_ts"]:
            by_day[datetime.fromtimestamp(j["start_ts"]).strftime("%Y-%m-%d")].append(j)

    cells = [f'<div class="h">{t(d)}</div>' for d in WEEKDAYS]
    d = grid_start
    while d <= grid_end:
        k = d.strftime("%Y-%m-%d")
        js = by_day.get(k, [])
        cls = "d" + (" out" if d.month != first.month else "") + (" today" if d == today else "")
        info = ""
        if js:
            hours = sum((j["end_ts"] - j["start_ts"]) / 3600 for j in js if j["end_ts"] and j["start_ts"] and j["end_ts"] > j["start_ts"])
            cost = sum(j.get("cost_total") or 0 for j in js)
            thumbs = "".join(
                f'<img src="images/{e(photos.get(j["id"]) or j["image"])}" alt="" loading="lazy">'
                for j in js[:6] if photos.get(j["id"]) or j.get("image")
            )
            info = (f'<span class="cc">{len(js)}</span><div class="cl">{tn(len(js), "{n} Druck", "{n} Drucke")}</div>'
                    f'<div class="muted cl">{fmt_hours(hours)} · <span class="calc">{fmt_num(cost, 2, "€")}</span></div>'
                    f'<div class="thumbs">{thumbs}</div>')
            tip = "<br>".join(e(j["task_name"] or "–") for j in js[:10])
            cells.append(f'<a class="{cls}" href="./?day={k}" data-tip="{e(tip)}"><b>{d.day}</b>{info}</a>')
        else:
            cells.append(f'<div class="{cls}"><b>{d.day}</b></div>')
        d += timedelta(days=1)

    prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
    next_m = (last + timedelta(days=1)).strftime("%Y-%m")
    month_jobs = [j for j in jobs if j["start_ts"] and datetime.fromtimestamp(j["start_ts"]).month == first.month]
    total = sum(j.get("cost_total") or 0 for j in month_jobs)
    body = f"""<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
<a href="calendar?month={prev_m}">←</a><h2 style="margin:0">{t(MONTHS[first.month - 1])} {first.year}</h2>
<a href="calendar?month={next_m}">→</a><a href="calendar" class="muted">{t("heute")}</a>
<span class="muted">{tn(len(month_jobs), "{n} Druck", "{n} Drucke")} · <span class="calc">{fmt_num(total, 2, "€")}</span></span></div>
<div class="cal">{"".join(cells)}</div>
<p class="muted">{t("Ein Klick auf einen Tag zeigt dessen Drucke in der Tabelle.")}</p>"""
    return layout(t("Kalender"), "calendar", body)


__all__ = ["page_stats", "page_calendar", "parse_materials"]
