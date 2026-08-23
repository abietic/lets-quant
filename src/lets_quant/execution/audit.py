from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .paper import PaperExchange, PaperOrder, TERMINAL_STATUSES


PAPER_AUDIT_INPUT_SCHEMA_VERSION = 1
PAPER_AUDIT_REPORT_SCHEMA_VERSION = 1
ACTIVE_ORDER_STATUSES = {"SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"}
TASK_STATUSES = {"ok", "failed", "timeout"}
EXTERNAL_SOURCE_KINDS = {"fixture", "broker"}
ORDER_STATUSES = ACTIVE_ORDER_STATUSES | TERMINAL_STATUSES


class PaperAuditError(ValueError):
    """Raised when an operational audit input violates its contract."""


@dataclass(frozen=True)
class AuditThresholds:
    max_quote_age_seconds: int
    max_task_age_seconds: int
    max_account_age_seconds: int
    max_open_order_age_seconds: int
    max_fill_latency_seconds: int
    max_abs_slippage_bps: float
    max_fee_deviation: float
    cash_tolerance: float


@dataclass(frozen=True)
class QuoteObservation:
    symbol: str
    price: float
    observed_at: datetime


@dataclass(frozen=True)
class TaskCheck:
    task_id: str
    status: str
    observed_at: datetime
    details: str


@dataclass(frozen=True)
class RiskState:
    frozen: bool
    reasons: Tuple[str, ...]


@dataclass(frozen=True)
class OrderExpectation:
    client_order_id: str
    decision_id: str
    symbol: str
    side: str
    expected_order_quantity: int
    expected_fill_quantity: int
    expected_average_fill_price: Optional[float]
    expected_fees: float
    expected_terminal_status: Optional[str]
    expected_fill_by: datetime


@dataclass(frozen=True)
class ExternalOrderSnapshot:
    client_order_id: str
    status: str
    filled_quantity: int
    venue_order_id: Optional[str]


@dataclass(frozen=True)
class ExternalAccountSnapshot:
    source: str
    source_kind: str
    observed_at: datetime
    cash: float
    positions: Dict[str, int]
    orders: Tuple[ExternalOrderSnapshot, ...]


@dataclass(frozen=True)
class PaperAuditInput:
    as_of: datetime
    thresholds: AuditThresholds
    required_tasks: Tuple[str, ...]
    quotes: Tuple[QuoteObservation, ...]
    order_expectations: Tuple[OrderExpectation, ...]
    task_checks: Tuple[TaskCheck, ...]
    risk_state: RiskState
    external_account: Optional[ExternalAccountSnapshot]
    input_sha256: str


@dataclass(frozen=True)
class AuditAlert:
    alert_id: str
    code: str
    severity: str
    subject: str
    message: str
    details: Dict[str, Any]


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _expect_keys(
    value: Mapping[str, Any],
    *,
    required: Sequence[str],
    allowed: Sequence[str],
    path: str,
) -> None:
    missing = sorted(set(required) - set(value))
    unknown = sorted(set(value) - set(allowed))
    if missing:
        raise PaperAuditError(f"{path} is missing fields: {', '.join(missing)}")
    if unknown:
        raise PaperAuditError(f"{path} has unknown fields: {', '.join(unknown)}")


def _object(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise PaperAuditError(f"{path} must be a JSON object")
    return dict(value)


def _array(value: Any, path: str) -> List[Any]:
    if not isinstance(value, list):
        raise PaperAuditError(f"{path} must be a JSON array")
    return list(value)


def _non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaperAuditError(f"{path} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, path: str) -> Optional[str]:
    if value is None:
        return None
    return _non_empty_string(value, path)


def _non_negative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PaperAuditError(f"{path} must be an integer >= 0")
    return value


def _positive_int(value: Any, path: str) -> int:
    result = _non_negative_int(value, path)
    if result == 0:
        raise PaperAuditError(f"{path} must be > 0")
    return result


def _non_negative_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaperAuditError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise PaperAuditError(f"{path} must be finite and >= 0")
    return result


def _positive_number(value: Any, path: str) -> float:
    result = _non_negative_number(value, path)
    if result == 0:
        raise PaperAuditError(f"{path} must be > 0")
    return result


def _timestamp(value: Any, path: str) -> datetime:
    if not isinstance(value, str):
        raise PaperAuditError(f"{path} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperAuditError(f"{path} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperAuditError(f"{path} must include a timezone")
    return parsed


def _string_list(value: Any, path: str, *, allow_empty: bool) -> Tuple[str, ...]:
    raw = _array(value, path)
    values = tuple(
        _non_empty_string(item, f"{path}[{index}]")
        for index, item in enumerate(raw)
    )
    if not allow_empty and not values:
        raise PaperAuditError(f"{path} must not be empty")
    if len(set(values)) != len(values):
        raise PaperAuditError(f"{path} must not contain duplicates")
    return values


def _positions(value: Any, path: str) -> Dict[str, int]:
    raw = _object(value, path)
    normalized: Dict[str, int] = {}
    for raw_symbol, raw_quantity in raw.items():
        symbol = _non_empty_string(raw_symbol, f"{path} symbol").upper()
        quantity = _non_negative_int(raw_quantity, f"{path}.{symbol}")
        if symbol in normalized:
            raise PaperAuditError(f"{path} has duplicate normalized symbol {symbol}")
        if quantity:
            normalized[symbol] = quantity
    return dict(sorted(normalized.items()))


def _parse_thresholds(value: Any) -> AuditThresholds:
    raw = _object(value, "thresholds")
    fields = (
        "max_quote_age_seconds",
        "max_task_age_seconds",
        "max_account_age_seconds",
        "max_open_order_age_seconds",
        "max_fill_latency_seconds",
        "max_abs_slippage_bps",
        "max_fee_deviation",
        "cash_tolerance",
    )
    _expect_keys(raw, required=fields, allowed=fields, path="thresholds")
    return AuditThresholds(
        max_quote_age_seconds=_positive_int(
            raw["max_quote_age_seconds"], "thresholds.max_quote_age_seconds"
        ),
        max_task_age_seconds=_positive_int(
            raw["max_task_age_seconds"], "thresholds.max_task_age_seconds"
        ),
        max_account_age_seconds=_positive_int(
            raw["max_account_age_seconds"],
            "thresholds.max_account_age_seconds",
        ),
        max_open_order_age_seconds=_positive_int(
            raw["max_open_order_age_seconds"],
            "thresholds.max_open_order_age_seconds",
        ),
        max_fill_latency_seconds=_positive_int(
            raw["max_fill_latency_seconds"],
            "thresholds.max_fill_latency_seconds",
        ),
        max_abs_slippage_bps=_non_negative_number(
            raw["max_abs_slippage_bps"],
            "thresholds.max_abs_slippage_bps",
        ),
        max_fee_deviation=_non_negative_number(
            raw["max_fee_deviation"], "thresholds.max_fee_deviation"
        ),
        cash_tolerance=_non_negative_number(
            raw["cash_tolerance"], "thresholds.cash_tolerance"
        ),
    )


def _parse_quotes(value: Any, as_of: datetime) -> Tuple[QuoteObservation, ...]:
    quotes: List[QuoteObservation] = []
    seen = set()
    for index, item in enumerate(_array(value, "quotes")):
        path = f"quotes[{index}]"
        raw = _object(item, path)
        fields = ("symbol", "price", "observed_at")
        _expect_keys(raw, required=fields, allowed=fields, path=path)
        symbol = _non_empty_string(raw["symbol"], f"{path}.symbol").upper()
        if symbol in seen:
            raise PaperAuditError(f"quotes has duplicate symbol {symbol}")
        observed_at = _timestamp(raw["observed_at"], f"{path}.observed_at")
        if observed_at > as_of:
            raise PaperAuditError(f"{path}.observed_at is after audit as_of")
        seen.add(symbol)
        quotes.append(
            QuoteObservation(
                symbol=symbol,
                price=_positive_number(raw["price"], f"{path}.price"),
                observed_at=observed_at,
            )
        )
    return tuple(sorted(quotes, key=lambda item: item.symbol))


def _parse_expectations(value: Any) -> Tuple[OrderExpectation, ...]:
    expectations: List[OrderExpectation] = []
    seen = set()
    fields = (
        "client_order_id",
        "decision_id",
        "symbol",
        "side",
        "expected_order_quantity",
        "expected_fill_quantity",
        "expected_average_fill_price",
        "expected_fees",
        "expected_terminal_status",
        "expected_fill_by",
    )
    for index, item in enumerate(_array(value, "order_expectations")):
        path = f"order_expectations[{index}]"
        raw = _object(item, path)
        _expect_keys(raw, required=fields, allowed=fields, path=path)
        order_id = _non_empty_string(
            raw["client_order_id"], f"{path}.client_order_id"
        )
        if order_id in seen:
            raise PaperAuditError(
                f"order_expectations has duplicate client_order_id {order_id}"
            )
        side = _non_empty_string(raw["side"], f"{path}.side").upper()
        if side not in {"BUY", "SELL"}:
            raise PaperAuditError(f"{path}.side must be BUY or SELL")
        order_quantity = _positive_int(
            raw["expected_order_quantity"],
            f"{path}.expected_order_quantity",
        )
        fill_quantity = _non_negative_int(
            raw["expected_fill_quantity"],
            f"{path}.expected_fill_quantity",
        )
        if fill_quantity > order_quantity:
            raise PaperAuditError(
                f"{path}.expected_fill_quantity exceeds order quantity"
            )
        fill_price_raw = raw["expected_average_fill_price"]
        if fill_quantity > 0:
            fill_price = _positive_number(
                fill_price_raw, f"{path}.expected_average_fill_price"
            )
        else:
            if fill_price_raw is not None:
                raise PaperAuditError(
                    f"{path}.expected_average_fill_price must be null when "
                    "expected_fill_quantity is zero"
                )
            fill_price = None
        terminal_status = _optional_string(
            raw["expected_terminal_status"],
            f"{path}.expected_terminal_status",
        )
        if terminal_status is not None:
            terminal_status = terminal_status.upper()
            if terminal_status not in TERMINAL_STATUSES:
                raise PaperAuditError(
                    f"{path}.expected_terminal_status must be a terminal status"
                )
        if terminal_status == "FILLED" and fill_quantity != order_quantity:
            raise PaperAuditError(
                f"{path} expects FILLED but not the full order quantity"
            )
        if terminal_status == "REJECTED" and fill_quantity != 0:
            raise PaperAuditError(
                f"{path} expects REJECTED with a nonzero fill quantity"
            )
        seen.add(order_id)
        expectations.append(
            OrderExpectation(
                client_order_id=order_id,
                decision_id=_non_empty_string(
                    raw["decision_id"], f"{path}.decision_id"
                ),
                symbol=_non_empty_string(
                    raw["symbol"], f"{path}.symbol"
                ).upper(),
                side=side,
                expected_order_quantity=order_quantity,
                expected_fill_quantity=fill_quantity,
                expected_average_fill_price=fill_price,
                expected_fees=_non_negative_number(
                    raw["expected_fees"], f"{path}.expected_fees"
                ),
                expected_terminal_status=terminal_status,
                expected_fill_by=_timestamp(
                    raw["expected_fill_by"], f"{path}.expected_fill_by"
                ),
            )
        )
    return tuple(sorted(expectations, key=lambda item: item.client_order_id))


def _parse_task_checks(
    value: Any, as_of: datetime
) -> Tuple[TaskCheck, ...]:
    checks: List[TaskCheck] = []
    seen = set()
    fields = ("task_id", "status", "observed_at", "details")
    for index, item in enumerate(_array(value, "task_checks")):
        path = f"task_checks[{index}]"
        raw = _object(item, path)
        _expect_keys(raw, required=fields, allowed=fields, path=path)
        task_id = _non_empty_string(raw["task_id"], f"{path}.task_id")
        if task_id in seen:
            raise PaperAuditError(f"task_checks has duplicate task_id {task_id}")
        status = _non_empty_string(raw["status"], f"{path}.status").lower()
        if status not in TASK_STATUSES:
            raise PaperAuditError(
                f"{path}.status must be one of {', '.join(sorted(TASK_STATUSES))}"
            )
        observed_at = _timestamp(raw["observed_at"], f"{path}.observed_at")
        if observed_at > as_of:
            raise PaperAuditError(f"{path}.observed_at is after audit as_of")
        seen.add(task_id)
        checks.append(
            TaskCheck(
                task_id=task_id,
                status=status,
                observed_at=observed_at,
                details=(
                    raw["details"].strip()
                    if isinstance(raw["details"], str)
                    else _non_empty_string(raw["details"], f"{path}.details")
                ),
            )
        )
    return tuple(sorted(checks, key=lambda item: item.task_id))


def _parse_risk_state(value: Any) -> RiskState:
    raw = _object(value, "risk_state")
    fields = ("frozen", "reasons")
    _expect_keys(raw, required=fields, allowed=fields, path="risk_state")
    if not isinstance(raw["frozen"], bool):
        raise PaperAuditError("risk_state.frozen must be a boolean")
    reasons = _string_list(raw["reasons"], "risk_state.reasons", allow_empty=True)
    if raw["frozen"] and not reasons:
        raise PaperAuditError("a frozen risk state must include at least one reason")
    if not raw["frozen"] and reasons:
        raise PaperAuditError("an unfrozen risk state must not include reasons")
    return RiskState(frozen=raw["frozen"], reasons=reasons)


def _parse_external_orders(value: Any) -> Tuple[ExternalOrderSnapshot, ...]:
    orders: List[ExternalOrderSnapshot] = []
    seen = set()
    fields = (
        "client_order_id",
        "status",
        "filled_quantity",
        "venue_order_id",
    )
    for index, item in enumerate(_array(value, "external_account.orders")):
        path = f"external_account.orders[{index}]"
        raw = _object(item, path)
        _expect_keys(raw, required=fields, allowed=fields, path=path)
        order_id = _non_empty_string(
            raw["client_order_id"], f"{path}.client_order_id"
        )
        if order_id in seen:
            raise PaperAuditError(
                f"external_account.orders has duplicate client_order_id {order_id}"
            )
        status = _non_empty_string(raw["status"], f"{path}.status").upper()
        if status not in ORDER_STATUSES:
            raise PaperAuditError(f"{path}.status is not supported")
        seen.add(order_id)
        orders.append(
            ExternalOrderSnapshot(
                client_order_id=order_id,
                status=status,
                filled_quantity=_non_negative_int(
                    raw["filled_quantity"], f"{path}.filled_quantity"
                ),
                venue_order_id=_optional_string(
                    raw["venue_order_id"], f"{path}.venue_order_id"
                ),
            )
        )
    return tuple(sorted(orders, key=lambda item: item.client_order_id))


def _parse_external_account(
    value: Any, as_of: datetime
) -> Optional[ExternalAccountSnapshot]:
    if value is None:
        return None
    raw = _object(value, "external_account")
    fields = (
        "source",
        "source_kind",
        "observed_at",
        "cash",
        "positions",
        "orders",
    )
    _expect_keys(raw, required=fields, allowed=fields, path="external_account")
    source_kind = _non_empty_string(
        raw["source_kind"], "external_account.source_kind"
    ).lower()
    if source_kind not in EXTERNAL_SOURCE_KINDS:
        raise PaperAuditError(
            "external_account.source_kind must be fixture or broker"
        )
    observed_at = _timestamp(
        raw["observed_at"], "external_account.observed_at"
    )
    if observed_at > as_of:
        raise PaperAuditError(
            "external_account.observed_at is after audit as_of"
        )
    return ExternalAccountSnapshot(
        source=_non_empty_string(raw["source"], "external_account.source"),
        source_kind=source_kind,
        observed_at=observed_at,
        cash=_non_negative_number(raw["cash"], "external_account.cash"),
        positions=_positions(raw["positions"], "external_account.positions"),
        orders=_parse_external_orders(raw["orders"]),
    )


def load_paper_audit_input(path: Path) -> PaperAuditInput:
    try:
        raw_value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PaperAuditError(f"paper audit input not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PaperAuditError(
            f"invalid paper audit JSON at line {exc.lineno}"
        ) from exc
    raw = _object(raw_value, "paper audit input")
    fields = (
        "schema_version",
        "as_of",
        "thresholds",
        "required_tasks",
        "quotes",
        "order_expectations",
        "task_checks",
        "risk_state",
        "external_account",
    )
    _expect_keys(raw, required=fields, allowed=fields, path="paper audit input")
    if raw["schema_version"] != PAPER_AUDIT_INPUT_SCHEMA_VERSION:
        raise PaperAuditError("unsupported paper audit input schema")
    as_of = _timestamp(raw["as_of"], "as_of")
    return PaperAuditInput(
        as_of=as_of,
        thresholds=_parse_thresholds(raw["thresholds"]),
        required_tasks=_string_list(
            raw["required_tasks"], "required_tasks", allow_empty=False
        ),
        quotes=_parse_quotes(raw["quotes"], as_of),
        order_expectations=_parse_expectations(raw["order_expectations"]),
        task_checks=_parse_task_checks(raw["task_checks"], as_of),
        risk_state=_parse_risk_state(raw["risk_state"]),
        external_account=_parse_external_account(raw["external_account"], as_of),
        input_sha256=_canonical_sha256(raw),
    )


def _age_seconds(as_of: datetime, observed_at: datetime) -> float:
    return max(0.0, (as_of - observed_at).total_seconds())


def _paper_timestamp(value: str, path: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperAuditError(f"{path} contains an invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperAuditError(f"{path} timestamp must include a timezone")
    return parsed


def _alert(
    code: str,
    severity: str,
    subject: str,
    message: str,
    details: Optional[Mapping[str, Any]] = None,
) -> AuditAlert:
    alert_details = dict(details or {})
    identity = {
        "code": code,
        "severity": severity,
        "subject": subject,
        "details": alert_details,
    }
    return AuditAlert(
        alert_id=_canonical_sha256(identity)[:20],
        code=code,
        severity=severity,
        subject=subject,
        message=message,
        details=alert_details,
    )


def _actual_fees(order: PaperOrder) -> float:
    return sum(fill.commission + fill.tax for fill in order.fills)


def _validate_state_as_of(
    exchange: PaperExchange, audit_input: PaperAuditInput
) -> None:
    future_events = [
        event.event_id
        for event in exchange.events
        if _paper_timestamp(
            event.occurred_at, f"event {event.event_id}.occurred_at"
        )
        > audit_input.as_of
    ]
    if future_events:
        raise PaperAuditError(
            "paper state contains events after audit as_of: "
            + ", ".join(sorted(future_events))
        )


def _execution_quality(
    exchange: PaperExchange,
    audit_input: PaperAuditInput,
    alerts: List[AuditAlert],
) -> List[Dict[str, Any]]:
    expectations = {
        item.client_order_id: item for item in audit_input.order_expectations
    }
    for order_id in sorted(set(exchange.orders) - set(expectations)):
        alerts.append(
            _alert(
                "missing_order_expectation",
                "critical",
                order_id,
                "paper order is not linked to an expected execution record",
            )
        )
    for order_id in sorted(set(expectations) - set(exchange.orders)):
        alerts.append(
            _alert(
                "expected_order_missing",
                "critical",
                order_id,
                "expected order does not exist in the paper state",
                {"decision_id": expectations[order_id].decision_id},
            )
        )

    rows: List[Dict[str, Any]] = []
    for order_id in sorted(set(expectations) & set(exchange.orders)):
        expectation = expectations[order_id]
        order = exchange.orders[order_id]
        submitted_at = _paper_timestamp(
            order.submitted_at, f"order {order_id}.submitted_at"
        )
        contract_mismatches = {}
        if order.symbol != expectation.symbol:
            contract_mismatches["symbol"] = {
                "expected": expectation.symbol,
                "actual": order.symbol,
            }
        if order.side != expectation.side:
            contract_mismatches["side"] = {
                "expected": expectation.side,
                "actual": order.side,
            }
        if order.quantity != expectation.expected_order_quantity:
            contract_mismatches["quantity"] = {
                "expected": expectation.expected_order_quantity,
                "actual": order.quantity,
            }
        if contract_mismatches:
            alerts.append(
                _alert(
                    "order_contract_mismatch",
                    "critical",
                    order_id,
                    "paper order differs from its expected execution contract",
                    contract_mismatches,
                )
            )

        if expectation.expected_fill_by < submitted_at:
            alerts.append(
                _alert(
                    "fill_deadline_before_submission",
                    "critical",
                    order_id,
                    "expected fill deadline is earlier than order submission",
                    {
                        "submitted_at": submitted_at.isoformat(),
                        "expected_fill_by": (
                            expectation.expected_fill_by.isoformat()
                        ),
                    },
                )
            )

        deadline_passed = audit_input.as_of >= expectation.expected_fill_by
        is_terminal = order.status in TERMINAL_STATUSES
        quantity_deviation = (
            order.filled_quantity - expectation.expected_fill_quantity
        )
        if quantity_deviation and (is_terminal or deadline_passed):
            alerts.append(
                _alert(
                    "fill_quantity_deviation",
                    "critical",
                    order_id,
                    "actual filled quantity differs from expectation",
                    {
                        "expected": expectation.expected_fill_quantity,
                        "actual": order.filled_quantity,
                        "deadline_passed": deadline_passed,
                    },
                )
            )

        expected_terminal = expectation.expected_terminal_status
        if expected_terminal is not None:
            if is_terminal and order.status != expected_terminal:
                alerts.append(
                    _alert(
                        "terminal_status_mismatch",
                        "critical",
                        order_id,
                        "paper order reached an unexpected terminal status",
                        {"expected": expected_terminal, "actual": order.status},
                    )
                )
            elif not is_terminal and deadline_passed:
                alerts.append(
                    _alert(
                        "terminal_status_overdue",
                        "critical",
                        order_id,
                        (
                            "paper order did not reach its expected terminal "
                            "status in time"
                        ),
                        {"expected": expected_terminal, "actual": order.status},
                    )
                )

        actual_price = (
            order.average_fill_price if order.filled_quantity > 0 else None
        )
        adverse_slippage_bps: Optional[float] = None
        expected_price = expectation.expected_average_fill_price
        if actual_price is not None and expected_price is not None:
            raw_deviation = (actual_price / expected_price - 1.0) * 10_000
            adverse_slippage_bps = (
                raw_deviation if order.side == "BUY" else -raw_deviation
            )
            if (
                abs(adverse_slippage_bps)
                > audit_input.thresholds.max_abs_slippage_bps
            ):
                alerts.append(
                    _alert(
                        "fill_price_deviation",
                        "critical",
                        order_id,
                        "actual average fill price exceeds the deviation threshold",
                        {
                            "expected": expected_price,
                            "actual": actual_price,
                            "adverse_slippage_bps": adverse_slippage_bps,
                            "threshold_bps": (
                                audit_input.thresholds.max_abs_slippage_bps
                            ),
                        },
                    )
                )

        actual_fees = _actual_fees(order)
        fee_deviation = actual_fees - expectation.expected_fees
        if (is_terminal or deadline_passed) and (
            abs(fee_deviation) > audit_input.thresholds.max_fee_deviation
        ):
            alerts.append(
                _alert(
                    "fill_fee_deviation",
                    "critical",
                    order_id,
                    "actual execution fees exceed the deviation threshold",
                    {
                        "expected": expectation.expected_fees,
                        "actual": actual_fees,
                        "deviation": fee_deviation,
                        "threshold": audit_input.thresholds.max_fee_deviation,
                    },
                )
            )

        fill_latency_seconds: Optional[float] = None
        if order.fills:
            last_fill_at = max(
                _paper_timestamp(
                    fill.occurred_at, f"order {order_id}.fill.occurred_at"
                )
                for fill in order.fills
            )
            fill_latency_seconds = (last_fill_at - submitted_at).total_seconds()
            if fill_latency_seconds < 0:
                raise PaperAuditError(
                    f"order {order_id} has a fill before its submission"
                )
            if (
                fill_latency_seconds
                > audit_input.thresholds.max_fill_latency_seconds
            ):
                alerts.append(
                    _alert(
                        "fill_latency_exceeded",
                        "critical",
                        order_id,
                        "paper fill latency exceeds the configured threshold",
                        {
                            "actual_seconds": fill_latency_seconds,
                            "threshold_seconds": (
                                audit_input.thresholds.max_fill_latency_seconds
                            ),
                        },
                    )
                )

        rows.append(
            {
                "client_order_id": order_id,
                "decision_id": expectation.decision_id,
                "symbol": order.symbol,
                "side": order.side,
                "status": order.status,
                "expected_order_quantity": expectation.expected_order_quantity,
                "expected_fill_quantity": expectation.expected_fill_quantity,
                "actual_filled_quantity": order.filled_quantity,
                "quantity_deviation": quantity_deviation,
                "expected_average_fill_price": expected_price,
                "actual_average_fill_price": actual_price,
                "adverse_slippage_bps": adverse_slippage_bps,
                "expected_fees": expectation.expected_fees,
                "actual_fees": actual_fees,
                "fee_deviation": fee_deviation,
                "expected_terminal_status": expected_terminal,
                "expected_fill_by": expectation.expected_fill_by.isoformat(),
                "deadline_passed": deadline_passed,
                "fill_latency_seconds": fill_latency_seconds,
            }
        )
    return rows


def _audit_tasks(
    audit_input: PaperAuditInput, alerts: List[AuditAlert]
) -> List[Dict[str, Any]]:
    checks = {item.task_id: item for item in audit_input.task_checks}
    for task_id in sorted(set(audit_input.required_tasks) - set(checks)):
        alerts.append(
            _alert(
                "required_task_missing",
                "critical",
                task_id,
                "required operational task has no health observation",
            )
        )
    rows = []
    for task_id, check in sorted(checks.items()):
        age_seconds = _age_seconds(audit_input.as_of, check.observed_at)
        if check.status != "ok":
            alerts.append(
                _alert(
                    "task_failed",
                    "critical",
                    task_id,
                    "operational task is failed or timed out",
                    {"status": check.status, "details": check.details},
                )
            )
        if age_seconds > audit_input.thresholds.max_task_age_seconds:
            alerts.append(
                _alert(
                    "task_observation_stale",
                    "critical",
                    task_id,
                    "operational task health observation is stale",
                    {
                        "age_seconds": age_seconds,
                        "threshold_seconds": (
                            audit_input.thresholds.max_task_age_seconds
                        ),
                    },
                )
            )
        rows.append(
            {
                "task_id": task_id,
                "status": check.status,
                "observed_at": check.observed_at.isoformat(),
                "age_seconds": age_seconds,
                "details": check.details,
                "required": task_id in audit_input.required_tasks,
            }
        )
    return rows


def _audit_open_orders_and_quotes(
    exchange: PaperExchange,
    audit_input: PaperAuditInput,
    alerts: List[AuditAlert],
) -> List[Dict[str, Any]]:
    quotes = {item.symbol: item for item in audit_input.quotes}
    rows = []
    for order in sorted(
        exchange.orders.values(), key=lambda item: item.client_order_id
    ):
        if order.status not in ACTIVE_ORDER_STATUSES:
            continue
        submitted_at = _paper_timestamp(
            order.submitted_at, f"order {order.client_order_id}.submitted_at"
        )
        if submitted_at > audit_input.as_of:
            raise PaperAuditError(
                f"order {order.client_order_id} was submitted after audit as_of"
            )
        order_age = _age_seconds(audit_input.as_of, submitted_at)
        if order_age > audit_input.thresholds.max_open_order_age_seconds:
            alerts.append(
                _alert(
                    "open_order_stale",
                    "critical",
                    order.client_order_id,
                    "open paper order exceeds the configured age threshold",
                    {
                        "age_seconds": order_age,
                        "threshold_seconds": (
                            audit_input.thresholds.max_open_order_age_seconds
                        ),
                    },
                )
            )
        quote = quotes.get(order.symbol)
        quote_age: Optional[float] = None
        if quote is None:
            alerts.append(
                _alert(
                    "missing_quote",
                    "critical",
                    order.symbol,
                    "active paper order has no current quote observation",
                    {"client_order_id": order.client_order_id},
                )
            )
        else:
            quote_age = _age_seconds(audit_input.as_of, quote.observed_at)
            if quote_age > audit_input.thresholds.max_quote_age_seconds:
                alerts.append(
                    _alert(
                        "stale_quote",
                        "critical",
                        order.symbol,
                        "quote observation for an active order is stale",
                        {
                            "client_order_id": order.client_order_id,
                            "age_seconds": quote_age,
                            "threshold_seconds": (
                                audit_input.thresholds.max_quote_age_seconds
                            ),
                        },
                    )
                )
        rows.append(
            {
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "status": order.status,
                "submitted_at": submitted_at.isoformat(),
                "age_seconds": order_age,
                "quote_observed_at": (
                    quote.observed_at.isoformat() if quote is not None else None
                ),
                "quote_price": quote.price if quote is not None else None,
                "quote_age_seconds": quote_age,
            }
        )
    return rows


def _audit_external_account(
    exchange: PaperExchange,
    audit_input: PaperAuditInput,
    alerts: List[AuditAlert],
) -> Dict[str, Any]:
    external = audit_input.external_account
    if external is None:
        alerts.append(
            _alert(
                "external_account_missing",
                "warning",
                "external_account",
                "no external account snapshot was supplied for recovery comparison",
            )
        )
        return {
            "status": "missing",
            "source": None,
            "source_kind": None,
            "observed_at": None,
            "age_seconds": None,
            "cash_error": None,
            "position_errors": {},
            "order_errors": {},
        }

    if external.source_kind == "fixture":
        alerts.append(
            _alert(
                "external_account_fixture",
                "warning",
                external.source,
                (
                    "fixture account snapshot validates the contract but is "
                    "not broker truth"
                ),
            )
        )
    age_seconds = _age_seconds(audit_input.as_of, external.observed_at)
    if age_seconds > audit_input.thresholds.max_account_age_seconds:
        alerts.append(
            _alert(
                "external_account_stale",
                "critical",
                external.source,
                "external account snapshot is stale",
                {
                    "age_seconds": age_seconds,
                    "threshold_seconds": (
                        audit_input.thresholds.max_account_age_seconds
                    ),
                },
            )
        )

    cash_error = exchange.cash - external.cash
    if abs(cash_error) > audit_input.thresholds.cash_tolerance:
        alerts.append(
            _alert(
                "account_cash_mismatch",
                "critical",
                external.source,
                "local paper cash differs from the external account snapshot",
                {
                    "local": exchange.cash,
                    "external": external.cash,
                    "error": cash_error,
                    "tolerance": audit_input.thresholds.cash_tolerance,
                },
            )
        )

    local_positions = {
        symbol: quantity
        for symbol, quantity in exchange.positions.items()
        if quantity
    }
    position_errors = {
        symbol: local_positions.get(symbol, 0)
        - external.positions.get(symbol, 0)
        for symbol in sorted(set(local_positions) | set(external.positions))
        if local_positions.get(symbol, 0) != external.positions.get(symbol, 0)
    }
    if position_errors:
        alerts.append(
            _alert(
                "account_position_mismatch",
                "critical",
                external.source,
                "local paper positions differ from the external account snapshot",
                {"position_errors": position_errors},
            )
        )

    external_orders = {
        order.client_order_id: order for order in external.orders
    }
    order_errors: Dict[str, Any] = {}
    for order_id in sorted(set(exchange.orders) | set(external_orders)):
        local_order = exchange.orders.get(order_id)
        external_order = external_orders.get(order_id)
        if local_order is None:
            order_errors[order_id] = {"error": "external_order_not_local"}
            continue
        if external_order is None:
            order_errors[order_id] = {"error": "local_order_not_external"}
            continue
        mismatches = {}
        if local_order.status != external_order.status:
            mismatches["status"] = {
                "local": local_order.status,
                "external": external_order.status,
            }
        if local_order.filled_quantity != external_order.filled_quantity:
            mismatches["filled_quantity"] = {
                "local": local_order.filled_quantity,
                "external": external_order.filled_quantity,
            }
        if local_order.venue_order_id != external_order.venue_order_id:
            mismatches["venue_order_id"] = {
                "local": local_order.venue_order_id,
                "external": external_order.venue_order_id,
            }
        if mismatches:
            order_errors[order_id] = mismatches
    if order_errors:
        alerts.append(
            _alert(
                "account_order_mismatch",
                "critical",
                external.source,
                "local paper orders differ from the external account snapshot",
                {"order_errors": order_errors},
            )
        )

    has_mismatch = (
        abs(cash_error) > audit_input.thresholds.cash_tolerance
        or bool(position_errors)
        or bool(order_errors)
        or age_seconds > audit_input.thresholds.max_account_age_seconds
    )
    return {
        "status": "mismatch" if has_mismatch else "match",
        "source": external.source,
        "source_kind": external.source_kind,
        "observed_at": external.observed_at.isoformat(),
        "age_seconds": age_seconds,
        "cash_error": cash_error,
        "position_errors": position_errors,
        "order_errors": order_errors,
    }


def audit_paper_exchange(
    exchange: PaperExchange, audit_input: PaperAuditInput
) -> Dict[str, Any]:
    _validate_state_as_of(exchange, audit_input)
    exchange_snapshot = exchange.to_snapshot()
    alerts: List[AuditAlert] = []
    if audit_input.risk_state.frozen:
        alerts.append(
            _alert(
                "risk_frozen",
                "critical",
                "risk_state",
                "risk state is frozen; execution must remain blocked",
                {"reasons": list(audit_input.risk_state.reasons)},
            )
        )

    task_health = _audit_tasks(audit_input, alerts)
    open_orders = _audit_open_orders_and_quotes(exchange, audit_input, alerts)
    execution_quality = _execution_quality(exchange, audit_input, alerts)
    account_reconciliation = _audit_external_account(
        exchange, audit_input, alerts
    )
    alerts.sort(
        key=lambda item: (
            0 if item.severity == "critical" else 1,
            item.code,
            item.subject,
            item.alert_id,
        )
    )
    severity_counts = {
        severity: sum(1 for item in alerts if item.severity == severity)
        for severity in ("critical", "warning")
    }
    if severity_counts["critical"]:
        status = "blocked"
    elif severity_counts["warning"]:
        status = "review_required"
    else:
        status = "pass"

    payload = {
        "schema_version": PAPER_AUDIT_REPORT_SCHEMA_VERSION,
        "artifact_type": "offline_paper_operational_audit",
        "as_of": audit_input.as_of.isoformat(),
        "status": status,
        "manual_review_required": status != "pass",
        "automatic_execution_allowed": False,
        "paper_state_sha256": exchange_snapshot["state_sha256"],
        "audit_input_sha256": audit_input.input_sha256,
        "thresholds": asdict(audit_input.thresholds),
        "risk_state": {
            "frozen": audit_input.risk_state.frozen,
            "reasons": list(audit_input.risk_state.reasons),
        },
        "summary": {
            "alert_count": len(alerts),
            "critical_alert_count": severity_counts["critical"],
            "warning_alert_count": severity_counts["warning"],
            "open_order_count": len(open_orders),
            "execution_record_count": len(execution_quality),
            "required_task_count": len(audit_input.required_tasks),
            "external_account_status": account_reconciliation["status"],
        },
        "alerts": [asdict(item) for item in alerts],
        "task_health": task_health,
        "open_orders": open_orders,
        "execution_quality": execution_quality,
        "account_reconciliation": account_reconciliation,
    }
    return {**payload, "report_sha256": _canonical_sha256(payload)}


def save_paper_audit_report(report: Mapping[str, Any], path: Path) -> None:
    expected_hash = report.get("report_sha256")
    payload = {key: value for key, value in report.items() if key != "report_sha256"}
    if expected_hash != _canonical_sha256(payload):
        raise PaperAuditError("paper audit report checksum is invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
