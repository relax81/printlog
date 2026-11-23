"""Prüft, dass jeder Text aus dem Code eine englische Übersetzung mit denselben Platzhaltern hat.

Aufruf: python -m pytest tests  (oder: python tests/test_i18n.py)
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.i18n import from_header, resolve  # noqa: E402
from app.i18n_en import EN  # noqa: E402
from tests.i18n_keys import keys  # noqa: E402

PH = re.compile(r"\{\w+\}")


def test_all_keys_translated() -> None:
    missing = [k for k in keys() if k not in EN]
    assert not missing, missing


def test_placeholders_match() -> None:
    wrong = [k for k, v in EN.items() if set(PH.findall(k)) != set(PH.findall(v))]
    assert not wrong, wrong


def test_language_choice() -> None:
    assert resolve("de", "en-US") == "de"
    assert resolve("en", "de-DE") == "en"
    assert resolve("auto", "de-DE,de;q=0.9,en;q=0.8") == "de"
    assert resolve("auto", "fr-FR,en;q=0.5,de;q=0.4") == "en"
    assert resolve(None, "") == "en"
    assert from_header("fr") == "en"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
