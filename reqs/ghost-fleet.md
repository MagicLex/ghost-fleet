# Ghost Fleet: shadow-fleet exposure from behavior alone

Real-time multi-source ML system. The prediction is a **shadow-fleet deception score**
per vessel, seeded by open sanctions lists, computed from how a vessel *behaves* (AIS
gaps, loitering, ship-to-ship rendezvous, flag-hopping) rather than from being already
listed. A derived encounter score and a Panama-Papers-style vessel **network graph**
sit on top. The deal-maker is the feature store: many sources at different clocks fused
point-in-time, served online, no leakage, no train/serve skew.

**Honesty rule (phishing/cascade lineage):** the labels are a population split
(sanctioned vessels vs general Baltic traffic), so the headline is the **lift over a
blind sanctions-list lookup**, not the absolute AUC. The system reports a *coordination
and evasion signal, never proof of a crime*.

**Scope v1:** Baltic Sea + Gulf of Finland (Russian oil price-cap shadow fleet loading
at Primorsk / Ust-Luga), plus a second small bounding box on the Laconian Gulf, Greece
(the Mediterranean ship-to-ship transfer hotspot). Bounded bboxes keep AIS ingest sane.

---

## AI-system card

| Field | Value |
|---|---|
| **Prediction problem** | Binary classification (vessel: shadow-fleet member y/n), plus a derived encounter suspicion score |
| **Entity / key** | Vessel = IMO (fallback MMSI). Encounter = GFW `event_id` / (imo_a, imo_b, t_start) |
| **KPI** | Analyst triage efficiency: of the top-K flagged vessels, fraction that are genuine shadow-fleet (watch-time saved vs scanning all traffic); coverage of known sanctioned vessels surfaced from behavior alone |
| **ML proxy metric** | PR-AUC on held-out sanctioned/dark-fleet labels; **lift over the blind baseline** (baseline = flag only vessels already on the list); precision@K and recall reported |
| **System type** | Real-time (live AIS collector + online serving) with an offline training loop |
| **Consumption** | Custom web app: oceanic-skin live map with a suspicion heat scale, a self-drawing vessel network graph, per-vessel dossier on click, external links (GFW, Equasis, MarineTraffic) |
| **Monitoring** | Inference logs every input + prediction to a feature group; feature drift + score-distribution tracked; alert on drift |

---

## Data sources (all pod-reachable, verified)

1. **AIS live stream**: `stream.aisstream.io` websocket (free key), subscribed to the
   v1 bounding boxes. Fields: mmsi, lat/lon, sog, cog, heading, nav_status, ship static
   (name, callsign, type, draught), timestamp. Timing taken from the **ingest clock**,
   not client `createdAt` (sky/cascade rule: reported clocks lie, and speed/darkness are
   the whole signal).
2. **Global Fishing Watch API v3** (free token): vessel identity (imo, flag, type, built
   year, gross tonnage) and derived **events**, ship-to-ship encounters, loitering, AIS
   gaps, port visits. This is fusion plus quasi-labels in one API.
3. **Sanctions vessel lists** (label source): OpenSanctions default dataset filtered to
   schema `Vessel` (aggregates OFAC + EU + UK + OFSI, carries `imoNumber`), cross-checked
   against OFAC SDN vessel records. Refreshed daily.
4. **Vessel registry / flag history**: derived from GFW identity + AIS static + MMSI MID
   country, accumulated over time to detect flag-hopping.
5. **open-meteo** weather at vessel position (wind, visibility): light context, so
   loitering in a storm is scored less suspicious than loitering in calm. v1-optional.
6. **Sentinel-1 SAR** via Copernicus DataSpace STAC (open): **v2 bonus** leg, detect a
   radar contact with no matching AIS = a truly dark ship. Not a v1 dependency.

---

## Pipelines (ordered, feature -> training -> inference)

### F1. AIS collector feature pipeline  `[blocked-by: nothing]`
- Streaming collector job `fleet-collect`: websockets.sync loop over the aisstream ws,
  bounded to the v1 bboxes, background-thread batched flush (the Delta commit-cost-grows
  scar), `statistics_config=False`. Window strictly **< cron interval**, cron-aligned,
  single writer (the cascade two-writers-corrupt-Delta scar).
- Writes raw FG **`ais_position`** v1 (offline, high-frequency).
- Env: clone `python-feature-pipeline` + pin `websockets` (base lacks it).
- Skill: **hops-features** -> hops-fg, hops-data-sources.

### F2. Vessel behavior feature pipeline  `[blocked-by: F1]`
- Job `fleet-features` (:00/:30): from `ais_position` + `gfw_event`, compute per-vessel
  rolling **MITs** into FG **`vessel_track_features`** v1: total AIS-dark time, gap
  count/duration, loitering time, avg/p95 speed, time in STS hotspots, rendezvous count,
  distinct flags seen (flag-hopping), dark-then-reappear count, draught change (laden vs
  ballast = the oil-transfer tell). `event_time` = last activity (convergent idempotent
  accumulation, playbook rule).
- Skill: **hops-features** -> hops-fg.

### F3. GFW identity + events feature pipeline  `[blocked-by: nothing]`
- Job `fleet-gfw` (hourly, rate-limit aware, cached): pull GFW vessel identity and events
  for vessels seen in the bboxes. Writes FGs **`vessel_identity`** v1 and **`gfw_event`**
  v1 (encounter/loitering/gap/port edges).
- Skill: **hops-features** -> hops-data-sources, hops-fg.

### F4. Sanctions label pipeline  `[blocked-by: nothing]`
- Job `fleet-sanctions` (daily): fetch OpenSanctions vessels + OFAC SDN, normalize to IMO,
  write FG **`sanctioned_vessel`** v1 (imo, on_list, list_name, listed_date). This is the
  weak ground-truth label.
- Skill: **hops-features** -> hops-data-sources, hops-fg.

### F5. Feature view + MDTs  `[blocked-by: F2, F3, F4]`
- FV **`shadow_vessel_fv`** v1: point-in-time join of `vessel_track_features` +
  `vessel_identity`, label = `sanctioned_vessel.on_list`. **MDTs** attached to the FV
  (identical at train and serve): impute missing identity, scale numeric behavior,
  encode flag / ship type. On-demand-ready (F2 features also computable live).
- Skill: **hops-fv**, **hops-transformations**.

### T1. Training pipeline  `[blocked-by: F5]`
- EDA first: profile behavior features, check leakage (a feature that trivially encodes
  the sanctions list, e.g. an owner id), confirm the honest population split. Skill:
  **hops-eda** / hops-eda-checklist.
- Train a gradient-boosting classifier (XGBoost / HistGradientBoosting) predicting
  shadow-fleet probability. **Grouped CV** by owner/flag so a known ring cannot leak
  across folds. Metrics: PR-AUC, precision@K, recall on held-out sanctioned vessels, and
  **lift over the blind baseline**. Register model **`shadow_vessel`** v1 + eval images
  (PR curve, confusion, calibration, feature importance) + a model card carrying the
  population-split caveat loud.
- Skill: **hops-train**.

### T2. Network graph builder  `[blocked-by: F3, T1]`
- Job `fleet-network` (hourly/daily): from `gfw_event` encounter edges among high-score
  vessels, build the shadow-fleet graph (connected components / community detection), write
  FG **`vessel_network`** v1 (edges + ring id). This is the Panama-Papers layer the app draws.
- Not a trained model in v1: a graph over scored vessels. Honest, not a z-score in costume.

### I1. Online inference (deployment)  `[blocked-by: T1]`
- KServe deployment **`shadowscorer`** (clone `pandas-inference-pipeline` base): given a
  vessel (imo or its recent track), pull precomputed `vessel_track_features` + identity
  from the online FV and compute **on-demand transforms (ODTs)** from the live AIS window
  in the request (last-N-hours gaps/loitering), fuse both, return shadow-fleet prob + the
  top plain-word reasons ("went dark 3x near Gotland, 2 STS rendezvous, flag changed
  Panama -> Gabon -> Cook Islands"). The **precomputed + on-demand fusion is the feature-
  store showpiece.** Logs inputs + predictions to a monitoring FG.
- **Encounter (derived) score**: a function of the two vessels' scores + darkness +
  offshore-STS location + duration + draught change. Computed at serving, per boss's
  hybrid choice. Gated on vessel scores so tiny/benign meetings do not flag on arithmetic
  (the cascade n>=8 gating scar).
- Skill: **hops-online-inference**, **hops-transformations** (ODTs), **hops-environments**.

### A1. App  `[blocked-by: I1, T2]`
- Custom web app **`ghostfleet`** (FastAPI + canvas/map, server-rendered so content is in
  the initial payload; no SPA). Backend runs the AIS stream, scores forming and known
  vessels through `shadowscorer`, backfills the window from the FGs on start so it is warm
  immediately. Fresh **oceanic skin**: calm blue traffic warming to red-hot suspect rings,
  a second **network-graph** view of the rings, a per-vessel **dossier** on click (dark
  gaps, rendezvous, flag history) with external source-of-truth links. Not the sky
  defense-black scope.
- Skill: **hops-app**.

### M1. Monitoring  `[blocked-by: I1]`
- Inference logging FG + feature drift + score-distribution tracking + drift alert.
- Skill: **hops-monitoring**.

---

## Dependency graph

```
F1 ais-collect ─┐
F3 gfw-id/events┼─► F2 vessel-features ─┐
F4 sanctions ───┘                       ├─► F5 shadow_vessel_fv ─► T1 train ─► I1 shadowscorer ─► A1 ghostfleet app
F3 ─────────────────────────────────────┘                                  │              (+ M1 monitoring)
F3 ─► T2 network graph ──────────────────────────────────────────────────────────────────► A1 (graph view)
```

## v1 vs later
- **v1**: F1-F5, T1-T2, I1, A1, M1 on the Baltic + Gulf of Finland + Laconian Gulf bboxes.
- **v2**: Sentinel-1 SAR dark-ship leg; encounter score as its own weakly-supervised model;
  live per-vessel destination prediction (a regression claim, deception = actual != predicted);
  broaden theaters (Persian Gulf, Malacca).

## Honesty and ethics
The output ranks vessels by behavioral similarity to sanctioned ships. It is a triage and
research signal for open-source investigation, not an accusation. Every flagged vessel links
to the raw source-of-truth services. The model card and the app both carry the
"evasion signal, not proof of crime" caveat.
