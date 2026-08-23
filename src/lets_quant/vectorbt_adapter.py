from __future__ import annotations

import csv
import json
import math
import platform
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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
from .data import load_prices, validate_market_coverage


ADAPTER_VERSION = "1"
SUPPORTED_VECTORBT_VERSION = "1.1.0"


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


def _resolve_prices_path(
    reference_directory: Path,
    supplied_prices_path: Optional[Path],
) -> Tuple[Path, Dict[str, Any]]:
    manifest_path = reference_directory / "manifest.json"
    manifest = _load_json_object(manifest_path)
    if manifest.get("artifact_type") != "backtest":
        raise EngineValidationError(
            f"{manifest_path} must describe a backtest artifact"
        )
    data_source = manifest.get("data_source")
    if not isinstance(data_source, dict) or data_source.get("type") != (
        "standalone_prices_csv"
    ):
        raise EngineValidationError(
            "VectorBT adapter v1 supports standalone_prices_csv runs only; "
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


def _load_frozen_order_intents(
    signals_path: Path,
    *,
    lot_size: int,
    symbols: Sequence[str],
    trading_dates: Sequence[str],
    market_prices: Mapping[str, Mapping[str, float]],
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
                location = (
                    f"{signals_path}:{line_number}:orders[{order_index}]"
                )
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
                expected_signal_price = market_prices.get(
                    signal_date, {}
                ).get(symbol)
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
                        "VectorBT adapter v1 cannot represent multiple orders "
                        f"for {symbol} on {execution_date}"
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


def _as_single_series(frame: Any, label: str) -> Any:
    if getattr(frame, "ndim", None) == 1:
        return frame
    if getattr(frame, "ndim", None) == 2 and frame.shape[1] == 1:
        return frame.iloc[:, 0]
    raise EngineValidationError(f"VectorBT returned multiple {label} groups")


def run_vectorbt_validation(
    *,
    reference_directory: Path,
    output_root: Path,
    prices_path: Optional[Path] = None,
    money_tolerance: float = 1e-6,
    ratio_tolerance: float = 1e-10,
) -> Tuple[Path, Dict[str, Any]]:
    if sys.version_info < (3, 11) or sys.version_info >= (3, 15):
        raise EngineValidationError(
            "VectorBT adapter requires Python 3.11 through 3.14; the core CLI "
            "remains compatible with Python 3.9"
        )
    try:
        import numpy as np
        import pandas as pd
        import vectorbt as vbt
        from vectorbt.portfolio import nb
    except ImportError as exc:
        raise EngineValidationError(
            "VectorBT adapter dependencies are missing; install with "
            "python -m pip install -e '.[vectorbt]'"
        ) from exc
    if vbt.__version__ != SUPPORTED_VECTORBT_VERSION:
        raise EngineValidationError(
            "VectorBT adapter was verified against vectorbt=="
            f"{SUPPORTED_VECTORBT_VERSION}, found {vbt.__version__}"
        )

    reference_directory = reference_directory.resolve()
    reference_identity(reference_directory)
    resolved_prices_path, reference_manifest = _resolve_prices_path(
        reference_directory, prices_path
    )
    policy = load_policy(reference_directory / "policy.snapshot.json")
    market = load_prices(resolved_prices_path)
    symbols = sorted(policy.strategy.target_weights)
    validate_market_coverage(market, symbols)
    reference_nav = read_nav_rows(reference_directory / "nav.csv")
    trading_dates = [row["date"] for row in reference_nav]
    if set(reference_nav[0]["positions"]) != set(symbols):
        raise EngineValidationError(
            "reference starting positions must contain exactly the strategy "
            "symbols"
        )
    if any(reference_nav[0]["positions"].values()):
        raise EngineValidationError(
            "VectorBT adapter v1 supports zero starting positions only"
        )
    market_prices: Dict[str, Dict[str, float]] = {
        trading_date.isoformat(): {
            symbol: market.prices_on(trading_date)[symbol] for symbol in symbols
        }
        for trading_date in market.dates
    }
    missing_dates = [date for date in trading_dates if date not in market_prices]
    if missing_dates:
        raise EngineValidationError(
            "price input is missing reference NAV dates: "
            + ", ".join(missing_dates[:5])
        )
    intents = _load_frozen_order_intents(
        reference_directory / "signals.csv",
        lot_size=policy.execution.lot_size,
        symbols=symbols,
        trading_dates=trading_dates,
        market_prices=market_prices,
    )

    close = pd.DataFrame(
        [
            [market_prices[trading_date][symbol] for symbol in symbols]
            for trading_date in trading_dates
        ],
        index=pd.to_datetime(trading_dates),
        columns=symbols,
    )
    close.index.name = "date"
    close.columns.name = "symbol"
    sizes = np.full(close.shape, np.nan, dtype=float)
    symbol_indexes = {symbol: index for index, symbol in enumerate(symbols)}
    date_indexes = {value: index for index, value in enumerate(trading_dates)}
    intents_by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for intent in intents:
        row_index = date_indexes[intent["execution_date"]]
        column_index = symbol_indexes[intent["symbol"]]
        sizes[row_index, column_index] = (
            intent["quantity"]
            if intent["side"] == "BUY"
            else -intent["quantity"]
        )
        intents_by_date[intent["execution_date"]].append(intent)

    call_sequence = np.tile(np.arange(len(symbols)), (len(close), 1))
    for trading_date, row_index in date_indexes.items():
        ordered_columns = [
            symbol_indexes[intent["symbol"]]
            for intent in intents_by_date.get(trading_date, [])
        ]
        remaining_columns = [
            index for index in range(len(symbols)) if index not in ordered_columns
        ]
        call_sequence[row_index] = ordered_columns + remaining_columns

    prices = close.to_numpy()
    commission_rate = policy.execution.commission_rate
    minimum_commission = policy.execution.minimum_commission
    sell_tax_rate = policy.execution.sell_tax_rate
    slippage = policy.execution.slippage_bps / 10_000
    lot_size = policy.execution.lot_size
    cash_buffer_weight = policy.portfolio.cash_buffer_weight
    order_nb = nb.order_nb.py_func if hasattr(nb.order_nb, "py_func") else nb.order_nb

    def order_function(context: Any, order_sizes: Any, current_prices: Any) -> Any:
        requested = order_sizes[context.i, context.col]
        if np.isnan(requested):
            return order_nb(size=np.nan)
        market_price = current_prices[context.i, context.col]
        if requested < 0:
            actual = -min(abs(requested), max(context.position_now, 0.0))
        else:
            current_nav = context.cash_now
            for column in range(context.from_col, context.to_col):
                current_nav += (
                    context.last_position[column]
                    * current_prices[context.i, column]
                )
            available_cash = max(
                0.0,
                context.cash_now - current_nav * cash_buffer_weight,
            )
            fill_price = market_price * (1.0 + slippage)
            candidate = min(
                requested,
                math.floor(available_cash / fill_price / lot_size) * lot_size,
            )
            while candidate > 0:
                gross_notional = candidate * fill_price
                commission = max(
                    minimum_commission,
                    gross_notional * commission_rate,
                )
                if gross_notional + commission <= available_cash + 1e-9:
                    break
                candidate -= lot_size
            actual = max(0.0, candidate)
        adjusted_price = market_price * (
            1.0 + slippage if actual >= 0 else 1.0 - slippage
        )
        gross_notional = abs(actual) * adjusted_price
        commission = (
            max(minimum_commission, gross_notional * commission_rate)
            if gross_notional > 0
            else 0.0
        )
        fixed_fees = max(
            0.0, commission - gross_notional * commission_rate
        )
        proportional_fees = commission_rate + (
            sell_tax_rate if actual < 0 else 0.0
        )
        return order_nb(
            size=actual,
            price=market_price,
            fees=proportional_fees,
            fixed_fees=fixed_fees,
            slippage=slippage,
            size_granularity=lot_size,
            allow_partial=False,
            direction=0,
        )

    try:
        portfolio = vbt.Portfolio.from_order_func(
            close,
            order_function,
            sizes,
            prices,
            init_cash=policy.portfolio.initial_cash,
            cash_sharing=True,
            group_by=True,
            call_seq=call_sequence,
            use_numba=False,
            update_value=True,
            freq="1D",
            max_orders=max(1, len(intents)),
            attach_call_seq=True,
        )
    except Exception as exc:
        raise EngineValidationError(
            f"VectorBT simulation failed: {type(exc).__name__}: {exc}"
        ) from exc

    records_by_cell: Dict[Tuple[int, int], Any] = {}
    for record in portfolio.orders.values:
        key = (int(record["idx"]), int(record["col"]))
        if key in records_by_cell:
            raise EngineValidationError(
                "VectorBT returned duplicate order records for one date/symbol"
            )
        records_by_cell[key] = record
    expected_cells = {
        (
            date_indexes[intent["execution_date"]],
            symbol_indexes[intent["symbol"]],
        )
        for intent in intents
    }
    unexpected_cells = sorted(set(records_by_cell) - expected_cells)
    if unexpected_cells:
        raise EngineValidationError(
            f"VectorBT returned unexpected order records: {unexpected_cells[:5]}"
        )

    trade_rows: List[Dict[str, Any]] = []
    for intent in intents:
        row_index = date_indexes[intent["execution_date"]]
        column_index = symbol_indexes[intent["symbol"]]
        record = records_by_cell.get((row_index, column_index))
        expected_side = 0 if intent["side"] == "BUY" else 1
        if record is None:
            filled_quantity = 0
            fill_price = prices[row_index, column_index] * (
                1.0 + slippage
                if intent["side"] == "BUY"
                else 1.0 - slippage
            )
            recorded_fees = 0.0
        else:
            if int(record["side"]) != expected_side:
                raise EngineValidationError(
                    "VectorBT order side differs from the frozen intent"
                )
            filled_float = float(record["size"])
            filled_quantity = int(round(filled_float))
            if abs(filled_float - filled_quantity) > 1e-8:
                raise EngineValidationError(
                    "VectorBT returned a fractional filled quantity"
                )
            fill_price = float(record["price"])
            recorded_fees = float(record["fees"])
        market_price = float(prices[row_index, column_index])
        gross_notional = filled_quantity * fill_price
        commission = (
            max(minimum_commission, gross_notional * commission_rate)
            if filled_quantity > 0
            else 0.0
        )
        tax = (
            gross_notional * sell_tax_rate
            if intent["side"] == "SELL"
            else 0.0
        )
        if abs(recorded_fees - commission - tax) > 1e-7:
            raise EngineValidationError(
                "VectorBT fee record cannot be decomposed into configured "
                "commission and sell tax"
            )
        if filled_quantity == 0:
            status = "rejected"
        elif filled_quantity < intent["quantity"]:
            status = "partial"
        else:
            status = "filled"
        trade_rows.append(
            {
                "signal_date": intent["signal_date"],
                "execution_date": intent["execution_date"],
                "symbol": intent["symbol"],
                "side": intent["side"],
                "requested_quantity": intent["quantity"],
                "filled_quantity": filled_quantity,
                "signal_price": intent["signal_price"],
                "market_price": market_price,
                "fill_price": fill_price,
                "gross_notional": gross_notional,
                "commission": commission,
                "tax": tax,
                "slippage_cost": abs(fill_price - market_price)
                * filled_quantity,
                "status": status,
            }
        )

    values = _as_single_series(portfolio.value(group_by=True), "value")
    cash = _as_single_series(portfolio.cash(group_by=True), "cash")
    assets = portfolio.assets()
    if getattr(assets, "ndim", None) == 1:
        assets = assets.to_frame(name=symbols[0])
    nav_rows: List[Dict[str, Any]] = []
    for row_index, trading_date in enumerate(trading_dates):
        positions: Dict[str, int] = {}
        for symbol in symbols:
            quantity_float = float(assets.iloc[row_index][symbol])
            quantity = int(round(quantity_float))
            if abs(quantity_float - quantity) > 1e-8 or quantity < 0:
                raise EngineValidationError(
                    "VectorBT returned invalid long-only positions"
                )
            positions[symbol] = quantity
        nav_rows.append(
            {
                "date": trading_date,
                "nav": float(values.iloc[row_index]),
                "cash": float(cash.iloc[row_index]),
                "positions": positions,
            }
        )

    metrics = summarize_candidate(nav_rows, trade_rows)
    candidate_directory = write_engine_candidate(
        reference_directory=reference_directory,
        output_root=output_root,
        engine={
            "name": "vectorbt",
            "version": vbt.__version__,
            "adapter_version": ADAPTER_VERSION,
            "python_version": platform.python_version(),
            "numpy_version": np.__version__,
            "pandas_version": pd.__version__,
        },
        nav_rows=nav_rows,
        trade_rows=trade_rows,
        metrics=metrics,
        validation_scope={
            "input": "frozen_order_intents",
            "validated_components": [
                "adapter output contract and reference-input binding",
                "VectorBT order records after semantic lowering",
                "daily cash, positions, NAV, and core summary metrics",
            ],
            "engine_native_components": [
                "shared-cash order application",
                "long-only positions",
                "proportional and fixed fee debits",
                "daily position valuation and portfolio value",
            ],
            "adapter_mapped_components": [
                "sell-before-buy call sequence",
                "dynamic cash-buffer affordable quantity",
                "whole-lot quantity and insufficient-cash reduction",
                "commission minimum, sell-tax split, and symmetric slippage",
            ],
            "excluded_components": [
                "strategy signal generation and point-in-time feature logic",
                "market data correctness and provider lineage",
                "tradability, corporate actions, and adjusted-price semantics",
                "order book, liquidity, price limits, and intraday execution",
            ],
            "reference_data_source": reference_manifest["data_source"]["type"],
            "prices_sha256": file_sha256(resolved_prices_path),
        },
        limitations=[
            "Parity validates software behavior for this frozen input only; it "
            "does not establish strategy or investment validity.",
            "Adapter v1 accepts standalone daily-close, long-only runs with "
            "zero starting positions and at most one order per symbol/date.",
            "The adapter independently replays order intents but deliberately "
            "does not regenerate strategy decisions.",
            "Dynamic cash-buffer and whole-lot affordable quantities are "
            "lowered by adapter code because VectorBT has no equivalent native "
            "rule; parity does not independently prove that sizing formula.",
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
