from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any, Dict, Mapping, Sequence, Tuple

from .models import Policy


class IndependentPolicyError(ValueError):
    """Raised when an independent engine cannot form a safe PIT decision."""


PriceObservation = Tuple[str, float]


def _decision_id(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decision(
    *,
    as_of: str,
    strategy_kind: str,
    status: str,
    reason: str,
    target_weights: Mapping[str, float],
    evidence: Mapping[str, Any],
    diagnostics: Sequence[str] = (),
) -> Dict[str, Any]:
    payload = {
        "as_of": as_of,
        "strategy_kind": strategy_kind,
        "status": status,
        "reason": reason,
        "target_weights": dict(sorted(target_weights.items())),
        "evidence": dict(evidence),
        "diagnostics": list(diagnostics),
    }
    return {
        "decision_id": _decision_id(payload),
        "strategy_kind": strategy_kind,
        "decision_status": status,
        "decision_reason": reason,
        "target_weights": payload["target_weights"],
        "decision_evidence": payload["evidence"],
        "diagnostics": payload["diagnostics"],
    }


def _validated_history(
    *,
    signal_date: str,
    symbols: Sequence[str],
    history_by_symbol: Mapping[str, Sequence[PriceObservation]],
) -> Dict[str, Tuple[PriceObservation, ...]]:
    try:
        as_of = date.fromisoformat(signal_date)
    except ValueError as exc:
        raise IndependentPolicyError("signal_date must be ISO-8601") from exc
    if set(history_by_symbol) != set(symbols):
        raise IndependentPolicyError(
            "PIT history symbols must exactly match the strategy universe"
        )
    normalized: Dict[str, Tuple[PriceObservation, ...]] = {}
    for symbol in symbols:
        observations = []
        previous_date = None
        for raw_date, raw_price in history_by_symbol[symbol]:
            try:
                observation_date = date.fromisoformat(str(raw_date))
            except ValueError as exc:
                raise IndependentPolicyError(
                    f"invalid history date for {symbol}: {raw_date}"
                ) from exc
            if observation_date > as_of:
                raise IndependentPolicyError(
                    f"future history exposed for {symbol}: {observation_date}"
                )
            if previous_date is not None and observation_date <= previous_date:
                raise IndependentPolicyError(
                    f"history dates must increase for {symbol}"
                )
            if isinstance(raw_price, bool):
                raise IndependentPolicyError(
                    f"history price must be numeric for {symbol}"
                )
            try:
                price = float(raw_price)
            except (TypeError, ValueError) as exc:
                raise IndependentPolicyError(
                    f"history price must be numeric for {symbol}"
                ) from exc
            if not math.isfinite(price) or price <= 0:
                raise IndependentPolicyError(
                    f"history price must be finite and positive for {symbol}"
                )
            observations.append((observation_date.isoformat(), price))
            previous_date = observation_date
        if not observations or observations[-1][0] != signal_date:
            raise IndependentPolicyError(
                f"PIT history for {symbol} must end on {signal_date}"
            )
        normalized[symbol] = tuple(observations)
    return normalized


def independent_decision(
    policy: Policy,
    *,
    signal_date: str,
    history_by_symbol: Mapping[str, Sequence[PriceObservation]],
) -> Dict[str, Any]:
    symbols = sorted(policy.strategy.target_weights)
    history = _validated_history(
        signal_date=signal_date,
        symbols=symbols,
        history_by_symbol=history_by_symbol,
    )
    strategy = policy.strategy
    if strategy.kind == "fixed_weight":
        return _decision(
            as_of=signal_date,
            strategy_kind="fixed_weight",
            status="ready",
            reason="fixed policy target weights",
            target_weights=strategy.target_weights,
            evidence={
                "method": "policy_target_weights",
                "price_date": signal_date,
            },
        )
    if strategy.kind != "momentum_filter":
        raise IndependentPolicyError(
            f"unsupported independent strategy kind: {strategy.kind}"
        )
    if strategy.lookback_trading_days is None:
        raise IndependentPolicyError(
            "momentum_filter requires lookback_trading_days"
        )
    if strategy.minimum_momentum is None:
        raise IndependentPolicyError(
            "momentum_filter requires minimum_momentum"
        )

    lookback = strategy.lookback_trading_days
    observations: Dict[str, Any] = {}
    insufficient = []
    selected = []
    target_weights: Dict[str, float] = {}
    for symbol in symbols:
        series = history[symbol]
        if len(series) <= lookback:
            observations[symbol] = {"status": "insufficient_history"}
            insufficient.append(symbol)
            target_weights[symbol] = 0.0
            continue
        start_date, start_price = series[-lookback - 1]
        end_date, end_price = series[-1]
        trailing_return = end_price / start_price - 1
        is_selected = trailing_return >= strategy.minimum_momentum
        observations[symbol] = {
            "start_date": start_date,
            "end_date": end_date,
            "start_price": start_price,
            "end_price": end_price,
            "return": trailing_return,
            "status": "selected" if is_selected else "filtered_to_cash",
        }
        target_weights[symbol] = (
            strategy.target_weights[symbol] if is_selected else 0.0
        )
        if is_selected:
            selected.append(symbol)

    evidence = {
        "method": "trailing_close_return_filter",
        "lookback_trading_days": lookback,
        "minimum_momentum": strategy.minimum_momentum,
        "observations": observations,
        "selected_symbols": selected,
    }
    if insufficient:
        return _decision(
            as_of=signal_date,
            strategy_kind="momentum_filter",
            status="insufficient_history",
            reason="strategy warm-up is incomplete",
            target_weights=target_weights,
            evidence=evidence,
            diagnostics=[
                "insufficient history for: " + ", ".join(insufficient)
            ],
        )
    return _decision(
        as_of=signal_date,
        strategy_kind="momentum_filter",
        status="ready",
        reason=(
            "selected positive-momentum assets; unselected allocation "
            "remains in cash"
        ),
        target_weights=target_weights,
        evidence=evidence,
    )


def independent_signal(
    policy: Policy,
    *,
    signal_date: str,
    execution_date: str,
    nav: float,
    positions: Mapping[str, int],
    current_prices: Mapping[str, float],
    history_by_symbol: Mapping[str, Sequence[PriceObservation]],
    risk_frozen: bool,
    pending_order_count: int,
) -> Dict[str, Any]:
    symbols = sorted(policy.strategy.target_weights)
    if set(current_prices) != set(symbols):
        raise IndependentPolicyError(
            "current prices must exactly match the strategy universe"
        )
    if set(positions) != set(symbols):
        raise IndependentPolicyError(
            "positions must exactly match the strategy universe"
        )
    if not math.isfinite(nav) or nav <= 0:
        raise IndependentPolicyError("NAV must be finite and positive")
    for symbol in symbols:
        if positions[symbol] < 0:
            raise IndependentPolicyError("short positions are unsupported")
        price = float(current_prices[symbol])
        if not math.isfinite(price) or price <= 0:
            raise IndependentPolicyError(
                f"current price must be finite and positive for {symbol}"
            )
    base = {
        "signal_date": signal_date,
        "execution_date": execution_date,
        "estimated_turnover": 0.0,
        "decision_id": "",
        "strategy_kind": "",
        "target_weights": {},
        "decision_evidence": {},
        "diagnostics": [],
        "orders": [],
    }
    if risk_frozen:
        return {
            **base,
            "status": "blocked",
            "reason": "maximum drawdown risk freeze is active",
        }
    if pending_order_count:
        return {
            **base,
            "status": "blocked",
            "reason": "an earlier rebalance is still pending execution",
            "strategy_kind": policy.strategy.kind,
        }

    decision = independent_decision(
        policy,
        signal_date=signal_date,
        history_by_symbol=history_by_symbol,
    )
    decision_fields = {
        "decision_id": decision["decision_id"],
        "strategy_kind": decision["strategy_kind"],
        "target_weights": decision["target_weights"],
        "decision_evidence": decision["decision_evidence"],
        "diagnostics": decision["diagnostics"],
    }
    if decision["decision_status"] != "ready":
        return {
            **base,
            **decision_fields,
            "status": "blocked",
            "reason": decision["decision_reason"],
        }

    desired: Dict[str, int] = {}
    lot_size = policy.execution.lot_size
    for symbol in symbols:
        raw_quantity = (
            nav * decision["target_weights"][symbol]
            / float(current_prices[symbol])
        )
        desired[symbol] = math.floor(raw_quantity / lot_size) * lot_size
    turnover_notional = sum(
        abs(desired[symbol] - positions[symbol])
        * float(current_prices[symbol])
        for symbol in symbols
    )
    turnover = turnover_notional / nav
    orders = []
    for symbol in symbols:
        delta = desired[symbol] - positions[symbol]
        if delta == 0:
            continue
        orders.append(
            {
                "signal_date": signal_date,
                "execution_date": execution_date,
                "symbol": symbol,
                "side": "BUY" if delta > 0 else "SELL",
                "quantity": abs(delta),
                "signal_price": float(current_prices[symbol]),
                "reason": f"{policy.strategy.kind}_rebalance",
            }
        )
    if turnover > policy.risk.max_turnover_per_rebalance + 1e-12:
        status = "blocked"
        reason = (
            f"estimated turnover {turnover:.4f} exceeds limit "
            f"{policy.risk.max_turnover_per_rebalance:.4f}"
        )
    elif not orders:
        status = "no_action"
        reason = "portfolio already matches target lots"
    else:
        status = "accepted"
        reason = "passed pre-trade risk checks"
    return {
        **base,
        **decision_fields,
        "status": status,
        "estimated_turnover": turnover,
        "reason": reason,
        "orders": orders,
    }
