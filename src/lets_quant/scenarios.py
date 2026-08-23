from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from .models import MarketData


SUPPORTED_SYNTHETIC_SCENARIOS = (
    "trend_up",
    "trend_down",
    "sideways",
    "crash_recovery",
    "regime_shift",
    "suspension",
)


@dataclass(frozen=True)
class SyntheticMarket:
    market: MarketData
    metadata: Dict[str, Any]


def _trading_dates(start_date: date, count: int) -> List[date]:
    dates: List[date] = []
    candidate = start_date
    while len(dates) < count:
        if candidate.weekday() < 5:
            dates.append(candidate)
        candidate += timedelta(days=1)
    return dates


def _asset_level(
    scenario: str,
    index: int,
    trading_days: int,
    rank: int,
    phase_offset: float,
) -> float:
    phase = phase_offset + rank * 0.9
    wave = 1 + 0.01 * math.sin(index / 9 + phase)

    if scenario == "trend_up":
        return math.exp((0.0007 + rank * 0.00008) * index) * wave
    if scenario == "trend_down":
        return math.exp((-0.00055 - rank * 0.00004) * index) * wave
    if scenario == "sideways":
        return 1 + 0.06 * math.sin(index / 22 + phase)
    if scenario == "crash_recovery":
        crash_index = int(trading_days * 0.45)
        base = math.exp(0.00045 * index) * wave
        if index < crash_index:
            return base
        recovery = math.exp(0.0013 * (index - crash_index))
        return base * 0.62 * recovery
    if scenario == "regime_shift":
        midpoint = trading_days // 2
        first_rate = 0.0009 if rank % 2 == 0 else -0.00015
        second_rate = -0.00035 if rank % 2 == 0 else 0.00105
        if index <= midpoint:
            return math.exp(first_rate * index) * wave
        first_half = math.exp(first_rate * midpoint)
        return first_half * math.exp(second_rate * (index - midpoint)) * wave
    if scenario == "suspension":
        return math.exp((0.00035 + rank * 0.00005) * index) * wave
    raise ValueError(f"unsupported synthetic scenario: {scenario}")


def generate_synthetic_market(
    scenario: str,
    *,
    start_date: date,
    trading_days: int,
    symbols: Sequence[str],
    benchmark: Optional[str] = None,
    seed: int = 0,
) -> SyntheticMarket:
    if scenario not in SUPPORTED_SYNTHETIC_SCENARIOS:
        raise ValueError(f"unsupported synthetic scenario: {scenario}")
    if trading_days < 20:
        raise ValueError("synthetic trading_days must be >= 20")
    normalized_symbols = [symbol.strip().upper() for symbol in symbols]
    if not normalized_symbols or any(not symbol for symbol in normalized_symbols):
        raise ValueError("synthetic symbols must be non-empty")
    if len(set(normalized_symbols)) != len(normalized_symbols):
        raise ValueError("synthetic symbols must be unique")

    benchmark_symbol = benchmark.strip().upper() if benchmark else None
    dates = _trading_dates(start_date, trading_days)
    asset_symbols = [
        symbol for symbol in normalized_symbols if symbol != benchmark_symbol
    ]
    if not asset_symbols:
        asset_symbols = list(normalized_symbols)
        benchmark_symbol = None

    phase_offset = (seed % 997) / 997 * 2 * math.pi
    initial_prices = {
        symbol: 80.0 + rank * 20.0
        for rank, symbol in enumerate(asset_symbols)
    }
    prices_by_date: Dict[date, Dict[str, float]] = {}
    for index, trading_date in enumerate(dates):
        daily_prices: Dict[str, float] = {}
        normalized_levels = []
        for rank, symbol in enumerate(asset_symbols):
            level = _asset_level(
                scenario,
                index,
                trading_days,
                rank,
                phase_offset,
            )
            price = initial_prices[symbol] * level
            daily_prices[symbol] = round(max(0.01, price), 6)
            normalized_levels.append(level)
        if benchmark_symbol is not None:
            daily_prices[benchmark_symbol] = round(
                100.0 * sum(normalized_levels) / len(normalized_levels), 6
            )
        prices_by_date[trading_date] = daily_prices

    tradable_by_date = {
        trading_date: set(normalized_symbols) for trading_date in dates
    }
    suspended_symbol = None
    suspended_range = None
    if scenario == "suspension":
        suspended_symbol = asset_symbols[0]
        suspension_start = int(trading_days * 0.45)
        suspension_end = int(trading_days * 0.55)
        for trading_date in dates[suspension_start:suspension_end]:
            tradable_by_date[trading_date].discard(suspended_symbol)
        suspended_range = {
            "start": dates[suspension_start].isoformat(),
            "end": dates[suspension_end - 1].isoformat(),
        }

    market = MarketData(
        dates=dates,
        prices_by_date=prices_by_date,
        tradable_by_date=tradable_by_date,
    )
    return SyntheticMarket(
        market=market,
        metadata={
            "type": "deterministic_synthetic_market",
            "scenario": scenario,
            "start_date": dates[0].isoformat(),
            "end_date": dates[-1].isoformat(),
            "trading_days": trading_days,
            "symbols": normalized_symbols,
            "benchmark": benchmark_symbol,
            "seed": seed,
            "suspended_symbol": suspended_symbol,
            "suspended_range": suspended_range,
            "investment_validity": False,
            "purpose": "software semantics and robustness testing only",
        },
    )
