# AGENT: import PDF layers into Figma floor frames

## Rules

- Before the first import: snapshot the page. `use_figma`: `const p = figma.currentPage.clone(); p.name = 'Floors before <date> layers';`. Never edit the snapshot.
- Touch only the frames of your floors: `<floor list>`. Do not touch other pages.
- One import per `use_figma` call. Calls over 2 minutes are cut by the MCP.
- Uploads one at a time. Parallel `upload_assets` created duplicate frames once.
- An error on a floor: skip it, write it in the report, do not fix it with a heuristic.

## Inputs

- Floors: `<F1, F2, ...>`
- Frame of floor F: `<node id rule, e.g. frame named "Floor F" on page "Floors">`
- Config: `FLOORPLAN_CONFIG=<path>`

## Code

1. Layer SVG:

```bash
uv run tools/plan-pdf-layers.py <F>
```

Expect: one line per floor with the path count and one line "by layer". Copy both into the report.

2. Upload: `upload_assets(count=1)` returns `submitUrl`, then

```bash
curl -X POST "<submitUrl>" -F "file=@<WORK>/plan-studio/v3/figma/layers-<F>.svg;type=image/svg+xml"
```

The answer has `placedOnNodeId`. The node lands on the CURRENT page of the app.

3. Place it (`use_figma`):

```js
const imp = await figma.getNodeByIdAsync('<placedOnNodeId>');
const page = figma.root.children.find((p) => p.name === '<Floors page>');
await figma.setCurrentPageAsync(page);
const fr = page.children.find((n) => n.name === 'Floor <F>');
for (const old of fr.children.filter((n) => n.name === 'outline-layers')) old.remove();
fr.appendChild(imp); imp.x = 0; imp.y = 0; imp.name = 'outline-layers'; imp.fills = []; imp.clipsContent = false;
return { id: imp.id, w: imp.width, layers: imp.children.map((g) => [g.name, g.children.length]) };
```

## Expected ranges

- `imp.width` equals the frame width within 1 px (the SVG covers the whole frame, no crop).
- Paths per floor: the same order of magnitude on every floor (real tower: 1865 to 1921). Above 3000: report, do not cut.
- Per-layer counts stable from floor to floor. A layer at 0 or at double: hold.

## Report

| Floor | group id | width | paths | per layer | status |
|---|---|---|---|---|---|
| F | | | | | ok / held: reason |
