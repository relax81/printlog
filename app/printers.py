"""Drucker-Zuordnung: welche Home-Assistant-Entitäten zu welchem Drucker gehören.

Gespeichert als JSON-Liste in der Einstellung ``printers``. Die Reihenfolge ist
auch die feste Farbreihenfolge in der Statistik. Pro Drucker:

* ``key``     Kurzname, unter dem die Drucke laufen (z. B. ``h2s``)
* ``name``    Anzeigename
* ``status``  Druckstatus-Sensor (``sensor.<prefix>_print_status``)
* ``prefix``  Namensanfang der übrigen Bambu-Entitäten (aus ``status`` ableitbar)
* ``ams``     Namensanfänge der AMS-Einheiten, Reihenfolge = AMS 1, 2 …
* ``ext``     Sensor der externen Spule
* ``energy``  Energiezähler in kWh (Steckdose), leer = keine Steckdose
* ``fixed_w`` Feste Leistung in W, wenn kein Zähler da ist oder er ausfällt

Die Werte lassen sich unter Einstellungen → Drucker pflegen. Schickt die
HA-Automation (Blueprint) die Entitäten im Event mit, übernimmt printlog sie
automatisch, neue Drucker legt es dabei selbst an.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .db import Store

log = logging.getLogger("printlog.printers")

SETTING = "printers"
KEY_RE = re.compile(r"[a-z0-9_]{1,20}")
ENTITY_RE = re.compile(r"[a-z_]+\.[a-z0-9_]+")
LEGACY_PREFIX_KEY = "printer_prefix:"   # früher aus image_entity gelernte Präfixe

# Optionales Namensschema für die Übernahme bestehender Installationen, z. B.
# "sensor.werkstatt_{printer}_plug_energy". Leer = Energiezähler von Hand eintragen.
_LEGACY_ENERGY = os.environ.get("ENERGY_ENTITY", "")


# ---- Laden und Speichern ---------------------------------------------------

def load(store: Store) -> list[dict[str, Any]]:
    raw = store.settings().get(SETTING)
    if raw is None:
        cfgs = _migrate(store)
        save(store, cfgs)
        if cfgs:
            log.info("Drucker-Zuordnung übernommen: %s", ", ".join(c["key"] for c in cfgs))
        return cfgs
    try:
        items = json.loads(raw)
    except ValueError:
        log.error("Einstellung %s ist kein JSON, ignoriert", SETTING)
        return []
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
        cfg = normalize(item)
        if cfg and cfg["key"] not in seen:
            seen.add(cfg["key"])
            out.append(cfg)
    return out


def by_key(store: Store) -> dict[str, dict[str, Any]]:
    return {c["key"]: c for c in load(store)}


def save(store: Store, cfgs: list[dict[str, Any]]) -> None:
    store.set_setting(SETTING, json.dumps([normalize(c) for c in cfgs], ensure_ascii=False))


def normalize(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    key = str(item.get("key") or "").strip().lower()
    if not KEY_RE.fullmatch(key):
        return None
    ams = item.get("ams") or []
    if isinstance(ams, str):
        ams = [a for a in re.split(r"[,\s]+", ams) if a]
    ams = [_ams_prefix(a) for a in ams if isinstance(a, str) and a.strip()]
    try:
        fixed_w = float(item["fixed_w"]) if item.get("fixed_w") not in (None, "") else None
    except (TypeError, ValueError):
        fixed_w = None
    return {
        "key": key,
        "name": str(item.get("name") or "").strip() or None,
        "status": _entity(item.get("status")),
        "prefix": _clean(item.get("prefix")),
        "ams": [a for a in ams if a],
        "ext": _entity(item.get("ext")),
        "energy": _entity(item.get("energy")),
        "fixed_w": fixed_w if fixed_w and fixed_w > 0 else None,
    }


def _migrate(store: Store) -> list[dict[str, Any]]:
    """Erster Start einer Installation von vor der Drucker-Zuordnung.

    Übernimmt die Drucker aus den vorhandenen Drucken und die früher aus
    ``image_entity`` gelernten Namensanfänge. Eine neue Installation startet leer.
    """
    seen_printers = {p["printer"]: p["printer_name"] for p in store.printers()}
    learned = {k[len(LEGACY_PREFIX_KEY):]: v for k, v in store.settings().items()
               if k.startswith(LEGACY_PREFIX_KEY)}
    out = []
    for key in sorted(set(seen_printers) | set(learned)):
        prefix = learned.get(key)
        out.append({"key": key, "name": seen_printers.get(key), "prefix": prefix,
                    "status": f"sensor.{prefix}_print_status" if prefix else None,
                    "energy": _LEGACY_ENERGY.format(printer=key) if _LEGACY_ENERGY else None})
    return [c for c in (normalize(c) for c in out) if c]


# ---- Abgeleitete Werte -----------------------------------------------------

def prefix(cfg: dict[str, Any]) -> str | None:
    if cfg.get("prefix"):
        return cfg["prefix"]
    m = re.fullmatch(r"sensor\.(.+)_print_status", cfg.get("status") or "")
    return m.group(1) if m else None


def status_entity(cfg: dict[str, Any]) -> str | None:
    if cfg.get("status"):
        return cfg["status"]
    p = prefix(cfg)
    return f"sensor.{p}_print_status" if p else None


def sensor(cfg: dict[str, Any], suffix: str) -> str | None:
    p = prefix(cfg)
    return f"sensor.{p}_{suffix}" if p else None


def image_entity(cfg: dict[str, Any]) -> str | None:
    p = prefix(cfg)
    return f"image.{p}_cover_image" if p else None


def entities(cfg: dict[str, Any]) -> list[str]:
    """Alle Entitäten, die sich unter Einstellungen prüfen lassen."""
    out = [status_entity(cfg), cfg.get("energy"), cfg.get("ext")]
    out += [f"sensor.{a}_tray_1" for a in cfg.get("ams") or []]
    return [x for x in out if x]


# ---- Aus Events lernen -----------------------------------------------------

def learn(store: Store, data: dict[str, Any]) -> None:
    """Übernimmt Drucker und Entitäten aus einem printlog-Event.

    Die Blueprint-Automation schickt ``status_entity``, ``energy_entity``,
    ``prefix``, ``ams`` und ``ext`` mit, die alte Automation nur
    ``image_entity`` (daraus wird der Namensanfang). Gesetzte Werte im Event
    gewinnen, leere ändern nichts. Name und fester Wattwert bleiben, wie sie
    unter Einstellungen stehen.
    """
    key = str(data.get("printer") or "").strip().lower()
    if not KEY_RE.fullmatch(key):
        return
    update: dict[str, Any] = {}
    m = re.fullmatch(r"image\.(.+)_cover_image", str(data.get("image_entity") or ""))
    if m:
        update["prefix"] = m.group(1)
    for field, event_key in (("status", "status_entity"), ("energy", "energy_entity"),
                             ("ext", "ext"), ("prefix", "prefix")):
        value = _clean(data.get(event_key))
        if value:
            update[field] = value
    ams = data.get("ams")
    if isinstance(ams, str):
        try:
            ams = json.loads(ams)
        except ValueError:
            ams = [a for a in re.split(r"[,\s]+", ams) if a]
    if isinstance(ams, list) and ams:
        update["ams"] = ams

    cfgs = load(store)
    cfg = next((c for c in cfgs if c["key"] == key), None)
    if cfg is None:
        if not update:
            return
        name = _clean(data.get("printer_name"))
        cfgs.append(normalize({"key": key, "name": name, **update}))
        save(store, cfgs)
        log.info("Neuer Drucker aus Event: %s", key)
        return
    merged = normalize({**cfg, **update})
    if merged and merged != cfg:
        cfgs[cfgs.index(cfg)] = merged
        save(store, cfgs)
        log.info("Drucker %s aus Event aktualisiert", key)


# ---- Hilfen ----------------------------------------------------------------

def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return None if s.lower() in ("", "none", "unknown", "unavailable", "[]") else s


def _entity(value: Any) -> str | None:
    s = _clean(value)
    return s.lower() if s and ENTITY_RE.fullmatch(s.lower()) else None


def _ams_prefix(value: str) -> str:
    """``sensor.x_ams_1_tray_1`` oder ``x_ams_1`` → ``x_ams_1``."""
    s = value.strip().lower()
    s = re.sub(r"^sensor\.", "", s)
    return re.sub(r"_tray_\d+$", "", s)
