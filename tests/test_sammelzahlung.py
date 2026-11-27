"""Zahlung für mehrere Posten: Verteilung, Überschuss, Guthaben und Mehrfachauswahl.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import bulkpay as bp  # noqa: E402
from app.db import Store  # noqa: E402


def test_verteilung() -> None:
    # älteste zuerst, Betrag reicht nicht
    assert bp.allocate([500, 1000, 300], 1200) == ([500, 700, 0], 0)
    # anteilig, Rundungsrest an die ersten
    give, credit = bp.allocate([500, 1000, 300], 900, "share")
    assert sum(give) == 900 and credit == 0 and give == [250, 500, 150]
    give, _ = bp.allocate([100, 100, 100], 100, "share")
    assert give == [34, 33, 33]
    # Überschuss: Trinkgeld an den neuesten, anteilig oder als Guthaben
    assert bp.allocate([500, 1000], 1800, surplus="tip") == ([500, 1300], 0)
    assert bp.allocate([500, 1000], 1800, surplus="spread") == ([600, 1200], 0)
    assert bp.allocate([500, 1000], 1800, surplus="credit") == ([500, 1000], 300)
    assert bp.allocate([500], 0) == ([0], 0)


def _store() -> tuple[Store, int, list[int]]:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    cid = store.save_customer(None, "Kundin", None, None)
    now = time.time()
    ids = []
    for i, price in enumerate((5.0, 10.0, 3.0)):
        ids.append(store.insert(printer="h2s", task_name=f"Druck {i}", state="done", status="fertig", start_ts=now - 86400 * (10 - i),
                                end_ts=now, cost_total=1.0, customer_id=cid, quantity=1, created_at=now, final_price=price))
    return store, cid, ids


def _paid(store: Store, jid: int) -> float:
    return round(sum(p["amount"] for p in store.payments(jid)), 2)


def test_buchen_mit_guthaben_und_rueckgaengig() -> None:
    store, cid, ids = _store()
    store.add_payment(ids[0], 2.0)
    data = bp.dialog_data(store, cid, None)
    assert [x["open"] for x in data["items"]] == [300, 1000, 300] and all(x["on"] for x in data["items"])

    # 20 € für alle: 16 € offen, 4 € als Guthaben
    r = bp.book(store, cid, ids, 20.0, "oldest", "credit", False, None, "bar")
    assert r["ok"] and r["full"] == 3 and r["credit"] == 400
    assert [_paid(store, j) for j in ids] == [5.0, 10.0, 3.0]
    assert store.credit_balance(cid) == 4.0
    assert bp.dialog_data(store, cid, None)["items"] == []

    # Rückgängig: Zahlungen und Guthaben wieder weg
    bp.undo(store, r["payments"], r["credits"])
    assert [_paid(store, j) for j in ids] == [2.0, 0, 0] and store.credit_balance(cid) == 0


def test_guthaben_verwenden() -> None:
    store, cid, ids = _store()
    store.add_credit(cid, 4.0)
    # 15 € bar + 3 € aus dem Guthaben decken alle 18 €
    r = bp.book(store, cid, ids, 15.0, "oldest", "tip", True, None, None)
    assert r["used"] == 300 and r["full"] == 3
    assert store.credit_balance(cid) == 1.0
    assert "Guthaben" in (store.payments(ids[0])[0]["note"] or "")


def test_mehrfachauswahl() -> None:
    store, cid, ids = _store()
    other = store.save_customer(None, "Anderer", None, None)
    fremd = store.insert(printer="h2s", task_name="Fremd", state="done", status="fertig", start_ts=time.time(), end_ts=time.time(),
                         cost_total=1.0, customer_id=other, quantity=1, created_at=time.time(), final_price=2.0)
    data = bp.dialog_data(store, None, [ids[1]])
    assert data["customer"]["id"] == cid and [x["on"] for x in data["items"]] == [False, True, False]
    assert "error" in bp.dialog_data(store, None, [ids[1], fremd])
    assert "error" in bp.book(store, cid, [fremd], 5.0, "oldest", "tip", False, None, None)
    # Kunde löschen entfernt das Guthaben
    store.add_credit(cid, 1.0)
    store.delete_customer(cid)
    assert store.credit_balance(cid) == 0
