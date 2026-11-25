"""Export eines Drucks zum Teilen oder Ablegen.

* ``bon_png``: Bild im Kassenbon-Stil (passend zum App-Icon), z. B. für WhatsApp.
  Variante „kunde“ zeigt nur Foto, Titel, Stückzahl, Preis und Zahlungsstand,
  Variante „intern“ zusätzlich Material, Kostenaufschlüsselung, Margen und Gewinn.
* ``report_pdf``: A4-PDF mit voller Aufschlüsselung (Kosten, Margen, Zahlungen,
  Übergaben) zum Ablegen. Grundlage für ein späteres Angebot bzw. eine Rechnung.

Beides wird im Container gerendert (Pillow bzw. ReportLab), ohne externe Dienste.
"""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .costing import hourly_rate, material_notes, parse_materials
from .db import Store, code_label
from .defects import apply_charge, charge_src, charge_text, charged, defect_label, no_charge
from .i18n import N_, note as t_note, t
from .pages import _short, _status_of, fmt_hours, margins, price_base, print_hours, printers_of
from .ui import STATIC_DIR, bon_tagline, fmt_date, fmt_dur, fmt_input, fmt_num, printer_names

VARIANTS = ("kunde", "intern")

FONT_DIRS = (Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu"),
             Path("/usr/share/fonts/TTF"))
FONT_FILES = {
    "mono": "DejaVuSansMono.ttf",
    "mono-b": "DejaVuSansMono-Bold.ttf",
    "sans": "DejaVuSans.ttf",
    "sans-b": "DejaVuSans-Bold.ttf",
}


def _font_path(kind: str) -> Path | None:
    for d in FONT_DIRS:
        p = d / FONT_FILES[kind]
        if p.exists():
            return p
    return None


@lru_cache(maxsize=64)
def _font(kind: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    p = _font_path(kind)
    return ImageFont.truetype(str(p), size) if p else ImageFont.load_default(size)


def _eur(v: float | None) -> str:
    return fmt_num(v, 2, "€")


def _date(ts: float | None, kind: str = "date") -> str:
    return fmt_date(ts, kind) if ts else "–"


# Dateinamen-Zusatz je Variante (Pfade der Export-URLs bleiben deutsch)
SUFFIXES = {"kunde": N_("kunde"), "intern": N_("intern"), "kosten": N_("kosten")}


def file_name(job: dict[str, Any], suffix: str, ext: str) -> str:
    """z. B. printlog_PL-7K3Q_Kabelhalter_kunde.png (nur ASCII, Umlaute umschrieben)."""
    name = job.get("task_name") or t("Druck")
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        name = name.replace(a, b)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:40] or t("Druck")
    ref = code_label(job.get("code")) or job["id"]
    suffix = t(SUFFIXES[suffix]) if suffix in SUFFIXES else suffix
    return f"printlog_{ref}_{name}{'_' + suffix if suffix else ''}.{ext}"


def bon_logo(store: Store, image_dir: Path) -> Path:
    """Eigenes Logo aus den Einstellungen, sonst das App-Logo."""
    name = store.settings().get("bon_logo")
    if name:
        path = image_dir / name
        if path.is_file():
            return path
    return STATIC_DIR / "logo-bon.png"


# ---- Daten ------------------------------------------------------------------

def collect(store: Store, job: dict[str, Any], image_dir: Path, status: bool = True) -> dict[str, Any]:
    """Alles, was beide Exporte brauchen, an einer Stelle berechnet.

    ``status=False`` lässt Zahlungs- und Übergabestand weg (Häkchen im Teilen-Menü).
    """
    qty = job.get("quantity") or 1
    cost = job.get("cost_total")
    pbase = price_base(job)  # beim Projekt ohne unberechnete Fehldrucke
    has_customer = bool(job.get("customer_id"))
    project = job.get("kind") == "project"
    kids = store.children([job["id"]]).get(job["id"], []) if project else []
    # Anteil (aufgeteilter Druck/Projekt): Material, Drucker und Bild kommen vom Druck, anteilig nach Stückzahl
    msrc = job
    frac = 1.0
    if job.get("kind") == "share":
        parent = store.get(job["share_of"]) if job.get("share_of") else None
        part = store.get(job["share_part"]) if job.get("share_part") else None
        msrc = part or parent or job
        frac = qty / (msrc.get("quantity") or 1)
    src_kids = store.children([msrc["id"]]).get(msrc["id"], []) if msrc is not job and msrc.get("kind") == "project" else []
    cm = job.get("customer_margin")
    general = margins(store)

    prices: list[dict[str, Any]] = []
    if cm is not None:
        prices.append({"name": f'{t("Kunde")} {job.get("customer_name") or ""}'.strip(), "pct": cm, "customer": True})
    prices += [{"name": n, "pct": p, "customer": False} for n, p in general]
    for p in prices:
        p["price"] = None if pbase is None else pbase * (1 + p["pct"] / 100)

    # Was der Kunde zahlt: Endpreis, sonst Kundenpreis. Ohne Kunde die erste allgemeine Marge.
    if job.get("final_price") is not None:
        due, src = job["final_price"], t("Endpreis")
    elif cm is not None and pbase is not None:
        due, src = pbase * (1 + cm / 100), t("Kosten + {p} % Kundenmarge", p=fmt_input(cm))
    elif not has_customer and general and pbase is not None:
        due, src = pbase * (1 + general[0][1] / 100), t("Kosten + {p} % ({name})", p=fmt_input(general[0][1]), name=general[0][0])
    else:
        due, src = pbase, t("Selbstkosten")
    gift = bool(job.get("gift"))
    scrap = bool(job.get("is_scrap"))
    # Fehldruck: anteilig/fester Betrag/voll berechnen oder (ohne Berechnung) nichts
    base = due
    nocharge = no_charge(job) and has_customer
    if charged(job) and has_customer:
        due, src = apply_charge(job, due), charge_src(job, due)
    elif nocharge:
        due, src = 0.0, t("Fehldruck, wird nicht berechnet")
    # Kunde mit „Keine Abrechnung“ (Eigenbedarf): nie Zahlungs- oder Übergabestand zeigen
    nobill = has_customer and bool(job.get("customer_nobill"))
    billed = has_customer and not nobill
    payable = status and billed and not gift and not nocharge and due is not None
    paid = job.get("paid") or 0.0

    # Material mit Kosten je Slot (gleiche Regeln wie costing.compute)
    factor = 1.0
    if msrc.get("status") == "abgebrochen" and msrc.get("progress") is not None:
        factor = max(0.0, min(1.0, msrc["progress"] / 100.0))
    mprices = store.material_prices()
    materials: list[dict[str, Any]] = []
    many = project or bool(src_kids)
    for src_job in kids or src_kids or [msrc]:
        f = factor
        if many:
            f = max(0.0, min(1.0, (src_job.get("progress") or 0) / 100.0)) \
                if src_job.get("status") == "abgebrochen" and src_job.get("progress") is not None else 1.0
        notes = material_notes(src_job)
        for i, m in enumerate(parse_materials(src_job.get("materials"))):
            m["note"] = notes.get(i)
            eur_kg = (mprices.get(m["name"] or "") or mprices.get(m["type"] or "") or {}).get("eur_per_kg")
            g = (m["grams"] or 0) * f * frac
            same = next((x for x in materials if many and (x["name"], x.get("color"), x["note"]) == (m["name"], m.get("color"), m["note"])), None)
            if same:  # Projekt: gleiche Filamente der Drucke zusammenzählen
                same["grams_used"] += g
                same["cost"] = None if eur_kg is None else same["grams_used"] / 1000 * eur_kg
                continue
            materials.append({**m, "grams_used": g, "eur_kg": eur_kg, "cost": None if eur_kg is None else g / 1000 * eur_kg})
    if many:
        for m in materials:
            m["slot"] = None

    rate = hourly_rate(store.printer_rates().get(msrc["printer"])) if not many and msrc.get("printer") else None
    photos = store.photos(job["id"]) + (store.photos(msrc["id"]) if msrc is not job else [])
    photo: Any = None
    for name in [p["file"] for p in photos] + ([msrc["image"]] if msrc.get("image") and not many else []):
        path = image_dir / name
        if path.exists() and path.suffix.lower() not in (".heic", ".heif"):
            photo = path
            break
    if photo is None and (kids or src_kids):
        photo = _mosaic(image_dir, kids or src_kids)
    names = printer_names(store)
    parts = [{"name": k.get("task_name") or t("Druck"), "ref": code_label(k.get("code")) or t("Nr. {id}", id=k["id"]),
              "printer": names.get(k["printer"], k.get("printer_name") or k["printer"]), "start": k.get("start_ts"),
              "dur": fmt_dur(k.get("start_ts"), k.get("end_ts")), "qty": k.get("quantity") or 1, "cost": k.get("cost_total"),
              "scrap": bool(k.get("is_scrap")), "nocharge": no_charge(k),
              "charge": charge_text(k) if k.get("is_scrap") and not no_charge(k) else "",
              "grams": k["filament_g"] if k.get("filament_g") is not None else k.get("weight_g")} for k in kids]

    grams = job["filament_g"] if job.get("filament_g") is not None else job.get("weight_g")
    return {
        "job": job,
        "ref": code_label(job.get("code")) or t("Nr. {id}", id=job["id"]),
        "logo": bon_logo(store, image_dir),
        "qty": qty,
        "cost": cost,
        "prices": prices,
        "due": 0.0 if gift else due,
        "due_calc": due,
        "due_src": src,
        "gift": gift,
        "nobill": nobill,
        "scrap": scrap,
        "nocharge": nocharge,
        "defect": defect_label(job, icon=False),
        "charge": charge_text(job) if has_customer else "",
        "base_due": base,
        "customer": job.get("customer_name") if has_customer else None,
        "payable": payable,
        "paid": paid,
        "open": max(0.0, (due or 0) - paid) if payable else 0.0,
        "status_shown": status,
        "payments": store.payments(job["id"]) if status else [],
        "handovers": store.handovers(job["id"]) if status else [],
        "delivered": min(job.get("delivered") or 0, qty),
        "show_handover": status and billed and not nocharge and job.get("state") == "done",
        "materials": materials,
        "grams": grams,
        "rate": rate,
        "printer": printers_of(kids or src_kids, names) if many else names.get(msrc["printer"], msrc["printer"]),
        "project": project,
        "parts": parts,
        "hours": print_hours(kids) if project else None,
        "pbase": pbase,
        "status": _status_of(job),
        "photo": photo,
        "profit": None if (cost is None or due is None or gift or nobill) else due - cost,
    }


def _mosaic(image_dir: Path, kids: list[dict[str, Any]]) -> io.BytesIO | None:
    """Bild eines Projekts ohne eigenes Foto: bis zu vier Vorschaubilder seiner Drucke im Raster."""
    files = []
    for k in sorted(kids, key=lambda k: bool(k.get("is_scrap"))):
        for name in [k.get("image")]:
            if name and (image_dir / name).exists() and name not in files:
                files.append(name)
    files = files[:4]
    if not files:
        return None
    n = 1 if len(files) == 1 else 2
    cell, gap = 400, 16
    size = n * cell + (n - 1) * gap
    out = Image.new("RGB", (size, size if len(files) > 2 or n == 1 else cell), (233, 226, 212))
    for i, name in enumerate(files):
        try:
            with Image.open(image_dir / name) as src:
                im = ImageOps.exif_transpose(src).convert("RGBA")
        except OSError:
            continue
        im.thumbnail((cell, cell), Image.LANCZOS)
        tile = Image.new("RGBA", (cell, cell), (244, 240, 232, 255))
        tile.alpha_composite(im, ((cell - im.width) // 2, (cell - im.height) // 2))
        out.paste(tile.convert("RGB"), ((i % n) * (cell + gap), (i // n) * (cell + gap)))
    buf = io.BytesIO()
    out.save(buf, "PNG")
    buf.seek(0)
    return buf


# ---- Bon (PNG) ----------------------------------------------------------------

PAPER = (244, 238, 227)
INK = (36, 33, 29)
MUTED = (128, 119, 106)
FAINT = (205, 195, 178)
ACCENT = (232, 106, 18)      # Orange aus dem Icon (€-Zeichen), etwas dunkler für Text auf Papier
OK = (31, 138, 76)
GIFT = (140, 82, 214)
BG_TOP, BG_BOTTOM = (32, 35, 44), (22, 24, 31)

W = 1080          # Bildbreite
PW = 900          # Papierbreite
PAD = 62          # Innenabstand auf dem Papier
CW = PW - 2 * PAD
TOOTH = 30        # Zacken an der Abrisskante


class _Bon:
    def __init__(self) -> None:
        self.img = Image.new("RGBA", (PW, 6000), PAPER + (255,))
        self.d = ImageDraw.Draw(self.img)
        self.y = 54
        self.stamps: list[tuple[str, tuple[int, int, int], int]] = []

    # Grundbausteine
    def gap(self, h: int) -> None:
        self.y += h

    def _w(self, text: str, font: Any) -> int:
        return int(self.d.textlength(text, font=font))

    def wrap(self, text: str, font: Any, width: int = CW) -> list[str]:
        lines: list[str] = []
        for para in text.split("\n"):
            cur = ""
            for word in para.split():
                while self._w(word, font) > width:          # sehr lange Wörter hart umbrechen
                    cut = len(word)
                    while cut > 1 and self._w(word[:cut], font) > width:
                        cut -= 1
                    if cur:
                        lines.append(cur)
                        cur = ""
                    lines.append(word[:cut])
                    word = word[cut:]
                test = f"{cur} {word}".strip()
                if self._w(test, font) <= width:
                    cur = test
                else:
                    lines.append(cur)
                    cur = word
            if cur:
                lines.append(cur)
        return lines or [""]

    def center(self, text: str, font: Any, fill: tuple = INK, spacing: int = 8, max_lines: int = 4) -> None:
        lines = self.wrap(text, font)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1].rstrip(" .,") + " …"
        asc, desc = font.getmetrics()
        for line in lines:
            self.d.text((PAD + (CW - self._w(line, font)) // 2, self.y), line, font=font, fill=fill)
            self.y += asc + desc + spacing

    def row(self, label: str, value: str, size: int = 30, bold: bool = False, fill: tuple = INK,
            vfill: tuple | None = None, leader: bool = True, indent: int = 0) -> None:
        f = _font("mono-b" if bold else "mono", size)
        fl = _font("mono", size) if not bold else f
        vw = self._w(value, f)
        lab = label
        while lab and self._w(lab, fl) > CW - indent - vw - 30:
            lab = lab[:-2] + "…"
        x0 = PAD + indent
        self.d.text((x0, self.y), lab, font=fl, fill=fill)
        self.d.text((PAD + CW - vw, self.y), value, font=f, fill=vfill or fill)
        asc, desc = f.getmetrics()
        if leader:
            a = x0 + self._w(lab, fl) + 14
            b = PAD + CW - vw - 14
            yy = self.y + asc - 4
            x = a
            while x < b:
                self.d.ellipse((x, yy, x + 3, yy + 3), fill=FAINT)
                x += 13
        self.y += asc + desc + 12

    def dot(self, color: str | None) -> None:
        """Farbpunkt vor einer Material-Zeile (Zeile danach mit indent=36)."""
        col = color or ""
        rgb = None
        if re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", col):
            rgb = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
        y = self.y + 8
        self.d.ellipse((PAD, y, PAD + 22, y + 22), fill=rgb or FAINT, outline=FAINT, width=2)

    def sub(self, text: str, size: int = 22, indent: int = 0) -> None:
        """Kleine graue Zeile unter einer Zeile (z. B. kWh, Gramm)."""
        f = _font("mono", size)
        self.y -= 8
        for line in self.wrap(text, f, CW - indent):
            self.d.text((PAD + indent, self.y), line, font=f, fill=MUTED)
            self.y += size + 8
        self.y += 6

    def dashed(self, pad: int = 26) -> None:
        self.y += pad
        x = PAD
        while x < PAD + CW:
            self.d.line((x, self.y, min(x + 14, PAD + CW), self.y), fill=FAINT, width=3)
            x += 24
        self.y += pad

    def double(self, pad: int = 18) -> None:
        self.y += pad
        for dy in (0, 8):
            self.d.line((PAD, self.y + dy, PAD + CW, self.y + dy), fill=INK, width=3)
        self.y += 8 + pad

    def section(self, title: str) -> None:
        f = _font("mono-b", 22)
        txt = " ".join(title.upper())
        tw = self._w(txt, f)
        yy = self.y + 13
        x = PAD
        while x < PAD + (CW - tw) // 2 - 16:
            self.d.line((x, yy, min(x + 10, PAD + (CW - tw) // 2 - 16), yy), fill=FAINT, width=2)
            x += 18
        x = PAD + (CW + tw) // 2 + 16
        while x < PAD + CW:
            self.d.line((x, yy, min(x + 10, PAD + CW), yy), fill=FAINT, width=2)
            x += 18
        self.d.text((PAD + (CW - tw) // 2, self.y), txt, font=f, fill=MUTED)
        self.y += 46

    def band(self, text: str) -> None:
        """Warnstreifen ganz oben (Variante intern)."""
        f = _font("mono-b", 24)
        self.d.rectangle((0, 0, PW, 58), fill=ACCENT)
        self.d.text(((PW - self._w(text, f)) // 2, 15), text, font=f, fill=(255, 255, 255))
        self.y = 58 + 46

    def logo(self, path: Path) -> None:
        try:
            with Image.open(path) as src:
                lg = ImageOps.exif_transpose(src).convert("RGBA")
            # App-Logo: feste Höhe. Eigenes Logo: in einen Rahmen einpassen (breite Schriftzüge
            # wie hohe Signets bleiben lesbar).
            if path.name == "logo-bon.png":
                h = 74
                lg = lg.resize((round(lg.width * h / lg.height), h), Image.LANCZOS)
            else:
                lg.thumbnail((min(CW, 600), 170), Image.LANCZOS)
                if lg.height < 74:  # kleine Logos etwas hochskalieren
                    f = min(74 / lg.height, min(CW, 600) / lg.width)
                    lg = lg.resize((round(lg.width * f), round(lg.height * f)), Image.LANCZOS)
                h = lg.height
            self.img.alpha_composite(lg, ((PW - lg.width) // 2, self.y))
            self.y += h + 14
        except OSError:
            self.center("printl€g", _font("sans-b", 56))
        self.center(bon_tagline(), _font("mono", 22), MUTED, spacing=0)

    def photo(self, path: Path | None, max_h: int = 560) -> None:
        if not path:
            return
        try:
            with Image.open(path) as src:
                im = ImageOps.exif_transpose(src).convert("RGBA")
        except OSError:
            return
        inner = 18
        bw = CW
        im.thumbnail((bw - 2 * inner, max_h - 2 * inner), Image.LANCZOS)
        bh = im.height + 2 * inner
        box = Image.new("RGBA", (bw, bh), (233, 226, 212, 255))
        box.alpha_composite(im, ((bw - im.width) // 2, inner))
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, bw - 1, bh - 1), radius=22, fill=255)
        self.img.paste(box, (PAD, self.y), mask)
        self.y += bh + 30

    def stamp(self, text: str, color: tuple[int, int, int]) -> None:
        """Schräger Stempel unter der Summe, mit eigenem Platz, damit er keinen Betrag verdeckt."""
        self.gap(10)
        self.stamps.append((text, color, self.y + 62))
        self.gap(134)

    def _press(self, text: str, color: tuple[int, int, int], y: int) -> None:
        f = _font("mono-b", 46)
        tw = self._w(text, f)
        st = Image.new("RGBA", (tw + 70, 110), (0, 0, 0, 0))
        sd = ImageDraw.Draw(st)
        sd.rounded_rectangle((4, 4, st.width - 5, st.height - 5), radius=16, outline=color + (215,), width=6)
        sd.rounded_rectangle((14, 14, st.width - 15, st.height - 15), radius=10, outline=color + (150,), width=2)
        sd.text((35, 24), text, font=f, fill=color + (215,))
        st = st.rotate(11, resample=Image.BICUBIC, expand=True)
        self.img.alpha_composite(st, (PAD + (CW - st.width) // 2, max(0, y - st.height // 2)))

    def barcode(self, code: str) -> None:
        digest = hashlib.sha256(code.encode()).digest()
        widths = [2 + b % 4 for b in digest] + [2 + b // 64 for b in digest]
        total = sum(widths[i] for i in range(0, len(widths), 1)) + len(widths) * 3
        x = PAD + (CW - total) // 2
        for i, w in enumerate(widths):
            if i % 2 == 0:
                self.d.rectangle((x, self.y, x + w, self.y + 78), fill=INK)
            x += w + 3
        self.y += 92
        self.center(code, _font("mono", 22), MUTED, spacing=0)

    def finish(self) -> Image.Image:
        for text, color, y in self.stamps:
            self._press(text, color, y)
        h = self.y + 40
        paper = self.img.crop((0, 0, PW, h))
        # Abrisskanten oben und unten
        mask = Image.new("L", (PW, h), 255)
        md = ImageDraw.Draw(mask)
        n = PW // TOOTH
        step = PW / n
        for edge in ("top", "bottom"):
            pts = []
            for i in range(n + 1):
                x = i * step
                if edge == "top":
                    pts += [(x, 0), (x + step / 2, 14)] if i < n else [(x, 0)]
                else:
                    pts += [(x, h), (x + step / 2, h - 14)] if i < n else [(x, h)]
            if edge == "top":
                md.polygon([(0, -1)] + pts + [(PW, -1)], fill=0)
            else:
                md.polygon([(0, h + 1)] + pts + [(PW, h + 1)], fill=0)
        top, bottom = 70, 80
        H = h + top + bottom
        out = Image.new("RGB", (W, H))
        od = ImageDraw.Draw(out)
        for yy in range(H):
            frac = yy / max(1, H - 1)
            od.line((0, yy, W, yy), fill=tuple(round(a + (b - a) * frac) for a, b in zip(BG_TOP, BG_BOTTOM)))
        shadow = Image.new("L", (W, H), 0)
        shadow.paste(mask.point(lambda v: 150 if v else 0), ((W - PW) // 2 + 6, top + 16))
        shadow = shadow.filter(ImageFilter.GaussianBlur(18))
        out.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), shadow)
        out.paste(paper.convert("RGB"), ((W - PW) // 2, top), mask)
        return out


def bon_png(data: dict[str, Any], variant: str) -> bytes:
    job = data["job"]
    intern = variant == "intern"
    qty = data["qty"]
    b = _Bon()
    if intern:
        b.band(t("INTERN · MIT KALKULATION"))
    b.logo(data["logo"])
    b.gap(20)
    f_small = _font("mono", 24)
    b.row(_date(job.get("end_ts") or job.get("start_ts")), data["ref"], size=24, fill=MUTED, leader=False)
    b.dashed(18)
    b.photo(data["photo"])
    b.center(job.get("task_name") or t("Druck"), _font("sans-b", 44), spacing=10, max_lines=3)
    if data["project"]:
        b.gap(2)
        b.center(t("Projekt aus {n} Drucken", n=len(data["parts"])), f_small, MUTED, max_lines=1)
    if job.get("project_title") and job["project_title"] != job.get("task_name"):
        b.gap(2)
        b.center(job["project_title"], f_small, MUTED, max_lines=2)
    b.dashed()

    if data["customer"]:
        b.row(t("Für"), data["customer"])
    b.row(t("Stückzahl"), t("{q} Stk.", q=qty))
    due = data["due_calc"]
    if qty > 1 and due is not None and not data["gift"]:
        b.row(t("Preis pro Stück"), _eur(due / qty))

    if data["project"] and not intern:
        b.gap(10)
        b.section(t("Enthält"))
        for part in data["parts"]:
            if part["nocharge"]:
                continue
            b.row(part["name"], t("{q} Stk.", q=part["qty"]), size=26)
            if part["scrap"] and part["charge"]:
                b.sub(t("Fehldruck – {x}", x=part["charge"]))
    if intern:
        _bon_intern(b, data)
    elif any(m["note"] for m in data["materials"]):
        # Für Kunden nur die eigenen Notizen (z. B. genauer Farbname), ohne Gramm und Kosten
        b.gap(10)
        b.section(t("Farben"))
        for m in data["materials"]:
            if m["note"]:
                b.dot(m.get("color"))
                b.row(m["note"], "", leader=False, indent=36)

    b.double()
    if data["gift"]:
        b.row(t("SUMME"), _eur(0), size=46, bold=True, leader=False)
        b.gap(4)
        b.sub(t("Geschenk, nichts zu zahlen") + (" " + t("(Wert {v})", v=_eur(due)) if intern and due is not None else ""))
    else:
        b.row(t("SUMME"), _eur(due), size=46, bold=True, leader=False)
        if data["scrap"] and data["customer"] and not data["nobill"]:
            b.gap(4)
            if data["nocharge"]:
                b.sub(t("Fehldruck, wird nicht berechnet"))
            elif data["charge"]:
                b.sub(t("Fehldruck – {x}", x=data["charge"]) + (" " + t("(statt {v})", v=_eur(data["base_due"]))
                                                               if data["base_due"] is not None and job.get("defect_charge") != "full" else ""))
        if data["payable"]:
            b.gap(6)
            b.row(t("Bezahlt"), _eur(data["paid"]), vfill=OK if data["paid"] > 0 else INK)
            if data["open"] >= 0.005:
                b.row(t("Offen"), _eur(data["open"]), bold=True, vfill=ACCENT)
    if data["show_handover"]:
        d, n = data["delivered"], qty
        b.row(t("Übergeben"), t("alles") if d >= n else t("{d} von {n} Stk.", d=d, n=n))
    if data["gift"]:
        b.stamp(t("GESCHENK"), GIFT)
    elif data["payable"] and data["open"] < 0.005:
        b.stamp(t("BEZAHLT"), OK)

    if intern and (data["payments"] or data["handovers"]):
        b.dashed(18)
        for p in data["payments"]:
            b.row(f'{_date(p["ts"], "date_s")} {t("Zahlung")}', _eur(p["amount"]), size=24, fill=MUTED)
            if p.get("note"):
                b.sub(p["note"], 20)
        for h in data["handovers"]:
            b.row(f'{_date(h["ts"], "date_s")} {t("Übergabe")}', t("{q} Stk.", q=h["qty"]), size=24, fill=MUTED)
            if h.get("note"):
                b.sub(h["note"], 20)

    b.dashed()
    b.center(t("Vielen Dank!"), _font("sans-b", 34), spacing=4)
    b.gap(18)
    b.barcode(code_label(job.get("code")) or f"PL-{job['id']:06d}")
    out = b.finish()
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _bon_intern(b: _Bon, data: dict[str, Any]) -> None:
    job = data["job"]
    qty = data["qty"]
    b.gap(10)
    if data["project"]:
        b.section(t("Projekt"))
        b.row(t("Drucker"), data["printer"])
        b.row(t("Start"), _date(job.get("start_ts"), "ts"))
        b.row(t("Druckzeit"), fmt_hours(data["hours"] or 0))
        b.gap(10)
        b.section(t("Drucke"))
        for part in data["parts"]:
            b.row(part["name"], _eur(part["cost"]), size=26, fill=MUTED if part["nocharge"] else INK)
            extra = ""
            if part["scrap"]:
                extra = " · " + (t("Fehldruck, nicht berechnet") if part["nocharge"] else t("Fehldruck – {x}", x=part["charge"]))
            b.sub(f'{part["printer"]} · {part["dur"]} · {t("{q} Stk.", q=part["qty"])}{extra}', 20)
    else:
        b.section(t("Druck"))
        b.row(t("Drucker"), data["printer"])
        b.row(t("Start"), _date(job.get("start_ts"), "ts"))
        b.row(t("Dauer"), fmt_dur(job.get("start_ts"), job.get("end_ts")))
    if data["status"] != "fertig" and not data["project"]:
        b.row(t("Status"), t(data["status"]) + (" " + t("bei {p} %", p=int(job["progress"])) if job.get("status") == "abgebrochen"
                                                and job.get("progress") is not None else ""))
    if data["scrap"]:
        b.row(t("Fehldruck"), data["defect"] or t("ja"), vfill=ACCENT)
        if data["customer"] and not data["nobill"]:
            b.row(t("Berechnung"), data["charge"] or t("nicht berechnet"))

    if data["materials"]:
        b.gap(10)
        b.section(t("Material"))
        for m in data["materials"]:
            b.dot(m.get("color"))
            b.row(m["note"] or _short(m["name"]) or (m["type"] or "?"), fmt_num(m["grams_used"], 1, "g"), indent=36)
            if m["note"]:
                b.sub(_short(m["name"]) or (m["type"] or ""), indent=36)

    b.gap(10)
    b.section(t("Kosten"))
    b.row(t("Strom"), _eur(job.get("cost")))
    b.sub(fmt_num(job.get("kwh"), 3, "kWh") if job.get("kwh") is not None else t("kein Zählerstand"))
    b.row(t("Filament"), _eur(job.get("cost_filament")))
    b.sub(fmt_num(data["grams"], 1, "g"))
    b.row(t("Drucker"), _eur(job.get("cost_printer")))
    b.sub(t("Abschreibung + Wartung") + (f' · {fmt_num(data["rate"], 2, "€")}/h' if data["rate"] else ""))
    b.dashed(10)
    b.row(t("Selbstkosten"), _eur(data["cost"]), bold=True)
    if qty > 1 and data["cost"] is not None:
        b.sub(t("{v} pro Stück", v=_eur(data["cost"] / qty)))
    if data["pbase"] is not None and data["cost"] is not None and abs(data["pbase"] - data["cost"]) >= 0.005:
        b.row(t("Basis für Preise"), _eur(data["pbase"]))
        b.sub(t("ohne unberechnete Fehldrucke"))

    if data["prices"] or job.get("final_price") is not None:
        b.gap(10)
        b.section(t("Preise"))
        for p in data["prices"]:
            b.row(f'{p["name"]} +{fmt_input(p["pct"])} %', _eur(p["price"]),
                  vfill=ACCENT if p["customer"] and job.get("final_price") is None else None)
            if qty > 1 and p["price"] is not None:
                b.sub(t("{v} pro Stück", v=_eur(p["price"] / qty)))
        if job.get("final_price") is not None:
            b.row(t("Endpreis"), _eur(job["final_price"]), bold=True, vfill=ACCENT)
    if data["profit"] is not None:
        pct = f' ({fmt_num(data["profit"] / data["cost"] * 100, 0)} %)' if data["cost"] else ""
        b.row(t("Gewinn"), _eur(data["profit"]) + pct, vfill=OK if data["profit"] >= 0 else ACCENT)
    b.sub(f'{t("Summe")}: {data["due_src"]}')


# ---- PDF ----------------------------------------------------------------------

_PDF_FONTS: tuple[str, str] | None = None


def _pdf_fonts() -> tuple[str, str]:
    """DejaVu einbetten (Umlaute, €, ✓), sonst die Standardschrift Helvetica."""
    global _PDF_FONTS
    if _PDF_FONTS:
        return _PDF_FONTS
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    reg, bold = _font_path("sans"), _font_path("sans-b")
    if reg and bold:
        pdfmetrics.registerFont(TTFont("PL", str(reg)))
        pdfmetrics.registerFont(TTFont("PL-B", str(bold)))
        from reportlab.lib.fonts import addMapping
        addMapping("PL", 0, 0, "PL")
        addMapping("PL", 1, 0, "PL-B")
        _PDF_FONTS = ("PL", "PL-B")
    else:
        _PDF_FONTS = ("Helvetica", "Helvetica-Bold")
    return _PDF_FONTS


def report_pdf(data: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (Image as RLImage, KeepTogether, Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)
    from xml.sax.saxutils import escape

    font, bold = _pdf_fonts()
    job = data["job"]
    qty = data["qty"]
    ink = colors.HexColor("#24211d")
    muted = colors.HexColor("#7a7266")
    line = colors.HexColor("#ddd5c6")
    accent = colors.HexColor("#e86a12")
    soft = colors.HexColor("#f6f2ea")

    st_body = ParagraphStyle("b", fontName=font, fontSize=9.5, leading=13, textColor=ink)
    st_muted = ParagraphStyle("m", parent=st_body, textColor=muted, fontSize=8.5, leading=11.5)
    st_title = ParagraphStyle("t", parent=st_body, fontName=bold, fontSize=20, leading=24, spaceAfter=3)
    st_h = ParagraphStyle("h", parent=st_body, fontName=bold, fontSize=10, leading=13, textColor=accent,
                          spaceBefore=12, spaceAfter=5)
    st_r = ParagraphStyle("r", parent=st_body, alignment=2)
    st_rb = ParagraphStyle("rb", parent=st_r, fontName=bold)

    def P(text: Any, style: ParagraphStyle = st_body) -> Paragraph:
        return Paragraph(escape(str(text)), style)

    W_ = A4[0] - 36 * mm

    def table(rows: list[list[Any]], widths: list[float], head: bool = True, sum_rows: int = 0) -> Table:
        tbl = Table(rows, colWidths=widths, hAlign="LEFT")
        cmds = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5), ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (0, 0), (-1, -1 - sum_rows), 0.4, line)]
        if head:
            cmds += [("BACKGROUND", (0, 0), (-1, 0), soft), ("LINEBELOW", (0, 0), (-1, 0), 0.8, muted)]
        if sum_rows:
            cmds += [("LINEABOVE", (0, -sum_rows), (-1, -sum_rows), 0.9, ink)]
        tbl.setStyle(TableStyle(cmds))
        return tbl

    def H(text: str) -> list[Any]:
        return [P(text, st_h)]

    def hrow(*cells: str) -> list[Paragraph]:
        return [P(c, ParagraphStyle("hh", parent=st_muted, fontName=bold, alignment=2 if i and c and i == len(cells) - 1 else 0))
                for i, c in enumerate(cells)]

    story: list[Any] = []

    # Kopf: Schriftzug links, Foto rechts
    head_left: list[Any] = []
    try:
        logo = data["logo"]
        if logo.name == "logo-bon.png":
            lg = ImageReader(str(logo))
            iw, ih = lg.getSize()
            head_left.append(RLImage(str(logo), width=34 * mm, height=34 * mm * ih / iw, hAlign="LEFT"))
        else:
            with Image.open(logo) as src:
                iw, ih = ImageOps.exif_transpose(src).size
            scale = min(60 * mm / iw, 22 * mm / ih)  # höchstens 6 × 2,2 cm
            head_left.append(RLImage(str(logo), width=iw * scale, height=ih * scale, hAlign="LEFT"))
    except Exception:  # noqa: BLE001
        head_left.append(P("printl€g", st_title))
    head_left += [Spacer(1, 3 * mm), P(t("Kostenaufstellung · intern"), st_muted), Spacer(1, 6 * mm),
                  P(job.get("task_name") or t("Druck"), st_title)]
    meta = [t("Projekt {ref}", ref=data["ref"]) if data["project"] else t("Druck {ref}", ref=data["ref"]),
            _date(job.get("end_ts") or job.get("start_ts"))]
    if data["customer"]:
        meta.insert(0, t("Kunde: {name}", name=data["customer"]))
    head_left.append(P(" · ".join(meta), st_muted))
    if job.get("project_title") or job.get("project_url"):
        url = job.get("project_url") or ""
        label = escape(job.get("project_title") or url)
        link = (f'<a href="{escape(url)}" color="#e86a12">{label}</a>' if url.startswith(("http://", "https://")) else label)
        head_left.append(Paragraph(escape(t("Projekt")) + ": " + link, st_muted))
    head_right: Any = ""
    if data["photo"]:
        try:
            with Image.open(data["photo"]) as src:
                im = ImageOps.exif_transpose(src).convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            bg.alpha_composite(im)
            im = bg.convert("RGB")
            im.thumbnail((900, 900))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            buf.seek(0)
            side = 52 * mm
            scale = min(side / im.width, side / im.height)
            head_right = RLImage(buf, width=im.width * scale, height=im.height * scale, hAlign="RIGHT")
        except OSError:
            head_right = ""
    ht = Table([[head_left, head_right]], colWidths=[W_ - 56 * mm, 56 * mm])
    ht.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    story += [ht, Spacer(1, 4 * mm)]

    # Ergebnis-Kacheln
    def tile(label: str, value: str, sub: str = "", strong: bool = False) -> list[Paragraph]:
        return [P(label, st_muted),
                P(value, ParagraphStyle("tv", parent=st_body, fontName=bold, fontSize=15, leading=19,
                                        textColor=accent if strong else ink)),
                P(sub, st_muted)]
    due = data["due_calc"]
    tiles = [tile(t("Selbstkosten"), _eur(data["cost"]), t("{v} pro Stück", v=_eur(data["cost"] / qty)) if qty > 1 and data["cost"] is not None else t("für das ganze Projekt") if data["project"] else t("für den ganzen Druck")),
             tile(t("Geschenkt") if data["gift"] else t("Wert (keine Abrechnung)") if data["nobill"] else t("Zu zahlen"), _eur(0 if data["gift"] else due),
                  (t("Wert {v}", v=_eur(due)) if data["gift"] else data["due_src"]), True)]
    if data["profit"] is not None:
        pct = t("{p} % auf die Kosten", p=fmt_num(data["profit"] / data["cost"] * 100, 0)) if data["cost"] else ""
        tiles.append(tile(t("Gewinn"), _eur(data["profit"]), pct))
    if data["payable"]:
        tiles.append(tile(t("Bezahlt"), _eur(data["paid"]), t("alles bezahlt") if data["open"] < 0.005 else t("{v} offen", v=_eur(data["open"]))))
    tt = Table([tiles], colWidths=[W_ / len(tiles)] * len(tiles))
    tt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), soft), ("BOX", (0, 0), (-1, -1), 0.5, line),
                            ("LINEAFTER", (0, 0), (-2, -1), 0.5, line), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                            ("LEFTPADDING", (0, 0), (-1, -1), 9)]))
    story.append(tt)

    # Druck
    aborted = " " + t("bei {p} %", p=int(job["progress"])) if job.get("status") == "abgebrochen" and job.get("progress") is not None else ""
    facts = [(t("Drucker"), data["printer"]), (t("Status"), t(data["status"]) + aborted),
             (t("Start"), _date(job.get("start_ts"), "ts_long")), (t("Ende"), _date(job.get("end_ts"), "ts_long")),
             (t("Dauer"), fmt_dur(job.get("start_ts"), job.get("end_ts"))), (t("Energie"), fmt_num(job.get("kwh"), 3, "kWh")),
             (t("Filament"), fmt_num(data["grams"], 1, "g")), (t("Stückzahl"), t("{q} Stk.", q=qty))]
    if data["scrap"]:
        facts.append((t("Fehldruck"), data["defect"] or t("ja")))
        if data["customer"] and not data["nobill"]:
            facts.append((t("Berechnung"), data["charge"] or t("nicht berechnet")))
    if data["gift"]:
        facts.append((t("Abrechnung"), t("verschenkt")))
    elif data["nobill"]:
        facts.append((t("Abrechnung"), t("keine (Eigenbedarf)")))
    if data["project"]:
        facts = [(t("Drucker"), data["printer"]), (t("Status"), t(data["status"])),
                 (t("Start"), _date(job.get("start_ts"), "ts_long")), (t("Ende"), _date(job.get("end_ts"), "ts_long")),
                 (t("Druckzeit"), fmt_hours(data["hours"] or 0)), (t("Energie"), fmt_num(job.get("kwh"), 3, "kWh")),
                 (t("Filament"), fmt_num(data["grams"], 1, "g")), (t("Stückzahl"), t("{q} Stk.", q=qty))]
        if data["gift"]:
            facts.append((t("Abrechnung"), t("verschenkt")))
        elif data["nobill"]:
            facts.append((t("Abrechnung"), t("keine (Eigenbedarf)")))
    rows = []
    for i in range(0, len(facts), 2):
        pair = facts[i:i + 2] + [("", "")] * (2 - len(facts[i:i + 2]))
        rows.append([P(pair[0][0], st_muted), P(pair[0][1]), P(pair[1][0], st_muted), P(pair[1][1])])
    story += H(t("Projekt") if data["project"] else t("Druck")) + [table(rows, [24 * mm, W_ / 2 - 24 * mm, 24 * mm, W_ / 2 - 24 * mm], head=False)]

    if data["project"]:
        rows = [hrow(t("Druck"), t("Drucker"), t("Start"), t("Dauer"), t("Stück"), t("Kosten"))]
        for part in data["parts"]:
            name = escape(part["name"]) + f'<br/><font color="#7a7266" size="8.5">{escape(part["ref"])}'
            if part["scrap"]:
                name += " · " + escape(t("Fehldruck, nicht berechnet") if part["nocharge"] else t("Fehldruck – {x}", x=part["charge"]))
            name += "</font>"
            rows.append([Paragraph(name, st_body), P(part["printer"]), P(_date(part["start"], "ts") if part["start"] else "–"),
                         P(part["dur"], st_r), P(part["qty"], st_r), P(_eur(part["cost"]), st_r)])
        rows.append([P(t("Summe"), ParagraphStyle("ps", parent=st_body, fontName=bold)), P(""), P(""),
                     P(fmt_hours(data["hours"] or 0), st_rb), P(""), P(_eur(data["cost"]), st_rb)])
        story += H(t("Drucke im Projekt")) + [table(rows, [W_ - 118 * mm, 24 * mm, 30 * mm, 18 * mm, 16 * mm, 30 * mm], sum_rows=1)]

    # Material
    if data["materials"]:
        rows = [hrow(t("Slot"), t("Filament"), t("Typ"), t("Farbe"), t("Gramm"), "€/kg", t("Kosten"))]
        for m in data["materials"]:
            col = m.get("color") or ""
            swatch: Any = ""
            if re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", col):
                swatch = Table([[""]], colWidths=[5 * mm], rowHeights=[3.6 * mm])
                swatch.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), colors.HexColor(col[:7])),
                                            ("BOX", (0, 0), (0, 0), 0.4, line)]))
            fil = Paragraph(escape(m.get("name") or "?") + (f'<br/><font color="#7a7266" size="8.5">{escape(m["note"])}</font>' if m["note"] else ""), st_body)
            rows.append([P(t(m["slot"]) if m.get("slot") else "–"), fil, P(m.get("type") or "–"), swatch,
                         P(fmt_num(m["grams_used"], 1, "g"), st_r), P(fmt_num(m["eur_kg"], 2, "€") if m["eur_kg"] is not None else "–", st_r),
                         P(_eur(m["cost"]), st_r)])
        story += H(t("Material")) + [table(rows, [14 * mm, W_ - 116 * mm, 18 * mm, 14 * mm, 22 * mm, 22 * mm, 26 * mm])]

    # Kosten
    dur_h = ((job["end_ts"] - job["start_ts"]) / 3600) if job.get("start_ts") and job.get("end_ts") and job["end_ts"] > job["start_ts"] else None
    rows = [hrow(t("Posten"), t("Grundlage"), t("Betrag")),
            [P(t("Strom")), P(fmt_num(job.get("kwh"), 3, "kWh") if job.get("kwh") is not None else t("kein Zählerstand"), st_muted), P(_eur(job.get("cost")), st_r)],
            [P(t("Filament")), P(fmt_num(data["grams"], 1, "g") + (" " + t("(anteilig bis zum Abbruch)") if job.get("status") == "abgebrochen" else ""), st_muted), P(_eur(job.get("cost_filament")), st_r)],
            [P(t("Drucker")), P(t("Abschreibung + Wartung") + (f' · {fmt_dur(job["start_ts"], job["end_ts"])} × {fmt_num(data["rate"], 2, "€")}/h' if data["rate"] and dur_h else
                                                              f' · {fmt_hours(data["hours"] or 0)}' if data["project"] else ""), st_muted),
             P(_eur(job.get("cost_printer")), st_r)],
            [P(t("Selbstkosten"), ParagraphStyle("sb", parent=st_body, fontName=bold)),
             P(t("{v} pro Stück bei {q} Stück", v=_eur(data["cost"] / qty), q=qty) if qty > 1 and data["cost"] is not None else "", st_muted),
             P(_eur(data["cost"]), st_rb)]]
    if data["pbase"] is not None and data["cost"] is not None and abs(data["pbase"] - data["cost"]) >= 0.005:
        rows.append([P(t("Basis für Preise")), P(t("ohne unberechnete Fehldrucke"), st_muted), P(_eur(data["pbase"]), st_r)])
    story += H(t("Kosten")) + [table(rows, [34 * mm, W_ - 64 * mm, 30 * mm], sum_rows=2 if len(rows) > 5 else 1)]

    # Preise
    if data["prices"] or job.get("final_price") is not None:
        rows = [hrow(t("Preis"), t("Aufschlag"), t("pro Stück"), t("Gesamt"))]
        for p in data["prices"]:
            rows.append([P(p["name"]), P(f'+{fmt_input(p["pct"])} %', st_muted),
                         P(_eur(p["price"] / qty) if p["price"] is not None else "–", st_r), P(_eur(p["price"]), st_r)])
        if job.get("final_price") is not None:
            fp = job["final_price"]
            rows.append([P(t("Endpreis"), ParagraphStyle("eb", parent=st_body, fontName=bold)), P(t("fest vereinbart"), st_muted),
                         P(_eur(fp / qty), st_r), P(_eur(fp), st_rb)])
        story += H(t("Preise")) + [table(rows, [W_ - 92 * mm, 30 * mm, 30 * mm, 32 * mm],
                                      sum_rows=1 if job.get("final_price") is not None else 0)]

    # Zahlungen
    if data["customer"] and not data["nobill"] and not data["nocharge"] and data["status_shown"]:
        block: list[Any] = H(t("Zahlungen"))
        if data["gift"]:
            block.append(P(t("Verschenkt, keine Zahlung nötig."), st_muted))
        else:
            rows = [hrow(t("Datum"), t("Notiz"), t("Betrag"))]
            rows += [[P(_date(p["ts"])), P(p.get("note") or "", st_muted), P(_eur(p["amount"]), st_r)] for p in data["payments"]]
            if not data["payments"]:
                rows.append([P("–"), P(t("noch keine Zahlung"), st_muted), P("", st_r)])
            rows.append([P(t("Bezahlt"), ParagraphStyle("pb", parent=st_body, fontName=bold)), P(t("von {v}", v=_eur(due)), st_muted), P(_eur(data["paid"]), st_rb)])
            if data["open"] >= 0.005:
                rows.append([P(t("Offen"), ParagraphStyle("ob", parent=st_body, fontName=bold, textColor=accent)), P(""),
                             P(_eur(data["open"]), ParagraphStyle("orb", parent=st_rb, textColor=accent))])
            block.append(table(rows, [30 * mm, W_ - 60 * mm, 30 * mm], sum_rows=2 if data["open"] >= 0.005 else 1))
        story.append(KeepTogether(block))

    # Übergaben
    if data["show_handover"]:
        rows = [hrow(t("Datum"), t("Notiz"), t("Stück"))]
        rows += [[P(_date(h["ts"])), P(h.get("note") or "", st_muted), P(t("{q} Stk.", q=h["qty"]), st_r)] for h in data["handovers"]]
        if not data["handovers"]:
            rows.append([P("–"), P(t("noch nichts übergeben"), st_muted), P("", st_r)])
        d = data["delivered"]
        rows.append([P(t("Übergeben"), ParagraphStyle("ub", parent=st_body, fontName=bold)),
                     P(t("alles übergeben") if d >= qty else t("noch {k} offen", k=qty - d), st_muted), P(t("{d} von {n}", d=d, n=qty), st_rb)])
        story.append(KeepTogether(H(t("Übergaben")) + [table(rows, [30 * mm, W_ - 60 * mm, 30 * mm], sum_rows=1)]))

    # Notizen
    extra = []
    if job.get("job_note"):
        extra.append((t("Notiz"), job["job_note"]))
    if job.get("local_path"):
        extra.append((t("Ordner"), job["local_path"]))
    if job.get("note"):
        extra.append((t("Hinweis"), t_note(job["note"])))
    if extra:
        story += H(t("Notizen")) + [table([[P(k, st_muted), Paragraph(escape(v).replace("\n", "<br/>"), st_body)] for k, v in extra],
                                       [24 * mm, W_ - 24 * mm], head=False)]

    created = fmt_date(datetime.now(), "ts_long")
    foot_left = (t("printlog · Projekt {ref} · erstellt am {when}", ref=data["ref"], when=created) if data["project"]
                 else t("printlog · Druck {ref} · erstellt am {when}", ref=data["ref"], when=created))
    page_label = t("Seite")

    def footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(18 * mm, 10 * mm, foot_left)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"{page_label} {doc.page}")
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.4)
        canvas.line(18 * mm, 13.5 * mm, A4[0] - 18 * mm, 13.5 * mm)
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=20 * mm, title=f'{job.get("task_name") or t("Druck")} – {t("Kostenaufstellung")}',
                            author="printlog", creator="printlog", subject=t("Druck {ref}", ref=data["ref"]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
