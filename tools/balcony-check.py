#!/usr/bin/env python3
"""Проверка балконов: открытые стороны в чертеже, попадание в контур ховера, расхождение площади.

.venv/bin/python tools/balcony-check.py [F …|--all]

Источники (только чтение):
  plan-studio/data/floor-F.json                 — units[N].poly / .balconies (pt, локальные)
  plan-studio/v3/figma/floor-F-manual.svg           — чертёж ревьюера (px = raw * scale(S))
  site/data/floors/floor-F.json    — units[N].outline (px, контур ховера сайта)
  site-assets/data/units.json     — units[] с number/floor/balcony (м²)

px = pt * 7.05 (константа перевода локальных координат данных в пиксели чертежа/сайта).
м² = pt² * 0.0705**2 (площадь полигона из данных, shoelace).

Часть 1 (чертёж): для каждого ребра каждого балкона, не являющегося внутренним (границей с
квартирой), сэмплируем точки каждые 5 px и проверяем, «закрыта» ли точка чёрным элементом
чертежа (fill=black, либо fill=none/отсутствует + stroke=black) в радиусе 12 px. Для путей с
bbox > 400 px по обеим сторонам (контур плиты и т.п.) проверяем расстояние до самих отрезков
пути, а не до bbox — иначе такие пути «закрывают» всё в своём bbox. Ребро «открытое», если
покрытие < 0.8 И длина ребра > 15 px.

Часть 2 (контур сайта, только если файлы site/data/floors уже пересобраны):
балкон «не в контуре», если < 90% его площади лежит внутри units[N].outline.

Часть 3 (площадь): |Σ area(balconies) - прайс| / прайс > 5% → расхождение.

Вывод: сводная таблица по этажам в stdout + JSON в plan-studio/out-v4/balcony-check.json.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

REPO = fpconfig.WORK
DATA_DIR = REPO / "plan-studio/data"
SVG_DIR = REPO / "plan-studio/v3/figma"
SITE_FLOORS_DIR = REPO / "site/data/floors"
UNITS_JSON = fpconfig.SCHEDULE
OUT_JSON = REPO / "plan-studio/out-v4/balcony-check.json"

PT2PX = 7.05
M2_PER_PT2 = 0.0705 ** 2
SAMPLE_STEP_PX = 5.0
CLOSED_RADIUS_PX = 12.0
INNER_EDGE_TOL_PT = 3.0
COVERAGE_OPEN_THRESHOLD = 0.8
MIN_OPEN_EDGE_LEN_PX = 15.0
LARGE_BBOX_PX = 400.0
AREA_MISMATCH_FRACTION = 0.05
OUTLINE_AREA_FRACTION = 0.90
GRID_CELL = 60.0

FLOOR_RANGE = range(2, 27)  # 2..26 inclusive


# --------------------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------------------

def shoelace_area(poly):
    n = len(poly)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def point_seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = x1 + t * dx, y1 + t * dy
    return math.hypot(px - cx, py - cy)


def point_to_polyline_dist(px, py, poly_closed):
    """Min distance from (px,py) to the boundary of a closed polygon (list of pt vertices)."""
    n = len(poly_closed)
    best = math.inf
    for i in range(n):
        x1, y1 = poly_closed[i]
        x2, y2 = poly_closed[(i + 1) % n]
        d = point_seg_dist(px, py, x1, y1, x2, y2)
        if d < best:
            best = d
    return best


# --------------------------------------------------------------------------------------
# SVG path parsing (M L H V C Z, absolute, uppercase only — verified for this drawing)
# --------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r'[MLHVCZ]|-?\d+\.?\d*(?:[eE]-?\d+)?')


def parse_path(d):
    """Returns (vertices, segments) in raw (unscaled) coordinates.

    vertices: all points incl. bezier control points (for bbox purposes on small paths).
    segments: list of (x1,y1,x2,y2) straight-line approximation (C treated as a straight
    chord start->end, per spec: bbox-of-control-points is enough for small/door curves).
    """
    tokens = _TOKEN_RE.findall(d)
    i = 0
    n = len(tokens)
    cx = cy = 0.0
    start = None
    vertices = []
    segments = []
    while i < n:
        t = tokens[i]
        if t in 'MLHVCZ':
            cmd = t
            i += 1
            if cmd == 'Z':
                if start is not None:
                    if (cx, cy) != start:
                        segments.append((cx, cy, start[0], start[1]))
                    vertices.append(start)
                    cx, cy = start
                continue
            if cmd == 'M':
                x, y = float(tokens[i]), float(tokens[i + 1])
                i += 2
                cx, cy = x, y
                start = (x, y)
                vertices.append((x, y))
            elif cmd == 'L':
                x, y = float(tokens[i]), float(tokens[i + 1])
                i += 2
                segments.append((cx, cy, x, y))
                cx, cy = x, y
                vertices.append((x, y))
            elif cmd == 'H':
                x = float(tokens[i])
                i += 1
                segments.append((cx, cy, x, cy))
                cx = x
                vertices.append((cx, cy))
            elif cmd == 'V':
                y = float(tokens[i])
                i += 1
                segments.append((cx, cy, cx, y))
                cy = y
                vertices.append((cx, cy))
            elif cmd == 'C':
                x1, y1 = float(tokens[i]), float(tokens[i + 1])
                x2, y2 = float(tokens[i + 2]), float(tokens[i + 3])
                x, y = float(tokens[i + 4]), float(tokens[i + 5])
                i += 6
                segments.append((cx, cy, x, y))  # approx chord, ok per spec
                vertices.extend([(x1, y1), (x2, y2), (x, y)])
                cx, cy = x, y
        else:
            i += 1  # stray token, ignore
    return vertices, segments


_PATH_TAG_RE = re.compile(r'<path\b([^>]*?)/?>')
_ATTR_RE = re.compile(r'([\w:-]+)="([^"]*)"')


def is_wall_attrs(fill, stroke):
    if fill == 'black':
        return True
    if stroke == 'black' and (fill is None or fill == 'none'):
        return True
    return False


def load_svg_wall_elements(svg_text):
    m = re.search(r'<g[^>]*transform="scale\(([\d.]+)\)"', svg_text)
    scale = float(m.group(1)) if m else 1.0

    elements = []
    for tag_m in _PATH_TAG_RE.finditer(svg_text):
        attrs_text = tag_m.group(1)
        attrs = dict(_ATTR_RE.findall(attrs_text))
        fill = attrs.get('fill')
        stroke = attrs.get('stroke')
        d = attrs.get('d')
        if not d or not is_wall_attrs(fill, stroke):
            continue
        vertices, segments = parse_path(d)
        if not vertices:
            continue
        sv = [(x * scale, y * scale) for x, y in vertices]
        xs = [p[0] for p in sv]
        ys = [p[1] for p in sv]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        large = (bbox[2] - bbox[0]) > LARGE_BBOX_PX and (bbox[3] - bbox[1]) > LARGE_BBOX_PX
        ssegs = None
        if large:
            ssegs = [(x1 * scale, y1 * scale, x2 * scale, y2 * scale) for x1, y1, x2, y2 in segments]
        elements.append({
            'id': attrs.get('id', ''),
            'bbox': bbox,
            'large': large,
            'segments': ssegs,
        })
    return scale, elements


class WallIndex:
    def __init__(self, elements):
        self.elements = elements
        self.cell = GRID_CELL
        self.grid = {}
        for idx, el in enumerate(elements):
            x0, y0, x1, y1 = el['bbox']
            x0 -= CLOSED_RADIUS_PX
            y0 -= CLOSED_RADIUS_PX
            x1 += CLOSED_RADIUS_PX
            y1 += CLOSED_RADIUS_PX
            cx0, cy0 = int(x0 // self.cell), int(y0 // self.cell)
            cx1, cy1 = int(x1 // self.cell), int(y1 // self.cell)
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    self.grid.setdefault((cx, cy), []).append(idx)

    def covered(self, x, y):
        cx, cy = int(x // self.cell), int(y // self.cell)
        seen = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for idx in self.grid.get((cx + dx, cy + dy), ()):
                    if idx in seen:
                        continue
                    seen.add(idx)
                    el = self.elements[idx]
                    if el['large']:
                        for (x1, y1, x2, y2) in el['segments']:
                            if point_seg_dist(x, y, x1, y1, x2, y2) <= CLOSED_RADIUS_PX:
                                return True
                    else:
                        bx0, by0, bx1, by1 = el['bbox']
                        if (bx0 - CLOSED_RADIUS_PX <= x <= bx1 + CLOSED_RADIUS_PX and
                                by0 - CLOSED_RADIUS_PX <= y <= by1 + CLOSED_RADIUS_PX):
                            return True
        return False


# --------------------------------------------------------------------------------------
# per-floor processing
# --------------------------------------------------------------------------------------

def unit_balconies(u):
    if 'balconies' in u and u['balconies']:
        return u['balconies']
    if 'balcony' in u and u['balcony']:
        return [u['balcony']]
    return []


def check_floor_svg(fnum, data, svg_text):
    """Part 1: open edges against the drawing."""
    scale, wall_elements = load_svg_wall_elements(svg_text)
    index = WallIndex(wall_elements)

    edges_out = []
    balcony_count = 0
    for number, u in data['units'].items():
        poly_pt = u.get('poly') or []
        poly_closed = poly_pt
        for b_idx, bal in enumerate(unit_balconies(u)):
            balcony_count += 1
            n = len(bal)
            if n < 2:
                continue
            for i in range(n):
                p1 = bal[i]
                p2 = bal[(i + 1) % n]
                mid_pt = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
                if poly_closed and point_to_polyline_dist(mid_pt[0], mid_pt[1], poly_closed) < INNER_EDGE_TOL_PT:
                    continue  # internal edge (borders the apartment) — skip
                p1_px = (p1[0] * PT2PX, p1[1] * PT2PX)
                p2_px = (p2[0] * PT2PX, p2[1] * PT2PX)
                length_px = math.hypot(p2_px[0] - p1_px[0], p2_px[1] - p1_px[1])
                if length_px <= 0:
                    continue
                steps = max(1, round(length_px / SAMPLE_STEP_PX))
                closed_n = 0
                total_n = steps + 1
                for s in range(total_n):
                    t = s / steps
                    sx = p1_px[0] + t * (p2_px[0] - p1_px[0])
                    sy = p1_px[1] + t * (p2_px[1] - p1_px[1])
                    if index.covered(sx, sy):
                        closed_n += 1
                coverage = closed_n / total_n
                is_open = coverage < COVERAGE_OPEN_THRESHOLD and length_px > MIN_OPEN_EDGE_LEN_PX
                edges_out.append({
                    'floor': fnum,
                    'unit': number,
                    'balcony_idx': b_idx,
                    'edge_idx': i,
                    'p1_px': [round(p1_px[0], 1), round(p1_px[1], 1)],
                    'p2_px': [round(p2_px[0], 1), round(p2_px[1], 1)],
                    'length_px': round(length_px, 1),
                    'coverage': round(coverage, 3),
                    'open': is_open,
                })
    return balcony_count, edges_out


def load_outline_polys(fnum):
    path = SITE_FLOORS_DIR / f"floor-{fnum}.json"
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    out = {}
    for number, u in d.get('units', {}).items():
        outline = u.get('outline')
        if outline:
            out[number] = outline
    return out


def check_floor_outline(fnum, data, outline_by_unit):
    """Part 2: balcony-in-hover-outline check via shapely."""
    from shapely.geometry import Polygon
    from shapely.validation import make_valid

    results = []
    for number, u in data['units'].items():
        if number not in outline_by_unit:
            continue
        try:
            outline_poly = Polygon(outline_by_unit[number])
            if not outline_poly.is_valid:
                outline_poly = make_valid(outline_poly)
        except Exception:
            continue
        for b_idx, bal in enumerate(unit_balconies(u)):
            if len(bal) < 3:
                continue
            bal_px = [(x * PT2PX, y * PT2PX) for x, y in bal]
            try:
                bal_poly = Polygon(bal_px)
                if not bal_poly.is_valid:
                    bal_poly = make_valid(bal_poly)
                area = bal_poly.area
                if area <= 0:
                    continue
                inter = bal_poly.intersection(outline_poly).area
                frac = inter / area
            except Exception:
                continue
            in_outline = frac >= OUTLINE_AREA_FRACTION
            results.append({
                'floor': fnum,
                'unit': number,
                'balcony_idx': b_idx,
                'fraction_in_outline': round(frac, 3),
                'in_outline': in_outline,
            })
    return results


def check_floor_area(fnum, data, price_by_unit):
    results = []
    for number, u in data['units'].items():
        if number not in price_by_unit:
            continue
        price_balcony = price_by_unit[number]
        if price_balcony is None:
            continue
        total_area_m2 = 0.0
        for bal in unit_balconies(u):
            if len(bal) < 3:
                continue
            total_area_m2 += shoelace_area(bal) * M2_PER_PT2
        if price_balcony == 0:
            mismatch = total_area_m2 > 0
            frac = None
        else:
            frac = abs(total_area_m2 - price_balcony) / price_balcony
            mismatch = frac > AREA_MISMATCH_FRACTION
        results.append({
            'floor': fnum,
            'unit': number,
            'computed_m2': round(total_area_m2, 2),
            'price_m2': price_balcony,
            'diff_fraction': round(frac, 3) if frac is not None else None,
            'mismatch': mismatch,
        })
    return results


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('floors', nargs='*', help='floor numbers, or omit with --all')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--skip-outline', action='store_true', help='skip part 2 (hover-outline) even if files exist')
    args = ap.parse_args()

    if args.all or not args.floors:
        floors = list(FLOOR_RANGE)
    else:
        floors = [int(f) for f in args.floors]

    price_by_unit = {}
    if UNITS_JSON.exists():
        units_data = json.loads(UNITS_JSON.read_text())
        for u in units_data.get('units', []):
            price_by_unit[str(u.get('number'))] = u.get('balcony')

    per_floor_summary = []
    all_edges = []
    all_outline = []
    all_area = []

    for fnum in floors:
        data_path = DATA_DIR / f"floor-{fnum}.json"
        svg_path = SVG_DIR / f"floor-{fnum}-manual.svg"
        if not data_path.exists() or not svg_path.exists():
            print(f"WARN floor {fnum}: missing data/svg, skipped", file=sys.stderr)
            continue
        data = json.loads(data_path.read_text())
        svg_text = svg_path.read_text()

        balcony_count, edges = check_floor_svg(fnum, data, svg_text)
        all_edges.extend(edges)
        open_edges = [e for e in edges if e['open']]

        outline_by_unit = None if args.skip_outline else load_outline_polys(fnum)
        outline_results = []
        not_in_outline = []
        if outline_by_unit:
            outline_results = check_floor_outline(fnum, data, outline_by_unit)
            all_outline.extend(outline_results)
            not_in_outline = [r for r in outline_results if not r['in_outline']]

        area_results = check_floor_area(fnum, data, price_by_unit)
        all_area.extend(area_results)
        area_mismatch = [r for r in area_results if r['mismatch']]

        per_floor_summary.append({
            'floor': fnum,
            'balconies': balcony_count,
            'open_edges': len(open_edges),
            'not_in_outline': len(not_in_outline),
            'area_mismatch': len(area_mismatch),
            'outline_checked': outline_by_unit is not None,
        })
        print(f"floor {fnum}: balconies={balcony_count} open_edges={len(open_edges)} "
              f"not_in_outline={len(not_in_outline)}{'*' if outline_by_unit is None else ''} "
              f"area_mismatch={len(area_mismatch)}", file=sys.stderr)

    result = {
        'floors': per_floor_summary,
        'open_edges': [e for e in all_edges if e['open']],
        'all_edges': all_edges,
        'not_in_outline': [r for r in all_outline if not r['in_outline']],
        'outline_checked_floors': [s['floor'] for s in per_floor_summary if s['outline_checked']],
        'area_mismatch': [r for r in all_area if r['mismatch']],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    # summary table
    print()
    print("| Этаж | балконов | открытых рёбер | балконов не в контуре | расхождений площади |")
    print("|---|---|---|---|---|")
    for s in per_floor_summary:
        noc = str(s['not_in_outline']) + ('' if s['outline_checked'] else ' (н/д)')
        print(f"| {s['floor']} | {s['balconies']} | {s['open_edges']} | {noc} | {s['area_mismatch']} |")
    print(f"\nJSON: {OUT_JSON}")


if __name__ == '__main__':
    main()
