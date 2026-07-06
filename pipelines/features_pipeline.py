"""F2 vessel behaviour feature pipeline.

Reads the raw FGs (ais_position history, gfw_event, vessel_identity), computes
the per-vessel behaviour features with the SHARED extractor (same code training
and serving use, no skew), and writes `vessel_track_features` keyed by MMSI.

event_time = the vessel's last activity, so re-inserting a grown track converges
(latest write wins) rather than leaving a partial row stuck (playbook rule).

Scheduled every 30 min.
"""
import glob
import os
import sys

import pandas as pd

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in [_here] + sorted(glob.glob("/hopsfs/Users/*/ghost-fleet")):
    if os.path.exists(os.path.join(_p, "ghost_features.py")):
        sys.path.insert(0, _p)
        break
from ghost_features import (FEATURE_COLUMNS, featurize_vessel,  # noqa: E402
                            ship_type_group)

LOOKBACK_DAYS = int(os.environ.get("FEAT_LOOKBACK_DAYS", "30"))
MIN_POSITIONS = int(os.environ.get("FEAT_MIN_POSITIONS", "3"))

_TYPE_MAP = {"TANKER": "tanker", "CARGO": "cargo", "PASSENGER": "passenger",
             "FISHING": "service", "TUG": "service", "SEISMIC_VESSEL": "service"}


def _read(fs, name):
    try:
        return fs.get_feature_group(name, version=1).read()
    except Exception as e:
        print(f"{name} empty/absent ({type(e).__name__})", flush=True)
        return pd.DataFrame()


def main():
    import hopsworks
    proj = hopsworks.login()
    fs = proj.get_feature_store()

    pos = _read(fs, "ais_position")
    if pos.empty:
        raise RuntimeError("ais_position empty, nothing to featurize")
    cutoff = pd.Timestamp.utcnow() - pd.Timedelta(days=LOOKBACK_DAYS)
    pos = pos[pd.to_datetime(pos["ts"], utc=True) >= cutoff]
    events = _read(fs, "gfw_event")
    identity = _read(fs, "vessel_identity")
    ident_by_mmsi = {str(r["mmsi"]): r for _, r in identity.iterrows()} if not identity.empty else {}

    ev_by_mmsi = {}
    if not events.empty:
        for _, e in events.iterrows():
            for k in (str(e.get("vessel_mmsi") or ""), str(e.get("counterpart_mmsi") or "")):
                if k:
                    ev_by_mmsi.setdefault(k, []).append(
                        {"type": e.get("event_type"),
                         "duration_hours": e.get("duration_hours")})

    year = pd.Timestamp.utcnow().year
    rows = []
    for mmsi, grp in pos.groupby("mmsi"):
        if len(grp) < MIN_POSITIONS:
            continue
        grp = grp.sort_values("ts")
        recs = grp.to_dict("records")
        idn_row = ident_by_mmsi.get(str(mmsi))
        identity_arg = None
        flags_seen = {r.get("flag") for r in recs if r.get("flag")}
        imo = _mode_nonempty([r.get("imo") for r in recs])
        if idn_row is not None:
            if idn_row.get("flag"):
                flags_seen.add(idn_row["flag"])
            imo = imo or (str(idn_row.get("imo")) if idn_row.get("imo") else "")
            stg = _TYPE_MAP.get(str(idn_row.get("ship_type", "")).upper())
            identity_arg = {
                "flags": list(flags_seen),
                "built_year": idn_row.get("built_year"),
                "as_of_year": year,
                "gross_tonnage": idn_row.get("gross_tonnage"),
                "ship_type_group": stg,
            }
        else:
            identity_arg = {"flags": list(flags_seen), "as_of_year": year}

        feats = featurize_vessel(recs, ev_by_mmsi.get(str(mmsi)), identity_arg)
        last = recs[-1]
        rows.append({
            "mmsi": str(mmsi),
            "imo": str(imo or ""),
            "ship_name": (last.get("ship_name") or "")[:120],
            "flag": last.get("flag") or "",
            "ship_type_grp": _grp_of(idn_row, recs),
            "last_lat": float(last["lat"]), "last_lon": float(last["lon"]),
            "last_ts": pd.to_datetime(last["ts"], utc=True),
            **{c: feats[c] for c in FEATURE_COLUMNS},
        })

    if not rows:
        raise RuntimeError("no vessels met MIN_POSITIONS")
    df = pd.DataFrame(rows)
    fg = fs.get_or_create_feature_group(
        name="vessel_track_features", version=1,
        description="Per-vessel behaviour features (dark time, loitering, STS "
                    "rendezvous, flag-hopping, draught swings) from the shared "
                    "extractor. Keyed by MMSI, carries best-known IMO for the "
                    "sanctions label join. Convergent on last activity.",
        primary_key=["mmsi"], event_time="last_ts",
        online_enabled=True, statistics_config=False)
    fg.insert(df, write_options={"start_offline_materialization": True})
    print(f"inserted {len(df)} vessel feature rows", flush=True)


def _grp_of(idn_row, recs):
    if idn_row is not None and idn_row.get("ship_type"):
        g = _TYPE_MAP.get(str(idn_row["ship_type"]).upper())
        if g:
            return g
    codes = [r.get("ship_type") for r in recs if r.get("ship_type")]
    return ship_type_group(codes[0]) if codes else "unknown"


def _mode_nonempty(vals):
    vals = [str(v) for v in vals if v not in (None, "", "nan")]
    if not vals:
        return ""
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get)


if __name__ == "__main__":
    main()
