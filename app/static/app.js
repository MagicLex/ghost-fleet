"use strict";
const BASE = (window.__BASE__ || "").replace(/\/$/, "");
const api = (p) => fetch(BASE + p).then(r => r.json());
const cv = document.getElementById("map"), cx = cv.getContext("2d");

// Baltic + Gulf of Finland default frame (the shadow-fleet theatre)
const FRAME = { lon0: 9, lon1: 30.5, lat0: 53.3, lat1: 61.0 };
let cam = { cx: (FRAME.lon0 + FRAME.lon1) / 2, cy: (FRAME.lat0 + FRAME.lat1) / 2, scale: 1 };
let DPR = 1, W = 0, H = 0;
const S = { view: "live", vessels: [], watch: [], heat: null, net: null,
            sel: null, trail: [], base: null, netSel: null, netHover: null };

function resize() {
  DPR = Math.min(2, window.devicePixelRatio || 1);
  W = cv.clientWidth || window.innerWidth;
  H = cv.clientHeight || window.innerHeight;
  if (!W || !H) { requestAnimationFrame(resize); return; }
  cv.width = W * DPR; cv.height = H * DPR;
  const sx = W / (FRAME.lon1 - FRAME.lon0);
  const sy = H / (FRAME.lat1 - FRAME.lat0);
  cam.scale = Math.min(sx, sy) * 0.98 / baseScale();
  draw();
}
function baseScale() { return 1; }
const cosMid = () => Math.cos((cam.cy) * Math.PI / 180);
function proj(lon, lat) {
  const k = cam.scale;
  return { x: W / 2 + (lon - cam.cx) * k * cosMid(),
           y: H / 2 - (lat - cam.cy) * k };
}
function unproj(x, y) {
  const k = cam.scale;
  return { lon: cam.cx + (x - W / 2) / (k * cosMid()),
           lat: cam.cy - (y - H / 2) / k };
}
// display position: dead-reckoned pos when animating, else the last server fix
const vlon = v => v._dlo != null ? v._dlo : v.lo;
const vlat = v => v._dla != null ? v._dla : v.la;

function colorFor(v) {
  if (v.sanc) return "#ff2d55";
  const s = v.s;
  if (s == null) return v.foc ? "#6f88b0" : "#4d7fa0";
  if (s < 0.5) return lerp("#3aa0ff", "#ffb020", s / 0.5);
  return lerp("#ffb020", "#ff3b30", (s - 0.5) / 0.5);
}
function lerp(a, b, t) {
  const pa = [1, 3, 5].map(i => parseInt(a.substr(i, 2), 16));
  const pb = [1, 3, 5].map(i => parseInt(b.substr(i, 2), 16));
  const c = pa.map((x, i) => Math.round(x + (pb[i] - x) * Math.max(0, Math.min(1, t))));
  return "rgb(" + c.join(",") + ")";
}

// ---- draw ----
function draw() {
  cx.save(); cx.scale(DPR, DPR);
  const g = cx.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, "#0a2233"); g.addColorStop(1, "#071722");
  cx.fillStyle = g; cx.fillRect(0, 0, W, H);
  drawBase();
  if (S.view === "heat") drawHeat();
  if (S.view === "net") { drawNetwork(); cx.restore(); return; }
  if (S.view === "live" || S.view === "heat") drawVessels();
  if (S.trail.length) drawTrail();
  cx.restore();
}

function drawBase() {
  const b = S.base; if (!b) return;
  cx.lineJoin = "round";
  (b.land || []).forEach(poly => {
    cx.beginPath();
    poly.forEach((p, i) => { const q = proj(p[0], p[1]); i ? cx.lineTo(q.x, q.y) : cx.moveTo(q.x, q.y); });
    cx.closePath(); cx.fillStyle = "#12212b"; cx.fill();
  });
  cx.strokeStyle = "#274d66"; cx.lineWidth = 0.7;
  (b.coast || []).forEach(line => {
    cx.beginPath();
    line.forEach((p, i) => { const q = proj(p[0], p[1]); i ? cx.lineTo(q.x, q.y) : cx.moveTo(q.x, q.y); });
    cx.stroke();
  });
}

function drawVessels() {
  const dim = S.view === "heat" ? 0.4 : 1;
  for (const v of S.vessels) {
    const q = proj(vlon(v), vlat(v));
    if (q.x < -20 || q.x > W + 20 || q.y < -20 || q.y > H + 20) continue;
    const col = colorFor(v);
    const hot = v.sanc || (v.s != null && v.s >= 0.5);
    if (hot) {
      cx.beginPath(); cx.arc(q.x, q.y, 12, 0, 7);
      cx.fillStyle = (v.sanc ? "rgba(255,45,85," : "rgba(255,59,48,") + (0.13 * dim) + ")"; cx.fill();
    }
    cx.globalAlpha = dim;
    // heading wake so movement reads on a screen recording
    if (v.sog != null && v.sog > 0.5 && v.cog != null) {
      const c = v.cog * Math.PI / 180, len = 5 + Math.min(11, v.sog);
      cx.strokeStyle = col; cx.lineWidth = 1.5; cx.beginPath();
      cx.moveTo(q.x, q.y); cx.lineTo(q.x + Math.sin(c) * len, q.y - Math.cos(c) * len); cx.stroke();
    }
    cx.beginPath(); cx.arc(q.x, q.y, hot ? 5 : 3.4, 0, 7);
    cx.fillStyle = col; cx.fill();
    if (v.sanc) { cx.strokeStyle = "#ff2d55"; cx.lineWidth = 1.5; cx.beginPath(); cx.arc(q.x, q.y, 8, 0, 7); cx.stroke(); }
    cx.globalAlpha = 1;
    if (v === S.sel) {
      cx.strokeStyle = "#dce8f0"; cx.lineWidth = 1.3;
      cx.beginPath(); cx.arc(q.x, q.y, 13, 0, 7); cx.stroke();
      cx.fillStyle = "#dce8f0"; cx.font = "11px monospace";
      cx.fillText((v.n || v.m) + (v.im ? "  IMO " + v.im : ""), q.x + 15, q.y + 3);
    }
  }
}

function drawTrail() {
  cx.strokeStyle = "rgba(220,232,240,.5)"; cx.lineWidth = 1;
  cx.beginPath();
  S.trail.forEach((p, i) => { const q = proj(p[1], p[0]); i ? cx.lineTo(q.x, q.y) : cx.moveTo(q.x, q.y); });
  cx.stroke();
}

// thermal ramp: keeps the blue -> amber -> red suspicion code, with a hot-white
// peak for the satellite-thermal feel.
const HEAT_STOPS = [[0, [16, 46, 74]], [0.28, [58, 160, 255]], [0.5, [80, 200, 172]],
                    [0.7, [255, 176, 32]], [0.86, [255, 59, 48]], [1, [255, 240, 205]]];
function thermal(t) {
  for (let i = 1; i < HEAT_STOPS.length; i++) {
    if (t <= HEAT_STOPS[i][0]) {
      const a = HEAT_STOPS[i - 1], b = HEAT_STOPS[i], k = (t - a[0]) / (b[0] - a[0]);
      return a[1].map((v, j) => Math.round(v + (b[1][j] - v) * k));
    }
  }
  return HEAT_STOPS[HEAT_STOPS.length - 1][1];
}
let heatBuf = null;
// Precise satellite-thermal raster: splat each event/corridor point as a gaussian
// into a low-res intensity grid, thermal-map it, scale up NEAREST (crisp pixels).
function drawHeat() {
  const h = S.heat; if (!h) return;
  const SC = 5;                                   // px per raster cell (precision vs pixel look)
  const gw = Math.max(1, Math.ceil(W / SC)), gh = Math.max(1, Math.ceil(H / SC));
  const acc = new Float32Array(gw * gh);
  const splat = (arr, mul, rad) => (arr || []).forEach(p => {
    const q = proj(p[1], p[0]), gx = q.x / SC, gy = q.y / SC, wt = (p[2] || 1) * mul, r2 = rad * rad;
    const x0 = Math.max(0, (gx - rad) | 0), x1 = Math.min(gw - 1, (gx + rad) | 0);
    const y0 = Math.max(0, (gy - rad) | 0), y1 = Math.min(gh - 1, (gy + rad) | 0);
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
      const dx = x - gx, dy = y - gy, d2 = dx * dx + dy * dy;
      if (d2 <= r2) acc[y * gw + x] += wt * Math.exp(-d2 / (r2 * 0.35));
    }
  });
  splat(h.dark, 1.0, 9);
  splat(h.corridor, 1.3, 7);
  let mx = 0; for (let i = 0; i < acc.length; i++) if (acc[i] > mx) mx = acc[i];
  if (mx > 0) {
    if (!heatBuf) heatBuf = document.createElement("canvas");
    heatBuf.width = gw; heatBuf.height = gh;
    const hcx = heatBuf.getContext("2d"), img = hcx.createImageData(gw, gh), D = img.data;
    for (let i = 0; i < acc.length; i++) {
      const t = acc[i] / mx;
      if (t <= 0.03) { D[i * 4 + 3] = 0; continue; }
      const c = thermal(t);
      D[i * 4] = c[0]; D[i * 4 + 1] = c[1]; D[i * 4 + 2] = c[2];
      D[i * 4 + 3] = Math.round(230 * Math.pow(Math.min(1, t), 0.55));
    }
    hcx.putImageData(img, 0, 0);
    cx.imageSmoothingEnabled = false;             // crisp pixel raster, not blur
    cx.globalCompositeOperation = "lighter";
    cx.drawImage(heatBuf, 0, 0, gw, gh, 0, 0, gw * SC, gh * SC);
    cx.globalCompositeOperation = "source-over";
    cx.imageSmoothingEnabled = true;
  }
  cx.setLineDash([5, 4]); cx.strokeStyle = "rgba(255,90,80,.5)"; cx.lineWidth = 1;
  (h.hotspots || []).forEach(p => {
    const q = proj(p[1], p[0]), r = p[2] / 111 * cam.scale;
    cx.beginPath(); cx.arc(q.x, q.y, r, 0, 7); cx.stroke();
  });
  cx.setLineDash([]);
}

function netColor(v) {
  if (v.sanc) return "#ff2d55";
  const s = v.score;
  if (s == null) return "#3aa0ff";
  if (s < 0.5) return lerp("#3aa0ff", "#ffb020", s / 0.5);
  return lerp("#ffb020", "#ff3b30", (s - 0.5) / 0.5);
}
const nodeRadius = v => (v.sanc ? 7 : 4) + Math.min(7, v.deg || 0) + (v.score ? v.score * 4 : 0);
function drawNetwork() {
  const g = cx.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, "#0a2233"); g.addColorStop(1, "#071722");
  cx.fillStyle = g; cx.fillRect(0, 0, W, H);
  const n = S.net;
  if (!n || !n.nodes.length) {
    cx.fillStyle = "#5f7f94"; cx.font = "13px monospace";
    cx.fillText("no confirmed rendezvous rings yet. the graph fills as", 40, H / 2 - 10);
    cx.fillText("ship-to-ship encounters accumulate in the theatre.", 40, H / 2 + 10);
    return;
  }
  const idx = {}; n.nodes.forEach((v, i) => idx[v.mmsi] = i);
  const cxp = W / 2, cyp = H / 2, R = Math.min(W, H) * 0.37;
  n.nodes.forEach((v, i) => {
    const a = i / n.nodes.length * Math.PI * 2 - Math.PI / 2;
    v._x = cxp + R * Math.cos(a); v._y = cyp + R * Math.sin(a);
  });
  const active = S.netSel || S.netHover;
  const nb = new Set();
  if (active) n.edges.forEach(e => { if (e.a === active) nb.add(e.b); if (e.b === active) nb.add(e.a); });
  n.edges.forEach(e => {
    const a = n.nodes[idx[e.a]], b = n.nodes[idx[e.b]]; if (!a || !b) return;
    const on = active && (e.a === active || e.b === active);
    cx.strokeStyle = on ? "rgba(255,190,110,.95)" : (active ? "rgba(255,120,90,.12)" : "rgba(255,120,90,.3)");
    cx.lineWidth = on ? Math.min(6, 1.6 + e.w) : Math.min(3, e.w);
    cx.beginPath(); cx.moveTo(a._x, a._y); cx.lineTo(b._x, b._y); cx.stroke();
  });
  n.nodes.forEach(v => {
    const isSel = S.netSel === v.mmsi, isAct = active === v.mmsi, isNb = nb.has(v.mmsi);
    const r = nodeRadius(v), dim = active && !isAct && !isNb ? 0.28 : 1;
    const hot = v.sanc || (v.score != null && v.score >= 0.5);
    cx.globalAlpha = dim;
    if (hot) { cx.beginPath(); cx.arc(v._x, v._y, r + 7, 0, 7); cx.fillStyle = v.sanc ? "rgba(255,45,85,.15)" : "rgba(255,59,48,.15)"; cx.fill(); }
    cx.beginPath(); cx.arc(v._x, v._y, r, 0, 7); cx.fillStyle = netColor(v); cx.fill();
    if (v.sanc) { cx.strokeStyle = "#ff2d55"; cx.lineWidth = 1.6; cx.beginPath(); cx.arc(v._x, v._y, r + 3, 0, 7); cx.stroke(); }
    if (isSel) { cx.strokeStyle = "#dce8f0"; cx.lineWidth = 1.8; cx.beginPath(); cx.arc(v._x, v._y, r + 5, 0, 7); cx.stroke(); }
    const showLabel = isAct || isNb || v.sanc || hot || n.nodes.length <= 26;
    if (showLabel) {
      cx.fillStyle = isAct ? "#eaf2f8" : "#9fb8c8"; cx.font = (isAct ? "11px" : "10px") + " monospace";
      cx.fillText((v.name || v.mmsi) + (v.flag ? "  " + v.flag : ""), v._x + r + 5, v._y + 3);
      if ((isAct || isNb) && v.score != null) {
        cx.fillStyle = "#7fa0b4"; cx.font = "9px monospace";
        cx.fillText((v.score * 100).toFixed(0) + "%", v._x + r + 5, v._y + 15);
      }
    }
    cx.globalAlpha = 1;
  });
  cx.fillStyle = "#5f7f94"; cx.font = "10px monospace";
  cx.fillText(n.nodes.length + " vessels, " + n.edges.length + " rendezvous edges. click a node for its dossier.", 16, H - 16);
}
function nodeAt(px, py) {
  const n = S.net; if (!n) return null;
  let best = null, bd = 16;
  for (const v of n.nodes) {
    if (v._x == null) continue;
    const d = Math.hypot(v._x - px, v._y - py);
    if (d < bd) { bd = d; best = v; }
  }
  return best;
}
function netHit(px, py) {
  const v = nodeAt(px, py);
  if (v) { S.netSel = v.mmsi; api("/api/vessel/" + v.mmsi).then(showDossier); draw(); }
  else { S.netSel = null; document.getElementById("dossier").classList.remove("show"); draw(); }
}

// ---- rail + dossier ----
function renderRail() {
  const el = document.getElementById("rail");
  el.innerHTML = S.watch.map(w =>
    `<div class="card ${w.sanc ? "sanc" : ""}" data-m="${w.m}">
       <div class="h"><span>${esc(w.n)}</span><span class="sc">${w.sanc ? "LISTED" : (w.s * 100).toFixed(0) + "%"}</span></div>
       <div class="w">${(w.why || []).map(esc).join(" &middot; ")}</div>
     </div>`).join("") ||
    `<div class="card" style="border-left-color:#274d66;cursor:default">
       <div class="w">watching ${S.vessels.length} vessels. sanctioned ships and high-suspicion tracks surface here.</div></div>`;
  el.querySelectorAll(".card[data-m]").forEach(c =>
    c.onclick = () => focusVessel(c.getAttribute("data-m"), true));
}

let panAnim = null;
function panTo(lon, lat) {                          // gentle eased pan, no hard jump
  const x0 = cam.cx, y0 = cam.cy, t0 = performance.now(), dur = 420;
  cancelAnimationFrame(panAnim);
  (function step(t) {
    const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
    cam.cx = x0 + (lon - x0) * e; cam.cy = y0 + (lat - y0) * e; draw();
    if (k < 1) panAnim = requestAnimationFrame(step);
  })(t0);
}
function focusVessel(mmsi, recenter) {
  const v = S.vessels.find(x => x.m === mmsi);
  if (v) { S.sel = v; if (recenter) panTo(vlon(v), vlat(v)); }  // map clicks stay put
  api("/api/trail/" + mmsi).then(d => { S.trail = d.trail || []; draw(); });
  api("/api/vessel/" + mmsi).then(showDossier);
  draw();
}

function showDossier(d) {
  const el = document.getElementById("dossier");
  const sc = d.score == null ? "&mdash;" : (d.score * 100).toFixed(0) + "%";
  const rows = [["flag", d.flag], ["type", d.type], ["built", d.built_year ? d.built_year | 0 : "—"],
    ["tonnage", d.gross_tonnage ? d.gross_tonnage | 0 : "—"], ["destination", d.destination || "—"],
    ["draught", d.draught || "—"]];
  el.innerHTML = `<div class="x" onclick="document.getElementById('dossier').classList.remove('show')">&times;</div>
    <h2>${esc(d.name || d.mmsi)}${d.sanctioned ? '<span class="badge">SANCTIONED</span>' : ""}</h2>
    <div class="sub">MMSI ${d.mmsi}${d.imo ? " &nbsp; IMO " + d.imo : ""}</div>
    <div class="score" style="color:${d.sanctioned ? "#ff2d55" : "#ffb020"}">${sc}<span style="font-size:11px;color:#7fa0b4"> suspicion</span></div>
    ${d.sanction ? `<div class="w" style="color:#ff8095;font-size:11px;margin:6px 0">${esc(d.sanction.programs || "")}</div>` : ""}
    <ul>${(d.reasons || []).map(r => "<li>" + esc(r) + "</li>").join("") || "<li style='color:#7fa0b4'>no behavioural flags on record</li>"}</ul>
    ${d.events && d.events.length ? `<div class="sub">${d.events.length} GFW events: ` +
      d.events.slice(0, 4).map(e => e.type).join(", ") + "</div>" : ""}
    <div class="rows">${rows.map(r => `<div class="row"><span>${r[0]}</span><span>${esc(String(r[1] ?? "—"))}</span></div>`).join("")}</div>
    <div style="margin-top:10px">
      <a href="${d.links.marinetraffic}" target="_blank">MarineTraffic</a>
      ${d.links.gfw ? `<a href="${d.links.gfw}" target="_blank">Global Fishing Watch</a>` : ""}
      <a href="${d.links.equasis}" target="_blank">Equasis</a>
    </div>`;
  el.classList.add("show");
}
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---- polling ----
async function tick() {
  try {
    if (S.view === "net") { S.net = await api("/api/network"); draw(); return; }
    const d = await api("/api/state");
    S.vessels = d.vessels; S.watch = d.watch;
    if (S.sel) S.sel = S.vessels.find(v => v.m === S.sel.m) || S.sel;
    document.getElementById("s-tracked").textContent = d.stats.tracked;
    document.getElementById("s-sanc").textContent = d.stats.sanctioned_live;
    document.getElementById("s-scored").textContent = d.stats.scored;
    const mo = document.getElementById("s-model");
    mo.textContent = d.stats.model ? "MODEL LIVE" : "MODEL OFFLINE";
    mo.style.color = d.stats.model ? "#3aa0ff" : "#4a6579";
    if (S.view === "heat") S.heat = await api("/api/heatmap");
    renderRail(); draw();
  } catch (e) { /* transient */ }
}

// ---- interaction ----
let drag = null;
cv.addEventListener("pointerdown", e => drag = { x: e.clientX, y: e.clientY, cx: cam.cx, cy: cam.cy, moved: 0 });
cv.addEventListener("pointermove", e => {
  if (!drag) {                                      // hover: cursor + network highlight
    if (S.view === "net") {
      const h = nodeAt(e.clientX, e.clientY), m = h ? h.mmsi : null;
      cv.style.cursor = h ? "pointer" : "default";
      if (m !== S.netHover) { S.netHover = m; draw(); }
    } else if (S.view === "live") {
      cv.style.cursor = nearestVessel(e.clientX, e.clientY, 18) ? "pointer" : "grab";
    }
    return;
  }
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  drag.moved += Math.abs(dx) + Math.abs(dy);
  cam.cx = drag.cx - dx / (cam.scale * cosMid());
  cam.cy = drag.cy + dy / cam.scale;
  draw();
});
cv.addEventListener("pointerup", e => {
  if (drag && drag.moved < 5) {
    if (S.view === "live") hitTest(e.clientX, e.clientY);
    else if (S.view === "net") netHit(e.clientX, e.clientY);
  }
  drag = null;
});
cv.addEventListener("wheel", e => {
  e.preventDefault();
  const f = Math.exp(-e.deltaY * 0.0016);
  cam.scale *= f; draw();
}, { passive: false });

function nearestVessel(px, py, tol) {
  let best = null, bd = tol;
  for (const v of S.vessels) {
    const q = proj(vlon(v), vlat(v));
    const d = Math.hypot(q.x - px, q.y - py);
    if (d < bd) { bd = d; best = v; }
  }
  return best;
}
function hitTest(px, py) {
  const best = nearestVessel(px, py, 18);           // bigger target on a wide map
  if (best) focusVessel(best.m, false);             // no camera yank on a map click
  else { S.sel = null; S.trail = []; document.getElementById("dossier").classList.remove("show"); draw(); }
}

document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("on"));
  t.classList.add("on");
  S.view = t.getAttribute("data-v");
  document.getElementById("legend").style.display = S.view === "net" ? "none" : "block";
  tick();
});

// ---- live motion: dead-reckon vessels forward between 5s server fixes so the
// fleet glides continuously (reads as live on a screen recording) ----
let _lastT = 0;
function animate(t) {
  requestAnimationFrame(animate);
  if (S.view !== "live") { _lastT = t; return; }
  const dt = _lastT ? Math.min(1.5, (t - _lastT) / 1000) : 0; _lastT = t;
  if (!dt) return;
  let moved = false;
  for (const v of S.vessels) {
    if (v.sog == null || v.sog < 0.3 || v.cog == null) continue;
    if (v._dla == null) { v._dla = v.la; v._dlo = v.lo; }
    const dLat = v.sog / 216000 * dt, c = v.cog * Math.PI / 180;   // knots -> deg-lat/s
    v._dla += dLat * Math.cos(c);
    v._dlo += dLat * Math.sin(c) / Math.max(0.2, Math.cos(v.la * Math.PI / 180));
    moved = true;
  }
  if (moved) draw();
}
requestAnimationFrame(animate);

// ---- live UTC clock ----
function clockTick() {
  const d = new Date(), p = n => String(n).padStart(2, "0"),
        el = document.getElementById("s-clock");
  if (el) el.textContent = p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds()) + "Z";
}
setInterval(clockTick, 1000); clockTick();

window.addEventListener("resize", resize);
window.addEventListener("load", resize);
api("/static/basemap.json").then(b => { S.base = b; resize(); draw(); });
resize(); tick();
setInterval(tick, 5000);
