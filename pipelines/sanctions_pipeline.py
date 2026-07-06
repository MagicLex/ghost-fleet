"""F4 sanctions label pipeline (keyless).

Downloads the OpenSanctions **Consolidated Sanctions** collection (aggregates
OFAC SDN + EU FSF + UK OFSI + AU + CH + UN ...), keeps schema=Vessel rows,
extracts IMO + flags + which authorities listed it, and writes the
`sanctioned_vessel` feature group. This is the weak ground-truth label: a
vessel present here is a known shadow-fleet / sanctioned ship, and the feature
view turns presence into the y label (absent -> 0).

Detention / port-state-control lists (Tokyo/Abuja MoU) are deliberately NOT
used: an inspection is not a sanction, and mixing them poisons the label.
The Consolidated Sanctions collection already excludes them.

Scheduled daily. Resolves the dated artifact URL from the collection index, so
nothing is hardcoded to a snapshot.
"""
import csv
import os
import re
import shutil
import tempfile

import pandas as pd
import requests

INDEX = "https://data.opensanctions.org/datasets/latest/sanctions/index.json"
IMO_RE = re.compile(r"IMO(\d{7})")


def _resolve_csv_url():
    idx = requests.get(INDEX, timeout=30).json()
    for r in idx.get("resources", []):
        if r["name"] == "targets.simple.csv":
            return r["url"]
    raise RuntimeError("targets.simple.csv not found in sanctions collection index")


def _harvest():
    url = _resolve_csv_url()
    print(f"streaming {url}", flush=True)
    tmp = os.path.join(tempfile.gettempdir(), "opensanctions_targets.csv")
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        r.raw.decode_content = True          # transparently gunzip the stream
        with open(tmp, "wb") as f:
            shutil.copyfileobj(r.raw, f, length=1 << 20)
    by_imo = {}
    seen_vessels = 0
    with open(tmp, encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        col = {name: i for i, name in enumerate(header)}
        ci, cs, cn = col["identifiers"], col["schema"], col["name"]
        cc, cd = col["countries"], col["dataset"]
        for row in reader:
            if len(row) <= cd or row[cs] != "Vessel":
                continue
            seen_vessels += 1
            m = IMO_RE.search(row[ci] or "")
            if not m:
                continue
            imo = m.group(1)
            rec = by_imo.setdefault(imo, {"vessel_name": row[cn], "flags": set(),
                                          "programs": set()})
            rec["flags"].update(f.strip().upper() for f in (row[cc] or "").split(";") if f.strip())
            rec["programs"].update(p.strip() for p in (row[cd] or "").split(";") if p.strip())
            if not rec["vessel_name"]:
                rec["vessel_name"] = row[cn]
    os.remove(tmp)
    print(f"vessels scanned: {seen_vessels}, with IMO: {len(by_imo)}", flush=True)
    return by_imo


def main():
    import hopsworks
    by_imo = _harvest()
    if not by_imo:
        raise RuntimeError("no sanctioned vessels harvested")
    now = pd.Timestamp.utcnow()
    df = pd.DataFrame([{
        "imo": imo,
        "vessel_name": rec["vessel_name"][:120],
        "flags": ";".join(sorted(rec["flags"])),
        "programs": ";".join(sorted(rec["programs"]))[:500],
        "on_list": 1,
        "as_of": now,
    } for imo, rec in by_imo.items()])
    df["as_of"] = pd.to_datetime(df["as_of"], utc=True)

    proj = hopsworks.login()
    fs = proj.get_feature_store()
    fg = fs.get_or_create_feature_group(
        name="sanctioned_vessel", version=1,
        description="Vessels on consolidated international sanctions lists "
                    "(OpenSanctions: OFAC SDN + EU FSF + UK OFSI + AU + CH + UN), "
                    "keyed by IMO. Weak ground-truth label for the shadow-fleet "
                    "classifier; port-state detention lists excluded on purpose.",
        primary_key=["imo"], event_time="as_of",
        online_enabled=True, statistics_config=False)
    fg.insert(df, write_options={"start_offline_materialization": True})
    print(f"inserted {len(df)} sanctioned vessels", flush=True)


if __name__ == "__main__":
    main()
