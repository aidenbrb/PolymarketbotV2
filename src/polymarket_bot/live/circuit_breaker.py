"""Daily-loss circuit breaker. Defaults ON (config.CircuitBreakerSettings) --
this is the recommended, easily-overridable safety net (set
CIRCUIT_BREAKER_ENABLED=false to fully disable it, which is your explicit
choice to make).

Reads/writes a small state file fresh on every check (never cached in
memory) so a separate `live-reset-breaker` CLI invocation -- a different OS
process -- can clear a halt just by rewriting the file, and the already
running live-start process picks it up on its next refresh cycle with no
socket/RPC machinery.
"""

from __future__ import annotations

from typing import Any, Optional

from .. import config, storage
from ..logger import get_logger
from ..models import utcnow_iso
from .cancel_safeguard import cancel_all_and_verify
from .us_client import LiveUsClient

logger = get_logger("live.circuit_breaker")

STATE_FILE = config.LIVE_TRADES_DIR / "circuit_breaker_state.json"


class CircuitBreaker:
    def __init__(self, settings: Optional[config.CircuitBreakerSettings] = None):
        self.settings = settings or config.load_settings().circuit_breaker

    def is_halted(self) -> bool:
        state = storage.load_json(STATE_FILE, default={})
        return bool(state.get("halted", False))

    def evaluate(
        self,
        total_pnl_usd: float,
        client: LiveUsClient,
        open_orders: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        """Returns True if trading should be halted (already halted, or just
        tripped by this check). If enabled and today's P/L crosses
        -daily_loss_limit_usd, cancels all resting orders and persists a halt."""
        if not self.settings.enabled:
            return False

        state = storage.load_json(STATE_FILE, default={})
        if state.get("halted", False):
            cancel_all_and_verify(
                client,
                open_orders=open_orders,
                context="daily circuit breaker already halted",
            )
            return True

        # A manual reset explicitly accepts the loss already incurred today
        # and grants a fresh allowance from that point. The adjustment is
        # date-scoped so it cannot leak into the next UTC day's genuinely
        # fresh daily P/L calculation.
        today = utcnow_iso()[:10]
        reset_baseline = 0.0
        if state.get("reset_baseline_date") == today:
            try:
                reset_baseline = float(state.get("reset_baseline_pnl_usd", 0.0))
            except (TypeError, ValueError):
                reset_baseline = 0.0
        effective_pnl_usd = round(total_pnl_usd - reset_baseline, 4)

        if effective_pnl_usd <= -abs(self.settings.daily_loss_limit_usd):
            logger.warning(
                "Circuit breaker tripped: protected daily P/L $%.2f crossed "
                "-$%.2f limit (raw daily P/L $%.2f, reset baseline $%.2f). "
                "Cancelling all resting orders and halting.",
                effective_pnl_usd, self.settings.daily_loss_limit_usd,
                total_pnl_usd, reset_baseline,
            )
            self._persist_halt(total_pnl_usd, effective_pnl_usd, state)
            cancel_all_and_verify(
                client,
                open_orders=open_orders,
                context="daily circuit breaker trip",
            )
            return True

        return False

    def reset(self) -> None:
        state = storage.load_json(STATE_FILE, default={})
        reset_at = utcnow_iso()
        reset_date = reset_at[:10]
        try:
            baseline = (
                float(state.get("pnl_at_trip_usd", 0.0))
                if str(state.get("tripped_at", ""))[:10] == reset_date
                else 0.0
            )
        except (TypeError, ValueError):
            baseline = 0.0
        storage.save_json(
            STATE_FILE,
            {
                "halted": False,
                "reset_at": reset_at,
                "reset_baseline_date": reset_date,
                "reset_baseline_pnl_usd": baseline,
            },
        )
        logger.warning(
            "Circuit breaker manually reset with today's P/L baseline at $%.2f.",
            baseline,
        )

    def _persist_halt(
        self,
        total_pnl_usd: float,
        effective_pnl_usd: float,
        prior_state: dict[str, Any],
    ) -> None:
        storage.save_json(
            STATE_FILE,
            {
                "halted": True,
                "tripped_at": utcnow_iso(),
                "pnl_at_trip_usd": total_pnl_usd,
                "protected_pnl_at_trip_usd": effective_pnl_usd,
                "reset_baseline_date": prior_state.get("reset_baseline_date"),
                "reset_baseline_pnl_usd": prior_state.get("reset_baseline_pnl_usd", 0.0),
            },
        )


class SessionCircuitBreaker:
    """Loss limit measured since THIS live-start process began, separate
    from CircuitBreaker's UTC-midnight-resetting daily limit. Deliberately
    **in-memory only, no state file** -- a fresh baseline captured on first
    use already gives "resets every live-start" for free, with no explicit
    reset logic needed. This also means, unlike CircuitBreaker, a separate
    CLI invocation cannot reach in and clear a halt (there's no shared file
    to rewrite) -- restarting live-start is the only reset path, which is
    the intended behavior, not a gap."""

    def __init__(self, settings: Optional[config.SessionCircuitBreakerSettings] = None):
        self.settings = settings or config.load_settings().session_circuit_breaker
        self._halted = False
        self._baseline: Optional[float] = None

    def is_halted(self) -> bool:
        return self._halted

    def diff(self, total_pnl_usd: float) -> float:
        """Lazily captures a baseline on first call (returns 0.0 regardless
        of the raw figure's sign -- this breaker only cares about movement
        since it started watching); every later call returns the diff
        against that captured baseline."""
        if self._baseline is None:
            self._baseline = total_pnl_usd
            return 0.0
        return round(total_pnl_usd - self._baseline, 4)

    def evaluate(
        self,
        total_pnl_usd: float,
        client: LiveUsClient,
        open_orders: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        """Same contract as CircuitBreaker.evaluate() -- total_pnl_usd is
        already a diffed figure (see diff() above), not raw/cumulative."""
        if not self.settings.enabled:
            return False

        if self._halted:
            cancel_all_and_verify(
                client,
                open_orders=open_orders,
                context="session circuit breaker already halted",
            )
            return True

        if total_pnl_usd <= -abs(self.settings.loss_limit_usd):
            logger.warning(
                "Session circuit breaker tripped: session P/L $%.2f crossed -$%.2f limit. "
                "Cancelling all resting orders and halting.",
                total_pnl_usd, self.settings.loss_limit_usd,
            )
            self._halted = True
            cancel_all_and_verify(
                client,
                open_orders=open_orders,
                context="session circuit breaker trip",
            )
            return True

        return False

    def reset(self) -> None:
        self._halted = False
        logger.warning("Session circuit breaker manually reset.")
