# AGENT: apply one rule to many floors in Figma

## Rules

- The rule comes from the diff of a floor the reviewer edited by hand (`figma-floor-export.py`). Do not invent rules.
- Dry run first, on the reference floor and on one neighbour. Apply only when both pass.
- Snapshot before the batch: `figma.currentPage.clone()` named `'Floors before <date> <rule name>'`.
- Touch only nodes the rule selects, only on your floors `<floor list>`.
- Anything out of the expected range goes to "held" with the node ids. Do not change it.

## Inputs

- Rule: `<one sentence: layer + geometry, e.g. "in layer A-CORE delete lines shorter than 12 px that touch a stair outline">`
- Reference floor and its manual edit list: `<F>`, `<ids from the diff>`
- Floors: `<F1, F2, ...>`

## Code

```js
// DRY = true: only return candidates. DRY = false: apply.
const DRY = true;
const FLOORS = [<F1>, <F2>];
const page = figma.root.children.find((p) => p.name === '<Floors page>');
await figma.setCurrentPageAsync(page);
const out = {};
for (const F of FLOORS) {
  const fr = page.children.find((n) => n.name === `Floor ${F}`);
  const g = fr.findOne((n) => n.name === 'outline-layers');
  const layer = g.children.find((n) => n.name === '<layer>');
  const cand = layer.children.filter((n) => /* the rule */ false);
  out[F] = { n: cand.length, ids: cand.slice(0, 50).map((n) => n.name) };
  if (!DRY) for (const n of cand) n.remove();
}
return out;
```

## Expected ranges

- Reference floor: the candidate names equal the reviewer's manual edits (same `pdf N` ids, nothing extra).
- Other floors: `<min>` to `<max>` candidates per floor. Out of range: hold that floor.

## Report

| Floor | candidates | applied | held | note |
|---|---|---|---|---|
| F | | | | |

Last line: snapshot page name, and "orphan nodes on the page: 0".
