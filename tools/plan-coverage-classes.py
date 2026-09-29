#!/usr/bin/env python3
"""Классы точного совпадения планировок по всему дому (перенос + отражения по x/y).

.venv/bin/python tools/plan-coverage-classes.py [--tol 1.0] [--out plan-studio/coverage-classes.json]

1. Для каждой квартиры каждого этажа берутся контур (floor-F.json) и структурные векторы PDF (стены #6A6A6A/#FD8379,
   двери #FF6600, окна = белые заливки) в кадре квартиры.
2. Пары сравниваются в 4 отражениях: выравнивание по bbox контура, затем уточнение переноса голосованием
   по стенным сегментам, затем hausdorff контура и доля несовпавших структурных сегментов (в обе стороны).
3. Квартиры жадно раскладываются по классам: совпадение = hausdorff ≤ tol и несовпавших структурных сегментов ≤ 2 %
   (и ≤ 6 штук). Класс "покрыт", если в нём есть один из 22 собранных типов (plan-studio/v3/plans/index.json).
Отчёт: классы, их члены, покрытие, для непокрытых — ближайший тип и что отличается.
"""
import json, sys, math, collections
from pathlib import Path
import pymupdf
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
PLANS = ROOT / "plan-studio/v3/plans"
DATA = ROOT / "plan-studio/data"
PDF = fpconfig.PDF
TOL = 1.0; OUT = ROOT / "plan-studio/coverage-classes.json"
for i, a in enumerate(sys.argv):
    if a == "--tol": TOL = float(sys.argv[i + 1])
    if a == "--out": OUT = Path(sys.argv[i + 1])
WALL = {"#6A6A6A", "#FD8379"}; DOOR = "#FF6600"


def rgb(c):
    return None if c is None else "#%02X%02X%02X" % tuple(int(round(x * 255)) for x in c[:3])


def seg_dist_point(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay; L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def poly_points(poly, step=2.0):
    pts = []; n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]; L = math.hypot(b[0] - a[0], b[1] - a[1]); k = max(1, int(L / step))
        for j in range(k):
            t = j / k; pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return pts


def hausdorff(P, Q):
    def d1(A, B):
        worst = 0; n = len(B)
        for p in A:
            best = min(seg_dist_point(p, B[i], B[(i + 1) % n]) for i in range(n))
            if best > worst: worst = best
        return worst
    return max(d1(poly_points(P), Q), d1(poly_points(Q), P))


def bbox(pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def tf(pts, fx, fy, bb, sh=(0, 0)):
    x0, y0, x1, y1 = bb; out = []
    for x, y in pts:
        nx = (x1 - (x - x0)) if fx else x; ny = (y1 - (y - y0)) if fy else y
        out.append((nx - x0 + sh[0], ny - y0 + sh[1]))
    return out


class Unit:
    pass


def load_all():
    doc = pymupdf.open(PDF); floors = json.loads((DATA / "index.json").read_text())["floors"]
    units = []; pagecache = {}
    for F in floors:
        fd = json.loads((DATA / f"floor-{F}.json").read_text()); fx0, fy0 = fd["bbox"][0], fd["bbox"][1]
        pg = fd["page"]
        if pg not in pagecache: pagecache[pg] = doc[pg - 1].get_drawings()
        for u, d in fd["units"].items():
            U = Unit(); U.id = u; U.floor = F; U.poly = [tuple(p) for p in d["poly"]]; U.bal = [tuple(p) for p in (d.get("balcony") or [])]
            pts = U.poly + U.bal; U.bb = bbox(U.poly); U.bbAll = bbox(pts); x0, y0, x1, y1 = U.bbAll; m = 2.0
            segs = []
            for dr in pagecache[pg]:
                r = dr["rect"]
                if r.x1 - fx0 < x0 - m or r.x0 - fx0 > x1 + m or r.y1 - fy0 < y0 - m or r.y0 - fy0 > y1 + m: continue
                fill = rgb(dr.get("fill")); stroke = rgb(dr.get("color"))
                cat = "wall" if fill in WALL else ("door" if stroke == DOOR else ("window" if fill == "#FFFFFF" else None))
                if cat is None: continue
                for it in dr["items"]:
                    k = it[0]
                    if k == "l": ss = [((it[1].x - fx0, it[1].y - fy0), (it[2].x - fx0, it[2].y - fy0))]
                    elif k == "c": ss = [((it[1].x - fx0, it[1].y - fy0), (it[4].x - fx0, it[4].y - fy0))]
                    elif k == "re":
                        rr = it[1]; a = (rr.x0 - fx0, rr.y0 - fy0); b = (rr.x1 - fx0, rr.y0 - fy0); c = (rr.x1 - fx0, rr.y1 - fy0); dd = (rr.x0 - fx0, rr.y1 - fy0); ss = [(a, b), (b, c), (c, dd), (dd, a)]
                    elif k == "qu":
                        q = it[1]; P = [(q.ul.x - fx0, q.ul.y - fy0), (q.ur.x - fx0, q.ur.y - fy0), (q.lr.x - fx0, q.lr.y - fy0), (q.ll.x - fx0, q.ll.y - fy0)]; ss = [(P[i], P[(i + 1) % 4]) for i in range(4)]
                    else: ss = []
                    for s in ss:
                        mx = (s[0][0] + s[1][0]) / 2; my = (s[0][1] + s[1][1]) / 2
                        if x0 - m <= mx <= x1 + m and y0 - m <= my <= y1 + m and math.hypot(s[1][0] - s[0][0], s[1][1] - s[0][1]) > 0.3:
                            segs.append((cat, s))
            U.segs = segs
            units.append(U)
    return units


def place(U, fx, fy, sh=(0, 0)):
    return {"poly": tf(U.poly, fx, fy, U.bb, sh), "bal": tf(U.bal, fx, fy, U.bb, sh), "segs": [(c, tuple(tf(list(s), fx, fy, U.bb, sh))) for c, s in U.segs]}


def index_segs(segs):
    idx = collections.defaultdict(list)
    for c, (a, b) in segs:
        idx[(c, round(a[0] / 4), round(a[1] / 4))].append((a, b)); idx[(c, round(b[0] / 4), round(b[1] / 4))].append((a, b))
    return idx


def unmatched(A, idxB, tol):
    un = []
    for c, (a, b) in A:
        ok = False
        for (p, q) in idxB.get((c, round(a[0] / 4), round(a[1] / 4)), []):
            if (math.hypot(a[0] - p[0], a[1] - p[1]) <= tol and math.hypot(b[0] - q[0], b[1] - q[1]) <= tol) or (math.hypot(a[0] - q[0], a[1] - q[1]) <= tol and math.hypot(b[0] - p[0], b[1] - p[1]) <= tol):
                ok = True; break
        if not ok:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if ok: break
                    for (p, q) in idxB.get((c, round(a[0] / 4) + dx, round(a[1] / 4) + dy), []):
                        if (math.hypot(a[0] - p[0], a[1] - p[1]) <= tol and math.hypot(b[0] - q[0], b[1] - q[1]) <= tol) or (math.hypot(a[0] - q[0], a[1] - q[1]) <= tol and math.hypot(b[0] - p[0], b[1] - p[1]) <= tol):
                            ok = True; break
        if not ok: un.append((c, (a, b)))
    return un


def refine_shift(A, B):
    """Голосование по стенным сегментам: сдвиг, совмещающий максимум стен."""
    bw = [(a, b) for c, (a, b) in B if c == "wall"]
    votes = collections.Counter()
    for c, (a, b) in A:
        if c != "wall": continue
        L = math.hypot(b[0] - a[0], b[1] - a[1]); mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2; hor = abs(b[0] - a[0]) >= abs(b[1] - a[1])
        for (p, q) in bw:
            L2 = math.hypot(q[0] - p[0], q[1] - p[1])
            if abs(L - L2) > 0.5 or (abs(q[0] - p[0]) >= abs(q[1] - p[1])) != hor: continue
            dx = (p[0] + q[0]) / 2 - mx; dy = (p[1] + q[1]) / 2 - my
            if abs(dx) <= 12 and abs(dy) <= 12: votes[(round(dx * 4) / 4, round(dy * 4) / 4)] += 1
    if not votes: return (0, 0), 0
    (sh, n) = votes.most_common(1)[0]
    return sh, n


def seglen(s):
    return math.hypot(s[1][0] - s[0][0], s[1][1] - s[0][1])


def clip_seg(a, b, box):
    """Liang–Barsky: отрезок a-b, обрезанный по прямоугольнику box=(x0,y0,x1,y1); None если вне."""
    x0, y0, x1, y1 = box; dx, dy = b[0] - a[0], b[1] - a[1]; t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0: return None
            continue
        t = q / p
        if p < 0:
            if t > t1: return None
            if t > t0: t0 = t
        else:
            if t < t0: return None
            if t < t1: t1 = t
    if t1 - t0 <= 0: return None
    return ((a[0] + dx * t0, a[1] + dy * t0), (a[0] + dx * t1, a[1] + dy * t1))


def trim(segs, box):
    out = []
    for c, (a, b) in segs:
        r = clip_seg(a, b, box)
        if r and seglen(r) > 0.5: out.append((c, r))
    return out


def placed_box(U, fx, fy, sh):
    x0, y0, x1, y1 = U.bbAll; pts = tf([(x0, y0), (x1, y1)], fx, fy, U.bb, sh)
    return (min(pts[0][0], pts[1][0]), min(pts[0][1], pts[1][1]), max(pts[0][0], pts[1][0]), max(pts[0][1], pts[1][1]))


TOLC = {"wall": 1.5, "door": 2.5, "window": 2.5}


def grid_index(segs):
    idx = collections.defaultdict(list)
    for c, (a, b) in segs:
        x0, x1 = sorted((a[0], b[0])); y0, y1 = sorted((a[1], b[1]))
        for gx in range(int(math.floor((x0 - 3) / 8)), int(math.floor((x1 + 3) / 8)) + 1):
            for gy in range(int(math.floor((y0 - 3) / 8)), int(math.floor((y1 + 3) / 8)) + 1):
                idx[(c, gx, gy)].append((a, b))
    return idx


def uncovered(A, idxB):
    """Для каждого сегмента A: длина, не лежащая на сегментах той же категории B (в пределах TOLC)."""
    per_cat = collections.Counter(); worst = {}; ex = collections.defaultdict(list); total = collections.Counter()
    for c, (a, b) in A:
        L = seglen((a, b)); total[c] += L; tol = TOLC[c]
        n = max(1, int(L / 1.5)); miss = 0
        for i in range(n + 1):
            t = i / n; p = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            ok = False
            for (u, v) in idxB.get((c, int(math.floor(p[0] / 8)), int(math.floor(p[1] / 8))), []):
                if seg_dist_point(p, u, v) <= tol: ok = True; break
            if not ok: miss += 1
        if miss:
            um = L * miss / (n + 1); per_cat[c] += um
            if um > worst.get(c, 0): worst[c] = um
            if um >= 3 and len(ex[c]) < 6: ex[c].append(f"({a[0]:.0f},{a[1]:.0f})-({b[0]:.0f},{b[1]:.0f}) L={L:.0f} miss={um:.0f}")
    return {c: round(per_cat[c], 1) for c in TOLC}, {c: round(worst.get(c, 0), 1) for c in TOLC}, {c: round(total[c], 1) for c in TOLC}, dict(ex)


def compare(U, V, tol=None):
    """Лучшее совмещение U на V: выравнивание по bbox контура, затем сдвиг по стенам; метрика = непокрытая длина по категориям."""
    B = place(V, False, False); best = None
    for fx in (False, True):
        for fy in (False, True):
            A0 = place(U, fx, fy); sh, n = refine_shift(A0["segs"], B["segs"])
            A = place(U, fx, fy, sh) if sh != (0, 0) else A0
            h = hausdorff(A["poly"], B["poly"])
            hb = hausdorff(A["bal"], B["bal"]) if (U.bal and V.bal) else None
            ba = placed_box(U, fx, fy, sh); bbv = placed_box(V, False, False, (0, 0)); box = (max(ba[0], bbv[0]) + 1, max(ba[1], bbv[1]) + 1, min(ba[2], bbv[2]) - 1, min(ba[3], bbv[3]) - 1)
            As = trim(A["segs"], box); Bs = trim(B["segs"], box)
            unA, wA, totA, exA = uncovered(As, grid_index(Bs)); unB, wB, totB, exB = uncovered(Bs, grid_index(As))
            score = (unA["wall"] + unB["wall"]) / max(1, totA["wall"] + totB["wall"]) + (unA["door"] + unB["door"]) / max(1, totA["door"] + totB["door"]) + 0.5 * (unA["window"] + unB["window"]) / max(1, totA["window"] + totB["window"]) + h * 0.02
            if best is None or score < best["score"]:
                best = {"score": round(score, 4), "fx": fx, "fy": fy, "shift": sh, "h": round(h, 2), "hbal": None if hb is None else round(hb, 2),
                        "U": {"un": unA, "worst": wA, "tot": totA, "ex": exA}, "V": {"un": unB, "worst": wB, "tot": totB, "ex": exB}}
    return best


def same(res, tol, level="loose"):
    a, b = res["U"], res["V"]
    wall = a["un"]["wall"] + b["un"]["wall"]; wworst = max(a["worst"]["wall"], b["worst"]["wall"])
    door = a["un"]["door"] + b["un"]["door"]; win = a["un"]["window"] + b["un"]["window"]; wworstw = max(a["worst"]["window"], b["worst"]["window"])
    hb = res["hbal"]
    if level == "strict":
        return res["h"] <= 1.5 and wall <= 8 and wworst <= 6 and door <= 6 and win <= 25 and wworstw <= 8 and (hb is None or hb <= 3)
    return res["h"] <= 3.5 and wall <= 12 and wworst <= 4 and door <= 12 and (hb is None or hb <= 4)


def flags(res):
    """Чем пара отличается (текстовые флаги для отчёта)."""
    a, b = res["U"], res["V"]; f = []
    wall = a["un"]["wall"] + b["un"]["wall"]; door = a["un"]["door"] + b["un"]["door"]; win = a["un"]["window"] + b["un"]["window"]
    if wall > 12 or max(a["worst"]["wall"], b["worst"]["wall"]) > 4: f.append(f"стены Δ{wall:.0f}pt")
    if door > 12: f.append(f"двери Δ{door:.0f}pt")
    if res["h"] > 1.5: f.append(f"контур {res['h']:.1f}pt")
    if res["hbal"] is not None and res["hbal"] > 3: f.append(f"балкон {res['hbal']:.1f}pt")
    if win > 25: f.append(f"окна/колонны/остекление Δ{win:.0f}pt")
    return f


def main():
    units = load_all(); byid = {U.id: U for U in units}
    reps = [u["unit"] for u in json.loads((PLANS / "index.json").read_text())["units"]]
    order = sorted(units, key=lambda U: (U.id not in reps, U.floor, int(U.id)))   # представители первыми
    classes = []  # {canon, members, res}
    assign = {}
    for U in order:
        w, h = U.bb[2] - U.bb[0], U.bb[3] - U.bb[1]; hit = None
        cands = [C for C in classes if abs(C["w"] - w) <= 4 and abs(C["h"] - h) <= 4]
        for C in cands:
            res = compare(U, byid[C["canon"]], TOL)
            if same(res, TOL): hit = (C, res); break
        if hit:
            C, res = hit; C["members"].append(U.id); assign[U.id] = {"class": C["canon"], "fx": res["fx"], "fy": res["fy"], "h": res["h"], "hbal": res["hbal"], "strict": same(res, TOL, "strict"), "flags": flags(res), "U": res["U"], "V": res["V"]}
        else:
            classes.append({"canon": U.id, "w": w, "h": h, "members": [U.id]}); assign[U.id] = {"class": U.id, "fx": False, "fy": False, "h": 0, "hbal": 0, "strict": True, "flags": [], "U": {}, "V": {}}
        a = assign[U.id]; print(f"{U.id}: class {a['class']} fx={a['fx']} fy={a['fy']} strict={a['strict']} flags={a['flags']} h={a['h']} hbal={a['hbal']} unU={a['U'].get('un')} unV={a['V'].get('un')}", flush=True)
    # coverage + nearest rep for uncovered classes
    repU = [byid[r] for r in reps]
    out = []
    for C in classes:
        covered = any(m in reps for m in C["members"]); near = None
        if not covered:
            cu = byid[C["canon"]]
            for R in repU:
                if abs((R.bb[2] - R.bb[0]) - C["w"]) > 12 or abs((R.bb[3] - R.bb[1]) - C["h"]) > 12: continue
                res = compare(cu, R, TOL)
                if near is None or res["score"] < near["score"]: near = dict(res, rep=R.id)
            if near is None:
                for R in repU:
                    res = compare(cu, R, TOL)
                    if near is None or res["score"] < near["score"]: near = dict(res, rep=R.id)
        if near: near["flags"] = flags(near)
        out.append({"canon": C["canon"], "covered": covered, "n": len(C["members"]), "members": C["members"], "nearest": near,
                    "member_flags": {m: assign[m]["flags"] for m in C["members"] if assign[m]["flags"]}})
    print("\n=== CLASSES ===")
    for C in out:
        line = f"{'OK ' if C['covered'] else 'MISS'} class {C['canon']} n={C['n']}: {', '.join(C['members'])}"
        if C["member_flags"]: line += f"\n     flags: {C['member_flags']}"
        if C["nearest"]:
            nr = C["nearest"]; line += f"\n     nearest {nr['rep']} fx={nr['fx']} fy={nr['fy']} FLAGS={nr['flags']} h={nr['h']} hbal={nr['hbal']} unU={nr['U']['un']} worstU={nr['U']['worst']} exU={nr['U']['ex']} | unR={nr['V']['un']} worstR={nr['V']['worst']} exR={nr['V']['ex']}"
        print(line)
    print(f"TOTAL classes {len(out)}, covered {sum(1 for c in out if c['covered'])}, missing {sum(1 for c in out if not c['covered'])}, units in missing classes {sum(c['n'] for c in out if not c['covered'])}")
    OUT.write_text(json.dumps({"tol": TOL, "assign": assign, "classes": out}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
