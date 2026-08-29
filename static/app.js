/* Graphiti Lens - client.
   Canvas force-graph over a read-only Neo4j API. No build step, no CDN. */
(() => {
"use strict";

// --------------------------------------------------------------------- state
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const S = {
  mode: "explore",
  group: "",
  meta: null,
  nodes: [], edges: [], byId: new Map(),
  sel: null, selEdge: null, hover: null, hoverEdge: null,
  neighbours: new Set(),
  asofOn: false, asofISO: null, hideSuper: false,
  colorBy: "kind", groupColor: new Map(),
  sim: null, transform: d3.zoomIdentity, paused: false,
  span: [null, null],
  lastResults: [], listSel: null,
};

const C = {
  entity: "#3987e5", episodic: "#d95926", community: "#199e70",
  live: "#6f6e68", invalidated: "#fab219", expired: "#d03b3b",
  ink: "#ffffff", ink2: "#c3c2b7", ink3: "#8a8980", ink4: "#5f5e58",
  surface: "#121211", surface1: "#1a1a19",
  good: "#0ca30c", warning: "#fab219", critical: "#d03b3b",
};
// Validated categorical order (dark steps). 7 slots + neutral "other".
const GROUP_HUES = ["#3987e5","#d95926","#199e70","#c98500","#d55181","#008300","#9085e9"];
const OTHER_HUE = "#6f6e68";

const $ = (s) => document.querySelector(s);
const el = (t, c, txt) => { const n = document.createElement(t); if (c) n.className = c; if (txt != null) n.textContent = txt; return n; };
const esc = (s) => String(s == null ? "" : s);
const fmtN = (n) => (n == null ? "—" : Number(n).toLocaleString());
const shortDate = (iso) => iso ? String(iso).slice(0, 10) : "—";
const dayMs = 86400000;

function relDays(iso) {
  if (!iso) return "—";
  const d = Math.floor((Date.now() - Date.parse(iso)) / dayMs);
  if (!isFinite(d)) return "—";
  return d <= 0 ? "today" : d === 1 ? "1 day ago" : `${d} days ago`;
}

async function api(path, opts) {
  const sep = path.includes("?") ? "&" : "?";
  const url = path + (TOKEN ? `${sep}t=${encodeURIComponent(TOKEN)}` : "");
  const res = await fetch(url, Object.assign({ headers: TOKEN ? { "X-Lens-Token": TOKEN } : {} }, opts || {}));
  const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok || body.error) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

// ------------------------------------------------------------------ renderer
const canvas = $("#canvas");
const ctx = canvas.getContext("2d");
let W = 0, H = 0, DPR = 1;

function resize() {
  const r = canvas.parentElement.getBoundingClientRect();
  DPR = Math.min(2, window.devicePixelRatio || 1);
  W = Math.max(1, Math.floor(r.width)); H = Math.max(1, Math.floor(r.height));
  canvas.width = W * DPR; canvas.height = H * DPR;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  draw();
}

function nodeRadius(n) {
  const base = n.kind === "episodic" ? 4.5 : 4;
  return base + Math.min(10, Math.sqrt(n.degree || 0) * 1.7);
}

function nodeColor(n) {
  if (S.colorBy === "group") return S.groupColor.get(n.group) || OTHER_HUE;
  return C[n.kind] || C.entity;
}

function edgeVisible(e) {
  if (S.hideSuper && e.state !== "live") return false;
  return true;
}
function edgeAlpha(e) {
  if (S.asofOn) return e.live ? 0.85 : 0.1;
  return e.state === "live" ? 0.5 : 0.75;
}
function edgeStroke(e) { return C[e.state] || C.live; }
function edgeDash(e) {
  return e.state === "invalidated" ? [5, 4] : e.state === "expired" ? [1.5, 3.5] : null;
}

function setSim(reheat) {
  if (S.sim) S.sim.stop();
  const n = S.nodes.length;
  const linkDist = n > 900 ? 26 : n > 300 ? 38 : 56;
  const charge = n > 900 ? -90 : n > 300 ? -150 : -230;
  S.sim = d3.forceSimulation(S.nodes)
    .force("link", d3.forceLink(S.edges.filter(edgeVisible)).id((d) => d.id).distance(linkDist).strength(0.32))
    .force("charge", d3.forceManyBody().strength(charge).distanceMax(520))
    .force("collide", d3.forceCollide().radius((d) => nodeRadius(d) + 3).iterations(1))
    .force("x", d3.forceX(0).strength(0.035))
    .force("y", d3.forceY(0).strength(0.035))
    .alpha(reheat ? 0.9 : 0.4)
    .alphaDecay(0.022)
    .on("tick", draw);
  if (S.paused) S.sim.stop();
}

function draw() {
  ctx.save();
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = C.surface; ctx.fillRect(0, 0, W, H);
  const t = S.transform;
  ctx.translate(t.x, t.y); ctx.scale(t.k, t.k);

  // edges
  const focusId = S.sel ? S.sel.id : (S.hover ? S.hover.id : null);
  for (const e of S.edges) {
    if (!edgeVisible(e)) continue;
    const a = e.source, b = e.target;
    if (!a || !b || a.x == null || b.x == null) continue;
    const touched = focusId && (a.id === focusId || b.id === focusId);
    const isHover = S.hoverEdge === e || S.selEdge === e;
    let alpha = edgeAlpha(e);
    if (focusId && !touched) alpha *= 0.22;
    if (isHover) alpha = 1;
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = isHover ? "#ffffff" : edgeStroke(e);
    ctx.lineWidth = (isHover ? 2.4 : touched ? 1.7 : 1) / t.k;
    const dash = edgeDash(e);
    ctx.setLineDash(dash ? dash.map((d) => d / t.k) : []);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
  }
  ctx.setLineDash([]);

  // nodes
  for (const n of S.nodes) {
    if (n.x == null) continue;
    const r = nodeRadius(n);
    const dim = focusId && n.id !== focusId && !S.neighbours.has(n.id);
    ctx.globalAlpha = dim ? 0.28 : 1;
    ctx.fillStyle = nodeColor(n);
    ctx.beginPath();
    if (n.kind === "episodic") ctx.rect(n.x - r, n.y - r, r * 2, r * 2);
    else ctx.arc(n.x, n.y, r, 0, 6.2832);
    ctx.fill();
    if (S.sel && n.id === S.sel.id) {
      ctx.globalAlpha = 1; ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2.2 / t.k;
      ctx.beginPath(); ctx.arc(n.x, n.y, r + 3.5 / t.k, 0, 6.2832); ctx.stroke();
    }
  }

  // labels
  const showAll = t.k > 1.15;
  const cands = S.nodes.filter((n) => n.x != null && (showAll || (n.degree || 0) >= 4 || (S.sel && (n.id === S.sel.id || S.neighbours.has(n.id)))));
  cands.sort((a, b) => (b.degree || 0) - (a.degree || 0));
  ctx.globalAlpha = 1;
  ctx.font = `${Math.max(9, 11 / t.k)}px -apple-system, system-ui, sans-serif`;
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (const n of cands.slice(0, 220)) {
    const dim = focusId && n.id !== focusId && !S.neighbours.has(n.id);
    ctx.globalAlpha = dim ? 0.22 : 0.95;
    const label = n.name.length > 34 ? n.name.slice(0, 33) + "…" : n.name;
    const y = n.y + nodeRadius(n) + 3 / t.k;
    ctx.lineWidth = 3 / t.k; ctx.strokeStyle = "rgba(18,18,17,.92)";
    ctx.strokeText(label, n.x, y);
    ctx.fillStyle = (S.sel && n.id === S.sel.id) ? C.ink : C.ink2;
    ctx.fillText(label, n.x, y);
  }
  ctx.restore();
  updateStats();
}

function updateStats() {
  const vis = S.edges.filter(edgeVisible);
  const byState = { live: 0, invalidated: 0, expired: 0 };
  for (const e of vis) byState[e.state] = (byState[e.state] || 0) + 1;
  const liveAsOf = S.asofOn ? vis.filter((e) => e.live).length : null;
  $("#stats").textContent = S.nodes.length
    ? `${fmtN(S.nodes.length)} nodes · ${fmtN(vis.length)} facts` +
      (S.asofOn ? ` · ${fmtN(liveAsOf)} live on ${shortDate(S.asofISO)}` : "") +
      (byState.invalidated || byState.expired ? ` · ${fmtN(byState.invalidated + byState.expired)} superseded` : "")
    : "—";
  $("#empty").style.display = S.nodes.length ? "none" : "grid";
}


// ------------------------------------------------------------ graph loading
function setGraph(payload, opts) {
  const keepPos = (opts && opts.merge) ? S.byId : null;
  const nodes = payload.nodes.map((n) => {
    const prev = keepPos && keepPos.get(n.id);
    const m = Object.assign({}, n);
    if (prev) { m.x = prev.x; m.y = prev.y; m.vx = prev.vx; m.vy = prev.vy; }
    else { const a = Math.random() * 6.2832, d = 30 + Math.random() * 220; m.x = Math.cos(a) * d; m.y = Math.sin(a) * d; }
    return m;
  });
  S.byId = new Map(nodes.map((n) => [n.id, n]));
  S.nodes = nodes;
  // Pre-resolve endpoints so hidden edges keep their objects when unhidden.
  S.edges = payload.edges
    .map((e) => Object.assign({}, e, { source: S.byId.get(e.source), target: S.byId.get(e.target) }))
    .filter((e) => e.source && e.target);
  S.sel = payload.focus ? S.byId.get(payload.focus) || null : null;
  S.selEdge = null; S.hover = null; S.hoverEdge = null;
  computeNeighbours();
  assignGroupColors();
  setSim(true);
  if (!(opts && opts.merge)) setTimeout(fit, 420);
  renderInspector();
  renderLegend();
  draw();
}

function mergeGraph(payload) {
  const nodes = new Map(S.nodes.map((n) => [n.id, n]));
  for (const n of payload.nodes) if (!nodes.has(n.id)) nodes.set(n.id, n);
  const edges = new Map(S.edges.map((e) => [e.id, e]));
  for (const e of payload.edges) if (!edges.has(e.id)) edges.set(e.id, e);
  setGraph({
    nodes: [...nodes.values()].map((n) => ({ ...n })),
    edges: [...edges.values()].map((e) => ({
      ...e,
      source: typeof e.source === "object" ? e.source.id : e.source,
      target: typeof e.target === "object" ? e.target.id : e.target,
    })),
    focus: S.sel ? S.sel.id : null,
  }, { merge: true });
}

function computeNeighbours() {
  S.neighbours = new Set();
  if (!S.sel) return;
  for (const e of S.edges) {
    if (!edgeVisible(e)) continue;
    const a = e.source.id || e.source, b = e.target.id || e.target;
    if (a === S.sel.id) S.neighbours.add(b);
    if (b === S.sel.id) S.neighbours.add(a);
  }
}

function assignGroupColors() {
  const counts = new Map();
  for (const n of S.nodes) counts.set(n.group, (counts.get(n.group) || 0) + 1);
  const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1]).map((x) => x[0]);
  S.groupColor = new Map();
  ranked.slice(0, GROUP_HUES.length).forEach((g, i) => S.groupColor.set(g, GROUP_HUES[i]));
  S.colorBy = (S.mode === "bridges" && counts.size > 1) ? "group" : "kind";
}

function fit() {
  if (!S.nodes.length) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of S.nodes) {
    if (n.x == null) continue;
    x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x);
    y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y);
  }
  if (!isFinite(x0)) return;
  const pad = 60;
  const k = Math.max(0.05, Math.min(3, Math.min((W - pad * 2) / Math.max(1, x1 - x0), (H - pad * 2) / Math.max(1, y1 - y0))));
  const t = d3.zoomIdentity.translate(W / 2 - ((x0 + x1) / 2) * k, H / 2 - ((y0 + y1) / 2) * k).scale(k);
  d3.select(canvas).transition().duration(380).call(zoom.transform, t);
}

// --------------------------------------------------------------- interaction
function nodeAt(px, py) {
  const t = S.transform, x = t.invertX(px), y = t.invertY(py);
  let best = null, bestD = Infinity;
  for (const n of S.nodes) {
    if (n.x == null) continue;
    const r = nodeRadius(n) + 5 / t.k, dx = n.x - x, dy = n.y - y, d = dx * dx + dy * dy;
    if (d < r * r && d < bestD) { best = n; bestD = d; }
  }
  return best;
}

function edgeAt(px, py) {
  if (S.edges.length > 6000) return null;
  const t = S.transform, x = t.invertX(px), y = t.invertY(py), tol = 6 / t.k;
  for (const e of S.edges) {
    if (!edgeVisible(e) || !e.source || e.source.x == null) continue;
    const ax = e.source.x, ay = e.source.y, bx = e.target.x, by = e.target.y;
    const dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy || 1;
    let u = ((x - ax) * dx + (y - ay) * dy) / len2;
    u = Math.max(0, Math.min(1, u));
    const cx = ax + u * dx, cy = ay + u * dy;
    if ((cx - x) ** 2 + (cy - y) ** 2 < tol * tol) return e;
  }
  return null;
}

const zoom = d3.zoom().scaleExtent([0.04, 8])
  .filter((e) => e.type === "wheel")
  .on("zoom", (e) => { S.transform = e.transform; draw(); });

d3.select(canvas).call(zoom);

d3.select(canvas).call(d3.drag()
  .subject((e) => {
    const n = nodeAt(e.x, e.y);
    if (n) return { node: n, x: S.transform.applyX(n.x), y: S.transform.applyY(n.y) };
    return { pan: true, x: e.x, y: e.y };
  })
  .on("start", (e) => {
    canvas.classList.add("grabbing");
    if (e.subject.node && S.sim && !S.paused) S.sim.alphaTarget(0.25).restart();
  })
  .on("drag", (e) => {
    if (e.subject.node) {
      e.subject.node.fx = S.transform.invertX(e.x);
      e.subject.node.fy = S.transform.invertY(e.y);
    } else {
      zoom.translateBy(d3.select(canvas), e.dx / S.transform.k, e.dy / S.transform.k);
    }
    draw();
  })
  .on("end", (e) => {
    canvas.classList.remove("grabbing");
    if (e.subject.node) {
      if (S.sim) S.sim.alphaTarget(0);
      e.subject.node.fx = null; e.subject.node.fy = null;
    }
  }));

canvas.addEventListener("mousemove", (ev) => {
  const r = canvas.getBoundingClientRect();
  const px = ev.clientX - r.left, py = ev.clientY - r.top;
  const n = nodeAt(px, py);
  const e = n ? null : edgeAt(px, py);
  if (n !== S.hover || e !== S.hoverEdge) {
    S.hover = n; S.hoverEdge = e;
    showTooltip(n, e, px, py);
    draw();
  } else if (n || e) {
    positionTooltip(px, py);
  }
});
canvas.addEventListener("mouseleave", () => {
  S.hover = null; S.hoverEdge = null; $("#tooltip").style.display = "none"; draw();
});

canvas.addEventListener("click", (ev) => {
  const r = canvas.getBoundingClientRect();
  const px = ev.clientX - r.left, py = ev.clientY - r.top;
  const n = nodeAt(px, py);
  if (n) { selectNode(n); return; }
  const e = edgeAt(px, py);
  if (e) { S.sel = null; S.selEdge = e; computeNeighbours(); renderInspector(); draw(); }
});

canvas.addEventListener("dblclick", async (ev) => {
  const r = canvas.getBoundingClientRect();
  const n = nodeAt(ev.clientX - r.left, ev.clientY - r.top);
  if (!n || n.kind !== "entity") return;
  try {
    const sub = await api(`/api/ego?uuid=${encodeURIComponent(n.id)}&depth=1&limit=250${asofParam()}`);
    mergeGraph(sub);
    selectNode(S.byId.get(n.id) || n);
  } catch (err) { console.warn(err); }
});

function showTooltip(n, e, px, py) {
  const tip = $("#tooltip");
  if (!n && !e) { tip.style.display = "none"; return; }
  tip.innerHTML = "";
  if (n) {
    tip.appendChild(el("div", "tt", n.name));
    if (n.summary) tip.appendChild(el("div", "", n.summary.slice(0, 190) + (n.summary.length > 190 ? "…" : "")));
    tip.appendChild(el("div", "tm", `${n.kind} · ${n.group} · ${n.degree || 0} facts`));
  } else {
    tip.appendChild(el("div", "tt", e.fact || e.name));
    const bits = [e.state.toUpperCase()];
    if (e.valid_at) bits.push("from " + shortDate(e.valid_at));
    if (e.invalid_at) bits.push("superseded " + shortDate(e.invalid_at));
    if (e.expired_at) bits.push("retracted " + shortDate(e.expired_at));
    tip.appendChild(el("div", "tm", bits.join(" · ")));
  }
  tip.style.display = "block";
  positionTooltip(px, py);
}
function positionTooltip(px, py) {
  const tip = $("#tooltip");
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.min(W - w - 10, px + 16) + "px";
  tip.style.top = Math.max(8, Math.min(H - h - 10, py + 16)) + "px";
}

async function selectNode(n) {
  S.sel = n; S.selEdge = null;
  computeNeighbours(); draw();
  renderInspector({ loading: true });
  try {
    const detail = await api(`/api/node?uuid=${encodeURIComponent(n.id)}`);
    if (S.sel && S.sel.id === n.id) renderInspector({ detail });
  } catch (err) { renderInspector({ error: err.message }); }
}


// -------------------------------------------------------------------- legend
function dashSwatch(color, dash) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("width", "16"); svg.setAttribute("height", "13"); svg.setAttribute("aria-hidden", "true");
  const ln = document.createElementNS(ns, "line");
  ln.setAttribute("x1", "0"); ln.setAttribute("y1", "6.5"); ln.setAttribute("x2", "16"); ln.setAttribute("y2", "6.5");
  ln.setAttribute("stroke", color); ln.setAttribute("stroke-width", "2");
  if (dash) ln.setAttribute("stroke-dasharray", dash);
  svg.appendChild(ln);
  return svg;
}
function legendRow(swatch, label) {
  const d = el("div", "li"); d.appendChild(swatch); d.appendChild(el("span", "", label)); return d;
}
function colorDot(color, square) {
  const s = el("span", "sw" + (square ? " sq" : "")); s.style.background = color; return s;
}

function renderLegend() {
  const box = $("#legend"); box.innerHTML = "";
  const counts = { entity: 0, episodic: 0, community: 0 };
  for (const n of S.nodes) counts[n.kind] = (counts[n.kind] || 0) + 1;

  box.appendChild(el("h4", "", S.colorBy === "group" ? "Project" : "Node"));
  if (S.colorBy === "group") {
    const shown = [...S.groupColor.entries()];
    for (const [g, c] of shown) box.appendChild(legendRow(colorDot(c), g));
    const others = new Set(S.nodes.filter((n) => !S.groupColor.has(n.group)).map((n) => n.group));
    if (others.size) box.appendChild(legendRow(colorDot(OTHER_HUE), `other (${others.size} projects)`));
  } else {
    if (counts.entity) box.appendChild(legendRow(colorDot(C.entity), `entity (${fmtN(counts.entity)})`));
    if (counts.episodic) box.appendChild(legendRow(colorDot(C.episodic, true), `episode (${fmtN(counts.episodic)})`));
    if (counts.community) box.appendChild(legendRow(colorDot(C.community), `community (${fmtN(counts.community)})`));
  }
  const st = { live: 0, invalidated: 0, expired: 0 };
  for (const e of S.edges) if (edgeVisible(e)) st[e.state]++;
  box.appendChild(el("h4", "", "Fact"));
  box.appendChild(legendRow(dashSwatch(C.live, null), `live (${fmtN(st.live)})`));
  if (st.invalidated) box.appendChild(legendRow(dashSwatch(C.invalidated, "5 4"), `superseded (${fmtN(st.invalidated)})`));
  if (st.expired) box.appendChild(legendRow(dashSwatch(C.expired, "1.5 3"), `retracted (${fmtN(st.expired)})`));
}

// ----------------------------------------------------------------- inspector
function renderInspector(opt) {
  const box = $("#rightBody"); box.innerHTML = "";
  $("#rightCount").textContent = "";
  const o = opt || {};

  if (S.selEdge) return renderEdgeInspector(box, S.selEdge);
  if (!S.sel) {
    box.innerHTML = '<div class="hint">Click a node or a fact to inspect it.<br><br><kbd>double-click</kbd> a node to expand its neighbours.<br><kbd>/</kbd> search &nbsp; <kbd>F</kbd> fit &nbsp; <kbd>Space</kbd> pause &nbsp; <kbd>Esc</kbd> clear</div>';
    return;
  }
  const n = o.detail || S.sel;
  box.appendChild(el("h3", "", n.name));
  const sub = el("div", "sub"); sub.textContent = n.id; box.appendChild(sub);

  const meta = el("section");
  const dl = el("dl", "kv");
  const put = (k, v) => { dl.appendChild(el("dt", "", k)); dl.appendChild(el("dd", "", v)); };
  put("project", n.group || "—");
  put("kind", n.kind);
  put("facts", String(n.degree != null ? n.degree : (n.facts ? n.facts.length : "—")));
  put("created", shortDate(n.created_at));
  meta.appendChild(dl);
  if (n.types && n.types.length) {
    const c = el("div", ""); c.style.marginTop = "8px";
    n.types.filter((t) => t !== "Entity").forEach((t) => c.appendChild(el("span", "chip", t)));
    if (c.childNodes.length) meta.appendChild(c);
  }
  box.appendChild(meta);

  if (n.summary) {
    const s = el("section"); s.appendChild(el("h5", "", "Summary"));
    s.appendChild(el("div", "prose", n.summary)); box.appendChild(s);
  }
  if (o.loading) { box.appendChild(el("div", "loading", "loading facts…")); return; }
  if (o.error) { box.appendChild(el("div", "loading", "error: " + o.error)); return; }

  if (n.facts && n.facts.length) {
    const s = el("section");
    s.appendChild(el("h5", "", `Facts (${n.facts.length})`));
    const sorted = n.facts.slice().sort((a, b) => (a.state === "live" ? -1 : 1) - (b.state === "live" ? -1 : 1));
    for (const f of sorted.slice(0, 120)) {
      const d = el("div", "fact " + f.state);
      d.appendChild(el("div", "f", (f.outgoing ? "→ " : "← ") + (f.fact || f.name)));
      const m = el("div", "m");
      const st = el("span", "state " + f.state, f.state);
      m.appendChild(st);
      m.appendChild(el("span", "", f.other || ""));
      if (f.valid_at) m.appendChild(el("span", "", "from " + shortDate(f.valid_at)));
      if (f.invalid_at) m.appendChild(el("span", "", "→ " + shortDate(f.invalid_at)));
      d.appendChild(m);
      d.onclick = () => { const t = S.byId.get(f.other_id); if (t) selectNode(t); };
      s.appendChild(d);
    }
    box.appendChild(s);
  }
  if (n.episodes && n.episodes.length) {
    const s = el("section");
    s.appendChild(el("h5", "", `Source episodes (${n.episodes.length})`));
    for (const e of n.episodes.slice(0, 20)) {
      const b = el("button", "row");
      b.appendChild(el("div", "t", e.name || "(episode)"));
      b.appendChild(el("div", "s", `${shortDate(e.created_at)} · ${e.source_description || "no source_description"}`));
      b.onclick = () => openEpisode(e.uuid);
      s.appendChild(b);
    }
    box.appendChild(s);
  }
}

function renderEdgeInspector(box, e) {
  box.appendChild(el("h3", "", e.fact || e.name));
  const sub = el("div", "sub"); sub.textContent = e.id; box.appendChild(sub);
  const s = el("section");
  const dl = el("dl", "kv");
  const put = (k, v) => { dl.appendChild(el("dt", "", k)); dl.appendChild(el("dd", "", v)); };
  put("state", e.state);
  put("relation", e.name);
  put("project", e.group || "—");
  put("valid_at", shortDate(e.valid_at));
  put("invalid_at", shortDate(e.invalid_at));
  put("expired_at", shortDate(e.expired_at));
  put("created_at", shortDate(e.created_at));
  s.appendChild(dl);
  box.appendChild(s);
  if (e.invalid_at && e.expired_at) {
    const w = el("section");
    w.appendChild(el("h5", "", "Bitemporal note"));
    w.appendChild(el("div", "prose",
      "Both timestamps are set, which is correct for a superseded fact. invalid_at is valid time — " +
      "when this stopped being true in the world. expired_at is transaction time — when the record " +
      "stopped being the current one. They are different axes and rarely share a value."));
    box.appendChild(w);
  }
  const nav = el("section");
  nav.appendChild(el("h5", "", "Endpoints"));
  for (const side of ["source", "target"]) {
    const node = typeof e[side] === "object" ? e[side] : S.byId.get(e[side]);
    if (!node) continue;
    const b = el("button", "row");
    b.appendChild(el("div", "t", node.name));
    b.appendChild(el("div", "s", side));
    b.onclick = () => selectNode(node);
    nav.appendChild(b);
  }
  box.appendChild(nav);
  if (e.episodes && e.episodes.length) {
    const s2 = el("section");
    s2.appendChild(el("h5", "", `Derived from ${e.episodes.length} episode(s)`));
    for (const id of e.episodes.slice(0, 10)) {
      const b = el("button", "row");
      b.appendChild(el("div", "t", id.slice(0, 8) + "…"));
      b.appendChild(el("div", "s", "open episode trace"));
      b.onclick = () => openEpisode(id);
      s2.appendChild(b);
    }
    box.appendChild(s2);
  }
}


// --------------------------------------------------------------------- modes
function asofParam() { return S.asofOn && S.asofISO ? `&asof=${encodeURIComponent(S.asofISO)}` : ""; }
function groupParam() { return S.group ? `&group=${encodeURIComponent(S.group)}` : ""; }
function setLeft(title, count) { $("#leftTitle").textContent = title; $("#leftCount").textContent = count || ""; }
function leftBody() { const b = $("#leftBody"); b.innerHTML = ""; return b; }
function leftLoading(msg) { const b = leftBody(); b.appendChild(el("div", "loading", msg || "loading…")); }
function leftError(e) { const b = leftBody(); const d = el("div", "loading", "error: " + e); d.style.color = C.critical; b.appendChild(d); }

async function setMode(mode) {
  S.mode = mode;
  document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
  const body = $("#body");
  body.classList.toggle("audit", mode === "audit");
  body.classList.toggle("wide-left", mode === "cypher");
  $("#audit").hidden = mode !== "audit";
  $("#searchField").style.display = (mode === "explore") ? "" : "none";
  if (mode === "audit") return renderAudit();
  if (mode === "bridges") return loadBridges();
  if (mode === "episodes") return loadEpisodes();
  if (mode === "cypher") return renderCypher();
  return renderExploreLeft();
}

// ---- explore
let searchTimer = null;
function renderExploreLeft() {
  setLeft("Results", "");
  const b = leftBody();
  if (!S.lastResults.length) {
    b.innerHTML = '<div class="hint">Type to search. Pick a result to open its neighbourhood.<br><br>Or hit <b>Load project</b> to render a whole group at once.</div>';
    const load = el("button", "btn"); load.textContent = "Load project graph";
    load.style.margin = "0 13px"; load.onclick = loadGroupGraph;
    b.appendChild(load);
    return;
  }
  renderResults(S.lastResults);
}

function renderResults(results) {
  setLeft("Results", String(results.length));
  const b = leftBody();
  if (!results.length) { b.innerHTML = '<div class="hint">No matches.</div>'; return; }
  for (const r of results) {
    const btn = el("button", "row");
    btn.appendChild(el("div", "t", r.label || r.name));
    const s = el("div", "s wrap", (r.summary || r.fact || "").slice(0, 150));
    btn.appendChild(s);
    const chips = el("div", "");
    if (r.group) chips.appendChild(el("span", "chip grp", r.group));
    if (r.state && r.state !== "live") chips.appendChild(el("span", "chip", r.state));
    btn.appendChild(chips);
    btn.onclick = async () => {
      document.querySelectorAll("#leftBody .row").forEach((x) => x.removeAttribute("aria-current"));
      btn.setAttribute("aria-current", "true");
      if (r.kind === "episodic") return openEpisode(r.id);
      const anchor = r.kind ? r.id : r.source;
      try {
        const sub = await api(`/api/ego?uuid=${encodeURIComponent(anchor)}&depth=1&limit=400${asofParam()}`);
        setGraph(sub);
        const n = S.byId.get(anchor); if (n) selectNode(n);
      } catch (e) { leftError(e.message); }
    };
    b.appendChild(btn);
  }
}

async function runSearch() {
  const q = $("#search").value.trim();
  if (q.length < 2) { S.lastResults = []; return renderExploreLeft(); }
  setLeft("Results", "…");
  try {
    const kind = $("#kind").value;
    const r = await api(`/api/search?q=${encodeURIComponent(q)}&kind=${kind}&limit=60${groupParam()}`);
    S.lastResults = r.results;
    renderResults(r.results);
  } catch (e) { leftError(e.message); }
}

async function loadGroupGraph() {
  leftLoading("loading project graph…");
  try {
    const sub = await api(`/api/group?limit=1200&order=degree${groupParam()}${asofParam()}`);
    setGraph(sub);
    setLeft("Results", `${sub.nodes.length} nodes`);
    const b = leftBody();
    b.innerHTML = `<div class="hint">Showing the ${sub.nodes.length} best-connected entities${S.group ? " in <b>" + S.group + "</b>" : ""}.${sub.truncated ? "<br><br>Truncated at the node budget — narrow by project or search instead." : ""}</div>`;
  } catch (e) { leftError(e.message); }
}

// ---- bridges
async function loadBridges() {
  setLeft("Bridge entities", "…");
  leftLoading("scanning for names shared across projects…");
  try {
    const r = await api("/api/bridges?min_groups=2&limit=200");
    setLeft("Bridge entities", String(r.bridges.length));
    const b = leftBody();
    const note = el("div", "hint");
    note.innerHTML = "Entities whose name appears in more than one project. Graphiti dedupes inside a <code>group_id</code>, not across them — so these are where your portfolio actually overlaps.";
    b.appendChild(note);
    for (const br of r.bridges) {
      const btn = el("button", "row");
      btn.appendChild(el("div", "t", br.display));
      btn.appendChild(el("div", "s", `${br.group_count} projects · ${br.node_count} copies`));
      const chips = el("div", "");
      br.groups.slice(0, 6).forEach((g) => chips.appendChild(el("span", "chip grp", g)));
      if (br.groups.length > 6) chips.appendChild(el("span", "chip", `+${br.groups.length - 6}`));
      btn.appendChild(chips);
      btn.onclick = async () => {
        document.querySelectorAll("#leftBody .row").forEach((x) => x.removeAttribute("aria-current"));
        btn.setAttribute("aria-current", "true");
        try {
          const sub = await api(`/api/bridge_graph?key=${encodeURIComponent(br.key)}&hops=1&limit=500${asofParam()}`);
          setGraph(sub);
        } catch (e) { leftError(e.message); }
      };
      b.appendChild(btn);
    }
  } catch (e) { leftError(e.message); }
}

// ---- episodes
async function loadEpisodes() {
  setLeft("Episodes", "…");
  leftLoading("loading episode timeline…");
  try {
    const r = await api(`/api/episodes?limit=200${groupParam()}`);
    setLeft("Episodes", String(r.episodes.length));
    const b = leftBody();
    for (const e of r.episodes) {
      const btn = el("button", "row");
      btn.appendChild(el("div", "t", e.name || "(episode)"));
      btn.appendChild(el("div", "s", `${shortDate(e.created_at)} · ${e.mentions} entities · ${e.edge_count} facts`));
      const chips = el("div", "");
      chips.appendChild(el("span", "chip grp", e.group));
      const tag = el("span", "chip", e.tagged ? "provenance ✓" : "untagged");
      if (!e.tagged) tag.style.color = C.warning;
      chips.appendChild(tag);
      if (!e.source_description) { const c = el("span", "chip", "no source"); c.style.color = C.critical; chips.appendChild(c); }
      btn.appendChild(chips);
      btn.onclick = () => {
        document.querySelectorAll("#leftBody .row").forEach((x) => x.removeAttribute("aria-current"));
        btn.setAttribute("aria-current", "true");
        openEpisode(e.uuid);
      };
      b.appendChild(btn);
    }
  } catch (e) { leftError(e.message); }
}

async function openEpisode(uuid) {
  try {
    const ep = await api(`/api/episode?uuid=${encodeURIComponent(uuid)}`);
    setGraph(ep.graph || { nodes: [], edges: [] });
    const box = $("#rightBody"); box.innerHTML = "";
    box.appendChild(el("h3", "", ep.name || "(episode)"));
    const sub = el("div", "sub"); sub.textContent = ep.uuid; box.appendChild(sub);
    const s = el("section");
    const dl = el("dl", "kv");
    const put = (k, v) => { dl.appendChild(el("dt", "", k)); dl.appendChild(el("dd", "", v)); };
    put("project", ep.group_id || "—");
    put("created", shortDate(ep.created_at));
    put("valid_at", shortDate(ep.valid_at));
    put("source", ep.source || "—");
    put("entities", String(ep.extracted_count));
    put("facts", String((ep.derived_edges || []).length));
    s.appendChild(dl);
    box.appendChild(s);
    const p = el("section");
    p.appendChild(el("h5", "", "Provenance"));
    const badge = el("div", "badge " + (ep.provenance_ok ? "good" : "warning"));
    badge.appendChild(colorDot(ep.provenance_ok ? C.good : C.warning));
    badge.appendChild(el("span", "", ep.provenance_ok ? "tagged OBSERVED/DERIVED" : "content is not tagged"));
    p.appendChild(badge);
    p.appendChild(el("div", "prose", ep.source_description || "(no source_description)"));
    box.appendChild(p);
    const c = el("section");
    c.appendChild(el("h5", "", "Content"));
    c.appendChild(el("div", "prose", (ep.content || "").slice(0, 4000)));
    box.appendChild(c);
    if (ep.derived_edges && ep.derived_edges.length) {
      const f = el("section");
      f.appendChild(el("h5", "", `Facts written by this episode (${ep.derived_edges.length})`));
      for (const e of ep.derived_edges.slice(0, 60)) {
        const d = el("div", "fact " + e.state);
        d.appendChild(el("div", "f", e.fact || e.name));
        d.appendChild(el("div", "m", `${e.state} · ${shortDate(e.valid_at)}`));
        f.appendChild(d);
      }
      box.appendChild(f);
    }
  } catch (e) { leftError(e.message); }
}


// ---- cypher console
const SNIPPETS = [
  ["Facts live on a given date", `// everything true on 2026-06-01, ignoring what was later superseded
MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
WHERE coalesce(r.valid_at, r.created_at) <= datetime('2026-06-01T00:00:00Z')
  AND (r.invalid_at IS NULL OR r.invalid_at > datetime('2026-06-01T00:00:00Z'))
RETURN a, r, b LIMIT 200`],
  ["What replaced a superseded fact", `MATCH (a:Entity)-[old:RELATES_TO]->(b:Entity)
WHERE old.invalid_at IS NOT NULL
MATCH (a)-[new:RELATES_TO]->(b)
WHERE new.invalid_at IS NULL AND new.uuid <> old.uuid
RETURN a.name AS entity, b.name AS other,
       old.fact AS was, old.invalid_at AS until, new.fact AS now
LIMIT 100`],
  ["Same entity name across projects", `MATCH (n:Entity)
WITH toLower(trim(n.name)) AS k, collect(DISTINCT n.group_id) AS projects, collect(n.name)[0] AS name
WHERE size(projects) > 2
RETURN name, size(projects) AS projects_count, projects
ORDER BY projects_count DESC LIMIT 50`],
  ["Episodes missing the provenance tag", `MATCH (e:Episodic)
WHERE NOT (coalesce(e.content,'') STARTS WITH 'OBSERVED:'
        OR coalesce(e.content,'') STARTS WITH 'DERIVED:')
RETURN e.group_id AS project, count(*) AS untagged, max(e.created_at) AS latest
ORDER BY untagged DESC`],
  ["Edges stamping invalid_at AND expired_at", `MATCH ()-[r:RELATES_TO]->()
WHERE r.invalid_at IS NOT NULL AND r.expired_at IS NOT NULL
RETURN r.group_id AS project, count(r) AS both_stamped
ORDER BY both_stamped DESC`],
  ["Orphan entities (extracted, never linked)", `MATCH (n:Entity) WHERE NOT (n)-[:RELATES_TO]-()
RETURN n.group_id AS project, count(*) AS orphans
ORDER BY orphans DESC`],
  ["Busiest entities in a project", `MATCH (n:Entity {group_id:'buylandfl'})-[r:RELATES_TO]-()
RETURN n.name AS entity, count(r) AS facts
ORDER BY facts DESC LIMIT 30`],
];

function renderCypher() {
  setLeft("Cypher", "read-only");
  const b = leftBody();
  const ta = el("textarea"); ta.id = "cyq"; ta.spellcheck = false;
  ta.value = SNIPPETS[0][1];
  b.appendChild(ta);
  const row = el("div", "cyrow");
  const run = el("button", "btn on"); run.textContent = "Run  ⌘⏎";
  const clear = el("button", "btn ghost"); clear.textContent = "Clear";
  row.appendChild(run); row.appendChild(clear);
  b.appendChild(row);
  const err = el("div"); err.id = "cyerr"; b.appendChild(err);
  const snips = el("div", "snips");
  snips.appendChild(el("div", "panel-head", "Graphiti recipes"));
  for (const [label, q] of SNIPPETS) {
    const s = el("button", "", label);
    s.onclick = () => { ta.value = q; };
    snips.appendChild(s);
  }
  b.appendChild(snips);
  const out = el("div"); out.id = "cytable"; b.appendChild(out);

  clear.onclick = () => { ta.value = ""; err.style.display = "none"; out.innerHTML = ""; };
  run.onclick = async () => {
    err.style.display = "none"; out.innerHTML = '<div class="loading">running…</div>';
    try {
      const r = await api("/api/cypher", {
        method: "POST",
        headers: Object.assign({ "Content-Type": "application/json" }, TOKEN ? { "X-Lens-Token": TOKEN } : {}),
        body: JSON.stringify({ query: ta.value, limit: 500 }),
      });
      if (r.graph && r.graph.nodes.length) setGraph(r.graph);
      renderTable(out, r);
      setLeft("Cypher", `${r.row_count} rows${r.truncated ? " (capped)" : ""}`);
    } catch (e) {
      out.innerHTML = "";
      err.textContent = e.message; err.style.display = "block";
      setLeft("Cypher", "error");
    }
  };
  ta.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); run.click(); }
  });
}

function renderTable(out, r) {
  out.innerHTML = "";
  if (!r.rows.length) { out.innerHTML = '<div class="loading">0 rows.</div>'; return; }
  const t = el("table"), thead = el("thead"), tr = el("tr");
  for (const c of r.columns) tr.appendChild(el("th", "", c));
  thead.appendChild(tr); t.appendChild(thead);
  const tb = el("tbody");
  for (const row of r.rows.slice(0, 400)) {
    const x = el("tr");
    for (const c of r.columns) {
      const v = row[c];
      const s = (v && typeof v === "object") ? (v.name || v.fact || JSON.stringify(v)) : String(v == null ? "—" : v);
      const td = el("td", "", s.length > 90 ? s.slice(0, 89) + "…" : s);
      td.title = s;
      x.appendChild(td);
    }
    tb.appendChild(x);
  }
  t.appendChild(tb); out.appendChild(t);
}

// -------------------------------------------------------------------- audit
function sparkline(data, w, h) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("width", "100%"); svg.setAttribute("height", String(h));
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Episodes ingested per day, last ${data.length} days with activity`);
  if (!data.length) return svg;
  const pad = { t: 12, r: 10, b: 20, l: 30 };
  const max = Math.max(1, d3.max(data, (d) => d.count));
  const x = d3.scaleUtc()
    .domain(d3.extent(data, (d) => new Date(d.day)))
    .range([pad.l, w - pad.r]);
  const y = d3.scaleLinear().domain([0, max]).nice().range([h - pad.b, pad.t]);

  // recessive baseline + max gridline
  for (const v of [0, max]) {
    const ln = document.createElementNS(ns, "line");
    ln.setAttribute("x1", pad.l); ln.setAttribute("x2", w - pad.r);
    ln.setAttribute("y1", y(v)); ln.setAttribute("y2", y(v));
    ln.setAttribute("stroke", "#33332f"); ln.setAttribute("stroke-width", "1");
    svg.appendChild(ln);
  }
  for (const [v, dy] of [[max, 4], [0, -2]]) {
    const tx = document.createElementNS(ns, "text");
    tx.setAttribute("x", pad.l - 6); tx.setAttribute("y", y(v) + dy);
    tx.setAttribute("text-anchor", "end"); tx.setAttribute("fill", "#5f5e58");
    tx.setAttribute("font-size", "10"); tx.setAttribute("font-family", "ui-monospace, monospace");
    tx.textContent = String(v);
    svg.appendChild(tx);
  }
  const line = d3.line().x((d) => x(new Date(d.day))).y((d) => y(d.count)).curve(d3.curveMonotoneX);
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", line(data));
  path.setAttribute("fill", "none"); path.setAttribute("stroke", C.entity);
  path.setAttribute("stroke-width", "2"); path.setAttribute("stroke-linejoin", "round"); path.setAttribute("stroke-linecap", "round");
  svg.appendChild(path);

  for (const [i, lbl] of [[0, shortDate(data[0].day)], [data.length - 1, shortDate(data[data.length - 1].day)]]) {
    const tx = document.createElementNS(ns, "text");
    tx.setAttribute("x", x(new Date(data[i].day))); tx.setAttribute("y", h - 5);
    tx.setAttribute("text-anchor", i === 0 ? "start" : "end");
    tx.setAttribute("fill", "#5f5e58"); tx.setAttribute("font-size", "10");
    tx.setAttribute("font-family", "ui-monospace, monospace");
    tx.textContent = lbl;
    svg.appendChild(tx);
  }
  // hover layer
  const dot = document.createElementNS(ns, "circle");
  dot.setAttribute("r", "4"); dot.setAttribute("fill", C.entity);
  dot.setAttribute("stroke", "#1a1a19"); dot.setAttribute("stroke-width", "2");
  dot.setAttribute("opacity", "0"); svg.appendChild(dot);
  const lab = document.createElementNS(ns, "text");
  lab.setAttribute("fill", "#ffffff"); lab.setAttribute("font-size", "11");
  lab.setAttribute("text-anchor", "middle"); lab.setAttribute("opacity", "0");
  svg.appendChild(lab);
  const bisect = d3.bisector((d) => new Date(d.day)).center;
  svg.addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect();
    const mx = ((ev.clientX - r.left) / r.width) * w;
    const d = data[Math.max(0, Math.min(data.length - 1, bisect(data, x.invert(mx))))];
    dot.setAttribute("cx", x(new Date(d.day))); dot.setAttribute("cy", y(d.count)); dot.setAttribute("opacity", "1");
    lab.setAttribute("x", Math.max(34, Math.min(w - 34, x(new Date(d.day)))));
    lab.setAttribute("y", Math.max(11, y(d.count) - 9));
    lab.textContent = `${d.count} on ${shortDate(d.day)}`;
    lab.setAttribute("opacity", "1");
  });
  svg.addEventListener("mouseleave", () => { dot.setAttribute("opacity", "0"); lab.setAttribute("opacity", "0"); });
  return svg;
}


async function renderAudit() {
  const root = $("#audit");
  root.innerHTML = '<div class="loading">running audit…</div>';
  let h;
  try { h = await api(`/api/health?${groupParam().slice(1)}`); }
  catch (e) { root.innerHTML = `<div class="loading" style="color:${C.critical}">audit failed: ${esc(e.message)}</div>`; return; }
  root.innerHTML = "";

  const head = el("div", "aud-head");
  head.appendChild(el("h2", "", `Memory audit — ${h.group || "all projects"}`));
  head.appendChild(el("div", "m",
    `${fmtN(h.totals.entities)} entities · ${fmtN(h.totals.episodes)} episodes · ${fmtN(h.totals.edges)} facts · last ingest ${relDays(h.provenance.last_ingest)}`));
  const copy = el("button", "btn"); copy.textContent = "Copy JSON";
  copy.onclick = () => navigator.clipboard.writeText(JSON.stringify(h, null, 2)).then(() => { copy.textContent = "Copied"; setTimeout(() => copy.textContent = "Copy JSON", 1400); });
  head.appendChild(copy);
  root.appendChild(head);

  // KPI tiles
  const tiles = el("div", "grid tiles");
  for (const c of h.checks) {
    const t = el("div", "tile");
    t.appendChild(el("div", "lbl", c.label));
    const v = el("div", "val", String(c.value));
    v.appendChild(el("span", "", c.unit));
    t.appendChild(v);
    const badge = el("div", "badge " + c.status);
    const g = el("span", "g"); g.style.background = C[c.status] || C.ink3;
    badge.appendChild(g); badge.appendChild(el("span", "", c.status === "good" ? "healthy" : c.status));
    t.appendChild(badge);
    const meter = el("div", "meter");
    const fill = el("i");
    fill.style.width = Math.max(1.5, Math.min(100, c.value)) + "%";
    fill.style.background = C[c.status] || C.ink3;
    meter.appendChild(fill); t.appendChild(meter);
    t.appendChild(el("div", "det", c.detail));
    tiles.appendChild(t);
  }
  root.appendChild(tiles);

  const two = el("div", "grid two");

  // bitemporal composition
  const bt = el("div", "card2");
  bt.appendChild(el("h3", "", "Fact lifecycle"));
  bt.appendChild(el("div", "cap", "Every RELATES_TO edge, by whether it still stands. Superseded facts are not deleted — Graphiti keeps them and stamps when they stopped being true."));
  const b = h.bitemporal;
  const liveN = Math.max(0, b.total - b.invalidated - b.expired_only);
  const bar = el("div", "stackbar");
  const segs = [
    ["live", liveN, C.live], ["superseded", b.invalidated, C.warning], ["retracted", b.expired_only, C.critical],
  ];
  for (const [, n, col] of segs) {
    if (!n) continue;
    const i = el("i"); i.style.flex = String(n); i.style.background = col; bar.appendChild(i);
  }
  bt.appendChild(bar);
  const lg = el("div", "slegend");
  for (const [name, n, col] of segs) {
    const s = el("div", "");
    const sw = el("span", "sw"); sw.style.background = col; s.appendChild(sw);
    s.appendChild(el("span", "", name + " "));
    const strong = el("b", "", `${fmtN(n)} (${b.total ? Math.round(n / b.total * 100) : 0}%)`);
    s.appendChild(strong);
    lg.appendChild(s);
  }
  bt.appendChild(lg);
  if (b.both_stamped) {
    const w = el("div", "det");
    w.style.marginTop = "12px"; w.style.color = C.warning; w.style.fontSize = "11.5px"; w.style.lineHeight = "1.5";
    w.style.color = C.ink3;
    w.textContent = `${fmtN(b.both_stamped)} of ${fmtN(b.invalidated)} superseded edges carry both timestamps — ` +
      `invalid_at (when it stopped being true) and expired_at (when the record stopped being current). ` +
      `That is correct bitemporal behaviour, not a defect.`;
    bt.appendChild(w);
  }
  two.appendChild(bt);

  // ingest sparkline
  const ing = el("div", "card2");
  ing.appendChild(el("h3", "", "Ingestion, last 60 days"));
  ing.appendChild(el("div", "cap", "Episodes written per day. Flat stretches mean a project stopped feeding the graph."));
  ing.appendChild(sparkline(h.ingest_daily, 560, 150));
  two.appendChild(ing);

  // stale projects
  const st = el("div", "card2");
  st.appendChild(el("h3", "", "Quietest projects"));
  st.appendChild(el("div", "cap", "Ordered by last episode. The top of this list is where your memory has gone stale."));
  st.appendChild(table(["Project", "Last episode", "Episodes"],
    h.group_staleness.slice(0, 12).map((r) => [r.group, relDays(r.last), fmtN(r.episodes)]), [false, false, true]));
  two.appendChild(st);

  // duplicates
  const du = el("div", "card2");
  du.appendChild(el("h3", "", "Duplicate entity names"));
  du.appendChild(el("div", "cap", "Same name, same project, more than one node. Each pair splits facts that should sit on one entity."));
  du.appendChild(h.duplicates.length
    ? table(["Entity", "Project", "Copies"], h.duplicates.slice(0, 14).map((r) => [r.display, r.group, String(r.copies)]), [false, false, true])
    : el("div", "cap", "None found."));
  two.appendChild(du);

  root.appendChild(two);

  // hygiene counts
  const hy = el("div", "card2"); hy.style.marginTop = "12px";
  hy.appendChild(el("h3", "", "Hygiene counts"));
  hy.appendChild(el("div", "cap", "Raw numbers behind the tiles. Click a Cypher recipe to pull the underlying rows."));
  const rows = [
    ["Orphan entities (no facts at all)", h.hygiene.orphan_entities],
    ["Entities never mentioned by an episode", h.hygiene.unmentioned_entities],
    ["Episodes that produced no entities", h.hygiene.episodes_with_no_entities],
    ["Entities with no summary", h.hygiene.entities_without_summary],
    ["Duplicate name clusters", h.hygiene.duplicate_clusters],
    ["Episodes with no source_description", h.provenance.no_source_description],
    ["Episodes untagged (no OBSERVED:/DERIVED:)", h.provenance.untagged],
  ];
  hy.appendChild(table(["Check", "Count"], rows.map(([k, v]) => [k, fmtN(v)]), [false, true]));
  root.appendChild(hy);
}

function table(headers, rows, numeric) {
  const t = el("table"), thead = el("thead"), tr = el("tr");
  headers.forEach((hh, i) => { const th = el("th", "", hh); if (numeric && numeric[i]) th.style.textAlign = "right"; tr.appendChild(th); });
  thead.appendChild(tr); t.appendChild(thead);
  const tb = el("tbody");
  for (const r of rows) {
    const x = el("tr");
    r.forEach((c, i) => x.appendChild(el("td", numeric && numeric[i] ? "n" : "", String(c))));
    tb.appendChild(x);
  }
  t.appendChild(tb); return t;
}

// ---------------------------------------------------------------------- init
function applyAsof() {
  const on = S.asofOn;
  $("#asof").disabled = !on;
  $("#asofToggle").classList.toggle("on", on);
  $("#asofToggle").textContent = on ? "As of: on" : "As of: off";
  $("#asofLabel").textContent = on && S.asofISO ? shortDate(S.asofISO) : "—";
  // Recompute liveness client-side so the slider is instant.
  for (const e of S.edges) {
    if (!on) { e.live = true; continue; }
    const start = e.valid_at || e.created_at;
    e.live = (!start || start <= S.asofISO)
      && (!e.invalid_at || e.invalid_at > S.asofISO)
      && (!e.expired_at || e.expired_at > S.asofISO);
  }
  renderLegend(); draw();
}

function sliderToISO(v) {
  const [a, z] = S.span;
  if (!a || !z) return null;
  return new Date(a + (z - a) * (v / 1000)).toISOString();
}

async function boot() {
  resize();
  window.addEventListener("resize", resize);
  try {
    const meta = await api("/api/meta");
    S.meta = meta;
    $("#conn").textContent = `${fmtN(meta.totals.entities)} entities · ${fmtN(meta.totals.edges)} facts · ${meta.groups.length} projects`;
    const sel = $("#group");
    sel.appendChild(new Option("all projects", ""));
    for (const g of meta.groups) sel.appendChild(new Option(`${g.group} (${fmtN(g.entities)})`, g.group));
    const first = meta.span.first ? Date.parse(meta.span.first) : null;
    const last = meta.span.last ? Date.parse(meta.span.last) : null;
    S.span = [first, last];
    S.asofISO = last ? new Date(last).toISOString() : null;
    $("#asofLabel").textContent = "—";
  } catch (e) {
    $("#conn").textContent = "connection failed";
    $("#emptyHint").textContent = e.message;
    return;
  }

  $("#tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setMode(b.dataset.mode); });
  $("#group").addEventListener("change", (e) => {
    S.group = e.target.value;
    if (S.mode === "audit") renderAudit();
    else if (S.mode === "episodes") loadEpisodes();
    else if (S.mode === "explore" && $("#search").value.trim().length >= 2) runSearch();
  });
  $("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 210); });
  $("#kind").addEventListener("change", runSearch);
  $("#fit").addEventListener("click", fit);
  $("#pause").addEventListener("click", () => {
    S.paused = !S.paused;
    $("#pause").textContent = S.paused ? "Resume" : "Pause";
    $("#pause").classList.toggle("on", S.paused);
    if (S.sim) S.paused ? S.sim.stop() : S.sim.alpha(0.35).restart();
  });
  $("#hideSuper").addEventListener("click", () => {
    S.hideSuper = !S.hideSuper;
    $("#hideSuper").classList.toggle("on", S.hideSuper);
    computeNeighbours(); setSim(false); renderLegend(); draw();
  });
  $("#asofToggle").addEventListener("click", () => {
    S.asofOn = !S.asofOn;
    if (S.asofOn) S.asofISO = sliderToISO(+$("#asof").value);
    applyAsof();
  });
  $("#asof").addEventListener("input", (e) => { S.asofISO = sliderToISO(+e.target.value); applyAsof(); });

  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (e.key === "/" && !typing) { e.preventDefault(); $("#search").focus(); }
    if (e.key === "Escape") { S.sel = null; S.selEdge = null; computeNeighbours(); renderInspector(); draw(); if (typing) document.activeElement.blur(); }
    if (typing) return;
    if (e.key === "f" || e.key === "F") fit();
    if (e.key === " ") { e.preventDefault(); $("#pause").click(); }
  });

  setMode("explore");
  renderLegend();
}

boot();
})();
