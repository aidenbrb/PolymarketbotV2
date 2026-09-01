import base64
from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live.credentials import ApiCredentials
from polymarket_bot.live.us_client import LiveUsClient
from polymarket_bot.live.ws_auth_signer import (
    CryptographyDependencyMissing,
    WebSocketAuthSigner,
)

VALID_SECRET = base64.b64encode(b"a" * 32).decode()
CREDS = ApiCredentials(key_id="key-123", secret_key=VALID_SECRET)


def _settings(**overrides):
    defaults = dict(api_base_url="https://api-test.polymarket.us")
    defaults.update(overrides)
    return config.LiveTradingSettings(**defaults)


def test_websocket_headers_are_byte_identical_to_live_us_client(monkeypatch):
    """The extraction must not change real authentication at all: a signer
    constructed standalone must produce the exact same headers LiveUsClient
    produces for the same credentials, path, and timestamp."""
    fixed_timestamp_ms = 1_700_000_000_000
    monkeypatch.setattr(
        "polymarket_bot.live.ws_auth_signer.time.time",
        lambda: fixed_timestamp_ms / 1000,
    )

    client = LiveUsClient(credentials=CREDS, settings=_settings())
    signer = WebSocketAuthSigner(CREDS)

    assert signer.websocket_headers("/v1/ws/markets") == client.websocket_headers(
        "/v1/ws/markets"
    )


def test_signed_headers_are_byte_identical_to_live_us_client(monkeypatch):
    fixed_timestamp_ms = 1_700_000_000_000
    monkeypatch.setattr(
        "polymarket_bot.live.ws_auth_signer.time.time",
        lambda: fixed_timestamp_ms / 1000,
    )

    client = LiveUsClient(credentials=CREDS, settings=_settings())
    signer = WebSocketAuthSigner(CREDS)

    assert signer.signed_headers("GET", "/v1/orders") == client._signed_headers(
        "GET", "/v1/orders"
    )


def test_signer_has_no_order_or_account_methods():
    """Structural guarantee: WebSocketAuthSigner must never grow an
    order-placement or account-access method, even by accident -- a caller
    that only holds a signer (e.g. the dry-run module) must have no path
    to real trading capability."""
    banned_prefixes = (
        "place_", "create_order", "post_order", "cancel_",
        "get_open_orders", "get_all_positions", "get_position", "whoami",
    )
    for attr_name in dir(WebSocketAuthSigner):
        assert not any(attr_name.startswith(p) for p in banned_prefixes), (
            f"WebSocketAuthSigner must not define '{attr_name}'"
        )


def test_signer_raises_friendly_error_without_cryptography_dependency(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("no module named cryptography")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(CryptographyDependencyMissing):
        WebSocketAuthSigner(CREDS)
