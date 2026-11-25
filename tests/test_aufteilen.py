"""Stückzahl aufteilen: Anteile je Kunde, Vorrat, Abgeben, Ausbuchen, Sätze und Einzelteile bei Projekten.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Store  # noqa: E402
from app.export import bon_png, collect, report_pdf  # noqa: E402
from app.pages import FILTER_KEYS, amount_due, csv_rows, page_job, page_jobs, results  # noqa: E402
from tests.test_projekte import _job  # noqa: E402


def _store() -> tuple[Store, int, int]:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    return store, store.save_customer(None, "Kundin", None, None, 20), store.save_customer(None, "Andere", None, None)


def _split_print(store: Store, kundin: int, andere: int) -> int:
    j = _job(store, kundin, "Clips", cost=10.0, quantity=10)
    store.add_payment(j, 1.0)
    assert store.set_split(j, [{"customer_id": kundin, "qty": 2}, {"customer_id": andere, "qty": 4}]) is None
    return j


def test_aufteilen_verschiebt_zahlung_und_rechnet_anteilig() -> None:
    store, kundin, andere = _store()
    j = _split_print(store, kundin, andere)
    p = store.get(j)
    assert p["split"] == 1 and p["customer_id"] is None
    shares = store.shares([j])[j]
    mine = next(s for s in shares if s["customer_id"] == kundin)
    other = next(s for s in shares if s["customer_id"] == andere)
    assert (mine["quantity"], round(mine["cost_total"], 2)) == (2, 2.0)
    assert (other["quantity"], round(other["cost_total"], 2)) == (4, 4.0)
    assert [x["amount"] for x in store.payments(mine["id"])] == [1.0] and store.payments(j) == []
    assert round(amount_due(store.get(mine["id"])), 2) == 2.4  # 2 € + 20 % Kundenmarge
    assert store.stock(p)["units"] == 4
    # Liste zeigt nur den Druck, „Offen“ die Anteile; Kundenfilter findet den Druck
    assert [x["id"] for x in store.list_jobs()] == [j]
    assert {x["id"] for x in store.list_jobs(pending="1")} == {mine["id"], other["id"]}
    assert [x["id"] for x in store.list_jobs(customer=str(andere))] == [j]
    # Kostenänderung am Druck zieht die Anteile mit
    store.update(j, cost_total=20.0)
    assert round(store.get(other["id"])["cost_total"], 2) == 8.0


def test_zu_viel_vergeben_wird_abgelehnt() -> None:
    store, kundin, andere = _store()
    j = _split_print(store, kundin, andere)
    sid = next(s["id"] for s in store.shares([j])[j] if s["customer_id"] == andere)
    assert store.set_split(j, [{"sid": sid, "customer_id": andere, "qty": 11}]) is not None
    store.add_handover(sid, 3)
    assert store.set_split(j, [{"sid": sid, "customer_id": andere, "qty": 2}]) is not None  # schon 3 übergeben


def test_abgeben_ausbuchen_und_zuruecknehmen() -> None:
    store, kundin, andere = _store()
    j = _split_print(store, kundin, andere)
    sid = store.give(j, andere, 3, handed=True, final_price=5.0)
    assert isinstance(sid, int)
    s = store.get(sid)
    assert s["share_kind"] == "stock" and s["delivered"] == 3 and s["final_price"] == 5.0
    assert isinstance(store.give(j, kundin, 2), str)  # nur noch 1 im Vorrat
    out = store.give(j, None, 1, note="kaputt")
    assert store.get(out)["share_kind"] == "out" and store.stock(store.get(j))["units"] == 0
    assert store.stock_items() == []
    assert store.remove_share(out) and store.stock(store.get(j))["units"] == 1
    assert not store.remove_share(sid)  # hat eine Übergabe


def test_projekt_saetze_und_einzelteile() -> None:
    store, kundin, andere = _store()
    a = _job(store, None, "Kopf", cost=5.0, quantity=5)
    b = _job(store, None, "Arme", cost=5.0, quantity=10)
    pid = store.create_project([a, b], name="Figur", quantity=5)
    assert store.set_split(pid, [{"customer_id": kundin, "qty": 2}]) is None
    st = store.stock(store.get(pid))
    assert st["units"] == 3 and st["loose"] == {}
    sid = store.give(pid, andere, 1, part=a)
    assert isinstance(sid, int)
    share = store.get(sid)
    assert round(share["cost_total"], 2) == 1.0 and "Kopf" in share["task_name"]
    st = store.stock(store.get(pid))
    assert st["units"] == 2 and st["loose"] == {b: 2}
    # Satz-Anteil kostet einen Satz (2 € pro Satz)
    mine = next(s for s in store.shares([pid])[pid] if s["customer_id"] == kundin)
    assert round(mine["cost_total"], 2) == 4.0
    assert isinstance(store.give(pid, andere, 3, part=a), str)  # nur noch 2 Köpfe
    # Auflösen hebt die Aufteilung auf
    store.dissolve_project(pid)
    assert store.shares([pid]) == {}


def test_aufheben_gibt_kunde_und_zahlung_zurueck() -> None:
    store, kundin, andere = _store()
    j = _split_print(store, kundin, andere)
    store.unsplit(j)
    p = store.get(j)
    assert not p["split"] and p["customer_id"] == kundin
    assert [x["amount"] for x in store.payments(j)] == [1.0]
    assert store.shares([j]) == {}


def test_papierkorb_mit_anteilen() -> None:
    store, kundin, andere = _store()
    j = _split_print(store, kundin, andere)
    store.give(j, None, 1, note="selbst verwendet")
    store.trash_job(j)
    assert store.get(j) is None and store.list_jobs(pending="1") == []
    store.restore_job(j)
    assert len(store.shares([j])[j]) == 3 and store.stock(store.get(j))["units"] == 3


def test_seiten_csv_und_export() -> None:
    store, kundin, andere = _store()
    j = _split_print(store, kundin, andere)
    f = {k: None for k in FILTER_KEYS}
    html = page_job(store, store.get(j), None)
    assert 'id="split"' in html and "Kundin" in html and "Andere" in html and 'id="spdata"' in html
    assert "Vorrat" in page_jobs(store, f, True)
    assert "Clips" in results(store, {**f, "view": "stock"})
    assert "Kundin" in results(store, {**f, "view": "todo"})
    rows = csv_rows(store, f)
    col = rows[0].index("Aufteilung")
    assert "Kundin 2" in rows[1][col] and "Vorrat 4" in rows[1][col]
    sid = next(s["id"] for s in store.shares([j])[j] if s["customer_id"] == andere)
    assert "Anteil von" in page_job(store, store.get(sid), None)
    data = collect(store, store.get(sid), Path(tempfile.mkdtemp()))
    assert data["customer"] == "Andere" and data["qty"] == 4
    assert bon_png(data, "kunde")[:4] == b"\x89PNG" and report_pdf(data)[:4] == b"%PDF"


def test_stueck_an_projekt() -> None:
    store, kundin, andere = _store()
    x = _job(store, kundin, "Teil X", cost=4.0)
    pid = store.create_project([x], name="Set")
    j = _job(store, None, "Clips", cost=10.0, quantity=10, materials='[{"name": "PLA", "grams": 100}]')
    assert store.set_split(j, [{"customer_id": andere, "qty": 2}, {"project_id": pid, "qty": 4}]) is None
    kids = store.children([pid])[pid]
    share = next(k for k in kids if k["kind"] == "share")
    assert share["quantity"] == 4 and share["customer_id"] == kundin and '"grams": 40.0' in share["materials"]
    assert round(store.get(pid)["cost_total"], 2) == 8.0  # 4 € + 4 von 10 Clips
    assert store.stock(store.get(j))["units"] == 4
    # Projekt rechnet ab, nicht der Anteil; Statistik zählt weiter nur die Drucke
    assert {r["id"] for r in store.list_jobs(paid="offen")} >= {pid} and share["id"] not in {r["id"] for r in store.list_jobs(pending="1")}
    assert store.summary()["total"]["n"] == 2
    # Kosten des Drucks ändern sich → Projekt zieht mit
    store.update(j, cost_total=20.0)
    assert round(store.get(pid)["cost_total"], 2) == 12.0
    html = page_job(store, store.get(j), None)
    assert "Set" in html and "über das Projekt" in html
    # Aus dem Projekt lösen → zurück in den Vorrat
    store.remove_from_project(share["id"])
    assert store.get(share["id"]) is None and store.stock(store.get(j))["units"] == 8
    assert round(store.get(pid)["cost_total"], 2) == 4.0


def test_neues_projekt_und_aufheben() -> None:
    store, kundin, andere = _store()
    j = _job(store, None, "Clips", cost=10.0, quantity=10)
    assert store.set_split(j, [{"new_project": "Werkbank", "qty": 3}]) is None
    share = store.shares([j])[j][0]
    assert share["project_name"] == "Werkbank" and store.get(share["project_id"])["kind"] == "project"
    store.unsplit(j)
    assert store.get(share["project_id"]) is None  # leeres Projekt verschwindet wieder
    pid = store.create_project([_job(store, kundin, "Y")], name="P")
    assert store.set_split(pid, [{"project_id": pid, "qty": 1}]) is not None  # Projekt nicht in Projekt
