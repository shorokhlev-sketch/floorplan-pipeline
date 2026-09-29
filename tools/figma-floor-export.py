#!/usr/bin/env python3
"""Figma floor export (download_assets svg of the floor frame) -> clean floor SVG for the assembler,
plus the diff by object id against the machine version.

    uv run tools/figma-floor-export.py F <export.svg> [--group NAME]
    uv run tools/figma-floor-export.py --diff <before.svg> <after.svg>   (layers-F.svg, floor-F-manual.svg
                                                                      or a raw export, in any pair)

What it does: (1) decodes ids (Figma writes UTF-8 bytes as &#N; and html.unescape breaks them);
(2) takes the body of the drawing group (default "outline-layers", the group the layer SVG was imported
as; no PDF underlay, no background); (3) the export is shrunk to 4096 px on the long side, so the scale
is read from the frame rect and undone with a transform=scale(W/4096) wrapper; fill="none" on the
wrapper is required, otherwise the slab outline gets filled; (4) white fills in the columns layer ->
black; (5) drops thick red review marks; (6) writes WORK/plan-studio/v3/figma/floor-F-manual.svg
(px, 1 px = 1 cm, origin = floor bbox) and, if layers-F.svg exists, prints the diff of the manual edits:
deleted / moved / recolored / added, by "pdf N" id and by layer.

The diff is the main acceptance tool: one manual floor turns into a list of edits, and the list turns
into a rule that is dry-run on the other floors (docs/harness.md).
"""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
K = fpconfig.CM_PER_PT
GROUP = "outline-layers"                     # name of the imported layer group in the Figma frame
COLUMNS = fpconfig.layers().get("columns_layer", "")
NUM = r'[-+]?\d*\.?\d+(?:e[-+]?\d+)?'


def unent(s):
    return re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), s).encode('latin-1', 'ignore').decode('utf-8', 'replace')


def bbox_of_d(d):
    toks = re.findall(r'[MmLlHhVvCcZz]|' + NUM, d)
    xs, ys = [], []; cx = cy = sx = sy = 0.0; cmd = None; i = 0
    while i < len(toks):
        t = toks[i]
        if re.fullmatch(r'[MmLlHhVvCcZz]', t):
            cmd = t; i += 1
            if cmd in 'Zz': cx, cy = sx, sy
            continue
        if cmd in 'Hh':
            v = float(t); cx = v if cmd == 'H' else cx + v; xs.append(cx); ys.append(cy); i += 1
        elif cmd in 'Vv':
            v = float(t); cy = v if cmd == 'V' else cy + v; xs.append(cx); ys.append(cy); i += 1
        elif cmd in 'Cc':
            p = [float(x) for x in toks[i:i + 6]]; i += 6
            for j in range(0, 6, 2):
                px, py = (p[j], p[j + 1]) if cmd == 'C' else (cx + p[j], cy + p[j + 1]); xs.append(px); ys.append(py)
            cx, cy = xs[-1], ys[-1]
        else:
            px, py = float(toks[i]), float(toks[i + 1]); i += 2
            if cmd in 'ml': px += cx; py += cy
            cx, cy = px, py; xs.append(cx); ys.append(cy)
            if cmd in 'Mm': sx, sy = cx, cy; cmd = 'L' if cmd == 'M' else 'l'
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def parse_paths(svg, scale=1.0):
    out, layer = {}, None
    for m in re.finditer(r'<g id="([^"]+)">|<path id="(pdf [^"]+)"([^>]*)/>', svg):
        if m.group(1):
            layer = m.group(1); continue
        attrs = m.group(3)
        d = re.search(r' d="([^"]+)"', attrs).group(1)
        fill = re.search(r'fill="([^"]+)"', attrs); fill = (fill.group(1) if fill else 'none').lower()
        fill = {'black': '#000000', 'white': '#ffffff'}.get(fill, fill)
        b = bbox_of_d(d)
        out[m.group(2)] = dict(layer=layer, bbox=tuple(v / scale for v in b) if b else None, fill=fill)
    return out


def diff_by_id(O, L):
    """O, L: parse_paths() of the original and the edited SVG (same coordinate system).
    Returns deleted, added, moved [(id, max bbox shift px)], recolored [(id, old fill, new fill)]."""
    deleted = [p for p in O if p not in L]; added = [p for p in L if p not in O]
    moved = [(p, round(max(abs(a - b) for a, b in zip(O[p]['bbox'], L[p]['bbox'])), 1)) for p in O
             if p in L and O[p]['bbox'] and L[p]['bbox'] and max(abs(a - b) for a, b in zip(O[p]['bbox'], L[p]['bbox'])) > 1.0]
    recol = [(p, O[p]['fill'], L[p]['fill']) for p in O if p in L and O[p]['fill'] != L[p]['fill']]
    return dict(deleted=deleted, added=added, moved=moved, recolored=recol)


def print_diff(O, L, name):
    import collections
    d = diff_by_id(O, L)
    by_layer = lambda ids: dict(collections.Counter(O[p]["layer"] for p in ids))
    print(f'  diff vs {name}: deleted {len(d["deleted"])} {by_layer(d["deleted"])}; '
          f'moved/changed {len(d["moved"])} {by_layer([m[0] for m in d["moved"]])}; '
          f'recolored {len(d["recolored"])}; added {len(d["added"])}')
    big = sorted(d["moved"], key=lambda m: -m[1])[:12]
    if big: print('  largest moves:', ', '.join(f'{p} {v}px' for p, v in big))
    return d


def load_for_diff(p):
    """parse_paths() of any of the three SVG kinds, in layer SVG px: layers-F.svg as is;
    floor-F-manual.svg keeps the Figma coordinates under <g ... transform="scale(1/s)">, so its bboxes
    are multiplied by that factor; a raw Figma export is divided by the scale of its frame rect."""
    svg = re.sub(r'id="([^"]*&#\d+;[^"]*)"', lambda m: 'id="' + unent(m.group(1)) + '"', p.read_text())
    wrap = re.search(r'<g id="[^"]*" fill="none" transform="scale\(([\d.]+)\)">', svg)
    frame = re.search(r'<rect width="[\d.]+" height="[\d.]+" transform="scale\(([\d.]+)\)" fill="white"/>', svg)
    t = float(wrap.group(1)) if wrap else (1 / float(frame.group(1)) if frame else 1.0)
    P = parse_paths(svg)
    if t != 1.0:
        P = {k: dict(v, bbox=tuple(x * t for x in v['bbox']) if v['bbox'] else None) for k, v in P.items()}
    return P


def main():
    if sys.argv[1:2] == ['--diff']:
        a, b = Path(sys.argv[2]), Path(sys.argv[3])
        print_diff(load_for_diff(a), load_for_diff(b), a.name)
        return
    F = int(sys.argv[1]); src = Path(sys.argv[2])
    group = sys.argv[sys.argv.index('--group') + 1] if '--group' in sys.argv else GROUP
    fd = json.loads((ROOT / f'plan-studio/data/floor-{F}.json').read_text())
    W, H = fd['w'] * K, fd['h'] * K
    raw = src.read_text()
    svg = re.sub(r'id="([^"]*)"', lambda m: 'id="' + unent(m.group(1)) + '"', raw)
    vb = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    # масштаб экспорта берём из белого прямоугольника фрейма: <rect width=FW height=FH transform="scale(S)" fill="white"/>.
    # Считать 4096/W нельзя: фрейм в Figma могут растянуть (этаж 3 стал 4362×2710 — план «поплыл» на сайте, 22.09).
    mrect = re.search(r'<rect width="([\d.]+)" height="([\d.]+)" transform="scale\(([\d.]+)\)" fill="white"/>', svg)
    if mrect:
        s = float(mrect.group(3)); FW, FH = float(mrect.group(1)), float(mrect.group(2))
        if abs(FW - W) > 2 or abs(FH - H) > 2:
            print(f'  ! floor {F} frame in Figma is {FW:.1f}x{FH:.1f}, expected {W:.1f}x{H:.1f}: the drawing group is assumed at (0,0) of the frame')
    else:
        s = float(vb.group(1)) / W                  # 4096/W при ужатии, ~1 если экспорт не ужат
    start = svg.find(f'<g id="{group}">'); end = svg.find('<defs>')
    if end < 0: end = svg.rfind('</svg>')
    assert start > 0, f'no group "{group}" in the export'
    body = svg[start + len(f'<g id="{group}">'):end]
    body = body[:body.rfind('</g>')]; body = body[:body.rfind('</g>')]
    assert '<image' not in body, 'в теле есть растр'
    # красные пометки ревьюера (stroke red/#FF0000, толстые) живут внутри «Обводка · слои» и уезжали на сайт (22.09) — снять
    nred = 0
    def _drop_red(m):
        nonlocal nred
        tag = m.group(0)
        if re.search(r'stroke="(red|#F{2}0{4})"', tag, re.I) and float((re.search(r'stroke-width="([\d.]+)"', tag) or [None, '1'])[1]) >= 6:
            nred += 1; return ''
        return tag
    body = re.sub(r'<path\b[^>]*/>', _drop_red, body)
    gcol = re.search(r'<g id="' + re.escape(COLUMNS) + r'">.*?</g>', body, re.S) if COLUMNS else None
    nwhite = 0
    if gcol:
        nwhite = gcol.group(0).count('fill="white"')
        body = body.replace(gcol.group(0), gcol.group(0).replace('fill="white"', 'fill="black"'))
    out = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.1f}" height="{H:.1f}" viewBox="0 0 {W:.1f} {H:.1f}">\n'
           f'<g id="{group} · manual" fill="none" transform="scale({1 / s:.6f})">{body}</g>\n</svg>')
    dst = ROOT / f'plan-studio/v3/figma/floor-{F}-manual.svg'
    dst.write_text(out)
    n_paths = out.count('<path')
    print(f'floor {F}: {dst.name} {len(out) // 1024} KB, {n_paths} paths, export scale {s:.4f}, white columns -> black {nwhite}, red review marks removed {nred}')

    layers = ROOT / f'plan-studio/v3/figma/layers-{F}.svg'
    if layers.exists():
        O = parse_paths(layers.read_text()); L = parse_paths(out, scale=1.0)
        # координаты в out уже под transform; для сравнения bbox домножим на 1/s
        L = {k: dict(v, bbox=tuple(x / s for x in v['bbox']) if v['bbox'] else None) for k, v in L.items()}
        print_diff(O, L, layers.name)


if __name__ == '__main__':
    main()
