import math
import unittest
from datetime import date, timedelta

from lets_quant.models import MarketData, NavRecord
from lets_quant.regimes import RegimeProtocol, attribute_market_regimes


class MarketRegimeAttributionTest(unittest.TestCase):
    def test_labels_use_only_information_through_previous_day(self) -> None:
        dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(6)]
        benchmark_prices = [100.0, 110.0, 121.0, 60.0, 60.0, 60.0]
        market = MarketData(
            dates=dates,
            prices_by_date={
                trading_date: {"BENCH": benchmark_prices[index]}
                for index, trading_date in enumerate(dates)
            },
        )
        nav_values = [100.0, 101.0, 102.0, 90.0, 91.0, 92.0]
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

        attribution = attribute_market_regimes(
            market,
            "BENCH",
            nav,
            protocol=RegimeProtocol(lookback_trading_days=2),
        )
        by_date = {
            row.trading_date: row for row in attribution.observations
        }

        self.assertEqual(by_date[dates[3]].regime, "rising")
        self.assertEqual(
            by_date[dates[3]].information_cutoff_date, dates[2]
        )
        self.assertEqual(by_date[dates[4]].regime, "stress")
        self.assertEqual(
            by_date[dates[4]].information_cutoff_date, dates[3]
        )

    def test_log_return_contributions_reconcile_total_return(self) -> None:
        dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(5)]
        market = MarketData(
            dates=dates,
            prices_by_date={
                trading_date: {"BENCH": 100.0 + index * 2}
                for index, trading_date in enumerate(dates)
            },
        )
        nav_values = [100.0, 102.0, 101.0, 104.0, 105.0]
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

        attribution = attribute_market_regimes(
            market,
            "BENCH",
            nav,
            protocol=RegimeProtocol(lookback_trading_days=2),
        )
        summary = attribution.to_summary()

        self.assertAlmostEqual(
            summary["strategy_total_return_reconstructed"],
            nav_values[-1] / nav_values[0] - 1,
            places=14,
        )
        self.assertAlmostEqual(
            sum(
                row["strategy_log_return_contribution"]
                for row in summary["regimes"]
            ),
            math.log(nav_values[-1] / nav_values[0]),
            places=14,
        )
        self.assertLess(abs(summary["strategy_reconciliation_error"]), 1e-12)

    def test_missing_benchmark_disables_attribution_explicitly(self) -> None:
        dates = [date(2025, 1, 1), date(2025, 1, 2)]
        market = MarketData(
            dates=dates,
            prices_by_date={value: {"ASSET": 10.0} for value in dates},
        )
        nav = [
            NavRecord(value, 100.0, 100.0, 0.0, False, {})
            for value in dates
        ]

        attribution = attribute_market_regimes(market, None, nav)

        self.assertFalse(attribution.enabled)
        self.assertEqual(attribution.to_summary()["classified_day_count"], 0)


if __name__ == "__main__":
    unittest.main()
