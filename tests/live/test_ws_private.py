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


def test_store_reconciles_and_applies_order_lifecycle_deltas():
    store = PrivateStateStore()
    store.seed_open_orders([{"id": "o1", "marketSlug": "m1", "state": "ORDER_STATE_NEW"}])
    assert [o["id"] for o in store.open_orders_snapshot()] == ["o1"]

    store.handle_message({"orderSubscriptionUpdate": {"execution": {"id": "e1", "order": {
        "id": "o2", "marketSlug": "m2", "state": "ORDER_STATE_NEW",
    }}}})
    assert {o["id"] for o in store.open_orders_snapshot()} == {"o1", "o2"}

    store.handle_message({"orderSubscriptionUpdate": {"execution": {"id": "e2", "order": {
        "id": "o1", "marketSlug": "m1", "state": "ORDER_STATE_FILLED",
    }}}})
    assert [o["id"] for o in store.open_orders_snapshot()] == ["o2"]


def test_store_records_only_explicit_terminal_zero_fill_orders():
    store = PrivateStateStore()
    for order in (
        {"id": "zero", "state": "ORDER_STATE_CANCELED", "cumQuantity": "0"},
        {"id": "partial", "state": "ORDER_STATE_CANCELED", "cumQuantity": "0.5"},
        {"id": "unknown", "state": "ORDER_STATE_CANCELED"},
        {"id": "filled", "state": "ORDER_STATE_FILLED", "cumQuantity": "0"},
    ):
        store.handle_message({"orderSubscriptionUpdate": {"execution": {"order": order}}})

    assert store.terminal_no_fill_order_ids() == {"zero"}


def test_store_position_reconciliation_and_flat_delta_removal():
    store = PrivateStateStore()
    store.seed_positions({"m1": {"marketSlug": "m1", "netPositionDecimal": "2"}})
    assert "m1" in store.positions_snapshot()

    store.handle_message({"positionSubscription": {"afterPosition": {
        "marketSlug": "m1", "netPositionDecimal": "0",
    }}})
    assert store.positions_snapshot() == {}


def test_malformed_position_delta_does_not_replace_last_good_state():
    store = PrivateStateStore()
    store.seed_positions({"m1": {"marketSlug": "m1", "netPositionDecimal": "2"}})

    store.handle_message({"positionSubscription": {"afterPosition": {
        "marketSlug": "m1", "netPositionDecimal": "not-a-number",
    }}})

    assert store.positions_snapshot()["m1"]["netPositionDecimal"] == "2"


def test_racing_rest_order_snapshot_cannot_overwrite_newer_delta():
    store = PrivateStateStore()
    store.seed_open_orders([{"id": "old", "marketSlug": "m1"}])
    version_before_request = store.order_version()
    store.handle_message({"orderSubscriptionUpdate": {"execution": {"id": "e1", "order": {
        "id": "new", "marketSlug": "m2", "state": "ORDER_STATE_NEW",
    }}}})

    installed = store.reconcile_open_orders(
        [{"id": "old", "marketSlug": "m1"}], version_before_request,
    )

    assert installed is False
    assert {order["id"] for order in store.open_orders_snapshot()} == {"old", "new"}


def test_local_order_removal_updates_authoritative_snapshot():
    store = PrivateStateStore()
    store.seed_open_orders([
        {"id": "o1", "marketSlug": "m1"},
        {"id": "o2", "marketSlug": "m2"},
    ])

    store.remove_orders(["o1"])

    assert [order["id"] for order in store.open_orders_snapshot()] == ["o2"]


def test_upsert_local_orders_bumps_version():
    # Previously upsert_local_orders never bumped _order_version at all --
    # a real, since-fixed bug that broke the optimistic-concurrency check
    # reconcile_open_orders() relies on (see the racing-snapshot test below
    # for the concrete failure this caused).
    store = PrivateStateStore()
    store.seed_open_orders([])
    version_before = store.order_version()

    store.upsert_local_orders([{"id": "new1", "marketSlug": "m1"}])

    assert store.order_version() != version_before


def test_upsert_local_orders_with_no_valid_ids_does_not_bump_version():
    store = PrivateStateStore()
    store.seed_open_orders([])
    version_before = store.order_version()

    store.upsert_local_orders([{"marketSlug": "m1"}])  # no "id"/"orderId" -- nothing upserted

    assert store.order_version() == version_before


def test_racing_rest_snapshot_cannot_drop_a_locally_upserted_order():
    # The exact scenario from the bug report: a market gets quoted for the
    # first time (nothing to cancel), upsert_local_orders() records the new
    # order, and a REST reconciliation that started BEFORE the upsert
    # completes AFTER it. Before the fix, the stale REST snapshot (which
    # doesn't yet reflect the new order -- a real, documented eventual-
    # consistency lag on this exchange) would silently overwrite it because
    # the version never moved. After the fix, reconcile_open_orders() must
    # detect the version changed and refuse to install the stale snapshot.
    store = PrivateStateStore()
    store.seed_open_orders([])  # nothing resting yet for this market
    version_before_rest_fetch = store.order_version()

    store.upsert_local_orders([{"id": "new1", "marketSlug": "m1", "state": "ORDER_STATE_NEW"}])

    installed = store.reconcile_open_orders([], version_before_rest_fetch)  # stale: doesn't see "new1" yet

    assert installed is False
    assert [o["id"] for o in store.open_orders_snapshot()] == ["new1"]


def test_replace_market_orders_bumps_version_even_when_removing_without_replacement():
    # A cancel-with-no-replacement (orders=[]) still mutates _open_orders
    # (removes the market's old entries) and must still bump the version --
    # gating the bump on `if orders:` (the old, since-fixed behavior) let
    # that removal go unrecorded.
    store = PrivateStateStore()
    store.seed_open_orders([{"id": "o1", "marketSlug": "m1"}])
    version_before = store.order_version()

    store.replace_market_orders("m1", [])

    assert store.order_version() != version_before
    assert store.open_orders_snapshot() == []


def test_mark_connected_sets_reconnect_pending_on_genuine_transition():
    store = PrivateStateStore()
    assert store.reconnect_pending() is False

    store.mark_connected()

    assert store.reconnect_pending() is True


def test_clear_reconnect_pending_resets_flag():
    store = PrivateStateStore()
    store.mark_connected()

    store.clear_reconnect_pending()

    assert store.reconnect_pending() is False


def test_mark_connected_while_already_connected_does_not_extend_pending_state():
    # Repeated heartbeats/on_open calls while never actually disconnected
    # must not perpetually re-arm the flag once it's been cleared -- only a
    # genuine False -> True transition should.
    store = PrivateStateStore()
    store.mark_connected()
    store.clear_reconnect_pending()

    store.mark_connected()

    assert store.reconnect_pending() is False


def test_disconnect_then_reconnect_sets_pending_again():
    store = PrivateStateStore()
    store.mark_connected()
    store.clear_reconnect_pending()

    store.mark_disconnected()
    store.mark_connected()

    assert store.reconnect_pending() is True


def test_unrecognized_order_state_is_logged_once_and_kept_as_open(caplog):
    # _TERMINAL_ORDER_STATES/_KNOWN_NON_TERMINAL_ORDER_STATES have never
    # been cross-checked against a real order's full lifecycle -- a real
    # terminal state this module doesn't happen to name would otherwise
    # silently leave a filled/cancelled order stuck open forever. An
    # unrecognized value must fail safe (kept as open) AND be logged so a
    # live session's bot.log becomes the way to actually verify this.
    store = PrivateStateStore()
    store.seed_open_orders([])
    with caplog.at_level("WARNING"):
        store.handle_message({"orderSubscriptionUpdate": {"execution": {"id": "e1", "order": {
            "id": "o1", "marketSlug": "m1", "state": "ORDER_STATE_MYSTERY",
        }}}})

    assert [o["id"] for o in store.open_orders_snapshot()] == ["o1"]
    assert any("ORDER_STATE_MYSTERY" in record.message for record in caplog.records)


def test_unrecognized_order_state_is_only_logged_once_per_distinct_value(caplog):
    store = PrivateStateStore()
    with caplog.at_level("WARNING"):
        for i in range(3):
            store.handle_message({"orderSubscriptionUpdate": {"execution": {"id": f"e{i}", "order": {
                "id": f"o{i}", "marketSlug": "m1", "state": "ORDER_STATE_MYSTERY",
            }}}})

    matching = [r for r in caplog.records if "ORDER_STATE_MYSTERY" in r.message]
    assert len(matching) == 1


def test_known_terminal_and_non_terminal_states_are_never_logged_as_unrecognized(caplog):
    store = PrivateStateStore()
    with caplog.at_level("WARNING"):
        store.handle_message({"orderSubscriptionUpdate": {"execution": {"id": "e1", "order": {
            "id": "o1", "marketSlug": "m1", "state": "ORDER_STATE_NEW",
        }}}})
        store.handle_message({"orderSubscriptionUpdate": {"execution": {"id": "e2", "order": {
            "id": "o1", "marketSlug": "m1", "state": "ORDER_STATE_FILLED",
        }}}})

    assert caplog.records == []


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
