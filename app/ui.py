"""Gemeinsame HTML-Bausteine: Layout, Styles, Formatierung."""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import Store
from .i18n import N_, is_en, js, lang, t

e = html.escape

NAV = [
    ("./", N_("Drucke"), "jobs"),
    ("stats", N_("Statistik"), "stats"),
    ("calendar", N_("Kalender"), "calendar"),
    ("customers", N_("Kunden"), "customers"),
    ("settings", N_("Einstellungen"), "settings"),
]


def printer_names(store: Store) -> dict[str, str]:
    """Kurzname → Anzeigename. Erst die eingerichteten Drucker (feste Farbreihenfolge),
    dann solche, die nur noch in alten Drucken vorkommen."""
    from . import printers as pcfg

    names = {c["key"]: c["name"] or c["key"] for c in pcfg.load(store)}
    for p in store.printers():
        names.setdefault(p["printer"], p["printer_name"] or p["printer"])
    return names


# ---- Formatierung ---------------------------------------------------------

# Datumsformate je Sprache: Deutsch mit Punkten, Englisch mit Monatsnamen (eindeutig).
DATE_FORMATS = {
    "de": {"ts": "%d.%m.%y %H:%M", "date": "%d.%m.%Y", "date_s": "%d.%m.%y", "dm": "{d}.{m}.", "dm_hm": "{d}.{m}. %H:%M",
           "ts_long": "%d.%m.%Y %H:%M"},
    "en": {"ts": "%d %b %Y %H:%M", "date": "%d %b %Y", "date_s": "%d %b %Y", "dm": "{d} %b", "dm_hm": "{d} %b %H:%M",
           "ts_long": "%d %b %Y %H:%M"},
}


def fmt_date(value: datetime | float | None, kind: str = "date") -> str:
    """Datum/Zeit in der aktuellen Sprache. kind: ts, ts_long, date, date_s, dm, dm_hm."""
    if value is None or value == 0:
        return "–"
    d = value if isinstance(value, datetime) else datetime.fromtimestamp(value)
    fmt = DATE_FORMATS["en" if is_en() else "de"][kind]
    return d.strftime(fmt.replace("{d}", str(d.day)).replace("{m}", str(d.month)))


def fmt_ts(ts: float | None) -> str:
    return fmt_date(ts, "ts") if ts else "–"


def fmt_dur(start: float | None, end: float | None) -> str:
    if not start or not end or end < start:
        return "–"
    return fmt_hours((end - start) / 3600)


def fmt_hours(h: float | None) -> str:
    if h is None:
        return "–"
    mins = int(round(h * 60))
    return f"{mins // 60}:{mins % 60:02d} h"


def fmt_num(v: float | None, digits: int, unit: str = "") -> str:
    if v is None:
        return "–"
    s = f"{v:,.{digits}f}"
    if not is_en():
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {unit}".strip()


def fmt_input(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v:g}" if is_en() else f"{v:g}".replace(".", ",")


def parse_num(v: Any) -> float | None:
    if v is None:
        return None
    v = str(v).strip().replace("€", "").replace(" ", "")
    if not v:
        return None
    if is_en():
        # Englisch: Komma trennt Tausender (1,299.50), ein einzelnes Komma gilt als Dezimalzeichen
        if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d*)?", v):
            v = v.replace(",", "")
        elif "," in v and "." not in v:
            v = v.replace(",", ".")
    elif "," in v:
        v = v.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", v):
        v = v.replace(".", "")  # deutsches Tausenderformat, z. B. 1.299
    try:
        f = float(v)
    except ValueError:
        return None
    return f if f >= 0 else None


def opt(value: str, label: str, current: str | None) -> str:
    sel = " selected" if value == (current or "") else ""
    return f'<option value="{e(value)}"{sel}>{e(label)}</option>'


def bon_tagline() -> str:
    """Gesperrte Zeile unter dem Logo auf Bon und Logo-Vorschau, z. B. „3 D - D R U C K“."""
    return " ".join(t("3D-DRUCK"))


# ---- Layout -----------------------------------------------------------------

# Farben: Kategorien in fester Reihenfolge (Referenzpalette, validiert für hell und dunkel).
STATIC_DIR = Path(__file__).resolve().parent / "static"
# Schriftzug inline, damit er per currentColor dem Hell/Dunkel-Theme folgt
LOGO_SVG = (STATIC_DIR / "logo.svg").read_text(encoding="utf-8").strip()

EYE_SVG = ('<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
           '<path class="o" d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle class="o" cx="12" cy="12" r="3"/>'
           '<path class="c" d="M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6C3.7 8.4 2 12 2 12s3.6 7 10 7a9.7 9.7 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>')

CSS = """
:root{--bg:#f6f7f9;--card:#fcfcfb;--fg:#1c1f24;--muted:#6b7280;--line:#e5e7eb;--grid:#eceef1;
--ok:#15803d;--bad:#b91c1c;--warn:#b45309;--run:#1d4ed8;--accent:#0f766e;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--s8:#e34948}
@media (prefers-color-scheme:dark){:root{--bg:#111315;--card:#1a1a19;--fg:#f0efec;--muted:#a3a29b;
--line:#2c3036;--grid:#26292d;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--run:#60a5fa;--accent:#2dd4bf;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#0ca30c;--s7:#9085e9;--s8:#e66767}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px}
a{color:var(--accent)}h1{font-size:18px;margin:0}
.logo a{display:block;color:var(--fg);line-height:0}.logo svg{height:28px;width:auto}h2{font-size:15px;margin:20px 0 8px}
.muted{color:var(--muted)}
.pid{display:inline-block;font:600 11px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.04em;
color:var(--muted);border:1px solid var(--line);border-radius:5px;padding:0 5px;margin-left:6px;vertical-align:2px;white-space:nowrap}
.meta .pid,.badge .pid{margin-left:0;vertical-align:0}
button.pidb{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.04em;cursor:copy}
.top{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;justify-content:space-between;margin-bottom:12px}
nav{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center}
nav a{text-decoration:none;font-weight:600;color:var(--muted)}nav a.on{color:var(--fg)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:10px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kpi b{display:block;font-size:20px;font-variant-numeric:tabular-nums}.kpi.main b{color:var(--accent)}
.mini{display:flex;flex-wrap:wrap;gap:6px 16px;margin:0 0 12px;font-size:13px}
form.f{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;align-items:center}
select,button,input,textarea{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font:inherit}
input.num{width:110px;text-align:right}input.qty{width:56px;text-align:right;padding:4px 6px}
input.search{min-width:220px;flex:1 1 220px;max-width:360px}
button{cursor:pointer}button.primary{background:var(--accent);color:#fff;border-color:var(--accent);padding:8px 14px}
.wrap{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
table{border-collapse:collapse;width:100%}table.jobs{min-width:1180px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.03em}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.sum{font-weight:700}tr:last-child td{border-bottom:0}
img.t{width:56px;height:56px;object-fit:contain;border-radius:6px;background:var(--bg);cursor:zoom-in}
.ph{width:56px;height:56px;border-radius:6px;background:var(--bg)}
.s{font-weight:600;white-space:nowrap}.s-fertig{color:var(--ok)}.s-abgebrochen{color:var(--bad)}
.s-unvollständig{color:var(--warn)}.s-läuft,.s-kühlt{color:var(--run)}
.note{font-size:12px;color:var(--muted)}.del{border:0;background:none;color:var(--muted);font-size:16px}
.chips{display:flex;flex-wrap:wrap;gap:4px 10px;margin-top:3px;font-size:12px;color:var(--muted)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px;vertical-align:-1px;border:1px solid var(--line)}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.banner{background:var(--card);border:1px solid var(--accent);border-radius:10px;padding:8px 12px;margin-bottom:12px}
.edit select{max-width:160px;padding:4px 6px}.edit{display:flex;gap:6px;align-items:center;margin-top:4px;flex-wrap:wrap}
.saved{color:var(--ok);font-size:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,520px),1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card h3{font-size:14px;margin:0 0 6px}.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--muted);margin-bottom:6px}
svg text{fill:var(--muted);font-size:11px}svg .val{fill:var(--fg)}
.tip{position:fixed;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:6px 9px;font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.18);display:none;z-index:20;white-space:nowrap}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg a{padding:6px 12px;text-decoration:none;color:var(--muted);font-weight:600}.seg a.on{background:var(--accent);color:#fff}
.cal{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}
.cal .h{font-size:12px;color:var(--muted);text-align:center}
.cal .d{background:var(--card);border:1px solid var(--line);border-radius:8px;min-height:92px;padding:6px;font-size:12px;text-decoration:none;color:var(--fg);display:block;overflow:hidden}
.cal .d.out{opacity:.35}.cal .d.today{border-color:var(--accent)}
.cal .d b{font-size:13px}.cal .d .thumbs{display:flex;flex-wrap:wrap;gap:2px;margin-top:4px}
.cal .d .thumbs img{width:22px;height:22px;object-fit:contain;border-radius:4px;background:var(--bg)}
.heat{display:grid;grid-auto-flow:column;grid-template-rows:repeat(7,12px);gap:3px;overflow-x:auto;padding-bottom:4px}
.heat i{width:12px;height:12px;border-radius:3px;background:var(--grid);display:block}
.eye{border:0;background:none;color:var(--muted);padding:2px 4px;line-height:0;cursor:pointer}
.eye .c{display:none}.priv .eye{color:var(--accent)}.priv .eye .o{display:none}.priv .eye .c{display:inline}
.priv .calc{filter:blur(7px);pointer-events:none;user-select:none;-webkit-user-select:none}
.priv .pop{display:none!important}
#zoom{position:fixed;inset:0;background:rgba(0,0,0,.75);display:none;align-items:center;justify-content:center;z-index:9}
#zoom img{max-width:92vw;max-height:92vh;border-radius:10px;background:#fff}
@media (max-width:640px){body{padding:12px}.cal .d{min-height:64px}.cal .d .thumbs{display:none}}
@media (max-width:700px){
.kpis{grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.kpi{padding:7px 9px}.kpi span{font-size:11px}.kpi b{font-size:15px}
table.stack thead{display:none}
table.stack tr{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 10px;padding:10px 12px;border-bottom:1px solid var(--line)}
table.stack tr:last-child{border-bottom:0}
table.stack td{border:0;padding:0;text-align:left;white-space:normal}
table.stack td:first-child,table.stack td[colspan]{grid-column:1/-1;font-weight:600}
table.stack td[data-l]:before{content:attr(data-l);display:block;font-size:11px;color:var(--muted)}
table.stack input.num{width:100%;text-align:left}
table.stack .edit input{flex:1 1 140px;width:auto!important}
}
"""

BASE_JS = """
document.getElementById('eye').onclick=()=>{const on=document.documentElement.classList.toggle('priv');
 try{localStorage.setItem('pl_priv',on?'1':'0')}catch(_){}};
const zd=document.getElementById('zoom');
if(zd){zd.onclick=()=>zd.style.display='none'}
function z(src){zd.querySelector('img').src=src;zd.style.display='flex'}
const tip=document.getElementById('tip');
document.addEventListener('mousemove',ev=>{const t=ev.target.closest('[data-tip]');
 if(!t){tip.style.display='none';return}
 tip.innerHTML=t.dataset.tip;tip.style.display='block';
 const x=Math.min(ev.clientX+14,innerWidth-tip.offsetWidth-8);tip.style.left=x+'px';tip.style.top=(ev.clientY+14)+'px'});
"""


def layout(title: str, active: str, body: str, script: str = "") -> str:
    links = "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{e(t(label))}</a>' for href, label, key in NAV
    )
    return f"""<!doctype html><html lang="{lang()}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/static/icon.svg" type="image/svg+xml">
<link rel="icon" href="/static/favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<meta name="theme-color" content="#16181f"><meta name="apple-mobile-web-app-title" content="Printlog">
<script>try{{if(localStorage.getItem('pl_priv')==='1')document.documentElement.classList.add('priv')}}catch(_){{}}</script>
<style>{CSS}</style></head><body>
<div class="top"><h1 class="logo"><a href="/" title="Printlog">{LOGO_SVG}</a></h1><nav>{links}
<button type="button" class="eye" id="eye" title="{e(t("Kalkulation aus-/einblenden (Diskret-Modus)"))}" aria-label="{e(t("Kalkulation aus- oder einblenden"))}">{EYE_SVG}</button></nav></div>
{body}
<div id="zoom"><img alt=""></div><div id="tip" class="tip"></div>
<script>{BASE_JS}{js(script)}</script></body></html>"""
