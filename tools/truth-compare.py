#!/usr/bin/env python3
"""Ground truth for unit plans: crop the architect's PDF page around each unit and put it side by side
with our walls-only render (plan-studio/editor-data/unit-N.png) and our full crop
(site/assets/plans/unit-N.png).

Usage: .venv/bin/python tools/truth-compare.py [unit ...]      (default: the 21 type representatives + 1005)
Writes plan-studio/truth/unit-N-pdf.png and plan-studio/truth/compare-N.png, plus truth-sheet.png (all rows).
Coordinates: plan-studio/data/floor-F.json coords = PDF page coords minus floor bbox origin (bbox[0], bbox[1]).
"""
import json, sys
from pathlib import Path
import pymupdf
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
PDF = fpconfig.PDF
OUT = ROOT / "plan-studio/truth"; OUT.mkdir(parents=True, exist_ok=True)
REPS = ["202", "204", "307", "308", "309", "310", "1311", "208", "1510", "201", "209", "1709", "2308",
        "203", "205", "206", "207", "1201", "2201", "2203", "2204", "1005"]
S = 4.0  # PDF raster scale


def font(sz):
    try:
        return ImageFont.truetype("/System/Library/Fonts/Supplemental/Georgia.ttf", sz)
    except Exception:
        return ImageFont.load_default()


def truth_crop(doc, n, margin=14.0):
    fl = int(n[:-2]); d = json.load(open(ROOT / f"plan-studio/data/floor-{fl}.json"))
    u = d["units"][n]; polys = [u["poly"]] + ([u["balcony"]] if u.get("balcony") else [])
    xs = [p[0] for pl in polys for p in pl]; ys = [p[1] for pl in polys for p in pl]
    bx, by = d["bbox"][0], d["bbox"][1]
    clip = pymupdf.Rect(min(xs) - margin + bx, min(ys) - margin + by, max(xs) + margin + bx, max(ys) + margin + by)
    page = doc[d["page"] - 1]
    pix = page.get_pixmap(matrix=pymupdf.Matrix(S, S), clip=clip, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def main(units):
    doc = pymupdf.open(str(PDF)); rows = []
    for n in units:
        truth = truth_crop(doc, n); truth.save(OUT / f"unit-{n}-pdf.png")
        panels = [(truth, f"{n}: PDF архитектора")]
        for p, label in ((ROOT / f"plan-studio/editor-data/unit-{n}.png", "наши стены (editor-data)"),
                         (ROOT / f"site/assets/plans/unit-{n}.png", "наш кроп с мебелью")):
            if p.exists():
                panels.append((Image.open(p).convert("RGB"), label))
        H = 520
        ims = [(im.resize((round(im.width * H / im.height), H), Image.LANCZOS), lb) for im, lb in panels]
        W = sum(im.width for im, _ in ims) + 40 * (len(ims) - 1)
        row = Image.new("RGB", (W, H + 50), "white"); dr = ImageDraw.Draw(row); x = 0
        for im, lb in ims:
            row.paste(im, (x, 50)); dr.text((x, 8), lb, fill="#182E46", font=font(26)); x += im.width + 40
        row.save(OUT / f"compare-{n}.png"); rows.append(row)
    W = max(r.width for r in rows); sheet = Image.new("RGB", (W, sum(r.height + 30 for r in rows)), "white"); y = 0
    for r in rows:
        sheet.paste(r, (0, y)); y += r.height + 30
    sheet.save(OUT / "truth-sheet.png"); print(OUT / "truth-sheet.png", sheet.size)


if __name__ == "__main__":
    main(sys.argv[1:] or REPS)
