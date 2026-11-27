"""Fehldrucke: Ursache, Berechnung (nichts/anteilig/Betrag/voll), Nachdruck und Statistik.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Store  # noqa: E402
from app.pages import _todo_view, amount_due, page_job  # noqa: E402


def _job(store: Store, cid: int | None, name: str, **extra) -> int:
    now = time.time()
    fields = dict(printer="h2s", task_name=name, state="done", status="fertig", start_ts=now - 3600,
                  end_ts=now, cost_total=2.0, customer_id=cid, quantity=1, created_at=now, final_price=3.0)
    fields.update(extra)
    return store.insert(**fields)


def _store() -> tuple[Store, int]:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    return store, store.save_customer(None, "Kundin", None, None)


def test_berechnung() -> None:
    store, kunde = _store()
    nichts = _job(store, kunde, "Nichts", scrap=1, defect="settings")
    halb = _job(store, kunde, "Halb", scrap=1, defect="settings", defect_charge="pct", defect_value=50.0)
    betrag = _job(store, kunde, "Betrag", scrap=1, defect="material", defect_charge="amount", defect_value=1.2)
    voll = _job(store, kunde, "Voll", scrap=1, defect="customer", defect_charge="full")
    abbruch = _job(store, kunde, "Abbruch", status="abgebrochen")
    assert amount_due(store.get(halb)) == 1.5
    assert amount_due(store.get(betrag)) == 1.2
    assert amount_due(store.get(voll)) == 3.0

    # Offen: nur berechnete Fehldrucke, mit dem reduzierten Betrag (SQL und Python gleich)
    ids = {j["id"] for j in store.list_jobs(paid="offen")}
    assert ids == {halb, betrag, voll}
    assert nichts not in ids and abbruch not in ids
    html = _todo_view(store, {})
    assert "1,50 €" in html and "Nichts" not in html
    c = next(c for c in store.customers() if c["id"] == kunde)
    assert round(c["open_amount"], 2) == 5.7
    # Bezahlt nach 1,50 €
    store.add_payment(halb, 1.5)
    assert halb not in {j["id"] for j in store.list_jobs(paid="offen")}


def test_filter_und_statistik() -> None:
    store, kunde = _store()
    _job(store, kunde, "Haftung", scrap=1, defect="adhesion")
    _job(store, kunde, "Wunsch", scrap=1, defect="customer", defect_charge="full")
    _job(store, None, "Abbruch", status="abgebrochen", cost_total=1.0)
    _job(store, kunde, "Gut")
    assert [j["task_name"] for j in store.list_jobs(status="fd:adhesion")] == ["Haftung"]
    assert [j["task_name"] for j in store.list_jobs(status="fd:none")] == ["Abbruch"]
    assert len(store.list_jobs(status="ausschuss")) == 3

    tot = store.summary()["total"]
    assert tot["scrap_n"] == 3 and round(tot["scrap_cost"], 2) == 5.0 and round(tot["scrap_charged"], 2) == 3.0

    st = store.defect_stats(0, time.time() + 10)
    by = {r["defect"]: r for r in st["rows"]}
    assert by["customer"]["charged"] == 3.0 and by["adhesion"]["n"] == 1 and by[None]["n"] == 1
    # „Kundenwunsch geändert“ zählt nicht als Fehlschlag
    rows = store.buckets("month", 0, time.time() + 10)
    assert sum(r["failed"] for r in rows) == 2 and sum(r["ok"] for r in rows) == 2


def test_nachdruck_und_seite() -> None:
    store, kunde = _store()
    fd = _job(store, kunde, "Vase", scrap=1, defect="settings", defect_charge="pct", defect_value=50.0, job_note="zu schnell")
    neu = _job(store, kunde, "Vase", start_ts=time.time() + 60)
    assert [c["id"] for c in store.reprint_candidates(store.get(fd))][0] == neu
    store.set_reprint(fd, neu)
    assert [r["id"] for r in store.reprints(fd)] == [neu]
    html = page_job(store, store.get(fd), None)
    assert 'class="box fdbox"' in html and "cat on" in html and "zu schnell" in html and "1,50 €" in html
    assert f'href="job?id={neu}"' in html
    assert f'href="job?id={fd}"' in page_job(store, store.get(neu), None)
    store.set_reprint(fd, None)
    assert store.reprints(fd) == []


def test_filament_gebrochen_steht_oben():
    from app.defects import DEFECTS
    from app.i18n_en import EN
    assert next(iter(DEFECTS)) == "filament_break"
    assert EN[DEFECTS["filament_break"][1]] == "Filament broke"
