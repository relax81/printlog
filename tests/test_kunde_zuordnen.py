"""Kunde schnell zuordnen: Chip in der Liste, Vorschlag fürs gleiche Modell, Mehrfachauswahl, Rückgängig.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Store  # noqa: E402
from app.pages import FILTER_KEYS, page_job, page_jobs, results  # noqa: E402


def _job(store: Store, cid: int | None, name: str, ago: float = 3600) -> int:
    now = time.time()
    return store.insert(printer="p1s", task_name=name, state="done", status="fertig", start_ts=now - ago, end_ts=now - ago + 1800,
                        cost_total=1.0, cost=0.2, cost_filament=0.6, cost_printer=0.2, kwh=0.1, filament_g=10.0,
                        customer_id=cid, quantity=1, created_at=now)


def _store() -> tuple[Store, int, int]:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    return store, store.save_customer(None, "Tom", None, None, 20), store.save_customer(None, "Anna", None, None)


def _f(**kw: str) -> dict:
    f = {k: None for k in FILTER_KEYS}
    f.update(kw)
    return f


def test_zuordnen_und_rueckgaengig() -> None:
    store, tom, anna = _store()
    a, b = _job(store, None, "Halter"), _job(store, anna, "Box")
    prev = store.assign_customer([a, b], tom)
    assert prev == {a: None, b: anna}
    assert store.get(a)["customer_id"] == tom and store.get(b)["customer_id"] == tom
    # Rückgängig = früheren Kunden je Druck wieder setzen
    store.assign_customer([a], None)
    store.assign_customer([b], anna)
    assert store.get(a)["customer_id"] is None and store.get(b)["customer_id"] == anna
    # Unbekannter Kunde ändert nichts
    assert store.assign_customer([a], 999) == {} and store.get(a)["customer_id"] is None


def test_druck_im_projekt_setzt_projektkunden() -> None:
    store, tom, anna = _store()
    a, b = _job(store, anna, "A"), _job(store, anna, "B")
    pid = store.create_project([a, b], name="Set")
    prev = store.assign_customer([a, b], tom)
    assert prev == {pid: anna}
    assert all(store.get(i)["customer_id"] == tom for i in (pid, a, b))


def test_neuer_kunde_nur_mit_namen() -> None:
    store, tom, _ = _store()
    assert store.customer_by_name("tom") == tom  # vorhandener Name wird wiederverwendet
    nid = store.customer_by_name("  Lena  ")
    assert next(c for c in store.customers() if c["id"] == nid)["name"] == "Lena"


def test_vorschlag_gleiches_modell() -> None:
    store, tom, anna = _store()
    for _ in range(2):
        _job(store, tom, "Halter", 9000)
    _job(store, anna, "Halter", 8000)
    _job(store, None, "Halter")
    # Eigenbedarf: meist ohne Kunde gedruckt → kein Vorschlag
    _job(store, tom, "Clip", 9000)
    for _ in range(2):
        _job(store, None, "Clip")
    sug = store.customer_suggestions(["Halter", "Clip", "Neu"])
    assert sug == {"Halter": (tom, "Tom", 2)}


def test_chips_in_liste_und_detail() -> None:
    store, tom, _ = _store()
    _job(store, tom, "Halter", 9000)
    a = _job(store, None, "Halter")
    b = _job(store, None, "Einzelstück")
    html = results(store, _f())
    assert 'class="kchip sug"' in html and f'data-kid="{a}" data-sug="{tom}"' in html
    assert f'class="kchip kc" data-kid="{b}"' in html
    assert 'class="tag kset kc"' in html  # vorhandener Kunde: antippen zum Ändern
    page = page_jobs(store, _f(), True)
    assert 'id="kdata"' in page and 'class="btn kbtn"' in page and "api/customers/assign" in page
    assert 'class="kchip sug"' in page_job(store, store.get(a), None)
