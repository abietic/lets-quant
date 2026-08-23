import unittest
from datetime import date, timedelta

from lets_quant.backtest import run_backtest
from lets_quant.independent_policy import (
    IndependentPolicyError,
    independent_decision,
    independent_signal,
)
from lets_quant.strategies import HistoricalContext, build_strategy

from tests.helpers import make_market, make_policy


def _history(market, symbols, as_of):
    return {
        symbol: [
            (trading_date.isoformat(), market.prices_on(trading_date)[symbol])
            for trading_date in market.dates
            if trading_date <= as_of
        ]
        for symbol in symbols
    }


def _reference_decision(policy, market, as_of):
    decision = build_strategy(policy).decide(HistoricalContext(market, as_of))
    return {
        "decision_id": decision.decision_id,
        "strategy_kind": decision.strategy_kind,
        "decision_status": decision.status,
        "decision_reason": decision.reason,
        "target_weights": decision.target_weights,
        "decision_evidence": decision.evidence,
        "diagnostics": decision.diagnostics,
    }


class IndependentPolicyTest(unittest.TestCase):
    def test_fixed_weight_decision_matches_reference_implementation(self) -> None:
        dates = [date(2025, 1, 2), date(2025, 1, 3)]
        market = make_market(
            dates,
            [
                {"AAA": 10.0, "BBB": 20.0},
                {"AAA": 11.0, "BBB": 19.0},
            ],
        )
        policy = make_policy(weights={"AAA": 0.5, "BBB": 0.4})
        as_of = dates[0]

        actual = independent_decision(
            policy,
            signal_date=as_of.isoformat(),
            history_by_symbol=_history(
                market, policy.strategy.target_weights, as_of
            ),
        )

        self.assertEqual(actual, _reference_decision(policy, market, as_of))

    def test_momentum_warmup_and_selection_match_reference(self) -> None:
        start = date(2025, 1, 2)
        dates = [start + timedelta(days=index) for index in range(8)]
        market = make_market(
            dates,
            [
                {
                    "UP": 100.0 + index * 2,
                    "DOWN": 100.0 - index * 2,
                }
                for index in range(len(dates))
            ],
        )
        policy = make_policy(
            strategy_kind="momentum_filter",
            weights={"UP": 0.5, "DOWN": 0.4},
            lookback_trading_days=5,
            minimum_momentum=0.0,
        )

        for as_of in (dates[3], dates[-1]):
            with self.subTest(as_of=as_of):
                actual = independent_decision(
                    policy,
                    signal_date=as_of.isoformat(),
                    history_by_symbol=_history(
                        market, policy.strategy.target_weights, as_of
                    ),
                )
                self.assertEqual(
                    actual, _reference_decision(policy, market, as_of)
                )

        selected = independent_decision(
            policy,
            signal_date=dates[-1].isoformat(),
            history_by_symbol=_history(
                market, policy.strategy.target_weights, dates[-1]
            ),
        )
        self.assertEqual(
            selected["decision_evidence"]["selected_symbols"], ["UP"]
        )
        self.assertEqual(selected["target_weights"]["DOWN"], 0.0)

    def test_turnover_block_keeps_proposed_orders_and_matches_reference(self) -> None:
        dates = [date(2025, 1, 2), date(2025, 1, 3)]
        market = make_market(dates, [{"AAA": 10.0}, {"AAA": 11.0}])
        policy = make_policy(max_turnover=0.1)
        reference = run_backtest(policy, market).signals[0]

        actual = independent_signal(
            policy,
            signal_date=dates[0].isoformat(),
            execution_date=dates[1].isoformat(),
            nav=policy.portfolio.initial_cash,
            positions={"AAA": 0},
            current_prices=market.prices_on(dates[0]),
            history_by_symbol=_history(
                market, policy.strategy.target_weights, dates[0]
            ),
            risk_frozen=False,
            pending_order_count=0,
        )

        self.assertEqual(actual["status"], reference.status)
        self.assertEqual(actual["reason"], reference.reason)
        self.assertEqual(
            actual["estimated_turnover"], reference.estimated_turnover
        )
        self.assertEqual(actual["decision_id"], reference.decision_id)
        self.assertEqual(actual["strategy_kind"], reference.strategy_kind)
        self.assertEqual(actual["target_weights"], reference.target_weights)
        self.assertEqual(
            actual["decision_evidence"], reference.decision_evidence
        )
        self.assertEqual(actual["diagnostics"], reference.diagnostics)
        self.assertEqual(
            actual["orders"],
            [
                {
                    "signal_date": order.signal_date.isoformat(),
                    "execution_date": order.execution_date.isoformat(),
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "signal_price": order.signal_price,
                    "reason": order.reason,
                }
                for order in reference.orders
            ],
        )
        self.assertTrue(actual["orders"])

    def test_rejects_future_history_exposure(self) -> None:
        policy = make_policy()

        with self.assertRaisesRegex(
            IndependentPolicyError, "future history exposed"
        ):
            independent_decision(
                policy,
                signal_date="2025-01-02",
                history_by_symbol={
                    "AAA": [
                        ("2025-01-02", 10.0),
                        ("2025-01-03", 11.0),
                    ]
                },
            )


if __name__ == "__main__":
    unittest.main()
