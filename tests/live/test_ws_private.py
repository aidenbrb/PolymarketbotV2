import json

from polymarket_bot import config
from polymarket_bot.live.ws_private import (
    PRIVATE_WS_PATH,
    PrivateStateStore,
    PrivateWebSocketClient,
)


def test_store_parses_order_execution_update():
    store = PrivateStateStore()
    store.handle_message({
        "requestId": "r1",
        "subscriptionType": "SUBSCRIPTION_TYPE_ORDER",
        "orderSubscriptionUpdate": {
            "execution": {
                "id": "exec-1",
                "lastShares": "10.0",
                "lastPx": {"value": "0.45", "currency": "USD"},
                "type": "EXECUTION_TYPE_FILL",
                "tradeId": "trade-1",
            }
        },
    })

    executions = store.recent_executions()
    assert len(executions) == 1
    assert executions[0]["id"] == "exec-1"
    assert executions[0]["type"] == "EXECUTION_TYPE_FILL"


def test_store_caps_recent_executions_at_max():
    store = PrivateStateStore(max_recent_executions=3)
    for i in range(5):
        store.handle_message({"orderSubscriptionUpdate": {"execution": {"id": f"exec-{i}"}}})

    executions = store.recent_executions()
    assert [e["id"] for e in executions] == ["exec-2", "exec-3", "exec-4"]


def test_store_parses_position_update_keyed_by_market_slug():
    store = PrivateStateStore()
    store.handle_message({
        "positionSubscription": {
            "afterPosition": {
                "marketSlug": "m1",
                "netPositionDecimal": "17.5",
                "cost": {"value": "9.0", "currency": "USD"},
            }
        }
    })

    position = store.get_position("m1")
    assert position["netPositionDecimal"] == "17.5"
    assert store.get_position("unknown-market") is None


def test_store_parses_balance_update():
    store = PrivateStateStore()
    assert store.last_balance_update() is None

    store.handle_message({
        "accountBalancesUpdate": {"balanceChange": {"description": "order fill"}},
    })

    assert store.last_balance_update()["balanceChange"]["description"] == "order fill"


def test_store_ignores_malformed_messages_without_raising():
    store = PrivateStateStore()
    store.handle_message({"orderSubscriptionUpdate": "not-a-dict"})
    store.handle_message({"positionSubscription": {"afterPosition": "not-a-dict"}})
    store.handle_message({"positionSubscription": {"afterPosition": {"no": "slug"}}})
    store.handle_message({})

    assert store.recent_executions() == []
    assert store.get_position("m1") is None
    assert store.last_balance_update() is None


def test_websocket_client_subscribes_to_order_position_and_balance_on_open():
    sent = []

    class FakeWs:
        def send(self, payload):
            sent.append(json.loads(payload))

    settings = config.LiveTradingSettings(api_base_url="https://api.polymarket.us")
    client = PrivateWebSocketClient(
        settings=settings,
        signed_headers=lambda path: {"X-Test-Path": path},
        store=PrivateStateStore(),
    )
    client._on_open(FakeWs())

    subscription_types = {msg["subscribe"]["subscriptionType"] for msg in sent}
    assert subscription_types == {
        "SUBSCRIPTION_TYPE_ORDER",
        "SUBSCRIPTION_TYPE_POSITION",
        "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE",
    }


def test_websocket_client_builds_signed_private_url_and_headers():
    captured = {}

    class FakeApp:
        def __init__(self, url, header, **kwargs):
            captured["url"] = url
            captured["header"] = header
            captured["kwargs"] = kwargs

    settings = config.LiveTradingSettings(api_base_url="https://api.polymarket.us")
    client = PrivateWebSocketClient(
        settings=settings,
        signed_headers=lambda path: {"X-Test-Path": path},
        store=PrivateStateStore(),
        websocket_app_factory=FakeApp,
    )

    client._build_app()

    assert captured["url"] == "wss://api.polymarket.us/v1/ws/private"
    assert captured["header"] == [f"X-Test-Path: {PRIVATE_WS_PATH}"]


def test_on_message_feeds_store_and_handles_heartbeat():
    store = PrivateStateStore()
    client = PrivateWebSocketClient(
        settings=config.LiveTradingSettings(),
        signed_headers=lambda path: {},
        store=store,
    )

    class FakeWs:
        def __init__(self):
            self.sent = []

        def send(self, payload):
            self.sent.append(payload)

    ws = FakeWs()
    client._on_message(ws, json.dumps({"heartbeat": {}}))
    assert json.loads(ws.sent[0]) == {"heartbeat": {}}

    client._on_message(ws, json.dumps({"orderSubscriptionUpdate": {"execution": {"id": "exec-1"}}}))
    assert store.recent_executions()[0]["id"] == "exec-1"

    client._on_message(ws, "not json")  # must not raise
