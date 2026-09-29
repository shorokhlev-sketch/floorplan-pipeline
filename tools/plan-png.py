#!/usr/bin/env python3
"""План v3: SVG (plan-studio/v3/out/) → PNG через Chromium (rasterize_svg из trace-plans) + лист сравнения.

Usage: .venv/bin/python tools/plan-png.py [unit ...] [--compare] [--scale 3.2]
  --compare  для каждой квартиры собрать plan-studio/v3/out/compare-N.png:
             PDF архитектора (plan-studio/truth/unit-N-pdf.png) | старый рендер (editor-data/unit-N.png) | новый
"""
import importlib.util, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
OUT = ROOT / "plan-studio/v3/out"
PLANS = ROOT / "plan-studio/v3/plans"

_spec = importlib.util.spec_from_file_location("trace_plans", fpconfig.CODE / "tools/trace-plans.py")
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)


def main():
    args = sys.argv[1:]
    scale = 3.2
    if "--scale" in args:
        scale = float(args[args.index("--scale") + 1])
    units = [a for a in args if not a.startswith("--") and not a.replace(".", "").isdigit() or (a.isdigit() and len(a) >= 3)]
    units = [a for a in args if a.isdigit() and len(a) >= 3]
    if not units:
        units = [u["unit"] for u in json.loads((PLANS / "index.json").read_text())["units"]]
    for u in units:
        for suffix in ("", "-walls"):
            svg = OUT / f"unit-{u}{suffix}.svg"
            if svg.exists():
                tp.rasterize_svg(svg.read_text(), scale, OUT / f"unit-{u}{suffix}.png")
        print(u, "png ok")
    tp.close_rasterizer()

    if "--compare" in args:
        from PIL import Image, ImageDraw
        for u in units:
            cols = []
            for label, path in (("PDF архитектора", ROOT / f"plan-studio/truth/unit-{u}-pdf.png"),
                                ("старый рендер", ROOT / f"site/assets/plans/unit-{u}.png"),
                                ("v3 без мебели", OUT / f"unit-{u}-walls.png"),
                                ("v3", OUT / f"unit-{u}.png")):
                if path.exists():
                    cols.append((label, Image.open(path).convert("RGB")))
            if not cols:
                continue
            H = max(im.height for _, im in cols)
            ims = []
            for label, im in cols:
                if im.height != H:
                    im = im.resize((round(im.width * H / im.height), H), Image.LANCZOS)
                ims.append((label, im))
            pad, top = 16, 40
            W = sum(im.width for _, im in ims) + pad * (len(ims) + 1)
            sheet = Image.new("RGB", (W, H + top + pad), "#F4F4F4")
            d = ImageDraw.Draw(sheet)
            x = pad
            for label, im in ims:
                d.text((x, 12), f"{u}: {label}", fill="#182E46")
                sheet.paste(im, (x, top))
                x += im.width + pad
            sheet.save(OUT / f"compare-{u}.png")
            print(u, "compare ok")


if __name__ == "__main__":
    main()
