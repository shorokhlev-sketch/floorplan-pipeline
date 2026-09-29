// Редактор v3 — операции над документом (чистые мутации) и история.
import { wallGeom, wallOpenings } from '../render.js';

export const NODE_TOL = 0.06;
const r2 = (v) => Math.round(v * 100) / 100;

export function newId(doc, prefix, coll) {
  const used = new Set(coll === 'nodes' ? Object.keys(doc.nodes) : doc[coll].map((x) => x.id));
  let k = 1;
  while (used.has(prefix + k)) k++;
  return prefix + k;
}

export function findNode(doc, p, tol) {
  tol = tol == null ? NODE_TOL : tol;
  for (const id of Object.keys(doc.nodes)) {
    const q = doc.nodes[id];
    if (Math.abs(q[0] - p[0]) <= tol && Math.abs(q[1] - p[1]) <= tol) return id;
  }
  return null;
}

export function addNode(doc, p) {
  const ex = findNode(doc, p);
  if (ex) return ex;
  const id = newId(doc, 'n', 'nodes');
  doc.nodes[id] = [r2(p[0]), r2(p[1])];
  return id;
}

export function addWall(doc, aId, bId, t, side, kind) {
  if (aId === bId) return null;
  const w = { id: newId(doc, 'w', 'walls'), a: aId, b: bId, t: r2(t), side: side || 1, kind: kind || 'partition' };
  doc.walls.push(w);
  return w;
}

export function wallById(doc, id) { return doc.walls.find((w) => w.id === id); }
export function openingById(doc, id) { return doc.openings.find((o) => o.id === id); }
export function areaById(doc, id) { return doc.areas.find((r) => r.id === id); }
export function blockById(doc, id) { return doc.blocks.find((b) => b.id === id); }
export function columnById(doc, id) { return doc.columns.find((c) => c.id === id); }

export function nodeWalls(doc, nid) { return doc.walls.filter((w) => w.a === nid || w.b === nid); }

export function moveNode(doc, nid, p) {
  doc.nodes[nid] = [r2(p[0]), r2(p[1])];
  // проёмы стен этого узла: удерживаем в пределах новой длины
  for (const w of nodeWalls(doc, nid)) clampOpenings(doc, w);
}

export function clampOpenings(doc, w) {
  const g = wallGeom(doc, w);
  for (const o of wallOpenings(doc, w.id)) {
    if (o.width > g.L) o.width = r2(Math.max(0.5, g.L));
    if (o.pos + o.width > g.L) o.pos = r2(Math.max(0, g.L - o.width));
    if (o.pos < 0) o.pos = 0;
  }
}

/** Разрезать стену в точке s (pt от a). Возвращает новую стену (вторую половину). */
export function splitWall(doc, wallId, s) {
  const w = wallById(doc, wallId);
  const g = wallGeom(doc, w);
  if (s <= 0.3 || s >= g.L - 0.3) return null;
  const p = [g.a[0] + g.u[0] * s, g.a[1] + g.u[1] * s];
  const mid = addNode(doc, p);
  const w2 = { id: newId(doc, 'w', 'walls'), a: mid, b: w.b, t: w.t, side: w.side, kind: w.kind };
  w.b = mid;
  doc.walls.push(w2);
  for (const o of doc.openings) {
    if (o.wall !== w.id) continue;
    if (o.pos >= s) { o.wall = w2.id; o.pos = r2(o.pos - s); }
    else if (o.pos + o.width > s) {
      // проём поперёк разреза: оставляем на первой половине, вторую часть режем
      o.width = r2(s - o.pos);
    }
  }
  return w2;
}

/** Склеить две коллинеарные стены с общим узлом. */
export function joinWalls(doc, id1, id2) {
  const w1 = wallById(doc, id1), w2 = wallById(doc, id2);
  if (!w1 || !w2) return null;
  const shared = [w1.a, w1.b].find((n) => n === w2.a || n === w2.b);
  if (!shared) return null;
  if (nodeWalls(doc, shared).length !== 2) return null;
  const g1 = wallGeom(doc, w1), g2 = wallGeom(doc, w2);
  if (Math.abs(g1.u[0] * g2.u[1] - g1.u[1] * g2.u[0]) > 0.02) return null;   // не коллинеарны
  // ориентируем: w1 = a→shared, w2 = shared→far
  const farEnd = w2.a === shared ? w2.b : w2.a;
  const start = w1.a === shared ? w1.b : w1.a;
  // проёмы w2 переводим в систему объединённой стены (от start)
  const gA = wallGeom(doc, { a: start, b: shared, side: 1, t: 1 });
  const L1 = gA.L;
  const flip1 = w1.a === shared;   // w1 шла shared→start: pos пересчитать
  for (const o of doc.openings) {
    if (o.wall === w1.id && flip1) o.pos = r2(L1 - o.pos - o.width);
    if (o.wall === w2.id) {
      const flip2 = w2.b === shared;
      const L2 = g2.L;
      const pos2 = flip2 ? L2 - o.pos - o.width : o.pos;
      o.pos = r2(L1 + pos2); o.wall = w1.id;
      if (flip1 !== flip2 && o.kind === 'door') { /* сторона распаха относительно n сохраняется через side ниже */ }
    }
  }
  // сторона выдавливания: если направление стены развернулось, side меняет знак
  const oldDir = w1.a === start ? 1 : -1;
  const side = w1.side * oldDir;
  w1.a = start; w1.b = farEnd; w1.side = side;
  if (oldDir === -1) for (const o of doc.openings) if (o.wall === w1.id && o.hinge) o.hinge = o.hinge === 'a' ? 'b' : 'a';
  doc.walls = doc.walls.filter((w) => w.id !== w2.id);
  delete doc.nodes[shared];
  return w1;
}

export function deleteWall(doc, id) {
  doc.openings = doc.openings.filter((o) => o.wall !== id);
  doc.walls = doc.walls.filter((w) => w.id !== id);
  cleanupNodes(doc);
}

export function cleanupNodes(doc) {
  const used = new Set();
  for (const w of doc.walls) { used.add(w.a); used.add(w.b); }
  for (const id of Object.keys(doc.nodes)) if (!used.has(id)) delete doc.nodes[id];
}

export function addOpening(doc, wallId, pos, width, kind) {
  const w = wallById(doc, wallId);
  const g = wallGeom(doc, w);
  width = Math.min(width, g.L);
  pos = Math.max(0, Math.min(g.L - width, pos));
  const o = { id: newId(doc, 'o', 'openings'), wall: wallId, pos: r2(pos), width: r2(width), kind };
  if (kind === 'door') { o.hinge = 'a'; o.swing = -1; }
  doc.openings.push(o);
  return o;
}

export function flipWallSide(doc, w) {
  // разворот стороны так, чтобы стена осталась на месте: опорная линия переезжает на другую грань
  const g = wallGeom(doc, w);
  const na = [g.a[0] + g.n[0] * w.t, g.a[1] + g.n[1] * w.t];
  const nb = [g.b[0] + g.n[0] * w.t, g.b[1] + g.n[1] * w.t];
  // если узлы общие с другими стенами — просто меняем сторону (геометрически стена «перепрыгнет»)
  if (nodeWalls(doc, w.a).length === 1 && nodeWalls(doc, w.b).length === 1) {
    doc.nodes[w.a] = [r2(na[0]), r2(na[1])]; doc.nodes[w.b] = [r2(nb[0]), r2(nb[1])];
  }
  w.side = -w.side;
  for (const o of doc.openings) if (o.wall === w.id && o.kind === 'door') o.swing = -(o.swing || -1);
}

// ---------------------------------------------------------------- области

function segIntersect(p, q, a, b) {
  // пересечение бесконечной прямой p→q с отрезком a–b; возвращает t по отрезку и точку
  const d = [q[0] - p[0], q[1] - p[1]];
  const e = [b[0] - a[0], b[1] - a[1]];
  const den = d[0] * e[1] - d[1] * e[0];
  if (Math.abs(den) < 1e-9) return null;
  const w = [a[0] - p[0], a[1] - p[1]];
  const t = (w[0] * d[1] - w[1] * d[0]) / den;   // по отрезку a–b
  if (t < -1e-6 || t > 1 + 1e-6) return null;
  return { t: Math.max(0, Math.min(1, t)), p: [a[0] + e[0] * t, a[1] + e[1] * t] };
}

/** Разрезать полигон области прямой p–q. Возвращает [area1, area2] (area1 = исходная, урезанная) или null. */
export function splitArea(doc, areaId, p, q) {
  const r = areaById(doc, areaId);
  if (!r) return null;
  const poly = r.poly;
  const hits = [];
  for (let i = 0; i < poly.length; i++) {
    const a = poly[i], b = poly[(i + 1) % poly.length];
    const h = segIntersect(p, q, a, b);
    if (h && h.t < 1 - 1e-6) hits.push({ i, t: h.t, p: h.p });
  }
  if (hits.length < 2) return null;
  // берём два пересечения, ближайшие к p и q соответственно вдоль прямой
  const d = [q[0] - p[0], q[1] - p[1]];
  const along = (pt) => (pt[0] - p[0]) * d[0] + (pt[1] - p[1]) * d[1];
  hits.sort((x, y) => along(x.p) - along(y.p));
  // выбираем пару, между которой лежит середина p–q
  const mid = along([(p[0] + q[0]) / 2, (p[1] + q[1]) / 2]);
  let h1 = null, h2 = null;
  for (let k = 0; k + 1 < hits.length; k++) {
    if (along(hits[k].p) <= mid && along(hits[k + 1].p) >= mid) { h1 = hits[k]; h2 = hits[k + 1]; break; }
  }
  if (!h1) { h1 = hits[0]; h2 = hits[hits.length - 1]; }
  if (h1.i === h2.i) return null;
  const A = [], B = [];
  const P1 = [r2(h1.p[0]), r2(h1.p[1])], P2 = [r2(h2.p[0]), r2(h2.p[1])];
  // A: от h1 по кольцу до h2; B: остальное
  let i = h1.i;
  A.push(P1);
  while (true) {
    i = (i + 1) % poly.length;
    A.push(poly[i]);
    if (i === h2.i) break;
  }
  A.push(P2);
  B.push(P2);
  i = h2.i;
  while (true) {
    i = (i + 1) % poly.length;
    B.push(poly[i]);
    if (i === h1.i) break;
  }
  B.push(P1);
  const dedupe = (pts) => pts.filter((pt, k) => k === 0 || Math.hypot(pt[0] - pts[k - 1][0], pt[1] - pts[k - 1][1]) > 0.05);
  const pa = dedupe(A), pb = dedupe(B);
  if (pa.length < 3 || pb.length < 3) return null;
  r.poly = pa;
  const r2b = { id: newId(doc, 'r', 'areas'), kind: r.kind, name: '', poly: pb, label: null };
  doc.areas.push(r2b);
  return [r, r2b];
}

export function centroid(poly) {
  let x = 0, y = 0, a = 0;
  for (let i = 0; i < poly.length; i++) {
    const [x0, y0] = poly[i], [x1, y1] = poly[(i + 1) % poly.length];
    const c = x0 * y1 - x1 * y0;
    a += c; x += (x0 + x1) * c; y += (y0 + y1) * c;
  }
  if (Math.abs(a) < 1e-9) return poly[0];
  return [x / (3 * a), y / (3 * a)];
}

// ---------------------------------------------------------------- история

export class History {
  constructor(limit) { this.past = []; this.future = []; this.limit = limit || 200; }
  push(doc) { this.past.push(JSON.stringify(doc)); if (this.past.length > this.limit) this.past.shift(); this.future = []; }
  undo(doc) { if (!this.past.length) return null; this.future.push(JSON.stringify(doc)); return JSON.parse(this.past.pop()); }
  redo(doc) { if (!this.future.length) return null; this.past.push(JSON.stringify(doc)); return JSON.parse(this.future.pop()); }
  get canUndo() { return this.past.length > 0; }
  get canRedo() { return this.future.length > 0; }
}
