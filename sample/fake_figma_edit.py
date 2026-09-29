#!/usr/bin/env python3
"""Simulate a manual cleanup in Figma and its SVG export, for the diff-by-id step.

    uv run sample/fake_figma_edit.py F [--out PATH]

Reads WORK/plan-studio/v3/figma/layers-F.svg (the machine version) and writes what Figma's
download_assets would return after a reviewer cleaned the floor by hand:
    - 3 stair treads deleted (layer A-CORE)
    - 1 column moved 20 px right (layer A-COLS)
    - 1 window brick recolored black (layer A-GLAZ)
    - 1 thick red review mark left on the drawing (the exporter must drop it)
The export is shrunk to 4096 px on the long side, like Figma does, and wrapped in the frame
group structure the exporter expects. Output: WORK/tmp/figma-export-F.svg
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import fpconfig

NUM = re.compile(r"-?\d+(?:\.\d+)?")


def scale_d(d: str, s: float, dx: float = 0.0) -> str:
    """scale absolute M/L/C/Z path data; x values are shifted by dx before scaling"""
    out, i = [], 0
    for tok in re.findall(r"[MLCZ]|-?\d+(?:\.\d+)?", d):
        if tok in "MLCZ":
            out.append(tok)
            if tok in "MLC":
                i = 0
            continue
        v = float(tok)
        v = (v + dx) * s if i % 2 == 0 else v * s
        out.append(f"{v:.2f}")
        i += 1
    return " ".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("floor", type=int)
    ap.add_argument("--out")
    a = ap.parse_args()
    src = fpconfig.WORK / f"plan-studio/v3/figma/layers-{a.floor}.svg"
    svg = src.read_text(encoding="utf-8")
    W, H = map(float, re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg).groups())
    s = 4096 / max(W, H)

    layer = None
    treads_left, moved, recolored = 3, 0, 0
    lines = []
    for line in svg.splitlines():
        g = re.match(r'<g id="([^"]+)">', line)
        if g:
            layer = g.group(1)
            lines.append(line)
            continue
        m = re.match(r'<path id="([^"]+)" d="([^"]+)"(.*)/>', line)
        if not m:
            if line.startswith("</g>"):
                lines.append(line)
            continue
        pid, d, rest = m.groups()
        dx = 0.0
        if layer == "A-CORE" and treads_left and d.count("L") == 1:
            treads_left -= 1
            continue                                    # deleted by hand
        if layer == "A-COLS" and not moved:
            dx, moved = 20.0, 1                         # moved by hand
        if layer == "A-GLAZ" and not recolored and 'fill="#FFFFFF"' in rest:
            rest, recolored = rest.replace('fill="#FFFFFF"', 'fill="#000000"'), 1
        rest = re.sub(r'stroke-width="([\d.]+)"', lambda k: f'stroke-width="{float(k.group(1)) * s:.2f}"', rest)
        lines.append(f'<path id="{pid}" d="{scale_d(d, s, dx)}"{rest}/>')
    lines.append(f'<path d="M {400 * s:.1f} {300 * s:.1f} L {900 * s:.1f} {300 * s:.1f}" stroke="#FF0000" stroke-width="14"/>')

    out = Path(a.out) if a.out else fpconfig.WORK / f"tmp/figma-export-{a.floor}.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    ew, eh = W * s, H * s
    out.write_text(
        f'<svg width="{ew:.0f}" height="{eh:.0f}" viewBox="0 0 {ew:.0f} {eh:.0f}" fill="none" xmlns="http://www.w3.org/2000/svg">\n'
        f'<g id="Floor {a.floor}">\n'
        f'<rect width="{W:.1f}" height="{H:.1f}" transform="scale({s:.6f})" fill="white"/>\n'
        f'<g id="outline-layers">\n' + "\n".join(lines) + "\n</g>\n</g>\n<defs>\n</defs>\n</svg>\n",
        encoding="utf-8")
    print(f"{out}: scale {s:.4f}, deleted {3 - treads_left}, moved {moved}, recolored {recolored}, red marks 1")


if __name__ == "__main__":
    main()
