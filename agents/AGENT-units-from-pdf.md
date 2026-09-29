# AGENT: unit and balcony polygons from the area layer

## Rules

- Dry run first: `--dry` writes nothing.
- No fitting. The floor files get exactly the loops from the PDF; everything doubtful goes to the report.
- Do not relax tolerances to make a floor pass. Report instead.

## Inputs

- Floors: `<F1, F2, ...>` or `--all`
- Area schedule: path in the config (`schedule`)
- Config: `FLOORPLAN_CONFIG=<path>`

## Code

```bash
uv run tools/floor-index.py                     # only for a new project: page, bbox, texts per floor
uv run tools/plan-units-from-pdf.py --all --dry
# after the dry table is clean:
uv run tools/plan-units-from-pdf.py --all
uv run tools/render-check.py <F> --units
```

## Expected ranges

- matched = units in the schedule on every floor.
- area check: all units within 3 % (living) and 5 % (balcony); in practice within 0.05 m2.
- balconies found = balconies attached (+ offices on office floors only).

## Report

Paste the table the tool prints, then the "details" block for every floor with problems.
