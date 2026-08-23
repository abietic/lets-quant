import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from lets_quant.config import load_policy
from lets_quant.experiments import (
    ExperimentError,
    load_experiment_spec,
    market_identity,
    run_experiment,
)
from lets_quant.scenarios import (
    SUPPORTED_SYNTHETIC_SCENARIOS,
    generate_synthetic_market,
)

from tests.helpers import make_policy


ROOT = Path(__file__).resolve().parents[1]


class ExperimentTest(unittest.TestCase):
    def test_all_synthetic_scenarios_have_complete_deterministic_coverage(
        self,
    ) -> None:
        symbols = ["ASSET_A", "ASSET_B", "BENCH"]
        for scenario in SUPPORTED_SYNTHETIC_SCENARIOS:
            with self.subTest(scenario=scenario):
                generated = generate_synthetic_market(
                    scenario,
                    start_date=date(2025, 1, 2),
                    trading_days=40,
                    symbols=symbols,
                    benchmark="BENCH",
                    seed=7,
                )
                self.assertEqual(len(generated.market.dates), 40)
                self.assertTrue(
                    all(
                        set(generated.market.prices_on(trading_date))
                        == set(symbols)
                        for trading_date in generated.market.dates
                    )
                )
                if scenario == "suspension":
                    self.assertTrue(
                        any(
                            "ASSET_A"
                            not in generated.market.tradable_by_date[trading_date]
                            for trading_date in generated.market.dates
                        )
                    )

    def test_same_inputs_produce_same_market_and_result_hash(self) -> None:
        policy = load_policy(ROOT / "config/policy.momentum.example.json")
        spec = load_experiment_spec(
            ROOT / "config/experiment.m1_5.example.json"
        )
        symbols = ["ASSET_A", "ASSET_B", "BENCH"]
        first_market = generate_synthetic_market(
            "regime_shift",
            start_date=date(2022, 1, 3),
            trading_days=780,
            symbols=symbols,
            benchmark="BENCH",
            seed=spec.seed,
        ).market
        second_market = generate_synthetic_market(
            "regime_shift",
            start_date=date(2022, 1, 3),
            trading_days=780,
            symbols=symbols,
            benchmark="BENCH",
            seed=spec.seed,
        ).market

        self.assertEqual(
            market_identity(first_market), market_identity(second_market)
        )
        first = run_experiment(spec, policy, first_market)
        second = run_experiment(spec, policy, second_market)

        self.assertEqual(first.experiment_input_id, second.experiment_input_id)
        self.assertEqual(first.result_sha256, second.result_sha256)
        self.assertEqual(len(first.cases), 9)
        self.assertEqual(
            {case.window.role for case in first.cases},
            {"train", "validation", "test"},
        )
        delayed = [
            case
            for case in first.cases
            if case.execution_scenario.name == "delayed-execution"
        ]
        self.assertTrue(
            all(
                case.result.metrics["execution_delay_trading_days"] == 3
                for case in delayed
            )
        )

    def test_overlapping_windows_are_rejected(self) -> None:
        raw = json.loads(
            (ROOT / "config/experiment.m1_5.example.json").read_text(
                encoding="utf-8"
            )
        )
        raw["windows"][1]["start"] = "2023-06-30"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "experiment.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ExperimentError, "must not overlap"):
                load_experiment_spec(path)

    def test_schema_v2_runs_rolling_parameter_stability_matrix(self) -> None:
        policy = load_policy(ROOT / "config/policy.momentum.example.json")
        spec = load_experiment_spec(
            ROOT / "config/experiment.m2_stability.example.json"
        )
        market = generate_synthetic_market(
            "regime_shift",
            start_date=date(2022, 1, 3),
            trading_days=780,
            symbols=["ASSET_A", "ASSET_B", "BENCH"],
            benchmark="BENCH",
            seed=spec.seed,
        ).market

        result = run_experiment(spec, policy, market)

        self.assertEqual(len(result.cases), 24)
        self.assertEqual(
            {case.window.fold for case in result.cases},
            {"fold-1", "fold-2"},
        )
        self.assertEqual(
            {case.parameter_variant.name for case in result.cases},
            {
                "configured",
                "shorter-lookback",
                "longer-lookback",
                "stricter-momentum",
            },
        )
        stability = result.summary["test_parameter_stability"]
        self.assertTrue(stability["sensitivity_evaluated"])
        self.assertTrue(stability["descriptive_only"])
        self.assertFalse(stability["automatic_parameter_selection"])
        self.assertEqual(stability["comparison_count"], 2)
        self.assertEqual(result.summary["walk_forward"]["fold_count"], 2)
        self.assertFalse(
            result.summary["walk_forward"]["model_refit_per_fold"]
        )
        regime_summary = result.summary["test_market_regime_attribution"]
        self.assertTrue(regime_summary["enabled"])
        self.assertTrue(regime_summary["descriptive_only"])
        self.assertFalse(regime_summary["used_for_parameter_selection"])
        self.assertEqual(regime_summary["test_case_count"], 8)
        self.assertEqual(regime_summary["comparison_count"], 20)
        self.assertTrue(
            all(
                abs(
                    case.regime_attribution.to_summary()[
                        "strategy_reconciliation_error"
                    ]
                )
                < 1e-12
                for case in result.cases
            )
        )
        uncertainty = result.summary["test_bootstrap_uncertainty"]
        self.assertTrue(uncertainty["enabled"])
        self.assertTrue(uncertainty["descriptive_only"])
        self.assertFalse(uncertainty["p_value_reported"])
        self.assertFalse(uncertainty["pooled_performance_estimate"])
        self.assertFalse(uncertainty["test_windows_overlap"])
        self.assertEqual(uncertainty["test_case_count"], 8)
        self.assertEqual(uncertainty["enabled_case_count"], 8)
        self.assertEqual(uncertainty["comparison_count"], 4)
        self.assertTrue(
            all(
                comparison["strategy_total_return"]["available_case_count"]
                == 2
                for comparison in uncertainty["comparisons"]
            )
        )
        self.assertTrue(
            all(
                case.bootstrap_uncertainty.enabled
                == (case.window.role == "test")
                for case in result.cases
            )
        )

    def test_walk_forward_test_windows_must_advance(self) -> None:
        raw = json.loads(
            (
                ROOT / "config/experiment.m2_stability.example.json"
            ).read_text(encoding="utf-8")
        )
        raw["windows"][3]["start"] = "2022-01-03"
        raw["windows"][3]["end"] = "2022-12-30"
        raw["windows"][4]["start"] = "2023-01-02"
        raw["windows"][4]["end"] = "2023-06-30"
        raw["windows"][5]["start"] = "2023-07-03"
        raw["windows"][5]["end"] = "2023-12-29"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "experiment.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ExperimentError, "must advance"):
                load_experiment_spec(path)

    def test_parameter_baseline_and_overrides_are_strict(self) -> None:
        raw = json.loads(
            (
                ROOT / "config/experiment.m2_stability.example.json"
            ).read_text(encoding="utf-8")
        )
        raw["parameter_variants"][0]["lookback_trading_days"] = 60
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "experiment.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(
                ExperimentError, "override-free"
            ):
                load_experiment_spec(path)

    def test_momentum_overrides_are_rejected_for_fixed_weight_policy(
        self,
    ) -> None:
        spec = load_experiment_spec(
            ROOT / "config/experiment.m2_stability.example.json"
        )
        market = generate_synthetic_market(
            "regime_shift",
            start_date=date(2022, 1, 3),
            trading_days=780,
            symbols=["ASSET_A", "ASSET_B"],
            benchmark=None,
            seed=spec.seed,
        ).market
        policy = make_policy(weights={"ASSET_A": 0.5, "ASSET_B": 0.4})

        with self.assertRaisesRegex(
            ExperimentError, "cannot override momentum"
        ):
            run_experiment(spec, policy, market)

    def test_experiment_without_benchmark_disables_regime_attribution(
        self,
    ) -> None:
        spec = load_experiment_spec(
            ROOT / "config/experiment.m1_5.example.json"
        )
        market = generate_synthetic_market(
            "regime_shift",
            start_date=date(2022, 1, 3),
            trading_days=780,
            symbols=["ASSET_A", "ASSET_B"],
            benchmark=None,
            seed=spec.seed,
        ).market
        policy = make_policy(
            weights={"ASSET_A": 0.5, "ASSET_B": 0.4},
            rebalance_every=20,
        )

        result = run_experiment(spec, policy, market)
        summary = result.summary["test_market_regime_attribution"]

        self.assertFalse(summary["enabled"])
        self.assertEqual(summary["test_case_count"], 3)
        self.assertEqual(summary["comparison_count"], 0)
        uncertainty = result.summary["test_bootstrap_uncertainty"]
        self.assertTrue(uncertainty["enabled"])
        self.assertIsNone(uncertainty["benchmark"])
        self.assertEqual(uncertainty["enabled_case_count"], 3)
        self.assertEqual(uncertainty["comparison_count"], 3)
        self.assertTrue(
            all(
                comparison["strategy_total_return"] is not None
                and comparison["benchmark_total_return"] is None
                and comparison["strategy_relative_to_benchmark"] is None
                for comparison in uncertainty["comparisons"]
            )
        )


if __name__ == "__main__":
    unittest.main()
