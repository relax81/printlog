"""−/+-Knöpfe der Arbeitszeit: Schritt mit Vorzeichen.

Aufruf: python -m pytest tests
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web import _delta  # noqa: E402


def test_delta_mit_vorzeichen() -> None:
    assert _delta("-15") == -15 and _delta("-0.5") == -0.5 and _delta("5") == 5
    assert _delta("−5") == -5 and _delta("") is None and _delta(None) is None and _delta("x") is None
