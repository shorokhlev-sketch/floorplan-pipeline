// Final 201, 2026-09-11 (по ответу ревьюера): клин убрать; колонна в углу = сплошной ink; вернуть стену архитектора 131–167;
// одна полоса остекления 167–370,6 на высоте белых брусков PDF (887,4–902,4) с 3 линиями + скобка у проёма; вторую полосу и
// линии, идущие сквозь проём, удалить; справа от проёма у архитектора глухая стена (121:41) — окно там не рисуется.
// Правило универсальное — см. RESUME.md раздел 4 п.8. Применяется к живому фрейму 121:2, id узлов — конкретные (снято 2026-09-11).
const page = figma.root.children.find(p => p.name === 'Планировки');
await figma.setCurrentPageAsync(page);
const INK = { r: 0x18/255, g: 0x2E/255, b: 0x46/255 }, WHITE = { r: 1, g: 1, b: 1 };
const fin = await figma.getNodeByIdAsync('121:2');
const g = fin.children.find(c => c.name === 'Чертёж');
const byId = async (id) => { const n = await figma.getNodeByIdAsync(id); if (!n) throw new Error('missing ' + id); return n; };
// 1) удалить: клин, старая полоса + линии, вторая полоса (обе части) + линии, правая скобка, линии-кромки на всю ширину (шли сквозь проём), линии на правой грани колонны
const rm = ['129:12', '125:42', '125:43', '125:44', '129:2', '129:3', '129:4', '129:5', '129:6', '129:7', '129:8', '129:9', '129:11', '121:21', '121:11', '121:661', '121:16'];
for (const id of rm) (await byId(id)).remove();
// 2) колонна -> сплошной ink, без обводки (остаётся VECTOR)
const col = await byId('121:657'); col.fills = [{ type: 'SOLID', color: INK }]; col.strokes = [];
// 3) стена архитектора между колонной и началом окна
const mk = (data, x, y, stroke, w, fill, name) => { const v = figma.createVector(); g.appendChild(v); v.vectorPaths = [{ windingRule: 'NONZERO', data }]; v.x = x; v.y = y; v.strokes = stroke ? [{ type: 'SOLID', color: stroke }] : []; if (stroke) { v.strokeWeight = w; v.strokeCap = 'NONE'; } v.fills = fill ? [{ type: 'SOLID', color: fill }] : []; v.name = name; return v; };
const Y0 = 887.4, H = 15, X0 = 166.8, X1 = 370.6, W = X1 - X0;
mk(`M 0 0 L 35.8 0 L 35.8 ${H} L 0 ${H} Z`, 131, Y0, null, 0, INK, 'стена');
// 4) одна полоса остекления: белая, три линии ink 1.2 (верх/середина/низ)
mk(`M 0 0 L ${W.toFixed(1)} 0 L ${W.toFixed(1)} ${H} L 0 ${H} Z`, X0, Y0, null, 0, WHITE, 'окно балкона');
for (const f of [0, 0.5, 1]) mk(`M 0 0 L ${W.toFixed(1)} 0`, X0, Y0 + H * f, INK, 1.2, null, 'остекление');
// 5) скобка у проёма: 4×15, поверх полосы
const jamb = await byId('129:10'); jamb.vectorPaths = [{ windingRule: 'NONZERO', data: `M 0 0 L 4 0 L 4 ${H} L 0 ${H} L 0 0 Z` }]; jamb.x = X1 - 4; jamb.y = Y0; g.appendChild(jamb);
// 6) пол балкона до верха полосы (в проёме бежевый насквозь)
const bf = await byId('121:1307'); const bBottom = bf.y + bf.height; const bx = bf.x, bw = bf.width; bf.vectorPaths = [{ windingRule: 'NONZERO', data: `M 0 0 L ${bw.toFixed(1)} 0 L ${bw.toFixed(1)} ${(bBottom - Y0).toFixed(1)} L 0 ${(bBottom - Y0).toFixed(1)} Z` }]; bf.x = bx; bf.y = Y0;
return { nodes: g.children.length };
