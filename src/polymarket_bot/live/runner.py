"""Runs the periodic refresh loop for live market-making.

No heartbeat thread is needed for Polymarket US -- unlike the international
platform, there's no documented requirement to keep resting orders alive
with a keep-alive signal, so this is simpler than a two-thread design.
"""

from __future__ import annotations

import dataclasses
import threading
from typing import Optional

from .. import config
from ..logger import get_logger
from .circuit_breaker import CircuitBreaker, SessionCircuitBreaker
from .equity_protection import EquityProtection
from .instance_lock import InstanceLock
from . import ledger as ledger_module
from .ledger import get_total_position_pnl_usd
from .market_maker import EmergencySafeguardFailedError
from .startup_recovery import recover_from_prior_crash
from .us_client import LiveUsClient

logger = get_logger("live.runner")


class LiveTradingBot:
    def __init__(
        self,
        client: LiveUsClient,
        market_maker,
        circuit_breaker: Optional[CircuitBreaker] = None,
        session_circuit_breaker: Optional[SessionCircuitBreaker] = None,
        equity_protection: Optional[EquityProtection] = None,
        settings: Optional[config.LiveTradingSettings] = None,
    ):
        self.client = client
        self.market_maker = market_maker
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.session_circuit_breaker = session_circuit_breaker or SessionCircuitBreaker()
        self.equity_protection = equity_protection or EquityProtection()
        self.settings = settings or config.load_settings().live
        self._stop_event = threading.Event()

    def run_forever(self) -> None:
        with InstanceLock():
            if self.settings.startup_crash_recovery_enabled:
                try:
                    recover_from_prior_crash(self.client)
                except Exception as exc:
                    # Deliberately NOT swallowed -- a leftover resting order
                    # this run doesn't know about is a real, unmanaged risk;
                    # refuse to start rather than place new orders on top of
                    # unknown prior state. See startup_recovery.py.
                    logger.exception("Startup crash recovery failed -- refusing to start: %s", exc)
                    raise
            try:
                while not self._stop_event.is_set():
                    self._run_one_cycle()
                    self._stop_event.wait(timeout=self.settings.refresh_interval_seconds)
            except KeyboardInterrupt:
                logger.warning("Ctrl+C received. Cancelling all resting orders and stopping.")
            except Exception as exc:
                logger.exception("Live trading process crashed unexpectedly: %s", exc)
                raise
            finally:
                self._stop_event.set()
                self.client.cancel_all()

    def _run_one_cycle(self) -> None:
        total_now, daily_pnl, session_pnl = self._estimate_pnl_figures()
        if total_now is None:
            logger.error(
                "Daily P/L is unavailable; cancelling resting orders and "
                "skipping this cycle so the circuit breaker cannot fail open."
            )
            self.client.cancel_all()
            return
        if self.circuit_breaker.evaluate(total_pnl_usd=daily_pnl, client=self.client):
            logger.warning("Circuit breaker halted -- skipping this refresh cycle.")
            return
        if self.session_circuit_breaker.evaluate(total_pnl_usd=session_pnl, client=self.client):
            logger.warning("Session circuit breaker halted -- skipping this refresh cycle.")
            return

        halted, size_multiplier = self.equity_protection.evaluate(
            total_pnl_usd=daily_pnl, client=self.client, lifetime_pnl_usd=total_now,
        )
        if halted:
            logger.warning("Equity protection halted -- skipping this refresh cycle.")
            return

        settings_override = None
        if size_multiplier != 1.0:
            settings_override = dataclasses.replace(
                self.settings,
                order_shares_min=self.settings.order_shares_min * size_multiplier,
                order_shares_max=self.settings.order_shares_max * size_multiplier,
            )

        try:
            self.market_maker.refresh_quotes(settings_override=settings_override)
        except EmergencySafeguardFailedError:
            # Deliberately NOT caught like an ordinary refresh failure --
            # this means the last-resort emergency cancel safeguard itself
            # couldn't be confirmed to have worked. Left to propagate to
            # run_forever()'s own top-level `except Exception: raise`,
            # which stops the process rather than retrying next cycle.
            raise
        except Exception as exc:  # noqa: BLE001 -- keep the bot alive across one bad cycle
            logger.error("Refresh cycle failed: %s", exc)

    def _estimate_pnl_figures(self) -> tuple[Optional[float], float, float]:
        """Fetches get_total_position_pnl_usd() ONCE and derives both the
        daily-diffed (UTC-midnight-resetting) and session-diffed (since this
        process started) figures from it -- mirrors ws_runner.py's own
        _estimate_pnl_figures, and avoids a second positions fetch for
        either the session breaker or equity protection's drawdown check.
        Returns (total_now, daily_pnl, session_pnl); total_now is None only
        if positions can't be fetched, in which case the caller fails closed
        for the cycle."""
        total_now = get_total_position_pnl_usd(self.client)
        if total_now is None:
            logger.warning(
                "Daily P/L could not be computed this cycle. Trading will "
                "remain halted until circuit-breaker state is trustworthy."
            )
            return None, 0.0, 0.0
        daily_pnl = ledger_module.diff_against_baseline(total_now, ledger_module.DAILY_BALANCE_FILE)
        session_pnl = self.session_circuit_breaker.diff(total_now)
        return total_now, daily_pnl, session_pnl
