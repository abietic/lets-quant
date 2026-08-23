from __future__ import annotations

import csv
import io
import math
from datetime import date
from typing import Any, Dict, List

from ..data import DataError
from . import DailyBarsRequest, ProviderPayload


class AkshareEtfDailyBarsProvider:
    """Optional AKShare adapter; importing the core package stays dependency-free."""

    name = "akshare"

    def __init__(self) -> None:
        try:
            import akshare  # type: ignore
        except ImportError as exc:
            raise DataError(
                "AKShare is not installed; run "
                "python3 -m pip install -e '.[akshare]'"
            ) from exc
        self._akshare = akshare
        self.version = str(getattr(akshare, "__version__", "unknown"))

    @staticmethod
    def _provider_symbol(symbol: str) -> str:
        code = symbol.split(".", 1)[0]
        if len(code) != 6 or not code.isdigit():
            raise DataError(
                f"AKShare ETF adapter requires a six-digit symbol: {symbol}"
            )
        return code

    @staticmethod
    def _number(value: Any, field: str, symbol: str, trading_date: date) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise DataError(
                f"AKShare returned non-numeric {field} for "
                f"{symbol} on {trading_date}"
            ) from exc
        if not math.isfinite(result):
            raise DataError(
                f"AKShare returned non-finite {field} for "
                f"{symbol} on {trading_date}"
            )
        return result

    def fetch_daily_bars(
        self, request: DailyBarsRequest
    ) -> ProviderPayload:
        if request.start_date > request.end_date:
            raise DataError("AKShare start_date must not be after end_date")
        if request.adjustment not in {"none", "qfq", "hfq"}:
            raise DataError("AKShare adjustment must be none, qfq, or hfq")
        if not request.symbols:
            raise DataError("AKShare request must contain at least one symbol")

        required_columns = {
            "日期",
            "开盘",
            "收盘",
            "最高",
            "最低",
            "成交量",
            "成交额",
        }
        adjustment_argument = (
            "" if request.adjustment == "none" else request.adjustment
        )
        output_rows: List[Dict[str, Any]] = []
        for symbol in sorted(set(request.symbols)):
            frame = self._akshare.fund_etf_hist_em(
                symbol=self._provider_symbol(symbol),
                period="daily",
                start_date=request.start_date.strftime("%Y%m%d"),
                end_date=request.end_date.strftime("%Y%m%d"),
                adjust=adjustment_argument,
            )
            columns = set(str(column) for column in frame.columns)
            missing = sorted(required_columns - columns)
            if missing:
                raise DataError(
                    f"AKShare response for {symbol} is missing columns: "
                    f"{', '.join(missing)}"
                )
            for raw in frame.to_dict(orient="records"):
                raw_date = raw["日期"]
                try:
                    trading_date = date.fromisoformat(str(raw_date)[:10])
                except ValueError as exc:
                    raise DataError(
                        f"AKShare returned an invalid date for {symbol}: "
                        f"{raw_date!r}"
                    ) from exc
                output_rows.append(
                    {
                        "date": trading_date.isoformat(),
                        "symbol": symbol,
                        "open": self._number(
                            raw["开盘"], "open", symbol, trading_date
                        ),
                        "high": self._number(
                            raw["最高"], "high", symbol, trading_date
                        ),
                        "low": self._number(
                            raw["最低"], "low", symbol, trading_date
                        ),
                        "close": self._number(
                            raw["收盘"], "close", symbol, trading_date
                        ),
                        "volume": self._number(
                            raw["成交量"], "volume", symbol, trading_date
                        ),
                        "amount": self._number(
                            raw["成交额"], "amount", symbol, trading_date
                        ),
                        # Daily bars are used only after a conservative close lag.
                        "available_at": (
                            f"{trading_date.isoformat()}T15:30:00+08:00"
                        ),
                        "adjustment": request.adjustment,
                    }
                )
        if not output_rows:
            raise DataError("AKShare returned no ETF daily bars")
        output_rows.sort(key=lambda row: (row["date"], row["symbol"]))

        buffer = io.StringIO(newline="")
        fieldnames = [
            "date",
            "symbol",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "available_at",
            "adjustment",
        ]
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)
        return ProviderPayload(
            content=buffer.getvalue().encode("utf-8"),
            filename="bars.csv",
            dataset="etf_daily_bars",
            provider=self.name,
            provider_version=self.version,
            request={
                "symbols": sorted(set(request.symbols)),
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
                "period": "daily",
                "adjustment": request.adjustment,
                "adapter": "fund_etf_hist_em",
                "availability_lag": "market_close_plus_30_minutes",
            },
        )
