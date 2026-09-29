"use strict";

/* ============================================================
   Plan Studio — стилизация планов этажей
   Ванильный JS, без сборки, без библиотек.
   ============================================================ */

const SVGNS = "http://www.w3.org/2000/svg";
const LS_KEY = "plan-studio:style:v1";

const ROLES = [
  { id: "walls", label: "Стены" },
  { id: "partitions", label: "Перегородки" },
  { id: "windows", label: "Окна" },
  { id: "doors", label: "Двери" },
  { id: "furniture", label: "Мебель" },
  { id: "sanitary", label: "Сантехника" },
  { id: "balcony", label: "Балкон" },
  { id: "hatch", label: "Штриховка" },
  { id: "dims", label: "Размеры" },
  { id: "text", label: "Текст" },
  { id: "icons", label: "Значки" },
  { id: "other", label: "Прочее" },
];

const UNIT_TYPES = [
  { id: "studio", label: "Студия" },
  { id: "1br", label: "1-комн" },
  { id: "2br", label: "2-комн" },
  { id: "3br", label: "3-комн" },
];

const DEFAULT_TYPE_COLORS = {
  studio: "#F2B84B",
  "1br": "#6FCF97",
  "2br": "#56B4E9",
  "3br": "#C77DD1",
};

const UNKNOWN_TYPE_COLOR = "#9E9E9E";

// Cluster ids seeded with a plausible role in demo mode only, purely so
// the built-in demo data demonstrates the presets meaningfully. Real
// project data has no semantic hints, so roles stay "other" until the
// person using the studio assigns them once.
const DEMO_ROLE_HINTS = { c00: "walls", c01: "windows", c02: "furniture" };

// "Наш стиль" preset — mirrors plan-studio/style.json (the approved style the batch
// renderer, tools/render-plans.py, uses) 1:1 by cluster id. Real project floor data has
// no semantic role hints (see DEMO_ROLE_HINTS note above), so this preset must key off
// the raw cluster ids rather than m.role to actually reproduce the batch render here.
// Keep in sync with plan-studio/style.json by hand if that file changes.
const OURS_CLUSTER_STYLE = {
  c08: { fill: "#182E46" },
  c12: { fill: "#182E46" },
  c14: { fill: "#182E46" },
  c06: { fill: "#F3EFE8" },
  c03: { stroke: "#182E46", width: 0.7 },
  c07: { stroke: "#182E46", width: 0.55 },
  c02: { stroke: "#182E46", width: 0.4 },
  c05: { stroke: "#182E46", width: 0.4 },
};

/* ---------------- state ---------------- */

const state = {
  bg: "#F4F1EA",
  clusters: {},      // id -> {show,role,stroke,width,fill,fillEnabled,opacity,origStroke,origFill,origWidth,n}
  clusterOrder: [],  // ids sorted by n_total desc
  units: {
    showPolygons: true,
    showLabels: true,
    showOriginalTexts: false,
    labelColor: "#1E1B17",
    typeColors: Object.assign({}, DEFAULT_TYPE_COLORS),
    fillOpacity: 0.35,
  },
  index: null,
  floorCache: {},
  currentFloor: null,
  unitsInventory: null,
  mode: "loading", // loading | normal | demo | error
};

let transform = { scale: 1, tx: 0, ty: 0 };
let currentFloorData = null;
let hoveredClusterId = null;
let saveTimer = null;

/* ---------------- dom refs ---------------- */

const $ = (sel, ctx) => (ctx || document).querySelector(sel);

const dom = {};

document.addEventListener("DOMContentLoaded", init);

async function init() {
  dom.panel = $("#panel");
  dom.floorSelect = $("#floor-select");
  dom.floorPrev = $("#floor-prev");
  dom.floorNext = $("#floor-next");
  dom.bgColor = $("#bg-color");
  dom.statusLine = $("#status-line");
  dom.statusMsg = $("#status-msg");
  dom.clusterList = $("#cluster-list");
  dom.typeColors = $("#type-colors");
  dom.chkUnitPolys = $("#chk-unit-polys");
  dom.chkUnitLabels = $("#chk-unit-labels");
  dom.chkOrigTexts = $("#chk-orig-texts");
  dom.unitLabelColor = $("#unit-label-color");
  dom.stage = $("#stage");
  dom.stageInner = $("#stage-inner");
  dom.emptyMsg = $("#empty-msg");
  dom.btnFit = $("#btn-fit");
  dom.zoomReadout = $("#zoom-readout");
  dom.fatalOverlay = $("#fatal-overlay");
  dom.fatalText = $("#fatal-text");
  dom.btnExportStyle = $("#btn-export-style");
  dom.btnExportSvg = $("#btn-export-svg");
  dom.inputLoadStyle = $("#input-load-style");

  wireStaticControls();
  buildTypeColorInputs();

  await loadData();

  if (state.mode === "error") {
    showFatal(
      "Не найден ни plan-studio/data/index.json, ни plan-studio/data/demo-floor.json.\n" +
      "Ожидаю данные от параллельной подготовки (index.json + floor-N.json), либо demo-floor.json для локальной разработки интерфейса."
    );
    return;
  }

  loadStyleFromLocalStorage();
  buildClusterPanel();
  syncPanelInputs();
  populateFloorSelect();

  const urlFloor = new URLSearchParams(location.search).get("floor");
  const startFloor = pickStartFloor(urlFloor);
  await selectFloor(startFloor, { fit: true });

  setStatus(statusText(), state.mode === "demo" ? "warn" : "");
}

/* ---------------- data loading ---------------- */

async function fetchJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(url + " -> " + res.status);
  return res.json();
}

async function loadData() {
  try {
    state.index = await fetchJSON("data/index.json");
    state.mode = "normal";
    seedClusterMetaFromSummary(state.index.cluster_summary || {});
  } catch (e1) {
    try {
      const demo = await fetchJSON("data/demo-floor.json");
      state.floorCache["demo"] = demo;
      state.index = buildSyntheticIndexFromDemo(demo);
      state.mode = "demo";
      seedClusterMetaFromSummary(state.index.cluster_summary, true);
    } catch (e2) {
      state.mode = "error";
      return;
    }
  }

  // Apartment inventory (area schedule, units.json). Optional — the
  // studio still works, just without type-based coloring, if this fails.
  const invPaths = [
    "data/units.json",
    "../work/site-assets/data/units.json",
  ];
  for (const p of invPaths) {
    try {
      const inv = await fetchJSON(p);
      const map = {};
      (inv.units || []).forEach((u) => { map[String(u.number)] = u; });
      state.unitsInventory = map;
      break;
    } catch (e) { /* try next path */ }
  }
}

function buildSyntheticIndexFromDemo(demo) {
  const summary = {};
  (demo.clusters || []).forEach((c) => {
    summary[c.id] = { stroke: c.stroke, fill: c.fill, width: c.width, n_total: c.n };
  });
  return {
    floors: ["demo"],
    files: { demo: "demo-floor.json" },
    cluster_summary: summary,
  };
}

// Source PDFs commonly use stroke-width 0 to mean "hairline / thinnest
// renderable line" (a standard PDF convention), and some extraction
// pipelines simply don't preserve width at all (both show up as 0 or
// null here). Either way we fall back to a sensible visible default
// instead of rendering an invisible zero-width stroke.
function effectiveWidth(w) {
  return (w != null && w > 0) ? w : 0.5;
}

function seedClusterMetaFromSummary(summary, isDemo) {
  const ids = Object.keys(summary || {});
  ids.sort((a, b) => (summary[b].n_total || 0) - (summary[a].n_total || 0));
  state.clusterOrder = ids;
  ids.forEach((id) => {
    const s = summary[id] || {};
    state.clusters[id] = {
      show: true,
      role: (isDemo && DEMO_ROLE_HINTS[id]) || "other",
      stroke: s.stroke || "#1E1B17",
      width: effectiveWidth(s.width),
      fill: s.fill || "#888888",
      fillEnabled: !!s.fill,
      opacity: 1,
      origStroke: s.stroke || null,
      origFill: s.fill || null,
      origWidth: s.width != null ? s.width : null,
      n: s.n_total || 0,
    };
  });
}

function registerUnknownCluster(id, floorCluster) {
  if (state.clusters[id]) return;
  state.clusters[id] = {
    show: true,
    role: "other",
    stroke: floorCluster.stroke || "#1E1B17",
    width: effectiveWidth(floorCluster.width),
    fill: floorCluster.fill || "#888888",
    fillEnabled: !!floorCluster.fill,
    opacity: 1,
    origStroke: floorCluster.stroke || null,
    origFill: floorCluster.fill || null,
    origWidth: floorCluster.width != null ? floorCluster.width : null,
    n: floorCluster.n || 0,
  };
  state.clusterOrder.push(id);
  dom.clusterList.appendChild(buildClusterRow(id));
}

/* ---------------- persistence ---------------- */

function saveToLocalStorageNow() {
  try {
    const payload = { bg: state.bg, clusters: {}, units: state.units };
    state.clusterOrder.forEach((id) => {
      const m = state.clusters[id];
      payload.clusters[id] = {
        show: m.show, role: m.role, stroke: m.stroke, width: m.width,
        fill: m.fill, fillEnabled: m.fillEnabled, opacity: m.opacity,
      };
    });
    localStorage.setItem(LS_KEY, JSON.stringify(payload));
  } catch (e) { /* ignore quota / privacy errors */ }
}

function saveToLocalStorage() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveToLocalStorageNow, 250);
}

function loadStyleFromLocalStorage() {
  let raw;
  try { raw = localStorage.getItem(LS_KEY); } catch (e) { return; }
  if (!raw) return;
  try { applyStylePayload(JSON.parse(raw)); } catch (e) { /* corrupt, ignore */ }
}

function applyStylePayload(payload) {
  if (!payload || typeof payload !== "object") return;
  if (payload.bg) state.bg = payload.bg;
  if (payload.units) Object.assign(state.units, payload.units);
  if (payload.clusters) {
    Object.keys(payload.clusters).forEach((id) => {
      if (!state.clusters[id]) return; // unknown cluster id, skip
      Object.assign(state.clusters[id], payload.clusters[id]);
    });
  }
}

/* ---------------- panel: static controls ---------------- */

function wireStaticControls() {
  dom.floorPrev.addEventListener("click", () => stepFloor(-1));
  dom.floorNext.addEventListener("click", () => stepFloor(1));
  dom.floorSelect.addEventListener("change", () => selectFloor(dom.floorSelect.value, { fit: true }));

  document.querySelectorAll(".btn-preset").forEach((btn) => {
    btn.addEventListener("click", () => applyPreset(btn.dataset.preset));
  });

  dom.bgColor.addEventListener("input", () => {
    state.bg = dom.bgColor.value;
    applyBg();
    saveToLocalStorage();
  });

  dom.chkUnitPolys.addEventListener("change", () => {
    state.units.showPolygons = dom.chkUnitPolys.checked;
    applyUnitsLayer();
    saveToLocalStorage();
  });
  dom.chkUnitLabels.addEventListener("change", () => {
    state.units.showLabels = dom.chkUnitLabels.checked;
    applyUnitsLayer();
    saveToLocalStorage();
  });
  dom.chkOrigTexts.addEventListener("change", () => {
    state.units.showOriginalTexts = dom.chkOrigTexts.checked;
    applyUnitsLayer();
    saveToLocalStorage();
  });
  dom.unitLabelColor.addEventListener("input", () => {
    state.units.labelColor = dom.unitLabelColor.value;
    applyUnitsLayer();
    saveToLocalStorage();
  });

  dom.btnFit.addEventListener("click", () => { if (currentFloorData) fitToView(); });

  dom.btnExportStyle.addEventListener("click", exportStyleJson);
  dom.btnExportSvg.addEventListener("click", exportFloorSvg);
  dom.inputLoadStyle.addEventListener("change", onLoadStyleFile);

  document.addEventListener("keydown", (e) => {
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (/INPUT|SELECT|TEXTAREA/.test(tag)) return;
    if (e.key === "ArrowLeft") stepFloor(-1);
    else if (e.key === "ArrowRight") stepFloor(1);
  });

  wireStage();
}

function buildTypeColorInputs() {
  dom.typeColors.innerHTML = "";
  UNIT_TYPES.forEach((t) => {
    const wrap = document.createElement("div");
    wrap.className = "type-color-item";
    const label = document.createElement("span");
    label.textContent = t.label;
    const input = document.createElement("input");
    input.type = "color";
    input.value = DEFAULT_TYPE_COLORS[t.id];
    input.dataset.type = t.id;
    input.addEventListener("input", () => {
      state.units.typeColors[t.id] = input.value;
      applyUnitsLayer();
      saveToLocalStorage();
    });
    wrap.appendChild(input);
    wrap.appendChild(label);
    dom.typeColors.appendChild(wrap);
  });
}

function syncPanelInputs() {
  dom.bgColor.value = state.bg;
  dom.chkUnitPolys.checked = state.units.showPolygons;
  dom.chkUnitLabels.checked = state.units.showLabels;
  dom.chkOrigTexts.checked = state.units.showOriginalTexts;
  dom.unitLabelColor.value = state.units.labelColor;
  dom.typeColors.querySelectorAll("input[type=color]").forEach((inp) => {
    inp.value = state.units.typeColors[inp.dataset.type] || DEFAULT_TYPE_COLORS[inp.dataset.type];
  });
  state.clusterOrder.forEach(syncClusterRowInputs);
}

/* ---------------- panel: floor select ---------------- */

function populateFloorSelect() {
  dom.floorSelect.innerHTML = "";
  (state.index.floors || []).forEach((f) => {
    const opt = document.createElement("option");
    opt.value = f;
    opt.textContent = state.mode === "demo" ? "demo" : ("Этаж " + f);
    dom.floorSelect.appendChild(opt);
  });
}

function pickStartFloor(urlFloor) {
  const floors = (state.index.floors || []).map(String);
  if (urlFloor && floors.includes(String(urlFloor))) return urlFloor;
  return floors[0];
}

function stepFloor(dir) {
  const floors = (state.index.floors || []).map(String);
  const cur = String(state.currentFloor);
  const i = floors.indexOf(cur);
  const next = floors[i + dir];
  if (next != null) selectFloor(next, { fit: true });
}

function updateFloorNavButtons() {
  const floors = (state.index.floors || []).map(String);
  const i = floors.indexOf(String(state.currentFloor));
  dom.floorPrev.disabled = i <= 0;
  dom.floorNext.disabled = i === -1 || i >= floors.length - 1;
}

/* ---------------- cluster panel ---------------- */

function buildClusterPanel() {
  dom.clusterList.innerHTML = "";
  state.clusterOrder.forEach((id) => dom.clusterList.appendChild(buildClusterRow(id)));

  dom.clusterList.addEventListener("input", onClusterControlChange);
  dom.clusterList.addEventListener("change", onClusterControlChange);
  dom.clusterList.addEventListener("pointerover", (e) => {
    const row = e.target.closest(".cluster-row");
    if (row && row.dataset.id !== hoveredClusterId) setHoveredCluster(row.dataset.id);
  });
  dom.clusterList.addEventListener("pointerleave", () => setHoveredCluster(null));
}

function buildClusterRow(id) {
  const m = state.clusters[id];
  const row = document.createElement("div");
  row.className = "cluster-row";
  row.dataset.id = id;

  const swatchColor = m.origStroke || m.origFill || "#555";

  row.innerHTML = `
    <div class="cr-top">
      <span class="cr-swatch" style="background:${swatchColor}"></span>
      <span class="cr-id">${id}</span>
      <label class="mini" title="Показывать слой">
        <input type="checkbox" data-field="show" ${m.show ? "checked" : ""}>
      </label>
      <span class="cr-n">${m.n.toLocaleString("ru-RU")}</span>
    </div>
    <select class="cr-role" data-field="role">
      ${ROLES.map((r) => `<option value="${r.id}" ${r.id === m.role ? "selected" : ""}>${r.label}</option>`).join("")}
    </select>
    <div class="cr-controls">
      <label class="mini">обв.</label>
      <input type="color" data-field="stroke" value="${m.stroke}">
      <label class="mini">толщ.</label>
      <input type="number" data-field="width" min="0" max="4" step="0.1" value="${m.width}">
      <div class="cr-fill-row">
        <label class="mini">залив.</label>
        <input type="color" data-field="fill" value="${m.fill}">
        <label class="mini"><input type="checkbox" data-field="fillEnabled" ${m.fillEnabled ? "" : "checked"}> без заливки</label>
        <span style="flex:1"></span>
        <label class="mini">α</label>
        <input type="number" data-field="opacity" min="0" max="1" step="0.05" value="${m.opacity}" style="width:44px">
      </div>
    </div>
  `;
  return row;
}

function syncClusterRowInputs(id) {
  const m = state.clusters[id];
  const row = dom.clusterList.querySelector(`.cluster-row[data-id="${cssEsc(id)}"]`);
  if (!row || !m) return;
  row.querySelector('[data-field="show"]').checked = m.show;
  row.querySelector('[data-field="role"]').value = m.role;
  row.querySelector('[data-field="stroke"]').value = m.stroke;
  row.querySelector('[data-field="width"]').value = m.width;
  row.querySelector('[data-field="fill"]').value = m.fill;
  row.querySelector('[data-field="fillEnabled"]').checked = !m.fillEnabled;
  row.querySelector('[data-field="opacity"]').value = m.opacity;
  row.classList.toggle("hidden-row", !m.show);
}

function onClusterControlChange(e) {
  const field = e.target.dataset.field;
  if (!field) return;
  const row = e.target.closest(".cluster-row");
  const id = row.dataset.id;
  const m = state.clusters[id];
  if (!m) return;

  switch (field) {
    case "show": m.show = e.target.checked; break;
    case "role": m.role = e.target.value; break;
    case "stroke": m.stroke = e.target.value; break;
    case "width": m.width = clampNum(parseFloat(e.target.value) || 0, 0, 4); break;
    case "fill": m.fill = e.target.value; break;
    case "fillEnabled": m.fillEnabled = !e.target.checked; break; // checkbox reads "без заливки"
    case "opacity": m.opacity = clampNum(parseFloat(e.target.value), 0, 1); break;
  }
  row.classList.toggle("hidden-row", !m.show);
  applyClusterStyle(id);
  saveToLocalStorage();
}

function setHoveredCluster(id) {
  hoveredClusterId = id;
  const svg = dom.stageInner.querySelector("svg");
  if (!svg) return;
  svg.classList.toggle("hover-active", !!id);
  svg.querySelectorAll(".cluster-group.is-target").forEach((g) => g.classList.remove("is-target"));
  if (id) {
    svg.querySelectorAll(`[data-cluster="${cssEsc(id)}"]`).forEach((g) => g.classList.add("is-target"));
  }
}

function scrollToClusterRow(id) {
  const row = dom.clusterList.querySelector(`.cluster-row[data-id="${cssEsc(id)}"]`);
  if (!row) return;
  row.scrollIntoView({ block: "center", behavior: "smooth" });
  row.classList.add("flash");
  setTimeout(() => row.classList.remove("flash"), 900);
}

/* ---------------- presets ---------------- */

function applyPreset(name) {
  if (name === "pdf") {
    state.bg = "#FFFFFF";
    state.clusterOrder.forEach((id) => {
      const m = state.clusters[id];
      m.show = true;
      m.stroke = m.origStroke || "#1E1B17";
      m.width = effectiveWidth(m.origWidth);
      m.fillEnabled = !!m.origFill;
      m.fill = m.origFill || m.fill || "#888888";
      m.opacity = 1;
    });
  } else if (name === "clean") {
    state.bg = "#FFFFFF";
    state.clusterOrder.forEach((id) => {
      const m = state.clusters[id];
      const hide = m.role === "hatch" || m.role === "dims" || m.role === "icons";
      m.show = !hide;
      m.stroke = "#1E1B17";
      m.width = m.role === "walls" ? 1.2 : 0.4;
      m.fillEnabled = false;
      m.opacity = 1;
    });
  } else if (name === "ours") {
    state.bg = "#FFFFFF";
    state.clusterOrder.forEach((id) => {
      const m = state.clusters[id];
      const s = OURS_CLUSTER_STYLE[id];
      if (s) {
        m.show = true;
        m.opacity = 1;
        if (s.fill) {
          m.fillEnabled = true;
          m.fill = s.fill;
          m.stroke = "none";
        } else {
          m.fillEnabled = false;
          m.stroke = s.stroke;
          m.width = s.width;
        }
      } else {
        m.show = false;
      }
    });
  }

  syncPanelInputs();
  applyBg();
  applyAllClusterStyles();
  saveToLocalStorage();
}

/* ---------------- floor loading + svg build ---------------- */

async function selectFloor(floorId, opts) {
  opts = opts || {};
  state.currentFloor = floorId;
  dom.floorSelect.value = floorId;
  updateFloorNavButtons();
  const url = new URL(location.href);
  url.searchParams.set("floor", floorId);
  history.replaceState(null, "", url);

  let data = state.floorCache[floorId];
  if (!data) {
    const filename = (state.index.files || {})[floorId];
    if (!filename) { showEmptyStage("Для этажа " + floorId + " не указан файл в index.json."); return; }
    try {
      data = await fetchJSON("data/" + filename);
      state.floorCache[floorId] = data;
    } catch (e) {
      showEmptyStage("data/" + filename + " ещё не готов (файл не найден).");
      return;
    }
  }

  currentFloorData = data;
  mountFloorSvg(data);
  applyBg();
  applyAllClusterStyles();
  applyUnitsLayer();
  if (opts.fit) fitToView();
  setStatus(statusText());
}

function showEmptyStage(msg) {
  currentFloorData = null;
  dom.stageInner.innerHTML = "";
  dom.emptyMsg.hidden = false;
  dom.emptyMsg.textContent = msg;
  setStatus(msg, "warn");
}

function mountFloorSvg(data) {
  dom.emptyMsg.hidden = true;
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${data.w} ${data.h}`);
  svg.setAttribute("width", data.w);
  svg.setAttribute("height", data.h);

  const paper = document.createElementNS(SVGNS, "rect");
  paper.setAttribute("class", "paper");
  paper.setAttribute("x", 0);
  paper.setAttribute("y", 0);
  paper.setAttribute("width", data.w);
  paper.setAttribute("height", data.h);
  svg.appendChild(paper);

  const clustersLayer = document.createElementNS(SVGNS, "g");
  clustersLayer.setAttribute("class", "layer-clusters");
  (data.clusters || []).forEach((c) => {
    if (!state.clusters[c.id]) registerUnknownCluster(c.id, c);
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("data-cluster", c.id);
    g.setAttribute("class", "cluster-group");
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("d", (c.paths || []).join(" "));
    g.appendChild(path);
    clustersLayer.appendChild(g);
  });
  svg.appendChild(clustersLayer);

  const unitsLayer = document.createElementNS(SVGNS, "g");
  unitsLayer.setAttribute("class", "layer-units");
  const fillsGroup = document.createElementNS(SVGNS, "g");
  fillsGroup.setAttribute("class", "units-fills");
  const labelsGroup = document.createElementNS(SVGNS, "g");
  labelsGroup.setAttribute("class", "units-labels");

  Object.keys(data.units || {}).forEach((number) => {
    const u = data.units[number];
    const inv = state.unitsInventory && state.unitsInventory[number];
    const type = inv ? inv.type : "unknown";

    if (u.balcony) {
      const bal = document.createElementNS(SVGNS, "polygon");
      bal.setAttribute("class", "unit-balcony-fill");
      bal.setAttribute("points", pointsAttr(u.balcony));
      bal.dataset.type = type;
      fillsGroup.appendChild(bal);
    }
    const poly = document.createElementNS(SVGNS, "polygon");
    poly.setAttribute("class", "unit-fill");
    poly.setAttribute("points", pointsAttr(u.poly));
    poly.dataset.type = type;
    fillsGroup.appendChild(poly);

    const c = polygonCentroid(u.poly);
    const text = document.createElementNS(SVGNS, "text");
    text.setAttribute("class", "unit-label");
    text.setAttribute("x", c.x);
    text.setAttribute("y", c.y);
    text.setAttribute("text-anchor", "middle");
    const t1 = document.createElementNS(SVGNS, "tspan");
    t1.setAttribute("class", "num");
    t1.setAttribute("x", c.x);
    t1.setAttribute("dy", "-4");
    t1.setAttribute("font-size", "11");
    t1.textContent = number;
    const t2 = document.createElementNS(SVGNS, "tspan");
    t2.setAttribute("class", "area");
    t2.setAttribute("x", c.x);
    t2.setAttribute("dy", "13");
    t2.setAttribute("font-size", "9");
    t2.textContent = inv ? formatArea(inv.total) : "";
    text.appendChild(t1);
    text.appendChild(t2);
    labelsGroup.appendChild(text);
  });

  unitsLayer.appendChild(fillsGroup);
  unitsLayer.appendChild(labelsGroup);
  svg.appendChild(unitsLayer);

  const origTextsLayer = document.createElementNS(SVGNS, "g");
  origTextsLayer.setAttribute("class", "layer-orig-texts");
  (data.texts || []).forEach((t) => {
    const text = document.createElementNS(SVGNS, "text");
    text.setAttribute("x", t.x);
    text.setAttribute("y", t.y);
    text.setAttribute("font-size", t.size || 8);
    text.textContent = t.str;
    origTextsLayer.appendChild(text);
  });
  svg.appendChild(origTextsLayer);

  svg.addEventListener("click", (e) => {
    if (draggedThisGesture) return;
    const g = e.target.closest("[data-cluster]");
    if (g) scrollToClusterRow(g.dataset.cluster);
  });

  dom.stageInner.innerHTML = "";
  dom.stageInner.appendChild(svg);
}

function pointsAttr(pts) {
  return (pts || []).map((p) => p[0] + "," + p[1]).join(" ");
}

function polygonCentroid(pts) {
  if (!pts || !pts.length) return { x: 0, y: 0 };
  let area = 0, cx = 0, cy = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x0, y0] = pts[i];
    const [x1, y1] = pts[(i + 1) % pts.length];
    const cross = x0 * y1 - x1 * y0;
    area += cross;
    cx += (x0 + x1) * cross;
    cy += (y0 + y1) * cross;
  }
  area *= 0.5;
  if (Math.abs(area) < 1e-6) {
    const n = pts.length;
    const sx = pts.reduce((a, p) => a + p[0], 0) / n;
    const sy = pts.reduce((a, p) => a + p[1], 0) / n;
    return { x: sx, y: sy };
  }
  return { x: cx / (6 * area), y: cy / (6 * area) };
}

function formatArea(v) {
  if (v == null) return "";
  return v.toFixed(1).replace(".", ",") + " м²";
}

/* ---------------- style application ---------------- */

function applyBg() {
  const rect = dom.stageInner.querySelector("svg .paper");
  if (rect) rect.setAttribute("fill", state.bg);
}

function applyClusterStyle(id) {
  const m = state.clusters[id];
  if (!m) return;
  const svg = dom.stageInner.querySelector("svg");
  if (!svg) return;
  svg.querySelectorAll(`[data-cluster="${cssEsc(id)}"]`).forEach((g) => {
    g.style.display = m.show ? "" : "none";
    g.style.setProperty("--c-stroke", m.stroke);
    g.style.setProperty("--c-width", m.width);
    g.style.setProperty("--c-fill", m.fillEnabled ? m.fill : "none");
    g.style.setProperty("--c-opacity", m.opacity);
  });
}

function applyAllClusterStyles() {
  state.clusterOrder.forEach(applyClusterStyle);
}

function applyUnitsLayer() {
  const svg = dom.stageInner.querySelector("svg");
  if (!svg) return;
  const fillsGroup = svg.querySelector(".units-fills");
  const labelsGroup = svg.querySelector(".units-labels");
  const origGroup = svg.querySelector(".layer-orig-texts");
  if (fillsGroup) fillsGroup.style.display = state.units.showPolygons ? "" : "none";
  if (labelsGroup) labelsGroup.style.display = state.units.showLabels ? "" : "none";
  if (origGroup) origGroup.style.display = state.units.showOriginalTexts ? "" : "none";

  if (fillsGroup) {
    fillsGroup.querySelectorAll(".unit-fill, .unit-balcony-fill").forEach((el) => {
      const type = el.dataset.type;
      const color = state.units.typeColors[type] || UNKNOWN_TYPE_COLOR;
      el.setAttribute("fill", color);
      el.setAttribute("fill-opacity", el.classList.contains("unit-balcony-fill")
        ? state.units.fillOpacity * 0.6 : state.units.fillOpacity);
    });
  }
  if (labelsGroup) {
    labelsGroup.querySelectorAll(".unit-label").forEach((t) => t.setAttribute("fill", state.units.labelColor));
  }
}

/* ---------------- scene: pan / zoom / fit ---------------- */

let dragState = null;
let draggedThisGesture = false;

function wireStage() {
  dom.stage.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    dragState = { x: e.clientX, y: e.clientY, tx: transform.tx, ty: transform.ty };
    draggedThisGesture = false;
    dom.stage.classList.add("dragging");
    window.addEventListener("mousemove", onDragMove);
    window.addEventListener("mouseup", onDragEnd);
  });

  dom.stage.addEventListener("wheel", (e) => {
    if (!currentFloorData) return;
    e.preventDefault();
    const rect = dom.stage.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const factor = e.deltaY > 0 ? 0.9 : 1.1;
    const newScale = clampNum(transform.scale * factor, 0.02, 40);
    const wx = (mx - transform.tx) / transform.scale;
    const wy = (my - transform.ty) / transform.scale;
    transform.scale = newScale;
    transform.tx = mx - wx * newScale;
    transform.ty = my - wy * newScale;
    applyTransform();
  }, { passive: false });
}

function onDragMove(e) {
  if (!dragState) return;
  const dx = e.clientX - dragState.x;
  const dy = e.clientY - dragState.y;
  if (Math.abs(dx) > 3 || Math.abs(dy) > 3) draggedThisGesture = true;
  transform.tx = dragState.tx + dx;
  transform.ty = dragState.ty + dy;
  applyTransform();
}

function onDragEnd() {
  dragState = null;
  dom.stage.classList.remove("dragging");
  window.removeEventListener("mousemove", onDragMove);
  window.removeEventListener("mouseup", onDragEnd);
}

function applyTransform() {
  dom.stageInner.style.transform = `translate(${transform.tx}px, ${transform.ty}px) scale(${transform.scale})`;
  dom.zoomReadout.textContent = Math.round(transform.scale * 100) + "%";
}

function fitToView() {
  if (!currentFloorData) return;
  const rect = dom.stage.getBoundingClientRect();
  const sx = rect.width / currentFloorData.w;
  const sy = rect.height / currentFloorData.h;
  const scale = Math.min(sx, sy) * 0.94;
  transform.scale = scale;
  transform.tx = (rect.width - currentFloorData.w * scale) / 2;
  transform.ty = (rect.height - currentFloorData.h * scale) / 2;
  applyTransform();
}

/* ---------------- export / import ---------------- */

function exportStyleJson() {
  const payload = { version: 1, bg: state.bg, clusters: {}, units: state.units };
  state.clusterOrder.forEach((id) => {
    const m = state.clusters[id];
    payload.clusters[id] = {
      show: m.show, role: m.role, stroke: m.stroke, width: m.width,
      fill: m.fill, fillEnabled: m.fillEnabled, opacity: m.opacity,
    };
  });
  downloadText(JSON.stringify(payload, null, 2), "style.json", "application/json");
}

function onLoadStyleFile(e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      applyStylePayload(JSON.parse(reader.result));
      syncPanelInputs();
      applyBg();
      applyAllClusterStyles();
      applyUnitsLayer();
      saveToLocalStorageNow();
      setStatus("style.json загружен", "");
    } catch (err) {
      setStatus("Не удалось прочитать style.json: " + err.message, "warn");
    }
  };
  reader.readAsText(file);
  e.target.value = "";
}

function exportFloorSvg() {
  if (!currentFloorData) return;
  const data = currentFloorData;
  const parts = [];
  parts.push(`<svg xmlns="${SVGNS}" viewBox="0 0 ${data.w} ${data.h}" width="${data.w}" height="${data.h}">`);
  parts.push(`<rect x="0" y="0" width="${data.w}" height="${data.h}" fill="${state.bg}"/>`);

  (data.clusters || []).forEach((c) => {
    const m = state.clusters[c.id];
    if (!m || !m.show) return;
    const d = (c.paths || []).join(" ");
    const fill = m.fillEnabled ? m.fill : "none";
    parts.push(`<g id="${xmlEsc(c.id)}"><path d="${d}" stroke="${m.stroke}" stroke-width="${m.width}" fill="${fill}" opacity="${m.opacity}"/></g>`);
  });

  if (state.units.showPolygons) {
    parts.push('<g class="units-fills">');
    Object.keys(data.units || {}).forEach((number) => {
      const u = data.units[number];
      const inv = state.unitsInventory && state.unitsInventory[number];
      const type = inv ? inv.type : "unknown";
      const color = state.units.typeColors[type] || UNKNOWN_TYPE_COLOR;
      if (u.balcony) {
        parts.push(`<polygon points="${pointsAttr(u.balcony)}" fill="${color}" fill-opacity="${state.units.fillOpacity * 0.6}" stroke="rgba(0,0,0,0.2)" stroke-width="0.5"/>`);
      }
      parts.push(`<polygon points="${pointsAttr(u.poly)}" fill="${color}" fill-opacity="${state.units.fillOpacity}" stroke="rgba(0,0,0,0.35)" stroke-width="0.6"/>`);
    });
    parts.push("</g>");
  }

  if (state.units.showLabels) {
    parts.push('<g class="units-labels" font-family="DM Sans, sans-serif">');
    Object.keys(data.units || {}).forEach((number) => {
      const u = data.units[number];
      const inv = state.unitsInventory && state.unitsInventory[number];
      const c = polygonCentroid(u.poly);
      const area = inv ? formatArea(inv.total) : "";
      parts.push(`<text x="${c.x}" y="${c.y}" text-anchor="middle" fill="${state.units.labelColor}">` +
        `<tspan x="${c.x}" dy="-4" font-size="11" font-weight="700">${xmlEsc(number)}</tspan>` +
        (area ? `<tspan x="${c.x}" dy="13" font-size="9">${xmlEsc(area)}</tspan>` : "") +
        `</text>`);
    });
    parts.push("</g>");
  }

  if (state.units.showOriginalTexts) {
    parts.push('<g class="orig-texts" font-family="DM Sans, sans-serif" fill="#66605A" opacity="0.85">');
    (data.texts || []).forEach((t) => {
      parts.push(`<text x="${t.x}" y="${t.y}" font-size="${t.size || 8}">${xmlEsc(t.str)}</text>`);
    });
    parts.push("</g>");
  }

  parts.push("</svg>");
  downloadText(parts.join(""), `floor-${data.floor}-style.svg`, "image/svg+xml");
}

function downloadText(text, filename, mime) {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/* ---------------- misc helpers ---------------- */

function clampNum(v, min, max) {
  if (Number.isNaN(v)) v = min;
  return Math.min(max, Math.max(min, v));
}

function cssEsc(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

function xmlEsc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;",
  }[c]));
}

function statusText() {
  const bits = [];
  bits.push(state.mode === "demo" ? "demo-данные (index.json / floor-N.json не найдены)" : "данные загружены");
  bits.push(state.clusterOrder.length + " слоёв");
  if (!state.unitsInventory) bits.push("инвентарь квартир не найден — цвета типов недоступны");
  return bits.join(" · ");
}

function setStatus(text, cls) {
  dom.statusLine.textContent = text;
  dom.statusLine.className = "status-line" + (cls ? " " + cls : "");
  dom.statusMsg.textContent = text;
  dom.statusMsg.className = "status-msg" + (cls ? " " + cls : "");
}

function showFatal(text) {
  dom.fatalOverlay.hidden = false;
  dom.fatalText.textContent = text;
}
