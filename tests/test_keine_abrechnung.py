"""Kunde mit „Keine Abrechnung“: Kosten zählen, aber nie offen, unbezahlt oder nicht übergeben.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Store  # noqa: E402
from app.pages import billable, customer_badges  # noqa: E402


def _setup():
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    own = store.save_customer(None, "Eigene Uploads", None, None, no_billing=True)
    paying = store.save_customer(None, "Kunde", None, None)
    now = time.time()
    for cid in (own, paying):
        store.insert(printer="h2s", task_name="Teil", state="done", status="fertig", start_ts=now - 3600,
                     end_ts=now, cost_total=2.5, customer_id=cid, quantity=2, created_at=now)
    return store, own, paying


def test_nichts_offen() -> None:
    store, own, paying = _setup()
    by_id = {c["id"]: c for c in store.customers()}
    assert by_id[own]["open_n"] == 0 and by_id[own]["undelivered_n"] == 0
    assert by_id[own]["cost_total"] == 2.5 and by_id[own]["n"] == 1
    assert by_id[paying]["open_n"] == 1 and by_id[paying]["undelivered_n"] == 1
    assert store.count_jobs(paid="offen") == 1
    assert store.count_jobs(handover="offen") == 1
    assert store.count_jobs(customer=str(own), paid="offen") == 0
    # Kosten zählen weiter in der Summe
    assert store.summary()["total"]["cost_total"] == 5.0


def test_keine_badges() -> None:
    store, own, paying = _setup()
    jobs = {j["customer_id"]: j for j in store.list_jobs()}
    assert not billable(jobs[own]) and customer_badges(jobs[own]) == ""
    assert billable(jobs[paying]) and "💶" in customer_badges(jobs[paying])


def test_haekchen_abwaehlen() -> None:
    store, own, _ = _setup()
    store.save_customer(own, "Eigene Uploads", None, None, no_billing=False)
    assert {c["id"]: c for c in store.customers()}[own]["open_n"] == 1
