// Двери по образцу ревьюера (2026-09-11): на торцах стен у проёма — «коробка» (белый прямоугольник в толщину стены × 4 px, обводка ink 1.2
// по центру → визуально чуть шире стены); между коробками — «рамка проёма» (белый брус в толщину стены, обводка 1.2 внутрь, кромки заподлицо
// со стеной); «полотно двери» ink 1.5 от внутреннего угла коробки перпендикулярно стене на L = проём − 2·4; «дуга двери» ink 1.5 ПУНКТИР [6,4]
// четверть окружности с центром в петле до второй коробки. Для выхода на балкон рамки нет (пол насквозь), только коробки + полотно + дуга.
// Полоса остекления: y = y стенных кусков (887–902), линии ink 1.2 внутрь полосы (Y0+0.6, середина, Y1−0.6), x от торца стены до проёма.
// Координаты 201 сняты 2026-09-11 после правок ревьюера. Для других квартир — те же формулы, свои X/Y/T.
const page = figma.root.children.find(p => p.name === 'Планировки');
await figma.setCurrentPageAsync(page);
const INK = { r: 0x18/255, g: 0x2E/255, b: 0x46/255 }, WHITE = { r: 1, g: 1, b: 1 };
const fin = await figma.getNodeByIdAsync('121:2');
const g = fin.children.find(c => c.name === 'Чертёж');
const byId = async (id) => { const n = await figma.getNodeByIdAsync(id); if (!n) throw new Error('missing ' + id); return n; };
const mk = (data, x, y, stroke, w, fill, name, dash) => { const v = figma.createVector(); g.appendChild(v); v.vectorPaths = [{ windingRule: 'NONZERO', data }]; v.x = x; v.y = y; v.strokes = stroke ? [{ type: 'SOLID', color: stroke }] : []; if (stroke) { v.strokeWeight = w; v.strokeCap = 'NONE'; v.strokeJoin = 'MITER'; } if (dash) v.dashPattern = dash; v.fills = fill ? [{ type: 'SOLID', color: fill }] : []; v.name = name; return v; };
const rect = (w, h) => `M 0 0 L ${w.toFixed(2)} 0 L ${w.toFixed(2)} ${h.toFixed(2)} L 0 ${h.toFixed(2)} Z`;
const K = 0.5523; const DASH = [6, 4];
// старые детали дверей (Т-скобки, полотна, дуги)
for (const id of ['121:163', '121:166', '121:167', '121:168', '121:169', '121:170', '121:171', '121:172', '121:173', '121:174', '121:131', '121:142']) (await byId(id)).remove();
// 1) дверь санузла: стена x 287.15–297.14, проём y 185.32–255.26, открывается влево от верхней петли
{ const X = 287.15, T = 9.99, Y0 = 185.32, Y1 = 255.26, J = 4; const L = (Y1 - J) - (Y0 + J);
  mk(rect(T - 1.2, L), X + 0.6, Y0 + J, INK, 1.2, WHITE, 'рамка проёма');
  mk(rect(T, J), X, Y0, INK, 1.2, WHITE, 'коробка'); mk(rect(T, J), X, Y1 - J, INK, 1.2, WHITE, 'коробка');
  const hy = Y0 + J; mk(`M 0 0 L ${L.toFixed(2)} 0`, X - L, hy, INK, 1.5, null, 'полотно двери');
  const kL = K * L; mk(`M 0 0 C 0 ${kL.toFixed(2)} ${(L - kL).toFixed(2)} ${L.toFixed(2)} ${L.toFixed(2)} ${L.toFixed(2)}`, X - L, hy, INK, 1.5, null, 'дуга двери', DASH); }
// 2) входная дверь: верхняя стена y 79.27–87.99, проём x 321.32–421.24, открывается вниз от левой петли
{ const Y = 79.27, T = 8.72, X0 = 321.32, X1 = 421.24, J = 4; const L = (X1 - J) - (X0 + J);
  mk(rect(L, T - 1.2), X0 + J, Y + 0.6, INK, 1.2, WHITE, 'рамка проёма');
  mk(rect(J, T), X0, Y, INK, 1.2, WHITE, 'коробка'); mk(rect(J, T), X1 - J, Y, INK, 1.2, WHITE, 'коробка');
  const hx = X0 + J, hy = Y + T; mk(`M 0 0 L 0 ${L.toFixed(2)}`, hx, hy, INK, 1.5, null, 'полотно двери');
  const kL = K * L; mk(`M ${L.toFixed(2)} 0 C ${L.toFixed(2)} ${kL.toFixed(2)} ${kL.toFixed(2)} ${L.toFixed(2)} 0 ${L.toFixed(2)}`, hx, hy, INK, 1.5, null, 'дуга двери', DASH); }
// 3) выход на балкон: полоса y 887–902, проём x 370.6–457, открывается вверх от правой петли; рамки нет
{ const Y0 = 887, H = 15, X0 = 167, X1 = 370.6, XW = 457, J = 4;
  (await byId('149:2')).y = Y0;
  const band = await byId('149:3'); band.vectorPaths = [{ windingRule: 'NONZERO', data: rect(X1 - X0, H) }]; band.x = X0; band.y = Y0;
  const ys = [Y0 + 0.6, Y0 + H / 2, Y0 + H - 0.6]; let i = 0; for (const id of ['149:4', '149:5', '149:6']) { const ln = await byId(id); ln.vectorPaths = [{ windingRule: 'NONZERO', data: `M 0 0 L ${(X1 - X0).toFixed(2)} 0` }]; ln.x = X0; ln.y = ys[i++]; }
  const jl = await byId('129:10'); jl.vectorPaths = [{ windingRule: 'NONZERO', data: rect(J, H) }]; jl.x = X1 - J; jl.y = Y0; jl.name = 'коробка'; g.appendChild(jl);
  mk(rect(J, H), XW - J, Y0, INK, 1.2, WHITE, 'коробка');
  const hx = XW - J, L = hx - X1, kL = K * L;
  const leaf = await byId('121:114'); leaf.vectorPaths = [{ windingRule: 'NONZERO', data: `M 0 ${L.toFixed(2)} L 0 0` }]; leaf.x = hx; leaf.y = Y0 - L; leaf.strokeWeight = 1.5; leaf.name = 'полотно двери';
  const arc = await byId('121:103'); arc.vectorPaths = [{ windingRule: 'NONZERO', data: `M 0 ${L.toFixed(2)} C 0 ${(L - kL).toFixed(2)} ${(L - kL).toFixed(2)} 0 ${L.toFixed(2)} 0` }]; arc.x = X1; arc.y = Y0 - L; arc.dashPattern = DASH; }
return { nodes: g.children.length };
