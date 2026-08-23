PYTHON ?= python3
PYTHONPATH := $(CURDIR)/src

.PHONY: check compile test validate demo plan m1-validate m1-demo m15-demo m2-demo paper-demo

check: compile test

compile:
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m compileall -q src tests

test:
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m unittest discover -s tests -v

validate:
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant validate-policy \
		--policy config/policy.example.json

demo:
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant backtest \
		--policy config/policy.example.json \
		--prices examples/prices.csv

plan:
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant plan-orders \
		--policy config/policy.example.json \
		--prices examples/prices.csv \
		--holdings examples/holdings.csv \
		--cash 25000

m1-validate:
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant validate-research-policy \
		--research-policy config/research_policy.cn-etf.example.json
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant validate-policy \
		--policy config/policy.cn-etf.example.json

m1-demo:
	@set -eu; \
	snapshot_dir="$$(PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant snapshot-data \
		--provider local_csv \
		--provider-version 1 \
		--dataset-name etf_daily_bars \
		--input examples/m1/bars.csv \
		--license-manifest config/data_providers.example.json \
		--request-json '{"fixture":"m1-offline-demo"}' \
		| $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["snapshot_directory"])')"; \
	dataset_dir="$$(PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant curate-data \
		--snapshot "$$snapshot_dir" \
		--research-policy config/research_policy.cn-etf.example.json \
		--calendar examples/m1/calendar.csv \
		--instruments examples/m1/instruments.csv \
		--suspensions examples/m1/suspensions.csv \
		--corporate-actions examples/m1/corporate_actions.csv \
		--as-of 2025-01-08T23:59:59+08:00 \
		| $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["dataset_directory"])')"; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant backtest \
			--policy config/policy.cn-etf.example.json \
			--dataset "$$dataset_dir"

m15-demo:
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant run-experiment \
		--policy config/policy.momentum.example.json \
		--experiment config/experiment.m1_5.example.json \
		--scenario regime_shift \
		--scenario-start 2022-01-03 \
		--scenario-trading-days 780

m2-demo:
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant run-experiment \
		--policy config/policy.momentum.example.json \
		--experiment config/experiment.m2_stability.example.json \
		--scenario regime_shift \
		--scenario-start 2022-01-03 \
		--scenario-trading-days 780

paper-demo:
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant replay-paper-events \
		--initial-cash 100000 \
		--events examples/paper/events.jsonl \
		--state-out artifacts/paper/demo-state.json
