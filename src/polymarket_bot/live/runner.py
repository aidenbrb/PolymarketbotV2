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
from .circuit_breaker import CircuitBreaker
from .equity_protection import EquityProtection
from .instance_lock import InstanceLock
from .ledger import estimate_daily_pnl_usd
from .us_client import LiveUsClient

logger = get_logger("live.runner")


class LiveTradingBot:
    def __init__(
        self,
        client: LiveUsClient,
        market_maker,
        circuit_breaker: Optional[CircuitBreaker] = None,
        equity_protection: Optional[EquityProtection] = None,
        settings: Optional[config.LiveTradingSettings] = None,
    ):
        self.client = client
        self.market_maker = market_maker
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.equity_protection = equity_protection or EquityProtection()
        self.settings = settings or config.load_settings().live
        self._stop_event = threading.Event()

    def run_forever(self) -> None:
        with InstanceLock():
            try:
                while not self._stop_event.is_set():
                    self._run_one_cycle()
                    self._stop_event.wait(timeout=self.settings.refresh_interval_seconds)
            except KeyboardInterrupt:
                logger.warning("Ctrl+C received. Cancelling all resting orders and stopping.")
            finally:
                self._stop_event.set()
                self.client.cancel_all()

    def _run_one_cycle(self) -> None:
        total_pnl = self._estimate_daily_pnl()
        if self.circuit_breaker.evaluate(total_pnl_usd=total_pnl, client=self.client):
            logger.warning("Circuit breaker halted -- skipping this refresh cycle.")
            return

        halted, size_multiplier = self.equity_protection.evaluate(total_pnl_usd=total_pnl, client=self.client)
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
        except Exception as exc:  # noqa: BLE001 -- keep the bot alive across one bad cycle
            logger.error("Refresh cycle failed: %s", exc)

    def _estimate_daily_pnl(self) -> float:
        pnl = estimate_daily_pnl_usd(self.client)
        if pnl is None:
            logger.warning(
                "Daily P/L could not be computed this cycle (account balances "
                "endpoint unreachable). Treating as $0 so trading continues, but "
                "this means the circuit breaker is NOT actively protecting you "
                "this cycle."
            )
            return 0.0
        return pnl
