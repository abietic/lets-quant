from __future__ import annotations

import math
from typing import Dict, List, Mapping, Optional

from .models import Policy


def floor_to_lot(raw_quantity: float, lot_size: int) -> int:
    if raw_quantity <= 0:
        return 0
    lots = math.floor(raw_quantity / lot_size)
    return lots * lot_size


def target_quantities(
    policy: Policy,
    nav: float,
    prices: Mapping[str, float],
    target_weights: Optional[Mapping[str, float]] = None,
) -> Dict[str, int]:
    weights = (
        policy.strategy.target_weights
        if target_weights is None
        else target_weights
    )
    quantities: Dict[str, int] = {}
    for symbol, weight in weights.items():
        quantities[symbol] = floor_to_lot(
            nav * weight / prices[symbol], policy.execution.lot_size
        )
    return quantities


def estimate_turnover(
    positions: Mapping[str, int],
    desired: Mapping[str, int],
    prices: Mapping[str, float],
    nav: float,
) -> float:
    if nav <= 0:
        return float("inf")
    notional = sum(
        abs(desired.get(symbol, 0) - positions.get(symbol, 0)) * prices[symbol]
        for symbol in desired
    )
    return notional / nav


def rebalance_violations(policy: Policy, turnover: float) -> List[str]:
    violations: List[str] = []
    if turnover > policy.risk.max_turnover_per_rebalance + 1e-12:
        violations.append(
            "estimated turnover "
            f"{turnover:.4f} exceeds limit "
            f"{policy.risk.max_turnover_per_rebalance:.4f}"
        )
    return violations
