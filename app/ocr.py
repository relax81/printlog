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
_CHIP = re.compile(rf"\b({_TYPES})(?:[-\s](?:CF|HF|GF|Basic|Matte|Silk\+?|Tough|Lite))?[\s|I!l]*?({_NUM})\s*g\b", re.I)


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
    img = ImageOps.exif_transpose(img).convert("RGB")
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


def parse_makerworld(passes: list[list[dict[str, Any]]], img: Image.Image | None = None) -> dict[str, Any] | None:
    ls = passes[0]
    txt = "\n".join(ln["text"] for ln in ls)
    pm = re.search(r"(\d+)\s*(?:Platten?|Plates?|Druckplatten?)\b", txt, re.I)
    hm = re.search(rf"({_NUM})\s*h\b", txt)
    if not (pm and hm):
        return None
    n = max(1, min(int(pm.group(1)), 60))
    minutes = _f(hm.group(1)) * 60
    total = None
    for ln in ls:  # Gesamtgramm steht in der Zeile mit Platten/Zeit
        if re.search(r"Platte|Plate", ln["text"], re.I):
            gm = re.search(rf"({_NUM})\s*g\b", ln["text"][ln["text"].find(hm.group(0)) + len(hm.group(0)):] if hm.group(0) in ln["text"] else ln["text"])
            if gm:
                total = _f(gm.group(1))
                break
    chips: list[tuple[str, float]] = []
    colors: dict[tuple[str, float], str | None] = {}
    for ls_ in passes:
        for ln in ls_:
            if re.search(r"Platte|Plate", ln["text"], re.I):
                continue
            for m in _CHIP.finditer(ln["text"]):
                c = (m.group(1).upper(), _f(m.group(2)))
                if c not in chips:
                    chips.append(c)
                    w = next((w for w in ln["words"] if w["text"].upper().startswith(c[0])), None)
                    colors[c] = _chip_color(img, w) if img is not None and w else None
    warn = []
    fil = [{"type": tp, "name": tp, "color": colors.get((tp, g)), "grams": g} for tp, g in chips]
    got = sum(g for _, g in chips)
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
    res = parse_bambu(passes[0], img) or parse_bambu(passes[1], img) or parse_makerworld(passes, img)
    if not res:
        return {"error": t("Im Bild steht kein Slicing-Ergebnis aus Bambu Studio und keine MakerWorld-Profilangabe. Trag die Werte von Hand ein.")}
    note = t("Aus dem Screenshot gelesen, bitte prüfen.")
    return {"source": "ocr", "title": "", "url": "", "printer": res.get("printer"), "compatible": res.get("compatible"),
            "plates": res["plates"], "note": " ".join([note, *res["warn"]])}
