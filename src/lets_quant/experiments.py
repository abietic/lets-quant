from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .backtest import run_backtest
from .config import PolicyError, validate_policy
from .models import BacktestResult, MarketData, Policy
from .regimes import (
    DEFAULT_REGIME_PROTOCOL,
    REGIME_NAMES,
    RegimeAttribution,
    RegimeAttributionError,
    attribute_market_regimes,
)
from .uncertainty import (
    DEFAULT_BOOTSTRAP_PROTOCOL,
    BootstrapInterval,
    BootstrapUncertainty,
    BootstrapUncertaintyError,
    bootstrap_return_uncertainty,
    disabled_bootstrap_uncertainty,
)


class ExperimentError(ValueError):
    """Raised when an experiment definition or evaluation is invalid."""


@dataclass(frozen=True)
class EvaluationWindow:
    name: str
    role: str
    start: date
    end: date
    fold: str = "default"

    def to_dict(self, *, include_fold: bool = True) -> Dict[str, Any]:
        payload = {
            "name": self.name,
            "role": self.role,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }
        if include_fold:
            payload["fold"] = self.fold
        return payload


@dataclass(frozen=True)
class ParameterVariant:
    name: str
    rebalance_every_n_trading_days: Optional[int] = None
    lookback_trading_days: Optional[int] = None
    minimum_momentum: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionScenario:
    name: str
    commission_rate: Optional[float] = None
    minimum_commission: Optional[float] = None
    sell_tax_rate: Optional[float] = None
    slippage_bps: Optional[float] = None
    execution_delay_trading_days: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExperimentSpec:
    schema_version: int
    name: str
    seed: int
    windows: List[EvaluationWindow]
    execution_scenarios: List[ExecutionScenario]
    parameter_variants: List[ParameterVariant]

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "name": self.name,
            "seed": self.seed,
            "windows": [
                window.to_dict(include_fold=self.schema_version >= 2)
                for window in self.windows
            ],
            "execution_scenarios": [
                scenario.to_dict() for scenario in self.execution_scenarios
            ],
        }
        if self.schema_version >= 2:
            payload["parameter_variants"] = [
                variant.to_dict() for variant in self.parameter_variants
            ]
        return payload


@dataclass(frozen=True)
class ExperimentCaseResult:
    case_id: str
    window: EvaluationWindow
    execution_scenario: ExecutionScenario
    parameter_variant: ParameterVariant
    result: BacktestResult
    regime_attribution: RegimeAttribution
    bootstrap_uncertainty: BootstrapUncertainty


@dataclass(frozen=True)
class ExperimentResult:
    experiment_input_id: str
    result_sha256: str
    spec: ExperimentSpec
    cases: List[ExperimentCaseResult]
    summary: Dict[str, Any]


def _expect_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ExperimentError(f"{path} must be a JSON object")
    return value


def _expect_keys(
    value: Mapping[str, Any],
    *,
    required: Sequence[str],
    allowed: Sequence[str],
    path: str,
) -> None:
    missing = sorted(set(required) - set(value))
    if missing:
        raise ExperimentError(
            f"{path} is missing required keys: {', '.join(missing)}"
        )
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ExperimentError(
            f"{path} has unknown keys: {', '.join(unknown)}"
        )


def _parse_date(value: Any, path: str) -> date:
    if not isinstance(value, str):
        raise ExperimentError(f"{path} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ExperimentError(f"{path} must be YYYY-MM-DD") from exc


def _optional_number(value: Any, path: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExperimentError(f"{path} must be a number or null")
    number = float(value)
    if not math.isfinite(number):
        raise ExperimentError(f"{path} must be finite")
    return number


def _optional_positive_int(value: Any, path: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExperimentError(f"{path} must be a positive integer or null")
    return value


def load_experiment_spec(path: Path) -> ExperimentSpec:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExperimentError(f"experiment file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ExperimentError(
            f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}"
        ) from exc

    root = _expect_mapping(raw, "experiment")
    common_root_keys = {
        "schema_version",
        "name",
        "seed",
        "windows",
        "execution_scenarios",
    }
    schema_version = root.get("schema_version")
    if isinstance(schema_version, bool) or schema_version not in {1, 2}:
        raise ExperimentError(
            "only experiment schema_version 1 and 2 are supported"
        )
    root_keys = set(common_root_keys)
    if schema_version >= 2:
        root_keys.add("parameter_variants")
    _expect_keys(
        root,
        required=tuple(root_keys),
        allowed=tuple(root_keys),
        path="experiment",
    )
    name = root["name"]
    if not isinstance(name, str) or not name.strip():
        raise ExperimentError("experiment.name must be a non-empty string")
    seed = root["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ExperimentError("experiment.seed must be an integer")

    windows_raw = root["windows"]
    if not isinstance(windows_raw, list) or not windows_raw:
        raise ExperimentError("experiment.windows must be a non-empty array")
    windows: List[EvaluationWindow] = []
    for index, item in enumerate(windows_raw):
        path_prefix = f"experiment.windows[{index}]"
        window_raw = _expect_mapping(item, path_prefix)
        keys = (
            ("name", "role", "start", "end", "fold")
            if schema_version >= 2
            else ("name", "role", "start", "end")
        )
        _expect_keys(
            window_raw,
            required=keys,
            allowed=keys,
            path=path_prefix,
        )
        window_name = window_raw["name"]
        role = window_raw["role"]
        if not isinstance(window_name, str) or not window_name.strip():
            raise ExperimentError(f"{path_prefix}.name must not be empty")
        if role not in {"train", "validation", "test"}:
            raise ExperimentError(
                f"{path_prefix}.role must be train, validation, or test"
            )
        start = _parse_date(window_raw["start"], f"{path_prefix}.start")
        end = _parse_date(window_raw["end"], f"{path_prefix}.end")
        if start > end:
            raise ExperimentError(f"{path_prefix}.start must be <= end")
        fold = (
            window_raw["fold"] if schema_version >= 2 else "default"
        )
        if not isinstance(fold, str) or not fold.strip():
            raise ExperimentError(f"{path_prefix}.fold must not be empty")
        windows.append(
            EvaluationWindow(
                window_name.strip(), role, start, end, fold.strip()
            )
        )

    if len({window.name for window in windows}) != len(windows):
        raise ExperimentError("experiment window names must be unique")
    fold_names = list(dict.fromkeys(window.fold for window in windows))
    chronological: List[EvaluationWindow] = []
    test_windows: List[EvaluationWindow] = []
    for fold_name in fold_names:
        fold_windows = sorted(
            (
                window for window in windows if window.fold == fold_name
            ),
            key=lambda window: window.start,
        )
        roles = [window.role for window in fold_windows]
        if roles != ["train", "validation", "test"]:
            raise ExperimentError(
                "each experiment fold must define chronological train, "
                "validation, and test windows"
            )
        for previous, current in zip(fold_windows, fold_windows[1:]):
            if previous.end >= current.start:
                raise ExperimentError(
                    "experiment windows within a fold must not overlap"
                )
        chronological.extend(fold_windows)
        test_windows.append(fold_windows[-1])
    for previous, current in zip(test_windows, test_windows[1:]):
        if (
            current.start <= previous.start
            or current.end <= previous.end
        ):
            raise ExperimentError(
                "walk-forward fold test windows must advance through time"
            )

    scenarios_raw = root["execution_scenarios"]
    if not isinstance(scenarios_raw, list) or not scenarios_raw:
        raise ExperimentError(
            "experiment.execution_scenarios must be a non-empty array"
        )
    scenarios: List[ExecutionScenario] = []
    scenario_keys = {
        "name",
        "commission_rate",
        "minimum_commission",
        "sell_tax_rate",
        "slippage_bps",
        "execution_delay_trading_days",
    }
    for index, item in enumerate(scenarios_raw):
        path_prefix = f"experiment.execution_scenarios[{index}]"
        scenario_raw = _expect_mapping(item, path_prefix)
        _expect_keys(
            scenario_raw,
            required=("name",),
            allowed=tuple(scenario_keys),
            path=path_prefix,
        )
        scenario_name = scenario_raw["name"]
        if not isinstance(scenario_name, str) or not scenario_name.strip():
            raise ExperimentError(f"{path_prefix}.name must not be empty")
        delay = scenario_raw.get("execution_delay_trading_days", 1)
        if isinstance(delay, bool) or not isinstance(delay, int) or delay <= 0:
            raise ExperimentError(
                f"{path_prefix}.execution_delay_trading_days must be > 0"
            )
        commission_rate = _optional_number(
            scenario_raw.get("commission_rate"),
            f"{path_prefix}.commission_rate",
        )
        minimum_commission = _optional_number(
            scenario_raw.get("minimum_commission"),
            f"{path_prefix}.minimum_commission",
        )
        sell_tax_rate = _optional_number(
            scenario_raw.get("sell_tax_rate"),
            f"{path_prefix}.sell_tax_rate",
        )
        slippage_bps = _optional_number(
            scenario_raw.get("slippage_bps"),
            f"{path_prefix}.slippage_bps",
        )
        if commission_rate is not None and not 0 <= commission_rate <= 1:
            raise ExperimentError(f"{path_prefix}.commission_rate is invalid")
        if minimum_commission is not None and minimum_commission < 0:
            raise ExperimentError(
                f"{path_prefix}.minimum_commission must be >= 0"
            )
        if sell_tax_rate is not None and not 0 <= sell_tax_rate <= 1:
            raise ExperimentError(f"{path_prefix}.sell_tax_rate is invalid")
        if slippage_bps is not None and not 0 <= slippage_bps < 10_000:
            raise ExperimentError(f"{path_prefix}.slippage_bps is invalid")
        scenarios.append(
            ExecutionScenario(
                name=scenario_name.strip(),
                commission_rate=commission_rate,
                minimum_commission=minimum_commission,
                sell_tax_rate=sell_tax_rate,
                slippage_bps=slippage_bps,
                execution_delay_trading_days=delay,
            )
        )
    if len({scenario.name for scenario in scenarios}) != len(scenarios):
        raise ExperimentError("execution scenario names must be unique")

    if schema_version == 1:
        parameter_variants = [ParameterVariant(name="configured")]
    else:
        variants_raw = root["parameter_variants"]
        if not isinstance(variants_raw, list) or len(variants_raw) < 2:
            raise ExperimentError(
                "experiment.parameter_variants must contain at least two variants"
            )
        variant_keys = {
            "name",
            "rebalance_every_n_trading_days",
            "lookback_trading_days",
            "minimum_momentum",
        }
        parameter_variants = []
        for index, item in enumerate(variants_raw):
            path_prefix = f"experiment.parameter_variants[{index}]"
            variant_raw = _expect_mapping(item, path_prefix)
            _expect_keys(
                variant_raw,
                required=("name",),
                allowed=tuple(variant_keys),
                path=path_prefix,
            )
            variant_name = variant_raw["name"]
            if not isinstance(variant_name, str) or not variant_name.strip():
                raise ExperimentError(f"{path_prefix}.name must not be empty")
            rebalance = _optional_positive_int(
                variant_raw.get("rebalance_every_n_trading_days"),
                f"{path_prefix}.rebalance_every_n_trading_days",
            )
            lookback = _optional_positive_int(
                variant_raw.get("lookback_trading_days"),
                f"{path_prefix}.lookback_trading_days",
            )
            minimum_momentum = _optional_number(
                variant_raw.get("minimum_momentum"),
                f"{path_prefix}.minimum_momentum",
            )
            if minimum_momentum is not None and minimum_momentum < -1:
                raise ExperimentError(
                    f"{path_prefix}.minimum_momentum must be >= -1"
                )
            parameter_variants.append(
                ParameterVariant(
                    name=variant_name.strip(),
                    rebalance_every_n_trading_days=rebalance,
                    lookback_trading_days=lookback,
                    minimum_momentum=minimum_momentum,
                )
            )
        if len({variant.name for variant in parameter_variants}) != len(
            parameter_variants
        ):
            raise ExperimentError("parameter variant names must be unique")
        configured = [
            variant
            for variant in parameter_variants
            if variant.name == "configured"
        ]
        if len(configured) != 1 or any(
            value is not None
            for key, value in configured[0].to_dict().items()
            if key != "name"
        ):
            raise ExperimentError(
                "parameter_variants must contain one override-free "
                "'configured' baseline"
            )
        if any(
            variant.name != "configured"
            and all(
                value is None
                for key, value in variant.to_dict().items()
                if key != "name"
            )
            for variant in parameter_variants
        ):
            raise ExperimentError(
                "non-baseline parameter variants must override a parameter"
            )

    return ExperimentSpec(
        schema_version=schema_version,
        name=name.strip(),
        seed=seed,
        windows=chronological,
        execution_scenarios=scenarios,
        parameter_variants=parameter_variants,
    )


def _json_ready(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, dict):
        return {
            str(key): _json_ready(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_ready(item) for item in value)
    return value


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        _json_ready(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def market_identity(market: MarketData) -> Dict[str, Any]:
    return {
        "price_adjustment": market.price_adjustment,
        "dates": [trading_date.isoformat() for trading_date in market.dates],
        "prices": {
            trading_date.isoformat(): dict(
                sorted(market.prices_on(trading_date).items())
            )
            for trading_date in market.dates
        },
        "tradable": (
            {
                trading_date.isoformat(): sorted(
                    market.tradable_by_date.get(trading_date, set())
                )
                for trading_date in market.dates
            }
            if market.tradable_by_date is not None
            else None
        ),
        "corporate_actions": {
            trading_date.isoformat(): [
                _json_ready(action)
                for action in market.corporate_actions_on(trading_date)
            ]
            for trading_date in market.dates
            if market.corporate_actions_on(trading_date)
        },
    }


def _scenario_policy(policy: Policy, scenario: ExecutionScenario) -> Policy:
    execution = replace(
        policy.execution,
        commission_rate=(
            policy.execution.commission_rate
            if scenario.commission_rate is None
            else scenario.commission_rate
        ),
        minimum_commission=(
            policy.execution.minimum_commission
            if scenario.minimum_commission is None
            else scenario.minimum_commission
        ),
        sell_tax_rate=(
            policy.execution.sell_tax_rate
            if scenario.sell_tax_rate is None
            else scenario.sell_tax_rate
        ),
        slippage_bps=(
            policy.execution.slippage_bps
            if scenario.slippage_bps is None
            else scenario.slippage_bps
        ),
    )
    return replace(policy, execution=execution)


def _parameter_policy(
    policy: Policy, variant: ParameterVariant
) -> Policy:
    _optional_positive_int(
        variant.rebalance_every_n_trading_days,
        f"parameter variant {variant.name}.rebalance_every_n_trading_days",
    )
    _optional_positive_int(
        variant.lookback_trading_days,
        f"parameter variant {variant.name}.lookback_trading_days",
    )
    minimum_momentum = _optional_number(
        variant.minimum_momentum,
        f"parameter variant {variant.name}.minimum_momentum",
    )
    if minimum_momentum is not None and minimum_momentum < -1:
        raise ExperimentError(
            f"parameter variant {variant.name}.minimum_momentum must be >= -1"
        )
    if policy.strategy.kind == "fixed_weight" and (
        variant.lookback_trading_days is not None
        or variant.minimum_momentum is not None
    ):
        raise ExperimentError(
            "fixed_weight experiments cannot override momentum parameters"
        )
    strategy = replace(
        policy.strategy,
        rebalance_every_n_trading_days=(
            policy.strategy.rebalance_every_n_trading_days
            if variant.rebalance_every_n_trading_days is None
            else variant.rebalance_every_n_trading_days
        ),
        lookback_trading_days=(
            policy.strategy.lookback_trading_days
            if variant.lookback_trading_days is None
            else variant.lookback_trading_days
        ),
        minimum_momentum=(
            policy.strategy.minimum_momentum
            if variant.minimum_momentum is None
            else variant.minimum_momentum
        ),
    )
    candidate = replace(policy, strategy=strategy)
    try:
        validate_policy(candidate)
    except PolicyError as exc:
        raise ExperimentError(
            f"parameter variant {variant.name!r} is invalid: {exc}"
        ) from exc
    return candidate


def _case_summary(case: ExperimentCaseResult) -> Dict[str, Any]:
    metrics = case.result.metrics
    return {
        "case_id": case.case_id,
        "fold": case.window.fold,
        "window": case.window.name,
        "role": case.window.role,
        "execution_scenario": case.execution_scenario.name,
        "parameter_variant": case.parameter_variant.name,
        "parameter_overrides": case.parameter_variant.to_dict(),
        "total_return": metrics["total_return"],
        "annualized_return": metrics["annualized_return"],
        "annualized_volatility": metrics["annualized_volatility"],
        "sharpe_ratio": metrics["sharpe_ratio"],
        "sortino_ratio": metrics["sortino_ratio"],
        "max_drawdown": metrics["max_drawdown"],
        "max_drawdown_duration_trading_days": metrics[
            "max_drawdown_duration_trading_days"
        ],
        "turnover_ratio": metrics["turnover_ratio"],
        "total_cost": (
            metrics["total_commission"]
            + metrics["total_sell_tax"]
            + metrics["total_slippage_cost"]
        ),
        "decision_count": metrics["decision_count"],
        "filled_trade_count": metrics["filled_trade_count"],
        "market_regime_attribution": case.regime_attribution.to_summary(),
        "bootstrap_uncertainty": case.bootstrap_uncertainty.to_summary(),
    }


def _parameter_stability_summary(
    spec: ExperimentSpec,
    cases: Sequence[ExperimentCaseResult],
) -> Dict[str, Any]:
    test_cases = [case for case in cases if case.window.role == "test"]
    grouped: Dict[tuple[str, str], List[ExperimentCaseResult]] = {}
    for case in test_cases:
        key = (case.window.fold, case.execution_scenario.name)
        grouped.setdefault(key, []).append(case)

    comparisons = []
    for (fold, execution_scenario), group in grouped.items():
        returns = {
            case.parameter_variant.name: case.result.metrics["total_return"]
            for case in group
        }
        values = list(returns.values())
        comparisons.append(
            {
                "fold": fold,
                "execution_scenario": execution_scenario,
                "variant_total_returns": dict(sorted(returns.items())),
                "minimum_total_return": min(values),
                "maximum_total_return": max(values),
                "total_return_range": max(values) - min(values),
                "positive_variant_count": sum(
                    1 for value in values if value > 0
                ),
                "variant_count": len(values),
            }
        )

    variant_summaries = []
    for variant in spec.parameter_variants:
        returns = [
            case.result.metrics["total_return"]
            for case in test_cases
            if case.parameter_variant.name == variant.name
        ]
        variant_summaries.append(
            {
                "name": variant.name,
                "overrides": variant.to_dict(),
                "case_count": len(returns),
                "minimum_total_return": min(returns),
                "maximum_total_return": max(returns),
                "mean_total_return": statistics.mean(returns),
                "positive_case_count": sum(
                    1 for value in returns if value > 0
                ),
            }
        )
    return {
        "sensitivity_evaluated": len(spec.parameter_variants) > 1,
        "descriptive_only": True,
        "automatic_parameter_selection": False,
        "variant_count": len(spec.parameter_variants),
        "comparison_count": len(comparisons),
        "comparisons": comparisons,
        "variants": variant_summaries,
    }


def _test_window_metadata(
    cases: Sequence[ExperimentCaseResult],
) -> Dict[str, Any]:
    unique_windows = sorted(
        {
            (case.window.fold, case.window.start, case.window.end)
            for case in cases
            if case.window.role == "test"
        },
        key=lambda item: (item[1], item[2], item[0]),
    )
    overlapping_windows = any(
        first_window[1] <= second_window[2]
        and second_window[1] <= first_window[2]
        for index, first_window in enumerate(unique_windows)
        for second_window in unique_windows[index + 1 :]
    )
    return {
        "test_window_count": len(unique_windows),
        "test_windows_overlap": overlapping_windows,
    }


def _bootstrap_interval_aggregate(
    intervals: Sequence[Optional[BootstrapInterval]],
) -> Optional[Dict[str, Any]]:
    available = [interval for interval in intervals if interval is not None]
    if not available:
        return None
    return {
        "available_case_count": len(available),
        "minimum_point_estimate": min(
            interval.point_estimate for interval in available
        ),
        "maximum_point_estimate": max(
            interval.point_estimate for interval in available
        ),
        "minimum_lower_bound": min(interval.lower for interval in available),
        "maximum_upper_bound": max(interval.upper for interval in available),
        "minimum_positive_resample_fraction": min(
            interval.positive_resample_fraction for interval in available
        ),
    }


def _test_bootstrap_uncertainty_summary(
    cases: Sequence[ExperimentCaseResult],
) -> Dict[str, Any]:
    if not cases:
        raise ExperimentError("bootstrap summary requires experiment cases")
    uncertainties = [case.bootstrap_uncertainty for case in cases]
    first = uncertainties[0]
    if any(
        uncertainty.protocol != first.protocol
        or uncertainty.benchmark != first.benchmark
        for uncertainty in uncertainties
    ):
        raise ExperimentError(
            "bootstrap protocol or benchmark changed within an experiment"
        )
    if any(
        uncertainty.enabled
        != (uncertainty.strategy_total_return is not None)
        for uncertainty in uncertainties
    ):
        raise ExperimentError("bootstrap enabled state is internally inconsistent")
    if any(
        not uncertainty.enabled
        and (
            uncertainty.benchmark_total_return is not None
            or uncertainty.strategy_relative_to_benchmark is not None
            or uncertainty.resample_schedule_sha256 is not None
            or uncertainty.replicates_sha256 is not None
        )
        for uncertainty in uncertainties
    ):
        raise ExperimentError("disabled bootstrap contains generated results")
    if any(
        case.window.role != "test" and case.bootstrap_uncertainty.enabled
        for case in cases
    ):
        raise ExperimentError("bootstrap must not run outside test windows")
    if first.benchmark is None and any(
        uncertainty.benchmark_total_return is not None
        or uncertainty.strategy_relative_to_benchmark is not None
        for uncertainty in uncertainties
    ):
        raise ExperimentError(
            "benchmark bootstrap intervals require a configured benchmark"
        )
    if first.benchmark is not None and any(
        uncertainty.enabled
        and (
            uncertainty.benchmark_total_return is None
            or uncertainty.strategy_relative_to_benchmark is None
        )
        for uncertainty in uncertainties
    ):
        raise ExperimentError(
            "enabled benchmark bootstrap must include paired intervals"
        )

    test_cases = [case for case in cases if case.window.role == "test"]
    grouped: Dict[tuple[str, str], List[ExperimentCaseResult]] = {}
    for case in test_cases:
        key = (
            case.execution_scenario.name,
            case.parameter_variant.name,
        )
        grouped.setdefault(key, []).append(case)

    comparisons: List[Dict[str, Any]] = []
    for (execution_scenario, parameter_variant), group in grouped.items():
        group_uncertainties = [case.bootstrap_uncertainty for case in group]
        comparisons.append(
            {
                "execution_scenario": execution_scenario,
                "parameter_variant": parameter_variant,
                "case_count": len(group),
                "fold_count": len({case.window.fold for case in group}),
                "enabled_case_count": sum(
                    1 for uncertainty in group_uncertainties if uncertainty.enabled
                ),
                "disabled_case_count": sum(
                    1
                    for uncertainty in group_uncertainties
                    if not uncertainty.enabled
                ),
                "disabled_reasons": sorted(
                    {
                        uncertainty.disabled_reason
                        for uncertainty in group_uncertainties
                        if uncertainty.disabled_reason is not None
                    }
                ),
                "strategy_total_return": _bootstrap_interval_aggregate(
                    [
                        uncertainty.strategy_total_return
                        for uncertainty in group_uncertainties
                    ]
                ),
                "benchmark_total_return": _bootstrap_interval_aggregate(
                    [
                        uncertainty.benchmark_total_return
                        for uncertainty in group_uncertainties
                    ]
                ),
                "strategy_relative_to_benchmark": (
                    _bootstrap_interval_aggregate(
                        [
                            uncertainty.strategy_relative_to_benchmark
                            for uncertainty in group_uncertainties
                        ]
                    )
                ),
            }
        )

    enabled_test_count = sum(
        1 for case in test_cases if case.bootstrap_uncertainty.enabled
    )
    return {
        "enabled": enabled_test_count > 0,
        "benchmark": first.benchmark,
        "protocol": first.protocol.to_dict(),
        "test_only": True,
        "resampling_unit": "daily log-return blocks",
        "benchmark_pairing": (
            "shared source indices"
            if first.benchmark is not None
            else "not applicable"
        ),
        "confidence_interval_method": "percentile",
        "descriptive_only": True,
        "investment_validity_established": False,
        "p_value_reported": False,
        "used_for_strategy_decisions": False,
        "used_for_parameter_selection": False,
        "pooled_performance_estimate": False,
        "aggregation_unit": "per-case interval bounds",
        "test_case_count": len(test_cases),
        "enabled_case_count": enabled_test_count,
        "disabled_case_count": len(test_cases) - enabled_test_count,
        "disabled_reasons": sorted(
            {
                case.bootstrap_uncertainty.disabled_reason
                for case in test_cases
                if case.bootstrap_uncertainty.disabled_reason is not None
            }
        ),
        **_test_window_metadata(cases),
        "comparison_count": len(comparisons),
        "comparisons": comparisons,
    }


def _test_market_regime_summary(
    cases: Sequence[ExperimentCaseResult],
) -> Dict[str, Any]:
    if not cases:
        raise ExperimentError("market-regime summary requires experiment cases")
    attributions = [case.regime_attribution for case in cases]
    enabled_values = {attribution.enabled for attribution in attributions}
    if len(enabled_values) != 1:
        raise ExperimentError(
            "market-regime attribution must be consistently enabled"
        )
    first = attributions[0]
    base = {
        "enabled": first.enabled,
        "benchmark": first.benchmark,
        "protocol": first.protocol.to_dict(),
        "descriptive_only": True,
        "used_for_strategy_decisions": False,
        "used_for_parameter_selection": False,
        "pooled_performance_estimate": False,
        "aggregation_unit": "per-case log-return contribution",
    }
    test_cases = [case for case in cases if case.window.role == "test"]
    if not first.enabled:
        return {
            **base,
            "disabled_reason": first.disabled_reason,
            "test_case_count": len(test_cases),
            **_test_window_metadata(cases),
            "comparison_count": 0,
            "comparisons": [],
        }
    if any(
        attribution.benchmark != first.benchmark
        or attribution.protocol != first.protocol
        for attribution in attributions
    ):
        raise ExperimentError(
            "market-regime attribution protocol changed within an experiment"
        )

    grouped: Dict[
        tuple[str, str, str], List[tuple[ExperimentCaseResult, Dict[str, Any]]]
    ] = {}
    for case in test_cases:
        by_regime = {
            row["regime"]: row
            for row in case.regime_attribution.regime_summaries()
        }
        for regime in REGIME_NAMES:
            key = (
                case.execution_scenario.name,
                case.parameter_variant.name,
                regime,
            )
            grouped.setdefault(key, []).append((case, by_regime[regime]))

    comparisons: List[Dict[str, Any]] = []
    execution_order = {
        name: index
        for index, name in enumerate(
            dict.fromkeys(case.execution_scenario.name for case in test_cases)
        )
    }
    parameter_order = {
        name: index
        for index, name in enumerate(
            dict.fromkeys(case.parameter_variant.name for case in test_cases)
        )
    }
    regime_order = {name: index for index, name in enumerate(REGIME_NAMES)}
    ordered_keys = sorted(
        grouped,
        key=lambda item: (
            execution_order[item[0]],
            parameter_order[item[1]],
            regime_order[item[2]],
        ),
    )
    for key in ordered_keys:
        execution_scenario, parameter_variant, regime = key
        group = grouped[key]
        strategy_values = [
            row["strategy_log_return_contribution"] for _, row in group
        ]
        benchmark_values = [
            row["benchmark_log_return_contribution"] for _, row in group
        ]
        excess_values = [
            row["excess_log_return_contribution"] for _, row in group
        ]
        comparisons.append(
            {
                "execution_scenario": execution_scenario,
                "parameter_variant": parameter_variant,
                "regime": regime,
                "case_count": len(group),
                "fold_count": len({case.window.fold for case, _ in group}),
                "observed_case_count": sum(
                    1 for _, row in group if row["day_count"] > 0
                ),
                "total_day_count": sum(row["day_count"] for _, row in group),
                "minimum_strategy_log_return_contribution": min(
                    strategy_values
                ),
                "maximum_strategy_log_return_contribution": max(
                    strategy_values
                ),
                "mean_strategy_log_return_contribution": statistics.mean(
                    strategy_values
                ),
                "mean_benchmark_log_return_contribution": statistics.mean(
                    benchmark_values
                ),
                "mean_excess_log_return_contribution": statistics.mean(
                    excess_values
                ),
                "positive_strategy_contribution_case_count": sum(
                    1 for value in strategy_values if value > 0
                ),
            }
        )

    return {
        **base,
        "test_case_count": len(test_cases),
        **_test_window_metadata(cases),
        "comparison_count": len(comparisons),
        "comparisons": comparisons,
    }


def run_experiment(
    spec: ExperimentSpec, policy: Policy, market: MarketData
) -> ExperimentResult:
    input_payload = {
        "spec": spec.to_dict(),
        "policy": policy.to_dict(),
        "market_sha256": _sha256_json(market_identity(market)),
        "regime_protocol": DEFAULT_REGIME_PROTOCOL.to_dict(),
        "bootstrap_protocol": DEFAULT_BOOTSTRAP_PROTOCOL.to_dict(),
    }
    experiment_input_id = _sha256_json(input_payload)
    cases: List[ExperimentCaseResult] = []
    for window in spec.windows:
        for parameter_variant in spec.parameter_variants:
            parameter_policy = _parameter_policy(policy, parameter_variant)
            for execution_scenario in spec.execution_scenarios:
                case_policy = _scenario_policy(
                    parameter_policy, execution_scenario
                )
                result = run_backtest(
                    case_policy,
                    market,
                    execution_delay_trading_days=(
                        execution_scenario.execution_delay_trading_days
                    ),
                    start_date=window.start,
                    end_date=window.end,
                )
                case_id = _sha256_json(
                    {
                        "experiment_input_id": experiment_input_id,
                        "window": window.to_dict(),
                        "execution_scenario": execution_scenario.to_dict(),
                        "parameter_variant": parameter_variant.to_dict(),
                    }
                )
                try:
                    regime_attribution = attribute_market_regimes(
                        market,
                        policy.portfolio.benchmark,
                        result.nav,
                        protocol=DEFAULT_REGIME_PROTOCOL,
                    )
                except RegimeAttributionError as exc:
                    raise ExperimentError(
                        f"market-regime attribution failed: {exc}"
                    ) from exc
                bootstrap_seed_material = _sha256_json(
                    {"experiment_seed": spec.seed, "case_id": case_id}
                )
                try:
                    if window.role == "test":
                        bootstrap_uncertainty = bootstrap_return_uncertainty(
                            market,
                            case_policy.portfolio.benchmark,
                            result.nav,
                            seed_material=bootstrap_seed_material,
                            protocol=DEFAULT_BOOTSTRAP_PROTOCOL,
                        )
                    else:
                        bootstrap_uncertainty = (
                            disabled_bootstrap_uncertainty(
                                observation_count=max(0, len(result.nav) - 1),
                                seed_material=bootstrap_seed_material,
                                reason=(
                                    "bootstrap uncertainty is limited to "
                                    "test windows"
                                ),
                                benchmark=case_policy.portfolio.benchmark,
                                protocol=DEFAULT_BOOTSTRAP_PROTOCOL,
                            )
                        )
                except BootstrapUncertaintyError as exc:
                    raise ExperimentError(
                        f"bootstrap uncertainty failed: {exc}"
                    ) from exc
                cases.append(
                    ExperimentCaseResult(
                        case_id=case_id,
                        window=window,
                        execution_scenario=execution_scenario,
                        parameter_variant=parameter_variant,
                        result=result,
                        regime_attribution=regime_attribution,
                        bootstrap_uncertainty=bootstrap_uncertainty,
                    )
                )

    case_summaries = [_case_summary(case) for case in cases]
    test_returns = [
        case["total_return"]
        for case in case_summaries
        if case["role"] == "test"
        and case["parameter_variant"] == "configured"
    ]
    summary = {
        "experiment_name": spec.name,
        "case_count": len(cases),
        "research_only": True,
        "investment_validity_established": False,
        "cases": case_summaries,
        "walk_forward": {
            "fold_count": len(
                {window.fold for window in spec.windows}
            ),
            "folds": list(
                dict.fromkeys(window.fold for window in spec.windows)
            ),
            "model_refit_per_fold": False,
            "description": (
                "rolling time-split evaluation; no model fitting or "
                "automatic parameter selection is performed"
            ),
        },
        "test_execution_robustness": {
            "minimum_total_return": min(test_returns),
            "maximum_total_return": max(test_returns),
            "positive_case_count": sum(
                1 for total_return in test_returns if total_return > 0
            ),
            "case_count": len(test_returns),
        },
        "test_parameter_stability": _parameter_stability_summary(
            spec, cases
        ),
        "test_market_regime_attribution": _test_market_regime_summary(cases),
        "test_bootstrap_uncertainty": (
            _test_bootstrap_uncertainty_summary(cases)
        ),
    }
    result_sha256 = _sha256_json(
        {
            "experiment_input_id": experiment_input_id,
            "cases": [
                {
                    "case_id": case.case_id,
                    "window": case.window.to_dict(),
                    "execution_scenario": case.execution_scenario.to_dict(),
                    "parameter_variant": case.parameter_variant.to_dict(),
                    "result": case.result,
                    "regime_attribution": case.regime_attribution,
                    "bootstrap_uncertainty": case.bootstrap_uncertainty,
                }
                for case in cases
            ],
            "summary": summary,
        }
    )
    return ExperimentResult(
        experiment_input_id=experiment_input_id,
        result_sha256=result_sha256,
        spec=spec,
        cases=cases,
        summary=summary,
    )
