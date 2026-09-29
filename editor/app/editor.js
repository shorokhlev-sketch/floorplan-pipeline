// Редактор планов v3 — правит документ напрямую, рисует через ../render.js.
import { renderInto, wallGeom, wallOpenings, wallQuad, areaM2, docBBox, M_PER_PT } from '../render.js';
import { SYMBOLS, SYMBOL_ORDER } from '../symbols.js';
import * as S from './state.js';
import { snapPoint, rawPoints, projectOnWall, snapGrid } from './snap.js';
import { faceDragThickness, refFaceDrag, endDragPoint } from './wallops.js';
import * as IO from './io.js';

const $ = (id) => document.getElementById(id);
const r2 = (v) => Math.round(v * 100) / 100;
const fmtArea = (m2) => m2.toFixed(1).replace('.', ',');

const KIND_RU = { exterior: 'наружная', corridor: 'к коридору', party: 'к соседу', partition: 'перегородка', balcony: 'балкон', railing: 'ограждение' };
const OPEN_RU = { door: 'дверь', window: 'окно', sliding: 'раздвижная', passage: 'проём' };
const DEFAULT_OPEN_W = { door: 11.5, window: 12, sliding: 18, passage: 11.5 };
const TOOL_HINT = {
  select: 'Клик — выбрать, Shift — добавить. Тяните узлы, стены (за середину — перпендикулярно), проёмы вдоль стены, блоки. Пусто — панорама. Стена: углы — двигать, середины длинных граней — толщина, торцы — длина, центр — перенос.',
  wall: 'Клики ставят узлы цепочки стен. Enter/Esc или двойной клик — закончить. Backspace — убрать последнюю. Углы 45° и вертикаль/горизонталь ловятся сами, Shift — жёстко, Alt — без привязки.',
  split: 'Клик по стене — разрез в этой точке (появится узел).',
  door: 'Клик по стене — дверь в этом месте. Потом F — петля, G — распах, ширина в свойствах.',
  window: 'Клик по стене — окно в этом месте.',
  cut: 'Два клика через область — разрезать её на две комнаты. Площади посчитаются сами.',
  pen: 'Рисуйте красным поверх плана — замечания заказчику. Не печатаются.',
  text: 'Клик — подпись-замечание.',
  hand: 'Таскайте план. Колесо — зум.',
};

const st = {
  index: null, unit: null, doc: null, raw: null, rawPts: [], style: {},
  hist: new S.History(200),
  sel: new Set(),
  tool: 'select',
  view: { x: 0, y: 0, w: 100, h: 100 },
  layers: { pdf: true, pdfOp: 0.45, raw: false, furniture: true, labels: false, notes: true, nodes: true, flip: 0 },
  preset: { t: 1.4, kind: 'partition' },
  draft: null, cut: null, cursor: null,
  drag: null, dirty: false, hover: null,
  keys: { space: false, alt: false, shift: false },
};

const dom = {
  stage: $('stage'), svg: $('svg'), gPlan: $('g-plan'), gRaw: $('g-raw'), gOv: $('g-overlay'), pdf: $('pdf'),
  units: $('units'), tools: $('tools'), hint: $('tool-hint'), props: $('props'), areas: $('areas'), palette: $('palette'),
  title: $('title'), status: $('status'), coords: $('coords'), zoom: $('zoom'), textInput: $('text-input'),
};

// ---------------------------------------------------------------- вид

function stageRect() { return dom.stage.getBoundingClientRect(); }
function scale() { return stageRect().width / st.view.w; }
function applyView() {
  const r = stageRect();
  st.view.h = st.view.w * (r.height / r.width);
  dom.svg.setAttribute('viewBox', `${st.view.x} ${st.view.y} ${st.view.w} ${st.view.h}`);
  dom.zoom.textContent = Math.round(scale() * 100 / 3.2) + '%';
  drawOverlay();
}
function screenToPlan(cx, cy) {
  const r = stageRect();
  const k = st.view.w / r.width;
  return [st.view.x + (cx - r.left) * k, st.view.y + (cy - r.top) * k];
}
function fitView() {
  const b = st.doc.bbox || docBBox(st.doc);
  const r = stageRect();
  const pad = 1.06;
  const wByW = b.w * pad, wByH = b.h * pad * (r.width / r.height);
  st.view.w = Math.max(wByW, wByH);
  st.view.h = st.view.w * (r.height / r.width);
  st.view.x = b.x0 + b.w / 2 - st.view.w / 2;
  st.view.y = b.y0 + b.h / 2 - st.view.h / 2;
  applyView();
}
dom.stage.addEventListener('wheel', (e) => {
  e.preventDefault();
  const p = screenToPlan(e.clientX, e.clientY);
  const f = e.deltaY > 0 ? 1.1 : 0.9;
  const nw = Math.min(4000, Math.max(8, st.view.w * f));
  const k = nw / st.view.w;
  st.view.x = p[0] - (p[0] - st.view.x) * k;
  st.view.y = p[1] - (p[1] - st.view.y) * k;
  st.view.w = nw;
  applyView();
}, { passive: false });
window.addEventListener('resize', applyView);

// ---------------------------------------------------------------- рендер

function redraw() {
  if (!st.doc) return;
  renderInto(dom.gPlan, st.doc, st.style, {
    hit: true, furniture: st.layers.furniture, labels: st.layers.labels, notes: st.layers.notes, paper: true,
    bbox: st.doc.bbox,
  });
  dom.gPlan.style.display = st.layers.flip === 1 ? 'none' : '';
  const u = st.doc.underlay || st.doc.bbox;
  dom.pdf.setAttribute('href', IO.TRUTH + (u.src || ('unit-' + st.doc.unit + '-pdf.png')));
  dom.pdf.setAttribute('x', u.x0); dom.pdf.setAttribute('y', u.y0);
  dom.pdf.setAttribute('width', u.w); dom.pdf.setAttribute('height', u.h);
  const showPdf = st.layers.flip === 1 ? true : (st.layers.flip === 2 ? false : st.layers.pdf);
  dom.pdf.style.display = showPdf ? '' : 'none';
  dom.pdf.setAttribute('opacity', st.layers.flip === 1 ? 1 : st.layers.pdfOp);
  dom.gRaw.style.display = st.layers.raw ? '' : 'none';
  drawOverlay();
  renderProps();
  renderAreas();
  updateButtons();
}

function drawRaw() {
  dom.gRaw.innerHTML = '';
  if (!st.raw) return;
  const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  g.setAttribute('transform', `translate(${st.raw.crop.x0} ${st.raw.crop.y0})`);
  const frag = document.createDocumentFragment();
  for (const pr of st.raw.prims) {
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', pr.d);
    p.setAttribute('class', (pr.role ? '' : 'dropped') + (pr.type === 'f' && pr.role ? ' f' : ''));
    frag.appendChild(p);
  }
  g.appendChild(frag);
  dom.gRaw.appendChild(g);
}

const NS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs, cls) {
  const e = document.createElementNS(NS, tag);
  for (const k of Object.keys(attrs)) e.setAttribute(k, attrs[k]);
  if (cls) e.setAttribute('class', cls);
  return e;
}
function pxPt(px) { return px / scale(); }

function selKinds() {
  const m = {};
  for (const s of st.sel) { const [k, id] = s.split(':'); (m[k] = m[k] || []).push(id); }
  return m;
}

function drawOverlay() {
  const ov = dom.gOv;
  ov.innerHTML = '';
  if (!st.doc) return;
  const doc = st.doc;
  const k = selKinds();
  const hs = pxPt(4);   // половина размера ручки в pt
  // выделение
  for (const id of k.wall || []) {
    const w = S.wallById(doc, id); if (!w) continue;
    const q = wallQuad(wallGeom(doc, w), 0, wallGeom(doc, w).L);
    ov.appendChild(svgEl('path', { d: 'M ' + q.map((p) => p.join(' ')).join(' L ') + ' Z' }, 'sel-outline'));
  }
  for (const id of k.block || []) {
    const b = S.blockById(doc, id); if (!b) continue;
    ov.appendChild(svgEl('rect', { x: b.x, y: b.y, width: b.w, height: b.h, transform: blockTf(b) }, 'sel-outline'));
  }
  for (const id of k.column || []) {
    const c = S.columnById(doc, id); if (!c) continue;
    ov.appendChild(svgEl('rect', { x: c.x, y: c.y, width: c.w, height: c.h }, 'sel-outline'));
  }
  for (const id of k.area || []) {
    const r = S.areaById(doc, id); if (!r) continue;
    ov.appendChild(svgEl('path', { d: 'M ' + r.poly.map((p) => p.join(' ')).join(' L ') + ' Z' }, 'sel-outline'));
    r.poly.forEach((p, i) => ov.appendChild(svgEl('rect', { x: p[0] - hs, y: p[1] - hs, width: 2 * hs, height: 2 * hs, 'data-h': `areav:${r.id}:${i}` }, 'h-areav')));
  }
  for (const id of k.opening || []) {
    const o = S.openingById(doc, id); if (!o) continue;
    const w = S.wallById(doc, o.wall); if (!w) continue;
    const g = wallGeom(doc, w);
    const p = (s, off) => [g.a[0] + g.u[0] * s + g.n[0] * off, g.a[1] + g.u[1] * s + g.n[1] * off].join(' ');
    ov.appendChild(svgEl('path', { d: `M ${p(o.pos, -hs)} L ${p(o.pos + o.width, -hs)} L ${p(o.pos + o.width, g.t + hs)} L ${p(o.pos, g.t + hs)} Z`, 'data-h': 'open:' + o.id }, 'h-open'));
  }
  // узлы
  if (st.layers.nodes && (st.tool === 'select' || st.tool === 'wall')) {
    const deg = {};
    for (const w of doc.walls) { deg[w.a] = (deg[w.a] || 0) + 1; deg[w.b] = (deg[w.b] || 0) + 1; }
    const selNodes = new Set();
    for (const id of k.wall || []) { const w = S.wallById(doc, id); if (w) { selNodes.add(w.a); selNodes.add(w.b); } }
    const s0 = pxPt(2.5);
    for (const nid of Object.keys(doc.nodes)) {
      const p = doc.nodes[nid];
      const h = selNodes.has(nid) ? hs : s0;
      ov.appendChild(svgEl('rect', { x: p[0] - h, y: p[1] - h, width: 2 * h, height: 2 * h, 'data-h': 'node:' + nid }, 'h-node' + (deg[nid] > 1 ? ' shared' : '')));
    }
    for (const id of k.wall || []) {
      const w = S.wallById(doc, id); if (!w) continue;
      const g = wallGeom(doc, w);
      const deg = Math.atan2(g.u[1], g.u[0]) * 180 / Math.PI;
      const midS = g.L / 2;
      const at = (s, off) => [g.a[0] + g.u[0] * s + g.n[0] * off, g.a[1] + g.u[1] * s + g.n[1] * off];
      // грани (толщина): прямоугольники 10×5 экранных px вдоль стены, в середине длины
      const fl = pxPt(5), ft = pxPt(2.5);
      for (const off of [0, g.t]) {
        const [cx, cy] = at(midS, off);
        ov.appendChild(svgEl('rect', { x: cx - fl, y: cy - ft, width: fl * 2, height: ft * 2, transform: `rotate(${deg} ${cx} ${cy})`, 'data-h': `wallface:${w.id}:${off === 0 ? 0 : 1}` }, 'h-face'));
      }
      // торцы (длина): квадраты 6px у соответствующего узла, на середине толщины
      const es = pxPt(3);
      for (const which of ['a', 'b']) {
        const [cx, cy] = at(which === 'a' ? 0 : g.L, g.t / 2);
        ov.appendChild(svgEl('rect', { x: cx - es, y: cy - es, width: es * 2, height: es * 2, transform: `rotate(${deg} ${cx} ${cy})`, 'data-h': `wallend:${w.id}:${which}` }, 'h-end'));
      }
      // центр (перенос стены целиком)
      const [ccx, ccy] = at(midS, g.t / 2);
      ov.appendChild(svgEl('circle', { cx: ccx, cy: ccy, r: hs, 'data-h': 'wallcenter:' + w.id }, 'h-center'));
    }
  }
  // якоря подписей — только у выбранных областей, и только когда слой подписей включён
  if (st.layers.labels) {
    for (const id of k.area || []) {
      const r = S.areaById(doc, id);
      if (!r || !r.label) continue;
      const a = pxPt(3);
      ov.appendChild(svgEl('path', { d: `M ${r.label.x - a} ${r.label.y} L ${r.label.x + a} ${r.label.y} M ${r.label.x} ${r.label.y - a} L ${r.label.x} ${r.label.y + a}`, 'data-h': 'label:' + r.id }, 'h-label'));
      ov.appendChild(svgEl('rect', { x: r.label.x - a * 1.5, y: r.label.y - a * 3, width: a * 10, height: a * 4.5, fill: 'transparent', 'data-h': 'label:' + r.id, style: 'cursor:move' }));
    }
  }
  // черновик стены
  if (st.draft && st.draft.pts.length) {
    const pts = st.draft.pts.slice();
    if (st.cursor) pts.push(st.cursor);
    if (pts.length > 1) {
      for (let i = 0; i + 1 < pts.length; i++) {
        const g = { a: pts[i], u: null, n: null, L: 0, t: st.preset.t };
        const dx = pts[i + 1][0] - pts[i][0], dy = pts[i + 1][1] - pts[i][1];
        g.L = Math.hypot(dx, dy) || 1e-9; g.u = [dx / g.L, dy / g.L]; g.n = [-g.u[1], g.u[0]];
        const q = wallQuad(g, 0, g.L);
        ov.appendChild(svgEl('path', { d: 'M ' + q.map((p) => p.join(' ')).join(' L ') + ' Z' }, 'draft-band'));
      }
      ov.appendChild(svgEl('path', { d: 'M ' + pts.map((p) => p.join(' ')).join(' L ') }, 'draft'));
    }
  }
  if (st.cut && st.cursor) ov.appendChild(svgEl('line', { x1: st.cut[0], y1: st.cut[1], x2: st.cursor[0], y2: st.cursor[1] }, 'cut-line'));
  // направляющие привязки
  for (const gd of st.guides || []) {
    if (gd.kind === 'node' || gd.kind === 'raw' || gd.kind === 'corner') ov.appendChild(svgEl('circle', { cx: gd.p[0], cy: gd.p[1], r: pxPt(4) }, 'guide-dot'));
    else if (gd.kind === 'angle') {
      let [x1, y1] = gd.p, [x2, y2] = gd.q;
      if (gd.deg % 90 === 0) {
        const gx = x2 - x1, gy = y2 - y1, gl = Math.hypot(gx, gy) || 1e-9;
        const ux = gx / gl, uy = gy / gl;
        x1 -= ux * 30; y1 -= uy * 30; x2 += ux * 30; y2 += uy * 30;
      }
      ov.appendChild(svgEl('line', { x1, y1, x2, y2 }, 'guide'));
    }
    else ov.appendChild(svgEl('line', { x1: gd.p[0], y1: gd.p[1], x2: gd.q[0], y2: gd.q[1] }, 'guide'));
  }
}

function blockTf(b) {
  const cx = b.x + b.w / 2, cy = b.y + b.h / 2;
  return `translate(${cx} ${cy}) ${b.rot ? 'rotate(' + b.rot + ')' : ''} ${b.mirror ? 'scale(-1 1)' : ''} translate(${-cx} ${-cy})`;
}

// ---------------------------------------------------------------- свойства

function propRow(label, html) { return `<div class="prop"><span>${label}</span>${html}</div>`; }
function optionList(map, cur) { return Object.keys(map).map((k) => `<option value="${k}" ${k === cur ? 'selected' : ''}>${map[k]}</option>`).join(''); }

/** Пишет расчёт по полигону в подпись области; если подписи нет — создаёт с якорем в центроиде. */
function takeAreaLabel(r) {
  const t = fmtArea(areaM2(r.poly));
  if (!r.label) { const c = S.centroid(r.poly); r.label = { text: t, x: r2(c[0] - 6), y: r2(c[1] + 2) }; }
  else r.label.text = t;
}

function renderProps() {
  const doc = st.doc, k = selKinds();
  const n = st.sel.size;
  if (!doc || !n) { dom.props.innerHTML = `<div class="faint">Ничего не выбрано. Квартира ${doc ? doc.unit : ''}: стен ${doc ? doc.walls.length : 0}, проёмов ${doc ? doc.openings.length : 0}, блоков ${doc ? doc.blocks.length : 0}.</div>`; return; }
  let h = '';
  if (n > 1) h += `<div class="title">Выбрано: ${n}</div>`;
  if (k.wall && k.wall.length === 1) {
    const w = S.wallById(doc, k.wall[0]); const g = wallGeom(doc, w);
    h += `<div class="title">Стена ${w.id}</div>`
      + propRow('длина', `<span>${g.L.toFixed(2)} pt · ${(g.L * M_PER_PT).toFixed(2)} м</span>`)
      + propRow('толщина t, pt', `<input type="number" step="0.1" min="0.3" data-prop="wall.t" value="${w.t}">`)
      + propRow('тип', `<select data-prop="wall.kind">${optionList(KIND_RU, w.kind)}</select>`)
      + propRow('проёмов', `<span>${wallOpenings(doc, w.id).length}</span>`)
      + `<div class="row"><button data-act="flipSide">Сторона <kbd>X</kbd></button><button data-act="joinSel" ${k.wall.length === 1 ? 'disabled' : ''}>Склеить <kbd>J</kbd></button></div>`;
  } else if (k.wall && k.wall.length > 1) {
    h += propRow('стен', `<span>${k.wall.length}</span>`)
      + propRow('толщина t, pt', `<input type="number" step="0.1" min="0.3" data-prop="wall.t" value="">`)
      + propRow('тип', `<select data-prop="wall.kind"><option value="">—</option>${optionList(KIND_RU, '')}</select>`)
      + `<div class="row"><button data-act="joinSel">Склеить две <kbd>J</kbd></button></div>`;
  }
  if (k.opening && k.opening.length === 1) {
    const o = S.openingById(doc, k.opening[0]);
    h += `<div class="title">${OPEN_RU[o.kind] || o.kind} ${o.id}</div>`
      + propRow('тип', `<select data-prop="open.kind">${optionList(OPEN_RU, o.kind)}</select>`)
      + propRow('ширина, pt', `<input type="number" step="0.1" min="1" data-prop="open.width" value="${o.width}">`)
      + propRow('от узла a, pt', `<input type="number" step="0.1" min="0" data-prop="open.pos" value="${o.pos}">`)
      + propRow('ширина, м', `<span>${(o.width * M_PER_PT).toFixed(2)}</span>`);
    if (o.kind === 'door') h += `<div class="row"><button data-act="flipHinge">Петля: ${o.hinge || 'a'} <kbd>F</kbd></button><button data-act="flipSwing">Распах: ${o.swing === 1 ? 'n' : '−n'} <kbd>G</kbd></button></div>`;
  }
  if (k.block && k.block.length === 1) {
    const b = S.blockById(doc, k.block[0]);
    const symOpts = `<option value="raw" ${b.sym === 'raw' ? 'selected' : ''}>из PDF</option>` + SYMBOL_ORDER.map((s) => `<option value="${s}" ${b.sym === s ? 'selected' : ''}>${SYMBOLS[s].label}</option>`).join('');
    h += `<div class="title">Блок ${b.id} · ${b.role || 'furniture'}</div>`
      + propRow('символ', `<select data-prop="block.sym">${symOpts}</select>`)
      + propRow('размер, pt', `<span><input type="number" step="0.5" min="1" data-prop="block.w" value="${b.w}"> × <input type="number" step="0.5" min="1" data-prop="block.h" value="${b.h}"></span>`)
      + propRow('размер, м', `<span>${(b.w * M_PER_PT).toFixed(2)} × ${(b.h * M_PER_PT).toFixed(2)}</span>`)
      + propRow('поворот', `<select data-prop="block.rot">${[0, 90, 180, 270].map((r) => `<option value="${r}" ${b.rot === r ? 'selected' : ''}>${r}°</option>`).join('')}</select>`)
      + `<div class="row"><button data-act="rotate">Повернуть <kbd>R</kbd></button><button data-act="mirror">Зеркало <kbd>M</kbd></button><button data-act="dup">Дубль <kbd>⌘D</kbd></button></div>`;
    if (b.sym === 'raw') h += `<div class="faint">Сырые пути PDF (${(b.paths || []).length}). Замените символом из списка — размер сохранится.</div>`;
  }
  if (k.area && k.area.length === 1) {
    const r = S.areaById(doc, k.area[0]);
    h += `<div class="title">Область ${r.id}</div>`
      + propRow('тип', `<select data-prop="area.kind">${optionList({ room: 'комната', balcony: 'балкон', shaft: 'шахта' }, r.kind)}</select>`)
      + propRow('название', `<input type="text" data-prop="area.name" value="${(r.name || '').replace(/"/g, '&quot;')}" style="width:140px">`)
      + `<div class="row"><button data-act="labelCenter">Якорь подписи в центр</button></div>`;
  }
  if (k.column && k.column.length === 1) {
    const c = S.columnById(doc, k.column[0]);
    h += `<div class="title">Колонна ${c.id}</div>`
      + propRow('размер, pt', `<span><input type="number" step="0.1" min="1" data-prop="col.w" value="${c.w}"> × <input type="number" step="0.1" min="1" data-prop="col.h" value="${c.h}"></span>`);
  }
  h += `<div class="row"><button data-act="delete" class="is-danger">Удалить <kbd>⌫</kbd></button><button data-act="deselect">Снять выделение <kbd>Esc</kbd></button></div>`;
  dom.props.innerHTML = h;
}

// ---------------------------------------------------------------- площади (таблица всех областей)

const AREA_KIND_SHORT = { room: 'комната', balcony: 'балкон', shaft: 'шахта' };

function renderAreas() {
  const doc = st.doc;
  if (!doc) { dom.areas.innerHTML = ''; return; }
  const selArea = new Set(selKinds().area || []);
  let sumRoom = 0, sumBalcony = 0;
  let rows = '';
  for (const r of doc.areas) {
    const auto = areaM2(r.poly);
    if (r.kind === 'room') sumRoom += auto;
    else if (r.kind === 'balcony') sumBalcony += auto;
    rows += `<tr class="${selArea.has(r.id) ? 'is-on' : ''}" data-area="${r.id}">`
      + `<td class="areas-kind">${AREA_KIND_SHORT[r.kind] || r.kind}</td>`
      + `<td><input type="text" data-aprop="name" data-id="${r.id}" value="${(r.name || '').replace(/"/g, '&quot;')}" style="width:90px"></td>`
      + `<td><input type="text" data-aprop="label" data-id="${r.id}" value="${(r.label ? r.label.text : '').replace(/"/g, '&quot;')}" style="width:56px" placeholder="—"></td>`
      + `<td class="areas-auto">${fmtArea(auto)}</td>`
      + `<td><button data-aact="take" data-id="${r.id}">взять</button></td>`
      + `</tr>`;
  }
  const total = `<div class="areas-total">комнаты ${fmtArea(sumRoom)} м² · балконы ${fmtArea(sumBalcony)} м²</div>`;
  dom.areas.innerHTML = doc.areas.length
    ? `<table class="areas-table"><tbody>${rows}</tbody></table>${total}`
    : '<div class="faint">Областей нет</div>';
}

dom.areas.addEventListener('change', (e) => {
  const t = e.target; const prop = t.dataset.aprop; if (!prop) return;
  const id = t.dataset.id;
  commit(() => {
    const r = S.areaById(st.doc, id); if (!r) return;
    if (prop === 'name') r.name = t.value;
    if (prop === 'label') {
      const v = t.value;
      if (!v) r.label = null;
      else if (!r.label) { const c = S.centroid(r.poly); r.label = { text: v, x: r2(c[0] - 6), y: r2(c[1] + 2) }; }
      else r.label.text = v;
    }
  });
});
dom.areas.addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-aact]');
  if (btn) { commit(() => takeAreaLabel(S.areaById(st.doc, btn.dataset.id))); return; }
  if (e.target.closest('input')) return;
  const tr = e.target.closest('tr[data-area]'); if (!tr) return;
  st.sel.clear(); st.sel.add('area:' + tr.dataset.area);
  redraw();
});

dom.props.addEventListener('change', (e) => {
  const t = e.target; const prop = t.dataset.prop; if (!prop) return;
  const doc = st.doc, k = selKinds();
  commit(() => {
    const v = t.type === 'number' ? parseFloat(t.value) : t.value;
    if (prop === 'wall.t' && !isNaN(v)) for (const id of k.wall || []) { const w = S.wallById(doc, id); w.t = r2(Math.max(0.3, v)); }
    if (prop === 'wall.kind' && v) for (const id of k.wall || []) S.wallById(doc, id).kind = v;
    if (prop.startsWith('open.')) {
      const o = S.openingById(doc, k.opening[0]); const w = S.wallById(doc, o.wall); const g = wallGeom(doc, w);
      if (prop === 'open.kind') { o.kind = v; if (v === 'door' && !o.hinge) { o.hinge = 'a'; o.swing = -1; } }
      if (prop === 'open.width' && !isNaN(v)) o.width = r2(Math.min(g.L, Math.max(1, v)));
      if (prop === 'open.pos' && !isNaN(v)) o.pos = r2(Math.max(0, Math.min(g.L - o.width, v)));
      S.clampOpenings(doc, w);
    }
    if (prop.startsWith('block.')) {
      const b = S.blockById(doc, k.block[0]);
      if (prop === 'block.sym') {
        if (v !== 'raw' && b.sym === 'raw') { b._rawPaths = b.paths; b.paths = []; }
        if (v === 'raw' && b._rawPaths) { b.paths = b._rawPaths; delete b._rawPaths; }
        b.sym = v;
        if (v !== 'raw' && !(b.w > 1 && b.h > 1)) { [b.w, b.h] = SYMBOLS[v].size; }
      }
      if (prop === 'block.w' && !isNaN(v)) b.w = r2(Math.max(1, v));
      if (prop === 'block.h' && !isNaN(v)) b.h = r2(Math.max(1, v));
      if (prop === 'block.rot') b.rot = parseInt(v, 10) || 0;
    }
    if (prop.startsWith('area.')) {
      const r = S.areaById(doc, k.area[0]);
      if (prop === 'area.kind') r.kind = v;
      if (prop === 'area.name') r.name = v;
      if (prop === 'area.label') {
        if (!v) r.label = null;
        else { if (!r.label) { const c = S.centroid(r.poly); r.label = { text: v, x: r2(c[0] - 6), y: r2(c[1] + 2) }; } else r.label.text = v; }
      }
    }
    if (prop.startsWith('col.')) {
      const c = S.columnById(doc, k.column[0]);
      if (prop === 'col.w' && !isNaN(v)) c.w = r2(v); if (prop === 'col.h' && !isNaN(v)) c.h = r2(v);
    }
  });
});
dom.props.addEventListener('click', (e) => {
  const b = e.target.closest('button[data-act]'); if (!b) return;
  runAction(b.dataset.act);
});

function runAction(act) {
  const doc = st.doc, k = selKinds();
  switch (act) {
    case 'delete': return deleteSelection();
    case 'deselect': st.sel.clear(); return redraw();
    case 'flipSide': return commit(() => { for (const id of k.wall || []) S.flipWallSide(doc, S.wallById(doc, id)); });
    case 'joinSel': {
      if (!k.wall || k.wall.length !== 2) return setStatus('Выберите две коллинеарные стены с общим узлом', true);
      return commit(() => {
        const w = S.joinWalls(doc, k.wall[0], k.wall[1]);
        if (!w) setStatus('Не склеилось: нет общего узла или не коллинеарны', true);
        else { st.sel.clear(); st.sel.add('wall:' + w.id); }
      });
    }
    case 'flipHinge': return commit(() => { for (const id of k.opening || []) { const o = S.openingById(doc, id); if (o.kind === 'door') o.hinge = o.hinge === 'b' ? 'a' : 'b'; } });
    case 'flipSwing': return commit(() => { for (const id of k.opening || []) { const o = S.openingById(doc, id); if (o.kind === 'door') o.swing = o.swing === 1 ? -1 : 1; } });
    case 'rotate': return commit(() => { for (const id of k.block || []) { const b = S.blockById(doc, id); b.rot = ((b.rot || 0) + 90) % 360; } });
    case 'mirror': return commit(() => { for (const id of k.block || []) { const b = S.blockById(doc, id); b.mirror = !b.mirror; } });
    case 'dup': return commit(() => {
      const ids = [];
      for (const id of k.block || []) { const b = JSON.parse(JSON.stringify(S.blockById(doc, id))); b.id = S.newId(doc, 'b', 'blocks'); b.x = r2(b.x + 4); b.y = r2(b.y + 4); doc.blocks.push(b); ids.push(b.id); }
      st.sel.clear(); ids.forEach((id) => st.sel.add('block:' + id));
    });
    case 'labelAuto': return commit(() => takeAreaLabel(S.areaById(doc, k.area[0])));
    case 'labelCenter': return commit(() => { const r = S.areaById(doc, k.area[0]); const c = S.centroid(r.poly); if (!r.label) r.label = { text: fmtArea(areaM2(r.poly)), x: 0, y: 0 }; r.label.x = r2(c[0] - 6); r.label.y = r2(c[1] + 2); });
    default: return null;
  }
}

function deleteSelection() {
  const doc = st.doc, k = selKinds();
  if (!st.sel.size) return;
  commit(() => {
    for (const id of k.opening || []) doc.openings = doc.openings.filter((o) => o.id !== id);
    for (const id of k.wall || []) S.deleteWall(doc, id);
    for (const id of k.block || []) doc.blocks = doc.blocks.filter((b) => b.id !== id);
    for (const id of k.column || []) doc.columns = doc.columns.filter((c) => c.id !== id);
    for (const id of k.area || []) doc.areas = doc.areas.filter((r) => r.id !== id);
    st.sel.clear();
  });
}

// ---------------------------------------------------------------- изменения / история

function commit(fn) {
  st.hist.push(st.doc);
  fn();
  markDirty();
  redraw();
}
function markDirty() { st.dirty = true; IO.saveDraft(st.doc); updateButtons(); }
function undo() { const d = st.hist.undo(st.doc); if (d) { st.doc = d; st.sel.clear(); markDirty(); redraw(); } }
function redo() { const d = st.hist.redo(st.doc); if (d) { st.doc = d; st.sel.clear(); markDirty(); redraw(); } }
function updateButtons() {
  $('btn-undo').disabled = !st.hist.canUndo; $('btn-redo').disabled = !st.hist.canRedo;
  $('btn-save').textContent = st.dirty ? 'Сохранить на сервер ● ' : 'Сохранить на сервер';
  $('btn-save').classList.toggle('primary', st.dirty);
  for (const b of dom.tools.querySelectorAll('button')) b.classList.toggle('is-on', b.dataset.tool === st.tool);
  dom.hint.textContent = TOOL_HINT[st.tool] || '';
  dom.stage.className = 'stage tool-' + st.tool;
  for (const b of dom.units.querySelectorAll('button')) b.classList.toggle('is-on', b.dataset.unit === st.unit);
}
function setStatus(msg, bad) { dom.status.textContent = msg; dom.status.style.color = bad ? '#E0342B' : ''; }

// ---------------------------------------------------------------- привязка

/** Углы полигонов областей, колонн и bbox блоков (с учётом rot, без mirror — bbox симметричен) — цели привязки. */
function collectCorners(doc) {
  const pts = [];
  for (const r of doc.areas) for (const v of r.poly) pts.push([v[0], v[1]]);
  for (const c of doc.columns) pts.push([c.x, c.y], [c.x + c.w, c.y], [c.x, c.y + c.h], [c.x + c.w, c.y + c.h]);
  for (const b of doc.blocks) {
    const cx = b.x + b.w / 2, cy = b.y + b.h / 2;
    let hw = b.w / 2, hh = b.h / 2;
    if (b.rot === 90 || b.rot === 270) { const t = hw; hw = hh; hh = t; }
    pts.push([cx - hw, cy - hh], [cx + hw, cy - hh], [cx - hw, cy + hh], [cx + hw, cy + hh]);
  }
  return pts;
}

/** Из кандидатов-соседей выбирает того, к которому текущий угол (p относительно кандидата) ближе всего к кратному 45°. */
function pickAnchor(p, candidates) {
  let anchor = null, bestDiff = Infinity;
  for (const nb of candidates || []) {
    const dx = p[0] - nb[0], dy = p[1] - nb[1];
    if (Math.hypot(dx, dy) < 1e-6) continue;
    let deg = Math.atan2(dy, dx) * 180 / Math.PI;
    if (deg < 0) deg += 360;
    const diff = Math.abs(deg - Math.round(deg / 45) * 45);
    if (diff < bestDiff) { bestDiff = diff; anchor = nb; }
  }
  return anchor;
}

function snapCtx(excludeNodes, refs, anchor) {
  const ex = new Set(excludeNodes || []);
  const nodes = [];
  for (const id of Object.keys(st.doc.nodes)) if (!ex.has(id)) nodes.push([st.doc.nodes[id][0], st.doc.nodes[id][1], id]);
  return { nodes, rawPts: st.layers.raw ? st.rawPts : [], corners: collectCorners(st.doc), refs: refs || [], tol: pxPt(8), off: st.keys.alt, anchor: anchor || null, shiftKey: st.keys.shift };
}
function snapTo(p, ctx) { const r = snapPoint(p, ctx); st.guides = r.guides; return r.p; }

function hitAt(target) {
  const h = target.closest && target.closest('[data-h]');
  if (h) {
    const parts = h.dataset.h.split(':');
    const raw = parts[2];
    const idx = raw == null ? null : (/^-?\d+(\.\d+)?$/.test(raw) ? +raw : raw);
    return { h: parts[0], id: parts[1], idx };
  }
  const e = target.closest && target.closest('[data-kind][data-id]');
  if (e && e.closest('[data-layer="hit"]')) return { kind: e.dataset.kind, id: e.dataset.id };
  return null;
}

// ---------------------------------------------------------------- указатель

dom.svg.addEventListener('pointerdown', onDown);
dom.svg.addEventListener('pointermove', onMove);
dom.svg.addEventListener('pointerup', onUp);
dom.svg.addEventListener('pointercancel', onUp);
dom.svg.addEventListener('dblclick', (e) => { if (st.tool === 'wall') finishDraft(); });
dom.svg.addEventListener('contextmenu', (e) => e.preventDefault());
dom.stage.addEventListener('dragstart', (e) => e.preventDefault());   // никакого нативного drag картинки-подложки

function onDown(e) {
  if (!st.doc) return;
  dom.svg.setPointerCapture(e.pointerId);
  const p = screenToPlan(e.clientX, e.clientY);
  const pan = e.button === 1 || st.keys.space || st.tool === 'hand' || e.button === 2;
  if (pan) { st.drag = { type: 'pan', sx: e.clientX, sy: e.clientY, vx: st.view.x, vy: st.view.y }; dom.stage.classList.add('panning'); return; }
  if (e.button !== 0) return;
  const hit = hitAt(e.target);
  const doc = st.doc;
  switch (st.tool) {
    case 'select': return downSelect(e, p, hit);
    case 'wall': return downWall(p, hit);
    case 'split': {
      if (hit && hit.kind === 'wall') { const w = S.wallById(doc, hit.id); const pr = projectOnWall(wallGeom(doc, w), p); commit(() => { const w2 = S.splitWall(doc, w.id, pr.s); if (w2) { st.sel.clear(); st.sel.add('wall:' + w.id); st.sel.add('wall:' + w2.id); } }); }
      return;
    }
    case 'door': case 'window': {
      if (hit && hit.kind === 'wall') {
        const w = S.wallById(doc, hit.id); const pr = projectOnWall(wallGeom(doc, w), p);
        const width = DEFAULT_OPEN_W[st.tool];
        commit(() => { const o = S.addOpening(doc, w.id, pr.s - width / 2, width, st.tool); if (o) { st.sel.clear(); st.sel.add('opening:' + o.id); } });
      }
      return;
    }
    case 'cut': {
      const q = snapTo(p, snapCtx([], st.cut ? [st.cut] : [], st.cut));
      if (!st.cut) { st.cut = q; drawOverlay(); return; }
      const a = st.cut; st.cut = null;
      const mid = [(a[0] + q[0]) / 2, (a[1] + q[1]) / 2];
      const k = selKinds();
      let area = (k.area || []).map((id) => S.areaById(doc, id)).find((r) => r && pointInPoly(mid, r.poly))
        || doc.areas.find((r) => r.kind !== 'shaft' && pointInPoly(mid, r.poly));
      if (!area) { setStatus('Линия не проходит через область', true); drawOverlay(); return; }
      commit(() => {
        const res = S.splitArea(doc, area.id, a, q);
        if (!res) { setStatus('Не удалось разрезать: линия должна пересечь контур дважды', true); return; }
        for (const r of res) { const c = S.centroid(r.poly); r.label = { text: fmtArea(areaM2(r.poly)), x: r2(c[0] - 6), y: r2(c[1] + 2) }; }
        st.sel.clear(); st.sel.add('area:' + res[1].id);
        setStatus(`Область разрезана: ${fmtArea(areaM2(res[0].poly))} и ${fmtArea(areaM2(res[1].poly))} м²`);
      });
      return;
    }
    case 'pen': {
      st.hist.push(doc);
      const note = { id: S.newId(doc, 'k', 'notes'), kind: 'pen', pts: [[r2(p[0]), r2(p[1])]] };
      doc.notes = doc.notes || []; doc.notes.push(note);
      st.drag = { type: 'pen', note };
      return;
    }
    case 'text': {
      const inp = dom.textInput; const r = stageRect();
      inp.classList.remove('hidden'); inp.style.left = (e.clientX - r.left) + 'px'; inp.style.top = (e.clientY - r.top) + 'px';
      inp.value = ''; inp.dataset.x = p[0]; inp.dataset.y = p[1];
      try { dom.svg.releasePointerCapture(e.pointerId); } catch (err) { /* noop */ }
      setTimeout(() => inp.focus(), 30);
      return;
    }
    default: return;
  }
}

function pointInPoly(p, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
    if (((yi > p[1]) !== (yj > p[1])) && (p[0] < (xj - xi) * (p[1] - yi) / (yj - yi + 1e-12) + xi)) inside = !inside;
  }
  return inside;
}

function downSelect(e, p, hit) {
  const doc = st.doc;
  if (hit && hit.h) {
    // ручки
    const d = { type: hit.h, id: hit.id, idx: hit.idx, start: p, pushed: false };
    if (hit.h === 'node') d.orig = doc.nodes[hit.id].slice();
    if (hit.h === 'wallface') { const w = S.wallById(doc, hit.id); const g = wallGeom(doc, w); d.wall = w; d.face = hit.idx; d.g0 = { a: g.a.slice(), b: g.b.slice(), u: g.u, n: g.n, L: g.L, t: g.t }; }
    if (hit.h === 'wallend') { const w = S.wallById(doc, hit.id); const g = wallGeom(doc, w); d.wall = w; d.which = hit.idx; d.nid = hit.idx === 'a' ? w.a : w.b; d.g0 = { a: g.a.slice(), b: g.b.slice(), u: g.u, n: g.n, L: g.L, t: g.t }; }
    if (hit.h === 'wallcenter') { const w = S.wallById(doc, hit.id); d.wall = w; d.origA = doc.nodes[w.a].slice(); d.origB = doc.nodes[w.b].slice(); }
    if (hit.h === 'open') { const o = S.openingById(doc, hit.id); d.o = o; const g = wallGeom(doc, S.wallById(doc, o.wall)); d.grab = projectOnWall(g, p).s - o.pos; }
    if (hit.h === 'areav') { const r = S.areaById(doc, hit.id); d.r = r; }
    if (hit.h === 'label') { const r = S.areaById(doc, hit.id); d.r = r; d.grab = [p[0] - r.label.x, p[1] - r.label.y]; }
    st.drag = d; return;
  }
  if (hit && hit.kind) {
    const key = hit.kind + ':' + hit.id;
    if (e.shiftKey) { if (st.sel.has(key)) st.sel.delete(key); else st.sel.add(key); }
    else if (!st.sel.has(key)) { st.sel.clear(); st.sel.add(key); }
    drawOverlay(); renderProps();
    const k = selKinds();
    const d = { type: 'move', start: p, pushed: false, items: [] };
    for (const id of k.wall || []) { const w = S.wallById(doc, id); d.items.push({ kind: 'wall', w, origA: doc.nodes[w.a].slice(), origB: doc.nodes[w.b].slice() }); }
    for (const id of k.block || []) { const b = S.blockById(doc, id); d.items.push({ kind: 'block', b, ox: b.x, oy: b.y }); }
    for (const id of k.column || []) { const c = S.columnById(doc, id); d.items.push({ kind: 'column', c, ox: c.x, oy: c.y }); }
    if (k.opening && k.opening.length === 1 && hit.kind === 'opening') {
      const o = S.openingById(doc, hit.id); const g = wallGeom(doc, S.wallById(doc, o.wall));
      st.drag = { type: 'open', o, grab: projectOnWall(g, p).s - o.pos, start: p, pushed: false }; return;
    }
    st.drag = d.items.length ? d : null;
    return;
  }
  // пусто: снять выделение, панорама
  if (!e.shiftKey && st.sel.size) { st.sel.clear(); drawOverlay(); renderProps(); }
  st.drag = { type: 'pan', sx: e.clientX, sy: e.clientY, vx: st.view.x, vy: st.view.y };
  dom.stage.classList.add('panning');
}

function downWall(p, hit) {
  const doc = st.doc;
  const last = st.draft && st.draft.pts.length ? st.draft.pts[st.draft.pts.length - 1] : null;
  const q = snapTo(p, snapCtx([], last ? [last] : [], last));
  if (!st.draft) { st.draft = { pts: [q], nodeIds: [] }; drawOverlay(); return; }
  if (last && Math.hypot(q[0] - last[0], q[1] - last[1]) < 0.3) return finishDraft();
  st.hist.push(doc);
  const aId = st.draft.nodeIds.length ? st.draft.nodeIds[st.draft.nodeIds.length - 1] : S.addNode(doc, last);
  const bId = S.addNode(doc, q);
  const w = S.addWall(doc, aId, bId, st.preset.t, 1, st.preset.kind);
  if (!st.draft.nodeIds.length) st.draft.nodeIds.push(aId);
  st.draft.nodeIds.push(bId);
  st.draft.pts.push(q);
  if (w) { st.sel.clear(); st.sel.add('wall:' + w.id); }
  markDirty(); redraw();
}
function finishDraft() { st.draft = null; st.guides = []; drawOverlay(); }

function onMove(e) {
  if (!st.doc) return;
  const p = screenToPlan(e.clientX, e.clientY);
  st.cursor = p;
  dom.coords.textContent = `${p[0].toFixed(2)}, ${p[1].toFixed(2)} pt`;
  const d = st.drag;
  if (!d) {
    if (st.tool === 'wall' && st.draft) { const last = st.draft.pts[st.draft.pts.length - 1]; st.cursor = snapTo(p, snapCtx([], [last], last)); drawOverlay(); }
    else if (st.tool === 'cut' && st.cut) { st.cursor = snapTo(p, snapCtx([], [st.cut], st.cut)); drawOverlay(); }
    return;
  }
  const doc = st.doc;
  if (d.type === 'pan') {
    const k = st.view.w / stageRect().width;
    st.view.x = d.vx - (e.clientX - d.sx) * k; st.view.y = d.vy - (e.clientY - d.sy) * k;
    applyView(); return;
  }
  if (d.type === 'pen') {
    const pts = d.note.pts; const last = pts[pts.length - 1];
    if (Math.hypot(p[0] - last[0], p[1] - last[1]) >= 0.3) { pts.push([r2(p[0]), r2(p[1])]); redrawNotesOnly(); }
    return;
  }
  const dx = p[0] - d.start[0], dy = p[1] - d.start[1];
  if (!d.pushed) {
    if (Math.hypot(dx, dy) < pxPt(3)) return;      // дрожание клика — не движение
    st.hist.push(doc); d.pushed = true;
  }
  if (d.type === 'node') {
    const nbrs = [];
    for (const w of S.nodeWalls(doc, d.id)) nbrs.push(doc.nodes[w.a === d.id ? w.b : w.a]);
    const anchor = pickAnchor(p, nbrs);
    const q = snapTo(p, snapCtx([d.id], nbrs, anchor));
    S.moveNode(doc, d.id, q);
  } else if (d.type === 'wallface') {
    const w = d.wall, g0 = d.g0;
    if (d.face === 1) {
      // наружная грань: меняет толщину, узлы на месте
      w.t = faceDragThickness(g0, p);
    } else {
      // опорная грань: наружная грань остаётся на месте, узлы едут вдоль n
      const { d: off, t } = refFaceDrag(g0, p);
      w.t = t;
      S.moveNode(doc, w.a, [g0.a[0] + g0.n[0] * off, g0.a[1] + g0.n[1] * off]);
      S.moveNode(doc, w.b, [g0.b[0] + g0.n[0] * off, g0.b[1] + g0.n[1] * off]);
    }
    S.clampOpenings(doc, w);
    st.guides = [];
  } else if (d.type === 'wallend') {
    const w = d.wall, g0 = d.g0, which = d.which;
    const other = which === 'a' ? g0.b : g0.a;
    const axisPt = endDragPoint(g0, which, p);
    const s = (axisPt[0] - other[0]) * g0.u[0] + (axisPt[1] - other[1]) * g0.u[1];
    const snapped = snapTo(p, snapCtx([d.nid], [], null));
    const sSnap = (snapped[0] - other[0]) * g0.u[0] + (snapped[1] - other[1]) * g0.u[1];
    const onLine = [other[0] + g0.u[0] * sSnap, other[1] + g0.u[1] * sSnap];
    const distFromLine = Math.hypot(snapped[0] - onLine[0], snapped[1] - onLine[1]);
    let target;
    if (distFromLine <= 0.05) { target = snapped; }
    else { const sr = Math.round(s / 0.25) * 0.25; target = [other[0] + g0.u[0] * sr, other[1] + g0.u[1] * sr]; st.guides = []; }
    const len = Math.hypot(target[0] - other[0], target[1] - other[1]);
    if (len < 1) {
      const k = len > 1e-6 ? 1 / len : 0;
      const dirx = len > 1e-6 ? (target[0] - other[0]) * k : g0.u[0] * Math.sign(s || 1);
      const diry = len > 1e-6 ? (target[1] - other[1]) * k : g0.u[1] * Math.sign(s || 1);
      target = [other[0] + dirx, other[1] + diry];
    }
    S.moveNode(doc, d.nid, target);
  } else if (d.type === 'wallcenter') {
    const w = d.wall;
    const target = [d.origA[0] + dx, d.origA[1] + dy];
    const snapped = snapTo(target, snapCtx([w.a, w.b], [], null));
    const shift = [snapped[0] - d.origA[0], snapped[1] - d.origA[1]];
    S.moveNode(doc, w.a, snapped);
    S.moveNode(doc, w.b, [d.origB[0] + shift[0], d.origB[1] + shift[1]]);
  } else if (d.type === 'move') {
    const sx = snapGrid(dx), sy = snapGrid(dy);
    for (const it of d.items) {
      if (it.kind === 'wall') { S.moveNode(doc, it.w.a, [it.origA[0] + sx, it.origA[1] + sy]); S.moveNode(doc, it.w.b, [it.origB[0] + sx, it.origB[1] + sy]); }
      if (it.kind === 'block') {
        // привязка левого верхнего угла блока к узлам / сырым точкам
        const q = snapTo([it.ox + dx, it.oy + dy], snapCtx([], []));
        it.b.x = q[0]; it.b.y = q[1];
      }
      if (it.kind === 'column') { it.c.x = r2(it.ox + sx); it.c.y = r2(it.oy + sy); }
    }
  } else if (d.type === 'open') {
    const o = d.o; const w = S.wallById(doc, o.wall); const g = wallGeom(doc, w);
    const s = projectOnWall(g, p).s - d.grab;
    o.pos = r2(Math.max(0, Math.min(g.L - o.width, snapGrid(s))));
  } else if (d.type === 'areav') {
    const refs = [d.r.poly[(d.idx + 1) % d.r.poly.length], d.r.poly[(d.idx - 1 + d.r.poly.length) % d.r.poly.length]];
    const anchor = pickAnchor(p, refs);
    const ctx = snapCtx([], refs, anchor);
    for (const r of doc.areas) if (r !== d.r) for (const v of r.poly) ctx.nodes.push([v[0], v[1]]);
    const q = snapTo(p, ctx);
    d.r.poly[d.idx] = [q[0], q[1]];
  } else if (d.type === 'label') {
    d.r.label.x = r2(p[0] - d.grab[0]); d.r.label.y = r2(p[1] - d.grab[1]);
  }
  redrawLight();
}

function onUp(e) {
  const d = st.drag; st.drag = null;
  dom.stage.classList.remove('panning');
  st.guides = [];
  if (!d) return;
  if (d.type === 'pan') { drawOverlay(); return; }
  if (d.type === 'pen') { if (d.note.pts.length < 2) st.doc.notes.pop(); markDirty(); redraw(); return; }
  if (d.pushed) { markDirty(); }
  redraw();
}

// лёгкая перерисовка во время перетаскивания: план целиком (быстро: документ маленький) + оверлей
function redrawLight() {
  renderInto(dom.gPlan, st.doc, st.style, { hit: true, furniture: st.layers.furniture, labels: st.layers.labels, notes: st.layers.notes, paper: true, bbox: st.doc.bbox });
  drawOverlay();
}
function redrawNotesOnly() { redrawLight(); }

function commitTextNote() {
  const inp = dom.textInput;
  if (inp.classList.contains('hidden')) return;
  const v = inp.value.trim();
  inp.classList.add('hidden');
  inp.value = '';
  if (document.activeElement === inp) inp.blur();   // иначе следующие клавиши уйдут в скрытый инпут
  if (v) commit(() => { st.doc.notes = st.doc.notes || []; st.doc.notes.push({ id: S.newId(st.doc, 'k', 'notes'), kind: 'text', x: r2(+inp.dataset.x), y: r2(+inp.dataset.y), text: v }); });
}
dom.textInput.addEventListener('keydown', (e) => {
  e.stopPropagation();
  if (e.key === 'Enter') commitTextNote();
  if (e.key === 'Escape') { dom.textInput.value = ''; dom.textInput.classList.add('hidden'); dom.textInput.blur(); }
});
dom.textInput.addEventListener('blur', commitTextNote);

// ---------------------------------------------------------------- клавиатура (по e.code — не зависит от раскладки)

function setTool(t) { st.tool = t; st.draft = null; st.cut = null; st.guides = []; updateButtons(); drawOverlay(); }
dom.tools.addEventListener('click', (e) => { const b = e.target.closest('button[data-tool]'); if (b) setTool(b.dataset.tool); });

window.addEventListener('keydown', (e) => {
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'select' || tag === 'textarea') { if (e.key === 'Escape') e.target.blur(); return; }
  const mod = e.metaKey || e.ctrlKey;
  if (e.code === 'Space') { st.keys.space = true; e.preventDefault(); return; }
  if (e.key === 'Alt') { st.keys.alt = true; return; }
  if (e.key === 'Shift') { st.keys.shift = true; return; }
  if (mod && e.code === 'KeyZ') { e.preventDefault(); return e.shiftKey ? redo() : undo(); }
  if (mod && e.code === 'KeyS') { e.preventDefault(); return save(); }
  if (mod && e.code === 'KeyD') { e.preventDefault(); return runAction('dup'); }
  if (mod) return;
  switch (e.code) {
    case 'KeyV': return setTool('select');
    case 'KeyW': return setTool('wall');
    case 'KeyS': return setTool('split');
    case 'KeyD': return setTool('door');
    case 'KeyO': return setTool('window');
    case 'KeyA': return setTool('cut');
    case 'KeyN': return setTool('pen');
    case 'KeyT': return setTool('text');
    case 'KeyH': return setTool('hand');
    case 'KeyX': return runAction('flipSide');
    case 'KeyJ': return runAction('joinSel');
    case 'KeyF': return runAction('flipHinge');
    case 'KeyG': return runAction('flipSwing');
    case 'KeyR': return runAction('rotate');
    case 'KeyM': return runAction('mirror');
    case 'KeyP': $('ly-pdf').checked = st.layers.pdf = !st.layers.pdf; return redraw();
    case 'KeyC': $('ly-raw').checked = st.layers.raw = !st.layers.raw; return redraw();
    case 'Tab': e.preventDefault(); st.layers.flip = (st.layers.flip + 1) % 3; setStatus(['обычный вид', 'только PDF', 'только план'][st.layers.flip]); return redraw();
    case 'Delete': case 'Backspace':
      if (st.tool === 'wall' && st.draft) { e.preventDefault(); return undo(); }
      e.preventDefault(); return deleteSelection();
    case 'Enter': if (st.tool === 'wall') return finishDraft(); return null;
    case 'Escape':
      if (st.draft) return finishDraft();
      if (st.cut) { st.cut = null; return drawOverlay(); }
      if (st.sel.size) { st.sel.clear(); return redraw(); }
      return setTool('select');
    case 'ArrowLeft': case 'ArrowRight': case 'ArrowUp': case 'ArrowDown': {
      e.preventDefault();
      const step = e.shiftKey ? 2.5 : 0.25;
      const dx = e.code === 'ArrowLeft' ? -step : e.code === 'ArrowRight' ? step : 0;
      const dy = e.code === 'ArrowUp' ? -step : e.code === 'ArrowDown' ? step : 0;
      return nudge(dx, dy);
    }
    default: return null;
  }
});
window.addEventListener('keyup', (e) => { if (e.code === 'Space') st.keys.space = false; if (e.key === 'Alt') st.keys.alt = false; if (e.key === 'Shift') st.keys.shift = false; });

function nudge(dx, dy) {
  const doc = st.doc, k = selKinds();
  if (!st.sel.size) return;
  commit(() => {
    const moved = new Set();
    for (const id of k.wall || []) { const w = S.wallById(doc, id); for (const n of [w.a, w.b]) if (!moved.has(n)) { moved.add(n); S.moveNode(doc, n, [doc.nodes[n][0] + dx, doc.nodes[n][1] + dy]); } }
    for (const id of k.block || []) { const b = S.blockById(doc, id); b.x = r2(b.x + dx); b.y = r2(b.y + dy); }
    for (const id of k.column || []) { const c = S.columnById(doc, id); c.x = r2(c.x + dx); c.y = r2(c.y + dy); }
    for (const id of k.opening || []) { const o = S.openingById(doc, id); const w = S.wallById(doc, o.wall); const g = wallGeom(doc, w); o.pos = r2(Math.max(0, Math.min(g.L - o.width, o.pos + (dx || dy)))); }
  });
}

// ---------------------------------------------------------------- панель: слои, палитра, данные

$('ly-pdf').addEventListener('change', (e) => { st.layers.pdf = e.target.checked; redraw(); });
$('ly-pdf-op').addEventListener('input', (e) => { st.layers.pdfOp = +e.target.value; redraw(); });
$('ly-raw').addEventListener('change', (e) => { st.layers.raw = e.target.checked; redraw(); });
$('ly-furn').addEventListener('change', (e) => { st.layers.furniture = e.target.checked; redraw(); });
$('ly-labels').addEventListener('change', (e) => { st.layers.labels = e.target.checked; redraw(); });
$('ly-notes').addEventListener('change', (e) => { st.layers.notes = e.target.checked; redraw(); });
$('ly-nodes').addEventListener('change', (e) => { st.layers.nodes = e.target.checked; redraw(); });
$('preset-t').addEventListener('change', (e) => { st.preset.t = Math.max(0.3, parseFloat(e.target.value) || 1.4); });
$('preset-kind').addEventListener('change', (e) => { st.preset.kind = e.target.value; });
$('btn-undo').addEventListener('click', undo);
$('btn-redo').addEventListener('click', redo);
$('btn-fit').addEventListener('click', fitView);

for (const s of SYMBOL_ORDER) {
  const b = document.createElement('button'); b.textContent = SYMBOLS[s].label; b.dataset.sym = s;
  b.addEventListener('click', () => {
    const [w, h] = SYMBOLS[s].size;
    const cx = st.view.x + st.view.w / 2, cy = st.view.y + st.view.h / 2;
    commit(() => {
      const blk = { id: S.newId(st.doc, 'b', 'blocks'), sym: s, role: 'furniture', x: snapGrid(cx - w / 2), y: snapGrid(cy - h / 2), w, h, rot: 0, mirror: false, paths: [] };
      st.doc.blocks.push(blk); st.sel.clear(); st.sel.add('block:' + blk.id);
    });
    setTool('select');
  });
  dom.palette.appendChild(b);
}

async function save() {
  if (!st.doc) return;
  const clean = stripPrivate(st.doc);
  setStatus('Сохраняю…');
  try {
    const r = await IO.saveDoc(clean);
    st.doc.saved = r.saved; st.dirty = false; IO.clearDraft(st.doc.unit);
    setStatus('Сохранено ' + r.saved.replace('T', ' '));
    updateButtons(); refreshDraftBox();
  } catch (err) {
    setStatus('Не сохранилось: ' + err.message + ' (черновик в браузере цел)', true);
  }
}
function stripPrivate(doc) {
  const d = JSON.parse(JSON.stringify(doc));
  for (const b of d.blocks) delete b._rawPaths;
  return d;
}
$('btn-save').addEventListener('click', save);
$('btn-json').addEventListener('click', () => IO.downloadJSON(stripPrivate(st.doc)));
$('btn-svg').addEventListener('click', () => IO.exportSVG(stripPrivate(st.doc), st.style, { labels: true }));
$('btn-png').addEventListener('click', async () => {
  try { const blob = await IO.exportPNG(stripPrivate(st.doc), st.style, { labels: true }, 3.2); IO.downloadBlob('unit-' + st.doc.unit + '.png', blob); }
  catch (e) { setStatus('PNG: ' + e.message, true); }
});
$('btn-notes-copy').addEventListener('click', async () => {
  try {
    const blob = await IO.exportPNG(stripPrivate(st.doc), st.style, { labels: true, notes: true }, 4.8, 'кв. ' + st.doc.unit + ' · замечания');
    const ok = await IO.copyBlob(blob);
    if (!ok) IO.downloadBlob('notes-unit-' + st.doc.unit + '.png', blob);
    setStatus(ok ? 'PNG замечаний в буфере обмена' : 'PNG замечаний скачан');
  } catch (e) { setStatus('PNG: ' + e.message, true); }
});
$('btn-notes-clear').addEventListener('click', () => commit(() => { st.doc.notes = []; }));
$('btn-reload').addEventListener('click', () => loadUnit(st.unit, true));
$('btn-draft-restore').addEventListener('click', () => {
  const d = IO.loadDraft(st.unit); if (!d) return;
  st.hist.push(st.doc); st.doc = d.doc; st.dirty = true; st.sel.clear(); updateButtons(); redraw(); $('draft-box').classList.add('hidden');
  setStatus('Черновик восстановлен — не забудьте сохранить');
});
$('btn-draft-drop').addEventListener('click', () => { IO.clearDraft(st.unit); refreshDraftBox(); });

function refreshDraftBox() {
  const d = IO.loadDraft(st.unit);
  const savedAt = st.doc && st.doc.saved ? Date.parse(st.doc.saved) : 0;
  const show = d && d.at > savedAt && !st.dirty;
  $('draft-box').classList.toggle('hidden', !show);
  if (show) $('draft-info').textContent = 'В браузере есть несохранённый черновик от ' + new Date(d.at).toLocaleString('ru-RU');
}

// ---------------------------------------------------------------- загрузка

async function loadUnit(unit, force) {
  if (st.doc && st.dirty && st.unit !== unit) IO.saveDraft(st.doc);
  st.unit = unit;
  setStatus('Загружаю ' + unit + '…');
  try {
    st.doc = await IO.loadDoc(unit);
  } catch (e) { setStatus(e.message, true); return; }
  st.doc.notes = st.doc.notes || [];
  st.hist = new S.History(200); st.sel.clear(); st.dirty = false; st.draft = null; st.cut = null;
  dom.title.textContent = `${st.doc.title || ''} ${unit} · ${st.doc.total != null ? st.doc.total + ' м²' : ''}`;
  history.replaceState(null, '', '?unit=' + unit);
  st.raw = null; st.rawPts = []; drawRaw();
  IO.loadRaw(unit).then((raw) => { if (st.unit !== unit) return; st.raw = raw; st.rawPts = rawPoints(raw); drawRaw(); });
  fitView();
  redraw();
  refreshDraftBox();
  setStatus(`Квартира ${unit}: стен ${st.doc.walls.length}, проёмов ${st.doc.openings.length}, блоков ${st.doc.blocks.length}` + (st.doc.saved ? ' · сохранено ' + st.doc.saved.replace('T', ' ') : ''));
}

async function boot() {
  st.style = await IO.loadStyle();
  st.index = await IO.loadIndex();
  dom.units.innerHTML = '';
  for (const u of st.index.units) {
    const b = document.createElement('button'); b.textContent = u.unit; b.dataset.unit = u.unit; b.title = `${u.title} · ${u.total} м²`;
    b.addEventListener('click', () => loadUnit(u.unit));
    dom.units.appendChild(b);
  }
  const q = new URLSearchParams(location.search).get('unit');
  const first = st.index.units.find((u) => u.unit === q) || st.index.units[0];
  updateButtons();
  if (first) await loadUnit(first.unit);
}

window.__plan = st;   // отладка из консоли
boot().catch((e) => setStatus('Ошибка запуска: ' + e.message, true));
