"""Fotos verkleinern und Projekttitel aus einem Link holen."""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path

import aiohttp

log = logging.getLogger("printlog.media")

MAX_EDGE = 2560        # längste Kante in Pixeln, reicht für Vollbild am Monitor
JPEG_QUALITY = 85


def shrink_photo(path: Path) -> Path:
    """Dreht das Foto nach EXIF, verkleinert es und speichert es als JPEG.

    Gibt den neuen Pfad zurück. Bei Fehlern bleibt das Original unverändert.
    """
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            if max(img.size) > MAX_EDGE:
                img.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            if has_alpha:
                target = path.with_suffix(".png")
                img.save(target, optimize=True)
            else:
                target = path.with_suffix(".jpg")
                img.convert("RGB").save(target, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
        if target != path:
            path.unlink(missing_ok=True)
        return target
    except Exception:  # noqa: BLE001
        log.warning("Foto %s konnte nicht verkleinert werden, Original bleibt", path.name, exc_info=True)
        return path


LOGO_MAX_EDGE = 1200   # reicht für Bon (max. 600 px breit) und PDF


def save_logo(data: bytes, image_dir: Path, knockout_white: bool = False) -> str | None:
    """Eigenes Bon-Logo als PNG ablegen (gedreht nach EXIF, verkleinert).

    ``knockout_white`` macht fast weiße Pixel durchsichtig, damit ein Logo mit weißem
    Hintergrund auf dem cremefarbenen Bonpapier nicht als Kasten erscheint.
    Gibt den Dateinamen zurück, bei unlesbaren Dateien None.
    """
    import io
    import time

    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as src:
            img = ImageOps.exif_transpose(src).convert("RGBA")
    except Exception:  # noqa: BLE001
        log.warning("Logo konnte nicht gelesen werden", exc_info=True)
        return None
    img.thumbnail((LOGO_MAX_EDGE, LOGO_MAX_EDGE), Image.Resampling.LANCZOS)
    if knockout_white:
        px = [(r, g, b, 0 if min(r, g, b) >= 238 else a) for r, g, b, a in img.getdata()]
        img.putdata(px)
    bbox = img.getchannel("A").getbbox()  # durchsichtigen Rand abschneiden
    if bbox:
        img = img.crop(bbox)
    name = f"bon_logo_{int(time.time())}.png"
    img.save(image_dir / name, optimize=True)
    return name


_TITLE_PATTERNS = [
    re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title', re.I),
    re.compile(r"<title[^>]*>([^<]+)</title>", re.I),
]
_SUFFIX = re.compile(r"\s*[-|–]\s*(MakerWorld|Printables(\.com)?|Thingiverse|Cults)\b.*$", re.I)


async def fetch_title(session: aiohttp.ClientSession | None, url: str) -> str | None:
    """Liest den Seitentitel. MakerWorld blockt Bots manchmal, dann bleibt es beim Link."""
    if not session or not url.startswith(("http://", "https://")):
        return None
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=8),
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/126.0 Safari/537.36", "Accept-Language": "de,en;q=0.8"},
        ) as resp:
            if resp.status != 200:
                return None
            text = (await resp.content.read(400_000)).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    for pat in _TITLE_PATTERNS:
        m = pat.search(text)
        if m:
            title = _SUFFIX.sub("", html.unescape(m.group(1))).strip()
            if title and title.lower() not in ("makerworld", "just a moment...", "attention required!"):
                return title[:200]
    return None


def title_from_url(url: str) -> str | None:
    """Fallback: aus dem Pfad einen lesbaren Titel machen (z. B. /models/123-cable-holder)."""
    m = re.search(r"/models?/(?:\d+-)?([a-z0-9-]{3,})", url, re.I)
    if not m or m.group(1).isdigit():
        return None
    return m.group(1).replace("-", " ").strip().capitalize()


def clean_local_path(raw: str) -> str | None:
    """Windows-Pfad aufräumen: Anführungszeichen von „Als Pfad kopieren“ weg, file:///-Links umwandeln."""
    from urllib.parse import unquote, urlsplit

    p = raw.strip().strip('"').strip()
    if p.lower().startswith("file:"):
        parts = urlsplit(p)
        p = unquote(parts.path)
        if parts.netloc and parts.netloc.lower() != "localhost":
            p = f"//{parts.netloc}{p}"  # file://server/freigabe -> \\server\freigabe
        elif re.match(r"^/[A-Za-z]:", p):
            p = p[1:]
        p = p.replace("/", "\\")
    if len(p) > 3:
        p = p.rstrip("\\/")
    return p[:500] or None
