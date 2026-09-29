// Unified final-plan pipeline; rebuild final 201 and screenshot
const DATA = __DATA__; // {unit: {t1, t2, areas: [{k: 'room' | 'balcony', p: [[x, y], ...], l: 'area label'}]}} in frame px (pt * 7.05), from the plan document
await figma.loadFontAsync({ family: 'Instrument Serif', style: 'Regular' });
const FONT = { family: 'Instrument Serif', style: 'Regular' };
const INK = { r: 0x18/255, g: 0x2E/255, b: 0x46/255 }, MUTED = { r: 0x9A/255, g: 0xA3/255, b: 0xAD/255 }, FLOOR = { r: 0xF3/255, g: 0xEF/255, b: 0xE8/255 }, WHITE = { r: 1, g: 1, b: 1 };
const hex = (c) => '#' + [c.r, c.g, c.b].map(v => Math.round(v * 255).toString(16).padStart(2, '0')).join('').toUpperCase();
function inside(pt, poly) { let ins = false; for (let i = 0, n = poly.length; i < n; i++) { const [x1, y1] = poly[i], [x2, y2] = poly[(i + 1) % n]; if ((y1 > pt[1]) !== (y2 > pt[1]) && pt[0] < (x2 - x1) * (pt[1] - y1) / (y2 - y1) + x1) ins = !ins; } return ins; }
function distPoly(pt, poly) { let best = 1e9; for (let i = 0, n = poly.length; i < n; i++) { const [x1, y1] = poly[i], [x2, y2] = poly[(i + 1) % n]; const dx = x2 - x1, dy = y2 - y1, L = dx * dx + dy * dy; const t = L === 0 ? 0 : Math.max(0, Math.min(1, ((pt[0] - x1) * dx + (pt[1] - y1) * dy) / L)); best = Math.min(best, Math.hypot(pt[0] - (x1 + t * dx), pt[1] - (y1 + t * dy))); } return best; }
const bboxOf = (n) => [n.x, n.y, n.x + n.width, n.y + n.height]; const within = (b, B, e) => b[0] >= B[0] - e && b[1] >= B[1] - e && b[2] <= B[2] + e && b[3] <= B[3] + e; const hit = (b, B) => !(b[2] < B[0] || b[0] > B[2] || b[3] < B[1] || b[1] > B[3]);
const page = figma.currentPage; const results = {};
for (const [u, D] of Object.entries(DATA)) {
  const ROOM = D.areas.find(a => a.k === 'room').p; const BALS = D.areas.filter(a => a.k === 'balcony');
  const sd = (p) => (inside(p, ROOM) ? -1 : 1) * distPoly(p, ROOM);
  const old = page.children.find(c => c.type === 'FRAME' && c.name.startsWith(u + ' · ') && c.name.includes('final')); if (old) old.remove();
  const src = page.children.find(c => c.type === 'FRAME' && c.name.startsWith(u + ' · ') && c.name.includes('cleanup'));
  const fr = src.clone(); page.appendChild(fr); fr.name = src.name.replace('cleanup', 'final'); fr.x = src.x; fr.y = src.y - src.height - 400; fr.fills = [{ type: 'SOLID', color: WHITE }];
  for (const c of [...fr.children]) if (c.name !== 'PDF-векторы') c.remove();
  const g = fr.children.find(c => c.name === 'PDF-векторы'); g.name = 'Чертёж';
  const mkPoly = (name, poly, col) => { const v = figma.createVector(); fr.appendChild(v); v.name = name; v.vectorPaths = [{ windingRule: 'EVENODD', data: 'M ' + poly.map(p => p.join(' ')).join(' L ') + ' Z' }]; v.fills = [{ type: 'SOLID', color: col }]; v.strokes = []; v.x = Math.min(...poly.map(p => p[0])); v.y = Math.min(...poly.map(p => p[1])); fr.insertChild(0, v); return v; };
  for (const a of D.areas) mkPoly(a.k === 'room' ? 'Пол' : 'Пол балкона', a.p, FLOOR);
  const mkLine = (x1, y1, x2, y2, w, col, name) => { const ln = figma.createLine(); g.appendChild(ln); ln.strokes = [{ type: 'SOLID', color: col }]; ln.strokeWeight = w; ln.strokeCap = 'NONE'; ln.resize(Math.max(Math.hypot(x2 - x1, y2 - y1), 0.01), 0); ln.rotation = -Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI; ln.x = x1; ln.y = y1; ln.name = name; return ln; };
  // restyle
  const cls = new Map(); const st = {}; const bump = (k) => st[k] = (st[k] || 0) + 1;
  for (const n of [...g.children]) {
    if (n.type !== 'VECTOR') continue;
    const fl = n.fills.length && n.fills[0].type === 'SOLID' ? hex(n.fills[0].color) : null; const sk = n.strokes.length && n.strokes[0].type === 'SOLID' ? hex(n.strokes[0].color) : null;
    const d = n.vectorPaths.length ? n.vectorPaths[0].data : ''; const curve = /C/.test(d); const npts = (d.match(/-?\d+\.?\d*/g) || []).length / 2;
    if (fl === '#6A6A6A' || fl === '#FD8379') { n.fills = [{ type: 'SOLID', color: INK }]; n.strokes = []; n.name = 'стена'; cls.set(n, 'wall'); bump('wall'); continue; }
    if (fl === '#000000' || fl === '#40A5FF' || fl === '#BD8B17') { n.fills = [{ type: 'SOLID', color: INK }]; n.strokes = []; n.name = 'клин окна'; cls.set(n, 'wedge'); bump('wedge'); continue; }
    if (sk === '#FF0000' || sk === '#0000FF' || sk === '#80C2FF' || sk === '#DCB900' || sk === '#DF0000' || sk === '#A80F02') { n.remove(); bump('service'); continue; }
    if (fl === '#FFFFFF') { n.fills = [{ type: 'SOLID', color: WHITE }]; if (sk) { n.strokes = [{ type: 'SOLID', color: INK }]; n.strokeWeight = 1.5; } cls.set(n, 'white'); bump('white'); continue; }
    if (sk === '#FF6600') { if (curve) { n.strokes = [{ type: 'SOLID', color: INK }]; n.strokeWeight = 1.5; n.dashPattern = []; n.name = 'дуга двери'; cls.set(n, 'arc'); } else { n.strokes = [{ type: 'SOLID', color: INK }]; n.strokeWeight = 2; n.dashPattern = []; n.name = 'окно/полотно'; if (npts <= 2) cls.set(n, 'line'); } bump('orange'); continue; }
    if (sk === '#6A6A6A' || sk === '#000000' || sk === '#7F7F7F' || sk === '#999999' && false) { n.strokes = [{ type: 'SOLID', color: INK }]; n.strokeWeight = 1.5; n.name = 'мебель'; if (!curve && npts <= 2) cls.set(n, 'line'); bump('ink'); continue; }
    if (sk === '#AAAAAA' || sk === '#999999' || sk === '#CCCCCC') { n.strokes = [{ type: 'SOLID', color: MUTED }]; n.strokeWeight = 1.2; n.name = 'мебель-2'; if (!curve && npts <= 2) cls.set(n, 'line'); bump('muted'); continue; }
    if (fl) { n.fills = [{ type: 'SOLID', color: WHITE }]; cls.set(n, 'white'); bump('fill-other'); continue; }
    if (sk) { n.strokes = [{ type: 'SOLID', color: INK }]; n.strokeWeight = 1.5; if (!curve && npts <= 2) cls.set(n, 'line'); bump('stroke-other'); }
  }
  const walls = () => [...cls].filter(([n, c]) => c === 'wall' && !n.removed).map(([n]) => bboxOf(n));
  const lines = () => [...cls].filter(([n, c]) => c === 'line' && !n.removed).map(([n]) => n);
  // windows niches & columns
  let W = walls();
  const isDiag = (n) => { const q = (n.vectorPaths[0].data.match(/-?\d+\.?\d*/g) || []).map(Number); return q.length >= 4 && Math.abs(q[2] - q[0]) > 3 && Math.abs(q[3] - q[1]) > 3; };
  for (const [n, c] of cls) { if (c !== 'white' || n.removed) continue; const pts = (n.vectorPaths[0].data.match(/-?\d+\.?\d*/g) || []).length / 2; if (pts > 5) continue; const b = bboxOf(n); const thin = Math.min(n.width, n.height), long = Math.max(n.width, n.height); const ctr = [n.x + n.width / 2, n.y + n.height / 2]; const s = sd(ctr);
    if (long >= 40 && thin >= 8 && thin <= 40 && s > -10 && s <= 45) { n.strokes = []; n.name = 'окно'; for (const l of lines()) if (within(bboxOf(l), b, 1)) l.remove(); const horiz = n.width >= n.height; for (const f of [1 / 3, 2 / 3]) { if (horiz) mkLine(n.x, n.y + n.height * f, n.x + n.width, n.y + n.height * f, 1.5, INK, 'остекление'); else mkLine(n.x + n.width * f, n.y, n.x + n.width * f, n.y + n.height, 1.5, INK, 'остекление'); } bump('niche'); continue; }
    const dg = lines().filter(l => within(bboxOf(l), b, 1) && isDiag(l));
    if (Math.abs(n.width - n.height) < 15 && n.width >= 40 && n.width <= 90 && dg.length >= 2 && (W.some(Wb => hit(b, Wb)) || (s > -40))) { n.strokes = [{ type: 'SOLID', color: INK }]; n.strokeWeight = 6.3; n.strokeAlign = 'INSIDE'; n.name = 'колонна'; for (const l of dg) l.remove(); bump('column'); }
  }
  // door jamb brackets: small white boxes (~3x10) not inside a wall → snap across into nearest wall band
  W = walls();
  for (const [n, c] of cls) { if (c !== 'white' || n.removed) continue; const b = bboxOf(n); const mn = Math.min(n.width, n.height), mx = Math.max(n.width, n.height); if (!(mn <= 4.5 && mx >= 8 && mx <= 12)) continue; if (W.some(Wb => within(b, Wb, 1.5))) continue;
    const vert = n.height > n.width; const cx = n.x + n.width / 2, cy = n.y + n.height / 2; let best = null;
    for (const Wb of W) { const alongOK = vert ? (cy >= Wb[1] - 2 && cy <= Wb[3] + 2) : (cx >= Wb[0] - 2 && cx <= Wb[2] + 2); if (!alongOK) continue; const across = vert ? Math.min(Math.abs(Wb[0] - cx), Math.abs(Wb[2] - cx)) : Math.min(Math.abs(Wb[1] - cy), Math.abs(Wb[3] - cy)); if (across <= 14 && (!best || across < best.d)) best = { d: across, Wb }; }
    if (!best) continue; const Wb = best.Wb; const oldBox = bboxOf(n);
    if (vert) { n.resize(Wb[2] - Wb[0], n.height); n.x = Wb[0]; } else { n.resize(n.width, Wb[3] - Wb[1]); n.y = Wb[1]; }
    for (const l of lines()) { const lb = bboxOf(l); if (within(lb, [oldBox[0] - 1, oldBox[1] - 1, oldBox[2] + 1, oldBox[3] + 1], 0)) { if (l.height > 5 && l.width < 0.5 && !vert) { l.resize(0.01, Wb[3] - Wb[1]); l.y = Wb[1]; } else if (l.width > 5 && l.height < 0.5 && vert) { l.resize(Wb[2] - Wb[0], 0.01); l.x = Wb[0]; } else if (l.width < 0.5 && !vert) { l.y = Math.abs(l.y - oldBox[1]) < 0.6 ? Wb[1] : Wb[3]; } else if (l.height < 0.5 && vert) { l.x = Math.abs(l.x - oldBox[0]) < 0.6 ? Wb[0] : Wb[2]; } }
      else if (Math.max(l.width, l.height) <= 5.2 && (vert ? (Math.abs(l.y - cy) < 8 && lb[0] >= oldBox[0] - 1 && lb[2] <= oldBox[2] + 12) : (Math.abs(l.x - cx) < 8 && lb[1] >= oldBox[1] - 1 && lb[3] <= oldBox[3] + 12))) l.remove(); }
    bump('bracket'); }
  // balcony glazing band between room and balcony, extended to walls
  W = walls();
  const edges = (poly) => poly.map((p, i) => [p, poly[(i + 1) % poly.length]]);
  for (const B of BALS) { for (const [b1, b2] of edges(B.p)) { const horiz = Math.abs(b1[1] - b2[1]) < 0.5, vert = Math.abs(b1[0] - b2[0]) < 0.5; if (!horiz && !vert) continue;
      for (const [r1, r2] of edges(ROOM)) { if (horiz && Math.abs(r1[1] - r2[1]) < 0.5 && Math.abs(r1[1] - b1[1]) >= 5 && Math.abs(r1[1] - b1[1]) <= 25) { const lo = Math.max(Math.min(b1[0], b2[0]), Math.min(r1[0], r2[0])), hi = Math.min(Math.max(b1[0], b2[0]), Math.max(r1[0], r2[0])); if (hi - lo < 40) continue; const y0 = Math.min(r1[1], b1[1]), y1 = Math.max(r1[1], b1[1]); let x0 = lo, x1 = hi; const cy = (y0 + y1) / 2;
          for (let k = 0; k < 80; k++) { const p = [x0 - 1, cy]; if (W.some(Wb => p[0] >= Wb[0] && p[0] <= Wb[2] && p[1] >= Wb[1] && p[1] <= Wb[3])) break; x0 -= 1; } for (let k = 0; k < 80; k++) { const p = [x1 + 1, cy]; if (W.some(Wb => p[0] >= Wb[0] && p[0] <= Wb[2] && p[1] >= Wb[1] && p[1] <= Wb[3])) break; x1 += 1; }
          const band = [x0, y0 - 1, x1, y1 + 1]; for (const n of [...g.children]) { if (n.removed) continue; const nb = bboxOf(n); if (within(nb, band, 0.5) && (n.width < (x1 - x0) - 2 || n.fills.length === 0 || (n.fills.length && cls.get(n) !== 'wall') )) { if (cls.get(n) === 'wall' && n.width >= (x1 - x0) - 2) continue; n.remove(); } }
          const r = figma.createRectangle(); g.appendChild(r); r.name = 'остекление балкона'; r.resize(x1 - x0, y1 - y0); r.x = x0; r.y = y0; r.fills = [{ type: 'SOLID', color: WHITE }]; r.strokes = []; for (const f of [1 / 3, 2 / 3]) mkLine(x0, y0 + (y1 - y0) * f, x1, y0 + (y1 - y0) * f, 1.5, INK, 'остекление'); bump('bal-glazing'); }
        if (vert && Math.abs(r1[0] - r2[0]) < 0.5 && Math.abs(r1[0] - b1[0]) >= 5 && Math.abs(r1[0] - b1[0]) <= 25) { const lo = Math.max(Math.min(b1[1], b2[1]), Math.min(r1[1], r2[1])), hi = Math.min(Math.max(b1[1], b2[1]), Math.max(r1[1], r2[1])); if (hi - lo < 40) continue; const x0 = Math.min(r1[0], b1[0]), x1 = Math.max(r1[0], b1[0]); let y0 = lo, y1 = hi; const cx = (x0 + x1) / 2;
          for (let k = 0; k < 80; k++) { const p = [cx, y0 - 1]; if (W.some(Wb => p[0] >= Wb[0] && p[0] <= Wb[2] && p[1] >= Wb[1] && p[1] <= Wb[3])) break; y0 -= 1; } for (let k = 0; k < 80; k++) { const p = [cx, y1 + 1]; if (W.some(Wb => p[0] >= Wb[0] && p[0] <= Wb[2] && p[1] >= Wb[1] && p[1] <= Wb[3])) break; y1 += 1; }
          const band = [x0 - 1, y0, x1 + 1, y1]; for (const n of [...g.children]) { if (n.removed) continue; const nb = bboxOf(n); if (within(nb, band, 0.5)) { if (cls.get(n) === 'wall' && n.height >= (y1 - y0) - 2) continue; n.remove(); } }
          const r = figma.createRectangle(); g.appendChild(r); r.name = 'остекление балкона'; r.resize(x1 - x0, y1 - y0); r.x = x0; r.y = y0; r.fills = [{ type: 'SOLID', color: WHITE }]; r.strokes = []; for (const f of [1 / 3, 2 / 3]) mkLine(x0 + (x1 - x0) * f, y0, x0 + (x1 - x0) * f, y1, 1.5, INK, 'остекление'); bump('bal-glazing'); } } } }
  // balcony outer edge tick band
  for (const B of BALS) { let far = null; for (const [a, b] of edges(B.p)) { const m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]; const dd = distPoly(m, ROOM); if (!far || dd > far.d) far = { a, b, d: dd }; } if (!far || far.d < 30) continue;
    const a = far.a, b = far.b; const L = Math.hypot(b[0] - a[0], b[1] - a[1]); const ux = (b[0] - a[0]) / L, uy = (b[1] - a[1]) / L; let nx = -uy, ny = ux; const c = [(a[0] + b[0]) / 2 + nx * 5, (a[1] + b[1]) / 2 + ny * 5]; if (!inside(c, B.p)) { nx = -nx; ny = -ny; }
    for (const n of [...g.children]) { if (n.removed || n.fills.length) continue; const nb = bboxOf(n); const m = [(nb[0] + nb[2]) / 2, (nb[1] + nb[3]) / 2]; const dEdge = Math.abs((m[0] - a[0]) * nx + (m[1] - a[1]) * ny); const t = ((m[0] - a[0]) * ux + (m[1] - a[1]) * uy); if (dEdge <= 12 && t > -5 && t < L + 5 && Math.max(n.width, n.height) > 15) n.remove(); }
    const off = (d) => [a[0] + nx * d, a[1] + ny * d, b[0] + nx * d, b[1] + ny * d]; const l1 = off(8.3), l2 = off(-1.7); mkLine(l1[0], l1[1], l1[2], l1[3], 2, INK, 'ограждение'); mkLine(l2[0], l2[1], l2[2], l2[3], 2, INK, 'ограждение');
    for (let t = 6; t < L; t += 12) { const p1 = [a[0] + ux * t + nx * 8.3, a[1] + uy * t + ny * 8.3], p2 = [a[0] + ux * t - nx * 1.7, a[1] + uy * t - ny * 1.7]; mkLine(p1[0], p1[1], p2[0], p2[1], 1.2, INK, 'ограждение'); } bump('bal-edge'); }
  // labels & title
  const boxes = g.children.filter(n => !n.removed).map(n => bboxOf(n));
  const rectClear = (b) => { let m = 1e9; for (const Bb of boxes) { if (hit(b, Bb)) return -1; const dx = Math.max(Bb[0] - b[2], 0, b[0] - Bb[2]); const dy = Math.max(Bb[1] - b[3], 0, b[1] - Bb[3]); m = Math.min(m, Math.hypot(dx, dy)); } return m; };
  const place = (poly, w, h, extra) => { const xs = poly.map(p => p[0]), ys = poly.map(p => p[1]); let best = null; for (let x = Math.min(...xs) + 8; x + w <= Math.max(...xs) - 8; x += 4) for (let y = Math.min(...ys) + 8; y + h <= Math.max(...ys) - 8; y += 4) { const b = [x, y, x + w, y + h]; if (![[b[0], b[1]], [b[2], b[1]], [b[0], b[3]], [b[2], b[3]]].every(p => inside(p, poly))) continue; if (extra.some(e => hit(b, e))) continue; const c = rectClear(b); if (c < 0) continue; if (!best || c > best.c) best = { x, y, c }; } return best; };
  const taken = []; let T = null, t1, t2, tw, th, fs;
  for (fs of [40, 34, 30, 26, 22]) { t1 = figma.createText(); fr.appendChild(t1); t1.fontName = FONT; t1.characters = D.t1; t1.fontSize = fs; t1.fills = [{ type: 'SOLID', color: INK }]; t2 = figma.createText(); fr.appendChild(t2); t2.fontName = FONT; t2.characters = D.t2; t2.fontSize = fs; t2.fills = [{ type: 'SOLID', color: INK }]; tw = Math.max(t1.width, t2.width) + 14; th = t1.height + t2.height + 6; T = place(ROOM, tw + 6, th + 6, []); if (T) break; t1.remove(); t2.remove(); }
  if (T) { const tx = T.x + 14, ty = T.y + 3; t1.x = tx; t1.y = ty; t2.x = tx; t2.y = ty + t1.height; const br = figma.createVector(); fr.appendChild(br); br.name = 'уголок'; br.vectorPaths = [{ windingRule: 'NONZERO', data: `M 0 0 L 0 ${th} L ${tw} ${th}` }]; br.strokes = [{ type: 'SOLID', color: INK }]; br.strokeWeight = 1.5; br.fills = []; br.x = tx - 11; br.y = ty; figma.group([t1, t2, br], fr).name = 'Заголовок'; taken.push([T.x - 16, T.y - 16, T.x + tw + 22, T.y + th + 22]); }
  const labels = [];
  for (const a of D.areas) { if (!a.l) continue; const t = figma.createText(); fr.appendChild(t); t.fontName = FONT; t.characters = a.l; t.fontSize = a.k === 'room' ? 40 : 34; t.fills = [{ type: 'SOLID', color: INK }]; t.name = a.l; const P = place(a.p, t.width + 8, t.height + 4, taken); if (P) { t.x = P.x + 4; t.y = P.y + 2; taken.push([P.x - 10, P.y - 10, P.x + t.width + 18, P.y + t.height + 14]); } else { const xs = a.p.map(p => p[0]), ys = a.p.map(p => p[1]); t.x = (Math.min(...xs) + Math.max(...xs)) / 2 - t.width / 2; t.y = (Math.min(...ys) + Math.max(...ys)) / 2 - t.height / 2; } labels.push(t); }
  if (labels.length) figma.group(labels, fr).name = 'Подписи';
  results[u] = { id: fr.id, stat: st, titleFont: T ? fs : null, shot: await fr.screenshot({ scale: 0.6 }) };
}
return results;