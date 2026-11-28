"""Mit Kundenmarge zeigt der Verkaufspreis nur sie; allgemeine Margen auf Wunsch.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Store  # noqa: E402
from app.pages import _no_more, _prices, margin_rows, margins  # noqa: E402


def _store() -> Store:
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    store.set_setting("margin1_name", "Privat")
    store.set_setting("margin1_pct", "30")
    store.set_setting("margin2_name", "Gewerbe")
    store.set_setting("margin2_pct", "60")
    return store


def _job(cm):
    return {"id": 1, "cost_total": 10.0, "quantity": 1, "customer_name": "Olli", "customer_margin": cm,
            "customer_work": 0, "kind": None}


def test_detail_nur_kundenmarge_sichtbar():
    store = _store()
    rows, more = margin_rows(store, _job(80), 1, 0)
    assert "Olli" in rows and "Privat" not in rows
    assert 'class="mmore"' in more and "Privat" in more and "Gewerbe" in more
    assert "Privat" not in _no_more(rows + more)


def test_detail_ohne_kundenmarge_alle_margen():
    store = _store()
    rows, more = margin_rows(store, _job(None), 1, 0)
    assert "Privat" in rows and "Gewerbe" in rows and more == ""


def test_liste_weitere_margen_im_popup():
    store = _store()
    mg = margins(store)
    html = _prices(_job(80), mg)
    assert "Olli" in html and 'class="cb mb calc"' in html
    assert html.index("Privat") > html.index('class="pop mpop"')
    assert "cb mb" not in _prices(_job(None), mg)
