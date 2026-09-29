"""Project configuration shared by every script in tools/.

The scripts were written against one real project and used hardcoded paths. In this
repository all of them resolve paths through this module instead:

    CODE       repository root (code, config, editor)
    WORK       workspace with the project data (floor JSON, layer SVGs, renders, site assets)
    PDF        the architect's PDF
    STYLE      render style (config/style.json by default)
    SCHEDULE   official area schedule (units.json: number, floor, living, balcony, ...)

Which config is used:
    1. env FLOORPLAN_CONFIG=<path to json>
    2. config/project.json            (your own project, git-ignored)
    3. config/project.example.json    (fallback, paths point to nothing until you add a PDF)

Relative paths in the config resolve against the repository root.

Workspace layout (kept from the original project so the big scripts work unchanged):
    WORK/plan-studio/data/floor-F.json, index.json   floor geometry, one file per floor
    WORK/plan-studio/v3/plans/unit-N.json             plan documents (editor/SCHEMA.md)
    WORK/plan-studio/v3/figma/layers-F.svg            floor SVG by PDF layers (plan-pdf-layers.py)
    WORK/plan-studio/v3/figma/floor-F-manual.svg        floor SVG after manual cleanup in Figma
    WORK/site/                                        site output (assets/plans, data/*.js)
    WORK/site-assets/data/units.json                  area schedule (SCHEDULE)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]


def _config_path() -> Path:
    env = os.environ.get("FLOORPLAN_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    own = CODE / "config" / "project.json"
    if own.exists():
        return own
    return CODE / "config" / "project.example.json"


def _abs(p: str | None, default: str) -> Path:
    v = Path(os.path.expanduser(p or default))
    return v if v.is_absolute() else (CODE / v)


CONFIG_PATH = _config_path()
CFG: dict = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

WORK = _abs(CFG.get("workspace"), "work")
PDF = _abs(CFG.get("pdf"), "input/architect.pdf")
STYLE = _abs(CFG.get("style"), "config/style.json")
SCHEDULE = _abs(CFG.get("schedule"), str(WORK / "site-assets" / "data" / "units.json"))
LAYERS_PATH = _abs(CFG.get("layers"), "config/layers.example.json")

# drawing scale: 1 pt of the PDF sheet = CM_PER_PT cm in the building (1:200 sheet -> 7.05)
CM_PER_PT = float(CFG.get("cm_per_pt", 7.05))
M_PER_PT = CM_PER_PT / 100.0

# page -> floor mapping of the architect's PDF (1-based pages)
PAGES = CFG.get("pages", {})
PAGE_FIRST = int(PAGES.get("first", 1))
PAGE_LAST = int(PAGES.get("last", PAGE_FIRST))
FLOOR_FIRST = int(PAGES.get("first_floor", 1))

# font for captions rendered with PIL (optional; scripts fall back to a default font)
FONT = CFG.get("font")


def page_for_floor(floor: int) -> int:
    return PAGE_FIRST + (floor - FLOOR_FIRST)


def floor_for_page(page: int) -> int:
    return FLOOR_FIRST + (page - PAGE_FIRST)


def layers() -> dict:
    """OCG layer rules (see config/layers.example.json)."""
    return json.loads(LAYERS_PATH.read_text(encoding="utf-8"))


def fonts(*extra: str | Path) -> list[Path]:
    """Candidate font files for PIL captions: config 'font' first, then the given fallbacks."""
    out = []
    if FONT:
        out.append(_abs(FONT, FONT))
    out += [Path(os.path.expanduser(str(p))) for p in extra]
    return out
