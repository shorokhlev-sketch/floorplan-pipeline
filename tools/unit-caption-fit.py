#!/usr/bin/env python3
"""Проверка и подбор позиции ярлыка «Тип N / площадь» внутри плана квартиры.

Ярлык на сайте (js/app.js planImg + css .plan-cap) — две строки Instrument Serif/Georgia:
«Studio 4» кеглем fs·W (доля ширины картинки) и «34.6 m²» кеглем 0.88·fs·W, межстрочный 1.15,
слева вертикальная черта 0.06em + отступ 0.28em. Позиция tx/ty = левый верхний угол ярлыка
в долях ширины/высоты картинки (data/captions.js).

Скрипт строит по PNG плана маску препятствий (всё, что не белое и не «пол» #F3EFE8),
дилатирует её на 6 px, проверяет, не лежит ли ярлык на стенах/мебели, и при наезде ищет
ближайшее свободное место (2D-префиксные суммы), при необходимости уменьшая кегль.

  tools/unit-caption-fit.py --all [--dry]
  tools/unit-caption-fit.py 201 2208
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageFont
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
SITE = ROOT / "site"
PLANS_DIR = SITE / "assets/plans"
CAPTIONS = SITE / "data/captions.js"
PLANS_JS = SITE / "data/plans.js"
UNITS_JSON = fpconfig.SCHEDULE

FLOOR = (243, 239, 232)      # #F3EFE8
FLOOR_TOL = 12
WHITE_TOL = 12
DILATE = 6                   # px в масштабе 2x
PAD = 10                     # поле вокруг ярлыка, px
OVERLAP_LIMIT = 0.005        # >0.5 % пикселей препятствий внутри box = наезд
BAR_EM = 0.06                # border-left
GAP_EM = 0.28                # padding-left
LINE = 1.15
SUB = 0.88

TYPE_EN = {"studio": "Studio", "1br": "1BR", "2br": "2BR", "3br": "3BR"}
TYPE_RU = {"studio": "Студия", "1br": "1-комн.", "2br": "2-комн.", "3br": "3-комн."}

FONT_CANDIDATES = [
    Path.home() / "Library/Fonts/InstrumentSerif-Regular.ttf",
    Path.home() / "Library/Fonts/Instrument Serif Regular.ttf",
    Path("/Library/Fonts/InstrumentSerif-Regular.ttf"),
    # на select.html Instrument Serif не подключён и кириллицы в нём нет → реальный шрифт Georgia
    Path("/System/Library/Fonts/Supplemental/Georgia.ttf"),
    Path("/System/Library/Fonts/Supplemental/Times New Roman.ttf"),
]


def font_path():
    for p in FONT_CANDIDATES:
        if p.exists():
            return p
    return None


FONT = font_path()
_font_cache = {}


def text_width(text, size):
    """Ширина строки в px при данном кегле."""
    size = max(4, int(round(size)))
    if FONT is None:
        return 0.44 * size * len(text)
    key = (FONT, size)
    f = _font_cache.get(key)
    if f is None:
        f = _font_cache[key] = ImageFont.truetype(str(FONT), size)
    return f.getlength(text)


# ---------------------------------------------------------------- данные сайта
def load_plans():
    src = PLANS_JS.read_text()
    m = re.search(r"window\.PLANS_DATA\s*=\s*(\{.*\})\s*;?\s*$", src, re.S)
    return json.loads(m.group(1))["units"]


def load_units():
    return {u["number"]: u for u in json.loads(UNITS_JSON.read_text())["units"]}


def load_captions():
    src = CAPTIONS.read_text()
    m = re.search(r"window\.PLAN_CAPTIONS\s*=\s*(\{.*\})\s*;?\s*$", src, re.S)
    return json.loads(m.group(1))


def save_captions(caps):
    CAPTIONS.write_text("window.PLAN_CAPTIONS = " + json.dumps(caps, separators=(",", ":"), ensure_ascii=False) + ";\n")


def base_texts(base, plans, units):
    """Самая широкая пара строк среди квартир базы (учитываем ru и en — сайт по умолчанию ru)."""
    best = None
    for num, p in plans.items():
        if p.get("base") != base:
            continue
        u = units.get(num)
        if not u:
            continue
        for table in (TYPE_EN, TYPE_RU):
            t1 = f"{table.get(u['type'], u['type'])} {u['slot']}"
            t2 = f"{u['total']:.1f}".replace(".", "," if table is TYPE_RU else ".") + (" м²" if table is TYPE_RU else " m²")
            w = max(text_width(t1, 100), text_width(t2, 88))
            if best is None or w > best[0]:
                best = (w, t1, t2)
    return (best[1], best[2]) if best else None


# ------------------------------------------------------------------- геометрия
def box_size(t1, t2, font1):
    """Размер ярлыка в px (без поля PAD)."""
    w_text = max(text_width(t1, font1), text_width(t2, font1 * SUB))
    w = (BAR_EM + GAP_EM) * font1 + w_text
    h = LINE * font1 * (1 + SUB)
    return w, h


def obstacle_mask(path):
    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(np.int16)
    white = (a > 255 - WHITE_TOL).all(axis=2)
    floor = (np.abs(a - np.array(FLOOR, dtype=np.int16)) <= FLOOR_TOL).all(axis=2)
    mask = ~(white | floor)
    if DILATE:
        m = Image.fromarray((mask * 255).astype(np.uint8), "L")
        m = m.filter(ImageFilter.MaxFilter(2 * DILATE + 1))
        mask = np.asarray(m) > 0
    return mask, floor, im.size


def integral(mask):
    return np.pad(np.cumsum(np.cumsum(mask.astype(np.int64), axis=0), axis=1), ((1, 0), (1, 0)))


def rect_sum(S, y, x, h, w):
    return int(S[y + h, x + w] - S[y, x + w] - S[y + h, x] + S[y, x])


def find_free(S, F, H, W, bw, bh, ax, ay, lim):
    """Ближайшая к якорю (ax, ay) позиция box (с полем PAD) без препятствий.

    S — интеграл маски препятствий, F — интеграл маски «пол»; lim — минимальная доля пола
    под ярлыком (ярлык должен лежать внутри плана, а не в белом поле вокруг него).
    """
    Wb = int(math.ceil(bw)) + 2 * PAD
    Hb = int(math.ceil(bh)) + 2 * PAD
    if Wb > W or Hb > H:
        return None

    def wins(I):
        return I[Hb:, Wb:] - I[:-Hb, Wb:] - I[Hb:, :-Wb] + I[:-Hb, :-Wb]

    free = wins(S) == 0
    if not free.any():
        return None
    floor_frac = wins(F) / float(Wb * Hb)
    ok = free & (floor_frac >= lim)
    if not ok.any():
        return None
    qs, ps = np.nonzero(ok)
    i = int(np.argmin((ps + Wb / 2.0 - ax) ** 2 + (qs + Hb / 2.0 - ay) ** 2))
    return int(ps[i]) + PAD, int(qs[i]) + PAD, float(floor_frac[qs[i], ps[i]])


def overlap_ratio(S, W, H, x, y, bw, bh):
    """Доля препятствий в box с полем PAD; None если box выходит за картинку."""
    bx0, by0 = int(round(x)) - PAD, int(round(y)) - PAD
    bx1, by1 = int(round(x + bw)) + PAD, int(round(y + bh)) + PAD
    out = bx0 < 0 or by0 < 0 or bx1 > W or by1 > H
    cx0, cy0 = max(0, bx0), max(0, by0)
    cx1, cy1 = min(W, bx1), min(H, by1)
    if cx1 <= cx0 or cy1 <= cy0:
        return 1.0, True
    s = rect_sum(S, cy0, cx0, cy1 - cy0, cx1 - cx0)
    return s / float((cx1 - cx0) * (cy1 - cy0)), out


# ----------------------------------------------------------------------- логика
def process(base, caps, plans, units, dry):
    png = PLANS_DIR / f"unit-{base}.png"
    if not png.exists():
        return {"base": base, "status": "нет PNG"}
    cap = caps.get(base)
    if not cap:
        return {"base": base, "status": "нет записи в captions.js"}
    texts = base_texts(base, plans, units)
    if not texts:
        return {"base": base, "status": "нет квартир"}
    t1, t2 = texts
    mask, floor, (W, H) = obstacle_mask(png)
    S, F = integral(mask), integral(floor)
    font1 = cap["fs"] * W
    bw, bh = box_size(t1, t2, font1)
    x, y = cap["tx"] * W, cap["ty"] * H
    before, out = overlap_ratio(S, W, H, x, y, bw, bh)
    res = {"base": base, "t1": t1, "t2": t2, "before": before, "out": out,
           "old": (cap["tx"], cap["ty"], cap["fs"]), "size": (W, H)}
    if before <= OVERLAP_LIMIT and not out:
        res["status"] = "ок"
        res["after"] = before
        return res
    ax, ay = x + bw / 2.0, y + bh / 2.0
    # сначала пытаемся уложить ярлык целиком на пол квартиры (при нужде уменьшая кегль),
    # и только если это невозможно — допускаем место частично/полностью вне плана
    for lim in (0.9, 0.5, 0.0):
        for k in range(0, 5):                   # 100 %, 90 % … 60 %
            scale = 1.0 - 0.1 * k
            f = font1 * scale
            w2, h2 = box_size(t1, t2, f)
            pos = find_free(S, F, H, W, w2, h2, ax, ay, lim)
            if pos is None:
                continue
            nx, ny, ff = pos
            after, _ = overlap_ratio(S, W, H, nx, ny, w2, h2)
            res.update({"status": "перенесён" + ("" if scale == 1.0 else f", кегль {int(round(scale*100))} %"),
                        "new": (round(nx / W, 4), round(ny / H, 4), round(f / W, 4)), "after": after,
                        "floor": ff, "moved": math.hypot(nx - x, ny - y)})
            if not dry:
                caps[base] = {"tx": res["new"][0], "ty": res["new"][1], "fs": res["new"][2]}
            return res
    res["status"] = "МЕСТА НЕТ — оставлено как есть"
    res["after"] = before
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bases", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    caps = load_captions()
    plans, units = load_plans(), load_units()
    bases = sorted(caps, key=lambda b: int(b)) if (a.all or not a.bases) else a.bases
    rows = [process(b, caps, plans, units, a.dry) for b in bases]
    print(f"шрифт замера: {FONT}\n")
    print("| База | текст | наезд до | новое положение (tx,ty,fs) | наезд после | статус |")
    print("|---|---|---|---|---|---|")
    changed = 0
    for r in rows:
        if "before" not in r:
            print(f"| {r['base']} | — | — | — | — | {r['status']} |")
            continue
        new = r.get("new")
        if new:
            changed += 1
        print(f"| {r['base']} | {r.get('t1','')} / {r.get('t2','')} | {r['before']*100:.2f} %"
              f"{' + за краем' if r['out'] else ''} | "
              f"{('%.4f, %.4f, %.4f' % new) if new else '—'} | "
              f"{r['after']*100:.2f} % | {r['status']}"
              f"{'' if not new else (', на полу %d %%' % round(r['floor']*100))} |")
    if not a.dry and changed:
        save_captions(caps)
        print(f"\n{CAPTIONS}: обновлено баз — {changed}")
    elif a.dry:
        print("\n--dry: файл не изменён")
    else:
        print("\nизменений нет")
    return rows


if __name__ == "__main__":
    main()
