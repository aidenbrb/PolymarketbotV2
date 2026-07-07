import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live import ledger as ledger_module
from polymarket_bot.live.event_exposure import compute_event_exposures
from polymarket_bot.live.ledger import record_cycle
from polymarket_bot.live.market_maker import MarketMaker
from polymarket_bot.live.models import LiveQuoteCycle, PostedLeg
from polymarket_bot.live.multi_market_maker import MultiMarketMaker
from polymarket_bot.live.us_client import UsApiError
from polymarket_bot.models import Market, ScoredMarket


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, "LEDGER_FILE", tmp_path / "orders.json")


def _record_known_order(order_id: str, market_id: str = "m1") -> None:
    """Seeds the (isolated) ledger with a recorded cycle so order_id is
    recognized as bot-owned by ledger.get_known_order_ids() -- required
    before any stale/unmanaged-candidate sweep test can expect a
    cancellation, now that the sweep only touches orders it recognizes."""
    record_cycle(LiveQuoteCycle(
        cycle_id=f"seed-{order_id}",
        market_id=market_id,
        reference_price=0.5,
        tick_size=0.01,
        bid=PostedLeg(side="BUY", price=0.5, size=1.0, order_id=order_id),
        ask=PostedLeg(side="SELL", price=0.5, size=1.0, order_id=None),
        timestamp="2026-01-01T00:00:00+00:00",
    ))


def _client(**overrides):
    client = Mock()
    client.get_open_orders.return_value = []
    client.get_position.return_value = None
    client.get_all_positions.return_value = {}
    client.create_order.side_effect = lambda **kwargs: {"id": f"order-{client.create_order.call_count}"}
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


def _read_client(**bbo_overrides):
    read_client = Mock()
    bbo = {"best_bid": 0.48, "best_ask": 0.52, "current_price": 0.5, "last_trade_price": 0.5}
    bbo.update(bbo_overrides)
    read_client.get_market_bbo.return_value = bbo
    # A real, sufficiently deep L2 book matching the same bid/ask. Default
    # settings require this (LIVE_REQUIRE_L2_DEPTH defaults True).
    read_client.get_market_book.return_value = {
        "bids": [
            {"price": bbo["best_bid"], "quantity": 15.0},
            {"price": bbo["best_bid"] - 0.01, "quantity": 15.0},
        ],
        "asks": [
            {"price": bbo["best_ask"], "quantity": 15.0},
            {"price": bbo["best_ask"] + 0.01, "quantity": 15.0},
        ],
    }
    return read_client


def _scored(market_id: str) -> ScoredMarket:
    market = Market(
        market_id=market_id,
        event_id=f"e-{market_id}",
        question=f"Will {market_id} happen?",
        category="politics",
        token_ids=[f"t-{market_id}"],
        spread=0.04,
        raw={"orderPriceMinTickSize": 0.01},
    )
    return ScoredMarket(
        market_id=market_id,
        question=market.question,
        total_score=90.0,
        component_scores={},
        explanation=[],
        recommendation="PAPER_CANDIDATE",
        market=market,
    )


def test_multi_market_maker_stops_at_order_budget(monkeypatch, isolated_ledger):
    client = _client()
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0,
        order_shares_max=20.0,
        max_orders_per_cycle=3,
    )

    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1"), _scored("m2"), _scored("m3")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()

    assert len(cycles) == 2
    assert client.create_order.call_count == 3
    assert cycles[0].bid.order_id is not None
    assert cycles[0].ask.order_id is not None
    assert cycles[1].bid.order_id is not None
    assert cycles[1].ask.order_id is None
    assert "live order budget reached" in cycles[1].ask.error


def test_multi_market_maker_fetches_open_orders_once_regardless_of_candidate_count(
    monkeypatch, isolated_ledger
):
    client = _client()
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=10,
    )

    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1"), _scored("m2"), _scored("m3")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    maker.refresh_quotes()

    # Previously each of the 3 candidates independently called
    # get_open_orders() (an account-wide endpoint, not per-market) -- this
    # was hitting the real rate limit. It must now be fetched exactly once
    # per cycle and shared across all candidates.
    client.get_open_orders.assert_called_once()


def test_multi_market_maker_skips_whole_cycle_when_open_orders_unavailable(
    monkeypatch, isolated_ledger
):
    client = _client(get_open_orders=Mock(side_effect=UsApiError("429 too many requests")))
    read_client = _read_client()
    settings = config.LiveTradingSettings(max_orders_per_cycle=10)

    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()

    assert cycles == []
    client.create_order.assert_not_called()


def test_multi_market_maker_manages_held_position_outside_ranked_candidates(isolated_ledger):
    # A position exists on a market that ISN'T among this cycle's ranked
    # candidates (e.g. it fell out of the top-N spread ranking since it was
    # last quoted). It must still get a MarketMaker turn instead of being
    # silently abandoned with no cancel/re-price/cost-basis protection.
    client = _client(
        get_all_positions=Mock(return_value={
            "orphan-slug": {
                "netPositionDecimal": "17.5",
                "cost": {"value": "9.0", "currency": "USD"},
                "cashValue": {"value": "8.5", "currency": "USD"},
            }
        }),
    )
    client.get_position.return_value = {
        "netPositionDecimal": "17.5",
        "cost": {"value": "9.0", "currency": "USD"},
        "cashValue": {"value": "8.5", "currency": "USD"},
    }
    read_client = _read_client()
    settings = config.LiveTradingSettings(order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=10)

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes(candidates=[])  # nothing ranked this cycle at all

    assert len(cycles) == 1
    assert cycles[0].market_id == "orphan-slug"


def test_multi_market_maker_orphaned_positions_take_priority_over_new_candidates_for_budget(
    monkeypatch, isolated_ledger
):
    # Real money already at stake in a held position must outrank opening a
    # brand-new speculative quote when the shared order budget is tight --
    # otherwise a held position can get starved out indefinitely whenever
    # there are enough ranked candidates to fill the budget on their own
    # (this happened for real on 2026-07-05).
    client = _client(
        get_all_positions=Mock(return_value={
            "orphan-slug": {"netPositionDecimal": "17.5", "cost": {"value": "9.0"}, "cashValue": {"value": "8.5"}}
        }),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=2,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()

    # The orphan's bid+ask consume the whole budget of 2 -- m1 (a brand-new
    # candidate with no existing exposure) never gets a turn this cycle, but
    # the cycle must still complete cleanly.
    assert len(cycles) == 1
    assert cycles[0].market_id == "orphan-slug"
    assert all(c.market_id != "m1" for c in cycles)


def test_multi_market_maker_cancels_stale_orders_on_markets_no_longer_selected(
    monkeypatch, isolated_ledger
):
    # A market that was quoted in a past cycle but is now neither a ranked
    # candidate nor a held position -- its resting order would otherwise
    # never be revisited by anything, since no MarketMaker gets constructed
    # for a market outside both sets.
    _record_known_order("stale-1", market_id="abandoned-market")
    client = _client(
        get_open_orders=Mock(return_value=[
            {"id": "stale-1", "marketSlug": "abandoned-market"},
        ]),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=10,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    maker.refresh_quotes()

    client.cancel_order.assert_called_once_with("stale-1", "abandoned-market")


def test_multi_market_maker_does_not_cancel_candidate_orders_before_their_turn(
    monkeypatch, isolated_ledger
):
    # m1 has its own resting order at the start of the cycle. The stale-order
    # sweep must not treat it as abandoned just because its MarketMaker turn
    # hasn't run yet -- candidate_slugs is computed from the full candidate
    # list up front, before either loop starts.
    client = _client(
        get_open_orders=Mock(return_value=[
            {"id": "m1-resting", "marketSlug": "m1"},
        ]),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=10,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    maker.refresh_quotes()

    # Cancelled exactly once, by m1's own MarketMaker cancel-before-post --
    # not an extra time from being wrongly swept as "stale."
    client.cancel_order.assert_called_once_with("m1-resting", "m1")


def test_multi_market_maker_does_not_treat_orphaned_position_markets_as_stale(
    monkeypatch, isolated_ledger
):
    client = _client(
        get_open_orders=Mock(return_value=[
            {"id": "orphan-resting", "marketSlug": "orphan-slug"},
        ]),
        get_all_positions=Mock(return_value={
            "orphan-slug": {"netPositionDecimal": "17.5", "cost": {"value": "9.0"}, "cashValue": {"value": "8.5"}},
        }),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=10,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    maker.refresh_quotes()

    # Cancelled exactly once, by the orphan's own MarketMaker
    # cancel-before-post -- the stale-order sweep must not ALSO cancel it.
    matching_calls = [
        c for c in client.cancel_order.call_args_list if c.args == ("orphan-resting", "orphan-slug")
    ]
    assert len(matching_calls) == 1


def test_multi_market_maker_stale_cancel_failure_is_logged_and_cycle_continues(
    monkeypatch, isolated_ledger
):
    _record_known_order("stale-1", market_id="abandoned-1")
    _record_known_order("stale-2", market_id="abandoned-2")
    client = _client(
        get_open_orders=Mock(return_value=[
            {"id": "stale-1", "marketSlug": "abandoned-1"},
            {"id": "stale-2", "marketSlug": "abandoned-2"},
        ]),
    )
    client.cancel_order.side_effect = UsApiError("network error")
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=10,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()  # must not raise despite both cancels failing

    assert cycles == []
    assert client.cancel_order.call_count == 2  # tried both despite failures


# ---------------------------------------------------------------------
# Issue 1: candidates starved of the order budget must not be left unmanaged
# ---------------------------------------------------------------------

def test_multi_market_maker_cancels_unmanaged_candidate_order_when_budget_exhausted(
    monkeypatch, isolated_ledger
):
    # m1 consumes the whole budget of 2 (bid+ask) -- m2 never gets a
    # MarketMaker turn this cycle, so its old resting order was never
    # refreshed and must be cancelled as unmanaged, not left resting.
    _record_known_order("m2-old", market_id="m2")
    client = _client(
        get_open_orders=Mock(return_value=[{"id": "m2-old", "marketSlug": "m2"}]),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=2,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1"), _scored("m2")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()

    assert len(cycles) == 1
    assert cycles[0].market_id == "m1"
    client.cancel_order.assert_called_once_with("m2-old", "m2")


def test_multi_market_maker_cancels_unmanaged_candidate_after_orphan_exhausts_budget(
    monkeypatch, isolated_ledger
):
    # The orphaned position consumes the whole budget of 2 -- m1, a ranked
    # candidate, never gets a turn this cycle. Its old resting order must
    # still be cancelled as unmanaged, even though it lost out to an
    # orphaned position rather than to another candidate.
    _record_known_order("m1-old", market_id="m1")
    client = _client(
        get_open_orders=Mock(return_value=[{"id": "m1-old", "marketSlug": "m1"}]),
        get_all_positions=Mock(return_value={
            "orphan-slug": {"netPositionDecimal": "17.5", "cost": {"value": "9.0"}, "cashValue": {"value": "8.5"}},
        }),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=2,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()

    assert len(cycles) == 1
    assert cycles[0].market_id == "orphan-slug"
    client.cancel_order.assert_called_once_with("m1-old", "m1")


def test_multi_market_maker_unmanaged_candidate_cancel_failure_does_not_stop_others(
    monkeypatch, isolated_ledger
):
    # m1 consumes the budget; m2 and m3 are both starved and both have old
    # resting orders. A cancel failure on one must not prevent the other
    # from being attempted, and must not raise out of the cycle.
    _record_known_order("m2-old", market_id="m2")
    _record_known_order("m3-old", market_id="m3")
    client = _client(
        get_open_orders=Mock(return_value=[
            {"id": "m2-old", "marketSlug": "m2"},
            {"id": "m3-old", "marketSlug": "m3"},
        ]),
    )
    client.cancel_order.side_effect = UsApiError("network error")
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=2,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1"), _scored("m2"), _scored("m3")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()  # must not raise despite both cancels failing

    assert len(cycles) == 1
    assert cycles[0].market_id == "m1"
    assert client.cancel_order.call_count == 2  # tried both m2 and m3 despite failures


# ---------------------------------------------------------------------
# Zero order budget means "post nothing," not "leave stale orders resting"
# ---------------------------------------------------------------------

def test_multi_market_maker_zero_budget_still_cleans_bot_owned_stale_orders(
    monkeypatch, isolated_ledger
):
    _record_known_order("stale-1", market_id="abandoned-market")
    client = _client(
        get_open_orders=Mock(return_value=[
            {"id": "stale-1", "marketSlug": "abandoned-market"},
        ]),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(max_orders_per_cycle=0)
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()

    assert cycles == []
    client.create_order.assert_not_called()
    client.cancel_order.assert_called_once_with("stale-1", "abandoned-market")


def test_multi_market_maker_zero_budget_cleans_unmanaged_candidate_orders(
    monkeypatch, isolated_ledger
):
    _record_known_order("m1-old", market_id="m1")
    client = _client(
        get_open_orders=Mock(return_value=[{"id": "m1-old", "marketSlug": "m1"}]),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(max_orders_per_cycle=0)
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()

    assert cycles == []
    client.create_order.assert_not_called()
    client.cancel_order.assert_called_once_with("m1-old", "m1")


def test_multi_market_maker_zero_budget_respects_unknown_positions_guard(
    monkeypatch, isolated_ledger
):
    _record_known_order("abandoned-old", market_id="abandoned-market")
    client = _client(
        get_open_orders=Mock(return_value=[
            {"id": "abandoned-old", "marketSlug": "abandoned-market"},
        ]),
        get_all_positions=Mock(side_effect=UsApiError("network error")),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(max_orders_per_cycle=0)
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()

    assert cycles == []
    client.create_order.assert_not_called()
    client.cancel_order.assert_not_called()


# ---------------------------------------------------------------------
# Issue 2: an unknown position list must disable the non-candidate sweep
# ---------------------------------------------------------------------

def test_multi_market_maker_skips_stale_sweep_when_positions_unknown(monkeypatch, isolated_ledger, caplog):
    # Even a bot-owned, otherwise-cancellable order must be left alone here
    # -- the point is that positions being unknown disables the sweep
    # entirely, not that ownership is what's saving it.
    _record_known_order("abandoned-old", market_id="abandoned-market")
    client = _client(
        get_open_orders=Mock(return_value=[{"id": "abandoned-old", "marketSlug": "abandoned-market"}]),
        get_all_positions=Mock(side_effect=UsApiError("network error")),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=10,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    with caplog.at_level("WARNING"):
        cycles = maker.refresh_quotes()

    client.cancel_order.assert_not_called()
    assert len(cycles) == 1
    assert cycles[0].market_id == "m1"
    assert any(
        "could not confirm the full set of held positions" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------
# Issue 3: sweeps must only ever cancel orders this bot recognizes as its own
# ---------------------------------------------------------------------

def test_multi_market_maker_leaves_unrecognized_stale_order_alone(monkeypatch, isolated_ledger, caplog):
    # Never recorded in the ledger -- e.g. a manual trade, or a different
    # strategy sharing the same account. Must never be cancelled by the
    # stale sweep just because its market isn't a candidate or position.
    client = _client(
        get_open_orders=Mock(return_value=[{"id": "unknown-order", "marketSlug": "abandoned-market"}]),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=10,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    with caplog.at_level("INFO"):
        maker.refresh_quotes()

    client.cancel_order.assert_not_called()
    assert any(
        "not recognized as an order this bot placed" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------
# A market that crashes mid-turn must not be marked "managed"
# ---------------------------------------------------------------------

def test_multi_market_maker_does_not_mark_managed_when_market_maker_crashes(
    monkeypatch, isolated_ledger
):
    # m1's own MarketMaker.refresh_quotes() raises before it can reach its
    # internal cancel-before-post step. It must NOT be marked "managed" --
    # otherwise its stale resting order becomes permanently invisible to
    # the unmanaged-candidate cleanup sweep for as long as the error recurs.
    _record_known_order("m1-old", market_id="m1")
    client = _client(
        get_open_orders=Mock(return_value=[{"id": "m1-old", "marketSlug": "m1"}]),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=10,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.MarketMaker.refresh_quotes",
        Mock(side_effect=RuntimeError("boom")),
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()  # must not raise

    assert cycles == []
    # m1 never got a genuine turn -- the unmanaged-candidate sweep must
    # still pick up its stale resting order.
    client.cancel_order.assert_called_once_with("m1-old", "m1")


def test_multi_market_maker_marks_managed_when_market_maker_succeeds(
    monkeypatch, isolated_ledger
):
    # Regression check for the fix above: a market that runs normally
    # (whether or not it posts a fresh quote) must still be excluded from
    # the unmanaged-candidate sweep, same as before the fix.
    client = _client()
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=15.0, order_shares_max=20.0, max_orders_per_cycle=10,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()

    assert len(cycles) == 1
    assert cycles[0].market_id == "m1"
    client.cancel_order.assert_not_called()  # nothing stale to sweep -- m1 was managed normally


def test_refresh_quotes_with_no_override_behaves_as_before(monkeypatch, isolated_ledger):
    client = _client()
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes(candidates=[_scored("m1")])

    assert cycles[0].bid.size == pytest.approx(16.0)
    assert cycles[0].ask.size == pytest.approx(16.0)


def test_refresh_quotes_settings_override_only_affects_order_size(monkeypatch, isolated_ledger):
    client = _client()
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )
    override = dataclasses.replace(settings, order_shares_min=8.0, order_shares_max=8.0)

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes(candidates=[_scored("m1")], settings_override=override)

    assert cycles[0].bid.size == pytest.approx(8.0)
    assert cycles[0].ask.size == pytest.approx(8.0)


def test_refresh_quotes_settings_override_does_not_affect_order_budget(monkeypatch, isolated_ledger):
    # The order budget (and everything else about candidate handling) must
    # always come from self.settings, never settings_override -- only order
    # share size is meant to be affected (see live/equity_protection.py).
    client = _client()
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=3,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1"), _scored("m2"), _scored("m3")],
    )
    # A settings_override with a SMALLER budget than self.settings -- if the
    # budget loop incorrectly read it, fewer orders would be placed.
    override = dataclasses.replace(settings, max_orders_per_cycle=0)

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes(
        candidates=[_scored("m1"), _scored("m2"), _scored("m3")], settings_override=override,
    )

    # self.settings' budget of 3 (not override's 0) was used -- if the
    # override had incorrectly been read for the budget, this would be 0.
    assert client.create_order.call_count == 3


class TestEffectiveSettingsFor:
    def test_unchanged_when_not_in_cooldown(self):
        settings = config.LiveTradingSettings(min_edge_cents=0.5, order_shares_min=15.0, order_shares_max=20.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        assert maker._effective_settings_for(settings, in_cooldown=False) is settings

    def test_widened_and_halved_when_in_cooldown(self):
        settings = config.LiveTradingSettings(
            min_edge_cents=0.5, order_shares_min=15.0, order_shares_max=20.0,
            toxicity_min_edge_multiplier=2.0, toxicity_size_multiplier=0.5,
        )
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        effective = maker._effective_settings_for(settings, in_cooldown=True)
        assert effective.min_edge_cents == pytest.approx(1.0)
        assert effective.order_shares_min == pytest.approx(7.5)
        assert effective.order_shares_max == pytest.approx(10.0)

    def test_composes_on_top_of_existing_settings_override(self):
        settings = config.LiveTradingSettings(
            min_edge_cents=0.5, order_shares_min=15.0, order_shares_max=20.0,
            toxicity_min_edge_multiplier=2.0, toxicity_size_multiplier=0.5,
        )
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        # An equity-protection-style override already halved size once.
        equity_override = dataclasses.replace(settings, order_shares_min=7.5, order_shares_max=10.0)
        effective = maker._effective_settings_for(equity_override, in_cooldown=True)
        # Toxicity halves it AGAIN, on top of the already-halved override.
        assert effective.order_shares_min == pytest.approx(3.75)
        assert effective.order_shares_max == pytest.approx(5.0)
        # Multipliers always come from self.settings, not the override.
        assert effective.min_edge_cents == pytest.approx(1.0)

    def test_near_resolution_widens_edge_only_no_size_change(self):
        settings = config.LiveTradingSettings(
            min_edge_cents=0.5, order_shares_min=15.0, order_shares_max=20.0,
            near_resolution_min_edge_multiplier=2.0,
        )
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        effective = maker._effective_settings_for(settings, in_cooldown=False, near_resolution=True)
        assert effective.min_edge_cents == pytest.approx(1.0)
        assert effective.order_shares_min == pytest.approx(15.0)
        assert effective.order_shares_max == pytest.approx(20.0)

    def test_near_resolution_stacks_with_toxicity_cooldown(self):
        settings = config.LiveTradingSettings(
            min_edge_cents=0.5, order_shares_min=15.0, order_shares_max=20.0,
            toxicity_min_edge_multiplier=2.0, toxicity_size_multiplier=0.5,
            near_resolution_min_edge_multiplier=2.0,
        )
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        effective = maker._effective_settings_for(settings, in_cooldown=True, near_resolution=True)
        # Both multipliers apply to min_edge_cents (0.5 * 2.0 * 2.0); only
        # toxicity's multiplier touches order size.
        assert effective.min_edge_cents == pytest.approx(2.0)
        assert effective.order_shares_min == pytest.approx(7.5)
        assert effective.order_shares_max == pytest.approx(10.0)


class TestEventStartedGating:
    def _raw(self, hours_from_now: float) -> dict:
        when = (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).isoformat()
        return {"gameStartTime": when}

    def test_fail_open_when_raw_missing(self):
        maker = MultiMarketMaker(client=_client(), settings=config.LiveTradingSettings(), read_client=_read_client())
        assert maker._is_event_started(None) is False
        assert maker._is_event_started({}) is False

    def test_true_when_game_start_time_in_the_past(self):
        settings = config.LiveTradingSettings(max_started_event_hours=0.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        assert maker._is_event_started(self._raw(-1.0)) is True

    def test_false_when_game_start_time_in_the_future(self):
        settings = config.LiveTradingSettings(max_started_event_hours=0.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        assert maker._is_event_started(self._raw(1.0)) is False

    def test_reduce_only_reason_includes_event_started(self):
        settings = config.LiveTradingSettings(max_started_event_hours=0.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        _in_cooldown, reason, _warn = maker._event_and_toxicity_gating(
            "some-slug", self._raw(-1.0), {},
        )
        assert reason == "event already started"

    def test_reduce_only_reason_none_when_not_started_and_no_other_trigger(self):
        settings = config.LiveTradingSettings(max_started_event_hours=0.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        _in_cooldown, reason, _warn = maker._event_and_toxicity_gating(
            "some-slug", self._raw(1.0), {},
        )
        assert reason is None


class TestNearResolutionGating:
    def _raw(self, hours_from_now: float) -> dict:
        when = (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).isoformat()
        return {"gameStartTime": when}

    def test_fail_open_when_raw_missing(self):
        maker = MultiMarketMaker(client=_client(), settings=config.LiveTradingSettings(), read_client=_read_client())
        assert maker._is_near_resolution(None) is False
        assert maker._is_near_resolution({}) is False

    def test_true_within_threshold(self):
        settings = config.LiveTradingSettings(near_resolution_hours_threshold=24.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        assert maker._is_near_resolution(self._raw(12.0)) is True

    def test_false_outside_threshold(self):
        settings = config.LiveTradingSettings(near_resolution_hours_threshold=24.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        assert maker._is_near_resolution(self._raw(48.0)) is False

    def test_false_when_already_started(self):
        # Negative hours (already started) is handled by event-started
        # reduce-only instead -- near-resolution deliberately doesn't also
        # fire, since it would needlessly suppress the reducing leg too.
        settings = config.LiveTradingSettings(near_resolution_hours_threshold=24.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        assert maker._is_near_resolution(self._raw(-1.0)) is False


def test_refresh_quotes_blocks_orphaned_position_via_extra_raw_by_slug_once_event_started(
    monkeypatch, isolated_ledger,
):
    # The orphaned position's market isn't in this cycle's ranked candidates
    # at all (e.g. it fell out of eligibility once the event started) --
    # its raw data only reaches the gating logic via extra_raw_by_slug (the
    # cross-cutting fix), which is what ws_runner.py threads through in
    # production from its broader pre-filter scan.
    client = _client()
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
        max_started_event_hours=0.0,
    )
    slug = "orphaned-started-market"
    client.get_all_positions.return_value = {
        slug: {"netPositionDecimal": "10", "cost": {"value": "5.0"}, "cashValue": {"value": "5.0"}},
    }
    # MarketMaker fetches its own per-market position separately (not from
    # the get_all_positions() dict above) -- must reflect the same long
    # position or the reducing/increasing split below won't match.
    client.get_position.return_value = {
        "netPositionDecimal": "10", "cost": {"value": "5.0"}, "cashValue": {"value": "5.0"},
    }
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [],
    )
    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    started_raw = {"gameStartTime": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}

    cycles = maker.refresh_quotes(candidates=[], extra_raw_by_slug={slug: started_raw})

    assert len(cycles) == 1
    # Long position -- SELL is reducing (posts normally), BUY is increasing
    # (blocked: the event already started).
    assert cycles[0].ask.order_id is not None
    assert cycles[0].bid.order_id is None
    assert "event already started" in cycles[0].bid.error


def test_refresh_quotes_gives_different_effective_settings_per_market_cooldown_state(monkeypatch, isolated_ledger):
    client = _client()
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
        toxicity_min_edge_multiplier=2.0, toxicity_size_multiplier=0.5,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1"), _scored("m2")],
    )
    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    maker.toxicity_tracker.record_markout("m1", -100.0)  # push m1 into cooldown; m2 stays clean
    assert maker.toxicity_tracker.is_in_cooldown("m1") is True
    assert maker.toxicity_tracker.is_in_cooldown("m2") is False

    constructed = []

    def _spy(*args, **kwargs):
        constructed.append(kwargs)
        return MarketMaker(*args, **kwargs)

    monkeypatch.setattr("polymarket_bot.live.multi_market_maker.MarketMaker", _spy)

    maker.refresh_quotes(candidates=[_scored("m1"), _scored("m2")])

    by_slug = {kwargs["market_slug"]: kwargs for kwargs in constructed}
    assert by_slug["m1"]["reduce_only_reason"] == "toxicity cooldown"
    assert by_slug["m1"]["settings"].order_shares_min == pytest.approx(8.0)
    assert by_slug["m2"]["reduce_only_reason"] is None
    assert by_slug["m2"]["settings"].order_shares_min == pytest.approx(16.0)


def test_toxicity_tracking_disabled_is_a_full_kill_switch(monkeypatch, isolated_ledger):
    client = _client()
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
        toxicity_tracking_enabled=False,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )
    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    maker.toxicity_tracker.record_markout("m1", -100.0)  # would normally trigger cooldown
    assert maker.toxicity_tracker.is_in_cooldown("m1") is True  # tracker itself is unaffected

    cycles = maker.refresh_quotes(candidates=[_scored("m1")])

    # But the kill switch means the market is quoted at full size, both legs.
    assert cycles[0].bid.size == pytest.approx(16.0)
    assert cycles[0].bid.order_id is not None


def _scored_in_bucket(market_id: str, bucket_suffix: str = "ev-2026-01-01") -> ScoredMarket:
    # "typ-" + bucket_suffix -- both slugs bucketing to "ev-2026-01-01" via
    # derive_event_bucket_key's slug heuristic (drop "typ", date triplet
    # 2026-01-01 found).
    market = Market(
        market_id=f"typ-{bucket_suffix}-{market_id}",
        event_id=f"e-{market_id}",
        question=f"Will {market_id} happen?",
        category="sports",
        token_ids=[f"t-{market_id}"],
        spread=0.04,
        raw={"orderPriceMinTickSize": 0.01, "marketType": "props"},
    )
    return ScoredMarket(
        market_id=market.market_id,
        question=market.question,
        total_score=90.0,
        component_scores={},
        explanation=[],
        recommendation="PAPER_CANDIDATE",
        market=market,
    )


class TestEventExposureEnforcement:
    def test_zero_position_market_blocked_when_its_bucket_is_over_cap_via_siblings(
        self, monkeypatch, isolated_ledger,
    ):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
            max_event_exposure_pct=0.20,
        )
        # An existing position, in the SAME bucket, already over the 20% cap
        # (25% of the $100 capital reference) -- but NOT itself a candidate
        # this cycle, so it's a pure sibling-exposure signal.
        client.get_all_positions.return_value = {
            "typ-ev-2026-01-01-existing": {
                "netPositionDecimal": "10", "cost": {"value": "25.0"}, "cashValue": {"value": "25.0"},
            },
        }
        new_candidate = _scored_in_bucket("new")
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: [new_candidate],
        )
        maker = MultiMarketMaker(
            client=client, settings=settings, read_client=read_client,
            equity_protection_settings=config.EquityProtectionSettings(starting_capital_usd=100.0),
        )

        cycles = maker.refresh_quotes(candidates=[new_candidate])

        # Flat position + reduce-only -> both legs blocked -> no orders, no cycle.
        assert cycles == []
        client.create_order.assert_not_called()

    def test_market_with_its_own_over_cap_position_is_reduce_only_not_blocked_both_ways(
        self, monkeypatch, isolated_ledger,
    ):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
            max_event_exposure_pct=0.20,
        )
        scored = _scored_in_bucket("long")
        slug = scored.market.market_id
        client.get_all_positions.return_value = {
            slug: {"netPositionDecimal": "10", "cost": {"value": "25.0"}, "cashValue": {"value": "25.0"}},
        }
        client.get_position.return_value = {
            "netPositionDecimal": "10", "cost": {"value": "2.5"}, "cashValue": {"value": "2.5"},
        }
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: [scored],
        )
        maker = MultiMarketMaker(
            client=client, settings=settings, read_client=read_client,
            equity_protection_settings=config.EquityProtectionSettings(starting_capital_usd=100.0),
        )

        cycles = maker.refresh_quotes(candidates=[scored])

        # Long position -- SELL is reducing (posts normally), BUY is
        # increasing (blocked by the event cap).
        assert cycles[0].ask.order_id is not None
        assert cycles[0].bid.order_id is None
        assert "event exposure" in cycles[0].bid.error

    def test_combined_reason_when_toxicity_and_event_cap_both_apply(self, monkeypatch, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
            max_event_exposure_pct=0.20,
        )
        scored = _scored_in_bucket("both")
        slug = scored.market.market_id
        client.get_all_positions.return_value = {
            slug: {"netPositionDecimal": "10", "cost": {"value": "25.0"}, "cashValue": {"value": "25.0"}},
        }
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: [scored],
        )
        maker = MultiMarketMaker(
            client=client, settings=settings, read_client=read_client,
            equity_protection_settings=config.EquityProtectionSettings(starting_capital_usd=100.0),
        )
        maker.toxicity_tracker.record_markout(slug, -100.0)

        exposure_by_bucket = {
            e.bucket_key: e
            for e in compute_event_exposures(client.get_all_positions.return_value, 100.0)
        }
        in_cooldown, reason, _warn = maker._event_and_toxicity_gating(
            slug, scored.market.raw, exposure_by_bucket,
        )
        assert in_cooldown is True
        assert reason == "toxicity cooldown + event exposure at or above cap"

    def test_warn_tier_widens_edge_and_shrinks_size_without_reduce_only(self, monkeypatch, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
            max_event_exposure_pct=0.20, warn_event_exposure_pct=0.10,
            event_exposure_warn_edge_multiplier=1.25, event_exposure_warn_size_multiplier=0.75,
        )
        # 12% of $100 capital -- above the 10% warn threshold, below the 20% cap.
        client.get_all_positions.return_value = {
            "typ-ev-2026-01-01-existing": {
                "netPositionDecimal": "10", "cost": {"value": "12.0"}, "cashValue": {"value": "12.0"},
            },
        }
        new_candidate = _scored_in_bucket("warn")
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: [new_candidate],
        )
        maker = MultiMarketMaker(
            client=client, settings=settings, read_client=read_client,
            equity_protection_settings=config.EquityProtectionSettings(starting_capital_usd=100.0),
        )

        cycles = maker.refresh_quotes(candidates=[new_candidate])

        # Not blocked (below hard cap) -- both legs post, at reduced size.
        assert cycles[0].bid.order_id is not None
        assert cycles[0].ask.order_id is not None
        assert cycles[0].bid.size == pytest.approx(12.0)  # 16.0 * 0.75

    def test_capital_reference_falls_back_to_deployed_cost_basis_when_unconfigured(
        self, monkeypatch, isolated_ledger,
    ):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
            max_event_exposure_pct=0.20,
        )
        # No starting_capital_usd configured -- capital reference falls back
        # to deployed cost basis: 20.0 total, existing position IS 100% of
        # it (well above any cap), so a same-bucket candidate is blocked.
        client.get_all_positions.return_value = {
            "typ-ev-2026-01-01-existing": {
                "netPositionDecimal": "10", "cost": {"value": "20.0"}, "cashValue": {"value": "20.0"},
            },
        }
        new_candidate = _scored_in_bucket("fallback")
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: [new_candidate],
        )
        maker = MultiMarketMaker(
            client=client, settings=settings, read_client=read_client,
            equity_protection_settings=config.EquityProtectionSettings(starting_capital_usd=0.0),
        )

        cycles = maker.refresh_quotes(candidates=[new_candidate])

        assert cycles == []
        client.create_order.assert_not_called()
