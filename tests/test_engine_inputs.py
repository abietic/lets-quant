import json
import tempfile
import unittest
from pathlib import Path

from lets_quant.cross_engine import EngineValidationError
from lets_quant.engine_inputs import (
    load_frozen_order_intents,
    reject_unsupported_unadjusted_actions,
    resolve_engine_market_input,
)

from tests.engine_helpers import build_curated_reference


class EngineInputsTest(unittest.TestCase):
    def test_curated_input_preserves_bound_ohlcv_and_tradability(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            fixture = build_curated_reference(
                temporary,
                suspended_symbol="511010.XSHG",
            )

            market_input = resolve_engine_market_input(
                fixture["reference"],
                supplied_prices_path=None,
                supplied_dataset_path=fixture["dataset"],
                adapter_name="test adapter",
            )

            self.assertEqual(market_input.source_type, "curated_dataset")
            self.assertEqual(market_input.market.price_adjustment, "hfq")
            bar = market_input.bars_by_date["2025-01-03"]["511010.XSHG"]
            self.assertEqual(bar.open, 102.19)
            self.assertEqual(bar.high, 102.28)
            self.assertEqual(bar.volume, 118000)
            self.assertFalse(bar.tradable)
            self.assertIsNotNone(market_input.dataset_snapshot_sha256)

            with self.assertRaisesRegex(
                EngineValidationError, "pass --dataset"
            ):
                resolve_engine_market_input(
                    fixture["reference"],
                    supplied_prices_path=fixture["prices"],
                    supplied_dataset_path=None,
                    adapter_name="test adapter",
                )

    def test_unadjusted_corporate_actions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_curated_reference(
                Path(temp_dir), adjustment="none"
            )
            market_input = resolve_engine_market_input(
                fixture["reference"],
                supplied_prices_path=None,
                supplied_dataset_path=fixture["dataset"],
                adapter_name="test adapter",
            )

            with self.assertRaisesRegex(
                EngineValidationError, "corporate-action accounting"
            ):
                reject_unsupported_unadjusted_actions(
                    market_input, adapter_name="test adapter"
                )

    def test_curated_reference_rejects_another_valid_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference_fixture = build_curated_reference(
                temporary / "reference-fixture",
                suspended_symbol="511010.XSHG",
            )
            other_fixture = build_curated_reference(
                temporary / "other-fixture",
                suspended_symbol=None,
            )

            with self.assertRaisesRegex(
                EngineValidationError, "differs from the reference snapshot"
            ):
                resolve_engine_market_input(
                    reference_fixture["reference"],
                    supplied_prices_path=None,
                    supplied_dataset_path=other_fixture["dataset"],
                    adapter_name="test adapter",
                )

    def test_frozen_loader_ignores_blocked_proposed_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            signals_path = Path(temp_dir) / "signals.csv"
            proposed_order = {
                "signal_date": "2025-01-02",
                "execution_date": "2025-01-03",
                "symbol": "AAA",
                "side": "BUY",
                "quantity": 100,
                "signal_price": 10.0,
                "reason": "fixed_weight_rebalance",
            }
            signals_path.write_text(
                "signal_date,execution_date,status,orders\n"
                '2025-01-02,2025-01-03,blocked,"'
                + json.dumps([proposed_order]).replace('"', '""')
                + '"\n',
                encoding="utf-8",
            )

            intents = load_frozen_order_intents(
                signals_path,
                lot_size=100,
                symbols=["AAA"],
                trading_dates=["2025-01-02", "2025-01-03"],
                market_prices={"2025-01-02": {"AAA": 10.0}},
                adapter_name="test adapter",
            )

            self.assertEqual(intents, [])


if __name__ == "__main__":
    unittest.main()
