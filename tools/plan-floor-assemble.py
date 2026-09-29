#!/usr/bin/env python3
"""
Assemble floor plans from the FINAL per-unit SVG drawings (Figma export).

  python3 tools/plan-floor-assemble.py --floor 4     # one floor -> floor-4.svg/.png only
  python3 tools/plan-floor-assemble.py --all         # every floor + rewrite plans.json / data/plans.js

What it does
  1. Canvas = floor w/h (pt) x K(7.05) px.  All source geometry (unit polygons, clusters)
     already lives in FLOOR-LOCAL pt, so px = pt * K (the floor bbox is the PDF page crop,
     used only for w/h).
  2. Underlay = the OLD renderer's floor body (core/stairs/lifts, corridors, wall bands,
     partitions, doors, furniture) drawn in pt inside <g transform="scale(K)">, clipped to
     building hull MINUS every unit+balcony polygon (evenodd) -> only the "not an apartment"
     part survives.
  3. Every apartment is the final SVG of its base unit, translated into place.
  4. Floor fill recoloured per unit type, one-line caption placed from manifest.title.
"""
import argparse, asyncio, importlib.util, io, json, math, re, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
SVG_DIR_DEFAULT = ROOT / "plan-studio" / "final-svg"
DATA_DIR = ROOT / "plan-studio" / "data"
PLANS_DIR = ROOT / "plan-studio" / "v3" / "plans"
STYLE_PATH = fpconfig.STYLE
UNITS_JSON_PATH = fpconfig.SCHEDULE
COVERAGE_PATH = ROOT / "plan-studio" / "coverage-classes.json"
STRICT_PATH = ROOT / "plan-studio" / "coverage-strict-plan.json"
OUT_DIR = ROOT / "site" / "assets" / "plans"
PLANS_JSON_PATH = OUT_DIR / "plans.json"
# ревьюер 22.09: картинка квартиры = экспорт СТРОГОГО класса (coverage-strict-plan.json: 33 базы + 62 производных = 279 квартир)
try:
    STRICT_CLASS = {str(m): str(c["class"]) for c in json.loads((ROOT / "plan-studio" / "coverage-strict-plan.json").read_text(encoding="utf-8")) for m in c.get("members", [])}
except Exception:
    STRICT_CLASS = {}
PLANS_JS_PATH = ROOT / "site" / "data" / "plans.js"
FLOORS_DIR = PLANS_JS_PATH.parent / "floors"   # per-floor JSON split out of plans.js (perf: 25*~10KB fetched on demand instead of one 250KB blob)
SITE_VERSION_SCRIPT = fpconfig.CODE / "tools" / "site-version.py"

K = 7.05                      # px per pt (1 px == 1 cm), fixed by the Figma export
ALIGN_TOL = 6.0               # px
AUTOFIT_MAX_LT = 20.0          # px: --source glue autofit (task B, the reviewer 21.09) - only nudge a
                              # final that's within this of the expected wall band on L and T
AUTOFIT_AGREE_TOL = 6.0       # px: |L-R| and |T-B| must be under this for the L/T/R/B residual
                              # to read as one uniform shift rather than a size mismatch (a real
                              # size difference pushes L and R in OPPOSITE directions, e.g. 311)
WALL_BAND = 10.0              # px: the exterior wall band the final drawings put OUTSIDE the
                              # unit polygon on every side - the expected, systematic offset
TYPE_LABEL = {"studio": "Studio", "1br": "1BR", "2br": "2BR", "3br": "3BR"}
LABEL_PX = 46.0
BALCONY_ALPHA = 0.5
BALCONY_LABEL_PX = LABEL_PX   # заказчик 2026-09-22: все подписи этажа одного кегля (было 30)
BALCONY_LABEL_MIN_W_PT = 12.0  # skip the caption if the balcony polygon is narrower than this
WALLS_ONLY = True            # the reviewer 21.09: no furniture on the floor plan, hue only in the GUI
NEUTRAL_FLOOR = "#F3EFE8"    # rooms; balconies get the same at BALCONY_ALPHA
ASSET_VER = time.strftime("%Y%m%d%H%M")   # cache-buster: png names get ?v=…; the web server may cache images for 1h
OFFICE_LABEL_PX = LABEL_PX    # заказчик 2026-09-22: один кегль (было 40)
TEXT_MIN_AREA = 20.0
OFFICE_FLOORS = {2}          # the PDF marks offices (hatched 34.6 + 40.8 = officeSqm 75.4) only on floor 2; elsewhere free-standing area texts are corridors

FONT_STYLE = ("<style>@import url('https://fonts.googleapis.com/css2?"
              "family=Instrument+Serif&amp;display=swap');</style>")


# ---------------------------------------------------------------- old renderer (import)
def load_render_plans():
    """tools/render-plans.py is not importable by name (dash); load it by path. Its module
    body is constants + defs only, so importing it has no side effects."""
    spec = importlib.util.spec_from_file_location("render_plans", fpconfig.CODE / "tools" / "render-plans.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["render_plans"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- svg path geometry
_TOK = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")
_ARGC = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}


def path_points(d):
    """Every on-path point (+ bezier control points, which only ever make the bbox a hair
    too generous) of an SVG path, absolute + relative commands, H/V shorthand included."""
    seq = [("c", m.group(1)) if m.group(1) else ("n", float(m.group(2))) for m in _TOK.finditer(d)]
    pts, i, cur, start, cmd = [], 0, (0.0, 0.0), (0.0, 0.0), None

    def take(k):
        nonlocal i
        v = []
        while len(v) < k and i < len(seq) and seq[i][0] == "n":
            v.append(seq[i][1]); i += 1
        return v

    while i < len(seq):
        if seq[i][0] == "c":
            cmd = seq[i][1]; i += 1
        if cmd is None:
            i += 1; continue
        C, rel = cmd.upper(), cmd.islower()
        if C == "Z":
            cur = start; continue
        v = take(_ARGC[C])
        if len(v) < _ARGC[C]:
            break
        ox, oy = (cur if rel else (0.0, 0.0))
        if C in ("M", "L", "T"):
            cur = (v[0] + ox, v[1] + oy); pts.append(cur)
            if C == "M":
                start = cur
                cmd = "l" if rel else "L"       # implicit lineto for extra M coordinate pairs
        elif C == "H":
            cur = (v[0] + ox, cur[1]); pts.append(cur)
        elif C == "V":
            cur = (cur[0], v[0] + oy); pts.append(cur)
        elif C in ("C", "S", "Q"):
            n = 6 if C == "C" else 4
            for k in range(0, n, 2):
                pts.append((v[k] + ox, v[k + 1] + oy))
            cur = pts[-1]
        elif C == "A":
            cur = (v[5] + ox, v[6] + oy); pts.append(cur)
    return pts


def bbox(pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _scale_path_d(d, k, ndigits=1):
    """Re-emit an SVG path `d` string with every coordinate multiplied by `k` and rounded to
    `ndigits` - used for --figma-svg (the reviewer, 2026-09-21: per-element SVG for Figma import, «никаких
    transform - координаты абсолютные в px»), so a raw PDF-cluster path (pt) can be written
    straight as px without a wrapping <g transform="scale(K)">. Command letters pass through
    unchanged; only the "A" (arc) command has non-coordinate numeric args (x-axis-rotation,
    large-arc-flag, sweep-flag at arg positions 2/3/4 of its 7) - left unscaled. Not expected in
    this PDF's rectilinear wall/door paths (M/L/H/V/Z only) but handled for robustness."""
    cmd, argc, arg_i = None, 0, 0
    out = []
    for m in _TOK.finditer(d):
        if m.group(1):
            cmd = m.group(1)
            out.append(cmd)
            argc = _ARGC.get(cmd.upper(), 0)
            arg_i = 0
            continue
        val = float(m.group(2))
        if cmd and cmd.upper() == "A" and argc == 7 and (arg_i % 7) in (2, 3, 4):
            out.append(f"{val:g}")
        else:
            out.append(f"{val * k:.{ndigits}f}")
        arg_i += 1
    return " ".join(out)


# ---------------------------------------------------------------- pole of inaccessibility
def _poly_dist(pt, poly):
    """Signed distance point->polygon (positive inside)."""
    x, y = pt
    inside = False
    best = float("inf")
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]; bx, by = poly[(i + 1) % n]
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / ((by - ay) or 1e-12) + ax:
            inside = not inside
        dx, dy = bx - ax, by - ay
        t = 0.0 if (dx == 0 and dy == 0) else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return best if inside else -best


def pole_of_inaccessibility(poly, precision=1.0):
    """Grid + refine. Good enough for label placement, no external deps."""
    x0, y0, x1, y1 = bbox(poly)
    step = max((x1 - x0), (y1 - y0)) / 24.0 or 1.0
    best, bestd = ((x0 + x1) / 2, (y0 + y1) / 2), -1e18
    while step > precision:
        yy = y0 + step / 2
        while yy < y1:
            xx = x0 + step / 2
            while xx < x1:
                d = _poly_dist((xx, yy), poly)
                if d > bestd:
                    bestd, best = d, (xx, yy)
                xx += step
            yy += step
        x0, y0 = best[0] - step, best[1] - step
        x1, y1 = best[0] + step, best[1] + step
        step /= 4.0
    return best


# ---------------------------------------------------------------- unit svg surgery
_RE_SVG_OPEN = re.compile(r"<svg\b[^>]*>", re.S)
_RE_PAGE_BG = re.compile(r"\s*<rect\b[^>]*fill=\"#F5F5F5\"[^>]*/>", re.I)
_RE_EXPORT_G = re.compile(r"<g\b[^>]*id=\"[^\"]*export[^\"]*\"[^>]*>")
_RE_WHITE_RECT = re.compile(r"\s*<rect\b[^>]*fill=\"white\"[^>]*/>", re.I)
_RE_ID = re.compile(r'\bid="([^"]*)"')
_RE_URLREF = re.compile(r'url\(#([^)]*)\)')
_RE_HREF = re.compile(r'((?:xlink:)?href)="#([^"]*)"')


def _ent(name):
    """Figma's SVG export writes layer names into id="" as one decimal entity per UTF-8 byte."""
    return "".join(ch if ord(ch) < 128 else "".join(f"&#{b};" for b in ch.encode("utf-8")) for ch in name)


def _id_alt(name):
    return "(?:" + re.escape(name) + "|" + re.escape(_ent(name)) + ")"


# glazing-band details that read as a "comb" at floor scale: sash outlines, mullions, rails, handles
_COMB_NAMES = ["створка", "стойка", "перекладина", "ручка", "порог", "стекло"]
_RE_COMB = re.compile(r'<(?:path|rect|line)\b[^>]*\bid="(?:' + "|".join(_id_alt(n) for n in _COMB_NAMES) + r')[^"]*"[^>]*/>')
_RE_BALCONY = re.compile(r'(<path\b[^>]*\bid="' + _id_alt("Пол балкона") + r'(?:_\d+)?"[^>]*?)(/>)')
_RE_FILL_ATTR = re.compile(r'\s+fill="[^"]*"')
# balcony glazing/parapet layers of the base drawing - dropped when the member's balcony differs
_BALCONY_NAMES = ["окно балкона", "остекление", "рама окна", "ограждение"]
_RE_BALCONY_EL = re.compile(r'<(?:path|rect|line)\b[^>]*\bid="(?:u\d+_)?(?:' + "|".join(_id_alt(n) for n in _BALCONY_NAMES) + r')[^"]*"[^>]*/>')
BALCONY_DELTA_PT = 2.0


_KEEP_NAMES = ["стена", "колонна", "окно", "окно балкона", "окно-стекло", "клин окна", "коробка", "коробка петли",
               "полотно", "полотно двери", "дуга двери", "рамка проёма", "перегородка", "пилон",
               "остекление"]   # S1 parapet lines: the ONLY layer drawing the outer balcony edge in 303/306/… (diag 21.09)
# task B (the reviewer 21.09: «тонкие стенки между квартирой и балконом сделать такими же, как стены») -
# "остекление"/"рама окна"/"ограждение" were the glazing/frame/railing LINES drawn inside or on
# top of the "окно балкона" white strip; dropped outright (strip_to_walls's whitelist above no
# longer lists them - see _KEEP_NAMES history for the pre-v4 list) rather than kept and
# recoloured, since a stray thin line surviving on top of the new solid-ink strip would just
# look like a crack in the wall. "окно"/"клин окна"/"окно-стекло" (windows in EXTERIOR walls)
# are untouched - still kept, still white with the usual clipped wedge.
_RE_ANY_EL = re.compile(r'<(path|rect|line|polygon|polyline|circle|ellipse)\b([^>]*)/>')
_RE_ID_ATTR = re.compile(r'\bid="([^"]*)"')
_RE_BALCONY_WIN_EL = re.compile(r'(<(?:path|rect|line|polygon|polyline)\b[^>]*\bid="(?:u\d+_)?'
                                 + _id_alt("окно балкона") + r'(?:_\d+)?"[^>]*)/>')
# S1 parapet = white 10px strip + two "остекление" lines at 2.5/7.5 px from the outer face; some
# finals (303/304/305/306/309/311/312, diag 21.09) draw the outer balcony edge ONLY with these two
# lines -> the balcony came out open. Rendered as two 5px ink strokes they fuse into the 10px wall.
_RE_GLAZING_EL = re.compile(r'(<(?:path|rect|line|polygon|polyline)\b[^>]*\bid="(?:u\d+_)?'
                            + _id_alt("остекление") + r'(?:_\d+)?"[^>]*)/>')
GLAZING_LINE_STROKE_PX = 5.0


def _dec_id(v):
    """Layer name from an exported id: decimal entities per UTF-8 byte -> text; strip _N suffix."""
    try:
        t = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), v).encode("latin-1").decode("utf-8")
    except Exception:
        t = v
    t = re.sub(r'^u\d+_', '', t)          # instance() namespaces ids before the whitelist runs
    return re.sub(r'_\d+$', '', t)


def strip_to_walls(body):
    """Keep only whitelisted layers (by decoded id); elements without id (clip rects) are kept."""
    def rep(m):
        idm = _RE_ID_ATTR.search(m.group(2))
        if not idm:
            return m.group(0)
        name = _dec_id(idm.group(1))
        return m.group(0) if name in _KEEP_NAMES else ""
    return _RE_ANY_EL.sub(rep, body)


# task C (the reviewer 21.09, «щели у дверных коробок»): floor-gap-check's own sliver class - 2x8/8x2/3x14
# px light slivers at floor scale, every one sitting at a white "коробка"/"коробка петли" jamb
# box's own seam against the dark "стена" fill that abuts it. Both shapes are plain fills (no
# stroke on стена; коробка has a thin white/ink stroke) whose edges are meant to land on exactly
# the same line, a sub-pixel fraction apart in the original drawing - Chromium (the headless
# rasterizer, see rasterize()/save_png) antialiases each shape independently, so that fraction
# blends to a visibly lighter seam instead of cancelling out.
#
# shape-rendering="crispEdges" (tried first, per the task) does NOT fix this: it only helps when
# BOTH abutting shapes get it, and стена can't - several finals draw genuinely diagonal wall
# segments (balcony corners), and crispEdges staircases a diagonal edge, worse than the sliver.
# Tried and measured on floor 3 (baseline 8 slivers): crispEdges+stroke-bump on коробка alone ->
# 14 slivers; stroke-bump alone (no crisp) -> 17; crispEdges alone on every AXIS-ALIGNED
# wall-family element (стена/коробка/колонна/…, skipping only diagonal paths) -> 12, including a
# new 8x76 sliver - the underlay (layer 3, PDF c08/c19) and the X-pylons (layer 5) abut these
# same walls and are never crisped, so crisping only the final's own geometry just moves the
# mismatch to THOSE seams instead of closing it.
#
# What actually works, with no crispEdges and no risk to any diagonal wall: grow the коробка's
# OWN rectangle geometry by BOX_GROW_PX on every side before it's drawn. A plain fill overlap is
# antialiasing-mode-agnostic - two overlapping opaque same-ink-adjacent shapes merge visually
# whatever the rasterizer's edge treatment, so this closes the seam regardless of what стена (or
# the underlay, or an X-pylon) next to it does. 1.2px is under the wall band's own 10px width, so
# the box does not visibly grow into the room - tuned on floor 3: 0.6px -> 7 slivers, 1.2px -> 2
# (both under 20 area px, down from 8 at up to 42 area px, no new gap/stub/island), 1.6px starts
# growing a коробка clean past its own wall into a free-floating "island" finding - so 1.2px is
# the largest safe margin, not pushed further just because 2.0px alone reads better. Only
# коробка/коробка петли geometry is touched; стена and everything else are untouched - no
# коробки are removed, per the task.
_BOX_NAMES = {"коробка", "коробка петли"}
BOX_GROW_PX = 1.2
_RE_D_ATTR = re.compile(r'\bd="([^"]*)"')


def _grow_box_rect(d, grow):
    """Expand a коробка path's own bbox by `grow` on every side, re-emitted as an axis-aligned
    M/H/V/Z rectangle (every коробка/коробка петли path in the finals already is one - see
    _KEEP_NAMES/strip_to_walls)."""
    pts = path_points(d)
    if len(pts) < 2:
        return None
    x0, y0, x1, y1 = bbox(pts)
    x0, y0, x1, y1 = x0 - grow, y0 - grow, x1 + grow, y1 + grow
    return f"M{x0:.3f} {y0:.3f}H{x1:.3f}V{y1:.3f}H{x0:.3f}V{y0:.3f}Z"


def _crisp_box_edges(body):
    def rep(m):
        tag, attrs = m.group(1), m.group(2)
        idm = _RE_ID_ATTR.search(attrs)
        if not idm or _dec_id(idm.group(1)) not in _BOX_NAMES:
            return m.group(0)
        if tag == "rect":
            return m.group(0)             # no <rect>-shaped коробка seen in practice; skip safely
        dm = _RE_D_ATTR.search(attrs)
        if not dm:
            return m.group(0)
        new_d = _grow_box_rect(dm.group(1), BOX_GROW_PX)
        if new_d is None:
            return m.group(0)
        new_attrs = _RE_D_ATTR.sub(lambda _: f'd="{new_d}"', attrs)
        return f'<{tag}{new_attrs}/>'
    return _RE_ANY_EL.sub(rep, body)


class UnitDrawing:
    """One base unit's final SVG, parsed once and reused for every member of its class."""

    def __init__(self, number, path):
        self.number = number
        text = path.read_text(encoding="utf-8")
        m = _RE_SVG_OPEN.search(text)
        if not m:
            raise ValueError("no <svg> root")
        body = text[m.end(): text.rindex("</svg>")]
        # 1) page background (Figma canvas), 2) frame background (white rect right after the
        #    "<n> - export" group opens). Everything else - defs/masks included - is kept.
        body = _RE_PAGE_BG.sub("", body, count=1)
        g = _RE_EXPORT_G.search(body)
        if g:
            head, tail = body[:g.end()], body[g.end():]
            body = head + _RE_WHITE_RECT.sub("", tail, count=1)
        else:
            body = _RE_WHITE_RECT.sub("", body, count=1)
        self.body = body
        # ink bbox (walls) for the alignment self-check
        ink = []
        for tag in re.finditer(r"<(path|rect|polygon|polyline)\b[^>]*>", text):
            s = tag.group(0)
            if "#182E46" not in s.upper():
                continue
            d = re.search(r'\bd="([^"]*)"', s)
            if d:
                ink += path_points(d.group(1))
                continue
            if tag.group(1) == "rect":
                try:
                    x = float(re.search(r'\bx="([^"]*)"', s).group(1)) if 'x="' in s else 0.0
                    y = float(re.search(r'\by="([^"]*)"', s).group(1)) if 'y="' in s else 0.0
                    w = float(re.search(r'\bwidth="([^"]*)"', s).group(1))
                    h = float(re.search(r'\bheight="([^"]*)"', s).group(1))
                    ink += [(x, y), (x + w, y + h)]
                except Exception:
                    pass
        self.ink_bbox = bbox(ink) if ink else None

    def instance(self, member, floor_color, drop_balcony=False, ink=None):
        """A copy with every internal id namespaced (several units share `clip0_914_2`) and
        the #F3EFE8 floor fill swapped for the type colour."""
        pfx = f"u{member}_"
        b = _RE_ID.sub(lambda m: f'id="{pfx}{m.group(1)}"', self.body)
        b = _RE_URLREF.sub(lambda m: f"url(#{pfx}{m.group(1)})", b)
        b = _RE_HREF.sub(lambda m: f'{m.group(1)}="#{pfx}{m.group(2)}"', b)
        if floor_color:
            b = re.sub(r'fill="#F3EFE8"', f'fill="{floor_color}"', b, flags=re.I)
            # balcony: same tint, lighter (the finals leave it unfilled by the reviewer's rule 22)
            b = _RE_BALCONY.sub(lambda m: _RE_FILL_ATTR.sub("", m.group(1)) + f' fill="{floor_color}" fill-opacity="{BALCONY_ALPHA}"' + m.group(2), b)
        b = _RE_COMB.sub("", b)
        if WALLS_ONLY:
            b = strip_to_walls(b)
            b = _crisp_box_edges(b)
            if ink:
                # task B (the reviewer 21.09): the thin strip between apartment and balcony reads as a
                # wall now, same solid ink fill as "стена"/"колонна" - not a white window strip
                # with its glazing/frame/railing lines drawn separately (those are gone, see
                # _KEEP_NAMES above); same geometry, outline (if any) dropped.
                def _ink_balcony_win(m):
                    attrs = _RE_FILL_ATTR.sub("", m.group(1))
                    attrs = re.sub(r'\s+stroke="[^"]*"', "", attrs)
                    attrs = re.sub(r'\s+stroke-width="[^"]*"', "", attrs)
                    return f'{attrs} fill="{ink}"/>'
                b = _RE_BALCONY_WIN_EL.sub(_ink_balcony_win, b)
                def _ink_glazing(m):
                    attrs = _RE_FILL_ATTR.sub("", m.group(1))
                    attrs = re.sub(r'\s+stroke="[^"]*"', "", attrs)
                    attrs = re.sub(r'\s+stroke-width="[^"]*"', "", attrs)
                    attrs = re.sub(r'\s+stroke-linecap="[^"]*"', "", attrs)
                    return f'{attrs} fill="none" stroke="{ink}" stroke-width="{GLAZING_LINE_STROKE_PX}" stroke-linecap="butt"/>'
                b = _RE_GLAZING_EL.sub(_ink_glazing, b)
        if drop_balcony:
            # only the balcony layers inside the base balcony's zone (frame px, padded) - a unit
            # with several balconies keeps the ones that do match the member
            region = drop_balcony if isinstance(drop_balcony, (list, tuple)) else None
            def _rm(m):
                if region is None:
                    return ""
                dm = re.search(r'\bd="([^"]*)"', m.group(0))
                pts = path_points(dm.group(1)) if dm else []
                if not pts:
                    return ""
                xs = [q[0] for q in pts]; ys = [q[1] for q in pts]
                cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
                return "" if (region[0] <= cx <= region[2] and region[1] <= cy <= region[3]) else m.group(0)
            b = _RE_BALCONY_EL.sub(_rm, b)
        return b


# ---------------------------------------------------------------- data
def load_inputs():
    style = json.loads(STYLE_PATH.read_text(encoding="utf-8"))
    units_db = {u["number"]: u for u in json.loads(UNITS_JSON_PATH.read_text(encoding="utf-8"))["units"]}
    assign = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))["assign"]
    strict = json.loads(STRICT_PATH.read_text(encoding="utf-8"))
    cls = {c["class"]: c for c in strict}         # class -> {base, edit, members}
    return style, units_db, assign, cls


def load_manifest(svg_dir):
    p = svg_dir / "manifest.json"
    if not p.exists():
        return {}, None
    m = json.loads(p.read_text(encoding="utf-8"))
    units = m.get("units") or {}
    if isinstance(units, list):                    # tolerate the list-shaped variant
        units = {str(u["unit"]): u for u in units}
    return units, m.get("k", K)


def frame_origin(base):
    """Origin (pt, floor-local) of the base unit's Figma frame = its plan document bbox."""
    p = PLANS_DIR / f"unit-{base}.json"
    if not p.exists():
        return None
    bb = json.loads(p.read_text(encoding="utf-8"))["bbox"]
    return bb["x0"], bb["y0"]


def point_in_poly(pt, poly):
    x, y = pt; n = len(poly); ins = False
    for i in range(n):
        x1, y1 = poly[i]; x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xi > x:
                ins = not ins
    return ins


def unit_outline(poly, balcony):
    """One closed contour for the GUI highlight: apartment footprint united with its balcony
    (the reviewer 21.09: «выделение цельное, с балконом»). Falls back to the footprint alone.
    `balcony` - один полигон (старое поведение, без изменений) ИЛИ СПИСОК полигонов
    (ревьюер, 2026-09-22: units[N]["balconies"], балконов может быть несколько).

    Для списка union делается иначе: контуры из PDF (слой «ბინების კვადრატულობა») обведены по
    разным граням разделяющей стены, между poly и балконом остаётся зазор 1.4-2.2 pt, и
    прежний buffer(0.6) его НЕ перекрывает - union разваливался в MultiPolygon, а max(...)
    выбрасывал балкон из контура ховера. Поэтому здесь морфологическое замыкание
    buffer(+CLOSE) -> buffer(-CLOSE): щель стены зашивается, а сама фигура не раздувается."""
    if not balcony:
        return poly
    is_list = isinstance(balcony[0][0], (list, tuple))
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
        if is_list:
            CLOSE = 1.6   # > половины стены (2.2 pt) между контуром квартиры и контуром балкона
            u = unary_union([Polygon(poly).buffer(0)] + [Polygon(b).buffer(0) for b in balcony])
            u = u.buffer(CLOSE, join_style=2).buffer(-CLOSE, join_style=2)
        else:
            u = unary_union([Polygon(poly).buffer(0), Polygon(balcony).buffer(0.6, join_style=2)])
        if u.geom_type == "MultiPolygon":
            u = max(u.geoms, key=lambda g: g.area)
        u = u.simplify(0.15)
        return [(x, y) for x, y in u.exterior.coords][:-1]
    except Exception:
        return poly


# ---------------------------------------------------------------- ownership cells (v4)
def floor_cells(fd, style, extra_wall_boxes=None, seeds=None, balcony_seeds=None):
    """FLOOR-CONTRACT.md § «Ячейки владения». Rasterizes the floor at `res_pt` (style.json
    floor_render.cells) and grows every apartment (poly + balcony, one seed label each) and
    the public space (opened to drop door-gap slivers, seed label 0) into every wall/gap pixel
    via a nearest-seed distance transform - the boundary between two apartments lands in the
    MIDDLE of their shared wall instead of at either polygon's own (interior-face) edge, and
    the exterior/structural walls (c08+c19) are never claimed by any apartment. Cells further
    than max_dist_pt from every seed are left unlabeled (-1) - deep wall/shaft mass, drawn only
    by the separate `structure` layer, never by an apartment's fill.

    `extra_wall_boxes`: additional (x0,y0,x1,y1) pt bboxes burned into the wall mask `W` on top
    of c08/c19 - used for confirmed X-pylons (_xpilons_pdf), which this PDF never puts in c19
    (see _xpilons_pdf's own docstring). Without this, a pylon embedded in the shared wall
    between two apartments is neither an apartment seed nor a wall seed, so the distance
    transform hands its pixels to the nearer apartment as ordinary floor area - a white hole
    where the pylon should be (--source pdf, task 2026-09-21).

    `seeds` (--source trace «подстановка», 2026-09-21 вечер, ревьюер: «области из стен, а не из
    импорта»): optional {number: [poly, ...]} - when given, REPLACES fd["units"][n]["poly"]/
    ["balcony"] as this unit's seed geometry (every polygon in the list gets the same label i);
    fd["units"][n]["poly"] is still read for the `poly`/`outline` OUTPUT fields elsewhere, this
    only changes what floor_cells() itself grows from. `balcony_seeds`: optional {number:
    [poly, ...]} - replaces the single fd["units"][n].get("balcony") as the balcony_mask source
    (a unit can now have 0, 1 or several balconies - floor-21.json only ever carries one).
    Both default to None -> old behaviour (single poly + single balcony from fd, unchanged).

    Returns {"cells": {number: [(x,y),...] or None}, "res_pt", "grid": (nx,ny), "P": ndarray,
    "W": ndarray, "final": ndarray, "warnings": [...]}. Coordinates are floor-local pt, same
    frame as fd["units"][n]["poly"]."""
    import numpy as np
    from PIL import Image, ImageDraw
    from scipy import ndimage
    from shapely.geometry import box, Polygon
    from shapely.ops import unary_union

    cfg = ((style.get("floor_render") or {}).get("cells")) or {}
    res = float(cfg.get("res_pt", 0.25))
    open_pt = float(cfg.get("public_open_pt", 1.2))
    max_dist_pt = float(cfg.get("max_dist_pt", 4.0))
    simplify_pt = float(cfg.get("simplify_pt", 0.15))

    w, h = fd["w"], fd["h"]
    nx = max(1, math.ceil(w / res))
    ny = max(1, math.ceil(h / res))
    numbers = sorted(fd["units"], key=int)
    idx_of = {n: i + 1 for i, n in enumerate(numbers)}  # 1..N; 0 == public, -1 == unlabeled

    # step 1: apartment seeds (poly + balcony -> same label); balcony mask kept separately
    # per the contract's own wording, even though the `balcony` output field stays the raw
    # PDF polygon (unchanged) rather than anything re-derived from this raster.
    lbl_img = Image.new("I", (nx, ny), 0)
    ld = ImageDraw.Draw(lbl_img)
    bal_img = Image.new("1", (nx, ny), 0)
    bd = ImageDraw.Draw(bal_img)
    for n in numbers:
        u = fd["units"][n]
        i = idx_of[n]
        if seeds is not None:
            for poly in seeds.get(n, []):
                if poly and len(poly) >= 3:
                    ld.polygon([(x / res, y / res) for x, y in poly], fill=i)
        else:
            ld.polygon([(x / res, y / res) for x, y in u["poly"]], fill=i)
            if u.get("balcony"):
                ld.polygon([(x / res, y / res) for x, y in u["balcony"]], fill=i)
        if balcony_seeds is not None:
            for poly in balcony_seeds.get(n, []):
                if poly and len(poly) >= 3:
                    bd.polygon([(x / res, y / res) for x, y in poly], fill=1)
        elif u.get("balcony"):
            bd.polygon([(x / res, y / res) for x, y in u["balcony"]], fill=1)
    unit_label = np.asarray(lbl_img, dtype=np.int32)
    balcony_mask = np.asarray(bal_img, dtype=bool)

    # walls = c08 (grey) union c19 (structural/pink) raw PDF path polygons
    wall_img = Image.new("1", (nx, ny), 0)
    wd = ImageDraw.Draw(wall_img)
    for cid in ("c08", "c19"):
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        for p in (c.get("paths") if c else []):
            pts = path_points(p)
            if len(pts) >= 3:
                wd.polygon([(x / res, y / res) for x, y in pts], fill=1)
    for x0, y0, x1, y1 in (extra_wall_boxes or []):
        wd.rectangle([x0 / res, y0 / res, x1 / res, y1 / res], fill=1)
    W = np.asarray(wall_img, dtype=bool)

    # step 2: public space = not(apartments | walls), opened to drop sub-`public_open_pt`
    # slivers (door gaps) - what survives (corridors, halls, "outside the building") seeds
    # label 0.
    apartments_any = unit_label > 0
    P_raw = ~(apartments_any | W)
    radius_px = max(1, round(open_pt / res))
    yy, xx = np.ogrid[-radius_px:radius_px + 1, -radius_px:radius_px + 1]
    disk = (xx * xx + yy * yy) <= radius_px * radius_px
    P = ndimage.binary_opening(P_raw, structure=disk)

    # step 3: nearest-seed growth (distance_transform_edt over the "not a seed" mask)
    seed_bool = apartments_any | P
    label_grid = np.where(apartments_any, unit_label, 0)
    dist, idx = ndimage.distance_transform_edt(~seed_bool, sampling=(res, res), return_indices=True)
    nearest = label_grid[idx[0], idx[1]]
    final = np.where(dist <= max_dist_pt, nearest, -1)

    def row_runs_boxes(mask):
        """Row-run rectangles (not one box per pixel) - keeps the union() input small."""
        boxes = []
        for y in range(mask.shape[0]):
            row = mask[y]
            if not row.any():
                continue
            dd = np.diff(row.astype(np.int8))
            starts = list(np.where(dd == 1)[0] + 1)
            if row[0]:
                starts = [0] + starts
            ends = list(np.where(dd == -1)[0] + 1)
            if row[-1]:
                ends = ends + [mask.shape[1]]
            for s, e in zip(starts, ends):
                boxes.append(box(s * res, y * res, e * res, (y + 1) * res))
        return boxes

    # step 4: cell i = pixels labeled i, polygonized (row-run boxes -> shapely union); holes
    # discarded (a hole this small is wall/column mass closer to this apartment than to any
    # other seed - see contract note), largest component kept, simplified.
    cells, warnings = {}, []
    for n in numbers:
        i = idx_of[n]
        mask = final == i
        if not mask.any():
            warnings.append(f"floor_cells: {n} - пустая ячейка (нет пикселей с этой меткой)")
            cells[n] = None
            continue
        poly = unary_union(row_runs_boxes(mask))
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.interiors:
            poly = Polygon(poly.exterior)
        poly = poly.simplify(simplify_pt, preserve_topology=True)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.is_empty:
            warnings.append(f"floor_cells: {n} - пустой полигон после simplify/buffer(0)")
            cells[n] = None
            continue
        cells[n] = [(round(x, 4), round(y, 4)) for x, y in poly.exterior.coords[:-1]]
    return {"cells": cells, "res_pt": res, "grid": (nx, ny), "P": P, "W": W, "final": final,
            "balcony_mask": balcony_mask, "warnings": warnings}


NEIGHBOR_OVERLAP_TOL_PT2 = 0.1  # property 2: independent per-cell simplify(0.15pt) leaves each
                                # side of a shared wall off by a hair - two independently
                                # simplified polygons can overlap by a sliver even when the raw
                                # raster (pre-simplify) tiles perfectly; the reviewer 2026-09-21.


def floor_cells_checks(fd, cellinfo, adj):
    """The three FLOOR-CONTRACT self-checks, computed on the (simplified) cell polygons -
    property 1 (cell covers footprint + 0.9x balcony), property 2 (neighbours don't overlap
    beyond NEIGHBOR_OVERLAP_TOL_PT2 and don't gap beyond 0.05pt along their shared wall -
    checked pairwise over `adj`), and property 3 (a cell never intrudes into the opened public
    space `P`, EXCLUDING P components that lie entirely inside the cell's own exterior ring).

    Property 3 is measured in vector space, not by re-rasterizing the polygon: `P`'s local
    neighbourhood (cropped to the cell's own bbox, to keep the union small) is turned back into
    a polygon by the exact same row-run-box union used for the cells, kept as separate
    components (not merged into one shape). A component fully inside the cell's exterior is a
    closed pocket - a triangular sliver from the PDF unit polygon's own vertex noise, or a
    stair/column void wholly surrounded by this apartment - and `floor_cells()` deliberately
    fills it with the apartment's tone (see its "discard holes" step); that is correct, per the reviewer
    2026-09-21, not a corridor intrusion, so it is excluded here. Only a component that reaches
    OUTSIDE the exterior (a real, connected corridor/office edge the cell's boundary actually
    crosses) counts toward the violation. Violations are returned as data, not raised - the
    caller logs them as warnings."""
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union
    import numpy as np

    res = cellinfo["res_pt"]
    nx, ny = cellinfo["grid"]
    P = cellinfo["P"]
    cells = cellinfo["cells"]
    polys = {n: Polygon(pts) for n, pts in cells.items() if pts}

    def local_public_components(gx0, gy0, sub):
        """Row-run boxes of P, cropped to `sub` = P[gy0:gy1, gx0:gx1], in floor-local pt -
        same construction floor_cells() uses for the cells themselves, so both sides of the
        intersection agree pixel-for-pixel on where a wall/opening boundary actually falls.
        Returns a list of individual polygons (not merged) - containment is tested per
        component, so two components on opposite sides of the cell aren't conflated."""
        boxes = []
        for y in range(sub.shape[0]):
            row = sub[y]
            if not row.any():
                continue
            dd = np.diff(row.astype(np.int8))
            starts = list(np.where(dd == 1)[0] + 1)
            if row[0]:
                starts = [0] + starts
            ends = list(np.where(dd == -1)[0] + 1)
            if row[-1]:
                ends = ends + [sub.shape[1]]
            for s, e in zip(starts, ends):
                boxes.append(box((s + gx0) * res, (y + gy0) * res, (e + gx0) * res, (y + 1 + gy0) * res))
        if not boxes:
            return []
        u = unary_union(boxes)
        return list(u.geoms) if u.geom_type == "MultiPolygon" else [u]

    checks = {}
    for n, poly in polys.items():
        u = fd["units"][n]
        poly_a = Polygon(u["poly"]).area
        bal_a = Polygon(u["balcony"]).area if u.get("balcony") else 0.0
        cell_a = poly.area
        prop1_ok = cell_a >= poly_a + 0.9 * bal_a - 1e-6

        x0, y0, x1, y1 = poly.bounds
        gx0, gy0 = max(0, int(x0 / res) - 2), max(0, int(y0 / res) - 2)
        gx1, gy1 = min(nx, math.ceil(x1 / res) + 2), min(ny, math.ceil(y1 / res) + 2)
        components = local_public_components(gx0, gy0, P[gy0:gy1, gx0:gx1])
        corridor_overlap = 0.0
        enclosed_pockets = 0
        for comp in components:
            if poly.contains(comp):
                enclosed_pockets += 1  # closed pocket inside this cell - sanctioned fill, not an intrusion
                continue
            corridor_overlap += poly.intersection(comp).area

        checks[n] = {"poly_area": poly_a, "balcony_area": bal_a, "cell_area": cell_a,
                     "prop1_ok": prop1_ok, "corridor_overlap_pt2": corridor_overlap,
                     "prop3_ok": corridor_overlap < 0.01, "enclosed_pockets": enclosed_pockets, "neighbors": {}}

    seen = set()
    for a in polys:
        for b in adj.get(a, ()):
            if b not in polys or (b, a) in seen or a == b:
                continue
            seen.add((a, b))
            inter = polys[a].intersection(polys[b]).area
            gap = polys[a].distance(polys[b]) if inter < 1e-9 else 0.0
            ok = inter < NEIGHBOR_OVERLAP_TOL_PT2 and gap < 0.05
            checks[a]["neighbors"][b] = {"overlap_pt2": inter, "gap_pt": gap, "ok": ok}
            checks[b]["neighbors"][a] = {"overlap_pt2": inter, "gap_pt": gap, "ok": ok}
    return checks


def fmt_area(v):
    return f"{v:.1f}"


_RE_BAND = re.compile(r'<polygon points="(?:[-\d.]+,[-\d.]+ ){3}[-\d.]+,[-\d.]+" fill="#182E46"/>')
_RE_TEXT = re.compile(r'<text[^>]*>.*?</text>', re.S)


def _stroke_pt(px):
    """target stroke width in PX -> pt, so it comes out right after the caller's scale(K)."""
    return px / K


_RE_LIFT_LABEL = re.compile(r'^\d+[xX]\d+$')


def _fill_cluster_svg(fd, cid, color):
    """A cluster's raw paths as one solid fill (walls c08/c19, glazing mullions c21)."""
    c = next((x for x in fd["clusters"] if x["id"] == cid), None)
    if not c or not c.get("paths"):
        return ""
    d = " ".join(c["paths"])
    return f'<path d="{d}" fill="{color}" stroke="none" fill-rule="nonzero"/>'


def _doors_svg(rp, fd, ink):
    """c00 door-swing symbols (extract_pdf_doors: quarter-circle arc + straight leaf), in the
    finals' own language - empty opening, no jamb box: dashed arc [6,4] 1.2px + leaf 1.5px."""
    try:
        doors = rp.extract_pdf_doors(fd)
    except Exception:
        return ""
    leaf_w, arc_w = _stroke_pt(1.5), _stroke_pt(1.2)
    dash = f"{_stroke_pt(6):.3f},{_stroke_pt(4):.3f}"
    parts = []
    for d in doors:
        hinge, jamb2, leaf_tip, r = d["hinge"], d["jamb2"], d["leaf_tip"], d["r"]
        parts.append(f'<path d="M {hinge[0]:.2f} {hinge[1]:.2f} L {leaf_tip[0]:.2f} {leaf_tip[1]:.2f}" '
                      f'stroke="{ink}" stroke-width="{leaf_w:.3f}" stroke-linecap="round" fill="none"/>')
        a0 = math.atan2(leaf_tip[1] - hinge[1], leaf_tip[0] - hinge[0])
        a1 = math.atan2(jamb2[1] - hinge[1], jamb2[0] - hinge[0])
        delta = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
        segs = 16
        pts = [(hinge[0] + math.cos(a0 + delta * k / segs) * r, hinge[1] + math.sin(a0 + delta * k / segs) * r)
               for k in range(segs + 1)]
        dpath = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in pts)
        parts.append(f'<path d="{dpath}" stroke="{ink}" stroke-width="{arc_w:.3f}" '
                      f'stroke-dasharray="{dash}" stroke-linecap="round" fill="none"/>')
    return "".join(parts)


DOOR_BOX_W_PX = 4.0        # FLOOR-TRACE-SPEC.md правило 3/7: «коробка» - 4px вдоль проёма
DOOR_BOX_STROKE_PX = 0.8
DOOR_FRAME_SHRINK_PX = 1.2   # «рамка проёма» толщиной T-1.2 (чуть уже коробок, чтобы не вылезать)
DOOR_FRAME_STROKE_PX = 1.2
DOOR_LEAF_STROKE_PX = 1.5
DOOR_ARC_STROKE_PX = 1.2
DOOR_WALL_SEARCH_PT = 40.0   # окно поиска толщины стены вдоль перпендикуляра к проёму


def _wall_thickness_pt(cellinfo, p, dir_perp, search=DOOR_WALL_SEARCH_PT):
    """FLOOR-TRACE-SPEC.md правило 3: «T = толщина стены в этом месте, по W-маске». Идём вдоль
    перпендикуляра к пробегу проёма (dir_perp) от -search до +search с шагом res_pt, собираем
    непрерывные пробеги True в W (растровая маска стен floor_cells), берём пробег, ближайший к
    t=0 (сам торец двери может лежать чуть внутри или чуть снаружи стеновой маски - выбор
    ближайшего пробега терпим к обоим случаям). None, если рядом с точкой вообще нет стены."""
    res = cellinfo["res_pt"]
    W = cellinfo["W"]
    ny, nx = W.shape
    n = max(1, round(search / res))
    runs, cur0 = [], None
    for i in range(-n, n + 1):
        t = i * res
        x = p[0] + dir_perp[0] * t
        y = p[1] + dir_perp[1] * t
        gx, gy = int(x / res), int(y / res)
        inside = 0 <= gx < nx and 0 <= gy < ny and bool(W[gy, gx])
        if inside and cur0 is None:
            cur0 = t
        if not inside and cur0 is not None:
            runs.append((cur0, t - res))
            cur0 = None
    if cur0 is not None:
        runs.append((cur0, n * res))
    if not runs:
        return None

    def dist0(run):
        s, e = run
        return 0.0 if s <= 0 <= e else min(abs(s), abs(e))
    s, e = min(runs, key=dist0)
    return e - s + res


def _cell_label_at(cellinfo, p):
    res = cellinfo["res_pt"]
    final = cellinfo["final"]
    ny, nx = final.shape
    gx, gy = int(p[0] / res), int(p[1] / res)
    if 0 <= gx < nx and 0 <= gy < ny:
        return int(final[gy, gx])
    return -1


def _doors_svg_trace(rp, fd, ink, cellinfo, report, soft_walls=None, soft_wall_T=None, doors=None, collect=None):
    """FLOOR-TRACE-SPEC.md правило 3 «Двери»: по каждой дуге/полотну c00 (extract_pdf_doors) -
    две «коробка» 4px x T (T по W-маске, _wall_thickness_pt) у торцов проёма (hinge/jamb2),
    «рамка проёма» между ними (только у внутренних дверей - обе стороны проёма в одной ячейке
    владения floor_cells), «полотно» ink 1.5 от коробки, дуга - пунктир [6,4] ink 1.2 (как
    _doors_svg). Если торец стены не найден (T=None) в маске W (только c08∪c19) - запасной
    источник T: `soft_walls` (shapely-геометрия балконных стенок/рядов остекления, шаг 4 -
    двери НА балкон сидят в этой стенке, а не в W) с фиксированной толщиной `soft_wall_T`. Если
    и там не нашлось - коробки для этой двери не рисуются, дуга и полотно рисуются как есть
    (_doors_svg-эквивалент), это уходит в report["warnings"]. `doors`: список от
    extract_pdf_doors, если вызывающий код уже его посчитал (подстановка балконов читает те же
    hinge/jamb2) - извлекается заново, только если не передан."""
    if doors is None:
        try:
            doors = rp.extract_pdf_doors(fd)
        except Exception as e:
            report.setdefault("warnings", []).append(f"extract_pdf_doors упал: {e}")
            return ""

    box_half_w = (DOOR_BOX_W_PX / K) / 2.0
    box_sw = _stroke_pt(DOOR_BOX_STROKE_PX)
    frame_sw = _stroke_pt(DOOR_FRAME_STROKE_PX)
    frame_shrink = DOOR_FRAME_SHRINK_PX / K
    leaf_w = _stroke_pt(DOOR_LEAF_STROKE_PX)
    arc_w = _stroke_pt(DOOR_ARC_STROKE_PX)
    dash = f"{_stroke_pt(6):.3f},{_stroke_pt(4):.3f}"

    n_boxed, n_no_wall, n_interior_frame = 0, 0, 0
    parts = []
    for d in doors:
        hinge, jamb2, leaf_tip, r = d["hinge"], d["jamb2"], d["leaf_tip"], d["r"]
        gap_len = math.hypot(jamb2[0] - hinge[0], jamb2[1] - hinge[1])
        if gap_len < 1e-6:
            continue
        dir_along = ((jamb2[0] - hinge[0]) / gap_len, (jamb2[1] - hinge[1]) / gap_len)
        dir_perp = (-dir_along[1], dir_along[0])
        mid = ((hinge[0] + jamb2[0]) / 2, (hinge[1] + jamb2[1]) / 2)

        T = _wall_thickness_pt(cellinfo, mid, dir_perp) if cellinfo else None
        via_soft_wall = False
        if (T is None or T <= 0 or T > 60.0) and soft_walls is not None and not soft_walls.is_empty:
            from shapely.geometry import Point as _Point
            if _Point(mid).distance(soft_walls) <= 4.0:
                T = soft_wall_T
                via_soft_wall = True
        if T is None or T <= 0 or T > 60.0:
            n_no_wall += 1
            report.setdefault("trace_doors_no_wall", []).append([round(hinge[0], 1), round(hinge[1], 1)])
        else:
            n_boxed += 1
            if via_soft_wall:
                report.setdefault("trace_doors_via_balcony_band", []).append([round(hinge[0], 1), round(hinge[1], 1)])

            def box_d(center):
                a = (center[0] - dir_along[0] * box_half_w - dir_perp[0] * T / 2,
                     center[1] - dir_along[1] * box_half_w - dir_perp[1] * T / 2)
                b = (center[0] + dir_along[0] * box_half_w - dir_perp[0] * T / 2,
                     center[1] + dir_along[1] * box_half_w - dir_perp[1] * T / 2)
                c = (center[0] + dir_along[0] * box_half_w + dir_perp[0] * T / 2,
                     center[1] + dir_along[1] * box_half_w + dir_perp[1] * T / 2)
                e = (center[0] - dir_along[0] * box_half_w + dir_perp[0] * T / 2,
                     center[1] - dir_along[1] * box_half_w + dir_perp[1] * T / 2)
                pts = " L ".join(f"{x:.2f} {y:.2f}" for x, y in (a, b, c, e))
                return f"M {pts} Z"

            for center in (hinge, jamb2):
                bd = box_d(center)
                parts.append(f'<path d="{bd}" fill="#FFFFFF" stroke="{ink}" stroke-width="{box_sw:.3f}"/>')
                if collect is not None:
                    collect.append(("коробка", bd))

            # «рамка проёма» - только у внутренних дверей: сэмплируем обе стороны проёма поперёк
            # (mid +- (T/2+3) вдоль dir_perp) в ячейках владения floor_cells; один и тот же
            # положительный номер квартиры с обеих сторон -> внутренняя дверь.
            is_interior = False
            if cellinfo:
                off = T / 2 + 3.0
                p1 = (mid[0] + dir_perp[0] * off, mid[1] + dir_perp[1] * off)
                p2 = (mid[0] - dir_perp[0] * off, mid[1] - dir_perp[1] * off)
                l1, l2 = _cell_label_at(cellinfo, p1), _cell_label_at(cellinfo, p2)
                is_interior = l1 > 0 and l1 == l2
            if is_interior:
                n_interior_frame += 1
                fh = T - frame_shrink
                inner_half = max(0.0, gap_len / 2 - box_half_w)
                a = (mid[0] - dir_along[0] * inner_half - dir_perp[0] * fh / 2,
                     mid[1] - dir_along[1] * inner_half - dir_perp[1] * fh / 2)
                b = (mid[0] + dir_along[0] * inner_half - dir_perp[0] * fh / 2,
                     mid[1] + dir_along[1] * inner_half - dir_perp[1] * fh / 2)
                c = (mid[0] + dir_along[0] * inner_half + dir_perp[0] * fh / 2,
                     mid[1] + dir_along[1] * inner_half + dir_perp[1] * fh / 2)
                e = (mid[0] - dir_along[0] * inner_half + dir_perp[0] * fh / 2,
                     mid[1] - dir_along[1] * inner_half + dir_perp[1] * fh / 2)
                pts = " L ".join(f"{x:.2f} {y:.2f}" for x, y in (a, b, c, e))
                frame_d = f"M {pts} Z"
                parts.append(f'<path d="{frame_d}" fill="#FFFFFF" stroke="{ink}" stroke-width="{frame_sw:.3f}"/>')
                if collect is not None:
                    collect.append(("рамка проёма", frame_d))

        # «полотно» - от коробки (hinge) к leaf_tip
        leaf_d = f"M {hinge[0]:.2f} {hinge[1]:.2f} L {leaf_tip[0]:.2f} {leaf_tip[1]:.2f}"
        parts.append(f'<path d="{leaf_d}" '
                      f'stroke="{ink}" stroke-width="{leaf_w:.3f}" stroke-linecap="round" fill="none"/>')
        if collect is not None:
            collect.append(("полотно", leaf_d))
        # дуга - пунктир [6,4]
        a0 = math.atan2(leaf_tip[1] - hinge[1], leaf_tip[0] - hinge[0])
        a1 = math.atan2(jamb2[1] - hinge[1], jamb2[0] - hinge[0])
        delta = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
        segs = 16
        pts = [(hinge[0] + math.cos(a0 + delta * k / segs) * r, hinge[1] + math.sin(a0 + delta * k / segs) * r)
               for k in range(segs + 1)]
        dpath = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in pts)
        parts.append(f'<path d="{dpath}" stroke="{ink}" stroke-width="{arc_w:.3f}" '
                      f'stroke-dasharray="{dash}" stroke-linecap="round" fill="none"/>')
        if collect is not None:
            collect.append(("дуга двери", dpath))

    report["trace_doors_total"] = len(doors)
    report["trace_doors_boxed"] = n_boxed
    report["trace_doors_no_wall_count"] = n_no_wall
    report["trace_doors_interior_frame"] = n_interior_frame
    if n_no_wall:
        report.setdefault("warnings", []).append(
            f"двери: {n_no_wall} из {len(doors)} без найденной толщины стены (T) - коробки не нарисованы, "
            "только дуга+полотно (см. trace_doors_no_wall)")
    return "".join(parts)


def _near_any_edge(rp, bbox, edges, tol):
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    for a, b in edges:
        if rp._point_seg_dist((cx, cy), a, b) <= tol:
            return True
    return False


def _classify_columns_pdf(rp, fd, wall_edges, aspect_tread=1.8, pilon_wall_tol=3.5):
    """load_floor_data()'s own extract_columns() already pulled every small axis-rectangle
    that sits in c06_furn (post split_c06) OUTSIDE every unit/balcony polygon into
    fd["_columns"] - meant, upstream, to be redrawn as solid-ink structural columns fused
    into the wall. On this PDF that bucket is actually two different things by shape alone:
    a run of elongated strips (~18x4pt, some floors ~7x17pt for a perpendicular flight) IS
    the stair tread geometry (the same shapes tried via c06_furn directly, before
    discovering extract_columns had already lifted them out from under that lookup) - kept
    as tread outlines, wherever they sit. The squarish ones (aspect < ~1.8) are real
    columns/pylons ONLY when they actually sit against a c08/c19 wall (rule 9: a pylon is
    always in a wall corner) - a squarish icon glyph floating alone mid-room (fire cabinet,
    wifi mark) fails that test and is dropped; one glued to a wall (icon cabinets are
    usually wall-mounted too) still slips through occasionally, accepted as a minor,
    visually small defect (see report)."""
    treads, pilons = [], []
    for b in fd.get("_columns") or []:
        w, h = b[2] - b[0], b[3] - b[1]
        if w <= 0 or h <= 0:
            continue
        aspect = max(w, h) / min(w, h)
        if aspect >= aspect_tread:
            treads.append(b)
        elif _near_any_edge(rp, b, wall_edges, pilon_wall_tol):
            pilons.append(b)
    return _tread_runs(treads), pilons


def _tread_runs(treads, min_run=4, gap_factor=2.2, along_tol=0.5, len_tol=0.35):
    """A stair flight is a RUN of >=min_run parallel treads of similar length, stacked along
    their short axis with a regular gap; a lone elongated rectangle (a desk, a cabinet icon in
    an office) fails that and is dropped. (the reviewer 21.09: false 'treads' in the floor-2 offices.)"""
    by_orient = {"h": [], "v": []}
    for b in treads:
        w, h = b[2] - b[0], b[3] - b[1]
        o = "h" if w >= h else "v"
        by_orient[o].append(b)
    keep = []
    for o, items in by_orient.items():
        # along = coordinate along the flight (stacking axis), across = centre along the tread
        def key(b):
            return ((b[1] + b[3]) / 2, (b[0] + b[2]) / 2, b[2] - b[0], b[3] - b[1]) if o == "h" else ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, b[3] - b[1], b[2] - b[0])
        # two flights side by side interleave when sorted along the stacking axis - cluster by
        # the across-centre first (one cluster per flight), then look for runs inside each
        items = sorted(items, key=lambda b: key(b)[1])
        clusters, cur = [], []
        for b in items:
            if cur and abs(key(b)[1] - key(cur[-1])[1]) > along_tol * max(key(b)[2], key(cur[-1])[2]):
                clusters.append(cur); cur = []
            cur.append(b)
        if cur:
            clusters.append(cur)
        for cl in clusters:
            cl = sorted(cl, key=lambda b: key(b)[0])
            run = []
            def flush():
                if len(run) >= min_run:
                    keep.extend(run)
                run.clear()
            for b in cl:
                a, c, L, T = key(b)
                if run:
                    a0, c0, L0, T0 = key(run[-1])
                    if a - a0 <= gap_factor * max(T, T0, 1.0) and abs(L - L0) <= len_tol * max(L, L0):
                        run.append(b); continue
                    flush()
                run.append(b)
            flush()
    return keep


def _xpilons_pdf(rp, fd, short_min=3.0, short_max=12.0, long_max=40.0, rect_ratio_min=0.9, corner_tol=0.6):
    """FLOOR-CONTRACT v4, task A (the reviewer 21.09, «несущих местами нет»). This PDF draws an X-pylon
    (structural column embedded in a shared wall) as THREE separate primitives, none of them
    in c19 (the pink structural cluster) or c08: a white-fill rectangle in c06/c06_furn (same
    drawing convention as every other furniture body) plus its two corner-to-corner diagonals
    as their own 2-point line paths in c02 (grey), c03 (dark grey) or c07 (black). Nothing in
    the pipeline paints c06_furn, so today it's simply invisible; worse, when the rectangle
    happens to also be one of the small axis-rects extract_columns() already pulled into
    fd["_columns"], _classify_columns_pdf's own aspect-ratio split (>=1.8 => stair tread)
    misreads its ~1:3.5 shape as a tread, same as a genuine stair-flight rectangle.

    Candidate = a white-fill rectangle (>=4 unique vertices, filled-area/bbox-area >=
    rect_ratio_min) whose short side is short_min..short_max pt and long side <= long_max pt.
    Two sources, deduped by rounded bbox: (a) fd["_columns"] - extract_columns' own bboxes,
    already known-rectangular, ratio treated as 1.0 (no path kept once extracted); (b)
    whatever is still sitting in c06_furn/c06_floor - this is where 202/203's own pylon lives:
    it straddles BOTH apartments' polygons (one corner inside 202, the opposite inside 203),
    so extract_columns' own "every corner outside every room" test never pulls it out.

    Confirmed = exactly one 2-point line matches EACH of the rectangle's two diagonals (both
    endpoints within corner_tol of that diagonal's two corners, either direction) - i.e.
    exactly two confirming segments total, one per diagonal; 0/1 on either diagonal, or >1 on
    either, leaves the candidate unconfirmed (a rectangle alone is not proof - some furniture
    icons are plain squares/rects with no diagonals at all).

    Returns (xpilons, suspicious): both lists of (x0,y0,x1,y1) pt bboxes, floor-local, same
    frame as fd["units"][n]["poly"]. `suspicious` (a strict subset of `xpilons`) flags a
    confirmed pylon whose area exceeds 200pt2 or whose centre sits more than 3pt inside some
    apartment's own polygon (i.e. it reads as living-room furniture, not a wall pylon) - kept
    in the result (still drawn - the diagonal confirmation is real), just called out in the
    report for a human to eyeball."""
    seen, cands = set(), []
    for b in fd.get("_columns") or []:
        x0, y0, x1, y1 = b
        key = (round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))
        if key in seen:
            continue
        seen.add(key)
        cands.append((x0, y0, x1, y1))
    for cid in ("c06_furn", "c06_floor", "c06"):
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths", []):
            bb = rp._path_bbox(p)
            if bb is None:
                continue
            x0, y0, x1, y1 = bb
            key = (round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))
            if key in seen:
                continue
            bw, bh = x1 - x0, y1 - y0
            if bw <= 0 or bh <= 0:
                continue
            pts = rp._path_points(p)
            uniq = rp._unique_vertices(pts)
            if len(uniq) not in (4, 5):
                continue
            area = _polygon_area_local(pts) if len(pts) >= 3 else 0.0
            if area / (bw * bh) < rect_ratio_min:
                continue
            seen.add(key)
            cands.append((x0, y0, x1, y1))

    diagonals = []
    for cid in ("c02", "c03", "c07"):
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths", []):
            pts = path_points(p)
            if len(pts) == 2:
                diagonals.append(pts)

    def _match(corner_a, corner_b):
        hits = 0
        for (sx0, sy0), (sx1, sy1) in diagonals:
            fwd = math.hypot(sx0 - corner_a[0], sy0 - corner_a[1]) <= corner_tol and math.hypot(sx1 - corner_b[0], sy1 - corner_b[1]) <= corner_tol
            rev = math.hypot(sx0 - corner_b[0], sy0 - corner_b[1]) <= corner_tol and math.hypot(sx1 - corner_a[0], sy1 - corner_a[1]) <= corner_tol
            if fwd or rev:
                hits += 1
        return hits

    xpilons, suspicious = [], []
    for x0, y0, x1, y1 in cands:
        w, h = x1 - x0, y1 - y0
        short, long_ = min(w, h), max(w, h)
        if not (short_min <= short <= short_max and long_ <= long_max):
            continue
        diag0 = _match((x0, y0), (x1, y1))
        diag1 = _match((x0, y1), (x1, y0))
        if diag0 != 1 or diag1 != 1:
            continue
        xpilons.append((x0, y0, x1, y1))
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        deep = any(point_in_poly((cx, cy), u["poly"]) and _poly_dist((cx, cy), u["poly"]) > 3.0
                   for u in fd["units"].values())
        if (w * h) > 200.0 or deep:
            suspicious.append((x0, y0, x1, y1))
    return xpilons, suspicious


def _polygon_area_local(pts):
    """Shoelace area, |.| - same formula render-plans.py's own _polygon_area uses, kept local
    so _xpilons_pdf doesn't depend on that module's private helper staying importable."""
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x0, y0 = pts[i]; x1, y1 = pts[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


def _long_thin_rect(rp, p, len_min=15.0, len_max=100.0, thick_max=6.0, aspect_min=2.5):
    pts = rp._path_points(p)
    uniq = rp._unique_vertices(pts)
    if len(uniq) not in (4, 5):
        return None
    b = rp._path_bbox(p)
    if b is None:
        return None
    w, h = b[2] - b[0], b[3] - b[1]
    long_, short_ = max(w, h), min(w, h)
    if not (len_min <= long_ <= len_max) or short_ <= 0 or short_ > thick_max or long_ / short_ < aspect_min:
        return None
    return b, (w >= h)


def _detect_handrails_pdf(rp, fd):
    """c26 (thin grey stroke) is mostly dimension/marker noise (task: not drawn), except for
    the long narrow rectangles that run a stair flight's full length - real handrails, kept
    by shape alone (long, narrow, axis-aligned)."""
    c = next((x for x in fd["clusters"] if x["id"] == "c26"), None)
    if not c or not c.get("paths"):
        return []
    out = []
    for p in c["paths"]:
        r = _long_thin_rect(rp, p)
        if r:
            out.append(r)
    return out


def _wall_edges_pdf(fd, cids=("c08", "c19")):
    edges = []
    for cid in cids:
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        if not c:
            continue
        for p in c.get("paths", []):
            pts = path_points(p)
            n = len(pts)
            for i in range(n):
                edges.append((pts[i], pts[(i + 1) % n]))
    return edges


def _cast_void(px, py, edges, pad=0.5):
    """Ray-cast a point out in +-x/+-y against rectilinear wall edges -> the bbox of the
    void (room/shaft) that contains it, found straight from the PDF's own wall fill."""
    xr = xl = yd = yu = None
    for a, b in edges:
        ax, ay = a; bx, by = b
        if abs(ax - bx) < 0.6 and abs(ay - by) > 0.6:      # near-vertical edge
            x = (ax + bx) / 2.0
            y0, y1 = (ay, by) if ay < by else (by, ay)
            if y0 - pad <= py <= y1 + pad:
                if x > px and (xr is None or x < xr):
                    xr = x
                if x < px and (xl is None or x > xl):
                    xl = x
        if abs(ay - by) < 0.6 and abs(ax - bx) > 0.6:      # near-horizontal edge
            y = (ay + by) / 2.0
            x0, x1 = (ax, bx) if ax < bx else (bx, ax)
            if x0 - pad <= px <= x1 + pad:
                if y > py and (yd is None or y < yd):
                    yd = y
                if y < py and (yu is None or y > yu):
                    yu = y
    return xl, yu, xr, yd


def _detect_lift_cells_pdf(rp, fd):
    """The 2 lift cars' own PDF cross/hatch mark (found in c14) turned out to be one big
    annotation fan spanning the WHOLE shaft (both cars + the gap between them, no break at
    any grouping gap down to 0.05pt) rather than 2 per-car crosses, and c14 is fully dropped
    by filter_wall_fills anyway (not a wall/column rectangle) - so it can't be reused as-is.
    Instead: find each car's own capacity label ('140X240' etc, always present next to a
    'KG' text in this PDF), ray-cast it against the c08/c19 wall fill to the shaft void's own
    bbox, then split that void evenly among however many labels share it (by whichever axis
    the labels spread over) - one cell, one clean corner-to-corner cross, per car."""
    labels = [t for t in fd.get("texts", []) if _RE_LIFT_LABEL.match(str(t.get("str", "")).strip())]
    if not labels:
        return []
    edges = _wall_edges_pdf(fd)
    cast = []
    for t in labels:
        xl, yu, xr, yd = _cast_void(t["x"], t["y"], edges)
        if None in (xl, yu, xr, yd) or xr <= xl or yd <= yu:
            continue
        cast.append((t, (xl, yu, xr, yd)))
    if not cast:
        return []
    # 2 cars sharing one shaft ray-cast to slightly different boxes (a few pt off, from a
    # local notch in the wall fill) rather than an identical box, so group by bbox OVERLAP
    # (not an exact-rounded key) and use the group's INTERSECTION - the largest box every
    # member agrees is void - as that shaft's true cell.
    groups = rp._union_find_groups([b for _, b in cast], gap=-8.0)
    cells = []
    for members in groups.values():
        group = [cast[i] for i in members]
        xl = max(b[0] for _, b in group); yu = max(b[1] for _, b in group)
        xr = min(b[2] for _, b in group); yd = min(b[3] for _, b in group)
        if xr <= xl or yd <= yu:
            continue
        members_t = [t for t, _ in group]
        n = len(members_t)
        xs = [m["x"] for m in members_t]; ys = [m["y"] for m in members_t]
        vertical = (max(ys) - min(ys)) >= (max(xs) - min(xs))
        if vertical:
            step = (yd - yu) / n
            cells += [(xl, yu + i * step, xr, yu + (i + 1) * step) for i in range(n)]
        else:
            step = (xr - xl) / n
            cells += [(xl + i * step, yu, xl + (i + 1) * step, yd) for i in range(n)]
    return cells


def _lift_cross_svg(bbox, ink, margin=1.8):
    x0, y0, x1, y1 = bbox
    x0m, y0m, x1m, y1m = x0 + margin, y0 + margin, x1 - margin, y1 - margin
    if x1m <= x0m or y1m <= y0m:
        return ""
    ow, cw = _stroke_pt(2.0), _stroke_pt(1.2)
    return (f'<rect x="{x0m:.2f}" y="{y0m:.2f}" width="{x1m - x0m:.2f}" height="{y1m - y0m:.2f}" '
            f'fill="#FFFFFF" stroke="{ink}" stroke-width="{ow:.3f}"/>'
            f'<path d="M {x0m:.2f} {y0m:.2f} L {x1m:.2f} {y1m:.2f} M {x0m:.2f} {y1m:.2f} L {x1m:.2f} {y0m:.2f}" '
            f'stroke="{ink}" stroke-width="{cw:.3f}" fill="none"/>')


def _core_furniture_pdf(rp, fd, style):
    """Stair treads, non-X columns/pylons (fd["_columns"], split by aspect ratio - see
    _classify_columns_pdf), a handrail (shape-detected out of c26, see _detect_handrails_pdf)
    and the 2 synthesized lift crosses (see _detect_lift_cells_pdf) - straight from the PDF
    vectors, no doors/c21/c08/c19 (those are their own layers). Factored out of underlay_pdf
    so the --source pdf build's own unclipped "ядро" layer (FLOOR-CONTRACT task 2026-09-21)
    can reuse the exact same detection instead of duplicating it."""
    ink = style["ink"]
    muted = style.get("muted_ink", "#9AA3AD")
    parts = []
    for bbox in _detect_lift_cells_pdf(rp, fd):
        parts.append(_lift_cross_svg(bbox, ink))
    wall_edges = _wall_edges_pdf(fd)
    treads, pilons = _classify_columns_pdf(rp, fd, wall_edges)
    tw = _stroke_pt(1.2)
    for x0, y0, x1, y1 in treads:
        parts.append(f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}" '
                      f'fill="none" stroke="{muted}" stroke-width="{tw:.3f}"/>')
    for x0, y0, x1, y1 in pilons:
        parts.append(f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}" fill="{ink}" stroke="none"/>')
    hw = _stroke_pt(1.5)
    for (x0, y0, x1, y1), vertical in _detect_handrails_pdf(rp, fd):
        if vertical:
            cx = (x0 + x1) / 2
            d = f"M {cx:.2f} {y0:.2f} L {cx:.2f} {y1:.2f}"
        else:
            cy = (y0 + y1) / 2
            d = f"M {x0:.2f} {cy:.2f} L {x1:.2f} {cy:.2f}"
        parts.append(f'<path d="{d}" stroke="{ink}" stroke-width="{hw:.3f}" stroke-linecap="round" fill="none"/>')
    return "".join(parts)


def underlay_pdf(rp, fd, style, include_walls=True, include_mullions=True):
    """--source finals underlay, built straight from the PDF vectors (fd["clusters"]) instead
    of the v15 raster-adjacent renderer. Draws ONLY: c08/c19 wall fill (ink) - unless
    `include_walls` is False (FLOOR-CONTRACT v4: c08/c19 move to the unclipped `structure`
    layer, so this clipped underlay must not draw them a second time), c00 doors (dashed arc +
    leaf), c21 glazing mullions (ink; c24 glass stays the paper white already behind it), and
    the rest of the core furniture (_core_furniture_pdf: stair treads, columns/pylons,
    handrail, lift crosses). Every other cluster (c02/c03/c05/c07/c12/c14 included) is real PDF
    content but, on floor 2 and floor 4, is either pure dimension/icon/hatch noise, or - for
    c12/c14 specifically - verified EMPTY once the caller's own load_floor_data() cleanup
    (split_c06, filter_furniture_paths, filter_wall_fills) has already run, so it is left
    undrawn."""
    ink = style["ink"]
    parts = []
    if include_walls:
        parts += [_fill_cluster_svg(fd, "c08", ink), _fill_cluster_svg(fd, "c19", ink)]
    parts.append(_doors_svg(rp, fd, ink))
    if include_mullions:
        parts.append(_fill_cluster_svg(fd, "c21", ink))
    parts.append(_core_furniture_pdf(rp, fd, style))
    return "".join(parts)


WINDOW_GLASS_STROKE_PX = 0.8   # --source pdf task 2026-09-21: thin ink outline around each c24
                               # glass-pane strip, so an exterior-wall opening (already a plain
                               # gap in c08 - see _windows_svg_pdf) reads as a window, not a hole


def _windows_svg_pdf(fd, ink):
    """--source pdf layer 6 «Окна»: the window OPENING itself is already a hole in c08 (this
    PDF's wall fill polygon is pre-cut at every window - nothing to punch here); what's missing
    without the Figma finals is the two marks that read "window" rather than "gap in the wall":
    the mullion/stile posts (c21, same ink fill as underlay_pdf/_core_furniture_pdf's callers
    already draw) and a thin glazing line across the opening. c24 is this PDF's own glass-pane
    strip for exactly that line - drawn here with a white fill (so it reads as the empty pane,
    matching the paper behind every other wall gap) and a WINDOW_GLASS_STROKE_PX ink outline
    (thin, not a wall-weight stroke) tracing its rectangle. No c24 on a floor -> stiles only,
    same as underlay_pdf's own comment always said ("c24 glass stays the paper white already
    behind it")."""
    parts = [_fill_cluster_svg(fd, "c21", ink)]
    c24 = next((x for x in fd["clusters"] if x["id"] == "c24"), None)
    sw = _stroke_pt(WINDOW_GLASS_STROKE_PX)
    for p in (c24.get("paths") if c24 else []) or []:
        parts.append(f'<path d="{p}" fill="#FFFFFF" stroke="{ink}" stroke-width="{sw:.4f}"/>')
    return "".join(parts)


WINDOW_AREA_MIN_PT2 = 20.0   # FLOOR-TRACE-SPEC.md правило 2: белый полигон c06 в проёме стены
WINDOW_AREA_MAX_PT2 = 400.0
WINDOW_RECT_RATIO_MIN = 0.85
WINDOW_MIN_SIDE_PT = 4.0      # отсекает вырожденные линии-размерные засечки (bbox 1-2 pt толщиной)
WINDOW_WALL_TOUCH_TOL_PT = 0.6   # контакт со стеной с ДВУХ ПРОТИВОПОЛОЖНЫХ сторон bbox - проверено
                                  # на этаже 21 по кропам PDF (санузел ~85.9-111.1x108.4-128.1pt,
                                  # окно ~369.8-379.9x99.2-120.8pt): с этим допуском и требованием
                                  # именно противоположных сторон находит настоящие окна и не
                                  # хватает мебель/сантехнику (у которой либо 0-1 сторона касается
                                  # стены, либо ratio/площадь вне диапазона)
WINDOW_KLIN_MATCH_TOL_PT = 1.5   # «пара полигонов одного bbox» (правило 17) - bbox клина и bbox
                                  # белого окна совпадают с точностью до этого допуска на сторону
WINDOW_ICON_EXCLUDE_TOL_PT = 2.0   # спека: «c22/c18 красные квадраты 7×7 — иконки, НЕ рисовать».
                                    # На этаже 21 c12 («клин окна» по конвенции пайплайна финалов)
                                    # рисует НЕ только клинья: подтверждено кропом PDF (105,73) -
                                    # это иконка wifi-роутера, обведённая красной рамкой c22 точно
                                    # по тому же bbox, что и «клин»-кандидат. Кандидат с белым c06
                                    # bbox, совпадающим (в пределах этого допуска) с bbox c22/c18,
                                    # исключается из поиска окон целиком (и как клин-пара, и как
                                    # обычное окно) - это иконка, а не окно.


def _window_klin_groups_pdf(rp, fd, gap=3.0):
    """c12 («клин окна», FLOOR-TRACE-SPEC.md: fill #000000 → клин окна) в этом PDF - не один
    путь на окно, а компактный кластер из нескольких вложенных путей (сам клин + 1-2 внутренние
    отметки), сгруппированных геометрически как один символ. Группировка через
    rp._union_find_groups (тот же приём, что _detect_lift_cells_pdf/_xpilons_pdf используют для
    родственных фрагментов одного символа) по общему bbox с зазором `gap` pt. Возвращает список
    (bbox, d_combined) - d_combined = все пути группы через пробел, единым <path
    fill-rule="nonzero">, что и воспроизводит исходный треугольный значок клина."""
    c12 = next((x for x in fd["clusters"] if x["id"] == "c12"), None)
    paths = (c12.get("paths") if c12 else []) or []
    bboxes = [rp._path_bbox(p) for p in paths]
    idx = [i for i, b in enumerate(bboxes) if b is not None]
    groups = rp._union_find_groups([bboxes[i] for i in idx], gap=gap)
    out = []
    for members in groups.values():
        real_idx = [idx[m] for m in members]
        mb = [bboxes[i] for i in real_idx]
        bbox = (min(b[0] for b in mb), min(b[1] for b in mb), max(b[2] for b in mb), max(b[3] for b in mb))
        d_combined = " ".join(paths[i] for i in real_idx)
        out.append((bbox, d_combined))
    return out


def _windows_svg_trace(rp, fd, ink, report, collect=None):
    """FLOOR-TRACE-SPEC.md правило 2 «Окна с клином»: белый полигон c06 (здесь: c06_furn -
    c06_floor остаётся заливкой пола комнат, площадь >=1500pt2, окна на 1-2 порядка меньше) в
    проёме стены.

    Найдено кропами исходного PDF (этаж 21, см. отчёт): c12 («клин окна» по конвенции пайплайна
    финалов) на этом этаже рисует не только оконный клин, но и как минимум иконку wifi-роутера -
    подтверждено совпадением bbox с красной рамкой-иконкой c22. Поэтому ЛЮБОЙ кандидат (клин ИЛИ
    белый c06), чей bbox совпадает с c22/c18 (WINDOW_ICON_EXCLUDE_TOL_PT), исключается целиком -
    это иконка, «НЕ рисовать» по спеке кластеров.

    Два уровня уверенности (после исключения иконок):
      A) «пара полигонов одного bbox» (правило 17, дословно) - белый c06_furn прямоугольник,
         чей bbox совпадает (WINDOW_KLIN_MATCH_TOL_PT на сторону) с bbox кластера клина c12
         (_window_klin_groups_pdf) - окно с клином поверх.
      B) без клина: белый c06_furn прямоугольник (rect_ratio>=0.85, площадь 20-400pt2, обе
         стороны bbox >=4pt - отсекает линии-засечки) в контакте со стеной (WALL_TOUCH_TOL_PT) с
         ДВУХ ПРОТИВОПОЛОЖНЫХ сторон bbox - белая заливка окно + обводка ink 0.8 px, без клина.
         Проверено кропами PDF: находит настоящие окна (санузел ~85.9-111.1×108.4-128.1pt - белая
         вставка в серой полосе стены) и не хватает мебель/сантехнику.

    Возвращает SVG-строку (в pt, для обёртки <g transform="scale(K)">) и пишет счётчики в
    report["trace_windows_klin"] / report["trace_windows_no_klin"]."""
    c06f = next((x for x in fd["clusters"] if x["id"] == "c06_furn"), None)
    paths = (c06f.get("paths") if c06f else []) or []
    wall_edges = _wall_edges_pdf(fd)
    klin_groups = _window_klin_groups_pdf(rp, fd)

    icon_bboxes = []
    for cid in ("c22", "c18"):
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        for ip in (c.get("paths") if c else []) or []:
            ib = rp._path_bbox(ip)
            if ib is not None:
                icon_bboxes.append(ib)

    def is_icon(bbox, tol=WINDOW_ICON_EXCLUDE_TOL_PT):
        for ib in icon_bboxes:
            if all(abs(bbox[k] - ib[k]) <= tol for k in range(4)):
                return True
        return False

    klin_groups = [(kb, d) for kb, d in klin_groups if not is_icon(kb)]
    klin_used = [False] * len(klin_groups)

    def wall_dist(pt):
        best = 1e9
        for a, b in wall_edges:
            dd = rp._point_seg_dist(pt, a, b)
            if dd < best:
                best = dd
        return best

    def side_touch(bbox, tol):
        x0, y0, x1, y1 = bbox
        dl = (wall_dist((x0, y0)) + wall_dist((x0, y1))) / 2
        dr = (wall_dist((x1, y0)) + wall_dist((x1, y1))) / 2
        dt = (wall_dist((x0, y0)) + wall_dist((x1, y0))) / 2
        db = (wall_dist((x0, y1)) + wall_dist((x1, y1))) / 2
        return dl <= tol, dr <= tol, dt <= tol, db <= tol

    sw = _stroke_pt(0.8)
    win_klin, win_plain, win_icon_skipped = [], [], []
    parts = []
    for p in paths:
        bbox = rp._path_bbox(p)
        if bbox is None:
            continue
        x0, y0, x1, y1 = bbox
        bw, bh = x1 - x0, y1 - y0
        if bw <= 0 or bh <= 0 or min(bw, bh) < WINDOW_MIN_SIDE_PT:
            continue
        pts = rp._path_points(p)
        uniq = rp._unique_vertices(pts)
        if len(uniq) not in (4, 5):
            continue
        area = _polygon_area_local(pts) if len(pts) >= 3 else 0.0
        if not (WINDOW_AREA_MIN_PT2 <= area <= WINDOW_AREA_MAX_PT2):
            continue
        ratio = area / (bw * bh) if (bw * bh) > 0 else 0.0
        if ratio < WINDOW_RECT_RATIO_MIN:
            continue
        if is_icon(bbox):
            win_icon_skipped.append(bbox)
            continue

        klin_i = None
        for i, (kbb, _) in enumerate(klin_groups):
            if klin_used[i]:
                continue
            if all(abs(bbox[k] - kbb[k]) <= WINDOW_KLIN_MATCH_TOL_PT for k in range(4)):
                klin_i = i
                break

        if klin_i is None:
            # rule 2's own contact test (no klin to lean on): two OPPOSITE sides of the bbox
            # touch the wall - see WINDOW_WALL_TOUCH_TOL_PT for the crop-verified tolerance.
            l, r, t, b = side_touch(bbox, WINDOW_WALL_TOUCH_TOL_PT)
            if not ((l and r) or (t and b)):
                continue

        parts.append(f'<path d="{p}" fill="#FFFFFF" stroke="{ink}" stroke-width="{sw:.4f}"/>')
        if collect is not None:
            collect.append(("окно", p))
        if klin_i is not None:
            klin_used[klin_i] = True
            parts.append(f'<path d="{klin_groups[klin_i][1]}" fill="{ink}" fill-rule="nonzero"/>')
            if collect is not None:
                collect.append(("клин окна", klin_groups[klin_i][1]))
            win_klin.append(bbox)
        else:
            win_plain.append(bbox)

    unmatched_klin = [kb for i, (kb, _) in enumerate(klin_groups) if not klin_used[i]]
    report["trace_window_rects_pt"] = win_klin + win_plain   # для _facade_window_gaps_pdf: не дублировать
    report["trace_windows_icon_skipped"] = len(win_icon_skipped)
    report["trace_windows_klin"] = len(win_klin)
    report["trace_windows_no_klin"] = len(win_plain)
    report["trace_klin_groups_total"] = len(klin_groups)
    report["trace_klin_unmatched"] = unmatched_klin
    if unmatched_klin:
        report.setdefault("warnings", []).append(
            f"окна: {len(unmatched_klin)} кластеров клина c12 без парного белого bbox (правило 17 не сработало) - "
            + "; ".join(f"[{b[0]:.1f},{b[1]:.1f},{b[2]:.1f},{b[3]:.1f}]" for b in unmatched_klin))
    return "".join(parts)


BALCONY_BAND_WALL_OVERLAP_PT = 0.3   # iteration 2 (the reviewer 2026-09-21, gap-check sliver review):
                                      # the band's outer edge must OVERLAP the adjoining c08/c19
                                      # wall by ~2px, not just touch it - antialiasing on two
                                      # merely-coincident fills leaves a light 1px seam. Folded
                                      # into the unary_union with structure (build_floor_pdf) so
                                      # there's no seam by construction, not by a stroke hack.


def _balcony_wall_band(balcony, poly, band_pt):
    """--source pdf «Балконные стенки»: a band_pt-wide ink strip running INSIDE the balcony
    polygon along every edge EXCEPT the one(s) shared with the apartment footprint (the
    threshold/door - within 0.8pt of `poly`, per FLOOR-CONTRACT task 2026-09-21). The band is
    intersected against `bp.buffer(BALCONY_BAND_WALL_OVERLAP_PT)`, not `bp` itself, so its outer
    edge pokes BALCONY_BAND_WALL_OVERLAP_PT past the balcony's own true boundary into whatever
    wall sits there - it is folded into the same unary_union as c08/c19/X-pylons (see
    build_floor_pdf's structure layer), so this overlap disappears into the interior of the
    unioned shape instead of leaving a seam. Returns (band_polys, band_area, balcony_area) so
    the caller can both draw it and self-check band_area/balcony_area < 0.4 (a correct
    implementation only ever bands the balcony's own parapet, never fills the whole balcony
    solid) - `balcony_area` stays the TRUE (unbuffered) balcony polygon area throughout."""
    from shapely.geometry import Polygon, LineString
    from shapely.ops import unary_union

    bp = Polygon(balcony).buffer(0)
    ap = Polygon(poly).buffer(0)
    n = len(balcony)
    segs = []
    for i in range(n):
        a, b = balcony[i], balcony[(i + 1) % n]
        seg = LineString([a, b])
        if seg.length < 1e-6 or seg.distance(ap) < 0.8:
            continue   # threshold/door edge shared with the apartment - not a wall
        segs.append(seg)
    if not segs or bp.is_empty:
        return [], 0.0, bp.area
    band = unary_union([seg.buffer(band_pt, cap_style=2, join_style=2) for seg in segs]).intersection(
        bp.buffer(BALCONY_BAND_WALL_OVERLAP_PT))
    if band.is_empty:
        return [], 0.0, bp.area
    band_polys = list(band.geoms) if band.geom_type == "MultiPolygon" else [band]
    band_polys = [g for g in band_polys if not g.is_empty and g.geom_type == "Polygon"]
    band_area = sum(g.area for g in band_polys)
    return band_polys, band_area, bp.area


def _balcony_wall_band_segments(balcony, poly, band_pt):
    """--figma-svg (the reviewer, 2026-09-21): та же геометрия, что _balcony_wall_band, но БЕЗ финального
    unary_union across edges - координатор хочет «по одному пути на ребро», а _balcony_wall_band
    сознательно объединяет соседние рёбер-полосы в один (или несколько) кусков ради чистого PNG-
    рендера/растровых барьеров. Возвращает список shapely Polygon, один на небандированное
    ребро балкона (те же исключения: рёбра короче 1e-6 или в пределах 0.8pt от `poly` - порог -
    пропущены, как в _balcony_wall_band)."""
    from shapely.geometry import Polygon, LineString

    bp = Polygon(balcony).buffer(0)
    ap = Polygon(poly).buffer(0)
    if bp.is_empty:
        return []
    n = len(balcony)
    out = []
    for i in range(n):
        a, b = balcony[i], balcony[(i + 1) % n]
        seg = LineString([a, b])
        if seg.length < 1e-6 or seg.distance(ap) < 0.8:
            continue
        band = seg.buffer(band_pt, cap_style=2, join_style=2).intersection(bp.buffer(BALCONY_BAND_WALL_OVERLAP_PT))
        if band.is_empty:
            continue
        pieces = list(band.geoms) if band.geom_type == "MultiPolygon" else [band]
        out.extend(g for g in pieces if not g.is_empty and g.geom_type == "Polygon")
    return out


GLAZING_ROW_GAP_MAX_PT = 120.0   # FLOOR-TRACE-SPEC.md правило 4: ряд = >=2 стойки на одной
                                  # линии с шагом <= этого
GLAZING_ROW_SHARED_TOL_PT = 2.0  # допуск «на одной линии» (общая координата ряда)
GLAZING_ROW_MIN_RUN = 2


def _balcony_glazing_rows_pdf(rp, fd, band_pt):
    """FLOOR-TRACE-SPEC.md правило 4, «дополнительно»: ряды стоек c21 (+ стекло c24, тот же
    геометрический ряд) ВНЕ полигонов балконов - наружное остекление/разделители между
    балконами, не парапет самого балкона (тот уже покрыт _balcony_wall_band). Стойка (bbox
    4-6.5 x 12-17pt) классифицируется как «вертикальная» (стоит в столбик, стойки делят wide
    across y, общая x) или «горизонтальная» (общая y) по тому, какая сторона bbox длиннее;
    группируется по общей координате (GLAZING_ROW_SHARED_TOL_PT) и разбивается на пробеги с
    шагом <= GLAZING_ROW_GAP_MAX_PT; пробег короче GLAZING_ROW_MIN_RUN штук (одиночная стойка -
    «мишура», ревьюер) отбрасывается. Возвращает список shapely-полигонов band_pt-шириной,
    центрированных на общей координате ряда, от первой до последней стойки пробега."""
    from shapely.geometry import box as _box

    balcony_polys = [u["balcony"] for u in fd["units"].values() if u.get("balcony")]
    bboxes = []
    for cid in ("c21", "c24"):
        c = next((x for x in fd["clusters"] if x["id"] == cid), None)
        for p in (c.get("paths") if c else []) or []:
            bb = rp._path_bbox(p)
            if bb is None:
                continue
            cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            if any(point_in_poly((cx, cy), bal) for bal in balcony_polys):
                continue   # внутри балкона - парапет, уже в _balcony_wall_band
            bboxes.append(bb)

    vert, horiz = [], []
    for bb in bboxes:
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        (vert if h >= w else horiz).append(bb)

    def runs(items, shared, pos):
        groups = {}
        for bb in items:
            k = round(shared(bb) / GLAZING_ROW_SHARED_TOL_PT) * GLAZING_ROW_SHARED_TOL_PT
            groups.setdefault(k, []).append(bb)
        out = []
        for mem in groups.values():
            mem = sorted(mem, key=pos)
            run = []
            def flush():
                if len(run) >= GLAZING_ROW_MIN_RUN:
                    out.append(list(run))
                run.clear()
            for bb in mem:
                if run and pos(bb) - pos(run[-1]) > GLAZING_ROW_GAP_MAX_PT:
                    flush()
                run.append(bb)
            flush()
        return out

    row_groups = runs(vert, shared=lambda b: (b[0] + b[2]) / 2, pos=lambda b: (b[1] + b[3]) / 2)
    row_groups += runs(horiz, shared=lambda b: (b[1] + b[3]) / 2, pos=lambda b: (b[0] + b[2]) / 2)

    bands = []
    for row in row_groups:
        w_avg = sum(b[2] - b[0] for b in row) / len(row)
        h_avg = sum(b[3] - b[1] for b in row) / len(row)
        vertical = h_avg >= w_avg
        if vertical:
            xc = sum((b[0] + b[2]) / 2 for b in row) / len(row)
            y0 = min(b[1] for b in row); y1 = max(b[3] for b in row)
            bands.append(_box(xc - band_pt / 2, y0, xc + band_pt / 2, y1))
        else:
            yc = sum((b[1] + b[3]) / 2 for b in row) / len(row)
            x0 = min(b[0] for b in row); x1 = max(b[2] for b in row)
            bands.append(_box(x0, yc - band_pt / 2, x1, yc + band_pt / 2))
    return bands


# ---- facade window-gap rule (итерация 2026-09-21 вечер, координатор: окна на этаже 21 - НЕ
# белые полигоны c06 + клин c12 повсюду, а линии c01/c20/c29/c24/c21; правило 1: «проём наружной
# стены = окно», детерминированно, без чтения символов, см. коммит-сообщение).
FACADE_HULL_BUFFER_PT = 4.0        # смыкает мелкие зазоры между poly соседних квартир в один контур
FACADE_WALL_CHECK_TOL_PT = 5.0     # «в полосе толщиной стены (+-3pt от контура) нет ink стен» -
                                    # координатор дал 3pt, но контур = union(...).buffer(4pt)
                                    # (FACADE_HULL_BUFFER_PT), а балконная стенка сидит своим
                                    # band_pt (~1.4pt) У ИСТИННОЙ границы балкона, т.е. на 3.7-4pt
                                    # ОТ контура - с допуском 3pt почти весь прямой пробег вдоль
                                    # забандированного балкона (не только углы) читался как «нет
                                    # стены», см. отчёт агента (этаж 21, верх/право фасада, пример
                                    # coords: (140.0,6.5) dist=5.45, (557.0,6.0) dist=5.45). 5pt -
                                    # не эвристика поверх правила, а согласование этого же допуска
                                    # с уже заданным в правиле буфером контура (4pt) + запас
FACADE_DOOR_EXCLUDE_PX = 20.0      # «нет дуги двери c00 в радиусе 20 px»
FACADE_GAP_MIN_PT = 30.0 / K       # 30-400 px -> pt (K=7.05): ~4.3-56.7 pt
FACADE_GAP_MAX_PT = 400.0 / K
FACADE_SAMPLE_STEP_PT = 1.0
FACADE_WALL_T_FALLBACK_PT = 3.2
FACADE_KLIN_AREA_MIN_PT2 = 2.0
FACADE_KLIN_AREA_MAX_PT2 = 60.0
FACADE_EXCLUDE_MATCH_TOL_PT = 2.0   # не дублировать проёмы, уже нарисованные _windows_svg_trace


def _facade_window_gaps_pdf(rp, fd, cellinfo, walls_and_bands_geom, ink, report, exclude_rects=None, collect=None):
    """Правило 1 (координатор, 2026-09-21 вечер): наружная граница здания = контур
    unary_union(все units[n].poly ∪ balcony), буфер FACADE_HULL_BUFFER_PT (join_style=mitre -
    буфер с круглыми стыками превращает каждый прямой угол в веер из ~10 микро-сегментов,
    непригодных для покраевого прохода). Вдоль контура (по рёбрам многоугольника, сэмплы с шагом
    FACADE_SAMPLE_STEP_PT) ищем пробеги, где точка НЕ в пределах FACADE_WALL_CHECK_TOL_PT от
    walls_and_bands_geom (стены + балконные стенки, БЕЗ рядов остекления - те не «стена») И не
    в пределах FACADE_DOOR_EXCLUDE_PX/K pt от жамба любой двери (extract_pdf_doors) - такой
    пробег длиной FACADE_GAP_MIN_PT..FACADE_GAP_MAX_PT = окно.

    Прямоугольник окна: по пробегу (длина) x T (толщина стены по W-маске в середине пробега,
    _wall_thickness_pt с перпендикуляром = внешняя нормаль контура в этой точке; fallback
    FACADE_WALL_T_FALLBACK_PT), центрирован на самом контуре (T/2 внутрь + T/2 наружу). Если
    внутри прямоугольника лежит полигон c12 площадью FACADE_KLIN_AREA_MIN_PT2..MAX_PT2 - клин
    (правило 17), иначе без клина. Стойки c21, центр которых попал в прямоугольник, - ink-блоки
    5xT (правило 2 координатора, п.2); остальные (вне проёмов) не рисуются.

    `exclude_rects` (список bbox из _windows_svg_trace) - проёмы, чей midpoint уже попал в один
    из этих bbox (+ допуск), пропускаются - не дублировать окно, уже найденное c06+клин
    детектором (санузлы: там резерв c06 сработал точно, см. предыдущую итерацию)."""
    from shapely.geometry import Polygon, Point, LinearRing
    from shapely.ops import unary_union

    polys = []
    for u in fd["units"].values():
        polys.append(Polygon(u["poly"]).buffer(0))
        if u.get("balcony"):
            polys.append(Polygon(u["balcony"]).buffer(0))
    if not polys:
        return ""
    hull = unary_union(polys).buffer(FACADE_HULL_BUFFER_PT, join_style=2, mitre_limit=5.0)
    if hull.geom_type == "MultiPolygon":
        hull = max(hull.geoms, key=lambda g: g.area)
    ring = hull.exterior.simplify(0.3, preserve_topology=True)
    coords = list(ring.coords)

    try:
        doors = rp.extract_pdf_doors(fd)
    except Exception:
        doors = []
    door_pts = [d["hinge"] for d in doors] + [d["jamb2"] for d in doors]
    door_tol = FACADE_DOOR_EXCLUDE_PX / K

    c12 = next((x for x in fd["clusters"] if x["id"] == "c12"), None)
    c12_items = []
    for p in (c12.get("paths") if c12 else []) or []:
        pts = path_points(p)
        if len(pts) < 3:
            continue
        area = _polygon_area_local(pts)
        if FACADE_KLIN_AREA_MIN_PT2 <= area <= FACADE_KLIN_AREA_MAX_PT2:
            c12_items.append((Polygon(pts).buffer(0), p))

    c21 = next((x for x in fd["clusters"] if x["id"] == "c21"), None)
    c21_items = []
    for p in (c21.get("paths") if c21 else []) or []:
        bb = rp._path_bbox(p)
        if bb:
            c21_items.append(((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2, p))

    exclude_rects = exclude_rects or []

    def excluded(mid):
        for x0, y0, x1, y1 in exclude_rects:
            if (x0 - FACADE_EXCLUDE_MATCH_TOL_PT <= mid[0] <= x1 + FACADE_EXCLUDE_MATCH_TOL_PT
                    and y0 - FACADE_EXCLUDE_MATCH_TOL_PT <= mid[1] <= y1 + FACADE_EXCLUDE_MATCH_TOL_PT):
                return True
        return False

    ink_sw = _stroke_pt(0.8)
    parts = []
    segments = []
    n_klin = n_plain = n_with_stoiki = 0

    for i in range(len(coords) - 1):
        ax, ay = coords[i]; bx, by = coords[i + 1]
        L = math.hypot(bx - ax, by - ay)
        if L < 1e-6:
            continue
        dirv = ((bx - ax) / L, (by - ay) / L)
        normal = (dirv[1], -dirv[0])
        mid_edge = ((ax + bx) / 2 + normal[0] * 1.0, (ay + by) / 2 + normal[1] * 1.0)
        if hull.contains(Point(mid_edge)):
            normal = (-normal[0], -normal[1])   # flip so `normal` points OUTWARD

        n_samples = max(1, int(L / FACADE_SAMPLE_STEP_PT))
        flags = []
        for k in range(n_samples + 1):
            t = min(k * FACADE_SAMPLE_STEP_PT, L)
            p = (ax + dirv[0] * t, ay + dirv[1] * t)
            wall_hit = (walls_and_bands_geom is not None and not walls_and_bands_geom.is_empty
                        and Point(p).distance(walls_and_bands_geom) <= FACADE_WALL_CHECK_TOL_PT)
            door_near = any(math.hypot(p[0] - dx, p[1] - dy) <= door_tol for dx, dy in door_pts)
            flags.append((t, not wall_hit and not door_near))

        runs, run_start, run_end = [], None, None
        for t, free in flags:
            if free:
                if run_start is None:
                    run_start = t
                run_end = t
            elif run_start is not None:
                runs.append((run_start, run_end)); run_start = None
        if run_start is not None:
            runs.append((run_start, run_end))

        for t0, t1 in runs:
            length = t1 - t0
            if not (FACADE_GAP_MIN_PT <= length <= FACADE_GAP_MAX_PT):
                continue
            p0 = (ax + dirv[0] * t0, ay + dirv[1] * t0)
            p1 = (ax + dirv[0] * t1, ay + dirv[1] * t1)
            mid = ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)
            if excluded(mid):
                continue

            T = _wall_thickness_pt(cellinfo, mid, normal) if cellinfo else None
            if T is None or T <= 0 or T > 40.0:
                T = FACADE_WALL_T_FALLBACK_PT
            hl = length / 2.0
            a_ = (mid[0] - dirv[0] * hl - normal[0] * T / 2, mid[1] - dirv[1] * hl - normal[1] * T / 2)
            b_ = (mid[0] + dirv[0] * hl - normal[0] * T / 2, mid[1] + dirv[1] * hl - normal[1] * T / 2)
            c_ = (mid[0] + dirv[0] * hl + normal[0] * T / 2, mid[1] + dirv[1] * hl + normal[1] * T / 2)
            e_ = (mid[0] - dirv[0] * hl + normal[0] * T / 2, mid[1] - dirv[1] * hl + normal[1] * T / 2)
            rect = Polygon([a_, b_, c_, e_])
            pts_svg = " L ".join(f"{x:.2f} {y:.2f}" for x, y in (a_, b_, c_, e_))
            rect_d = f"M {pts_svg} Z"
            parts.append(f'<path d="{rect_d}" fill="#FFFFFF" stroke="{ink}" stroke-width="{ink_sw:.3f}"/>')
            if collect is not None:
                collect.append(("окно", rect_d))
                # «порог» (FLOOR-TRACE-SPEC правило 2 «поверх - линия порога ink 0.8 по
                # комнатной кромке»): a_/b_ sit at -normal*T/2 - the INWARD (room-facing) edge,
                # since `normal` was flipped above to point outward.
                collect.append(("порог", f"M {a_[0]:.2f} {a_[1]:.2f} L {b_[0]:.2f} {b_[1]:.2f}"))

            has_klin = False
            for kp, kd in c12_items:
                if rect.intersects(kp):
                    parts.append(f'<path d="{kd}" fill="{ink}" fill-rule="nonzero"/>')
                    if collect is not None:
                        collect.append(("клин окна", kd))
                    has_klin = True
            n_stoiki = 0
            for cx, cy, cp in c21_items:
                if rect.contains(Point(cx, cy)):
                    parts.append(f'<path d="{cp}" fill="{ink}" fill-rule="nonzero"/>')
                    if collect is not None:
                        collect.append(("стойка", cp))
                    n_stoiki += 1

            if has_klin:
                n_klin += 1
            else:
                n_plain += 1
            if n_stoiki:
                n_with_stoiki += 1
            segments.append({"mid": [round(mid[0], 1), round(mid[1], 1)], "len_pt": round(length, 1),
                              "T_pt": round(T, 2), "klin": has_klin, "stoiki": n_stoiki})
            report.setdefault("trace_facade_window_rect_polys", []).append(
                [[round(x, 2), round(y, 2)] for x, y in (a_, b_, c_, e_)])

    report["trace_facade_windows"] = len(segments)
    report["trace_facade_windows_klin"] = n_klin
    report["trace_facade_windows_plain"] = n_plain
    report["trace_facade_windows_with_stoiki"] = n_with_stoiki
    report["trace_facade_window_segments"] = segments
    return "".join(parts)


# ---- «подстановка» v2 (координатор, 2026-09-21 ночь): оверлей v1 показал, что замыкание стен
# для КОМНАТ ненадёжно (там, где PDF рисует фасад стеклом, а не c08/c19, квартиры сливаются в
# растре) - комнаты/семена ячеек снова читаются из units[n]["poly"] (см. build_floor_trace).
# «Подстановка» остаётся ТОЛЬКО для балконов: floor-21.json несёт РОВНО один "balcony" на
# квартиру, у 2110/2111 в PDF по площади подписаны по два, вторые в JSON отсутствуют. Семя
# балкона = сама подпись площади PDF (не растровая компонента целиком, как в v1) - см.
# _substitute_balconies_pdf.
SUBST_HULL_BUFFER_PT = 6.0
PT2_TO_M2 = (K / 100.0) ** 2          # 1 pt = K px = K см -> pt² -> m² = (K/100)²; проверено на
                                      # поле: unit 2101 poly=5838.0 pt² x PT2_TO_M2 = 29.02 m²,
                                      # units.json living=29.1 - совпадает.
SUBST_DOOR_CHORD_W_PT = 2.0 / K
SUBST_DOOR_CHORD_OVERLAP_PT = 1.5   # запас поверх ~0.5pt зазора hinge/jamb2 - стена (см. отчёт v1)
SUBST_TEXT_SIZE_MIN = 7.0
SUBST_TEXT_VALUE_MIN = 2.0
SUBST_TEXT_VALUE_MAX = 40.0
SUBST_AREA_MATCH_TOL = 0.22            # координатор: «±22 %» (площадь семени и сумма на квартиру)
SUBST_OWNER_DIST_TOL_PT = 5.0
SUBST_BALCONY_RADIUS_M = 15.0
SUBST_BALCONY_RADIUS_PT = SUBST_BALCONY_RADIUS_M / (K / 100.0)   # 15 м -> pt
SUBST_RDP_PT = 0.8


def _rasterize_geom(draw, geom, res, fill=1):
    """PIL ImageDraw fill of a shapely (Multi)Polygon at 1/res scale, holes punched (fill=0)."""
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        polys = [geom]
    elif geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        polys = [g for g in geom.geoms if g.geom_type == "Polygon" and not g.is_empty]
    else:
        polys = []
    for p in polys:
        draw.polygon([(x / res, y / res) for x, y in p.exterior.coords], fill=fill)
        for interior in p.interiors:
            draw.polygon([(x / res, y / res) for x, y in interior.coords], fill=0)


def _parse_area_text(s):
    try:
        v = float(s.strip().replace(",", "."))
    except (ValueError, AttributeError):
        return None
    return v


def _region_row_runs_boxes(mask, res):
    import numpy as np
    from shapely.geometry import box as _box
    boxes = []
    for y in range(mask.shape[0]):
        row = mask[y]
        if not row.any():
            continue
        dd = np.diff(row.astype(np.int8))
        starts = list(np.where(dd == 1)[0] + 1)
        if row[0]:
            starts = [0] + starts
        ends = list(np.where(dd == -1)[0] + 1)
        if row[-1]:
            ends = ends + [mask.shape[1]]
        for s, e in zip(starts, ends):
            boxes.append(_box(s * res, y * res, e * res, (y + 1) * res))
    return boxes


def _substitute_balconies_pdf(rp, fd, style, structure_geom, doors, units_db, report):
    """«Подстановка» v2 (координатор, 2026-09-21 ночь) - балконы ТОЛЬКО, комнаты снова из
    units[n]["poly"] (см. build_floor_trace). Семя = сама подпись площади PDF (fd["texts"]):
    число с точкой/запятой, size>=7, значение SUBST_TEXT_VALUE_MIN..MAX м², центр НЕ внутри
    любого units[n]["poly"] и не в ядре (compute_core_bbox - если None, условие пропускается).

    Растровая 4-связная заливка (res_pt) от семени; барьеры = structure_geom (стены+пилоны+
    ИМЕЮЩИЕСЯ балконные полосы floor-F.json+ряды стоек - до подмешивания новых полос шагом 5
    build_floor_trace, чтобы не зависеть от собственного ещё не найденного результата) ∪ хорда
    между hinge/jamb2 КАЖДОЙ двери (extract_pdf_doors, тот же приём с запасом
    SUBST_DOOR_CHORD_OVERLAP_PT, что подстановка v1) ∪ ВСЕ units[n]["poly"] (растр - не только
    ближайшей квартиры, любой, чтобы заливка не могла перетечь в комнату через ещё не
    забандированный порог) ∪ снаружи контура здания (union(poly∪balcony).buffer(6pt)).
    Компонента, содержащая семя, обрезается диском радиуса SUBST_BALCONY_RADIUS_PT (15 м) от
    семени - защита от протечки; кандидат валиден, если его площадь (pt²xPT2_TO_M2) совпадает
    с подписью в пределах SUBST_AREA_MATCH_TOL (±22%).

    Владелец кандидата - квартира, чей poly ближе всего (<=SUBST_OWNER_DIST_TOL_PT); сумма
    площадей кандидатов квартиры сверяется с units_db[n]["balcony"] (±22%) - несовпадение не
    отменяет находку, только пишет report["subst_balcony_sum_warn"] (координатор: «балконы
    могут быть подписаны в PDF не все»).

    Возвращает {number: [poly,...]} (пусто, если не нашлось ни одного кандидата на квартиру -
    вызывающий код сам подставляет units[n]["balcony"] из floor-F.json как fallback) и пишет
    report["subst_seed_table"]/["subst_seed_skipped"]/["subst_units_with_balcony"]."""
    import numpy as np
    from PIL import Image, ImageDraw
    from scipy import ndimage
    from shapely.geometry import Polygon, LineString
    from shapely.ops import unary_union

    cfg = ((style.get("floor_render") or {}).get("cells")) or {}
    res = float(cfg.get("res_pt", 0.25))
    w, h = fd["w"], fd["h"]
    nx = max(1, math.ceil(w / res)); ny = max(1, math.ceil(h / res))
    numbers = sorted(fd["units"], key=int)

    door_chords = []
    for d in doors:
        hinge, jamb2 = d["hinge"], d["jamb2"]
        L = math.hypot(jamb2[0] - hinge[0], jamb2[1] - hinge[1])
        if L <= 1e-6:
            continue
        ux, uy = (jamb2[0] - hinge[0]) / L, (jamb2[1] - hinge[1]) / L
        p0 = (hinge[0] - ux * SUBST_DOOR_CHORD_OVERLAP_PT, hinge[1] - uy * SUBST_DOOR_CHORD_OVERLAP_PT)
        p1 = (jamb2[0] + ux * SUBST_DOOR_CHORD_OVERLAP_PT, jamb2[1] + uy * SUBST_DOOR_CHORD_OVERLAP_PT)
        door_chords.append(LineString([p0, p1]).buffer(SUBST_DOOR_CHORD_W_PT / 2, cap_style=2))

    unit_poly_geoms = {n: Polygon(fd["units"][n]["poly"]).buffer(0) for n in numbers}
    barrier_parts = ([structure_geom] if structure_geom is not None and not structure_geom.is_empty else [])
    barrier_parts += door_chords + list(unit_poly_geoms.values())
    barrier_geom = unary_union(barrier_parts)

    barrier_img = Image.new("1", (nx, ny), 0)
    _rasterize_geom(ImageDraw.Draw(barrier_img), barrier_geom, res)
    BARRIER = np.asarray(barrier_img, dtype=bool)

    hull_polys = []
    for u in fd["units"].values():
        hull_polys.append(Polygon(u["poly"]).buffer(0))
        if u.get("balcony"):
            hull_polys.append(Polygon(u["balcony"]).buffer(0))
    hull = unary_union(hull_polys).buffer(SUBST_HULL_BUFFER_PT, join_style=2, mitre_limit=5.0)
    hull_img = Image.new("1", (nx, ny), 0)
    _rasterize_geom(ImageDraw.Draw(hull_img), hull, res)
    HULL = np.asarray(hull_img, dtype=bool)

    struct4 = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]])
    free = HULL & ~BARRIER
    lbl, _n_comp = ndimage.label(free, structure=struct4)

    try:
        core_bbox = rp.compute_core_bbox(fd)
    except Exception:
        core_bbox = None

    def in_core(x, y):
        if core_bbox is None:
            return False
        x0, y0, x1, y1 = core_bbox
        return x0 <= x <= x1 and y0 <= y <= y1

    table, skipped = [], []
    candidates_by_unit = {n: [] for n in numbers}
    for t in fd.get("texts", []):
        v = _parse_area_text(str(t.get("str", "")))
        if v is None or not (SUBST_TEXT_VALUE_MIN <= v <= SUBST_TEXT_VALUE_MAX) or t.get("size", 0) < SUBST_TEXT_SIZE_MIN:
            continue
        tx, ty = t["x"], t["y"]
        if any(point_in_poly((tx, ty), fd["units"][n]["poly"]) for n in numbers):
            continue
        if in_core(tx, ty):
            continue

        gx, gy = int(tx / res), int(ty / res)
        if not (0 <= gx < nx and 0 <= gy < ny) or not free[gy, gx]:
            skipped.append({"text": v, "pos": [round(tx, 1), round(ty, 1)], "reason": "семя на барьере"})
            continue

        comp_id = lbl[gy, gx]
        mask = lbl == comp_id
        ys, xs = np.where(mask)
        d2 = (xs * res - tx) ** 2 + (ys * res - ty) ** 2
        outside_radius = d2 > SUBST_BALCONY_RADIUS_PT ** 2
        if outside_radius.any():
            mask = mask.copy()
            mask[ys[outside_radius], xs[outside_radius]] = False

        area_m2 = float(mask.sum()) * res * res * PT2_TO_M2
        row = {"text": v, "pos": [round(tx, 1), round(ty, 1)], "area_m2": round(area_m2, 2)}
        if abs(area_m2 - v) > SUBST_AREA_MATCH_TOL * v:
            table.append({**row, "unit": None, "status": f"площадь не сошлась (допуск {SUBST_AREA_MATCH_TOL:.0%})"})
            continue

        poly = unary_union(_region_row_runs_boxes(mask, res))
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.interiors:
            poly = Polygon(poly.exterior)
        poly = poly.simplify(SUBST_RDP_PT, preserve_topology=True)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.is_empty:
            table.append({**row, "unit": None, "status": "пустой полигон после симплификации"})
            continue

        best_n, best_d = None, 1e18
        for n in numbers:
            dd = poly.distance(unit_poly_geoms[n])
            if dd < best_d:
                best_d, best_n = dd, n
        if best_n is None or best_d > SUBST_OWNER_DIST_TOL_PT:
            table.append({**row, "unit": None,
                          "status": f"нет квартиры ближе {SUBST_OWNER_DIST_TOL_PT}pt (ближайшая {best_d:.1f}pt)"})
            continue

        candidates_by_unit[best_n].append({"poly": poly, "area_m2": area_m2, "text": v})
        table.append({**row, "unit": best_n, "status": "ok"})

    balconies_out = {}
    for n in numbers:
        cands = candidates_by_unit[n]
        total = sum(c["area_m2"] for c in cands)
        target = (units_db.get(n) or {}).get("balcony")
        if cands and target is not None and abs(total - target) > SUBST_AREA_MATCH_TOL * target:
            report.setdefault("subst_balcony_sum_warn", []).append(
                {"unit": n, "found_sum_m2": round(total, 2), "target_m2": target, "n_candidates": len(cands)})
        balconies_out[n] = [{"poly": [[round(x, 2), round(y, 2)] for x, y in c["poly"].exterior.coords[:-1]],
                              "text": c["text"], "area_m2": round(c["area_m2"], 2)} for c in cands]

    report["subst_seed_table"] = table
    report["subst_seed_skipped"] = skipped
    report["subst_units_with_balcony"] = sum(1 for n in numbers if balconies_out[n])
    return balconies_out


def _poly_path_px(poly):
    """SVG <path> `d` for a shapely Polygon that may have holes (a short balcony band -
    _balcony_wall_band above - can union into a ring with the UNBANDED middle as an interior
    hole rather than a second exterior; the party-wall bands on either short end there are wide
    enough, relative to a short/wide balcony, that their union's OUTER ring is the balcony's
    own full rectangle, with the true unbanded floor area surviving only as `interiors`). A
    plain <polygon points="exterior only"> silently drops that hole and paints the whole
    balcony solid - use this + fill-rule="evenodd" instead. Coordinates: floor-local pt -> px
    (x*K, y*K), one M..Z subpath per ring."""
    parts = []
    for ring in [poly.exterior] + list(poly.interiors):
        pts = list(ring.coords)[:-1]
        if len(pts) < 3:
            continue
        parts.append("M " + " L ".join(f"{x*K:.1f} {y*K:.1f}" for x, y in pts) + " Z")
    return " ".join(parts)


def _poly_path_pt(poly):
    """Like _poly_path_px, but coordinates stay in floor-local pt (no *K) - --figma-svg writes
    every element's own `d` pre-scaled to px via _scale_path_d at output time, not via a
    <g transform>, so intermediate pt-space polygons (glazing rows, per-edge balcony bands -
    both built directly as shapely geometry, never as a PDF path string) need this instead."""
    parts = []
    for ring in [poly.exterior] + list(poly.interiors):
        pts = list(ring.coords)[:-1]
        if len(pts) < 3:
            continue
        parts.append("M " + " L ".join(f"{x:.3f} {y:.3f}" for x, y in pts) + " Z")
    return " ".join(parts)


def _multipoly_path_px(geom):
    """SVG <path> `d` for a shapely Polygon/MultiPolygon (any holes included via
    _poly_path_px), all parts concatenated - pair with fill-rule="evenodd". None/empty/
    non-polygonal geometry -> "". Used for the --source pdf structure layer (build_floor_pdf):
    unary_union(c08 ∪ c19 ∪ X-pylons ∪ balcony bands) can legitimately come back as several
    disjoint polygons (the building isn't one blob), so this has to handle both cases, unlike
    _poly_path_px which only had to handle one polygon-with-holes."""
    if geom is None or geom.is_empty:
        return ""
    if geom.geom_type == "Polygon":
        polys = [geom]
    elif geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        polys = [g for g in geom.geoms if g.geom_type == "Polygon" and not g.is_empty]
    else:
        polys = []
    return " ".join(_poly_path_px(p) for p in polys)


def _cluster_polys(fd, cid):
    """A cluster's raw paths as shapely Polygons (buffer(0) each - closes any near-degenerate
    self-touch from the PDF export), floor-local pt. Used to fold c08/c19 into the structure
    layer's own unary_union (build_floor_pdf, --source pdf task 2026-09-21) instead of drawing
    them as a flat nonzero-fill <path> (_fill_cluster_svg) that can't be unioned with anything
    else."""
    from shapely.geometry import Polygon
    c = next((x for x in fd["clusters"] if x["id"] == cid), None)
    polys = []
    for p in (c.get("paths") if c else []) or []:
        pts = path_points(p)
        if len(pts) >= 3:
            poly = Polygon(pts).buffer(0)
            if not poly.is_empty:
                polys.append(poly)
    return polys


def _component_pole(mask, res):
    """--source pdf caption anchor (task iteration 2, the reviewer 2026-09-21: captions were landing on
    partition walls because the anchor was the pole of inaccessibility of the WHOLE apartment
    polygon, which can sit inside a wall when the room layout is non-convex). Pole of
    inaccessibility, EDT flavour, of the LARGEST 4-connected component of a boolean raster mask
    on floor_cells' own res_pt grid: label the mask 4-connected (not 8 - two rooms that only
    touch corner-to-corner across a column are different rooms), keep the biggest component,
    distance_transform_edt over it, return the argmax cell's centre in floor-local pt. None if
    `mask` has no True pixels at all."""
    import numpy as np
    from scipy import ndimage
    if not mask.any():
        return None
    lbl, n = ndimage.label(mask, structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]]))
    if n == 0:
        return None
    sizes = ndimage.sum(mask, lbl, index=range(1, n + 1))
    biggest = int(np.argmax(sizes)) + 1
    comp = lbl == biggest
    dist = ndimage.distance_transform_edt(comp)
    iy, ix = np.unravel_index(np.argmax(dist), dist.shape)
    return ((ix + 0.5) * res, (iy + 0.5) * res)


# ---- подписи этажа: выравнивание по рядам (заказчик 2026-09-22) -------------------------
CAPTION_PAD_PX = 14.0          # воздух вокруг текста до стены
CAPTION_ROW_GAP_PX = 300.0     # якоря квартир ближе по y - один ряд
CAPTION_ROW_SPAN_PX = 420.0    # ряд не шире этого по y
CAPTION_ROW_SEARCH_PX = 260.0  # поиск общей высоты ряда за пределами разброса якорей
BALCONY_ROW_GAP_PX = 70.0
BALCONY_ROW_SEARCH_PX = 60.0


def _strip_svg_group(svg, gid):
    """Вырезать <g id="gid">…</g> с учётом вложенных <g>. Возвращает (svg, сколько вырезано)."""
    n = 0
    while True:
        m = re.search(r'<g\b[^>]*\bid="' + re.escape(gid) + r'"[^>]*>', svg)
        if not m:
            return svg, n
        depth, pos = 1, m.end()
        for t in re.finditer(r'<g\b[^>]*?(/?)>|</g\s*>', svg[pos:]):
            if t.group(0).startswith("</"):
                depth -= 1
            elif t.group(1) != "/":
                depth += 1
            if depth == 0:
                svg = svg[:m.start()] + svg[pos + t.end():]
                n += 1
                break
        else:
            return svg, n


def _caption_w(text, fs):
    """Ширина строки Instrument Serif (узкий): ~0.46 em на знак, сверено по floor-4.png."""
    return len(text) * 0.46 * fs


def _largest_component(mask):
    import numpy as np
    from scipy import ndimage
    if mask is None or not mask.any():
        return None
    lbl, n = ndimage.label(mask, structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]]))
    if n == 0:
        return None
    sizes = ndimage.sum(mask, lbl, index=range(1, n + 1))
    return lbl == (int(np.argmax(sizes)) + 1)


def _box_in_mask(comp, res, cx, cy, hw, hh):
    """Бокс подписи (px холста, центр cx,cy, полуразмеры hw,hh) целиком внутри маски комнаты
    (сетка floor_cells, шаг res pt, начало = 0 pt)."""
    import math as _m
    s = K * res
    gx0, gx1 = int(_m.floor((cx - hw) / s)), int(_m.ceil((cx + hw) / s))
    gy0, gy1 = int(_m.floor((cy - hh) / s)), int(_m.ceil((cy + hh) / s))
    h, w = comp.shape
    if gx0 < 0 or gy0 < 0 or gx1 > w or gy1 > h:
        return False
    return bool(comp[gy0:gy1, gx0:gx1].all())


def _balcony_pending(poly_pt, text):
    """Балконная подпись: центр полигона (у вытянутого балкона полюс недоступности мог
    уехать к торцу - «5.4 m²» у края), иначе полюс. fits - бокс внутри полигона."""
    from shapely.geometry import Polygon as _P, box as _bx
    P = _P([(x * K, y * K) for x, y in poly_pt]).buffer(0)
    hw = _caption_w(text, BALCONY_LABEL_PX) / 2 + 6
    hh = BALCONY_LABEL_PX * 0.5 + 4
    fits = lambda x, y: P.contains(_bx(x - hw, y - hh, x + hw, y + hh))
    c = P.centroid
    if fits(c.x, c.y):
        x, y = c.x, c.y
    else:
        px, py = pole_of_inaccessibility(poly_pt)
        x, y = px * K, py * K
    return {"x": x, "y": y, "text": text, "fits": fits}


def _align_rows(items, gap, span, search, step):
    """Разбить подписи на ряды по y (цепочка с шагом < gap, ширина ряда <= span) и поставить
    каждый ряд на одну высоту: перебор y в [min-search, max+search], у каждой подписи x свой
    (при неудаче - сдвиг по x до 150 px в своей комнате); максимум влезающих, при равенстве -
    ближе к медиане якорей. Не влезающие остаются на своих якорях. Возвращает сводку рядов."""
    import statistics
    rows, cur = [], []
    for it in sorted([i for i in items if i.get("fits")], key=lambda i: i["y"]):
        if cur and (it["y"] - cur[-1]["y"] > gap or it["y"] - cur[0]["y"] > span):
            rows.append(cur); cur = []
        cur.append(it)
    if cur:
        rows.append(cur)
    dxs = [0] + [d * sgn for d in range(10, 151, 10) for sgn in (1, -1)]
    summary = []
    for row in rows:
        if len(row) < 2:
            continue
        ys = [i["y"] for i in row]
        med = statistics.median(ys)
        best = None
        y = min(ys) - search
        while y <= max(ys) + search:
            placed = {}
            for k, it in enumerate(row):
                for dx in dxs:
                    if it["fits"](it["x"] + dx, y):
                        placed[k] = it["x"] + dx
                        break
            score = (len(placed), -abs(y - med))
            if best is None or score > best[0]:
                best = (score, y, placed)
            y += step
        _, by, placed = best
        for k, it in enumerate(row):
            if k in placed:
                it["x"], it["y"] = placed[k], by
        summary.append({"n": len(row), "aligned": len(placed), "y": round(by, 1),
                        "spread_before": round(max(ys) - min(ys), 1)})
    return summary


def underlay_v15(svg, floor_num):
    """Body of an accepted v15 floor SVG without wrapper, building clip, background, wall bands
    and labels; internal ids namespaced per floor."""
    m = re.search(r'<svg[^>]*>(.*)</svg>\s*$', svg, re.S)
    body = m.group(1) if m else svg
    body = re.sub(r'<defs><clipPath id="building-clip">.*?</clipPath></defs>', '', body, count=1, flags=re.S)
    body = body.replace('<g clip-path="url(#building-clip)">', '<g>', 1)
    body = re.sub(r'<rect x="0" y="0" width="[\d.]+" height="[\d.]+" fill="#FFFFFF"/>', '', body, count=1)
    body = _RE_BAND.sub('', body)
    body = _RE_TEXT.sub('', body)
    pfx = f"f{floor_num}_"
    body = re.sub(r'id="([^"]+)"', lambda mm: f'id="{pfx}{mm.group(1)}"', body)
    body = re.sub(r'url\(#([^)]+)\)', lambda mm: f'url(#{pfx}{mm.group(1)})', body)
    return body


# ---------------------------------------------------------------- underlay
def underlay_svg(rp, fd, fp, style, w, h):
    """The old pipeline's floor body WITHOUT apartment fills/labels/beds. Anything that falls
    inside an apartment is removed by the caller's clip, so what is left is the core
    (stairs/lifts/shafts), the corridors and the walls between apartments."""
    parts = [rp.cluster_layers_svg(fd, style, context="floor", only_roles=[("floor",)])]
    if fp["core_bbox"] and fp["hull_all"]:
        parts.append(rp.core_fill_svg(fp["core_bbox"], fp["polys_all"], fp["hull_all"], w, h, style["core_fill"]))
    ink = fp["ink"]
    # wall bands (ext/corridor/shared) are synthesized from the unit polygons - the Figma finals
    # already carry every apartment wall, so drawing them again doubled the exterior walls.
    parts.append("".join(fp["stair_wall_svg"]))
    parts.append(rp.cluster_layers_svg(fd, style, context="floor", only_roles=[("walls", "columns")]))
    parts.append(rp.cluster_layers_svg(fd, style, context="floor", only_roles=[("partitions",)]))
    parts.append("".join(fp["door_svg"]))
    parts.append("".join(fp["stair_door_svg"]))
    parts.append(rp.furniture_zorder_svg(fd, style, context="floor"))
    geo = fp["geo"]
    stair_ink = style.get("stair_ink", style["muted_ink"])
    for f in fp["flights"]:
        try:
            parts.append(rp.stair_flight_svg(f, stair_ink, geo.get("stair_tread_width_pt", 0.5),
                                             geo.get("stair_contour_width_pt", 0.5),
                                             geo.get("stair_contour_dasharray", "")))
        except Exception:
            pass
    try:
        for g in rp.pair_stair_flights(fp["flights"]):
            parts.append(rp.handrail_svg(g, ink, geo.get("stair_handrail_gap_pt", 2.0),
                                         geo.get("stair_handrail_width_pt", 0.45)))
    except Exception:
        pass
    if fp["lift_bodies"]:
        try:
            parts.append(rp.lift_group_svg(
                fp["lift_bodies"], fp["lift_door_side"], ink,
                shaft_wall=fp["stair_wall_pt"], cabin_inset=geo.get("lift_cabin_inset_pt", 1.5),
                cross_w=geo.get("lift_cross_width_pt", 0.4), door_gap=geo.get("lift_door_line_gap_pt", 1.5),
                door_stroke=geo.get("lift_door_line_width_pt", 0.5), door_frac=geo.get("lift_door_frac", 0.6)))
        except Exception:
            pass
    return "".join(parts)


def _px_path(poly):
    return "M" + " L".join(f"{x * K:.2f},{y * K:.2f}" for x, y in poly) + " Z"


def build_floor_pdf(rp, fd, floor_num, style, units_db, xpilons, W, H, ink, report):
    """--source pdf (task 2026-09-21, the reviewer: «стены из двух источников... PDF - единственный
    источник истины»). Every element on the floor - walls, doors, windows, balcony parapets,
    core furniture, apartment fills - comes from fd (PDF vectors, rp.load_floor_data) and
    floor-F.json alone; no Figma final-svg is read, UnitDrawing/dr.instance() is never called,
    so drawings can be entirely unloaded without this function touching them.

    Layers bottom to top:
      1 paper
      2 ownership-cell fill (floor_cells; wall mask `W` gets the confirmed X-pylons folded in
        via `extra_wall_boxes=xpilons`, so a pylon embedded in a shared wall is wall, not public
        space swallowed into whichever apartment happens to be nearest)
      3+4+7 structure: c08 ∪ c19 ∪ X-pylons ∪ balcony walls, unioned into ONE shapely
        (Multi)Polygon (unary_union, task iteration 2 - the reviewer 2026-09-21: independently-filled
        adjoining shapes left a 1px antialiased seam at every touching edge) and drawn as ONE
        <path fill-rule="evenodd"> in <g id="structure">, unclipped
      5 doors: c00 (extract_pdf_doors), whole floor, unclipped - no apartment clip needed since
        the apartment interiors are PDF vectors too, doors included
      6 windows: c21 stiles + c24 glazing outline (_windows_svg_pdf)
      8 core furniture: stair treads, non-X columns, handrail, lift crosses
        (_core_furniture_pdf), unclipped
      9 captions: apartment (two lines), balcony area, office area - anchored at the pole of
        inaccessibility of the unit's/balcony's own LARGEST room (_component_pole, task
        iteration 2 - the reviewer 2026-09-21: anchoring on the whole apartment polygon let a caption
        land on a partition wall), no manifest.json (that lives under final-svg/, out of scope
        for this source)."""
    muted = style.get("muted", "#9AA3AD")
    font = style["labels"]["font"]

    numbers = sorted(fd["units"], key=lambda x: int(x))
    idx_of = {n: i + 1 for i, n in enumerate(numbers)}   # same numbering floor_cells() uses internally

    adj = rp.build_unit_adjacency(fd["units"], style["floor_render"].get("units_adjacency_tol_pt", 2.5))
    palette = style["floor_render"]["units_fill_palette"]
    tone_index, used = {}, [0] * len(palette)
    for n in sorted(fd["units"], key=lambda x: int(x)):
        taken = {tone_index[m] for m in adj.get(n, ()) if m in tone_index}
        free = [i for i in range(len(palette)) if i not in taken] or list(range(len(palette)))
        t = min(free, key=lambda i: (used[i], i)); tone_index[n] = t; used[t] += 1

    # ---- layer 2: ownership cells (wall mask includes confirmed X-pylons)
    cellinfo, cell_checks = None, {}
    try:
        cellinfo = floor_cells(fd, style, extra_wall_boxes=xpilons)
        cell_checks = floor_cells_checks(fd, cellinfo, adj)
        for wmsg in cellinfo["warnings"]:
            report["warnings"].append(wmsg)
    except Exception as e:
        report["warnings"].append(
            f"floor_cells failed: {e} - этаж {floor_num} (--source pdf) заливка по poly, без ячеек владения")
    report["cells_checks"] = cell_checks
    report["legacy_fill"] = False
    report["source"] = "pdf"

    cell_fill_pieces = []
    for number in sorted(fd["units"], key=lambda n: int(n)):
        poly = fd["units"][number]["poly"]
        cell_pts = (cellinfo or {}).get("cells", {}).get(number) if cellinfo else None
        if not cell_pts:
            report["warnings"].append(f"floor_cells: {number} - нет ячейки, заливка по poly (без балкона)")
            cell_pts = poly
        cell_fill_pieces.append(
            f'<polygon points="{" ".join(f"{x*K:.1f},{y*K:.1f}" for x, y in cell_pts)}" fill="{NEUTRAL_FLOOR}"/>')

    # ---- layer 3+4+7: structure = c08 ∪ c19 ∪ X-pylons ∪ balcony walls, ONE unary_union
    # (task iteration 2, the reviewer 2026-09-21: gap-check found 1px antialiasing seams at every
    # touching edge between independently-filled shapes - c08↔balcony band, c08↔c08,
    # pylon↔band; unioning them removes the seam by construction instead of a stroke hack)
    from shapely.geometry import box as _box
    from shapely.ops import unary_union as _unary_union

    structure_parts = _cluster_polys(fd, "c08") + _cluster_polys(fd, "c19")
    structure_parts += [_box(x0, y0, x1, y1) for x0, y0, x1, y1 in xpilons]

    band_pt = WALL_BAND / K
    for number in numbers:
        balcony = fd["units"][number].get("balcony")
        if not balcony:
            continue
        band_polys, band_area, bal_area = _balcony_wall_band(balcony, fd["units"][number]["poly"], band_pt)
        report.setdefault("balcony_wall_bands", []).append({
            "unit": number, "band_area_pt2": round(band_area, 2), "balcony_area_pt2": round(bal_area, 2),
            "ratio": round(band_area / bal_area, 3) if bal_area > 0 else None,
        })
        structure_parts.extend(band_polys)

    structure_geom = _unary_union(structure_parts) if structure_parts else None
    structure_d = _multipoly_path_px(structure_geom) if structure_geom is not None else ""
    structure_svg = '<g id="structure">' + (f'<path d="{structure_d}" fill="{ink}" fill-rule="evenodd"/>' if structure_d else "") + '</g>'

    # ---- layer 5: doors, whole floor, unclipped
    doors_svg = f'<g transform="scale({K})">{_doors_svg(rp, fd, ink)}</g>'

    # ---- layer 6: windows (c21 stiles + c24 glazing outline)
    windows_svg = f'<g transform="scale({K})">{_windows_svg_pdf(fd, ink)}</g>'

    # ---- layer 8: core furniture (stairs, columns, handrail, lift crosses), unclipped
    core_svg = f'<g transform="scale({K})">{_core_furniture_pdf(rp, fd, style)}</g>'

    # ---- layer 9: captions (anchor = pole of inaccessibility of the unit's/balcony's own
    # LARGEST room - task iteration 2, the reviewer 2026-09-21: anchoring on the whole apartment polygon
    # let a caption land on a partition wall, e.g. "1BR 6"/"Studio 1")
    labels, units_out = [], {}
    for number in numbers:
        u = fd["units"][number]
        poly, balcony = u["poly"], u.get("balcony")
        info = units_db.get(number)
        idx = idx_of[number]
        cell_pts = (cellinfo or {}).get("cells", {}).get(number) if cellinfo else None
        if not cell_pts:
            cell_pts = poly

        if info:
            ftype = info.get("type")
            room_pole = None
            if cellinfo:
                room_mask = (cellinfo["final"] == idx) & ~cellinfo["W"] & ~cellinfo["balcony_mask"]
                room_pole = _component_pole(room_mask, cellinfo["res_pt"])
            px, py = room_pole if room_pole else pole_of_inaccessibility(poly)
            lx, ly = px * K, py * K
            report.setdefault("caption_anchors", []).append(
                {"unit": number, "room_px": [round(float(lx), 1), round(float(ly), 1)], "from_room_mask": room_pole is not None})
            line1 = f'{TYPE_LABEL.get(ftype, "")} {info["slot"]}'.strip()
            line2 = f'{fmt_area(info["total"])} m²'
            line_h = LABEL_PX * 1.15
            y1 = ly - line_h / 2 + LABEL_PX * 0.35
            y2 = ly + line_h / 2 + LABEL_PX * 0.35
            labels.append(f'<text x="{lx:.1f}" y="{y1:.1f}" text-anchor="middle" '
                          f'font-family="{font}, serif" font-weight="400" font-size="{LABEL_PX}" '
                          f'fill="{ink}">{line1}</text>'
                          f'<text x="{lx:.1f}" y="{y2:.1f}" text-anchor="middle" '
                          f'font-family="{font}, serif" font-weight="400" font-size="{LABEL_PX}" '
                          f'fill="{ink}">{line2}</text>')
        else:
            report["skipped"].append(f"{number} (нет записи в units_db - без подписи)")

        outline_pt = cell_pts if cell_pts is not poly else unit_outline(poly, balcony)
        units_out[number] = {
            "poly": [[round(x * K, 1), round(y * K, 1)] for x, y in poly],
            "balcony": [[round(x * K, 1), round(y * K, 1)] for x, y in balcony] if balcony else None,
            "outline": [[round(x * K, 1), round(y * K, 1)] for x, y in outline_pt],
            "tone": tone_index.get(number, 0),
            "label_living": (info or {}).get("living"),
        }

        if balcony and info and info.get("balcony") is not None:
            bxs = [p[0] for p in balcony]; bys = [p[1] for p in balcony]
            bal_w = min(max(bxs) - min(bxs), max(bys) - min(bys))
            if bal_w < BALCONY_LABEL_MIN_W_PT:
                report.setdefault("balcony_labels_skipped", []).append(
                    f"{number} (узкий балкон {bal_w:.1f}pt < {BALCONY_LABEL_MIN_W_PT}pt)")
            else:
                bal_pole = None
                if cellinfo:
                    bal_mask = (cellinfo["final"] == idx) & cellinfo["balcony_mask"]
                    bal_pole = _component_pole(bal_mask, cellinfo["res_pt"])
                bpx, bpy = bal_pole if bal_pole else pole_of_inaccessibility(balcony)
                blx, bly = bpx * K, bpy * K
                report.setdefault("caption_anchors_balcony", []).append(
                    {"unit": number, "balcony_px": [round(float(blx), 1), round(float(bly), 1)], "from_balcony_mask": bal_pole is not None})
                btxt = fmt_area(info["balcony"]) + " m²"
                labels.append(f'<text x="{blx:.1f}" y="{bly + BALCONY_LABEL_PX * 0.35:.1f}" text-anchor="middle" '
                              f'font-family="{font}, serif" font-weight="400" font-size="{BALCONY_LABEL_PX}" '
                              f'fill="{muted}">{btxt}</text>')
                report.setdefault("balcony_labels", []).append(number)

    # non-residential zones (offices on floor 2): same PDF area-text scan as --source finals
    for t in fd.get("texts", []):
        try:
            v = float(t["str"])
        except ValueError:
            continue
        if t.get("size", 0) < 7 or v < TEXT_MIN_AREA or "." not in t["str"]:
            continue
        if floor_num not in OFFICE_FLOORS:
            continue
        if any(point_in_poly((t["x"], t["y"]), u["poly"]) for u in fd["units"].values()):
            continue
        lx, ly = (t["x"] + 8) * K, t["y"] * K
        labels.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" font-family="{font}, serif" '
                      f'font-weight="400" font-size="{OFFICE_LABEL_PX}" fill="{muted}">Office / {fmt_area(v)} m²</text>')
        report.setdefault("offices", []).append(f"{v}")

    report["units"] = len(units_out)
    paper_rect = f'<rect x="0" y="0" width="{W}" height="{H}" fill="{style["paper"]}"/>'
    svg_body = (FONT_STYLE + paper_rect + "".join(cell_fill_pieces) + structure_svg + doors_svg
                + windows_svg + core_svg + "".join(labels))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}">{svg_body}</svg>')
    return svg, W, H, units_out


def build_floor_trace(rp, fd, floor_num, style, units_db, xpilons, W, H, ink, report, figma_collect=None):
    """--source trace (эксперимент 2026-09-21, ревьюер: «выкинем квартиры из схемы, просто
    протрейсим стены, балконы, клинья и двери»). plan-studio/FLOOR-TRACE-SPEC.md. Этаж рисуется
    целиком из векторов PDF (fd) нашим языком чертежа - как build_floor_pdf, но без
    UnitDrawing/final-svg и с более детальными окнами/дверями/балконами.

    «Подстановка» v2 (координатор, 2026-09-21 ночь, по итогам оверлея v1): замыкание стен
    ненадёжно для КОМНАТ (там, где PDF рисует фасад стеклом, а не c08/c19, квартиры сливаются в
    растре) - комнаты/семена ячеек снова читаются из units[n]["poly"], как до подстановки.
    Подстановка остаётся ТОЛЬКО для балконов (floor-21.json несёт один balcony на квартиру, у
    2110/2111 в PDF их два - см. _substitute_balconies_pdf) и идёт ПОСЛЕ структуры/дверей, но
    ДО финальных окон (фасадные окна теперь смотрят на структуру, уже дополненную полосами
    найденных балконов - ложные «окна в воздухе» у 2110/2111 больше не должны появляться).

    Порядок:
      1 structure preliminary (c08∪c19∪X-пилоны∪имеющиеся в floor-F.json балконные полосы∪
        ряды стоек) - барьер для подстановки балконов
      2 двери (extract_pdf_doors один раз - и для коробок, и для хорд подстановки)
      3 подстановка балконов (_substitute_balconies_pdf) - семя = подпись площади PDF
      4 structure final - полосы по ВСЕМ балконам квартиры (найденным или, если не нашлось,
        исходному из floor-F.json), вместо преliminary-полос
      5 окна (c06+клин, правило 2/17; проёмы фасада, правило 1) - на structure final
      6 floor_cells: семена = poly ∪ все балконы квартиры (как раньше poly∪balcony, только
        список балконов может быть длиннее одного)
      7 ядро, подписи (по ВСЕМ балконам квартиры)."""
    muted = style.get("muted", "#9AA3AD")
    font = style["labels"]["font"]

    numbers = sorted(fd["units"], key=lambda x: int(x))
    idx_of = {n: i + 1 for i, n in enumerate(numbers)}

    adj = rp.build_unit_adjacency(fd["units"], style["floor_render"].get("units_adjacency_tol_pt", 2.5))
    palette = style["floor_render"]["units_fill_palette"]
    tone_index, used = {}, [0] * len(palette)
    for n in sorted(fd["units"], key=lambda x: int(x)):
        taken = {tone_index[m] for m in adj.get(n, ()) if m in tone_index}
        free = [i for i in range(len(palette)) if i not in taken] or list(range(len(palette)))
        t = min(free, key=lambda i: (used[i], i)); tone_index[n] = t; used[t] += 1
    report["legacy_fill"] = False
    report["source"] = "trace"

    # ---- шаг 1: structure preliminary = c08 ∪ c19 ∪ X-пилоны ∪ имеющиеся (floor-F.json)
    # балконные полосы ∪ ряды стоек остекления - только как барьер для подстановки, шаг 3.
    from shapely.geometry import box as _box, Polygon as _Polygon
    from shapely.ops import unary_union as _unary_union

    base_wall_parts = _cluster_polys(fd, "c08") + _cluster_polys(fd, "c19")
    base_wall_parts += [_box(x0, y0, x1, y1) for x0, y0, x1, y1 in xpilons]
    report["trace_wall_paths"] = len(_cluster_polys(fd, "c08")) + len(_cluster_polys(fd, "c19"))
    report["trace_xpilons"] = len(xpilons)

    if figma_collect is not None:
        # --figma-svg (the reviewer, 2026-09-21: поэлементный SVG для импорта в Figma, руками чистит
        # сам) - «Стены»: РАЗДЕЛЬНЫЕ пути, один на исходный полигон PDF, не unary_union (в
        # отличие от structure_geom выше, который существует только для растровых барьеров/
        # рендера PNG и никак не используется здесь).
        for cid in ("c08", "c19"):
            c = next((x for x in fd["clusters"] if x["id"] == cid), None)
            for p in (c.get("paths") if c else []) or []:
                figma_collect.setdefault("walls", []).append(("стена", p))
        for x0, y0, x1, y1 in xpilons:
            figma_collect.setdefault("walls", []).append(
                ("колонна", f"M{x0:.2f} {y0:.2f}H{x1:.2f}V{y1:.2f}H{x0:.2f}V{y0:.2f}Z"))

    band_pt = WALL_BAND / K
    glazing_rows = _balcony_glazing_rows_pdf(rp, fd, band_pt)
    report["trace_glazing_rows"] = len(glazing_rows)

    prelim_bands = []
    for number in numbers:
        balcony = fd["units"][number].get("balcony")
        if not balcony:
            continue
        band_polys, _ba, _bb = _balcony_wall_band(balcony, fd["units"][number]["poly"], band_pt)
        prelim_bands.extend(band_polys)
    structure_geom_prelim = _unary_union(base_wall_parts + prelim_bands + glazing_rows)

    # ownership cells, prelim pass (wall mask W = c08∪c19∪xpilons only - независим от balcony
    # seeds, нужен только для толщины стены под окнами/дверями ниже)
    cellinfo_prelim = None
    try:
        cellinfo_prelim = floor_cells(fd, style, extra_wall_boxes=xpilons)
        for wmsg in cellinfo_prelim["warnings"]:
            report["warnings"].append(wmsg)
    except Exception as e:
        report["warnings"].append(f"floor_cells (prelim) failed: {e}")

    # ---- шаг 2: двери (один раз - коробки + подстановка ниже читает те же hinge/jamb2)
    try:
        doors = rp.extract_pdf_doors(fd)
    except Exception as e:
        doors = []
        report["warnings"].append(f"extract_pdf_doors упал: {e}")

    # ---- шаг 3: подстановка балконов - семя = подпись площади PDF, барьеры = structure
    # preliminary ∪ хорды дверей ∪ ВСЕ units[n].poly ∪ снаружи контура здания.
    balconies_found = _substitute_balconies_pdf(rp, fd, style, structure_geom_prelim, doors, units_db, report)

    effective_balconies = {}
    for number in numbers:
        found = balconies_found.get(number) or []
        if found:
            effective_balconies[number] = found
        elif fd["units"][number].get("balcony"):
            effective_balconies[number] = [{"poly": fd["units"][number]["balcony"], "text": None, "area_m2": None}]
        else:
            effective_balconies[number] = []
    report["trace_balconies_found_n"] = sum(1 for n in numbers if balconies_found.get(n))
    report["trace_balconies_fallback_n"] = sum(1 for n in numbers if not balconies_found.get(n) and effective_balconies[n])

    # ---- шаг 4: structure final - полосы по ВСЕМ effective_balconies (заменяют preliminary)
    final_bands = []
    for number in numbers:
        for bal in effective_balconies[number]:
            band_polys, band_area, bal_area = _balcony_wall_band(bal["poly"], fd["units"][number]["poly"], band_pt)
            report.setdefault("balcony_wall_bands", []).append({
                "unit": number, "band_area_pt2": round(band_area, 2), "balcony_area_pt2": round(bal_area, 2),
                "ratio": round(band_area / bal_area, 3) if bal_area > 0 else None,
                "source": "подстановка" if bal["text"] is not None else "floor-F.json",
            })
            final_bands.extend(band_polys)
    structure_parts = base_wall_parts + final_bands + glazing_rows
    structure_geom = _unary_union(structure_parts)
    walls_and_bands_geom = _unary_union(base_wall_parts + final_bands)   # без рядов стоек - для правила 1
    structure_d = _multipoly_path_px(structure_geom) if structure_geom is not None else ""
    structure_svg = '<g id="structure">' + (f'<path d="{structure_d}" fill="{ink}" fill-rule="evenodd"/>' if structure_d else "") + '</g>'

    # ---- двери: коробки + рамка + полотно + дуга (правило 3/7), T «по W-маске», fallback -
    # контур эффективных балконов (не только исходного floor-F.json - подстановка могла найти
    # у этой же квартиры ещё один).
    balcony_boundaries = [_Polygon(bal["poly"]).buffer(0).boundary
                           for number in numbers for bal in effective_balconies[number]]
    soft_walls = _unary_union(balcony_boundaries) if balcony_boundaries else None
    doors_collect = figma_collect.setdefault("doors", []) if figma_collect is not None else None
    doors_svg_body = _doors_svg_trace(rp, fd, ink, cellinfo_prelim, report, soft_walls, band_pt, doors=doors,
                                       collect=doors_collect)
    doors_svg = f'<g transform="scale({K})">{doors_svg_body}</g>'

    # ---- шаг 5: окна (c06+клин + фасадные проёмы) - на structure FINAL (полосы найденных
    # балконов уже внутри - ложные проёмы фасада у 2110/2111 должны закрыться сами собой).
    windows_collect = figma_collect.setdefault("windows", []) if figma_collect is not None else None
    windows_c06_svg = _windows_svg_trace(rp, fd, ink, report, collect=windows_collect)
    windows_facade_svg = _facade_window_gaps_pdf(
        rp, fd, cellinfo_prelim, walls_and_bands_geom, ink, report, exclude_rects=report.get("trace_window_rects_pt"),
        collect=windows_collect)
    windows_svg = f'<g transform="scale({K})">{windows_c06_svg}{windows_facade_svg}</g>'

    if figma_collect is not None:
        # «Балконы»: полосы по контуру КАЖДОГО effective-балкона - по одному пути на ребро (не
        # unary_union, в отличие от final_bands выше, который тоже нужен только для растра/PNG),
        # плюс ряды стоек остекления вне балконов (уже по одному пути на ряд, ничего менять не
        # нужно) - обе группы правило 4, id «окно балкона» по просьбе координатора.
        for number in numbers:
            for bal in effective_balconies[number]:
                for seg_poly in _balcony_wall_band_segments(bal["poly"], fd["units"][number]["poly"], band_pt):
                    figma_collect.setdefault("balconies", []).append(("окно балкона", _poly_path_pt(seg_poly)))
        for row_geom in glazing_rows:
            figma_collect.setdefault("balconies", []).append(("окно балкона", _poly_path_pt(row_geom)))

        # «Ядро»: ступень/перила/лифт, те же under-lying детекторы, что _core_furniture_pdf
        # использует для PNG-рендера (не трогаем саму _core_furniture_pdf - её строковый вывод
        # смешивает всё в одну пачку без разбивки по слоям).
        wall_edges_core = _wall_edges_pdf(fd)
        treads, pilons = _classify_columns_pdf(rp, fd, wall_edges_core)
        for x0, y0, x1, y1 in treads:
            figma_collect.setdefault("core", []).append(
                ("ступень", f"M{x0:.2f} {y0:.2f}H{x1:.2f}V{y1:.2f}H{x0:.2f}V{y0:.2f}Z"))
        for x0, y0, x1, y1 in pilons:
            figma_collect.setdefault("walls", []).append(
                ("колонна", f"M{x0:.2f} {y0:.2f}H{x1:.2f}V{y1:.2f}H{x0:.2f}V{y0:.2f}Z"))
        for (hx0, hy0, hx1, hy1), vertical in _detect_handrails_pdf(rp, fd):
            if vertical:
                cx = (hx0 + hx1) / 2
                figma_collect.setdefault("core", []).append(("перила", f"M{cx:.2f} {hy0:.2f}L{cx:.2f} {hy1:.2f}"))
            else:
                cy = (hy0 + hy1) / 2
                figma_collect.setdefault("core", []).append(("перила", f"M{hx0:.2f} {cy:.2f}L{hx1:.2f} {cy:.2f}"))
        for lx0, ly0, lx1, ly1 in _detect_lift_cells_pdf(rp, fd):
            margin = 1.8
            mx0, my0, mx1, my1 = lx0 + margin, ly0 + margin, lx1 - margin, ly1 - margin
            if mx1 > mx0 and my1 > my0:
                figma_collect.setdefault("core", []).append(
                    ("лифт", f"M {mx0:.2f} {my0:.2f} L {mx1:.2f} {my1:.2f} M {mx0:.2f} {my1:.2f} L {mx1:.2f} {my0:.2f}"))

    # ---- шаг 6: ownership cells, финальный проход - семена = poly ∪ все балконы квартиры
    seeds, balcony_seeds = {}, {}
    for number in numbers:
        bal_polys = [bal["poly"] for bal in effective_balconies[number]]
        seeds[number] = [fd["units"][number]["poly"]] + bal_polys
        balcony_seeds[number] = bal_polys

    cellinfo, cell_checks = None, {}
    try:
        cellinfo = floor_cells(fd, style, extra_wall_boxes=xpilons, seeds=seeds, balcony_seeds=balcony_seeds)
        cell_checks = floor_cells_checks(fd, cellinfo, adj)
        for wmsg in cellinfo["warnings"]:
            report["warnings"].append(wmsg)
    except Exception as e:
        report["warnings"].append(
            f"floor_cells failed: {e} - этаж {floor_num} (--source trace) заливка по poly, без ячеек владения")
        cellinfo = cellinfo_prelim
    report["cells_checks"] = cell_checks

    cell_fill_pieces = []
    for number in numbers:
        poly = fd["units"][number]["poly"]
        cell_pts = (cellinfo or {}).get("cells", {}).get(number) if cellinfo else None
        if not cell_pts:
            report["warnings"].append(f"floor_cells: {number} - нет ячейки, заливка по poly (без балкона)")
            cell_pts = poly
        cell_fill_pieces.append(
            f'<polygon points="{" ".join(f"{x*K:.1f},{y*K:.1f}" for x, y in cell_pts)}" fill="{NEUTRAL_FLOOR}"/>')

    # ---- ядро (stairs, columns, handrail, lift crosses), unclipped
    core_svg = f'<g transform="scale({K})">{_core_furniture_pdf(rp, fd, style)}</g>'

    # ---- подписи: якорь квартиры как раньше (полюс недоступности самой большой комнаты по
    # маске ячейки); балконные подписи - по каждому найденному/effective балкону квартиры.
    labels, units_out = [], {}
    for number in numbers:
        u = fd["units"][number]
        poly = u["poly"]
        info = units_db.get(number)
        idx = idx_of[number]
        cell_pts = (cellinfo or {}).get("cells", {}).get(number) if cellinfo else None
        if not cell_pts:
            cell_pts = poly
        bal_list = effective_balconies.get(number) or []

        if info:
            ftype = info.get("type")
            room_pole = None
            if cellinfo:
                room_mask = (cellinfo["final"] == idx) & ~cellinfo["W"] & ~cellinfo["balcony_mask"]
                room_pole = _component_pole(room_mask, cellinfo["res_pt"])
            px, py = room_pole if room_pole else pole_of_inaccessibility(poly)
            lx, ly = px * K, py * K
            report.setdefault("caption_anchors", []).append(
                {"unit": number, "room_px": [round(float(lx), 1), round(float(ly), 1)], "from_room_mask": room_pole is not None})
            line1 = f'{TYPE_LABEL.get(ftype, "")} {info["slot"]}'.strip()
            line2 = f'{fmt_area(info["total"])} m²'
            line_h = LABEL_PX * 1.15
            y1 = ly - line_h / 2 + LABEL_PX * 0.35
            y2 = ly + line_h / 2 + LABEL_PX * 0.35
            labels.append(f'<text x="{lx:.1f}" y="{y1:.1f}" text-anchor="middle" '
                          f'font-family="{font}, serif" font-weight="400" font-size="{LABEL_PX}" '
                          f'fill="{ink}">{line1}</text>'
                          f'<text x="{lx:.1f}" y="{y2:.1f}" text-anchor="middle" '
                          f'font-family="{font}, serif" font-weight="400" font-size="{LABEL_PX}" '
                          f'fill="{ink}">{line2}</text>')
        else:
            report["skipped"].append(f"{number} (нет записи в units_db - без подписи)")

        outline_pt = cell_pts if cell_pts is not poly else unit_outline(poly, bal_list[0]["poly"] if bal_list else None)
        units_out[number] = {
            "poly": [[round(x * K, 1), round(y * K, 1)] for x, y in poly],
            "balcony": ([[round(x * K, 1), round(y * K, 1)] for x, y in
                        max(bal_list, key=lambda b: _Polygon(b["poly"]).area)["poly"]] if bal_list else None),
            "balconies": [[[round(x * K, 1), round(y * K, 1)] for x, y in b["poly"]] for b in bal_list],
            "outline": [[round(x * K, 1), round(y * K, 1)] for x, y in outline_pt],
            "tone": tone_index.get(number, 0),
            "label_living": (info or {}).get("living"),
        }

        for bal_idx, bal in enumerate(bal_list):
            bal_poly = bal["poly"]
            bxs = [p[0] for p in bal_poly]; bys = [p[1] for p in bal_poly]
            bal_w = min(max(bxs) - min(bxs), max(bys) - min(bys))
            if bal_w < BALCONY_LABEL_MIN_W_PT:
                report.setdefault("balcony_labels_skipped", []).append(
                    f"{number}#{bal_idx} (узкий балкон {bal_w:.1f}pt < {BALCONY_LABEL_MIN_W_PT}pt)")
                continue
            bpx, bpy = pole_of_inaccessibility(bal_poly)
            blx, bly = bpx * K, bpy * K
            area_val = bal["text"] if bal["text"] is not None else bal["area_m2"]
            if area_val is None and info:
                area_val = info.get("balcony")
            btxt = (fmt_area(area_val) if area_val is not None else "?") + " m²"
            report.setdefault("caption_anchors_balcony", []).append(
                {"unit": number, "idx": bal_idx, "balcony_px": [round(float(blx), 1), round(float(bly), 1)],
                 "source": "подстановка" if bal["text"] is not None else "floor-F.json"})
            labels.append(f'<text x="{blx:.1f}" y="{bly + BALCONY_LABEL_PX * 0.35:.1f}" text-anchor="middle" '
                          f'font-family="{font}, serif" font-weight="400" font-size="{BALCONY_LABEL_PX}" '
                          f'fill="{muted}">{btxt}</text>')
            report.setdefault("balcony_labels", []).append(f"{number}#{bal_idx}")

    for t in fd.get("texts", []):
        try:
            v = float(t["str"])
        except ValueError:
            continue
        if t.get("size", 0) < 7 or v < TEXT_MIN_AREA or "." not in t["str"]:
            continue
        if floor_num not in OFFICE_FLOORS:
            continue
        if any(point_in_poly((t["x"], t["y"]), u["poly"]) for u in fd["units"].values()):
            continue
        lx, ly = (t["x"] + 8) * K, t["y"] * K
        labels.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" font-family="{font}, serif" '
                      f'font-weight="400" font-size="{OFFICE_LABEL_PX}" fill="{muted}">Office / {fmt_area(v)} m²</text>')
        report.setdefault("offices", []).append(f"{v}")

    report["units"] = len(units_out)
    paper_rect = f'<rect x="0" y="0" width="{W}" height="{H}" fill="{style["paper"]}"/>'
    svg_body = (FONT_STYLE + paper_rect + "".join(cell_fill_pieces) + structure_svg + doors_svg
                + windows_svg + core_svg + "".join(labels))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}">{svg_body}</svg>')
    return svg, W, H, units_out


FIGMA_IN_TMPL = "plan-studio/v3/figma/floor-{F}-manual.svg"   # --source figma: где лежит ручная чистка


def build_floor_figma(rp, fd, floor_num, style, units_db, W, H, ink, report, figma_in_path):
    """--source figma (ревьюер, 2026-09-21: чертёж этажа - ручная чистка в Figma, сборщик
    добавляет ячейки/подписи). Полная противоположность --source pdf/trace: НИЧЕГО в чертеже
    не детектируется и не перерисовывается - берём готовый SVG, экспортированный ревьюером из
    Figma (plan-studio/v3/figma/floor-F-manual.svg), и вставляем его тело как есть, только
    перекрасив black -> style.ink (белое остаётся белым). Система координат файла уже наша:
    px, 1px = 1см, начало = bbox этажа из floor-F.json, холст W x H = bbox.w*K x bbox.h*K.

    Сборщик добавляет к чертежу только то, что нужно сайту (ровно как build_floor_trace):
      1 бумага (белый rect)
      2 ячейки владения квартир (floor_cells по семенам poly+balcony из floor-F.json,
        заливка NEUTRAL_FLOOR - балконная часть ячейки тем же цветом, как в trace)
      3 чертёж из Figma (как есть)
      4 подписи квартир (две строки: «тип слот» / «площадь m²») и балконов
    плюс units_out (outline для ховера на сайте).

    Чего здесь НЕТ по сравнению с --source trace (и почему): X-пилоны (extra_wall_boxes=None -
    стены чертежа нас не касаются, маска стен для ячеек читается из PDF-кластеров c08/c19,
    как и было), подстановка балконов по PDF (_substitute_balconies_pdf), ряды стоек
    остекления, детекторы дверей/окон/ядра. Балкон квартиры - ровно один полигон из
    floor-F.json (или ни одного)."""
    from shapely.geometry import Polygon as _Polygon

    muted = style.get("muted", "#9AA3AD")
    font = style["labels"]["font"]

    # ---- шаг 0: чертёж из Figma - тело корневого <svg> как есть, только перекраска black->ink
    src_path = Path(figma_in_path)
    raw = src_path.read_text(encoding="utf-8")
    m = re.search(r"<svg\b[^>]*>(.*)</svg\s*>", raw, re.S)
    if not m:
        raise ValueError(f"{src_path}: не найден корневой <svg> (ожидается экспорт Figma)")
    drawing = m.group(1)
    # пробная группа «Подписи» (Figma, этаж 4, 2026-09-22) - в экспорте это контуры букв;
    # подписи ставит сборщик, иначе на плане два комплекта
    drawing, n_captions = _strip_svg_group(drawing, "Подписи")
    report["figma_in_captions_stripped"] = n_captions
    drawing, n_fill = re.subn(r'fill="black"', f'fill="{ink}"', drawing)
    drawing, n_stroke = re.subn(r'stroke="black"', f'stroke="{ink}"', drawing)
    report["source"] = "figma"
    report["figma_in"] = str(src_path)
    report["figma_in_paths"] = drawing.count("<path")
    report["figma_in_groups"] = drawing.count("<g ")
    report["figma_in_recolor"] = {"fill": n_fill, "stroke": n_stroke}
    report["figma_in_bytes"] = len(raw.encode("utf-8"))
    drawing_svg = '<g id="figma-drawing">' + drawing + "</g>"

    numbers = sorted(fd["units"], key=lambda x: int(x))
    idx_of = {n: i + 1 for i, n in enumerate(numbers)}

    # Экспорт якорей подписей для Figma (--labels-json, ревьюер 2026-09-22): те же x/y в px холста,
    # что уходят в <text> итогового SVG, т.е. координаты внутри фрейма этажа в Figma.
    labels_export = {"floor": floor_num, "w": W, "h": H, "units": [], "balconies": [], "offices": []}

    adj = rp.build_unit_adjacency(fd["units"], style["floor_render"].get("units_adjacency_tol_pt", 2.5))
    palette = style["floor_render"]["units_fill_palette"]
    tone_index, used = {}, [0] * len(palette)
    for n in sorted(fd["units"], key=lambda x: int(x)):
        taken = {tone_index[m] for m in adj.get(n, ()) if m in tone_index}
        free = [i for i in range(len(palette)) if i not in taken] or list(range(len(palette)))
        t = min(free, key=lambda i: (used[i], i)); tone_index[n] = t; used[t] += 1
    report["legacy_fill"] = False

    # ---- ячейки владения: семена = poly ∪ (единственный) балкон квартиры из floor-F.json.
    # Никакой подстановки балконов по PDF (в отличие от --source trace) и никаких X-пилонов в
    # маске стен - только c08/c19, как их видит сам floor_cells.
    # ревьюер, 2026-09-22: tools/plan-units-from-pdf.py пишет балконы из слоя «ბინების კვადრატულობა»
    # самого PDF, и у квартиры их может быть НЕСКОЛЬКО (2611: 22.4 + 3.7 м²). Новое поле
    # units[N]["balconies"] = [poly, ...] имеет приоритет; старое units[N]["balcony"] (один
    # полигон) остаётся запасным путём для файлов, которые ещё не пересобраны.
    effective_balconies = {}
    n_multi = 0
    for number in numbers:
        u = fd["units"][number]
        bals = u.get("balconies")
        if bals:
            # у квартиры с ОДНИМ балконом подпись остаётся из прайса (точная цифра застройщика);
            # у квартиры с НЕСКОЛЬКИМИ подписывать каждому полный прайс нельзя (у 2611 оба куска
            # подписывались «26.2 m²» вместо 22.4 и 3.7) - считаем площадь каждого полигона сами
            multi = len(bals) > 1
            effective_balconies[number] = [
                {"poly": b, "text": None,
                 "area_m2": round(_Polygon(b).area * PT2_TO_M2, 1) if multi else None}
                for b in bals]
            if multi:
                n_multi += 1
        else:
            bal = u.get("balcony")
            effective_balconies[number] = [{"poly": bal, "text": None, "area_m2": None}] if bal else []
    report["figma_balconies_json_n"] = sum(1 for n in numbers if effective_balconies[n])
    report["figma_balconies_multi_n"] = n_multi
    report["figma_balconies_total"] = sum(len(effective_balconies[n]) for n in numbers)

    seeds, balcony_seeds = {}, {}
    for number in numbers:
        bal_polys = [bal["poly"] for bal in effective_balconies[number]]
        seeds[number] = [fd["units"][number]["poly"]] + bal_polys
        balcony_seeds[number] = bal_polys

    cellinfo, cell_checks = None, {}
    try:
        cellinfo = floor_cells(fd, style, extra_wall_boxes=None, seeds=seeds, balcony_seeds=balcony_seeds)
        cell_checks = floor_cells_checks(fd, cellinfo, adj)
        for wmsg in cellinfo["warnings"]:
            report["warnings"].append(wmsg)
    except Exception as e:
        report["warnings"].append(
            f"floor_cells failed: {e} - этаж {floor_num} (--source figma) заливка по poly, без ячеек владения")
        cellinfo = None
    report["cells_checks"] = cell_checks

    # ---- заливка (ревьюер, 2026-09-21 вечер, по итогам просмотра этажа 4 на сайте: «потерялись
    # несколько балконов и не выделяются цветом»): в --source figma ячейка floor_cells НЕ
    # годится как заливка - она растёт флуд-филлом через проёмы в маске стен PDF, и комната
    # без «прохода» (скошенные комнаты 411/412, комнаты 409 над ядром, левые 406/407) остаётся
    # белой. Полигоны квартиры и балкона из floor-F.json ложатся на чертёж ревьюера точно
    # (проверено оверлеем), поэтому заливаем ИМИ напрямую: poly + balcony, оба NEUTRAL_FLOOR.
    # floor_cells остаётся только якорем подписи (room_mask ниже) - другие режимы не тронуты.
    cell_fill_pieces = []
    for number in numbers:
        poly = fd["units"][number]["poly"]
        cell_fill_pieces.append(
            f'<polygon points="{" ".join(f"{x*K:.1f},{y*K:.1f}" for x, y in poly)}" fill="{NEUTRAL_FLOOR}"/>')
        for bal in effective_balconies.get(number) or []:
            cell_fill_pieces.append(
                f'<polygon points="{" ".join(f"{x*K:.1f},{y*K:.1f}" for x, y in bal["poly"])}" fill="{NEUTRAL_FLOOR}"/>')
    # Офисы (ревьюер, 2026-09-22: «это офисы, да»). tools/plan-units-from-pdf.py кладёт их в
    # fd["offices"] = [{"poly", "balconies", "area"}] теми же петлями слоя «ბინების კვადრატულობა»,
    # что и квартиры: это серые петли >= 8 м², которым не нашлось номера в прайсе (на 2-м этаже
    # 40.7 и 35.1 м²). Заливаются как квартиры (NEUTRAL_FLOOR, poly + свои балконы), но лотами
    # НЕ являются - в units_out и в ячейки владения floor_cells они не попадают.
    offices_fd = fd.get("offices", []) or []
    for off in offices_fd:
        cell_fill_pieces.append(
            f'<polygon points="{" ".join(f"{x*K:.1f},{y*K:.1f}" for x, y in off["poly"])}" fill="{NEUTRAL_FLOOR}"/>')
        for b in off.get("balconies") or []:
            cell_fill_pieces.append(
                f'<polygon points="{" ".join(f"{x*K:.1f},{y*K:.1f}" for x, y in b)}" fill="{NEUTRAL_FLOOR}"/>')
    report["figma_offices_json_n"] = len(offices_fd)
    report["figma_fill"] = "poly+balcony+offices (floor-F.json), без floor_cells"

    # ---- подписи: якорь квартиры - полюс недоступности самой большой комнаты по маске ячейки
    # (ровно как в --source trace); если floor_cells упал или ячейки нет - полюс самого poly.
    # Балконная подпись - по полигону балкона из floor-F.json.
    labels, units_out = [], {}
    pend_units, pend_bals, pend_offs = [], [], []   # подписи копятся, ставятся после выравнивания по рядам
    for number in numbers:
        u = fd["units"][number]
        poly = u["poly"]
        info = units_db.get(number)
        idx = idx_of[number]
        bal_list = effective_balconies.get(number) or []

        if info:
            ftype = info.get("type")
            room_pole, comp = None, None
            if cellinfo:
                room_mask = (cellinfo["final"] == idx) & ~cellinfo["W"] & ~cellinfo["balcony_mask"]
                room_pole = _component_pole(room_mask, cellinfo["res_pt"])
                comp = _largest_component(room_mask)
            px, py = room_pole if room_pole else pole_of_inaccessibility(poly)
            lx, ly = px * K, py * K
            report.setdefault("caption_anchors", []).append(
                {"unit": number, "room_px": [round(float(lx), 1), round(float(ly), 1)], "from_room_mask": room_pole is not None})
            line1 = f'{TYPE_LABEL.get(ftype, "")} {info["slot"]}'.strip()
            line2 = f'{fmt_area(info["total"])} m²'
            hw = max(_caption_w(line1, LABEL_PX), _caption_w(line2, LABEL_PX)) / 2 + CAPTION_PAD_PX
            hh = LABEL_PX * 1.1 + CAPTION_PAD_PX
            fits = None
            if comp is not None:
                fits = (lambda c, hw_, hh_: lambda x, y: _box_in_mask(c, cellinfo["res_pt"], x, y, hw_, hh_))(comp, hw, hh)
            pend_units.append({"number": number, "ftype": ftype, "slot": info.get("slot"),
                               "total": info.get("total"), "line1": line1, "line2": line2,
                               "x": lx, "y": ly, "fits": fits})
        else:
            report["skipped"].append(f"{number} (нет записи в units_db - без подписи)")

        # ховер на сайте: контур = poly ∪ balcony (та же правка ревьюера 2026-09-21 вечер -
        # ячейка floor_cells сюда больше не попадает, см. комментарий к заливке выше)
        # outline = poly ∪ ВСЕ балконы квартиры (а не только первый) - ревьюер, 2026-09-22
        outline_pt = unit_outline(poly, [b["poly"] for b in bal_list] if bal_list else None)
        units_out[number] = {
            "poly": [[round(x * K, 1), round(y * K, 1)] for x, y in poly],
            "balcony": ([[round(x * K, 1), round(y * K, 1)] for x, y in
                        max(bal_list, key=lambda b: _Polygon(b["poly"]).area)["poly"]] if bal_list else None),
            "balconies": [[[round(x * K, 1), round(y * K, 1)] for x, y in b["poly"]] for b in bal_list],
            "outline": [[round(x * K, 1), round(y * K, 1)] for x, y in outline_pt],
            "tone": tone_index.get(number, 0),
            "label_living": (info or {}).get("living"),
        }

        for bal_idx, bal in enumerate(bal_list):
            bal_poly = bal["poly"]
            bxs = [p[0] for p in bal_poly]; bys = [p[1] for p in bal_poly]
            bal_w = min(max(bxs) - min(bxs), max(bys) - min(bys))
            if bal_w < BALCONY_LABEL_MIN_W_PT:
                report.setdefault("balcony_labels_skipped", []).append(
                    f"{number}#{bal_idx} (узкий балкон {bal_w:.1f}pt < {BALCONY_LABEL_MIN_W_PT}pt)")
                continue
            area_val = bal["text"] if bal["text"] is not None else bal["area_m2"]
            if area_val is None and info:
                area_val = info.get("balcony")
            btxt = (fmt_area(area_val) if area_val is not None else "?") + " m²"
            pb = _balcony_pending(bal_poly, btxt)
            pb.update({"number": number, "idx": bal_idx, "area": area_val})
            pend_bals.append(pb)

    # ---- подписи офисов. СТАРЫЙ блок «офисных подписей по свободным текстам PDF»
    # (OFFICE_FLOORS + fd["texts"], как в --source trace/pdf) здесь ОТКЛЮЧЁН (ревьюер, 2026-09-22):
    # он подписывал те же самые два офиса 2-го этажа вторично и по координате текста, а не по
    # геометрии. Теперь источник один - fd["offices"] из tools/plan-units-from-pdf.py.
    # Кегль как у квартир (LABEL_PX), цвет muted; площади балконов офисов - как у квартир.
    for off_idx, off in enumerate(offices_fd):
        opx, opy = pole_of_inaccessibility(off["poly"])
        olx, oly = opx * K, opy * K
        otxt = f'Office / {fmt_area(off["area"])} m²'
        report.setdefault("offices", []).append(f'{off["area"]}')
        off_export = {"area": off["area"], "x": round(float(olx), 1), "y": round(float(oly), 1),
                      "balconies": []}
        # офис выравнивается в ряд с квартирами (заказчик 22.09); бокс - внутри полигона офиса
        _OP = _Polygon([(x * K, y * K) for x, y in off["poly"]]).buffer(0)
        _ohw, _ohh = _caption_w(otxt, LABEL_PX) / 2 + CAPTION_PAD_PX, LABEL_PX * 0.5 + CAPTION_PAD_PX
        from shapely.geometry import box as _obox
        pend_offs.append({"x": olx, "y": oly, "text": otxt, "export": off_export,
                          "fits": (lambda P, a, b: lambda x, y: P.contains(_obox(x - a, y - b, x + a, y + b)))(_OP, _ohw, _ohh)})
        for b_idx, b in enumerate(off.get("balconies") or []):
            bxs = [q[0] for q in b]; bys = [q[1] for q in b]
            bal_w = min(max(bxs) - min(bxs), max(bys) - min(bys))
            if bal_w < BALCONY_LABEL_MIN_W_PT:
                report.setdefault("balcony_labels_skipped", []).append(
                    f"office{off_idx}#{b_idx} (узкий балкон {bal_w:.1f}pt < {BALCONY_LABEL_MIN_W_PT}pt)")
                continue
            b_area = round(_Polygon(b).area * PT2_TO_M2, 1)
            pb = _balcony_pending(b, f"{fmt_area(b_area)} m²")
            pb.update({"office": off_idx, "idx": b_idx, "area": b_area, "export": off_export})
            pend_bals.append(pb)
        labels_export["offices"].append(off_export)

    # ---- выравнивание (заказчик 2026-09-22: «подписи скачут»): квартиры одного ряда - на одну
    # высоту, балконы одного фасада - на одну линию; кто на общей линии не влезает в свою
    # комнату/балкон - остаётся на своём якоре (в отчёт).
    report["caption_rows"] = _align_rows(pend_units + pend_offs, gap=CAPTION_ROW_GAP_PX, span=CAPTION_ROW_SPAN_PX,
                                         search=CAPTION_ROW_SEARCH_PX, step=4)
    report["balcony_rows"] = _align_rows(pend_bals, gap=BALCONY_ROW_GAP_PX, span=BALCONY_ROW_GAP_PX * 2,
                                         search=BALCONY_ROW_SEARCH_PX, step=2)
    line_h = LABEL_PX * 1.15
    for pu in pend_units:
        lx, ly = pu["x"], pu["y"]
        y1 = ly - line_h / 2 + LABEL_PX * 0.35
        y2 = ly + line_h / 2 + LABEL_PX * 0.35
        labels.append(f'<text x="{lx:.1f}" y="{y1:.1f}" text-anchor="middle" '
                      f'font-family="{font}, serif" font-weight="400" font-size="{LABEL_PX}" '
                      f'fill="{ink}">{pu["line1"]}</text>'
                      f'<text x="{lx:.1f}" y="{y2:.1f}" text-anchor="middle" '
                      f'font-family="{font}, serif" font-weight="400" font-size="{LABEL_PX}" '
                      f'fill="{ink}">{pu["line2"]}</text>')
        labels_export["units"].append(
            {"number": pu["number"], "type": pu["ftype"], "slot": pu["slot"],
             "total": pu["total"], "x": round(float(lx), 1), "y": round(float(ly), 1)})
    for po in pend_offs:
        labels.append(f'<text x="{po["x"]:.1f}" y="{po["y"] + LABEL_PX * 0.35:.1f}" text-anchor="middle" '
                      f'font-family="{font}, serif" font-weight="400" font-size="{LABEL_PX}" '
                      f'fill="{muted}">{po["text"]}</text>')
        po["export"]["x"], po["export"]["y"] = round(float(po["x"]), 1), round(float(po["y"]), 1)
    for pb in pend_bals:
        blx, bly = pb["x"], pb["y"]
        labels.append(f'<text x="{blx:.1f}" y="{bly + BALCONY_LABEL_PX * 0.35:.1f}" text-anchor="middle" '
                      f'font-family="{font}, serif" font-weight="400" font-size="{BALCONY_LABEL_PX}" '
                      f'fill="{muted}">{pb["text"]}</text>')
        if "office" in pb:
            report.setdefault("balcony_labels", []).append(f'office{pb["office"]}#{pb["idx"]}')
            pb["export"]["balconies"].append(
                {"idx": pb["idx"], "area": pb["area"], "x": round(float(blx), 1), "y": round(float(bly), 1)})
        else:
            report.setdefault("caption_anchors_balcony", []).append(
                {"unit": pb["number"], "idx": pb["idx"], "balcony_px": [round(float(blx), 1), round(float(bly), 1)],
                 "source": "floor-F.json"})
            report.setdefault("balcony_labels", []).append(f'{pb["number"]}#{pb["idx"]}')
            labels_export["balconies"].append(
                {"number": pb["number"], "idx": pb["idx"], "area": pb["area"],
                 "x": round(float(blx), 1), "y": round(float(bly), 1)})

    report["labels_export"] = labels_export
    report["units"] = len(units_out)
    paper_rect = f'<rect x="0" y="0" width="{W}" height="{H}" fill="{style["paper"]}"/>'
    # порядок слоёв: бумага -> ячейки владения -> чертёж ревьюера -> подписи
    svg_body = FONT_STYLE + paper_rect + "".join(cell_fill_pieces) + drawing_svg + "".join(labels)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}">{svg_body}</svg>')
    return svg, W, H, units_out


def build_floor_glue(rp, fd, floor_num, style, units_db, assign, cls, drawings, manifest, xpilons, W, H, ink, report):
    """--source glue (task 2026-09-21, the reviewer: сравнение двух методов сборки этажа - этаж 2 идёт
    целиком из PDF (--source pdf), этаж 3 склеивается из готовых чертежей квартир и НИКАКИХ
    стен из второго источника поверх них, потому что второй источник стен давал щели).

    Three sources, cleanly separated - no PDF vector is ever drawn where a Figma final already
    drew a wall:
      1 paper
      2 ownership-cell fill (floor_cells, same construction as --source pdf: wall mask gets the
        confirmed X-pylons folded in via extra_wall_boxes=xpilons)
      3 underlay: core/corridor ONLY - underlay_pdf(..., include_walls=True) (the pre-v4 shape
        of that function - c08/c19 wall fill included, since here it is the ONLY layer allowed
        to draw them), clipped to the floor rect MINUS every unit's poly+balcony (evenodd) - so
        not one PDF pixel lands inside an apartment or its balcony; what survives is core,
        stairs, lifts, corridor/exterior walls, doors (c00, via underlay_pdf), offices
      4 apartment interiors: Figma finals (dr.instance(...), WALLS_ONLY) - the ONLY source of
        an apartment's own walls, including the balcony-wall/pdf_balcony logic --source finals
        already uses for derived members whose balcony doesn't match their base's
      5 X-pylons (_xpilons_pdf): ink, unclipped, drawn LAST - a pylon embedded in the shared
        wall between two apartments would otherwise be split (or missing) between two
        independently-placed final drawings that don't know about each other
      6 captions: apartment (two lines, anchored at the manifest title bbox exactly like
        --source finals), balcony area, office area

    No new heuristics beyond the contract: a white sliver where two neighbouring finals' own
    wall bands don't quite meet is NOT patched here - see tools/floor-gap-check.py and the run's
    own report for how big/how many, on purpose, as the argument in the method comparison."""
    muted = style.get("muted", "#9AA3AD")
    font = style["labels"]["font"]

    adj = rp.build_unit_adjacency(fd["units"], style["floor_render"].get("units_adjacency_tol_pt", 2.5))
    palette = style["floor_render"]["units_fill_palette"]
    tone_index, used = {}, [0] * len(palette)
    for n in sorted(fd["units"], key=lambda x: int(x)):
        taken = {tone_index[m] for m in adj.get(n, ()) if m in tone_index}
        free = [i for i in range(len(palette)) if i not in taken] or list(range(len(palette)))
        t = min(free, key=lambda i: (used[i], i)); tone_index[n] = t; used[t] += 1

    # ---- layer 2: ownership cells (wall mask includes confirmed X-pylons, same as --source pdf)
    cellinfo, cell_checks = None, {}
    try:
        cellinfo = floor_cells(fd, style, extra_wall_boxes=xpilons)
        cell_checks = floor_cells_checks(fd, cellinfo, adj)
        for wmsg in cellinfo["warnings"]:
            report["warnings"].append(wmsg)
    except Exception as e:
        report["warnings"].append(
            f"floor_cells failed: {e} - этаж {floor_num} (--source glue) заливка по poly, без ячеек владения")
    report["cells_checks"] = cell_checks
    report["legacy_fill"] = False
    report["source"] = "glue"

    cell_fill_pieces = []
    for number in sorted(fd["units"], key=lambda n: int(n)):
        poly = fd["units"][number]["poly"]
        cell_pts = (cellinfo or {}).get("cells", {}).get(number) if cellinfo else None
        if not cell_pts:
            report["warnings"].append(f"floor_cells: {number} - нет ячейки, заливка по poly (без балкона)")
            cell_pts = poly
        cell_fill_pieces.append(
            f'<polygon points="{" ".join(f"{x*K:.1f},{y*K:.1f}" for x, y in cell_pts)}" fill="{NEUTRAL_FLOOR}"/>')

    # ---- layer 3: underlay - core/corridor ONLY, PDF walls included (pre-v4 underlay_pdf
    # shape), clipped to floor rect MINUS every unit poly+balcony - never drawn inside an
    # apartment/balcony, so it can never fight a final's own wall for the same pixels.
    under = ""
    try:
        subtract = []
        for number, u in fd["units"].items():
            subtract.append(u["poly"])
            if u.get("balcony"):
                subtract.append(u["balcony"])
        # c21 mullions off: the finals carry all glazing; raw PDF mullions poked past the unit
        # clip as rows of loose ticks along the facade («мишура», diag 21.09)
        body = underlay_pdf(rp, fd, style, include_walls=True, include_mullions=False)
        clip_d = f"M0,0 L{W},0 L{W},{H} L0,{H} Z " + " ".join(_px_path(p) for p in subtract)
        under = (f'<defs><clipPath id="floor-underlay-clip" clip-rule="evenodd">'
                 f'<path d="{clip_d}" clip-rule="evenodd"/></clipPath></defs>'
                 f'<g clip-path="url(#floor-underlay-clip)">'
                 f'<g transform="scale({K})">{body}</g></g>')
    except Exception as e:
        report["warnings"].append(f"underlay failed: {e} - floor drawn without underlay")
        print(f"  ! underlay failed: {e} - floor drawn without underlay")

    # ---- layer 4: apartment interiors, Figma finals only - the sole source of apartment walls
    color = NEUTRAL_FLOOR  # WALLS_ONLY is a module constant; kept explicit for readability here
    interior_pieces, labels, units_out = [], [], {}
    for number in sorted(fd["units"], key=lambda n: int(n)):
        u = fd["units"][number]
        poly, balcony = u["poly"], u.get("balcony")
        info = units_db.get(number)
        a = assign.get(number)
        if not a:
            report["skipped"].append(f"{number} (no coverage class)")
            continue
        klass = a["class"]
        base = (cls.get(klass) or {}).get("base", klass)
        edit = (cls.get(klass) or {}).get("edit", "")
        derived = (klass != base) or (edit and edit != "база") or (a.get("strict") is False)
        dr = drawings.get(base)
        if dr is None:
            report["skipped"].append(f"{number} (no final-svg for base {base})")
            continue
        origin = frame_origin(base)
        if origin is None:
            report["skipped"].append(f"{number} (no v3/plans/unit-{base}.json)")
            continue
        base_floor = (units_db.get(base) or {}).get("floor")
        base_poly = None
        if base_floor is not None:
            try:
                base_poly = json.loads((DATA_DIR / f"floor-{base_floor}.json").read_text(encoding="utf-8"))["units"][base]["poly"]
            except Exception:
                base_poly = None
        if base_poly is None:
            report["skipped"].append(f"{number} (no floor polygon for base {base})")
            continue

        bx0, by0 = bbox([(x, y) for x, y in base_poly])[:2]
        mx0, my0 = bbox([(x, y) for x, y in poly])[:2]
        tx = (origin[0] + (mx0 - bx0)) * K
        ty = (origin[1] + (my0 - by0)) * K

        if dr.ink_bbox:
            ib = [dr.ink_bbox[0] + tx, dr.ink_bbox[1] + ty, dr.ink_bbox[2] + tx, dr.ink_bbox[3] + ty]
            fb = bbox([(x * K, y * K) for x, y in (poly + (balcony or []))])
            res = [ib[0] - fb[0] + WALL_BAND, ib[1] - fb[1] + WALL_BAND,
                   ib[2] - fb[2] - WALL_BAND, ib[3] - fb[3] - WALL_BAND]
            # task B (the reviewer 21.09, автопосадка): a residual where L/T/R/B all read as ONE uniform
            # translation (not a size mismatch, which pushes L and R - or T and B - in OPPOSITE
            # directions, e.g. 311's R=41 with L~0) gets nudged out here instead of left as a
            # permanent WARN. dx/dy = the mean of each opposite pair - the signed amount the
            # final's ink is offset past the expected WALL_BAND from the unit polygon; shifting
            # the final by (-dx,-dy) cancels it.
            L, T, R, B = res
            if abs(L) <= AUTOFIT_MAX_LT and abs(T) <= AUTOFIT_MAX_LT \
                    and abs(L - R) <= AUTOFIT_AGREE_TOL and abs(T - B) <= AUTOFIT_AGREE_TOL:
                dx, dy = (L + R) / 2.0, (T + B) / 2.0
                if abs(dx) > 0.05 or abs(dy) > 0.05:
                    tx -= dx
                    ty -= dy
                    report.setdefault("autofit", []).append((number, round(dx, 1), round(dy, 1)))
                    ib = [dr.ink_bbox[0] + tx, dr.ink_bbox[1] + ty, dr.ink_bbox[2] + tx, dr.ink_bbox[3] + ty]
                    res = [ib[0] - fb[0] + WALL_BAND, ib[1] - fb[1] + WALL_BAND,
                           ib[2] - fb[2] - WALL_BAND, ib[3] - fb[3] - WALL_BAND]
            worst = max(abs(v) for v in res)
            report["align"].append((number, base, derived, [round(v, 1) for v in res]))
            if worst > ALIGN_TOL:
                tag = f"производный класс, база {base}" if derived else "БАЗОВЫЙ КЛАСС"
                report["warn_align"].append(
                    f"WARN align {number} (класс {klass} -> база {base}, {tag}): "
                    f"невязка L/T/R/B = {[round(v,1) for v in res]} px (порог {ALIGN_TOL})")
        if derived:
            report["derived"].append(f"{number} <- класс {klass}, база {base} ({edit or 'нестрогий'})")

        ftype = (info or {}).get("type")
        base_bal = None
        try:
            base_bal = json.loads((DATA_DIR / f"floor-{base_floor}.json").read_text(encoding="utf-8"))["units"][base].get("balcony")
        except Exception:
            pass
        bal_delta = 0.0
        if bool(balcony) != bool(base_bal):
            bal_delta = 999.0
        elif balcony and base_bal:
            bm = bbox([(x, y) for x, y in balcony]); bbs = bbox([(x, y) for x, y in base_bal])
            bal_delta = max(abs(bm[0] - (bbs[0] + (mx0 - bx0))), abs(bm[1] - (bbs[1] + (my0 - by0))),
                            abs(bm[2] - (bbs[2] + (mx0 - bx0))), abs(bm[3] - (bbs[3] + (my0 - by0))))
        pdf_balcony = derived and bal_delta > BALCONY_DELTA_PT
        drop_region = None
        if pdf_balcony and base_bal:
            bbs = bbox([(x, y) for x, y in base_bal])
            drop_region = [(bbs[0] - origin[0]) * K - 25, (bbs[1] - origin[1]) * K - 25, (bbs[2] - origin[0]) * K + 25, (bbs[3] - origin[1]) * K + 25]
        if pdf_balcony:
            report.setdefault("pdf_balcony", []).append(f"{number} (Δ{bal_delta:.1f}pt)")
        interior_svg = (f'<g id="unit-{number}" fill="none" transform="translate({tx:.2f},{ty:.2f})">'
                        + dr.instance(number, color, drop_balcony=(drop_region or True) if pdf_balcony else False, ink=ink) + "</g>")
        interior_pieces.append(interior_svg)
        # derived member whose balcony doesn't match the base's - same PDF-balcony wall band as
        # --source finals (task B): a WALL_BAND-wide ink band along the MEMBER's own PDF balcony
        # polygon, replacing the base's (dropped) glazing/parapet layers.
        if pdf_balcony and balcony:
            try:
                # band along the member's own PDF balcony edges except the threshold (same helper as
                # --source pdf), drawn as an evenodd <path> - exterior-only <polygon> filled the whole
                # balcony solid when the band closed into a ring (floor 20, 21.09)
                band_polys, _ba, _bal = _balcony_wall_band(balcony, fd["units"][number]["poly"], WALL_BAND / K)
                for g in band_polys:
                    if g.is_empty:
                        continue
                    interior_pieces.append(f'<path d="{_poly_path_px(g)}" fill="{ink}" fill-rule="evenodd"/>')
            except Exception as e:
                report["warnings"].append(f"{number}: парапет PDF-балкона (10px band) не построен - {e}")

        cell_pts = (cellinfo or {}).get("cells", {}).get(number) if cellinfo else None
        if not cell_pts:
            cell_pts = poly

        # caption: manifest title-bbox anchor, same as --source finals
        if info:
            t = manifest.get(base, {}).get("title")
            if t:
                lx, ly = tx + t["x"] + t["w"] / 2, ty + t["y"] + t["h"] / 2
            else:
                px, py = pole_of_inaccessibility(poly)
                lx, ly = px * K, py * K
            line1 = f'{TYPE_LABEL.get(ftype, "")} {info["slot"]}'.strip()
            line2 = f'{fmt_area(info["total"])} m²'
            line_h = LABEL_PX * 1.15
            y1 = ly - line_h / 2 + LABEL_PX * 0.35
            y2 = ly + line_h / 2 + LABEL_PX * 0.35
            labels.append(f'<text x="{lx:.1f}" y="{y1:.1f}" text-anchor="middle" '
                          f'font-family="{font}, serif" font-weight="400" font-size="{LABEL_PX}" '
                          f'fill="{ink}">{line1}</text>'
                          f'<text x="{lx:.1f}" y="{y2:.1f}" text-anchor="middle" '
                          f'font-family="{font}, serif" font-weight="400" font-size="{LABEL_PX}" '
                          f'fill="{ink}">{line2}</text>')
        else:
            report["skipped"].append(f"{number} (нет записи в units_db - без подписи)")

        outline_pt = cell_pts if cell_pts is not poly else unit_outline(poly, balcony)
        units_out[number] = {
            "poly": [[round(x * K, 1), round(y * K, 1)] for x, y in poly],
            "balcony": [[round(x * K, 1), round(y * K, 1)] for x, y in balcony] if balcony else None,
            "outline": [[round(x * K, 1), round(y * K, 1)] for x, y in outline_pt],
            "tone": tone_index.get(number, 0),
            "label_living": (info or {}).get("living"),
        }

        if balcony and info and info.get("balcony") is not None:
            bxs = [p[0] for p in balcony]; bys = [p[1] for p in balcony]
            bal_w = min(max(bxs) - min(bxs), max(bys) - min(bys))
            if bal_w < BALCONY_LABEL_MIN_W_PT:
                report.setdefault("balcony_labels_skipped", []).append(
                    f"{number} (узкий балкон {bal_w:.1f}pt < {BALCONY_LABEL_MIN_W_PT}pt)")
            else:
                bpx, bpy = pole_of_inaccessibility(balcony)
                blx, bly = bpx * K, bpy * K
                btxt = fmt_area(info["balcony"]) + " m²"
                labels.append(f'<text x="{blx:.1f}" y="{bly + BALCONY_LABEL_PX * 0.35:.1f}" text-anchor="middle" '
                              f'font-family="{font}, serif" font-weight="400" font-size="{BALCONY_LABEL_PX}" '
                              f'fill="{muted}">{btxt}</text>')
                report.setdefault("balcony_labels", []).append(number)

    # non-residential zones (offices on floor 2): same PDF area-text scan as --source finals/pdf
    for t in fd.get("texts", []):
        try:
            v = float(t["str"])
        except ValueError:
            continue
        if t.get("size", 0) < 7 or v < TEXT_MIN_AREA or "." not in t["str"]:
            continue
        if floor_num not in OFFICE_FLOORS:
            continue
        if any(point_in_poly((t["x"], t["y"]), u["poly"]) for u in fd["units"].values()):
            continue
        lx, ly = (t["x"] + 8) * K, t["y"] * K
        labels.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" font-family="{font}, serif" '
                      f'font-weight="400" font-size="{OFFICE_LABEL_PX}" fill="{muted}">Office / {fmt_area(v)} m²</text>')
        report.setdefault("offices", []).append(f"{v}")

    # ---- layer 5: X-pylons only (ink, unclipped, on top) - c08/c19 wall fill is NOT repeated
    # here, it already came from the clipped underlay (layer 3) - only the pylon draw is PDF-only
    # and sits, by the contract, on top of everything (task A: "несущих местами нет").
    report["xpilons"] = xpilons
    xpilon_svg = "".join(
        f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}" fill="{ink}" stroke="none"/>'
        for x0, y0, x1, y1 in xpilons)
    structure_svg = f'<g id="structure" transform="scale({K})">{xpilon_svg}</g>'

    report["units"] = len(units_out)
    paper_rect = f'<rect x="0" y="0" width="{W}" height="{H}" fill="{style["paper"]}"/>'
    svg_body = (FONT_STYLE + paper_rect + "".join(cell_fill_pieces) + under
                + "".join(interior_pieces) + structure_svg + "".join(labels))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}">{svg_body}</svg>')
    return svg, W, H, units_out


# ---------------------------------------------------------------- one floor
def build_floor(rp, floor_num, style, units_db, assign, cls, drawings, manifest, report, underlay_mode="v15", legacy_fill=False, source="pdf", figma_collect=None, figma_in=None):
    fd = rp.load_floor_data(floor_num)
    # task A (FLOOR-CONTRACT v4, the reviewer 21.09 «несущих местами нет»): X-pylons the PDF draws as
    # a white c06/c06_furn rectangle + two diagonal 2-point lines (c02/c03/c07) rather than as
    # c19 structural fill - see _xpilons_pdf. Any confirmed pylon that DID make it into
    # fd["_columns"] is pulled back out here so _classify_columns_pdf (below, via
    # underlay_pdf) never misreads its shape as a stair tread.
    xpilons, xpilons_suspicious = _xpilons_pdf(rp, fd)
    report["xpilons"] = xpilons
    report["xpilons_suspicious"] = xpilons_suspicious
    if xpilons:
        xpset = {(round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)) for x0, y0, x1, y1 in xpilons}
        fd["_columns"] = [b for b in (fd.get("_columns") or []) if tuple(round(v, 1) for v in b) not in xpset]
    w, h = fd["w"], fd["h"]
    W, H = round(w * K), round(h * K)
    ink = style["ink"]

    if source == "pdf":
        # FLOOR-CONTRACT task 2026-09-21: assembled ONLY from PDF vectors (fd) + floor-F.json -
        # no final-svg, no UnitDrawing/dr.instance(). --legacy-fill is a --source finals concept
        # (pre-v4 per-unit poly+balcony fill vs v4 ownership cells) and has no meaning here.
        if legacy_fill:
            report["warnings"].append(
                "--legacy-fill игнорируется при --source pdf (относится только к --source finals)")
        return build_floor_pdf(rp, fd, floor_num, style, units_db, xpilons, W, H, ink, report)

    if source == "trace":
        # FLOOR-TRACE-SPEC.md (эксперимент 2026-09-21, ревьюер): та же PDF-only сборка, что
        # --source pdf, но с более детальными окнами/дверями/балконными стенками своим языком
        # чертежа. --legacy-fill не имеет смысла (v4-only ячейки владения).
        if legacy_fill:
            report["warnings"].append(
                "--legacy-fill игнорируется при --source trace (относится только к --source finals)")
        return build_floor_trace(rp, fd, floor_num, style, units_db, xpilons, W, H, ink, report, figma_collect=figma_collect)

    if source == "figma":
        # --source figma (ревьюер, 2026-09-21: чертёж этажа - ручная чистка в Figma, сборщик
        # добавляет ячейки/подписи). Чертёж берётся ГОТОВЫМ из figma_in, ни один детектор
        # (X-пилоны, двери, окна, ядро, подстановка балконов) не участвует. --legacy-fill,
        # как и в pdf/trace/glue, относится только к --source finals.
        if legacy_fill:
            report["warnings"].append(
                "--legacy-fill игнорируется при --source figma (относится только к --source finals)")
        return build_floor_figma(rp, fd, floor_num, style, units_db, W, H, ink, report, figma_in)

    if source == "glue":
        # FLOOR-CONTRACT task 2026-09-21 (the reviewer, сравнение методов): apartments are Figma finals
        # (the only source of their own walls); PDF draws only core/corridor (clipped to outside
        # every apartment+balcony) and, unclipped, the confirmed X-pylons. --legacy-fill is a
        # --source finals concept and has no meaning here either.
        if legacy_fill:
            report["warnings"].append(
                "--legacy-fill игнорируется при --source glue (относится только к --source finals)")
        return build_floor_glue(rp, fd, floor_num, style, units_db, assign, cls, drawings, manifest, xpilons, W, H, ink, report)

    palette = style["floor_render"]["units_fill_palette"]
    _uc = rp.assign_unit_fill_colors(fd["units"], palette, style["floor_render"].get("units_adjacency_tol_pt", 2.5))
    unit_colors = _uc[0] if isinstance(_uc, tuple) else _uc
    # chessboard tone for the GUI: greedy over the adjacency graph, preferring the least-used
    # tone among those free (rp's greedy takes the first free one -> only two tones on a ring)
    adj = rp.build_unit_adjacency(fd["units"], style["floor_render"].get("units_adjacency_tol_pt", 2.5))
    tone_index, used = {}, [0] * len(palette)
    for n in sorted(fd["units"], key=lambda x: int(x)):
        taken = {tone_index[m] for m in adj.get(n, ()) if m in tone_index}
        free = [i for i in range(len(palette)) if i not in taken] or list(range(len(palette)))
        t = min(free, key=lambda i: (used[i], i)); tone_index[n] = t; used[t] += 1
    font = style["labels"]["font"]

    # ---- v4 ownership cells (FLOOR-CONTRACT.md): skipped entirely under --legacy-fill, which
    # reproduces the pre-v4 pipeline (per-unit poly+balcony fill, underlay draws c08/c19 itself,
    # clipped, no separate structure layer, outline = unit_outline()'s poly-union-balcony).
    cellinfo, cell_checks = None, {}
    if not legacy_fill:
        try:
            cellinfo = floor_cells(fd, style)
            cell_checks = floor_cells_checks(fd, cellinfo, adj)
            for wmsg in cellinfo["warnings"]:
                report["warnings"].append(wmsg)
        except Exception as e:
            report["warnings"].append(
                f"floor_cells failed: {e} - этаж {floor_num} собран через --legacy-fill (не по контракту v4)")
            legacy_fill = True
    report["legacy_fill"] = legacy_fill
    report["cells_checks"] = cell_checks

    # ---- underlay: the accepted v15 floor render (plan-studio/data/floor-underlay/floor-F.svg,
    # pt coordinates, same origin as floor-F.json) minus its synthesized wall bands (4-point ink
    # polygons - the Figma finals carry every apartment wall) and unit labels, clipped to the
    # building hull minus every apartment/balcony polygon: what survives is the core (stairs,
    # lifts, shafts), the corridors and the exterior where no apartment sits.
    under = ""
    hull_pt = None
    try:
        subtract = []
        for number, u in fd["units"].items():
            subtract.append(u["poly"])
            if u.get("balcony"):
                subtract.append(u["balcony"])
        upath = DATA_DIR / "floor-underlay" / f"floor-{floor_num}.svg"
        if underlay_mode == "pdf":
            body = underlay_pdf(rp, fd, style, include_walls=legacy_fill)
        else:
            body = underlay_v15(upath.read_text(encoding="utf-8"), floor_num)
            if not legacy_fill:
                # underlay_v15 works on an already-flattened SVG string (no per-element role
                # tagging survives), so c08/c19 can't be reliably stripped out of it the way
                # underlay_pdf's include_walls=False does - v4 + --underlay v15 will double
                # draw walls (clipped v15 copy + unclipped structure layer on top, same ink,
                # so it's a redundant overlap rather than a visible defect, but it's not the
                # clean separation the contract asks for).
                report["warnings"].append(
                    "v4: --underlay v15 не поддерживает вырезание c08/c19 из подложки (только "
                    "--underlay pdf, дефолт) - стены на этом этаже дублируются (подложка + structure)")
        hull = None
        m = re.search(r'<clipPath id="building-clip"><polygon points="([^"]*)"', upath.read_text(encoding="utf-8"))
        if m and underlay_mode != "pdf":
            # v15 only: its body carries page junk outside the building. The pdf underlay draws
            # walls/doors/stairs alone, and the v15 convex hull cut off the floor-2 offices.
            hull = [tuple(float(v) for v in pt.split(",")) for pt in m.group(1).split()]
        hull_pt = hull
        if hull:
            clip_d = _px_path(hull) + " " + " ".join(_px_path(p) for p in subtract)
        else:
            clip_d = f"M0,0 L{W},0 L{W},{H} L0,{H} Z " + " ".join(_px_path(p) for p in subtract)
        under = (f'<defs><clipPath id="floor-underlay-clip" clip-rule="evenodd">'
                 f'<path d="{clip_d}" clip-rule="evenodd"/></clipPath></defs>'
                 f'<g clip-path="url(#floor-underlay-clip)">'
                 f'<g transform="scale({K})">{body}</g></g>')
    except Exception as e:
        report["warnings"].append(f"underlay failed: {e} - floor drawn without underlay")
        print(f"  ! underlay failed: {e} - floor drawn without underlay")

    # ---- apartments
    muted = style.get("muted", "#9AA3AD")
    # legacy: one interleaved z-order list per unit (fill, balcony fill, interior). v4: two
    # separate layers - `cell_fill_pieces` (layer 2, all units, under the underlay) and
    # `interior_pieces` (layer 4, all units, over the underlay); `pieces` stays the legacy name
    # so --legacy-fill's assembly code below is untouched.
    pieces, cell_fill_pieces, interior_pieces, labels, units_out = [], [], [], [], {}
    for number in sorted(fd["units"], key=lambda n: int(n)):
        u = fd["units"][number]
        poly, balcony = u["poly"], u.get("balcony")
        info = units_db.get(number)
        a = assign.get(number)
        if not a:
            report["skipped"].append(f"{number} (no coverage class)")
            continue
        klass = a["class"]
        base = (cls.get(klass) or {}).get("base", klass)
        edit = (cls.get(klass) or {}).get("edit", "")
        derived = (klass != base) or (edit and edit != "база") or (a.get("strict") is False)
        dr = drawings.get(base)
        if dr is None:
            report["skipped"].append(f"{number} (no final-svg for base {base})")
            continue
        origin = frame_origin(base)
        if origin is None:
            report["skipped"].append(f"{number} (no v3/plans/unit-{base}.json)")
            continue
        base_floor = (units_db.get(base) or {}).get("floor")
        base_poly = None
        if base_floor is not None:
            try:
                base_poly = json.loads((DATA_DIR / f"floor-{base_floor}.json").read_text(encoding="utf-8"))["units"][base]["poly"]
            except Exception:
                base_poly = None
        if base_poly is None:
            report["skipped"].append(f"{number} (no floor polygon for base {base})")
            continue

        bx0, by0 = bbox([(x, y) for x, y in base_poly])[:2]
        mx0, my0 = bbox([(x, y) for x, y in poly])[:2]
        tx = (origin[0] + (mx0 - bx0)) * K
        ty = (origin[1] + (my0 - by0)) * K

        # alignment self-check: ink bbox (walls) vs the unit footprint, minus the systematic
        # WALL_BAND px the drawings put outside the polygon on every side
        if dr.ink_bbox:
            ib = [dr.ink_bbox[0] + tx, dr.ink_bbox[1] + ty, dr.ink_bbox[2] + tx, dr.ink_bbox[3] + ty]
            fb = bbox([(x * K, y * K) for x, y in (poly + (balcony or []))])
            res = [ib[0] - fb[0] + WALL_BAND, ib[1] - fb[1] + WALL_BAND,
                   ib[2] - fb[2] - WALL_BAND, ib[3] - fb[3] - WALL_BAND]
            worst = max(abs(v) for v in res)
            report["align"].append((number, base, derived, [round(v, 1) for v in res]))
            if worst > ALIGN_TOL:
                tag = f"производный класс, база {base}" if derived else "БАЗОВЫЙ КЛАСС"
                report["warn_align"].append(
                    f"WARN align {number} (класс {klass} -> база {base}, {tag}): "
                    f"невязка L/T/R/B = {[round(v,1) for v in res]} px (порог {ALIGN_TOL})")
        if derived:
            report["derived"].append(f"{number} <- класс {klass}, база {base} ({edit or 'нестрогий'})")

        ftype = (info or {}).get("type")
        color = NEUTRAL_FLOOR if WALLS_ONLY else unit_colors.get(number, palette[0])
        # derived member whose balcony is not the base's balcony (shifted by the same translation):
        # drop the base's glazing/parapet and draw a parapet along the PDF balcony polygon instead
        base_bal = None
        try:
            base_bal = json.loads((DATA_DIR / f"floor-{base_floor}.json").read_text(encoding="utf-8"))["units"][base].get("balcony")
        except Exception:
            pass
        bal_delta = 0.0
        if bool(balcony) != bool(base_bal):
            bal_delta = 999.0
        elif balcony and base_bal:
            bm = bbox([(x, y) for x, y in balcony]); bbs = bbox([(x, y) for x, y in base_bal])
            bal_delta = max(abs(bm[0] - (bbs[0] + (mx0 - bx0))), abs(bm[1] - (bbs[1] + (my0 - by0))),
                            abs(bm[2] - (bbs[2] + (mx0 - bx0))), abs(bm[3] - (bbs[3] + (my0 - by0))))
        pdf_balcony = derived and bal_delta > BALCONY_DELTA_PT
        drop_region = None
        if pdf_balcony and base_bal:
            bbs = bbox([(x, y) for x, y in base_bal])
            drop_region = [(bbs[0] - origin[0]) * K - 25, (bbs[1] - origin[1]) * K - 25, (bbs[2] - origin[0]) * K + 25, (bbs[3] - origin[1]) * K + 25]
        if pdf_balcony:
            report.setdefault("pdf_balcony", []).append(f"{number} (Δ{bal_delta:.1f}pt)")
        interior_svg = (f'<g id="unit-{number}" fill="none" transform="translate({tx:.2f},{ty:.2f})">'
                        + dr.instance(number, color, drop_balcony=(drop_region or True) if pdf_balcony else False, ink=ink) + "</g>")
        if legacy_fill:
            if WALLS_ONLY:
                # the finals' «Пол» carries furniture as holes (raster floor, rule 28b) - use the unit footprint instead
                pieces.append(f'<polygon points="{" ".join(f"{x*K:.1f},{y*K:.1f}" for x, y in poly)}" fill="{color}"/>')
            if balcony:
                stroke = f' stroke="{ink}" stroke-width="1.2" stroke-linejoin="miter"' if pdf_balcony else ''
                pieces.append(f'<polygon points="{" ".join(f"{x*K:.1f},{y*K:.1f}" for x, y in balcony)}" '
                              f'fill="{color}" fill-opacity="{BALCONY_ALPHA}"{stroke}/>')
            pieces.append(interior_svg)
        else:
            # v4 contract § «Сборка этажа» layer 2: one uniform cell fill per apartment - no
            # separate poly/balcony fills at BALCONY_ALPHA any more (symptom #4).
            cell_pts = (cellinfo or {}).get("cells", {}).get(number) if cellinfo else None
            if not cell_pts:
                report["warnings"].append(f"floor_cells: {number} - нет ячейки, заливка по poly (без балкона)")
                cell_pts = poly
            cell_fill_pieces.append(
                f'<polygon points="{" ".join(f"{x*K:.1f},{y*K:.1f}" for x, y in cell_pts)}" fill="{NEUTRAL_FLOOR}"/>')
            interior_pieces.append(interior_svg)
            # task B, derived balcony: dr.instance() above already dropped the BASE's own
            # "окно балкона"/etc inside the base balcony zone (drop_balcony=True), since this
            # member's own balcony polygon doesn't match the base's - draw a 10px ink band
            # along the MEMBER's own PDF balcony polygon instead (WALL_BAND, same width as
            # every other wall here), replacing the old legacy-only stroke outline.
            if pdf_balcony and balcony:
                try:
                    # band along the member's own PDF balcony edges except the threshold (same helper as
                    # --source pdf), drawn as an evenodd <path> - a plain exterior-only <polygon> filled the
                    # whole balcony solid when the band's union closed into a ring (floor 20, 21.09)
                    band_polys, _ba, _bal = _balcony_wall_band(balcony, fd["units"][number]["poly"], WALL_BAND / K)
                    for g in band_polys:
                        if g.is_empty:
                            continue
                        interior_pieces.append(f'<path d="{_poly_path_px(g)}" fill="{ink}" fill-rule="evenodd"/>')
                except Exception as e:
                    report["warnings"].append(f"{number}: парапет PDF-балкона (10px band) не построен - {e}")

        # caption
        if info:
            t = manifest.get(base, {}).get("title")
            if t:
                lx, ly = tx + t["x"] + t["w"] / 2, ty + t["y"] + t["h"] / 2
            else:
                px, py = pole_of_inaccessibility(poly)
                lx, ly = px * K, py * K
            # task C (the reviewer 21.09): two lines - «вид квартиры» then, below it, «площадь» - not
            # one «1BR 4 / 88.2 m²» line. Same font/size both lines, 1.15·LABEL_PX leading,
            # block centred on the same anchor (lx,ly) the single-line caption used.
            line1 = f'{TYPE_LABEL.get(ftype, "")} {info["slot"]}'.strip()
            line2 = f'{fmt_area(info["total"])} m²'
            line_h = LABEL_PX * 1.15
            y1 = ly - line_h / 2 + LABEL_PX * 0.35
            y2 = ly + line_h / 2 + LABEL_PX * 0.35
            labels.append(f'<text x="{lx:.1f}" y="{y1:.1f}" text-anchor="middle" '
                          f'font-family="{font}, serif" font-weight="400" font-size="{LABEL_PX}" '
                          f'fill="{ink}">{line1}</text>'
                          f'<text x="{lx:.1f}" y="{y2:.1f}" text-anchor="middle" '
                          f'font-family="{font}, serif" font-weight="400" font-size="{LABEL_PX}" '
                          f'fill="{ink}">{line2}</text>')

        # outline: legacy = unit_outline() (poly ∪ balcony.buffer(0.6), unchanged); v4 = the
        # ownership cell computed above (falls back to unit_outline() if floor_cells missed
        # this unit, same as the cell-fill fallback a few lines up, logged there already).
        if legacy_fill:
            outline_pt = unit_outline(poly, balcony)
        else:
            outline_pt = cell_pts if cell_pts is not poly else unit_outline(poly, balcony)
        units_out[number] = {
            "poly": [[round(x * K, 1), round(y * K, 1)] for x, y in poly],
            "balcony": [[round(x * K, 1), round(y * K, 1)] for x, y in balcony] if balcony else None,
            "outline": [[round(x * K, 1), round(y * K, 1)] for x, y in outline_pt],
            "tone": tone_index.get(number, 0),
            "label_living": (info or {}).get("living"),
        }

        # v4 § «Сборка этажа» layer 6: balcony caption (area from units_db, fmt_area + " m²"),
        # placed at the raw PDF balcony polygon's pole of inaccessibility. Skipped (and
        # reported) when the balcony's narrow dimension is under BALCONY_LABEL_MIN_W_PT - a
        # sliver too thin to hold 30px text without spilling into the apartment label.
        if not legacy_fill and balcony and info and info.get("balcony") is not None:
            bxs = [p[0] for p in balcony]; bys = [p[1] for p in balcony]
            bal_w = min(max(bxs) - min(bxs), max(bys) - min(bys))
            if bal_w < BALCONY_LABEL_MIN_W_PT:
                report.setdefault("balcony_labels_skipped", []).append(
                    f"{number} (узкий балкон {bal_w:.1f}pt < {BALCONY_LABEL_MIN_W_PT}pt)")
            else:
                bpx, bpy = pole_of_inaccessibility(balcony)
                blx, bly = bpx * K, bpy * K
                btxt = fmt_area(info["balcony"]) + " m²"
                labels.append(f'<text x="{blx:.1f}" y="{bly + BALCONY_LABEL_PX * 0.35:.1f}" text-anchor="middle" '
                              f'font-family="{font}, serif" font-weight="400" font-size="{BALCONY_LABEL_PX}" '
                              f'fill="{muted}">{btxt}</text>')
                report.setdefault("balcony_labels", []).append(number)

    # non-residential zones (offices on floor 2): the PDF's own area labels that sit in no apartment
    for t in fd.get("texts", []):
        try:
            v = float(t["str"])
        except ValueError:
            continue
        if t.get("size", 0) < 7 or v < TEXT_MIN_AREA or "." not in t["str"]:
            continue
        if floor_num not in OFFICE_FLOORS:
            continue
        if any(point_in_poly((t["x"], t["y"]), u["poly"]) for u in fd["units"].values()):
            continue
        lx, ly = (t["x"] + 8) * K, t["y"] * K
        labels.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" font-family="{font}, serif" '
                      f'font-weight="400" font-size="{OFFICE_LABEL_PX}" fill="{muted}">Office / {fmt_area(v)} m²</text>')
        report.setdefault("offices", []).append(f"{v}")
    report["units"] = len(units_out)
    paper_rect = f'<rect x="0" y="0" width="{W}" height="{H}" fill="{style["paper"]}"/>'
    if legacy_fill:
        svg_body = FONT_STYLE + paper_rect + under + "".join(pieces) + "".join(labels)
    else:
        # v4 § «Сборка этажа»: 1 paper, 2 cell fill (all units), 3 underlay (no c08/c19,
        # clipped to hull-minus-units as before), 4 interiors (all units), 5 structure
        # (c08+c19, unclipped, own group, drawn on top of everything else), 6 labels.
        xpilon_svg = "".join(
            f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}" fill="{ink}" stroke="none"/>'
            for x0, y0, x1, y1 in xpilons)
        structure_svg = (f'<g id="structure" transform="scale({K})">{_fill_cluster_svg(fd, "c08", ink)}'
                          f'{_fill_cluster_svg(fd, "c19", ink)}{xpilon_svg}</g>')
        svg_body = (FONT_STYLE + paper_rect + "".join(cell_fill_pieces) + under
                    + "".join(interior_pieces) + structure_svg + "".join(labels))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}">{svg_body}</svg>')
    return svg, W, H, units_out


# ---------------------------------------------------------------- render
async def rasterize(page, svg, W, H, transparent=False):
    bg = "transparent" if transparent else "#fff"
    html = ('<!doctype html><html><head><meta charset="utf-8">'
            '<link rel="preconnect" href="https://fonts.googleapis.com">'
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            '<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:wght@400&display=swap" rel="stylesheet">'
            '<style>html,body{margin:0;padding:0;background:' + bg + '}svg{display:block}</style>'
            '</head><body>' + svg + '</body></html>')
    await page.set_viewport_size({"width": min(W, 4000), "height": min(H, 4000)})
    await page.set_content(html, wait_until="load")
    try:
        await page.evaluate("document.fonts.load('16px \"Instrument Serif\"').then(()=>document.fonts.ready)")
    except Exception:
        pass
    await page.wait_for_timeout(200)
    return await page.locator("svg").screenshot(omit_background=transparent)


async def run(args):
    rp = load_render_plans()
    style, units_db, assign, cls = load_inputs()

    drawings, manifest = {}, {}
    if args.source == "pdf":
        # --source pdf (FLOOR-CONTRACT task 2026-09-21): apartments are PDF vectors, not Figma
        # finals - final-svg/manifest.json and unit-*.svg are never read, drawings stays {}.
        print("--source pdf: final-svg не читается, квартиры собираются из PDF-векторов")
    else:
        svg_dir = Path(args.svg_dir) if args.svg_dir else SVG_DIR_DEFAULT
        manifest, mk = load_manifest(svg_dir)
        if mk and abs(mk - K) > 1e-6:
            print(f"note: manifest k={mk} differs from K={K}; using K={K}")

        missing = []
        bases = sorted({c["base"] for c in cls.values()}, key=int)
        for b in bases:
            p = svg_dir / f"unit-{b}.svg"
            if not p.exists():
                missing.append(b); continue
            try:
                drawings[b] = UnitDrawing(b, p)
            except Exception as e:
                missing.append(f"{b} ({e})")
        print(f"final-svg: {len(drawings)}/{len(bases)} баз загружено" + (f"; НЕТ: {', '.join(map(str, missing))}" if missing else ""))
        if not manifest:
            print("manifest.json отсутствует - подписи ставятся по полюсу недоступности полигона")

    out_dir = Path(args.out) if args.out else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    all_floors = json.loads((DATA_DIR / "index.json").read_text(encoding="utf-8"))["floors"]
    floors = all_floors if args.all else ([args.floor] if args.floor is not None else [])
    if not floors:
        print("нечего делать: укажи --floor N или --all"); return

    from playwright.async_api import async_playwright
    floors_out, reports = {}, []
    async with async_playwright() as pw:
        # --labels-only: растеризации нет, chromium не поднимаем (прогон идёт секунды)
        browser = None if args.labels_only else await pw.chromium.launch()
        page = None if browser is None else await browser.new_page(device_scale_factor=1)
        for fn in floors:
            rep = {"floor": fn, "units": 0, "skipped": [], "derived": [], "warn_align": [], "align": [], "warnings": [],
                   "legacy_fill": args.legacy_fill, "source": args.source, "cells_checks": {}, "balcony_labels": [], "balcony_labels_skipped": []}
            figma_collect = {"walls": [], "windows": [], "doors": [], "balconies": [], "core": []} if (args.figma_svg and args.source == "trace") else None
            if args.figma_svg and args.source != "trace":
                print(f"! --figma-svg игнорируется: --source {args.source} (нужен --source trace)")
            # --source figma (ревьюер, 2026-09-21: чертёж этажа - ручная чистка в Figma, сборщик
            # добавляет ячейки/подписи): путь свой на каждый этаж ({F} -> номер). Нет файла -
            # этаж просто пропускаем (в plans.json/plans.js он остаётся прежним), не падаем.
            figma_in = None
            if args.source == "figma":
                figma_in = Path((args.figma_in or FIGMA_IN_TMPL).format(F=fn))
                if not figma_in.is_absolute():
                    figma_in = ROOT / figma_in
                if not figma_in.exists():
                    msg = f"--source figma: нет чертежа {figma_in} - этаж {fn} пропущен (экспортируй его из Figma)"
                    rep["warnings"].append(msg)
                    print("! " + msg)
                    reports.append(rep); continue
            try:
                svg, W, H, units_out = build_floor(rp, fn, style, units_db, assign, cls, drawings, manifest, rep, underlay_mode=args.underlay, legacy_fill=args.legacy_fill, source=args.source, figma_collect=figma_collect, figma_in=figma_in)
            except Exception as e:
                rep["warnings"].append(f"build_floor failed: {e}")
                print(f"этаж {fn}: СБОРКА УПАЛА - {e}")
                reports.append(rep); continue
            # --labels-only без явного --out ничего на диск из чертежа не кладёт (чтобы не
            # трогать боевые floor-F.svg рядом с идущей полной пересборкой)
            if not (args.labels_only and not args.out):
                (out_dir / f"floor-{fn}.svg").write_text(svg, encoding="utf-8")
            if args.labels_json:
                ldir = Path(args.labels_json); ldir.mkdir(parents=True, exist_ok=True)
                lex = rep.get("labels_export")
                if lex is None:
                    msg = (f"--labels-json: --source {args.source} не отдаёт якоря подписей "
                           f"(реализовано для --source figma) - labels-{fn}.json не записан")
                    rep["warnings"].append(msg); print("! " + msg)
                else:
                    (ldir / f"labels-{fn}.json").write_text(
                        json.dumps(lex, ensure_ascii=False, indent=1), encoding="utf-8")
                    print(f"  якоря подписей -> {ldir / f'labels-{fn}.json'} "
                          f"(квартир {len(lex['units'])}, балконов {len(lex['balconies'])}, "
                          f"офисов {len(lex['offices'])})")
            if figma_collect is not None:
                write_figma_svg(figma_collect, W, H, args.figma_svg, style["ink"], style.get("muted", "#9AA3AD"), rep)
            if args.labels_only:
                reports.append(rep)
                print_report(rep, W, H)
                continue
            try:
                png = await rasterize(page, svg, W, H)
                rp.save_png(png, out_dir / f"floor-{fn}.png", colors=style["floor_render"]["palette_colors"])
                # WebP: PNG stays on disk as fallback, frontend loads webp. Full-res for the
                # plan itself + a half-width companion for rail/thumbnail previews.
                rp.save_webp(png, out_dir / f"floor-{fn}.webp", colors=style["floor_render"]["palette_colors"])
                rp.save_webp(png, out_dir / f"floor-{fn}-half.webp", colors=style["floor_render"]["palette_colors"], half=True)
                # (see save_webp docstring: full plan lossless ~74KB, half thumb lossy q75 ~49KB - both < 90/60KB targets)
                if args.figma_dir:
                    fd_dir = Path(args.figma_dir); fd_dir.mkdir(parents=True, exist_ok=True)
                    svg_t = re.sub(r'<rect x="0" y="0" width="[\d.]+" height="[\d.]+" fill="#FFFFFF"/>', '', svg, count=1, flags=re.I)
                    (fd_dir / f"floor-{fn}.png").write_bytes(await rasterize(page, svg_t, W, H, transparent=True))
            except Exception as e:
                rep["warnings"].append(f"png failed: {e}")
                print(f"этаж {fn}: PNG не собрался - {e}")
            floors_out[str(fn)] = {"png": f"floor-{fn}.webp?v={ASSET_VER}", "thumb": f"floor-{fn}-half.webp?v={ASSET_VER}", "w": W, "h": H, "units": units_out}
            reports.append(rep)
            print_report(rep, W, H)
        if browser is not None:
            await browser.close()

    if not args.out and not args.labels_only:
        # NOTE (2026-09-21, the reviewer): this block used to run only under --all and did a wholesale
        # `plans["floors"] = floors_out` / fresh `floors_index = {}` - correct for a full run
        # (floors_out then covers every floor), but silently DESTRUCTIVE for a single-floor
        # run (`--floor 2` alone): floors_out would hold only {"2": ...} and every other
        # floor's entry in plans.json/data/plans.js would be wiped. Now it runs for any
        # non---out run (single floor included) and MERGES floors_out into whatever is
        # already on disk, so a `--floor 2` run only ever touches floor 2's own entries.
        plans = json.loads(PLANS_JSON_PATH.read_text(encoding="utf-8")) if PLANS_JSON_PATH.exists() else {}
        plans.setdefault("floors", {}).update(floors_out)
        # unit cards: every apartment shows the finished plan of its base type (PNG export of
        # the Figma final, 2x), instead of the old per-unit crops of the floor render
        units_sec = {}
        webp_cache = {}  # base -> bool converted ok; 33 unique bases behind 279 unit numbers, convert each once
        for number, info in units_db.items():
            a = assign.get(number)
            if not a:
                continue
            klass = a["class"]; base = (cls.get(klass) or {}).get("base", klass)
            # ревьюер 22.09: картинка = экспортный клон СТРОГОГО КЛАССА (33 базы + 62 производных, «Заголовок» = тип + площадь класса
            # впечён в Figma), а не базы; если экспорта класса ещё нет — падаем на базу. Размер — из PNG (у производных фреймы шире).
            strict = STRICT_CLASS.get(str(number), klass)      # строгий класс (95) — свой экспорт с точной площадью
            img = strict if (OUT_DIR / f"unit-{strict}.png").exists() else (klass if (OUT_DIR / f"unit-{klass}.png").exists() else base)
            klass = strict
            png = OUT_DIR / f"unit-{img}.png"
            if not png.exists():
                continue
            if img not in webp_cache:
                webp_path = OUT_DIR / f"unit-{img}.webp"
                try:
                    if not webp_path.exists() or webp_path.stat().st_mtime < png.stat().st_mtime:
                        rp.save_webp_from_png(png, webp_path)  # lossless: ~50KB vs ~180-210KB PNG on these flat-fill plans
                    webp_cache[img] = True
                except Exception as e:
                    webp_cache[img] = False
                    print(f"unit-{img}: webp convert failed - {e}")
            try:
                from PIL import Image as _Img
                with _Img.open(png) as _im: pw, ph = _im.size
            except Exception:
                mu = manifest.get(img) or {}; pw, ph = round(mu.get("w", 0) * 2), round(mu.get("h", 0) * 2)
            units_sec[number] = {"png": (f"unit-{img}.webp?v={ASSET_VER}" if webp_cache[img] else f"unit-{img}.png?v={ASSET_VER}"), "w": pw, "h": ph, "base": base, "cls": klass}
        plans["units"] = units_sec
        plans.pop("missing", None); plans.pop("ambiguous", None)
        plans["notes"] = "floors: tools/plan-floor-assemble.py (Figma finals + v15 underlay); units: WebP of the base type final (Figma export 2x, PNG kept as fallback)"
        PLANS_JSON_PATH.write_text(json.dumps(plans, ensure_ascii=False, indent=1), encoding="utf-8")

        # Site-facing data/plans.js: split floors out into one JSON file per floor (perf -
        # the rail/floors view only ever needs the current + neighbour floors, not all 25 at
        # once), keep units + a floorsIndex map of floor -> per-floor file name in plans.js.
        # floorsIndex is recovered from the existing file first (not started empty) so a
        # partial run's write only adds/refreshes its own floor(s), same reasoning as above.
        FLOORS_DIR.mkdir(parents=True, exist_ok=True)
        floors_index = {}
        if PLANS_JS_PATH.exists():
            try:
                m = re.search(r'window\.PLANS_DATA\s*=\s*(\{.*\});?\s*$', PLANS_JS_PATH.read_text(encoding="utf-8"), re.S)
                if m:
                    floors_index = json.loads(m.group(1)).get("floorsIndex") or {}
            except Exception as e:
                print(f"! could not recover existing floorsIndex from {PLANS_JS_PATH}: {e} "
                      f"- starting empty (a partial run would then drop every OTHER floor's index entry - aborting write)")
                if not args.all:
                    floors_index = None
        if floors_index is None:
            print(f"! plans.js write skipped for safety (see warning above) - {PLANS_JSON_PATH} floors merge above still applied")
        else:
            for fn_str, fdata in floors_out.items():
                (FLOORS_DIR / f"floor-{fn_str}.json").write_text(json.dumps(fdata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                floors_index[fn_str] = f"floor-{fn_str}.json?v={ASSET_VER}"
            site_data = {"units": units_sec, "floorsIndex": floors_index, "notes": plans["notes"]}
            PLANS_JS_PATH.write_text("window.PLANS_DATA = " + json.dumps(site_data, ensure_ascii=False, separators=(",", ":")) + ";",
                                     encoding="utf-8")
            print(f"\nперезаписаны {PLANS_JSON_PATH}, {PLANS_JS_PATH} и {len(floors_out)} файлов в {FLOORS_DIR} "
                  f"(этот прогон: floors={sorted(floors_out.keys())}, всего в индексе={len(floors_index)}, units={len(units_sec)})")

        if SITE_VERSION_SCRIPT.exists():
            subprocess.run([sys.executable, str(SITE_VERSION_SCRIPT), "--version", ASSET_VER], check=False)

    print("\n===== ИТОГ =====")
    for r in reports:
        print(f"этаж {r['floor']}: квартир {r['units']}, пропущено {len(r['skipped'])}, "
              f"WARN выравнивания {len(r['warn_align'])}, производных {len(r['derived'])}"
              + (f", ошибки: {len(r['warnings'])}" if r["warnings"] else ""))


FIGMA_GROUPS = ["Стены", "Окна", "Двери", "Балконы", "Ядро"]
FIGMA_GROUP_KEY = {"Стены": "walls", "Окна": "windows", "Двери": "doors", "Балконы": "balconies", "Ядро": "core"}
FIGMA_ELEMENT_STYLE = {
    "стена": {"fill": "ink"},
    "колонна": {"fill": "ink"},
    "окно": {"fill": "white", "stroke": "ink", "stroke-width": "0.8"},
    "клин окна": {"fill": "ink"},
    "стойка": {"fill": "ink"},
    "порог": {"stroke": "ink", "stroke-width": "0.8", "fill": "none"},
    "коробка": {"fill": "white", "stroke": "ink", "stroke-width": "0.8"},
    "рамка проёма": {"fill": "white", "stroke": "ink", "stroke-width": "1.2"},
    "полотно": {"stroke": "ink", "stroke-width": "1.5", "fill": "none", "stroke-linecap": "round"},
    "дуга двери": {"stroke": "ink", "stroke-width": "1.2", "fill": "none", "stroke-dasharray": "6 4", "stroke-linecap": "round"},
    "окно балкона": {"fill": "ink"},
    "ступень": {"fill": "none", "stroke": "muted", "stroke-width": "1.2"},
    "перила": {"stroke": "ink", "stroke-width": "1.5", "fill": "none", "stroke-linecap": "round"},
    "лифт": {"stroke": "ink", "stroke-width": "1.2", "fill": "none"},
}


def write_figma_svg(figma_collect, W, H, out_path, ink, muted, report):
    """--figma-svg (the reviewer, 2026-09-21: этажи идут в Figma, чистит руками - «никаких заливок,
    тонов, ячеек, подписей и площадей», только геометрия). Каждый элемент build_floor_trace
    уже collect-ировал (id слоя, `d` в floor-local pt) становится своим <path id="...">, id -
    точное имя слоя (кириллица как есть, UTF-8, никаких numeric entities - Figma при импорте
    SVG берёт id узла как имя слоя). Координаты - _scale_path_d(d, K) в px, округление 0.1,
    БЕЗ transform (ревьюер: «координаты абсолютные»). Фон прозрачный - без <rect> бумаги, без
    заливок пола/ячеек/подписей (эти слои build_floor_trace вообще не отдаёт в figma_collect)."""
    colors = {"ink": ink, "white": "#FFFFFF", "muted": muted, "none": "none"}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">']
    counts = {}
    for group_name in FIGMA_GROUPS:
        key = FIGMA_GROUP_KEY[group_name]
        items = (figma_collect or {}).get(key, [])
        parts.append(f'<g id="{group_name}">')
        for elem_id, d_pt in items:
            d_px = _scale_path_d(d_pt, K, ndigits=1)
            style = FIGMA_ELEMENT_STYLE.get(elem_id, {"fill": "ink"})
            attrs = []
            for prop in ("fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap"):
                if prop in style:
                    v = style[prop]
                    attrs.append(f'{prop}="{colors.get(v, v)}"')
            parts.append(f'<path id="{elem_id}" d="{d_px}" {" ".join(attrs)}/>')
            counts[elem_id] = counts.get(elem_id, 0) + 1
        parts.append("</g>")
    parts.append("</svg>")
    svg = "".join(parts)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(svg, encoding="utf-8")
    report["figma_svg_path"] = str(out_path)
    report["figma_svg_counts"] = counts
    report["figma_svg_group_counts"] = {g: len((figma_collect or {}).get(FIGMA_GROUP_KEY[g], [])) for g in FIGMA_GROUPS}
    report["figma_svg_bytes"] = len(svg.encode("utf-8"))
    return svg


def print_report(rep, W, H):
    src = rep.get("source", "finals")
    print(f"\nэтаж {rep['floor']}: холст {W}x{H} px, квартир {rep['units']} [--source {src}]"
          + (" [--legacy-fill]" if rep.get("legacy_fill") else ""))
    if rep["skipped"]:
        print("  пропущены: " + "; ".join(rep["skipped"]))
    if "trace_doors_total" in rep:
        print(f"  двери (--source trace): всего {rep['trace_doors_total']}, с коробками {rep['trace_doors_boxed']} "
              f"(из них по балконной стенке {len(rep.get('trace_doors_via_balcony_band') or [])}), "
              f"без найденной толщины стены {rep['trace_doors_no_wall_count']}, рамка проёма (внутренние) {rep['trace_doors_interior_frame']}")
    if "trace_glazing_rows" in rep:
        print(f"  ряды стоек остекления вне балконов (--source trace, правило 4): {rep['trace_glazing_rows']}")
    if "trace_windows_klin" in rep:
        print(f"  окна (--source trace): с клином {rep['trace_windows_klin']}, без клина {rep['trace_windows_no_klin']}, "
              f"клин-кластеров c12 всего {rep.get('trace_klin_groups_total', 0)} (непарных {len(rep.get('trace_klin_unmatched') or [])}), "
              f"пропущено как иконка {rep.get('trace_windows_icon_skipped', 0)}")
    if "trace_facade_windows" in rep:
        print(f"  окна фасада (правило 1, проёмы наружной стены): {rep['trace_facade_windows']} "
              f"(с клином {rep['trace_facade_windows_klin']}, без клина {rep['trace_facade_windows_plain']}, "
              f"со стойками {rep['trace_facade_windows_with_stoiki']})")
    if "trace_balconies_found_n" in rep:
        print(f"  подстановка балконов: найдено у {rep['trace_balconies_found_n']}/11 квартир, "
              f"fallback на floor-F.json у {rep['trace_balconies_fallback_n']}")
        if rep.get("subst_balcony_sum_warn"):
            for w in rep["subst_balcony_sum_warn"]:
                print(f"    WARN сумма балконов {w['unit']}: найдено {w['found_sum_m2']}m2 "
                      f"({w['n_candidates']} шт), в units_db {w['target_m2']}m2")
        if rep.get("subst_seed_skipped"):
            print(f"    семян пропущено: {len(rep['subst_seed_skipped'])}")
    if rep.get("figma_in"):
        rc = rep.get("figma_in_recolor") or {}
        print(f"  чертёж (--source figma): {rep['figma_in']} ({rep.get('figma_in_bytes', 0)/1024:.1f} KB) - "
              f"путей {rep.get('figma_in_paths', 0)}, групп-слоёв {rep.get('figma_in_groups', 0)}, "
              f"перекрашено black->ink: fill={rc.get('fill', 0)}, stroke={rc.get('stroke', 0)}")
        print(f"  балконы из floor-{rep['floor']}.json: {rep.get('figma_balconies_json_n', 0)} "
              f"(подстановки по PDF в этом режиме нет)")
    if rep.get("figma_svg_path"):
        gc = rep.get("figma_svg_group_counts", {})
        print(f"  --figma-svg: {rep['figma_svg_path']} ({rep['figma_svg_bytes']/1024:.1f} KB) - "
              + ", ".join(f"{g}={gc.get(g, 0)}" for g in FIGMA_GROUPS))
        print("    по слоям: " + ", ".join(f"{k}={v}" for k, v in sorted(rep.get("figma_svg_counts", {}).items())))
    if rep["derived"]:
        print(f"  производные классы ({len(rep['derived'])}): " + "; ".join(rep["derived"]))
    if rep.get("pdf_balcony"):
        print(f"  балкон по PDF ({len(rep['pdf_balcony'])}): " + "; ".join(rep["pdf_balcony"]))
    autofit = rep.get("autofit") or []
    if autofit:
        print(f"  автопосадка: {len(autofit)} квартир, сдвиги: "
              + "; ".join(f"{n}:(dx={dx:+.1f},dy={dy:+.1f})" for n, dx, dy in autofit))
    xp = rep.get("xpilons") or []
    if xp:
        susp = {tuple(round(v, 1) for v in b) for b in (rep.get("xpilons_suspicious") or [])}
        bbtxt = "; ".join(
            f"[{x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f}]" + (" ПОДОЗРИТЕЛЬНЫЙ" if (round(x0,1),round(y0,1),round(x1,1),round(y1,1)) in susp else "")
            for x0, y0, x1, y1 in xp)
        print(f"  X-пилоны: {len(xp)} - " + bbtxt)
    else:
        print("  X-пилоны: 0")
    for w in rep["warn_align"]:
        print("  " + w)
    if not rep["warn_align"]:
        print("  выравнивание: все квартиры в допуске")
    if rep["align"]:
        worst = max(rep["align"], key=lambda a: max(abs(v) for v in a[3]))
        print(f"  невязки L/T/R/B (px, после вычета полосы стены {WALL_BAND}px): "
              + ", ".join(f"{n}:{d}" for n, b, dv, d in rep["align"]))
        print(f"  худшая: {worst[0]} -> {worst[3]}")
    checks = rep.get("cells_checks") or {}
    if checks:
        print("  ячейки владения (FLOOR-CONTRACT § «Ячейки владения», pt²):")
        n1 = n2 = n3 = 0
        for n in sorted(checks, key=int):
            c = checks[n]
            neigh = c.get("neighbors") or {}
            bad_neigh = {m: v for m, v in neigh.items() if not v["ok"]}
            if not c["prop1_ok"]:
                n1 += 1
            if bad_neigh:
                n2 += 1
            if not c["prop3_ok"]:
                n3 += 1
            neigh_txt = ", ".join(f"{m}:overlap={v['overlap_pt2']:.4f}/gap={v['gap_pt']:.4f}"
                                   for m, v in sorted(neigh.items(), key=lambda kv: int(kv[0])))
            pockets = c.get("enclosed_pockets", 0)
            print(f"    {n}: poly={c['poly_area']:.1f} balcony={c['balcony_area']:.1f} "
                  f"cell={c['cell_area']:.1f} prop1={'ok' if c['prop1_ok'] else 'WARN'} "
                  f"prop3(corridor∩)={c['corridor_overlap_pt2']:.4f} {'ok' if c['prop3_ok'] else 'WARN'}"
                  + (f" (закрытых карманов заполнено: {pockets})" if pockets else "")
                  + (f" | соседи: {neigh_txt}" if neigh_txt else ""))
        if n1 or n2 or n3:
            print(f"    WARN нарушения свойств: prop1={n1}, prop2(соседи)={n2}, prop3(коридор)={n3} из {len(checks)} квартир")
        else:
            print("    все 3 свойства выполняются на всех квартирах")
    if rep.get("balcony_labels") or rep.get("balcony_labels_skipped"):
        print(f"  подписи балконов: поставлено {len(rep.get('balcony_labels') or [])}, "
              f"пропущено {len(rep.get('balcony_labels_skipped') or [])}"
              + (f" ({'; '.join(rep['balcony_labels_skipped'])})" if rep.get("balcony_labels_skipped") else ""))
    if rep.get("balcony_wall_bands"):
        print("  балконные стенки (полоса / балкон, pt²):")
        for b in sorted(rep["balcony_wall_bands"], key=lambda b: int(b["unit"])):
            ratio = b["ratio"]
            flag = " WARN >=40%" if ratio is not None and ratio >= 0.4 else ""
            print(f"    {b['unit']}: полоса={b['band_area_pt2']:.2f} балкон={b['balcony_area_pt2']:.2f} "
                  f"ratio={ratio if ratio is not None else 'n/a'}{flag}")
    if rep.get("caption_anchors"):
        print("  якоря подписей квартир (px, по наибольшей комнате):")
        for a in sorted(rep["caption_anchors"], key=lambda a: int(a["unit"])):
            src = "room_mask" if a["from_room_mask"] else "FALLBACK poly"
            print(f"    {a['unit']}: {a['room_px']} ({src})")
    if rep.get("caption_anchors_balcony"):
        print("  якоря подписей балконов (px, по маске балкона):")
        for a in sorted(rep["caption_anchors_balcony"], key=lambda a: int(a["unit"])):
            src = a.get("source") or ("balcony_mask" if a.get("from_balcony_mask") else "FALLBACK poly")
            idx = f"#{a['idx']}" if "idx" in a else ""
            print(f"    {a['unit']}{idx}: {a['balcony_px']} ({src})")
    for w in rep["warnings"]:
        print("  ! " + w)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=int, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--source", choices=["pdf", "finals", "glue", "trace", "figma"], default="glue",
                     help="floor assembly source: glue (default since 2026-09-21, the reviewer's decision after the floor-2/floor-3 method comparison - apartments are Figma finals only, PDF draws only core/corridor outside every unit+balcony plus unclipped X-pylons; --underlay/--legacy-fill do not apply), pdf (walls/doors/windows/apartments ALL from PDF vectors, no final-svg at all), or finals (the previous pipeline - apartment interiors from Figma final-svg, --underlay/--legacy-fill apply), or figma (ревьюер, 2026-09-21: чертёж этажа берётся ГОТОВЫМ из --figma-in, сборщик добавляет только ячейки владения, подписи и outline)")
    ap.add_argument("--underlay", choices=["v15", "pdf"], default="pdf", help="--source finals only: 'not an apartment' underlay source: v15 (old renderer) or pdf (straight from PDF vectors, default)")
    ap.add_argument("--svg-dir", type=str, default=None)
    ap.add_argument("--out", type=str, default=None, help="override output dir (never touches plans.json)")
    ap.add_argument("--labels-json", type=str, default=None,
                    help="каталог для DIR/labels-F.json: якоря подписей квартир/балконов/офисов "
                         "(x,y в px холста = координаты внутри фрейма этажа в Figma). "
                         "Реализовано для --source figma")
    ap.add_argument("--labels-only", action="store_true",
                    help="с --labels-json: собрать SVG в памяти и отчёт, но НЕ растеризовать "
                         "PNG/WebP и НЕ трогать plans.json / plans.js / data/floors")
    ap.add_argument("--figma-dir", type=str, default=None, help="also write transparent PNGs (no paper) for the Figma «Этажи» page")
    ap.add_argument("--figma-svg", type=str, default=None,
                     help="--source trace only: also write a per-element SVG for Figma import (the reviewer 2026-09-21) - "
                          "one <path id=\"layer name\"> per source primitive (walls/windows/doors/balconies/core), "
                          "no unions, no cell fills/captions/paper, no transform (absolute px)")
    ap.add_argument("--figma-in", type=str, default=None,
                     help=f"--source figma only: путь к ручной чистке этажа, экспортированной из Figma "
                          f"({{F}} = номер этажа; по умолчанию {FIGMA_IN_TMPL} относительно корня репозитория)")
    ap.add_argument("--legacy-fill", action="store_true", help="--source finals only: reproduces the pre-v4 pipeline (per-unit poly+balcony fill, underlay draws c08/c19 itself, no ownership cells) for side-by-side comparison; ignored (warned) under --source pdf")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
