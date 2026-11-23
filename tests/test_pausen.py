"""Pausen und fehlende Start-Events dürfen keinen Druck zerreißen oder „unvollständig“ machen.

Aufruf: python -m pytest tests
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Store  # noqa: E402
from app.logic import Processor  # noqa: E402

T0 = 1_800_000_000.0  # beliebige Startzeit


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


async def _no_image(job_id: int, entity: str) -> None:
    return None


def _setup(meter=None):
    store = Store(Path(tempfile.mkdtemp()) / "t.db")

    async def energy_at(printer: str, ts: float):
        return meter(ts) if meter else None

    return store, Processor(store, _no_image, energy_at)


def _run(coro):
    return asyncio.run(coro)


def _jobs(store):
    return [dict(r) for r in store._conn.execute("SELECT * FROM jobs ORDER BY id")]


def test_pause_bleibt_ein_druck() -> None:
    store, proc = _setup()
    start = {"phase": "start", "printer": "h3c", "printer_start_time": _iso(T0),
             "energy_kwh": 1.0, "progress": 0, "task_name": "Box"}
    _run(proc.handle(start, T0 + 10))
    # Lange Pause zum Magnete-Einsetzen, dann weiter: Blueprint schickt erneut start.
    _run(proc.handle({**start, "energy_kwh": 1.4, "progress": 40}, T0 + 3 * 3600))
    _run(proc.handle({"phase": "stop", "printer": "h3c", "status": "finish",
                      "printer_start_time": _iso(T0), "energy_kwh": 2.0, "progress": 100}, T0 + 5 * 3600))
    _run(proc.handle({"phase": "final", "printer": "h3c", "energy_kwh": 2.1, "price": 0.3}, T0 + 5 * 3600 + 300))
    jobs = _jobs(store)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "fertig"
    assert abs(jobs[0]["kwh"] - 1.1) < 1e-9


def test_start_aus_pause_holt_fehlenden_start_nach() -> None:
    # Erster Start fehlte (Drucker pausierte schon beim Vorbereiten), weiter mit start aus Pause.
    store, proc = _setup(meter=lambda ts: 1.59)
    _run(proc.handle({"phase": "start", "printer": "h3c", "printer_start_time": _iso(T0),
                      "energy_kwh": 1.6, "progress": 0}, T0 + 18))
    _run(proc.handle({"phase": "start", "printer": "h3c", "printer_start_time": _iso(T0),
                      "energy_kwh": 1.7, "progress": 0}, T0 + 30))
    assert len(_jobs(store)) == 1


def test_ende_ohne_start_holt_zaehlerstand_aus_verlauf() -> None:
    store, proc = _setup(meter=lambda ts: 1.590923 if ts <= T0 else None)
    _run(proc.handle({"phase": "stop", "printer": "h3c", "status": "finish",
                      "printer_start_time": _iso(T0), "energy_kwh": 3.046506, "progress": 100}, T0 + 31000))
    _run(proc.handle({"phase": "final", "printer": "h3c", "energy_kwh": 3.047358, "price": 0.24}, T0 + 31075))
    job = _jobs(store)[0]
    assert job["status"] == "fertig"
    assert job["start_ts"] == T0
    assert abs(job["kwh"] - 1.4564) < 1e-9
    assert "HA-Verlauf" in job["note"]


def test_ende_ohne_start_ohne_verlauf_bleibt_unvollstaendig() -> None:
    store, proc = _setup()
    _run(proc.handle({"phase": "stop", "printer": "h3c", "status": "finish",
                      "printer_start_time": _iso(T0), "energy_kwh": 3.0, "progress": 100}, T0 + 3600))
    _run(proc.handle({"phase": "final", "printer": "h3c", "energy_kwh": 3.0, "price": 0.24}, T0 + 3700))
    assert _jobs(store)[0]["status"] == "unvollständig"


def test_neuer_druck_schliesst_alten_ohne_ende() -> None:
    store, proc = _setup()
    _run(proc.handle({"phase": "start", "printer": "h3c", "printer_start_time": _iso(T0),
                      "energy_kwh": 1.0, "progress": 0}, T0))
    _run(proc.handle({"phase": "start", "printer": "h3c", "printer_start_time": _iso(T0 + 7200),
                      "energy_kwh": 2.0, "progress": 0}, T0 + 7200))
    jobs = _jobs(store)
    assert len(jobs) == 2
    assert jobs[0]["status"] == "unvollständig" and jobs[1]["state"] == "running"


def test_alter_eintrag_ohne_druckerstartzeit_und_pause() -> None:
    # Einträge von vor diesem Update kennen printer_start_ts nicht: Abgleich über start_ts.
    store, proc = _setup()
    job_id = store.insert(printer="h3c", state="running", start_ts=T0 + 20, start_kwh=1.0)
    _run(proc.handle({"phase": "start", "printer": "h3c", "printer_start_time": _iso(T0),
                      "energy_kwh": 1.5, "progress": 55}, T0 + 4 * 3600))
    jobs = _jobs(store)
    assert len(jobs) == 1 and jobs[0]["id"] == job_id and jobs[0]["printer_start_ts"] == T0


def test_alter_unvollstaendiger_eintrag_wird_repariert() -> None:
    # So stand die Kabelbox vor dem Update in der Datenbank.
    store, proc = _setup()
    _run(proc.handle({"phase": "stop", "printer": "h3c", "status": "finish",
                      "printer_start_time": _iso(T0), "energy_kwh": 3.046506, "progress": 100}, T0 + 31000))
    _run(proc.handle({"phase": "final", "printer": "h3c", "energy_kwh": 3.047358, "price": 0.24}, T0 + 31075))
    assert _jobs(store)[0]["status"] == "unvollständig"

    async def meter(printer, ts):
        return 1.590923
    proc.energy_at = meter
    assert _run(proc.repair_missing_start(T0 - 3600)) == 1
    job = _jobs(store)[0]
    assert job["status"] == "fertig" and abs(job["kwh"] - 1.4564) < 1e-9
    assert job["note"] == "Startwert aus HA-Verlauf ergänzt"
    assert _run(proc.repair_missing_start(T0 - 3600)) == 0  # nur einmal


def test_job_during_fuer_nachholen() -> None:
    store, _ = _setup()
    store.insert(printer="h3c", state="done", start_ts=T0, end_ts=T0 + 3600)
    assert store.job_during("h3c", T0 + 1800)
    assert not store.job_during("h3c", T0 + 7200)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
