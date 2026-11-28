"""Filamentmenge von Hand korrigieren, wenn Home Assistant 0 g gemeldet hat.

Aufruf: python -m pytest tests
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from app.costing import compute  # noqa: E402
from app.db import Store  # noqa: E402
from app.web import setup_routes  # noqa: E402


def _store():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "img").mkdir()
    store = Store(tmp / "t.db")
    store.set_setting("language", "de")
    store.set_material_price("PLA", "type", 20.0)

    now = time.time()
    mats = json.dumps([{"slot": "aktiv", "name": "Bambu PLA Basic", "type": "PLA", "color": "#556B2F", "grams": 0}])
    jid = store.insert(printer="h3c", task_name="Glocken", state="done", status="fertig", start_ts=now - 3600,
                       end_ts=now, weight_g=0, materials=mats, created_at=now)
    store.update(jid, **compute(store.get(jid), store))
    return store, tmp, jid


async def _post(store, tmp, path, data):
    app = web.Application()
    setup_routes(app, store, tmp / "img", None)
    async with TestClient(TestServer(app)) as client:
        r = await client.post(path, data=data, allow_redirects=False)
        return r.status


def test_menge_nachtragen() -> None:
    store, tmp, jid = _store()
    assert not store.get(jid)["filament_g"]
    status = asyncio.run(_post(store, tmp, f"/api/jobs/{jid}?redirect=1", {"mgram__0": "145,2", "mnote__0": ""}))
    assert status == 303
    job = store.get(jid)
    assert job["filament_g"] == 145.2
    m = json.loads(job["materials"])[0]
    assert m["grams"] == 145.2 and m["manual"] is True
    assert abs(job["cost_filament"] - 145.2 / 1000 * 20) < 0.001 and job["cost_total"] is not None


def test_unveraendert_bleibt_automatisch() -> None:
    store, tmp, jid = _store()
    asyncio.run(_post(store, tmp, f"/api/jobs/{jid}?redirect=1", {"mgram__0": "0", "job_note": "x"}))
    assert "manual" not in json.loads(store.get(jid)["materials"])[0]


def test_hinweis_und_feld_auf_der_seite() -> None:
    store, tmp, jid = _store()

    async def run():
        app = web.Application()
        setup_routes(app, store, tmp / "img", None)
        async with TestClient(TestServer(app)) as client:
            return await (await client.get(f"/job?id={jid}")).text()

    html = asyncio.run(run())
    assert 'name="mgram__0"' in html
    assert "keine Filamentmenge gemeldet" in html


def test_laufender_druck_ohne_feld() -> None:
    store, tmp, jid = _store()
    store.update(jid, state="running")
    asyncio.run(_post(store, tmp, f"/api/jobs/{jid}?redirect=1", {"mgram__0": "50"}))
    assert json.loads(store.get(jid)["materials"])[0]["grams"] == 0
