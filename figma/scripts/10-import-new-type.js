
const UNIT = __UNIT__; const IMPID = __IMPORT_NODE_ID__; // unit number; node id of the uploaded pdf-N.svg (upload_assets result)
const OPS = __OPS__;          // wall-thinning operations from tools/plan-wallthin.py <unit> (stdout JSON)
const R = __KEEP_RANGES__;    // "pdf" seqno ranges to keep, e.g. "1175 1711-1782 ..." (result of the cleanup pass)
const page = figma.currentPage;
const fr = page.children.find(c => c.type === 'FRAME' && c.name.startsWith(UNIT + ' · ') && !c.name.includes('cleanup') && !c.name.includes('final'));
const old = fr.children.find(c => c.name === 'PDF-векторы'); if (old) old.remove();
const imp = await figma.getNodeByIdAsync(IMPID);
let g = imp; if (imp.type === 'FRAME' && imp.children.length === 1 && 'children' in imp.children[0]) g = imp.children[0];
// ВАЖНО: позицию группы внутри импорт-фрейма СОХРАНЯТЬ (SVG-импорт обрезает пустые поля и кладёт группу со смещением).
// Обнуление g.x/g.y = сдвиг всех векторов (ошибка 2026-09-11, исправлено сдвигом по опорным элементам SVG — см. fix-скрипт в RESUME).
const ax = g.x, ay = g.y; fr.appendChild(g); g.x = ax; g.y = ay; g.name = 'PDF-векторы'; if (g !== imp) imp.remove();
const pdf = fr.children.find(c => c.name === 'PDF архитектора'); if (pdf) { fr.insertChild(0, pdf); pdf.locked = true; pdf.opacity = 0.45; pdf.visible = true; }
const EPS = 0.6;
const isGuide = (n) => n.strokes.length && n.strokes[0].type === 'SOLID' && ((n.strokes[0].color.b > 0.9 && n.strokes[0].color.r < 0.1) || (n.strokes[0].color.r > 0.9 && n.strokes[0].color.g < 0.1 && n.strokes[0].color.b < 0.1));
const byName = {}; for (const c of g.children) byName[c.name] = c;
const keep = new Set(); let resized = 0, removed = 0, trimmed = 0;
for (const [s, x, y, w, h] of OPS.fills) { const n = byName['pdf ' + s]; if (!n) continue; keep.add(n.name); n.resize(w == null ? n.width : w, h == null ? n.height : h); if (x != null) n.x = x; if (y != null) n.y = y; resized++; }
for (const b of OPS.bands) {
  for (const n of [...g.children]) {
    if (n.type !== 'VECTOR' || keep.has(n.name) || isGuide(n)) continue;
    const x0 = n.x, y0 = n.y, x1 = n.x + n.width, y1 = n.y + n.height;
    const across = b.axis === 'y' ? [y0, y1] : [x0, x1]; const alongR = b.axis === 'y' ? [x0, x1] : [y0, y1];
    if (!b.along.some(([a, c]) => alongR[0] >= a - EPS && alongR[1] <= c + EPS)) continue;
    if (across[1] - across[0] < 0.05 && Math.abs(across[0] - b.blue) < EPS) continue;
    if (across[0] >= b.lo - EPS && across[1] <= b.hi + EPS) { n.remove(); removed++; continue; }
    const d = n.vectorPaths.length === 1 ? n.vectorPaths[0].data : ''; const m = d.match(/^M ([\d.-]+) ([\d.-]+) L ([\d.-]+) ([\d.-]+) ?Z?$/); if (!m) continue;
    const P = [[x0 + +m[1], y0 + +m[2]], [x0 + +m[3], y0 + +m[4]]]; const v = (p) => b.axis === 'y' ? p[1] : p[0];
    const inBand = P.map(p => v(p) >= b.lo - EPS && v(p) <= b.hi + EPS); if (inBand[0] === inBand[1]) continue;
    const io = inBand[0] ? 0 : 1, ii = 1 - io; const innerOk = (b.blue === b.hi) ? v(P[ii]) > b.blue : v(P[ii]) < b.blue; if (!innerOk) continue;
    const t = (b.blue - v(P[io])) / (v(P[ii]) - v(P[io])); const Q = [P[io][0] + (P[ii][0] - P[io][0]) * t, P[io][1] + (P[ii][1] - P[io][1]) * t];
    n.resize(Math.max(Math.abs(Q[0] - P[ii][0]), 0.01), Math.max(Math.abs(Q[1] - P[ii][1]), 0.01)); n.x = Math.min(Q[0], P[ii][0]); n.y = Math.min(Q[1], P[ii][1]); trimmed++;
  }
}
// cleanup copy above the original
const drop = new Set(); for (const tok of R.split(' ')) { const [a, b] = tok.split('-').map(Number); if (b == null) drop.add(a); else for (let i = a; i <= b; i++) drop.add(i); }
const oldc = page.children.find(c => c.type === 'FRAME' && c.name.startsWith(UNIT + ' · ') && c.name.includes('cleanup')); if (oldc) oldc.remove();
const copy = fr.clone(); page.appendChild(copy); copy.name = fr.name + ' · cleanup'; copy.x = fr.x; copy.y = fr.y - fr.height - 400;
const gc = copy.children.find(c => c.name === 'PDF-векторы'); let dropped = 0; const before = gc.children.length;
for (const n of [...gc.children]) { const s = parseInt(n.name.replace('pdf ', '')); if (drop.has(s)) { n.remove(); dropped++; } }
const pc = copy.children.find(c => c.name === 'PDF архитектора'); if (pc) { pc.visible = true; pc.opacity = 0.45; pc.locked = true; }
return { unit: UNIT, frame: fr.id, vectors: g.children.length, resized, removed, trimmed, cleanup: copy.id, cleanupY: Math.round(copy.y), before, dropped, left: gc.children.length, layers: fr.children.map(c => c.name) };
