import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from lets_quant.cross_engine import EngineValidationError
from lets_quant.engine_inputs import (
    load_frozen_order_intents,
    load_reference_initial_positions,
    resolve_engine_market_input,
)

from tests.engine_helpers import build_curated_reference


class EngineInputsTest(unittest.TestCase):
    def test_initial_holdings_snapshot_declaration_must_match_file_hash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            holdings_path = temporary / "holdings.csv"
            holdings_path.write_text(
                "symbol,quantity\n510300.XSHG,100\n",
                encoding="utf-8",
            )
            fixture = build_curated_reference(
                temporary / "fixture",
                initial_holdings_path=holdings_path,
            )
            manifest_path = fixture["reference"] / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["initial_holdings_snapshot_sha256"] = "0" * 64
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                EngineValidationError,
                "initial holdings snapshot hash is inconsistent",
            ):
                load_reference_initial_positions(
                    fixture["reference"],
                    symbols=["510300.XSHG", "511010.XSHG"],
                    lot_size=100,
                    adapter_name="test adapter",
                )

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
            instrument = market_input.instruments["510300.XSHG"]
            self.assertEqual(instrument.exchange, "XSHG")
            self.assertEqual(instrument.asset_type, "ETF")
            self.assertEqual(
                market_input.instrument_master_source, "curated_dataset"
            )
            self.assertEqual(len(market_input.instrument_master_sha256), 64)

            with self.assertRaisesRegex(
                EngineValidationError, "pass --dataset"
            ):
                resolve_engine_market_input(
                    fixture["reference"],
                    supplied_prices_path=fixture["prices"],
                    supplied_dataset_path=None,
                    adapter_name="test adapter",
                )

    def test_unadjusted_corporate_actions_are_preserved_for_adapters(self) -> None:
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

            self.assertEqual(market_input.market.price_adjustment, "none")
            actions = market_input.market.corporate_actions_by_date
            self.assertEqual(set(actions or {}), {date(2025, 1, 7)})
            action = actions[date(2025, 1, 7)][0]
            self.assertEqual(action.symbol, "511010.XSHG")
            self.assertEqual(action.event_type, "cash_dividend")
            self.assertEqual(action.cash_amount, 0.05)

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
