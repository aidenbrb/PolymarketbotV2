import pytest
import responses
from urllib.parse import parse_qs, urlparse

from polymarket_bot import config
from polymarket_bot.polymarket_client import (
    PolymarketClient,
    PolymarketClientError,
    PolymarketClientNotFound,
)


def _fast_settings(**overrides) -> config.APISettings:
    defaults = dict(
        gateway_base_url="https://gateway-api.test",
        timeout_seconds=1,
        max_retries=2,
        backoff_base_seconds=0.001,
        page_limit=2,
    )
    defaults.update(overrides)
    return config.APISettings(**defaults)


@responses.activate
def test_get_markets_single_page():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/markets",
        json={"markets": [{"id": "1", "slug": "m1", "question": "Q1"}]},
        status=200,
    )
    client = PolymarketClient(settings=settings)
    markets = client.get_markets()
    assert len(markets) == 1
    assert markets[0]["slug"] == "m1"


@responses.activate
def test_get_markets_requests_newest_listings_first():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/markets",
        json={"markets": []},
        status=200,
    )

    PolymarketClient(settings=settings).get_markets()

    query = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert query["active"] == ["true"]
    assert query["closed"] == ["false"]
    assert query["orderBy"] == ["created_at"]
    assert query["orderDirection"] == ["desc"]


@responses.activate
def test_get_markets_paginates_until_short_page():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/markets",
        json={"markets": [{"id": "1"}, {"id": "2"}]},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/markets",
        json={"markets": [{"id": "3"}]},
        status=200,
    )
    client = PolymarketClient(settings=settings)
    markets = client.get_markets(max_pages=5)
    assert [m["id"] for m in markets] == ["1", "2", "3"]


@responses.activate
def test_request_fails_safely_after_retries():
    settings = _fast_settings(max_retries=2)
    responses.add(responses.GET, f"{settings.gateway_base_url}/v1/markets", status=500)
    responses.add(responses.GET, f"{settings.gateway_base_url}/v1/markets", status=500)
    client = PolymarketClient(settings=settings)
    # Pagination catches the error internally and returns whatever it has (empty).
    markets = client.get_markets()
    assert markets == []


@responses.activate
def test_low_level_get_raises_client_error_after_retries():
    settings = _fast_settings(max_retries=2)
    responses.add(responses.GET, f"{settings.gateway_base_url}/v1/markets/m1/bbo", status=500)
    responses.add(responses.GET, f"{settings.gateway_base_url}/v1/markets/m1/bbo", status=500)
    client = PolymarketClient(settings=settings)
    import pytest
    with pytest.raises(PolymarketClientError):
        client._get(f"{settings.gateway_base_url}/v1/markets/m1/bbo")


@responses.activate
def test_get_market_bbo_missing_returns_none():
    settings = _fast_settings(max_retries=1)
    responses.add(responses.GET, f"{settings.gateway_base_url}/v1/markets/m1/bbo", status=500)
    client = PolymarketClient(settings=settings)
    bbo = client.get_market_bbo("m1")
    assert bbo is None


@responses.activate
def test_get_market_bbo_parses_quote_values():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/markets/m1/bbo",
        json={
            "marketData": {
                "marketSlug": "m1",
                "bestBid": {"value": "0.40", "currency": "USD"},
                "bestAsk": {"value": "0.45", "currency": "USD"},
                "currentPx": {"value": "0.42", "currency": "USD"},
                "lastTradePx": {"value": "0.41", "currency": "USD"},
            }
        },
        status=200,
    )
    client = PolymarketClient(settings=settings)
    bbo = client.get_market_bbo("m1")
    assert bbo is not None
    assert bbo["best_bid"] == 0.40
    assert bbo["best_ask"] == 0.45
    assert bbo["current_price"] == 0.42
    assert bbo["last_trade_price"] == 0.41


@responses.activate
def test_get_market_bbo_missing_marketdata_returns_none():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/markets/m1/bbo",
        json={},
        status=200,
    )
    client = PolymarketClient(settings=settings)
    assert client.get_market_bbo("m1") is None


@responses.activate
def test_get_market_book_parses_depth_levels():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/markets/m1/book",
        json={
            "marketData": {
                "bids": [
                    {"px": {"value": "0.40", "currency": "USD"}, "qty": "12.5"},
                ],
                "offers": [
                    {"px": {"value": "0.45", "currency": "USD"}, "qty": "14"},
                ],
            }
        },
        status=200,
    )
    client = PolymarketClient(settings=settings)
    book = client.get_market_book("m1")
    assert book == {
        "bids": [{"price": 0.40, "quantity": 12.5}],
        "asks": [{"price": 0.45, "quantity": 14.0}],
    }


@responses.activate
def test_get_market_book_missing_returns_none():
    settings = _fast_settings(max_retries=1)
    responses.add(responses.GET, f"{settings.gateway_base_url}/v1/markets/m1/book", status=500)
    client = PolymarketClient(settings=settings)
    assert client.get_market_book("m1") is None


@responses.activate
def test_low_level_get_raises_not_found_on_404_without_retrying():
    # A 404 is a clean, final answer ("this doesn't exist yet"), never a
    # transient failure -- costs exactly one HTTP request, unlike a 5xx/
    # network error which retries up to max_retries.
    settings = _fast_settings(max_retries=3)
    responses.add(responses.GET, f"{settings.gateway_base_url}/v1/markets/m1/settlement", status=404)
    client = PolymarketClient(settings=settings)

    with pytest.raises(PolymarketClientNotFound):
        client._get(f"{settings.gateway_base_url}/v1/markets/m1/settlement")

    assert len(responses.calls) == 1


@responses.activate
def test_get_market_settlement_returns_value_on_success():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/markets/m1/settlement",
        json={"slug": "m1", "settlement": "1.0"},
        status=200,
    )
    client = PolymarketClient(settings=settings)

    result = client.get_market_settlement("m1")

    assert result == {"slug": "m1", "settlement": 1.0}


@responses.activate
def test_get_market_settlement_accepts_fractional_value():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/markets/m1/settlement",
        json={"slug": "m1", "settlement": 0.37},
        status=200,
    )
    client = PolymarketClient(settings=settings)

    result = client.get_market_settlement("m1")

    assert result == {"slug": "m1", "settlement": 0.37}


@responses.activate
def test_get_market_settlement_returns_none_on_404():
    settings = _fast_settings(max_retries=3)
    responses.add(responses.GET, f"{settings.gateway_base_url}/v1/markets/m1/settlement", status=404)
    client = PolymarketClient(settings=settings)

    assert client.get_market_settlement("m1") is None
    assert len(responses.calls) == 1


@responses.activate
def test_get_market_settlement_raises_on_slug_mismatch():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/markets/m1/settlement",
        json={"slug": "m2", "settlement": 1.0},
        status=200,
    )
    client = PolymarketClient(settings=settings)

    with pytest.raises(PolymarketClientError, match="mismatch"):
        client.get_market_settlement("m1")


@responses.activate
def test_get_market_settlement_raises_on_out_of_range_value():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/markets/m1/settlement",
        json={"slug": "m1", "settlement": 1.5},
        status=200,
    )
    client = PolymarketClient(settings=settings)

    with pytest.raises(PolymarketClientError, match="out of range"):
        client.get_market_settlement("m1")


@responses.activate
def test_get_market_settlement_raises_on_non_numeric_value():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/markets/m1/settlement",
        json={"slug": "m1", "settlement": "not-a-number"},
        status=200,
    )
    client = PolymarketClient(settings=settings)

    with pytest.raises(PolymarketClientError):
        client.get_market_settlement("m1")


@responses.activate
def test_get_market_settlement_raises_after_exhausted_retries():
    settings = _fast_settings(max_retries=2)
    responses.add(responses.GET, f"{settings.gateway_base_url}/v1/markets/m1/settlement", status=500)
    responses.add(responses.GET, f"{settings.gateway_base_url}/v1/markets/m1/settlement", status=500)
    client = PolymarketClient(settings=settings)

    with pytest.raises(PolymarketClientError):
        client.get_market_settlement("m1")


@responses.activate
def test_get_market_metadata_returns_market_dict():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/market/slug/m1",
        json={"market": {"slug": "m1", "closed": True, "status": "MARKET_STATUS_RESOLVED", "active": True}},
        status=200,
    )
    client = PolymarketClient(settings=settings)

    metadata = client.get_market_metadata("m1")

    assert metadata == {"slug": "m1", "closed": True, "status": "MARKET_STATUS_RESOLVED", "active": True}


@responses.activate
def test_get_market_metadata_returns_none_on_404():
    settings = _fast_settings(max_retries=3)
    responses.add(responses.GET, f"{settings.gateway_base_url}/v1/market/slug/m1", status=404)
    client = PolymarketClient(settings=settings)

    assert client.get_market_metadata("m1") is None
    assert len(responses.calls) == 1


@responses.activate
def test_get_market_metadata_raises_on_missing_market_key():
    settings = _fast_settings()
    responses.add(
        responses.GET,
        f"{settings.gateway_base_url}/v1/market/slug/m1",
        json={"unexpected": "shape"},
        status=200,
    )
    client = PolymarketClient(settings=settings)

    with pytest.raises(PolymarketClientError):
        client.get_market_metadata("m1")
