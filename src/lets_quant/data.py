from __future__ import annotations

import csv
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import Holding, InstrumentMetadata, MarketData


INSTRUMENT_COLUMNS = {
    "symbol",
    "exchange",
    "asset_type",
    "listed_on",
    "delisted_on",
    "available_at",
}


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


def _instrument_date(
    value: Any, location: str, *, allow_empty: bool = False
) -> Optional[date]:
    if not isinstance(value, str):
        raise DataError(f"{location} must be YYYY-MM-DD")
    normalized = value.strip()
    if allow_empty and not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise DataError(f"{location} must be YYYY-MM-DD") from exc


def _instrument_timestamp(
    value: Any, location: str, *, allow_empty: bool
) -> Optional[datetime]:
    if not isinstance(value, str):
        raise DataError(f"{location} must be an ISO-8601 timestamp")
    normalized = value.strip()
    if allow_empty and not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DataError(f"{location} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataError(f"{location} must include a timezone")
    return parsed


def load_instrument_master(
    path: Path,
    *,
    as_of: Optional[datetime] = None,
    allow_missing_available_at: bool = False,
) -> List[InstrumentMetadata]:
    if as_of is not None and (
        as_of.tzinfo is None or as_of.utcoffset() is None
    ):
        raise DataError("as_of must include a timezone")
    instruments: List[InstrumentMetadata] = []
    seen = set()
    try:
        handle = path.open(newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise DataError(f"instrument master not found: {path}") from exc

    with handle:
        reader = csv.DictReader(handle)
        _require_header(reader.fieldnames, INSTRUMENT_COLUMNS, path)
        for line_number, row in enumerate(reader, start=2):
            location = f"{path}:{line_number}"
            available_at = _instrument_timestamp(
                row["available_at"],
                f"{location}:available_at",
                allow_empty=allow_missing_available_at,
            )
            if as_of is not None and available_at is None:
                raise DataError(
                    f"{location}:available_at is required for point-in-time filtering"
                )
            if as_of is not None and available_at is not None and available_at > as_of:
                continue
            symbol = row["symbol"].strip().upper()
            exchange = row["exchange"].strip().upper()
            asset_type = row["asset_type"].strip().upper()
            if not symbol:
                raise DataError(f"{location}:symbol must not be empty")
            if symbol in seen:
                raise DataError(f"{location}:duplicate instrument symbol")
            if not exchange:
                raise DataError(f"{location}:exchange must not be empty")
            if not asset_type:
                raise DataError(f"{location}:asset_type must not be empty")
            seen.add(symbol)
            listed_on = _instrument_date(
                row["listed_on"], f"{location}:listed_on"
            )
            delisted_on = _instrument_date(
                row["delisted_on"],
                f"{location}:delisted_on",
                allow_empty=True,
            )
            assert listed_on is not None
            if delisted_on is not None and delisted_on < listed_on:
                raise DataError(
                    f"{location}:delisted_on is earlier than listed_on"
                )
            instruments.append(
                InstrumentMetadata(
                    symbol=symbol,
                    exchange=exchange,
                    asset_type=asset_type,
                    listed_on=listed_on,
                    delisted_on=delisted_on,
                    available_at=available_at,
                )
            )
    return sorted(instruments, key=lambda item: item.symbol)


def generated_instrument_master(
    market: MarketData,
) -> List[InstrumentMetadata]:
    first_date = market.dates[0]
    return [
        InstrumentMetadata(
            symbol=symbol,
            exchange="SYNTH",
            asset_type="SYNTHETIC",
            listed_on=first_date,
            delisted_on=None,
            available_at=None,
        )
        for symbol in market.symbols
    ]
