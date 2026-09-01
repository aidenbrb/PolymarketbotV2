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

    @property
    def is_resting(self) -> bool:
        """True only when this leg actually has live exposure on the
        exchange right now. order_id alone isn't enough to tell: a leg
        that was posted and then immediately cancelled (see
        MarketMaker._unwind_unpaired_entry) deliberately keeps its
        order_id -- so the ledger/execution-backfill can still discover a
        partial fill from the submit-to-cancel race -- even though it has
        nothing resting. size > 0 is what actually distinguishes the two
        once order_id can be set either way."""
        return bool(self.order_id) and self.size > 0

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
    section. The private-WS execution schema has since been verified
    against real fills (outcome, commission_usd, transact_time -- see the
    field comments below and live/RUNBOOK.md's most recent sections), but
    every field below except fill_id/execution_type/detected_at/
    raw_execution stays honestly Optional and None when it can't be
    resolved on a given fill, not a guessed/default value."""

    fill_id: str
    order_id: Optional[str]
    market_slug: Optional[str]
    side: Optional[str]
    price: Optional[float]
    shares: Optional[float]
    # "YES"/"NO", normalized from raw_execution["order"]["outcomeSide"]
    # (falls back to marketMetadata.outcome). Part of true position
    # identity -- market_slug ALONE is not unique: real fills exist on both
    # outcome tokens of the same slug. See live/lot_accounting.py.
    outcome: Optional[str]
    # raw_execution["commissionNotionalCollected"]["value"] as a signed
    # cash adjustment. Real-account verification found negative values reduce
    # P&L; lot accounting therefore adds this signed amount directly.
    commission_usd: Optional[float]
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
    markout_15m_cents: Optional[float]
    markout_15m_computed_at: Optional[str]
    detected_at: str
    raw_execution: dict[str, Any]
    # Executable-price counterparts to markout_1m/5m/15m_cents above: marked
    # against best_bid (BUY) / best_ask (SELL) -- a price actually
    # achievable to close the position -- rather than the midpoint, which
    # can't actually be transacted at. Additive; the mid-based fields above
    # are unchanged and still feed toxicity_tracker.py/family_performance.py.
    executable_markout_1m_cents: Optional[float] = None
    executable_markout_5m_cents: Optional[float] = None
    executable_markout_15m_cents: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)
