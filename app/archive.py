"""Komplettsicherung als ZIP: herunterladen, wieder einspielen, zurücksetzen.

Ein ZIP enthält

* ``printlog.db``       die SQLite-Datenbank (Drucke, Kunden, Zahlungen, Papierkorb und
                        alle Einstellungen inkl. Drucker-Zuordnung und Preise)
* ``images/…``          Cover-Bilder, eigene Fotos und das eigene Bon-Logo
* ``manifest.json``     Erstellzeit, Anzahl Drucke/Bilder (für die Vorschau)
* ``einstellungen.json`` die Einstellungen noch einmal lesbar (wird beim Einspielen nicht genutzt)

Beim Einspielen wird das ZIP streng geprüft (nur diese Einträge, keine Pfade außerhalb
des Datenordners, keine Links, Größenlimits, gültige SQLite-Datei ohne Trigger) und erst
nach einer Vorschau mit Bestätigung übernommen. Vorher legt printlog automatisch eine
Sicherung des aktuellen Stands unter ``<DATA_DIR>/sicherungen`` an.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import stat
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import Store

log = logging.getLogger("printlog.archive")

DB_NAME = "printlog.db"
MANIFEST = "manifest.json"
SETTINGS_JSON = "einstellungen.json"
FORMAT = 1

# Limits gegen versehentlich riesige oder präparierte ZIPs (Zip-Bombe).
MAX_UPLOAD = int(os.environ.get("IMPORT_MAX_MB", "4096")) * 1024**2
MAX_UNPACKED = 3 * MAX_UPLOAD
MAX_ENTRIES = 50_000
MAX_RATIO = 200          # Kompressionsrate pro Datei (Fotos sind kaum komprimierbar)
KEEP_SAFETY = int(os.environ.get("SAFETY_KEEP", "5"))
STAGE_TTL = 3600         # hochgeladene, nicht eingespielte ZIPs nach 1 h wegräumen

IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,150}")
SAFETY_RE = re.compile(r"printlog-[a-z-]+-\d{4}-\d{2}-\d{2}_\d{6}\.zip")
TOKEN_RE = re.compile(r"[a-f0-9]{32}")
IGNORED = ("__MACOSX/", ".DS_Store", "Thumbs.db")


class ArchiveError(Exception):
    """Fehler mit deutschem Text als i18n-Schlüssel (``key``) und Werten."""

    def __init__(self, key: str, **values: Any) -> None:
        super().__init__(key.format(**values) if values else key)
        self.key = key
        self.values = values


# ---- Ordner ---------------------------------------------------------------


def safety_dir(data_dir: Path) -> Path:
    d = data_dir / "sicherungen"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage_dir(data_dir: Path) -> Path:
    d = data_dir / "import"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_stage(data_dir: Path) -> None:
    now = time.time()
    for p in stage_dir(data_dir).iterdir():
        try:
            if now - p.stat().st_mtime > STAGE_TTL:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            log.warning("Konnte %s nicht aufräumen", p, exc_info=True)


def safety_files(data_dir: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(safety_dir(data_dir).glob("printlog-*.zip"), reverse=True):
        if SAFETY_RE.fullmatch(p.name):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "ts": st.st_mtime})
    return out


def safety_path(data_dir: Path, name: str) -> Path | None:
    if not SAFETY_RE.fullmatch(name or ""):
        return None
    p = safety_dir(data_dir) / name
    return p if p.is_file() else None


# ---- Erstellen ------------------------------------------------------------


def _settings_dump(store: Store) -> dict[str, Any]:
    cfg = store.settings()
    printers = cfg.get("printers")
    try:
        printers_val: Any = json.loads(printers) if printers else []
    except ValueError:
        printers_val = printers
    return {
        "settings": {k: v for k, v in cfg.items() if k != "printers"},
        "printers": printers_val,
        "material_prices": store.material_prices(),
        "printer_rates": store.printer_rates(),
    }


def build_zip(store: Store, image_dir: Path, target: Path) -> dict[str, Any]:
    """Schreibt eine Komplettsicherung nach ``target`` (atomar über eine .tmp-Datei)."""
    tmp_db = target.with_name(target.name + ".db.tmp")
    tmp_zip = target.with_name(target.name + ".tmp")
    try:
        store.backup_to(tmp_db)
        counts = store.counts()
        images = sorted(p for p in image_dir.iterdir() if p.is_file() and IMAGE_RE.fullmatch(p.name)) \
            if image_dir.is_dir() else []
        manifest = {
            "app": "printlog",
            "format": FORMAT,
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "jobs": counts["jobs"],
            "customers": counts["customers"],
            "images": len(images),
        }
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
            z.write(tmp_db, DB_NAME)
            try:
                z.writestr(SETTINGS_JSON, json.dumps(_settings_dump(store), ensure_ascii=False, indent=2, default=str))
            except Exception:  # noqa: BLE001 – nur zum Lesen, darf die Sicherung nicht verhindern
                log.warning("einstellungen.json übersprungen", exc_info=True)
            for p in images:
                # Fotos sind schon komprimiert (JPEG/PNG), nur speichern spart Zeit.
                z.write(p, f"images/{p.name}", compress_type=zipfile.ZIP_STORED)
        tmp_zip.replace(target)
        return manifest
    finally:
        tmp_db.unlink(missing_ok=True)
        tmp_zip.unlink(missing_ok=True)


def safety_backup(store: Store, image_dir: Path, data_dir: Path, reason: str) -> Path:
    """Automatische Sicherung vor Einspielen/Zurücksetzen; behält die letzten ``KEEP_SAFETY``."""
    d = safety_dir(data_dir)
    target = d / f"printlog-{reason}-{datetime.now():%Y-%m-%d_%H%M%S}.zip"
    build_zip(store, image_dir, target)
    for old in [p for p in sorted(d.glob("printlog-*.zip"), key=lambda p: p.stat().st_mtime)
                if SAFETY_RE.fullmatch(p.name)][:-KEEP_SAFETY]:
        old.unlink(missing_ok=True)
    return target


# ---- Prüfen und Auspacken -------------------------------------------------


def _entry_kind(name: str) -> tuple[str, str] | None:
    """("db"|"manifest"|"settings"|"image", Dateiname) oder None = ignorieren. Wirft bei Unerlaubtem."""
    if name.startswith(IGNORED[0]) or name.rsplit("/", 1)[-1] in IGNORED[1:]:
        return None
    # Manche Packer legen alles in einen Oberordner („printlog-2026-…/printlog.db“).
    if "\\" in name or name.startswith("/") or re.match(r"[A-Za-z]:", name):
        raise ArchiveError("Unerlaubter Pfad im ZIP: {name}", name=name[:80])
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts[:-1]) or parts[-1] in (".", ".."):
        raise ArchiveError("Unerlaubter Pfad im ZIP: {name}", name=name[:80])
    if name.endswith("/"):
        return None  # Ordner-Eintrag
    if len(parts) >= 2 and parts[-2] == "images" and len(parts) <= 3:
        if not IMAGE_RE.fullmatch(parts[-1]):
            raise ArchiveError("Unerlaubter Dateiname im ZIP: {name}", name=name[:80])
        return "image", parts[-1]
    if len(parts) <= 2 and parts[-1] == DB_NAME:
        return "db", DB_NAME
    if len(parts) <= 2 and parts[-1] == MANIFEST:
        return "manifest", MANIFEST
    if len(parts) <= 2 and parts[-1] == SETTINGS_JSON:
        return "settings", SETTINGS_JSON
    raise ArchiveError("Unerwartete Datei im ZIP: {name}", name=name[:80])


def _copy_limited(src: Any, dst: Path, limit: int) -> int:
    n = 0
    with open(dst, "xb") as out:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            n += len(chunk)
            if n > limit:
                raise ArchiveError("Das ZIP ist entpackt zu groß.")
            out.write(chunk)
    return n


def check_db(path: Path) -> dict[str, Any]:
    """Prüft, dass ``path`` eine printlog-Datenbank ist, und liefert Kennzahlen."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ArchiveError("Die Datenbank im ZIP ist beschädigt.") from exc
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ArchiveError("Die Datenbank im ZIP ist beschädigt.")
        objs = {(r[0], r[1]) for r in conn.execute("SELECT type, name FROM sqlite_master")}
        tables = {n for ty, n in objs if ty == "table"}
        if not {"jobs", "settings", "customers"} <= tables:
            raise ArchiveError("Das ZIP enthält keine printlog-Datenbank.")
        if any(ty in ("trigger", "view") for ty, _ in objs):
            raise ArchiveError("Die Datenbank im ZIP enthält unerwartete Trigger oder Views.")
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        info = {
            "jobs": one("SELECT COUNT(*) FROM jobs"),
            "customers": one("SELECT COUNT(*) FROM customers"),
            "photos": one("SELECT COUNT(*) FROM photos") if "photos" in tables else 0,
            "trash": one("SELECT COUNT(*) FROM trash") if "trash" in tables else 0,
            "payments": one("SELECT COUNT(*) FROM payments") if "payments" in tables else 0,
            "first_ts": one("SELECT MIN(start_ts) FROM jobs"),
            "last_ts": one("SELECT MAX(start_ts) FROM jobs"),
        }
        refs = {r[0] for r in conn.execute("SELECT image FROM jobs WHERE image IS NOT NULL")}
        if "photos" in tables:
            refs |= {r[0] for r in conn.execute("SELECT file FROM photos")}
        logo = conn.execute("SELECT value FROM settings WHERE key = 'bon_logo'").fetchone()
        if logo and logo[0]:
            refs.add(logo[0])
        info["refs"] = refs
        return info
    except sqlite3.Error as exc:
        raise ArchiveError("Die Datenbank im ZIP ist beschädigt.") from exc
    finally:
        conn.close()


def unpack(zip_path: Path, dest: Path) -> dict[str, Any]:
    """Prüft das ZIP und packt es nach ``dest`` aus (``printlog.db``, ``images/``).

    Pfade werden nie aus dem ZIP übernommen, sondern aus geprüften Dateinamen neu gebaut.
    """
    try:
        z = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError("Die Datei ist kein gültiges ZIP.") from exc
    with z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ArchiveError("Das ZIP enthält zu viele Dateien.")
        plan: list[tuple[zipfile.ZipInfo, str, str]] = []
        seen: set[str] = set()
        total = 0
        for info in infos:
            kind = _entry_kind(info.filename)
            if kind is None:
                continue
            mode = info.external_attr >> 16
            if mode and (stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_IFMT(mode) == 0)):
                raise ArchiveError("Das ZIP enthält Links oder Sonderdateien: {name}", name=info.filename[:80])
            if info.flag_bits & 0x1:
                raise ArchiveError("Verschlüsselte ZIPs werden nicht unterstützt.")
            key = f"{kind[0]}:{kind[1]}"
            if key in seen:
                raise ArchiveError("Datei doppelt im ZIP: {name}", name=info.filename[:80])
            seen.add(key)
            if info.compress_size and info.file_size / info.compress_size > MAX_RATIO and info.file_size > 10 * 1024**2:
                raise ArchiveError("Das ZIP ist entpackt zu groß.")
            total += info.file_size
            plan.append((info, *kind))
        if total > MAX_UNPACKED:
            raise ArchiveError("Das ZIP ist entpackt zu groß.")
        if "db:printlog.db" not in seen:
            raise ArchiveError("Im ZIP fehlt die Datenbank printlog.db.")

        dest.mkdir(parents=True, exist_ok=False)
        img_dest = dest / "images"
        img_dest.mkdir()
        manifest: dict[str, Any] = {}
        budget = MAX_UNPACKED
        for info, kind, name in plan:
            if kind == "settings":
                continue
            if kind == "manifest":
                try:
                    manifest = json.loads(z.read(info)[:100_000].decode("utf-8"))
                    if not isinstance(manifest, dict):
                        manifest = {}
                except (ValueError, UnicodeDecodeError):
                    manifest = {}
                continue
            target = (img_dest if kind == "image" else dest) / name
            if target.resolve().parent != (img_dest if kind == "image" else dest).resolve():
                raise ArchiveError("Unerlaubter Pfad im ZIP: {name}", name=info.filename[:80])
            try:
                with z.open(info) as src:
                    budget -= _copy_limited(src, target, min(budget, info.file_size))
            except (zipfile.BadZipFile, OSError, EOFError) as exc:
                if isinstance(exc, FileExistsError):
                    raise ArchiveError("Datei doppelt im ZIP: {name}", name=info.filename[:80]) from exc
                raise ArchiveError("Das ZIP ist beschädigt ({name}).", name=info.filename[:80]) from exc

    info_db = check_db(dest / DB_NAME)
    images = {p.name for p in img_dest.iterdir()}
    refs = info_db.pop("refs")
    return {
        **info_db,
        "images": len(images),
        "missing": sorted(refs - images),
        "created": str(manifest.get("created") or "")[:40],
        "format": manifest.get("format"),
        "size": zip_path.stat().st_size,
    }


def new_token() -> str:
    return secrets.token_hex(16)


def staged(data_dir: Path, token: str) -> Path | None:
    if not TOKEN_RE.fullmatch(token or ""):
        return None
    p = stage_dir(data_dir) / token
    return p if (p / DB_NAME).is_file() else None


# ---- Übernehmen -----------------------------------------------------------


def _replace_images(image_dir: Path, new_dir: Path) -> None:
    """Bilderordner durch ``new_dir`` ersetzen. Der Ordner selbst bleibt (kann ein Mount sein)."""
    image_dir.mkdir(parents=True, exist_ok=True)
    for p in image_dir.iterdir():
        if p.is_file() or p.is_symlink():
            p.unlink()
    for p in new_dir.iterdir():
        shutil.move(str(p), image_dir / p.name)


def apply(store: Store, image_dir: Path, data_dir: Path, token: str) -> dict[str, Any]:
    """Spielt ein geprüftes, ausgepacktes ZIP ein. Legt vorher eine Sicherung an."""
    src = staged(data_dir, token)
    if src is None:
        raise ArchiveError("Die hochgeladene Sicherung ist abgelaufen. Bitte noch einmal hochladen.")
    check_db(src / DB_NAME)  # nochmal, falls zwischendurch etwas passiert ist
    safety = safety_backup(store, image_dir, data_dir, "vor-import")
    store.restore_from(src / DB_NAME)
    _replace_images(image_dir, src / "images")
    shutil.rmtree(src, ignore_errors=True)
    log.info("Sicherung eingespielt, vorheriger Stand in %s", safety.name)
    return {"safety": safety.name, **store.counts()}


def reset(store: Store, image_dir: Path, data_dir: Path, everything: bool) -> str:
    """Zurücksetzen nach automatischer Sicherung; gibt den Namen der Sicherung zurück."""
    safety = safety_backup(store, image_dir, data_dir, "vor-reset")
    keep = set() if everything else {store.settings().get("bon_logo")} - {None}
    store.reset(everything)
    if image_dir.is_dir():
        for p in image_dir.iterdir():
            if p.is_file() and p.name not in keep:
                p.unlink()
    log.info("Zurückgesetzt (%s), vorheriger Stand in %s", "alles" if everything else "Drucke", safety.name)
    return safety.name
