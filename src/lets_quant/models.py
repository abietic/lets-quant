from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set


@dataclass(frozen=True)
class StrategyPolicy:
    kind: str
    target_weights: Dict[str, float]
    rebalance_every_n_trading_days: int
    lookback_trading_days: Optional[int] = None
    minimum_momentum: Optional[float] = None


@dataclass(frozen=True)
class PortfolioPolicy:
    initial_cash: float
    benchmark: Optional[str]
    cash_buffer_weight: float


@dataclass(frozen=True)
class ExecutionPolicy:
    mode: str
    lot_size: int
    commission_rate: float
    minimum_commission: float
    sell_tax_rate: float
    slippage_bps: float


@dataclass(frozen=True)
class RiskPolicy:
    max_single_weight: float
    max_gross_exposure: float
    max_turnover_per_rebalance: float
    max_drawdown: float


@dataclass(frozen=True)
class Policy:
    schema_version: int
    name: str
    base_currency: str
    strategy: StrategyPolicy
    portfolio: PortfolioPolicy
    execution: ExecutionPolicy
    risk: RiskPolicy

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    event_type: str
    ex_date: date
    announced_at: Optional[datetime] = None
    cash_amount: Optional[float] = None
    ratio: Optional[float] = None
    available_at: Optional[datetime] = None


@dataclass(frozen=True)
class MarketData:
    dates: List[date]
    prices_by_date: Dict[date, Dict[str, float]]
    tradable_by_date: Optional[Dict[date, Set[str]]] = None
    corporate_actions_by_date: Optional[
        Dict[date, List[CorporateAction]]
    ] = None
    price_adjustment: str = "none"

    def __post_init__(self) -> None:
        if self.price_adjustment not in {"none", "qfq", "hfq"}:
            raise ValueError(
                "market price_adjustment must be none, qfq, or hfq"
            )
        if self.dates != sorted(set(self.dates)):
            raise ValueError("market dates must be unique and sorted")
        if self.corporate_actions_by_date is not None:
            for trading_date, actions in self.corporate_actions_by_date.items():
                if any(action.ex_date != trading_date for action in actions):
                    raise ValueError(
                        "corporate action map key must match action ex_date"
                    )

    @property
    def symbols(self) -> List[str]:
        symbols = set()
        for prices in self.prices_by_date.values():
            symbols.update(prices)
        return sorted(symbols)

    def prices_on(self, trading_date: date) -> Dict[str, float]:
        return self.prices_by_date[trading_date]

    def is_tradable(self, trading_date: date, symbol: str) -> bool:
        if self.tradable_by_date is None:
            return True
        return symbol in self.tradable_by_date.get(trading_date, set())

    def corporate_actions_on(
        self, trading_date: date
    ) -> List[CorporateAction]:
        if self.corporate_actions_by_date is None:
            return []
        return list(self.corporate_actions_by_date.get(trading_date, []))


@dataclass(frozen=True)
class OrderIntent:
    signal_date: date
    execution_date: date
    symbol: str
    side: str
    quantity: int
    signal_price: float
    reason: str


@dataclass(frozen=True)
class StrategyDecision:
    decision_id: str
    as_of: date
    strategy_kind: str
    status: str
    reason: str
    target_weights: Dict[str, float]
    evidence: Dict[str, Any]
    diagnostics: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SignalRecord:
    signal_date: date
    execution_date: date
    status: str
    estimated_turnover: float
    reason: str
    orders: List[OrderIntent] = field(default_factory=list)
    decision_id: Optional[str] = None
    strategy_kind: Optional[str] = None
    target_weights: Dict[str, float] = field(default_factory=dict)
    decision_evidence: Dict[str, Any] = field(default_factory=dict)
    diagnostics: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class TradeRecord:
    signal_date: date
    execution_date: date
    symbol: str
    side: str
    requested_quantity: int
    filled_quantity: int
    signal_price: float
    market_price: float
    fill_price: float
    gross_notional: float
    commission: float
    tax: float
    slippage_cost: float
    status: str


@dataclass(frozen=True)
class NavRecord:
    trading_date: date
    nav: float
    cash: float
    drawdown: float
    risk_frozen: bool
    positions: Dict[str, int]


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    sequence: int
    trading_date: date
    event_type: str
    symbol: Optional[str]
    quantity_delta: int
    cash_delta: float
    expense: float
    reference_id: str
    description: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountingRecord:
    trading_date: date
    status: str
    ledger_entry_count: int
    cash: float
    expected_cash: float
    market_value: float
    expected_market_value: float
    nav: float
    expected_nav: float
    cash_error: float
    nav_error: float
    positions: Dict[str, int]
    expected_positions: Dict[str, int]
    position_errors: Dict[str, int]


@dataclass(frozen=True)
class BacktestResult:
    metrics: Dict[str, Any]
    nav: List[NavRecord]
    signals: List[SignalRecord]
    trades: List[TradeRecord]
    assumptions: List[str]
    ledger: List[LedgerEntry] = field(default_factory=list)
    accounting: List[AccountingRecord] = field(default_factory=list)


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: int


@dataclass(frozen=True)
class OrderRecommendation:
    symbol: str
    side: str
    quantity: int
    reference_price: float
    estimated_fill_price: float
    estimated_notional: float
    estimated_fees: float
    current_quantity: int
    target_quantity: int


@dataclass(frozen=True)
class ManualOrderPlan:
    as_of: date
    policy_name: str
    nav: float
    cash: float
    estimated_turnover: float
    approval_required: bool
    automatic_execution_allowed: bool
    status: str
    violations: List[str]
    recommendations: List[OrderRecommendation]
    decision_id: Optional[str] = None
    strategy_kind: Optional[str] = None
    target_weights: Dict[str, float] = field(default_factory=dict)
    decision_evidence: Dict[str, Any] = field(default_factory=dict)
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["as_of"] = self.as_of.isoformat()
        return payload
