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

from typing import Optional

from .. import config, storage
from ..logger import get_logger
from ..models import utcnow_iso
from .us_client import LiveUsClient

logger = get_logger("live.circuit_breaker")

STATE_FILE = config.LIVE_TRADES_DIR / "circuit_breaker_state.json"


class CircuitBreaker:
    def __init__(self, settings: Optional[config.CircuitBreakerSettings] = None):
        self.settings = settings or config.load_settings().circuit_breaker

    def is_halted(self) -> bool:
        state = storage.load_json(STATE_FILE, default={})
        return bool(state.get("halted", False))

    def evaluate(self, total_pnl_usd: float, client: LiveUsClient) -> bool:
        """Returns True if trading should be halted (already halted, or just
        tripped by this check). If enabled and today's P/L crosses
        -daily_loss_limit_usd, cancels all resting orders and persists a halt."""
        if not self.settings.enabled:
            return False

        if self.is_halted():
            return True

        if total_pnl_usd <= -abs(self.settings.daily_loss_limit_usd):
            logger.warning(
                "Circuit breaker tripped: daily P/L $%.2f crossed -$%.2f limit. "
                "Cancelling all resting orders and halting.",
                total_pnl_usd, self.settings.daily_loss_limit_usd,
            )
            client.cancel_all()
            self._persist_halt(total_pnl_usd)
            return True

        return False

    def reset(self) -> None:
        storage.save_json(STATE_FILE, {"halted": False})
        logger.warning("Circuit breaker manually reset.")

    def _persist_halt(self, total_pnl_usd: float) -> None:
        storage.save_json(
            STATE_FILE,
            {"halted": True, "tripped_at": utcnow_iso(), "pnl_at_trip_usd": total_pnl_usd},
        )
