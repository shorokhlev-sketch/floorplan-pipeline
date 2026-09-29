// Final 201: list and fix door bracket elements above the top wall (y 72–84.5 near x 321 / 421), then zoom
const hex = (c) => '#' + [c.r, c.g, c.b].map(v => Math.round(v * 255).toString(16).padStart(2, '0')).join('').toUpperCase();
const fr = figma.currentPage.children.find(c => c.type === 'FRAME' && c.name.startsWith('201 · ') && c.name.includes('final') && !c.name.includes('baseline'));
const g = fr.children.find(c => c.name === 'Чертёж');
const before = [], fixed = [];
for (const n of [...g.children]) { const inX = (n.x >= 314 && n.x + n.width <= 330) || (n.x >= 414 && n.x + n.width <= 428); if (!inX || n.y + n.height < 70 || n.y > 90) continue; before.push(`${n.type} ${n.name} [${n.x.toFixed(1)},${n.y.toFixed(1)} ${n.width.toFixed(1)}x${n.height.toFixed(1)}] f${n.fills.length ? hex(n.fills[0].color) : '-'} s${n.strokes.length ? hex(n.strokes[0].color) : '-'}`);
  if (n.y < 78 && n.y + n.height <= 84.5) { const isV = n.height > n.width; if (n.fills.length) { n.resize(n.width, 8.7); n.y = 79.3; } else if (isV) { n.resize(Math.max(n.width, 0.01), 8.7); n.y = 79.3; } else { n.y = Math.abs(n.y - 73) < 1.5 ? 79.3 : 88; } fixed.push(n.name); }
  else if (n.height <= 4 && n.width < 0.5 && n.y >= 78 && n.y <= 90) { n.remove(); fixed.push('del ' + n.name); } }
const t = figma.createFrame(); fr.appendChild(t); t.name = 'tmp'; t.fills = []; t.resize(190, 90); t.x = 280; t.y = 55; t.clipsContent = true; const shot = await t.screenshot({ scale: 2.5, contentsOnly: false }); t.remove();
return { before, fixed, shot };