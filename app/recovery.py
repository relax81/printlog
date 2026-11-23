"""Holt Druck-Events nach, die verpasst wurden, während der Container aus war.

Home Assistant schickt das ``printlog``-Event nur einmal. Läuft der Container
in diesem Moment nicht (Redeploy, Neustart) oder ist die WebSocket-Verbindung
weg, ginge der Druckstart bzw. das Druckende verloren.

Nach jedem (Neu-)Verbinden liest der Container deshalb aus dem HA-Recorder,
wie sich der Druckstatus seit der letzten Verbindung geändert hat, und spielt
die Übergänge nach denselben Regeln wie die HA-Automation nach:

* ``start``: Status wechselt auf ``running``, aber nicht aus Pause/Offline.
* ``stop``:  Status wechselt von ``running``/``pause``/``prepare`` auf
             ``finish``/``failed``.
* ``final``: Für Drucke, deren Ende länger als die Abkühlphase zurückliegt,
             gilt der Zählerstand 6,5 min nach dem Ende (max. 5 min Abkühlen
             + 70 s wie in der Automation).

Der Zählerstand kommt jeweils aus dem Verlauf des Plug-Zählers zum Zeitpunkt
des Übergangs, damit sind die kWh genauso exakt wie bei einem Live-Event.
Was schon in der Datenbank steht, wird nicht doppelt angelegt.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

from .db import Store
from .ha import HomeAssistant
from .logic import Processor
from . import printers as pcfg

log = logging.getLogger("printlog.recovery")

START_NOT_FROM = {"running", "pause", "unavailable", "unknown", "offline"}
STOP_FROM = {"running", "pause", "prepare"}
STOP_TO = {"finish", "failed"}

SEEN_KEY = "ha_seen_ts"          # zuletzt mit HA verbunden (Unix-Zeit)

OVERLAP_S = 5 * 60               # Sicherheitsabstand vor der letzten Verbindung
FIRST_LOOKBACK_S = 60 * 60       # ohne bekannte letzte Verbindung
MAX_LOOKBACK_S = 7 * 24 * 3600   # Recorder hält standardmäßig 10 Tage
DUPLICATE_S = 3 * 60             # gleiches Event, wenn so nah an einem bekannten
FINAL_AFTER_S = 390              # 5 min Abkühlen + 70 s, wie in der Automation
FINAL_WAIT_S = FINAL_AFTER_S + 60

PriceGetter = Callable[[], Awaitable[float | None]]
ImageFetcher = Callable[[int, str], Awaitable[None]]


class Recovery:
    def __init__(self, store: Store, ha: HomeAssistant, processor: Processor,
                 fetch_image: ImageFetcher, get_price: PriceGetter) -> None:
        self.store = store
        self.ha = ha
        self.processor = processor
        self.fetch_image = fetch_image
        self.get_price = get_price

    # ---- Laufender Betrieb -------------------------------------------------

    async def handle(self, data: dict[str, Any], fired_ts: float | None) -> None:
        """Live-Event: Drucker-Zuordnung lernen, dann normal verarbeiten."""
        self.learn(data)
        if data.get("phase") in ("stop", "final") and _num(data.get("price")) is None:
            # Blueprint ohne Strompreis-Entität: printlog fragt selbst (PRICE_ENTITY).
            data = {**data, "price": await self.get_price()}
        await self.processor.handle(data, fired_ts)

    def learn(self, data: dict[str, Any]) -> None:
        pcfg.learn(self.store, data)

    def mark_seen(self) -> None:
        self.store.set_setting(SEEN_KEY, f"{time.time():.0f}")

    def printers(self) -> dict[str, dict[str, Any]]:
        """Drucker mit bekanntem Druckstatus-Sensor (nur die lassen sich nachholen)."""
        return {c["key"]: c for c in pcfg.load(self.store) if pcfg.status_entity(c)}

    # ---- Nachholen ---------------------------------------------------------

    async def run(self) -> None:
        now = time.time()
        settings = self.store.settings()
        try:
            seen = float(settings[SEEN_KEY])
        except (KeyError, ValueError):
            seen = None
        since = seen - OVERLAP_S if seen else now - FIRST_LOOKBACK_S
        unfinished = self.store.unfinished_jobs()
        for job in unfinished:
            if job["state"] == "closing" and job.get("end_ts"):
                since = min(since, job["end_ts"] - 60)
        since = max(since, now - MAX_LOOKBACK_S)

        printers = self.printers()
        entities: list[str] = []
        for key, cfg in printers.items():
            entities += [_status(cfg), _energy(cfg), _sensor(cfg, "task_name"),
                         _sensor(cfg, "start_time"), _sensor(cfg, "print_progress")]
        entities = list(dict.fromkeys(e for e in entities if e))
        if not entities:
            return
        history = await self.ha.get_history(entities, since, now)

        transitions = []
        for key, cfg in printers.items():
            points = history.get(_status(cfg), [])
            for (_, prev), (ts, cur) in zip(points, points[1:]):
                if ts < since:
                    continue
                if cur == "running" and prev not in START_NOT_FROM:
                    transitions.append((ts, key, "start", cur))
                elif prev in STOP_FROM and cur in STOP_TO:
                    transitions.append((ts, key, "stop", cur))
        transitions.sort()

        price = await self.get_price()
        replayed = 0
        for i, (ts, key, phase, cur) in enumerate(transitions):
            column = "start_ts" if phase == "start" else "end_ts"
            if self.store.job_near(key, column, ts, DUPLICATE_S):
                continue
            cfg = printers[key]
            latest = not any(t[1] == key for t in transitions[i + 1:])
            data = await self._event(key, cfg, phase, cur, ts, history, price, latest)
            log.info("Hole verpasstes %s-Event nach: %s um %s", phase, key,
                     time.strftime("%d.%m. %H:%M:%S", time.localtime(ts)))
            await self.processor.handle(data, ts)
            replayed += 1

        # Fehlende Vorschaubilder (Neustart kurz nach Start/Ende).
        for job in self.store.unfinished_jobs():
            cfg = printers.get(job["printer"])
            image = pcfg.image_entity(cfg) if cfg else None
            if image and not job.get("image"):
                await self.fetch_image(job["id"], image)

        # Abkühlphase verpasst: Zählerstand 6,5 min nach dem Ende nehmen.
        closed = 0
        for job in self.store.unfinished_jobs():
            if job["state"] != "closing" or not job.get("end_ts"):
                continue
            if now - job["end_ts"] < FINAL_WAIT_S:
                continue  # final kommt noch live
            final_ts = job["end_ts"] + FINAL_AFTER_S
            for ts, key, phase, _ in transitions:
                if key == job["printer"] and phase == "start" and job["end_ts"] < ts < final_ts:
                    final_ts = ts  # direkter Neustart: kein Nachlauf
                    break
            energy = _energy(printers.get(job["printer"], {}))
            kwh = _value_at(history.get(energy, []), final_ts) if energy else None
            if kwh is None or (job.get("end_kwh") is not None and kwh < job["end_kwh"]):
                continue  # dann schließt der Sweeper mit dem Wert vom Druckende
            await self.processor.handle(
                {"phase": "final", "printer": job["printer"], "energy_kwh": kwh,
                 "price": job.get("price") if job.get("price") is not None else price},
                final_ts,
            )
            closed += 1

        if replayed or closed:
            log.info("Nachgeholt: %s Start/Ende, %s Abkühlphasen", replayed, closed)

    async def _event(self, key: str, cfg: dict[str, Any], phase: str, status: str, ts: float,
                     history: dict[str, list[tuple[float, str]]], price: float | None,
                     latest: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "phase": phase,
            "printer": key,
            "status": status,
            "energy_kwh": _value_at(history.get(_energy(cfg) or "", []), ts),
            "task_name": _state_at(history.get(_sensor(cfg, "task_name"), []), ts),
            "printer_start_time": _state_at(history.get(_sensor(cfg, "start_time"), []), ts),
            "progress": _value_at(history.get(_sensor(cfg, "print_progress"), []), ts),
            "price": price,
            "image_entity": pcfg.image_entity(cfg),
        }
        if latest:
            # Seitdem kein neuer Druck: Gewicht und Slots gehören noch zu diesem Auftrag.
            weight, materials = await self._materials(cfg)
            data["weight_g"] = weight
            data["materials"] = materials
            name_entity = _sensor(cfg, "printer_name")
            name = await self.ha.get_state(name_entity) if name_entity else None
            data["printer_name"] = (name or {}).get("state")
        return data

    async def _materials(self, cfg: dict[str, Any]) -> tuple[float | None, str | None]:
        """Wie das materials-Template der Automation, aus dem aktuellen Zustand."""
        weight_entity = _sensor(cfg, "print_weight")
        weight = await self.ha.get_state(weight_entity) if weight_entity else None
        if not weight:
            return None, None
        attrs = weight.get("attributes", {})
        items = []
        for slot, grams in attrs.items():
            m = re.fullmatch(r"AMS (\d+) Tray (\d+)", slot)
            if not m:
                continue
            n, tray = int(m.group(1)), m.group(2)
            ams = cfg.get("ams") or []
            entity = f"sensor.{ams[n - 1]}_tray_{tray}" if n <= len(ams) else None
            items.append(await self._slot(slot, entity, grams))
        for slot, grams in attrs.items():
            if "xternal" in slot:
                items.append(await self._slot(slot, cfg.get("ext"), grams))
        if not items:
            items.append(await self._slot("aktiv", _sensor(cfg, "active_tray"),
                                          _num(weight.get("state")) or 0))
        return _num(weight.get("state")), json.dumps(items, ensure_ascii=False)

    async def _slot(self, slot: str, entity: str | None, grams: Any) -> dict[str, Any]:
        state = await self.ha.get_state(entity) if entity else None
        attrs = (state or {}).get("attributes", {})
        return {"slot": slot, "name": (state or {}).get("state"), "type": attrs.get("type"),
                "color": attrs.get("color"), "grams": grams}


def _status(cfg: dict[str, Any]) -> str | None:
    return pcfg.status_entity(cfg)


def _sensor(cfg: dict[str, Any], suffix: str) -> str | None:
    return pcfg.sensor(cfg, suffix)


def _energy(cfg: dict[str, Any]) -> str | None:
    return cfg.get("energy")


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _state_at(points: list[tuple[float, str]], ts: float) -> str | None:
    found = None
    for t, state in points:
        if t > ts:
            break
        found = state
    return found


def _value_at(points: list[tuple[float, str]], ts: float) -> float | None:
    """Letzter gültige Zahlenwert bis einschließlich ``ts``."""
    found = None
    for t, state in points:
        if t > ts:
            break
        value = _num(state)
        if value is not None:
            found = value
    return found
