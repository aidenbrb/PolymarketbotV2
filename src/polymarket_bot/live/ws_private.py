"""Reconciled private WebSocket account state for live trading decisions.

REST supplies the initial full order/position snapshots and periodically
reconciles them. Between reconciliations, private WebSocket deltas are the
authoritative state used by the market maker. Connection and snapshot health
are explicit so callers can fail closed instead of treating missing state as
an empty account.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional

from .. import config
from ..logger import get_logger
from .ws_market_data import WebSocketDependencyMissing

logger = get_logger("live.ws_private")

PRIVATE_WS_PATH = "/v1/ws/private"
SUBSCRIPTION_TYPE_ORDER = "SUBSCRIPTION_TYPE_ORDER"
SUBSCRIPTION_TYPE_POSITION = "SUBSCRIPTION_TYPE_POSITION"
SUBSCRIPTION_TYPE_ACCOUNT_BALANCE = "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE"

_TERMINAL_ORDER_STATES = frozenset({
    "ORDER_STATE_CANCELED",
    "ORDER_STATE_CANCELLED",
    "ORDER_STATE_FILLED",
    "ORDER_STATE_REJECTED",
    "ORDER_STATE_EXPIRED",
})
# The only non-terminal state actually observed/tested against real data so
# far -- see live/RUNBOOK.md. Every literal in this module is otherwise
# unverified (never cross-checked against a real order's full lifecycle of
# state transitions); an unrecognized state already fails safe (kept as
# open -- see _handle_order_update below), but the opposite direction is
# the real risk: a real terminal state this set doesn't happen to name
# would leave a filled/cancelled order stuck open indefinitely. A truly
# unrecognized value (neither set) is logged once per distinct value so a
# live session's bot.log becomes the way to actually verify this.
_KNOWN_NON_TERMINAL_ORDER_STATES = frozenset({"ORDER_STATE_NEW"})
_ZERO_FILL_TERMINAL_ORDER_STATES = frozenset({
    "ORDER_STATE_CANCELED",
    "ORDER_STATE_CANCELLED",
    "ORDER_STATE_REJECTED",
    "ORDER_STATE_EXPIRED",
})


class PrivateStateStore:
    """Thread-safe, REST-bootstrapped account state updated by WS deltas."""

    def __init__(self, max_recent_executions: int = 50):
        self.max_recent_executions = max_recent_executions
        self._lock = threading.RLock()
        self._recent_executions: list[dict[str, Any]] = []
        self._terminal_no_fill_order_ids: set[str] = set()
        self._open_orders: dict[str, dict[str, Any]] = {}
        self._positions: dict[str, dict[str, Any]] = {}
        self._last_balance_update: Optional[dict[str, Any]] = None
        self._orders_initialized = False
        self._positions_initialized = False
        self._unrecognized_order_states_logged: set[str] = set()
        self._connected = False
        self._last_message_monotonic: Optional[float] = None
        self._order_version = 0
        self._position_version = 0
        self._reconnect_pending = False

    def mark_connected(self) -> None:
        with self._lock:
            # A reconnect resuming the socket is not proof the exchange
            # replayed whatever deltas were missed while disconnected --
            # private WS protocols typically only stream new events going
            # forward, no backfill. is_healthy() reports True again the
            # instant the socket reopens, which on a fast reconnect can
            # happen entirely between two quote cycles, so the caller never
            # observes an unhealthy sample and never knows to force a fresh
            # REST cross-check. Recording the transition here lets the
            # caller force exactly one such check regardless of how briefly
            # (or unobserved) the outage was.
            if not self._connected:
                self._reconnect_pending = True
            self._connected = True
            self._last_message_monotonic = time.monotonic()

    def mark_disconnected(self) -> None:
        with self._lock:
            self._connected = False

    def mark_message(self) -> None:
        with self._lock:
            self._last_message_monotonic = time.monotonic()

    def is_healthy(self, stale_after_seconds: float) -> bool:
        with self._lock:
            if not self._connected or self._last_message_monotonic is None:
                return False
            return time.monotonic() - self._last_message_monotonic <= stale_after_seconds

    def reconnect_pending(self) -> bool:
        """True once a connect/reconnect transition happened that hasn't
        yet been confirmed by a successful post-reconnect REST
        reconciliation -- see mark_connected()."""
        with self._lock:
            return self._reconnect_pending

    def clear_reconnect_pending(self) -> None:
        """Callers should only call this after orders AND positions have
        BOTH been freshly, successfully re-fetched via REST -- clearing it
        on a failed or partial attempt would let a missed-delta gap go
        unconfirmed until the next periodic reconciliation."""
        with self._lock:
            self._reconnect_pending = False

    def seed_open_orders(self, orders: list[dict[str, Any]]) -> None:
        with self._lock:
            self._open_orders = {
                str(order_id): dict(order)
                for order in orders
                if (order_id := _order_id(order))
            }
            self._orders_initialized = True
            self._order_version += 1

    def seed_positions(self, positions: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            self._positions = {str(slug): dict(position) for slug, position in positions.items()}
            self._positions_initialized = True
            self._position_version += 1

    def order_version(self) -> int:
        with self._lock:
            return self._order_version

    def position_version(self) -> int:
        with self._lock:
            return self._position_version

    def reconcile_open_orders(self, orders: list[dict[str, Any]], expected_version: int) -> bool:
        """Install a REST snapshot only if no WS/local delta raced its request."""
        with self._lock:
            if self._order_version != expected_version:
                return False
            self._open_orders = {
                str(order_id): dict(order)
                for order in orders
                if (order_id := _order_id(order))
            }
            self._orders_initialized = True
            self._order_version += 1
            return True

    def reconcile_positions(
        self, positions: dict[str, dict[str, Any]], expected_version: int,
    ) -> bool:
        with self._lock:
            if self._position_version != expected_version:
                return False
            self._positions = {str(slug): dict(position) for slug, position in positions.items()}
            self._positions_initialized = True
            self._position_version += 1
            return True

    def open_orders_snapshot(self) -> Optional[list[dict[str, Any]]]:
        with self._lock:
            if not self._orders_initialized:
                return None
            return [dict(order) for order in self._open_orders.values()]

    def positions_snapshot(self) -> Optional[dict[str, dict[str, Any]]]:
        with self._lock:
            if not self._positions_initialized:
                return None
            return {slug: dict(position) for slug, position in self._positions.items()}

    def replace_market_orders(self, market_slug: str, orders: list[dict[str, Any]]) -> None:
        """Apply a successful local cancel-and-requote immediately.

        The private stream will confirm the same transition shortly; this
        closes the small interval in which the next cycle could otherwise
        act on its own stale pre-cancel snapshot.
        """
        with self._lock:
            if not self._orders_initialized:
                return
            self._open_orders = {
                order_id: order for order_id, order in self._open_orders.items()
                if _market_slug(order) != market_slug
            }
            for order in orders:
                order_id = _order_id(order)
                if order_id:
                    self._open_orders[str(order_id)] = dict(order)
            # Unconditional -- the filter-out above always mutates
            # _open_orders (removes this market's old entries) even when
            # `orders` is empty (a cancel with no replacement). Gating the
            # bump on `if orders:` let that removal go unrecorded, so a
            # racing reconcile_open_orders() could see an unchanged version
            # and install a stale REST snapshot that resurrects the just-
            # removed orders -- a real, since-fixed bug (see
            # upsert_local_orders' identical fix below and RUNBOOK.md's
            # most recent section).
            self._order_version += 1

    def upsert_local_orders(self, orders: list[dict[str, Any]]) -> None:
        """Apply locally-known-successful order placements immediately, so
        a racing REST reconciliation can't silently drop them (see
        replace_market_orders' docstring -- same reasoning, this is the
        "add" counterpart to that method's "replace")."""
        with self._lock:
            if not self._orders_initialized:
                return
            upserted_any = False
            for order in orders:
                order_id = _order_id(order)
                if order_id:
                    self._open_orders[str(order_id)] = dict(order)
                    upserted_any = True
            # Must bump whenever a mutation actually happened -- previously
            # this method never bumped the version at all, meaning a newly
            # posted order (the common case: first time quoting a market,
            # nothing to cancel) could be silently dropped from
            # _open_orders by a racing REST reconcile_open_orders() call
            # that started before this upsert and completed after it, since
            # the version check would see no change and trust the (now
            # stale) REST snapshot instead. See RUNBOOK.md's most recent
            # section.
            if upserted_any:
                self._order_version += 1

    def remove_orders(self, order_ids: list[str]) -> None:
        with self._lock:
            if not self._orders_initialized:
                return
            for order_id in order_ids:
                self._open_orders.pop(str(order_id), None)
            if order_ids:
                self._order_version += 1

    def handle_message(self, message: dict[str, Any]) -> None:
        self.mark_message()
        order_update = message.get("orderSubscriptionUpdate")
        if isinstance(order_update, dict):
            self._handle_order_update(order_update)

        position_update = message.get("positionSubscription")
        if isinstance(position_update, dict):
            self._handle_position_update(position_update)

        balance_update = message.get("accountBalancesUpdate")
        if isinstance(balance_update, dict):
            with self._lock:
                self._last_balance_update = balance_update

    def _handle_order_update(self, order_update: dict[str, Any]) -> None:
        execution = order_update.get("execution")
        if not isinstance(execution, dict):
            return
        with self._lock:
            self._recent_executions.append(execution)
            self._recent_executions = self._recent_executions[-self.max_recent_executions:]
            order = execution.get("order")
            if not isinstance(order, dict):
                return
            order_id = _order_id(order)
            if not order_id:
                return
            state = str(order.get("state") or "").upper()
            if (
                state
                and state not in _TERMINAL_ORDER_STATES
                and state not in _KNOWN_NON_TERMINAL_ORDER_STATES
                and state not in self._unrecognized_order_states_logged
            ):
                self._unrecognized_order_states_logged.add(state)
                logger.warning(
                    "Order %s reported unrecognized state %r -- neither a known "
                    "terminal nor non-terminal value. Treating it as still open "
                    "(fail-safe), but this state has never been verified against "
                    "real data -- see live/RUNBOOK.md.",
                    order_id, state,
                )
            if state in _TERMINAL_ORDER_STATES:
                self._open_orders.pop(str(order_id), None)
                # An explicit zero cumulative quantity on a cancel/reject/
                # expiry is authoritative proof that REST backfill has
                # nothing to find.  Do not infer zero from a missing field,
                # and never classify FILLED this way.
                cum_quantity = _optional_float(order.get("cumQuantity"))
                if state in _ZERO_FILL_TERMINAL_ORDER_STATES and cum_quantity == 0.0:
                    self._terminal_no_fill_order_ids.add(str(order_id))
            else:
                self._open_orders[str(order_id)] = dict(order)
            self._order_version += 1

    def _handle_position_update(self, position_update: dict[str, Any]) -> None:
        after = position_update.get("afterPosition")
        if not isinstance(after, dict):
            return
        slug = after.get("marketSlug") or after.get("market_slug")
        if not slug:
            return
        if not _position_is_valid(after):
            logger.warning("Ignoring malformed private position update for %s.", slug)
            return
        with self._lock:
            if _position_is_flat(after):
                self._positions.pop(str(slug), None)
            else:
                self._positions[str(slug)] = after
            self._position_version += 1

    def recent_executions(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._recent_executions)

    def terminal_no_fill_order_ids(self) -> set[str]:
        """Orders the private stream authoritatively closed with zero fills."""
        with self._lock:
            return set(self._terminal_no_fill_order_ids)

    def get_position(self, market_slug: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._positions.get(market_slug)

    def last_balance_update(self) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._last_balance_update


class PrivateWebSocketClient:
    """Connects to Polymarket US's authenticated private WebSocket and feeds
    incoming messages into a PrivateStateStore. Deliberately mirrors
    live/ws_market_data.py::LiveMarketWebSocketClient's connect/reconnect
    shape -- see that module for the proven pattern this follows."""

    def __init__(
        self,
        settings: config.LiveTradingSettings,
        signed_headers: Callable[[str], dict[str, str]],
        store: PrivateStateStore,
        websocket_app_factory: Optional[Callable[..., Any]] = None,
    ):
        self.settings = settings
        self.signed_headers = signed_headers
        self.store = store
        self.websocket_app_factory = websocket_app_factory
        self._ws = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._connected_this_attempt = False

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
            except Exception as exc:  # noqa: BLE001 -- keep retrying, never crash the bot
                logger.warning("Private WebSocket disconnected: %s", exc)
            finally:
                with self._lock:
                    self._ws = None
                self.store.mark_disconnected()

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
            _ws_url(self.settings.api_base_url, PRIVATE_WS_PATH),
            header=_header_list(self.signed_headers(PRIVATE_WS_PATH)),
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
        )

    def _on_open(self, ws) -> None:
        self._connected_this_attempt = True
        self.store.mark_connected()
        logger.info("Private WebSocket connected.")
        for subscription_type in (
            SUBSCRIPTION_TYPE_ORDER, SUBSCRIPTION_TYPE_POSITION, SUBSCRIPTION_TYPE_ACCOUNT_BALANCE,
        ):
            request = {
                "subscribe": {
                    "requestId": f"private-{subscription_type}-{int(time.time() * 1000)}",
                    "subscriptionType": subscription_type,
                }
            }
            ws.send(json.dumps(request))
        logger.info("Subscribed to private order/position/balance updates.")

    def _on_message(self, ws, raw_message: str) -> None:
        try:
            message = json.loads(raw_message)
        except (TypeError, json.JSONDecodeError):
            return
        if isinstance(message, dict) and "heartbeat" in message:
            self.store.mark_message()
            try:
                ws.send(json.dumps({"heartbeat": {}}))
            except Exception:  # noqa: BLE001
                pass
            return
        if isinstance(message, dict) and message.get("error"):
            logger.warning("Private WebSocket error: %s", message.get("error"))
            return
        if isinstance(message, dict):
            self.store.handle_message(message)

    def _on_error(self, _ws, error) -> None:
        self.store.mark_disconnected()
        logger.warning("Private WebSocket error: %s", error)


def _order_id(order: dict[str, Any]) -> Optional[str]:
    value = order.get("id") or order.get("orderId") or order.get("order_id")
    return str(value) if value else None


def _market_slug(order: dict[str, Any]) -> Optional[str]:
    value = order.get("marketSlug") or order.get("market_slug")
    return str(value) if value else None


def _position_is_flat(position: dict[str, Any]) -> bool:
    value = position.get("netPositionDecimal")
    if value is None:
        value = position.get("net_position")
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _position_is_valid(position: dict[str, Any]) -> bool:
    value = position.get("netPositionDecimal")
    if value is None:
        value = position.get("net_position")
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _optional_float(value: Any) -> Optional[float]:
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
