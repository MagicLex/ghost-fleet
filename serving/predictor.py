"""KServe predictor for shadow_vessel (deployment `shadowscorer`).

Request instances, mixable:
    {"mmsi": "273123456"}                     online lookup of precomputed features
    {"mmsi": "...", "track": [ {lat,lon,sog,draught,ts}, ... ]}
                                              on-demand: recompute the live-track
                                              features and fuse over the stored ones
    {"pair": ["mmsiA", "mmsiB"]}              derived encounter suspicion for a meeting

Response per instance:
    {"mmsi": ..., "score": 0..1, "reasons": [plain words], "flag": ...}
    {"pair": [...], "encounter_score": 0..1, "vessels": [{mmsi,score}, ...]}

The precomputed history (vessel_track_features via the online FV) fused with the
on-demand live-track features is the feature-store showpiece. The shared
extractor ships next to model.joblib, so features are bit-identical to training.
"""
import glob
import os
import sys

import joblib
import numpy as np


def _find_artifacts():
    cands = [os.environ.get("ARTIFACT_FILES_PATH"),
             os.environ.get("MODEL_FILES_PATH"), "/mnt/models", "."]
    cands += [os.path.dirname(p) for p in
              glob.glob("/mnt/**/ghost_features.py", recursive=True)]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "ghost_features.py")):
            return c
    import hopsworks
    mr = hopsworks.login().get_model_registry()
    models = mr.get_models("shadow_vessel")
    if not models:
        raise RuntimeError("no shadow_vessel model registered yet")
    model = max(models, key=lambda m: m.version)
    d = model.download()
    if not os.path.exists(os.path.join(d, "ghost_features.py")):
        raise RuntimeError(f"extractor missing from registry download {d}")
    return d


ART = _find_artifacts()
sys.path.insert(0, ART)
from ghost_features import (FEATURE_COLUMNS, FOC_FLAGS,  # noqa: E402
                            ONDEMAND_COLUMNS, featurize_vessel, reasons)

# encounter suspicion weights (a derived function of the two vessels, gated so a
# benign meeting cannot flag on arithmetic -- the cascade n>=8 scar)
STS_KM = 2.0


class Predict(object):
    def __init__(self):
        b = joblib.load(os.path.join(ART, "model.joblib"))
        self.model = b["model"]
        self.cols = b["feature_columns"]
        self._fv = None

    def _feature_view(self):
        if self._fv is None:
            import hopsworks
            fs = hopsworks.login().get_feature_store()
            fv = fs.get_feature_view("shadow_vessel_fv", version=1)
            fv.init_serving()
            self._fv = fv
        return self._fv

    def _stored(self, mmsi):
        try:
            vec = self._feature_view().get_feature_vector(
                entry={"mmsi": str(mmsi)}, return_type="pandas")
            return vec.iloc[0].to_dict()
        except Exception:
            return {}

    def _features_and_flag(self, inst):
        """Fuse stored (precomputed) features with on-demand live-track ones."""
        mmsi = str(inst.get("mmsi"))
        stored = self._stored(mmsi)
        feats = {c: stored.get(c) for c in FEATURE_COLUMNS}
        flag = stored.get("flag") or ""
        if inst.get("track"):
            od = featurize_vessel(inst["track"])
            # only AIS-behavioural cols; a bare track has no events/identity, so
            # never let it clobber stored gfw_*/flag/age/tonnage signal.
            for c in ONDEMAND_COLUMNS:                # on-demand wins when fresher
                if od.get(c) not in (None, 0.0) or feats.get(c) in (None, ):
                    feats[c] = od[c]
        return feats, flag

    def _score(self, feats):
        x = np.array([[np.nan if feats.get(c) is None else float(feats[c])
                       for c in self.cols]])
        return float(self.model.predict_proba(x)[0, 1])

    def _score_instance(self, inst):
        if inst.get("pair"):
            return self._score_pair(inst["pair"])
        feats, flag = self._features_and_flag(inst)
        score = self._score(feats)
        flags_seen = [f for f in [flag] if f]
        return {"mmsi": inst.get("mmsi"), "score": round(score, 4),
                "flag": flag, "reasons": reasons(feats, flags_seen)}

    def _score_pair(self, pair):
        a, b = str(pair[0]), str(pair[1])
        fa, _ = self._features_and_flag({"mmsi": a})
        fb, _ = self._features_and_flag({"mmsi": b})
        sa, sb = self._score(fa), self._score(fb)
        hot = max(fa.get("frac_in_sts_hotspot", 0) or 0,
                  fb.get("frac_in_sts_hotspot", 0) or 0)
        dark = (fa.get("gfw_gaps", 0) or 0) + (fb.get("gfw_gaps", 0) or 0)
        # gate: both vessels must carry some suspicion, else a normal meeting
        enc = min(sa, sb) * (0.6 + 0.4 * hot) + min(0.2, 0.05 * dark)
        enc = enc if min(sa, sb) > 0.15 else 0.0
        return {"pair": [a, b], "encounter_score": round(min(1.0, enc), 4),
                "vessels": [{"mmsi": a, "score": round(sa, 4)},
                            {"mmsi": b, "score": round(sb, 4)}]}

    @staticmethod
    def _norm(inputs):
        while isinstance(inputs, dict) and "instances" in inputs:
            inputs = inputs["instances"]
        if isinstance(inputs, list):
            while len(inputs) == 1 and isinstance(inputs[0], list):
                inputs = inputs[0]
        return inputs if isinstance(inputs, list) else [inputs]

    def predict(self, inputs):
        out = []
        for inst in self._norm(inputs):
            try:
                out.append(self._score_instance(inst))
            except Exception as e:
                out.append({"mmsi": inst.get("mmsi") if isinstance(inst, dict) else None,
                            "error": str(e)})
        return {"predictions": out}
