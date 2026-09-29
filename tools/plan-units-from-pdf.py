#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plan-units-from-pdf.py - apartment and balcony polygons straight from the architect's PDF.

The first extractor (extract-plans.py :: match_balconies) guessed balconies with a heuristic and
often put them on the wrong side of the unit. The PDF has an OCG layer (units.layer in the layer
config) where the architect outlined the measured areas:

    unit_color     (153,153,153) #999999 -> apartment outlines (area >= min_unit_m2; smaller = markers)
    balcony_color  (0,0,0)               -> balcony outlines   (area >= min_balcony_m2; a balcony can be 2 pieces)
    other colors                          -> dimensions, hatch - ignored

The outlines are drawn as SINGLE segments (items = one ('l', a, b)), so they are chained into closed
loops by matching ends (eps 0.05 pt). Area = shoelace(pt) * M_PER_PT^2.

Steps:
  1. chain loops, keep grey (units) and black (balconies) inside the floor bbox;
  2. match grey loops to unit numbers: by max overlap with the OLD units[N].poly, and where there is
     none (or overlap < 50 %) by the area from the official schedule (living +-3 %) and by the area
     label from fd["texts"] inside the loop;
  3. attach each black loop to the unit it borders (shared edge >= 0.5 pt or distance <= 3.0 pt, the
     wall thickness between the two outlines, see NEAR_MAX_PT); ambiguous ones go to the unit whose
     balcony sum is closer to the schedule;
  4. check |area(poly) - living|/living <= 3 % and |sum balconies - balcony|/balcony <= 5 %,
     everything that fails gets units[N]["_check"] and goes into the report;
  5. write floor-F.json in place (other fields kept),
     units[N]["balconies"] = [poly, ...] + units[N]["balcony"] = the largest one (compatibility).

No fitting of polygons: the file gets exactly the loops from the PDF. Anything doubtful goes to the report.

Usage:
    uv run tools/plan-units-from-pdf.py 26
    uv run tools/plan-units-from-pdf.py --all --dry
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from itertools import product
from pathlib import Path

import sys

import pymupdf
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
DATA = ROOT / "plan-studio" / "data"
PRICE = fpconfig.SCHEDULE          # official area schedule: units[] with number, floor, living, balcony
PDF = fpconfig.PDF

_UCFG = fpconfig.layers()["units"]
LAYER = _UCFG["layer"]
COLOR_UNIT = tuple(_UCFG.get("unit_color", (153, 153, 153)))
COLOR_BALCONY = tuple(_UCFG.get("balcony_color", (0, 0, 0)))
PT_TO_M = fpconfig.M_PER_PT # 1 pt чертежа = 7.05 см на листе 1:200
MIN_UNIT_M2 = float(_UCFG.get("min_unit_m2", 8.0))
MIN_BALCONY_M2 = float(_UCFG.get("min_balcony_m2", 1.0))
CHAIN_EPS = 0.05            # pt, сцепка концов отрезков
OVERLAP_MIN = 0.50          # доля площади петли, перекрытая старым poly -> уверенное совпадение
LIVING_TOL = 0.03
BALCONY_TOL = 0.05
SHARE_MIN_PT = 0.5          # длина общей границы квартира/балкон
# Замер на этаже 26 (ревьюер, 2026-09-22): контур квартиры и контур балкона обведены по РАЗНЫМ
# граням разделяющей стены, поэтому «расстояние < 0.3 pt» из ТЗ не срабатывает почти нигде -
# реальный зазор равен толщине стены и составляет 1.41 / 2.09 / 2.12 pt (10-15 см). Порог
# поднят до толщины стены; всё, что дальше, соседом не считается (ближайший «чужой» кандидат
# на 26-м - 3.01 pt). Это единственное отступление от ТЗ, см. отчёт.
NEAR_MAX_PT = 3.0           # либо просто расстояние между полигонами (через стену)
# ревьюер, 2026-09-22: серые петли >= MIN_UNIT_M2, которым не нашлось квартиры в прайсе - это
# ОФИСЫ. Они есть только на 2-м этаже (40.7 и 35.1 м²); на любом другом этаже такая петля -
# признак ошибки сопоставления, поэтому туда офисы НЕ пишем, только сообщаем в отчёт.
OFFICE_FLOORS = set(_UCFG.get("office_floors", []))


# ----------------------------------------------------------------- геометрия
def chain_loops(sg, eps=CHAIN_EPS):
    """sg = [((ax,ay),(bx,by)), ...] -> список путей (петель) по совпадению концов."""
    used = [False] * len(sg)
    out = []
    near = lambda p, q: abs(p[0] - q[0]) < eps and abs(p[1] - q[1]) < eps
    for i, (a, b) in enumerate(sg):
        if used[i]:
            continue
        used[i] = True
        path = [a, b]
        while True:
            end = path[-1]
            found = False
            for j, (c, d) in enumerate(sg):
                if used[j]:
                    continue
                if near(c, end):
                    path.append(d); used[j] = True; found = True; break
                if near(d, end):
                    path.append(c); used[j] = True; found = True; break
            if not found or near(path[-1], path[0]):
                break
        out.append(path)
    return out


def shoelace_m2(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        s += ax * by - bx * ay
    return abs(s) / 2.0 * PT_TO_M * PT_TO_M


def dedupe(pts, eps=CHAIN_EPS):
    """Убрать замыкающую точку-дубль и подряд идущие совпадения (петля хранится открытой,
    как в существующем floor-F.json)."""
    out = []
    for p in pts:
        if out and abs(p[0] - out[-1][0]) < eps and abs(p[1] - out[-1][1]) < eps:
            continue
        out.append(p)
    while len(out) > 1 and abs(out[0][0] - out[-1][0]) < eps and abs(out[0][1] - out[-1][1]) < eps:
        out.pop()
    return out


def shp(pts):
    p = Polygon(pts)
    if not p.is_valid:
        p = p.buffer(0)
    return p


def point_in_poly(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


# ----------------------------------------------------------------- PDF
def pdf_loops(page, bbox):
    """Петли слоя LAYER внутри bbox этажа, в локальных pt. -> {color: [(pts, area_m2), ...]}"""
    x0, y0, x1, y1 = bbox
    segs = defaultdict(list)
    for d in page.get_drawings():
        if d.get("layer") != LAYER:
            continue
        items = d["items"]
        if len(items) != 1 or items[0][0] != "l":
            continue                      # многосегментные пути в слое - штриховка, не контуры
        col = d.get("color")
        if col is None:
            continue
        c = tuple(int(round(v * 255)) for v in col)
        if c not in (COLOR_UNIT, COLOR_BALCONY):
            continue                      # (223,0,0) размеры, (219,184,0) штриховка и прочее
        a, b = items[0][1], items[0][2]
        if not (x0 - 1 <= a.x <= x1 + 1 and y0 - 1 <= a.y <= y1 + 1):
            continue
        if not (x0 - 1 <= b.x <= x1 + 1 and y0 - 1 <= b.y <= y1 + 1):
            continue
        segs[c].append(((a.x - x0, a.y - y0), (b.x - x0, b.y - y0)))

    out = {}
    for c, sg in segs.items():
        loops = []
        for path in chain_loops(sg):
            pts = dedupe([(round(x, 2), round(y, 2)) for x, y in path])
            if len(pts) < 3:
                continue
            loops.append((pts, shoelace_m2(pts)))
        out[c] = loops
    return out


# ----------------------------------------------------------------- сопоставление
def match_units(unit_loops, fd_units, price_by_number, texts, problems):
    """Серые петли -> номера квартир. Возвращает {number: pts} и список нерешённых петель."""
    numbers = sorted(price_by_number, key=lambda n: int(n))
    loop_shapes = [shp(p) for p, _ in unit_loops]

    # шаг 1: по максимальному пересечению со старым poly (жадно, от лучшей пары)
    pairs = []
    for li, ls in enumerate(loop_shapes):
        if ls.is_empty or ls.area <= 0:
            continue
        for n in numbers:
            old = fd_units.get(n, {}).get("poly")
            if not old:
                continue
            try:
                inter = ls.intersection(shp(old)).area
            except Exception:
                continue
            if inter <= 0:
                continue
            frac = inter / ls.area
            if frac >= OVERLAP_MIN:
                pairs.append((frac, li, n))
    pairs.sort(reverse=True)
    assign = {}
    used_loops = set()
    for frac, li, n in pairs:
        if li in used_loops or n in assign:
            continue
        assign[n] = li
        used_loops.add(li)

    # шаг 2: остаток - по площади из прайса (living ±3 %) и по подписи площади внутри петли
    rest_loops = [li for li in range(len(unit_loops)) if li not in used_loops]
    rest_numbers = [n for n in numbers if n not in assign]
    for n in list(rest_numbers):
        living = price_by_number[n].get("living") or 0.0
        if living <= 0:
            continue
        cands = []
        for li in rest_loops:
            if li in used_loops:
                continue
            area = unit_loops[li][1]
            if abs(area - living) / living > LIVING_TOL:
                continue
            # подпись площади внутри петли (fd["texts"]) - вторая опора
            label = None
            for t in texts:
                try:
                    v = float(t["str"])
                except (ValueError, KeyError, TypeError):
                    continue
                if point_in_poly((t["x"], t["y"]), unit_loops[li][0]):
                    if abs(v - living) / living <= LIVING_TOL:
                        label = v
                        break
            cands.append((abs(area - living), li, label))
        if not cands:
            continue
        cands.sort()
        # если подпись подтверждает ровно одну петлю - берём её, иначе ближайшую по площади,
        # но только когда она однозначна (следующая хуже более чем вдвое)
        labeled = [c for c in cands if c[2] is not None]
        if len(labeled) == 1:
            pick = labeled[0]
        elif len(cands) == 1:
            pick = cands[0]
        elif cands[1][0] > cands[0][0] * 2 + 0.05:
            pick = cands[0]
        else:
            problems.append(f"{n}: several grey loops fit the area "
                            f"({', '.join(f'{unit_loops[li][1]:.1f}' for _, li, _ in cands)} м² "
                            f"for living {living}) - not guessing")
            continue
        assign[n] = pick[1]
        used_loops.add(pick[1])
        rest_numbers.remove(n)

    for n in numbers:
        if n not in assign:
            problems.append(f"{n}: no grey loop found (living {price_by_number[n].get('living')} m2)")
    leftover = [li for li in range(len(unit_loops)) if li not in used_loops]
    for li in leftover:
        pass  # разбирается вызывающим кодом как офис (см. match_offices / OFFICE_FLOORS)
    return {n: unit_loops[li][0] for n, li in assign.items()}, leftover


def match_balconies(bal_loops, unit_polys, price_by_number, problems, notes):
    """Чёрные петли -> квартиры. {number: [pts, ...]}"""
    shapes = {n: shp(p) for n, p in unit_polys.items()}
    cand = {}
    for bi, (pts, area) in enumerate(bal_loops):
        bs = shp(pts)
        hits = []
        for n, us in shapes.items():
            try:
                dist = us.distance(bs)
                share = bs.exterior.intersection(us.buffer(0.15)).length
            except Exception:
                continue
            if share >= SHARE_MIN_PT or dist < NEAR_MAX_PT:
                hits.append((share, dist, n))
        hits.sort(key=lambda t: (-t[0], t[1]))
        cand[bi] = [h[2] for h in hits]

    out = defaultdict(list)
    ambiguous, orphans = [], []
    for bi, hits in cand.items():
        if len(hits) == 1:
            out[hits[0]].append(bi)
        elif not hits:
            orphans.append(bi)
        else:
            ambiguous.append(bi)

    # неоднозначные: перебор вариантов, минимизируем суммарное отклонение от прайса balcony
    if ambiguous:
        opts = [cand[bi][:3] for bi in ambiguous]
        combos = 1
        for o in opts:
            combos *= len(o)
        if combos > 4096:
            opts = [o[:2] for o in opts]
        best, best_err = None, None
        for combo in product(*opts):
            trial = {n: list(v) for n, v in out.items()}
            for bi, n in zip(ambiguous, combo):
                trial.setdefault(n, []).append(bi)
            err = 0.0
            for n in price_by_number:
                want = price_by_number[n].get("balcony") or 0.0
                got = sum(bal_loops[bi][1] for bi in trial.get(n, []))
                err += abs(got - want)
            if best_err is None or err < best_err:
                best_err, best = err, combo
        for bi, n in zip(ambiguous, best):
            out[n].append(bi)
            notes.append(f"balcony {bal_loops[bi][1]:.1f} m2 borders {cand[bi]} "
                         f"-> given to {n} (closest balcony sum in the schedule)")

    return ({n: [bal_loops[bi][0] for bi in sorted(v, key=lambda i: -bal_loops[i][1])]
             for n, v in out.items()}, cand, orphans)


def match_offices(office_loops, orphan_bal, bal_loops, problems):
    """Серые петли без квартиры в прайсе = офисы. К каждому офису привязываются те чёрные
    петли, что остались НЕ привязанными к квартирам (квартиры прайса имеют приоритет - их
    балконы уже разобраны match_balconies) и граничат с офисом по тому же правилу
    (общая граница >= SHARE_MIN_PT или расстояние <= NEAR_MAX_PT)."""
    offices = []
    used = set()
    for pts, area in office_loops:
        os_ = shp(pts)
        mine = []
        for bi in orphan_bal:
            if bi in used:
                continue
            bs = shp(bal_loops[bi][0])
            try:
                dist = os_.distance(bs)
                share = bs.exterior.intersection(os_.buffer(0.15)).length
            except Exception:
                continue
            if share >= SHARE_MIN_PT or dist <= NEAR_MAX_PT:
                mine.append(bi)
                used.add(bi)
        mine.sort(key=lambda i: -bal_loops[i][1])
        offices.append({
            "poly": [[round(x, 1), round(y, 1)] for x, y in pts],
            "balconies": [[[round(x, 1), round(y, 1)] for x, y in bal_loops[bi][0]] for bi in mine],
            "area": round(area, 1),
        })
    for bi in orphan_bal:
        if bi not in used:
            problems.append(f"balcony {bal_loops[bi][1]:.1f} m2 borders neither a scheduled unit "
                            f"nor an office - not attached")
    return offices


# ----------------------------------------------------------------- этаж
def process_floor(floor, price_units, doc, dry, problems, notes):
    fpath = DATA / f"floor-{floor}.json"
    fd = json.loads(fpath.read_text(encoding="utf-8"))
    page = doc[fd["page"] - 1]
    loops = pdf_loops(page, fd["bbox"])

    unit_loops = [(p, a) for p, a in loops.get(COLOR_UNIT, []) if a >= MIN_UNIT_M2]
    bal_loops = [(p, a) for p, a in loops.get(COLOR_BALCONY, []) if a >= MIN_BALCONY_M2]

    price_by_number = {str(u["number"]): u for u in price_units if int(u["floor"]) == floor}
    if not price_by_number:
        problems.append(f"floor {floor}: no units in the schedule")
        return None

    unit_polys, leftover = match_units(unit_loops, fd["units"], price_by_number,
                                       fd.get("texts", []), problems)
    bal_by_unit, _, orphan_bal = match_balconies(bal_loops, unit_polys, price_by_number,
                                                 problems, notes)

    # ---- офисы: серые петли без квартиры в прайсе (+ оставшиеся чёрные петли рядом с ними)
    office_loops = [unit_loops[li] for li in leftover]
    if office_loops and floor not in OFFICE_FLOORS:
        for pts, area in office_loops:
            problems.append(f"grey loop {area:.1f} m2 without a scheduled unit on floor {floor} "
                            f"- offices are expected only on {sorted(OFFICE_FLOORS)}, NOT written")
        offices = []
    else:
        offices = match_offices(office_loops, orphan_bal, bal_loops, problems)
    for o in offices:
        notes.append(f"office {o['area']} m2, balconies {len(o['balconies'])} "
                     f"({', '.join(f'{shoelace_m2(b):.1f}' for b in o['balconies']) or '-'} м²)")
    if not offices and orphan_bal and floor not in OFFICE_FLOORS:
        for bi in orphan_bal:
            problems.append(f"balcony {bal_loops[bi][1]:.1f} m2 borders no scheduled unit "
                            f"- not attached")

    # ---- сверка системы координат: новые серые петли против старых poly
    coord_dev = []
    for n, pts in unit_polys.items():
        old = fd["units"].get(n, {}).get("poly")
        if not old:
            continue
        a, b = shp(pts), shp(old)
        if a.area > 0:
            coord_dev.append(a.intersection(b).area / max(a.area, b.area))
    coord_iou = min(coord_dev) if coord_dev else None

    # ---- запись
    units_new = dict(fd["units"])
    ok_living = ok_balcony = 0
    bad_living, bad_balcony = [], []
    for n in sorted(price_by_number, key=lambda x: int(x)):
        pr = price_by_number[n]
        u = dict(units_new.get(n) or {})
        checks = []
        if n in unit_polys:
            pts = unit_polys[n]
            u["poly"] = [[round(x, 1), round(y, 1)] for x, y in pts]
            area = shoelace_m2(pts)
            living = pr.get("living") or 0.0
            if living > 0 and abs(area - living) / living <= LIVING_TOL:
                ok_living += 1
            else:
                bad_living.append(f"{n}: loop {area:.1f} vs living {living}")
                checks.append(f"living {area:.1f}≠{living}")
        else:
            checks.append("no grey loop in the PDF (poly left as is)")

        bals = bal_by_unit.get(n, [])
        want_bal = pr.get("balcony") or 0.0
        got_bal = sum(shoelace_m2(b) for b in bals)
        if bals:
            u["balconies"] = [[[round(x, 1), round(y, 1)] for x, y in b] for b in bals]
            u["balcony"] = max(u["balconies"], key=lambda p: abs(shoelace_m2(p)))
        else:
            u.pop("balconies", None)
            u["balcony"] = None
        if want_bal <= 0 and not bals:
            ok_balcony += 1
        elif want_bal > 0 and bals and abs(got_bal - want_bal) / want_bal <= BALCONY_TOL:
            ok_balcony += 1
        else:
            bad_balcony.append(f"{n}: {got_bal:.1f} ({len(bals)} pcs) vs balcony {want_bal}")
            checks.append(f"balcony {got_bal:.1f}≠{want_bal}")

        if checks:
            u["_check"] = "; ".join(checks)
        else:
            u.pop("_check", None)
        units_new[n] = u

    # квартиры, которых нет в прайсе, остаются как были
    fd_out = dict(fd)
    fd_out["units"] = units_new
    if offices:
        fd_out["offices"] = offices
    else:
        fd_out.pop("offices", None)

    if not dry:
        fpath.write_text(json.dumps(fd_out, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")

    return {
        "floor": floor,
        "price_n": len(price_by_number),
        "matched": len(unit_polys),
        "offices": len(offices),
        "office_bal": sum(len(o["balconies"]) for o in offices),
        "bal_found": len(bal_loops),
        "bal_assigned": (sum(len(v) for v in bal_by_unit.values())
                         + sum(len(o["balconies"]) for o in offices)),
        "ok_living": ok_living,
        "bad_living": bad_living,
        "ok_balcony": ok_balcony,
        "bad_balcony": bad_balcony,
        "coord_iou": coord_iou,
        "gray_loops": len(loops.get(COLOR_UNIT, [])),
        "black_loops": len(loops.get(COLOR_BALCONY, [])),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("floors", nargs="*", type=int)
    ap.add_argument("--all", action="store_true", help="все этажи из plan-studio/data/index.json")
    ap.add_argument("--dry", action="store_true", help="только отчёт, без записи")
    args = ap.parse_args()

    idx = json.loads((DATA / "index.json").read_text(encoding="utf-8"))
    floors = sorted(idx["floors"]) if args.all else args.floors
    if not floors:
        ap.error("укажи этажи или --all")

    price_units = json.loads(PRICE.read_text(encoding="utf-8"))["units"]
    doc = pymupdf.open(PDF)

    rows, all_problems, all_notes = [], {}, {}
    for f in floors:
        problems, notes = [], []
        try:
            row = process_floor(f, price_units, doc, args.dry, problems, notes)
        except Exception as e:
            problems.append(f"ERROR: {type(e).__name__}: {e}")
            row = None
        if row:
            rows.append(row)
        all_problems[f] = problems
        all_notes[f] = notes

    print(f"\nPDF: {PDF}   layer: {LAYER}   mode: {'DRY (nothing written)' if args.dry else 'WRITE'}\n")
    hdr = ("| Floor | units in schedule | matched | offices | balconies found | balconies attached "
           "| unit area ok/fail | balconies ok/fail | problems |")
    print(hdr)
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        f = r["floor"]
        nprob = len(all_problems.get(f, []))
        print(f"| {f} | {r['price_n']} | {r['matched']} "
              f"| {r['offices']}{' (+%s balc.)' % r['office_bal'] if r['offices'] else ''} "
              f"| {r['bal_found']} | {r['bal_assigned']} "
              f"| {r['ok_living']}/{r['price_n'] - r['ok_living']} "
              f"| {r['ok_balcony']}/{r['price_n'] - r['ok_balcony']} | {nprob or '-'} |")

    print("\n--- details ---")
    for r in rows:
        f = r["floor"]
        det = []
        if r["coord_iou"] is not None:
            det.append(f"min IoU of new loops vs old poly = {r['coord_iou']:.2f}")
        det += [f"area FAIL {x}" for x in r["bad_living"]]
        det += [f"balconies FAIL {x}" for x in r["bad_balcony"]]
        det += all_problems.get(f, [])
        det += ["(resolved) " + x for x in all_notes.get(f, [])]
        if det:
            print(f"\nfloor {f}:")
            for d in det:
                print("   ", d)
    for f, p in all_problems.items():
        if p and not any(r["floor"] == f for r in rows):
            print(f"\nfloor {f}: (not processed)")
            for d in p:
                print("   ", d)


if __name__ == "__main__":
    main()
