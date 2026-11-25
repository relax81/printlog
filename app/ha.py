"""Verbindung zu Home Assistant: Event-Abo per WebSocket und Bildabruf per REST.

Der Container spricht nur mit Home Assistant, nie mit der Bambu-Cloud. Das
Vorschaubild kommt aus dem Bild, das die Bambu-Integration in HA ohnehin
schon zwischengespeichert hat.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger("printlog.ha")

EVENT_TYPE = "printlog"
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}

EventHandler = Callable[[dict[str, Any], float | None], Awaitable[None]]
ConnectHandler = Callable[[], Awaitable[None]]


class HomeAssistant:
    def __init__(self, base_url: str, token: str, session: aiohttp.ClientSession) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = session
        self.connected = False
        self.last_event: str | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    # ---- REST ------------------------------------------------------------

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        url = f"{self.base_url}/api/states/{entity_id}"
        async with self.session.get(url, headers=self._headers) as resp:
            if resp.status != 200:
                log.warning("Status %s für %s", resp.status, entity_id)
                return None
            return await resp.json()

    async def call_service(self, service: str, data: dict[str, Any]) -> bool:
        """Dienst aufrufen, z. B. ``notify.mobile_app_x`` oder ``persistent_notification.create``."""
        domain, _, name = service.partition(".")
        if not domain or not name:
            return False
        url = f"{self.base_url}/api/services/{domain}/{name}"
        async with self.session.post(url, headers=self._headers, json=data) as resp:
            if resp.status >= 300:
                log.warning("Dienst %s: HTTP %s", service, resp.status)
                return False
            return True

    async def download_image(self, entity_id: str, target_stem: Path) -> str | None:
        """Lädt das aktuelle Bild einer image-Entität. Gibt den Dateinamen zurück."""
        state = await self.get_state(entity_id)
        picture = (state or {}).get("attributes", {}).get("entity_picture")
        if not picture:
            log.warning("Kein entity_picture für %s", entity_id)
            return None
        async with self.session.get(self.base_url + picture, headers=self._headers) as resp:
            if resp.status != 200:
                log.warning("Bildabruf %s: HTTP %s", entity_id, resp.status)
                return None
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
            data = await resp.read()
        if not data:
            return None
        ext = EXTENSIONS.get(ctype, ".png")
        target = target_stem.with_suffix(ext)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Alte Varianten mit anderer Endung entfernen.
        for old in target.parent.glob(target_stem.name + ".*"):
            if old != target:
                old.unlink(missing_ok=True)
        target.write_bytes(data)
        return target.name

    async def get_history(self, entity_ids: list[str], start_ts: float,
                          end_ts: float) -> dict[str, list[tuple[float, str]]]:
        """Zustandsverlauf aus dem HA-Recorder: {entity_id: [(ts, state), ...]}.

        Der erste Eintrag ist der Zustand zu Beginn des Zeitraums.
        """
        start = _iso(start_ts)
        end = _iso(end_ts)
        params = {
            "filter_entity_id": ",".join(entity_ids),
            "end_time": end,
            "minimal_response": "",
            "no_attributes": "",
            "significant_changes_only": "0",
        }
        url = f"{self.base_url}/api/history/period/{start}"
        async with self.session.get(url, headers=self._headers, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"History-Abruf: HTTP {resp.status}")
            data = await resp.json()
        out: dict[str, list[tuple[float, str]]] = {}
        for series in data or []:
            if not series:
                continue
            entity_id = series[0].get("entity_id")
            if not entity_id:
                continue
            points = []
            for item in series:
                try:
                    ts = datetime.fromisoformat(item["last_changed"]).timestamp()
                except (KeyError, ValueError):
                    continue
                points.append((ts, str(item.get("state"))))
            out[entity_id] = points
        return out

    # ---- WebSocket -------------------------------------------------------

    async def listen(self, handler: EventHandler,
                     on_connect: ConnectHandler | None = None) -> None:
        """Hält das Event-Abo offen und verbindet bei Abbruch neu.

        ``on_connect`` läuft nach jedem (Neu-)Abo, bevor die ersten Events
        verarbeitet werden. Events, die währenddessen eintreffen, warten im
        Socket und kommen danach an.
        """
        ws_url = self.base_url.replace("http", "ws", 1) + "/api/websocket"
        delay = 5
        while True:
            try:
                async with self.session.ws_connect(ws_url, heartbeat=30) as ws:
                    await self._auth(ws)
                    await ws.send_json(
                        {"id": 1, "type": "subscribe_events", "event_type": EVENT_TYPE}
                    )
                    self.connected = True
                    delay = 5
                    log.info("Mit Home Assistant verbunden, warte auf %s-Events", EVENT_TYPE)
                    if on_connect is not None:
                        try:
                            await on_connect()
                        except Exception:  # noqa: BLE001 - Nachholen darf das Abo nicht verhindern
                            log.exception("Nachholen verpasster Events fehlgeschlagen")
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        payload = json.loads(msg.data)
                        if payload.get("type") == "result" and not payload.get("success"):
                            raise RuntimeError(f"Abo abgelehnt: {payload}")
                        if payload.get("type") != "event":
                            continue
                        event = payload.get("event", {})
                        self.last_event = event.get("time_fired")
                        try:
                            await handler(event.get("data", {}), _fired(event))
                        except Exception:  # noqa: BLE001 - ein kaputtes Event darf das Abo nicht beenden
                            log.exception("Fehler beim Verarbeiten von %s", event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("HA-Verbindung verloren (%s), neuer Versuch in %ss", exc, delay)
            self.connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)

    async def _auth(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        hello = await ws.receive_json()
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unerwartete Begrüßung: {hello}")
        await ws.send_json({"type": "auth", "access_token": self.token})
        result = await ws.receive_json()
        if result.get("type") != "auth_ok":
            raise RuntimeError("Token abgelehnt – HA_TOKEN prüfen")


def _fired(event: dict[str, Any]) -> float | None:
    try:
        return datetime.fromisoformat(event["time_fired"]).timestamp()
    except (KeyError, ValueError):
        return None


def _iso(ts: float) -> str:
    # "Z" statt "+00:00": ein "+" würde in der Query zu einem Leerzeichen.
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")
