PYTHON ?= python3
PYTHONPATH := $(CURDIR)/src

.PHONY: ci install-git-hooks check compile lint test validate demo plan m1-validate m1-demo m15-demo m2-demo experiment-verify-demo experiment-replay-demo experiment-compare-demo experiment-catalog-demo paper-demo paper-audit-demo vectorbt-test vectorbt-demo rqalpha-test rqalpha-demo

ci: lint check

install-git-hooks:
	git config --local core.hooksPath .githooks

check: compile test

compile:
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m compileall -q src tests

lint:
	$(PYTHON) -m ruff check --target-version py39 --select E,F src tests

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

experiment-verify-demo:
	@set -eu; \
	experiment_dir="$$(PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant run-experiment \
		--policy config/policy.momentum.example.json \
		--experiment config/experiment.m1_5.example.json \
		--scenario regime_shift \
		--scenario-start 2022-01-03 \
		--scenario-trading-days 780 \
		| $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["artifact_directory"])')"; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant verify-experiment \
		--experiment-run "$$experiment_dir"

experiment-replay-demo:
	@set -eu; \
	experiment_dir="$$(PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant run-experiment \
		--policy config/policy.momentum.example.json \
		--experiment config/experiment.m1_5.example.json \
		--scenario regime_shift \
		--scenario-start 2022-01-03 \
		--scenario-trading-days 780 \
		| $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["artifact_directory"])')"; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant replay-experiment \
		--experiment-run "$$experiment_dir"

experiment-compare-demo:
	@set -eu; \
	baseline_dir="$$(PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant run-experiment \
		--policy config/policy.momentum.example.json \
		--experiment config/experiment.m1_5.example.json \
		--scenario regime_shift \
		--scenario-start 2022-01-03 \
		--scenario-trading-days 780 \
		| $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["artifact_directory"])')"; \
	candidate_dir="$$(PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant run-experiment \
		--policy config/policy.momentum.example.json \
		--experiment config/experiment.m1_5.example.json \
		--scenario trend_up \
		--scenario-start 2022-01-03 \
		--scenario-trading-days 780 \
		| $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["artifact_directory"])')"; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant compare-experiments \
			--baseline-run "$$baseline_dir" \
			--candidate-run "$$candidate_dir"

experiment-catalog-demo:
	@set -eu; \
	work_dir="$$(mktemp -d)"; \
	trap 'rm -rf "$$work_dir"' EXIT; \
	experiments_root="$$work_dir/experiments"; \
	mkdir -p "$$experiments_root"; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant run-experiment \
		--policy config/policy.momentum.example.json \
		--experiment config/experiment.m1_5.example.json \
		--scenario regime_shift \
		--scenario-start 2022-01-03 \
		--scenario-trading-days 780 \
		--output-root "$$experiments_root" >/dev/null; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant run-experiment \
		--policy config/policy.momentum.example.json \
		--experiment config/experiment.m1_5.example.json \
		--scenario regime_shift \
		--scenario-start 2022-01-03 \
		--scenario-trading-days 780 \
		--output-root "$$experiments_root" >/dev/null; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant catalog-experiments \
		--experiments-root "$$experiments_root" \
		--catalog-out "$$work_dir/catalog.json"

paper-demo:
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant replay-paper-events \
		--initial-cash 100000 \
		--events examples/paper/events.jsonl \
		--state-out artifacts/paper/demo-state.json

paper-audit-demo:
	@set -eu; \
	state_path="artifacts/paper/audit-demo-state.json"; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant replay-paper-events \
		--initial-cash 100000 \
		--events examples/paper/audit_events.jsonl \
		--state-out "$$state_path" >/dev/null; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant audit-paper-state \
		--state "$$state_path" \
		--audit-input examples/paper/audit_input.json \
		--report-out artifacts/paper/audit-demo-report.json

vectorbt-test:
	$(PYTHON) -c 'import vectorbt; assert vectorbt.__version__ == "1.1.0"'
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m unittest \
		tests.test_vectorbt_adapter -v

vectorbt-demo:
	@set -eu; \
	reference_dir="$$(PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant backtest \
		--policy config/policy.example.json \
		--prices examples/prices.csv \
		| $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["artifact_directory"])')"; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant validate-vectorbt \
		--reference-run "$$reference_dir" \
		--prices examples/prices.csv

rqalpha-test:
	$(PYTHON) -c 'import rqalpha; assert rqalpha.__version__ == "6.3.0"'
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m unittest \
		tests.test_rqalpha_adapter -v

rqalpha-demo:
	@set -eu; \
	reference_dir="$$(PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant backtest \
		--policy config/policy.example.json \
		--prices examples/prices.csv \
		| $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["artifact_directory"])')"; \
	PYTHONPATH="$(PYTHONPATH)" $(PYTHON) -m lets_quant validate-rqalpha \
		--reference-run "$$reference_dir" \
		--prices examples/prices.csv
