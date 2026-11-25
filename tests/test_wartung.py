"""Wartung pro 1000 Druckstunden: Berechnung und Umrechnung alter Werte (€/Jahr ÷ Druckstunden/Jahr).

Aufruf: python -m pytest tests
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.costing import hourly_rate  # noqa: E402
from app.db import Store  # noqa: E402


def test_stundensatz() -> None:
    assert hourly_rate({"maint_per_1000h": 75}) == 0.075
    assert round(hourly_rate({"purchase_eur": 1000, "life_hours": 5000, "maint_per_1000h": 60}), 6) == 0.26
    # Alter Verschleiß zählt nicht mehr extra (steckt nach der Umrechnung in der Wartung)
    assert hourly_rate({"extra_per_hour": 0.05}) is None
    assert hourly_rate({"maint_per_1000h": None}) is None


def test_umrechnung_alter_werte() -> None:
    path = Path(tempfile.mkdtemp()) / "alt.db"
    Store(path)
    conn = sqlite3.connect(path)
    # Stand vor der Umstellung: Spalte maint_per_1000h gibt es noch nicht.
    conn.execute("ALTER TABLE printer_rates DROP COLUMN maint_per_1000h")
    conn.executemany("INSERT INTO printer_rates (printer, purchase_eur, life_hours, maint_per_year, hours_per_year, "
                     "extra_per_hour) VALUES (?, ?, ?, ?, ?, ?)",
                     [("a", 1000, 5000, 60, 800, None), ("b", None, None, 50, None, None), ("c", 500, 2500, None, None, None),
                      ("d", 1000, 5000, 60, 800, 0.05), ("e", None, None, None, None, 0.02)])
    conn.commit()
    conn.close()
    store = Store(path)
    rates = store.printer_rates()
    assert rates["a"]["maint_per_1000h"] == 75.0
    assert hourly_rate(rates["a"]) == 1000 / 5000 + 60 / 800
    assert rates["b"]["maint_per_1000h"] is None  # ohne Druckstunden/Jahr zählte die Wartung schon bisher nicht
    assert rates["c"]["maint_per_1000h"] is None
    # Verschleiß €/h wandert ×1000 in die Wartung, der Stundensatz bleibt gleich
    assert rates["d"]["maint_per_1000h"] == 125.0
    assert hourly_rate(rates["d"]) == 1000 / 5000 + 60 / 800 + 0.05
    assert rates["e"]["maint_per_1000h"] == 20.0
    # Speichern über die Einstellungen schreibt nur noch den neuen Wert
    store.set_printer_rate("a", purchase_eur=1000, life_hours=5000, maint_per_1000h=80)
    assert store.printer_rates()["a"]["maint_per_1000h"] == 80
    assert store.printer_rates_set()
