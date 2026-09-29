#!/usr/bin/env python3
"""
export-plan-vectors.py

Выгружает векторную графику планов этажей (страницы config "pages" = этажи, в исходном проекте 82..106 = этажи 2..26
архитектурного PDF) в JSON для веб-студии стилизации планов:

  $ROOT/plan-studio/data/floor-<N>.json   - один файл на этаж
  $ROOT/plan-studio/data/index.json       - сводка кластеров по всему дому
  $ROOT/plan-studio/data/check-floor-10.png - контрольный оверлей (этаж 10)

Переиспользует из tools/extract-plans.py (без изменения файла):
  - соответствие страница -> этаж, PAGE_FIRST/PAGE_LAST, FLOOR_MARGIN_PT
  - match_floor() / match_balconies() - сопоставление units.json с полигонами
    квартир/балконов на странице (в pt PDF)
  - bbox_of_points() - bbox плана этажа = объединение полигонов квартир + поле

Ничего не меняет в extract-plans.py, plans.json, файлах сайта.
"""

import importlib.util
import json
import sys
import time
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
TOOLS_DIR = fpconfig.CODE / "tools"
OUT_DIR = ROOT / "plan-studio" / "data"
CHECK_FLOOR = 10

# ---------------------------------------------------------------- import extract-plans.py

_spec = importlib.util.spec_from_file_location(
    "extract_plans", str(TOOLS_DIR / "extract-plans.py")
)
ep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ep)  # type: ignore[union-attr]

PDF_PATH = ep.PDF_PATH
UNITS_PATH = ep.UNITS_PATH
PAGE_FIRST = ep.PAGE_FIRST
PAGE_LAST = ep.PAGE_LAST
FLOOR_MARGIN_PT = ep.FLOOR_MARGIN_PT
match_floor = ep.match_floor
match_balconies = ep.match_balconies
bbox_of_points = ep.bbox_of_points


# ---------------------------------------------------------------- color helpers

def to_hex(rgb):
    if rgb is None:
        return None
    r, g, b = rgb
    return "#%02x%02x%02x" % (
        max(0, min(255, round(r * 255))),
        max(0, min(255, round(g * 255))),
        max(0, min(255, round(b * 255))),
    )


def cluster_key(d):
    stroke = to_hex(d.get("color"))
    fill = to_hex(d.get("fill"))
    width = round(d.get("width") or 0.0, 1)
    return (stroke, fill, width)


# ---------------------------------------------------------------- path building

def build_path(items, dx, dy):
    """Собрать один SVG path из items одного drawing, переводя координаты
    на (dx, dy) = (-bbox.x0, -bbox.y0) и округляя до 0.1 pt."""
    parts = []
    last = None

    def pt(p):
        return (round(p.x + dx, 1), round(p.y + dy, 1))

    for it in items:
        kind = it[0]
        if kind == "l":
            p1, p2 = pt(it[1]), pt(it[2])
            if last == p1:
                parts.append(f"L {p2[0]} {p2[1]}")
            else:
                parts.append(f"M {p1[0]} {p1[1]} L {p2[0]} {p2[1]}")
            last = p2
        elif kind == "c":
            p1, p2, p3, p4 = pt(it[1]), pt(it[2]), pt(it[3]), pt(it[4])
            if last == p1:
                parts.append(f"C {p2[0]} {p2[1]} {p3[0]} {p3[1]} {p4[0]} {p4[1]}")
            else:
                parts.append(
                    f"M {p1[0]} {p1[1]} C {p2[0]} {p2[1]} {p3[0]} {p3[1]} {p4[0]} {p4[1]}"
                )
            last = p4
        elif kind == "re":
            rect = it[1]
            x0, y0 = round(rect.x0 + dx, 1), round(rect.y0 + dy, 1)
            w = round(rect.width, 1)
            h = round(rect.height, 1)
            parts.append(f"M {x0} {y0} h {w} v {h} h {-w} Z")
            last = None
        elif kind == "qu":
            quad = it[1]
            p1, p2, p3, p4 = pt(quad.ul), pt(quad.ur), pt(quad.lr), pt(quad.ll)
            parts.append(
                f"M {p1[0]} {p1[1]} L {p2[0]} {p2[1]} L {p3[0]} {p3[1]} L {p4[0]} {p4[1]} Z"
            )
            last = None
        # unknown item kinds are skipped (none observed in this PDF)
    return " ".join(parts)


# ---------------------------------------------------------------- main

def main():
    # --only-floor N: compute the full global cluster-id assignment exactly as before (so ids
    # stay identical to the existing index.json), but only write out floor-N.json — used to
    # add the "seq" field to one floor without touching index.json or any other floor file.
    only_floor = None
    if "--only-floor" in sys.argv:
        i = sys.argv.index("--only-floor")
        only_floor = int(sys.argv[i + 1])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    units_data = json.loads(UNITS_PATH.read_text())
    all_units = units_data["units"]

    doc = fitz.open(str(PDF_PATH))

    # per floor: {"floor", "page", "bbox", "w", "h", "units", "texts",
    #             "raw_clusters": {key: {"n": int, "paths": [...]}}}
    floor_data = {}
    global_n_total = {}  # key -> total n across all floors

    notes_all = []
    ambiguous_all = []

    for page_no in range(PAGE_FIRST, PAGE_LAST + 1):
        floor_no = fpconfig.floor_for_page(page_no)
        page = doc[page_no - 1]
        floor_units = [u for u in all_units if u["floor"] == floor_no]
        if not floor_units:
            continue

        notes = []
        ambiguous = []
        matched, missing = match_floor(page, floor_units, floor_no, notes, ambiguous)
        if not matched:
            notes_all.append(f"floor {floor_no}: NO units matched, skipped")
            continue
        balcony_polys = match_balconies(page, floor_units, matched, floor_no, ambiguous, notes)
        notes_all.extend(notes)
        ambiguous_all.extend(ambiguous)

        all_poly_pts = [c["pts"] for c in matched.values()]
        union_bbox = bbox_of_points(all_poly_pts)
        bbox = fitz.Rect(
            union_bbox.x0 - FLOOR_MARGIN_PT,
            union_bbox.y0 - FLOOR_MARGIN_PT,
            union_bbox.x1 + FLOOR_MARGIN_PT,
            union_bbox.y1 + FLOOR_MARGIN_PT,
        )
        dx, dy = -bbox.x0, -bbox.y0

        def tr(pts):
            return [[round(x + dx, 1), round(y + dy, 1)] for (x, y) in pts]

        units_out = {}
        for number, c in matched.items():
            units_out[number] = {
                "poly": tr(c["pts"]),
                "balcony": tr(balcony_polys[number]) if number in balcony_polys else None,
            }

        # --- vector drawings within bbox, grouped by (stroke, fill, width)
        drawings = page.get_drawings()
        raw_clusters = {}
        for d_idx, d in enumerate(drawings):
            rect = d.get("rect")
            if rect is None or not rect.intersects(bbox):
                continue
            key = cluster_key(d)
            path = build_path(d.get("items", []), dx, dy)
            if not path:
                continue
            entry = raw_clusters.setdefault(key, {"n": 0, "paths": [], "seq": []})
            entry["n"] += 1
            entry["paths"].append(path)
            entry["seq"].append(d_idx)  # original PDF drawing order, for z-order-correct re-render
            global_n_total[key] = global_n_total.get(key, 0) + 1

        # --- texts within bbox
        texts = []
        for w in page.get_text("words"):
            x0, y0, x1, y1, wtext = w[0], w[1], w[2], w[3], w[4]
            wrect = fitz.Rect(x0, y0, x1, y1)
            if not wrect.intersects(bbox):
                continue
            texts.append(
                {
                    "x": round(x0 + dx, 1),
                    "y": round(y0 + dy, 1),
                    "size": round(y1 - y0, 1),
                    "str": wtext,
                }
            )

        floor_data[floor_no] = {
            "floor": floor_no,
            "page": page_no,
            "bbox": [round(bbox.x0, 1), round(bbox.y0, 1), round(bbox.x1, 1), round(bbox.y1, 1)],
            "w": round(bbox.width, 1),
            "h": round(bbox.height, 1),
            "units": units_out,
            "texts": texts,
            "raw_clusters": raw_clusters,
        }
        print(
            f"floor {floor_no} (page {page_no}): {len(units_out)} units, "
            f"{len(raw_clusters)} cluster keys, "
            f"{sum(e['n'] for e in raw_clusters.values())} drawings, "
            f"{len(texts)} words",
            file=sys.stderr,
        )

    # ---- assign global cluster ids by total n across all floors, descending
    sorted_keys = sorted(global_n_total.items(), key=lambda kv: -kv[1])
    key_to_id = {}
    for i, (key, _n) in enumerate(sorted_keys):
        key_to_id[key] = f"c{i:02d}"

    cluster_summary = {}
    for key, n_total in sorted_keys:
        cid = key_to_id[key]
        stroke, fill, width = key
        cluster_summary[cid] = {
            "stroke": stroke,
            "fill": fill,
            "width": width,
            "n_total": n_total,
        }

    # ---- write per-floor json files
    floors_sorted = sorted(floor_data.keys())
    files = {}
    sizes_kb = []
    top10_floor10 = []

    for floor_no in floors_sorted:
        fd = floor_data[floor_no]
        raw_clusters = fd.pop("raw_clusters")
        clusters_list = []
        for key, entry in raw_clusters.items():
            cid = key_to_id[key]
            stroke, fill, width = key
            clusters_list.append(
                {
                    "id": cid,
                    "stroke": stroke,
                    "fill": fill,
                    "width": width,
                    "n": entry["n"],
                    "paths": entry["paths"],
                    "seq": entry["seq"],
                }
            )
        clusters_list.sort(key=lambda c: -c["n"])
        fd["clusters"] = clusters_list

        text = json.dumps(fd, ensure_ascii=False, separators=(",", ":"))
        kb = len(text.encode("utf-8")) / 1024
        sizes_kb.append(kb)
        out_path = OUT_DIR / f"floor-{floor_no}.json"
        files[str(floor_no)] = out_path.name
        if only_floor is None or floor_no == only_floor:
            out_path.write_text(text)

        if floor_no == CHECK_FLOOR:
            top10_floor10 = [
                [c["id"], c["n"], c["stroke"], c["fill"], c["width"]]
                for c in clusters_list[:10]
            ]

    index = {
        "floors": floors_sorted,
        "files": files,
        "cluster_summary": cluster_summary,
    }
    if only_floor is None:
        (OUT_DIR / "index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2)
        )
        # ---- check overlay for floor 10
        fd10 = floor_data.get(CHECK_FLOOR)
        if fd10:
            draw_check_png(fd10, OUT_DIR / f"check-floor-{CHECK_FLOOR}.png")

    avg_kb = sum(sizes_kb) / len(sizes_kb) if sizes_kb else 0
    result = {
        "floors": len(floors_sorted),
        "clusters_global": len(sorted_keys),
        "avg_json_kb": round(avg_kb, 1),
        "top10_floor10": top10_floor10,
        "notes": (
            f"{len(notes_all)} extract-plans notes, {len(ambiguous_all)} ambiguous "
            f"matches carried over unchanged from match_floor/match_balconies logic."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- check overlay

def parse_path_points(path):
    """Extract raw (x,y) coordinate pairs from an SVG-ish path string built by
    build_path(), ignoring command letters, for simple polyline drawing."""
    pts = []
    tokens = path.replace(",", " ").split()
    i = 0
    cur_cmd = None
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("M", "L"):
            cur_cmd = tok
            x, y = float(tokens[i + 1]), float(tokens[i + 2])
            pts.append(("M" if tok == "M" else "L", x, y))
            i += 3
        elif tok == "C":
            x2, y2 = float(tokens[i + 1]), float(tokens[i + 2])
            x3, y3 = float(tokens[i + 3]), float(tokens[i + 4])
            x4, y4 = float(tokens[i + 5]), float(tokens[i + 6])
            pts.append(("L", x2, y2))
            pts.append(("L", x3, y3))
            pts.append(("L", x4, y4))
            i += 7
        elif tok in ("h", "v", "Z", "z"):
            # rare 're'/'qu' fallback path syntax; skip robustly
            i += 1
        else:
            i += 1
    return pts


def draw_check_png(fd, out_path, scale=1.5):
    w = int(fd["w"] * scale) + 4
    h = int(fd["h"] * scale) + 4
    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    palette = [
        (200, 30, 30), (30, 100, 200), (30, 160, 60), (200, 140, 20),
        (140, 40, 180), (20, 160, 160), (160, 60, 20), (90, 90, 90),
        (200, 20, 140), (60, 60, 200),
    ]

    for ci, c in enumerate(fd["clusters"]):
        color = palette[ci % len(palette)]
        for path in c["paths"]:
            pts = parse_path_points(path)
            poly = []
            for cmd, x, y in pts:
                if cmd == "M" and poly:
                    if len(poly) >= 2:
                        draw.line(poly, fill=color, width=1)
                    poly = [(x * scale, y * scale)]
                else:
                    poly.append((x * scale, y * scale))
            if len(poly) >= 2:
                draw.line(poly, fill=color, width=1)

    for number, u in fd["units"].items():
        poly = [(p[0] * scale, p[1] * scale) for p in u["poly"]]
        if len(poly) >= 2:
            draw.polygon(poly, outline=(0, 200, 0))
        if u.get("balcony"):
            bpoly = [(p[0] * scale, p[1] * scale) for p in u["balcony"]]
            if len(bpoly) >= 2:
                draw.polygon(bpoly, outline=(0, 150, 255))

    img.save(out_path)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"done in {time.time()-t0:.1f}s", file=sys.stderr)
