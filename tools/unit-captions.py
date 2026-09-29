#!/usr/bin/env python3
"""manifest.json финалов → site/data/captions.js: позиция ярлыка «Тип N / площадь» внутри плана квартиры
(bbox старого «Заголовка» в Figma, доли от ширины/высоты картинки) — сайт кладёт ярлык поверх PNG (ревьюер 2026-09-22: «нет ярлыка
с уголком, площадью и комнатами», как у референса — внутри плана). fs = кегль как доля ширины (две строки в bbox h)."""
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig
ROOT = fpconfig.WORK
m = json.loads((ROOT / "plan-studio/final-svg/manifest.json").read_text())
out = {}
for base, mu in m["units"].items():
    t = mu.get("title"); w, h = mu["w"], mu["h"]
    if not t: continue
    out[base] = {"tx": round(t["x"] / w, 4), "ty": round(t["y"] / h, 4), "fs": round(t["h"] / 2 / 1.15 / w, 4)}
p = ROOT / "site/data/captions.js"
p.write_text("window.PLAN_CAPTIONS = " + json.dumps(out, separators=(",", ":")) + ";\n")
print(f"{p}: {len(out)} баз")
