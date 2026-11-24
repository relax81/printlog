"""Ansicht „Offen“: nur Drucke mit offenem Betrag oder offenen Stück, nach Kunden gruppiert.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Store  # noqa: E402
from app.pages import _todo_view  # noqa: E402


def _job(store: Store, cid: int | None, name: str, **extra) -> int:
    now = time.time()
    fields = dict(printer="h2s", task_name=name, state="done", status="fertig", start_ts=now - 3600,
                  end_ts=now, cost_total=2.0, customer_id=cid, quantity=2, created_at=now)
    fields.update(extra)
    return store.insert(**fields)


def test_offene_posten() -> None:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    kunde = store.save_customer(None, "Kundin", None, None)
    eigen = store.save_customer(None, "Eigenbedarf", None, None, no_billing=True)
    offen = _job(store, kunde, "Offen")
    erledigt = _job(store, kunde, "Erledigt", final_price=5.0)
    store.add_payment(erledigt, 5.0)
    store.add_handover(erledigt, 2)
    geschenk = _job(store, kunde, "Geschenk", gift=1)
    geschenk_weg = _job(store, kunde, "Geschenk übergeben", gift=1)
    store.add_handover(geschenk_weg, 2)
    _job(store, eigen, "Eigenbedarf")
    _job(store, None, "Ohne Kunde")
    _job(store, kunde, "Ausschuss", status="abgebrochen")

    ids = {j["id"] for j in store.list_jobs(pending="1")}
    assert ids == {offen, geschenk}

    html = _todo_view(store, {})
    assert "Offen" in html and "Geschenk" in html
    assert "Erledigt" not in html and "Eigenbedarf" not in html and "Ohne Kunde" not in html
    # Geschenk: keine Zahlung, nur Übergabe
    assert html.count("btn pay act") == 1 and html.count("btn ho act") == 2


def test_nichts_offen() -> None:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    assert "oempty" in _todo_view(store, {})
