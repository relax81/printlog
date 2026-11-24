"""Demo-Modus: dieselbe Oberfläche unter ``/demo/`` mit erfundenen Beispieldaten.

Gedacht für Screenshots und zum Ausprobieren. Die Demo hat eine eigene Datenbank
und einen eigenen Bilderordner (``DATA_DIR/demo``). Sie kennt die echte Datenbank
nur, um beim ersten Öffnen die Sprache zu übernehmen, und schreibt nie hinein.
Home Assistant wird nicht angesprochen: keine Events, kein Bildabruf, kein Backup.

Die Daten werden relativ zu „jetzt“ erzeugt (ein Jahr, laufende Drucke
inklusive) und spätestens nach 12 Stunden, nach einem Neustart oder beim
Sprachwechsel neu angelegt, damit sie immer aktuell aussehen.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

from . import printers as pcfg
from .costing import compute
from .db import Store
from .demo_images import cover, photo
from .i18n import resolve
from .ui import DEMO

log = logging.getLogger("printlog.demo")

DAYS = 365               # ein Jahr Historie, damit die Statistik (letzte 12 Monate) gefüllt ist
MAX_AGE_S = 12 * 3600    # danach frisch erzeugen (laufende Drucke sollen „jetzt“ laufen)
SEED = 4711              # gleiche Zahlen bei jedem Erzeugen, nur die Daten wandern mit

# ---- Stammdaten --------------------------------------------------------------

# Leistung in W je Material (Durchschnitt über den Druck), Abschreibung/Wartung, ab wann im Einsatz
PRINTERS = [
    {"key": "a1mini", "name": "A1 mini", "watts": {"PLA": 72, "PETG": 92, "TPU": 70}, "max_g": 80, "per_day": 0.45,
     "since": DAYS, "rate": {"purchase_eur": 219, "life_hours": 3000, "maint_per_year": 30, "hours_per_year": 450}},
    {"key": "p1s", "name": "P1S", "watts": {"PLA": 118, "PETG": 158, "ASA": 176, "TPU": 110}, "max_g": 260, "per_day": 0.62,
     "since": 300, "rate": {"purchase_eur": 599, "life_hours": 4000, "maint_per_year": 50, "hours_per_year": 700}},
    {"key": "h2s", "name": "H2S", "watts": {"PLA": 146, "PETG": 196, "ASA": 218, "TPU": 140}, "max_g": 420, "per_day": 0.7,
     "since": 72, "rate": {"purchase_eur": 1399, "life_hours": 5000, "maint_per_year": 90, "hours_per_year": 900}},
]

# Sorte → Farben (Hex wie aus Home Assistant, Farbname für Notizen)
FILAMENTS: dict[str, list[tuple[str, float, list[tuple[str, str]]]]] = {
    "PLA": [
        ("Bambu PLA Basic", 0.58, [("#FFFFFF", "Jade White"), ("#000000", "Black"), ("#C12E1F", "Red"), ("#00AE42", "Bambu Green"),
                                   ("#0A2989", "Blue"), ("#FF6A13", "Orange"), ("#F4EE2A", "Yellow"), ("#8E9089", "Gray")]),
        ("Bambu PLA Matte", 0.36, [("#E8E3D6", "Ivory White"), ("#3E3E3E", "Charcoal"), ("#A25A37", "Terracotta"),
                                   ("#E8AFCF", "Sakura Pink"), ("#A3D8E1", "Ice Blue"), ("#68724D", "Dark Green"),
                                   ("#F7D959", "Lemon Yellow")]),
        ("Bambu PLA Silk+", 0.06, [("#F4A925", "Gold"), ("#C8C8C8", "Silver")]),
    ],
    "PETG": [("Bambu PETG HF", 1.0, [("#000000", "Black"), ("#FFFFFF", "White"), ("#C12E1F", "Red"), ("#0F5FA6", "Blue"),
                                     ("#5C6670", "Gray")])],
    "TPU": [("Bambu TPU 95A HF", 1.0, [("#101010", "Black"), ("#5B6670", "Gray"), ("#E0E0E0", "White")])],
    "ASA": [("Bambu ASA", 1.0, [("#000000", "Black"), ("#FFFFFF", "White")])],
}
TYPE_PRICES = {"PLA": 19.99, "PETG": 21.99, "TPU": 34.99, "ASA": 24.99}
NAME_PRICES = {"Bambu PLA Matte": 22.99, "Bambu PLA Silk+": 26.99, "Bambu TPU 95A HF": 41.99}

# Name DE, Name EN, Form, Gramm/Stück, Stunden/Stück, Materialtypen, Stückzahlen, Modelltitel (für Projektlink), Saison
OBJECTS: list[tuple[Any, ...]] = [
    ("Kabelhalter Schreibtisch", "Desk cable holder", "hook", (12, 28), (0.7, 1.4), "PLA", (1, 4, 6), "Under-Desk Cable Holder", ""),
    ("Gridfinity Box 2x3", "Gridfinity bin 2x3", "box", (45, 95), (2.2, 4.2), "PLA", (1, 2, 4), "Gridfinity Bin 2x3 Stackable", ""),
    ("Gridfinity Grundplatte 4x4", "Gridfinity baseplate 4x4", "plate", (70, 120), (2.8, 4.6), "PLA", (1,), "Gridfinity Baseplate 4x4", ""),
    ("Spiralvase", "Spiral vase", "vase", (35, 85), (1.6, 3.4), "PLA|PETG", (1,), "Twisted Spiral Vase", ""),
    ("Pflanztopf mit Untersetzer", "Planter with saucer", "pot", (90, 190), (4.5, 8.5), "PETG|PLA", (1, 2), "Self-Watering Planter", ""),
    ("Untersetzer-Set", "Coaster set", "coaster", (8, 14), (0.4, 0.7), "PLA|TPU", (4, 6, 8), "Hexagon Coaster Set", ""),
    ("Schlüsselanhänger mit Namen", "Name keychains", "tag", (3, 7), (0.3, 0.8), "PLA", (6, 10, 12, 20), "Custom Name Keychain", ""),
    ("Handyständer", "Phone stand", "stand", (40, 80), (2.0, 3.6), "PLA|PETG", (1, 2), "Minimal Phone Stand", ""),
    ("Zahnradsatz Getriebe", "Gearbox gear set", "gear", (30, 90), (1.6, 3.2), "PETG|PLA", (1, 3), "Planetary Gear Set", ""),
    ("Rolle Geschirrspüler", "Dishwasher rack wheel", "cyl", (6, 14), (0.5, 1.1), "PETG", (2, 4, 8), None, ""),
    ("Wandhaken", "Wall hooks", "hook", (10, 24), (0.6, 1.3), "PETG|PLA", (2, 4, 6), "Strong Wall Hook", ""),
    ("Kopfhörerhalter", "Headphone stand", "stand", (70, 130), (3.2, 5.4), "PLA", (1,), "Headphone Stand", ""),
    ("Drache artikuliert", "Articulated dragon", "dragon", (40, 110), (3.0, 6.5), "PLA", (1, 2), "Articulated Crystal Dragon", ""),
    ("Benchy", "Benchy", "boat", (12, 16), (0.7, 1.3), "PLA", (1,), "3DBenchy", ""),
    ("Tischkarten Hochzeit", "Wedding place cards", "tag", (4, 8), (0.3, 0.6), "PLA", (12, 20, 30), None, ""),
    ("Ausstechform Stern", "Star cookie cutter", "cutter", (8, 16), (0.5, 1.0), "PLA", (2, 3, 4), "Star Cookie Cutter", "xmas"),
    ("Schubladen-Organizer", "Drawer organizer", "box", (80, 160), (4.0, 7.5), "PLA", (1, 2), "Modular Drawer Organizer", ""),
    ("Kabelkanal-Clips", "Cable duct clips", "hook", (2, 5), (0.2, 0.4), "PETG", (10, 20), None, ""),
    ("Fahrradlampen-Halter", "Bike light mount", "hook", (15, 30), (1.0, 2.0), "PETG|ASA", (1, 2), "Bike Light Mount", ""),
    ("Handyhülle", "Phone case", "box", (18, 30), (1.4, 2.6), "TPU", (1,), None, ""),
    ("Gerätefüße Dämpfer", "Anti-vibration feet", "cyl", (4, 7), (0.3, 0.5), "TPU", (4, 8), "Anti-Vibration Feet", ""),
    ("Lampenschirm Voronoi", "Voronoi lamp shade", "lamp", (110, 220), (6.0, 11.5), "PLA", (1,), "Voronoi Lamp Shade", ""),
    ("Lochwand-Werkzeughalter", "Pegboard tool holder", "hook", (20, 45), (1.0, 2.2), "PETG|PLA", (2, 4, 6), "Pegboard Tool Holder", ""),
    ("Vereinsschild mit Logo", "Club sign with logo", "plate", (60, 140), (3.0, 6.0), "PLA", (1, 2), None, ""),
    ("Medaillenhalter", "Medal hanger", "plate", (80, 140), (3.0, 5.2), "PLA", (1,), "Medal Hanger", ""),
    ("Zahnbürstenhalter", "Toothbrush holder", "pot", (40, 70), (2.0, 3.6), "PETG", (1,), "Toothbrush Holder", ""),
    ("Kalibrierwürfel", "Calibration cube", "cube", (6, 10), (0.4, 0.6), "PLA|PETG", (1,), "XYZ Calibration Cube", ""),
    ("Ersatzknopf Herd", "Stove knob", "cyl", (8, 15), (0.6, 1.1), "PETG", (1, 2), None, ""),
    ("Weihnachtsstern", "Christmas star", "star", (10, 25), (0.8, 1.6), "PLA", (4, 6, 8), "Christmas Star Ornament", "xmas"),
    ("Bilderrahmen", "Picture frame", "frame", (40, 80), (2.0, 3.6), "PLA", (1, 2), "Simple Picture Frame", ""),
    ("Becheruntersetzer Café", "Café cup coasters", "coaster", (6, 10), (0.4, 0.6), "PETG", (6, 8, 12), None, ""),
    ("Menükartenhalter", "Menu card holder", "stand", (25, 45), (1.2, 2.2), "PLA", (6, 8, 10), None, ""),
]
_IDX = {o[0]: i for i, o in enumerate(OBJECTS)}

# Kunden: Name DE/EN, Marge, keine Abrechnung, Kontakt, Notiz DE/EN, Gewicht, bevorzugte Objekte
CUSTOMERS: list[dict[str, Any]] = [
    {"de": "Anna Becker", "margin": None, "contact": "anna.becker@example.com", "w": 3,
     "pref": ["Spiralvase", "Bilderrahmen", "Pflanztopf mit Untersetzer", "Drache artikuliert"]},
    {"de": "Tom Wagner", "margin": 20, "contact": "tom.wagner@example.com", "w": 3,
     "pref": ["Gridfinity Box 2x3", "Gridfinity Grundplatte 4x4", "Fahrradlampen-Halter", "Lochwand-Werkzeughalter"]},
    {"de": "SV Blau-Weiß Musterstadt", "en": "Blue & White Sports Club", "margin": 15, "contact": "kasse@example.org", "w": 2,
     "note": ("Rechnung an den Kassenwart", "Invoice goes to the treasurer"),
     "pref": ["Vereinsschild mit Logo", "Medaillenhalter", "Schlüsselanhänger mit Namen"]},
    {"de": "Café Sonnendeck", "en": "Sundeck Café", "margin": 25, "contact": "hallo@example.net", "w": 2,
     "pref": ["Becheruntersetzer Café", "Menükartenhalter", "Pflanztopf mit Untersetzer"]},
    {"de": "Familie", "en": "Family", "margin": None, "gift": True, "w": 2,
     "note": ("Wird meistens verschenkt", "Mostly given as presents"),
     "pref": ["Spiralvase", "Lampenschirm Voronoi", "Weihnachtsstern", "Bilderrahmen", "Drache artikuliert", "Ausstechform Stern"]},
    {"de": "MakerWorld (eigene Modelle)", "en": "MakerWorld (own models)", "margin": None, "no_billing": True, "w": 2,
     "note": ("Eigene Entwürfe und Testdrucke für Uploads", "Own designs and test prints for uploads"),
     "pref": ["Kabelhalter Schreibtisch", "Wandhaken", "Handyständer", "Kopfhörerhalter", "Zahnradsatz Getriebe"]},
    {"de": "Lena Hoffmann", "margin": 10, "contact": "lena.h@example.com", "w": 2,
     "pref": ["Tischkarten Hochzeit", "Schlüsselanhänger mit Namen", "Handyhülle"]},
    {"de": "Jonas Klein", "margin": 20, "contact": "jonas.klein@example.com", "w": 1, "slow": True,
     "pref": ["Handyständer", "Kopfhörerhalter", "Drache artikuliert"]},
]

JOB_NOTES = [("Magnete 6×3 mm eingesetzt", "Inserted 6×3 mm magnets"),
             ("0,16 mm Schichthöhe, sieht top aus", "0.16 mm layers, looks great"),
             ("Mit Fuzzy Skin gedruckt", "Printed with fuzzy skin"),
             ("Stützen schwer zu entfernen", "Supports were hard to remove"),
             ("Modell 5 % größer skaliert", "Scaled the model up by 5 %"),
             ("Kunde möchte noch eine zweite Farbe", "Customer wants a second colour too")]
SCRAP_NOTES = [("Warping an der Ecke", "Warping at one corner"), ("Layer-Shift nach 2 h", "Layer shift after 2 h"),
               ("Haftung verloren, Spaghetti", "Lost bed adhesion, spaghetti")]
PAY_NOTES = [("bar", "cash"), ("PayPal", "PayPal"), ("Überweisung", "bank transfer")]
HANDOVER_NOTES = [(None, None), (None, None), ("abgeholt", "picked up"), ("per Post", "by post")]


# ---- Home Assistant (nur gespielt) ------------------------------------------

class DemoHA:
    """Antwortet wie ein verbundenes Home Assistant, ohne je etwas anzufragen."""

    connected = True
    last_event: str | None = None

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        if entity_id.endswith("_print_status"):
            return {"state": "idle" if "a1mini" in entity_id else "running"}
        if entity_id.endswith("_energy"):
            return {"state": "248.61"}
        if "_tray_" in entity_id:
            return {"state": "PLA Basic"}
        return {"state": "on"}


# ---- Daten erzeugen ----------------------------------------------------------

def _pick(rnd: random.Random, items: list[tuple[str, float, Any]]) -> tuple[str, float, Any]:
    x = rnd.random() * sum(w for _, w, _ in items)
    for it in items:
        x -= it[1]
        if x <= 0:
            return it
    return items[-1]


def _render_color(hex_color: str) -> str:
    """Schwarz/Weiß fürs Bild leicht anheben, damit die Schattierung sichtbar bleibt."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    if max(r, g, b) < 50:
        return "#2C2D31"
    if min(r, g, b) > 240:
        return "#ECEBE6"
    return hex_color


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _materials(rnd: random.Random, mtype: str, grams: float, ams_units: int) -> list[dict[str, Any]]:
    name, _, colors = _pick(rnd, FILAMENTS[mtype])
    if mtype == "TPU":  # TPU läuft über die externe Spule
        hexc, _cn = rnd.choice(colors)
        return [{"slot": "aktiv", "name": name, "type": mtype, "color": hexc, "grams": round(grams, 2)}]
    n = 1 if rnd.random() > 0.24 else rnd.choice((2, 2, 3))
    chosen = rnd.sample(colors, min(n, len(colors)))
    shares = [1.0] if n == 1 else ([0.78, 0.22] if n == 2 else [0.7, 0.2, 0.1])
    trays = rnd.sample([(u, tr) for u in range(1, ams_units + 1) for tr in range(1, 5)], len(chosen))
    out = []
    for (hexc, _cn), share, (unit, tray) in zip(chosen, shares, trays):
        out.append({"slot": f"AMS {unit} Tray {tray}", "name": name, "type": mtype, "color": hexc,
                    "grams": round(grams * share * rnd.uniform(0.95, 1.05), 2)})
    return out


def seed(store: Store, image_dir: Path, lang: str, now: float | None = None) -> int:
    """Löscht die Demo-Datenbank und legt neue Beispieldaten an. Gibt die Zahl der Drucke zurück."""
    en = lang == "en"
    L = (lambda pair: pair[1] if en else pair[0])  # noqa: E731
    now = now or time.time()
    rnd = random.Random(SEED)
    t0 = time.time()

    store.reset(everything=True)
    image_dir.mkdir(parents=True, exist_ok=True)
    for p in image_dir.iterdir():
        if p.is_file():
            p.unlink(missing_ok=True)

    # Einstellungen
    pcfg.save(store, [pcfg.normalize({
        "key": p["key"], "name": p["name"], "status": f"sensor.demo_{p['key']}_print_status",
        "prefix": f"demo_{p['key']}", "ams": [f"demo_{p['key']}_ams_{u}" for u in range(1, 3 if p["key"] == "h2s" else 2)],
        "ext": f"sensor.demo_{p['key']}_external_spool", "energy": f"sensor.demo_plug_{p['key']}_energy"}) for p in PRINTERS])
    for p in PRINTERS:
        store.set_printer_rate(p["key"], **p["rate"])
    for k, v in TYPE_PRICES.items():
        store.set_material_price(k, "type", v)
    for k, v in NAME_PRICES.items():
        store.set_material_price(k, "name", v)
    store.set_setting("margin1_name", "Private" if en else "Privat")
    store.set_setting("margin1_pct", "30")
    store.set_setting("margin2_name", "Business" if en else "Gewerbe")
    store.set_setting("margin2_pct", "60")

    # Kunden
    cust_ids: list[int] = []
    for c in CUSTOMERS:
        name = c.get("en", c["de"]) if en else c["de"]
        note = L(c["note"]) if c.get("note") else None
        cust_ids.append(store.save_customer(None, name, c.get("contact"), note, c.get("margin"), bool(c.get("no_billing"))))
    cust_weights = [c["w"] for c in CUSTOMERS]

    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    start_day = today - timedelta(days=DAYS - 1)
    meter = {p["key"]: rnd.uniform(120, 260) for p in PRINTERS}
    busy = {p["key"]: start_day.timestamp() for p in PRINTERS}
    jobs: list[dict[str, Any]] = []

    def choose_object(cust: int | None, day: datetime, max_g: float) -> tuple[Any, ...]:
        pool = OBJECTS
        if cust is not None and rnd.random() < 0.75:
            pool = [OBJECTS[_IDX[n]] for n in CUSTOMERS[cust]["pref"]]
        xmas = day.month in (11, 12)
        for _ in range(30):
            o = rnd.choice(pool)
            if o[8] == "xmas" and not xmas:
                continue
            if o[3][0] * min(o[6]) <= max_g:
                return o
        return OBJECTS[_IDX["Kalibrierwürfel"]]

    def plan(printer: dict[str, Any], start: float, day: datetime, force: tuple[Any, ...] | None = None,
             cust: int | None = None, qty: int | None = None) -> dict[str, Any]:
        o = force or choose_object(cust, day, printer["max_g"])
        qtys = [q for q in o[6] if o[3][1] * q <= printer["max_g"]] or [min(o[6])]
        q = qty or rnd.choice(qtys)
        types = [t for t in o[5].split("|") if t in printer["watts"]] or ["PLA"]
        mtype = rnd.choice(types)
        grams = rnd.uniform(*o[3]) * q
        hours = rnd.uniform(*o[4]) * q ** 0.82
        ams = 2 if printer["key"] == "h2s" else 1
        return {"obj": o, "qty": q, "type": mtype, "grams": grams, "hours": hours,
                "materials": _materials(rnd, mtype, grams, ams), "cust": cust}

    # Zwei Drucke laufen gerade
    running = {"h2s": (OBJECTS[_IDX["Lampenschirm Voronoi"]], 3.4, None, 1),
               "p1s": (OBJECTS[_IDX["Gridfinity Box 2x3"]], 0.7, 1, 4)}

    day = start_day
    while day <= today:
        weekend = day.weekday() >= 5
        for p in PRINTERS:
            if (today - day).days >= p["since"]:
                continue
            # Das Hobby wächst: vor einem Jahr etwa halb so viele Drucke wie heute
            ramp = 0.45 + 0.55 * (1 - (today - day).days / DAYS)
            lam = p["per_day"] * ramp * (1.6 if weekend else 1.0)
            n = sum(1 for _ in range(4) if rnd.random() < lam / 4)
            hour = rnd.uniform(9, 14) if weekend else rnd.choice((rnd.uniform(6.5, 7.5), rnd.uniform(16.5, 21)))
            cursor = max(busy[p["key"]], (day + timedelta(hours=hour)).timestamp())
            for _ in range(n):
                cust = None
                if rnd.random() < 0.52:
                    cust = rnd.choices(range(len(CUSTOMERS)), weights=cust_weights)[0]
                j = plan(p, cursor, day, cust=cust)
                end = cursor + j["hours"] * 3600
                if p["key"] in running and end > now - running[p["key"]][1] * 3600 - 1800:
                    break
                if end > now - 600:
                    break
                j.update(printer=p, start=cursor, end=end)
                jobs.append(j)
                cursor = end + rnd.uniform(6, 45) * 60
                if datetime.fromtimestamp(cursor).hour >= 23 or datetime.fromtimestamp(cursor).hour < 6:
                    break
            busy[p["key"]] = cursor
        day += timedelta(days=1)

    # Status, Strom, Kosten
    tariffs = [((today - timedelta(days=58)).timestamp(), 0.31), ((today - timedelta(days=230)).timestamp(), 0.34)]
    incomplete = set(rnd.sample(range(len(jobs)), 2))
    out_ids: list[int] = []
    for idx, j in enumerate(jobs):
        p, o = j["printer"], j["obj"]
        status, progress, scrap, note, job_note = "fertig", 100.0, None, None, None
        r = rnd.random()
        if r < 0.035:
            status, progress = "abgebrochen", round(rnd.uniform(6, 82), 0)
            job_note = L(rnd.choice(SCRAP_NOTES))
        elif r < 0.048:
            scrap = 1
            job_note = L(rnd.choice(SCRAP_NOTES))
        elif rnd.random() < 0.1:
            job_note = L(rnd.choice(JOB_NOTES))
        end = j["start"] + (j["end"] - j["start"]) * progress / 100
        watts = p["watts"].get(j["type"], 120) * rnd.uniform(0.9, 1.1)
        kwh = round((end - j["start"]) / 3600 * watts / 1000 + rnd.uniform(0.006, 0.02), 3)
        start_kwh = round(meter[p["key"]], 3)
        meter[p["key"]] += kwh + rnd.uniform(0.01, 0.08)  # Standby zwischen den Drucken
        price = next((pr for since, pr in tariffs if j["start"] >= since), 0.36)
        fields: dict[str, Any] = dict(
            printer=p["key"], printer_name=p["name"], task_name=L(o[:2]), state="done", status=status,
            start_ts=j["start"], printer_start_ts=j["start"], end_ts=end, final_ts=end + rnd.uniform(4, 11) * 60,
            start_kwh=start_kwh, end_kwh=round(start_kwh + kwh, 3), kwh=kwh, price=price, cost=round(kwh * price, 4),
            weight_g=round(j["grams"], 2), progress=progress, materials=json.dumps(j["materials"]),
            quantity=j["qty"] if j["qty"] > 1 else None, scrap=scrap, job_note=job_note, created_at=j["start"],
            customer_id=cust_ids[j["cust"]] if j["cust"] is not None else None)
        if idx in incomplete and status == "fertig":
            fields.update(status="unvollständig", start_kwh=None, kwh=None, cost=None, note="Druckstart nicht empfangen")
        if o[7] and rnd.random() < 0.55:
            if rnd.random() < 0.75:
                fields["project_url"] = f"https://makerworld.com/{'en' if en else 'de'}/models/{_slug(o[7])}"
            else:
                fields["project_url"] = f"https://www.printables.com/model/{_slug(o[7])}"
            fields["project_title"] = o[7]
        if rnd.random() < 0.22:
            base = "D:\\3D Printing" if en else "D:\\3D-Druck"
            fields["local_path"] = f"{base}\\{_slug(o[1]).replace('-', '_')}\\{_slug(o[1])}.3mf"
        if rnd.random() < 0.15:
            fields["material_notes"] = json.dumps({"0": next(cn for hx, cn in sum((c for _, _, c in FILAMENTS[j["type"]]), [])
                                                             if hx == j["materials"][0]["color"])})
        job_id = store.insert(**fields)
        store.update(job_id, **compute(store.get(job_id) or {}, store))
        j["id"], j["status"], j["scrap"] = job_id, status, scrap or status == "abgebrochen"
        out_ids.append(job_id)

    # Laufende Drucke
    for key, (o, hours_ago, cust, qty) in running.items():
        p = next(x for x in PRINTERS if x["key"] == key)
        start = max(now - hours_ago * 3600, busy[key])
        j = plan(p, start, today, force=o, cust=cust, qty=qty)
        jid = store.insert(printer=key, printer_name=p["name"], task_name=L(o[:2]), state="running", start_ts=start,
                           printer_start_ts=start, start_kwh=round(meter[key], 3), weight_g=round(j["grams"], 2),
                           materials=json.dumps(j["materials"]), quantity=qty if qty and qty > 1 else None,
                           customer_id=cust_ids[cust] if cust is not None else None, created_at=start)
        j.update(id=jid, printer=p, start=start, end=None, status="läuft", scrap=False)
        jobs.append(j)

    # Zahlungen, Übergaben, Geschenke
    for j in jobs:
        if j["cust"] is None or j["status"] == "läuft":
            continue
        c = CUSTOMERS[j["cust"]]
        job = store.get(j["id"]) or {}
        if c.get("no_billing") or j["scrap"]:
            continue
        age = (now - j["end"]) / 86400
        qty = j["qty"]
        if c.get("gift") or rnd.random() < 0.04:
            store.update(j["id"], gift=1)
            if age > 2 or rnd.random() < 0.3:
                store.add_handover(j["id"], qty, j["end"] + rnd.uniform(0.5, min(age, 6)) * 86400 if age > 0.6 else now - 600)
            continue
        due = (job.get("cost_total") or 0) * (1 + (c.get("margin") or 0) / 100)
        if rnd.random() < 0.45:
            step = 0.5 if due < 10 else 1.0
            due = max(step, math.ceil(due * rnd.uniform(1.0, 1.25) / step) * step)
            store.update(j["id"], final_price=due)
        if c.get("slow") and age > 20:
            paid_share, handed = (0.0 if rnd.random() < 0.6 else 0.5), qty
        elif age > 21:
            paid_share, handed = (1.0 if rnd.random() < 0.94 else 0.5), qty
        elif age > 6:
            r = rnd.random()
            paid_share = 1.0 if r < 0.6 else (0.5 if r < 0.8 else 0.0)
            handed = qty if rnd.random() < 0.75 else (qty // 2 if qty > 1 else 0)
        else:
            r = rnd.random()
            paid_share = 1.0 if r < 0.3 else (0.5 if r < 0.5 else 0.0)
            r = rnd.random()
            handed = 0 if r < 0.45 else (max(1, qty // 2) if r < 0.7 and qty > 1 else qty)
        when = lambda lo, hi: min(now - 300, j["end"] + rnd.uniform(lo, hi) * 86400)  # noqa: E731
        if handed:
            if handed == qty and qty > 3 and rnd.random() < 0.3:
                first = qty // 2
                store.add_handover(j["id"], first, when(0.3, 3), L(rnd.choice(HANDOVER_NOTES)))
                store.add_handover(j["id"], qty - first, when(3, 9), L(rnd.choice(HANDOVER_NOTES)))
            else:
                store.add_handover(j["id"], handed, when(0.3, 5), L(rnd.choice(HANDOVER_NOTES)))
        if paid_share > 0 and due > 0:
            if paid_share < 1:
                store.add_payment(j["id"], round(due * paid_share, 2), when(0.2, 4), L(rnd.choice(PAY_NOTES)))
            elif rnd.random() < 0.15 and due > 4:
                half = round(due / 2, 2)
                store.add_payment(j["id"], half, when(0, 2), L(rnd.choice(PAY_NOTES)))
                store.add_payment(j["id"], round(due - half, 2), when(3, 10), L(rnd.choice(PAY_NOTES)))
            else:
                store.add_payment(j["id"], round(due, 2), when(0.2, 6), L(rnd.choice(PAY_NOTES)))

    # Bilder: Vorschau für jeden Druck, Fotos für einige fertige
    for j in jobs:
        o = j["obj"]
        colors = [_render_color(m["color"]) for m in j["materials"]]
        name = f"job_{j['id']}.png"
        cover(image_dir / name, o[2], colors, j["qty"], j["id"])
        store.update(j["id"], image=name)
    done = [j for j in jobs if j["status"] == "fertig" and not j["scrap"]]
    with_photos = [j for j in done if j["cust"] is not None and rnd.random() < 0.45] + \
                  [j for j in done if j["cust"] is None and rnd.random() < 0.08]
    for j in with_photos[:34]:
        for k in range(rnd.choice((1, 1, 2, 3))):
            name = f"photo_{j['id']}_{k}.jpg"
            colors = [_render_color(m["color"]) for m in j["materials"]]
            photo(image_dir / name, j["obj"][2], colors, j["qty"], j["id"] * 7 + k)
            store.add_photo(j["id"], name)

    # Papierkorb: zwei eigene Fehlversuche, vor ein paar Tagen gelöscht
    trash = [j for j in jobs if j["cust"] is None and j["status"] != "läuft" and now - j["start"] < 20 * 86400][:2]
    for k, j in enumerate(trash):
        store.trash_job(j["id"])
        with store._lock, store._conn:  # noqa: SLF001 - Löschzeitpunkt für die Demo vordatieren
            store._conn.execute("UPDATE trash SET deleted_ts = ? WHERE id = ?", (now - (2 + 4 * k) * 86400, j["id"]))  # noqa: SLF001

    store.set_setting("demo_lang", lang)
    store.set_setting("demo_ts", f"{now:.0f}")
    log.info("Demo-Daten erzeugt: %s Drucke in %.1f s", len(jobs), time.time() - t0)
    return len(jobs)


# ---- Unter-App /demo/ ---------------------------------------------------------

def create_demo_app(data_dir: Path, real_store: Store) -> web.Application:
    """Eigene aiohttp-App für ``/demo/`` mit eigener Datenbank, eigenen Bildern und gespieltem HA."""
    from .web import setup_routes

    demo_dir = data_dir / "demo"
    image_dir = demo_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    store = Store(demo_dir / "printlog.db")
    if not store.settings().get("language") and real_store.settings().get("language"):
        store.set_setting("language", real_store.settings()["language"])
    lock = asyncio.Lock()
    started = {"done": False}  # nach jedem Neustart einmal frisch erzeugen

    def wanted_lang(request: web.Request) -> str:
        return resolve(store.settings().get("language"), request.headers.get("Accept-Language"))

    async def ensure(request: web.Request, force: bool = False) -> None:
        lang = wanted_lang(request)
        async with lock:
            cfg = store.settings()
            stale = time.time() - float(cfg.get("demo_ts") or 0) > MAX_AGE_S
            if force or not started["done"] or stale or cfg.get("demo_lang") != lang:
                await asyncio.get_running_loop().run_in_executor(None, seed, store, image_dir, lang)
                started["done"] = True

    @web.middleware
    async def demo_mode(request: web.Request, handler: Any) -> web.StreamResponse:
        if not request.path.startswith(("/demo/images/", "/demo/static/")) and not request.path.endswith("/demo-neu"):
            await ensure(request)
        token = DEMO.set(True)
        try:
            return await handler(request)
        finally:
            DEMO.reset(token)

    app = web.Application(client_max_size=60 * 1024**2, middlewares=[demo_mode])
    app["store"] = store

    async def regenerate(request: web.Request) -> web.Response:
        await ensure(request, force=True)
        raise web.HTTPSeeOther("./")

    async def warm_up(_app: web.Application) -> None:
        """Beim Start im Hintergrund erzeugen, damit das erste Öffnen nicht warten muss."""
        setting = store.settings().get("language")
        if setting in ("de", "en"):
            async def run() -> None:
                async with lock:
                    await asyncio.get_running_loop().run_in_executor(None, seed, store, image_dir, setting)
                    started["done"] = True
            _app["warm_up"] = asyncio.create_task(run())

    app.on_startup.append(warm_up)
    app.router.add_post("/demo-neu", regenerate)
    setup_routes(app, store, image_dir, DemoHA(), demo=True)
    return app
