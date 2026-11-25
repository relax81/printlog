"""Berechnet Drucker-, Filament- und Gesamtkosten eines Drucks.

* Drucker: Druckdauer × (Anschaffungspreis / Lebensdauer + Verschleiß pro Stunde
  + Wartungskosten pro 1000 Druckstunden / 1000)
* Filament: Gramm pro Slot × €/kg. Preis nach Filamentname, sonst nach Typ.
  Bei Abbruch nur der Anteil bis zum Abbruch (Fortschritt in %).
* Strom: kommt schon fertig aus der Messung (Spalte ``cost``).
"""

from __future__ import annotations

import json
import re
from typing import Any

from .db import Store

KNOWN_TYPES = ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "HIPS", "PET"]


def parse_materials(value: Any) -> list[dict[str, Any]]:
    """HA schickt die Liste als JSON-Text oder schon als Liste."""
    if value in (None, "", "unknown", "unavailable"):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    out = []
    for m in value:
        if not isinstance(m, dict):
            continue
        name = _clean(m.get("name"))
        mtype = _clean(m.get("type")) or guess_type(name)
        try:
            grams = float(m.get("grams") or 0)
        except (TypeError, ValueError):
            grams = 0.0
        if grams <= 0 and not name:
            continue
        out.append(
            {
                "slot": _clean(m.get("slot")),
                "name": name,
                "type": mtype,
                "color": _clean(m.get("color")),
                "grams": round(grams, 2),
            }
        )
    return out


def material_notes(job: dict[str, Any]) -> dict[int, str]:
    """Eigene Notizen je Material-Zeile (z. B. genauer Farbname), Schlüssel = Index in ``materials``."""
    raw = job.get("material_notes")
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {int(k): str(v).strip() for k, v in data.items() if str(k).isdigit() and str(v or "").strip()}


def guess_type(name: str | None) -> str | None:
    if not name:
        return None
    for t in sorted(KNOWN_TYPES, key=len, reverse=True):
        if re.search(rf"\b{t}\b", name, re.IGNORECASE):
            return t
    return None


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in ("", "none", "unknown", "unavailable", "empty") else s


def hourly_rate(rate: dict[str, Any] | None) -> float | None:
    if not rate:
        return None
    total = 0.0
    found = False
    if rate.get("purchase_eur") and rate.get("life_hours"):
        total += rate["purchase_eur"] / rate["life_hours"]
        found = True
    if rate.get("extra_per_hour"):
        total += rate["extra_per_hour"]
        found = True
    if rate.get("maint_per_1000h"):
        total += rate["maint_per_1000h"] / 1000
        found = True
    return total if found else None


def compute(job: dict[str, Any], store: Store) -> dict[str, Any]:
    """Gibt die Kostenfelder für einen abgeschlossenen Job zurück."""
    factor = 1.0
    if job.get("status") == "abgebrochen" and job.get("progress") is not None:
        factor = max(0.0, min(1.0, job["progress"] / 100.0))

    # Drucker
    cost_printer = None
    rate = hourly_rate(store.printer_rates().get(job["printer"]))
    if rate is not None and job.get("start_ts") and job.get("end_ts") and job["end_ts"] > job["start_ts"]:
        cost_printer = round((job["end_ts"] - job["start_ts"]) / 3600 * rate, 4)

    # Filament
    materials = parse_materials(job.get("materials"))
    prices = store.material_prices()
    cost_filament = None
    filament_g = None
    if materials:
        filament_g = round(sum(m["grams"] for m in materials) * factor, 2)
        for m in materials:
            price = (prices.get(m["name"] or "") or prices.get(m["type"] or "") or {}).get("eur_per_kg")
            if price is not None:
                cost_filament = (cost_filament or 0.0) + m["grams"] * factor / 1000 * price
        if cost_filament is not None:
            cost_filament = round(cost_filament, 4)
    elif job.get("weight_g"):
        filament_g = round(job["weight_g"] * factor, 2)

    parts = [c for c in (job.get("cost"), cost_printer, cost_filament) if c is not None]
    return {
        "cost_printer": cost_printer,
        "cost_filament": cost_filament,
        "filament_g": filament_g,
        "cost_total": round(sum(parts), 4) if parts else None,
    }


def recompute_all(store: Store) -> int:
    jobs = store.done_jobs()
    for job in jobs:
        store.update(job["id"], **compute(job, store))
    store.refresh_projects()
    return len(jobs)
