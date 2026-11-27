"""Platten pro Position: Sätze, Auswahlgruppen, aus Vorrat, Zuordnung per Plattennummer.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models as mdl  # noqa: E402
from app import orders as od  # noqa: E402
from app import plates as pl  # noqa: E402
from app.db import Store  # noqa: E402


def _store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "t.db")


def _job(store: Store, name: str, plate: int | None, mid: int | None = None, qty: int = 1, state: str = "done",
         ago: float = 3600, **extra) -> int:
    now = time.time()
    f = dict(printer="h2s", task_name=name, state=state, status="fertig" if state == "done" else "läuft", start_ts=now - ago,
             end_ts=now - ago + 3600 if state == "done" else None, cost_total=1.0, cost_work=0.0, filament_g=30.0,
             quantity=qty, model_id=mid, model_state="auto" if mid else None, gcode_plate=plate, created_at=now,
             materials='[{"slot":1,"type":"PLA","color":"#FF0000","grams":30}]')
    f.update(extra)
    return store.insert(**f)


def test_saetze_auswahl_und_zuordnung() -> None:
    store = _store()
    cid = store.save_customer(None, "Verein", None, None, 0)
    mid = mdl.create_model(store, "Lochwand-Set")
    for p, q in ((1, 1), (2, 8), (3, 1)):
        _job(store, "Lochwand-Set", p, mid, q, ago=20 * 86400)  # gelernte Platten
    oid = od.create_order(store, cid, "Set", None, None, None)
    it = od.add_item(store, oid, mid, None, 2)
    o = od.order(store, oid)
    i = o["items"][0]
    assert i["by_plate"] == 1 and i["pl"] and [p["plate"] for p in i["pl"]["plates"]] == [1, 2, 3]
    assert i["pl"]["n_print"] == 6 and i["done"] == 0 and i["qty"] == 2
    assert i["pl"]["plates"][1]["pieces"] == 8

    # Platte 2 und 3 sind eine Auswahl „Design“: pro Satz eine davon, Standard die erste
    pl.save_plates(store, i, [{"plate": 2, "name": "Design A", "grp": "Design"}, {"plate": 3, "name": "Design B", "grp": "Design"}])
    i = od.order(store, oid)["items"][0]
    assert i["pl"]["n_print"] == 4
    pl.set_cell(store, i, 2, 3)  # Satz 2 nimmt Design B
    i = od.order(store, oid)["items"][0]
    want = i["pl"]["grid"]["want"]
    assert want[(2, 3)] == "print" and want[(2, 2)] == "skip" and want[(1, 2)] == "print"

    # Drucke zählen bei ihrer Platte
    store.set_setting("orders_since", str(time.time() - 600))
    a = _job(store, "Lochwand-Set", 1, None, ago=300)
    b = _job(store, "Lochwand-Set", 3, None, ago=200)
    od.match_pending(store)
    assert store.get(a)["order_item_id"] == it and store.get(b)["order_item_id"] == it
    assert store.get(b)["quantity"] == 1  # Stück dieser Platte, nicht die 8 von Platte 2
    i = od.order(store, oid)["items"][0]
    cells = i["pl"]["grid"]["cells"]
    assert cells[(1, 1)]["state"] == "done" and cells[(2, 3)]["state"] == "done" and cells[(1, 2)]["state"] == "open"
    # Platte 3 wird nicht mehr gebraucht: ein weiterer Druck davon passt nicht
    c = _job(store, "Lochwand-Set", 3, None, state="running", ago=100)
    od.match_pending(store)
    assert store.get(c)["order_item_id"] is None
    # Platte 2 noch offen (Satz 1): läuft
    d = _job(store, "Lochwand-Set", 2, None, qty=None, state="running", ago=50)
    od.match_pending(store)
    assert store.get(d)["order_item_id"] == it and store.get(d)["quantity"] == 8
    o = od.order(store, oid)
    assert o["state"] == "running" and od.estimate(o)["plates"] == 1  # Platte 1 für Satz 2

    # Satz 2, Platte 1 aus dem Vorrat: zwei Sätze, sobald Platte 2 fertig ist
    i = o["items"][0]
    pl.set_cell(store, i, 2, 1)
    store.update(d, state="done", status="fertig", end_ts=time.time())
    o = od.order(store, oid)
    assert o["items"][0]["done"] == 2 and o["state"] == "done"
    assert "Lochwand-Set" in od.page_order(store, o)


def test_vorrat_ausbuchen_und_freitext() -> None:
    store = _store()
    cid = store.save_customer(None, "Kunde", None, None, 0)
    mid = mdl.create_model(store, "Figur")
    _job(store, "Figur", 2, mid, ago=10 * 86400)
    par = _job(store, "Figur", 1, mid, qty=5, ago=9 * 86400, customer_id=cid)
    assert store.set_split(par, [{"sid": None, "customer_id": cid, "qty": 2}]) is None  # 3 im Vorrat
    _job(store, "Figur", 1, mid, qty=1, ago=8 * 86400)  # zuletzt 1 Stück pro Platte 1
    oid = od.create_order(store, cid, "Figuren", None, 10.0, None)
    od.add_item(store, oid, mid, None, 2)
    i = od.order(store, oid)["items"][0]
    p1 = i["pl"]["plates"][0]
    assert pl.stock_for(store, mid, p1)["pieces"] == 3
    for s in (1, 2):
        pl.set_cell(store, i, s, 1)  # Platte 1 → aus Vorrat
    for s in (1, 2):
        j = _job(store, "Figur", 2, mid, ago=100 + s)
        od.link(store, j, i["id"])
    o = od.order(store, oid)
    assert o["state"] == "done" and o["items"][0]["pl"]["n_stock"] == 2
    target, note = od.bill(store, oid)
    assert target and note is None
    assert pl.stock_for(store, mid, p1)["pieces"] == 1  # 2 ausgebucht
    given = [s for s in store.shares([par])[par] if s["share_kind"] == "stock"]
    assert sum(g["quantity"] for g in given) == 2 and all(g["final_price"] == 0 for g in given)  # im Preis enthalten

    # Freitext mit 3 Platten; Drucke ohne Plattennummer füllen die ersten offenen Felder
    oid2 = od.create_order(store, cid, "Neu", None, None, None)
    iid = od.add_item(store, oid2, None, "MakerWorld Drache", 1)
    it = od.order(store, oid2)["items"][0]
    pl.save_plates(store, it, [], 3)
    od.set_item(store, iid, by_plate=1)
    it = od.order(store, oid2)["items"][0]
    assert [p["plate"] for p in it["pl"]["plates"]] == [1, 2, 3]
    od.link(store, _job(store, "MakerWorld Drache", None), iid)
    it = od.order(store, oid2)["items"][0]
    assert it["pl"]["grid"]["cells"][(1, 1)]["state"] == "done" and it["pl"]["n_open"] == 2
    # Plattennamen der Freitext-Position gehen ans Modell, sobald es eins gibt
    pl.save_plates(store, it, [{"plate": 2, "name": "Flügel"}])
    m2 = mdl.create_model(store, "MakerWorld Drache")
    od.link(store, _job(store, "MakerWorld Drache", 2, m2, model_state="new"), iid)
    it = od.order(store, oid2)["items"][0]
    assert it["model_id"] == m2 and it["pl"]["plates"][1]["name"] == "Flügel"
    od.delete_order(store, oid2)
    assert not store._conn.execute("SELECT 1 FROM plate_cells WHERE item_id = ?", (iid,)).fetchone()


def test_gemeinsame_teile_und_farbe() -> None:
    store = _store()
    cid = store.save_customer(None, "Verein", None, None, 0)
    other = store.save_customer(None, "Andere", None, None, 0)
    kopf = mdl.create_model(store, "Kopf + Hände")
    blau = mdl.create_model(store, "Figur Blau")
    rot = mdl.create_model(store, "Figur Rot")
    haut = '[{"slot":1,"type":"PLA","color":"#F1C27D","grams":10}]'
    gelb = '[{"slot":1,"type":"PLA","color":"#FFE000","grams":10}]'
    # 6 Köpfe als eigenes Modell gedruckt, einer verkauft → 5 im Vorrat (hautfarben)
    k = _job(store, "Kopf + Hände", 1, kopf, qty=6, ago=20 * 86400, customer_id=other, materials=haut)
    store.set_split(k, [{"sid": None, "customer_id": other, "qty": 1}])
    # Figur Blau, Platte 1 = gemeinsames Teil; 3 Köpfe in Gelb übrig
    b1 = _job(store, "Figur Blau", 1, blau, qty=4, ago=15 * 86400, customer_id=other, materials=gelb)
    store.set_split(b1, [{"sid": None, "customer_id": other, "qty": 1}])
    _job(store, "Figur Blau", 1, blau, qty=1, ago=14 * 86400, materials=gelb)
    _job(store, "Figur Blau", 2, blau, ago=14 * 86400)
    pl.save_plates(store, {"id": 0, "model_id": blau}, [{"plate": 1, "name": "Kopf + Hände", "part_model_id": kopf}])
    # Figur Rot: noch nie gedruckt, Platten von Hand angelegt
    pl.save_plates(store, {"id": 0, "model_id": rot}, [{"plate": 1, "name": "Kopf + Hände", "part_model_id": kopf},
                                                       {"plate": 2, "name": "Trikot Rot"}])
    assert pl.has_plates(store, rot)
    oid = od.create_order(store, cid, "Vereinsfiguren", None, None, None)
    iid = od.add_item(store, oid, rot, None, 3)
    i = od.order(store, oid)["items"][0]
    assert i["by_plate"] == 1 and i["pl"]["n_print"] == 6
    sug = pl.suggestions(store, i)
    assert len(sug) == 1 and sug[0]["plate"] == 1 and sug[0]["shared"]
    assert sug[0]["match"] == 5 and sug[0]["pieces"] == 8 and sug[0]["n_sets"] == 3  # Gelb zählt nicht als passend
    assert sug[0]["other_colors"] == ["#FFE000"]
    html = od.page_order(store, od.order(store, oid))
    assert "Kopf + Hände" in html and "anderer Farbe" in html
    assert pl.use_stock(store, i, 1) == 3
    i = od.order(store, oid)["items"][0]
    assert i["pl"]["n_stock"] == 3 and i["pl"]["n_open"] == 3 and not pl.suggestions(store, i)
    for s in range(3):
        od.link(store, _job(store, "Figur Rot", 2, rot, ago=100 + s), iid)
    o = od.order(store, oid)
    assert o["state"] == "done"
    target, note = od.bill(store, oid)
    assert target and note is None
    left = pl.stock_for(store, rot, od.order(store, oid)["items"][0]["pl"]["plates"][0])
    assert left["match"] == 2 and left["pieces"] == 5  # hautfarbene zuerst genommen, die gelben bleiben

    # Nur andere Farbe im Vorrat: Vorschlag „trotzdem“
    oid2 = od.create_order(store, cid, "Noch mehr", None, None, None)
    od.add_item(store, oid2, rot, None, 4)
    i2 = od.order(store, oid2)["items"][0]
    sg = pl.suggestions(store, i2)[0]
    assert sg["match"] == 2 and sg["n_sets"] == 2
    assert pl.use_stock(store, i2, 1, 4) == 4  # 2 passende + 2 gelbe
    # Löschen des gemeinsamen Modells: Verweise werden gelöst
    mdl.delete_model(store, kopf)
    assert not store._conn.execute("SELECT 1 FROM plate_info WHERE part_model_id = ?", (kopf,)).fetchone()


def test_feld_explizit_waehlen() -> None:
    store = _store()
    cid = store.save_customer(None, "Verein", None, None, 0)
    mid = mdl.create_model(store, "Set")
    for p in (1, 2):
        _job(store, "Set", p, mid, ago=20 * 86400)
    oid = od.create_order(store, cid, "Set", None, None, None)
    od.add_item(store, oid, mid, None, 1)
    i = od.order(store, oid)["items"][0]
    pl.set_cell(store, i, 1, 1, "stock")
    i = od.order(store, oid)["items"][0]
    pl.set_cell(store, i, 1, 1, "stock")  # gleiche Wahl nochmal: bleibt, schaltet nicht weiter
    i = od.order(store, oid)["items"][0]
    assert i["pl"]["grid"]["want"][(1, 1)] == "stock"
    pl.set_cell(store, i, 1, 2, "skip")
    i = od.order(store, oid)["items"][0]
    assert i["pl"]["grid"]["want"][(1, 2)] == "skip"


def test_wunschfarbe() -> None:
    store = _store()
    cid = store.save_customer(None, "Verein", None, None, 0)
    mid = mdl.create_model(store, "Figur")
    _job(store, "Figur", 1, mid, ago=20 * 86400)  # rot gedruckt
    _job(store, "Figur", 2, mid, ago=20 * 86400)
    yellow = '[{"slot":1,"type":"PLA","color":"#FFD21F","grams":10}]'
    par = _job(store, "Figur", 1, mid, qty=3, ago=10 * 86400, materials=yellow)
    store.set_split(par, [{"sid": None, "customer_id": cid, "qty": 1}])  # 2 gelbe im Vorrat
    _job(store, "Figur", 1, mid, ago=5 * 86400)  # letzter Druck wieder rot
    oid = od.create_order(store, cid, "Figuren", None, None, None)
    od.add_item(store, oid, mid, None, 2)
    i = od.order(store, oid)["items"][0]
    p = i["pl"]["plates"][0]
    assert p["want"] == [] and pl.stock_for(store, mid, p)["match"] == 0  # rot gewünscht (letzter Druck)
    pl.save_plates(store, i, [{"plate": 1, "colors": "ffd21f, #zz"}])
    i = od.order(store, oid)["items"][0]
    p = i["pl"]["plates"][0]
    assert p["want"] == ["#FFD21F"] and pl.stock_for(store, mid, p)["match"] == 2
    pl.save_plates(store, i, [{"plate": 1, "colors": ""}])  # leer: wieder wie der letzte Druck
    assert od.order(store, oid)["items"][0]["pl"]["plates"][0]["want"] == []
