from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, Mapping, Optional

from .data import DataError
from .models import (
    Holding,
    ManualOrderPlan,
    MarketData,
    OrderRecommendation,
    Policy,
)
from .risk import (
    estimate_turnover,
    rebalance_violations,
    target_quantities,
)
from .strategies import (
    HistoricalContext,
    build_strategy,
    validate_strategy_decision,
)


def _commission(policy: Policy, notional: float) -> float:
    if notional <= 0:
        return 0.0
    return max(
        policy.execution.minimum_commission,
        notional * policy.execution.commission_rate,
    )


def _resolve_date(market: MarketData, requested: Optional[date]) -> date:
    if requested is None:
        return market.dates[-1]
    if requested not in market.prices_by_date:
        raise DataError(
            f"no prices are available for as-of date {requested.isoformat()}"
        )
    return requested


def build_manual_order_plan(
    policy: Policy,
    market: MarketData,
    holdings: Iterable[Holding],
    cash: float,
    as_of: Optional[date] = None,
) -> ManualOrderPlan:
    if cash < 0:
        raise DataError("cash must be >= 0")

    plan_date = _resolve_date(market, as_of)
    prices = market.prices_on(plan_date)
    positions: Dict[str, int] = {
        holding.symbol: holding.quantity for holding in holdings
    }

    missing_prices = sorted(set(positions) - set(prices))
    if missing_prices:
        raise DataError(
            "missing as-of prices for holdings: " + ", ".join(missing_prices)
        )
    missing_targets = sorted(set(policy.strategy.target_weights) - set(prices))
    if missing_targets:
        raise DataError(
            "missing as-of prices for target symbols: "
            + ", ".join(missing_targets)
        )

    nav = cash + sum(
        quantity * prices[symbol] for symbol, quantity in positions.items()
    )
    if nav <= 0:
        raise DataError("portfolio NAV must be > 0")

    decision = build_strategy(policy).decide(
        HistoricalContext(market, plan_date)
    )
    validate_strategy_decision(policy, decision, expected_as_of=plan_date)

    violations = []
    if decision.status != "ready":
        violations.append(
            f"strategy decision is not ready: {decision.reason}"
        )
    unmanaged = sorted(set(positions) - set(policy.strategy.target_weights))
    if unmanaged:
        violations.append(
            "holdings outside the policy require an explicit decision: "
            + ", ".join(unmanaged)
        )

    desired = target_quantities(
        policy, nav, prices, decision.target_weights
    )
    managed_positions: Mapping[str, int] = {
        symbol: positions.get(symbol, 0) for symbol in desired
    }
    turnover = estimate_turnover(
        managed_positions, desired, prices, nav
    )
    violations.extend(rebalance_violations(policy, turnover))

    slippage_fraction = policy.execution.slippage_bps / 10_000
    recommendations = []
    estimated_ending_cash = cash

    for symbol in sorted(desired):
        if decision.status != "ready":
            break
        current = positions.get(symbol, 0)
        target = desired[symbol]
        delta = target - current
        if delta == 0:
            continue

        side = "BUY" if delta > 0 else "SELL"
        quantity = abs(delta)
        reference_price = prices[symbol]
        fill_price = reference_price * (
            1 + slippage_fraction if side == "BUY" else 1 - slippage_fraction
        )
        notional = quantity * fill_price
        fees = _commission(policy, notional)
        if side == "SELL":
            fees += notional * policy.execution.sell_tax_rate
            estimated_ending_cash += notional - fees
        else:
            estimated_ending_cash -= notional + fees

        recommendations.append(
            OrderRecommendation(
                symbol=symbol,
                side=side,
                quantity=quantity,
                reference_price=reference_price,
                estimated_fill_price=fill_price,
                estimated_notional=notional,
                estimated_fees=fees,
                current_quantity=current,
                target_quantity=target,
            )
        )

    non_tradable = sorted(
        order.symbol
        for order in recommendations
        if not market.is_tradable(plan_date, order.symbol)
    )
    if non_tradable:
        violations.append(
            "as-of market state marks target orders non-tradable: "
            + ", ".join(non_tradable)
        )

    required_cash = nav * policy.portfolio.cash_buffer_weight
    if estimated_ending_cash + 1e-9 < required_cash:
        violations.append(
            "estimated ending cash falls below the configured cash buffer"
        )

    if violations:
        status = "blocked"
    elif not recommendations:
        status = "no_action"
    else:
        status = "ready_for_manual_review"

    return ManualOrderPlan(
        as_of=plan_date,
        policy_name=policy.name,
        nav=nav,
        cash=cash,
        estimated_turnover=turnover,
        approval_required=True,
        automatic_execution_allowed=False,
        status=status,
        violations=violations,
        recommendations=recommendations,
        decision_id=decision.decision_id,
        strategy_kind=decision.strategy_kind,
        target_weights=decision.target_weights,
        decision_evidence=decision.evidence,
        diagnostics=decision.diagnostics,
    )
