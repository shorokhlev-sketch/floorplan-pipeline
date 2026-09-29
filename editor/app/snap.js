// Редактор v3 — привязки. Всё в pt плана.
export const GRID = 0.25;
export const ANGLE_TOL_DEG = 5;

const r2 = (v) => Math.round(v * 100) / 100;
export function snapGrid(v) { return r2(Math.round(v / GRID) * GRID); }

/**
 * ctx: { nodes: [[x,y,id]], rawPts: [[x,y]], corners: [[x,y]] (углы полигонов/колонн/блоков),
 *        refs: [[x,y]] (для ортогонали), anchor: [x,y]|null (для угловой привязки 45°),
 *        shiftKey: bool (жёсткая угловая привязка), tol: pt, ortho: bool, off: bool }
 * Возвращает { p: [x,y], guides: [{kind:'node'|'raw'|'corner'|'angle'|'ortho-x'|'ortho-y', p, q?, deg?}] }.
 */
export function snapPoint(p, ctx) {
  if (ctx.off) return { p: [r2(p[0]), r2(p[1])], guides: [] };
  const tol = ctx.tol || 1.0;
  let best = null;
  for (const n of ctx.nodes || []) {
    const d = Math.hypot(n[0] - p[0], n[1] - p[1]);
    if (d <= tol && (!best || d < best.d)) best = { d, p: [n[0], n[1]], kind: 'node' };
  }
  if (best) return { p: best.p, guides: [{ kind: 'node', p: best.p }] };
  // углы полигонов/колонн/блоков — цели привязки наравне с узлами, тот же допуск, после узлов
  let bestC = null;
  for (const c of ctx.corners || []) {
    const d = Math.hypot(c[0] - p[0], c[1] - p[1]);
    if (d <= tol && (!bestC || d < bestC.d)) bestC = { d, p: [c[0], c[1]] };
  }
  if (bestC) return { p: bestC.p, guides: [{ kind: 'corner', p: bestC.p }] };
  for (const q of ctx.rawPts || []) {
    const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
    if (d <= tol * 0.8 && (!best || d < best.d)) best = { d, p: [q[0], q[1]], kind: 'raw' };
  }
  if (best) return { p: best.p, guides: [{ kind: 'raw', p: best.p }] };
  // угловая привязка относительно якоря: луч якорь→p к ближайшему кратному 45°
  if (ctx.anchor) {
    const [ax, ay] = ctx.anchor;
    const dx = p[0] - ax, dy = p[1] - ay;
    if (Math.hypot(dx, dy) > 1e-9) {
      let deg = Math.atan2(dy, dx) * 180 / Math.PI;
      if (deg < 0) deg += 360;
      const nearestRaw = Math.round(deg / 45) * 45;
      const diff = Math.abs(deg - nearestRaw);
      if (ctx.shiftKey || diff <= ANGLE_TOL_DEG) {
        const degF = ((nearestRaw % 360) + 360) % 360;
        const rad = degF * Math.PI / 180;
        const dir = [Math.cos(rad), Math.sin(rad)];
        const proj = dx * dir[0] + dy * dir[1];
        let x = ax + dir[0] * proj, y = ay + dir[1] * proj;
        if (degF % 90 === 0) { if (degF === 0 || degF === 180) x = snapGrid(x); else y = snapGrid(y); }
        return { p: [r2(x), r2(y)], guides: [{ kind: 'angle', p: [ax, ay], q: [r2(x), r2(y)], deg: degF }] };
      }
    }
  }
  // ортогональ к опорным точкам: выравниваем x и/или y
  let x = p[0], y = p[1];
  const guides = [];
  if (ctx.ortho !== false) {
    let bx = null, by = null;
    for (const r of ctx.refs || []) {
      const dx = Math.abs(r[0] - p[0]), dy = Math.abs(r[1] - p[1]);
      if (dx <= tol && (!bx || dx < bx.d)) bx = { d: dx, r };
      if (dy <= tol && (!by || dy < by.d)) by = { d: dy, r };
    }
    if (bx) { x = bx.r[0]; guides.push({ kind: 'ortho-x', p: bx.r, q: [x, y] }); }
    if (by) { y = by.r[1]; guides.push({ kind: 'ortho-y', p: by.r, q: [x, y] }); }
  }
  if (!guides.length) { x = snapGrid(x); y = snapGrid(y); }
  else { if (!guides.some((g) => g.kind === 'ortho-x')) x = snapGrid(x); if (!guides.some((g) => g.kind === 'ortho-y')) y = snapGrid(y); }
  return { p: [r2(x), r2(y)], guides };
}

/** Точки сырых примитивов из raw-N.json (crop-local + смещение кропа) — цели привязки. */
export function rawPoints(raw) {
  const pts = [];
  if (!raw || !raw.prims) return pts;
  const ox = raw.crop.x0, oy = raw.crop.y0;
  for (const pr of raw.prims) {
    if (!pr.role) continue;            // отсеянные примитивы не тянут курсор
    const toks = pr.d.split(/[\s,]+/);
    let cmd = null, cx = 0, cy = 0;
    for (let i = 0; i < toks.length;) {
      const t = toks[i];
      if (/^[MLChvZz]$/.test(t)) { cmd = t; i++; continue; }
      if (cmd === 'M' || cmd === 'L') { cx = +toks[i]; cy = +toks[i + 1]; pts.push([r2(cx + ox), r2(cy + oy)]); i += 2; }
      else if (cmd === 'C') { cx = +toks[i + 4]; cy = +toks[i + 5]; pts.push([r2(cx + ox), r2(cy + oy)]); i += 6; }
      else if (cmd === 'h') { cx += +toks[i]; pts.push([r2(cx + ox), r2(cy + oy)]); i++; }
      else if (cmd === 'v') { cy += +toks[i]; pts.push([r2(cx + ox), r2(cy + oy)]); i++; }
      else i++;
    }
  }
  return pts;
}

/** Проекция точки на отрезок: { s: pt от a вдоль u, d: расстояние, p } */
export function projectOnWall(g, p) {
  let s = (p[0] - g.a[0]) * g.u[0] + (p[1] - g.a[1]) * g.u[1];
  s = Math.max(0, Math.min(g.L, s));
  const q = [g.a[0] + g.u[0] * s, g.a[1] + g.u[1] * s];
  return { s, d: Math.hypot(p[0] - q[0], p[1] - q[1]), p: q };
}
