"""Read-only Polymarket US data client.

Uses only PUBLIC, unauthenticated market-data endpoints on Polymarket US's
Gateway API (gateway.polymarket.us) -- markets listing and best-bid/offer.
This is a completely different platform from the international
polymarket.com (Gamma/CLOB APIs) this project targeted earlier -- Polymarket
US is a separate, CFTC-regulated, USD-settled exchange (QCX LLC), and the
user is legally restricted to it.

This module intentionally contains NO methods for placing, signing, or
cancelling orders, and never requires an API key. See
tests/test_no_live_orders.py, which enforces this.
"""

from __future__ import annotations

import math
import time
from typing import Any, Optional

import requests

from . import config
from .logger import get_logger

logger = get_logger("polymarket_client")


class PolymarketClientError(Exception):
    """Raised when a Polymarket US API request ultimately fails."""


class PolymarketClientNotFound(PolymarketClientError):
    """Raised when the API responds 404 -- a clean, final answer (e.g. "this
    market has no settlement yet"), never a transient failure worth
    retrying. _get() raises this immediately, bypassing its own retry loop,
    so a 404 costs exactly one HTTP request."""


class PolymarketClient:
    """Thin, defensive wrapper around Polymarket US's public Gateway API."""

    def __init__(self, settings: Optional[config.APISettings] = None, session: Optional[requests.Session] = None):
        self.settings = settings or config.load_settings().api
        self.session = session or requests.Session()

    # ------------------------------------------------------------------
    # Low-level request helper with retry/backoff
    # ------------------------------------------------------------------
    def _get(self, url: str, params: Optional[dict[str, Any]] = None) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.settings.max_retries + 1):
            try:
                response = self.session.get(
                    url, params=params, timeout=self.settings.timeout_seconds
                )
                if response.status_code == 404:
                    raise PolymarketClientNotFound(f"{url} returned 404")
                if response.status_code == 429:
                    raise PolymarketClientError(f"Rate limited by {url}")
                response.raise_for_status()
                return response.json()
            except PolymarketClientNotFound:
                raise
            except (requests.RequestException, PolymarketClientError, ValueError) as exc:
                last_error = exc
                wait = self.settings.backoff_base_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "Request to %s failed (attempt %d/%d): %s. Retrying in %.1fs",
                    url, attempt, self.settings.max_retries, exc, wait,
                )
                if attempt < self.settings.max_retries:
                    time.sleep(wait)
        raise PolymarketClientError(
            f"Failed to fetch {url} after {self.settings.max_retries} attempts: {last_error}"
        )

    # ------------------------------------------------------------------
    # Gateway API: markets metadata
    # ------------------------------------------------------------------
    def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: Optional[int] = None,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """Fetch markets with safe pagination.

        NOTE: verified live that `active=true` alone does NOT exclude closed
        markets -- always pass `closed=false` explicitly too (the default
        here) to actually get tradable-looking markets.
        """
        return self._paginate(
            f"{self.settings.gateway_base_url}/v1/markets",
            {
                "active": str(active).lower(),
                "closed": str(closed).lower(),
                # The gateway's unsorted/default order currently returns old,
                # long-dated listings first.  The live backend accepts this
                # snake_case value (rather than the documented `createdAt`)
                # and then exposes the newest opportunity set to pagination.
                "orderBy": "created_at",
                "orderDirection": "desc",
            },
            limit=limit,
            max_pages=max_pages,
        )

    def _paginate(
        self,
        url: str,
        base_params: dict[str, Any],
        limit: Optional[int],
        max_pages: int,
    ) -> list[dict[str, Any]]:
        page_size = self.settings.page_limit
        results: list[dict[str, Any]] = []
        offset = 0
        for _ in range(max_pages):
            params = dict(base_params)
            params["limit"] = page_size
            params["offset"] = offset
            try:
                page = self._get(url, params=params)
            except PolymarketClientError as exc:
                logger.error("Pagination stopped for %s at offset %d: %s", url, offset, exc)
                break

            if isinstance(page, dict):
                page = page.get("markets", page.get("data", page.get("results", [])))
            if not isinstance(page, list) or not page:
                break

            results.extend(page)
            if limit is not None and len(results) >= limit:
                return results[:limit]
            if len(page) < page_size:
                break
            offset += page_size

        return results

    # ------------------------------------------------------------------
    # Gateway API: best bid/offer
    # ------------------------------------------------------------------
    def get_market_bbo(self, slug: str, *, priority: bool = False) -> Optional[dict[str, Any]]:
        """Returns the parsed `marketData` object (bestBid/bestAsk/currentPx/
        etc, all as floats) for one market by slug, or None if unavailable.
        `priority` is accepted for interface compatibility with
        StreamingReadClient (whose REST-fallback budget it bypasses) --
        this direct client has no budget of its own, so it's a no-op here."""
        try:
            data = self._get(f"{self.settings.gateway_base_url}/v1/markets/{slug}/bbo")
        except PolymarketClientError as exc:
            logger.warning("No BBO data for market %s: %s", slug, exc)
            return None

        market_data = data.get("marketData") if isinstance(data, dict) else None
        if not market_data:
            return None

        def _quote_value(key: str) -> Optional[float]:
            quote = market_data.get(key)
            if not isinstance(quote, dict):
                return None
            try:
                return float(quote.get("value"))
            except (TypeError, ValueError):
                return None

        return {
            "best_bid": _quote_value("bestBid"),
            "best_ask": _quote_value("bestAsk"),
            "current_price": _quote_value("currentPx"),
            "last_trade_price": _quote_value("lastTradePx"),
        }

    def get_market_book(self, slug: str, *, priority: bool = False) -> Optional[dict[str, Any]]:
        """Returns normalized L2 book data for one market by slug, or None if
        unavailable. Prices and quantities are floats. `priority` is accepted
        for interface compatibility with StreamingReadClient -- a no-op
        here, since this direct client has no REST-fallback budget."""
        try:
            data = self._get(f"{self.settings.gateway_base_url}/v1/markets/{slug}/book")
        except PolymarketClientError as exc:
            logger.warning("No book data for market %s: %s", slug, exc)
            return None

        market_data = data.get("marketData", data) if isinstance(data, dict) else None
        if not isinstance(market_data, dict):
            return None

        def _levels(*keys: str) -> list[dict[str, float]]:
            raw_levels = None
            for key in keys:
                value = market_data.get(key)
                if isinstance(value, list):
                    raw_levels = value
                    break
            if raw_levels is None:
                return []

            normalized: list[dict[str, float]] = []
            for level in raw_levels:
                if not isinstance(level, dict):
                    continue
                price = _level_price(level)
                qty = _level_quantity(level)
                if price is None or qty is None:
                    continue
                normalized.append({"price": price, "quantity": qty})
            return normalized

        bids = _levels("bids")
        asks = _levels("offers", "asks")
        if not bids and not asks:
            return None
        return {"bids": bids, "asks": asks}

    # ------------------------------------------------------------------
    # Gateway API: settlement/resolution (used only by observation-mode
    # settlement-based finalization -- see live/market_observation.py)
    # ------------------------------------------------------------------
    def get_market_settlement(self, slug: str) -> Optional[dict[str, Any]]:
        """Returns {"slug": slug, "settlement": <float 0..1>} once the
        market has a confirmed settlement, or None on a clean 404 ("not
        settled yet" -- a final answer, never retried, see
        PolymarketClientNotFound). Raises PolymarketClientError for
        anything that can't be trusted as a real answer either way
        (malformed body, a response for a different slug, a non-finite or
        out-of-[0,1] value, or an exhausted network/5xx retry) -- callers
        must treat that as genuinely unknown, not as "not settled"."""
        try:
            data = self._get(f"{self.settings.gateway_base_url}/v1/markets/{slug}/settlement")
        except PolymarketClientNotFound:
            return None
        if not isinstance(data, dict):
            raise PolymarketClientError(f"Malformed settlement response for {slug}: {data!r}")
        if data.get("slug") != slug:
            raise PolymarketClientError(
                f"Settlement response slug mismatch for {slug}: got {data.get('slug')!r}"
            )
        try:
            value = float(data.get("settlement"))
        except (TypeError, ValueError):
            raise PolymarketClientError(
                f"Non-numeric settlement value for {slug}: {data.get('settlement')!r}"
            )
        if not math.isfinite(value) or not (0.0 <= value <= 1.0):
            raise PolymarketClientError(f"Settlement value out of range for {slug}: {value!r}")
        return {"slug": slug, "settlement": value}

    def get_market_metadata(self, slug: str) -> Optional[dict[str, Any]]:
        """Returns the raw market metadata dict (closed/status/active/
        updatedAt/ep3SyncedAt/etc) for one market by slug, via the
        singular /v1/market/slug/{slug} route (distinct from the plural
        /v1/markets listing and the /v1/markets/{slug}/bbo|book routes).
        None on a clean 404 (market genuinely doesn't exist at this slug).
        Raises PolymarketClientError for a malformed body."""
        try:
            data = self._get(f"{self.settings.gateway_base_url}/v1/market/slug/{slug}")
        except PolymarketClientNotFound:
            return None
        if not isinstance(data, dict):
            raise PolymarketClientError(f"Malformed market metadata response for {slug}: {data!r}")
        market = data.get("market")
        if not isinstance(market, dict):
            raise PolymarketClientError(f"Market metadata response missing 'market' for {slug}")
        return market


def _level_price(level: dict[str, Any]) -> Optional[float]:
    for key in ("px", "price"):
        value = level.get(key)
        if isinstance(value, dict):
            value = value.get("value")
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    return None


def _level_quantity(level: dict[str, Any]) -> Optional[float]:
    for key in ("qty", "quantity", "size"):
        try:
            return float(level.get(key))
        except (TypeError, ValueError):
            pass
    return None
