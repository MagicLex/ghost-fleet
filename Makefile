# ghost-fleet -- FTI on Hopsworks: shadow-fleet exposure from behaviour, Baltic + Laconian
# Feature (AIS + GFW + sanctions FGs) -> Training (GBM deception score + network) -> Inference (KServe + oceanic app)
FEAT_ENV = python-feature-pipeline
COLLECT_ENV = ghost-collect-env
TRAIN_ENV = pandas-training-pipeline

envs:                ## clone the collector env (feature-pipeline base + websockets)
	python3 tools/build_envs.py

sanctions-job:       ## deploy + schedule the sanctions label refresh (daily)
	hops job deploy fleet-sanctions pipelines/sanctions_pipeline.py --env $(FEAT_ENV) --overwrite
	python3 tools/schedule.py fleet-sanctions "0 30 4 ? * *" --run

collect-job:         ## deploy + schedule the live AIS collector (hourly, 45-min window)
	hops job deploy fleet-collect pipelines/ais_pipeline.py --env $(COLLECT_ENV) --overwrite
	python3 tools/schedule.py fleet-collect "0 0 0/1 ? * *" --run

gfw-job:             ## deploy + schedule GFW identity + events pull (hourly)
	hops job deploy fleet-gfw pipelines/gfw_pipeline.py --env $(FEAT_ENV) --overwrite
	python3 tools/schedule.py fleet-gfw "0 20 0/1 ? * *" --run

features-job:        ## deploy + schedule the vessel behaviour features (every 30 min)
	hops job deploy fleet-features pipelines/features_pipeline.py --env $(FEAT_ENV) --overwrite
	python3 tools/schedule.py fleet-features "0 0/30 * ? * *" --run

train-job:           ## deploy + schedule the shadow_vessel retrain (daily, promotion-gated)
	hops job deploy fleet-train pipelines/train.py --env $(TRAIN_ENV) --overwrite
	python3 tools/schedule.py fleet-train "0 40 2 ? * *"

network-job:         ## deploy + schedule the shadow-fleet network graph builder (hourly)
	hops job deploy fleet-network pipelines/network_pipeline.py --env $(TRAIN_ENV) --overwrite
	python3 tools/schedule.py fleet-network "0 50 0/1 ? * *" --run

serve:               ## deploy the shadowscorer KServe deployment (after train)
	python3 serving/deploy_serving.py

app:                 ## deploy the ghostfleet oceanic app
	python3 app/deploy_app.py

smoke-sanctions:     ## run the sanctions harvest from the terminal pod
	python3 pipelines/sanctions_pipeline.py
smoke-collect:       ## 2-min live AIS collect from the terminal pod
	RUN_MINUTES=2 python3 pipelines/ais_pipeline.py

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/  --/'
.PHONY: envs sanctions-job collect-job gfw-job features-job train-job network-job serve app smoke-sanctions smoke-collect help
