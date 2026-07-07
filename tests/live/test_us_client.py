import base64
import json
from urllib.parse import parse_qs, urlparse

import responses

from polymarket_bot import config
from polymarket_bot.live.credentials import ApiCredentials
from polymarket_bot.live.us_client import LiveUsClient, UsApiError

VALID_SECRET = base64.b64encode(b"a" * 32).decode()
CREDS = ApiCredentials(key_id="key-123", secret_key=VALID_SECRET)


def _settings(**overrides):
    defaults = dict(api_base_url="https://api-test.polymarket.us")
    defaults.update(overrides)
    return config.LiveTradingSettings(**defaults)


def _client(settings=None):
    return LiveUsClient(credentials=CREDS, settings=settings or _settings())


def test_client_constructs_from_valid_credentials():
    client = _client()
    assert client is not None


@responses.activate
def test_signed_headers_present_on_every_request():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/whoami", json={"user": "u1"}, status=200
    )
    client = _client(settings)
    client.whoami()

    sent = responses.calls[0].request
    assert sent.headers["X-PM-Access-Key"] == "key-123"
    assert "X-PM-Timestamp" in sent.headers
    assert "X-PM-Signature" in sent.headers


@responses.activate
def test_whoami_returns_parsed_json():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/whoami",
        json={"user": "u1", "firm": "f1"}, status=200,
    )
    result = _client(settings).whoami()
    assert result == {"user": "u1", "firm": "f1"}


@responses.activate
def test_get_account_balances_returns_list():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/account/balances",
        json={"balances": [{"currentBalance": 100.0, "currency": "USD"}]}, status=200,
    )
    balances = _client(settings).get_account_balances()
    assert balances == [{"currentBalance": 100.0, "currency": "USD"}]


@responses.activate
def test_get_position_returns_matching_market():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/portfolio/positions",
        json={"positions": {"m1": {
            "netPositionDecimal": "100.0",
            "cost": {"value": "50.00", "currency": "USD"},
            "cashValue": {"value": "52.00", "currency": "USD"},
        }}},
        status=200,
    )
    position = _client(settings).get_position("m1")
    assert position["netPositionDecimal"] == "100.0"
    assert position["cost"]["value"] == "50.00"
    assert parse_qs(urlparse(responses.calls[0].request.url).query) == {"market": ["m1"]}


@responses.activate
def test_get_position_returns_none_when_no_position_held():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/portfolio/positions",
        json={"positions": {}}, status=200,
    )
    assert _client(settings).get_position("m1") is None


@responses.activate
def test_get_position_signs_the_bare_path_without_query_string():
    # The server's own signature check reconstructs the message from the
    # unqueried path -- folding "?market=..." into the signed path (as an
    # earlier version of this client did) causes a real 401. Verify the
    # signature actually matches signing the bare path, not path+query.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/portfolio/positions",
        json={"positions": {}}, status=200,
    )
    _client(settings).get_position("a/b")

    sent = responses.calls[0].request
    assert parse_qs(urlparse(sent.url).query) == {"market": ["a/b"]}

    private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(VALID_SECRET)[:32])
    public_key = private_key.public_key()
    message = f"{sent.headers['X-PM-Timestamp']}GET/v1/portfolio/positions".encode()
    public_key.verify(base64.b64decode(sent.headers["X-PM-Signature"]), message)


@responses.activate
def test_get_all_positions_returns_full_positions_dict():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/portfolio/positions",
        json={
            "positions": {
                "m1": {"netPositionDecimal": "5.0"},
                "m2": {"netPositionDecimal": "-2.0"},
            },
            "eof": True,
        },
        status=200,
    )
    positions = _client(settings).get_all_positions()
    assert set(positions.keys()) == {"m1", "m2"}
    assert parse_qs(urlparse(responses.calls[0].request.url).query) == {}


@responses.activate
def test_get_all_positions_returns_empty_dict_when_none_held():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/portfolio/positions",
        json={"positions": {}, "eof": True}, status=200,
    )
    assert _client(settings).get_all_positions() == {}


@responses.activate
def test_get_all_positions_warns_when_more_pages_exist(caplog):
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/portfolio/positions",
        json={"positions": {"m1": {"netPositionDecimal": "5.0"}}, "eof": False, "nextCursor": "abc"},
        status=200,
    )
    with caplog.at_level("WARNING"):
        positions = _client(settings).get_all_positions()
    assert "m1" in positions
    assert any("paginated" in r.message.lower() or "more pages" in r.message.lower() for r in caplog.records)


@responses.activate
def test_get_open_orders_handles_orders_key():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open",
        json={"orders": [{"id": "o1"}]}, status=200,
    )
    orders = _client(settings).get_open_orders()
    assert orders == [{"id": "o1"}]


@responses.activate
def test_create_order_sends_expected_body():
    settings = _settings()
    responses.add(
        responses.POST, f"{settings.api_base_url}/v1/orders",
        json={"id": "order-1"}, status=200,
    )
    client = _client(settings)
    result = client.create_order(
        market_slug="m1", outcome_side="OUTCOME_SIDE_YES", action="ORDER_ACTION_BUY",
        price=0.49, quantity=100.0,
    )
    assert result == {"id": "order-1"}

    body = json.loads(responses.calls[0].request.body)
    assert body["marketSlug"] == "m1"
    assert body["outcomeSide"] == "OUTCOME_SIDE_YES"
    assert body["action"] == "ORDER_ACTION_BUY"
    assert body["price"]["value"] == "0.490000"
    assert body["quantity"] == 100.0


@responses.activate
def test_cancel_order_posts_market_slug():
    settings = _settings()
    responses.add(
        responses.POST, f"{settings.api_base_url}/v1/order/order-1/cancel",
        json={}, status=200,
    )
    client = _client(settings)
    client.cancel_order("order-1", "m1")
    body = json.loads(responses.calls[0].request.body)
    assert body == {"marketSlug": "m1"}


@responses.activate
def test_cancel_all_cancels_each_open_order():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open",
        json={"orders": [
            {"id": "o1", "marketSlug": "m1"},
            {"id": "o2", "marketSlug": "m2"},
        ]},
        status=200,
    )
    responses.add(
        responses.POST, f"{settings.api_base_url}/v1/order/o1/cancel", json={}, status=200
    )
    responses.add(
        responses.POST, f"{settings.api_base_url}/v1/order/o2/cancel", json={}, status=200
    )
    client = _client(settings)
    client.cancel_all()

    cancel_calls = [c for c in responses.calls if "/cancel" in c.request.url]
    assert len(cancel_calls) == 2


@responses.activate
def test_cancel_all_skips_orders_missing_fields_and_continues():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open",
        json={"orders": [{"id": "o1"}, {"id": "o2", "marketSlug": "m2"}]},
        status=200,
    )
    responses.add(
        responses.POST, f"{settings.api_base_url}/v1/order/o2/cancel", json={}, status=200
    )
    client = _client(settings)
    client.cancel_all()  # must not raise despite o1 missing marketSlug

    cancel_calls = [c for c in responses.calls if "/cancel" in c.request.url]
    assert len(cancel_calls) == 1


@responses.activate
def test_request_failure_raises_us_api_error():
    settings = _settings()
    responses.add(responses.GET, f"{settings.api_base_url}/v1/whoami", status=500)
    client = _client(settings)
    import pytest
    with pytest.raises(UsApiError):
        client.whoami()


def test_missing_cryptography_dependency_gives_friendly_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("no module named cryptography")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    from polymarket_bot.live.us_client import CryptographyDependencyMissing
    import pytest

    with pytest.raises(CryptographyDependencyMissing):
        LiveUsClient(credentials=CREDS, settings=_settings())
