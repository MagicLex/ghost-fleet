"""Shared, skew-free feature logic for ghost-fleet.

ONE module imported by:
  - the AIS collector      (normalize_position / normalize_static)
  - the feature pipeline    (featurize_vessel -> vessel_track_features)
  - training                (featurize_vessel over history)
  - the KServe predictor    (featurize_vessel on a live track + reasons)

so a feature is computed the same way at train and serve time. No skew.

The behaviour signal is deliberately honest: from our own receiver we cannot
prove a vessel switched AIS off (non-reception != transmitter off), so true
"dark" comes from GFW gap events; our AIS carries speed, loitering, draught and
rendezvous-proximity. Everything degrades gracefully when GFW events or identity
are absent (v1 before the GFW token), so the pipeline runs on AIS alone.
"""
import math

# --- v1 theatres ---------------------------------------------------------
# aisstream BoundingBoxes: each box is [[lat_sw, lon_sw], [lat_ne, lon_ne]].
BBOXES = [
    [[53.5, 9.0], [60.7, 30.5]],    # Baltic Sea + Gulf of Finland (Russian crude)
    [[35.6, 22.0], [37.2, 24.2]],   # Laconian Gulf, Greece (Mediterranean STS hotspot)
]

# Known offshore ship-to-ship / loitering zones (lat, lon, radius_km). A vessel
# sitting slow inside one of these is far more suspicious than one in a port.
STS_HOTSPOTS = [
    (36.35, 22.75, 40.0),   # Laconian Gulf anchorage
    (59.45, 24.20, 25.0),   # Gulf of Finland off Tallinn/Naissaar
    (57.90, 20.00, 40.0),   # Baltic Proper off Gotland (Irbe/eastern)
    (55.30, 15.20, 35.0),   # off Bornholm
]

# Flags of convenience / shadow-fleet re-flagging destinations (ISO-3166 alpha-2).
FOC_FLAGS = {"PA", "LR", "MH", "CK", "GA", "PW", "CM", "BB", "VC", "KM",
             "TZ", "MN", "TG", "SL", "DJ", "GW", "HN", "BZ"}

# MMSI Maritime Identification Digits (first 3) -> ISO alpha-2. Reference table,
# the maritime-flag subset that shows up in the Baltic / shadow fleet; GFW gives
# the authoritative flag, this is the AIS-only fallback.
MID_FLAG = {
    "273": "RU", "230": "FI", "265": "SE", "266": "SE", "276": "EE",
    "275": "LV", "277": "LT", "211": "DE", "218": "DE", "219": "DK",
    "220": "DK", "257": "NO", "258": "NO", "259": "NO", "244": "NL",
    "245": "NL", "246": "NL", "232": "GB", "233": "GB", "234": "GB",
    "235": "GB", "227": "FR", "228": "FR", "247": "IT", "237": "GR",
    "239": "GR", "240": "GR", "241": "GR", "351": "PA", "352": "PA",
    "353": "PA", "354": "PA", "355": "PA", "356": "PA", "357": "PA",
    "370": "PA", "371": "PA", "372": "PA", "373": "PA", "636": "LR",
    "637": "LR", "538": "MH", "518": "CK", "626": "GA", "511": "PW",
    "613": "CM", "314": "BB", "375": "VC", "376": "VC", "377": "VC",
    "616": "KM", "677": "TZ", "674": "MN", "671": "TG", "667": "SL",
    "621": "DJ", "630": "GW", "334": "HN", "312": "BZ", "525": "ID",
    "563": "SG", "564": "SG", "565": "SG", "412": "CN", "413": "CN",
    "477": "HK", "422": "IR", "425": "IR", "470": "AE", "471": "AE",
}

# AIS ship-type code -> coarse group (tankers are the shadow-fleet story).
def ship_type_group(code):
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "unknown"
    if 80 <= c <= 89:
        return "tanker"
    if 70 <= c <= 79:
        return "cargo"
    if 60 <= c <= 69:
        return "passenger"
    if 30 <= c <= 59:
        return "service"
    return "other"


def flag_from_mmsi(mmsi):
    return MID_FLAG.get(str(mmsi)[:3], "")


# --- AIS message normalization (aisstream.io) ----------------------------
# aisstream wraps each message as {"MessageType": ..., "MetaData": {...},
# "Message": {"<Type>": {...}}}. We stamp the INGEST clock, never client time.
POSITION_COLUMNS = [
    "mmsi", "imo", "ship_name", "ship_type", "flag",
    "lat", "lon", "sog", "cog", "heading", "nav_status",
    "draught", "destination", "bbox", "ts",
]


def normalize_position(wrapper, ingest_ts, static_cache=None):
    """aisstream PositionReport -> one ais_position row, enriched with the last
    seen static data for that MMSI (imo/name/type/draught/destination)."""
    md = wrapper.get("MetaData") or {}
    rep = (wrapper.get("Message") or {}).get("PositionReport") or {}
    mmsi = str(md.get("MMSI") or rep.get("UserID") or "").strip()
    lat = md.get("latitude", rep.get("Latitude"))
    lon = md.get("longitude", rep.get("Longitude"))
    if not mmsi or lat is None or lon is None:
        return None
    st = (static_cache or {}).get(mmsi, {})
    return {
        "mmsi": mmsi,
        "imo": str(st.get("imo", "") or ""),
        "ship_name": st.get("ship_name", "") or (md.get("ShipName", "") or "").strip(),
        "ship_type": st.get("ship_type", ""),
        "flag": st.get("flag", "") or flag_from_mmsi(mmsi),
        "lat": float(lat),
        "lon": float(lon),
        "sog": _num(rep.get("Sog")),
        "cog": _num(rep.get("Cog")),
        "heading": _num(rep.get("TrueHeading")),
        "nav_status": _num(rep.get("NavigationalStatus")),
        "draught": _num(st.get("draught")),
        "destination": st.get("destination", ""),
        "bbox": _which_bbox(float(lat), float(lon)),
        "ts": ingest_ts,
    }


def normalize_static(wrapper):
    """aisstream ShipStaticData -> {mmsi: {static fields}} to feed the cache."""
    md = wrapper.get("MetaData") or {}
    s = (wrapper.get("Message") or {}).get("ShipStaticData") or {}
    mmsi = str(md.get("MMSI") or s.get("UserID") or "").strip()
    if not mmsi:
        return None, None
    imo = s.get("ImoNumber") or s.get("IMONumber")
    return mmsi, {
        "imo": str(imo) if imo else "",
        "ship_name": (s.get("Name") or "").strip(),
        "ship_type": s.get("Type", ""),
        "flag": flag_from_mmsi(mmsi),
        "draught": _num(s.get("MaximumStaticDraught")),
        "destination": (s.get("Destination") or "").strip(),
    }


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _which_bbox(lat, lon):
    for i, ((la1, lo1), (la2, lo2)) in enumerate(BBOXES):
        if la1 <= lat <= la2 and lo1 <= lon <= lo2:
            return "laconian" if i == 1 else "baltic"
    return "other"


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _in_hotspot(lat, lon):
    for hlat, hlon, hr in STS_HOTSPOTS:
        if haversine_km(lat, lon, hlat, hlon) <= hr:
            return True
    return False


# --- the model features (MITs), skew-free -------------------------------
# Output is a flat, lowercase dict (Hopsworks lowercases feature names). Every
# key is a column of vessel_track_features. Absent GFW events / identity leave
# their fields at neutral defaults so the pipeline runs on AIS alone.
FEATURE_COLUMNS = [
    "n_positions", "span_hours", "avg_sog", "p95_sog", "frac_slow",
    "n_loiter", "loiter_hours", "draught_mean", "draught_std",
    "n_draught_changes", "n_destinations", "n_destination_changes",
    "frac_in_sts_hotspot", "max_recv_gap_hours",
    "gfw_gaps", "gfw_gap_hours", "gfw_loitering", "gfw_encounters",
    "gfw_port_visits", "n_flags", "flag_is_foc", "age_years",
    "gross_tonnage", "is_tanker",
]

SLOW_KT = 1.0          # under this = effectively stopped / loitering
LOITER_MIN = 60.0      # a loiter episode = slow + offshore for this long


def featurize_vessel(positions, events=None, identity=None):
    """positions: list[dict] (or DataFrame.to_dict('records')) sorted by ts,
    each with lat/lon/sog/draught/destination/ts (ts = epoch seconds or
    pandas Timestamp). events: list of GFW event dicts. identity: dict.
    Returns a flat feature dict (FEATURE_COLUMNS)."""
    pos = _as_records(positions)
    f = {c: 0.0 for c in FEATURE_COLUMNS}
    if not pos:
        return f
    pos.sort(key=lambda r: _epoch(r.get("ts")))
    ts = [_epoch(r.get("ts")) for r in pos]
    sog = [r.get("sog") for r in pos]
    f["n_positions"] = float(len(pos))
    f["span_hours"] = round((ts[-1] - ts[0]) / 3600.0, 3)

    valid_sog = [s for s in sog if s is not None]
    if valid_sog:
        f["avg_sog"] = round(sum(valid_sog) / len(valid_sog), 3)
        f["p95_sog"] = round(_pct(valid_sog, 95), 3)
        f["frac_slow"] = round(sum(1 for s in valid_sog if s < SLOW_KT) / len(valid_sog), 4)

    # loiter episodes: contiguous slow + offshore runs longer than LOITER_MIN
    f["n_loiter"], f["loiter_hours"] = _loiter(pos, ts, sog)

    draughts = [r.get("draught") for r in pos if r.get("draught")]
    if draughts:
        f["draught_mean"] = round(sum(draughts) / len(draughts), 3)
        f["draught_std"] = round(_std(draughts), 3)
        f["n_draught_changes"] = float(_changes(draughts, 0.5))

    dests = [(r.get("destination") or "").strip().upper() for r in pos]
    dests = [d for d in dests if d]
    f["n_destinations"] = float(len(set(dests)))
    f["n_destination_changes"] = float(_str_changes(dests))

    in_hot = [1 for r in pos if _in_hotspot(r["lat"], r["lon"])]
    f["frac_in_sts_hotspot"] = round(len(in_hot) / len(pos), 4)

    gaps_h = [(ts[i] - ts[i - 1]) / 3600.0 for i in range(1, len(ts))]
    f["max_recv_gap_hours"] = round(max(gaps_h), 3) if gaps_h else 0.0

    # GFW events (authoritative dark/rendezvous), when present
    for ev in (events or []):
        et = (ev.get("type") or ev.get("event_type") or "").lower()
        if "gap" in et:
            f["gfw_gaps"] += 1
            f["gfw_gap_hours"] += float(ev.get("duration_hours", 0) or 0)
        elif "loiter" in et:
            f["gfw_loitering"] += 1
        elif "encounter" in et:
            f["gfw_encounters"] += 1
        elif "port" in et:
            f["gfw_port_visits"] += 1

    # identity (GFW), when present
    idn = identity or {}
    flags = set(f_ for f_ in (idn.get("flags") or []) if f_)
    flags |= {r["flag"] for r in pos if r.get("flag")}
    f["n_flags"] = float(len(flags)) if flags else 1.0
    f["flag_is_foc"] = 1.0 if (flags & FOC_FLAGS) else 0.0
    by = idn.get("built_year")
    if by:
        f["age_years"] = max(0.0, float(idn.get("as_of_year", 2026)) - float(by))
    f["gross_tonnage"] = float(idn.get("gross_tonnage") or 0.0)
    tg = idn.get("ship_type_group") or ship_type_group(_mode([r.get("ship_type") for r in pos]))
    f["is_tanker"] = 1.0 if tg == "tanker" else 0.0
    return f


def reasons(feats, flags_seen=None):
    """Plain-word evidence for the dossier, numbers not codes."""
    out = []
    if feats.get("gfw_encounters", 0) >= 1:
        out.append(f"{int(feats['gfw_encounters'])} ship-to-ship rendezvous at sea")
    if feats.get("gfw_gaps", 0) >= 1:
        h = feats.get("gfw_gap_hours", 0)
        out.append(f"went dark {int(feats['gfw_gaps'])}x (AIS off {h:.0f}h total)")
    if feats.get("n_loiter", 0) >= 1:
        out.append(f"{int(feats['n_loiter'])} offshore loitering episodes "
                   f"({feats.get('loiter_hours', 0):.0f}h)")
    if feats.get("n_flags", 0) >= 2:
        fl = f" ({' -> '.join(flags_seen)})" if flags_seen else ""
        out.append(f"flag changed {int(feats['n_flags'])-1}x{fl}")
    if feats.get("n_draught_changes", 0) >= 2:
        out.append(f"draught swung laden<->ballast {int(feats['n_draught_changes'])}x")
    if feats.get("frac_in_sts_hotspot", 0) >= 0.3:
        out.append(f"{feats['frac_in_sts_hotspot']*100:.0f}% of time in a known STS zone")
    if feats.get("is_tanker", 0) and feats.get("age_years", 0) >= 15:
        out.append(f"aging tanker ({int(feats['age_years'])} yr)")
    return out


# --- small numeric helpers ----------------------------------------------
def _as_records(x):
    if hasattr(x, "to_dict"):
        return x.to_dict("records")
    return list(x or [])


def _epoch(ts):
    if ts is None:
        return 0.0
    if hasattr(ts, "timestamp"):
        return ts.timestamp()
    return float(ts)


def _pct(vals, p):
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _std(vals):
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


def _changes(vals, thresh):
    return sum(1 for i in range(1, len(vals)) if abs(vals[i] - vals[i - 1]) >= thresh)


def _str_changes(vals):
    return sum(1 for i in range(1, len(vals)) if vals[i] != vals[i - 1])


def _loiter(pos, ts, sog):
    n, total_h, run_start = 0, 0.0, None
    for i, r in enumerate(pos):
        slow = sog[i] is not None and sog[i] < SLOW_KT and _in_hotspot(r["lat"], r["lon"])
        if slow and run_start is None:
            run_start = ts[i]
        elif not slow and run_start is not None:
            dur = (ts[i - 1] - run_start) / 60.0
            if dur >= LOITER_MIN:
                n += 1
                total_h += dur / 60.0
            run_start = None
    if run_start is not None:
        dur = (ts[-1] - run_start) / 60.0
        if dur >= LOITER_MIN:
            n += 1
            total_h += dur / 60.0
    return float(n), round(total_h, 3)


def _mode(vals):
    vals = [v for v in vals if v not in (None, "", 0)]
    if not vals:
        return None
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get)
