from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from .models import MarketData, Policy, StrategyDecision


class StrategyError(ValueError):
    """Raised when a strategy cannot produce a safe, valid decision."""


class FutureDataAccessError(StrategyError):
    """Raised when a strategy asks for data after its decision timestamp."""


class HistoricalContext:
    """Read-only point-in-time market view presented to a strategy."""

    def __init__(self, market: MarketData, as_of: date) -> None:
        if as_of not in market.dates:
            raise StrategyError(
                f"strategy as-of date is not a trading date: {as_of.isoformat()}"
            )
        self._as_of = as_of
        self._dates: Tuple[date, ...] = tuple(
            trading_date
            for trading_date in market.dates
            if trading_date <= as_of
        )
        self._prices_by_date = {
            trading_date: MappingProxyType(
                dict(market.prices_on(trading_date))
            )
            for trading_date in self._dates
        }

    @property
    def as_of(self) -> date:
        return self._as_of

    @property
    def dates(self) -> Tuple[date, ...]:
        return self._dates

    def prices_on(self, trading_date: date) -> Mapping[str, float]:
        if trading_date > self._as_of:
            raise FutureDataAccessError(
                "strategy attempted to access future prices: "
                f"requested={trading_date.isoformat()}, "
                f"as_of={self._as_of.isoformat()}"
            )
        try:
            return self._prices_by_date[trading_date]
        except KeyError as exc:
            raise StrategyError(
                "strategy requested a date unavailable in its historical "
                f"context: {trading_date.isoformat()}"
            ) from exc

    def latest_prices(self) -> Mapping[str, float]:
        return self.prices_on(self._as_of)

    def trailing_return(
        self, symbol: str, lookback_trading_days: int
    ) -> Optional[Dict[str, Any]]:
        if lookback_trading_days <= 0:
            raise StrategyError("lookback_trading_days must be > 0")
        if len(self._dates) <= lookback_trading_days:
            return None
        start_date = self._dates[-lookback_trading_days - 1]
        end_date = self._dates[-1]
        start_prices = self.prices_on(start_date)
        end_prices = self.prices_on(end_date)
        if symbol not in start_prices or symbol not in end_prices:
            raise StrategyError(
                f"missing price history for strategy symbol: {symbol}"
            )
        start_price = start_prices[symbol]
        end_price = end_prices[symbol]
        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "start_price": start_price,
            "end_price": end_price,
            "return": end_price / start_price - 1,
        }


class Strategy(Protocol):
    kind: str

    def decide(self, context: HistoricalContext) -> StrategyDecision:
        ...


def _canonical_decision_id(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decision(
    *,
    as_of: date,
    strategy_kind: str,
    status: str,
    reason: str,
    target_weights: Mapping[str, float],
    evidence: Mapping[str, Any],
    diagnostics: Sequence[str] = (),
) -> StrategyDecision:
    payload = {
        "as_of": as_of.isoformat(),
        "strategy_kind": strategy_kind,
        "status": status,
        "reason": reason,
        "target_weights": dict(sorted(target_weights.items())),
        "evidence": dict(evidence),
        "diagnostics": list(diagnostics),
    }
    return StrategyDecision(
        decision_id=_canonical_decision_id(payload),
        as_of=as_of,
        strategy_kind=strategy_kind,
        status=status,
        reason=reason,
        target_weights=dict(sorted(target_weights.items())),
        evidence=dict(evidence),
        diagnostics=list(diagnostics),
    )


class FixedWeightStrategy:
    kind = "fixed_weight"

    def __init__(self, target_weights: Mapping[str, float]) -> None:
        self._target_weights = dict(target_weights)

    def decide(self, context: HistoricalContext) -> StrategyDecision:
        return _decision(
            as_of=context.as_of,
            strategy_kind=self.kind,
            status="ready",
            reason="fixed policy target weights",
            target_weights=self._target_weights,
            evidence={
                "method": "policy_target_weights",
                "price_date": context.as_of.isoformat(),
            },
        )


class MomentumFilterStrategy:
    kind = "momentum_filter"

    def __init__(
        self,
        target_weights: Mapping[str, float],
        lookback_trading_days: int,
        minimum_momentum: float,
    ) -> None:
        self._target_weights = dict(target_weights)
        self._lookback_trading_days = lookback_trading_days
        self._minimum_momentum = minimum_momentum

    def decide(self, context: HistoricalContext) -> StrategyDecision:
        observations: Dict[str, Any] = {}
        insufficient: List[str] = []
        selected: List[str] = []
        target_weights: Dict[str, float] = {}

        for symbol, base_weight in sorted(self._target_weights.items()):
            observation = context.trailing_return(
                symbol, self._lookback_trading_days
            )
            if observation is None:
                observations[symbol] = {"status": "insufficient_history"}
                insufficient.append(symbol)
                target_weights[symbol] = 0.0
                continue
            is_selected = observation["return"] >= self._minimum_momentum
            observations[symbol] = {
                **observation,
                "status": "selected" if is_selected else "filtered_to_cash",
            }
            target_weights[symbol] = base_weight if is_selected else 0.0
            if is_selected:
                selected.append(symbol)

        evidence = {
            "method": "trailing_close_return_filter",
            "lookback_trading_days": self._lookback_trading_days,
            "minimum_momentum": self._minimum_momentum,
            "observations": observations,
            "selected_symbols": selected,
        }
        if insufficient:
            diagnostics = [
                "insufficient history for: " + ", ".join(insufficient)
            ]
            return _decision(
                as_of=context.as_of,
                strategy_kind=self.kind,
                status="insufficient_history",
                reason="strategy warm-up is incomplete",
                target_weights=target_weights,
                evidence=evidence,
                diagnostics=diagnostics,
            )

        return _decision(
            as_of=context.as_of,
            strategy_kind=self.kind,
            status="ready",
            reason=(
                "selected positive-momentum assets; unselected allocation "
                "remains in cash"
            ),
            target_weights=target_weights,
            evidence=evidence,
        )


def build_strategy(policy: Policy) -> Strategy:
    strategy_policy = policy.strategy
    if strategy_policy.kind == "fixed_weight":
        return FixedWeightStrategy(strategy_policy.target_weights)
    if strategy_policy.kind == "momentum_filter":
        if strategy_policy.lookback_trading_days is None:
            raise StrategyError(
                "momentum_filter requires lookback_trading_days"
            )
        if strategy_policy.minimum_momentum is None:
            raise StrategyError("momentum_filter requires minimum_momentum")
        return MomentumFilterStrategy(
            strategy_policy.target_weights,
            strategy_policy.lookback_trading_days,
            strategy_policy.minimum_momentum,
        )
    raise StrategyError(f"unsupported strategy kind: {strategy_policy.kind}")


def validate_strategy_decision(
    policy: Policy,
    decision: StrategyDecision,
    *,
    expected_as_of: Optional[date] = None,
) -> None:
    if expected_as_of is not None and decision.as_of != expected_as_of:
        raise StrategyError(
            "strategy decision as_of does not match the historical context"
        )
    if decision.strategy_kind != policy.strategy.kind:
        raise StrategyError(
            "strategy decision kind does not match policy.strategy.kind"
        )
    if decision.status not in {"ready", "insufficient_history"}:
        raise StrategyError(f"unsupported strategy decision status: {decision.status}")

    declared = set(policy.strategy.target_weights)
    actual = set(decision.target_weights)
    if actual != declared:
        missing = sorted(declared - actual)
        unknown = sorted(actual - declared)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise StrategyError(
            "strategy decision symbols do not match policy universe: "
            + "; ".join(details)
        )

    for symbol, weight in decision.target_weights.items():
        if not math.isfinite(weight) or weight < 0:
            raise StrategyError(
                f"strategy weight for {symbol} must be finite and >= 0"
            )
        if weight > policy.risk.max_single_weight + 1e-12:
            raise StrategyError(
                f"strategy weight for {symbol} exceeds max_single_weight"
            )

    gross_exposure = sum(decision.target_weights.values())
    if gross_exposure > policy.risk.max_gross_exposure + 1e-12:
        raise StrategyError(
            "strategy decision exceeds risk.max_gross_exposure"
        )
    if (
        gross_exposure + policy.portfolio.cash_buffer_weight
        > 1 + 1e-12
    ):
        raise StrategyError(
            "strategy decision plus cash buffer exceeds total portfolio weight"
        )
