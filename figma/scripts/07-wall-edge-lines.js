// Универсальный: убрать линии-обводки на кромках ink-стен (ревьюер 2026-09-11: «у части стены есть outline тонкая, у части нет — перепад»).
// Кандидат = VECTOR без заливки, с обводкой, осевой отрезок (w<0.6 или h<0.6), длина ≥10 px, лежит в пределах 1.6 px от кромки
// ink-залитого прямоугольника (стена/колонна, w,h>3) и перекрывает его протяжённость ≥60 %. Короче 10 px не трогаем (смеситель у стены и т.п.).
// Имена остекление/ограждение/дуга двери/окно/полотно/скобка — исключены. FRAME_ID подставить.
const FRAME_ID = __FRAME_ID__; // node id of the unit's final frame
const page = figma.root.children.find(p => p.name === 'Планировки');
await figma.setCurrentPageAsync(page);
const hex = (c) => [c.r, c.g, c.b].map(v => Math.round(v * 255).toString(16).padStart(2, '0')).join('');
const fin = await figma.getNodeByIdAsync(FRAME_ID);
const g = fin.children.find(c => c.name === 'Чертёж');
const isInkFill = (n) => n.fills && n.fills.length && n.fills[0].type === 'SOLID' && hex(n.fills[0].color) === '182e46';
const walls = g.children.filter(n => n.type === 'VECTOR' && isInkFill(n) && n.width > 3 && n.height > 3).map(n => ({ id: n.id, x0: n.x, y0: n.y, x1: n.x + n.width, y1: n.y + n.height }));
const SKIP = new Set(['остекление', 'ограждение', 'дуга двери', 'окно/полотно', 'скобка', 'полотно']);
const segs = g.children.filter(n => n.type === 'VECTOR' && !SKIP.has(n.name) && (!n.fills || !n.fills.length) && n.strokes && n.strokes.length && (n.width < 0.6 || n.height < 0.6) && Math.max(n.width, n.height) >= 10);
const TOL = 1.6, EXT = 2.5; const removed = [];
for (const s of segs) {
  const horiz = s.height < 0.6; const a0 = horiz ? s.x : s.y, a1 = horiz ? s.x + s.width : s.y + s.height; const c = horiz ? s.y : s.x; const L = a1 - a0;
  for (const w of walls) {
    const edges = horiz ? [w.y0, w.y1] : [w.x0, w.x1]; const e0 = horiz ? w.x0 : w.y0, e1 = horiz ? w.x1 : w.y1;
    if (!edges.some(e => Math.abs(e - c) <= TOL)) continue;
    const ov = Math.min(a1, e1 + EXT) - Math.max(a0, e0 - EXT); if (ov / L < 0.6) continue;
    removed.push(s.id + ' ' + s.name + ' ' + (horiz ? 'H@' : 'V@') + c.toFixed(1) + ' ' + a0.toFixed(0) + '–' + a1.toFixed(0)); s.remove(); break;
  }
}
return { removedCount: removed.length, removed, nodes: g.children.length };
