#!/usr/bin/env python3
"""PDF архитектора → SVG векторами 1:1 для Figma, без интерпретации.

.venv/bin/python tools/plan-pdfvec.py [unit ...]
Для каждой квартиры берёт страницу этажа (plan-studio/data/floor-F.json: page, bbox), все графические элементы
PyMuPDF page.get_drawings(), чей прямоугольник пересекает кадр квартиры (bbox документа v3), и пишет
plan-studio/v3/figma/pdf-N.svg: один <path> на элемент PDF (id = "pdf <seqno>"), координаты = (PDF − кадр) × 7,05 px/pt,
цвета/толщины/пунктир как в PDF (толщина 0 = волосяная → 0.7 px). Ничего не сливается и не классифицируется.
"""
import json, sys
from pathlib import Path
import pymupdf
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
PLANS = ROOT / "plan-studio/v3/plans"
DATA = ROOT / "plan-studio/data"
OUT = ROOT / "plan-studio/v3/figma"
PDF = fpconfig.PDF
K = 7.05
HAIR = 0.7


def f(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def rgb(c):
    if c is None:
        return "none"
    return "#%02X%02X%02X" % tuple(int(round(x * 255)) for x in c[:3])


def main():
    units = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not units:
        units = [u["unit"] for u in json.loads((PLANS / "index.json").read_text())["units"]]
    doc = pymupdf.open(PDF)
    pages = {}
    OUT.mkdir(parents=True, exist_ok=True)
    for u in units:
        d = json.loads((PLANS / f"unit-{u}.json").read_text())
        fd = json.loads((DATA / f"floor-{d['floor']}.json").read_text())
        fx0, fy0 = fd["bbox"][0], fd["bbox"][1]
        bb = d["bbox"]
        clip = pymupdf.Rect(fx0 + bb["x0"], fy0 + bb["y0"], fx0 + bb["x0"] + bb["w"], fy0 + bb["y0"] + bb["h"])
        pno = fd["page"] - 1          # в floor-F.json страница 1-based (как в tools/truth-compare.py)
        if pno not in pages:
            pages[pno] = doc[pno].get_drawings()
        drawings = pages[pno]
        W, H = bb["w"] * K, bb["h"] * K

        def P(p):
            return f((p.x - clip.x0) * K) + " " + f((p.y - clip.y0) * K)

        paths = []
        n_items = 0
        for dr in drawings:
            r = dr["rect"]
            if r.x1 < clip.x0 or r.x0 > clip.x1 or r.y1 < clip.y0 or r.y0 > clip.y1:
                continue
            segs = []
            cur = None
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
                n_items += 1
            if not segs:
                continue
            if dr.get("closePath"):
                segs.append("Z")
            attrs = []
            t = dr["type"]
            fill = rgb(dr.get("fill")) if "f" in t else "none"
            attrs.append(f'fill="{fill}"')
            if fill != "none":
                attrs.append('fill-rule="evenodd"' if dr.get("even_odd") else 'fill-rule="nonzero"')
                if dr.get("fill_opacity") not in (None, 1.0):
                    attrs.append(f'fill-opacity="{f(dr["fill_opacity"])}"')
            if "s" in t and dr.get("color") is not None:
                w = dr.get("width") or 0
                attrs.append(f'stroke="{rgb(dr["color"])}" stroke-width="{f(max(w * K, HAIR))}"')
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
            paths.append(f'<path id="pdf {dr["seqno"]}" d="{" ".join(segs)}" {" ".join(attrs)}/>')
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{f(W)}" height="{f(H)}" viewBox="0 0 {f(W)} {f(H)}">\n'
               f'<g id="PDF-векторы">\n' + "\n".join(paths) + "\n</g>\n</svg>")
        (OUT / f"pdf-{u}.svg").write_text(svg)
        print(f"{u}: page {pno} (json {fd['page']}) clip {clip} → {len(paths)} paths, {n_items} items, {len(svg) // 1024} KB")


if __name__ == "__main__":
    main()
