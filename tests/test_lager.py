"""Lager: Lagerorte wie Kunden (anlegen, wiederverwenden), feste Kennungen, Inventar mit Lagerort, Seiten.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import extras as zk  # noqa: E402
from app import storage as lg  # noqa: E402
from app.db import Store  # noqa: E402
from app.pages import FILTER_KEYS, page_job, page_jobs, page_settings  # noqa: E402


def _store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "t.db")


def _job(store: Store, name: str) -> int:
    now = time.time()
    return store.insert(printer="p1s", task_name=name, state="done", status="fertig", start_ts=now - 3600, end_ts=now,
                        cost_total=1.0, quantity=1, created_at=now)


def _f(**kw: str) -> dict:
    f = {k: None for k in FILTER_KEYS}
    f.update(kw)
    return f


def test_lagerort_wie_kunde() -> None:
    store = _store()
    k3 = lg.location_by_name(store, "Kiste 3")
    assert lg.location_by_name(store, "kiste 3") == k3  # gleicher Name wird wiederverwendet
    loc = lg.location(store, k3)
    assert loc and len(loc["code"]) == 4 and lg.by_code(store, "locations", loc["code"])["id"] == k3
    a, b = _job(store, "Halter"), _job(store, "Box")
    assert lg.assign_jobs(store, [a, b], k3) == {a: None, b: None}
    assert store.get(a)["location_name"] == "Kiste 3"
    assert lg.assign_jobs(store, [a], 999) == {}  # unbekannter Lagerort ändert nichts
    assert {j["id"] for j in store.list_jobs(loc=str(k3))} == {a, b}
    assert len(store.list_jobs(q="kiste")) == 2
    assert lg.locations(store)[0]["n_jobs"] == 2
    lg.delete_location(store, k3)
    assert store.get(a)["location_id"] is None and not lg.locations(store)


def test_kennungen_und_codes() -> None:
    store = _store()
    aid = zk.save_accessory(store, None, name="Ring", stock=10)
    a = zk.accessory(store, aid)
    assert a["code"] and lg.by_code(store, "accessories", a["code"])["id"] == aid
    assert lg.parse_item_code(f"a-{a['code'].lower()}") == ("A", a["code"])
    assert lg.parse_item_code("L 7K3Q") == ("L", "7K3Q") and lg.parse_item_code("PL-7K3Q") is None
    # ältere Datenbank ohne Kennung: wird beim Start nachgetragen
    store._conn.execute("UPDATE accessories SET code = NULL")
    store.ensure_codes()
    assert zk.accessory(store, aid)["code"]


def test_inventar_speichern_mit_lagerort() -> None:
    store = _store()
    aid = zk.save_accessory(store, None, name="Magnet", stock=5)
    zk.save_inventory(store, {f"acc_name__{aid}": "Magnet", f"acc_loc__{aid}": "", f"acc_locnew__{aid}": "Schublade",
                              "acc_name__new": "Ring", "acc_pq__new": "100", "acc_pp__new": "7", "acc_loc__new": "",
                              "acc_locnew__new": "schublade"})
    accs = {a["name"]: a for a in zk.accessories(store)}
    assert accs["Magnet"]["loc_name"].lower() == "schublade" and accs["Ring"]["location_id"] == accs["Magnet"]["location_id"]
    assert len(lg.locations(store)) == 1
    lid = accs["Ring"]["location_id"]
    zk.save_inventory(store, {f"acc_name__{aid}": "Magnet", f"acc_loc__{aid}": "", f"acc_locnew__{aid}": ""})
    assert zk.accessory(store, aid)["location_id"] is None
    lg.item_booking(store, aid, 10, 2.0, 3)
    assert zk.accessory(store, aid)["stock"] == 12
    assert lid


def test_seiten() -> None:
    store = _store()
    j = _job(store, "Halter")
    lid = lg.location_by_name(store, "Regal")
    lg.assign_jobs(store, [j], lid)
    aid = zk.save_accessory(store, None, name="Ring", stock=10, location_id=lid)
    page = lg.page_storage(store, None, None)
    assert "acc_loc__" in page and 'id="ldata"' in page and "Regal" in page
    orte = lg.page_storage(store, "orte", None)
    loc = lg.location(store, lid)
    assert f"loc?c={loc['code']}" in orte
    lp = lg.page_location(store, loc, None)
    assert "Halter" in lp and "Ring" in lp and f"L-{loc['code']}" in lp
    ip = lg.page_item(store, zk.accessory(store, aid), None)
    assert "A-" in ip and "Regal" in ip
    jp = page_job(store, store.get(j), None)
    assert 'class="bdg kset lc"' in jp and "api/locations/assign" in jp
    lst = page_jobs(store, _f(), True)
    assert "lbtn" in lst and 'name="loc"' in lst
    assert 'href="storage"' in page_settings(store, None)
