// plan-studio/v3/render.js — план v3: документ → SVG. ES-модуль без зависимостей, работает в браузере и в node.
// Формат документа: editor/SCHEMA.md. Один рендерер для редактора, сайта и PNG.
import { SYMBOLS } from './symbols.js';

export const DEFAULT_STYLE = {
  paper: '#FFFFFF',
  floor: '#F3EFE8',
  ink: '#182E46',
  muted: '#9AA3AD',
  w_furniture: 0.5,
  w_leaf: 0.5,
  w_arc: 0.4,
  arc_dash: '1.5 1.5',
  w_column: 0.9,
  window: { lines: [1 / 3, 2 / 3], w: 0.5 },     // доли толщины стены, на которых лежат линии остекления
  sliding: { w: 0.5, overlap: 1.0 },
  margin: 14,
  label: { font: 'Instrument Serif, Georgia, serif', size: 6.5, color: '#182E46', unit: ' м²' },
  title: { font: 'Instrument Serif, Georgia, serif', size_name: 10, size_area: 10, color: '#182E46' },
  note: { color: '#E0342B', w: 0.9, size: 4.2 },
};

export const DEFAULT_OPTS = {
  furniture: true,   // блоки мебели (role furniture); стены/шахты рисуются всегда
  labels: false,     // подписи площадей (финальный проход)
  title: false,      // заголовок квартиры
  notes: false,      // красные замечания
  paper: true,       // фон-подложка
  hit: false,        // невидимый слой попаданий для редактора (data-kind/data-id)
  bbox: null,        // {x0,y0,w,h} — кадр; по умолчанию doc.bbox
};

// ---------------------------------------------------------------- геометрия

export const M_PER_PT = 0.0705;

export function wallGeom(doc, w) {
  const a = doc.nodes[w.a], b = doc.nodes[w.b];
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const L = Math.hypot(dx, dy) || 1e-9;
  const u = [dx / L, dy / L];
  const n = [-u[1] * w.side, u[0] * w.side];
  return { a, b, u, n, L, t: w.t };
}

export function polyArea(poly) {
  let s = 0;
  for (let i = 0; i < poly.length; i++) {
    const [x0, y0] = poly[i], [x1, y1] = poly[(i + 1) % poly.length];
    s += x0 * y1 - x1 * y0;
  }
  return Math.abs(s) / 2;
}

export function areaM2(poly) { return polyArea(poly) * M_PER_PT * M_PER_PT; }

export function wallOpenings(doc, wallId) {
  return doc.openings.filter((o) => o.wall === wallId).sort((p, q) => p.pos - q.pos);
}

/** Сплошные куски стены между проёмами: [[s0,s1], ...] в pt от узла a. */
export function solidRuns(L, openings) {
  const runs = [];
  let cur = 0;
  for (const o of openings) {
    const o0 = Math.max(0, o.pos), o1 = Math.min(L, o.pos + o.width);
    if (o0 > cur + 1e-6) runs.push([cur, o0]);
    cur = Math.max(cur, o1);
  }
  if (cur < L - 1e-6) runs.push([cur, L]);
  return runs;
}

/** Четырёхугольник стены (или её куска) с согласованной ориентацией (для nonzero-объединения). */
export function wallQuad(g, s0, s1) {
  const p = (s, off) => [g.a[0] + g.u[0] * s + g.n[0] * off, g.a[1] + g.u[1] * s + g.n[1] * off];
  const q = [p(s0, 0), p(s1, 0), p(s1, g.t), p(s0, g.t)];
  // ориентация: делаем площадь положительной (одинаковой у всех квадов)
  let s = 0;
  for (let i = 0; i < 4; i++) { const A = q[i], B = q[(i + 1) % 4]; s += A[0] * B[1] - B[0] * A[1]; }
  return s < 0 ? q.reverse() : q;
}

export function docBBox(doc, margin) {
  // по геометрии: узлы (+ толщина), области, колонны, блоки
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const add = (x, y) => { if (x < x0) x0 = x; if (y < y0) y0 = y; if (x > x1) x1 = x; if (y > y1) y1 = y; };
  for (const w of doc.walls) {
    const g = wallGeom(doc, w);
    for (const q of wallQuad(g, 0, g.L)) add(q[0], q[1]);
  }
  for (const r of doc.areas) for (const p of r.poly) add(p[0], p[1]);
  for (const c of doc.columns) { add(c.x, c.y); add(c.x + c.w, c.y + c.h); }
  for (const b of doc.blocks) { add(b.x, b.y); add(b.x + b.w, b.y + b.h); }
  if (!isFinite(x0)) return { x0: 0, y0: 0, w: 100, h: 100 };
  const m = margin == null ? DEFAULT_STYLE.margin : margin;
  return { x0: r2(x0 - m), y0: r2(y0 - m), w: r2(x1 - x0 + 2 * m), h: r2(y1 - y0 + 2 * m) };
}

const r2 = (v) => Math.round(v * 100) / 100;
const f = (v) => (Math.round(v * 100) / 100).toString();
const pt = (p) => f(p[0]) + ' ' + f(p[1]);

function color(style, tok) {
  if (tok == null) return 'none';
  if (tok === 'ink' || tok === 'paper' || tok === 'floor' || tok === 'muted') return style[tok];
  return tok;
}

// ---------------------------------------------------------------- дерево примитивов

function el(tag, attrs, children, text) {
  return { tag, attrs: attrs || {}, children: children || [], text: text == null ? null : String(text) };
}

function quadPath(q) {
  return 'M ' + q.map(pt).join(' L ') + ' Z';
}

function blockTransform(b) {
  const cx = b.x + b.w / 2, cy = b.y + b.h / 2;
  let t = 'translate(' + f(cx) + ' ' + f(cy) + ')';
  if (b.rot) t += ' rotate(' + f(b.rot) + ')';
  if (b.mirror) t += ' scale(-1 1)';
  t += ' translate(' + f(-b.w / 2) + ' ' + f(-b.h / 2) + ')';
  return t;
}

function blockPrims(doc, style, b) {
  const kids = [];
  if (b.sym === 'raw' || !b.sym) {
    for (const p of b.paths || []) {
      const attrs = { d: p.d, fill: color(style, p.fill), stroke: color(style, p.stroke) };
      if (p.stroke) attrs['stroke-width'] = f(p.w || style.w_furniture);
      if (p.fill) attrs['fill-rule'] = 'evenodd';
      kids.push(el('path', attrs));
    }
  } else {
    const sym = SYMBOLS[b.sym];
    if (sym) {
      for (const p of sym.draw(b.w, b.h)) {
        const attrs = Object.assign({}, p.attrs);
        if ('fill' in attrs) attrs.fill = color(style, attrs.fill); else attrs.fill = 'none';
        if ('stroke' in attrs) attrs.stroke = color(style, attrs.stroke);
        if (attrs.stroke && attrs.stroke !== 'none' && !('stroke-width' in attrs)) attrs['stroke-width'] = f(style.w_furniture);
        kids.push(el(p.tag, attrs));
      }
    } else {
      kids.push(el('rect', { x: 0, y: 0, width: f(b.w), height: f(b.h), fill: 'none', stroke: style.muted,
        'stroke-width': '0.4', 'stroke-dasharray': '1 1' }));
    }
  }
  return el('g', { transform: blockTransform(b), 'data-kind': 'block', 'data-id': b.id }, kids);
}

function doorPrims(doc, style, g, o) {
  const hingeAt = o.hinge === 'b' ? o.pos + o.width : o.pos;
  const dir = o.hinge === 'b' ? -1 : 1;                   // от петли к другому краю проёма вдоль u
  const swing = o.swing === 1 ? 1 : -1;
  const faceOff = swing === 1 ? g.t : 0;                   // грань стены со стороны распаха
  const H = [g.a[0] + g.u[0] * hingeAt + g.n[0] * faceOff, g.a[1] + g.u[1] * hingeAt + g.n[1] * faceOff];
  const tip = [H[0] + g.n[0] * swing * o.width, H[1] + g.n[1] * swing * o.width];
  const E = [H[0] + g.u[0] * dir * o.width, H[1] + g.u[1] * dir * o.width];
  const z = (tip[0] - H[0]) * (E[1] - H[1]) - (tip[1] - H[1]) * (E[0] - H[0]);
  const sweep = z > 0 ? 1 : 0;
  const r = f(o.width);
  return [
    el('path', { d: 'M ' + pt(tip) + ' A ' + r + ' ' + r + ' 0 0 ' + sweep + ' ' + pt(E),
      fill: 'none', stroke: style.muted, 'stroke-width': f(style.w_arc), 'stroke-dasharray': style.arc_dash }),
    el('line', { x1: f(H[0]), y1: f(H[1]), x2: f(tip[0]), y2: f(tip[1]),
      stroke: style.ink, 'stroke-width': f(style.w_leaf), 'stroke-linecap': 'square' }),
  ];
}

function windowPrims(doc, style, g, o) {
  const out = [];
  const s0 = o.pos, s1 = o.pos + o.width;
  for (const k of style.window.lines) {
    const off = g.t * k;
    const p0 = [g.a[0] + g.u[0] * s0 + g.n[0] * off, g.a[1] + g.u[1] * s0 + g.n[1] * off];
    const p1 = [g.a[0] + g.u[0] * s1 + g.n[0] * off, g.a[1] + g.u[1] * s1 + g.n[1] * off];
    out.push(el('line', { x1: f(p0[0]), y1: f(p0[1]), x2: f(p1[0]), y2: f(p1[1]),
      stroke: style.ink, 'stroke-width': f(style.window.w), 'stroke-linecap': 'butt' }));
  }
  return out;
}

function slidingPrims(doc, style, g, o) {
  const half = o.width / 2, ov = style.sliding.overlap;
  const seg = (s0, s1, k) => {
    const off = g.t * k;
    const p0 = [g.a[0] + g.u[0] * s0 + g.n[0] * off, g.a[1] + g.u[1] * s0 + g.n[1] * off];
    const p1 = [g.a[0] + g.u[0] * s1 + g.n[0] * off, g.a[1] + g.u[1] * s1 + g.n[1] * off];
    return el('line', { x1: f(p0[0]), y1: f(p0[1]), x2: f(p1[0]), y2: f(p1[1]),
      stroke: style.ink, 'stroke-width': f(style.sliding.w), 'stroke-linecap': 'butt' });
  };
  return [seg(o.pos, o.pos + half + ov, 0.35), seg(o.pos + half - ov, o.pos + o.width, 0.65)];
}

/** Строит дерево примитивов. Возвращает {root, bbox}. */
export function build(doc, style, opts) {
  style = Object.assign({}, DEFAULT_STYLE, style || {});
  opts = Object.assign({}, DEFAULT_OPTS, opts || {});
  const bbox = opts.bbox || doc.bbox || docBBox(doc, style.margin);
  const layers = [];

  if (opts.paper) {
    layers.push(el('rect', { x: f(bbox.x0), y: f(bbox.y0), width: f(bbox.w), height: f(bbox.h), fill: style.paper, 'data-kind': 'paper' }));
  }

  // области (пол / балкон / шахта)
  const gAreas = el('g', { 'data-layer': 'areas' });
  for (const r of doc.areas) {
    const fill = r.kind === 'room' ? style.floor : style.paper;
    gAreas.children.push(el('path', { d: 'M ' + r.poly.map(pt).join(' L ') + ' Z', fill, stroke: 'none',
      'fill-rule': 'evenodd', 'data-kind': 'area', 'data-id': r.id }));
  }
  layers.push(gAreas);

  // стены: один путь, nonzero → объединение без швов
  const quads = [];
  const openingPrims = [];
  for (const w of doc.walls) {
    const g = wallGeom(doc, w);
    const ops = wallOpenings(doc, w.id);
    for (const [s0, s1] of solidRuns(g.L, ops)) quads.push(wallQuad(g, s0, s1));
    for (const o of ops) {
      let prims = [];
      if (o.kind === 'door') prims = doorPrims(doc, style, g, o);
      else if (o.kind === 'window') prims = windowPrims(doc, style, g, o);
      else if (o.kind === 'sliding') prims = slidingPrims(doc, style, g, o);
      if (prims.length) openingPrims.push(el('g', { 'data-kind': 'opening', 'data-id': o.id }, prims));
    }
  }
  // блоки-стены (raw ink) — в тот же слой, поверх объединения
  const gWalls = el('g', { 'data-layer': 'walls' });
  gWalls.children.push(el('path', { d: quads.map(quadPath).join(' '), fill: style.ink, stroke: 'none', 'fill-rule': 'nonzero', 'data-kind': 'walls' }));
  for (const b of doc.blocks) if (b.role === 'wall') gWalls.children.push(blockPrims(doc, style, b));
  layers.push(gWalls);

  layers.push(el('g', { 'data-layer': 'openings' }, openingPrims));

  // шахты — поверх стен (белый короб на полосе стены)
  const shafts = doc.blocks.filter((b) => b.role === 'shaft').map((b) => blockPrims(doc, style, b));
  layers.push(el('g', { 'data-layer': 'shafts' }, shafts));

  // колонны
  const gCols = el('g', { 'data-layer': 'columns' });
  for (const c of doc.columns) {
    gCols.children.push(el('rect', { x: f(c.x), y: f(c.y), width: f(c.w), height: f(c.h), fill: style.paper,
      stroke: style.ink, 'stroke-width': f(style.w_column), 'data-kind': 'column', 'data-id': c.id }));
  }
  layers.push(gCols);

  // мебель
  if (opts.furniture) {
    const gF = el('g', { 'data-layer': 'furniture' });
    for (const b of doc.blocks) if (!b.role || b.role === 'furniture') gF.children.push(blockPrims(doc, style, b));
    layers.push(gF);
  }

  // подписи (финальный проход)
  if (opts.labels) {
    const gL = el('g', { 'data-layer': 'labels' });
    for (const r of doc.areas) {
      if (!r.label || !r.label.text) continue;
      gL.children.push(el('text', { x: f(r.label.x), y: f(r.label.y), 'font-family': style.label.font,
        'font-size': f(style.label.size), fill: style.label.color, 'pointer-events': 'none', 'data-kind': 'label', 'data-id': r.id },
      [], r.label.text + style.label.unit));
    }
    layers.push(gL);
  }
  if (opts.title && doc.title) {
    const tp = doc.titlePos || [bbox.x0 + style.margin + 4, bbox.y0 + bbox.h - style.margin - 10];
    const gT = el('g', { 'data-layer': 'title' });
    gT.children.push(el('text', { x: f(tp[0]), y: f(tp[1]), 'font-family': style.title.font, 'font-size': f(style.title.size_name), fill: style.title.color }, [], doc.title + (doc.unit ? ' ' + doc.unit : '')));
    if (doc.total != null) {
      gT.children.push(el('text', { x: f(tp[0]), y: f(tp[1] + style.title.size_area * 1.1), 'font-family': style.title.font, 'font-size': f(style.title.size_area), fill: style.title.color }, [], String(doc.total).replace('.', ',') + ' м²'));
      const yl = tp[1] + style.title.size_area * 1.1 + 2.5;
      gT.children.push(el('path', { d: 'M ' + f(tp[0] - 3) + ' ' + f(tp[1] - style.title.size_name) + ' L ' + f(tp[0] - 3) + ' ' + f(yl) + ' L ' + f(tp[0] + 34) + ' ' + f(yl),
        fill: 'none', stroke: style.title.color, 'stroke-width': '0.5' }));
    }
    layers.push(gT);
  }

  if (opts.notes && doc.notes && doc.notes.length) {
    const gN = el('g', { 'data-layer': 'notes' });
    for (const k of doc.notes) {
      if (k.kind === 'pen' && k.pts && k.pts.length > 1) {
        gN.children.push(el('path', { d: 'M ' + k.pts.map(pt).join(' L '), fill: 'none', stroke: style.note.color,
          'stroke-width': f(style.note.w), 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'data-kind': 'note', 'data-id': k.id }));
      } else if (k.kind === 'text') {
        gN.children.push(el('text', { x: f(k.x), y: f(k.y), 'font-family': 'DM Sans, Helvetica, Arial, sans-serif',
          'font-size': f(style.note.size), fill: style.note.color, 'paint-order': 'stroke', stroke: style.paper, 'stroke-width': '1.2',
          'data-kind': 'note', 'data-id': k.id }, [], k.text || ''));
      }
    }
    layers.push(gN);
  }

  // слой попаданий для редактора: прозрачные фигуры с data-kind/data-id (сверху, порядок = приоритет клика)
  if (opts.hit) {
    const gH = el('g', { 'data-layer': 'hit', fill: 'transparent', stroke: 'none' });
    for (const r of doc.areas) gH.children.push(el('path', { d: 'M ' + r.poly.map(pt).join(' L ') + ' Z', 'data-kind': 'area', 'data-id': r.id }));
    for (const w of doc.walls) {
      const g = wallGeom(doc, w);
      gH.children.push(el('path', { d: quadPath(wallQuad(g, 0, g.L)), 'data-kind': 'wall', 'data-id': w.id }));
    }
    for (const c of doc.columns) gH.children.push(el('rect', { x: f(c.x), y: f(c.y), width: f(c.w), height: f(c.h), 'data-kind': 'column', 'data-id': c.id }));
    for (const b of doc.blocks) {
      if (!opts.furniture && (!b.role || b.role === 'furniture')) continue;
      gH.children.push(el('rect', { x: 0, y: 0, width: f(b.w), height: f(b.h), transform: blockTransform(b), 'data-kind': 'block', 'data-id': b.id }));
    }
    for (const o of doc.openings) {
      const w = doc.walls.find((x) => x.id === o.wall);
      if (!w) continue;
      const g = wallGeom(doc, w);
      // область проёма плюс полоса распаха для двери
      const reach = o.kind === 'door' ? o.width : 0;
      const off0 = o.kind === 'door' && o.swing !== 1 ? -reach : 0;
      const off1 = o.kind === 'door' && o.swing === 1 ? g.t + reach : g.t;
      const p = (s, off) => [g.a[0] + g.u[0] * s + g.n[0] * off, g.a[1] + g.u[1] * s + g.n[1] * off];
      const q = [p(o.pos, off0), p(o.pos + o.width, off0), p(o.pos + o.width, off1), p(o.pos, off1)];
      gH.children.push(el('path', { d: quadPath(q), 'data-kind': 'opening', 'data-id': o.id }));
    }
    layers.push(gH);
  }

  const root = el('svg', { xmlns: 'http://www.w3.org/2000/svg', width: f(bbox.w), height: f(bbox.h),
    viewBox: f(bbox.x0) + ' ' + f(bbox.y0) + ' ' + f(bbox.w) + ' ' + f(bbox.h) }, layers);
  return { root, bbox, style, opts };
}

// ---------------------------------------------------------------- сериализация

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export function toString(node, indent) {
  indent = indent || '';
  const attrs = Object.keys(node.attrs).filter((k) => node.attrs[k] != null)
    .map((k) => ' ' + k + '="' + esc(node.attrs[k]) + '"').join('');
  if (!node.children.length && node.text == null) return indent + '<' + node.tag + attrs + '/>';
  if (node.text != null && !node.children.length) return indent + '<' + node.tag + attrs + '>' + esc(node.text) + '</' + node.tag + '>';
  return indent + '<' + node.tag + attrs + '>\n' + node.children.map((c) => toString(c, indent + ' ')).join('\n') + '\n' + indent + '</' + node.tag + '>';
}

export function renderSVG(doc, style, opts) {
  return toString(build(doc, style, opts).root);
}

const SVG_NS = 'http://www.w3.org/2000/svg';

export function toDOM(node, document) {
  const e = document.createElementNS(SVG_NS, node.tag);
  for (const k of Object.keys(node.attrs)) if (node.attrs[k] != null) e.setAttribute(k, node.attrs[k]);
  if (node.text != null) e.textContent = node.text;
  for (const c of node.children) e.appendChild(toDOM(c, document));
  return e;
}

/** Перерисовать документ внутрь существующего <svg> или <g> (редактор). Возвращает bbox.
 *  Для <svg> выставляется viewBox по кадру; для <g> — нет (редактор сам управляет видом). */
export function renderInto(target, doc, style, opts) {
  const b = build(doc, style, opts);
  const root = b.root;
  while (target.firstChild) target.removeChild(target.firstChild);
  if (target.tagName && target.tagName.toLowerCase() === 'svg') target.setAttribute('viewBox', root.attrs.viewBox);
  for (const c of root.children) target.appendChild(toDOM(c, target.ownerDocument));
  return b.bbox;
}
