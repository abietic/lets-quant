from __future__ import annotations

import math
import platform
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    load_frozen_order_intents,
    resolve_engine_market_input,
)


ADAPTER_VERSION = "3"
SUPPORTED_VECTORBT_VERSION = "1.1.0"

def run_vectorbt_validation(
    *,
    reference_directory: Path,
    output_root: Path,
    prices_path: Optional[Path] = None,
    dataset_path: Optional[Path] = None,
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
    policy = load_policy(reference_directory / "policy.snapshot.json")
    market_input = resolve_engine_market_input(
        reference_directory,
        supplied_prices_path=prices_path,
        supplied_dataset_path=dataset_path,
        adapter_name="VectorBT adapter v3",
    )
    market = market_input.market
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
            "VectorBT adapter v3 supports zero starting positions only"
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
    intents = load_frozen_order_intents(
        reference_directory / "signals.csv",
        lot_size=policy.execution.lot_size,
        symbols=symbols,
        trading_dates=trading_dates,
        market_prices=market_prices,
        adapter_name="VectorBT adapter v3",
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
    tradable_intents = set()
    stale_intent_sequences = set()
    quantity_actions = [
        action
        for actions in (market.corporate_actions_by_date or {}).values()
        for action in actions
        if action.event_type in {"split", "reverse_split"}
    ]
    for intent in intents:
        row_index = date_indexes[intent["execution_date"]]
        column_index = symbol_indexes[intent["symbol"]]
        crosses_quantity_action = market.price_adjustment == "none" and any(
            action.symbol == intent["symbol"]
            and intent["signal_date"]
            < action.ex_date.isoformat()
            <= intent["execution_date"]
            for action in quantity_actions
        )
        if crosses_quantity_action:
            stale_intent_sequences.add(int(intent["sequence"]))
        elif market.is_tradable(
            date.fromisoformat(intent["execution_date"]), intent["symbol"]
        ):
            tradable_intents.add(
                (intent["execution_date"], intent["symbol"])
            )
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
    action_rows: List[Dict[str, Any]] = []
    actions_by_date = {
        trading_date: sorted(
            market.corporate_actions_on(date.fromisoformat(trading_date)),
            key=lambda action: (action.symbol, action.event_type),
        )
        for trading_date in trading_dates
    }
    captured_cash = np.full(len(trading_dates), np.nan, dtype=float)
    captured_nav = np.full(len(trading_dates), np.nan, dtype=float)
    captured_positions = np.full(close.shape, np.nan, dtype=float)

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

    def pre_segment_function(context: Any) -> Tuple[()]:
        trading_date = trading_dates[context.i]
        for action in actions_by_date[trading_date]:
            column_index = symbol_indexes.get(action.symbol)
            quantity_before = (
                0.0
                if column_index is None
                else float(context.last_position[column_index])
            )
            quantity_after = quantity_before
            cash_delta = 0.0
            accounting_event_type = "corporate_action_embedded"
            if market.price_adjustment == "none":
                accounting_event_type = action.event_type
                if action.event_type == "cash_dividend":
                    if action.cash_amount is None:
                        raise EngineValidationError(
                            "VectorBT cash dividend is missing cash_amount"
                        )
                    cash_delta = quantity_before * float(action.cash_amount)
                    context.last_cash[context.group] += cash_delta
                    context.last_free_cash[context.group] += cash_delta
                else:
                    if action.ratio is None:
                        raise EngineValidationError(
                            "VectorBT split action is missing ratio"
                        )
                    raw_quantity = quantity_before * float(action.ratio)
                    quantity_after = round(raw_quantity)
                    if abs(raw_quantity - quantity_after) > 1e-9:
                        raise EngineValidationError(
                            "VectorBT corporate action creates fractional shares "
                            f"without a cash-in-lieu policy: {action.symbol}"
                        )
                    if column_index is not None:
                        context.last_position[column_index] = quantity_after
            action_rows.append(
                {
                    "trading_date": trading_date,
                    "symbol": action.symbol,
                    "source_event_type": action.event_type,
                    "accounting_event_type": accounting_event_type,
                    "quantity_delta": int(round(quantity_after - quantity_before)),
                    "cash_delta": cash_delta,
                    "cash_amount": action.cash_amount,
                    "ratio": action.ratio,
                    "reference_id": (
                        f"corporate_action:{trading_date}:{action.symbol}:"
                        f"{action.event_type}"
                    ),
                }
            )
        return ()

    def post_segment_function(context: Any) -> None:
        captured_cash[context.i] = float(context.last_cash[context.group])
        captured_nav[context.i] = float(context.last_value[context.group])
        for column_index in range(context.from_col, context.to_col):
            captured_positions[context.i, column_index] = float(
                context.last_position[column_index]
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
            pre_segment_func_nb=pre_segment_function,
            post_segment_func_nb=post_segment_function,
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
        if (intent["execution_date"], intent["symbol"])
        in tradable_intents
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
        is_stale = int(intent["sequence"]) in stale_intent_sequences
        is_tradable = (
            intent["execution_date"], intent["symbol"]
        ) in tradable_intents
        expected_side = 0 if intent["side"] == "BUY" else 1
        if is_stale:
            if record is not None:
                raise EngineValidationError(
                    "VectorBT received an order invalidated by a corporate action"
                )
            filled_quantity = 0
            fill_price = float(prices[row_index, column_index])
            recorded_fees = 0.0
        elif not is_tradable:
            if record is not None:
                raise EngineValidationError(
                    "VectorBT received an order for a non-tradable observation"
                )
            filled_quantity = 0
            fill_price = float(prices[row_index, column_index])
            recorded_fees = 0.0
        elif record is None:
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
        if is_stale:
            status = "rejected_corporate_action"
        elif not is_tradable:
            status = "rejected_not_tradable"
        elif filled_quantity == 0:
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
    intent_sequences = {
        (intent["execution_date"], intent["symbol"]): intent["sequence"]
        for intent in intents
    }
    trade_rows.sort(
        key=lambda row: (
            date_indexes[row["execution_date"]],
            {
                "rejected_corporate_action": 0,
                "rejected_not_tradable": 1,
            }.get(row["status"], 2),
            intent_sequences[(row["execution_date"], row["symbol"])],
        )
    )

    if (
        np.isnan(captured_cash).any()
        or np.isnan(captured_nav).any()
        or np.isnan(captured_positions).any()
    ):
        raise EngineValidationError(
            "VectorBT did not expose a complete daily simulation state"
        )
    nav_rows: List[Dict[str, Any]] = []
    for row_index, trading_date in enumerate(trading_dates):
        positions: Dict[str, int] = {}
        for symbol in symbols:
            quantity_float = float(
                captured_positions[row_index, symbol_indexes[symbol]]
            )
            quantity = int(round(quantity_float))
            if abs(quantity_float - quantity) > 1e-8 or quantity < 0:
                raise EngineValidationError(
                    "VectorBT returned invalid long-only positions"
                )
            positions[symbol] = quantity
        nav_rows.append(
            {
                "date": trading_date,
                "nav": float(captured_nav[row_index]),
                "cash": float(captured_cash[row_index]),
                "positions": positions,
            }
        )

    metrics = summarize_candidate(nav_rows, trade_rows, action_rows)
    market_scope: Dict[str, Any] = {
        "reference_data_source": market_input.source_type,
        "prices_sha256": file_sha256(market_input.prices_path),
        "price_adjustment": market.price_adjustment,
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
            "name": "vectorbt",
            "version": vbt.__version__,
            "adapter_version": ADAPTER_VERSION,
            "python_version": platform.python_version(),
            "numpy_version": np.__version__,
            "pandas_version": pd.__version__,
        },
        nav_rows=nav_rows,
        trade_rows=trade_rows,
        corporate_action_rows=action_rows,
        metrics=metrics,
        validation_scope={
            "input": "frozen_order_intents",
            "validated_components": [
                "adapter output contract and reference-input binding",
                "VectorBT order records after semantic lowering",
                "corporate-action event journal and resulting account state",
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
                "cash dividends, splits, reverse splits, and stale-intent "
                "rejection applied before same-day orders",
            ],
            "excluded_components": [
                "strategy signal generation and point-in-time feature logic",
                "market data correctness and provider lineage",
                "correctness of adjusted-price factors",
                "order book, liquidity, price limits, and intraday execution",
            ],
            **market_scope,
        },
        limitations=[
            "Parity validates software behavior for this frozen input only; it "
            "does not establish strategy or investment validity.",
            "Adapter v3 accepts standalone prices or validated curated daily "
            "datasets, long-only runs with zero starting positions, and at "
            "most one order per symbol/date.",
            "Curated tradability rejection is adapter logic; VectorBT does not "
            "natively model suspensions in this integration.",
            "Corporate actions mutate VectorBT simulation state through adapter "
            "callbacks; they are independently journaled but are not a native "
            "VectorBT account feature.",
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
