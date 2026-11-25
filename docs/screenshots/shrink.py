"""Screenshots für die README zuschneiden, verkleinern und auf 256 Farben reduzieren (siehe capture.js)."""
from PIL import Image, ImageChops
import sys, os
src, dst = sys.argv[1], sys.argv[2]
os.makedirs(dst, exist_ok=True)
def trim_bottom(im, pad=16):
    bg = im.getpixel((im.width-2, im.height-2))
    diff = ImageChops.difference(im, Image.new('RGB', im.size, bg)).getbbox()
    if diff: im = im.crop((0, 0, im.width, min(im.height, diff[3]+pad)))
    return im
def save(im, name, colors=256):
    q = im.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
    q.save(f'{dst}/{name}.png', optimize=True)
desk = ['prints','prints-filtered','gallery','open-items','print-detail','print-billing','statistics','calendar','customers']
for n in desk:
    im = Image.open(f'{src}/{n}.png').convert('RGB')
    if n in ('statistics','calendar','customers','print-billing','print-detail'): im = trim_bottom(im)
    save(im, n)
for n in ['failed-print', 'failed-print-stats', 'project-list', 'project-detail', 'assign-customer']:
    if os.path.exists(f'{src}/{n}.png'):
        im = Image.open(f'{src}/{n}.png').convert('RGB')
        save(trim_bottom(im) if n == 'project-detail' else im, n)
for n in ['phone-list','phone-detail','phone-open']:
    im = Image.open(f'{src}/{n}.png').convert('RGB'); im = im.resize((585, int(im.height*585/im.width)), Image.LANCZOS); save(im, n)
for a,b in [('raw-bon-kunde','receipt-customer'),('raw-bon-intern','receipt-internal'),('raw-cost','cost-sheet')]:
    if dst.endswith('-dark') or not os.path.exists(f'{src}/{a}.png'):
        continue  # Bon und PDF sehen in beiden Farbschemata gleich aus
    im = Image.open(f'{src}/{a}.png').convert('RGB')
    if im.width > 600: im = im.resize((600, int(im.height*600/im.width)), Image.LANCZOS)
    save(im, b)
