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

import base64
import threading
import time
from typing import Any, Optional

import requests

from .. import config
from ..logger import get_logger
from .credentials import ApiCredentials

logger = get_logger("live.us_client")


class UsApiError(Exception):
    """Raised when a Polymarket US API request ultimately fails."""


class CryptographyDependencyMissing(RuntimeError):
    pass


class LiveUsClient:
    """Narrow, locked wrapper around Polymarket US's authenticated REST API."""

    def __init__(
        self,
        credentials: ApiCredentials,
        settings: config.LiveTradingSettings,
        session: Optional[requests.Session] = None,
    ):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError as exc:
            raise CryptographyDependencyMissing(
                "cryptography is not installed. Run: pip install cryptography"
            ) from exc

        self._lock = threading.RLock()
        self._settings = settings
        self._credentials = credentials
        self._session = session or requests.Session()
        decoded = base64.b64decode(credentials.secret_key)
        self._private_key = Ed25519PrivateKey.from_private_bytes(decoded[:32])

    # ------------------------------------------------------------------
    def _signed_headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method}{path}"
        signature = base64.b64encode(self._private_key.sign(message.encode())).decode()
        return {
            "X-PM-Access-Key": self._credentials.key_id,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": signature,
            "Content-Type": "application/json",
        }

    def websocket_headers(self, path: str) -> dict[str, str]:
        """Signed headers for the WebSocket opening handshake."""
        headers = self._signed_headers("GET", path)
        headers.pop("Content-Type", None)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        """`path` (unqueried) is what gets signed -- Polymarket US's signing
        scheme covers only the route, not the query string. `params` is
        appended by requests separately and must NOT be folded into `path`,
        or the server's own signature check (which reconstructs the message
        from the bare path) will reject it with a 401."""
        url = f"{self._settings.api_base_url}{path}"
        with self._lock:
            headers = self._signed_headers(method, path)
            try:
                response = self._session.request(
                    method, url, headers=headers, json=json_body, params=params, timeout=10,
                )
                response.raise_for_status()
                if not response.content:
                    return {}
                return response.json()
            except requests.RequestException as exc:
                raise UsApiError(f"{method} {path} failed: {exc}") from exc

    # ------------------------------------------------------------------
    def whoami(self) -> dict[str, Any]:
        """Cheap connectivity/auth check -- places no order, cancels nothing."""
        return self._request("GET", "/v1/whoami")

    def get_account_balances(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/v1/account/balances")
        return data.get("balances", []) if isinstance(data, dict) else []

    def get_position(self, market_slug: str) -> Optional[dict[str, Any]]:
        """Returns the position dict for this market (net shares, cost basis,
        current value), or None if no position is held there."""
        data = self._request("GET", "/v1/portfolio/positions", params={"market": market_slug})
        positions = data.get("positions", {}) if isinstance(data, dict) else {}
        return positions.get(market_slug)

    def get_all_positions(self) -> dict[str, dict[str, Any]]:
        """Returns every held position on the account, keyed by market slug --
        unlike get_position(), with no market filter. Needed so a position on
        a market that's fallen out of active candidate ranking is still
        visible and can keep being managed instead of silently abandoned.

        Only fetches the first page: this account is small enough that
        pagination isn't expected in practice, but if the API reports more
        pages exist, that's surfaced as a warning rather than failing silently.
        """
        data = self._request("GET", "/v1/portfolio/positions")
        if not isinstance(data, dict):
            return {}
        if data.get("eof") is False:
            logger.warning(
                "Positions response has more pages than fetched; some open "
                "positions may not be visible this cycle."
            )
        positions = data.get("positions")
        return positions if isinstance(positions, dict) else {}

    def get_open_orders(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/v1/orders/open")
        if isinstance(data, dict):
            return data.get("orders", data.get("data", []))
        return data if isinstance(data, list) else []

    def create_order(
        self,
        market_slug: str,
        outcome_side: str,
        action: str,
        price: float,
        quantity: float,
        tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    ) -> dict[str, Any]:
        body = {
            "marketSlug": market_slug,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{price:.6f}", "currency": "USD"},
            "quantity": quantity,
            "tif": tif,
            "outcomeSide": outcome_side,
            "action": action,
        }
        return self._request("POST", "/v1/orders", json_body=body)

    def cancel_order(self, order_id: str, market_slug: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/v1/order/{order_id}/cancel", json_body={"marketSlug": market_slug}
        )

    def cancel_all(self) -> None:
        """No dedicated cancel-all endpoint is documented -- cancels each
        currently open order individually."""
        for order in self.get_open_orders():
            order_id = order.get("id") or order.get("orderId")
            market_slug = order.get("marketSlug") or order.get("market_slug")
            if order_id and market_slug:
                try:
                    self.cancel_order(order_id, market_slug)
                except UsApiError as exc:
                    logger.warning("Failed to cancel order %s: %s", order_id, exc)
