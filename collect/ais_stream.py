"""aisstream.io websocket reader -> normalized ais_position rows.

Jetstream-style single loop (websockets.sync, no asyncio). Subscribes to the
v1 bounding boxes, keeps a per-MMSI static cache from ShipStaticData so every
position row carries imo / type / draught / destination, and stamps the INGEST
clock (never the client-reported time: velocity and darkness are the signal and
a spoofed clock would poison them).
"""
import glob
import json
import os
import sys
import time

from websockets.sync.client import connect

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in [_here] + sorted(glob.glob("/hopsfs/Users/*/ghost-fleet")):
    if os.path.exists(os.path.join(_p, "ghost_features.py")):
        sys.path.insert(0, _p)
        break
from ghost_features import BBOXES, normalize_position, normalize_static  # noqa: E402

URL = "wss://stream.aisstream.io/v0/stream"


def stream_rows(key, run_seconds, bboxes=BBOXES):
    """Generator of ais_position rows for run_seconds. Reconnects on drop."""
    sub = {"APIKey": key, "BoundingBoxes": bboxes,
           "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}
    static_cache = {}
    deadline = time.time() + run_seconds
    while time.time() < deadline:
        try:
            with connect(URL, open_timeout=30, close_timeout=5,
                         max_size=2 ** 22) as ws:
                ws.send(json.dumps(sub))
                while time.time() < deadline:
                    try:
                        raw = ws.recv(timeout=min(15.0, max(1.0, deadline - time.time())))
                    except TimeoutError:
                        continue
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if "error" in msg:
                        raise RuntimeError(f"aisstream error: {msg['error']}")
                    mt = msg.get("MessageType")
                    now = time.time()
                    if mt == "ShipStaticData":
                        mmsi, static = normalize_static(msg)
                        if mmsi and static:
                            static_cache[mmsi] = static
                    elif mt == "PositionReport":
                        row = normalize_position(msg, now, static_cache)
                        if row:
                            yield row
        except RuntimeError:
            raise
        except Exception as e:
            if time.time() >= deadline:
                break
            print(f"ws reconnect after {type(e).__name__}: {e}", flush=True)
            time.sleep(3)
