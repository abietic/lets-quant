from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from . import __version__
from .experiment_verification import verify_experiment_artifacts


class ExperimentComparisonError(ValueError):
    """Raised when verified experiments cannot be compared safely."""


_CASE_METRICS = (
    "total_return",
    "annualized_return",
    "annualized_volatility",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown",
    "max_drawdown_duration_trading_days",
    "turnover_ratio",
    "total_cost",
    "decision_count",
    "filled_trade_count",
)
_MAX_JSON_DIFFERENCES = 200


def _load_json_object(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExperimentComparisonError(f"file not found: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentComparisonError(f"invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ExperimentComparisonError(f"{path} must contain a JSON object")
    return payload


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _pointer(path: str, component: Any) -> str:
    escaped = str(component).replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"


def _json_differences(
    baseline: Any,
    candidate: Any,
    *,
    limit: int = _MAX_JSON_DIFFERENCES,
) -> Dict[str, Any]:
    differences: List[Dict[str, Any]] = []
    difference_count = 0

    def record(payload: Dict[str, Any]) -> None:
        nonlocal difference_count
        difference_count += 1
        if len(differences) < limit:
            differences.append(payload)

    def visit(left: Any, right: Any, path: str) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right), key=str):
                location = _pointer(path, key)
                if key not in left:
                    record(
                        {
                            "path": location,
                            "baseline_present": False,
                            "candidate_present": True,
                            "candidate": right[key],
                        }
                    )
                elif key not in right:
                    record(
                        {
                            "path": location,
                            "baseline_present": True,
                            "candidate_present": False,
                            "baseline": left[key],
                        }
                    )
                else:
                    visit(left[key], right[key], location)
            return
        if isinstance(left, list) and isinstance(right, list):
            common_length = min(len(left), len(right))
            for index in range(common_length):
                visit(left[index], right[index], _pointer(path, index))
            for index in range(common_length, len(left)):
                record(
                    {
                        "path": _pointer(path, index),
                        "baseline_present": True,
                        "candidate_present": False,
                        "baseline": left[index],
                    }
                )
            for index in range(common_length, len(right)):
                record(
                    {
                        "path": _pointer(path, index),
                        "baseline_present": False,
                        "candidate_present": True,
                        "candidate": right[index],
                    }
                )
            return
        if left != right:
            record(
                {
                    "path": path or "/",
                    "baseline_present": True,
                    "candidate_present": True,
                    "baseline": left,
                    "candidate": right,
                }
            )

    visit(baseline, candidate, "")
    return {
        "equal": difference_count == 0,
        "difference_count": difference_count,
        "reported_difference_count": len(differences),
        "truncated": difference_count > len(differences),
        "differences": differences,
    }


def _value_comparison(baseline: Any, candidate: Any) -> Dict[str, Any]:
    return {
        "baseline": baseline,
        "candidate": candidate,
        "equal": baseline == candidate,
    }


def _optional_value_comparison(
    baseline: Any, candidate: Any
) -> Dict[str, Any]:
    baseline_available = baseline is not None
    candidate_available = candidate is not None
    return {
        "baseline": baseline,
        "candidate": candidate,
        "baseline_available": baseline_available,
        "candidate_available": candidate_available,
        "equal": (
            baseline == candidate
            if baseline_available and candidate_available
            else None
        ),
    }


def _artifact_context(directory: Path) -> Dict[str, Any]:
    verification = verify_experiment_artifacts(directory)
    root = Path(verification["experiment_directory"])
    manifest = _load_json_object(root / "manifest.json")
    summary = _load_json_object(root / "summary.json")
    policy = _load_json_object(root / "policy.snapshot.json")
    experiment = _load_json_object(root / "experiment.snapshot.json")
    market_source = manifest.get("market_source")
    if not isinstance(market_source, dict):
        raise ExperimentComparisonError("manifest.market_source is malformed")
    replay_input = manifest.get("replay_input")
    if isinstance(replay_input, dict):
        market_sha256 = replay_input.get("market_sha256")
    else:
        market_snapshot_path = root / "market.snapshot.json"
        if market_snapshot_path.is_file():
            market_snapshot = _load_json_object(market_snapshot_path)
            market_sha256 = _canonical_sha256(market_snapshot.get("market"))
        else:
            market_sha256 = None
    return {
        "root": root,
        "verification": verification,
        "manifest": manifest,
        "summary": summary,
        "policy": policy,
        "experiment": experiment,
        "market": {
            "identity_sha256": market_sha256,
            "source_type": market_source.get("type"),
            "source_sha256": market_source.get("sha256"),
            "portable_replay_input_verified": verification[
                "replay_input_verified"
            ],
        },
    }


def _experiment_lookups(
    experiment: Mapping[str, Any],
) -> Tuple[Dict[Tuple[str, str, str], Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    windows = experiment.get("windows")
    scenarios = experiment.get("execution_scenarios")
    if not isinstance(windows, list) or not isinstance(scenarios, list):
        raise ExperimentComparisonError("experiment snapshot is malformed")
    window_lookup: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for value in windows:
        if not isinstance(value, dict):
            raise ExperimentComparisonError("experiment window is malformed")
        normalized = dict(value)
        normalized.setdefault("fold", "default")
        key = (
            normalized.get("fold"),
            normalized.get("name"),
            normalized.get("role"),
        )
        if not all(isinstance(item, str) for item in key) or key in window_lookup:
            raise ExperimentComparisonError(
                "experiment windows cannot form unique comparison keys"
            )
        window_lookup[key] = normalized
    scenario_lookup: Dict[str, Dict[str, Any]] = {}
    for value in scenarios:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise ExperimentComparisonError("execution scenario is malformed")
        name = value["name"]
        if name in scenario_lookup:
            raise ExperimentComparisonError(
                "execution scenarios cannot form unique comparison keys"
            )
        scenario_lookup[name] = dict(value)
    return window_lookup, scenario_lookup


def _case_contract(
    case: Mapping[str, Any],
    windows: Mapping[Tuple[str, str, str], Mapping[str, Any]],
    scenarios: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    window_key = (case.get("fold"), case.get("window"), case.get("role"))
    window = windows.get(window_key)
    if window is None:
        raise ExperimentComparisonError(
            f"summary case has no matching experiment window: {window_key}"
        )
    scenario_name = case.get("execution_scenario")
    scenario = scenarios.get(scenario_name)
    if scenario is None:
        raise ExperimentComparisonError(
            f"summary case has no matching execution scenario: {scenario_name}"
        )
    parameter_variant = case.get("parameter_overrides")
    if not isinstance(parameter_variant, dict):
        raise ExperimentComparisonError("case parameter overrides are malformed")
    if parameter_variant.get("name") != case.get("parameter_variant"):
        raise ExperimentComparisonError(
            "case parameter variant does not match its overrides"
        )
    return {
        "window": dict(window),
        "execution_scenario": dict(scenario),
        "parameter_variant": dict(parameter_variant),
    }


def _case_map(context: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    experiment = context["experiment"]
    summary = context["summary"]
    windows, scenarios = _experiment_lookups(experiment)
    cases = summary.get("cases")
    if not isinstance(cases, list):
        raise ExperimentComparisonError("experiment summary cases are malformed")
    mapped: Dict[str, Dict[str, Any]] = {}
    for value in cases:
        if not isinstance(value, dict):
            raise ExperimentComparisonError("experiment summary case is malformed")
        contract = _case_contract(value, windows, scenarios)
        contract_sha256 = _canonical_sha256(contract)
        if contract_sha256 in mapped:
            raise ExperimentComparisonError(
                "experiment contains duplicate case comparison contracts"
            )
        mapped[contract_sha256] = {
            "contract_sha256": contract_sha256,
            "contract": contract,
            "case": value,
        }
    return mapped


def _case_identity(value: Mapping[str, Any]) -> Dict[str, Any]:
    case = value["case"]
    return {
        "contract_sha256": value["contract_sha256"],
        "contract": value["contract"],
        "case_id": case["case_id"],
    }


def _compare_aligned_case(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> Dict[str, Any]:
    baseline_case = baseline["case"]
    candidate_case = candidate["case"]
    metrics = {}
    changed_metric_count = 0
    for name in _CASE_METRICS:
        baseline_value = baseline_case[name]
        candidate_value = candidate_case[name]
        changed = baseline_value != candidate_value
        changed_metric_count += int(changed)
        metrics[name] = {
            "baseline": baseline_value,
            "candidate": candidate_value,
            "delta": candidate_value - baseline_value,
            "changed": changed,
        }
    regime_equal = (
        baseline_case["market_regime_attribution"]
        == candidate_case["market_regime_attribution"]
    )
    bootstrap_equal = (
        baseline_case["bootstrap_uncertainty"]
        == candidate_case["bootstrap_uncertainty"]
    )
    return {
        "contract_sha256": baseline["contract_sha256"],
        "contract": baseline["contract"],
        "baseline_case_id": baseline_case["case_id"],
        "candidate_case_id": candidate_case["case_id"],
        "metrics": metrics,
        "changed_metric_count": changed_metric_count,
        "market_regime_attribution_equal": regime_equal,
        "bootstrap_uncertainty_equal": bootstrap_equal,
        "changed": (
            changed_metric_count > 0 or not regime_equal or not bootstrap_equal
        ),
    }


def _artifact_identity(context: Mapping[str, Any]) -> Dict[str, Any]:
    manifest = context["manifest"]
    verification = context["verification"]
    return {
        "experiment_directory": str(context["root"]),
        "artifact_schema_version": verification["artifact_schema_version"],
        "manifest_sha256": verification["manifest_sha256"],
        "experiment_id": manifest["experiment_id"],
        "experiment_input_id": manifest["experiment_input_id"],
        "result_sha256": manifest["result_sha256"],
        "source_tree_sha256": manifest["source_tree_sha256"],
        "source_revision": manifest.get("source_revision"),
        "python_version": manifest["python_version"],
    }


def compare_experiment_artifacts(
    baseline_directory: Path, candidate_directory: Path
) -> Dict[str, Any]:
    baseline = _artifact_context(baseline_directory)
    candidate = _artifact_context(candidate_directory)
    baseline_cases = _case_map(baseline)
    candidate_cases = _case_map(candidate)
    baseline_keys = set(baseline_cases)
    candidate_keys = set(candidate_cases)
    aligned_keys = sorted(baseline_keys & candidate_keys)
    baseline_only_keys = sorted(baseline_keys - candidate_keys)
    candidate_only_keys = sorted(candidate_keys - baseline_keys)
    aligned_cases = [
        _compare_aligned_case(
            baseline_cases[contract_sha256],
            candidate_cases[contract_sha256],
        )
        for contract_sha256 in aligned_keys
    ]
    changed_case_count = sum(case["changed"] for case in aligned_cases)

    baseline_manifest = baseline["manifest"]
    candidate_manifest = candidate["manifest"]
    input_id_equal = (
        baseline_manifest["experiment_input_id"]
        == candidate_manifest["experiment_input_id"]
    )
    result_equal = (
        baseline_manifest["result_sha256"]
        == candidate_manifest["result_sha256"]
    )
    alignment_complete = baseline_keys == candidate_keys
    if input_id_equal and result_equal:
        comparison_status = "identical"
    elif alignment_complete:
        comparison_status = "aligned_with_differences"
    elif aligned_keys:
        comparison_status = "partially_aligned"
    else:
        comparison_status = "not_aligned"

    input_comparison = {
        "experiment_input_id": _value_comparison(
            baseline_manifest["experiment_input_id"],
            candidate_manifest["experiment_input_id"],
        ),
        "policy": _json_differences(
            baseline["policy"], candidate["policy"]
        ),
        "experiment": _json_differences(
            baseline["experiment"], candidate["experiment"]
        ),
        "market": {
            "identity_sha256": _optional_value_comparison(
                baseline["market"]["identity_sha256"],
                candidate["market"]["identity_sha256"],
            ),
            "source_type": _optional_value_comparison(
                baseline["market"]["source_type"],
                candidate["market"]["source_type"],
            ),
            "source_sha256": _value_comparison(
                baseline["market"]["source_sha256"],
                candidate["market"]["source_sha256"],
            ),
            "portable_replay_input_verified": {
                "baseline": baseline["market"][
                    "portable_replay_input_verified"
                ],
                "candidate": candidate["market"][
                    "portable_replay_input_verified"
                ],
            },
        },
        "runtime": {
            "python_version": _value_comparison(
                baseline_manifest["python_version"],
                candidate_manifest["python_version"],
            ),
            "source_tree_sha256": _value_comparison(
                baseline_manifest["source_tree_sha256"],
                candidate_manifest["source_tree_sha256"],
            ),
            "source_revision": _optional_value_comparison(
                baseline_manifest.get("source_revision"),
                candidate_manifest.get("source_revision"),
            ),
        },
    }
    summary = {
        "comparison_status": comparison_status,
        "baseline_case_count": len(baseline_cases),
        "candidate_case_count": len(candidate_cases),
        "aligned_case_count": len(aligned_cases),
        "baseline_only_case_count": len(baseline_only_keys),
        "candidate_only_case_count": len(candidate_only_keys),
        "changed_aligned_case_count": changed_case_count,
        "unchanged_aligned_case_count": len(aligned_cases)
        - changed_case_count,
        "case_alignment_complete": alignment_complete,
        "experiment_input_id_equal": input_id_equal,
        "result_sha256_equal": result_equal,
    }
    report = {
        "artifact_type": "research_experiment_comparison",
        "report_schema_version": 1,
        "comparison_tool_version": __version__,
        "status": "completed",
        "comparison_status": comparison_status,
        "baseline": _artifact_identity(baseline),
        "candidate": _artifact_identity(candidate),
        "input_comparison": input_comparison,
        "case_comparison": {
            "metric_delta_definition": "candidate_minus_baseline",
            "alignment_complete": alignment_complete,
            "aligned": aligned_cases,
            "baseline_only": [
                _case_identity(baseline_cases[key])
                for key in baseline_only_keys
            ],
            "candidate_only": [
                _case_identity(candidate_cases[key])
                for key in candidate_only_keys
            ],
        },
        "summary": summary,
        "descriptive_only": True,
        "ranking_performed": False,
        "preferred_experiment": None,
        "automatic_parameter_selection": False,
        "artifact_authenticity_verified": False,
        "investment_validity_established": False,
        "automatic_execution_allowed": False,
    }
    return {**report, "report_sha256": _canonical_sha256(report)}


def verify_experiment_comparison_report(report: Mapping[str, Any]) -> None:
    expected = report.get("report_sha256")
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or any(character not in "0123456789abcdef" for character in expected)
    ):
        raise ExperimentComparisonError(
            "comparison report has an invalid report_sha256"
        )
    payload = {key: value for key, value in report.items() if key != "report_sha256"}
    actual = _canonical_sha256(payload)
    if actual != expected:
        raise ExperimentComparisonError(
            f"comparison report hash mismatch: expected {expected}, got {actual}"
        )


def write_experiment_comparison_report(
    report: Mapping[str, Any], path: Path
) -> None:
    verify_experiment_comparison_report(report)
    output_path = path.resolve()
    for role in ("baseline", "candidate"):
        identity = report.get(role)
        if not isinstance(identity, dict):
            raise ExperimentComparisonError(
                f"comparison report {role} identity is malformed"
            )
        root_value = identity.get("experiment_directory")
        if not isinstance(root_value, str):
            raise ExperimentComparisonError(
                f"comparison report {role} directory is malformed"
            )
        try:
            output_path.relative_to(Path(root_value).resolve())
        except ValueError:
            pass
        else:
            raise ExperimentComparisonError(
                "comparison report must not be written inside an experiment "
                f"directory: {path}"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
    except FileExistsError as exc:
        raise ExperimentComparisonError(
            f"comparison report already exists: {path}"
        ) from exc
