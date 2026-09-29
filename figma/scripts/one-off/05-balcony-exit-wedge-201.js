// Final 201: wedge in the bottom-left corner; balcony exit restyled — window band only over windows with 3 lines, floor through the door opening, jambs at window ends; zooms
const INK = { r: 0x18/255, g: 0x2E/255, b: 0x46/255 }, WHITE = { r: 1, g: 1, b: 1 }, FLOOR = { r: 0xF3/255, g: 0xEF/255, b: 0xE8/255 };
const fr = figma.currentPage.children.find(c => c.type === 'FRAME' && c.name.startsWith('201 · ') && c.name.includes('final') && !c.name.includes('baseline'));
const g = fr.children.find(c => c.name === 'Чертёж');
const mkVec = (data, x, y, stroke, w, fill, name) => { const v = figma.createVector(); g.appendChild(v); v.vectorPaths = [{ windingRule: 'NONZERO', data }]; v.x = x; v.y = y; v.strokes = stroke ? [{ type: 'SOLID', color: stroke }] : []; if (stroke) { v.strokeWeight = w; v.strokeCap = 'NONE'; } v.fills = fill ? [{ type: 'SOLID', color: fill }] : []; v.name = name; return v; };
// current band
const rect = g.children.find(n => n.name === 'остекление балкона'); const X0 = rect.x, X1 = rect.x + rect.width, Y0 = rect.y, Y1 = rect.y + rect.height; const H = Y1 - Y0;
const OPEN = [370.6, 452.5];
for (const n of [...g.children]) if (n.name === 'остекление' && n.y >= Y0 - 1 && n.y <= Y1 + 1) n.remove();
rect.remove();
const segs = [[X0, OPEN[0]], [OPEN[1], X1]];
for (const [a, b] of segs) { mkVec(`M 0 0 L ${(b - a).toFixed(2)} 0 L ${(b - a).toFixed(2)} ${H.toFixed(2)} L 0 ${H.toFixed(2)} Z`, a, Y0, null, 0, WHITE, 'окно балкона'); for (const f of [0, 0.5, 1]) mkVec(`M 0 0 L ${(b - a).toFixed(2)} 0`, a, Y0 + H * f, INK, 1.2, null, 'остекление'); }
// jambs at the opening sides: 4 px white boxes with ink outline
for (const x of [OPEN[0] - 4, OPEN[1]]) mkVec(`M 0 0 L 4 0 L 4 ${H.toFixed(2)} L 0 ${H.toFixed(2)} Z`, x, Y0, INK, 1.2, WHITE, 'скобка');
// floor through the opening: extend balcony floor up to the band top
const bf = fr.children.find(c => c.name === 'Пол балкона'); const bx = bf.x, bw = bf.width, bBottom = bf.y + bf.height; bf.vectorPaths = [{ windingRule: 'EVENODD', data: `M 0 0 L ${bw.toFixed(1)} 0 L ${bw.toFixed(1)} ${(bBottom - Y0).toFixed(1)} L 0 ${(bBottom - Y0).toFixed(1)} Z` }]; bf.x = bx; bf.y = Y0;
// wedge in the bottom-left corner: from the column's right edge along the band bottom
const col = g.children.find(n => n.name === 'колонна' && n.x < 200 && n.y > 800); const cx = col.x + col.width;
mkVec(`M 0 0 L 0 ${H.toFixed(2)} L 35 ${H.toFixed(2)} Z`, cx, Y0, null, 0, INK, 'клин');
const shots = []; for (const [x, y, w, h] of [[85, 820, 140, 105], [90, 870, 430, 50]]) { const t = figma.createFrame(); fr.appendChild(t); t.name = 'tmp'; t.fills = []; t.resize(w, h); t.x = x; t.y = y; t.clipsContent = true; shots.push(await t.screenshot({ scale: 3, contentsOnly: false })); t.remove(); }
return { band: [X0, X1, Y0, Y1], wedgeAt: cx, shots };