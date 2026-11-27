"""Fehldrucke: Ursache, Berechnung und Nachdruck.

Ein Druck ist Fehldruck, wenn ``scrap`` = 1 ist oder (ohne Angabe) der Druck abgebrochen wurde
(``is_scrap`` aus der Datenbank). Dazu kommen:

* ``defect``: Ursache (Schlüssel aus ``DEFECTS``), leer = nicht angegeben.
* ``defect_charge``: was der Kunde zahlt. ``none`` (leer) = nichts, ``pct`` = Anteil in %,
  ``amount`` = fester Betrag, ``full`` = voller Preis.
* ``defect_value``: Prozent bzw. Betrag zu ``pct``/``amount``.
* ``reprint_of``: steht beim Nachdruck und zeigt auf den Fehldruck.

Ein Fehldruck, der (anteilig) berechnet wird, verhält sich bei Zahlung und Übergabe wie ein
normaler Druck. Ohne Berechnung fällt er wie bisher aus „Offen“ und Abrechnung heraus.
"""

from __future__ import annotations

from typing import Any

from .i18n import N_, t
from .ui import fmt_input, fmt_num

# Schlüssel → (Symbol, Name). Reihenfolge = Anzeige.
DEFECTS: dict[str, tuple[str, str]] = {
    "filament_break": ("💥", N_("Filament gebrochen")),
    "settings": ("⚙️", N_("Einstellungen")),
    "material": ("🧵", N_("Material")),
    "adhesion": ("🧲", N_("Haftung")),
    "printer": ("🔧", N_("Drucker-Defekt")),
    "customer": ("💬", N_("Kundenwunsch geändert")),
    "other": ("❓", N_("Sonstiges")),
}
# Diese Ursache liegt nicht am Druck und zählt deshalb nicht gegen die Erfolgsquote.
NOT_A_FAILURE = "customer"
CHARGES = ("pct", "amount", "full")


def defect_key(j: dict[str, Any]) -> str | None:
    k = j.get("defect")
    return k if k in DEFECTS else None


def defect_label(j: dict[str, Any], icon: bool = True) -> str:
    """Ursache als Text, z. B. „🧲 Haftung“; leer, wenn keine angegeben."""
    k = defect_key(j)
    if not k:
        return ""
    sym, name = DEFECTS[k]
    return f"{sym} {t(name)}" if icon else t(name)


def charged(j: dict[str, Any]) -> bool:
    """Fehldruck, für den (anteilig) etwas berechnet wird."""
    return bool(j.get("is_scrap")) and j.get("defect_charge") in CHARGES


def no_charge(j: dict[str, Any]) -> bool:
    """Fehldruck, für den nichts berechnet wird (fällt aus Offen und Abrechnung heraus)."""
    return bool(j.get("is_scrap")) and j.get("defect_charge") not in CHARGES


def apply_charge(j: dict[str, Any], base: float | None) -> float | None:
    """Betrag nach Fehldruck-Regel aus dem normalen Preis ``base``."""
    if not j.get("is_scrap"):
        return base
    mode, v = j.get("defect_charge"), j.get("defect_value")
    if mode == "pct":
        return None if base is None else base * (v or 0) / 100
    if mode == "amount":
        return v or 0.0
    if mode == "full":
        return base
    return 0.0


def charge_text(j: dict[str, Any]) -> str:
    """Kurz für Badges: „50 % berechnet“, „1,50 € berechnet“, „voll berechnet“; leer ohne Berechnung."""
    if not charged(j):
        return ""
    mode = j["defect_charge"]
    if mode == "pct":
        return t("{p} % berechnet", p=fmt_input(j.get("defect_value") or 0))
    if mode == "amount":
        return t("{v} berechnet", v=fmt_num(j.get("defect_value") or 0, 2, "€"))
    return t("voll berechnet")


def charge_src(j: dict[str, Any], base: float | None) -> str:
    """Herkunft des Betrags, z. B. „Fehldruck: 50 % von 3,00 €“."""
    mode = j.get("defect_charge")
    if mode == "pct":
        return t("Fehldruck: {p} % von {v}", p=fmt_input(j.get("defect_value") or 0), v=fmt_num(base, 2, "€"))
    if mode == "amount":
        return t("Fehldruck: fester Betrag statt {v}", v=fmt_num(base, 2, "€"))
    if mode == "full":
        return t("Fehldruck, voll berechnet")
    return t("Fehldruck, wird nicht berechnet")
