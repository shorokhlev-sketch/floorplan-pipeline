# Harness: how the agents did the work

This is the method behind the pipeline, written after two weeks of unit plans and three days of floor plans on one real tower (25 floors, 279 apartments). The code is in `tools/`; this file explains how it was driven. One main model held the architecture and the acceptance. Smaller models (Sonnet, sometimes Opus) did the mechanical work from instruction files. A human reviewer looked only at pictures and the live site.

## 1. Rules, in order of weight

1. **Look for the structure of the source before writing heuristics.** For two weeks the vectors of the PDF were classified by color (grey = wall, orange = door, blue = mullion). The PDF had real OCG layers the whole time (`page.get_drawings()[i]["layer"]` in PyMuPDF). A layer filter left 1.9k objects per floor instead of 39k, with no color heuristic, and the reviewer accepted the result in one evening. Rule: before parsing any third-party file, inventory its metadata (layers, names, groups), then the geometry. `tools/plan-pdf-layers.py F --inventory` does this step.
2. **The reviewer edits by hand faster than we guess rules.** Every good batch of the week came from the reviewer's manual edits on one floor, not from our guesses: pictograms, stairs, stubs, facade panels, white pylons. "Human cleans one floor, diff, rule, dry run on a neighbour, batch" worked. "We invent a rule, show it, roll back" cost three rollbacks.
3. **Diff by object id is the main acceptance tool.** Every object carries `id="pdf <seqno>"`; the Figma SVG export keeps the id. A diff "deleted / moved / recolored / added" shows in seconds what the reviewer did, and the same diff becomes the rule. Without ids we would compare pictures.
4. **One source of walls per drawing.** Hybrids (unit finals glued into the PDF floor) gave seams and mismatches. One PDF layer set plus manual cleanup has no seams by construction.
5. **Polygons from the source beat our own geometry.** The architect's measured outlines (an OCG layer with single segments) matched the reviewer's walls on every floor, and the areas match the official schedule within 0.05 m2. Filling cells straight from those polygons beat a flood fill through door openings, which lost rooms.
6. **A script run is a picture, not a log.** Every run is rendered and looked at (Playwright, SVG to PNG, about 3 s) before it goes to Figma or the site. Three bugs of one week were obvious in the picture and invisible in the logs: white columns on white paper, a slab outline filled black because the wrapper had no `fill="none"`, segments chained without regard to direction. `tools/render-check.py` is this step.
7. **Snapshots at every stage are not bureaucracy.** A `page.clone()` snapshot before each Figma batch restored 156 stair nodes by name after a bad rule. A git commit per step and a server snapshot before each deploy do the same for code and site.

## 2. The loop for "do this everywhere"

1. The reviewer edits one floor by hand.
2. Export that floor, diff by id against the machine version (`tools/figma-floor-export.py F export.svg`). The output is a list per layer: what was deleted, moved, recolored, added.
3. State the rule in terms of the layer plus geometry (thickness, length, what it touches), not in terms of color.
4. **Dry run on the reference floor**: the candidate list must equal the reviewer's manual edits.
5. **Dry run on a neighbour floor**: the counters must fall into the expected range. Example of stable per-floor counters on the real tower: bearing wall objects 1064 to 1167, curtain wall objects 100 to 139, window wedges 111 to 141. Out of range means stop and ask, never "fix" with another heuristic. A layer SVG above 3000 paths is reported, not cut.
6. Snapshot: `page.clone()` named "<page> before <date> <stage>".
7. Batch: one `use_figma` call for light edits, or parallel agents by instruction file for heavy ones. Anomalies go to a "held" list with the question.
8. Control call: counters for every floor, no orphan nodes on the page.

## 3. Instruction files instead of chat prompts

An agent gets a file, not a paragraph. One file = one type of action (`agents/AGENT-*.md`). Each file has: the rules block first (backups, what not to touch), the inputs, ready code, one loop, the stop conditions and a report table to fill. Five Sonnet agents with five floors each closed 25 floors in about 2 minutes and returned counters that could be checked in one glance. Mechanical work never runs in the main context: 25 `download_assets` calls and 50-item candidate lists eat the context faster than expected.

## 4. Figma MCP quirks we hit twice

- The SVG export shrinks the frame to 4096 px on the long side. Read the scale from the frame rect, not from `4096 / W`: a frame stretched by hand moved the whole plan on the site.
- Ids in the export are UTF-8 bytes written as `&#N;`. `html.unescape` breaks them; decode bytes, then UTF-8.
- A group that becomes empty is deleted by Figma during the script; the reference goes stale.
- `upload_assets` from parallel agents created 13 duplicate frames with broken names. Uploads go one at a time.
- Review marks live anywhere: in a group, in the frame, on the page, in another frame. Find them by the red 14 px stroke, not by parent. The exporter drops them so they never reach the site.

## 5. Weak spots, honestly

- **Geometric rules cut too much.** A stair rule removed full-size steps because a break diagonal crosses them in the PDF. A "stub" rule almost deleted a 2 m wall. Only the dry run with a stop threshold saved it.
- **The site read other data than we edited.** Statuses lived in one file, fixes went to another. One data source for the site, generated by the assembler.
- **Data holes, not drawing holes.** Missing terrace polygons and cut polygons showed as white on the site while the drawing was clean. They were closed with the same layer method, not by hand.
- **Cells depend on the wall mask.** Ownership cells use walls from the PDF while the drawing comes from the reviewer's cleanup. If the cleanup drifts from the PDF, the cell borders drift too.

## 6. Pipelines we now run with closed eyes

**Floor ready to site (one floor, about 5 minutes):**

1. `download_assets` of the floor frame as SVG.
2. `uv run tools/figma-floor-export.py F export.svg` writes `floor-F-manual.svg` and prints the diff of the manual edits.
3. `uv run tools/plan-floor-assemble.py --floor F --source figma` writes the floor images and merges the floor into the site data.
4. Look at the PNG: every room and balcony filled, labels in place.
5. Commit, deploy with a server snapshot.

**New layer from the PDF:**

1. `uv run tools/plan-pdf-layers.py F --inventory` lists every layer in the floor bbox with its object count and whether the layer config keeps it.
2. Add it to the layer config (`config/layers.*.json`), with `drop_strokes` if its strokes are hatch or crosses.
3. Regenerate, render check, re-upload the floors by agents.

**Find what the reviewer circled:** nodes with a red 14 px stroke in the frame, in groups and on the page; under each, objects with more than 30 % overlap, with the `pdf N` name, layer, bbox and color. One call, answer as text, no screenshots.

## 7. Where each rule lives in the code

| Rule | Code |
|---|---|
| Inventory before parsing | `tools/plan-pdf-layers.py --inventory` |
| Layer filter instead of colors | `config/layers.*.json`, `tools/plan-pdf-layers.py` |
| Polygons from the source | `tools/plan-units-from-pdf.py` |
| Diff by id | `tools/figma-floor-export.py` (also `--diff a.svg b.svg` for any pair of layer SVG, manual SVG or raw export) |
| Render check | `tools/render-check.py` |
| Dry run with expected ranges | `plan-units-from-pdf.py --dry`, counters printed by every tool |
| Ownership cells and self checks | `tools/plan-floor-assemble.py`, `docs/floor-contract.md` |
| Instruction files | `agents/` |
| Snapshots in Figma | `figma/scripts/make-baseline.js`, `diff-vs-baseline.js` |
