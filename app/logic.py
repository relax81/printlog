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

from .i18n import N_
from .costing import compute, parse_materials
from .db import Store
from . import printers as pcfg

log = logging.getLogger("printlog.logic")

FINAL_GRACE_S = 20 * 60

STATUS_DONE = "fertig"
STATUS_FAILED = "abgebrochen"
STATUS_INCOMPLETE = "unvollständig"

ImageFetcher = Callable[[int, str], Awaitable[None]]
# Zählerstand eines Druckers zu einem Zeitpunkt (aus dem HA-Verlauf), None wenn unbekannt.
EnergyLookup = Callable[[str, float], Awaitable[float | None]]

# Gleicher Druck, wenn die Startzeit laut Drucker so nah an der gespeicherten liegt.
SAME_PRINT_S = 2 * 60
# Ältere Einträge ohne gespeicherte Druckerstartzeit: Abstand zum Start in printlog.
SAME_PRINT_FALLBACK_S = 15 * 60


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
    def __init__(self, store: Store, fetch_image: ImageFetcher,
                 energy_at: EnergyLookup | None = None) -> None:
        self.store = store
        self.fetch_image = fetch_image
        self.energy_at = energy_at

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
        printer_start = _parse_iso(data.get("printer_start_time"))
        old = self.store.open_job(printer, "running")
        if old and _same_print(old, printer_start, data):
            # Weiter nach einer Pause (Magnete einsetzen, Platte prüfen …) oder ein
            # nachgereichtes Start-Event: derselbe Druck, kein neuer Eintrag.
            fields: dict[str, Any] = {}
            if old.get("start_kwh") is None:
                kwh = await self._energy_at(printer, printer_start)
                if kwh is None:
                    kwh = _num(data.get("energy_kwh"))
                if kwh is not None:
                    fields["start_kwh"] = kwh
            if printer_start and not old.get("printer_start_ts"):
                fields["printer_start_ts"] = printer_start
            for key in ("task_name", "printer_name"):
                val = _text(data.get(key))
                if val and not old.get(key):
                    fields[key] = val
            if fields:
                self.store.update(old["id"], **fields)
            log.info("Start %s: Job %s läuft weiter (gleicher Druck)", printer, old["id"])
            return
        if old:
            # Ein noch offener Druck desselben Druckers hat nie ein Ende gesehen.
            self.store.update(
                old["id"],
                state="done",
                status=STATUS_INCOMPLETE,
                note=N_("Kein Druckende empfangen (neuer Druck gestartet)"),
            )
            log.info("Job %s ohne Ende als unvollständig geschlossen", old["id"])

        job_id = self.store.insert(
            printer=printer,
            printer_name=_text(data.get("printer_name")),
            task_name=_text(data.get("task_name")),
            state="running",
            start_ts=ts,
            printer_start_ts=printer_start,
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

        printer_start = _parse_iso(data.get("printer_start_time"))
        if job is None:
            # Start nicht mitbekommen (Container war aus, Start-Event fehlte o. ä.).
            # Den Zählerstand zur Startzeit des Druckers holt printlog aus dem HA-Verlauf.
            start_kwh = await self._energy_at(printer, printer_start)
            job_id = self.store.insert(
                printer=printer,
                start_ts=printer_start or ts,
                printer_start_ts=printer_start,
                start_kwh=start_kwh,
                note=N_("Startwert aus HA-Verlauf ergänzt") if start_kwh is not None
                else N_("Druckstart nicht empfangen"),
                **fields,
            )
        else:
            job_id = job["id"]
            if job.get("start_kwh") is None:
                # Plug war beim Start offline o. ä.: Startwert nachträglich holen.
                start_kwh = await self._energy_at(printer, printer_start or job.get("start_ts"))
                if start_kwh is not None:
                    fields["start_kwh"] = start_kwh
            self.store.update(job_id, **fields)
        log.info("Stop %s Job %s (%s)", printer, job_id, status)

        # Bild am Ende nochmal holen: dann gehört es sicher zu diesem Auftrag.
        image_entity = _text(data.get("image_entity"))
        if image_entity:
            await self.fetch_image(job_id, image_entity)

    async def _energy_at(self, printer: str, ts: float | None) -> float | None:
        if not ts or not self.energy_at:
            return None
        try:
            return await self.energy_at(printer, ts)
        except Exception:  # noqa: BLE001
            log.exception("Zählerstand für %s aus dem HA-Verlauf fehlgeschlagen", printer)
            return None

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
                notes.append(N_("Zählerstand fehlte (Plug offline?)"))
        elif end_kwh < start_kwh:
            notes.append(N_("Zähler wurde zurückgesetzt"))
        else:
            kwh = round(end_kwh - start_kwh, 4)
        if kwh is None:
            # Ohne Steckdose (oder wenn sie ausfiel): feste Leistung × Druckdauer.
            estimate = _estimate_kwh(cfg.get("fixed_w"), job.get("start_ts"), job.get("end_ts") or final_ts)
            if estimate is not None:
                kwh = estimate
                notes.append(f"kWh geschätzt mit {cfg['fixed_w']:g} W")  # Übersetzung: i18n.note
            elif not has_meter and not any("Druckstart" in n for n in notes):
                notes.append(N_("Kein Energiezähler und kein fester Wattwert (Einstellungen → Drucker)"))
        if kwh is None:
            status = STATUS_INCOMPLETE if status == STATUS_DONE else status
            if status == STATUS_FAILED:
                notes.append(N_("Kosten unbekannt"))
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

    async def repair_missing_start(self, since_ts: float) -> int:
        """Drucke, deren Start fehlte, nachträglich mit dem Zählerstand aus dem HA-Verlauf rechnen."""
        repaired = 0
        for job in self.store.missing_start_jobs(since_ts):
            start = job.get("printer_start_ts") or job.get("start_ts")
            start_kwh = await self._energy_at(job["printer"], start)
            if start_kwh is None or start_kwh > job["end_kwh"]:
                continue
            drop = {"Druckstart nicht empfangen", "Kosten unbekannt"}
            notes = [n for n in (job.get("note") or "").split("; ") if n and n not in drop]
            job = {**job, "start_kwh": start_kwh,
                   "note": "; ".join(notes + [N_("Startwert aus HA-Verlauf ergänzt")]),
                   # „unvollständig“ kam hier nur von den fehlenden kWh, das Ende war „fertig“.
                   "status": STATUS_DONE if job["status"] == STATUS_INCOMPLETE else job["status"]}
            self.store.update(job["id"], start_kwh=start_kwh)
            self._close(job, job["end_kwh"], job["price"], job.get("final_ts") or job.get("end_ts"))
            log.info("Job %s: Startwert %s kWh aus dem HA-Verlauf nachgetragen", job["id"], start_kwh)
            repaired += 1
        return repaired

    def sweep(self, default_price: float | None) -> None:
        """Schließt Jobs, deren final-Event nie kam."""
        for job in self.store.stale_closing(time.time() - FINAL_GRACE_S):
            price = job["price"] if job["price"] is not None else default_price
            self._close(job, job["end_kwh"], price, job["end_ts"],
                        extra_note=N_("Abkühlphase nicht gemessen"))


def _same_print(job: dict[str, Any], printer_start: float | None, data: dict[str, Any]) -> bool:
    """Gehört ein Start-Event zum noch laufenden Job (z. B. Weiterdrucken nach Pause)?"""
    if printer_start:
        known = job.get("printer_start_ts")
        if known:
            return abs(printer_start - known) <= SAME_PRINT_S
        start = job.get("start_ts")
        return bool(start) and abs(printer_start - start) <= SAME_PRINT_FALLBACK_S
    # Ohne Startzeit vom Drucker: Ein neuer Druck beginnt bei 0 %, ein fortgesetzter nicht.
    progress = _num(data.get("progress"))
    return progress is not None and progress > 0


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
