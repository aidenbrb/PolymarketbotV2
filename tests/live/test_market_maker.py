from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live import ledger as ledger_module
from polymarket_bot.live.market_maker import MarketMaker
from polymarket_bot.live.models import PostedLeg


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, "LEDGER_FILE", tmp_path / "orders.json")


def _settings(**overrides):
    defaults = dict(order_shares_min=15.0, order_shares_max=20.0, min_edge_cents=0.5)
    defaults.update(overrides)
    return config.LiveTradingSettings(**defaults)


def _maker(client=None, read_client=None, settings=None, market_slug="m1", tick_size=0.01, reduce_only_reason=None):
    client = client or Mock()
    client.get_open_orders.return_value = []
    client.create_order.side_effect = [
        {"id": "bid-1"},
        {"id": "ask-1"},
    ]
    client.get_position.return_value = None  # flat by default
    read_client = read_client or Mock()
    # 4c-wide book with a 1c tick: improving both sides by 1 tick leaves a
    # real 2c captured spread -- well above the 0.5c default minimum.
    read_client.get_market_bbo.return_value = {
        "best_bid": 0.48, "best_ask": 0.52, "current_price": 0.5, "last_trade_price": 0.5,
    }
    # A real, sufficiently deep L2 book matching the same bid/ask. Default
    # settings require this (LIVE_REQUIRE_L2_DEPTH defaults True) -- tests
    # that don't care about depth specifically still need a valid book.
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.48, "quantity": 15.0}, {"price": 0.47, "quantity": 15.0}],
        "asks": [{"price": 0.52, "quantity": 15.0}, {"price": 0.53, "quantity": 15.0}],
    }
    return MarketMaker(
        client=client, market_slug=market_slug, tick_size=tick_size,
        settings=settings or _settings(), read_client=read_client,
        reduce_only_reason=reduce_only_reason,
    ), client, read_client


def test_refresh_quotes_cancels_before_posting(isolated_ledger):
    maker, client, _ = _maker()
    client.get_open_orders.return_value = [{"id": "old-order", "marketSlug": "m1"}]

    call_order = []
    client.cancel_order.side_effect = lambda oid, slug: call_order.append(("cancel", oid))
    client.create_order.side_effect = lambda **kwargs: call_order.append(("post", None)) or {
        "id": "new-order"
    }

    maker.refresh_quotes()

    assert call_order[0] == ("cancel", "old-order")
    assert call_order[1][0] == "post"


def test_refresh_quotes_improves_the_real_book_by_one_tick(isolated_ledger):
    maker, client, _ = _maker()
    maker.refresh_quotes()

    assert client.create_order.call_count == 2
    bid_kwargs = client.create_order.call_args_list[0].kwargs
    ask_kwargs = client.create_order.call_args_list[1].kwargs
    # best_bid=0.48, best_ask=0.52, tick=0.01 -> improved to 0.49 / 0.51.
    assert bid_kwargs["price"] == pytest.approx(0.49)
    assert ask_kwargs["price"] == pytest.approx(0.51)
    assert bid_kwargs["outcome_side"] == "OUTCOME_SIDE_YES"
    assert bid_kwargs["action"] == "ORDER_ACTION_BUY"
    assert ask_kwargs["action"] == "ORDER_ACTION_SELL"
    assert bid_kwargs["quantity"] == pytest.approx(17.0)
    assert ask_kwargs["quantity"] == pytest.approx(17.0)


def test_refresh_quotes_records_to_ledger(isolated_ledger):
    maker, client, _ = _maker()
    cycle = maker.refresh_quotes()

    from polymarket_bot.live.ledger import get_all_cycles
    cycles = get_all_cycles()
    assert len(cycles) == 1
    assert cycles[0]["cycle_id"] == cycle.cycle_id
    assert cycles[0]["market_id"] == "m1"


def test_refresh_quotes_raises_when_no_book_available_and_depth_not_required(isolated_ledger):
    # With LIVE_REQUIRE_L2_DEPTH off (an explicit opt-out), a genuinely
    # unusable price (no book, no BBO either) still has to fail loudly.
    settings = _settings(require_l2_depth=False)
    maker, client, read_client = _maker(settings=settings)
    read_client.get_market_book.return_value = None
    read_client.get_market_bbo.return_value = None
    with pytest.raises(RuntimeError):
        maker.refresh_quotes()


def test_refresh_quotes_skips_and_cancels_when_l2_book_unavailable_by_default(isolated_ledger):
    # Default behavior (LIVE_REQUIRE_L2_DEPTH=True): no L2 book at all means
    # no quoting, full stop -- never silently fall back to BBO-only pricing.
    maker, client, read_client = _maker()
    read_client.get_market_book.return_value = None
    client.get_open_orders.return_value = [{"id": "stale-order", "marketSlug": "m1"}]

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.cancel_order.assert_called_once_with("stale-order", "m1")
    client.create_order.assert_not_called()


def test_refresh_quotes_skips_when_no_edge_but_still_cancels_stale_orders(isolated_ledger):
    maker, client, read_client = _maker()
    # Book only 1 tick wide -- improving both sides leaves zero/negative edge.
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.50, "quantity": 15.0}, {"price": 0.49, "quantity": 15.0}],
        "asks": [{"price": 0.51, "quantity": 15.0}, {"price": 0.52, "quantity": 15.0}],
    }
    client.get_open_orders.return_value = [{"id": "stale-order", "marketSlug": "m1"}]

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.cancel_order.assert_called_once_with("stale-order", "m1")
    client.create_order.assert_not_called()


def test_cancel_existing_orders_only_targets_this_market(isolated_ledger):
    maker, client, _ = _maker()
    client.get_open_orders.return_value = [
        {"id": "o1", "marketSlug": "m1"},
        {"id": "o2", "marketSlug": "other-market"},
    ]
    maker.refresh_quotes()

    assert client.cancel_order.call_count == 1
    client.cancel_order.assert_called_once_with("o1", "m1")


def test_post_leg_failure_is_captured_not_raised(isolated_ledger):
    from polymarket_bot.live.us_client import UsApiError

    maker, client, _ = _maker()
    client.create_order.side_effect = [{"id": "bid-1"}, UsApiError("insufficient balance")]

    cycle = maker.refresh_quotes()
    assert cycle.bid.order_id == "bid-1"
    assert cycle.ask.order_id is None
    assert "insufficient balance" in cycle.ask.error


def _position(net_position, cost_value, cash_value):
    return {
        "netPositionDecimal": str(net_position),
        "cost": {"value": str(cost_value), "currency": "USD"},
        "cashValue": {"value": str(cash_value), "currency": "USD"},
    }


def test_refresh_quotes_floors_the_sell_leg_at_cost_basis_when_long(isolated_ledger):
    # book-implied ask is 0.51, but the bot is long at an avg cost of 0.55 --
    # selling at 0.51 would voluntarily realize a loss, so the ask must be
    # floored up to cost basis instead.
    maker, client, _ = _maker()
    client.create_order.side_effect = [{"id": "bid-1"}, {"id": "ask-1"}]
    # cash_value kept well under the default $40 cap so only the cost-basis
    # floor is exercised here, not the inventory cap too.
    client.get_position.return_value = _position(net_position=100, cost_value=55.0, cash_value=20.0)

    maker.refresh_quotes()

    ask_kwargs = client.create_order.call_args_list[1].kwargs
    assert ask_kwargs["price"] == pytest.approx(0.55)


def test_refresh_quotes_caps_the_buy_leg_at_cost_basis_when_short(isolated_ledger):
    # book-implied bid is 0.49, but the bot is short at an avg cost of 0.45 --
    # buying back at 0.49 would voluntarily realize a loss, so the bid must be
    # capped down to cost basis instead.
    maker, client, _ = _maker()
    client.create_order.side_effect = [{"id": "bid-1"}, {"id": "ask-1"}]
    # cash_value kept well under the default $40 cap so only the cost-basis
    # floor is exercised here, not the inventory cap too.
    client.get_position.return_value = _position(net_position=-100, cost_value=45.0, cash_value=20.0)

    maker.refresh_quotes()

    bid_kwargs = client.create_order.call_args_list[0].kwargs
    assert bid_kwargs["price"] == pytest.approx(0.45)


def test_refresh_quotes_skips_increasing_leg_once_position_cap_is_hit(isolated_ledger):
    settings = _settings(max_position_usd=40.0)
    maker, client, _ = _maker(settings=settings)
    # Only one leg (the ask) should actually post -- the bid is skipped.
    client.create_order.side_effect = [{"id": "ask-1"}]
    client.get_position.return_value = _position(net_position=100, cost_value=45.0, cash_value=45.0)

    cycle = maker.refresh_quotes()

    assert client.create_order.call_count == 1
    posted_kwargs = client.create_order.call_args_list[0].kwargs
    assert posted_kwargs["action"] == "ORDER_ACTION_SELL"
    assert cycle.bid.order_id is None
    assert cycle.bid.size == 0.0
    assert "skipped" in cycle.bid.error
    assert "position already at cap" in cycle.bid.error


def test_refresh_quotes_skips_entire_cycle_when_both_legs_blocked(isolated_ledger):
    settings = _settings(max_position_usd=40.0)
    maker, client, _ = _maker(settings=settings)
    client.get_open_orders.return_value = [{"id": "stale-order", "marketSlug": "m1"}]
    # Long, already at the position cap, AND the sell leg would realize a
    # loss too far away to floor within valid tick bounds -- both legs blocked.
    client.get_position.return_value = _position(net_position=100, cost_value=99.9, cash_value=45.0)

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.cancel_order.assert_called_once_with("stale-order", "m1")
    client.create_order.assert_not_called()


def test_refresh_quotes_uses_provided_open_orders_without_refetching(isolated_ledger):
    maker, client, _ = _maker()
    provided_open_orders = [{"id": "old-order", "marketSlug": "m1"}]

    maker.refresh_quotes(open_orders=provided_open_orders)

    client.get_open_orders.assert_not_called()
    client.cancel_order.assert_called_once_with("old-order", "m1")


def test_refresh_quotes_fetches_open_orders_itself_when_not_provided(isolated_ledger):
    maker, client, _ = _maker()
    client.get_open_orders.return_value = [{"id": "old-order", "marketSlug": "m1"}]

    maker.refresh_quotes()

    client.get_open_orders.assert_called_once()
    client.cancel_order.assert_called_once_with("old-order", "m1")


def test_refresh_quotes_blocks_increasing_leg_at_extreme_price_with_thin_edge(isolated_ledger):
    # bid=0.13/ask=0.18, tick=0.01 -> improved bid=0.14 (extreme low,
    # <=0.15) / improved ask=0.17 (not extreme). Captured spread is 3c,
    # below the 4c minimum extreme_price_min_edge_cents requires -- only the
    # extreme-priced bid leg should be blocked.
    maker, client, read_client = _maker()
    read_client.get_market_bbo.return_value = {
        "best_bid": 0.13, "best_ask": 0.18, "current_price": 0.155, "last_trade_price": 0.155,
    }
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.13, "quantity": 15.0}, {"price": 0.12, "quantity": 15.0}],
        "asks": [{"price": 0.18, "quantity": 15.0}, {"price": 0.19, "quantity": 15.0}],
    }
    client.create_order.side_effect = [{"id": "ask-1"}]

    cycle = maker.refresh_quotes()

    assert client.create_order.call_count == 1
    posted_kwargs = client.create_order.call_args_list[0].kwargs
    assert posted_kwargs["action"] == "ORDER_ACTION_SELL"
    assert posted_kwargs["price"] == pytest.approx(0.17)
    assert cycle.bid.order_id is None
    assert "extreme price" in cycle.bid.error


def test_refresh_quotes_allows_extreme_price_when_captured_spread_wide_enough(isolated_ledger):
    # bid=0.09/ask=0.30, tick=0.01 -> improved bid=0.10 (extreme low) but
    # captured spread is 19c, well above the 4c minimum -- not blocked.
    maker, client, read_client = _maker()
    read_client.get_market_bbo.return_value = {
        "best_bid": 0.09, "best_ask": 0.30, "current_price": 0.195, "last_trade_price": 0.195,
    }
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.09, "quantity": 15.0}, {"price": 0.08, "quantity": 15.0}],
        "asks": [{"price": 0.30, "quantity": 15.0}, {"price": 0.31, "quantity": 15.0}],
    }

    cycle = maker.refresh_quotes()

    assert client.create_order.call_count == 2
    assert cycle.bid.order_id is not None
    assert cycle.ask.order_id is not None


def test_refresh_quotes_blocks_both_legs_when_payoff_ratio_too_extreme(isolated_ledger):
    # bid=0.49/ask=0.52 (not extreme priced) -> improved bid=0.50/ask=0.51,
    # captured spread only 1c. Worst-case loss per share (~50c/49c) versus a
    # 1c captured spread is a ~50x/49x payoff ratio, over the 30x default cap.
    maker, client, read_client = _maker()
    read_client.get_market_bbo.return_value = {
        "best_bid": 0.49, "best_ask": 0.52, "current_price": 0.505, "last_trade_price": 0.505,
    }
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.49, "quantity": 15.0}, {"price": 0.48, "quantity": 15.0}],
        "asks": [{"price": 0.52, "quantity": 15.0}, {"price": 0.53, "quantity": 15.0}],
    }
    client.get_open_orders.return_value = [{"id": "stale-order", "marketSlug": "m1"}]

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.cancel_order.assert_called_once_with("stale-order", "m1")
    client.create_order.assert_not_called()


def test_reducing_leg_not_blocked_by_extreme_price_or_payoff_ratio_checks(isolated_ledger):
    # Same thin/extreme book as the blocking test above, but short
    # (net_position=-100): BUY becomes the REDUCING leg (profitable
    # buy-back at 0.10 against an avg cost of 0.20) and posts unaffected by
    # either new check, since both only ever apply to the increasing side.
    # SELL remains increasing and is still blocked (thin edge at an extreme
    # price), proving the two legs are evaluated independently. Inventory
    # skew disabled to isolate this from the extreme-price/payoff-ratio
    # checks under test -- it's covered separately elsewhere.
    settings = _settings(inventory_skew_enabled=False)
    maker, client, read_client = _maker(settings=settings)
    read_client.get_market_bbo.return_value = {
        "best_bid": 0.09, "best_ask": 0.14, "current_price": 0.115, "last_trade_price": 0.115,
    }
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.09, "quantity": 15.0}, {"price": 0.08, "quantity": 15.0}],
        "asks": [{"price": 0.14, "quantity": 15.0}, {"price": 0.15, "quantity": 15.0}],
    }
    client.create_order.side_effect = [{"id": "bid-1"}]
    client.get_position.return_value = _position(net_position=-100, cost_value=20.0, cash_value=-10.0)

    cycle = maker.refresh_quotes()

    assert client.create_order.call_count == 1
    posted_kwargs = client.create_order.call_args_list[0].kwargs
    assert posted_kwargs["action"] == "ORDER_ACTION_BUY"
    assert posted_kwargs["price"] == pytest.approx(0.10)
    assert cycle.ask.order_id is None
    assert "extreme price" in cycle.ask.error


def test_refresh_quotes_flat_position_behaves_as_before(isolated_ledger):
    maker, client, _ = _maker()
    client.get_position.return_value = None

    maker.refresh_quotes()

    bid_kwargs = client.create_order.call_args_list[0].kwargs
    ask_kwargs = client.create_order.call_args_list[1].kwargs
    assert bid_kwargs["price"] == pytest.approx(0.49)
    assert ask_kwargs["price"] == pytest.approx(0.51)


def test_refresh_quotes_respects_one_order_budget(isolated_ledger):
    maker, client, _ = _maker()

    cycle = maker.refresh_quotes(max_orders=1)

    assert client.create_order.call_count == 1
    assert cycle.bid.order_id == "bid-1"
    assert cycle.ask.order_id is None
    assert "live order budget reached" in cycle.ask.error


def test_refresh_quotes_uses_l2_book_and_skips_thin_depth(isolated_ledger):
    maker, client, read_client = _maker()
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.48, "quantity": 2.0}],
        "asks": [{"price": 0.52, "quantity": 20.0}],
    }
    client.get_open_orders.return_value = [{"id": "stale-order", "marketSlug": "m1"}]

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.cancel_order.assert_called_once_with("stale-order", "m1")
    client.create_order.assert_not_called()


def test_refresh_quotes_uses_l2_book_when_depth_is_good(isolated_ledger):
    maker, client, read_client = _maker()
    read_client.get_market_book.return_value = {
        "bids": [
            {"price": 0.47, "quantity": 15.0},
            {"price": 0.46, "quantity": 15.0},
        ],
        "asks": [
            {"price": 0.53, "quantity": 15.0},
            {"price": 0.54, "quantity": 15.0},
        ],
    }

    maker.refresh_quotes()

    bid_kwargs = client.create_order.call_args_list[0].kwargs
    ask_kwargs = client.create_order.call_args_list[1].kwargs
    assert bid_kwargs["price"] == pytest.approx(0.48)
    assert ask_kwargs["price"] == pytest.approx(0.52)


def test_refresh_quotes_skips_when_recent_price_move_exceeds_volatility_guard(isolated_ledger):
    settings = _settings(volatility_filter_enabled=True, max_recent_move_cents=3.0)
    maker, client, read_client = _maker(settings=settings)

    maker.refresh_quotes()  # first cycle: records a reference price of 0.50

    client.create_order.reset_mock()
    client.create_order.side_effect = [{"id": "bid-2"}, {"id": "ask-2"}]
    client.get_open_orders.return_value = [{"id": "resting-1", "marketSlug": "m1"}]
    # Market jumped hard between cycles -- reference price moves from 0.50
    # to 0.60, a 10c move, well past the 3c guard.
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.58, "quantity": 15.0}, {"price": 0.57, "quantity": 15.0}],
        "asks": [{"price": 0.62, "quantity": 15.0}, {"price": 0.63, "quantity": 15.0}],
    }

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.create_order.assert_not_called()
    client.cancel_order.assert_called_once_with("resting-1", "m1")


def test_refresh_quotes_allows_quoting_when_volatility_filter_disabled(isolated_ledger):
    settings = _settings(volatility_filter_enabled=False)
    maker, client, read_client = _maker(settings=settings)

    maker.refresh_quotes()
    client.create_order.reset_mock()
    client.create_order.side_effect = [{"id": "bid-2"}, {"id": "ask-2"}]

    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.58, "quantity": 15.0}, {"price": 0.57, "quantity": 15.0}],
        "asks": [{"price": 0.62, "quantity": 15.0}, {"price": 0.63, "quantity": 15.0}],
    }

    cycle = maker.refresh_quotes()

    assert cycle is not None
    assert client.create_order.call_count == 2


def test_refresh_quotes_skews_size_and_price_to_reduce_long_inventory(isolated_ledger):
    settings = _settings(
        inventory_skew_enabled=True,
        inventory_skew_threshold_usd=10.0,
        inventory_reducing_size_multiplier=1.5,
        inventory_increasing_size_multiplier=0.5,
    )
    maker, client, read_client = _maker(settings=settings)
    # Same effective bid/ask (0.48/0.52) as _maker()'s default BBO, just via
    # an explicit L2 book -- this test is about inventory skew math, not
    # depth-checking, so it just needs a book that passes the depth gate.
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.48, "quantity": 15.0}, {"price": 0.47, "quantity": 15.0}],
        "asks": [{"price": 0.52, "quantity": 15.0}, {"price": 0.53, "quantity": 15.0}],
    }
    client.get_position.return_value = _position(net_position=100, cost_value=45.0, cash_value=20.0)

    maker.refresh_quotes()

    bid_kwargs = client.create_order.call_args_list[0].kwargs
    ask_kwargs = client.create_order.call_args_list[1].kwargs
    assert bid_kwargs["price"] == pytest.approx(0.48)
    assert ask_kwargs["price"] == pytest.approx(0.50)
    assert bid_kwargs["quantity"] == pytest.approx(8.0)
    assert ask_kwargs["quantity"] == pytest.approx(26.0)


def test_reduce_only_blocks_both_legs_when_flat(isolated_ledger, caplog):
    # At net_position == 0, BOTH BUY and SELL count as "increasing" (there's
    # nothing to reduce from flat) -- reduce_only must block both legs
    # entirely. Both legs blocked means refresh_quotes() returns None (same
    # existing "both legs blocked by position guards" path), but the market
    # still gets its stale orders cancelled.
    maker, client, _ = _maker(reduce_only_reason="toxicity cooldown")
    client.get_open_orders.return_value = [{"id": "stale-order", "marketSlug": "m1"}]

    with caplog.at_level("INFO"):
        cycle = maker.refresh_quotes()

    assert cycle is None
    client.create_order.assert_not_called()
    client.cancel_order.assert_called_once_with("stale-order", "m1")
    assert any("reduce-only" in r.message for r in caplog.records)


def test_reduce_only_blocks_increasing_leg_when_long(isolated_ledger):
    # Long 100 shares -- BUY is the increasing side (blocked by
    # reduce_only_reason), SELL is the reducing side and posts normally,
    # still subject to the existing cost-basis floor.
    maker, client, _ = _maker(reduce_only_reason="toxicity cooldown")
    client.get_position.return_value = _position(net_position=100, cost_value=45.0, cash_value=20.0)

    cycle = maker.refresh_quotes()

    assert client.create_order.call_count == 1
    posted_kwargs = client.create_order.call_args_list[0].kwargs
    assert posted_kwargs["action"] == "ORDER_ACTION_SELL"
    assert cycle.bid.order_id is None
    assert "reduce-only" in cycle.bid.error
    assert cycle.ask.order_id is not None


def test_reduce_only_false_by_default_matches_existing_behavior(isolated_ledger):
    maker, client, _ = _maker()  # reduce_only_reason defaults to None
    assert maker.reduce_only_reason is None

    maker.refresh_quotes()

    assert client.create_order.call_count == 2  # both legs post normally, unaffected
