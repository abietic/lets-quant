from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence

from .models import MarketData, NavRecord


class BootstrapUncertaintyError(ValueError):
    """Raised when bootstrap uncertainty inputs or results are invalid."""


@dataclass(frozen=True)
class BootstrapProtocol:
    version: int = 1
    method: str = "circular_moving_block"
    block_length: int = 20
    resample_count: int = 1000
    confidence_level: float = 0.95
    minimum_observations: int = 60

    def __post_init__(self) -> None:
        integer_fields = {
            "version": self.version,
            "block_length": self.block_length,
            "resample_count": self.resample_count,
            "minimum_observations": self.minimum_observations,
        }
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in integer_fields.values()
        ):
            raise ValueError("bootstrap protocol count fields must be integers")
        if self.version != 1:
            raise ValueError("only bootstrap protocol version 1 is supported")
        if self.method != "circular_moving_block":
            raise ValueError("unsupported bootstrap method")
        if self.block_length <= 0:
            raise ValueError("bootstrap block_length must be > 0")
        if self.resample_count < 100:
            raise ValueError("bootstrap resample_count must be >= 100")
        if (
            isinstance(self.confidence_level, bool)
            or not isinstance(self.confidence_level, (int, float))
            or not math.isfinite(self.confidence_level)
            or not 0 < self.confidence_level < 1
        ):
            raise ValueError("bootstrap confidence_level must be between 0 and 1")
        if self.minimum_observations < self.block_length * 2:
            raise ValueError(
                "bootstrap minimum_observations must cover at least two blocks"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


DEFAULT_BOOTSTRAP_PROTOCOL = BootstrapProtocol()


@dataclass(frozen=True)
class BootstrapInterval:
    point_estimate: float
    lower: float
    median: float
    upper: float
    positive_resample_fraction: float

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class BootstrapUncertainty:
    enabled: bool
    protocol: BootstrapProtocol
    observation_count: int
    seed_sha256: str
    benchmark: Optional[str] = None
    strategy_total_return: Optional[BootstrapInterval] = None
    benchmark_total_return: Optional[BootstrapInterval] = None
    strategy_relative_to_benchmark: Optional[BootstrapInterval] = None
    resample_schedule_sha256: Optional[str] = None
    replicates_sha256: Optional[str] = None
    strategy_reconciliation_error: Optional[float] = None
    benchmark_reconciliation_error: Optional[float] = None
    disabled_reason: Optional[str] = None

    def to_summary(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "protocol": self.protocol.to_dict(),
            "observation_count": self.observation_count,
            "seed_sha256": self.seed_sha256,
            "benchmark": self.benchmark,
            "resample_schedule_sha256": self.resample_schedule_sha256,
            "replicates_sha256": self.replicates_sha256,
            "strategy_total_return": (
                self.strategy_total_return.to_dict()
                if self.strategy_total_return is not None
                else None
            ),
            "benchmark_total_return": (
                self.benchmark_total_return.to_dict()
                if self.benchmark_total_return is not None
                else None
            ),
            "strategy_relative_to_benchmark": (
                self.strategy_relative_to_benchmark.to_dict()
                if self.strategy_relative_to_benchmark is not None
                else None
            ),
            "strategy_reconciliation_error": self.strategy_reconciliation_error,
            "benchmark_reconciliation_error": (
                self.benchmark_reconciliation_error
            ),
            "disabled_reason": self.disabled_reason,
            "descriptive_only": True,
            "investment_validity_established": False,
            "p_value_reported": False,
            "annualized": False,
        }


def _seed_sha256(
    seed_material: str, protocol: BootstrapProtocol
) -> str:
    if not isinstance(seed_material, str) or not seed_material.strip():
        raise BootstrapUncertaintyError(
            "bootstrap seed_material must be a non-empty string"
        )
    payload = json.dumps(
        {
            "seed_material": seed_material,
            "protocol": protocol.to_dict(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_benchmark(benchmark: Optional[str]) -> Optional[str]:
    if benchmark is None:
        return None
    if not isinstance(benchmark, str) or not benchmark.strip():
        raise BootstrapUncertaintyError(
            "benchmark must be a non-empty string or null"
        )
    return benchmark.strip().upper()


def disabled_bootstrap_uncertainty(
    *,
    observation_count: int,
    seed_material: str,
    reason: str,
    benchmark: Optional[str] = None,
    protocol: BootstrapProtocol = DEFAULT_BOOTSTRAP_PROTOCOL,
) -> BootstrapUncertainty:
    if (
        isinstance(observation_count, bool)
        or not isinstance(observation_count, int)
        or observation_count < 0
    ):
        raise BootstrapUncertaintyError("observation_count must be >= 0")
    if not isinstance(reason, str) or not reason.strip():
        raise BootstrapUncertaintyError("disabled bootstrap reason must not be empty")
    benchmark_symbol = _normalize_benchmark(benchmark)
    return BootstrapUncertainty(
        enabled=False,
        protocol=protocol,
        observation_count=observation_count,
        seed_sha256=_seed_sha256(seed_material, protocol),
        benchmark=benchmark_symbol,
        disabled_reason=reason,
    )


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise BootstrapUncertaintyError("bootstrap samples must not be empty")
    if not 0 <= probability <= 1:
        raise BootstrapUncertaintyError("quantile probability must be in [0, 1]")
    position = (len(sorted_values) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    fraction = position - lower_index
    return (
        sorted_values[lower_index] * (1 - fraction)
        + sorted_values[upper_index] * fraction
    )


def _interval(
    point_estimate: float,
    samples: Sequence[float],
    confidence_level: float,
) -> BootstrapInterval:
    ordered = sorted(samples)
    alpha = (1 - confidence_level) / 2
    return BootstrapInterval(
        point_estimate=point_estimate,
        lower=_quantile(ordered, alpha),
        median=_quantile(ordered, 0.5),
        upper=_quantile(ordered, 1 - alpha),
        positive_resample_fraction=(
            sum(1 for value in samples if value > 0) / len(samples)
        ),
    )


def _block_start(
    seed_sha256: str,
    resample_index: int,
    block_index: int,
    observation_count: int,
) -> int:
    payload = (
        f"{seed_sha256}:{resample_index}:{block_index}".encode("ascii")
    )
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big") % observation_count


def _daily_log_returns(nav: Sequence[NavRecord]) -> List[float]:
    if not nav:
        raise BootstrapUncertaintyError("bootstrap requires NAV rows")
    dates = [row.trading_date for row in nav]
    if dates != sorted(set(dates)):
        raise BootstrapUncertaintyError(
            "bootstrap NAV dates must be unique and sorted"
        )
    for row in nav:
        if not math.isfinite(row.nav) or row.nav <= 0:
            raise BootstrapUncertaintyError(
                "bootstrap requires finite, strictly positive NAV"
            )
    return [
        math.log(current.nav / previous.nav)
        for previous, current in zip(nav, nav[1:])
    ]


def _benchmark_log_returns(
    market: MarketData,
    benchmark: str,
    nav: Sequence[NavRecord],
) -> List[float]:
    prices: List[float] = []
    for row in nav:
        try:
            raw_price = market.prices_on(row.trading_date)[benchmark]
        except KeyError as exc:
            raise BootstrapUncertaintyError(
                f"benchmark {benchmark} is missing from bootstrap dates"
            ) from exc
        try:
            price = float(raw_price)
        except (TypeError, ValueError) as exc:
            raise BootstrapUncertaintyError(
                f"benchmark {benchmark} has an invalid bootstrap price"
            ) from exc
        if not math.isfinite(price) or price <= 0:
            raise BootstrapUncertaintyError(
                f"benchmark {benchmark} has an invalid bootstrap price"
            )
        prices.append(price)
    return [
        math.log(current / previous)
        for previous, current in zip(prices, prices[1:])
    ]


def bootstrap_return_uncertainty(
    market: MarketData,
    benchmark: Optional[str],
    nav: Sequence[NavRecord],
    *,
    seed_material: str,
    protocol: BootstrapProtocol = DEFAULT_BOOTSTRAP_PROTOCOL,
) -> BootstrapUncertainty:
    benchmark_symbol = _normalize_benchmark(benchmark)
    strategy_logs = _daily_log_returns(nav)
    observation_count = len(strategy_logs)
    benchmark_logs = (
        _benchmark_log_returns(market, benchmark_symbol, nav)
        if benchmark_symbol is not None
        else None
    )
    if benchmark_logs is not None and len(benchmark_logs) != observation_count:
        raise BootstrapUncertaintyError(
            "strategy and benchmark bootstrap returns must align"
        )
    if observation_count < protocol.minimum_observations:
        return disabled_bootstrap_uncertainty(
            observation_count=observation_count,
            seed_material=seed_material,
            reason=(
                "fewer daily returns than bootstrap minimum_observations"
            ),
            benchmark=benchmark_symbol,
            protocol=protocol,
        )

    strategy_point_log = sum(strategy_logs, 0.0)
    strategy_point = math.expm1(strategy_point_log)
    strategy_expected = nav[-1].nav / nav[0].nav - 1
    strategy_error = strategy_point - strategy_expected
    if abs(strategy_error) > 1e-12:
        raise BootstrapUncertaintyError(
            "strategy bootstrap point return did not reconcile"
        )
    benchmark_point_log = (
        sum(benchmark_logs, 0.0) if benchmark_logs is not None else None
    )
    benchmark_point = (
        math.expm1(benchmark_point_log)
        if benchmark_point_log is not None
        else None
    )
    benchmark_error: Optional[float] = None
    if benchmark_logs is not None and benchmark_symbol is not None:
        first_price = float(
            market.prices_on(nav[0].trading_date)[benchmark_symbol]
        )
        last_price = float(
            market.prices_on(nav[-1].trading_date)[benchmark_symbol]
        )
        benchmark_expected = last_price / first_price - 1
        assert benchmark_point is not None
        benchmark_error = benchmark_point - benchmark_expected
        if abs(benchmark_error) > 1e-12:
            raise BootstrapUncertaintyError(
                "benchmark bootstrap point return did not reconcile"
            )

    seed_sha256 = _seed_sha256(seed_material, protocol)
    strategy_samples: List[float] = []
    benchmark_samples: List[float] = []
    relative_samples: List[float] = []
    schedule_digest = hashlib.sha256()
    replicate_digest = hashlib.sha256()
    for resample_index in range(protocol.resample_count):
        sampled = 0
        block_index = 0
        strategy_log_total = 0.0
        benchmark_log_total = 0.0
        while sampled < observation_count:
            start = _block_start(
                seed_sha256,
                resample_index,
                block_index,
                observation_count,
            )
            take = min(protocol.block_length, observation_count - sampled)
            for offset in range(take):
                source_index = (start + offset) % observation_count
                schedule_digest.update(source_index.to_bytes(8, "big"))
                strategy_log_total += strategy_logs[source_index]
                if benchmark_logs is not None:
                    benchmark_log_total += benchmark_logs[source_index]
            sampled += take
            block_index += 1
        schedule_digest.update(b"\n")
        try:
            strategy_sample = math.expm1(strategy_log_total)
            benchmark_sample = (
                math.expm1(benchmark_log_total)
                if benchmark_logs is not None
                else None
            )
            relative_sample = (
                math.expm1(strategy_log_total - benchmark_log_total)
                if benchmark_logs is not None
                else None
            )
        except OverflowError as exc:
            raise BootstrapUncertaintyError(
                "bootstrap resample return overflowed"
            ) from exc
        strategy_samples.append(strategy_sample)
        if benchmark_sample is not None and relative_sample is not None:
            benchmark_samples.append(benchmark_sample)
            relative_samples.append(relative_sample)
        replicate_digest.update(format(strategy_sample, ".17g").encode("ascii"))
        replicate_digest.update(b",")
        if benchmark_sample is not None and relative_sample is not None:
            replicate_digest.update(
                format(benchmark_sample, ".17g").encode("ascii")
            )
            replicate_digest.update(b",")
            replicate_digest.update(
                format(relative_sample, ".17g").encode("ascii")
            )
        replicate_digest.update(b"\n")

    relative_point = (
        math.expm1(strategy_point_log - benchmark_point_log)
        if benchmark_point_log is not None
        else None
    )
    return BootstrapUncertainty(
        enabled=True,
        protocol=protocol,
        observation_count=observation_count,
        seed_sha256=seed_sha256,
        benchmark=benchmark_symbol,
        strategy_total_return=_interval(
            strategy_point,
            strategy_samples,
            protocol.confidence_level,
        ),
        benchmark_total_return=(
            _interval(
                benchmark_point,
                benchmark_samples,
                protocol.confidence_level,
            )
            if benchmark_point is not None
            else None
        ),
        strategy_relative_to_benchmark=(
            _interval(
                relative_point,
                relative_samples,
                protocol.confidence_level,
            )
            if relative_point is not None
            else None
        ),
        resample_schedule_sha256=schedule_digest.hexdigest(),
        replicates_sha256=replicate_digest.hexdigest(),
        strategy_reconciliation_error=strategy_error,
        benchmark_reconciliation_error=benchmark_error,
    )
