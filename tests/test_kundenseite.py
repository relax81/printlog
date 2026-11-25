"""Kundenseite: Kennzahlen, Aufträge, Filament und öfter gedruckte Modelle eines Kunden.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.customer import page_customer, summarize  # noqa: E402
from app.db import Store  # noqa: E402
from app.pages import page_customers  # noqa: E402

MAT = '[{"slot":1,"type":"PLA","name":"PLA Basic","color":"#1E40FF","grams":%s}]'


def _job(store: Store, cid: int | None, name: str, cost: float = 3.0, ago: float = 3600, **extra) -> int:
    now = time.time()
    fields = dict(printer="h2s", task_name=name, state="done", status="fertig", start_ts=now - ago,
                  end_ts=now - ago + 1800, cost_total=cost, cost=cost / 4, cost_filament=cost / 2, cost_printer=cost / 4,
                  kwh=0.1, filament_g=10.0, customer_id=cid, quantity=1, created_at=now, materials=MAT % 10)
    fields.update(extra)
    return store.insert(**fields)


def test_kennzahlen_und_seite() -> None:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    cid = store.save_customer(None, "Siggi", "per Telefon", None, 80)
    a = _job(store, cid, "Clip", ago=5 * 86400)       # 5,40 €, 15 € bezahlt
    store.add_payment(a, 15.0, ts=time.time() - 2 * 86400)
    _job(store, cid, "Clip", ago=3 * 86400)           # 5,40 € offen
    _job(store, cid, "Hut", gift=1)                   # verschenkt
    b = _job(store, cid, "A", 1.0)
    c = _job(store, cid, "B", 2.0)
    pid = store.create_project([b, c], name="Set", quantity=2)
    store.add_handover(pid, 2)
    _job(store, None, "Fremd")

    d = summarize(store, cid)
    tot = d["tot"]
    assert tot["n"] == 4 and tot["billed"] == 3 and tot["gift_n"] == 1
    assert round(tot["revenue"], 2) == round(15 + 5.4 + 5.4, 2)
    assert round(tot["open"], 2) == 10.8 and round(tot["paid"], 2) == 15.0
    assert tot["prints"] == 5 and round(tot["grams"]) == 50
    assert [m["name"] for m in d["models"]] == ["Clip"]
    assert d["pay_days"] is not None and 2 < d["pay_days"] < 3.1
    assert tot["delivered"] == 2 and tot["qty"] == 5  # Geschenk wird auch übergeben

    html = page_customer(store, cid)
    assert html and "Siggi" in html and "Auf einen Blick" in html and "Öfter gedruckt" in html and "bezahlt +9,60" in html
    assert page_customer(store, 9999) is None
    assert f'href="customer?id={cid}"' in page_customers(store, None)


def test_eigenbedarf() -> None:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    cid = store.save_customer(None, "MakerWorld", None, None, no_billing=True)
    _job(store, cid, "Upload")
    html = page_customer(store, cid)
    assert "Eigenbedarf" in html and "Umsatz pro Monat" not in html
