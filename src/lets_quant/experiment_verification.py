from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Sequence

from .snapshots import file_sha256


class ExperimentArtifactError(ValueError):
    """Raised when a research experiment artifact is malformed or inconsistent."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_ROOT_REQUIRED_FILES = {
    "experiment.snapshot.json",
    "manifest.json",
    "policy.snapshot.json",
    "summary.json",
}
_ROOT_OPTIONAL_FILES = {
    "dataset.snapshot.json",
    "market.snapshot.json",
}
_CASE_REQUIRED_FILES = {
    "accounting.csv",
    "bootstrap_uncertainty.json",
    "case.snapshot.json",
    "ledger.csv",
    "metrics.json",
    "nav.csv",
    "regime_attribution.csv",
    "signals.csv",
    "trades.csv",
}
_CASE_SNAPSHOT_KEYS = {
    "bootstrap_uncertainty",
    "case_id",
    "execution_scenario",
    "experiment_result_sha256",
    "market_regime_attribution",
    "parameter_variant",
    "window",
}
_CASE_SUMMARY_KEYS = {
    "annualized_return",
    "annualized_volatility",
    "bootstrap_uncertainty",
    "case_id",
    "decision_count",
    "execution_scenario",
    "filled_trade_count",
    "fold",
    "market_regime_attribution",
    "max_drawdown",
    "max_drawdown_duration_trading_days",
    "parameter_overrides",
    "parameter_variant",
    "role",
    "sharpe_ratio",
    "sortino_ratio",
    "total_cost",
    "total_return",
    "turnover_ratio",
    "window",
}
_SUMMARY_METRIC_FIELDS = {
    "annualized_return",
    "annualized_volatility",
    "decision_count",
    "filled_trade_count",
    "max_drawdown",
    "max_drawdown_duration_trading_days",
    "sharpe_ratio",
    "sortino_ratio",
    "total_return",
    "turnover_ratio",
}
_CSV_HEADERS = {
    "accounting.csv": [
        "date",
        "status",
        "ledger_entry_count",
        "cash",
        "expected_cash",
        "market_value",
        "expected_market_value",
        "nav",
        "expected_nav",
        "cash_error",
        "nav_error",
        "positions",
        "expected_positions",
        "position_errors",
    ],
    "ledger.csv": [
        "entry_id",
        "sequence",
        "date",
        "event_type",
        "symbol",
        "quantity_delta",
        "cash_delta",
        "expense",
        "reference_id",
        "description",
        "metadata",
    ],
    "nav.csv": [
        "date",
        "nav",
        "cash",
        "drawdown",
        "risk_frozen",
        "positions",
    ],
    "regime_attribution.csv": [
        "date",
        "information_cutoff_date",
        "regime",
        "trailing_benchmark_return",
        "trailing_benchmark_drawdown",
        "strategy_return",
        "benchmark_return",
        "strategy_log_return",
        "benchmark_log_return",
        "excess_log_return",
    ],
    "signals.csv": [
        "signal_date",
        "execution_date",
        "status",
        "estimated_turnover",
        "reason",
        "decision_id",
        "strategy_kind",
        "target_weights",
        "decision_evidence",
        "diagnostics",
        "orders",
    ],
    "trades.csv": [
        "signal_date",
        "execution_date",
        "symbol",
        "side",
        "requested_quantity",
        "filled_quantity",
        "signal_price",
        "market_price",
        "fill_price",
        "gross_notional",
        "commission",
        "tax",
        "slippage_cost",
        "status",
    ],
}


def _load_json_object(path: Path) -> Dict[str, Any]:
    def reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ExperimentArtifactError(
                    f"{path} contains duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ExperimentArtifactError(
            f"{path} contains non-finite JSON value {value}"
        )

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except FileNotFoundError as exc:
        raise ExperimentArtifactError(f"file not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ExperimentArtifactError(f"{path} is not UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise ExperimentArtifactError(
            f"{path} is invalid JSON at line {exc.lineno} column {exc.colno}"
        ) from exc
    if not isinstance(payload, dict):
        raise ExperimentArtifactError(f"{path} must contain a JSON object")
    return payload


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ExperimentArtifactError(f"{location} must be a JSON object")
    return value


def _list(value: Any, location: str) -> List[Any]:
    if not isinstance(value, list):
        raise ExperimentArtifactError(f"{location} must be a JSON array")
    return value


def _integer(value: Any, location: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ExperimentArtifactError(
            f"{location} must be an integer >= {minimum}"
        )
    return value


def _finite(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExperimentArtifactError(f"{location} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ExperimentArtifactError(f"{location} must be a finite number")
    return parsed


def _nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentArtifactError(f"{location} must be a non-empty string")
    return value


def _sha256(value: Any, location: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ExperimentArtifactError(
            f"{location} must be a lowercase SHA-256 digest"
        )
    return value


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _exact_keys(
    value: Mapping[str, Any], expected: set[str], location: str
) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise ExperimentArtifactError(
            f"{location} is missing keys: {', '.join(missing)}"
        )
    if unknown:
        raise ExperimentArtifactError(
            f"{location} has unknown keys: {', '.join(unknown)}"
        )


def _safe_relative_file(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ExperimentArtifactError(
            f"{location} must be a canonical relative POSIX path"
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ExperimentArtifactError(
            f"{location} must be a canonical relative POSIX path"
        )
    return value


def _read_csv(path: Path, expected_header: Sequence[str]) -> List[Dict[str, str]]:
    try:
        handle = path.open("r", newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise ExperimentArtifactError(f"file not found: {path}") from exc
    try:
        with handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != list(expected_header):
                raise ExperimentArtifactError(
                    f"{path} has an unexpected CSV header"
                )
            rows = list(reader)
    except UnicodeDecodeError as exc:
        raise ExperimentArtifactError(f"{path} is not UTF-8 CSV") from exc
    if any(
        None in row or any(value is None for value in row.values())
        for row in rows
    ):
        raise ExperimentArtifactError(
            f"{path} contains a row with the wrong number of fields"
        )
    return rows


def _verify_interval(value: Any, location: str) -> None:
    interval = _mapping(value, location)
    _exact_keys(
        interval,
        {
            "lower",
            "median",
            "point_estimate",
            "positive_resample_fraction",
            "upper",
        },
        location,
    )
    lower = _finite(interval["lower"], f"{location}.lower")
    median = _finite(interval["median"], f"{location}.median")
    upper = _finite(interval["upper"], f"{location}.upper")
    _finite(interval["point_estimate"], f"{location}.point_estimate")
    positive_fraction = _finite(
        interval["positive_resample_fraction"],
        f"{location}.positive_resample_fraction",
    )
    if not lower <= median <= upper:
        raise ExperimentArtifactError(
            f"{location} interval bounds are not ordered"
        )
    if not 0 <= positive_fraction <= 1:
        raise ExperimentArtifactError(
            f"{location}.positive_resample_fraction must be in [0, 1]"
        )


def _verify_bootstrap(value: Any, location: str) -> Mapping[str, Any]:
    summary = _mapping(value, location)
    required = {
        "annualized",
        "benchmark",
        "benchmark_reconciliation_error",
        "benchmark_total_return",
        "descriptive_only",
        "disabled_reason",
        "enabled",
        "investment_validity_established",
        "observation_count",
        "p_value_reported",
        "protocol",
        "replicates_sha256",
        "resample_schedule_sha256",
        "seed_sha256",
        "strategy_reconciliation_error",
        "strategy_relative_to_benchmark",
        "strategy_total_return",
    }
    _exact_keys(summary, required, location)
    if not isinstance(summary["enabled"], bool):
        raise ExperimentArtifactError(f"{location}.enabled must be boolean")
    if summary["descriptive_only"] is not True:
        raise ExperimentArtifactError(f"{location} must remain descriptive-only")
    if summary["investment_validity_established"] is not False:
        raise ExperimentArtifactError(
            f"{location} cannot establish investment validity"
        )
    if summary["p_value_reported"] is not False:
        raise ExperimentArtifactError(f"{location} cannot report a p-value")
    if summary["annualized"] is not False:
        raise ExperimentArtifactError(f"{location} must not be annualized")
    _integer(summary["observation_count"], f"{location}.observation_count")
    _sha256(summary["seed_sha256"], f"{location}.seed_sha256")
    protocol = _mapping(summary["protocol"], f"{location}.protocol")
    _exact_keys(
        protocol,
        {
            "block_length",
            "confidence_level",
            "method",
            "minimum_observations",
            "resample_count",
            "version",
        },
        f"{location}.protocol",
    )
    _integer(protocol["version"], f"{location}.protocol.version", minimum=1)
    _nonempty_string(protocol["method"], f"{location}.protocol.method")
    _integer(
        protocol["block_length"],
        f"{location}.protocol.block_length",
        minimum=1,
    )
    _integer(
        protocol["resample_count"],
        f"{location}.protocol.resample_count",
        minimum=1,
    )
    _integer(
        protocol["minimum_observations"],
        f"{location}.protocol.minimum_observations",
        minimum=1,
    )
    confidence = _finite(
        protocol["confidence_level"],
        f"{location}.protocol.confidence_level",
    )
    if not 0 < confidence < 1:
        raise ExperimentArtifactError(
            f"{location}.protocol.confidence_level must be in (0, 1)"
        )
    benchmark = summary["benchmark"]
    if benchmark is not None:
        _nonempty_string(benchmark, f"{location}.benchmark")

    if summary["enabled"]:
        if summary["disabled_reason"] is not None:
            raise ExperimentArtifactError(
                f"{location}.disabled_reason must be null when enabled"
            )
        _sha256(
            summary["resample_schedule_sha256"],
            f"{location}.resample_schedule_sha256",
        )
        _sha256(
            summary["replicates_sha256"],
            f"{location}.replicates_sha256",
        )
        _finite(
            summary["strategy_reconciliation_error"],
            f"{location}.strategy_reconciliation_error",
        )
        if abs(float(summary["strategy_reconciliation_error"])) > 1e-12:
            raise ExperimentArtifactError(
                f"{location}.strategy_reconciliation_error exceeds tolerance"
            )
        _verify_interval(
            summary["strategy_total_return"],
            f"{location}.strategy_total_return",
        )
        if benchmark is None:
            if any(
                summary[key] is not None
                for key in (
                    "benchmark_reconciliation_error",
                    "benchmark_total_return",
                    "strategy_relative_to_benchmark",
                )
            ):
                raise ExperimentArtifactError(
                    f"{location} has benchmark results without a benchmark"
                )
        else:
            _finite(
                summary["benchmark_reconciliation_error"],
                f"{location}.benchmark_reconciliation_error",
            )
            if abs(float(summary["benchmark_reconciliation_error"])) > 1e-12:
                raise ExperimentArtifactError(
                    f"{location}.benchmark_reconciliation_error exceeds tolerance"
                )
            _verify_interval(
                summary["benchmark_total_return"],
                f"{location}.benchmark_total_return",
            )
            _verify_interval(
                summary["strategy_relative_to_benchmark"],
                f"{location}.strategy_relative_to_benchmark",
            )
    else:
        _nonempty_string(
            summary["disabled_reason"], f"{location}.disabled_reason"
        )
        if any(
            summary[key] is not None
            for key in (
                "benchmark_reconciliation_error",
                "benchmark_total_return",
                "replicates_sha256",
                "resample_schedule_sha256",
                "strategy_reconciliation_error",
                "strategy_relative_to_benchmark",
                "strategy_total_return",
            )
        ):
            raise ExperimentArtifactError(
                f"{location} contains generated results while disabled"
            )
    return summary


def _verify_manifest_files(
    root: Path, manifest: Mapping[str, Any]
) -> tuple[List[str], Dict[str, str], Dict[str, set[str]]]:
    declared_values = _list(manifest.get("files"), "manifest.files")
    declared_files = [
        _safe_relative_file(value, f"manifest.files[{index}]")
        for index, value in enumerate(declared_values)
    ]
    if declared_files != sorted(declared_files):
        raise ExperimentArtifactError("manifest.files must be sorted")
    if len(declared_files) != len(set(declared_files)):
        raise ExperimentArtifactError("manifest.files contains duplicates")
    if "manifest.json" not in declared_files:
        raise ExperimentArtifactError("manifest.files must include manifest.json")

    declared_hashes_value = _mapping(
        manifest.get("file_sha256"), "manifest.file_sha256"
    )
    declared_hashes = dict(declared_hashes_value)
    expected_hash_names = set(declared_files) - {"manifest.json"}
    if set(declared_hashes) != expected_hash_names:
        raise ExperimentArtifactError(
            "manifest.file_sha256 keys do not match manifest.files"
        )
    for name, digest in declared_hashes.items():
        _sha256(digest, f"manifest.file_sha256[{name!r}]")

    actual_files = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ExperimentArtifactError(
                f"experiment artifact contains symbolic link: {path}"
            )
        if path.is_file():
            actual_files.add(path.relative_to(root).as_posix())
    missing = sorted(set(declared_files) - actual_files)
    extra = sorted(actual_files - set(declared_files))
    if missing:
        raise ExperimentArtifactError(
            "experiment artifact is missing declared files: " + ", ".join(missing)
        )
    if extra:
        raise ExperimentArtifactError(
            "experiment artifact contains undeclared files: " + ", ".join(extra)
        )

    root_files = {name for name in declared_files if "/" not in name}
    unexpected_root = sorted(
        root_files - _ROOT_REQUIRED_FILES - _ROOT_OPTIONAL_FILES
    )
    missing_root = sorted(_ROOT_REQUIRED_FILES - root_files)
    if missing_root:
        raise ExperimentArtifactError(
            "experiment artifact is missing root files: " + ", ".join(missing_root)
        )
    if unexpected_root:
        raise ExperimentArtifactError(
            "experiment artifact has unsupported root files: "
            + ", ".join(unexpected_root)
        )

    case_files: Dict[str, set[str]] = {}
    for name in declared_files:
        if "/" not in name:
            continue
        parts = PurePosixPath(name).parts
        if len(parts) != 3 or parts[0] != "cases":
            raise ExperimentArtifactError(
                f"unsupported experiment artifact path: {name}"
            )
        case_files.setdefault(parts[1], set()).add(parts[2])
    if not case_files:
        raise ExperimentArtifactError("experiment artifact has no case directories")
    for case_directory, names in case_files.items():
        if names != _CASE_REQUIRED_FILES:
            missing_case = sorted(_CASE_REQUIRED_FILES - names)
            extra_case = sorted(names - _CASE_REQUIRED_FILES)
            details = []
            if missing_case:
                details.append("missing " + ", ".join(missing_case))
            if extra_case:
                details.append("unsupported " + ", ".join(extra_case))
            raise ExperimentArtifactError(
                f"cases/{case_directory} file contract failed: "
                + "; ".join(details)
            )

    for name in sorted(expected_hash_names):
        actual_hash = file_sha256(root / PurePosixPath(name))
        if actual_hash != declared_hashes[name]:
            raise ExperimentArtifactError(
                f"experiment artifact integrity failed for {name}: expected "
                f"{declared_hashes[name]}, got {actual_hash}"
            )
    return declared_files, declared_hashes, case_files


def _verify_case(
    root: Path,
    case_directory: str,
    summary_case: Mapping[str, Any],
    experiment_input_id: str,
    result_sha256: str,
) -> Dict[str, Any]:
    location = f"cases/{case_directory}"
    case_root = root / "cases" / case_directory
    snapshot = _load_json_object(case_root / "case.snapshot.json")
    _exact_keys(snapshot, _CASE_SNAPSHOT_KEYS, f"{location}/case.snapshot.json")
    _exact_keys(summary_case, _CASE_SUMMARY_KEYS, f"summary case {case_directory}")

    case_id = _sha256(snapshot["case_id"], f"{location}.case_id")
    expected_case_id = _canonical_sha256(
        {
            "experiment_input_id": experiment_input_id,
            "window": snapshot["window"],
            "execution_scenario": snapshot["execution_scenario"],
            "parameter_variant": snapshot["parameter_variant"],
        }
    )
    if case_id != expected_case_id:
        raise ExperimentArtifactError(
            f"{location} case_id does not match experiment inputs"
        )
    if summary_case["case_id"] != case_id:
        raise ExperimentArtifactError(
            f"{location} case_id does not match summary.json"
        )
    if not case_directory.endswith(f"-{case_id[:8]}"):
        raise ExperimentArtifactError(
            f"{location} directory suffix does not match case_id"
        )
    if snapshot["experiment_result_sha256"] != result_sha256:
        raise ExperimentArtifactError(
            f"{location} is bound to another experiment result"
        )

    window = _mapping(snapshot["window"], f"{location}.window")
    for key in ("end", "fold", "name", "role", "start"):
        if key not in window:
            raise ExperimentArtifactError(f"{location}.window is missing {key}")
    role = _nonempty_string(window["role"], f"{location}.window.role")
    if role not in {"train", "validation", "test"}:
        raise ExperimentArtifactError(f"{location}.window.role is unsupported")
    try:
        start = date.fromisoformat(
            _nonempty_string(window["start"], f"{location}.window.start")
        )
        end = date.fromisoformat(
            _nonempty_string(window["end"], f"{location}.window.end")
        )
    except ValueError as exc:
        raise ExperimentArtifactError(
            f"{location}.window dates must be YYYY-MM-DD"
        ) from exc
    if start > end:
        raise ExperimentArtifactError(f"{location}.window start exceeds end")

    execution = _mapping(
        snapshot["execution_scenario"], f"{location}.execution_scenario"
    )
    parameter = _mapping(
        snapshot["parameter_variant"], f"{location}.parameter_variant"
    )
    execution_name = _nonempty_string(
        execution.get("name"), f"{location}.execution_scenario.name"
    )
    parameter_name = _nonempty_string(
        parameter.get("name"), f"{location}.parameter_variant.name"
    )
    expected_summary_values = {
        "execution_scenario": execution_name,
        "fold": window["fold"],
        "parameter_overrides": dict(parameter),
        "parameter_variant": parameter_name,
        "role": role,
        "window": window["name"],
    }
    for key, expected in expected_summary_values.items():
        if summary_case[key] != expected:
            raise ExperimentArtifactError(
                f"{location} {key} does not match summary.json"
            )

    bootstrap = _verify_bootstrap(
        snapshot["bootstrap_uncertainty"],
        f"{location}.bootstrap_uncertainty",
    )
    bootstrap_file = _load_json_object(
        case_root / "bootstrap_uncertainty.json"
    )
    if (
        bootstrap_file != bootstrap
        or summary_case["bootstrap_uncertainty"] != bootstrap
    ):
        raise ExperimentArtifactError(
            f"{location} bootstrap summaries do not match"
        )
    if role != "test" and bootstrap["enabled"]:
        raise ExperimentArtifactError(
            f"{location} enables bootstrap outside a test window"
        )
    regime = _mapping(
        snapshot["market_regime_attribution"],
        f"{location}.market_regime_attribution",
    )
    if summary_case["market_regime_attribution"] != regime:
        raise ExperimentArtifactError(
            f"{location} market-regime summaries do not match"
        )

    metrics = _load_json_object(case_root / "metrics.json")
    for key in _SUMMARY_METRIC_FIELDS:
        if key not in metrics or summary_case[key] != metrics[key]:
            raise ExperimentArtifactError(
                f"{location} metric {key} does not match summary.json"
            )
    total_commission = _finite(
        metrics.get("total_commission"),
        f"{location}.metrics.total_commission",
    )
    total_sell_tax = _finite(
        metrics.get("total_sell_tax"),
        f"{location}.metrics.total_sell_tax",
    )
    total_slippage_cost = _finite(
        metrics.get("total_slippage_cost"),
        f"{location}.metrics.total_slippage_cost",
    )
    total_cost = total_commission + total_sell_tax + total_slippage_cost
    if summary_case["total_cost"] != total_cost:
        raise ExperimentArtifactError(
            f"{location} total_cost does not match metrics.json"
        )
    if bootstrap["enabled"]:
        strategy_total_return = _finite(
            metrics.get("total_return"), f"{location}.metrics.total_return"
        )
        if strategy_total_return <= -1:
            raise ExperimentArtifactError(
                f"{location}.metrics.total_return must be greater than -1"
            )
        strategy_point = bootstrap["strategy_total_return"]["point_estimate"]
        if abs(strategy_point - strategy_total_return) > 1e-12:
            raise ExperimentArtifactError(
                f"{location} bootstrap strategy point does not match metrics"
            )
        benchmark_metrics = metrics.get("benchmark")
        if bootstrap["benchmark"] is None:
            if benchmark_metrics is not None:
                raise ExperimentArtifactError(
                    f"{location} metrics contain an unexpected benchmark"
                )
        else:
            benchmark_metrics = _mapping(
                benchmark_metrics, f"{location}.metrics.benchmark"
            )
            if benchmark_metrics.get("symbol") != bootstrap["benchmark"]:
                raise ExperimentArtifactError(
                    f"{location} benchmark symbols do not match"
                )
            benchmark_point = bootstrap["benchmark_total_return"][
                "point_estimate"
            ]
            benchmark_total_return = _finite(
                benchmark_metrics.get("total_return"),
                f"{location}.metrics.benchmark.total_return",
            )
            if benchmark_total_return <= -1:
                raise ExperimentArtifactError(
                    f"{location}.metrics benchmark return must be greater than -1"
                )
            if abs(benchmark_point - benchmark_total_return) > 1e-12:
                raise ExperimentArtifactError(
                    f"{location} bootstrap benchmark point does not match metrics"
                )
            expected_relative = (
                (1 + strategy_total_return) / (1 + benchmark_total_return) - 1
            )
            relative_point = bootstrap["strategy_relative_to_benchmark"][
                "point_estimate"
            ]
            if abs(relative_point - expected_relative) > 1e-12:
                raise ExperimentArtifactError(
                    f"{location} bootstrap relative point does not reconcile"
                )
    if metrics.get("evaluation_window") != {
        "start": start.isoformat(),
        "end": end.isoformat(),
    }:
        raise ExperimentArtifactError(
            f"{location} evaluation_window does not match case snapshot"
        )

    csv_rows = {
        name: _read_csv(case_root / name, header)
        for name, header in _CSV_HEADERS.items()
    }
    nav_rows = csv_rows["nav.csv"]
    trading_days = _integer(
        metrics.get("trading_days"), f"{location}.metrics.trading_days", minimum=1
    )
    if len(nav_rows) != trading_days:
        raise ExperimentArtifactError(
            f"{location} nav row count does not match trading_days"
        )
    try:
        nav_dates = [date.fromisoformat(row["date"]) for row in nav_rows]
    except ValueError as exc:
        raise ExperimentArtifactError(
            f"{location} NAV dates must be YYYY-MM-DD"
        ) from exc
    if nav_dates != sorted(set(nav_dates)):
        raise ExperimentArtifactError(f"{location} NAV dates are not canonical")
    if nav_dates[0] < start or nav_dates[-1] > end:
        raise ExperimentArtifactError(
            f"{location} NAV dates exceed the evaluation window"
        )
    if len(csv_rows["accounting.csv"]) != trading_days:
        raise ExperimentArtifactError(
            f"{location} accounting row count does not match trading_days"
        )
    if [row["date"] for row in csv_rows["accounting.csv"]] != [
        value.isoformat() for value in nav_dates
    ]:
        raise ExperimentArtifactError(
            f"{location} accounting dates do not match NAV"
        )
    if any(row["status"] != "pass" for row in csv_rows["accounting.csv"]):
        raise ExperimentArtifactError(
            f"{location} contains a failed accounting row"
        )
    expected_counts = {
        "ledger.csv": _integer(
            metrics.get("ledger_entry_count"),
            f"{location}.metrics.ledger_entry_count",
        ),
        "signals.csv": _integer(
            metrics.get("signal_count"), f"{location}.metrics.signal_count"
        ),
    }
    for name, expected_count in expected_counts.items():
        if len(csv_rows[name]) != expected_count:
            raise ExperimentArtifactError(
                f"{location} {name} row count does not match metrics.json"
            )
    filled_trade_count = sum(
        1 for row in csv_rows["trades.csv"] if row["status"] == "filled"
    )
    if filled_trade_count != metrics["filled_trade_count"]:
        raise ExperimentArtifactError(
            f"{location} filled trade count does not match metrics.json"
        )
    if bootstrap["observation_count"] != max(0, trading_days - 1):
        raise ExperimentArtifactError(
            f"{location} bootstrap observation count does not match NAV"
        )
    regime_enabled = regime.get("enabled")
    if not isinstance(regime_enabled, bool):
        raise ExperimentArtifactError(
            f"{location}.market_regime_attribution.enabled must be boolean"
        )
    attributed_days = _integer(
        regime.get("attributed_day_count"),
        f"{location}.market_regime_attribution.attributed_day_count",
    )
    if len(csv_rows["regime_attribution.csv"]) != attributed_days:
        raise ExperimentArtifactError(
            f"{location} regime row count does not match attribution summary"
        )
    if regime_enabled and attributed_days != max(0, trading_days - 1):
        raise ExperimentArtifactError(
            f"{location} enabled regime attribution does not cover NAV returns"
        )
    if regime_enabled and [
        row["date"] for row in csv_rows["regime_attribution.csv"]
    ] != [value.isoformat() for value in nav_dates[1:]]:
        raise ExperimentArtifactError(
            f"{location} regime dates do not match NAV returns"
        )
    if not regime_enabled and attributed_days != 0:
        raise ExperimentArtifactError(
            f"{location} disabled regime attribution contains observations"
        )
    return {
        "case_id": case_id,
        "role": role,
        "bootstrap": bootstrap,
        "regime": regime,
    }


def verify_experiment_artifacts(experiment_directory: Path) -> Dict[str, Any]:
    if experiment_directory.is_symlink():
        raise ExperimentArtifactError(
            "experiment directory must not be a symbolic link"
        )
    root = experiment_directory.resolve()
    if not root.is_dir():
        raise ExperimentArtifactError(
            f"experiment directory not found: {experiment_directory}"
        )
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink():
        raise ExperimentArtifactError(
            "experiment manifest must not be a symbolic link"
        )
    manifest = _load_json_object(manifest_path)
    if manifest.get("artifact_type") != "research_experiment":
        raise ExperimentArtifactError(
            f"{manifest_path} must describe a research_experiment artifact"
        )
    schema_version = manifest.get("artifact_schema_version", 0)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version not in {0, 1}
    ):
        raise ExperimentArtifactError(
            "manifest.artifact_schema_version must be 1 or absent for v0.15"
        )
    if manifest.get("research_only") is not True:
        raise ExperimentArtifactError("experiment manifest must be research-only")
    if manifest.get("investment_validity_established") is not False:
        raise ExperimentArtifactError(
            "experiment manifest cannot establish investment validity"
        )
    created_at = _nonempty_string(manifest.get("created_at"), "manifest.created_at")
    try:
        created_timestamp = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise ExperimentArtifactError(
            "manifest.created_at must be an ISO-8601 timestamp"
        ) from exc
    if created_timestamp.tzinfo is None or created_timestamp.utcoffset() is None:
        raise ExperimentArtifactError("manifest.created_at must include a timezone")
    experiment_id = _sha256(manifest.get("experiment_id"), "manifest.experiment_id")
    experiment_input_id = _sha256(
        manifest.get("experiment_input_id"), "manifest.experiment_input_id"
    )
    result_sha256 = _sha256(
        manifest.get("result_sha256"), "manifest.result_sha256"
    )
    experiment_sha256 = _sha256(
        manifest.get("experiment_sha256"), "manifest.experiment_sha256"
    )
    policy_sha256 = _sha256(
        manifest.get("policy_sha256"), "manifest.policy_sha256"
    )
    source_tree_sha256 = _sha256(
        manifest.get("source_tree_sha256"), "manifest.source_tree_sha256"
    )
    market_source = _mapping(manifest.get("market_source"), "manifest.market_source")
    market_source_sha256 = _sha256(
        market_source.get("sha256"), "manifest.market_source.sha256"
    )
    expected_experiment_id = hashlib.sha256(
        (
            experiment_input_id
            + source_tree_sha256
            + policy_sha256
            + experiment_sha256
            + market_source_sha256
        ).encode("utf-8")
    ).hexdigest()
    if experiment_id != expected_experiment_id:
        raise ExperimentArtifactError(
            "manifest.experiment_id does not match its bound input hashes"
        )
    _nonempty_string(manifest.get("python_version"), "manifest.python_version")

    declared_files, declared_hashes, case_files = _verify_manifest_files(
        root, manifest
    )
    summary = _load_json_object(root / "summary.json")
    if summary.get("research_only") is not True:
        raise ExperimentArtifactError("summary.json must be research-only")
    if summary.get("investment_validity_established") is not False:
        raise ExperimentArtifactError(
            "summary.json cannot establish investment validity"
        )
    experiment_snapshot = _load_json_object(
        root / "experiment.snapshot.json"
    )
    policy_snapshot = _load_json_object(root / "policy.snapshot.json")
    if summary.get("experiment_name") != experiment_snapshot.get("name"):
        raise ExperimentArtifactError(
            "summary experiment_name does not match experiment snapshot"
        )
    _nonempty_string(policy_snapshot.get("name"), "policy.snapshot.name")
    cases = _list(summary.get("cases"), "summary.cases")
    case_count = _integer(summary.get("case_count"), "summary.case_count", minimum=1)
    if case_count != len(cases) or case_count != len(case_files):
        raise ExperimentArtifactError(
            "summary case_count does not match cases or case directories"
        )

    summary_by_id: Dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(cases):
        case = _mapping(value, f"summary.cases[{index}]")
        case_id = _sha256(case.get("case_id"), f"summary.cases[{index}].case_id")
        if case_id in summary_by_id:
            raise ExperimentArtifactError("summary.json contains duplicate case_id")
        summary_by_id[case_id] = case

    verified_cases = []
    seen_case_ids = set()
    for case_directory in sorted(case_files):
        snapshot = _load_json_object(
            root / "cases" / case_directory / "case.snapshot.json"
        )
        case_id = _sha256(
            snapshot.get("case_id"), f"cases/{case_directory}.case_id"
        )
        if case_id not in summary_by_id:
            raise ExperimentArtifactError(
                f"cases/{case_directory} is absent from summary.json"
            )
        if case_id in seen_case_ids:
            raise ExperimentArtifactError(
                f"multiple case directories use case_id {case_id}"
            )
        seen_case_ids.add(case_id)
        verified_cases.append(
            _verify_case(
                root,
                case_directory,
                summary_by_id[case_id],
                experiment_input_id,
                result_sha256,
            )
        )
    if seen_case_ids != set(summary_by_id):
        raise ExperimentArtifactError(
            "summary.json contains cases without artifact directories"
        )

    protocols = {
        json.dumps(case["bootstrap"]["protocol"], sort_keys=True)
        for case in verified_cases
    }
    benchmarks = {case["bootstrap"]["benchmark"] for case in verified_cases}
    if len(protocols) != 1 or len(benchmarks) != 1:
        raise ExperimentArtifactError(
            "bootstrap protocol or benchmark changes across cases"
        )
    seed_hashes = [case["bootstrap"]["seed_sha256"] for case in verified_cases]
    if len(seed_hashes) != len(set(seed_hashes)):
        raise ExperimentArtifactError("bootstrap seed hashes must be unique per case")

    test_cases = [case for case in verified_cases if case["role"] == "test"]
    enabled_test_cases = [
        case for case in test_cases if case["bootstrap"]["enabled"]
    ]
    bootstrap_summary = _mapping(
        summary.get("test_bootstrap_uncertainty"),
        "summary.test_bootstrap_uncertainty",
    )
    expected_bootstrap_values = {
        "benchmark": verified_cases[0]["bootstrap"]["benchmark"],
        "disabled_case_count": len(test_cases) - len(enabled_test_cases),
        "enabled": bool(enabled_test_cases),
        "enabled_case_count": len(enabled_test_cases),
        "investment_validity_established": False,
        "p_value_reported": False,
        "pooled_performance_estimate": False,
        "protocol": verified_cases[0]["bootstrap"]["protocol"],
        "test_case_count": len(test_cases),
        "test_only": True,
        "used_for_parameter_selection": False,
        "used_for_strategy_decisions": False,
    }
    for key, expected in expected_bootstrap_values.items():
        if bootstrap_summary.get(key) != expected:
            raise ExperimentArtifactError(
                f"summary.test_bootstrap_uncertainty.{key} is inconsistent"
            )
    comparisons = _list(
        bootstrap_summary.get("comparisons"),
        "summary.test_bootstrap_uncertainty.comparisons",
    )
    if bootstrap_summary.get("comparison_count") != len(comparisons):
        raise ExperimentArtifactError(
            "bootstrap comparison_count does not match comparisons"
        )

    regime_summary = _mapping(
        summary.get("test_market_regime_attribution"),
        "summary.test_market_regime_attribution",
    )
    regime_enabled = {case["regime"].get("enabled") for case in verified_cases}
    regime_benchmarks = {case["regime"].get("benchmark") for case in verified_cases}
    regime_protocols = {
        json.dumps(case["regime"].get("protocol"), sort_keys=True)
        for case in verified_cases
    }
    if (
        len(regime_enabled) != 1
        or len(regime_benchmarks) != 1
        or len(regime_protocols) != 1
    ):
        raise ExperimentArtifactError(
            "market-regime protocol or enabled state changes across cases"
        )
    if (
        regime_summary.get("enabled") != next(iter(regime_enabled))
        or regime_summary.get("benchmark") != next(iter(regime_benchmarks))
        or regime_summary.get("protocol") != verified_cases[0]["regime"].get("protocol")
        or regime_summary.get("test_case_count") != len(test_cases)
    ):
        raise ExperimentArtifactError(
            "test market-regime summary is inconsistent with cases"
        )

    return {
        "status": "pass",
        "artifact_type": "research_experiment",
        "artifact_schema_version": schema_version,
        "legacy_schema_inferred": schema_version == 0,
        "experiment_directory": str(root),
        "experiment_id": experiment_id,
        "experiment_input_id": experiment_input_id,
        "result_sha256": result_sha256,
        "manifest_sha256": file_sha256(manifest_path),
        "verified_file_count": len(declared_hashes),
        "case_count": case_count,
        "test_case_count": len(test_cases),
        "bootstrap_enabled_test_case_count": len(enabled_test_cases),
        "file_hashes_verified": True,
        "cross_file_consistency_verified": True,
        "replay_performed": False,
        "artifact_authenticity_verified": False,
        "investment_validity_established": False,
        "automatic_execution_allowed": False,
        "declared_file_count": len(declared_files),
    }
