# -*- coding: utf-8 -*-
"""
plan-room-areas.py — площади комнат по финальной геометрии Figma (final/rooms/geom-<unit>.json).

Растеризуем контур пола на сетку 1px=1 клетка (1px = 1см), вычитаем балконы/ink/окна/коробки,
"закрываем" дверные проёмы виртуальной стенкой между парными коробками (чтобы дверь не сливала
комнаты в одну компоненту), находим связные компоненты — это комнаты, для каждой считаем площадь
и место под подпись (полюс недоступности через chamfer-дистанс-трансформ).

Только стандартная библиотека (numpy/PIL не установлены).

CLI: python3 tools/plan-room-areas.py <unit> [<unit> ...] [--ascii] [--png]
"""
import json
import math
import os
import sys
from collections import deque

# ---------------------------------------------------------------------------
# растеризация полигонов (чётно-нечётное правило, scanline по центрам клеток)
# ---------------------------------------------------------------------------

def fill_polygon(mask, w, h, poly, value):
    """Заливает полигон в mask (bytearray w*h, индекс y*w+x) значением value.
    Стандартный scanline: на каждой строке (y+0.5) ищем пересечения с рёбрами,
    сортируем и заливаем клетки между парами пересечений (even-odd)."""
    n = len(poly)
    if n < 3:
        return
    ys = [p[1] for p in poly]
    y_min = max(0, int(math.floor(min(ys))))
    y_max = min(h - 1, int(math.ceil(max(ys))) - 1)
    for row in range(y_min, y_max + 1):
        yc = row + 0.5
        xs = []
        for i in range(n):
            x0, y0 = poly[i]
            x1, y1 = poly[(i + 1) % n]
            if y0 == y1:
                continue  # горизонтальное ребро — не пересекает строго
            if (y0 <= yc < y1) or (y1 <= yc < y0):
                t = (yc - y0) / (y1 - y0)
                xs.append(x0 + t * (x1 - x0))
        xs.sort()
        row_off = row * w
        for i in range(0, len(xs) - 1, 2):
            xa, xb = xs[i], xs[i + 1]
            col_start = max(0, int(math.ceil(xa - 0.5)))
            col_end = min(w - 1, int(math.floor(xb - 0.5)))
            if col_end < col_start:
                continue
            for col in range(col_start, col_end + 1):
                mask[row_off + col] = value


def fill_rect(mask, w, h, x0, y0, x1, y1, value):
    """Заливает прямоугольник [x0,x1]x[y0,y1] (в тех же координатах кадра, px)."""
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    col0 = max(0, int(math.floor(x0)))
    col1 = min(w - 1, int(math.ceil(x1)) - 1)
    row0 = max(0, int(math.floor(y0)))
    row1 = min(h - 1, int(math.ceil(y1)) - 1)
    for row in range(row0, row1 + 1):
        off = row * w
        for col in range(col0, col1 + 1):
            mask[off + col] = value


# ---------------------------------------------------------------------------
# закрытие дверных проёмов по парам коробок ("коробка" = jamb)
# ---------------------------------------------------------------------------

def _bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def find_door_closures(jambs):
    """Ищет пары коробок, образующие дверной проём, и возвращает список
    прямоугольников (x0,y0,x1,y1) для закрытия проёма виртуальной стенкой.

    Коробка — тонкий прямоугольник: длинная ось совпадает с толщиной стены,
    короткая (~4px) — с направлением вдоль проёма. Пара образует дверь, если:
      - длинные оси параллельны (та же ориентация bbox),
      - разница центров идёт ПРЕИМУЩЕСТВЕННО поперёк длинной оси (вдоль проёма),
      - зазор между внутренними гранями по этой поперечной оси — 40..130 px.
    Жадное сопоставление: для каждой ещё не занятой коробки берём ближайшую
    подходящую пару.
    """
    boxes = []
    for j in jambs:
        x0, y0, x1, y1 = _bbox(j['poly'])
        w, h = x1 - x0, y1 - y0
        long_axis = 'x' if w >= h else 'y'
        boxes.append({'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1, 'long': long_axis})

    n = len(boxes)
    used = [False] * n
    closures = []
    for i in range(n):
        if used[i]:
            continue
        a = boxes[i]
        best = None  # (k, gap, gap_axis)
        for k in range(i + 1, n):
            if used[k]:
                continue
            b = boxes[k]
            if a['long'] != b['long']:
                continue
            gap_axis = 'y' if a['long'] == 'x' else 'x'
            if gap_axis == 'x':
                gap = max(a['x0'] - b['x1'], b['x0'] - a['x1'])
                gap_diff = abs((a['x0'] + a['x1']) / 2 - (b['x0'] + b['x1']) / 2)
                align_diff = abs((a['y0'] + a['y1']) / 2 - (b['y0'] + b['y1']) / 2)
            else:
                gap = max(a['y0'] - b['y1'], b['y0'] - a['y1'])
                gap_diff = abs((a['y0'] + a['y1']) / 2 - (b['y0'] + b['y1']) / 2)
                align_diff = abs((a['x0'] + a['x1']) / 2 - (b['x0'] + b['x1']) / 2)
            if not (40 <= gap <= 130):
                continue
            if gap_diff <= align_diff:
                continue  # центры должны расходиться в основном вдоль проёма, не поперёк
            if best is None or gap < best[1]:
                best = (k, gap, gap_axis)
        if best is None:
            continue
        k, gap, gap_axis = best
        b = boxes[k]
        used[i] = True
        used[k] = True
        if gap_axis == 'x':
            if a['x1'] <= b['x0']:
                left, right = a, b
            else:
                left, right = b, a
            gx0, gx1 = left['x1'], right['x0']
            gy0, gy1 = min(a['y0'], b['y0']), max(a['y1'], b['y1'])
        else:
            if a['y1'] <= b['y0']:
                top, bottom = a, b
            else:
                top, bottom = b, a
            gy0, gy1 = top['y1'], bottom['y0']
            gx0, gx1 = min(a['x0'], b['x0']), max(a['x1'], b['x1'])
        closures.append((gx0, gy0, gx1, gy1))
    return closures


# ---------------------------------------------------------------------------
# связные компоненты (4-связность), итеративный flood fill
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# эрозия + обратный рост (watershed): щели ≤ 2·ERODE_R px между стенами не сливают комнаты
# ---------------------------------------------------------------------------
ERODE_R = 5

def erode(mask, w, h, r):
    """Клетка остаётся 1, если все клетки в квадрате радиуса r — 1 (separable min)."""
    tmp = bytearray(w * h)
    for y in range(h):
        row = y * w
        run = 0  # длина текущей серии единиц
        for x in range(w):
            run = run + 1 if mask[row + x] else 0
            if run >= 2 * r + 1:
                tmp[row + x - r] = 1
    out = bytearray(w * h)
    for x in range(w):
        run = 0
        for y in range(h):
            run = run + 1 if tmp[y * w + x] else 0
            if run >= 2 * r + 1:
                out[(y - r) * w + x] = 1
    return out


def regrow(mask, w, h, labels):
    """Многоисточниковый BFS: помеченные клетки растут в непомеченные клетки mask==1."""
    q = deque(i for i in range(w * h) if labels[i])
    while q:
        idx = q.popleft()
        lab = labels[idx]
        y, x = divmod(idx, w)
        for nidx in ((idx - w) if y > 0 else -1, (idx + w) if y < h - 1 else -1,
                     (idx - 1) if x > 0 else -1, (idx + 1) if x < w - 1 else -1):
            if nidx >= 0 and mask[nidx] == 1 and labels[nidx] == 0:
                labels[nidx] = lab
                q.append(nidx)
    comps = {}
    for i, lab in enumerate(labels):
        if lab:
            comps.setdefault(lab, []).append(i)
    return labels, [comps[k] for k in sorted(comps)]

def label_components(mask, w, h):
    """Возвращает (labels, comps): labels — список int той же длины что mask
    (0 — не пол/стена, иначе номер компоненты с 1), comps — список списков
    индексов клеток по компонентам (в порядке обнаружения)."""
    labels = [0] * (w * h)
    comps = []
    next_label = 1
    for start in range(w * h):
        if mask[start] != 1 or labels[start] != 0:
            continue
        labels[start] = next_label
        cells = [start]
        q = deque([start])
        while q:
            idx = q.popleft()
            y, x = divmod(idx, w)
            if y > 0:
                nidx = idx - w
                if mask[nidx] == 1 and labels[nidx] == 0:
                    labels[nidx] = next_label
                    cells.append(nidx)
                    q.append(nidx)
            if y < h - 1:
                nidx = idx + w
                if mask[nidx] == 1 and labels[nidx] == 0:
                    labels[nidx] = next_label
                    cells.append(nidx)
                    q.append(nidx)
            if x > 0:
                nidx = idx - 1
                if mask[nidx] == 1 and labels[nidx] == 0:
                    labels[nidx] = next_label
                    cells.append(nidx)
                    q.append(nidx)
            if x < w - 1:
                nidx = idx + 1
                if mask[nidx] == 1 and labels[nidx] == 0:
                    labels[nidx] = next_label
                    cells.append(nidx)
                    q.append(nidx)
        comps.append(cells)
        next_label += 1
    return labels, comps


# ---------------------------------------------------------------------------
# полюс недоступности: chamfer distance transform (L1, два прохода) в bbox компоненты
# ---------------------------------------------------------------------------

def pole_candidates(cells, w):
    """Возвращает список (dist, cx, cy) — топ-50 клеток компоненты по убыванию
    расстояния до ближайшей не-своей клетки (стена/другая комната/за краем сетки),
    cx,cy — координаты центра клетки в px кадра."""
    xs = [c % w for c in cells]
    ys = [c // w for c in cells]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    bw = x1 - x0 + 3  # +1 клетка полей с каждой стороны
    bh = y1 - y0 + 3
    local = bytearray(bw * bh)
    for idx in cells:
        y = idx // w
        x = idx - y * w
        lx = x - x0 + 1
        ly = y - y0 + 1
        local[ly * bw + lx] = 1

    INF = bw + bh + 10
    dist = [0 if v == 0 else INF for v in local]
    # прямой проход (сверху-слева)
    for ly in range(bh):
        rowoff = ly * bw
        for lx in range(bw):
            i = rowoff + lx
            if local[i] == 0:
                continue
            d = dist[i]
            if lx > 0:
                d = min(d, dist[i - 1] + 1)
            if ly > 0:
                d = min(d, dist[i - bw] + 1)
            dist[i] = d
    # обратный проход (снизу-справа)
    for ly in range(bh - 1, -1, -1):
        rowoff = ly * bw
        for lx in range(bw - 1, -1, -1):
            i = rowoff + lx
            if local[i] == 0:
                continue
            d = dist[i]
            if lx < bw - 1:
                d = min(d, dist[i + 1] + 1)
            if ly < bh - 1:
                d = min(d, dist[i + bw] + 1)
            dist[i] = d

    candidates = []
    for ly in range(bh):
        rowoff = ly * bw
        for lx in range(bw):
            if local[rowoff + lx]:
                candidates.append((dist[rowoff + lx], lx, ly))
    candidates.sort(key=lambda t: -t[0])
    # все клетки с запасом до стены ≥ 20 px, по убыванию расстояния (полюс часто занят заголовком)
    top = [c for c in candidates if c[0] >= 20][:200000] or candidates[:50]
    return [(d, lx + x0 - 1 + 0.5, ly + y0 - 1 + 0.5) for d, lx, ly in top]


LABEL_W = 90.0
LABEL_H = 34.0


def place_label(candidates, label_bboxes):
    """Выбирает лучшую (по расстоянию) клетку из candidates, для которой
    рамка подписи ~90x34 px не пересекает существующие labels. Если ни одна
    не подходит — берёт полюс (первый кандидат) и ставит label_collision=True."""
    PAD = 24.0  # зазор до чужих подписей/заголовка
    for d, cx, cy in candidates:
        if d < LABEL_H / 2 + 6:
            break  # слишком близко к стене — дальше кандидаты только хуже
        bx0, by0, bx1, by1 = cx - LABEL_W / 2 - PAD, cy - LABEL_H / 2 - PAD, cx + LABEL_W / 2 + PAD, cy + LABEL_H / 2 + PAD
        collide = False
        for (lx0, ly0, lx1, ly1) in label_bboxes:
            if bx0 < lx1 and bx1 > lx0 and by0 < ly1 and by1 > ly0:
                collide = True
                break
        if not collide:
            return cx, cy, False
    if candidates:
        _, cx, cy = candidates[0]
        return cx, cy, True
    return 0.0, 0.0, True


def format_area(area_m2):
    return '{:.1f} m²'.format(area_m2).replace('.', ',')


# ---------------------------------------------------------------------------
# основной пайплайн
# ---------------------------------------------------------------------------

ROOM_MIN_CELLS = 10000     # 1.0 m²
SLIVER_MIN_CELLS = 3000    # 0.3 m² — обрезки между 0.3 и 1.0 m² только считаем и предупреждаем



def _norm_poly(p):
    """bbox [x0,y0,x1,y1] (4 чисел) -> прямоугольник; список точек -> как есть."""
    if p and len(p) == 4 and all(isinstance(v, (int, float)) for v in p):
        x0, y0, x1, y1 = p
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    return p

def _normalize(data):
    data = dict(data)
    if data.get('floor'):
        data['floor'] = _norm_poly(data['floor'])
    for k in ('balconies', 'ink', 'white'):
        data[k] = [_norm_poly(p) for p in data.get(k, [])]
    data['jambs'] = [dict(j, poly=_norm_poly(j['poly'])) for j in data.get('jambs', [])]
    return data

def analyze(data, return_debug=False):
    data = _normalize(data)
    w = int(math.ceil(data['w']))
    h = int(math.ceil(data['h']))

    floor_mask = bytearray(w * h)
    fill_polygon(floor_mask, w, h, data['floor'], 1)
    floor_total_cells = sum(floor_mask)  # площадь пятна пола целиком — для проверки >60%

    mask = bytearray(floor_mask)
    # балконы НЕ вычитаем: полигон 'Пол' их не включает, а полигоны балконов из
    # final-data местами залезают в комнату (310/410/207) — вычитание срезало бы комнату
    for poly in data.get('ink', []):
        fill_polygon(mask, w, h, poly, 0)
    for poly in data.get('white', []):
        fill_polygon(mask, w, h, poly, 0)
    jambs = data.get('jambs', [])
    for j in jambs:
        fill_polygon(mask, w, h, j['poly'], 0)

    closures = find_door_closures(jambs)
    for (gx0, gy0, gx1, gy1) in closures:
        fill_rect(mask, w, h, gx0, gy0, gx1, gy1, 0)
    n_doors_closed = len(closures)

    eroded = erode(mask, w, h, ERODE_R)
    labels_grid, _ = label_components(eroded, w, h)
    labels_grid, comps = regrow(mask, w, h, labels_grid)
    # подпись living-площади (в 'Подписи', внутри пола, вне балконов) будет заменена
    # подписями комнат — из списка коллизий её убираем; первые две записи = заголовок
    label_bboxes = []
    for i, bb in enumerate(data.get('labels', [])):
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        ix, iy = int(cx), int(cy)
        inside_floor = 0 <= ix < w and 0 <= iy < h and floor_mask[iy * w + ix] == 1
        if i >= 2 and inside_floor:
            continue
        label_bboxes.append(bb)

    rooms = []
    sliver_count = 0
    for comp_id, cells in enumerate(comps, start=1):
        n = len(cells)
        if n < ROOM_MIN_CELLS:
            if SLIVER_MIN_CELLS <= n < ROOM_MIN_CELLS:
                sliver_count += 1
            continue
        xs = [c % w for c in cells]
        ys = [c // w for c in cells]
        bbox = [min(xs), min(ys), max(xs) + 1, max(ys) + 1]
        candidates = pole_candidates(cells, w)
        cx, cy, collision = place_label(candidates, label_bboxes)
        area_m2 = round(n / 10000.0, 1)
        rooms.append({
            '_comp': comp_id,
            'cells': n,
            'area_m2': area_m2,
            'label': format_area(area_m2),
            'x': round(cx, 1),
            'y': round(cy, 1),
            'bbox': bbox,
            'label_collision': collision,
        })

    rooms.sort(key=lambda r: -r['cells'])
    comp_to_id = {}
    for i, r in enumerate(rooms, start=1):
        r['id'] = i
        comp_to_id[r.pop('_comp')] = i

    sum_m2 = round(sum(r['area_m2'] for r in rooms), 1)
    living = data.get('living')
    diff_pct = None
    warnings = []
    if living:
        diff_pct = round((sum_m2 - living) / living * 100, 1)
        if abs(diff_pct) > 5:
            warnings.append('diff_pct {:+.1f}% превышает ±5% от living'.format(diff_pct))
    if sliver_count:
        warnings.append('{} отброшенных обрезков (0.3–1.0 m²)'.format(sliver_count))
    if len(closures) >= 2 and floor_total_cells:
        for r in rooms:
            if r['cells'] > 0.6 * floor_total_cells:
                warnings.append('комната {} занимает >60% площади пола — возможен незакрытый проём'.format(r['id']))

    result = {
        'unit': data.get('unit'),
        'rooms': rooms,
        'sum_m2': sum_m2,
        'living': living,
        'diff_pct': diff_pct,
        'n_doors_closed': n_doors_closed,
        'warnings': warnings,
    }
    if return_debug:
        return result, {'w': w, 'h': h, 'labels': labels_grid, 'comp_to_id': comp_to_id}
    return result


def render_ascii(w, h, labels_grid, comp_to_id, step=20):
    """Уменьшенная (1 символ = step px) карта комнат для отладки: цифра — id комнаты (mod 10),
    '.' — стена/фон/коридор без комнаты."""
    lines = []
    for by in range(0, h, step):
        cy = min(by + step // 2, h - 1)
        row_chars = []
        for bx in range(0, w, step):
            cx = min(bx + step // 2, w - 1)
            comp = labels_grid[cy * w + cx]
            rid = comp_to_id.get(comp)
            row_chars.append(str(rid % 10) if rid else '.')
        lines.append(''.join(row_chars))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _final_rooms_dir():
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, tools_dir)
    import fpconfig
    return os.path.join(str(fpconfig.WORK), 'plan-studio', 'v3', 'figma', 'final', 'rooms')


def main(argv):
    flags = {a for a in argv if a.startswith('--')}
    units = [a for a in argv if not a.startswith('--')]
    if not units:
        print('usage: python3 tools/plan-room-areas.py <unit> [<unit> ...] [--ascii] [--png]', file=sys.stderr)
        return 1

    base = _final_rooms_dir()
    want_ascii = '--ascii' in flags
    # --png сознательно проигнорирован: PIL не установлен, растрового вывода нет.

    for unit in units:
        in_path = os.path.join(base, 'geom-{}.json'.format(unit))
        with open(in_path, encoding='utf-8') as f:
            data = json.load(f)

        result, debug = analyze(data, return_debug=True)

        out_path = os.path.join(base, 'rooms-{}.json'.format(unit))
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.write('\n')

        areas = [r['area_m2'] for r in result['rooms']]
        parts = [
            '{}:'.format(unit),
            '{} rooms'.format(len(result['rooms'])),
            '{}'.format(areas),
            'sum {}'.format(result['sum_m2']),
            'living {}'.format(result['living']),
        ]
        if result['diff_pct'] is not None:
            parts.append('({:+.1f}%)'.format(result['diff_pct']))
        parts.append('doors {}'.format(result['n_doors_closed']))
        print(' '.join(parts))
        for w_text in result['warnings']:
            print('  warning: {}'.format(w_text))

        if want_ascii:
            print(render_ascii(debug['w'], debug['h'], debug['labels'], debug['comp_to_id']))

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
