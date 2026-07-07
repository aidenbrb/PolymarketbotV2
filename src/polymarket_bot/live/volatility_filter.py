"""Lightweight adverse-selection guard: skip quoting a market that just
moved a lot. The bot's refresh cadence (as fast as 60s) can't react to a
market that's actively repricing between cycles -- quoting into that is a
good way to get picked off by someone with fresher information. This tracks
recently observed reference prices per market in memory and flags a market
as too volatile to quote if the range within a short recent window exceeds
a configurable threshold.

Deliberately minimal: no persistence across process restarts, no history
beyond what's needed for one rolling window. This is a safety guard, not a
research/analytics tool.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class VolatilityTracker:
    def __init__(self, window_seconds: float = 300.0):
        self.window_seconds = window_seconds
        self._history: dict[str, list[tuple[float, float]]] = {}
        self._lock = threading.RLock()

    def record(self, market_slug: str, price: float) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            history = [(t, p) for t, p in self._history.get(market_slug, []) if t >= cutoff]
            history.append((now, price))
            self._history[market_slug] = history

    def recent_move_cents(self, market_slug: str) -> Optional[float]:
        """Range (max - min) of prices recorded for this market within the
        window, in cents. None if fewer than two observations exist yet --
        there's nothing to compare a single price against."""
        with self._lock:
            history = self._history.get(market_slug, [])
            if len(history) < 2:
                return None
            prices = [p for _, p in history]
            return (max(prices) - min(prices)) * 100
