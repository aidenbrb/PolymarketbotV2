import responses

from polymarket_bot import config
from polymarket_bot.polymarket_client import PolymarketClient, PolymarketClientError


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
