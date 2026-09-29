#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/unit-pdf.py — генерирует по одному PDF на каждую из 279 квартир проекта:
титул (тип, №, этаж, общая/жилая/балкон) + планировка базового типа
(assets/plans/unit-<base>.png, base = PLANS_DATA.units[N].base из data/plans.js).

Результат: site/assets/plans/pdf/unit-<номер>.pdf (279 файлов).
PIL есть, reportlab нет -> собираем страницу как RGB-изображение и сохраняем
через Image.save(path, "PDF", resolution=150.0) (одностраничный PDF).

Запуск:
    ROOT/.venv/bin/python tools/unit-pdf.py [--only 201,404] [--force]
"""
import argparse
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

HERE = Path(__file__).resolve().parent            # .../tools
ROOT = fpconfig.WORK
SITE = ROOT / "site"
PLANS_DIR = SITE / "assets" / "plans"
OUT_DIR = PLANS_DIR / "pdf"

PAGE_W, PAGE_H = 1240, 1754  # A4 @ ~150dpi
MARGIN = 90
INK = (22, 44, 37)          # #162C25 - primary color of the site
MUTED = (121, 121, 121)

ROOMS_RU = {"studio": "Студия", "1br": "1-комнатная", "2br": "2-комнатная", "3br": "3-комнатная"}


def load_json_var(path, varname):
    txt = path.read_text(encoding="utf-8")
    m = re.match(r"^\s*window\." + re.escape(varname) + r"\s*=\s*(.*?);?\s*$", txt, re.S)
    if not m:
        raise SystemExit(f"не нашёл window.{varname} в {path}")
    return json.loads(m.group(1))


def load_font(size, bold=False):
    candidates = (
        ["/System/Library/Fonts/Supplemental/Georgia Bold.ttf", "/System/Library/Fonts/Supplemental/Georgia.ttf"]
        if bold else
        ["/System/Library/Fonts/Supplemental/Georgia.ttf"]
    ) + [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            continue
    return ImageFont.load_default()


def fmt_area(v):
    return f"{v:.1f}".replace(".", ",")


def build_pdf(unit, base_png, out_path):
    page = Image.new("RGB", (PAGE_W, PAGE_H), "#FFFFFF")
    dr = ImageDraw.Draw(page)

    f_title = load_font(46, bold=True)
    f_h2 = load_font(26)
    f_label = load_font(20)
    f_val = load_font(24, bold=True)
    f_foot = load_font(16)

    room = ROOMS_RU.get(unit["type"], unit["type"])
    dr.text((MARGIN, 70), fpconfig.CFG.get("project_name", ""), font=f_foot, fill=MUTED)
    dr.text((MARGIN, 110), f"{room} • {fmt_area(unit['total'])} м²", font=f_title, fill=INK)
    dr.text((MARGIN, 175), f"этаж {unit['floor']} | юнит {unit['number']}", font=f_h2, fill=MUTED)
    dr.line([(MARGIN, 225), (PAGE_W - MARGIN, 225)], fill=(230, 230, 230), width=2)

    # планировка базового типа
    plan_top = 255
    plan_h = 1120
    plan_w = PAGE_W - 2 * MARGIN
    if base_png.exists():
        img = Image.open(base_png).convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)
        img = bg.convert("RGB")
        s = min(plan_w / img.width, plan_h / img.height)
        nw, nh = max(1, int(img.width * s)), max(1, int(img.height * s))
        img = img.resize((nw, nh), Image.LANCZOS)
        x = MARGIN + (plan_w - nw) // 2
        y = plan_top + (plan_h - nh) // 2
        page.paste(img, (x, y))
    else:
        dr.text((MARGIN, plan_top + plan_h // 2), "план недоступен", font=f_h2, fill=MUTED)

    # таблица площадей внизу
    rows = [
        ("Общая площадь, м²", fmt_area(unit["total"])),
        ("Жилая площадь, м²", fmt_area(unit["living"])),
        ("Балкон, м²", fmt_area(unit["balcony"])),
    ]
    ty = plan_top + plan_h + 20
    dr.line([(MARGIN, ty), (PAGE_W - MARGIN, ty)], fill=(230, 230, 230), width=2)
    ty += 24
    col_w = (PAGE_W - 2 * MARGIN) // 3
    for i, (label, val) in enumerate(rows):
        cx = MARGIN + i * col_w
        dr.text((cx, ty), label, font=f_label, fill=MUTED)
        dr.text((cx, ty + 32), val, font=f_val, fill=INK)

    page.save(out_path, "PDF", resolution=150.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="список номеров квартир через запятую (для отладки)")
    ap.add_argument("--force", action="store_true", help="перегенерировать, даже если PDF уже есть")
    args = ap.parse_args()

    data = load_json_var(SITE / "data" / "units.js", "DATA")
    plans = load_json_var(SITE / "data" / "plans.js", "PLANS_DATA")
    units_by_number = {u["number"]: u for u in data["units"]}
    only = set(args.only.split(",")) if args.only else None

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    done, skipped, missing_plan = 0, 0, []
    for number, unit in sorted(units_by_number.items()):
        if only and number not in only:
            continue
        out_path = OUT_DIR / f"unit-{number}.pdf"
        if out_path.exists() and not args.force:
            skipped += 1
            continue
        pdata = plans.get("units", {}).get(number)
        base = pdata["base"] if pdata else number
        base_png = PLANS_DIR / f"unit-{base}.png"
        if not pdata or not base_png.exists():
            missing_plan.append(number)
        build_pdf(unit, base_png, out_path)
        done += 1

    print(f"сгенерировано: {done}, пропущено (уже есть): {skipped}", file=sys.stderr)
    if missing_plan:
        print(f"без картинки (титул без плана): {', '.join(missing_plan)}", file=sys.stderr)


if __name__ == "__main__":
    main()
