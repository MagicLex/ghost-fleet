"use strict";
const BASE = (window.__BASE__ || "").replace(/\/$/, "");
const api = (p) => fetch(BASE + p).then(r => r.json());
const cv = document.getElementById("map"), cx = cv.getContext("2d");

// Baltic + Gulf of Finland default frame (the shadow-fleet theatre)
const FRAME = { lon0: 9, lon1: 30.5, lat0: 53.3, lat1: 61.0 };
let cam = { cx: (FRAME.lon0 + FRAME.lon1) / 2, cy: (FRAME.lat0 + FRAME.lat1) / 2, scale: 1 };
let DPR = 1, W = 0, H = 0;
const S = { view: "live", vessels: [], watch: [], heat: null, net: null,
            sel: null, trail: [], base: null };

function resize() {
  DPR = Math.min(2, window.devicePixelRatio || 1);
  W = cv.clientWidth; H = cv.clientHeight;
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
  const dim = S.view === "heat" ? 0.35 : 1;
  for (const v of S.vessels) {
    const q = proj(v.lo, v.la);
    if (q.x < -20 || q.x > W + 20 || q.y < -20 || q.y > H + 20) continue;
    const col = colorFor(v);
    const hot = v.sanc || (v.s != null && v.s >= 0.5);
    if (hot) {
      cx.beginPath(); cx.arc(q.x, q.y, 9, 0, 7);
      cx.fillStyle = (v.sanc ? "rgba(255,45,85," : "rgba(255,59,48,") + (0.12 * dim) + ")"; cx.fill();
    }
    cx.globalAlpha = dim;
    cx.beginPath(); cx.arc(q.x, q.y, hot ? 3.4 : 2.2, 0, 7);
    cx.fillStyle = col; cx.fill();
    if (v.sanc) { cx.strokeStyle = "#ff2d55"; cx.lineWidth = 1.4; cx.beginPath(); cx.arc(q.x, q.y, 6, 0, 7); cx.stroke(); }
    cx.globalAlpha = 1;
    if (v === S.sel) {
      cx.strokeStyle = "#dce8f0"; cx.lineWidth = 1.2;
      cx.beginPath(); cx.arc(q.x, q.y, 11, 0, 7); cx.stroke();
      cx.fillStyle = "#dce8f0"; cx.font = "11px monospace";
      cx.fillText((v.n || v.m) + (v.im ? "  IMO " + v.im : ""), q.x + 13, q.y + 3);
    }
  }
}

function drawTrail() {
  cx.strokeStyle = "rgba(220,232,240,.5)"; cx.lineWidth = 1;
  cx.beginPath();
  S.trail.forEach((p, i) => { const q = proj(p[1], p[0]); i ? cx.lineTo(q.x, q.y) : cx.moveTo(q.x, q.y); });
  cx.stroke();
}

function radial(q, r, c0) {
  const g = cx.createRadialGradient(q.x, q.y, 0, q.x, q.y, r);
  g.addColorStop(0, c0); g.addColorStop(1, "rgba(0,0,0,0)");
  cx.fillStyle = g; cx.beginPath(); cx.arc(q.x, q.y, r, 0, 7); cx.fill();
}
function drawHeat() {
  const h = S.heat; if (!h) return;
  cx.globalCompositeOperation = "screen";
  (h.dark || []).forEach(p => radial(proj(p[1], p[0]), 26 + 8 * p[2], "rgba(255,59,48,.42)"));
  (h.corridor || []).forEach(p => radial(proj(p[1], p[0]), 20, "rgba(255,176,32,.35)"));
  cx.globalCompositeOperation = "source-over";
  cx.setLineDash([5, 4]); cx.strokeStyle = "rgba(255,90,80,.5)"; cx.lineWidth = 1;
  (h.hotspots || []).forEach(p => {
    const q = proj(p[1], p[0]); const r = p[2] / 111 * cam.scale;
    cx.beginPath(); cx.arc(q.x, q.y, r, 0, 7); cx.stroke();
  });
  cx.setLineDash([]);
}

function drawNetwork() {
  const g = cx.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, "#0a2233"); g.addColorStop(1, "#071722");
  cx.fillStyle = g; cx.fillRect(0, 0, W, H);
  const n = S.net;
  if (!n || !n.nodes.length) {
    cx.fillStyle = "#5f7f94"; cx.font = "13px monospace";
    cx.fillText("no confirmed rendezvous rings yet — the graph fills as", 40, H / 2 - 10);
    cx.fillText("ship-to-ship encounters accumulate in the theatre.", 40, H / 2 + 10);
    return;
  }
  const idx = {}; n.nodes.forEach((v, i) => idx[v.mmsi] = i);
  const cxp = W / 2, cyp = H / 2, R = Math.min(W, H) * 0.36;
  n.nodes.forEach((v, i) => {
    const a = i / n.nodes.length * Math.PI * 2;
    v._x = cxp + R * Math.cos(a); v._y = cyp + R * Math.sin(a);
  });
  cx.strokeStyle = "rgba(255,120,90,.4)";
  n.edges.forEach(e => {
    const a = n.nodes[idx[e.a]], b = n.nodes[idx[e.b]];
    if (!a || !b) return;
    cx.lineWidth = Math.min(4, e.w); cx.beginPath();
    cx.moveTo(a._x, a._y); cx.lineTo(b._x, b._y); cx.stroke();
  });
  n.nodes.forEach(v => {
    cx.beginPath(); cx.arc(v._x, v._y, v.sanc ? 7 : 4 + Math.min(6, v.deg), 0, 7);
    cx.fillStyle = v.sanc ? "#ff2d55" : "#3aa0ff"; cx.fill();
    cx.fillStyle = "#9fb8c8"; cx.font = "9px monospace"; cx.fillText(v.mmsi, v._x + 8, v._y + 3);
  });
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
    c.onclick = () => focusVessel(c.getAttribute("data-m")));
}

function focusVessel(mmsi) {
  const v = S.vessels.find(x => x.m === mmsi);
  if (v) { S.sel = v; cam.cx = v.lo; cam.cy = v.la; }
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
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  drag.moved += Math.abs(dx) + Math.abs(dy);
  cam.cx = drag.cx - dx / (cam.scale * cosMid());
  cam.cy = drag.cy + dy / cam.scale;
  draw();
});
cv.addEventListener("pointerup", e => {
  if (drag && drag.moved < 5 && S.view === "live") hitTest(e.clientX, e.clientY);
  drag = null;
});
cv.addEventListener("wheel", e => {
  e.preventDefault();
  const f = Math.exp(-e.deltaY * 0.0016);
  cam.scale *= f; draw();
}, { passive: false });

function hitTest(px, py) {
  let best = null, bd = 12;
  for (const v of S.vessels) {
    const q = proj(v.lo, v.la);
    const d = Math.hypot(q.x - px, q.y - py);
    if (d < bd) { bd = d; best = v; }
  }
  if (best) focusVessel(best.m);
  else { S.sel = null; S.trail = []; document.getElementById("dossier").classList.remove("show"); draw(); }
}

document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("on"));
  t.classList.add("on");
  S.view = t.getAttribute("data-v");
  document.getElementById("legend").style.display = S.view === "net" ? "none" : "block";
  tick();
});

window.addEventListener("resize", resize);
api("/static/basemap.json").then(b => { S.base = b; draw(); });
resize(); tick();
setInterval(tick, 5000);
