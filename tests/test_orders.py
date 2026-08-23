import unittest
from datetime import date

from lets_quant.models import Holding
from lets_quant.orders import build_manual_order_plan

from tests.helpers import make_market, make_policy


class ManualOrderPlanTest(unittest.TestCase):
    def test_plan_always_requires_manual_approval(self) -> None:
        trading_date = date(2025, 1, 2)
        market = make_market(
            [trading_date],
            [{"AAA": 10.0, "BBB": 20.0}],
        )
        policy = make_policy(
            weights={"AAA": 0.5, "BBB": 0.4},
            cash_buffer=0.1,
            lot_size=10,
            max_single_weight=0.6,
            max_gross_exposure=0.9,
        )

        plan = build_manual_order_plan(
            policy,
            market,
            [Holding("AAA", 10), Holding("BBB", 10)],
            cash=800,
        )

        self.assertEqual(plan.status, "ready_for_manual_review")
        self.assertTrue(plan.approval_required)
        self.assertFalse(plan.automatic_execution_allowed)
        self.assertGreater(len(plan.recommendations), 0)
        self.assertTrue(
            all(order.quantity % 10 == 0 for order in plan.recommendations)
        )

    def test_unmanaged_holding_blocks_plan(self) -> None:
        trading_date = date(2025, 1, 2)
        market = make_market(
            [trading_date],
            [{"AAA": 10.0, "EXTRA": 30.0}],
        )
        policy = make_policy(weights={"AAA": 0.5})

        plan = build_manual_order_plan(
            policy,
            market,
            [Holding("EXTRA", 1)],
            cash=1_000,
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("EXTRA", plan.violations[0])
        self.assertFalse(plan.automatic_execution_allowed)
        self.assertNotIn(
            "EXTRA", {order.symbol for order in plan.recommendations}
        )

    def test_suspended_target_blocks_plan(self) -> None:
        trading_date = date(2025, 1, 2)
        market = make_market(
            [trading_date],
            [{"AAA": 10.0}],
            {trading_date: set()},
        )

        plan = build_manual_order_plan(
            make_policy(weights={"AAA": 0.5}),
            market,
            [],
            cash=1_000,
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("non-tradable", plan.violations[0])


if __name__ == "__main__":
    unittest.main()
