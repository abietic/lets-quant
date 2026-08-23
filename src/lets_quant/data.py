from __future__ import annotations

import csv
import math
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .models import Holding, MarketData


class DataError(ValueError):
    """Raised when input market or portfolio data is invalid."""


def _require_header(
    actual: Optional[List[str]], expected: Iterable[str], path: Path
) -> None:
    if actual is None:
        raise DataError(f"{path} is empty")
    actual_set = set(actual)
    expected_set = set(expected)
    if actual_set != expected_set:
        raise DataError(
            f"{path} must have exactly these columns: {', '.join(sorted(expected_set))}"
        )


def load_prices(path: Path) -> MarketData:
    prices: Dict[date, Dict[str, float]] = {}
    seen = set()
    try:
        handle = path.open(newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise DataError(f"price file not found: {path}") from exc

    with handle:
        reader = csv.DictReader(handle)
        _require_header(reader.fieldnames, {"date", "symbol", "close"}, path)
        for line_number, row in enumerate(reader, start=2):
            try:
                trading_date = date.fromisoformat(row["date"].strip())
            except (AttributeError, ValueError) as exc:
                raise DataError(
                    f"{path}:{line_number}: date must be YYYY-MM-DD"
                ) from exc

            symbol = row["symbol"].strip().upper()
            if not symbol:
                raise DataError(f"{path}:{line_number}: symbol must not be empty")
            try:
                close = float(row["close"])
            except (TypeError, ValueError) as exc:
                raise DataError(
                    f"{path}:{line_number}: close must be a number"
                ) from exc
            if not math.isfinite(close) or close <= 0:
                raise DataError(
                    f"{path}:{line_number}: close must be finite and > 0"
                )

            key = (trading_date, symbol)
            if key in seen:
                raise DataError(
                    f"{path}:{line_number}: duplicate price for {symbol} on "
                    f"{trading_date.isoformat()}"
                )
            seen.add(key)
            prices.setdefault(trading_date, {})[symbol] = close

    if not prices:
        raise DataError(f"{path} contains no price rows")
    dates = sorted(prices)
    return MarketData(dates=dates, prices_by_date=prices)


def validate_market_coverage(
    market: MarketData, required_symbols: Iterable[str]
) -> None:
    required = set(required_symbols)
    for trading_date in market.dates:
        missing = sorted(required - set(market.prices_on(trading_date)))
        if missing:
            raise DataError(
                f"missing prices on {trading_date.isoformat()}: {', '.join(missing)}"
            )


def load_holdings(path: Path) -> List[Holding]:
    holdings: List[Holding] = []
    seen = set()
    try:
        handle = path.open(newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise DataError(f"holdings file not found: {path}") from exc

    with handle:
        reader = csv.DictReader(handle)
        _require_header(reader.fieldnames, {"symbol", "quantity"}, path)
        for line_number, row in enumerate(reader, start=2):
            symbol = row["symbol"].strip().upper()
            if not symbol:
                raise DataError(f"{path}:{line_number}: symbol must not be empty")
            if symbol in seen:
                raise DataError(
                    f"{path}:{line_number}: duplicate holding for {symbol}"
                )
            seen.add(symbol)
            try:
                quantity = int(row["quantity"])
            except (TypeError, ValueError) as exc:
                raise DataError(
                    f"{path}:{line_number}: quantity must be an integer"
                ) from exc
            if quantity < 0:
                raise DataError(
                    f"{path}:{line_number}: quantity must be >= 0"
                )
            holdings.append(Holding(symbol=symbol, quantity=quantity))
    return holdings
