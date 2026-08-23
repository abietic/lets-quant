from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import __version__
from .experiment_verification import verify_experiment_artifacts


class ExperimentCatalogError(ValueError):
    """Raised when an experiment catalog cannot be built or verified safely."""


_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_object(value: bytes, location: str) -> Dict[str, Any]:
    try:
        payload = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentCatalogError(f"invalid JSON: {location}") from exc
    if not isinstance(payload, dict):
        raise ExperimentCatalogError(f"{location} must contain a JSON object")
    return payload


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ExperimentCatalogError(f"{location} must be a JSON object")
    return value


def _sequence(value: Any, location: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ExperimentCatalogError(f"{location} must be a JSON array")
    return value


def _nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentCatalogError(f"{location} must be a non-empty string")
    return value


def _optional_string(value: Any, location: str) -> Optional[str]:
    if value is None:
        return None
    return _nonempty_string(value, location)


def _nonnegative_integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExperimentCatalogError(f"{location} must be an integer >= 0")
    return value


def _bound_payloads(
    root: Path, verification: Mapping[str, Any]
) -> Dict[str, Dict[str, Any]]:
    manifest_path = root / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    if _bytes_sha256(manifest_bytes) != verification["manifest_sha256"]:
        raise ExperimentCatalogError(
            f"manifest changed while cataloging: {manifest_path}"
        )
    manifest = _json_object(manifest_bytes, str(manifest_path))
    declared_hashes = _mapping(
        manifest.get("file_sha256"), "manifest.file_sha256"
    )

    payloads = {"manifest": manifest}
    for stem in ("experiment", "policy", "summary"):
        name = f"{stem}.snapshot.json" if stem != "summary" else "summary.json"
        path = root / name
        value = path.read_bytes()
        expected = declared_hashes.get(name)
        if not isinstance(expected, str) or _bytes_sha256(value) != expected:
            raise ExperimentCatalogError(
                f"verified file changed while cataloging: {path}"
            )
        payloads[stem] = _json_object(value, str(path))
    return payloads


def _invalid_entry(candidate: Path, root: Path, exc: Exception) -> Dict[str, Any]:
    entry = {
        "directory_name": candidate.name,
        "experiment_directory": str(root / candidate.name),
        "verification_state": "verification_failed",
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    if candidate.is_symlink():
        return entry

    manifest_path = candidate / "manifest.json"
    if manifest_path.is_symlink():
        return entry
    try:
        manifest = _json_object(
            manifest_path.read_bytes(),
            str(manifest_path),
        )
    except (OSError, ValueError):
        return entry
    if (
        manifest.get("artifact_type") == "research_experiment"
        and "artifact_schema_version" not in manifest
        and "file_sha256" not in manifest
    ):
        entry["verification_state"] = "unverifiable_legacy_format"
        entry["format_hint"] = "manifest_without_schema_or_file_hashes"
    return entry


def _catalog_entry(
    directory_name: str,
    verification: Mapping[str, Any],
    payloads: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    manifest = payloads["manifest"]
    experiment = payloads["experiment"]
    policy = payloads["policy"]
    summary = payloads["summary"]
    strategy = _mapping(policy.get("strategy"), "policy.snapshot.strategy")
    market_source = _mapping(
        manifest.get("market_source"), "manifest.market_source"
    )
    walk_forward = _mapping(summary.get("walk_forward"), "summary.walk_forward")
    stability = _mapping(
        summary.get("test_parameter_stability"),
        "summary.test_parameter_stability",
    )
    windows = _sequence(experiment.get("windows"), "experiment.snapshot.windows")
    scenarios = _sequence(
        experiment.get("execution_scenarios"),
        "experiment.snapshot.execution_scenarios",
    )
    created_at = _nonempty_string(manifest.get("created_at"), "manifest.created_at")
    try:
        datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise ExperimentCatalogError(
            "manifest.created_at must be an ISO-8601 timestamp"
        ) from exc

    return {
        "directory_name": directory_name,
        "experiment_directory": verification["experiment_directory"],
        "created_at": created_at,
        "artifact_schema_version": verification["artifact_schema_version"],
        "legacy_schema_inferred": verification["legacy_schema_inferred"],
        "manifest_sha256": verification["manifest_sha256"],
        "experiment_id": verification["experiment_id"],
        "experiment_input_id": verification["experiment_input_id"],
        "result_sha256": verification["result_sha256"],
        "experiment_name": _nonempty_string(
            summary.get("experiment_name"), "summary.experiment_name"
        ),
        "experiment_schema_version": _nonnegative_integer(
            experiment.get("schema_version", 1),
            "experiment.snapshot.schema_version",
        ),
        "policy_name": _nonempty_string(
            policy.get("name"), "policy.snapshot.name"
        ),
        "strategy_kind": _nonempty_string(
            strategy.get("kind"), "policy.snapshot.strategy.kind"
        ),
        "market_source_type": _nonempty_string(
            market_source.get("type"), "manifest.market_source.type"
        ),
        "market_source_sha256": _nonempty_string(
            market_source.get("sha256"), "manifest.market_source.sha256"
        ),
        "case_count": verification["case_count"],
        "test_case_count": verification["test_case_count"],
        "fold_count": _nonnegative_integer(
            walk_forward.get("fold_count"), "summary.walk_forward.fold_count"
        ),
        "window_count": len(windows),
        "execution_scenario_count": len(scenarios),
        "parameter_variant_count": _nonnegative_integer(
            stability.get("variant_count"),
            "summary.test_parameter_stability.variant_count",
        ),
        "bootstrap_enabled_test_case_count": verification[
            "bootstrap_enabled_test_case_count"
        ],
        "portable_replay_input_verified": verification[
            "replay_input_verified"
        ],
        "python_version": _nonempty_string(
            manifest.get("python_version"), "manifest.python_version"
        ),
        "source_revision": _optional_string(
            manifest.get("source_revision"), "manifest.source_revision"
        ),
        "source_tree_sha256": _nonempty_string(
            manifest.get("source_tree_sha256"),
            "manifest.source_tree_sha256",
        ),
    }


def _entry_sort_key(entry: Mapping[str, Any]) -> tuple[float, str]:
    return (
        datetime.fromisoformat(entry["created_at"]).timestamp(),
        entry["directory_name"],
    )


def _experiment_groups(
    entries: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Mapping[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(entry["experiment_id"], []).append(entry)

    result = []
    for experiment_id in sorted(grouped):
        members = sorted(grouped[experiment_id], key=_entry_sort_key, reverse=True)
        result_hashes = sorted({member["result_sha256"] for member in members})
        result.append(
            {
                "experiment_id": experiment_id,
                "artifact_count": len(members),
                "repeated": len(members) > 1,
                "result_consistent": len(result_hashes) == 1,
                "distinct_result_count": len(result_hashes),
                "result_sha256s": result_hashes,
                "members": [
                    {
                        "directory_name": member["directory_name"],
                        "experiment_directory": member[
                            "experiment_directory"
                        ],
                        "created_at": member["created_at"],
                        "manifest_sha256": member["manifest_sha256"],
                        "result_sha256": member["result_sha256"],
                        "source_revision": member["source_revision"],
                    }
                    for member in members
                ],
            }
        )
    return result


def _review_items(
    entries: Sequence[Mapping[str, Any]],
    invalid_entries: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for entry in invalid_entries:
        legacy_format = entry["verification_state"] == "unverifiable_legacy_format"
        items.append(
            {
                "severity": "error",
                "code": (
                    "legacy_artifact_unverifiable"
                    if legacy_format
                    else "artifact_verification_failed"
                ),
                "experiment_directory": entry["experiment_directory"],
                "verification_state": entry["verification_state"],
                "error_type": entry["error_type"],
                "error": entry["error"],
            }
        )
    for group in groups:
        if group["artifact_count"] <= 1:
            continue
        directories = [
            member["experiment_directory"] for member in group["members"]
        ]
        if group["result_consistent"]:
            items.append(
                {
                    "severity": "info",
                    "code": "repeated_verified_experiment",
                    "experiment_id": group["experiment_id"],
                    "artifact_count": group["artifact_count"],
                    "experiment_directories": directories,
                    "automatic_cleanup_performed": False,
                }
            )
        else:
            items.append(
                {
                    "severity": "error",
                    "code": "inconsistent_results_for_experiment_id",
                    "experiment_id": group["experiment_id"],
                    "artifact_count": group["artifact_count"],
                    "result_sha256s": group["result_sha256s"],
                    "experiment_directories": directories,
                }
            )

    legacy = [
        entry
        for entry in entries
        if not entry["portable_replay_input_verified"]
    ]
    if legacy:
        items.append(
            {
                "severity": "warning",
                "code": "verified_but_nonportable_replay_input",
                "artifact_count": len(legacy),
                "experiment_directories": [
                    entry["experiment_directory"] for entry in legacy
                ],
                "replay_performed": False,
            }
        )

    def sort_key(item: Mapping[str, Any]) -> tuple[int, str, str]:
        directory = item.get("experiment_directory", "")
        identity = item.get("experiment_id", "")
        return (_SEVERITY_ORDER[item["severity"]], item["code"], directory + identity)

    return sorted(items, key=sort_key)


def _catalog_summary(
    entries: Sequence[Mapping[str, Any]],
    invalid_entries: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
    review_items: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    schema_counts: Dict[str, int] = {}
    for entry in entries:
        key = str(entry["artifact_schema_version"])
        schema_counts[key] = schema_counts.get(key, 0) + 1
    repeated_groups = [group for group in groups if group["repeated"]]
    consistent_repeated_groups = [
        group for group in repeated_groups if group["result_consistent"]
    ]
    inconsistent_groups = [
        group for group in repeated_groups if not group["result_consistent"]
    ]
    return {
        "candidate_directory_count": len(entries) + len(invalid_entries),
        "verified_artifact_count": len(entries),
        "invalid_artifact_count": len(invalid_entries),
        "unverifiable_legacy_artifact_count": sum(
            entry["verification_state"] == "unverifiable_legacy_format"
            for entry in invalid_entries
        ),
        "artifact_schema_counts": dict(sorted(schema_counts.items())),
        "portable_replay_artifact_count": sum(
            bool(entry["portable_replay_input_verified"]) for entry in entries
        ),
        "nonportable_replay_artifact_count": sum(
            not entry["portable_replay_input_verified"] for entry in entries
        ),
        "experiment_identity_count": len(groups),
        "repeated_experiment_group_count": len(repeated_groups),
        "consistent_repeated_group_count": len(consistent_repeated_groups),
        "redundant_verified_artifact_count": sum(
            group["artifact_count"] - 1 for group in consistent_repeated_groups
        ),
        "inconsistent_result_group_count": len(inconsistent_groups),
        "review_item_count": len(review_items),
        "blocking_issue_count": sum(
            item["severity"] == "error" for item in review_items
        ),
    }


def build_experiment_catalog(experiments_root: Path) -> Dict[str, Any]:
    if experiments_root.is_symlink():
        raise ExperimentCatalogError(
            "experiments root must not be a symbolic link"
        )
    root = experiments_root.resolve()
    if not root.is_dir():
        raise ExperimentCatalogError(
            f"experiments root not found: {experiments_root}"
        )

    candidates = [
        child
        for child in sorted(root.iterdir(), key=lambda value: value.name)
        if child.is_symlink() or child.is_dir()
    ]
    entries: List[Dict[str, Any]] = []
    invalid_entries: List[Dict[str, Any]] = []
    for candidate in candidates:
        try:
            verification = verify_experiment_artifacts(candidate)
            verified_root = Path(verification["experiment_directory"])
            payloads = _bound_payloads(verified_root, verification)
            entries.append(
                _catalog_entry(candidate.name, verification, payloads)
            )
        except (OSError, ValueError) as exc:
            invalid_entries.append(_invalid_entry(candidate, root, exc))

    entries.sort(key=_entry_sort_key, reverse=True)
    invalid_entries.sort(key=lambda entry: entry["directory_name"])
    groups = _experiment_groups(entries)
    review_items = _review_items(entries, invalid_entries, groups)
    summary = _catalog_summary(entries, invalid_entries, groups, review_items)
    if not candidates:
        status = "empty"
    elif summary["blocking_issue_count"]:
        status = "attention_required"
    else:
        status = "pass"

    catalog = {
        "artifact_type": "research_experiment_catalog",
        "catalog_schema_version": 1,
        "catalog_tool_version": __version__,
        "status": status,
        "experiments_root": str(root),
        "discovery_scope": "immediate_child_directories_only",
        "entries": entries,
        "invalid_entries": invalid_entries,
        "experiment_groups": groups,
        "review_items": review_items,
        "summary": summary,
        "descriptive_only": True,
        "ranking_performed": False,
        "preferred_experiment": None,
        "automatic_cleanup_performed": False,
        "artifact_authenticity_verified": False,
        "replay_performed": False,
        "investment_validity_established": False,
        "automatic_execution_allowed": False,
    }
    return {**catalog, "catalog_sha256": _canonical_sha256(catalog)}


def verify_experiment_catalog_report(catalog: Mapping[str, Any]) -> None:
    if catalog.get("artifact_type") != "research_experiment_catalog":
        raise ExperimentCatalogError(
            "catalog must describe a research_experiment_catalog"
        )
    if catalog.get("catalog_schema_version") != 1:
        raise ExperimentCatalogError("catalog schema version must be 1")
    for key, expected in (
        ("descriptive_only", True),
        ("ranking_performed", False),
        ("preferred_experiment", None),
        ("automatic_cleanup_performed", False),
        ("artifact_authenticity_verified", False),
        ("replay_performed", False),
        ("investment_validity_established", False),
        ("automatic_execution_allowed", False),
    ):
        if catalog.get(key) != expected:
            raise ExperimentCatalogError(f"catalog boundary {key} is invalid")

    entries = _sequence(catalog.get("entries"), "catalog.entries")
    invalid_entries = _sequence(
        catalog.get("invalid_entries"), "catalog.invalid_entries"
    )
    groups = _sequence(
        catalog.get("experiment_groups"), "catalog.experiment_groups"
    )
    review_items = _sequence(
        catalog.get("review_items"), "catalog.review_items"
    )
    expected_groups = _experiment_groups(entries)
    if list(groups) != expected_groups:
        raise ExperimentCatalogError("catalog experiment groups are inconsistent")
    expected_review_items = _review_items(entries, invalid_entries, groups)
    if list(review_items) != expected_review_items:
        raise ExperimentCatalogError("catalog review items are inconsistent")
    expected_summary = _catalog_summary(
        entries, invalid_entries, groups, review_items
    )
    if catalog.get("summary") != expected_summary:
        raise ExperimentCatalogError("catalog summary is inconsistent")
    expected_status = (
        "empty"
        if expected_summary["candidate_directory_count"] == 0
        else (
            "attention_required"
            if expected_summary["blocking_issue_count"]
            else "pass"
        )
    )
    if catalog.get("status") != expected_status:
        raise ExperimentCatalogError("catalog status is inconsistent")

    expected_hash = catalog.get("catalog_sha256")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
    ):
        raise ExperimentCatalogError("catalog has an invalid catalog_sha256")
    payload = {
        key: value for key, value in catalog.items() if key != "catalog_sha256"
    }
    actual_hash = _canonical_sha256(payload)
    if actual_hash != expected_hash:
        raise ExperimentCatalogError(
            f"catalog hash mismatch: expected {expected_hash}, got {actual_hash}"
        )


def write_experiment_catalog(catalog: Mapping[str, Any], path: Path) -> None:
    verify_experiment_catalog_report(catalog)
    root = Path(
        _nonempty_string(catalog.get("experiments_root"), "catalog.experiments_root")
    ).resolve()
    output_path = path.resolve()
    try:
        output_path.relative_to(root)
    except ValueError:
        pass
    else:
        raise ExperimentCatalogError(
            f"catalog must not be written inside the experiments root: {path}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(catalog, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
    except FileExistsError as exc:
        raise ExperimentCatalogError(f"catalog already exists: {path}") from exc
