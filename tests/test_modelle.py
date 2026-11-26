"""Modelle: Erkennung mit Punkten, Lernen, Ablehnen, Designkosten einmal bzw. verteilt.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models as mdl  # noqa: E402
from app.db import Store  # noqa: E402

IMG = Path(tempfile.mkdtemp())


def _store():
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    store.set_setting("design_rate", "30")
    store.set_setting("models_ready", "1")
    return store


def _job(store, name, ts, **f):
    f.setdefault("weight_g", 20)
    return store.insert(printer="a1", task_name=name, state="done", status="fertig", start_ts=ts, end_ts=ts + 600,
                        created_at=ts, **f)


def test_normalize() -> None:
    assert mdl.normalize("836179-Snowflakes_olli_firma.gcode.gcode") == "snowflakes olli firma"
    assert mdl.clean_name("2244797-Wolfbox_plate_2.gcode") == "Wolfbox"
    assert mdl.similar("schluesselanhaenger name", "schluesselanhaenger namen")


def test_stufen_und_lernen() -> None:
    store = _store()
    t0 = time.time() - 10 * 86400
    a = _job(store, "Wolfbox", t0, img_hash="ffff0000ffff0000", gcode_plate=1)
    assert mdl.recognize(store, a, IMG) is None
    ja = store.get(a)
    assert ja["model_state"] == "new" and ja["model_id"]
    mid = ja["model_id"]
    # gleicher Name + gleiches Bild → automatisch
    b = _job(store, "Wolfbox", t0 + 86400, img_hash="ffff0000ffff0001", gcode_plate=1)
    mdl.recognize(store, b, IMG)
    jb = store.get(b)
    assert jb["model_state"] == "auto" and jb["model_id"] == mid and jb["model_score"] >= 85
    # gleicher Name, andere Platte, anderes Bild → vorläufig mit Hinweis
    c = _job(store, "Wolfbox", t0 + 2 * 86400, img_hash="0000ffff0000ffff", gcode_plate=2, weight_g=80)
    assert mdl.recognize(store, c, IMG)
    jc = store.get(c)
    assert jc["model_state"] == "tentative" and jc["model_hint"] == 1 and jc["model_id"] == mid
    # ähnlicher Name → nur Vorschlag, kein Modell
    d = _job(store, "Wolfbox klein", t0 + 3 * 86400, weight_g=5)
    mdl.recognize(store, d, IMG)
    jd = store.get(d)
    assert jd["model_state"] == "suggest" and jd["model_id"] is None
    # bestätigen → Alias gelernt, nächster Druck mit dem Namen geht durch
    mdl.assign(store, d, mid)
    assert store.get(d)["model_state"] == "confirmed"
    e = _job(store, "Wolfbox klein", t0 + 4 * 86400, img_hash="ffff0000ffff0000")
    mdl.recognize(store, e, IMG)
    assert store.get(e)["model_state"] == "auto" and store.get(e)["model_id"] == mid
    assert len(mdl.open_hints(store)) == 1


def test_ablehnen_wird_gemerkt() -> None:
    store = _store()
    t0 = time.time() - 10 * 86400
    a = _job(store, "Vase Spirale", t0)
    mdl.recognize(store, a, IMG)
    mid = store.get(a)["model_id"]
    b = _job(store, "Vase Spirale gross", t0 + 86400)
    mdl.recognize(store, b, IMG)
    assert store.get(b)["model_state"] == "suggest"
    new = mdl.make_new(store, b)
    assert new and new != mid and store.get(b)["model_hint"] is None
    # gleicher Name nochmal: das abgelehnte Modell kommt nicht mehr, das neue schon
    c = _job(store, "Vase Spirale gross", t0 + 2 * 86400)
    mdl.recognize(store, c, IMG)
    assert store.get(c)["model_id"] == new


def test_kritisch_bei_zwei_gleich_guten() -> None:
    store = _store()
    m1 = mdl.create_model(store, "Halter")
    m2 = mdl.create_model(store, "Halter")
    j = _job(store, "Halter", time.time())
    mdl.recognize(store, j, IMG)
    jj = store.get(j)
    assert jj["model_state"] == "critical" and jj["model_id"] is None and jj["model_hint"] == 1
    mdl.assign(store, j, m2)
    assert store.get(j)["model_id"] == m2
    assert m1


def test_design_einmal_und_verteilt() -> None:
    store = _store()
    cid = store.save_customer(None, "Lena", None, None)
    t0 = time.time() - 10 * 86400
    own = _job(store, "Anhänger", t0)                       # eigener Druck: nicht berechnet
    first = _job(store, "Anhänger", t0 + 86400, customer_id=cid)
    second = _job(store, "Anhänger", t0 + 2 * 86400, customer_id=cid)
    for j in (own, first, second):
        mdl.recognize(store, j, IMG)
    mid = store.get(own)["model_id"]
    assert store.get(first)["model_id"] == mid == store.get(second)["model_id"]
    mdl.save_model(store, mid, design_min=60)
    assert store.get(own)["design_min"] is None
    assert store.get(first)["design_min"] == 60 and round(store.get(first)["cost_design"], 2) == 30
    assert store.get(second)["design_min"] is None
    mdl.save_model(store, mid, design_mode="spread", spread_n=2)
    assert store.get(first)["design_min"] == 30 and store.get(second)["design_min"] == 30
    # Geschenk zählt nicht als berechnet → rückt nach
    mdl.save_model(store, mid, design_mode=None)
    store.update(first, gift=1)
    mdl.apply_all(store)
    assert store.get(first)["design_min"] is None and store.get(second)["design_min"] == 60


def test_zusammenlegen() -> None:
    store = _store()
    a = _job(store, "Clip A", time.time() - 3600)
    b = _job(store, "Klammer", time.time())
    for j in (a, b):
        mdl.recognize(store, j, IMG)
    ma, mb = store.get(a)["model_id"], store.get(b)["model_id"]
    assert ma != mb
    mdl.save_model(store, ma, design_min=20)
    mdl.merge(store, ma, mb)
    assert store.get(a)["model_id"] == mb and mdl.model(store, ma) is None
    assert mdl.model(store, mb)["design_min"] == 20
    c = _job(store, "Clip A", time.time() + 60)
    mdl.recognize(store, c, IMG)
    assert store.get(c)["model_id"] == mb


def test_erster_lauf_still() -> None:
    store = _store()
    store.set_setting("models_ready", None)
    t0 = time.time() - 10 * 86400
    for k, n in enumerate(("Box", "Box", "Box gross", "Deckel")):
        _job(store, n, t0 + k * 3600)
    assert mdl.recognize_pending(store, IMG) == []
    assert store.settings().get("models_ready") == "1"
    assert not mdl.open_hints(store)
    rows = store._conn.execute("SELECT model_id FROM jobs").fetchall()
    assert all(r[0] for r in rows)


def test_umbenennen_behaelt_alten_namen() -> None:
    store = _store()
    t0 = time.time() - 10 * 86400
    a = _job(store, "Wolfbox", t0, img_hash="ffff0000ffff0000", gcode_plate=1)
    mdl.recognize(store, a, IMG)
    mid = store.get(a)["model_id"]
    mdl.save_model(store, mid, name="Wolf-Aufbewahrungsbox")
    # alter Name bleibt als Alias → neuer Druck mit altem Namen wird weiter automatisch erkannt
    b = _job(store, "Wolfbox", t0 + 86400, img_hash="ffff0000ffff0001", gcode_plate=1)
    mdl.recognize(store, b, IMG)
    jb = store.get(b)
    assert jb["model_id"] == mid and jb["model_state"] == "auto"
    # Zurückbenennen: der wieder aktuelle Name steht nicht doppelt als Alias
    mdl.save_model(store, mid, name="Wolfbox")
    al = {r["alias"] for r in store._conn.execute("SELECT alias FROM model_aliases WHERE model_id = ?", (mid,))}
    assert "wolfbox" not in al and "wolf aufbewahrungsbox" in al
