from __future__ import annotations

import hashlib
import json
import math
import platform
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from . import __version__
from .config import load_policy
from .experiment_verification import verify_experiment_artifacts
from .experiments import load_experiment_spec, market_identity, run_experiment
from .models import CorporateAction, MarketData
from .snapshots import file_sha256


class ExperimentReplayError(ValueError):
    """Raised when a verified experiment cannot be reproduced offline."""


_MARKET_KEYS = {
    "corporate_actions",
    "dates",
    "price_adjustment",
    "prices",
    "tradable",
}
_ACTION_KEYS = {
    "announced_at",
    "available_at",
    "cash_amount",
    "event_type",
    "ex_date",
    "ratio",
    "symbol",
}
_ACTION_TYPES = {"cash_dividend", "split", "reverse_split"}


def _load_json_object(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExperimentReplayError(f"file not found: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentReplayError(f"invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ExperimentReplayError(f"{path} must contain a JSON object")
    return payload


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ExperimentReplayError(f"{location} must be a JSON object")
    return value


def _sequence(value: Any, location: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ExperimentReplayError(f"{location} must be a JSON array")
    return value


def _nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentReplayError(f"{location} must be a non-empty string")
    return value


def _finite(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExperimentReplayError(f"{location} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ExperimentReplayError(f"{location} must be a finite number")
    return parsed


def _optional_finite(value: Any, location: str) -> Optional[float]:
    if value is None:
        return None
    return _finite(value, location)


def _date(value: Any, location: str) -> date:
    raw = _nonempty_string(value, location)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ExperimentReplayError(
            f"{location} must be a YYYY-MM-DD date"
        ) from exc


def _optional_timestamp(value: Any, location: str) -> Optional[datetime]:
    if value is None:
        return None
    raw = _nonempty_string(value, location)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ExperimentReplayError(
            f"{location} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExperimentReplayError(f"{location} must include a timezone")
    return parsed


def _exact_keys(
    value: Mapping[str, Any], expected: Set[str], location: str
) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise ExperimentReplayError(
            f"{location} is missing keys: {', '.join(missing)}"
        )
    if unknown:
        raise ExperimentReplayError(
            f"{location} has unknown keys: {', '.join(unknown)}"
        )


def _parse_action(
    value: Any,
    *,
    map_date: date,
    prices: Mapping[str, float],
    location: str,
) -> CorporateAction:
    action = _mapping(value, location)
    _exact_keys(action, _ACTION_KEYS, location)
    symbol = _nonempty_string(action["symbol"], f"{location}.symbol")
    if symbol not in prices:
        raise ExperimentReplayError(
            f"{location}.symbol is absent from prices on the ex-date"
        )
    event_type = _nonempty_string(
        action["event_type"], f"{location}.event_type"
    )
    if event_type not in _ACTION_TYPES:
        raise ExperimentReplayError(f"{location}.event_type is unsupported")
    ex_date = _date(action["ex_date"], f"{location}.ex_date")
    if ex_date != map_date:
        raise ExperimentReplayError(
            f"{location}.ex_date does not match its market date"
        )
    announced_at = _optional_timestamp(
        action["announced_at"], f"{location}.announced_at"
    )
    available_at = _optional_timestamp(
        action["available_at"], f"{location}.available_at"
    )
    if (
        announced_at is not None
        and available_at is not None
        and announced_at > available_at
    ):
        raise ExperimentReplayError(
            f"{location}.announced_at exceeds available_at"
        )
    cash_amount = _optional_finite(
        action["cash_amount"], f"{location}.cash_amount"
    )
    ratio = _optional_finite(action["ratio"], f"{location}.ratio")
    if event_type == "cash_dividend" and (
        cash_amount is None or cash_amount < 0
    ):
        raise ExperimentReplayError(
            f"{location}.cash_dividend requires cash_amount >= 0"
        )
    if event_type == "split" and (ratio is None or ratio <= 1):
        raise ExperimentReplayError(f"{location}.split requires ratio > 1")
    if event_type == "reverse_split" and (
        ratio is None or not 0 < ratio < 1
    ):
        raise ExperimentReplayError(
            f"{location}.reverse_split requires ratio in (0, 1)"
        )
    return CorporateAction(
        symbol=symbol,
        event_type=event_type,
        ex_date=ex_date,
        announced_at=announced_at,
        cash_amount=cash_amount,
        ratio=ratio,
        available_at=available_at,
    )


def load_embedded_market_snapshot(path: Path) -> MarketData:
    snapshot = _load_json_object(path)
    _exact_keys(snapshot, {"market", "metadata"}, str(path))
    metadata = _mapping(snapshot["metadata"], f"{path}.metadata")
    metadata_type = metadata.get("type")
    if metadata_type not in {
        "deterministic_synthetic_market",
        "frozen_experiment_market",
    }:
        raise ExperimentReplayError(
            "embedded market snapshot has an unsupported metadata type"
        )
    if metadata.get("investment_validity") is not False:
        raise ExperimentReplayError(
            "embedded market snapshot cannot establish investment validity"
        )
    if metadata_type == "frozen_experiment_market":
        _nonempty_string(
            metadata.get("source_type"), f"{path}.metadata.source_type"
        )
        if metadata.get("source_authenticity_verified") is not False:
            raise ExperimentReplayError(
                "frozen market snapshot cannot establish source authenticity"
            )
    market = _mapping(snapshot["market"], f"{path}.market")
    _exact_keys(market, _MARKET_KEYS, f"{path}.market")

    raw_dates = _sequence(market["dates"], f"{path}.market.dates")
    dates = [
        _date(value, f"{path}.market.dates[{index}]")
        for index, value in enumerate(raw_dates)
    ]
    if not dates or dates != sorted(set(dates)):
        raise ExperimentReplayError(
            f"{path}.market.dates must be non-empty, unique, and sorted"
        )
    date_strings = [value.isoformat() for value in dates]

    raw_prices = _mapping(market["prices"], f"{path}.market.prices")
    if set(raw_prices) != set(date_strings):
        raise ExperimentReplayError(
            f"{path}.market.prices dates do not match market.dates"
        )
    prices_by_date: Dict[date, Dict[str, float]] = {}
    for trading_date in dates:
        date_key = trading_date.isoformat()
        raw_row = _mapping(
            raw_prices[date_key], f"{path}.market.prices[{date_key}]"
        )
        if not raw_row:
            raise ExperimentReplayError(
                f"{path}.market.prices[{date_key}] must not be empty"
            )
        row: Dict[str, float] = {}
        for symbol_value, price_value in raw_row.items():
            symbol = _nonempty_string(
                symbol_value, f"{path}.market.prices[{date_key}].symbol"
            )
            price = _finite(
                price_value,
                f"{path}.market.prices[{date_key}][{symbol}]",
            )
            if price <= 0:
                raise ExperimentReplayError(
                    f"{path}.market.prices[{date_key}][{symbol}] must be > 0"
                )
            row[symbol] = price
        prices_by_date[trading_date] = row

    raw_tradable = market["tradable"]
    tradable_by_date: Optional[Dict[date, Set[str]]]
    if raw_tradable is None:
        tradable_by_date = None
    else:
        tradable = _mapping(raw_tradable, f"{path}.market.tradable")
        if set(tradable) != set(date_strings):
            raise ExperimentReplayError(
                f"{path}.market.tradable dates do not match market.dates"
            )
        tradable_by_date = {}
        for trading_date in dates:
            date_key = trading_date.isoformat()
            values = _sequence(
                tradable[date_key], f"{path}.market.tradable[{date_key}]"
            )
            symbols = [
                _nonempty_string(
                    value,
                    f"{path}.market.tradable[{date_key}][{index}]",
                )
                for index, value in enumerate(values)
            ]
            if symbols != sorted(set(symbols)):
                raise ExperimentReplayError(
                    f"{path}.market.tradable[{date_key}] is not canonical"
                )
            if not set(symbols).issubset(prices_by_date[trading_date]):
                raise ExperimentReplayError(
                    f"{path}.market.tradable[{date_key}] contains unknown symbols"
                )
            tradable_by_date[trading_date] = set(symbols)

    raw_actions = _mapping(
        market["corporate_actions"], f"{path}.market.corporate_actions"
    )
    if not set(raw_actions).issubset(date_strings):
        raise ExperimentReplayError(
            f"{path}.market.corporate_actions contains unknown dates"
        )
    corporate_actions_by_date: Dict[date, List[CorporateAction]] = {}
    seen_actions = set()
    for date_key in sorted(raw_actions):
        trading_date = _date(
            date_key, f"{path}.market.corporate_actions date"
        )
        values = _sequence(
            raw_actions[date_key],
            f"{path}.market.corporate_actions[{date_key}]",
        )
        parsed_actions = []
        for index, value in enumerate(values):
            action = _parse_action(
                value,
                map_date=trading_date,
                prices=prices_by_date[trading_date],
                location=(
                    f"{path}.market.corporate_actions[{date_key}][{index}]"
                ),
            )
            key = (action.symbol, action.event_type, action.ex_date)
            if key in seen_actions:
                raise ExperimentReplayError(
                    f"{path}.market.corporate_actions contains duplicates"
                )
            seen_actions.add(key)
            parsed_actions.append(action)
        corporate_actions_by_date[trading_date] = parsed_actions

    price_adjustment = _nonempty_string(
        market["price_adjustment"], f"{path}.market.price_adjustment"
    )
    try:
        reconstructed = MarketData(
            dates=dates,
            prices_by_date=prices_by_date,
            tradable_by_date=tradable_by_date,
            corporate_actions_by_date=corporate_actions_by_date,
            price_adjustment=price_adjustment,
        )
    except ValueError as exc:
        raise ExperimentReplayError(
            f"{path}.market is invalid: {exc}"
        ) from exc
    if market_identity(reconstructed) != dict(market):
        raise ExperimentReplayError(
            "embedded market snapshot is not canonically reversible"
        )
    return reconstructed


def replay_experiment_artifacts(experiment_directory: Path) -> Dict[str, Any]:
    integrity = verify_experiment_artifacts(experiment_directory)
    root = Path(integrity["experiment_directory"])
    manifest = _load_json_object(root / "manifest.json")
    expected_python = _nonempty_string(
        manifest.get("python_version"), "manifest.python_version"
    )
    actual_python = platform.python_version()
    if actual_python != expected_python:
        raise ExperimentReplayError(
            "experiment replay requires the recorded Python version: "
            f"expected {expected_python}, running {actual_python}"
        )
    market_snapshot_path = root / "market.snapshot.json"
    if not market_snapshot_path.is_file():
        raise ExperimentReplayError(
            "experiment replay requires an embedded market.snapshot.json; "
            "legacy artifacts without one remain verify-only"
        )
    market_snapshot = _load_json_object(market_snapshot_path)
    market_source = _mapping(
        manifest.get("market_source"), "manifest.market_source"
    )
    schema_version = integrity["artifact_schema_version"]
    market_source_type = _nonempty_string(
        market_source.get("type"), "manifest.market_source.type"
    )
    snapshot_metadata = _mapping(
        market_snapshot.get("metadata"), "market.snapshot.json.metadata"
    )
    if (
        snapshot_metadata.get("type") == "frozen_experiment_market"
        and snapshot_metadata.get("source_type") != market_source_type
    ):
        raise ExperimentReplayError(
            "embedded market snapshot metadata changed after verification"
        )
    if schema_version >= 2:
        replay_input = _mapping(
            manifest.get("replay_input"), "manifest.replay_input"
        )
        if replay_input.get("file_sha256") != file_sha256(
            market_snapshot_path
        ):
            raise ExperimentReplayError(
                "embedded market snapshot file changed after integrity verification"
            )
        market_payload = _mapping(
            market_snapshot.get("market"), "market.snapshot.json.market"
        )
        market_sha256 = hashlib.sha256(
            json.dumps(
                market_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        if replay_input.get("market_sha256") != market_sha256:
            raise ExperimentReplayError(
                "embedded market snapshot identity changed after verification"
            )
        if (
            replay_input.get("source_type") != market_source_type
            or replay_input.get("source_sha256")
            != market_source.get("sha256")
        ):
            raise ExperimentReplayError(
                "embedded market snapshot lineage changed after verification"
            )
    else:
        source_sha256 = hashlib.sha256(
            json.dumps(
                market_snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        if market_source.get("sha256") != source_sha256:
            raise ExperimentReplayError(
                "legacy embedded market snapshot does not match market_source"
            )

    market = load_embedded_market_snapshot(market_snapshot_path)
    policy = load_policy(root / "policy.snapshot.json")
    spec = load_experiment_spec(root / "experiment.snapshot.json")
    replayed = run_experiment(spec, policy, market)
    expected_input_id = manifest["experiment_input_id"]
    if replayed.experiment_input_id != expected_input_id:
        raise ExperimentReplayError(
            "replayed experiment_input_id differs from the artifact: "
            f"expected {expected_input_id}, got {replayed.experiment_input_id}"
        )
    expected_result_sha256 = manifest["result_sha256"]
    if replayed.result_sha256 != expected_result_sha256:
        raise ExperimentReplayError(
            "replayed result_sha256 differs from the artifact: "
            f"expected {expected_result_sha256}, got {replayed.result_sha256}"
        )
    stored_summary = _load_json_object(root / "summary.json")
    if replayed.summary != stored_summary:
        raise ExperimentReplayError(
            "replayed summary differs despite the recorded result hash"
        )
    return {
        "status": "pass",
        "artifact_type": "research_experiment_replay",
        "artifact_schema_version": schema_version,
        "experiment_directory": str(root),
        "experiment_id": manifest["experiment_id"],
        "experiment_input_id": replayed.experiment_input_id,
        "result_sha256": replayed.result_sha256,
        "manifest_sha256": integrity["manifest_sha256"],
        "replay_tool_version": __version__,
        "python_version": actual_python,
        "python_version_match": True,
        "market_source_type": market_source_type,
        "portable_replay_input_verified": integrity["replay_input_verified"],
        "integrity_verified_before_replay": True,
        "embedded_market_snapshot_verified": True,
        "experiment_input_id_match": True,
        "result_sha256_match": True,
        "summary_match": True,
        "replayed_case_count": len(replayed.cases),
        "replay_performed": True,
        "artifact_authenticity_verified": False,
        "investment_validity_established": False,
        "automatic_execution_allowed": False,
    }
