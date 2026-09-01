"""WebSocket-driven live market-making loop."""

from __future__ import annotations

import dataclasses
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from .. import config, storage
from ..logger import get_logger
from ..models import utcnow_iso
from .circuit_breaker import CircuitBreaker, SessionCircuitBreaker
from .cancel_safeguard import cancel_all_and_verify
from .equity_protection import EquityProtection
from .fills import (
    already_recorded_fill_ids,
    build_fill_record,
    compute_executable_markout_cents,
    compute_markout_cents,
    find_due_markout_windows,
    get_all_fills,
    is_actual_fill,
    overwrite_fills,
    record_fill,
    resolve_order_id_and_market_slug,
)
from .instance_lock import InstanceLock
from . import ledger as ledger_module
from .ledger import get_known_order_details, get_total_position_pnl_usd, strategy_total_pnl_usd
from .market_maker import (
    EmergencySafeguardFailedError,
    MarketMaker,
    book_without_bot_orders,
)
from .market_observation import MarketObservationTracker
from .market_selection import event_or_close_datetime, select_target_markets
from .multi_market_maker import MultiMarketMaker
from .session_metrics import SessionMetricsRecorder, flat_round_trip_fill_pnl
from .startup_recovery import StartupRecoveryError, recover_from_prior_crash, verify_flat_for_observation
from .us_client import LiveUsClient
from .ws_market_data import (
    LiveMarketWebSocketClient,
    StreamingMarketDataStore,
    StreamingReadClient,
)
from .ws_private import PrivateStateStore, PrivateWebSocketClient

logger = get_logger("live.ws_runner")
PILOT_RESULTS_FILE = config.LIVE_TRADES_DIR / "pilot_results.json"


class ObservationIntegrityError(RuntimeError):
    """An order or position appeared while observation-only mode was
    running. Observation-only mode never manages or cancels anything (see
    run_forever) -- continuing to watch silently would leave whatever
    appeared invisible and unmanaged for the rest of the run, so this is
    deliberately left uncaught to stop the process instead."""


class ObservationFeedStalledError(RuntimeError):
    """Observation had a non-empty subscription universe but received no
    heartbeat or market message even after bounded recovery reconnects.
    REST scans are not a substitute for a verified live stream, so the
    process stops visibly."""


class PilotFlatnessError(RuntimeError):
    """The guarded pilot could not prove a flat account at startup/shutdown."""


def _position_is_flat(position: dict) -> bool:
    value = position.get("netPositionDecimal")
    if value is None:
        value = position.get("net_position")
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


class WebSocketLiveTradingBot:
    def __init__(
        self,
        client: LiveUsClient,
        circuit_breaker: Optional[CircuitBreaker] = None,
        session_circuit_breaker: Optional[SessionCircuitBreaker] = None,
        equity_protection: Optional[EquityProtection] = None,
        settings: Optional[config.LiveTradingSettings] = None,
        market_ws: Optional[LiveMarketWebSocketClient] = None,
        store: Optional[StreamingMarketDataStore] = None,
        maker: Optional[MultiMarketMaker] = None,
        private_ws: Optional[PrivateWebSocketClient] = None,
        private_store: Optional[PrivateStateStore] = None,
        observation_tracker: Optional[MarketObservationTracker] = None,
    ):
        self.client = client
        self.settings = settings or config.load_settings().live
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.session_circuit_breaker = session_circuit_breaker or SessionCircuitBreaker()
        self.equity_protection = equity_protection or EquityProtection()
        self.observation_tracker = observation_tracker or MarketObservationTracker(self.settings)
        self.store = store or StreamingMarketDataStore(
            self.settings.websocket_stale_after_seconds,
            activity_window_seconds=self.settings.activity_window_seconds,
            observation_tracker=self.observation_tracker,
        )
        if store is not None:
            self.store.observation_tracker = self.observation_tracker
        self.market_ws = market_ws or LiveMarketWebSocketClient(
            settings=self.settings,
            signed_headers=client.websocket_headers,
            store=self.store,
        )
        self.private_store = private_store or PrivateStateStore()
        self.private_ws = private_ws or PrivateWebSocketClient(
            settings=self.settings,
            signed_headers=client.websocket_headers,
            store=self.private_store,
        )
        self._candidates = []
        self._observation_universe = []
        self._watched_slugs: list[str] = []
        self._observation_cursor = 0
        self._observation_active_until: dict[str, float] = {}
        self._last_gate_summary: Optional[tuple[int, int, bool]] = None
        self._extra_raw_by_slug: dict[str, dict] = {}
        self._candidates_lock = threading.RLock()
        self._last_candidate_refresh = 0.0
        self._last_candidate_refresh_attempt = 0.0
        self._last_candidate_refresh_failed = False
        self._candidate_refresh_thread: Optional[threading.Thread] = None
        self._was_halted = False
        read_client = StreamingReadClient(self.store)
        self.maker = maker or MultiMarketMaker(
            client=self.client,
            settings=self.settings,
            read_client=read_client,
            candidate_provider=self._get_candidates,
            private_store=self.private_store,
        )
        self._stop_event = threading.Event()
        self._ws_thread: Optional[threading.Thread] = None
        self._private_ws_thread: Optional[threading.Thread] = None
        self._session_metrics: Optional[SessionMetricsRecorder] = None
        self._latest_total_pnl: Optional[float] = None
        self._pilot_started_monotonic: Optional[float] = None
        self._pilot_start_fill_count: Optional[int] = None
        self._pilot_start_epoch: Optional[float] = None
        self._market_feed_recovery_attempts = 0
        self._market_feed_recovery_started_epoch: Optional[float] = None
        self._market_feed_activity_before_recovery = 0.0
        self._observation_finalization_wait_started: Optional[float] = None
        self._observation_finalization_last_log = 0.0
        self._observation_finalization_reconnect_requested = False

    def _pilot_in_drain(self) -> bool:
        """True once the pilot's entry window has elapsed and only the
        mandatory drain/force-flat phase remains. Used to skip candidate
        discovery that _run_one_cycle already ignores during this phase
        (drain_only forces candidates=[] regardless), so shutdown-critical
        account-reconciliation REST calls aren't competing with a market
        scan for rate-limit budget."""
        if not (self.settings.pilot_mode and self._pilot_started_monotonic is not None):
            return False
        elapsed = time.monotonic() - self._pilot_started_monotonic
        return elapsed >= self.settings.pilot_entry_hours * 3600

    def _check_fast_repricing(self) -> None:
        """Cancels resting orders immediately once their market has moved
        beyond max_recent_move_cents since they were quoted, instead of
        waiting up to refresh_interval_seconds for the next full cycle to
        notice. The normal check reads only in-memory/local state
        (open_orders_snapshot, market L2, the local ledger), so it is safe to
        run far more often than a full quote refresh. A REST verification is
        made only after cancellation actually triggers, because returning
        from two sequential cancel calls is not proof that both paired legs
        disappeared. It only ever cancels; it never places a new order.

        Two correctness requirements, not just performance ones:
        - Bot-owned orders only. get_known_order_details() (the same
          ledger-backed ownership source multi_market_maker.py's own
          stale-order sweep uses) is required to recognize an order before
          it's ever touched -- a manual or unrecognized account order is
          left alone, fail-closed, exactly like the rest of the codebase's
          ownership checks.
        - Directional, side-specific comparison. Comparing a resting order's
          own price against the book's MIDPOINT is wrong for a market maker
          resting near one edge of a wide book (this pilot's opportunity set
          allows spreads up to 98c) -- a completely unchanged 39c bid/85c
          ask book has a 62c midpoint, which would make a 40c bid look like
          it "moved" 22c and get cancelled on a static market. A BUY order
          is compared against the current external best_bid, a SELL against
          the current external best_ask -- after recognized bot liquidity is
          removed from the L2 book. Only movement in the adverse direction
          triggers: a BUY above a falling bid or a SELL below a rising ask.

        Cancels are grouped per market: if any bot-owned leg on a market
        needs cancelling, every bot-owned leg resting on that market is
        cancelled together, so a paired entry (both legs from flat) can
        never be left one-sided by a fast cancel that only touched the
        drifted side."""
        orders = self.private_store.open_orders_snapshot()
        if not orders:
            return
        known = get_known_order_details()
        if not known:
            return
        by_slug: dict[str, list[tuple[str, dict, dict]]] = {}
        for order in orders:
            order_id = order.get("id") or order.get("orderId") or order.get("order_id")
            if not order_id:
                continue
            details = known.get(str(order_id))
            if details is None:
                continue  # not a bot-recognized order -- never touch it
            slug = details.get("market_id")
            if not slug:
                continue
            by_slug.setdefault(slug, []).append((str(order_id), order, details))

        for slug, entries in by_slug.items():
            book = self.store.get_market_book(slug)
            if not book:
                continue
            # The public book includes this bot's own orders. Normalize the
            # private snapshot with ledger fallbacks, then subtract those
            # quantities before deciding whether the external market moved.
            # Without this, an exposed 40c bid can compare against itself at
            # 40c while the next real bid has already collapsed to 20c.
            normalized_orders: list[dict] = []
            bot_order_ids: set[str] = set()
            for order_id, order, details in entries:
                normalized = dict(order)
                if not normalized.get("id") and not normalized.get("orderId"):
                    normalized["id"] = order_id
                if not normalized.get("marketSlug") and not normalized.get("market_slug"):
                    normalized["marketSlug"] = slug
                if not normalized.get("side") and not normalized.get("action"):
                    normalized["side"] = details.get("side")
                if normalized.get("price") is None:
                    normalized["price"] = details.get("price")
                if not any(
                    normalized.get(key) is not None
                    for key in ("leavesQuantity", "leaves_quantity", "quantity")
                ):
                    normalized["leavesQuantity"] = details.get("size")
                normalized_orders.append(normalized)
                bot_order_ids.add(order_id)
            external_book = book_without_bot_orders(
                book,
                normalized_orders,
                market_slug=slug,
                bot_order_ids=bot_order_ids,
            )
            bids = external_book.get("bids") or []
            asks = external_book.get("asks") or []
            bbo = {
                "best_bid": bids[0].get("price") if bids else None,
                "best_ask": asks[0].get("price") if asks else None,
            }
            best_bid, best_ask = MarketMaker._pick_book(bbo)
            drifted = False
            for _order_id, order, details in entries:
                quoted_price = order.get("price")
                if isinstance(quoted_price, dict):
                    quoted_price = quoted_price.get("value")
                if quoted_price is None:
                    quoted_price = details.get("price")
                try:
                    quoted_price = float(quoted_price)
                except (TypeError, ValueError):
                    continue
                side = str(details.get("side") or "").upper()
                if side.endswith("BUY"):
                    target = best_bid
                    moved_cents = (
                        None if target is None else (quoted_price - target) * 100
                    )
                elif side.endswith("SELL"):
                    target = best_ask
                    moved_cents = (
                        None if target is None else (target - quoted_price) * 100
                    )
                else:
                    continue
                if moved_cents is None:
                    continue
                if moved_cents > self.settings.max_recent_move_cents:
                    drifted = True
            if not drifted:
                continue
            attempted_ids = {order_id for order_id, _order, _details in entries}
            any_cancel_failed = False
            for order_id, _order, _details in entries:
                try:
                    self.client.cancel_order(order_id, slug)
                    logger.warning(
                        "Fast repricing: cancelled bot-owned resting order "
                        "%s for %s (side-specific target moved beyond the "
                        "%.2fc guard).",
                        order_id, slug, self.settings.max_recent_move_cents,
                    )
                except Exception as exc:  # noqa: BLE001 -- verify the group below; escalate if any leg survived
                    any_cancel_failed = True
                    logger.warning("Fast repricing cancel failed for %s: %s", slug, exc)

            if not any_cancel_failed:
                # Every leg's own cancel_order() call already succeeded --
                # the same trust signal _cancel_existing_orders() relies on
                # elsewhere with no follow-up REST check at all. cancel_order
                # is a single-attempt write (unlike get_open_orders, it is
                # never called with retryable=True -- see us_client.py), and
                # Polymarket's own API documents a 200 {} response as a
                # successful cancellation. Re-verifying via a fresh REST call
                # here regardless adds no real safety value over that, and
                # its failure mode (escalating to an account-wide cancel-
                # all-and-verify) is disproportionate overhead to pay on
                # every single successful cancellation -- this is exactly
                # what turned a clean pair of cancels into a 429 cascade
                # that crashed a real pilot run. Only fall through to REST
                # verification when a leg's own cancel actually failed -- a
                # genuine reason for doubt.
                self.private_store.remove_orders(attempted_ids)
                continue

            # Per-order cancellation is not atomic. Verify the complete group
            # before returning to the five-second loop; otherwise a successful
            # first cancel plus a failed sibling cancel can leave the paired
            # entry naked for almost a full quote cycle. This REST read occurs
            # only after a drift-triggered cancellation, never on every cheap
            # five-second check.
            try:
                remaining = self.client.get_open_orders()
            except Exception as exc:  # noqa: BLE001 -- uncertainty is unsafe
                logger.error(
                    "Could not verify fast repricing cancellation for %s: %s; "
                    "using the fail-closed account-wide safeguard.",
                    slug,
                    exc,
                )
                cancel_all_and_verify(
                    self.client,
                    open_orders=orders,
                    context=f"fast repricing verification for {slug}",
                )
                self.private_store.seed_open_orders([])
                return
            if not isinstance(remaining, list):
                cancel_all_and_verify(
                    self.client,
                    open_orders=orders,
                    context=f"fast repricing invalid verification for {slug}",
                )
                self.private_store.seed_open_orders([])
                return
            remaining_ids = {
                str(order.get("id") or order.get("orderId") or order.get("order_id"))
                for order in remaining
                if order.get("id") or order.get("orderId") or order.get("order_id")
            }
            survivors = attempted_ids & remaining_ids
            if survivors:
                logger.error(
                    "Fast repricing left bot-owned order(s) resting on %s: %s; "
                    "using the fail-closed account-wide safeguard.",
                    slug,
                    sorted(survivors),
                )
                cancel_all_and_verify(
                    self.client,
                    open_orders=remaining,
                    context=f"incomplete fast repricing cancellation for {slug}",
                )
                self.private_store.seed_open_orders([])
                return
            self.private_store.remove_orders(attempted_ids)

    def _wait_with_fast_repricing(self, timeout: float) -> None:
        """Waits up to `timeout` seconds for _stop_event, but when
        fast_reprice_enabled, wakes up every fast_reprice_check_seconds in
        between to run _check_fast_repricing() -- reacting to a market move
        faster than waiting for the next full refresh_interval_seconds cycle
        would allow. A no-op behavior change when disabled (the default):
        identical to a single plain wait."""
        if not self.settings.fast_reprice_enabled:
            self._stop_event.wait(timeout=timeout)
            return
        deadline = time.monotonic() + timeout
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._check_fast_repricing()
            self._stop_event.wait(
                timeout=min(self.settings.fast_reprice_check_seconds, remaining)
            )

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
            if self.settings.observation_only_mode:
                try:
                    if not self.settings.enable_private_websocket:
                        raise StartupRecoveryError(
                            "Observation-only mode requires the private WebSocket for "
                            "read-only integrity monitoring, but "
                            "LIVE_ENABLE_PRIVATE_WEBSOCKET is disabled."
                        )
                    open_orders, positions = verify_flat_for_observation(self.client)
                except Exception as exc:
                    # Same fail-closed posture as crash recovery above --
                    # observation-only mode never manages or cancels
                    # anything for the whole run (see the main loop below),
                    # so starting it against non-flat state would leave
                    # that position/order unmanaged and invisible for as
                    # long as this process runs. See startup_recovery.py.
                    logger.exception(
                        "Cannot start observation-only mode with non-flat account state: %s", exc,
                    )
                    raise
                # Confirmed-empty baseline, not a second REST round trip --
                # the private WS's own delta stream layers on top of this
                # from here on, with no further polling.
                self.private_store.seed_open_orders(open_orders)
                self.private_store.seed_positions(positions)
            elif self.settings.pilot_mode:
                if not self.settings.enable_private_websocket:
                    raise PilotFlatnessError(
                        "The guarded pilot requires the private WebSocket."
                    )
                try:
                    open_orders, positions = verify_flat_for_observation(self.client)
                except Exception as exc:
                    raise PilotFlatnessError(
                        f"Pilot requires a verified-flat account: {exc}"
                    ) from exc
                self.private_store.seed_open_orders(open_orders)
                self.private_store.seed_positions(positions)
            try:
                self._session_metrics = SessionMetricsRecorder()
                try:
                    self._session_metrics.start()
                except Exception as exc:  # noqa: BLE001 -- diagnostics cannot block trading
                    logger.warning("Could not start session metrics: %s", exc)
                    self._session_metrics = None
                self._maybe_refresh_candidates()
                self._start_ws_thread()
                if self.settings.enable_private_websocket:
                    self._start_private_ws_thread()
                if self.settings.observation_only_mode:
                    logger.warning(
                        "OBSERVATION-ONLY MODE: passive market-data collection is active, "
                        "with the private WebSocket running read-only for integrity "
                        "monitoring. The maker and all cancellation paths are unreachable; "
                        "an unexpected order or position will abort the run rather than "
                        "being managed."
                    )
                elif self.settings.pilot_mode:
                    pilot_start_epoch = time.time()
                    pilot_start_fill_count = len(get_all_fills())
                    self._pilot_started_monotonic = time.monotonic()
                    self._pilot_start_fill_count = pilot_start_fill_count
                    self._pilot_start_epoch = pilot_start_epoch
                    logger.warning(
                        "GUARDED PILOT: profile=%s qualification_bypassed=%s; "
                        "new entries stop after %.2f hours, followed by a "
                        "%.0f-minute passive drain and verified force-flat exit.",
                        self.settings.pilot_strategy_profile,
                        self.settings.pilot_qualification_bypassed,
                        self.settings.pilot_entry_hours,
                        self.settings.pilot_drain_minutes,
                    )

                while not self._stop_event.is_set():
                    if not self._pilot_in_drain():
                        self._maybe_refresh_candidates()
                    if self.settings.observation_only_mode:
                        self._abort_if_unexpected_activity_during_observation()
                        if self.observation_tracker.evaluation_complete():
                            finalization = self._finalize_observation_when_ready()
                            if finalization is None:
                                self._abort_if_observation_feed_stalled()
                                self._wait_with_fast_repricing(
                                    self.settings.refresh_interval_seconds
                                )
                                continue
                            result = self.observation_tracker.controlled_qualification()
                            logger.warning(
                                "The observation evidence target is complete: "
                                "controlled status=%s round_trips=%d events=%d "
                                "total_pnl=$%.4f. Observation is stopping; a "
                                "pilot is never started automatically.",
                                result["status"],
                                result["completed_round_trips"],
                                result["distinct_event_count"],
                                result["total_pnl_usd"],
                            )
                            break
                        self._abort_if_observation_feed_stalled()
                    else:
                        drain_only = False
                        if (
                            self.settings.pilot_mode
                            and self._pilot_started_monotonic is not None
                        ):
                            elapsed = (
                                time.monotonic() - self._pilot_started_monotonic
                            )
                            entry_seconds = self.settings.pilot_entry_hours * 3600
                            end_seconds = (
                                entry_seconds
                                + self.settings.pilot_drain_minutes * 60
                            )
                            if elapsed >= end_seconds:
                                logger.warning(
                                    "Pilot drain window complete; starting "
                                    "mandatory force-flat shutdown."
                                )
                                break
                            drain_only = elapsed >= entry_seconds
                        self._run_one_cycle(drain_only=drain_only)
                    self._wait_with_fast_repricing(self.settings.refresh_interval_seconds)
            except KeyboardInterrupt:
                if self.settings.observation_only_mode:
                    logger.warning("Ctrl+C received. Closing passive observation WebSocket.")
                else:
                    logger.warning("Ctrl+C received. Closing WebSocket and cancelling all resting orders.")
            except Exception as exc:
                logger.exception("Live trading process crashed unexpectedly: %s", exc)
                raise
            finally:
                self._stop_event.set()
                pilot_flat_error: Optional[Exception] = None
                if self.settings.pilot_mode:
                    pilot_flat = False
                    try:
                        self._finish_pilot_flat()
                        pilot_flat = True
                    except Exception as exc:  # fail closed after resource cleanup
                        pilot_flat_error = exc
                    finally:
                        self._record_pilot_acceptance(pilot_flat)
                self.market_ws.stop()
                self.private_ws.stop()
                try:
                    self.observation_tracker.flush()
                except Exception as exc:  # noqa: BLE001 -- diagnostics cannot block shutdown
                    logger.warning("Could not flush market observations during shutdown: %s", exc)
                if self._ws_thread is not None:
                    self._ws_thread.join(timeout=5)
                if self._private_ws_thread is not None:
                    self._private_ws_thread.join(timeout=5)
                if not self.settings.observation_only_mode:
                    self._cancel_all_resting_orders(context="shutdown")
                if self._session_metrics is not None:
                    try:
                        self._session_metrics.finish(self._latest_total_pnl)
                    except Exception as exc:  # noqa: BLE001 -- diagnostics cannot block shutdown
                        logger.warning("Could not finalize session metrics: %s", exc)
                if pilot_flat_error is not None:
                    raise pilot_flat_error

    def _abort_if_unexpected_activity_during_observation(self) -> None:
        """Observation-only mode never manages or cancels anything (see
        run_forever) -- it only watches. If an order or position appears
        anyway (a manual trade, credentials shared with something else),
        continuing to run would leave it invisible and unmanaged for
        however long this process keeps going. Checked against the private
        WebSocket's own in-memory state, seeded flat at startup by
        verify_flat_for_observation -- no REST polling here, which is
        exactly what observation-only mode exists to avoid. Deliberately
        does not attempt to cancel or manage what it finds: it only stops,
        by raising ObservationIntegrityError, left uncaught so it reaches
        run_forever()'s own top-level `except Exception: raise`."""
        open_orders = self.private_store.open_orders_snapshot() or []
        if open_orders:
            ids = [order.get("id") or order.get("orderId") for order in open_orders]
            raise ObservationIntegrityError(
                f"{len(open_orders)} open order(s) appeared during observation-only "
                f"mode -- refusing to continue silently: {ids}"
            )
        positions = self.private_store.positions_snapshot() or {}
        non_flat = [slug for slug, position in positions.items() if not _position_is_flat(position)]
        if non_flat:
            raise ObservationIntegrityError(
                f"{len(non_flat)} open position(s) appeared during observation-only "
                f"mode -- refusing to continue silently: {sorted(non_flat)}"
            )

    def _finalize_observation_when_ready(self) -> Optional[dict]:
        """Wait boundedly for fresh books before sweeping shadow inventory.

        Candidate refresh is intentionally asynchronous and can take over a
        minute. An observer restarted after its evidence target was already
        reached must not finalize against an empty store before that refresh
        has subscribed the persisted inventory slugs.
        """
        max_age = self.settings.observation_feed_stale_after_seconds

        def provider(slug: str):
            return self.store.get_market_book_snapshot(
                slug, max_age_seconds=max_age,
            )

        inventory = self.observation_tracker.open_inventory_slugs()
        missing = {
            slug for slug in inventory
            if not self._two_sided_book(provider(slug))
        }
        if not missing:
            return self.observation_tracker.finalize_evaluation(provider)

        now = time.monotonic()
        if self._observation_finalization_wait_started is None:
            self._observation_finalization_wait_started = now
            self._observation_finalization_last_log = 0.0
        watched = set(self._watched_slugs)
        if (
            not self._observation_finalization_reconnect_requested
            and inventory.issubset(watched)
        ):
            # A reconnect causes the exchange to send fresh initial books for
            # every inventory-priority subscription, including quiet markets.
            self.market_ws.force_reconnect()
            self._observation_finalization_reconnect_requested = True
        elapsed = now - self._observation_finalization_wait_started
        wait_limit = max(120.0, min(900.0, max_age * 2.0))
        if elapsed < wait_limit:
            if (
                self._observation_finalization_last_log == 0.0
                or now - self._observation_finalization_last_log >= 60.0
            ):
                logger.warning(
                    "Observation evidence target reached; entries are frozen "
                    "while waiting up to %.0fs for fresh L2 books on %d/%d "
                    "shadow-inventory markets before finalization.",
                    wait_limit, len(missing), len(inventory),
                )
                self._observation_finalization_last_log = now
            return None

        logger.error(
            "Fresh finalization books are still missing for %d shadow-inventory "
            "markets after %.0fs; recording an incomplete finalization rather "
            "than using stale marks.",
            len(missing), elapsed,
        )
        return self.observation_tracker.finalize_evaluation(
            provider, max_book_attempts=1, retry_seconds=0,
        )

    @staticmethod
    def _two_sided_book(book: Optional[dict]) -> bool:
        return bool(book and book.get("bids") and book.get("asks"))

    def _abort_if_observation_feed_stalled(self) -> None:
        health = self.observation_tracker.feed_health()
        if health.get("reason") == "empty_candidate_pool":
            if not health["stalled"]:
                return
            raise ObservationFeedStalledError(
                "Observation candidate universe remained empty for "
                f"{float(health['age_seconds']):.0f}s. Stopping instead of "
                "silently running without any markets to observe."
            )
        now = time.time()
        latest_activity = float(health.get("latest_activity_epoch") or 0.0)
        if self._market_feed_recovery_started_epoch is not None:
            if latest_activity > self._market_feed_activity_before_recovery:
                logger.warning(
                    "Market WebSocket feed recovered after watchdog reconnect."
                )
                self._market_feed_recovery_attempts = 0
                self._market_feed_recovery_started_epoch = None
                self._market_feed_activity_before_recovery = 0.0
            else:
                grace = max(
                    1.0, self.settings.observation_feed_stale_after_seconds,
                )
                if now - self._market_feed_recovery_started_epoch <= grace:
                    return
                self._market_feed_recovery_started_epoch = None
        if not health["stalled"]:
            return
        if self._market_feed_recovery_attempts < 2:
            self._market_feed_recovery_attempts += 1
            self._market_feed_recovery_started_epoch = now
            self._market_feed_activity_before_recovery = latest_activity
            logger.warning(
                "Observation market WebSocket produced no heartbeat or market "
                "message for %.0fs; requesting recovery reconnect %d/2 before "
                "failing closed.",
                float(health["age_seconds"]),
                self._market_feed_recovery_attempts,
            )
            self.market_ws.force_reconnect()
            return
        raise ObservationFeedStalledError(
            "Observation market-data feed produced no heartbeat or market message for "
            f"{float(health['age_seconds']):.0f}s while "
            f"{int(health['candidate_count'])} market(s) required observation. "
            "Two clean reconnect attempts also produced no activity; stopping "
            "instead of silently collecting REST scans without queue/tape evidence."
        )

    def _cancel_all_resting_orders(self, context: str) -> None:
        """Emergency/shutdown cancel-all. A stale private-WS snapshot must
        not be trusted here: unlike the main refresh loop (which can just
        skip a cycle and retry once state is trustworthy again), this is
        often the bot's LAST action -- shutdown, or a response to already-
        degraded P/L visibility -- so under-cancelling here can leave real
        resting orders live with nothing left to manage them. Only pass the
        WS snapshot when the private WS itself vouches it's fresh;
        otherwise let cancel_all() fetch its own fresh REST snapshot (it
        already logs and returns gracefully if even that fails)."""
        if self.private_store.is_healthy(self.settings.private_state_stale_after_seconds):
            self.client.cancel_all(open_orders=self.private_store.open_orders_snapshot())
        else:
            logger.warning(
                "Private WS snapshot is stale during %s cancel-all; fetching a "
                "fresh REST snapshot instead of trusting it.", context,
            )
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

    def _compute_activity_scores(self) -> dict[str, float]:
        """Blends real WS-observed activity (recent trades, executed shares,
        and sharesTraded growth -- see ws_market_data.py's
        rolling-window tracking) into a single 0..1 confidence per
        currently-watched market, for select_target_markets' activity_scores
        param. Only ever covers markets already in self._candidates (the
        pool watched as of the START of this refresh) -- there's no
        activity history for a market that's never been watched yet. See
        live/RUNBOOK.md's most recent section."""
        if not self.settings.activity_tracking_enabled:
            return {}
        with self._candidates_lock:
            watched_slugs = list(self._watched_slugs)
        scores: dict[str, float] = {}
        for slug in watched_slugs:
            trade_count = self.store.recent_trade_count(slug)
            trade_quantity = self.store.recent_trade_quantity(slug)
            growth = self.store.shares_traded_growth(slug)
            # Book-update frequency is deliberately excluded: cancellations
            # and quote changes can make a book look busy while nobody
            # trades.  The fill objective needs actual executions.
            trade_component = min(1.0, trade_count / 3.0)
            quantity_component = min(1.0, trade_quantity / 10.0)
            growth_component = 1.0 if (growth is not None and growth > 0) else 0.0
            scores[slug] = (
                0.60 * trade_component
                + 0.25 * quantity_component
                + 0.15 * growth_component
            )
        return scores

    def _refresh_candidates(self) -> bool:
        """Returns True on success, False when the market scan itself
        failed -- _maybe_refresh_candidates() uses this to retry promptly
        instead of waiting the full websocket_candidate_refresh_seconds
        window (previously ~15min by default; a caught scan failure still
        left the bot with zero candidates for that whole window)."""
        candidate_limit = max(
            1,
            min(
                self.settings.websocket_subscription_limit,
                self.settings.websocket_candidate_pool_size,
            ),
        )
        raw_by_slug_out: dict[str, dict] = {}
        observation_markets: list = []
        try:
            candidates = select_target_markets(
                settings=self.settings, max_targets=candidate_limit, raw_by_slug_out=raw_by_slug_out,
                activity_scores=(
                    self._compute_activity_scores()
                    if self.settings.activity_rerank_enabled else {}
                ),
                observation_markets_out=observation_markets,
            )
        except Exception as exc:  # noqa: BLE001 -- an ordinary scan/network failure must never crash the bot
            # A transient market-scan failure (bad API response, network
            # error, a parsing edge case in one of hundreds of scanned
            # markets) used to propagate straight out of here, exactly like
            # the set_market_slugs() bug below it -- this is called
            # directly from run_forever()'s main loop via
            # _maybe_refresh_candidates(), which has no try/except of its
            # own, and run_forever()'s only handler catches
            # KeyboardInterrupt. The previous cycle's candidates/raw data
            # are left untouched (not cleared) so a still-tradeable market
            # isn't dropped just because this one refresh failed; the next
            # refresh retries.
            logger.warning(
                "Could not refresh candidate markets this cycle (will retry sooner "
                "than the normal refresh interval): %s", exc,
            )
            return False
        quote_slugs = [c.market.market_id for c in candidates if c.market is not None]
        if not observation_markets:
            observation_markets = list(candidates)
        observation_by_slug = {
            item.market.market_id: item for item in observation_markets if item.market is not None
        }
        capacity = max(
            1,
            min(self.settings.websocket_subscription_limit, self.settings.observation_universe_size),
        )
        # Passive observation gets the full broad comparison universe. Live
        # candidates do not receive subscription priority merely because
        # ordinary live restrictions admitted them; the v4 portfolio
        # allocator ranks the watched L2/tape itself.
        inventory_slugs = sorted(self.observation_tracker.open_inventory_slugs())
        if self.settings.observation_only_mode:
            if len(inventory_slugs) > capacity:
                logger.error(
                    "Shadow inventory requires %d subscriptions but observation "
                    "capacity is only %d; candidate refresh failed closed.",
                    len(inventory_slugs), capacity,
                )
                return False
            # Inventory owns subscription priority.  An event aging out may
            # stop NEW shadow entries, but must never orphan its exits.
            slugs = list(inventory_slugs)
        else:
            slugs = list(dict.fromkeys(quote_slugs))[:capacity]
        now = time.monotonic()
        for slug in self._watched_slugs:
            if self.store.recent_trade_count(slug) > 0:
                self._observation_active_until[slug] = max(
                    self._observation_active_until.get(slug, 0.0),
                    now + self.settings.observation_active_hold_seconds,
                )
        self._observation_active_until = {
            slug: deadline
            for slug, deadline in self._observation_active_until.items()
            if deadline > now and slug in observation_by_slug
        }
        retained = [
            slug for slug in self._observation_active_until
            if slug not in slugs
        ]
        retained.sort(
            key=lambda slug: (
                self.store.recent_trade_count(slug),
                self.store.recent_trade_quantity(slug),
                self._observation_active_until[slug],
            ),
            reverse=True,
        )
        retained_limit = max(
            0,
            min(
                self.settings.observation_retained_active_slots,
                capacity - len(slugs),
            ),
        )
        slugs.extend(retained[:retained_limit])
        broad_slugs = [slug for slug in observation_by_slug if slug not in slugs]
        remaining = capacity - len(slugs)
        if broad_slugs and remaining > 0:
            start = self._observation_cursor % len(broad_slugs)
            rotated = broad_slugs[start:] + broad_slugs[:start]
            slugs.extend(rotated[:remaining])
            self._observation_cursor = (start + remaining) % len(broad_slugs)
        # V4 observation eligibility is intentionally independent of live
        # restrictions.  Only actually subscribed markets enter the shadow
        # allocation pool, so stale REST candidates cannot be "filled"
        # without an L2/tape feed.
        self.observation_tracker.set_live_candidate_slugs(slugs)
        for slug in slugs:
            item = observation_by_slug.get(slug)
            if item is None:
                item = next((candidate for candidate in candidates if candidate.market and candidate.market.market_id == slug), None)
            if item is not None and item.market is not None:
                raw = item.market.raw or {}
                try:
                    tick_size = float(raw.get("orderPriceMinTickSize") or 0.01)
                except (TypeError, ValueError):
                    tick_size = 0.01
                event_dt = event_or_close_datetime(item.market)
                self.observation_tracker.register_market(
                    slug,
                    tick_size=tick_size,
                    question=item.question,
                    event_id=str(item.market.event_id or ""),
                    event_or_close_epoch=(
                        event_dt.timestamp() if event_dt is not None else None
                    ),
                    raw=raw,
                )
        with self._candidates_lock:
            self._candidates = candidates
            self._observation_universe = observation_markets
            self._watched_slugs = slugs
            # Replaced (not merged) each refresh -- a market that's since been
            # delisted should age out rather than linger with stale data.
            self._extra_raw_by_slug = raw_by_slug_out
        try:
            self.market_ws.set_market_slugs(slugs)
        except Exception as exc:  # noqa: BLE001 -- an ordinary WS reconnect race must never crash the bot
            # set_market_slugs() deliberately lets a send failure during a
            # connection-drop/reconnect race propagate (see
            # ws_market_data.py) so a later call retries the failed slugs --
            # it never marks them as subscribed on failure. This method is
            # called directly from run_forever()'s main loop via
            # _maybe_refresh_candidates(), which has no try/except of its
            # own, and run_forever()'s only handler catches KeyboardInterrupt
            # -- an uncaught exception here used to crash the entire bot
            # process on what is an ordinary, expected event (a real,
            # since-fixed bug). _candidates/_extra_raw_by_slug above are
            # already updated regardless of this failure, so the only
            # consequence is the market-data WS subscription lagging until
            # the next refresh.
            logger.warning(
                "Could not update market-data WS subscriptions this refresh "
                "(will retry next refresh): %s", exc,
            )
        logger.info(
            "Streaming runner watching %d markets (%d currently quote-eligible before observation gate).",
            len(slugs), len(quote_slugs),
        )
        # The candidate scan itself (this method's actual job) succeeded --
        # a WS-subscription send failure above is caught and self-heals on
        # its own next refresh, so it doesn't count as a failure here.
        return True

    def _get_candidates(self):
        with self._candidates_lock:
            candidates = list(self._candidates)
        scores = (
            self._compute_activity_scores()
            if self.settings.activity_rerank_enabled else {}
        )
        # Re-rank the already-watched pool every quote cycle, not merely at
        # the next expensive market scan.  If no actual trade activity has
        # been observed, preserve the selector's original order exactly.
        if any(score > 0 for score in scores.values()):
            candidates = sorted(
                candidates,
                key=lambda scored: scores.get(
                    scored.market.market_id if scored.market is not None else "", 0.0,
                ),
                reverse=True,
            )
        if self.settings.observation_only_mode:
            allowed = []
        elif self.settings.observation_gate_enabled:
            allowed = [
                scored for scored in candidates
                if scored.market is not None
                and self.observation_tracker.entry_eligible(scored.market.market_id)[0]
            ]
        else:
            allowed = candidates
        summary = (len(candidates), len(allowed), self.settings.observation_only_mode)
        if summary != self._last_gate_summary:
            logger.info(
                "Observation entry gate: allowed=%d/%d observation_only=%s.",
                len(allowed), len(candidates), self.settings.observation_only_mode,
            )
            self._last_gate_summary = summary
        return allowed

    def _get_extra_raw_by_slug(self) -> dict[str, dict]:
        with self._candidates_lock:
            return dict(self._extra_raw_by_slug)

    def _any_breaker_halted(self) -> bool:
        return (
            self.circuit_breaker.is_halted()
            or self.equity_protection.is_halted()
            or self.session_circuit_breaker.is_halted()
        )

    def _maybe_refresh_candidates(self) -> None:
        """Skips the market scan + WS resubscription entirely while any
        breaker is halted -- that work is only useful once new quotes might
        actually be placed. Forces an immediate refresh on the cycle a halt
        clears (rather than waiting for the next timer window), so
        candidates aren't stale for up to
        websocket_candidate_refresh_seconds after resuming.

        A single due-check gated on _last_candidate_refresh_attempt (the
        last attempt, success or failure), using a SHORTER interval
        (websocket_candidate_refresh_retry_seconds) whenever that last
        attempt failed. Deliberately NOT "due if either the normal OR retry
        interval elapsed" checked against their own separate baselines: a
        failed attempt leaves _last_candidate_refresh (success) frozen at
        its old value (0.0 if there's never been one), and time.monotonic()
        has an arbitrary epoch (often system boot, not process start) -- so
        `now - 0.0` is already far past 900s on the very next tick,
        permanently satisfying a same-baseline "normal" check and
        completely bypassing the retry throttle for as long as refreshes
        keep failing. A single shared baseline avoids that."""
        # Observation collection has no entry risk and remains useful while
        # a trading breaker is halted.  Keep rotating the watched universe
        # in observation-only mode even though quote placement is disabled.
        halted = self._any_breaker_halted() and not self.settings.observation_only_mode
        now = time.monotonic()
        resumed = self._was_halted and not halted
        min_gap = (
            self.settings.websocket_candidate_refresh_retry_seconds
            if self._last_candidate_refresh_failed
            else self.settings.websocket_candidate_refresh_seconds
        )
        due = now - self._last_candidate_refresh_attempt >= min_gap
        in_progress = (
            self._candidate_refresh_thread is not None and self._candidate_refresh_thread.is_alive()
        )
        if not halted and (due or resumed) and not in_progress:
            self._last_candidate_refresh_attempt = now
            self._candidate_refresh_thread = threading.Thread(
                target=self._refresh_candidates_in_background,
                name="polymarket-candidate-refresh",
                daemon=True,
            )
            self._candidate_refresh_thread.start()
        self._was_halted = halted

    def _refresh_candidates_in_background(self) -> None:
        """Runs _refresh_candidates() (a full ~5000-market scan plus up to
        settings.liquidity_fallback_max_lookups sequential, unpaced L2
        order-book REST lookups -- confirmed capable of taking well over a
        minute) on its own thread so it can never block the main
        refresh_quotes() loop. A confirmed real incident: 38 gaps totaling
        ~81 minutes (~14% of a 9h40m run) where quote management stopped
        entirely while this ran synchronously in-line every
        websocket_candidate_refresh_seconds window. See live/RUNBOOK.md's
        most recent section.

        _refresh_candidates() itself is unchanged -- it already builds
        candidates/raw data fully before swapping self._candidates under
        self._candidates_lock, so it's already safe to call from any
        thread. Only _last_candidate_refresh/_last_candidate_refresh_failed
        are updated here, after completion, under the same lock, so
        _maybe_refresh_candidates() on the main thread always sees a
        consistent view. Not joined at shutdown -- unlike the WS threads,
        this holds no resource needing a clean close."""
        succeeded = self._refresh_candidates()
        with self._candidates_lock:
            self._last_candidate_refresh_failed = not succeeded
            if succeeded:
                self._last_candidate_refresh = time.monotonic()

    def _run_one_cycle(self, *, drain_only: bool = False) -> None:
        # Defense in depth: run_forever() does not call this in passive
        # mode, but direct callers/tests must not accidentally enter the
        # private-state, breaker, maker, or cancel-all paths either.
        if self.settings.observation_only_mode:
            return
        # Best-effort, and deliberately BEFORE either halt check below -- a
        # fill that already executed on the exchange must be persisted
        # regardless of whether this cycle is about to halt.
        self._persist_new_fills()
        self._compute_due_markouts()

        _total_now, daily_pnl, session_pnl = self._estimate_pnl_figures()

        if _total_now is None:
            logger.error(
                "P/L state is unavailable; cancelling known resting orders and "
                "skipping this cycle so breaker protection never fails open."
            )
            self._cancel_all_resting_orders(context="P/L-unavailable safeguard")
            return

        self._latest_total_pnl = _total_now
        protected_session_pnl = session_pnl
        if self._session_metrics is not None:
            try:
                session_record = self._session_metrics.update(_total_now)
                if session_record.get("session_pnl_source") == "flat_round_trip_fills":
                    fill_pnl = session_record.get("flat_round_trip_fill_pnl_usd")
                    if fill_pnl is not None:
                        # The position-endpoint delta overstated both latest
                        # sessions as profitable. Once session fills return
                        # flat, signed fill cashflow is exact and the more
                        # conservative loss figure must drive protection.
                        protected_session_pnl = min(session_pnl, float(fill_pnl))
            except Exception as exc:  # noqa: BLE001 -- diagnostics must not stop risk controls
                logger.warning("Could not update session metrics: %s", exc)

        # Same accuracy correction as the session breaker above, but
        # windowed to today's UTC fills instead of since-session-start --
        # the daily breaker's window can span multiple live-start processes,
        # so it can't reuse the session's fill-count baseline. Without this,
        # the exact position-endpoint overstatement that required a fix for
        # the session breaker could just as easily mask a real loss from the
        # daily breaker, which never resets except at UTC midnight.
        protected_daily_pnl = daily_pnl
        try:
            daily_fill_pnl = self._daily_flat_round_trip_fill_pnl_usd()
            if daily_fill_pnl is not None:
                protected_daily_pnl = min(daily_pnl, daily_fill_pnl)
        except Exception as exc:  # noqa: BLE001 -- diagnostics must not stop risk controls
            logger.warning("Could not compute today's flat-round-trip fill P/L: %s", exc)

        open_orders_snapshot = self.private_store.open_orders_snapshot()
        if self.circuit_breaker.evaluate(
            total_pnl_usd=protected_daily_pnl,
            client=self.client,
            open_orders=open_orders_snapshot,
        ):
            logger.warning("Circuit breaker halted -- skipping this refresh cycle.")
            return

        if self.session_circuit_breaker.evaluate(
            total_pnl_usd=protected_session_pnl,
            client=self.client,
            open_orders=open_orders_snapshot,
        ):
            logger.warning("Session circuit breaker halted -- skipping this refresh cycle.")
            return

        halted, size_multiplier = self.equity_protection.evaluate(
            total_pnl_usd=daily_pnl,
            client=self.client,
            lifetime_pnl_usd=_total_now,
            open_orders=open_orders_snapshot,
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

        breaker_risk = self._compute_breaker_risk(protected_daily_pnl, protected_session_pnl)

        candidates = [] if drain_only else self._get_candidates()
        cycles: list = []
        try:
            cycles = self.maker.refresh_quotes(
                candidates=candidates,
                settings_override=settings_override,
                extra_raw_by_slug=self._get_extra_raw_by_slug(),
                breaker_risk=breaker_risk,
                drain_only=drain_only,
            ) or []
        except EmergencySafeguardFailedError:
            # Deliberately NOT caught like an ordinary refresh failure --
            # this means the last-resort emergency cancel safeguard itself
            # couldn't be confirmed to have worked. Left to propagate to
            # run_forever()'s own top-level `except Exception: raise`,
            # which stops the process rather than retrying next cycle.
            raise
        except Exception as exc:  # noqa: BLE001 -- keep the bot alive across one bad cycle
            logger.error("Streaming refresh cycle failed: %s", exc)

        if not drain_only and self.settings.pilot_mode:
            # Keep this pilot's own dedicated shadow-comparison profile
            # tracking exactly the markets that actually got an order posted
            # or retained THIS cycle (PostedLeg.is_resting), not the
            # pre-execution candidate list -- refresh_quotes() can skip a
            # candidate (depth/edge/budget/reduce-only) or place an order for
            # an orphaned held position that was never in `candidates` at
            # all, so only its returned cycles reflect what the real maker
            # actually did. This is an evidence-quality improvement, not
            # core trading logic -- a failure here must not stop trading,
            # which has already happened above by this point.
            try:
                self.observation_tracker.override_profile_allocation(
                    self.settings.pilot_strategy_profile,
                    [
                        cycle.market_id for cycle in cycles
                        if cycle.bid.is_resting or cycle.ask.is_resting
                    ],
                )
            except Exception as exc:  # noqa: BLE001 -- diagnostics/evidence-quality only, must not block trading
                logger.warning("Could not pin pilot shadow allocation: %s", exc)

    def _finish_pilot_flat(self) -> None:
        """Force every position flat, then verify orders and positions empty.

        This bypasses entry/breaker checks deliberately: after the pilot
        window (or Ctrl+C/crash cleanup), reducing risk is mandatory even if
        a loss breaker has already tripped.
        """
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                self.maker.refresh_quotes(
                    candidates=[],
                    extra_raw_by_slug=self._get_extra_raw_by_slug(),
                    breaker_risk=True,
                    drain_only=True,
                    force_flatten_all=True,
                )
                open_orders, positions = verify_flat_for_observation(self.client)
                self.private_store.seed_open_orders(open_orders)
                self.private_store.seed_positions(positions)
                # Leave the private feed alive briefly so a force-flat IOC's
                # execution event can be persisted before acceptance is
                # assessed. This is attribution only; flatness above came
                # from authoritative REST state.
                time.sleep(1.0)
                self._persist_new_fills()
                self._compute_due_markouts()
                logger.warning("Guarded pilot shutdown verified flat.")
                return
            except Exception as exc:  # noqa: BLE001 -- retry bounded force-flat
                last_error = exc
                logger.error("Pilot force-flat verification attempt failed: %s", exc)
                if attempt < 2:
                    time.sleep(2.0)
        raise PilotFlatnessError(
            "Guarded pilot exited fail-closed: account could not be verified "
            f"flat after three force-flat attempts ({last_error})."
        )

    def _record_pilot_acceptance(self, verified_flat: bool) -> None:
        """Persist the fixed acceptance decision; never changes parameters."""
        try:
            if (
                self._pilot_start_fill_count is None
                or self._pilot_start_epoch is None
            ):
                result = {
                    "recorded_at": utcnow_iso(),
                    "status": "REVIEW_REQUIRED",
                    "pilot_status": "NOT_STARTED",
                    "qualification_bypassed": (
                        self.settings.pilot_qualification_bypassed
                    ),
                    "pilot_strategy_profile": self.settings.pilot_strategy_profile,
                    "shadow_comparison_profile": self.settings.pilot_strategy_profile,
                    "comparison_status": "UNAVAILABLE",
                    "real_fill_count": 0,
                    "completed_real_round_trip_count": 0,
                    "flat_round_trip_fill_pnl_usd": None,
                    "verified_flat_shutdown": verified_flat,
                    "breaker_breach": False,
                    "real_markout_sample_count": 0,
                    "shadow_markout_sample_count": 0,
                    "real_avg_markout_5m_cents": None,
                    "shadow_avg_markout_5m_cents": None,
                    "markouts_reasonably_match": False,
                    "automatic_scaling_permitted": False,
                }
                records = storage.load_json(PILOT_RESULTS_FILE, default=[])
                if not isinstance(records, list):
                    records = []
                records.append(result)
                storage.save_json(PILOT_RESULTS_FILE, records)
                logger.warning(
                    "Pilot acceptance result: REVIEW_REQUIRED (pilot never "
                    "reached its session attribution marker). No historical "
                    "fills or shadow evidence were attributed to this attempt."
                )
                return

            new_fills = get_all_fills()[self._pilot_start_fill_count:]
            fill_pnl = flat_round_trip_fill_pnl(new_fills)
            by_market: dict[tuple[object, object], dict[str, float]] = {}
            for fill in new_fills:
                key = (fill.get("market_slug"), fill.get("outcome"))
                bucket = by_market.setdefault(key, {"count": 0.0, "net": 0.0})
                bucket["count"] += 1
                shares = float(fill.get("shares") or 0.0)
                bucket["net"] += shares if fill.get("side") == "BUY" else -shares
            completed_round_trips = sum(
                1 for item in by_market.values()
                if item["count"] >= 2 and abs(item["net"]) <= 1e-6
            )
            real_markouts = [
                float(fill["markout_5m_cents"])
                for fill in new_fills
                if fill.get("markout_5m_cents") is not None
            ]
            real_avg_markout = (
                sum(real_markouts) / len(real_markouts)
                if real_markouts else None
            )
            shadow = self.observation_tracker.session_shadow_markouts(
                self.settings.pilot_strategy_profile,
                self._pilot_start_epoch,
            )
            shadow_avg_markout = shadow.get("avg_markout_5m_cents")
            shadow_markout_count = int(shadow.get("sample_count") or 0)
            comparison_available = (
                bool(real_markouts)
                and shadow_markout_count > 0
                and real_avg_markout is not None
                and shadow_avg_markout is not None
            )
            markouts_match = (
                comparison_available
                and abs(real_avg_markout - float(shadow_avg_markout))
                <= max(5.0, abs(float(shadow_avg_markout)) * 2)
            )
            comparison_status = (
                "UNAVAILABLE"
                if not comparison_available
                else ("MATCH" if markouts_match else "MISMATCH")
            )
            breaker_breach = (
                self.circuit_breaker.is_halted()
                or self.session_circuit_breaker.is_halted()
            )
            # "Mechanically valid" means every structural check passed --
            # real fills, a completed round trip, a verified-flat shutdown,
            # no breaker breach, matching markouts, and a computable P&L --
            # but says nothing about whether the session was profitable.
            # "ACCEPTED" additionally requires positive fill_pnl; a
            # mechanically clean but unprofitable session (e.g. the first
            # real pilot's -$0.10 result) must not be reported as accepted.
            mechanically_valid = (
                bool(new_fills)
                and completed_round_trips >= 1
                and verified_flat
                and not breaker_breach
                and markouts_match
                and fill_pnl is not None
            )
            accepted = mechanically_valid and fill_pnl > 0
            if accepted:
                status = "ACCEPTED"
            elif mechanically_valid:
                status = "MECHANICALLY_VALID"
            else:
                status = "REVIEW_REQUIRED"
            result = {
                "recorded_at": utcnow_iso(),
                "status": status,
                "pilot_status": "STARTED",
                "qualification_bypassed": (
                    self.settings.pilot_qualification_bypassed
                ),
                "pilot_strategy_profile": self.settings.pilot_strategy_profile,
                "shadow_comparison_profile": self.settings.pilot_strategy_profile,
                "comparison_status": comparison_status,
                "real_fill_count": len(new_fills),
                "completed_real_round_trip_count": completed_round_trips,
                "flat_round_trip_fill_pnl_usd": fill_pnl,
                "verified_flat_shutdown": verified_flat,
                "breaker_breach": breaker_breach,
                "real_markout_sample_count": len(real_markouts),
                "shadow_markout_sample_count": shadow_markout_count,
                "real_avg_markout_5m_cents": real_avg_markout,
                "shadow_avg_markout_5m_cents": shadow_avg_markout,
                "markouts_reasonably_match": markouts_match,
                "automatic_scaling_permitted": False,
            }
            records = storage.load_json(PILOT_RESULTS_FILE, default=[])
            if not isinstance(records, list):
                records = []
            records.append(result)
            storage.save_json(PILOT_RESULTS_FILE, records)
            logger.warning(
                "Pilot acceptance result: %s (fills=%d round_trips=%d "
                "flat=%s breaker=%s markouts_match=%s). No automatic "
                "scaling is permitted.",
                result["status"], len(new_fills), completed_round_trips,
                verified_flat, breaker_breach, markouts_match,
            )
        except Exception as exc:  # noqa: BLE001 -- attribution must not hide flatness result
            logger.exception("Could not persist pilot acceptance result: %s", exc)

    def _compute_breaker_risk(self, daily_pnl: float, session_pnl: float) -> bool:
        """True if today's daily P&L or this session's P&L has crossed
        breaker_risk_warning_fraction (default 0.75) of its respective
        circuit breaker's loss limit -- used as the "breaker risk" component
        of reduce-only exit urgency (see MarketMaker.reduce_urgent), so
        exits get more aggressive as either breaker approaches tripping,
        even before it actually trips. See live/RUNBOOK.md's most recent
        section."""
        warning_fraction = self.settings.breaker_risk_warning_fraction
        daily_limit = self.circuit_breaker.settings.daily_loss_limit_usd
        if daily_limit and daily_pnl <= -abs(daily_limit) * warning_fraction:
            return True
        session_limit = self.session_circuit_breaker.settings.loss_limit_usd
        if session_limit and session_pnl <= -abs(session_limit) * warning_fraction:
            return True
        return False

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
            affected_slugs: set[str] = set()
            for execution in self.private_store.recent_executions():
                if not is_actual_fill(execution):
                    continue  # order-lifecycle noise (NEW/CANCELED/REJECTED/...), not a fill
                fill_id = execution.get("id") or execution.get("tradeId")
                if not fill_id or fill_id in already_seen:
                    continue
                _order_id, market_slug = resolve_order_id_and_market_slug(execution, order_details)
                current_bbo = None
                if market_slug is not None:
                    affected_slugs.add(market_slug)
                    if market_slug not in bbo_cache:
                        bbo_cache[market_slug] = self.maker.read_client.get_market_bbo(market_slug)
                    current_bbo = bbo_cache[market_slug]
                record_fill(build_fill_record(execution, order_details, current_bbo))
            if affected_slugs:
                self.maker.note_fills(affected_slugs)
        except Exception as exc:  # noqa: BLE001 -- must never interrupt the refresh cycle
            logger.error("Could not persist new fills this cycle: %s", exc)

    def _compute_due_markouts(self) -> None:
        """Scans fills.json for any fill whose real 1-minute/5-minute/
        15-minute mark (from its actual exchange transact_time, not
        detected_at) has passed and hasn't been resolved yet -- computes it
        against a fresh BBO, or resolves it to None if the mark is too stale
        to mean anything (see live/RUNBOOK.md's "-9." section). Feeds
        newly-computed 1-minute markouts into self.maker.toxicity_tracker.
        Best-effort, must never interrupt the refresh cycle."""
        if not self.settings.markout_tracking_enabled:
            return
        try:
            fills = get_all_fills()
            now = datetime.now(timezone.utc)
            windows = (("1m", 60.0), ("5m", 300.0), ("15m", self.settings.markout_long_window_seconds))
            due = find_due_markout_windows(
                fills, now, self.settings.markout_max_staleness_seconds, windows=windows,
            )
            if not due:
                return

            bbo_cache: dict[str, Optional[dict]] = {}
            now_iso = utcnow_iso()
            changed = False
            newly_computed_1m: list[tuple[str, float]] = []

            for item in due:
                cents_field = f"markout_{item['window']}_cents"
                computed_field = f"markout_{item['window']}_computed_at"
                executable_cents_field = f"executable_markout_{item['window']}_cents"

                if item["status"] == "stale":
                    fills[item["index"]][cents_field] = None
                    fills[item["index"]][executable_cents_field] = None
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
                executable_markout_cents = compute_executable_markout_cents(
                    item["side"], item["price"], bbo["best_bid"], bbo["best_ask"],
                )
                fills[item["index"]][cents_field] = markout_cents
                fills[item["index"]][executable_cents_field] = executable_markout_cents
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

    def _estimate_pnl_figures(self) -> tuple[Optional[float], float, float]:
        """Fetches get_total_position_pnl_usd() ONCE and derives both the
        daily-diffed (UTC-midnight-resetting) and session-diffed (since this
        process started) figures from it -- avoids a second positions fetch
        for the new session breaker. Returns (total_now, daily_pnl,
        session_pnl); total_now is None only if complete P/L state cannot be
        established, in which case the caller fails closed for the cycle."""
        positions = self.private_store.positions_snapshot()
        if (
            self.settings.private_order_state_enabled
            and positions is not None
            and self.private_store.is_healthy(self.settings.private_state_stale_after_seconds)
        ):
            total_now = strategy_total_pnl_usd(positions)
        else:
            total_now = get_total_position_pnl_usd(self.client)
        if total_now is None:
            logger.warning(
                "P/L could not be computed this cycle; trading will remain "
                "halted until breaker state is trustworthy."
            )
            return None, 0.0, 0.0
        daily_pnl = ledger_module.diff_against_baseline(total_now, ledger_module.DAILY_BALANCE_FILE)
        session_pnl = self.session_circuit_breaker.diff(total_now)
        return total_now, daily_pnl, session_pnl

    def _daily_flat_round_trip_fill_pnl_usd(self) -> Optional[float]:
        """Today's (UTC) counterpart to session_metrics.py's
        flat_round_trip_fill_pnl: exact signed fill cashflow, but windowed
        by transact_time's UTC date instead of a since-session-start fill
        count, since the daily circuit breaker's window can span multiple
        live-start processes and has no such baseline to slice from. Returns
        None (no correction available) whenever inventory hasn't returned
        flat today, exactly like the session-scoped version."""
        today = utcnow_iso()[:10]
        todays_fills = [
            fill for fill in get_all_fills()
            if isinstance(fill, dict) and str(fill.get("transact_time") or "")[:10] == today
        ]
        return flat_round_trip_fill_pnl(todays_fills)
