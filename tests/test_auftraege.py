"""Aufträge und Favoriten: Positionen, Fortschritt, Stand, Zuordnen, Abrechnen, Schätzung und Seiten.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models as mdl  # noqa: E402
from app import orders as od  # noqa: E402
from app.customer import page_customer  # noqa: E402
from app.db import Store  # noqa: E402


def _store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "t.db")


def _job(store: Store, name: str, qty: int = 4, mid: int | None = None, state: str = "done", ago: float = 3600, **extra) -> int:
    now = time.time()
    f = dict(printer="h2s", task_name=name, state=state, status="fertig" if state == "done" else "läuft", start_ts=now - ago,
             end_ts=now - ago + 7200 if state == "done" else None, cost_total=2.0, cost_work=0.0, filament_g=40.0,
             quantity=qty, model_id=mid, model_state="auto" if mid else None, created_at=now)
    f.update(extra)
    return store.insert(**f)


def test_fortschritt_stand_und_abrechnung() -> None:
    store = _store()
    cid = store.save_customer(None, "Café", None, None, 25)
    mid = mdl.create_model(store, "Untersetzer")
    old = _job(store, "Untersetzer", 8, mid, ago=10 * 86400)  # Vorlage für die Schätzung (8 Stk., 2 h)
    oid = od.create_order(store, cid, "Nachbestellung", "2030-01-05", None, None)
    o = od.order(store, oid)
    assert o["state"] == "open" and o["items"] == []

    it = od.add_item(store, oid, mid, None, 16)
    free = od.add_item(store, oid, None, "Halter Akkuschrauber", 2)
    assert od.add_item(store, oid, mid, None, 4) == it  # gleiches Modell: Stückzahl erhöht
    o = od.order(store, oid)
    assert [i["qty"] for i in o["items"]] == [20, 2] and o["items"][1]["model_id"] is None
    est = od.estimate(o)
    assert est["plates"] == 3 + 2 and round(est["rest_min"]) == 3 * 120  # Freitext ohne Schätzung
    assert est["missing"] >= 1
    assert round(est["price"], 2) == round(20 * 0.25 * 1.25, 2)

    run = _job(store, "Untersetzer", 8, mid, state="running")
    assert od.link(store, run, it)
    assert store.get(run)["customer_id"] == cid  # Kunde kommt vom Auftrag
    assert od.order(store, oid)["state"] == "running"
    assert od.bill(store, oid)[1]  # läuft noch: nicht abrechnen

    store.update(run, state="done", status="fertig", end_ts=time.time())
    bad = _job(store, "Untersetzer", 8, mid, scrap=1)
    good = _job(store, "Untersetzer", 12, mid)
    for j in (bad, good):
        od.link(store, j, it)
    o = od.order(store, oid)
    assert o["items"][0]["done"] == 20  # Fehldruck zählt nicht
    assert o["state"] == "running"  # Freitext fehlt noch
    od.set_item(store, free, adjust=2)  # von Hand als gedruckt gezählt
    assert od.order(store, oid)["state"] == "done"

    od.save_order(store, oid, agreed_price=30.0)
    target, err = od.bill(store, oid)
    assert err is None and target
    proj = store.get(target)
    assert proj["kind"] == "project" and proj["final_price"] == 30.0 and proj["customer_id"] == cid
    o = od.order(store, oid)
    assert o["state"] == "billed"
    store.add_payment(target, 30.0)
    store.add_handover(target, proj.get("quantity") or 1)
    assert od.order(store, oid)["state"] == "archived"
    od.reopen(store, oid)
    assert od.order(store, oid)["state"] == "done"
    assert store.get(old)["order_item_id"] is None


def test_einzelner_druck_loeschen_und_zuordnen() -> None:
    store = _store()
    cid = store.save_customer(None, "Lena", None, None)
    mid = mdl.create_model(store, "Anhänger")
    oid = od.create_order(store, cid, "Kita", None, 12.5, None)
    it = od.add_item(store, oid, mid, None, 10)
    j = _job(store, "Anhänger", 10, mid)
    other = _job(store, "Vase", 1)
    o = od.order(store, oid)
    cands = [c["id"] for c in od.candidates(store, o)]
    assert cands[0] == j and other in cands  # passendes Modell zuerst
    od.link(store, j, it)
    target, err = od.bill(store, oid)
    assert target == j and err is None  # ein Druck: kein Projekt nötig
    assert store.get(j)["final_price"] == 12.5
    assert 'href="order?id=' in od.job_chip(store, store.get(j))

    od.delete_order(store, oid)
    assert od.order(store, oid) is None and store.get(j)["order_item_id"] is None


def test_favoriten_wieder_drucken_und_seiten() -> None:
    store = _store()
    cid = store.save_customer(None, "Tom", None, None, 20)
    mid = mdl.create_model(store, "Halter")
    for k in range(3):
        _job(store, "Halter", 6, mid, ago=(k + 1) * 86400, customer_id=cid)
    mdl.save_model(store, mid, fav=1)
    assert mdl.model(store, mid)["fav"] == 1
    assert od.top_customers(store)[mid][:2] == (cid, "Tom")
    assert [m["id"] for m in od.often(store, cid)] == [mid]

    oid = od.again(store, mid, cid, 12)
    o = od.order(store, oid)
    assert o["title"] == "Halter" and o["items"][0]["qty"] == 12 and o["customer_id"] == cid
    assert od.again(store, mid, cid, 6, into=oid) == oid
    assert od.order(store, oid)["items"][0]["qty"] == 18

    html = mdl.page_models(store, flt="fav")
    assert "Halter" in html and "mstar on" in html and "meist für Tom (3×)" in html and 'id="againdlg"' in html
    assert "Halter" not in mdl.page_models(store, flt="old").split('class="ml"')[1].split("</div>")[0]
    assert "Bestellt oft" in page_customer(store, cid) and "Aufträge" in page_customer(store, cid)
    board = od.page_orders(store)
    assert "Halter" in board and "Tom" in board
    assert "18" in od.page_order(store, od.order(store, oid))
    assert "Neuer Auftrag" in od.page_new(store, cid)

    # Modell zusammenlegen bzw. löschen: Positionen folgen bzw. werden Freitext
    m2 = mdl.create_model(store, "Halter 2")
    mdl.merge(store, mid, m2)
    assert od.order(store, oid)["items"][0]["model_id"] == m2 and mdl.model(store, m2)["fav"] == 1
    mdl.delete_model(store, m2)
    it = od.order(store, oid)["items"][0]
    assert it["model_id"] is None and it["label"] == "Halter 2"
