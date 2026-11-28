"""Screenshot lesen (Texterkennung mit Tesseract) für „Kosten vorab kalkulieren“.

Zwei Vorlagen werden erkannt:

* **Bambu Studio, „Slicing Result“** (Vorschau nach dem Slicen): pro Filament „6.44g / 2.12m“, die Zeile „Total“, die
  Gesamtzeit („Total time: 3h40m“) und der Drucker oben links („Bambu Lab H3C“). Die Farbe jeder Filamentzeile kommt aus
  dem farbigen Kästchen links davon (Pixel im Bild), der Typ aus der Liste „Project Filaments“, falls sie mit drauf ist.
  Weicht die Summe der Zeilen von „Total“ ab, wird eine Zeile mit fehlendem Komma (741 statt 7.41) korrigiert.
* **MakerWorld-Modellseite** (Kasten mit Profil): „1 Platte · 1.7 h · 11 g“, Filament-Chips „PLA | 7 g“ und die Liste
  der kompatiblen Drucker. Die Zeit wird gleichmäßig auf die Platten verteilt; was die Chips nicht abdecken, kommt als
  eigene Zeile dazu.

Das Bild wird vergrößert und in beiden Helligkeiten gelesen (dunkle Oberfläche invertiert). Alle Werte bleiben im
Rechner änderbar, die Texterkennung ist eine Hilfe zum Abtippen.
"""

from __future__ import annotations

import io
import logging
import re
from itertools import product
import shutil
import subprocess
from typing import Any

from PIL import Image, ImageOps

from .costing import KNOWN_TYPES
from .i18n import t

log = logging.getLogger("printlog.ocr")

MAX_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 40_000_000
_TYPES = "|".join(sorted(KNOWN_TYPES, key=len, reverse=True))
_NUM = r"\d+(?:[.,]\d+)?"
# „6.44g / 2.12m“; Bambu zeigt Gramm immer mit zwei Nachkommastellen, ein „g“ wird manchmal als „9“ gelesen
_BAMBU_ROW = re.compile(r"(\d+[.,]\d{2}|\d+)\s*[g9]\s*[/|\\lI1!]?\s*\d+(?:[.,]\d+)?\s*m\b", re.I)
_DUR = re.compile(r"(?:(\d+)\s*d\s*)?(?:(\d+)\s*h\s*)?(?:(\d+)\s*m(?!m)\s*)?(?:(\d+)\s*s)?", re.I)
_CHIP = re.compile(rf"\b({_TYPES})(?:[-\s](?:CF|HF|GF|Basic|Matte|Silk\+?|Tough|Lite))?[\s|I!l]*?({_NUM})\s*[g¢9]?(?:\b|$)", re.I)


def available() -> bool:
    return shutil.which("tesseract") is not None


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def duration(text: str) -> float | None:
    """„3h40m“, „17m0s“, „1d2h“ → Minuten."""
    for m in _DUR.finditer(text or ""):
        if any(m.groups()):
            d, h, mi, s = (int(x) if x else 0 for x in m.groups())
            total = d * 1440 + h * 60 + mi + s / 60
            if total > 0:
                return round(total, 1)
    return None


# ---- Texterkennung ---------------------------------------------------------------

_LANGS: list[str] = []


def _lang() -> str:
    """„deu+eng“, wenn das deutsche Sprachpaket da ist (liest auch Zahlen wie „1.7h“ zuverlässiger), sonst „eng“."""
    if not _LANGS:
        try:
            out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, timeout=20, check=False).stdout.decode()
        except (OSError, subprocess.SubprocessError):
            out = ""
        _LANGS.append("deu+eng" if re.search(r"^deu$", out, re.M) else "eng")
    return _LANGS[0]


def _tsv(img: Image.Image, psm: int = 11) -> list[dict[str, Any]]:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    try:
        out = subprocess.run(["tesseract", "stdin", "stdout", "-l", _lang(), "--psm", str(psm), "tsv"],
                             input=buf.getvalue(), capture_output=True, timeout=60, check=False).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as err:
        log.warning("Tesseract fehlgeschlagen: %s", err)
        return []
    words = []
    for row in out.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        try:
            words.append({"x": int(c[6]), "y": int(c[7]), "w": int(c[8]), "h": int(c[9]), "text": c[11].strip()})
        except ValueError:
            continue
    return words


def lines(words: list[dict[str, Any]], scale: float = 1.0) -> list[dict[str, Any]]:
    """Wörter zu Zeilen (nach Höhe gruppiert, links nach rechts). Koordinaten zurück auf das Originalbild."""
    ws = sorted(({**w, "x": w["x"] / scale, "y": w["y"] / scale, "w": w["w"] / scale, "h": w["h"] / scale} for w in words),
                key=lambda w: w["y"] + w["h"] / 2)
    out: list[dict[str, Any]] = []
    for w in ws:
        cy = w["y"] + w["h"] / 2
        if out and abs(out[-1]["cy"] - cy) <= max(out[-1]["h"], w["h"]) * 0.5:
            ln = out[-1]
            ln["words"].append(w)
            ln["h"] = max(ln["h"], w["h"])
        else:
            out.append({"cy": cy, "h": w["h"], "words": [w]})
    for ln in out:
        # weit auseinander stehende Wortgruppen (Spalten) bleiben eigene Zeilen
        ln["words"].sort(key=lambda w: w["x"])
    res = []
    for ln in out:
        group: list[dict[str, Any]] = []
        for w in ln["words"]:
            if group and w["x"] - (group[-1]["x"] + group[-1]["w"]) > max(ln["h"] * 12, 150):
                res.append(_line(group))
                group = []
            group.append(w)
        if group:
            res.append(_line(group))
    return res


def _line(ws: list[dict[str, Any]]) -> dict[str, Any]:
    return {"text": " ".join(w["text"] for w in ws), "words": ws, "x": ws[0]["x"],
            "cy": sum(w["y"] + w["h"] / 2 for w in ws) / len(ws), "h": max(w["h"] for w in ws)}


def read_image(data: bytes) -> tuple[Image.Image, list[list[dict[str, Any]]]]:
    """Bild öffnen und zweimal lesen (invertiert und normal, vergrößert). Gibt das Bild und beide Zeilenlisten zurück."""
    img = Image.open(io.BytesIO(data))
    if img.width * img.height > MAX_PIXELS:
        raise ValueError("zu groß")
    return _read(ImageOps.exif_transpose(img).convert("RGB"))


def _read(img: Image.Image) -> tuple[Image.Image, list[list[dict[str, Any]]]]:
    gray = img.convert("L")
    scale = 3 if img.width < 1000 else 2 if img.width < 3000 else 1
    big = gray.resize((gray.width * scale, gray.height * scale), Image.LANCZOS) if scale > 1 else gray
    dark = sum(gray.resize((32, 32)).getdata()) / 1024 < 128
    first, second = (ImageOps.invert(big), big) if dark else (big, ImageOps.invert(big))
    return img, [lines(_tsv(first), scale), lines(_tsv(second), scale)]


# ---- Bambu Studio ------------------------------------------------------------------

def _square_color(img: Image.Image, x0: float, cy: float, h: float) -> str | None:
    """Farbe des Kästchens links neben einer Filamentzeile (Slicing Result): das nächste farbige Feld links von den
    Gramm, gemessen gegen den Hintergrund direkt davor. Mehrere Höhen, weil die Ziffer im Kästchen stört."""
    if x0 <= 2 or h <= 0:
        return None
    px = img.load()
    left, right = max(0, int(x0 - h * 25)), int(x0) - 1
    for dy in (-0.3, 0.3, 0.0):
        y = int(round(cy + dy * h))
        if not (0 <= y < img.height) or right - left < 4:
            continue
        row = [px[x, y] for x in range(left, right)]
        near = sorted(row[-max(4, len(row) // 3):], key=sum)
        bg = near[len(near) // 2]  # Hintergrund des Kastens zwischen Kästchen und Zahl
        hit = [max(abs(c1 - c2) for c1, c2 in zip(p, bg)) > 45 for p in row]
        i = len(row) - 1
        while i >= 0:  # von der Zahl nach links: erstes Feld in Kästchengröße
            if hit[i]:
                j = i
                while j > 0 and hit[j - 1]:
                    j -= 1
                if h * 0.6 <= i - j + 1 <= h * 2.5:
                    band = [px[left + x, yy] for x in range(j + 1, i) for yy in range(max(0, y - 2), min(img.height, y + 3))]
                    if band:
                        r, g, bl = (sorted(c[k] for c in band)[len(band) // 2] for k in range(3))
                        return f"#{r:02X}{g:02X}{bl:02X}"
                i = j - 1
            else:
                i -= 1
    return None


def parse_bambu(ls: list[dict[str, Any]], img: Image.Image | None = None) -> dict[str, Any] | None:
    rows, total_g, total_min, printer, warn = [], None, None, None, []
    for ln in ls:
        txt = ln["text"]
        m = _BAMBU_ROW.search(txt)
        if m:
            if re.search(r"\btotal\b", txt, re.I):
                total_g = _f(m.group(1))
                total_min = total_min or duration(txt[m.end():])
            else:
                g_word = next((w for w in ln["words"] if re.match(r"\d+(?:[.,]\d+)?\s*[g9]", w["text"])), ln["words"][0])
                rows.append({"grams": _f(m.group(1)), "x": g_word["x"], "cy": ln["cy"], "h": ln["h"]})
            continue
        tm = re.search(r"total\s*time\s*:?\s*(.+)", txt, re.I)
        if tm:
            total_min = duration(tm.group(1)) or total_min
        pm = re.search(r"bambu\s*lab\s+([A-Z][A-Z0-9]{1,4}(?:\s*(?:mini|Pro|Carbon))?)", txt, re.I)
        if pm and not printer:
            printer = pm.group(1).strip()
    if not rows:
        return None
    if total_g and abs(sum(r["grams"] for r in rows) - total_g) > max(0.02 * total_g, 0.2):
        fixed = False
        for r in rows:
            rest = total_g - (sum(x["grams"] for x in rows) - r["grams"])
            if rest > 0 and any(abs(r["grams"] / rest - k) / k < 0.05 for k in (10, 100, 1000)):
                warn.append(t("{a} g als {b} g gelesen, korrigiert.", a=f"{rest:g}", b=f"{r['grams']:g}"))
                r["grams"] = round(rest, 2)
                fixed = True
                break
        if not fixed:
            warn.append(t("Die Filamentzeilen ergeben nicht die Gesamtmenge ({g} g). Bitte prüfen.", g=f"{total_g:g}"))
    # Filamenttypen aus „Project Filaments“ (gleiche Reihenfolge wie die Zeilen)
    types = []
    for ln in ls:
        if _BAMBU_ROW.search(ln["text"]):
            continue
        for m in re.finditer(rf"\b({_TYPES})\b(?:\s+\w+)?", ln["text"]):
            types.append(m.group(1).upper())
    fil = []
    for i, r in enumerate(rows):
        tp = types[i] if len(types) >= len(rows) else "PLA"
        fil.append({"type": tp, "name": None, "grams": r["grams"],
                    "color": _square_color(img, r["x"], r["cy"], r["h"]) if img is not None else None})
    if len(types) < len(rows):
        warn.append(t("Filamenttyp nicht im Bild, PLA angenommen."))
    if not total_min:
        warn.append(t("Druckzeit nicht gefunden, bitte eintragen."))
    return {"kind": "bambu", "printer": printer, "plates": [{"name": "", "minutes": total_min, "fil": fil}], "warn": warn}


# ---- MakerWorld ------------------------------------------------------------------

def _chip_color(img: Image.Image, w: dict[str, Any]) -> str | None:
    """Hintergrund eines Filament-Chips (= Filamentfarbe) direkt links vom Text."""
    x, y = int(w["x"]) - 3, int(w["y"] + w["h"] / 2)
    if x < 1 or not (0 <= y < img.height):
        return None
    px = img.load()
    band = [px[xx, yy] for xx in range(max(0, x - 2), x + 1) for yy in range(max(0, y - 2), min(img.height, y + 3))]
    r, g, b = (sorted(c[k] for c in band)[len(band) // 2] for k in range(3))
    return f"#{r:02X}{g:02X}{b:02X}"


def _ocr_text(img: Image.Image) -> str:
    """Kurzen Ausschnitt (ein Wort, ein Chip) als eine Textzeile lesen."""
    buf = io.BytesIO()
    img.save(buf, "PNG")
    try:
        return subprocess.run(["tesseract", "stdin", "stdout", "-l", _lang(), "--psm", "7"], input=buf.getvalue(),
                              capture_output=True, timeout=30, check=False).stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _crop_read(img: Image.Image, box: tuple[int, int, int, int], scale: int = 4) -> str:
    """Ausschnitt einzeln, stark vergrößert lesen: kleine Punkte („1.5 h“) gehen so nicht verloren."""
    x0, y0, x1, y1 = (int(v) for v in box)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(img.width, x1), min(img.height, y1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return ""
    c = img.convert("L").crop((x0, y0, x1, y1))
    dark = sum(c.getdata()) / (c.width * c.height) < 128
    c = c.resize((c.width * scale, c.height * scale), Image.LANCZOS)
    if dark:
        c = ImageOps.invert(c)
    return _ocr_text(ImageOps.expand(c, border=20, fill=255))


def _word_box(ws: list[dict[str, Any]], pad: int = 4) -> tuple[int, int, int, int]:
    return (int(min(w["x"] for w in ws)) - pad, int(min(w["y"] for w in ws)) - pad,
            int(max(w["x"] + w["w"] for w in ws)) + pad, int(max(w["y"] + w["h"] for w in ws)) + pad)


def _chip_boxes(img: Image.Image, y0: int, y1: int) -> list[tuple[int, int, int, int]]:
    """Farbige Filament-Chips (Rechtecke) in einem Streifen des Bildes finden, von links nach rechts."""
    y0, y1 = max(0, y0), min(img.height, y1)
    if y1 - y0 < 8:
        return []
    px = img.load()
    strip = img.crop((0, y0, img.width, y1)).convert("RGB")
    bg = max(strip.getcolors(strip.width * strip.height) or [(1, px[1, y0])])[1]  # häufigste Farbe = Hintergrund
    far = lambda p: max(abs(a - b) for a, b in zip(p, bg)) > 28  # noqa: E731
    rows = [sum(1 for x in range(0, img.width, 2) if far(px[x, y])) for y in range(y0, y1)]
    band, start = None, None
    for i, n in enumerate(rows + [0]):
        on = n > img.width / 2 * 0.12
        if on and start is None:
            start = i
        elif not on and start is not None:
            if 10 <= i - start <= 60:
                band = (y0 + start, y0 + i)
                break
            start = None
    if not band:
        return []
    ya, yb = band
    mid = [(ya + yb) // 2 + d for d in (-(yb - ya) // 3, 0, (yb - ya) // 3)]
    cols = [sum(1 for y in mid if far(px[x, y])) >= 2 for x in range(img.width)]
    # Ein Chip: Spalten mit Chipfarbe, kleine Lücken (Buchstaben auf gleicher Farbe wie der Hintergrund) überbrücken
    boxes, start, gap = [], None, 0
    for x, on in enumerate(cols + [False] * 8):
        if on:
            start = x if start is None else start
            gap = 0
        elif start is not None:
            gap += 1
            if gap > 4:
                if x - gap - start >= (yb - ya) * 1.5:
                    boxes.append((start, ya, x - gap + 1, yb))
                start, gap = None, 0
    return boxes


def _box_color(img: Image.Image, box: tuple[int, int, int, int]) -> str:
    """Häufigste Farbe in einem Chip (= Hintergrund des Chips)."""
    c = img.crop(box).convert("RGB").quantize(colors=4).convert("RGB")
    (_n, col), *_ = sorted(c.getcolors(64) or [(1, (128, 128, 128))], reverse=True)
    return f"#{col[0]:02X}{col[1]:02X}{col[2]:02X}"


def _fix_digits(s: str) -> str:
    """Typische Verwechslungen in Zahlen (S→5, O→0, l/I→1) vor einer Einheit."""
    return re.sub(r"(?<=[|I!l\s])([0-9SOolI]{1,4}(?:[.,][0-9SOolI]+)?)\s*(?=[g¢9]\b)",
                  lambda m: m.group(1).translate(str.maketrans("SOolI", "50011")), s)


def _mw_stats(ls: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Zeile „1 Platte · 1.5 h · 8 g“ im MakerWorld-Kasten."""
    return next((ln for ln in ls if re.search(r"\d+\s*(?:Platten?|Plates?)\b", ln["text"], re.I)), None)


MW_TEXT_H = 16  # Schrifthöhe der Statistikzeile in px, auf die MakerWorld-Screenshots gebracht werden


def _match_total(chips: list[tuple[str, float, str | None]], total: float | None) -> list[tuple[str, float, str | None]]:
    """Passen die Chips nicht zur Gesamtmenge, war oft das „g“ als Ziffer gelesen („3g“ → 38, „5g“ → 55):
    die letzte Ziffer der Chips weglassen, bei denen es dann aufgeht."""
    if not total or not chips or len(chips) > 6 or abs(sum(g for _, g, _ in chips) - total) <= 0.5:
        return chips
    alts = [[g] + ([float(int(g) // 10)] if g >= 10 and g == int(g) else []) for _, g, _ in chips]
    for combo in product(*alts):
        if abs(sum(combo) - total) <= 0.5:
            return [(tp, g, col) for (tp, _, col), g in zip(chips, combo)]
    return chips


def parse_makerworld(passes: list[list[dict[str, Any]]], img: Image.Image | None = None) -> dict[str, Any] | None:
    ls = passes[0]
    stats = _mw_stats(ls)
    if not stats or not re.search(rf"{_NUM}\s*h\b", stats["text"]):
        return None
    n = max(1, min(int(re.search(r"(\d+)\s*(?:Platten?|Plates?)\b", stats["text"], re.I).group(1)), 60))
    hours = total = None
    hours_dot = False
    ws = stats["words"]
    for i, w in enumerate(ws):  # Zeit und Gramm einzeln vergrößert nachlesen (Dezimalpunkt)
        nxt = ws[i + 1]["text"] if i + 1 < len(ws) else ""
        unit = "h" if re.fullmatch(rf"{_NUM}h", w["text"], re.I) or (re.fullmatch(_NUM, w["text"]) and nxt.lower() == "h") else \
               "g" if re.fullmatch(rf"{_NUM}g", w["text"], re.I) or (re.fullmatch(_NUM, w["text"]) and nxt.lower() == "g") else None
        if not unit or (unit == "h" and hours is not None) or (unit == "g" and total is not None):
            continue
        parts = [w] + ([ws[i + 1]] if re.fullmatch(_NUM, w["text"]) else [])
        txt = _crop_read(img, _word_box(parts)) if img is not None else ""
        m = re.search(rf"({_NUM})\s*{unit}", txt, re.I) or re.search(rf"({_NUM})", w["text"])
        if not m:
            continue
        if unit == "h":
            hours, hours_dot = _f(m.group(1)), bool(re.search(r"[.,]", m.group(1)))
        else:
            total = _f(m.group(1))
    if hours is None:
        hours = _f(re.search(rf"({_NUM})\s*h\b", stats["text"]).group(1))
    warn = []
    # MakerWorld zeigt Stunden mit einer Nachkommastelle; ohne Punkt und unplausibel wenig Filament pro Stunde
    if not hours_dot and hours >= 10 and total and total / hours < 2:
        hours /= 10
        warn.append(t("Druckzeit als {h} h gelesen (Dezimalpunkt ergänzt).", h=f"{hours:g}"))
    minutes = hours * 60

    chips: list[tuple[str, float, str | None]] = []
    if img is not None:  # Chips im Bild suchen (Streifen unter der Zeile mit Platten/Zeit) und einzeln lesen
        y = int(stats["cy"] + stats["h"])
        for box in _chip_boxes(img, y, int(y + stats["h"] * 5)):
            m = _CHIP.search(_fix_digits(_crop_read(img, (box[0] + 2, box[1] + 2, box[2] - 2, box[3] - 2))))
            if m:
                chips.append((m.group(1).upper(), _f(m.group(2)), _box_color(img, box)))
    if not chips:  # sonst aus dem Text der ganzen Seite
        for ls_ in passes:
            for ln in ls_:
                if ln is stats or re.search(r"Platte|Plate", ln["text"], re.I):
                    continue
                for m in _CHIP.finditer(_fix_digits(ln["text"])):
                    c = (m.group(1).upper(), _f(m.group(2)))
                    if c not in [(a, b) for a, b, _ in chips]:
                        w = next((w for w in ln["words"] if w["text"].upper().startswith(c[0])), None)
                        chips.append((*c, _chip_color(img, w) if img is not None and w else None))
    chips = _match_total(chips, total)
    fil = [{"type": tp, "name": tp, "color": col, "grams": g} for tp, g, col in chips]
    got = sum(g for _, g, _ in chips)
    if total and got < total - 0.5:
        fil.append({"type": chips[0][0] if chips else "PLA", "name": None, "color": None, "grams": round(total - got, 1)})
        warn.append(t("Nicht alle Filamente erkannt, Rest ({g} g) als eigene Zeile.", g=f"{total - got:g}"))
    if not fil and total:
        fil = [{"type": "PLA", "name": None, "color": None, "grams": total}]
    compat: list[str] = []
    for i, ln in enumerate(ls):
        if ln["text"].count(",") >= 2 and re.search(r"\b[AHPX]\d", ln["text"]):
            joined = ln["text"]
            nxt = next((x["text"] for x in ls[i + 1:i + 4] if re.search(r"[A-Za-z0-9]{2}", x["text"])), "")
            if nxt and re.fullmatch(r"[\w ,+-]+", nxt) and re.search(r"\b[AHPX]\d|mini", nxt):
                joined += ("" if joined.endswith(",") else " ") + nxt
            compat = [c.strip(" .-") for c in re.sub(r"^\W+", "", joined).split(",") if c.strip(" .-")]
            break
    plates = [{"name": "", "minutes": round(minutes / n, 1), "fil": [{**f, "grams": round(f["grams"] / n, 2)} for f in fil]}
              for _ in range(n)]
    if n > 1:
        warn.append(t("Zeit und Gramm gleichmäßig auf {n} Platten verteilt.", n=n))
    return {"kind": "makerworld", "printer": None, "compatible": compat or None, "plates": plates, "warn": warn}


def analyse(data: bytes) -> dict[str, Any]:
    """Screenshot → Daten für den Rechner oder {"error": …}."""
    if not available():
        return {"error": t("Texterkennung ist nicht installiert (tesseract). Trag die Werte von Hand ein.")}
    if len(data) > MAX_BYTES:
        return {"error": t("Das Bild ist zu groß.")}
    try:
        img, passes = read_image(data)
    except Exception as err:  # noqa: BLE001
        log.info("Screenshot nicht lesbar: %s", err)
        return {"error": t("Das ist kein lesbares Bild.")}
    res = parse_bambu(passes[0], img) or parse_bambu(passes[1], img)
    if not res:
        stats = _mw_stats(passes[0])
        if stats and stats["h"] and abs(stats["h"] / MW_TEXT_H - 1) > 0.15:
            # Screenshot auf eine feste Schriftgröße bringen (Zoom, Retina, Handy): Punkte und Chips sind sonst
            # je nach Auflösung zu klein oder zu groß für die Erkennung
            f = MW_TEXT_H / stats["h"]
            if img.width * f * img.height * f <= MAX_PIXELS:
                img, passes = _read(img.resize((max(1, round(img.width * f)), max(1, round(img.height * f))), Image.LANCZOS))
        res = parse_makerworld(passes, img)
    if not res:
        return {"error": t("Im Bild steht kein Slicing-Ergebnis aus Bambu Studio und keine MakerWorld-Profilangabe. Trag die Werte von Hand ein.")}
    note = t("Aus dem Screenshot gelesen, bitte prüfen.")
    return {"source": "ocr", "title": "", "url": "", "printer": res.get("printer"), "compatible": res.get("compatible"),
            "plates": res["plates"], "note": " ".join([note, *res["warn"]])}
