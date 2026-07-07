"""WebSocket-backed live market data for Polymarket US."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional

from .. import config
from ..logger import get_logger
from ..polymarket_client import PolymarketClient

logger = get_logger("live.ws_market_data")

MARKETS_WS_PATH = "/v1/ws/markets"
SUBSCRIPTION_TYPE_MARKET_DATA = 1


class WebSocketDependencyMissing(RuntimeError):
    pass


class StreamingMarketDataStore:
    def __init__(self, stale_after_seconds: float = 10.0):
        self.stale_after_seconds = stale_after_seconds
        self._lock = threading.RLock()
        self._books: dict[str, dict[str, Any]] = {}
        self._bbo: dict[str, dict[str, Any]] = {}
        self._updated_at: dict[str, float] = {}

    def update_message(self, message: dict[str, Any]) -> None:
        market_data = _first_dict(message, "marketData", "market_data")
        if market_data:
            self.update_market_data(market_data)

        market_data_lite = _first_dict(message, "marketDataLite", "market_data_lite")
        if market_data_lite:
            self.update_market_data_lite(market_data_lite)

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

        with self._lock:
            if book["bids"] or book["asks"]:
                self._books[slug] = book
            if bbo:
                self._bbo[slug] = bbo
            self._updated_at[slug] = time.monotonic()

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
        with self._lock:
            self._bbo[slug] = bbo
            self._updated_at[slug] = time.monotonic()

    def get_market_book(self, slug: str) -> Optional[dict[str, Any]]:
        with self._lock:
            if self._is_stale_locked(slug):
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
            if self._is_stale_locked(slug):
                return None
            bbo = self._bbo.get(slug)
            return dict(bbo) if bbo else None

    def age_seconds(self, slug: str) -> Optional[float]:
        with self._lock:
            updated = self._updated_at.get(slug)
            if updated is None:
                return None
            return time.monotonic() - updated

    def _is_stale_locked(self, slug: str) -> bool:
        updated = self._updated_at.get(slug)
        return updated is None or (time.monotonic() - updated) >= self.stale_after_seconds


class StreamingReadClient:
    """Read client used by MarketMaker: prefer fresh WebSocket data, then REST."""

    def __init__(self, store: StreamingMarketDataStore, fallback: Optional[PolymarketClient] = None):
        self.store = store
        self.fallback = fallback or PolymarketClient()

    def get_market_book(self, slug: str) -> Optional[dict[str, Any]]:
        return self.store.get_market_book(slug) or self.fallback.get_market_book(slug)

    def get_market_bbo(self, slug: str) -> Optional[dict[str, Any]]:
        return self.store.get_market_bbo(slug) or self.fallback.get_market_bbo(slug)


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
        self._lock = threading.RLock()

    def set_market_slugs(self, market_slugs: list[str]) -> None:
        with self._lock:
            self._market_slugs = list(dict.fromkeys(market_slugs))[: self.settings.websocket_subscription_limit]
            ws = self._ws
        if ws is not None:
            self._send_subscription(ws)

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
            except Exception as exc:  # noqa: BLE001
                logger.warning("Market WebSocket disconnected: %s", exc)
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
            _ws_url(self.settings.api_base_url, MARKETS_WS_PATH),
            header=_header_list(self.signed_headers(MARKETS_WS_PATH)),
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
        )

    def _on_open(self, ws) -> None:
        logger.info("Market WebSocket connected.")
        self._send_subscription(ws)

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
            logger.warning("Market WebSocket error: %s", message.get("error"))
            return
        if isinstance(message, dict):
            self.store.update_message(message)

    def _on_error(self, _ws, error) -> None:
        logger.warning("Market WebSocket error: %s", error)

    def _send_subscription(self, ws) -> None:
        with self._lock:
            market_slugs = list(self._market_slugs)
        if not market_slugs:
            return
        request = {
            "subscribe": {
                "request_id": f"market-data-{int(time.time() * 1000)}",
                "subscription_type": SUBSCRIPTION_TYPE_MARKET_DATA,
                "market_slugs": market_slugs,
                "responses_debounced": self.settings.websocket_responses_debounced,
            }
        }
        ws.send(json.dumps(request))
        logger.info("Subscribed WebSocket market data for %d markets.", len(market_slugs))


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
