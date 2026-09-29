#!/usr/bin/env python3
"""
Batch-render floor plans and unit layouts in a single approved style (plan-studio/style.json)
via SVG -> headless Chromium (Playwright) -> PNG, replacing the site's plan images.

Usage:
  python tools/render-plans.py --floor 10                 # test: floor 10 plan + its units (no plans.json rewrite)
  python tools/render-plans.py --units 1001,1005,1009      # test: just these unit crops (no plans.json rewrite)
  python tools/render-plans.py --all                       # full batch: every floor + every unit + rewrite plans.json/plans.js

Inputs:
  plan-studio/data/floor-<N>.json, plan-studio/data/index.json
  site-assets/data/units.json
  plan-studio/style.json

Outputs (REPLACES existing files):
  site/assets/plans/floor-<N>.png (+ .svg)
  site/assets/plans/unit-<number>.png (+ .svg)
  site/assets/plans/plans.json (--all only)
  site/data/plans.js (--all only)
"""
import argparse
import io
import json
import math
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.async_api import async_playwright
import asyncio
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
DATA_DIR = ROOT / "plan-studio" / "data"
STYLE_PATH = fpconfig.STYLE
UNITS_JSON_PATH = fpconfig.SCHEDULE
OUT_DIR = ROOT / "site" / "assets" / "plans"
PLANS_JSON_PATH = OUT_DIR / "plans.json"
PLANS_JS_PATH = ROOT / "site" / "data" / "plans.js"

FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:wght@400&display=swap" rel="stylesheet">'
)

TYPE_TITLE = {"studio": "Студия", "1br": "1-комн.", "2br": "2-комн.", "3br": "3-комн."}

# floor draw order: floor fill -> (type overlay injected between these two by render_floor) ->
# walls/columns -> walls_outline -> partitions -> furniture bodies (ex-c06 small shapes) ->
# furniture/fixture line-art (+ c01 dashed) -> labels
ROLE_ORDER = [
    ("floor",),
    ("walls", "columns"),
    ("walls_outline",),
    ("partitions",),
    ("furniture_body",),
    ("furniture", "fixtures", "dashed"),
]

_NUM_RE = re.compile(r"-?\d+\.?\d*")


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def fmt_area(x):
    if x is None:
        return ""
    return f"{x:.1f}".replace(".", ",") + " м²"


def polygon_centroid(pts):
    """Area-weighted centroid of a simple polygon; falls back to vertex average for degenerate shapes."""
    n = len(pts)
    if n == 0:
        return 0.0, 0.0
    A = 0.0
    Cx = 0.0
    Cy = 0.0
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


def bbox_of(point_lists):
    xs = []
    ys = []
    for pts in point_lists:
        for x, y in pts:
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def build_unit_adjacency(units, tol=2.5):
    """Two units are adjacent if their polygons touch (or nearly touch - a shared wall's own
    thickness sits between the two interior-face polygons, so exact edge contact is rare) -
    approximated as: their bboxes still overlap once each is padded by `tol` on every side.
    Returns {unit_number: set(other_unit_numbers)}."""
    nums = list(units.keys())
    bboxes = {}
    for num in nums:
        poly = units[num].get("poly")
        if not poly:
            continue
        bboxes[num] = bbox_of([poly])
    adj = {num: set() for num in nums}
    for i in range(len(nums)):
        a = nums[i]
        if a not in bboxes:
            continue
        ax0, ay0, ax1, ay1 = bboxes[a]
        ax0, ay0, ax1, ay1 = ax0 - tol, ay0 - tol, ax1 + tol, ay1 + tol
        for j in range(i + 1, len(nums)):
            b = nums[j]
            if b not in bboxes:
                continue
            bx0, by0, bx1, by1 = bboxes[b]
            if ax1 < bx0 or ax0 > bx1 or ay1 < by0 or ay0 > by1:
                continue
            adj[a].add(b)
            adj[b].add(a)
    return adj


def assign_unit_fill_colors(units, palette, tol=2.5):
    """Greedy-colour the unit adjacency graph, in unit-number order, so no two units that
    share a wall (or sit within `tol` of each other) get the same fill - the alternate to
    colouring purely by unit type, which merges same-type neighbours into one visual blob.
    Returns {unit_number: hex_color}; also returns the adjacency dict for verification."""
    adj = build_unit_adjacency(units, tol)
    try:
        order = sorted(units.keys(), key=lambda n: int(n))
    except ValueError:
        order = sorted(units.keys())
    colors = {}
    for num in order:
        used = {colors[nb] for nb in adj.get(num, ()) if nb in colors}
        choice = next((c for c in palette if c not in used), palette[0])
        colors[num] = choice
    return colors, adj


def _path_bbox(d):
    nums = [float(n) for n in _NUM_RE.findall(d)]
    if not nums:
        return None
    xs, ys = nums[0::2], nums[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def _path_points(d):
    """All (x,y) pairs referenced by a path's d string, in order. Cubic-bezier control points
    are treated as ordinary vertices — an approximation that's fine here (furniture curves are
    gentle; wall/partition paths are straight)."""
    nums = [float(n) for n in _NUM_RE.findall(d)]
    return list(zip(nums[0::2], nums[1::2]))


def _cluster_width(spec, context):
    return spec.get(f"{context}_width", spec.get("width", 0.5))


def _cluster_visible(spec, context):
    return spec.get(f"{context}_visible", spec.get("visible", False))


def cluster_layers_svg(floor_data, style, bounds=None, context="floor", only_roles=None):
    """Layered markup for visible clusters, in the mandated draw order.
    `bounds` (x0,y0,x1,y1), if given, drops paths whose bbox doesn't touch it — used for
    unit crops so the saved SVG doesn't embed the whole floor's geometry behind the clip.
    `context` selects per-context stroke widths/visibility (floor plans vs. unit crops use
    different weights — see cluster `floor_width`/`unit_width`/`floor_visible`/`unit_visible`).
    `only_roles`, if given, restricts drawing to those ROLE_ORDER groups (used to interleave
    the per-unit colour overlay / synthetic wall bands between drawing stages)."""
    clusters_by_id = {c["id"]: c for c in floor_data["clusters"]}
    style_clusters = style["clusters"]
    parts = []
    filter_defs = {}  # id -> radius, for clusters that need gap-closing (hatch-style strokes)
    pad = 2.0
    for roles in ROLE_ORDER:
        if only_roles is not None and roles not in only_roles:
            continue
        ids = sorted(cid for cid, spec in style_clusters.items() if _cluster_visible(spec, context) and spec.get("role") in roles)
        for cid in ids:
            c = clusters_by_id.get(cid)
            if not c or not c.get("paths"):
                continue
            spec = style_clusters[cid]
            paths = c["paths"]
            if bounds is not None:
                bx0, by0, bx1, by1 = bounds
                kept = []
                for p in paths:
                    pb = _path_bbox(p)
                    if pb is None:
                        continue
                    px0, py0, px1, py1 = pb
                    if px1 >= bx0 - pad and px0 <= bx1 + pad and py1 >= by0 - pad and py0 <= by1 + pad:
                        kept.append(p)
                paths = kept
            if not paths:
                continue
            d = " ".join(paths)
            fill = spec.get("fill")
            stroke = spec.get("stroke")
            attrs = []
            if fill and stroke:
                # furniture "bodies" (ex-c06 small shapes): white fill + thin ink outline
                w = _cluster_width(spec, context)
                attrs = [f'fill="{fill}"', f'stroke="{stroke}"', f'stroke-width="{w}"', 'stroke-linejoin="round"']
            elif fill:
                attrs = [f'fill="{fill}"', 'stroke="none"', 'fill-rule="nonzero"']
            elif stroke:
                w = _cluster_width(spec, context)
                cap = spec.get("linecap", "round")
                attrs = [f'fill="none"', f'stroke="{stroke}"', f'stroke-width="{w}"', 'stroke-linejoin="round"', f'stroke-linecap="{cap}"']
                dash = spec.get("dasharray")
                if dash:
                    attrs.append(f'stroke-dasharray="{dash}"')
            else:
                continue
            markup = f'<path d="{d}" {" ".join(attrs)}/>'
            # Some clusters (e.g. c03) are a 45deg hatch texture drawn as many short,
            # disconnected diagonal ticks representing wall fill/insulation in section —
            # not a continuous outline. A plain dilate closes the gaps but permanently
            # fattens the band; a morphological CLOSE (dilate then erode a bit less)
            # bridges the same gaps without growing the line past its real thickness.
            dr = spec.get("close_dilate_pt")
            if dr:
                fid = "close-" + cid
                filter_defs[fid] = (dr, spec.get("close_erode_pt", 0))
                markup = f'<g filter="url(#{fid})">{markup}</g>'
            parts.append(markup)
    defs_parts = []
    for fid, (dr, er) in filter_defs.items():
        prims = f'<feMorphology operator="dilate" radius="{dr}"' + (' result="d"/>' if er else '/>')
        if er:
            prims += f'<feMorphology operator="erode" radius="{er}" in="d"/>'
        defs_parts.append(
            f'<filter id="{fid}" x="-50%" y="-50%" width="200%" height="200%" primitiveUnits="userSpaceOnUse">{prims}</filter>'
        )
    defs = "".join(defs_parts)
    if defs:
        defs = f"<defs>{defs}</defs>"
    return defs + "".join(parts)


def points_attr(pts):
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)


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
    lines = []   # offset line for edge i: (px, py, ux, uy)
    normals = []  # unit outward normal for edge i: (nx, ny)
    verts = list(pts)
    for i in range(n):
        x0, y0 = verts[i]
        x1, y1 = verts[(i + 1) % n]
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
            # sharp/degenerate corner: fall back to a simple averaged-normal push
            # (bounded excursion, avoids runaway miter spikes on acute reflex corners)
            anx = normals[i - 1][0] + normals[i][0]
            any_ = normals[i - 1][1] + normals[i][1]
            alen = math.hypot(anx, any_) or 1.0
            pt = (ox + anx / alen * delta, oy + any_ / alen * delta)
        out.append(pt)
    return out


def offset_polygon(pts, delta):
    """Push a simple polygon's boundary outward by `delta` (edge-translate + re-intersect).
    Works for both axis-aligned and diagonal edges. Orientation-agnostic: tries both normal
    directions and keeps whichever grows the polygon (outward offset must increase area)."""
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
        return pts  # degenerate; don't shrink
    return best


def path_d(pts):
    if not pts:
        return ""
    d = f"M{pts[0][0]:.2f} {pts[0][1]:.2f} "
    d += " ".join(f"L{x:.2f} {y:.2f}" for x, y in pts[1:])
    d += " Z"
    return d


def _point_in_poly(pt, poly):
    x, y = pt
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            denom = (yj - yi) or 1e-12
            xint = (xj - xi) * (y - yi) / denom + xi
            if x < xint:
                inside = not inside
        j = i
    return inside


def _subdivide_edges(poly, max_len=15.0):
    segs = []
    n = len(poly)
    for i in range(n):
        p1, p2 = poly[i], poly[(i + 1) % n]
        length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        if length < 1e-6:
            continue
        k = max(1, math.ceil(length / max_len))
        for j in range(k):
            t0, t1 = j / k, (j + 1) / k
            a = (p1[0] + (p2[0] - p1[0]) * t0, p1[1] + (p2[1] - p1[1]) * t0)
            b = (p1[0] + (p2[0] - p1[0]) * t1, p1[1] + (p2[1] - p1[1]) * t1)
            segs.append((a, b))
    return segs


def synth_wall_bands(poly, c08_polys, band_w, check_pt):
    """Where an edge of the unit's own polygon has no c08 (solid wall fill) within
    `check_pt`, that edge is only backed by the c03 hatch texture (now switched off on unit
    crops) — draw a solid ink band of width `band_w` along that edge, just outside the
    contour, so the unit's perimeter reads as continuously solid on all sides."""
    bands = []
    for p1, p2 in _subdivide_edges(poly):
        length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        if length < 1e-6:
            continue
        dx, dy = (p2[0] - p1[0]) / length, (p2[1] - p1[1]) / length
        n1, n2 = (dy, -dx), (-dy, dx)
        mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
        test1 = (mx + n1[0] * 0.5, my + n1[1] * 0.5)
        outward = n1 if not _point_in_poly(test1, poly) else n2
        covered = False
        for t in (0.25, 0.5, 0.75):
            sx, sy = p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t
            tx, ty = sx + outward[0] * check_pt * 0.7, sy + outward[1] * check_pt * 0.7
            if any(_point_in_poly((tx, ty), cp) for cp in c08_polys):
                covered = True
                break
        if covered:
            continue
        ext = band_w * 0.5  # slight tangential extension so adjacent bands overlap at corners
        ex, ey = dx * ext, dy * ext
        q1 = (p1[0] - ex, p1[1] - ey)
        q2 = (p2[0] + ex, p2[1] + ey)
        q3 = (q2[0] + outward[0] * band_w, q2[1] + outward[1] * band_w)
        q4 = (q1[0] + outward[0] * band_w, q1[1] + outward[1] * band_w)
        bands.append([q1, q2, q3, q4])
    return bands


def find_wall_gaps(poly, c08_polys, min_gap=9.0, max_gap=16.0, sample_step=0.5, check_pt=2.0):
    """Sample a unit polygon's own boundary and find contiguous runs where there's no c08
    (solid wall fill) just outside the edge — i.e. an opening only ever given as the thin c03
    hatch texture. Returns a list of (jamb1, jamb2, outward_dir) for runs whose length lands
    in [min_gap, max_gap] (door-width openings; wider runs are windows/open frontage, not
    filtered here since callers only care about door-scale gaps)."""
    n = len(poly)
    samples = []
    for i in range(n):
        p1, p2 = poly[i], poly[(i + 1) % n]
        length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        if length < 1e-6:
            continue
        steps = max(1, int(length / sample_step))
        dx, dy = (p2[0] - p1[0]) / length, (p2[1] - p1[1]) / length
        n1, n2 = (dy, -dx), (-dy, dx)
        mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
        outward = n1 if not _point_in_poly((mx + n1[0] * 0.5, my + n1[1] * 0.5), poly) else n2
        for s in range(steps):
            t = s / steps
            pt = (p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t)
            samples.append((pt, outward))
    covered = []
    for pt, outward in samples:
        tx, ty = pt[0] + outward[0] * check_pt * 0.7, pt[1] + outward[1] * check_pt * 0.7
        covered.append(any(_point_in_poly((tx, ty), cp) for cp in c08_polys))
    gaps = []
    i, N = 0, len(samples)
    while i < N:
        if not covered[i]:
            j = i
            while j < N and not covered[j]:
                j += 1
            run_len = (j - i) * sample_step
            if min_gap <= run_len <= max_gap:
                gaps.append((samples[i][0], samples[j - 1][0], samples[(i + j) // 2][1]))
            i = j
        else:
            i += 1
    return gaps


def synth_door_swing(jamb1, jamb2, outward, arc_segments=14):
    """One door-swing symbol: leaf (open) hinged at jamb1, quarter-circle arc back to jamb2,
    swinging inward (away from `outward`) per convention for an entrance opening. Returns
    (leaf_points, arc_points)."""
    w = math.hypot(jamb2[0] - jamb1[0], jamb2[1] - jamb1[1])
    inward = (-outward[0], -outward[1])
    leaf_end = (jamb1[0] + inward[0] * w, jamb1[1] + inward[1] * w)
    a0 = math.atan2(inward[1], inward[0])
    a1 = math.atan2(jamb2[1] - jamb1[1], jamb2[0] - jamb1[0])
    delta = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    arc_pts = []
    for k in range(arc_segments + 1):
        a = a0 + delta * (k / arc_segments)
        arc_pts.append((jamb1[0] + math.cos(a) * w, jamb1[1] + math.sin(a) * w))
    return [jamb1, leaf_end], arc_pts


def _parse_door_arc_path(d):
    """Parse a c00 path string ('M x y C x1 y1 x2 y2 x3 y3 [C ...]') into (S, ctrl1, E):
    the starting point, the first Bezier control point, and the endpoint of the LAST
    non-degenerate C segment. Returns (None, None, None) if the path has no curve."""
    cmds = re.findall(r"[MLC]|-?\d+\.?\d*", d)
    segs = []
    cur = None
    vals = []
    for tok in cmds:
        if tok in ("M", "L", "C"):
            if cur is not None:
                segs.append((cur, vals))
            cur, vals = tok, []
        else:
            vals.append(float(tok))
    if cur is not None:
        segs.append((cur, vals))
    S = ctrl1 = E = None
    for cmd, vals in segs:
        pts = list(zip(vals[0::2], vals[1::2]))
        if not pts:
            continue
        if cmd == "M":
            S = pts[0]
        elif cmd == "C" and len(pts) >= 3:
            if ctrl1 is None:
                ctrl1 = pts[0]
            E = pts[2]
    return S, ctrl1, E


def _point_seg_dist(pt, a, b):
    ax, ay = a
    bx, by = b
    px, py = pt
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def extract_pdf_doors(fd, r_min=5.0, r_max=45.0, radius_tol=1.0, wall_tol=2.5):
    """The architect's real door-swing symbols (quarter-circle arc, sometimes with a
    straight open-leaf line) live in cluster c00 - a cluster the previous pass mislabelled
    'annotations' and never rendered, so every door in this project was being synthesised
    from wall-gap heuristics instead of read off the PDF. c00 also carries dimension witness
    lines (also orange, same stroke/width bucket), so only paths with a real Bezier curve
    ('C' command) are candidate arcs; a straight 2-point line is never a door by itself here.

    For an arc from S to E, the true quarter-circle centre (hinge) is one of the two axis-
    aligned corners of its bounding box: (S.x, E.y) or (E.x, S.y). The correct one is found by
    checking which corner makes the first control point's tangent perpendicular to the radius
    at S (true for a real circular arc's Bezier approximation, not for the wrong corner).
    Whichever of {S, E} sits closer to some wall geometry (a unit/balcony polygon edge, a c07
    partition line, or a c08/c12/c14 wall-fill edge) is `jamb2` (the gap's other edge, in the
    wall); the other is `leaf_tip` (the open leaf's swept-to position, off the wall into the
    room) - `hinge` and `jamb2` then bound the actual doorway gap to cut in a wall band.
    Returns a list of {hinge, jamb2, leaf_tip, r, outward} dicts, floor-wide (all door types:
    entrance, balcony/loggia, interior)."""
    c00 = next((c for c in fd["clusters"] if c["id"] == "c00"), None)
    if not c00 or not c00.get("paths"):
        return []

    wall_segs = []
    for u in fd["units"].values():
        for key in ("poly", "balcony"):
            poly = u.get(key)
            if poly:
                n = len(poly)
                for i in range(n):
                    wall_segs.append((tuple(poly[i]), tuple(poly[(i + 1) % n])))
    for cid in ("c07", "c08", "c12", "c14", "c03"):
        cl = next((x for x in fd["clusters"] if x["id"] == cid), None)
        if not cl:
            continue
        for p in cl.get("paths", []):
            pts = _path_points(p)
            for i in range(len(pts) - 1):
                wall_segs.append((pts[i], pts[i + 1]))

    def wall_dist(pt):
        best = 1e9
        for a, b in wall_segs:
            dd = _point_seg_dist(pt, a, b)
            if dd < best:
                best = dd
            if best < 0.1:
                break
        return best

    doors = []
    for p in c00["paths"]:
        if "C" not in p:
            continue
        S, ctrl1, E = _parse_door_arc_path(p)
        if S is None or ctrl1 is None or E is None:
            continue
        if math.hypot(E[0] - S[0], E[1] - S[1]) < 0.5:
            continue
        best_hinge, best_score = None, None
        for C in ((S[0], E[1]), (E[0], S[1])):
            r1 = math.hypot(C[0] - S[0], C[1] - S[1])
            r2 = math.hypot(C[0] - E[0], C[1] - E[1])
            if not (r_min <= r1 <= r_max) or abs(r1 - r2) > radius_tol:
                continue
            v1 = (ctrl1[0] - S[0], ctrl1[1] - S[1])
            v2 = (S[0] - C[0], S[1] - C[1])
            score = abs(v1[0] * v2[0] + v1[1] * v2[1])
            if best_score is None or score < best_score:
                best_score, best_hinge = score, C
        if best_hinge is None:
            continue
        hinge = best_hinge
        r = math.hypot(hinge[0] - S[0], hinge[1] - S[1])
        dS, dE = wall_dist(S), wall_dist(E)
        jamb2, leaf_tip = (S, E) if dS <= dE else (E, S)
        if min(dS, dE) > wall_tol * 4:
            continue  # neither end is near any wall - not a real door, skip
        li = math.hypot(leaf_tip[0] - hinge[0], leaf_tip[1] - hinge[1]) or 1.0
        outward = ((hinge[0] - leaf_tip[0]) / li, (hinge[1] - leaf_tip[1]) / li)
        doors.append({"hinge": hinge, "jamb2": jamb2, "leaf_tip": leaf_tip, "r": r, "outward": outward})
    return doors


def _cut_edge_at_doors(p1, p2, doors, wall_tol=1.2, min_seg=0.5):
    """Split wall edge p1->p2 into sub-segments with any door gap (hinge<->jamb2, collinear
    with and inside this edge) removed. Returns [(a, b, is_original_start, is_original_end)]
    so callers know which cut ends are true polygon corners (get the mitring tangential
    extension) vs a fresh door-jamb cut (flush, no extension)."""
    length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    if length < 1e-6:
        return [(p1, p2, True, True)]
    ux, uy = (p2[0] - p1[0]) / length, (p2[1] - p1[1]) / length

    def param(pt):
        return (pt[0] - p1[0]) * ux + (pt[1] - p1[1]) * uy

    def perp(pt):
        return abs((pt[0] - p1[0]) * (-uy) + (pt[1] - p1[1]) * ux)

    cuts = []
    for d in doors:
        a, b = d["hinge"], d["jamb2"]
        if perp(a) > wall_tol or perp(b) > wall_tol:
            continue
        ta, tb = param(a), param(b)
        t0, t1 = min(ta, tb), max(ta, tb)
        if t1 < 0.3 or t0 > length - 0.3:
            continue
        cuts.append((max(t0, 0.0), min(t1, length)))
    if not cuts:
        return [(p1, p2, True, True)]
    cuts.sort()
    merged = []
    for t0, t1 in cuts:
        if merged and t0 <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], t1))
        else:
            merged.append((t0, t1))

    def pt_at(t):
        return (p1[0] + ux * t, p1[1] + uy * t)

    segs = []
    prev = 0.0
    for t0, t1 in merged:
        if t0 - prev > min_seg:
            segs.append((pt_at(prev), pt_at(t0), prev == 0.0, False))
        prev = max(prev, t1)
    if length - prev > min_seg:
        segs.append((pt_at(prev), pt_at(length), False, True))
    return segs


def convex_hull(points):
    """Monotone-chain convex hull. Returns hull vertices in CCW order."""
    pts = sorted(set((float(p[0]), float(p[1])) for p in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _edge_key(p1, p2, tol):
    """Order-independent, tolerance-snapped key for matching shared edges between polygons."""
    def snap(p):
        return (round(p[0] / tol), round(p[1] / tol))
    a, b = snap(p1), snap(p2)
    return (a, b) if a <= b else (b, a)


def build_wall_bands(floor_data, style, doors=()):
    """Geometric wall system for floor plans, built directly from the (exact) unit polygons
    instead of the noisy c03 hatch texture:
      - exterior envelope edges (not shared with any other unit/balcony, and not facing the
        corridor/core) get a solid band OUTSIDE the edge (wall_bands.exterior_pt);
      - edges shared between two polygons (unit-unit, unit-balcony of a DIFFERENT unit) get a
        band CENTRED on the shared edge (wall_bands.inter_unit_pt, split evenly both sides);
      - edges facing the corridor/core (not shared with anyone, but their outward side falls
        inside the convex hull of every unit+balcony polygon on the floor - i.e. it opens onto
        the building's own interior circulation, not true exterior) get a band OUTSIDE the edge
        (wall_bands.corridor_pt);
      - an edge shared between a unit and its OWN balcony is a doorway/opening, not a wall, and
        is skipped entirely.
    Wherever a `doors` entry's hinge<->jamb2 span is collinear with (and inside) an edge, that
    span is cut OUT of the band - real entrance/balcony/interior door gaps instead of a
    continuously solid wall with a swing symbol floating over it (see extract_pdf_doors /
    _cut_edge_at_doors). Each remaining sub-segment gets its own quad band, with the tangential
    corner-mitring extension only at ends that are still a true polygon corner - a fresh
    door-jamb cut stays flush. Returns a flat list of (points, category) for category in
    ("ext", "shared", "corridor")."""
    wb = style["wall_bands"]
    tol = wb["shared_tol_pt"]
    hull_test = wb["hull_test_pt"]

    polys = []  # (unit_number, kind, poly)
    all_pts = []
    for number, u in floor_data["units"].items():
        if u.get("poly"):
            polys.append((number, "poly", u["poly"]))
            all_pts.extend(u["poly"])
        if u.get("balcony"):
            polys.append((number, "balcony", u["balcony"]))
            all_pts.extend(u["balcony"])

    hull = convex_hull(all_pts)

    # index every edge by its tolerance-snapped endpoint key, so we can find the (at most one)
    # matching edge from a different polygon.
    edge_index = {}
    for pi, (number, kind, poly) in enumerate(polys):
        n = len(poly)
        for i in range(n):
            p1, p2 = poly[i], poly[(i + 1) % n]
            if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) < 1e-6:
                continue
            key = _edge_key(p1, p2, tol)
            edge_index.setdefault(key, []).append((pi, i, p1, p2))

    bands = []

    def emit(a, b, ext_a, ext_b, outward, band_w, base_off, category):
        seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg_len < 1e-6:
            return
        ux, uy = (b[0] - a[0]) / seg_len, (b[1] - a[1]) / seg_len
        ea = band_w * 0.5 if ext_a else 0.0
        eb = band_w * 0.5 if ext_b else 0.0
        q1 = (a[0] - ux * ea + outward[0] * base_off, a[1] - uy * ea + outward[1] * base_off)
        q2 = (b[0] + ux * eb + outward[0] * base_off, b[1] + uy * eb + outward[1] * base_off)
        q3 = (q2[0] + outward[0] * band_w, q2[1] + outward[1] * band_w)
        q4 = (q1[0] + outward[0] * band_w, q1[1] + outward[1] * band_w)
        bands.append(([q1, q2, q3, q4], category))

    seen = set()
    for pi, (number, kind, poly) in enumerate(polys):
        n = len(poly)
        for i in range(n):
            p1, p2 = poly[i], poly[(i + 1) % n]
            length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            if length < 1e-6:
                continue
            key = _edge_key(p1, p2, tol)
            if key in seen:
                continue
            matches = [m for m in edge_index.get(key, []) if m[0] != pi]
            dx, dy = (p2[0] - p1[0]) / length, (p2[1] - p1[1]) / length
            n1, n2 = (dy, -dx), (-dy, dx)
            mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
            outward = n1 if not _point_in_poly((mx + n1[0] * 0.5, my + n1[1] * 0.5), poly) else n2
            segs = _cut_edge_at_doors(p1, p2, doors)

            if matches:
                other_pi, _, _, _ = matches[0]
                other_number = polys[other_pi][0]
                seen.add(key)
                if other_number == number:
                    continue  # own room<->balcony opening, not a wall
                band_w = wb["inter_unit_pt"]
                for a, b, ea, eb in segs:
                    emit(a, b, ea, eb, outward, band_w, -band_w / 2, "shared")
                continue

            # A balcony edge whose inward side (a few pt in, opposite `outward`) falls inside
            # the SAME unit's own room polygon is the near/room-facing edge of the balcony
            # shape - real projects routinely draw that edge 1-3pt off the room polygon's own
            # boundary (a slab-thickness offset), which is just under `shared_tol_pt` for one
            # sub-run and over it for another, so the ordinary edge-key match above misses it.
            # The TRUE wall + door there is the room polygon's own edge (handled in its own
            # iteration); drawing a second, near-duplicate band 1-3pt away here would double
            # the wall and can leave a same-width door gap looking closed. Skip it - it's an
            # opening, like the exact-match case above.
            if kind == "balcony":
                room_poly = floor_data["units"].get(number, {}).get("poly")
                if room_poly:
                    inward_test = (mx - outward[0] * 5.0, my - outward[1] * 5.0)
                    if _point_in_poly(inward_test, room_poly):
                        continue

            test_pt = (mx + outward[0] * hull_test, my + outward[1] * hull_test)
            is_corridor = _point_in_poly(test_pt, hull)
            band_w = wb["corridor_pt"] if is_corridor else wb["exterior_pt"]
            for a, b, ea, eb in segs:
                emit(a, b, ea, eb, outward, band_w, 0.0, "corridor" if is_corridor else "ext")
    return bands


def remove_hatch_ticks(fd, cids=("c07",), len_min=4.0, len_max=14.0, min_run=5, gap=4.0, dir_tol=0.15):
    """A run of many short, mutually-parallel, evenly-spaced 2-point line segments is a hatch/
    fire-rating marking symbol (seen on a highlighted special-status wall, e.g. the wall
    between a unit and the lift/stair core on every floor - drawn in red in the PDF, with
    these black tick marks alongside it), not a real partition: a real c07 partition is drawn
    as one long path, never as a dense comb of same-length diagonal ticks. This used to be
    cleared by strip_core_clutter/remove_corridor_signs' "outside every unit" sweep, but that
    test is exact-boundary (see _bbox_near_any_unit) and a hatch tick planted right along a
    unit's own polygon edge (the core-facing wall coincides with it) can land just inside -
    exactly the same tolerance widening real interior partitions near the core needed. Detect
    the hatch geometrically instead (a tight chain of >= min_run same-length, same-direction
    ticks) so it's stripped regardless of which side of the boundary it happens to sit on."""
    for cid in cids:
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        if not c or not c.get("paths"):
            continue
        paths = c["paths"]
        cand = []  # (idx, dirvec) - direction folded to a single half-plane (dx>=0)
        for i, p in enumerate(paths):
            pts = _path_points(p)
            if len(pts) != 2:
                continue
            p1, p2 = pts
            length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            if not (len_min <= length <= len_max):
                continue
            dx, dy = (p2[0] - p1[0]) / length, (p2[1] - p1[1]) / length
            if dx < 0 or (dx == 0 and dy < 0):
                dx, dy = -dx, -dy
            cand.append((i, dx, dy))
        if len(cand) < min_run:
            continue
        # Bucket by direction FIRST (a hatch run is one direction; the floor as a whole has
        # dozens of unrelated short segments in many directions, and spatially grouping them
        # all together before checking direction can chain an entire hatch run into one giant
        # mixed-direction blob via an unrelated nearby tick, at which point the "every member
        # must agree" check trivially fails and nothing gets dropped) - THEN group spatially
        # within each direction bucket, so only a genuinely parallel run can ever qualify.
        angle_bucket_deg = math.degrees(math.acos(max(-1.0, min(1.0, 1 - dir_tol)))) or 1.0
        buckets = {}
        for ci, (i, dx, dy) in enumerate(cand):
            key = round(math.degrees(math.atan2(dy, dx)) / angle_bucket_deg)
            buckets.setdefault(key, []).append(ci)
        drop = set()
        for members_ci in buckets.values():
            if len(members_ci) < min_run:
                continue
            idxs = [cand[ci][0] for ci in members_ci]
            bboxes = [_path_bbox(paths[i]) for i in idxs]
            groups = _union_find_groups(bboxes, gap)
            for members in groups.values():
                if len(members) >= min_run:
                    drop.update(idxs[m] for m in members)
        if drop:
            seqs = c.get("seq")
            c["paths"] = [p for i, p in enumerate(paths) if i not in drop]
            if seqs:
                c["seq"] = [s for i, s in enumerate(seqs) if i not in drop]
    return fd


def remove_turning_circles(fd, diam_min=17.0, diam_max=27.0, min_frags=12, merge_gap=4.5,
                            ring_cids=("c02", "c05"), content_cids=("c02", "c05")):
    """Wheelchair-turning-radius accessibility annotations: a dashed circle (many short
    fragments/dashes arranged in a ring, diam ~17-27pt) with a small glyph inside. Detect the
    ring purely by geometry (a compact, near-square group of >=12 small fragments in that size
    range - furniture is rectangular and much bigger, so it can't match) and drop it, plus any
    path from `content_cids` whose bbox sits fully inside the resulting circle without
    touching it (the wheelchair glyph itself)."""
    circles = []  # (cx, cy, r)
    for cid in ring_cids:
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        if not c or not c.get("paths"):
            continue
        paths = c["paths"]
        bboxes = [_path_bbox(p) for p in paths]
        groups = _union_find_groups(bboxes, merge_gap)
        drop = set()
        for members in groups.values():
            if len(members) < min_frags:
                continue
            mb = [bboxes[i] for i in members]
            gx0, gy0 = min(b[0] for b in mb), min(b[1] for b in mb)
            gx1, gy1 = max(b[2] for b in mb), max(b[3] for b in mb)
            w, h = gx1 - gx0, gy1 - gy0
            if max(w, h) / max(min(w, h), 0.01) > 1.3:
                continue
            if not (diam_min <= w <= diam_max and diam_min <= h <= diam_max):
                continue
            circles.append(((gx0 + gx1) / 2, (gy0 + gy1) / 2, (w + h) / 4))
            drop.update(members)
        if drop:
            c["paths"] = [p for i, p in enumerate(paths) if i not in drop]
            if c.get("seq"):
                c["seq"] = [s for i, s in enumerate(c["seq"]) if i not in drop]

    if not circles:
        return fd
    for cid in content_cids:
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        if not c or not c.get("paths"):
            continue
        paths = c["paths"]
        drop = set()
        for i, p in enumerate(paths):
            bb = _path_bbox(p)
            if bb is None:
                continue
            corners = [(bb[0], bb[1]), (bb[0], bb[3]), (bb[2], bb[1]), (bb[2], bb[3])]
            for cx, cy, r in circles:
                if all(math.hypot(x - cx, y - cy) <= r + 1.5 for x, y in corners):
                    drop.add(i)
                    break
        if drop:
            c["paths"] = [p for i, p in enumerate(paths) if i not in drop]
            if c.get("seq"):
                c["seq"] = [s for i, s in enumerate(c["seq"]) if i not in drop]
    return fd


def filter_glyph_fills(fd, cid, max_bbox, min_members, merge_gap=1.0):
    """Drop small decorative glyphs (wheelchair-turn icon, fire-cabinet hearts, arrows) that
    got swept into a WALL-FILL cluster (c08/c12) purely by sharing its fill colour: a genuine
    wall/column chunk is 1-2 simple filled polygons; a glyph is >=5 small sub-paths packed into
    a compact area. Columns (1-2 members) are always kept regardless of size."""
    c = next((x for x in fd["clusters"] if x["id"] == cid), None)
    if not c or not c.get("paths"):
        return fd
    paths = c["paths"]
    bboxes = [_path_bbox(p) for p in paths]
    groups = _union_find_groups(bboxes, merge_gap)
    drop = set()
    for members in groups.values():
        if len(members) < min_members:
            continue
        mb = [bboxes[i] for i in members]
        gx0, gy0 = min(b[0] for b in mb), min(b[1] for b in mb)
        gx1, gy1 = max(b[2] for b in mb), max(b[3] for b in mb)
        if (gx1 - gx0) <= max_bbox and (gy1 - gy0) <= max_bbox:
            drop.update(members)
    if drop:
        c["paths"] = [p for i, p in enumerate(paths) if i not in drop]
        if c.get("seq"):
            c["seq"] = [s for i, s in enumerate(c["seq"]) if i not in drop]
    return fd


def remove_door_chevrons(fd, gap_midpoints, max_pt, radius_pt):
    """Strip the short angled chevron tick-pair markers (c07) that sit in a wall opening -
    used both for openings we drew a synthesized door swing at, and any other detected
    door-scale gap."""
    c07 = next((x for x in fd["clusters"] if x["id"] == "c07"), None)
    if not c07 or not c07.get("paths") or not gap_midpoints:
        return fd
    paths = c07["paths"]
    drop = set()
    for i, p in enumerate(paths):
        bb = _path_bbox(p)
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        if bw > max_pt or bh > max_pt:
            continue
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        for gx, gy in gap_midpoints:
            if math.hypot(cx - gx, cy - gy) < radius_pt:
                drop.add(i)
                break
    if drop:
        c07["paths"] = [p for i, p in enumerate(paths) if i not in drop]
        if c07.get("seq"):
            c07["seq"] = [s for i, s in enumerate(c07["seq"]) if i not in drop]
    return fd


def remove_door_wedges(fd, gap_midpoints, radius_pt, max_fill_area, cids=("c07", "c08", "c12"), group_gap=1.5, group_max_bbox=10.0):
    """Door-marker leftovers (a filled wedge/triangle, distinct from our own plain-line leaf+
    arc) near a wall opening, in c07/c08/c12 within `radius_pt` of a door-scale gap - on
    either side (swing side or straight across in the corridor). Two passes: (1) any single
    CLOSED 3-vertex figure, or any fill under `max_fill_area` pt2; (2) markers built from
    several small sub-paths (like a column glyph) - group nearby small fragments and drop the
    whole group if it's compact (<= group_max_bbox on each side), since a real column/wall
    chunk near a door would either be a single simple shape (caught by (1) if small) or too
    big to be mistaken for a marker."""
    if not gap_midpoints:
        return fd
    for cid in cids:
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        if not c or not c.get("paths"):
            continue
        paths = c["paths"]
        bboxes = [_path_bbox(p) for p in paths]
        near_gap = []
        for i, bb in enumerate(bboxes):
            if bb is None:
                continue
            cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            if any(math.hypot(cx - gx, cy - gy) < radius_pt for gx, gy in gap_midpoints):
                near_gap.append(i)

        drop = set()
        for i in near_gap:
            p, bb = paths[i], bboxes[i]
            pts = _path_points(p)
            n = len(pts)
            closed = n >= 3 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 0.6
            is_triangle = closed and n in (3, 4)
            area = (bb[2] - bb[0]) * (bb[3] - bb[1])
            if is_triangle or area < max_fill_area:
                drop.add(i)

        # Columns (c08/c12) legitimately consist of 5-10 small sub-paths within a compact
        # ~5x5pt box each - geometrically identical to a compact marker glyph, and unlike
        # partitions there's no safe way to tell a column from a marker by grouping alone
        # (learned the hard way earlier on filter_glyph_fills). Only do the group-compaction
        # pass for c07, where a real partition is a long run, not a small blob.
        remaining = [i for i in near_gap if i not in drop]
        if remaining and cid == "c07":
            groups = _union_find_groups([bboxes[i] for i in remaining], group_gap)
            for members in groups.values():
                idxs = [remaining[m] for m in members]
                mb = [bboxes[i] for i in idxs]
                gx0, gy0 = min(b[0] for b in mb), min(b[1] for b in mb)
                gx1, gy1 = max(b[2] for b in mb), max(b[3] for b in mb)
                if (gx1 - gx0) <= group_max_bbox and (gy1 - gy0) <= group_max_bbox:
                    drop.update(idxs)

        if drop:
            c["paths"] = [p for i, p in enumerate(paths) if i not in drop]
            if c.get("seq"):
                c["seq"] = [s for i, s in enumerate(c["seq"]) if i not in drop]
    return fd


def _polygon_area(pts):
    return abs(_signed_area(pts))


def _unique_vertices(pts, tol=0.15):
    uniq = []
    for p in pts:
        if not any(math.hypot(p[0] - q[0], p[1] - q[1]) < tol for q in uniq):
            uniq.append(p)
    return uniq


def _dist_point_to_segment(pt, p1, p2):
    x, y = pt
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 < 1e-9:
        return math.hypot(x - x1, y - y1)
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / l2))
    px, py = x1 + t * dx, y1 + t * dy
    return math.hypot(x - px, y - py)


def _point_near_poly(pt, poly, tol=1.5):
    """True if `pt` is inside `poly`, or within `tol` of its boundary - an "expanded polygon"
    membership test with no actual polygon-offset geometry (which misbehaves at reflex
    corners). Used everywhere a cleanup pass must never touch content that's arguably still
    inside a unit: a wall-fill sliver or a partition segment right on a unit's own boundary
    line can have its path bbox centroid fall a fraction of a point outside the exact polygon,
    which used to be enough for strip_core_clutter/filter_wall_fills to delete it."""
    if _point_in_poly(pt, poly):
        return True
    n = len(poly)
    for i in range(n):
        if _dist_point_to_segment(pt, poly[i], poly[(i + 1) % n]) <= tol:
            return True
    return False


def _bbox_near_any_unit(bb, polys, tol=1.5):
    """True if any corner OR the centre of bbox `bb` is inside/near (see _point_near_poly) any
    polygon in `polys` (typically every unit room + balcony on the floor) - used so a cleanup
    pass keeps a whole path the instant ANY part of it could belong to a unit, rather than
    just its centroid."""
    x0, y0, x1, y1 = bb
    pts = ((x0, y0), (x1, y0), (x0, y1), (x1, y1), ((x0 + x1) / 2, (y0 + y1) / 2))
    return any(_point_near_poly(p, poly, tol) for p in pts for poly in polys)


def exterior_edges(floor_data, style):
    """Just the true building-exterior edges (not shared between two polygons, not facing the
    corridor/core) of every unit+balcony polygon on the floor - the same classification
    build_wall_bands uses for its "ext" category, exposed here as raw (p1,p2) segments so a
    caller can test proximity to the real building envelope (e.g. to spare the angled facade
    corners from a triangle-marker sweep)."""
    wb = style["wall_bands"]
    tol = wb["shared_tol_pt"]
    hull_test = wb["hull_test_pt"]
    polys = []
    all_pts = []
    for number, u in floor_data["units"].items():
        if u.get("poly"):
            polys.append((number, u["poly"]))
            all_pts.extend(u["poly"])
        if u.get("balcony"):
            polys.append((number, u["balcony"]))
            all_pts.extend(u["balcony"])
    hull = convex_hull(all_pts)
    edge_index = {}
    for pi, (number, poly) in enumerate(polys):
        n = len(poly)
        for i in range(n):
            p1, p2 = poly[i], poly[(i + 1) % n]
            if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) < 1e-6:
                continue
            key = _edge_key(p1, p2, tol)
            edge_index.setdefault(key, []).append(pi)
    edges = []
    seen = set()
    for pi, (number, poly) in enumerate(polys):
        n = len(poly)
        for i in range(n):
            p1, p2 = poly[i], poly[(i + 1) % n]
            length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            if length < 1e-6:
                continue
            key = _edge_key(p1, p2, tol)
            if key in seen:
                continue
            matches = [m for m in edge_index.get(key, []) if m != pi]
            if matches:
                seen.add(key)
                continue  # shared with another polygon -> not exterior
            dx, dy = (p2[0] - p1[0]) / length, (p2[1] - p1[1]) / length
            n1, n2 = (dy, -dx), (-dy, dx)
            mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
            outward = n1 if not _point_in_poly((mx + n1[0] * 0.5, my + n1[1] * 0.5), poly) else n2
            test_pt = (mx + outward[0] * hull_test, my + outward[1] * hull_test)
            if _point_in_poly(test_pt, hull):
                continue  # corridor/core-facing -> not exterior
            edges.append((p1, p2))
    return edges


def filter_wall_fills(fd, style, rect_ratio_min=0.85, facade_margin=6.0, cids=("c08", "c12", "c14")):
    """c08/c12/c14 are a colour-clustered grab-bag of real wall/column fill AND every small
    decorative glyph (arrows, exit signs, door-leaf wedges) that happened to share that fill
    colour in the original PDF. Rather than keep inventing removal rules for each glyph shape,
    keep only what a wall/column fill actually looks like: an axis-aligned rectangle (its own
    polygon area ~= its bbox area, 4-5 vertices), OR anything within facade_margin pt of the
    building's true exterior contour (the angled facade corners are real wall pieces that
    aren't plain rectangles), OR anything inside/near (see _point_near_poly) any unit's own
    room or balcony polygon - a real wall/column fill INSIDE a unit is kept regardless of its
    shape, since a glyph sharing this fill colour never happens to sit inside a unit's own
    footprint. Everything else in these three clusters is dropped."""
    ext_edges = exterior_edges(fd, style)
    unit_polys = []
    for u in fd["units"].values():
        if u.get("poly"):
            unit_polys.append(u["poly"])
        if u.get("balcony"):
            unit_polys.append(u["balcony"])
    for cid in cids:
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        if not c or not c.get("paths"):
            continue
        paths = c["paths"]
        seqs = c.get("seq")
        keep_idx = []
        for i, p in enumerate(paths):
            bb = _path_bbox(p)
            if bb is None:
                continue
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            bbox_area = bw * bh
            if bbox_area <= 0:
                continue
            cx0, cy0 = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            if unit_polys and any(_point_in_poly((cx0, cy0), poly) for poly in unit_polys):
                # Centre genuinely INSIDE a room/balcony (not just near its boundary - the
                # core-facing wall centreline often runs almost exactly along a unit's own
                # polygon edge, and a wall-hatch/fire-rating tick mark drawn along THAT line
                # would otherwise get the same free pass a real interior wall-fill piece
                # needs): a real wall/column fill inside a unit is kept regardless of shape.
                keep_idx.append(i)
                continue
            pts = _path_points(p)
            uniq = _unique_vertices(pts)
            n = len(uniq)
            poly_area = _polygon_area(pts) if len(pts) >= 3 else 0.0
            ratio = poly_area / bbox_area if bbox_area > 0 else 0.0
            is_rect = ratio >= rect_ratio_min and n in (4, 5)
            if is_rect:
                keep_idx.append(i)
                continue
            if ext_edges:
                cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
                if any(_dist_point_to_segment((cx, cy), s0, s1) < facade_margin for s0, s1 in ext_edges):
                    keep_idx.append(i)
        c["paths"] = [paths[i] for i in keep_idx]
        if seqs:
            c["seq"] = [seqs[i] for i in keep_idx]
    return fd



def compute_core_bbox(fd):
    """Bounding box of the stair/lift core: c07/c08 content that falls inside the building's
    own footprint (convex hull of every unit+balcony polygon) but outside every unit polygon.
    The corridor's own walls come from synthetic bands, not raw cluster paths, so whatever
    c07/c08 is left in that hull-but-outside-units region is specifically the stairs/shaft
    structure - this gives a reasonable core rectangle to fill and to spare from the
    corridor-sign sweep."""
    polys = []
    for u in fd["units"].values():
        if u.get("poly"):
            polys.append(u["poly"])
        if u.get("balcony"):
            polys.append(u["balcony"])
    if not polys:
        return None
    all_pts = [pt for poly in polys for pt in poly]
    hull = convex_hull(all_pts)
    boxes = []
    for cid in ("c07", "c08"):
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths", []):
            bb = _path_bbox(p)
            if bb is None:
                continue
            cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            if not _point_in_poly((cx, cy), hull):
                continue
            if _bbox_near_any_unit(bb, polys, 1.5):
                continue
            boxes.append(bb)
    if not boxes:
        return None
    # c07/c08 outside every unit polygon is NOT just the stairs - it is every corridor wall
    # and perimeter wall segment too, which chains across the whole floor under any generous
    # gap tolerance. The stairs/lift shafts are instead a dense cluster of MANY tread-line and
    # shaft-wall fragments packed within a small footprint, so use a tight gap (real fragments
    # of the same structure touch or nearly touch) and take the group with the most members -
    # tested at gap 2/2.5/3pt, all agree on the same ~217x140pt block, which is stable evidence
    # it is a real structure and not a chaining artifact.
    groups = _union_find_groups(boxes, gap=2.5)
    biggest = max(groups.values(), key=len)
    xs, ys = [], []
    for i in biggest:
        xs += [boxes[i][0], boxes[i][2]]
        ys += [boxes[i][1], boxes[i][3]]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_overlap_frac(a, b):
    ox = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    oy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ox * oy
    if inter <= 0:
        return 0.0
    area_a = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / min(area_a, area_b)


def _span_overlap_frac(a, b):
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    if hi <= lo:
        return 0.0
    return (hi - lo) / min(a[1] - a[0], b[1] - b[0])


def detect_stair_flights(fd, cids=("c02", "c05", "c07", "c06_furn"), len_min=8.0, len_max=30.0,
                          pitch_min=1.0, pitch_max=5.0, min_steps=6):
    """A stair flight in the source PDF is drawn as a run of many small tread marks - short,
    near-identical-length bands (8-30pt) repeating at a tight, regular pitch (1.5-4pt, widened
    a bit here for real-data slop) - usually duplicated across two or three clusters at once
    (e.g. a thin c02 line AND a filled c06_furn tread-rectangle at the very same spot). Group
    candidate bands by orientation (their long axis), merge same-spot duplicates first (else a
    tread drawn twice looks like "2 steps" and skews the pitch chain), then chain consecutive,
    similarly-long, span-overlapping bands whose pitch stays in range; a chain of >= min_steps
    is a real flight. Returns a list of {orient, bbox, n} - `orient` "H" means the treads
    themselves are horizontal bands (so the flight is walked top-to-bottom), "V" the reverse."""
    raw = []
    for cid in cids:
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths") or []:
            bb = _path_bbox(p)
            if bb is None:
                continue
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            if len_min <= bw <= len_max and bh <= 6.0:
                raw.append(dict(orient="H", bb=bb, length=bw, perp=(bb[1] + bb[3]) / 2, span=(bb[0], bb[2])))
            elif len_min <= bh <= len_max and bw <= 6.0:
                raw.append(dict(orient="V", bb=bb, length=bh, perp=(bb[0] + bb[2]) / 2, span=(bb[1], bb[3])))
    if not raw:
        return []
    # merge same-spot duplicates (same tread drawn in more than one cluster)
    n = len(raw)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if raw[i]["orient"] == raw[j]["orient"] and _bbox_overlap_frac(raw[i]["bb"], raw[j]["bb"]) > 0.3:
                union(i, j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(raw[i])
    merged = []
    for g in groups.values():
        xs = [r["bb"][0] for r in g] + [r["bb"][2] for r in g]
        ys = [r["bb"][1] for r in g] + [r["bb"][3] for r in g]
        bb = (min(xs), min(ys), max(xs), max(ys))
        orient = g[0]["orient"]
        length = bb[2] - bb[0] if orient == "H" else bb[3] - bb[1]
        perp = (bb[1] + bb[3]) / 2 if orient == "H" else (bb[0] + bb[2]) / 2
        span = (bb[0], bb[2]) if orient == "H" else (bb[1], bb[3])
        merged.append(dict(orient=orient, bb=bb, length=length, perp=perp, span=span))

    flights = []
    for orient in ("H", "V"):
        items = [r for r in merged if r["orient"] == orient]
        m = len(items)
        parent = list(range(m))

        def find2(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union2(a, b):
            ra, rb = find2(a), find2(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(m):
            for j in range(i + 1, m):
                if _span_overlap_frac(items[i]["span"], items[j]["span"]) > 0.6 and \
                        abs(items[i]["length"] - items[j]["length"]) <= 0.25 * max(items[i]["length"], items[j]["length"]):
                    union2(i, j)
        gg = {}
        for i in range(m):
            gg.setdefault(find2(i), []).append(items[i])
        for g in gg.values():
            g.sort(key=lambda r: r["perp"])
            chain = [g[0]]
            for r in g[1:]:
                pitch = r["perp"] - chain[-1]["perp"]
                if pitch_min <= pitch <= pitch_max:
                    chain.append(r)
                else:
                    if len(chain) >= min_steps:
                        flights.append(_flight_from_chain(orient, chain))
                    chain = [r]
            if len(chain) >= min_steps:
                flights.append(_flight_from_chain(orient, chain))
    return flights


def _flight_from_chain(orient, chain):
    xs = [r["bb"][0] for r in chain] + [r["bb"][2] for r in chain]
    ys = [r["bb"][1] for r in chain] + [r["bb"][3] for r in chain]
    return dict(orient=orient, bbox=(min(xs), min(ys), max(xs), max(ys)), n=len(chain))


def strip_stair_originals(fd, flights, margin=2.0, cids=("c02", "c05", "c07", "c06_furn")):
    """Erase every original path from `cids` that falls inside a detected flight's own
    rectangle (padded by `margin`) - the scribble (tread fills, handrail ticks, an old
    direction arrow, a break mark) that the clean stair_flight_svg symbol replaces."""
    for f in flights:
        x0, y0, x1, y1 = f["bbox"]
        x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
        for c in fd["clusters"]:
            if c["id"] not in cids:
                continue
            paths = c.get("paths") or []
            if not paths:
                continue
            keep = []
            keep_seq = []
            seqs = c.get("seq")
            for i, p in enumerate(paths):
                bb = _path_bbox(p)
                if bb is None:
                    keep.append(p)
                    if seqs:
                        keep_seq.append(seqs[i])
                    continue
                cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    continue
                keep.append(p)
                if seqs:
                    keep_seq.append(seqs[i])
            c["paths"] = keep
            if seqs:
                c["seq"] = keep_seq
    return fd


def stair_flight_svg(flight, muted, tread_w=0.5, contour_w=0.5, contour_dash=""):
    """Light-but-legible plan symbol for one stair flight (reference style: no arrows, no
    break mark) - a contour and n evenly-spaced tread lines, both in the muted grey-blue
    accent rather than full ink. The handrail is drawn separately (see pair_stair_flights /
    handrail_svg) since twin flights share ONE rail on their dividing line."""
    x0, y0, x1, y1 = flight["bbox"]
    n = max(1, flight["n"])
    orient = flight["orient"]
    dash_attr = f' stroke-dasharray="{contour_dash}"' if contour_dash else ""
    parts = [f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}" '
             f'fill="none" stroke="{muted}" stroke-width="{contour_w}"{dash_attr}/>']
    if orient == "H":
        for i in range(n):
            yy = y0 + (y1 - y0) * (i + 0.5) / n
            parts.append(f'<line x1="{x0:.2f}" y1="{yy:.2f}" x2="{x1:.2f}" y2="{yy:.2f}" stroke="{muted}" stroke-width="{tread_w}"/>')
    else:
        for i in range(n):
            xx = x0 + (x1 - x0) * (i + 0.5) / n
            parts.append(f'<line x1="{xx:.2f}" y1="{y0:.2f}" x2="{xx:.2f}" y2="{y1:.2f}" stroke="{muted}" stroke-width="{tread_w}"/>')
    return "".join(parts)


def pair_stair_flights(flights, touch_gap=3.0, overlap_min=0.5):
    """Group flights into twins (two runs sharing a dividing line, same orientation, their
    perpendicular ranges overlapping) or singles. A twin pair gets ONE shared handrail on the
    boundary between them; a single gets its own down its own centre axis."""
    n = len(flights)
    used = [False] * n
    groups = []
    for i in range(n):
        if used[i]:
            continue
        a = flights[i]
        best = None
        for j in range(n):
            if j == i or used[j]:
                continue
            b = flights[j]
            if a["orient"] != b["orient"]:
                continue
            ax0, ay0, ax1, ay1 = a["bbox"]
            bx0, by0, bx1, by1 = b["bbox"]
            if a["orient"] == "H":
                gap = min(abs(ax1 - bx0), abs(bx1 - ax0))
                lo, hi = max(ay0, by0), min(ay1, by1)
                overlap = max(0.0, hi - lo) / max(1e-6, min(ay1 - ay0, by1 - by0))
            else:
                gap = min(abs(ay1 - by0), abs(by1 - ay0))
                lo, hi = max(ax0, bx0), min(ax1, bx1)
                overlap = max(0.0, hi - lo) / max(1e-6, min(ax1 - ax0, bx1 - bx0))
            if gap <= touch_gap and overlap >= overlap_min:
                best = j
                break
        if best is not None:
            groups.append([a, flights[best]])
            used[i] = used[best] = True
        else:
            groups.append([a])
            used[i] = True
    return groups


def _u_handrail_vertical(cx, y0, y1, ink, gap, width):
    half = gap / 2
    x_l, x_r = cx - half, cx + half
    top_y = y0 + half
    return (
        f'<line x1="{x_l:.2f}" y1="{y1:.2f}" x2="{x_l:.2f}" y2="{top_y:.2f}" stroke="{ink}" stroke-width="{width}"/>'
        f'<line x1="{x_r:.2f}" y1="{y1:.2f}" x2="{x_r:.2f}" y2="{top_y:.2f}" stroke="{ink}" stroke-width="{width}"/>'
        f'<path d="M{x_l:.2f},{top_y:.2f} A{half:.2f},{half:.2f} 0 0 1 {x_r:.2f},{top_y:.2f}" fill="none" stroke="{ink}" stroke-width="{width}"/>'
    )


def _u_handrail_horizontal(cy, x0, x1, ink, gap, width):
    half = gap / 2
    y_t, y_b = cy - half, cy + half
    cap_x = x0 + half
    return (
        f'<line x1="{x1:.2f}" y1="{y_t:.2f}" x2="{cap_x:.2f}" y2="{y_t:.2f}" stroke="{ink}" stroke-width="{width}"/>'
        f'<line x1="{x1:.2f}" y1="{y_b:.2f}" x2="{cap_x:.2f}" y2="{y_b:.2f}" stroke="{ink}" stroke-width="{width}"/>'
        f'<path d="M{cap_x:.2f},{y_t:.2f} A{half:.2f},{half:.2f} 0 0 0 {cap_x:.2f},{y_b:.2f}" fill="none" stroke="{ink}" stroke-width="{width}"/>'
    )


def handrail_svg(group, ink, gap=2.0, width=0.4):
    """U-shaped handrail (two parallel ink lines, closed by a semicircle at the "up" end,
    open at the bottom) - one per flight, or one shared on the dividing line for a twin pair."""
    if len(group) == 2:
        a, b = group
        ax0, ay0, ax1, ay1 = a["bbox"]
        bx0, by0, bx1, by1 = b["bbox"]
        if a["orient"] == "H":
            boundary = (ax1 + bx0) / 2 if ax1 <= bx0 else (bx1 + ax0) / 2
            y0, y1 = min(ay0, by0), max(ay1, by1)
            return _u_handrail_vertical(boundary, y0, y1, ink, gap, width)
        else:
            boundary = (ay1 + by0) / 2 if ay1 <= by0 else (by1 + ay0) / 2
            x0, x1 = min(ax0, bx0), max(ax1, bx1)
            return _u_handrail_horizontal(boundary, x0, x1, ink, gap, width)
    f = group[0]
    x0, y0, x1, y1 = f["bbox"]
    if f["orient"] == "H":
        return _u_handrail_vertical((x0 + x1) / 2, y0, y1, ink, gap, width)
    return _u_handrail_horizontal((y0 + y1) / 2, x0, x1, ink, gap, width)


def detect_lift_bodies(fd, core_bbox, w_min=35.0, w_max=75.0, h_min=25.0, h_max=75.0, cid="c06_furn"):
    """The brief's own description: white c06 furniture-body shapes inside core_bbox, roughly
    square, ~40-70pt a side. Real data has two of these stacked (each ~42.5 wide, ~30 tall -
    loosened the height floor to 25pt to match) sitting left of the stairs in the core, exactly
    matching the two boxed-X lift symbols in the reference. If nothing in this size band turns
    up, return an empty list rather than inventing a lift."""
    if not core_bbox:
        return []
    x0, y0, x1, y1 = core_bbox
    c = next((cc for cc in fd["clusters"] if cc["id"] == cid), None)
    if not c:
        return []
    polys = []
    for u in fd["units"].values():
        if u.get("poly"):
            polys.append(u["poly"])
        if u.get("balcony"):
            polys.append(u["balcony"])
    out = []
    for p in c.get("paths") or []:
        bb = _path_bbox(p)
        if bb is None:
            continue
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        if not (x0 <= cx <= x1 and y0 <= cy <= y1):
            continue
        if any(_point_in_poly((cx, cy), poly) for poly in polys):
            continue  # core_bbox is only a bounding rect - don't pick up a unit's own furniture
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        if w_min <= bw <= w_max and h_min <= bh <= h_max:
            out.append(bb)
    return out


def strip_lift_originals(fd, lifts, margin=1.0, cids=("c02", "c05", "c06_furn", "c07")):
    """Erase whatever the source drew inside each lift body's own footprint (the nested
    inset-rectangle detailing seen in the raw render) before drawing the clean symbol."""
    for bbox in lifts:
        x0, y0, x1, y1 = bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin
        for c in fd["clusters"]:
            if c["id"] not in cids:
                continue
            paths = c.get("paths") or []
            if not paths:
                continue
            keep, keep_seq = [], []
            seqs = c.get("seq")
            for i, p in enumerate(paths):
                bb = _path_bbox(p)
                if bb is None:
                    keep.append(p)
                    if seqs:
                        keep_seq.append(seqs[i])
                    continue
                cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    continue
                keep.append(p)
                if seqs:
                    keep_seq.append(seqs[i])
            c["paths"] = keep
            if seqs:
                c["seq"] = keep_seq
    return fd


def lift_group_svg(lift_bboxes, door_side, ink, shaft_wall=2.4, cabin_inset=1.5,
                    cross_w=0.4, door_gap=1.5, door_stroke=0.5, door_frac=0.6, touch_tol=1.0):
    """Reference-style lift shaft(s): ink shaft walls on every side of each cabin except the
    door side, with adjacent cabins sharing ONE wall band on their common boundary instead of
    each drawing its own (so two stacked lifts don't get a doubled-up wall between them); the
    door side gets a thin double line (sliding-door mark) instead of a wall; the cabin itself
    is inset from the shaft's inner face and carries a full corner-to-corner cross."""
    n = len(lift_bboxes)
    shared_sides = [set() for _ in range(n)]
    shared_bands = []  # ('h'|'v', coord, span0, span1)
    for i in range(n):
        ax0, ay0, ax1, ay1 = lift_bboxes[i]
        for j in range(i + 1, n):
            bx0, by0, bx1, by1 = lift_bboxes[j]
            if min(ax1, bx1) - max(ax0, bx0) > 0:
                if abs(ay1 - by0) <= touch_tol:
                    shared_sides[i].add("bottom"); shared_sides[j].add("top")
                    shared_bands.append(("h", (ay1 + by0) / 2, max(ax0, bx0), min(ax1, bx1)))
                elif abs(by1 - ay0) <= touch_tol:
                    shared_sides[j].add("bottom"); shared_sides[i].add("top")
                    shared_bands.append(("h", (by1 + ay0) / 2, max(ax0, bx0), min(ax1, bx1)))
            if min(ay1, by1) - max(ay0, by0) > 0:
                if abs(ax1 - bx0) <= touch_tol:
                    shared_sides[i].add("right"); shared_sides[j].add("left")
                    shared_bands.append(("v", (ax1 + bx0) / 2, max(ay0, by0), min(ay1, by1)))
                elif abs(bx1 - ax0) <= touch_tol:
                    shared_sides[j].add("right"); shared_sides[i].add("left")
                    shared_bands.append(("v", (bx1 + ax0) / 2, max(ay0, by0), min(ay1, by1)))

    parts = []
    for axis, coord, s0, s1 in shared_bands:
        if axis == "h":
            parts.append(f'<rect x="{s0 - shaft_wall:.2f}" y="{coord - shaft_wall / 2:.2f}" '
                         f'width="{s1 - s0 + 2 * shaft_wall:.2f}" height="{shaft_wall:.2f}" fill="{ink}"/>')
        else:
            parts.append(f'<rect x="{coord - shaft_wall / 2:.2f}" y="{s0 - shaft_wall:.2f}" '
                         f'width="{shaft_wall:.2f}" height="{s1 - s0 + 2 * shaft_wall:.2f}" fill="{ink}"/>')

    for i, (x0, y0, x1, y1) in enumerate(lift_bboxes):
        for side in ("top", "bottom", "left", "right"):
            if side == door_side or side in shared_sides[i]:
                continue
            if side == "top":
                parts.append(f'<rect x="{x0 - shaft_wall:.2f}" y="{y0 - shaft_wall:.2f}" width="{x1 - x0 + 2 * shaft_wall:.2f}" height="{shaft_wall:.2f}" fill="{ink}"/>')
            elif side == "bottom":
                parts.append(f'<rect x="{x0 - shaft_wall:.2f}" y="{y1:.2f}" width="{x1 - x0 + 2 * shaft_wall:.2f}" height="{shaft_wall:.2f}" fill="{ink}"/>')
            elif side == "left":
                parts.append(f'<rect x="{x0 - shaft_wall:.2f}" y="{y0 - shaft_wall:.2f}" width="{shaft_wall:.2f}" height="{y1 - y0 + 2 * shaft_wall:.2f}" fill="{ink}"/>')
            else:
                parts.append(f'<rect x="{x1:.2f}" y="{y0 - shaft_wall:.2f}" width="{shaft_wall:.2f}" height="{y1 - y0 + 2 * shaft_wall:.2f}" fill="{ink}"/>')

        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if door_side in ("left", "right"):
            span = (y1 - y0) * door_frac
            ya, yb = cy - span / 2, cy + span / 2
            xx = x0 if door_side == "left" else x1
            for off in (-door_gap / 2, door_gap / 2):
                parts.append(f'<line x1="{xx + off:.2f}" y1="{ya:.2f}" x2="{xx + off:.2f}" y2="{yb:.2f}" stroke="{ink}" stroke-width="{door_stroke}"/>')
        else:
            span = (x1 - x0) * door_frac
            xa, xb = cx - span / 2, cx + span / 2
            yy = y0 if door_side == "top" else y1
            for off in (-door_gap / 2, door_gap / 2):
                parts.append(f'<line x1="{xa:.2f}" y1="{yy + off:.2f}" x2="{xb:.2f}" y2="{yy + off:.2f}" stroke="{ink}" stroke-width="{door_stroke}"/>')

        ix0, iy0, ix1, iy1 = x0 + cabin_inset, y0 + cabin_inset, x1 - cabin_inset, y1 - cabin_inset
        parts.append(f'<rect x="{ix0:.2f}" y="{iy0:.2f}" width="{ix1 - ix0:.2f}" height="{iy1 - iy0:.2f}" fill="white" stroke="{ink}" stroke-width="0.5"/>')
        parts.append(f'<line x1="{ix0:.2f}" y1="{iy0:.2f}" x2="{ix1:.2f}" y2="{iy1:.2f}" stroke="{ink}" stroke-width="{cross_w}"/>')
        parts.append(f'<line x1="{ix0:.2f}" y1="{iy1:.2f}" x2="{ix1:.2f}" y2="{iy0:.2f}" stroke="{ink}" stroke-width="{cross_w}"/>')
    return "".join(parts)


def strip_core_clutter(fd, core_bbox, exempt_cids=("c08", "c12", "c14", "c06_floor"), wall_min_thickness=2.0):
    """The core should read as walls + floor + stairs + lifts and nothing else - everything
    already handled (stair flights, lift cabins) has been stripped from its own footprint by
    this point, so this is a blanket final sweep: drop any remaining path, in any other
    cluster, centred inside core_bbox AND outside every unit/balcony polygon (core_bbox is
    only a bounding rectangle - units 1009/1010 sit right against its top edge, so without the
    "outside every unit" guard this would also erase their real furniture, exactly the same
    bleed bug core_fill_svg had to guard against). c08/c12/c14 (walls/columns) and the floor
    fill are exempt from the blanket sweep BUT even there, a piece thinner than
    `wall_min_thickness` (our narrowest synthesised wall band is 2.0pt) isn't a real wall - it
    is exactly the kind of floating decorative-hatch sliver the brief is pointing at - so those
    get swept too, regardless of cluster (still gated on being outside every unit)."""
    if not core_bbox:
        return fd
    x0, y0, x1, y1 = core_bbox
    polys = []
    for u in fd["units"].values():
        if u.get("poly"):
            polys.append(u["poly"])
        if u.get("balcony"):
            polys.append(u["balcony"])
    for c in fd["clusters"]:
        wall_like = c["id"] in ("c08", "c12", "c14")
        if c["id"] in exempt_cids and not wall_like:
            continue
        paths = c.get("paths") or []
        if not paths:
            continue
        keep, keep_seq = [], []
        seqs = c.get("seq")
        for i, p in enumerate(paths):
            bb = _path_bbox(p)
            if bb is None:
                keep.append(p)
                if seqs:
                    keep_seq.append(seqs[i])
                continue
            cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            in_core = x0 <= cx <= x1 and y0 <= cy <= y1
            # Expanded (+1.5pt) membership, not a bare centroid-in-polygon test: a partition
            # or wall-fill sliver right on a unit's own boundary line - exactly what units
            # pressed against the core (309, 310, ...) have plenty of - can have its path
            # bbox centroid fall a fraction of a point outside the exact polygon, which used
            # to be enough for this "blanket" sweep to delete real interior content.
            in_unit = in_core and (polys and _bbox_near_any_unit(bb, polys, 1.5))
            if in_core and not in_unit:
                if wall_like and min(bb[2] - bb[0], bb[3] - bb[1]) >= wall_min_thickness:
                    pass  # thick enough to be a real wall/column piece - keep it
                else:
                    continue
            keep.append(p)
            if seqs:
                keep_seq.append(seqs[i])
        c["paths"] = keep
        if seqs:
            c["seq"] = keep_seq
    return fd


def _bed_shape_ok(fd, bbox, long_axis, cids=("c06_furn", "c02", "c02_table")):
    """A rectangle only reads as a real bed if the architect actually drew pillows or a
    blanket fold: 1-2 inner sub-rects at ONE short end, each spanning 15-35% of the bed's
    length and >=30% of its width, OR a genuinely diagonal short segment (a blanket-fold
    corner) near one of the 4 corners. A bare rectangle, or one whose only inner feature is a
    single rect spanning most of the length (a sofa seat cushion, a wardrobe shelf), is not a
    bed - that's exactly the shape of the phantom sofa/wardrobe hits this rule exists for."""
    x0, y0, x1, y1 = bbox
    L = (x1 - x0) if long_axis == "x" else (y1 - y0)
    W = (y1 - y0) if long_axis == "x" else (x1 - x0)
    if L <= 0 or W <= 0:
        return False, "degenerate bbox"
    inner = []
    for cid in cids:
        c = next((cc for cc in fd["clusters"] if cc["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths") or []:
            bb = _path_bbox(p)
            if bb is None:
                continue
            if bb[0] < x0 - 0.5 or bb[2] > x1 + 0.5 or bb[1] < y0 - 0.5 or bb[3] > y1 + 0.5:
                continue
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            if bw <= 0 or bh <= 0:
                continue
            if bw >= (x1 - x0) - 0.5 and bh >= (y1 - y0) - 0.5:
                continue  # the outer rect itself
            inner.append((bb, p))
    if not inner:
        return False, "no inner shapes at all"

    pillow_hits = 0
    long_rect_hit = False
    for bb, _p in inner:
        if re.search(r"(?:^|\s)C(?:\s|$)", _p):
            continue  # a curved (bezier) shape - a sink/handle/arc fixture, never a pillow
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        if long_axis == "x":
            span_len, span_w = bw, bh
            near_start, near_end = (bb[0] - x0) < L * 0.1, (x1 - bb[2]) < L * 0.1
        else:
            span_len, span_w = bh, bw
            near_start, near_end = (bb[1] - y0) < L * 0.1, (y1 - bb[3]) < L * 0.1
        frac_len, frac_w = span_len / L, span_w / W
        if 0.15 <= frac_len <= 0.35 and frac_w >= 0.3 and (near_start or near_end):
            pillow_hits += 1
        elif frac_len > 0.5:
            long_rect_hit = True

    if pillow_hits:
        return True, f"{pillow_hits} pillow-like inner rect(s)"

    corners = [(x0, y0), (x0, y1), (x1, y0), (x1, y1)]
    for bb, p in inner:
        pts = _path_points(p)
        if len(pts) not in (2, 3):
            continue
        (px0, py0), (px1, py1) = pts[0], pts[-1]
        if abs(px0 - px1) < 1.0 or abs(py0 - py1) < 1.0:
            continue  # axis-aligned, not a diagonal fold
        mid = ((px0 + px1) / 2, (py0 + py1) / 2)
        if any(math.hypot(mid[0] - cx, mid[1] - cy) < max(L, W) * 0.25 for cx, cy in corners):
            return True, "diagonal blanket-fold line near a corner"

    if long_rect_hit:
        return False, "only a long inner rect (sofa seat / wardrobe shelf), no pillow zone"
    return False, "no pillow rects or blanket fold found"


def _segments_intersect(p1, p2, p3, p4):
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)

    o1, o2 = orient(p1, p2, p3), orient(p1, p2, p4)
    o3, o4 = orient(p3, p4, p1), orient(p3, p4, p2)
    return o1 != o2 and o3 != o4 and o1 != 0 and o2 != 0 and o3 != 0 and o4 != 0


def _room_separator_between(fd, pt_a, pt_b, fill_cids=("c08", "c12", "c14"), stroke_cids=("c07",), min_area_pt=0.1):
    """True if a wall/partition path crosses the straight line between two bed candidates -
    meaning they're in different enclosed rooms, not duplicates in the same open room. ANY
    c07 partition path counts, regardless of thickness - confirmed on real data (209) that a
    thin (~1.5pt) c07/c08 partition is a genuine room divider, not decorative; c08/c12/c14
    only need a non-degenerate bbox (both dims > min_area_pt) since a truly zero-area 2-point
    "path" is fill-rule invisible and can't be a real rendered wall at all."""
    for cid in stroke_cids:
        c = next((cc for cc in fd["clusters"] if cc["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths") or []:
            pts = _path_points(p)
            for i in range(len(pts) - 1):
                if _segments_intersect(pt_a, pt_b, pts[i], pts[i + 1]):
                    return True
    for cid in fill_cids:
        c = next((cc for cc in fd["clusters"] if cc["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths") or []:
            bb = _path_bbox(p)
            if bb is None or min(bb[2] - bb[0], bb[3] - bb[1]) < min_area_pt:
                continue  # thinner than any real synthesised wall band (2.0pt) - a decorative
                # sliver or furniture-scale divider, not an actual room-separating wall (same
                # threshold strip_core_clutter uses to tell a real wall from PDF noise)
            pts = _path_points(p)
            for i in range(len(pts) - 1):
                if _segments_intersect(pt_a, pt_b, pts[i], pts[i + 1]):
                    return True
    return False


def detect_beds(fd, cids=("c06_furn", "c02_table"),
                 double_short=(18.0, 30.0), double_long=(26.0, 36.0),
                 single_short=(11.0, 17.0), single_long=(26.0, 36.0), debug_log=None):
    """Beds, wherever a unit has them: a c06_furn (or c02_table-shaped) body sized like a
    double (short side 18-30pt, long side 26-36pt) or single (11-17 / 26-36) mattress, lying
    inside a unit's own polygon, THAT ALSO shows a real pillow/blanket-fold pattern (see
    _bed_shape_ok) - a bare rectangle or a rectangle whose only inner feature is a long sofa-
    seat-style rect is rejected outright, regardless of size. Among double beds that pass, at
    most one survives per enclosed room (no c07/c08/c12/c14 wall crossing the line between two
    candidates' centres) - a second candidate in the same open room is left as the original
    drawing (not stripped, not redrawn). The long axis tells us head-to-foot; which END is the
    headboard is decided separately (see bed_headboard_side). Pass `debug_log` (a list) to
    collect (unit, bbox, kind, status, reason) for every candidate seen, accepted or not."""
    candidates = []
    for num, u in fd["units"].items():
        poly = u.get("poly")
        if not poly:
            continue
        for cid in cids:
            c = next((x for x in fd["clusters"] if x["id"] == cid), None)
            if not c:
                continue
            for p in c.get("paths") or []:
                bb = _path_bbox(p)
                if bb is None:
                    continue
                cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
                if not _point_in_poly((cx, cy), poly):
                    continue
                bw, bh = bb[2] - bb[0], bb[3] - bb[1]
                short, long_ = min(bw, bh), max(bw, bh)
                kind = None
                if double_short[0] <= short <= double_short[1] and double_long[0] <= long_ <= double_long[1]:
                    kind = "double"
                elif single_short[0] <= short <= single_short[1] and single_long[0] <= long_ <= single_long[1]:
                    kind = "single"
                if kind:
                    candidates.append({"unit": num, "bbox": bb, "kind": kind, "long_axis": "x" if bw >= bh else "y"})

    beds = []
    accepted_doubles_by_unit = {}
    for cand in candidates:
        ok, reason = _bed_shape_ok(fd, cand["bbox"], cand["long_axis"])
        if not ok:
            if debug_log is not None:
                debug_log.append((cand["unit"], cand["bbox"], cand["kind"], "rejected", reason))
            continue
        if cand["kind"] == "double":
            x0, y0, x1, y1 = cand["bbox"]
            centre = ((x0 + x1) / 2, (y0 + y1) / 2)
            dup_of = None
            for other in accepted_doubles_by_unit.get(cand["unit"], []):
                ox0, oy0, ox1, oy1 = other["bbox"]
                # Only even consider two candidates for the same-room merge if their bboxes
                # are close/touching (<=10pt gap) - a straight line between two GENUINELY
                # separate, non-adjacent rooms (e.g. opposite ends of a 3-bedroom unit) often
                # doesn't cross any wall either (it can run down a hallway instead), so that
                # test alone is only reliable for candidates that are already right next to
                # each other, which is the actual failure case (two "beds" crammed into one
                # small room) this rule targets.
                gap_x = max(x0, ox0) - min(x1, ox1)
                gap_y = max(y0, oy0) - min(y1, oy1)
                if max(gap_x, gap_y) > 10.0:
                    continue
                other_centre = ((ox0 + ox1) / 2, (oy0 + oy1) / 2)
                if not _room_separator_between(fd, centre, other_centre):
                    dup_of = other
                    break
            if dup_of is not None:
                if debug_log is not None:
                    debug_log.append((cand["unit"], cand["bbox"], cand["kind"], "rejected",
                                       f"same room as existing bed {dup_of['bbox']}, {reason}"))
                continue
            accepted_doubles_by_unit.setdefault(cand["unit"], []).append(cand)
        if debug_log is not None:
            debug_log.append((cand["unit"], cand["bbox"], cand["kind"], "accepted", reason))
        beds.append(cand)
    return beds


def bed_headboard_side(bed, fd, wall_tol=3.0, stand_tol=4.0, stand_size=(5.0, 9.0)):
    """Which end of the bed is the headboard: the short side nearest a real wall (a unit
    polygon edge within `wall_tol`), or if neither short side is near a wall, the one with a
    nightstand-sized body (5-9pt) within `stand_tol` of it."""
    x0, y0, x1, y1 = bed["bbox"]
    poly = fd["units"][bed["unit"]]["poly"]
    candidates = ["left", "right"] if bed["long_axis"] == "x" else ["top", "bottom"]
    segs = {"left": ((x0, y0), (x0, y1)), "right": ((x1, y0), (x1, y1)),
            "top": ((x0, y0), (x1, y0)), "bottom": ((x0, y1), (x1, y1))}
    edges = list(zip(poly, poly[1:] + poly[:1]))

    def dist_to_poly(pt):
        return min(_dist_point_to_segment(pt, s0, s1) for s0, s1 in edges)

    mids = {side: ((segs[side][0][0] + segs[side][1][0]) / 2, (segs[side][0][1] + segs[side][1][1]) / 2) for side in candidates}
    outward = {
        "left": (mids["left"][0] - 0.1, mids["left"][1]) if "left" in mids else None,
        "right": (mids["right"][0] + 0.1, mids["right"][1]) if "right" in mids else None,
        "top": (mids["top"][0], mids["top"][1] - 0.1) if "top" in mids else None,
        "bottom": (mids["bottom"][0], mids["bottom"][1] + 0.1) if "bottom" in mids else None,
    }
    wall_d = {side: dist_to_poly(outward[side]) for side in candidates}
    best_wall = min(wall_d, key=wall_d.get)
    if wall_d[best_wall] <= wall_tol:
        return best_wall

    small_bodies = []
    for cid in ("c06_furn", "c02", "c05"):
        c = next((cc for cc in fd["clusters"] if cc["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths") or []:
            bb = _path_bbox(p)
            if bb is None:
                continue
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            if stand_size[0] <= bw <= stand_size[1] and stand_size[0] <= bh <= stand_size[1]:
                small_bodies.append(((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2))

    def stand_d(side):
        s0, s1 = segs[side]
        if not small_bodies:
            return 1e18
        return min(_dist_point_to_segment(pt, s0, s1) for pt in small_bodies)

    stand_dd = {side: stand_d(side) for side in candidates}
    best_stand = min(stand_dd, key=stand_dd.get)
    if stand_dd[best_stand] <= stand_tol + 4.5:  # +~half a nightstand's own footprint
        return best_stand
    return best_wall  # last resort: whichever side is nearer a wall, even past wall_tol


def strip_bed_originals(fd, beds, margin=1.5, exempt_cids=("c08", "c12", "c14")):
    """Erase every original path (any cluster except real walls/columns) inside a detected
    bed's own footprint (+margin) before drawing the clean etalon symbol."""
    for bed in beds:
        x0, y0, x1, y1 = bed["bbox"]
        x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
        for c in fd["clusters"]:
            if c["id"] in exempt_cids:
                continue
            paths = c.get("paths") or []
            if not paths:
                continue
            keep, keep_seq = [], []
            seqs = c.get("seq")
            for i, p in enumerate(paths):
                bb = _path_bbox(p)
                if bb is None:
                    keep.append(p)
                    if seqs:
                        keep_seq.append(seqs[i])
                    continue
                cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    continue
                keep.append(p)
                if seqs:
                    keep_seq.append(seqs[i])
            c["paths"] = keep
            if seqs:
                c["seq"] = keep_seq
    return fd


def bed_symbol_svg(bed, headboard_side, ink, pillow_h_frac=0.22, pillow_w_frac=0.42,
                    corner_r=1.0, cut_leg=2.5, stroke=0.5, pillow_stroke=0.45):
    """One consistent bed symbol (the etalon): white body with a small cut corner at the foot,
    a separator line closing off the pillow zone at the headboard, and one rounded pillow
    (single) or two (double) side by side with a gap."""
    x0, y0, x1, y1 = bed["bbox"]
    bw, bh = x1 - x0, y1 - y0
    L, W = (bw, bh) if headboard_side in ("left", "right") else (bh, bw)

    if headboard_side == "left":
        to_xy = lambda a, c: (x0 + a, y0 + c)
    elif headboard_side == "right":
        to_xy = lambda a, c: (x1 - a, y0 + c)
    elif headboard_side == "top":
        to_xy = lambda a, c: (x0 + c, y0 + a)
    else:
        to_xy = lambda a, c: (x0 + c, y1 - a)

    def rect_g(a0, a1, c0, c1):
        p1, p2 = to_xy(a0, c0), to_xy(a1, c1)
        return (min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1]))

    cut = max(0.0, min(cut_leg, L * 0.3, W * 0.3))
    outer_pts = [to_xy(*ac) for ac in ((0, 0), (L, 0), (L, W - cut), (L - cut, W), (0, W))]
    parts = [f'<polygon points="{points_attr(outer_pts)}" fill="white" stroke="{ink}" stroke-width="{stroke}"/>']

    ph = L * pillow_h_frac
    p1, p2 = to_xy(ph, 0), to_xy(ph, W)
    parts.append(f'<line x1="{p1[0]:.2f}" y1="{p1[1]:.2f}" x2="{p2[0]:.2f}" y2="{p2[1]:.2f}" stroke="{ink}" stroke-width="{stroke}"/>')

    if bed["kind"] == "double":
        pw = W * pillow_w_frac
        pillow_spans = [(0, pw), (W - pw, W)]
    else:
        pw = W * 0.76
        pillow_spans = [((W - pw) / 2, (W - pw) / 2 + pw)]
    for c0, c1 in pillow_spans:
        rx0, ry0, rx1, ry1 = rect_g(0, ph, c0, c1)
        parts.append(f'<rect x="{rx0:.2f}" y="{ry0:.2f}" width="{rx1 - rx0:.2f}" height="{ry1 - ry0:.2f}" '
                     f'rx="{corner_r}" ry="{corner_r}" fill="white" stroke="{ink}" stroke-width="{pillow_stroke}"/>')
    return "".join(parts)


def _nearest_side_to_point(bbox, pt):
    """Which of the 4 sides of `bbox` has its centre nearest `pt` - used to pick the side of
    a stair/lift enclosure that should face the shared lobby (the lift bodies, or whatever
    landmark is passed in)."""
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = pt[0] - cx, pt[1] - cy
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "bottom" if dy > 0 else "top"


def _side_already_walled(fd, bbox, side, tol=2.0, cids=("c08", "c12", "c14")):
    """True if a real (kept, post filter_wall_fills) wall/column fill already sits right on
    this side of `bbox`, within `tol` - so we don't draw a second, doubled-up wall band there."""
    x0, y0, x1, y1 = bbox
    if side == "top":
        line_c, seg = y0, (x0, x1)
    elif side == "bottom":
        line_c, seg = y1, (x0, x1)
    elif side == "left":
        line_c, seg = x0, (y0, y1)
    else:
        line_c, seg = x1, (y0, y1)
    for cid in cids:
        c = next((cc for cc in fd["clusters"] if cc["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths") or []:
            bb = _path_bbox(p)
            if bb is None:
                continue
            if side in ("top", "bottom"):
                near = min(abs(bb[1] - line_c), abs(bb[3] - line_c)) <= tol
                overlap = min(bb[2], seg[1]) - max(bb[0], seg[0]) > 0
            else:
                near = min(abs(bb[0] - line_c), abs(bb[2] - line_c)) <= tol
                overlap = min(bb[3], seg[1]) - max(bb[1], seg[0]) > 0
            if near and overlap:
                return True
    return False


def stair_enclosure_svg(bbox, door_side, wall_width=2.4, door_width=10.0, expand=3.0, ink="#182E46", skip_sides=()):
    """Wall band (ink) around a stair's own bbox (expanded by `expand`) on every side except
    `door_side` and any side already covered by a real wall (`skip_sides`) - plus the jamb
    points for a synthesized door swing on `door_side`, matching the reference's stair
    enclosure. Returns (wall_svg_parts, (jamb1, jamb2, outward))."""
    x0, y0, x1, y1 = bbox
    x0, y0, x1, y1 = x0 - expand, y0 - expand, x1 + expand, y1 + expand
    parts = []

    def hwall(xa, xb, y, below):
        yy0, yy1 = (y, y + wall_width) if below else (y - wall_width, y)
        parts.append(f'<rect x="{min(xa, xb):.2f}" y="{yy0:.2f}" width="{abs(xb - xa):.2f}" height="{wall_width:.2f}" fill="{ink}"/>')

    def vwall(ya, yb, x, right):
        xx0, xx1 = (x, x + wall_width) if right else (x - wall_width, x)
        parts.append(f'<rect x="{xx0:.2f}" y="{min(ya, yb):.2f}" width="{wall_width:.2f}" height="{abs(yb - ya):.2f}" fill="{ink}"/>')

    for side in ("top", "bottom", "left", "right"):
        if side == door_side or side in skip_sides:
            continue
        if side == "top":
            hwall(x0 - wall_width, x1 + wall_width, y0, False)
        elif side == "bottom":
            hwall(x0 - wall_width, x1 + wall_width, y1, True)
        elif side == "left":
            vwall(y0 - wall_width, y1 + wall_width, x0, False)
        else:
            vwall(y0 - wall_width, y1 + wall_width, x1, True)

    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = door_width / 2
    if door_side == "left":
        jamb1, jamb2, outward = (x0, cy - half), (x0, cy + half), (-1.0, 0.0)
    elif door_side == "right":
        jamb1, jamb2, outward = (x1, cy - half), (x1, cy + half), (1.0, 0.0)
    elif door_side == "top":
        jamb1, jamb2, outward = (cx - half, y0), (cx + half, y0), (0.0, -1.0)
    else:
        jamb1, jamb2, outward = (cx - half, y1), (cx + half, y1), (0.0, 1.0)
    return parts, (jamb1, jamb2, outward)


def nightstand_candidate_rects(bed, headboard_side, w_pt=5.0, d_pt=9.0):
    """Two candidate nightstand slots flanking a double bed's headboard end, both touching the
    same wall the headboard is against (d_pt deep off that wall, w_pt wide along it, sitting
    just beyond the bed's own width on either side)."""
    x0, y0, x1, y1 = bed["bbox"]
    if headboard_side == "left":
        return [(x0, y0 - d_pt, x0 + w_pt, y0), (x0, y1, x0 + w_pt, y1 + d_pt)]
    if headboard_side == "right":
        return [(x1 - w_pt, y0 - d_pt, x1, y0), (x1 - w_pt, y1, x1, y1 + d_pt)]
    if headboard_side == "top":
        return [(x0 - d_pt, y0, x0, y0 + w_pt), (x1, y0, x1 + d_pt, y0 + w_pt)]
    return [(x0 - d_pt, y1 - w_pt, x0, y1), (x1, y1 - w_pt, x1 + d_pt, y1)]


def rect_occupied(fd, rect, cids=("c06_furn", "c02", "c05", "c07", "c08", "c12", "c14"), margin=0.5):
    """True if some real furniture/wall path already overlaps this candidate rect."""
    rx0, ry0, rx1, ry1 = rect
    for cid in cids:
        c = next((cc for cc in fd["clusters"] if cc["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths") or []:
            bb = _path_bbox(p)
            if bb is None:
                continue
            if bb[2] < rx0 + margin or bb[0] > rx1 - margin or bb[3] < ry0 + margin or bb[1] > ry1 - margin:
                continue
            return True
    return False


def find_existing_nightstand(fd, rect, size_range=(5.0, 9.0), near_pt=6.0):
    """A nightstand-sized (5-9pt square) body already sitting at/near this candidate slot in
    c06_furn or c02 - if found, we keep it rather than draw a second one on top."""
    rx0, ry0, rx1, ry1 = rect
    rcx, rcy = (rx0 + rx1) / 2, (ry0 + ry1) / 2
    for cid in ("c06_furn", "c02"):
        c = next((cc for cc in fd["clusters"] if cc["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths") or []:
            bb = _path_bbox(p)
            if bb is None:
                continue
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            if not (size_range[0] <= bw <= size_range[1] and size_range[0] <= bh <= size_range[1]):
                continue
            cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            if math.hypot(cx - rcx, cy - rcy) <= near_pt:
                return bb
    return None


def nightstand_svg(rect, ink, lamp_r=1.2, stroke=0.5):
    x0, y0, x1, y1 = rect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return (
        f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}" fill="white" stroke="{ink}" stroke-width="{stroke}"/>'
        f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{lamp_r}" fill="none" stroke="{ink}" stroke-width="0.4"/>'
    )


_AREA_NUM_RE = re.compile(r"^\d{1,2}[.,]\d$")  # must carry a decimal fraction - a bare
# 2-digit integer ("24") is indistinguishable from an unrelated PDF annotation (a travel-
# distance callout, a grid reference) and produced exactly that false positive in testing.


def extract_room_area_labels(fd, poly, exclude_value=None, min_v=1.0, max_v=60.0,
                              pair_dx=22.0, pair_dy=4.0, margin_pt=6.0,
                              max_frac_of_total=0.9, sum_frac_of_total=1.05):
    """Small room-area callouts from the architect's own PDF text ("11,4 m2", split by the
    exporter into a number token and a unit-suffix token) - accepted only if: the number is
    strictly inside `poly` with >= margin_pt clearance from every edge (a label sitting right
    on/past the boundary is almost always the NEIGHBOUR's, not this unit's own); a "m2"/"m²"
    token sits within (pair_dx, pair_dy) of it (an unpaired bare number is not a room-area
    callout at all - see the tightened _AREA_NUM_RE note); the value is <= max_frac_of_total
    of the unit's own grand total (catches an adjacent unit's total bleeding in, e.g. "45,0 m2"
    surfacing inside a 34.2 m2 studio); and the running sum of everything accepted so far stays
    <= sum_frac_of_total of the total (a last-resort sanity cap)."""
    texts = fd.get("texts") or []
    unit_tokens = [t for t in texts if re.match(r"^m2$|^m²$|^м2$|^м²$", t["str"].strip(), re.IGNORECASE)]
    edges = list(zip(poly, poly[1:] + poly[:1])) if len(poly) >= 2 else []

    def margin_ok(pt):
        if not edges:
            return True
        return min(_dist_point_to_segment(pt, s0, s1) for s0, s1 in edges) >= margin_pt

    candidates = []
    for t in texts:
        raw = t["str"].strip()
        if not _AREA_NUM_RE.match(raw):
            continue
        try:
            val = float(raw.replace(",", "."))
        except ValueError:
            continue
        if not (min_v <= val <= max_v):
            continue
        if exclude_value is not None and val > max_frac_of_total * exclude_value + 1e-6:
            continue
        pt = (t["x"], t["y"])
        if not _point_in_poly(pt, poly) or not margin_ok(pt):
            continue
        paired = next((ut for ut in unit_tokens if 0 <= ut["x"] - t["x"] <= pair_dx and abs(ut["y"] - t["y"]) <= pair_dy), None)
        if paired is None:
            continue
        cx, cy = (t["x"] + paired["x"]) / 2, (t["y"] + paired["y"]) / 2
        candidates.append((val, cx, cy, t.get("size", 7.0)))

    cap = sum_frac_of_total * exclude_value if exclude_value is not None else None
    out, running = [], 0.0
    for val, cx, cy, sz in candidates:
        if cap is not None and running + val > cap:
            continue
        running += val
        out.append((val, cx, cy, sz))
    return out


def core_fill_svg(core_bbox, polys, hull, w, h, fill):
    """Flat core fill, clipped to core_bbox MINUS every unit/balcony polygon MINUS anything
    outside the building's own exterior contour (the union hull of every unit+balcony polygon)
    - done as an SVG mask so no polygon-boolean math is needed. Fixes the fill bleeding into
    the balcony strip above 1009/1010."""
    x0, y0, x1, y1 = core_bbox
    mid = f"core-fill-mask-{int(x0)}-{int(y0)}"
    outer = f"M0,0 L{w:.2f},0 L{w:.2f},{h:.2f} L0,{h:.2f} Z"
    hull_d = "M" + " L".join(f"{px:.2f},{py:.2f}" for px, py in hull) + " Z"
    parts = [f'<mask id="{mid}">',
             f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}" fill="white"/>',
             f'<path d="{outer} {hull_d}" fill="black" fill-rule="evenodd"/>']
    for poly in polys:
        parts.append(f'<polygon points="{points_attr(poly)}" fill="black"/>')
    parts.append("</mask>")
    parts.append(f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}" fill="{fill}" mask="url(#{mid})"/>')
    return "".join(parts)


def remove_corridor_signs(fd, max_bbox=12.0, core_bbox=None, core_margin=2.0, core_wall_cids=("c07",)):
    """The corridor should read as bare floor + walls + doors: drop anything - in ANY cluster,
    including c07/c08/c12/c14 now that those are already pre-filtered to real wall/column
    fill only (see filter_wall_fills) - with bbox <= max_bbox on each side, whose centre sits
    inside the building's own footprint (the convex hull of every unit+balcony polygon) but
    outside every unit polygon. This runs INSIDE the stair/lift core too (tested: the only
    core casualties are two sub-14pt partial tread-line fragments out of ~15 treads - a
    trivial trade-off for actually clearing the wayfinding pictograms (wheelchair/exit/arrow
    icons) that live right at the core's edge, which a core-wide size exemption would have
    protected right along with them).
    `core_wall_cids`, if `core_bbox` is given, are additionally stripped of EVERY path
    (regardless of size) centred inside the core: this is the raw cross-hatch wall linework
    (c07) that used to outline the core - replaced now by the flat core_fill rect drawn under
    everything, so no hatch texture should reach the core at all."""
    polys = []
    for u in fd["units"].values():
        if u.get("poly"):
            polys.append(u["poly"])
        if u.get("balcony"):
            polys.append(u["balcony"])
    if not polys:
        return fd
    all_pts = [pt for poly in polys for pt in poly]
    hull = convex_hull(all_pts)
    cb = None
    if core_bbox:
        cb = (core_bbox[0] - core_margin, core_bbox[1] - core_margin, core_bbox[2] + core_margin, core_bbox[3] + core_margin)
    for c in fd["clusters"]:
        paths = c.get("paths") or []
        if not paths:
            continue
        strip_all_in_core = bool(cb) and c["id"] in core_wall_cids
        drop = set()
        for i, p in enumerate(paths):
            bb = _path_bbox(p)
            if bb is None:
                continue
            cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            in_core = cb and cb[0] <= cx <= cb[2] and cb[1] <= cy <= cb[3]
            # +1.5pt expanded membership (see _bbox_near_any_unit): units pressed right against
            # the core (309, 310, ...) can have a real partition's bbox centre fall inside the
            # (core_margin-padded) core box, or a hair outside their own polygon's exact edge -
            # either used to be enough for this sweep to erase real interior partitions.
            near_unit = _bbox_near_any_unit(bb, polys, 1.5)
            if strip_all_in_core and in_core and not near_unit:
                drop.add(i)
                continue
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            if bw > max_bbox or bh > max_bbox:
                continue
            if not _point_in_poly((cx, cy), hull):
                continue
            if near_unit:
                continue
            drop.add(i)
        if drop:
            c["paths"] = [p for i, p in enumerate(paths) if i not in drop]
            if c.get("seq"):
                c["seq"] = [s for i, s in enumerate(c["seq"]) if i not in drop]
    return fd



def find_c07_gaps(poly, c07_paths, min_gap=8.0, max_gap=14.0, min_continuation=10.0, merge_tol=0.6):
    """Bathroom-door-scale gap in a c07 partition line, found as two chains of connected c07
    fragments (each spanning >= min_continuation) whose nearest facing endpoints are
    min_gap..max_gap apart and roughly collinear with each chain's own local direction - so a
    real gap in an otherwise-straight wall run is found, not two unrelated furniture strokes.
    Returns a list of (jamb1, jamb2)."""
    x0, y0, x1, y1 = bbox_of([poly])
    cand = [p for p in c07_paths if not (_path_bbox(p)[2] < x0 - 2 or _path_bbox(p)[0] > x1 + 2 or _path_bbox(p)[3] < y0 - 2 or _path_bbox(p)[1] > y1 + 2)]
    if len(cand) < 2:
        return []
    eps = [(_path_points(p)[0], _path_points(p)[-1]) for p in cand]
    n = len(cand)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    vmap = {}

    def vkey(pt):
        return (round(pt[0] / merge_tol), round(pt[1] / merge_tol))

    for i, (s, e) in enumerate(eps):
        vmap.setdefault(vkey(s), []).append(i)
        vmap.setdefault(vkey(e), []).append(i)
    for i, (s, e) in enumerate(eps):
        for pt in (s, e):
            kx, ky = vkey(pt)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in vmap.get((kx + dx, ky + dy), []):
                        if j != i:
                            union(i, j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    chains = []
    for idxs in groups.values():
        pts_all = []
        length = 0.0
        for i in idxs:
            pp = _path_points(cand[i])
            pts_all.extend(pp)
            for k in range(len(pp) - 1):
                length += math.hypot(pp[k + 1][0] - pp[k][0], pp[k + 1][1] - pp[k][1])
        if length < min_continuation or not pts_all:
            continue
        cxm = sum(pt[0] for pt in pts_all) / len(pts_all)
        cym = sum(pt[1] for pt in pts_all) / len(pts_all)
        a = max(pts_all, key=lambda pt: math.hypot(pt[0] - cxm, pt[1] - cym))
        b = max(pts_all, key=lambda pt: math.hypot(pt[0] - a[0], pt[1] - a[1]))
        chains.append({"ends": (a, b), "length": length})

    def chain_dir(chain, near_pt):
        a, b = chain["ends"]
        other = b if near_pt == a else a
        dx, dy = other[0] - near_pt[0], other[1] - near_pt[1]
        l = math.hypot(dx, dy) or 1.0
        return (dx / l, dy / l)

    gaps = []
    for ci in range(len(chains)):
        for cj in range(ci + 1, len(chains)):
            A, B = chains[ci], chains[cj]
            best = None
            for pa in A["ends"]:
                for pb in B["ends"]:
                    d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                    if best is None or d < best[0]:
                        best = (d, pa, pb)
            d, pa, pb = best
            if not (min_gap <= d <= max_gap):
                continue
            gdx, gdy = pb[0] - pa[0], pb[1] - pa[1]
            glen = math.hypot(gdx, gdy) or 1.0
            gdir = (gdx / glen, gdy / glen)
            da = chain_dir(A, pa)
            db = chain_dir(B, pb)
            cos_a = abs(gdir[0] * da[0] + gdir[1] * da[1])
            cos_b = abs(gdir[0] * db[0] + gdir[1] * db[1])
            if cos_a < 0.85 or cos_b < 0.85:
                continue
            gaps.append((pa, pb))
    return gaps


def find_c07_free_ends(poly, c07_paths, min_continuation=10.0, merge_tol=0.6, boundary_margin=4.0, door_width=8.0):
    """A doorway that isn't a gap IN a wall but a wall that simply STOPS (a partition drawn
    only partway, leaving the rest of the opening unwalled entirely - e.g. a bathroom wall
    that ends and the room is just "open" beyond it, with no facing jamb for find_c07_gaps to
    pair against). Finds c07 chain endpoints that sit INSIDE the unit (>= boundary_margin from
    every polygon edge) and are not shared with another chain (a true free end, not a corner),
    then returns a synthetic (jamb1, jamb2) door-scale opening `door_width` pt long, running
    from that end perpendicular to the wall's local direction into the open room."""
    x0, y0, x1, y1 = bbox_of([poly])
    cand = [p for p in c07_paths if not (_path_bbox(p)[2] < x0 - 2 or _path_bbox(p)[0] > x1 + 2 or _path_bbox(p)[3] < y0 - 2 or _path_bbox(p)[1] > y1 + 2)]
    if len(cand) < 1:
        return []
    eps = [(_path_points(p)[0], _path_points(p)[-1]) for p in cand]
    n = len(cand)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    vmap = {}

    def vkey(pt):
        return (round(pt[0] / merge_tol), round(pt[1] / merge_tol))

    for i, (s0, e0) in enumerate(eps):
        vmap.setdefault(vkey(s0), []).append(i)
        vmap.setdefault(vkey(e0), []).append(i)
    for i, (s0, e0) in enumerate(eps):
        for pt in (s0, e0):
            kx, ky = vkey(pt)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in vmap.get((kx + dx, ky + dy), []):
                        if j != i:
                            union(i, j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    edges = list(zip(poly, poly[1:] + poly[:1]))

    def margin_ok(pt):
        return min(_dist_point_to_segment(pt, s0, s1) for s0, s1 in edges) >= boundary_margin

    # how many chains (other than this one) touch each merge-vertex, to tell a true free end
    # (touched by exactly 1 chain) from an interior corner/junction (touched by >=2)
    vertex_owner_count = {}
    for i, (s0, e0) in enumerate(eps):
        for pt in (s0, e0):
            vertex_owner_count[vkey(pt)] = vertex_owner_count.get(vkey(pt), 0) + 1

    results = []
    for idxs in groups.values():
        pts_all = []
        length = 0.0
        for i in idxs:
            pp = _path_points(cand[i])
            pts_all.extend(pp)
            for k in range(len(pp) - 1):
                length += math.hypot(pp[k + 1][0] - pp[k][0], pp[k + 1][1] - pp[k][1])
        if length < min_continuation or not pts_all:
            continue
        cxm = sum(pt[0] for pt in pts_all) / len(pts_all)
        cym = sum(pt[1] for pt in pts_all) / len(pts_all)
        a = max(pts_all, key=lambda pt: math.hypot(pt[0] - cxm, pt[1] - cym))
        b = max(pts_all, key=lambda pt: math.hypot(pt[0] - a[0], pt[1] - a[1]))
        for end, other in ((a, b), (b, a)):
            if vertex_owner_count.get(vkey(end), 0) != 1:
                continue  # a junction/corner shared with another chain, not a free end
            if not _point_in_poly(end, poly) or not margin_ok(end):
                continue  # touches the unit's own outer boundary - a real wall-to-wall corner
            dx, dy = other[0] - end[0], other[1] - end[1]
            dlen = math.hypot(dx, dy) or 1.0
            wall_dir = (dx / dlen, dy / dlen)
            perp = (-wall_dir[1], wall_dir[0])
            jamb2 = (end[0] + perp[0] * door_width, end[1] + perp[1] * door_width)
            if not _point_in_poly(jamb2, poly):
                perp = (-perp[0], -perp[1])
                jamb2 = (end[0] + perp[0] * door_width, end[1] + perp[1] * door_width)
                if not _point_in_poly(jamb2, poly):
                    continue
            results.append((end, jamb2))
    return results


def collect_wall_solid_ends(fd, poly, expand=3.0):
    """Every "solid end" that could bound one side of a door opening, inside the unit polygon
    (expanded by `expand`): a c07 partition path's own two endpoints, or the two ends of a
    c08/c12/c14 wall-fill rect's LONG axis (its short axis is the wall's own thickness, so the
    long axis is the direction the wall - and any gap in it - runs). Found empirically: a real
    bathroom door can be a gap between two c08 fill strips with NO c07 involved at all (1005's
    case - two 1.4pt-thick c08 slivers with a 14.2pt gap between them, jamb-tick marks from
    c06_furn sitting right at each end), which is exactly why the old c07-only gap-pairing
    could never find it. Returns [(point, unit_direction, source_cid), ...]."""
    x0, y0, x1, y1 = bbox_of([poly])
    x0, y0, x1, y1 = x0 - expand, y0 - expand, x1 + expand, y1 + expand
    ends = []
    c07 = next((cc for cc in fd["clusters"] if cc["id"] == "c07"), None)
    if c07:
        for p in c07.get("paths") or []:
            bb = _path_bbox(p)
            if bb is None or bb[2] < x0 or bb[0] > x1 or bb[3] < y0 or bb[1] > y1:
                continue
            pts = _path_points(p)
            if len(pts) < 2:
                continue
            a, b = pts[0], pts[-1]
            dx, dy = b[0] - a[0], b[1] - a[1]
            dlen = math.hypot(dx, dy) or 1.0
            d = (dx / dlen, dy / dlen)
            ends.append((a, d, "c07"))
            ends.append((b, d, "c07"))
    for cid in ("c08", "c12", "c14"):
        c = next((cc for cc in fd["clusters"] if cc["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths") or []:
            bb = _path_bbox(p)
            if bb is None or bb[2] < x0 or bb[0] > x1 or bb[3] < y0 or bb[1] > y1:
                continue
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            if bw <= 0 or bh <= 0:
                continue
            if bw >= bh:
                a, b = (bb[0], (bb[1] + bb[3]) / 2), (bb[2], (bb[1] + bb[3]) / 2)
                d = (1.0, 0.0)
            else:
                a, b = ((bb[0] + bb[2]) / 2, bb[1]), ((bb[0] + bb[2]) / 2, bb[3])
                d = (0.0, 1.0)
            ends.append((a, d, cid))
            ends.append((b, d, cid))
    return ends


def _wall_ink_between(fd, p1, p2, cids=("c07", "c08", "c12", "c14"), n_samples=5, tol=0.6):
    """True if any wall/partition fill or line covers a sample point on the p1-p2 segment -
    i.e. there's real wall material there, so it can't be a door opening."""
    for i in range(n_samples):
        t = (i + 0.5) / n_samples
        pt = (p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t)
        for cid in cids:
            c = next((cc for cc in fd["clusters"] if cc["id"] == cid), None)
            if not c:
                continue
            for p in c.get("paths") or []:
                bb = _path_bbox(p)
                if bb is None:
                    continue
                if bb[0] - tol <= pt[0] <= bb[2] + tol and bb[1] - tol <= pt[1] <= bb[3] + tol:
                    return True
    return False


def find_wall_gap_doors(fd, poly, min_gap=7.0, max_gap=14.5, expand=3.0, align_tol_deg=5.0,
                         existing_mids=None, dedup_tol=4.0):
    """General doorway model: a 7-14.5pt gap between two solid ends (c07 endpoint, or a
    c08/c12/c14 fill rect's long-axis end) that lie on a common line - the connecting segment
    parallel (within align_tol_deg) to at least one end's own wall direction, and carrying no
    wall ink itself (sampled). Deduplicated against `existing_mids` (door midpoints already
    found by the entrance/balcony/c07-gap detectors) within dedup_tol. This is what actually
    catches 1005's bathroom door (a gap between two c08 slivers, no c07 involved at all)."""
    ends = collect_wall_solid_ends(fd, poly, expand)
    cos_thresh = math.cos(math.radians(align_tol_deg))
    existing_mids = existing_mids or []
    results = []
    seen_mid = []
    n = len(ends)
    for i in range(n):
        p1, d1, _s1 = ends[i]
        for j in range(i + 1, n):
            p2, d2, _s2 = ends[j]
            dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            if not (min_gap <= dist <= max_gap):
                continue
            gdir = ((p2[0] - p1[0]) / dist, (p2[1] - p1[1]) / dist)
            cos1 = abs(gdir[0] * d1[0] + gdir[1] * d1[1])
            cos2 = abs(gdir[0] * d2[0] + gdir[1] * d2[1])
            if cos1 < cos_thresh and cos2 < cos_thresh:
                continue
            if _wall_ink_between(fd, p1, p2):
                continue
            mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
            if not _point_in_poly(mid, poly):
                continue
            if any(math.hypot(mid[0] - m[0], mid[1] - m[1]) < dedup_tol for m in existing_mids):
                continue
            if any(math.hypot(mid[0] - m[0], mid[1] - m[1]) < dedup_tol for m in seen_mid):
                continue
            seen_mid.append(mid)
            results.append((p1, p2))
    return results


def pick_swing_side(mid, perp, block_polys, probe_dist=10.0, room_poly=None, room_probe_max=70.0, room_probe_step=5.0):
    """Which of the two perpendicular directions off an internal door gap to swing the leaf
    into. Two modes: with `room_poly` given, swing into the SMALLER of the two rooms the gap
    connects (standard drafting convention - the leaf shouldn't sweep through the more-used
    space) - measured by how far a ray in each direction travels before leaving the unit
    polygon or entering a blocker, fewer clear steps means the smaller room. Without
    `room_poly` (or if both sides measure equal), fall back to the original local-obstruction
    probe: swing away from whichever side is immediately blocked (fewer nearby wall/furniture
    polygons) - used for e.g. a bathroom door swinging toward the room rather than a fixture."""
    if room_poly is not None:
        def clear_steps(direction):
            n = 0
            for i in range(1, int(room_probe_max / room_probe_step) + 1):
                pt = (mid[0] + direction[0] * room_probe_step * i, mid[1] + direction[1] * room_probe_step * i)
                if not _point_in_poly(pt, room_poly) or any(_point_in_poly(pt, bp) for bp in block_polys):
                    break
                n = i
            return n

        n1, n2 = clear_steps(perp), clear_steps((-perp[0], -perp[1]))
        if n1 != n2:
            return perp if n1 < n2 else (-perp[0], -perp[1])
        # equal - fall through to the local-obstruction probe below

    p1 = (mid[0] + perp[0] * probe_dist, mid[1] + perp[1] * probe_dist)
    p2 = (mid[0] - perp[0] * probe_dist, mid[1] - perp[1] * probe_dist)

    def blocked(pt):
        return any(_point_in_poly(pt, bp) for bp in block_polys)

    b1, b2 = blocked(p1), blocked(p2)
    if b1 and not b2:
        return (-perp[0], -perp[1])
    return perp

def _nearby_polys(floor_data, cid, x0, y0, x1, y1, pad=5.0):
    polys = []
    for c in floor_data["clusters"]:
        if c["id"] != cid:
            continue
        for p in c.get("paths", []):
            bb = _path_bbox(p)
            if bb is None or bb[2] < x0 - pad or bb[0] > x1 + pad or bb[3] < y0 - pad or bb[1] > y1 + pad:
                continue
            pts = _path_points(p)
            if len(pts) >= 3:
                polys.append(pts)
    return polys


def _max_free_rect(mask_img, w, h):
    """Largest all-free (nonzero) axis-aligned rectangle in a binary raster, via the classic
    histogram/stack method. Returns (top, left, height, width) in grid cells, or None."""
    data = list(mask_img.getdata())
    rows = [data[r * w:(r + 1) * w] for r in range(h)]
    heights = [0] * w
    best = None  # (area, top, left, h, w)
    for r in range(h):
        row = rows[r]
        for c in range(w):
            heights[c] = heights[c] + 1 if row[c] else 0
        stack = []
        for c in range(w + 1):
            hh = heights[c] if c < w else 0
            start = c
            while stack and stack[-1][1] >= hh:
                s2, sh = stack.pop()
                width = c - s2
                area = sh * width
                if best is None or area > best[0]:
                    best = (area, r - sh + 1, s2, sh, width)
                start = s2
            stack.append((start, hh))
    if best is None or best[0] == 0:
        return None
    _, top, left, hh, ww = best
    return top, left, hh, ww


def find_free_rect_in_poly(floor_data, style, poly, mask_cids, min_w, min_h, context="floor", res=1.0, extra_obstacles=None):
    """Largest axis-aligned rectangle inside `poly` that clears every mask cluster (walls,
    partitions, furniture bodies, furniture/fixture line-art) - used to park a floor-plan
    area label on bare floor instead of over a wall or a piece of furniture. Returns
    (x, y, w, h) in floor-pt coords, or None if nothing meets (min_w, min_h).
    `extra_obstacles`, if given, is a list of (x0,y0,x1,y1) boxes to mask out in addition to
    the mask_cids clusters - used for symbols (beds) drawn straight to the SVG body rather
    than left in floor_data, which this function otherwise can't see."""
    x0, y0, x1, y1 = bbox_of([poly])
    W = max(1, math.ceil((x1 - x0) * res))
    H = max(1, math.ceil((y1 - y0) * res))
    if W < 2 or H < 2:
        return None
    im = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(im)

    def px(pt):
        return ((pt[0] - x0) * res, (pt[1] - y0) * res)

    draw.polygon([px(p) for p in poly], fill=255)  # room interior = free

    clusters_by_id = {c["id"]: c for c in floor_data["clusters"]}
    style_clusters = style["clusters"]
    for cid in mask_cids:
        spec = style_clusters.get(cid)
        c = clusters_by_id.get(cid)
        if not spec or not c:
            continue
        fill = spec.get("fill")
        for p in c.get("paths", []):
            pts = _path_points(p)
            if len(pts) < 2:
                continue
            ppx = [px(pt) for pt in pts]
            xs = [q[0] for q in ppx]
            ys = [q[1] for q in ppx]
            if fill:
                if max(xs) < 0 or min(xs) > W or max(ys) < 0 or min(ys) > H or len(pts) < 3:
                    continue
                draw.polygon(ppx, fill=0)
            else:
                w_pt = _cluster_width(spec, context)
                w_px = max(1, round(w_pt * res))
                if max(xs) < -w_px or min(xs) > W + w_px or max(ys) < -w_px or min(ys) > H + w_px:
                    continue
                draw.line(ppx, fill=0, width=w_px)

    if extra_obstacles:
        for bb in extra_obstacles:
            bx0, by0 = px((bb[0], bb[1]))
            bx1, by1 = px((bb[2], bb[3]))
            draw.rectangle([bx0, by0, bx1, by1], fill=0)

    found = _max_free_rect(im, W, H)
    if not found:
        return None
    top, left, hh, ww = found
    if ww < min_w or hh < min_h:
        return None
    return (x0 + left / res, y0 + top / res, ww / res, hh / res)


def _union_find_groups(boxes, gap):
    """Groups of indices into `boxes` whose bboxes overlap or sit within `gap` of each other."""
    n = len(boxes)
    if n == 0:
        return {}
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def near(b1, b2):
        return not (b1[2] + gap < b2[0] or b2[2] + gap < b1[0] or b1[3] + gap < b2[1] or b2[3] + gap < b1[1])

    for i in range(n):
        for j in range(i + 1, n):
            if near(boxes[i], boxes[j]):
                union(i, j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return groups


def split_c06(fd):
    """c06 mixes room-floor fills (a handful of large polygons) with furniture-body silhouettes
    (bed/desk/kitchen-counter/nightstand - numerous but small), all originally the same white
    fill, which is why painting all of c06 cream made the furniture vanish into the floor.
    Split by each shape's own bbox area into two synthetic clusters so they can be styled
    differently (see style.json geometry.c06_furniture_max_area_pt2)."""
    src = next((c for c in fd["clusters"] if c["id"] == "c06"), None)
    if src is None:
        return fd
    floor_paths, furn_paths = [], []
    for p in src.get("paths", []):
        bb = _path_bbox(p)
        if bb is None:
            continue
        area = (bb[2] - bb[0]) * (bb[3] - bb[1])
        (floor_paths if area >= 1500.0 else furn_paths).append(p)
    fd["clusters"] = [c for c in fd["clusters"] if c["id"] != "c06"]
    fd["clusters"].append({"id": "c06_floor", "paths": floor_paths})
    fd["clusters"].append({"id": "c06_furn", "paths": furn_paths})
    return fd


def extract_columns(fd, size_min=3.0, size_max=26.0, rect_ratio_min=0.85):
    """A structural column shows up in the source as a small rectangle carved OUT of the unit
    polygon (the room boundary has a notch cut around its footprint) but drawn - like ordinary
    furniture - in c06 (post split_c06, c06_furn), which paints it white-fill/ink-stroke and
    reads as a hollow window punched in the wall (see unit 307, the loggia column). The
    distinguishing test vs. real furniture: a real furniture body's bbox sits INSIDE some
    unit's room or balcony polygon; a column's notch means every corner of its bbox is
    OUTSIDE every one of them (it lives in the wall fabric, not the room). Matches are pulled
    out of c06_furn (so they're never drawn white) and returned as plain bboxes for the caller
    to draw solid ink, fused into the wall, in the walls/columns z-layer."""
    c06f = next((c for c in fd["clusters"] if c["id"] == "c06_furn"), None)
    if not c06f or not c06f.get("paths"):
        return []
    polys = []
    for u in fd["units"].values():
        if u.get("poly"):
            polys.append(u["poly"])
        if u.get("balcony"):
            polys.append(u["balcony"])
    paths = c06f["paths"]
    seqs = c06f.get("seq")
    keep_idx = []
    columns = []
    for i, p in enumerate(paths):
        bb = _path_bbox(p)
        if bb is None:
            keep_idx.append(i)
            continue
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        if not (size_min <= bw <= size_max and size_min <= bh <= size_max):
            keep_idx.append(i)
            continue
        pts = _path_points(p)
        uniq = _unique_vertices(pts)
        bbox_area = bw * bh
        area = _polygon_area(pts) if len(pts) >= 3 else 0.0
        ratio = area / bbox_area if bbox_area > 0 else 0.0
        if not (ratio >= rect_ratio_min and len(uniq) in (4, 5)):
            keep_idx.append(i)
            continue
        corners = [(bb[0], bb[1]), (bb[2], bb[1]), (bb[0], bb[3]), (bb[2], bb[3])]
        if polys and any(any(_point_in_poly(c, poly) for poly in polys) for c in corners):
            keep_idx.append(i)  # a corner is inside a room/balcony - real furniture, keep it
            continue
        columns.append(bb)
    c06f["paths"] = [paths[i] for i in keep_idx]
    if seqs:
        c06f["seq"] = [seqs[i] for i in keep_idx]
    return columns


def filter_furniture_paths(fd):
    """Drop two kinds of clutter from c02/c05 (furniture/fixture line-art):
    (a) point-symbols (bbox under 2.5x2.5pt - sockets, "**" marks, dots);
    (b) decorative multi-segment icon glyphs (palm tree, fire extinguisher): empirically a
    tight cluster of >=5 small fragments packed into a ~3-14pt box - far denser than any real
    furniture stroke grouping (a small fixture tops out at 2-3 nearby segments)."""
    for c in fd["clusters"]:
        if c["id"] not in ("c02", "c05"):
            continue
        paths = c.get("paths") or []
        bboxes = [_path_bbox(p) for p in paths]
        keep = [i for i, bb in enumerate(bboxes) if bb and not ((bb[2] - bb[0]) < 2.5 and (bb[3] - bb[1]) < 2.5)]
        small = [i for i in keep if (bboxes[i][2] - bboxes[i][0]) <= 12 and (bboxes[i][3] - bboxes[i][1]) <= 12]
        groups = _union_find_groups([bboxes[i] for i in small], gap=1.0)
        drop = set()
        for members in groups.values():
            if len(members) < 5:
                continue
            idxs = [small[m] for m in members]
            mb = [bboxes[i] for i in idxs]
            gx0, gy0 = min(b[0] for b in mb), min(b[1] for b in mb)
            gx1, gy1 = max(b[2] for b in mb), max(b[3] for b in mb)
            if 3 <= (gx1 - gx0) <= 14 and 3 <= (gy1 - gy0) <= 14:
                drop.update(idxs)
        keep_set = set(keep) - drop
        c["paths"] = [p for i, p in enumerate(paths) if i in keep_set]
    return fd


def furniture_zorder_svg(floor_data, style, bounds=None, context="floor"):
    """Draw c06_furn (furniture bodies) + c02/c05/c01 (furniture/fixture line-art) as ONE pass
    sorted by each path's original PDF drawing order ("seq"), so later-drawn elements (a
    tabletop drawn after its chairs) correctly occlude earlier ones, matching the architect's
    intended z-order. Falls back to the old grouped draw order (bodies, then lines) for any
    floor whose data doesn't carry "seq" yet."""
    ids = ["c06_furn", "c02_table", "c02", "c05", "c01"]
    clusters_by_id = {c["id"]: c for c in floor_data["clusters"]}
    style_clusters = style["clusters"]
    has_seq = any(clusters_by_id.get(cid, {}).get("seq") for cid in ids)
    if not has_seq:
        return cluster_layers_svg(
            floor_data, style, bounds=bounds, context=context,
            only_roles=[("furniture_body",), ("furniture", "fixtures", "dashed")],
        )
    pad = 2.0
    items = []  # (seq, cid, path_d)
    for cid in ids:
        spec = style_clusters.get(cid)
        c = clusters_by_id.get(cid)
        if not spec or not c or not _cluster_visible(spec, context):
            continue
        paths = c.get("paths", [])
        seqs = c.get("seq") or list(range(len(paths)))
        for p, sq in zip(paths, seqs):
            if bounds is not None:
                pb = _path_bbox(p)
                if pb is None:
                    continue
                bx0, by0, bx1, by1 = bounds
                if not (pb[2] >= bx0 - pad and pb[0] <= bx1 + pad and pb[3] >= by0 - pad and pb[1] <= by1 + pad):
                    continue
            items.append((sq, cid, p))
    items.sort(key=lambda t: t[0])

    # Hard z-order rule for table bodies (seq alone proved unreliable): every table draws
    # AFTER anything that touches/comes within 6pt of it (chairs, chair backs) and BEFORE
    # anything that sits fully inside it (plates, a sink/stove drawn on the counter) -
    # regardless of what the original PDF seq says.
    NEAR_PT = 6.0

    def _is_table_shaped(cid, p):
        if cid == "c02_table":
            return True
        if cid != "c06_furn":
            return False
        bb = _path_bbox(p)
        if bb is None:
            return False
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        area = bw * bh
        if not (60.0 <= area <= 1400.0):
            return False
        return max(bw, bh) / max(min(bw, bh), 0.01) <= 3.5

    table_items = [t for t in items if _is_table_shaped(t[1], t[2])]
    other_items = [t for t in items if not _is_table_shaped(t[1], t[2])]
    if table_items:
        table_boxes = [_path_bbox(t[2]) for t in table_items]
        under, over, unrelated = [], [], []
        for t in other_items:
            pb = _path_bbox(t[2])
            if pb is None:
                unrelated.append(t)
                continue
            classified = False
            for tb in table_boxes:
                ex0, ey0, ex1, ey1 = tb[0] - NEAR_PT, tb[1] - NEAR_PT, tb[2] + NEAR_PT, tb[3] + NEAR_PT
                if pb[2] < ex0 or pb[0] > ex1 or pb[3] < ey0 or pb[1] > ey1:
                    continue  # not near this table
                fully_inside = tb[0] - 0.3 <= pb[0] and pb[2] <= tb[2] + 0.3 and tb[1] - 0.3 <= pb[1] and pb[3] <= tb[3] + 0.3
                (over if fully_inside else under).append(t)
                classified = True
                break
            if not classified:
                unrelated.append(t)
        items = sorted(unrelated + under, key=lambda t: t[0]) + table_items + sorted(over, key=lambda t: t[0])

    parts = []
    for _sq, cid, p in items:
        spec = style_clusters[cid]
        fill = spec.get("fill")
        stroke = spec.get("stroke")
        if fill and stroke:
            w = _cluster_width(spec, context)
            parts.append(f'<path d="{p}" fill="{fill}" stroke="{stroke}" stroke-width="{w}" stroke-linejoin="round"/>')
        elif fill:
            parts.append(f'<path d="{p}" fill="{fill}" stroke="none" fill-rule="nonzero"/>')
        elif stroke:
            w = _cluster_width(spec, context)
            cap = spec.get("linecap", "round")
            dash = spec.get("dasharray")
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            parts.append(
                f'<path d="{p}" fill="none" stroke="{stroke}" stroke-width="{w}" '
                f'stroke-linejoin="round" stroke-linecap="{cap}"{dash_attr}/>'
            )
    return "".join(parts)


def _frag_parse(p):
    """Parse a fragment's d-string into (vertices, segments-between-consecutive-vertices),
    where each segment is ('L',) or ('C', c1, c2)."""
    tokens = p.replace(",", " ").split()
    i = 0
    verts = []
    segs = []
    while i < len(tokens):
        tok = tokens[i]
        if tok == "M":
            verts.append((float(tokens[i + 1]), float(tokens[i + 2])))
            i += 3
        elif tok == "L":
            verts.append((float(tokens[i + 1]), float(tokens[i + 2])))
            segs.append(("L",))
            i += 3
        elif tok == "C":
            c1 = (float(tokens[i + 1]), float(tokens[i + 2]))
            c2 = (float(tokens[i + 3]), float(tokens[i + 4]))
            verts.append((float(tokens[i + 5]), float(tokens[i + 6])))
            segs.append(("C", c1, c2))
            i += 7
        else:
            i += 1
    return verts, segs


def _frag_reverse(verts, segs):
    rv = list(reversed(verts))
    rs = []
    for seg in reversed(segs):
        rs.append(("L",) if seg[0] == "L" else ("C", seg[2], seg[1]))
    return rv, rs


def _frag_emit(verts, segs):
    parts = [f"M {verts[0][0]:.2f} {verts[0][1]:.2f}"]
    for k, seg in enumerate(segs):
        if seg[0] == "L":
            parts.append(f"L {verts[k + 1][0]:.2f} {verts[k + 1][1]:.2f}")
        else:
            _, c1, c2 = seg
            parts.append(
                f"C {c1[0]:.2f} {c1[1]:.2f} {c2[0]:.2f} {c2[1]:.2f} {verts[k + 1][0]:.2f} {verts[k + 1][1]:.2f}"
            )
    return " ".join(parts)


def trace_closed_loop(frag_strs, tol=0.35):
    """Chain a set of disjoint path fragments end-to-end (reversing as needed) into one
    continuous closed path. Returns the combined `d` string (with a trailing Z), or None if
    the fragments don't form a single simple loop."""
    frags = [_frag_parse(p) for p in frag_strs]
    if any(len(v) < 2 for v, _ in frags):
        return None

    def dist(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    chain_verts, chain_segs = list(frags[0][0]), list(frags[0][1])
    start_pt = chain_verts[0]
    used = {0}
    while len(used) < len(frags):
        cur_end = chain_verts[-1]
        found = False
        for i in range(len(frags)):
            if i in used:
                continue
            v, s = frags[i]
            if dist(v[0], cur_end) < tol:
                chain_verts += v[1:]
                chain_segs += s
                used.add(i)
                found = True
                break
            if dist(v[-1], cur_end) < tol:
                rv, rs = _frag_reverse(v, s)
                chain_verts += rv[1:]
                chain_segs += rs
                used.add(i)
                found = True
                break
        if not found:
            return None
    if dist(chain_verts[-1], start_pt) >= tol:
        return None
    return _frag_emit(chain_verts[:-1], chain_segs[:-1]) + " Z"


def _is_closed_path(p, tol=0.6):
    pts = _path_points(p)
    if len(pts) < 3:
        return False
    return math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < tol


def _table_seq(bbox, all_c02_paths, all_c02_seqs, member_seqs, pad=3.0):
    """A table's fill must occlude ANY chair drawn nearby regardless of the table outline's
    own position in the original PDF sequence, so use the max seq among every c02 path whose
    bbox overlaps the table's (padded) bbox - not just the table's own fragments."""
    bx0, by0, bx1, by1 = bbox
    bx0 -= pad; by0 -= pad; bx1 += pad; by1 += pad
    best = max(member_seqs) if member_seqs else 0
    for p, sq in zip(all_c02_paths, all_c02_seqs):
        pb = _path_bbox(p)
        if pb is None:
            continue
        if pb[2] < bx0 or pb[0] > bx1 or pb[3] < by0 or pb[1] > by1:
            continue
        if sq > best:
            best = sq
    return best + 0.5


def detect_table_bodies(fd, area_min, area_max, ar_max, frag_min=3, frag_max=10, round_diam_min=None, round_diam_max=None):
    """Dining/side tables in c02 are drawn as an OUTLINE only (unlike computer desks, which
    already have a c06 body) - disjoint line/curve fragments that trace a compact
    rectangle/oval, with no fill. Chain-reconstruct any such closed, table-shaped outline into
    one proper polygon and promote it to a synthetic filled "c02_table" body (styled like
    c06_furn), removing its source fragments from plain c02. Also handles round tables drawn
    as a single already-closed circular/oval path."""
    src = next((c for c in fd["clusters"] if c["id"] == "c02"), None)
    if src is None or not src.get("paths"):
        return fd
    paths = list(src["paths"])
    seqs = list(src.get("seq") or list(range(len(paths))))
    all_paths_snapshot = list(paths)
    all_seqs_snapshot = list(seqs)
    n = len(paths)
    eps = [(_path_points(p)[0], _path_points(p)[-1]) for p in paths]
    tol = 0.35
    vmap = {}

    def vkey(pt):
        return (round(pt[0] / tol), round(pt[1] / tol))

    for i, (s, e) in enumerate(eps):
        vmap.setdefault(vkey(s), []).append(i)
        vmap.setdefault(vkey(e), []).append(i)

    def neighbours(pt):
        kx, ky = vkey(pt)
        out = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out.extend(vmap.get((kx + dx, ky + dy), []))
        return out

    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, (s, e) in enumerate(eps):
        for pt in (s, e):
            for j in neighbours(pt):
                if j != i:
                    union(i, j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    table_paths = []
    table_seqs = []
    consumed = set()
    for idxs in groups.values():
        if not (frag_min <= len(idxs) <= frag_max):
            continue
        bbs = [_path_bbox(paths[i]) for i in idxs]
        gx0, gy0 = min(b[0] for b in bbs), min(b[1] for b in bbs)
        gx1, gy1 = max(b[2] for b in bbs), max(b[3] for b in bbs)
        w, h = gx1 - gx0, gy1 - gy0
        area = w * h
        if not (area_min <= area <= area_max):
            continue
        ar = max(w, h) / max(min(w, h), 0.01)
        if ar > ar_max:
            continue
        loop = trace_closed_loop([paths[i] for i in idxs], tol=tol)
        if loop is None:
            continue
        table_paths.append(loop)
        table_seqs.append(_table_seq((gx0, gy0, gx1, gy1), all_paths_snapshot, all_seqs_snapshot, [seqs[i] for i in idxs]))
        consumed.update(idxs)

    if round_diam_min is not None:
        for i in range(n):
            if i in consumed:
                continue
            p = paths[i]
            if "C" not in p or not _is_closed_path(p):
                continue
            bb = _path_bbox(p)
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            if not (round_diam_min <= w <= round_diam_max and round_diam_min <= h <= round_diam_max):
                continue
            if max(w, h) / max(min(w, h), 0.01) > 1.3:
                continue
            table_paths.append(p)
            table_seqs.append(_table_seq(bb, all_paths_snapshot, all_seqs_snapshot, [seqs[i]]))
            consumed.add(i)

    if not table_paths:
        return fd
    keep = [i for i in range(n) if i not in consumed]
    src["paths"] = [paths[i] for i in keep]
    src["seq"] = [seqs[i] for i in keep] if src.get("seq") else src.get("seq")
    fd["clusters"].append({"id": "c02_table", "paths": table_paths, "seq": table_seqs})
    return fd


class Renderer:
    def __init__(self, page, style):
        self.page = page
        self.style = style

    async def rasterize(self, svg_body, view_x, view_y, view_w, view_h, out_w, out_h):
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{out_w}" height="{out_h}" '
            f'viewBox="{view_x:.2f} {view_y:.2f} {view_w:.2f} {view_h:.2f}">{svg_body}</svg>'
        )
        html = (
            "<!doctype html><html><head><meta charset=\"utf-8\">" + FONT_LINK +
            "<style>html,body{margin:0;padding:0;background:#fff}svg{display:block}</style>"
            "</head><body>" + svg + "</body></html>"
        )
        await self.page.set_viewport_size({"width": out_w, "height": out_h})
        await self.page.set_content(html, wait_until="load")
        try:
            await self.page.evaluate(
                "document.fonts.load('16px \"Instrument Serif\"').then(()=>document.fonts.ready)"
            )
        except Exception:
            pass
        await self.page.wait_for_timeout(120)
        png_bytes = await self.page.locator("svg").screenshot()
        return svg, png_bytes


def save_png(png_bytes, out_path, colors):
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    im = im.quantize(colors=colors, method=Image.MEDIANCUT)
    im.save(out_path, optimize=True)


def save_webp(png_bytes, out_path, colors, half=False):
    """Same quantized raster as save_png, encoded as WebP.

    Measured on floor-2 (4159x2337, flat-fill line-art after quantize): lossy q82
    barely beats the PNG (122KB vs 134KB), but *lossless* WebP drops to 74KB -
    lossless's flat-region prediction wins on this content where lossy's DCT
    blocking doesn't. So the full-res plan is saved lossless (method=6, slowest/
    smallest effort). The resize for the half-width thumbnail breaks the flat-color
    property (LANCZOS introduces antialiasing gradients), which makes lossless
    balloon back up (140KB, bigger than the full image) - so half=True instead
    resizes then saves lossy at q75 (~49KB), which is both smaller and the
    resolution loss already hides the artifacts."""
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    im = im.quantize(colors=colors, method=Image.MEDIANCUT).convert("RGB")
    if half:
        w, h = im.size
        im = im.resize((max(1, w // 2), max(1, h // 2)), Image.LANCZOS)
        im.save(out_path, "WEBP", quality=75, method=6)
    else:
        im.save(out_path, "WEBP", lossless=True, quality=100, method=6)


def save_webp_from_png(png_path, out_path, lossless=True, quality=82):
    """Convert an already-rasterized PNG (e.g. a pre-made unit plan export) straight
    to WebP, no re-quantization. Same lossless-wins-on-flat-art finding as save_webp."""
    im = Image.open(png_path).convert("RGB")
    if lossless:
        im.save(out_path, "WEBP", lossless=True, quality=100, method=6)
    else:
        im.save(out_path, "WEBP", quality=quality, method=6)


def prepare_floor(fd, style):
    """Runs every floor-wide, crop-independent synthesis pass ONCE per floor - wall bands,
    stair/lift/core symbols (+ the data-stripping that clears their raw PDF art), bed
    detection + etalon symbols + nightstands, and door synthesis (leaf + dashed arc for every
    entrance/balcony/inter-room gap, found via find_wall_gaps on both the unit polygon AND its
    balcony, plus find_c07_gaps at both bathroom- and room-scale) with the marker/wedge/
    corridor-sign sweeps that clean up c07/c08/c12/c14 before any of it gets drawn. Both
    render_floor (draws it floor-wide) and render_unit (draws the SAME wall_bands/door_svg/
    bed symbols, clipped to one unit's own footprint) call this once per floor and reuse the
    result, so a fix to how a door or a bed reads is automatically consistent between views."""
    warnings = fd.pop("_warnings", [])
    geo = style["geometry"]
    muted = style["muted_ink"]
    ink = style["ink"]
    w, h = fd["w"], fd["h"]

    fill_palette = style["floor_render"].get("units_fill_palette", ["#E4DACD", "#A9BBC8", "#D6E1EA", "#EDE3D3"])
    try:
        unit_colors, unit_adj = assign_unit_fill_colors(fd["units"], fill_palette, style["floor_render"].get("units_adjacency_tol_pt", 2.5))
    except Exception as e:
        warnings.append(f"assign_unit_fill_colors: {e} - falling back to a plain rotation (no adjacency check)")
        unit_colors = {num: fill_palette[i % len(fill_palette)] for i, num in enumerate(fd["units"].keys())}
        unit_adj = {}
    tints_used = len(set(unit_colors.values()))
    adjacency_conflicts = sum(1 for a, nbrs in unit_adj.items() for b in nbrs if unit_colors.get(a) == unit_colors.get(b)) // 2
    if adjacency_conflicts:
        warnings.append(f"assign_unit_fill_colors: {adjacency_conflicts} adjacent unit pair(s) share a colour")

    polys_all = []
    for u in fd["units"].values():
        if u.get("poly"):
            polys_all.append(u["poly"])
        if u.get("balcony"):
            polys_all.append(u["balcony"])
    hull_all = convex_hull([pt for poly in polys_all for pt in poly]) if polys_all else []

    # Stairs: replace the raw PDF scribble with one light symbol per flight (reference
    # reference: no arrows, no break mark) - detect runs of >=6 evenly-pitched tread marks in
    # c02/c05/c07/c06_furn, erase everything the source drew inside each flight's own
    # rectangle, draw a dashed muted-grey contour + n muted tread lines (stair_flight_svg),
    # then a U-shaped ink handrail - one shared rail on the dividing line for a twin pair
    # (pair_stair_flights), one down its own axis for a single flight.
    # Lifts: the brief points at specific white c06 furniture-body shapes inside the core
    # (~40-70pt square-ish) rather than any wall/column geometry - detect_lift_bodies looks
    # there directly; if nothing in that size band exists, lifts stay empty rather than guessed.
    core_bbox = None
    flights = []
    lift_bodies = []
    stair_wall_svg = []
    stair_door_svg = []
    lift_door_side = "right"
    stair_wall_pt = style["wall_bands"].get("corridor_pt", 2.4)
    try:
        core_bbox = compute_core_bbox(fd)
    except Exception as e:
        warnings.append(f"compute_core_bbox: {e} - core fill/sign-sweep skipped")
    try:
        flights = detect_stair_flights(fd)
        strip_stair_originals(fd, flights)
    except Exception as e:
        warnings.append(f"detect_stair_flights: {e} - no stair symbol drawn")
        flights = []
    try:
        lift_bodies = detect_lift_bodies(
            fd, core_bbox,
            geo.get("lift_body_w_min_pt", 35.0), geo.get("lift_body_w_max_pt", 75.0),
            geo.get("lift_body_h_min_pt", 25.0), geo.get("lift_body_h_max_pt", 75.0),
        )
        strip_lift_originals(fd, lift_bodies)
    except Exception as e:
        warnings.append(f"detect_lift_bodies: {e} - no lift symbol drawn")
        lift_bodies = []
    # Sweep whatever's left in the core (small stray rectangles, angled off-cuts, decorative
    # boxes) - stairs and lifts have already had their own footprints cleared above, so this
    # blanket pass only catches genuine clutter, never the symbols this code itself draws.
    try:
        strip_core_clutter(fd, core_bbox)
    except Exception as e:
        warnings.append(f"strip_core_clutter: {e}")

    # Stair + lift enclosures: v14's stair floated in bare white space with no walls, and the
    # lifts were just a thin contour + X. The reference draws the stair boxed in on 3 sides
    # with a door opening toward the lift lobby, and each lift as a proper walled shaft with a
    # sliding-door mark on that SAME side, so both openings face each other across one shared
    # landing - computed mutually via _nearest_side_to_point (stair -> lift centroid, lift ->
    # stair centroid), not hardcoded, so it stays correct if the geometry shifts. Every stage
    # here is independently fail-soft: one malformed stair group (or none at all, or no lift
    # nearby) never blocks the others.
    try:
        stair_groups = pair_stair_flights(flights)
    except Exception as e:
        warnings.append(f"pair_stair_flights: {e}")
        stair_groups = []
    lift_group_bbox = None
    try:
        lift_group_bbox = bbox_of([[(b[0], b[1]), (b[2], b[3])] for b in lift_bodies]) if lift_bodies else None
    except Exception as e:
        warnings.append(f"lift_group_bbox: {e}")
    stair_door_w = geo.get("stair_door_width_pt", 10.0)
    stair_expand = geo.get("stair_enclosure_expand_pt", 3.0)
    stair_group_bboxes = []
    for g in stair_groups:
        try:
            xs = [f["bbox"][0] for f in g] + [f["bbox"][2] for f in g]
            ys = [f["bbox"][1] for f in g] + [f["bbox"][3] for f in g]
            sbbox = (min(xs), min(ys), max(xs), max(ys))
            stair_group_bboxes.append(sbbox)
            target = (
                ((lift_group_bbox[0] + lift_group_bbox[2]) / 2, (lift_group_bbox[1] + lift_group_bbox[3]) / 2)
                if lift_group_bbox else None
            )
            door_side = _nearest_side_to_point(sbbox, target) if target else "left"
            skip = {s for s in ("top", "bottom", "left", "right") if s != door_side and _side_already_walled(fd, sbbox, s)}
            walls, (jamb1, jamb2, outward) = stair_enclosure_svg(
                sbbox, door_side, wall_width=stair_wall_pt, door_width=stair_door_w,
                expand=stair_expand, ink=ink, skip_sides=skip,
            )
            stair_wall_svg.extend(walls)
            leaf, arc = synth_door_swing(jamb1, jamb2, outward)
            stair_door_svg.append(
                f'<polyline points="{points_attr(leaf)}" fill="none" stroke="{ink}" stroke-width="{geo.get("door_leaf_width_pt", 0.5)}"/>'
                f'<polyline points="{points_attr(arc)}" fill="none" stroke="{muted}" stroke-width="{geo.get("door_arc_width_pt", 0.4)}" stroke-dasharray="{geo.get("door_arc_dasharray", "1.5 1.5")}"/>'
            )
        except Exception as e:
            warnings.append(f"stair_enclosure_svg: {e} - one stair group left unwalled")

    try:
        if lift_group_bbox and stair_group_bboxes:
            lcx, lcy = (lift_group_bbox[0] + lift_group_bbox[2]) / 2, (lift_group_bbox[1] + lift_group_bbox[3]) / 2
            best, best_d = None, 1e18
            for sbbox in stair_group_bboxes:
                scx, scy = (sbbox[0] + sbbox[2]) / 2, (sbbox[1] + sbbox[3]) / 2
                d = (scx - lcx) ** 2 + (scy - lcy) ** 2
                if d < best_d:
                    best_d, best = d, (scx, scy)
            lift_door_side = _nearest_side_to_point(lift_group_bbox, best)
    except Exception as e:
        warnings.append(f"lift_door_side: {e} - defaulted to 'right'")

    # Beds: every unit's bed (raw PDF art ranges from a plain chamfered box with no pillows to
    # something already close to the etalon) redrawn as ONE consistent symbol - detect double
    # and single mattress-sized bodies inside each unit, work out which short end is the
    # headboard (nearest wall, else nearest nightstand), strip the original art from each
    # bed's own footprint, and draw bed_symbol_svg for all of them so every bed on the floor
    # matches.
    beds = []
    try:
        beds = detect_beds(
            fd,
            double_short=(geo.get("bed_double_short_min_pt", 18.0), geo.get("bed_double_short_max_pt", 30.0)),
            double_long=(geo.get("bed_double_long_min_pt", 26.0), geo.get("bed_double_long_max_pt", 36.0)),
            single_short=(geo.get("bed_single_short_min_pt", 11.0), geo.get("bed_single_short_max_pt", 17.0)),
            single_long=(geo.get("bed_single_long_min_pt", 26.0), geo.get("bed_single_long_max_pt", 36.0)),
        )
    except Exception as e:
        warnings.append(f"detect_beds: {e} - no beds redrawn")
        beds = []
    bed_heads = []
    for bed in beds:
        try:
            bed_heads.append((bed, bed_headboard_side(bed, fd)))
        except Exception as e:
            warnings.append(f"bed_headboard_side (unit {bed.get('unit')}): {e} - that bed left as-is")
    try:
        strip_bed_originals(fd, [bed for bed, _ in bed_heads])
    except Exception as e:
        warnings.append(f"strip_bed_originals: {e}")

    # Nightstands: for every double bed, one on each side of the headboard end, if a free
    # 5x9pt slot against the same wall exists and isn't already occupied - or kept as-is if
    # the source already drew a nightstand-sized (5-9pt) body right there.
    nightstands_added = []
    nightstands_kept = []
    for bed, hside in bed_heads:
        try:
            if bed["kind"] != "double":
                continue
            unit_poly = fd["units"].get(bed["unit"], {}).get("poly")
            if not unit_poly:
                continue
            for cand in nightstand_candidate_rects(bed, hside, geo.get("nightstand_w_pt", 5.0), geo.get("nightstand_d_pt", 9.0)):
                corners = [(cand[0], cand[1]), (cand[2], cand[1]), (cand[0], cand[3]), (cand[2], cand[3])]
                if not all(_point_in_poly(c, unit_poly) for c in corners):
                    continue
                existing = find_existing_nightstand(fd, cand)
                if existing:
                    nightstands_kept.append((bed["unit"], existing))
                    continue
                if rect_occupied(fd, cand):
                    continue
                nightstands_added.append((bed["unit"], cand))
        except Exception as e:
            warnings.append(f"nightstand check (unit {bed.get('unit')}): {e}")

    # Core (stairs/lifts) fill data - the actual <rect>/mask markup is only meaningful at
    # floor scale (drawn by render_floor); render_unit never touches the core.

    # Doors: the architect's real leaf+arc symbols live in cluster c00 - mislabelled
    # "annotations" and left invisible by the previous pass, which then searched c01/c02/c05/
    # c11 (never c00), found nothing, and synthesised every door from wall-gap heuristics
    # instead - see extract_pdf_doors for how the real geometry is read (c00 also carries
    # dimension witness lines sharing the same stroke colour/width, filtered out because they
    # have no Bezier curve). This is now the ONLY door source: no gap-guessing, no
    # swing-side-guessing, and no doors that aren't backed by a real PDF arc.
    try:
        pdf_doors = extract_pdf_doors(fd)
    except Exception as e:
        warnings.append(f"extract_pdf_doors: {e}")
        pdf_doors = []

    # Walls: pure geometry from the (exact) unit polygons - exterior envelope, inter-unit
    # party walls, and corridor/core-facing edges each get their own band width, with a real
    # gap cut wherever a pdf_doors entry's hinge<->jamb2 span is collinear with the edge (see
    # _cut_edge_at_doors) - entrance and balcony openings no longer read as solid wall behind
    # the door swing drawn over them. No c03, no morphological anything. Shared verbatim with
    # render_unit so a unit crop's corridor-side wall reads exactly like the floor plan's.
    try:
        wall_bands = build_wall_bands(fd, style, pdf_doors)
    except Exception as e:
        warnings.append(f"build_wall_bands: {e}")
        wall_bands = []

    # Structural columns (extract_columns, run once in load_floor_data - pulled out of
    # c06_furn so they're never drawn white/hollow) are solid ink squares fused into the wall,
    # drawn in the same walls/columns z-layer as wall_bands.
    for bb in fd.get("_columns", []):
        x0, y0, x1, y1 = bb
        wall_bands.append(([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], "column"))

    door_leaf_w = geo.get("door_leaf_width_pt", 0.5)
    door_arc_w = geo.get("door_arc_width_pt", 0.4)
    door_arc_dash = geo.get("door_arc_dasharray", "1.5 1.5")
    door_svg = []
    gap_midpoints = []  # every real door's hinge/jamb2/midpoint, for chevron/wedge cleanup radii
    doors_synth = 0
    interior_doors_new = []  # (unit, midpoint) - kept for the per-unit report; unit unknown here, filled in below
    for d in pdf_doors:
        try:
            leaf, arc = synth_door_swing(d["hinge"], d["jamb2"], d["outward"])
            mid = ((d["hinge"][0] + d["jamb2"][0]) / 2, (d["hinge"][1] + d["jamb2"][1]) / 2)
            gap_midpoints.append(mid)
            gap_midpoints.append(d["hinge"])
            gap_midpoints.append(d["jamb2"])
            door_svg.append(
                f'<polyline points="{points_attr(leaf)}" fill="none" stroke="{ink}" stroke-width="{door_leaf_w}"/>'
                f'<polyline points="{points_attr(arc)}" fill="none" stroke="{muted}" stroke-width="{door_arc_w}" stroke-dasharray="{door_arc_dash}"/>'
            )
            doors_synth += 1
            for _unum, u in fd["units"].items():
                if _point_in_poly(mid, u["poly"]) or (u.get("balcony") and _point_in_poly(mid, u["balcony"])):
                    interior_doors_new.append((_unum, mid))
                    break
        except Exception as e:
            warnings.append(f"door synthesis: {e} - one door skipped")
    try:
        remove_door_chevrons(fd, gap_midpoints, geo["door_chevron_max_pt"], geo["door_chevron_radius_pt"])
    except Exception as e:
        warnings.append(f"remove_door_chevrons: {e}")
    try:
        # Leftover jamb-break marks (c06_furn, small rectangles) and any stray filled wedge
        # (c07/c08/c12) near a now-open gap - real doors only, so no separate wide-gap pass.
        remove_door_wedges(fd, gap_midpoints, geo.get("door_wedge_radius_pt", 14.0), geo.get("door_wedge_max_area_pt2", 12.0), cids=("c07", "c08", "c12", "c06_furn"))
    except Exception as e:
        warnings.append(f"remove_door_wedges: {e}")
    # Corridor + core-adjacent: bare floor + walls + doors only - strip small wayfinding marks
    # in ANY cluster (c08/c12/c14 are already pre-filtered to real wall/column fill by
    # filter_wall_fills, so this can safely include them too); the stair/lift core itself is
    # spared (filled solid instead, see the core_bbox rect drawn in render_floor).
    try:
        remove_corridor_signs(fd, geo.get("corridor_sign_max_bbox_pt", 14.0), core_bbox=core_bbox)
    except Exception as e:
        warnings.append(f"remove_corridor_signs: {e}")

    return {
        "warnings": warnings, "geo": geo, "muted": muted, "ink": ink,
        "polys_all": polys_all, "hull_all": hull_all,
        "core_bbox": core_bbox, "flights": flights, "lift_bodies": lift_bodies,
        "stair_wall_svg": stair_wall_svg, "stair_door_svg": stair_door_svg, "lift_door_side": lift_door_side,
        "stair_wall_pt": stair_wall_pt,
        "beds": beds, "bed_heads": bed_heads,
        "nightstands_added": nightstands_added, "nightstands_kept": nightstands_kept,
        "wall_bands": wall_bands, "door_svg": door_svg, "doors_synth": doors_synth,
        "interior_doors_new": interior_doors_new,
        "unit_colors": unit_colors, "unit_adj": unit_adj, "tints_used": tints_used,
        "adjacency_conflicts": adjacency_conflicts,
    }


async def render_floor(renderer, floor_num, floor_data, units_db, style, out_dir, fp=None):
    if fp is None:
        fp = prepare_floor(floor_data, style)
    warnings = fp["warnings"]
    w, h = floor_data["w"], floor_data["h"]
    px_per_pt = style["floor_render"]["px_per_pt"]
    out_w, out_h = round(w * px_per_pt), round(h * px_per_pt)

    fill_opacity = style["floor_render"]["units_fill_opacity"]
    fill_palette = style["floor_render"].get("units_fill_palette", ["#E4DACD", "#A9BBC8", "#D6E1EA", "#EDE3D3"])
    unit_colors = fp["unit_colors"]
    floor_fill = style["floor_fill"]
    ink = fp["ink"]
    muted = fp["muted"]
    geo = fp["geo"]
    font = style["labels"]["font"]
    fa_pt = style["labels"]["floor_area_pt"]
    mask_cids = style["floor_render"]["caption_mask_clusters"]
    min_w = style["floor_render"]["caption_min_w_pt"]
    min_h = style["floor_render"]["caption_min_h_pt"]

    core_bbox = fp["core_bbox"]
    flights = fp["flights"]
    lift_bodies = fp["lift_bodies"]
    stair_wall_svg = fp["stair_wall_svg"]
    stair_door_svg = fp["stair_door_svg"]
    lift_door_side = fp["lift_door_side"]
    stair_wall_pt = fp["stair_wall_pt"]
    beds = fp["beds"]
    bed_heads = fp["bed_heads"]
    polys_all = fp["polys_all"]
    hull_all = fp["hull_all"]
    wall_bands = fp["wall_bands"]
    door_svg = fp["door_svg"]

    # Draw order: floor fill -> per-unit colour overlay (under everything else so it never
    # mutes the linework) -> geometric wall bands (built from the exact unit polygons, not the
    # noisy c03 hatch texture - see build_wall_bands) -> c08/c12/c14 fills (columns/core) ->
    # partitions -> door swings -> furniture bodies + line-art in one PDF-drawing-order pass ->
    # labels (on top, always legible).
    body = f'<rect x="0" y="0" width="{w}" height="{h}" fill="{style["paper"]}"/>'
    body += cluster_layers_svg(floor_data, style, context="floor", only_roles=[("floor",)])

    # Core (stairs/lifts): a flat fill under the step lines, clipped to core_bbox minus every
    # unit/balcony polygon and minus anything outside the building's own exterior contour - a
    # plain rect here used to bleed into the balcony strip above 1009/1010. White per the
    # reference style (was the #E4E1DA grey).
    if core_bbox and hull_all:
        body += core_fill_svg(core_bbox, polys_all, hull_all, w, h, style["core_fill"])

    units_out = {}
    fills = []
    labels = []
    for number, u in floor_data["units"].items():
        try:
            poly = u["poly"]
            balcony = u.get("balcony")
            info = units_db.get(number)
            if balcony:
                fills.append(f'<polygon points="{points_attr(balcony)}" fill="{floor_fill}" fill-opacity="0.6" stroke="none"/>')
            color = unit_colors.get(number, fill_palette[0])
            fills.append(f'<polygon points="{points_attr(poly)}" fill="{color}" fill-opacity="{fill_opacity}" stroke="none"/>')
            total = info["total"] if info else None
            if total is not None:
                # Beds are drawn straight to `body` (see bed_symbol_svg below), not left in
                # floor_data, so the mask_cids rasteriser above can't see their footprint on its
                # own - hand it this unit's bed boxes directly, or the label can land on a pillow.
                bed_obstacles = [bed["bbox"] for bed in beds if bed["unit"] == number]
                rect = find_free_rect_in_poly(floor_data, style, poly, mask_cids, min_w, min_h, context="floor", extra_obstacles=bed_obstacles)
                if rect:
                    lx, ly = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
                else:
                    lx, ly = polygon_centroid(poly)
                labels.append(
                    f'<text x="{lx:.2f}" y="{ly + fa_pt * 0.35:.2f}" text-anchor="middle" font-family="{font}" '
                    f'font-weight="400" font-size="{fa_pt}" fill="{ink}">{fmt_area(total)}</text>'
                )
            units_out[number] = {
                "poly": [[round(x * px_per_pt, 1), round(y * px_per_pt, 1)] for x, y in poly],
                "balcony": [[round(x * px_per_pt, 1), round(y * px_per_pt, 1)] for x, y in balcony] if balcony else None,
                "label_living": info["living"] if info else None,
            }
        except Exception as e:
            warnings.append(f"unit fill/label (unit {number}): {e} - that unit skipped in fills/labels")

    body += "".join(fills)
    body += "".join(f'<polygon points="{points_attr(pts)}" fill="{ink}"/>' for pts, _cat in wall_bands)
    body += "".join(stair_wall_svg)

    # Walls/columns fill draw happens HERE - AFTER every wedge/glyph/sign removal in
    # prepare_floor, so the cleaned-up c08/c12/c14 data is what actually reaches the SVG
    # (drawing this earlier was a real bug across the last few rounds: the removals correctly
    # edited floor_data, but the walls/columns markup had already been appended before they ran).
    body += cluster_layers_svg(floor_data, style, context="floor", only_roles=[("walls", "columns")])
    body += cluster_layers_svg(floor_data, style, context="floor", only_roles=[("partitions",)])
    body += "".join(door_svg)
    body += "".join(stair_door_svg)
    body += furniture_zorder_svg(floor_data, style, context="floor")
    body += "".join(labels)
    stair_ink = style.get("stair_ink", style["muted_ink"])
    stairs_drawn = 0
    for f in flights:
        try:
            body += stair_flight_svg(f, stair_ink, geo.get("stair_tread_width_pt", 0.5),
                                      geo.get("stair_contour_width_pt", 0.5), geo.get("stair_contour_dasharray", ""))
            stairs_drawn += 1
        except Exception as e:
            warnings.append(f"stair_flight_svg: {e} - one flight not drawn")
    try:
        for g in pair_stair_flights(flights):
            body += handrail_svg(g, ink, geo.get("stair_handrail_gap_pt", 2.0), geo.get("stair_handrail_width_pt", 0.45))
    except Exception as e:
        warnings.append(f"handrail_svg: {e}")
    if lift_bodies:
        try:
            body += lift_group_svg(
                lift_bodies, lift_door_side, ink,
                shaft_wall=stair_wall_pt, cabin_inset=geo.get("lift_cabin_inset_pt", 1.5),
                cross_w=geo.get("lift_cross_width_pt", 0.4), door_gap=geo.get("lift_door_line_gap_pt", 1.5),
                door_stroke=geo.get("lift_door_line_width_pt", 0.5), door_frac=geo.get("lift_door_frac", 0.6),
            )
        except Exception as e:
            warnings.append(f"lift_group_svg: {e} - lifts not drawn")
    beds_drawn = 0
    for bed, side in bed_heads:
        try:
            body += bed_symbol_svg(bed, side, ink,
                                    geo.get("bed_pillow_h_frac", 0.22), geo.get("bed_pillow_w_frac", 0.42),
                                    geo.get("bed_pillow_corner_r_pt", 1.0), geo.get("bed_foot_cut_pt", 2.5))
            beds_drawn += 1
        except Exception as e:
            warnings.append(f"bed_symbol_svg (unit {bed.get('unit')}): {e} - that bed not drawn")

    # Nothing should render past the building's own exterior contour (+4pt slack for the wall
    # bands themselves) - a stray orientation-arrow glyph outside the top wall was found
    # sitting right in the margin. Wall bands (<=3.2pt) and balconies (already inside the hull)
    # both fit comfortably inside that 4pt slack, so one clip on the whole body is enough.
    if hull_all:
        try:
            clip_hull = offset_polygon(hull_all, 4.0)
            body = (
                f'<defs><clipPath id="building-clip"><polygon points="{points_attr(clip_hull)}"/></clipPath></defs>'
                f'<g clip-path="url(#building-clip)">{body}</g>'
            )
        except Exception as e:
            warnings.append(f"building-clip: {e} - floor rendered unclipped")

    svg, png_bytes = await renderer.rasterize(body, 0, 0, w, h, out_w, out_h)
    (out_dir / f"floor-{floor_num}.svg").write_text(svg, encoding="utf-8")
    save_png(png_bytes, out_dir / f"floor-{floor_num}.png", colors=style["floor_render"]["palette_colors"])
    return {
        "png": f"floor-{floor_num}.png", "w": out_w, "h": out_h, "units": units_out,
        "warnings": warnings,
        "stats": {
            "units": len(units_out), "tints": fp["tints_used"], "adjacency_conflicts": fp["adjacency_conflicts"],
            "lifts": len(lift_bodies), "stairs": stairs_drawn, "beds": beds_drawn,
        },
    }

async def render_unit(renderer, number, floor_data, info, style, out_dir, fp, walls_only=False):
    poly = floor_data["units"][number]["poly"]
    balcony = floor_data["units"][number].get("balcony")
    include_balcony = style["unit_crop"]["include_balcony"]
    crop_polys = [poly] + ([balcony] if (balcony and include_balcony) else [])
    x0, y0, x1, y1 = bbox_of(crop_polys)
    margin = style["unit_crop"]["margin_pt"]
    cx0, cy0, cx1, cy1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
    cw, ch = cx1 - cx0, cy1 - cy0
    px_per_pt = style["unit_crop"]["px_per_pt"]

    ink = fp["ink"]
    geo = fp["geo"]
    total = info["total"] if info else None

    # Round 18: the PNG/SVG is the plan ONLY - margin_pt on every side, no caption band at
    # all. The caption (title/area/room labels) is now data in plans.json (see "rooms" below)
    # for the site and the type-sheet builder to draw themselves, so it's never subject to a
    # per-image pixel size that a later fit-to-cell resize could distort.
    out_w, out_h = round(cw * px_per_pt), round(ch * px_per_pt)

    body = f'<rect x="0" y="0" width="{cw:.2f}" height="{ch:.2f}" fill="{style["unit_crop"]["bg"]}"/>'

    # Clip = poly (+ balcony) pushed outward so the unit's own perimeter walls / synthetic
    # wall bands stay visible while neighbouring units'/corridor content beyond that is cut.
    clip_offset = style["unit_crop"]["clip_offset_pt"]
    clip_shapes = [path_d(offset_polygon(poly, clip_offset))]
    if balcony and include_balcony:
        clip_shapes.append(path_d(offset_polygon(balcony, clip_offset)))

    clip_id = f"crop-{number}"
    plan = f'<defs><clipPath id="{clip_id}">' + "".join(f'<path d="{d}"/>' for d in clip_shapes) + "</clipPath></defs>"

    bounds = (cx0, cy0, cx1, cy1)
    pad = margin

    def _in_bounds(bb):
        return bb is not None and bb[2] >= cx0 - pad and bb[0] <= cx1 + pad and bb[3] >= cy0 - pad and bb[1] <= cy1 + pad

    # --walls-only (for the drag-and-drop editor export): no furniture at all, and the floor
    # fill is painted under the WHOLE unit polygon first (a plain polygon, not just wherever
    # the source's own per-room c06_floor paths happen to cover) - furniture in the original
    # PDF sometimes sat over a gap in that per-room fill, invisible normally but a real hole
    # once the furniture on top of it is removed.
    base_fill_svg = ""
    if walls_only:
        floor_fill_color = style["floor_fill"]
        base_fill_svg = f'<polygon points="{points_attr(poly)}" fill="{floor_fill_color}" stroke="none"/>'
        if balcony and include_balcony:
            base_fill_svg += f'<polygon points="{points_attr(balcony)}" fill="{floor_fill_color}" stroke="none"/>'
    floor_svg = cluster_layers_svg(floor_data, style, bounds=bounds, context="unit", only_roles=[("floor",)])

    # Balcony: floor painted white (not the room's warm floor tint) with a parapet/railing
    # line inset from its own outer edge - the solid 3.2pt exterior band on the true outer
    # edge already comes from the shared wall_bands (build_wall_bands treats an un-shared
    # balcony edge exactly like an exterior envelope edge), this just adds the railing that
    # distinguishes "open balcony" from "solid exterior wall" on the unit crop.
    balcony_svg = ""
    if balcony and include_balcony:
        balcony_svg = f'<polygon points="{points_attr(balcony)}" fill="white" stroke="none"/>'
        try:
            pts_dd = _dedupe(balcony)
            if len(pts_dd) >= 3:
                orig_area = abs(_signed_area(pts_dd))
                cand_a = _offset_once(pts_dd, 2.0, 1)
                cand_b = _offset_once(pts_dd, 2.0, -1)
                area_a = abs(_signed_area(cand_a)) if len(cand_a) >= 3 else 1e18
                area_b = abs(_signed_area(cand_b)) if len(cand_b) >= 3 else 1e18
                rail = cand_a if area_a < area_b else cand_b  # inward = the SHRINKING direction
                if rail and abs(_signed_area(rail)) < orig_area:
                    balcony_svg += f'<polygon points="{points_attr(rail)}" fill="none" stroke="{ink}" stroke-width="0.8"/>'
        except Exception:
            pass

    # Walls: the SAME geometry render_floor uses (see prepare_floor / build_wall_bands) - a
    # unit's corridor-facing edge gets its correct 2.4pt band here too, not the old per-unit
    # synth_wall_bands pass that only handled exterior (c08-backed) edges and could leave a
    # corridor-side wall missing entirely. Plus raw c08/c12/c14 (real columns/facade pieces)
    # and c07 (interior partitions, which aren't part of any unit-to-unit boundary).
    wall_bands_svg = "".join(
        f'<polygon points="{points_attr(pts)}" fill="{ink}"/>'
        for pts, _cat in fp["wall_bands"] if _in_bounds(bbox_of([pts]))
    )
    cols_svg = cluster_layers_svg(floor_data, style, bounds=bounds, context="unit", only_roles=[("walls", "columns")])
    partitions_svg = cluster_layers_svg(floor_data, style, bounds=bounds, context="unit", only_roles=[("partitions",)])

    # Doors: the same synthesized leaf+dashed-arc list render_floor draws (entrances, balcony
    # openings, and every interior gap found by find_wall_gap_doors) - every string is tiny
    # and in absolute floor coordinates, so rather than re-parse/filter them here, include
    # them all and let the SVG clip-path below do the real cropping to this unit's footprint.
    doors_svg = "".join(fp["door_svg"])

    if walls_only:
        furniture_svg = beds_svg = nightstands_svg = ""
    else:
        furniture_svg = furniture_zorder_svg(floor_data, style, bounds=bounds, context="unit")
        unit_beds = [(bed, side) for bed, side in fp["bed_heads"] if bed["unit"] == number]
        beds_svg = "".join(
            bed_symbol_svg(bed, side, ink, geo.get("bed_pillow_h_frac", 0.22), geo.get("bed_pillow_w_frac", 0.42),
                           geo.get("bed_pillow_corner_r_pt", 1.0), geo.get("bed_foot_cut_pt", 2.5))
            for bed, side in unit_beds
        )
        nightstands_svg = "".join(
            nightstand_svg(rect, ink) for u, rect in fp["nightstands_added"] if u == number
        )

    plan += (
        f'<g clip-path="url(#{clip_id})">' + base_fill_svg + floor_svg + balcony_svg + wall_bands_svg + cols_svg + partitions_svg
        + doors_svg + furniture_svg + beds_svg + nightstands_svg + "</g>"
    )

    # Room-area and balcony labels stay INSIDE the plan image (only the title band moved out).
    lab = style["labels"]
    font = lab["font"]; room_pt = lab["floor_area_pt"]; bal_pt = lab["balcony_pt"]
    try:
        for val, rx, ry, _sz in extract_room_area_labels(floor_data, poly, exclude_value=total):
            plan += (
                f'<text x="{rx:.2f}" y="{ry + room_pt * 0.8:.2f}" text-anchor="middle" font-family="{font}" '
                f'font-weight="400" font-size="{room_pt}" fill="{ink}">{fmt_area(val)}</text>'
            )
    except Exception:
        pass
    if balcony and info is not None and info.get("balcony") is not None:
        bx, by = polygon_centroid(balcony)
        plan += (
            f'<text x="{bx:.2f}" y="{by + bal_pt * 0.35:.2f}" text-anchor="middle" font-family="{font}" font-weight="400" '
            f'font-size="{bal_pt}" fill="{ink}">{fmt_area(info["balcony"])}</text>'
        )

    body += f'<g transform="translate({-cx0:.2f},{-cy0:.2f})">' + plan + "</g>"

    svg, png_bytes = await renderer.rasterize(body, 0, 0, cw, ch, out_w, out_h)
    (out_dir / f"unit-{number}.svg").write_text(svg, encoding="utf-8")
    save_png(png_bytes, out_dir / f"unit-{number}.png", colors=style["unit_crop"]["palette_colors"])

    poly_px = [[round((x - cx0) * px_per_pt, 1), round((y - cy0) * px_per_pt, 1)] for x, y in poly]
    balcony_px = (
        [[round((x - cx0) * px_per_pt, 1), round((y - cy0) * px_per_pt, 1)] for x, y in balcony]
        if balcony else None
    )

    # Caption data for consumers to draw themselves (the site, build-type-sheet.py) - never
    # baked into the PNG again, so no per-image pixel size can ever look inconsistent.
    ftype = info["type"] if info else "studio"
    rooms = []
    try:
        for val, rx, ry, _sz in extract_room_area_labels(floor_data, poly, exclude_value=total):
            rooms.append({
                "area": val,
                "x": round((rx - cx0) * px_per_pt, 1),
                "y": round((ry - cy0) * px_per_pt, 1),
            })
    except Exception:
        pass
    balcony_area = info.get("balcony") if info else None
    balcony_label = None
    if balcony and balcony_area is not None:
        bx, by = polygon_centroid(balcony)
        balcony_label = {"area": balcony_area, "x": round((bx - cx0) * px_per_pt, 1), "y": round((by - cy0) * px_per_pt, 1)}

    return {
        "png": f"unit-{number}.png", "w": out_w, "h": out_h, "poly": poly_px, "balcony": balcony_px,
        "number": number, "type_label": TYPE_TITLE.get(ftype, ""), "total": total, "floor": info["floor"] if info else None,
        "balcony_label": balcony_label, "rooms": rooms,
        "crop": {"x0": cx0, "y0": cy0, "w": cw, "h": ch, "px_per_pt": px_per_pt},
    }
def _bed_export_box(bbox, headboard_side):
    """Canonical (rot=0) x,y,w,h + rot for a bed, per the editor's convention: rot=0 means
    headboard on the TOP edge, 90 = RIGHT, 180 = BOTTOM, 270 = LEFT; w = short side (along the
    headboard), h = long side (length); x,y is the canonical box's own top-left - rotating
    (x,y,w,h) by `rot` degrees about its centre recovers the actual on-plan bbox."""
    bx0, by0, bx1, by1 = bbox
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    bw, bh = bx1 - bx0, by1 - by0
    rot = {"top": 0, "right": 90, "bottom": 180, "left": 270}.get(headboard_side, 0)
    w, h = (bw, bh) if rot in (0, 180) else (bh, bw)
    return cx - w / 2, cy - h / 2, w, h, rot


def _angle_to_rot(dx, dy):
    """Degrees clockwise from "up" (0 = target directly above, 90 = to the right, ...) - the
    same rot convention as beds, used to point a chair's rot at the table it belongs to."""
    return math.degrees(math.atan2(dx, -dy)) % 360.0


def _classify_raw_group(bw, bh):
    """Cheap bbox-only classification for a "raw" furniture group - orientation-tolerant
    (checked both ways round) since nothing here tracks the fixture's own facing. Falls back
    to "raw" (the group still carries its original paths either way, see build_unit_objects)."""
    for kind, wr, hr in (
        ("bathtub", (16, 20), (7, 9)),
        ("toilet", (5, 8), (8, 11)),
        ("sink", (5, 9), (4, 7)),
        ("kitchen_row", (25, 1e9), (5, 7)),
        ("sofa", (18, 30), (7, 11)),
        ("wardrobe", (12, 1e9), (5, 7)),
    ):
        if (wr[0] <= bw <= wr[1] and hr[0] <= bh <= hr[1]) or (wr[0] <= bh <= wr[1] and hr[0] <= bw <= hr[1]):
            return kind
    return "raw"


def _bbox_gap(a, b):
    gx = max(a[0], b[0]) - min(a[2], b[2])
    gy = max(a[1], b[1]) - min(a[3], b[3])
    return max(gx, gy, 0.0)


def build_unit_objects(fd, fp, style, number):
    """Editable furniture objects for the drag-and-drop editor, absolute plan-pt coordinates
    (the editor subtracts crop.x0/y0 itself). By the time this runs, prepare_floor has already
    stripped beds'/stairs'/turning-circles'/chevrons' raw art from floor_data, so every
    remaining c06_furn/c02/c05/c02_table primitive inside the unit polygon is genuinely
    unclaimed furniture - it ends up in EXACTLY one object: bed / nightstand / table / chair,
    or "raw" (a proximity group carrying its own original path strokes, so the editor can
    always draw the real thing even with no symbol for it - nothing is ever dropped)."""
    poly = fd["units"][number]["poly"]
    objects = []
    claimed = set()  # (cid, index) already spoken for
    seq_n = {"b": 0, "n": 0, "t": 0, "c": 0, "r": 0}

    def cluster(cid):
        return next((c for c in fd["clusters"] if c["id"] == cid), None)

    def next_id(prefix):
        seq_n[prefix] += 1
        return f"{prefix}{seq_n[prefix]}"

    # ---- beds ----
    for bed, side in fp["bed_heads"]:
        if bed["unit"] != number:
            continue
        x, y, w, h, rot = _bed_export_box(bed["bbox"], side)
        objects.append({"id": next_id("b"), "kind": f"bed_{bed['kind']}", "x": round(x, 2), "y": round(y, 2),
                         "w": round(w, 2), "h": round(h, 2), "rot": rot, "src": "detected"})

    # ---- nightstands (added = synthesised, no source path; kept = an existing path, which
    # must be marked claimed or it would ALSO turn up as a duplicate "raw" group) ----
    for u, rect in fp["nightstands_added"]:
        if u != number:
            continue
        x0, y0, x1, y1 = rect
        objects.append({"id": next_id("n"), "kind": "nightstand", "x": round(x0, 2), "y": round(y0, 2),
                         "w": round(x1 - x0, 2), "h": round(y1 - y0, 2), "rot": 0, "src": "detected"})
    kept_bboxes = [bb for u, bb in fp["nightstands_kept"] if u == number]
    for bb in kept_bboxes:
        x0, y0, x1, y1 = bb
        objects.append({"id": next_id("n"), "kind": "nightstand", "x": round(x0, 2), "y": round(y0, 2),
                         "w": round(x1 - x0, 2), "h": round(y1 - y0, 2), "rot": 0, "src": "detected"})
        for cid in ("c06_furn", "c02"):
            c = cluster(cid)
            if not c:
                continue
            found = False
            for i, p in enumerate(c.get("paths") or []):
                if (cid, i) in claimed:
                    continue
                pb = _path_bbox(p)
                if pb and all(abs(pb[k] - bb[k]) <= 0.3 for k in range(4)):
                    claimed.add((cid, i))
                    found = True
                    break
            if found:
                break

    # ---- tables ----
    table_boxes = []
    c02t = cluster("c02_table")
    if c02t:
        for i, p in enumerate(c02t.get("paths") or []):
            bb = _path_bbox(p)
            if bb is None:
                continue
            cxp, cyp = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            if not _point_in_poly((cxp, cyp), poly):
                continue
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            ar = max(bw, bh) / max(min(bw, bh), 0.01)
            is_round = ("C" in p) and ar <= 1.3
            objects.append({"id": next_id("t"), "kind": "table_round" if is_round else "table_rect",
                             "x": round(bb[0], 2), "y": round(bb[1], 2), "w": round(bw, 2), "h": round(bh, 2),
                             "rot": 0, "src": "detected"})
            claimed.add(("c02_table", i))
            table_boxes.append(bb)

    # ---- chairs: remaining c02 paths within 6pt of a table, grouped by proximity ----
    c02 = cluster("c02")
    chair_candidates = []
    if c02 and table_boxes:
        for i, p in enumerate(c02.get("paths") or []):
            if ("c02", i) in claimed:
                continue
            bb = _path_bbox(p)
            if bb is None:
                continue
            cxp, cyp = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            if not _point_in_poly((cxp, cyp), poly):
                continue
            if any(_bbox_gap(bb, tb) <= 6.0 for tb in table_boxes):
                chair_candidates.append((i, bb))
    if chair_candidates:
        n = len(chair_candidates)
        parent = list(range(n))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(n):
            for j in range(i + 1, n):
                if _bbox_gap(chair_candidates[i][1], chair_candidates[j][1]) <= 2.0:
                    union(i, j)
        groups = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)
        for members in groups.values():
            idxs = [chair_candidates[m][0] for m in members]
            bbs = [chair_candidates[m][1] for m in members]
            gx0, gy0 = min(b[0] for b in bbs), min(b[1] for b in bbs)
            gx1, gy1 = max(b[2] for b in bbs), max(b[3] for b in bbs)
            ccx, ccy = (gx0 + gx1) / 2, (gy0 + gy1) / 2
            nt = min(table_boxes, key=lambda tb: math.hypot((tb[0] + tb[2]) / 2 - ccx, (tb[1] + tb[3]) / 2 - ccy))
            tcx, tcy = (nt[0] + nt[2]) / 2, (nt[1] + nt[3]) / 2
            rot = _angle_to_rot(tcx - ccx, tcy - ccy)
            objects.append({"id": next_id("c"), "kind": "chair", "x": round(gx0, 2), "y": round(gy0, 2),
                             "w": round(max(gx1 - gx0, 1.0), 2), "h": round(max(gy1 - gy0, 1.0), 2),
                             "rot": round(rot, 1), "src": "detected"})
            for idx in idxs:
                claimed.add(("c02", idx))

    # ---- raw: everything else in c06_furn / c02 / c05 inside the unit polygon ----
    raw_candidates = []
    for cid in ("c06_furn", "c02", "c05"):
        c = cluster(cid)
        if not c:
            continue
        for i, p in enumerate(c.get("paths") or []):
            if (cid, i) in claimed:
                continue
            bb = _path_bbox(p)
            if bb is None:
                continue
            cxp, cyp = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            if not _point_in_poly((cxp, cyp), poly):
                continue
            raw_candidates.append((cid, i, bb, p))
    n = len(raw_candidates)
    parent = list(range(n))

    def find2(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union2(a, b):
        ra, rb = find2(a), find2(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if _bbox_gap(raw_candidates[i][2], raw_candidates[j][2]) <= 1.5:
                union2(i, j)
    groups2 = {}
    for i in range(n):
        groups2.setdefault(find2(i), []).append(i)
    style_clusters = style["clusters"]
    for members in groups2.values():
        entries = [raw_candidates[m] for m in members]
        bbs = [e[2] for e in entries]
        gx0, gy0 = min(b[0] for b in bbs), min(b[1] for b in bbs)
        gx1, gy1 = max(b[2] for b in bbs), max(b[3] for b in bbs)
        paths_out = []
        for cid, idx, bb, p in entries:
            spec = style_clusters.get(cid, {})
            fill = spec.get("fill")
            width = _cluster_width(spec, "unit") if spec.get("stroke") else 0.5
            paths_out.append({"d": p, "fill": fill, "width": width})
            claimed.add((cid, idx))
        kind = _classify_raw_group(gx1 - gx0, gy1 - gy0)
        objects.append({"id": next_id("r"), "kind": kind, "x": round(gx0, 2), "y": round(gy0, 2),
                         "w": round(gx1 - gx0, 2), "h": round(gy1 - gy0, 2), "rot": 0, "paths": paths_out})

    # ---- sanity: every c06_furn/c02/c05/c02_table primitive inside the polygon claimed once ----
    total_inside = 0
    for cid in ("c06_furn", "c02", "c05", "c02_table"):
        c = cluster(cid)
        if not c:
            continue
        for i, p in enumerate(c.get("paths") or []):
            bb = _path_bbox(p)
            if bb is None:
                continue
            cxp, cyp = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            if _point_in_poly((cxp, cyp), poly):
                total_inside += 1
    by_kind = {}
    for o in objects:
        by_kind[o["kind"]] = by_kind.get(o["kind"], 0) + 1
    stats = {"paths_before": total_inside, "paths_claimed": len(claimed), "unassigned": total_inside - len(claimed), "by_kind": by_kind}
    return objects, stats


def write_unit_export(fd, fp, style, number, info, render_result, out_dir, export_stats):
    """Writes plan-studio/editor-data/unit-N.json (used with --walls-only --export-objects)
    and folds this unit's sanity stats into `export_stats` for the batch-level report."""
    objects, stats = build_unit_objects(fd, fp, style, number)
    export_stats[number] = stats
    poly = fd["units"][number]["poly"]
    balcony = fd["units"][number].get("balcony")
    doc = {
        "unit": number,
        "floor": info["floor"] if info else render_result.get("floor"),
        "type_label": render_result.get("type_label"),
        "total": render_result.get("total"),
        "balcony": info.get("balcony") if info else None,
        "crop": render_result.get("crop"),
        "poly": poly,
        "balcony_poly": balcony,
        "objects": objects,
    }
    (out_dir / f"unit-{number}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tag = " - UNASSIGNED PATHS!" if stats["unassigned"] else ""
    print(f"  {number}: {len(objects)} objects {stats['by_kind']}{tag}")


def write_editor_index(out_dir, extra_units=()):
    """plan-studio/editor-data/index.json: one row per unit-shape-group representative (in
    plan-studio/unit-shape-groups.json order), plus any extra standalone units (e.g. 1005)
    appended at the end with group: null."""
    groups = load_json(ROOT / "plan-studio" / "unit-shape-groups.json")
    units_db, _ = index_units_db()
    rows = []
    for gi, g in enumerate(groups, start=1):
        rep = g[0]
        info = units_db.get(rep)
        if not info:
            continue
        floors = sorted({int(n[:-2]) for n in g})
        fl_str = str(floors[0]) if len(floors) == 1 else f"{floors[0]}\u2013{floors[-1]}"
        rows.append({
            "unit": rep, "group": gi, "type_label": TYPE_TITLE.get(info.get("type"), ""),
            "total": info.get("total"), "count": len(g), "floors": fl_str,
        })
    for number in extra_units:
        info = units_db.get(number)
        if not info:
            continue
        rows.append({
            "unit": number, "group": None, "type_label": TYPE_TITLE.get(info.get("type"), ""),
            "total": info.get("total"), "count": 1, "floors": str(info["floor"]),
        })
    (out_dir / "index.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_dir / 'index.json'} ({len(rows)} rows)")


def index_units_db():
    raw = load_json(UNITS_JSON_PATH)
    by_number = {}
    by_floor = {}
    for u in raw["units"]:
        by_number[u["number"]] = u
        by_floor.setdefault(u["floor"], []).append(u["number"])
    return by_number, by_floor


def _path_in_bounds(d, w, h, tol):
    nums = [float(n) for n in _NUM_RE.findall(d)]
    if not nums:
        return True
    xs = nums[0::2]
    ys = nums[1::2]
    return not (min(xs) < -tol or max(xs) > w + tol or min(ys) < -tol or max(ys) > h + tol)


def filter_floor_data(fd, tol=10):
    """Drop stray paths whose geometry falls (well) outside this floor's own bbox.
    Source clusters are grouped by original stroke/fill colour, not semantics, so a
    handful of leader/dimension-line paths sometimes share a colour with wall
    outlines / partitions; those render as noise shooting off the building footprint.
    A tiny, uncontroversial minority of paths per cluster are affected (verified on
    floor 10: 10/2035 in c03, 2/495 in c07, 1/1010 in c06, 0 elsewhere)."""
    w, h = fd["w"], fd["h"]
    for c in fd["clusters"]:
        paths = c.get("paths") or []
        c["paths"] = [d for d in paths if _path_in_bounds(d, w, h, tol)]
    return fd


def load_floor_data(floor_num):
    """Loads + runs every per-floor heuristic. Each stage is independently fail-soft: a
    detector that throws (or just finds nothing) on an atypical floor - ground floor retail,
    a mechanical/roof level, a top floor with a different core - logs a warning into
    fd["_warnings"] and leaves the data as the previous stage left it, rather than aborting
    the whole floor. render_floor pops "_warnings" back out and merges it into its own list."""
    warnings = []
    style = load_json(STYLE_PATH)
    fd = load_json(DATA_DIR / f"floor-{floor_num}.json")
    fd = filter_floor_data(fd)
    fd = split_c06(fd)
    try:
        fd = filter_furniture_paths(fd)
    except Exception as e:
        warnings.append(f"filter_furniture_paths: {e}")
    geo = style.get("geometry", {})
    # c08/c12/c14 are a colour-clustered grab-bag of real wall/column fill plus every small
    # decorative glyph (arrows, exit signs, door-leaf wedges) that shared that fill colour in
    # the PDF - keep only what actually looks like a wall/column (axis rectangle) or a true
    # facade-corner piece; this supersedes the older per-glyph removal heuristics.
    try:
        fd = filter_wall_fills(fd, style, geo.get("wall_fill_rect_ratio_min", 0.85), geo.get("wall_fill_facade_margin_pt", 6.0))
    except Exception as e:
        warnings.append(f"filter_wall_fills: {e}")
    try:
        fd = detect_table_bodies(
            fd,
            geo.get("c02_table_area_min_pt2", 60),
            geo.get("c02_table_area_max_pt2", 1400),
            geo.get("c02_table_aspect_max", 3.5),
            frag_min=geo.get("c02_table_frag_min", 2),
            frag_max=geo.get("c02_table_frag_max", 14),
            round_diam_min=geo.get("c02_round_table_diam_min"),
            round_diam_max=geo.get("c02_round_table_diam_max"),
        )
    except Exception as e:
        warnings.append(f"detect_table_bodies: {e}")
    try:
        fd = remove_turning_circles(
            fd,
            geo.get("turning_circle_diam_min", 17.0),
            geo.get("turning_circle_diam_max", 27.0),
            geo.get("turning_circle_min_frags", 12),
            merge_gap=2.0,
            ring_cids=("c02", "c05", "c06_furn", "c07"),
            content_cids=("c02", "c05", "c06_furn"),
        )
    except Exception as e:
        warnings.append(f"remove_turning_circles: {e}")
    try:
        fd = remove_hatch_ticks(fd)
    except Exception as e:
        warnings.append(f"remove_hatch_ticks: {e}")
    try:
        fd["_columns"] = extract_columns(fd)
    except Exception as e:
        warnings.append(f"extract_columns: {e}")
        fd["_columns"] = []
    fd["_warnings"] = warnings
    return fd


async def run(args):
    style = load_json(STYLE_PATH)
    units_db, units_by_floor = index_units_db()
    index = load_json(DATA_DIR / "index.json")
    all_floors = index["floors"]
    out_dir = Path(args.out).resolve() if getattr(args, "out", None) else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    export_stats = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        renderer = Renderer(page, style)

        floors_out = {}
        units_out = {}

        if args.floor is not None:
            floors_to_do = [args.floor]
        elif args.all:
            floors_to_do = all_floors
        else:
            floors_to_do = []

        floor_reports = []
        for fn in floors_to_do:
            report = {"floor": fn, "units": 0, "tints": 0, "adjacency_conflicts": 0, "lifts": 0, "stairs": 0, "beds": 0, "warnings": []}
            try:
                fd = load_floor_data(fn)
            except Exception as e:
                msg = f"load_floor_data failed: {e}"
                report["warnings"].append(msg)
                floor_reports.append(report)
                print(f"floor {fn}: LOAD FAILED - {msg} - floor skipped, batch continues")
                continue
            print(f"floor {fn}: rendering plan...")
            try:
                fp = prepare_floor(fd, style)
            except Exception as e:
                msg = f"prepare_floor failed: {e}"
                report["warnings"].append(msg)
                floor_reports.append(report)
                print(f"floor {fn}: PREPARE FAILED - {msg} - floor skipped, batch continues")
                continue
            try:
                result = await render_floor(renderer, fn, fd, units_db, style, out_dir, fp=fp)
            except Exception as e:
                msg = f"render_floor failed: {e}"
                report["warnings"].append(msg)
                floor_reports.append(report)
                print(f"floor {fn}: RENDER FAILED - {msg} - floor skipped, batch continues")
                continue
            stats = result.pop("stats", {})
            fwarnings = result.pop("warnings", [])
            report.update(stats)
            report["warnings"] = fwarnings
            floors_out[str(fn)] = result
            if not args.no_units:
                unit_fail = 0
                for number in units_by_floor.get(fn, []):
                    info = units_db.get(number)
                    try:
                        units_out[number] = await render_unit(renderer, number, fd, info, style, out_dir, fp, walls_only=args.walls_only)
                        if args.export_objects:
                            write_unit_export(fd, fp, style, number, info, units_out[number], out_dir, export_stats)
                    except Exception as e:
                        unit_fail += 1
                        report["warnings"].append(f"unit {number} render failed: {e}")
                if unit_fail:
                    print(f"  floor {fn}: {unit_fail} unit(s) failed to render, rest continued")
            floor_reports.append(report)

        if args.units:
            wanted = [u.strip() for u in args.units.split(",") if u.strip()]
            floor_cache = {}
            fp_cache = {}
            for number in wanted:
                info = units_db.get(number)
                if not info:
                    print(f"  unit {number}: not found in units.json, skip")
                    continue
                fn = info["floor"]
                try:
                    if fn not in floor_cache:
                        floor_cache[fn] = load_floor_data(fn)
                        fp_cache[fn] = prepare_floor(floor_cache[fn], style)
                    print(f"unit {number} (floor {fn})...")
                    units_out[number] = await render_unit(renderer, number, floor_cache[fn], info, style, out_dir, fp_cache[fn], walls_only=args.walls_only)
                    if args.export_objects:
                        write_unit_export(floor_cache[fn], fp_cache[fn], style, number, info, units_out[number], out_dir, export_stats)
                except Exception as e:
                    print(f"  unit {number}: FAILED - {e}")

        await browser.close()

    if args.all:
        old = load_json(PLANS_JSON_PATH) if PLANS_JSON_PATH.exists() else {}
        plans = {
            "floors": floors_out,
            "units": units_out,
            "missing": old.get("missing", []),
            "notes": old.get("notes", []),
            "ambiguous": old.get("ambiguous", []),
        }
        PLANS_JSON_PATH.write_text(json.dumps(plans, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        PLANS_JS_PATH.write_text(
            "window.PLANS_DATA = " + json.dumps(plans, ensure_ascii=False, separators=(",", ":")) + ";",
            encoding="utf-8",
        )
        print(f"wrote {PLANS_JSON_PATH} and {PLANS_JS_PATH}")

    if floor_reports:
        report_path = ROOT / "plan-studio" / "v2" / "batch-report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(floor_reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {report_path}")
        for r in floor_reports:
            if r["warnings"]:
                print(f"  floor {r['floor']}: {len(r['warnings'])} warning(s) - " + " | ".join(r["warnings"][:3]))

    if args.export_objects:
        write_editor_index(out_dir, extra_units=[u for u in (args.units.split(",") if args.units else []) if u.strip() not in {g[0] for g in load_json(ROOT / "plan-studio" / "unit-shape-groups.json")}])
        total_unassigned = sum(st["unassigned"] for st in export_stats.values())
        by_kind_total = {}
        for st in export_stats.values():
            for k, v in st["by_kind"].items():
                by_kind_total[k] = by_kind_total.get(k, 0) + v
        print(f"export-objects: {len(export_stats)} units, unassigned_paths={total_unassigned}, by_kind={by_kind_total}")

    print(f"done: {len(floors_out)} floors, {len(units_out)} units")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=int, default=None, help="render a single floor plan + its units (test mode)")
    ap.add_argument("--units", type=str, default=None, help="comma-separated unit numbers (test mode)")
    ap.add_argument("--all", action="store_true", help="render every floor + every unit, rewrite plans.json/plans.js")
    ap.add_argument("--no-units", action="store_true", help="with --floor, render only the floor plan, skip its units")
    ap.add_argument("--out", type=str, default=None, help="override the output directory (default: site/assets/plans)")
    ap.add_argument("--walls-only", action="store_true", help="no furniture/beds/nightstands; full-polygon floor fill; for the editor's raw plan export")
    ap.add_argument("--export-objects", action="store_true", help="also write unit-N.json (editable furniture objects) + index.json into the output dir")
    args = ap.parse_args()
    if args.floor is None and not args.units and not args.all:
        print(__doc__)
        sys.exit(1)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
