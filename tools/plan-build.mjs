#!/usr/bin/env node
// План v3: документы → SVG. node tools/plan-build.mjs [unit ...] [--labels] [--title] [--plans DIR] [--out DIR]
// Пишет WORK/plan-studio/v3/out/unit-N.svg (с мебелью) и unit-N-walls.svg (без). PNG делает tools/plan-png.py.
// Demo: node tools/plan-build.mjs --plans editor/plans --out sample/work/plan-out --labels
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { pathToFileURL } from 'node:url';
import { CODE, WORK, opt } from './fpconfig.mjs';

const args = process.argv.slice(2);
const PLANS = opt(args, '--plans', join(WORK, 'plan-studio/v3/plans'));
const OUT = opt(args, '--out', join(WORK, 'plan-studio/v3/out'));
mkdirSync(OUT, { recursive: true });

const { renderSVG } = await import(pathToFileURL(join(CODE, 'editor/render.js')).href);

const optVals = new Set(['--plans', '--out'].map((o) => args[args.indexOf(o) + 1]).filter(Boolean));
const flags = new Set(args.filter((a) => a.startsWith('--')));
let units = args.filter((a) => !a.startsWith('--') && !optVals.has(a));
if (!units.length) {
  const idx = JSON.parse(readFileSync(join(PLANS, 'index.json'), 'utf8'));
  units = idx.units.map((u) => u.unit);
}
const style = JSON.parse(readFileSync(join(CODE, 'editor/style.json'), 'utf8'));
const baseOpts = { labels: flags.has('--labels'), title: flags.has('--title') };

for (const u of units) {
  const file = join(PLANS, `unit-${u}.json`);
  if (!existsSync(file)) { console.log(`${u}: no document`); continue; }
  const doc = JSON.parse(readFileSync(file, 'utf8'));
  writeFileSync(join(OUT, `unit-${u}.svg`), renderSVG(doc, style, Object.assign({ furniture: true }, baseOpts)));
  writeFileSync(join(OUT, `unit-${u}-walls.svg`), renderSVG(doc, style, Object.assign({ furniture: false }, baseOpts)));
  console.log(`${u}: walls ${doc.walls.length}, openings ${doc.openings.length}, blocks ${doc.blocks.length}`);
}
