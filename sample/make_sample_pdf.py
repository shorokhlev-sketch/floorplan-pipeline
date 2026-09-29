#!/usr/bin/env python3
"""Synthetic architect's PDF with OCG layers, for running the pipeline end to end without client data.

    uv run sample/make_sample_pdf.py [--out sample/work]

Writes:
    <out>/sample.pdf                         2 pages = floors 2 and 3, 1:200 sheet (1 pt = 7.05 cm)
    <out>/site-assets/data/units.json        area schedule (number, floor, type, living, balcony, total)

Layers (OCG), named like a CAD export would name them:
    A-WALL   grey wall fills #6A6A6A, orange door leaves and arcs #FF6600 (arcs = many segments under 1 pt)
    A-COLS   columns: white fill + red hatch cross #A80F02, and one column drawn only as 4 axis lines
    A-GLAZ   windows: white bricks + blue mullions #97CBFF
    A-CORE   stairs (treads) and a lift with a cross
    A-AREA   measured outlines as single segments: units grey #999999, balconies black; yellow hatch;
             area labels as text
    A-FURN   furniture (must be dropped by the layer filter)
    A-DIMS   dimension lines in red and their text (must be dropped)

The geometry is invented. Areas in the schedule are computed from the outlines and rounded to 0.1 m2,
the same way an official area schedule rounds.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pymupdf

M_PER_PT = 0.0705
GREY = (0x6A / 255, 0x6A / 255, 0x6A / 255)
ORANGE = (1.0, 0x66 / 255, 0.0)
WHITE = (1.0, 1.0, 1.0)
BLUE = (0x97 / 255, 0xCB / 255, 1.0)
HATCH_RED = (0xA8 / 255, 0x0F / 255, 0x02 / 255)
AREA_GREY = (153 / 255, 153 / 255, 153 / 255)
BLACK = (0.0, 0.0, 0.0)
YELLOW = (219 / 255, 184 / 255, 0.0)
DIM_RED = (223 / 255, 0.0, 0.0)
FURN = (0.55, 0.55, 0.55)

# building: exterior walls centred on the footprint lines
X0, Y0, X1, Y1 = 60.0, 60.0, 440.0, 260.0
T_EXT, T_PARTY = 3.0, 2.0
CY0, CY1 = 140.0, 156.0          # corridor (wall centre lines), off-centre so unit areas differ
CORE_X0, CORE_X1 = 200.0, 280.0  # core between the units, north (stairs) and south (lift)


def shoelace_m2(pts):
    s = 0.0
    for i in range(len(pts)):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % len(pts)]
        s += ax * by - bx * ay
    return abs(s) / 2.0 * M_PER_PT * M_PER_PT


def rect_pts(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


class Sheet:
    def __init__(self, doc, page, ocg):
        self.doc, self.page, self.ocg = doc, page, ocg

    def fill_rect(self, layer, r, fill, color=None, width=0):
        sh = self.page.new_shape()
        sh.draw_rect(pymupdf.Rect(*r))
        sh.finish(fill=fill, color=color, width=width, oc=self.ocg[layer])
        sh.commit()

    def line(self, layer, a, b, color, width=0.25, dashes=None):
        sh = self.page.new_shape()
        sh.draw_line(a, b)
        sh.finish(color=color, width=width, dashes=dashes, closePath=False, oc=self.ocg[layer])
        sh.commit()

    def outline(self, layer, pts, color, width=0.25):
        """closed outline as SINGLE segments, one PDF object per segment (like the architect's area layer)"""
        for i in range(len(pts)):
            self.line(layer, pts[i], pts[(i + 1) % len(pts)], color, width)

    def text(self, layer, xy, s, size=5):
        self.page.insert_text(xy, s, fontsize=size, oc=self.ocg[layer])


def wall_rects(windows_n, windows_s, doors_n, doors_s, party_n, party_s):
    """Wall fills with gaps for windows (exterior) and doors (corridor walls)."""
    h = T_EXT / 2
    rects = []

    def split(a, b, gaps):
        out, cur = [], a
        for g0, g1 in sorted(gaps):
            if g0 > cur:
                out.append((cur, g0))
            cur = g1
        if cur < b:
            out.append((cur, b))
        return out

    for a, b in split(X0 - h, X1 + h, windows_n):          # north facade
        rects.append((a, Y0 - h, b, Y0 + h))
    for a, b in split(X0 - h, X1 + h, windows_s):          # south facade
        rects.append((a, Y1 - h, b, Y1 + h))
    rects.append((X0 - h, Y0 + h, X0 + h, Y1 - h))         # west
    rects.append((X1 - h, Y0 + h, X1 + h, Y1 - h))         # east
    p = T_PARTY / 2
    for a, b in split(X0 + h, X1 - h, doors_n):            # corridor wall north
        rects.append((a, CY0 - p, b, CY0 + p))
    for a, b in split(X0 + h, X1 - h, doors_s):            # corridor wall south
        rects.append((a, CY1 - p, b, CY1 + p))
    for x in party_n:                                      # party walls between units / core, north half
        rects.append((x - p, Y0 + h, x + p, CY0 - p))
    for x in party_s:                                      # south half
        rects.append((x - p, CY1 + p, x + p, Y1 - h))
    return rects


def door(sheet, hinge, width, direction, swing):
    """leaf + quarter arc drawn as ~40 segments under 1 pt, like a CAD export does"""
    hx, hy = hinge
    dx, dy = direction
    sx, sy = swing
    tip = (hx + sx * width, hy + sy * width)
    sheet.line("A-WALL", (hx, hy), tip, ORANGE, 0.25)
    n = 40
    prev = tip
    for i in range(1, n + 1):
        t = (math.pi / 2) * i / n
        p = (hx + sx * width * math.cos(t) + dx * width * math.sin(t),
             hy + sy * width * math.cos(t) + dy * width * math.sin(t))
        sheet.line("A-WALL", prev, p, ORANGE, 0.18)
        prev = p


def window(sheet, x0, x1, y):
    """white brick across the wall + blue mullions at the ends and in the middle"""
    h = T_EXT / 2
    sheet.fill_rect("A-GLAZ", (x0, y - h, x1, y + h), WHITE, BLACK, 0.2)
    for x in (x0, (x0 + x1) / 2 - 0.6, x1 - 1.2):
        sheet.fill_rect("A-GLAZ", (x, y - h, x + 1.2, y + h), BLUE)


def column(sheet, cx, cy, s=6.0, outline_only=False):
    r = (cx - s / 2, cy - s / 2, cx + s / 2, cy + s / 2)
    if outline_only:   # a column drawn as 4 axis lines, no fill: ink mode must turn it into a black fill
        pts = rect_pts(*r)
        for i in range(4):   # 4 separate single-segment objects, the way the CAD export writes them
            sheet.line("A-COLS", pts[i], pts[(i + 1) % 4], BLACK, 0.25)
        return
    sheet.fill_rect("A-COLS", r, WHITE)
    sheet.line("A-COLS", (r[0], r[1]), (r[2], r[3]), HATCH_RED, 0.2)
    sheet.line("A-COLS", (r[2], r[1]), (r[0], r[3]), HATCH_RED, 0.2)


def core(sheet):
    # stairs, north half of the core
    x0, x1 = CORE_X0 + 6, CORE_X1 - 6
    for i in range(10):
        y = Y0 + 10 + i * 6
        sheet.line("A-CORE", (x0, y), (x1, y), BLACK, 0.2)
    sheet.line("A-CORE", ((x0 + x1) / 2, Y0 + 10), ((x0 + x1) / 2, Y0 + 64), BLACK, 0.4)
    # lift, south half of the core
    r = (CORE_X0 + 18, CY1 + 14, CORE_X1 - 18, CY1 + 58)
    sh = sheet.page.new_shape()
    sh.draw_rect(pymupdf.Rect(*r))
    sh.finish(color=BLACK, width=0.3, oc=sheet.ocg["A-CORE"])
    sh.commit()
    sheet.line("A-CORE", (r[0], r[1]), (r[2], r[3]), BLACK, 0.2)
    sheet.line("A-CORE", (r[2], r[1]), (r[0], r[3]), BLACK, 0.2)


def furniture(sheet, units):
    for u in units:
        x0, y0, x1, y1 = u["box"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        sheet.fill_rect("A-FURN", (cx - 11, cy - 15, cx + 11, cy + 15), WHITE, FURN, 0.3)   # bed
        sh = sheet.page.new_shape()
        sh.draw_circle((x0 + 16, y0 + 16), 6)                                              # table
        sh.finish(color=FURN, width=0.3, oc=sheet.ocg["A-FURN"])
        sh.commit()


def dims(sheet):
    y = Y1 + 24
    sheet.line("A-DIMS", (X0, y), (X1, y), DIM_RED, 0.2)
    for x in (X0, CORE_X0, CORE_X1, X1):
        sheet.line("A-DIMS", (x, y - 3), (x, y + 3), DIM_RED, 0.2)
    sheet.text("A-DIMS", ((X0 + X1) / 2 - 10, y - 4), f"{(X1 - X0) * M_PER_PT:.2f}", 4)


def floor_units(floor):
    """unit boxes (wall centre lines) and balconies for one floor"""
    h, p = T_EXT / 2, T_PARTY / 2
    north = [(X0, Y0, CORE_X0, CY0), (CORE_X1, Y0, X1, CY0)]
    south = [(X0, CY1, CORE_X0, Y1), (CORE_X1, CY1, X1, Y1)]
    if floor >= 3:     # the east north unit is split into two studios on the upper floor
        mid = CORE_X1 + 70.0
        north = [north[0], (CORE_X1, Y0, mid, CY0), (mid, Y0, X1, CY0)]
    boxes = north + south
    units = []
    for i, (x0, y0, x1, y1) in enumerate(boxes, start=1):
        ix0 = x0 + (h if x0 == X0 else p)
        ix1 = x1 - (h if x1 == X1 else p)
        iy0 = y0 + (h if y0 == Y0 else p)
        iy1 = y1 - (h if y1 == Y1 else p)
        poly = rect_pts(round(ix0, 2), round(iy0, 2), round(ix1, 2), round(iy1, 2))
        bal = []
        w = ix1 - ix0
        if y0 == Y0 and (w > 100 or floor >= 3 and i == 3):          # north balcony
            bal.append(rect_pts(round(ix0 + 12, 2), 43.0, round(ix1 - 12, 2), Y0 - h + 1.0))
        if y1 == Y1:                                                   # south balconies, big units get two pieces
            if w > 100:
                bal.append(rect_pts(round(ix0 + 8, 2), Y1 + h - 1.0, round(ix0 + w / 2 - 4, 2), 277.0))
                bal.append(rect_pts(round(ix0 + w / 2 + 4, 2), Y1 + h - 1.0, round(ix1 - 8, 2), 277.0))
            else:
                bal.append(rect_pts(round(ix0 + 8, 2), Y1 + h - 1.0, round(ix1 - 8, 2), 277.0))
        units.append({"number": f"{floor}{i:02d}", "box": (x0, y0, x1, y1), "poly": poly, "balconies": bal})
    return units


def draw_floor(doc, ocg, floor):
    page = doc.new_page(width=500, height=320)
    sheet = Sheet(doc, page, ocg)
    units = floor_units(floor)
    edges = lambda us: {u["box"][0] for u in us if u["box"][0] > X0} | {u["box"][2] for u in us if u["box"][2] < X1}
    party_n = sorted(edges([u for u in units if u["box"][1] == Y0]))
    party_s = sorted(edges([u for u in units if u["box"][3] == Y1]))

    # windows: one per unit on each facade
    win_n, win_s = [], []
    for u in units:
        x0, y0, x1, y1 = u["box"]
        a, b = x0 + (x1 - x0) * 0.3, x0 + (x1 - x0) * 0.7
        (win_n if y0 == Y0 else win_s).append((a, b))
    # doors: one per unit in the corridor wall, 9 pt wide (63 cm)
    door_n, door_s = [], []
    for u in units:
        x0, y0, x1, y1 = u["box"]
        dx = x1 - 20
        (door_n if y1 == CY0 else door_s).append((dx - 9, dx))
    for r in wall_rects(win_n, win_s, door_n, door_s, party_n, party_s):
        sheet.fill_rect("A-WALL", r, GREY)
    for a, b in win_n:
        window(sheet, a, b, Y0)
    for a, b in win_s:
        window(sheet, a, b, Y1)
    for u in units:
        x0, y0, x1, y1 = u["box"]
        dx = x1 - 20
        if y1 == CY0:
            door(sheet, (dx, CY0 - T_PARTY / 2), 9, (-1, 0), (0, -1))
        else:
            door(sheet, (dx, CY1 + T_PARTY / 2), 9, (-1, 0), (0, 1))
    for x in (CORE_X0, CORE_X1):
        column(sheet, x, CY0 - 8)
    column(sheet, CORE_X0, CY1 + 8, outline_only=True)
    column(sheet, CORE_X1, CY1 + 8)
    core(sheet)
    furniture(sheet, units)
    dims(sheet)

    # measured outlines + hatch + labels
    for u in units:
        sheet.outline("A-AREA", u["poly"], AREA_GREY)
        for b in u["balconies"]:
            sheet.outline("A-AREA", b, BLACK)
        xs = [p[0] for p in u["poly"]]
        ys = [p[1] for p in u["poly"]]
        cx, cy = sum(xs) / 4, sum(ys) / 4
        sheet.text("A-AREA", (cx - 6, cy + 10), f"{shoelace_m2(u['poly']):.1f}", 5)
        sh = page.new_shape()                    # yellow hatch: multi-segment path, ignored by the extractor
        for k in range(3):
            sh.draw_line((min(xs) + 4 + k * 6, min(ys) + 4), (min(xs) + 10 + k * 6, min(ys) + 10))
        sh.finish(color=YELLOW, width=0.2, closePath=False, oc=ocg["A-AREA"])
        sh.commit()
    return units


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "work"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open()
    ocg = {name: doc.add_ocg(name, on=True)
           for name in ("A-WALL", "A-COLS", "A-GLAZ", "A-CORE", "A-AREA", "A-FURN", "A-DIMS")}
    schedule = []
    for floor in (2, 3):
        for u in draw_floor(doc, ocg, floor):
            living = round(shoelace_m2(u["poly"]), 1)
            balcony = round(sum(shoelace_m2(b) for b in u["balconies"]), 1)
            kind = "studio" if living < 40 else "1br" if living < 55 else "2br"
            schedule.append({"number": u["number"], "floor": floor, "type": kind,
                             "living": living, "balcony": balcony, "total": round(living + balcony, 1)})
    pdf = out / "sample.pdf"
    doc.save(pdf, garbage=3, deflate=True)
    sched = out / "site-assets" / "data" / "units.json"
    sched.parent.mkdir(parents=True, exist_ok=True)
    sched.write_text(json.dumps({"units": schedule}, indent=1), encoding="utf-8")
    total = sum(u["total"] for u in schedule)
    print(f"{pdf}: {len(doc)} pages, {len(schedule)} units, total {total:.1f} m2; schedule -> {sched}")


if __name__ == "__main__":
    main()
