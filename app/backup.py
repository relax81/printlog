"""Backup (täglich oder wöchentlich): Datenbank-Kopie plus Bilder in einen (Netzwerk-)Ordner.

Ziel ist ``BACKUP_DIR`` (im Container, z. B. ``/backup``), das per Volume auf eine
NAS-Freigabe zeigt. Ohne ``BACKUP_DIR`` passiert nichts. Intervall, Wochentag und
Uhrzeit kommen aus den Einstellungen (Tabelle ``settings``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from .db import Store

log = logging.getLogger("printlog.backup")

BACKUP_DIR = os.environ.get("BACKUP_DIR", "").strip()
BACKUP_HOUR = int(os.environ.get("BACKUP_HOUR", "3"))
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "14"))  # so viele Stände der Datenbank

INTERVALS = {"daily": "Täglich", "weekly": "Wöchentlich"}
DEFAULT_INTERVAL = "weekly"
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
DEFAULT_WEEKDAY = 6  # Sonntag


def schedule(cfg: dict[str, str]) -> tuple[str, int, int]:
    """(Intervall, Wochentag 0=Mo, Stunde) aus den Einstellungen, mit Standardwerten."""
    interval = cfg.get("backup_interval") or DEFAULT_INTERVAL
    if interval not in INTERVALS:
        interval = DEFAULT_INTERVAL

    def num(key: str, default: int, hi: int) -> int:
        try:
            v = int(cfg.get(key) or default)
        except ValueError:
            return default
        return v if 0 <= v <= hi else default

    return interval, num("backup_weekday", DEFAULT_WEEKDAY, 6), num("backup_hour", BACKUP_HOUR, 23)


def next_run(cfg: dict[str, str], now: datetime) -> datetime:
    interval, weekday, hour = schedule(cfg)
    nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if interval == "weekly":
        nxt += timedelta(days=(weekday - nxt.weekday()) % 7)
        if nxt <= now:
            nxt += timedelta(days=7)
    elif nxt <= now:
        nxt += timedelta(days=1)
    return nxt


def describe(cfg: dict[str, str]) -> str:
    interval, weekday, hour = schedule(cfg)
    if interval == "weekly":
        return f"Jeden {WEEKDAYS[weekday]} um {hour}:00 Uhr"
    return f"Jede Nacht um {hour}:00 Uhr" if hour < 6 else f"Täglich um {hour}:00 Uhr"


def enabled() -> bool:
    return bool(BACKUP_DIR)


def run_backup(store: Store, image_dir: Path) -> str:
    """Erstellt einen Stand und gibt eine kurze Zusammenfassung zurück."""
    target = Path(BACKUP_DIR)
    if not target.is_dir():
        raise RuntimeError(f"Backup-Ordner {target} fehlt oder ist nicht eingehängt")
    db_dir = target / "db"
    img_dir = target / "images"
    db_dir.mkdir(exist_ok=True)
    img_dir.mkdir(exist_ok=True)

    name = f"printlog-{datetime.now():%Y-%m-%d}.db"
    tmp = db_dir / (name + ".tmp")
    store.backup_to(tmp)
    tmp.replace(db_dir / name)
    for old in sorted(db_dir.glob("printlog-*.db"))[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)

    # Bilder spiegeln: neue und geänderte kopieren. Gelöschte bleiben im Backup.
    copied = 0
    for src in image_dir.iterdir():
        if not src.is_file():
            continue
        dst = img_dir / src.name
        st = src.stat()
        if dst.exists() and dst.stat().st_size == st.st_size:
            continue
        shutil.copy2(src, dst)
        copied += 1
    return f"Datenbank {name}, {copied} neue Bilder"


async def backup_loop(store: Store, image_dir: Path) -> None:
    if not enabled():
        log.info("Kein BACKUP_DIR gesetzt, Backup ist aus")
        return
    nxt: datetime | None = None
    while True:
        now = datetime.now()
        if nxt is not None and now >= nxt:
            await backup_now(store, image_dir)
            nxt = None
            continue
        # Jede Minute neu planen, damit geänderte Einstellungen ohne Neustart greifen.
        nxt = next_run(store.settings(), now)
        await asyncio.sleep(min(60.0, max(1.0, (nxt - now).total_seconds())))


async def backup_now(store: Store, image_dir: Path) -> None:
    try:
        msg = await asyncio.get_running_loop().run_in_executor(None, run_backup, store, image_dir)
        store.set_setting("backup_last", f"{time.time():.0f}|ok|{msg}")
        log.info("Backup: %s", msg)
    except Exception as exc:  # noqa: BLE001
        store.set_setting("backup_last", f"{time.time():.0f}|fehler|{exc}")
        log.exception("Backup fehlgeschlagen")
