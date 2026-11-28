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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
