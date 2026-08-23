from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


SCHEMA_VERSION = 3
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
SIGNAL_FIELDS = [
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
]
SIGNAL_STATUSES = {"accepted", "blocked", "no_action"}
ORDER_FIELDS = [
    "order_id",
    "signal_date",
    "execution_date",
    "symbol",
    "side",
    "requested_quantity",
    "filled_quantity",
    "avg_fill_price",
    "commission",
    "tax",
    "final_status",
    "event_count",
    "trade_count",
]
EVENT_FIELDS = [
    "sequence",
    "event_time",
    "event_type",
    "order_id",
    "trade_id",
    "symbol",
    "side",
    "requested_quantity",
    "cumulative_filled_quantity",
    "event_fill_quantity",
    "order_status",
    "fill_price",
    "commission",
    "tax",
    "message",
]
FINAL_ORDER_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}
ORDER_STATUSES = FINAL_ORDER_STATUSES | {"PENDING_NEW", "ACTIVE"}
ORDER_EVENT_TYPES = {
    "order_pending_new",
    "order_creation_pass",
    "order_creation_reject",
    "trade",
    "order_cancellation_pass",
    "order_unsolicited_update",
}
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


def _parse_date_field(path: Path, line_number: int, field: str, value: Any) -> str:
    parsed = str(value or "").strip()
    try:
        datetime.strptime(parsed, "%Y-%m-%d")
    except ValueError as exc:
        raise EngineValidationError(
            f"{path}:{line_number}:{field} must be YYYY-MM-DD"
        ) from exc
    return parsed


def _parse_json_field(
    path: Path, line_number: int, field: str, value: Any
) -> Any:
    try:
        parsed = json.loads(str(value or ""))
    except json.JSONDecodeError as exc:
        raise EngineValidationError(
            f"{path}:{line_number}:{field} must be valid JSON"
        ) from exc
    return _validated_json_value(
        parsed, f"{path}:{line_number}:{field}"
    )


def _validated_json_value(value: Any, location: str) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EngineValidationError(f"{location} contains a non-finite number")
        return value
    if isinstance(value, list):
        return [
            _validated_json_value(item, f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        normalized: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise EngineValidationError(
                    f"{location} contains an invalid object key"
                )
            normalized[key] = _validated_json_value(
                item, f"{location}.{key}"
            )
        return normalized
    raise EngineValidationError(f"{location} contains an unsupported JSON value")


def _parse_signal_weights(value: Any, location: str) -> Dict[str, float]:
    if not isinstance(value, dict):
        raise EngineValidationError(f"{location} must be a JSON object")
    weights: Dict[str, float] = {}
    for raw_symbol, raw_weight in value.items():
        symbol = str(raw_symbol).strip().upper()
        if not symbol or symbol in weights:
            raise EngineValidationError(
                f"{location} contains an invalid or duplicate symbol"
            )
        if isinstance(raw_weight, bool):
            raise EngineValidationError(f"{location}.{symbol} must be a number")
        weight = _finite_float(raw_weight, f"{location}.{symbol}")
        if weight < 0:
            raise EngineValidationError(f"{location}.{symbol} must be >= 0")
        weights[symbol] = weight
    return dict(sorted(weights.items()))


def _parse_signal_orders(
    value: Any,
    *,
    path: Path,
    line_number: int,
    signal_date: str,
    execution_date: str,
) -> List[Dict[str, Any]]:
    location = f"{path}:{line_number}:orders"
    if not isinstance(value, list):
        raise EngineValidationError(f"{location} must be a JSON array")
    required = {
        "signal_date",
        "execution_date",
        "symbol",
        "side",
        "quantity",
        "signal_price",
        "reason",
    }
    orders: List[Dict[str, Any]] = []
    seen_symbols = set()
    for index, raw_order in enumerate(value):
        order_location = f"{location}[{index}]"
        if not isinstance(raw_order, dict) or set(raw_order) != required:
            raise EngineValidationError(
                f"{order_location} must have exactly these fields: "
                + ", ".join(sorted(required))
            )
        order_signal_date = str(raw_order["signal_date"]).strip()
        order_execution_date = str(raw_order["execution_date"]).strip()
        if order_signal_date != signal_date or order_execution_date != execution_date:
            raise EngineValidationError(
                f"{order_location} dates must match their signal row"
            )
        symbol = str(raw_order["symbol"]).strip().upper()
        if not symbol or symbol in seen_symbols:
            raise EngineValidationError(
                f"{order_location}.symbol is invalid or duplicated"
            )
        seen_symbols.add(symbol)
        side = str(raw_order["side"]).strip().upper()
        if side not in {"BUY", "SELL"}:
            raise EngineValidationError(
                f"{order_location}.side must be BUY or SELL"
            )
        quantity = _non_negative_int(
            raw_order["quantity"], f"{order_location}.quantity"
        )
        if quantity <= 0:
            raise EngineValidationError(
                f"{order_location}.quantity must be > 0"
            )
        signal_price = _finite_float(
            raw_order["signal_price"], f"{order_location}.signal_price"
        )
        if signal_price <= 0:
            raise EngineValidationError(
                f"{order_location}.signal_price must be > 0"
            )
        reason = str(raw_order["reason"]).strip()
        if not reason:
            raise EngineValidationError(
                f"{order_location}.reason must not be empty"
            )
        orders.append(
            {
                "signal_date": order_signal_date,
                "execution_date": order_execution_date,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "signal_price": signal_price,
                "reason": reason,
            }
        )
    return orders


def read_signal_rows(path: Path) -> List[Dict[str, Any]]:
    try:
        handle = path.open(newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise EngineValidationError(f"file not found: {path}") from exc
    rows: List[Dict[str, Any]] = []
    seen_signal_dates = set()
    with handle:
        reader = csv.DictReader(handle)
        actual = set(reader.fieldnames or [])
        required = set(SIGNAL_FIELDS)
        if actual != required:
            raise EngineValidationError(
                f"{path} must have exactly these columns: "
                + ", ".join(sorted(required))
            )
        for line_number, row in enumerate(reader, start=2):
            signal_date = _parse_date_field(
                path, line_number, "signal_date", row.get("signal_date")
            )
            execution_date = _parse_date_field(
                path,
                line_number,
                "execution_date",
                row.get("execution_date"),
            )
            if signal_date >= execution_date:
                raise EngineValidationError(
                    f"{path}:{line_number}:execution_date must follow signal_date"
                )
            if signal_date in seen_signal_dates:
                raise EngineValidationError(
                    f"{path}:{line_number}:duplicate signal_date {signal_date}"
                )
            seen_signal_dates.add(signal_date)
            status = str(row.get("status") or "").strip()
            if status not in SIGNAL_STATUSES:
                raise EngineValidationError(
                    f"{path}:{line_number}:unsupported signal status {status}"
                )
            turnover = _finite_float(
                row.get("estimated_turnover"),
                f"{path}:{line_number}:estimated_turnover",
            )
            if turnover < 0:
                raise EngineValidationError(
                    f"{path}:{line_number}:estimated_turnover must be >= 0"
                )
            reason = str(row.get("reason") or "").strip()
            if not reason:
                raise EngineValidationError(
                    f"{path}:{line_number}:reason must not be empty"
                )
            decision_id = str(row.get("decision_id") or "").strip()
            if decision_id and re.fullmatch(r"[0-9a-f]{64}", decision_id) is None:
                raise EngineValidationError(
                    f"{path}:{line_number}:decision_id must be lowercase SHA-256"
                )
            strategy_kind = str(row.get("strategy_kind") or "").strip()
            target_weights = _parse_signal_weights(
                _parse_json_field(
                    path, line_number, "target_weights", row.get("target_weights")
                ),
                f"{path}:{line_number}:target_weights",
            )
            evidence = _parse_json_field(
                path,
                line_number,
                "decision_evidence",
                row.get("decision_evidence"),
            )
            if not isinstance(evidence, dict):
                raise EngineValidationError(
                    f"{path}:{line_number}:decision_evidence must be a JSON object"
                )
            diagnostics = _parse_json_field(
                path, line_number, "diagnostics", row.get("diagnostics")
            )
            if not isinstance(diagnostics, list) or not all(
                isinstance(item, str) and item.strip() for item in diagnostics
            ):
                raise EngineValidationError(
                    f"{path}:{line_number}:diagnostics must contain strings"
                )
            orders = _parse_signal_orders(
                _parse_json_field(
                    path, line_number, "orders", row.get("orders")
                ),
                path=path,
                line_number=line_number,
                signal_date=signal_date,
                execution_date=execution_date,
            )
            if status == "accepted" and not orders:
                raise EngineValidationError(
                    f"{path}:{line_number}:accepted signal requires orders"
                )
            if status == "no_action" and orders:
                raise EngineValidationError(
                    f"{path}:{line_number}:no_action signal cannot contain orders"
                )
            if status in {"accepted", "no_action"} and not decision_id:
                raise EngineValidationError(
                    f"{path}:{line_number}:{status} signal requires a decision_id"
                )
            if decision_id:
                if not strategy_kind or not target_weights or not evidence:
                    raise EngineValidationError(
                        f"{path}:{line_number}:decision fields are incomplete"
                    )
            elif target_weights or evidence or diagnostics:
                raise EngineValidationError(
                    f"{path}:{line_number}:decision payload requires a decision_id"
                )
            rows.append(
                {
                    "signal_date": signal_date,
                    "execution_date": execution_date,
                    "status": status,
                    "estimated_turnover": turnover,
                    "reason": reason,
                    "decision_id": decision_id,
                    "strategy_kind": strategy_kind,
                    "target_weights": target_weights,
                    "decision_evidence": evidence,
                    "diagnostics": diagnostics,
                    "orders": orders,
                }
            )
    return rows


def read_order_rows(path: Path) -> List[Dict[str, Any]]:
    try:
        handle = path.open(newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise EngineValidationError(f"file not found: {path}") from exc
    rows: List[Dict[str, Any]] = []
    seen_order_ids = set()
    with handle:
        reader = csv.DictReader(handle)
        actual = set(reader.fieldnames or [])
        required = set(ORDER_FIELDS)
        if actual != required:
            raise EngineValidationError(
                f"{path} must have exactly these columns: "
                + ", ".join(sorted(required))
            )
        for line_number, row in enumerate(reader, start=2):
            order_id = str(row.get("order_id") or "").strip()
            if not order_id:
                raise EngineValidationError(
                    f"{path}:{line_number}:order_id must not be empty"
                )
            if order_id in seen_order_ids:
                raise EngineValidationError(
                    f"{path}:{line_number}:duplicate order_id {order_id}"
                )
            seen_order_ids.add(order_id)
            symbol = str(row.get("symbol") or "").strip().upper()
            side = str(row.get("side") or "").strip().upper()
            final_status = str(row.get("final_status") or "").strip().upper()
            if not symbol:
                raise EngineValidationError(
                    f"{path}:{line_number}:symbol must not be empty"
                )
            if side not in {"BUY", "SELL"}:
                raise EngineValidationError(
                    f"{path}:{line_number}:side must be BUY or SELL"
                )
            if final_status not in FINAL_ORDER_STATUSES:
                raise EngineValidationError(
                    f"{path}:{line_number}:final_status is unsupported"
                )
            parsed = {
                "order_id": order_id,
                "signal_date": _parse_date_field(
                    path, line_number, "signal_date", row.get("signal_date")
                ),
                "execution_date": _parse_date_field(
                    path,
                    line_number,
                    "execution_date",
                    row.get("execution_date"),
                ),
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
                "avg_fill_price": _finite_float(
                    row.get("avg_fill_price"),
                    f"{path}:{line_number}:avg_fill_price",
                ),
                "commission": _finite_float(
                    row.get("commission"),
                    f"{path}:{line_number}:commission",
                ),
                "tax": _finite_float(
                    row.get("tax"), f"{path}:{line_number}:tax"
                ),
                "final_status": final_status,
                "event_count": _non_negative_int(
                    row.get("event_count"),
                    f"{path}:{line_number}:event_count",
                ),
                "trade_count": _non_negative_int(
                    row.get("trade_count"),
                    f"{path}:{line_number}:trade_count",
                ),
            }
            if parsed["requested_quantity"] <= 0:
                raise EngineValidationError(
                    f"{path}:{line_number}:requested_quantity must be > 0"
                )
            if parsed["filled_quantity"] > parsed["requested_quantity"]:
                raise EngineValidationError(
                    f"{path}:{line_number}:filled_quantity exceeds requested_quantity"
                )
            if any(
                parsed[field] < 0
                for field in ("avg_fill_price", "commission", "tax")
            ):
                raise EngineValidationError(
                    f"{path}:{line_number}:money fields must be >= 0"
                )
            if parsed["filled_quantity"] > 0 and parsed["avg_fill_price"] <= 0:
                raise EngineValidationError(
                    f"{path}:{line_number}:avg_fill_price must be > 0 for a fill"
                )
            rows.append(parsed)
    return rows


def read_event_rows(path: Path) -> List[Dict[str, Any]]:
    try:
        handle = path.open(newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise EngineValidationError(f"file not found: {path}") from exc
    rows: List[Dict[str, Any]] = []
    with handle:
        reader = csv.DictReader(handle)
        actual = set(reader.fieldnames or [])
        required = set(EVENT_FIELDS)
        if actual != required:
            raise EngineValidationError(
                f"{path} must have exactly these columns: "
                + ", ".join(sorted(required))
            )
        for line_number, row in enumerate(reader, start=2):
            event_time = str(row.get("event_time") or "").strip()
            try:
                parsed_time = datetime.fromisoformat(event_time)
            except ValueError as exc:
                raise EngineValidationError(
                    f"{path}:{line_number}:event_time must be ISO-8601"
                ) from exc
            if parsed_time.tzinfo is None:
                raise EngineValidationError(
                    f"{path}:{line_number}:event_time must include a timezone"
                )
            event_type = str(row.get("event_type") or "").strip()
            order_id = str(row.get("order_id") or "").strip()
            trade_id = str(row.get("trade_id") or "").strip()
            symbol = str(row.get("symbol") or "").strip().upper()
            side = str(row.get("side") or "").strip().upper()
            order_status = str(row.get("order_status") or "").strip().upper()
            if event_type not in ORDER_EVENT_TYPES:
                raise EngineValidationError(
                    f"{path}:{line_number}:unsupported event_type {event_type}"
                )
            if not order_id or not symbol:
                raise EngineValidationError(
                    f"{path}:{line_number}:order_id and symbol must not be empty"
                )
            if side not in {"BUY", "SELL"}:
                raise EngineValidationError(
                    f"{path}:{line_number}:side must be BUY or SELL"
                )
            if order_status not in ORDER_STATUSES:
                raise EngineValidationError(
                    f"{path}:{line_number}:unsupported order_status"
                )
            if event_type == "trade" and not trade_id:
                raise EngineValidationError(
                    f"{path}:{line_number}:trade event requires trade_id"
                )
            if event_type != "trade" and trade_id:
                raise EngineValidationError(
                    f"{path}:{line_number}:non-trade event cannot have trade_id"
                )
            parsed = {
                "sequence": _non_negative_int(
                    row.get("sequence"), f"{path}:{line_number}:sequence"
                ),
                "event_time": event_time,
                "event_type": event_type,
                "order_id": order_id,
                "trade_id": trade_id,
                "symbol": symbol,
                "side": side,
                "requested_quantity": _non_negative_int(
                    row.get("requested_quantity"),
                    f"{path}:{line_number}:requested_quantity",
                ),
                "cumulative_filled_quantity": _non_negative_int(
                    row.get("cumulative_filled_quantity"),
                    f"{path}:{line_number}:cumulative_filled_quantity",
                ),
                "event_fill_quantity": _non_negative_int(
                    row.get("event_fill_quantity"),
                    f"{path}:{line_number}:event_fill_quantity",
                ),
                "order_status": order_status,
                "fill_price": _finite_float(
                    row.get("fill_price"),
                    f"{path}:{line_number}:fill_price",
                ),
                "commission": _finite_float(
                    row.get("commission"),
                    f"{path}:{line_number}:commission",
                ),
                "tax": _finite_float(
                    row.get("tax"), f"{path}:{line_number}:tax"
                ),
                "message": str(row.get("message") or ""),
            }
            if parsed["sequence"] <= 0 or parsed["requested_quantity"] <= 0:
                raise EngineValidationError(
                    f"{path}:{line_number}:sequence and requested_quantity must be > 0"
                )
            if parsed["cumulative_filled_quantity"] > parsed["requested_quantity"]:
                raise EngineValidationError(
                    f"{path}:{line_number}:cumulative fill exceeds requested quantity"
                )
            if any(parsed[field] < 0 for field in ("fill_price", "commission", "tax")):
                raise EngineValidationError(
                    f"{path}:{line_number}:money fields must be >= 0"
                )
            if parsed["event_fill_quantity"] > 0 and parsed["fill_price"] <= 0:
                raise EngineValidationError(
                    f"{path}:{line_number}:positive fill requires fill_price > 0"
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
    order_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    event_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    signal_rows: Optional[Sequence[Mapping[str, Any]]] = None,
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
    if (order_rows is None) != (event_rows is None):
        raise EngineValidationError(
            "order_rows and event_rows must be supplied together"
        )
    normalized_scope = _validated_scope(
        validation_scope, "validation_scope"
    )
    if (
        normalized_scope["input"] == "independent_policy"
        and signal_rows is None
    ):
        raise EngineValidationError(
            "independent_policy candidates must contain signal_rows"
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
    if signal_rows is not None:
        _write_csv(
            destination / "signals.csv",
            (
                {
                    "signal_date": str(row["signal_date"]),
                    "execution_date": str(row["execution_date"]),
                    "status": row["status"],
                    "estimated_turnover": (
                        f"{float(row['estimated_turnover']):.10f}"
                    ),
                    "reason": row["reason"],
                    "decision_id": row.get("decision_id", ""),
                    "strategy_kind": row.get("strategy_kind", ""),
                    "target_weights": json.dumps(
                        row.get("target_weights", {}),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "decision_evidence": json.dumps(
                        row.get("decision_evidence", {}),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "diagnostics": json.dumps(
                        row.get("diagnostics", []),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "orders": json.dumps(
                        row.get("orders", []),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
                for row in signal_rows
            ),
            SIGNAL_FIELDS,
        )
        read_signal_rows(destination / "signals.csv")
    if order_rows is not None and event_rows is not None:
        _write_csv(
            destination / "orders.csv",
            (
                {
                    **{field: row[field] for field in ORDER_FIELDS},
                    **{
                        field: f"{float(row[field]):.8f}"
                        for field in ("avg_fill_price", "commission", "tax")
                    },
                }
                for row in order_rows
            ),
            ORDER_FIELDS,
        )
        _write_csv(
            destination / "events.csv",
            (
                {
                    **{field: row[field] for field in EVENT_FIELDS},
                    **{
                        field: f"{float(row[field]):.8f}"
                        for field in ("fill_price", "commission", "tax")
                    },
                }
                for row in event_rows
            ),
            EVENT_FIELDS,
        )
    files = {
        name: file_sha256(destination / name)
        for name in ("metrics.json", "nav.csv", "trades.csv")
    }
    if order_rows is not None:
        files.update(
            {
                name: file_sha256(destination / name)
                for name in ("orders.csv", "events.csv")
            }
        )
    if signal_rows is not None:
        files["signals.csv"] = file_sha256(destination / "signals.csv")
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


def _validate_order_lifecycle(
    order_rows: Sequence[Mapping[str, Any]],
    event_rows: Sequence[Mapping[str, Any]],
    trade_rows: Sequence[Mapping[str, Any]],
    *,
    money_tolerance: float,
) -> Dict[str, Any]:
    mismatches: List[Dict[str, Any]] = []
    expected_sequences = list(range(1, len(event_rows) + 1))
    actual_sequences = [int(event["sequence"]) for event in event_rows]
    if actual_sequences != expected_sequences:
        mismatches.append(
            {
                "field": "global_event_sequence",
                "expected": expected_sequences[:20],
                "actual": actual_sequences[:20],
            }
        )
    event_times = [
        datetime.fromisoformat(str(event["event_time"])) for event in event_rows
    ]
    if any(
        current < previous
        for previous, current in zip(event_times, event_times[1:])
    ):
        mismatches.append({"field": "global_event_time_order"})

    seen_trade_ids = set()
    for event in event_rows:
        if event["event_type"] != "trade":
            continue
        trade_id = str(event["trade_id"])
        if trade_id in seen_trade_ids:
            mismatches.append(
                {"field": "duplicate_trade_id", "trade_id": trade_id}
            )
        seen_trade_ids.add(trade_id)

    orders_by_id = {str(row["order_id"]): row for row in order_rows}
    events_by_order: Dict[str, List[Mapping[str, Any]]] = {
        order_id: [] for order_id in orders_by_id
    }
    for event in event_rows:
        order_id = str(event["order_id"])
        order = orders_by_id.get(order_id)
        if order is None:
            mismatches.append(
                {"field": "orphan_event", "order_id": order_id}
            )
            continue
        events_by_order[order_id].append(event)
        for field in ("symbol", "side", "requested_quantity"):
            if event[field] != order[field]:
                mismatches.append(
                    {
                        "order_id": order_id,
                        "field": f"event_{field}",
                        "expected": order[field],
                        "actual": event[field],
                    }
                )

    candidate_trades_by_key: Dict[tuple, Mapping[str, Any]] = {}
    for trade in trade_rows:
        key = (
            trade["signal_date"],
            trade["execution_date"],
            trade["symbol"],
            trade["side"],
            trade["requested_quantity"],
        )
        if key in candidate_trades_by_key:
            mismatches.append(
                {"field": "duplicate_candidate_trade", "key": list(key)}
            )
        candidate_trades_by_key[key] = trade

    partial_order_count = 0
    rejected_order_count = 0
    cancelled_order_count = 0
    for order_id, order in orders_by_id.items():
        events = events_by_order[order_id]
        if not events:
            mismatches.append(
                {"order_id": order_id, "field": "missing_events"}
            )
            continue
        if len(events) != order["event_count"]:
            mismatches.append(
                {
                    "order_id": order_id,
                    "field": "event_count",
                    "expected": order["event_count"],
                    "actual": len(events),
                }
            )
        if events[0]["event_type"] != "order_pending_new" or events[0][
            "order_status"
        ] != "PENDING_NEW":
            mismatches.append(
                {
                    "order_id": order_id,
                    "field": "initial_event",
                    "actual": {
                        "event_type": events[0]["event_type"],
                        "order_status": events[0]["order_status"],
                    },
                }
            )

        creation_passed = False
        final_seen = False
        cumulative_fill = 0
        trade_count = 0
        weighted_fill_value = 0.0
        commission = 0.0
        tax = 0.0
        for event_index, event in enumerate(events):
            event_type = str(event["event_type"])
            status = str(event["order_status"])
            if str(event["event_time"])[:10] != order["execution_date"]:
                mismatches.append(
                    {
                        "order_id": order_id,
                        "field": "event_execution_date",
                        "expected": order["execution_date"],
                        "actual": str(event["event_time"])[:10],
                    }
                )
            if final_seen:
                mismatches.append(
                    {
                        "order_id": order_id,
                        "field": "event_after_final_status",
                        "event_index": event_index,
                    }
                )
            if event_type != "trade":
                if event["event_fill_quantity"] != 0:
                    mismatches.append(
                        {
                            "order_id": order_id,
                            "field": "non_trade_event_fill_quantity",
                            "event_index": event_index,
                            "actual": event["event_fill_quantity"],
                        }
                    )
                for field in ("fill_price", "commission", "tax"):
                    if float(event[field]) != 0.0:
                        mismatches.append(
                            {
                                "order_id": order_id,
                                "field": f"non_trade_{field}",
                                "event_index": event_index,
                                "actual": event[field],
                            }
                        )
                if event["cumulative_filled_quantity"] != cumulative_fill:
                    mismatches.append(
                        {
                            "order_id": order_id,
                            "field": "non_trade_cumulative_filled_quantity",
                            "event_index": event_index,
                            "expected": cumulative_fill,
                            "actual": event["cumulative_filled_quantity"],
                        }
                    )
            if event_type == "order_pending_new":
                if event_index != 0 or status != "PENDING_NEW":
                    mismatches.append(
                        {
                            "order_id": order_id,
                            "field": "pending_new_transition",
                            "event_index": event_index,
                        }
                    )
            elif event_type == "order_creation_pass":
                if event_index != 1 or creation_passed or status != "ACTIVE":
                    mismatches.append(
                        {
                            "order_id": order_id,
                            "field": "creation_pass_transition",
                            "event_index": event_index,
                        }
                    )
                creation_passed = True
            elif event_type == "order_creation_reject":
                if event_index != 1 or creation_passed or status != "REJECTED":
                    mismatches.append(
                        {
                            "order_id": order_id,
                            "field": "creation_reject_transition",
                            "event_index": event_index,
                        }
                    )
                final_seen = True
            elif event_type == "trade":
                event_fill = int(event["event_fill_quantity"])
                next_cumulative_fill = cumulative_fill + event_fill
                expected_status = (
                    "FILLED"
                    if next_cumulative_fill == order["requested_quantity"]
                    else "ACTIVE"
                )
                if (
                    not creation_passed
                    or event_fill <= 0
                    or status != expected_status
                ):
                    mismatches.append(
                        {
                            "order_id": order_id,
                            "field": "trade_transition",
                            "event_index": event_index,
                            "event_fill_quantity": event_fill,
                            "expected_status": expected_status,
                            "status": status,
                        }
                    )
                trade_count += 1
                cumulative_fill = next_cumulative_fill
                weighted_fill_value += event_fill * float(event["fill_price"])
                commission += float(event["commission"])
                tax += float(event["tax"])
                if cumulative_fill != event["cumulative_filled_quantity"]:
                    mismatches.append(
                        {
                            "order_id": order_id,
                            "field": "cumulative_filled_quantity",
                            "expected": cumulative_fill,
                            "actual": event["cumulative_filled_quantity"],
                        }
                    )
                if status == "FILLED":
                    if cumulative_fill != order["requested_quantity"]:
                        mismatches.append(
                            {
                                "order_id": order_id,
                                "field": "filled_status_quantity",
                                "expected": order["requested_quantity"],
                                "actual": cumulative_fill,
                            }
                        )
                    final_seen = True
            elif event_type in {
                "order_cancellation_pass",
                "order_unsolicited_update",
            }:
                valid_cancel = (
                    status == "CANCELLED"
                    and cumulative_fill < order["requested_quantity"]
                )
                valid_unsolicited_reject = (
                    event_type == "order_unsolicited_update"
                    and status == "REJECTED"
                    and cumulative_fill == 0
                )
                if not creation_passed or not (
                    valid_cancel or valid_unsolicited_reject
                ):
                    mismatches.append(
                        {
                            "order_id": order_id,
                            "field": "final_update_status",
                            "creation_passed": creation_passed,
                            "filled_quantity": cumulative_fill,
                            "actual_status": status,
                        }
                    )
                final_seen = True

        final_event_status = str(events[-1]["order_status"])
        if not final_seen or final_event_status != order["final_status"]:
            mismatches.append(
                {
                    "order_id": order_id,
                    "field": "final_status",
                    "expected": order["final_status"],
                    "actual": final_event_status,
                }
            )
        if order["final_status"] == "FILLED" and cumulative_fill != order[
            "requested_quantity"
        ]:
            mismatches.append(
                {
                    "order_id": order_id,
                    "field": "filled_order_quantity",
                    "expected": order["requested_quantity"],
                    "actual": cumulative_fill,
                }
            )
        if order["final_status"] == "CANCELLED" and cumulative_fill >= order[
            "requested_quantity"
        ]:
            mismatches.append(
                {
                    "order_id": order_id,
                    "field": "cancelled_order_quantity",
                    "requested": order["requested_quantity"],
                    "actual": cumulative_fill,
                }
            )
        if order["final_status"] == "REJECTED" and cumulative_fill != 0:
            mismatches.append(
                {
                    "order_id": order_id,
                    "field": "rejected_order_quantity",
                    "actual": cumulative_fill,
                }
            )
        for field, expected, actual in (
            ("filled_quantity", order["filled_quantity"], cumulative_fill),
            ("trade_count", order["trade_count"], trade_count),
        ):
            if expected != actual:
                mismatches.append(
                    {
                        "order_id": order_id,
                        "field": field,
                        "expected": expected,
                        "actual": actual,
                    }
                )
        expected_avg_price = (
            weighted_fill_value / cumulative_fill if cumulative_fill else 0.0
        )
        for field, expected, actual in (
            ("avg_fill_price", order["avg_fill_price"], expected_avg_price),
            ("commission", order["commission"], commission),
            ("tax", order["tax"], tax),
        ):
            if abs(float(expected) - float(actual)) > money_tolerance:
                mismatches.append(
                    {
                        "order_id": order_id,
                        "field": field,
                        "expected": expected,
                        "actual": actual,
                    }
                )

        trade_key = (
            order["signal_date"],
            order["execution_date"],
            order["symbol"],
            order["side"],
            order["requested_quantity"],
        )
        candidate_trade = candidate_trades_by_key.pop(trade_key, None)
        if candidate_trade is None:
            mismatches.append(
                {
                    "order_id": order_id,
                    "field": "missing_candidate_trade",
                }
            )
        else:
            expected_trade_status = (
                "filled"
                if order["final_status"] == "FILLED"
                and cumulative_fill == order["requested_quantity"]
                else "partial"
                if cumulative_fill > 0
                else "rejected"
            )
            for field, expected, actual in (
                (
                    "filled_quantity",
                    cumulative_fill,
                    candidate_trade["filled_quantity"],
                ),
                ("status", expected_trade_status, candidate_trade["status"]),
            ):
                if expected != actual:
                    mismatches.append(
                        {
                            "order_id": order_id,
                            "field": f"candidate_trade_{field}",
                            "expected": expected,
                            "actual": actual,
                        }
                    )
            candidate_money_fields = [
                ("commission", commission, candidate_trade["commission"]),
                ("tax", tax, candidate_trade["tax"]),
            ]
            if cumulative_fill > 0:
                candidate_money_fields.insert(
                    0,
                    (
                        "fill_price",
                        expected_avg_price,
                        candidate_trade["fill_price"],
                    ),
                )
            for field, expected, actual in candidate_money_fields:
                if abs(float(expected) - float(actual)) > money_tolerance:
                    mismatches.append(
                        {
                            "order_id": order_id,
                            "field": f"candidate_trade_{field}",
                            "expected": expected,
                            "actual": actual,
                        }
                    )

        if 0 < cumulative_fill < order["requested_quantity"]:
            partial_order_count += 1
        if order["final_status"] == "REJECTED":
            rejected_order_count += 1
        if order["final_status"] == "CANCELLED":
            cancelled_order_count += 1

    for key in candidate_trades_by_key:
        mismatches.append(
            {"field": "orphan_candidate_trade", "key": list(key)}
        )
    return {
        "passed": not mismatches,
        "details": {
            "order_count": len(order_rows),
            "event_count": len(event_rows),
            "partial_order_count": partial_order_count,
            "rejected_order_count": rejected_order_count,
            "cancelled_order_count": cancelled_order_count,
            "mismatches": _differences_limited(mismatches),
        },
    }


def _validate_policy_signals(
    reference_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    *,
    ratio_tolerance: float,
) -> Dict[str, Any]:
    mismatches: List[Dict[str, Any]] = []
    if len(reference_rows) != len(candidate_rows):
        mismatches.append(
            {
                "field": "signal_count",
                "expected": len(reference_rows),
                "actual": len(candidate_rows),
            }
        )
    exact_fields = (
        "signal_date",
        "execution_date",
        "status",
        "reason",
        "decision_id",
        "strategy_kind",
        "target_weights",
        "decision_evidence",
        "diagnostics",
    )
    exact_order_fields = (
        "signal_date",
        "execution_date",
        "symbol",
        "side",
        "quantity",
        "reason",
    )
    for signal_index, (reference, candidate) in enumerate(
        zip(reference_rows, candidate_rows)
    ):
        for field in exact_fields:
            if reference[field] != candidate[field]:
                mismatches.append(
                    {
                        "signal_index": signal_index,
                        "field": field,
                        "expected": reference[field],
                        "actual": candidate[field],
                    }
                )
        turnover_difference = abs(
            float(reference["estimated_turnover"])
            - float(candidate["estimated_turnover"])
        )
        if turnover_difference > ratio_tolerance:
            mismatches.append(
                {
                    "signal_index": signal_index,
                    "field": "estimated_turnover",
                    "expected": reference["estimated_turnover"],
                    "actual": candidate["estimated_turnover"],
                    "difference": turnover_difference,
                }
            )
        reference_orders = reference["orders"]
        candidate_orders = candidate["orders"]
        if len(reference_orders) != len(candidate_orders):
            mismatches.append(
                {
                    "signal_index": signal_index,
                    "field": "order_count",
                    "expected": len(reference_orders),
                    "actual": len(candidate_orders),
                }
            )
        for order_index, (reference_order, candidate_order) in enumerate(
            zip(reference_orders, candidate_orders)
        ):
            for field in exact_order_fields:
                if reference_order[field] != candidate_order[field]:
                    mismatches.append(
                        {
                            "signal_index": signal_index,
                            "order_index": order_index,
                            "field": f"order_{field}",
                            "expected": reference_order[field],
                            "actual": candidate_order[field],
                        }
                    )
            price_difference = abs(
                float(reference_order["signal_price"])
                - float(candidate_order["signal_price"])
            )
            if price_difference > 1e-8:
                mismatches.append(
                    {
                        "signal_index": signal_index,
                        "order_index": order_index,
                        "field": "order_signal_price",
                        "expected": reference_order["signal_price"],
                        "actual": candidate_order["signal_price"],
                        "difference": price_difference,
                    }
                )
    return {
        "passed": not mismatches,
        "details": {
            "reference_signal_count": len(reference_rows),
            "candidate_signal_count": len(candidate_rows),
            "reference_decision_count": sum(
                1 for row in reference_rows if row["decision_id"]
            ),
            "candidate_decision_count": sum(
                1 for row in candidate_rows if row["decision_id"]
            ),
            "candidate_accepted_count": sum(
                1 for row in candidate_rows if row["status"] == "accepted"
            ),
            "mismatches": _differences_limited(mismatches),
        },
    }


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

    base_candidate_files = {"metrics.json", "nav.csv", "trades.csv"}
    lifecycle_candidate_files = {"orders.csv", "events.csv"}
    decision_candidate_files = {"signals.csv"}
    declared_file_names = set(declared_files)
    lifecycle_present = lifecycle_candidate_files.issubset(declared_file_names)
    decisions_present = decision_candidate_files.issubset(declared_file_names)
    valid_candidate_file_sets = {
        frozenset(base_candidate_files),
        frozenset(base_candidate_files | lifecycle_candidate_files),
        frozenset(base_candidate_files | decision_candidate_files),
        frozenset(
            base_candidate_files
            | lifecycle_candidate_files
            | decision_candidate_files
        ),
    }
    file_mismatches: List[Dict[str, Any]] = []
    if frozenset(declared_file_names) not in valid_candidate_file_sets:
        file_mismatches.append(
            {
                "field": "file_set",
                "expected": sorted(
                    sorted(value) for value in valid_candidate_file_sets
                ),
                "actual": sorted(str(name) for name in declared_files),
            }
        )
    if validation_scope["input"] == "independent_policy" and not decisions_present:
        file_mismatches.append(
            {
                "field": "signals.csv",
                "expected": "required for independent_policy",
                "actual": "missing",
            }
        )
    allowed_candidate_files = (
        base_candidate_files
        | lifecycle_candidate_files
        | decision_candidate_files
    )
    for name in sorted(allowed_candidate_files & declared_file_names):
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
    decision_summary: Dict[str, Any] = {"present": False}
    if decisions_present:
        reference_signals = read_signal_rows(
            reference_directory / "signals.csv"
        )
        candidate_signals = read_signal_rows(
            candidate_directory / "signals.csv"
        )
        decision_result = _validate_policy_signals(
            reference_signals,
            candidate_signals,
            ratio_tolerance=ratio_tolerance,
        )
        decision_summary = {
            "present": True,
            **decision_result["details"],
        }
        checks.append(
            _check(
                "policy_decisions",
                decision_result["passed"],
                decision_result["details"],
            )
        )
    lifecycle_summary: Dict[str, Any] = {"present": False}
    if lifecycle_present:
        order_rows = read_order_rows(candidate_directory / "orders.csv")
        event_rows = read_event_rows(candidate_directory / "events.csv")
        lifecycle_result = _validate_order_lifecycle(
            order_rows,
            event_rows,
            candidate_trades,
            money_tolerance=money_tolerance,
        )
        lifecycle_summary = {
            "present": True,
            **lifecycle_result["details"],
        }
        checks.append(
            _check(
                "order_lifecycle",
                lifecycle_result["passed"],
                lifecycle_result["details"],
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
            "policy_decisions": decision_summary,
            "order_lifecycle": lifecycle_summary,
        },
        "investment_validity_established": False,
        "automatic_execution_allowed": False,
    }
    report["report_sha256"] = _canonical_sha256(report)
    return report


def write_reconciliation_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, report)
