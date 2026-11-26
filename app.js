/* ============================================================================
 * Logic Synthesis Studio & Industrial VCD Waveform Viewer
 * Pure HTML5 / CSS3 / ES6 + Pyodide v0.26.2 (Zero Backend, Zero npm Build)
 * ============================================================================ */

const PRESETS = {
  default_sop: {
    expr: "F = A'B'C + A'BC + AB'C' + AB'C + ABC",
    dc: "",
    tech: "cmos"
  },
  minterm_dc: {
    expr: "F(A,B,C,D) = m(1,3,4,5,7,9,11,13,15) + d(2,6)",
    dc: "",
    tech: "cmos"
  },
  petrick_cyclic: {
    expr: "F(A,B,C) = m(0,1,2,5,6,7)",
    dc: "",
    tech: "cmos"
  },
  bcd_seg_a: {
    expr: "SegA(A,B,C,D) = m(0,2,3,5,6,7,8,9) + d(10-15)",
    dc: "",
    tech: "nand"
  },
  alu_ctrl_5v: {
    expr: "Ctrl(A,B,C,D,E) = m(1,3,5,7,17,19,21,23,24,25,28,29) + d(15,31)",
    dc: "",
    tech: "cmos"
  }
};

const studioState = {
  pyodide: null,
  core: null,
  lastSynth: null,
  waveform: null,
  vcdRawText: "",
  moduleName: "synth_logic",

  // Truth table live state
  varNames: ["A", "B", "C"],
  ttStates: ["0", "1", "0", "1", "1", "1", "0", "1"],
  outputName: "F",

  // Schematic viewport state
  schemPanX: 0,
  schemPanY: 0,
  schemZoom: 1.0,
  schemDragging: false,
  schemLastX: 0,
  schemLastY: 0,

  // GTKWave VCD viewer state
  viewStartNs: 0,
  viewEndNs: 180,
  cursorANs: 10.0,
  cursorBNs: 15.5,
  activeCursorMode: "A", // "A" or "B" on left click
  snapToEdge: true,
  wavePanning: false,
  wavePanLastX: 0
};

// ============================================================================
// Boot Pyodide & Mount ./src/eda_core.py via Relative Paths
// ============================================================================

function setBootBar(pct, text) {
  const bar = document.getElementById("boot-bar-inner");
  const msg = document.getElementById("boot-status-text");
  if (bar) bar.style.width = `${pct}%`;
  if (msg) msg.textContent = text;
}

async function initPyodideEngine() {
  try {
    setBootBar(20, "Đang tải runtime Pyodide v0.26.2 WebAssembly...");
    const pyodide = await loadPyodide({
      indexURL: "https://cdn.jsdelivr.net/pyodide/v0.26.2/full/"
    });
    studioState.pyodide = pyodide;

    setBootBar(60, "Đang nạp lõi tổng hợp ./src/eda_core.py vào pyodide.FS...");
    await pyodide.runPythonAsync(`
import os, sys
os.makedirs("src", exist_ok=True)
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())
    `);

    const cwd = pyodide.FS.cwd();
    const initResp = await fetch("./src/__init__.py", { cache: "no-store" });
    const initCode = initResp.ok ? await initResp.text() : "";
    pyodide.FS.writeFile(`${cwd}/src/__init__.py`, initCode);

    const coreResp = await fetch("./src/eda_core.py", { cache: "no-store" });
    if (!coreResp.ok) {
      throw new Error(`Không thể tải ./src/eda_core.py (HTTP ${coreResp.status})`);
    }
    const coreCode = await coreResp.text();
    pyodide.FS.writeFile(`${cwd}/src/eda_core.py`, coreCode);

    setBootBar(85, "Đang khởi tạo thuật toán Quine-McCluskey + Petrick & VCD Parser...");
    await pyodide.runPythonAsync(`
import importlib
from src import eda_core
importlib.reload(eda_core)
    `);
    studioState.core = pyodide.globals.get("eda_core");

    setBootBar(96, "Đang tổng hợp mạch mẫu mặc định...");
    runExpressionSynthesis(true);

    document.getElementById("wasm-status").textContent =
      "Pyodide v0.26.2 Ready • Pure Python Stdlib Engine";
    setBootBar(100, "Hoàn tất!");
    setTimeout(() => {
      document.getElementById("boot-overlay").classList.add("hidden");
    }, 160);
  } catch (err) {
    console.error(err);
    const errText =
      (err && (err.message || (err.errno !== undefined ? `FS Errno ${err.errno}` : String(err)))) ||
      String(err);
    setBootBar(
      100,
      `Lỗi khởi tạo: ${errText}. Hãy mở qua GitHub Pages hoặc chạy 'python -m http.server 8000'.`
    );
  }
}

// ============================================================================
// Synthesis Invocations (Expression / Minterms & Interactive Truth Table)
// ============================================================================

function showError(msg) {
  const box = document.getElementById("synth-error-box");
  if (!msg) {
    box.hidden = true;
    box.textContent = "";
  } else {
    box.hidden = false;
    box.textContent = msg;
  }
}

function runExpressionSynthesis(resetCursors = false) {
  if (!studioState.core) return;
  const expr = document.getElementById("expr-input").value.trim();
  const dc = document.getElementById("dc-input").value.trim();
  const tech = document.getElementById("tech-select").value;

  const rawJson = studioState.core.synthesize_expression_json(
    expr,
    dc,
    "",
    tech,
    "synth_logic"
  );
  const res = JSON.parse(rawJson);
  if (!res.ok) {
    showError(res.error);
    return;
  }
  showError("");
  applySynthesisResult(res, resetCursors);
}

function runTruthTableSynthesis() {
  if (!studioState.core) return;
  const tech = document.getElementById("tech-select").value;
  const rawJson = studioState.core.synthesize_truth_table_json(
    JSON.stringify(studioState.varNames),
    JSON.stringify(studioState.ttStates),
    studioState.outputName || "F",
    tech,
    "synth_logic"
  );
  const res = JSON.parse(rawJson);
  if (!res.ok) {
    showError(res.error);
    return;
  }
  showError("");

  // Sync expression input box with compact minterm + don't-care representation
  const mPart = `m(${res.minterms.join(",")})`;
  const dPart = res.dont_cares.length > 0 ? ` + d(${res.dont_cares.join(",")})` : "";
  document.getElementById("expr-input").value =
    `${res.output_name}(${res.var_names.join(",")}) = ${mPart}${dPart}`;
  document.getElementById("dc-input").value = "";

  applySynthesisResult(res, false);
}

function applySynthesisResult(res, resetCursors) {
  studioState.lastSynth = res;
  studioState.varNames = res.var_names;
  studioState.ttStates = res.truth_table;
  studioState.outputName = res.output_name;
  studioState.vcdRawText = res.vcd_raw;
  studioState.moduleName = res.module_name;

  // 1. Update Left Summary & Truth Table
  document.getElementById("minimized-sop-display").textContent =
    `${res.output_name} = ${res.minimized_expr}`;
  document.getElementById("pi-count-val").textContent = String(res.prime_implicants.length);
  document.getElementById("epi-list-val").textContent =
    res.essential_pis.length > 0 ? res.essential_pis.join(", ") : "None (Petrick Core)";

  const varCountSel = document.getElementById("tt-var-count");
  if (res.var_names.length >= 2 && res.var_names.length <= 8) {
    varCountSel.value = String(res.var_names.length);
  }
  renderTruthTable();

  // 2. Update PPA & Static Timing Card
  const ppa = res.ppa;
  document.getElementById("ppa-saving").textContent = `${ppa.area_saving_pct.toFixed(1)}%`;
  document.getElementById("ppa-gates-detail").textContent =
    `${ppa.pre_gate_count} \u2192 ${ppa.post_gate_count} gates (${ppa.post_area_ge} GE)`;
  document.getElementById("ppa-crit-delay").textContent = `${ppa.critical_path_ns.toFixed(2)} ns`;
  document.getElementById("ppa-pre-delay").textContent =
    `Pre-opt: ${ppa.pre_critical_ns.toFixed(2)} ns`;
  document.getElementById("ppa-fmax").textContent = `${ppa.fmax_mhz.toFixed(2)} MHz`;

  document.getElementById("cnt-not").textContent = String(ppa.gate_breakdown.NOT || 0);
  document.getElementById("cnt-nand").textContent = String(ppa.gate_breakdown.NAND || 0);
  document.getElementById("cnt-and").textContent = String(ppa.gate_breakdown.AND || 0);
  document.getElementById("cnt-or").textContent = String(ppa.gate_breakdown.OR || 0);
  document.getElementById("ppa-crit-trace").textContent =
    ppa.critical_path_summary.join(" \u2192 ");

  // 3. Render IEEE Gate-Level Schematic
  renderGateSchematic(res.schematic);

  // 4. Render Verilog-2001 & Testbench
  document.getElementById("design-filename").textContent = `${res.module_name}.v (Verilog-2001)`;
  document.getElementById("tb-filename").textContent =
    `${res.module_name}_tb.v (Self-Checking + $dumpvars)`;
  document.getElementById("verilog-design-code").textContent = res.verilog_module;
  document.getElementById("verilog-tb-code").textContent = res.verilog_tb;

  // 5. Render Quine-McCluskey Chart Tab
  renderQuineMcCluskeyTable(res);

  // 6. Load Synthesized Waveform into Bottom VCD Viewer
  document.getElementById("vcd-source-badge").textContent =
    `Synthesized Gate Sim (${res.waveform.timescale})`;
  loadWaveformData(res.waveform, resetCursors, ppa.critical_path_ns);
}

// ============================================================================
// Interactive Truth Table Renderer (Click to cycle 0 -> 1 -> X -> 0)
// ============================================================================

function renderTruthTable() {
  const thead = document.getElementById("tt-thead");
  const tbody = document.getElementById("tt-tbody");
  const vars = studioState.varNames;
  const nVars = vars.length;

  thead.innerHTML = `
    <tr>
      <th>#</th>
      ${vars.map((v) => `<th>${escapeHtml(v)}</th>`).join("")}
      <th>${escapeHtml(studioState.outputName || "F")}</th>
    </tr>
  `;

  const frag = document.createDocumentFragment();
  const total = 1 << nVars;
  for (let idx = 0; idx < total; idx++) {
    const tr = document.createElement("tr");
    let bitsHtml = `<td class="tt-idx">${idx}</td>`;
    for (let b = nVars - 1; b >= 0; b--) {
      bitsHtml += `<td>${(idx >> b) & 1}</td>`;
    }
    const st = studioState.ttStates[idx] || "0";
    bitsHtml += `
      <td class="tt-out-cell" data-idx="${idx}" title="Click để chuyển 0 → 1 → X">
        <span class="tt-pill st-${st}">${st}</span>
      </td>
    `;
    tr.innerHTML = bitsHtml;
    tr.querySelector(".tt-out-cell").addEventListener("click", () => {
      const order = ["0", "1", "X"];
      const cur = studioState.ttStates[idx] || "0";
      studioState.ttStates[idx] = order[(order.indexOf(cur) + 1) % 3];
      runTruthTableSynthesis();
    });
    frag.appendChild(tr);
  }
  tbody.replaceChildren(frag);
}

function renderQuineMcCluskeyTable(res) {
  document.getElementById("qm-canon-expr").textContent = `${res.output_name} = ${res.canonical_expr}`;
  document.getElementById("qm-min-expr").textContent = `${res.output_name} = ${res.minimized_expr}`;

  const tbody = document.getElementById("qm-tbody");
  if (!res.prime_implicants.length) {
    tbody.innerHTML = `<tr><td colspan="6">Constant Function (${escapeHtml(res.minimized_expr)})</td></tr>`;
    return;
  }

  tbody.innerHTML = res.prime_implicants
    .map((pi) => {
      let roleHtml = `<span class="mono-dim">Redundant</span>`;
      if (pi.is_essential) {
        roleHtml = `<span class="role-epi">&#10004; Essential PI</span>`;
      } else if (pi.is_selected) {
        roleHtml = `<span class="role-petrick">&#9733; Selected (Petrick)</span>`;
      }
      return `
        <tr>
          <td>PI_${pi.index}</td>
          <td><code>${escapeHtml(pi.pattern)}</code></td>
          <td><strong>${escapeHtml(pi.algebraic)}</strong></td>
          <td>${pi.literal_count}</td>
          <td>m(${pi.covered_minterms.join(", ")})</td>
          <td>${roleHtml}</td>
        </tr>
      `;
    })
    .join("");
}

// ============================================================================
// IEEE Std 91 SVG Gate-Level Schematic Renderer + Red Critical Path + Pan/Zoom
// ============================================================================

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    el.setAttribute(k, String(v));
  }
  return el;
}

function getNodeGeometry(node) {
  if (node.kind === "INPUT" || node.kind === "CONST") {
    return { w: 54, h: 28, inX: node.x - 27, outX: node.x + 27 };
  }
  if (node.kind === "OUTPUT") {
    return { w: 56, h: 28, inX: node.x - 28, outX: node.x + 28 };
  }
  if (node.kind === "NOT") {
    return { w: 52, h: 36, inX: node.x - 26, outX: node.x + 28 };
  }
  const fanin = Math.max(2, (node.inputs || []).length);
  const h = Math.max(44, Math.min(92, 22 + fanin * 11));
  const w = 66;
  const hasBubble = node.kind === "NAND";
  return {
    w,
    h,
    inX: node.x - w / 2,
    outX: node.x + w / 2 + (hasBubble ? 9 : 0)
  };
}

function getInputPinPos(targetNode, pinIndex, pinCount) {
  const geom = getNodeGeometry(targetNode);
  if (pinCount <= 1) {
    return { x: geom.inX, y: targetNode.y };
  }
  const usableH = geom.h * 0.72;
  const topY = targetNode.y - usableH / 2;
  const py = topY + (usableH * pinIndex) / (pinCount - 1);
  // Slight x inset for concave OR input back-arc so wires touch the curved body cleanly
  const orInset = targetNode.kind === "OR" ? 6 : 0;
  return { x: geom.inX + orInset, y: py };
}

function buildIeeeGateShape(group, node, geom) {
  const x0 = node.x - geom.w / 2;
  const y0 = node.y - geom.h / 2;
  const w = geom.w;
  const h = geom.h;

  if (node.kind === "INPUT" || node.kind === "CONST" || node.kind === "OUTPUT") {
    group.appendChild(
      svgEl("rect", {
        class: "io-terminal",
        x: x0,
        y: y0,
        width: w,
        height: h,
        rx: node.kind === "OUTPUT" ? 13 : 5
      })
    );
    const txt = svgEl("text", {
      class: "gate-lbl",
      x: node.x,
      y: node.y + 4,
      "text-anchor": "middle"
    });
    txt.textContent = node.label;
    group.appendChild(txt);
    return;
  }

  if (node.kind === "NOT") {
    // IEEE Triangle + Inversion Bubble
    const triRight = x0 + w - 8;
    const pts = `${x0},${y0} ${triRight},${node.y} ${x0},${y0 + h}`;
    group.appendChild(svgEl("polygon", { class: "gate-body", points: pts }));
    group.appendChild(
      svgEl("circle", {
        class: "gate-body",
        cx: triRight + 5,
        cy: node.y,
        r: 4.5
      })
    );
  } else if (node.kind === "AND" || node.kind === "NAND") {
    // IEEE D-shaped AND Gate (flat left back + semicircular right nose)
    const midX = x0 + w * 0.52;
    const r = h / 2;
    const d = `M ${x0} ${y0} L ${midX} ${y0} A ${r} ${r} 0 0 1 ${midX} ${y0 + h} L ${x0} ${y0 + h} Z`;
    group.appendChild(svgEl("path", { class: "gate-body", d }));
    if (node.kind === "NAND") {
      group.appendChild(
        svgEl("circle", {
          class: "gate-body",
          cx: midX + r + 4.8,
          cy: node.y,
          r: 4.5
        })
      );
    }
  } else if (node.kind === "OR") {
    // IEEE Pointed Ogive OR Gate with Concave Input Arc
    const tipX = x0 + w;
    const d = [
      `M ${x0} ${y0}`,
      `Q ${x0 + w * 0.25} ${node.y} ${x0} ${y0 + h}`,
      `Q ${x0 + w * 0.64} ${y0 + h} ${tipX} ${node.y}`,
      `Q ${x0 + w * 0.64} ${y0} ${x0} ${y0}`,
      "Z"
    ].join(" ");
    group.appendChild(svgEl("path", { class: "gate-body", d }));
  }

  const kindTxt = svgEl("text", {
    class: "gate-lbl",
    x: node.x - 2,
    y: node.y + 3.5,
    "text-anchor": "middle"
  });
  kindTxt.textContent = node.kind;
  group.appendChild(kindTxt);

  const delayTxt = svgEl("text", {
    class: "gate-timing-tag",
    x: node.x,
    y: y0 + h + 13,
    "text-anchor": "middle"
  });
  delayTxt.textContent = `+${node.delay_ns.toFixed(1)}ns (t=${node.arrival_ns.toFixed(1)}ns)`;
  group.appendChild(delayTxt);
}

function renderGateSchematic(schematic) {
  const world = document.getElementById("schem-world");
  const svg = document.getElementById("schematic-svg");
  world.replaceChildren();

  const nodeMap = {};
  let maxY = 400;
  for (const nd of schematic.nodes) {
    nodeMap[nd.id] = nd;
    if (nd.y + 85 > maxY) maxY = nd.y + 85;
  }
  svg.setAttribute("viewBox", `0 0 1100 ${Math.round(maxY)}`);

  // Draw non-critical wires first, then critical wires on top so red path is unbroken
  const orderedWires = [...schematic.wires].sort(
    (a, b) => Number(a.is_critical) - Number(b.is_critical)
  );

  for (let i = 0; i < orderedWires.length; i++) {
    const w = orderedWires[i];
    const src = nodeMap[w.source];
    const dst = nodeMap[w.target];
    if (!src || !dst) continue;

    const srcGeom = getNodeGeometry(src);
    const x1 = srcGeom.outX;
    const y1 = src.y;
    const dstPin = getInputPinPos(dst, w.pin_index, w.pin_count);
    const x2 = dstPin.x;
    const y2 = dstPin.y;

    // Stagger orthogonal channel X slightly by pin_index to avoid overlapping vertical trunks
    const channelOffset = ((w.pin_index % 5) - 2) * 7;
    const midX = (x1 + x2) * 0.5 + channelOffset;
    const d = `M ${x1} ${y1} L ${midX} ${y1} L ${midX} ${y2} L ${x2} ${y2}`;

    const path = svgEl("path", {
      class: w.is_critical ? "schem-wire critical" : "schem-wire",
      d
    });
    world.appendChild(path);
  }

  // Draw gates & I/O terminals
  const tooltip = document.getElementById("schem-tooltip");
  const wrap = document.getElementById("schem-viewport-wrap");

  for (const nd of schematic.nodes) {
    const geom = getNodeGeometry(nd);
    const g = svgEl("g", {
      class: nd.is_critical ? "gate-node critical" : "gate-node"
    });
    buildIeeeGateShape(g, nd, geom);

    g.addEventListener("mousemove", (ev) => {
      const rect = wrap.getBoundingClientRect();
      tooltip.innerHTML = `
        <div><strong style="color:#38BDF8">${escapeHtml(nd.label)}</strong> ${
          nd.is_critical ? '<span style="color:#FF4D6D">[CRITICAL]</span>' : ""
        }</div>
        <div>Gate Type: ${escapeHtml(nd.kind)} (${nd.delay_ns.toFixed(1)} ns)</div>
        <div>Arrival Time: ${nd.arrival_ns.toFixed(2)} ns</div>
      `;
      tooltip.style.left = `${Math.min(rect.width - 210, ev.clientX - rect.left + 14)}px`;
      tooltip.style.top = `${Math.max(8, ev.clientY - rect.top - 10)}px`;
      tooltip.hidden = false;
    });
    g.addEventListener("mouseleave", () => {
      tooltip.hidden = true;
    });

    world.appendChild(g);
  }

  applySchematicTransform();
}

function applySchematicTransform() {
  const world = document.getElementById("schem-world");
  world.setAttribute(
    "transform",
    `translate(${studioState.schemPanX.toFixed(1)}, ${studioState.schemPanY.toFixed(1)}) scale(${studioState.schemZoom.toFixed(3)})`
  );
  document.getElementById("schem-zoom-lbl").textContent =
    `${Math.round(studioState.schemZoom * 100)}%`;
}

function setupSchematicPanZoom() {
  const wrap = document.getElementById("schem-viewport-wrap");
  const svg = document.getElementById("schematic-svg");

  function toSvgPoint(clientX, clientY) {
    const ctm = svg.getScreenCTM();
    if (ctm) {
      const pt = svg.createSVGPoint();
      pt.x = clientX;
      pt.y = clientY;
      return pt.matrixTransform(ctm.inverse());
    }
    return { x: clientX, y: clientY };
  }

  wrap.addEventListener("mousedown", (ev) => {
    if (ev.button !== 0) return;
    studioState.schemDragging = true;
    studioState.schemLastX = ev.clientX;
    studioState.schemLastY = ev.clientY;
    wrap.classList.add("panning");
  });

  window.addEventListener("mousemove", (ev) => {
    if (!studioState.schemDragging) return;
    const p0 = toSvgPoint(studioState.schemLastX, studioState.schemLastY);
    const p1 = toSvgPoint(ev.clientX, ev.clientY);
    studioState.schemPanX += p1.x - p0.x;
    studioState.schemPanY += p1.y - p0.y;
    studioState.schemLastX = ev.clientX;
    studioState.schemLastY = ev.clientY;
    applySchematicTransform();
  });

  window.addEventListener("mouseup", () => {
    studioState.schemDragging = false;
    wrap.classList.remove("panning");
  });

  wrap.addEventListener(
    "wheel",
    (ev) => {
      ev.preventDefault();
      const pt = toSvgPoint(ev.clientX, ev.clientY);
      const factor = ev.deltaY < 0 ? 1.13 : 1 / 1.13;
      const nextZ = Math.min(3.0, Math.max(0.45, studioState.schemZoom * factor));
      studioState.schemPanX = pt.x - ((pt.x - studioState.schemPanX) / studioState.schemZoom) * nextZ;
      studioState.schemPanY = pt.y - ((pt.y - studioState.schemPanY) / studioState.schemZoom) * nextZ;
      studioState.schemZoom = nextZ;
      applySchematicTransform();
    },
    { passive: false }
  );

  document.getElementById("btn-schem-zin").addEventListener("click", () => {
    studioState.schemZoom = Math.min(3.0, studioState.schemZoom * 1.18);
    applySchematicTransform();
  });
  document.getElementById("btn-schem-zout").addEventListener("click", () => {
    studioState.schemZoom = Math.max(0.45, studioState.schemZoom / 1.18);
    applySchematicTransform();
  });
  document.getElementById("btn-schem-fit").addEventListener("click", () => {
    studioState.schemPanX = 0;
    studioState.schemPanY = 0;
    studioState.schemZoom = 1.0;
    applySchematicTransform();
  });
}

// ============================================================================
// Industrial GTKWave-Style VCD Waveform Viewer (Canvas + Zoom + Cursors A & B)
// ============================================================================

const WAVE_RULER_H = 28;
const WAVE_ROW_H = 34;

function loadWaveformData(wave, resetView = true, critDelayNs = 5.5) {
  studioState.waveform = wave;
  const maxNs = Math.max(10, Number(wave.end_time_ns || 100));

  if (resetView || studioState.viewEndNs > maxNs * 1.5 || studioState.viewEndNs <= 0) {
    studioState.viewStartNs = 0;
    studioState.viewEndNs = Math.min(maxNs, Math.max(40, Math.min(180, maxNs)));
    studioState.cursorANs = Math.min(maxNs * 0.25, 10.0);
    studioState.cursorBNs = Math.min(maxNs, studioState.cursorANs + (critDelayNs || 5.5));
  }

  renderWaveSignalSidebar();
  renderVcdCanvas();
  updateCursorReadout();
}

function sampleSignalAtTime(sig, timeNs) {
  const trans = sig.transitions || [];
  if (!trans.length) return { state: "x", label: "X" };
  let cur = trans[0];
  if (timeNs < cur.time_ns) {
    return { state: "x", label: "X" };
  }
  for (let i = 0; i < trans.length; i++) {
    if (trans[i].time_ns <= timeNs + 1e-9) {
      cur = trans[i];
    } else {
      break;
    }
  }
  return cur;
}

function renderWaveSignalSidebar() {
  const list = document.getElementById("wave-sig-list");
  const wave = studioState.waveform;
  if (!wave) {
    list.innerHTML = "";
    return;
  }

  list.innerHTML = wave.signals
    .map((sig) => {
      const valA = sampleSignalAtTime(sig, studioState.cursorANs).label;
      const valB = sampleSignalAtTime(sig, studioState.cursorBNs).label;
      return `
        <div class="wave-sig-row" title="${escapeHtml(sig.full_name)} (${sig.width}-bit)">
          <span class="sig-name-lbl">${escapeHtml(sig.name)}</span>
          <span class="sig-vals-pair">${escapeHtml(valA)} / ${escapeHtml(valB)}</span>
        </div>
      `;
    })
    .join("");
}

function chooseNiceTickStep(spanNs, pixelWidth) {
  const targetTicks = Math.max(4, Math.floor(pixelWidth / 95));
  const rawStep = spanNs / targetTicks;
  const mag = Math.pow(10, Math.floor(Math.log10(Math.max(1e-6, rawStep))));
  const norm = rawStep / mag;
  if (norm <= 1) return 1 * mag;
  if (norm <= 2) return 2 * mag;
  if (norm <= 5) return 5 * mag;
  return 10 * mag;
}

function drawCrossHatchX(ctx, x0, x1, topY, botY) {
  const w = Math.max(0, x1 - x0);
  if (w < 0.5) return;
  ctx.save();
  ctx.beginPath();
  ctx.rect(x0, topY, w, botY - topY);
  ctx.fillStyle = "rgba(255, 77, 109, 0.16)";
  ctx.fill();
  ctx.clip();

  ctx.strokeStyle = "rgba(255, 77, 109, 0.65)";
  ctx.lineWidth = 1;
  const h = botY - topY;
  for (let x = x0 - h; x < x1 + h; x += 7) {
    ctx.beginPath();
    ctx.moveTo(x, botY);
    ctx.lineTo(x + h, topY);
    ctx.stroke();
  }
  ctx.restore();

  ctx.strokeStyle = "#FF4D6D";
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  ctx.moveTo(x0, topY);
  ctx.lineTo(x1, topY);
  ctx.moveTo(x0, botY);
  ctx.lineTo(x1, botY);
  ctx.stroke();
}

function drawHexBusSegment(ctx, x0, x1, topY, botY, label) {
  const w = x1 - x0;
  if (w <= 0.5) return;
  const midY = (topY + botY) * 0.5;
  const bevel = Math.min(5.5, w * 0.28);

  ctx.beginPath();
  ctx.moveTo(x0, midY);
  ctx.lineTo(x0 + bevel, topY);
  ctx.lineTo(x1 - bevel, topY);
  ctx.lineTo(x1, midY);
  ctx.lineTo(x1 - bevel, botY);
  ctx.lineTo(x0 + bevel, botY);
  ctx.closePath();

  ctx.fillStyle = "rgba(56, 189, 248, 0.11)";
  ctx.fill();
  ctx.strokeStyle = "#38BDF8";
  ctx.lineWidth = 1.6;
  ctx.stroke();

  if (w > 28) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(x0 + bevel + 2, topY, Math.max(0, w - bevel * 2 - 4), botY - topY);
    ctx.clip();
    ctx.fillStyle = "#E6EDF3";
    ctx.font = "600 10.5px 'JetBrains Mono', Consolas, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, (x0 + x1) * 0.5, midY + 0.5);
    ctx.restore();
  }
}

function renderVcdCanvas() {
  const wrap = document.getElementById("wave-canvas-wrap");
  const canvas = document.getElementById("vcd-canvas");
  const wave = studioState.waveform;
  if (!wave) return;

  const dpr = window.devicePixelRatio || 1;
  const cssW = Math.max(320, wrap.clientWidth);
  const sigCount = wave.signals.length;
  const cssH = Math.max(wrap.clientHeight, WAVE_RULER_H + sigCount * WAVE_ROW_H);

  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  canvas.style.width = `${cssW}px`;
  canvas.style.height = `${cssH}px`;

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  // Background
  ctx.fillStyle = "#0D0F12";
  ctx.fillRect(0, 0, cssW, cssH);

  const startNs = studioState.viewStartNs;
  const endNs = Math.max(startNs + 0.05, studioState.viewEndNs);
  const spanNs = endNs - startNs;
  const timeToX = (tNs) => ((tNs - startNs) / spanNs) * cssW;

  // Time Ruler & Vertical Grid
  ctx.fillStyle = "#171B22";
  ctx.fillRect(0, 0, cssW, WAVE_RULER_H);
  ctx.strokeStyle = "#262B34";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, WAVE_RULER_H + 0.5);
  ctx.lineTo(cssW, WAVE_RULER_H + 0.5);
  ctx.stroke();

  const tickStep = chooseNiceTickStep(spanNs, cssW);
  const firstTick = Math.ceil(startNs / tickStep) * tickStep;
  ctx.font = "10px 'JetBrains Mono', Consolas, monospace";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";

  for (let t = firstTick; t <= endNs + tickStep * 0.01; t += tickStep) {
    const x = Math.round(timeToX(t)) + 0.5;
    // Grid line
    ctx.strokeStyle = "rgba(45, 53, 66, 0.45)";
    ctx.beginPath();
    ctx.moveTo(x, WAVE_RULER_H);
    ctx.lineTo(x, cssH);
    ctx.stroke();

    // Ruler tick
    ctx.strokeStyle = "#626E80";
    ctx.beginPath();
    ctx.moveTo(x, WAVE_RULER_H - 6);
    ctx.lineTo(x, WAVE_RULER_H);
    ctx.stroke();

    ctx.fillStyle = "#94A0B2";
    const prec = tickStep < 0.1 ? 2 : tickStep < 1 ? 1 : 0;
    ctx.fillText(`${t.toFixed(prec)}ns`, x + 4, 13);
  }

  // Draw Each Signal Row
  const maxSimNs = Number(wave.end_time_ns || endNs);

  wave.signals.forEach((sig, rowIdx) => {
    const rowTop = WAVE_RULER_H + rowIdx * WAVE_ROW_H;
    const rowBot = rowTop + WAVE_ROW_H;
    const yHigh = rowTop + 7;
    const yLow = rowBot - 7;
    const yMid = (yHigh + yLow) * 0.5;

    // Row separator
    ctx.strokeStyle = "rgba(38, 43, 52, 0.65)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, rowBot + 0.5);
    ctx.lineTo(cssW, rowBot + 0.5);
    ctx.stroke();

    const trans = sig.transitions || [];
    if (!trans.length) return;

    for (let i = 0; i < trans.length; i++) {
      const segStart = trans[i].time_ns;
      const segEnd = i + 1 < trans.length ? trans[i + 1].time_ns : Math.max(maxSimNs, endNs);
      if (segEnd < startNs || segStart > endNs) continue;

      const x0 = Math.max(-4, timeToX(segStart));
      const x1 = Math.min(cssW + 4, timeToX(segEnd));
      const st = trans[i].state;
      const lbl = trans[i].label;

      if (sig.width > 1 && st === "bus") {
        drawHexBusSegment(ctx, x0, x1, yHigh, yLow, lbl);
      } else if (st === "x") {
        drawCrossHatchX(ctx, x0, x1, yHigh, yLow);
      } else if (st === "z") {
        ctx.strokeStyle = "#FACC15";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x0, yMid);
        ctx.lineTo(x1, yMid);
        ctx.stroke();
      } else if (st === "1") {
        ctx.fillStyle = "rgba(0, 255, 136, 0.11)";
        ctx.fillRect(x0, yHigh, Math.max(0, x1 - x0), yLow - yHigh);
        ctx.strokeStyle = "#00FF88";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x0, yHigh);
        ctx.lineTo(x1, yHigh);
        ctx.stroke();
      } else {
        // Digital '0' low rail
        ctx.strokeStyle = "#3B82F6";
        ctx.lineWidth = 1.8;
        ctx.beginPath();
        ctx.moveTo(x0, yLow);
        ctx.lineTo(x1, yLow);
        ctx.stroke();
      }

      // Vertical rising/falling edge between consecutive 0/1 scalar transitions
      if (sig.width === 1 && i > 0 && segStart >= startNs && segStart <= endNs) {
        const prevSt = trans[i - 1].state;
        if ((prevSt === "0" && st === "1") || (prevSt === "1" && st === "0")) {
          ctx.strokeStyle = "#00FF88";
          ctx.lineWidth = 1.9;
          ctx.beginPath();
          ctx.moveTo(x0, yLow);
          ctx.lineTo(x0, yHigh);
          ctx.stroke();
        }
      }
    }
  });

  // Draw Shaded Δt Span Between Cursor A & Cursor B
  const xCurA = timeToX(studioState.cursorANs);
  const xCurB = timeToX(studioState.cursorBNs);
  const leftCur = Math.min(xCurA, xCurB);
  const rightCur = Math.max(xCurA, xCurB);
  if (rightCur > 0 && leftCur < cssW) {
    ctx.fillStyle = "rgba(0, 255, 136, 0.045)";
    ctx.fillRect(Math.max(0, leftCur), WAVE_RULER_H, Math.min(cssW, rightCur) - Math.max(0, leftCur), cssH - WAVE_RULER_H);
  }

  // Draw Cursor A (Amber) & Cursor B (Cyan)
  drawTimingCursor(ctx, xCurA, cssH, "#F59E0B", `A: ${studioState.cursorANs.toFixed(2)}ns`);
  drawTimingCursor(ctx, xCurB, cssH, "#38BDF8", `B: ${studioState.cursorBNs.toFixed(2)}ns`);
}

function drawTimingCursor(ctx, x, cssH, color, tagText) {
  if (x < -2 || x > ctx.canvas.clientWidth + 2) return;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  ctx.moveTo(x, 0);
  ctx.lineTo(x, cssH);
  ctx.stroke();
  ctx.setLineDash([]);

  // Header flag
  ctx.font = "700 10px 'JetBrains Mono', Consolas, monospace";
  const tw = ctx.measureText(tagText).width + 10;
  const fx = Math.min(ctx.canvas.clientWidth - tw - 2, Math.max(2, x + 3));
  ctx.fillStyle = color;
  ctx.fillRect(fx, 4, tw, 17);
  ctx.fillStyle = "#0B0D10";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(tagText, fx + 5, 12.5);
  ctx.restore();
}

function findNearestEdgeTime(targetNs, pixelTolerancePx, cssWidth) {
  const wave = studioState.waveform;
  if (!wave || !studioState.snapToEdge) return targetNs;
  const spanNs = studioState.viewEndNs - studioState.viewStartNs;
  const tolNs = (pixelTolerancePx / Math.max(1, cssWidth)) * spanNs;

  let bestTime = targetNs;
  let bestDist = tolNs;
  for (const sig of wave.signals) {
    for (const tr of sig.transitions || []) {
      const d = Math.abs(tr.time_ns - targetNs);
      if (d < bestDist) {
        bestDist = d;
        bestTime = tr.time_ns;
      }
    }
  }
  return bestTime;
}

function updateCursorReadout() {
  const a = studioState.cursorANs;
  const b = studioState.cursorBNs;
  const dt = b - a;
  const absDt = Math.abs(dt);

  document.getElementById("cur-a-readout").textContent = `${a.toFixed(2)} ns`;
  document.getElementById("cur-b-readout").textContent = `${b.toFixed(2)} ns`;
  document.getElementById("cur-dt-readout").textContent =
    `${dt >= 0 ? "+" : ""}${dt.toFixed(2)} ns (|Δt| = ${absDt.toFixed(2)} ns)`;
  document.getElementById("cur-freq-readout").textContent =
    absDt > 1e-6 ? `(${(1000.0 / absDt).toFixed(2)} MHz)` : "(0 Hz)";
}

function setupWaveformInteractions() {
  const wrap = document.getElementById("wave-canvas-wrap");
  const sigList = document.getElementById("wave-sig-list");

  // Sync vertical scroll between signal names and waveform canvas
  wrap.addEventListener("scroll", () => {
    sigList.scrollTop = wrap.scrollTop;
  });

  function placeCursorFromMouse(ev, targetCursor) {
    if (!studioState.waveform) return;
    const rect = wrap.getBoundingClientRect();
    const relX = Math.max(0, Math.min(rect.width, ev.clientX - rect.left));
    const spanNs = studioState.viewEndNs - studioState.viewStartNs;
    const rawNs = studioState.viewStartNs + (relX / Math.max(1, rect.width)) * spanNs;
    const snappedNs = Math.max(0, findNearestEdgeTime(rawNs, 8, rect.width));

    if (targetCursor === "B") {
      studioState.cursorBNs = snappedNs;
    } else {
      studioState.cursorANs = snappedNs;
    }
    updateCursorReadout();
    renderWaveSignalSidebar();
    renderVcdCanvas();
  }

  wrap.addEventListener("mousedown", (ev) => {
    if (ev.button === 1 || (ev.button === 0 && ev.altKey)) {
      studioState.wavePanning = true;
      studioState.wavePanLastX = ev.clientX;
      return;
    }
    if (ev.button === 2 || (ev.button === 0 && ev.shiftKey)) {
      ev.preventDefault();
      placeCursorFromMouse(ev, "B");
      return;
    }
    if (ev.button === 0) {
      placeCursorFromMouse(ev, studioState.activeCursorMode);
    }
  });

  wrap.addEventListener("contextmenu", (ev) => ev.preventDefault());

  window.addEventListener("mousemove", (ev) => {
    if (!studioState.wavePanning || !studioState.waveform) return;
    const dx = ev.clientX - studioState.wavePanLastX;
    studioState.wavePanLastX = ev.clientX;
    const spanNs = studioState.viewEndNs - studioState.viewStartNs;
    const dtNs = -(dx / Math.max(1, wrap.clientWidth)) * spanNs;
    const maxEnd = Math.max(spanNs, Number(studioState.waveform.end_time_ns || 100));
    let nextStart = Math.max(0, Math.min(maxEnd - spanNs, studioState.viewStartNs + dtNs));
    studioState.viewStartNs = nextStart;
    studioState.viewEndNs = nextStart + spanNs;
    renderVcdCanvas();
  });

  window.addEventListener("mouseup", () => {
    studioState.wavePanning = false;
  });

  // Mouse Wheel Zoom In/Out anchored at cursor timestamp
  wrap.addEventListener(
    "wheel",
    (ev) => {
      if (!studioState.waveform) return;
      ev.preventDefault();
      const rect = wrap.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (ev.clientX - rect.left) / Math.max(1, rect.width)));
      const span = studioState.viewEndNs - studioState.viewStartNs;
      const anchorNs = studioState.viewStartNs + ratio * span;

      const maxSim = Math.max(10, Number(studioState.waveform.end_time_ns || 100));
      const zoomFactor = ev.deltaY < 0 ? 0.8 : 1.25;
      const nextSpan = Math.max(2.0, Math.min(maxSim * 1.15, span * zoomFactor));

      let nextStart = anchorNs - ratio * nextSpan;
      if (nextStart < 0) nextStart = 0;
      studioState.viewStartNs = nextStart;
      studioState.viewEndNs = nextStart + nextSpan;
      renderVcdCanvas();
    },
    { passive: false }
  );

  document.getElementById("btn-wave-zin").addEventListener("click", () => {
    const mid = (studioState.viewStartNs + studioState.viewEndNs) * 0.5;
    const half = Math.max(1.0, (studioState.viewEndNs - studioState.viewStartNs) * 0.38);
    studioState.viewStartNs = Math.max(0, mid - half);
    studioState.viewEndNs = studioState.viewStartNs + half * 2;
    renderVcdCanvas();
  });

  document.getElementById("btn-wave-zout").addEventListener("click", () => {
    if (!studioState.waveform) return;
    const maxSim = Math.max(10, Number(studioState.waveform.end_time_ns || 100));
    const mid = (studioState.viewStartNs + studioState.viewEndNs) * 0.5;
    const half = Math.min(maxSim * 0.6, (studioState.viewEndNs - studioState.viewStartNs) * 0.65);
    studioState.viewStartNs = Math.max(0, mid - half);
    studioState.viewEndNs = studioState.viewStartNs + half * 2;
    renderVcdCanvas();
  });

  document.getElementById("btn-wave-fit").addEventListener("click", () => {
    if (!studioState.waveform) return;
    studioState.viewStartNs = 0;
    studioState.viewEndNs = Math.max(10, Number(studioState.waveform.end_time_ns || 100));
    renderVcdCanvas();
  });

  document.querySelectorAll("[data-cur-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-cur-mode]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      studioState.activeCursorMode = btn.dataset.curMode;
    });
  });

  document.getElementById("chk-snap-edge").addEventListener("change", (ev) => {
    studioState.snapToEdge = ev.target.checked;
  });

  window.addEventListener("resize", () => {
    renderVcdCanvas();
  });
}

// ============================================================================
// Utilities: File Upload (.vcd), Copy, Download (.v / _tb.v / .vcd), Tabs
// ============================================================================

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function downloadTextFile(filename, content) {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function copyToClipboard(text, btnEl) {
  try {
    await navigator.clipboard.writeText(text);
    const orig = btnEl.textContent;
    btnEl.textContent = "Copied!";
    setTimeout(() => {
      btnEl.textContent = orig;
    }, 1100);
  } catch (_e) {
    // Fallback if clipboard blocked
  }
}

function bindStudioControls() {
  document.getElementById("btn-synthesize").addEventListener("click", () => {
    runExpressionSynthesis(false);
  });

  document.getElementById("tech-select").addEventListener("change", () => {
    runExpressionSynthesis(false);
  });

  document.getElementById("preset-select").addEventListener("change", (ev) => {
    const p = PRESETS[ev.target.value];
    if (!p) return;
    document.getElementById("expr-input").value = p.expr;
    document.getElementById("dc-input").value = p.dc;
    document.getElementById("tech-select").value = p.tech;
    studioState.schemPanX = 0;
    studioState.schemPanY = 0;
    studioState.schemZoom = 1.0;
    runExpressionSynthesis(true);
  });

  // Truth table variable count change & Clear All 0
  document.getElementById("tt-var-count").addEventListener("change", (ev) => {
    const n = Number(ev.target.value);
    const nextVars = Array.from({ length: n }, (_, i) => String.fromCharCode(65 + i));
    const nextStates = new Array(1 << n).fill("0");
    for (let i = 0; i < Math.min(studioState.ttStates.length, nextStates.length); i++) {
      nextStates[i] = studioState.ttStates[i];
    }
    studioState.varNames = nextVars;
    studioState.ttStates = nextStates;
    runTruthTableSynthesis();
  });

  document.getElementById("btn-tt-zero").addEventListener("click", () => {
    studioState.ttStates = new Array(1 << studioState.varNames.length).fill("0");
    runTruthTableSynthesis();
  });

  // Center Pane Tabs
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.tab;
      document.querySelectorAll(".tab-btn").forEach((b) => {
        const active = b === btn;
        b.classList.toggle("active", active);
        b.setAttribute("aria-selected", String(active));
      });
      document.getElementById("tab-pane-schematic").hidden = target !== "schematic";
      document.getElementById("tab-pane-verilog").hidden = target !== "verilog";
      document.getElementById("tab-pane-implicants").hidden = target !== "implicants";
      document.getElementById("schem-zoom-controls").style.visibility =
        target === "schematic" ? "visible" : "hidden";
    });
  });

  // Verilog Copy & Download Buttons
  document.getElementById("btn-copy-v").addEventListener("click", (ev) => {
    if (studioState.lastSynth) {
      copyToClipboard(studioState.lastSynth.verilog_module, ev.currentTarget);
    }
  });
  document.getElementById("btn-copy-tb").addEventListener("click", (ev) => {
    if (studioState.lastSynth) {
      copyToClipboard(studioState.lastSynth.verilog_tb, ev.currentTarget);
    }
  });
  document.getElementById("btn-dl-v").addEventListener("click", () => {
    if (studioState.lastSynth) {
      downloadTextFile(`${studioState.moduleName}.v`, studioState.lastSynth.verilog_module);
    }
  });
  document.getElementById("btn-dl-tb").addEventListener("click", () => {
    if (studioState.lastSynth) {
      downloadTextFile(`${studioState.moduleName}_tb.v`, studioState.lastSynth.verilog_tb);
    }
  });

  // VCD Upload & Export
  document.getElementById("vcd-file-input").addEventListener("change", async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file || !studioState.core) return;
    const content = await file.text();
    const res = JSON.parse(studioState.core.parse_vcd_json(content));
    if (!res.ok) {
      showError(`VCD Parser Error: ${res.error}`);
      return;
    }
    showError("");
    studioState.vcdRawText = content;
    document.getElementById("vcd-source-badge").textContent =
      `Uploaded: ${file.name} (${res.waveform.timescale}, ${res.waveform.signal_count} sigs)`;
    loadWaveformData(res.waveform, true, 5.0);
    ev.target.value = "";
  });

  document.getElementById("btn-download-vcd").addEventListener("click", () => {
    if (studioState.vcdRawText) {
      downloadTextFile(`${studioState.moduleName}.vcd`, studioState.vcdRawText);
    }
  });
}

window.addEventListener("DOMContentLoaded", () => {
  renderTruthTable();
  setupSchematicPanZoom();
  setupWaveformInteractions();
  bindStudioControls();
  initPyodideEngine();
});
