from __future__ import annotations

import math
import statistics
from datetime import date
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .accounting import AccountingError, AccountingLedger
from .data import validate_market_coverage
from .models import (
    BacktestResult,
    Holding,
    MarketData,
    NavRecord,
    OrderIntent,
    Policy,
    SignalRecord,
    TradeRecord,
)
from .risk import (
    estimate_turnover,
    floor_to_lot,
    rebalance_violations,
    target_quantities,
)
from .strategies import (
    HistoricalContext,
    Strategy,
    StrategyError,
    build_strategy,
    validate_strategy_decision,
)


BASE_ASSUMPTIONS = [
    "Orders use whole lots. Sells execute before buys; buys may be reduced "
    "when cash is insufficient.",
    "Commission, sell tax, and symmetric slippage come only from the policy "
    "configuration.",
    "The simulator has no order book, intraday liquidity, price-limit, "
    "or partial-fill probability model. Curated datasets can mark a symbol "
    "non-tradable; such orders are rejected rather than queued.",
    "Input prices must use one declared adjustment convention. Corporate "
    "actions are posted explicitly for unadjusted prices and recorded as "
    "embedded for adjusted prices. Missing prices are rejected.",
    "For unadjusted prices, a pending order that crosses a split or reverse "
    "split is rejected because its original share quantity is stale.",
    "When adjusted prices are used, whole-lot quantities and transaction costs "
    "are research approximations rather than executable historical amounts.",
    "This reference simulator never connects to a broker and is not a live "
    "execution engine.",
]


def _assumptions(
    execution_delay_trading_days: int,
    market: MarketData,
    initial_positions: Mapping[str, int],
) -> List[str]:
    opening_assumptions = (
        [
            "Configured initial cash remains cash in addition to imported "
            "opening positions; returns and baselines start from first-day NAV."
        ]
        if any(initial_positions.values())
        else []
    )
    return [
        "Signals are generated after a daily close and execute "
        f"{execution_delay_trading_days} trading day(s) later at the close.",
        f"Market price adjustment convention is {market.price_adjustment}.",
        *opening_assumptions,
        *BASE_ASSUMPTIONS,
    ]


def _portfolio_value(
    cash: float, positions: Mapping[str, int], prices: Mapping[str, float]
) -> float:
    return cash + sum(
        quantity * prices[symbol] for symbol, quantity in positions.items()
    )


def _commission(policy: Policy, notional: float) -> float:
    if notional <= 0:
        return 0.0
    return max(
        policy.execution.minimum_commission,
        notional * policy.execution.commission_rate,
    )


def _affordable_buy_quantity(
    policy: Policy,
    requested_quantity: int,
    fill_price: float,
    available_cash: float,
) -> int:
    candidate = min(
        requested_quantity,
        floor_to_lot(
            available_cash / fill_price,
            policy.execution.lot_size,
        ),
    )
    while candidate > 0:
        notional = candidate * fill_price
        if notional + _commission(policy, notional) <= available_cash + 1e-9:
            return candidate
        candidate -= policy.execution.lot_size
    return 0


def _execute_orders(
    policy: Policy,
    orders: Sequence[OrderIntent],
    execution_prices: Mapping[str, float],
    cash: float,
    positions: Dict[str, int],
) -> Tuple[float, List[TradeRecord]]:
    trades: List[TradeRecord] = []
    slippage_fraction = policy.execution.slippage_bps / 10_000
    ordered = sorted(orders, key=lambda item: 0 if item.side == "SELL" else 1)

    for order in ordered:
        market_price = execution_prices[order.symbol]
        if order.side == "SELL":
            fill_price = market_price * (1 - slippage_fraction)
            filled_quantity = min(
                order.quantity, positions.get(order.symbol, 0)
            )
        else:
            fill_price = market_price * (1 + slippage_fraction)
            current_nav = _portfolio_value(cash, positions, execution_prices)
            minimum_cash = current_nav * policy.portfolio.cash_buffer_weight
            available_cash = max(0.0, cash - minimum_cash)
            filled_quantity = _affordable_buy_quantity(
                policy,
                order.quantity,
                fill_price,
                available_cash,
            )

        gross_notional = filled_quantity * fill_price
        commission = _commission(policy, gross_notional)
        tax = (
            gross_notional * policy.execution.sell_tax_rate
            if order.side == "SELL"
            else 0.0
        )
        slippage_cost = (
            abs(fill_price - market_price) * filled_quantity
        )

        if order.side == "SELL":
            positions[order.symbol] = (
                positions.get(order.symbol, 0) - filled_quantity
            )
            cash += gross_notional - commission - tax
        else:
            positions[order.symbol] = (
                positions.get(order.symbol, 0) + filled_quantity
            )
            cash -= gross_notional + commission

        if filled_quantity == 0:
            status = "rejected"
        elif filled_quantity < order.quantity:
            status = "partial"
        else:
            status = "filled"

        trades.append(
            TradeRecord(
                signal_date=order.signal_date,
                execution_date=order.execution_date,
                symbol=order.symbol,
                side=order.side,
                requested_quantity=order.quantity,
                filled_quantity=filled_quantity,
                signal_price=order.signal_price,
                market_price=market_price,
                fill_price=fill_price,
                gross_notional=gross_notional,
                commission=commission,
                tax=tax,
                slippage_cost=slippage_cost,
                status=status,
            )
        )
    return cash, trades


def _reject_non_tradable_order(
    order: OrderIntent, market_price: float
) -> TradeRecord:
    return TradeRecord(
        signal_date=order.signal_date,
        execution_date=order.execution_date,
        symbol=order.symbol,
        side=order.side,
        requested_quantity=order.quantity,
        filled_quantity=0,
        signal_price=order.signal_price,
        market_price=market_price,
        fill_price=market_price,
        gross_notional=0.0,
        commission=0.0,
        tax=0.0,
        slippage_cost=0.0,
        status="rejected_not_tradable",
    )


def _crosses_quantity_changing_action(
    market: MarketData, order: OrderIntent
) -> bool:
    if market.price_adjustment != "none":
        return False
    return any(
        action.symbol == order.symbol
        and action.event_type in {"split", "reverse_split"}
        and order.signal_date < action.ex_date <= order.execution_date
        for actions in (market.corporate_actions_by_date or {}).values()
        for action in actions
    )


def _reject_stale_quantity_order(
    order: OrderIntent, market_price: float
) -> TradeRecord:
    return TradeRecord(
        signal_date=order.signal_date,
        execution_date=order.execution_date,
        symbol=order.symbol,
        side=order.side,
        requested_quantity=order.quantity,
        filled_quantity=0,
        signal_price=order.signal_price,
        market_price=market_price,
        fill_price=market_price,
        gross_notional=0.0,
        commission=0.0,
        tax=0.0,
        slippage_cost=0.0,
        status="rejected_corporate_action",
    )


def _build_order_intents(
    policy: Policy,
    signal_date: date,
    execution_date: date,
    prices: Mapping[str, float],
    positions: Mapping[str, int],
    desired: Mapping[str, int],
    reason: str,
) -> List[OrderIntent]:
    orders: List[OrderIntent] = []
    for symbol in sorted(desired):
        delta = desired[symbol] - positions.get(symbol, 0)
        if delta == 0:
            continue
        orders.append(
            OrderIntent(
                signal_date=signal_date,
                execution_date=execution_date,
                symbol=symbol,
                side="BUY" if delta > 0 else "SELL",
                quantity=abs(delta),
                signal_price=prices[symbol],
                reason=reason,
            )
        )
    return orders


def _drawdown(values: Iterable[float]) -> float:
    peak = -float("inf")
    maximum_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            maximum_drawdown = min(maximum_drawdown, value / peak - 1)
    return maximum_drawdown


def _drawdown_duration(values: Sequence[float]) -> Tuple[int, float]:
    peak = -float("inf")
    current_duration = 0
    maximum_duration = 0
    underwater_days = 0
    for value in values:
        if value >= peak:
            peak = value
            current_duration = 0
            continue
        current_duration += 1
        underwater_days += 1
        maximum_duration = max(maximum_duration, current_duration)
    ratio = underwater_days / len(values) if values else 0.0
    return maximum_duration, ratio


def _performance_metrics(
    values: Sequence[float], dates: Sequence[date]
) -> Dict[str, float]:
    if not values:
        return {
            "total_return": 0.0,
            "annualized_return": 0.0,
            "annualized_volatility": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "calmar_ratio": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_duration_trading_days": 0,
            "time_underwater_ratio": 0.0,
        }

    total_return = values[-1] / values[0] - 1
    elapsed_days = max(1, (dates[-1] - dates[0]).days)
    if total_return <= -1:
        annualized_return = -1.0
    else:
        annualized_return = (1 + total_return) ** (365.25 / elapsed_days) - 1

    daily_returns = [
        current / previous - 1
        for previous, current in zip(values, values[1:])
        if previous != 0
    ]
    if len(daily_returns) >= 2:
        daily_std = statistics.stdev(daily_returns)
        annualized_volatility = daily_std * math.sqrt(252)
        sharpe = (
            statistics.mean(daily_returns) / daily_std * math.sqrt(252)
            if daily_std > 0
            else 0.0
        )
        downside_deviation = math.sqrt(
            statistics.mean(
                min(daily_return, 0.0) ** 2
                for daily_return in daily_returns
            )
        )
        sortino = (
            statistics.mean(daily_returns)
            / downside_deviation
            * math.sqrt(252)
            if downside_deviation > 0
            else 0.0
        )
    else:
        annualized_volatility = 0.0
        sharpe = 0.0
        sortino = 0.0

    max_drawdown = _drawdown(values)
    max_duration, underwater_ratio = _drawdown_duration(values)
    calmar = (
        annualized_return / abs(max_drawdown)
        if max_drawdown < 0
        else 0.0
    )

    return {
        "total_return": total_return,
        "annualized_return": annualized_return,
        "annualized_volatility": annualized_volatility,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "max_drawdown": max_drawdown,
        "max_drawdown_duration_trading_days": max_duration,
        "time_underwater_ratio": underwater_ratio,
    }


def _active_metrics(
    strategy_values: Sequence[float], benchmark_values: Sequence[float]
) -> Dict[str, float]:
    strategy_returns = [
        current / previous - 1
        for previous, current in zip(strategy_values, strategy_values[1:])
        if previous != 0
    ]
    benchmark_returns = [
        current / previous - 1
        for previous, current in zip(benchmark_values, benchmark_values[1:])
        if previous != 0
    ]
    active_returns = [
        strategy_return - benchmark_return
        for strategy_return, benchmark_return in zip(
            strategy_returns, benchmark_returns
        )
    ]
    if len(active_returns) < 2:
        return {"tracking_error": 0.0, "information_ratio": 0.0}
    active_std = statistics.stdev(active_returns)
    tracking_error = active_std * math.sqrt(252)
    information_ratio = (
        statistics.mean(active_returns) / active_std * math.sqrt(252)
        if active_std > 0
        else 0.0
    )
    return {
        "tracking_error": tracking_error,
        "information_ratio": information_ratio,
    }


def _buy_and_hold_values(
    initial_cash: float,
    target_weights: Mapping[str, float],
    market: MarketData,
) -> List[float]:
    initial_prices = market.prices_on(market.dates[0])
    cash = initial_cash * (1.0 - sum(target_weights.values()))
    quantities = {
        symbol: initial_cash * weight / initial_prices[symbol]
        for symbol, weight in target_weights.items()
    }
    values: List[float] = []
    for index, trading_date in enumerate(market.dates):
        if index > 0 and market.price_adjustment == "none":
            for action in market.corporate_actions_on(trading_date):
                quantity = quantities.get(action.symbol, 0.0)
                if action.event_type == "cash_dividend":
                    cash += quantity * (action.cash_amount or 0.0)
                elif action.ratio is not None:
                    quantities[action.symbol] = quantity * action.ratio
        prices = market.prices_on(trading_date)
        values.append(
            cash
            + sum(
                quantity * prices[symbol]
                for symbol, quantity in quantities.items()
            )
        )
    return values


def _static_target_values(
    policy: Policy, market: MarketData, initial_capital: float
) -> List[float]:
    return _buy_and_hold_values(
        initial_capital,
        policy.strategy.target_weights,
        market,
    )


def _opening_positions(
    policy: Policy, initial_holdings: Iterable[Holding]
) -> Dict[str, int]:
    positions = {symbol: 0 for symbol in policy.strategy.target_weights}
    seen = set()
    for holding in initial_holdings:
        symbol = str(holding.symbol).strip().upper()
        quantity = holding.quantity
        if not symbol:
            raise StrategyError("initial holding symbol must not be empty")
        if symbol in seen:
            raise StrategyError(f"duplicate initial holding for {symbol}")
        seen.add(symbol)
        if symbol not in positions:
            raise StrategyError(
                f"initial holding is outside the strategy scope: {symbol}"
            )
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 0
        ):
            raise StrategyError(
                f"initial holding for {symbol} must be an integer >= 0"
            )
        if quantity % policy.execution.lot_size != 0:
            raise StrategyError(
                f"initial holding for {symbol} must be a multiple of lot_size"
            )
        positions[symbol] = quantity
    return positions


def _evaluation_market(
    market: MarketData,
    start_date: Optional[date],
    end_date: Optional[date],
) -> MarketData:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise StrategyError("backtest start_date must be <= end_date")
    dates = [
        trading_date
        for trading_date in market.dates
        if (start_date is None or trading_date >= start_date)
        and (end_date is None or trading_date <= end_date)
    ]
    if not dates:
        raise StrategyError("backtest evaluation window contains no trading days")
    tradable_by_date = None
    if market.tradable_by_date is not None:
        tradable_by_date = {
            trading_date: set(
                market.tradable_by_date.get(trading_date, set())
            )
            for trading_date in dates
        }
    return MarketData(
        dates=dates,
        prices_by_date={
            trading_date: dict(market.prices_on(trading_date))
            for trading_date in dates
        },
        tradable_by_date=tradable_by_date,
        corporate_actions_by_date={
            trading_date: market.corporate_actions_on(trading_date)
            for trading_date in dates
            if market.corporate_actions_on(trading_date)
        },
        price_adjustment=market.price_adjustment,
    )


def run_backtest(
    policy: Policy,
    market: MarketData,
    *,
    strategy: Optional[Strategy] = None,
    execution_delay_trading_days: int = 1,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    initial_holdings: Iterable[Holding] = (),
) -> BacktestResult:
    if (
        isinstance(execution_delay_trading_days, bool)
        or not isinstance(execution_delay_trading_days, int)
        or execution_delay_trading_days <= 0
    ):
        raise StrategyError("execution_delay_trading_days must be a positive integer")

    required_symbols = set(policy.strategy.target_weights)
    if policy.portfolio.benchmark:
        required_symbols.add(policy.portfolio.benchmark)
    validate_market_coverage(market, required_symbols)

    evaluation_market = _evaluation_market(market, start_date, end_date)
    evaluation_dates = evaluation_market.dates
    global_indices = {
        trading_date: index
        for index, trading_date in enumerate(market.dates)
    }
    last_evaluation_index = global_indices[evaluation_dates[-1]]
    strategy_instance = strategy or build_strategy(policy)
    positions = _opening_positions(policy, initial_holdings)
    opening_positions = dict(positions)
    cash = policy.portfolio.initial_cash
    pending_orders: List[OrderIntent] = []
    nav_records: List[NavRecord] = []
    signals: List[SignalRecord] = []
    trades: List[TradeRecord] = []
    accounting_records = []
    ledger = AccountingLedger()
    ledger.record_initial_cash(evaluation_dates[0], cash)
    ledger.record_initial_positions(evaluation_dates[0], opening_positions)
    peak_nav = 0.0
    risk_frozen = False

    for evaluation_index, trading_date in enumerate(evaluation_dates):
        prices = market.prices_on(trading_date)
        cash = ledger.apply_corporate_actions(
            market, trading_date, cash, positions
        )

        orders_due = [
            order
            for order in pending_orders
            if order.execution_date == trading_date
        ]
        if orders_due:
            stale_quantity_orders = [
                order
                for order in orders_due
                if _crosses_quantity_changing_action(market, order)
            ]
            trades.extend(
                _reject_stale_quantity_order(order, prices[order.symbol])
                for order in stale_quantity_orders
            )
            executable_orders = [
                order
                for order in orders_due
                if order not in stale_quantity_orders
                if market.is_tradable(trading_date, order.symbol)
            ]
            trades.extend(
                _reject_non_tradable_order(order, prices[order.symbol])
                for order in orders_due
                if order not in stale_quantity_orders
                if not market.is_tradable(trading_date, order.symbol)
            )
            cash, executed = _execute_orders(
                policy,
                executable_orders,
                prices,
                cash,
                positions,
            )
            for executed_trade in executed:
                trades.append(executed_trade)
                ledger.record_trade(executed_trade, len(trades))
            pending_orders = [
                order
                for order in pending_orders
                if order.execution_date != trading_date
            ]

        nav = _portfolio_value(cash, positions, prices)
        peak_nav = max(peak_nav, nav)
        drawdown = nav / peak_nav - 1 if peak_nav > 0 else -1.0
        if drawdown <= -policy.risk.max_drawdown:
            risk_frozen = True

        nav_records.append(
            NavRecord(
                trading_date=trading_date,
                nav=nav,
                cash=cash,
                drawdown=drawdown,
                risk_frozen=risk_frozen,
                positions=dict(positions),
            )
        )
        accounting_record = ledger.reconcile(
            trading_date=trading_date,
            cash=cash,
            positions=positions,
            prices=prices,
            nav=nav,
        )
        if accounting_record.status != "pass":
            raise AccountingError(
                "daily accounting reconciliation failed on "
                f"{trading_date.isoformat()}"
            )
        accounting_records.append(accounting_record)

        is_signal_day = (
            evaluation_index
            % policy.strategy.rebalance_every_n_trading_days
            == 0
        )
        global_index = global_indices[trading_date]
        execution_index = global_index + execution_delay_trading_days
        if (
            not is_signal_day
            or execution_index > last_evaluation_index
        ):
            continue

        execution_date = market.dates[execution_index]
        if risk_frozen:
            signals.append(
                SignalRecord(
                    signal_date=trading_date,
                    execution_date=execution_date,
                    status="blocked",
                    estimated_turnover=0.0,
                    reason="maximum drawdown risk freeze is active",
                )
            )
            continue

        if pending_orders:
            signals.append(
                SignalRecord(
                    signal_date=trading_date,
                    execution_date=execution_date,
                    status="blocked",
                    estimated_turnover=0.0,
                    reason="an earlier rebalance is still pending execution",
                    strategy_kind=policy.strategy.kind,
                )
            )
            continue

        decision = strategy_instance.decide(
            HistoricalContext(market, trading_date)
        )
        validate_strategy_decision(
            policy, decision, expected_as_of=trading_date
        )
        if decision.status != "ready":
            signals.append(
                SignalRecord(
                    signal_date=trading_date,
                    execution_date=execution_date,
                    status="blocked",
                    estimated_turnover=0.0,
                    reason=decision.reason,
                    decision_id=decision.decision_id,
                    strategy_kind=decision.strategy_kind,
                    target_weights=decision.target_weights,
                    decision_evidence=decision.evidence,
                    diagnostics=decision.diagnostics,
                )
            )
            continue

        desired = target_quantities(
            policy, nav, prices, decision.target_weights
        )
        turnover = estimate_turnover(positions, desired, prices, nav)
        violations = rebalance_violations(policy, turnover)
        orders = _build_order_intents(
            policy,
            trading_date,
            execution_date,
            prices,
            positions,
            desired,
            reason=f"{decision.strategy_kind}_rebalance",
        )

        if violations:
            status = "blocked"
            reason = "; ".join(violations)
        elif not orders:
            status = "no_action"
            reason = "portfolio already matches target lots"
        else:
            status = "accepted"
            reason = "passed pre-trade risk checks"
            pending_orders.extend(orders)

        signals.append(
            SignalRecord(
                signal_date=trading_date,
                execution_date=execution_date,
                status=status,
                estimated_turnover=turnover,
                reason=reason,
                orders=orders,
                decision_id=decision.decision_id,
                strategy_kind=decision.strategy_kind,
                target_weights=decision.target_weights,
                decision_evidence=decision.evidence,
                diagnostics=decision.diagnostics,
            )
        )

    nav_values = [record.nav for record in nav_records]
    metrics: Dict[str, Any] = _performance_metrics(
        nav_values, evaluation_dates
    )
    gross_exposures = []
    cash_weights = []
    for record in nav_records:
        prices = market.prices_on(record.trading_date)
        invested = sum(
            quantity * prices[symbol]
            for symbol, quantity in record.positions.items()
        )
        gross_exposures.append(invested / record.nav if record.nav > 0 else 0.0)
        cash_weights.append(record.cash / record.nav if record.nav > 0 else 0.0)
    metrics.update(
        {
            "starting_nav": nav_values[0],
            "ending_nav": nav_values[-1],
            "trading_days": len(nav_records),
            "signal_count": len(signals),
            "decision_count": sum(
                1 for signal in signals if signal.decision_id is not None
            ),
            "filled_trade_count": sum(
                1 for trade in trades if trade.filled_quantity > 0
            ),
            "total_trade_notional": sum(
                trade.gross_notional for trade in trades
            ),
            "total_commission": sum(trade.commission for trade in trades),
            "total_sell_tax": sum(trade.tax for trade in trades),
            "total_slippage_cost": sum(
                trade.slippage_cost for trade in trades
            ),
            "ledger_total_expense": sum(
                entry.expense for entry in ledger.entries
            ),
            "turnover_ratio": (
                sum(trade.gross_notional for trade in trades)
                / statistics.mean(nav_values)
                if nav_values and statistics.mean(nav_values) > 0
                else 0.0
            ),
            "risk_frozen": risk_frozen,
            "accounting_reconciled": all(
                record.status == "pass" for record in accounting_records
            ),
            "ledger_entry_count": len(ledger.entries),
            "corporate_action_entry_count": sum(
                1
                for entry in ledger.entries
                if entry.event_type
                in {
                    "cash_dividend",
                    "split",
                    "reverse_split",
                    "corporate_action_embedded",
                }
            ),
            "total_cash_dividends": sum(
                entry.cash_delta
                for entry in ledger.entries
                if entry.event_type == "cash_dividend"
            ),
            "maximum_accounting_cash_error": max(
                abs(record.cash_error) for record in accounting_records
            ),
            "maximum_accounting_nav_error": max(
                abs(record.nav_error) for record in accounting_records
            ),
            "average_gross_exposure": statistics.mean(gross_exposures),
            "average_cash_weight": statistics.mean(cash_weights),
            "evaluation_window": {
                "start": evaluation_dates[0].isoformat(),
                "end": evaluation_dates[-1].isoformat(),
            },
            "execution_delay_trading_days": execution_delay_trading_days,
            "warnings": (
                [
                    "Fewer than 252 trading days were supplied; annualized "
                    "return, volatility, and Sharpe ratio are unstable."
                ]
                if len(nav_records) < 252
                else []
            ),
        }
    )
    execution_cost = (
        metrics["total_commission"]
        + metrics["total_sell_tax"]
        + metrics["total_slippage_cost"]
    )
    cost_attribution_error = metrics["ledger_total_expense"] - execution_cost
    if abs(cost_attribution_error) > 1e-7:
        raise AccountingError("ledger execution cost attribution failed")
    metrics["cost_attribution_error"] = cost_attribution_error

    initial_capital = nav_values[0]
    cash_values = [initial_capital for _ in evaluation_dates]
    static_target_values = _static_target_values(
        policy, evaluation_market, initial_capital
    )
    metrics["baselines"] = {
        "cash": {
            "description": "hold first recorded NAV in cash; no market exposure",
            **_performance_metrics(cash_values, evaluation_dates),
        },
        "static_target_weights": {
            "description": (
                "buy fractional target weights at the first close and never "
                "rebalance; fees and lot constraints are excluded"
            ),
            **_performance_metrics(static_target_values, evaluation_dates),
        },
    }

    benchmark = policy.portfolio.benchmark
    if benchmark:
        benchmark_values = _buy_and_hold_values(
            initial_capital,
            {benchmark: 1.0},
            evaluation_market,
        )
        benchmark_metrics = _performance_metrics(
            benchmark_values, evaluation_dates
        )
        metrics["benchmark"] = {
            "symbol": benchmark,
            **benchmark_metrics,
            **_active_metrics(nav_values, benchmark_values),
        }

    comparison = {
        "strategy_minus_cash_total_return": (
            metrics["total_return"]
            - metrics["baselines"]["cash"]["total_return"]
        ),
        "strategy_minus_static_target_total_return": (
            metrics["total_return"]
            - metrics["baselines"]["static_target_weights"]["total_return"]
        ),
    }
    if benchmark:
        comparison["strategy_minus_benchmark_total_return"] = (
            metrics["total_return"] - metrics["benchmark"]["total_return"]
        )
    metrics["comparison"] = comparison

    return BacktestResult(
        metrics=metrics,
        nav=nav_records,
        signals=signals,
        trades=trades,
        assumptions=_assumptions(
            execution_delay_trading_days, market, opening_positions
        ),
        ledger=ledger.entries,
        accounting=accounting_records,
    )
