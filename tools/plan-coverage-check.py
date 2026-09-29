#!/usr/bin/env python3
"""Проверка покрытия дома 22 типовыми планировками (только отражения по x/y + перенос).

.venv/bin/python tools/plan-coverage-check.py [--tol 1.0] [--json out.json]

Для каждой квартиры каждого этажа (plan-studio/data/floor-F.json) берётся контур (poly + balcony) и сырые векторы
PDF (PyMuPDF get_drawings, обрезка по кадру квартиры). Для каждого из 22 представителей (plan-studio/v3/plans)
пробуются 4 варианта (как есть, зеркало x, зеркало y, оба), выравнивание по bbox контура.
Метрики: hausdorff контура (pt), несовпавшие структурные элементы PDF по категориям
(стены = заливки #6A6A6A/#FD8379, двери = обводка #FF6600, окна = белые заливки, прочее).
Отчёт: по каждой квартире лучший тип, трансформация, расстояния и списки несовпавших элементов.
"""
import json, sys, math, collections
from pathlib import Path
import pymupdf
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
PLANS = ROOT / "plan-studio/v3/plans"
DATA = ROOT / "plan-studio/data"
GROUPS = ROOT / "plan-studio/unit-shape-groups.json"
PDF = fpconfig.PDF
TOL = 1.0
for i, a in enumerate(sys.argv):
    if a == "--tol":
        TOL = float(sys.argv[i + 1])
OUT_JSON = None
for i, a in enumerate(sys.argv):
    if a == "--json":
        OUT_JSON = Path(sys.argv[i + 1])

WALL = {"#6A6A6A", "#FD8379"}
DOOR = "#FF6600"


def rgb(c):
    if c is None:
        return None
    return "#%02X%02X%02X" % tuple(int(round(x * 255)) for x in c[:3])


def seg_dist_point(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def poly_points(poly, step=2.0):
    pts = []
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        k = max(1, int(L / step))
        for j in range(k):
            t = j / k
            pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return pts


def hausdorff(P, Q):
    def d1(A, B):
        worst = 0
        n = len(B)
        for p in A:
            best = 1e9
            for i in range(n):
                dd = seg_dist_point(p, B[i], B[(i + 1) % n])
                if dd < best:
                    best = dd
            worst = max(worst, best)
        return worst
    return max(d1(poly_points(P), Q), d1(poly_points(Q), P))


def bbox(pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def transform(pts, flipx, flipy, bb):
    x0, y0, x1, y1 = bb
    out = []
    for x, y in pts:
        nx = (x1 - (x - x0)) if flipx else x
        ny = (y1 - (y - y0)) if flipy else y
        out.append((nx - x0, ny - y0))
    return out


def load_floor(F):
    return json.loads((DATA / f"floor-{F}.json").read_text())


def unit_shape(fd, u):
    d = fd["units"][u]
    return [tuple(p) for p in d["poly"]], [tuple(p) for p in (d.get("balcony") or [])]


def unit_drawings(doc, fd, poly, bal, margin=2.0):
    pts = poly + bal
    x0, y0, x1, y1 = bbox(pts)
    fx0, fy0 = fd["bbox"][0], fd["bbox"][1]
    clip = pymupdf.Rect(fx0 + x0 - margin, fy0 + y0 - margin, fx0 + x1 + margin, fy0 + y1 + margin)
    page = doc[fd["page"] - 1]
    key = ("pg", fd["page"])
    if key not in unit_drawings.cache:
        unit_drawings.cache[key] = page.get_drawings()
    items = []
    for dr in unit_drawings.cache[key]:
        r = dr["rect"]
        if r.x1 < clip.x0 or r.x0 > clip.x1 or r.y1 < clip.y0 or r.y0 > clip.y1:
            continue
        fill = rgb(dr.get("fill")); stroke = rgb(dr.get("color"))
        if fill in WALL:
            cat = "wall"
        elif stroke == DOOR:
            cat = "door"
        elif fill == "#FFFFFF":
            cat = "window"
        elif fill is not None:
            cat = "fill"
        else:
            cat = "line"
        segs = []
        for it in dr["items"]:
            k = it[0]
            if k == "l":
                segs.append(((it[1].x - fx0, it[1].y - fy0), (it[2].x - fx0, it[2].y - fy0)))
            elif k == "c":
                segs.append(((it[1].x - fx0, it[1].y - fy0), (it[4].x - fx0, it[4].y - fy0)))
            elif k == "re":
                rr = it[1]
                a = (rr.x0 - fx0, rr.y0 - fy0); b = (rr.x1 - fx0, rr.y0 - fy0); c = (rr.x1 - fx0, rr.y1 - fy0); d = (rr.x0 - fx0, rr.y1 - fy0)
                segs += [(a, b), (b, c), (c, d), (d, a)]
            elif k == "qu":
                q = it[1]
                segs += [((q.ul.x - fx0, q.ul.y - fy0), (q.ur.x - fx0, q.ur.y - fy0)), ((q.ur.x - fx0, q.ur.y - fy0), (q.lr.x - fx0, q.lr.y - fy0)), ((q.lr.x - fx0, q.lr.y - fy0), (q.ll.x - fx0, q.ll.y - fy0)), ((q.ll.x - fx0, q.ll.y - fy0), (q.ul.x - fx0, q.ul.y - fy0))]
        for s in segs:
            # keep only segments whose midpoint is inside the clip
            mx = (s[0][0] + s[1][0]) / 2; my = (s[0][1] + s[1][1]) / 2
            if x0 - margin <= mx <= x1 + margin and y0 - margin <= my <= y1 + margin:
                items.append((cat, fill, stroke, s))
    return items


unit_drawings.cache = {}


def match_segments(A, B, tol):
    """A,B: lists of (cat, fill, stroke, ((x,y),(x,y))) already in a common frame. Returns unmatched of A."""
    idx = collections.defaultdict(list)
    for it in B:
        idx[(it[0], it[1], it[2])].append(it[3])
    un = []
    for it in A:
        cands = idx.get((it[0], it[1], it[2]), [])
        (a, b) = it[3]
        ok = False
        for (c, d) in cands:
            if (math.hypot(a[0] - c[0], a[1] - c[1]) <= tol and math.hypot(b[0] - d[0], b[1] - d[1]) <= tol) or (math.hypot(a[0] - d[0], a[1] - d[1]) <= tol and math.hypot(b[0] - c[0], b[1] - c[1]) <= tol):
                ok = True; break
        if not ok:
            un.append(it)
    return un


def main():
    groups = json.loads(GROUPS.read_text())
    reps = {u["unit"] for u in json.loads((PLANS / "index.json").read_text())["units"]}
    rep_of_group = []
    for g in groups:
        r = [u for u in g if u in reps]
        rep_of_group.append(r[0] if r else None)
    doc = pymupdf.open(PDF)
    floors = json.loads((DATA / "index.json").read_text())["floors"]
    # representative shapes/drawings in their own frame
    rep_data = {}
    for r in reps:
        d = json.loads((PLANS / f"unit-{r}.json").read_text())
        fd = load_floor(d["floor"])
        poly, bal = unit_shape(fd, r)
        pts = poly + bal
        bb = bbox(pts)
        rep_data[r] = {"poly": transform(poly, False, False, bb), "bal": transform(bal, False, False, bb), "bb": bb,
                       "drw": [(c, f, s, tuple(transform(list(sg), False, False, bb))) for (c, f, s, sg) in unit_drawings(doc, fd, poly, bal)]}
    report = {}
    summary = collections.Counter()
    for F in floors:
        fd = load_floor(F)
        for u in fd["units"]:
            poly, bal = unit_shape(fd, u)
            pts = poly + bal
            bb = bbox(pts)
            drw = unit_drawings(doc, fd, poly, bal)
            best = None
            for r, R in rep_data.items():
                # quick reject by bbox size (either orientation same since mirror keeps w/h)
                if abs((bb[2] - bb[0]) - (R["bb"][2] - R["bb"][0])) > 3 or abs((bb[3] - bb[1]) - (R["bb"][3] - R["bb"][1])) > 3:
                    continue
                for fx in (False, True):
                    for fy in (False, True):
                        P = transform(poly, fx, fy, bb)
                        h = hausdorff(P, R["poly"])
                        if bal and R["bal"]:
                            h = max(h, hausdorff(transform(bal, fx, fy, bb), R["bal"]))
                        if best is None or h < best["h"]:
                            best = {"rep": r, "fx": fx, "fy": fy, "h": h}
            if best is None:
                report[u] = {"floor": F, "rep": None, "note": "no rep with same bbox"}
                summary["NO_REP"] += 1
                continue
            R = rep_data[best["rep"]]
            A = [(c, f, s, tuple(transform(list(sg), best["fx"], best["fy"], bb))) for (c, f, s, sg) in drw]
            unA = match_segments(A, R["drw"], TOL)
            unB = match_segments(R["drw"], A, TOL)
            cats = collections.Counter(it[0] for it in unA); catsB = collections.Counter(it[0] for it in unB)
            tot = collections.Counter(it[0] for it in A)
            struct_un = cats["wall"] + cats["door"] + cats["window"] + catsB["wall"] + catsB["door"] + catsB["window"]
            status = "OK" if best["h"] <= TOL and struct_un == 0 else ("OUTLINE" if best["h"] > TOL else "INTERIOR")
            summary[status] += 1
            def brief(lst):
                out = []
                for c, f, s, sg in lst:
                    if c in ("wall", "door", "window"):
                        out.append(f"{c} ({sg[0][0]:.0f},{sg[0][1]:.0f})-({sg[1][0]:.0f},{sg[1][1]:.0f})")
                return out[:12]
            report[u] = {"floor": F, "rep": best["rep"], "flipx": best["fx"], "flipy": best["fy"], "h": round(best["h"], 2), "status": status,
                         "unmatched_unit": dict(cats), "unmatched_rep": dict(catsB), "total": dict(tot), "examples_unit": brief(unA), "examples_rep": brief(unB)}
    # print
    for F in floors:
        row = []
        for u, r in report.items():
            if r["floor"] != F:
                continue
            if r.get("rep") is None:
                row.append(f"{u}:NO_REP"); continue
            fl = ("x" if r["flipx"] else "") + ("y" if r["flipy"] else "")
            row.append(f"{u}->{r['rep']}{('/' + fl) if fl else ''} h={r['h']} {r['status']}" + ("" if r["status"] == "OK" else f" un={r['unmatched_unit']}|{r['unmatched_rep']}"))
        print(f"floor {F}: " + " ; ".join(row))
    print("SUMMARY", dict(summary))
    if OUT_JSON:
        OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
