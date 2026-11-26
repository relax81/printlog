"""Einstiegspunkt: Web-Server, HA-Event-Abo und Sweeper."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from .backup import backup_loop
from .db import Store
from .demo import create_demo_app
from .ha import HomeAssistant
from .logic import Processor
from . import models, orders
from .recovery import Recovery
from .web import purge_expired, setup_routes

log = logging.getLogger("printlog")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
IMAGE_DIR = DATA_DIR / "images"
HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
PRICE_ENTITY = os.environ.get("PRICE_ENTITY", "input_number.strompreis")
PORT = int(os.environ.get("PORT", "8080"))
# Wartezeit vor dem Bildabruf: Die Integration lädt das Cover erst kurz nach dem Start.
IMAGE_DELAY_S = float(os.environ.get("IMAGE_DELAY_S", "60"))


async def _start_background(app: web.Application) -> None:
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    ha: HomeAssistant = app["ha"]
    ha.session = session
    store: Store = app["store"]
    pending: set[asyncio.Task] = set()

    async def fetch_image(job_id: int, entity_id: str) -> None:
        async def run() -> None:
            await asyncio.sleep(IMAGE_DELAY_S)
            try:
                name = await ha.download_image(entity_id, IMAGE_DIR / f"job_{job_id}")
            except Exception:  # noqa: BLE001
                log.exception("Bildabruf für Job %s fehlgeschlagen", job_id)
                return
            if name and store.get(job_id):
                store.update(job_id, image=name)

        task = asyncio.create_task(run())
        pending.add(task)
        task.add_done_callback(pending.discard)

    processor = Processor(store, fetch_image)

    async def get_price() -> float | None:
        try:
            state = await ha.get_state(PRICE_ENTITY) if ha.connected else None
            return float(state["state"]) if state else None
        except Exception:  # noqa: BLE001
            return None

    recovery = Recovery(store, ha, processor, fetch_image, get_price)
    processor.energy_at = recovery.energy_at  # fehlenden Startwert aus dem HA-Verlauf holen
    started = time.time()
    recovered = asyncio.Event()
    caught_up = False  # letzter Nachhol-Lauf erfolgreich

    async def on_connect() -> None:
        nonlocal caught_up
        caught_up = False
        try:
            await recovery.run()
            recovery.mark_seen()
            caught_up = True
        finally:
            recovered.set()

    async def sweeper() -> None:
        while True:
            # Nur weiterschieben, wenn bis jetzt alles nachgeholt ist. Sonst
            # versucht es die nächste Verbindung mit dem alten Zeitpunkt erneut.
            if ha.connected and caught_up:
                recovery.mark_seen()
            elif ha.connected and recovered.is_set():
                try:
                    await on_connect()  # letzter Versuch schlug fehl, erneut probieren
                except Exception:  # noqa: BLE001
                    log.exception("Nachholen verpasster Events fehlgeschlagen")
            # Erst nachholen lassen: Das kennt den Zählerstand nach der Abkühlphase,
            # der Sweeper nur den vom Druckende. Ohne HA nach 10 min trotzdem.
            if recovered.is_set() or time.time() - started > 600:
                try:
                    processor.sweep(await get_price())
                except Exception:  # noqa: BLE001
                    log.exception("Sweeper-Fehler")
                try:
                    hints = models.recognize_pending(store, IMAGE_DIR)
                    try:
                        hints += orders.match_pending(store)
                    except Exception:  # noqa: BLE001
                        log.exception("Auftrags-Zuordnung fehlgeschlagen")
                    target = (store.settings().get("hint_notify") or "").strip()
                    if hints and target and ha.connected:
                        if target == "persistent_notification":
                            target = "persistent_notification.create"
                        await ha.call_service(target, {"title": "printlog", "message": models.notify_text(hints)})
                except Exception:  # noqa: BLE001
                    log.exception("Modell-Erkennung fehlgeschlagen")
            try:
                purge_expired(store, IMAGE_DIR)
            except Exception:  # noqa: BLE001
                log.exception("Papierkorb leeren fehlgeschlagen")
            await asyncio.sleep(60)

    app["tasks"] = [
        asyncio.create_task(ha.listen(recovery.handle, on_connect)),
        asyncio.create_task(sweeper()),
        asyncio.create_task(backup_loop(store, IMAGE_DIR)),
    ]
    app["session"] = session


async def _stop_background(app: web.Application) -> None:
    for task in app["tasks"]:
        task.cancel()
    await asyncio.gather(*app["tasks"], return_exceptions=True)
    await app["session"].close()


async def _to_demo(_request: web.Request) -> web.Response:
    raise web.HTTPFound("/demo/")


def create_app() -> web.Application:
    if not HA_TOKEN:
        raise SystemExit("HA_TOKEN fehlt (Long-Lived Access Token aus Home Assistant)")
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    store = Store(DATA_DIR / "printlog.db")
    if not store.settings().get("language") and (store.count_jobs() or store.customers()):
        # Bestehende Installationen von vor der Sprachwahl bleiben deutsch; neue folgen dem Browser.
        store.set_setting("language", "de")
    ha = HomeAssistant(HA_URL, HA_TOKEN, session=None)  # type: ignore[arg-type]
    app = web.Application(client_max_size=60 * 1024**2)  # Foto-Uploads
    app["store"] = store
    app["ha"] = ha
    setup_routes(app, store, IMAGE_DIR, ha)
    # Demo-Modus: gleiche Oberfläche mit Beispieldaten, eigene Datenbank unter DATA_DIR/demo
    app.router.add_get("/demo", _to_demo)
    app.add_subapp("/demo/", create_demo_app(DATA_DIR, store))
    app.on_startup.append(_start_background)
    app.on_cleanup.append(_stop_background)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    web.run_app(create_app(), port=PORT, access_log=None)


if __name__ == "__main__":
    main()
