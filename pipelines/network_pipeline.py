"""T2 shadow-fleet network builder.

Turns GFW ship-to-ship encounter edges into the network the app draws: pairs of
vessels that meet at sea, grouped into rings (connected components). A vessel
that keeps meeting the same cluster in the dark is the Panama-Papers-style tell.

Writes `vessel_network` (one row per edge, with its ring id + size). Node
suspicion (sanctioned / high model score) is joined in the app from the live
scores; here we persist the topology and the sanctioned flag.

v1 edges = GFW encounters (authoritative). AIS co-loitering pairs are a v2
enrichment (more edges, some false positives, needs the pairwise proximity pass).

Scheduled hourly.
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


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def main():
    import hopsworks
    proj = hopsworks.login()
    fs = proj.get_feature_store()

    ev = fs.get_feature_group("gfw_event", version=1).read()
    enc = ev[ev["event_type"] == "encounter"] if not ev.empty else ev
    if enc.empty:
        print("no encounter edges yet; network is empty", flush=True)
        return

    # sanctioned IMO set + mmsi->imo from identity, to flag sanctioned nodes
    try:
        san = set(fs.get_feature_group("sanctioned_vessel", 1).read()["imo"].astype(str))
    except Exception:
        san = set()
    try:
        idf = fs.get_feature_group("vessel_identity", 1).read()
        mmsi_imo = {str(r["mmsi"]): str(r["imo"]) for _, r in idf.iterrows() if r.get("imo")}
    except Exception:
        mmsi_imo = {}

    weight = {}
    uf = _UF()
    for _, e in enc.iterrows():
        a, b = str(e.get("vessel_mmsi") or ""), str(e.get("counterpart_mmsi") or "")
        if not a or not b or a == b:
            continue
        k = tuple(sorted((a, b)))
        weight[k] = weight.get(k, 0) + 1
        uf.union(a, b)

    if not weight:
        print("no valid encounter pairs; network empty", flush=True)
        return

    ring_of = {}
    for (a, b) in weight:
        ring_of[a] = uf.find(a)
        ring_of[b] = uf.find(b)
    ring_ids = {r: i for i, r in enumerate(sorted(set(ring_of.values())))}
    ring_size = {}
    for m, r in ring_of.items():
        ring_size[ring_ids[r]] = ring_size.get(ring_ids[r], 0) + 1

    now = pd.Timestamp.utcnow()
    rows = []
    for (a, b), w in weight.items():
        rid = ring_ids[uf.find(a)]
        rows.append({
            "src_mmsi": a, "dst_mmsi": b, "weight": int(w),
            "ring_id": int(rid), "ring_size": int(ring_size[rid]),
            "src_sanctioned": int(mmsi_imo.get(a, "") in san),
            "dst_sanctioned": int(mmsi_imo.get(b, "") in san),
            "as_of": now,
        })
    df = pd.DataFrame(rows)
    df["as_of"] = pd.to_datetime(df["as_of"], utc=True)

    fg = fs.get_or_create_feature_group(
        name="vessel_network", version=1,
        description="Shadow-fleet network: GFW ship-to-ship encounter edges "
                    "grouped into rings (connected components). A vessel that "
                    "keeps meeting the same cluster in the dark is the tell.",
        primary_key=["src_mmsi", "dst_mmsi"], event_time="as_of",
        online_enabled=False, statistics_config=False)
    fg.insert(df, write_options={"start_offline_materialization": True})
    n_rings = len({r for r in ring_ids.values()})
    print(f"inserted {len(df)} edges, {n_rings} rings "
          f"(largest {max(ring_size.values())} vessels)", flush=True)


if __name__ == "__main__":
    main()
