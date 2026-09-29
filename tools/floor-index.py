#!/usr/bin/env python3
"""Bootstrap the floor files of a new project from the PDF's OCG layers.

    uv run tools/floor-index.py [F ...] [--margin 12] [--force]

For every floor page (config "pages": first..last, first_floor) writes
WORK/plan-studio/data/floor-F.json with the fields the other tools read:

    floor, page (1-based), bbox [x0, y0, x1, y1] in PDF pt, w, h,
    units {}  (filled by plan-units-from-pdf.py),
    texts [{x, y, size, str}]  (floor-local pt, used as area labels when matching units)

The bbox is the union of every object on the kept layers and the units layer (layer config), plus a
margin, clipped to the page. Nothing is classified by color.
The original project got page, bbox and texts from export-plan-vectors.py, which found floors through
ArchiCAD clip paths and a unit list; this tool is the layer-based replacement for new projects.
Also writes WORK/plan-studio/data/index.json ({floors, files}).
Existing floor files are kept (units and extra fields survive) unless --force.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

DATA = fpconfig.WORK / "plan-studio" / "data"


def floor_bbox(page, layers, margin):
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    for dr in page.get_drawings():
        if dr.get("layer") not in layers:
            continue
        r = dr["rect"]
        x0, y0, x1, y1 = min(x0, r.x0), min(y0, r.y0), max(x1, r.x1), max(y1, r.y1)
    if x0 == float("inf"):
        return None
    pr = page.rect
    return [round(max(pr.x0, x0 - margin), 2), round(max(pr.y0, y0 - margin), 2),
            round(min(pr.x1, x1 + margin), 2), round(min(pr.y1, y1 + margin), 2)]


def page_texts(page, bbox):
    out = []
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]:
            out.append({"x": round(cx - bbox[0], 2), "y": round(cy - bbox[1], 2),
                        "size": round(y1 - y0, 2), "str": word})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("floors", nargs="*", type=int)
    ap.add_argument("--margin", type=float, default=12.0, help="pt around the drawn floor")
    ap.add_argument("--force", action="store_true", help="rewrite existing floor files from scratch")
    args = ap.parse_args()

    cfg = fpconfig.layers()
    layers = {r["layer"] for r in cfg["keep"]} | {cfg["units"]["layer"]}
    if not fpconfig.PDF.exists():
        sys.exit(f"PDF not found: {fpconfig.PDF} (set \"pdf\" in {fpconfig.CONFIG_PATH})")
    doc = pymupdf.open(fpconfig.PDF)
    floors = args.floors or [fpconfig.floor_for_page(p) for p in range(fpconfig.PAGE_FIRST, fpconfig.PAGE_LAST + 1)]
    DATA.mkdir(parents=True, exist_ok=True)

    done = []
    for F in floors:
        pno = fpconfig.page_for_floor(F)
        if not 1 <= pno <= len(doc):
            print(f"floor {F}: page {pno} is outside the PDF ({len(doc)} pages)")
            continue
        page = doc[pno - 1]
        bbox = floor_bbox(page, layers, args.margin)
        if bbox is None:
            print(f"floor {F}: page {pno} has none of the configured layers - check names with plan-pdf-layers.py --inventory")
            continue
        path = DATA / f"floor-{F}.json"
        fd = {} if args.force or not path.exists() else json.loads(path.read_text(encoding="utf-8"))
        fd.update({"floor": F, "page": pno, "bbox": bbox,
                   "w": round(bbox[2] - bbox[0], 2), "h": round(bbox[3] - bbox[1], 2),
                   "texts": page_texts(page, bbox)})
        fd.setdefault("units", {})
        path.write_text(json.dumps(fd, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        done.append(F)
        print(f"floor {F}: page {pno}, bbox {bbox}, {fd['w']:.0f} x {fd['h']:.0f} pt, "
              f"{len(fd['texts'])} words -> {path}")

    idx_path = DATA / "index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {}
    known = sorted(set(idx.get("floors", [])) | set(done))
    idx["floors"] = known
    idx["files"] = {str(F): f"floor-{F}.json" for F in known}
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
