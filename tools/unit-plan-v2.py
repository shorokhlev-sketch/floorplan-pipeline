#!/usr/bin/env python3
"""
unit-plan-v2.py — hand-redraw-quality unit floor plan (single unit), styled after
a reference unit plan (optional image, not shipped): solid dark walls, cream floor, thin-line
furniture symbols (own library, not raw ArchiCAD blocks), windows/doors drawn as
clean symbols, room + unit area labels in Instrument Serif.

Usage:
  python tools/unit-plan-v2.py 1005

Reads:
  plan-studio/data/floor-<N>.json   (clusters, unit poly/balcony, texts)
  plan-studio/style.json
  site-assets/data/units.json

Writes:
  plan-studio/v2/unit-<number>.svg
  plan-studio/v2/unit-<number>.png
  plan-studio/v2/compare-<number>.png   (ours + reference side by side)

Does NOT touch render-plans.py / plans.json / site files / _assets.
"""
import argparse
import io
import json
import math
import re
import sys
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright
import asyncio
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
DATA_DIR = ROOT / "plan-studio" / "data"
STYLE_PATH = fpconfig.STYLE
UNITS_JSON_PATH = fpconfig.SCHEDULE
OUT_DIR = ROOT / "plan-studio" / "v2"
REF_IMG = ROOT / "refs" / "unit-plan-ref.jpg"   # optional side-by-side reference, not shipped

FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:wght@400&display=swap" rel="stylesheet">'
)

NUM_RE = re.compile(r"-?\d+\.?\d*")


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- geometry --

def path_bbox(d):
    nums = [float(n) for n in NUM_RE.findall(d)]
    if not nums:
        return None
    xs, ys = nums[0::2], nums[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_overlap(b, region, pad=0.0):
    if b is None:
        return False
    x0, y0, x1, y1 = b
    rx0, ry0, rx1, ry1 = region
    return not (x1 < rx0 - pad or x0 > rx1 + pad or y1 < ry0 - pad or y0 > ry1 + pad)


def bbox_of(point_lists):
    xs, ys = [], []
    for pts in point_lists:
        for x, y in pts:
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def polygon_centroid(pts):
    n = len(pts)
    A = Cx = Cy = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        A += cross
        Cx += (x0 + x1) * cross
        Cy += (y0 + y1) * cross
    A *= 0.5
    if abs(A) < 1e-6:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return sum(xs) / len(xs), sum(ys) / len(ys)
    return Cx / (6 * A), Cy / (6 * A)


def shoelace_area(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


# -- polygon outward offset (copied algorithm shape from tools/render-plans.py, --
#    independent implementation so that file stays untouched) --

def _dedupe(pts):
    out = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > 1e-6:
            out.append(p)
    if len(out) > 1 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) < 1e-6:
        out.pop()
    return out


def _signed_area(pts):
    a = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return a * 0.5


def _line_intersect(l1, l2):
    x1, y1, dx1, dy1 = l1
    x2, y2, dx2, dy2 = l2
    denom = dx1 * dy2 - dy1 * dx2
    if abs(denom) < 1e-9:
        return None
    t = ((x2 - x1) * dy2 - (y2 - y1) * dx2) / denom
    return (x1 + dx1 * t, y1 + dy1 * t)


def _offset_once(pts, delta, sign):
    n = len(pts)
    lines, normals = [], []
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        ux, uy = dx / length, dy / length
        nx, ny = uy * sign, -ux * sign
        lines.append((x0 + nx * delta, y0 + ny * delta, ux, uy))
        normals.append((nx, ny))
    m = len(lines)
    if m < 3:
        return pts
    out = []
    for i in range(m):
        ox, oy = pts[i % len(pts)]
        pt = _line_intersect(lines[i - 1], lines[i])
        if pt is None or math.hypot(pt[0] - ox, pt[1] - oy) > 4 * abs(delta) + 1:
            anx = normals[i - 1][0] + normals[i][0]
            any_ = normals[i - 1][1] + normals[i][1]
            alen = math.hypot(anx, any_) or 1.0
            pt = (ox + anx / alen * delta, oy + any_ / alen * delta)
        out.append(pt)
    return out


def offset_polygon(pts, delta):
    pts = _dedupe(pts)
    if len(pts) < 3:
        return pts
    orig_area = abs(_signed_area(pts))
    cand_a = _offset_once(pts, delta, 1)
    cand_b = _offset_once(pts, delta, -1)
    area_a = abs(_signed_area(cand_a)) if len(cand_a) >= 3 else 0
    area_b = abs(_signed_area(cand_b)) if len(cand_b) >= 3 else 0
    best = cand_a if area_a >= area_b else cand_b
    if max(area_a, area_b) < orig_area:
        return pts
    return best


def path_d(pts):
    if not pts:
        return ""
    d = f"M{pts[0][0]:.2f} {pts[0][1]:.2f} "
    d += " ".join(f"L{x:.2f} {y:.2f}" for x, y in pts[1:])
    d += " Z"
    return d


def pts_attr(pts):
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)


def fmt_area(x):
    return f"{x:.1f}".replace(".", ",") + " м²"


def area_markup(x):
    """Area text as SVG markup with a manually-built superscript "2" (a
    <tspan>, not the U+00B2 glyph) — Instrument Serif's web-font subset
    turned out not to cover U+00B2, and the browser's per-glyph fallback
    for just that character landed on macOS's "Last Resort" font, which
    rendered a stray pictographic placeholder glyph instead of a small 2."""
    val = f"{x:.1f}".replace(".", ",")
    return f'{val} м<tspan baseline-shift="35%" font-size="70%">2</tspan>'


# --------------------------------------------------------- furniture library --
# Simple hand-drawn-style symbols, single ink stroke (0.5pt), white backing fill
# so lines read cleanly over the cream floor. All in *local* pt coordinates,
# placed via a translate(x,y) wrapper by the caller.

INK_W = 0.5


def _g(x, y, body):
    return f'<g transform="translate({x:.2f},{y:.2f})">{body}</g>'


def backing_rect(w, h, rx=0.6):
    return f'<rect x="0" y="0" width="{w:.2f}" height="{h:.2f}" rx="{rx}" fill="#FFFFFF"/>'


def outline_rect(w, h, rx=0.6, ink="#182E46"):
    return (f'<rect x="0" y="0" width="{w:.2f}" height="{h:.2f}" rx="{rx}" '
            f'fill="none" stroke="{ink}" stroke-width="{INK_W}"/>')


def bed(w, h, ink="#182E46"):
    """Headboard at local y=0, foot at y=h."""
    pillow_h = h * 0.20
    pillow_w = (w - w * 0.12) / 2
    gap = w * 0.06
    blanket_y = h * 0.62
    corner = min(w, h) * 0.10
    body = backing_rect(w, h, 1.2)
    body += outline_rect(w, h, 1.2, ink)
    # two pillows
    for i in range(2):
        px = w * 0.06 + i * (pillow_w + gap)
        body += (f'<rect x="{px:.2f}" y="{h*0.08:.2f}" width="{pillow_w:.2f}" height="{pillow_h:.2f}" '
                  f'rx="1.4" fill="none" stroke="{ink}" stroke-width="{INK_W}"/>')
    # blanket fold line with a corner turn-back (like the reference bed)
    body += (f'<path d="M0 {blanket_y:.2f} H{w - corner:.2f} L{w:.2f} {blanket_y + corner:.2f} V{h:.2f}" '
              f'fill="none" stroke="{ink}" stroke-width="{INK_W}"/>')
    return body


def nightstand(w, h, ink="#182E46"):
    return backing_rect(w, h, 0.8) + outline_rect(w, h, 0.8, ink)


def wardrobe(w, h, ink="#182E46", rail_frac=0.30):
    body = backing_rect(w, h) + outline_rect(w, h, 0.6, ink)
    body += f'<path d="M0 0 L{w:.2f} {h:.2f}" fill="none" stroke="{ink}" stroke-width="{INK_W}"/>'
    ry = h * rail_frac
    body += f'<path d="M{w*0.08:.2f} {ry:.2f} H{w*0.92:.2f}" fill="none" stroke="{ink}" stroke-width="{INK_W}"/>'
    return body


def dining_table(w, h, ink="#182E46"):
    return backing_rect(w, h, 1.0) + outline_rect(w, h, 1.0, ink)


def chair(s, ink="#182E46"):
    return backing_rect(s, s, 1.4) + outline_rect(s, s, 1.4, ink)


def fridge(w, h, ink="#182E46"):
    body = backing_rect(w, h, 0.6) + outline_rect(w, h, 0.6, ink)
    body += f'<path d="M{w*0.15:.2f} {h*0.15:.2f} L{w*0.85:.2f} {h*0.5:.2f}" fill="none" stroke="{ink}" stroke-width="{INK_W}"/>'
    return body


def kitchen_run_vertical(w, h, ink="#182E46"):
    """Counter strip along a vertical wall: sink (top half) + hob (bottom half).
    Fridge is a separate symbol, placed by the caller below/beside this run."""
    body = backing_rect(w, h, 0.6) + outline_rect(w, h, 0.6, ink)
    seg = h / 2
    body += f'<path d="M0 {seg:.2f} H{w:.2f}" fill="none" stroke="{ink}" stroke-width="{INK_W}"/>'
    # sink (oval) in the top segment
    cx, cy = w / 2, seg * 0.5
    rx, ry = w * 0.34, seg * 0.30
    body += f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" fill="none" stroke="{ink}" stroke-width="{INK_W}"/>'
    # hob (4 small circles) in the bottom segment
    cy2 = seg * 1.5
    r = min(w, seg) * 0.15
    offs = [(-1, -1), (1, -1), (-1, 1), (1, 1)]
    for ox, oy in offs:
        body += (f'<circle cx="{w/2 + ox*w*0.20:.2f}" cy="{cy2 + oy*seg*0.18:.2f}" r="{r:.2f}" '
                  f'fill="none" stroke="{ink}" stroke-width="{INK_W*0.8}"/>')
    return body


def toilet(w, h, ink="#182E46"):
    tank_h = h * 0.28
    body = f'<rect x="0" y="0" width="{w:.2f}" height="{tank_h:.2f}" rx="0.6" fill="#FFFFFF" stroke="{ink}" stroke-width="{INK_W}"/>'
    cx = w / 2
    cy = tank_h + (h - tank_h) * 0.56
    rx = w * 0.42
    ry = (h - tank_h) * 0.5
    body += f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" fill="#FFFFFF" stroke="{ink}" stroke-width="{INK_W}"/>'
    return body


def bath_sink(w, h, ink="#182E46"):
    body = backing_rect(w, h, 1.4) + outline_rect(w, h, 1.4, ink)
    body += (f'<ellipse cx="{w/2:.2f}" cy="{h/2:.2f}" rx="{w*0.36:.2f}" ry="{h*0.30:.2f}" '
              f'fill="none" stroke="{ink}" stroke-width="{INK_W}"/>')
    return body


def shower_tray(w, h, ink="#182E46"):
    body = backing_rect(w, h, 1.0) + outline_rect(w, h, 1.0, ink)
    body += (f'<path d="M0 0 L{w:.2f} {h:.2f} M{w:.2f} 0 L0 {h:.2f}" '
              f'fill="none" stroke="{ink}" stroke-width="{INK_W*0.7}"/>')
    body += f'<circle cx="{w/2:.2f}" cy="{h/2:.2f}" r="{min(w,h)*0.06:.2f}" fill="{ink}"/>'
    return body


# ------------------------------------------------------------- door / window --

def door_symbol(hinge, opening_w, swing_dir, wall_normal, ink="#182E46"):
    """hinge: (x,y) point on the wall centerline where the leaf is hinged.
    swing_dir: unit vector along the wall the leaf sweeps FROM (leaf-closed direction).
    wall_normal: unit vector pointing INTO the room (swing direction of the arc).
    Draws leaf line (closed->open @ 90') + quarter-circle arc, 0.5pt."""
    hx, hy = hinge
    sx, sy = swing_dir
    nx, ny = wall_normal
    leaf_end = (hx + sx * opening_w, hy + sy * opening_w)
    open_end = (hx + nx * opening_w, hy + ny * opening_w)
    large_arc = 0
    sweep = 1
    d = (f'M{leaf_end[0]:.2f} {leaf_end[1]:.2f} A{opening_w:.2f} {opening_w:.2f} 0 {large_arc} {sweep} '
         f'{open_end[0]:.2f} {open_end[1]:.2f}')
    body = f'<path d="{d}" fill="none" stroke="{ink}" stroke-width="{INK_W}"/>'
    body += (f'<path d="M{hx:.2f} {hy:.2f} L{open_end[0]:.2f} {open_end[1]:.2f}" '
              f'fill="none" stroke="{ink}" stroke-width="{INK_W}"/>')
    return body


def window_symbol(p0, p1, ink="#182E46", band=1.6):
    """Two thin parallel lines across the opening + end jambs, perpendicular to the
    wall run p0->p1. band = half-thickness of the wall (offset of the two lines)."""
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux
    a0 = (x0 + nx * band, y0 + ny * band)
    a1 = (x1 + nx * band, y1 + ny * band)
    b0 = (x0 - nx * band, y0 - ny * band)
    b1 = (x1 - nx * band, y1 - ny * band)
    body = f'<path d="M{a0[0]:.2f} {a0[1]:.2f} L{a1[0]:.2f} {a1[1]:.2f}" fill="none" stroke="{ink}" stroke-width="{INK_W}"/>'
    body += f'<path d="M{b0[0]:.2f} {b0[1]:.2f} L{b1[0]:.2f} {b1[1]:.2f}" fill="none" stroke="{ink}" stroke-width="{INK_W}"/>'
    body += f'<path d="M{a0[0]:.2f} {a0[1]:.2f} L{b0[0]:.2f} {b0[1]:.2f}" fill="none" stroke="{ink}" stroke-width="{INK_W}"/>'
    body += f'<path d="M{a1[0]:.2f} {a1[1]:.2f} L{b1[0]:.2f} {b1[1]:.2f}" fill="none" stroke="{ink}" stroke-width="{INK_W}"/>'
    return body


# --------------------------------------------------------------- wall build --

def wall_reinforcement(poly, skip_ranges, thickness=3.2, ink="#182E46"):
    """Draw an outward-offset reinforcement band along every edge of `poly`
    except the portions listed in skip_ranges (edge_index -> list of (a,b) sub
    ranges, in the edge's own running parameter 0..edge_length, to omit e.g.
    for a window or door). Guarantees no gaps at the true exterior boundary."""
    off = offset_polygon(poly, thickness / 2.0)
    n = len(poly)
    parts = []
    for i in range(n):
        x0, y0 = off[i % len(off)]
        x1, y1 = off[(i + 1) % len(off)]
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-6:
            continue
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        cuts = sorted(skip_ranges.get(i, []))
        cursor = 0.0
        segs = []
        for a, b in cuts:
            a = max(0.0, min(length, a))
            b = max(0.0, min(length, b))
            if a > cursor:
                segs.append((cursor, a))
            cursor = max(cursor, b)
        if cursor < length:
            segs.append((cursor, length))
        for a, b in segs:
            if b - a < 0.05:
                continue
            p0 = (x0 + ux * a, y0 + uy * a)
            p1 = (x0 + ux * b, y0 + uy * b)
            parts.append(
                f'<path d="M{p0[0]:.2f} {p0[1]:.2f} L{p1[0]:.2f} {p1[1]:.2f}" '
                f'fill="none" stroke="{ink}" stroke-width="{thickness}" stroke-linecap="square"/>'
            )
    return "".join(parts)


# A handful of source paths in c12 near this unit are small free-floating
# CAD glyphs (icons/annotation symbols), not wall or column mass — e.g. one
# ~5x5pt blob at (183.8,264.8)-(189.0,270.0), nowhere near the poly boundary,
# which reads as a solid-filled decorative icon once rendered (looked, at
# this drawing's scale, like a little emblem — not plan-relevant). c08/c12/
# c14 paths are legitimate walls/columns everywhere else near this unit
# (verified against the poly boundary), so this is excluded by exact bbox
# rather than dropping the clusters wholesale.
_ICON_EXCLUDE_BBOXES = [
    (183.0, 264.0, 190.0, 271.0),
]


def _is_excluded_icon(bbox):
    if bbox is None:
        return False
    x0, y0, x1, y1 = bbox
    for ex0, ey0, ex1, ey1 in _ICON_EXCLUDE_BBOXES:
        if x0 >= ex0 and y0 >= ey0 and x1 <= ex1 and y1 <= ey1:
            return True
    return False


def cluster_fills_and_lines(floor_data, bounds, ink="#182E46"):
    """Real extracted wall fills (c08/c12/c14 only), filtered to paths whose
    bbox overlaps `bounds`. c07 is deliberately NOT passed through raw here:
    on this floor, near this unit, c07 mixes genuine wall-return segments
    with an unrelated circular icon (fit as a near-perfect circle, r=10.6pt,
    centered ~166,203 — a fixture/decor symbol, not a partition), and there
    is no clean way to separate the two from geometry alone. The defensive
    perimeter reinforcement band (drawn by the caller) plus these solid
    fills fully reconstruct the walls without that noise."""
    clusters = {c["id"]: c for c in floor_data["clusters"]}
    parts = []
    for cid in ("c08", "c12", "c14"):
        c = clusters.get(cid)
        if not c:
            continue
        kept = [
            p for p in c["paths"]
            if bbox_overlap(path_bbox(p), bounds, pad=0.0) and not _is_excluded_icon(path_bbox(p))
        ]
        if kept:
            d = " ".join(kept)
            parts.append(f'<path d="{d}" fill="{ink}" stroke="none" fill-rule="nonzero"/>')
    return "".join(parts)


# ------------------------------------------------------------------ layout --
# Example of a hand layout, kept as it ran: the coordinates below fit one real unit only.
# Hand-placed layout for unit 1005 (floor 10), derived from: real poly/balcony
# geometry, the real window gap found in c08 (top wall, x 185.6-199.7), and
# architectural inference for the rest (this studio's source data has no
# separate bathroom polygon / no unambiguous per-door arcs in c01 for this
# unit — c01 near this unit is dominated by a large, unrelated section-marker
# circle, confirmed by circle-fit, radius ~120pt). See task notes.

def layout_1005(poly, balcony, pt_per_m):
    L = {}
    L["window"] = {"edge": 17, "range": (202.6 - 199.7, 202.6 - 185.6)}  # param along edge (dir: x decreasing)
    L["door_entry"] = {"edge": 2, "range": (230.0 - 228.3, 242.0 - 228.3)}
    L["wardrobe"] = (150.1, 188.2, 161.4 - 150.1, 197.3 - 188.2)
    L["bed"] = (180.0, 188.2, 201.2 - 180.0, 216.0 - 188.2)
    L["nightstand"] = (162.9, 188.2, 179.9 - 162.9, 197.0 - 188.2)
    L["table"] = (163.0, 227.0, 175.0 - 163.0, 238.0 - 227.0)
    L["chairs"] = [(157.0, 231.0), (181.0, 231.0)]
    L["wc"] = (189.0, 236.0, 202.6 - 189.0, 262.0 - 236.0)
    L["toilet"] = (192.5, 238.8, 7.5, 9.0)
    L["shower"] = (191.5, 250.5, 9.5, 10.0)
    L["kitchen"] = (194.0, 264.0, 202.6 - 194.0, 17.0)
    L["fridge"] = (194.0, 281.0, 202.6 - 194.0, 8.0)
    L["balcony_band"] = (151.5, 202.6, 295.2, 297.4)
    L["balcony_door"] = {"hinge": (156.0, 295.2), "w": 12.0}
    return L


def build_unit_svg(floor_data, unit_number, unit_info, style, pt_per_m):
    poly = floor_data["units"][unit_number]["poly"]
    balcony = floor_data["units"][unit_number].get("balcony")
    ink = style["ink"]
    floor_fill = style["floor_fill"]
    font = f"{style['labels']['font']}, Georgia, 'Noto Serif', serif"

    crop_polys = [poly] + ([balcony] if balcony else [])
    x0, y0, x1, y1 = bbox_of(crop_polys)
    margin = 10.0
    cx0, cy0, cx1, cy1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
    cw, ch = cx1 - cx0, cy1 - cy0

    clip_offset = 4.0
    clip_shapes = [path_d(offset_polygon(poly, clip_offset))]
    if balcony:
        clip_shapes.append(path_d(offset_polygon(balcony, clip_offset)))
    clip_id = f"crop-{unit_number}"
    defs = f'<defs><clipPath id="{clip_id}">' + "".join(f'<path d="{d}"/>' for d in clip_shapes) + "</clipPath></defs>"

    body = f'<rect x="{cx0:.2f}" y="{cy0:.2f}" width="{cw:.2f}" height="{ch:.2f}" fill="#FFFFFF"/>'

    inner = ""
    # floor
    inner += f'<polygon points="{pts_attr(poly)}" fill="{floor_fill}" stroke="none"/>'
    if balcony:
        inner += f'<polygon points="{pts_attr(balcony)}" fill="{floor_fill}" fill-opacity="0.6" stroke="none"/>'

    L = layout_1005(poly, balcony, pt_per_m)

    # defensive perimeter reinforcement (skip window + entry-door portions)
    skip = {L["window"]["edge"]: [L["window"]["range"]], L["door_entry"]["edge"]: [L["door_entry"]["range"]]}
    inner += wall_reinforcement(poly, skip, thickness=3.2, ink=ink)

    # real extracted wall geometry on top (more accurate where present)
    wall_bounds = (cx0 - 6, cy0 - 6, cx1 + 6, cy1 + 6)
    inner += cluster_fills_and_lines(floor_data, wall_bounds, ink=ink)

    # re-open the window + entry-door gaps that the real geometry might have
    # covered again (its data includes solid pieces flanking each opening,
    # not the opening itself, so nothing further is needed) — draw the
    # window + door symbols now, on top of everything so they read cleanly.
    wx0, wy0 = 202.6 - L["window"]["range"][0], 181.9
    wx1, wy1 = 202.6 - L["window"]["range"][1], 181.9
    inner += window_symbol((wx0, wy0), (wx1, wy1), ink=ink, band=1.6)

    ex, ey0 = 150.1, 228.3 + L["door_entry"]["range"][0]
    ey1 = 228.3 + L["door_entry"]["range"][1]
    door_w = ey1 - ey0
    inner += door_symbol((ex, ey1), door_w, swing_dir=(0, -1), wall_normal=(1, 0), ink=ink)

    # balcony band: ribbon window across, + one operable door leaf
    bx0, bx1, by0, by1 = L["balcony_band"]
    inner += window_symbol((bx0, (by0 + by1) / 2), (bx1, (by0 + by1) / 2), ink=ink, band=1.1)
    hinge = L["balcony_door"]["hinge"]
    dw = L["balcony_door"]["w"]
    inner += door_symbol(hinge, dw, swing_dir=(1, 0), wall_normal=(0, -1), ink=ink)

    # WC partition (thin) — north + west sides of the nook
    wcx, wcy, wcw, wch = L["wc"]
    inner += (f'<path d="M{wcx:.2f} {wcy:.2f} H{wcx+wcw:.2f} M{wcx:.2f} {wcy:.2f} V{wcy+wch:.2f}" '
              f'fill="none" stroke="{ink}" stroke-width="1.4" stroke-linecap="square"/>')
    # small WC door on the west partition, swinging OUT into the corridor
    # (away from the toilet/shower, which sit right against that partition)
    wc_hinge = (wcx, wcy + wch - 4.0)
    inner += door_symbol(wc_hinge, 7.0, swing_dir=(0, -1), wall_normal=(-1, 0), ink=ink)

    # -- furniture --
    x, y, w, h = L["wardrobe"]
    inner += _g(x, y, wardrobe(w, h, ink))
    x, y, w, h = L["bed"]
    inner += _g(x, y, bed(w, h, ink))
    x, y, w, h = L["nightstand"]
    inner += _g(x, y, nightstand(w, h, ink))
    x, y, w, h = L["table"]
    inner += _g(x, y, dining_table(w, h, ink))
    for (ccx, ccy) in L["chairs"]:
        s = 5.5
        inner += _g(ccx - s / 2, ccy - s / 2, chair(s, ink))
    x, y, w, h = L["toilet"]
    inner += _g(x, y, toilet(w, h, ink))
    x, y, w, h = L["shower"]
    inner += _g(x, y, shower_tray(w, h, ink))
    x, y, w, h = L["kitchen"]
    inner += _g(x, y, kitchen_run_vertical(w, h, ink))

    body += f'<g clip-path="url(#{clip_id})">' + inner + "</g>"

    # -- labels --
    # This unit is tall & narrow (interior ~46pt wide) unlike the wider reference
    # studio, so the "big free rectangle" for the title block is a slim corridor
    # strip between the west wall and the WC/kitchen block (x~152-186, y~248-286)
    # rather than a literal 90x36pt box. The title is stacked on three short
    # lines (measured with Chromium's own getBBox: "Студия" @9pt ~30pt wide,
    # "1005" @9pt ~17pt, "34,2 м²" @8pt ~21pt) so nothing overflows into the
    # WC partition at x=189.
    title_pt = 9.0
    area_pt = 8.0
    room_pt = 6.0
    corner_x = 151.5
    corner_top_y = 248.0
    corner_bot_y = 284.0
    corner_x2 = 186.0
    body += (f'<path d="M{corner_x:.2f} {corner_top_y:.2f} V{corner_bot_y:.2f} H{corner_x2:.2f}" '
              f'fill="none" stroke="{ink}" stroke-width="0.5"/>')
    text_x = corner_x + 3.5
    line1_y = corner_top_y + 11.0
    line2_y = line1_y + 12.0
    line3_y = line2_y + 11.0
    body += (f'<text x="{text_x:.2f}" y="{line1_y:.2f}" font-family="{font}" font-weight="400" '
              f'font-size="{title_pt}" fill="{ink}">Студия</text>')
    body += (f'<text x="{text_x:.2f}" y="{line2_y:.2f}" font-family="{font}" font-weight="400" '
              f'font-size="{title_pt}" fill="{ink}">{unit_number}</text>')
    total = unit_info["total"]
    body += (f'<text x="{text_x:.2f}" y="{line3_y:.2f}" font-family="{font}" font-weight="400" '
              f'font-size="{area_pt}" fill="{ink}">{area_markup(total)}</text>')

    # room areas: main open room (living total minus the WC nook we drew) + WC + balcony
    wc_area_m2 = (wcw * wch) / (pt_per_m ** 2)
    main_area_m2 = unit_info["living"] - wc_area_m2
    body += (f'<text x="163.0" y="222.0" font-family="{font}" font-weight="400" '
              f'font-size="{room_pt}" fill="{ink}">{area_markup(main_area_m2)}</text>')
    body += (f'<text x="{wcx+wcw/2:.2f}" y="{wcy-2.5:.2f}" text-anchor="middle" font-family="{font}" font-weight="400" '
              f'font-size="5.0" fill="{ink}">{area_markup(wc_area_m2)}</text>')
    if balcony and unit_info.get("balcony") is not None:
        bxc, byc = polygon_centroid(balcony)
        body += (f'<text x="{bxc:.2f}" y="{byc:.2f}" text-anchor="middle" font-family="{font}" font-weight="400" '
                  f'font-size="{room_pt}" fill="{ink}">{area_markup(unit_info["balcony"])}</text>')

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{cw:.2f}" height="{ch:.2f}" '
        f'viewBox="{cx0:.2f} {cy0:.2f} {cw:.2f} {ch:.2f}">{defs}{body}</svg>'
    )
    return svg, (cx0, cy0, cw, ch)


# ------------------------------------------------------------------ render --

async def rasterize(page, svg, out_w, out_h):
    html = (
        "<!doctype html><html><head><meta charset=\"utf-8\">" + FONT_LINK +
        "<style>html,body{margin:0;padding:0;background:#fff}svg{display:block}</style>"
        "</head><body>" + svg + "</body></html>"
    )
    await page.set_viewport_size({"width": out_w, "height": out_h})
    await page.set_content(html, wait_until="load")
    try:
        await page.evaluate("document.fonts.load('16px \"Instrument Serif\"').then(()=>document.fonts.ready)")
    except Exception:
        pass
    await page.wait_for_timeout(150)
    png_bytes = await page.locator("svg").screenshot()
    return png_bytes


def make_compare(our_png_path, ref_path, out_path):
    ours = Image.open(our_png_path).convert("RGB")
    ref = Image.open(ref_path).convert("RGB")
    target_h = 1400
    def resize_to_h(im, h):
        w = round(im.width * h / im.height)
        return im.resize((w, h), Image.LANCZOS)
    ours_r = resize_to_h(ours, target_h)
    ref_r = resize_to_h(ref, target_h)
    gap = 24
    total_w = ours_r.width + ref_r.width + gap
    canvas = Image.new("RGB", (total_w, target_h), "#FFFFFF")
    canvas.paste(ours_r, (0, 0))
    canvas.paste(ref_r, (ours_r.width + gap, 0))
    canvas.save(out_path)


async def run(unit_number):
    style = load_json(STYLE_PATH)
    units_db = {u["number"]: u for u in load_json(UNITS_JSON_PATH)["units"]}
    info = units_db[unit_number]
    floor_num = info["floor"]
    floor_data = load_json(DATA_DIR / f"floor-{floor_num}.json")

    balcony = floor_data["units"][unit_number].get("balcony")
    bal_area_m2 = info["balcony"]
    bal_area_pt2 = shoelace_area(balcony + [balcony[0]])
    pt_per_m = math.sqrt(bal_area_pt2 / bal_area_m2)

    svg, _ = build_unit_svg(floor_data, unit_number, info, style, pt_per_m)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    svg_path = OUT_DIR / f"unit-{unit_number}.svg"
    svg_path.write_text(svg, encoding="utf-8")

    px_per_pt = 4
    m = re.search(r'viewBox="[-\d.]+ [-\d.]+ ([\d.]+) ([\d.]+)"', svg)
    vw, vh = float(m.group(1)), float(m.group(2))
    out_w, out_h = round(vw * px_per_pt), round(vh * px_per_pt)
    # svg element's own width/height are in pt (== crop size); for a crisp
    # raster at px_per_pt, re-tag them to the target pixel size (viewBox,
    # which controls the coordinate mapping, is untouched).
    raster_svg = re.sub(
        r'^<svg xmlns="[^"]+" width="[^"]+" height="[^"]+"',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{out_w}" height="{out_h}"',
        svg,
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        png_bytes = await rasterize(page, raster_svg, out_w, out_h)
        await browser.close()

    png_path = OUT_DIR / f"unit-{unit_number}.png"
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")  # no palette
    im.save(png_path)

    compare_path = OUT_DIR / f"compare-{unit_number}.png"
    if REF_IMG.exists():
        make_compare(png_path, REF_IMG, compare_path)

    print(f"pt_per_m = {pt_per_m:.4f}")
    print(f"wrote {svg_path}")
    print(f"wrote {png_path} ({out_w}x{out_h})")
    print(f"wrote {compare_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("unit", help="unit number, e.g. 1005")
    args = ap.parse_args()
    asyncio.run(run(args.unit))


if __name__ == "__main__":
    main()
