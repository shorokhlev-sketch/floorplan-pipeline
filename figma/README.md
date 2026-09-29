# Figma scripts (use_figma)

Scripts for the Figma MCP `use_figma` tool (Figma Plugin API, top-level `await`, `return` gives the result). They were run on one real file; node names are the Russian layer names of that file (`Чертёж` = drawing, `стена` = wall, `окно балкона` = balcony window band, `коробка` = door frame, `створка` = sash). Placeholders like `__DATA__` are filled by the main session before the call; the comment next to each says what goes there.

| Script | What it does |
|---|---|
| `scripts/01-final-pipeline-201-203.js` | first version of the unit "final" pipeline: restyle PDF vectors by rule, floor fill, labels |
| `scripts/09-final-pipeline-v2.js` | unit final pipeline v2: cleanup frame to final frame, walls, window wedges, doors with frames, leaf and dashed arc, glazing bands, railings, labels. Only VECTOR nodes. One unit per call |
| `scripts/13-bands-like-pdf.js` | redraws balcony glazing bands exactly as in the PDF (bricks, sashes, mullions), idempotent, 5 to 7 units per call |
| `scripts/10-import-new-type.js` | imports a new unit type from `pdf-N.svg`, keeps only the listed `pdf` seqno ranges, applies wall thinning from `tools/plan-wallthin.py` |
| `scripts/07-wall-edge-lines.js` | edge lines along walls of one frame |
| `scripts/make-baseline.js` | hidden, locked clone of a frame: the snapshot before a manual review |
| `scripts/diff-vs-baseline.js` | diff of the frame against the baseline: removed, added, moved and resized nodes |
| `scripts/one-off/*` | patches for one unit during the rule-finding phase; unlike the scripts above they keep that unit's coordinates (frame px) and node ids of that file (02 takes the outlines as placeholders). Kept as a record of how rules 7 to 10 were derived |

The floor-level flow (import layer SVGs into frames, batch rules, export and diff) is in `agents/` and `docs/harness.md`.
