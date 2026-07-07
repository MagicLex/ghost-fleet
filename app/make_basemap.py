"""Augment app/static/basemap.json with country borders + label points.

Natural Earth 50m, clipped to the theatre bbox derived from the existing land
polygons. Land + ports are kept as-is (they were built the same way).

Run from repo root:  python3 app/make_basemap.py
"""
import json
import urllib.request
from pathlib import Path

NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
OUT = Path(__file__).parent / "static" / "basemap.json"
PAD = 1.0


def fetch(name):
    with urllib.request.urlopen(f"{NE}/{name}.geojson", timeout=120) as r:
        return json.load(r)


def main():
    base = json.loads(OUT.read_text())
    lons = [p[0] for poly in base["land"] for p in poly]
    lats = [p[1] for poly in base["land"] for p in poly]
    x0, x1 = min(lons) - PAD, max(lons) + PAD
    y0, y1 = min(lats) - PAD, max(lats) + PAD
    inside = lambda lo, la: x0 <= lo <= x1 and y0 <= la <= y1

    borders = []
    for f in fetch("ne_50m_admin_0_boundary_lines_land")["features"]:
        g = f["geometry"]
        lines = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
        for line in lines:
            seg = []
            for lo, la in line:
                if inside(lo, la):
                    seg.append([round(lo, 3), round(la, 3)])
                elif len(seg) > 1:
                    borders.append(seg)
                    seg = []
                else:
                    seg = []
            if len(seg) > 1:
                borders.append(seg)

    labels = []
    for f in fetch("ne_50m_admin_0_countries")["features"]:
        pr = f["properties"]
        lo, la = pr.get("LABEL_X"), pr.get("LABEL_Y")
        if lo is not None and inside(lo, la):
            labels.append([round(lo, 2), round(la, 2), pr["NAME"].upper()])

    base["borders"] = borders
    base["labels"] = labels
    OUT.write_text(json.dumps(base, separators=(",", ":")))
    print(f"borders: {len(borders)} segments, labels: {len(labels)} countries, "
          f"{OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
