#!/usr/bin/env python3
"""Поджим наружных стен к пунктирным линиям (синий #0000FF / красный #FF0000) — расчёт операций для Figma.

.venv/bin/python tools/plan-wallthin.py <unit ...>  → JSON операций на stdout (для скрипта use_figma) + отчёт в stderr.

Правило (заказчик, 2026-09-08): внутренняя грань стены остаётся, внешняя поджимается к пунктиру.
Трогаем стену только если по одну сторону от неё квартира (комната или балкон), а по другую — нет.
Стена, у которой с обеих сторон квартира (комната/балкон, межкомнатная), не трогается. Колонны (белые заливки) не трогаем.
Работает по plan-studio/v3/figma/pdf-N.svg (px кадра) и контурам квартиры из plan-studio/data/floor-F.json.
"""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
K = 7.05
WALL_FILLS = {"#6A6A6A", "#FD8379"}
GUIDES = {"#0000FF": "blue", "#FF0000": "red"}


def inside(pt, poly):
    x, y = pt; n = len(poly); ins = False
    for i in range(n):
        x1, y1 = poly[i]; x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            ins = not ins
    return ins


def main():
    units = sys.argv[1:]
    out = {}
    for u in units:
        d = json.loads((ROOT / f"plan-studio/v3/plans/unit-{u}.json").read_text())
        bb = d["bbox"]
        fd = json.loads((ROOT / f"plan-studio/data/floor-{d['floor']}.json").read_text())
        fu = fd["units"][u]
        px = lambda poly: [((x - bb["x0"]) * K, (y - bb["y0"]) * K) for x, y in poly]
        polys = [px(fu["poly"])] + [px(b) for b in (fu.get("balcony_polys") or ([fu["balcony"]] if fu.get("balcony") else []))]
        in_unit = lambda pt: any(inside(pt, p) for p in polys)
        in_balcony = lambda pt: any(inside(pt, p) for p in polys[1:])

        svg = (ROOT / f"plan-studio/v3/figma/pdf-{u}.svg").read_text()
        els = re.findall(r'<path id="pdf (\d+)" d="([^"]*)" ([^/]*)/>', svg)
        segs = []   # guide segments
        fills = []  # wall fills (axis-aligned rects)
        for seq, dd, a in els:
            st = re.search(r'stroke="(#\w+)"', a); fl = re.search(r'fill="(#\w+)"', a)
            nums = [float(v) for v in re.findall(r'-?\d+\.?\d*', dd)]
            xs, ys = nums[0::2], nums[1::2]
            if st and st.group(1).upper() in GUIDES and len(xs) == 2:
                segs.append((GUIDES[st.group(1).upper()], xs, ys))
            if fl and fl.group(1).upper() in WALL_FILLS and 4 <= len(xs) <= 5:
                x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
                # прямоугольник по осям: все точки на границах bbox
                if all(abs(x - x0) < 0.05 or abs(x - x1) < 0.05 for x in xs) and all(abs(y - y0) < 0.05 or abs(y - y1) < 0.05 for y in ys):
                    fills.append(dict(seq=seq, x0=x0, x1=x1, y0=y0, y1=y1, fill=fl.group(1).upper()))
        # guide lines: cluster collinear axis-aligned segments
        lines = []
        for col, xs, ys in segs:
            if abs(ys[0] - ys[1]) < 0.3:
                key = ("y", round(ys[0], 1)); ext = (min(xs), max(xs))
            elif abs(xs[0] - xs[1]) < 0.3:
                key = ("x", round(xs[0], 1)); ext = (min(ys), max(ys))
            else:
                continue
            for L in lines:
                if L["key"][0] == key[0] and abs(L["key"][1] - key[1]) < 0.6 and L["col"] == col and ext[0] <= L["ext"][1] + 30 and ext[1] >= L["ext"][0] - 30:
                    L["ext"] = (min(L["ext"][0], ext[0]), max(L["ext"][1], ext[1])); break
            else:
                lines.append(dict(key=key, col=col, ext=ext))
        ops_fills, bands, report = [], [], []
        done = set()
        for L in lines:
            axis, c = L["key"]
            if L["col"] == "red" and L["ext"][1] - L["ext"][0] < 150:
                report.append(f"{u}: ignore short red guide {axis}={c} ({L['ext'][1]-L['ext'][0]:.0f} px)"); continue
            red_cont = L["col"] == "red" and any(B["col"] == "blue" and B["key"][0] == axis and abs(B["key"][1] - c) < 0.6 for B in lines)
            for f in fills:
                if f["seq"] in done: continue
                if L["col"] == "red" and not red_cont and f["fill"] != "#FD8379":
                    if (axis == "y" and f["y0"] + 0.5 < c < f["y1"] - 0.5 and not (f["x1"] < L["ext"][0] - 1 or f["x0"] > L["ext"][1] + 1)) or (axis == "x" and f["x0"] + 0.5 < c < f["x1"] - 0.5 and not (f["y1"] < L["ext"][0] - 1 or f["y0"] > L["ext"][1] + 1)):
                        report.append(f"{u}: CANDIDATE (red guide on grey wall, not applied) fill {f['seq']} {axis}={c} thickness {(f['y1']-f['y0']) if axis=='y' else (f['x1']-f['x0']):.0f} px")
                    continue
                if axis == "y":
                    if not (f["y0"] + 0.5 < c < f["y1"] - 0.5): continue
                    if f["x1"] < L["ext"][0] - 1 or f["x0"] > L["ext"][1] + 1: continue
                    samples = [f["x0"] + 1 + i * 8 for i in range(int((f["x1"] - f["x0"] - 2) // 8) + 1)] or [(f["x0"] + f["x1"]) / 2]
                    side_lo, side_hi = any(in_unit((sx, f["y0"] - 2)) for sx in samples), any(in_unit((sx, f["y1"] + 2)) for sx in samples)
                    bal = any(in_balcony((sx, f["y0"] - 2)) or in_balcony((sx, f["y1"] + 2)) for sx in samples)
                else:
                    if not (f["x0"] + 0.5 < c < f["x1"] - 0.5): continue
                    if f["y1"] < L["ext"][0] - 1 or f["y0"] > L["ext"][1] + 1: continue
                    samples = [f["y0"] + 1 + i * 8 for i in range(int((f["y1"] - f["y0"] - 2) // 8) + 1)] or [(f["y0"] + f["y1"]) / 2]
                    side_lo, side_hi = any(in_unit((f["x0"] - 2, sy)) for sy in samples), any(in_unit((f["x1"] + 2, sy)) for sy in samples)
                    bal = any(in_balcony((f["x0"] - 2, sy)) or in_balcony((f["x1"] + 2, sy)) for sy in samples)
                if side_lo == side_hi:
                    report.append(f"{u}: skip fill {f['seq']} on {axis}={c} ({'both sides in unit — interior' if side_lo else 'neither side in unit'})")
                    continue
                # балконная стена (с любой стороны балкон) — межкомнатная, не трогаем
                if bal:
                    report.append(f"{u}: skip fill {f['seq']} on {axis}={c} (balcony wall)"); continue
                # стена должна идти вдоль направляющей
                along_len = (f["x1"] - f["x0"]) if axis == "y" else (f["y1"] - f["y0"])
                across = (f["y1"] - f["y0"]) if axis == "y" else (f["x1"] - f["x0"])
                if along_len < 1.5 * across:
                    report.append(f"{u}: skip fill {f['seq']} on {axis}={c} (not along guide: {along_len:.0f}×{across:.0f})"); continue
                cut = min(c - (f["y0"] if axis == "y" else f["x0"]), (f["y1"] if axis == "y" else f["x1"]) - c)
                if cut < 2:
                    report.append(f"{u}: skip fill {f['seq']} on {axis}={c} (guide at the edge)"); continue
                done.add(f["seq"])
                inner_is_lo = side_lo  # квартира со стороны lo → внутренняя грань = lo-грань, внешняя (hi) поджимается к c
                if axis == "y":
                    lo, hi = f["y0"], f["y1"]
                    band = dict(axis="y", lo=(c if inner_is_lo else lo), hi=(hi if inner_is_lo else c), blue=c, along=[[f["x0"], f["x1"]]])
                    newy = lo if inner_is_lo else c; newh = (c - lo) if inner_is_lo else (hi - c)
                    ops_fills.append([f["seq"], None, round(newy, 2), None, round(newh, 2)])
                else:
                    lo, hi = f["x0"], f["x1"]
                    band = dict(axis="x", lo=(c if inner_is_lo else lo), hi=(hi if inner_is_lo else c), blue=c, along=[[f["y0"], f["y1"]]])
                    newx = lo if inner_is_lo else c; neww = (c - lo) if inner_is_lo else (hi - c)
                    ops_fills.append([f["seq"], round(newx, 2), None, round(neww, 2), None])
                bands.append(band)
                report.append(f"{u}: fill {f['seq']} {f['fill']} {axis}={c} {L['col']} → inner {'lo' if inner_is_lo else 'hi'} face, thickness {hi-lo:.1f} → {(c-lo) if inner_is_lo else (hi-c):.1f} px")
        for L in lines:
            report.append(f"{u}: guide {L['col']} {L['key'][0]}={L['key'][1]} ext {L['ext'][0]:.0f}..{L['ext'][1]:.0f}")
        out[u] = dict(fills=ops_fills, bands=bands)
        print("\n".join(report), file=sys.stderr)
    print(json.dumps(out, separators=(",", ":")))


if __name__ == "__main__":
    main()
