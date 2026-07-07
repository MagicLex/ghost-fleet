"""F3 GFW identity + events pipeline.

Pulls Global Fishing Watch v3 events (encounters, loitering, AIS gaps, port
visits) over the v1 theatres for the last LOOKBACK_DAYS, plus vessel identity
for the vessels involved. Writes:
  - gfw_event      : the derived behaviour edges (rendezvous, dark gaps, ...)
  - vessel_identity: mmsi -> imo / flag / ship type / built year / tonnage

GFW is authoritative for "dark" (global reception) and for STS encounters, which
our single receiver cannot infer. IMO is often null for AIS-only vessels here,
so the label join leans on AIS static IMO and uses GFW's only to fill gaps.

Scheduled hourly. Rate-limit aware, vessel-identity calls capped (logged, never
silently truncated).
"""
import glob
import os
import sys
import time

import pandas as pd
import requests

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in [_here] + sorted(glob.glob("/hopsfs/Users/*/ghost-fleet")):
    if os.path.exists(os.path.join(_p, "ghost_features.py")):
        sys.path.insert(0, _p)
        break
from ghost_features import BBOXES  # noqa: E402

BASE = "https://gateway.api.globalfishingwatch.org/v3"
EVENT_DATASETS = {
    "encounter": "public-global-encounters-events:latest",
    "loitering": "public-global-loitering-events:latest",
    "gap": "public-global-gaps-events:latest",
}
if os.environ.get("GFW_PORT_VISITS") == "1":   # voluminous in the Baltic, opt-in
    EVENT_DATASETS["port_visit"] = "public-global-port-visits-events:latest"
LOOKBACK_DAYS = int(os.environ.get("GFW_LOOKBACK_DAYS", "60"))
MAX_VESSELS = int(os.environ.get("GFW_MAX_VESSELS", "500"))
MAX_PAGES = int(os.environ.get("GFW_MAX_PAGES", "40"))
THEATRES = [("baltic", BBOXES[0]), ("laconian", BBOXES[1])]
# Wall-clock budget so an hourly job never laps its own cron. Under GFW throttling
# the 500-vessel identity loop alone can run hours; bound it and write what we got.
BUDGET_MIN = float(os.environ.get("GFW_BUDGET_MIN", "12"))


def _polygon(box):
    (la1, lo1), (la2, lo2) = box
    return {"type": "Polygon", "coordinates": [[[lo1, la1], [lo2, la1],
            [lo2, la2], [lo1, la2], [lo1, la1]]]}


def _session(token):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


def _get(s, url, **kw):
    for attempt in range(4):
        r = s.get(url, timeout=40, **kw)
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def _post(s, url, body, **kw):
    for attempt in range(4):
        r = s.post(url, json=body, timeout=60, **kw)
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def _fetch_events(s, start, end, deadline):
    rows, vessel_ids = [], set()
    for etype, ds in EVENT_DATASETS.items():
        for tag, box in THEATRES:
            if time.time() > deadline:
                print(f"event budget hit, stopping before {etype}/{tag}", flush=True)
                return rows, vessel_ids
            body = {"datasets": [ds], "startDate": start, "endDate": end,
                    "geometry": _polygon(box)}
            offset, total, pages = 0, 1, 0
            while offset < total and pages < MAX_PAGES and time.time() <= deadline:
                try:
                    d = _post(s, f"{BASE}/events?limit=100&offset={offset}", body)
                except requests.HTTPError as e:
                    print(f"{etype}/{tag} skipped ({e})", flush=True)
                    break
                pages += 1
                total = d.get("total", 0)
                for e in d.get("entries", []):
                    v = e.get("vessel") or {}
                    cp = (e.get("encounter") or {}).get("vessel") or {}
                    if v.get("id"):
                        vessel_ids.add(v["id"])
                    if cp.get("id"):
                        vessel_ids.add(cp["id"])
                    st, en = e.get("start"), e.get("end")
                    dur = _hours(st, en)
                    pos = e.get("position") or {}
                    rows.append({
                        "event_id": e.get("id"),
                        "event_type": etype,
                        "start_ts": st, "end_ts": en,
                        "duration_hours": dur,
                        "lat": _f(pos.get("lat")), "lon": _f(pos.get("lon")),
                        "vessel_mmsi": str(v.get("ssvid") or ""),
                        "vessel_id": v.get("id") or "",
                        "vessel_flag": v.get("flag") or "",
                        "counterpart_mmsi": str(cp.get("ssvid") or ""),
                        "counterpart_id": cp.get("id") or "",
                        "distance_km": _f((e.get("encounter") or {}).get("medianDistanceKilometers")),
                        "bbox": tag,
                    })
                offset = d.get("nextOffset") or total
            capped = " (page-capped)" if pages >= MAX_PAGES and offset < total else ""
            print(f"{etype}/{tag}: {total} events{capped}", flush=True)
    return rows, vessel_ids


def _fetch_identity(s, vessel_ids, deadline):
    ids = sorted(vessel_ids)
    if len(ids) > MAX_VESSELS:
        print(f"capping identity calls at {MAX_VESSELS} of {len(ids)} vessels", flush=True)
        ids = ids[:MAX_VESSELS]
    out = {}
    for vid in ids:
        if time.time() > deadline:
            print(f"identity budget hit at {len(out)}/{len(ids)} vessels", flush=True)
            break
        try:
            d = _get(s, f"{BASE}/vessels/{vid}",
                     params={"dataset": "public-global-vessel-identity:latest"})
        except requests.HTTPError:
            continue
        sri = (d.get("selfReportedInfo") or [{}])[0]
        reg = (d.get("registryInfo") or [{}])
        reg = reg[0] if reg else {}
        csi = (d.get("combinedSourcesInfo") or [{}])[0]
        shiptypes = csi.get("shiptypes") or []
        mmsi = str(sri.get("ssvid") or "")
        if not mmsi:
            continue
        imo = sri.get("imo") or reg.get("imo") or ""
        out[mmsi] = {
            "mmsi": mmsi, "vessel_id": vid, "imo": str(imo or ""),
            "shipname": sri.get("shipname") or reg.get("shipname") or "",
            "flag": sri.get("flag") or reg.get("flag") or "",
            "ship_type": (shiptypes[0]["name"] if shiptypes else "") or reg.get("vesselType", ""),
            "built_year": _f(reg.get("builtYear") or reg.get("buildYear")),
            "gross_tonnage": _f(reg.get("grossTonnage") or reg.get("tonnageGt")),
            "length_m": _f(reg.get("lengthM") or reg.get("length")),
        }
        time.sleep(0.15)
    return list(out.values())


def _hours(a, b):
    try:
        ta = pd.Timestamp(a); tb = pd.Timestamp(b)
        return round((tb - ta).total_seconds() / 3600.0, 3)
    except Exception:
        return 0.0


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    import hopsworks
    proj = hopsworks.login()
    token = hopsworks.get_secrets_api().get_secret("GFW_TOKEN").value
    s = _session(token)

    end = pd.Timestamp.utcnow().strftime("%Y-%m-%d")
    start = (pd.Timestamp.utcnow() - pd.Timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    print(f"GFW events {start}..{end} (budget {BUDGET_MIN:.0f} min)", flush=True)
    t0 = time.time()
    budget_s = BUDGET_MIN * 60
    # events get ~40% of the budget; the rest is guaranteed for identity
    # (mmsi->imo, which grows the sanctions-label join) so neither leg starves.
    events, vessel_ids = _fetch_events(s, start, end, t0 + budget_s * 0.4)
    identities = _fetch_identity(s, vessel_ids, t0 + budget_s)
    print(f"events={len(events)} vessels={len(identities)}", flush=True)

    fs = proj.get_feature_store()
    now = pd.Timestamp.utcnow()

    if events:
        edf = pd.DataFrame(events).drop_duplicates("event_id")
        edf["start_ts"] = pd.to_datetime(edf["start_ts"], utc=True, errors="coerce")
        edf["end_ts"] = pd.to_datetime(edf["end_ts"], utc=True, errors="coerce")
        for c in ["lat", "lon", "duration_hours", "distance_km"]:
            edf[c] = pd.to_numeric(edf[c], errors="coerce").astype("float64")
        eg = fs.get_or_create_feature_group(
            name="gfw_event", version=1,
            description="Global Fishing Watch v3 derived events (STS encounters, "
                        "loitering, AIS gaps, port visits) over Baltic + Laconian, "
                        "with the vessels involved. The behaviour edges.",
            primary_key=["event_id"], event_time="start_ts",
            online_enabled=False, statistics_config=False)
        eg.insert(edf, write_options={"start_offline_materialization": True})
        print(f"inserted {len(edf)} events", flush=True)

    if identities:
        idf = pd.DataFrame(identities)
        idf["as_of"] = pd.to_datetime(now, utc=True)
        for c in ["built_year", "gross_tonnage", "length_m"]:
            idf[c] = pd.to_numeric(idf[c], errors="coerce").astype("float64")
        ig = fs.get_or_create_feature_group(
            name="vessel_identity", version=1,
            description="GFW vessel identity keyed by MMSI: imo, flag, ship type, "
                        "built year, tonnage. Best-effort enrichment; sparse for "
                        "AIS-only vessels with no registry record.",
            primary_key=["mmsi"], event_time="as_of",
            online_enabled=True, statistics_config=False)
        ig.insert(idf, write_options={"start_offline_materialization": True})
        print(f"inserted {len(idf)} identities", flush=True)


if __name__ == "__main__":
    main()
