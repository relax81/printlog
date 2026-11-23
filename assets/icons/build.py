"""Erzeugt die finalen Icon-/Logo-SVGs (Entwurf A = App-Icon, Entwurf D = Schriftzug).

Aufruf: python assets/icons/build.py  (braucht fonttools + Inter Display Black)
PNGs danach mit: node assets/icons/render.js
"""
import math
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "static"
SRC = Path(__file__).resolve().parent
FONT = "/usr/share/fonts/opentype/inter/InterDisplay-Black.otf"
CREAM, ORANGE, BG = "#f4eee3", "#ff7a1a", "#16181f"

DEFS = """<linearGradient id="bgg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#20232c"/><stop offset="1" stop-color="#16181f"/></linearGradient>
<linearGradient id="brass" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#e9b45a"/><stop offset=".5" stop-color="#ffd98a"/><stop offset="1" stop-color="#c98a2c"/></linearGradient>
<linearGradient id="block" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#d9dde5"/><stop offset="1" stop-color="#9aa1ae"/></linearGradient>"""


def nozzle(x, tipy, s):
    return (f'<g transform="translate({x},{tipy-140*s:.1f}) scale({s})">'
            '<rect x="-70" y="0" width="140" height="66" rx="14" fill="url(#block)"/>'
            '<rect x="-70" y="22" width="140" height="8" fill="#7d8492" opacity=".5"/>'
            '<path d="M-46 66 H46 V100 H-46Z" fill="url(#brass)"/>'
            '<path d="M-30 66 V100 M30 66 V100" stroke="#b07a22" stroke-width="5"/>'
            '<path d="M-36 100 H36 L12 136 Q0 142 -12 136Z" fill="url(#brass)"/></g>')


def euro(cx, cy, r, sw, color, a=48):
    t = math.radians(a)
    x, y1, y2 = cx + r * math.cos(t), cy - r * math.sin(t), cy + r * math.sin(t)
    p = (f'<path d="M{x:.1f} {y1:.1f} A{r} {r} 0 1 0 {x:.1f} {y2:.1f}" fill="none" '
         f'stroke="{color}" stroke-width="{sw}" stroke-linecap="round"/>')
    for dy in (-r * 0.2, r * 0.2):
        p += (f'<path d="M{cx-r*1.32:.1f} {cy+dy:.1f} H{cx+r*0.28:.1f}" stroke="{color}" '
              f'stroke-width="{sw*0.72:.1f}" stroke-linecap="round"/>')
    return p


def bon():
    n, w = 7, 212 / 7
    zz = " ".join(f"L{366-i*w:.1f} {430 if i % 2 == 0 else 410}" for i in range(n + 1))
    return f'''<path d="M150 176 H362 V430 {zz} L150 430 Z" fill="{CREAM}"/>
<rect x="150" y="176" width="212" height="10" fill="#d9d0c0"/>
<g stroke="#b9b0a2" stroke-width="12" stroke-linecap="round"><path d="M182 220 H262"/><path d="M302 220 H330"/><path d="M182 252 H240"/><path d="M302 252 H330"/></g>
<path d="M182 284 H330" stroke="#d2c9ba" stroke-width="4" stroke-dasharray="8 8"/>
{euro(262, 345, 40, 18, ORANGE)}
<path d="M256 150 V176" stroke="{ORANGE}" stroke-width="14" stroke-linecap="round"/>
{nozzle(256, 152, 0.78)}'''


def icon(maskable=False):
    # maskable/apple: vollflächiger Hintergrund, Motiv in die sichere Zone (80 %) verkleinert
    bg = '<rect width="512" height="512" fill="url(#bgg)"/>' if maskable else \
         '<rect width="512" height="512" rx="112" fill="url(#bgg)"/>'
    body = bon()
    if maskable:
        body = f'<g transform="translate(256 290) scale(.82) translate(-256 -290)">{body}</g>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">'
            f'<defs>{DEFS}</defs>{bg}{body}</svg>\n')


def wordmark():
    """Einzeiliger Schriftzug „printl€g“: Text in currentColor, das o ist eine €-Münze."""
    f = TTFont(FONT)
    gs, cmap = f.getGlyphSet(), f.getBestCmap()
    upm = f["head"].unitsPerEm
    xh = f["OS/2"].sxHeight
    size, track = 100, -4  # px, Laufweite
    k = size / upm
    x, paths = 0.0, []

    def glyph(ch):
        nonlocal x
        name = cmap[ord(ch)]
        pen = SVGPathPen(gs)
        gs[name].draw(TransformPen(pen, (k, 0, 0, -k, x, 100)))
        paths.append(pen.getCommands())
        x += gs[name].width * k + track

    for ch in "printl":
        glyph(ch)
    o_w = gs[cmap[ord("o")]].width * k
    r = xh * k / 2 * 1.12
    cx, cy = x + o_w / 2, 100 - xh * k / 2
    coin = f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{ORANGE}"/>' + \
        euro(round(cx + r * 0.1, 1), round(cy, 1), round(r * 0.55, 1), round(r * 0.22, 1), "#16181f")
    x += o_w + track
    glyph("g")
    h = 100 + 24  # Unterlänge vom g
    top = 100 - f["OS/2"].sCapHeight * k - 4
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 {top:.0f} {x:.0f} {h-top:.0f}" '
            f'role="img" aria-label="Printlog"><path fill="currentColor" d="{" ".join(paths)}"/>{coin}</svg>\n')


if __name__ == "__main__":
    (STATIC / "icon.svg").write_text(icon())
    (SRC / "icon-maskable.svg").write_text(icon(True))
    (STATIC / "logo.svg").write_text(wordmark())
    print("ok")
