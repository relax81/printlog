"""Kosten vorab kalkulieren: MakerWorld-Daten lesen, Drucker erkennen, Kosten je Drucker, Übernahme in Aufträge.

Aufruf: python -m pytest tests
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import calc  # noqa: E402
from app import ocr  # noqa: E402
from app import orders as od  # noqa: E402
from app import plates as pl  # noqa: E402
from app import printers as pcfg  # noqa: E402
from app.db import Store  # noqa: E402


def _store() -> Store:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    pcfg.save(store, [{"key": "h2s", "name": "H2S"}, {"key": "a1m", "name": "A1 mini", "fixed_w": 100}])
    store.set_printer_rate("h2s", purchase_eur=1000, life_hours=5000, maint_per_1000h=100)  # 0,30 €/h
    store.set_printer_rate("a1m", purchase_eur=300, life_hours=3000, maint_per_1000h=None)  # 0,10 €/h
    store.set_material_price("PLA", "type", 20.0)
    store.set_material_price("PETG", "type", 25.0)
    now = time.time()
    # Verlauf: H2S braucht 0,25 kW, Strom kostet 0,30 €/kWh
    store.insert(printer="h2s", task_name="x", state="done", status="fertig", start_ts=now - 7200, end_ts=now,
                 kwh=0.5, cost=0.15, created_at=now)
    return store


def _html(design: dict) -> str:
    data = {"props": {"pageProps": {"design": design}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


DESIGN = {
    "title": "Testmodell",
    "instances": [
        {"id": 11, "title": "Standard", "isDefault": True, "prediction": 3600, "weight": 30,
         "extention": {"modelInfo": {
             "compatibility": {"devProductName": "H2S"}, "otherCompatibility": [{"devProductName": "P1S"}],
             "plates": [{"index": 1, "name": "Teil A", "prediction": 1800, "weight": 20, "thumbnail": {"url": "https://x/p1.png"},
                         "filaments": [{"type": "PLA", "color": "#FF0000", "usedG": "15"}, {"type": "PETG", "color": "#00FF00FF", "usedG": "5"}]},
                        {"index": 2, "name": "", "prediction": 1800, "weight": 10,
                         "filaments": [{"type": "PLA", "color": "#0000FF", "usedG": "10"}]}]}}},
        {"id": 12, "title": "Ohne Platten", "prediction": 7200, "weight": 50,
         "instanceFilaments": [{"type": "PLA Basic", "color": "#FFFFFF", "usedG": "50"}],
         "extention": {"modelInfo": {"compatibility": {"devProductName": "Bambu Lab A1 mini"}}}},
    ],
}


def test_makerworld_lesen() -> None:
    d = calc.parse_makerworld(_html(DESIGN), "https://makerworld.com/de/models/1-test")
    assert d["title"] == "Testmodell" and d["profile"] == 0 and len(d["profiles"]) == 2
    p = d["profiles"][0]
    assert p["printer"] == "H2S" and p["compatible"] == ["H2S", "P1S"]
    assert [x["minutes"] for x in p["plates"]] == [30, 30] and p["plates"][0]["name"] == "Teil A"
    assert p["plates"][0]["fil"][1] == {"type": "PETG", "name": "PETG", "color": "#00FF00", "grams": 5.0}
    assert p["plates"][0]["thumb"] == "https://x/p1.png"
    # Profil ohne Plattendaten: Gesamtwerte als eine Platte, Typ aus „PLA Basic“
    q = d["profiles"][1]
    assert len(q["plates"]) == 1 and q["plates"][0]["minutes"] == 120 and q["plates"][0]["fil"][0]["type"] == "PLA"
    # Profil aus dem Link wählen
    d2 = calc.parse_makerworld(_html(DESIGN), "https://makerworld.com/de/models/1-test#profileId-12")
    assert d2["profile"] == 1
    assert calc.parse_makerworld("<html>kein JSON</html>") is None
    assert calc.parse_makerworld(_html({"title": "leer", "instances": []})) is None


def test_link_pruefen() -> None:
    assert calc.is_makerworld("https://makerworld.com/de/models/1")
    assert calc.is_makerworld("https://www.makerworld.com/en/models/1")
    assert not calc.is_makerworld("https://www.printables.com/model/1")
    assert not calc.is_makerworld("https://makerworld.com.evil.example/x")


def test_drucker_erkennen() -> None:
    names = {"h2s": "H2S", "a1m": "A1 mini", "x1c": "X1 Carbon", "p1s": "Werkstatt P1S"}
    assert calc.match_printer(names, "Bambu Lab H2S") == "h2s"
    assert calc.match_printer(names, "A1 mini") == "a1m"
    assert calc.match_printer(names, "X1C") == "x1c"
    assert calc.match_printer(names, "A1") is None
    assert calc.match_printer(names, None) is None


def test_kosten_je_drucker() -> None:
    store = _store()
    d = {"plates": [{"minutes": 120, "fil": [{"key": "PLA", "grams": 100}, {"key": "PETG", "grams": 40}]}],
         "hint": "Bambu Lab H2S", "compatible": ["H2S"], "sets": 2, "pieces": 3}
    q = calc.quote(store, d)
    rows = {r["key"]: r for r in q["rows"]}
    assert abs(q["filament"] - 3.0) < 1e-9  # 0,1 kg × 20 € + 0,04 kg × 25 €
    h = rows["h2s"]
    assert abs(h["energy"] - 0.15) < 1e-9 and abs(h["printer"] - 0.6) < 1e-9 and abs(h["total"] - 3.75) < 1e-9
    a = rows["a1m"]  # feste Leistung 100 W, gleiche Zeit
    assert abs(a["energy"] - 0.06) < 1e-9 and abs(a["printer"] - 0.2) < 1e-9 and a["compat"] is False
    assert q["detected"] == "h2s" and q["sel"] == "h2s" and q["cheapest"] == "h2s"  # A1 mini laut Liste nicht kompatibel
    assert abs(q["cost"] - 7.5) < 1e-9 and abs(q["per_piece"] - 7.5 / 6) < 1e-9 and q["price"] is not None
    # anderer Drucker gewählt, Platte abgewählt zählt nicht
    d["printer"] = "a1m"
    d["plates"].append({"on": False, "minutes": 999, "fil": [{"key": "PLA", "grams": 999}]})
    q2 = calc.quote(store, d)
    assert q2["sel"] == "a1m" and abs(q2["per_set"] - 3.26) < 1e-9 and q2["n_plates"] == 1


def test_marge_arbeit_zubehoer_und_fehlende_preise() -> None:
    store = _store()
    store.set_setting("margin1_pct", "50")
    store.set_setting("work_rate", "30")
    store.set_setting("work_margin_default", "0")
    cid = store.save_customer(None, "Café", None, None, 20)
    d = {"plates": [{"minutes": 60, "fil": [{"key": "PLA", "grams": 50}, {"key": "ASA", "grams": 10}]}],
         "printer": "h2s", "work_min": 10, "extras": 0.5, "margin": "m1"}
    q = calc.quote(store, d)
    base = 1.0 + 0.075 + 0.3 + 0.5  # Filament + Strom + Drucker + Zubehör
    assert abs(q["cost"] - base) < 1e-9 and abs(q["work"] - 5.0) < 1e-9
    assert abs(q["price"] - (base * 1.5 + 5.0)) < 1e-9
    assert q["no_price"] == ["ASA"]
    d["margin"] = f"c{cid}"
    assert abs(calc.quote(store, d)["price"] - (base * 1.2 + 5.0)) < 1e-9
    prm = calc.params(store)
    assert any(m.get("customer") == "Café" for m in prm["margins"])


def test_uebernahme_in_auftrag() -> None:
    store = _store()
    d = {"title": "Set", "url": "https://makerworld.com/de/models/1", "sets": 2, "pieces": 4, "printer": "h2s",
         "plates": [{"name": "Teil A", "minutes": 60, "fil": [{"key": "PLA", "grams": 50}]},
                    {"name": "Teil B", "minutes": 120, "fil": [{"key": "PLA", "grams": 100}]}]}
    q = calc.quote(store, d)
    f = calc.item_fields(d, q)
    assert f["qty"] == 2 and f["n_plates"] == 2 and f["per_plate"] is None and f["est_min"] == 90
    assert abs(f["est_cost"] - q["per_set"] / 2) < 1e-4
    oid = od.create_order(store, None, "Set", None, None, None)
    name, qty = f.pop("name"), f.pop("qty")
    iid = od.add_item(store, oid, None, name, qty, **f)
    pl.save_plates(store, {"id": iid, "model_id": None}, calc.plate_names(d))
    o = od.order(store, oid)
    it = o["items"][0]
    assert it["by_plate"] == 1 and it["url"] == d["url"]
    assert [p["name"] for p in pl.plates_of(store, it)] == ["Teil A", "Teil B"]
    est = od.estimate(o)
    assert est["plates"] == 4 and abs(est["rest_min"] - 360) < 1e-6
    assert est["price"] is not None and abs(est["price"] - q["per_set"] * 2) < 0.01

    # eine Platte: Stück pro Platte, Kosten pro Stück = Kosten pro Platte ÷ Stück
    d1 = {"title": "Einzeln", "sets": 3, "pieces": 2, "printer": "h2s",
          "plates": [{"minutes": 60, "fil": [{"key": "PLA", "grams": 50}]}]}
    q1 = calc.quote(store, d1)
    f1 = calc.item_fields(d1, q1)
    assert f1["qty"] == 6 and f1["per_plate"] == 2 and f1["n_plates"] == 1
    oid1 = od.create_order(store, None, "Einzeln", None, None, None)
    od.add_item(store, oid1, None, f1.pop("name"), f1.pop("qty"), **f1)
    e1 = od.estimate(od.order(store, oid1))
    assert e1["plates"] == 3 and abs(e1["price"] - q1["per_set"] * 3) < 0.01


def test_seiten() -> None:
    store = _store()
    html = calc.page_calc(store)
    assert 'data-kc=' in html and "calc/order" in html
    o = od.create_order(store, None, "A", None, None, None)
    assert 'class="kdlg"' in od.page_new(store) and 'data-order="' in od.page_order(store, od.order(store, o))
    assert 'href="calc"' in od.page_orders(store)
    d = calc.demo_sample("https://makerworld.com/de/models/1")
    assert len(d["profiles"]) == 2 and len(d["profiles"][1]["plates"]) == 3



def _ln(text: str, x: float = 0, cy: float = 10, h: float = 14) -> dict:
    """Zeile wie aus ocr.lines (Wörter mit Position)."""
    ws, pos = [], x
    for w in text.split():
        ws.append({"text": w, "x": pos, "y": cy - h / 2, "w": len(w) * 7, "h": h})
        pos += len(w) * 7 + 6
    return {"text": text, "words": ws, "x": x, "cy": cy, "h": h}


def test_dauer_lesen() -> None:
    assert ocr.duration("3h40m") == 220 and ocr.duration("17m0s") == 17 and ocr.duration("1d2h") == 1560
    assert ocr.duration("21m52s") == round(21 + 52 / 60, 1) and ocr.duration("–") is None


def test_screenshot_bambu() -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 140), (38, 38, 39))
    d = ImageDraw.Draw(img)
    d.rectangle((20, 13, 33, 26), fill=(244, 238, 42))   # gelbes Kästchen vor Zeile 1
    d.rectangle((20, 39, 33, 52), fill=(63, 142, 67))    # grünes vor Zeile 2
    d.rectangle((24, 42, 28, 48), fill=(230, 230, 230))  # Ziffer im Kästchen
    ls = [_ln("~ Bambu Lab H3C", 200, 120), _ln("6.44g / 2.12m 0.4 Std 17m0s", 140, 20),
          _ln("2 7.479 1 2.46m 0.4 Std 23m46s", 120, 46),  # „g“ als „9“ gelesen
          _ln("| 741g | 36.95m 0.4 Std 2h37m", 120, 72),   # Komma verloren: 7.41 g
          _ln("Total 21.32g / 44.01m 3h40m", 100, 98), _ln("Total time: 3h40m", 100, 130)]
    r = ocr.parse_bambu(ls, img)
    fil = r["plates"][0]["fil"]
    assert r["printer"] == "H3C" and r["plates"][0]["minutes"] == 220
    assert [f["grams"] for f in fil] == [6.44, 7.47, 7.41]
    assert any("korrigiert" in w for w in r["warn"])
    assert fil[0]["color"] == "#F4EE2A" and fil[1]["color"] == "#3F8E43"
    assert ocr.parse_bambu([_ln("Hallo Welt")]) is None


def test_screenshot_makerworld() -> None:
    ls = [_ln("0.08mm layer, 2 walls, 100% infill", 0, 10), _ln("A1,X1 Carbon,H2S,P1S,A1", 0, 30), _ln("-", 0, 36),
          _ln("mini,H3C", 0, 42), _ln("{A 2 Platten © 1.5h @ 30g & 0.4mm", 0, 70), _ln("Ams PLAI7g PETG | 13 g", 0, 90)]
    r = ocr.parse_makerworld([ls, []])
    assert r["compatible"] == ["A1", "X1 Carbon", "H2S", "P1S", "A1 mini", "H3C"]
    assert len(r["plates"]) == 2 and r["plates"][0]["minutes"] == 45
    grams = [f["grams"] for f in r["plates"][0]["fil"]]
    assert grams == [3.5, 6.5, 5.0]  # 7 g PLA, 13 g PETG, Rest 10 g – je Platte halbiert
    assert [f["type"] for f in r["plates"][0]["fil"]][:2] == ["PLA", "PETG"]
    assert ocr.parse_makerworld([[_ln("nichts")], []]) is None



def test_screenshot_makerworld_punkt_und_chips() -> None:
    from PIL import Image, ImageDraw

    # „1.5 h“ ohne erkannten Punkt: 15 h für 8 g ist unplausibel → 1,5 h; „S“ statt 5 im Chip
    ls = [_ln("{A 1 Platte © 15h @ 8g & 0.4mm", 0, 20), _ln("Ams PLAI3g PLAISg", 0, 40)]
    r = ocr.parse_makerworld([ls, []])
    assert r["plates"][0]["minutes"] == 90 and any("Dezimalpunkt" in w for w in r["warn"])
    assert [f["grams"] for f in r["plates"][0]["fil"]] == [3.0, 5.0]
    # lange Drucke bleiben, wie sie sind
    r2 = ocr.parse_makerworld([[_ln("1 Platte 15h 300g", 0, 20)], []])
    assert r2["plates"][0]["minutes"] == 900
    # Chips im Bild: weißes und schwarzes Rechteck auf dunklem Grund, von links nach rechts
    img = Image.new("RGB", (300, 60), (36, 36, 36))
    d = ImageDraw.Draw(img)
    d.rectangle((10, 20, 70, 42), fill=(255, 255, 255))
    d.rectangle((90, 20, 150, 42), fill=(0, 0, 0))
    d.rectangle((100, 26, 104, 36), fill=(255, 255, 255))  # Schrift im schwarzen Chip
    boxes = ocr._chip_boxes(img, 0, 60)
    assert [b[0] for b in boxes] == [10, 90] and ocr._box_color(img, boxes[0]) == "#FFFFFF"
    # „g“ als Ziffer gelesen („3g“ → 38): passt erst ohne letzte Ziffer zur Gesamtmenge
    assert [g for _, g, _ in ocr._match_total([("PLA", 38.0, None), ("PLA", 5.0, None)], 8)] == [3.0, 5.0]
    assert [g for _, g, _ in ocr._match_total([("PLA", 30.0, None)], 30)] == [30.0]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
