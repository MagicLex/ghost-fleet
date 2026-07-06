"""GHOST FLEET scope -- custom Hopsworks app (FastAPI, oceanic canvas).

Thin client of the FTI system:
- live vessels: a server-side aisstream reader (shared collect/ais_stream.py,
  same normalize -- no skew) keeps the current fleet in memory.
- sanctioned + identity + events: loaded from the feature store on start and
  refreshed periodically, for the dossier, the heatmap and the network.
- suspicion score: batch-called from the shadowscorer deployment when it is
  running (the model layer lights up once fleet-train has registered a model).

No heavy model in this pod, no online-store map scans. The map is drawn on a
canvas from a vendored coastline; the store is read in batch and cached, never
scanned per request.
"""
import asyncio
import glob
import json
import math
import os
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


def _find_root():
    cand = Path(__file__).resolve().parents[1]
    for p in [cand] + [Path(g) for g in sorted(glob.glob("/hopsfs/Users/*/ghost-fleet"))]:
        if (p / "ghost_features.py").exists():
            return p
    raise RuntimeError("repo root not found")


ROOT = _find_root()
sys.path.insert(0, str(ROOT))
from collect.ais_stream import stream_rows  # noqa: E402
from ghost_features import (FOC_FLAGS, STS_HOTSPOTS, haversine_km,  # noqa: E402
                            reasons)

BASE = os.environ.get("APP_BASE_URL_PATH", "").rstrip("/")
STATIC = ROOT / "app" / "static"

VESSELS = {}                       # mmsi -> latest row
TRAILS = {}                        # mmsi -> deque[(lat,lon)]
TRAIL_LEN = 120
SCORE = {}                         # mmsi -> {"score", "reasons", "flag"}
ENRICH = {"sanctioned": {}, "identity": {}, "events": [], "at": 0.0}
HEAT = {"corridor": [], "dark": [], "at": 0.0}
NETWORK = {"nodes": [], "edges": [], "at": 0.0}
_dep = {"handle": None, "checked": 0.0}
STATS = {"msgs": 0, "at": 0.0}


# --- feature-store enrichment (batch read, cached) ----------------------
def _refresh_enrichment():
    try:
        import hopsworks
        fs = hopsworks.login().get_feature_store()

        def read(n):
            try:
                return fs.get_feature_group(n, version=1).read()
            except Exception:
                import pandas as pd
                return pd.DataFrame()

        san = read("sanctioned_vessel")
        ident = read("vessel_identity")
        ev = read("gfw_event")
        ENRICH["sanctioned"] = {str(r["imo"]): {"name": r.get("vessel_name"),
                                "flags": r.get("flags"), "programs": r.get("programs")}
                                for _, r in san.iterrows()} if not san.empty else {}
        ENRICH["identity"] = {str(r["mmsi"]): r.to_dict()
                              for _, r in ident.iterrows()} if not ident.empty else {}
        ENRICH["events"] = ev.to_dict("records") if not ev.empty else []
        _rebuild_heat_and_network()
        ENRICH["at"] = time.time()
        print(f"enrichment: {len(ENRICH['sanctioned'])} sanctioned, "
              f"{len(ENRICH['identity'])} identities, {len(ENRICH['events'])} events",
              flush=True)
    except Exception as e:
        print(f"enrichment refresh failed: {e}", flush=True)


def _rebuild_heat_and_network():
    """Dark/STS hotspots from GFW loitering+encounter positions; corridors from
    sanctioned/high-score vessels' live positions; network from encounter edges."""
    dark = []
    edges, nodes = {}, {}
    for e in ENRICH["events"]:
        et = e.get("event_type")
        if e.get("lat") and e.get("lon") and et in ("loitering", "encounter", "gap"):
            w = {"encounter": 3.0, "loitering": 1.5, "gap": 2.0}.get(et, 1.0)
            dark.append([round(float(e["lat"]), 3), round(float(e["lon"]), 3), w])
        if et == "encounter":
            a, b = str(e.get("vessel_mmsi") or ""), str(e.get("counterpart_mmsi") or "")
            if a and b:
                k = tuple(sorted((a, b)))
                edges[k] = edges.get(k, 0) + 1
                for m in (a, b):
                    nodes[m] = nodes.get(m, 0) + 1
    HEAT["dark"] = dark
    NETWORK["nodes"] = [{"mmsi": m, "deg": d,
                         "sanc": _is_sanctioned_mmsi(m)} for m, d in nodes.items()]
    NETWORK["edges"] = [{"a": a, "b": b, "w": w} for (a, b), w in edges.items()]
    NETWORK["at"] = time.time()


def _is_sanctioned_mmsi(mmsi):
    idn = ENRICH["identity"].get(str(mmsi)) or {}
    imo = str(idn.get("imo") or "")
    return bool(imo and imo in ENRICH["sanctioned"])


def _corridor():
    """Live positions of sanctioned / high-score vessels = where the shadow
    fleet actually transits (weighted by suspicion)."""
    pts = []
    now = time.time()
    for m, r in VESSELS.items():
        if now - r["ts"] > 300:
            continue
        s = (SCORE.get(m) or {}).get("score")
        w = s if s is not None else (0.8 if _sanctioned_row(r) else 0.0)
        if w and w > 0.4:
            pts.append([round(r["lat"], 3), round(r["lon"], 3), round(float(w), 2)])
    return pts


def _sanctioned_row(r):
    imo = str(r.get("imo") or "")
    return bool(imo and imo in ENRICH["sanctioned"])


# --- live AIS reader -----------------------------------------------------
async def ais_loop():
    import hopsworks
    key = hopsworks.get_secrets_api().get_secret("AISSTREAM_KEY").value
    loop = asyncio.get_event_loop()

    def run():
        for row in stream_rows(key, run_seconds=10 ** 9):
            mmsi = row["mmsi"]
            VESSELS[mmsi] = row
            STATS["msgs"] += 1
            STATS["at"] = time.time()
            t = TRAILS.get(mmsi)
            if t is None:
                t = TRAILS[mmsi] = deque(maxlen=TRAIL_LEN)
            if not t or (t[-1][0], t[-1][1]) != (round(row["lat"], 4), round(row["lon"], 4)):
                t.append((round(row["lat"], 4), round(row["lon"], 4)))
    while True:
        try:
            await asyncio.to_thread(run)
        except Exception as e:
            print(f"ais_loop reconnect: {e}", flush=True)
            await asyncio.sleep(5)


async def score_loop():
    """Batch-score the live fleet through shadowscorer when it is up."""
    while True:
        await asyncio.sleep(15)
        try:
            now = time.time()
            if _dep["handle"] is None:
                if now - _dep["checked"] < 120:
                    continue
                _dep["checked"] = now
                import hopsworks
                dep = hopsworks.login().get_model_serving().get_deployment("shadowscorer")
                if dep is None or not dep.is_running():
                    continue
                _dep["handle"] = dep
                print("score_loop: shadowscorer attached", flush=True)
            insts = [{"mmsi": m} for m, r in VESSELS.items() if now - r["ts"] <= 300]
            for i in range(0, len(insts), 200):
                preds = await asyncio.to_thread(
                    _dep["handle"].predict, inputs=insts[i:i + 200])
                for p in preds.get("predictions", []):
                    if p.get("mmsi") and "score" in p:
                        SCORE[str(p["mmsi"])] = {"score": p["score"],
                                                 "reasons": p.get("reasons", []),
                                                 "flag": p.get("flag", "")}
        except Exception as e:
            _dep["handle"] = None
            print(f"score_loop: {e}", flush=True)


async def enrich_loop():
    while True:
        await asyncio.to_thread(_refresh_enrichment)
        await asyncio.sleep(600)


# --- API -----------------------------------------------------------------
app = FastAPI()
app.add_middleware(GZipMiddleware, minimum_size=2048)


@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC / "index.html").read_text()
    v = str(int((STATIC / "app.js").stat().st_mtime))
    html = html.replace("app.js", f"app.js?v={v}")
    return HTMLResponse(html.replace("__BASE__", BASE),
                        headers={"Cache-Control": "no-cache"})


@app.get("/static/{name}")
def static(name: str):
    p = STATIC / name
    if not p.is_file() or "/" in name or name.startswith("."):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


@app.get("/api/state")
def state():
    now = time.time()
    out = []
    for m, r in VESSELS.items():
        if now - r["ts"] > 300:
            continue
        sc = SCORE.get(m) or {}
        sanc = _sanctioned_row(r)
        foc = (r.get("flag") or "") in FOC_FLAGS
        out.append({
            "m": m, "n": (r.get("ship_name") or "")[:24], "im": r.get("imo") or "",
            "la": round(r["lat"], 4), "lo": round(r["lon"], 4),
            "sog": r.get("sog"), "cog": r.get("cog"), "fl": r.get("flag") or "",
            "s": sc.get("score"), "sanc": 1 if sanc else 0, "foc": 1 if foc else 0,
            "age": round(now - r["ts"], 1),
        })
    return {"t": now, "vessels": out, "watch": _watch(now),
            "stats": {"tracked": len(out),
                      "scored": sum(1 for v in out if v["s"] is not None),
                      "sanctioned_live": sum(1 for v in out if v["sanc"]),
                      "msg_age": round(now - STATS["at"], 1) if STATS["at"] else None,
                      "model": _dep["handle"] is not None}}


def _watch(now):
    """Ranked attention rail: sanctioned first, then high model score, plain words."""
    w = []
    for m, r in VESSELS.items():
        if now - r["ts"] > 300:
            continue
        sc = SCORE.get(m) or {}
        s = sc.get("score") or 0
        sanc = _sanctioned_row(r)
        why = list(sc.get("reasons") or [])
        score = (1000 if sanc else 0) + 100 * s
        if sanc:
            why = ["on an international sanctions list"] + why
        elif (r.get("flag") or "") in FOC_FLAGS:
            why = why + [f"flag of convenience ({r.get('flag')})"]
        if sanc or s >= 0.5:
            w.append({"m": m, "n": (r.get("ship_name") or m)[:24],
                      "la": round(r["lat"], 3), "lo": round(r["lon"], 3),
                      "s": round(s, 3), "sanc": 1 if sanc else 0,
                      "why": why[:3], "score": round(score, 1)})
    w.sort(key=lambda x: -x["score"])
    return w[:14]


@app.get("/api/heatmap")
def heatmap():
    return {"dark": HEAT["dark"], "corridor": _corridor(),
            "hotspots": [[la, lo, r] for la, lo, r in STS_HOTSPOTS]}


@app.get("/api/network")
def network():
    return {"nodes": NETWORK["nodes"], "edges": NETWORK["edges"], "at": NETWORK["at"]}


@app.get("/api/trail/{mmsi}")
def trail(mmsi: str):
    if not mmsi.isdigit() or len(mmsi) > 12:
        return JSONResponse({"error": "bad mmsi"}, status_code=400)
    return {"trail": [[la, lo] for la, lo in TRAILS.get(mmsi, [])]}


@app.get("/api/vessel/{mmsi}")
def vessel(mmsi: str):
    if not mmsi.isdigit() or len(mmsi) > 12:
        return JSONResponse({"error": "bad mmsi"}, status_code=400)
    r = VESSELS.get(mmsi) or {}
    idn = ENRICH["identity"].get(mmsi) or {}
    imo = str(r.get("imo") or idn.get("imo") or "")
    sanc = ENRICH["sanctioned"].get(imo)
    sc = SCORE.get(mmsi) or {}
    evs = [e for e in ENRICH["events"]
           if str(e.get("vessel_mmsi")) == mmsi or str(e.get("counterpart_mmsi")) == mmsi]
    return {
        "mmsi": mmsi, "imo": imo, "name": r.get("ship_name") or idn.get("shipname"),
        "flag": r.get("flag") or idn.get("flag"), "type": idn.get("ship_type"),
        "built_year": idn.get("built_year"), "gross_tonnage": idn.get("gross_tonnage"),
        "destination": r.get("destination"), "draught": r.get("draught"),
        "score": sc.get("score"), "reasons": sc.get("reasons") or [],
        "sanctioned": bool(sanc), "sanction": sanc,
        "events": [{"type": e.get("event_type"), "start": str(e.get("start_ts")),
                    "lat": e.get("lat"), "lon": e.get("lon"),
                    "dist_km": e.get("distance_km")} for e in evs[:20]],
        "links": {
            "marinetraffic": f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi}",
            "equasis": "https://www.equasis.org/",
            "gfw": (f"https://globalfishingwatch.org/map?vesselId=&query={imo}" if imo else None),
        },
    }


@app.get("/health")
def health():
    return {"ok": True, "tracked": len(VESSELS), "msgs": STATS["msgs"]}


async def _lifespan(_):
    tasks = [asyncio.create_task(ais_loop()), asyncio.create_task(score_loop()),
             asyncio.create_task(enrich_loop())]
    yield
    for t in tasks:
        t.cancel()


asgi = FastAPI(lifespan=asynccontextmanager(_lifespan))
asgi.mount(BASE or "/", app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(asgi, host="0.0.0.0", port=int(os.environ.get("APP_PORT", 8000)))
