#!/usr/bin/env python3
"""
extract-plans.py

Извлекает планы этажей и планировки квартир из архитектурного PDF
(векторный экспорт ArchiCAD) и генерирует:
  - site-assets/plans/floor-<N>.png       (план этажа N)
  - site-assets/plans/unit-<number>.png   (планировка квартиры)
  - site-assets/plans/plans.json           (координаты полигонов в px)
  - site-assets/plans/check-floor-<N>.png  (оверлей для проверки, N=2,10,26)

Ничего не меняет в units.json и файлах сайта.
"""

import json
import math
import re
import statistics
import sys
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
PDF_PATH = fpconfig.PDF
UNITS_PATH = fpconfig.SCHEDULE
OUT_DIR = ROOT / "site-assets/plans"

PAGE_FIRST = fpconfig.PAGE_FIRST   # 1-based, first floor page (config "pages")
PAGE_LAST = fpconfig.PAGE_LAST     # 1-based, last floor page

FLOOR_DPI = 220
UNIT_DPI = 300
FLOOR_MARGIN_PT = 24
UNIT_MARGIN_PT = 12

CHECK_FLOORS = [2, 10, 26]

NUM_RE = re.compile(r"^\d+\.\d+$")

# ---------------------------------------------------------------- geometry

def poly_from_items(items):
    """Собрать последовательность точек из списка ('l', p1, p2) элементов клипа."""
    pts = []
    for it in items:
        if it[0] != "l":
            continue
        p1, p2 = it[1], it[2]
        if not pts:
            pts.append((p1.x, p1.y))
        pts.append((p2.x, p2.y))
    return pts


def poly_area(pts):
    area = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def point_in_poly(x, y, pts):
    n = len(pts)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def bbox_of_points(pts_list):
    xs = [x for pts in pts_list for (x, y) in pts]
    ys = [y for pts in pts_list for (x, y) in pts]
    return fitz.Rect(min(xs), min(ys), max(xs), max(ys))


def dist_bbox(a, b):
    dx = max(a.x0 - b.x1, b.x0 - a.x1, 0)
    dy = max(a.y0 - b.y1, b.y0 - a.y1, 0)
    return dx, dy


def x_overlap_frac(a, b):
    lo, hi = max(a.x0, b.x0), min(a.x1, b.x1)
    xo = max(0, hi - lo)
    m = min(a.width, b.width)
    return xo / m if m > 0 else 0


def y_overlap_frac(a, b):
    lo, hi = max(a.y0, b.y0), min(a.y1, b.y1)
    yo = max(0, hi - lo)
    m = min(a.height, b.height)
    return yo / m if m > 0 else 0


# ---------------------------------------------------------------- candidates

def get_apartment_candidates(page):
    """Собрать полигоны-кандидаты на квартиру/помещение из clip-путей страницы."""
    drawings = page.get_drawings(extended=True)
    clips = [d for d in drawings if d.get("type") == "clip"]

    words = page.get_text("words")
    nums = []
    for w in words:
        if NUM_RE.match(w[4]):
            cx = (w[0] + w[2]) / 2
            cy = (w[1] + w[3]) / 2
            nums.append((cx, cy, float(w[4])))

    candidates = []
    seen_keys = set()
    for d in clips:
        bbox = d.get("scissor") or d.get("rect")
        if bbox is None:
            continue
        w, h = bbox.width, bbox.height
        if w > 450 or h > 450:
            continue
        items = d.get("items", [])
        pts = poly_from_items(items)
        if len(pts) < 6:
            continue
        # Note: strict first==last closure is NOT required here. Some genuine
        # apartment hatch clips (e.g. L-shaped rooms) are emitted by ArchiCAD as
        # several concatenated sub-loops without an explicit reset, so the
        # overall first/last points don't coincide even though the shape is a
        # valid (self-closing-per-loop) hatch outline. Point-in-polygon below
        # implicitly closes the ring via modulo indexing, which is good enough
        # for containment tests. We rely on the area/size bounds instead to
        # reject junk paths.
        area = poly_area(pts)
        if area < 800 or area > 40000:
            continue
        key = (round(bbox.x0), round(bbox.y0), round(bbox.x1), round(bbox.y1), len(items))
        if key in seen_keys:
            continue
        seen_keys.add(key)

        contained = [v for (cx, cy, v) in nums if point_in_poly(cx, cy, pts)]
        if not contained:
            continue

        cxc = sum(p[0] for p in pts) / len(pts)
        cyc = sum(p[1] for p in pts) / len(pts)
        candidates.append(
            {
                "bbox": bbox,
                "pts": pts,
                "area": area,
                "nums": contained,
                "centroid": (cxc, cyc),
            }
        )
    return candidates


def get_small_clip_bboxes(page):
    """Все маленькие клип-bbox на странице (кандидаты в балконы)."""
    drawings = page.get_drawings(extended=True)
    clips = [d for d in drawings if d.get("type") == "clip"]
    out = []
    seen = set()
    for d in clips:
        bbox = d.get("scissor") or d.get("rect")
        if bbox is None:
            continue
        w, h = bbox.width, bbox.height
        if w < 8 or h < 8:
            continue
        if w > 250 or h > 250:
            continue
        key = (round(bbox.x0), round(bbox.y0), round(bbox.x1), round(bbox.y1))
        if key in seen:
            continue
        seen.add(key)
        out.append(bbox)
    return out


# ---------------------------------------------------------------- matching

def match_floor(page, floor_units, floor_no, notes, ambiguous):
    """Сопоставить units.json с полигонами квартир на странице этажа."""
    candidates = get_apartment_candidates(page)

    # value(rounded 1dp) -> list of candidate dicts whose contained nums include it
    value_to_cands = {}
    for c in candidates:
        for v in c["nums"]:
            rv = round(v, 1)
            value_to_cands.setdefault(rv, []).append(c)

    value_to_units = {}
    for u in floor_units:
        rv = round(u["living"], 1)
        value_to_units.setdefault(rv, []).append(u)

    matched = {}  # number -> candidate dict
    used_cand_ids = set()

    # Pass 1: unique-living units
    for rv, units in value_to_units.items():
        if len(units) != 1:
            continue
        u = units[0]
        cands = value_to_cands.get(rv, [])
        cands = [c for c in cands if id(c) not in used_cand_ids]
        if not cands:
            continue
        # prefer the one with the biggest area (most likely the true hatch, not a sub-room)
        best = max(cands, key=lambda c: c["area"])
        matched[u["number"]] = best
        used_cand_ids.add(id(best))

    # Global axis+sign fit kept only as a last-resort fallback (see below) -
    # slot number vs. position is NOT monotonic over a whole floor in
    # general, because units walk around the building perimeter (the trend
    # reverses at each corner). A global fit gets locally-adjacent duplicate
    # units (e.g. two studios both "29.5 m²" next to each other) backwards
    # about as often as right. We instead resolve each ambiguous group using
    # the *local* uniquely-matched neighbour(s) immediately before/after it
    # in slot order, which correctly tracks the corridor direction for that
    # specific stretch.
    slot_to_number = {u["slot"]: u["number"] for u in floor_units}

    def anchor_for_slot(slot):
        num = slot_to_number.get(slot)
        if num is not None and num in matched:
            return matched[num]["centroid"]
        return None

    def global_proj_fallback(cands, units_sorted):
        anchors = []
        for u in floor_units:
            if u["number"] in matched:
                cx, cy = matched[u["number"]]["centroid"]
                anchors.append((u["slot"], cx, cy))
        if len(anchors) < 3:
            axis, sign = "x", 1
        else:
            slots = [a[0] for a in anchors]
            xs = [a[1] for a in anchors]
            ys = [a[2] for a in anchors]
            try:
                cx_corr = statistics.correlation(slots, xs)
            except Exception:
                cx_corr = 0
            try:
                cy_corr = statistics.correlation(slots, ys)
            except Exception:
                cy_corr = 0
            if abs(cx_corr) >= abs(cy_corr):
                axis, sign = "x", (1 if cx_corr >= 0 else -1)
            else:
                axis, sign = "y", (1 if cy_corr >= 0 else -1)

        def proj(c):
            cx, cy = c["centroid"]
            v = cx if axis == "x" else cy
            return v * sign

        return sorted(cands, key=proj), f"global-{axis}-position"

    def nearest_neighbor_order(cands, start_point):
        remaining = list(cands)
        cur = start_point
        ordered = []
        while remaining:
            nxt = min(
                remaining,
                key=lambda c: (c["centroid"][0] - cur[0]) ** 2
                + (c["centroid"][1] - cur[1]) ** 2,
            )
            ordered.append(nxt)
            remaining.remove(nxt)
            cur = nxt["centroid"]
        return ordered

    # Pass 2: ambiguous groups (duplicate living values)
    for rv, units in value_to_units.items():
        if len(units) <= 1:
            continue
        cands = value_to_cands.get(rv, [])
        cands = [c for c in cands if id(c) not in used_cand_ids]
        units_sorted = sorted(units, key=lambda u: u["slot"])

        if len(cands) < len(units_sorted):
            notes.append(
                f"floor {floor_no}: living={rv} - only {len(cands)} candidate polygon(s) "
                f"for {len(units_sorted)} units {[u['number'] for u in units_sorted]}; "
                f"some left unmatched"
            )

        anchor_before = anchor_for_slot(units_sorted[0]["slot"] - 1)
        anchor_after = anchor_for_slot(units_sorted[-1]["slot"] + 1)

        method = ""
        if anchor_before is not None and anchor_after is not None:
            bx, by = anchor_before
            ax_, ay_ = anchor_after
            vx, vy = ax_ - bx, ay_ - by

            def proj(c, bx=bx, by=by, vx=vx, vy=vy):
                cx, cy = c["centroid"]
                return (cx - bx) * vx + (cy - by) * vy

            cands_sorted = sorted(cands, key=proj)
            method = "local neighbour direction (before+after anchors)"
        elif anchor_before is not None:
            cands_sorted = nearest_neighbor_order(cands, anchor_before)
            method = "nearest-neighbour chain from preceding slot"
        elif anchor_after is not None:
            cands_sorted = list(reversed(nearest_neighbor_order(cands, anchor_after)))
            method = "nearest-neighbour chain from following slot (reversed)"
        else:
            cands_sorted, method = global_proj_fallback(cands, units_sorted)
            method = f"fallback {method} (no local anchors found)"

        n = min(len(units_sorted), len(cands_sorted))
        for u, c in zip(units_sorted[:n], cands_sorted[:n]):
            matched[u["number"]] = c
            used_cand_ids.add(id(c))
            ambiguous.append(
                f"floor {floor_no} unit {u['number']} (living={rv}): matched by "
                f"{method} (duplicate living value on floor)"
            )

    missing = [u["number"] for u in floor_units if u["number"] not in matched]
    return matched, missing


def match_balconies(page, floor_units, matched, floor_no, ambiguous, notes):
    """Найти полигон балкона для каждой сопоставленной квартиры (эвристика по геометрии)."""
    small_bboxes = get_small_clip_bboxes(page)

    # estimate scale (pt^2 per m^2) from confidently matched apartments
    ratios = []
    unit_by_number = {u["number"]: u for u in floor_units}
    for number, c in matched.items():
        u = unit_by_number[number]
        if u["living"] > 0:
            ratios.append(c["area"] / u["living"])
    scale2 = statistics.median(ratios) if ratios else 200.7

    # Overall floor envelope = union of all matched apartment bboxes. Real
    # balconies are cantilevered outward and protrude past this envelope on
    # at least one side; small clips fully inside it (e.g. elevator machine
    # room dimension boxes, closets, nooks) are NOT balconies even if their
    # area happens to coincide numerically.
    floor_env = bbox_of_points([c["pts"] for c in matched.values()])

    balcony_polys = {}

    def gather(require_floor_protrusion, ratio_lo, ratio_hi):
        all_candidates = []
        for number, c in matched.items():
            u = unit_by_number[number]
            apt = c["bbox"]
            exp_area = u["balcony"] * scale2
            if exp_area <= 0:
                continue
            for bb in small_bboxes:
                dx, dy = dist_bbox(apt, bb)
                if dx > 6 or dy > 6:
                    continue
                extends_apt = (
                    bb.x1 > apt.x1 + 3
                    or bb.x0 < apt.x0 - 3
                    or bb.y1 > apt.y1 + 3
                    or bb.y0 < apt.y0 - 3
                )
                if not extends_apt:
                    continue
                extends_floor = (
                    bb.x1 > floor_env.x1 + 3
                    or bb.x0 < floor_env.x0 - 3
                    or bb.y1 > floor_env.y1 + 3
                    or bb.y0 < floor_env.y0 - 3
                )
                if require_floor_protrusion and not extends_floor:
                    continue
                if bb.y1 > apt.y1 + 3 or bb.y0 < apt.y0 - 3:
                    ov = x_overlap_frac(apt, bb)
                else:
                    ov = y_overlap_frac(apt, bb)
                if ov < 0.55:
                    continue
                area = bb.width * bb.height
                ratio = area / exp_area
                if not (ratio_lo < ratio < ratio_hi):
                    continue
                score = abs(ratio - 1) - ov * 0.3
                all_candidates.append((score, number, bb, ratio, ov, extends_floor))
        return all_candidates

    used_bbox_keys = set()
    assigned_numbers = set()

    # Pass A: candidate must protrude past the floor's overall apartment
    # envelope (real balconies do). Generous area-ratio tolerance since
    # corner/irregular balconies aren't always a clean rectangle.
    # Pass B (fallback): candidate stays inside the envelope (interior nook /
    # loggia) - much higher risk of being a false positive (elevator core,
    # storage nook, etc.), so require a tight area-ratio match before we
    # trust it at all.
    passes = [(True, 0.5, 2.0), (False, 0.75, 1.35)]

    for require_protrusion, ratio_lo, ratio_hi in passes:
        cands = gather(require_protrusion, ratio_lo, ratio_hi)
        cands.sort(key=lambda t: t[0])
        for score, number, bb, ratio, ov, extends_floor in cands:
            if number in assigned_numbers:
                continue
            key = (round(bb.x0), round(bb.y0), round(bb.x1), round(bb.y1))
            if key in used_bbox_keys:
                continue
            used_bbox_keys.add(key)
            assigned_numbers.add(number)
            pts = [
                (bb.x0, bb.y0),
                (bb.x1, bb.y0),
                (bb.x1, bb.y1),
                (bb.x0, bb.y1),
            ]
            balcony_polys[number] = pts
            if not extends_floor:
                ambiguous.append(
                    f"floor {floor_no} unit {number}: balcony polygon guessed with low "
                    f"confidence - candidate does not protrude past the floor's "
                    f"apartment envelope (ratio {ratio:.2f}); may be an interior "
                    f"nook/core clip mistaken for a balcony"
                )
            elif not (0.6 <= ratio <= 1.6):
                ambiguous.append(
                    f"floor {floor_no} unit {number}: balcony polygon guessed with low "
                    f"confidence (area ratio {ratio:.2f})"
                )

    no_balcony = [n for n in matched if n not in balcony_polys]
    if no_balcony:
        notes.append(
            f"floor {floor_no}: no balcony polygon found for units {no_balcony} "
            f"(balcony set to null)"
        )

    return balcony_polys


# ---------------------------------------------------------------- rendering

def render_clip(page, clip_rect, dpi, out_path):
    pix = page.get_pixmap(clip=clip_rect, dpi=dpi)
    pix.save(str(out_path))
    return pix.width, pix.height


def pts_to_px(pts, clip_rect, dpi):
    scale = dpi / 72.0
    return [[round((x - clip_rect.x0) * scale, 1), round((y - clip_rect.y0) * scale, 1)] for (x, y) in pts]


# ---------------------------------------------------------------- overlay check

def draw_check_overlay(floor_png_path, floor_w, floor_h, units_px, out_path):
    base = Image.open(floor_png_path).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.truetype(
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf", 22
        )
    except Exception:
        font = ImageFont.load_default()

    colors = [
        (255, 60, 60, 90),
        (60, 140, 255, 90),
        (60, 200, 100, 90),
        (255, 180, 40, 90),
        (200, 80, 220, 90),
    ]
    for i, (number, data) in enumerate(sorted(units_px.items())):
        color = colors[i % len(colors)]
        poly = [tuple(p) for p in data["poly"]]
        draw.polygon(poly, fill=color, outline=(255, 255, 255, 220))
        if data.get("balcony"):
            bpoly = [tuple(p) for p in data["balcony"]]
            draw.polygon(bpoly, fill=(255, 255, 0, 90), outline=(0, 0, 0, 200))
        cx = sum(p[0] for p in poly) / len(poly)
        cy = sum(p[1] for p in poly) / len(poly)
        text = number
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.rectangle(
            [cx - tw / 2 - 4, cy - th / 2 - 4, cx + tw / 2 + 4, cy + th / 2 + 4],
            fill=(0, 0, 0, 180),
        )
        draw.text((cx - tw / 2, cy - th / 2), text, fill=(255, 255, 255, 255), font=font)

    out = Image.alpha_composite(base, overlay).convert("RGB")
    out.save(out_path)


# ---------------------------------------------------------------- main

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    units_data = json.loads(UNITS_PATH.read_text())
    all_units = units_data["units"]

    doc = fitz.open(str(PDF_PATH))

    result = {"floors": {}, "units": {}, "missing": [], "notes": [], "ambiguous": []}
    ambiguous = result["ambiguous"]
    notes = result["notes"]

    floors_done = 0
    units_matched = 0

    for page_no in range(PAGE_FIRST, PAGE_LAST + 1):
        floor_no = fpconfig.floor_for_page(page_no)
        page = doc[page_no - 1]
        floor_units = [u for u in all_units if u["floor"] == floor_no]
        if not floor_units:
            notes.append(f"page {page_no}: no units.json entries for floor {floor_no}, skipped")
            continue

        matched, missing = match_floor(page, floor_units, floor_no, notes, ambiguous)
        balcony_polys = match_balconies(page, floor_units, matched, floor_no, ambiguous, notes)

        result["missing"].extend(missing)

        if not matched:
            notes.append(f"floor {floor_no}: NO units matched at all - check page mapping")
            continue

        # floor bbox = union of apt polygons + margin
        all_poly_pts = [c["pts"] for c in matched.values()]
        union_bbox = bbox_of_points(all_poly_pts)
        clip_rect = fitz.Rect(
            union_bbox.x0 - FLOOR_MARGIN_PT,
            union_bbox.y0 - FLOOR_MARGIN_PT,
            union_bbox.x1 + FLOOR_MARGIN_PT,
            union_bbox.y1 + FLOOR_MARGIN_PT,
        )
        floor_png = OUT_DIR / f"floor-{floor_no}.png"
        fw, fh = render_clip(page, clip_rect, FLOOR_DPI, floor_png)

        floor_units_out = {}
        for number, c in matched.items():
            poly_px = pts_to_px(c["pts"], clip_rect, FLOOR_DPI)
            bal_px = None
            if number in balcony_polys:
                bal_px = pts_to_px(balcony_polys[number], clip_rect, FLOOR_DPI)
            u = next(u for u in floor_units if u["number"] == number)
            floor_units_out[number] = {
                "poly": poly_px,
                "balcony": bal_px,
                "label_living": u["living"],
            }

        result["floors"][str(floor_no)] = {
            "png": floor_png.name,
            "w": fw,
            "h": fh,
            "units": floor_units_out,
        }

        # per-unit crops
        for number, c in matched.items():
            polys_for_bbox = [c["pts"]]
            if number in balcony_polys:
                polys_for_bbox.append(balcony_polys[number])
            ub = bbox_of_points(polys_for_bbox)
            uclip = fitz.Rect(
                ub.x0 - UNIT_MARGIN_PT,
                ub.y0 - UNIT_MARGIN_PT,
                ub.x1 + UNIT_MARGIN_PT,
                ub.y1 + UNIT_MARGIN_PT,
            )
            unit_png = OUT_DIR / f"unit-{number}.png"
            uw, uh = render_clip(page, uclip, UNIT_DPI, unit_png)
            poly_px = pts_to_px(c["pts"], uclip, UNIT_DPI)
            bal_px = None
            if number in balcony_polys:
                bal_px = pts_to_px(balcony_polys[number], uclip, UNIT_DPI)
            result["units"][number] = {
                "png": unit_png.name,
                "w": uw,
                "h": uh,
                "poly": poly_px,
                "balcony": bal_px,
            }
            units_matched += 1

        floors_done += 1
        print(f"floor {floor_no} (page {page_no}): matched {len(matched)}/{len(floor_units)}, "
              f"missing={missing}", file=sys.stderr)

        if floor_no in CHECK_FLOORS:
            check_path = OUT_DIR / f"check-floor-{floor_no}.png"
            draw_check_overlay(floor_png, fw, fh, floor_units_out, check_path)

    plans_path = OUT_DIR / "plans.json"
    plans_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    summary = {
        "floors_done": floors_done,
        "units_matched": units_matched,
        "units_missing": result["missing"],
        "ambiguous": ambiguous,
        "check_pngs": [str(OUT_DIR / f"check-floor-{f}.png") for f in CHECK_FLOORS],
    }
    (OUT_DIR / "_run-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
