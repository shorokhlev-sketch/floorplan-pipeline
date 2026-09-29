// Final 201: extend the balcony railing band (lines + ticks) and the balcony floor to the balcony side walls; zoom both ends
const INK = { r: 0x18/255, g: 0x2E/255, b: 0x46/255 };
const fr = figma.currentPage.children.find(c => c.type === 'FRAME' && c.name.startsWith('201 · ') && c.name.includes('final') && !c.name.includes('baseline'));
const g = fr.children.find(c => c.name === 'Чертёж');
const walls = g.children.filter(n => n.name === 'стена' && n.fills.length && n.height > 60 && n.y + n.height > 1000);
const left = walls.filter(w => w.x < 300).sort((a, b) => b.x - a.x)[0], right = walls.filter(w => w.x >= 300).sort((a, b) => a.x - b.x)[0];
const X0 = left.x, X1 = right.x + right.width;   // под стены
const band = g.children.filter(n => n.name === 'ограждение');
const horiz = band.filter(n => n.width > 100), ticks = band.filter(n => n.width <= 2);
const ys = horiz.map(n => n.y); const yTop = Math.min(...ys), yBot = Math.max(...ys);
for (const n of horiz) { n.vectorPaths = [{ windingRule: 'NONZERO', data: `M 0 0 L ${(X1 - X0).toFixed(2)} 0` }]; n.x = X0; }
const tickXs = ticks.map(t => t.x).sort((a, b) => a - b); const step = 12; let added = 0;
const mkTick = (x) => { const v = figma.createVector(); g.appendChild(v); v.vectorPaths = [{ windingRule: 'NONZERO', data: `M 0 0 L 0 ${(yBot - yTop).toFixed(2)}` }]; v.x = x; v.y = yTop; v.strokes = [{ type: 'SOLID', color: INK }]; v.strokeWeight = 1.2; v.strokeCap = 'NONE'; v.fills = []; v.name = 'ограждение'; added++; };
for (let x = tickXs[0] - step; x > left.x + left.width + 2; x -= step) mkTick(x);
for (let x = tickXs[tickXs.length - 1] + step; x < right.x - 2; x += step) mkTick(x);
const bf = fr.children.find(c => c.name === 'Пол балкона'); const y0 = bf.y, h = bf.height; bf.vectorPaths = [{ windingRule: 'EVENODD', data: `M 0 0 L ${(X1 - X0).toFixed(1)} 0 L ${(X1 - X0).toFixed(1)} ${h.toFixed(1)} L 0 ${h.toFixed(1)} Z` }]; bf.x = X0; bf.y = y0;
const shots = []; for (const [x, y, w, h2] of [[80, 1010, 120, 50], [420, 1010, 120, 50]]) { const t = figma.createFrame(); fr.appendChild(t); t.name = 'tmp'; t.fills = []; t.resize(w, h2); t.x = x; t.y = y; t.clipsContent = true; shots.push(await t.screenshot({ scale: 3, contentsOnly: false })); t.remove(); }
return { X0, X1, yTop, yBot, ticksAdded: added, shots };