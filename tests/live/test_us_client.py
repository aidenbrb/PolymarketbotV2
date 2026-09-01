import base64
import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from polymarket_bot import config
from polymarket_bot.live import us_client as us_client_module
from polymarket_bot.live.credentials import ApiCredentials
from polymarket_bot.live.us_client import LiveUsClient, UsApiError, is_client_rejection

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
def test_get_order_returns_full_terminal_order_state():
    # Real response shape (confirmed 2026-07-19 against a real order): a
    # GetOrderResponse wrapping the order in an {"order": {...}} envelope --
    # NOT the bare order object.
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/order/o1",
        json={"order": {
            "id": "o1", "marketSlug": "m1", "state": "ORDER_STATE_FILLED",
            "cumQuantity": "5.0", "avgPx": {"value": "0.45", "currency": "USD"},
            "commissionNotionalTotalCollected": {"value": "0.02", "currency": "USD"},
        }},
        status=200,
    )
    order = _client(settings).get_order("o1")
    assert order["state"] == "ORDER_STATE_FILLED"
    assert order["cumQuantity"] == "5.0"
    assert order["avgPx"]["value"] == "0.45"


@responses.activate
def test_get_order_returns_none_when_envelope_is_malformed():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/order/o1",
        json={"unexpected": "shape"}, status=200,
    )
    assert _client(settings).get_order("o1") is None


@responses.activate
def test_get_order_returns_none_on_404_not_found():
    settings = _settings(request_max_retries=1)  # a 404 will never succeed -- no need to retry it here
    responses.add(responses.GET, f"{settings.api_base_url}/v1/order/missing", status=404)
    assert _client(settings).get_order("missing") is None


@responses.activate
def test_get_order_still_raises_on_a_non_404_failure(monkeypatch):
    monkeypatch.setattr(us_client_module.time, "sleep", Mock())
    settings = _settings(request_max_retries=1)
    responses.add(responses.GET, f"{settings.api_base_url}/v1/order/o1", status=500)
    with pytest.raises(UsApiError):
        _client(settings).get_order("o1")


@responses.activate
def test_get_order_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(us_client_module.time, "sleep", Mock())
    settings = _settings(request_max_retries=3)
    responses.add(responses.GET, f"{settings.api_base_url}/v1/order/o1", status=429)
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/order/o1",
        json={"order": {"id": "o1", "state": "ORDER_STATE_CANCELED"}}, status=200,
    )
    order = _client(settings).get_order("o1")
    assert order["state"] == "ORDER_STATE_CANCELED"
    assert len(responses.calls) == 2


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
def test_get_all_positions_follows_cursor_pagination():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/portfolio/positions",
        json={"positions": {"m1": {"netPositionDecimal": "5.0"}}, "eof": False, "nextCursor": "abc"},
        status=200,
    )
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/portfolio/positions",
        json={"positions": {"m2": {"netPositionDecimal": "2.0"}}, "eof": True},
        status=200,
    )
    positions = _client(settings).get_all_positions()
    assert set(positions) == {"m1", "m2"}
    assert parse_qs(urlparse(responses.calls[1].request.url).query) == {"cursor": ["abc"]}


@responses.activate
def test_get_all_positions_rejects_partial_page_without_cursor():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/portfolio/positions",
        json={"positions": {"m1": {}}, "eof": False}, status=200,
    )
    with pytest.raises(UsApiError, match="cursor"):
        _client(settings).get_all_positions()


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
def test_get_open_orders_follows_cursor_pagination():
    settings = _settings()
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open",
        json={"orders": [{"id": "o1"}], "eof": False, "nextCursor": "next"},
        status=200,
    )
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open",
        json={"orders": [{"id": "o2"}], "eof": True}, status=200,
    )

    orders = _client(settings).get_open_orders()

    assert [order["id"] for order in orders] == ["o1", "o2"]
    assert parse_qs(urlparse(responses.calls[1].request.url).query) == {"cursor": ["next"]}


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
    assert body["participateDontInitiate"] is False


@responses.activate
def test_create_order_sends_participate_dont_initiate_when_requested():
    settings = _settings()
    responses.add(
        responses.POST, f"{settings.api_base_url}/v1/orders",
        json={"id": "order-1"}, status=200,
    )
    client = _client(settings)
    client.create_order(
        market_slug="m1", outcome_side="OUTCOME_SIDE_YES", action="ORDER_ACTION_BUY",
        price=0.49, quantity=100.0, participate_dont_initiate=True,
    )

    body = json.loads(responses.calls[0].request.body)
    assert body["participateDontInitiate"] is True


class TestIsClientRejection:
    """market_maker.py::_post_leg uses this to tell a definite, unambiguous
    rejection (nothing was placed) apart from a genuinely uncertain
    placement state -- see live/RUNBOOK.md's most recent section."""

    @responses.activate
    def test_true_for_a_definite_400(self):
        settings = _settings(request_max_retries=1)
        responses.add(
            responses.POST, f"{settings.api_base_url}/v1/orders", status=400,
        )
        client = _client(settings)
        with pytest.raises(UsApiError) as excinfo:
            client.create_order(
                market_slug="m1", outcome_side="OUTCOME_SIDE_YES", action="ORDER_ACTION_BUY",
                price=0.49, quantity=100.0,
            )

        assert is_client_rejection(excinfo.value) is True

    @responses.activate
    def test_false_for_409_conflict_duplicate_order(self):
        # Confirmed via docs.polymarket.us's error-handling reference: a
        # 409 on order creation can mean "duplicate order with the same
        # ClOrdID" -- genuinely ambiguous about whether an EARLIER attempt
        # actually succeeded, unlike a clean 400/422/etc validation
        # rejection. Must be treated as uncertain (like a 5xx), not a
        # benign skip.
        settings = _settings(request_max_retries=1)
        responses.add(
            responses.POST, f"{settings.api_base_url}/v1/orders", status=409,
        )
        client = _client(settings)
        with pytest.raises(UsApiError) as excinfo:
            client.create_order(
                market_slug="m1", outcome_side="OUTCOME_SIDE_YES", action="ORDER_ACTION_BUY",
                price=0.49, quantity=100.0,
            )

        assert is_client_rejection(excinfo.value) is False

    @responses.activate
    def test_false_for_a_5xx(self, monkeypatch):
        monkeypatch.setattr(us_client_module.time, "sleep", Mock())
        settings = _settings(request_max_retries=1)
        responses.add(
            responses.POST, f"{settings.api_base_url}/v1/orders", status=500,
        )
        client = _client(settings)
        with pytest.raises(UsApiError) as excinfo:
            client.create_order(
                market_slug="m1", outcome_side="OUTCOME_SIDE_YES", action="ORDER_ACTION_BUY",
                price=0.49, quantity=100.0,
            )

        assert is_client_rejection(excinfo.value) is False

    def test_false_when_the_cause_carries_no_response(self):
        assert is_client_rejection(UsApiError("no cause at all")) is False


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
def test_cancel_all_uses_supplied_snapshot_without_rest_read():
    settings = _settings()
    responses.add(
        responses.POST, f"{settings.api_base_url}/v1/order/o1/cancel", json={}, status=200,
    )

    _client(settings).cancel_all(open_orders=[{"id": "o1", "marketSlug": "m1"}])

    assert len(responses.calls) == 1
    assert responses.calls[0].request.method == "POST"


@responses.activate
def test_request_failure_raises_us_api_error():
    settings = _settings()
    responses.add(responses.GET, f"{settings.api_base_url}/v1/whoami", status=500)
    client = _client(settings)
    import pytest
    with pytest.raises(UsApiError):
        client.whoami()


@responses.activate
def test_get_open_orders_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(us_client_module.time, "sleep", Mock())
    settings = _settings(request_max_retries=3)
    responses.add(responses.GET, f"{settings.api_base_url}/v1/orders/open", status=429)
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open",
        json={"orders": [{"id": "o1"}]}, status=200,
    )
    orders = _client(settings).get_open_orders()
    assert orders == [{"id": "o1"}]
    assert len(responses.calls) == 2


@responses.activate
def test_get_open_orders_exhausts_retries_and_raises_us_api_error(monkeypatch):
    monkeypatch.setattr(us_client_module.time, "sleep", Mock())
    settings = _settings(request_max_retries=3)
    for _ in range(3):
        responses.add(responses.GET, f"{settings.api_base_url}/v1/orders/open", status=429)
    with pytest.raises(UsApiError):
        _client(settings).get_open_orders()
    assert len(responses.calls) == 3


@responses.activate
def test_retry_regenerates_signed_headers_per_attempt(monkeypatch):
    monkeypatch.setattr(us_client_module.time, "sleep", Mock())
    settings = _settings(request_max_retries=3)
    responses.add(responses.GET, f"{settings.api_base_url}/v1/orders/open", status=429)
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open",
        json={"orders": []}, status=200,
    )
    client = _client(settings)
    spy = Mock(wraps=client._signed_headers)
    client._signed_headers = spy

    client.get_open_orders()

    assert spy.call_count == 2


@responses.activate
def test_429_backs_off_harder_than_generic_error(monkeypatch):
    sleeps = []
    monkeypatch.setattr(us_client_module.time, "sleep", lambda s: sleeps.append(s))
    settings = _settings(
        request_max_retries=2, request_backoff_base_seconds=1.0, rate_limit_backoff_multiplier=4.0,
    )

    responses.add(responses.GET, f"{settings.api_base_url}/v1/orders/open", status=429)
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open", json={"orders": []}, status=200,
    )
    _client(settings).get_open_orders()
    rate_limited_wait = sleeps[0]

    responses.add(responses.GET, f"{settings.api_base_url}/v1/orders/open", status=503)
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open", json={"orders": []}, status=200,
    )
    sleeps.clear()
    _client(settings).get_open_orders()
    generic_wait = sleeps[0]

    assert rate_limited_wait == pytest.approx(generic_wait * 4.0)


@responses.activate
def test_retry_after_header_used_when_present(monkeypatch):
    sleeps = []
    monkeypatch.setattr(us_client_module.time, "sleep", lambda s: sleeps.append(s))
    settings = _settings(request_max_retries=2)
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open",
        status=429, headers={"Retry-After": "2.5"},
    )
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open", json={"orders": []}, status=200,
    )
    _client(settings).get_open_orders()
    assert sleeps == [2.5]


@responses.activate
def test_zero_retry_after_cannot_force_immediate_429_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(us_client_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    settings = _settings(
        request_max_retries=2,
        request_backoff_base_seconds=1.0,
        rate_limit_backoff_multiplier=4.0,
    )
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open",
        status=429, headers={"Retry-After": "0"},
    )
    responses.add(
        responses.GET, f"{settings.api_base_url}/v1/orders/open",
        json={"orders": []}, status=200,
    )

    _client(settings).get_open_orders()

    assert sleeps == [4.0]


@responses.activate
def test_create_order_does_not_retry_on_429():
    settings = _settings(request_max_retries=3)
    responses.add(responses.POST, f"{settings.api_base_url}/v1/orders", status=429)
    client = _client(settings)
    with pytest.raises(UsApiError):
        client.create_order(
            market_slug="m1", outcome_side="OUTCOME_SIDE_YES", action="ORDER_ACTION_BUY",
            price=0.49, quantity=100.0,
        )
    assert len(responses.calls) == 1


@responses.activate
def test_cancel_order_does_not_retry_on_429():
    settings = _settings(request_max_retries=3)
    responses.add(responses.POST, f"{settings.api_base_url}/v1/order/order-1/cancel", status=429)
    client = _client(settings)
    with pytest.raises(UsApiError):
        client.cancel_order("order-1", "m1")
    assert len(responses.calls) == 1


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
