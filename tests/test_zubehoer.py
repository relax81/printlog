"""Arbeit & Zubehör: Kosten, Preise mit/ohne Marge auf Arbeit, Bestand und Montage.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import extras as zk  # noqa: E402
from app.db import Store  # noqa: E402
from app.pages import amount_due, margins, mprice  # noqa: E402


def _setup(work_margin=None):
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    store.set_setting("work_rate", "20")
    store.set_setting("design_rate", "30")
    cid = store.save_customer(None, "Lena", None, None, margin_pct=10, work_margin=work_margin)
    now = time.time()
    jid = store.insert(printer="a1", task_name="Schlüsselanhänger", state="done", status="fertig", start_ts=now - 3600,
                       end_ts=now, cost=0.05, cost_total=1.98, customer_id=cid, quantity=10, created_at=now)
    ring = zk.save_accessory(store, None, name="Ring", pack_qty=100, pack_price=7.0, unit_price=0.07, stock=90, warn_at=20)
    kette = zk.save_accessory(store, None, name="Kette", pack_qty=50, pack_price=6.0, unit_price=0.12, stock=40)
    return store, jid, ring, kette


def _fill(store, jid, ring, kette):
    store.update(jid, prep_min=10, post_min=1.5, design_min=15)
    zk.add_extra(store, jid, ring, 1, True)
    zk.add_extra(store, jid, kette, 1, True)
    zk.add_extra(store, jid, None, 1, False, "Beutel", 0.80)


def test_kosten_und_preis() -> None:
    store, jid, ring, kette = _setup()
    _fill(store, jid, ring, kette)
    j = store.get(jid)
    assert round(j["cost_extras"], 2) == 2.70
    # Druck 1,98 ohne Drucker-/Filamentpreise: compute rechnet nur Strom + Zubehör neu
    assert round(j["cost_total"], 2) == round(0.05 + 2.70, 2)
    assert round(j["cost_work"], 2) == round(25 / 60 * 20 + 7.5, 2)
    assert round(j["cost_design"], 2) == 7.5
    # Kunde +10 %, Marge nicht auf Arbeit
    due = amount_due(j)
    assert abs(due - (j["cost_total"] * 1.1 + j["cost_work"])) < 1e-6
    # gleiche Rechnung in SQL (Offen)
    c = [c for c in store.customers() if c["id"] == j["customer_id"]][0]
    assert abs(c["open_amount"] - due) < 1e-6


def test_marge_auf_arbeit_pro_kunde_und_marge() -> None:
    store, jid, ring, kette = _setup(work_margin=1)
    _fill(store, jid, ring, kette)
    j = store.get(jid)
    assert abs(amount_due(j) - (j["cost_total"] + j["cost_work"]) * 1.1) < 1e-6
    c = [c for c in store.customers() if c["id"] == j["customer_id"]][0]
    assert abs(c["open_amount"] - amount_due(j)) < 1e-6
    store.set_setting("margin1_name", "Gewerbe")
    store.set_setting("margin1_pct", "60")
    store.set_setting("margin1_work", "1")
    m = margins(store)[0]
    assert m.work and abs(mprice(j, m) - (j["cost_total"] + j["cost_work"]) * 1.6) < 1e-6


def test_bestand_reserviert_und_montiert() -> None:
    store, jid, ring, kette = _setup()
    _fill(store, jid, ring, kette)
    assert zk.accessory(store, ring)["stock"] == 90
    assert zk.reserved(store)[ring] == 10
    zk.set_mounted(store, jid, 4)
    assert zk.accessory(store, ring)["stock"] == 86 and zk.reserved(store)[ring] == 6
    # Übergabe zählt als montiert
    store.add_handover(jid, 10)
    zk.on_delivered(store, jid)
    assert zk.accessory(store, ring)["stock"] == 80 and ring not in zk.reserved(store)
    # Entfernen bucht zurück
    x = [x for x in zk.job_extras(store, jid) if x["accessory_id"] == ring][0]
    zk.remove_extra(store, x["id"])
    assert zk.accessory(store, ring)["stock"] == 90


def test_stueckzahl_aendert_kosten() -> None:
    store, jid, ring, kette = _setup()
    _fill(store, jid, ring, kette)
    store.update(jid, quantity=20)
    zk.sync_stock(store, jid)
    zk.refresh(store, jid)
    j = store.get(jid)
    assert round(j["cost_extras"], 2) == round(20 * 0.19 + 0.8, 2)
    assert round(j["cost_work"], 2) == round((10 + 30) / 60 * 20 + 7.5, 2)


def test_zugang_mittelt_preis() -> None:
    store, jid, ring, kette = _setup()
    zk.restock(store, ring, 100, 10.0)
    a = zk.accessory(store, ring)
    assert a["stock"] == 190 and abs(a["unit_price"] - (90 * 0.07 + 10) / 190) < 1e-4


def test_projekt_mit_eigenem_zubehoer() -> None:
    store, jid, ring, kette = _setup()
    store.update(jid, prep_min=10)
    zk.refresh(store, jid)
    pid = store.create_project([jid], "Set")
    zk.add_extra(store, pid, kette, 2, True)
    p = store.get(pid)
    kid = store.get(jid)
    assert round(p["cost_extras"], 2) == 0.24
    assert round(p["cost_total"], 4) == round(kid["cost_total"] + 0.24, 4)
    assert round(p["bill_work"], 4) == round(kid["cost_work"], 4)
