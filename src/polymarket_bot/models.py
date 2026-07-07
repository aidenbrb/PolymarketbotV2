"""Shared dataclasses used across the pipeline.

No field in this module holds a wallet address, private key, or API secret.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Market:
    """A normalized Polymarket market record (public data only)."""

    market_id: str
    event_id: Optional[str]
    question: str
    category: Optional[str]
    tags: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    outcome_prices: list[float] = field(default_factory=list)
    token_ids: list[str] = field(default_factory=list)
    volume_24h: float = 0.0
    liquidity: float = 0.0
    end_date: Optional[str] = None
    closed: bool = False
    active: bool = True
    has_order_book: bool = False
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    midpoint: Optional[float] = None
    spread: Optional[float] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d.pop("raw", None)
        return d


@dataclass
class FilterResult:
    market_id: str
    question: str
    accepted: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class ScoredMarket:
    market_id: str
    question: str
    total_score: float
    component_scores: dict[str, float]
    explanation: list[str]
    recommendation: str  # REJECT | WATCH | STRONG_WATCH | PAPER_CANDIDATE
    market: Optional[Market] = None


@dataclass
class PaperTrade:
    trade_id: str
    market_id: str
    token_id: Optional[str]
    outcome: str
    side: str  # "BUY" or "SELL"/"CLOSE"
    entry_price: float
    size_usd: float
    shares: float
    entry_time: str
    reason_entry: str
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    reason_exit: Optional[str] = None
    status: str = "OPEN"  # OPEN | CLOSED
    realized_pnl: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class PortfolioSnapshot:
    timestamp: str
    cash: float
    open_positions_value: float
    total_equity: float
    realized_pnl: float
    unrealized_pnl: float
    win_rate: float
    avg_return_pct: float
    max_drawdown_pct: float
    num_open_positions: int
    num_closed_trades: int

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)
