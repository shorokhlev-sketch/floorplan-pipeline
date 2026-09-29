// Редактор v3 — загрузка/сохранение/экспорт.
import { renderSVG } from '../render.js';

export const PLANS = '../plans/';
export const TRUTH = '../../truth/';
export const RAW = '../../editor-data/';
// nginx проксирует /plan-studio/api/ → plan-api /api/; локально plan-api сам раздаёт статику с корня
export const API = location.pathname.startsWith('/plan-studio/') ? '/plan-studio/api/' : '/api/';

const bust = () => '?t=' + Date.now();

export async function loadIndex() {
  const r = await fetch(PLANS + 'index.json' + bust(), { cache: 'no-store' });
  if (!r.ok) throw new Error('index.json: ' + r.status);
  return r.json();
}

export async function loadDoc(unit) {
  const r = await fetch(PLANS + 'unit-' + unit + '.json' + bust(), { cache: 'no-store' });
  if (!r.ok) throw new Error('unit-' + unit + '.json: ' + r.status);
  return r.json();
}

export async function loadRaw(unit) {
  try {
    const r = await fetch(RAW + 'raw-' + unit + '.json', { cache: 'no-store' });
    return r.ok ? await r.json() : null;
  } catch (e) { return null; }
}

export async function loadStyle() {
  const r = await fetch('../style.json' + bust(), { cache: 'no-store' });
  return r.ok ? r.json() : {};
}

export async function saveDoc(doc) {
  const r = await fetch(API + 'v3/plan/' + doc.unit, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(doc) });
  let body = null;
  try { body = await r.json(); } catch (e) { body = { ok: false, error: 'HTTP ' + r.status }; }
  if (!r.ok || !body.ok) throw new Error(body.error || (body.problems || []).join('; ') || ('HTTP ' + r.status));
  return body;
}

// черновики в браузере (страховка до успешного PUT)
const DKEY = (unit) => 'plan-v3-draft:' + unit;
export function saveDraft(doc) { try { localStorage.setItem(DKEY(doc.unit), JSON.stringify({ at: Date.now(), doc })); } catch (e) { /* quota */ } }
export function loadDraft(unit) { try { const s = localStorage.getItem(DKEY(unit)); return s ? JSON.parse(s) : null; } catch (e) { return null; } }
export function clearDraft(unit) { try { localStorage.removeItem(DKEY(unit)); } catch (e) { /* noop */ } }

export function downloadBlob(name, blob) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
}

export function downloadJSON(doc) {
  downloadBlob('unit-' + doc.unit + '.json', new Blob([JSON.stringify(doc, null, 1)], { type: 'application/json' }));
}

export function exportSVG(doc, style, opts) {
  const svg = renderSVG(doc, style, Object.assign({ furniture: true, labels: true }, opts || {}));
  downloadBlob('unit-' + doc.unit + (opts && opts.notes ? '-notes' : '') + '.svg', new Blob([svg], { type: 'image/svg+xml' }));
}

/** SVG-строка → PNG blob через canvas (pxPerPt), с белым фоном. */
export function svgToPng(svgStr, wPt, hPt, pxPerPt, caption) {
  return new Promise((resolve, reject) => {
    const W = Math.round(wPt * pxPerPt), H = Math.round(hPt * pxPerPt);
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement('canvas');
      canvas.width = W; canvas.height = H + (caption ? Math.round(pxPerPt * 6) : 0);
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, W, H);
      if (caption) { ctx.fillStyle = '#E0342B'; ctx.font = Math.round(pxPerPt * 3.5) + 'px DM Sans, Helvetica, sans-serif'; ctx.fillText(caption, 8, H + pxPerPt * 4.2); }
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('toBlob'))), 'image/png');
    };
    img.onerror = () => reject(new Error('svg → image'));
    img.src = 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(svgStr)));
  });
}

export async function exportPNG(doc, style, opts, pxPerPt, caption) {
  const o = Object.assign({ furniture: true, labels: true }, opts || {});
  const svg = renderSVG(doc, style, o);
  const bbox = o.bbox || doc.bbox;
  return svgToPng(svg, bbox.w, bbox.h, pxPerPt || 3.2, caption);
}

export async function copyBlob(blob) {
  if (navigator.clipboard && window.ClipboardItem) {
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
    return true;
  }
  return false;
}
