"""Standalone Ed25519 request/WebSocket-handshake signing, extracted from
LiveUsClient (live/us_client.py) so a caller that only needs signed
headers for the public market-data WebSocket -- never order placement or
account access -- has no way to reach those capabilities even by mistake.

Deliberately narrow: credential loading + signing + websocket_headers()
only. No place_order/cancel_order/get_open_orders/get_all_positions/
get_position/cancel_all -- those exist only on LiveUsClient, the one
class allowed to place or cancel a real order (see
tests/test_no_live_orders.py). LiveUsClient composes a WebSocketAuthSigner
internally and delegates to it; this class has no dependency on
LiveUsClient and can be constructed on its own.
"""

from __future__ import annotations

import base64
import time

from .credentials import ApiCredentials


class CryptographyDependencyMissing(RuntimeError):
    pass


class WebSocketAuthSigner:
    """Ed25519 signing capability only -- produces byte-identical headers
    to LiveUsClient's own signing (see
    tests/live/test_ws_auth_signer.py's header-equivalence tests)."""

    def __init__(self, credentials: ApiCredentials):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError as exc:
            raise CryptographyDependencyMissing(
                "cryptography is not installed. Run: pip install cryptography"
            ) from exc

        self._credentials = credentials
        decoded = base64.b64decode(credentials.secret_key)
        self._private_key = Ed25519PrivateKey.from_private_bytes(decoded[:32])

    def signed_headers(self, method: str, path: str) -> dict[str, str]:
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
        headers = self.signed_headers("GET", path)
        headers.pop("Content-Type", None)
        return headers
