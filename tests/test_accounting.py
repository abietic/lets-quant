import random
import unittest
from datetime import date, timedelta

from lets_quant.accounting import AccountingError
from lets_quant.backtest import run_backtest
from lets_quant.models import CorporateAction

from tests.helpers import make_market, make_policy


class AccountingLedgerTest(unittest.TestCase):
    def test_unadjusted_split_and_dividend_are_posted_explicitly(self) -> None:
        dates = [date(2025, 1, day) for day in (2, 3, 6, 7)]
        actions = {
            dates[2]: [
                CorporateAction(
                    symbol="AAA",
                    event_type="split",
                    ex_date=dates[2],
                    ratio=2.0,
                )
            ],
            dates[3]: [
                CorporateAction(
                    symbol="AAA",
                    event_type="cash_dividend",
                    ex_date=dates[3],
                    cash_amount=0.5,
                )
            ],
        }
        market = make_market(
            dates,
            [
                {"AAA": 10.0},
                {"AAA": 10.0},
                {"AAA": 5.0},
                {"AAA": 5.0},
            ],
            corporate_actions_by_date=actions,
            price_adjustment="none",
        )
        policy = make_policy(
            weights={"AAA": 1.0},
            initial_cash=1_000,
            rebalance_every=10,
            max_single_weight=1.0,
            max_gross_exposure=1.0,
        )

        result = run_backtest(policy, market)

        self.assertEqual(result.nav[2].positions["AAA"], 200)
        self.assertEqual(result.nav[3].cash, 100.0)
        self.assertEqual(result.nav[3].nav, 1_100.0)
        self.assertEqual(result.metrics["total_cash_dividends"], 100.0)
        self.assertTrue(result.metrics["accounting_reconciled"])
        self.assertTrue(
            all(record.status == "pass" for record in result.accounting)
        )
        self.assertEqual(
            [
                entry.event_type
                for entry in result.ledger
                if entry.event_type in {"split", "cash_dividend"}
            ],
            ["split", "cash_dividend"],
        )

    def test_adjusted_prices_record_actions_without_double_counting(self) -> None:
        dates = [date(2025, 1, day) for day in (2, 3, 6)]
        action = CorporateAction(
            symbol="AAA",
            event_type="cash_dividend",
            ex_date=dates[2],
            cash_amount=1.0,
        )
        market = make_market(
            dates,
            [{"AAA": 10.0}, {"AAA": 10.0}, {"AAA": 10.0}],
            corporate_actions_by_date={dates[2]: [action]},
            price_adjustment="hfq",
        )
        policy = make_policy(
            weights={"AAA": 1.0},
            initial_cash=1_000,
            rebalance_every=10,
            max_single_weight=1.0,
            max_gross_exposure=1.0,
        )

        result = run_backtest(policy, market)

        self.assertEqual(result.nav[-1].cash, 0.0)
        self.assertEqual(result.metrics["total_cash_dividends"], 0.0)
        self.assertIn(
            "corporate_action_embedded",
            [entry.event_type for entry in result.ledger],
        )

    def test_fractional_reverse_split_fails_closed(self) -> None:
        dates = [date(2025, 1, day) for day in (2, 3, 6)]
        action = CorporateAction(
            symbol="AAA",
            event_type="reverse_split",
            ex_date=dates[2],
            ratio=1 / 3,
        )
        market = make_market(
            dates,
            [{"AAA": 10.0}, {"AAA": 10.0}, {"AAA": 30.0}],
            corporate_actions_by_date={dates[2]: [action]},
        )
        policy = make_policy(
            weights={"AAA": 1.0},
            initial_cash=1_000,
            rebalance_every=10,
            max_single_weight=1.0,
            max_gross_exposure=1.0,
        )

        with self.assertRaisesRegex(AccountingError, "fractional shares"):
            run_backtest(policy, market)

    def test_pending_order_crossing_split_is_rejected_as_stale(self) -> None:
        dates = [date(2025, 1, day) for day in (2, 3, 6)]
        action = CorporateAction(
            symbol="AAA",
            event_type="split",
            ex_date=dates[2],
            ratio=2.0,
        )
        market = make_market(
            dates,
            [{"AAA": 10.0}, {"AAA": 10.0}, {"AAA": 5.0}],
            corporate_actions_by_date={dates[2]: [action]},
        )
        policy = make_policy(
            weights={"AAA": 1.0},
            initial_cash=1_000,
            rebalance_every=10,
            max_single_weight=1.0,
            max_gross_exposure=1.0,
        )

        result = run_backtest(
            policy, market, execution_delay_trading_days=2
        )

        self.assertEqual(result.nav[-1].positions["AAA"], 0)
        self.assertEqual(result.nav[-1].cash, 1_000)
        self.assertEqual(result.trades[0].filled_quantity, 0)
        self.assertEqual(
            result.trades[0].status, "rejected_corporate_action"
        )
        self.assertTrue(result.metrics["accounting_reconciled"])

    def test_seeded_price_paths_preserve_accounting_invariants(self) -> None:
        generator = random.Random(20260820)
        start = date(2025, 1, 2)
        dates = [start + timedelta(days=index) for index in range(120)]
        prices = []
        current = {"AAA": 10.0, "BBB": 20.0}
        for _ in dates:
            current = {
                symbol: max(
                    1.0,
                    price * (1 + generator.uniform(-0.03, 0.03)),
                )
                for symbol, price in current.items()
            }
            prices.append(dict(current))
        market = make_market(dates, prices)
        policy = make_policy(
            weights={"AAA": 0.45, "BBB": 0.45},
            cash_buffer=0.1,
            rebalance_every=5,
            commission_rate=0.0003,
            minimum_commission=1.0,
            sell_tax_rate=0.0005,
            slippage_bps=8.0,
            max_single_weight=0.5,
            max_gross_exposure=0.9,
        )

        result = run_backtest(policy, market)

        self.assertTrue(result.metrics["accounting_reconciled"])
        self.assertLessEqual(
            result.metrics["maximum_accounting_cash_error"], 1e-7
        )
        self.assertLessEqual(
            result.metrics["maximum_accounting_nav_error"], 1e-7
        )
        self.assertTrue(all(record.cash >= 0 for record in result.nav))
        self.assertTrue(
            all(
                quantity >= 0
                for record in result.nav
                for quantity in record.positions.values()
            )
        )


if __name__ == "__main__":
    unittest.main()
