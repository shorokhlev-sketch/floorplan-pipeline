"""End-to-end test on the synthetic sample: PDF with OCG layers -> floor files -> layer SVG ->
unit polygons -> render check -> manual edit diff by id. Runs in a temp workspace, no network."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

REPO = Path(__file__).resolve().parents[1]
M_PER_PT = 0.0705


def area_m2(pts):
    s = 0.0
    for i in range(len(pts)):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % len(pts)]
        s += ax * by - bx * ay
    return abs(s) / 2.0 * M_PER_PT ** 2


@pytest.fixture(scope="module")
def ws(tmp_path_factory):
    work = tmp_path_factory.mktemp("work")
    cfg = json.loads((REPO / "config/sample.json").read_text())
    cfg.update(pdf=str(work / "sample.pdf"), workspace=str(work),
               schedule=str(work / "site-assets/data/units.json"))
    cfg_path = work / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    env = dict(os.environ, FLOORPLAN_CONFIG=str(cfg_path))

    def run(*args, check=True):
        r = subprocess.run([sys.executable, *map(str, args)], cwd=REPO, env=env,
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            raise AssertionError(f"{args} failed:\n{r.stdout}\n{r.stderr}")
        return r

    run("sample/make_sample_pdf.py", "--out", work)
    run("tools/floor-index.py")
    run("tools/plan-pdf-layers.py", 2, 3)
    run("tools/plan-units-from-pdf.py", "--all")
    return work, run


def test_pdf_has_ocg_layers(ws):
    work, _ = ws
    doc = pymupdf.open(work / "sample.pdf")
    names = {v["name"] for v in doc.get_ocgs().values()}
    assert {"A-WALL", "A-COLS", "A-GLAZ", "A-CORE", "A-AREA", "A-FURN", "A-DIMS"} <= names
    layers = {d.get("layer") for d in doc[0].get_drawings()}
    assert "A-FURN" in layers and "A-DIMS" in layers


def test_floor_index(ws):
    work, _ = ws
    idx = json.loads((work / "plan-studio/data/index.json").read_text())
    assert idx["floors"] == [2, 3]
    fd = json.loads((work / "plan-studio/data/floor-2.json").read_text())
    assert fd["page"] == 1 and len(fd["bbox"]) == 4
    assert any(t["str"] == "53.0" for t in fd["texts"])


def test_layer_svg_keeps_only_configured_layers(ws):
    work, _ = ws
    svg = (work / "plan-studio/v3/figma/layers-2.svg").read_text()
    groups = re.findall(r'<g id="([^"]+)">', svg)
    assert groups == ["A-WALL", "A-COLS", "A-GLAZ", "A-CORE"]
    assert "A-FURN" not in svg and "A-DIMS" not in svg and "A-AREA" not in svg
    ids = re.findall(r'<path id="([^"]+)"', svg)
    assert ids and all(i.startswith("pdf ") for i in ids)
    # ink mode: no hatch color, no orange, everything black or white
    colors = set(re.findall(r'(?:fill|stroke)="(#[0-9A-Fa-f]{6})"', svg))
    assert colors <= {"#000000", "#FFFFFF"}
    # the column drawn as 4 axis lines became one black fill
    cols = re.search(r'<g id="A-COLS">(.*?)</g>', svg, re.S).group(1)
    assert cols.count("<path") == 4 and cols.count('fill="#000000"') == 4
    # door arcs (40 segments each) are chained into one polyline per arc
    walls = re.search(r'<g id="A-WALL">(.*?)</g>', svg, re.S).group(1)
    assert len(re.findall(r'id="pdf \d+-\d+"', walls)) == 4


def test_units_match_schedule(ws):
    work, _ = ws
    sched = {u["number"]: u for u in json.loads((work / "site-assets/data/units.json").read_text())["units"]}
    seen = 0
    for F in (2, 3):
        fd = json.loads((work / f"plan-studio/data/floor-{F}.json").read_text())
        for n, u in fd["units"].items():
            assert "_check" not in u, (n, u.get("_check"))
            assert abs(area_m2(u["poly"]) - sched[n]["living"]) <= 0.05
            bal = sum(area_m2(b) for b in u.get("balconies", []))
            assert abs(bal - sched[n]["balcony"]) <= 0.05
            seen += 1
    assert seen == len(sched) == 9


def test_render_check(ws):
    work, run = ws
    r = run("tools/render-check.py", 2, "--units", check=False)
    if r.returncode != 0 and "Executable doesn't exist" in (r.stdout + r.stderr):
        pytest.skip("Chromium for Playwright is not installed (uv run playwright install chromium)")
    assert r.returncode == 0, r.stdout + r.stderr
    png = work / "plan-studio/v3/figma/floor-2-check.png"
    assert png.exists() and png.stat().st_size > 10_000


def test_diff_by_id(ws):
    work, run = ws
    run("sample/fake_figma_edit.py", 2)
    r = run("tools/figma-floor-export.py", 2, work / "tmp/figma-export-2.svg")
    out = r.stdout
    assert "deleted 3 {'A-CORE': 3}" in out
    assert "moved/changed 1 {'A-COLS': 1}" in out
    assert "recolored 1" in out and "added 0" in out
    manual = (work / "plan-studio/v3/figma/floor-2-manual.svg").read_text()
    assert 'stroke="#FF0000"' not in manual          # review marks never reach the site


def test_diff_mode_undoes_export_scale(ws):
    work, run = ws
    fig = work / "plan-studio/v3/figma"
    if not (fig / "floor-2-manual.svg").exists():
        run("sample/fake_figma_edit.py", 2)
        run("tools/figma-floor-export.py", 2, work / "tmp/figma-export-2.svg")
    # --diff against the scaled manual SVG and against the raw 4096 px export gives the same edits
    for edited in (fig / "floor-2-manual.svg", work / "tmp/figma-export-2.svg"):
        out = run("tools/figma-floor-export.py", "--diff", fig / "layers-2.svg", edited).stdout
        assert "deleted 3 {'A-CORE': 3}" in out and "moved/changed 1 {'A-COLS': 1}" in out, out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_plan_document_renders(tmp_path):
    r = subprocess.run(["node", "tools/plan-build.mjs", "--plans", "editor/plans", "--out", str(tmp_path), "--labels"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    svg = (tmp_path / "unit-901.svg").read_text()
    assert svg.startswith("<svg") and "26,8" in svg
