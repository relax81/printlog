"""Bilder für den Demo-Modus: Vorschaubilder wie aus Bambu Studio und „Handyfotos“.

Kein 3D-Programm nötig: Formen werden als Grundriss (Polygon) beschrieben, in die
Höhe gezogen und isometrisch mit Pillow gezeichnet. Das reicht für Kacheln in der
Liste, die Galerie und die Detailseite.
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageChops, ImageDraw, ImageFilter

Point = tuple[float, float]
# Teil eines Modells: Grundriss, Unterkante, Oberkante, Farbe (Hex), Loch (nur Deckfläche, dunkel)
Part = tuple[list[Point], float, float, str, bool]

COS30 = math.cos(math.radians(30))
SIN30 = 0.5


# ---- Grundrisse -------------------------------------------------------------

def circle(cx: float, cy: float, r: float, n: int = 48, ry: float | None = None) -> list[Point]:
    ry = r if ry is None else ry
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]


def rrect(x0: float, y0: float, x1: float, y1: float, r: float = 3) -> list[Point]:
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    pts: list[Point] = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for k in range(7):
            a = math.radians(a0 + k * 15)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def star(cx: float, cy: float, r1: float, r2: float, n: int = 5) -> list[Point]:
    return [(cx + (r1 if i % 2 == 0 else r2) * math.cos(math.pi * i / n - math.pi / 2),
             cy + (r1 if i % 2 == 0 else r2) * math.sin(math.pi * i / n - math.pi / 2)) for i in range(2 * n)]


def gear(cx: float, cy: float, r: float, teeth: int = 14) -> list[Point]:
    pts: list[Point] = []
    for i in range(teeth):
        a = 2 * math.pi * i / teeth
        step = 2 * math.pi / teeth
        for frac, rad in ((0.0, r * 0.84), (0.18, r), (0.5, r), (0.68, r * 0.84)):
            pts.append((cx + rad * math.cos(a + frac * step), cy + rad * math.sin(a + frac * step)))
    return pts


# ---- Zeichnen ---------------------------------------------------------------

def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _shade(rgb: tuple[int, int, int], f: float, lift: int = 0) -> tuple[int, int, int, int]:
    return tuple(max(0, min(255, int(c * f + lift))) for c in rgb) + (255,)  # type: ignore[return-value]


def _iso(p: Point, z: float, s: float, ox: float, oy: float) -> Point:
    x, y = p
    return ox + (x - y) * COS30 * s, oy + (x + y) * SIN30 * s - z * s


def _signed_area(poly: list[Point]) -> float:
    return sum(poly[i - 1][0] * poly[i][1] - poly[i][0] * poly[i - 1][1] for i in range(len(poly))) / 2


def render(parts: list[Part], size: int = 512, margin: float = 0.08) -> Image.Image:
    """Zeichnet die Teile isometrisch auf transparenten Grund, eingepasst in ``size`` × ``size``."""
    ss = 2  # doppelt so groß zeichnen und verkleinern = glatte Kanten
    pts = [_iso(p, z, 1.0, 0, 0) for poly, z0, z1, *_ in parts for p in poly for z in (z0, z1)]
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    inner = size * ss * (1 - 2 * margin)
    s = inner / max(x1 - x0, y1 - y0, 1e-6)
    canvas = size * ss
    ox = canvas / 2 - (x0 + x1) / 2 * s
    oy = canvas / 2 - (y0 + y1) / 2 * s
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Licht von links oben: Seiten nach +x (rechts vorne) dunkler als nach +y (links vorne).
    light = (-0.35, 0.9)
    # Hinten vor vorne, unten vor oben; bei Gleichstand (Loch im selben Teil) die Reihenfolge der Liste.
    order = sorted(range(len(parts)), key=lambda i: (parts[i][1], round(sum(x + y for x, y in parts[i][0]) / len(parts[i][0]), 1), i))
    for poly, z0, z1, color, hole in (parts[i] for i in order):
        rgb = _rgb(color)
        if hole:
            d.polygon([_iso(p, z1, s, ox, oy) for p in poly], fill=_shade(rgb, 0.28))
            continue
        if _signed_area(poly) < 0:
            poly = poly[::-1]
        if z1 > z0:
            edges = []
            for i in range(len(poly)):
                a, b = poly[i], poly[(i + 1) % len(poly)]
                nx, ny = b[1] - a[1], -(b[0] - a[0])
                ln = math.hypot(nx, ny) or 1
                nx, ny = nx / ln, ny / ln
                if nx + ny <= 0:  # zeigt nach hinten
                    continue
                depth = (a[0] + b[0] + a[1] + b[1]) / 2
                edges.append((depth, a, b, nx, ny))
            for _, a, b, nx, ny in sorted(edges):
                lam = 0.55 + 0.33 * max(-1.0, min(1.0, nx * light[0] + ny * light[1]))
                quad = [_iso(a, z0, s, ox, oy), _iso(b, z0, s, ox, oy), _iso(b, z1, s, ox, oy), _iso(a, z1, s, ox, oy)]
                d.polygon(quad, fill=_shade(rgb, lam))
        d.polygon([_iso(p, z1, s, ox, oy) for p in poly], fill=_shade(rgb, 1.0, 22),
                  outline=_shade(rgb, 0.92, 10))
    return img.reduce(ss)


# ---- Modelle ----------------------------------------------------------------

def _move(poly: Iterable[Point], dx: float, dy: float, k: float = 1.0) -> list[Point]:
    return [(x * k + dx, y * k + dy) for x, y in poly]


def model(shape: str, colors: list[str], rnd: random.Random) -> list[Part]:
    """Teile eines Modells um den Ursprung, etwa 60 Einheiten groß."""
    c1 = colors[0]
    c2 = colors[1] if len(colors) > 1 else c1
    c3 = colors[2] if len(colors) > 2 else c2
    if shape == "box":  # Kiste/Organizer: Wanne mit Innenraum
        w, dpt, h = rnd.choice([(60, 40, 30), (50, 50, 26), (70, 36, 22)])
        parts: list[Part] = [(rrect(-w / 2, -dpt / 2, w / 2, dpt / 2, 5), 0, h, c1, False),
                             (rrect(-w / 2 + 3, -dpt / 2 + 3, w / 2 - 3, dpt / 2 - 3, 3), 0, h, c1, True)]
        if c2 != c1:
            parts.append((rrect(-w / 2 + 6, dpt / 2 - 1, w / 2 - 6, dpt / 2 + 0.5, 0.5), h * 0.3, h * 0.7, c2, False))
        return parts
    if shape == "stand":  # Handy-/Tabletständer: Fuß + schräge Lehne
        return [(rrect(-30, -22, 30, 22, 4), 0, 6, c1, False),
                (rrect(-30, -6, 30, 6, 2), 6, 44, c1, False),
                (rrect(-30, 12, 30, 20, 2), 6, 12, c2, False)]
    if shape in ("cyl", "vase", "pot", "lamp"):
        r, h = {"cyl": (16, 14), "vase": (20, 70), "pot": (28, 42), "lamp": (30, 64)}[shape]
        n = 10 if shape == "vase" else 48
        parts = [(circle(0, 0, r, n if shape == "vase" else 48), 0, h, c1, False)]
        if shape in ("vase", "pot", "lamp"):
            parts.append((circle(0, 0, r - 2.5, n if shape == "vase" else 48), 0, h, c1, True))
        if shape == "pot":
            parts.insert(0, (circle(0, 0, r + 6), 0, 5, c2, False))
        if shape == "cyl" and c2 != c1:
            parts.append((circle(0, 0, r * 0.45, 24), h, h + 4, c2, False))
        return parts
    if shape == "plate":  # Schild, Untersetzer, Anhänger: flach mit erhabener Schrift
        w, dpt = rnd.choice([(70, 46), (60, 60), (80, 30)])
        parts = [(rrect(-w / 2, -dpt / 2, w / 2, dpt / 2, 6), 0, 4, c1, False)]
        for i, lw in enumerate((0.7, 0.5, 0.6)):
            y = -dpt / 2 + 10 + i * (dpt - 18) / 3
            parts.append((rrect(-w * lw / 2, y, w * lw / 2, y + 5, 1.5), 4, 6, c2, False))
        parts.append((circle(-w / 2 + 7, -dpt / 2 + 7, 3, 20), 0, 4, c1, True))
        return parts
    if shape == "coaster":
        return [(circle(0, 0, 30), 0, 4, c1, False), (circle(0, 0, 24), 4, 5, c2, False),
                (star(0, 0, 14, 6, 6), 5, 6, c3, False)]
    if shape == "tag":  # Schlüsselanhänger
        return [(rrect(-30, -10, 30, 10, 9), 0, 3, c1, False), (circle(-23, 0, 3, 20), 0, 3, c1, True),
                (rrect(-14, -4, 24, 4, 1.5), 3, 5, c2, False)]
    if shape == "gear":
        r = 30
        parts = [(gear(0, 0, r, 16), 0, 10, c1, False), (circle(0, 0, 7, 24), 0, 10, c1, True)]
        parts.append((gear(36, 26, 16, 10), 0, 10, c2, False))
        parts.append((circle(36, 26, 4, 20), 0, 10, c2, True))
        return parts
    if shape == "hook":  # Haken/Halter: L-Profil liegend
        poly = [(-30, -6), (20, -6), (20, -24), (30, -24), (30, 6), (-30, 6)]
        return [(poly, 0, 18, c1, False), (circle(-18, 0, 3, 20), 0, 18, c1, True)]
    if shape == "star":
        return [(star(0, 0, 32, 14), 0, 6, c1, False), (star(0, 0, 18, 8), 6, 8, c2, False)]
    if shape == "cutter":  # Ausstechform: dünne Wand
        return [(star(0, 0, 32, 15), 0, 16, c1, False), (star(0, 0, 29.5, 13.2), 0, 16, c1, True)]
    if shape == "frame":
        return [(rrect(-34, -26, 34, 26, 3), 0, 8, c1, False), (rrect(-24, -16, 24, 16, 1), 0, 8, c1, True)]
    if shape == "boat":  # Testboot
        hull = [(x, y * 0.5) for x, y in circle(0, 0, 30, 40)]
        return [(hull, 0, 14, c1, False), (rrect(-4, -9, 16, 9, 2), 14, 30, c1, False),
                (rrect(-1, -6, 13, 6, 1), 30, 30, c1, True), (circle(-14, 0, 4, 20), 14, 26, c1, False),
                (circle(-14, 0, 2.6, 20), 26, 26, c1, True)]
    if shape == "dragon":  # Gelenkdrache: Segmente entlang einer Kurve
        parts = []
        for i in range(11):
            x = -50 + i * 9
            y = 10 * math.sin(i / 1.8)
            r = 9 - abs(i - 3) * 0.55
            col = c1 if i % 2 == 0 else c2
            parts.append((circle(x, y, max(3.5, r), 6), 0, 8 + max(0, 4 - abs(i - 3)), col, False))
        parts.append((circle(-58, 6, 10, 7), 0, 14, c1, False))
        return parts
    if shape == "cube":
        return [(rrect(-10, -10, 10, 10, 0.6), 0, 20, c1, False),
                (rrect(-5, -5, 5, 5, 0.4), 20, 20.6, c2 if c2 != c1 else c1, True)]
    return [(rrect(-30, -20, 30, 20, 4), 0, 20, c1, False)]


def plate(shape: str, colors: list[str], qty: int, seed: int) -> list[Part]:
    """Mehrere Stück auf der Platte im Raster (max. 9 gezeichnet)."""
    rnd = random.Random(seed)
    one = model(shape, colors, rnd)
    n = max(1, min(qty, 9))
    cols = math.ceil(math.sqrt(n))
    xs = [x for poly, *_ in one for x, _ in poly]
    ys = [y for poly, *_ in one for _, y in poly]
    w, h = (max(xs) - min(xs)) * 1.15, (max(ys) - min(ys)) * 1.15
    parts: list[Part] = []
    for i in range(n):
        cx, cy = (i % cols) * w, (i // cols) * h
        for poly, z0, z1, col, hole in one:
            parts.append((_move(poly, cx, cy), z0, z1, col, hole))
    return parts


def cover(path: Path, shape: str, colors: list[str], qty: int, seed: int) -> None:
    render(plate(shape, colors, qty, seed), 512).save(path, compress_level=4)


# ---- Fotos ------------------------------------------------------------------

BACKGROUNDS = [
    ((54, 112, 84), (36, 82, 62), "mat"),      # grüne Schneidematte mit Raster
    ((176, 132, 92), (128, 88, 58), "wood"),   # Holztisch
    ((214, 212, 206), (168, 166, 160), "plain"),  # heller Tisch
    ((58, 60, 66), (32, 33, 37), "plain"),     # dunkle Druckplatte
]


_NOISE: dict[tuple[int, int], Image.Image] = {}


def _noise(w: int, h: int) -> Image.Image:
    """Sensorrauschen, einmal erzeugt und wiederverwendet (effect_noise ist langsam)."""
    if (w, h) not in _NOISE:
        _NOISE[(w, h)] = Image.effect_noise((w, h), 40).convert("RGB")
    return _NOISE[(w, h)]


def photo(path: Path, shape: str, colors: list[str], qty: int, seed: int) -> None:
    """Ein „Foto“ vom fertigen Druck: Objekt mit Schatten auf Tisch, Matte oder Platte."""
    rnd = random.Random(seed)
    W, H = 1600, 1200
    top, bottom, kind = BACKGROUNDS[seed % len(BACKGROUNDS)]
    bg = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(bg)
    for y in range(H):
        f = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] * (1 - f) + bottom[i] * f) for i in range(3)))
    if kind == "mat":
        for x in range(-40, W + 40, 64):
            d.line([(x, 0), (x + 120, H)], fill=(84, 146, 112), width=2)
        for y in range(0, H, 64):
            d.line([(0, y), (W, y + 30)], fill=(84, 146, 112), width=2)
    elif kind == "wood":
        for _ in range(70):
            y = rnd.randint(0, H)
            d.line([(0, y), (W, y + rnd.randint(-40, 40))], fill=tuple(c - rnd.randint(8, 26) for c in top), width=rnd.randint(1, 4))
    noise = _noise(W, H)
    bg = Image.blend(bg, ImageChops.add(bg, noise, scale=1.0, offset=-128), 0.5)

    obj = render(plate(shape, colors, min(qty, 4), seed), 980, margin=0.02)
    ox, oy = (W - obj.width) // 2 + rnd.randint(-120, 120), (H - obj.height) // 2 + rnd.randint(-40, 60)
    small = Image.new("L", (W // 4, H // 4), 0)
    alpha = obj.getchannel("A").point(lambda a: 150 if a > 10 else 0)
    small.paste(alpha.resize((obj.width // 4, obj.height // 4)), ((ox + 26) // 4, (oy + 34) // 4))
    shadow = small.filter(ImageFilter.GaussianBlur(7)).resize((W, H), Image.Resampling.BILINEAR)
    bg = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), bg, shadow)
    bg.paste(obj, (ox, oy), obj)

    # Vignette und leichte Unschärfe wie bei einer Handykamera
    vign = Image.new("L", (W // 8, H // 8), 0)
    ImageDraw.Draw(vign).ellipse([-W * 0.25 / 8, -H * 0.3 / 8, W * 1.25 / 8, H * 1.3 / 8], fill=255)
    vign = vign.filter(ImageFilter.GaussianBlur(20)).resize((W, H), Image.Resampling.BILINEAR)
    bg = Image.composite(bg, Image.new("RGB", (W, H), (0, 0, 0)), vign.point(lambda v: 150 + v * 105 // 255))
    bg = bg.filter(ImageFilter.GaussianBlur(0.6))
    bg.save(path, "JPEG", quality=82, progressive=True)
