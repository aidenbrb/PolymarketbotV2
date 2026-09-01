"""Ed25519-signed HTTP client for Polymarket US's authenticated trading API.

This is the ONLY file in polymarket_bot allowed to contain live order
placement/cancellation code -- see tests/test_no_live_orders.py, which
enforces that boundary against the rest of the package.

Signs every request per Polymarket US's documented scheme: the message
`timestamp + method + path` is signed with the account's Ed25519 private
key, base64-encoded, and sent as X-PM-Signature alongside X-PM-Access-Key
(the key id) and X-PM-Timestamp (ms since epoch). Timestamps must be within
30 seconds of server time.

NOTE (see live/RUNBOOK.md): exact response-field names for order IDs are not
fully verified against a real account yet, and whether OUTCOME_SIDE_YES/NO
applies cleanly to non-binary (e.g. sports team moneyline) markets is
unverified -- both flagged as pre-go-live checklist items.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

import requests

from .. import config
from ..logger import get_logger
from .credentials import ApiCredentials
from .ws_auth_signer import CryptographyDependencyMissing, WebSocketAuthSigner

logger = get_logger("live.us_client")


class UsApiError(Exception):
    """Raised when a Polymarket US API request ultimately fails."""


def _is_not_found(exc: UsApiError) -> bool:
    """True only for a genuine 404 -- _request() preserves the original
    requests exception as __cause__ (`raise ... from last_exc`), so its
    response status is still introspectable even after being wrapped."""
    response = getattr(exc.__cause__, "response", None)
    return response is not None and response.status_code == 404


def is_client_rejection(exc: UsApiError) -> bool:
    """True for a definite, UNAMBIGUOUS 4xx -- the exchange synchronously
    validated and rejected this specific request, so nothing was created
    and there's no uncertainty about whether it succeeded (unlike a 5xx/
    timeout/connection failure, genuinely uncertain whether the request
    was processed). Not underscore-prefixed -- market_maker.py::_post_leg
    imports this to tell a clean order rejection (e.g. a
    participateDontInitiate order that would have crossed the book) apart
    from an uncertain placement state. Same __cause__-introspection
    pattern as _is_not_found.

    Deliberately EXCLUDES 409 Conflict: confirmed via docs.polymarket.us's
    error-handling reference, a 409 on order creation can mean "duplicate
    order with the same ClOrdID" -- genuinely ambiguous about whether an
    EARLIER attempt actually succeeded, not a clean "nothing was placed"
    outcome. The docs' own guidance for a 409 is to verify order state via
    the API, not assume nothing happened -- treated here as uncertain,
    same as a 5xx, so it still triggers the account-wide cancel safeguard
    rather than being silently skipped. See live/RUNBOOK.md's most recent
    section."""
    response = getattr(exc.__cause__, "response", None)
    if response is None:
        return False
    return 400 <= response.status_code < 500 and response.status_code != 409


class LiveUsClient:
    """Narrow, locked wrapper around Polymarket US's authenticated REST API."""

    def __init__(
        self,
        credentials: ApiCredentials,
        settings: config.LiveTradingSettings,
        session: Optional[requests.Session] = None,
    ):
        self._lock = threading.RLock()
        self._settings = settings
        self._credentials = credentials
        self._session = session or requests.Session()
        # Raises CryptographyDependencyMissing if `cryptography` isn't
        # installed -- preserved by delegating construction, not catching it.
        self._signer = WebSocketAuthSigner(credentials)

    # ------------------------------------------------------------------
    def _signed_headers(self, method: str, path: str) -> dict[str, str]:
        return self._signer.signed_headers(method, path)

    def websocket_headers(self, path: str) -> dict[str, str]:
        """Signed headers for the WebSocket opening handshake."""
        return self._signer.websocket_headers(path)

    def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        retryable: bool = False,
    ) -> Any:
        """`path` (unqueried) is what gets signed -- Polymarket US's signing
        scheme covers only the route, not the query string. `params` is
        appended by requests separately and must NOT be folded into `path`,
        or the server's own signature check (which reconstructs the message
        from the bare path) will reject it with a 401.

        `retryable=True` (only ever passed by the three read-only methods --
        get_open_orders/get_all_positions/get_position) retries up to
        settings.request_max_retries times with exponential backoff, harder
        on a 429 specifically (settings.rate_limit_backoff_multiplier).
        Order-placement/cancellation stay single-attempt fail-fast (the
        default) -- retrying a write blindly risks a duplicate action if the
        first attempt actually succeeded server-side but the response was
        lost. Signed headers are regenerated on EVERY attempt, never reused
        across a retry -- the signature's timestamp must be within 30s of
        server time, which a reused signature would violate after a
        backoff sleep."""
        url = f"{self._settings.api_base_url}{path}"
        max_attempts = self._settings.request_max_retries if retryable else 1
        with self._lock:
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                headers = self._signed_headers(method, path)
                try:
                    response = self._session.request(
                        method, url, headers=headers, json=json_body, params=params,
                        timeout=self._settings.request_timeout_seconds,
                    )
                    response.raise_for_status()
                    if not response.content:
                        return {}
                    return response.json()
                except requests.RequestException as exc:
                    last_exc = exc
                    if attempt >= max_attempts:
                        break
                    is_rate_limited = (
                        exc.response is not None and exc.response.status_code == 429
                    )
                    wait = self._retry_wait_seconds(exc, attempt, is_rate_limited)
                    logger.warning(
                        "Retryable %s %s failure (attempt %d/%d): %s. Retrying in %.1fs.",
                        method, path, attempt, max_attempts, exc, wait,
                    )
                    time.sleep(wait)
        raise UsApiError(f"{method} {path} failed: {last_exc}") from last_exc

    def _retry_wait_seconds(
        self, exc: requests.RequestException, attempt: int, is_rate_limited: bool
    ) -> float:
        wait = self._settings.request_backoff_base_seconds * (2 ** (attempt - 1))
        if is_rate_limited:
            wait *= self._settings.rate_limit_backoff_multiplier
        retry_after = self._parse_retry_after(exc)
        if retry_after is not None:
            # A real response has sent Retry-After: 0 on a repeated 429.
            # Treat it as a lower bound, not permission to retry instantly.
            return max(wait, retry_after) if is_rate_limited else retry_after
        return wait

    @staticmethod
    def _parse_retry_after(exc: requests.RequestException) -> Optional[float]:
        """Best-effort: honor a Retry-After header (seconds form) if the
        server sends one. Not confirmed whether Polymarket US actually sends
        this -- falls through to the exponential-backoff calculation if
        absent or unparseable, never raises."""
        response = getattr(exc, "response", None)
        if response is None:
            return None
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    def whoami(self) -> dict[str, Any]:
        """Cheap connectivity/auth check -- places no order, cancels nothing."""
        return self._request("GET", "/v1/whoami")

    def get_account_balances(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/v1/account/balances")
        return data.get("balances", []) if isinstance(data, dict) else []

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        """Returns the order's full state by exchange-assigned ID, including
        `cumQuantity`/`avgPx`/commission fields for a FILLED or CANCELED
        order -- unlike get_open_orders(), this works for terminal orders
        too. Returns None only when the exchange has no record of this
        order_id at all (a 404); any other failure (network, 5xx, rate
        limit) still raises UsApiError after retrying, same as
        get_position()/get_open_orders(). Used to backfill an execution the
        private WebSocket never delivered (a disconnect can silently and
        permanently drop a fill otherwise -- see live/RUNBOOK.md's most
        recent section).

        CONFIRMED against a real order (2026-07-19): the response is
        wrapped in an {"order": {...}} envelope (a GetOrderResponse), not
        the bare order object -- unwrapped here so every caller works with
        the order fields directly, same convention as get_position()
        unwrapping its own "positions" envelope."""
        try:
            data = self._request("GET", f"/v1/order/{order_id}", retryable=True)
        except UsApiError as exc:
            if _is_not_found(exc):
                return None
            raise
        order = data.get("order") if isinstance(data, dict) else None
        return order if isinstance(order, dict) else None

    def get_position(self, market_slug: str) -> Optional[dict[str, Any]]:
        """Returns the position dict for this market (net shares, cost basis,
        current value), or None if no position is held there."""
        data = self._request(
            "GET", "/v1/portfolio/positions", params={"market": market_slug}, retryable=True,
        )
        positions = data.get("positions", {}) if isinstance(data, dict) else {}
        return positions.get(market_slug)

    def get_all_positions(self) -> dict[str, dict[str, Any]]:
        """Returns every held position on the account, keyed by market slug --
        unlike get_position(), with no market filter. Needed so a position on
        a market that's fallen out of active candidate ranking is still
        visible and can keep being managed instead of silently abandoned.

        Follows cursor pagination to completion. A missing/repeated cursor or
        malformed page raises instead of returning partial state that could
        hide a position from exposure controls.
        """
        positions: dict[str, dict[str, Any]] = {}
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        for _page in range(100):
            params = {"cursor": cursor} if cursor else None
            data = self._request(
                "GET", "/v1/portfolio/positions", params=params, retryable=True,
            )
            if not isinstance(data, dict):
                raise UsApiError("Positions endpoint returned a non-object response")
            page_positions = data.get("positions")
            if not isinstance(page_positions, dict):
                raise UsApiError("Positions endpoint omitted its positions object")
            positions.update(page_positions)
            if data.get("eof") is not False:
                return positions
            next_cursor = data.get("nextCursor") or data.get("next_cursor")
            if not next_cursor or str(next_cursor) in seen_cursors:
                raise UsApiError(
                    "Positions pagination did not provide a new cursor; refusing partial state"
                )
            cursor = str(next_cursor)
            seen_cursors.add(cursor)
        raise UsApiError("Positions pagination exceeded 100 pages; refusing partial state")

    def get_open_orders(self) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        for _page in range(100):
            params = {"cursor": cursor} if cursor else None
            data = self._request(
                "GET", "/v1/orders/open", params=params, retryable=True,
            )
            if isinstance(data, list):
                return data
            if not isinstance(data, dict):
                raise UsApiError("Open-orders endpoint returned an invalid response")
            page_orders = data.get("orders", data.get("data", []))
            if not isinstance(page_orders, list):
                raise UsApiError("Open-orders endpoint omitted its orders list")
            orders.extend(order for order in page_orders if isinstance(order, dict))
            if data.get("eof") is not False:
                return orders
            next_cursor = data.get("nextCursor") or data.get("next_cursor")
            if not next_cursor or str(next_cursor) in seen_cursors:
                raise UsApiError(
                    "Open-orders pagination did not provide a new cursor; refusing partial state"
                )
            cursor = str(next_cursor)
            seen_cursors.add(cursor)
        raise UsApiError("Open-orders pagination exceeded 100 pages; refusing partial state")

    def create_order(
        self,
        market_slug: str,
        outcome_side: str,
        action: str,
        price: float,
        quantity: float,
        tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        participate_dont_initiate: bool = False,
    ) -> dict[str, Any]:
        """participate_dont_initiate=True (confirmed via docs.polymarket.us's
        create-order schema as the real `participateDontInitiate` field):
        maker-only -- "order must rest on the book prior to matching; order
        will be rejected if it would immediately match." A clean, definite,
        synchronous rejection, not an uncertain outcome -- see
        market_maker.py::_post_leg for how callers must (and do) treat that
        distinction. See live/RUNBOOK.md's most recent section."""
        body = {
            "marketSlug": market_slug,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{price:.6f}", "currency": "USD"},
            "quantity": quantity,
            "tif": tif,
            "outcomeSide": outcome_side,
            "action": action,
            "participateDontInitiate": participate_dont_initiate,
        }
        return self._request("POST", "/v1/orders", json_body=body)

    def cancel_order(self, order_id: str, market_slug: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/v1/order/{order_id}/cancel", json_body={"marketSlug": market_slug}
        )

    def cancel_all(self, open_orders: Optional[list[dict[str, Any]]] = None) -> None:
        """Cancel every supplied/current order without aborting midway.

        Callers with a reconciled private-WS snapshot should pass it so this
        emergency path still works while the REST open-orders endpoint is
        rate-limited or unavailable.
        """
        if open_orders is None:
            try:
                open_orders = self.get_open_orders()
            except UsApiError as exc:
                logger.error("Cannot enumerate orders for cancel-all: %s", exc)
                return
        for order in open_orders:
            order_id = order.get("id") or order.get("orderId")
            market_slug = order.get("marketSlug") or order.get("market_slug")
            if order_id and market_slug:
                try:
                    self.cancel_order(order_id, market_slug)
                except UsApiError as exc:
                    logger.warning("Failed to cancel order %s: %s", order_id, exc)
