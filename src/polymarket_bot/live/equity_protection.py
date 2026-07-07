"""Two safety rules layered on top of the daily-loss circuit breaker
(circuit_breaker.py): a lifetime/session peak-account-value drawdown stop,
and a same-day profit-lock order-size reduction. See live/RUNBOOK.md's
"-9." section.

Reads/writes a small state file fresh on every check (never cached in
memory), exactly like circuit_breaker.py -- a separate
`live-reset-equity-protection` CLI invocation can clear a halt just by
rewriting the file, and the already-running live-start process picks it up
on its next refresh cycle.

Deliberately does NOT use any account-balance API field for "account
value" -- both buyingPower and currentBalance are documented as unreliable
for money math elsewhere in this codebase (see ledger.py's module
docstring; both caused real false circuit-breaker trips historically).
Account value is instead starting_capital_usd (a number the user sets
manually, since no balance field can be trusted) plus
ledger.get_total_position_pnl_usd() -- the same non-resetting, position-data-
based P/L computation the daily circuit breaker's own daily figure is built
on top of.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import config, storage
from ..logger import get_logger
from ..models import utcnow_iso
from .ledger import get_total_position_pnl_usd
from .us_client import LiveUsClient

logger = get_logger("live.equity_protection")

STATE_FILE = config.LIVE_TRADES_DIR / "equity_protection_state.json"


class EquityProtection:
    def __init__(self, settings: Optional[config.EquityProtectionSettings] = None):
        self.settings = settings or config.load_settings().equity_protection

    def is_halted(self) -> bool:
        state = storage.load_json(STATE_FILE, default={})
        return bool(state.get("halted", False))

    def evaluate(self, total_pnl_usd: float, client: LiveUsClient) -> tuple[bool, float]:
        """Returns (halted, size_multiplier_for_this_cycle). `total_pnl_usd`
        is the DAILY-resetting figure (same one the circuit breaker uses) --
        used only for the same-day profit-lock sizing check. The
        drawdown-from-peak check uses its own lifetime, non-resetting figure
        (get_total_position_pnl_usd), fetched separately below, only when
        starting_capital_usd is configured."""
        if not self.settings.enabled:
            return False, 1.0

        state = storage.load_json(STATE_FILE, default={})
        if state.get("halted", False):
            return True, 1.0

        today = datetime.now(timezone.utc).date().isoformat()
        # Ratchet: one-way within a UTC day, resets at the next day boundary
        # -- mirrors estimate_daily_pnl_usd's own day-boundary reset idiom.
        profit_lock_active = (
            state.get("profit_lock_active", False)
            and state.get("profit_lock_date") == today
        )
        if not profit_lock_active and total_pnl_usd >= self.settings.profit_lock_daily_usd:
            profit_lock_active = True
        state["profit_lock_active"] = profit_lock_active
        state["profit_lock_date"] = today
        size_multiplier = (
            self.settings.profit_lock_size_multiplier if profit_lock_active else 1.0
        )

        if self.settings.starting_capital_usd > 0:
            total_position_pnl = get_total_position_pnl_usd(client)
            if total_position_pnl is None:
                logger.warning(
                    "Could not fetch positions for the equity-protection "
                    "drawdown check -- skipping it this cycle (failing "
                    "open), trading continues."
                )
            else:
                account_value = self.settings.starting_capital_usd + total_position_pnl
                peak = state.get("peak_account_value_usd", account_value)  # bootstrap
                peak = max(peak, account_value)
                state["peak_account_value_usd"] = peak
                threshold = peak * (1 - self.settings.drawdown_from_peak_pct / 100)
                if account_value <= 0 or account_value <= threshold:
                    logger.warning(
                        "Equity protection tripped: account value $%.2f <= "
                        "threshold $%.2f (peak $%.2f). Cancelling all "
                        "resting orders and halting.",
                        account_value, threshold, peak,
                    )
                    client.cancel_all()
                    state["halted"] = True
                    state["halted_at"] = utcnow_iso()
                    state["halted_reason"] = (
                        f"account value ${account_value:.2f} <= threshold "
                        f"${threshold:.2f} (peak ${peak:.2f})"
                    )
                    storage.save_json(STATE_FILE, state)
                    return True, 1.0
        elif not state.get("starting_capital_warned"):
            logger.warning(
                "EQUITY_PROTECTION_STARTING_CAPITAL_USD is not set -- "
                "drawdown-from-peak protection is INACTIVE. Only same-day "
                "profit-lock sizing is active."
            )
            state["starting_capital_warned"] = True

        storage.save_json(STATE_FILE, state)
        return False, size_multiplier

    def reset(self) -> None:
        # Clears the halt only -- peak_account_value_usd and profit_lock_*
        # are deliberately PRESERVED. Resuming trading after a manual review
        # must not erase the achieved high-water mark, or the very next
        # drawdown check would start from a falsely-lowered baseline.
        state = storage.load_json(STATE_FILE, default={})
        state["halted"] = False
        state.pop("halted_at", None)
        state.pop("halted_reason", None)
        storage.save_json(STATE_FILE, state)
        logger.warning("Equity protection manually reset (halt cleared; peak preserved).")
