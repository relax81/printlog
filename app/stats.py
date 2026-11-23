"""Statistikseite (Tag/Woche/Monat) und Monatskalender."""

from __future__ import annotations

import calendar as cal
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from .costing import parse_materials
from .db import Store
from .ui import e, fmt_hours, fmt_num, layout, printer_names

PERIODS = {
    "day": ("Tag", 30),      # letzte 30 Tage
    "week": ("Woche", 12),   # letzte 12 Wochen
    "month": ("Monat", 12),  # letzte 12 Monate
}
MATERIAL_ORDER = ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC"]
COST_SERIES = [("cost_energy", "Strom"), ("cost_filament", "Filament"), ("cost_printer", "Drucker")]
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
          "Oktober", "November", "Dezember"]


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
            labels.append(d.strftime("%d.%m."))
        since = datetime.combine(start, datetime.min.time())
        until = datetime.combine(today + timedelta(days=1), datetime.min.time())
    elif period == "week":
        monday = today - timedelta(days=today.weekday())
        start = monday - timedelta(weeks=n - 1)
        for i in range(n):
            d = start + timedelta(weeks=i)
            keys.append(d.strftime("%Y-W%W"))
            labels.append(d.strftime("%d.%m."))
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
    left, bottom, top = 44, 22, 8
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
            tip_lines.append(f"Summe: {e(fmt(totals[i], False))}")
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
    cost_series = [(k, lbl, _series_color(i)) for i, (k, lbl) in enumerate(COST_SERIES)]

    types_seen = {r["mtype"] for r in mrows}
    mat_order = [t for t in MATERIAL_ORDER if t in types_seen] + sorted(types_seen - set(MATERIAL_ORDER))
    if len(mat_order) > 7:  # mehr als 7 Typen: Rest unter "Andere"
        keep = set(mat_order[:6])
        mat_order = mat_order[:6] + ["Andere"]
    else:
        keep = set(mat_order)
    grams: dict[tuple[str, str], float] = defaultdict(float)
    for r in mrows:
        t = r["mtype"] if r["mtype"] in keep else "Andere"
        grams[(r["bucket"], t)] += r["grams"] or 0
    mat_series = [(t, t, _series_color(i)) for i, t in enumerate(mat_order)] or [("PLA", "PLA", "var(--s1)")]

    # Kennzahlen für den Zeitraum
    tot: dict[str, float] = defaultdict(float)
    per_printer: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        for k in ("n", "hours", "grams", "kwh", "cost_total", "ok", "failed"):
            tot[k] += r[k] or 0
            per_printer[r["printer"]][k] += r[k] or 0
    rate = f'{tot["ok"] / (tot["ok"] + tot["failed"]) * 100:.0f} %' if tot["ok"] + tot["failed"] else "–"
    kpis = [
        ("Druckstunden", fmt_hours(tot["hours"])),
        ("Drucke", str(int(tot["n"]))),
        ("Erfolgsquote", rate),
        ("Filament", fmt_num(tot["grams"] / 1000, 2, "kg")),
        ("Energie", fmt_num(tot["kwh"], 1, "kWh")),
        ("Gesamtkosten", fmt_num(tot["cost_total"], 2, "€")),
    ]
    kpi_html = "".join(f'<div class="kpi"><span class="muted">{e(k)}</span><b>{e(v)}</b></div>' for k, v in kpis)

    printer_rows = "".join(
        f'<tr><td><span class="sw" style="background:{c}"></span>{e(lbl)}</td>'
        f'<td class="n" data-l="Drucke">{int(per_printer[p]["n"])}</td><td class="n" data-l="Stunden">{fmt_hours(per_printer[p]["hours"])}</td>'
        f'<td class="n" data-l="Erfolg">{(per_printer[p]["ok"] / (per_printer[p]["ok"] + per_printer[p]["failed"]) * 100) if per_printer[p]["ok"] + per_printer[p]["failed"] else 0:.0f} %</td>'
        f'<td class="n" data-l="Filament">{fmt_num(per_printer[p]["grams"] / 1000, 2, "kg")}</td>'
        f'<td class="n" data-l="Energie">{fmt_num(per_printer[p]["kwh"], 2, "kWh")}</td>'
        f'<td class="n" data-l="Kosten">{fmt_num(per_printer[p]["cost_total"], 2, "€")}</td></tr>'
        for p, lbl, c in printer_series if p in per_printer
    ) or '<tr><td colspan="7" class="muted">Im Zeitraum noch keine Drucke.</td></tr>'

    seg = "".join(
        f'<a href="stats?period={k}" class="{"on" if k == period else ""}">{lbl}</a>' for k, (lbl, _) in PERIODS.items()
    )
    range_txt = {"day": "letzte 30 Tage", "week": "letzte 12 Wochen", "month": "letzte 12 Monate"}[period]

    body = f"""<div class="f" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
<span class="seg">{seg}</span><span class="muted">{range_txt}</span></div>
<div class="kpis">{kpi_html}</div>
<div class="grid2">
<div class="card"><h3>Druckstunden pro Drucker</h3>{bar_chart(keys, labels, printer_series, hours, _fmt_h)}</div>
<div class="card"><h3>Kosten</h3>{bar_chart(keys, labels, cost_series, costs, _fmt_eur)}</div>
<div class="card"><h3>Verdrucktes Filament nach Material</h3>{bar_chart(keys, labels, mat_series, grams, _fmt_g)}</div>
<div class="card"><h3>Drucker im Zeitraum</h3><div class="wrap" style="border:0">
<table class="stack"><thead><tr><th>Drucker</th><th class="n">Drucke</th><th class="n">Stunden</th><th class="n">Erfolg</th>
<th class="n">Filament</th><th class="n">Energie</th><th class="n">Kosten</th></tr></thead><tbody>{printer_rows}</tbody></table></div></div>
</div>
<h2>Druckstunden pro Tag, letzte 12 Monate</h2>
<div class="card">{heatmap(store)}</div>"""
    return layout("Statistik", "stats", f'<div class="calc">{body}</div>')


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
        tip = f'{d.strftime("%d.%m.%Y")}: {fmt_hours(h) if h else "kein Druck"}' + (f' · {count[k]} Drucke' if count.get(k) else "")
        link = f'<a href="./?day={k}" title=""><i style="{style}" data-tip="{e(tip)}"></i></a>' if h else f'<i data-tip="{e(tip)}"></i>'
        cells.append(link)
        d += timedelta(days=1)
    return (f'<div class="heat">{"".join(cells)}</div>'
            '<div class="legend" style="margin-top:6px"><span>weniger</span>'
            + "".join(f'<i style="display:inline-block;width:12px;height:12px;border-radius:3px;'
                      f'background:color-mix(in srgb, var(--s1) {p}%, var(--grid))"></i>' for p in (0, 25, 50, 75, 100))
            + '<span>mehr</span></div>')


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
    jobs = store.list_jobs(limit=5000, since=since, until=until)
    photos = store.first_photos([j["id"] for j in jobs])
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for j in jobs:
        if j["start_ts"]:
            by_day[datetime.fromtimestamp(j["start_ts"]).strftime("%Y-%m-%d")].append(j)

    cells = [f'<div class="h">{d}</div>' for d in WEEKDAYS]
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
            info = (f'<div>{len(js)} Druck{"e" if len(js) != 1 else ""}</div>'
                    f'<div class="muted">{fmt_hours(hours)} · <span class="calc">{fmt_num(cost, 2, "€")}</span></div>'
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
<a href="calendar?month={prev_m}">←</a><h2 style="margin:0">{MONTHS[first.month - 1]} {first.year}</h2>
<a href="calendar?month={next_m}">→</a><a href="calendar" class="muted">heute</a>
<span class="muted">{len(month_jobs)} Drucke · <span class="calc">{fmt_num(total, 2, "€")}</span></span></div>
<div class="cal">{"".join(cells)}</div>
<p class="muted">Ein Klick auf einen Tag zeigt dessen Drucke in der Tabelle.</p>"""
    return layout("Kalender", "calendar", body)


__all__ = ["page_stats", "page_calendar", "parse_materials"]
