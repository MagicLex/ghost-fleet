"""Clone the collector env: python-feature-pipeline base + websockets.

The AIS collector reads the aisstream websocket; the base feature-pipeline env
has no `websockets`, so fleet-collect runs on this clone. Idempotent.
"""
from pathlib import Path

import hopsworks

NAME = "ghost-collect-env"
_here = Path(__file__).resolve()
ROOT_REL = str(Path(str(_here).split("/hopsfs/", 1)[1]).parent.parent)


def main():
    proj = hopsworks.login()
    env_api = proj.get_environment_api()
    env = env_api.get_environment(NAME)
    if env is None:
        env = env_api.create_environment(
            NAME, base_environment_name="python-feature-pipeline")
        print(f"cloned {NAME}", flush=True)
    env.install_requirements(f"{ROOT_REL}/requirements-collect.txt",
                             await_installation=True)
    print(f"installed collector deps into {NAME}", flush=True)


if __name__ == "__main__":
    main()
