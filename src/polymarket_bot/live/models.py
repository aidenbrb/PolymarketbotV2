"""Live-only dataclasses.

Kept out of the shared top-level models.py, which explicitly claims to hold
no key/secret/live-order material -- deleting this live/ subpackage removes
100% of these data structures with zero edits elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class PostedLeg:
    side: str  # "BUY" or "SELL"
    price: float
    size: float
    order_id: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class LiveQuoteCycle:
    cycle_id: str
    market_id: str  # market slug
    reference_price: float
    tick_size: float
    bid: PostedLeg
    ask: PostedLeg
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "market_id": self.market_id,
            "reference_price": self.reference_price,
            "tick_size": self.tick_size,
            "bid": self.bid.to_dict(),
            "ask": self.ask.to_dict(),
            "timestamp": self.timestamp,
        }


@dataclass
class FillRecord:
    """A single fill/execution detected on the private WebSocket
    (live/ws_private.py), enriched (best-effort) with the bot's own ledger
    data. See live/fills.py::build_fill_record and live/RUNBOOK.md's "-9."
    section -- the private-WS execution schema is unverified against a real
    fill, so every field below except fill_id/execution_type/detected_at/
    raw_execution is honestly Optional and None when it can't be resolved,
    not a guessed/default value."""

    fill_id: str
    order_id: Optional[str]
    market_slug: Optional[str]
    side: Optional[str]
    price: Optional[float]
    shares: Optional[float]
    execution_type: str
    transact_time: Optional[str]  # real exchange fill time (execution["transactTime"]), not detected_at
    quoted_price: Optional[float]
    current_mid_at_detection: Optional[float]
    edge_vs_current_mid_cents: Optional[float]
    fill_quality: Optional[str]
    markout_1m_cents: Optional[float]
    markout_1m_computed_at: Optional[str]
    markout_5m_cents: Optional[float]
    markout_5m_computed_at: Optional[str]
    detected_at: str
    raw_execution: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)
