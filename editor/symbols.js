// plan-studio/v3/symbols.js — библиотека символов мебели для плана v3.
// Каждый символ: { label, size: [w, h] по умолчанию (pt), draw(w, h) → [{tag, attrs}] в локальных координатах блока }.
// Цвета в attrs — токены ('ink', 'paper', 'floor', 'muted') или hex; рендерер подставит палитру.
// Перенесено из старого редактора (plan-studio/editor/editor.js SYMS), доводка под эталон — отдельно.

const S = { stroke: 'ink', 'stroke-width': 0.5, fill: 'none' };          // основной контур
const T = { stroke: 'ink', 'stroke-width': 0.35, fill: 'none' };         // тонкая внутренняя линия
const B = { stroke: 'ink', 'stroke-width': 0.5, fill: 'paper' };         // корпус с белой заливкой

const p = (tag, a) => ({ tag, attrs: a });
const rect = (x, y, w, h, a, rx) => p('rect', Object.assign({ x, y, width: w, height: h }, rx ? { rx, ry: rx } : {}, a));
const line = (x1, y1, x2, y2, a) => p('line', Object.assign({ x1, y1, x2, y2 }, a));
const ellipse = (cx, cy, rx, ry, a) => p('ellipse', Object.assign({ cx, cy, rx, ry }, a));
const circle = (cx, cy, r, a) => p('circle', Object.assign({ cx, cy, r }, a));
const path = (d, a) => p('path', Object.assign({ d }, a));

function bedDouble(w, h) {
  const cut = Math.min(w, h) * 0.14;
  const pw = w * 0.42, ph = h * 0.22, margin = w * 0.06, blanketY = h * 0.36;
  return [
    path(`M0,0 L${w},0 L${w},${h - cut} L${w - cut},${h} L0,${h} Z`, B),
    rect(margin, h * 0.06, pw, ph, T, ph * 0.4),
    rect(w - margin - pw, h * 0.06, pw, ph, T, ph * 0.4),
    line(0, blanketY, w, blanketY, T),
  ];
}

function bedSingle(w, h) {
  const cut = Math.min(w, h) * 0.14;
  const pw = w * 0.6, ph = h * 0.22, blanketY = h * 0.36;
  return [
    path(`M0,0 L${w},0 L${w},${h - cut} L${w - cut},${h} L0,${h} Z`, B),
    rect((w - pw) / 2, h * 0.06, pw, ph, T, ph * 0.4),
    line(0, blanketY, w, blanketY, T),
  ];
}

function nightstand(w, h) {
  return [rect(0, 0, w, h, B), circle(w / 2, h * 0.45, Math.min(w, h) * 0.22, T)];
}

function tableRect(w, h) { return [rect(0, 0, w, h, B)]; }
function tableRound(w, h) { return [ellipse(w / 2, h / 2, w / 2, h / 2, B)]; }

function chair(w, h) {
  return [rect(0, 0, w, h, B, Math.min(w, h) * 0.15), line(w * 0.15, h * 0.18, w * 0.85, h * 0.18, T)];
}

function sofaLike(w, h, armRatio) {
  const arm = Math.min(w, h) * armRatio;
  return [
    rect(0, 0, w, h, B),
    rect(0, 0, arm, h, T),
    rect(w - arm, 0, arm, h, T),
    line(arm, h * 0.3, w - arm, h * 0.3, T),
  ];
}

function wardrobe(w, h) { return [rect(0, 0, w, h, B), line(0, 0, w, h, T)]; }

function kitchenRow(w, h) {
  const cellW = w / 4;
  const out = [rect(0, 0, w, h, B)];
  for (let i = 1; i < 4; i++) out.push(line(cellW * i, 0, cellW * i, h, T));
  out.push(ellipse(cellW * 0.5, h / 2, cellW * 0.3, h * 0.28, T));
  const hobCx = cellW * 2.5, hobCy = h / 2, hobR = Math.min(cellW, h) * 0.14, off = Math.min(cellW, h) * 0.2;
  for (const o of [[-off, -off], [off, -off], [-off, off], [off, off]]) out.push(circle(hobCx + o[0], hobCy + o[1], hobR, T));
  return out;
}

function desk(w, h) {
  const mw = w * 0.3, mh = h * 0.18;
  return [rect(0, 0, w, h, B), rect((w - mw) / 2, h * 0.08, mw, mh, T)];
}

function bathtub(w, h) {
  const r = Math.min(w, h) * 0.4;
  return [rect(0, 0, w, h, B, r), ellipse(w / 2, h / 2, w * 0.5 - w * 0.12, h * 0.5 - h * 0.18, T)];
}

function toilet(w, h) {
  const tankH = h * 0.25;
  return [rect(0, 0, w, tankH, B), ellipse(w / 2, tankH + (h - tankH) / 2, w / 2, (h - tankH) / 2, B)];
}

function sink(w, h) { return [rect(0, 0, w, h, B), ellipse(w / 2, h / 2, w * 0.36, h * 0.32, T)]; }

function shower(w, h) {
  return [rect(0, 0, w, h, B), line(0, 0, w, h, T), circle(w * 0.7, h * 0.7, Math.min(w, h) * 0.1, T)];
}

function washer(w, h) { return [rect(0, 0, w, h, B), circle(w / 2, h * 0.55, Math.min(w, h) * 0.32, T)]; }
function tvStand(w, h) { return [rect(0, 0, w, h, B)]; }

export const SYMBOLS = {
  bed_double: { label: 'Кровать 2-сп.', size: [22, 32], draw: bedDouble },
  bed_single: { label: 'Кровать 1-сп.', size: [14, 32], draw: bedSingle },
  nightstand: { label: 'Тумба', size: [5, 9], draw: nightstand },
  table_rect: { label: 'Стол прямой', size: [20, 10], draw: tableRect },
  table_round: { label: 'Стол круглый', size: [16, 16], draw: tableRound },
  chair: { label: 'Стул', size: [5, 5], draw: chair },
  sofa: { label: 'Диван', size: [24, 9], draw: (w, h) => sofaLike(w, h, 0.12) },
  armchair: { label: 'Кресло', size: [8, 8], draw: (w, h) => sofaLike(w, h, 0.22) },
  wardrobe: { label: 'Шкаф', size: [20, 6], draw: wardrobe },
  kitchen_row: { label: 'Кухня', size: [30, 6], draw: kitchenRow },
  desk: { label: 'Письменный стол', size: [14, 7], draw: desk },
  bathtub: { label: 'Ванна', size: [18, 8], draw: bathtub },
  toilet: { label: 'Унитаз', size: [6, 9], draw: toilet },
  sink: { label: 'Раковина', size: [8, 5], draw: sink },
  shower: { label: 'Душ', size: [9, 9], draw: shower },
  washer: { label: 'Стир. машина', size: [7, 7], draw: washer },
  tv_stand: { label: 'Тумба ТВ', size: [16, 4], draw: tvStand },
};

export const SYMBOL_ORDER = Object.keys(SYMBOLS);
