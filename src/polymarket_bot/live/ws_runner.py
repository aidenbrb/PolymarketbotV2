"""WebSocket-driven live market-making loop."""

from __future__ import annotations

import dataclasses
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from .. import config
from ..logger import get_logger
from ..models import utcnow_iso
from .circuit_breaker import CircuitBreaker
from .equity_protection import EquityProtection
from .fills import (
    already_recorded_fill_ids,
    build_fill_record,
    compute_markout_cents,
    find_due_markout_windows,
    get_all_fills,
    is_actual_fill,
    overwrite_fills,
    record_fill,
    resolve_order_id_and_market_slug,
)
from .instance_lock import InstanceLock
from .ledger import estimate_daily_pnl_usd, get_known_order_details
from .market_selection import select_target_markets
from .multi_market_maker import MultiMarketMaker
from .us_client import LiveUsClient
from .ws_market_data import (
    LiveMarketWebSocketClient,
    StreamingMarketDataStore,
    StreamingReadClient,
)
from .ws_private import PrivateStateStore, PrivateWebSocketClient

logger = get_logger("live.ws_runner")


class WebSocketLiveTradingBot:
    def __init__(
        self,
        client: LiveUsClient,
        circuit_breaker: Optional[CircuitBreaker] = None,
        equity_protection: Optional[EquityProtection] = None,
        settings: Optional[config.LiveTradingSettings] = None,
        market_ws: Optional[LiveMarketWebSocketClient] = None,
        store: Optional[StreamingMarketDataStore] = None,
        maker: Optional[MultiMarketMaker] = None,
        private_ws: Optional[PrivateWebSocketClient] = None,
        private_store: Optional[PrivateStateStore] = None,
    ):
        self.client = client
        self.settings = settings or config.load_settings().live
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.equity_protection = equity_protection or EquityProtection()
        self.store = store or StreamingMarketDataStore(self.settings.websocket_stale_after_seconds)
        self.market_ws = market_ws or LiveMarketWebSocketClient(
            settings=self.settings,
            signed_headers=client.websocket_headers,
            store=self.store,
        )
        # Scaffold only -- see live/ws_private.py's module docstring. Nothing
        # in self.maker reads private_store; it exists for visibility only.
        self.private_store = private_store or PrivateStateStore()
        self.private_ws = private_ws or PrivateWebSocketClient(
            settings=self.settings,
            signed_headers=client.websocket_headers,
            store=self.private_store,
        )
        self._candidates = []
        self._extra_raw_by_slug: dict[str, dict] = {}
        self._candidates_lock = threading.RLock()
        read_client = StreamingReadClient(self.store)
        self.maker = maker or MultiMarketMaker(
            client=self.client,
            settings=self.settings,
            read_client=read_client,
            candidate_provider=self._get_candidates,
        )
        self._stop_event = threading.Event()
        self._ws_thread: Optional[threading.Thread] = None
        self._private_ws_thread: Optional[threading.Thread] = None

    def run_forever(self) -> None:
        with InstanceLock():
            try:
                self._refresh_candidates()
                self._start_ws_thread()
                if self.settings.enable_private_websocket:
                    self._start_private_ws_thread()

                last_candidate_refresh = time.monotonic()
                while not self._stop_event.is_set():
                    now = time.monotonic()
                    if now - last_candidate_refresh >= self.settings.websocket_candidate_refresh_seconds:
                        self._refresh_candidates()
                        last_candidate_refresh = now

                    self._run_one_cycle()
                    self._stop_event.wait(timeout=self.settings.refresh_interval_seconds)
            except KeyboardInterrupt:
                logger.warning("Ctrl+C received. Closing WebSocket and cancelling all resting orders.")
            finally:
                self._stop_event.set()
                self.market_ws.stop()
                self.private_ws.stop()
                if self._ws_thread is not None:
                    self._ws_thread.join(timeout=5)
                if self._private_ws_thread is not None:
                    self._private_ws_thread.join(timeout=5)
                self.client.cancel_all()

    def _start_ws_thread(self) -> None:
        self._ws_thread = threading.Thread(
            target=self.market_ws.run_forever,
            name="polymarket-market-ws",
            daemon=True,
        )
        self._ws_thread.start()

    def _start_private_ws_thread(self) -> None:
        self._private_ws_thread = threading.Thread(
            target=self.private_ws.run_forever,
            name="polymarket-private-ws",
            daemon=True,
        )
        self._private_ws_thread.start()

    def _refresh_candidates(self) -> None:
        candidate_limit = max(
            1,
            min(
                self.settings.websocket_subscription_limit,
                self.settings.max_orders_per_cycle,
            ),
        )
        raw_by_slug_out: dict[str, dict] = {}
        candidates = select_target_markets(
            settings=self.settings, max_targets=candidate_limit, raw_by_slug_out=raw_by_slug_out,
        )
        slugs = [c.market.market_id for c in candidates if c.market is not None]
        with self._candidates_lock:
            self._candidates = candidates
            # Replaced (not merged) each refresh -- a market that's since been
            # delisted should age out rather than linger with stale data.
            self._extra_raw_by_slug = raw_by_slug_out
        self.market_ws.set_market_slugs(slugs)
        logger.info("Streaming runner tracking %d candidate markets.", len(slugs))

    def _get_candidates(self):
        with self._candidates_lock:
            return list(self._candidates)

    def _get_extra_raw_by_slug(self) -> dict[str, dict]:
        with self._candidates_lock:
            return dict(self._extra_raw_by_slug)

    def _run_one_cycle(self) -> None:
        total_pnl = self._estimate_daily_pnl()

        # Best-effort, and deliberately BEFORE either halt check below -- a
        # fill that already executed on the exchange must be persisted
        # regardless of whether this cycle is about to halt.
        self._persist_new_fills()
        self._compute_due_markouts()

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
            self.maker.refresh_quotes(
                candidates=self._get_candidates(),
                settings_override=settings_override,
                extra_raw_by_slug=self._get_extra_raw_by_slug(),
            )
        except Exception as exc:  # noqa: BLE001 -- keep the bot alive across one bad cycle
            logger.error("Streaming refresh cycle failed: %s", exc)

    def _persist_new_fills(self) -> None:
        """Drains PrivateStateStore.recent_executions() into fills.py's
        persisted JSON file, enriching each with the bot's own ledger data
        where possible. See live/RUNBOOK.md's "-9." section -- this is a
        best-effort, unverified-schema diagnostic and must never be allowed
        to interrupt the main refresh cycle."""
        try:
            already_seen = already_recorded_fill_ids()
            order_details = get_known_order_details()
            bbo_cache: dict[str, Optional[dict]] = {}
            for execution in self.private_store.recent_executions():
                if not is_actual_fill(execution):
                    continue  # order-lifecycle noise (NEW/CANCELED/REJECTED/...), not a fill
                fill_id = execution.get("id") or execution.get("tradeId")
                if not fill_id or fill_id in already_seen:
                    continue
                _order_id, market_slug = resolve_order_id_and_market_slug(execution, order_details)
                current_bbo = None
                if market_slug is not None:
                    if market_slug not in bbo_cache:
                        bbo_cache[market_slug] = self.maker.read_client.get_market_bbo(market_slug)
                    current_bbo = bbo_cache[market_slug]
                record_fill(build_fill_record(execution, order_details, current_bbo))
        except Exception as exc:  # noqa: BLE001 -- must never interrupt the refresh cycle
            logger.error("Could not persist new fills this cycle: %s", exc)

    def _compute_due_markouts(self) -> None:
        """Scans fills.json for any fill whose real 1-minute/5-minute mark
        (from its actual exchange transact_time, not detected_at) has passed
        and hasn't been resolved yet -- computes it against a fresh BBO, or
        resolves it to None if the mark is too stale to mean anything (see
        live/RUNBOOK.md's "-9." section). Feeds newly-computed 1-minute
        markouts into self.maker.toxicity_tracker. Best-effort, must never
        interrupt the refresh cycle."""
        if not self.settings.markout_tracking_enabled:
            return
        try:
            fills = get_all_fills()
            now = datetime.now(timezone.utc)
            due = find_due_markout_windows(fills, now, self.settings.markout_max_staleness_seconds)
            if not due:
                return

            bbo_cache: dict[str, Optional[dict]] = {}
            now_iso = utcnow_iso()
            changed = False
            newly_computed_1m: list[tuple[str, float]] = []

            for item in due:
                cents_field = f"markout_{item['window']}_cents"
                computed_field = f"markout_{item['window']}_computed_at"

                if item["status"] == "stale":
                    fills[item["index"]][cents_field] = None
                    fills[item["index"]][computed_field] = now_iso
                    changed = True
                    continue

                slug = item["market_slug"]
                if slug not in bbo_cache:
                    bbo_cache[slug] = self.maker.read_client.get_market_bbo(slug)
                bbo = bbo_cache[slug]
                if not bbo or bbo.get("best_bid") is None or bbo.get("best_ask") is None:
                    continue  # retry next cycle

                mid = (bbo["best_bid"] + bbo["best_ask"]) / 2
                markout_cents = compute_markout_cents(item["side"], item["price"], mid)
                fills[item["index"]][cents_field] = markout_cents
                fills[item["index"]][computed_field] = now_iso
                changed = True
                if item["window"] == "1m":
                    newly_computed_1m.append((slug, markout_cents))

            if changed:
                overwrite_fills(fills)

            if self.settings.toxicity_tracking_enabled:
                for slug, markout_cents in newly_computed_1m:
                    self.maker.toxicity_tracker.record_markout(slug, markout_cents)
        except Exception as exc:  # noqa: BLE001 -- must never interrupt the refresh cycle
            logger.error("Could not compute due markouts this cycle: %s", exc)

    def _estimate_daily_pnl(self) -> float:
        pnl = estimate_daily_pnl_usd(self.client)
        if pnl is None:
            logger.warning(
                "Daily P/L could not be computed this cycle. Treating as $0 so "
                "trading continues, but circuit-breaker protection is degraded."
            )
            return 0.0
        return pnl
