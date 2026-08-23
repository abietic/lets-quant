import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from lets_quant.data import (
    DataError,
    load_instrument_master,
    load_prices,
    validate_market_coverage,
)


class MarketDataTest(unittest.TestCase):
    def test_instrument_master_is_point_in_time_and_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "instruments.csv"
            path.write_text(
                "symbol,exchange,asset_type,listed_on,delisted_on,available_at\n"
                "510300.XSHG,XSHG,ETF,2012-05-28,,"
                "2012-05-28T09:00:00+08:00\n"
                "159915.XSHE,XSHE,ETF,2011-12-09,,"
                "2026-01-01T09:00:00+08:00\n",
                encoding="utf-8",
            )

            instruments = load_instrument_master(
                path,
                as_of=datetime.fromisoformat("2025-01-01T00:00:00+08:00"),
            )

            self.assertEqual(
                [instrument.symbol for instrument in instruments],
                ["510300.XSHG"],
            )

    def test_instrument_master_rejects_missing_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "instruments.csv"
            path.write_text(
                "symbol,exchange,asset_type,listed_on,delisted_on,available_at\n"
                "510300.XSHG,,ETF,2012-05-28,,"
                "2012-05-28T09:00:00+08:00\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(DataError, "exchange must not be empty"):
                load_instrument_master(path)

    def test_instrument_master_rejects_naive_as_of(self) -> None:
        with self.assertRaisesRegex(DataError, "as_of must include a timezone"):
            load_instrument_master(
                Path("unused.csv"),
                as_of=datetime.fromisoformat("2025-01-01T00:00:00"),
            )

    def test_duplicate_prices_are_rejected(self) -> None:
        content = (
            "date,symbol,close\n"
            "2025-01-02,AAA,10\n"
            "2025-01-02,AAA,11\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "prices.csv"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(DataError, "duplicate price"):
                load_prices(path)

    def test_missing_daily_coverage_is_rejected(self) -> None:
        content = (
            "date,symbol,close\n"
            "2025-01-02,AAA,10\n"
            "2025-01-02,BBB,20\n"
            "2025-01-03,AAA,11\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "prices.csv"
            path.write_text(content, encoding="utf-8")
            market = load_prices(path)
            with self.assertRaisesRegex(DataError, "missing prices"):
                validate_market_coverage(market, {"AAA", "BBB"})


if __name__ == "__main__":
    unittest.main()
