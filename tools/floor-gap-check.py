#!/usr/bin/env python3
"""Детектор щелей/разрывов/висячих кусков в стенах на растровом рендере этажа.

.venv/bin/python tools/floor-gap-check.py <png> [--out <dir>] [--json <path>]

Ink-маска = пиксели в пределах ±40 от #182E46 (цвет линий плана). Пол #F3EFE8, бумага белая,
1 px = 1 см, толщина стены 10-40 px.

wall_mask = ink-компоненты (8-connectivity) площадью >= WALL_MIN_AREA (реальные куски стен, БЕЗ
эрозии — углы остаются точными). Текст/дверные пунктиры/дверные пороги/оконные импосты/значки
мебели в этом рендере — либо мелкие ink-компоненты (< WALL_MIN_AREA), либо вообще светло-серые
(не ink) линии; оба случая исключаются на уровне масок, а не порогами по форме.

Классы находок:
  sliver  — тонкая светлая полоска ВНУТРИ стены (два источника стен сдвинуты на 1-10 px):
            binary_closing(wall_mask, disk r=8) minus wall_mask → компоненты дыр; берём вытянутые
            и узкие (см. SLIVER_*). Дополнительно отбрасываются находки рядом с дверной дугой-
            пунктиром (dash_centers/near_door_dashes) и впритык к светло-серой линии дверного
            порога/коробки (grey_mask) — это не дефект стены, а условное обозначение двери.
  gap     — разрыв: два свободных торца стен (free_ends), лежащих друг напротив друга на одной оси
            в 6-30 px, не образующие дверной проём (looks_like_door: белая коробка/дуга между ними).
  stub    — висящий короткий кусок стены: один торец держится на другой стене, второй висит в воздухе.
  island  — короткий кусок стены, оба торца свободны (ни с чем не соединён).

Известные спорные случаи (см. отчёт агента, а не только код): 1px горизонтальные sliver на всю
ширину большого перекрытия (потолок паркинга) — похоже на реальный шов заливка/обводка, но это
не стена; мебельные значки (шкаф/тумба с вертикальным "хвостом") иногда читаются как sliver/stub.

Вывод: JSON [{cls,x,y,w,h,area}, ...] на stdout (или --json <path>) + сводка в stderr.
--out <dir>: gaps-sheet.png (контакт-лист кропов 240x240, до 40 шт, по классам/площади) и
             gaps-overlay.png (весь план с красными рамками находок, уменьшенный до ширины 2000).
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK

INK = (0x18, 0x2E, 0x46)
INK_TOL = 40

# Стены отличаем от текста/дверных пунктиров/дуг/оконных импостов/штриховки/мебельных значков
# по площади ink-компоненты (8-connectivity), БЕЗ эрозии/opening (эрозия рвёт углы стен и сама
# создаёт ложные "щели" в closing-diff). На floor-2.png v4 распределение площадей компонент имеет
# чистый разрыв: подписи/пунктир/значки <= ~500 px, реальные куски стен >= ~750 px. WALL_MIN_AREA
# выбран с запасом внутри этого разрыва.
WALL_MIN_AREA = 600

# --- sliver (щель между двумя сдвинутыми источниками стен) ---
CLOSE_RADIUS = 8          # px, радиус диска для binary_closing
SLIVER_MAX_THIN = 8       # min(w,h) находки <= этого -> тонкая полоска
SLIVER_MIN_LONG = 20      # max(w,h) находки >= этого -> достаточно длинная
SLIVER_SMALL_AREA = 400   # или: площадь <= этого
SLIVER_ELONG = 3.0        # и вытянутость (bbox major/минор) >= этого
SLIVER_MIN_AREA = 15      # ниже этого - остаточный шум closing() на плоских углах, не видно глазом

# Порог/коробка двери в этом рендере рисуется СВЕТЛО-СЕРОЙ линией (не ink-цветом), вплотную к
# торцам стены на реву проёма - closing() ошибочно видит зазор под коробкой как sliver. Если
# щель впритык (1px) граничит с таким серым пикселем - это порог двери, а не дефект стены.
GREY_SPREAD_MAX = 25       # max(R,G,B)-min(R,G,B) для "серой" линии
GREY_MEAN_LO = 130
GREY_MEAN_HI = 235

# --- разрывы (торцы стен) ---
END_ALONG_CLEAR = 3       # px пусто вдоль полосы за торцом
END_CROSS_MAX = 40        # px макс. поперечная ширина полосы у торца (<= толщина стены)
GAP_MIN = 6                # px мин. расстояние между торцами-кандидатами
GAP_MAX = 30               # px макс. расстояние

# Дверная коробка/порог в этом рендере рисуется СВЕТЛО-СЕРОЙ линией (не ink-цветом) и лежит
# впритык к торцам стены на реву проёма — closing() ошибочно видит это как sliver (щель).
# Дверная дуга-пунктир, наоборот, рисуется ink-цветом, но мелкими (2-30 px) несвязанными
# штрихами. Признак "рядом дверь" = много таких мелких ink-штрихов в радиусе DOOR_DASH_RADIUS
# от находки (сама дуга обычно начинается в 40-100 px от петли двери).
DOOR_DASH_AREA_MIN = 2
DOOR_DASH_AREA_MAX = 30
DOOR_DASH_RADIUS = 70
DOOR_DASH_MIN_COUNT = 4

# --- висячие куски / острова ---
STUB_MAX_AREA = 2500       # px^2
STUB_MIN_DIM = 16          # min bbox dim <= этого
STUB_MAX_DIM_LO = 20       # max bbox dim в диапазоне [20,120]
STUB_MAX_DIM_HI = 120
STUB_FREE_RADIUS = 12      # px радиус проверки "пусто вокруг свободного торца"

MAX_SHEET_ITEMS = 40
CROP = 240


def ink_mask(arr):
    d = arr.astype(np.int16) - np.array(INK, dtype=np.int16)
    return (np.abs(d).sum(axis=-1) <= INK_TOL * 3) & (np.abs(d).max(axis=-1) <= INK_TOL)


def disk(r):
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= r * r


def wall_mask_by_area(full_mask, min_area=WALL_MIN_AREA):
    """Оставляет только ink-компоненты площадью >= min_area (реальные куски стен), без эрозии —
    так углы стен остаются точными и closing() не плодит ложные щели на плоских углах."""
    lbl, n = ndimage.label(full_mask, structure=np.ones((3, 3)))
    if n == 0:
        return np.zeros_like(full_mask), lbl
    sizes = ndimage.sum(full_mask, lbl, range(1, n + 1))
    keep = np.zeros(n + 1, dtype=bool)
    keep[1:] = sizes >= min_area
    wall = keep[lbl]
    return wall, lbl


def dash_centers(full_mask):
    """Центроиды мелких (DOOR_DASH_AREA_MIN..MAX px) несвязанных ink-компонент — штрихи дверных
    дуг (и часть шума типа засечек текста, но нам важна только их локальная плотность у находки)."""
    lbl, n = ndimage.label(full_mask, structure=np.ones((3, 3)))
    if n == 0:
        return np.zeros((0, 2))
    sizes = ndimage.sum(full_mask, lbl, range(1, n + 1))
    objs = ndimage.find_objects(lbl)
    pts = []
    for i, sl in enumerate(objs, start=1):
        if sl is None:
            continue
        a = sizes[i - 1]
        if DOOR_DASH_AREA_MIN <= a <= DOOR_DASH_AREA_MAX:
            ys, xs = sl
            pts.append(((ys.start + ys.stop) / 2.0, (xs.start + xs.stop) / 2.0))
    return np.array(pts) if pts else np.zeros((0, 2))


def near_door_dashes(cx, cy, dashes, radius=DOOR_DASH_RADIUS, min_count=DOOR_DASH_MIN_COUNT):
    """True, если рядом с (cx,cy) достаточно мелких ink-штрихов, чтобы это была дверная дуга
    (а не дефект стены): порог/коробка двери и пунктирная дуга живут в одном месте плана."""
    if len(dashes) == 0:
        return False
    d = np.hypot(dashes[:, 0] - cy, dashes[:, 1] - cx)
    return int((d <= radius).sum()) >= min_count


def grey_mask(arr):
    """Светло-серая линия (порог/коробка двери) - не ink, но и не чистый пол/бумага/белый."""
    a16 = arr.astype(np.int16)
    spread = a16.max(axis=-1) - a16.min(axis=-1)
    mean = a16.mean(axis=-1)
    return (spread <= GREY_SPREAD_MAX) & (mean >= GREY_MEAN_LO) & (mean <= GREY_MEAN_HI)


def find_slivers(mask, dashes=None, grey=None):
    closed = ndimage.binary_closing(mask, structure=disk(CLOSE_RADIUS))
    holes = closed & ~mask
    lbl, n = ndimage.label(holes, structure=np.ones((3, 3)))
    out = []
    if n == 0:
        return out
    objs = ndimage.find_objects(lbl)
    for i, sl in enumerate(objs, start=1):
        if sl is None:
            continue
        ys, xs = sl
        h = ys.stop - ys.start
        w = xs.stop - xs.start
        area = int((lbl[sl] == i).sum())
        if area < SLIVER_MIN_AREA:
            continue
        thin_long = min(w, h) <= SLIVER_MAX_THIN and max(w, h) >= SLIVER_MIN_LONG
        elong_small = False
        if area <= SLIVER_SMALL_AREA:
            ys_idx, xs_idx = np.nonzero(lbl[sl] == i)
            if len(xs_idx) >= 4:
                cov = np.cov(np.vstack([xs_idx, ys_idx]).astype(float))
                evals = np.linalg.eigvalsh(cov)
                evals = np.clip(evals, 1e-6, None)
                elong = (evals[1] / evals[0]) ** 0.5
                elong_small = elong >= SLIVER_ELONG
        if not (thin_long or elong_small):
            continue
        if dashes is not None and near_door_dashes(xs.start + w / 2.0, ys.start + h / 2.0, dashes):
            continue  # порог/коробка двери (дуга рядом), не щель
        if grey is not None:
            y0 = max(0, ys.start - 1); y1 = min(mask.shape[0], ys.stop + 1)
            x0 = max(0, xs.start - 1); x1 = min(mask.shape[1], xs.stop + 1)
            comp = (lbl[y0:y1, x0:x1] == i)
            ring = ndimage.binary_dilation(comp, structure=np.ones((3, 3))) & ~comp
            if ring.any() and grey[y0:y1, x0:x1][ring].any():
                continue  # щель впритык к серой линии порога двери - не дефект стены
        out.append(dict(cls="sliver", x=int(xs.start), y=int(ys.start), w=int(w), h=int(h), area=area))
    return out


def label_ink(mask):
    lbl, n = ndimage.label(mask, structure=np.ones((3, 3)))
    return lbl, n


def component_stats(lbl, n):
    objs = ndimage.find_objects(lbl)
    stats = {}
    for i, sl in enumerate(objs, start=1):
        if sl is None:
            continue
        ys, xs = sl
        area = int((lbl[sl] == i).sum())
        stats[i] = dict(sl=sl, y0=ys.start, y1=ys.stop, x0=xs.start, x1=xs.stop,
                         h=ys.stop - ys.start, w=xs.stop - xs.start, area=area)
    return stats


def free_ends(mask, lbl, comp_id, st, only_free=True):
    """Торцы полосы (>=1, <=2): скан по контуру bbox компоненты. Для каждого торца считаем его
    "free" (за ним пусто на END_ALONG_CLEAR px вдоль оси) или "attached" (там ещё ink -> торец
    держится на другой/этой же компоненте). only_free=True (по умолчанию) возвращает только
    свободные - для поиска gap; only_free=False возвращает оба статуса - для stub/island."""
    comp = (lbl == comp_id)
    sl = st["sl"]
    sub = comp[sl]
    h, w = sub.shape
    horiz = w >= h  # стена вытянута по x -> торцы слева/справа
    ends = []
    if horiz:
        col_has = sub.any(axis=0)
        cols = np.nonzero(col_has)[0]
        if len(cols) == 0:
            return ends
        left, right = cols.min(), cols.max()
        for edge, direction in ((left, -1), (right, 1)):
            rows = np.nonzero(sub[:, edge])[0]
            cross = rows.max() - rows.min() + 1 if len(rows) else 0
            if cross > END_CROSS_MAX or cross == 0:
                continue
            gx0 = st["x0"] + edge
            gy0 = st["y0"] + rows.min()
            gy1 = st["y0"] + rows.max() + 1
            probe_x0 = gx0 + direction * END_ALONG_CLEAR if direction > 0 else gx0 + direction * (END_ALONG_CLEAR + 1) + 1
            x_lo = gx0 + 1 if direction > 0 else gx0 - END_ALONG_CLEAR - 1
            x_hi = gx0 + END_ALONG_CLEAR + 1 if direction > 0 else gx0
            x_lo = max(0, x_lo); x_hi = min(mask.shape[1], x_hi)
            y_lo = max(0, gy0); y_hi = min(mask.shape[0], gy1)
            free = True
            if x_hi > x_lo and y_hi > y_lo:
                strip = mask[y_lo:y_hi, x_lo:x_hi]
                free = not strip.any()
            if only_free and not free:
                continue
            ends.append(dict(axis="x", dir=direction, x=int(gx0), y=int((gy0 + gy1) // 2),
                              cross0=int(gy0), cross1=int(gy1), comp=comp_id, free=free))
    else:
        row_has = sub.any(axis=1)
        rows = np.nonzero(row_has)[0]
        if len(rows) == 0:
            return ends
        top, bottom = rows.min(), rows.max()
        for edge, direction in ((top, -1), (bottom, 1)):
            cols = np.nonzero(sub[edge, :])[0]
            cross = cols.max() - cols.min() + 1 if len(cols) else 0
            if cross > END_CROSS_MAX or cross == 0:
                continue
            gy0 = st["y0"] + edge
            gx0 = st["x0"] + cols.min()
            gx1 = st["x0"] + cols.max() + 1
            y_lo = gy0 + 1 if direction > 0 else gy0 - END_ALONG_CLEAR - 1
            y_hi = gy0 + END_ALONG_CLEAR + 1 if direction > 0 else gy0
            y_lo = max(0, y_lo); y_hi = min(mask.shape[0], y_hi)
            x_lo = max(0, gx0); x_hi = min(mask.shape[1], gx1)
            free = True
            if y_hi > y_lo and x_hi > x_lo:
                strip = mask[y_lo:y_hi, x_lo:x_hi]
                free = not strip.any()
            if only_free and not free:
                continue
            ends.append(dict(axis="y", dir=direction, y=int(gy0), x=int((gx0 + gx1) // 2),
                              cross0=int(gx0), cross1=int(gx1), comp=comp_id, free=free))
    return ends


def looks_like_door(arr, mask, e1, e2):
    """Между двумя торцами: если есть белые пиксели с тёмной 1-2px обводкой (дверная коробка)
    или разреженный пунктир (дуга двери) - считаем дверным проёмом, не gap."""
    if e1["axis"] == "x" and e2["axis"] == "x":
        x0, x1 = sorted([e1["x"], e2["x"]])
        y0 = min(e1["cross0"], e2["cross0"])
        y1 = max(e1["cross1"], e2["cross1"])
    elif e1["axis"] == "y" and e2["axis"] == "y":
        y0, y1 = sorted([e1["y"], e2["y"]])
        x0 = min(e1["cross0"], e2["cross0"])
        x1 = max(e1["cross1"], e2["cross1"])
    else:
        return False
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(arr.shape[1], x1), min(arr.shape[0], y1)
    if x1 <= x0 or y1 <= y0:
        return False
    region = arr[y0:y1, x0:x1]
    rmask = mask[y0:y1, x0:x1]
    if region.size == 0:
        return False
    # доля ink-пикселей внутри проёма (обводка коробки / пунктир дуги)
    ink_frac = rmask.mean()
    # белая коробка/дверь: не сплошная ink-стена, но есть заметная ink-разметка (обводка/дуга/штрихи)
    if 0.01 <= ink_frac <= 0.55:
        return True
    return False


def find_gaps_and_stubs(arr, full_mask, mask):
    """mask = wall_mask (толстые стены, без текста/пунктира/импостов), full_mask = вся ink-маска
    (для проверки дверной разметки и "не пусто ли" вокруг свободного торца)."""
    lbl, n = label_ink(mask)
    stats = component_stats(lbl, n)
    all_ends = []          # только свободные торцы -> для gap
    all_ends_full = []     # оба торца, со статусом free/attached -> для stub/island
    for cid, st in stats.items():
        all_ends.extend(free_ends(mask, lbl, cid, st, only_free=True))
        all_ends_full.extend(free_ends(mask, lbl, cid, st, only_free=False))

    gaps = []
    used_pairs = set()
    for i, e1 in enumerate(all_ends):
        for e2 in all_ends[i + 1:]:
            if e1["axis"] != e2["axis"]:
                continue
            if e1["axis"] == "x":
                if abs(e1["y"] - e2["y"]) > END_CROSS_MAX:
                    continue
                dist = abs(e1["x"] - e2["x"])
                if not (GAP_MIN <= dist <= GAP_MAX):
                    continue
                if e1["dir"] == e2["dir"]:
                    continue
                # должны смотреть друг на друга
                if e1["x"] < e2["x"] and e1["dir"] != 1:
                    continue
                if e1["x"] > e2["x"] and e1["dir"] != -1:
                    continue
            else:
                if abs(e1["x"] - e2["x"]) > END_CROSS_MAX:
                    continue
                dist = abs(e1["y"] - e2["y"])
                if not (GAP_MIN <= dist <= GAP_MAX):
                    continue
                if e1["dir"] == e2["dir"]:
                    continue
                if e1["y"] < e2["y"] and e1["dir"] != 1:
                    continue
                if e1["y"] > e2["y"] and e1["dir"] != -1:
                    continue
            key = tuple(sorted([(e1["comp"], e1["x"], e1["y"]), (e2["comp"], e2["x"], e2["y"])]))
            if key in used_pairs:
                continue
            if looks_like_door(arr, full_mask, e1, e2):
                continue
            used_pairs.add(key)
            xs = [e1.get("x", e1.get("cross0")), e2.get("x", e2.get("cross0"))]
            ys = [e1.get("y", e1.get("cross0")), e2.get("y", e2.get("cross0"))]
            if e1["axis"] == "x":
                x0, x1 = sorted([e1["x"], e2["x"]])
                y0 = min(e1["cross0"], e2["cross0"]); y1 = max(e1["cross1"], e2["cross1"])
            else:
                y0, y1 = sorted([e1["y"], e2["y"]])
                x0 = min(e1["cross0"], e2["cross0"]); x1 = max(e1["cross1"], e2["cross1"])
            gaps.append(dict(cls="gap", x=int(x0), y=int(y0), w=int(max(1, x1 - x0)),
                              h=int(max(1, y1 - y0)), area=int(max(1, x1 - x0) * max(1, y1 - y0))))

    # stub / island: маленькие обособленные компоненты (не часть большой сети стен),
    # классификация по числу свободных/закреплённых торцов (free_ends only_free=False).
    ends_by_comp = {}
    for e in all_ends_full:
        ends_by_comp.setdefault(e["comp"], []).append(e)

    stubs = []
    for cid, st in stats.items():
        area = st["area"]
        mind = min(st["w"], st["h"])
        maxd = max(st["w"], st["h"])
        if area >= STUB_MAX_AREA or mind > STUB_MIN_DIM or not (STUB_MAX_DIM_LO <= maxd <= STUB_MAX_DIM_HI):
            continue
        comp_ends = ends_by_comp.get(cid, [])
        if len(comp_ends) == 0:
            continue
        touching = sum(1 for e in comp_ends if not e["free"])
        free = sum(1 for e in comp_ends if e["free"])
        free_end = next((e for e in comp_ends if e["free"]), None)
        if touching >= 1 and free >= 1:
            # проверить, что вокруг свободного торца реально пусто (не дверная коробка/пилон)
            if free_end is not None:
                ex, ey = free_end.get("x"), free_end.get("y")
                y_lo = max(0, ey - STUB_FREE_RADIUS); y_hi = min(mask.shape[0], ey + STUB_FREE_RADIUS + 1)
                x_lo = max(0, ex - STUB_FREE_RADIUS); x_hi = min(mask.shape[1], ex + STUB_FREE_RADIUS + 1)
                around = arr[y_lo:y_hi, x_lo:x_hi]
                around_mask = full_mask[y_lo:y_hi, x_lo:x_hi]
                # белая коробка = почти чисто белый регион вокруг; исключаем как не-stub только если
                # регион явно НЕ пустой пол/бумага (т.е. есть ink помимо самой стены -> вероятно объект)
                extra_ink = around_mask.sum() - st["area"]
                if extra_ink > around_mask.size * 0.15:
                    continue
            stubs.append(dict(cls="stub", x=int(st["x0"]), y=int(st["y0"]), w=int(st["w"]),
                               h=int(st["h"]), area=int(area)))
        elif free >= 2 and touching == 0:
            stubs.append(dict(cls="island", x=int(st["x0"]), y=int(st["y0"]), w=int(st["w"]),
                               h=int(st["h"]), area=int(area)))
    return gaps, stubs


def dedupe(items, tol=6):
    out = []
    for it in items:
        cx, cy = it["x"] + it["w"] / 2, it["y"] + it["h"] / 2
        dup = False
        for o in out:
            if o["cls"] != it["cls"]:
                continue
            ocx, ocy = o["x"] + o["w"] / 2, o["y"] + o["h"] / 2
            if abs(cx - ocx) < tol and abs(cy - ocy) < tol:
                dup = True
                break
        if not dup:
            out.append(it)
    return out


def make_sheet(img, findings, out_path):
    order = {"sliver": 0, "gap": 1, "stub": 2, "island": 3}
    items = sorted(findings, key=lambda f: (order.get(f["cls"], 9), -f["area"]))[:MAX_SHEET_ITEMS]
    if not items:
        return
    cols = 5
    rows = (len(items) + cols - 1) // cols
    pad = 4
    label_h = 22
    cell = CROP + pad * 2
    sheet = Image.new("RGB", (cols * cell, rows * (cell + label_h)), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
    except Exception:
        font = ImageFont.load_default()
    W, H = img.size
    for idx, it in enumerate(items):
        cx = it["x"] + it["w"] / 2
        cy = it["y"] + it["h"] / 2
        half = CROP // 2
        left = int(max(0, min(W - CROP, cx - half)))
        top = int(max(0, min(H - CROP, cy - half)))
        crop = img.crop((left, top, left + CROP, top + CROP)).convert("RGB")
        cdraw = ImageDraw.Draw(crop)
        bx0 = it["x"] - left
        by0 = it["y"] - top
        bx1 = bx0 + max(it["w"], 1)
        by1 = by0 + max(it["h"], 1)
        cdraw.rectangle([bx0 - 2, by0 - 2, bx1 + 2, by1 + 2], outline=(255, 0, 0), width=2)
        col = idx % cols
        row = idx // cols
        px = col * cell + pad
        py = row * (cell + label_h) + pad
        sheet.paste(crop, (px, py))
        label = f"{it['cls']} ({it['x']},{it['y']}) {it['w']}x{it['h']} a={it['area']}"
        draw.text((px, py + CROP + 2), label, fill=(0, 0, 0), font=font)
    sheet.save(out_path)


def make_overlay(img, findings, out_path, max_w=2000):
    im = img.convert("RGB").copy()
    draw = ImageDraw.Draw(im)
    colors = {"sliver": (255, 0, 0), "gap": (255, 140, 0), "stub": (200, 0, 200), "island": (0, 120, 255)}
    for it in findings:
        col = colors.get(it["cls"], (255, 0, 0))
        draw.rectangle([it["x"] - 3, it["y"] - 3, it["x"] + it["w"] + 3, it["y"] + it["h"] + 3], outline=col, width=3)
    if im.width > max_w:
        ratio = max_w / im.width
        im = im.resize((max_w, int(im.height * ratio)), Image.LANCZOS)
    im.save(out_path)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    png_path = Path(args[0])
    out_dir = None
    json_path = None
    i = 1
    while i < len(args):
        if args[i] == "--out":
            out_dir = Path(args[i + 1]); i += 2
        elif args[i] == "--json":
            json_path = Path(args[i + 1]); i += 2
        else:
            i += 1

    img = Image.open(png_path).convert("RGB")
    arr = np.array(img)
    full_mask = ink_mask(arr)
    wall_mask, _ = wall_mask_by_area(full_mask)

    dashes = dash_centers(full_mask)
    grey = grey_mask(arr)
    slivers = find_slivers(wall_mask, dashes, grey)
    gaps, stubs_islands = find_gaps_and_stubs(arr, full_mask, wall_mask)
    stubs = [f for f in stubs_islands if f["cls"] == "stub"]
    islands = [f for f in stubs_islands if f["cls"] == "island"]

    all_findings = dedupe(slivers) + dedupe(gaps) + dedupe(stubs) + dedupe(islands)

    counts = collections_count(all_findings)
    summary = f"sliver {counts.get('sliver', 0)} / gap {counts.get('gap', 0)} / stub {counts.get('stub', 0)} / island {counts.get('island', 0)}"
    print(summary, file=sys.stderr)

    out_json = json.dumps(all_findings, ensure_ascii=False, indent=2)
    if json_path:
        json_path.write_text(out_json)
    else:
        print(out_json)

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        make_sheet(img, all_findings, out_dir / "gaps-sheet.png")
        make_overlay(img, all_findings, out_dir / "gaps-overlay.png")
        print(f"-> {out_dir / 'gaps-sheet.png'}", file=sys.stderr)
        print(f"-> {out_dir / 'gaps-overlay.png'}", file=sys.stderr)


def collections_count(items):
    c = {}
    for it in items:
        c[it["cls"]] = c.get(it["cls"], 0) + 1
    return c


if __name__ == "__main__":
    main()
