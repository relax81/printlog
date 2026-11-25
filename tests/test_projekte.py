"""Projekte: mehrere Drucke als ein Auftrag (bilden, erweitern, lösen, auflösen, Abrechnung, Statistik).

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Store  # noqa: E402
from app.export import bon_png, collect, report_pdf  # noqa: E402
from app.pages import FILTER_KEYS, _todo_view, amount_due, csv_rows, page_job, page_jobs  # noqa: E402


def _job(store: Store, cid: int | None, name: str, cost: float = 2.0, printer: str = "h2s", ago: float = 3600, **extra) -> int:
    now = time.time()
    fields = dict(printer=printer, task_name=name, state="done", status="fertig", start_ts=now - ago,
                  end_ts=now - ago + 1800, cost_total=cost, cost=cost / 4, cost_filament=cost / 2, cost_printer=cost / 4,
                  kwh=0.1, filament_g=10.0, customer_id=cid, quantity=1, created_at=now)
    fields.update(extra)
    return store.insert(**fields)


def _store() -> tuple[Store, int, int]:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    return store, store.save_customer(None, "Kundin", None, None, 20), store.save_customer(None, "Andere", None, None)


def test_bilden_summen_und_kunde() -> None:
    store, kundin, andere = _store()
    a = _job(store, kundin, "A", 2.0, "h2s", 7200)
    b = _job(store, kundin, "B", 3.0, "p1s", 7000)
    c = _job(store, andere, "C", 1.0, "a1mini", 6000)
    store.add_payment(a, 1.0)
    pid = store.create_project([a, b, c], name="Set", quantity=1)
    p = store.get(pid)
    assert p["kind"] == "project" and p["task_name"] == "Set" and p["code"]
    assert round(p["cost_total"], 2) == 6.0 and round(p["filament_g"], 1) == 30.0
    # Häufigster Kunde gewinnt und gilt für alle Drucke
    assert p["customer_id"] == kundin
    assert all(store.get(i)["customer_id"] == kundin for i in (a, b, c))
    assert all(store.get(i)["project_id"] == pid for i in (a, b, c))
    # Zahlung wandert ins Projekt
    assert store.payments(a) == [] and [x["amount"] for x in store.payments(pid)] == [1.0]
    # Kunde am Projekt ändern → Drucke ziehen mit
    store.update(pid, customer_id=andere)
    assert all(store.get(i)["customer_id"] == andere for i in (a, b, c))


def test_abrechnung_nur_auf_projekt() -> None:
    store, kundin, _ = _store()
    a = _job(store, kundin, "A", 2.0)
    b = _job(store, kundin, "B", 3.0)
    pid = store.create_project([a, b], name="Set")
    open_ids = {j["id"] for j in store.list_jobs(paid="offen")}
    assert open_ids == {pid}
    assert round(amount_due(store.get(pid)), 2) == 6.0  # 5 € + 20 %
    html = _todo_view(store, {})
    assert "Set" in html and ">A<" not in html
    k = next(c for c in store.customers() if c["id"] == kundin)
    assert k["n"] == 2 and round(k["cost_total"], 2) == 5.0 and round(k["open_amount"], 2) == 6.0


def test_unberechneter_fehldruck_senkt_basis() -> None:
    store, kundin, _ = _store()
    a = _job(store, kundin, "A", 2.0)
    fd = _job(store, kundin, "Fehldruck", 1.0, scrap=1, defect="settings")
    halb = _job(store, kundin, "Halb", 2.0, scrap=1, defect="settings", defect_charge="pct", defect_value=50.0)
    pid = store.create_project([a, fd, halb], name="Set")
    p = store.get(pid)
    assert round(p["cost_total"], 2) == 5.0
    assert round(p["bill_base"], 2) == 3.0  # 2 + 0 + 50 % von 2
    assert round(amount_due(p), 2) == 3.6


def test_statistik_zaehlt_drucke_nicht_projekt() -> None:
    store, kundin, _ = _store()
    a = _job(store, kundin, "A", 2.0)
    b = _job(store, kundin, "B", 3.0)
    before = store.summary()["total"]
    store.create_project([a, b], name="Set")
    after = store.summary()["total"]
    assert after["n"] == before["n"] == 2
    assert round(after["cost_total"], 2) == round(before["cost_total"], 2) == 5.0
    rows = csv_rows(store, {})
    assert len(rows) == 3 and rows[0][-1] == "Gehört zu Projekt"


def test_hinzufuegen_zusammenlegen_loesen_aufloesen() -> None:
    store, kundin, _ = _store()
    a, b, c, d = (_job(store, kundin, n) for n in "ABCD")
    p1 = store.create_project([a, b], name="Eins")
    p2 = store.create_project([c], name="Zwei")
    store.add_payment(p2, 2.0)
    # Projekt in Projekt: Zwei wird aufgelöst, seine Drucke und Zahlungen landen in Eins
    store.create_project([p2, d], into=p1)
    assert store.get(p2) is None
    assert {k["id"] for k in store.children([p1])[p1]} == {a, b, c, d}
    assert sum(x["amount"] for x in store.payments(p1)) == 2.0
    # Einzelnen Druck lösen
    assert store.remove_from_project(d) == p1
    assert store.get(d)["project_id"] is None
    assert round(store.get(p1)["cost_total"], 2) == 6.0
    # Auflösen: Drucke bleiben, Zahlung geht an den ersten Druck
    store.dissolve_project(p1)
    assert store.get(p1) is None
    assert all(store.get(i)["project_id"] is None for i in (a, b, c))
    assert sum(x["amount"] for i in (a, b, c) for x in store.payments(i)) == 2.0


def test_papierkorb() -> None:
    store, kundin, _ = _store()
    a, b, c = (_job(store, kundin, n) for n in "ABC")
    pid = store.create_project([a, b, c], name="Set")
    store.trash_job(c)
    assert round(store.get(pid)["cost_total"], 2) == 4.0
    store.restore_job(c)
    assert store.get(c)["project_id"] == pid and round(store.get(pid)["cost_total"], 2) == 6.0
    # Projekt „löschen“ heißt auflösen, die Drucke bleiben
    assert store.trash_job(pid) is None
    assert store.get(pid) is None and all(store.get(i) for i in (a, b, c))


def test_nachdruck_kommt_ins_projekt() -> None:
    store, kundin, _ = _store()
    a = _job(store, kundin, "A")
    fd = _job(store, kundin, "Fehldruck", scrap=1, defect="printer", ago=5000)
    pid = store.create_project([a, fd], name="Set")
    neu = _job(store, kundin, "Nachdruck", ago=100)
    store.set_reprint(fd, neu)
    assert store.get(neu)["project_id"] == pid


def test_seiten_und_export() -> None:
    store, kundin, _ = _store()
    a = _job(store, kundin, "Teil A", printer="h2s")
    b = _job(store, kundin, "Teil B", printer="p1s")
    pid = store.create_project([a, b], name="Werkstatt")
    html = page_job(store, store.get(pid), None)
    assert "Drucke im Projekt" in html and "Teil A" in html and "Teil B" in html
    html = page_job(store, store.get(a), None)
    assert "Teil von" in html and "Werkstatt" in html
    html = page_jobs(store, {k: None for k in FILTER_KEYS}, True)
    assert 'data-kind="project"' in html and 'data-parent="' in html
    img = Path(tempfile.mkdtemp())
    data = collect(store, store.get(pid), img)
    assert data["project"] and [x["name"] for x in data["parts"]] == ["Teil A", "Teil B"]
    assert bon_png(data, "kunde")[:4] == b"\x89PNG" and bon_png(data, "intern")[:4] == b"\x89PNG"
    assert report_pdf(data)[:4] == b"%PDF"
