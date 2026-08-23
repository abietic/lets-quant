from __future__ import annotations

import csv
import hashlib
import json
import platform
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from .experiments import ExperimentResult
from .models import (
    BacktestResult,
    Holding,
    InstrumentMetadata,
    ManualOrderPlan,
    Policy,
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision(path: Path) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if completed.returncode != 0:
        return None
    revision = completed.stdout.strip()
    return revision or None


def _find_project_root(path: Path) -> Path:
    current = path.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return current


def _source_tree_sha256(project_root: Path) -> str:
    candidates = [project_root / "pyproject.toml"]
    source_root = project_root / "src"
    if source_root.is_dir():
        candidates.extend(sorted(source_root.rglob("*.py")))

    digest = hashlib.sha256()
    for path in sorted(
        (item for item in candidates if item.is_file()),
        key=lambda item: str(item.relative_to(project_root)),
    ):
        relative_path = str(path.relative_to(project_root))
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _run_directory(root: Path, fingerprint: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = root / f"{timestamp}-{fingerprint[:8]}"
    destination.mkdir(parents=True, exist_ok=False)
    return destination


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(
    path: Path, rows: Iterable[Mapping[str, Any]], fieldnames: Iterable[str]
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)


def _write_backtest_result_files(
    destination: Path, result: BacktestResult
) -> None:
    _write_json(destination / "metrics.json", result.metrics)
    _write_csv(
        destination / "nav.csv",
        (
            {
                "date": record.trading_date.isoformat(),
                "nav": f"{record.nav:.8f}",
                "cash": f"{record.cash:.8f}",
                "drawdown": f"{record.drawdown:.10f}",
                "risk_frozen": str(record.risk_frozen).lower(),
                "positions": json.dumps(
                    record.positions, sort_keys=True, separators=(",", ":")
                ),
            }
            for record in result.nav
        ),
        ["date", "nav", "cash", "drawdown", "risk_frozen", "positions"],
    )
    _write_csv(
        destination / "signals.csv",
        (
            {
                "signal_date": signal.signal_date.isoformat(),
                "execution_date": signal.execution_date.isoformat(),
                "status": signal.status,
                "estimated_turnover": f"{signal.estimated_turnover:.10f}",
                "reason": signal.reason,
                "decision_id": signal.decision_id or "",
                "strategy_kind": signal.strategy_kind or "",
                "target_weights": json.dumps(
                    signal.target_weights,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "decision_evidence": json.dumps(
                    signal.decision_evidence,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "diagnostics": json.dumps(
                    signal.diagnostics,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "orders": json.dumps(
                    [
                        {
                            **asdict(order),
                            "signal_date": order.signal_date.isoformat(),
                            "execution_date": order.execution_date.isoformat(),
                        }
                        for order in signal.orders
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            for signal in result.signals
        ),
        [
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
    )
    _write_csv(
        destination / "trades.csv",
        (
            {
                "signal_date": trade.signal_date.isoformat(),
                "execution_date": trade.execution_date.isoformat(),
                "symbol": trade.symbol,
                "side": trade.side,
                "requested_quantity": trade.requested_quantity,
                "filled_quantity": trade.filled_quantity,
                "signal_price": f"{trade.signal_price:.8f}",
                "market_price": f"{trade.market_price:.8f}",
                "fill_price": f"{trade.fill_price:.8f}",
                "gross_notional": f"{trade.gross_notional:.8f}",
                "commission": f"{trade.commission:.8f}",
                "tax": f"{trade.tax:.8f}",
                "slippage_cost": f"{trade.slippage_cost:.8f}",
                "status": trade.status,
            }
            for trade in result.trades
        ),
        [
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
    )
    _write_csv(
        destination / "ledger.csv",
        (
            {
                "entry_id": entry.entry_id,
                "sequence": entry.sequence,
                "date": entry.trading_date.isoformat(),
                "event_type": entry.event_type,
                "symbol": entry.symbol or "",
                "quantity_delta": entry.quantity_delta,
                "cash_delta": f"{entry.cash_delta:.8f}",
                "expense": f"{entry.expense:.8f}",
                "reference_id": entry.reference_id,
                "description": entry.description,
                "metadata": json.dumps(
                    entry.metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            for entry in result.ledger
        ),
        [
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
    )
    _write_csv(
        destination / "accounting.csv",
        (
            {
                "date": record.trading_date.isoformat(),
                "status": record.status,
                "ledger_entry_count": record.ledger_entry_count,
                "cash": f"{record.cash:.8f}",
                "expected_cash": f"{record.expected_cash:.8f}",
                "market_value": f"{record.market_value:.8f}",
                "expected_market_value": (
                    f"{record.expected_market_value:.8f}"
                ),
                "nav": f"{record.nav:.8f}",
                "expected_nav": f"{record.expected_nav:.8f}",
                "cash_error": f"{record.cash_error:.12f}",
                "nav_error": f"{record.nav_error:.12f}",
                "positions": json.dumps(
                    record.positions,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "expected_positions": json.dumps(
                    record.expected_positions,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "position_errors": json.dumps(
                    record.position_errors,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            for record in result.accounting
        ),
        [
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
    )


def write_backtest_artifacts(
    result: BacktestResult,
    policy: Policy,
    policy_path: Path,
    prices_path: Path,
    output_root: Path,
    initial_holdings: Sequence[Holding] = (),
    initial_holdings_path: Optional[Path] = None,
    dataset_manifest: Optional[Mapping[str, Any]] = None,
    instrument_master: Sequence[InstrumentMetadata] = (),
    instrument_master_source: str = "",
) -> Path:
    policy_hash = _file_sha256(policy_path)
    prices_hash = _file_sha256(prices_path)
    if not instrument_master_source.strip():
        raise ValueError("instrument_master_source must not be empty")
    normalized_instruments = [
        {
            "symbol": instrument.symbol.strip().upper(),
            "exchange": instrument.exchange.strip().upper(),
            "asset_type": instrument.asset_type.strip().upper(),
            "listed_on": instrument.listed_on.isoformat(),
            "delisted_on": (
                instrument.delisted_on.isoformat()
                if instrument.delisted_on is not None
                else ""
            ),
            "available_at": (
                instrument.available_at.isoformat()
                if instrument.available_at is not None
                else ""
            ),
        }
        for instrument in sorted(
            instrument_master, key=lambda item: item.symbol.strip().upper()
        )
    ]
    instrument_symbols = [item["symbol"] for item in normalized_instruments]
    if len(set(instrument_symbols)) != len(instrument_symbols):
        raise ValueError("instrument master symbols must be unique")
    required_instruments = set(policy.strategy.target_weights)
    if policy.portfolio.benchmark:
        required_instruments.add(policy.portfolio.benchmark)
    missing_instruments = sorted(
        required_instruments - set(instrument_symbols)
    )
    if missing_instruments:
        raise ValueError(
            "instrument master is missing policy symbols: "
            + ", ".join(missing_instruments)
        )
    instrument_identity = hashlib.sha256(
        json.dumps(
            normalized_instruments,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    normalized_holdings = [
        {
            "symbol": holding.symbol.strip().upper(),
            "quantity": holding.quantity,
        }
        for holding in sorted(
            (holding for holding in initial_holdings if holding.quantity > 0),
            key=lambda item: item.symbol.strip().upper(),
        )
    ]
    holdings_identity = hashlib.sha256(
        json.dumps(
            normalized_holdings,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    holdings_input_hash = (
        _file_sha256(initial_holdings_path)
        if initial_holdings_path is not None
        else None
    )
    holdings_fingerprint = holdings_input_hash or holdings_identity
    project_root = _find_project_root(policy_path)
    destination = _run_directory(
        output_root,
        hashlib.sha256(
            (
                policy_hash
                + prices_hash
                + holdings_fingerprint
                + instrument_identity
            ).encode()
        ).hexdigest(),
    )

    _write_backtest_result_files(destination, result)
    _write_json(destination / "policy.snapshot.json", policy.to_dict())
    _write_csv(
        destination / "initial_holdings.csv",
        normalized_holdings,
        ["symbol", "quantity"],
    )
    _write_csv(
        destination / "instrument_master.csv",
        normalized_instruments,
        [
            "symbol",
            "exchange",
            "asset_type",
            "listed_on",
            "delisted_on",
            "available_at",
        ],
    )
    if dataset_manifest is not None:
        _write_json(
            destination / "dataset.snapshot.json", dict(dataset_manifest)
        )

    files = [
        "manifest.json",
        "accounting.csv",
        "initial_holdings.csv",
        "instrument_master.csv",
        "ledger.csv",
        "metrics.json",
        "nav.csv",
        "policy.snapshot.json",
        "signals.csv",
        "trades.csv",
    ]
    if dataset_manifest is not None:
        files.append("dataset.snapshot.json")
    file_sha256 = {
        name: _file_sha256(destination / name)
        for name in files
        if name != "manifest.json"
    }

    manifest: Dict[str, Any] = {
        "artifact_type": "backtest",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy_name": policy.name,
        "policy_path": str(policy_path.resolve()),
        "policy_sha256": policy_hash,
        "prices_path": str(prices_path.resolve()),
        "prices_sha256": prices_hash,
        "initial_holdings_path": (
            str(initial_holdings_path.resolve())
            if initial_holdings_path is not None
            else None
        ),
        "initial_holdings_input_sha256": holdings_input_hash,
        "initial_holdings_snapshot_sha256": file_sha256[
            "initial_holdings.csv"
        ],
        "instrument_master_source": instrument_master_source,
        "instrument_master_snapshot_sha256": file_sha256[
            "instrument_master.csv"
        ],
        "project_root": str(project_root),
        "source_revision": _git_revision(project_root),
        "source_tree_sha256": _source_tree_sha256(project_root),
        "python_version": platform.python_version(),
        "assumptions": result.assumptions,
        "data_source": (
            {
                "type": "curated_dataset",
                "dataset_id": dataset_manifest.get("dataset_id"),
                "as_of": dataset_manifest.get("as_of"),
                "quality_status": dataset_manifest.get("quality_status"),
                "instrument_master_sha256": dataset_manifest.get(
                    "files", {}
                ).get("instruments.csv"),
                "source_snapshot_id": (
                    dataset_manifest.get("source_snapshot", {}).get(
                        "snapshot_id"
                    )
                    if isinstance(
                        dataset_manifest.get("source_snapshot"), dict
                    )
                    else None
                ),
            }
            if dataset_manifest is not None
            else {"type": "standalone_prices_csv"}
        ),
        "files": sorted(files),
        "file_sha256": file_sha256,
    }
    _write_json(destination / "manifest.json", manifest)
    return destination


def _safe_component(value: str) -> str:
    sanitized = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in value
    ).strip("-")
    return sanitized or "case"


def write_experiment_artifacts(
    result: ExperimentResult,
    policy: Policy,
    policy_path: Path,
    experiment_path: Path,
    output_root: Path,
    market_source: Mapping[str, Any],
    market_source_path: Optional[Path] = None,
    market_snapshot: Optional[Mapping[str, Any]] = None,
    dataset_manifest: Optional[Mapping[str, Any]] = None,
) -> Path:
    policy_hash = _file_sha256(policy_path)
    experiment_hash = _file_sha256(experiment_path)
    project_root = _find_project_root(policy_path)
    source_tree_hash = _source_tree_sha256(project_root)
    source_hash = (
        _file_sha256(market_source_path)
        if market_source_path is not None
        else hashlib.sha256(
            json.dumps(
                market_snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
    )
    experiment_id = hashlib.sha256(
        (
            result.experiment_input_id
            + source_tree_hash
            + policy_hash
            + experiment_hash
            + source_hash
        ).encode("utf-8")
    ).hexdigest()
    destination = _run_directory(output_root, experiment_id)

    _write_json(destination / "summary.json", result.summary)
    _write_json(destination / "experiment.snapshot.json", result.spec.to_dict())
    _write_json(destination / "policy.snapshot.json", policy.to_dict())
    if dataset_manifest is not None:
        _write_json(destination / "dataset.snapshot.json", dict(dataset_manifest))
    if market_snapshot is not None:
        _write_json(destination / "market.snapshot.json", dict(market_snapshot))

    cases_root = destination / "cases"
    cases_root.mkdir()
    for index, case in enumerate(result.cases, start=1):
        case_directory = cases_root / (
            f"{index:02d}-{_safe_component(case.window.fold)}-"
            f"{_safe_component(case.window.role)}-"
            f"{_safe_component(case.parameter_variant.name)}-"
            f"{_safe_component(case.execution_scenario.name)}-"
            f"{case.case_id[:8]}"
        )
        case_directory.mkdir()
        _write_json(
            case_directory / "case.snapshot.json",
            {
                "case_id": case.case_id,
                "window": case.window.to_dict(),
                "execution_scenario": case.execution_scenario.to_dict(),
                "parameter_variant": case.parameter_variant.to_dict(),
                "experiment_result_sha256": result.result_sha256,
            },
        )
        _write_backtest_result_files(case_directory, case.result)

    files = sorted(
        str(path.relative_to(destination))
        for path in destination.rglob("*")
        if path.is_file()
    )
    files.append("manifest.json")
    source_payload = dict(market_source)
    source_payload["sha256"] = source_hash
    if market_source_path is not None:
        source_payload["path"] = str(market_source_path.resolve())
    _write_json(
        destination / "manifest.json",
        {
            "artifact_type": "research_experiment",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "experiment_id": experiment_id,
            "experiment_input_id": result.experiment_input_id,
            "result_sha256": result.result_sha256,
            "policy_path": str(policy_path.resolve()),
            "policy_sha256": policy_hash,
            "experiment_path": str(experiment_path.resolve()),
            "experiment_sha256": experiment_hash,
            "market_source": source_payload,
            "project_root": str(project_root),
            "source_revision": _git_revision(project_root),
            "source_tree_sha256": source_tree_hash,
            "python_version": platform.python_version(),
            "research_only": True,
            "investment_validity_established": False,
            "files": sorted(files),
        },
    )
    return destination


def write_plan_artifacts(
    plan: ManualOrderPlan,
    policy: Policy,
    policy_path: Path,
    prices_path: Path,
    holdings_path: Path,
    output_root: Path,
    dataset_manifest: Optional[Mapping[str, Any]] = None,
) -> Path:
    policy_hash = _file_sha256(policy_path)
    prices_hash = _file_sha256(prices_path)
    holdings_hash = _file_sha256(holdings_path)
    project_root = _find_project_root(policy_path)
    fingerprint = hashlib.sha256(
        (policy_hash + prices_hash + holdings_hash).encode()
    ).hexdigest()
    destination = _run_directory(output_root, fingerprint)

    _write_json(destination / "plan.json", plan.to_dict())
    _write_json(destination / "policy.snapshot.json", policy.to_dict())
    if dataset_manifest is not None:
        _write_json(
            destination / "dataset.snapshot.json", dict(dataset_manifest)
        )
    _write_csv(
        destination / "orders.csv",
        (
            {
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "reference_price": f"{order.reference_price:.8f}",
                "estimated_fill_price": f"{order.estimated_fill_price:.8f}",
                "estimated_notional": f"{order.estimated_notional:.8f}",
                "estimated_fees": f"{order.estimated_fees:.8f}",
                "current_quantity": order.current_quantity,
                "target_quantity": order.target_quantity,
            }
            for order in plan.recommendations
        ),
        [
            "symbol",
            "side",
            "quantity",
            "reference_price",
            "estimated_fill_price",
            "estimated_notional",
            "estimated_fees",
            "current_quantity",
            "target_quantity",
        ],
    )

    files = [
        "manifest.json",
        "orders.csv",
        "plan.json",
        "policy.snapshot.json",
    ]
    if dataset_manifest is not None:
        files.append("dataset.snapshot.json")
    _write_json(
        destination / "manifest.json",
        {
            "artifact_type": "manual_order_plan",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "policy_name": policy.name,
            "policy_path": str(policy_path.resolve()),
            "policy_sha256": policy_hash,
            "prices_path": str(prices_path.resolve()),
            "prices_sha256": prices_hash,
            "holdings_path": str(holdings_path.resolve()),
            "holdings_sha256": holdings_hash,
            "project_root": str(project_root),
            "source_revision": _git_revision(project_root),
            "source_tree_sha256": _source_tree_sha256(project_root),
            "python_version": platform.python_version(),
            "data_source": (
                {
                    "type": "curated_dataset",
                    "dataset_id": dataset_manifest.get("dataset_id"),
                    "as_of": dataset_manifest.get("as_of"),
                    "quality_status": dataset_manifest.get("quality_status"),
                }
                if dataset_manifest is not None
                else {"type": "standalone_prices_csv"}
            ),
            "execution_boundary": {
                "approval_required": True,
                "automatic_execution_allowed": False,
            },
            "files": sorted(files),
        },
    )
    return destination
