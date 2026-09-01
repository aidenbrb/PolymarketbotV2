from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import requests

from polymarket_bot import config
from polymarket_bot.live import ledger as ledger_module
from polymarket_bot.live.market_maker import EmergencySafeguardFailedError, MarketMaker
from polymarket_bot.live.models import PostedLeg
from polymarket_bot.live.us_client import UsApiError


def _api_error(status_code: int, message: str = "rejected") -> UsApiError:
    """Matches the real shape LiveUsClient._request() produces: UsApiError
    with the original requests exception preserved as __cause__ (`raise
    ... from last_exc`), so its response status is introspectable via
    is_client_rejection()."""
    response = Mock(status_code=status_code)
    cause = requests.exceptions.HTTPError(message, response=response)
    exc = UsApiError(f"POST /v1/orders failed: {cause}")
    exc.__cause__ = cause
    return exc


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, "LEDGER_FILE", tmp_path / "orders.json")


def _settings(**overrides):
    # Legacy unit tests isolate their original concern; production enables
    # flat-first and uses a tighter payoff cap via .env. Dedicated tests
    # below opt into those controls explicitly.
    defaults = dict(
        order_shares_min=15.0,
        order_shares_max=20.0,
        min_edge_cents=0.5,
        max_payoff_loss_to_capture_ratio=30.0,
        flat_first_inventory_enabled=False,
        require_both_entry_legs=False,
    )
    defaults.update(overrides)
    return config.LiveTradingSettings(**defaults)


def _maker(
    client=None, read_client=None, settings=None, market_slug="m1", tick_size=0.01,
    reduce_only_reason=None, reduce_urgent=False, in_cooldown=False, event_cap_remaining_usd=None,
    increasing_order_shares=None,
    position_age_hours=None, force_flatten=False, bot_order_ids=None,
):
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
        reduce_urgent=reduce_urgent, in_cooldown=in_cooldown,
        event_cap_remaining_usd=event_cap_remaining_usd,
        increasing_order_shares=increasing_order_shares,
        position_age_hours=position_age_hours,
        force_flatten=force_flatten,
        bot_order_ids=bot_order_ids,
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


def test_refresh_quotes_keeps_unchanged_orders_without_losing_queue_priority(isolated_ledger):
    maker, client, _ = _maker()
    client.get_open_orders.return_value = [
        {
            "id": "bid-old", "marketSlug": "m1", "side": "ORDER_SIDE_BUY",
            "price": {"value": "0.49"}, "leavesQuantity": 17.0,
        },
        {
            "id": "ask-old", "marketSlug": "m1", "side": "ORDER_SIDE_SELL",
            "price": {"value": "0.51"}, "leavesQuantity": 17.0,
        },
    ]

    cycle = maker.refresh_quotes()

    client.cancel_order.assert_not_called()
    client.create_order.assert_not_called()
    assert cycle.bid.order_id == "bid-old"
    assert cycle.ask.order_id == "ask-old"


def test_refresh_quotes_keeps_safe_partial_remainder(isolated_ledger):
    maker, client, _ = _maker()
    client.get_open_orders.return_value = [
        {
            "id": "partial", "marketSlug": "m1", "side": "ORDER_SIDE_BUY",
            "price": {"value": "0.49"}, "quantity": 17.0, "leavesQuantity": 4.0,
        },
    ]

    cycle = maker.refresh_quotes()

    client.cancel_order.assert_not_called()
    assert cycle.bid.order_id == "partial"
    assert cycle.bid.size == pytest.approx(4.0)
    assert client.create_order.call_count == 1  # only the missing ask


def _seconds_ago(seconds: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.isoformat().replace("+00:00", "Z")


def test_keeps_order_within_one_tick_hysteresis(isolated_ledger):
    # Default target is bid=0.49 (1c-tick book improvement) -- a resting
    # bid at 0.48 is exactly one tick away, within the default
    # price_hysteresis_ticks=1.0 band, so it must be kept even though the
    # price isn't an exact match.
    maker, client, _ = _maker()
    client.get_open_orders.return_value = [
        {"id": "bid-old", "marketSlug": "m1", "side": "ORDER_SIDE_BUY", "price": {"value": "0.48"}, "leavesQuantity": 17.0},
        {"id": "ask-old", "marketSlug": "m1", "side": "ORDER_SIDE_SELL", "price": {"value": "0.51"}, "leavesQuantity": 17.0},
    ]

    cycle = maker.refresh_quotes()

    client.cancel_order.assert_not_called()
    assert cycle.bid.order_id == "bid-old"


def test_cancels_order_beyond_hysteresis_band_when_old_enough(isolated_ledger):
    # Two ticks away from the 0.49 target, and no timestamp field at all
    # (age unknown) -- unknown age must NOT protect it, only hysteresis
    # would, and two ticks is outside the default one-tick band.
    maker, client, _ = _maker()
    client.get_open_orders.return_value = [
        {"id": "bid-old", "marketSlug": "m1", "side": "ORDER_SIDE_BUY", "price": {"value": "0.47"}, "leavesQuantity": 17.0},
    ]

    maker.refresh_quotes()

    client.cancel_order.assert_called_once_with("bid-old", "m1")


def test_min_resting_time_protects_a_young_order_beyond_hysteresis(isolated_ledger):
    # Two ticks away from target (outside hysteresis) but posted only 10s
    # ago -- default min_resting_seconds=90 must keep it anyway.
    maker, client, _ = _maker()
    client.get_open_orders.return_value = [
        {
            "id": "bid-old", "marketSlug": "m1", "side": "ORDER_SIDE_BUY", "price": {"value": "0.47"},
            "leavesQuantity": 17.0, "insertTime": _seconds_ago(10),
        },
    ]

    cycle = maker.refresh_quotes()

    client.cancel_order.assert_not_called()
    assert cycle.bid.order_id == "bid-old"


def test_min_resting_time_does_not_protect_an_order_older_than_the_window(isolated_ledger):
    # Same two-ticks-away setup, but posted 200s ago -- past the default
    # 90s min_resting_seconds, so it must be repriced.
    maker, client, _ = _maker()
    client.get_open_orders.return_value = [
        {
            "id": "bid-old", "marketSlug": "m1", "side": "ORDER_SIDE_BUY", "price": {"value": "0.47"},
            "leavesQuantity": 17.0, "insertTime": _seconds_ago(200),
        },
    ]

    maker.refresh_quotes()

    client.cancel_order.assert_called_once_with("bid-old", "m1")


def test_min_resting_time_still_protects_a_young_sell_priced_above_target(isolated_ledger):
    # Symmetric safe-direction case for SELL: asking MORE than the new
    # target is conservative (less likely to fill, not adverse-selection
    # risk), so a young order there is still protected.
    maker, client, _ = _maker()
    client.get_open_orders.return_value = [
        {
            "id": "ask-old", "marketSlug": "m1", "side": "ORDER_SIDE_SELL", "price": {"value": "0.53"},
            "leavesQuantity": 17.0, "insertTime": _seconds_ago(10),
        },
    ]

    cycle = maker.refresh_quotes()

    client.cancel_order.assert_not_called()
    assert cycle.ask.order_id == "ask-old"


def test_cancels_a_young_buy_priced_materially_above_a_sharply_lower_target(isolated_ledger):
    # The unsafe scenario this closes: min_resting_seconds must NOT protect
    # a BUY that's drifted to the AGGRESSIVE side of a sharply-lower new
    # target just because it's young -- a stale high bid is
    # adverse-selection risk (the bot would keep buying above fair value
    # if filled), regardless of age.
    maker, client, read_client = _maker()
    read_client.get_market_bbo.return_value = {
        "best_bid": 0.28, "best_ask": 0.32, "current_price": 0.30, "last_trade_price": 0.30,
    }
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.28, "quantity": 15.0}, {"price": 0.27, "quantity": 15.0}],
        "asks": [{"price": 0.32, "quantity": 15.0}, {"price": 0.33, "quantity": 15.0}],
    }
    client.get_open_orders.return_value = [
        {
            "id": "bid-old", "marketSlug": "m1", "side": "ORDER_SIDE_BUY", "price": {"value": "0.49"},
            "leavesQuantity": 17.0, "insertTime": _seconds_ago(2),
        },
    ]

    maker.refresh_quotes()

    client.cancel_order.assert_called_once_with("bid-old", "m1")


def test_cancels_a_young_sell_priced_materially_below_a_sharply_higher_target(isolated_ledger):
    # Symmetric unsafe scenario for SELL: asking materially LESS than a
    # sharply-higher new target must reprice immediately regardless of age
    # -- a stale low ask would keep selling below fair value if filled.
    maker, client, read_client = _maker()
    read_client.get_market_bbo.return_value = {
        "best_bid": 0.68, "best_ask": 0.72, "current_price": 0.70, "last_trade_price": 0.70,
    }
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.68, "quantity": 15.0}, {"price": 0.67, "quantity": 15.0}],
        "asks": [{"price": 0.72, "quantity": 15.0}, {"price": 0.73, "quantity": 15.0}],
    }
    client.get_open_orders.return_value = [
        {
            "id": "ask-old", "marketSlug": "m1", "side": "ORDER_SIDE_SELL", "price": {"value": "0.51"},
            "leavesQuantity": 17.0, "insertTime": _seconds_ago(2),
        },
    ]

    maker.refresh_quotes()

    client.cancel_order.assert_called_once_with("ask-old", "m1")


def test_order_age_prefers_insert_time_over_last_transact_time(isolated_ledger):
    # lastTransactTime updates on any transaction event, including a
    # partial fill -- must not be preferred over insertTime, or a
    # long-resting, since-partially-filled order would look freshly
    # posted and wrongly re-earn min-resting-time protection. Safe
    # direction (BUY below target) so hysteresis/direction alone doesn't
    # already explain a cancel -- this isolates the timestamp-preference
    # fix specifically: insertTime (200s ago, past the window) must win
    # over a lastTransactTime that (falsely) suggests the order is brand new.
    maker, client, _ = _maker()
    client.get_open_orders.return_value = [
        {
            "id": "bid-old", "marketSlug": "m1", "side": "ORDER_SIDE_BUY", "price": {"value": "0.47"},
            "leavesQuantity": 17.0, "insertTime": _seconds_ago(200), "lastTransactTime": _seconds_ago(5),
        },
    ]

    maker.refresh_quotes()

    client.cancel_order.assert_called_once_with("bid-old", "m1")


def test_reduce_urgent_overrides_hysteresis_and_min_resting_time(isolated_ledger):
    # A risk exit (reduce_urgent) must reprice immediately even for an
    # order that would otherwise be protected by BOTH hysteresis (one tick
    # away) AND min resting time (posted seconds ago).
    maker, client, _ = _maker(reduce_urgent=True)
    client.get_open_orders.return_value = [
        {
            "id": "bid-old", "marketSlug": "m1", "side": "ORDER_SIDE_BUY", "price": {"value": "0.48"},
            "leavesQuantity": 17.0, "insertTime": _seconds_ago(5),
        },
    ]

    maker.refresh_quotes()

    client.cancel_order.assert_called_once_with("bid-old", "m1")


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


def test_refresh_quotes_cancels_and_skips_when_no_book_available(isolated_ledger):
    # With LIVE_REQUIRE_L2_DEPTH off (an explicit opt-out), a genuinely
    # unusable price (no book, no BBO either) still has to fail loudly.
    settings = _settings(require_l2_depth=False)
    maker, client, read_client = _maker(settings=settings)
    read_client.get_market_book.return_value = None
    read_client.get_market_bbo.return_value = None
    assert maker.refresh_quotes() is None
    client.create_order.assert_not_called()


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


def test_post_leg_failure_cancels_known_sibling_and_stops_cycle(isolated_ledger):
    from polymarket_bot.live.us_client import UsApiError

    maker, client, _ = _maker()
    client.create_order.side_effect = [{"id": "bid-1"}, UsApiError("insufficient balance")]

    cycle = maker.refresh_quotes()
    assert cycle is None
    client.cancel_order.assert_called_once_with("bid-1", "m1")
    client.cancel_all.assert_called_once()


def test_ordinary_entry_legs_are_maker_only(isolated_ledger):
    maker, client, _ = _maker()

    maker.refresh_quotes()

    for call in client.create_order.call_args_list:
        assert call.kwargs["participate_dont_initiate"] is True
        assert call.kwargs["tif"] == "TIME_IN_FORCE_GOOD_TILL_CANCEL"


def test_client_rejection_is_a_benign_skip_not_a_cancel_safeguard(isolated_ledger):
    # A definite 4xx (e.g. a participateDontInitiate order that would have
    # crossed the book) is a clean, known outcome -- nothing was placed, no
    # uncertainty -- and must NOT trigger the nuclear account-wide cancel
    # safeguard the way a genuinely uncertain failure does. Uses the
    # default test settings (require_both_entry_legs=False), so a single
    # surviving leg is legitimate here -- see the paired-entry-required
    # tests below for the case where it's NOT.
    maker, client, _ = _maker(settings=_settings(require_both_entry_legs=False))
    client.create_order.side_effect = [{"id": "bid-1"}, _api_error(400)]

    cycle = maker.refresh_quotes()

    assert cycle is not None
    assert cycle.bid.order_id == "bid-1"
    assert cycle.ask.order_id is None
    assert "rejected" in cycle.ask.error
    client.cancel_all.assert_not_called()


def test_paired_entry_required_cancels_surviving_buy_when_sell_rejected(isolated_ledger):
    # The bug this closes: from a flat position with require_both_entry_legs,
    # a maker-only rejection on ONE leg must not leave the other resting
    # alone -- that's exactly the naked, unpaired directional bet the
    # paired-entry guard exists to prevent, and the pre-placement
    # paired_entry_blocked check can't see an exchange-side rejection that
    # only happens after both legs are actually attempted.
    maker, client, _ = _maker(settings=_settings(require_both_entry_legs=True))
    client.create_order.side_effect = [{"id": "bid-1"}, _api_error(400)]

    cycle = maker.refresh_quotes()

    assert cycle is not None
    client.cancel_order.assert_called_once_with("bid-1", "m1")
    # order_id is deliberately PRESERVED (not nulled) so the ledger row
    # record_cycle() writes below still lets execution-backfill discover a
    # partial fill from the submit-to-cancel race -- is_resting (driven by
    # size, not order_id) is what now signals "not actually resting."
    assert cycle.bid.order_id == "bid-1"
    assert cycle.bid.is_resting is False
    assert "cancelled" in cycle.bid.error
    assert cycle.ask.order_id is None
    assert "bid-1" in maker.last_cancelled_order_ids
    client.cancel_all.assert_not_called()


def test_paired_entry_required_cancels_surviving_sell_when_buy_rejected(isolated_ledger):
    # Mirror direction of the above.
    maker, client, _ = _maker(settings=_settings(require_both_entry_legs=True))
    client.create_order.side_effect = [_api_error(400), {"id": "ask-1"}]

    cycle = maker.refresh_quotes()

    assert cycle is not None
    client.cancel_order.assert_called_once_with("ask-1", "m1")
    assert cycle.ask.order_id == "ask-1"
    assert cycle.ask.is_resting is False
    assert "cancelled" in cycle.ask.error
    assert cycle.bid.order_id is None
    assert "ask-1" in maker.last_cancelled_order_ids
    client.cancel_all.assert_not_called()


def test_paired_entry_unwind_cancels_a_kept_surviving_leg_too(isolated_ledger):
    # Must cover a PREVIOUSLY KEPT resting order, not just a freshly
    # posted one -- both normalize to PostedLeg.order_id, so a BUY kept
    # unchanged from an earlier cycle is just as unsafe to leave naked as
    # one posted this same cycle, once its sibling SELL is rejected.
    maker, client, _ = _maker(settings=_settings(require_both_entry_legs=True))
    client.get_open_orders.return_value = [
        {
            "id": "bid-kept", "marketSlug": "m1", "side": "ORDER_SIDE_BUY",
            "price": {"value": "0.49"}, "leavesQuantity": 17.0,
        },
    ]
    client.create_order.side_effect = [_api_error(400)]  # only the SELL leg is freshly attempted

    cycle = maker.refresh_quotes()

    assert cycle is not None
    client.cancel_order.assert_called_once_with("bid-kept", "m1")
    assert cycle.bid.order_id == "bid-kept"
    assert cycle.bid.is_resting is False
    assert "cancelled" in cycle.bid.error
    assert "bid-kept" in maker.last_cancelled_order_ids


def test_unwound_leg_order_id_is_recorded_in_the_ledger(isolated_ledger):
    # The gap this closes: if the surviving leg partially fills during the
    # brief submit-to-cancel race and the private WebSocket misses that
    # execution, execution-backfill (multi_market_maker.py::
    # _backfill_missed_executions) can only ever discover it by looking up
    # an order_id the ledger already knows about -- which requires
    # record_cycle() below to have actually been called with that order_id
    # still attached, not nulled out first.
    maker, client, _ = _maker(settings=_settings(require_both_entry_legs=True))
    client.create_order.side_effect = [{"id": "bid-1"}, _api_error(400)]

    cycle = maker.refresh_quotes()

    assert cycle is not None
    known = ledger_module.get_known_order_details()
    assert "bid-1" in known
    assert known["bid-1"]["market_id"] == "m1"
    assert known["bid-1"]["side"] == "BUY"


def test_paired_entry_unwind_failure_triggers_account_wide_cancel_safeguard(isolated_ledger):
    # Fail closed: if the unwind's own cancel can't be confirmed, don't
    # silently leave the surviving leg resting -- fall through to the same
    # nuclear account-wide-cancel safeguard used for any other uncertain
    # placement state. cancel_all() itself never raises (best-effort, see
    # us_client.py), so its mere return proves nothing -- the emergency
    # path must independently VERIFY no order for this market is still
    # open, and here it genuinely isn't clean (get_open_orders still
    # reports the order as resting), so this must raise a fatal error
    # rather than quietly returning None and letting the bot continue.
    maker, client, _ = _maker(settings=_settings(require_both_entry_legs=True))
    client.create_order.side_effect = [{"id": "bid-1"}, _api_error(400)]
    client.cancel_order.side_effect = UsApiError("cancel uncertain")
    # get_open_orders() is used for two different things in one cycle:
    # start-of-cycle reconciliation (passed explicitly as [] here, so it's
    # not consulted) and _verify_clean_slate()'s post-safeguard check
    # (consulted via this mock) -- still reporting the order as open is
    # exactly what "cancellation cannot be confirmed" looks like in
    # practice.
    client.get_open_orders.return_value = [{"id": "bid-1", "marketSlug": "m1"}]

    with pytest.raises(EmergencySafeguardFailedError):
        maker.refresh_quotes(open_orders=[])

    client.cancel_all.assert_called_once()


def test_emergency_safeguard_confirmed_clean_returns_normally(isolated_ledger):
    # The counterpart to the test above: when the post-safeguard
    # verification genuinely confirms no order remains open for this
    # market, refresh_quotes() must NOT raise -- only an unconfirmed clean
    # slate is fatal, not every use of the safeguard.
    maker, client, _ = _maker(settings=_settings(require_both_entry_legs=True))
    client.create_order.side_effect = [{"id": "bid-1"}, _api_error(400)]
    client.cancel_order.side_effect = UsApiError("cancel uncertain")
    client.get_open_orders.return_value = []

    cycle = maker.refresh_quotes(open_orders=[])

    assert cycle is None
    client.cancel_all.assert_called_once()


def test_409_conflict_is_not_a_benign_skip(isolated_ledger):
    # Confirmed via docs.polymarket.us: a 409 can mean "duplicate order
    # with the same ClOrdID" -- ambiguous about whether an earlier attempt
    # actually succeeded, unlike a clean 400/422/etc rejection. Must keep
    # triggering the nuclear account-wide-cancel safeguard, not be treated
    # as a benign skip the way a definite 400 is.
    maker, client, _ = _maker()
    client.create_order.side_effect = [{"id": "bid-1"}, _api_error(409)]

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.cancel_all.assert_called_once()


def test_server_error_still_triggers_cancel_safeguard(isolated_ledger):
    # Contrast case: a 5xx is genuinely uncertain (unlike a clean 4xx
    # rejection above) and must keep triggering the existing nuclear
    # account-wide-cancel safeguard.
    maker, client, _ = _maker()
    client.create_order.side_effect = [{"id": "bid-1"}, _api_error(503)]

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.cancel_all.assert_called_once()


def test_failed_cancel_blocks_replacement_orders(isolated_ledger):
    from polymarket_bot.live.us_client import UsApiError

    maker, client, _ = _maker()
    client.get_open_orders.return_value = [{"id": "old", "marketSlug": "m1"}]
    client.cancel_order.side_effect = UsApiError("cancel uncertain")

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.create_order.assert_not_called()


def test_success_response_without_order_id_triggers_cancel_safeguard(isolated_ledger):
    maker, client, _ = _maker()
    client.create_order.side_effect = [{}]

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.cancel_all.assert_called_once()


def test_ledger_write_failure_compensates_new_orders(monkeypatch, isolated_ledger):
    maker, client, _ = _maker()
    monkeypatch.setattr(
        "polymarket_bot.live.market_maker.record_cycle",
        Mock(side_effect=OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        maker.refresh_quotes()

    assert {call.args for call in client.cancel_order.call_args_list} == {
        ("bid-1", "m1"), ("ask-1", "m1"),
    }


def test_position_read_failure_cancels_stale_and_never_assumes_flat(isolated_ledger):
    from polymarket_bot.live.us_client import UsApiError

    maker, client, _ = _maker()
    client.get_open_orders.return_value = [{"id": "old", "marketSlug": "m1"}]
    client.get_position.side_effect = UsApiError("positions unavailable")

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.cancel_order.assert_called_once_with("old", "m1")
    client.create_order.assert_not_called()


def test_missing_cost_basis_never_allows_unbounded_reducing_order(isolated_ledger):
    maker, client, _ = _maker(reduce_only_reason="maximum holding time", reduce_urgent=True)
    client.get_position.return_value = {
        "netPositionDecimal": "10",
        "cost": {"value": "0"},
        "cashValue": {"value": "2"},
    }

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.create_order.assert_not_called()


def test_malformed_nonflat_position_fails_closed(isolated_ledger):
    maker, client, _ = _maker()
    client.get_position.return_value = {
        "netPositionDecimal": "10",
        "cost": {},
        "cashValue": {"value": "2"},
    }

    assert maker.refresh_quotes() is None
    client.create_order.assert_not_called()


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
    maker, client, _ = _maker(settings=_settings(max_position_usd=100.0))
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
    # Short cost is complementary collateral: 0.55/share means the YES
    # short was opened at 0.45.
    client.get_position.return_value = _position(net_position=-100, cost_value=55.0, cash_value=20.0)

    maker.refresh_quotes()

    bid_kwargs = client.create_order.call_args_list[0].kwargs
    assert bid_kwargs["price"] == pytest.approx(0.45)


def test_short_cost_basis_is_inverted_from_complementary_collateral(isolated_ledger):
    maker, client, _ = _maker()
    client.get_position.return_value = _position(
        net_position=-18, cost_value=14.04, cash_value=14.0,
    )

    net, avg_entry, exposure = maker._get_position_summary()

    assert net == pytest.approx(-18.0)
    assert avg_entry == pytest.approx(0.22)
    assert exposure == pytest.approx(14.04)


def test_aged_position_can_exit_at_small_bounded_loss(isolated_ledger):
    settings = _settings(
        liquidation_max_holding_hours=6.0,
        liquidation_max_loss_cents=6.0,
        liquidation_max_loss_usd=10.0,
    )
    maker, client, _ = _maker(settings=settings, position_age_hours=3.0)
    client.get_position.return_value = _position(net_position=10, cost_value=5.5, cash_value=5.0)

    maker.refresh_quotes()

    ask_kwargs = client.create_order.call_args_list[1].kwargs
    assert ask_kwargs["price"] == pytest.approx(0.52)


def test_urgent_position_uses_bounded_urgent_loss_allowance(isolated_ledger):
    settings = _settings(
        liquidation_urgent_max_loss_cents=10.0,
        liquidation_max_loss_usd=10.0,
    )
    maker, client, _ = _maker(settings=settings, reduce_urgent=True)
    client.get_position.return_value = _position(net_position=10, cost_value=6.5, cash_value=5.0)

    maker.refresh_quotes()

    ask_kwargs = client.create_order.call_args_list[1].kwargs
    assert ask_kwargs["price"] == pytest.approx(0.55)


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


def test_losing_mark_cannot_reopen_room_under_position_cap(isolated_ledger):
    settings = _settings(max_position_usd=40.0)
    maker, client, _ = _maker(settings=settings)
    client.get_position.return_value = _position(
        net_position=100, cost_value=45.0, cash_value=20.0,
    )

    cycle = maker.refresh_quotes()

    assert cycle.bid.order_id is None
    assert "position already at cap" in cycle.bid.error
    assert cycle.ask.order_id is not None


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


def test_paired_entry_blocks_directional_leg_when_opposite_leg_fails(isolated_ledger):
    settings = _settings(
        max_payoff_loss_to_capture_ratio=20.0,
        require_both_entry_legs=True,
    )
    maker, client, read_client = _maker(settings=settings)
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.22, "quantity": 15.0}, {"price": 0.21, "quantity": 15.0}],
        "asks": [{"price": 0.26, "quantity": 15.0}, {"price": 0.27, "quantity": 15.0}],
    }
    client.get_open_orders.return_value = [{"id": "stale-order", "marketSlug": "m1"}]

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.create_order.assert_not_called()
    client.cancel_order.assert_called_once_with("stale-order", "m1")


def test_paired_entry_posts_both_legs_when_both_pass_twenty_x_cap(isolated_ledger):
    settings = _settings(
        max_payoff_loss_to_capture_ratio=20.0,
        require_both_entry_legs=True,
    )
    maker, client, read_client = _maker(settings=settings)
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.53, "quantity": 15.0}, {"price": 0.52, "quantity": 15.0}],
        "asks": [{"price": 0.58, "quantity": 15.0}, {"price": 0.59, "quantity": 15.0}],
    }

    cycle = maker.refresh_quotes()

    assert client.create_order.call_count == 2
    assert cycle.bid.order_id is not None
    assert cycle.ask.order_id is not None


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


def test_own_resting_quotes_are_removed_before_depth_and_price_calculation(isolated_ledger):
    maker, client, read_client = _maker(bot_order_ids={"bid-old", "ask-old"})
    open_orders = [
        {
            "id": "bid-old", "marketSlug": "m1", "side": "BUY",
            "price": {"value": "0.49"}, "leavesQuantity": "15",
        },
        {
            "id": "ask-old", "marketSlug": "m1", "side": "SELL",
            "price": {"value": "0.51"}, "leavesQuantity": "15",
        },
    ]
    # Our own orders are the displayed top levels.  The external book one
    # tick behind is deep and produces exactly those same target prices.
    read_client.get_market_book.return_value = {
        "bids": [
            {"price": 0.49, "quantity": 15.0},
            {"price": 0.48, "quantity": 15.0},
            {"price": 0.47, "quantity": 15.0},
        ],
        "asks": [
            {"price": 0.51, "quantity": 15.0},
            {"price": 0.52, "quantity": 15.0},
            {"price": 0.53, "quantity": 15.0},
        ],
    }

    cycle = maker.refresh_quotes(open_orders=open_orders)

    assert cycle.bid.order_id == "bid-old"
    assert cycle.ask.order_id == "ask-old"
    client.cancel_order.assert_not_called()
    client.create_order.assert_not_called()


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
        max_position_usd=100.0,
    )
    maker, client, read_client = _maker(settings=settings, reduce_urgent=True)
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


def test_reducing_buy_is_capped_to_remaining_short_position(isolated_ledger):
    maker, client, _ = _maker(reduce_only_reason="maximum holding time")
    # The configured quote size is much larger than the remaining short.
    # The BUY may close 3 shares, but must never flip the account long.
    client.get_position.return_value = _position(
        net_position=-3, cost_value=2.4, cash_value=2.4,
    )

    cycle = maker.refresh_quotes()

    assert cycle.bid.order_id is not None
    bid_kwargs = client.create_order.call_args.kwargs
    assert bid_kwargs["action"] == "ORDER_ACTION_BUY"
    assert bid_kwargs["quantity"] == pytest.approx(3.0)
    assert cycle.ask.order_id is None


def test_reducing_sell_is_capped_to_remaining_long_position(isolated_ledger):
    maker, client, _ = _maker(reduce_only_reason="maximum holding time")
    client.get_position.return_value = _position(
        net_position=2, cost_value=0.9, cash_value=0.9,
    )

    cycle = maker.refresh_quotes()

    assert cycle.ask.order_id is not None
    ask_kwargs = client.create_order.call_args.kwargs
    assert ask_kwargs["action"] == "ORDER_ACTION_SELL"
    assert ask_kwargs["quantity"] == pytest.approx(2.0)
    assert cycle.bid.order_id is None


def test_toxicity_discount_never_shrinks_the_reducing_exit_leg(isolated_ledger):
    """Regression test for the real pilot incident: a 1-share held position
    was only offered at 0.5 shares because a toxicity-cooldown size
    discount landed on the reducing/exit leg instead of only the increasing
    leg. increasing_order_shares must never shrink the reducing leg below
    the full position it's capped against."""
    maker, client, _ = _maker(
        in_cooldown=True,
        # Bypasses the separate reducing-patience skip (see
        # test_reducing_leg_skipped_when_not_urgent_and_in_cooldown) so the
        # leg actually prices instead of being withheld entirely.
        reduce_urgent=True,
        # What a 0.5x toxicity multiplier would produce from a much larger
        # base -- the exact shape of the real incident.
        increasing_order_shares=0.5,
    )
    client.get_position.return_value = _position(
        net_position=1, cost_value=0.45, cash_value=0.45,
    )

    cycle = maker.refresh_quotes()

    assert cycle.ask.order_id is not None
    ask_kwargs = client.create_order.call_args.kwargs
    assert ask_kwargs["action"] == "ORDER_ACTION_SELL"
    assert ask_kwargs["quantity"] == pytest.approx(1.0)


def test_flat_first_long_position_posts_only_reducing_sell(isolated_ledger):
    settings = _settings(flat_first_inventory_enabled=True)
    maker, client, _ = _maker(settings=settings)
    client.get_position.return_value = _position(
        net_position=6.5, cost_value=3.25, cash_value=3.25,
    )

    cycle = maker.refresh_quotes()

    assert client.create_order.call_count == 1
    order = client.create_order.call_args.kwargs
    assert order["action"] == "ORDER_ACTION_SELL"
    assert cycle.bid.order_id is None
    assert "flat-first inventory mode" in cycle.bid.error
    assert cycle.ask.order_id is not None


def test_flat_first_short_position_posts_only_reducing_buy(isolated_ledger):
    settings = _settings(flat_first_inventory_enabled=True)
    maker, client, _ = _maker(settings=settings)
    client.get_position.return_value = _position(
        net_position=-6.5, cost_value=3.25, cash_value=3.25,
    )

    cycle = maker.refresh_quotes()

    assert client.create_order.call_count == 1
    order = client.create_order.call_args.kwargs
    assert order["action"] == "ORDER_ACTION_BUY"
    assert cycle.bid.order_id is not None
    assert cycle.ask.order_id is None
    assert "flat-first inventory mode" in cycle.ask.error


def test_hard_flatten_long_crosses_to_best_bid_despite_thin_depth_and_cost(isolated_ledger):
    settings = _settings(
        flat_first_inventory_enabled=True,
        require_l2_depth=True,
        volatility_filter_enabled=True,
    )
    maker, client, read_client = _maker(settings=settings, force_flatten=True)
    client.get_position.return_value = _position(
        net_position=5.5, cost_value=2.75, cash_value=0.33,
    )
    # Mirrors the real collapsing market: thin book and a bid far below
    # cost. The emergency path must sell at the executable bid, not place a
    # cost-protected order above the ask.
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.06, "quantity": 1.0}],
        "asks": [{"price": 0.37, "quantity": 1.0}],
    }

    cycle = maker.refresh_quotes()

    assert client.create_order.call_count == 1
    order = client.create_order.call_args.kwargs
    assert order["action"] == "ORDER_ACTION_SELL"
    assert order["price"] == pytest.approx(0.06)
    assert order["quantity"] == pytest.approx(5.0)
    assert order["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert cycle.bid.order_id is None
    assert cycle.ask.order_id is not None


def test_hard_flatten_short_crosses_to_best_ask(isolated_ledger):
    maker, client, read_client = _maker(force_flatten=True)
    client.get_position.return_value = _position(
        net_position=-4, cost_value=2.0, cash_value=2.0,
    )
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.10, "quantity": 1.0}],
        "asks": [{"price": 0.37, "quantity": 1.0}],
    }

    cycle = maker.refresh_quotes()

    order = client.create_order.call_args.kwargs
    assert order["action"] == "ORDER_ACTION_BUY"
    assert order["price"] == pytest.approx(0.37)
    assert order["quantity"] == pytest.approx(4.0)
    assert cycle.ask.order_id is None


def test_force_flatten_orders_are_not_maker_only(isolated_ledger):
    # A mandatory liquidation must be allowed to take liquidity -- unlike
    # ordinary entries, force_flatten's IOC legs must NOT set
    # participateDontInitiate.
    maker, client, read_client = _maker(force_flatten=True)
    client.get_position.return_value = _position(
        net_position=-4, cost_value=2.0, cash_value=2.0,
    )
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.10, "quantity": 1.0}],
        "asks": [{"price": 0.37, "quantity": 1.0}],
    }

    maker.refresh_quotes()

    order = client.create_order.call_args.kwargs
    assert order["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert order["participate_dont_initiate"] is False


def test_hard_flatten_uses_bbo_when_l2_book_is_unavailable(isolated_ledger):
    maker, client, read_client = _maker(force_flatten=True)
    client.get_position.return_value = _position(
        net_position=3, cost_value=1.5, cash_value=0.6,
    )
    read_client.get_market_book.return_value = None
    read_client.get_market_bbo.return_value = {"best_bid": 0.20, "best_ask": 0.25}

    maker.refresh_quotes()

    order = client.create_order.call_args.kwargs
    assert order["action"] == "ORDER_ACTION_SELL"
    assert order["price"] == pytest.approx(0.20)


def test_get_book_passes_priority_true_when_force_flatten_or_reduce_urgent(isolated_ledger):
    # _get_book()'s own L2 fetch must bypass StreamingReadClient's REST
    # fallback budget for an emergency exit, same as the BBO fallbacks --
    # a blocked exit-path fallback is worse than the 429 risk the budget
    # otherwise guards against.
    maker, _client, read_client = _maker(reduce_urgent=True)
    maker.refresh_quotes()
    read_client.get_market_book.assert_called_once_with("m1", priority=True)


def test_get_book_passes_priority_false_for_ordinary_quoting(isolated_ledger):
    maker, _client, read_client = _maker()
    maker.refresh_quotes()
    read_client.get_market_book.assert_called_once_with("m1", priority=False)


def test_hard_flatten_bbo_fallback_uses_priority(isolated_ledger):
    maker, client, read_client = _maker(force_flatten=True)
    client.get_position.return_value = _position(
        net_position=3, cost_value=1.5, cash_value=0.6,
    )
    read_client.get_market_book.return_value = None
    read_client.get_market_bbo.return_value = {"best_bid": 0.20, "best_ask": 0.25}

    maker.refresh_quotes()

    read_client.get_market_bbo.assert_called_once_with("m1", priority=True)


def test_require_l2_depth_false_bbo_fallback_uses_priority_for_reduce_urgent(isolated_ledger):
    settings = _settings(require_l2_depth=False)
    maker, _client, read_client = _maker(settings=settings, reduce_urgent=True)
    read_client.get_market_book.return_value = None

    maker.refresh_quotes()

    read_client.get_market_bbo.assert_called_once_with("m1", priority=True)


def test_require_l2_depth_false_bbo_fallback_priority_false_by_default(isolated_ledger):
    settings = _settings(require_l2_depth=False)
    maker, _client, read_client = _maker(settings=settings)
    read_client.get_market_book.return_value = None

    maker.refresh_quotes()

    read_client.get_market_bbo.assert_called_once_with("m1", priority=False)


def test_hard_flatten_long_can_exit_a_one_sided_book(isolated_ledger):
    maker, client, read_client = _maker(force_flatten=True)
    client.get_position.return_value = _position(
        net_position=5, cost_value=2.5, cash_value=0.3,
    )
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.06, "quantity": 1.0}],
        "asks": [],
    }

    cycle = maker.refresh_quotes()

    assert cycle is not None
    order = client.create_order.call_args.kwargs
    assert order["action"] == "ORDER_ACTION_SELL"
    assert order["price"] == pytest.approx(0.06)
    read_client.get_market_bbo.assert_not_called()


def test_hard_flatten_without_any_bbo_preserves_reducing_order(isolated_ledger):
    maker, client, read_client = _maker(force_flatten=True)
    client.get_position.return_value = _position(
        net_position=3, cost_value=1.5, cash_value=0.6,
    )
    read_client.get_market_book.return_value = None
    read_client.get_market_bbo.return_value = None
    client.get_open_orders.return_value = [
        {"id": "increase", "marketSlug": "m1", "side": "ORDER_SIDE_BUY"},
        {"id": "reduce", "marketSlug": "m1", "side": "ORDER_SIDE_SELL"},
    ]

    cycle = maker.refresh_quotes()

    assert cycle is None
    client.cancel_order.assert_called_once_with("increase", "m1")
    client.create_order.assert_not_called()


def test_inventory_skew_nudge_skipped_when_reducing_not_urgent(isolated_ledger):
    # Same setup as the urgent-reduction test above, but reduce_urgent=False:
    # the reducing leg (ask) should NOT get the extra one-tick-more-
    # aggressive nudge -- it stays at the plain book-aware price (0.51, the
    # book improved by one tick, before any inventory-skew adjustment).
    settings = _settings(
        inventory_skew_enabled=True,
        inventory_skew_threshold_usd=10.0,
        max_position_usd=100.0,
    )
    maker, client, read_client = _maker(settings=settings, reduce_urgent=False)
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.48, "quantity": 15.0}, {"price": 0.47, "quantity": 15.0}],
        "asks": [{"price": 0.52, "quantity": 15.0}, {"price": 0.53, "quantity": 15.0}],
    }
    client.get_position.return_value = _position(net_position=100, cost_value=45.0, cash_value=20.0)

    maker.refresh_quotes()

    ask_kwargs = client.create_order.call_args_list[1].kwargs
    assert ask_kwargs["price"] == pytest.approx(0.51)


def test_reducing_leg_skipped_when_not_urgent_and_in_cooldown(isolated_ledger):
    # Long 100 shares, not urgent, in a toxicity cooldown -- the reducing
    # SELL leg is skipped entirely this cycle instead of exiting into what
    # may be a fast adverse move (the real COL/LAD incident this guards
    # against). The BUY leg is still blocked as "increasing" from a long
    # position at net_position > 0 only if reduce_only_reason is set; here
    # it isn't, so BUY posts normally and only the reducing SELL is skipped.
    maker, client, _ = _maker(
        settings=_settings(max_position_usd=100.0),
        reduce_urgent=False,
        in_cooldown=True,
    )
    client.get_position.return_value = _position(net_position=100, cost_value=45.0, cash_value=20.0)

    cycle = maker.refresh_quotes()

    assert cycle.bid.order_id is not None
    assert cycle.ask.order_id is None
    assert "reducing patience" in cycle.ask.error


def test_reducing_leg_still_reduces_when_urgent_even_in_cooldown(isolated_ledger):
    maker, client, _ = _maker(reduce_urgent=True, in_cooldown=True)
    client.get_position.return_value = _position(net_position=100, cost_value=45.0, cash_value=20.0)

    cycle = maker.refresh_quotes()

    assert cycle.ask.order_id is not None


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


def test_event_cap_remaining_usd_none_leaves_sizing_unaffected(isolated_ledger):
    maker, client, _ = _maker(event_cap_remaining_usd=None)

    maker.refresh_quotes()

    bid_kwargs = client.create_order.call_args_list[0].kwargs
    ask_kwargs = client.create_order.call_args_list[1].kwargs
    # (order_shares_min + max) / 2 = 17.5, floored to whole shares (default
    # min_trade_qty=1.0) -- unclipped by any event-cap constraint.
    assert bid_kwargs["quantity"] == pytest.approx(17.0)
    assert ask_kwargs["quantity"] == pytest.approx(17.0)


def test_increasing_leg_clipped_to_event_cap_remaining_usd(isolated_ledger):
    # Flat position -- BOTH BUY and SELL count as increasing simultaneously,
    # but a filled bid and filled ask on the SAME token can't both be
    # simultaneously realized risk (if both fill, they net to a captured
    # spread, not additive exposure) -- so each leg is clipped against the
    # FULL $1.0 headroom independently, not a shared/split budget (see
    # live/RUNBOOK.md's most recent section). Base shares 17.5 each; bid
    # risks 0.49/share -> 1.0/0.49=2.0408.. floors to 2.0; ask risks the
    # complementary 1-0.51=0.49/share -> the same 2.0.
    maker, client, _ = _maker(event_cap_remaining_usd=1.0)

    maker.refresh_quotes()

    bid_kwargs = client.create_order.call_args_list[0].kwargs
    ask_kwargs = client.create_order.call_args_list[1].kwargs
    assert bid_kwargs["quantity"] == pytest.approx(2.0)
    assert ask_kwargs["quantity"] == pytest.approx(2.0)
    # Each leg independently respects the cap in isolation -- their SUM is
    # deliberately allowed to exceed it (up to ~2x), since they can't both
    # be simultaneously realized risk. The cross-market running tally
    # (multi_market_maker.py::_record_provisional_exposure) is what
    # actually protects against sibling markets compounding real risk
    # within one cycle, and stays fully additive.
    assert bid_kwargs["quantity"] * bid_kwargs["price"] <= 1.0 + 1e-6
    assert ask_kwargs["quantity"] * (1.0 - ask_kwargs["price"]) <= 1.0 + 1e-6


def test_flat_asymmetric_legs_each_independently_respect_the_shared_cap(isolated_ledger):
    # Asymmetric book -- bid risks $0.20/share, ask risks (1-0.90)=$0.10/
    # share -- different risk-per-share on each side. Each leg is clipped
    # against the FULL $1.0 cap independently: bid reaches 1.0/0.20=5.0
    # shares ($1.00), ask reaches 1.0/0.10=10.0 shares ($1.00). Their
    # combined $2.00 deliberately exceeds the $1.00 cap -- a filled bid and
    # filled ask on the same token can't both be simultaneously realized
    # risk, so the true worst case is each leg's OWN commitment, not their
    # sum. See live/RUNBOOK.md's most recent section.
    maker, client, read_client = _maker(event_cap_remaining_usd=1.0)
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.19, "quantity": 20.0}, {"price": 0.18, "quantity": 20.0}],
        "asks": [{"price": 0.91, "quantity": 20.0}, {"price": 0.92, "quantity": 20.0}],
    }

    maker.refresh_quotes()

    bid_kwargs = client.create_order.call_args_list[0].kwargs
    ask_kwargs = client.create_order.call_args_list[1].kwargs
    assert bid_kwargs["price"] == pytest.approx(0.20)
    assert ask_kwargs["price"] == pytest.approx(0.90)
    assert bid_kwargs["quantity"] == pytest.approx(5.0)
    assert ask_kwargs["quantity"] == pytest.approx(10.0)
    assert bid_kwargs["quantity"] * bid_kwargs["price"] <= 1.0 + 1e-6
    assert ask_kwargs["quantity"] * (1.0 - ask_kwargs["price"]) <= 1.0 + 1e-6


def test_short_increasing_leg_uses_complementary_cap_risk(isolated_ledger):
    settings = _settings(
        extreme_price_low_threshold=0.01,
        extreme_price_high_threshold=0.99,
    )
    maker, client, read_client = _maker(
        settings=settings, event_cap_remaining_usd=0.9,
    )
    client.get_position.return_value = _position(
        net_position=-5, cost_value=0.5, cash_value=0.5,
    )
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.88, "quantity": 20.0}, {"price": 0.87, "quantity": 20.0}],
        "asks": [{"price": 0.92, "quantity": 20.0}, {"price": 0.93, "quantity": 20.0}],
    }

    maker.refresh_quotes()

    ask = next(
        call.kwargs for call in client.create_order.call_args_list
        if call.kwargs["action"] == "ORDER_ACTION_SELL"
    )
    assert ask["quantity"] == pytest.approx(10.0)


def test_increasing_leg_skipped_when_event_cap_headroom_is_zero(isolated_ledger):
    maker, client, _ = _maker(event_cap_remaining_usd=0.0)

    cycle = maker.refresh_quotes()

    assert cycle.bid.order_id is None
    assert "event exposure cap: no headroom remaining" in cycle.bid.error
    assert cycle.ask.order_id is None
    assert "event exposure cap: no headroom remaining" in cycle.ask.error


def test_reducing_leg_never_clipped_by_event_cap(isolated_ledger):
    # Long position -- SELL is the reducing leg, BUY is increasing. The
    # event-exposure cap must only ever clip the increasing (BUY) leg.
    position = _position(net_position=100, cost_value=45.0, cash_value=20.0)

    roomy = _settings(max_position_usd=100.0)
    maker_unclipped, client_unclipped, _ = _maker(
        settings=roomy, event_cap_remaining_usd=None,
    )
    client_unclipped.get_position.return_value = position
    maker_unclipped.refresh_quotes()
    unclipped_bid_shares = client_unclipped.create_order.call_args_list[0].kwargs["quantity"]
    unclipped_ask_shares = client_unclipped.create_order.call_args_list[1].kwargs["quantity"]

    maker_clipped, client_clipped, _ = _maker(
        settings=roomy, event_cap_remaining_usd=0.5,
    )
    client_clipped.get_position.return_value = position
    maker_clipped.refresh_quotes()
    clipped_bid_shares = client_clipped.create_order.call_args_list[0].kwargs["quantity"]
    clipped_ask_shares = client_clipped.create_order.call_args_list[1].kwargs["quantity"]

    assert clipped_ask_shares == pytest.approx(unclipped_ask_shares)  # reducing leg unaffected
    assert clipped_bid_shares < unclipped_bid_shares  # increasing leg WAS clipped smaller
