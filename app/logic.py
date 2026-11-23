"""Verarbeitet die printlog-Events aus Home Assistant.

Ablauf pro Druck (alle Events kommen von einer HA-Automation):

* ``start``: Druck läuft los. Zählerstand des Plugs, Auftragsname usw.
* ``stop``:  Drucker meldet finish/failed. Zählerstand zu diesem Zeitpunkt.
* ``final``: Nach der Abkühlphase (Leistung unter Standby oder Timeout).
             Zählerstand, mit dem endgültig gerechnet wird.

Kommt ``final`` nie an (z. B. HA-Neustart in der Abkühlphase), schließt
der Sweeper den Eintrag nach ``FINAL_GRACE_S`` mit dem Zählerstand vom
``stop``-Event ab.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

import json

from .costing import compute, parse_materials
from .db import Store
from . import printers as pcfg

log = logging.getLogger("printlog.logic")

FINAL_GRACE_S = 20 * 60

STATUS_DONE = "fertig"
STATUS_FAILED = "abgebrochen"
STATUS_INCOMPLETE = "unvollständig"

ImageFetcher = Callable[[int, str], Awaitable[None]]


def _num(value: Any) -> float | None:
    """Zahl aus einem HA-Wert, None bei unknown/unavailable/leer."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return None if s.lower() in ("", "unknown", "unavailable", "none") else s


class Processor:
    def __init__(self, store: Store, fetch_image: ImageFetcher) -> None:
        self.store = store
        self.fetch_image = fetch_image

    async def handle(self, data: dict[str, Any], fired_ts: float | None = None) -> None:
        phase = data.get("phase")
        printer = _text(data.get("printer"))
        if not printer:
            log.warning("Event ohne printer ignoriert: %s", data)
            return
        ts = fired_ts or time.time()
        if phase == "start":
            await self._start(printer, data, ts)
        elif phase == "stop":
            await self._stop(printer, data, ts)
        elif phase == "final":
            self._final(printer, data, ts)
        else:
            log.warning("Unbekannte phase %r ignoriert", phase)

    # ---- Phasen ----------------------------------------------------------

    async def _start(self, printer: str, data: dict[str, Any], ts: float) -> None:
        # Ein noch offener Druck desselben Druckers hat nie ein Ende gesehen.
        old = self.store.open_job(printer, "running")
        if old:
            self.store.update(
                old["id"],
                state="done",
                status=STATUS_INCOMPLETE,
                note="Kein Druckende empfangen (neuer Druck gestartet)",
            )
            log.info("Job %s ohne Ende als unvollständig geschlossen", old["id"])

        job_id = self.store.insert(
            printer=printer,
            printer_name=_text(data.get("printer_name")),
            task_name=_text(data.get("task_name")),
            state="running",
            start_ts=ts,
            start_kwh=_num(data.get("energy_kwh")),
            weight_g=_num(data.get("weight_g")),
            materials=_materials_json(data.get("materials")),
        )
        log.info("Start %s Job %s (%s)", printer, job_id, data.get("task_name"))
        image_entity = _text(data.get("image_entity"))
        if image_entity:
            await self.fetch_image(job_id, image_entity)

    async def _stop(self, printer: str, data: dict[str, Any], ts: float) -> None:
        status = STATUS_DONE if data.get("status") == "finish" else STATUS_FAILED
        job = self.store.open_job(printer, "running")
        fields: dict[str, Any] = dict(
            state="closing",
            status=status,
            end_ts=ts,
            end_kwh=_num(data.get("energy_kwh")),
            progress=_num(data.get("progress")),
            price=_num(data.get("price")),
        )
        for key in ("task_name", "printer_name"):
            val = _text(data.get(key))
            if val:
                fields[key] = val
        weight = _num(data.get("weight_g"))
        if weight:
            fields["weight_g"] = weight
        materials = _materials_json(data.get("materials"))
        if materials:
            fields["materials"] = materials

        if job is None:
            # Start nicht mitbekommen (Container war aus o. ä.).
            start_ts = _parse_iso(data.get("printer_start_time"))
            job_id = self.store.insert(
                printer=printer,
                start_ts=start_ts or ts,
                note="Druckstart nicht empfangen",
                **fields,
            )
        else:
            job_id = job["id"]
            self.store.update(job_id, **fields)
        log.info("Stop %s Job %s (%s)", printer, job_id, status)

        # Bild am Ende nochmal holen: dann gehört es sicher zu diesem Auftrag.
        image_entity = _text(data.get("image_entity"))
        if image_entity:
            await self.fetch_image(job_id, image_entity)

    def _final(self, printer: str, data: dict[str, Any], ts: float) -> None:
        job = self.store.open_job(printer, "closing")
        if job is None:
            log.warning("final für %s ohne offenen Job ignoriert", printer)
            return
        end_kwh = _num(data.get("energy_kwh"))
        if end_kwh is None:
            end_kwh = job["end_kwh"]
        price = _num(data.get("price"))
        self._close(job, end_kwh, price if price is not None else job["price"], ts)

    # ---- Abschluss -------------------------------------------------------

    def _close(self, job: dict[str, Any], end_kwh: float | None, price: float | None,
               final_ts: float, extra_note: str | None = None) -> None:
        start_kwh = job["start_kwh"]
        status = job["status"]
        notes = [n for n in (job.get("note"), extra_note) if n]
        kwh = None
        cfg = pcfg.by_key(self.store).get(job["printer"]) or {}
        has_meter = bool(cfg.get("energy")) or start_kwh is not None or end_kwh is not None
        if start_kwh is None or end_kwh is None:
            if has_meter and not any("Druckstart" in n for n in notes):
                notes.append("Zählerstand fehlte (Plug offline?)")
        elif end_kwh < start_kwh:
            notes.append("Zähler wurde zurückgesetzt")
        else:
            kwh = round(end_kwh - start_kwh, 4)
        if kwh is None:
            # Ohne Steckdose (oder wenn sie ausfiel): feste Leistung × Druckdauer.
            estimate = _estimate_kwh(cfg.get("fixed_w"), job.get("start_ts"), job.get("end_ts") or final_ts)
            if estimate is not None:
                kwh = estimate
                notes.append(f"kWh geschätzt mit {cfg['fixed_w']:g} W")
            elif not has_meter and not any("Druckstart" in n for n in notes):
                notes.append("Kein Energiezähler und kein fester Wattwert (Einstellungen → Drucker)")
        if kwh is None:
            status = STATUS_INCOMPLETE if status == STATUS_DONE else status
            if status == STATUS_FAILED:
                notes.append("Kosten unbekannt")
        cost = round(kwh * price, 4) if kwh is not None and price is not None else None
        fields = dict(
            state="done",
            status=status,
            final_ts=final_ts,
            end_kwh=end_kwh,
            kwh=kwh,
            price=price,
            cost=cost,
            note="; ".join(notes) or None,
        )
        fields.update(compute({**job, **fields}, self.store))
        self.store.update(job["id"], **fields)
        log.info("Job %s abgeschlossen: %s kWh, %s €", job["id"], kwh, cost)

    def sweep(self, default_price: float | None) -> None:
        """Schließt Jobs, deren final-Event nie kam."""
        for job in self.store.stale_closing(time.time() - FINAL_GRACE_S):
            price = job["price"] if job["price"] is not None else default_price
            self._close(job, job["end_kwh"], price, job["end_ts"],
                        extra_note="Abkühlphase nicht gemessen")


def _estimate_kwh(watts: float | None, start: float | None, end: float | None) -> float | None:
    if not watts or not start or not end or end <= start:
        return None
    return round((end - start) / 3600 * watts / 1000, 4)


def _parse_iso(value: Any) -> float | None:
    from datetime import datetime

    s = _text(value)
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _materials_json(value: Any) -> str | None:
    materials = parse_materials(value)
    return json.dumps(materials, ensure_ascii=False) if materials else None
