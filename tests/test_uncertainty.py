import unittest
from dataclasses import replace
from datetime import date, timedelta

from lets_quant.models import MarketData, NavRecord
from lets_quant.uncertainty import (
    BootstrapProtocol,
    BootstrapUncertaintyError,
    bootstrap_return_uncertainty,
)


TEST_PROTOCOL = BootstrapProtocol(
    block_length=5,
    resample_count=100,
    confidence_level=0.95,
    minimum_observations=20,
)


def _market_and_nav(
    row_count: int = 81,
    *,
    identical_to_benchmark: bool = False,
) -> tuple[MarketData, list[NavRecord]]:
    dates = [
        date(2025, 1, 1) + timedelta(days=index)
        for index in range(row_count)
    ]
    benchmark_value = 100.0
    nav_value = 100.0
    benchmark_values = [benchmark_value]
    nav_values = [nav_value]
    benchmark_returns = [0.004, -0.003, 0.006, -0.001, 0.002]
    strategy_returns = [0.007, -0.005, 0.003, 0.001, -0.002, 0.006]
    for index in range(1, row_count):
        benchmark_value *= 1 + benchmark_returns[index % len(benchmark_returns)]
        benchmark_values.append(benchmark_value)
        if identical_to_benchmark:
            nav_value = benchmark_value
        else:
            nav_value *= 1 + strategy_returns[index % len(strategy_returns)]
        nav_values.append(nav_value)
    market = MarketData(
        dates=dates,
        prices_by_date={
            trading_date: {"BENCH": benchmark_values[index]}
            for index, trading_date in enumerate(dates)
        },
    )
    nav = [
        NavRecord(
            trading_date=trading_date,
            nav=nav_values[index],
            cash=nav_values[index],
            drawdown=0.0,
            risk_frozen=False,
            positions={},
        )
        for index, trading_date in enumerate(dates)
    ]
    return market, nav


class BootstrapUncertaintyTest(unittest.TestCase):
    def test_protocol_rejects_non_integer_count_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be integers"):
            BootstrapProtocol(block_length=5.0)

    def test_same_seed_reproduces_schedule_and_intervals(self) -> None:
        market, nav = _market_and_nav()

        first = bootstrap_return_uncertainty(
            market,
            "BENCH",
            nav,
            seed_material="experiment-case-a",
            protocol=TEST_PROTOCOL,
        )
        second = bootstrap_return_uncertainty(
            market,
            "BENCH",
            nav,
            seed_material="experiment-case-a",
            protocol=TEST_PROTOCOL,
        )
        changed_seed = bootstrap_return_uncertainty(
            market,
            "BENCH",
            nav,
            seed_material="experiment-case-b",
            protocol=TEST_PROTOCOL,
        )

        self.assertEqual(first.to_summary(), second.to_summary())
        self.assertNotEqual(
            first.resample_schedule_sha256,
            changed_seed.resample_schedule_sha256,
        )
        self.assertTrue(first.enabled)
        self.assertEqual(first.observation_count, 80)
        self.assertLess(abs(first.strategy_reconciliation_error or 0.0), 1e-12)
        strategy_interval = first.strategy_total_return
        self.assertIsNotNone(strategy_interval)
        self.assertLessEqual(
            strategy_interval.lower,
            strategy_interval.median,
        )
        self.assertLessEqual(
            strategy_interval.median,
            strategy_interval.upper,
        )

    def test_strategy_and_benchmark_use_paired_block_indices(self) -> None:
        market, nav = _market_and_nav(identical_to_benchmark=True)

        result = bootstrap_return_uncertainty(
            market,
            "BENCH",
            nav,
            seed_material="paired-case",
            protocol=TEST_PROTOCOL,
        )

        self.assertEqual(
            result.strategy_total_return,
            result.benchmark_total_return,
        )
        relative_interval = result.strategy_relative_to_benchmark
        self.assertIsNotNone(relative_interval)
        self.assertEqual(
            relative_interval.point_estimate,
            0.0,
        )
        self.assertEqual(relative_interval.lower, 0.0)
        self.assertEqual(relative_interval.upper, 0.0)
        self.assertEqual(
            relative_interval.positive_resample_fraction,
            0.0,
        )

    def test_short_sample_is_disabled_with_auditable_reason(self) -> None:
        market, nav = _market_and_nav(row_count=10)

        result = bootstrap_return_uncertainty(
            market,
            "BENCH",
            nav,
            seed_material="short-case",
            protocol=TEST_PROTOCOL,
        )

        self.assertFalse(result.enabled)
        self.assertEqual(result.benchmark, "BENCH")
        self.assertEqual(result.observation_count, 9)
        self.assertIn("minimum_observations", result.disabled_reason or "")
        self.assertIsNone(result.strategy_total_return)
        self.assertIsNone(result.resample_schedule_sha256)
        self.assertIsNone(result.strategy_reconciliation_error)

    def test_strategy_interval_does_not_require_a_benchmark(self) -> None:
        market, nav = _market_and_nav()

        result = bootstrap_return_uncertainty(
            market,
            None,
            nav,
            seed_material="strategy-only-case",
            protocol=TEST_PROTOCOL,
        )

        self.assertTrue(result.enabled)
        self.assertIsNotNone(result.strategy_total_return)
        self.assertIsNone(result.benchmark_total_return)
        self.assertIsNone(result.strategy_relative_to_benchmark)
        self.assertIsNone(result.benchmark_reconciliation_error)

    def test_short_sample_does_not_bypass_input_validation(self) -> None:
        market, nav = _market_and_nav(row_count=10)

        with self.assertRaisesRegex(
            BootstrapUncertaintyError,
            "missing from bootstrap dates",
        ):
            bootstrap_return_uncertainty(
                market,
                "MISSING",
                nav,
                seed_material="missing-benchmark-case",
                protocol=TEST_PROTOCOL,
            )
        with self.assertRaisesRegex(
            BootstrapUncertaintyError,
            "strictly positive NAV",
        ):
            bootstrap_return_uncertainty(
                market,
                None,
                [replace(nav[0], nav=0.0)],
                seed_material="invalid-nav-case",
                protocol=TEST_PROTOCOL,
            )


if __name__ == "__main__":
    unittest.main()
