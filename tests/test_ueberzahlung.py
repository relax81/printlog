"""Mehr bezahlt als der Preis: Betrag und Gewinn aus dem Gezahlten (Einzeldruck und Projekt).

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Store  # noqa: E402
from app.export import collect  # noqa: E402
from app.pages import FILTER_KEYS, overpaid, page_job, page_jobs, revenue  # noqa: E402


def _job(store: Store, cid: int | None, name: str, cost: float = 3.0, **extra) -> int:
    now = time.time()
    fields = dict(printer="h2s", task_name=name, state="done", status="fertig", start_ts=now - 3600,
                  end_ts=now - 1800, cost_total=cost, cost=cost / 4, cost_filament=cost / 2, cost_printer=cost / 4,
                  kwh=0.1, filament_g=10.0, customer_id=cid, quantity=1, created_at=now)
    fields.update(extra)
    return store.insert(**fields)


def _store() -> tuple[Store, int]:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    return store, store.save_customer(None, "Kundin", None, None, 80)


def test_einzeldruck_ueberzahlt() -> None:
    store, kundin = _store()
    jid = _job(store, kundin, "Hut")  # 3 € + 80 % = 5,40 €
    store.add_payment(jid, 15.0)
    j = store.get(jid)
    assert overpaid(j) == 9.6 and revenue(j) == 15.0
    html = page_job(store, j, None)
    assert "Gewinn über Selbstkosten" in html and "12,00 €" in html  # 15 − 3
    assert "9,60 € mehr bezahlt" in html and "15,00 € als Endpreis übernehmen" in html
    assert "+9,60 € mehr" in page_jobs(store, {k: None for k in FILTER_KEYS}, True)
    assert round(collect(store, j, Path(tempfile.mkdtemp()))["profit"], 2) == 12.0


def test_genau_bezahlt_ohne_endpreis_kein_gewinn() -> None:
    store, kundin = _store()
    jid = _job(store, kundin, "Hut")
    store.add_payment(jid, 5.4)
    j = store.get(jid)
    assert overpaid(j) == 0 and revenue(j) is None
    assert "Gewinn über Selbstkosten" not in page_job(store, j, None)


def test_projekt_ueberzahlt() -> None:
    store, kundin = _store()
    a = _job(store, kundin, "A", 2.0)
    b = _job(store, kundin, "B", 1.0)
    pid = store.create_project([a, b], name="Set", quantity=1)
    store.add_payment(pid, 15.0)
    p = store.get(pid)
    assert overpaid(p) == 9.6
    html = page_job(store, p, None)
    assert "Gewinn über Selbstkosten" in html and "12,00 €" in html
    # Drucke im Projekt rechnen nicht selbst ab
    assert overpaid(store.get(a)) == 0


def test_geschenk_nie_ueberzahlt() -> None:
    store, kundin = _store()
    jid = _job(store, kundin, "Hut", gift=1)
    store.add_payment(jid, 15.0)
    assert overpaid(store.get(jid)) == 0


def test_statistik_einnahmen_und_gewinn() -> None:
    from datetime import date

    from app.stats import _buckets, page_stats, revenue_data

    store, kundin = _store()
    eigen = store.save_customer(None, "Eigenbedarf", None, None, no_billing=True)
    a = _job(store, kundin, "Hut")              # 5,40 €, 15 € bezahlt → Gewinn 12 €
    store.add_payment(a, 15.0)
    _job(store, kundin, "Offen")                # 5,40 € offen → Gewinn 2,40 €
    _job(store, kundin, "Geschenk", gift=1)     # 3 € verschenkt
    _job(store, eigen, "Eigenbedarf")           # zählt nicht
    _keys, _labels, since, until = _buckets("month", date.today())
    rv = revenue_data(store, "month", since.timestamp(), until.timestamp())
    tot = rv["tot"]
    assert round(tot["revenue"], 2) == 20.4 and round(tot["paid"], 2) == 15.0 and round(tot["open"], 2) == 5.4
    assert round(tot["profit"], 2) == 14.4 and round(tot["gift"], 2) == 3.0 and tot["n"] == 2
    assert [c["name"] for c in rv["customers"]] == ["Kundin"]
    html = page_stats(store, "month")
    assert "Einnahmen und Gewinn" in html and "14,40 €" in html
