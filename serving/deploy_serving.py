"""Deploy shadow_vessel as KServe deployment `shadowscorer`.

Run after fleet-train has registered a model:
1. Clone pandas-inference-pipeline -> ghost-serve-env, pin the training stack
   from the model's requirements-serve.txt (KServe image skew rule).
2. Deploy the latest shadow_vessel with serving/predictor.py.

Idempotent: reuses env / deployment when present.
"""
import os
from pathlib import Path

import hopsworks

ENV_NAME = "ghost-serve-env"
DEP_NAME = "shadowscorer"

_here = Path(__file__).resolve()
REL = str(_here).split("/hopsfs/", 1)[1]
ROOT_REL = str(Path(REL).parent.parent)          # Users/<u>/ghost-fleet


def main():
    proj = hopsworks.login()
    mr = proj.get_model_registry()
    models = mr.get_models("shadow_vessel")
    if not models:
        raise RuntimeError("no shadow_vessel model registered yet; run fleet-train")
    # champion = best lift over blind (the advertised metric), newest on ties.
    # All runs are registered for version control, so pick by metric, not recency.
    model = max(models, key=lambda m: (m.training_metrics.get("lift_over_blind", 0), m.version))
    print(f"model shadow_vessel v{model.version} (lift {model.training_metrics.get('lift_over_blind')})", flush=True)

    env_api = proj.get_environment_api()
    env = env_api.get_environment(ENV_NAME)
    if env is None:
        import shutil
        dl = model.download()
        req_dir = f"/hopsfs/{ROOT_REL}/models"
        os.makedirs(req_dir, exist_ok=True)
        shutil.copy(os.path.join(dl, "requirements-serve.txt"),
                    os.path.join(req_dir, "requirements-serve.txt"))
        env = env_api.create_environment(
            ENV_NAME, base_environment_name="pandas-inference-pipeline")
        env.install_requirements(f"{ROOT_REL}/models/requirements-serve.txt",
                                 await_installation=True)
        print(f"cloned {ENV_NAME} + pinned training stack", flush=True)

    ms = proj.get_model_serving()
    dep = ms.get_deployment(DEP_NAME)
    if dep is None:
        dep = model.deploy(
            name=DEP_NAME,
            script_file=f"/Projects/{proj.name}/{ROOT_REL}/serving/predictor.py",
            environment=ENV_NAME,
            resources={"num_instances": 1,
                       "requests": {"cores": 1, "memory": 2048},
                       "limits": {"cores": 2, "memory": 4096}})
        print(f"created deployment {DEP_NAME}", flush=True)
    dep.start(await_running=600)
    print(f"deployment {DEP_NAME}: {dep.get_state().status}", flush=True)
    smoke = dep.predict(inputs=[{"mmsi": "___smoke___"}])
    print("smoke:", smoke, flush=True)


if __name__ == "__main__":
    main()
