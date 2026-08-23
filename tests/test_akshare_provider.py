import csv
import io
import sys
import types
import unittest
from datetime import date
from unittest.mock import patch

from lets_quant.providers import DailyBarsRequest
from lets_quant.providers.akshare import AkshareEtfDailyBarsProvider


class _FakeFrame:
    columns = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]

    def to_dict(self, orient):
        if orient != "records":
            raise AssertionError("unexpected orientation")
        return [
            {
                "日期": "2025-01-02",
                "开盘": 3.9,
                "收盘": 3.94,
                "最高": 3.96,
                "最低": 3.88,
                "成交量": 1000,
                "成交额": 3930,
            }
        ]


class AkshareProviderTest(unittest.TestCase):
    def test_adapter_normalizes_daily_etf_bars(self) -> None:
        module = types.ModuleType("akshare")
        module.__version__ = "test-version"
        module.fund_etf_hist_em = lambda **kwargs: _FakeFrame()

        with patch.dict(sys.modules, {"akshare": module}):
            provider = AkshareEtfDailyBarsProvider()
            payload = provider.fetch_daily_bars(
                DailyBarsRequest(
                    symbols=["510300.XSHG"],
                    start_date=date(2025, 1, 2),
                    end_date=date(2025, 1, 2),
                    adjustment="hfq",
                )
            )

        rows = list(
            csv.DictReader(io.StringIO(payload.content.decode("utf-8")))
        )
        self.assertEqual(payload.provider_version, "test-version")
        self.assertEqual(rows[0]["symbol"], "510300.XSHG")
        self.assertEqual(rows[0]["adjustment"], "hfq")
        self.assertEqual(
            rows[0]["available_at"], "2025-01-02T15:30:00+08:00"
        )


if __name__ == "__main__":
    unittest.main()
