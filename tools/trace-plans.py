#!/usr/bin/env python3
"""
trace-plans.py — traces apartment plans straight from the architect's PDF.

Principle: walls, columns, doors+swings, furniture, fixtures and balconies are
ALREADY DRAWN in the PDF. We only re-draw exactly what is there, in our own line
style. No synthesis, no detectors, no guessing. See task spec for full detail.

Usage: .venv/bin/python tools/trace-plans.py [unit ...]   (default: the 22 target units)

Outputs (per unit):
  plan-studio/editor-data/unit-N.svg / .png   - walls layer (floor, balcony, walls,
                                                 columns, windows, doors, labels)
  plan-studio/editor-data/unit-N.json         - metadata + poly + raw furniture objects
  site/assets/plans/unit-N.svg / .png - full furnished plan
"""
import json, re, sys, math
from pathlib import Path

import pymupdf
from PIL import Image

import os
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig
ROOT = Path(os.environ.get("TRACE_ROOT") or fpconfig.WORK)
PDF = Path(os.environ.get("TRACE_PDF") or fpconfig.PDF)
NO_PNG = os.environ.get("TRACE_NO_PNG") == "1"   # the server only needs SVG/JSON
DATA_DIR = ROOT / "plan-studio/data"
STYLE = json.loads(fpconfig.STYLE.read_text())
UNITS_META = {u["number"]: u for u in json.loads(
    fpconfig.SCHEDULE.read_text())["units"]}

import hashlib
OVERRIDES_PATH = ROOT / "plan-studio/cad-overrides.json"
try:
    OVERRIDES = json.loads(OVERRIDES_PATH.read_text()) if OVERRIDES_PATH.exists() else {}
except Exception:
    OVERRIDES = {}
OV_PRIMS = OVERRIDES.get("prims", {})        # id -> role | "drop"
OV_SIGS = OVERRIDES.get("signatures", {})    # "layer|type|stroke|fill|width" -> role | "drop"
OV_GEOM = OVERRIDES.get("geom", {})          # id -> [[[x,y],...], ...] subpaths, PLAN coords (client-edited vertices)
OV_POLYS = OVERRIDES.get("polys", {})        # unit -> [[x,y],...] room contour, PLAN coords


def reload_overrides():
    global OVERRIDES, OV_PRIMS, OV_SIGS
    try:
        OVERRIDES = json.loads(OVERRIDES_PATH.read_text()) if OVERRIDES_PATH.exists() else {}
    except Exception:
        OVERRIDES = {}
    OV_PRIMS = OVERRIDES.get("prims", {}); OV_SIGS = OVERRIDES.get("signatures", {})
    global OV_GEOM, OV_POLYS
    OV_GEOM = OVERRIDES.get("geom", {}); OV_POLYS = OVERRIDES.get("polys", {})


def _valid_pts(v, min_pts):
    return isinstance(v, list) and len(v) >= min_pts and all(isinstance(q, list) and len(q) == 2 and all(isinstance(c, (int, float)) for c in q) for q in v)


def merge_overrides(incoming):
    """Merge client rules into the server file (client wins per key); returns the merged dict."""
    reload_overrides()
    merged = {"prims": dict(OVERRIDES.get("prims", {})), "signatures": dict(OVERRIDES.get("signatures", {})),
              "geom": dict(OVERRIDES.get("geom", {})), "polys": dict(OVERRIDES.get("polys", {}))}
    for k in ("prims", "signatures"):
        for key, val in (incoming.get(k) or {}).items():
            if val is None:
                merged[k].pop(key, None)
            elif val in USER_ROLES:
                merged[k][key] = val
    for key, val in (incoming.get("geom") or {}).items():
        if val is None:
            merged["geom"].pop(key, None)
        elif isinstance(val, list) and val and all(_valid_pts(sp, 2) for sp in val):
            merged["geom"][key] = [[[round(float(x), 2), round(float(y), 2)] for x, y in sp] for sp in val]
    for key, val in (incoming.get("polys") or {}).items():
        if val is None:
            merged["polys"].pop(key, None)
        elif _valid_pts(val, 3):
            merged["polys"][key] = [[round(float(x), 2), round(float(y), 2)] for x, y in val]
    OVERRIDES_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=1))
    reload_overrides()
    return merged
USER_ROLES = ("wall_fill", "furniture_stroke", "furniture_fill", "door_arc", "door_leaf",
              "window_fill", "column_fill", "jamb_candidate", "drop")


def prim_signature(dr):
    return "|".join([str(dr.get("layer") or ""), str(dr.get("type")), str(to_hex(dr.get("color"))),
                     str(to_hex(dr.get("fill"))), f"{(dr.get('width') or 0):.2f}"])


def prim_id(dr, bx, by):
    key = prim_signature(dr) + "#" + build_path(dr.get("items", []), -bx, -by)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def apply_overrides(role, dr, bx, by):
    """Client decisions from the CAD editor: signature rule first, then per-primitive."""
    sig = prim_signature(dr)
    if sig in OV_SIGS:
        role = None if OV_SIGS[sig] == "drop" else OV_SIGS[sig]
    pid = prim_id(dr, bx, by)
    if pid in OV_PRIMS:
        role = None if OV_PRIMS[pid] == "drop" else OV_PRIMS[pid]
    return role, pid, sig


EDITOR_DATA = ROOT / "plan-studio/editor-data"
REBUILD_PLANS = ROOT / "site/assets/plans"
EDITOR_DATA.mkdir(parents=True, exist_ok=True)
REBUILD_PLANS.mkdir(parents=True, exist_ok=True)

TARGET_UNITS = ["202", "204", "307", "308", "309", "310", "1311", "208", "1510", "201",
                "209", "1709", "2308", "203", "205", "206", "207", "1201", "2201", "2203",
                "2204", "1005"]

TYPE_LABELS = {"studio": "Студия", "1br": "1-комн.", "2br": "2-комн.", "3br": "3-комн."}

INK = "#182E46"
FLOOR_FILL = "#F3EFE8"
BALCONY_FILL = "#FFFFFF"
MUTED = "#9AA3AD"
PX_PER_PT = 3.2
MARGIN = 14.0

W_FURNITURE = 0.5
W_PARTITION = 0.9
W_DOOR_LEAF = 0.5
W_DOOR_ARC = 0.4
DOOR_ARC_DASH = "1.5,1.5"

# ---------------------------------------------------------------- color helpers

def to_hex(rgb):
    if rgb is None:
        return None
    r, g, b = rgb
    return "#%02x%02x%02x" % (max(0, min(255, round(r * 255))),
                               max(0, min(255, round(g * 255))),
                               max(0, min(255, round(b * 255))))


def luminance(hexcolor):
    r = int(hexcolor[1:3], 16); g = int(hexcolor[3:5], 16); b = int(hexcolor[5:7], 16)
    return (r + g + b) / 3.0


# Explicit bright / annotation fill colors (icon backdrops, safety marks, highlight dots,
# rare gradient accents) -> always dropped regardless of size.
FILL_DROP = {
    "#ff0000", "#df0000", "#0000ff", "#40a5ff", "#97cbff", "#e0f5ff",
    "#f7e046", "#ffeb28", "#dcd214", "#41c341", "#e8e8e8", "#cccccc", "#96e196",
    "#bd8b17", "#999999", "#ffcc99",
}
# Load-bearing shear/core walls (lift & stair cores) are drawn as a salmon/red hatch fill
# instead of the usual grey -- same structural role as wall_fill, just a different pen colour.
FILL_WALL_EXTRA = {"#fd8379"}
# Window/glazing mullion ticks along the facade (tiny rects marking the window band) -- keep,
# they also count as "covering" the polygon edge for the wall-coverage check.
FILL_WINDOW = {"#97cbff", "#e0f5ff"}
# Furniture / fixture outline strokes (thin grey linework: beds, sanitary ware, appliances)
STROKE_FURNITURE = {"#7f7f7f", "#aaaaaa"}
# Secondary wall stroke (thin outline variant of the wall band)
STROKE_WALL = {"#888786"}
# NOTE: #000000 as a STROKE (not fill) turned out to be a chevron/arrow annotation mark
# plus a small hatch patch, not partition walls -- real partition walls in this PDF are
# always drawn as small filled rects (classified under wall_fill), so #000000 stroke is
# dropped as annotation clutter (see STROKE_PARTITION, kept empty on purpose).
STROKE_PARTITION = set()
# Door swing arc
STROKE_DOOR = {"#ff6600"}
# Everything else stroke-side (hatch textures, dimensions, RG tags, safety icons,
# red/blue/green annotation marks) is dropped by default.

ICON_FILL_MAX = 7.0        # px bbox cutoff: below this, a black fill is an icon glyph fragment
FURNITURE_FILL_MAX_AREA = 900.0   # pt^2: above this, a white fill is the PDF's own floor base plate
COLUMN_SEG_MIN = 4.5        # pt: hatch dashes are shorter than this; column-cross diagonals are longer
COLUMN_SEG_MAX = 30.0
COLUMN_AR_LO, COLUMN_AR_HI = 0.82, 1.22
COLUMN_GROUP_GAP = 6.0

AREA_WORD_RE = re.compile(r"^\d+[.,]\d+$")


# ---------------------------------------------------------------- geometry helpers

def poly_bbox(poly):
    xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def point_in_poly(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]; xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def dist_point_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def dist_point_poly(x, y, poly):
    n = len(poly)
    best = 1e18
    for i in range(n):
        ax, ay = poly[i]; bx, by = poly[(i + 1) % n]
        d = dist_point_seg(x, y, ax, ay, bx, by)
        if d < best:
            best = d
    return best


def near_any_poly(x, y, polys, margin):
    for poly in polys:
        if point_in_poly(x, y, poly):
            return True
        if dist_point_poly(x, y, poly) <= margin:
            return True
    return False


def sample_points(items):
    """Sample representative (x,y) points (endpoints + bezier control points + a few
    interior samples) from a drawing's items, in original PDF page coordinates."""
    pts = []
    for it in items:
        kind = it[0]
        if kind == "l":
            pts.append((it[1].x, it[1].y)); pts.append((it[2].x, it[2].y))
        elif kind == "c":
            p1, p2, p3, p4 = it[1], it[2], it[3], it[4]
            for t in (0.0, 0.25, 0.5, 0.75, 1.0):
                mt = 1 - t
                x = mt**3*p1.x + 3*mt**2*t*p2.x + 3*mt*t**2*p3.x + t**3*p4.x
                y = mt**3*p1.y + 3*mt**2*t*p2.y + 3*mt*t**2*p3.y + t**3*p4.y
                pts.append((x, y))
        elif kind == "re":
            r = it[1]
            pts += [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
        elif kind == "qu":
            q = it[1]
            pts += [(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.lr.x, q.lr.y), (q.ll.x, q.ll.y)]
    return pts


# ---------------------------------------------------------------- path building

def build_path(items, dx, dy, dec=1):
    parts = []
    last = None

    def pt(p):
        return (round(p.x + dx, dec), round(p.y + dy, dec))

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
                parts.append(f"M {p1[0]} {p1[1]} C {p2[0]} {p2[1]} {p3[0]} {p3[1]} {p4[0]} {p4[1]}")
            last = p4
        elif kind == "re":
            r = it[1]
            x0, y0 = round(r.x0 + dx, dec), round(r.y0 + dy, dec)
            w = round(r.width, dec); h = round(r.height, dec)
            parts.append(f"M {x0} {y0} h {w} v {h} h {-w} Z")
            last = None
        elif kind == "qu":
            q = it[1]
            p1, p2, p3, p4 = pt(q.ul), pt(q.ur), pt(q.lr), pt(q.ll)
            parts.append(f"M {p1[0]} {p1[1]} L {p2[0]} {p2[1]} L {p3[0]} {p3[1]} L {p4[0]} {p4[1]} Z")
            last = None
    return " ".join(parts)


def split_rings(poly):
    """Some unit polygons concatenate an outer ring and one or more inner rings (e.g. a
    column footprint cut out of the room) into a single point list, closing each ring by
    repeating its first point mid-array rather than starting a fresh list. Split on those
    repeats so each ring becomes its own subpath instead of one bogus connecting edge."""
    rings = []
    start = 0
    for i in range(1, len(poly)):
        if poly[i] == poly[start] and i > start:
            rings.append(poly[start:i + 1])
            start = i + 1
    if start < len(poly) - 1:
        rings.append(poly[start:])
    return rings if rings else [poly]


def poly_to_path(poly, dx, dy):
    parts = []
    for ring in split_rings(poly):
        pts = [f"{round(x+dx,1)} {round(y+dy,1)}" for x, y in ring]
        parts.append("M " + " L ".join(pts) + " Z")
    return " ".join(parts)


# ---------------------------------------------------------------- classification

class Prim:
    __slots__ = ("dr", "role", "bbox", "seqno", "d", "fill")

    def __init__(self, dr, role, bbox):
        self.dr = dr
        self.role = role
        self.bbox = bbox   # (x0,y0,x1,y1) in plan coords
        self.seqno = dr.get("seqno", 0)


# ---------------------------------------------------------------- ArchiCAD layers (PDF optional content)
# The PDF keeps the architect's layers; get_drawings(extended=True) reports them as dr["layer"].
LAYER_WALL = {"Structural - Bearing", "კედლები", "დიაფრაგმა", "Interior - Partition"}
LAYER_FURNITURE = {"ავეჯი სრული"}                    # furniture, full set
LAYER_WINDOW = {"ვიტრაჟები"}                          # glazing / curtain walls
LAYER_COLUMN = {"კოლონები"}                            # columns
LAYER_JAMB = {"", None}                                # un-layered: door leaves / jambs (black fills)
LAYER_BALCONY = {"კ2 აივანი"}
LAYER_SHAFT = {"შახტა"}                               # vent/duct shafts: white box + black wedge + grey outline
# Everything else (dimensions, axes, accessibility paths "დადგენილება #41", fire "ხანძარმედეგობა",
# area hatch "ბინების კვადრატულობა", parking, wet-area tiles, unit numbers, sections...) is dropped.
DOOR_LEAF_MIN, DOOR_LEAF_MAX = 6.0, 40.0   # wide balcony / double doors have 35-38 pt leaves


def classify(dr, bx, by):
    """Role by ArchiCAD layer first, graphic attributes second. None = drop."""
    typ = dr.get("type")
    r = dr["rect"]
    w, h = r.width, r.height
    fill = to_hex(dr.get("fill"))
    stroke = to_hex(dr.get("color"))
    layer = dr.get("layer")
    items = dr.get("items", [])
    is_curve = any(it[0] == "c" for it in items)

    if layer in LAYER_WALL or layer in LAYER_WINDOW:
        if typ == "f":
            if fill in FILL_WINDOW:
                return "window_fill"
            if fill == "#ffffff" and layer in LAYER_WALL and min(w, h) <= 1.6 and max(w, h) >= 0.3 and max(w, h) <= 6.0:
                return "wall_fill"       # client rule: white slivers inside a wall run are wall, not gaps
            if fill == "#ffffff" or fill is None:
                return None
            return "wall_fill"           # grey, #fd8379 shear walls, any wall material
        if typ == "s":
            if stroke in STROKE_DOOR:
                if is_curve and max(w, h) > 2.0:
                    return "door_arc"
                if not is_curve and DOOR_LEAF_MIN <= max(w, h) <= DOOR_LEAF_MAX and min(w, h) < 1.0:
                    return "door_leaf"
                if not is_curve and (max(w, h) < DOOR_LEAF_MIN or (min(w, h) >= 1.0 and max(w, h) <= 40.0)):
                    return "door_dash"      # swing arc drawn as dashes or as a polyline of slanted pieces
                return None
            if stroke == "#6a6a6a":
                return "wall_hatch_candidate"   # wall hatch; also feeds nothing else
            return None
        return None

    if layer in LAYER_FURNITURE:
        if typ == "f":
            if fill == "#ffffff" and w * h <= FURNITURE_FILL_MAX_AREA:
                return "furniture_fill"
            return None
        if typ == "s" and stroke in STROKE_FURNITURE:
            return "furniture_stroke"
        if typ == "s" and stroke == "#000000":
            return "furniture_stroke"
        return None

    if layer in LAYER_SHAFT:
        if typ == "f" and fill == "#ffffff":
            return "shaft_white"
        if typ == "f" and fill == "#000000":
            return "shaft_black"
        if typ == "s" and stroke in ("#7f7f7f", "#6a6a6a", "#000000"):
            return "shaft_stroke"
        return None

    if layer in LAYER_COLUMN:
        if typ == "f" and fill == "#ffffff" and 4.0 <= max(w, h) <= 14.0:
            return "column_fill"
        return None

    if layer in LAYER_JAMB:
        if typ == "f" and fill == "#000000":
            return "jamb_candidate"
        if typ == "s" and stroke in STROKE_DOOR and not is_curve and (max(w, h) < DOOR_LEAF_MIN or (min(w, h) >= 1.0 and max(w, h) <= 40.0)):
            return "door_dash"
        if typ == "s" and stroke in STROKE_DOOR and not is_curve and DOOR_LEAF_MIN <= max(w, h) <= DOOR_LEAF_MAX and min(w, h) < 1.0:
            return "door_leaf"
        return None

    return None


def find_columns(hatch_candidates, dx, dy):
    """Among the dropped #6a6a6a hatch/cross strokes, find the ones long enough to be a
    column's diagonal cross (not the short hatch dashes), group by proximity, and where
    the group's bbox is roughly square treat it as a load-bearing column footprint."""
    segs = []
    for dr in hatch_candidates:
        r = dr["rect"]
        w, h = r.width, r.height
        if w < COLUMN_SEG_MIN and h < COLUMN_SEG_MIN:
            continue
        if max(w, h) > COLUMN_SEG_MAX:
            continue
        segs.append((r.x0, r.y0, r.x1, r.y1))
    n = len(segs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            ax0, ay0, ax1, ay1 = segs[i]; bx0, by0, bx1, by1 = segs[j]
            gap_x = max(ax0, bx0) - min(ax1, bx1)
            gap_y = max(ay0, by0) - min(ay1, by1)
            if gap_x <= COLUMN_GROUP_GAP and gap_y <= COLUMN_GROUP_GAP:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(segs[i])

    columns = []
    for members in groups.values():
        if len(members) < 2:
            continue
        x0 = min(m[0] for m in members); y0 = min(m[1] for m in members)
        x1 = max(m[2] for m in members); y1 = max(m[3] for m in members)
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            continue
        ar = w / h
        if COLUMN_AR_LO <= ar <= COLUMN_AR_HI and COLUMN_SEG_MIN <= max(w, h) <= COLUMN_SEG_MAX:
            columns.append((round(x0 + dx, 1), round(y0 + dy, 1), round(w, 1), round(h, 1)))
    return columns


# ---------------------------------------------------------------- text / labels

def collect_area_labels(page, clip, bx, by, polys):
    """Area labels ("NN.N m2") that sit inside this unit's own room/balcony polygons.
    Text is NOT given the +3pt clip margin other primitives get: a neighbouring unit's
    area figure can sit just outside our polygon but still within the bbox-margin crop
    rect, so we require the label to be strictly inside one of our own polygons."""
    words = page.get_text("words")
    labels = []
    used = set()
    for i, w in enumerate(words):
        x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
        if i in used:
            continue
        if not AREA_WORD_RE.match(txt):
            continue
        if x1 < clip.x0 or x0 > clip.x1 or y1 < clip.y0 or y0 > clip.y1:
            continue
        # Anchor point = where we actually place the text (x0, y1: baseline-left), not the
        # bbox centre -- matches the coordinator's "anchor point lies inside the polygon" rule.
        ax, ay = x0 - bx, y1 - by
        try:
            val = float(txt.replace(",", "."))
        except ValueError:
            val = None
        inside = any(point_in_poly(ax, ay, poly) for poly in polys)
        if not inside:
            # Tiny alcove exception only: small value, and the anchor is within 6pt of a
            # polygon edge (a neighbouring unit's own figure or a dimension-panel number
            # never lands this close in practice).
            near_tiny = (val is not None and val < 10.0 and
                         any(dist_point_poly(ax, ay, poly) <= 6.0 for poly in polys))
            if not near_tiny:
                continue
        # find an "m2" word right after this one, on the same line
        for j, w2 in enumerate(words):
            if j == i or j in used:
                continue
            tx0, ty0, tx1, ty1, ttxt = w2[0], w2[1], w2[2], w2[3], w2[4]
            if ttxt.lower() not in ("m2", "m²"):
                continue
            if abs(ty0 - y0) < 1.5 and 0 <= tx0 - x1 < 8:
                used.add(i); used.add(j)
                labels.append({
                    "value": txt.replace(".", ","),
                    "x": round(x0 - bx, 1), "y": round(y1 - by, 1),  # y1: bottom of glyph bbox, close to baseline
                    "size": round(y1 - y0, 1),
                })
                break
    return labels


# ---------------------------------------------------------------- union-find grouping (furniture)

def group_furniture(prims, gap=1.5):
    n = len(prims)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        ax0, ay0, ax1, ay1 = prims[i].bbox
        for j in range(i + 1, n):
            bx0, by0, bx1, by1 = prims[j].bbox
            gap_x = max(ax0, bx0) - min(ax1, bx1)
            gap_y = max(ay0, by0) - min(ay1, by1)
            if gap_x <= gap and gap_y <= gap:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(prims[i])
    return list(groups.values())


# ---------------------------------------------------------------- SVG assembly

def svg_header(w, h):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}">')


def render_svg(w, h, floor_poly_path, balcony_poly_path, wall_fill_paths, wall_stroke_paths,
               column_rects, door_arcs, furniture_paths, labels, include_furniture, window_lines=(),
               clip_paths=(), railing_lines=(), clip_id="unitclip", shafts=(), id_by_d=None):
    id_by_d = id_by_d or {}
    def _id(d):
        pid = id_by_d.get(d)
        return f' data-id="{pid}"' if pid else ""
    parts = [svg_header(w, h)]
    parts.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="#FFFFFF"/>')
    if clip_paths:
        parts.append('<defs><clipPath id="' + clip_id + '">' +
                     "".join(f'<path d="{d}"/>' for d in clip_paths) + '</clipPath></defs>')
        parts.append(f'<g clip-path="url(#{clip_id})">')
    if floor_poly_path:
        parts.append(f'<path d="{floor_poly_path}" fill="{FLOOR_FILL}" stroke="none" fill-rule="evenodd"/>')
    if balcony_poly_path:
        parts.append(f'<path d="{balcony_poly_path}" fill="{BALCONY_FILL}" stroke="none" fill-rule="evenodd"/>')
    for (p0, p1) in railing_lines:
        parts.append(f'<line x1="{p0[0]}" y1="{p0[1]}" x2="{p1[0]}" y2="{p1[1]}" '
                      f'stroke="{INK}" stroke-width="{W_RAILING}" stroke-linecap="square"/>')
    for d in wall_fill_paths:
        parts.append(f'<path d="{d}" fill="{INK}" stroke="none" fill-rule="evenodd"{_id(d)}/>')
    for d in wall_stroke_paths:
        parts.append(f'<path d="{d}" fill="none" stroke="{INK}" stroke-width="{W_PARTITION}" stroke-linecap="square"/>')
    for (p0, p1) in window_lines:
        parts.append(f'<line x1="{p0[0]}" y1="{p0[1]}" x2="{p1[0]}" y2="{p1[1]}" '
                      f'stroke="#FFFFFF" stroke-width="0.6"/>')
    for role, d in shafts:   # vent shafts: white box, black wedge, thin outline
        if role == "shaft_white":
            parts.append(f'<path d="{d}" fill="#FFFFFF" stroke="none"{_id(d)}/>')
        elif role == "shaft_black":
            parts.append(f'<path d="{d}" fill="{INK}" stroke="none"{_id(d)}/>')
        else:
            parts.append(f'<path d="{d}" fill="none" stroke="{INK}" stroke-width="{W_FURNITURE}"{_id(d)}/>')
    for (x, y, cw, ch) in column_rects:
        parts.append(f'<rect x="{x}" y="{y}" width="{cw}" height="{ch}" '
                      f'fill="#FFFFFF" stroke="{INK}" stroke-width="0.9"/>')
    for d in door_arcs:
        if isinstance(d, tuple) and d[0] == "dash":
            parts.append(f'<path d="{d[1]}" fill="none" stroke="{MUTED}" stroke-width="{W_DOOR_ARC}" stroke-linecap="round"/>')
        elif isinstance(d, tuple):
            parts.append(f'<path d="{d[1]}" fill="none" stroke="{INK}" stroke-width="{W_DOOR_LEAF}" stroke-linecap="square"{_id(d[1])}/>')
        else:
            parts.append(f'<path d="{d}" fill="none" stroke="{MUTED}" stroke-width="{W_DOOR_ARC}" '
                          f'stroke-dasharray="{DOOR_ARC_DASH}"{_id(d)}/>')
    if include_furniture:
        for fill, d in furniture_paths:
            if fill:
                parts.append(f'<path d="{d}" fill="{fill}" stroke="{INK}" stroke-width="{W_FURNITURE}" fill-rule="evenodd"{_id(d)}/>')
            else:
                parts.append(f'<path d="{d}" fill="none" stroke="{INK}" stroke-width="{W_FURNITURE}"{_id(d)}/>')
    if clip_paths:
        parts.append('</g>')
    for lab in labels:
        parts.append(f'<text x="{lab["x"]}" y="{lab["y"]}" font-family="Instrument Serif, Georgia, serif" '
                      f'font-size="6.5" fill="{INK}">{lab["value"]} м²</text>')
    parts.append('</svg>')
    return "\n".join(parts)


_BROWSER = {"pw": None, "browser": None, "page": None}


def _chromium_page():
    if _BROWSER["page"] is None:
        from playwright.sync_api import sync_playwright
        _BROWSER["pw"] = sync_playwright().start()
        _BROWSER["browser"] = _BROWSER["pw"].chromium.launch()
        _BROWSER["page"] = _BROWSER["browser"].new_page(device_scale_factor=1)
    return _BROWSER["page"]


def rasterize_svg(svg_text, px_per_pt, out_png):
    if NO_PNG:
        return
    """Rasterize with Chromium (MuPDF's SVG parser ignores <clipPath>), so the PNG equals
    what the editor shows in the browser. Instrument Serif is loaded from Google Fonts."""
    import re as _re
    m = _re.search(r'viewBox="[-\d.]+ [-\d.]+ ([\d.]+) ([\d.]+)"', svg_text)   # v3 SVGs have a plan-coord origin
    w, h = float(m.group(1)), float(m.group(2))
    W, H = int(round(w * px_per_pt)), int(round(h * px_per_pt))
    page = _chromium_page()
    page.set_viewport_size({"width": W, "height": H})
    html = ('<!doctype html><html><head><meta charset="utf-8">'
            '<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif&display=swap" rel="stylesheet">'
            '<style>html,body{margin:0;background:#fff}svg{display:block;width:%dpx;height:%dpx}</style></head><body>%s</body></html>'
            % (W, H, svg_text))
    page.set_content(html, wait_until="load")
    try:
        page.evaluate("document.fonts.ready.then(()=>true)")
        page.wait_for_timeout(150)
    except Exception:
        pass
    page.screenshot(path=str(out_png), clip={"x": 0, "y": 0, "width": W, "height": H})


def close_rasterizer():
    if _BROWSER["browser"] is not None:
        _BROWSER["browser"].close(); _BROWSER["pw"].stop()
        _BROWSER.update(pw=None, browser=None, page=None)


# ---------------------------------------------------------------- main per-unit

def _rect_contains(px, py, rect, tol):
    x0, y0, x1, y1 = rect
    return (x0 - tol) <= px <= (x1 + tol) and (y0 - tol) <= py <= (y1 + tol)


def check_wall_coverage(rings, wall_bboxes, column_rects, door_bboxes, window_bboxes=(),
                         balcony_ring=None, tol=3.5, door_tol=10.0, window_tol=6.0,
                         balcony_tol=2.0, step=1.0, min_gap=1.5):
    """Walk every edge of the unit polygon (crop-local coords) and flag stretches that are
    not adjacent to any wall fill/stroke, column, door opening, or the balcony's own ink
    outline (which we stroke ourselves where the room polygon runs along the balcony
    threshold) -- i.e. a hole in the envelope that isn't a door. Returns
    (total_uncovered_length, gaps) where gaps is a list of
    (ring_index, seg_index, frac_start, frac_end, length_pt)."""
    col_rects = [(cx, cy, cx + cw, cy + ch) for (cx, cy, cw, ch) in column_rects]
    gaps = []
    for ridx, ring in enumerate(rings):
        for i in range(len(ring) - 1):
            ax, ay = ring[i]; bx_, by_ = ring[i + 1]
            seg_len = math.hypot(bx_ - ax, by_ - ay)
            if seg_len < 1e-6:
                continue
            nsteps = max(1, int(seg_len / step))
            run_start = None
            for k in range(nsteps + 1):
                t = k / nsteps
                px, py = ax + (bx_ - ax) * t, ay + (by_ - ay) * t
                covered = (any(_rect_contains(px, py, rb, tol) for rb in wall_bboxes) or
                           any(_rect_contains(px, py, rb, tol) for rb in col_rects) or
                           any(_rect_contains(px, py, rb, door_tol) for rb in door_bboxes) or
                           any(_rect_contains(px, py, rb, window_tol) for rb in window_bboxes) or
                           (balcony_ring is not None and dist_point_poly(px, py, balcony_ring) <= balcony_tol))
                if not covered:
                    if run_start is None:
                        run_start = t
                else:
                    if run_start is not None:
                        gap_len = (t - run_start) * seg_len
                        if gap_len > min_gap:
                            gaps.append((ridx, i, run_start, t, gap_len))
                        run_start = None
            if run_start is not None:
                gap_len = (1.0 - run_start) * seg_len
                if gap_len > min_gap:
                    gaps.append((ridx, i, run_start, 1.0, gap_len))
    total = sum(g[4] for g in gaps)
    return total, gaps


def _rect_dims_along_axis(rect, ax, ay):
    x0, y0, x1, y1 = rect
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    vals = [cx * ax + cy * ay for cx, cy in corners]
    return max(vals) - min(vals)


def classify_boundary_point(px, py, wall_bboxes, door_bboxes, window_bboxes,
                             tol_wall=2.6, tol_door=10.0, tol_window=4.0):
    """wall wins over door/window: real fill is definitive evidence of a wall even right
    next to a door swing. Only where there is NO real fill do we ask whether it's a door
    (arc within tol_door) or a window (glazing fill within tol_window); anything left is
    'nothing' -- a stretch that needs a synthesized band."""
    if any(_rect_contains(px, py, rb, tol_wall) for rb in wall_bboxes):
        return "wall"
    if any(_rect_contains(px, py, rb, tol_door) for rb in door_bboxes):
        return "door"
    if any(_rect_contains(px, py, rb, tol_window) for rb in window_bboxes):
        return "window"
    return "nothing"


def edge_has_wall_evidence(p0, p1, wall_bboxes, tol_wall=2.6, step=0.5, min_along=1.0):
    """Is there at least one real wall-fill primitive that actually RUNS ALONG this edge
    (worth bridging its gaps), as opposed to an edge that was never a structural wall to
    begin with (e.g. a balcony's open railing), which we leave exactly as already drawn.

    Two checks, both needed:
      - CORNER proximity, not centre: a wall run's own end is often well past this edge's
        span, but its nearest CORNER sits right on the edge's line -- e.g. the wall above
        and below a small recessed niche only touches this edge at its very end.
      - elongated ALONG this edge's own direction, more than across it: this is what tells
        a genuine wall run (touches the edge, extends far along it) apart from a
        perpendicular connector/junction piece that merely ends flush with this edge's
        corner (e.g. a vertical party wall ending at a balcony's horizontal outer rail)."""
    ax, ay = p0; bx_, by_ = p1
    dx, dy = bx_ - ax, by_ - ay
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return False
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    for rect in wall_bboxes:
        along = _rect_dims_along_axis(rect, ux, uy)
        across = _rect_dims_along_axis(rect, nx, ny)
        if along < min_along or along <= across:
            continue
        x0, y0, x1, y1 = rect
        for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            t = (cx - ax) * ux + (cy - ay) * uy
            perp = abs((cx - ax) * nx + (cy - ay) * ny)
            if -tol_wall <= t <= L + tol_wall and perp <= tol_wall:
                return True
    return False


def edge_is_facade(p0, p1, window_bboxes, near_tol=6.0, step=0.5):
    ax, ay = p0; bx_, by_ = p1
    L = math.hypot(bx_ - ax, by_ - ay)
    if L < 1e-9:
        return False
    n = max(1, int(L / step))
    for k in range(n + 1):
        t = k / n
        px, py = ax + (bx_ - ax) * t, ay + (by_ - ay) * t
        if any(_rect_contains(px, py, rb, near_tol) for rb in window_bboxes):
            return True
    return False


def edge_thickness(p0, p1, wall_bboxes, near_tol=15.0, fallback=2.0):
    """Median thickness (measured perpendicular to the edge) of the real wall fills that
    touch this edge, so a synthesized band matches its real neighbours."""
    ax, ay = p0; bx_, by_ = p1
    dx, dy = bx_ - ax, by_ - ay
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return fallback
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    vals = []
    for rect in wall_bboxes:
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        t = (cx - ax) * ux + (cy - ay) * uy
        perp = abs((cx - ax) * nx + (cy - ay) * ny)
        if -near_tol <= t <= L + near_tol and perp <= near_tol + 3.0:
            along = _rect_dims_along_axis(rect, ux, uy)
            across = _rect_dims_along_axis(rect, nx, ny)
            if along <= across:
                # this bbox is elongated ACROSS the edge direction, not along it -- it's a
                # perpendicular connector/junction piece (e.g. a T-joint stub), not a run of
                # this wall, so its "across" extent is that stub's length, not a thickness
                continue
            vals.append(across)
    if vals:
        vals.sort()
        return vals[len(vals) // 2]
    return fallback


def outward_normal(p0, p1, ring, eps=0.5):
    ax, ay = p0; bx_, by_ = p1
    dx, dy = bx_ - ax, by_ - ay
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return (0.0, 0.0)
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    mx, my = (ax + bx_) / 2, (ay + by_) / 2
    if point_in_poly(mx + nx * eps, my + ny * eps, ring):
        return (-nx, -ny)
    return (nx, ny)


def quad_to_path(quad, dec=1):
    pts = [f"{round(x, dec)} {round(y, dec)}" for x, y in quad]
    return "M " + " L ".join(pts) + " Z"


def synthesize_wall_bands(rings, wall_bboxes, door_bboxes, window_bboxes, step=0.5,
                           tol_wall=2.6, tol_door=10.0, tol_window=4.0,
                           overlap=0.3, ext_default=3.2, int_default=2.0):
    """The client rule: a wall must read as ONE continuous band of ONE thickness; the only
    break is a real door opening. Walk every edge of every ring in 0.5pt steps, classify
    each point (wall/door/window/nothing) from real PDF geometry, and for any window/nothing
    stretch on an edge that has real wall evidence elsewhere (see edge_has_wall_evidence),
    synthesize an ink band of the same thickness as the neighbouring real wall, extruded
    OUTWARD from the polygon edge (so the floor area is untouched), overlapping 0.3pt into
    its non-door neighbours so it reads as one piece with no seam. Window stretches also get
    a thin white centre-line. Returns (band_paths, window_lines, log) where log is
    [(ring, edge, class, length_pt), ...] for the before/after report."""
    band_paths, window_lines, log = [], [], []
    for ridx, ring in enumerate(rings):
        for eidx in range(len(ring) - 1):
            p0, p1 = ring[eidx], ring[eidx + 1]
            ax, ay = p0; bx_, by_ = p1
            L = math.hypot(bx_ - ax, by_ - ay)
            if L < 1e-6:
                continue
            if not edge_has_wall_evidence(p0, p1, wall_bboxes, tol_wall, step):
                log.append((ridx, eidx, "skip(no-wall-evidence)", round(L, 1)))
                continue
            n = max(1, int(round(L / step)))
            samples = []
            for k in range(n + 1):
                t = k / n
                px, py = ax + (bx_ - ax) * t, ay + (by_ - ay) * t
                samples.append((t, classify_boundary_point(px, py, wall_bboxes, door_bboxes,
                                                             window_bboxes, tol_wall, tol_door, tol_window)))
            runs = []
            rs, rc = samples[0]
            for t, cls in samples[1:]:
                if cls != rc:
                    runs.append((rs, t, rc)); rs, rc = t, cls
            runs.append((rs, samples[-1][0], rc))

            fallback = ext_default if edge_is_facade(p0, p1, window_bboxes) else int_default
            for ri, (t0, t1, cls) in enumerate(runs):
                log.append((ridx, eidx, cls, round((t1 - t0) * L, 1)))
                if cls not in ("window", "nothing"):
                    continue
                thickness = edge_thickness(p0, p1, wall_bboxes, near_tol=15.0, fallback=fallback)
                nx, ny = outward_normal(p0, p1, ring)
                prev_cls = runs[ri - 1][2] if ri > 0 else None
                next_cls = runs[ri + 1][2] if ri < len(runs) - 1 else None
                ot0 = max(0.0, t0 - (overlap / L if (ri > 0 and prev_cls != "door") else 0.0))
                ot1 = min(1.0, t1 + (overlap / L if (ri < len(runs) - 1 and next_cls != "door") else 0.0))
                sx, sy = ax + (bx_ - ax) * ot0, ay + (by_ - ay) * ot0
                ex, ey = ax + (bx_ - ax) * ot1, ay + (by_ - ay) * ot1
                quad = [(sx, sy), (ex, ey), (ex + nx * thickness, ey + ny * thickness),
                        (sx + nx * thickness, sy + ny * thickness)]
                band_paths.append(quad_to_path(quad))
                if cls == "window":
                    mx0, my0 = sx + nx * thickness / 2, sy + ny * thickness / 2
                    mx1, my1 = ex + nx * thickness / 2, ey + ny * thickness / 2
                    window_lines.append(((round(mx0, 1), round(my0, 1)), (round(mx1, 1), round(my1, 1))))
    return band_paths, window_lines, log



# ---------------------------------------------------------------- boundary band (client rule v2)
# The apartment's boundary wall is drawn by US as one solid band of one thickness per wall
# class, built from the unit polygon itself: facade 3.2 pt (edges with glazing or shared with
# the balcony), corridor/core 2.4 pt, walls shared with a neighbouring unit 2.0 pt. Mitred
# corners, one path per ring. The only breaks are door openings (a PDF door arc touching the
# edge); windows keep the band and get a thin white centre line. PDF wall fills that sit in
# the band zone are dropped so nothing sticks out with another thickness.

T_FACADE = STYLE["wall_bands"]["exterior_pt"]       # 3.2
T_CORRIDOR = STYLE["wall_bands"]["corridor_pt"]     # 2.4
T_SHARED = STYLE["wall_bands"]["inter_unit_pt"]     # 2.0
W_RAILING = 0.8
T_SHARED_MAX = 6.5   # a party wall thicker than this is not a shared wall but something else
CLIP_PAD = T_SHARED_MAX + 0.5


def _line_intersect(p, d, q, e):
    """Intersection of lines p+t*d and q+s*e; None if parallel."""
    den = d[0] * e[1] - d[1] * e[0]
    if abs(den) < 1e-9:
        return None
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / den
    return (p[0] + d[0] * t, p[1] + d[1] * t)


def ring_edges(ring):
    """[(p0, p1)] for a closed ring given either closed (last == first) or open."""
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    pts = [pts[0]] + [q for i, q in enumerate(pts[1:], 1) if math.hypot(q[0]-pts[i-1][0], q[1]-pts[i-1][1]) > 1e-6]
    if len(pts) > 2 and math.hypot(pts[0][0]-pts[-1][0], pts[0][1]-pts[-1][1]) < 1e-6:
        pts = pts[:-1]
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def offset_ring_points(ring, dists):
    """Outer polyline of the band: for each vertex the intersection of the two adjacent
    offset lines (mitre). dists[i] = outward offset of edge i. Returns list aligned with
    edges: outer point at the START of edge i (== end of edge i-1)."""
    edges = ring_edges(ring)
    n = len(edges)
    pts_closed = [e[0] for e in edges] + [edges[0][0]]
    normals = []
    for (p0, p1) in edges:
        nx, ny = outward_normal(p0, p1, pts_closed)
        normals.append((nx, ny))
    outer = []
    for i in range(n):
        j = (i - 1) % n
        (a0, a1), (b0, b1) = edges[j], edges[i]
        na, nb = normals[j], normals[i]
        da, db = dists[j], dists[i]
        pa = (a0[0] + na[0] * da, a0[1] + na[1] * da); va = (a1[0] - a0[0], a1[1] - a0[1])
        pb = (b0[0] + nb[0] * db, b0[1] + nb[1] * db); vb = (b1[0] - b0[0], b1[1] - b0[1])
        m = _line_intersect(pa, va, pb, vb)
        if m is None:  # collinear: just offset the shared vertex
            m = (b0[0] + nb[0] * db, b0[1] + nb[1] * db)
        # guard absurd mitres on very sharp angles
        if math.hypot(m[0] - b0[0], m[1] - b0[1]) > 3.0 * max(da, db) + 0.5:
            m = (b0[0] + nb[0] * db, b0[1] + nb[1] * db)
        outer.append(m)
    return edges, normals, outer


def _proj_range(rect, p0, p1):
    """Projection of a bbox onto edge p0->p1 as (t0, t1) in edge fractions, plus its
    perpendicular distance (min over corners) from the edge line."""
    ax, ay = p0; bx_, by_ = p1
    dx, dy = bx_ - ax, by_ - ay
    L = math.hypot(dx, dy)
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    x0, y0, x1, y1 = rect
    ts, ps = [], []
    for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        ts.append(((cx - ax) * ux + (cy - ay) * uy) / L)
        ps.append(abs((cx - ax) * nx + (cy - ay) * ny))
    return max(0.0, min(ts)), min(1.0, max(ts)), min(ps), L


def _edge_touches_rect(rect, p0, p1, tol):
    t0, t1, perp, L = _proj_range(rect, p0, p1)
    return perp <= tol and t1 > t0


def edge_shares(p0, p1, other_ring, tol=3.0):
    return edge_share_dist(p0, p1, other_ring, tol) is not None


def edge_share_dist(p0, p1, other_ring, tol=3.0):
    """Perpendicular distance to the nearest parallel, overlapping edge of another ring
    (within tol), i.e. the thickness of the wall between the two rooms; None if none."""
    ax, ay = p0; bx_, by_ = p1
    L = math.hypot(bx_ - ax, by_ - ay)
    ux, uy = (bx_ - ax) / L, (by_ - ay) / L
    best = None
    for (q0, q1) in ring_edges(other_ring):
        M = math.hypot(q1[0] - q0[0], q1[1] - q0[1])
        if M < 1e-6:
            continue
        vx, vy = (q1[0] - q0[0]) / M, (q1[1] - q0[1]) / M
        if abs(ux * vx + uy * vy) < 0.985:
            continue
        perp = abs((q0[0] - ax) * (-uy) + (q0[1] - ay) * ux)
        if perp > tol:
            continue
        t0 = ((q0[0] - ax) * ux + (q0[1] - ay) * uy) / L
        t1 = ((q1[0] - ax) * ux + (q1[1] - ay) * uy) / L
        lo, hi = max(0.0, min(t0, t1)), min(1.0, max(t0, t1))
        if (hi - lo) * L > 4.0 and (best is None or perp < best):
            best = perp
    return best


def boundary_bands(ring, neighbour_rings, balcony_ring, door_bboxes, window_bboxes, wall_bboxes,
                   is_balcony=False, unit_ring=None, unit_face=None, edges_out=None):
    """Returns (band_paths, window_lines, railing_lines, door_runs_log, outer_face).
    edges_out (optional list): receives one dict per ring edge with the per-edge decision
    (p0, p1, cls, T, doors/wins as (t0,t1) fractions) for the v3 importer (tools/plan-import.py).
    ring / other rings in crop-local coords. For the balcony ring: edges shared with the
    unit are skipped (the unit's own band covers them), edges with real wall fills along
    them get a 2.0 band, the rest are railings (thin line)."""
    edges = ring_edges(ring)
    n = len(edges)
    # per-edge thickness class
    dists, classes = [], []
    for (p0, p1) in edges:
        if is_balcony:
            if unit_ring is not None and edge_shares(p0, p1, unit_ring):
                cls, T = "shared_with_unit", 0.0
            elif any(_edge_touches_rect(rb, p0, p1, 2.6) and _proj_range(rb, p0, p1)[1] - _proj_range(rb, p0, p1)[0] > 0.05 for rb in wall_bboxes):
                cls, T = "balcony_wall", T_SHARED
            else:
                cls, T = "railing", 0.0
        else:
            if balcony_ring and any(edge_shares(p0, p1, br) for br in balcony_ring):
                cls, T = "threshold", T_FACADE
            elif any(_edge_touches_rect(rb, p0, p1, 6.0) for rb in window_bboxes):
                cls, T = "facade", T_FACADE
            else:
                gaps = [g for g in (edge_share_dist(p0, p1, nr, T_SHARED_MAX) for nr in neighbour_rings) if g is not None]
                if gaps:
                    cls, T = "shared", T_SHARED          # client: party walls as thin as the balcony side walls
                else:
                    cls, T = "corridor", T_CORRIDOR
        dists.append(T); classes.append(cls)
    # Client rule: the OUTER face of a wall is one straight line; thickness steps
    # are on the room side. Group consecutive edges into "runs" of one wall (parallel edges,
    # possibly separated by short perpendicular jogs <= JOG_MAX) and give every edge of a run the
    # same outer line: T_k = O - pos_k with O = max(pos_k + T_k). Jog edges draw no band.
    JOG_MAX = 8.0   # perpendicular connectors up to this length (jogs, shaft niches) carry no band; the run continues across them
    n_e = len(edges)
    def _dir(e):
        L = math.hypot(e[1][0] - e[0][0], e[1][1] - e[0][1])
        return ((e[1][0] - e[0][0]) / L, (e[1][1] - e[0][1]) / L) if L > 1e-9 else (0.0, 0.0)
    def _len(e):
        return math.hypot(e[1][0] - e[0][0], e[1][1] - e[0][1])
    def _par(u, v):
        return abs(u[0] * v[0] + u[1] * v[1]) > 0.985
    is_jog = [False] * n_e
    for i in range(n_e):
        if _len(edges[i]) <= JOG_MAX:
            u_prev, u_next = _dir(edges[(i - 1) % n_e]), _dir(edges[(i + 1) % n_e])
            u_me = _dir(edges[i])
            if _par(u_prev, u_next) and not _par(u_me, u_prev):
                is_jog[i] = True
    # assign run ids
    run_id = [-1] * n_e
    rid = 0
    start = next((i for i in range(n_e) if not is_jog[i]), 0)
    order = [(start + k) % n_e for k in range(n_e)]
    cur_dir = None
    for i in order:
        if is_jog[i]:
            continue
        u = _dir(edges[i])
        if cur_dir is not None and _par(u, cur_dir):
            run_id[i] = rid
        else:
            rid += 1; run_id[i] = rid; cur_dir = u
    # the wrap-around: last run may continue into the first
    firsts = [i for i in order if not is_jog[i]]
    if len(firsts) > 1 and run_id[firsts[0]] != run_id[firsts[-1]] and _par(_dir(edges[firsts[0]]), _dir(edges[firsts[-1]])):
        old_id = run_id[firsts[-1]]
        for i in range(n_e):
            if run_id[i] == old_id:
                run_id[i] = run_id[firsts[0]]
    closed_ring = [e[0] for e in edges] + [edges[0][0]]
    runs = {}
    for i in range(n_e):
        if not is_jog[i] and dists[i] > 0:
            runs.setdefault(run_id[i], []).append(i)
    for members in runs.values():
        i0 = members[0]
        nx0, ny0 = outward_normal(edges[i0][0], edges[i0][1], closed_ring)
        pos = {k: edges[k][0][0] * nx0 + edges[k][0][1] * ny0 for k in members}
        O = max(pos[k] + dists[k] for k in members)
        for k in members:
            dists[k] = max(0.3, O - pos[k])
    for i in range(n_e):
        if is_jog[i]:
            dists[i] = 0.0; classes[i] = "jog"
    if is_balcony and unit_face:
        # a balcony side wall continues the unit's wall: put its outer face on the same line
        face_edges = ring_edges(unit_face)
        for i, (p0, p1) in enumerate(edges):
            if classes[i] != "balcony_wall":
                continue
            nx, ny = outward_normal(p0, p1, [e[0] for e in edges] + [edges[0][0]])
            u = _dir((p0, p1))
            best = None
            for (q0, q1) in face_edges:
                v = _dir((q0, q1))
                if not _par(u, v):
                    continue
                # perpendicular offset of the face line from this edge, along the outward normal
                off = (q0[0] - p0[0]) * nx + (q0[1] - p0[1]) * ny
                # must overlap along the edge direction or touch its end
                t0 = ((q0[0] - p0[0]) * u[0] + (q0[1] - p0[1]) * u[1]); t1 = ((q1[0] - p0[0]) * u[0] + (q1[1] - p0[1]) * u[1])
                L = _len((p0, p1))
                if max(t0, t1) < -6.0 or min(t0, t1) > L + 6.0:
                    continue
                if 0.4 <= off <= 6.5 and (best is None or abs(off - dists[i]) < abs(best - dists[i])):
                    best = off
            if best is not None:
                dists[i] = best
    _, normals, outer = offset_ring_points(ring, dists)
    band_paths, window_lines, railing_lines, log = [], [], [], []
    for i, (p0, p1) in enumerate(edges):
        T = dists[i]; cls = classes[i]
        L = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        if cls == "shared_with_unit" or cls == "jog":
            if edges_out is not None:
                edges_out.append({"i": i, "p0": p0, "p1": p1, "cls": cls, "T": 0.0, "L": L, "doors": [], "wins": []})
            continue
        if cls == "railing":
            railing_lines.append(((round(p0[0], 1), round(p0[1], 1)), (round(p1[0], 1), round(p1[1], 1))))
            log.append((i, cls, round(L, 1), []))
            if edges_out is not None:
                edges_out.append({"i": i, "p0": p0, "p1": p1, "cls": cls, "T": W_RAILING, "L": L, "doors": [], "wins": []})
            continue
        nx, ny = normals[i]
        m0, m1 = outer[i], outer[(i + 1) % n]
        # door openings on this edge: PDF arcs touching the edge; opening = arc bbox projection
        doors = []
        for dg in door_bboxes:   # door geometries (see door_geometry)
            run = door_opening_on_edge(dg, p0, p1)
            if run:
                doors.append(run)
        doors.sort()
        # merge overlapping door runs
        merged = []
        for d in doors:
            if merged and d[0] <= merged[-1][1] + 0.01:
                merged[-1] = (merged[-1][0], max(merged[-1][1], d[1]))
            else:
                merged.append(d)
        # solid runs = complement of door runs
        solid, cur = [], 0.0
        for (d0, d1) in merged:
            if d0 > cur + 1e-6:
                solid.append((cur, d0))
            cur = max(cur, d1)
        if cur < 1.0 - 1e-6:
            solid.append((cur, 1.0))
        for (t0, t1) in solid:
            i0 = (p0[0] + (p1[0] - p0[0]) * t0, p0[1] + (p1[1] - p0[1]) * t0)
            i1 = (p0[0] + (p1[0] - p0[0]) * t1, p0[1] + (p1[1] - p0[1]) * t1)
            o0 = m0 if t0 <= 1e-6 else (i0[0] + nx * T, i0[1] + ny * T)
            o1 = m1 if t1 >= 1 - 1e-6 else (i1[0] + nx * T, i1[1] + ny * T)
            band_paths.append(quad_to_path([i0, i1, o1, o0]))
        # windows: white centre line over glazing extents (threshold: whole non-door run)
        wins = []
        if cls == "threshold":
            wins = list(solid)
        else:
            for wb in window_bboxes:
                t0, t1, perp, _ = _proj_range(wb, p0, p1)
                if perp <= 4.0 and (t1 - t0) * L >= 2.0:
                    wins.append((t0, t1))
        for (t0, t1) in wins:
            # keep inside solid runs, inset 0.6 pt from run ends
            for (s0, s1) in solid:
                a, b = max(t0, s0), min(t1, s1)
                if (b - a) * L < 2.0:
                    continue
                ia = a + 0.6 / L; ib = b - 0.6 / L
                c0 = (p0[0] + (p1[0] - p0[0]) * ia + nx * T / 2, p0[1] + (p1[1] - p0[1]) * ia + ny * T / 2)
                c1 = (p0[0] + (p1[0] - p0[0]) * ib + nx * T / 2, p0[1] + (p1[1] - p0[1]) * ib + ny * T / 2)
                window_lines.append(((round(c0[0], 1), round(c0[1], 1)), (round(c1[0], 1), round(c1[1], 1))))
        log.append((i, cls, round(L, 1), [(round(a * L, 1), round(b * L, 1)) for a, b in merged]))
        if edges_out is not None:
            edges_out.append({"i": i, "p0": p0, "p1": p1, "cls": cls, "T": T, "L": L, "n": (nx, ny),
                              "doors": list(merged), "wins": list(wins)})
    return band_paths, window_lines, railing_lines, log, outer



def simplify_jogs(ring, max_depth=2.0, max_iter=50):
    """Remove tiny jogs (duct niches, drafting steps) of depth <= max_depth: a short edge
    whose neighbours are parallel is collapsed by moving the shorter neighbour onto the
    line of the longer one. Works on an open or closed ring; returns an open ring."""
    pts = [tuple(p) for p in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    for _ in range(max_iter):
        n = len(pts)
        if n < 5:
            break
        changed = False
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            if L < 1e-6 or L > max_depth:
                continue
            p_, q = pts[(i - 1) % n], pts[(i + 2) % n]
            v1 = (a[0] - p_[0], a[1] - p_[1]); v2 = (q[0] - b[0], q[1] - b[1])
            L1, L2 = math.hypot(*v1), math.hypot(*v2)
            if L1 < 1e-6 or L2 < 1e-6:
                continue
            if abs((v1[0] * v2[0] + v1[1] * v2[1]) / (L1 * L2)) < 0.985:
                continue
            if L1 >= L2:
                # drop b, project q onto line(p_, a)
                ux, uy = v1[0] / L1, v1[1] / L1
                t = (q[0] - a[0]) * ux + (q[1] - a[1]) * uy
                q2 = (a[0] + ux * t, a[1] + uy * t)
                pts[(i + 2) % n] = q2
                del pts[(i + 1) % n]
            else:
                ux, uy = v2[0] / L2, v2[1] / L2
                t = (p_[0] - b[0]) * ux + (p_[1] - b[1]) * uy
                p2 = (b[0] + ux * t, b[1] + uy * t)
                pts[(i - 1) % n] = p2
                del pts[i]
            changed = True
            break
        if not changed:
            break
    # drop now-degenerate zero-length edges / collinear midpoints
    out = []
    n = len(pts)
    for i in range(n):
        a, b, c = pts[(i - 1) % n], pts[i], pts[(i + 1) % n]
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-6:
            continue
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) < 1e-6 and (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1]) > 0:
            continue
        out.append(b)
    return out


def door_geometry(dr, gdx, gdy):
    """(centre, endpoints, samples) of a door-swing arc in crop-local coords. The hinge/centre
    of a quarter arc is the bbox corner farthest from the arc's midpoint."""
    items = dr.get("items", [])
    pts = [(x + gdx, y + gdy) for (x, y) in sample_points(items)]
    if not pts:
        return None
    first = (items[0][1].x + gdx, items[0][1].y + gdy)
    last_it = items[-1]
    last = (last_it[-1].x + gdx, last_it[-1].y + gdy) if last_it[0] in ("l", "c") else pts[-1]
    mid = pts[len(pts) // 2]
    r = dr["rect"]
    corners = [(r.x0 + gdx, r.y0 + gdy), (r.x1 + gdx, r.y0 + gdy), (r.x1 + gdx, r.y1 + gdy), (r.x0 + gdx, r.y1 + gdy)]
    centre = max(corners, key=lambda c: math.hypot(c[0] - mid[0], c[1] - mid[1]))
    return {"centre": centre, "ends": (first, last), "samples": pts,
            "radius": max(math.hypot(first[0] - centre[0], first[1] - centre[1]),
                          math.hypot(last[0] - centre[0], last[1] - centre[1]))}


def door_opening_on_edge(door, p0, p1, tol=3.0):
    """If this door sits IN the wall p0->p1 (its hinge and its on-wall endpoint both lie on
    the edge line), return the opening as (t0, t1) fractions along the edge; else None.
    A door that merely swings up to the wall (interior door of a partition ending at
    this wall) is NOT an opening in it."""
    ax, ay = p0; bx_, by_ = p1
    L = math.hypot(bx_ - ax, by_ - ay)
    if L < 1e-6:
        return None
    ux, uy = (bx_ - ax) / L, (by_ - ay) / L
    def perp(q): return abs((q[0] - ax) * (-uy) + (q[1] - ay) * ux)
    def along(q): return ((q[0] - ax) * ux + (q[1] - ay) * uy) / L
    c = door["centre"]
    if perp(c) > tol:
        return None
    on_wall = [e for e in door["ends"] if perp(e) <= tol]
    if not on_wall:
        return None
    if len(on_wall) == 2:
        t0, t1 = sorted((along(on_wall[0]), along(on_wall[1])))
    else:
        e = min(on_wall, key=perp)
        t0, t1 = sorted((along(c), along(e)))
    t0, t1 = max(0.0, t0), min(1.0, t1)
    if (t1 - t0) * L < 4.0:
        return None
    return (t0, t1)



def collapse_jogs_inward(ring, max_depth=2.0, max_iter=60):
    """Client rule: the room-side face of a wall is straight; small steps (<= max_depth) go
    OUTWARD only. For a short jog between two parallel edges, the outer edge is shifted onto
    the inner edge's line (whole-edge translation, so no wall is ever skewed). A jog is left
    alone when the shift would bend a neighbouring edge that is not parallel to the jog."""
    pts = [tuple(p) for p in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    def para(u, v):
        lu, lv = math.hypot(*u), math.hypot(*v)
        return lu > 1e-9 and lv > 1e-9 and abs((u[0]*v[0] + u[1]*v[1]) / (lu*lv)) > 0.985
    for _ in range(max_iter):
        n = len(pts)
        if n < 5:
            break
        changed = False
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            jog = (b[0] - a[0], b[1] - a[1])
            L = math.hypot(*jog)
            if L < 1e-6 or L > max_depth:
                continue
            p_, q = pts[(i - 1) % n], pts[(i + 2) % n]
            e1 = (a[0] - p_[0], a[1] - p_[1]); e2 = (q[0] - b[0], q[1] - b[1])
            if not para(e1, e2) or para(e1, jog):
                continue
            closed = pts + [pts[0]]
            nx, ny = outward_normal(p_, a, closed)
            # which of the two parallel edges is further out?
            e2_outer = (jog[0] * nx + jog[1] * ny) > 0
            if e2_outer:
                r = pts[(i + 3) % n]; nxt = (r[0] - q[0], r[1] - q[1])
                if not para(nxt, jog):
                    continue
                shift = (a[0] - b[0], a[1] - b[1])
                pts[(i + 2) % n] = (q[0] + shift[0], q[1] + shift[1])
                del pts[(i + 1) % n]
            else:
                o = pts[(i - 2) % n]; prv = (p_[0] - o[0], p_[1] - o[1])
                if not para(prv, jog):
                    continue
                shift = (b[0] - a[0], b[1] - a[1])
                pts[(i - 1) % n] = (p_[0] + shift[0], p_[1] + shift[1])
                del pts[i]
            changed = True
            break
        if not changed:
            break
    out = []
    n = len(pts)
    for i in range(n):
        a, b, c = pts[(i - 1) % n], pts[i], pts[(i + 1) % n]
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-6:
            continue
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) < 1e-6 and (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1]) > 0:
            continue
        out.append(b)
    return out

def clip_ring_path(ring, pad):
    edges, normals, outer = offset_ring_points(ring, [pad] * len(ring_edges(ring)))
    return "M " + " L ".join(f"{round(x,1)} {round(y,1)}" for x, y in outer) + " Z"


def bbox_center_inside(bbox, rings):
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return any(point_in_poly(cx, cy, r) for r in rings)


def bbox_in_band_zone(bbox, rings, pad):
    """True if the bbox centre lies outside every ring or within pad of a ring edge."""
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    inside = any(point_in_poly(cx, cy, r) for r in rings)
    if not inside:
        return True
    return any(dist_point_poly(cx, cy, r) <= pad for r in rings)



def _shared_len(cand, ring, tol=3.0):
    """Total length of cand's edges that run collinear with (within tol) and overlap ring's edges."""
    total = 0.0
    for (p0, p1) in ring_edges(cand):
        ax, ay = p0; L = math.hypot(p1[0] - ax, p1[1] - ay)
        if L < 1e-6:
            continue
        ux, uy = (p1[0] - ax) / L, (p1[1] - ay) / L
        for (q0, q1) in ring_edges(ring):
            M = math.hypot(q1[0] - q0[0], q1[1] - q0[1])
            if M < 1e-6:
                continue
            vx, vy = (q1[0] - q0[0]) / M, (q1[1] - q0[1]) / M
            if abs(ux * vx + uy * vy) < 0.985:
                continue
            if abs((q0[0] - ax) * (-uy) + (q0[1] - ay) * ux) > tol:
                continue
            t0 = ((q0[0] - ax) * ux + (q0[1] - ay) * uy) / L
            t1 = ((q1[0] - ax) * ux + (q1[1] - ay) * uy) / L
            lo, hi = max(0.0, min(t0, t1)), min(1.0, max(t0, t1))
            if hi > lo:
                total += (hi - lo) * L
    return total


def _clip_polygon(d, bx, by):
    """The clip's own outline in plan coords (slanted balconies are not rectangles)."""
    pts = []
    for it in d.get("items", []):
        if it[0] == "l":
            a = (round(it[1].x - bx, 1), round(it[1].y - by, 1)); b = (round(it[2].x - bx, 1), round(it[2].y - by, 1))
            if not pts or pts[-1] != a:
                pts.append(a)
            pts.append(b)
        elif it[0] == "qu":
            q = it[1]
            pts = [(round(p.x - bx, 1), round(p.y - by, 1)) for p in (q.ul, q.ur, q.lr, q.ll)]
            break
        elif it[0] == "re":
            r = it[1]
            pts = [(r.x0 - bx, r.y0 - by), (r.x1 - bx, r.y0 - by), (r.x1 - bx, r.y1 - by), (r.x0 - bx, r.y1 - by)]
            break
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        r = d.get("scissor") or d.get("rect")
        pts = [(r.x0 - bx, r.y0 - by), (r.x1 - bx, r.y0 - by), (r.x1 - bx, r.y1 - by), (r.x0 - bx, r.y1 - by)]
    return [[float(x), float(y)] for x, y in pts]


def find_extra_balconies(page, poly, known, bx, by, others=(), max_area=3200.0, max_value=16.0, min_shared=6.0):
    """Second/third balconies the upstream extraction missed: small clip regions outside the
    unit polygon that SHARE A WALL with it (collinear overlap >= min_shared, and not more with
    any other unit) and contain a small area label."""
    words = page.get_text("words")
    labels = []
    for w in words:
        if AREA_WORD_RE.match(w[4]):
            try:
                v = float(w[4].replace(",", "."))
            except ValueError:
                continue
            nxt = [w2 for w2 in words if w2[4].lower() in ("m2", "m²") and abs(w2[1] - w[1]) < 1.5 and 0 <= w2[0] - w[2] < 8]
            if nxt and v <= max_value:
                labels.append((v, (w[0] + w[2]) / 2 - bx, (w[1] + w[3]) / 2 - by))
    out = []
    for d in page.get_drawings(extended=True):
        if d.get("type") != "clip":
            continue
        r = d.get("scissor") or d.get("rect")
        if r is None or r.width < 8 or r.height < 8 or r.width * r.height > max_area:
            continue
        cand = _clip_polygon(d, bx, by)
        cx = sum(p[0] for p in cand) / len(cand); cy = sum(p[1] for p in cand) / len(cand)
        if point_in_poly(cx, cy, poly) or any(point_in_poly(cx, cy, k) for k in known):
            continue
        if not any(point_in_poly(lx, ly, cand) for v, lx, ly in labels):
            continue
        mine = _shared_len(cand, poly)
        if mine < min_shared:
            continue
        if any(_shared_len(cand, o) > mine for o in others):
            continue
        if any(abs(o[0][0] - cand[0][0]) < 1 and abs(o[0][1] - cand[0][1]) < 1 for o in out):
            continue
        out.append(cand)
    return out


def process_unit(unit, doc, dbg_combos=None, collect=None):
    floor_no = int(unit[:-2])
    fd = json.loads((DATA_DIR / f"floor-{floor_no}.json").read_text())
    u = fd["units"][unit]
    poly = OV_POLYS.get(unit) or u["poly"]
    balcony = u.get("balcony")
    bx, by = fd["bbox"][0], fd["bbox"][1]
    page = doc[fd["page"] - 1]
    balconies = [balcony] if balcony else []
    others = [split_rings(o["poly"])[0] for n, o in fd["units"].items() if n != unit]
    balconies += find_extra_balconies(page, poly, balconies, bx, by, others=others)
    polys = [poly] + balconies
    x0b, y0b, x1b, y1b = poly_bbox([p for pl in polys for p in pl])

    crop_x0, crop_y0 = x0b - MARGIN, y0b - MARGIN
    crop_w, crop_h = (x1b - x0b) + 2 * MARGIN, (y1b - y0b) + 2 * MARGIN
    dx, dy = -crop_x0, -crop_y0          # plan -> crop-local (poly/balcony are plan coords)
    gdx, gdy = -bx - crop_x0, -by - crop_y0   # raw PDF page coords -> crop-local (drawings/text)

    clip = pymupdf.Rect(crop_x0 + bx, crop_y0 + by, crop_x0 + crop_w + bx, crop_y0 + crop_h + by)
    drawings = page.get_drawings(extended=True)
    # NB: pymupdf's Rect.intersects() is False for zero-width/height rects (a single vertical or
    # horizontal line!), which silently dropped every door leaf and many furniture edges.
    def _hits(r):
        return not (r.x1 < clip.x0 or r.x0 > clip.x1 or r.y1 < clip.y0 or r.y0 > clip.y1)
    kept = [dr for dr in drawings if dr.get("rect") is not None and _hits(dr["rect"])]

    if dbg_combos is not None:
        from collections import Counter
        c = Counter()
        for dr in kept:
            key = (dr.get("type"), to_hex(dr.get("color")), to_hex(dr.get("fill")),
                   round(dr.get("width") or 0, 2))
            c[key] += 1
        dbg_combos[unit] = c

    clip_margin = 6.0   # generous: the SVG clipPath does the real cut
    wall_fill_paths, wall_stroke_paths, furniture_prims, door_arc_paths, hatch_candidates = [], [], [], [], []
    wall_bboxes, door_bboxes, window_bboxes = [], [], []   # crop-local (x0,y0,x1,y1), for the wall-coverage check
    window_lines = []   # thin white centre-lines drawn inside synthesized window bands
    wall_fill_bboxes = []
    wall_fill_pts = []      # crop-local sample points per wall fill (v3 importer: rectangle test)
    wall_stroke_bboxes = []
    jamb_candidates = []
    door_geoms = []
    column_fills = []
    shaft_paths = []
    door_leaf_paths = []
    door_dashes = []
    counts = {"wall_fill": 0, "wall_stroke": 0, "partition_stroke": 0, "door_arc": 0,
              "furniture_fill": 0, "furniture_stroke": 0, "dropped": 0, "column_hatch": 0}

    raw_records = []   # every primitive near the unit, for the CAD click-editor
    ID_BY_D = {}       # rendered path string -> primitive id (so the editor can edit the render)
    for dr in kept:
        role = classify(dr, bx, by)
        base_role = role
        role, pid, sig = apply_overrides(role, dr, bx, by)
        if pid in OV_GEOM:
            # client-edited vertices: rebuild the primitive as polylines (plan -> page coords)
            items2 = []
            for sp in OV_GEOM[pid]:
                for a, b in zip(sp[:-1], sp[1:]):
                    items2.append(("l", pymupdf.Point(a[0] + bx, a[1] + by), pymupdf.Point(b[0] + bx, b[1] + by)))
                if dr.get("type") == "f" and len(sp) > 2:
                    items2.append(("l", pymupdf.Point(sp[-1][0] + bx, sp[-1][1] + by), pymupdf.Point(sp[0][0] + bx, sp[0][1] + by)))
            if items2:
                allx = [q[0] + bx for sp in OV_GEOM[pid] for q in sp]; ally = [q[1] + by for sp in OV_GEOM[pid] for q in sp]
                dr = dict(dr); dr["items"] = items2; dr["rect"] = pymupdf.Rect(min(allx), min(ally), max(allx), max(ally))
        pts = sample_points(dr.get("items", []))
        pts_plan = [(x - bx, y - by) for (x, y) in pts]
        keep = any(near_any_poly(x, y, polys, clip_margin) for (x, y) in pts_plan) if pts_plan else False
        if keep and dr.get("type") in ("f", "s"):
            rr = dr["rect"]
            raw_records.append({
                "id": pid, "sig": sig, "layer": dr.get("layer") or "", "type": dr.get("type"),
                "stroke": to_hex(dr.get("color")), "fill": to_hex(dr.get("fill")),
                "width": round(dr.get("width") or 0, 2),
                "d": build_path(dr.get("items", []), gdx, gdy),
                "bbox": [round(rr.x0 + gdx, 1), round(rr.y0 + gdy, 1), round(rr.x1 + gdx, 1), round(rr.y1 + gdy, 1)],
                "base": base_role, "role": role,
            })

        if role == "wall_hatch_candidate":
            if keep:
                hatch_candidates.append(dr)
            counts["column_hatch"] += 1
            continue

        if role is None:
            counts["dropped"] += 1
            continue
        if not keep:
            counts["dropped"] += 1
            continue

        items = dr.get("items", [])
        d = build_path(items, gdx, gdy)
        if not d:
            counts["dropped"] += 1
            continue
        ID_BY_D[d] = pid
        r = dr["rect"]
        bbox = (r.x0 - bx, r.y0 - by, r.x1 - bx, r.y1 - by)

        if role == "column_fill":
            column_fills.append((r.x0 + gdx, r.y0 + gdy, r.width, r.height))
            continue
        if role in ("shaft_white", "shaft_black", "shaft_stroke"):
            shaft_paths.append((role, d, (r.x0 + gdx, r.y0 + gdy, r.x1 + gdx, r.y1 + gdy)))
            continue
        if role == "door_leaf":
            door_leaf_paths.append((d, (r.x0 + gdx, r.y0 + gdy, r.x1 + gdx, r.y1 + gdy)))
            continue
        if role == "door_dash":
            door_dashes.append((d, (r.x0 + gdx, r.y0 + gdy, r.x1 + gdx, r.y1 + gdy),
                                [(x + gdx, y + gdy) for (x, y) in sample_points(dr.get("items", []))]))
            continue
        if role == "jamb_candidate" or (role == "wall_fill" and to_hex(dr.get("fill")) == "#000000"):
            # black fills: door leaves / jambs when next to a door (decided later), else icon bits or walls
            jamb_candidates.append((d, (r.x0 + gdx, r.y0 + gdy, r.x1 + gdx, r.y1 + gdy), role))
            continue
        if role in ("wall_fill", "window_fill"):
            bb = (r.x0 + gdx, r.y0 + gdy, r.x1 + gdx, r.y1 + gdy)
            if role == "wall_fill":
                wall_fill_paths.append(d); wall_fill_bboxes.append(bb); counts["wall_fill"] += 1
                wall_fill_pts.append([(x + gdx, y + gdy) for (x, y) in sample_points(dr.get("items", []))])
                wall_bboxes.append(bb)
            else:
                window_bboxes.append(bb)
        elif role in ("wall_stroke", "partition_stroke"):
            wall_stroke_paths.append(d)
            wall_stroke_bboxes.append((r.x0 + gdx, r.y0 + gdy, r.x1 + gdx, r.y1 + gdy))
            wall_bboxes.append((r.x0 + gdx, r.y0 + gdy, r.x1 + gdx, r.y1 + gdy))
            counts[role] += 1
        elif role == "door_arc":
            door_arc_paths.append(d); counts["door_arc"] += 1
            door_bboxes.append((r.x0 + gdx, r.y0 + gdy, r.x1 + gdx, r.y1 + gdy))
            door_geoms.append(door_geometry(dr, gdx, gdy))
        elif role in ("furniture_fill", "furniture_stroke"):
            fill = "#FFFFFF" if role == "furniture_fill" else None
            furniture_prims.append(Prim(dr, role, bbox))
            furniture_prims[-1].d = d
            furniture_prims[-1].fill = fill
            counts[role] += 1

    # dashed swing arcs: cluster the 1-pt orange pieces by proximity; a cluster of >= 4 pieces
    # spanning >= 5 pt is a door swing (drawn piece by piece, so it keeps its dashed look)
    if door_dashes:
        n = len(door_dashes); parent = list(range(n))
        def _f(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]; i = parent[i]
            return i
        for i in range(n):
            a = door_dashes[i][1]
            for j in range(i + 1, n):
                b = door_dashes[j][1]
                if max(a[0], b[0]) - min(a[2], b[2]) <= 1.6 and max(a[1], b[1]) - min(a[3], b[3]) <= 1.6:
                    ri, rj = _f(i), _f(j)
                    if ri != rj:
                        parent[ri] = rj
        clusters = {}
        for i in range(n):
            clusters.setdefault(_f(i), []).append(door_dashes[i])
        for members in clusters.values():
            if len(members) < 4:
                continue
            x0 = min(m[1][0] for m in members); y0 = min(m[1][1] for m in members)
            x1 = max(m[1][2] for m in members); y1 = max(m[1][3] for m in members)
            if max(x1 - x0, y1 - y0) < 5.0:
                continue
            pts = [q for m in members for q in m[2]]
            # ends = the two points farthest apart; centre = bbox corner farthest from the arc's middle
            far = max(((math.hypot(a[0]-b[0], a[1]-b[1]), a, b) for a in pts[::3] for b in pts[::3]), key=lambda t: t[0])
            e0, e1 = far[1], far[2]
            mid = min(pts, key=lambda q: abs(math.hypot(q[0]-e0[0], q[1]-e0[1]) - math.hypot(q[0]-e1[0], q[1]-e1[1])))
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            centre = max(corners, key=lambda c: math.hypot(c[0]-mid[0], c[1]-mid[1]))
            # a half-circle swing (bbox ~2:1, chord along the long side): centre = chord midpoint
            Lb, Sb = max(x1 - x0, y1 - y0), min(x1 - x0, y1 - y0)
            chord = math.hypot(e0[0]-e1[0], e0[1]-e1[1])
            if abs(Lb - 2 * Sb) < 3.0 and chord > 0.9 * Lb:
                centre = ((e0[0] + e1[0]) / 2, (e0[1] + e1[1]) / 2)
            radius = max(math.hypot(e0[0]-centre[0], e0[1]-centre[1]), math.hypot(e1[0]-centre[0], e1[1]-centre[1]))
            door_arc_paths.append(("dash", " ".join(m[0] for m in members)))
            door_bboxes.append((x0, y0, x1, y1))
            door_geoms.append({"centre": centre, "ends": (e0, e1), "samples": pts, "radius": radius})
            counts["door_arc"] += 1

    # columns: the architect's own column layer (white squares); hatch heuristic only as fallback
    columns = [(round(x, 1), round(y, 1), round(cw, 1), round(ch, 1)) for (x, y, cw, ch) in column_fills] \
              or find_columns(hatch_candidates, gdx, gdy)

    # floor / balcony polygons (crop-local)
    floor_path = poly_to_path(poly, dx, dy)
    balcony_path = poly_to_path(balcony, dx, dy) if balcony else None

    # area labels
    labels_raw = collect_area_labels(page, clip, bx, by, polys)
    labels = []
    for lab in labels_raw:
        lx = lab["x"] + dx
        ly = lab["y"] + dy
        # keep on-canvas even when the PDF number sits right at the crop's edge
        lx = max(1.0, min(crop_w - 1.0, lx))
        ly = max(7.0, min(crop_h - 1.0, ly))
        labels.append({"value": lab["value"], "x": round(lx, 1), "y": round(ly, 1)})

    # furniture grouping (raw objects) — absolute plan coords for JSON
    groups = group_furniture(furniture_prims, gap=1.5)
    objects = []
    for gi, members in enumerate(groups, start=1):
        gx0 = min(m.bbox[0] for m in members); gy0 = min(m.bbox[1] for m in members)
        gx1 = max(m.bbox[2] for m in members); gy1 = max(m.bbox[3] for m in members)
        paths = []
        for m in members:
            d_abs = build_path(m.dr.get("items", []), 0.0, 0.0)  # absolute page coords
            # convert to absolute PLAN coords (page - floor bbox origin)
            d_abs = build_path(m.dr.get("items", []), -bx, -by)
            paths.append({"d": d_abs, "fill": m.fill, "width": W_FURNITURE, "id": ID_BY_D.get(m.d)})
        objects.append({
            "id": f"r{gi}", "kind": "raw",
            "x": round(gx0, 1), "y": round(gy0, 1),
            "w": round(gx1 - gx0, 1), "h": round(gy1 - gy0, 1),
            "rot": 0, "src": "pdf", "paths": paths,
        })


    # --- boundary bands (client rule v2), neighbour clipping, in-zone fill drop
    poly_local_rings = [[(x + dx, y + dy) for x, y in ring] for ring in split_rings(poly)]
    # the contour is taken EXACTLY as drawn (steps and all): no jog simplification, it skewed long walls
    outer_ring = collapse_jogs_inward(poly_local_rings[0], max_depth=0.0)   # exact contour: steps stay on the room side
    inner_rings = poly_local_rings[1:]
    balcony_rings = [collapse_jogs_inward([(x + dx, y + dy) for x, y in b], max_depth=0.0) for b in balconies]
    balcony_local_ring = balcony_rings[0] if balcony_rings else None
    # the floor / balcony fills follow the simplified rings so the band and the floor agree
    floor_path = "M " + " L ".join(f"{round(x,1)} {round(y,1)}" for x, y in outer_ring) + " Z"
    for ring in inner_rings:
        floor_path += " M " + " L ".join(f"{round(x,1)} {round(y,1)}" for x, y in ring) + " Z"
    balcony_path = " ".join("M " + " L ".join(f"{round(x,1)} {round(y,1)}" for x, y in br) + " Z" for br in balcony_rings) or None
    neighbour_rings = []
    for num, other in fd["units"].items():
        if num == unit:
            continue
        for ring in split_rings(other["poly"])[:1]:
            neighbour_rings.append([(x + dx, y + dy) for x, y in ring])
        if other.get("balcony"):
            neighbour_rings.append([(x + dx, y + dy) for x, y in other["balcony"]])

    keep_rings = [outer_ring] + balcony_rings
    # PDF wall fills: keep only those clearly inside (partitions, columns); the boundary is ours
    # PDF wall fills: only partitions/columns clearly inside the ROOM; balconies carry no interior walls
    interior_fills = [(d, bb, pts) for d, bb, pts in zip(wall_fill_paths, wall_fill_bboxes, wall_fill_pts)
                      if not bbox_in_band_zone(bb, [outer_ring], T_FACADE + 1.0)
                      and not bbox_center_inside(bb, balcony_rings)]   # (path, bbox, sample pts) crop-local
    wall_fill_paths = [d for d, _, _ in interior_fills]
    wall_stroke_paths = [d for d, bb in zip(wall_stroke_paths, wall_stroke_bboxes) if bbox_center_inside(bb, keep_rings)]
    # door arcs: ours if most of the swing lies inside the unit / balcony (>= 60 % of samples)
    def _inside_frac(dg):
        pts = dg["samples"] if dg else []
        if not pts:
            return 0.0
        return sum(1 for (x, y) in pts if near_any_poly(x, y, keep_rings, 1.0)) / len(pts)
    door_keep = [(d, bb, dg) for d, bb, dg in zip(door_arc_paths, door_bboxes, door_geoms) if _inside_frac(dg) >= 0.6]
    door_arc_paths = [d for d, _, _ in door_keep]; door_bboxes = [bb for _, bb, _ in door_keep]
    door_geoms = [dg for _, _, dg in door_keep]
    # door leaves (orange straight strokes next to a kept swing) -> thin ink line
    leaf_paths = [d for d, bb in door_leaf_paths
                  if any(_rect_contains((bb[0]+bb[2])/2, (bb[1]+bb[3])/2, db, 3.0) for db in door_bboxes)
                  or (bbox_center_inside(bb, keep_rings) and any(dist_point_poly((bb[0]+bb[2])/2, (bb[1]+bb[3])/2, r) <= 4.0 for r in keep_rings))]
    # jambs: small black fills right next to one of our doors
    jamb_paths = []
    remaining = jamb_candidates
    for d, bb, role0 in remaining:
        near_door = any(_rect_contains((bb[0]+bb[2])/2, (bb[1]+bb[3])/2, db, 4.0) for db in door_bboxes)
        if near_door:
            jamb_paths.append(d)
        elif role0 == "wall_fill" and not bbox_in_band_zone(bb, keep_rings, T_FACADE + 1.0):
            wall_fill_paths.append(d)
            interior_fills.append((d, bb, None))
    # columns: real ones are small and sit in / on our rings
    columns = [c for c in columns if max(c[2], c[3]) <= 14.0 and
               (bbox_center_inside((c[0], c[1], c[0]+c[2], c[1]+c[3]), keep_rings)
                or any(dist_point_poly(c[0]+c[2]/2, c[1]+c[3]/2, r) <= 6.0 for r in keep_rings))]   # corner columns sit half outside
    # furniture objects: groups whose bbox centre is inside
    objects = [o for o in objects if bbox_center_inside((o["x"] + dx, o["y"] + dy, o["x"] + o["w"] + dx, o["y"] + o["h"] + dy), keep_rings)]
    furniture_svg_paths = [(m.fill, m.d) for m in furniture_prims
                           if bbox_center_inside((m.bbox[0] + dx, m.bbox[1] + dy, m.bbox[2] + dx, m.bbox[3] + dy), keep_rings)]

    synth_log = []
    v3_edges = {"unit": [], "balconies": []}
    bands, wlines, rails, log, outer_face = boundary_bands(outer_ring, neighbour_rings, balcony_rings,
                                                           door_geoms, window_bboxes, wall_bboxes,
                                                           edges_out=v3_edges["unit"])
    wall_fill_paths.extend(bands); window_lines.extend(wlines); railing_lines = list(rails)
    clip_faces = [outer_face]
    synth_log.extend(("unit",) + e for e in log)
    for ring in inner_rings:  # column footprints cut out of the room: solid ink
        wall_fill_paths.append("M " + " L ".join(f"{round(x,1)} {round(y,1)}" for x, y in ring) + " Z")
    for br in balcony_rings:
        b_edges = []
        v3_edges["balconies"].append(b_edges)
        bands, wlines, rails, log, bface = boundary_bands(br, neighbour_rings, [], door_geoms, window_bboxes, wall_bboxes,
                                                          is_balcony=True, unit_ring=outer_ring, unit_face=outer_face,
                                                          edges_out=b_edges)
        wall_fill_paths.extend(bands); window_lines.extend(wlines); railing_lines.extend(rails)
        clip_faces.append(bface)
        synth_log.extend(("balcony",) + e for e in log)
    wall_fill_paths.extend(jamb_paths)
    door_arc_paths = door_arc_paths + [("leaf", d) for d in leaf_paths]
    shafts = [(role, d) for role, d, bb in shaft_paths
              if bbox_center_inside(bb, keep_rings) or any(dist_point_poly((bb[0]+bb[2])/2, (bb[1]+bb[3])/2, r) <= T_SHARED_MAX for r in keep_rings)]
    # clip exactly to the outer faces (+0.5 pt so railings keep their full width): nothing may
    # stick out beyond the straight outer line of a wall
    def _face_path(face):
        edges_f, normals_f, outer_f = offset_ring_points(face, [0.5] * len(ring_edges(face)))
        return "M " + " L ".join(f"{round(x,1)} {round(y,1)}" for x, y in outer_f) + " Z"
    clip_paths = [_face_path(f) for f in clip_faces]
    # load-bearing columns sit on the corners and may stick out past the wall face: keep them whole
    for (cx_, cy_, cw_, ch_) in columns:
        clip_paths.append(f"M {cx_-0.3:.1f} {cy_-0.3:.1f} h {cw_+0.6:.1f} v {ch_+0.6:.1f} h {-(cw_+0.6):.1f} Z")

    w_out = round(crop_w, 1); h_out = round(crop_h, 1)

    if collect is not None:
        # v3 importer hook: hand over the pre-render geometry (crop-local unless noted) and write nothing
        collect.update({
            "unit": unit, "floor": floor_no,
            "crop": {"x0": round(crop_x0, 2), "y0": round(crop_y0, 2), "w": w_out, "h": h_out},
            "outer_ring": outer_ring, "inner_rings": inner_rings, "balcony_rings": balcony_rings,
            "edges": v3_edges,
            "door_geoms": door_geoms, "window_bboxes": list(window_bboxes),
            "interior_fills": interior_fills, "jamb_paths": list(jamb_paths),
            "columns": list(columns),
            "shafts": [(role, d, bb) for role, d, bb in shaft_paths
                       if bbox_center_inside(bb, keep_rings) or any(dist_point_poly((bb[0]+bb[2])/2, (bb[1]+bb[3])/2, r) <= T_SHARED_MAX for r in keep_rings)],
            "labels": labels,
            "objects": objects,            # plan coords
            "poly": poly, "balconies": balconies,   # plan coords
            "meta": UNITS_META.get(unit, {}),
            "counts": counts,
        })
        return counts, len(columns), len(labels_raw), 0.0, [], synth_log

    walls_svg = render_svg(w_out, h_out, floor_path, balcony_path, wall_fill_paths, wall_stroke_paths,
                            columns, door_arc_paths, furniture_svg_paths, labels, include_furniture=False,
                            window_lines=window_lines, clip_paths=clip_paths, railing_lines=railing_lines,
                            clip_id=f"clip-{unit}", shafts=shafts, id_by_d=ID_BY_D)
    full_svg = render_svg(w_out, h_out, floor_path, balcony_path, wall_fill_paths, wall_stroke_paths,
                           columns, door_arc_paths, furniture_svg_paths, labels, include_furniture=True,
                           window_lines=window_lines, clip_paths=clip_paths, railing_lines=railing_lines,
                           clip_id=f"clip-{unit}", shafts=shafts, id_by_d=ID_BY_D)

    # --- write files
    (EDITOR_DATA / f"unit-{unit}.svg").write_text(walls_svg)
    rasterize_svg(walls_svg, PX_PER_PT, EDITOR_DATA / f"unit-{unit}.png")

    (REBUILD_PLANS / f"unit-{unit}.svg").write_text(full_svg)
    rasterize_svg(full_svg, PX_PER_PT, REBUILD_PLANS / f"unit-{unit}.png")

    meta = UNITS_META.get(unit, {})
    out_json = {
        "unit": unit, "floor": floor_no,
        "type_label": TYPE_LABELS.get(meta.get("type"), meta.get("type", "")),
        "total": meta.get("total"), "balcony": meta.get("balcony"),
        "crop": {"x0": round(crop_x0, 1), "y0": round(crop_y0, 1),
                 "w": w_out, "h": h_out, "px_per_pt": PX_PER_PT},
        "poly": poly, "balcony_poly": balconies[0] if balconies else None, "balcony_polys": balconies,
        "objects": objects,
    }
    (EDITOR_DATA / f"unit-{unit}.json").write_text(json.dumps(out_json, ensure_ascii=False, indent=2))
    (EDITOR_DATA / f"raw-{unit}.json").write_text(json.dumps({
        "unit": unit, "crop": out_json["crop"], "roles": list(USER_ROLES), "prims": raw_records,
    }, ensure_ascii=False))

    # --- wall-coverage check: every polygon edge should be covered by ink (wall/column/door),
    # except real door openings; uncovered length should be ~0.
    uncovered_total, gaps = check_wall_coverage(poly_local_rings, wall_bboxes, columns, door_bboxes,
                                                 window_bboxes, balcony_ring=balcony_local_ring)

    return counts, len(columns), len(labels_raw), uncovered_total, gaps, synth_log


if __name__ == "__main__":
    args = sys.argv[1:]
    units = args if args else TARGET_UNITS
    doc = pymupdf.open(str(PDF))
    dbg = {}
    print(f"{'unit':6s} {'wallF':>6s} {'wallS':>6s} {'partS':>6s} {'door':>5s} "
          f"{'furnF':>6s} {'furnS':>6s} {'drop':>6s} {'cols':>5s} {'labels':>6s} {'uncov_pt':>9s}")
    coverage_report = {}
    synth_reports = {}
    for unit in units:
        counts, ncols, nlabels, uncovered, gaps, synth_log = process_unit(unit, doc, dbg_combos=dbg)
        coverage_report[unit] = (uncovered, gaps)
        synth_reports[unit] = synth_log
        print(f"{unit:6s} {counts['wall_fill']:6d} {counts['wall_stroke']:6d} "
              f"{counts['partition_stroke']:6d} {counts['door_arc']:5d} "
              f"{counts['furniture_fill']:6d} {counts['furniture_stroke']:6d} "
              f"{counts['dropped']:6d} {ncols:5d} {nlabels:6d} {uncovered:9.1f}")
    print("done:", len(units), "units")
    close_rasterizer()

    print("\n--- wall-coverage gaps (ring, seg_idx, frac_start-frac_end, length_pt) ---")
    for unit, (uncovered, gaps) in coverage_report.items():
        if not gaps:
            continue
        print(f"{unit}: total uncovered {uncovered:.1f}pt")
        for ridx, segidx, t0, t1, glen in sorted(gaps, key=lambda g: -g[4]):
            print(f"    ring{ridx} seg{segidx} [{t0:.2f}-{t1:.2f}] {glen:.1f}pt")

    if "--log-bands" in sys.argv or len(units) <= 3:
        print("\n--- boundary classification per unit (ring, edge, class, length_pt) "
              "-- BEFORE relabeling; window/nothing get a synthesized band unless "
              "flagged skip(no-wall-evidence) ---")
        for unit, log in synth_reports.items():
            print(f"{unit}:")
            classes_seen = set()
            for ring_group, eidx, cls, length, doors in log:
                classes_seen.add(cls)
                print(f"    [{ring_group}] edge{eidx}: {cls:16s} {length:6.1f}pt  doors(pt along edge)={doors}")
            print(f"    edge classes: {sorted(classes_seen)}")
