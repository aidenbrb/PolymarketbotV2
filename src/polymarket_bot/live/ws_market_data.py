"""WebSocket-backed live market data for Polymarket US."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any, Callable, Optional
from uuid import uuid4

from .. import config
from ..logger import get_logger
from ..polymarket_client import PolymarketClient

logger = get_logger("live.ws_market_data")

MARKETS_WS_PATH = "/v1/ws/markets"
SUBSCRIPTION_TYPE_MARKET_DATA = 1
SUBSCRIPTION_TYPE_TRADE = 3


class WebSocketDependencyMissing(RuntimeError):
    pass


class StreamingMarketDataStore:
    def __init__(
        self,
        stale_after_seconds: float = 10.0,
        activity_window_seconds: float = 300.0,
        observation_tracker: Optional[Any] = None,
    ):
        self.stale_after_seconds = stale_after_seconds
        # Deliberately separate from stale_after_seconds -- that's a SHORT
        # safety threshold (is this BBO/book still trustworthy to quote
        # from), this is a LONGER trend window for real-activity ranking
        # (see market_selection.py's activity_scores). Same 300s default as
        # VolatilityTracker (volatility_filter.py), a different tracker for
        # a different purpose but the same "recent rolling window" shape.
        self.activity_window_seconds = activity_window_seconds
        self.observation_tracker = observation_tracker
        self._lock = threading.RLock()
        self._books: dict[str, dict[str, Any]] = {}
        self._bbo: dict[str, dict[str, Any]] = {}
        self._book_updated_at: dict[str, float] = {}
        self._bbo_updated_at: dict[str, float] = {}
        # Real-activity tracking -- all three previously discarded by this
        # store (confirmed live against docs.polymarket.us: sharesTraded/
        # openInterest already arrive in marketData's stats and
        # marketDataLite, but only currentPx/lastTradePx were ever read out
        # of them; trade events arrive on a completely separate,
        # never-subscribed SUBSCRIPTION_TYPE_TRADE channel). See
        # live/RUNBOOK.md's most recent section.
        self._shares_traded_samples: dict[str, list[tuple[float, float]]] = {}
        self._open_interest: dict[str, float] = {}
        self._book_update_times: dict[str, list[float]] = {}
        self._trades: dict[str, list[tuple[float, float, float]]] = {}

    def record_feed_activity(self) -> None:
        tracker = self.observation_tracker
        if tracker is None:
            return
        try:
            tracker.record_feed_activity()
        except Exception as exc:  # noqa: BLE001 -- feed bookkeeping must not break the socket
            logger.warning("Could not record market WebSocket activity: %s", exc)

    def update_message(self, message: dict[str, Any]) -> None:
        market_data = _first_dict(message, "marketData", "market_data")
        if market_data:
            self.update_market_data(market_data)

        market_data_lite = _first_dict(message, "marketDataLite", "market_data_lite")
        if market_data_lite:
            self.update_market_data_lite(market_data_lite)

        trade = _first_dict(message, "trade")
        if trade:
            self.update_trade(trade)

    def update_market_data(self, market_data: dict[str, Any]) -> None:
        slug = _market_slug(market_data)
        if not slug:
            return
        book = _normalize_book(market_data)
        bbo = _bbo_from_book(book)
        stats = market_data.get("stats") if isinstance(market_data.get("stats"), dict) else {}
        if stats:
            bbo["current_price"] = _quote_value(stats.get("currentPx") or stats.get("current_px"))
            bbo["last_trade_price"] = _quote_value(stats.get("lastTradePx") or stats.get("last_trade_px"))

        has_book_update = any(
            key in market_data for key in ("bids", "offers", "asks")
        )
        now = time.monotonic()
        with self._lock:
            if has_book_update:
                self._books[slug] = book
                self._book_updated_at[slug] = now
                self._record_book_update_locked(slug, now)
            # Only an actual book payload can refresh tradable BBO
            # freshness. Stats-only messages may contain a last price but
            # must not keep old top-of-book quotes alive.
            if has_book_update:
                self._bbo[slug] = bbo
                self._bbo_updated_at[slug] = now
            if stats:
                self._record_shares_traded_locked(slug, _float_value(stats.get("sharesTraded")), now)
                open_interest = _float_value(stats.get("openInterest"))
                if open_interest is not None:
                    self._open_interest[slug] = open_interest
        if has_book_update and self.observation_tracker is not None:
            try:
                self.observation_tracker.record_book(slug, book)
            except Exception as exc:  # noqa: BLE001 -- observation must not break market data
                logger.warning("Could not record observation book for %s: %s", slug, exc)

    def update_market_data_lite(self, market_data: dict[str, Any]) -> None:
        slug = _market_slug(market_data)
        if not slug:
            return
        bbo = {
            "best_bid": _quote_value(market_data.get("bestBid") or market_data.get("best_bid")),
            "best_ask": _quote_value(market_data.get("bestAsk") or market_data.get("best_ask")),
            "current_price": _quote_value(market_data.get("currentPx") or market_data.get("current_px")),
            "last_trade_price": _quote_value(market_data.get("lastTradePx") or market_data.get("last_trade_px")),
        }
        now = time.monotonic()
        with self._lock:
            self._record_shares_traded_locked(slug, _float_value(market_data.get("sharesTraded")), now)
            open_interest = _float_value(market_data.get("openInterest"))
            if open_interest is not None:
                self._open_interest[slug] = open_interest
            if bbo["best_bid"] is None or bbo["best_ask"] is None:
                return
            self._bbo[slug] = bbo
            self._bbo_updated_at[slug] = now

    def update_trade(self, trade: dict[str, Any]) -> None:
        slug = _market_slug(trade)
        if not slug:
            return
        price = _quote_value(trade.get("price"))
        quantity = _quote_value(trade.get("quantity"))
        if price is None or quantity is None:
            return
        now = time.monotonic()
        with self._lock:
            history = self._prune_locked(self._trades.get(slug, []), now)
            history.append((now, price, quantity))
            self._trades[slug] = history
        if self.observation_tracker is not None:
            maker = trade.get("maker") if isinstance(trade.get("maker"), dict) else {}
            try:
                self.observation_tracker.record_trade(
                    slug,
                    price=price,
                    quantity=quantity,
                    maker_side=str(maker.get("side") or ""),
                    trade_time=str(trade.get("tradeTime") or trade.get("trade_time") or ""),
                )
            except Exception as exc:  # noqa: BLE001 -- observation must not break market data
                logger.warning("Could not record observation trade for %s: %s", slug, exc)

    def _record_book_update_locked(self, slug: str, now: float) -> None:
        history = [t for t in self._book_update_times.get(slug, []) if t >= now - self.activity_window_seconds]
        history.append(now)
        self._book_update_times[slug] = history

    def _record_shares_traded_locked(self, slug: str, value: Optional[float], now: float) -> None:
        if value is None:
            return
        history = self._prune_locked(self._shares_traded_samples.get(slug, []), now)
        history.append((now, value))
        self._shares_traded_samples[slug] = history

    def _prune_locked(self, history: list, now: float) -> list:
        cutoff = now - self.activity_window_seconds
        return [entry for entry in history if entry[0] >= cutoff]

    def recent_trade_count(self, slug: str) -> int:
        with self._lock:
            return len(self._prune_locked(self._trades.get(slug, []), time.monotonic()))

    def recent_trade_quantity(self, slug: str) -> float:
        """Executed shares observed on the trade channel in the window."""
        with self._lock:
            history = self._prune_locked(self._trades.get(slug, []), time.monotonic())
            return sum(max(0.0, entry[2]) for entry in history)

    def shares_traded_growth(self, slug: str) -> Optional[float]:
        """Newest-minus-oldest sharesTraded sample within the activity
        window. None with fewer than two samples -- nothing to compare a
        single reading against."""
        with self._lock:
            history = self._prune_locked(self._shares_traded_samples.get(slug, []), time.monotonic())
            if len(history) < 2:
                return None
            return history[-1][1] - history[0][1]

    def book_update_frequency(self, slug: str) -> float:
        """Book updates per second within the activity window."""
        with self._lock:
            now = time.monotonic()
            cutoff = now - self.activity_window_seconds
            count = len([t for t in self._book_update_times.get(slug, []) if t >= cutoff])
            return count / self.activity_window_seconds if self.activity_window_seconds > 0 else 0.0

    def get_open_interest(self, slug: str) -> Optional[float]:
        """Latest observed value -- not windowed, unlike the other activity
        accessors, since it's a snapshot figure rather than a trend."""
        with self._lock:
            return self._open_interest.get(slug)

    def get_market_book(self, slug: str) -> Optional[dict[str, Any]]:
        with self._lock:
            if self._is_stale_locked(slug, self._book_updated_at):
                return None
            book = self._books.get(slug)
            if not book:
                return None
            return {
                "bids": [dict(level) for level in book.get("bids", [])],
                "asks": [dict(level) for level in book.get("asks", [])],
            }

    def get_market_book_snapshot(
        self, slug: str, *, max_age_seconds: float,
    ) -> Optional[dict[str, Any]]:
        """Return a bounded-age L2 snapshot for shadow deadline exits.

        Live quoting continues to use the much shorter `stale_after_seconds`
        guard.  Observation may model a forced taker exit from the latest
        event-driven book only when it is still within its explicit feed
        health window.
        """
        with self._lock:
            updated = self._book_updated_at.get(slug)
            if (
                updated is None
                or time.monotonic() - updated > max(0.0, max_age_seconds)
            ):
                return None
            book = self._books.get(slug)
            if not book:
                return None
            return {
                "bids": [dict(level) for level in book.get("bids", [])],
                "asks": [dict(level) for level in book.get("asks", [])],
            }

    def get_market_bbo(self, slug: str) -> Optional[dict[str, Any]]:
        with self._lock:
            if self._is_stale_locked(slug, self._bbo_updated_at):
                return None
            bbo = self._bbo.get(slug)
            return dict(bbo) if bbo else None

    def age_seconds(self, slug: str) -> Optional[float]:
        with self._lock:
            updated = self._bbo_updated_at.get(slug)
            if updated is None:
                return None
            return time.monotonic() - updated

    def _is_stale_locked(self, slug: str, timestamps: dict[str, float]) -> bool:
        updated = timestamps.get(slug)
        return updated is None or (time.monotonic() - updated) >= self.stale_after_seconds


class StreamingReadClient:
    """Read client used by MarketMaker: prefer fresh WebSocket data, then
    REST. The REST fallback is rate-limited to a rolling window (default 5
    calls / 60s, thread-safe) so a WS outage degrades to stale/missing
    market data rather than contributing to an account-wide 429 cascade.
    `priority=True` bypasses the budget entirely -- used by
    force_flatten/reduce_urgent exit paths, where a blocked emergency-exit
    fallback is worse than the 429 risk the budget otherwise guards
    against."""

    def __init__(
        self,
        store: StreamingMarketDataStore,
        fallback: Optional[PolymarketClient] = None,
        *,
        rest_fallback_budget: int = 5,
        rest_fallback_window_seconds: float = 60.0,
    ):
        self.store = store
        self.fallback = fallback or PolymarketClient()
        self._rest_fallback_budget = max(0, int(rest_fallback_budget))
        self._rest_fallback_window_seconds = max(0.0, float(rest_fallback_window_seconds))
        self._rest_fallback_calls: deque[float] = deque()
        self._rest_fallback_lock = threading.Lock()
        self._last_budget_warning_at: Optional[float] = None

    def get_market_book(self, slug: str, *, priority: bool = False) -> Optional[dict[str, Any]]:
        cached = self.store.get_market_book(slug)
        if cached is not None:
            return cached
        if not self._consume_rest_fallback_budget(priority=priority):
            return None
        return self.fallback.get_market_book(slug)

    def get_market_bbo(self, slug: str, *, priority: bool = False) -> Optional[dict[str, Any]]:
        cached = self.store.get_market_bbo(slug)
        if cached is not None:
            return cached
        if not self._consume_rest_fallback_budget(priority=priority):
            return None
        return self.fallback.get_market_bbo(slug)

    def _consume_rest_fallback_budget(self, *, priority: bool) -> bool:
        if priority:
            return True
        with self._rest_fallback_lock:
            now = time.monotonic()
            while (
                self._rest_fallback_calls
                and now - self._rest_fallback_calls[0] > self._rest_fallback_window_seconds
            ):
                self._rest_fallback_calls.popleft()
            if len(self._rest_fallback_calls) >= self._rest_fallback_budget:
                if (
                    self._last_budget_warning_at is None
                    or now - self._last_budget_warning_at >= self._rest_fallback_window_seconds
                ):
                    logger.warning(
                        "REST market-data fallback budget exhausted (%d "
                        "calls / %.0fs); skipping non-priority fallback to "
                        "avoid contributing to 429s.",
                        self._rest_fallback_budget,
                        self._rest_fallback_window_seconds,
                    )
                    self._last_budget_warning_at = now
                return False
            self._rest_fallback_calls.append(now)
            return True


class LiveMarketWebSocketClient:
    def __init__(
        self,
        settings: config.LiveTradingSettings,
        signed_headers: Callable[[str], dict[str, str]],
        store: StreamingMarketDataStore,
        websocket_app_factory: Optional[Callable[..., Any]] = None,
    ):
        self.settings = settings
        self.signed_headers = signed_headers
        self.store = store
        self.websocket_app_factory = websocket_app_factory
        self._ws = None
        self._stop_event = threading.Event()
        self._market_slugs: list[str] = []
        # Slugs already confirmed subscribed to THIS connection -- lets
        # set_market_slugs() only send the incremental diff instead of
        # resending the full list every ~300s refresh (the exchange's WS
        # server logs a warning for a redundant re-subscribe). Reset on
        # every reconnect in _on_open, since a fresh connection means the
        # server has no memory of prior subscriptions.
        self._subscribed_slugs: set[str] = set()
        # Keep request ids for the lifetime of this connection.  The US
        # WebSocket rejects a duplicate request_id, and the old millisecond
        # timestamp generated the same id for the market-data and trade
        # subscriptions sent back-to-back.  Remembering the request also
        # lets an asynchronous subscription error invalidate the optimistic
        # local subscription state and force a clean retry.
        self._pending_subscription_requests: dict[str, tuple[int, set[str]]] = {}
        self._connected_this_attempt = False
        self._lock = threading.RLock()

    def set_market_slugs(self, market_slugs: list[str]) -> None:
        with self._lock:
            self._market_slugs = list(dict.fromkeys(market_slugs))[: self.settings.websocket_subscription_limit]
            desired = set(self._market_slugs)
            removed_slugs = self._subscribed_slugs - desired
            new_slugs = [s for s in self._market_slugs if s not in self._subscribed_slugs]
            ws = self._ws
        if ws is not None and removed_slugs:
            # The US feed has no verified unsubscribe message in this
            # codebase. Reconnect so the server-side subscription set is
            # exactly the current candidate set instead of growing forever.
            logger.info(
                "Candidate set removed %d market(s); reconnecting market WebSocket.",
                len(removed_slugs),
            )
            ws.close()
            return
        if ws is not None and new_slugs:
            self._send_subscription(ws, new_slugs)

    def force_reconnect(self) -> None:
        """Close the current socket so run_forever reopens/resubscribes it."""
        with self._lock:
            ws = self._ws
        if ws is None:
            return
        logger.warning(
            "Market WebSocket feed-health watchdog requested a clean reconnect."
        )
        ws.close()

    def stop(self) -> None:
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def run_forever(self) -> None:
        backoff = self.settings.websocket_reconnect_initial_seconds
        while not self._stop_event.is_set():
            try:
                self._connected_this_attempt = False
                ws = self._build_app()
                with self._lock:
                    self._ws = ws
                ws.run_forever()
            except WebSocketDependencyMissing:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Market WebSocket disconnected: %s", exc)
            finally:
                with self._lock:
                    self._ws = None

            if self._stop_event.is_set():
                break
            if self._connected_this_attempt:
                backoff = self.settings.websocket_reconnect_initial_seconds
            time.sleep(backoff)
            backoff = min(backoff * 2, self.settings.websocket_reconnect_max_seconds)

    def _build_app(self):
        factory = self.websocket_app_factory
        if factory is None:
            try:
                import websocket
            except ImportError as exc:
                raise WebSocketDependencyMissing(
                    "websocket-client is not installed. Run: pip install websocket-client"
                ) from exc
            factory = websocket.WebSocketApp

        return factory(
            _ws_url(self.settings.api_base_url, MARKETS_WS_PATH),
            header=_header_list(self.signed_headers(MARKETS_WS_PATH)),
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
        )

    def _on_open(self, ws) -> None:
        logger.info("Market WebSocket connected.")
        # Fresh connection -- the server has no memory of prior
        # subscriptions, so forget what we thought was subscribed and
        # resend the full current list.
        with self._lock:
            self._connected_this_attempt = True
            self._subscribed_slugs = set()
            self._pending_subscription_requests = {}
        self._send_subscription(ws)

    def _on_message(self, ws, raw_message: str) -> None:
        try:
            message = json.loads(raw_message)
        except (TypeError, json.JSONDecodeError):
            return
        if isinstance(message, dict) and "heartbeat" in message:
            self.store.record_feed_activity()
            try:
                ws.send(json.dumps({"heartbeat": {}}))
            except Exception:  # noqa: BLE001
                pass
            return
        if isinstance(message, dict) and message.get("error"):
            logger.warning("Market WebSocket error: %s", message.get("error"))
            request_id = _request_id(message)
            with self._lock:
                failed = self._pending_subscription_requests.pop(request_id, None)
                if failed is not None:
                    self._subscribed_slugs.difference_update(failed[1])
            # There is no verified per-channel unsubscribe/retry protocol in
            # this client.  Reconnecting is the only way to prove the server
            # and local subscription sets agree after an async rejection.
            if failed is not None:
                logger.warning(
                    "Subscription request %s was rejected; reconnecting so it is retried cleanly.",
                    request_id,
                )
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
            return
        if isinstance(message, dict):
            self.store.record_feed_activity()
            request_id = _request_id(message)
            if request_id:
                with self._lock:
                    self._pending_subscription_requests.pop(request_id, None)
            self.store.update_message(message)

    def _on_error(self, _ws, error) -> None:
        logger.warning("Market WebSocket error: %s", error)

    def _send_subscription(self, ws, market_slugs: Optional[list[str]] = None) -> None:
        """market_slugs=None (the _on_open reconnect path) resends the full
        current list. An explicit list (the set_market_slugs incremental
        path) sends only those slugs -- idempotent, avoids re-subscribing
        slugs the server already has us subscribed to.

        Sends TWO subscribe requests for the same slugs -- market data
        (book/BBO/stats, unchanged) and, when activity_tracking_enabled,
        trades (SUBSCRIPTION_TYPE_TRADE -- confirmed via docs.polymarket.us
        as a real, previously-never-subscribed channel; see
        StreamingMarketDataStore.update_trade). A single _subscribed_slugs
        set covers both -- they're always sent together at the same points
        (reconnect, incremental new-slug additions), so there's no need to
        track them separately."""
        if market_slugs is None:
            with self._lock:
                market_slugs = list(self._market_slugs)
        if not market_slugs:
            return
        self._send_subscription_request(ws, market_slugs, SUBSCRIPTION_TYPE_MARKET_DATA)
        if self.settings.activity_tracking_enabled:
            self._send_subscription_request(ws, market_slugs, SUBSCRIPTION_TYPE_TRADE)
        # Only mark as subscribed after a successful send -- if it raised,
        # a later set_market_slugs() call will retry these slugs.
        with self._lock:
            self._subscribed_slugs.update(market_slugs)

    def _send_subscription_request(self, ws, market_slugs: list[str], subscription_type: int) -> None:
        request_id = f"market-{subscription_type}-{uuid4().hex}"
        request = {
            "subscribe": {
                "request_id": request_id,
                "subscription_type": subscription_type,
                "market_slugs": market_slugs,
                "responses_debounced": self.settings.websocket_responses_debounced,
            }
        }
        with self._lock:
            self._pending_subscription_requests[request_id] = (
                subscription_type, set(market_slugs),
            )
        try:
            ws.send(json.dumps(request))
        except Exception:
            with self._lock:
                self._pending_subscription_requests.pop(request_id, None)
            raise
        kind = "trade" if subscription_type == SUBSCRIPTION_TYPE_TRADE else "market data"
        logger.info("Subscribed WebSocket %s for %d markets.", kind, len(market_slugs))


def _request_id(message: dict[str, Any]) -> str:
    """Return an acknowledgement/error request id across observed wrappers."""
    value = message.get("request_id") or message.get("requestId")
    if value is None:
        for key in ("error", "subscription", "subscribe"):
            nested = message.get(key)
            if isinstance(nested, dict):
                value = nested.get("request_id") or nested.get("requestId")
                if value is not None:
                    break
    return str(value) if value is not None else ""


def _normalize_book(market_data: dict[str, Any]) -> dict[str, list[dict[str, float]]]:
    return {
        "bids": _levels(market_data.get("bids")),
        "asks": _levels(market_data.get("offers") or market_data.get("asks")),
    }


def _levels(raw_levels) -> list[dict[str, float]]:
    if not isinstance(raw_levels, list):
        return []
    levels = []
    for level in raw_levels:
        if not isinstance(level, dict):
            continue
        price = _quote_value(level.get("px") or level.get("price"))
        qty = _float_value(level.get("qty") or level.get("quantity") or level.get("size"))
        if price is None or qty is None:
            continue
        levels.append({"price": price, "quantity": qty})
    return levels


def _bbo_from_book(book: dict[str, list[dict[str, float]]]) -> dict[str, Optional[float]]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    return {
        "best_bid": bids[0]["price"] if bids else None,
        "best_ask": asks[0]["price"] if asks else None,
    }


def _market_slug(market_data: dict[str, Any]) -> Optional[str]:
    value = market_data.get("marketSlug") or market_data.get("market_slug")
    return str(value) if value else None


def _first_dict(d: dict[str, Any], *keys: str) -> Optional[dict[str, Any]]:
    for key in keys:
        value = d.get(key)
        if isinstance(value, dict):
            return value
    return None


def _quote_value(value) -> Optional[float]:
    if isinstance(value, dict):
        value = value.get("value")
    return _float_value(value)


def _float_value(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ws_url(api_base_url: str, path: str) -> str:
    base = api_base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}{path}"


def _header_list(headers: dict[str, str]) -> list[str]:
    return [f"{key}: {value}" for key, value in headers.items()]
