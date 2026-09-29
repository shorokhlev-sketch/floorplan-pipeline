#!/usr/bin/env python3
"""Наезд «Заголовка» (тип + площадь + уголок) на стены/мебель в картинках квартир.

Геометрия заголовков — plan-studio/final-svg/headers-2026-09-22.json (средние ряды Figma),
картинки — site/assets/plans/unit-<класс>.png (клоны «N · export», 2x).
Маска чернил = всё, что не белое и не пол #F3EFE8. Глифы заголовка рендерим Instrument Serif
по координатам Figma, подгоняем сдвигом ±6 px по корреляции с чернилами и вычитаем
(с дилатацией). Остаток внутри бокса заголовка = наезд; в поле 8 px (1x) вокруг — «впритык».

  tools/unit-header-overlap.py [--crops DIR]
"""
import argparse, json
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
PLANS = ROOT / "site/assets/plans"
HEAD = ROOT / "plan-studio/final-svg/headers-2026-09-22.json"
MAN = ROOT / "plan-studio/final-svg/manifest-export-2026-09-22.json"
FONT = next((f for f in fpconfig.fonts(Path.home() / "Library/Fonts/InstrumentSerif-Regular.ttf", "/Library/Fonts/InstrumentSerif-Regular.ttf") if f.exists()), None)  # Instrument Serif (OFL); set "font" in the config
FLOOR = np.array([243, 239, 232]); TOL = 28
MARGIN = 8.0          # px 1x — «впритык»
ALSO = {"508", "308", "1510", "1709", "2204", "311"}
MIN_HIT = 12          # px (2x) — меньше считаем шумом антиалиаса


def ink_mask(im):
    a = np.asarray(im.convert("RGBA")).astype(int)
    rgb, al = a[..., :3], a[..., 3]
    white = (np.abs(rgb - 255).max(-1) <= TOL)
    floor = (np.abs(rgb - FLOOR).max(-1) <= TOL)
    return (al > 128) & ~white & ~floor


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--crops", default=None); a = ap.parse_args()
    heads = {k: v for k, v in json.loads(HEAD.read_text()).items() if not k.startswith("_")}
    man = json.loads(MAN.read_text())
    rows = []
    for cls, items in heads.items():
        m = man[cls]; png = Image.open(PLANS / f"unit-{cls}.png"); s = png.width / m["w"]
        ink = ink_mask(png); H, W = ink.shape
        glyph = Image.new("L", (W, H), 0); d = ImageDraw.Draw(glyph)
        xs, ys, xe, ye = [], [], [], []
        corner = None
        for t, fs, x, y, w, h in items:
            xs.append(x); ys.append(y); xe.append(x + w); ye.append(y + h)
            if t == "|":
                corner = (x, y, w, h); continue
            f = ImageFont.truetype(str(FONT), int(round(fs * s)))
            asc, desc = f.getmetrics()
            lead = (h * s - (asc + desc)) / 2
            d.text((x * s, y * s + lead + asc), t, font=f, fill=255, anchor="ls")
        g = np.asarray(glyph) > 100
        bx0, by0, bx1, by1 = [int(v) for v in (min(xs) * s, min(ys) * s, max(xe) * s + 1, max(ye) * s + 1)]
        # подгонка сдвига глифов к чернилам (±6 px)
        best, bdx, bdy = -1, 0, 0
        sub = ink[max(0, by0 - 8):by1 + 8, max(0, bx0 - 8):bx1 + 8]
        for dy in range(-6, 7):
            for dx in range(-6, 7):
                gg = np.roll(np.roll(g, dy, 0), dx, 1)[max(0, by0 - 8):by1 + 8, max(0, bx0 - 8):bx1 + 8]
                sc = int((gg & sub).sum())
                if sc > best: best, bdx, bdy = sc, dx, dy
        g = np.roll(np.roll(g, bdy, 0), bdx, 1)
        gd = np.asarray(Image.fromarray((g * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(9))) > 0
        # связные фрагменты чернил вокруг бокса: фрагмент = буква, если он целиком в боксе
        # (с запасом 6 px) и хоть на 15 % лежит на отрисованных глифах; иначе препятствие
        from scipy import ndimage
        mg0 = int(MARGIN * s) + 40
        y0c, x0c = max(0, by0 - mg0), max(0, bx0 - mg0)
        win = ink[y0c:by1 + mg0, x0c:bx1 + mg0]
        lbl, n = ndimage.label(win, structure=np.ones((3, 3)))
        excl = np.zeros_like(ink)
        gwin = gd[y0c:by1 + mg0, x0c:bx1 + mg0]
        for sl_i, sl in enumerate(ndimage.find_objects(lbl), start=1):
            if sl is None: continue
            comp = lbl[sl] == sl_i
            yy0, yy1 = sl[0].start + y0c, sl[0].stop + y0c
            xx0, xx1 = sl[1].start + x0c, sl[1].stop + x0c
            inside_box = yy0 >= by0 - 6 and yy1 <= by1 + 6 and xx0 >= bx0 - 6 and xx1 <= bx1 + 6
            on_glyph = (comp & gwin[sl]).sum() / max(1, comp.sum())
            if inside_box and on_glyph >= 0.15:
                excl[yy0:yy1, xx0:xx1] |= comp
        if corner:
            cx, cy, cw, ch = corner
            excl[int(cy * s) - 4:int((cy + ch) * s) + 4, int(cx * s) - 4:int((cx + cw) * s) + 4] = True
        obst = ink & ~excl
        inside = obst[by0:by1, bx0:bx1]
        mg = int(MARGIN * s)
        ring = obst[max(0, by0 - mg):by1 + mg, max(0, bx0 - mg):bx1 + mg]
        n_in, n_ring = int(inside.sum()), int(ring.sum()) - int(inside.sum())
        glyph_fit = best / max(1, int(g.sum()))
        verdict = "НАЕЗД" if n_in >= MIN_HIT else ("впритык" if n_ring >= MIN_HIT else "ok")
        rows.append((cls, verdict, n_in, n_ring, round(glyph_fit, 2)))
        if a.crops and (verdict != "ok" or cls in ALSO):
            out = Path(a.crops); out.mkdir(parents=True, exist_ok=True)
            pad = 60
            crop = png.convert("RGB").crop((max(0, bx0 - pad), max(0, by0 - pad), min(W, bx1 + pad), min(H, by1 + pad)))
            ov = crop.copy(); od = ImageDraw.Draw(ov)
            ox, oy = max(0, bx0 - pad), max(0, by0 - pad)
            ys_, xs_ = np.nonzero(obst[oy:oy + crop.height, ox:ox + crop.width] & np.pad(np.ones((by1 - by0 + 2 * mg, bx1 - bx0 + 2 * mg), bool), 0)[:0].any() if False else obst[oy:oy + crop.height, ox:ox + crop.width])
            for yy, xx in zip(ys_, xs_):
                if bx0 - mg <= xx + ox < bx1 + mg and by0 - mg <= yy + oy < by1 + mg:
                    od.point((xx, yy), fill=(230, 30, 30))
            od.rectangle([bx0 - ox, by0 - oy, bx1 - ox, by1 - oy], outline=(0, 160, 255), width=2)
            ov.save(out / f"{verdict}-{cls}.png")
    for r in sorted(rows, key=lambda r: ({"НАЕЗД": 0, "впритык": 1, "ok": 2}[r[1]], -r[2])):
        print(f"{r[0]:>5}  {r[1]:8} внутри={r[2]:5} поле={r[3]:5} совпадение_глифов={r[4]}")
    from collections import Counter
    print(Counter(r[1] for r in rows))


if __name__ == "__main__":
    main()
