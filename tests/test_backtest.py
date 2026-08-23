import unittest
from datetime import date

from lets_quant.backtest import run_backtest

from tests.helpers import make_market, make_policy


class BacktestTest(unittest.TestCase):
    def test_signal_executes_on_next_trading_day(self) -> None:
        dates = [
            date(2025, 1, 2),
            date(2025, 1, 3),
            date(2025, 1, 6),
        ]
        market = make_market(
            dates,
            [
                {"AAA": 10.0},
                {"AAA": 11.0},
                {"AAA": 12.0},
            ],
        )

        result = run_backtest(make_policy(), market)

        self.assertTrue(result.metrics["warnings"])
        self.assertEqual(result.signals[0].signal_date, dates[0])
        self.assertEqual(result.trades[0].signal_date, dates[0])
        self.assertEqual(result.trades[0].execution_date, dates[1])
        self.assertEqual(result.trades[0].signal_price, 10.0)
        self.assertEqual(result.trades[0].market_price, 11.0)

    def test_turnover_limit_blocks_rebalance(self) -> None:
        dates = [date(2025, 1, 2), date(2025, 1, 3)]
        market = make_market(dates, [{"AAA": 10.0}, {"AAA": 11.0}])
        policy = make_policy(max_turnover=0.1)

        result = run_backtest(policy, market)

        self.assertEqual(result.signals[0].status, "blocked")
        self.assertIn("turnover", result.signals[0].reason)
        self.assertEqual(result.trades, [])

    def test_drawdown_activates_risk_freeze(self) -> None:
        dates = [
            date(2025, 1, 2),
            date(2025, 1, 3),
            date(2025, 1, 6),
            date(2025, 1, 7),
        ]
        market = make_market(
            dates,
            [
                {"AAA": 10.0},
                {"AAA": 10.0},
                {"AAA": 5.0},
                {"AAA": 5.0},
            ],
        )
        policy = make_policy(
            weights={"AAA": 1.0},
            max_single_weight=1.0,
            max_gross_exposure=1.0,
            max_drawdown=0.2,
        )

        result = run_backtest(policy, market)

        self.assertTrue(result.metrics["risk_frozen"])
        self.assertTrue(result.nav[2].risk_frozen)
        self.assertEqual(result.signals[-1].status, "blocked")
        self.assertIn("drawdown", result.signals[-1].reason)

    def test_non_tradable_execution_is_rejected(self) -> None:
        dates = [date(2025, 1, 2), date(2025, 1, 3)]
        market = make_market(
            dates,
            [{"AAA": 10.0}, {"AAA": 11.0}],
            {dates[0]: {"AAA"}, dates[1]: set()},
        )

        result = run_backtest(make_policy(), market)

        self.assertEqual(result.trades[0].status, "rejected_not_tradable")
        self.assertEqual(result.trades[0].filled_quantity, 0)
        self.assertEqual(result.metrics["filled_trade_count"], 0)

    def test_reports_cash_static_and_benchmark_comparisons(self) -> None:
        dates = [date(2025, 1, 2), date(2025, 1, 3)]
        market = make_market(
            dates,
            [
                {"AAA": 10.0, "BENCH": 100.0},
                {"AAA": 11.0, "BENCH": 105.0},
            ],
        )

        result = run_backtest(
            make_policy(benchmark="BENCH", max_turnover=1.0), market
        )

        self.assertIn("cash", result.metrics["baselines"])
        self.assertIn("static_target_weights", result.metrics["baselines"])
        self.assertIn(
            "strategy_minus_benchmark_total_return",
            result.metrics["comparison"],
        )
        self.assertIn("tracking_error", result.metrics["benchmark"])
        self.assertIn("sortino_ratio", result.metrics)

    def test_execution_delay_uses_requested_future_trading_day(self) -> None:
        dates = [
            date(2025, 1, 2),
            date(2025, 1, 3),
            date(2025, 1, 6),
            date(2025, 1, 7),
        ]
        market = make_market(
            dates,
            [
                {"AAA": 10.0},
                {"AAA": 11.0},
                {"AAA": 12.0},
                {"AAA": 13.0},
            ],
        )

        result = run_backtest(
            make_policy(), market, execution_delay_trading_days=2
        )

        self.assertEqual(result.trades[0].signal_date, dates[0])
        self.assertEqual(result.trades[0].execution_date, dates[2])
        self.assertEqual(result.trades[0].market_price, 12.0)
        self.assertEqual(result.signals[1].status, "blocked")
        self.assertIn("pending", result.signals[1].reason)


if __name__ == "__main__":
    unittest.main()
