from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence


SCHEMA_VERSION = 1
NAV_FIELDS = ["date", "nav", "cash", "positions"]
TRADE_FIELDS = [
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
]
METRIC_FIELDS = [
    "starting_nav",
    "ending_nav",
    "trading_days",
    "filled_trade_count",
    "total_trade_notional",
    "total_commission",
    "total_sell_tax",
    "total_slippage_cost",
    "turnover_ratio",
    "total_return",
    "max_drawdown",
]
MONEY_METRICS = {
    "starting_nav",
    "ending_nav",
    "total_trade_notional",
    "total_commission",
    "total_sell_tax",
    "total_slippage_cost",
}
COUNT_METRICS = {"trading_days", "filled_trade_count"}


class EngineValidationError(ValueError):
    """Raised when a cross-engine artifact is malformed or unsupported."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        handle = path.open("rb")
    except FileNotFoundError as exc:
        raise EngineValidationError(f"file not found: {path}") from exc
    with handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json_object(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EngineValidationError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EngineValidationError(
            f"{path} is invalid JSON at line {exc.lineno} column {exc.colno}"
        ) from exc
    if not isinstance(payload, dict):
        raise EngineValidationError(f"{path} must contain a JSON object")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
    fieldnames: Sequence[str],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)


def _finite_float(value: Any, location: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise EngineValidationError(f"{location} must be a number") from exc
    if not math.isfinite(parsed):
        raise EngineValidationError(f"{location} must be finite")
    return parsed


def _non_negative_int(value: Any, location: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise EngineValidationError(f"{location} must be an integer") from exc
    if str(parsed) != str(value).strip() and not (
        isinstance(value, int) and not isinstance(value, bool)
    ):
        raise EngineValidationError(f"{location} must be an integer")
    if parsed < 0:
        raise EngineValidationError(f"{location} must be >= 0")
    return parsed


def _parse_positions(value: Any, location: str) -> Dict[str, int]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise EngineValidationError(
                f"{location} must be a JSON object"
            ) from exc
    if not isinstance(value, dict):
        raise EngineValidationError(f"{location} must be a JSON object")
    positions: Dict[str, int] = {}
    for raw_symbol, quantity in value.items():
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            raise EngineValidationError(
                f"{location} contains an invalid symbol"
            )
        symbol = raw_symbol.strip().upper()
        if symbol in positions:
            raise EngineValidationError(
                f"{location} contains duplicate symbol {symbol}"
            )
        positions[symbol] = _non_negative_int(
            quantity, f"{location}.{symbol}"
        )
    return dict(sorted(positions.items()))


def _validated_scope(value: Any, location: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EngineValidationError(f"{location} must be an object")
    scope = dict(value)
    input_kind = scope.get("input")
    if not isinstance(input_kind, str) or not input_kind.strip():
        raise EngineValidationError(
            f"{location}.input must be a non-empty string"
        )
    for field in ("validated_components", "excluded_components"):
        components = scope.get(field)
        if not isinstance(components, list) or not components or not all(
            isinstance(item, str) and item.strip() for item in components
        ):
            raise EngineValidationError(
                f"{location}.{field} must contain non-empty strings"
            )
    return scope


def read_nav_rows(path: Path, *, candidate: bool = False) -> List[Dict[str, Any]]:
    try:
        handle = path.open(newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise EngineValidationError(f"file not found: {path}") from exc
    rows: List[Dict[str, Any]] = []
    seen_dates = set()
    with handle:
        reader = csv.DictReader(handle)
        actual = set(reader.fieldnames or [])
        required = set(NAV_FIELDS)
        if candidate and actual != required:
            raise EngineValidationError(
                f"{path} must have exactly these columns: "
                + ", ".join(sorted(required))
            )
        if not candidate and not required.issubset(actual):
            raise EngineValidationError(
                f"{path} is missing columns: "
                + ", ".join(sorted(required - actual))
            )
        for line_number, row in enumerate(reader, start=2):
            trading_date = (row.get("date") or "").strip()
            try:
                datetime.strptime(trading_date, "%Y-%m-%d")
            except ValueError as exc:
                raise EngineValidationError(
                    f"{path}:{line_number}: date must be YYYY-MM-DD"
                ) from exc
            if trading_date in seen_dates:
                raise EngineValidationError(
                    f"{path}:{line_number}: duplicate date {trading_date}"
                )
            seen_dates.add(trading_date)
            rows.append(
                {
                    "date": trading_date,
                    "nav": _finite_float(
                        row.get("nav"), f"{path}:{line_number}:nav"
                    ),
                    "cash": _finite_float(
                        row.get("cash"), f"{path}:{line_number}:cash"
                    ),
                    "positions": _parse_positions(
                        row.get("positions"),
                        f"{path}:{line_number}:positions",
                    ),
                }
            )
    if not rows:
        raise EngineValidationError(f"{path} contains no NAV rows")
    return rows


def read_trade_rows(
    path: Path, *, candidate: bool = False
) -> List[Dict[str, Any]]:
    try:
        handle = path.open(newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise EngineValidationError(f"file not found: {path}") from exc
    rows: List[Dict[str, Any]] = []
    with handle:
        reader = csv.DictReader(handle)
        actual = set(reader.fieldnames or [])
        required = set(TRADE_FIELDS)
        if candidate and actual != required:
            raise EngineValidationError(
                f"{path} must have exactly these columns: "
                + ", ".join(sorted(required))
            )
        if not candidate and not required.issubset(actual):
            raise EngineValidationError(
                f"{path} is missing columns: "
                + ", ".join(sorted(required - actual))
            )
        for line_number, row in enumerate(reader, start=2):
            signal_date = (row.get("signal_date") or "").strip()
            execution_date = (row.get("execution_date") or "").strip()
            for label, value in (
                ("signal_date", signal_date),
                ("execution_date", execution_date),
            ):
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError as exc:
                    raise EngineValidationError(
                        f"{path}:{line_number}:{label} must be YYYY-MM-DD"
                    ) from exc
            symbol = (row.get("symbol") or "").strip().upper()
            side = (row.get("side") or "").strip().upper()
            status = (row.get("status") or "").strip()
            if not symbol:
                raise EngineValidationError(
                    f"{path}:{line_number}: symbol must not be empty"
                )
            if side not in {"BUY", "SELL"}:
                raise EngineValidationError(
                    f"{path}:{line_number}: side must be BUY or SELL"
                )
            if not status:
                raise EngineValidationError(
                    f"{path}:{line_number}: status must not be empty"
                )
            parsed: Dict[str, Any] = {
                "signal_date": signal_date,
                "execution_date": execution_date,
                "symbol": symbol,
                "side": side,
                "requested_quantity": _non_negative_int(
                    row.get("requested_quantity"),
                    f"{path}:{line_number}:requested_quantity",
                ),
                "filled_quantity": _non_negative_int(
                    row.get("filled_quantity"),
                    f"{path}:{line_number}:filled_quantity",
                ),
                "status": status,
            }
            for field in (
                "signal_price",
                "market_price",
                "fill_price",
                "gross_notional",
                "commission",
                "tax",
                "slippage_cost",
            ):
                parsed[field] = _finite_float(
                    row.get(field), f"{path}:{line_number}:{field}"
                )
            if parsed["requested_quantity"] <= 0:
                raise EngineValidationError(
                    f"{path}:{line_number}:requested_quantity must be > 0"
                )
            if parsed["filled_quantity"] > parsed["requested_quantity"]:
                raise EngineValidationError(
                    f"{path}:{line_number}:filled_quantity exceeds requested_quantity"
                )
            for field in ("signal_price", "market_price", "fill_price"):
                if parsed[field] <= 0:
                    raise EngineValidationError(
                        f"{path}:{line_number}:{field} must be > 0"
                    )
            for field in (
                "gross_notional",
                "commission",
                "tax",
                "slippage_cost",
            ):
                if parsed[field] < 0:
                    raise EngineValidationError(
                        f"{path}:{line_number}:{field} must be >= 0"
                    )
            rows.append(parsed)
    return rows


def reference_identity(reference_directory: Path) -> Dict[str, Any]:
    reference_directory = reference_directory.resolve()
    manifest_path = reference_directory / "manifest.json"
    manifest = _load_json_object(manifest_path)
    if manifest.get("artifact_type") != "backtest":
        raise EngineValidationError(
            f"{manifest_path} must describe a backtest artifact"
        )
    declared_files = manifest.get("files")
    declared_hashes = manifest.get("file_sha256")
    if not isinstance(declared_files, list) or not all(
        isinstance(name, str)
        and name
        and Path(name).name == name
        and name not in {".", ".."}
        for name in declared_files
    ):
        raise EngineValidationError(
            f"{manifest_path} files must contain safe file names"
        )
    if len(set(declared_files)) != len(declared_files):
        raise EngineValidationError(f"{manifest_path} files contain duplicates")
    if "manifest.json" not in declared_files:
        raise EngineValidationError(
            f"{manifest_path} files must include manifest.json"
        )
    if not isinstance(declared_hashes, dict):
        raise EngineValidationError(
            "reference run has no anchored file_sha256 map; rerun the "
            "backtest with lets-quant v0.7.0+"
        )
    expected_hash_names = set(declared_files) - {"manifest.json"}
    if set(declared_hashes) != expected_hash_names:
        raise EngineValidationError(
            f"{manifest_path} file_sha256 keys do not match files"
        )
    for name in sorted(expected_hash_names):
        expected_hash = declared_hashes.get(name)
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise EngineValidationError(
                f"{manifest_path} has an invalid hash for {name}"
            )
        actual_hash = file_sha256(reference_directory / name)
        if actual_hash != expected_hash:
            raise EngineValidationError(
                f"reference artifact integrity failed for {name}: expected "
                f"{expected_hash}, got {actual_hash}"
            )
    required = [
        "policy.snapshot.json",
        "metrics.json",
        "nav.csv",
        "signals.csv",
        "trades.csv",
    ]
    missing_required = sorted(set(required) - expected_hash_names)
    if missing_required:
        raise EngineValidationError(
            f"{manifest_path} is missing required artifacts: "
            + ", ".join(missing_required)
        )
    file_hashes = {name: declared_hashes[name] for name in required}
    policy_input_hash = manifest.get("policy_sha256")
    prices_input_hash = manifest.get("prices_sha256")
    if not isinstance(policy_input_hash, str) or len(policy_input_hash) != 64:
        raise EngineValidationError(
            f"{manifest_path} has an invalid policy_sha256"
        )
    if not isinstance(prices_input_hash, str) or len(prices_input_hash) != 64:
        raise EngineValidationError(
            f"{manifest_path} has an invalid prices_sha256"
        )
    return {
        "run_id": reference_directory.name,
        "manifest_sha256": file_sha256(manifest_path),
        "policy_input_sha256": policy_input_hash,
        "prices_input_sha256": prices_input_hash,
        "policy_snapshot_sha256": file_hashes["policy.snapshot.json"],
        "metrics_sha256": file_hashes["metrics.json"],
        "nav_sha256": file_hashes["nav.csv"],
        "signals_sha256": file_hashes["signals.csv"],
        "trades_sha256": file_hashes["trades.csv"],
        "source_revision": manifest.get("source_revision"),
        "reference_file_hashes_verified": True,
    }


def summarize_candidate(
    nav_rows: Sequence[Mapping[str, Any]],
    trade_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    if not nav_rows:
        raise EngineValidationError("candidate must contain at least one NAV row")
    nav_values = [
        _finite_float(row["nav"], f"candidate NAV row {index}")
        for index, row in enumerate(nav_rows)
    ]
    if any(value <= 0 for value in nav_values):
        raise EngineValidationError("candidate NAV values must be > 0")
    peak = nav_values[0]
    max_drawdown = 0.0
    for value in nav_values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1 if peak else 0.0)
    total_notional = sum(float(row["gross_notional"]) for row in trade_rows)
    mean_nav = statistics.mean(nav_values)
    return {
        "starting_nav": nav_values[0],
        "ending_nav": nav_values[-1],
        "trading_days": len(nav_rows),
        "filled_trade_count": sum(
            1 for row in trade_rows if int(row["filled_quantity"]) > 0
        ),
        "total_trade_notional": total_notional,
        "total_commission": sum(
            float(row["commission"]) for row in trade_rows
        ),
        "total_sell_tax": sum(float(row["tax"]) for row in trade_rows),
        "total_slippage_cost": sum(
            float(row["slippage_cost"]) for row in trade_rows
        ),
        "turnover_ratio": total_notional / mean_nav if mean_nav > 0 else 0.0,
        "total_return": nav_values[-1] / nav_values[0] - 1,
        "max_drawdown": max_drawdown,
    }


def write_engine_candidate(
    *,
    reference_directory: Path,
    output_root: Path,
    engine: Mapping[str, Any],
    nav_rows: Sequence[Mapping[str, Any]],
    trade_rows: Sequence[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    validation_scope: Mapping[str, Any],
    limitations: Sequence[str],
) -> Path:
    engine_name = engine.get("name")
    engine_version = engine.get("version")
    adapter_version = engine.get("adapter_version")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (engine_name, engine_version, adapter_version)
    ):
        raise EngineValidationError(
            "engine name, version, and adapter_version must be non-empty strings"
        )
    if not limitations or not all(
        isinstance(item, str) and item.strip() for item in limitations
    ):
        raise EngineValidationError(
            "limitations must contain only non-empty strings"
        )
    normalized_scope = _validated_scope(
        validation_scope, "validation_scope"
    )
    missing_metrics = sorted(set(METRIC_FIELDS) - set(metrics))
    if missing_metrics:
        raise EngineValidationError(
            "candidate metrics are missing: " + ", ".join(missing_metrics)
        )
    reference = reference_identity(reference_directory)
    fingerprint = hashlib.sha256(
        (
            reference["manifest_sha256"]
            + str(engine_name)
            + str(engine_version)
            + str(adapter_version)
        ).encode("utf-8")
    ).hexdigest()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = output_root / f"{timestamp}-{fingerprint[:8]}"
    destination.mkdir(parents=True, exist_ok=False)

    _write_csv(
        destination / "nav.csv",
        (
            {
                "date": row["date"],
                "nav": f"{float(row['nav']):.8f}",
                "cash": f"{float(row['cash']):.8f}",
                "positions": json.dumps(
                    _parse_positions(row["positions"], "candidate positions"),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            for row in nav_rows
        ),
        NAV_FIELDS,
    )
    _write_csv(
        destination / "trades.csv",
        (
            {
                **{field: row[field] for field in TRADE_FIELDS},
                **{
                    field: f"{float(row[field]):.8f}"
                    for field in (
                        "signal_price",
                        "market_price",
                        "fill_price",
                        "gross_notional",
                        "commission",
                        "tax",
                        "slippage_cost",
                    )
                },
            }
            for row in trade_rows
        ),
        TRADE_FIELDS,
    )
    _write_json(
        destination / "metrics.json",
        {field: metrics[field] for field in METRIC_FIELDS},
    )
    files = {
        name: file_sha256(destination / name)
        for name in ("metrics.json", "nav.csv", "trades.csv")
    }
    manifest: Dict[str, Any] = {
        "artifact_type": "engine_candidate",
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "engine": dict(engine),
        "reference": reference,
        "files": files,
        "validation_scope": normalized_scope,
        "limitations": list(limitations),
        "investment_validity_established": False,
        "automatic_execution_allowed": False,
    }
    manifest["candidate_id"] = _canonical_sha256(
        {
            "engine": manifest["engine"],
            "reference": reference,
            "files": files,
            "validation_scope": manifest["validation_scope"],
            "limitations": manifest["limitations"],
        }
    )
    _write_json(destination / "manifest.json", manifest)
    return destination


def _check(name: str, passed: bool, details: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "name": name,
        "status": "pass" if passed else "blocked",
        "details": dict(details),
    }


def _differences_limited(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return items[:20]


def reconcile_engine_candidate(
    reference_directory: Path,
    candidate_directory: Path,
    *,
    money_tolerance: float = 1e-6,
    ratio_tolerance: float = 1e-10,
) -> Dict[str, Any]:
    if not math.isfinite(money_tolerance) or money_tolerance < 0:
        raise EngineValidationError(
            "money_tolerance must be finite and >= 0"
        )
    if not math.isfinite(ratio_tolerance) or ratio_tolerance < 0:
        raise EngineValidationError(
            "ratio_tolerance must be finite and >= 0"
        )
    reference_directory = reference_directory.resolve()
    candidate_directory = candidate_directory.resolve()
    current_reference = reference_identity(reference_directory)
    manifest_path = candidate_directory / "manifest.json"
    candidate_manifest = _load_json_object(manifest_path)
    if candidate_manifest.get("artifact_type") != "engine_candidate":
        raise EngineValidationError(
            f"{manifest_path} must describe an engine_candidate artifact"
        )
    if candidate_manifest.get("schema_version") != SCHEMA_VERSION:
        raise EngineValidationError(
            f"{manifest_path} schema_version must be {SCHEMA_VERSION}"
        )
    expected_manifest_fields = {
        "artifact_type",
        "schema_version",
        "created_at",
        "engine",
        "reference",
        "files",
        "validation_scope",
        "limitations",
        "investment_validity_established",
        "automatic_execution_allowed",
        "candidate_id",
    }
    actual_manifest_fields = set(candidate_manifest)
    if actual_manifest_fields != expected_manifest_fields:
        raise EngineValidationError(
            f"{manifest_path} must have exactly these fields: "
            + ", ".join(sorted(expected_manifest_fields))
        )
    created_at = candidate_manifest.get("created_at")
    if not isinstance(created_at, str):
        raise EngineValidationError(
            f"{manifest_path} created_at must be an ISO-8601 timestamp"
        )
    try:
        parsed_created_at = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise EngineValidationError(
            f"{manifest_path} created_at must be an ISO-8601 timestamp"
        ) from exc
    if parsed_created_at.tzinfo is None:
        raise EngineValidationError(
            f"{manifest_path} created_at must include a timezone"
        )
    if candidate_manifest.get("investment_validity_established") is not False:
        raise EngineValidationError(
            f"{manifest_path} cannot establish investment validity"
        )
    if candidate_manifest.get("automatic_execution_allowed") is not False:
        raise EngineValidationError(
            f"{manifest_path} cannot allow automatic execution"
        )
    engine = candidate_manifest.get("engine")
    bound_reference = candidate_manifest.get("reference")
    declared_files = candidate_manifest.get("files")
    validation_scope = _validated_scope(
        candidate_manifest.get("validation_scope"),
        f"{manifest_path} validation_scope",
    )
    limitations = candidate_manifest.get("limitations")
    if not isinstance(engine, dict):
        raise EngineValidationError(f"{manifest_path} engine must be an object")
    for field in ("name", "version", "adapter_version"):
        value = engine.get(field)
        if not isinstance(value, str) or not value.strip():
            raise EngineValidationError(
                f"{manifest_path} engine.{field} must be a non-empty string"
            )
    if not isinstance(bound_reference, dict):
        raise EngineValidationError(
            f"{manifest_path} reference must be an object"
        )
    if not isinstance(declared_files, dict):
        raise EngineValidationError(f"{manifest_path} files must be an object")
    if not isinstance(limitations, list) or not limitations or not all(
        isinstance(item, str) and item.strip() for item in limitations
    ):
        raise EngineValidationError(
            f"{manifest_path} limitations must be non-empty strings"
        )

    checks: List[Dict[str, Any]] = []
    binding_fields = set(current_reference) | set(bound_reference)
    binding_mismatches = [
        {
            "field": field,
            "expected": current_reference.get(field),
            "actual": bound_reference.get(field),
        }
        for field in sorted(binding_fields)
        if bound_reference.get(field) != current_reference.get(field)
    ]
    checks.append(
        _check(
            "reference_binding",
            not binding_mismatches,
            {"mismatches": _differences_limited(binding_mismatches)},
        )
    )

    expected_candidate_files = {"metrics.json", "nav.csv", "trades.csv"}
    file_mismatches: List[Dict[str, Any]] = []
    if set(declared_files) != expected_candidate_files:
        file_mismatches.append(
            {
                "field": "file_set",
                "expected": sorted(expected_candidate_files),
                "actual": sorted(str(name) for name in declared_files),
            }
        )
    for name in sorted(expected_candidate_files & set(declared_files)):
        actual_hash = file_sha256(candidate_directory / name)
        if declared_files.get(name) != actual_hash:
            file_mismatches.append(
                {
                    "field": name,
                    "expected": declared_files.get(name),
                    "actual": actual_hash,
                }
            )
    expected_candidate_id = _canonical_sha256(
        {
            "engine": engine,
            "reference": bound_reference,
            "files": declared_files,
            "validation_scope": validation_scope,
            "limitations": candidate_manifest.get("limitations"),
        }
    )
    if candidate_manifest.get("candidate_id") != expected_candidate_id:
        file_mismatches.append(
            {
                "field": "candidate_id",
                "expected": expected_candidate_id,
                "actual": candidate_manifest.get("candidate_id"),
            }
        )
    checks.append(
        _check(
            "candidate_file_integrity",
            not file_mismatches,
            {"mismatches": _differences_limited(file_mismatches)},
        )
    )

    reference_nav = read_nav_rows(reference_directory / "nav.csv")
    candidate_nav = read_nav_rows(
        candidate_directory / "nav.csv", candidate=True
    )
    reference_dates = [row["date"] for row in reference_nav]
    candidate_dates = [row["date"] for row in candidate_nav]
    checks.append(
        _check(
            "date_axis",
            reference_dates == candidate_dates,
            {
                "reference_rows": len(reference_dates),
                "candidate_rows": len(candidate_dates),
                "first_reference_only": next(
                    (date for date in reference_dates if date not in candidate_dates),
                    None,
                ),
                "first_candidate_only": next(
                    (date for date in candidate_dates if date not in reference_dates),
                    None,
                ),
            },
        )
    )
    candidate_nav_by_date = {row["date"]: row for row in candidate_nav}
    nav_differences: List[Dict[str, Any]] = []
    position_differences: List[Dict[str, Any]] = []
    max_nav_difference = 0.0
    max_cash_difference = 0.0
    for reference_row in reference_nav:
        candidate_row = candidate_nav_by_date.get(reference_row["date"])
        if candidate_row is None:
            continue
        nav_difference = abs(reference_row["nav"] - candidate_row["nav"])
        cash_difference = abs(reference_row["cash"] - candidate_row["cash"])
        max_nav_difference = max(max_nav_difference, nav_difference)
        max_cash_difference = max(max_cash_difference, cash_difference)
        if nav_difference > money_tolerance or cash_difference > money_tolerance:
            nav_differences.append(
                {
                    "date": reference_row["date"],
                    "nav_difference": nav_difference,
                    "cash_difference": cash_difference,
                }
            )
        if reference_row["positions"] != candidate_row["positions"]:
            position_differences.append(
                {
                    "date": reference_row["date"],
                    "expected": reference_row["positions"],
                    "actual": candidate_row["positions"],
                }
            )
    checks.append(
        _check(
            "nav_and_cash",
            not nav_differences and reference_dates == candidate_dates,
            {
                "max_abs_nav_difference": max_nav_difference,
                "max_abs_cash_difference": max_cash_difference,
                "mismatches": _differences_limited(nav_differences),
            },
        )
    )
    checks.append(
        _check(
            "positions",
            not position_differences and reference_dates == candidate_dates,
            {"mismatches": _differences_limited(position_differences)},
        )
    )

    reference_trades = read_trade_rows(reference_directory / "trades.csv")
    candidate_trades = read_trade_rows(
        candidate_directory / "trades.csv", candidate=True
    )
    trade_differences: List[Dict[str, Any]] = []
    exact_fields = (
        "signal_date",
        "execution_date",
        "symbol",
        "side",
        "requested_quantity",
        "filled_quantity",
        "status",
    )
    price_fields = ("signal_price", "market_price", "fill_price")
    money_fields = (
        "gross_notional",
        "commission",
        "tax",
        "slippage_cost",
    )
    if len(reference_trades) != len(candidate_trades):
        trade_differences.append(
            {
                "field": "trade_count",
                "expected": len(reference_trades),
                "actual": len(candidate_trades),
            }
        )
    for index, (reference_trade, candidate_trade) in enumerate(
        zip(reference_trades, candidate_trades)
    ):
        for field in exact_fields:
            if reference_trade[field] != candidate_trade[field]:
                trade_differences.append(
                    {
                        "trade_index": index,
                        "field": field,
                        "expected": reference_trade[field],
                        "actual": candidate_trade[field],
                    }
                )
        for field in price_fields:
            difference = abs(reference_trade[field] - candidate_trade[field])
            if difference > 1e-8:
                trade_differences.append(
                    {
                        "trade_index": index,
                        "field": field,
                        "difference": difference,
                    }
                )
        for field in money_fields:
            difference = abs(reference_trade[field] - candidate_trade[field])
            if difference > money_tolerance:
                trade_differences.append(
                    {
                        "trade_index": index,
                        "field": field,
                        "difference": difference,
                    }
                )
    checks.append(
        _check(
            "trades_and_costs",
            not trade_differences,
            {
                "reference_trade_count": len(reference_trades),
                "candidate_trade_count": len(candidate_trades),
                "mismatches": _differences_limited(trade_differences),
            },
        )
    )

    reference_metrics = _load_json_object(reference_directory / "metrics.json")
    candidate_metrics = _load_json_object(candidate_directory / "metrics.json")
    if set(candidate_metrics) != set(METRIC_FIELDS):
        raise EngineValidationError(
            f"{candidate_directory / 'metrics.json'} must have exactly these "
            "fields: " + ", ".join(sorted(METRIC_FIELDS))
        )
    metric_differences: List[Dict[str, Any]] = []
    for field in METRIC_FIELDS:
        if field not in reference_metrics or field not in candidate_metrics:
            metric_differences.append(
                {
                    "field": field,
                    "expected": reference_metrics.get(field),
                    "actual": candidate_metrics.get(field),
                }
            )
            continue
        if field in COUNT_METRICS:
            try:
                expected_count = _non_negative_int(
                    reference_metrics[field], f"reference metric {field}"
                )
                actual_count = _non_negative_int(
                    candidate_metrics[field], f"candidate metric {field}"
                )
            except EngineValidationError:
                raise
            if expected_count != actual_count:
                metric_differences.append(
                    {
                        "field": field,
                        "expected": expected_count,
                        "actual": actual_count,
                    }
                )
            continue
        expected_value = _finite_float(
            reference_metrics[field], f"reference metric {field}"
        )
        actual_value = _finite_float(
            candidate_metrics[field], f"candidate metric {field}"
        )
        tolerance = money_tolerance if field in MONEY_METRICS else ratio_tolerance
        difference = abs(expected_value - actual_value)
        if difference > tolerance:
            metric_differences.append(
                {
                    "field": field,
                    "expected": expected_value,
                    "actual": actual_value,
                    "difference": difference,
                    "tolerance": tolerance,
                }
            )
    checks.append(
        _check(
            "summary_metrics",
            not metric_differences,
            {"mismatches": _differences_limited(metric_differences)},
        )
    )

    status = (
        "pass"
        if all(check["status"] == "pass" for check in checks)
        else "blocked"
    )
    report: Dict[str, Any] = {
        "artifact_type": "engine_reconciliation",
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "engine": engine,
        "reference": current_reference,
        "candidate": {
            "candidate_id": candidate_manifest.get("candidate_id"),
            "manifest_sha256": file_sha256(manifest_path),
        },
        "tolerances": {
            "money_absolute": money_tolerance,
            "price_absolute": 1e-8,
            "ratio_absolute": ratio_tolerance,
            "quantities": "exact",
        },
        "validation_scope": validation_scope,
        "limitations": candidate_manifest.get("limitations"),
        "checks": checks,
        "summary": {
            "reference_nav_rows": len(reference_nav),
            "candidate_nav_rows": len(candidate_nav),
            "reference_trade_rows": len(reference_trades),
            "candidate_trade_rows": len(candidate_trades),
            "max_abs_nav_difference": max_nav_difference,
            "max_abs_cash_difference": max_cash_difference,
            "blocked_check_count": sum(
                1 for check in checks if check["status"] == "blocked"
            ),
        },
        "investment_validity_established": False,
        "automatic_execution_allowed": False,
    }
    report["report_sha256"] = _canonical_sha256(report)
    return report


def write_reconciliation_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, report)
