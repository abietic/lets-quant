from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .cross_engine import EngineValidationError, file_sha256


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
            if raw_orders and status != "accepted":
                raise EngineValidationError(
                    f"{signals_path}:{line_number}: only accepted signals may "
                    "contain frozen orders"
                )
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
