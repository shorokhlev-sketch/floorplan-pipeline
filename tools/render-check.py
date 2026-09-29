#!/usr/bin/env python3
"""Render check: every script run is looked at as a picture before it goes to Figma or the site.

    uv run tools/render-check.py F [F ...] [--units] [--svg PATH] [--out DIR] [--width 1600]

For floor F renders WORK/plan-studio/v3/figma/layers-F.svg (or --svg) to PNG with headless Chromium
(Playwright) and prints the time and the share of ink pixels. With --units it first writes
floor-F-check.svg: the layer drawing plus the unit and balcony polygons from floor-F.json, tinted,
with the unit number and area (m2) at each unit's centroid; this is the picture that shows whether
the outlines from the PDF sit on the walls.

Bugs this catches in seconds and logs never show: white columns on a white page, a slab outline
filled black because fill="none" was missing, chained segments joined in the wrong direction.
Exit code 1 when a PNG is blank (no ink).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

WORK = fpconfig.WORK
K = fpconfig.CM_PER_PT
TINT = ["#E4DACD", "#A9BBC8", "#D6E1EA", "#EDE3D3", "#CFE0D2"]


def shoelace_m2(pts):
    s = 0.0
    for i in range(len(pts)):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % len(pts)]
        s += ax * by - bx * ay
    return abs(s) / 2.0 * fpconfig.M_PER_PT ** 2


def units_overlay_svg(F: int, layers_svg: Path) -> Path:
    fd = json.loads((WORK / f"plan-studio/data/floor-{F}.json").read_text(encoding="utf-8"))
    body = layers_svg.read_text(encoding="utf-8")
    head_end = body.index(">", body.index("<svg")) + 1
    W, H = fd["w"] * K, fd["h"] * K
    pts = lambda poly: " ".join(f"{x * K:.1f},{y * K:.1f}" for x, y in poly)
    fills, labels = [], []
    for i, (n, u) in enumerate(sorted(fd.get("units", {}).items())):
        if not u.get("poly"):
            continue
        tint = TINT[i % len(TINT)]
        fills.append(f'<polygon id="unit {n}" points="{pts(u["poly"])}" fill="{tint}"/>')
        for b in u.get("balconies") or ([u["balcony"]] if u.get("balcony") else []):
            fills.append(f'<polygon id="balcony {n}" points="{pts(b)}" fill="{tint}" fill-opacity="0.55"/>')
        cx = sum(p[0] for p in u["poly"]) / len(u["poly"]) * K
        cy = sum(p[1] for p in u["poly"]) / len(u["poly"]) * K
        labels.append(f'<text x="{cx:.0f}" y="{cy:.0f}" font-family="Helvetica, Arial, sans-serif" font-size="{K * 7:.0f}" '
                      f'text-anchor="middle" fill="#182E46">{n} · {shoelace_m2(u["poly"]):.1f} m²</text>')
    out = layers_svg.with_name(f"floor-{F}-check.svg")
    svg = (body[:head_end] + f'\n<rect width="{W:.1f}" height="{H:.1f}" fill="#FFFFFF"/>\n<g id="units">' + "".join(fills)
           + "</g>\n" + body[head_end:body.rindex("</svg>")] + '<g id="labels">' + "".join(labels) + "</g>\n</svg>\n")
    out.write_text(svg, encoding="utf-8")
    return out


async def rasterize(jobs: list[tuple[Path, Path]], width: int) -> list[tuple[Path, float, float]]:
    from playwright.async_api import async_playwright
    from PIL import Image
    import numpy as np

    res = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        for svg, png in jobs:
            t0 = time.time()
            text = svg.read_text(encoding="utf-8")
            import re
            m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', text)
            vw, vh = (float(m.group(1)), float(m.group(2))) if m else (width, width)
            h = max(1, round(width * vh / vw))
            await page.set_viewport_size({"width": width, "height": h})
            await page.set_content(f'<html><body style="margin:0;background:#fff">{_inline(text, width, h)}</body></html>')
            await page.screenshot(path=str(png), full_page=False)
            dt = time.time() - t0
            a = np.asarray(Image.open(png).convert("L"))
            ink = float((a < 128).mean())
            res.append((png, dt, ink))
        await browser.close()
    return res


def _inline(svg_text: str, w: int, h: int) -> str:
    """inline <svg> scaled to the viewport (keeps the viewBox, sets the pixel size)"""
    import re
    s = svg_text[svg_text.index("<svg"):]
    s = re.sub(r'\swidth="[^"]*"', "", s, count=1)
    s = re.sub(r'\sheight="[^"]*"', "", s, count=1)
    return s.replace("<svg", f'<svg width="{w}" height="{h}"', 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("floors", nargs="*", type=int)
    ap.add_argument("--svg", help="render this SVG instead of layers-F.svg (one floor)")
    ap.add_argument("--units", action="store_true", help="overlay unit and balcony polygons from floor-F.json")
    ap.add_argument("--out", help="output dir (default: next to the SVG)")
    ap.add_argument("--width", type=int, default=1600)
    args = ap.parse_args()
    if not args.floors and not args.svg:
        ap.error("give floor numbers or --svg")

    jobs = []
    for F in args.floors or [None]:
        svg = Path(args.svg) if args.svg else WORK / f"plan-studio/v3/figma/layers-{F}.svg"
        if args.units and F is not None:
            svg = units_overlay_svg(F, svg)
        out_dir = Path(args.out) if args.out else svg.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        jobs.append((svg, out_dir / (svg.stem + ".png")))

    t0 = time.time()
    res = asyncio.run(rasterize(jobs, args.width))
    blank = 0
    for png, dt, ink in res:
        print(f"{png}: {dt:.2f} s, ink {ink * 100:.1f} % of pixels")
        blank += ink <= 0.0005
    print(f"render check: {len(res)} image(s) in {time.time() - t0:.1f} s")
    sys.exit(1 if blank else 0)


if __name__ == "__main__":
    main()
