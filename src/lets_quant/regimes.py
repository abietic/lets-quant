from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from .models import MarketData, NavRecord


REGIME_NAMES = (
    "insufficient_history",
    "stress",
    "falling",
    "sideways",
    "rising",
)


class RegimeAttributionError(ValueError):
    """Raised when market-regime attribution cannot be reconciled."""


@dataclass(frozen=True)
class RegimeProtocol:
    version: int = 1
    lookback_trading_days: int = 60
    rising_return_threshold: float = 0.02
    falling_return_threshold: float = -0.02
    stress_drawdown_threshold: float = -0.10

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError("only regime protocol version 1 is supported")
        if self.lookback_trading_days <= 0:
            raise ValueError("regime lookback_trading_days must be > 0")
        thresholds = (
            self.rising_return_threshold,
            self.falling_return_threshold,
            self.stress_drawdown_threshold,
        )
        if any(not math.isfinite(value) for value in thresholds):
            raise ValueError("regime thresholds must be finite")
        if self.rising_return_threshold <= 0:
            raise ValueError("rising_return_threshold must be > 0")
        if self.falling_return_threshold >= 0:
            raise ValueError("falling_return_threshold must be < 0")
        if not -1 < self.stress_drawdown_threshold < 0:
            raise ValueError(
                "stress_drawdown_threshold must be between -1 and 0"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


DEFAULT_REGIME_PROTOCOL = RegimeProtocol()


@dataclass(frozen=True)
class RegimeObservation:
    trading_date: date
    information_cutoff_date: Optional[date]
    regime: str
    trailing_benchmark_return: Optional[float]
    trailing_benchmark_drawdown: Optional[float]
    strategy_return: float
    benchmark_return: float
    strategy_log_return: float
    benchmark_log_return: float

    @property
    def excess_log_return(self) -> float:
        return self.strategy_log_return - self.benchmark_log_return


@dataclass(frozen=True)
class RegimeAttribution:
    enabled: bool
    benchmark: Optional[str]
    protocol: RegimeProtocol
    observations: List[RegimeObservation]
    strategy_reconciliation_error: float = 0.0
    benchmark_reconciliation_error: float = 0.0
    disabled_reason: Optional[str] = None

    def regime_summaries(self) -> List[Dict[str, Any]]:
        summaries: List[Dict[str, Any]] = []
        for regime in REGIME_NAMES:
            rows = [row for row in self.observations if row.regime == regime]
            strategy_log_return = sum(
                (row.strategy_log_return for row in rows), 0.0
            )
            benchmark_log_return = sum(
                (row.benchmark_log_return for row in rows), 0.0
            )
            summaries.append(
                {
                    "regime": regime,
                    "day_count": len(rows),
                    "positive_strategy_day_count": sum(
                        1 for row in rows if row.strategy_return > 0
                    ),
                    "strategy_log_return_contribution": strategy_log_return,
                    "strategy_compounded_return": math.expm1(
                        strategy_log_return
                    ),
                    "benchmark_log_return_contribution": benchmark_log_return,
                    "benchmark_compounded_return": math.expm1(
                        benchmark_log_return
                    ),
                    "excess_log_return_contribution": (
                        strategy_log_return - benchmark_log_return
                    ),
                }
            )
        return summaries

    def to_summary(self) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "enabled": self.enabled,
            "benchmark": self.benchmark,
            "protocol": self.protocol.to_dict(),
            "descriptive_only": True,
            "used_for_strategy_decisions": False,
            "label_source": "benchmark_prices_only",
            "label_information_cutoff": "previous_trading_day",
        }
        if not self.enabled:
            return {
                **base,
                "disabled_reason": self.disabled_reason,
                "attributed_day_count": 0,
                "classified_day_count": 0,
                "regimes": [],
            }
        regime_summaries = self.regime_summaries()
        strategy_total_log_return = sum(
            (row.strategy_log_return for row in self.observations), 0.0
        )
        benchmark_total_log_return = sum(
            (row.benchmark_log_return for row in self.observations), 0.0
        )
        unclassified_days = sum(
            1
            for row in self.observations
            if row.regime == "insufficient_history"
        )
        return {
            **base,
            "attributed_day_count": len(self.observations),
            "classified_day_count": len(self.observations) - unclassified_days,
            "unclassified_day_count": unclassified_days,
            "strategy_total_log_return": strategy_total_log_return,
            "strategy_total_return_reconstructed": math.expm1(
                strategy_total_log_return
            ),
            "benchmark_total_log_return": benchmark_total_log_return,
            "benchmark_total_return_reconstructed": math.expm1(
                benchmark_total_log_return
            ),
            "strategy_reconciliation_error": (
                self.strategy_reconciliation_error
            ),
            "benchmark_reconciliation_error": (
                self.benchmark_reconciliation_error
            ),
            "regimes": regime_summaries,
        }


def _regime_evidence(
    market: MarketData,
    benchmark: str,
    trading_date: date,
    protocol: RegimeProtocol,
) -> tuple[str, Optional[date], Optional[float], Optional[float]]:
    indexes = {value: index for index, value in enumerate(market.dates)}
    try:
        current_index = indexes[trading_date]
    except KeyError as exc:
        raise RegimeAttributionError(
            f"regime date is outside market data: {trading_date.isoformat()}"
        ) from exc
    if current_index == 0:
        return "insufficient_history", None, None, None
    cutoff_index = current_index - 1
    cutoff_date = market.dates[cutoff_index]
    if cutoff_index < protocol.lookback_trading_days:
        return "insufficient_history", cutoff_date, None, None
    start_index = cutoff_index - protocol.lookback_trading_days
    history_dates = market.dates[start_index : cutoff_index + 1]
    try:
        history_prices = [
            float(market.prices_on(value)[benchmark]) for value in history_dates
        ]
    except KeyError as exc:
        raise RegimeAttributionError(
            f"benchmark {benchmark} is missing from regime history"
        ) from exc
    if any(not math.isfinite(value) or value <= 0 for value in history_prices):
        raise RegimeAttributionError(
            f"benchmark {benchmark} has an invalid regime price"
        )
    trailing_return = history_prices[-1] / history_prices[0] - 1
    trailing_drawdown = history_prices[-1] / max(history_prices) - 1
    if trailing_drawdown <= protocol.stress_drawdown_threshold:
        regime = "stress"
    elif trailing_return <= protocol.falling_return_threshold:
        regime = "falling"
    elif trailing_return >= protocol.rising_return_threshold:
        regime = "rising"
    else:
        regime = "sideways"
    return regime, cutoff_date, trailing_return, trailing_drawdown


def attribute_market_regimes(
    market: MarketData,
    benchmark: Optional[str],
    nav: Sequence[NavRecord],
    *,
    protocol: RegimeProtocol = DEFAULT_REGIME_PROTOCOL,
) -> RegimeAttribution:
    if benchmark is None:
        return RegimeAttribution(
            enabled=False,
            benchmark=None,
            protocol=protocol,
            observations=[],
            disabled_reason="policy has no benchmark",
        )
    normalized_benchmark = benchmark.strip().upper()
    if not normalized_benchmark:
        raise RegimeAttributionError("benchmark must not be empty")
    if not nav:
        raise RegimeAttributionError("regime attribution requires NAV rows")
    nav_dates = [row.trading_date for row in nav]
    if nav_dates != sorted(set(nav_dates)):
        raise RegimeAttributionError(
            "regime attribution NAV dates must be unique and sorted"
        )

    observations: List[RegimeObservation] = []
    for previous, current in zip(nav, nav[1:]):
        if previous.nav <= 0 or current.nav <= 0:
            raise RegimeAttributionError(
                "regime attribution requires strictly positive NAV"
            )
        try:
            previous_benchmark = float(
                market.prices_on(previous.trading_date)[normalized_benchmark]
            )
            current_benchmark = float(
                market.prices_on(current.trading_date)[normalized_benchmark]
            )
        except KeyError as exc:
            raise RegimeAttributionError(
                f"benchmark {normalized_benchmark} is missing from NAV dates"
            ) from exc
        if (
            not math.isfinite(previous_benchmark)
            or not math.isfinite(current_benchmark)
            or previous_benchmark <= 0
            or current_benchmark <= 0
        ):
            raise RegimeAttributionError(
                f"benchmark {normalized_benchmark} has an invalid price"
            )
        regime, cutoff_date, trailing_return, trailing_drawdown = (
            _regime_evidence(
                market,
                normalized_benchmark,
                current.trading_date,
                protocol,
            )
        )
        strategy_ratio = current.nav / previous.nav
        benchmark_ratio = current_benchmark / previous_benchmark
        observations.append(
            RegimeObservation(
                trading_date=current.trading_date,
                information_cutoff_date=cutoff_date,
                regime=regime,
                trailing_benchmark_return=trailing_return,
                trailing_benchmark_drawdown=trailing_drawdown,
                strategy_return=strategy_ratio - 1,
                benchmark_return=benchmark_ratio - 1,
                strategy_log_return=math.log(strategy_ratio),
                benchmark_log_return=math.log(benchmark_ratio),
            )
        )

    expected_strategy_log_return = math.log(nav[-1].nav / nav[0].nav)
    actual_strategy_log_return = sum(
        (row.strategy_log_return for row in observations), 0.0
    )
    try:
        first_benchmark = float(
            market.prices_on(nav[0].trading_date)[normalized_benchmark]
        )
        last_benchmark = float(
            market.prices_on(nav[-1].trading_date)[normalized_benchmark]
        )
    except KeyError as exc:
        raise RegimeAttributionError(
            f"benchmark {normalized_benchmark} is missing from NAV boundaries"
        ) from exc
    expected_benchmark_log_return = math.log(
        last_benchmark / first_benchmark
    )
    actual_benchmark_log_return = sum(
        (row.benchmark_log_return for row in observations), 0.0
    )
    strategy_error = actual_strategy_log_return - expected_strategy_log_return
    benchmark_error = (
        actual_benchmark_log_return - expected_benchmark_log_return
    )
    if abs(strategy_error) > 1e-12 or abs(benchmark_error) > 1e-12:
        raise RegimeAttributionError(
            "market-regime log-return attribution did not reconcile"
        )
    return RegimeAttribution(
        enabled=True,
        benchmark=normalized_benchmark,
        protocol=protocol,
        observations=observations,
        strategy_reconciliation_error=strategy_error,
        benchmark_reconciliation_error=benchmark_error,
    )
