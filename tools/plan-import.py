#!/usr/bin/env python3
"""v3 importer: architect's PDF -> plan document (plan-studio/v3/plans/unit-N.json).

One-shot. Reuses tools/trace-plans.py for classification (layers/colours), the per-edge
boundary decision (thickness class, door runs) and furniture grouping, via
process_unit(..., collect={}) which hands over the pre-render geometry without writing files.
After import the document is the source of truth; edits happen in plan-studio/v3/editor/.

Usage:  .venv/bin/python tools/plan-import.py [unit ...]      (default: trace-plans TARGET_UNITS)
        --force   overwrite an existing document (default: skip units that already have one)
Schema: editor/SCHEMA.md
"""
import importlib.util, json, math, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
OUT_DIR = ROOT / "plan-studio/v3/plans"
OUT_DIR.mkdir(parents=True, exist_ok=True)

_spec = importlib.util.spec_from_file_location("trace_plans", fpconfig.CODE / "tools/trace-plans.py")
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)

KIND_BY_CLASS = {"threshold": "exterior", "facade": "exterior", "shared": "party", "corridor": "corridor",
                 "balcony_wall": "balcony", "railing": "railing"}
SLIVER_GAP = 1.6          # client rule: white slivers <= 1.6 pt inside a wall run are wall
NODE_TOL = 0.06
LINE_TOL = 3.0            # door hinge / end must be this close to a wall line (as door_opening_on_edge)
M_PER_PT = 0.0705


def r2(v):
    return round(v + 0.0, 2)


def perp(u):
    return (-u[1], u[0])


def unit_vec(a, b):
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    return ((b[0] - a[0]) / L, (b[1] - a[1]) / L) if L > 1e-9 else (0.0, 0.0), L


class Doc:
    def __init__(self, unit):
        self.unit = unit
        self.nodes = {}          # id -> (x, y)
        self._node_list = []     # for tolerance lookup
        self.walls, self.openings, self.areas, self.columns, self.blocks = [], [], [], [], []
        self._ids = {}

    def nid(self, prefix):
        self._ids[prefix] = self._ids.get(prefix, 0) + 1
        return f"{prefix}{self._ids[prefix]}"

    def node(self, p):
        x, y = r2(p[0]), r2(p[1])
        for nid, (nx, ny) in self._node_list:
            if abs(nx - x) <= NODE_TOL and abs(ny - y) <= NODE_TOL:
                return nid
        nid = self.nid("n")
        self.nodes[nid] = (x, y)
        self._node_list.append((nid, (x, y)))
        return nid

    def wall(self, a, b, t, side, kind):
        na, nb = self.node(a), self.node(b)
        if na == nb:
            return None
        w = {"id": self.nid("w"), "a": na, "b": nb, "t": r2(t), "side": side, "kind": kind}
        self.walls.append(w)
        return w

    def wall_geom(self, w):
        a, b = self.nodes[w["a"]], self.nodes[w["b"]]
        u, L = unit_vec(a, b)
        n = perp(u); n = (n[0] * w["side"], n[1] * w["side"])
        return a, b, u, n, L

    def opening(self, w, pos, width, kind, **extra):
        _, _, _, _, L = self.wall_geom(w)
        pos = max(0.0, pos); width = min(width, L - pos)
        if width <= 0.5:
            return None
        o = {"id": self.nid("o"), "wall": w["id"], "pos": r2(pos), "width": r2(width), "kind": kind}
        o.update(extra)
        self.openings.append(o)
        return o

    def to_json(self, meta, bbox):
        return {
            "v": 3, "unit": self.unit, "floor": meta.get("floor"), "type": meta.get("type", ""),
            "title": tp.TYPE_LABELS.get(meta.get("type"), meta.get("type", "")),
            "total": meta.get("total"), "living": meta.get("living"), "balcony": meta.get("balcony"),
            "bbox": bbox,
            "underlay": dict(bbox, src=f"unit-{self.unit}-pdf.png"),   # PDF-вырезка plan-studio/truth/, тот же кадр
            "nodes": {k: [v[0], v[1]] for k, v in self.nodes.items()},
            "walls": self.walls, "openings": self.openings, "areas": self.areas,
            "columns": self.columns, "blocks": self.blocks, "notes": [],
        }


# ---------------------------------------------------------------- path helpers

_NUM = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def translate_path(d, dx, dy):
    """Shift absolute coordinates of a build_path() string (M/L/C absolute, h/v relative, Z)."""
    out = []
    toks = d.replace(",", " ").split()
    i = 0
    cmd = None
    while i < len(toks):
        t = toks[i]
        if t in ("M", "L", "C", "h", "v", "Z", "z", "H", "V"):
            cmd = t; out.append(t); i += 1
            continue
        if cmd in ("M", "L", "C"):
            x = float(toks[i]) + dx; y = float(toks[i + 1]) + dy
            out.append(f"{r2(x):g} {r2(y):g}"); i += 2
        elif cmd in ("h", "v"):
            out.append(f"{r2(float(toks[i])):g}"); i += 1
        elif cmd == "H":
            out.append(f"{r2(float(toks[i]) + dx):g}"); i += 1
        elif cmd == "V":
            out.append(f"{r2(float(toks[i]) + dy):g}"); i += 1
        else:
            i += 1
    return " ".join(out)


def path_bbox(d):
    """bbox of a build_path() string (handles relative h/v)."""
    toks = d.replace(",", " ").split()
    xs, ys = [], []
    cx = cy = 0.0
    i = 0; cmd = None
    while i < len(toks):
        t = toks[i]
        if t in ("M", "L", "C", "h", "v", "Z", "z"):
            cmd = t; i += 1; continue
        if cmd in ("M", "L", "C"):
            cx, cy = float(toks[i]), float(toks[i + 1]); xs.append(cx); ys.append(cy); i += 2
        elif cmd == "h":
            cx += float(toks[i]); xs.append(cx); i += 1
        elif cmd == "v":
            cy += float(toks[i]); ys.append(cy); i += 1
        else:
            i += 1
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def poly_area(poly):
    s = 0.0
    for i in range(len(poly)):
        x0, y0 = poly[i]; x1, y1 = poly[(i + 1) % len(poly)]
        s += x0 * y1 - x1 * y0
    return abs(s) / 2


# ---------------------------------------------------------------- import

def is_rect(bb, pts, tol=0.35):
    if not pts:
        return True
    x0, y0, x1, y1 = bb
    ok = sum(1 for (x, y) in pts
             if (abs(x - x0) <= tol or abs(x - x1) <= tol) and (abs(y - y0) <= tol or abs(y - y1) <= tol))
    return ok >= 0.9 * len(pts)


def import_unit(unit, pdf):
    c = {}
    tp.process_unit(unit, pdf, collect=c)
    ox, oy = c["crop"]["x0"], c["crop"]["y0"]          # crop-local -> plan
    P = lambda p: (p[0] + ox, p[1] + oy)
    doc = Doc(unit)
    report = {"unit": unit, "orphan_doors": 0, "orphan_windows": 0, "raw_wall_blocks": 0, "dangling": 0}

    door_geoms = [{"centre": P(d["centre"]), "ends": (P(d["ends"][0]), P(d["ends"][1])),
                   "mid": P(d["samples"][len(d["samples"]) // 2]), "radius": d["radius"], "used": False}
                  for d in c["door_geoms"] if d]

    def door_for_run(wall, t0, t1):
        """Door geometry whose hinge sits on this wall's line inside the run (t fractions)."""
        a, b, u, n, L = doc.wall_geom(wall)
        best = None
        for dg in door_geoms:
            if dg["used"]:
                continue
            cx, cy = dg["centre"]
            perp_d = abs((cx - a[0]) * n[0] + (cy - a[1]) * n[1])
            along = ((cx - a[0]) * u[0] + (cy - a[1]) * u[1]) / L
            if perp_d <= LINE_TOL and t0 - 0.05 <= along <= t1 + 0.05:
                score = perp_d + min(abs(along - t0), abs(along - t1)) * L
                if best is None or score < best[0]:
                    best = (score, dg, along)
        return best

    def door_attrs(wall, dg, along, t0, t1):
        a, b, u, n, L = doc.wall_geom(wall)
        hinge = "a" if abs(along - t0) <= abs(along - t1) else "b"
        mx, my = dg["mid"]
        s = (mx - dg["centre"][0]) * n[0] + (my - dg["centre"][1]) * n[1]
        return {"hinge": hinge, "swing": 1 if s > 0 else -1}

    # --- 1. ring walls (unit + balconies) from the per-edge decision of boundary_bands
    def add_ring(edges, ring_local):
        closed = [tuple(p) for p in ring_local] + [tuple(ring_local[0])]
        for e in edges:
            cls = e["cls"]
            if cls in ("jog", "shared_with_unit") or e["T"] <= 0:
                continue
            p0, p1 = P(e["p0"]), P(e["p1"])
            n_out = e.get("n") or tp.outward_normal(e["p0"], e["p1"], closed)
            u, L = unit_vec(p0, p1)
            pu = perp(u)
            side = 1 if (n_out[0] * pu[0] + n_out[1] * pu[1]) > 0 else -1
            w = doc.wall(p0, p1, e["T"], side, KIND_BY_CLASS.get(cls, "partition"))
            if w is None:
                continue
            # doors
            for (t0, t1) in e["doors"]:
                hit = door_for_run(w, t0, t1)
                extra = {"hinge": "a", "swing": -1}
                if hit:
                    _, dg, along = hit
                    dg["used"] = True
                    extra = door_attrs(w, dg, along, t0, t1)
                doc.opening(w, t0 * L, (t1 - t0) * L, "door", **extra)
            # windows: inside the solid runs only
            solid, cur = [], 0.0
            for (d0, d1) in e["doors"]:
                if d0 > cur + 1e-6:
                    solid.append((cur, d0))
                cur = max(cur, d1)
            if cur < 1 - 1e-6:
                solid.append((cur, 1.0))
            for (t0, t1) in e["wins"]:
                for (s0, s1) in solid:
                    a_, b_ = max(t0, s0), min(t1, s1)
                    if (b_ - a_) * L >= 2.0:
                        doc.opening(w, a_ * L, (b_ - a_) * L, "window")

    add_ring(c["edges"]["unit"], c["outer_ring"])
    for br, be in zip(c["balcony_rings"], c["edges"]["balconies"]):
        add_ring(be, br)

    # --- 2. interior partitions from PDF wall fills (rectangles -> walls, the rest -> raw ink blocks)
    pieces = []   # (axis 'h'|'v', face coordinate, t, start, end)
    tiny = []     # slivers (<= 2 pt both ways): absorbed by the wall they sit in, else kept as raw ink
    for d, bb, pts in c["interior_fills"]:
        x0, y0, x1, y1 = bb
        w_, h_ = x1 - x0, y1 - y0
        pier = min(w_, h_) > 8.0 and max(w_, h_) / max(min(w_, h_), 0.01) < 2.0
        if max(w_, h_) <= 2.0:
            tiny.append((d, bb))
            continue
        if not is_rect(bb, pts) or pier or min(w_, h_) < 0.25:
            bbp = path_bbox(d)
            if bbp:
                bx0, by0, bx1, by1 = bbp
                doc.blocks.append({"id": doc.nid("b"), "sym": "raw", "role": "wall",
                                   "x": r2(bx0 + ox), "y": r2(by0 + oy), "w": r2(bx1 - bx0), "h": r2(by1 - by0),
                                   "rot": 0, "mirror": False,
                                   "paths": [{"d": translate_path(d, -bx0, -by0), "fill": "ink", "stroke": None, "w": 0}]})
                report["raw_wall_blocks"] += 1
            continue
        if w_ >= h_:
            pieces.append(("h", y0 + oy, h_, x0 + ox, x1 + ox))
        else:
            pieces.append(("v", x0 + ox, w_, y0 + oy, y1 + oy))

    # merge collinear pieces: same axis, same face (±0.3) and thickness (±0.3), gap <= SLIVER_GAP
    pieces.sort(key=lambda p: (p[0], round(p[1], 1), p[3]))
    merged = []
    for p in pieces:
        if merged:
            q = merged[-1]
            if q[0] == p[0] and abs(q[1] - p[1]) <= 0.3 and abs(q[2] - p[2]) <= 0.3 and p[3] - q[4] <= SLIVER_GAP:
                merged[-1] = (q[0], q[1], max(q[2], p[2]), q[3], max(q[4], p[4]))
                continue
        merged.append(p)
    # merge across a door gap: consecutive collinear pieces whose gap is spanned by an unused door arc
    merged2 = []
    for p in merged:
        if merged2:
            q = merged2[-1]
            gap = p[3] - q[4]
            if q[0] == p[0] and abs(q[1] - p[1]) <= 0.3 and abs(q[2] - p[2]) <= 0.3 and 3.0 <= gap <= 40.0:
                # candidate door: hinge on the face line near one gap end, radius ~ gap
                face = q[1]
                found = None
                for dg in door_geoms:
                    if dg["used"]:
                        continue
                    cx, cy = dg["centre"]
                    perp_d = abs((cy if p[0] == "h" else cx) - face)
                    along = cx if p[0] == "h" else cy
                    near_end = min(abs(along - q[4]), abs(along - p[3]))
                    if perp_d <= LINE_TOL + q[2] and near_end <= 2.5 and abs(dg["radius"] - gap) <= 3.0:
                        found = dg; break
                if found:
                    merged2[-1] = (q[0], q[1], max(q[2], p[2]), q[3], p[4], (q[4], p[3], found))
                    continue
        merged2.append(p)

    for p in merged2:
        axis, face, t, s0, s1 = p[:5]
        if axis == "h":
            a, b = (s0, face), (s1, face); side = 1      # perp((1,0)) = (0,1): extrude +y
        else:
            a, b = (face, s0), (face, s1); side = -1     # perp((0,1)) = (-1,0): extrude +x needs side -1
        w = doc.wall(a, b, t, side, "partition")
        if w is None:
            continue
        if len(p) == 6:
            g0, g1, dg = p[5]
            dg["used"] = True
            _, _, u, n, L = doc.wall_geom(w)
            # opening = hinge -> hinge ± radius (towards the gap), clipped to the gap (+0.5 tolerance)
            h_al = (dg["centre"][0] - a[0]) * u[0] + (dg["centre"][1] - a[1]) * u[1]   # pt from a
            if abs(h_al - (g1 - s0)) <= abs(h_al - (g0 - s0)):
                o0, o1 = h_al - dg["radius"], h_al
            else:
                o0, o1 = h_al, h_al + dg["radius"]
            o0 = max(o0, g0 - s0 - 0.5); o1 = min(o1, g1 - s0 + 0.5)
            t0, t1 = o0 / L, o1 / L
            doc.opening(w, o0, o1 - o0, "door", **door_attrs(w, dg, h_al / L, t0, t1))

    # slivers: inside a wall's body -> already covered by it; otherwise keep the ink
    def inside_some_wall(bb):
        cx, cy = (bb[0] + bb[2]) / 2 + ox, (bb[1] + bb[3]) / 2 + oy
        for w in doc.walls:
            a, b, u, n, L = doc.wall_geom(w)
            along = (cx - a[0]) * u[0] + (cy - a[1]) * u[1]
            off = (cx - a[0]) * n[0] + (cy - a[1]) * n[1]
            if -0.1 <= along <= L + 0.1 and -0.1 <= off <= w["t"] + 0.1:
                return True
        return False
    for d, bb in tiny:
        if inside_some_wall(bb):
            continue
        bbp = path_bbox(d)
        if not bbp:
            continue
        bx0, by0, bx1, by1 = bbp
        doc.blocks.append({"id": doc.nid("b"), "sym": "raw", "role": "wall",
                           "x": r2(bx0 + ox), "y": r2(by0 + oy), "w": r2(bx1 - bx0), "h": r2(by1 - by0),
                           "rot": 0, "mirror": False,
                           "paths": [{"d": translate_path(d, -bx0, -by0), "fill": "ink", "stroke": None, "w": 0}]})
        report["raw_wall_blocks"] += 1

    # --- 3. doors still unattached: try any wall (hinge + one end on the line), else report
    for dg in door_geoms:
        if dg["used"]:
            continue
        best = None
        for w in doc.walls:
            a, b, u, n, L = doc.wall_geom(w)
            def perp_d(q): return abs((q[0] - a[0]) * n[0] + (q[1] - a[1]) * n[1])
            def along(q): return ((q[0] - a[0]) * u[0] + (q[1] - a[1]) * u[1]) / L
            if perp_d(dg["centre"]) > LINE_TOL:
                continue
            ends_on = [e for e in dg["ends"] if perp_d(e) <= LINE_TOL]
            if not ends_on:
                continue
            e = min(ends_on, key=perp_d)
            t0, t1 = sorted((along(dg["centre"]), along(e)))
            if t1 < -0.02 or t0 > 1.02 or (t1 - t0) * L < 4.0:
                continue
            score = perp_d(dg["centre"]) + perp_d(e)
            if best is None or score < best[0]:
                best = (score, w, max(0.0, t0), min(1.0, t1), along(dg["centre"]))
        if best:
            _, w, t0, t1, along = best
            _, _, _, _, L = doc.wall_geom(w)
            dg["used"] = True
            doc.opening(w, t0 * L, (t1 - t0) * L, "door", **door_attrs(w, dg, along, t0, t1))
        else:
            report["orphan_doors"] += 1

    # --- 4. columns, shafts, inner rings
    for (x, y, w_, h_) in c["columns"]:
        doc.columns.append({"id": doc.nid("c"), "x": r2(x + ox), "y": r2(y + oy), "w": r2(w_), "h": r2(h_)})
    if c["shafts"]:
        # one block per proximity cluster of shaft prims
        items = [(role, d, bb) for role, d, bb in c["shafts"]]
        n = len(items); parent = list(range(n))
        def f(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]; i = parent[i]
            return i
        for i in range(n):
            for j in range(i + 1, n):
                A, B = items[i][2], items[j][2]
                if max(A[0], B[0]) - min(A[2], B[2]) <= 2.0 and max(A[1], B[1]) - min(A[3], B[3]) <= 2.0:
                    ri, rj = f(i), f(j)
                    if ri != rj: parent[ri] = rj
        clusters = {}
        for i in range(n):
            clusters.setdefault(f(i), []).append(items[i])
        def inside_unit(cx, cy):
            """plan point inside room/balcony polygon or inside a wall body (shaft boxes sit ON the wall band)."""
            for r in [c["poly"]] + list(c["balconies"]):
                if tp.point_in_poly(cx, cy, r):
                    return True
            for w in doc.walls:
                a, b, u, n, L = doc.wall_geom(w)
                along = (cx - a[0]) * u[0] + (cy - a[1]) * u[1]
                off = (cx - a[0]) * n[0] + (cy - a[1]) * n[1]
                if -0.5 <= along <= L + 0.5 and -0.5 <= off <= w["t"] + 0.5:
                    return True
            return False
        for members in clusters.values():
            x0 = min(m[2][0] for m in members); y0 = min(m[2][1] for m in members)
            x1 = max(m[2][2] for m in members); y1 = max(m[2][3] for m in members)
            if not inside_unit((x0 + x1) / 2 + ox, (y0 + y1) / 2 + oy):
                continue   # a neighbour's shaft peeking over the party wall
            paths = []
            for role, d, bb in members:
                if role == "shaft_white":
                    paths.append({"d": translate_path(d, -x0, -y0), "fill": "paper", "stroke": None, "w": 0})
                elif role == "shaft_black":
                    paths.append({"d": translate_path(d, -x0, -y0), "fill": "ink", "stroke": None, "w": 0})
                else:
                    paths.append({"d": translate_path(d, -x0, -y0), "fill": None, "stroke": "ink", "w": 0.5})
            # white first, then black, then strokes (as the old renderer did)
            order = {"paper": 0, "ink": 1, None: 2}
            paths.sort(key=lambda p: order.get(p["fill"], 2))
            doc.blocks.append({"id": doc.nid("b"), "sym": "raw", "role": "shaft",
                               "x": r2(x0 + ox), "y": r2(y0 + oy), "w": r2(x1 - x0), "h": r2(y1 - y0),
                               "rot": 0, "mirror": False, "paths": paths})
    for ring in c["inner_rings"]:
        pts = [P(p) for p in ring]
        x0 = min(p[0] for p in pts); y0 = min(p[1] for p in pts)
        x1 = max(p[0] for p in pts); y1 = max(p[1] for p in pts)
        d = "M " + " L ".join(f"{r2(x - x0):g} {r2(y - y0):g}" for x, y in pts) + " Z"
        doc.blocks.append({"id": doc.nid("b"), "sym": "raw", "role": "wall",
                           "x": r2(x0), "y": r2(y0), "w": r2(x1 - x0), "h": r2(y1 - y0), "rot": 0, "mirror": False,
                           "paths": [{"d": d, "fill": "ink", "stroke": None, "w": 0}]})

    # --- 5. areas + labels (labels are data; anchor = baseline-left as in the PDF)
    labels = [{"text": l["value"], "x": r2(l["x"] + ox), "y": r2(l["y"] + oy)} for l in c["labels"]]
    def take_label(poly):
        for i, l in enumerate(labels):
            if tp.point_in_poly(l["x"], l["y"], poly) or tp.dist_point_poly(l["x"], l["y"], poly) <= 6.0:
                return labels.pop(i)
        return None
    room_poly = [[r2(x), r2(y)] for x, y in (P(p) for p in c["outer_ring"])]
    if room_poly[0] == room_poly[-1]:
        room_poly.pop()
    doc.areas.append({"id": doc.nid("r"), "kind": "room", "name": "", "poly": room_poly, "label": take_label(room_poly)})
    for br in c["balcony_rings"]:
        poly = [[r2(x), r2(y)] for x, y in (P(p) for p in br)]
        if poly[0] == poly[-1]:
            poly.pop()
        doc.areas.append({"id": doc.nid("r"), "kind": "balcony", "name": "", "poly": poly, "label": take_label(poly)})

    # --- 6. furniture: raw blocks in local coords
    for o in c["objects"]:
        paths = []
        for p in o["paths"]:
            paths.append({"d": translate_path(p["d"], -o["x"], -o["y"]),
                          "fill": "paper" if p.get("fill") else None, "stroke": "ink", "w": 0.5})
        doc.blocks.append({"id": doc.nid("b"), "sym": "raw", "role": "furniture",
                           "x": r2(o["x"]), "y": r2(o["y"]), "w": r2(o["w"]), "h": r2(o["h"]),
                           "rot": 0, "mirror": False, "paths": paths})

    # --- 7. report: dangling wall ends (end cross-section touches no other wall body)
    def touches_other(me, key):
        a, b, u, n, L = doc.wall_geom(me)
        end = a if key == "a" else b
        samples = [(end[0] + n[0] * me["t"] * k / 4, end[1] + n[1] * me["t"] * k / 4) for k in range(5)]
        for col in doc.columns:   # a wall ending on a column is not dangling
            for (px, py) in samples:
                if col["x"] - 0.3 <= px <= col["x"] + col["w"] + 0.3 and col["y"] - 0.3 <= py <= col["y"] + col["h"] + 0.3:
                    return True
        for w in doc.walls:
            if w is me:
                continue
            wa, wb, wu, wn, wL = doc.wall_geom(w)
            for (px, py) in samples:
                along = (px - wa[0]) * wu[0] + (py - wa[1]) * wu[1]
                off = (px - wa[0]) * wn[0] + (py - wa[1]) * wn[1]
                if -0.3 <= along <= wL + 0.3 and -0.3 <= off <= w["t"] + 0.3:
                    return True
        return False
    deg = {}
    for w in doc.walls:
        deg[w["a"]] = deg.get(w["a"], 0) + 1; deg[w["b"]] = deg.get(w["b"], 0) + 1
    report["dangling_list"] = []
    for w in doc.walls:
        for key in ("a", "b"):
            if deg[w[key]] == 1 and w["kind"] != "railing" and not touches_other(w, key):
                report["dangling"] += 1
                report["dangling_list"].append(f"{w['id']}.{key}")

    meta = dict(c["meta"]); meta.setdefault("floor", c["floor"])
    bbox = {"x0": r2(c["crop"]["x0"]), "y0": r2(c["crop"]["y0"]), "w": c["crop"]["w"], "h": c["crop"]["h"]}
    out = doc.to_json(meta, bbox)
    problems = validate(out)
    report.update({"walls": len(doc.walls), "openings": len(doc.openings), "blocks": len(doc.blocks),
                   "columns": len(doc.columns), "areas": len(doc.areas), "problems": problems})
    return out, report


def validate(doc):
    """Schema invariants (also used by plan-api on save). Returns a list of problem strings."""
    p = []
    nodes = doc.get("nodes", {})
    wall_by_id = {}
    for w in doc.get("walls", []):
        if w["a"] not in nodes or w["b"] not in nodes:
            p.append(f"wall {w['id']}: missing node"); continue
        if w["a"] == w["b"]:
            p.append(f"wall {w['id']}: a == b")
        a, b = nodes[w["a"]], nodes[w["b"]]
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 0.05:
            p.append(f"wall {w['id']}: zero length")
        if w["id"] in wall_by_id:
            p.append(f"wall {w['id']}: duplicate id")
        wall_by_id[w["id"]] = w
    for o in doc.get("openings", []):
        w = wall_by_id.get(o["wall"])
        if not w:
            p.append(f"opening {o['id']}: wall {o['wall']} missing"); continue
        a, b = nodes[w["a"]], nodes[w["b"]]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if o["pos"] < -0.05 or o["width"] <= 0 or o["pos"] + o["width"] > L + 0.05:
            p.append(f"opening {o['id']}: outside wall {w['id']} (pos {o['pos']} w {o['width']} L {L:.2f})")
    for r in doc.get("areas", []):
        if len(r.get("poly", [])) < 3:
            p.append(f"area {r['id']}: < 3 points")
    for b in doc.get("blocks", []):
        if b.get("sym") == "raw" and not b.get("paths"):
            p.append(f"block {b['id']}: raw without paths")
    return p


def write_index():
    items = []
    for f in sorted(OUT_DIR.glob("unit-*.json"), key=lambda f: (len(f.stem), f.stem)):
        d = json.loads(f.read_text())
        items.append({"unit": d["unit"], "floor": d["floor"], "type": d["type"], "title": d["title"],
                      "total": d["total"], "balcony": d["balcony"], "file": f.name})
    (OUT_DIR / "index.json").write_text(json.dumps({"v": 3, "units": items}, ensure_ascii=False, indent=1))
    return len(items)


if __name__ == "__main__":
    import pymupdf
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    units = args or tp.TARGET_UNITS
    pdf = pymupdf.open(str(tp.PDF))
    print(f"{'unit':6s} {'walls':>5s} {'open':>5s} {'blocks':>6s} {'cols':>4s} {'orphD':>5s} {'rawW':>4s} {'dangl':>5s}  problems")
    for u in units:
        target = OUT_DIR / f"unit-{u}.json"
        if target.exists() and not force:
            print(f"{u:6s} exists, skipped (--force to overwrite)")
            continue
        doc, rep = import_unit(u, pdf)
        target.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
        print(f"{u:6s} {rep['walls']:5d} {rep['openings']:5d} {rep['blocks']:6d} {rep['columns']:4d} "
              f"{rep['orphan_doors']:5d} {rep['raw_wall_blocks']:4d} {rep['dangling']:5d}  "
              f"{'; '.join(rep['problems']) or '-'}  {' '.join(rep['dangling_list'])}")
    tp.close_rasterizer()
    print("index:", write_index(), "units")
