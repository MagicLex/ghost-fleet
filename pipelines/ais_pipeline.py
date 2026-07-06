"""F1 AIS collector job.

Streams live AIS from aisstream.io over the v1 bounding boxes for RUN_MINUTES,
inserting into the online-enabled `ais_position` FG. RonDB keeps the latest
position per vessel (serving lookups); the offline store accumulates the full
track history that the behaviour features and training consume.

High-frequency insert discipline (the sky/cascade scars):
  - batch rows and flush on a BACKGROUND thread so the ws loop never blocks
    (Delta commit cost grows with commit count),
  - materialize offline every other flush,
  - statistics_config=False on this hot FG.

Single-writer discipline: RUN_MINUTES must be < the cron interval and the job
cron-aligned, or two overlapping executions corrupt the Delta table.

    RUN_MINUTES=2 -> short smoke run.
"""
import glob
import os
import sys
import threading
import time

import pandas as pd

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in [_here] + sorted(glob.glob("/hopsfs/Users/*/ghost-fleet")):
    if os.path.exists(os.path.join(_p, "ghost_features.py")):
        ROOT = _p
        sys.path.insert(0, _p)
        break
from collect.ais_stream import stream_rows  # noqa: E402
from ghost_features import POSITION_COLUMNS  # noqa: E402

# run length: argv[1] (job --args) overrides env overrides default. Keep it
# STRICTLY less than the cron interval so two executions never write at once
# (concurrent writers corrupt the Delta table, the cascade scar).
RUN_MINUTES = float((sys.argv[1] if len(sys.argv) > 1 else None)
                    or os.environ.get("RUN_MINUTES", "45"))
FLUSH_SECONDS = 45.0
FLUSH_ROWS = 4000

STR_COLS = ["mmsi", "imo", "ship_name", "ship_type", "flag", "destination", "bbox"]
NUM_COLS = ["lat", "lon", "sog", "cog", "heading", "nav_status", "draught"]


def _to_frame(rows):
    df = pd.DataFrame(rows, columns=POSITION_COLUMNS)
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    for c in STR_COLS:
        df[c] = df[c].fillna("").astype(str)
    return df


def main():
    import hopsworks
    proj = hopsworks.login()
    key = hopsworks.get_secrets_api().get_secret("AISSTREAM_KEY").value
    fs = proj.get_feature_store()
    fg = fs.get_or_create_feature_group(
        name="ais_position", version=1,
        description="Live AIS positions over Baltic + Gulf of Finland + Laconian "
                    "Gulf (aisstream.io), ingest-clock stamped, enriched with "
                    "last-seen static (imo, type, draught, destination). Online = "
                    "latest per MMSI for serving; offline = track history.",
        primary_key=["mmsi"], event_time="ts",
        online_enabled=True, statistics_config=False)

    state = {"rows": 0, "failed": 0, "flushes": 0}
    lock = threading.Lock()
    worker = [None]

    def _flush(rows, materialize):
        t0 = time.time()
        try:
            fg.insert(_to_frame(rows),
                      write_options={"start_offline_materialization": materialize})
            with lock:
                state["rows"] += len(rows)
            print(f"flush {len(rows)} rows: insert {time.time()-t0:.1f}s "
                  f"(total {state['rows']:,})", flush=True)
        except Exception as e:
            with lock:
                state["failed"] += 1
            print(f"flush failed ({type(e).__name__}: {e})", flush=True)

    buf = []
    last = time.time()
    for row in stream_rows(key, RUN_MINUTES * 60):
        buf.append(row)
        busy = worker[0] is not None and worker[0].is_alive()
        if not busy and buf and (len(buf) >= FLUSH_ROWS or time.time() - last >= FLUSH_SECONDS):
            state["flushes"] += 1
            worker[0] = threading.Thread(
                target=_flush, args=(buf[:], state["flushes"] % 2 == 1))
            worker[0].start()
            buf, last = [], time.time()
        if state["failed"] >= 5:
            raise RuntimeError("5 flushes failed")

    if worker[0] is not None:
        worker[0].join(timeout=300)
    if buf:
        state["flushes"] += 1
        _flush(buf, True)
    if state["rows"] == 0:
        raise RuntimeError("no AIS rows inserted")
    print(f"run done: {state['rows']:,} rows, {state['flushes']} flushes", flush=True)


if __name__ == "__main__":
    main()
