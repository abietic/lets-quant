import unittest
from datetime import date, timedelta

from lets_quant.backtest import run_backtest
from lets_quant.strategies import (
    FutureDataAccessError,
    HistoricalContext,
    build_strategy,
)

from tests.helpers import make_market, make_policy


class StrategyContractTest(unittest.TestCase):
    def test_historical_context_rejects_future_access(self) -> None:
        dates = [date(2025, 1, 2), date(2025, 1, 3)]
        context = HistoricalContext(
            make_market(dates, [{"AAA": 10.0}, {"AAA": 11.0}]),
            dates[0],
        )

        self.assertEqual(context.dates, (dates[0],))
        with self.assertRaisesRegex(FutureDataAccessError, "future prices"):
            context.prices_on(dates[1])

    def test_momentum_filter_blocks_until_warmup_is_complete(self) -> None:
        start = date(2025, 1, 2)
        dates = [start + timedelta(days=index) for index in range(4)]
        market = make_market(
            dates,
            [{"AAA": 10.0 + index} for index in range(4)],
        )
        policy = make_policy(
            strategy_kind="momentum_filter",
            lookback_trading_days=2,
            minimum_momentum=0.0,
        )
        strategy = build_strategy(policy)

        warmup = strategy.decide(HistoricalContext(market, dates[1]))
        ready = strategy.decide(HistoricalContext(market, dates[2]))

        self.assertEqual(warmup.status, "insufficient_history")
        self.assertEqual(ready.status, "ready")
        self.assertEqual(ready.target_weights, {"AAA": 0.5})
        self.assertEqual(
            ready.evidence["observations"]["AAA"]["end_date"],
            dates[2].isoformat(),
        )

    def test_decision_ids_are_deterministic_and_persisted_on_signals(self) -> None:
        start = date(2025, 1, 2)
        dates = [start + timedelta(days=index) for index in range(6)]
        market = make_market(
            dates,
            [{"AAA": 10.0 + index} for index in range(6)],
        )
        policy = make_policy(
            strategy_kind="momentum_filter",
            lookback_trading_days=2,
            minimum_momentum=0.0,
            rebalance_every=1,
        )

        first = run_backtest(policy, market)
        second = run_backtest(policy, market)

        first_ids = [signal.decision_id for signal in first.signals]
        second_ids = [signal.decision_id for signal in second.signals]
        self.assertEqual(first_ids, second_ids)
        self.assertTrue(any(decision_id for decision_id in first_ids))
        ready_signals = [
            signal for signal in first.signals if signal.target_weights
        ]
        self.assertTrue(ready_signals)
        self.assertTrue(
            all(signal.strategy_kind == "momentum_filter" for signal in ready_signals)
        )


if __name__ == "__main__":
    unittest.main()
