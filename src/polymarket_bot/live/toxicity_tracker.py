"""Per-market EWMA of realized-spread markout (live/fills.py's
markout_1m_cents), used to trigger a temporary risk-off "cooldown" for a
specific market when its recent fills look adverse -- see
live/RUNBOOK.md's "-9." section.

Mirrors live/volatility_filter.py::VolatilityTracker's shape and framing
exactly: a threading.RLock-guarded, per-market, in-memory-only tracker.
Deliberately no persistence across process restarts -- a safety guard, not
an analytics/research tool (that's what fills.json is for). A restart
clearing cooldown state is the same accepted tradeoff already made for
VolatilityTracker.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class ToxicityTracker:
    def __init__(
        self,
        alpha: float = 0.3,
        adverse_threshold_cents: float = -1.0,
        cooldown_seconds: float = 600.0,
    ):
        self.alpha = alpha
        self.adverse_threshold_cents = adverse_threshold_cents
        self.cooldown_seconds = cooldown_seconds
        self._ewma: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._lock = threading.RLock()

    def record_markout(self, market_slug: str, markout_cents: float) -> None:
        """EWMA update -- the first observation for a market seeds it
        directly (no smoothing against a nonexistent prior). If the new
        EWMA crosses adverse_threshold_cents, (re-)starts a cooldown: a
        renewed adverse observation DURING an active cooldown extends
        cooldown_until further out; a favorable observation during cooldown
        does NOT shorten/cancel it early."""
        with self._lock:
            if market_slug in self._ewma:
                new_ewma = self.alpha * markout_cents + (1 - self.alpha) * self._ewma[market_slug]
            else:
                new_ewma = markout_cents
            self._ewma[market_slug] = new_ewma
            if new_ewma <= self.adverse_threshold_cents:
                self._cooldown_until[market_slug] = time.monotonic() + self.cooldown_seconds

    def is_in_cooldown(self, market_slug: str) -> bool:
        with self._lock:
            until = self._cooldown_until.get(market_slug)
            return until is not None and time.monotonic() < until

    def get_ewma(self, market_slug: str) -> Optional[float]:
        with self._lock:
            return self._ewma.get(market_slug)
