# AGENT: render check before anything ships

## Rules

- Look at every PNG. A run is accepted by the picture, not by the log.
- Do not fix anything. Describe what is wrong and where.
- No screenshots to the reviewer; the answer is text.

## Inputs

- Floors: `<F1, F2, ...>`
- What changed in this run: `<one line>`

## Code

```bash
uv run tools/render-check.py <F1> <F2> --units
```

Then open each `floor-F-check.png`.

## What to look for

- Walls continuous, columns black (white columns on white paper = the classic bug).
- No large black areas where there should be an outline (missing `fill="none"`).
- Door arcs are arcs, not zigzags (chaining direction).
- Every unit tinted, every balcony tinted and touching its unit.
- Labels inside their unit; the area matches the schedule.

## Report

| Floor | time, s | ink % | defects (where, what) |
|---|---|---|---|
| F | | | none / list |
