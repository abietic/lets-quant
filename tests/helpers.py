from datetime import date
from typing import Dict, List, Optional, Set

from lets_quant.models import (
    CorporateAction,
    ExecutionPolicy,
    MarketData,
    Policy,
    PortfolioPolicy,
    RiskPolicy,
    StrategyPolicy,
)


def make_policy(
    *,
    strategy_kind: str = "fixed_weight",
    weights: Optional[Dict[str, float]] = None,
    lookback_trading_days: Optional[int] = None,
    minimum_momentum: Optional[float] = None,
    benchmark: Optional[str] = None,
    initial_cash: float = 10_000,
    cash_buffer: float = 0.0,
    rebalance_every: int = 1,
    lot_size: int = 1,
    commission_rate: float = 0.0,
    minimum_commission: float = 0.0,
    sell_tax_rate: float = 0.0,
    slippage_bps: float = 0.0,
    max_single_weight: float = 1.0,
    max_gross_exposure: float = 1.0,
    max_turnover: float = 1.0,
    max_drawdown: float = 0.2,
) -> Policy:
    return Policy(
        schema_version=1,
        name="test-policy",
        base_currency="CNY",
        strategy=StrategyPolicy(
            kind=strategy_kind,
            target_weights=weights or {"AAA": 0.5},
            rebalance_every_n_trading_days=rebalance_every,
            lookback_trading_days=lookback_trading_days,
            minimum_momentum=minimum_momentum,
        ),
        portfolio=PortfolioPolicy(
            initial_cash=initial_cash,
            benchmark=benchmark,
            cash_buffer_weight=cash_buffer,
        ),
        execution=ExecutionPolicy(
            mode="manual",
            lot_size=lot_size,
            commission_rate=commission_rate,
            minimum_commission=minimum_commission,
            sell_tax_rate=sell_tax_rate,
            slippage_bps=slippage_bps,
        ),
        risk=RiskPolicy(
            max_single_weight=max_single_weight,
            max_gross_exposure=max_gross_exposure,
            max_turnover_per_rebalance=max_turnover,
            max_drawdown=max_drawdown,
        ),
    )


def make_market(
    dates: List[date],
    prices: List[Dict[str, float]],
    tradable_by_date: Optional[Dict[date, Set[str]]] = None,
    corporate_actions_by_date: Optional[
        Dict[date, List[CorporateAction]]
    ] = None,
    price_adjustment: str = "none",
) -> MarketData:
    return MarketData(
        dates=dates,
        prices_by_date=dict(zip(dates, prices)),
        tradable_by_date=tradable_by_date,
        corporate_actions_by_date=corporate_actions_by_date,
        price_adjustment=price_adjustment,
    )
