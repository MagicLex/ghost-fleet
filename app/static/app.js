"use strict";
const BASE = (window.__BASE__ || "").replace(/\/$/, "");
const api = (p) => fetch(BASE + p).then(r => r.json());
const cv = document.getElementById("map"), cx = cv.getContext("2d");

// Baltic + Gulf of Finland default frame (the shadow-fleet theatre)
const FRAME = { lon0: 9, lon1: 30.5, lat0: 53.3, lat1: 61.0 };
let cam = { cx: (FRAME.lon0 + FRAME.lon1) / 2, cy: (FRAME.lat0 + FRAME.lat1) / 2, scale: 1 };
let DPR = 1, W = 0, H = 0;
const S = { view: "live", vessels: [], watch: [], heat: null, net: null, dark: [],
            sel: null, trail: [], trails: {}, trailsAt: 0, base: null,
            netSel: null, netHover: null, filter: { scored: false, dark: false } };
// show-only filters: none active = everything; active chips = union of categories
const anyFilter = () => S.filter.scored || S.filter.dark;
const visV = v => !anyFilter() || (S.filter.scored && v.s != null && v.s > 0) || v.sanc;
const visDark = () => !anyFilter() || S.filter.dark;

function resize() {
  DPR = Math.min(2, window.devicePixelRatio || 1);
  W = cv.clientWidth || window.innerWidth;
  H = cv.clientHeight || window.innerHeight;
  if (!W || !H) { requestAnimationFrame(resize); return; }
  cv.width = W * DPR; cv.height = H * DPR;
  const sx = W / (FRAME.lon1 - FRAME.lon0);
  const sy = H / (mercY(FRAME.lat1) - mercY(FRAME.lat0));
  cam.scale = Math.min(sx, sy) * 0.98;
  draw();
}
// Web Mercator projection so satellite imagery tiles align pixel-perfect.
// mercY maps lat to a world y in [-180, 180] "degrees".
const MER = 180 / Math.PI;
const mercY = lat => MER * Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
const imercY = y => (2 * Math.atan(Math.exp(y / MER)) - Math.PI / 2) * 180 / Math.PI;
function proj(lon, lat) {
  const k = cam.scale;
  return { x: W / 2 + (lon - cam.cx) * k,
           y: H / 2 - (mercY(lat) - mercY(cam.cy)) * k };
}
function unproj(x, y) {
  const k = cam.scale;
  return { lon: cam.cx + (x - W / 2) / k,
           lat: imercY(mercY(cam.cy) - (y - H / 2) / k) };
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
  g.addColorStop(0, "#081c2b"); g.addColorStop(1, "#040f18");
  cx.fillStyle = g; cx.fillRect(0, 0, W, H);
  if (S.view === "net") { drawNetwork(); cx.restore(); return; }
  const tiled = drawTiles();
  drawBase(tiled);
  drawGraticule();
  if (S.view === "heat") drawHeat();
  if (S.view === "live") drawWakes();
  drawVessels();
  if (S.trail.length) drawTrail();
  if (S.dark && S.dark.length) drawDark();
  drawVignette();
  cx.restore();
}

// ---- satellite imagery: Esri World Imagery tiles (free, no key), drawn under a
// dark navy wash so vessel dots keep contrast. Vector coast stays as fallback. ----
const TILES = new Map();
function getTile(z, x, y) {
  const key = z + "/" + x + "/" + y;
  let t = TILES.get(key);
  if (t) return t;
  if (TILES.size > 400) TILES.delete(TILES.keys().next().value);
  t = { img: new Image(), ok: false };
  t.img.onload = () => { t.ok = true; draw(); };
  t.img.src = `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${z}/${y}/${x}`;
  TILES.set(key, t);
  return t;
}
function drawTiles() {
  const k = cam.scale, myc = mercY(cam.cy);
  let z = Math.max(3, Math.min(12, Math.round(Math.log2(k * 360 / 256))));
  let n = 1 << z;
  const lonW = W / k, myH = H / k;
  while (z > 3 && (lonW / (360 / n)) * (myH / (360 / n)) > 200) { z--; n = 1 << z; }
  const tx0 = Math.floor((cam.cx - lonW / 2 + 180) / 360 * n);
  const tx1 = Math.floor((cam.cx + lonW / 2 + 180) / 360 * n);
  const ty0 = Math.max(0, Math.floor((1 - (myc + myH / 2) / 180) / 2 * n));
  const ty1 = Math.min(n - 1, Math.floor((1 - (myc - myH / 2) / 180) / 2 * n));
  let drew = 0;
  for (let ty = ty0; ty <= ty1; ty++) for (let tx = tx0; tx <= tx1; tx++) {
    const t = getTile(z, ((tx % n) + n) % n, ty);
    if (!t.ok) continue;
    const lonA = tx / n * 360 - 180, lonB = (tx + 1) / n * 360 - 180;
    const myA = 180 * (1 - 2 * ty / n), myB = 180 * (1 - 2 * (ty + 1) / n);
    const x0 = W / 2 + (lonA - cam.cx) * k, x1 = W / 2 + (lonB - cam.cx) * k;
    const y0 = H / 2 - (myA - myc) * k, y1 = H / 2 - (myB - myc) * k;
    cx.drawImage(t.img, x0, y0, x1 - x0 + 0.6, y1 - y0 + 0.6);
    drew++;
  }
  if (drew) { cx.fillStyle = "rgba(4,15,26,.52)"; cx.fillRect(0, 0, W, H); }  // ops-room wash
  return drew > 0;
}

const RU_OIL = { "Primorsk": 1, "Ust-Luga": 1, "Vysotsk": 1 };  // shadow-fleet loading terminals
function landPath(poly) {
  cx.beginPath();
  poly.forEach((p, i) => { const q = proj(p[0], p[1]); i ? cx.lineTo(q.x, q.y) : cx.moveTo(q.x, q.y); });
  cx.closePath();
}
function drawBase(tiled) {
  const b = S.base; if (!b) return;
  cx.lineJoin = cx.lineCap = "round";
  const land = b.land || [];
  if (!tiled) {                                     // vector fallback while tiles load / offline
    land.forEach(poly => { landPath(poly); cx.fillStyle = "#12211c"; cx.fill(); });
    cx.strokeStyle = "rgba(78,168,200,.13)"; cx.lineWidth = 4;
    land.forEach(poly => { landPath(poly); cx.stroke(); });
  }
  cx.strokeStyle = tiled ? "rgba(132,206,230,.28)" : "rgba(132,206,230,.6)";        // crisp coastline
  cx.lineWidth = 1;
  land.forEach(poly => { landPath(poly); cx.stroke(); });
  cx.setLineDash([4, 4]); cx.strokeStyle = "rgba(150,180,205,.28)"; cx.lineWidth = 1;  // country borders
  (b.borders || []).forEach(line => {
    cx.beginPath();
    line.forEach((p, i) => { const q = proj(p[0], p[1]); i ? cx.lineTo(q.x, q.y) : cx.moveTo(q.x, q.y); });
    cx.stroke();
  });
  cx.setLineDash([]);
  cx.font = "10.5px monospace"; cx.fillStyle = "rgba(165,200,220,.42)";              // country names
  cx.textAlign = "center"; cx.letterSpacing = "3px";
  (b.labels || []).forEach(l => {
    const q = proj(l[0], l[1]);
    if (q.x < 30 || q.x > W - 30 || q.y < 60 || q.y > H - 30) return;
    cx.fillText(l[2], q.x, q.y);
  });
  cx.textAlign = "left"; cx.letterSpacing = "0px";
  (b.ports || []).forEach(p => {
    const q = proj(p[0], p[1]);
    if (q.x < -40 || q.x > W + 40 || q.y < -20 || q.y > H + 20) return;
    const oil = RU_OIL[p[2]], c = oil ? "255,176,32" : "127,208,232";
    cx.fillStyle = `rgb(${c})`; cx.beginPath(); cx.arc(q.x, q.y, oil ? 3.2 : 2.4, 0, 7); cx.fill();
    cx.strokeStyle = `rgba(${c},.45)`; cx.lineWidth = 1;
    cx.beginPath(); cx.arc(q.x, q.y, oil ? 6 : 4.5, 0, 7); cx.stroke();
    cx.fillStyle = oil ? "rgba(255,205,120,.92)" : "rgba(160,205,225,.8)";
    cx.font = (oil ? "9.5px" : "9px") + " monospace";
    cx.fillText(p[2], q.x + 7, q.y + 3);
  });
}

function drawGraticule() {
  cx.strokeStyle = "rgba(70,120,150,.09)"; cx.lineWidth = 1;
  cx.fillStyle = "rgba(120,160,190,.28)"; cx.font = "9px monospace";
  for (let lon = 0; lon <= 40; lon += 2) {
    const a = proj(lon, cam.cy); if (a.x < 24 || a.x > W) continue;
    cx.beginPath(); cx.moveTo(a.x, 0); cx.lineTo(a.x, H); cx.stroke();
    cx.fillText(lon + "°E", a.x + 2, H - 20);
  }
  for (let lat = 48; lat <= 66; lat += 1) {
    const a = proj(cam.cx, lat); if (a.y < 40 || a.y > H) continue;
    cx.beginPath(); cx.moveTo(0, a.y); cx.lineTo(W, a.y); cx.stroke();
    cx.fillText(lat + "°N", 4, a.y - 3);
  }
}

function drawVignette() {
  const g = cx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.36, W / 2, H / 2, Math.max(W, H) * 0.72);
  g.addColorStop(0, "rgba(0,0,0,0)"); g.addColorStop(1, "rgba(2,8,14,.5)");
  cx.fillStyle = g; cx.fillRect(0, 0, W, H);
}

// wake trails: recent track behind every moving vessel, so the fleet reads as
// alive even though real ship speed is ~1px/min at this zoom. Positions stay honest.
function drawWakes() {
  cx.lineJoin = cx.lineCap = "round";
  for (const v of S.vessels) {
    if (!visV(v)) continue;
    const tr = S.trails[v.m]; if (!tr || tr.length < 2) continue;
    const qh = proj(vlon(v), vlat(v));
    if (qh.x < -150 || qh.x > W + 150 || qh.y < -150 || qh.y > H + 150) continue;
    const col = colorFor(v);
    cx.strokeStyle = col; cx.globalAlpha = 0.16; cx.lineWidth = 1;
    cx.beginPath();
    tr.forEach((p, i) => { const q = proj(p[1], p[0]); i ? cx.lineTo(q.x, q.y) : cx.moveTo(q.x, q.y); });
    cx.lineTo(qh.x, qh.y); cx.stroke();
    cx.globalAlpha = 0.5; cx.lineWidth = 1.5;                    // brighter head segment
    cx.beginPath();
    tr.slice(-4).forEach((p, i) => { const q = proj(p[1], p[0]); i ? cx.lineTo(q.x, q.y) : cx.moveTo(q.x, q.y); });
    cx.lineTo(qh.x, qh.y); cx.stroke();
    cx.globalAlpha = 1;
  }
}

// vessels that went dark: ghost markers pulsing at last known position (the thesis).
// Violet + X-marker: "signal lost" is its own visual language, distinct from the
// blue->amber->red suspicion ramp.
function drawDark() {
  if (!visDark()) return;
  const pulse = 0.5 + 0.5 * Math.sin(performance.now() / 1000 * 2.2);
  cx.font = "10px monospace";
  for (const v of S.dark) {
    const q = proj(v.lo, v.la);
    if (q.x < -20 || q.x > W + 20 || q.y < -20 || q.y > H + 20) continue;
    const c = v.sanc ? "255,45,85" : "180,120,255";
    cx.setLineDash([3, 3]); cx.strokeStyle = `rgba(${c},${0.2 + 0.5 * pulse})`; cx.lineWidth = 1.4;
    cx.beginPath(); cx.arc(q.x, q.y, 7 + 6 * pulse, 0, 7); cx.stroke(); cx.setLineDash([]);
    cx.strokeStyle = `rgba(${c},.95)`; cx.lineWidth = 1.8;
    cx.beginPath();
    cx.moveTo(q.x - 3.5, q.y - 3.5); cx.lineTo(q.x + 3.5, q.y + 3.5);
    cx.moveTo(q.x + 3.5, q.y - 3.5); cx.lineTo(q.x - 3.5, q.y + 3.5);
    cx.stroke();
    cx.fillStyle = `rgba(${c},.95)`;
    cx.fillText("AIS LOST " + Math.round(v.dark_min) + "m", q.x + 9, q.y + 3);
  }
}

function drawVessels() {
  const dim = S.view === "heat" ? 0.4 : 1;
  for (const v of S.vessels) {
    if (!visV(v)) continue;
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
  splat(h.dark, 1.0, 9);            // one stable layer: AIS gaps + loitering + STS
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
    const q = proj(p[1], p[0]), r = p[2] / 111 * cam.scale / Math.cos(p[0] * Math.PI / 180);
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
// force-directed layout: positions persist across polls in NP so the graph keeps
// its shape and gently settles (gravity + repulsion + edge springs).
const NP = {};
function simulateNet() {
  const n = S.net; if (!n || !n.nodes.length) return;
  const nodes = n.nodes, cxp = W / 2, cyp = H / 2;
  const ring = Math.min(W, H) * 0.34;
  nodes.forEach((v, i) => {
    if (!NP[v.mmsi]) {
      const a = i / nodes.length * Math.PI * 2;
      NP[v.mmsi] = { x: cxp + Math.cos(a) * ring + (i % 6), y: cyp + Math.sin(a) * ring + (i % 4), vx: 0, vy: 0 };
    }
    const p = NP[v.mmsi]; p.fx = 0; p.fy = 0;
  });
  // layout scaled to the viewport: spring length from screen size and node count,
  // repulsion to match, gravity just enough to keep the constellation on screen
  const SPR = Math.max(120, Math.min(W, H) / (1.2 * Math.sqrt(nodes.length)));
  const K_REP = SPR * SPR * 1.6, K_SPR = 0.02, GRAV = 0.004, DAMP = 0.82, CAP = 8;
  for (let i = 0; i < nodes.length; i++) {
    const a = NP[nodes[i].mmsi];
    for (let j = i + 1; j < nodes.length; j++) {
      const b = NP[nodes[j].mmsi];
      let dx = a.x - b.x, dy = a.y - b.y, d2 = Math.max(400, dx * dx + dy * dy), d = Math.sqrt(d2), f = K_REP / d2;
      a.fx += f * dx / d; a.fy += f * dy / d; b.fx -= f * dx / d; b.fy -= f * dy / d;
    }
    a.fx += (cxp - a.x) * GRAV; a.fy += (cyp - a.y) * GRAV;
  }
  n.edges.forEach(e => {
    const a = NP[e.a], b = NP[e.b]; if (!a || !b) return;
    let dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1, f = K_SPR * (d - SPR);
    a.fx += f * dx / d; a.fy += f * dy / d; b.fx -= f * dx / d; b.fy -= f * dy / d;
  });
  nodes.forEach(v => {
    const a = NP[v.mmsi]; if (v.mmsi === netDrag) { a.vx = a.vy = 0; return; }
    a.vx = (a.vx + a.fx) * DAMP; a.vy = (a.vy + a.fy) * DAMP;
    const sp = Math.hypot(a.vx, a.vy); if (sp > CAP) { a.vx *= CAP / sp; a.vy *= CAP / sp; }
    a.x = Math.max(24, Math.min(W - 24, a.x + a.vx));
    a.y = Math.max(60, Math.min(H - 26, a.y + a.vy));
  });
}
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
  const active = S.netSel || S.netHover;
  const nb = new Set();
  if (active) n.edges.forEach(e => { if (e.a === active) nb.add(e.b); if (e.b === active) nb.add(e.a); });
  n.edges.forEach(e => {
    const a = NP[e.a], b = NP[e.b]; if (!a || !b) return;
    const on = active && (e.a === active || e.b === active);
    cx.strokeStyle = on ? "rgba(255,190,110,.95)" : (active ? "rgba(255,120,90,.12)" : "rgba(255,120,90,.3)");
    cx.lineWidth = on ? Math.min(6, 1.6 + e.w) : Math.min(3, e.w);
    cx.beginPath(); cx.moveTo(a.x, a.y); cx.lineTo(b.x, b.y); cx.stroke();
  });
  n.nodes.forEach(v => {
    const a = NP[v.mmsi]; if (!a) return;
    const isSel = S.netSel === v.mmsi, isAct = active === v.mmsi, isNb = nb.has(v.mmsi);
    const r = nodeRadius(v), dim = active && !isAct && !isNb ? 0.28 : 1;
    const hot = v.sanc || (v.score != null && v.score >= 0.5);
    cx.globalAlpha = dim;
    if (hot) { cx.beginPath(); cx.arc(a.x, a.y, r + 7, 0, 7); cx.fillStyle = v.sanc ? "rgba(255,45,85,.15)" : "rgba(255,59,48,.15)"; cx.fill(); }
    cx.beginPath(); cx.arc(a.x, a.y, r, 0, 7); cx.fillStyle = netColor(v); cx.fill();
    if (v.sanc) { cx.strokeStyle = "#ff2d55"; cx.lineWidth = 1.6; cx.beginPath(); cx.arc(a.x, a.y, r + 3, 0, 7); cx.stroke(); }
    if (isSel) { cx.strokeStyle = "#dce8f0"; cx.lineWidth = 1.8; cx.beginPath(); cx.arc(a.x, a.y, r + 5, 0, 7); cx.stroke(); }
    const showLabel = isAct || isNb || v.sanc || hot || n.nodes.length <= 26;
    if (showLabel) {
      cx.fillStyle = isAct ? "#eaf2f8" : "#9fb8c8"; cx.font = (isAct ? "11px" : "10px") + " monospace";
      cx.fillText((v.name || v.mmsi) + (v.flag ? "  " + v.flag : ""), a.x + r + 5, a.y + 3);
      if ((isAct || isNb) && v.score != null) {
        cx.fillStyle = "#7fa0b4"; cx.font = "9px monospace";
        cx.fillText((v.score * 100).toFixed(0) + "%", a.x + r + 5, a.y + 15);
      }
    }
    cx.globalAlpha = 1;
  });
  cx.fillStyle = "#5f7f94"; cx.font = "10px monospace";
  cx.fillText(n.nodes.length + " vessels, " + n.edges.length + " rendezvous edges. drag a node, click for its dossier.", 16, H - 16);
}
function nodeAt(px, py) {
  const n = S.net; if (!n) return null;
  let best = null, bd = 1e9;
  for (const v of n.nodes) {
    const a = NP[v.mmsi]; if (!a) continue;
    const d = Math.hypot(a.x - px, a.y - py) - nodeRadius(v);   // edge distance: big nodes = big targets
    if (d < bd) { bd = d; best = v; }
  }
  return bd <= 14 ? best : null;
}
// who this ship met at sea, ranked by encounter count -- the payload of the
// network view: click a node, read its ring, hop to the next vessel
function netPartners(mmsi) {
  const n = S.net; if (!n) return [];
  const out = [];
  n.edges.forEach(e => {
    const other = e.a === mmsi ? e.b : (e.b === mmsi ? e.a : null);
    if (!other) return;
    const v = n.nodes.find(x => x.mmsi === other) || {};
    out.push({ mmsi: other, name: v.name || other, w: e.w, sanc: v.sanc, score: v.score });
  });
  return out.sort((a, b) => b.w - a.w);
}
function selectNode(mmsi) {
  S.netSel = mmsi;
  api("/api/vessel/" + mmsi)
    .catch(() => null)                              // transient fetch fail: still show a card
    .then(d => showDossier(d, netPartners(mmsi)));
  draw();
}
function netHit(px, py) {
  const v = nodeAt(px, py);
  if (v) selectNode(v.mmsi);
  else { S.netSel = null; document.getElementById("dossier").classList.remove("show"); draw(); }
}

// ---- rail + dossier ----
function renderRail() {
  const el = document.getElementById("rail");
  el.innerHTML = S.watch.map(w => {
    const cls = w.dark ? "dark" : (w.sanc ? "sanc" : "");
    const tag = w.dark ? "DARK" : (w.sanc ? "LISTED" : (w.s * 100).toFixed(0) + "%");
    return `<div class="card ${cls}" data-m="${w.m}">
       <div class="h"><span>${esc(w.n)}</span><span class="sc">${tag}</span></div>
       <div class="w">${(w.why || []).map(esc).join(" &middot; ")}</div>
     </div>`; }).join("") ||
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

function showDossier(d, partners) {
  const el = document.getElementById("dossier");
  const meets = (partners && partners.length) ?
    `<div class="sub" style="margin-top:10px;letter-spacing:.14em">SHIP-TO-SHIP RENDEZVOUS</div>
     <div class="rows">` + partners.slice(0, 8).map(p =>
      `<div class="row partner" data-m="${p.mmsi}" style="cursor:pointer">
         <span style="color:${p.sanc ? "#ff2d55" : "#cfe0ec"}">${esc(p.name)}${p.score != null ? ` <i style="color:#7fa0b4;font-style:normal">${(p.score * 100).toFixed(0)}%</i>` : ""}</span>
         <span style="color:#ffb020">${p.w}&times;</span></div>`).join("") + `</div>` : "";
  const wire = () => el.querySelectorAll(".partner").forEach(x =>
    x.onclick = () => selectNode(x.getAttribute("data-m")));
  if (!d || d.error) {                              // node with no record: still show a card
    el.innerHTML = `<div class="x" onclick="document.getElementById('dossier').classList.remove('show')">&times;</div>
      <h2>no dossier on file</h2><div class="sub">this vessel has no live track or registry record yet</div>${meets}`;
    el.classList.add("show"); wire(); return;
  }
  d.links = d.links || {};
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
    ${meets}
    <div style="margin-top:10px">
      <a href="${d.links.marinetraffic}" target="_blank">MarineTraffic</a>
      ${d.links.gfw ? `<a href="${d.links.gfw}" target="_blank">Global Fishing Watch</a>` : ""}
      <a href="${d.links.equasis}" target="_blank">Equasis</a>
    </div>`;
  el.classList.add("show"); wire();
}
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---- polling ----
async function tick() {
  try {
    if (S.view === "net") { S.net = await api("/api/network"); draw(); return; }
    const d = await api("/api/state");
    S.vessels = d.vessels; S.watch = d.watch; S.dark = d.dark || [];
    if (S.sel) S.sel = S.vessels.find(v => v.m === S.sel.m) || S.sel;
    document.getElementById("s-tracked").textContent = d.stats.tracked;
    document.getElementById("s-sanc").textContent = d.stats.sanctioned_live;
    document.getElementById("s-scored").textContent = d.stats.scored;
    const dk = document.getElementById("s-dark"); if (dk) dk.textContent = d.stats.dark || 0;
    const mo = document.getElementById("s-model");
    mo.textContent = d.stats.model ? "MODEL LIVE" : "MODEL OFFLINE";
    mo.style.color = d.stats.model ? "#3aa0ff" : "#4a6579";
    if (S.view === "heat") S.heat = await api("/api/heatmap");
    if (Date.now() - S.trailsAt > 30000) {          // wakes change slowly: refresh every 30s
      S.trailsAt = Date.now();
      api("/api/trails").then(d => { S.trails = d.trails || {}; draw(); });
    }
    renderRail(); draw();
  } catch (e) { /* transient */ }
}

// ---- interaction ----
let drag = null, netDrag = null;
cv.addEventListener("pointerdown", e => {
  if (S.view === "net") {
    const h = nodeAt(e.clientX, e.clientY);
    if (h) { netDrag = h.mmsi; drag = { x: e.clientX, y: e.clientY, moved: 0 }; return; }  // grab a node
  }
  drag = { x: e.clientX, y: e.clientY, cx: cam.cx, my: mercY(cam.cy), moved: 0 };
});
// click vs drag: real pixel distance from pointerdown, not a per-event counter
// (a counter kills clicks -- the mouse always jiggles a couple of move events)
const dragDist = e => Math.hypot(e.clientX - drag.x, e.clientY - drag.y);
cv.addEventListener("pointermove", e => {
  if (netDrag) {                                    // drag a graph node, physics follows
    const p = NP[netDrag]; if (p) { p.x = e.clientX; p.y = e.clientY; p.vx = p.vy = 0; }
    drag.moved = Math.max(drag.moved, dragDist(e)); draw(); return;
  }
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
  drag.moved = Math.max(drag.moved, dragDist(e));
  cam.cx = drag.cx - (e.clientX - drag.x) / cam.scale;
  cam.cy = imercY(drag.my + (e.clientY - drag.y) / cam.scale);
  draw();
});
cv.addEventListener("pointerup", e => {
  if (netDrag) { if (drag && drag.moved < 5) selectNode(netDrag); netDrag = null; drag = null; return; }
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
    if (!visV(v)) continue;                         // invisible boats not clickable
    const q = proj(vlon(v), vlat(v));
    const d = Math.hypot(q.x - px, q.y - py);
    if (d < bd) { bd = d; best = v; }
  }
  return best;
}
function hitTest(px, py) {
  const best = nearestVessel(px, py, 18);           // bigger target on a wide map
  if (best) { focusVessel(best.m, false); return; } // no camera yank on a map click
  const dk = nearestDark(px, py, 22);               // dark ghosts are clickable (the interesting ones)
  if (dk) { S.sel = null; api("/api/vessel/" + dk.m).then(showDossier); draw(); return; }
  S.sel = null; S.trail = []; document.getElementById("dossier").classList.remove("show"); draw();
}
function nearestDark(px, py, tol) {
  let best = null, bd = tol;
  if (!visDark()) return null;
  for (const v of (S.dark || [])) {
    const q = proj(v.lo, v.la);
    const d = Math.hypot(q.x - px, q.y - py);
    if (d < bd) { bd = d; best = v; }
  }
  return best;
}

document.querySelectorAll(".fchip").forEach(c => c.onclick = () => {
  const f = c.getAttribute("data-f");
  S.filter[f] = !S.filter[f];
  c.classList.toggle("on", S.filter[f]);
  draw();
});
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("on"));
  t.classList.add("on");
  S.view = t.getAttribute("data-v");
  document.getElementById("legend").style.display = S.view === "live" ? "block" : "none";
  document.getElementById("heatlegend").style.display = S.view === "heat" ? "block" : "none";
  tick();
});

// ---- live motion: dead-reckon vessels forward between 5s server fixes so the
// fleet glides continuously (reads as live on a screen recording) ----
let _lastT = 0;
function animate(t) {
  requestAnimationFrame(animate);
  if (S.view === "net") { simulateNet(); draw(); _lastT = t; return; }   // force layout
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
  if (moved || (S.dark && S.dark.length)) draw();   // dark ghosts keep pulsing
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
