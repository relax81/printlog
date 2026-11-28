"""Kosten vorab kalkulieren: Druckzeit und Filament aus einem MakerWorld-Link (oder von Hand) → Kosten je Drucker.

* **Quelle**: Die MakerWorld-Modellseite trägt ihre Daten als JSON in sich (``__NEXT_DATA__``). Pro Druckprofil
  stehen dort Titel, Drucker des Profils, kompatible Drucker und pro Platte Druckzeit, Gramm und Filamente
  (Typ, Farbe, Gramm). Ein Login ist nicht nötig. MakerWorld kann den Abruf zeitweise blocken (Cloudflare),
  dann bleibt die Eingabe von Hand.
* **Rechnen** (``quote``) wie bei echten Drucken, nur vorab:
  Filament = Gramm × €/kg (eigenes Filament, sonst Typpreis), Drucker = Zeit × (Abschreibung + Wartung),
  Strom = Zeit × Ø-Leistung des Druckers aus den bisherigen Drucken (sonst feste Leistung aus den
  Drucker-Einstellungen) × Ø-Strompreis der bisherigen Drucke. Für andere Drucker gilt dieselbe Zeit (Näherung).
* **Preis** mit einer allgemeinen Marge oder der Marge eines Kunden, Arbeit zum Stundensatz wie sonst auch.
* Übernehmen geht in einen neuen oder bestehenden Auftrag (Freitext-Position mit Platten, Druckzeit und
  Kosten pro Platte) oder im Auftragsformular direkt in die Felder der Position.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import extras as _extras
from . import printers as _pcfg
from .costing import KNOWN_TYPES, guess_type, hourly_rate
from .db import Store
from .i18n import t
from .ui import e, layout, printer_names

log = logging.getLogger("printlog.calc")

MAX_HTML = 12_000_000          # MakerWorld-Seiten sind 2–3 MB groß
MAX_PLATES = 60
MAX_FIL = 32
_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_PROFILE_RE = re.compile(r"profileId[-=_](\d+)", re.I)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0 Safari/537.36")
# Bambu-Druckernamen, die anders geschrieben werden als der übliche Kurzname
_ALIASES = {"x1carbon": "x1c"}


# ---- Zahlen ---------------------------------------------------------------------

def _num(v: Any) -> float | None:
    try:
        n = float(str(v).replace(",", ".")) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return n if n is not None and n == n and n >= 0 else None


def _color(v: Any) -> str | None:
    s = str(v or "").strip()
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?", s)
    return f"#{m.group(1).upper()}" if m else None


# ---- MakerWorld -----------------------------------------------------------------

def is_makerworld(url: str | None) -> bool:
    return bool(re.match(r"https?://([a-z0-9-]+\.)*makerworld\.com(\.cn)?/", str(url or ""), re.I))


def _fil(f: Any) -> dict[str, Any] | None:
    if not isinstance(f, dict):
        return None
    name = str(f.get("type") or f.get("name") or "").strip()[:40]
    grams = _num(f.get("usedG") if f.get("usedG") is not None else f.get("grams"))
    if not name and not grams:
        return None
    return {"type": guess_type(name) or (name.split()[0].upper() if name else None), "name": name or None,
            "color": _color(f.get("color")), "grams": round(grams or 0.0, 2)}


def parse_makerworld(html: str, url: str = "") -> dict[str, Any] | None:
    """Druckprofile aus dem HTML einer MakerWorld-Modellseite, None wenn keine Daten drinstehen."""
    m = _NEXT_RE.search(html or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        design = data["props"]["pageProps"]["design"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(design, dict):
        return None
    profiles = []
    for inst in design.get("instances") or []:
        if not isinstance(inst, dict):
            continue
        mi = (inst.get("extention") or {}).get("modelInfo") or {}
        comp = mi.get("compatibility") or {}
        printer = str(comp.get("devProductName") or "").strip() or None
        compat = [printer] if printer else []
        for c in mi.get("otherCompatibility") or []:
            n = str((c or {}).get("devProductName") or "").strip() if isinstance(c, dict) else ""
            if n and n not in compat:
                compat.append(n)
        plates = []
        for p in (mi.get("plates") or [])[:MAX_PLATES]:
            if not isinstance(p, dict):
                continue
            sec = _num(p.get("prediction"))
            fil = [x for x in (_fil(f) for f in (p.get("filaments") or [])[:MAX_FIL]) if x]
            plates.append({"name": str(p.get("name") or "").strip()[:80], "minutes": round(sec / 60, 1) if sec else None,
                           "thumb": ((p.get("thumbnail") or {}).get("url") if isinstance(p.get("thumbnail"), dict) else None),
                           "fil": fil})
        if not plates:  # Profil ohne Plattendaten: Gesamtwerte als eine Platte
            sec = _num(inst.get("prediction"))
            fil = [x for x in (_fil(f) for f in (inst.get("instanceFilaments") or [])[:MAX_FIL]) if x]
            if not fil and _num(inst.get("weight")):
                fil = [{"type": None, "name": None, "color": None, "grams": _num(inst.get("weight"))}]
            plates = [{"name": "", "minutes": round(sec / 60, 1) if sec else None, "thumb": None, "fil": fil}]
        profiles.append({"id": inst.get("id"), "title": str(inst.get("title") or "").strip()[:160],
                         "printer": printer, "compatible": compat, "plates": plates, "default": bool(inst.get("isDefault"))})
    if not profiles:
        return None
    want = _PROFILE_RE.search(url or "")
    idx = next((i for i, p in enumerate(profiles) if want and str(p["id"]) == want.group(1)), None)
    if idx is None:
        idx = next((i for i, p in enumerate(profiles) if p["default"]), 0)
    return {"source": "makerworld", "title": str(design.get("title") or "").strip()[:200], "url": url,
            "profiles": profiles, "profile": idx}


async def fetch_makerworld(session: Any, url: str) -> tuple[dict[str, Any] | None, str | None]:
    """(Daten, Fehlertext) für einen MakerWorld-Link."""
    if not is_makerworld(url):
        return None, t("Das ist kein MakerWorld-Link. Trag die Werte von Hand ein.")
    if session is None:
        return None, t("MakerWorld ist gerade nicht erreichbar. Trag die Werte von Hand ein.")
    try:
        async with session.get(url, timeout=_timeout(), headers={"User-Agent": _UA, "Accept-Language": "de,en;q=0.8",
                                                                  "Accept": "text/html,application/xhtml+xml"}) as resp:
            text = (await resp.content.read(MAX_HTML)).decode("utf-8", "replace")
            status = resp.status
    except Exception as err:  # noqa: BLE001
        log.info("MakerWorld nicht erreichbar: %s", err)
        return None, t("MakerWorld ist gerade nicht erreichbar. Trag die Werte von Hand ein.")
    data = parse_makerworld(text, url) if status == 200 else None
    if data:
        return data, None
    if status in (403, 429, 503) or "just a moment" in text[:5000].lower() or "cf-chl" in text[:20000]:
        return None, t("MakerWorld hat den Abruf blockiert. Versuch es später noch mal oder trag die Werte von Hand ein.")
    return None, t("Auf der Seite stehen keine Druckprofile. Trag die Werte von Hand ein.")


def _timeout() -> Any:
    import aiohttp

    return aiohttp.ClientTimeout(total=20)


def demo_sample(url: str) -> dict[str, Any]:
    """Erfundenes Beispiel für den Demo-Modus (zwei Druckprofile, eins mit drei Platten)."""
    def fil(tp: str, color: str, g: float) -> dict[str, Any]:
        return {"type": tp, "name": tp, "color": color, "grams": g}
    return {"source": "makerworld", "title": t("Weihnachtsglocken mit Zweigen"), "url": url, "profile": 0,
            "note": t("Demo: Beispieldaten."),
            "profiles": [
                {"id": 1, "title": "0.2mm, 3 walls, 15% infill", "printer": "H2S", "compatible": ["H2S", "P1S", "X1C"], "default": True,
                 "plates": [{"name": t("Glocken"), "minutes": 220, "thumb": None,
                             "fil": [fil("PLA", "#F2D21B", 6.4), fil("PLA", "#D8322B", 7.5), fil("PLA", "#E8E8E8", 7.5), fil("PLA", "#2F9A3C", 112.0)]}]},
                {"id": 2, "title": "Set, 0.16mm", "printer": "P1S", "compatible": ["P1S", "H2S"], "default": False,
                 "plates": [{"name": t("Glocken"), "minutes": 260, "thumb": None, "fil": [fil("PLA", "#F2D21B", 6.6), fil("PLA", "#2F9A3C", 118.0)]},
                            {"name": t("Zweige"), "minutes": 95, "thumb": None, "fil": [fil("PLA", "#2F9A3C", 41.0)]},
                            {"name": t("Schleifen"), "minutes": 48, "thumb": None, "fil": [fil("PETG", "#D8322B", 15.0)]}]},
            ]}


# ---- Drucker erkennen -------------------------------------------------------------

def _norm(s: Any) -> str:
    n = re.sub(r"bambu\s*lab", "", str(s or "").lower())
    n = re.sub(r"[^a-z0-9]", "", n)
    return _ALIASES.get(n, n)


def match_printer(names: dict[str, str], product: str | None) -> str | None:
    """Kurzname des eigenen Druckers zu einem Bambu-Modellnamen (z. B. „Bambu Lab H3C“ → h3c)."""
    want = _norm(product)
    if not want:
        return None
    for key, name in names.items():
        if want in (_norm(name), _norm(key)):
            return key
    # Kurzname wie „a1m“ für „A1 mini“
    for key, name in names.items():
        if len(want) > 3 and _norm(key) and want.startswith(_norm(key)) and _norm(name).startswith(want[:2]):
            return key
    return None


# ---- Kennwerte aus den Einstellungen und bisherigen Drucken -------------------------

def params(store: Store) -> dict[str, Any]:
    """Alles, was die Rechnung braucht: Drucker (Ø-Leistung, Stundensatz), Strompreis, Filamentpreise, Margen."""
    from .pages import margins

    names = printer_names(store)
    cfgs = _pcfg.by_key(store)
    rates = store.printer_rates()
    c = store._conn
    hist = {r["printer"]: (r["kwh"], r["h"]) for r in c.execute(
        "SELECT printer, SUM(kwh) kwh, SUM(end_ts - start_ts) / 3600.0 h FROM jobs WHERE state = 'done' AND kind IS NULL "
        "AND kwh > 0 AND end_ts > start_ts + 600 GROUP BY printer")}
    tot_kwh = sum(v[0] for v in hist.values())
    tot_h = sum(v[1] for v in hist.values())
    avg_kw = tot_kwh / tot_h if tot_h > 0 else None
    r = c.execute("SELECT SUM(cost) cost, SUM(kwh) kwh FROM (SELECT cost, kwh FROM jobs WHERE state = 'done' AND kind IS NULL "
                  "AND kwh > 0.01 AND cost > 0 ORDER BY start_ts DESC LIMIT 200)").fetchone()
    kwh_price = (r["cost"] / r["kwh"]) if r and r["kwh"] else None
    printers = []
    for key, name in names.items():
        if key not in cfgs and key not in rates and key not in hist:
            continue
        kw, src = None, None
        kwh, h = hist.get(key, (0, 0))
        if h and h >= 1:
            kw, src = kwh / h, "hist"
        elif (cfgs.get(key) or {}).get("fixed_w"):
            kw, src = cfgs[key]["fixed_w"] / 1000, "fixed"
        elif avg_kw:
            kw, src = avg_kw, "avg"
        printers.append({"key": key, "name": name, "kw": round(kw, 4) if kw else None, "kw_src": src,
                         "rate": hourly_rate(rates.get(key))})
    prices = store.material_prices()
    types = sorted({*KNOWN_TYPES, *(k for k, v in prices.items() if v.get("kind") == "type")})
    fil = [{"key": tp, "label": tp, "type": tp, "price": (prices.get(tp) or {}).get("eur_per_kg")} for tp in types]
    for f in store.custom_filaments():
        tp = f.get("mtype") or guess_type(f["key"])
        own = f.get("eur_per_kg")
        fil.append({"key": f["key"], "label": f["key"], "type": tp, "custom": True,
                    "price": own if own is not None else (prices.get(tp or "") or {}).get("eur_per_kg")})
    ms = [{"key": f"m{i}", "label": f"{m[0]} +{m[1]:g} %", "pct": m[1], "work": m.work} for i, m in enumerate(margins(store), 1)]
    wd = store.settings().get("work_margin_default") != "0"
    for row in c.execute("SELECT id, name, margin_pct, work_margin FROM customers WHERE margin_pct IS NOT NULL ORDER BY name COLLATE NOCASE"):
        ms.append({"key": f"c{row['id']}", "label": f"{row['name']} +{row['margin_pct']:g} %", "pct": row["margin_pct"],
                   "work": bool(row["work_margin"]) if row["work_margin"] is not None else wd, "customer": row["name"]})
    wr, _dr = _extras.rates(store.settings())
    return {"printers": printers, "kwh_price": round(kwh_price, 4) if kwh_price else None, "filaments": fil,
            "margins": ms, "work_rate": wr}


def _fil_price(prm: dict[str, Any], key: Any, mtype: Any) -> float | None:
    by_key = {f["key"]: f for f in prm["filaments"]}
    for k in (key, mtype, guess_type(str(key or ""))):
        if k and k in by_key and by_key[k].get("price") is not None:
            return by_key[k]["price"]
    return None


def quote(store: Store, d: dict[str, Any], prm: dict[str, Any] | None = None) -> dict[str, Any]:
    """Kosten je Drucker und Preis für die Eingaben ``d`` (Platten, Sätze, Stück, Arbeit, Zubehör, Marge)."""
    prm = prm or params(store)
    plates = [p for p in (d.get("plates") or [])[:MAX_PLATES] if isinstance(p, dict) and p.get("on", True)]
    minutes = sum(_num(p.get("minutes")) or 0 for p in plates)
    grams, fil_cost, no_price = 0.0, 0.0, []
    for p in plates:
        for f in (p.get("fil") or [])[:MAX_FIL]:
            if not isinstance(f, dict):
                continue
            g = _num(f.get("grams")) or 0
            grams += g
            price = _fil_price(prm, f.get("key"), f.get("type"))
            if price is None:
                if g:
                    no_price.append(str(f.get("key") or f.get("type") or "?"))
            else:
                fil_cost += g / 1000 * price
    hours = minutes / 60
    kp = prm.get("kwh_price")
    compat = {_norm(x) for x in d.get("compatible") or [] if x}
    rows = []
    for pr in prm["printers"]:
        energy = pr["kw"] * hours * kp if pr.get("kw") and kp else None
        pcost = pr["rate"] * hours if pr.get("rate") else None
        total = fil_cost + (energy or 0) + (pcost or 0)
        rows.append({"key": pr["key"], "name": pr["name"], "minutes": minutes, "energy": energy, "printer": pcost,
                     "filament": fil_cost, "total": total, "kw_src": pr.get("kw_src"),
                     "compat": (_norm(pr["name"]) in compat or _norm(pr["key"]) in compat) if compat else None})
    detected = match_printer({r["key"]: r["name"] for r in rows}, d.get("hint"))
    want = d.get("printer") or detected
    sel = next((r for r in rows if r["key"] == want), rows[0] if rows else None)
    sets = max(1, min(int(_num(d.get("sets")) or 1), 9999))
    pieces = max(1, min(int(_num(d.get("pieces")) or 1), 9999))
    per_set = sel["total"] if sel else fil_cost
    extras = (_num(d.get("extras")) or 0) * sets
    cost = per_set * sets + extras
    work = (_num(d.get("work_min")) or 0) / 60 * (prm.get("work_rate") or 0) * sets
    m = next((x for x in prm["margins"] if x["key"] == d.get("margin")), None)
    price = _extras.sell_price(cost, work, m["pct"] if m else 0, bool(m and m["work"]))
    cheap = min((r for r in rows if r["compat"] is not False), key=lambda r: r["total"], default=None)
    return {"rows": rows, "sel": sel["key"] if sel else None, "detected": detected, "cheapest": cheap["key"] if cheap and len(rows) > 1 else None,
            "minutes": minutes, "grams": round(grams, 2), "filament": fil_cost, "no_price": sorted(set(no_price)),
            "n_plates": len(plates), "sets": sets, "pieces": pieces, "per_set": per_set, "extras": extras, "cost": cost,
            "work": work, "price": price, "margin": m["label"] if m else None,
            "per_piece": (price if price is not None else cost) / (sets * pieces),
            "kwh_price": kp, "work_rate": prm.get("work_rate")}


def item_fields(d: dict[str, Any], q: dict[str, Any]) -> dict[str, Any]:
    """Felder einer Freitext-Position aus einer Kalkulation: Platten, Stück, Druckzeit und Kosten pro Platte."""
    n = max(1, q["n_plates"])
    one = n == 1
    return {"name": str(d.get("title") or "").strip()[:120] or t("Position"),
            "url": str(d.get("url") or "").strip()[:500] or None,
            "qty": q["sets"] * (q["pieces"] if one else 1),
            "n_plates": n, "per_plate": q["pieces"] if one else None,
            "est_min": round(q["minutes"] / n, 1) if q["minutes"] else None,
            "est_g": round(q["grams"] / n, 1) if q["grams"] else None,
            "est_cost": round((q["per_set"] + q["extras"] / q["sets"]) / n, 4)}


def plate_names(d: dict[str, Any]) -> list[dict[str, Any]]:
    """Namen der mitgerechneten Platten als Zeilen für plates.save_plates (Nummer = Reihenfolge)."""
    on = [p for p in (d.get("plates") or [])[:MAX_PLATES] if isinstance(p, dict) and p.get("on", True)]
    if len(on) < 2:
        return []
    return [{"plate": n, "name": str(p.get("name") or "").strip()[:80]} for n, p in enumerate(on, 1) if str(p.get("name") or "").strip()]


# ---- Seite -------------------------------------------------------------------------

def page_calc(store: Store, order_id: int | None = None) -> str:
    from . import orders as od

    prm = params(store)
    opens = [o for o in od.all_orders(store) if o["state"] in ("open", "running")]
    opts = "".join(f'<option value="{o["id"]}"{" selected" if o["id"] == order_id else ""}>'
                   f'{e(o.get("title") or t("Auftrag"))}{" · " + e(o["customer_name"]) if o.get("customer_name") else ""}</option>'
                   for o in opens)
    into = (f'<span class="kinto"><select name="order" aria-label="{e(t("Auftrag"))}"><option value="">{e(t("In bestehenden Auftrag …"))}</option>{opts}</select>'
            f'<button class="btn" name="do" value="into" data-need="order">{e(t("Hinzufügen"))}</button></span>') if opens else ""
    acts = (f'<form method="post" action="calc/order" class="kacts" data-kc-form>'
            f'<input type="hidden" name="data">{into}'
            f'<button class="btn primary" name="do" value="new">＋ {e(t("Als neuen Auftrag anlegen"))}</button></form>')
    body = (f'<a class="back" href="orders">← {e(t("Aufträge"))}</a>'
            f'<h2>🧮 {e(t("Kosten vorab kalkulieren"))}</h2>'
            f'<p class="muted kintro">{e(t("MakerWorld-Link einfügen oder Werte von Hand eintragen. printlog rechnet mit deinen Einstellungen und vergleicht die Drucker."))}</p>'
            f'{widget(prm)}{acts}')
    return layout(t("Kalkulieren"), "orders", f'<style>{CSS}</style>{body}', CALC_JS)


def dialog(store: Store) -> str:
    """Dialog „Kalkulieren“ für das Auftragsformular und die Auftragsseite."""
    return (f'<dialog class="kdlg" id="kdlg"><div class="kdh"><b>🧮 {e(t("Kosten vorab kalkulieren"))}</b>'
            f'<button type="button" class="btn sm kclose">{e(t("Schließen"))}</button></div>'
            f'<div class="kdb">{widget(params(store), True)}</div>'
            f'<div class="kdf"><button type="button" class="btn kclose">{e(t("Abbrechen"))}</button>'
            f'<button type="button" class="btn primary ktake" disabled>{e(t("Übernehmen"))}</button></div></dialog>')


def widget(prm: dict[str, Any], dialog: bool = False) -> str:
    """Rechner-Baustein (Seite und Dialog im Auftragsformular). Die Kennwerte stehen als JSON im Attribut."""
    return (f'<div class="kc{" kdlgw" if dialog else ""}" data-kc=\'{e(json.dumps(_public(prm), ensure_ascii=False))}\'>'
            f'<div class="card ksrc"><div class="kurl"><input type="url" class="kurlin" placeholder="https://makerworld.com/…" '
            f'aria-label="{e(t("MakerWorld-Link"))}"><button type="button" class="btn primary kfetch">{e(t("Auslesen"))}</button>'
            f'<button type="button" class="btn khand">✏️ {e(t("Von Hand"))}</button></div>'
            f'<div class="kmsg muted"></div></div>'
            f'<div class="kbody" hidden></div></div>')


def _public(prm: dict[str, Any]) -> dict[str, Any]:
    return {"printers": [{"key": p["key"], "name": p["name"]} for p in prm["printers"]],
            "filaments": [{"key": f["key"], "label": f["label"], "type": f["type"], "price": f["price"]} for f in prm["filaments"]],
            "margins": [{"key": m["key"], "label": m["label"], "customer": m.get("customer")} for m in prm["margins"]]}


CSS = """
.kintro{margin:-4px 0 14px}
.ksrc{padding:14px 16px;margin-bottom:14px}.kurl{display:flex;gap:8px;flex-wrap:wrap}.kurl .kurlin{flex:1 1 260px;min-width:0}
.kmsg{margin-top:8px;font-size:14px}.kmsg.bad{color:var(--bad)}.kmsg.ok{color:var(--ok)}.kmsg:empty{display:none}
.kgrid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.25fr);gap:14px;align-items:start}
.kc .card{padding:14px 16px}.kc h4{margin:0 0 10px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.khead{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.khead .ktitle{flex:1 1 240px;font-weight:600}
.khead select{max-width:100%}
.kplate{border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin-bottom:10px}
.kplate.off{opacity:.55}.kph{display:flex;gap:10px;align-items:center;margin-bottom:8px}
.kph img{width:44px;height:44px;border-radius:8px;border:1px solid var(--line);object-fit:cover;background:#22252a}
.kph .kpn{flex:1;min-width:0;font-weight:600}.kph .kpn input{width:100%}.kph .kmin{width:96px;text-align:right}
.kfil{display:grid;grid-template-columns:30px minmax(0,1fr) 84px 28px;gap:6px;align-items:center;margin-top:6px}
.kfil input[type=color]{width:30px;height:30px;padding:0;border:1px solid var(--line);border-radius:7px;background:none}
.kfil .kg{text-align:right}.kfil select{min-width:0}
.kx{border:0;background:none;color:var(--muted);font-size:16px;cursor:pointer;min-height:0;padding:2px}
.kadd{border:0;background:none;color:var(--accent);font-weight:600;cursor:pointer;padding:6px 0;min-height:0}
.ksum{display:flex;justify-content:space-between;font-size:14px;color:var(--muted);margin-top:4px}
table.kcmp{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
table.kcmp th{font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em;text-align:right;padding:6px;border-bottom:1px solid var(--line)}
table.kcmp td{padding:8px 6px;text-align:right;border-bottom:1px solid color-mix(in srgb,var(--line) 60%,transparent);cursor:pointer}
table.kcmp th:first-child,table.kcmp td:first-child{text-align:left}table.kcmp td:not(:first-child){white-space:nowrap}
table.kcmp tr.on td{background:color-mix(in srgb,var(--accent) 13%,transparent)}table.kcmp tr.on td:first-child{box-shadow:inset 3px 0 var(--accent)}
table.kcmp tr.nc td{color:var(--muted)}
.kpill{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--muted);margin-left:5px;font-weight:500;white-space:nowrap}
.kpill.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 45%,var(--line))}
.kopts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:14px}
.kopts label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted)}.kopts input,.kopts select{min-width:0}
.ktot{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:14px}
.ktot>div{border:1px solid var(--line);border-radius:10px;padding:10px 12px}.ktot span{display:block;font-size:13px;color:var(--muted)}
.ktot b{font-size:21px;font-variant-numeric:tabular-nums}.ktot small{display:block;font-size:12px;color:var(--muted)}.ktot .acc b{color:var(--accent)}
.khint{font-size:13px;color:var(--muted);margin-top:10px;line-height:1.4}.khint.warn{color:var(--warn)}
.kacts{display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap;margin-top:14px}.kinto{display:flex;gap:8px;flex-wrap:wrap}
.kacts[hidden]{display:none}
dialog.kdlg{width:min(1100px,96vw);max-height:92vh;padding:0;border:1px solid var(--line);border-radius:16px;background:var(--bg);color:var(--fg)}
dialog.kdlg::backdrop{background:#0008}.kdlg .kdh{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:2}
.kdlg .kdh b{flex:1;font-size:17px}.kdlg .kdb{padding:14px 16px}
.kdlg .kdf{display:flex;justify-content:flex-end;gap:10px;padding:12px 16px;border-top:1px solid var(--line);position:sticky;bottom:0;background:var(--bg)}
.kcalc{margin:8px 0 2px}
@media (max-width:860px){.kgrid{grid-template-columns:minmax(0,1fr)}.kopts{grid-template-columns:repeat(2,minmax(0,1fr))}.ktot{grid-template-columns:repeat(2,minmax(0,1fr))}
 table.kcmp .hm{display:none}}
"""

# Rechner im Browser: Eingaben bearbeiten, Server rechnet (api/calc/quote), Ergebnis anzeigen.
CALC_JS = r"""
window.PLCalc=(()=>{const lang=document.documentElement.lang||'de';
const eur=v=>v==null?'–':v.toLocaleString(lang,{style:'currency',currency:'EUR'});
const num=(v,d)=>v==null?'–':v.toLocaleString(lang,{minimumFractionDigits:d,maximumFractionDigits:d});
const hm=m=>{if(!m)return '';m=Math.round(m);return Math.floor(m/60)+':'+String(m%60).padStart(2,'0')};
const pmin=v=>{v=String(v||'').trim().toLowerCase().replace(',','.');if(!v)return null;let m=v.match(/^(\d+):(\d{1,2})\s*h?$/);if(m)return +m[1]*60+ +m[2];
 m=v.match(/^(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s)?$/);if(m&&(m[1]||m[2]))return (+m[1]||0)*60+(+m[2]||0)+(+m[3]||0)/60;
 m=v.match(/^(\d+(?:\.\d+)?)\s*(h|std)?$/);return m?parseFloat(m[1])*60:null};
const pnum=v=>{v=String(v||'').trim().replace(/[€\sg]/g,'');if(!v)return null;if(lang!=='en')v=v.replace(',','.');const n=parseFloat(v);return isNaN(n)||n<0?null:n};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function init(root,opt){opt=opt||{};const P=JSON.parse(root.dataset.kc),body=root.querySelector('.kbody'),msg=root.querySelector('.kmsg'),urlIn=root.querySelector('.kurlin');
 let S=null,Q=null,timer=null,seq=0;
 const say=(t,cls)=>{msg.textContent=t||'';msg.className='kmsg '+(cls||'muted')};
 const filKey=f=>{if(f.key)return f.key;const ts=P.filaments.map(x=>x.key);const t=(f.type||'').toUpperCase();return ts.includes(t)?t:(ts.includes('PLA')?'PLA':ts[0])};
 const blank=()=>({name:'',minutes:null,thumb:null,on:true,fil:[{type:'PLA',key:'PLA',color:'#888888',grams:null}]});
 function start(d){const prof=d.profiles?d.profiles[d.profile||0]:null;
  S={title:d.title||'',url:d.url||'',profiles:d.profiles||null,profile:d.profile||0,printer:null,sets:S?S.sets:1,pieces:S?S.pieces:1,
   work_min:S?S.work_min:'',extras:S?S.extras:'',margin:S?S.margin:(opt.margin||(P.margins[0]||{}).key||''),
   hint:d.printer||null,compatible:d.compatible||null,plates:[],source:d.source||'hand',ocr:d.ocr||null};
  if(prof)useProfile(S.profile);else{S.plates=(d.plates||[blank()]).map(p=>({...p,on:true,fil:(p.fil||[]).map(f=>({...f,key:filKey(f)}))}))}
  if(opt.customer){const m=P.margins.find(x=>x.customer&&x.customer.toLowerCase()===opt.customer.toLowerCase());if(m)S.margin=m.key}
  body.hidden=false;render();calc()}
 function useProfile(i){const p=S.profiles[i];S.profile=i;S.hint=p.printer;S.compatible=p.compatible;S.printer=null;
  S.plates=p.plates.map(x=>({...x,on:true,fil:x.fil.map(f=>({...f,key:filKey(f)}))}))}
 function filOpts(sel){return P.filaments.map(f=>`<option value="${esc(f.key)}"${f.key===sel?' selected':''}>${esc(f.label)}${f.price==null?' · '+esc(_t("kein Preis")):''}</option>`).join('')}
 function render(){const prof=S.profiles;
  const pls=S.plates.map((p,i)=>`<div class="kplate${p.on?'':' off'}" data-i="${i}"><div class="kph">
   ${S.plates.length>1?`<input type="checkbox" class="kon"${p.on?' checked':''} aria-label="${esc(_t("Platte mitrechnen"))}">`:''}
   ${p.thumb?`<img src="${esc(p.thumb)}" alt="" loading="lazy" referrerpolicy="no-referrer">`:''}
   <span class="kpn"><input class="kpname" value="${esc(p.name)}" placeholder="${esc(_t("Platte")+' '+(i+1))}"></span>
   <input class="kmin" value="${esc(hm(p.minutes))}" placeholder="${esc(_t("h:mm"))}" aria-label="${esc(_t("Druckzeit"))}">
   ${S.plates.length>1?`<button type="button" class="kx kdelp" title="${esc(_t("Platte entfernen"))}">✕</button>`:''}</div>
   ${p.fil.map((f,j)=>`<div class="kfil" data-j="${j}"><input type="color" class="kcol" value="${esc(f.color||'#888888')}" aria-label="${esc(_t("Farbe"))}">
    <select class="kfk">${filOpts(f.key)}</select><input class="kg" inputmode="decimal" value="${f.grams==null?'':esc(num(f.grams,f.grams%1?1:0).replace(/\s/g,''))}" placeholder="g" aria-label="${esc(_t("Gramm"))}">
    <button type="button" class="kx kdelf" title="${esc(_t("Filament entfernen"))}">✕</button></div>`).join('')}
   <button type="button" class="kadd kaddf">＋ ${esc(_t("Filament"))}</button></div>`).join('');
  body.innerHTML=`<div class="khead"><input class="ktitle" value="${esc(S.title)}" placeholder="${esc(_t("Titel"))}" aria-label="${esc(_t("Titel"))}">
   ${prof&&prof.length>1?`<select class="kprof" aria-label="${esc(_t("Druckprofil"))}">${prof.map((p,i)=>`<option value="${i}"${i===S.profile?' selected':''}>${esc(p.title||(_t("Profil")+' '+(i+1)))}${p.printer?' · '+esc(p.printer):''}</option>`).join('')}</select>`:''}</div>
   <div class="kgrid"><div class="card"><h4>${esc(_t("Platten und Filament"))}</h4>${pls}<button type="button" class="kadd kaddp">＋ ${esc(_t("Platte"))}</button>
    <div class="ksum"><span class="kst"></span></div></div>
   <div class="card"><h4>${esc(_t("Drucker vergleichen"))}</h4><table class="kcmp"><thead><tr><th>${esc(_t("Drucker"))}</th><th>${esc(_t("Zeit"))}</th>
    <th class="hm">${esc(_t("Strom"))}</th><th class="hm">${esc(_t("Drucker"))}</th><th class="hm">${esc(_t("Filament"))}</th><th>${esc(_t("Summe"))}</th></tr></thead><tbody></tbody></table>
    <div class="khint kexpl"></div>
    <div class="kopts"><label>${esc(_t("Sätze"))}<input class="ksets" inputmode="numeric" value="${S.sets}"></label>
     <label>${esc(_t("Stück pro Satz"))}<input class="kpcs" inputmode="numeric" value="${S.pieces}"></label>
     <label>${esc(_t("Arbeit pro Satz (min)"))}<input class="kwork" inputmode="decimal" value="${esc(S.work_min)}" placeholder="0"></label>
     <label>${esc(_t("Zubehör pro Satz (€)"))}<input class="kext" inputmode="decimal" value="${esc(S.extras)}" placeholder="0"></label></div>
    <div class="kopts" style="grid-template-columns:1fr"><label>${esc(_t("Marge"))}<select class="kmar"><option value="">${esc(_t("ohne Marge"))}</option>
     ${P.margins.map(m=>`<option value="${esc(m.key)}"${m.key===S.margin?' selected':''}>${esc(m.label)}</option>`).join('')}</select></label></div>
    <div class="ktot"></div><div class="khint kwarn warn"></div></div></div>`;
  bind()}
 function bind(){const q=s=>body.querySelector(s);
  q('.ktitle').oninput=ev=>{S.title=ev.target.value};
  const pr=q('.kprof');if(pr)pr.onchange=ev=>{useProfile(+ev.target.value);render();calc()};
  body.querySelectorAll('.kplate').forEach(el=>{const p=S.plates[+el.dataset.i];
   const on=el.querySelector('.kon');if(on)on.onchange=()=>{p.on=on.checked;el.classList.toggle('off',!p.on);calc()};
   el.querySelector('.kpname').oninput=ev=>{p.name=ev.target.value};
   el.querySelector('.kmin').onchange=ev=>{p.minutes=pmin(ev.target.value);ev.target.value=hm(p.minutes);calc()};
   const dp=el.querySelector('.kdelp');if(dp)dp.onclick=()=>{S.plates.splice(+el.dataset.i,1);render();calc()};
   el.querySelector('.kaddf').onclick=()=>{p.fil.push({type:'PLA',key:'PLA',color:'#888888',grams:null});render()};
   el.querySelectorAll('.kfil').forEach(r=>{const f=p.fil[+r.dataset.j];
    r.querySelector('.kcol').oninput=ev=>{f.color=ev.target.value};
    r.querySelector('.kfk').onchange=ev=>{f.key=ev.target.value;const x=P.filaments.find(y=>y.key===f.key);f.type=x?x.type:null;calc()};
    r.querySelector('.kg').onchange=ev=>{f.grams=pnum(ev.target.value);calc()};
    r.querySelector('.kdelf').onclick=()=>{p.fil.splice(+r.dataset.j,1);render();calc()}})});
  q('.kaddp').onclick=()=>{S.plates.push(blank());render();calc()};
  const nb=(sel,k,int)=>{q(sel).onchange=ev=>{let v=int?Math.max(1,parseInt(ev.target.value)||1):ev.target.value;if(int)ev.target.value=v;S[k]=v;calc()}};
  nb('.ksets','sets',1);nb('.kpcs','pieces',1);nb('.kwork','work_min');nb('.kext','extras');
  q('.kmar').onchange=ev=>{S.margin=ev.target.value;calc()}}
 function payload(){return {title:S.title,url:S.url,printer:S.printer,compatible:S.compatible,sets:S.sets,pieces:S.pieces,
  work_min:pnum(S.work_min),extras:pnum(S.extras),margin:S.margin,plates:S.plates.map(p=>({name:p.name,minutes:p.minutes,on:p.on,
  fil:p.fil.map(f=>({key:f.key,type:f.type,color:f.color,grams:f.grams}))}))}}
 function calc(){clearTimeout(timer);timer=setTimeout(async()=>{const my=++seq;
  try{const r=await fetch(opt.base+'api/calc/quote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload(),hint:S.hint})});
   const d=await r.json();if(my!==seq)return;Q=d;if(!S.printer&&d.sel)S.printer=d.sel;show()}catch(_){}} ,180)}
 function show(){const tb=body.querySelector('.kcmp tbody');if(!tb||!Q)return;
  tb.innerHTML=Q.rows.length?Q.rows.map(r=>`<tr data-k="${esc(r.key)}" class="${r.key===S.printer?'on':''}${r.compat===false?' nc':''}">
   <td><b>${esc(r.name)}</b>${r.key===Q.detected?` <span class="kpill ok">${esc(_t("erkannt"))}</span>`:''}${r.key===Q.cheapest?` <span class="kpill">${esc(_t("günstigster"))}</span>`:''}${r.compat===false?` <span class="kpill">${esc(_t("laut MakerWorld nicht kompatibel"))}</span>`:''}</td>
   <td>${r.minutes?(r.key===Q.detected||!Q.detected?'':'~')+esc(hm(r.minutes)+' h'):'–'}</td><td class="hm">${eur(r.energy)}</td><td class="hm">${eur(r.printer)}</td><td class="hm">${eur(r.filament)}</td><td><b>${eur(r.total)}</b></td></tr>`).join('')
   :`<tr><td colspan="6" class="muted">${esc(_t("Keine Drucker eingerichtet. Es zählt nur das Filament."))}</td></tr>`;
  tb.querySelectorAll('tr[data-k]').forEach(tr=>tr.onclick=()=>{S.printer=tr.dataset.k;calc()});
  body.querySelector('.kst').textContent=`${Q.n_plates} ${Q.n_plates===1?_t("Platte"):_t("Platten")} · ${hm(Q.minutes)||'–'} h · ${num(Q.grams,1)} g · ${eur(Q.filament)} ${_t("Filament")}`;
  body.querySelector('.kexpl').textContent=_t("Strom = Ø-Verbrauch des Druckers aus deinen bisherigen Drucken × Zeit × Ø-Strompreis. Drucker = Abschreibung + Wartung aus den Einstellungen. Andere Drucker: gleiche Zeit angenommen (~). Tippe auf einen Drucker, um mit ihm zu rechnen.");
  const per=Q.sets*Q.pieces;
  body.querySelector('.ktot').innerHTML=`<div><span>${esc(_t("Kosten"))}</span><b>${eur(Q.cost)}</b><small>${Q.sets>1?esc(eur(Q.per_set)+' / '+_t("Satz")):(Q.extras?esc(_t("inkl. Zubehör")):'&nbsp;')}</small></div>
   <div><span>${esc(_t("Arbeit"))}</span><b>${eur(Q.work)}</b><small>${Q.work_rate?esc(eur(Q.work_rate)+' / h'):esc(_t("kein Stundensatz"))}</small></div>
   <div class="acc"><span>${esc(Q.margin?_t("Preis mit Marge"):_t("Preis ohne Marge"))}</span><b>${eur(Q.price)}</b><small>${per>1?esc(eur(Q.per_piece)+' / '+_t("Stk.")):'&nbsp;'}</small></div>`;
  const w=[];if(Q.no_price.length)w.push(_t("Kein Preis für:")+' '+Q.no_price.join(', '));if(!Q.kwh_price)w.push(_t("Noch kein Strompreis aus bisherigen Drucken, Strom fehlt."));
  body.querySelector('.kwarn').textContent=w.join(' ');
  if(opt.onQuote)opt.onQuote(Q,S)}
 async function fetchLink(u){u=(u||'').trim();if(!u){urlIn.focus();return}say(_t("Lese MakerWorld …"));
  try{const r=await fetch(opt.base+'api/calc/link',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u})});const d=await r.json();
   if(d.error){say(d.error,'bad');if(!S)start({url:u,source:'hand'});else S.url=u;return}
   say((d.note?d.note+' ':'')+'✓ '+(d.title||'')+(d.profiles.length>1?' · '+d.profiles.length+' '+_t("Druckprofile"):''),'ok');start(d)}
  catch(_){say(_t("MakerWorld ist gerade nicht erreichbar. Trag die Werte von Hand ein."),'bad')}}
 root.querySelector('.kfetch').onclick=()=>fetchLink(urlIn.value);
 urlIn.onkeydown=ev=>{if(ev.key==='Enter'){ev.preventDefault();fetchLink(urlIn.value)}};
 root.querySelector('.khand').onclick=()=>{say('');start({url:urlIn.value.trim(),source:'hand'})};
 return {fetchLink,start,state:()=>S,quote:()=>Q,payload,setCustomer:c=>{opt.customer=c},el:root}}
return {init,hm}})();
(()=>{const root=document.querySelector('.kc:not(.kdlgw)');if(!root)return;const base=document.baseURI.replace(/[^/]*$/,'');
 const f=document.querySelector('form[data-kc-form]');f.hidden=true;
 const C=PLCalc.init(root,{base,onQuote:()=>{f.hidden=false}});
 f.onsubmit=ev=>{const b=ev.submitter;if(b&&b.dataset.need==='order'&&!f.order.value){ev.preventDefault();f.order.focus();return}
  f.data.value=JSON.stringify({...C.payload(),hint:C.state().hint})};
 const u=new URLSearchParams(location.search).get('url');if(u){root.querySelector('.kurlin').value=u;C.fetchLink(u)}})();
"""

# Dialog im Auftragsformular: „fill“ trägt die Werte in die Felder der Position ein, „add“ legt auf der
# Auftragsseite gleich eine Position an.
DIALOG_JS = r"""
(()=>{const dlg=document.getElementById('kdlg');if(!dlg)return;const base=document.baseURI.replace(/[^/]*$/,'');
 const take=dlg.querySelector('.ktake'),lang=document.documentElement.lang||'de';let C=null,btn=null;
 const close=()=>dlg.close();dlg.querySelectorAll('.kclose').forEach(b=>b.onclick=close);bgClose(dlg,close);
 const custIn=()=>document.querySelector('input[name=customer]');
 function open(b){btn=b;const f=b.closest('form'),ci=custIn();
  if(!C)C=PLCalc.init(dlg.querySelector('.kc'),{base,customer:ci?ci.value:'',onQuote:()=>{take.disabled=false}});
  else C.setCustomer(ci?ci.value:'');
  const u=f&&f.url?f.url.value.trim():'',ui=dlg.querySelector('.kurlin');
  if(u&&ui.value!==u){ui.value=u;if(/makerworld\.com/i.test(u))C.fetchLink(u)}
  dlg.showModal()}
 document.querySelectorAll('.kcalc').forEach(b=>b.onclick=()=>open(b));
 take.onclick=()=>{const Q=C.quote(),S=C.state();if(!Q||!btn)return;
  if(btn.dataset.order){const f=document.createElement('form');f.method='post';f.action='calc/order';f.hidden=true;
   f.innerHTML='<input name="do" value="into"><input name="order"><input name="data">';f.order.value=btn.dataset.order;
   f.data.value=JSON.stringify({...C.payload(),hint:S.hint});document.body.appendChild(f);f.submit();return}
  const f=btn.closest('form'),n=Q.n_plates||1,one=n===1;
  const set=(k,v,empty)=>{const i=f.querySelector(`[name="${k}"]`);if(i&&v!=null&&(!empty||!i.value.trim()))i.value=v};
  set('item',S.title,1);set('name',S.title,1);set('url',S.url,1);
  set('qty',Q.sets*(one?Q.pieces:1));set('n_plates',n);set('per_plate',one?Q.pieces:'');
  set('est',PLCalc.hm(Q.minutes/n));set('est_cost',((Q.per_set+Q.extras/Q.sets)/n).toFixed(4));
  if(Q.price!=null){let v=Q.price.toFixed(2);if(lang!=='en')v=v.replace('.',',');set('agreed',v,1)}
  const d=f.querySelector('details.omore');if(d)d.open=true;close()}})();
"""
