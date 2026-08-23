from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping, Set


class ResearchPolicyError(ValueError):
    """Raised when the frozen research scope is invalid."""


@dataclass(frozen=True)
class ResearchInstrument:
    symbol: str
    exchange: str
    asset_type: str
    roles: List[str]


@dataclass(frozen=True)
class ResearchHorizon:
    start_date: date
    minimum_history_trading_days: int
    decision_frequency: str


@dataclass(frozen=True)
class ResearchPolicy:
    schema_version: int
    name: str
    purpose: str
    market: str
    base_currency: str
    bar_frequency: str
    adjustment: str
    point_in_time_mode: str
    horizon: ResearchHorizon
    benchmark: str
    max_drawdown: float
    instruments: List[ResearchInstrument]

    @property
    def symbols(self) -> Set[str]:
        return {instrument.symbol for instrument in self.instruments}

    @property
    def tradable_symbols(self) -> Set[str]:
        return {
            instrument.symbol
            for instrument in self.instruments
            if "tradable" in instrument.roles
        }

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["horizon"]["start_date"] = self.horizon.start_date.isoformat()
        return payload


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ResearchPolicyError(f"{path} must be a JSON object")
    return value


def _exact_keys(data: Mapping[str, Any], expected: Set[str], path: str) -> None:
    missing = sorted(expected - set(data))
    unknown = sorted(set(data) - expected)
    if missing:
        raise ResearchPolicyError(
            f"{path} is missing required keys: {', '.join(missing)}"
        )
    if unknown:
        raise ResearchPolicyError(
            f"{path} has unknown keys: {', '.join(unknown)}"
        )


def _non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchPolicyError(f"{path} must be a non-empty string")
    return value.strip()


def _load_date(value: Any, path: str) -> date:
    raw = _non_empty_string(value, path)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ResearchPolicyError(f"{path} must be YYYY-MM-DD") from exc


def load_research_policy(path: Path) -> ResearchPolicy:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ResearchPolicyError(
            f"research policy file not found: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ResearchPolicyError(
            f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}"
        ) from exc

    root = _mapping(raw, "research_policy")
    root_keys = {
        "schema_version",
        "name",
        "purpose",
        "market",
        "base_currency",
        "bar_frequency",
        "adjustment",
        "point_in_time_mode",
        "horizon",
        "benchmark",
        "max_drawdown",
        "instruments",
    }
    _exact_keys(root, root_keys, "research_policy")

    if root["schema_version"] != 1:
        raise ResearchPolicyError(
            "only research_policy schema_version 1 is supported"
        )
    purpose = _non_empty_string(root["purpose"], "research_policy.purpose")
    if purpose != "research_only":
        raise ResearchPolicyError(
            "research_policy.purpose must be research_only"
        )

    currency = _non_empty_string(
        root["base_currency"], "research_policy.base_currency"
    ).upper()
    if len(currency) != 3:
        raise ResearchPolicyError(
            "research_policy.base_currency must be a 3-letter currency code"
        )

    bar_frequency = _non_empty_string(
        root["bar_frequency"], "research_policy.bar_frequency"
    )
    if bar_frequency != "daily":
        raise ResearchPolicyError(
            "only research_policy.bar_frequency=daily is supported"
        )

    adjustment = _non_empty_string(
        root["adjustment"], "research_policy.adjustment"
    )
    if adjustment not in {"none", "qfq", "hfq"}:
        raise ResearchPolicyError(
            "research_policy.adjustment must be none, qfq, or hfq"
        )
    point_in_time_mode = _non_empty_string(
        root["point_in_time_mode"],
        "research_policy.point_in_time_mode",
    )
    if point_in_time_mode not in {
        "provider_publication",
        "local_observation",
    }:
        raise ResearchPolicyError(
            "research_policy.point_in_time_mode must be "
            "provider_publication or local_observation"
        )

    horizon_raw = _mapping(root["horizon"], "research_policy.horizon")
    horizon_keys = {
        "start_date",
        "minimum_history_trading_days",
        "decision_frequency",
    }
    _exact_keys(horizon_raw, horizon_keys, "research_policy.horizon")
    minimum_days = horizon_raw["minimum_history_trading_days"]
    if (
        isinstance(minimum_days, bool)
        or not isinstance(minimum_days, int)
        or minimum_days <= 0
    ):
        raise ResearchPolicyError(
            "research_policy.horizon.minimum_history_trading_days "
            "must be a positive integer"
        )
    decision_frequency = _non_empty_string(
        horizon_raw["decision_frequency"],
        "research_policy.horizon.decision_frequency",
    )
    if decision_frequency not in {"daily", "weekly", "monthly", "quarterly"}:
        raise ResearchPolicyError(
            "research_policy.horizon.decision_frequency is unsupported"
        )

    max_drawdown = root["max_drawdown"]
    if (
        isinstance(max_drawdown, bool)
        or not isinstance(max_drawdown, (int, float))
        or not math.isfinite(float(max_drawdown))
        or not 0 < float(max_drawdown) < 1
    ):
        raise ResearchPolicyError(
            "research_policy.max_drawdown must be > 0 and < 1"
        )

    instruments_raw = root["instruments"]
    if not isinstance(instruments_raw, list) or not instruments_raw:
        raise ResearchPolicyError(
            "research_policy.instruments must be a non-empty array"
        )

    instruments: List[ResearchInstrument] = []
    seen_symbols: Set[str] = set()
    allowed_roles = {"tradable", "benchmark"}
    for index, item in enumerate(instruments_raw):
        item_path = f"research_policy.instruments[{index}]"
        instrument_raw = _mapping(item, item_path)
        _exact_keys(
            instrument_raw,
            {"symbol", "exchange", "asset_type", "roles"},
            item_path,
        )
        symbol = _non_empty_string(
            instrument_raw["symbol"], f"{item_path}.symbol"
        ).upper()
        if symbol in seen_symbols:
            raise ResearchPolicyError(
                f"research instrument symbols must be unique: {symbol}"
            )
        seen_symbols.add(symbol)

        roles_raw = instrument_raw["roles"]
        if not isinstance(roles_raw, list) or not roles_raw:
            raise ResearchPolicyError(
                f"{item_path}.roles must be a non-empty array"
            )
        roles = [
            _non_empty_string(role, f"{item_path}.roles").lower()
            for role in roles_raw
        ]
        if len(set(roles)) != len(roles):
            raise ResearchPolicyError(f"{item_path}.roles must be unique")
        unknown_roles = sorted(set(roles) - allowed_roles)
        if unknown_roles:
            raise ResearchPolicyError(
                f"{item_path}.roles has unsupported values: "
                f"{', '.join(unknown_roles)}"
            )

        instruments.append(
            ResearchInstrument(
                symbol=symbol,
                exchange=_non_empty_string(
                    instrument_raw["exchange"], f"{item_path}.exchange"
                ).upper(),
                asset_type=_non_empty_string(
                    instrument_raw["asset_type"], f"{item_path}.asset_type"
                ).upper(),
                roles=roles,
            )
        )

    benchmark = _non_empty_string(
        root["benchmark"], "research_policy.benchmark"
    ).upper()
    matching_benchmark = [
        instrument
        for instrument in instruments
        if instrument.symbol == benchmark
    ]
    if not matching_benchmark or "benchmark" not in matching_benchmark[0].roles:
        raise ResearchPolicyError(
            "research_policy.benchmark must reference an instrument with "
            "the benchmark role"
        )
    if not any("tradable" in instrument.roles for instrument in instruments):
        raise ResearchPolicyError(
            "research_policy must contain at least one tradable instrument"
        )

    return ResearchPolicy(
        schema_version=1,
        name=_non_empty_string(root["name"], "research_policy.name"),
        purpose=purpose,
        market=_non_empty_string(
            root["market"], "research_policy.market"
        ).upper(),
        base_currency=currency,
        bar_frequency=bar_frequency,
        adjustment=adjustment,
        point_in_time_mode=point_in_time_mode,
        horizon=ResearchHorizon(
            start_date=_load_date(
                horizon_raw["start_date"],
                "research_policy.horizon.start_date",
            ),
            minimum_history_trading_days=minimum_days,
            decision_frequency=decision_frequency,
        ),
        benchmark=benchmark,
        max_drawdown=float(max_drawdown),
        instruments=instruments,
    )
