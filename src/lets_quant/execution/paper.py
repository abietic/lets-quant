from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


PAPER_STATE_SCHEMA_VERSION = 1
TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED"}


class PaperExecutionError(ValueError):
    """Raised when an offline paper event violates execution invariants."""


@dataclass(frozen=True)
class PaperFill:
    sequence: int
    fill_id: str
    quantity: int
    price: float
    commission: float
    tax: float
    occurred_at: str


@dataclass
class PaperOrder:
    client_order_id: str
    symbol: str
    side: str
    quantity: int
    status: str
    submitted_at: str
    venue_order_id: Optional[str] = None
    filled_quantity: int = 0
    average_fill_price: float = 0.0
    rejection_reason: Optional[str] = None
    fills: List[PaperFill] = field(default_factory=list)


@dataclass(frozen=True)
class PaperEvent:
    sequence: int
    event_id: str
    event_type: str
    occurred_at: str
    payload: Dict[str, Any]


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_content_sha256(
    event_id: str,
    event_type: str,
    occurred_at: str,
    payload: Mapping[str, Any],
) -> str:
    canonical_payload = {
        key: value
        for key, value in payload.items()
        if key != "idempotent_noop"
    }
    return _canonical_sha256(
        {
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "payload": canonical_payload,
        }
    )


def _non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaperExecutionError(f"{path} must be a non-empty string")
    return value.strip()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PaperExecutionError("paper event timestamps must include a timezone")
    return value.isoformat()


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PaperExecutionError(f"{path} must be a positive integer")
    return value


def _non_negative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PaperExecutionError(f"{path} must be an integer >= 0")
    return value


def _non_negative_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaperExecutionError(f"{path} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise PaperExecutionError(f"{path} must be finite and >= 0")
    return number


def _positive_number(value: Any, path: str) -> float:
    number = _non_negative_number(value, path)
    if number <= 0:
        raise PaperExecutionError(f"{path} must be > 0")
    return number


class PaperExchange:
    """Deterministic offline order state machine with persistent idempotency."""

    def __init__(
        self,
        *,
        initial_cash: float,
        initial_positions: Optional[Mapping[str, int]] = None,
    ) -> None:
        self.initial_cash = _non_negative_number(initial_cash, "initial_cash")
        self.initial_positions = self._normalize_positions(
            initial_positions or {}
        )
        self.cash = self.initial_cash
        self.positions = dict(self.initial_positions)
        self.orders: Dict[str, PaperOrder] = {}
        self.events: List[PaperEvent] = []
        self._processed_event_hashes: Dict[str, str] = {}

    @staticmethod
    def _normalize_positions(
        positions: Mapping[str, int]
    ) -> Dict[str, int]:
        normalized: Dict[str, int] = {}
        for raw_symbol, quantity in positions.items():
            symbol = str(raw_symbol).strip().upper()
            if not symbol:
                raise PaperExecutionError("position symbols must not be empty")
            if symbol in normalized:
                raise PaperExecutionError(
                    f"duplicate normalized position symbol: {symbol}"
                )
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity < 0
            ):
                raise PaperExecutionError(
                    f"initial position for {symbol} must be an integer >= 0"
                )
            normalized[symbol] = quantity
        return dict(sorted(normalized.items()))

    def _begin_event(
        self,
        *,
        event_id: str,
        event_type: str,
        occurred_at: datetime,
        payload: Mapping[str, Any],
    ) -> Tuple[bool, str, str]:
        normalized_event_id = _non_empty_string(event_id, "event_id")
        timestamp = _timestamp(occurred_at)
        event_hash = _event_content_sha256(
            normalized_event_id, event_type, timestamp, payload
        )
        previous_hash = self._processed_event_hashes.get(normalized_event_id)
        if previous_hash is not None:
            if previous_hash != event_hash:
                raise PaperExecutionError(
                    "event_id was reused with different content: "
                    f"{normalized_event_id}"
                )
            return False, event_hash, timestamp
        return True, event_hash, timestamp

    def _commit_event(
        self,
        *,
        event_id: str,
        event_type: str,
        timestamp: str,
        payload: Mapping[str, Any],
        event_hash: str,
    ) -> None:
        normalized_event_id = _non_empty_string(event_id, "event_id")
        self.events.append(
            PaperEvent(
                sequence=len(self.events) + 1,
                event_id=normalized_event_id,
                event_type=event_type,
                occurred_at=timestamp,
                payload=dict(payload),
            )
        )
        self._processed_event_hashes[normalized_event_id] = event_hash

    def _order(self, client_order_id: str) -> PaperOrder:
        normalized = _non_empty_string(client_order_id, "client_order_id")
        try:
            return self.orders[normalized]
        except KeyError as exc:
            raise PaperExecutionError(
                f"unknown client_order_id: {normalized}"
            ) from exc

    def submit(
        self,
        *,
        event_id: str,
        client_order_id: str,
        symbol: str,
        side: str,
        quantity: int,
        occurred_at: datetime,
    ) -> PaperOrder:
        normalized_order_id = _non_empty_string(
            client_order_id, "client_order_id"
        )
        normalized_symbol = _non_empty_string(symbol, "order symbol").upper()
        normalized_side = _non_empty_string(side, "order side").upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise PaperExecutionError("order side must be BUY or SELL")
        normalized_quantity = _positive_int(quantity, "order quantity")
        payload = {
            "client_order_id": normalized_order_id,
            "symbol": normalized_symbol,
            "side": normalized_side,
            "quantity": normalized_quantity,
        }
        is_new_event, event_hash, timestamp = self._begin_event(
            event_id=event_id,
            event_type="submit",
            occurred_at=occurred_at,
            payload=payload,
        )
        existing = self.orders.get(normalized_order_id)
        if not is_new_event:
            if existing is None:
                raise PaperExecutionError(
                    "processed submit event has no matching order"
                )
            return existing
        if existing is not None:
            if (
                existing.symbol != normalized_symbol
                or existing.side != normalized_side
                or existing.quantity != normalized_quantity
            ):
                raise PaperExecutionError(
                    "client_order_id was reused with different order content: "
                    f"{normalized_order_id}"
                )
            payload["idempotent_noop"] = True
            self._commit_event(
                event_id=event_id,
                event_type="submit",
                timestamp=timestamp,
                payload=payload,
                event_hash=event_hash,
            )
            return existing

        order = PaperOrder(
            client_order_id=normalized_order_id,
            symbol=normalized_symbol,
            side=normalized_side,
            quantity=normalized_quantity,
            status="SUBMITTED",
            submitted_at=timestamp,
        )
        self.orders[normalized_order_id] = order
        self._commit_event(
            event_id=event_id,
            event_type="submit",
            timestamp=timestamp,
            payload=payload,
            event_hash=event_hash,
        )
        return order

    def acknowledge(
        self,
        *,
        event_id: str,
        client_order_id: str,
        venue_order_id: str,
        occurred_at: datetime,
    ) -> PaperOrder:
        normalized_order_id = _non_empty_string(
            client_order_id, "client_order_id"
        )
        normalized_venue_id = _non_empty_string(
            venue_order_id, "venue_order_id"
        )
        payload = {
            "client_order_id": normalized_order_id,
            "venue_order_id": normalized_venue_id,
        }
        is_new_event, event_hash, timestamp = self._begin_event(
            event_id=event_id,
            event_type="acknowledge",
            occurred_at=occurred_at,
            payload=payload,
        )
        order = self._order(normalized_order_id)
        if not is_new_event:
            return order
        venue_owner = next(
            (
                existing_order.client_order_id
                for existing_order in self.orders.values()
                if existing_order.venue_order_id == normalized_venue_id
                and existing_order.client_order_id != normalized_order_id
            ),
            None,
        )
        if venue_owner is not None:
            raise PaperExecutionError(
                "venue_order_id belongs to another order: "
                f"{normalized_venue_id}"
            )
        if order.venue_order_id is not None:
            if order.venue_order_id != normalized_venue_id:
                raise PaperExecutionError(
                    "order was acknowledged with a different venue_order_id"
                )
            payload["idempotent_noop"] = True
        elif order.status != "SUBMITTED":
            raise PaperExecutionError(
                f"cannot acknowledge order in status {order.status}"
            )
        else:
            order.venue_order_id = normalized_venue_id
            order.status = "ACKNOWLEDGED"
        self._commit_event(
            event_id=event_id,
            event_type="acknowledge",
            timestamp=timestamp,
            payload=payload,
            event_hash=event_hash,
        )
        return order

    def fill(
        self,
        *,
        event_id: str,
        client_order_id: str,
        fill_id: str,
        quantity: int,
        price: float,
        occurred_at: datetime,
        commission: float = 0.0,
        tax: float = 0.0,
    ) -> PaperOrder:
        normalized_order_id = _non_empty_string(
            client_order_id, "client_order_id"
        )
        normalized_fill_id = _non_empty_string(fill_id, "fill_id")
        normalized_quantity = _positive_int(quantity, "fill quantity")
        normalized_price = _positive_number(price, "fill price")
        normalized_commission = _non_negative_number(
            commission, "fill commission"
        )
        normalized_tax = _non_negative_number(tax, "fill tax")
        payload = {
            "client_order_id": normalized_order_id,
            "fill_id": normalized_fill_id,
            "quantity": normalized_quantity,
            "price": normalized_price,
            "commission": normalized_commission,
            "tax": normalized_tax,
        }
        is_new_event, event_hash, timestamp = self._begin_event(
            event_id=event_id,
            event_type="fill",
            occurred_at=occurred_at,
            payload=payload,
        )
        order = self._order(normalized_order_id)
        if not is_new_event:
            return order
        existing_fill_match = next(
            (
                (existing_order, existing_fill)
                for existing_order in self.orders.values()
                for existing_fill in existing_order.fills
                if existing_fill.fill_id == normalized_fill_id
            ),
            None,
        )
        if existing_fill_match is not None:
            existing_order, existing_fill = existing_fill_match
            if existing_order.client_order_id != normalized_order_id:
                raise PaperExecutionError(
                    f"fill_id belongs to another order: {normalized_fill_id}"
                )
            expected = {
                "quantity": existing_fill.quantity,
                "price": existing_fill.price,
                "commission": existing_fill.commission,
                "tax": existing_fill.tax,
            }
            if any(payload[key] != value for key, value in expected.items()):
                raise PaperExecutionError(
                    f"fill_id was reused with different content: {normalized_fill_id}"
                )
            payload["idempotent_noop"] = True
            self._commit_event(
                event_id=event_id,
                event_type="fill",
                timestamp=timestamp,
                payload=payload,
                event_hash=event_hash,
            )
            return order
        if order.status not in {"ACKNOWLEDGED", "PARTIALLY_FILLED"}:
            raise PaperExecutionError(f"cannot fill order in status {order.status}")
        if order.filled_quantity + normalized_quantity > order.quantity:
            raise PaperExecutionError("fill quantity exceeds remaining order quantity")

        gross_notional = normalized_quantity * normalized_price
        expenses = normalized_commission + normalized_tax
        if order.side == "BUY":
            required_cash = gross_notional + expenses
            if required_cash > self.cash + 1e-9:
                raise PaperExecutionError("paper fill exceeds available cash")
            self.cash = max(0.0, self.cash - required_cash)
            self.positions[order.symbol] = (
                self.positions.get(order.symbol, 0) + normalized_quantity
            )
        else:
            if expenses > gross_notional:
                raise PaperExecutionError(
                    "sell fill expenses exceed gross notional"
                )
            available_quantity = self.positions.get(order.symbol, 0)
            if normalized_quantity > available_quantity:
                raise PaperExecutionError(
                    "paper fill exceeds available position quantity"
                )
            self.positions[order.symbol] = available_quantity - normalized_quantity
            self.cash += gross_notional - expenses

        previous_notional = order.average_fill_price * order.filled_quantity
        order.filled_quantity += normalized_quantity
        order.average_fill_price = (
            previous_notional + gross_notional
        ) / order.filled_quantity
        order.fills.append(
            PaperFill(
                sequence=sum(len(item.fills) for item in self.orders.values()) + 1,
                fill_id=normalized_fill_id,
                quantity=normalized_quantity,
                price=normalized_price,
                commission=normalized_commission,
                tax=normalized_tax,
                occurred_at=timestamp,
            )
        )
        order.status = (
            "FILLED"
            if order.filled_quantity == order.quantity
            else "PARTIALLY_FILLED"
        )
        self._commit_event(
            event_id=event_id,
            event_type="fill",
            timestamp=timestamp,
            payload=payload,
            event_hash=event_hash,
        )
        self._assert_reconciled()
        return order

    def cancel(
        self,
        *,
        event_id: str,
        client_order_id: str,
        occurred_at: datetime,
    ) -> PaperOrder:
        normalized_order_id = _non_empty_string(
            client_order_id, "client_order_id"
        )
        payload = {"client_order_id": normalized_order_id}
        is_new_event, event_hash, timestamp = self._begin_event(
            event_id=event_id,
            event_type="cancel",
            occurred_at=occurred_at,
            payload=payload,
        )
        order = self._order(normalized_order_id)
        if not is_new_event:
            return order
        if order.status == "CANCELED":
            payload["idempotent_noop"] = True
        elif order.status in {"SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"}:
            order.status = "CANCELED"
        else:
            raise PaperExecutionError(f"cannot cancel order in status {order.status}")
        self._commit_event(
            event_id=event_id,
            event_type="cancel",
            timestamp=timestamp,
            payload=payload,
            event_hash=event_hash,
        )
        return order

    def reject(
        self,
        *,
        event_id: str,
        client_order_id: str,
        reason: str,
        occurred_at: datetime,
    ) -> PaperOrder:
        normalized_order_id = _non_empty_string(
            client_order_id, "client_order_id"
        )
        normalized_reason = _non_empty_string(reason, "rejection reason")
        payload = {
            "client_order_id": normalized_order_id,
            "reason": normalized_reason,
        }
        is_new_event, event_hash, timestamp = self._begin_event(
            event_id=event_id,
            event_type="reject",
            occurred_at=occurred_at,
            payload=payload,
        )
        order = self._order(normalized_order_id)
        if not is_new_event:
            return order
        if order.status == "REJECTED":
            if order.rejection_reason != normalized_reason:
                raise PaperExecutionError(
                    "order was rejected previously for a different reason"
                )
            payload["idempotent_noop"] = True
        elif order.status in {"SUBMITTED", "ACKNOWLEDGED"}:
            order.status = "REJECTED"
            order.rejection_reason = normalized_reason
        else:
            raise PaperExecutionError(f"cannot reject order in status {order.status}")
        self._commit_event(
            event_id=event_id,
            event_type="reject",
            timestamp=timestamp,
            payload=payload,
            event_hash=event_hash,
        )
        return order

    def reconciliation(self) -> Dict[str, Any]:
        expected_cash = self.initial_cash
        expected_positions = dict(self.initial_positions)
        fills = sorted(
            (
                (order, fill)
                for order in self.orders.values()
                for fill in order.fills
            ),
            key=lambda item: item[1].sequence,
        )
        for order, fill in fills:
            notional = fill.quantity * fill.price
            expenses = fill.commission + fill.tax
            if order.side == "BUY":
                expected_cash -= notional + expenses
                expected_positions[order.symbol] = (
                    expected_positions.get(order.symbol, 0) + fill.quantity
                )
            else:
                expected_cash += notional - expenses
                expected_positions[order.symbol] = (
                    expected_positions.get(order.symbol, 0) - fill.quantity
                )
        all_symbols = sorted(set(self.positions) | set(expected_positions))
        position_errors = {
            symbol: self.positions.get(symbol, 0)
            - expected_positions.get(symbol, 0)
            for symbol in all_symbols
            if self.positions.get(symbol, 0)
            != expected_positions.get(symbol, 0)
        }
        cash_error = self.cash - expected_cash
        return {
            "status": (
                "pass"
                if abs(cash_error) <= 1e-7 and not position_errors
                else "fail"
            ),
            "cash": self.cash,
            "expected_cash": expected_cash,
            "cash_error": cash_error,
            "positions": dict(sorted(self.positions.items())),
            "expected_positions": dict(sorted(expected_positions.items())),
            "position_errors": position_errors,
        }

    def _assert_reconciled(self) -> None:
        if self.reconciliation()["status"] != "pass":
            raise PaperExecutionError("paper account reconciliation failed")

    @staticmethod
    def _order_payload(order: PaperOrder) -> Dict[str, Any]:
        return {
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "status": order.status,
            "submitted_at": order.submitted_at,
            "venue_order_id": order.venue_order_id,
            "filled_quantity": order.filled_quantity,
            "average_fill_price": order.average_fill_price,
            "rejection_reason": order.rejection_reason,
            "fills": [asdict(fill) for fill in order.fills],
        }

    def to_snapshot(self) -> Dict[str, Any]:
        self._assert_reconciled()
        payload = {
            "schema_version": PAPER_STATE_SCHEMA_VERSION,
            "artifact_type": "offline_paper_exchange_state",
            "automatic_execution_allowed": False,
            "initial_cash": self.initial_cash,
            "initial_positions": dict(sorted(self.initial_positions.items())),
            "cash": self.cash,
            "positions": dict(sorted(self.positions.items())),
            "orders": [
                self._order_payload(self.orders[order_id])
                for order_id in sorted(self.orders)
            ],
            "events": [asdict(event) for event in self.events],
            "processed_event_hashes": dict(
                sorted(self._processed_event_hashes.items())
            ),
            "reconciliation": self.reconciliation(),
        }
        return {**payload, "state_sha256": _canonical_sha256(payload)}

    def save(self, path: Path) -> None:
        snapshot = self.to_snapshot()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(
                snapshot,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> "PaperExchange":
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PaperExecutionError(f"paper state not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise PaperExecutionError(
                f"invalid paper state JSON at line {exc.lineno}"
            ) from exc
        if not isinstance(snapshot, dict):
            raise PaperExecutionError("paper state must be a JSON object")
        state_hash = snapshot.get("state_sha256")
        payload = {
            key: value for key, value in snapshot.items() if key != "state_sha256"
        }
        if state_hash != _canonical_sha256(payload):
            raise PaperExecutionError("paper state checksum mismatch")
        if (
            payload.get("schema_version") != PAPER_STATE_SCHEMA_VERSION
            or payload.get("artifact_type")
            != "offline_paper_exchange_state"
        ):
            raise PaperExecutionError("unsupported paper state schema")
        if payload.get("automatic_execution_allowed") is not False:
            raise PaperExecutionError("paper state execution boundary is invalid")

        initial_positions = payload.get("initial_positions")
        if not isinstance(initial_positions, dict):
            raise PaperExecutionError("paper state initial_positions are invalid")
        exchange = cls(
            initial_cash=payload.get("initial_cash"),
            initial_positions=initial_positions,
        )
        exchange.cash = _non_negative_number(payload.get("cash"), "state.cash")
        current_positions = payload.get("positions")
        if not isinstance(current_positions, dict):
            raise PaperExecutionError("paper state positions are invalid")
        exchange.positions = exchange._normalize_positions(current_positions)

        orders_raw = payload.get("orders")
        events_raw = payload.get("events")
        hashes_raw = payload.get("processed_event_hashes")
        if not isinstance(orders_raw, list) or not isinstance(events_raw, list):
            raise PaperExecutionError("paper state orders or events are invalid")
        if not isinstance(hashes_raw, dict):
            raise PaperExecutionError("paper state event hashes are invalid")
        for raw in orders_raw:
            order = cls._parse_order_snapshot(raw)
            if order.client_order_id in exchange.orders:
                raise PaperExecutionError("paper state has duplicate order IDs")
            exchange.orders[order.client_order_id] = order
        exchange.events = [cls._parse_event_snapshot(raw) for raw in events_raw]
        if [event.sequence for event in exchange.events] != list(
            range(1, len(exchange.events) + 1)
        ):
            raise PaperExecutionError("paper event sequence is not contiguous")
        if len({event.event_id for event in exchange.events}) != len(
            exchange.events
        ):
            raise PaperExecutionError("paper state has duplicate event IDs")
        exchange._processed_event_hashes = {
            str(event_id): str(event_hash)
            for event_id, event_hash in hashes_raw.items()
        }
        if set(exchange._processed_event_hashes) != {
            event.event_id for event in exchange.events
        }:
            raise PaperExecutionError("paper event hash index is inconsistent")
        for event in exchange.events:
            expected_event_hash = _event_content_sha256(
                event.event_id,
                event.event_type,
                event.occurred_at,
                event.payload,
            )
            if (
                exchange._processed_event_hashes[event.event_id]
                != expected_event_hash
            ):
                raise PaperExecutionError("paper event content hash mismatch")
        fill_sequences = sorted(
            fill.sequence
            for order in exchange.orders.values()
            for fill in order.fills
        )
        if fill_sequences != list(range(1, len(fill_sequences) + 1)):
            raise PaperExecutionError("paper fill sequence is not contiguous")
        exchange._assert_reconciled()
        if exchange.to_snapshot()["state_sha256"] != state_hash:
            raise PaperExecutionError("paper state did not round-trip exactly")
        replayed = cls(
            initial_cash=exchange.initial_cash,
            initial_positions=exchange.initial_positions,
        )
        for event in exchange.events:
            _replay_snapshot_event(replayed, event)
        if replayed.to_snapshot() != snapshot:
            raise PaperExecutionError(
                "paper state does not match replayed event history"
            )
        return replayed

    @staticmethod
    def _parse_order_snapshot(raw: Any) -> PaperOrder:
        if not isinstance(raw, dict):
            raise PaperExecutionError("paper order snapshot must be an object")
        fills_raw = raw.get("fills")
        if not isinstance(fills_raw, list):
            raise PaperExecutionError("paper order fills must be an array")
        fills = []
        for fill_raw in fills_raw:
            if not isinstance(fill_raw, dict):
                raise PaperExecutionError("paper fill snapshot must be an object")
            fills.append(
                PaperFill(
                    sequence=_positive_int(
                        fill_raw.get("sequence"), "fill.sequence"
                    ),
                    fill_id=str(fill_raw.get("fill_id", "")),
                    quantity=_positive_int(
                        fill_raw.get("quantity"), "fill.quantity"
                    ),
                    price=_positive_number(fill_raw.get("price"), "fill.price"),
                    commission=_non_negative_number(
                        fill_raw.get("commission"), "fill.commission"
                    ),
                    tax=_non_negative_number(fill_raw.get("tax"), "fill.tax"),
                    occurred_at=str(fill_raw.get("occurred_at", "")),
                )
            )
        status = str(raw.get("status", ""))
        if status not in {
            "SUBMITTED",
            "ACKNOWLEDGED",
            "PARTIALLY_FILLED",
            *TERMINAL_STATUSES,
        }:
            raise PaperExecutionError(f"invalid paper order status: {status}")
        order = PaperOrder(
            client_order_id=_non_empty_string(
                raw.get("client_order_id"), "order.client_order_id"
            ),
            symbol=_non_empty_string(raw.get("symbol"), "order.symbol").upper(),
            side=_non_empty_string(raw.get("side"), "order.side").upper(),
            quantity=_positive_int(raw.get("quantity"), "order.quantity"),
            status=status,
            submitted_at=str(raw.get("submitted_at", "")),
            venue_order_id=raw.get("venue_order_id"),
            filled_quantity=_non_negative_int(
                raw.get("filled_quantity"), "order.filled_quantity"
            ),
            average_fill_price=_non_negative_number(
                raw.get("average_fill_price"), "order.average_fill_price"
            ),
            rejection_reason=raw.get("rejection_reason"),
            fills=fills,
        )
        if order.side not in {"BUY", "SELL"}:
            raise PaperExecutionError("paper order side is invalid")
        if order.filled_quantity != sum(fill.quantity for fill in fills):
            raise PaperExecutionError("paper order filled quantity is inconsistent")
        if order.filled_quantity > order.quantity:
            raise PaperExecutionError("paper order is overfilled")
        if fills:
            expected_average = sum(
                fill.quantity * fill.price for fill in fills
            ) / order.filled_quantity
            if abs(order.average_fill_price - expected_average) > 1e-12:
                raise PaperExecutionError(
                    "paper order average fill price is inconsistent"
                )
        elif order.average_fill_price != 0:
            raise PaperExecutionError(
                "paper order without fills has a nonzero average price"
            )
        if order.status == "FILLED" and order.filled_quantity != order.quantity:
            raise PaperExecutionError("filled paper order has incomplete fills")
        if order.filled_quantity == order.quantity and order.status != "FILLED":
            raise PaperExecutionError("fully filled paper order has invalid status")
        if (
            0 < order.filled_quantity < order.quantity
            and order.status not in {"PARTIALLY_FILLED", "CANCELED"}
        ):
            raise PaperExecutionError(
                "partially filled paper order has invalid status"
            )
        if (
            order.filled_quantity == 0
            and order.status == "PARTIALLY_FILLED"
        ):
            raise PaperExecutionError("empty paper order cannot be partially filled")
        if order.status == "REJECTED" and order.filled_quantity != 0:
            raise PaperExecutionError("rejected paper order cannot contain fills")
        if (
            order.status in {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED"}
            and not order.venue_order_id
        ):
            raise PaperExecutionError(
                "acknowledged or filled paper order lacks venue_order_id"
            )
        return order

    @staticmethod
    def _parse_event_snapshot(raw: Any) -> PaperEvent:
        if not isinstance(raw, dict) or not isinstance(raw.get("payload"), dict):
            raise PaperExecutionError("paper event snapshot is invalid")
        return PaperEvent(
            sequence=_positive_int(raw.get("sequence"), "event.sequence"),
            event_id=_non_empty_string(raw.get("event_id"), "event.event_id"),
            event_type=_non_empty_string(
                raw.get("event_type"), "event.event_type"
            ),
            occurred_at=_non_empty_string(
                raw.get("occurred_at"), "event.occurred_at"
            ),
            payload=dict(raw["payload"]),
        )


def _parse_event_timestamp(value: Any, line_number: int) -> datetime:
    if not isinstance(value, str):
        raise PaperExecutionError(
            f"paper event line {line_number}: occurred_at must be an ISO timestamp"
        )
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperExecutionError(
            f"paper event line {line_number}: invalid occurred_at"
        ) from exc
    _timestamp(timestamp)
    return timestamp


def _replay_snapshot_event(
    exchange: PaperExchange, event: PaperEvent
) -> None:
    try:
        occurred_at = datetime.fromisoformat(
            event.occurred_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise PaperExecutionError(
            "paper snapshot contains an invalid event timestamp"
        ) from exc
    _timestamp(occurred_at)
    payload = {
        key: value
        for key, value in event.payload.items()
        if key != "idempotent_noop"
    }
    common = {
        "event_id": event.event_id,
        "client_order_id": payload.get("client_order_id"),
        "occurred_at": occurred_at,
    }
    if event.event_type == "submit":
        exchange.submit(
            **common,
            symbol=payload.get("symbol"),
            side=payload.get("side"),
            quantity=payload.get("quantity"),
        )
    elif event.event_type == "acknowledge":
        exchange.acknowledge(
            **common, venue_order_id=payload.get("venue_order_id")
        )
    elif event.event_type == "fill":
        exchange.fill(
            **common,
            fill_id=payload.get("fill_id"),
            quantity=payload.get("quantity"),
            price=payload.get("price"),
            commission=payload.get("commission", 0.0),
            tax=payload.get("tax", 0.0),
        )
    elif event.event_type == "cancel":
        exchange.cancel(**common)
    elif event.event_type == "reject":
        exchange.reject(**common, reason=payload.get("reason"))
    else:
        raise PaperExecutionError(
            f"unsupported paper snapshot event type: {event.event_type}"
        )


def _expect_event_keys(
    event: Mapping[str, Any],
    *,
    required: set[str],
    allowed: set[str],
    line_number: int,
) -> None:
    missing = sorted(required - set(event))
    unknown = sorted(set(event) - allowed)
    if missing:
        raise PaperExecutionError(
            f"paper event line {line_number}: missing keys: {', '.join(missing)}"
        )
    if unknown:
        raise PaperExecutionError(
            f"paper event line {line_number}: unknown keys: {', '.join(unknown)}"
        )


def replay_event_file(exchange: PaperExchange, path: Path) -> int:
    try:
        handle = path.open(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PaperExecutionError(f"paper event file not found: {path}") from exc
    processed = 0
    base = {"event_id", "type", "occurred_at", "client_order_id"}
    with handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise PaperExecutionError(
                    f"paper event line {line_number}: invalid JSON"
                ) from exc
            if not isinstance(event, dict):
                raise PaperExecutionError(
                    f"paper event line {line_number}: event must be an object"
                )
            event_type = event.get("type")
            timestamp = _parse_event_timestamp(
                event.get("occurred_at"), line_number
            )
            common = {
                "event_id": event.get("event_id"),
                "client_order_id": event.get("client_order_id"),
                "occurred_at": timestamp,
            }
            if event_type == "submit":
                allowed = base | {"symbol", "side", "quantity"}
                _expect_event_keys(
                    event,
                    required=allowed,
                    allowed=allowed,
                    line_number=line_number,
                )
                exchange.submit(
                    **common,
                    symbol=event["symbol"],
                    side=event["side"],
                    quantity=event["quantity"],
                )
            elif event_type == "acknowledge":
                allowed = base | {"venue_order_id"}
                _expect_event_keys(
                    event,
                    required=allowed,
                    allowed=allowed,
                    line_number=line_number,
                )
                exchange.acknowledge(
                    **common, venue_order_id=event["venue_order_id"]
                )
            elif event_type == "fill":
                required = base | {"fill_id", "quantity", "price"}
                allowed = required | {"commission", "tax"}
                _expect_event_keys(
                    event,
                    required=required,
                    allowed=allowed,
                    line_number=line_number,
                )
                exchange.fill(
                    **common,
                    fill_id=event["fill_id"],
                    quantity=event["quantity"],
                    price=event["price"],
                    commission=event.get("commission", 0.0),
                    tax=event.get("tax", 0.0),
                )
            elif event_type == "cancel":
                _expect_event_keys(
                    event,
                    required=base,
                    allowed=base,
                    line_number=line_number,
                )
                exchange.cancel(**common)
            elif event_type == "reject":
                allowed = base | {"reason"}
                _expect_event_keys(
                    event,
                    required=allowed,
                    allowed=allowed,
                    line_number=line_number,
                )
                exchange.reject(**common, reason=event["reason"])
            else:
                raise PaperExecutionError(
                    f"paper event line {line_number}: unsupported type {event_type!r}"
                )
            processed += 1
    return processed
