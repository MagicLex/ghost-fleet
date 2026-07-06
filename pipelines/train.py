"""T1 training pipeline (also creates F5, the feature view).

shadow_vessel_fv = vessel_track_features LEFT JOIN sanctioned_vessel on IMO, so
a vessel present on a sanctions list gets y=1, everyone else y=0. Trains a
gradient-boosting classifier on the behaviour features, grouped-CV by flag so a
known ring cannot leak across folds, and reports the lift over a blind
flag-of-convenience rule (the honest headline: the label is a population split).

No positives yet -> exits cleanly. The daily promotion-gated retrain trains for
real once sanctioned vessels have transited our AIS coverage.
"""
import glob
import json
import os
import shutil
import sys
import tempfile

import numpy as np
import pandas as pd

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in [_here] + sorted(glob.glob("/hopsfs/Users/*/ghost-fleet")):
    if os.path.exists(os.path.join(_p, "ghost_features.py")):
        ROOT = _p
        sys.path.insert(0, _p)
        break
from ghost_features import FEATURE_COLUMNS  # noqa: E402

MODEL_NAME = "shadow_vessel"
MIN_POSITIVES = int(os.environ.get("MIN_POSITIVES", "12"))
SERVE_REQS = ["scikit-learn==1.5.2", "numpy==1.26.4", "pandas==2.2.2",
              "joblib==1.4.2", "scipy==1.13.1"]


def _feature_view(fs):
    try:
        return fs.get_feature_view("shadow_vessel_fv", version=1)
    except Exception:
        vtf = fs.get_feature_group("vessel_track_features", version=1)
        san = fs.get_feature_group("sanctioned_vessel", version=1)
        q = vtf.select_all().join(san.select(["on_list"]), on=["imo"],
                                  join_type="left")
        fv = fs.create_feature_view(
            name="shadow_vessel_fv", version=1, query=q, labels=["on_list"],
            description="Vessel behaviour features labelled by sanctions-list "
                        "presence (IMO join). y=1 shadow-fleet/sanctioned.")
        print("created shadow_vessel_fv v1", flush=True)
        return fv


def main():
    import hopsworks
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import average_precision_score, roc_auc_score
    from sklearn.model_selection import GroupKFold, StratifiedKFold

    proj = hopsworks.login()
    fs = proj.get_feature_store()
    fv = _feature_view(fs)

    df = fv.get_batch_data() if hasattr(fv, "get_batch_data") else None
    # training_data path (features + labels)
    X_all, y_all = fv.training_data() if df is None else (df, None)
    if y_all is None:
        # fv.training_data returns (df, labels_series) in this SDK
        X_all, y_all = fv.training_data()
    df = X_all.copy()
    df["on_list"] = pd.to_numeric(
        (y_all if not isinstance(y_all, pd.DataFrame) else y_all.iloc[:, 0]),
        errors="coerce").fillna(0).astype(int).values

    n_pos = int(df["on_list"].sum())
    print(f"training rows={len(df)} positives={n_pos}", flush=True)
    if n_pos < MIN_POSITIVES:
        print(f"only {n_pos} positives (< {MIN_POSITIVES}); skipping train, "
              "waiting for sanctioned vessels to accumulate in AIS coverage.",
              flush=True)
        return

    X = df[FEATURE_COLUMNS].apply(pd.to_numeric, errors="coerce").astype(float)
    y = df["on_list"].values
    groups = df["flag"].fillna("").replace("", "NA").values

    # grouped CV out-of-fold predictions (fall back to stratified if too few groups)
    oof = np.zeros(len(y))
    n_groups = len(set(groups))
    splitter = (GroupKFold(n_splits=min(5, n_groups))
                if n_groups >= 5 else StratifiedKFold(n_splits=5, shuffle=True,
                                                      random_state=0))
    split_iter = (splitter.split(X, y, groups) if isinstance(splitter, GroupKFold)
                  else splitter.split(X, y))
    for tr, te in split_iter:
        m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.06,
                                            l2_regularization=1.0, random_state=0)
        m.fit(X.iloc[tr], y[tr])
        oof[te] = m.predict_proba(X.iloc[te])[:, 1]

    ap = average_precision_score(y, oof)
    roc = roc_auc_score(y, oof)
    base_ap = average_precision_score(y, X["flag_is_foc"].fillna(0).values)
    lift = ap / base_ap if base_ap > 0 else float("nan")
    k = max(10, n_pos)
    topk = np.argsort(oof)[::-1][:k]
    prec_at_k = float(y[topk].mean())
    print(f"CV PR-AUC={ap:.3f} ROC-AUC={roc:.3f} "
          f"blind-foc PR-AUC={base_ap:.3f} lift={lift:.2f} "
          f"precision@{k}={prec_at_k:.3f}", flush=True)

    # fit final on all data
    model = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.06,
                                           l2_regularization=1.0, random_state=0)
    model.fit(X, y)

    metrics = {"cv_pr_auc": round(float(ap), 4), "cv_roc_auc": round(float(roc), 4),
               "blind_foc_pr_auc": round(float(base_ap), 4),
               "lift_over_blind": round(float(lift), 3),
               f"precision_at_{k}": round(prec_at_k, 4),
               "n_rows": int(len(df)), "n_positives": n_pos}
    _register(proj, model, X, y, oof, metrics)


def _register(proj, model, X, y, oof, metrics):
    import joblib
    from sklearn.metrics import PrecisionRecallDisplay
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = tempfile.mkdtemp()
    joblib.dump({"model": model, "feature_columns": FEATURE_COLUMNS}, os.path.join(d, "model.joblib"))
    shutil.copy(os.path.join(ROOT, "ghost_features.py"), os.path.join(d, "ghost_features.py"))
    with open(os.path.join(d, "requirements-serve.txt"), "w") as f:
        f.write("\n".join(SERVE_REQS) + "\n")
    with open(os.path.join(d, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)

    img = os.path.join(d, "images")
    os.makedirs(img, exist_ok=True)
    PrecisionRecallDisplay.from_predictions(y, oof)
    plt.title(f"shadow_vessel PR (AP={metrics['cv_pr_auc']}, "
              f"lift x{metrics['lift_over_blind']} vs blind)")
    plt.savefig(os.path.join(img, "pr_curve.png"), dpi=110, bbox_inches="tight")
    plt.close()
    imp = getattr(model, "feature_importances_", None)
    if imp is None:
        from sklearn.inspection import permutation_importance
        imp = permutation_importance(model, X, y, n_repeats=5, random_state=0).importances_mean
    order = np.argsort(imp)[::-1][:15]
    plt.figure(figsize=(7, 5))
    plt.barh([FEATURE_COLUMNS[i] for i in order][::-1], [imp[i] for i in order][::-1])
    plt.title("shadow_vessel feature importance")
    plt.savefig(os.path.join(img, "feature_importance.png"), dpi=110, bbox_inches="tight")
    plt.close()

    mr = proj.get_model_registry()
    card = (
        "# shadow_vessel\n\n"
        "Ranks vessels by behavioural similarity to sanctioned shadow-fleet ships "
        "(AIS gaps, loitering, STS rendezvous, flag-hopping, draught swings, aging "
        "tanker + flag-of-convenience identity).\n\n"
        "**Read the lift, not the absolute.** Positives are vessels on consolidated "
        "sanctions lists; negatives are general Baltic/Laconian traffic. That is a "
        "population split, so the honest metric is the lift over a blind "
        "flag-of-convenience rule. A coordination and evasion signal, never proof "
        f"of a crime.\n\nCV PR-AUC {metrics['cv_pr_auc']}, lift "
        f"x{metrics['lift_over_blind']} over blind, on {metrics['n_positives']} "
        f"positives / {metrics['n_rows']} vessels.\n")
    with open(os.path.join(d, "README.md"), "w") as f:
        f.write(card)

    existing = mr.get_models(MODEL_NAME)
    champ = max([m.training_metrics.get("cv_pr_auc", 0) for m in existing], default=0) if existing else 0
    if metrics["cv_pr_auc"] < champ:
        print(f"challenger PR-AUC {metrics['cv_pr_auc']} < champion {champ}; not registering", flush=True)
        return
    m = mr.python.create_model(
        name=MODEL_NAME, metrics=metrics,
        description="Shadow-fleet deception score from vessel behaviour; "
                    "evasion signal, not proof of crime.",
        feature_view=proj.get_feature_store().get_feature_view("shadow_vessel_fv", 1))
    m.save(d)
    print(f"registered {MODEL_NAME} v{m.version} (PR-AUC {metrics['cv_pr_auc']})", flush=True)


if __name__ == "__main__":
    main()
