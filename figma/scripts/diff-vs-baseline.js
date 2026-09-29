// Diff final 201 (the reviewer's edits) against the hidden baseline: removed, added, moved/resized nodes and top-level changes
const hex = (c) => [c.r, c.g, c.b].map(v => Math.round(v * 255).toString(16).padStart(2, '0')).join('');
const page = figma.currentPage;
const cur = page.children.find(c => c.type === 'FRAME' && c.name.startsWith('201 · ') && c.name.includes('final') && !c.name.includes('baseline'));
const base = page.children.find(c => c.type === 'FRAME' && c.name.includes('201 · ') && c.name.includes('baseline'));
const sig = (n) => `${n.name}|${n.type}|${n.x.toFixed(1)}|${n.y.toFixed(1)}|${n.width.toFixed(1)}|${n.height.toFixed(1)}|${n.fills.length && n.fills[0].type === 'SOLID' ? hex(n.fills[0].color) : ''}|${n.strokes.length && n.strokes[0].type === 'SOLID' ? hex(n.strokes[0].color) : ''}|${n.strokeWeight ? +n.strokeWeight.toFixed(1) : 0}|${n.visible ? 1 : 0}`;
const collect = (fr) => { const m = new Map(); const walk = (node, path) => { for (const c of node.children) { const s = path + '/' + sig(c); m.set(s, (m.get(s) || 0) + 1); if ('children' in c && c.type !== 'TEXT') walk(c, path + '/' + c.name); } }; walk(fr, ''); return m; };
const A = collect(base), B = collect(cur);
const removed = [], added = [];
for (const [s, k] of A) { const kb = B.get(s) || 0; if (kb < k) removed.push(s + (k - kb > 1 ? ` ×${k - kb}` : '')); }
for (const [s, k] of B) { const ka = A.get(s) || 0; if (ka < k) added.push(s + (k - ka > 1 ? ` ×${k - ka}` : '')); }
const top = cur.children.map(c => `${c.name}:${c.type}:${Math.round(c.x)},${Math.round(c.y)} ${Math.round(c.width)}x${Math.round(c.height)} n${('children' in c) ? c.children.length : 0}${c.visible ? '' : ' hidden'}`);
return { top, removedCount: removed.length, addedCount: added.length, removed: removed.slice(0, 80), added: added.slice(0, 80) };