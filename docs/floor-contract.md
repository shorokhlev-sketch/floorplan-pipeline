# Floor contract v4: three layers, three owners

Decision of the reviewer, 2026-09-21, during the floor plan session. It applies to the first project and to any project after it.

## Symptoms this closes

1. Balconies were not part of the hover selection. `unit_outline()` merged a unit with its balcony at a 0.6 pt tolerance, but the wall between them is 1.4 to 2.9 pt. The result was a MultiPolygon and only the unit outline was used (161 of 274 balconies lost). At the time the floor data had one balcony polygon per unit, 274 in all; the PDF area layer used from the next day gives 349 balcony polygons, because 67 units have two or three.
2. White gap between neighbours. The polygons in `floor-F.json` are the inner faces of the walls, so a shared wall (2.8 pt) belongs to nobody.
3. Bearing elements missing in places. Columns and bearing walls (PDF cluster `c19`, pink fill `#fd8379`) were drawn in an underlay clipped by "minus units and balconies". A pylon that sticks into a room was cut, and in a wall shared by two units no final drawing drew it.
4. Fill not uniform. The unit in one color, the balcony in the same color at 50 %, the wall white.

## Three sources

| What | Source | Who does NOT take part |
|---|---|---|
| **One floor** (outline, all walls, core, columns) | the floor page of the PDF, clusters of `floor-F.json`: `c08` (grey walls `#6a6a6a`), `c19` (bearing `#fd8379`), doors `c00`, mullions `c21`, stairs, lifts and railings as in `underlay_pdf` | Figma |
| **Borders between units** | computed: raster Voronoi over the PDF polygons (below); the border is the middle of the shared wall | nobody draws them |
| **Uniform unit** | one ownership cell = one polygon = one fill = one tone = one hover object; the balcony inside the cell in the same tone, plus its own sub-polygon for the area label | the Figma finals only add the interior on top |

Note: `c22` (`#ff0000`, 7 x 7 pt squares) and `c18` (`#df0000`) are red icon frames (fire cabinets and similar), NOT columns. Columns and bearing walls are only `c19` (plus `c08` as normal walls).

## Ownership cells (`floor_cells`)

Input: `fd["units"][n]["poly"]`, `fd["units"][n].get("balcony")`, walls = all paths of `c08` and `c19` (pt, `floor-F.json` coordinates). Parameters in `style.json -> floor_render.cells`: `res_pt` 0.25, `public_open_pt` 1.2, `max_dist_pt` 4.0, `simplify_pt` 0.15.

Algorithm (numpy + scipy.ndimage, grid of `res_pt` over the floor bbox `fd["w"] x fd["h"]`, polygons rasterized with PIL.ImageDraw):

1. Seeds: each unit gets label `i` from its `poly`; its balcony gets the same label `i` (the balcony mask is kept separately for the sub-polygon). Walls `W` = mask of `c08` and `c19`.
2. Public space `P` = not (units or balconies or W), then `binary_opening` with a disk of radius `public_open_pt` (removes the 2 pt slivers of door openings; the corridor and "outside the building" stay). `P` gets label 0 and is a seed too.
3. `distance_transform_edt` over the "not a seed" mask with `return_indices=True`: every cell gets the label of the nearest seed; cells farther than `max_dist_pt` from any seed stay unlabeled (-1).
4. Cell of unit `i` = all pixels with label `i`. Polygonize: contour along cell edges, or `shapely` union of squares, whichever is simpler and more robust. Holes are dropped (a column inside a unit is closer to that unit, so it is the unit's), the largest polygon is kept, then `simplify(simplify_pt)`.
5. Output per unit: `cell` (pt), `balcony` (raw PDF polygon, as before), `poly` (as before).

Properties that must hold (checked on floor 2 and written to the report):

- for every unit with a balcony, area of `cell` >= area of `poly` + 0.9 x area of `balcony`;
- for every pair of neighbours, `cell_i` and `cell_j` overlap by less than 0.01 pt2 and are less than 0.05 pt apart along the shared wall;
- no cell enters the corridor: `cell` and `P` (after opening) do not overlap.

## Floor assembly (`plan-floor-assemble.py`, default mode = v4)

Layer order, bottom to top:

1. Paper.
2. **Cell fill**: one `<polygon points=cell fill=NEUTRAL_FLOOR>` per unit. `poly` and `balcony` are NOT drawn as separate polygons with `BALCONY_ALPHA` any more.
3. **Core underlay** as before (`underlay_pdf`: doors `c00`, mullions `c21`, stairs, railings, lifts) with the same clipPath "minus units and balconies". `c08` and `c19` are REMOVED from it; they move to layer 5.
4. **Unit interiors** from the finals (`dr.instance(...)`, `WALLS_ONLY`), as before, including the `pdf_balcony` logic for derived units.
5. **Floor structure**: `c08` and `c19` filled with `ink`, WITHOUT a clipPath, on top of everything. Separate group `<g id="structure">`.
6. Labels: the unit label as before ("1BR 4 / 88.2 m2"); **balcony**: a new label `fmt_area(units_db[n]["balcony"]) + " m2"` in `font`, size `BALCONY_LABEL_PX` (30 in this contract; since 2026-09-22 equal to `LABEL_PX` = 46, one size for all floor labels), color `muted`, at the pole of inaccessibility of the balcony polygon (`pole_of_inaccessibility`); if the balcony is narrower than 12 pt, skip it and write it to the report. Offices as before.

Output `units_out[n]`: `poly`, `balcony`, `outline` = **cell** (px, x K), `tone`, `label_living`. The front end (`js/plans.js`) reads `outline`; do not change it.

Keep the old behaviour (polygon + balcony at 0.5, underlay with `c08`/`c19` inside the clip) behind `--legacy-fill` for comparison. Do not delete it.

## What NOT to do

- Do not touch the finals, Figma, `final-svg/`, `floor-F.json`, `units.json`.
- Do not run `--all` before the reviewer accepted floor 2: only `--floor 2`.
- Do not change the front end, unless the pilot is invisible without it (then a minimal change, reported separately).
- Git: every logical change is its own commit with "what and why", no push.
