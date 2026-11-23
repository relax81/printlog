"""Sprachen der Oberfläche (Deutsch und Englisch).

Die Texte im Code sind deutsch und dienen zugleich als Schlüssel. ``t("…")`` gibt
in der aktuellen Sprache den passenden Text zurück, die englischen stehen in
``i18n_en.py``. Die Sprache gilt pro Anfrage (``LANG``) und kommt aus den
Einstellungen: ``de``, ``en`` oder ``auto`` (Sprache des Browsers).

* ``t(text, **werte)``: übersetzen, danach ``str.format`` mit den Werten.
* ``tn(n, einzahl, mehrzahl, **werte)``: Einzahl/Mehrzahl, ``{n}`` ist die Zahl.
* ``N_(text)``: markiert Texte in Tabellen, die erst bei der Ausgabe übersetzt werden.
* ``js(code)``: ersetzt ``_t("…")`` in JavaScript durch den übersetzten Text als JS-String.
"""

from __future__ import annotations

import contextvars
import json
import re
from typing import Any

from .i18n_en import EN

LANGS = {"de": "Deutsch", "en": "English"}
DEFAULT = "de"
LANG: contextvars.ContextVar[str] = contextvars.ContextVar("printlog_lang", default=DEFAULT)


def lang() -> str:
    return LANG.get()


def is_en() -> bool:
    return LANG.get() == "en"


def t(text: str, **values: Any) -> str:
    if LANG.get() == "en":
        text = EN.get(text, text)
    return text.format(**values) if values else text


def tn(n: int | float, one: str, many: str, **values: Any) -> str:
    return t(one if n == 1 else many, n=n, **values)


def N_(text: str) -> str:  # noqa: N802 – Markierung wie bei gettext
    return text


_JS_RE = re.compile(r'_t\("((?:[^"\\]|\\.)*)"\)')


def js(code: str) -> str:
    return _JS_RE.sub(lambda m: json.dumps(t(m.group(1)), ensure_ascii=False), code)


def from_header(accept: str | None) -> str:
    """Bevorzugte unterstützte Sprache aus Accept-Language, sonst Englisch."""
    found: list[tuple[float, int, str]] = []
    for i, part in enumerate((accept or "").split(",")):
        tag, _, params = part.strip().partition(";")
        code = tag.strip().lower()[:2]
        if code not in LANGS:
            continue
        m = re.search(r"q=([0-9.]+)", params)
        try:
            q = float(m.group(1)) if m else 1.0
        except ValueError:
            q = 0.0
        if q > 0:
            found.append((-q, i, code))
    return min(found)[2] if found else "en"


def resolve(setting: str | None, accept: str | None) -> str:
    """Einstellung ``language`` (de/en/auto) plus Browser-Sprache → de oder en."""
    if setting in LANGS:
        return setting
    return from_header(accept)


# Hinweise, die printlog beim Speichern eines Drucks festhält (Spalte ``note``,
# mehrere mit „; “ getrennt). Sie stehen deutsch in der Datenbank und werden erst
# bei der Anzeige übersetzt.
_NOTE_PATTERNS = [
    (re.compile(r"^kWh geschätzt mit (?P<w>[0-9.,]+) W$"), "kWh geschätzt mit {w} W"),
]


def note(text: str | None) -> str:
    if not text or LANG.get() != "en":
        return text or ""
    out = []
    for part in text.split("; "):
        for rx, key in _NOTE_PATTERNS:
            m = rx.match(part)
            if m:
                part = t(key, **m.groupdict())
                break
        else:
            part = t(part)
        out.append(part)
    return "; ".join(out)
