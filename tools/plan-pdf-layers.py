#!/usr/bin/env python3
"""Architect's PDF -> floor SVG by the PDF's own OCG layers, without color heuristics.

    uv run tools/plan-pdf-layers.py F [F ...] [--inventory] [--no-chain] [--pdf-colors] [--out DIR]

For floor F it reads the page and bbox from WORK/plan-studio/data/floor-F.json, takes every
page.get_drawings() object whose rect intersects the floor bbox and keeps only the layers listed in
the layer config (config/layers.*.json, see fpconfig.LAYERS_PATH), with the per-layer filters given
there (hatch colors out, only black wedges from a service layer, and so on).
Writes WORK/plan-studio/v3/figma/layers-F.svg: one <g id="<layer name>"> per layer, one
<path id="pdf <seqno>"> per PDF object, coordinates (p - bbox.xy) * CM_PER_PT (1 px = 1 cm),
colors, widths and dashes as in the PDF (width 0 -> 0.7). Nothing is merged or classified.
The "pdf <seqno>" ids survive a round trip through Figma, which is what makes the diff by id work
(figma-floor-export.py).

--inventory   print every layer inside the floor bbox with its object count (check names first);
              before floor-index.py has run, lists the whole page of floor F and writes nothing
--pdf-colors  keep PDF colors; default is ink mode: every non-white fill and every stroke -> #000000,
              white stays white, hatch and column crosses in drop_strokes layers are dropped
--no-chain    one <path> per PDF object; by default consecutive single segments of one style with
              end == start are chained into one polyline without loss (PDF door arcs are hundreds of
              segments under 1 pt; id = "pdf <first seqno>-<last seqno>")
"""
import json, sys
from pathlib import Path
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
DATA = ROOT / "plan-studio/data"
OUT = ROOT / "plan-studio/v3/figma"
PDF = fpconfig.PDF
K = fpconfig.CM_PER_PT
HAIR = 0.7


def f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def rgb(c):
    if c is None:
        return "none"
    return "#%02X%02X%02X" % tuple(int(round(x * 255)) for x in c[:3])


def hx(c):
    return rgb(c) if c is not None else None


def make_pred(rule):
    """Layer rule from the layer config -> predicate "keep this PDF object"."""
    take = rule.get("take", "all")
    fills = {c.upper() for c in rule.get("fill_colors", [])}
    also = {c.upper() for c in rule.get("also_stroke_colors", [])}
    excl = {c.upper() for c in rule.get("exclude_stroke_colors", [])}

    def pred(dr):
        col = hx(dr.get("color"))
        if col in excl:
            return False
        if take == "fills":
            if "f" in dr["type"]:
                return not fills or hx(dr.get("fill")) in fills
            return col in also
        if take == "strokes":
            return "s" in dr["type"]
        return True
    return pred


# PDF layer -> predicate "keep this object". Order = order of groups in the SVG (bottom to top).
LAYER_CFG = fpconfig.layers()
KEEP = [(r["layer"], make_pred(r)) for r in LAYER_CFG["keep"]]
KEEP_MAP = dict(KEEP)


def path_d(dr, P):
    segs, cur = [], None
    for it in dr["items"]:
        kind = it[0]
        if kind == "l":
            a, b = it[1], it[2]
            if cur is None or cur != a:
                segs.append("M " + P(a))
            segs.append("L " + P(b)); cur = b
        elif kind == "c":
            a, c1, c2, b = it[1], it[2], it[3], it[4]
            if cur is None or cur != a:
                segs.append("M " + P(a))
            segs.append("C " + P(c1) + " " + P(c2) + " " + P(b)); cur = b
        elif kind == "re":
            q = it[1]
            segs.append("M " + P(q.tl) + " L " + P(q.tr) + " L " + P(q.br) + " L " + P(q.bl) + " Z"); cur = None
        elif kind == "qu":
            q = it[1]
            segs.append("M " + P(q.ul) + " L " + P(q.ur) + " L " + P(q.lr) + " L " + P(q.ll) + " Z"); cur = None
    if segs and dr.get("closePath"):
        segs.append("Z")
    return " ".join(segs)


# Правило ревьюера (2026-09-21 ночь): «всё цвета 000000». В режиме ink (по умолчанию): любая заливка, кроме белой, → #000000;
# любая обводка → #000000; белое (кирпичи остекления, окна, подложки ядра) остаётся белым. Слои, где обводки = штриховка/крест,
# а не геометрия: კედლები (засечки на перегородках) и კოლონები (крест X-пилона) — диагональные обводки выбрасываются;
# замкнутый контур из осевых линий (колонна/стена без заливки в PDF) → чёрная заливка; незамкнутые осевые → чёрные линии.
INK = "#000000"
DROP_STROKES_IN = {r["layer"] for r in LAYER_CFG["keep"] if r.get("drop_strokes")}
WHITE_TO_INK = {r["layer"] for r in LAYER_CFG["keep"] if r.get("white_fill_to_ink")}
TICK_PX = float(LAYER_CFG.get("tick_px", 3.0))   # в слоях DROP_STROKES_IN незамкнутые обводки короче этого (px) = засечки штриховки → долой


def _is_axis(dr, eps=0.08):
    """все сегменты объекта — горизонтальные/вертикальные отрезки"""
    for it in dr["items"]:
        if it[0] != "l":
            return False
        a, b = it[1], it[2]
        if abs(a.x - b.x) > eps and abs(a.y - b.y) > eps:
            return False
    return True


def ink_chain_mode(layer, chain):
    """Для слоёв DROP_STROKES_IN решает судьбу цепочки обводок (без заливки):
    'fill'  — замкнутый контур из ≥3 осевых отрезков (колонна/стена, нарисованная контуром) → чёрная заливка;
    'line'  — незамкнутые осевые линии (грани стен с проёмами) → чёрные линии;
    None    — диагонали (крест X-пилона, штриховка перегородок) → выбросить."""
    if layer not in DROP_STROKES_IN or "f" in chain[0]["type"]:
        return "keep"
    if not all(_is_axis(dr) for dr in chain):
        return None
    segs = sum(len(dr["items"]) for dr in chain)
    a = chain[0]["items"][0][1]; b = chain[-1]["items"][-1][2]
    closed = segs >= 3 and abs(a - b) < 1e-3 or any(dr.get("closePath") for dr in chain)
    if closed:
        return "fill"
    length = sum(abs(it[2] - it[1]) for dr in chain for it in dr["items"]) * K
    return None if length < TICK_PX else "line"


def path_attrs(dr, ink=True):
    attrs = []
    t = dr["type"]
    fill = rgb(dr.get("fill")) if "f" in t else "none"
    if ink and fill not in ("none", "#FFFFFF"):
        fill = INK
    if ink and fill == "#FFFFFF" and dr.get("_ink_white_to_black"):
        fill = INK                                  # X-пилоны კოლონები: белый прямоугольник → чёрный (баг v1–v5, найден на этаже 4)
    attrs.append(f'fill="{fill}"')
    if fill != "none":
        attrs.append('fill-rule="evenodd"' if dr.get("even_odd") else 'fill-rule="nonzero"')
        if dr.get("fill_opacity") not in (None, 1.0):
            attrs.append(f'fill-opacity="{f(dr["fill_opacity"])}"')
    if "s" in t and dr.get("color") is not None:
        w = dr.get("width") or 0
        attrs.append(f'stroke="{INK if ink else rgb(dr["color"])}" stroke-width="{f(max(w * K, HAIR))}"')
        cap = (dr.get("lineCap") or (0,))[0]
        attrs.append('stroke-linecap="%s"' % ("round" if cap == 1 else "square" if cap == 2 else "butt"))
        join = dr.get("lineJoin") or 0
        attrs.append('stroke-linejoin="%s"' % ("round" if join == 1 else "bevel" if join == 2 else "miter"))
        dashes = dr.get("dashes")
        if dashes and dashes.startswith("[") and dashes.strip() not in ("[] 0", "[ ] 0"):
            arr = dashes.split("]")[0].strip("[ ").split()
            if arr:
                attrs.append('stroke-dasharray="%s"' % " ".join(f(float(a) * K) for a in arr))
        if dr.get("stroke_opacity") not in (None, 1.0):
            attrs.append(f'stroke-opacity="{f(dr["stroke_opacity"])}"')
    return " ".join(attrs)


def _style(dr):
    return (dr["type"], hx(dr.get("fill")), hx(dr.get("color")), round(dr.get("width") or 0, 3),
            dr.get("dashes"), tuple(dr.get("lineCap") or ()), dr.get("lineJoin"), dr.get("even_odd"),
            dr.get("stroke_opacity"), dr.get("fill_opacity"))


def chain_segments(drawings, eps=1e-3):
    """Без потерь: подряд идущие (по seqno) объекты «одна линия l», одного стиля, у которых конец предыдущего = начало
    следующего, сцепляются в одну полилинию. Так PDF рисует дуги дверей: ~137 отрезков < 1 pt на дугу."""
    chains, cur = [], []
    for dr in sorted(drawings, key=lambda d: d["seqno"]):
        single = len(dr["items"]) == 1 and dr["items"][0][0] == "l" and "f" not in dr["type"] and not dr.get("closePath")
        if cur and single and len(cur[-1]["items"]) == 1 and cur[-1]["items"][0][0] == "l" and "f" not in cur[-1]["type"] \
                and _style(cur[-1]) == _style(dr):
            end = cur[-1]["items"][0][2]
            if abs(end - dr["items"][0][1]) < eps:
                cur.append(dr); continue
            if abs(end - dr["items"][0][2]) < eps:      # отрезок нарисован в обратную сторону — разворачиваем
                rd = dict(dr); rd["items"] = [("l", dr["items"][0][2], dr["items"][0][1])]
                cur.append(rd); continue
        if True:
            if cur:
                chains.append(cur)
            cur = [dr]
    if cur:
        chains.append(cur)
    return chains


def build_floor(doc, F, out_dir, inventory=False, chain=True, ink=True):
    path = DATA / f"floor-{F}.json"
    if path.exists():
        fd = json.loads(path.read_text())
    elif inventory:
        # a new PDF: no floor file yet (floor-index.py needs the layer names this lists) -> whole page
        pno = fpconfig.page_for_floor(F)
        if not 1 <= pno <= len(doc):
            sys.exit(f"floor {F}: page {pno} is outside the PDF ({len(doc)} pages)")
        r = doc[pno - 1].rect
        fd = {"page": pno, "bbox": [r.x0, r.y0, r.x1, r.y1]}
    else:
        sys.exit(f"floor {F}: {path} not found; run tools/floor-index.py first")
    x0, y0, x1, y1 = fd["bbox"]
    clip = pymupdf.Rect(x0, y0, x1, y1)
    pno = fd["page"] - 1
    page = doc[pno]
    W, H = (x1 - x0) * K, (y1 - y0) * K

    def P(p):
        return f((p.x - x0) * K) + " " + f((p.y - y0) * K)

    groups = {name: [] for name, _ in KEEP}
    inv = {}
    for dr in page.get_drawings():
        r = dr["rect"]
        if r.x1 < clip.x0 or r.x0 > clip.x1 or r.y1 < clip.y0 or r.y0 > clip.y1:
            continue
        L = dr.get("layer") or ""
        inv[L] = inv.get(L, 0) + 1
        pred = KEEP_MAP.get(L)
        if pred is None or not pred(dr):
            continue
        if ink and L in DROP_STROKES_IN and "f" in dr["type"] and "s" in dr["type"]:
            dr = dict(dr); dr["type"] = "f"        # заливка есть, её обводку (тот же цвет) не рисуем
        if ink and L in WHITE_TO_INK and "f" in dr["type"]:
            dr = dict(dr); dr["_ink_white_to_black"] = True   # белые X-пилоны тоже чёрные (ревьюер: «залитый чёрный прямоугольник»)
        groups[L].append(dr)

    if inventory:
        print(f"--- layers of page {fd['page']} inside the bbox of floor {F}:")
        for L, n in sorted(inv.items(), key=lambda kv: -kv[1]):
            print(f"  {n:7d}  {L!r}  {'KEEP' if L in KEEP_MAP else '-'}")
        if not path.exists():
            print(f"  (no {path.name} yet: whole page {fd['page']}; fill the layer config, then run floor-index.py)")
            return 0

    raw_total = sum(len(v) for v in groups.values())
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{f(W)}" height="{f(H)}" viewBox="0 0 {f(W)} {f(H)}">']
    for name, _ in KEEP:
        parts.append(f'<g id="{name}">')
        paths = []
        for chain in (chain_segments(groups[name]) if chain else [[dr] for dr in groups[name]]):
            mode = ink_chain_mode(name, chain) if ink else "keep"
            if mode is None:
                continue
            if len(chain) == 1:
                d = path_d(chain[0], P)
                pid = f'pdf {chain[0]["seqno"]}'
            else:
                pts = [chain[0]["items"][0][1]] + [dr["items"][0][2] for dr in chain]
                d = "M " + P(pts[0]) + " " + " ".join("L " + P(q) for q in pts[1:])
                pid = f'pdf {chain[0]["seqno"]}-{chain[-1]["seqno"]}'
            if not d:
                continue
            if mode == "fill":
                paths.append(f'<path id="{pid}" d="{d} Z" fill="{INK}" fill-rule="nonzero"/>')
            else:
                paths.append(f'<path id="{pid}" d="{d}" {path_attrs(chain[0], ink)}/>')
        groups[name] = paths
        parts.extend(paths)
        parts.append("</g>")
    parts.append("</svg>")
    svg = "\n".join(parts)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"layers-{F}.svg"
    out.write_text(svg, encoding="utf-8")
    total = sum(len(v) for v in groups.values())
    counts = ", ".join(f"{n}={len(groups[n])}" for n, _ in KEEP)
    print(f"floor {F}: page {fd['page']} bbox {fd['bbox']} -> {f(W)}x{f(H)} px, {total} paths "
          f"({raw_total} PDF objects{', chained segments' if chain else ''}{', ink' if ink else ', PDF colors'}), {len(svg.encode('utf-8')) // 1024} KB -> {out}")
    print(f"  by layer: {counts}")
    if total > 3000:
        print(f"  !!! > 3000 paths ({total}): report to the reviewer, do not cut with a heuristic")
    return total


def main():
    args = sys.argv[1:]
    inventory = "--inventory" in args
    chain = "--no-chain" not in args
    ink = "--pdf-colors" not in args
    out_dir = OUT
    if "--out" in args:
        out_dir = Path(args[args.index("--out") + 1])
    floors = [int(a) for a in args if a.isdigit()]
    if not floors:
        print(__doc__); sys.exit(1)
    if not PDF.exists():
        sys.exit(f"PDF not found: {PDF} (set \"pdf\" in {fpconfig.CONFIG_PATH})")
    doc = pymupdf.open(PDF)
    for F in floors:
        build_floor(doc, F, out_dir, inventory, chain, ink)


if __name__ == "__main__":
    main()
