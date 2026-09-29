# Agent instruction files

One file = one type of action. The main session copies a template, fills the placeholders (`<...>`), and hands the file to a worker agent (Sonnet for mechanics, Opus when judgment is needed). The worker reads only this file.

Every file has the same shape:

1. **Rules** first: backups before any change, what not to touch, when to stop.
2. **Inputs**: floors, paths, node ids.
3. **Code**: ready to run, no improvisation.
4. **Loop**: one pass per floor.
5. **Expected ranges**: counters that say "normal". Out of range = hold and report.
6. **Report**: a table the main session can check in one glance.

| File | Action |
|---|---|
| `AGENT-layer-import.md` | PDF layers to SVG, upload into the floor frames in Figma |
| `AGENT-rule-batch.md` | apply one derived rule to many floors in Figma, dry run first |
| `AGENT-floor-export.md` | export cleaned floors from Figma, diff by id, table |
| `AGENT-render-check.md` | render outputs to PNG and look at them before anything ships |
| `AGENT-units-from-pdf.md` | unit and balcony polygons from the area layer, check against the schedule |

Five workers with five floors each closed 25 floors in about 2 minutes. See `docs/harness.md` for the method.
