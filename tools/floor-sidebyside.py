#!/usr/bin/env python
"""Side-by-side печать: слева страница PDF архитектора (кроп по bbox этажа), справа наш рендер.
Одна страница A3-альбом на этаж, все этажи из plan-studio/data/index.json → один PDF.
Использование: .venv/bin/python tools/floor-sidebyside.py [--out <pdf>] [--floors 2,3,20] [--plans-dir <dir>]

--plans-dir: откуда брать floor-F.png нашего рендера (по умолчанию site/assets/plans -
прод); для экспериментов (--source trace и т.п.) указать каталог прогона, напр.
plan-studio/out-v4/trace21/."""
import argparse, json, re
from pathlib import Path
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
DATA = ROOT / "plan-studio" / "data"
PDF = fpconfig.PDF

ap = argparse.ArgumentParser()
ap.add_argument("--out", default=str(ROOT / "plan-studio" / "out-v4" / "floors-side-by-side.pdf"))
ap.add_argument("--floors", default=None)
ap.add_argument("--plans-dir", default=str(ROOT / "site" / "assets" / "plans"),
                 help="каталог с floor-F.png нашего рендера (по умолчанию прод-каталог)")
a = ap.parse_args()
PLANS = Path(a.plans_dir)
floors = [int(x) for x in a.floors.split(",")] if a.floors else json.loads((DATA / "index.json").read_text())["floors"]
doc = fitz.open(str(PDF))
W = 4800                    # ширина листа; высота подбирается под картинки (экранный просмотр, не A3)
PAD, GAP, HEAD = 60, 60, 140
pages = []
try:
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 64)
    small = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 40)
except Exception:
    font = small = ImageFont.load_default()
for f in floors:
    fd = json.loads((DATA / f"floor-{f}.json").read_text())
    x0, y0, x1, y1 = fd["bbox"]
    pg = doc[fd["page"] - 1]
    pix = pg.get_pixmap(matrix=fitz.Matrix(3, 3), clip=fitz.Rect(x0, y0, x1, y1))
    pdf_im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    our = Image.open(PLANS / f"floor-{f}.png").convert("RGB")
    colw = (W - 2 * PAD - GAP) // 2
    def fit(im):
        s = colw / im.width
        return im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
    L, R = fit(pdf_im), fit(our)
    H = max(L.height, R.height) + HEAD + 2 * PAD
    page = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(page)
    d.text((PAD, PAD), f"Этаж {f}", fill="black", font=font)
    d.text((PAD, PAD + 80), f"PDF архитектора, стр. {fd['page']}", fill="#555", font=small)
    d.text((PAD + colw + GAP, PAD + 80), f"Наш рендер ({PLANS})", fill="#555", font=small)
    page.paste(L, (PAD, PAD + HEAD)); page.paste(R, (PAD + colw + GAP, PAD + HEAD))
    pages.append(page)
    print("этаж", f, "pdf", pdf_im.size, "our", our.size)
out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
pages[0].save(str(out), "PDF", resolution=300, save_all=True, append_images=pages[1:])
print("→", out, len(pages), "страниц")
