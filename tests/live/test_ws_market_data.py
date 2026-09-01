import json
from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live import ws_market_data as ws_market_data_module
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


def test_observation_snapshot_uses_its_explicit_bounded_age(monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr(ws_market_data_module.time, "monotonic", lambda: now["value"])
    store = StreamingMarketDataStore(stale_after_seconds=10.0)
    store.update_message({"marketData": {
        "marketSlug": "m1",
        "bids": [{"px": {"value": "0.40"}, "qty": "12"}],
        "offers": [{"px": {"value": "0.45"}, "qty": "14"}],
    }})

    now["value"] = 20.0
    assert store.get_market_book("m1") is None
    assert store.get_market_book_snapshot(
        "m1", max_age_seconds=300.0,
    )["bids"][0]["price"] == 0.40
    now["value"] = 301.0
    assert store.get_market_book_snapshot(
        "m1", max_age_seconds=300.0,
    ) is None


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


def test_streaming_read_client_rest_fallback_is_budget_limited(monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr(ws_market_data_module.time, "monotonic", lambda: now["value"])
    store = StreamingMarketDataStore(stale_after_seconds=0)  # always stale -> forces fallback

    class Fallback:
        def __init__(self):
            self.calls = 0

        def get_market_bbo(self, slug):
            self.calls += 1
            return {"best_bid": 0.39, "best_ask": 0.46}

        def get_market_book(self, slug):
            return None

    fallback = Fallback()
    read_client = StreamingReadClient(
        store, fallback=fallback,
        rest_fallback_budget=5, rest_fallback_window_seconds=60.0,
    )

    for _ in range(5):
        assert read_client.get_market_bbo("m1") is not None
    assert fallback.calls == 5

    # 6th call within the same window is over budget -- must not call the
    # real fallback, and must degrade to None rather than raising.
    assert read_client.get_market_bbo("m1") is None
    assert fallback.calls == 5


def test_streaming_read_client_budget_resets_after_the_window_elapses(monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr(ws_market_data_module.time, "monotonic", lambda: now["value"])
    store = StreamingMarketDataStore(stale_after_seconds=0)

    class Fallback:
        def __init__(self):
            self.calls = 0

        def get_market_bbo(self, slug):
            self.calls += 1
            return {"best_bid": 0.39, "best_ask": 0.46}

        def get_market_book(self, slug):
            return None

    fallback = Fallback()
    read_client = StreamingReadClient(
        store, fallback=fallback,
        rest_fallback_budget=2, rest_fallback_window_seconds=60.0,
    )
    read_client.get_market_bbo("m1")
    read_client.get_market_bbo("m1")
    assert read_client.get_market_bbo("m1") is None
    assert fallback.calls == 2

    now["value"] = 61.0
    assert read_client.get_market_bbo("m1") is not None
    assert fallback.calls == 3


def test_streaming_read_client_priority_bypasses_the_budget(monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr(ws_market_data_module.time, "monotonic", lambda: now["value"])
    store = StreamingMarketDataStore(stale_after_seconds=0)

    class Fallback:
        def __init__(self):
            self.calls = 0

        def get_market_bbo(self, slug):
            self.calls += 1
            return {"best_bid": 0.39, "best_ask": 0.46}

        def get_market_book(self, slug):
            return None

    fallback = Fallback()
    read_client = StreamingReadClient(
        store, fallback=fallback,
        rest_fallback_budget=1, rest_fallback_window_seconds=60.0,
    )
    read_client.get_market_bbo("m1")  # exhausts the budget
    assert read_client.get_market_bbo("m1") is None

    # An emergency-exit caller (force_flatten/reduce_urgent) must still get
    # through even with the budget exhausted.
    assert read_client.get_market_bbo("m1", priority=True) is not None
    assert fallback.calls == 2


def test_lite_updates_cannot_keep_old_l2_book_fresh(monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr(ws_market_data_module.time, "monotonic", lambda: now["value"])
    store = StreamingMarketDataStore(stale_after_seconds=10.0)
    store.update_message({"marketData": {
        "marketSlug": "m1",
        "bids": [{"px": {"value": "0.40"}, "qty": "12"}],
        "offers": [{"px": {"value": "0.45"}, "qty": "14"}],
    }})

    now["value"] = 20.0
    store.update_message({"marketDataLite": {
        "marketSlug": "m1",
        "bestBid": {"value": "0.41"},
        "bestAsk": {"value": "0.44"},
    }})

    assert store.get_market_book("m1") is None
    assert store.get_market_bbo("m1")["best_bid"] == 0.41


def test_market_data_captures_shares_traded_and_open_interest():
    store = StreamingMarketDataStore()
    store.update_message({"marketData": {
        "marketSlug": "m1",
        "bids": [{"px": {"value": "0.40"}, "qty": "12"}],
        "offers": [{"px": {"value": "0.45"}, "qty": "14"}],
        "stats": {"lastTradePx": {"value": "0.42"}, "sharesTraded": "100", "openInterest": "50"},
    }})

    assert store.get_open_interest("m1") == 50.0
    # A single sample -- growth needs at least two to compare.
    assert store.shares_traded_growth("m1") is None


def test_shares_traded_growth_across_two_samples(monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr(ws_market_data_module.time, "monotonic", lambda: now["value"])
    store = StreamingMarketDataStore(activity_window_seconds=300.0)
    store.update_message({"marketData": {
        "marketSlug": "m1", "stats": {"sharesTraded": "100"},
    }})
    now["value"] = 30.0
    store.update_message({"marketData": {
        "marketSlug": "m1", "stats": {"sharesTraded": "150"},
    }})

    assert store.shares_traded_growth("m1") == pytest.approx(50.0)


def test_shares_traded_samples_prune_outside_the_activity_window(monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr(ws_market_data_module.time, "monotonic", lambda: now["value"])
    store = StreamingMarketDataStore(activity_window_seconds=60.0)
    store.update_message({"marketData": {
        "marketSlug": "m1", "stats": {"sharesTraded": "100"},
    }})
    now["value"] = 120.0  # past the 60s window -- the first sample ages out
    store.update_message({"marketData": {
        "marketSlug": "m1", "stats": {"sharesTraded": "150"},
    }})

    # Only one sample survives the window -- nothing to compare against.
    assert store.shares_traded_growth("m1") is None


def test_market_data_lite_also_captures_shares_traded_even_without_bbo():
    # A lite message missing bid/ask is still worth capturing activity data
    # from -- these are independently meaningful, not coupled.
    store = StreamingMarketDataStore()
    store.update_message({"marketDataLite": {
        "marketSlug": "m1", "sharesTraded": "100", "openInterest": "20",
    }})

    assert store.get_open_interest("m1") == 20.0
    assert store.get_market_bbo("m1") is None


def test_book_update_frequency_counts_updates_within_the_window(monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr(ws_market_data_module.time, "monotonic", lambda: now["value"])
    store = StreamingMarketDataStore(activity_window_seconds=100.0)
    for t in (0.0, 10.0, 20.0):
        now["value"] = t
        store.update_message({"marketData": {
            "marketSlug": "m1",
            "bids": [{"px": {"value": "0.40"}, "qty": "12"}],
            "offers": [{"px": {"value": "0.45"}, "qty": "14"}],
        }})

    now["value"] = 20.0
    assert store.book_update_frequency("m1") == pytest.approx(3 / 100.0)


def test_book_update_frequency_prunes_outside_the_window(monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr(ws_market_data_module.time, "monotonic", lambda: now["value"])
    store = StreamingMarketDataStore(activity_window_seconds=50.0)
    store.update_message({"marketData": {
        "marketSlug": "m1",
        "bids": [{"px": {"value": "0.40"}, "qty": "12"}],
        "offers": [{"px": {"value": "0.45"}, "qty": "14"}],
    }})

    now["value"] = 100.0  # past the 50s window
    assert store.book_update_frequency("m1") == 0.0


def test_trade_message_is_recorded_and_counted():
    store = StreamingMarketDataStore()
    assert store.recent_trade_count("m1") == 0

    store.update_message({"trade": {
        "marketSlug": "m1",
        "price": {"value": "0.42"},
        "quantity": {"value": "5"},
        "tradeTime": "2026-07-21T00:00:00Z",
    }})

    assert store.recent_trade_count("m1") == 1
    assert store.recent_trade_quantity("m1") == pytest.approx(5.0)


def test_trades_prune_outside_the_activity_window(monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr(ws_market_data_module.time, "monotonic", lambda: now["value"])
    store = StreamingMarketDataStore(activity_window_seconds=60.0)
    store.update_message({"trade": {
        "marketSlug": "m1", "price": {"value": "0.42"}, "quantity": {"value": "5"},
    }})

    now["value"] = 120.0
    assert store.recent_trade_count("m1") == 0


def test_trade_message_missing_price_or_quantity_is_ignored():
    store = StreamingMarketDataStore()

    store.update_message({"trade": {"marketSlug": "m1", "price": {"value": "0.42"}}})

    assert store.recent_trade_count("m1") == 0


def test_websocket_client_sends_subscription_on_open():
    sent = []

    class FakeWs:
        def __init__(self):
            self.closed = False

        def send(self, payload):
            sent.append(json.loads(payload))

        def close(self):
            self.closed = True

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


def test_websocket_client_also_subscribes_to_trades():
    sent = []

    class FakeWs:
        def send(self, payload):
            sent.append(json.loads(payload))

    settings = config.LiveTradingSettings(api_base_url="https://api.polymarket.us")
    store = StreamingMarketDataStore()
    client = LiveMarketWebSocketClient(
        settings=settings,
        signed_headers=lambda path: {"X-Test-Path": path},
        store=store,
    )
    client.set_market_slugs(["m1", "m2"])
    client._on_open(FakeWs())

    trade_subscribes = [m["subscribe"] for m in sent if m["subscribe"]["subscription_type"] == 3]
    assert len(trade_subscribes) == 1
    assert trade_subscribes[0]["market_slugs"] == ["m1", "m2"]


def test_market_websocket_heartbeat_records_feed_health_and_is_echoed():
    tracker = Mock()
    store = StreamingMarketDataStore(observation_tracker=tracker)
    sent = []

    class FakeWs:
        def send(self, payload):
            sent.append(json.loads(payload))

    client = LiveMarketWebSocketClient(
        settings=config.LiveTradingSettings(api_base_url="https://api.polymarket.us"),
        signed_headers=lambda _path: {},
        store=store,
    )

    client._on_message(FakeWs(), json.dumps({"heartbeat": {}}))

    tracker.record_feed_activity.assert_called_once_with()
    assert sent == [{"heartbeat": {}}]


def test_market_and_trade_subscriptions_always_use_distinct_request_ids():
    client, sent = _client_with_open_ws()

    client.set_market_slugs(["m1"])

    request_ids = [message["subscribe"]["request_id"] for message in sent]
    assert len(request_ids) == 2
    assert len(set(request_ids)) == 2


def test_subscription_rejection_clears_local_state_and_reconnects():
    client, sent = _client_with_open_ws()
    client.set_market_slugs(["m1"])
    rejected_id = sent[1]["subscribe"]["request_id"]

    client._on_message(client._ws, json.dumps({
        "request_id": rejected_id,
        "error": "request id already exists",
    }))

    assert client._subscribed_slugs == set()
    assert client._ws.closed is True


def test_trade_subscription_skipped_when_activity_tracking_disabled():
    sent = []

    class FakeWs:
        def send(self, payload):
            sent.append(json.loads(payload))

    settings = config.LiveTradingSettings(
        api_base_url="https://api.polymarket.us", activity_tracking_enabled=False,
    )
    store = StreamingMarketDataStore()
    client = LiveMarketWebSocketClient(
        settings=settings,
        signed_headers=lambda path: {"X-Test-Path": path},
        store=store,
    )
    client.set_market_slugs(["m1", "m2"])
    client._on_open(FakeWs())

    assert all(m["subscribe"]["subscription_type"] != 3 for m in sent)
    assert len(sent) == 1  # only the market-data subscription


def _client_with_open_ws():
    sent = []

    class FakeWs:
        def __init__(self):
            self.closed = False

        def send(self, payload):
            sent.append(json.loads(payload))

        def close(self):
            self.closed = True

    settings = config.LiveTradingSettings(api_base_url="https://api.polymarket.us")
    store = StreamingMarketDataStore()
    client = LiveMarketWebSocketClient(
        settings=settings,
        signed_headers=lambda path: {"X-Test-Path": path},
        store=store,
    )
    client._ws = FakeWs()
    return client, sent


def _market_data_msgs(sent):
    return [m["subscribe"] for m in sent if m["subscribe"]["subscription_type"] == 1]


def test_set_market_slugs_second_call_with_overlapping_slugs_sends_only_new_ones():
    client, sent = _client_with_open_ws()

    client.set_market_slugs(["m1", "m2"])
    client.set_market_slugs(["m1", "m2", "m3"])

    # 2 logical subscribe calls x 2 subscription types (market data + trade).
    assert len(sent) == 4
    market_data = _market_data_msgs(sent)
    assert market_data[0]["market_slugs"] == ["m1", "m2"]
    assert market_data[1]["market_slugs"] == ["m3"]


def test_set_market_slugs_with_no_new_slugs_sends_nothing():
    client, sent = _client_with_open_ws()

    client.set_market_slugs(["m1", "m2"])
    client.set_market_slugs(["m1", "m2"])

    assert len(sent) == 2  # one logical subscribe call x 2 subscription types


def test_removing_market_reconnects_instead_of_leaking_subscription():
    client, sent = _client_with_open_ws()
    client.set_market_slugs(["m1", "m2"])

    client.set_market_slugs(["m2"])

    assert client._ws.closed is True
    assert len(sent) == 2  # one logical subscribe call x 2 subscription types


def test_feed_watchdog_force_reconnect_closes_current_socket():
    client, _sent = _client_with_open_ws()

    client.force_reconnect()

    assert client._ws.closed is True


def test_reconnect_resends_full_subscription_list_after_previously_subscribed():
    client, sent = _client_with_open_ws()
    client.set_market_slugs(["m1", "m2"])
    assert len(sent) == 2  # one logical subscribe call x 2 subscription types

    # Simulate a reconnect -- the server has no memory of prior
    # subscriptions, so the full list must be resent, not diffed.
    client._on_open(client._ws)

    assert len(sent) == 4
    assert _market_data_msgs(sent)[1]["market_slugs"] == ["m1", "m2"]


def test_failed_send_does_not_mark_slugs_as_subscribed():
    class FailingWs:
        def send(self, payload):
            raise RuntimeError("send failed")

    settings = config.LiveTradingSettings(api_base_url="https://api.polymarket.us")
    client = LiveMarketWebSocketClient(
        settings=settings,
        signed_headers=lambda path: {"X-Test-Path": path},
        store=StreamingMarketDataStore(),
    )
    client._ws = FailingWs()

    try:
        client.set_market_slugs(["m1"])
    except RuntimeError:
        pass

    assert client._subscribed_slugs == set()


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
