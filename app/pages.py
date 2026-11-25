"""Seiten: Drucke (Tabelle/Galerie), Druckdetails, Kunden, Einstellungen."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from urllib.parse import quote
from typing import Any

from .costing import KNOWN_TYPES, hourly_rate, material_notes, parse_materials
from .db import Store, code_label
from .defects import DEFECTS, apply_charge, charge_src, charge_text, charged, defect_label, no_charge
from .i18n import LANGS, N_, is_en as lang_en, note as t_note, t, tn
from .ui import (
    DEMO,
    THEMES,
    bon_tagline,
    e,
    parse_num,
    fmt_date,
    fmt_dur,
    fmt_input,
    fmt_num,
    fmt_ts,
    layout,
    opt,
    printer_names,
)

FILTER_KEYS = ("q", "printer", "month", "day", "status", "material", "customer", "model", "view", "group", "todo")
# Nur Darstellung, kein Datenbankfilter
VIEW_KEYS = ("view", "group", "todo")

# Filter „Offenes“: ein Auswahlfeld für Zahlung und Übergabe
TODO_OPTIONS = {
    "unbezahlt": (N_("💶 nicht bezahlt"), {"paid": "offen"}),
    "anbezahlt": (N_("💶 teilweise bezahlt"), {"paid": "teilweise"}),
    "bezahlt": (N_("✓ bezahlt"), {"paid": "bezahlt"}),
    "verschenkt": (N_("🎁 verschenkt"), {"paid": "verschenkt"}),
    "abholen": (N_("📦 nicht (ganz) übergeben"), {"handover": "offen"}),
    "teilweise": (N_("📦 teilweise übergeben"), {"handover": "teilweise"}),
    "uebergeben": (N_("✓ übergeben"), {"handover": "übergeben"}),
}



def _pcs(q: object) -> str:
    """„1 Stück“ / „3 Stück“ – im Englischen „1 piece“ / „3 pieces“."""
    return t("1 Stück") if q == 1 else t("{q} Stück", q=q)

def db_filters(f: dict[str, str | None]) -> dict[str, str | None]:
    """URL-Filter → Datenbankfilter (Darstellungsschalter raus, „Offenes“ übersetzt)."""
    q: dict[str, str | None] = {k: v for k, v in f.items() if k not in VIEW_KEYS}
    todo = TODO_OPTIONS.get(f.get("todo") or "")
    if todo:
        q.update(todo[1])
    return q


# Status steht deutsch in der Datenbank (und dient als CSS-Klasse), angezeigt wird t(status).
SLOT_LABELS = (N_("aktiv"),)  # Slot ohne AMS-Angabe (aktives Fach), sonst z. B. "AMS 1 Tray 2"
STATUS_LABELS = (N_("fertig"), N_("abgebrochen"), N_("unvollständig"), N_("läuft"), N_("kühlt ab"))


def _status_of(job: dict[str, Any]) -> str:
    if job["state"] == "running":
        return "läuft"
    if job["state"] == "closing":
        return "kühlt ab"
    return job["status"] or "–"


def _color(c: str | None) -> str:
    if c and c.startswith("#") and len(c) in (7, 9):
        return e(c[:7])
    return "var(--muted)"


def _short(name: str | None) -> str:
    if not name:
        return "?"
    return name.removeprefix("Bambu ").removeprefix("Generic ")


def _chips(job: dict[str, Any]) -> str:
    materials = parse_materials(job.get("materials"))
    chips = "".join(
        f'<span><span class="dot" style="background:{_color(m["color"])}"></span>'
        f'{e(_short(m["name"]))} {fmt_num(m["grams"], 1, "g")}</span>'
        for m in materials
    )
    return f'<div class="chips">{chips}</div>' if chips else ""


def _per_piece(job: dict[str, Any]) -> str:
    q = job.get("quantity")
    if q and q > 1 and job.get("cost_total") is not None:
        return t("{v}/Stk.", v=fmt_num((job["cost_total"] + _extras.work_total(job)) / q, 2, "€"))
    return ""


class Margin(tuple):
    """(Name, Prozent) mit ``work`` = Marge auch auf Arbeitszeit."""

    work: bool = False

    def __new__(cls, name: str, pct: float, work: bool = False) -> "Margin":
        m = super().__new__(cls, (name, pct))
        m.work = bool(work)
        return m


def margins(store: Store) -> list[Margin]:
    """Allgemeine Margen aus den Einstellungen, z. B. [("Privat", 30.0), ("Gewerbe", 60.0)]."""
    cfg = store.settings()
    out = []
    for i in (1, 2):
        pct = parse_num(cfg.get(f"margin{i}_pct"))
        if pct is not None:
            out.append(Margin(cfg.get(f"margin{i}_name") or t("Marge {i}", i=i), pct, cfg.get(f"margin{i}_work") == "1"))
    return out


def customer_margin(job: dict[str, Any]) -> Margin | None:
    """Kundenmarge als Margin (mit „Marge auch auf Arbeitszeit“ des Kunden)."""
    if job.get("customer_margin") is None:
        return None
    return Margin(f'{t("Kunde")} {job.get("customer_name") or ""}'.strip(), job["customer_margin"], bool(job.get("customer_work")))


def price_base(job: dict[str, Any]) -> float | None:
    """Kosten, auf die die Marge kommt: beim Projekt ohne unberechnete Fehldrucke."""
    if job.get("kind") in ("project", "share") and job.get("bill_base") is not None:
        return job["bill_base"]
    return job.get("cost_total")


def _price(job: dict[str, Any], pct: float, work_on: bool = False) -> float | None:
    """Preis mit Marge: Kosten (inkl. Zubehör) × Marge, Arbeit zum Stundensatz dazu (oder mit Marge)."""
    return _extras.sell_price(price_base(job), _extras.work_total(job), pct, work_on)


def mprice(job: dict[str, Any], m: tuple) -> float | None:
    return _price(job, m[1], getattr(m, "work", False))


def work_tag(m: tuple, work: float) -> str:
    """Hinweis an einer Marge, die auch auf die Arbeitszeit gerechnet wird."""
    return f' · {t("inkl. Arbeit")}' if work and getattr(m, "work", False) else ""


def extra_kpis(job: dict[str, Any]) -> str:
    """Kacheln „Zubehör“ und „Arbeit“ im Kopf, nur wenn etwas eingetragen ist."""
    out = ""
    zub = job.get("cost_extras") or 0
    work = _extras.work_total(job)
    if zub:
        out += f'<div class="kp"><span>{t("🔗 Zubehör")}</span><b>{fmt_num(zub, 2, "€")}</b><small>{t("im Druck enthalten")}</small></div>'
    if work:
        prep, post, design = _extras.work_minutes(job)
        sub = t("{m} min", m=fmt_input(round(prep + post)))
        if design:
            sub += " + " + t("{m} min Design", m=fmt_input(round(design)))
        if job.get("kind") == "project" and work != (job.get("cost_work") or 0):
            sub = t("inkl. Arbeit in den Drucken")
        out += f'<div class="kp"><span>{t("⏱ Arbeit")}</span><b>{fmt_num(work, 2, "€")}</b><small>{e(sub)}</small></div>'
    return out


def overpaid(j: dict[str, Any]) -> float:
    """Wie viel mehr bezahlt wurde als der Preis (z. B. aufgerundet oder Trinkgeld), sonst 0."""
    if not billable(j) or j.get("gift") or no_charge(j):
        return 0.0
    due, paid = amount_due(j), j.get("paid") or 0
    return round(paid - due, 2) if due is not None and paid > due + 0.005 else 0.0


def revenue(j: dict[str, Any]) -> float | None:
    """Was tatsächlich reinkommt: bei Überzahlung der gezahlte Betrag, sonst der Endpreis (ohne Endpreis: nichts)."""
    if overpaid(j):
        return j.get("paid") or 0
    return j.get("final_price")


def profit_rows(job: dict[str, Any], qty: int, endpreis: bool = True) -> str:
    """Mit Endpreis oder mehr bezahlt als der Preis: Gewinn über Selbstkosten und effektiver Stundenlohn."""
    fp = job.get("final_price")
    rev = revenue(job)
    if rev is None or job.get("cost_total") is None:
        return ""
    work = _extras.work_total(job)
    self_cost = (price_base(job) or 0) + work
    gain = rev - self_cost
    pct = f' <span class="tag">{fmt_num(gain / self_cost * 100, 1, "%")}</span>' if self_cost > 0 else ""
    cls = "pos" if gain >= 0 else "neg"
    out = f'<div class="pr gw"><div><b>{t("Endpreis")}</b></div><div><b>{fmt_num(fp, 2, "€")}</b></div></div>' if endpreis and fp is not None else ""
    over = overpaid(job)
    if over:
        out += (f'<div class="pr gw"><div><b>{t("Bezahlt")}</b> <span class="tag">{t("+{v} mehr", v=fmt_num(over, 2, "€"))}</span></div>'
                f'<div><b>{fmt_num(rev, 2, "€")}</b></div></div>')
    out += f'<div class="pr gw"><div>{t("Gewinn über Selbstkosten")}</div><div><b class="{cls}">{fmt_num(gain, 2, "€")}</b>{pct}</div></div>'
    prep, post, design = _extras.work_minutes(job)
    hours = (prep + post + design) / 60
    if hours > 0:
        hourly = (rev - (price_base(job) or 0)) / hours
        out += (f'<div class="pr gw"><div>{t("Effektiver Stundenlohn")} <span class="tag">{t("{m} min", m=fmt_input(round(hours * 60)))}</span></div>'
                f'<div><b class="{"pos" if hourly >= 0 else "neg"}">{fmt_num(hourly, 2, "€/h")}</b></div></div>')
    return out


def _price_cell(job: dict[str, Any], pct: float, work_on: bool = False) -> str:
    price = _price(job, pct, work_on)
    q = job.get("quantity")
    per = f'<div class="note">{t("{v}/Stk.", v=fmt_num(price / q, 2, "€"))}</div>' if price is not None and q and q > 1 else ""
    return f'<td class="n">{fmt_num(price, 2, "€")}{per}</td>'


def _customer_price(job: dict[str, Any]) -> str:
    pct = job.get("customer_margin")
    price = _price(job, pct, bool(job.get("customer_work"))) if pct is not None else None
    if price is None:
        return ""
    return (f'<div class="note" title="{e(t("Marge von {name}", name=job.get("customer_name") or ""))}">'
            f'{t("Kunde")} +{fmt_input(pct)} %: <b>{fmt_num(price, 2, "€")}</b></div>')


def _project(job: dict[str, Any]) -> str:
    url, title = job.get("project_url"), job.get("project_title")
    if not url and not title:
        return ""
    label = e(title or t("Projekt"))
    if url and url.startswith(("http://", "https://")):
        return f'<div class="note">🔗 <a href="{e(url)}" target="_blank" rel="noopener">{label}</a></div>'
    return f'<div class="note">🔗 {label}</div>'


def local_link(path: str) -> str:
    """Link für das Protokoll printlog-open (Registry-Eintrag aus den Einstellungen)."""
    return "printlog-open:" + quote(path, safe="")


def _local_path(job: dict[str, Any]) -> str:
    path = job["local_path"]
    return (f'<div class="lp"><code>{e(path)}</code> '
            f'<button type="button" class="btn sm" data-copy="{e(path)}">{t("📋 Kopieren")}</button> '
            f'<a class="btn sm" href="{e(local_link(path))}" title="{e(t("Braucht die einmalige Einrichtung unter Einstellungen"))}">{t("📂 Öffnen")}</a></div>')


def _thumb(job: dict[str, Any], photo: str | None = None, cls: str = "t") -> str:
    src = photo or job.get("image")
    if not src:
        return f'<div class="ph"></div>' if cls == "t" else '<div class="gimg ph"></div>'
    return f'<img class="{cls}" loading="lazy" src="images/{e(src)}" alt="">'


# ---- Drucke -------------------------------------------------------------------

# Projekt: Bild-Mosaik aus den Drucken (Liste, Offen, Detailseite)
MOS_CSS = """
.mos{display:grid!important;grid-template-columns:1fr 1fr;grid-auto-rows:1fr;gap:2px;padding:2px;box-sizing:border-box;background:var(--bg);
 border:1px solid var(--line);overflow:hidden;position:relative}
.mos.one{grid-template-columns:1fr}
.mos img{width:100%;height:100%;object-fit:contain;background:color-mix(in srgb,var(--fg) 4%,var(--bg));border-radius:4px;min-height:0;min-width:0;display:block}
.mos b{position:absolute;right:3px;bottom:3px;background:var(--accent);color:var(--bg);font-size:11px;font-weight:800;border-radius:7px;padding:0 6px;line-height:17px}
.pjb{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 55%,var(--line));background:color-mix(in srgb,var(--accent) 12%,transparent)}
"""

JOBS_CSS = """
.jr.proj{background:linear-gradient(90deg,color-mix(in srgb,var(--accent) 9%,transparent),transparent 55%);box-shadow:inset 3px 0 0 var(--accent)}
.jr .mos{width:64px;height:64px;border-radius:12px;cursor:pointer}
.xp{display:inline-flex;align-items:center;gap:5px;margin-left:8px;font:inherit;font-size:13px;font-weight:700;color:var(--accent);background:none;cursor:pointer;
 border:1px solid color-mix(in srgb,var(--accent) 50%,var(--line));border-radius:999px;padding:1px 10px;min-height:0;vertical-align:2px}
.xp:before{content:"▸";font-size:11px}.jr.open .xp:before{content:"▾"}
.jr.kid{background:color-mix(in srgb,var(--fg) 2.5%,var(--bg));box-shadow:inset 3px 0 0 color-mix(in srgb,var(--accent) 45%,var(--line));padding-top:9px;padding-bottom:9px}
.jr.kid[hidden]{display:none}
.jr.kid>:nth-child(2){padding-left:22px;position:relative}
.jr.kid>:nth-child(2):before{content:"";position:absolute;left:4px;top:-2px;width:11px;height:14px;border-left:2px solid var(--line);border-bottom:2px solid var(--line);border-radius:0 0 0 6px}
.jr.kid .t,.jr.kid .ph{width:46px;height:46px;justify-self:end}.jr.kid .jn{font-size:15px}
.jp.inproj{grid-template-columns:1fr;color:var(--muted);font-size:13px;gap:1px}.jp.inproj b{text-align:left;color:var(--fg);font-size:14px}.jp.inproj .nb{color:var(--muted)}
.mcard .mos{width:64px;height:64px;border-radius:10px;grid-row:span 2}
.mcard.proj{box-shadow:inset 3px 0 0 var(--accent)}
.gcard .mos.gimg{aspect-ratio:1;height:auto;border:0;border-radius:0}
.selbtn.on{background:var(--accent);color:#111;border-color:var(--accent)}
.selmode :is(.jr,.gcard,.mcard){position:relative;user-select:none}
.selmode :is(.jr,.gcard,.mcard)::after{content:"";position:absolute;left:8px;top:8px;width:24px;height:24px;border-radius:50%;z-index:2;
 border:2px solid var(--muted);background:var(--card);box-sizing:border-box;display:grid;place-items:center;font:700 15px/1 system-ui;color:#111}
.selmode :is(.jr,.gcard,.mcard).picked{outline:2px solid var(--accent);outline-offset:-2px;background:color-mix(in srgb,var(--accent) 10%,var(--card))}
.selmode :is(.jr,.gcard,.mcard).picked::after{content:"✓";background:var(--accent);border-color:var(--accent)}
.selmode :is(.jr,.gcard,.mcard) :is(a,button){pointer-events:none}.selmode .jr .xp{pointer-events:auto}
#selbar,#toast{position:fixed;left:50%;transform:translateX(-50%);bottom:16px;z-index:40;display:flex;align-items:center;gap:10px;
 background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 14px;box-shadow:0 10px 30px rgba(0,0,0,.4);
 width:min(640px,calc(100vw - 24px));box-sizing:border-box;flex-wrap:wrap}
#selbar[hidden],#toast[hidden]{display:none}#selbar .sp{flex:1}#toast span{flex:1}
#selbar .del{background:var(--bad);color:#fff;border-color:var(--bad);font-weight:700}#selbar .del:disabled{opacity:.4}
@media(max-width:700px){#selbar{gap:6px;padding:8px 10px}#selbar .none{display:none}#selbar .sp{display:none}#selbar b{flex:1}}
body.selmode{padding-bottom:90px}
#selbar{width:min(960px,calc(100vw - 24px))}#selbar button{white-space:nowrap}
#selbar .pjbtn{background:var(--accent);color:var(--bg);border-color:var(--accent);font-weight:700}#selbar .pjbtn:disabled{opacity:.4}
.dlgbg{position:fixed;inset:0;height:100%;height:100dvh;background:#000a;z-index:60;display:flex;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}
.dlgbg[hidden]{display:none}
.dlg{background:var(--card);border:1px solid var(--line);border-radius:18px;box-shadow:0 30px 80px #000c;width:min(720px,100%);padding:20px 22px;
 max-height:calc(100vh - 32px);max-height:calc(100dvh - 32px);overflow:auto;overscroll-behavior:contain;box-sizing:border-box}
.dlg h3{margin:0 0 4px;font-size:20px}.dlg .sub{color:var(--muted);font-size:14px;margin:0 0 14px}
.dlg .seg2{display:flex;border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-bottom:14px}
.dlg .seg2 button{flex:1;border:0;border-radius:0;background:none;padding:10px 6px;font-weight:700;color:var(--muted);font-size:15px;cursor:pointer}
.dlg .seg2 button+button{border-left:1px solid var(--line)}.dlg .seg2 button.on{background:var(--accent);color:var(--bg)}
.dlg .fr{display:grid;grid-template-columns:1.6fr 1fr 150px;gap:12px;margin-bottom:10px}
.dlg label{display:flex;flex-direction:column;gap:5px;font-size:13px;font-weight:600;color:var(--muted)}
.dlg label input,.dlg label select{width:100%;box-sizing:border-box}
.dlg .stp{display:flex;align-items:stretch;border:1px solid var(--line);border-radius:10px;overflow:hidden;height:var(--ctl-h)}
.dlg .stp button{border:0;border-radius:0;width:40px;min-height:0;padding:0;background:var(--bg);font-size:18px;cursor:pointer}
.dlg .stp input{border:0;border-radius:0;text-align:center;min-width:0;flex:1;-moz-appearance:textfield}
.dlg .hint{font-size:13px;color:var(--muted);margin:0 0 12px}
.dlg .madd{margin-bottom:12px}
.dlg .pl{border:1px solid var(--line);border-radius:12px;overflow:auto;max-height:260px;margin-bottom:14px}
.dlg .pl div{display:flex;gap:10px;align-items:center;padding:6px 12px;font-size:14px;border-bottom:1px solid var(--line)}
.dlg .pl div:last-child{border-bottom:0}
.dlg .pl img,.dlg .pl i{width:34px;height:34px;border-radius:8px;border:1px solid var(--line);object-fit:contain;flex:none}
.dlg .ft{display:flex;gap:10px;justify-content:flex-end;align-items:center;flex-wrap:wrap}.dlg .ft .sp{flex:1;font-size:13px;color:var(--muted);min-width:200px}
@media (max-width:700px){.dlg .fr{grid-template-columns:1fr 1fr}.dlg .fr label:first-child{grid-column:1/-1}#selbar .pjbtn{font-size:14px}}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
.gcard{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;text-decoration:none;color:var(--fg);display:block}
.gcard .gimg{width:100%;aspect-ratio:1;object-fit:contain;background:var(--bg);display:block}
.gcard .gt{padding:8px 10px}.gcard .gt b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gcard .badge{font-size:11px;color:var(--muted)}
.mlist{display:none}
.mcard{display:grid;grid-template-columns:64px minmax(0,1fr) auto;gap:4px 12px;align-items:start;
background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;text-decoration:none;color:var(--fg)}
.mcard:active{border-color:var(--accent)}
.mcard .t,.mcard .ph{width:64px;height:64px;cursor:default;grid-row:span 2}
.mcard .mt{font-weight:600;line-height:1.3;overflow-wrap:anywhere;hyphens:auto;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.mcard .mp{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mcard .mr{text-align:right;white-space:nowrap}.mcard .mr b{display:block;font-size:16px;font-variant-numeric:tabular-nums}
.mcard .mr .s{font-size:12px}
.mcb{display:flex;flex-direction:column;align-items:flex-end;gap:4px;margin-top:4px}.mcb .bdg{font-size:12px;padding:2px 8px}
.mcard .mm{grid-column:2/4;font-size:12px;color:var(--muted)}.mcard .mm .chips{margin-top:0}
.mcard .meta{overflow:hidden}.mcard .meta>div{display:flex;flex-wrap:wrap;margin-left:-14px}
.mcard .meta>div>span{white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}
.mcard .meta>div>span:before{content:"·";display:inline-block;width:14px;text-align:center}
.jk{grid-template-columns:1.4fr repeat(5,minmax(0,1fr));gap:12px}
.jk .kpi{border-radius:14px;padding:12px 16px}.jk .kpi span{display:block;font-size:13px;font-weight:600}
.jk .kpi b{font-size:24px;letter-spacing:-.01em}.jk .kpi small{display:block;color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.jk .kpi.main{border-color:var(--accent);background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 16%,transparent),transparent)}
.jk .kpi.main b{font-size:30px}
.jt{background:var(--card);border:1px solid var(--line);border-radius:16px;font-size:15px;overflow-x:auto}
.jr:last-child{border-radius:0 0 16px 16px}
.jh,.jr{display:grid;grid-template-columns:64px minmax(220px,1fr) 150px 92px 132px 250px 160px;gap:16px;align-items:center;padding:0 18px;min-width:1100px}
.jh{height:42px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:700;border-bottom:1px solid var(--line)}
.jr{padding-top:14px;padding-bottom:14px;border-bottom:1px solid var(--line);cursor:pointer;-webkit-tap-highlight-color:transparent}
.jr:last-child{border-bottom:0}
@media (hover:hover){.jr:hover{background:color-mix(in srgb,var(--fg) 3%,transparent)}}
.jr.run{background:linear-gradient(90deg,color-mix(in srgb,var(--run) 10%,transparent),transparent 45%)}
.jr.scrap .jn{text-decoration:line-through;text-decoration-color:color-mix(in srgb,var(--bad) 60%,transparent)}
.jr.scrap .t,.jr.scrap .ph,.jr.scrap .bar{opacity:.55}.jr.scrap .jn,.jr.scrap .jc b,.jr.scrap .cb>b,.jr.scrap .jp b{color:var(--muted)}
.jr .t,.jr .ph{width:64px;height:64px;border-radius:12px;border:1px solid var(--line);object-fit:contain;cursor:pointer}
.jn{font-size:17px;font-weight:700;line-height:1.25;color:var(--fg);text-decoration:none;overflow-wrap:anywhere}
.jr .chips{margin-top:4px;font-size:13px;gap:2px 12px}
.tags{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:7px}
.tag{display:inline-flex;align-items:center;gap:5px;padding:2px 10px;border-radius:999px;background:var(--bg);border:1px solid var(--line);font-size:13px;font-weight:600;white-space:nowrap;color:var(--fg);text-decoration:none;max-width:100%;overflow:hidden;text-overflow:ellipsis}
a.tag{color:var(--accent)}
.tnote{font-size:13px;color:var(--muted)}.tnote.warn{color:var(--warn)}.tnote.bad{color:var(--bad)}
.jc b{display:block;font-weight:600;white-space:nowrap}.jc span{display:block;font-size:13px;color:var(--muted);white-space:nowrap}
.jc b.prs{white-space:normal;overflow:hidden}
.jc b.prs>span{display:flex;flex-wrap:wrap;margin-left:-1.1em}
.jc b.prs i{font-style:normal;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.jc b.prs i::before{content:"·";display:inline-block;width:1.1em;text-align:center;color:var(--muted)}
.jc.r{text-align:right;font-variant-numeric:tabular-nums}
.jh .r{text-align:right}
.cb{all:unset;box-sizing:border-box;display:block;width:100%;text-align:right;cursor:pointer;position:relative;padding:4px 0;-webkit-tap-highlight-color:transparent}
.cb>b{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.cb .pp{display:block;font-size:12px;color:var(--muted)}
.bar{display:flex;gap:2px;height:7px;margin-top:6px;border-radius:4px;overflow:hidden}
.bar i{display:block;border-radius:2px;min-width:3px}
.c-str{background:var(--s4)}.c-fil{background:var(--s3)}.c-dru{background:var(--s7)}.c-zub{background:var(--s5)}.c-arb{background:var(--s2)}
.pop{display:none;position:fixed;left:0;top:0;z-index:35;min-width:250px;max-width:calc(100vw - 16px);text-align:left;cursor:default;
 background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px;box-shadow:0 8px 28px rgba(0,0,0,.35);font-size:14px}
.cb.open .pop{display:block}
@media (hover:hover){.cb:hover .pop{display:block}}
.pop>div{display:grid;grid-template-columns:12px 1fr auto;gap:8px;align-items:center;padding:4px 0}
.pop>div i{width:10px;height:10px;border-radius:3px}
.pop>div b{font-variant-numeric:tabular-nums;font-weight:600;text-align:right}
.pop small{grid-column:2/-1;margin-top:-4px;font-size:12px;color:var(--muted)}
.pop .sum{border-top:1px solid var(--line);margin-top:4px;padding-top:8px}.pop .sum b{font-weight:700}
.jp{display:grid;grid-template-columns:minmax(0,1fr) auto 64px;gap:3px 10px;align-items:baseline;font-size:14px}
.jp>*{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.jp b{text-align:right;font-variant-numeric:tabular-nums;font-size:15px}
.jp small{font-size:12px;color:var(--muted);text-align:right}
.jp .pct{font-size:12px;color:var(--muted)}
.jp .cu{color:var(--accent)}.jp .fp{color:var(--fg);font-weight:800}
.js{display:flex;flex-direction:column;align-items:flex-end;gap:6px}
.bdg{display:inline-flex;align-items:center;gap:6px;padding:3px 11px;border-radius:999px;font-size:13px;font-weight:700;white-space:nowrap;
 border:1px solid color-mix(in srgb,currentColor 35%,transparent);background:color-mix(in srgb,currentColor 10%,transparent)}
.bdg:before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.bdg.st-ok{color:var(--ok)}.bdg.st-bad{color:var(--bad)}.bdg.st-warn{color:var(--warn)}.bdg.st-run{color:var(--run)}.bdg.st-none{color:var(--muted)}.bdg.st-gift{color:#b07cf0}
.jday{display:flex;align-items:baseline;gap:10px;padding:16px 18px 8px;border-bottom:1px solid var(--line);min-width:1100px;
 background:color-mix(in srgb,var(--bg) 55%,var(--card))}
.jday b{font-size:17px}.jday span{color:var(--muted);font-size:14px}
.mday{display:flex;gap:8px;align-items:baseline;margin:10px 2px 0}.mday b{font-size:15px}.mday span{color:var(--muted);font-size:13px}
.mday:first-child{margin-top:0}.mlist .mday{grid-column:1/-1}
.gbtn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:8px;padding:6px 12px;font-weight:600;color:var(--muted);background:var(--card);cursor:pointer}
.gbtn.on{color:var(--accent);border-color:var(--accent)}
.legend2{display:flex;gap:14px;font-size:12px;color:var(--muted);margin:8px 4px 0}
.legend2 i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:-1px}
/* Tablet quer (z. B. 1180 px): alle Spalten, nur schmaler */
@media (min-width:1151px) and (max-width:1300px){.jh,.jr{grid-template-columns:56px minmax(200px,1fr) 128px 86px 120px 220px 140px;gap:12px;padding-left:14px;padding-right:14px}
 .jh,.jr,.jday{min-width:0}.jr .t,.jr .ph{width:56px;height:56px}}
@media (max-width:1150px){.jh,.jr,.jday{min-width:880px}
 .jh,.jr{grid-template-columns:56px minmax(200px,1fr) 130px 124px 230px 150px;gap:12px;padding-left:14px;padding-right:14px}
 .jh>:nth-child(4),.jr>:nth-child(4){display:none}.jr .t,.jr .ph{width:56px;height:56px}}
@media (max-width:1300px){.jk{grid-template-columns:repeat(3,minmax(0,1fr))}.jk .kpi.main{grid-column:span 1}}
@media (min-width:1301px) and (max-width:1500px){.jk .kpi b{font-size:22px}.jk .kpi.main b{font-size:27px}}
@media (min-width:701px) and (max-width:900px){.jt,.legend2{display:none}.mlist{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
@media (max-width:700px){
.jk{grid-template-columns:repeat(3,minmax(0,1fr))}.jk .kpi small{display:none}.jk .kpi b,.jk .kpi.main b{font-size:15px}.jk .kpi span{font-size:11px}.jk .kpi{padding:7px 9px;border-radius:10px}
.legend2{display:none}
.jt{display:none}.mlist{display:grid;gap:8px}
}
/* Filterleiste: große Suche, Ansichten als Icons, Filter als Chips */
form.fb{display:flex;flex-direction:column;flex-wrap:nowrap;align-items:stretch;gap:10px;margin-bottom:14px}
.fbrow{display:flex;gap:8px;align-items:center;min-width:0;width:100%}
.fsearch{flex:1 1 0;position:relative;min-width:0;display:block}
.fsearch svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none}
form.fb input.search{width:100%;max-width:none;min-width:0;box-sizing:border-box;padding-left:38px}
form.fb .vseg{height:var(--ctl-h);box-sizing:border-box;border-radius:var(--ctl-r);background:var(--card);flex:none}
form.fb .vseg a{display:flex;align-items:center;gap:7px;padding:0 12px;border-radius:0}
form.fb .vseg a+a{border-left:1px solid var(--line)}
form.fb .vseg a.on{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent)}
.vseg .cnt{background:var(--warn);color:#111;border-radius:999px;font-size:11px;font-weight:800;padding:0 6px;line-height:17px}
.vseg .cnt[hidden]{display:none}
.ib{display:inline-flex;align-items:center;justify-content:center;width:var(--ctl-h);height:var(--ctl-h);min-height:0;padding:0;flex:none;
 border:1px solid var(--line);border-radius:var(--ctl-r);background:var(--card);color:var(--fg)}
.ib.on{color:var(--accent);border-color:var(--accent);background:color-mix(in srgb,var(--accent) 14%,var(--card))}
.selbtn.ib.on{color:var(--bg);background:var(--accent)}
form.fb.todo .gbtn,form.fb.todo .selbtn{display:none}
.pills{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.pill{position:relative;display:inline-flex;align-items:center;height:34px;padding:0 13px;border:1px solid var(--line);border-radius:999px;
 background:var(--card);font-size:14px;font-weight:600;white-space:nowrap;box-sizing:border-box;flex:none}
.pill .pv{display:inline-flex;align-items:center;gap:6px;pointer-events:none}
.pill .car{color:var(--muted);font-size:11px}
.pill select{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer;border:0;padding:0}
.pill .px{display:none}
.pill.on{background:color-mix(in srgb,var(--accent) 16%,transparent);border-color:color-mix(in srgb,var(--accent) 60%,transparent);color:var(--accent);padding-right:5px}
.pill.on .pv b{color:var(--fg);font-weight:700;max-width:180px;overflow:hidden;text-overflow:ellipsis}
.pill.on .px{display:inline-grid;place-items:center;position:relative;z-index:1;width:24px;height:24px;min-height:0;margin-left:6px;padding:0;border:0;border-radius:50%;
 background:color-mix(in srgb,var(--accent) 28%,transparent);color:var(--fg);font-size:11px;cursor:pointer}
form.fb .fclear{border:0;background:none;color:var(--muted);text-decoration:underline;font-size:14px;padding:0 4px;min-height:0;cursor:pointer}
form.fb .fclear[hidden]{display:none}
.pills .conn{margin-left:auto;font-size:13px;color:var(--muted);white-space:nowrap}
@media (max-width:700px){.fbrow{flex-wrap:wrap}.fsearch{flex:1 0 100%}form.fb .vseg{margin-right:auto}form.fb .vseg a:not(.on) .t{display:none}form.fb .vseg a{padding:0 12px}
 .pills{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;margin:0 -12px;padding:0 12px 2px}.pills::-webkit-scrollbar{display:none}
 .pills .conn{display:none}}
"""

JOBS_JS = """
const form=document.querySelector('form.f');
let timer=null,ctrl=null;
async function refresh(){
  const params=new URLSearchParams(new FormData(form));
  for(const [k,v] of [...params]) if(!v) params.delete(k);
  history.replaceState(null,'',params.toString()?'?'+params:'./');
  params.set('partial','1');
  if(ctrl) ctrl.abort(); ctrl=new AbortController();
  try{const r=await fetch('./?'+params,{signal:ctrl.signal});
    document.getElementById('results').innerHTML=await r.text();bind();}catch(_){}
}
form.querySelectorAll('select').forEach(s=>s.onchange=refresh);
form.querySelector('input[name=q]').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(refresh,200)});
// Enter auf einer Projekt-ID (z. B. "pl-7k3q" oder "7K3Q") öffnet den Druck direkt
const CODE_RE=/^\\s*(pl[-\\s]?)?[2-9a-hj-km-np-z]{4}\\s*$/i;
form.onsubmit=ev=>{ev.preventDefault();const v=form.querySelector('input[name=q]').value;
  if(CODE_RE.test(v)){fetch('go?json=1&q='+encodeURIComponent(v)).then(r=>r.json()).then(d=>{if(d.id)location.href='job?id='+d.id;else refresh()}).catch(refresh)}
  else refresh()};
// Filter-Chips: Beschriftung zeigt den gewählten Wert, ✕ entfernt den Filter
const pills=[...form.querySelectorAll('.pill')],fclear=form.querySelector('.fclear');
function pillText(p){const s=p.querySelector('select'),v=p.querySelector('.pv');p.classList.toggle('on',!!s.value);
  v.textContent='';if(s.value){v.append(p.dataset.label+': ');const b=document.createElement('b');b.textContent=s.selectedOptions[0].text;v.append(b)}
  else{v.append(p.dataset.label);const c=document.createElement('span');c.className='car';c.textContent='▾';v.append(c)}}
function pillsDone(){pills.forEach(pillText);fclear.hidden=!pills.some(p=>p.querySelector('select').value)&&!form.querySelector('input[name=day]')}
pills.forEach(p=>{p.querySelector('select').addEventListener('change',pillsDone);
  p.querySelector('.px').onclick=ev=>{ev.preventDefault();p.querySelector('select').value='';pillsDone();refresh()}});
fclear.onclick=()=>{pills.forEach(p=>p.querySelector('select').value='');const d=form.querySelector('input[name=day]');if(d)d.remove();pillsDone();refresh()};
if(matchMedia('(min-width:701px)').matches)form.querySelector('input[name=q]').focus();
document.querySelectorAll('.seg a[data-view]').forEach(a=>a.onclick=ev=>{ev.preventDefault();
  form.querySelector('input[name=view]').value=a.dataset.view;form.classList.toggle('todo',a.dataset.view==='todo'||a.dataset.view==='stock');
  document.querySelectorAll('.seg a').forEach(x=>x.classList.toggle('on',x===a));refresh()});
function bind(){
  bindAct(refresh);
  const oc=document.getElementById('opendata'),cnt=document.getElementById('opencount');
  if(oc&&cnt){cnt.textContent=oc.dataset.n;cnt.hidden=oc.dataset.n==='0'}
  document.querySelectorAll('.jr[data-href]').forEach(r=>r.onclick=ev=>{
    if(ev.target.closest('a,button,input,select,label'))return;location.href=r.dataset.href});
  markPicked();
  let op=[];try{op=JSON.parse(sessionStorage.getItem('pl_open')||'[]')}catch(_){}
  document.querySelectorAll('.xp').forEach(b=>{const pid=b.dataset.pid;if(op.includes(pid))setOpen(pid,true,false);
    b.onclick=ev=>{ev.stopPropagation();ev.preventDefault();setOpen(pid,!b.closest('.jr').classList.contains('open'),true)}});
  document.querySelectorAll('.cb').forEach(b=>{b.onclick=ev=>{ev.stopPropagation();
    const o=b.classList.contains('open');document.querySelectorAll('.cb.open').forEach(x=>x.classList.remove('open'));
    if(!o){b.classList.add('open');placePop(b)}};
    b.onmouseenter=()=>placePop(b)});
}
// Kosten-Aufschlüsselung schwebt (position:fixed) über der Tabelle, damit der Tabellen-Rahmen
// (overflow-x:auto) sie nicht abschneidet und keinen Scrollbalken bekommt. Öffnet nach unten,
// nach oben wenn unten kein Platz ist, und bleibt rechtsbündig am Balken im sichtbaren Bereich.
function placePop(b){const p=b.querySelector('.pop');if(!p)return;
  p.style.visibility='hidden';p.style.display='block';
  const r=b.getBoundingClientRect(),w=p.offsetWidth,h=p.offsetHeight,m=8,g=6;
  let x=Math.min(Math.max(m,r.right+8-w),innerWidth-w-m),y=r.bottom+g;
  if(y+h>innerHeight-m)y=r.top-g-h>=m?r.top-g-h:Math.max(m,innerHeight-m-h);
  p.style.left=Math.max(m,x)+'px';p.style.top=y+'px';p.style.display='';p.style.visibility=''}
addEventListener('scroll',()=>document.querySelectorAll('.cb.open,.cb:hover').forEach(placePop),{capture:true,passive:true});
addEventListener('resize',()=>document.querySelectorAll('.cb.open').forEach(placePop));
document.addEventListener('click',ev=>{if(!ev.target.closest('.cb'))document.querySelectorAll('.cb.open').forEach(x=>x.classList.remove('open'))});
// Projekt auf-/zuklappen (gemerkt für diese Sitzung)
function setOpen(pid,on,save){const row=document.querySelector('.jr.proj[data-href="job?id='+pid+'"]');if(!row)return;
  row.classList.toggle('open',on);row.querySelector('.xp').setAttribute('aria-expanded',on);
  document.querySelectorAll('.jr.kid[data-parent="'+pid+'"]').forEach(k=>k.hidden=!on);
  if(save){let op=[];try{op=JSON.parse(sessionStorage.getItem('pl_open')||'[]')}catch(_){}
    op=op.filter(x=>x!==pid);if(on)op.push(pid);try{sessionStorage.setItem('pl_open',JSON.stringify(op))}catch(_){}}}
const gb=form.querySelector('.gbtn');
if(gb)gb.onclick=()=>{const gi=form.querySelector('input[name=group]');gi.value=gi.value?'':'day';
  gb.classList.toggle('on',!!gi.value);document.cookie='pl_group='+(gi.value||'none')+';path=/;max-age=31536000;SameSite=Lax';refresh()};
// Mehrfachauswahl: Zeilen/Karten antippen, dann gesammelt in den Papierkorb
const picked=new Set(),sb=document.getElementById('selbar'),selBtn=form.querySelector('.selbtn'),toast=document.getElementById('toast');
const ITEMS='.jr[data-href],.gcard,.mcard';
function idOf(el){const m=(el.dataset.href||el.getAttribute('href')||'').match(/id=(\\d+)/);return m?m[1]:null}
function markPicked(){document.querySelectorAll(ITEMS).forEach(el=>el.classList.toggle('picked',picked.has(idOf(el))));
  sb.querySelector('.n').textContent=picked.size;sb.querySelector('.del').disabled=!picked.size;sb.querySelector('.pjbtn').disabled=!picked.size;sb.querySelector('.kbtn').disabled=!picked.size}
function setSel(on){document.body.classList.toggle('selmode',on);selBtn.classList.toggle('on',on);sb.hidden=!on;if(!on)picked.clear();markPicked()}
selBtn.onclick=()=>setSel(!document.body.classList.contains('selmode'));
document.addEventListener('click',ev=>{if(!document.body.classList.contains('selmode')||ev.target.closest('.xp'))return;
  const el=ev.target.closest(ITEMS);if(!el)return;ev.preventDefault();ev.stopPropagation();
  const id=idOf(el);if(!id)return;picked.has(id)?picked.delete(id):picked.add(id);markPicked()},true);
sb.querySelector('.all').onclick=()=>{document.querySelectorAll(ITEMS).forEach(el=>{const id=idOf(el);if(id&&el.offsetParent)picked.add(id)});markPicked()};
sb.querySelector('.none').onclick=()=>{picked.clear();markPicked()};
sb.querySelector('.cancel').onclick=()=>setSel(false);
// Projekt bilden: Dialog mit Name, Kunde, Stückzahl oder Auswahl eines vorhandenen Projekts
const dlg=document.getElementById('pjdlg'),df=dlg.querySelector('form');
function pickedEls(){const out=[];picked.forEach(id=>{const el=document.querySelector('.jr[data-href="job?id='+id+'"]')||document.querySelector('[href="job?id='+id+'"].mcard,[href="job?id='+id+'"].gcard');out.push([id,el])});return out}
function dlgMode(m){df.querySelectorAll('.seg2 button').forEach(b=>b.classList.toggle('on',b.dataset.m===m));
  df.querySelector('.mnew').hidden=m!=='new';df.querySelector('.madd').hidden=m!=='add';df.querySelector('input[name=name]').required=m==='new';
  df.dataset.m=m;df.querySelector('.go').textContent=m==='new'?_t("Projekt anlegen"):_t("Hinzufügen")}
df.querySelectorAll('.seg2 button').forEach(b=>b.onclick=()=>dlgMode(b.dataset.m));
df.querySelectorAll('.stp button').forEach(b=>b.onclick=()=>{const i=df.querySelector('input[name=quantity]');i.value=Math.max(1,(+i.value||1)+(+b.dataset.d))});
df.querySelector('.cancel').onclick=()=>dlg.hidden=true;
dlg.onclick=ev=>{if(ev.target===dlg)dlg.hidden=true};
sb.querySelector('.pjbtn').onclick=()=>{const els=pickedEls();if(!els.length)return;
  const projs=els.filter(([,el])=>el&&el.dataset.kind==='project'),prints=els.filter(([,el])=>!el||el.dataset.kind!=='project');
  const name=(prints.find(([,el])=>el)||projs[0]||[])[1];
  df.querySelector('input[name=name]').value=name?name.dataset.name:'';df.querySelector('input[name=quantity]').value=1;
  const cnt=new Map();els.forEach(([,el])=>{const c=el&&el.dataset.cust;if(c)cnt.set(c,(cnt.get(c)||0)+1)});
  let best='',bn=0;cnt.forEach((n,c)=>{if(n>bn){best=c;bn=n}});df.querySelector('select[name=customer_id]').value=best;
  const pl=df.querySelector('.pl');pl.textContent='';
  els.forEach(([id,el])=>{const d=document.createElement('div');const im=el&&el.querySelector('img');
    if(im){const i2=document.createElement('img');i2.src=im.src;d.appendChild(i2)}else d.appendChild(document.createElement('i'));
    const n=document.createElement('span');n.textContent=(el&&el.dataset.kind==='project'?'📦 ':'')+(el?el.dataset.name:'#'+id);d.appendChild(n);pl.appendChild(d)});
  const n=prints.length;df.querySelector('.ttl').textContent=n==1?_t("Projekt aus 1 Druck"):_t("Projekt aus {n} Drucken").replace('{n}',n);
  const into=df.querySelector('select[name=into]');
  if(projs.length===1&&into){into.value=projs[0][0];dlgMode('add')}else dlgMode('new');
  dlg.hidden=false;if(df.dataset.m==='new')df.querySelector('input[name=name]').select()};
df.onsubmit=async ev=>{ev.preventDefault();const fd=new FormData(df);fd.append('ids',[...picked].join(','));fd.append('mode',df.dataset.m);
  const btn=df.querySelector('.go');btn.disabled=true;
  try{const r=await fetch('api/projects',{method:'POST',body:fd});const d=await r.json();if(d.id){location.href='job?id='+d.id;return}}catch(_){}
  btn.disabled=false};
let toastTimer=null;
function showToast(msg,undo){toast.querySelector('span').textContent=msg;toast.hidden=false;
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>toast.hidden=true,10000);
  toast.querySelector('button').onclick=async()=>{toast.hidden=true;await undo();refresh()}}
// Kunde zuordnen: Chip in der Zeile/Karte oder „Kunde zuweisen“ in der Auswahlleiste
function assigned(d,what){refresh();
  showToast(d.customer_id?what+' → '+d.name:_t("{what}: Kunde entfernt").replace('{what}',what),()=>KP.undo(d.prev))}
document.addEventListener('click',ev=>{kpChip(ev,(d,el)=>{const c=el.closest('[data-kname]')||el.closest('.kchip')&&el.closest('.kchip').querySelector('[data-kname]');
  assigned(d,(c&&c.dataset.kname)||_t("Druck"))})},true);
sb.querySelector('.kbtn').onclick=ev=>{ev.stopPropagation();const ids=[...picked];if(!ids.length)return;
  const n=ids.length==1?_t("1 Druck"):_t("{n} Drucke").replace('{n}',ids.length);
  KP.open(sb.querySelector('.kbtn'),{ids,title:n,prefix:_t("Kunde zuweisen für")+' ',bulk:true,done:d=>{setSel(false);assigned(d,n)}})};
sb.querySelector('.del').onclick=async()=>{const n=picked.size;if(!n)return;
  if(!confirm((n==1?_t("1 Druck in den Papierkorb legen? Du kannst ihn dort wiederherstellen."):_t("{n} Drucke in den Papierkorb legen? Du kannst sie dort wiederherstellen.").replace('{n}',n))))return;
  const fd=new FormData();fd.append('ids',[...picked].join(','));
  const r=await fetch('api/jobs/delete',{method:'POST',body:fd});if(!r.ok)return;const ids=(await r.json()).ids;
  setSel(false);await refresh();
  toast.querySelector('span').textContent=(ids.length==1?_t("1 Druck im Papierkorb."):_t("{n} Drucke im Papierkorb.").replace('{n}',ids.length));toast.hidden=false;
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>toast.hidden=true,10000);
  toast.querySelector('button').onclick=async()=>{const f2=new FormData();f2.append('ids',ids.join(','));f2.append('json','1');
    await fetch('trash/restore',{method:'POST',body:f2});toast.hidden=true;refresh()}};
bind();
"""


# ---- Kunden-Schnellauswahl -------------------------------------------------------
# Chip „👤 ＋ Kunde“ bzw. „👤 Name ▾“ in Liste, Handy-Karten und Detailkopf; die Auswahlleiste
# hat „👤 Kunde zuweisen“. Alle öffnen dasselbe Fenster (KPICK_JS), Antippen speichert sofort.

def customer_picker(customers: list[dict[str, Any]]) -> str:
    """Kundenliste für das Auswahlfenster (zuletzt benutzte zuerst unter „Zuletzt“)."""
    data = [{"id": c["id"], "name": c["name"], "last": c.get("last_ts") or 0, "ago": _ago(c["last_ts"]) if c.get("last_ts") else ""}
            for c in customers]
    raw = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/json" id="kdata">{raw}</script>'


def add_suggestions(store: Store, jobs: list[dict[str, Any]]) -> None:
    """Hängt an Drucke ohne Kunde den Vorschlag (_sug) für das gleiche Modell."""
    todo = [j for j in jobs if not j.get("customer_id") and j.get("kind") != "project"]
    if not todo:
        return
    sug = store.customer_suggestions([j.get("task_name") or "" for j in todo])
    for j in todo:
        s = sug.get(j.get("task_name") or "")
        if s:
            j["_sug"] = s


def cust_chip(j: dict[str, Any], cls: str = "tag") -> str:
    """Kunden-Chip: antippen öffnet die Schnellauswahl; mit Vorschlag übernimmt ✓ ihn direkt."""
    if j.get("split") and j.get("kind") != "share":
        return _split.split_chips(j)
    attrs = f'data-kid="{j["id"]}" data-kname="{e(j.get("task_name") or "")}"'
    if j.get("customer_id") and j.get("customer_name"):
        return (f'<span role="button" tabindex="0" class="{cls} kset kc" {attrs} data-cur="{j["customer_id"]}" '
                f'title="{e(t("Kunde ändern"))}">👤 {e(j["customer_name"])}</span>')
    s = j.get("_sug")
    if s:
        why = tn(s[2], "gleiches Modell {n}×", "gleiches Modell {n}×")
        return (f'<span class="kchip sug" title="{e(t("Vorschlag: {why}", why=why))}">'
                f'<span role="button" tabindex="0" class="kc" {attrs} data-sug="{s[0]}" data-why="{e(why)}">👤 {e(s[1])}<i>?</i></span>'
                f'<span role="button" tabindex="0" class="kok" data-kid="{j["id"]}" data-sug="{s[0]}" '
                f'aria-label="{e(t("{name} übernehmen", name=s[1]))}" title="{e(t("{name} übernehmen", name=s[1]))}">✓</span></span>')
    return f'<span role="button" tabindex="0" class="kchip kc" {attrs}>👤 {t("＋ Kunde")}</span>'


KPICK_CSS = """
.kchip{display:inline-flex;align-items:center;gap:6px;padding:2px 11px;border-radius:999px;font-size:13px;font-weight:700;white-space:nowrap;cursor:pointer;
 border:1.5px dashed color-mix(in srgb,var(--accent) 60%,var(--line));color:var(--accent);background:transparent;-webkit-tap-highlight-color:transparent;max-width:100%}
.kchip:hover{background:color-mix(in srgb,var(--accent) 10%,transparent)}
.kchip.sug{border-style:solid;background:color-mix(in srgb,var(--accent) 12%,transparent);padding:0;gap:0;overflow:hidden}
.kchip.sug .kc{padding:2px 6px 2px 11px;overflow:hidden;text-overflow:ellipsis}.kchip.sug i{font-style:normal;color:var(--muted);font-weight:600;margin-left:3px}
.kchip.sug .kok{background:var(--accent);color:var(--bg);border-radius:999px;min-width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center;font-size:12px;margin:1px 2px 1px 0}
.kchip.sug .kok:hover{filter:brightness(1.12)}
.kset{cursor:pointer}.kset:after{content:"▾";color:var(--muted);font-size:11px;margin-left:2px}.kset:hover{border-color:var(--accent)}
.kc.act,.kchip:has(.kc.act){outline:2px solid var(--accent);outline-offset:2px}
.selmode .kc,.selmode .kok{pointer-events:none}
#selbar .kbtn{font-weight:700;color:var(--accent);border-color:var(--accent)}#selbar .kbtn:disabled{opacity:.4}#selbar .bs{display:none}
@media (max-width:700px){#selbar .bl{display:none}#selbar .bs{display:inline}#selbar .kbtn{font-size:14px}#selbar .all{display:none}
 #selbar{flex-wrap:nowrap}#selbar b{font-size:14px;line-height:1.2}#selbar button{padding-left:10px;padding-right:10px}}
.mcard .kchip{margin-top:6px;width:max-content;font-size:14px;padding:4px 12px}.mcard .kchip.sug{padding:0}.mcard .kchip.sug .kok{min-width:28px;height:28px}
.badges .kchip{font-size:14px;padding:5px 13px}.badges .kchip.sug{padding:0}.badges .kchip.sug .kc{padding:5px 8px 5px 13px}.badges .kchip.sug .kok{min-width:28px;height:28px;margin:2px 3px 2px 0}
#kpop{position:absolute;z-index:70;width:340px;max-width:calc(100vw - 16px);background:var(--card);border:1px solid var(--line);border-radius:16px;
 box-shadow:0 24px 70px rgba(0,0,0,.45);padding:12px;font-size:14px;display:flex;flex-direction:column}
#kpop[hidden],#kbg[hidden]{display:none}
#kpop .hd{font-size:12.5px;color:var(--muted);margin:0 4px 8px;line-height:1.35}#kpop .hd b{color:var(--fg)}
#kpop .srch{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:11px;padding:0 12px;background:var(--bg);margin-bottom:6px}
#kpop .srch:focus-within{border-color:var(--accent)}
#kpop .srch input{flex:1;border:0;background:transparent;color:var(--fg);font:inherit;padding:10px 0;outline:0;min-width:0}
#kpop .lst{overflow:auto;max-height:min(360px,50vh);overscroll-behavior:contain}
#kpop .lbl{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);font-weight:700;margin:10px 6px 4px}
#kpop .it{display:flex;align-items:center;gap:10px;padding:8px;border-radius:10px;font-weight:600;color:var(--fg);cursor:pointer;width:100%;
 border:1px solid transparent;background:none;font:inherit;font-weight:600;text-align:left}
#kpop .it:hover,#kpop .it.hl{background:color-mix(in srgb,var(--fg) 7%,transparent)}
#kpop .it .av{width:28px;height:28px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:11.5px;font-weight:800;color:#111;flex:none}
#kpop .it span.n{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#kpop .it small{margin-left:auto;color:var(--muted);font-weight:500;font-size:12px;white-space:nowrap;padding-left:8px}
#kpop .it.sg{background:color-mix(in srgb,var(--accent) 13%,transparent);border-color:color-mix(in srgb,var(--accent) 45%,var(--line))}
#kpop .it.sg small,#kpop .it.cur small{color:var(--accent)}
#kpop .it.nw{color:var(--accent)}
#kpop .ft{display:flex;gap:8px;margin-top:10px;border-top:1px solid var(--line);padding-top:10px}
#kpop .ft button{flex:1;border:1px solid var(--line);border-radius:10px;padding:9px;font-weight:700;font-size:13.5px;background:var(--bg);color:var(--fg);cursor:pointer}
#kpop .ft .nwb{color:var(--accent)}#kpop .ft button[hidden]{display:none}
#kpop .nwf{display:flex;gap:8px;margin-top:10px;border-top:1px solid var(--line);padding-top:10px}#kpop .nwf[hidden]{display:none}
#kpop .nwf input{flex:1;min-width:0;border:1px solid var(--accent);border-radius:10px;background:var(--bg);color:var(--fg);font:inherit;padding:9px 12px}
#kpop .nwf button{border:0;border-radius:10px;background:var(--accent);color:var(--bg);font-weight:800;padding:9px 14px;cursor:pointer}
#kpop .none{color:var(--muted);padding:10px 8px}
#kbg{position:fixed;inset:0;z-index:69;background:transparent}
@media (max-width:700px){
 #kbg{background:rgba(0,0,0,.55)}
 #kpop{position:fixed;left:0!important;right:0;bottom:0;top:auto!important;width:auto;max-width:none;border-radius:22px 22px 0 0;padding:10px 16px calc(18px + env(safe-area-inset-bottom));font-size:16px}
 #kpop:before{content:"";display:block;width:44px;height:5px;border-radius:3px;background:var(--line);margin:0 auto 12px}
 #kpop .it{padding:11px 8px}#kpop .it .av{width:34px;height:34px;font-size:13px}#kpop .lst{max-height:46vh}
 #kpop .srch input{font-size:16px}}
"""

KPICK_JS = r"""
// Kunden-Schnellauswahl: ein Fenster für Listen-Chips, Detailkopf und Mehrfachauswahl
const KP=(()=>{
  let custs=[];try{custs=JSON.parse(document.getElementById('kdata').textContent)}catch(_){}
  const COL=['#7dd3fc','#fda4af','#fcd34d','#a5b4fc','#fdba74','#86efac','#f0abfc','#5eead4','#fca5a5','#c4b5fd'];
  const col=n=>{let h=0;for(const ch of n)h=(h*31+ch.charCodeAt(0))>>>0;return COL[h%COL.length]};
  const ini=n=>n.replace(/[^\p{L}\p{N}\s-]/gu,'').split(/[\s-]+/).filter(Boolean).slice(0,2).map(x=>x[0]).join('').toUpperCase();
  const bg=document.createElement('div');bg.id='kbg';bg.hidden=true;
  const pop=document.createElement('div');pop.id='kpop';pop.hidden=true;pop.setAttribute('role','dialog');
  pop.innerHTML='<div class="hd"></div><label class="srch">🔍<input type="search" autocomplete="off" placeholder='+JSON.stringify(_t("Kunde suchen …"))+'></label>'+
    '<div class="lst"></div><div class="ft"><button type="button" class="nwb">'+_t("＋ Neuer Kunde")+'</button><button type="button" class="nob">'+_t("Ohne Kunde")+'</button></div>'+
    '<form class="nwf" hidden><input name="n" maxlength="120" required placeholder='+JSON.stringify(_t("Name des Kunden"))+'><button>'+_t("Anlegen")+'</button></form>';
  document.body.append(bg,pop);
  const q=pop.querySelector('input[type=search]'),lst=pop.querySelector('.lst'),nwf=pop.querySelector('.nwf');
  let cur=null,anchor=null;
  function item(c,small,cls){const b=document.createElement('button');b.type='button';b.className='it'+(cls?' '+cls:'');b.dataset.id=c.id;
    const av=document.createElement('span');av.className='av';av.style.background=col(c.name);av.textContent=ini(c.name);
    const n=document.createElement('span');n.className='n';n.textContent=c.name;b.append(av,n);
    if(small){const s=document.createElement('small');s.textContent=small;b.append(s)}return b}
  function label(txt){const d=document.createElement('div');d.className='lbl';d.textContent=txt;return d}
  function render(){lst.textContent='';const term=q.value.trim().toLowerCase();const cid=cur.cur?+cur.cur:null;
    if(term){const m=custs.filter(c=>c.name.toLowerCase().includes(term));
      m.forEach((c,i)=>lst.append(item(c,c.id===cid?'✓':'',(i===0?'hl ':'')+(c.id===cid?'cur':''))));
      if(!custs.some(c=>c.name.toLowerCase()===term)){const b=document.createElement('button');b.type='button';b.className='it nw'+(m.length?'':' hl');
        b.dataset.new=q.value.trim();b.textContent=_t("＋ „{name}“ als neuen Kunden anlegen").replace('{name}',q.value.trim());lst.append(b)}
      return}
    const sug=cur.sug?custs.find(c=>c.id===+cur.sug):null;
    if(sug){lst.append(label(_t("Vorschlag")),item(sug,cur.why||'','sg'+(sug.id===cid?' cur':'')))}
    const rec=custs.filter(c=>c.last&&c!==sug).sort((a,b)=>b.last-a.last).slice(0,3);
    if(rec.length){lst.append(label(_t("Zuletzt")));rec.forEach(c=>lst.append(item(c,c.id===cid?'✓ '+c.ago:c.ago,c.id===cid?'cur':'')))}
    lst.append(label(_t("Alle Kunden")));
    if(!custs.length){const d=document.createElement('div');d.className='none';d.textContent=_t("Noch keine Kunden.");lst.append(d)}
    custs.forEach(c=>lst.append(item(c,c.id===cid?'✓':'',c.id===cid?'cur':'')))}
  function place(){if(innerWidth<=700){pop.style.left='';pop.style.top='';return}
    const r=anchor.getBoundingClientRect(),w=pop.offsetWidth,h=pop.offsetHeight;
    let left=Math.min(Math.max(8,r.left),innerWidth-w-8),top=r.bottom+8;
    if(top+h>innerHeight-8&&r.top-h-8>8)top=r.top-h-8;else if(top+h>innerHeight-8)top=Math.max(8,innerHeight-h-8);
    pop.style.left=(left+scrollX)+'px';pop.style.top=(top+scrollY)+'px'}
  function open(a,opt){close();anchor=a;cur=opt;a.classList.add('act');
    const hd=pop.querySelector('.hd');hd.textContent='';
    if(opt.title){hd.append(opt.prefix||_t("Kunde für")+' ');const b=document.createElement('b');b.textContent=opt.title;hd.append(b)}
    q.value='';nwf.hidden=true;pop.querySelector('.ft').hidden=false;pop.querySelector('.nob').hidden=!opt.cur&&!opt.bulk;
    render();bg.hidden=false;pop.hidden=false;place();
    if(matchMedia('(pointer:fine)').matches)q.focus()}
  function close(){if(pop.hidden)return;pop.hidden=true;bg.hidden=true;if(anchor)anchor.classList.remove('act');anchor=null}
  async function save(fd){const o=cur;close();if(o.onpick){o.onpick(fd);return}fd.append('ids',o.ids.join(','));
    try{const r=await fetch('api/customers/assign',{method:'POST',body:fd});if(!r.ok)return;const d=await r.json();
      if(d.customer_id&&!custs.some(c=>c.id===d.customer_id))custs.push({id:d.customer_id,name:d.name,last:0,ago:''});
      const c=custs.find(x=>x.id===d.customer_id);if(c){c.last=Date.now()/1000;c.ago=_t("heute")}
      custs.sort((a,b)=>a.name.localeCompare(b.name));o.done&&o.done(d)}catch(_){}}
  function pick(id){const fd=new FormData();fd.append('customer_id',id==null?'':id);save(fd)}
  lst.addEventListener('click',ev=>{const b=ev.target.closest('.it');if(!b)return;
    if(b.dataset.new!==undefined){const fd=new FormData();fd.append('new_name',b.dataset.new);save(fd)}else pick(+b.dataset.id)});
  q.addEventListener('input',render);
  q.addEventListener('keydown',ev=>{if(ev.key==='Enter'){ev.preventDefault();const b=lst.querySelector('.it.hl')||lst.querySelector('.it');if(b)b.click()}});
  pop.querySelector('.nob').onclick=()=>pick(null);
  pop.querySelector('.nwb').onclick=()=>{nwf.hidden=false;pop.querySelector('.ft').hidden=true;const i=nwf.querySelector('input');i.value=q.value.trim();i.focus()};
  nwf.onsubmit=ev=>{ev.preventDefault();const n=nwf.querySelector('input').value.trim();if(!n)return;const fd=new FormData();fd.append('new_name',n);save(fd)};
  bg.onclick=close;document.addEventListener('keydown',ev=>{if(ev.key==='Escape')close()});
  addEventListener('resize',()=>{if(!pop.hidden&&anchor)place()});
  // Rückgängig: frühere Kunden wiederherstellen (nach Kunde gruppiert)
  async function undo(prev){const by=new Map();prev.forEach(([id,c])=>{const k=c==null?'':String(c);by.set(k,[...(by.get(k)||[]),id])});
    for(const [c,ids] of by){const fd=new FormData();fd.append('ids',ids.join(','));fd.append('customer_id',c);
      await fetch('api/customers/assign',{method:'POST',body:fd})}}
  return {open,close,pick,save,undo,get custs(){return custs}};
})();
// Chips: Antippen öffnet die Auswahl, ✓ übernimmt den Vorschlag
function kpChip(ev,done){
  if(document.body.classList.contains('selmode'))return false;
  const ok=ev.target.closest('.kok');
  if(ok){ev.preventDefault();ev.stopPropagation();const fd=new FormData();fd.append('customer_id',ok.dataset.sug);
    KP.open(ok,{ids:[ok.dataset.kid],done:d=>done&&done(d,ok)});KP.save(fd);return true}
  const c=ev.target.closest('.kc');if(!c)return false;ev.preventDefault();ev.stopPropagation();
  KP.open(c,{ids:[c.dataset.kid],title:c.dataset.kname,cur:c.dataset.cur,sug:c.dataset.sug,why:c.dataset.why,done:d=>done&&done(d,c)});return true}
document.addEventListener('keydown',ev=>{if((ev.key==='Enter'||ev.key===' ')&&ev.target.matches('.kc,.kok')){ev.preventDefault();ev.target.click()}});
"""


KPICK_DETAIL_JS = """
// Detailseite: nach dem Zuordnen neu laden (Preise, Abrechnung, Tabs ändern sich)
document.addEventListener('click',ev=>{kpChip(ev,d=>{const u=new URL(location.href);u.searchParams.set('saved','1');u.hash='';location.replace(u)})},true);
"""


I_SEARCH = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
            '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>')
I_TABLE = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">'
           '<path d="M4 6h16M4 12h16M4 18h16"/></svg>')
I_GRID = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
          '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/>'
          '<rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></svg>')
I_TODO = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">'
          '<path d="M10 6h10M10 12h10M10 18h10"/><path d="m3.5 6 1.2 1.2L7 5M3.5 12l1.2 1.2L7 11"/><circle cx="5" cy="18" r="1.3"/></svg>')
I_BOX = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true">'
         '<path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z"/><path d="M3 7.5 12 12l9-4.5M12 12v9"/></svg>')
I_CAL = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
         '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/></svg>')
I_CHECK = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
           '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="m8 12 3 3 5-6"/></svg>')


def _pill(name: str, label: str, options: str, current: str | None) -> str:
    """Filter als Chip: unsichtbares Auswahlfeld darüber, aktiv mit Wert und ✕ zum Entfernen."""
    shown = ""
    if current:
        m = re.search(r'<option value="([^"]*)" selected>([^<]*)</option>', options)
        shown = m.group(2) if m else e(current)
    text = f'{e(label)}: <b>{shown}</b>' if current else f'{e(label)}<span class="car">▾</span>'
    return (f'<span class="pill{" on" if current else ""}" data-label="{e(label)}"><span class="pv">{text}</span>'
            f'<select name="{name}" aria-label="{e(label)}">{options}</select>'
            f'<button type="button" class="px" aria-label="{e(t("Filter entfernen"))}">✕</button></span>')


def page_jobs(store: Store, f: dict[str, str | None], connected: bool) -> str:
    names = printer_names(store)
    customers = store.customers()
    view = f.get("view") or "table"

    printer_opts = opt("", t("Alle Drucker"), f["printer"]) + "".join(
        opt(k, v, f["printer"]) for k, v in names.items()
    )
    month_opts = opt("", t("Alle Monate"), f["month"]) + "".join(
        opt(m, datetime.strptime(m, "%Y-%m").strftime(t("%m/%Y")), f["month"]) for m in store.months()
    )
    status_opts = opt("", t("Alle Status"), f["status"]) + "".join(
        opt(s, t(s), f["status"]) for s in ("fertig", "abgebrochen", "unvollständig")
    ) + opt("ausschuss", t("nur Fehldrucke"), f["status"]) + "".join(
        opt(f"fd:{k}", t("Fehldruck: {x}", x=t(name)), f["status"]) for k, (_, name) in DEFECTS.items()
    ) + opt("fd:none", t("Fehldruck: Ursache fehlt"), f["status"]) + opt("gut", t("ohne Fehldrucke"), f["status"])
    mat_opts = opt("", t("Alle Materialien"), f["material"]) + "".join(
        opt(m, m, f["material"]) for m in store.material_types()
    )
    cust_opts = opt("", t("Alle Kunden"), f["customer"]) + opt("none", t("ohne Kunde"), f["customer"]) + "".join(
        opt(str(c["id"]), c["name"], f["customer"]) for c in customers
    )
    todo_opts = opt("", t("Zahlung/Übergabe"), f.get("todo")) + "".join(
        opt(k, t(label), f.get("todo")) for k, (label, _) in TODO_OPTIONS.items()
    )
    dot = "var(--ok)" if connected else "var(--bad)"
    conn = t("HA verbunden") if connected else t("keine Verbindung zu HA")
    day_hidden = f'<input type="hidden" name="day" value="{e(f["day"])}">' if f.get("day") else ""
    pills = "".join(_pill(name, label, opts, f.get(name)) for name, label, opts in (
        ("printer", t("Drucker"), printer_opts), ("month", t("Monat"), month_opts), ("status", t("Status"), status_opts),
        ("material", t("Material"), mat_opts), ("customer", t("Kunde"), cust_opts), ("todo", t("Zahlung/Übergabe"), todo_opts)))
    if f.get("model"):
        mname = _models.names(store).get(int(f["model"]), "?")
        pills += _pill("model", t("Modell"), opt("", t("Alle Modelle"), f["model"]) + opt(f["model"], mname, f["model"]), f["model"])
    active = any(f.get(k) for k in ("printer", "month", "status", "material", "customer", "model", "todo", "day"))
    n_open = store.count_jobs(pending="1")

    def view_link(key: str, icon: str, label: str, extra: str = "") -> str:
        return (f'<a href="#" data-view="{key}" class="{"on" if view == key else ""}" title="{e(label)}">{icon}'
                f'<span class="t">{e(label)}</span>{extra}</a>')

    views = (view_link("table", I_TABLE, t("Tabelle")) + view_link("gallery", I_GRID, t("Galerie")) +
             view_link("todo", I_TODO, t("Offen"), f'<span class="cnt" id="opencount"{"" if n_open else " hidden"}>{n_open}</span>') +
             view_link("stock", I_BOX, t("Vorrat")))
    body = f"""<form class="f fb{" todo" if view in ("todo", "stock") else ""}" method="get" autocomplete="off">
<div class="fbrow"><label class="fsearch">{I_SEARCH}<input class="search" type="search" name="q" value="{e(f["q"] or "")}" placeholder="{e(t("Suchen: Name, Material, Kunde, Projekt-ID …"))}"></label>
<span class="seg vseg">{views}</span>
<button type="button" class="ib gbtn{" on" if f.get("group") else ""}" title="{e(t("Liste nach Tagen gruppieren"))}" aria-label="{e(t("Nach Tagen"))}">{I_CAL}</button>
<button type="button" class="ib selbtn" title="{e(t("Mehrere Drucke auswählen, z. B. zum Löschen"))}" aria-label="{e(t("Auswählen"))}">{I_CHECK}</button></div>
<div class="pills">{pills}<button type="button" class="fclear"{"" if active else " hidden"}>{t("Filter löschen")}</button>
<span class="conn"><span class="dot" style="background:{dot};border:0"></span>{conn}</span></div>{day_hidden}
<input type="hidden" name="view" value="{e(view)}"><input type="hidden" name="group" value="{e(f.get("group") or "")}">
</form>
<div id="results">{results(store, f)}</div>
<div id="selbar" hidden><b>{t('<span class="n">0</span> ausgewählt')}</b><button type="button" class="all">{t("Alle")}</button>
<button type="button" class="none">{t("Keine")}</button><span class="sp"></span><button type="button" class="cancel" title="{e(t("Abbrechen"))}"><span class="bl">{t("Abbrechen")}</span><span class="bs">✕</span></button>
<button type="button" class="btn kbtn" disabled title="{e(t("Ausgewählten Drucken einen Kunden zuweisen"))}">👤 <span class="bl">{t("Kunde zuweisen")}</span><span class="bs">{t("Kunde")}</span></button>
<button type="button" class="btn pjbtn" disabled title="{e(t("Ausgewählte Drucke zu einem Projekt zusammenfassen"))}">📦 <span class="bl">{t("Projekt bilden")}</span></button>
<button type="button" class="btn danger del" disabled title="{e(t("Löschen"))}">🗑 <span class="bl">{t("Löschen")}</span></button></div>
{_project_dialog(store, customers)}{customer_picker(customers)}
<div id="toast" hidden><span></span><button type="button">{t("Rückgängig")}</button></div>"""
    return layout("Printlog", "jobs", f"<style>{MOS_CSS}{_models.CSS}{JOBS_CSS}{ACT_CSS}{TODO_CSS}{KPICK_CSS}{_split.SPLIT_CSS}</style>" + body,
                  ACT_JS + KPICK_JS + JOBS_JS + _split.SPLIT_JS)


def _project_dialog(store: Store, customers: list[dict[str, Any]]) -> str:
    """Dialog „Projekt bilden“ für die Mehrfachauswahl (Inhalt füllt JOBS_JS)."""
    cust_opts = opt("", t("– kein Kunde –"), "") + "".join(opt(str(c["id"]), c["name"], "") for c in customers)
    projs = store.projects(40)
    proj_opts = "".join(opt(str(p["id"]), " · ".join(x for x in (code_label(p.get("code")), p.get("task_name") or "–",
                                                                   p.get("customer_name") or "", tn(p["n"], "{n} Druck", "{n} Drucke")) if x), "")
                        for p in projs)
    add_btn = f'<button type="button" data-m="add">{t("Zu bestehendem Projekt …")}</button>' if projs else ""
    return f"""<div id="pjdlg" class="dlgbg" hidden><form class="dlg" autocomplete="off">
<h3>📦 <span class="ttl"></span></h3><p class="sub">{t("Die Drucke bleiben einzeln erhalten und erscheinen im Projekt. Preis, Zahlung und Übergabe laufen danach über das Projekt.")}</p>
<div class="seg2"><button type="button" data-m="new" class="on">{t("Neues Projekt")}</button>{add_btn}</div>
<div class="mnew"><div class="fr"><label>{t("Name")}<input name="name" required maxlength="120"></label>
<label>{t("Kunde")}<select name="customer_id">{cust_opts}</select></label>
<label>{t("Stückzahl")}<span class="stp"><button type="button" data-d="-1" aria-label="{e(t("eins weniger"))}">−</button><input name="quantity" type="number" min="1" value="1" inputmode="numeric"><button type="button" data-d="1" aria-label="{e(t("eins mehr"))}">+</button></span></label></div>
<p class="hint">{t("Fertige Objekte für den Kunden (z. B. 1 Set). Die Stückzahl der einzelnen Platten bleibt unverändert.")}</p></div>
<div class="madd" hidden><label>{t("Projekt")}<select name="into">{proj_opts}</select></label></div>
<div class="pl"></div>
<div class="ft"><span class="sp">{t("Bekommt eine eigene Projekt-ID (PL-…), die Drucke behalten ihre.")}</span>
<button type="button" class="btn cancel">{t("Abbrechen")}</button><button class="btn primary go">{t("Projekt anlegen")}</button></div>
</form></div>"""


def results(store: Store, f: dict[str, str | None]) -> str:
    """Der Teil der Seite, den die Live-Suche austauscht."""
    q = db_filters(f)
    marker = f'<span id="opendata" hidden data-n="{store.count_jobs(pending="1")}"></span>'
    if f.get("view") == "todo":
        return marker + _todo_view(store, q)
    if f.get("view") == "stock":
        return marker + _split.stock_view(store, f)
    jobs, kids, via = group_projects(store, store.list_jobs(**q))
    add_suggestions(store, jobs)
    _split.attach(store, jobs, kids)
    summary = store.summary(**q)
    mat_sum = store.material_summary(**q)
    names = printer_names(store)
    tot = summary["total"]

    n = tot["n"] or 0
    kpis = [
        (t("Gesamtkosten"), fmt_num(tot["cost_total"], 2, "€"),
         tn(n, "{n} Druck", "{n} Drucke") + (f' · Ø {fmt_num(tot["cost_total"] / n, 2, "€")}' if n else ""), True, True),
        (t("⚡ Strom"), fmt_num(tot["cost"], 2, "€"), fmt_num(tot["kwh"], 2, "kWh"), False, True),
        (t("🧵 Filament"), fmt_num(tot["cost_filament"], 2, "€"), fmt_num((tot["weight"] or 0) / 1000, 2, "kg"), False, True),
        (t("🖨 Drucker"), fmt_num(tot["cost_printer"], 2, "€"), t("Abschr. + Wartung"), False, True),
        (t("Drucke"), str(n), " ", False, False),
        (t("Fehldrucke"), fmt_num(tot["scrap_cost"], 2, "€"), tn(tot["scrap_n"], "{n} Druck", "{n} Drucke")
         + (" · " + t("{v} berechnet", v=fmt_num(tot["scrap_charged"], 2, "€")) if tot.get("scrap_charged", 0) >= 0.005 else ""), False, True),
    ]
    kpi_html = "".join(
        f'<div class="kpi{" main" if main else ""}"><span class="muted">{e(k)}</span><b class="{"calc" if calc else ""}">{e(v)}</b><small class="calc">{e(sub)}</small></div>'
        for k, v, sub, main, calc in kpis
    )
    per_printer = " · ".join(
        f'{e(names.get(p["printer"], p["printer"]))}: <b>{fmt_num(p["cost_total"] or p["cost"], 2, "€")}</b>'
        for p in summary["per_printer"]
    )
    per_mat = " · ".join(
        f'{e(m["mtype"])}: <b>{fmt_num((m["grams"] or 0) / 1000, 2, "kg")}</b>' for m in mat_sum
    )
    day_note = ""
    if f.get("day"):
        d = fmt_date(datetime.strptime(f["day"], "%Y-%m-%d"), "date")
        day_note = f'<span>{t("Tag")}: <b>{d}</b> · <a href="./">{t("alle Tage")}</a></span>'
    mini = f'<div class="mini">{day_note}<span class="calc">{per_printer}</span><span class="muted">{per_mat}</span></div>'

    banner = ""
    if jobs and not store.printer_rates_set() and not store.material_prices():
        banner = (f'<div class="banner">{t("Noch keine Drucker- oder Filamentpreise hinterlegt.")} '
                  f'<a href="settings">{t("Zu den Einstellungen")}</a></div>')

    photos = store.first_photos([j["id"] for j in jobs])
    if f.get("view") == "gallery":
        cards = "".join(_gallery_card(j, names, photos.get(j["id"]), kids.get(j["id"])) for j in jobs)
        listing = f'<div class="gallery">{cards}</div>' if cards else f'<p class="muted">{t("Keine Drucke gefunden.")}</p>'
    else:
        mg = margins(store)
        grouped = bool(f.get("group"))
        rows, cards = [], []
        for label, day_jobs in _by_day(jobs) if grouped else [(None, jobs)]:
            if label:
                rows.append(f'<div class="jday"><b>{label[0]}</b><span>{label[1]}</span></div>')
                cards.append(f'<div class="mday"><b>{label[0]}</b><span>{label[1]}</span></div>')
            for j in day_jobs:
                if j.get("kind") == "project":
                    rows.append(_project_rows(j, kids.get(j["id"], []), names, mg, grouped, j["id"] in via))
                else:
                    rows.append(_job_row(j, names, mg, grouped))
            cards += [_mobile_card(j, names, photos.get(j["id"]), kids.get(j["id"])) for j in day_jobs]
        empty = f'<p class="muted" style="padding:14px 18px;margin:0">{t("Keine Drucke gefunden.")}</p>'
        listing = f"""<div class="jt"><div class="jh"><div></div><div>{t("Auftrag · Material · Kunde")}</div><div>{t("Drucker · Start")}</div>
<div class="r">{t("Verbrauch")}</div><div class="r">{t("Kosten")}</div><div>{t("Verkaufspreise")}</div><div class="r">{t("Status")}</div></div>
{"".join(rows) or empty}</div>
<div class="legend2 calc"><span><i class="c-str"></i>{t("Strom")}</span><span><i class="c-fil"></i>{t("Filament")}</span><span><i class="c-dru"></i>{t("Drucker")}</span>
<span>· {t("Balken antippen für die Aufschlüsselung")}</span></div>
<div class="mlist">{"".join(cards) or empty}</div>"""
    return f'{marker}{banner}<div class="kpis jk">{kpi_html}</div>{mini}{listing}'


# ---- Offene Posten (Ansicht „Offen“ in der Druckliste) -------------------------

TODO_CSS = """
.okpis{grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:14px}
.okpis .kpi{border-radius:14px;padding:12px 16px}.okpis .kpi span{display:block;font-size:13px;font-weight:600;color:var(--muted)}
.okpis .kpi b{font-size:24px}.okpis .kpi small{display:block;color:var(--muted);font-size:13px}
.okpis .kpi.warn b{color:var(--warn)}.okpis .kpi.run b{color:var(--run)}
.og{background:var(--card);border:1px solid var(--line);border-radius:16px;margin-bottom:14px;overflow:hidden}
.og>header{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap;
 background:color-mix(in srgb,var(--bg) 45%,var(--card))}
.og .nm{font-size:18px;font-weight:800;display:flex;align-items:center;gap:10px;color:var(--fg);text-decoration:none}
.og .av{width:32px;height:32px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:14px;color:#fff;flex:none}
.og .sum{margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.otg{display:inline-flex;align-items:center;gap:6px;padding:3px 11px;border-radius:999px;font-weight:700;font-size:13px;white-space:nowrap;
 border:1px solid color-mix(in srgb,currentColor 35%,transparent);background:color-mix(in srgb,currentColor 10%,transparent)}
.otg.warn{color:var(--warn)}.otg.run{color:var(--run)}
.or{display:grid;grid-template-columns:48px minmax(0,1fr) 220px 170px 236px;gap:14px;align-items:center;padding:11px 16px;border-bottom:1px solid var(--line)}
.or:last-child{border-bottom:0}
.or .t,.or .ph{width:48px;height:48px;border-radius:10px;border:1px solid var(--line)}.or .t.mos{box-sizing:border-box}
.or .oname b{display:block;font-size:16px;line-height:1.25;overflow-wrap:anywhere}.or .oname a{color:var(--fg);text-decoration:none}
.or .oname>span{font-size:13px;color:var(--muted)}.or .pid{margin-left:6px}
.prog{display:grid;gap:4px;font-size:13px}.prog .l{display:flex;justify-content:space-between;gap:8px;color:var(--muted);white-space:nowrap}
.prog .l b{color:var(--fg);font-variant-numeric:tabular-nums}
.prog .bar2{height:6px;border-radius:4px;background:color-mix(in srgb,var(--fg) 10%,transparent);overflow:hidden}
.prog .bar2 i{display:block;height:100%;border-radius:4px}
.prog.pay .bar2 i{background:var(--warn)}.prog.ho .bar2 i{background:var(--run)}
.prog.done{opacity:.6}.prog.done .l b{color:var(--ok)}.prog.done .bar2 i{background:var(--ok)}
.prog.na{color:var(--muted)}
.oact{display:flex;gap:6px;justify-content:flex-end}
.oact .btn{min-height:34px;padding:0 11px;font-size:13px}
.oact .btn.pay{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,transparent)}
.oact .btn.ho{color:var(--run);border-color:color-mix(in srgb,var(--run) 45%,transparent)}
.ohint{font-size:13px;color:var(--muted);max-width:var(--w-text);margin:10px 2px 0}
.oempty{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px;text-align:center;color:var(--muted)}
@media (max-width:1150px){.or{grid-template-columns:44px minmax(0,1fr) 190px 140px 226px}.or .t,.or .ph{width:44px;height:44px}}
@media (max-width:900px){.or{grid-template-columns:44px minmax(0,1fr) 1fr 1fr}.or .oact{grid-column:1/-1}}
@media (max-width:700px){
 .okpis{grid-template-columns:1fr 1fr;gap:8px}.okpis .kpi{padding:8px 10px}.okpis .kpi b{font-size:18px}.okpis .kpi small{font-size:12px}
 .og>header{padding:10px 12px}.og .sum{margin-left:0;width:100%}
 .or{grid-template-columns:40px minmax(0,1fr);gap:6px 10px;padding:10px 12px}.or .t,.or .ph{width:40px;height:40px}
 .or .prog{grid-column:2}.or .oact{grid-column:1/-1}.or .oact .btn{flex:1}
}
"""


def _open_state(j: dict[str, Any]) -> tuple[bool, bool]:
    """(Geld offen, Stück offen) für die Ansicht „Offen“."""
    if not billable(j) or no_charge(j):
        return False, False
    due = amount_due(j)
    pay = not j.get("gift") and due is not None and (j.get("paid") or 0) < due - 0.005
    ho = j.get("state") == "done" and (j.get("delivered") or 0) < (j.get("quantity") or 1)
    return pay, ho


def _ago(ts: float | None) -> str:
    if not ts:
        return "–"
    days = (datetime.now().date() - datetime.fromtimestamp(ts).date()).days
    return t("heute") if days <= 0 else t("gestern") if days == 1 else t("vor {n} Tagen", n=days)


def _todo_row(j: dict[str, Any], photo: str | None, kids: list[dict[str, Any]] | None = None, note: str = "") -> str:
    pay, ho = _open_state(j)
    jid, href = j["id"], f'job?id={j["id"]}'
    n, d = j.get("quantity") or 1, min(j.get("delivered") or 0, j.get("quantity") or 1)
    if j.get("gift"):
        pay_html = f'<div class="prog na">{t("🎁 verschenkt")}</div>'
    else:
        due, paid = amount_due(j) or 0, j.get("paid") or 0
        pct = min(100, 100 * paid / due) if due else 0
        what = t("<b>{v}</b> offen", v=fmt_num(due - paid, 2, "€")) if pay else f'<b>{t("✓ bezahlt")}</b>'
        pay_html = (f'<div class="prog pay{"" if pay else " done"} calc"><div class="l"><span>💶 {what}</span>'
                    f'<span>{t("von {v}", v=fmt_num(due, 2, "€"))}</span></div><div class="bar2"><i style="width:{pct:.0f}%"></i></div></div>')
    what = t("<b>{d} von {n}</b> Stück", d=d, n=n) if ho else f'<b>{t("✓ übergeben")}</b>'
    ho_html = (f'<div class="prog ho{"" if ho else " done"}"><div class="l"><span>📦 {what}</span></div>'
               f'<div class="bar2"><i style="width:{100 * d / n:.0f}%"></i></div></div>')
    acts = ""
    if pay:
        _, _, title, items = _pay_menu(j) or ("", "", "", [])
        acts += f'<button type="button" class="btn pay act"{_acts(title, items)}>{t("💶 Zahlung")}</button>'
    if ho:
        _, _, title, items = _ho_menu(j)
        acts += f'<button type="button" class="btn ho act"{_acts(title, items)}>{t("📦 Übergabe")}</button>'
    when = fmt_date(j["start_ts"], "dm") if j.get("start_ts") else "–"
    pic = mosaic(kids or []) if j.get("kind") == "project" else _thumb(j, photo)
    pj = f'📦 {tn(len(kids or []), "{n} Druck", "{n} Drucke")} · ' if j.get("kind") == "project" else ""
    return (f'<div class="or">{pic}<div class="oname"><b><a href="{href}">{e(j["task_name"] or "–")}</a>{_pid(j)}{note}</b>'
            f'<span>{pj}{when} · {_ago(j.get("start_ts"))} · {_pcs(n)}</span></div>'
            f'{pay_html}{ho_html}<div class="oact">{acts}</div></div>')


def _todo_view(store: Store, q: dict[str, Any]) -> str:
    """Offene Posten: Drucke mit offenem Betrag oder offenen Stück, nach Kunden gruppiert."""
    jobs = store.list_jobs(pending="1", **q)
    photos = store.first_photos([j["id"] for j in jobs])
    kids = store.children([j["id"] for j in jobs if j.get("kind") == "project"])
    groups: dict[int, list[dict[str, Any]]] = {}
    for j in sorted(jobs, key=lambda j: j.get("start_ts") or 0):
        if any(_open_state(j)):
            groups.setdefault(j["customer_id"], []).append(j)
    if not groups:
        return f'<div class="oempty">{t("Nichts offen: alles bezahlt und übergeben. 🎉")}</div>'

    def sums(items: list[dict[str, Any]]) -> tuple[float, int, int, int]:
        money = pieces = n_pay = n_ho = 0
        for j in items:
            pay, ho = _open_state(j)
            if pay:
                money += (amount_due(j) or 0) - (j.get("paid") or 0)
                n_pay += 1
            if ho:
                pieces += (j.get("quantity") or 1) - min(j.get("delivered") or 0, j.get("quantity") or 1)
                n_ho += 1
        return money, pieces, n_pay, n_ho

    order = sorted(groups.items(), key=lambda kv: (-sums(kv[1])[0], -sums(kv[1])[1]))
    allj = [j for _, items in order for j in items]
    money, pieces, n_pay, n_ho = sums(allj)
    oldest = (datetime.now().date() - datetime.fromtimestamp(allj[0]["start_ts"] or time.time()).date()).days
    kpis = (f'<div class="kpis okpis"><div class="kpi warn"><span>{t("💶 Noch zu bekommen")}</span><b class="calc">{fmt_num(money, 2, "€")}</b>'
            f'<small>{tn(n_pay, "{n} Druck", "{n} Drucke")}</small></div>'
            f'<div class="kpi run"><span>{t("📦 Noch zu übergeben")}</span><b>{_pcs(pieces)}</b><small>{tn(n_ho, "{n} Druck", "{n} Drucke")}</small></div>'
            f'<div class="kpi"><span>{t("Kunden")}</span><b>{len(groups)}</b><small>{t("mit Offenem")}</small></div>'
            f'<div class="kpi"><span>{t("Ältester Posten")}</span><b>{tn(oldest, "{n} Tag", "{n} Tage")}</b><small>{t("seit Druckstart")}</small></div></div>')
    out = []
    pcache: dict[int, dict[str, Any] | None] = {}
    for i, (cid, items) in enumerate(order):
        m, p, _, _ = sums(items)
        name = items[0].get("customer_name") or "?"
        tags = (f'<span class="otg warn calc">💶 {t("{v} offen", v=fmt_num(m, 2, "€"))}</span>' if m > 0.005 else "") + \
               (f'<span class="otg run">📦 {t("{q} Stück zu übergeben", q=p)}</span>' if p else "")
        out.append(f'<section class="og"><header><a class="nm" href="./?customer={cid}" title="{e(t("Alle Drucke von {name}", name=name))}">'
                   f'<span class="av" style="background:var(--s{i % 8 + 1})">{e(name[:1].upper())}</span>{e(name)}</a>'
                   f'<span class="muted" style="font-size:13px">{tn(len(items), "{n} Druck", "{n} Drucke")}</span><span class="sum">{tags}</span></header>'
                   f'{"".join(_todo_row(j, photos.get(j["id"]), kids.get(j["id"]), _split.todo_note(store, j, pcache) if j.get("kind") == "share" else "") for j in items)}</section>')
    hint = t("Offen sind Drucke mit Kunde, die noch nicht ganz bezahlt oder übergeben sind. Kunden mit „Keine Abrechnung“ und Fehldrucke, "
             "die nicht berechnet werden, fehlen. Verschenkte Drucke stehen nur hier, bis sie übergeben sind.")
    return f'{kpis}{"".join(out)}<p class="ohint">{hint}</p>'


WEEKDAYS = (N_("Mo."), N_("Di."), N_("Mi."), N_("Do."), N_("Fr."), N_("Sa."), N_("So."))
WEEKDAYS_LONG = (N_("Montag"), N_("Dienstag"), N_("Mittwoch"), N_("Donnerstag"), N_("Freitag"), N_("Samstag"), N_("Sonntag"))


def _by_day(jobs: list[dict[str, Any]]) -> list[tuple[tuple[str, str] | None, list[dict[str, Any]]]]:
    """Drucke nach Starttag gruppieren (Reihenfolge bleibt, neueste zuerst)."""
    today = datetime.now().date()
    groups: list[tuple[tuple[str, str] | None, list[dict[str, Any]]]] = []
    last = object()
    for j in jobs:
        d = datetime.fromtimestamp(j["start_ts"]).date() if j.get("start_ts") else None
        if d != last:
            if d is None:
                label = (t("Ohne Datum"), "")
            else:
                diff = (today - d).days
                dm = fmt_date(datetime(d.year, d.month, d.day), "dm")
                short = f"{t(WEEKDAYS[d.weekday()])}, {dm}{'' if d.year == today.year else (' ' if lang_en() else '') + str(d.year)}"
                name = {0: t("Heute"), 1: t("Gestern")}.get(diff) or (t(WEEKDAYS_LONG[d.weekday()]) if 0 < diff < 7 else short)
                label = (name, short if name != short else "")
            groups.append((label, []))
            last = d
        groups[-1][1].append(j)
    return groups


def _bar_parts(j: dict[str, Any]) -> list[tuple[str, str, float, str]]:
    grams = j["filament_g"] if j.get("filament_g") is not None else j["weight_g"]
    parts = [
        (t("Strom"), "c-str", j.get("cost"), fmt_num(j.get("kwh"), 3, "kWh") if j.get("kwh") is not None else t("kein Zählerstand")),
        (t("Filament"), "c-fil", j.get("cost_filament"), fmt_num(grams, 1, "g")),
        (t("Drucker"), "c-dru", j.get("cost_printer"), t("Abschreibung + Wartung")),
    ]
    if j.get("cost_extras"):
        parts.append((t("Zubehör"), "c-zub", j["cost_extras"], t("Ringe, Magnete, Posten …")))
    work = _extras.work_total(j)
    if work:
        parts.append((t("Arbeit"), "c-arb", work, t("Arbeit und Design zum Stundensatz")))
    return parts


def _cost_box(j: dict[str, Any], note: str = "") -> str:
    total = j.get("cost_total")
    if total is not None:
        total += _extras.work_total(j)
    if total is None:
        return '<div class="jc r"><b>–</b></div>'
    parts = _bar_parts(j)
    bar = "".join(f'<i class="{c}" style="flex:{v:.4f}"></i>' for _, c, v, _ in parts if v)
    q = j.get("quantity")
    per = f'<span class="pp">{e(note) if note else _per_piece(j)}</span>' if note or (q and q > 1) else ""
    rows = "".join(
        f'<div><i class="{c}"></i><span>{n}</span><b>{fmt_num(v, 2, "€")}</b><small>{e(sub)}</small></div>'
        for n, c, v, sub in parts
    )
    rows += f'<div class="sum"><i></i><span>{t("Gesamt")}</span><b>{fmt_num(total, 2, "€")}</b>'
    rows += (f'<small>{t("{pp} bei {q} Stück", pp=_per_piece(j), q=q)}</small>' if q and q > 1 else "") + "</div>"
    return (f'<button type="button" class="cb" aria-label="{e(t("Kosten aufschlüsseln"))}"><b>{fmt_num(total, 2, "€")}</b>{per}'
            f'<span class="bar">{bar}</span><span class="pop">{rows}</span></button>')


def _prices(j: dict[str, Any], mg: list[tuple[str, float]]) -> str:
    if j.get("split") and j.get("kind") != "share":
        return _split.split_prices(j)
    q = j.get("quantity") or 1
    items: list[tuple[str, float, bool, bool]] = []
    if j.get("customer_margin") is not None:
        items.append((f'👤 {j.get("customer_name") or t("Kunde")}', j["customer_margin"], True, bool(j.get("customer_work"))))
    items += [(m[0], m[1], False, getattr(m, "work", False)) for m in mg]
    out = ""
    if j.get("final_price") is not None:
        fp = j["final_price"]
        per = t("{v}/Stk.", v=fmt_num(fp / q, 2)) if q > 1 else ""
        out += f'<span class="fp">{t("Endpreis")}</span><b class="fp">{fmt_num(fp, 2, "€")}</b><small>{per}</small>'
    over = overpaid(j)
    if over:
        out += (f'<span class="fp">{t("Bezahlt")}</span><b class="fp">{fmt_num(j.get("paid"), 2, "€")}</b>'
                f'<small class="calc" title="{e(t("+{v} mehr", v=fmt_num(over, 2, "€")))}">+{fmt_num(over, 2)}</small>')
    for name, pct, cu, wo in items:
        price = _price(j, pct, wo)
        cls = ' class="cu"' if cu else ""
        per = t("{v}/Stk.", v=fmt_num(price / q, 2)) if price is not None and q > 1 else ""
        # Ohne Endpreis ist der Kundenpreis das, was der Kunde zahlt: bleibt im Diskret-Modus sichtbar.
        shown = cu and j.get("final_price") is None
        c2 = (' class="cu"' if shown else ' class="cu calc"') if cu else ' class="calc"'
        out += (f'<span{c2}>{e(name)} <span class="pct calc">+{fmt_input(pct)} %</span></span>'
                f'<b{c2}>{fmt_num(price, 2, "€")}</b><small class="{"" if shown else "calc"}">{per}</small>')
    return f'<div class="jp">{out}</div>' if out else '<div class="jp muted">–</div>'


def _status_badges(j: dict[str, Any]) -> str:
    status = _status_of(j)
    tone = _STATUS_TONE.get(status.split()[0], "none")
    out = f'<span class="bdg st-{tone}">{e(t(status))}</span>' + defect_badges(j)
    return out + customer_badges(j)


def defect_badges(j: dict[str, Any], cls: str = "bdg") -> str:
    """Fehldruck mit Ursache und ggf. Berechnung; bei Abbruch ohne Ursache ein leiser Hinweis."""
    if not j.get("is_scrap"):
        return ""
    lbl = defect_label(j)
    if lbl:
        out = f'<span class="{cls} st-bad" title="{e(t("Fehldruck"))}">{e(lbl)}</span>'
    elif j["status"] != "abgebrochen":
        out = f'<span class="{cls} st-bad">{t("⚠ Fehldruck")}</span>'
    else:
        out = f'<span class="{cls} st-none" title="{e(t("Fehldruck ohne Ursache: in den Details auswählen"))}">{t("Ursache?")}</span>'
    if charged(j) and billable(j):
        out += f'<span class="{cls} st-warn">{e(charge_text(j))}</span>'
    return out


def billable(j: dict[str, Any]) -> bool:
    """Druck mit Kunde, bei dem abgerechnet wird (nicht „Keine Abrechnung“, nicht über ein Projekt)."""
    return has_billing(j) and not j.get("project_id") and not j.get("split")


def has_billing(j: dict[str, Any]) -> bool:
    """Kunde vorhanden, bei dem abgerechnet wird (Drucke im Projekt eingeschlossen)."""
    return bool(j.get("customer_id")) and not j.get("customer_nobill")


def base_due(j: dict[str, Any]) -> float | None:
    """Normaler Preis: Endpreis, sonst Kosten + Kundenmarge."""
    if j.get("final_price") is not None:
        return j["final_price"]
    return _price(j, j.get("customer_margin") or 0, bool(j.get("customer_work")))


def amount_due(j: dict[str, Any]) -> float | None:
    """Was der Kunde zahlen soll: normaler Preis, bei einem berechneten Fehldruck anteilig bzw. fester Betrag."""
    base = base_due(j)
    return apply_charge(j, base) if charged(j) else base


def _acts(title: str, items: list[tuple[str, str, dict[str, str] | None]]) -> str:
    """Menü für ein Badge: (Text, URL, POST-Daten oder None für einen Link)."""
    return f' data-title="{e(title)}" data-acts="{e(json.dumps(items))}"'


def _pay_menu(j: dict[str, Any]) -> tuple[str, str, str, list[Any]] | None:
    """Zahlung eines Drucks: (Text, Ton, Titel, Menüpunkte) oder None ohne Preis."""
    jid = j["id"]
    due = amount_due(j)
    if due is None:
        return None
    paid = j.get("paid") or 0
    rest = max(0.0, due - paid)
    url = f"api/jobs/{jid}/pay"
    detail = (t("Betrag eingeben …"), f"job?id={jid}#ab", None)
    if rest < 0.005:
        label, tone = t("bezahlt"), "ok"
        items = [(t("Zurücksetzen (nicht bezahlt)"), url, {"reset": "1"}), (t("Zahlungen ansehen …"), f"job?id={jid}#ab", None)]
    else:
        label, tone = ((f"{fmt_num(paid, 2)}/{fmt_num(due, 2, '€')}", "run") if paid > 0 else (t("nicht bezahlt"), "warn"))
        items = [(t("Alles bezahlt ({v})", v=fmt_num(rest, 2, '€')), url, {"rest": "1"}), detail]
        if paid > 0:
            items.append((t("Zurücksetzen"), url, {"reset": "1"}))
        else:
            items.append((t("🎁 Verschenkt"), f"api/jobs/{jid}", {"gift": "1"}))
    title = t("{paid} von {due} bezahlt", paid=fmt_num(paid, 2, '€'), due=fmt_num(due, 2, '€'))
    return label, tone, title, items


def _ho_menu(j: dict[str, Any]) -> tuple[str, str, str, list[Any]]:
    """Übergabe eines Drucks: (Text, Ton, Titel, Menüpunkte)."""
    jid = j["id"]
    n, d = j.get("quantity") or 1, min(j.get("delivered") or 0, j.get("quantity") or 1)
    url = f"api/jobs/{jid}/handover"
    label, tone = ((t("übergeben"), "ok") if d >= n else
                   (t("{d}/{n} übergeben", d=d, n=n), "run") if d else (t("nicht übergeben"), "warn"))
    items: list[Any] = []
    if d < n:
        items.append((t("Alles übergeben ({k} Stück)", k=n - d) if n > 1 else t("Übergeben"), url, {"qty": "rest"}))
        if n - d > 1:
            items.append((t("1 Stück übergeben"), url, {"qty": "1"}))
            items.append((t("Andere Anzahl …"), f"job?id={jid}#ab", None))
    if d:
        items.append((t("Zurücksetzen (nicht übergeben)"), url, {"reset": "1"}))
    return label, tone, t("{d} von {n} Stück übergeben", d=d, n=n), items


def customer_badges(j: dict[str, Any], cls: str = "bdg") -> str:
    """Zahlung und Übergabe als antippbare Badges (nur Drucke mit Kunde, kein unberechneter Fehldruck)."""
    if j.get("split") and j.get("kind") != "share":
        return _split.split_badges(j, cls)
    if j.get("gift") and not billable(j) and not j.get("is_scrap"):
        return f'<span class="{cls} st-gift">{t("🎁 verschenkt")}</span>'
    if not billable(j) or no_charge(j):
        return ""
    jid = j["id"]
    out = ""
    pay = None if j.get("gift") else _pay_menu(j)
    if j.get("gift"):
        items = [(t("Doch berechnen"), f"api/jobs/{jid}", {"gift": "0"}), (t("Abrechnung ansehen …"), f"job?id={jid}#ab", None)]
        out += f'<button type="button" class="{cls} st-gift act"{_acts(t("Verschenkt, keine Zahlung nötig"), items)}>{t("🎁 verschenkt")}</button>'
    elif pay:
        label, tone, title, items = pay
        out += f'<button type="button" class="{cls} st-{tone} act"{_acts(title, items)}>💶 {label}</button>'
    if j.get("state") == "done":
        label, tone, title, items = _ho_menu(j)
        out += f'<button type="button" class="{cls} st-{tone} act"{_acts(title, items)}>📦 {label}</button>'
    return out


# Menü an Badges (Zahlung/Übergabe); nach Erfolg wird done() aufgerufen
ACT_JS = """
let actMenu=null;
function closeAct(){if(actMenu){actMenu.remove();actMenu=null}}
function bindAct(done){
 document.querySelectorAll('button.act[data-acts]').forEach(b=>b.onclick=ev=>{ev.stopPropagation();ev.preventDefault();
   const open=actMenu&&actMenu.dataset.for===b.dataset.acts;closeAct();if(open)return;
   const m=document.createElement('div');m.className='actmenu';m.dataset.for=b.dataset.acts;
   const t=document.createElement('b');t.textContent=b.dataset.title;m.appendChild(t);
   JSON.parse(b.dataset.acts).forEach(([label,url,data])=>{const x=document.createElement(data?'button':'a');x.textContent=label;
     if(data){x.type='button';x.onclick=async e2=>{e2.stopPropagation();const fd=new FormData();for(const k in data)fd.append(k,data[k]);
       x.disabled=true;const r=await fetch(url,{method:'POST',body:fd});closeAct();if(r.ok)done()}}
     else x.href=url;m.appendChild(x)});
   document.body.appendChild(m);const rc=b.getBoundingClientRect();
   let top=scrollY+rc.bottom+6;if(rc.bottom+m.offsetHeight+10>innerHeight&&rc.top>m.offsetHeight+10)top=scrollY+rc.top-m.offsetHeight-6;
   m.style.top=top+'px';m.style.left=Math.max(8,Math.min(scrollX+rc.right-m.offsetWidth,scrollX+innerWidth-m.offsetWidth-8))+'px';
   actMenu=m});
}
document.addEventListener('click',ev=>{if(actMenu&&!actMenu.contains(ev.target))closeAct()});
"""

ACT_CSS = """
button.bdg.act:before{display:none}
button.bdg.act{cursor:pointer;font:inherit;font-size:13px;font-weight:700;-webkit-tap-highlight-color:transparent}
button.bdg.act:hover{filter:brightness(1.15)}
.actmenu{position:absolute;z-index:30;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px;
 box-shadow:0 8px 28px rgba(0,0,0,.35);display:grid;gap:6px;min-width:230px;font-size:14px}
.actmenu b{padding:4px 6px 2px;font-size:13px;color:var(--muted)}
.actmenu a{display:block;padding:10px 12px;border-radius:9px;border:1px solid var(--line);font-weight:600;text-decoration:none}
.actmenu button{text-align:left;padding:10px 12px;border-radius:9px;border:1px solid var(--line);background:var(--bg);font-weight:600;cursor:pointer}
"""


def _pid(j: dict[str, Any], cls: str = "pid") -> str:
    """Kurze Projekt-ID als kleines Schild, z. B. PL-7K3Q."""
    return f'<span class="{cls}">{e(code_label(j.get("code")))}</span>' if j.get("code") else ""


def group_projects(store: Store, jobs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]], set[int]]:
    """Liste für die Anzeige: Drucke eines Projekts erscheinen unter ihrem Projekt.

    Passt nur ein Druck des Projekts zum Filter, kommt das Projekt trotzdem in die Liste (``via``: aufgeklappt).
    Gibt (oberste Ebene, Drucke je Projekt, Projekte nur über einen Druck gefunden) zurück.
    """
    tops = [j for j in jobs if not j.get("project_id")]
    have = {j["id"] for j in tops}
    via: set[int] = set()
    for j in jobs:
        pid = j.get("project_id")
        if pid and pid not in have:
            p = store.get(pid)
            if p:
                tops.append(p)
                have.add(pid)
                via.add(pid)
    if via:
        tops.sort(key=lambda j: (j.get("start_ts") or 0, j["id"]), reverse=True)
    kids = store.children([j["id"] for j in tops if j.get("kind") == "project"])
    return tops, kids, via


def mosaic(kids: list[dict[str, Any]], cls: str = "t", count: bool = True) -> str:
    """Vorschaubild eines Projekts: bis zu vier Bilder seiner Drucke, unten rechts die Anzahl."""
    imgs: list[str] = []
    for k in sorted(kids, key=lambda k: bool(k.get("is_scrap"))):
        if k.get("image") and k["image"] not in imgs:
            imgs.append(k["image"])
    imgs = imgs[:4]
    tiles = "".join(f'<img loading="lazy" src="images/{e(i)}" alt="">' for i in imgs) or '<span></span>'
    one = " one" if len(imgs) == 1 else ""
    n = f'<b>{len(kids)}</b>' if count and kids else ""
    return f'<div class="{cls} mos{one}">{tiles}{n}</div>'


def printers_of(kids: list[dict[str, Any]], names: dict[str, str]) -> str:
    return " · ".join(dict.fromkeys(names.get(k["printer"], k.get("printer_name") or k["printer"]) for k in kids)) or "–"


def printers_html(kids: list[dict[str, Any]], names: dict[str, str]) -> str:
    """Drucker eines Projekts für die Tabelle: in einer Zeile, solange Platz ist, sonst je Drucker umbrechen."""
    ps = list(dict.fromkeys(names.get(k["printer"], k.get("printer_name") or k["printer"]) for k in kids))
    if len(ps) < 2:
        return f'<b>{e(ps[0] if ps else "–")}</b>'
    items = "".join(f'<i title="{e(x)}">{e(x)}</i>' for x in ps)
    return f'<b class="prs"><span>{items}</span></b>'


def print_hours(kids: list[dict[str, Any]]) -> float:
    return sum(max(0.0, (k["end_ts"] or time.time()) - k["start_ts"]) for k in kids if k.get("start_ts")) / 3600


def fmt_hours(h: float) -> str:
    m = int(round(h * 60))
    return f"{m // 60}:{m % 60:02d} h" if h else "–"


def span_text(kids: list[dict[str, Any]]) -> str:
    """Zeitraum eines Projekts, z. B. „22.–23.11.“ oder „22.11.“."""
    days = sorted({datetime.fromtimestamp(k["start_ts"]).date() for k in kids if k.get("start_ts")})
    if not days:
        return "–"
    a, b = days[0], days[-1]
    if a == b:
        return fmt_date(datetime(a.year, a.month, a.day), "dm")
    if a.month == b.month and a.year == b.year and not lang_en():
        return f"{a.day}.–{fmt_date(datetime(b.year, b.month, b.day), 'dm')}"
    return f"{fmt_date(datetime(a.year, a.month, a.day), 'dm')}–{fmt_date(datetime(b.year, b.month, b.day), 'dm')}"


def project_chips(kids: list[dict[str, Any]]) -> str:
    """Material eines Projekts zusammengefasst (gleiches Filament addiert)."""
    agg: dict[tuple[str, str], float] = {}
    for k in kids:
        factor = 1.0
        if k.get("status") == "abgebrochen" and k.get("progress") is not None:
            factor = max(0.0, min(1.0, k["progress"] / 100))
        for m in parse_materials(k.get("materials")):
            key = (m["name"] or "?", (m["color"] or "")[:7].upper())
            agg[key] = agg.get(key, 0.0) + (m["grams"] or 0) * factor
    items = sorted(agg.items(), key=lambda kv: -kv[1])
    chips = "".join(f'<span><span class="dot" style="background:{_color(c)}"></span>{e(_short(n))} {fmt_num(g, 1, "g")}</span>'
                    for (n, c), g in items[:4])
    more = f'<span>+{len(items) - 4}</span>' if len(items) > 4 else ""
    return f'<div class="chips">{chips}{more}</div>' if chips else ""


def share_of(k: dict[str, Any], project: dict[str, Any]) -> float | None:
    """Anteil eines Drucks an der Preisbasis des Projekts (0 bei unberechnetem Fehldruck)."""
    base = price_base(project)
    if not base or k.get("cost_total") is None:
        return None
    if no_charge(k):
        return 0.0
    c = k["cost_total"]
    if k.get("is_scrap") and k.get("defect_charge") == "pct":
        c = c * (k.get("defect_value") or 0) / 100
    elif k.get("is_scrap") and k.get("defect_charge") == "amount":
        c = (k.get("defect_value") or 0) / (1 + (project.get("customer_margin") or 0) / 100)
    return c / base


def _project_rows(p: dict[str, Any], kids: list[dict[str, Any]], names: dict[str, str], mg: list[tuple[str, float]],
                  grouped: bool, open_: bool) -> str:
    """Projektzeile in der Tabelle, darunter (aufklappbar) seine Drucke."""
    href = f'job?id={p["id"]}'
    tags = [cust_chip(p)]
    if p.get("quantity"):
        tags.append(f'<span class="tag">{_pcs(p["quantity"])}</span>')
    if p.get("job_note"):
        tags.append(f'<span class="tnote">📝 {e(p["job_note"])}</span>')
    when = span_text(kids)
    if grouped and p.get("start_ts") and len({datetime.fromtimestamp(k["start_ts"]).date() for k in kids if k.get("start_ts")}) <= 1:
        when = datetime.fromtimestamp(p["start_ts"]).strftime("%H:%M")
    hours = print_hours(kids)
    n_fd = sum(1 for k in kids if k.get("is_scrap"))
    pp = (t("davon {n} Fehldruck", n=n_fd) if n_fd == 1 else t("davon {n} Fehldrucke", n=n_fd)) if n_fd else ""
    cost = _cost_box(p, pp)
    toggle = (f'<button type="button" class="xp" data-pid="{p["id"]}" aria-expanded="{"true" if open_ else "false"}" '
              f'title="{e(t("Drucke ein- oder ausblenden"))}">{tn(len(kids), "{n} Druck", "{n} Drucke")}</button>')
    cls = "jr proj" + (" open" if open_ else "") + (" run" if p.get("status") == "läuft" else "")
    grams = p.get("filament_g")
    row = (f'<div class="{cls}" data-href="{href}" data-kind="project" data-name="{e(p["task_name"] or "")}" data-cust="{p.get("customer_id") or ""}">'
           f'{mosaic(kids)}'
           f'<div><a class="jn" href="{href}">{e(p["task_name"] or "–")}</a>{_pid(p)}{toggle}{project_chips(kids)}'
           f'<div class="tags">{"".join(tags)}</div></div>'
           f'<div class="jc">{printers_html(kids, names)}<span>{when}{" · " + fmt_hours(hours) if hours else ""}</span></div>'
           f'<div class="jc r"><b>{fmt_num(grams, 1, "g")}</b><span>{fmt_num(p.get("kwh"), 3, "kWh")}</span></div>'
           f'<div class="calc">{cost}</div>{_prices(p, mg)}<div class="js">{_status_badges(p)}</div></div>')
    out = [row]
    for i, k in enumerate(kids):
        out.append(_job_row(k, names, mg, grouped, parent=p, last=i == len(kids) - 1, hidden=not open_))
    return "".join(out)


def _job_row(j: dict[str, Any], names: dict[str, str], mg: list[tuple[str, float]], grouped: bool = False,
             parent: dict[str, Any] | None = None, last: bool = False, hidden: bool = False) -> str:
    href = f'job?id={j["id"]}'
    grams = j["filament_g"] if j.get("filament_g") is not None else j["weight_g"]
    tags = [] if parent else [cust_chip(j)]
    q = j.get("quantity")
    if q and q > 1:
        tags.append(f'<span class="tag">{_pcs(q)}</span>')
    if j.get("reprint_of"):
        tags.append(f'<span class="tnote">↻ {t("Nachdruck")}</span>')
    url, title = j.get("project_url"), j.get("project_title")
    if url and url.startswith(("http://", "https://")):
        tags.append(f'<a class="tag" href="{e(url)}" target="_blank" rel="noopener">🔗 {e(title or t("Projekt"))}</a>')
    elif title:
        tags.append(f'<span class="tag">🔗 {e(title)}</span>')
    if j.get("local_path"):
        tags.append(f'<span class="tag" title="{e(t("Lokaler Ordner hinterlegt"))}">📂</span>')
    if j["status"] == "abgebrochen" and j["progress"] is not None:
        tags.append(f'<span class="tnote bad">{t("bei {p} % abgebrochen", p=int(j["progress"]))}</span>')
    if j.get("note"):
        tags.append(f'<span class="tnote warn">{e(t_note(j["note"]))}</span>')
    if j.get("job_note"):
        tags.append(f'<span class="tnote">📝 {e(j["job_note"])}</span>')
    tag_html = f'<div class="tags">{"".join(tags)}</div>' if tags else ""
    if j.get("start_ts"):
        st = datetime.fromtimestamp(j["start_ts"])
        when = st.strftime("%H:%M") if grouped else fmt_date(st, "dm_hm")
    else:
        when = "–"
    dur = fmt_dur(j["start_ts"], j["end_ts"])
    cls = "jr"
    if j["state"] in ("running", "closing"):
        cls += " run"
    if no_charge(j):
        cls += " scrap"
    prices = _prices(j, mg)
    attrs = f' data-name="{e(j["task_name"] or "")}" data-cust="{j.get("customer_id") or ""}"'
    if parent:
        cls += " kid" + (" last" if last else "")
        attrs += f' data-parent="{parent["id"]}"' + (" hidden" if hidden else "")
        sh = share_of(j, parent)
        prices = (f'<div class="jp inproj calc"><span>{t("im Projekt")}</span><span></span><span></span>'
                  + (f'<b class="nb">{t("nicht berechnet")}</b>' if sh == 0 else
                     f'<b>{t("{p} % der Kosten", p=fmt_num(sh * 100, 1))}</b>' if sh is not None else "<b>–</b>") + "</div>")
    return (
        f'<div class="{cls}" data-href="{href}"{attrs}>{_thumb(j)}'
        f'<div><a class="jn" href="{href}">{e(j["task_name"] or "–")}</a>{_pid(j)}{_models.dot(j)}{_chips(j)}{tag_html}</div>'
        f'<div class="jc"><b>{e(names.get(j["printer"], j["printer_name"] or j["printer"]))}</b>'
        f'<span>{when}{" · " + dur if dur != "–" else ""}</span></div>'
        f'<div class="jc r"><b>{fmt_num(grams, 1, "g")}</b><span>{fmt_num(j["kwh"], 3, "kWh")}</span></div>'
        f'<div class="calc">{_cost_box(j)}</div>{prices}<div class="js">{_status_badges(j)}</div></div>'
    )


def _mobile_card(j: dict[str, Any], names: dict[str, str], photo: str | None,
                 kids: list[dict[str, Any]] | None = None) -> str:
    """Karte für schmale Bildschirme statt der breiten Tabelle; Tippen öffnet die Details."""
    if j.get("kind") == "project":
        kids = kids or []
        meta = [e(x) for x in dict.fromkeys(names.get(k["printer"], k.get("printer_name") or k["printer"]) for k in kids)]
        meta.append(span_text(kids))
        if j.get("quantity"):
            meta.append(t("{q} Stk.", q=j["quantity"]))
        if j.get("customer_name"):
            meta.append(e(j["customer_name"]))
        if j.get("code"):
            meta.append(_pid(j))
        status = _status_of(j)
        return (f'<a class="mcard proj" href="job?id={j["id"]}" data-kind="project" data-name="{e(j["task_name"] or "")}" '
                f'data-cust="{j.get("customer_id") or ""}">{mosaic(kids)}'
                f'<div><div class="mt">{e(j["task_name"] or "–")}</div><div class="mp">📦 {t("Projekt")} · {tn(len(kids), "{n} Druck", "{n} Drucke")}</div></div>'
                f'<div class="mr"><b class="calc">{fmt_num(j.get("cost_total"), 2, "€")}</b>'
                f'<div class="s s-{e(status.split()[0])}">{e(t(status))}</div><div class="mcb">{customer_badges(j)}</div></div>'
                f'<div class="mm">{project_chips(kids)}<div class="meta"><div>{"".join(f"<span>{m}</span>" for m in meta if m)}</div></div>'
                f'{"" if j.get("customer_id") else cust_chip(j)}</div></a>')
    status = _status_of(j)
    grams = j["filament_g"] if j.get("filament_g") is not None else j["weight_g"]
    extra = ""
    if j["status"] == "abgebrochen" and j["progress"] is not None:
        extra = f'<div class="note">{t("bei {p} %", p=int(j["progress"]))}</div>'
    scrap = ""
    if j.get("is_scrap") and (j["status"] != "abgebrochen" or j.get("defect")):
        scrap = f'<div class="s s-abgebrochen">{e(defect_label(j) or t("Fehldruck"))}</div>'
    if charged(j) and billable(j):
        scrap += f'<div class="note">{e(charge_text(j))}</div>'
    q = j.get("quantity")
    meta = [e(names.get(j["printer"], j["printer_name"] or j["printer"])), fmt_ts(j["start_ts"])]
    if j["end_ts"]:
        meta.append(fmt_dur(j["start_ts"], j["end_ts"]))
    chips = _chips(j)
    if not chips and grams is not None:
        meta.append(fmt_num(grams, 1, "g"))
    if q and q > 1:
        meta.append(t("{q} Stk.", q=q))
    if j.get("customer_name"):
        meta.append(e(j["customer_name"]))
    if j.get("code"):
        meta.append(_pid(j))
    sub = j.get("project_title") or j.get("job_note")
    per = f'<div class="note">{_per_piece(j)}</div>' if _per_piece(j) else ""
    return (
        f'<a class="mcard" href="job?id={j["id"]}" data-name="{e(j["task_name"] or "")}" data-cust="{j.get("customer_id") or ""}">{_thumb(j, photo)}'
        f'<div><div class="mt">{e(j["task_name"] or "–")}</div>'
        + (f'<div class="mp">{e(sub)}</div>' if sub else "")
        + f'</div><div class="mr"><b class="calc">{fmt_num(j.get("cost_total"), 2, "€")}</b><span class="calc">{per}</span>'
        f'<div class="s s-{e(status.split()[0])}">{e(t(status))}</div>{extra}{scrap}'
        f'<div class="mcb">{customer_badges(j)}</div></div>'
        f'<div class="mm">{chips}<div class="meta"><div>{"".join(f"<span>{m}</span>" for m in meta if m)}</div></div>'
        f'{"" if j.get("customer_id") or j.get("project_id") else cust_chip(j)}</div></a>'
    )


def _gallery_card(j: dict[str, Any], names: dict[str, str], photo: str | None,
                  kids: list[dict[str, Any]] | None = None) -> str:
    status = _status_of(j)
    if j.get("kind") == "project":
        kids = kids or []
        cust = f' · {e(j["customer_name"])}' if j.get("customer_name") else ""
        img = _thumb(j, photo, "gimg") if photo else mosaic(kids, "gimg", count=False)
        return (f'<a class="gcard" href="job?id={j["id"]}">{img}'
                f'<div class="gt"><b>📦 {e(j["task_name"] or "–")}</b>'
                f'<span class="badge">{tn(len(kids), "{n} Druck", "{n} Drucke")} · {span_text(kids)}{cust} · {_pid(j)}</span><br>'
                f'<span class="badge"><span class="s s-{e(status.split()[0])}">{e(t(status))}</span> · '
                f'<span class="calc">{fmt_num(j.get("cost_total"), 2, "€")}</span></span></div></a>')
    cust = f' · {e(j["customer_name"])}' if j.get("customer_name") else ""
    badge = (" · 📷" if photo else "") + (" · 🔗" if j.get("project_url") else "") + (" · 📂" if j.get("local_path") else "")
    return (
        f'<a class="gcard" href="job?id={j["id"]}">{_thumb(j, photo, "gimg")}'
        f'<div class="gt"><b>{e(j["task_name"] or "–")}</b>'
        + (f'<span class="badge" style="display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{e(j["project_title"])}</span>' if j.get("project_title") else "")
        + f'<span class="badge">{e(names.get(j["printer"], j["printer"]))} · {fmt_ts(j["start_ts"])}{cust}{badge}{" · " + _pid(j) if j.get("code") else ""}</span><br>'
        f'<span class="badge"><span class="s s-{e(status.split()[0])}">{e(t(status))}</span> · '
        f'<span class="calc">{fmt_num(j.get("cost_total"), 2, "€")}</span></span></div></a>'
    )


# ---- Druckdetails -------------------------------------------------------------

DETAIL_CSS = """
.jd{font-size:16px;line-height:1.45}
.jtop{display:flex;justify-content:space-between;align-items:center;gap:10px}.jtop .jdel{margin:0}
.jact{display:flex;gap:8px;align-items:center;margin:0 0 10px}
.actmenu.share button{display:grid;gap:1px}.actmenu.share small{font-weight:500;color:var(--muted);font-size:12px}
.actmenu.share .chk{display:flex;align-items:center;gap:8px;padding:8px 6px 2px;font-size:13px;font-weight:600;cursor:pointer}
.actmenu.share .chk input{width:18px;height:18px;margin:0}
.actmenu.share .hint{padding:2px 6px;color:var(--muted);font-size:12px}
.jd .back{display:inline-flex;font-weight:600;padding:4px 0 10px;text-decoration:none}
.jd h3{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:700}
.hero{display:grid;grid-template-columns:220px minmax(0,1fr) minmax(280px,360px);gap:22px;align-items:start;
 background:var(--card);border:1px solid var(--line);border-radius:20px;padding:18px}
.hero .big{width:100%;aspect-ratio:1;object-fit:contain;background:var(--bg);border-radius:14px;border:1px solid var(--line);cursor:zoom-in;display:block}
.hero .noimg{display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:14px;cursor:default}
.jtitle{font-size:28px;line-height:1.15;margin:2px 0 10px;letter-spacing:-.01em;overflow-wrap:anywhere}
.badges{display:flex;flex-wrap:wrap;gap:8px}a.bdg{text-decoration:none}
.bdg{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:999px;background:var(--bg);
 border:1px solid var(--line);font-size:14px;font-weight:600;white-space:nowrap}
.bdg.st:before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor}
.bdg.st-ok{color:var(--ok)}.bdg.st-bad{color:var(--bad)}.bdg.st-warn{color:var(--warn)}.bdg.st-run{color:var(--run)}.bdg.st-gift{color:#b07cf0}
.kps{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:16px}
.kp{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:12px 14px;min-width:0}
.kp span{display:block;font-size:13px;color:var(--muted);font-weight:600}
.kp b{display:block;font-size:24px;font-variant-numeric:tabular-nums;margin-top:2px;white-space:nowrap}
.kp small{display:block;color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kp.main{border-color:var(--accent);background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 16%,transparent),transparent)}
.kp.main b{color:var(--accent);font-size:32px}
.prices{background:var(--bg);border:1px solid var(--line);border-radius:16px;padding:14px 16px}
.pr{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
.pr:last-child{border-bottom:0}.pr b{font-size:20px;font-variant-numeric:tabular-nums;white-space:nowrap}
.pr .tag{font-size:13px;color:var(--muted);white-space:nowrap}
.tabs{display:flex;gap:6px;margin:18px 0 14px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:5px;width:max-content;max-width:100%}
.tabs button{border:0;background:none;padding:10px 22px;border-radius:10px;color:var(--muted);font-weight:700;font-size:15px}
.tabs button.on{background:var(--accent);color:var(--bg)}
.pane{display:none}.pane.on{display:block}
.ovg{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;align-items:stretch}
.box{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;min-width:0}
.box+.box{margin-top:16px}.ovg .box+.box{margin-top:0}
.ssum{display:none}.sec>.sh{margin:0 0 10px}
.strip{display:flex;flex-wrap:wrap;align-items:center;gap:12px 18px;background:var(--card);border:1px solid var(--line);border-radius:16px;
 padding:12px 18px;margin-bottom:16px}
.strip .sa span{display:block;font-size:13px;color:var(--muted)}.strip .sa b{font-size:22px;font-variant-numeric:tabular-nums;margin-right:6px}
.strip .sa small{font-size:13px;color:var(--muted)}.strip .btn.sm{margin-left:auto;min-height:40px;padding:0 14px;font-weight:600}
.dealg{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-bottom:16px;align-items:stretch}
.dealg .box+.box{margin-top:0}
.deal{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:8px}
.deal span{display:block;font-size:13px;color:var(--muted)}.deal b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.deal small{color:var(--muted);font-size:13px}
.ho{display:grid;grid-template-columns:80px auto 1fr auto;gap:10px;align-items:center;padding:6px 0;border-top:1px solid var(--line);font-size:14px}
.ho form{margin:0}
.hof{display:flex;flex-wrap:wrap;gap:8px 10px;margin-top:12px;align-items:flex-end}.hof input{background:var(--bg)}
.hof .lf.amt{flex:0 0 104px}.hof .lf.amt input{text-align:right}.hof .lf.dt{flex:0 0 160px}.hof .lf.nt{flex:1 1 160px}
.hof .lf.fp{flex:0 0 130px}.hof .lf.fp input{text-align:right}.hof>.muted{align-self:center}
.facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:12px 16px}
.facts span{display:block;font-size:13px;color:var(--muted)}.facts b{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}
.mbar{display:flex;height:12px;border-radius:6px;overflow:hidden;margin:2px 0 10px;border:1px solid var(--line)}
.mrow{display:flex;align-items:center;gap:10px;padding:6px 0;font-size:15px}
.mrow .dot{width:16px;height:16px;margin:0;flex:none}
.mrow .mnote{display:block;font-size:13px;color:var(--muted)}
.fields .mfields{display:grid;gap:8px}.fields .mfields>span{font-size:13px;font-weight:600;color:var(--muted)}
.fields label.mf{display:grid;grid-template-columns:16px minmax(110px,220px) minmax(0,1fr);gap:10px;align-items:center;font-weight:500;color:var(--fg)}
.fields .mf .dot{width:16px;height:16px;margin:0}.fields .mf .mn{font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:760px){.fields label.mf{grid-template-columns:16px minmax(0,1fr)}.fields .mf input{grid-column:1/-1}}
.mrow .g{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}
.slot{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:0 6px;white-space:nowrap}
.jd .note{font-size:14px}
.fields{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:14px 16px}.fields>*{grid-column:span 3}
.fields label{display:grid;gap:6px;font-size:13px;font-weight:600;color:var(--muted)}.fields .full{grid-column:1/-1}.fields>.c3{grid-column:span 2}
.fields .nrow{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px 16px;align-items:end}
.fields .fl{display:grid;gap:6px}.fields .fl label{white-space:nowrap}
.step{display:inline-flex;align-items:center;justify-self:start;height:var(--ctl-h);padding:3px;gap:2px;border:1px solid var(--line);border-radius:var(--ctl-r);background:var(--bg)}
.fields .step input{width:48px;height:calc(var(--ctl-h) - 8px);padding:0;border:0;border-radius:7px;background:none;text-align:center;font-size:var(--ctl-fs);font-weight:700;font-variant-numeric:tabular-nums;-moz-appearance:textfield;appearance:textfield}
.step input::-webkit-outer-spin-button,.step input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.step:focus-within{border-color:var(--accent)}.fields .step input:focus{outline:none}
.step button{width:calc(var(--ctl-h) - 8px);height:calc(var(--ctl-h) - 8px);min-height:0;padding:0;border:0;border-radius:7px;background:var(--card);color:var(--fg);font-size:20px;font-weight:500;line-height:1;cursor:pointer;-webkit-tap-highlight-color:transparent;touch-action:manipulation;transition:background .12s,color .12s}
@media (hover:hover){.step button:hover{background:var(--accent);color:var(--bg)}}.step button:active{transform:scale(.94)}
.step button:disabled{opacity:.35;cursor:default;background:var(--card);color:var(--fg)}
.fields input,.fields select,.fields textarea{background:var(--bg);width:100%;color:var(--fg);font-weight:400}
.fields textarea{min-height:88px;resize:vertical}
.fields .act{grid-column:1/-1;display:flex;justify-content:flex-end}
.jd button.primary{padding:0 22px;font-weight:700}
.pane[data-pane=ed]>.box{max-width:var(--w-form)}
.photos{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:10px}
.pane .photos.lg{grid-template-columns:repeat(auto-fill,minmax(200px,1fr))}
.photos figure{margin:0;position:relative}.photos img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:12px;cursor:zoom-in;display:block}
.photos form{position:absolute;top:6px;right:6px}.photos button{min-height:0;background:rgba(0,0,0,.6);color:#fff;border:0;border-radius:8px;padding:4px 10px;font-size:16px}
.up{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.jd .btn{background:var(--bg)}.jd .btn.primary{background:var(--accent)}.jd .btn.danger{background:transparent}
.btn input{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}
.jdel{display:flex;justify-content:flex-end;margin-top:14px}
.fdbox{border-color:color-mix(in srgb,var(--bad) 45%,var(--line));background:color-mix(in srgb,var(--bad) 6%,var(--card));margin-bottom:16px}
.fdbox .fdh{display:flex;align-items:center;flex-wrap:wrap;gap:6px 12px;margin:0 0 14px}.fdbox .fdh>b{font-size:17px;color:var(--bad)}
.fdbox .fdh .sp{flex:1}.fdbox .fdh form{margin:0}
.fdg{display:grid;grid-template-columns:1.15fr 1fr 1.1fr;gap:22px}.fdg>div{min-width:0}
.fdg h4{margin:0 0 8px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.cats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0}
.cat{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;padding:9px 4px;min-height:64px;border:1px solid var(--line);
 border-radius:12px;background:var(--card);font-weight:600;font-size:13px;text-align:center;color:var(--muted);cursor:pointer;-webkit-tap-highlight-color:transparent}
.cat i{font-style:normal;font-size:20px;line-height:1.1}.cat:hover{border-color:color-mix(in srgb,var(--bad) 60%,var(--line))}
.cat.on{border-color:var(--bad);color:var(--fg);background:color-mix(in srgb,var(--bad) 18%,var(--card));box-shadow:0 0 0 1px var(--bad)}
.bseg{display:flex;border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:0 0 10px}
.bseg button{flex:1;border:0;border-radius:0;background:none;padding:10px 4px;font-weight:600;color:var(--muted);font-size:14px;cursor:pointer;min-height:44px}
.bseg button+button{border-left:1px solid var(--line)}.bseg button.on{background:var(--accent);color:var(--bg)}
.amt{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 10px}.amt input[name=defect_value]{width:90px;text-align:right;font-size:17px}
.amt .u{display:flex;border:1px solid var(--line);border-radius:var(--ctl-r);overflow:hidden}
.amt .u label{display:flex;align-items:center;padding:0 12px;min-height:38px;font-weight:600;color:var(--muted);cursor:pointer}
.amt .u input{position:absolute;opacity:0;pointer-events:none}.amt .u label:has(input:checked){background:color-mix(in srgb,var(--accent) 25%,var(--card));color:var(--fg)}
.res{display:grid;grid-template-columns:1fr 1fr;gap:10px}.res span{display:block;font-size:13px;color:var(--muted)}
.res b{font-size:20px;font-variant-numeric:tabular-nums}.res small{display:block;font-size:12px;color:var(--muted)}.res .bad b{color:var(--bad)}.res .ok b{color:var(--ok)}
.fdnote{font-size:14px;line-height:1.45;margin:0 0 10px;padding:9px 11px;border-left:3px solid var(--bad);white-space:pre-wrap;
 background:color-mix(in srgb,var(--fg) 4%,transparent);border-radius:0 8px 8px 0}
.fdph{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 10px}.fdph img,.fdph a.add{width:62px;height:62px;border-radius:10px;border:1px solid var(--line);object-fit:cover}
.fdph a.add{display:flex;align-items:center;justify-content:center;font-size:22px;color:var(--muted);text-decoration:none}
.rp{display:flex;align-items:center;gap:10px;padding:8px 11px;border:1px solid var(--line);border-radius:10px;font-size:14px;margin-bottom:8px}
.rp img{width:36px;height:36px;border-radius:8px;border:1px solid var(--line);object-fit:contain}.rp a.nm{color:var(--fg);text-decoration:none}
.rp b{display:block}.rp small{color:var(--muted)}.rp form{margin:0 0 0 auto}.rp .del{min-height:32px}
.rpf{display:flex;gap:8px;margin:0}.rpf select{flex:1;min-width:0}
@media (max-width:1100px){.fdg{grid-template-columns:1fr 1fr}.fdg>div:last-child{grid-column:1/-1}}
@media (max-width:760px){.fdg{grid-template-columns:1fr;gap:16px}.fdg>div:last-child{grid-column:auto}.fdbox .fdh .muted{flex-basis:100%;order:3}}
.lp{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:4px}.lp code{word-break:break-all;font-size:13px}
@media (min-width:1300px){.hero{grid-template-columns:260px minmax(0,1fr) minmax(300px,400px)}.kps{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media (max-width:1100px){.hero{grid-template-columns:200px minmax(0,1fr)}.prices{grid-column:1/-1}.ovg{grid-template-columns:1fr 1fr}}
@media (max-width:760px){.jd{font-size:15px}.hero{grid-template-columns:1fr;padding:12px;gap:14px}
 .hero .big{aspect-ratio:auto;height:240px}.jtitle{font-size:22px}
 .kps{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.kps .main{grid-column:1/-1}.kp{padding:10px}.kp b{font-size:18px}.kp.main b{font-size:28px}
 .ovg,.fields,.dealg{grid-template-columns:1fr}.fields>*,.fields>.c3{grid-column:1/-1}.fields .nrow{grid-template-columns:1fr}.tabs{width:100%}.tabs button{flex:1;padding:10px 6px}
 .hero{grid-template-columns:84px minmax(0,1fr);gap:10px 12px}.hero>div:first-child{grid-row:1}
 .hero .big{width:84px;height:84px;aspect-ratio:1;border-radius:12px}.hero .noimg{font-size:0}
 .hero>div:nth-child(2){display:contents}.jtitle{grid-column:2;grid-row:1;align-self:center;font-size:19px;margin:0}
 .hero .badges,.hero .kps,.hero .prices{grid-column:1/-1}.badges{gap:6px}.bdg{font-size:13px;padding:3px 10px}
 .kps{margin-top:0;grid-template-columns:repeat(2,minmax(0,1fr))}.kps .main{grid-column:auto}.kp.main b{font-size:24px}
 .tabs{position:sticky;top:0;z-index:6;margin:12px 0;box-shadow:0 6px 14px rgba(0,0,0,.25)}
 .ovg,.dealg{gap:10px}.dealg{margin-bottom:10px}
 .strip{display:grid;grid-template-columns:minmax(0,1fr) auto;padding:12px 14px;margin-bottom:10px;gap:10px}
 .strip .btn.sm{margin-left:0;grid-row:1;grid-column:2}.strip .badges{grid-column:1/-1}
 .tabs button{padding:10px 4px;font-size:14px}
 .sec{padding:0}.sec .sh{display:flex;align-items:center;gap:10px;margin:0;padding:15px 16px;cursor:pointer;min-height:52px;
  -webkit-tap-highlight-color:transparent;user-select:none}
 .sec .sh:after{content:"";width:8px;height:8px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);
  transform:rotate(45deg);margin:-4px 2px 0 0;transition:transform .15s;flex:none}
 .sec.open .sh:after{transform:rotate(-135deg);margin-top:4px}
 .sec .ssum{display:block;margin-left:auto;text-transform:none;letter-spacing:0;font-size:14px;font-weight:600;color:var(--fg);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;text-align:right}
 .sec.open .ssum{display:none}.sec .sb{display:none;padding:0 16px 16px}.sec.open .sb{display:block}
 .hero .prices.sec .sh{padding:13px 14px}}
"""

DETAIL_JS = """
document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=async()=>{
  const t=b.dataset.copy,old=b.textContent;let ok=false;
  try{await navigator.clipboard.writeText(t);ok=true}catch(_){}
  if(!ok){const a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity='0';
    document.body.appendChild(a);a.select();try{ok=document.execCommand('copy')}catch(_){}a.remove()}
  b.textContent=ok?_t("✓ Kopiert"):_t("Strg+C");if(!ok)prompt(_t("Kopieren:"),t);
  setTimeout(()=>b.textContent=old,1500)});
document.querySelectorAll('img.big,.photos img').forEach(i=>i.onclick=()=>z(i.src));
document.querySelectorAll('.photos form').forEach(f=>f.onsubmit=()=>confirm(_t("Foto löschen?")));
bindAct(()=>location.reload());
document.querySelectorAll('.step').forEach(w=>{const i=w.querySelector('input'),bs=w.querySelectorAll('button');
 const sync=()=>{bs[0].disabled=(parseInt(i.value)||1)<=1};
 bs.forEach(b=>b.onclick=()=>{i.value=Math.max(1,(parseInt(i.value)||1)+ +b.dataset.step);sync()});
 i.addEventListener('input',sync);sync()});
(function(){let st={};try{st=JSON.parse(localStorage.getItem('pl_secs')||'{}')}catch(_){}
 const secs=[...document.querySelectorAll('.sec')];const has=k=>secs.some(x=>x.dataset.sec===k);
 secs.forEach(x=>{const k=x.dataset.sec;const def=has('pay')?k==='pay':k==='druck';
   x.classList.toggle('open',k in st?st[k]:def);
   x.querySelector('.sh').onclick=()=>{if(!matchMedia('(max-width:760px)').matches)return;
     const o=x.classList.toggle('open');st[k]=o;try{localStorage.setItem('pl_secs',JSON.stringify(st))}catch(_){}}});})();
document.querySelectorAll('.ho form').forEach(f=>f.onsubmit=()=>confirm(_t("Eintrag löschen?")));
document.querySelectorAll('form.jdel').forEach(f=>f.onsubmit=()=>confirm(_t("Diesen Druck in den Papierkorb legen? Du kannst ihn unter Einstellungen → Papierkorb wiederherstellen.")));
document.querySelectorAll('.up input[type=file]').forEach(inp=>inp.onchange=()=>{
  if(!inp.files.length) return;
  const f=inp.form;f.querySelectorAll('input[type=file]').forEach(o=>{if(o!==inp)o.disabled=true});
  f.querySelector('.up-status').textContent=_t("Lade hoch …");f.submit();});
function tab(n,push){if(!document.querySelector('.pane[data-pane="'+n+'"]'))return;
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.tab===n));
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.dataset.pane===n));
  if(push)history.replaceState(null,'','#'+n)}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>tab(b.dataset.tab,true));
document.querySelectorAll('[data-goto]').forEach(a=>a.onclick=ev=>{ev.preventDefault();tab(a.dataset.goto,true);scrollTo({top:0})});
if(location.hash)tab(location.hash.slice(1),false);
addEventListener('hashchange',()=>{closeAct();tab(location.hash.slice(1),false);scrollTo({top:0})});
"""

DETAIL_TABS = {
    "ov": N_("Übersicht"),
    "ed_missing": N_("Bearbeiten, solange Kunde oder Stückzahl fehlen"),
    "ed": N_("Immer Bearbeiten"),
}

_STATUS_TONE = {"fertig": "ok", "abgebrochen": "bad", "unvollständig": "warn", "läuft": "run", "kühlt": "run"}


def _upload_form(job_id: int) -> str:
    return (f'<form method="post" action="api/jobs/{job_id}/photos" enctype="multipart/form-data" class="up">'
            f'<label class="btn primary">{t("📷 Foto aufnehmen")}<input type="file" name="photo" accept="image/*" capture="environment"></label>'
            f'<label class="btn">{t("🖼 Aus Galerie oder Dateien")}<input type="file" name="photo" accept="image/*" multiple></label>'
            f'<span class="muted up-status" style="font-size:13px">{t("Das Foto wird nach der Auswahl automatisch hochgeladen.")}</span></form>')


def _photo_tiles(photos: list[dict[str, Any]]) -> str:
    return "".join(
        f'<figure><img src="images/{e(p["file"])}" loading="lazy" alt="">'
        f'<form method="post" action="api/photos/{p["id"]}/delete"><button title="{e(t("Foto löschen"))}">×</button></form></figure>'
        for p in photos
    )


def _sec(key: str, title: str, summary: str, body: str, cls: str = "box") -> str:
    """Box, die sich auf dem Handy einklappen lässt; zugeklappt steht die Zusammenfassung in der Kopfzeile."""
    return (f'<div class="{cls} sec" data-sec="{key}"><h3 class="sh"><span>{title}</span>'
            f'<span class="ssum">{summary}</span></h3><div class="sb">{body}</div></div>')


def _deal_box(job: dict[str, Any], handovers: list[dict[str, Any]], payments: list[dict[str, Any]]) -> str:
    """Box „Preis, Zahlung & Übergabe“ auf der Detailseite (nur mit Kunde, der abgerechnet wird)."""
    if not billable(job):
        return ""
    if no_charge(job):
        return (f'<div class="dealg"><div class="box"><h3>{t("Zahlung &amp; Übergabe")}</h3>'
                f'<p class="muted" style="margin:0">{t("Fehldruck, wird nicht berechnet.")} '
                f'<a href="#ov" data-goto="ov">{t("Ändern im Fehldruck-Kasten →")}</a></p></div></div>')
    jid = job["id"]
    n = job.get("quantity") or 1
    d = job.get("delivered") or 0
    due = amount_due(job)
    paid = job.get("paid") or 0
    rest = max(0.0, (due or 0) - paid)
    today = datetime.now().strftime("%Y-%m-%d")
    suggest = _price(job, job.get("customer_margin") or 0, bool(job.get("customer_work")))
    src = (t("Endpreis") if job.get("final_price") is not None
           else t("Vorschlag: Kosten + {p} % Kundenmarge", p=fmt_input(job.get("customer_margin") or 0)))
    fd_note = ""
    if charged(job):
        src = charge_src(job, base_due(job))
        fd_note = (f'<div class="fdnote">⚠ {e(t("Fehldruck"))}{" · " + e(defect_label(job)) if defect_label(job) else ""} · {e(charge_text(job))} '
                   f'<a href="#ov" data-goto="ov">{t("ändern")}</a></div>')

    def entry(ts: float, what: str, note: str | None, url: str, title: str) -> str:
        return (f'<div class="ho"><span>{fmt_date(ts, "date_s")}</span><b>{what}</b>'
                f'<span class="muted">{e(note or "")}</span>'
                f'<form method="post" action="{url}"><button class="del" title="{title}">×</button></form></div>')

    pay_rows = "".join(entry(p["ts"], fmt_num(p["amount"], 2, "€"), p["note"], f'api/payments/{p["id"]}/delete', t("Zahlung löschen"))
                       for p in payments)
    ho_rows = "".join(entry(h["ts"], _pcs(h["qty"]), h["note"], f'api/handovers/{h["id"]}/delete', t("Übergabe löschen"))
                      for h in handovers)
    price_form = (f'<form class="hof" method="post" action="api/jobs/{jid}?redirect=ab">'
                  f'<label class="lf fp">{t("Endpreis €")}<input name="final_price" inputmode="decimal" value="{fmt_input(job.get("final_price"))}" '
                  f'placeholder="{fmt_input(round(suggest, 2)) if suggest is not None else ""}"></label>'
                  f'<button class="btn">{t("Speichern")}</button></form>')
    pay_form = ""
    if due is not None and rest >= 0.005:
        pay_form = (f'<form class="hof" method="post" action="api/jobs/{jid}/pay?redirect=1">'
                    f'<label class="lf amt">{t("Betrag €")}<input name="amount" inputmode="decimal" value="{fmt_input(round(rest, 2))}"></label>'
                    f'<label class="lf dt">{t("Datum")}<input name="date" type="date" value="{today}"></label>'
                    f'<label class="lf nt">{t("Notiz")}<input name="note" placeholder="{e(t("optional"))}"></label>'
                    f'<button class="btn primary">{t("Zahlung eintragen")}</button></form>')
    ho_form = ""
    if job.get("state") == "done" and d < n:
        ho_form = (f'<form class="hof" method="post" action="api/jobs/{jid}/handover?redirect=1">'
                   f'<label class="lf amt">{t("Stück")}<input name="qty" type="number" min="1" max="{n - d}" value="{n - d}" inputmode="numeric"></label>'
                   f'<label class="lf dt">{t("Datum")}<input name="date" type="date" value="{today}"></label>'
                   f'<label class="lf nt">{t("Notiz")}<input name="note" placeholder="{e(t("optional"))}"></label>'
                   f'<button class="btn primary">{t("Übergabe eintragen")}</button></form>')
    pay_state = (t("bezahlt") if due is not None and rest < 0.005 else
                 t("{paid} bezahlt, {rest} offen", paid=fmt_num(paid, 2, '€'), rest=fmt_num(rest, 2, '€')) if due is not None
                 else t("Preis fehlt noch"))
    gift_form = (f'<form class="hof" method="post" action="api/jobs/{jid}?redirect=ab"><input type="hidden" name="gift" value="{0 if job.get("gift") else 1}">'
                 f'<button class="btn">{t("Doch berechnen") if job.get("gift") else t("🎁 Als verschenkt markieren")}</button>'
                 f'<span class="muted">{t("Zählt weiter in Kosten und Statistik.") if not job.get("gift") else ""}</span></form>')
    if job.get("gift"):
        pay_body = (f'<div class="deal"><div><span>{e(job.get("customer_name") or t("Kunde"))}</span><b>{t("🎁 verschenkt")}</b>'
                    f'<small>{t("keine Zahlung nötig, zählt weiter in Kosten und Statistik")}</small></div></div>{pay_rows}{gift_form}')
    else:
        pay_body = ""
    over = overpaid(job)
    shown_due = due
    if over:
        # Mehr bezahlt als der Preis: das Gezahlte ist der tatsächliche Preis, der Rest steht klein darunter.
        shown_due = paid
        short = t("Endpreis") if job.get("final_price") is not None else t("Fehldruck") if charged(job) else t("Vorschlag")
        src = t("{src} {v} · {o} mehr bezahlt", src=short, v=fmt_num(due, 2, "€"), o=fmt_num(over, 2, "€"))
        pay_state = t("bezahlt, {v} mehr als der Preis", v=fmt_num(over, 2, "€"))
        if job.get("final_price") is None or abs(job["final_price"] - paid) >= 0.005:
            price_form += (f'<form class="hof" method="post" action="api/jobs/{jid}?redirect=ab">'
                           f'<input type="hidden" name="final_price" value="{fmt_input(round(paid, 2))}">'
                           f'<button class="btn">{t("{v} als Endpreis übernehmen", v=fmt_num(paid, 2, "€"))}</button>'
                           f'<span class="muted">{t("Dann zählt der Betrag als Preis statt als Trinkgeld.")}</span></form>')
    pay_body = pay_body or (f'<div class="deal"><div><span>{e(t("{name} zahlt", name=job.get("customer_name") or t("Kunde")))}</span><b>{fmt_num(shown_due, 2, "€")}</b>'
            f'<small class="calc">{e(src)}</small></div><div><span>{t("Stand")}</span><b>{fmt_num(paid, 2, "€")}</b><small>{pay_state}</small></div></div>'
            f'{price_form}{pay_rows}{pay_form}{gift_form}')
    pay_body = fd_note + pay_body
    ho_body = (f'<div class="deal"><div><span>{t("Übergeben")}</span><b>{t("{d} von {n} Stück", d=min(d, n), n=n)}</b>'
               f'<small>{t("alles übergeben") if d >= n else t("noch {k} offen", k=n - d)}</small></div></div>{ho_rows}{ho_form}')
    return (f'<div class="dealg"><div class="box"><h3>{t("Preis &amp; Zahlung")}</h3>{pay_body}</div>'
            f'<div class="box"><h3>{t("Übergabe")}</h3>{ho_body}</div></div>')


def _defect_box(store: Store, job: dict[str, Any], photos: list[dict[str, Any]]) -> str:
    """Kasten „Fehldruck“ in der Übersicht: Ursache, was der Kunde zahlt, Notiz/Fotos und Nachdruck."""
    if not job.get("is_scrap"):
        return ""
    jid = job["id"]
    act = f"api/jobs/{jid}?redirect=1"
    cur = job.get("defect") if job.get("defect") in DEFECTS else ""
    if job["status"] == "abgebrochen":
        sub = t("Abgebrochen bei {p} %", p=int(job["progress"])) if job.get("progress") is not None else t("Abgebrochen")
    else:
        sub = t("Druck ist fertig, aber nicht so wie gedacht")
    if not cur:
        sub += " · " + t("Ursache auswählen")
    head = (f'<div class="fdh"><b>⚠ {t("Fehldruck")}</b><span class="muted">{e(sub)}</span><span class="sp"></span>'
            f'<form method="post" action="{act}"><input type="hidden" name="scrap" value="0">'
            f'<button class="btn sm" title="{e(t("Doch in Ordnung: zählt dann wieder als normaler Druck"))}">{t("Kein Fehldruck")}</button></form></div>')
    cats = "".join(
        f'<button class="cat{" on" if k == cur else ""}" name="defect" value="{"" if k == cur else k}" '
        f'title="{e(t("Auswahl aufheben") if k == cur else t(name))}"><i>{sym}</i>{e(t(name))}</button>'
        for k, (sym, name) in DEFECTS.items())
    cause = f'<div><h4>{t("Ursache")}</h4><form class="cats" method="post" action="{act}">{cats}</form></div>'

    cost = job.get("cost_total")
    if has_billing(job):
        mode = job.get("defect_charge") if charged(job) else "none"
        seg = "".join(f'<button name="defect_charge" value="{v}" class="{"on" if (mode in ("pct", "amount") and v == "part") or mode == v else ""}">{lbl}</button>'
                      for v, lbl in (("none", t("Nichts")), ("part", t("Anteilig")), ("full", t("Voll"))))
        base = base_due(job)
        name = job.get("customer_name") or t("Kunde")
        amt = ""
        if mode in ("pct", "amount"):
            val = job.get("defect_value")
            amt = (f'<form class="amt" method="post" action="{act}"><input type="hidden" name="defect_charge" value="part">'
                   f'<input name="defect_value" inputmode="decimal" value="{fmt_input(val)}" aria-label="{e(t("Anteil"))}">'
                   f'<span class="u"><label><input type="radio" name="defect_unit" value="pct"{" checked" if mode == "pct" else ""}>%</label>'
                   f'<label><input type="radio" name="defect_unit" value="eur"{" checked" if mode == "amount" else ""}>€</label></span>'
                   f'<button class="btn">{t("Speichern")}</button>'
                   f'<span class="muted" style="font-size:13px">{t("von {v}", v=fmt_num(base, 2, "€"))} '
                   f'({t("Endpreis") if job.get("final_price") is not None else t("Kosten + Kundenmarge")})</span></form>')
        due = amount_due(job) if mode != "none" else 0.0
        gap = None if cost is None or due is None else cost - due
        left = (f'<div><span>{e(t("{name} zahlt", name=name))}</span><b class="calc">{fmt_num(due, 2, "€")}</b>'
                f'<small>{(t("zählt nicht zum Projektpreis") if mode == "none" else t("fließt in den Projektpreis ein")) if job.get("project_id") else t("nichts, fällt aus „Offen“ heraus") if mode == "none" else t("steht unter „Offen“ und auf dem Bon")}</small></div>')
        right = (f'<div class="{"bad" if gap is not None and gap >= 0.005 else "ok"}"><span>{t("Ungedeckt") if gap is None or gap >= 0.005 else t("Gedeckt")}</span>'
                 f'<b class="calc">{fmt_num(max(gap, 0) if gap is not None else None, 2, "€")}</b>'
                 f'<small class="calc">{t("Kosten {c} − {d}", c=fmt_num(cost, 2, "€"), d=fmt_num(due, 2, "€"))}</small></div>')
        bill = (f'<div><h4>{e(t("Was bekommt {name} berechnet?", name=name))}</h4>'
                f'<form class="bseg" method="post" action="{act}">{seg}</form>{amt}<div class="res">{left}{right}</div></div>')
    else:
        why = t("Ohne Kunde gibt es nichts zu berechnen.") if not job.get("customer_id") else t("Kunde mit „Keine Abrechnung“.")
        bill = (f'<div><h4>{t("Berechnung")}</h4><p class="muted" style="margin:0 0 10px">{why}</p>'
                f'<div class="res"><div class="bad"><span>{t("Fehldruck-Kosten")}</span><b class="calc">{fmt_num(cost, 2, "€")}</b>'
                f'<small>{t("trägst du selbst")}</small></div></div></div>')

    note = (f'<div class="fdnote">{e(job["job_note"])}</div>' if job.get("job_note") else
            f'<p class="muted" style="margin:0 0 10px;font-size:14px">{t("Noch keine Notiz.")} <a href="#ed" data-goto="ed">{t("Im Tab „Bearbeiten“ beschreiben →")}</a></p>')
    pics = "".join(f'<a href="#ph" data-goto="ph"><img src="images/{e(p["file"])}" loading="lazy" alt=""></a>' for p in photos[:3])
    pics = f'<div class="fdph">{pics}<a class="add" href="#ph" data-goto="ph" title="{e(t("Fotos hinzufügen"))}">＋</a></div>'
    rps = store.reprints(jid)
    if rps:
        rp_html = "".join(
            f'<div class="rp">{_thumb(r)}<a class="nm" href="job?id={r["id"]}"><b>{t("Nachdruck {ref}", ref=e(code_label(r.get("code")) or "#" + str(r["id"])))}</b>'
            f'<small>{fmt_date(r["start_ts"], "dm") if r.get("start_ts") else "–"} · {e(t(_status_of(r)))} · {fmt_num(r.get("cost_total"), 2, "€")}</small></a>'
            f'<form method="post" action="{act}"><input type="hidden" name="reprint" value="">'
            f'<button class="btn sm del" title="{e(t("Zuordnung lösen"))}">×</button></form></div>'
            for r in rps)
    else:
        cands = store.reprint_candidates(job)
        if cands:
            options = opt("", t("– Nachdruck wählen –"), "") + "".join(
                opt(str(c["id"]), " · ".join(x for x in (code_label(c.get("code")), c.get("task_name") or "–",
                                                          fmt_date(c["start_ts"], "dm") if c.get("start_ts") else "") if x), "")
                for c in cands)
            rp_html = (f'<form class="rpf" method="post" action="{act}"><select name="reprint" aria-label="{e(t("Nachdruck"))}">{options}</select>'
                       f'<button class="btn">{t("Zuordnen")}</button></form>')
        else:
            rp_html = f'<p class="muted" style="margin:0;font-size:14px">{t("Noch kein späterer Druck, der als Nachdruck passt.")}</p>'
    what = f'<div><h4>{t("Was ist passiert?")}</h4>{note}{pics}<h4 style="margin-top:12px">{t("Nachdruck")}</h4>{rp_html}</div>'
    return f'<div class="box fdbox" id="fd">{head}<div class="fdg">{cause}{bill}{what}</div></div>'


def _deal_strip(job: dict[str, Any]) -> str:
    """Kompakte Zeile in der Übersicht: Betrag + antippbare Badges, Details im Tab „Abrechnung“."""
    if not billable(job) or no_charge(job):
        return ""
    due = amount_due(job)
    src = t("Endpreis") if job.get("final_price") is not None else t("Vorschlag")
    if charged(job):
        src = charge_src(job, base_due(job))
    name = job.get("customer_name") or t("Kunde")
    over = overpaid(job)
    if over:
        due = job.get("paid") or 0
        src = t("{o} mehr als {src}", o=fmt_num(over, 2, "€"), src=src)
    head = (f'<span>{e(name)}</span><b>{t("nichts")}</b><small>{t("verschenkt")}</small>' if job.get("gift") else
            f'<span>{e(t("{name} zahlt", name=name))}</span><b>{fmt_num(due, 2, "€")}</b><small class="calc">{src}</small>')
    return (f'<div class="strip"><div class="sa">{head}</div>'
            f'<div class="badges">{customer_badges(job, "bdg st")}</div>'
            f'<a href="#ab" data-goto="ab" class="btn sm">{t("Abrechnung →")}</a></div>')


def _share_button(job: dict[str, Any]) -> str:
    """Knopf „Teilen“: Bon-Bild für Kunden, Bon intern, PDF (Liste für SHARE_JS)."""
    from .export import file_name

    jid = job["id"]
    items = [
        (t("🧾 Bild für Kunden"), t("Foto, Stückzahl, Preis, Zahlungsstand"), f"export/{jid}/bon-kunde.png",
         file_name(job, "kunde", "png"), "image/png", 0),
        (t("🧾 Bild intern"), t("zusätzlich Kosten, Margen und Gewinn"), f"export/{jid}/bon-intern.png",
         file_name(job, "intern", "png"), "image/png", 1),
        ("📄 PDF", t("volle Aufschlüsselung zum Ablegen"), f"export/{jid}/kosten.pdf",
         file_name(job, "kosten", "pdf"), "application/pdf", 1),
    ]
    pos = ' data-pos="1"' if job.get("cost_extras") or _extras.work_total(job) else ""
    return (f'<button type="button" class="btn sm" id="share"{pos} data-exports="{e(json.dumps(items))}" '
            f'title="{e(t("Als Bild oder PDF teilen bzw. speichern"))}">{t("📤 Teilen")}</button>')


# Teilen: am Handy/Tablet über das Teilen-Menü des Systems (z. B. WhatsApp), sonst Download.
SHARE_JS = """
(function(){const b=document.getElementById('share');if(!b)return;
 const items=JSON.parse(b.dataset.exports);const touch=matchMedia('(pointer:coarse)').matches;
 function save(blob,name){const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;
   document.body.appendChild(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},5000)}
 async function share(file){await navigator.share({files:[file],title:file.name})}
 async function run(x,it,st,ps){const [label,desc,u,name,type]=it;const qs=[st?'':'status=0',ps?'pos=1':''].filter(Boolean).join('&');const url=u+(qs?'?'+qs:'');x.disabled=true;x.querySelector('small').textContent=_t("wird erstellt …");
   try{const r=await fetch(url);if(!r.ok)throw new Error(r.status);const blob=await r.blob();
     const file=new File([blob],name,{type});
     if(touch&&navigator.canShare&&navigator.canShare({files:[file]})){
       try{await share(file);closeAct();return}
       catch(err){if(err.name==='AbortError'){closeAct();return}
         if(err.name==='NotAllowedError'){x.disabled=false;x.querySelector('small').textContent=_t("fertig, nochmal tippen zum Teilen");
           x.onclick=async ev=>{ev.stopPropagation();try{await share(file)}catch(_){}closeAct()};return}}}
     save(blob,name);closeAct()}
   catch(err){x.disabled=false;x.querySelector('small').textContent=_t("Fehler, bitte nochmal tippen")}}
 b.onclick=ev=>{ev.stopPropagation();const open=actMenu&&actMenu.dataset.for==='share';closeAct();if(open)return;
   const m=document.createElement('div');m.className='actmenu share';m.dataset.for='share';
   const t=document.createElement('b');t.textContent=touch?_t("Teilen"):_t("Herunterladen");m.appendChild(t);
   const priv=document.documentElement.classList.contains('priv');
   const cl=document.createElement('label');cl.className='chk';const cb=document.createElement('input');cb.type='checkbox';
   let keep='1';try{keep=localStorage.getItem('pl_share_status')||'1'}catch(_){}cb.checked=keep==='1';
   cb.onchange=()=>{try{localStorage.setItem('pl_share_status',cb.checked?'1':'0')}catch(_){}};
   cl.onclick=e3=>e3.stopPropagation();cl.append(cb,document.createTextNode(' '+_t("Bezahlt/Übergeben mit drauf")));
   let pl=null,pb=null;if(b.dataset.pos){pl=document.createElement('label');pl.className='chk';pb=document.createElement('input');pb.type='checkbox';
     let kp='0';try{kp=localStorage.getItem('pl_share_pos')||'0'}catch(_){}pb.checked=kp==='1';
     pb.onchange=()=>{try{localStorage.setItem('pl_share_pos',pb.checked?'1':'0')}catch(_){}};
     pl.onclick=e3=>e3.stopPropagation();pl.append(pb,document.createTextNode(' '+_t("Zubehör und Arbeit auf dem Kunden-Bon")))}
   items.forEach(it=>{if(it[5]&&priv)return;const x=document.createElement('button');x.type='button';
     const l=document.createElement('span');l.textContent=it[0];const s=document.createElement('small');s.textContent=it[1];
     x.append(l,s);x.onclick=e2=>{e2.stopPropagation();run(x,it,cb.checked,pb&&pb.checked)};m.appendChild(x)});
   m.appendChild(cl);if(pl)m.appendChild(pl);
   if(priv){const n=document.createElement('small');n.className='hint';n.textContent=_t("Diskret-Modus: interne Varianten ausgeblendet");m.appendChild(n)}
   document.body.appendChild(m);const rc=b.getBoundingClientRect();
   m.style.top=(scrollY+rc.bottom+6)+'px';
   m.style.left=Math.max(8,Math.min(scrollX+rc.right-m.offsetWidth,scrollX+innerWidth-m.offsetWidth-8))+'px';actMenu=m}})();
"""


def page_job(store: Store, job: dict[str, Any], saved: str | None) -> str:
    if job.get("kind") == "project":
        return page_project(store, job, saved)
    if job.get("kind") == "share":
        return _split.page_share(store, job, saved)
    parent = store.get(job["project_id"]) if job.get("project_id") else None
    split = bool(job.get("split"))
    if split:
        _split.attach(store, [job])
    names = printer_names(store)
    customers = store.customers()
    photos = store.photos(job["id"])
    status = _status_of(job)
    qty = job.get("quantity") or 0
    total = job.get("cost_total")
    big = (f'<img class="big" src="images/{e(job["image"])}" alt="">' if job.get("image")
           else f'<div class="big noimg">{t("kein Vorschaubild")}</div>')
    grams = job["filament_g"] if job.get("filament_g") is not None else job["weight_g"]

    # Kopfbereich: Badges, Kennzahlen, Verkaufspreise
    tone = _STATUS_TONE.get(status.split()[0], "")
    extra = " " + t("bei {p} %", p=int(job["progress"])) if job["status"] == "abgebrochen" and job["progress"] is not None else ""
    badges = f'<span class="bdg st st-{tone}">{e(t(status))}{extra}</span>'
    if not parent:
        add_suggestions(store, [job])
        badges += cust_chip(job, "bdg")
        if (job.get("quantity") or 1) > 1:
            badges += _split.split_button(job)
    if job.get("code"):
        badges += (f'<button type="button" class="bdg pidb" data-copy="{e(code_label(job["code"]))}" '
                   f'title="{e(t("Projekt-ID: tippe sie in die Suche, um den Druck wiederzufinden. Klick kopiert."))}">'
                   f'# {e(code_label(job["code"]))}</button>')
    badges += _models.chip(store, job)
    if job.get("is_scrap"):
        lbl = defect_label(job)
        badges += f'<a class="bdg st st-bad" href="#ov" data-goto="ov">⚠ {t("Fehldruck")}{" · " + e(lbl) if lbl else ""}</a>'
        if charged(job) and billable(job):
            badges += f'<span class="bdg st st-warn">{e(charge_text(job))}</span>'
    if job.get("reprint_of"):
        orig = store.get(job["reprint_of"])
        if orig:
            badges += (f'<a class="bdg" href="job?id={orig["id"]}" title="{e(t("Ersetzt einen Fehldruck"))}">'
                       f'↻ {t("Nachdruck von {ref}", ref=e(code_label(orig.get("code")) or "#" + str(orig["id"])))}</a>')
    if parent:
        badges += (f'<a class="bdg pjb" href="job?id={parent["id"]}" title="{e(t("Gehört zum Projekt {name}", name=parent.get("task_name") or ""))}">'
                   f'📦 {e(code_label(parent.get("code")))}</a>')
    badges += f'<span class="bdg">🖨 {e(names.get(job["printer"], job["printer"]))}</span>'
    if parent and job.get("customer_name"):
        badges += f'<span class="bdg">👤 {e(job["customer_name"])}</span>'
    if qty > 1:
        badges += f'<span class="bdg">{_pcs(qty)}</span>'
    work = _extras.work_total(job)
    if total is not None or work:
        total = (total or 0) + work
    per = (t("wird nach dem Druck berechnet") if total is None
           else t("{v} pro Stück", v=fmt_num(total / qty, 2, "€")) if qty > 1 else t("für den ganzen Druck"))
    xk = extra_kpis(job)
    kpis = (f'<div class="kp main"><span>{t("Gesamtkosten")}</span><b>{fmt_num(total, 2, "€")}</b><small>{per}</small></div>'
            f'<div class="kp"><span>{t("⚡ Strom")}</span><b>{fmt_num(job["cost"], 2, "€")}</b><small>{fmt_num(job["kwh"], 3, "kWh")}</small></div>'
            f'<div class="kp"><span>{t("🧵 Filament")}</span><b>{fmt_num(job.get("cost_filament"), 2, "€")}</b><small>{fmt_num(grams, 1, "g")}</small></div>'
            f'<div class="kp"><span>{t("🖨 Drucker")}</span><b>{fmt_num(job.get("cost_printer"), 2, "€")}</b><small>{t("Abschr. + Wartung")}</small></div>') + xk
    mg = list(margins(store))
    if customer_margin(job):
        mg.insert(0, customer_margin(job))
    price_rows = ""
    for m in mg:
        n, p = m
        price = mprice(job, m)
        each = (f' <span class="tag">{t("{v}/Stk.", v=fmt_num(price / qty, 2, "€"))}</span>'
                if price is not None and qty > 1 else "")
        price_rows += (f'<div class="pr"><div>{e(n)} <span class="tag">+{e(fmt_input(p))} %{work_tag(m, work)}</span></div>'
                       f'<div><b>{fmt_num(price, 2, "€")}</b>{each}</div></div>')
    price_rows += profit_rows(job, qty)
    if not price_rows:
        price_rows = f'<p class="muted" style="margin:6px 0 0">{t("Keine Margen eingestellt (Einstellungen).")}</p>'
    elif work:
        price_rows += f'<p class="muted wnote">{t("Marge auf Druck + Zubehör, Arbeit zum Stundensatz (außer „inkl. Arbeit“)")}</p>'
    pj_banner = pj_strip = ""
    if parent:
        sibs = store.children([parent["id"]]).get(parent["id"], [])
        pos = next((i + 1 for i, k in enumerate(sibs) if k["id"] == job["id"]), 0)
        sh = share_of(job, parent)
        due = amount_due(parent) if billable(parent) else None
        price_rows = (f'<div class="share"><div class="bigv">{t("nicht berechnet") if sh == 0 else (fmt_num(sh * 100, 1) + " %") if sh is not None else "–"}</div>'
                      f'<div class="muted">{t("{c} von {b} berechneten Kosten", c=fmt_num(job.get("cost_total") if sh else 0, 2, "€"), b=fmt_num(price_base(parent), 2, "€"))}</div>'
                      '<div class="bar3">' + "".join(
                          f'<i style="flex:{max(share_of(k, parent) or 0, 0):.4f}" class="{"me" if k["id"] == job["id"] else ""}"></i>' for k in sibs) + '</div>'
                      + (f'<div>{t("Entspricht <b>{v}</b> vom Preis {p}", v=fmt_num(due * sh, 2, "€"), p=fmt_num(due, 2, "€"))}</div>'
                         if due is not None and sh else "") + '</div>')
        pj_banner = (f'<div class="pjbanner">{mosaic(sibs)}<div class="t2"><b>📦 {t("Teil von „{name}“", name=e(parent.get("task_name") or "–"))} · {e(code_label(parent.get("code")))}</b>'
                     f'<small>{t("Druck {i} von {n}", i=pos, n=len(sibs))} · {t("Kunde, Preis, Zahlung und Übergabe laufen über das Projekt.")}</small></div>'
                     f'<a class="btn primary" href="job?id={parent["id"]}">{t("Zum Projekt →")}</a></div>')
        if has_billing(parent):
            pd, pp = amount_due(parent), parent.get("paid") or 0
            info = [fmt_num(pd, 2, "€")]
            if pd is not None and not parent.get("gift"):
                info.append(t("bezahlt") if pp >= pd - 0.005 else t("{v} offen", v=fmt_num(pd - pp, 2, "€")))
            n, d = parent.get("quantity") or 1, min(parent.get("delivered") or 0, parent.get("quantity") or 1)
            info.append(t("{d} von {n} übergeben", d=d, n=n))
            pj_strip = (f'<div class="strip"><div class="sa"><span>{t("Abrechnung")}</span><b class="pjn">{t("im Projekt {name}", name=e(parent.get("task_name") or ""))}</b>'
                        f'<small class="calc">{" · ".join(info)}</small></div><a href="job?id={parent["id"]}#ab" class="btn sm">{t("Zum Projekt →")}</a></div>')

    # Übersicht
    materials = parse_materials(job.get("materials"))
    mat_total = sum(m["grams"] or 0 for m in materials)
    mat_bar = ('<div class="mbar">' + "".join(
        f'<div style="flex:{m["grams"]};background:{_color(m["color"])}"></div>' for m in materials if m["grams"]
    ) + "</div>") if mat_total > 0 else ""
    mnotes = material_notes(job)
    mat_rows = "".join(
        f'<div class="mrow"><span class="dot" style="background:{_color(m["color"])}"></span>'
        f'<span>{e(m["name"] or "?")} <span class="muted" style="font-size:13px">{e(m["type"] or "")}</span>'
        + (f'<span class="mnote">{e(mnotes[i])}</span>' if i in mnotes else "") + '</span>'
        + (f'<span class="slot">{e(t(m["slot"]))}</span>' if m["slot"] else "")
        + f'<span class="g">{fmt_num(m["grams"], 1, "g")}</span></div>'
        for i, m in enumerate(materials)
    ) or f'<span class="muted">{t("keine Angaben")}</span>'
    types = list(dict.fromkeys(_short(m["name"]) for m in materials if m["name"]))
    mat_sum = (", ".join(types[:2]) + (" …" if len(types) > 2 else "") + " · " if types else "") + fmt_num(grams, 1, "g")
    dur = fmt_dur(job["start_ts"], job["end_ts"])
    druck_sum = (fmt_date(job["start_ts"], "dm_hm") if job["start_ts"] else "–") + (f" · {dur}" if dur != "–" else "")
    price_sum = " · ".join(re.sub(r"<[^>]+>", "", x) for x in re.findall(r"<b>([^<]*)</b>", price_rows)[:3])
    facts = (f'<div><span>{t("Start")}</span><b>{fmt_ts(job["start_ts"])}</b></div>'
             f'<div><span>{t("Ende")}</span><b>{fmt_ts(job["end_ts"])}</b></div>'
             f'<div><span>{t("Dauer")}</span><b>{fmt_dur(job["start_ts"], job["end_ts"])}</b></div>'
             f'<div><span>{t("Energie")}</span><b>{fmt_num(job["kwh"], 3, "kWh")}</b></div>'
             f'<div><span>{t("Filament")}</span><b>{fmt_num(grams, 1, "g")}</b></div>')
    side = ""
    if job.get("project_url") or job.get("project_title"):
        side += f'<h3 style="margin-top:16px">{t("Projekt")}</h3>{_project(job)}'
    if job.get("local_path"):
        side += f'<h3 style="margin-top:16px">{t("Ordner")}</h3>{_local_path(job)}'
    if job.get("job_note"):
        side += f'<h3 style="margin-top:16px">{t("Notiz")}</h3><div style="white-space:pre-wrap">{e(job["job_note"])}</div>'
    if job.get("note"):
        side += f'<h3 style="margin-top:16px">{t("Hinweis")}</h3><div class="muted">{e(t_note(job["note"]))}</div>'
    preview = photos[:5]
    photo_box = (f'<div class="photos">{_photo_tiles(preview)}</div>' if preview
                 else f'<p class="muted" style="margin:0 0 10px">{t("Noch keine eigenen Fotos.")}</p>')
    more = (f'<p style="margin:10px 0 0"><a href="#ph" data-goto="ph">{t("Alle {n} Fotos ansehen →", n=len(photos))}</a></p>'
            if len(photos) > len(preview) else "")

    # Bearbeiten
    mat_fields = ""
    if materials:
        mat_fields = (f'<div class="full mfields"><span>{t("Filament-Notizen (z. B. genauer Farbname, erscheint auf Bon und PDF)")}</span>'
                      + "".join(f'<label class="mf"><span class="dot" style="background:{_color(m["color"])}"></span>'
                                f'<span class="mn">{e((t(m["slot"]) + " · ") if m["slot"] else "")}{e(_short(m["name"]))}</span>'
                                f'<input name="mnote__{i}" value="{e(mnotes.get(i, ""))}" maxlength="120" placeholder="{e(t("z. B. Ivory White"))}"></label>'
                                for i, m in enumerate(materials)) + '</div>')
    cur = str(job.get("customer_id") or "")
    cust_opts = opt("", t("– kein Kunde –"), cur) + "".join(opt(str(c["id"]), c["name"], cur) for c in customers)
    if parent:
        cust_field = (f'<label class="c3">{t("Kunde")}<span class="ro">{e(job.get("customer_name") or t("– kein Kunde –"))} · '
                      f'<a href="job?id={parent["id"]}#ed">{t("im Projekt ändern")}</a></span></label>')
        gift_field = ""
    elif split:
        cust_field, gift_field = _split.edit_field(), ""
    else:
        cust_field = f'<label class="c3">{t("Kunde")}<select name="customer_id">{cust_opts}</select></label>'
        gift_field = (f'<label class="c3">{t("Abrechnung")}<select name="gift">{opt("0", t("berechnen"), "1" if job.get("gift") else "0")}'
                      f'{opt("1", t("🎁 verschenkt (keine Zahlung)"), "1" if job.get("gift") else "0")}</select></label>')
    sv = "" if job.get("scrap") is None else str(job["scrap"])
    scrap_opts = (opt("", t("automatisch ({x})", x=t("ja, abgebrochen") if job["status"] == "abgebrochen" else t("nein")), sv)
                  + opt("1", t("ja, Fehldruck"), sv) + opt("0", t("nein, in Ordnung"), sv))

    # Start-Tab laut Einstellung; nach dem Speichern immer die Übersicht.
    mode = store.settings().get("detail_tab") or "ov"
    missing = not (job.get("customer_id") and job.get("quantity"))
    start = "ed" if not saved and (mode == "ed" or (mode == "ed_missing" and missing)) else "ov"

    def on(tab: str) -> str:
        return " on" if tab == start else ""

    ab_tab = ab_pane = ""
    if billable(job):
        ab_tab = f'<button type="button" data-tab="ab" class="{on("ab").strip()}">{t("Abrechnung")}</button>'
        ab_pane = (f'<section class="pane{on("ab")}" data-pane="ab">'
                   f'{_deal_box(job, store.handovers(job["id"]), store.payments(job["id"]))}</section>')
    banner = f'<div class="banner">{t("Gespeichert.")}</div>' if saved else ""
    unproject = (f'<form class="jdel" method="post" action="api/jobs/{job["id"]}/unproject"><button class="btn" '
                 f'title="{e(t("Der Druck wird wieder einzeln abgerechnet."))}">{t("📦 Aus Projekt lösen")}</button></form>') if parent else ""
    body = f"""<div class="jd">{banner}<div class="jtop"><a class="back" href="./">{t("← alle Drucke")}</a>
<div class="jact">{_share_button(job)}<form class="jdel" method="post" action="api/jobs/{job["id"]}/delete"><button class="btn sm danger" title="{e(t("Diesen Druck löschen"))}">{t("🗑 Löschen")}</button></form></div></div>
{pj_banner}<section class="hero"><div>{big}</div>
<div><h2 class="jtitle">{e(job["task_name"] or "–")}</h2><div class="badges">{badges}</div><div class="kps calc{" six" if xk else ""}">{kpis}</div></div>
{_sec("prices", t("Anteil am Projekt") if parent else t("Verkaufspreise"), "" if parent else price_sum, price_rows, "prices calc")}</section>
<div class="tabs" role="tablist"><button type="button" data-tab="ov" class="{on("ov").strip()}">{t("Übersicht")}</button>
{ab_tab}<button type="button" data-tab="ed" class="{on("ed").strip()}">{t("Bearbeiten")}</button>
<button type="button" data-tab="ph" class="{on("ph").strip()}">{t("Fotos ({n})", n=len(photos))}</button></div>
<section class="pane{on("ov")}" data-pane="ov">{_models.hint_card(store, job, f"job?id={job['id']}")}{_defect_box(store, job, photos)}{_deal_strip(job)}{pj_strip}{_split.split_box(store, job) if split else ""}{_extras.box(store, job)}<div class="ovg">
{_sec("druck", t("Druck"), druck_sum, f'<div class="facts">{facts}</div>{side}')}
{_sec("mat", t("Material"), mat_sum, mat_bar + mat_rows)}
{_sec("fotos", t("Eigene Fotos"), tn(len(photos), "{n} Foto", "{n} Fotos"), _upload_form(job["id"]) + photo_box + more)}
</div></section>
{ab_pane}<section class="pane{on("ed")}" data-pane="ed"><div class="box">
<form class="fields" method="post" action="api/jobs/{job["id"]}?redirect=1">
<div class="nrow full"><label>{t("Name")}<input name="task_name" value="{e(job["task_name"] or "")}"></label>
<div class="fl"><label for="f_qty">{t("Stückzahl auf der Platte")}</label><div class="step">
<button type="button" data-step="-1" aria-label="{e(t("eins weniger"))}">−</button>
<input id="f_qty" name="quantity" type="number" min="1" step="1" inputmode="numeric" value="{job.get("quantity") or ""}" placeholder="1">
<button type="button" data-step="1" aria-label="{e(t("eins mehr"))}">+</button></div></div></div>
{cust_field}
<label class="c3">{t("Fehldruck")}<select name="scrap">{scrap_opts}</select></label>
{gift_field}
<label>{t("Projekttitel")}<input name="project_title" value="{e(job.get("project_title") or "")}" placeholder="{e(t("wird beim Speichern aus dem Link geholt, wenn leer"))}"></label>
<label>{t("Projektlink (MakerWorld, Printables …)")}<input name="project_url" type="url" value="{e(job.get("project_url") or "")}" placeholder="https://makerworld.com/…"></label>
<label class="full">{t("Lokaler Ordner oder 3D-Datei (Windows-Pfad)")}<input name="local_path" value="{e(job.get("local_path") or "")}" placeholder="{e(t("z. B. D:\\3D-Druck\\Projekte\\Halter"))}" spellcheck="false"></label>
{mat_fields}<label class="full">{t("Notiz")}<textarea name="job_note" rows="3">{e(job.get("job_note") or "")}</textarea></label>
<div class="act"><button class="primary">{t("Speichern")}</button></div>
</form></div>
{unproject}<form class="jdel" method="post" action="api/jobs/{job["id"]}/delete"><button class="btn danger">{t("🗑 Druck löschen")}</button></form></section>
<section class="pane{on("ph")}" data-pane="ph"><div class="box">{_upload_form(job["id"])}
{f'<div class="photos lg">{_photo_tiles(photos)}</div>' if photos else f'<p class="muted">{t("Noch keine eigenen Fotos.")}</p>'}</div></section>
</div>"""
    sp = _split.dialogs(store, job) if not parent else ""
    return layout(job["task_name"] or t("Druck"), "jobs", f"<style>{MOS_CSS}{DETAIL_CSS}{PROJ_CSS}{ACT_CSS}{KPICK_CSS}{_split.SPLIT_CSS}{_extras.BOX_CSS}{_models.CSS}</style>"
                  + body + customer_picker(customers) + sp + _extras.picker(store, job),
                  ACT_JS + DETAIL_JS + SHARE_JS + KPICK_JS + KPICK_DETAIL_JS + _split.SPLIT_JS + _extras.box_js(job))


# ---- Projekt (mehrere Drucke als ein Auftrag) -------------------------------------

PROJ_CSS = """
.hero .mos{width:100%;aspect-ratio:1;border-radius:14px;gap:4px;padding:4px}.hero .mos b{font-size:15px;padding:2px 10px;right:10px;bottom:10px;border-radius:10px;line-height:20px}
.bdg.pjb{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 55%,var(--line));background:color-mix(in srgb,var(--accent) 12%,transparent)}
.pjbanner{display:flex;align-items:center;gap:14px;background:color-mix(in srgb,var(--accent) 10%,var(--card));border:1px solid color-mix(in srgb,var(--accent) 45%,var(--line));
 border-radius:16px;padding:12px 16px;margin-bottom:14px}
.pjbanner .mos{width:54px;height:54px;flex:none;border-radius:10px}.pjbanner .t2{flex:1;min-width:0}
.pjbanner b{font-size:16px}.pjbanner small{display:block;color:var(--muted);font-size:13.5px;margin-top:2px}.pjbanner .btn{white-space:nowrap}
.share{font-size:14px;line-height:1.5}.share .bigv{font-size:26px;font-weight:800;font-variant-numeric:tabular-nums}
.share .bar3{display:flex;gap:2px;height:12px;border-radius:6px;overflow:hidden;margin:8px 0 6px}
.share .bar3 i{display:block;height:100%;background:color-mix(in srgb,var(--fg) 16%,transparent);min-width:2px}.share .bar3 i.me{background:var(--accent)}
.strip .sa b.pjn{font-size:18px}
.fields .ro{display:flex;align-items:center;min-height:var(--ctl-h);font-weight:400;color:var(--fg);font-size:15px;gap:4px;flex-wrap:wrap}
.boxh{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}.boxh h3{margin:0!important}.boxh .sp{flex:1}.boxh .muted{font-size:13px}
.ptab{width:100%;border-collapse:collapse;font-size:14px}
.ptab td,.ptab th{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
.ptab th{font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:700}
.ptab .r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.ptab img,.ptab .ph{width:40px;height:40px;border-radius:8px;border:1px solid var(--line);object-fit:contain;display:block}
.ptab .nm a{display:block;font-size:15px;font-weight:700;color:var(--fg);text-decoration:none}.ptab .nm small{color:var(--muted);font-size:12px}
.ptab tr.fd .nm a{text-decoration:line-through;text-decoration-color:color-mix(in srgb,var(--bad) 60%,transparent);color:var(--muted)}
.ptab tr.fd td.r{color:var(--muted)}
.ptab tfoot td{font-weight:800;border-bottom:0}.ptab tfoot small{display:block;font-weight:500;color:var(--muted);font-size:12px}
.ptab .st{display:flex;flex-wrap:wrap;gap:4px}.ptab .st .bdg{font-size:12px;padding:2px 9px}
.ptab form{margin:0}.ptab .x{border:0;background:none;color:var(--muted);font-size:18px;cursor:pointer;min-height:0;padding:4px 8px}
.ptab .x:hover{color:var(--bad)}
.addf{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 0}.addf select{flex:1 1 260px;min-width:0;background:var(--bg)}
.tl{position:relative;margin-top:6px}
.tl .ln{display:grid;grid-template-columns:76px 1fr;gap:10px;align-items:center;height:34px}
.tl .ln>span{font-size:13px;font-weight:700;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tl .tr{position:relative;height:26px;background:color-mix(in srgb,var(--fg) 4%,transparent);border-radius:8px}
.tl .bk{position:absolute;top:2px;bottom:2px;border-radius:6px;background:color-mix(in srgb,var(--accent) 55%,var(--card));font-size:11.5px;font-weight:700;color:var(--fg);padding:0 7px;
 display:flex;align-items:center;white-space:nowrap;overflow:hidden;border:1px solid color-mix(in srgb,var(--accent) 80%,transparent);box-sizing:border-box;text-decoration:none;min-width:4px}
.tl .bk.bad{background:color-mix(in srgb,var(--bad) 35%,var(--card));border-color:var(--bad)}
.tl .bk.run{background:color-mix(in srgb,var(--run) 40%,var(--card));border-color:var(--run)}
.tl .ax{display:grid;grid-template-columns:76px 1fr;gap:10px;font-size:11.5px;color:var(--muted);margin-top:4px}
.tl .ax div{position:relative;height:16px}.tl .ax i{position:absolute;font-style:normal;transform:translateX(-50%);white-space:nowrap}
.tl .ax i:first-child{transform:none}.tl .ax i:last-child{transform:translateX(-100%)}
.calcl{font-size:14px;border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:12px 0 4px}
.calcl div{display:flex;justify-content:space-between;gap:10px;padding:3px 0}.calcl .m{color:var(--muted)}.calcl .bad{color:var(--bad)}
.calcl .t{border-top:1px solid var(--line);margin-top:4px;padding-top:7px;font-weight:700}
.dissolve{margin-top:16px}.dissolve p{margin:6px 0 0;font-size:13px;color:var(--muted)}
.kphotos h3{margin-top:18px!important}
@media (max-width:760px){.ptab .hm{display:none}.ptab td,.ptab th{padding:7px 4px}.ptab .nm a{font-size:14px}
 .tl .ln,.tl .ax{grid-template-columns:56px 1fr}.tl .ax i.mid{display:none}.pjbanner{flex-wrap:wrap}.pjbanner .btn{width:100%;text-align:center;justify-content:center}}
"""


def _timeline(kids: list[dict[str, Any]], names: dict[str, str]) -> str:
    """Zeitleiste: je Drucker eine Spur, jeder Druck ein Balken (zeigt, was parallel lief)."""
    items = [k for k in kids if k.get("start_ts")]
    if not items:
        return ""
    now = time.time()
    t0 = min(k["start_ts"] for k in items)
    t1 = max((k["end_ts"] or now) for k in items)
    span = max(t1 - t0, 60.0)
    lanes: dict[str, list[dict[str, Any]]] = {}
    for k in items:
        lanes.setdefault(k["printer"], []).append(k)
    rows = []
    for key, ks in lanes.items():
        bars = ""
        for k in ks:
            a = (k["start_ts"] - t0) / span * 100
            w = max(((k["end_ts"] or now) - k["start_ts"]) / span * 100, 0.6)
            cls = "bad" if k.get("is_scrap") else "run" if k["state"] != "done" else ""
            label = ("⚠ " if k.get("is_scrap") else "") + (k.get("task_name") or "–")
            tip = f'{k.get("task_name") or "–"} · {fmt_date(k["start_ts"], "dm_hm")} · {fmt_dur(k["start_ts"], k["end_ts"])}'
            bars += (f'<a class="bk {cls}" href="job?id={k["id"]}" title="{e(tip)}" '
                     f'style="left:{a:.3f}%;width:{min(w, 100 - a):.3f}%">{e(label)}</a>')
        rows.append(f'<div class="ln"><span>{e(names.get(key, ks[0].get("printer_name") or key))}</span><div class="tr">{bars}</div></div>')
    multi_day = datetime.fromtimestamp(t0).date() != datetime.fromtimestamp(t1).date()
    ticks = []
    for i in range(5):
        ts = t0 + span * i / 4
        txt = fmt_date(ts, "dm_hm") if multi_day else datetime.fromtimestamp(ts).strftime("%H:%M")
        ticks.append(f'<i class="{"mid" if 0 < i < 4 else ""}" style="left:{i * 25}%">{txt}</i>')
    most = 0
    for k in items:
        s_, e_ = k["start_ts"], k["end_ts"] or now
        most = max(most, sum(1 for o in items if o["start_ts"] < e_ and (o["end_ts"] or now) > s_))
    sub = t("bis zu {n} Drucker gleichzeitig", n=most) if most > 1 else t("nacheinander gedruckt")
    return (f'<div class="box"><div class="boxh"><h3>{t("Zeitleiste")}</h3><span class="muted">{sub}</span></div>'
            f'<div class="tl">{"".join(rows)}<div class="ax"><span></span><div>{"".join(ticks)}</div></div></div></div>')


def _parts_box(store: Store, p: dict[str, Any], kids: list[dict[str, Any]], names: dict[str, str]) -> str:
    """Tabelle der Drucke im Projekt mit Summe; Drucke lösen oder hinzufügen."""
    rows = ""
    for k in kids:
        grams = k["filament_g"] if k.get("filament_g") is not None else k.get("weight_g")
        st = f'<span class="bdg st-{_STATUS_TONE.get(_status_of(k).split()[0], "none")}">{e(t(_status_of(k)))}</span>'
        st += defect_badges(k)
        if k.get("reprint_of"):
            st += f'<span class="bdg st-run">↻ {t("Nachdruck")}</span>'
        rows += (f'<tr class="{"fd" if no_charge(k) else ""}"><td>{_thumb(k)}</td>'
                 f'<td class="nm"><a href="job?id={k["id"]}">{e(k.get("task_name") or "–")}</a><small>{e(code_label(k.get("code")))} · '
                 f'{e(names.get(k["printer"], k.get("printer_name") or k["printer"]))}</small></td>'
                 f'<td class="hm">{fmt_date(k["start_ts"], "dm_hm") if k.get("start_ts") else "–"}</td>'
                 f'<td class="r hm">{fmt_dur(k["start_ts"], k["end_ts"])}</td><td class="r hm">{k.get("quantity") or 1}</td>'
                 f'<td class="r hm">{fmt_num(grams, 1, "g")}</td><td class="r calc"><b>{fmt_num(k.get("cost_total"), 2, "€")}</b></td>'
                 f'<td><div class="st">{st}</div></td>'
                 f'<td class="hm"><form method="post" action="api/jobs/{k["id"]}/unproject?back=project">'
                 f'<button class="x" title="{e(t("Aus Projekt lösen"))}">×</button></form></td></tr>')
    base = price_base(p)
    foot = (f'<tr><td></td><td>{t("Summe")}</td><td class="hm"></td><td class="r hm">{fmt_hours(print_hours(kids))}</td><td class="hm"></td>'
            f'<td class="r hm">{fmt_num(p.get("filament_g"), 1, "g")}</td><td class="r calc">{fmt_num(p.get("cost_total"), 2, "€")}'
            + (f'<small>{t("berechnet {v}", v=fmt_num(base, 2, "€"))}</small>' if base is not None and p.get("cost_total") is not None
               and abs(base - p["cost_total"]) >= 0.005 else "") + '</td><td colspan="2"></td></tr>')
    cands = store.project_candidates(p)
    add = ""
    if cands:
        options = "".join(opt(str(c["id"]), " · ".join(x for x in (code_label(c.get("code")), c.get("task_name") or "–",
                                                                     names.get(c["printer"], c.get("printer_name") or c["printer"]),
                                                                     fmt_date(c["start_ts"], "dm_hm") if c.get("start_ts") else "") if x), "")
                          for c in cands)
        add = (f'<form class="addf" method="post" action="api/projects/{p["id"]}/add"><select name="ids" aria-label="{e(t("Druck hinzufügen"))}">'
               f'{options}</select><button class="btn">{t("＋ Hinzufügen")}</button></form>')
    head = (f'<div class="boxh"><h3>{t("Drucke im Projekt")}</h3><span class="muted">'
            f'{tn(len(kids), "{n} Druck", "{n} Drucke")} · {printers_of(kids, names)} · {t("{h} Druckzeit", h=fmt_hours(print_hours(kids)))}</span></div>')
    return (f'<div class="box">{head}<div style="overflow-x:auto"><table class="ptab"><thead><tr><th style="width:44px"></th><th>{t("Druck")}</th>'
            f'<th class="hm">{t("Start")}</th><th class="r hm">{t("Dauer")}</th><th class="r hm">{t("Stk.")}</th><th class="r hm">{t("Filament")}</th>'
            f'<th class="r">{t("Kosten")}</th><th>{t("Status")}</th><th class="hm"></th></tr></thead><tbody>{rows}</tbody>'
            f'<tfoot>{foot}</tfoot></table></div>{add}</div>')


def _calc_lines(p: dict[str, Any], kids: list[dict[str, Any]]) -> str:
    """Wie der Preisvorschlag des Projekts entsteht (Kosten, Fehldrucke, Marge)."""
    total, base = p.get("cost_total"), price_base(p)
    if total is None or base is None:
        return ""
    lines = f'<div class="m"><span>{t("Kosten aller Drucke")}</span><span>{fmt_num(total, 2, "€")}</span></div>'
    for k in kids:
        if not k.get("is_scrap") or k.get("cost_total") is None:
            continue
        sh = share_of(k, p) or 0.0
        diff = k["cost_total"] - sh * base
        if abs(diff) >= 0.005:
            why = t("nicht berechnet") if no_charge(k) else charge_text(k)
            lines += (f'<div class="bad"><span>− {t("Fehldruck {name}", name=e(k.get("task_name") or "–"))} ({e(why)})</span>'
                      f'<span>−{fmt_num(diff, 2, "€")}</span></div>')
    m = p.get("customer_margin") or 0
    lines += f'<div><span>{t("Basis")}</span><span>{fmt_num(base, 2, "€")}</span></div>'
    if m:
        lines += f'<div class="m"><span>{t("+ {p} % Kundenmarge", p=fmt_input(m))}</span><span>{fmt_num(base * m / 100, 2, "€")}</span></div>'
    lines += f'<div class="t"><span>{t("Vorschlag")}</span><span>{fmt_num(base * (1 + m / 100), 2, "€")}</span></div>'
    return f'<div class="calcl calc">{lines}</div>'


def page_project(store: Store, p: dict[str, Any], saved: str | None) -> str:
    """Detailseite eines Projekts: Kopf wie beim Druck, dazu Drucke, Zeitleiste, Abrechnung fürs Ganze."""
    names = printer_names(store)
    customers = store.customers()
    kids = store.children([p["id"]]).get(p["id"], [])
    photos = store.photos(p["id"])
    kid_photos = [ph for k in kids for ph in store.photos(k["id"])]
    status = _status_of(p)
    qty = p.get("quantity") or 1
    total = p.get("cost_total")
    n_fd = sum(1 for k in kids if k.get("is_scrap"))
    hours = print_hours(kids)
    split = bool(p.get("split"))
    if split:
        _split.attach(store, [p], {p["id"]: kids})

    tone = _STATUS_TONE.get(status.split()[0], "")
    badges = f'<span class="bdg st st-{tone}">{e(t(status))}</span>'
    badges += cust_chip(p, "bdg") + _split.split_button(p)
    badges += f'<span class="bdg pjb">📦 {t("Projekt")} · {tn(len(kids), "{n} Druck", "{n} Drucke")}</span>'
    if p.get("code"):
        badges += (f'<button type="button" class="bdg pidb" data-copy="{e(code_label(p["code"]))}" '
                   f'title="{e(t("Projekt-ID: tippe sie in die Suche, um das Projekt wiederzufinden. Klick kopiert."))}">'
                   f'# {e(code_label(p["code"]))}</button>')
    if p.get("quantity"):
        badges += f'<span class="bdg">{_pcs(qty)}</span>'
    sub = (t("inkl. {n} Fehldruck", n=n_fd) if n_fd == 1 else t("inkl. {n} Fehldrucke", n=n_fd)) if n_fd else \
        (t("{v} pro Stück", v=fmt_num(total / qty, 2, "€")) if qty > 1 and total is not None else tn(len(kids), "{n} Druck", "{n} Drucke"))
    kpis = (f'<div class="kp main"><span>{t("Gesamtkosten")}</span><b>{fmt_num(None if total is None else total + _extras.work_total(p), 2, "€")}</b><small>{e(sub)}</small></div>'
            f'<div class="kp"><span>{t("⚡ Strom")}</span><b>{fmt_num(p.get("cost"), 2, "€")}</b><small>{fmt_num(p.get("kwh"), 3, "kWh")}</small></div>'
            f'<div class="kp"><span>{t("🧵 Filament")}</span><b>{fmt_num(p.get("cost_filament"), 2, "€")}</b><small>{fmt_num(p.get("filament_g"), 1, "g")}</small></div>'
            f'<div class="kp"><span>{t("🖨 Drucker")}</span><b>{fmt_num(p.get("cost_printer"), 2, "€")}</b>'
            f'<small>{tn(len({k["printer"] for k in kids}), "{n} Drucker", "{n} Drucker")} · {fmt_hours(hours)}</small></div>') + extra_kpis(p)
    mg = list(margins(store))
    if customer_margin(p):
        mg.insert(0, customer_margin(p))
    price_rows = ""
    if p.get("final_price") is not None:
        each = f' <span class="tag">{t("{v}/Stk.", v=fmt_num(p["final_price"] / qty, 2, "€"))}</span>' if qty > 1 else ""
        price_rows += f'<div class="pr"><div><b>{t("Endpreis")}</b></div><div><b>{fmt_num(p["final_price"], 2, "€")}</b>{each}</div></div>'
    for m in mg:
        n, pct = m
        price = mprice(p, m)
        each = f' <span class="tag">{t("{v}/Stk.", v=fmt_num(price / qty, 2, "€"))}</span>' if price is not None and qty > 1 else ""
        price_rows += (f'<div class="pr"><div>{e(n)} <span class="tag">+{e(fmt_input(pct))} %{work_tag(m, _extras.work_total(p))}</span></div>'
                       f'<div><b>{fmt_num(price, 2, "€")}</b>{each}</div></div>')
    price_rows += profit_rows(p, qty, endpreis=False)
    base = price_base(p)
    if base is not None and total is not None and abs(base - total) >= 0.005:
        price_rows += f'<p class="muted" style="margin:8px 0 0;font-size:13px">{t("Basis {v} (ohne unberechnete Fehldrucke)", v=fmt_num(base, 2, "€"))}</p>'
    if not price_rows:
        price_rows = f'<p class="muted" style="margin:6px 0 0">{t("Keine Margen eingestellt (Einstellungen).")}</p>'
    price_sum = " · ".join(re.sub(r"<[^>]+>", "", x) for x in re.findall(r"<b>([^<]*€[^<]*)</b>", price_rows)[:2])

    start = "ov"

    def on(tab: str) -> str:
        return " on" if tab == start else ""

    ab_tab = ab_pane = ""
    if billable(p):
        ab_tab = f'<button type="button" data-tab="ab" class="">{t("Abrechnung")}</button>'
        ab_pane = (f'<section class="pane" data-pane="ab">{_deal_box(p, store.handovers(p["id"]), store.payments(p["id"]))}'
                   f'<div class="box"><h3>{t("So entsteht der Preisvorschlag")}</h3>{_calc_lines(p, kids)}</div></section>')
    note = (f'<div class="box"><h3>{t("Notiz")}</h3><div style="white-space:pre-wrap">{e(p["job_note"])}</div></div>'
            if p.get("job_note") else "")
    cur = str(p.get("customer_id") or "")
    cust_opts = opt("", t("– kein Kunde –"), cur) + "".join(opt(str(c["id"]), c["name"], cur) for c in customers)
    gv = "1" if p.get("gift") else "0"
    banner = f'<div class="banner">{t("Gespeichert.")}</div>' if saved else ""
    all_photos = (f'<div class="photos lg">{_photo_tiles(photos)}</div>' if photos else f'<p class="muted">{t("Noch keine eigenen Fotos.")}</p>')
    kid_tiles = "".join(f'<figure><a href="job?id={ph["job_id"]}"><img src="images/{e(ph["file"])}" loading="lazy" alt=""></a></figure>'
                        for ph in kid_photos)
    kid_box = f'<div class="kphotos"><h3>{t("Fotos der einzelnen Drucke")}</h3><div class="photos lg">{kid_tiles}</div></div>' if kid_tiles else ""
    body = f"""<div class="jd">{banner}<div class="jtop"><a class="back" href="./">{t("← alle Drucke")}</a>
<div class="jact">{_share_button(p)}<form class="jdel" method="post" action="api/projects/{p["id"]}/dissolve"><button class="btn sm danger" title="{e(t("Projekt auflösen: die Drucke bleiben erhalten und werden wieder einzeln geführt"))}">{t("Auflösen")}</button></form></div></div>
<section class="hero"><div>{mosaic(kids)}</div>
<div><h2 class="jtitle">{e(p["task_name"] or "–")}</h2><div class="badges">{badges}</div><div class="kps calc{" six" if extra_kpis(p) else ""}">{kpis}</div></div>
{_sec("prices", t("Verkaufspreise"), price_sum, price_rows, "prices calc")}</section>
<div class="tabs" role="tablist"><button type="button" data-tab="ov" class="on">{t("Übersicht")}</button>
{ab_tab}<button type="button" data-tab="ed" class="">{t("Bearbeiten")}</button>
<button type="button" data-tab="ph" class="">{t("Fotos ({n})", n=len(photos) + len(kid_photos))}</button></div>
<section class="pane on" data-pane="ov">{_deal_strip(p)}{_split.split_box(store, p, kids) if split else ""}{_extras.box(store, p)}{_parts_box(store, p, kids, names)}{_timeline(kids, names)}{note}</section>
{ab_pane}<section class="pane" data-pane="ed"><div class="box">
<form class="fields" method="post" action="api/jobs/{p["id"]}?redirect=1">
<div class="nrow full"><label>{t("Name")}<input name="task_name" value="{e(p["task_name"] or "")}"></label>
<div class="fl"><label for="f_qty">{t("Stückzahl (fertige Objekte)")}</label><div class="step">
<button type="button" data-step="-1" aria-label="{e(t("eins weniger"))}">−</button>
<input id="f_qty" name="quantity" type="number" min="1" step="1" inputmode="numeric" value="{p.get("quantity") or ""}" placeholder="1">
<button type="button" data-step="1" aria-label="{e(t("eins mehr"))}">+</button></div></div></div>
{_split.edit_field() if split else f'<label class="c3">{t("Kunde")}<select name="customer_id">{cust_opts}</select></label>'}
{"" if split else f'<label class="c3">{t("Abrechnung")}<select name="gift">{opt("0", t("berechnen"), gv)}{opt("1", t("🎁 verschenkt (keine Zahlung)"), gv)}</select></label>'}
<label class="c3"></label>
<label class="full">{t("Notiz")}<textarea name="job_note" rows="3">{e(p.get("job_note") or "")}</textarea></label>
<div class="act"><button class="primary">{t("Speichern")}</button></div>
</form></div>
<form class="jdel dissolve" method="post" action="api/projects/{p["id"]}/dissolve"><button class="btn danger">{t("📦 Projekt auflösen")}</button>
<p>{t("Die Drucke bleiben erhalten und werden wieder einzeln geführt. Zahlungen gehen an den ersten Druck.")}</p></form></section>
<section class="pane" data-pane="ph"><div class="box">{_upload_form(p["id"])}{all_photos}{kid_box}</div></section>
</div>"""
    return layout(p["task_name"] or t("Projekt"), "jobs", f"<style>{MOS_CSS}{DETAIL_CSS}{PROJ_CSS}{ACT_CSS}{KPICK_CSS}{_split.SPLIT_CSS}{_extras.BOX_CSS}</style>" + body
                  + customer_picker(customers) + _split.dialogs(store, p, kids) + _extras.picker(store, p),
                  ACT_JS + DETAIL_JS + SHARE_JS + KPICK_JS + KPICK_DETAIL_JS + _split.SPLIT_JS + _extras.box_js(p))


# ---- Kunden -------------------------------------------------------------------

GIFT_PERIODS = {"": N_("Gesamter Zeitraum"), "monat": N_("Dieser Monat"), "vormonat": N_("Letzter Monat"),
                "jahr": N_("Dieses Jahr"), "vorjahr": N_("Letztes Jahr"), "frei": N_("Frei wählen …")}


def gift_period(period: str | None, von: str | None, bis: str | None) -> tuple[float | None, float | None, str | None]:
    """Zeitraum → (ab, bis exklusiv, Monat für den Listenfilter oder None)."""
    now = datetime.now()
    m0 = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "monat":
        nxt = (m0.replace(year=m0.year + 1, month=1) if m0.month == 12 else m0.replace(month=m0.month + 1))
        return m0.timestamp(), nxt.timestamp(), m0.strftime("%Y-%m")
    if period == "vormonat":
        prev = m0.replace(year=m0.year - 1, month=12) if m0.month == 1 else m0.replace(month=m0.month - 1)
        return prev.timestamp(), m0.timestamp(), prev.strftime("%Y-%m")
    if period in ("jahr", "vorjahr"):
        y = now.year - (period == "vorjahr")
        return datetime(y, 1, 1).timestamp(), datetime(y + 1, 1, 1).timestamp(), None
    if period == "frei":
        def day(v: str | None, plus: int = 0) -> float | None:
            try:
                return datetime.strptime(v or "", "%Y-%m-%d").timestamp() + plus * 86400
            except ValueError:
                return None
        return day(von), day(bis, 1), None
    return None, None, None


def page_customers(store: Store, saved: str | None, show: str | None = None,
                   period: str | None = None, von: str | None = None, bis: str | None = None) -> str:
    all_customers = store.customers()
    period = period if period in GIFT_PERIODS else ""
    since, until, month = gift_period(period, von, bis)
    gifts = store.gifts_by_customer(since, until)
    gq = f"&todo=verschenkt" + (f"&month={month}" if month else "")

    def gift_tag(c: dict[str, Any]) -> str:
        g = gifts.get(c["id"])
        if not g:
            return "–"
        return (f'<a class="otag gift" href="./?customer={c["id"]}{gq}" title="{e(t("Kosten {v}", v=fmt_num(g["cost"], 2, "€")))}">'
                f'🎁 {g["n"]} · {fmt_num(g["value"], 2, "€")}</a>')

    customers = [c for c in all_customers if not show or (show == "offen" and (c["open_n"] or c["undelivered_n"]))
                 or (show == "verschenkt" and c["id"] in gifts)]
    def row(c: dict[str, Any]) -> str:
        sub = " · ".join(e(x) for x in (c["contact"], c["note"]) if x)
        mg = c.get("margin_pct")
        mtag = (f' <span class="otag none calc" title="{e(t("Marge %"))}">+{fmt_input(mg)} %</span>' if mg else "")
        return (f'<tr class="crow"><td class="cn"><a class="cname" href="customer?id={c["id"]}" title="{e(t("Kundenseite mit Statistik"))}"><b>{e(c["name"])}</b></a>{mtag}{f"<small>{sub}</small>" if sub else ""}</td>'
                f'<td class="n" data-l="{e(t("Drucke"))}">{c["n"]}</td>'
                f'<td class="n wide" data-l="{e(t("Offen"))}">{_open_tags(c)}</td>'
                f'<td class="n" data-l="{e(t("Bezahlt"))}">{fmt_num(c["paid_amount"], 2, "€") if c["paid_amount"] else "–"}</td>'
                f'<td class="n" data-l="{e(t("Verschenkt"))}">{gift_tag(c)}</td>'
                f'<td class="n calc" data-l="{e(t("Kosten"))}">{fmt_num(c["cost_total"], 2, "€")}</td>'
                f'<td class="n" data-l="{e(t("Letzter Druck"))}" title="{fmt_ts(c["last_ts"]) if c["last_ts"] else ""}">{fmt_date(c["last_ts"], "date_s") if c["last_ts"] else "–"}</td>'
                f'<td class="cact"><a class="btn sm" href="./?customer={c["id"]}">{t("Drucke")}</a>'
                f'<button type="button" class="btn sm" data-edit="{c["id"]}" aria-expanded="false">{t("Bearbeiten")}</button></td></tr>'
                f'<tr class="ced" id="ced{c["id"]}" hidden><td colspan="8">{_customer_form(c)}'
                f'<form method="post" action="customers/{c["id"]}/delete" class="d" id="cdel{c["id"]}"></form></td></tr>')

    rows = "".join(row(c) for c in customers) or ('<tr><td colspan="8" class="muted">' + (t("Im Zeitraum nichts verschenkt.") if show == "verschenkt" else
          t("Keine offenen Beträge oder Übergaben.") if show else t("Noch keine Kunden angelegt.")) + '</td></tr>')
    open_total = sum(c["open_amount"] or 0 for c in all_customers)
    undelivered = sum(c["undelivered_n"] or 0 for c in all_customers)
    banner = f'<div class="banner">{t("Gespeichert.")}</div>' if saved else ""
    pq = "".join(f"&{k}={e(v)}" for k, v in (("period", period), ("von", von), ("bis", bis)) if v and (k == "period" or period == "frei"))
    seg = (f'<span class="seg"><a href="customers?{pq[1:]}" class="{"" if show else "on"}">{t("Alle")}</a>'
           f'<a href="customers?show=offen{pq}" class="{"on" if show == "offen" else ""}">{t("Nur mit Offenem")}</a>'
           f'<a href="customers?show=verschenkt{pq}" class="{"on" if show == "verschenkt" else ""}">{t("Mit Verschenktem")}</a></span>')
    g_n = sum(g["n"] for g in gifts.values())
    g_val = sum(g["value"] for g in gifts.values())
    g_cost = sum(g["cost"] for g in gifts.values())
    per_opts = "".join(opt(k, t(v), period) for k, v in GIFT_PERIODS.items())
    free = (f'<input type="date" name="von" value="{e(von or "")}"><input type="date" name="bis" value="{e(bis or "")}">'
            if period == "frei" else "")
    gift_form = (f'<form class="gper" method="get" action="customers">{f"<input type=hidden name=show value={e(show)}>" if show else ""}'
                 f'<span class="otag gift">{tn(g_n, "🎁 verschenkt <b>{n}</b> Druck", "🎁 verschenkt <b>{n}</b> Drucke")} · {t("Wert")} <b>{fmt_num(g_val, 2, "€")}</b>'
                 f'<span class="calc">· {t("Kosten {v}", v=fmt_num(g_cost, 2, "€"))}</span></span>'
                 f'<select name="period" onchange="this.form.submit()">{per_opts}</select>{free}'
                 f'{"<button>" + t("Anzeigen") + "</button>" if period == "frei" else ""}</form>')
    summary = (f'<div class="osum"><span class="otag warn">{t("💶 offen")} <b>{fmt_num(open_total, 2, "€")}</b></span>'
               f'<span class="otag run">{tn(undelivered, "📦 nicht übergeben <b>{n}</b> Druck", "📦 nicht übergeben <b>{n}</b> Drucke")}</span>{seg}</div>'
               f'<div class="osum">{gift_form}</div>')
    body = f"""<style>.osum{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:0 0 12px}}
.otag{{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:999px;font-weight:600;font-size:13px;white-space:nowrap;text-decoration:none;
 border:1px solid color-mix(in srgb,currentColor 35%,transparent);background:color-mix(in srgb,currentColor 10%,transparent)}}
.otag.none{{color:var(--muted)}}.otag.warn{{color:var(--warn)}}.otag.run{{color:var(--run)}}.otag.ok{{color:var(--ok)}}.otag.gift{{color:#b07cf0}}
.gper{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0}}.gper select,.gper input{{width:auto}}td .otag{{margin:2px 0 2px 4px}}{CUSTOMERS_CSS}</style>
{banner}<div class="chead"><h2>{t("Kunden")}</h2>
<button type="button" class="btn primary" id="newc" aria-expanded="{"true" if not all_customers else "false"}">+ {t("Neuer Kunde")}</button></div>
<div class="cnew box"{"" if not all_customers else " hidden"}><h3>{t("Neuer Kunde")}</h3>{_customer_form(None)}</div>
{summary}
<div class="wrap"><table class="stack ctab"><thead><tr><th>{t("Name · Kontakt · Notiz")}</th><th class="n">{t("Drucke")}</th><th class="n">{t("Offen")}</th><th class="n">{t("Bezahlt")}</th><th class="n">{t("Verschenkt")}</th>
<th class="n calc">{t("Kosten")}</th><th class="n">{t("Letzter Druck")}</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="help muted"><p>{t("Offen = Endpreis (oder Kosten + Kundenmarge) minus eingetragene Zahlungen, ohne Verschenktes und ohne Fehldrucke, die nicht berechnet werden. Ein Klick auf ein Schild zeigt die passenden Drucke.")}</p>
<p>{t("Verschenkt: Anzahl und Wert (Endpreis bzw. Kosten + Kundenmarge) der verschenkten Drucke im gewählten Zeitraum, nach Druckstart. Die Kosten stehen im Tooltip.")}</p>
<p>{t("Die Marge eines Kunden gilt für alle seine Drucke und wird zusätzlich zu den allgemeinen Margen angezeigt.")}</p>
<p>{t("Keine Abrechnung: für Eigenbedarf, z. B. Drucke für eigene Uploads. Kosten und Statistik zählen weiter, aber es gibt keine Zahlungen, keine Übergaben und nichts Offenes. Anders als „verschenkt“ (pro Druck) gilt das für alle Drucke des Kunden.")}</p>
<p>{t("Drucke weist du auf der Detailseite eines Drucks (Tab „Bearbeiten“) einem Kunden zu. Beim Löschen eines Kunden bleiben seine Drucke erhalten, nur ohne Kunde.")}</p></div>"""
    return layout(t("Kunden"), "customers", body, CUSTOMERS_JS)


CUSTOMERS_CSS = """
.chead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 12px}.chead h2{margin:0}
.cnew{margin:0 0 14px;max-width:var(--w-form)}.cnew h3{margin:0 0 10px;font-size:14px}
.box{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px}
.cform{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1.2fr) minmax(0,1.6fr) 110px;gap:10px 12px;align-items:end;margin:0}
.cform .lf.m input{text-align:right}
.cform .cbar{grid-column:1/-1;display:flex;flex-wrap:wrap;align-items:center;gap:10px}.cform .cbar .sp{flex:1}
.nob{display:inline-flex;align-items:center;gap:8px;white-space:nowrap;font-size:14px;font-weight:600;cursor:pointer}
.ctab td{vertical-align:middle}.ctab .cname{color:var(--fg);text-decoration:none}.ctab .cname:hover b{color:var(--accent);text-decoration:underline}.ctab td.wide{white-space:normal}@media (min-width:701px){.ctab td.wide{min-width:150px}}.ctab .cn b{font-size:15px}.ctab .cn small{display:block;color:var(--muted);font-size:13px;
 max-width:360px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ctab .cact{white-space:nowrap;text-align:right}.ctab .cact .btn+.btn{margin-left:6px}
.ctab tr.open td{border-bottom:0;background:color-mix(in srgb,var(--accent) 6%,transparent)}
.ctab .ced td{background:color-mix(in srgb,var(--accent) 6%,transparent);padding:4px 10px 14px}
.ctab .ced form.d{display:none}.ctab .ced .cform{max-width:var(--w-form)}
.btn.danger-o{color:var(--bad);background:transparent}
.help{margin-top:14px}
/* Tablet hochkant: Karten statt Tabelle, Kennzahlen in einer Zeile */
@media (min-width:701px) and (max-width:1100px){
 .ctab thead{display:none}.ctab tr.crow{display:grid;grid-template-columns:repeat(5,minmax(0,1fr)) auto;grid-auto-flow:row dense;gap:8px 14px;
  padding:12px 14px;border-bottom:1px solid var(--line)}
 .ctab td{border:0;padding:0;text-align:left;white-space:normal}.ctab td.cn{grid-column:1/-2}.ctab td.cact{grid-column:-2;grid-row:1}
 .ctab td.wide{grid-column:1/-1;min-width:0}.ctab td.wide .otag{margin:2px 6px 2px 0}
 .ctab td[data-l]:before{content:attr(data-l);display:block;font-size:11px;color:var(--muted)}
 .ctab tr.ced{display:block}.ctab tr.ced[hidden]{display:none}.ctab .ced td{display:block;padding:4px 14px 14px}}
@media (max-width:760px){.cform{grid-template-columns:1fr 1fr}.cform .lf:not(.m){grid-column:1/-1}}
@media (max-width:700px){
 .ctab tr.crow{grid-auto-flow:row dense}.ctab td.wide{grid-column:1/-1}.ctab td.wide:empty{display:none}
 .ctab .cact{grid-column:1/-1;display:flex;gap:6px;text-align:left}.ctab .cact .btn{flex:1}.ctab .cact .btn+.btn{margin-left:0}
 .ctab .cn small{max-width:none;white-space:normal}.ctab tr.ced{display:block}.ctab tr.ced[hidden]{display:none}
 .ctab .ced td{display:block;padding:12px}.ctab tr.open,.ctab tr.ced{background:color-mix(in srgb,var(--accent) 6%,transparent)}.ctab tr.open td,.ctab .ced td{background:none}
 .cform .cbar .nob{flex:1 1 100%}.cform .cbar .sp{display:none}.cform .cbar .btn{flex:1}.ctab td .otag{margin:2px 4px 2px 0}.chead .btn{padding:0 12px}}
"""

CUSTOMERS_JS = """
document.querySelectorAll('form.d').forEach(f=>f.onsubmit=()=>confirm(_t("Kunde löschen? Die Drucke bleiben erhalten.")));
const nb=document.getElementById('newc'),nc=document.querySelector('.cnew');
nb.onclick=()=>{nc.hidden=!nc.hidden;nb.setAttribute('aria-expanded',!nc.hidden);if(!nc.hidden)nc.querySelector('input[name=name]').focus()};
document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>{const r=document.getElementById('ced'+b.dataset.edit),row=b.closest('tr');
  r.hidden=!r.hidden;row.classList.toggle('open',!r.hidden);b.setAttribute('aria-expanded',!r.hidden);
  if(!r.hidden)r.querySelector('input[name=name]').focus()});
{const ed=new URLSearchParams(location.search).get('edit'),b=ed&&document.querySelector('[data-edit="'+ed+'"]');if(b){b.click();b.closest('tr').scrollIntoView({block:'center'})}}
document.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=()=>{const r=b.closest('tr.ced');r.hidden=true;
  const row=r.previousElementSibling;row.classList.remove('open');row.querySelector('[data-edit]').setAttribute('aria-expanded','false')});
"""


def _customer_form(c: dict[str, Any] | None) -> str:
    """Formular für neuen oder bestehenden Kunden (gleiche Felder, beschriftet)."""
    c = c or {}
    hid = f'<input type="hidden" name="id" value="{c["id"]}">' if c.get("id") else ""
    opt_ph = f' placeholder="{e(t("optional"))}"'
    wm = "" if c.get("work_margin") is None else str(c["work_margin"])
    btns = (f'<button class="btn danger-o" form="cdel{c["id"]}">{t("Kunde löschen")}</button>'
            f'<button type="button" class="btn" data-cancel>{t("Abbrechen")}</button><button class="btn primary">{t("Speichern")}</button>'
            if c.get("id") else f'<button class="btn primary">{t("Anlegen")}</button>')
    return (f'<form method="post" action="customers" class="cform">{hid}'
            f'<label class="lf">{t("Name")}<input name="name" value="{e(c.get("name") or "")}" required></label>'
            f'<label class="lf">{t("Kontakt")}<input name="contact" value="{e(c.get("contact") or "")}"{opt_ph}></label>'
            f'<label class="lf">{t("Notiz")}<input name="note" value="{e(c.get("note") or "")}"{opt_ph}></label>'
            f'<label class="lf m calc" title="{e(t("Eigene Marge in %, hat Vorrang"))}">{t("Marge %")}'
            f'<input name="margin_pct" inputmode="decimal" value="{fmt_input(c.get("margin_pct"))}"{opt_ph}></label>'
            f'<label class="lf m calc" title="{e(t("Marge auch auf Arbeitszeit und Design"))}">{t("Marge auf Arbeit")}'
            f'<select name="work_margin">{opt("", t("Standard (nein)"), wm)}{opt("1", t("ja"), wm)}{opt("0", t("nein"), wm)}</select></label>'
            f'<div class="cbar">{_nobill_box(c.get("no_billing"))}<span class="sp"></span>{btns}</div></form>')


def _nobill_box(on: Any) -> str:
    return (f'<label class="nob" title="{e(t("Kosten zählen weiter, aber nie offen, unbezahlt oder nicht übergeben"))}">'
            f'<input type="checkbox" name="no_billing" value="1"{" checked" if on else ""}>{t("Keine Abrechnung")}</label>')


def _open_tags(c: dict[str, Any]) -> str:
    if c.get("no_billing"):
        return f'<span class="otag none">{t("Keine Abrechnung")}</span>'
    out = ""
    if c["open_n"]:
        out += (f'<a class="otag warn" href="./?customer={c["id"]}&todo=unbezahlt">💶 {fmt_num(c["open_amount"], 2, "€")}'
                f' · {tn(c["open_n"], "{n} Druck", "{n} Drucke")}</a>')
    if c["undelivered_n"]:
        out += (f'<a class="otag run" href="./?customer={c["id"]}&todo=abholen">📦 {t("{n} nicht übergeben", n=c["undelivered_n"])}</a>')
    if not out and c["n"]:
        out = f'<span class="otag ok">{t("✓ alles erledigt")}</span>'
    return out or "–"


# ---- Papierkorb ---------------------------------------------------------------

def page_trash(store: Store, days: int, done: str | None) -> str:
    names = printer_names(store)
    jobs = store.trashed()
    now = time.time()
    rows = ""
    for j in jobs:
        left = max(0, int((j["deleted_ts"] + days * 86400 - now) // 86400))
        rows += (f'<tr><td data-l="{e(t("Druck"))}"><b>{e(j.get("task_name") or "–")}</b></td>'
                 f'<td data-l="{e(t("Drucker"))}">{e(names.get(j["printer"], j.get("printer_name") or j["printer"]))}</td>'
                 f'<td data-l="{e(t("Start"))}">{fmt_ts(j.get("start_ts"))}</td>'
                 f'<td class="n calc" data-l="{e(t("Kosten"))}">{fmt_num(j.get("cost_total"), 2, "€")}</td>'
                 f'<td data-l="{e(t("Gelöscht"))}">{fmt_ts(j["deleted_ts"])}</td>'
                 f'<td data-l="{e(t("Endgültig"))}">{t("heute") if left == 0 else t("morgen") if left == 1 else t("in {n} Tagen", n=left)}</td>'
                 f'<td class="tact"><form method="post" action="trash/restore"><input type="hidden" name="ids" value="{j["id"]}">'
                 f'<button class="btn sm">{t("↩ Wiederherstellen")}</button></form>'
                 f'<form method="post" action="trash/purge" class="pg"><input type="hidden" name="ids" value="{j["id"]}">'
                 f'<button class="del" title="{e(t("Endgültig löschen"))}">×</button></form></td></tr>')
    if not rows:
        rows = f'<tr><td colspan="7" class="muted">{t("Der Papierkorb ist leer.")}</td></tr>'
    msg = ""
    if done and done[1:].isdigit():
        n = int(done[1:])
        msg = (f'<div class="banner">' + (tn(n, "{n} Druck wiederhergestellt.", "{n} Drucke wiederhergestellt.") if done[0] == "r"
               else tn(n, "{n} Druck endgültig gelöscht.", "{n} Drucke endgültig gelöscht.")) + '</div>')
    ids = ",".join(str(j["id"]) for j in jobs)
    actions = (f'<div class="osum" style="display:flex;gap:10px;flex-wrap:wrap;margin:0 0 12px">'
               f'<form method="post" action="trash/restore" style="margin:0"><input type="hidden" name="ids" value="{ids}"><button class="btn">{t("↩ Alle wiederherstellen")}</button></form>'
               f'<form method="post" action="trash/purge" class="pg all" style="margin:0"><input type="hidden" name="all" value="1"><button class="btn danger">{t("Papierkorb leeren")}</button></form></div>'
               if jobs else "")
    body = f"""<style>.tact{{display:flex;gap:6px;align-items:center;justify-content:flex-end}}.tact form{{margin:0}}
@media (max-width:700px){{table.stack td.tact{{grid-column:1/-1;justify-content:space-between}}}}</style><div class="narrow">{msg}<p><a href="settings">{t("← Einstellungen")}</a></p><h2 style="margin-top:0">{t("Papierkorb")}</h2>
<p class="muted">{t("Gelöschte Drucke bleiben {n} Tage hier (ändern unter Einstellungen) und werden danach samt Fotos, Zahlungen und Übergaben endgültig gelöscht.", n=days)}</p>
{actions}<div class="wrap"><table class="stack"><thead><tr><th>{t("Druck")}</th><th>{t("Drucker")}</th><th>{t("Start")}</th><th class="n calc">{t("Kosten")}</th><th>{t("Gelöscht")}</th><th>{t("Endgültig weg")}</th><th></th></tr></thead>
<tbody>{rows}</tbody></table></div></div>"""
    script = ("document.querySelectorAll('form.pg').forEach(f=>f.onsubmit=()=>confirm(f.classList.contains('all')?"
              '_t("Papierkorb leeren? Alle Drucke darin werden endgültig gelöscht."):_t("Diesen Druck endgültig löschen?")));')
    return layout(t("Papierkorb"), "settings", body, script)


# ---- Einstellungen ------------------------------------------------------------

def _demo_row() -> str:
    """Einstellungen → Ansicht: Demo-Modus öffnen bzw. (in der Demo) beenden oder neu erzeugen."""
    if DEMO.get():
        ctl = (f'<a class="btn sm" href="../">{t("Demo beenden")}</a>'
               f'<button type="submit" class="btn sm" formaction="demo-neu" formmethod="post">{t("Beispieldaten neu erzeugen")}</button>')
        hint = t("Du bist im Demo-Modus. Alles hier ist erfunden und lässt sich gefahrlos ändern. Die Beispieldaten werden "
                 "nach 12 Stunden, nach einem Neustart und beim Sprachwechsel neu erzeugt.")
    else:
        ctl = f'<a class="btn sm" href="demo/">{t("Demo öffnen")}</a>'
        hint = t("Dieselbe Oberfläche mit erfundenen Beispieldaten, z. B. für Screenshots oder zum Ausprobieren. "
                 "Die Demo hat eine eigene Datenbank: Deine echten Drucke, Home Assistant und das Backup bleiben unberührt.")
    return (f'<div class="frow"><span class="flbl">{t("Demo-Modus")}</span><span class="fctl">{ctl}</span>'
            f'<small class="fhint muted">{hint}</small></div>')


def page_settings(store: Store, saved: str | None, logo: str | None = None,
                  live: dict[str, Any] | None = None, printer_msg: str | None = None,
                  safety: list[dict[str, Any]] | None = None, data_msg: dict[str, str] | None = None) -> str:
    names = printer_names(store)
    rates = store.printer_rates()
    prices = store.material_prices()

    def field(key: str, name: str, r: dict[str, Any], placeholder: str, label: str) -> str:
        return (f'<td class="n" data-l="{e(label)}"><input class="num" name="{name}__{e(key)}" value="{fmt_input(r.get(name))}" '
                f'inputmode="decimal" placeholder="{e(placeholder)}"></td>')

    printer_rows = []
    for key, name in names.items():
        r = rates.get(key, {})
        rate = hourly_rate(r)
        printer_rows.append(
            f"<tr><td>{e(name)}</td>"
            + field(key, "purchase_eur", r, t("z. B. {v}", v=1299), t("Anschaffung €"))
            + field(key, "life_hours", r, t("z. B. {v}", v=5000), t("Lebensdauer h"))
            + field(key, "maint_per_1000h", r, t("z. B. {v}", v=75), t("Wartung €/1000 h"))
            + f'<td class="n" data-l="{e(t("= pro Stunde"))}">{fmt_num(rate, 3, "€/h")}</td></tr>'
        )

    types = sorted(set(KNOWN_TYPES) | set(store.material_types())
                   | {k for k, v in prices.items() if v["kind"] == "type"})
    def tile(label: str, name: str, value: Any, placeholder: str) -> str:
        return (f'<label class="ftile"><span class="flbl">{label}</span><span class="ig"><input class="num" name="{name}" '
                f'value="{fmt_input(value)}" inputmode="decimal" placeholder="{e(placeholder)}"><i>€/kg</i></span></label>')

    type_rows = "".join(tile(e(ty), f"type__{e(ty)}", prices.get(ty, {}).get("eur_per_kg"), "–") for ty in types)
    seen = {m["name"]: m["type"] for m in store.material_names()}
    for k, v in prices.items():
        if v["kind"] == "name":
            seen.setdefault(k, None)
    name_rows = "".join(
        tile(f'{e(n)} <small class="muted">{e(ty or "")}</small>', f"name__{e(n)}", prices.get(n, {}).get("eur_per_kg"), t("wie Typ"))
        for n, ty in sorted(seen.items())
    )

    banner = f'<div class="banner">{t("Gespeichert. {n} Drucke wurden neu berechnet.", n=e(saved))}</div>' if saved else ""
    cfg = store.settings()
    margin_rows = "".join(
        f'<div class="frow"><span class="flbl">{t("Marge {i}", i=i)}</span><span class="fctl">'
        f'<input name="margin{i}_name" value="{e(cfg.get(f"margin{i}_name") or "")}" placeholder="{ph}" aria-label="{e(t("Name"))}" class="mname">'
        f'<span class="ig"><input class="num" name="margin{i}_pct" value="{e(cfg.get(f"margin{i}_pct") or "")}" inputmode="decimal" '
        f'placeholder="0" aria-label="{e(t("Aufschlag %"))}"><i>%</i></span>'
        f'<label class="mwork" title="{e(t("Aufschlag auch auf Arbeit und Design (sonst kommen sie zum Stundensatz dazu)"))}">'
        f'<input type="checkbox" name="margin{i}_work" value="1"{" checked" if cfg.get(f"margin{i}_work") == "1" else ""}> {t("auch auf Arbeitszeit")}</label></span></div>'
        for i, ph in ((1, t("z. B. Privat")), (2, t("z. B. Gewerbe")))
    )
    from datetime import datetime

    from .backup import BACKUP_DIR, INTERVALS, WEEKDAYS, backup_msg, describe, enabled, next_run, schedule
    last = (cfg.get("backup_last") or "").split("|", 2)
    interval, weekday, hour = schedule(cfg)

    def options(items: list[tuple[str, str]], current: str) -> str:
        return "".join(f'<option value="{e(v)}"{" selected" if v == current else ""}>{e(lbl)}</option>' for v, lbl in items)

    backup_form = (
        '<div class="fbox inline">'
        f'<label class="fcol">{t("Intervall")}<select name="backup_interval">{options([(k, t(v)) for k, v in INTERVALS.items()], interval)}</select></label>'
        f'<label class="fcol">{t("Wochentag")} <span class="muted">{t("(nur wöchentlich)")}</span><select name="backup_weekday">'
        f'{options([(str(i), t(d)) for i, d in enumerate(WEEKDAYS)], str(weekday))}</select></label>'
        f'<label class="fcol">{t("Uhrzeit")}<select name="backup_hour">'
        f'{options([(str(h), f"{h:02d}:00") for h in range(24)], str(hour))}</select></label>'
        '</div>'
    )
    if DEMO.get():
        backup_html = (f'<h2>{t("Automatisches Backup")}</h2><p class="muted">'
                       + t("Im Demo-Modus aus. Die Beispieldaten werden nie ins Backup-Ziel geschrieben.") + '</p>')
    elif enabled():
        state = (t("noch keins") if len(last) < 3 else
                 f'{fmt_ts(float(last[0]))}: {"✓ " + e(backup_msg(last[2])) if last[1] == "ok" else "⚠ " + e(last[2])}')
        nxt = next_run(cfg, datetime.now())
        backup_html = (f'<h2>{t("Automatisches Backup")}</h2><p>{t("{when} nach {dir}.", when=e(describe(cfg)), dir=f"<code>{e(BACKUP_DIR)}</code>")} '
                       f'{t("Nächstes Backup")}: {fmt_ts(nxt.timestamp())}. {t("Letztes Backup")}: {state}</p>'
                       f'{backup_form}'
                       f'<p><button type="submit" formaction="backup" formmethod="post">{t("Jetzt sichern")}</button></p>')
    else:
        backup_html = (f'<h2>{t("Automatisches Backup")}</h2><p class="muted">'
                       + t("Aus. Zum Einschalten im Stack einen Netzwerkordner als <code>/backup</code> einhängen und "
                           "<code>BACKUP_DIR=/backup</code> setzen (siehe README). Den Zeitplan kannst du schon vorab festlegen.")
                       + f'</p>{backup_form}')
    custom_logo = cfg.get("bon_logo")
    logo_src = f"images/{e(custom_logo)}" if custom_logo else "static/logo-bon.png"
    logo_note = {"ok": t("Neues Logo gespeichert."), "reset": t("Bon nutzt wieder das App-Logo."),
                 "bad": t("Das Bild ließ sich nicht lesen. Bitte PNG, JPG oder WebP nehmen.")}.get(logo or "", "")
    reset_btn = (f'<button type="submit" name="bon_logo_reset" value="1" class="lnk">{t("Zurück zum App-Logo")}</button>'
                 if custom_logo else "")
    logo_html = f"""<h2 style="margin-top:0">{t("Logo auf Bon und PDF")}</h2>
<p class="muted">{t("Steht oben auf dem Bon-Bild und im Kopf der PDF-Kostenaufstellung. Ohne eigenes Logo gilt das App-Logo. Am besten ein PNG mit durchsichtigem Hintergrund, mindestens 600 px breit.")}</p>
{f'<div class="banner">{e(logo_note)}</div>' if logo_note else ""}
<div class="logobox"><div class="paper"><img src="{logo_src}" alt=""><span>{e(bon_tagline())}</span></div>
<div class="logoctl"><b>{t("Eigenes Logo") if custom_logo else t("App-Logo (Standard)")}</b>
<label class="btn file">🖼 {t("Anderes Logo wählen") if custom_logo else t("Eigenes Logo hochladen")}<input type="file" name="bon_logo" accept="image/png,image/jpeg,image/webp"></label>
<label class="chk"><input type="checkbox" name="bon_logo_white" value="1"> {t("Weißen Hintergrund durchsichtig machen")}</label>
<span class="muted logofile"></span>
{reset_btn}
</div></div>"""

    printers_html = _printers_pane(store, live, printer_msg)

    def pane(key: str, label: str, html: str) -> tuple[str, str, str]:
        return key, label, html

    cur_lang = cfg.get("language") or "auto"
    lang_opts = opt("auto", t("Automatisch (Sprache des Browsers)"), cur_lang) + "".join(
        opt(k, v, cur_lang) for k, v in LANGS.items())
    price_entity = e(os.environ.get("PRICE_ENTITY", "input_number.strompreis"))
    panes = [
        pane("kosten", t("Kosten"), f"""<h2 style="margin-top:0">{t("Drucker: Abschreibung und Wartung")}</h2>
<p class="muted">{t("Kosten pro Druckstunde = Anschaffung ÷ Lebensdauer in Druckstunden + Wartung pro 1000 Druckstunden ÷ 1000. Wartung meint alles, was mit der Laufzeit verschleißt und ersetzt oder gepflegt wird (Düse, Riemen, Fett, Filter, Druckplatte). Leere Felder zählen nicht.")}</p>
<div class="wrap"><table class="stack rates"><thead><tr><th>{t("Drucker")}</th><th class="n">{t("Anschaffung €")}</th><th class="n">{t("Lebensdauer h")}</th>
<th class="n">{t("Wartung €/1000 h")}</th><th class="n">{t("= pro Stunde")}</th></tr></thead>
<tbody>{"".join(printer_rows)}</tbody></table></div>
<p class="muted">{t("Der Strompreis kommt aus Home Assistant ({entity}) bzw. aus der HA-Automation und wird pro Druck zum Druckende festgehalten.", entity=price_entity)}</p>"""),
        pane("drucker", t("Drucker"), printers_html),
        pane("filament", t("Filament"), f"""<h2 style="margin-top:0">{t("Filamentpreise nach Typ")}</h2>
<p class="muted">{t("Standardpreis pro Kilogramm, gilt für alle Filamente dieses Typs.")}</p>
<div class="fgrid">{type_rows}</div>
<h2>{t("Filamentpreise nach Sorte (optional)")}</h2>
<p class="muted">{t("Überschreibt den Typpreis für eine bestimmte Sorte, z. B. „Bambu PLA Matte“. Leer lassen = Typpreis.")}</p>
{f'<div class="fgrid wide">{name_rows}</div>' if name_rows else f'<p class="muted">{t("Erscheint, sobald Drucke mit Materialangaben erfasst sind.")}</p>'}"""),
        pane("zubehoer", t("Arbeit &amp; Zubehör"), _extras.settings_section(store)),
        pane("margen", t("Margen"), f"""<h2 style="margin-top:0">{t("Margen")}</h2>
<p class="muted">{t("Bis zu zwei allgemeine Aufschläge auf die Gesamtkosten. Jede Marge bekommt in der Tabelle eine eigene Spalte mit Verkaufspreis (und Preis pro Stück). Eine Marge pro Kunde stellst du unter „Kunden“ ein.")}</p>
<div class="fbox">{margin_rows}</div>
<p class="muted">{t("„Auch auf Arbeitszeit“: Ist dein Stundensatz schon dein Preis, bleibt es aus (Arbeit kommt nach der Marge dazu). Rechnest du den Stundensatz als Kosten (z. B. bei Gewerbekunden), schalte es ein. Pro Kunde unter „Kunden“ einstellbar.")}</p>
<h2>{t("Vorrat")}</h2>
<div class="fbox"><div class="frow"><label class="flbl" for="s_stock">{t("Preis beim Abgeben aus dem Vorrat")}</label>
<select id="s_stock" name="stock_price">{"".join(opt(k, t(v), cfg.get("stock_price") or "cost") for k, v in _split.STOCK_PRICE.items())}</select>
<small class="fhint muted">{t("Vorschlag im Fenster „Aus Vorrat abgeben“. Der Preis lässt sich dort immer ändern.")}</small></div></div>"""),
        pane("bon", t("Bon &amp; PDF"), logo_html),
        pane("ansicht", t("Ansicht"), f"""<h2 style="margin-top:0">{t("Ansicht")}</h2>
<div class="fbox">
<div class="frow"><label class="flbl" for="s_lang">Sprache · Language</label><select id="s_lang" name="language">{lang_opts}</select>
<small class="fhint muted">{t("Sprache der Oberfläche, von Bon, PDF und CSV-Export. „Automatisch“ richtet sich nach der Sprache des Browsers.")}</small></div>
<div class="frow"><label class="flbl" for="s_theme">{t("Farbschema")}</label><select id="s_theme" name="theme">{"".join(opt(k, t(v), cfg.get("theme") or "auto") for k, v in THEMES.items())}</select>
<small class="fhint muted">{t("Hell oder dunkel, für alle Geräte. „Automatisch“ richtet sich nach der Einstellung des Geräts.")}</small></div>
<div class="frow"><label class="flbl" for="s_dtab">{t("Detailseite")}: {t("Start-Tab")}</label><select id="s_dtab" name="detail_tab">{"".join(opt(k, t(v), cfg.get("detail_tab") or "ov") for k, v in DETAIL_TABS.items())}</select>
<small class="fhint muted">{t("Welcher Tab beim Öffnen eines Drucks aktiv ist. Nach dem Speichern zeigt die Seite immer die Übersicht.")}</small></div>
<div class="frow"><label class="flbl" for="trash_days">{t("Papierkorb")}</label><span class="fctl">
<span class="ig"><input class="num" id="trash_days" name="trash_days" inputmode="numeric" value="{e(cfg.get("trash_days") or "30")}"><i>{t("Tage")}</i></span>
<a class="btn sm" href="trash">{t("Papierkorb öffnen ({n})", n=store.trash_count())}</a></span>
<small class="fhint muted">{t("Gelöschte Drucke landen erst im Papierkorb und lassen sich dort wiederherstellen. Nach dieser Zeit werden sie samt Fotos endgültig gelöscht.")}</small></div>
<div class="frow"><label class="flbl" for="s_hn">🔔 {t("Hinweise an Home Assistant")}</label><span class="fctl">
<input id="s_hn" name="hint_notify" value="{e(cfg.get("hint_notify") or "")}" placeholder="notify.mobile_app_… / persistent_notification" maxlength="120">
<a class="btn sm" href="hints">{t("Hinweise öffnen")}</a> <a class="btn sm" href="models">🧩 {t("Modelle")}</a></span>
<small class="fhint muted">{t("Fragt printlog bei einem Druck nach dem Modell, kann es dir zusätzlich eine Nachricht schicken: Dienst eintragen, z. B. notify.mobile_app_handy, oder persistent_notification für eine Meldung in Home Assistant. Leer = nur die Glocke oben.")}</small></div>
{_demo_row()}
</div>"""),
        pane("backup", t("Daten &amp; Backup"), _data_pane(store, names, backup_html, safety or [], data_msg or {})),
        pane("windows", "Windows", f"""<h2 style="margin-top:0">{t("Ordner-Links (Windows)")}</h2>
<p>{t("Bei jedem Druck kannst du einen lokalen Ordner oder eine Datei eintragen. „📋 Kopieren“ geht immer (dann Win+E, Strg+L, einfügen). Damit „📂 Öffnen“ direkt funktioniert, richte einmalig pro Windows-PC das Link-Protokoll ein:")}</p>
<ol>
<li>{t('<a href="tools/printlog-einrichten.cmd" download>printlog-einrichten.cmd</a> herunterladen und doppelklicken. Falls Windows „Der Computer wurde geschützt“ zeigt: „Weitere Informationen“ → „Trotzdem ausführen“.')}</li>
<li>{t("Im Dialog deinen <b>Projektordner</b> wählen. 3D-Dateien darin (3MF, STL, STEP, OBJ) öffnen sich dann direkt im Standardprogramm, z. B. Bambu Studio. „Abbrechen“ heißt: Dateien nie direkt öffnen, nur im Explorer markieren.")}</li>
<li>{t("Beim ersten Klick auf „📂 Öffnen“ fragt der Browser nach. Mit „Immer erlauben“ kommt die Frage nicht wieder.")}</li>
</ol>
<p class="muted">{t("Sicherheit: Ordner öffnen sich nur im Explorer. Eine Datei startet nur, wenn sie eine 3D-Endung hat <i>und</i> in deinem Projektordner liegt, sonst wird sie im Explorer markiert. Programme und Skripte startet ein Link nie. Den Projektordner legt nur dein PC fest, nicht PrintLog. Zum Ändern die Einrichtung erneut starten.")}
{t("Pfade mit Leerzeichen und Umlauten sowie Netzwerkpfade (<code>\\\\server\\freigabe</code>) gehen. Die Einrichtung legt alles unter <code>%LOCALAPPDATA%\\printlog</code> ab, ohne Adminrechte.")}
{t('Entfernen: <a href="tools/printlog-open-entfernen.reg" download>printlog-open-entfernen.reg</a> öffnen. Nur das Skript: <a href="tools/printlog-open.ps1" download>printlog-open.ps1</a>.')}</p>"""),
    ]
    tabs = "".join(f'<button type="button" data-tab="{k}" class="{"on" if i == 0 else ""}">{lbl}</button>'
                   for i, (k, lbl, _) in enumerate(panes))
    panes_html = "".join(f'<section class="spane{" on" if i == 0 else ""}" data-pane="{k}">{h}</section>'
                         for i, (k, _, h) in enumerate(panes))
    body = f"""<style>{SETTINGS_CSS}{DATA_CSS}{_extras.SETTINGS_CSS}</style>{banner}<form method="post" enctype="multipart/form-data" class="settings">
<input type="hidden" name="tab" value="kosten">
<div class="stabs" role="tablist">{tabs}</div>
{panes_html}
<div class="sbar"><button class="primary" type="submit">{t("Speichern")}</button>
<span class="muted">{t("Preise gelten danach für alle Drucke (werden neu berechnet).")}</span></div>
</form>
<form id="csvf" action="export.csv" method="get"></form>
<form id="impf" action="import" method="post" enctype="multipart/form-data"></form>
<form id="safef" action="import-safety" method="post"></form>"""
    return layout(t("Einstellungen"), "settings", body, SETTINGS_JS)


SETTINGS_CSS = """
.settings{max-width:960px}
.spane>p,.spane>ol{max-width:76ch}
.spane table.rates input.num{width:116px}
/* Tablet hochkant: Drucker-Kosten wie am Handy als Karten (Felder sonst zu schmal für die Platzhalter) */
@media (min-width:701px) and (max-width:1000px){.spane table.rates thead{display:none}
 .spane table.rates tr{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px 12px;padding:12px 14px;border-bottom:1px solid var(--line)}
 .spane table.rates tr:last-child{border-bottom:0}.spane table.rates td{border:0;padding:0;text-align:left;white-space:normal}
 .spane table.rates td:first-child{grid-column:1/-1;font-weight:600}
 .spane table.rates td[data-l]:before{content:attr(data-l);display:block;font-size:11px;color:var(--muted);margin-bottom:3px}
 .spane table.rates input.num{width:100%;text-align:left}}
.fgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(178px,1fr));gap:8px}
.fgrid.wide{grid-template-columns:repeat(auto-fill,minmax(min(100%,290px),1fr))}
.ftile{display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:5px 5px 5px 12px;min-width:0}
.ftile:focus-within{border-color:var(--accent)}
.ftile .flbl{font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ftile .flbl small{font-weight:400}
.ig{display:inline-flex;align-items:center;flex:0 0 auto;border:1px solid var(--line);border-radius:8px;background:var(--bg);overflow:hidden}
.ig input.num{border:0;border-radius:0;background:none;width:68px;height:calc(var(--ctl-h) - 2px);padding:0 4px 0 8px}
.ig input:focus{outline:none}
.ig i{font-style:normal;font-size:12px;line-height:1;color:var(--muted);padding:0 9px 0 2px;white-space:nowrap}
.fbox{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:4px 14px;max-width:680px}
.frow{display:grid;grid-template-columns:170px minmax(0,1fr);gap:6px 14px;align-items:center;padding:9px 0}
.frow+.frow{border-top:1px solid var(--line)}
.frow .flbl{font-weight:600}
.frow .fhint{grid-column:2;font-size:12px;line-height:1.4}
.frow select{width:100%;max-width:340px}
.mwork{display:inline-flex;align-items:center;gap:6px;font-size:13.5px;font-weight:600;color:var(--muted);margin-left:12px;white-space:nowrap}
.fctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;min-width:0}
.fctl .mname{flex:1 1 160px;min-width:0;max-width:260px}
.fbox.inline{display:flex;flex-wrap:wrap;align-items:flex-end;gap:10px 16px;padding:12px 14px}
.fcol{display:flex;flex-direction:column;gap:4px;font-size:12px;font-weight:600;color:var(--muted);white-space:nowrap}
.fcol select{min-width:150px;font-weight:400;color:var(--fg)}
@media (max-width:520px){
 .frow{grid-template-columns:1fr;gap:6px}.frow .fhint{grid-column:1}
 .frow select{max-width:none}
 .fgrid{grid-template-columns:repeat(2,minmax(0,1fr))}
 .fgrid.wide{grid-template-columns:1fr}
 .fgrid{gap:6px}.ftile{padding:4px 4px 4px 10px;gap:6px}.ftile .ig input.num{width:56px}.fgrid.wide .ftile .ig input.num{width:70px}
 .fbox.inline .fcol{flex:1 1 140px}.fcol select{min-width:0;width:100%}
}
.stabs{display:flex;gap:4px;margin:0 0 16px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:4px;
  width:max-content;max-width:100%;overflow-x:auto;scrollbar-width:none}
.stabs::-webkit-scrollbar{display:none}
.stabs button{border:0;background:none;min-height:0;padding:8px 16px;border-radius:9px;color:var(--muted);font-weight:700;white-space:nowrap;flex:0 0 auto}
.stabs button.on{background:var(--accent);color:var(--bg)}
.spane{display:none}.spane.on{display:block}
.sbar{position:sticky;bottom:0;display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:18px;padding:12px 0;
  background:linear-gradient(transparent,var(--bg) 30%)}
.spane[data-pane=windows].on~.sbar{display:none}
.logobox{display:flex;gap:18px;flex-wrap:wrap;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.logobox .paper{background:#f4eee3;border-radius:8px;padding:18px 26px;display:flex;flex-direction:column;align-items:center;gap:8px;
  min-width:260px;box-shadow:0 2px 10px rgba(0,0,0,.12)}
.logobox .paper img{max-width:300px;max-height:85px;object-fit:contain}
.logobox .paper span{font:11px ui-monospace,Menlo,Consolas,monospace;color:#80776a}
.logoctl{display:flex;flex-direction:column;gap:10px;align-items:flex-start}
.chk{display:flex;gap:6px;align-items:center}
.plist{display:flex;flex-direction:column;gap:12px}
.pcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.phead{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin-bottom:10px}
.phead .pk{font-size:15px;font-weight:700;background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:6px 10px}
.phead .grow{flex:1 1 220px}
.phead .prm{color:var(--bad);font-size:13px;padding-bottom:8px}
.pcard label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted);font-weight:600}
.pcard label.chk{flex-direction:row}
.pcard input:not([type=checkbox]){width:100%;font-family:ui-monospace,Menlo,Consolas,monospace}
.phead input:not([type=checkbox]){font-family:inherit}
.pkey{max-width:140px}
.pgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px 14px}
.pcard small{font-weight:400}
.pcard details{margin-top:10px}
.pcard summary{cursor:pointer;font-size:13px;color:var(--accent);font-weight:600;margin-bottom:8px}
.pchk{font-size:12px;font-weight:600}.pchk.ok{color:var(--ok,#2e9e5b)}.pchk.bad{color:var(--bad)}
.padd{margin-top:12px}.padd>summary{cursor:pointer;font-weight:700;color:var(--accent);margin-bottom:10px}
button.lnk{border:0;background:none;padding:0;color:var(--bad);text-decoration:underline}
@media (max-width:700px){
 .stabs{position:sticky;top:0;z-index:5;width:auto;box-shadow:0 6px 14px rgba(0,0,0,.18)}
 .stabs button{padding:8px 12px}
 .logobox .paper{min-width:0;width:100%}
 .sbar .muted{font-size:12px}
}
"""

SETTINGS_JS = """
const sf=document.querySelector('form.settings');
function stab(n){if(!sf.querySelector('.spane[data-pane="'+n+'"]'))return;
  sf.querySelectorAll('.stabs button').forEach(b=>{const on=b.dataset.tab===n;b.classList.toggle('on',on);if(on)b.scrollIntoView({block:'nearest',inline:'nearest'})});
  sf.querySelectorAll('.spane').forEach(p=>p.classList.toggle('on',p.dataset.pane===n));
  sf.querySelector('input[name=tab]').value=n}
sf.querySelectorAll('.stabs button').forEach(b=>b.onclick=()=>{stab(b.dataset.tab);history.replaceState(null,'','#'+b.dataset.tab)});
if(location.hash)stab(location.hash.slice(1));
const imf=document.querySelector('input[name=file][form=impf]');
if(imf)imf.onchange=()=>{const f=imf.files[0];if(f)document.querySelector('.impname').textContent=f.name};
const lf=sf.querySelector('input[name=bon_logo]');
if(lf)lf.onchange=()=>{const f=lf.files[0];if(!f)return;sf.querySelector('.logofile').textContent=f.name+' – '+_t("wird beim Speichern übernommen");
  const im=sf.querySelector('.logobox img');im.src=URL.createObjectURL(f)};
"""


def _printers_pane(store: Store, live: dict[str, Any] | None, msg: str | None) -> str:
    """Einstellungen → Drucker: Zuordnung der HA-Entitäten pro Drucker."""
    from . import printers as pcfg

    def check(entity: str | None, unit: str = "") -> str:
        if not entity:
            return ""
        if live is None:
            return f'<span class="pchk muted" title="{e(t("Home Assistant nicht verbunden"))}">?</span>'
        state = live.get(entity)
        if state is None:
            return f'<span class="pchk bad" title="{e(t("Gibt es in Home Assistant nicht"))}">{t("⚠ nicht gefunden")}</span>'
        val = str(state)
        if unit and val not in ("unavailable", "unknown"):
            val = f"{val} {unit}"
        return f'<span class="pchk ok">✓ {e(val)}</span>'

    def inp(name: str, value: Any, placeholder: str = "", cls: str = "", extra: str = "") -> str:
        v = "" if value is None else (f"{value:g}" if isinstance(value, float) else str(value))
        return (f'<input name="{name}" value="{e(v)}" placeholder="{e(placeholder)}" '
                f'class="{cls}" autocomplete="off" spellcheck="false"{extra}>')

    def card(c: dict[str, Any] | None) -> str:
        new = c is None
        c = c or {"key": "", "name": "", "status": "", "prefix": "", "ams": [], "ext": "", "energy": "", "fixed_w": None}
        f = "prnew" if new else f"pr__{c['key']}"
        status = c.get("status") or (pcfg.status_entity(c) if not new else "")
        ams = ", ".join(c.get("ams") or [])
        ams_check = "".join(check(f"sensor.{a}_tray_1") for a in (c.get("ams") or [])[:1])
        head = (f'<div class="phead"><label>{t("Kurzname")} {inp(f"{f}__key", "", t("z. B. {v}", v="x1c"), "pkey", " pattern=[a-z0-9_]{1,20}")}</label>'
                f'<label class="grow">{t("Name")} {inp(f"{f}__name", "", t("z. B. {v}", v=t("X1C Werkstatt")))}</label></div>'
                if new else
                f'<div class="phead"><code class="pk">{e(c["key"])}</code>'
                f'<label class="grow">{t("Name")} {inp(f"{f}__name", c.get("name"), c["key"])}</label>'
                f'<label class="chk prm"><input type="checkbox" name="{f}__remove" value="1"> {t("Entfernen")}</label></div>')
        return f"""<div class="pcard{' pnew' if new else ''}">{head}
<div class="pgrid">
<label>{t("Druckstatus-Sensor")} {inp(f"{f}__status", status, "sensor.…_print_status")}{check(status)}</label>
<label>{t("Energiezähler (kWh)")} {inp(f"{f}__energy", c.get("energy"), t("leer = keine Steckdose"))}{check(c.get("energy"), "kWh")}</label>
<label>{t("Feste Leistung (W)")} {inp(f"{f}__fixed_w", c.get("fixed_w"), t("z. B. {v}", v=120), "", ' inputmode="decimal"')}
<small class="muted">{t("Ohne Steckdose oder wenn sie ausfällt: kWh = Druckdauer × Leistung")}</small></label>
</div>
<details{' open' if new else ''}><summary>{t("Filament und weitere Entitäten")}</summary><div class="pgrid">
<label>{t("AMS (Namensanfang, mit Komma)")} {inp(f"{f}__ams", ams, t("z. B. {v}", v="x1c_ams_1"))}{ams_check}</label>
<label>{t("Externe Spule")} {inp(f"{f}__ext", c.get("ext"), "sensor.…_external_spool")}{check(c.get("ext"))}</label>
<label>{t("Namensanfang der übrigen Entitäten")} {inp(f"{f}__prefix", c.get("prefix"), pcfg.prefix({"status": status}) or t("aus dem Druckstatus"))}
<small class="muted">{t("Für Auftrag, Gewicht, Fortschritt und Vorschaubild (sensor.&lt;anfang&gt;_task_name …). Leer = aus dem Druckstatus.")}</small></label>
</div></details></div>"""

    cfgs = pcfg.load(store)
    note = f'<div class="banner">{e(msg)}</div>' if msg else ""
    cards = "".join(card(c) for c in cfgs) or f'<p class="muted">{t("Noch kein Drucker eingerichtet.")}</p>'
    return f"""<h2 style="margin-top:0">{t("Drucker und Home Assistant")}</h2>
<p class="muted">{t("Welche Entitäten aus Home Assistant zu welchem Drucker gehören. printlog braucht sie, um nach einem Neustart verpasste Drucke nachzuholen und Material und Strom zuzuordnen. Mit dem Blueprint „printlog: Druck erfassen“ (siehe README) trägt die Automation ihre Entitäten beim nächsten Druck selbst ein und legt neue Drucker an. ✓ zeigt den aktuellen Wert aus Home Assistant.")}</p>
{note}<div class="plist">{cards}</div>
<details class="padd"><summary>{t("＋ Drucker von Hand hinzufügen")}</summary>{card(None)}</details>"""


def csv_rows(store: Store, f: dict[str, str | None]) -> list[list[Any]]:
    names = printer_names(store)
    header = [t(h) for h in (N_("ID"), N_("Projekt-ID"), N_("Auftrag"), N_("Drucker"), N_("Kunde"), N_("Stück"), N_("Start"), N_("Ende"), N_("Dauer h"), N_("Status"),
              N_("Fortschritt %"), N_("Filament g"), N_("Material"), N_("kWh"), N_("Strom €"), N_("Filament €"), N_("Drucker €"),
              N_("Gesamt €"), N_("Fehldruck"), N_("Ursache"), N_("Berechnung"), N_("Projekt"), N_("Projektlink"), N_("Ordner"), N_("Notiz"))]
    mg = margins(store)
    at = header.index(t("Gesamt €")) + 1
    header[at:at] = [f"{n} +{fmt_input(p)} % €" for n, p in mg]
    header += [t("Endpreis €"), t("Zu zahlen €"), t("Bezahlt €"), t("Übergeben Stück"), t("Gehört zu Projekt"), t("Aufteilung"),
               t("Zubehör €"), t("Zubehör"), t("Arbeit min"), t("Design min"), t("Arbeit + Design €"), t("Montiert Stück")]
    out: list[list[Any]] = [header]
    q = db_filters(f)
    codes = {p["id"]: code_label(p.get("code")) for p in store.projects(limit=100000)}
    jobs = store.list_jobs(limit=100000, real="1", **q)
    _split.attach(store, jobs)
    for j in jobs:
        split = None
        due = (0 if j.get("gift") or no_charge(j) else round(amount_due(j), 2)) if billable(j) and amount_due(j) is not None else None
        paid, delivered = round(j.get("paid") or 0, 2), j.get("delivered") or 0
        if j.get("_shares") is not None:
            sh = j["_shares"]
            split = "; ".join([f'{s.get("customer_name") or t("Ausgebucht")} {fmt_input(s.get("quantity"))}' for s in sh]
                              + [f'{t("Vorrat")} {_split.stock_text(j)}'])
            bill = [s for s in sh if billable(s)]
            due = round(sum(0 if s.get("gift") else amount_due(s) or 0 for s in bill), 2) if bill else None
            paid = round(sum(s.get("paid") or 0 for s in bill), 2)
            delivered = sum(s.get("delivered") or 0 for s in bill)
        mats = "; ".join(f'{m["name"] or "?"} {m["grams"]} g' for m in parse_materials(j.get("materials")))
        dur = (j["end_ts"] - j["start_ts"]) / 3600 if j["start_ts"] and j["end_ts"] and j["end_ts"] > j["start_ts"] else None
        grams = j["filament_g"] if j.get("filament_g") is not None else j["weight_g"]
        out.append([
            j["id"], code_label(j.get("code")), j["task_name"], names.get(j["printer"], j["printer"]), j.get("customer_name"),
            j.get("quantity"), fmt_ts(j["start_ts"]), fmt_ts(j["end_ts"]),
            round(dur, 2) if dur else None, t(_status_of(j)), j["progress"], grams, mats, j["kwh"],
            j["cost"], j.get("cost_filament"), j.get("cost_printer"), j.get("cost_total"), *[round(mprice(j, m), 2) if mprice(j, m) is not None else None for m in mg],
            t("ja") if j.get("is_scrap") else t("nein"), defect_label(j, icon=False) if j.get("is_scrap") else None,
            (charge_text(j) or t("nicht berechnet")) if j.get("is_scrap") else None, j.get("project_title"), j.get("project_url"), j.get("local_path"), j.get("job_note"),
            j.get("final_price"), due, paid, delivered, codes.get(j.get("project_id") or 0), split,
            j.get("cost_extras"), _extras.csv_extras(store, j) or None,
            round(sum(_extras.work_minutes(j)[:2]), 1) or None, j.get("design_min"), j.get("cost_work"), j.get("mounted"),
        ])
    return out



# ---- Einstellungen → Daten & Backup: Export, Import, Zurücksetzen --------------


def fmt_size(n: float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else fmt_num(n, 1, unit)
        n /= 1024
    return ""


def _data_pane(store: Store, names: dict[str, str], backup_html: str,
               safety: list[dict[str, Any]], msg: dict[str, str]) -> str:
    month_opts = opt("", t("Alle Monate"), None) + "".join(
        opt(m, datetime.strptime(m, "%Y-%m").strftime(t("%m/%Y")), None) for m in store.months())
    printer_opts = opt("", t("Alle Drucker"), None) + "".join(opt(k, v, None) for k, v in names.items())
    banner = ""
    if msg.get("error"):
        banner = f'<div class="banner bad">⚠ {e(msg["error"])}</div>'
    elif msg.get("restored") or msg.get("reset"):
        name = msg.get("restored") or msg.get("reset")
        text = t("Sicherung eingespielt.") if msg.get("restored") else t("printlog wurde zurückgesetzt.")
        banner = (f'<div class="banner">✓ {text} '
                  + t("Der vorherige Stand liegt als {link} bereit.",
                      link=f'<a href="safety/{e(name)}" download>{e(name)}</a>') + '</div>')
    rows = "".join(
        f'<tr><td data-l="{e(t("Anlass"))}">{t("vor dem Einspielen") if "vor-import" in s["name"] else t("vor dem Zurücksetzen")}</td>'
        f'<td data-l="{e(t("Erstellt"))}">{fmt_ts(s["ts"])}</td><td class="n" data-l="{e(t("Größe"))}">{fmt_size(s["size"])}</td>'
        f'<td><a class="btn sm" href="safety/{e(s["name"])}" download>⬇ ZIP</a></td>'
        f'<td><button type="submit" class="btn sm" form="safef" name="name" value="{e(s["name"])}">{t("Einspielen …")}</button></td></tr>'
        for s in safety)
    safety_html = (f'<h3>{t("Automatische Sicherungen")}</h3>'
                   f'<p class="muted">{t("Vor jedem Einspielen und Zurücksetzen legt printlog den bisherigen Stand hier ab (die letzten {n}).", n=5)}</p>'
                   f'<div class="wrap"><table class="stack"><tbody>{rows}</tbody></table></div>') if rows else ""
    return f"""{banner}<h2 style="margin-top:0">{t("Exportieren")}</h2>
<div class="dgrid">
<div class="dcard"><h3>📦 {t("Komplettsicherung (ZIP)")}</h3>
<p class="muted">{t("Alles in einer Datei: Datenbank mit Drucken, Kunden, Zahlungen und Papierkorb, alle Fotos und Cover-Bilder, das eigene Bon-Logo und sämtliche Einstellungen (Drucker, Kosten, Filamentpreise, Margen).")}</p>
<a class="btn primary" href="backup.zip" download>⬇ {t("ZIP herunterladen")}</a></div>
<div class="dcard"><h3>📄 {t("CSV für Tabellenkalkulation")}</h3>
<p class="muted">{t("Liste der Drucke mit Kosten, z. B. für Excel. Nur zum Auswerten, lässt sich nicht wieder einspielen.")}</p>
<div class="row"><select name="month" form="csvf">{month_opts}</select><select name="printer" form="csvf">{printer_opts}</select>
<button type="submit" class="btn" form="csvf">⬇ {t("CSV herunterladen")}</button></div></div>
</div>
<h2>{t("Sicherung einspielen")}</h2>
<div class="dcard"><p class="muted">{t("ZIP aus „Komplettsicherung“ hochladen. printlog prüft die Datei und zeigt erst eine Vorschau, überschrieben wird nichts, bevor du bestätigst. Vorher sichert printlog automatisch den jetzigen Stand.")}</p>
<div class="row"><label class="btn file">🗂 <span class="impname">{t("ZIP auswählen")}</span><input type="file" name="file" form="impf" accept=".zip,application/zip" required></label>
<button type="submit" class="btn" form="impf">{t("Prüfen und Vorschau")} →</button></div>
{safety_html}</div>
{backup_html}
<h2>{t("Zurücksetzen")}</h2>
<div class="dcard danger"><p>{t("Löscht alle Drucke samt Verlauf, wahlweise auch alle Einstellungen. Vorher legt printlog automatisch eine Sicherung an, die du herunterladen und wieder einspielen kannst.")}</p>
<a class="btn danger" href="reset">{t("Zurücksetzen …")}</a></div>"""


def _cmp_rows(cur: dict[str, Any], new: dict[str, Any]) -> str:
    def span(d: dict[str, Any]) -> str:
        if not d.get("first_ts"):
            return "–"
        return f'{fmt_date(d["first_ts"])} – {fmt_date(d["last_ts"])}'

    items = [
        (t("Drucke"), cur["jobs"], new["jobs"]),
        (t("Kunden"), cur["customers"], new["customers"]),
        (t("Eigene Fotos"), cur["photos"], new["photos"]),
        (t("Bilddateien"), cur.get("images", "–"), new["images"]),
        (t("Zahlungen"), cur["payments"], new["payments"]),
        (t("Im Papierkorb"), cur["trash"], new["trash"]),
        (t("Zeitraum"), span(cur), span(new)),
    ]
    return "".join(f'<tr><td>{lbl}</td><td class="n">{e(str(a))}</td><td class="n"><b>{e(str(b))}</b></td></tr>'
                   for lbl, a, b in items)


def page_import(cur: dict[str, Any], new: dict[str, Any] | None, token: str | None, error: str | None,
                source: str | None = None) -> str:
    back = f'<p><a href="settings#backup">{t("← Einstellungen")}</a></p>'
    if error or not new:
        body = (f'{back}<h2 style="margin-top:0">{t("Sicherung einspielen")}</h2>'
                f'<div class="banner bad">⚠ {e(error or "")}</div>'
                f'<p class="muted">{t("Es wurde nichts verändert.")}</p>')
        return layout(t("Sicherung einspielen"), "settings", f"<style>{DATA_CSS}</style>{body}")
    warns = []
    if new.get("missing"):
        warns.append(tn(len(new["missing"]), "{n} Bild fehlt im ZIP, der Druck bleibt aber erhalten.",
                        "{n} Bilder fehlen im ZIP, die Drucke bleiben aber erhalten."))
    if cur.get("running"):
        warns.append(t("Gerade läuft ein Druck. Er fehlt nach dem Einspielen, wenn er nicht im ZIP ist. Besser nach dem Druck einspielen."))
    if new["jobs"] < cur["jobs"]:
        warns.append(t("Das ZIP enthält weniger Drucke als jetzt erfasst sind."))
    if isinstance(new.get("format"), int) and new["format"] > 1:
        warns.append(t("Die Sicherung stammt von einer neueren printlog-Version."))
    warn_html = "".join(f'<div class="banner warn">⚠ {w}</div>' for w in warns)
    try:
        created = fmt_date(datetime.fromisoformat(new["created"]).replace(tzinfo=None), "ts")
    except (KeyError, TypeError, ValueError):
        created = t("unbekannt")
    src = e(source) if source else t("hochgeladene Datei")
    body = f"""{back}<h2 style="margin-top:0">{t("Vorschau: Sicherung einspielen")}</h2>
<p>{t("Quelle")}: <b>{src}</b> · {t("erstellt")} {created} · {fmt_size(new["size"])}</p>
<p class="ok">✓ {t("Inhalt geprüft: gültige printlog-Datenbank, nur erlaubte Dateien, keine Pfade außerhalb des Datenordners.")}</p>
{warn_html}
<div class="wrap cmp"><table><thead><tr><th></th><th class="n">{t("Jetzt")}</th><th class="n">{t("Nach dem Einspielen")}</th></tr></thead>
<tbody>{_cmp_rows(cur, new)}</tbody></table></div>
<form method="post" action="import/apply" class="dcard danger confirm">
<input type="hidden" name="token" value="{e(token or "")}">
<p><b>{t("Der jetzige Stand wird komplett ersetzt:")}</b> {t("Drucke, Kunden, Fotos, Logo und Einstellungen kommen dann aus der Sicherung.")}
{t("Vorher legt printlog automatisch eine Sicherung des jetzigen Stands an, die du unter Einstellungen → Daten &amp; Backup wieder herunterladen oder einspielen kannst.")}</p>
<label class="chk"><input type="checkbox" name="confirm" value="1" required> {t("Ja, ich will diese Sicherung einspielen.")}</label>
<div class="row"><a class="btn" href="settings#backup">{t("Abbrechen")}</a><button type="submit" class="btn danger">{t("Jetzt einspielen")}</button></div>
</form>"""
    return layout(t("Sicherung einspielen"), "settings", f"<style>{DATA_CSS}</style>{body}")


def page_reset(cur: dict[str, Any], token: str, code: str, error: str | None = None) -> str:
    err = f'<div class="banner bad">⚠ {e(error)}</div>' if error else ""
    body = f"""<p><a href="settings#backup">{t("← Einstellungen")}</a></p>
<h2 style="margin-top:0">{t("printlog zurücksetzen")}</h2>{err}
<p>{t("Jetzt erfasst: {jobs} Drucke, {customers} Kunden, {photos} Fotos, {trash} im Papierkorb.",
       jobs=cur["jobs"], customers=cur["customers"], photos=cur["photos"], trash=cur["trash"])}</p>
<form method="post" action="reset" class="rform" autocomplete="off">
<input type="hidden" name="token" value="{e(token)}">
<h3>1. {t("Was soll gelöscht werden?")}</h3>
<label class="ropt"><input type="radio" name="scope" value="history" required>
<span><b>{t("Nur Drucke und Verlauf")}</b><br><span class="muted">{t("Alle Drucke, Fotos, Zahlungen, Übergaben und der Papierkorb. Kunden, Drucker, Kosten, Filamentpreise, Margen, Bon-Logo und Einstellungen bleiben.")}</span></span></label>
<label class="ropt"><input type="radio" name="scope" value="all" required>
<span><b>{t("Alles")}</b><br><span class="muted">{t("Zusätzlich Kunden, Drucker-Zuordnung, Kosten, Filamentpreise, Margen, Bon-Logo und alle übrigen Einstellungen. Nur die Sprache bleibt. Wie eine frische Installation.")}</span></span></label>
<h3>2. {t("Zur Bestätigung den Code eintippen")}</h3>
<p>{t("Code")}: <span class="code">{e(code)}</span></p>
<input name="code" class="codein" maxlength="8" required placeholder="{e(code[:1])}…" autocapitalize="characters" spellcheck="false">
<p class="muted">{t("Vorher legt printlog automatisch eine Sicherung an. Du kannst sie danach herunterladen und über „Sicherung einspielen“ zurückholen.")}</p>
<div class="row"><a class="btn" href="settings#backup">{t("Abbrechen")}</a>
<button type="submit" class="btn danger" disabled>{t("Endgültig zurücksetzen")}</button></div>
</form>"""
    script = """
const rf=document.querySelector('form.rform'),go=rf.querySelector('button[type=submit]'),want=%s;
function chk(){go.disabled=!(rf.querySelector('input[name=scope]:checked')&&rf.code.value.trim().toUpperCase()===want)}
rf.addEventListener('input',chk);rf.addEventListener('change',chk);
""" % json.dumps(code)
    return layout(t("printlog zurücksetzen"), "settings", f"<style>{DATA_CSS}</style>{body}", script)


DATA_CSS = """
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:12px}
.dcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:12px}
.dcard h3{margin:0 0 6px;font-size:15px}.dcard .row+h3{margin-top:18px}.dcard p{margin:0 0 10px}
.dcard.danger{border-color:var(--bad)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.btn.danger{background:var(--bad);border-color:var(--bad);color:#fff}
.banner.bad{border-color:var(--bad)}.banner.warn{border-color:var(--warn)}
.ok{color:var(--ok)}
.cmp{max-width:620px;margin-bottom:14px}
.confirm{max-width:620px}.confirm .chk{display:flex;gap:8px;align-items:center;margin:0 0 12px;font-weight:600}
.rform{max-width:640px}
.ropt{display:flex;gap:10px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:8px;cursor:pointer}
.ropt:has(input:checked){border-color:var(--bad)}
.ropt input{margin-top:3px}
.code{font:700 22px ui-monospace,Menlo,Consolas,monospace;letter-spacing:.18em;background:var(--card);border:1px dashed var(--bad);border-radius:8px;padding:4px 12px}
.codein{font:700 20px ui-monospace,Menlo,Consolas,monospace;letter-spacing:.18em;text-transform:uppercase;width:180px;margin-bottom:10px}
"""


from . import split as _split  # noqa: E402  (nutzt Helfer aus diesem Modul)
from . import extras as _extras  # noqa: E402
from . import models as _models  # noqa: E402
