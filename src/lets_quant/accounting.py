from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Mapping, MutableMapping, Optional

from .models import (
    AccountingRecord,
    CorporateAction,
    LedgerEntry,
    MarketData,
    TradeRecord,
)


class AccountingError(ValueError):
    """Raised when postings cannot be applied or reconciled exactly enough."""


class AccountingLedger:
    def __init__(self) -> None:
        self._entries: List[LedgerEntry] = []

    @property
    def entries(self) -> List[LedgerEntry]:
        return list(self._entries)

    def _record(
        self,
        *,
        trading_date: date,
        event_type: str,
        symbol: Optional[str],
        quantity_delta: int,
        cash_delta: float,
        expense: float,
        reference_id: str,
        description: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> LedgerEntry:
        sequence = len(self._entries) + 1
        identity_payload = {
            "sequence": sequence,
            "trading_date": trading_date.isoformat(),
            "event_type": event_type,
            "symbol": symbol,
            "quantity_delta": quantity_delta,
            "cash_delta": cash_delta,
            "expense": expense,
            "reference_id": reference_id,
            "description": description,
            "metadata": dict(metadata or {}),
        }
        entry_id = hashlib.sha256(
            json.dumps(
                identity_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        entry = LedgerEntry(
            entry_id=entry_id,
            sequence=sequence,
            trading_date=trading_date,
            event_type=event_type,
            symbol=symbol,
            quantity_delta=quantity_delta,
            cash_delta=cash_delta,
            expense=expense,
            reference_id=reference_id,
            description=description,
            metadata=dict(metadata or {}),
        )
        self._entries.append(entry)
        return entry

    def record_initial_cash(self, trading_date: date, cash: float) -> None:
        if not math.isfinite(cash) or cash <= 0:
            raise AccountingError("initial ledger cash must be > 0")
        self._record(
            trading_date=trading_date,
            event_type="initial_cash",
            symbol=None,
            quantity_delta=0,
            cash_delta=cash,
            expense=0.0,
            reference_id="portfolio_initial_cash",
            description="opening research portfolio cash",
        )

    def record_initial_positions(
        self, trading_date: date, positions: Mapping[str, int]
    ) -> None:
        for symbol, quantity in sorted(positions.items()):
            if not isinstance(symbol, str) or not symbol.strip():
                raise AccountingError(
                    "initial ledger position symbol must not be empty"
                )
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity < 0
            ):
                raise AccountingError(
                    f"initial ledger position for {symbol} must be an integer >= 0"
                )
            if quantity == 0:
                continue
            normalized_symbol = symbol.strip().upper()
            self._record(
                trading_date=trading_date,
                event_type="initial_position",
                symbol=normalized_symbol,
                quantity_delta=quantity,
                cash_delta=0.0,
                expense=0.0,
                reference_id=f"portfolio_initial_position:{normalized_symbol}",
                description="opening research portfolio position",
            )

    def record_trade(self, trade: TradeRecord, trade_index: int) -> None:
        if trade.filled_quantity <= 0:
            return
        reference_id = (
            f"trade:{trade_index}:{trade.execution_date.isoformat()}:"
            f"{trade.symbol}:{trade.side}"
        )
        quantity_delta = (
            trade.filled_quantity if trade.side == "BUY" else -trade.filled_quantity
        )
        cash_delta = (
            -trade.gross_notional
            if trade.side == "BUY"
            else trade.gross_notional
        )
        self._record(
            trading_date=trade.execution_date,
            event_type="trade_principal",
            symbol=trade.symbol,
            quantity_delta=quantity_delta,
            cash_delta=cash_delta,
            expense=0.0,
            reference_id=reference_id,
            description=f"{trade.side.lower()} filled quantity at fill price",
            metadata={
                "filled_quantity": trade.filled_quantity,
                "fill_price": trade.fill_price,
                "market_price": trade.market_price,
            },
        )
        if trade.commission > 0:
            self._record(
                trading_date=trade.execution_date,
                event_type="commission",
                symbol=trade.symbol,
                quantity_delta=0,
                cash_delta=-trade.commission,
                expense=trade.commission,
                reference_id=reference_id,
                description="simulated broker commission",
            )
        if trade.tax > 0:
            self._record(
                trading_date=trade.execution_date,
                event_type="sell_tax",
                symbol=trade.symbol,
                quantity_delta=0,
                cash_delta=-trade.tax,
                expense=trade.tax,
                reference_id=reference_id,
                description="simulated sell-side transaction tax",
            )
        if trade.slippage_cost > 0:
            self._record(
                trading_date=trade.execution_date,
                event_type="slippage_attribution",
                symbol=trade.symbol,
                quantity_delta=0,
                cash_delta=0.0,
                expense=trade.slippage_cost,
                reference_id=reference_id,
                description="cost already embedded in fill price",
            )

    def apply_corporate_actions(
        self,
        market: MarketData,
        trading_date: date,
        cash: float,
        positions: MutableMapping[str, int],
    ) -> float:
        actions = sorted(
            market.corporate_actions_on(trading_date),
            key=lambda action: (action.symbol, action.event_type),
        )
        by_symbol: Dict[str, List[CorporateAction]] = defaultdict(list)
        for action in actions:
            by_symbol[action.symbol].append(action)
        if market.price_adjustment == "none":
            ambiguous = sorted(
                symbol
                for symbol, symbol_actions in by_symbol.items()
                if len(symbol_actions) > 1
            )
            if ambiguous:
                raise AccountingError(
                    "multiple same-day unadjusted corporate actions require "
                    "an explicit ordering policy: "
                    + ", ".join(ambiguous)
                )

        for action in actions:
            reference_id = (
                f"corporate_action:{action.ex_date.isoformat()}:"
                f"{action.symbol}:{action.event_type}"
            )
            metadata = {
                "source_event_type": action.event_type,
                "cash_amount": action.cash_amount,
                "ratio": action.ratio,
                "price_adjustment": market.price_adjustment,
            }
            if action.event_type not in {
                "cash_dividend",
                "split",
                "reverse_split",
            }:
                raise AccountingError(
                    f"unsupported corporate action type: {action.event_type}"
                )
            if (
                action.event_type == "split"
                and (action.ratio is None or action.ratio <= 1)
            ):
                raise AccountingError("split requires ratio > 1")
            if (
                action.event_type == "reverse_split"
                and (action.ratio is None or action.ratio >= 1)
            ):
                raise AccountingError("reverse_split requires ratio < 1")
            if market.price_adjustment != "none":
                self._record(
                    trading_date=trading_date,
                    event_type="corporate_action_embedded",
                    symbol=action.symbol,
                    quantity_delta=0,
                    cash_delta=0.0,
                    expense=0.0,
                    reference_id=reference_id,
                    description="event is embedded in adjusted price history",
                    metadata=metadata,
                )
                continue

            current_quantity = positions.get(action.symbol, 0)
            if action.event_type == "cash_dividend":
                if (
                    action.cash_amount is None
                    or not math.isfinite(action.cash_amount)
                    or action.cash_amount < 0
                ):
                    raise AccountingError(
                        "cash dividend requires a finite cash_amount >= 0"
                    )
                cash_delta = current_quantity * action.cash_amount
                cash += cash_delta
                self._record(
                    trading_date=trading_date,
                    event_type="cash_dividend",
                    symbol=action.symbol,
                    quantity_delta=0,
                    cash_delta=cash_delta,
                    expense=0.0,
                    reference_id=reference_id,
                    description="cash dividend credited per held share",
                    metadata=metadata,
                )
                continue

            if (
                action.ratio is None
                or not math.isfinite(action.ratio)
                or action.ratio <= 0
            ):
                raise AccountingError("split action is missing a positive ratio")
            raw_quantity = current_quantity * action.ratio
            resulting_quantity = round(raw_quantity)
            if abs(raw_quantity - resulting_quantity) > 1e-9:
                raise AccountingError(
                    "corporate action creates fractional shares without a "
                    f"cash-in-lieu policy: {action.symbol}"
                )
            quantity_delta = resulting_quantity - current_quantity
            positions[action.symbol] = resulting_quantity
            self._record(
                trading_date=trading_date,
                event_type=action.event_type,
                symbol=action.symbol,
                quantity_delta=quantity_delta,
                cash_delta=0.0,
                expense=0.0,
                reference_id=reference_id,
                description="position quantity adjusted by corporate action",
                metadata=metadata,
            )
        return cash

    def reconcile(
        self,
        *,
        trading_date: date,
        cash: float,
        positions: Mapping[str, int],
        prices: Mapping[str, float],
        nav: float,
        tolerance: float = 1e-7,
    ) -> AccountingRecord:
        applicable = [
            entry
            for entry in self._entries
            if entry.trading_date <= trading_date
        ]
        expected_cash = sum(entry.cash_delta for entry in applicable)
        expected_positions: Dict[str, int] = defaultdict(int)
        for entry in applicable:
            if entry.symbol is not None:
                expected_positions[entry.symbol] += entry.quantity_delta

        all_symbols = sorted(set(positions) | set(expected_positions))
        normalized_positions = {
            symbol: positions.get(symbol, 0) for symbol in all_symbols
        }
        normalized_expected = {
            symbol: expected_positions.get(symbol, 0) for symbol in all_symbols
        }
        position_errors = {
            symbol: normalized_positions[symbol] - normalized_expected[symbol]
            for symbol in all_symbols
            if normalized_positions[symbol] != normalized_expected[symbol]
        }
        missing_prices = sorted(
            symbol
            for symbol, quantity in normalized_expected.items()
            if quantity != 0 and symbol not in prices
        )
        if missing_prices:
            raise AccountingError(
                "cannot reconcile positions without prices: "
                + ", ".join(missing_prices)
            )
        market_value = sum(
            quantity * prices[symbol]
            for symbol, quantity in normalized_positions.items()
            if quantity != 0
        )
        expected_market_value = sum(
            quantity * prices[symbol]
            for symbol, quantity in normalized_expected.items()
            if quantity != 0
        )
        expected_nav = expected_cash + expected_market_value
        cash_error = cash - expected_cash
        nav_error = nav - expected_nav
        status = (
            "pass"
            if abs(cash_error) <= tolerance
            and abs(nav_error) <= tolerance
            and not position_errors
            else "fail"
        )
        return AccountingRecord(
            trading_date=trading_date,
            status=status,
            ledger_entry_count=len(applicable),
            cash=cash,
            expected_cash=expected_cash,
            market_value=market_value,
            expected_market_value=expected_market_value,
            nav=nav,
            expected_nav=expected_nav,
            cash_error=cash_error,
            nav_error=nav_error,
            positions=normalized_positions,
            expected_positions=normalized_expected,
            position_errors=position_errors,
        )
