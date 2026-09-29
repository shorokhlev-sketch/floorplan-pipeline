#!/usr/bin/env python3
"""Contact sheet of all unique unit shapes (mirror-invariant groups from plan-studio/unit-shape-groups.json).

Round 18: the unit PNGs are plan-only (no caption baked in any more - see render_unit), so
this script draws the per-card title/area caption itself, at a FIXED pixel size identical on
every card regardless of the unit's own footprint size (that was the whole point of moving
the caption out of the per-unit image in the first place)."""
import json, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig
ROOT = fpconfig.WORK
PLANS = ROOT / "site/assets/plans"
groups = json.load(open(ROOT / "plan-studio/unit-shape-groups.json"))
db = {u["number"]: u for u in json.load(open(fpconfig.SCHEDULE))["units"]}
TYPE = {"studio": "Студия", "1br": "1 спальня", "2br": "2 спальни", "3br": "3 спальни"}
TYPE_TITLE = {"studio": "Студия", "1br": "1-комн.", "2br": "2-комн.", "3br": "3-комн."}
COLS, CELL_W, CELL_H, PAD, CAP = 7, 420, 560, 28, 78
rows = -(-len(groups) // COLS)
W = PAD + COLS * (CELL_W + PAD); H = 120 + rows * (CELL_H + CAP + PAD)
sheet = Image.new("RGB", (W, H), "#FFFFFF"); d = ImageDraw.Draw(sheet)


def font(sz):
    for p in (
        Path.home() / "Library/Fonts/InstrumentSerif-Regular.ttf",
        Path("/Library/Fonts/InstrumentSerif-Regular.ttf"),
        Path.home() / "Library/Fonts/Instrument Serif.ttf",
        Path("/Library/Fonts/Instrument Serif.ttf"),
        Path("/System/Library/Fonts/Supplemental/Georgia.ttf"),
        Path("/Library/Fonts/Georgia.ttf"),
    ):
        try:
            return ImageFont.truetype(str(p), sz)
        except Exception:
            pass
    return ImageFont.load_default()


F_T, F_H, F_S = font(44), font(26), font(20)
F_TITLE, F_AREA = font(34), font(28)  # per-card caption, identical on every card
INK = "#182E46"
d.text((PAD, 30), f"{fpconfig.CFG.get('project_name', 'Project')} — типовые планировки: {len(groups)} уникальных контуров (зеркальные объединены), {sum(map(len, groups))} квартир", fill=INK, font=F_T)
for k, g in enumerate(groups):
    rep = g[0]
    floors = sorted({int(n[:-2]) for n in g})
    types = sorted({db.get(n, {}).get("type", "?") for n in g})
    areas = sorted({db[n]["total"] for n in g if n in db})
    col, row = k % COLS, k // COLS
    x = PAD + col * (CELL_W + PAD); y = 120 + row * (CELL_H + CAP + PAD)
    d.rectangle([x, y, x + CELL_W, y + CELL_H + CAP], outline="#D9DEE5", width=2)
    CAPTION_H = 110  # reserved band for title/area; the plan is fitted BELOW it, never under it
    img_top = y + CAPTION_H
    img_area_h = CELL_H - CAPTION_H - 12
    p = PLANS / f"unit-{rep}.png"
    if p.exists():
        im = Image.open(p).convert("RGB")
        s = min((CELL_W - 24) / im.width, img_area_h / im.height)
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
        img_x = x + (CELL_W - im.width) // 2
        img_y = img_top + (img_area_h - im.height) // 2
        sheet.paste(im, (img_x, img_y))

    # Per-unit caption: title + area + a vertical/horizontal bracket line, drawn at a FIXED
    # size in sheet pixels - never scaled by the plan image's own fit-to-cell factor, so a
    # 127 m2 unit's caption reads exactly the same size as a 34 m2 studio's.
    rep_info = db.get(rep, {})
    title = TYPE_TITLE.get(rep_info.get("type"), "")
    total = rep_info.get("total")
    area_str = f"{total:g} м²".replace(".", ",") if total is not None else ""
    lx, ly = x + 14, y + 20 + 34
    d.text((lx, y + 20), title, fill=INK, font=F_TITLE)
    ly2 = y + 20 + 34 + 6
    if area_str:
        d.text((lx, ly2), area_str, fill=INK, font=F_AREA)
        ly2 += 28 + 4
    corner_bottom = ly2 + 4
    corner_right = lx + 90
    d.line([(lx - 6, y + 14), (lx - 6, corner_bottom)], fill=INK, width=2)
    d.line([(lx - 6, corner_bottom), (corner_right, corner_bottom)], fill=INK, width=2)

    d.rectangle([x, y + CELL_H, x + CELL_W, y + CELL_H + CAP], fill="#F3EFE8")
    a = f"{areas[0]:g}" if len(areas) == 1 else f"{areas[0]:g}–{areas[-1]:g}" if areas else "?"
    fl = f"эт. {floors[0]}" if len(floors) == 1 else f"эт. {floors[0]}–{floors[-1]}"
    d.text((x + 14, y + CELL_H + 8), f"{k+1}. {' / '.join(TYPE.get(t, t) for t in types)} · {a} м²", fill=INK, font=F_H)
    d.text((x + 14, y + CELL_H + 44), f"{len(g)} кв. · {fl} · напр. {rep}", fill="#6B7A90", font=F_S)
out = ROOT / "plan-studio/v2/unit-types-sheet.png"
sheet.save(out, optimize=True); print(out, sheet.size)
