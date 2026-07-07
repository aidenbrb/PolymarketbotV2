"""WebSocket-backed private account state for Polymarket US: order fills,
position changes, and balance updates, streamed in near-real-time instead
of only being visible on the next REST poll.

SCAFFOLD ONLY -- see live/RUNBOOK.md. This connects, subscribes, and parses
incoming messages into PrivateStateStore, and IS started/run by
live/ws_runner.py. But nothing in live/market_maker.py or
live/multi_market_maker.py reads from PrivateStateStore yet -- every real
order-placement decision still goes through the already-verified REST
get_position()/get_all_positions() path. docs.polymarket.us documents this
channel's message schema as "key fields include..." rather than an
exhaustive, guaranteed shape, so trusting it to drive real-money decisions
needs verification against the real account first. That verification is a
deliberate follow-up, not an oversight -- do not assume this feeds trading
decisions without checking live/market_maker.py directly.
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


class PrivateStateStore:
    """Most recent order-execution, position, and balance update messages
    seen over the private WebSocket. Read-only scaffolding -- see module
    docstring for why nothing consumes this for trading decisions yet."""

    def __init__(self, max_recent_executions: int = 50):
        self.max_recent_executions = max_recent_executions
        self._lock = threading.RLock()
        self._recent_executions: list[dict[str, Any]] = []
        self._positions: dict[str, dict[str, Any]] = {}
        self._last_balance_update: Optional[dict[str, Any]] = None

    def handle_message(self, message: dict[str, Any]) -> None:
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

    def _handle_position_update(self, position_update: dict[str, Any]) -> None:
        after = position_update.get("afterPosition")
        if not isinstance(after, dict):
            return
        slug = after.get("marketSlug") or after.get("market_slug")
        if not slug:
            return
        with self._lock:
            self._positions[str(slug)] = after

    def recent_executions(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._recent_executions)

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

            if self._stop_event.is_set():
                break
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
        logger.warning("Private WebSocket error: %s", error)


def _ws_url(api_base_url: str, path: str) -> str:
    base = api_base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}{path}"


def _header_list(headers: dict[str, str]) -> list[str]:
    return [f"{key}: {value}" for key, value in headers.items()]
