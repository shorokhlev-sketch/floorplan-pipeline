# AGENT: export cleaned floors from Figma and diff them

## Rules

- Read only. Do not change Figma.
- One `download_assets` per floor. Do not paste the asset lists into the report.
- Keep the machine version `layers-F.svg` untouched; the diff needs it.

## Inputs

- Floors: `<F1, F2, ...>`
- Frame of floor F: `<node id or name>`
- Config: `FLOORPLAN_CONFIG=<path>`

## Code

```bash
# 1. download_assets(nodeId=<frame of F>, format=svg) returns a URL
curl -s "<url>" -o "<WORK>/tmp/figma-export-<F>.svg"
# 2. clean SVG + diff by id against layers-F.svg
uv run tools/figma-floor-export.py <F> "<WORK>/tmp/figma-export-<F>.svg"
# 3. picture
uv run tools/render-check.py --svg "<WORK>/plan-studio/v3/figma/floor-<F>-manual.svg"
```

## Expected ranges

- "red marks removed" is a small number (the reviewer's circles). Above 20: report.
- Export scale below 1 when the frame is wider than 4096 px. A frame size warning means the frame was stretched: report it.
- The diff is the reviewer's work: report it as it is, the largest moves included.

## Report

| Floor | paths | deleted (by layer) | moved | recolored | added | red marks | largest moves |
|---|---|---|---|---|---|---|---|
| F | | | | | | | |
