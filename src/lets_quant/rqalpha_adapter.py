from __future__ import annotations

import math
import platform
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .config import load_policy
from .cross_engine import (
    EngineValidationError,
    file_sha256,
    read_nav_rows,
    reconcile_engine_candidate,
    reference_identity,
    summarize_candidate,
    write_engine_candidate,
    write_reconciliation_report,
)
from .data import validate_market_coverage
from .engine_inputs import (
    EngineBar,
    load_frozen_order_intents,
    load_json_object,
    resolve_engine_market_input,
)
from .independent_policy import (
    IndependentPolicyError,
    independent_signal,
)
from .models import CorporateAction, Policy


ADAPTER_VERSION = "4"
SUPPORTED_RQALPHA_VERSION = "6.3.0"
CHINA_TIMEZONE = timezone(timedelta(hours=8))
DECISION_MODES = {"independent_policy", "frozen_orders"}

__config__ = {"priority": 200}


@dataclass
class _RuntimeContext:
    trading_dates: List[str]
    symbols: List[str]
    mapped_symbols: Dict[str, str]
    original_symbols: Dict[str, str]
    market_prices: Dict[str, Dict[str, float]]
    market_bars: Dict[str, Dict[str, EngineBar]]
    tradable_by_date: Dict[str, Dict[str, bool]]
    corporate_actions_by_date: Dict[str, List[CorporateAction]]
    price_adjustment: str
    volumes: Dict[str, Dict[str, int]]
    lot_size: int
    cash_buffer_weight: float
    slippage_fraction: float
    policy: Policy
    decision_mode: str
    execution_delay_trading_days: int
    intents: List[Dict[str, Any]]
    pending_intents: List[Dict[str, Any]]
    signals: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    nav_rows: List[Dict[str, Any]] = field(default_factory=list)
    corporate_action_rows: List[Dict[str, Any]] = field(default_factory=list)
    peak_nav: float = 0.0
    risk_frozen: bool = False
    submitting_intent: Optional[Dict[str, Any]] = None


_RUNTIMES: Dict[str, _RuntimeContext] = {}


def _enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    return str(name if name is not None else value).upper()


def _event_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=CHINA_TIMEZONE)
    return value.isoformat()


def _mapped_symbol(index: int) -> str:
    return f"LQ{index + 1:06d}.XSHG"


def _load_liquidity(
    path: Optional[Path],
    *,
    trading_dates: Sequence[str],
    symbols: Sequence[str],
    default_volumes: Mapping[str, Mapping[str, int]],
) -> Tuple[Dict[str, Dict[str, int]], Optional[str]]:
    if path is None:
        return {
            trading_date: {
                symbol: int(default_volumes[trading_date][symbol])
                for symbol in symbols
            }
            for trading_date in trading_dates
        }, None

    import csv

    try:
        handle = path.open(newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise EngineValidationError(f"file not found: {path}") from exc
    expected_keys = {
        (trading_date, symbol)
        for trading_date in trading_dates
        for symbol in symbols
    }
    values: Dict[Tuple[str, str], int] = {}
    with handle:
        reader = csv.DictReader(handle)
        if set(reader.fieldnames or []) != {"date", "symbol", "volume"}:
            raise EngineValidationError(
                f"{path} must have exactly date, symbol, and volume columns"
            )
        for line_number, row in enumerate(reader, start=2):
            trading_date = str(row.get("date") or "").strip()
            symbol = str(row.get("symbol") or "").strip().upper()
            key = (trading_date, symbol)
            if key not in expected_keys:
                raise EngineValidationError(
                    f"{path}:{line_number}:date/symbol is outside the reference scope"
                )
            if key in values:
                raise EngineValidationError(
                    f"{path}:{line_number}:duplicate date/symbol row"
                )
            raw_volume = str(row.get("volume") or "").strip()
            try:
                volume = int(raw_volume)
            except ValueError as exc:
                raise EngineValidationError(
                    f"{path}:{line_number}:volume must be an integer"
                ) from exc
            if str(volume) != raw_volume or volume < 0:
                raise EngineValidationError(
                    f"{path}:{line_number}:volume must be an integer >= 0"
                )
            values[key] = volume
    missing = sorted(expected_keys - set(values))
    if missing:
        raise EngineValidationError(
            f"{path} is missing liquidity rows: {missing[:5]}"
        )
    return {
        trading_date: {
            symbol: values[(trading_date, symbol)] for symbol in symbols
        }
        for trading_date in trading_dates
    }, file_sha256(path)


class _FrozenDailyDataSource:
    def __init__(self, runtime: _RuntimeContext) -> None:
        import numpy as np
        import pandas as pd
        from rqalpha.const import (
            INSTRUMENT_TYPE,
            MARKET,
            TRADING_CALENDAR_TYPE,
        )
        from rqalpha.interface import ExchangeRate
        from rqalpha.model.instrument import Instrument

        self._np = np
        self._pd = pd
        self._calendar_type = TRADING_CALENDAR_TYPE.CN_STOCK
        self._instrument_type = INSTRUMENT_TYPE.CS
        self._exchange_rate_type = ExchangeRate
        self._runtime = runtime
        first_date = date.fromisoformat(runtime.trading_dates[0])
        last_date = date.fromisoformat(runtime.trading_dates[-1])
        self._calendar = pd.DatetimeIndex(runtime.trading_dates)
        self._instruments: Dict[str, Any] = {}
        self._aliases: Dict[str, Any] = {}
        for symbol in runtime.symbols:
            mapped = runtime.mapped_symbols[symbol]
            instrument = Instrument(
                {
                    "order_book_id": mapped,
                    "symbol": symbol,
                    "round_lot": runtime.lot_size,
                    "listed_date": (first_date - timedelta(days=365)).isoformat(),
                    "de_listed_date": (last_date + timedelta(days=365)).isoformat(),
                    "type": "CS",
                    "exchange": "XSHG",
                    "board_type": "MainBoard",
                    "status": "Active",
                    "special_type": "Normal",
                    "market_tplus": 1,
                },
                market=MARKET.CN,
            )
            self._instruments[mapped] = instrument
            self._aliases[mapped] = instrument
            self._aliases[symbol] = instrument

    @staticmethod
    def _date_key(value: Any) -> str:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return str(value)[:10]

    def get_instruments(
        self,
        id_or_syms: Optional[Iterable[str]] = None,
        types: Optional[Iterable[Any]] = None,
    ) -> Iterable[Any]:
        if id_or_syms is not None:
            seen = set()
            for value in id_or_syms:
                instrument = self._aliases.get(str(value))
                if instrument is not None and instrument not in seen:
                    seen.add(instrument)
                    yield instrument
            return
        allowed = set(types or [self._instrument_type])
        if self._instrument_type in allowed:
            yield from self._instruments.values()

    def get_trading_calendars(self) -> Dict[Any, Any]:
        return {self._calendar_type: self._calendar}

    def available_data_range(self, frequency: str) -> Tuple[date, date]:
        if frequency != "1d":
            raise NotImplementedError("frozen adapter supports daily bars only")
        return (
            date.fromisoformat(self._runtime.trading_dates[0]),
            date.fromisoformat(self._runtime.trading_dates[-1]),
        )

    def _bar(self, mapped_symbol: str, trading_date: str) -> Optional[Dict[str, Any]]:
        original = self._runtime.original_symbols.get(mapped_symbol)
        if original is None:
            return None
        source_bar = self._runtime.market_bars.get(trading_date, {}).get(original)
        if source_bar is None:
            return None
        volume = self._runtime.volumes[trading_date][original]
        return {
            "datetime": int(trading_date.replace("-", "")) * 1_000_000,
            "open": source_bar.open,
            "high": source_bar.high,
            "low": source_bar.low,
            "close": source_bar.close,
            "volume": volume,
            "total_turnover": source_bar.close * volume,
            "limit_up": source_bar.close * 10.0,
            "limit_down": source_bar.close * 0.1,
            "settlement": self._np.nan,
            "prev_settlement": self._np.nan,
            "open_interest": 0.0,
            "basis_spread": self._np.nan,
            "discount_rate": self._np.nan,
            "acc_net_value": self._np.nan,
            "unit_net_value": self._np.nan,
        }

    def get_bar(self, instrument: Any, dt: Any, frequency: str) -> Any:
        if frequency != "1d":
            raise NotImplementedError("frozen adapter supports daily bars only")
        return self._bar(instrument.order_book_id, self._date_key(dt))

    def get_open_auction_bar(self, instrument: Any, dt: Any) -> Any:
        return self.get_bar(instrument, dt, "1d")

    def get_open_auction_volume(self, instrument: Any, dt: Any) -> int:
        bar = self.get_bar(instrument, dt, "1d")
        return 0 if bar is None else int(bar["volume"])

    def history_bars(
        self,
        instrument: Any,
        bar_count: Optional[int],
        frequency: str,
        fields: Any,
        dt: Any,
        skip_suspended: bool = True,
        include_now: bool = False,
        adjust_type: str = "pre",
        adjust_orig: Optional[datetime] = None,
    ) -> Any:
        del include_now, adjust_type, adjust_orig
        if frequency != "1d":
            raise NotImplementedError("frozen adapter supports daily bars only")
        end = self._date_key(dt)
        bars = []
        for trading_date in self._runtime.trading_dates:
            if trading_date > end:
                break
            bar = self._bar(instrument.order_book_id, trading_date)
            if bar is None or (skip_suspended and bar["volume"] == 0):
                continue
            bars.append(bar)
        if bar_count is not None:
            bars = bars[-bar_count:]
        if isinstance(fields, str):
            dtype = "i8" if fields == "datetime" else "f8"
            return self._np.array([bar[fields] for bar in bars], dtype=dtype)
        selected_fields = list(
            fields
            or [
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "total_turnover",
            ]
        )
        dtype = [
            (field, "i8" if field == "datetime" else "f8")
            for field in selected_fields
        ]
        return self._np.array(
            [tuple(bar[field] for field in selected_fields) for bar in bars],
            dtype=dtype,
        )

    def get_yield_curve(self, start_date: Any, end_date: Any, tenor: Any = None) -> Any:
        del start_date, end_date, tenor
        return self._pd.DataFrame()

    def get_dividend(self, instrument: Any) -> Any:
        if self._runtime.price_adjustment != "none":
            return None
        original = self._runtime.original_symbols.get(instrument.order_book_id)
        if original is None:
            return None
        actions = sorted(
            (
                action
                for day_actions in self._runtime.corporate_actions_by_date.values()
                for action in day_actions
                if action.symbol == original
                and action.event_type == "cash_dividend"
            ),
            key=lambda action: action.ex_date,
        )
        if not actions:
            return None
        dtype = [
            ("book_closure_date", "i8"),
            ("announcement_date", "i8"),
            ("dividend_cash_before_tax", "f8"),
            ("ex_dividend_date", "i8"),
            ("payable_date", "i8"),
            ("round_lot", "f8"),
        ]
        rows = []
        for action in actions:
            ex_date = int(action.ex_date.strftime("%Y%m%d"))
            announcement_date = (
                action.announced_at.date()
                if action.announced_at is not None
                else action.ex_date
            )
            rows.append(
                (
                    ex_date,
                    int(announcement_date.strftime("%Y%m%d")),
                    float(action.cash_amount),
                    ex_date,
                    ex_date,
                    1.0,
                )
            )
        return self._np.array(rows, dtype=dtype)

    def get_split(self, instrument: Any) -> Any:
        if self._runtime.price_adjustment != "none":
            return None
        original = self._runtime.original_symbols.get(instrument.order_book_id)
        if original is None:
            return None
        actions = sorted(
            (
                action
                for day_actions in self._runtime.corporate_actions_by_date.values()
                for action in day_actions
                if action.symbol == original
                and action.event_type in {"split", "reverse_split"}
            ),
            key=lambda action: action.ex_date,
        )
        if not actions:
            return None
        return self._np.array(
            [
                (
                    int(action.ex_date.strftime("%Y%m%d")) * 1_000_000,
                    float(action.ratio),
                )
                for action in actions
            ],
            dtype=[("ex_date", "i8"), ("split_factor", "f8")],
        )

    def get_settle_price(self, instrument: Any, trading_date: Any) -> float:
        del instrument, trading_date
        return self._np.nan

    def history_ticks(self, instrument: Any, count: int, dt: Any) -> List[Any]:
        del instrument, count, dt
        return []

    def current_snapshot(self, instrument: Any, frequency: str, dt: Any) -> Any:
        raise NotImplementedError("intraday snapshots are outside adapter scope")

    def get_trading_minutes_for(self, instrument: Any, trading_dt: Any) -> List[Any]:
        del instrument, trading_dt
        return []

    def get_futures_trading_parameters(self, instrument: Any, dt: Any) -> Any:
        raise NotImplementedError("futures are outside adapter scope")

    def get_merge_ticks(
        self, order_book_id_list: Sequence[str], trading_date: Any, last_dt: Any = None
    ) -> List[Any]:
        del order_book_id_list, trading_date, last_dt
        return []

    def get_share_transformation(self, order_book_id: str) -> None:
        del order_book_id
        return None

    def is_suspended(self, order_book_id: str, dates: Sequence[Any]) -> List[bool]:
        original = self._runtime.original_symbols.get(order_book_id)
        if original is None:
            return [True] * len(dates)
        return [
            not self._runtime.tradable_by_date.get(
                self._date_key(value), {}
            ).get(original, False)
            for value in dates
        ]

    def is_st_stock(self, order_book_id: str, dates: Sequence[Any]) -> List[bool]:
        del order_book_id
        return [False] * len(dates)

    def get_algo_bar(
        self, id_or_ins: Any, start_min: int, end_min: int, dt: Any
    ) -> None:
        del id_or_ins, start_min, end_min, dt
        return None

    def get_exchange_rate(
        self, trading_date: Any, local: Any, settlement: Any = None
    ) -> Any:
        del trading_date, local, settlement
        return self._exchange_rate_type(1.0, 1.0, 1.0, 1.0, 1.0, 1.0)


class _LetsQuantRqalphaMod:
    def __init__(self) -> None:
        self._env: Any = None
        self._runtime: Optional[_RuntimeContext] = None

    def start_up(self, env: Any, mod_config: Any) -> None:
        from rqalpha.core.events import EVENT

        runtime_id = str(mod_config.runtime_id)
        try:
            runtime = _RUNTIMES[runtime_id]
        except KeyError as exc:
            raise RuntimeError("lets-quant RQAlpha runtime context is missing") from exc
        self._env = env
        self._runtime = runtime
        env.set_data_source(_FrozenDailyDataSource(runtime))
        for event_type, normalized_name in (
            (EVENT.ORDER_PENDING_NEW, "order_pending_new"),
            (EVENT.ORDER_CREATION_PASS, "order_creation_pass"),
            (EVENT.ORDER_CREATION_REJECT, "order_creation_reject"),
            (EVENT.ORDER_CANCELLATION_PASS, "order_cancellation_pass"),
            (EVENT.ORDER_UNSOLICITED_UPDATE, "order_unsolicited_update"),
            (EVENT.TRADE, "trade"),
        ):
            env.event_bus.add_listener(
                event_type,
                self._event_listener(normalized_name),
                user=True,
            )
        env.event_bus.add_listener(
            EVENT.POST_SETTLEMENT, self._capture_nav, user=True
        )
        env.event_bus.add_listener(
            EVENT.POST_BEFORE_TRADING,
            self._capture_corporate_actions,
            user=True,
        )

    def _event_listener(self, event_type: str) -> Any:
        def listener(event: Any) -> None:
            if self._runtime is None or self._env is None:
                raise RuntimeError("RQAlpha collector is not initialized")
            order = getattr(event, "order", None)
            if order is None:
                if event_type == "order_creation_reject":
                    intent = self._runtime.submitting_intent
                    mapped = str(getattr(event, "order_book_id", ""))
                    if (
                        intent is None
                        or self._runtime.mapped_symbols[intent["symbol"]]
                        != mapped
                    ):
                        raise RuntimeError(
                            "RQAlpha precheck rejection cannot be bound to "
                            "the submitting intent"
                        )
                    self._runtime.events.append(
                        {
                            "sequence": len(self._runtime.events) + 1,
                            "event_time": _event_time(self._env.calendar_dt),
                            "event_type": (
                                "order_tradability_reject"
                                if not self._runtime.tradable_by_date[
                                    intent["execution_date"]
                                ][intent["symbol"]]
                                else "order_precheck_reject"
                            ),
                            "order_id": (
                                "rqalpha-precheck-"
                                f"{int(intent['sequence']) + 1}"
                            ),
                            "trade_id": "",
                            "symbol": intent["symbol"],
                            "side": intent["side"],
                            "requested_quantity": int(intent["quantity"]),
                            "cumulative_filled_quantity": 0,
                            "event_fill_quantity": 0,
                            "order_status": "REJECTED",
                            "fill_price": 0.0,
                            "commission": 0.0,
                            "tax": 0.0,
                            "message": str(getattr(event, "reason", "")),
                        }
                    )
                    return
                raise RuntimeError(
                    f"RQAlpha emitted {event_type} without an order object"
                )
            trade = getattr(event, "trade", None)
            mapped = str(order.order_book_id)
            symbol = self._runtime.original_symbols[mapped]
            self._runtime.events.append(
                {
                    "sequence": len(self._runtime.events) + 1,
                    "event_time": _event_time(self._env.calendar_dt),
                    "event_type": event_type,
                    "order_id": str(order.order_id),
                    "trade_id": str(trade.exec_id) if trade is not None else "",
                    "symbol": symbol,
                    "side": _enum_name(order.side),
                    "requested_quantity": int(order.quantity),
                    "cumulative_filled_quantity": int(order.filled_quantity),
                    "event_fill_quantity": (
                        int(trade.last_quantity) if trade is not None else 0
                    ),
                    "order_status": _enum_name(order.status),
                    "fill_price": float(trade.last_price) if trade is not None else 0.0,
                    "commission": float(trade.commission) if trade is not None else 0.0,
                    "tax": float(trade.tax) if trade is not None else 0.0,
                    "message": str(order.message or ""),
                }
            )

        return listener

    def _capture_nav(self, event: Any) -> None:
        del event
        if self._runtime is None or self._env is None:
            raise RuntimeError("RQAlpha collector is not initialized")
        positions = {symbol: 0 for symbol in self._runtime.symbols}
        for position in self._env.portfolio.get_positions():
            quantity = int(position.quantity)
            if quantity == 0:
                continue
            mapped = str(position.order_book_id)
            symbol = self._runtime.original_symbols.get(mapped)
            if symbol is None:
                raise RuntimeError(f"unexpected RQAlpha position {mapped}")
            if quantity < 0:
                raise RuntimeError("short positions are outside adapter scope")
            positions[symbol] += quantity
        self._runtime.nav_rows.append(
            {
                "date": self._env.trading_dt.date().isoformat(),
                "nav": float(self._env.portfolio.total_value),
                "cash": float(self._env.portfolio.cash),
                "positions": positions,
            }
        )

    def _capture_corporate_actions(self, event: Any) -> None:
        del event
        if self._runtime is None or self._env is None:
            raise RuntimeError("RQAlpha collector is not initialized")
        trading_date = self._env.trading_dt.date().isoformat()
        previous_positions = (
            dict(self._runtime.nav_rows[-1]["positions"])
            if self._runtime.nav_rows
            else {symbol: 0 for symbol in self._runtime.symbols}
        )
        previous_cash = (
            float(self._runtime.nav_rows[-1]["cash"])
            if self._runtime.nav_rows
            else float(self._runtime.policy.portfolio.initial_cash)
        )
        actual_positions = {symbol: 0 for symbol in self._runtime.symbols}
        for position in self._env.portfolio.get_positions():
            quantity = int(position.quantity)
            if quantity == 0:
                continue
            symbol = self._runtime.original_symbols.get(
                str(position.order_book_id)
            )
            if symbol is None:
                raise RuntimeError(
                    f"unexpected RQAlpha position {position.order_book_id}"
                )
            actual_positions[symbol] += quantity

        expected_positions = dict(previous_positions)
        expected_cash = previous_cash
        action_rows: List[Dict[str, Any]] = []
        for action in sorted(
            self._runtime.corporate_actions_by_date.get(trading_date, []),
            key=lambda item: (item.symbol, item.event_type),
        ):
            quantity_before = expected_positions.get(action.symbol, 0)
            quantity_after = quantity_before
            cash_delta = 0.0
            accounting_event_type = "corporate_action_embedded"
            if self._runtime.price_adjustment == "none":
                accounting_event_type = action.event_type
                if action.event_type == "cash_dividend":
                    if action.cash_amount is None:
                        raise RuntimeError(
                            "RQAlpha cash dividend is missing cash_amount"
                        )
                    cash_delta = quantity_before * float(action.cash_amount)
                    expected_cash += cash_delta
                else:
                    if action.ratio is None:
                        raise RuntimeError(
                            "RQAlpha split action is missing ratio"
                        )
                    raw_quantity = quantity_before * float(action.ratio)
                    quantity_after = round(raw_quantity)
                    if abs(raw_quantity - quantity_after) > 1e-9:
                        raise RuntimeError(
                            "RQAlpha corporate action creates fractional shares "
                            f"without a cash-in-lieu policy: {action.symbol}"
                        )
                    if action.symbol in expected_positions:
                        expected_positions[action.symbol] = int(quantity_after)
            action_rows.append(
                {
                    "trading_date": trading_date,
                    "symbol": action.symbol,
                    "source_event_type": action.event_type,
                    "accounting_event_type": accounting_event_type,
                    "quantity_delta": int(quantity_after - quantity_before),
                    "cash_delta": cash_delta,
                    "cash_amount": action.cash_amount,
                    "ratio": action.ratio,
                    "reference_id": (
                        f"corporate_action:{trading_date}:{action.symbol}:"
                        f"{action.event_type}"
                    ),
                }
            )
        actual_cash = float(self._env.portfolio.cash)
        if actual_positions != expected_positions:
            raise RuntimeError(
                "RQAlpha native corporate-action position state differs from "
                f"the frozen input on {trading_date}: expected "
                f"{expected_positions}, got {actual_positions}"
            )
        if abs(actual_cash - expected_cash) > 1e-7:
            raise RuntimeError(
                "RQAlpha native corporate-action cash state differs from the "
                f"frozen input on {trading_date}: expected {expected_cash}, "
                f"got {actual_cash}"
            )
        self._runtime.corporate_action_rows.extend(action_rows)

    def tear_down(self, code: Any, exception: Any = None) -> Dict[str, Any]:
        del code, exception
        if self._runtime is None:
            return {"events": [], "nav_rows": []}
        return {
            "events": list(self._runtime.events),
            "nav_rows": list(self._runtime.nav_rows),
            "corporate_action_rows": list(
                self._runtime.corporate_action_rows
            ),
        }


def load_mod() -> _LetsQuantRqalphaMod:
    return _LetsQuantRqalphaMod()


def _strategy_init(context: Any) -> None:
    from rqalpha.api import subscribe

    runtime = _RUNTIMES[str(context.runtime_id)]
    context.lets_quant_runtime_id = str(context.runtime_id)
    subscribe([runtime.mapped_symbols[symbol] for symbol in runtime.symbols])


def _portfolio_positions(
    portfolio: Any, runtime: _RuntimeContext
) -> Dict[str, int]:
    positions = {symbol: 0 for symbol in runtime.symbols}
    for position in portfolio.get_positions():
        quantity = int(position.quantity)
        if quantity == 0:
            continue
        mapped = str(position.order_book_id)
        symbol = runtime.original_symbols.get(mapped)
        if symbol is None:
            raise IndependentPolicyError(
                f"unexpected RQAlpha position {mapped}"
            )
        if quantity < 0:
            raise IndependentPolicyError(
                "short positions are outside adapter scope"
            )
        positions[symbol] += quantity
    return positions


def _current_bar_prices(
    bar_dict: Any, runtime: _RuntimeContext
) -> Dict[str, float]:
    prices: Dict[str, float] = {}
    for symbol in runtime.symbols:
        mapped = runtime.mapped_symbols[symbol]
        price = float(bar_dict[mapped].close)
        if not math.isfinite(price) or price <= 0:
            raise IndependentPolicyError(
                f"RQAlpha current bar is invalid for {symbol}"
            )
        prices[symbol] = price
    return prices


def _visible_history(
    runtime: _RuntimeContext,
    *,
    trading_date: str,
    current_prices: Mapping[str, float],
) -> Dict[str, List[Tuple[str, float]]]:
    from rqalpha.api import history_bars

    history = {
        symbol: [(trading_date, current_prices[symbol])]
        for symbol in runtime.symbols
    }
    lookback = runtime.policy.strategy.lookback_trading_days
    if runtime.policy.strategy.kind != "momentum_filter" or lookback is None:
        return history
    for symbol in runtime.symbols:
        mapped = runtime.mapped_symbols[symbol]
        raw_bars = history_bars(
            mapped,
            lookback + 1,
            "1d",
            ["datetime", "close"],
            skip_suspended=False,
        )
        observations: List[Tuple[str, float]] = []
        for raw_bar in raw_bars:
            raw_datetime = str(int(raw_bar["datetime"])).zfill(14)
            observed_date = datetime.strptime(
                raw_datetime[:8], "%Y%m%d"
            ).date().isoformat()
            observations.append((observed_date, float(raw_bar["close"])))
        if observations and observations[-1][0] == trading_date:
            observations[-1] = (trading_date, current_prices[symbol])
        else:
            observations.append((trading_date, current_prices[symbol]))
        history[symbol] = observations
    return history


def _submit_intent(
    context: Any,
    runtime: _RuntimeContext,
    intent: Mapping[str, Any],
) -> None:
    from rqalpha.api import deposit, order_shares, withdraw

    mapped = runtime.mapped_symbols[str(intent["symbol"])]
    amount = int(intent["quantity"])
    runtime.submitting_intent = dict(intent)
    try:
        if intent["side"] == "SELL":
            order_shares(mapped, -amount)
            return
        reserve = max(
            0.0,
            float(context.portfolio.total_value)
            * runtime.cash_buffer_weight,
        )
        reserved_cash = min(
            reserve, max(0.0, float(context.portfolio.cash))
        )
        if reserved_cash > 0:
            withdraw("STOCK", reserved_cash)
        try:
            order_shares(mapped, amount)
        finally:
            if reserved_cash > 0:
                deposit("STOCK", reserved_cash)
    finally:
        runtime.submitting_intent = None


def _crosses_quantity_changing_action(
    runtime: _RuntimeContext, intent: Mapping[str, Any]
) -> bool:
    if runtime.price_adjustment != "none":
        return False
    return any(
        action.symbol == intent["symbol"]
        and action.event_type in {"split", "reverse_split"}
        and intent["signal_date"]
        < action.ex_date.isoformat()
        <= intent["execution_date"]
        for actions in runtime.corporate_actions_by_date.values()
        for action in actions
    )


def _record_corporate_action_rejection(
    context: Any,
    runtime: _RuntimeContext,
    intent: Mapping[str, Any],
) -> None:
    runtime.events.append(
        {
            "sequence": len(runtime.events) + 1,
            "event_time": _event_time(context.now),
            "event_type": "order_corporate_action_reject",
            "order_id": (
                "rqalpha-corporate-action-"
                f"{int(intent['sequence']) + 1}"
            ),
            "trade_id": "",
            "symbol": intent["symbol"],
            "side": intent["side"],
            "requested_quantity": int(intent["quantity"]),
            "cumulative_filled_quantity": 0,
            "event_fill_quantity": 0,
            "order_status": "REJECTED",
            "fill_price": 0.0,
            "commission": 0.0,
            "tax": 0.0,
            "message": (
                "intent quantity became stale across a split or reverse split"
            ),
        }
    )


def _strategy_handle_bar(context: Any, bar_dict: Any) -> None:
    runtime = _RUNTIMES[context.lets_quant_runtime_id]
    trading_date = context.now.date().isoformat()
    due_intents = [
        intent
        for intent in runtime.pending_intents
        if intent["execution_date"] == trading_date
    ]
    runtime.pending_intents = [
        intent
        for intent in runtime.pending_intents
        if intent["execution_date"] != trading_date
    ]
    for intent in sorted(
        due_intents,
        key=lambda item: (0 if item["side"] == "SELL" else 1, item["sequence"]),
    ):
        if _crosses_quantity_changing_action(runtime, intent):
            _record_corporate_action_rejection(context, runtime, intent)
        else:
            _submit_intent(context, runtime, intent)

    if runtime.decision_mode == "frozen_orders":
        return
    current_prices = _current_bar_prices(bar_dict, runtime)
    positions = _portfolio_positions(context.portfolio, runtime)
    nav = float(context.portfolio.cash) + sum(
        positions[symbol] * current_prices[symbol]
        for symbol in runtime.symbols
    )
    runtime.peak_nav = max(runtime.peak_nav, nav)
    drawdown = nav / runtime.peak_nav - 1 if runtime.peak_nav > 0 else -1.0
    if drawdown <= -runtime.policy.risk.max_drawdown:
        runtime.risk_frozen = True

    current_index = runtime.trading_dates.index(trading_date)
    execution_index = current_index + runtime.execution_delay_trading_days
    if (
        current_index
        % runtime.policy.strategy.rebalance_every_n_trading_days
        != 0
        or execution_index >= len(runtime.trading_dates)
    ):
        return
    if runtime.risk_frozen or runtime.pending_intents:
        history = {
            symbol: [(trading_date, current_prices[symbol])]
            for symbol in runtime.symbols
        }
    else:
        history = _visible_history(
            runtime,
            trading_date=trading_date,
            current_prices=current_prices,
        )
    signal = independent_signal(
        runtime.policy,
        signal_date=trading_date,
        execution_date=runtime.trading_dates[execution_index],
        nav=nav,
        positions=positions,
        current_prices=current_prices,
        history_by_symbol=history,
        risk_frozen=runtime.risk_frozen,
        pending_order_count=len(runtime.pending_intents),
    )
    runtime.signals.append(signal)
    if signal["status"] != "accepted":
        return
    for order in signal["orders"]:
        intent = {
            "sequence": len(runtime.intents),
            **dict(order),
        }
        runtime.intents.append(intent)
        runtime.pending_intents.append(intent)


def _normalize_engine_results(
    runtime: _RuntimeContext,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    events_by_order: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for event in runtime.events:
        events_by_order[event["order_id"]].append(event)

    intents_by_key = {
        (
            intent["execution_date"],
            intent["symbol"],
            intent["side"],
            intent["quantity"],
        ): intent
        for intent in runtime.intents
    }
    order_rows_by_sequence: Dict[int, Dict[str, Any]] = {}
    trade_rows_by_sequence: Dict[int, Dict[str, Any]] = {}
    consumed_keys = set()
    for order_id, events in events_by_order.items():
        first = events[0]
        execution_date = str(first["event_time"])[:10]
        key = (
            execution_date,
            first["symbol"],
            first["side"],
            first["requested_quantity"],
        )
        intent = intents_by_key.get(key)
        if intent is None or key in consumed_keys:
            raise EngineValidationError(
                f"RQAlpha order {order_id} cannot be bound to one candidate intent"
            )
        consumed_keys.add(key)
        trade_events = [event for event in events if event["event_type"] == "trade"]
        filled_quantity = sum(event["event_fill_quantity"] for event in trade_events)
        weighted_fill_value = sum(
            event["event_fill_quantity"] * event["fill_price"]
            for event in trade_events
        )
        avg_fill_price = (
            weighted_fill_value / filled_quantity if filled_quantity else 0.0
        )
        commission = sum(event["commission"] for event in trade_events)
        tax = sum(event["tax"] for event in trade_events)
        final_status = events[-1]["order_status"]
        event_sequence = int(first["sequence"])
        order_rows_by_sequence[event_sequence] = {
            "order_id": order_id,
            "signal_date": intent["signal_date"],
            "execution_date": intent["execution_date"],
            "symbol": intent["symbol"],
            "side": intent["side"],
            "requested_quantity": intent["quantity"],
            "filled_quantity": filled_quantity,
            "avg_fill_price": avg_fill_price,
            "commission": commission,
            "tax": tax,
            "final_status": final_status,
            "event_count": len(events),
            "trade_count": len(trade_events),
        }
        market_price = runtime.market_prices[intent["execution_date"]][
            intent["symbol"]
        ]
        is_corporate_action_reject = (
            events[0]["event_type"] == "order_corporate_action_reject"
        )
        is_non_tradable = not runtime.tradable_by_date[
            intent["execution_date"]
        ][intent["symbol"]]
        if filled_quantity == 0:
            fill_price = (
                market_price
                if is_non_tradable or is_corporate_action_reject
                else market_price
                * (
                    1.0 + runtime.slippage_fraction
                    if intent["side"] == "BUY"
                    else 1.0 - runtime.slippage_fraction
                )
            )
            slippage = 0.0
        else:
            fill_price = avg_fill_price
            slippage = abs(fill_price - market_price) * filled_quantity
        status = (
            "rejected_corporate_action"
            if filled_quantity == 0 and is_corporate_action_reject
            else "rejected_not_tradable"
            if filled_quantity == 0 and is_non_tradable
            else "filled"
            if final_status == "FILLED"
            and filled_quantity == intent["quantity"]
            else "partial"
            if filled_quantity > 0
            else "rejected"
        )
        trade_rows_by_sequence[event_sequence] = {
            "signal_date": intent["signal_date"],
            "execution_date": intent["execution_date"],
            "symbol": intent["symbol"],
            "side": intent["side"],
            "requested_quantity": intent["quantity"],
            "filled_quantity": filled_quantity,
            "signal_price": intent["signal_price"],
            "market_price": market_price,
            "fill_price": fill_price,
            "gross_notional": filled_quantity * fill_price,
            "commission": commission,
            "tax": tax,
            "slippage_cost": slippage,
            "status": status,
        }
    if consumed_keys != set(intents_by_key):
        missing = sorted(set(intents_by_key) - consumed_keys)
        raise EngineValidationError(
            f"RQAlpha did not emit orders for candidate intents: {missing[:5]}"
        )
    sequence_order = sorted(order_rows_by_sequence)
    date_order = {
        trading_date: index
        for index, trading_date in enumerate(runtime.trading_dates)
    }
    trade_order = sorted(
        trade_rows_by_sequence,
        key=lambda sequence: (
            date_order[
                trade_rows_by_sequence[sequence]["execution_date"]
            ],
            {
                "rejected_corporate_action": 0,
                "rejected_not_tradable": 1,
            }.get(trade_rows_by_sequence[sequence]["status"], 2),
            sequence,
        ),
    )
    return (
        [order_rows_by_sequence[index] for index in sequence_order],
        [trade_rows_by_sequence[index] for index in trade_order],
    )


def run_rqalpha_validation(
    *,
    reference_directory: Path,
    output_root: Path,
    prices_path: Optional[Path] = None,
    dataset_path: Optional[Path] = None,
    liquidity_path: Optional[Path] = None,
    decision_mode: str = "independent_policy",
    volume_percent: float = 1.0,
    money_tolerance: float = 1e-6,
    ratio_tolerance: float = 1e-10,
) -> Tuple[Path, Dict[str, Any]]:
    if sys.version_info < (3, 9) or sys.version_info >= (3, 15):
        raise EngineValidationError(
            "RQAlpha adapter requires Python 3.9 through 3.14"
        )
    if not math.isfinite(volume_percent) or not 0 < volume_percent <= 1:
        raise EngineValidationError("volume_percent must be finite in (0, 1]")
    if decision_mode not in DECISION_MODES:
        raise EngineValidationError(
            "decision_mode must be independent_policy or frozen_orders"
        )
    try:
        import numpy as np
        import pandas as pd
        import rqalpha
        from rqalpha import main as rqalpha_main
        from rqalpha.utils.config import parse_config
        from rqalpha.utils.functools import clear_all_cached_functions
    except ImportError as exc:
        raise EngineValidationError(
            "RQAlpha adapter dependencies are missing; install with "
            "python -m pip install -e '.[rqalpha]'"
        ) from exc
    if rqalpha.__version__ != SUPPORTED_RQALPHA_VERSION:
        raise EngineValidationError(
            "RQAlpha adapter was verified against rqalpha=="
            f"{SUPPORTED_RQALPHA_VERSION}, found {rqalpha.__version__}"
        )

    reference_directory = reference_directory.resolve()
    reference_identity(reference_directory)
    policy = load_policy(reference_directory / "policy.snapshot.json")
    market_input = resolve_engine_market_input(
        reference_directory,
        supplied_prices_path=prices_path,
        supplied_dataset_path=dataset_path,
        adapter_name="RQAlpha adapter v4",
    )
    reference_metrics = load_json_object(
        reference_directory / "metrics.json"
    )
    execution_delay = reference_metrics.get("execution_delay_trading_days")
    if (
        isinstance(execution_delay, bool)
        or not isinstance(execution_delay, int)
        or execution_delay <= 0
    ):
        raise EngineValidationError(
            "reference metrics must declare a positive integer "
            "execution_delay_trading_days"
        )
    market = market_input.market
    symbols = sorted(policy.strategy.target_weights)
    validate_market_coverage(market, symbols)
    reference_nav = read_nav_rows(reference_directory / "nav.csv")
    trading_dates = [row["date"] for row in reference_nav]
    if set(reference_nav[0]["positions"]) != set(symbols):
        raise EngineValidationError(
            "reference starting positions must contain exactly the strategy symbols"
        )
    if any(reference_nav[0]["positions"].values()):
        raise EngineValidationError(
            "RQAlpha adapter v4 supports zero starting positions only"
        )
    market_prices = {
        trading_date.isoformat(): {
            symbol: market.prices_on(trading_date)[symbol] for symbol in symbols
        }
        for trading_date in market.dates
    }
    missing_dates = [value for value in trading_dates if value not in market_prices]
    if missing_dates:
        raise EngineValidationError(
            "price input is missing reference NAV dates: "
            + ", ".join(missing_dates[:5])
        )
    if decision_mode == "frozen_orders":
        intents = load_frozen_order_intents(
            reference_directory / "signals.csv",
            lot_size=policy.execution.lot_size,
            symbols=symbols,
            trading_dates=trading_dates,
            market_prices=market_prices,
            adapter_name="RQAlpha adapter v4 frozen-orders mode",
        )
    else:
        intents = []
    default_volumes = {
        trading_date: {
            symbol: (
                market_input.bars_by_date[trading_date][symbol].volume
                if market_input.bars_by_date[trading_date][symbol].tradable
                else 0
            )
            for symbol in symbols
        }
        for trading_date in trading_dates
    }
    volumes, liquidity_sha256 = _load_liquidity(
        liquidity_path,
        trading_dates=trading_dates,
        symbols=symbols,
        default_volumes=default_volumes,
    )
    mapped_symbols = {
        symbol: _mapped_symbol(index) for index, symbol in enumerate(symbols)
    }
    runtime = _RuntimeContext(
        trading_dates=trading_dates,
        symbols=symbols,
        mapped_symbols=mapped_symbols,
        original_symbols={mapped: symbol for symbol, mapped in mapped_symbols.items()},
        market_prices=market_prices,
        market_bars={
            trading_date: {
                symbol: market_input.bars_by_date[trading_date][symbol]
                for symbol in symbols
            }
            for trading_date in trading_dates
        },
        tradable_by_date={
            trading_date: {
                symbol: market_input.bars_by_date[trading_date][
                    symbol
                ].tradable
                for symbol in symbols
            }
            for trading_date in trading_dates
        },
        corporate_actions_by_date={
            trading_date: market.corporate_actions_on(
                date.fromisoformat(trading_date)
            )
            for trading_date in trading_dates
        },
        price_adjustment=market.price_adjustment,
        volumes=volumes,
        lot_size=policy.execution.lot_size,
        cash_buffer_weight=policy.portfolio.cash_buffer_weight,
        slippage_fraction=policy.execution.slippage_bps / 10_000,
        policy=policy,
        decision_mode=decision_mode,
        execution_delay_trading_days=execution_delay,
        intents=intents,
        pending_intents=list(intents),
        peak_nav=policy.portfolio.initial_cash,
    )
    runtime_id = uuid.uuid4().hex
    _RUNTIMES[runtime_id] = runtime
    commission_multiplier = policy.execution.commission_rate / 0.0008
    tax_multiplier = policy.execution.sell_tax_rate / 0.0005
    config = {
        "base": {
            "start_date": trading_dates[0],
            "end_date": trading_dates[-1],
            "frequency": "1d",
            "accounts": {"stock": policy.portfolio.initial_cash},
            "init_positions": {},
            "capital_gain_tax_rate": 0,
            "partial_fill_on_insufficient_cash": True,
        },
        "extra": {
            "log_level": "error",
            "context_vars": {"runtime_id": runtime_id},
        },
        "mod": {
            "sys_progress": {"enabled": False},
            "sys_analyser": {"enabled": False},
            "sys_scheduler": {"enabled": False},
            "sys_accounts": {
                "enabled": True,
                "auto_switch_order_value": False,
            },
            "sys_simulation": {
                "enabled": True,
                "matching_type": "current_bar",
                "signal": False,
                "price_limit": True,
                "volume_limit": True,
                "volume_percent": volume_percent,
                "inactive_limit": True,
                "slippage_model": "PriceRatioSlippage",
                "slippage": policy.execution.slippage_bps / 10_000,
            },
            "sys_risk": {
                "enabled": True,
                "validate_price": True,
                "validate_is_trading": True,
                "validate_cash": True,
                "validate_self_trade": False,
            },
            "sys_transaction_cost": {
                "enabled": True,
                "stock_min_commission": policy.execution.minimum_commission,
                "stock_commission_multiplier": commission_multiplier,
                "tax_multiplier": tax_multiplier,
                "pit_tax": False,
            },
            "lets_quant": {
                "enabled": True,
                "lib": "lets_quant.rqalpha_adapter",
                "priority": 200,
                "runtime_id": runtime_id,
            },
        },
    }
    try:
        clear_all_cached_functions()
        strategy_funcs = {
            "init": _strategy_init,
            "handle_bar": _strategy_handle_bar,
        }
        parsed_config = parse_config(config, user_funcs=strategy_funcs)
        rqalpha_main.run(
            parsed_config,
            user_funcs=strategy_funcs,
        )
    except Exception as exc:
        raise EngineValidationError(
            f"RQAlpha simulation failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        _RUNTIMES.pop(runtime_id, None)

    if [row["date"] for row in runtime.nav_rows] != trading_dates:
        raise EngineValidationError(
            "RQAlpha NAV date axis differs from the frozen reference"
        )
    order_rows, trade_rows = _normalize_engine_results(runtime)
    metrics = summarize_candidate(
        runtime.nav_rows, trade_rows, runtime.corporate_action_rows
    )
    market_scope: Dict[str, Any] = {
        "reference_data_source": market_input.source_type,
        "prices_sha256": file_sha256(market_input.prices_path),
        "price_adjustment": market.price_adjustment,
        "bar_mapping": (
            "curated_ohlcv"
            if market_input.source_type == "curated_dataset"
            else "close_to_flat_ohlc"
        ),
        "tradability_source": (
            "curated_observations"
            if market_input.source_type == "curated_dataset"
            else "all_observations_tradable"
        ),
    }
    if market_input.dataset_manifest is not None:
        market_scope.update(
            {
                "dataset_id": market_input.dataset_manifest["dataset_id"],
                "dataset_snapshot_sha256": (
                    market_input.dataset_snapshot_sha256
                ),
                "observations_sha256": market_input.dataset_manifest[
                    "files"
                ]["observations.csv"],
            }
        )
    candidate_directory = write_engine_candidate(
        reference_directory=reference_directory,
        output_root=output_root,
        engine={
            "name": "rqalpha",
            "version": rqalpha.__version__,
            "adapter_version": ADAPTER_VERSION,
            "python_version": platform.python_version(),
            "numpy_version": np.__version__,
            "pandas_version": pd.__version__,
        },
        nav_rows=runtime.nav_rows,
        trade_rows=trade_rows,
        corporate_action_rows=runtime.corporate_action_rows,
        metrics=metrics,
        order_rows=order_rows,
        event_rows=runtime.events,
        signal_rows=(
            runtime.signals
            if decision_mode == "independent_policy"
            else None
        ),
        validation_scope={
            "input": decision_mode,
            "validated_components": [
                "adapter output contract and reference-input binding",
                *(
                    [
                        "independently generated PIT policy decisions, target "
                        "weights, turnover, and order intents"
                    ]
                    if decision_mode == "independent_policy"
                    else ["frozen order-intent replay"]
                ),
                "RQAlpha native order, trade, cancellation, and rejection events",
                "RQAlpha native cash-dividend and split account transitions",
                "corporate-action event journal and stale-intent rejection",
                "event-to-order-to-normalized-trade lifecycle reconciliation",
                "daily cash, positions, NAV, costs, and core summary metrics",
            ],
            "engine_native_components": [
                "daily event loop and current-bar matching",
                "prior-bar history access and current-bar visibility",
                "whole-lot and liquidity-constrained order matching",
                "insufficient-cash partial fills and unfilled market-order "
                "cancellation",
                "commission, sell tax, slippage, positions, cash, and valuation",
                "cash-dividend credits and integral split position changes",
            ],
            "adapter_mapped_components": [
                "synthetic RQAlpha instrument identifiers",
                "policy strategy semantics, target-lot sizing, turnover gate, "
                "and sell-before-buy submission",
                "dynamic cash-buffer withdrawal and immediate redeposit",
                "standalone closes mapped to flat OHLC or curated OHLCV bars",
                "curated suspension state mapped to RQAlpha tradability checks",
                "cross-action pending intent invalidation before submission",
            ],
            "excluded_components": [
                "market data correctness and provider lineage",
                "correctness of adjusted-price factors",
                "real order books, queue priority, and intraday execution",
            ],
            **market_scope,
            "liquidity_sha256": liquidity_sha256,
            "volume_percent": volume_percent,
            "decision_mode": decision_mode,
            "execution_delay_trading_days": execution_delay,
        },
        limitations=[
            "Parity validates software behavior for this frozen input only; it "
            "does not establish strategy or investment validity.",
            "Adapter v4 accepts standalone prices or validated curated daily "
            "datasets, long-only runs with zero starting positions, and at "
            "most one order per symbol/date.",
            "Adjusted datasets keep declared actions informational; unadjusted "
            "cash dividends and integral split results use RQAlpha's native "
            "account model.",
            (
                "Independent-policy mode reimplements fixed-weight and momentum "
                "policy semantics without reading reference signals or trades."
                if decision_mode == "independent_policy"
                else "Frozen-orders mode is execution-only diagnostics and does "
                "not independently validate strategy decisions."
            ),
            "Cash-buffer reserve mapping is adapter logic; fill quantities, native "
            "order states, transaction costs, and account changes are produced "
            "by RQAlpha.",
            "Synthetic common-stock instruments reproduce the configured fee and "
            "lot rules but do not establish exchange-specific market realism.",
        ],
    )
    report = reconcile_engine_candidate(
        reference_directory,
        candidate_directory,
        money_tolerance=money_tolerance,
        ratio_tolerance=ratio_tolerance,
    )
    write_reconciliation_report(
        report, candidate_directory / "reconciliation.json"
    )
    return candidate_directory, report
