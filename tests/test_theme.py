"""Farbschema: Einstellung auto/hell/dunkel landet als data-theme am <html>.

Aufruf: python -m pytest tests
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ui import CSS, THEME, layout  # noqa: E402


def _html(theme: str) -> str:
    token = THEME.set(theme)
    try:
        return layout("Test", "jobs", "<p>x</p>")
    finally:
        THEME.reset(token)


def test_theme_attribut() -> None:
    assert "data-theme" not in _html("auto").split("<head>")[0]
    assert 'data-theme="dark">' in _html("dark")
    assert 'data-theme="light">' in _html("light")


def test_theme_css() -> None:
    # dunkle Farben gelten bei System-dunkel (außer erzwungen hell) und immer bei erzwungen dunkel
    assert ":root:not([data-theme=light])" in CSS
    assert ":root[data-theme=dark]{--bg:#111315" in CSS
