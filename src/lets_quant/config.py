from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from .models import (
    ExecutionPolicy,
    Policy,
    PortfolioPolicy,
    RiskPolicy,
    StrategyPolicy,
)


class PolicyError(ValueError):
    """Raised when a policy is invalid or unsafe for this MVP."""


def _expect_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{path} must be a JSON object")
    return value


def _require_keys(data: Mapping[str, Any], keys: Iterable[str], path: str) -> None:
    missing = sorted(set(keys) - set(data))
    if missing:
        raise PolicyError(f"{path} is missing required keys: {', '.join(missing)}")


def _reject_unknown(
    data: Mapping[str, Any], allowed: Iterable[str], path: str
) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise PolicyError(f"{path} has unknown keys: {', '.join(unknown)}")


def _finite_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(f"{path} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise PolicyError(f"{path} must be finite")
    return number


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PolicyError(f"{path} must be a positive integer")
    return value


def _fraction(value: Any, path: str, *, allow_one: bool = True) -> float:
    number = _finite_number(value, path)
    upper_ok = number <= 1 if allow_one else number < 1
    if number < 0 or not upper_ok:
        operator = "<= 1" if allow_one else "< 1"
        raise PolicyError(f"{path} must be >= 0 and {operator}")
    return number


def load_policy(path: Path) -> Policy:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyError(f"policy file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PolicyError(
            f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}"
        ) from exc

    root = _expect_mapping(raw, "policy")
    root_keys = {
        "schema_version",
        "name",
        "base_currency",
        "strategy",
        "portfolio",
        "execution",
        "risk",
    }
    _require_keys(root, root_keys, "policy")
    _reject_unknown(root, root_keys, "policy")

    strategy_raw = _expect_mapping(root["strategy"], "policy.strategy")
    strategy_keys = {
        "kind",
        "target_weights",
        "rebalance_every_n_trading_days",
        "lookback_trading_days",
        "minimum_momentum",
    }
    _require_keys(
        strategy_raw,
        {"kind", "target_weights", "rebalance_every_n_trading_days"},
        "policy.strategy",
    )
    _reject_unknown(strategy_raw, strategy_keys, "policy.strategy")

    weights_raw = _expect_mapping(
        strategy_raw["target_weights"], "policy.strategy.target_weights"
    )
    if not weights_raw:
        raise PolicyError("policy.strategy.target_weights must not be empty")
    weights: Dict[str, float] = {}
    for symbol, raw_weight in weights_raw.items():
        if not isinstance(symbol, str) or not symbol.strip():
            raise PolicyError("target weight symbols must be non-empty strings")
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol in weights:
            raise PolicyError(
                "target weight symbols must be unique after normalization: "
                f"{normalized_symbol}"
            )
        weights[normalized_symbol] = _fraction(
            raw_weight, f"target_weights.{symbol}"
        )

    portfolio_raw = _expect_mapping(root["portfolio"], "policy.portfolio")
    portfolio_keys = {"initial_cash", "benchmark", "cash_buffer_weight"}
    _require_keys(portfolio_raw, portfolio_keys, "policy.portfolio")
    _reject_unknown(portfolio_raw, portfolio_keys, "policy.portfolio")

    execution_raw = _expect_mapping(root["execution"], "policy.execution")
    execution_keys = {
        "mode",
        "lot_size",
        "commission_rate",
        "minimum_commission",
        "sell_tax_rate",
        "slippage_bps",
    }
    _require_keys(execution_raw, execution_keys, "policy.execution")
    _reject_unknown(execution_raw, execution_keys, "policy.execution")

    risk_raw = _expect_mapping(root["risk"], "policy.risk")
    risk_keys = {
        "max_single_weight",
        "max_gross_exposure",
        "max_turnover_per_rebalance",
        "max_drawdown",
    }
    _require_keys(risk_raw, risk_keys, "policy.risk")
    _reject_unknown(risk_raw, risk_keys, "policy.risk")

    schema_version = root["schema_version"]
    if schema_version != 1:
        raise PolicyError("only policy schema_version 1 is supported")

    name = root["name"]
    if not isinstance(name, str) or not name.strip():
        raise PolicyError("policy.name must be a non-empty string")

    currency = root["base_currency"]
    if not isinstance(currency, str) or len(currency.strip()) != 3:
        raise PolicyError("policy.base_currency must be a 3-letter currency code")

    strategy_kind = strategy_raw["kind"]
    if strategy_kind not in {"fixed_weight", "momentum_filter"}:
        raise PolicyError(
            "policy.strategy.kind must be fixed_weight or momentum_filter"
        )

    lookback_trading_days = strategy_raw.get("lookback_trading_days")
    minimum_momentum = strategy_raw.get("minimum_momentum")
    if strategy_kind == "fixed_weight":
        if lookback_trading_days is not None or minimum_momentum is not None:
            raise PolicyError(
                "fixed_weight does not accept lookback_trading_days or "
                "minimum_momentum"
            )
    else:
        if lookback_trading_days is None:
            raise PolicyError(
                "momentum_filter requires lookback_trading_days"
            )
        lookback_trading_days = _positive_int(
            lookback_trading_days,
            "policy.strategy.lookback_trading_days",
        )
        if minimum_momentum is None:
            raise PolicyError("momentum_filter requires minimum_momentum")
        minimum_momentum = _finite_number(
            minimum_momentum, "policy.strategy.minimum_momentum"
        )
        if minimum_momentum < -1:
            raise PolicyError(
                "policy.strategy.minimum_momentum must be >= -1"
            )

    initial_cash = _finite_number(
        portfolio_raw["initial_cash"], "policy.portfolio.initial_cash"
    )
    if initial_cash <= 0:
        raise PolicyError("policy.portfolio.initial_cash must be > 0")

    benchmark = portfolio_raw["benchmark"]
    if benchmark is not None:
        if not isinstance(benchmark, str) or not benchmark.strip():
            raise PolicyError("policy.portfolio.benchmark must be null or a symbol")
        benchmark = benchmark.strip().upper()

    mode = execution_raw["mode"]
    if mode not in {"manual", "paper"}:
        raise PolicyError(
            "policy.execution.mode must be manual or paper; live is unsupported"
        )

    policy = Policy(
        schema_version=1,
        name=name.strip(),
        base_currency=currency.strip().upper(),
        strategy=StrategyPolicy(
            kind=strategy_kind,
            target_weights=weights,
            rebalance_every_n_trading_days=_positive_int(
                strategy_raw["rebalance_every_n_trading_days"],
                "policy.strategy.rebalance_every_n_trading_days",
            ),
            lookback_trading_days=lookback_trading_days,
            minimum_momentum=minimum_momentum,
        ),
        portfolio=PortfolioPolicy(
            initial_cash=initial_cash,
            benchmark=benchmark,
            cash_buffer_weight=_fraction(
                portfolio_raw["cash_buffer_weight"],
                "policy.portfolio.cash_buffer_weight",
                allow_one=False,
            ),
        ),
        execution=ExecutionPolicy(
            mode=mode,
            lot_size=_positive_int(
                execution_raw["lot_size"], "policy.execution.lot_size"
            ),
            commission_rate=_fraction(
                execution_raw["commission_rate"],
                "policy.execution.commission_rate",
            ),
            minimum_commission=_finite_number(
                execution_raw["minimum_commission"],
                "policy.execution.minimum_commission",
            ),
            sell_tax_rate=_fraction(
                execution_raw["sell_tax_rate"],
                "policy.execution.sell_tax_rate",
            ),
            slippage_bps=_finite_number(
                execution_raw["slippage_bps"],
                "policy.execution.slippage_bps",
            ),
        ),
        risk=RiskPolicy(
            max_single_weight=_fraction(
                risk_raw["max_single_weight"], "policy.risk.max_single_weight"
            ),
            max_gross_exposure=_fraction(
                risk_raw["max_gross_exposure"],
                "policy.risk.max_gross_exposure",
            ),
            max_turnover_per_rebalance=_finite_number(
                risk_raw["max_turnover_per_rebalance"],
                "policy.risk.max_turnover_per_rebalance",
            ),
            max_drawdown=_fraction(
                risk_raw["max_drawdown"],
                "policy.risk.max_drawdown",
                allow_one=False,
            ),
        ),
    )
    validate_policy(policy)
    return policy


def validate_policy(policy: Policy) -> None:
    if policy.execution.minimum_commission < 0:
        raise PolicyError("policy.execution.minimum_commission must be >= 0")
    if policy.execution.slippage_bps < 0:
        raise PolicyError("policy.execution.slippage_bps must be >= 0")
    if policy.execution.slippage_bps >= 10_000:
        raise PolicyError("policy.execution.slippage_bps must be < 10000")
    if policy.risk.max_turnover_per_rebalance <= 0:
        raise PolicyError("policy.risk.max_turnover_per_rebalance must be > 0")
    if policy.risk.max_single_weight <= 0:
        raise PolicyError("policy.risk.max_single_weight must be > 0")
    if policy.risk.max_gross_exposure <= 0:
        raise PolicyError("policy.risk.max_gross_exposure must be > 0")
    if policy.risk.max_drawdown <= 0:
        raise PolicyError("policy.risk.max_drawdown must be > 0")

    target_total = sum(policy.strategy.target_weights.values())
    if target_total <= 0:
        raise PolicyError("target weight total must be > 0")
    if target_total > policy.risk.max_gross_exposure + 1e-12:
        raise PolicyError(
            "target weight total exceeds risk.max_gross_exposure"
        )
    if target_total + policy.portfolio.cash_buffer_weight > 1 + 1e-12:
        raise PolicyError(
            "target weights plus cash_buffer_weight must not exceed 1"
        )

    oversized = sorted(
        symbol
        for symbol, weight in policy.strategy.target_weights.items()
        if weight > policy.risk.max_single_weight + 1e-12
    )
    if oversized:
        raise PolicyError(
            "target weights exceed risk.max_single_weight: "
            + ", ".join(oversized)
        )
