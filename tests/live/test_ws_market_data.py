import json

from polymarket_bot import config
from polymarket_bot.live.ws_market_data import (
    LiveMarketWebSocketClient,
    MARKETS_WS_PATH,
    StreamingMarketDataStore,
    StreamingReadClient,
)


def test_store_parses_full_market_data_message():
    store = StreamingMarketDataStore(stale_after_seconds=60)
    store.update_message(
        {
            "marketData": {
                "marketSlug": "m1",
                "bids": [{"px": {"value": "0.40", "currency": "USD"}, "qty": "12"}],
                "offers": [{"px": {"value": "0.45", "currency": "USD"}, "qty": "14"}],
                "stats": {"lastTradePx": {"value": "0.42", "currency": "USD"}},
            }
        }
    )

    assert store.get_market_book("m1") == {
        "bids": [{"price": 0.40, "quantity": 12.0}],
        "asks": [{"price": 0.45, "quantity": 14.0}],
    }
    assert store.get_market_bbo("m1")["best_bid"] == 0.40
    assert store.get_market_bbo("m1")["best_ask"] == 0.45


def test_store_parses_lite_message_and_falls_back_to_rest_when_stale():
    store = StreamingMarketDataStore(stale_after_seconds=0)
    store.update_message(
        {
            "market_data_lite": {
                "market_slug": "m1",
                "best_bid": {"value": "0.40"},
                "best_ask": {"value": "0.45"},
            }
        }
    )

    class Fallback:
        def get_market_bbo(self, slug):
            return {"best_bid": 0.39, "best_ask": 0.46}

        def get_market_book(self, slug):
            return None

    read_client = StreamingReadClient(store, fallback=Fallback())
    assert read_client.get_market_bbo("m1") == {"best_bid": 0.39, "best_ask": 0.46}


def test_websocket_client_sends_subscription_on_open():
    sent = []

    class FakeWs:
        def send(self, payload):
            sent.append(json.loads(payload))

    settings = config.LiveTradingSettings(
        api_base_url="https://api.polymarket.us",
        websocket_responses_debounced=True,
    )
    store = StreamingMarketDataStore()
    client = LiveMarketWebSocketClient(
        settings=settings,
        signed_headers=lambda path: {"X-Test-Path": path},
        store=store,
    )
    client.set_market_slugs(["m1", "m2"])
    client._on_open(FakeWs())

    subscribe = sent[0]["subscribe"]
    assert subscribe["subscription_type"] == 1
    assert subscribe["market_slugs"] == ["m1", "m2"]
    assert subscribe["responses_debounced"] is True


def test_websocket_client_builds_signed_market_url_and_headers():
    captured = {}

    class FakeApp:
        def __init__(self, url, header, **kwargs):
            captured["url"] = url
            captured["header"] = header
            captured["kwargs"] = kwargs

    settings = config.LiveTradingSettings(api_base_url="https://api.polymarket.us")
    client = LiveMarketWebSocketClient(
        settings=settings,
        signed_headers=lambda path: {"X-Test-Path": path},
        store=StreamingMarketDataStore(),
        websocket_app_factory=FakeApp,
    )

    client._build_app()

    assert captured["url"] == "wss://api.polymarket.us/v1/ws/markets"
    assert captured["header"] == [f"X-Test-Path: {MARKETS_WS_PATH}"]
