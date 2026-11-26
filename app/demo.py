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
     "since": DAYS, "rate": {"purchase_eur": 219, "life_hours": 3000, "maint_per_1000h": 65}},
    {"key": "p1s", "name": "P1S", "watts": {"PLA": 118, "PETG": 158, "ASA": 176, "TPU": 110}, "max_g": 260, "per_day": 0.62,
     "since": 300, "rate": {"purchase_eur": 599, "life_hours": 4000, "maint_per_1000h": 70}},
    {"key": "h2s", "name": "H2S", "watts": {"PLA": 146, "PETG": 196, "ASA": 218, "TPU": 140}, "max_g": 420, "per_day": 0.7,
     "since": 72, "rate": {"purchase_eur": 1399, "life_hours": 5000, "maint_per_1000h": 100}},
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
# Fehldruck-Ursache passend zur Notiz (eigener Zufall, damit die übrigen Beispieldaten gleich bleiben)
SCRAP_CAUSE = {"Warping an der Ecke": "adhesion", "Warping at one corner": "adhesion", "Layer-Shift nach 2 h": "printer",
               "Layer shift after 2 h": "printer", "Haftung verloren, Spaghetti": "adhesion", "Lost bed adhesion, spaghetti": "adhesion"}
DEFECT_NOTES = {
    "settings": ("Profil zu schnell (0,28 mm Draft): Oberfläche rau, Naht deutlich sichtbar. Nachdruck mit 0,16 mm.",
                 "Profile too fast (0.28 mm draft): rough surface, seam clearly visible. Reprint at 0.16 mm."),
    "material": ("Filament war feucht: Fäden und kleine Blasen an der Oberfläche.", "Filament was wet: stringing and small bubbles on the surface."),
    "customer": ("Kunde wollte doch eine andere Farbe.", "Customer changed their mind about the colour."),
    "other": ("Teil beim Ablösen von der Platte gebrochen.", "Part broke when taking it off the plate."),
}
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


def _defects(store: Store, jobs: list[dict[str, Any]], L: Any, en: bool) -> None:
    """Fehldrucke: Ursache, Berechnung und Nachdruck für die Beispieldaten."""
    r = random.Random(SEED + 7)
    scrap = [j for j in jobs if j.get("scrap")]
    # Nicht abgebrochen, aber trotzdem Fehldruck: andere Ursachen mit passender Notiz, Drucke mit Kunde zuerst
    done = sorted((j for j in scrap if (store.get(j["id"]) or {}).get("status") == "fertig"),
                  key=lambda j: (j.get("cust") is None, -j["start"]))
    extra = dict(zip((j["id"] for j in done), ["settings", "customer", "material", "settings"]))
    for j in scrap:
        job = store.get(j["id"]) or {}
        cause = SCRAP_CAUSE.get(job.get("job_note") or "")
        fields: dict[str, Any] = {}
        if j["id"] in extra:
            cause = extra[j["id"]]
            if cause == "customer" and not job.get("customer_id"):
                cause = "other"
            fields["job_note"] = L(DEFECT_NOTES[cause])
        elif job.get("status") != "fertig" and r.random() < 0.35:
            cause = None  # Ursache noch nicht angegeben
        fields["defect"] = cause
        if job.get("customer_id") and job.get("status") == "fertig" and cause in ("settings", "customer"):
            fields["defect_charge"], fields["defect_value"] = ("full", None) if cause == "customer" else ("pct", 50.0)
        store.update(j["id"], **fields)
        later = [x for x in jobs if x["id"] != j["id"] and x.get("start") and x["start"] > j["start"]
                 and x.get("obj") is j.get("obj") and x["start"] - j["start"] < 21 * 86400 and not x.get("scrap")]
        later.sort(key=lambda x: x["start"])
        if later and r.random() < 0.8:
            store.set_reprint(j["id"], later[0]["id"])


def _project(store: Store, jobs: list[dict[str, Any]], cust_ids: list[int], skip: set[int], now: float, L: Any,
             image_dir: Path) -> int | None:
    """Ein Beispielprojekt: Werkstatt-Teile für Tom, parallel auf allen drei Druckern gedruckt."""
    done = [j for j in jobs if j["status"] == "fertig" and not j["scrap"] and j["id"] not in skip
            and 2 * 86400 < now - j["start"] < 30 * 86400]
    best: list[dict[str, Any]] = []
    for a in done:  # spätester Zeitraum von 14 h, in dem alle Drucker etwas gedruckt haben
        win = [j for j in done if a["start"] <= j["start"] < a["start"] + 14 * 3600]
        if len({j["printer"]["key"] for j in win}) == len(PRINTERS):
            best = win[:5]
    if len(best) < 3:
        return None
    parts = [("Gridfinity Grundplatte 4x4", 2), ("Gridfinity Box 2x3", 4), ("Lochwand-Werkzeughalter", 6),
             ("Gridfinity Box 2x3", 4), ("Wandhaken", 4)]
    for j, (name, qty) in zip(best, parts):
        o = OBJECTS[_IDX[name]]
        j["obj"], j["qty"] = o, qty
        store.update(j["id"], task_name=L(o[:2]), quantity=qty, gift=None, project_url=None, project_title=None, local_path=None)
        cover(image_dir / f"job_{j['id']}.png", o[2], [_render_color(m["color"]) for m in j["materials"]], qty, j["id"])
    return store.create_project([j["id"] for j in best], name=L(("Garage-Lochwand", "Garage pegboard")), customer_id=cust_ids[1])


def _split(store: Store, jobs: list[dict[str, Any]], cust_ids: list[int], skip: set[int], now: float, L: Any,
           image_dir: Path, project: int | None = None) -> None:
    """Aufteilen und Vorrat: 12 Clips für Tom (2), Anna (4) und 2 ins Projekt, Rest im Vorrat; ein Bausatz mit Sätzen und Einzelteil."""
    o = OBJECTS[_IDX["Kabelkanal-Clips"]]
    done = [j for j in jobs if j["status"] == "fertig" and not j["scrap"] and j["id"] not in skip
            and 3 * 86400 < now - j["start"] < 25 * 86400 and not (store.get(j["id"]) or {}).get("project_id")]
    done.sort(key=lambda j: (j["obj"] is not o, -j["start"]))
    if done:
        j = done[0]
        store.clear_payments(j["id"])
        store.clear_handovers(j["id"])
        store.update(j["id"], task_name=L(o[:2]), quantity=12, customer_id=cust_ids[1], gift=None, final_price=None)
        cover(image_dir / f"job_{j['id']}.png", o[2], [_render_color(m["color"]) for m in j["materials"]], 12, j["id"])
        store.update(j["id"], image=f"job_{j['id']}.png")
        store.set_split(j["id"], [{"customer_id": cust_ids[1], "qty": 2}, {"customer_id": cust_ids[0], "qty": 4}]
                        + ([{"project_id": project, "qty": 2}] if project else []))
        shares = store.shares([j["id"]]).get(j["id"], [])
        tom = next(s for s in shares if s["customer_id"] == cust_ids[1])
        anna = next(s for s in shares if s["customer_id"] == cust_ids[0])
        day = j["end"] if j.get("end") else j["start"]
        store.add_handover(tom["id"], 2, day + 86400, L(("beim Training", "at practice")))
        store.add_payment(tom["id"], round(store.get(tom["id"])["cost_total"] * 1.2, 2), day + 86400, L(("bar", "cash")))
        store.add_handover(anna["id"], 4, day + 2 * 86400)
        store.give(j["id"], cust_ids[7], 1, handed=True)
        store.give(j["id"], None, 1, note="selbst verwendet")
        with store._lock, store._conn:  # noqa: SLF001 - Abgaben aus dem Vorrat zeitlich nach dem Druck
            store._conn.execute("UPDATE jobs SET start_ts = ? WHERE share_of = ? AND share_kind IN ('stock','out')",  # noqa: SLF001
                                (now - 2 * 86400, j["id"]))
    # Bausatz aus drei Teilen, 5 Sätze: Anna 2, Familie 1 (verschenkt), Rest im Vorrat; einzelner Kopf an Jonas
    free = [j for j in jobs if j["status"] == "fertig" and not j["scrap"] and j["id"] not in skip and j["cust"] is None
            and 6 * 86400 < now - j["start"] < 40 * 86400 and not (store.get(j["id"]) or {}).get("project_id")
            and not (store.get(j["id"]) or {}).get("split")][:3]
    if len(free) == 3:
        parts = [(("Figur Kopf", "Figure head"), 5), (("Figur Arme", "Figure arms"), 10), (("Figur Körper", "Figure body"), 5)]
        for j, (name, qty) in zip(free, parts):
            store.update(j["id"], task_name=L(name), quantity=qty, project_url=None, project_title=None, local_path=None, gift=None)
            cover(image_dir / f"job_{j['id']}.png", "dragon", [_render_color(m["color"]) for m in j["materials"]], qty, j["id"])
        pid = store.create_project([j["id"] for j in free], name=L(("Figuren-Bausatz", "Figure kit")), quantity=5)
        if pid:
            store.set_split(pid, [{"customer_id": cust_ids[0], "qty": 2}, {"customer_id": cust_ids[4], "qty": 1}])
            fam = next(s for s in store.shares([pid])[pid] if s["customer_id"] == cust_ids[4])
            store.update(fam["id"], gift=1)
            store.add_handover(fam["id"], 1, now - 3 * 86400)
            store.give(pid, cust_ids[7], 1, part=free[0]["id"])


def _locations(store: Store, L: Any) -> None:
    """Lager: drei Lagerorte, Zubehör darin, Vorrat in Kisten."""
    from . import storage as lg

    box1 = lg.create_location(store, L(("Kiste 1 · Anhänger", "Box 1 · Keychains")), L(("Regal im Arbeitszimmer", "Shelf in the office")))
    box2 = lg.create_location(store, L(("Kiste 2 · Vorrat", "Box 2 · Stock")))
    drawer = lg.create_location(store, L(("Schublade Kleinteile", "Small parts drawer")))
    rows = store._conn.execute("SELECT id, grp FROM accessories ORDER BY id").fetchall()  # noqa: SLF001
    for r in rows:
        lid = drawer if r["grp"] in ("Magnete", "Magnets") else box1 if r["grp"] in ("Anhänger", "Keychains") else None
        if lid:
            lg.assign_accessories(store, [r["id"]], lid)
    stock = [j["id"] for j in store.stock_items()]
    lg.assign_jobs(store, stock[:2], box2)
    lg.assign_jobs(store, stock[2:3], box1)


def _models(store: Store, image_dir: Path, L: Any) -> None:
    """Modelle wie nach dem ersten Start erkennen, dann zwei offene Fragen für die Glocke."""
    from . import models as mdl

    store.set_setting("models_ready", None)
    mdl.recognize_pending(store, image_dir)
    rows = store._conn.execute(  # noqa: SLF001
        "SELECT j.id, j.model_id, j.task_name FROM jobs j WHERE j.kind IS NULL AND j.state = 'done' AND j.customer_id IS NOT NULL "
        "AND j.project_id IS NULL AND COALESCE(j.split, 0) = 0 AND (SELECT COUNT(*) FROM jobs k WHERE k.model_id = j.model_id) >= 3 "
        "ORDER BY j.start_ts DESC").fetchall()
    seen: set[int] = set()
    picked = []
    for r in rows:
        if r["model_id"] not in seen:
            seen.add(r["model_id"])
            picked.append(r)
        if len(picked) == 2:
            break
    if len(picked) < 2:
        return
    a, b = picked
    store.update(a["id"], model_state="tentative", model_score=70, model_hint=1, gcode_plate=2,
                 model_cands=json.dumps([[a["model_id"], 70, ["name", "plate", "gpp"]]]))
    store.update(b["id"], model_id=None, model_state="suggest", model_score=45, model_hint=1,
                 task_name=f'{b["task_name"]} {L(("klein", "small"))}',
                 model_cands=json.dumps([[b["model_id"], 45, ["similar", "plate", "gpp"]]]))


def _orders(store: Store, cust_ids: list[int], pid: int | None, now: float, L: Any) -> None:
    """Aufträge: einer in Arbeit (mit laufendem Druck), drei offene, einer fertig zum Abrechnen, einer im Archiv.
    Dazu Favoriten auf den meistgedruckten Modellen."""
    from . import orders as od

    conn = store._conn  # noqa: SLF001
    top = conn.execute("SELECT model_id, COUNT(*) n FROM jobs WHERE kind IS NULL AND model_id IS NOT NULL "
                       "GROUP BY model_id ORDER BY n DESC LIMIT 5").fetchall()
    with store._lock, conn:  # noqa: SLF001
        for r in top:
            conn.execute("UPDATE models SET fav = 1 WHERE id = ?", (r["model_id"],))
    day = lambda d: datetime.fromtimestamp(now + d * 86400).date().isoformat()  # noqa: E731
    used: set[int] = set()

    def fav_of(cid: int, skip: set[int]) -> list[int]:
        return [r[0] for r in conn.execute(
            "SELECT model_id FROM jobs WHERE kind IS NULL AND customer_id = ? AND model_id IS NOT NULL GROUP BY model_id "
            "ORDER BY COUNT(*) DESC", (cid,)) if r[0] not in skip]

    def free_done(mid: int, n: int) -> list[dict[str, Any]]:
        """Fertige, gute Drucke eines Modells, die noch nicht abgerechnet sind (kein Projekt, keine Zahlung)."""
        rows = conn.execute(
            f"SELECT id, quantity FROM jobs WHERE kind IS NULL AND model_id = ? AND state = 'done' AND NOT {Store.SCRAP_SQL} "
            "AND project_id IS NULL AND COALESCE(split, 0) = 0 AND order_item_id IS NULL AND COALESCE(gift, 0) = 0 "
            "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.job_id = jobs.id) "
            "AND NOT EXISTS (SELECT 1 FROM handovers h WHERE h.job_id = jobs.id) ORDER BY start_ts DESC LIMIT ?", (mid, n)).fetchall()
        return [dict(r) for r in rows]

    billing = [c for c in cust_ids if not (store.customer(c) or {}).get("no_billing")]
    # 1) In Arbeit: der laufende Druck gehört zu einer Nachbestellung
    run = conn.execute("SELECT id, task_name, quantity FROM jobs WHERE kind IS NULL AND state = 'running' ORDER BY start_ts LIMIT 1").fetchone()
    if run and billing:
        cid = billing[0]
        mid = od.model_by_name(store, run["task_name"] or "")
        oid = od.create_order(store, cid, L(("Nachbestellung", "Reorder")), day(6), None, None)
        per = run["quantity"] or 1
        it = od.add_item(store, oid, mid, run["task_name"], per * 4)
        od.link(store, run["id"], it)
        for j in free_done(mid, 2) if mid else []:
            od.link(store, j["id"], it)
        others = fav_of(cid, {mid} if mid else set())
        if others:
            od.add_item(store, oid, others[0], None, 6)
            used.add(others[0])
    # 2) Offen: drei Aufträge mit Termin, einer mit Freitext
    open_first: list[tuple[int, int]] = []  # (Position, Modell) des ersten Eintrags je offenem Auftrag
    for k, (cid, title, due, qty) in enumerate(zip(billing[1:4], (("Dezember-Bestellung", "December order"), ("Geschenke", "Gifts"),
                                                                     ("Kleinkram", "Bits and pieces")), (3, 12, None), (6, 10, 2))):
        oid = od.create_order(store, cid, L(title), day(due) if due is not None else None, None, None)
        ms = fav_of(cid, used)
        if ms:
            open_first.append((od.add_item(store, oid, ms[0], None, qty), ms[0]))
            used.add(ms[0])
        if k == 1 and open_first:  # gleiches Modell wie in der Dezember-Bestellung: daraus wird eine Frage der Glocke
            open_first.append((od.add_item(store, oid, open_first[0][1], None, 4), open_first[0][1]))
        if k == 0 and len(ms) > 1:
            od.add_item(store, oid, ms[1], None, 1)
        if k == 2:
            od.add_item(store, oid, None, L(("Halter Akkuschrauber (Modell suchen)", "Drill holder (find a model)")), 2)
    # Glocke: ein fertiger Druck passt zu zwei offenen Aufträgen
    same = [i for i, m in open_first if open_first and m == open_first[0][1]]
    if len(same) > 1:
        js = free_done(open_first[0][1], 1)
        if js:
            store.update(js[0]["id"], order_state="ask", order_cands=json.dumps(same))
    store.set_setting("orders_since", f"{now:.0f}")
    # 3) Fertig: zwei gedruckte Positionen, vereinbarter Preis
    if len(billing) > 4:
        cid = billing[4]
        oid = od.create_order(store, cid, L(("Für den Schreibtisch", "For the desk")), None, None, None)
        got = 0
        for mid in fav_of(cid, used) + [r["model_id"] for r in top]:
            js = free_done(mid, 1)
            if not js or mid in used:
                continue
            it = od.add_item(store, oid, mid, None, js[0]["quantity"] or 1)
            od.link(store, js[0]["id"], it)
            used.add(mid)
            got += 1
            if got == 2:
                break
        o = od.order(store, oid)
        if o and o["items"]:
            est = od.estimate(o)
            if est["price"]:
                od.save_order(store, oid, agreed_price=float(math.ceil(est["price"])))
    # 5) Pro Platte: ein MakerWorld-Set mit 5 Platten, zwei Designs zur Auswahl, Clips aus dem Vorrat
    if len(billing) > 5:
        _plate_order(store, billing[5], now, L)
        _shared_parts(store, billing[2], billing[5], now, L)
    # 4) Archiv: das Beispielprojekt war ein Auftrag
    if pid:
        kids = [k for k in store.children([pid]).get(pid, []) if not k.get("kind")]
        proj = store.get(pid) or {}
        oid = od.create_order(store, proj.get("customer_id"), proj.get("task_name"), None, None, None)
        sums: dict[Any, list[dict[str, Any]]] = {}
        for k in kids:
            sums.setdefault(k.get("model_id") or k.get("task_name"), []).append(k)
        for key, ks in sums.items():
            mid = ks[0].get("model_id")
            it = od.add_item(store, oid, mid, ks[0].get("task_name"), max(1, sum(k.get("quantity") or 1 for k in ks if not k.get("is_scrap"))))
            with store._lock, conn:  # noqa: SLF001
                for k in ks:
                    conn.execute("UPDATE jobs SET order_item_id = ? WHERE id = ?", (it, k["id"]))
        with store._lock, conn:  # noqa: SLF001
            conn.execute("UPDATE orders SET billed_job = ?, created_at = ?, done_at = ? WHERE id = ?",
                         (pid, (proj.get("start_ts") or now) - 3 * 86400, proj.get("end_ts") or now, oid))



def _plate_order(store: Store, cid: int, now: float, L: Any) -> None:
    """Demo für Platten pro Satz (app/plates.py): gelernte Platten, Auswahlgruppe, Vorrat, Fortschritt."""
    from . import orders as od
    from . import plates as pl

    conn = store._conn  # noqa: SLF001
    imgs = [r[0] for r in conn.execute("SELECT DISTINCT image FROM jobs WHERE kind IS NULL AND image IS NOT NULL LIMIT 6")]
    base = conn.execute("SELECT * FROM jobs WHERE kind IS NULL AND state = 'done' AND cost_total IS NOT NULL "
                        "ORDER BY start_ts DESC LIMIT 1").fetchone()
    if not base or not imgs:
        return
    name = L(("Lochwand-Set (MakerWorld)", "Pegboard set (MakerWorld)"))
    from . import models as mdl
    mid = mdl.create_model(store, name)
    plates = [  # Platte, Name, Gruppe, Stück, Minuten, Gramm
        (1, ("Grundplatte", "Base plate"), None, 1, 130, 85),
        (2, ("Haken klein", "Small hooks"), None, 8, 65, 32),
        (3, ("Design A: Blume", "Design A: flower"), ("Design", "Design"), 1, 90, 40),
        (4, ("Design B: Stern", "Design B: star"), ("Design", "Design"), 1, 85, 38),
        (5, ("Schrauben-Clips", "Screw clips"), None, 2, 40, 12),
    ]
    fields = {k: base[k] for k in base.keys() if k not in ("id", "code", "order_item_id", "order_state", "order_cands",
                                                          "project_id", "split", "customer_id", "model_hint", "model_cands")}

    def job(plate: tuple[Any, ...], start: float, qty: int | None = None, cust: int | None = None) -> int:
        f = dict(fields)
        f.update(task_name=name, model_id=mid, model_state="auto", gcode_plate=plate[0], quantity=qty or plate[3],
                 start_ts=start, end_ts=start + plate[4] * 60, filament_g=plate[5], weight_g=plate[5],
                 cost_total=round(plate[5] * 0.025 + plate[4] / 60 * 0.12, 2), cost_work=0.0,
                 image=imgs[plate[0] % len(imgs)], customer_id=cust, created_at=start)
        return store.insert(**f)

    day = 86400
    for k, p in enumerate(plates):  # früher schon einmal gedruckt: daraus lernt printlog die Platten
        job(p, now - (40 - k) * day)
    clips = job(plates[4], now - 30 * day, qty=6, cust=cid)  # 6 Clips, 2 bekam der Kunde, 4 liegen im Vorrat
    store.set_split(clips, [{"sid": None, "customer_id": cid, "qty": 2}])
    job(plates[4], now - 29 * day)  # zuletzt 2 Clips pro Platte
    for p in plates:
        grp = L(p[2]) if p[2] else None
        pl.save_plates(store, {"id": 0, "model_id": mid}, [{"plate": p[0], "name": L(p[1]), "grp": grp}])
    oid = od.create_order(store, cid, L(("Garage-Lochwand", "Garage pegboard")), datetime.fromtimestamp(now + 9 * day).date().isoformat(),
                          None, None)
    iid = od.add_item(store, oid, mid, None, 2)
    item = od.order(store, oid)["items"][0]
    pl.set_cell(store, item, 2, 4)  # Satz 2 bekommt Design B
    for s in (1, 2):
        pl.set_cell(store, item, s, 5, "stock")  # Clips aus dem Vorrat
    for k, p in enumerate((plates[0], plates[0], plates[1], plates[2])):
        od.link(store, job(p, now - (3 - k * 0.5) * day), iid)


def _shared_parts(store: Store, cid: int, other: int, now: float, L: Any) -> None:
    """Demo für gemeinsame Teile: „Kopf + Hände“ liegt im Vorrat (auch in anderer Farbe), eine neue Figur nutzt es."""
    from . import models as mdl
    from . import orders as od
    from . import plates as pl

    conn = store._conn  # noqa: SLF001
    base = conn.execute("SELECT * FROM jobs WHERE kind IS NULL AND state = 'done' AND cost_total IS NOT NULL "
                        "ORDER BY start_ts DESC LIMIT 1").fetchone()
    imgs = [r[0] for r in conn.execute("SELECT DISTINCT image FROM jobs WHERE kind IS NULL AND image IS NOT NULL LIMIT 8")]
    if not base or not imgs:
        return
    fields = {k: base[k] for k in base.keys() if k not in ("id", "code", "order_item_id", "order_state", "order_cands",
                                                          "project_id", "split", "customer_id", "model_hint", "model_cands")}
    skin = '[{"slot":1,"name":"PLA Basic","type":"PLA","color":"#F1C27D","grams":14}]'
    yellow = '[{"slot":1,"name":"PLA Basic","type":"PLA","color":"#FFD21F","grams":14}]'
    red = '[{"slot":1,"name":"PLA Basic","type":"PLA","color":"#D7263D","grams":30},{"slot":2,"name":"PLA Basic","type":"PLA","color":"#FFFFFF","grams":6}]'

    def job(name: str, mid: int, plate: int, qty: int, start: float, mats: str, cust: int | None = None, img: int = 0) -> int:
        f = dict(fields)
        f.update(task_name=name, model_id=mid, model_state="auto", gcode_plate=plate, quantity=qty, start_ts=start,
                 end_ts=start + 50 * 60, filament_g=14.0 * qty, weight_g=14.0 * qty, cost_total=round(0.35 * qty + 0.1, 2),
                 cost_work=0.0, materials=mats, image=imgs[img % len(imgs)], customer_id=cust, created_at=start)
        return store.insert(**f)

    day = 86400
    head = L(("Kopf + Hände", "Head + hands"))
    fig_a, fig_b = L(("Brick-Figur Blau-Gelb", "Brick figure blue-yellow")), L(("Brick-Figur Rot-Weiß", "Brick figure red-white"))
    k = mdl.create_model(store, head)
    a = mdl.create_model(store, fig_a)
    b = mdl.create_model(store, fig_b)
    p = job(head, k, 1, 6, now - 25 * day, skin, other, img=3)  # 6 Köpfe gedruckt, 1 abgegeben → 5 im Vorrat
    store.set_split(p, [{"sid": None, "customer_id": other, "qty": 1}])
    y = job(fig_a, a, 1, 3, now - 22 * day, yellow, other, img=3)  # gelbe Köpfe der Blau-Gelb-Figur, 2 übrig
    store.set_split(y, [{"sid": None, "customer_id": other, "qty": 1}])
    job(fig_a, a, 2, 1, now - 22 * day, red, img=4)
    pl.save_plates(store, {"id": 0, "model_id": a}, [{"plate": 1, "name": head, "part_model_id": k},
                                                     {"plate": 2, "name": L(("Körper + Trikot", "Body + jersey"))}])
    pl.save_plates(store, {"id": 0, "model_id": b}, [{"plate": 1, "name": head, "part_model_id": k},
                                                     {"plate": 2, "name": L(("Körper + Trikot Rot-Weiß", "Body + jersey red-white"))},
                                                     {"plate": 3, "name": L(("Beine + Schuhe", "Legs + shoes"))}])
    oid = od.create_order(store, cid, L(("Vereinsfiguren", "Club figures")), datetime.fromtimestamp(now + 12 * day).date().isoformat(),
                          None, None)
    iid = od.add_item(store, oid, b, None, 3)
    od.link(store, job(fig_b, b, 2, 1, now - 1 * day, red, img=4), iid)

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

    _defects(store, jobs, L, en)

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

    # Arbeit & Zubehör: kleines Inventar, Anhänger mit Ring und Kette, ein paar Drucke mit Arbeitszeit
    from . import extras as zk
    store.set_setting("work_rate", "18")
    store.set_setting("design_rate", "30")
    acc = {}
    for key, icon, de_en, grp, pq, pp, st, wa in (
            ("ring", "⭕", ("Schlüsselring 25 mm", "Key ring 25 mm"), ("Anhänger", "Keychains"), 100, 6.99, 120, 20),
            ("kette", "⛓️", ("Kugelkette 10 cm", "Ball chain 10 cm"), ("Anhänger", "Keychains"), 50, 5.49, 45, 15),
            ("magnet", "🧲", ("Neodym-Magnet 6×2 mm", "Neodymium magnet 6×2 mm"), ("Magnete", "Magnets"), 100, 8.90, 140, 30),
            ("beutel", "🛍️", ("Organza-Beutel", "Organza bag"), ("Verpackung", "Packaging"), 50, 7.50, 38, 10)):
        acc[key] = zk.save_accessory(store, None, icon=icon, name=L(de_en), grp=L(grp), pack_qty=pq, pack_price=pp,
                                     unit_price=round(pp / pq, 4), stock=st, warn_at=wa)
    tags = [j for j in jobs if j["obj"][0] == "Schlüsselanhänger mit Namen" and j["cust"] is not None
            and j["status"] == "fertig" and not j["scrap"]]
    for k, j in enumerate(tags):
        store.update(j["id"], prep_min=8, post_min=1.5, design_min=20 if k == 0 else None)
        zk.add_extra(store, j["id"], acc["ring"], 1, True)
        if k % 2 == 0:
            zk.add_extra(store, j["id"], acc["kette"], 1, True)
        if k % 3 == 0:
            zk.add_extra(store, j["id"], acc["beutel"], 1, True)
        zk.refresh(store, j["id"])
    others = [j for j in jobs if j["cust"] is not None and j["status"] == "fertig" and not j["scrap"]
              and j["obj"][0] != "Schlüsselanhänger mit Namen"]
    for k, j in enumerate(others[::9][:8]):
        store.update(j["id"], prep_min=(5, 10, 15)[k % 3], post_min=(2, 5, 10)[k % 3], post_once=1)
        if k % 3 == 1:
            zk.add_extra(store, j["id"], acc["magnet"], 2, True)
        zk.refresh(store, j["id"])

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
        # Standard: Marge auch auf Arbeitszeit
        due = ((job.get("cost_total") or 0) + (job.get("cost_work") or 0)) * (1 + (c.get("margin") or 0) / 100)
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

    for j in tags + others[::9][:8]:
        zk.on_delivered(store, j["id"])
    if tags:
        zk.set_mounted(store, tags[-1]["id"], max(1, (tags[-1]["qty"] or 1) // 2))

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

    pid = _project(store, jobs, cust_ids, {j["id"] for j in trash}, now, L, image_dir)
    _split(store, jobs, cust_ids, {j["id"] for j in trash}, now, L, image_dir, pid)

    _models(store, image_dir, L)
    _locations(store, L)
    _orders(store, cust_ids, pid, now, L)

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
