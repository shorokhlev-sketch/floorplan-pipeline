// Редактор v3 — чистая математика ручек стены: толщина граней, длина по торцам.
// Ничего не мутирует, не знает про doc/state — только геометрия g = wallGeom(doc, w).

const proj = (px, py, fx, fy, dx, dy) => (px - fx) * dx + (py - fy) * dy;

export function round05(v) { return Math.round(v / 0.05) * 0.05; }

/** Драг наружной грани (offset t): новая толщина стены; узлы не двигаются.
 *  t' = clamp(проекция (cursor − a) на n, 0.3, 40), округление к 0.05. */
export function faceDragThickness(g, cursor) {
  const raw = proj(cursor[0], cursor[1], g.a[0], g.a[1], g.n[0], g.n[1]);
  const clamped = Math.min(40, Math.max(0.3, raw));
  return round05(clamped);
}

/** Драг опорной грани (offset 0) при неподвижной наружной грани.
 *  d = проекция (cursor − a) на n, округлить к 0.05, ограничить так, чтобы t_orig − d ≥ 0.3.
 *  Возвращает { d, t }: d — сдвиг узлов вдоль n (от исходных позиций g.a/g.b), t — новая толщина. */
export function refFaceDrag(g, cursor) {
  let d = round05(proj(cursor[0], cursor[1], g.a[0], g.a[1], g.n[0], g.n[1]));
  if (g.t - d < 0.3) d = round05(g.t - 0.3);
  return { d, t: round05(g.t - d) };
}

/** Точка на оси стены при драге торца which ('a' — двигаем узел a, 'b' — узел b), без снапа.
 *  Противоположный узел (other) берётся из g и остаётся неподвижным. */
export function endDragPoint(g, which, cursor) {
  const other = which === 'a' ? g.b : g.a;
  const s = proj(cursor[0], cursor[1], other[0], other[1], g.u[0], g.u[1]);
  return [other[0] + g.u[0] * s, other[1] + g.u[1] * s];
}
