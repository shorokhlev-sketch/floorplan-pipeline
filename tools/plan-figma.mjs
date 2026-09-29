#!/usr/bin/env node
// План v3 → Figma (элементарные фигуры). node tools/plan-figma.mjs [unit ...] [--plans DIR] [--out DIR]
// Пишет plan-studio/v3/figma/unit-N.svg + manifest.json. 1 px = 1 см (7,05 px на pt плана).
//
// SVG (импортируется в Figma как векторы, id = имя слоя):
//   Пол — полигон на область; Стены-raw — заливки стен из PDF; Шахты; Мебель — ОДИН путь на каждую сущность PDF
//   (2-точечный путь → <line>). Плюс скрытый <rect>, в id которого лежит JSON примитивов (стены/проёмы/колонны в px),
//   из него скрипт в Figma (use_figma; в репозиторий не входит) создаёт настоящие LINE/ELLIPSE/RECTANGLE:
//   стена = отрезок по осевой со stroke = толщина; дверь = вырез (белый отрезок) + полотно + дуга (ellipse arc);
//   окно = вырез + 2 линии остекления; колонна = прямоугольник.
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { pathToFileURL } from 'node:url';
import { CODE, WORK, opt } from './fpconfig.mjs';
const PLANS = opt(process.argv, '--plans', join(WORK, 'plan-studio/v3/plans'));
const OUT = opt(process.argv, '--out', join(WORK, 'plan-studio/v3/figma'));
mkdirSync(OUT, { recursive: true });

const { wallGeom } = await import(pathToFileURL(join(CODE, 'editor/render.js')).href);
const style = JSON.parse(readFileSync(join(CODE, 'editor/style.json'), 'utf8'));

const K = 7.05;
const KIND_RU = { exterior: 'наружная', corridor: 'коридор', party: 'к соседу', partition: 'перегородка', balcony: 'балкон', railing: 'ограждение' };

const r1 = (v) => Math.round(v * 10) / 10;
const f = (v) => (Math.round(v * 100) / 100).toString();
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
const color = (tok) => (tok == null ? 'none' : (tok === 'ink' || tok === 'paper' || tok === 'floor' || tok === 'muted') ? style[tok] : tok);

const optVals = new Set(['--plans', '--out'].map((o) => process.argv[process.argv.indexOf(o) + 1]).filter(Boolean));
let units = process.argv.slice(2).filter((a) => !a.startsWith('--') && !optVals.has(a));
if (!units.length) units = JSON.parse(readFileSync(join(PLANS, 'index.json'), 'utf8')).units.map((u) => u.unit);

const manifest = { k: K, units: [] };

for (const u of units) {
  const file = join(PLANS, `unit-${u}.json`);
  if (!existsSync(file)) { console.log(`${u}: no document`); continue; }
  const doc = JSON.parse(readFileSync(file, 'utf8'));
  const bb = doc.bbox;
  const W = bb.w * K, H = bb.h * K;
  const P = (p) => [(p[0] - bb.x0) * K, (p[1] - bb.y0) * K];
  const pt = (p) => { const q = P(p); return f(q[0]) + ' ' + f(q[1]); };
  const poly = (pts) => 'M ' + pts.map(pt).join(' L ') + ' Z';
  const out = [];
  const g = (id, body) => { out.push(`<g id="${esc(id)}">`); body(); out.push('</g>'); };

  // ---- пол
  g('Пол', () => {
    for (const r of doc.areas) {
      const fill = r.kind === 'room' ? style.floor : style.paper;
      const name = (r.kind === 'room' ? 'Комната ' : r.kind === 'balcony' ? 'Балкон ' : 'Шахта ') + r.id + (r.name ? ' · ' + r.name : '');
      out.push(`<path id="${esc(name)}" d="${poly(r.poly)}" fill="${fill}" fill-rule="evenodd"/>`);
    }
  });

  // ---- примитивы (для API Figma): стены, проёмы, колонны
  const prims = { walls: [], openings: [], columns: [] };
  const geoms = {};
  for (const w of doc.walls) {
    const gm = wallGeom(doc, w); geoms[w.id] = gm;
    const t = w.t * K, h = w.t / 2;
    const a = P([gm.a[0] + gm.n[0] * h, gm.a[1] + gm.n[1] * h]);
    const b = P([gm.b[0] + gm.n[0] * h, gm.b[1] + gm.n[1] * h]);
    prims.walls.push([w.id, KIND_RU[w.kind] || w.kind || '', r1(a[0]), r1(a[1]), r1(b[0]), r1(b[1]), r1(t)]);
  }
  for (const o of doc.openings) {
    const gm = geoms[o.wall]; if (!gm) continue;
    const at = (s, off) => P([gm.a[0] + gm.u[0] * s + gm.n[0] * off, gm.a[1] + gm.u[1] * s + gm.n[1] * off]);
    const h = gm.t / 2;
    const rec = { k: o.kind, id: o.id, wall: o.wall, t: r1(gm.t * K), cut: [...at(o.pos, h), ...at(o.pos + o.width, h)].map(r1) };
    if (o.kind === 'door') {
      const hingeAt = o.hinge === 'b' ? o.pos + o.width : o.pos;
      const dir = o.hinge === 'b' ? -1 : 1;
      const swing = o.swing === 1 ? 1 : -1;
      const faceOff = swing === 1 ? gm.t : 0;
      const Hp = [gm.a[0] + gm.u[0] * hingeAt + gm.n[0] * faceOff, gm.a[1] + gm.u[1] * hingeAt + gm.n[1] * faceOff];
      const tip = [Hp[0] + gm.n[0] * swing * o.width, Hp[1] + gm.n[1] * swing * o.width];
      const E = [Hp[0] + gm.u[0] * dir * o.width, Hp[1] + gm.u[1] * dir * o.width];
      const Hx = P(Hp), Tx = P(tip), Ex = P(E);
      const z = (Tx[0] - Hx[0]) * (Ex[1] - Hx[1]) - (Tx[1] - Hx[1]) * (Ex[0] - Hx[0]);
      const sweep = z > 0 ? 1 : 0;                        // 1 = по часовой на экране от tip к E
      const aT = Math.atan2(Tx[1] - Hx[1], Tx[0] - Hx[0]), aE = Math.atan2(Ex[1] - Hx[1], Ex[0] - Hx[0]);
      let s = sweep ? aT : aE, e = sweep ? aE : aT;
      while (e < s) e += Math.PI * 2;
      rec.leaf = [...Hx, ...Tx].map(r1);
      rec.arc = { cx: r1(Hx[0]), cy: r1(Hx[1]), r: r1(o.width * K), s: Math.round(s * 1000) / 1000, e: Math.round(e * 1000) / 1000 };
    } else if (o.kind === 'window') {
      rec.lines = style.window.lines.map((k) => [...at(o.pos, gm.t * k), ...at(o.pos + o.width, gm.t * k)].map(r1));
    } else if (o.kind === 'sliding') {
      const half = o.width / 2, ov = style.sliding.overlap;
      rec.lines = [[...at(o.pos, gm.t * 0.35), ...at(o.pos + half + ov, gm.t * 0.35)].map(r1), [...at(o.pos + half - ov, gm.t * 0.65), ...at(o.pos + o.width, gm.t * 0.65)].map(r1)];
    }
    prims.openings.push(rec);
  }
  for (const c of doc.columns) { const q = P([c.x, c.y]); prims.columns.push([c.id, r1(q[0]), r1(q[1]), r1(c.w * K), r1(c.h * K)]); }
  prims.labels = doc.areas.filter((r) => r.label && r.label.text).map((r) => { const q = P([r.label.x, r.label.y]); return [r.label.text + style.label.unit, r1(q[0]), r1(q[1])]; });
  prims.title = `${doc.title || ''} ${doc.unit}  ·  ${String(doc.total).replace('.', ',')} м²`;
  prims.style = { ink: style.ink, paper: style.paper, muted: style.muted, w_leaf: r1(style.w_leaf * K), w_arc: r1(style.w_arc * K), dash: r1(1.5 * K), w_win: r1(style.window.w * K), w_col: r1(style.w_column * K) };

  // ---- стены-raw, шахты, мебель: один путь на сущность PDF
  const rawWalls = doc.blocks.filter((b) => b.role === 'wall');
  if (rawWalls.length) g('Стены-raw', () => { for (const b of rawWalls) blockSvg(b, 'Стена-raw ' + b.id); });
  const shafts = doc.blocks.filter((b) => b.role === 'shaft');
  if (shafts.length) g('Шахты', () => { for (const b of shafts) blockSvg(b, 'Шахта ' + b.id); });
  g('Мебель', () => { for (const b of doc.blocks) if (!b.role || b.role === 'furniture') blockSvg(b, 'Мебель ' + b.id); });

  // скрытый носитель примитивов
  const payload = JSON.stringify(prims);
  const chunks = [];
  for (let i = 0; i < payload.length; i += 20000) chunks.push(payload.slice(i, i + 20000));
  chunks.forEach((c, i) => out.push(`<rect id="${esc('#prims ' + (i + 1) + '/' + chunks.length + ' ' + c)}" x="0" y="0" width="1" height="1" fill="none"/>`));

  function blockSvg(b, name) {
    out.push(`<g id="${esc(name)}">`);
    let i = 1;
    for (const p of b.paths || []) {
      const d = scalePath(p.d, b.x - bb.x0, b.y - bb.y0);
      const attrs = [`fill="${color(p.fill)}"`];
      if (p.fill) attrs.push('fill-rule="evenodd"');
      if (p.stroke) attrs.push(`stroke="${color(p.stroke)}" stroke-width="${f((p.w || style.w_furniture) * K)}" stroke-linecap="round" stroke-linejoin="round"`);
      const two = d.match(/^M ([\d.-]+) ([\d.-]+) L ([\d.-]+) ([\d.-]+)$/);
      if (two && !p.fill) out.push(`<line id="${esc('отрезок ' + i++)}" x1="${two[1]}" y1="${two[2]}" x2="${two[3]}" y2="${two[4]}" ${attrs.slice(1).join(' ')}/>`);
      else out.push(`<path id="${esc((p.fill ? 'заливка ' : 'линия ') + i++)}" d="${d}" ${attrs.join(' ')}/>`);
    }
    out.push('</g>');
  }

  function scalePath(d, ox, oy) {
    let i = 0;
    const s = d.replace(/-?\d*\.?\d+(?:e-?\d+)?|[A-Za-z]/g, (tok) => {
      if (/[A-Za-z]/.test(tok)) { i = 0; return tok; }
      const v = parseFloat(tok);
      const r = (i % 2 === 0) ? (v + ox) * K : (v + oy) * K;
      i++;
      return f(r);
    });
    return s.replace(/\s+/g, ' ').trim();
  }

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${f(W)}" height="${f(H)}" viewBox="0 0 ${f(W)} ${f(H)}">\n${out.join('\n')}\n</svg>`;
  writeFileSync(join(OUT, `unit-${u}.svg`), svg);

  manifest.units.push({
    unit: u, floor: doc.floor, type: doc.type, title: doc.title, total: doc.total, living: doc.living, balcony: doc.balcony,
    w: Math.round(W * 100) / 100, h: Math.round(H * 100) / 100,
    underlay: doc.underlay && doc.underlay.src ? join('plan-studio/truth', doc.underlay.src) : null,
    labels: doc.areas.filter((r) => r.label && r.label.text).map((r) => {
      const q = P([r.label.x, r.label.y]);
      return { id: r.id, kind: r.kind, text: r.label.text + style.label.unit, x: Math.round(q[0] * 100) / 100, y: Math.round(q[1] * 100) / 100 };
    }),
    prims: { walls: prims.walls.length, openings: prims.openings.length, columns: prims.columns.length, payloadBytes: payload.length },
    svgBytes: Buffer.byteLength(svg),
  });
  console.log(`${u}: ${f(W)}×${f(H)} px, walls ${prims.walls.length}, openings ${prims.openings.length}, cols ${prims.columns.length}, payload ${(payload.length / 1024).toFixed(1)} KB, svg ${(Buffer.byteLength(svg) / 1024).toFixed(0)} KB`);
}

writeFileSync(join(OUT, 'manifest.json'), JSON.stringify(manifest, null, 1));
