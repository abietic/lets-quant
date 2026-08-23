import tempfile
import unittest
from pathlib import Path

from lets_quant.data import DataError, load_prices, validate_market_coverage


class MarketDataTest(unittest.TestCase):
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

