"""Allgemeine Margen einzeln abschaltbar: abgeschaltet keine Spalte/kein Preis, Werte bleiben.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Store  # noqa: E402
from app.pages import margins, page_settings  # noqa: E402


def _store() -> Store:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    store.set_setting("margin1_name", "Privat")
    store.set_setting("margin1_pct", "30")
    store.set_setting("margin2_name", "Gewerbe")
    store.set_setting("margin2_pct", "60")
    return store


def test_ohne_einstellung_aktiv() -> None:
    assert [m[0] for m in margins(_store())] == ["Privat", "Gewerbe"]


def test_einzeln_aus() -> None:
    store = _store()
    store.set_setting("margin1_on", "0")
    assert [(m[0], m[1]) for m in margins(store)] == [("Gewerbe", 60.0)]
    store.set_setting("margin2_on", "0")
    assert margins(store) == []
    # Name und Prozent bleiben gespeichert, wieder an = wieder da
    store.set_setting("margin1_on", None)
    assert [(m[0], m[1]) for m in margins(store)] == [("Privat", 30.0)]


def test_einstellungen_zeigen_haekchen() -> None:
    store = _store()
    store.set_setting("margin2_on", "0")
    html = page_settings(store, None)
    assert 'name="margin1_on" value="1" checked' in html
    assert 'name="margin2_on" value="1" onchange' in html
    assert 'class="frow mrow off"' in html and 'value="Gewerbe"' in html
