from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .cross_engine import EngineValidationError, file_sha256
from .data import DataError, load_prices
from .datasets import load_curated_dataset
from .models import MarketData


ENGINE_OBSERVATION_FIELDS = {
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "tradable",
    "available_at",
    "source_snapshot_id",
    "adjustment",
}


@dataclass(frozen=True)
class EngineBar:
    open: float
    high: float
    low: float
    close: float
    volume: int
    tradable: bool


@dataclass(frozen=True)
class EngineMarketInput:
    market: MarketData
    bars_by_date: Dict[str, Dict[str, EngineBar]]
    prices_path: Path
    reference_manifest: Dict[str, Any]
    source_type: str
    dataset_directory: Optional[Path] = None
    dataset_manifest: Optional[Dict[str, Any]] = None
    dataset_snapshot_sha256: Optional[str] = None


def load_json_object(path: Path) -> Dict[str, Any]:
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


def resolve_standalone_prices_path(
    reference_directory: Path,
    supplied_prices_path: Optional[Path],
    *,
    adapter_name: str,
) -> Tuple[Path, Dict[str, Any]]:
    manifest_path = reference_directory / "manifest.json"
    manifest = load_json_object(manifest_path)
    if manifest.get("artifact_type") != "backtest":
        raise EngineValidationError(
            f"{manifest_path} must describe a backtest artifact"
        )
    data_source = manifest.get("data_source")
    if not isinstance(data_source, dict) or data_source.get("type") != (
        "standalone_prices_csv"
    ):
        raise EngineValidationError(
            f"{adapter_name} supports standalone_prices_csv runs only; "
            "curated datasets with tradability or corporate actions fail closed"
        )
    if supplied_prices_path is None:
        manifest_prices_path = manifest.get("prices_path")
        if not isinstance(manifest_prices_path, str) or not manifest_prices_path:
            raise EngineValidationError(
                "reference manifest has no prices_path; pass --prices explicitly"
            )
        prices_path = Path(manifest_prices_path)
    else:
        prices_path = supplied_prices_path
    expected_hash = manifest.get("prices_sha256")
    actual_hash = file_sha256(prices_path)
    if actual_hash != expected_hash:
        raise EngineValidationError(
            "price input SHA-256 does not match the reference backtest: "
            f"expected {expected_hash}, got {actual_hash}"
        )
    return prices_path.resolve(), manifest


def _standalone_bars(market: MarketData) -> Dict[str, Dict[str, EngineBar]]:
    return {
        trading_date.isoformat(): {
            symbol: EngineBar(
                open=float(close),
                high=float(close),
                low=float(close),
                close=float(close),
                volume=10**12,
                tradable=True,
            )
            for symbol, close in market.prices_on(trading_date).items()
        }
        for trading_date in market.dates
    }


def _curated_bars(
    directory: Path, market: MarketData
) -> Dict[str, Dict[str, EngineBar]]:
    observations_path = directory / "observations.csv"
    try:
        handle = observations_path.open(newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise EngineValidationError(
            f"file not found: {observations_path}"
        ) from exc
    bars: Dict[str, Dict[str, EngineBar]] = {}
    observed_keys = set()
    with handle:
        reader = csv.DictReader(handle)
        if set(reader.fieldnames or []) != ENGINE_OBSERVATION_FIELDS:
            raise EngineValidationError(
                f"{observations_path} has an unsupported observation schema"
            )
        for line_number, row in enumerate(reader, start=2):
            location = f"{observations_path}:{line_number}"
            raw_date = str(row.get("date") or "").strip()
            try:
                trading_date = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise EngineValidationError(
                    f"{location}:date must be YYYY-MM-DD"
                ) from exc
            symbol = str(row.get("symbol") or "").strip().upper()
            key = (trading_date, symbol)
            if not symbol or key in observed_keys:
                raise EngineValidationError(
                    f"{location}:symbol is empty or duplicated"
                )
            observed_keys.add(key)
            values = {
                field: _finite_positive_float(
                    row.get(field), f"{location}:{field}"
                )
                for field in ("open", "high", "low", "close")
            }
            if values["low"] > min(values["open"], values["close"]):
                raise EngineValidationError(
                    f"{location}:low exceeds open or close"
                )
            if values["high"] < max(values["open"], values["close"]):
                raise EngineValidationError(
                    f"{location}:high is below open or close"
                )
            raw_volume = _finite_non_negative_float(
                row.get("volume"), f"{location}:volume"
            )
            if not raw_volume.is_integer():
                raise EngineValidationError(
                    f"{location}:volume must be an integer"
                )
            normalized_tradable = str(
                row.get("tradable") or ""
            ).strip().lower()
            if normalized_tradable not in {"true", "false"}:
                raise EngineValidationError(
                    f"{location}:tradable must be true or false"
                )
            adjustment = str(row.get("adjustment") or "").strip()
            if adjustment != market.price_adjustment:
                raise EngineValidationError(
                    f"{location}:adjustment differs from the dataset manifest"
                )
            bars.setdefault(trading_date.isoformat(), {})[symbol] = EngineBar(
                **values,
                volume=int(raw_volume),
                tradable=normalized_tradable == "true",
            )
    expected_keys = {
        (trading_date, symbol)
        for trading_date in market.dates
        for symbol in market.prices_on(trading_date)
    }
    if observed_keys != expected_keys:
        raise EngineValidationError(
            "curated observations do not match the validated market prices"
        )
    return bars


def _finite_non_negative_float(value: Any, location: str) -> float:
    if isinstance(value, bool):
        raise EngineValidationError(f"{location} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise EngineValidationError(f"{location} must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise EngineValidationError(f"{location} must be finite and >= 0")
    return parsed


def resolve_engine_market_input(
    reference_directory: Path,
    *,
    supplied_prices_path: Optional[Path],
    supplied_dataset_path: Optional[Path],
    adapter_name: str,
) -> EngineMarketInput:
    if supplied_prices_path is not None and supplied_dataset_path is not None:
        raise EngineValidationError("--prices and --dataset are mutually exclusive")
    manifest_path = reference_directory / "manifest.json"
    manifest = load_json_object(manifest_path)
    if manifest.get("artifact_type") != "backtest":
        raise EngineValidationError(
            f"{manifest_path} must describe a backtest artifact"
        )
    data_source = manifest.get("data_source")
    if not isinstance(data_source, dict):
        raise EngineValidationError(
            f"{manifest_path} data_source must be an object"
        )
    source_type = data_source.get("type")
    if source_type == "standalone_prices_csv":
        if supplied_dataset_path is not None:
            raise EngineValidationError(
                f"{adapter_name} reference uses standalone prices; pass --prices"
            )
        prices_path, verified_manifest = resolve_standalone_prices_path(
            reference_directory,
            supplied_prices_path,
            adapter_name=adapter_name,
        )
        try:
            market = load_prices(prices_path)
        except DataError as exc:
            raise EngineValidationError(str(exc)) from exc
        return EngineMarketInput(
            market=market,
            bars_by_date=_standalone_bars(market),
            prices_path=prices_path,
            reference_manifest=verified_manifest,
            source_type="standalone_prices_csv",
        )
    if source_type != "curated_dataset":
        raise EngineValidationError(
            f"{adapter_name} does not support reference data source {source_type!r}"
        )
    if supplied_prices_path is not None:
        raise EngineValidationError(
            f"{adapter_name} reference uses a curated dataset; pass --dataset "
            "so tradability and lineage cannot be dropped"
        )
    if supplied_dataset_path is None:
        raw_prices_path = manifest.get("prices_path")
        if not isinstance(raw_prices_path, str) or not raw_prices_path:
            raise EngineValidationError(
                "reference manifest has no dataset path; pass --dataset explicitly"
            )
        dataset_path = Path(raw_prices_path).parent
    else:
        dataset_path = supplied_dataset_path
    try:
        dataset = load_curated_dataset(dataset_path)
    except DataError as exc:
        raise EngineValidationError(str(exc)) from exc
    snapshot_path = reference_directory / "dataset.snapshot.json"
    snapshot = load_json_object(snapshot_path)
    if dataset.manifest != snapshot:
        raise EngineValidationError(
            "curated dataset manifest differs from the reference snapshot"
        )
    expected_source = {
        "type": "curated_dataset",
        "dataset_id": dataset.manifest.get("dataset_id"),
        "as_of": dataset.manifest.get("as_of"),
        "quality_status": dataset.manifest.get("quality_status"),
        "source_snapshot_id": (
            dataset.manifest.get("source_snapshot", {}).get("snapshot_id")
            if isinstance(dataset.manifest.get("source_snapshot"), dict)
            else None
        ),
    }
    if data_source != expected_source:
        raise EngineValidationError(
            "reference data_source differs from the curated dataset identity"
        )
    prices_path = dataset.directory / "prices.csv"
    actual_prices_hash = file_sha256(prices_path)
    if actual_prices_hash != manifest.get("prices_sha256"):
        raise EngineValidationError(
            "curated prices SHA-256 does not match the reference backtest"
        )
    file_hashes = manifest.get("file_sha256")
    expected_snapshot_hash = (
        file_hashes.get("dataset.snapshot.json")
        if isinstance(file_hashes, dict)
        else None
    )
    actual_snapshot_hash = file_sha256(snapshot_path)
    if expected_snapshot_hash != actual_snapshot_hash:
        raise EngineValidationError(
            "reference dataset snapshot failed its bound SHA-256"
        )
    return EngineMarketInput(
        market=dataset.market,
        bars_by_date=_curated_bars(dataset.directory, dataset.market),
        prices_path=prices_path.resolve(),
        reference_manifest=manifest,
        source_type="curated_dataset",
        dataset_directory=dataset.directory.resolve(),
        dataset_manifest=dict(dataset.manifest),
        dataset_snapshot_sha256=actual_snapshot_hash,
    )


def reject_unsupported_unadjusted_actions(
    market_input: EngineMarketInput, *, adapter_name: str
) -> None:
    market = market_input.market
    action_count = sum(
        len(actions)
        for actions in (market.corporate_actions_by_date or {}).values()
    )
    if market.price_adjustment == "none" and action_count:
        raise EngineValidationError(
            f"{adapter_name} does not yet reproduce explicit unadjusted "
            "corporate-action accounting"
        )


def _positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool):
        raise EngineValidationError(f"{location} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise EngineValidationError(
            f"{location} must be a positive integer"
        ) from exc
    if isinstance(value, float) and not value.is_integer():
        raise EngineValidationError(f"{location} must be a positive integer")
    if isinstance(value, str) and str(parsed) != value.strip():
        raise EngineValidationError(f"{location} must be a positive integer")
    if parsed <= 0:
        raise EngineValidationError(f"{location} must be a positive integer")
    return parsed


def _finite_positive_float(value: Any, location: str) -> float:
    if isinstance(value, bool):
        raise EngineValidationError(f"{location} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise EngineValidationError(f"{location} must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise EngineValidationError(f"{location} must be finite and > 0")
    return parsed


def load_frozen_order_intents(
    signals_path: Path,
    *,
    lot_size: int,
    symbols: Sequence[str],
    trading_dates: Sequence[str],
    market_prices: Mapping[str, Mapping[str, float]],
    adapter_name: str,
) -> List[Dict[str, Any]]:
    required_signal_fields = {
        "signal_date",
        "execution_date",
        "status",
        "orders",
    }
    try:
        handle = signals_path.open(newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise EngineValidationError(f"file not found: {signals_path}") from exc
    intents: List[Dict[str, Any]] = []
    seen_execution_symbol = set()
    symbol_set = set(symbols)
    trading_date_set = set(trading_dates)
    with handle:
        reader = csv.DictReader(handle)
        actual_fields = set(reader.fieldnames or [])
        if not required_signal_fields.issubset(actual_fields):
            raise EngineValidationError(
                f"{signals_path} is missing columns: "
                + ", ".join(sorted(required_signal_fields - actual_fields))
            )
        sequence = 0
        for line_number, row in enumerate(reader, start=2):
            signal_date = (row.get("signal_date") or "").strip()
            execution_date = (row.get("execution_date") or "").strip()
            status = (row.get("status") or "").strip()
            if status not in {"accepted", "blocked", "no_action"}:
                raise EngineValidationError(
                    f"{signals_path}:{line_number}: unsupported signal status"
                )
            try:
                raw_orders = json.loads(row.get("orders") or "")
            except json.JSONDecodeError as exc:
                raise EngineValidationError(
                    f"{signals_path}:{line_number}: orders must be a JSON array"
                ) from exc
            if not isinstance(raw_orders, list):
                raise EngineValidationError(
                    f"{signals_path}:{line_number}: orders must be a JSON array"
                )
            if status != "accepted":
                continue
            for order_index, raw_order in enumerate(raw_orders):
                location = f"{signals_path}:{line_number}:orders[{order_index}]"
                if not isinstance(raw_order, dict):
                    raise EngineValidationError(f"{location} must be an object")
                order_signal_date = raw_order.get("signal_date")
                order_execution_date = raw_order.get("execution_date")
                if order_signal_date != signal_date:
                    raise EngineValidationError(
                        f"{location}.signal_date does not match its signal row"
                    )
                if order_execution_date != execution_date:
                    raise EngineValidationError(
                        f"{location}.execution_date does not match its signal row"
                    )
                if execution_date not in trading_date_set:
                    raise EngineValidationError(
                        f"{location}.execution_date is outside the reference NAV"
                    )
                symbol = str(raw_order.get("symbol") or "").strip().upper()
                if symbol not in symbol_set:
                    raise EngineValidationError(
                        f"{location}.symbol is outside the reference portfolio: "
                        f"{symbol or '<empty>'}"
                    )
                side = str(raw_order.get("side") or "").strip().upper()
                if side not in {"BUY", "SELL"}:
                    raise EngineValidationError(
                        f"{location}.side must be BUY or SELL"
                    )
                quantity = _positive_int(
                    raw_order.get("quantity"), f"{location}.quantity"
                )
                if quantity % lot_size != 0:
                    raise EngineValidationError(
                        f"{location}.quantity must be a multiple of lot_size"
                    )
                signal_price = _finite_positive_float(
                    raw_order.get("signal_price"), f"{location}.signal_price"
                )
                expected_signal_price = market_prices.get(signal_date, {}).get(
                    symbol
                )
                if expected_signal_price is None:
                    raise EngineValidationError(
                        f"{location} has no market price on its signal date"
                    )
                if abs(signal_price - expected_signal_price) > 1e-8:
                    raise EngineValidationError(
                        f"{location}.signal_price does not match the frozen "
                        "price input"
                    )
                duplicate_key = (execution_date, symbol)
                if duplicate_key in seen_execution_symbol:
                    raise EngineValidationError(
                        f"{adapter_name} cannot represent multiple orders for "
                        f"{symbol} on {execution_date}"
                    )
                seen_execution_symbol.add(duplicate_key)
                intents.append(
                    {
                        "sequence": sequence,
                        "signal_date": signal_date,
                        "execution_date": execution_date,
                        "symbol": symbol,
                        "side": side,
                        "quantity": quantity,
                        "signal_price": signal_price,
                    }
                )
                sequence += 1
    date_order = {value: index for index, value in enumerate(trading_dates)}
    return sorted(
        intents,
        key=lambda item: (
            date_order[item["execution_date"]],
            0 if item["side"] == "SELL" else 1,
            item["sequence"],
        ),
    )
