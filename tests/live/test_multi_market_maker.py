import dataclasses
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import requests

from polymarket_bot import config
from polymarket_bot.live import fills as fills_module
from polymarket_bot.live import ledger as ledger_module
from polymarket_bot.live import settlements as settlements_module
from polymarket_bot.live.event_exposure import compute_event_exposures
from polymarket_bot.live.ledger import record_cycle
from polymarket_bot.live.market_maker import EmergencySafeguardFailedError, MarketMaker
from polymarket_bot.live.models import FillRecord, LiveQuoteCycle, PostedLeg
from polymarket_bot.live.multi_market_maker import MultiMarketMaker, _count_placed_orders
from polymarket_bot.live.us_client import UsApiError
from polymarket_bot.live.ws_private import PrivateStateStore
from polymarket_bot.models import Market, ScoredMarket


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, "LEDGER_FILE", tmp_path / "orders.json")


@pytest.fixture(autouse=True)
def _isolated_pnl_attribution_files(tmp_path, monkeypatch):
    # refresh_quotes() now unconditionally reads fills.json and reads/
    # writes position_snapshots.json/settlements.json (P&L attribution --
    # see live/settlements.py) whenever positions are known. Must never
    # touch the real project's data/live_trades/ files from a test run --
    # same real-contamination risk this codebase already hit once with
    # bot.log (RUNBOOK "-11." section) and fills.json (ws_runner tests).
    monkeypatch.setattr(fills_module, "FILLS_FILE", tmp_path / "fills.json")
    monkeypatch.setattr(fills_module, "BACKFILL_RESOLVED_FILE", tmp_path / "backfill_resolved_orders.json")
    monkeypatch.setattr(settlements_module, "POSITION_SNAPSHOTS_FILE", tmp_path / "position_snapshots.json")
    monkeypatch.setattr(settlements_module, "SETTLEMENTS_FILE", tmp_path / "settlements.json")
    # Existing tests isolate pre-existing behavior. Production enables the
    # stricter controls through .env; dedicated regression tests opt in.
    monkeypatch.setenv("LIVE_MAX_PAYOFF_LOSS_TO_CAPTURE_RATIO", "30")
    monkeypatch.setenv("LIVE_REQUIRE_BOTH_ENTRY_LEGS", "false")
    monkeypatch.setenv("LIVE_FLAT_FIRST_INVENTORY_ENABLED", "false")
    monkeypatch.setenv("LIVE_HARD_FLATTEN_MINUTES_BEFORE_EVENT", "0")
    monkeypatch.setenv("LIVE_HARD_FLATTEN_ON_MAX_HOLDING_ENABLED", "false")
    monkeypatch.setenv("LIVE_ORDER_SETTLEMENT_SECONDS", "0")
    monkeypatch.setenv("LIVE_ONE_ROUND_TRIP_PER_MARKET_PER_SESSION", "false")


def _api_error(status_code: int, message: str = "rejected") -> UsApiError:
    """Matches the real shape LiveUsClient._request() produces: UsApiError
    with the original requests exception preserved as __cause__, so its
    response status is introspectable via is_client_rejection()."""
    response = Mock(status_code=status_code)
    cause = requests.exceptions.HTTPError(message, response=response)
    exc = UsApiError(f"POST /v1/orders failed: {cause}")
    exc.__cause__ = cause
    return exc


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
    # gameStartTime a safe 3 days out -- clear of both "today" (the new
    # same-day/unknown-timing reduce-only check) and the 24h near-resolution
    # window, so tests not specifically exercising timing behavior aren't
    # incidentally reduce-only'd or edge-widened.
    safe_future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    market = Market(
        market_id=market_id,
        event_id=f"e-{market_id}",
        question=f"Will {market_id} happen?",
        category="politics",
        token_ids=[f"t-{market_id}"],
        spread=0.04,
        raw={"orderPriceMinTickSize": 0.01, "gameStartTime": safe_future},
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


def test_held_candidate_is_prioritized_over_flat_candidate_for_order_budget(
    isolated_ledger,
):
    client = _client()
    read_client = _read_client()
    flat = _scored("flat-first-ranked")
    held = _scored("held-second-ranked")
    client.get_all_positions.return_value = {
        held.market.market_id: {
            "netPositionDecimal": "5",
            "cost": {"value": "2.5"},
            "cashValue": {"value": "2.5"},
        }
    }
    settings = config.LiveTradingSettings(
        order_shares_min=5.0,
        order_shares_max=5.0,
        max_orders_per_cycle=1,
        max_payoff_loss_to_capture_ratio=30.0,
        flat_first_inventory_enabled=True,
        hard_flatten_minutes_before_event=0.0,
    )
    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

    cycles = maker.refresh_quotes(candidates=[flat, held])

    assert len(cycles) == 1
    assert cycles[0].market_id == held.market.market_id
    assert cycles[0].ask.order_id is not None
    assert cycles[0].bid.order_id is None


def test_hard_flatten_candidate_bypasses_thin_depth_and_cost_floor(
    isolated_ledger,
):
    client = _client()
    read_client = _read_client()
    candidate = _scored("must-flatten")
    candidate.market.raw["gameStartTime"] = (
        datetime.now(timezone.utc) + timedelta(minutes=60)
    ).isoformat()
    client.get_all_positions.return_value = {
        candidate.market.market_id: {
            "netPositionDecimal": "5",
            "cost": {"value": "2.5"},
            "cashValue": {"value": "0.3"},
        }
    }
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.06, "quantity": 1.0}],
        "asks": [{"price": 0.37, "quantity": 1.0}],
    }
    settings = config.LiveTradingSettings(
        order_shares_min=5.0,
        order_shares_max=5.0,
        max_orders_per_cycle=4,
        max_payoff_loss_to_capture_ratio=30.0,
        flat_first_inventory_enabled=True,
        hard_flatten_minutes_before_event=90.0,
    )
    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

    cycles = maker.refresh_quotes(candidates=[candidate])

    assert len(cycles) == 1
    assert cycles[0].ask.price == pytest.approx(0.06)
    assert cycles[0].ask.order_id is not None
    assert cycles[0].bid.order_id is None


def test_order_settlement_barrier_requires_rest_reconciliation_before_requote(
    isolated_ledger,
):
    client = _client()
    read_client = _read_client()
    candidate = _scored("settlement-race")
    client.get_all_positions.return_value = {}
    settings = config.LiveTradingSettings(
        order_shares_min=5.0,
        order_shares_max=5.0,
        max_orders_per_cycle=4,
        max_payoff_loss_to_capture_ratio=30.0,
        require_both_entry_legs=True,
        flat_first_inventory_enabled=True,
        hard_flatten_minutes_before_event=0.0,
        hard_flatten_on_max_holding_enabled=False,
        order_settlement_seconds=15.0,
        one_round_trip_per_market_per_session=True,
    )
    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

    first = maker.refresh_quotes(candidates=[candidate])
    assert len(first) == 1
    assert client.create_order.call_count == 2

    # Even though the supplied account snapshots still look flat, no second
    # pair may be submitted while an earlier order's effects can be in flight.
    second = maker.refresh_quotes(candidates=[candidate])
    assert second == []
    assert client.create_order.call_count == 2

    # At the barrier boundary, authoritative REST now reports that one side
    # filled. The next action is only the reducing SELL -- never another pair.
    maker._settlement_not_before[candidate.market.market_id] = 0.0
    client.get_all_positions.return_value = {
        candidate.market.market_id: {
            "netPositionDecimal": "5",
            "cost": {"value": "2.5"},
            "cashValue": {"value": "2.5"},
        }
    }
    third = maker.refresh_quotes(candidates=[candidate])

    assert len(third) == 1
    assert client.create_order.call_count == 3
    assert client.create_order.call_args.kwargs["action"] == "ORDER_ACTION_SELL"

    # Once the reducing fill is observed and REST confirms flat, this market
    # is done for the session instead of immediately starting another churn
    # cycle in the same adverse book.
    maker.note_fills({candidate.market.market_id})
    client.get_all_positions.return_value = {}
    fourth = maker.refresh_quotes(candidates=[candidate])

    assert fourth == []
    assert client.create_order.call_count == 3


def test_new_short_transition_is_not_immediately_force_flattened_as_unknown_age(
    isolated_ledger,
):
    """Regression for the exact 2026-07-13/14 live loss mechanism.

    A passive SELL from flat is reported by the exchange as SELL_LONG, an
    intent lot accounting deliberately may classify as an untracked close.
    Authoritative flat->short account state still proves this is fresh
    in-session inventory, so max-holding liquidation must not fire.
    """
    client = _client()
    read_client = _read_client()
    candidate = _scored("fresh-short")
    client.get_all_positions.return_value = {}
    settings = config.LiveTradingSettings(
        order_shares_min=5.0,
        order_shares_max=5.0,
        max_orders_per_cycle=4,
        max_payoff_loss_to_capture_ratio=30.0,
        require_both_entry_legs=True,
        flat_first_inventory_enabled=True,
        hard_flatten_minutes_before_event=0.0,
        hard_flatten_on_max_holding_enabled=True,
        liquidation_max_holding_hours=1.0,
        order_settlement_seconds=15.0,
    )
    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

    first = maker.refresh_quotes(candidates=[candidate])
    assert len(first) == 1
    assert client.create_order.call_count == 2

    # Simulate the passive SELL filling and authoritative REST reporting the
    # new short when the submission barrier expires.
    maker._settlement_not_before[candidate.market.market_id] = 0.0
    client.get_all_positions.return_value = {
        candidate.market.market_id: {
            "netPositionDecimal": "-5",
            "cost": {"value": "2.5"},
            "cashValue": {"value": "2.5"},
        }
    }
    second = maker.refresh_quotes(candidates=[candidate])

    assert len(second) == 1
    assert client.create_order.call_count == 3
    reducing_call = client.create_order.call_args
    assert reducing_call.kwargs["action"] == "ORDER_ACTION_BUY"
    assert reducing_call.kwargs["tif"] == "TIME_IN_FORCE_GOOD_TILL_CANCEL"


def test_max_holding_deadline_forces_marketable_flatten(
    isolated_ledger,
):
    client = _client()
    read_client = _read_client()
    candidate = _scored("aged-position")
    candidate.market.raw["gameStartTime"] = (
        datetime.now(timezone.utc) + timedelta(days=3)
    ).isoformat()
    client.get_all_positions.return_value = {
        candidate.market.market_id: {
            "netPositionDecimal": "5",
            "cost": {"value": "2.5"},
            "cashValue": {"value": "0.3"},
        }
    }
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.06, "quantity": 1.0}],
        "asks": [{"price": 0.37, "quantity": 1.0}],
    }
    settings = config.LiveTradingSettings(
        order_shares_min=5.0,
        order_shares_max=5.0,
        max_orders_per_cycle=4,
        max_payoff_loss_to_capture_ratio=30.0,
        flat_first_inventory_enabled=True,
        hard_flatten_minutes_before_event=0.0,
        hard_flatten_on_max_holding_enabled=True,
        liquidation_max_holding_hours=1.0,
    )
    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

    cycles = maker.refresh_quotes(candidates=[candidate])

    assert len(cycles) == 1
    assert cycles[0].ask.price == pytest.approx(0.06)
    assert cycles[0].ask.order_id is not None
    assert cycles[0].bid.order_id is None
    assert client.create_order.call_args.kwargs["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"


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


def test_private_state_bootstraps_once_then_avoids_rest_polling(isolated_ledger):
    client = _client()
    settings = config.LiveTradingSettings(
        max_orders_per_cycle=0,
        private_order_state_enabled=True,
        private_state_reconcile_seconds=300.0,
    )
    store = PrivateStateStore()
    store.mark_connected()
    maker = MultiMarketMaker(
        client=client, settings=settings, read_client=_read_client(), private_store=store,
    )

    maker.refresh_quotes(candidates=[])
    maker.refresh_quotes(candidates=[])

    assert client.get_open_orders.call_count == 1
    assert client.get_all_positions.call_count == 1


def test_fast_ws_reconnect_between_cycles_still_forces_one_rest_reconciliation(
    isolated_ledger,
):
    # A private-WS disconnect+reconnect that resolves faster than the bot's
    # own refresh cadence is never observed as an unhealthy sample --
    # is_healthy() reports True again the instant the socket reopens (see
    # PrivateStateStore.mark_connected()'s docstring), so neither
    # "not ws_healthy" nor the periodic reconcile timer would have fired.
    # Without tracking the reconnect transition itself, stale pre-disconnect
    # state could be trusted for up to private_state_reconcile_seconds
    # longer even though the exchange gives no guarantee it replayed
    # whatever deltas were missed while disconnected.
    client = _client()
    settings = config.LiveTradingSettings(
        max_orders_per_cycle=0,
        private_order_state_enabled=True,
        private_state_reconcile_seconds=300.0,
    )
    store = PrivateStateStore()
    store.mark_connected()
    maker = MultiMarketMaker(
        client=client, settings=settings, read_client=_read_client(), private_store=store,
    )
    maker.refresh_quotes(candidates=[])  # bootstrap -- one REST fetch each
    assert client.get_open_orders.call_count == 1
    assert client.get_all_positions.call_count == 1

    # Simulate a disconnect+fast-reconnect entirely between two cycles --
    # nothing else here (periodic timer, ws_healthy) would force a re-check.
    store.mark_disconnected()
    store.mark_connected()

    maker.refresh_quotes(candidates=[])

    assert client.get_open_orders.call_count == 2
    assert client.get_all_positions.call_count == 2

    # The reconnect was confirmed by that fresh REST fetch -- a third cycle
    # with no further reconnect must not force another one.
    maker.refresh_quotes(candidates=[])

    assert client.get_open_orders.call_count == 2
    assert client.get_all_positions.call_count == 2


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
    # A resolvable, safely-future gameStartTime for the orphan -- otherwise
    # it has zero raw data and the same-day/unknown-timing reduce-only check
    # would block its increasing leg, letting it consume only half the
    # budget instead of the full 2 this test is actually about (order-budget
    # priority, not timing gating).
    safe_future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    cycles = maker.refresh_quotes(extra_raw_by_slug={"orphan-slug": {"gameStartTime": safe_future}})

    # An untracked-age orphan is conservatively reduce-only. Its reducing
    # order goes first; the one remaining slot may be used by the candidate.
    assert len(cycles) == 2
    assert cycles[0].market_id == "orphan-slug"
    assert cycles[0].bid.order_id is None
    assert cycles[0].ask.order_id is not None
    assert cycles[1].market_id == "m1"


def test_force_flatten_orphaned_position_outranks_other_orphaned_positions_for_budget(
    monkeypatch, isolated_ledger
):
    # A position past its hard-flatten deadline is a mandatory-liquidation
    # task, not a routine re-quote -- it must win the shared order budget
    # over every other orphaned position, regardless of the arbitrary order
    # positions.items() iterates in. "calm-slug" is listed FIRST in the
    # positions dict specifically so a pre-fix run (orphaned_slugs processed
    # in raw iteration order, no force_flatten-aware sort) would consume the
    # only budget slot on the calm slug first and starve the urgent one.
    monkeypatch.setenv("LIVE_HARD_FLATTEN_MINUTES_BEFORE_EVENT", "60")
    client = _client(
        get_all_positions=Mock(return_value={
            "calm-slug": {"netPositionDecimal": "5.0", "cost": {"value": "2.0"}, "cashValue": {"value": "2.0"}},
            "urgent-slug": {"netPositionDecimal": "5.0", "cost": {"value": "2.0"}, "cashValue": {"value": "2.0"}},
        }),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=1.0, order_shares_max=20.0, max_orders_per_cycle=1,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [],
    )

    safe_future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()  # inside the 60-minute deadline

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes(extra_raw_by_slug={
        "calm-slug": {"gameStartTime": safe_future},
        "urgent-slug": {"gameStartTime": soon},
    })

    assert len(cycles) == 1
    assert cycles[0].market_id == "urgent-slug"


def test_force_flatten_candidate_outranks_other_held_candidates_for_budget(
    monkeypatch, isolated_ledger
):
    # Same priority requirement as the orphaned-loop test above, but for a
    # held position that's still among this cycle's ranked candidates
    # rather than orphaned out of them. "calm-mkt" is ranked ahead of
    # "urgent-mkt" specifically so a pre-fix run (candidates sorted only by
    # held-vs-flat, not by force_flatten) would consume the only budget slot
    # on the calm one first and starve the one past its flatten deadline.
    monkeypatch.setenv("LIVE_HARD_FLATTEN_MINUTES_BEFORE_EVENT", "60")
    calm = _scored("calm-mkt")
    soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()  # inside the 60-minute deadline
    urgent = _scored("urgent-mkt")
    urgent = dataclasses.replace(
        urgent, market=dataclasses.replace(urgent.market, raw={"orderPriceMinTickSize": 0.01, "gameStartTime": soon})
    )
    client = _client(
        get_all_positions=Mock(return_value={
            "calm-mkt": {"netPositionDecimal": "5.0", "cost": {"value": "2.0"}, "cashValue": {"value": "2.0"}},
            "urgent-mkt": {"netPositionDecimal": "5.0", "cost": {"value": "2.0"}, "cashValue": {"value": "2.0"}},
        }),
    )
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=1.0, order_shares_max=20.0, max_orders_per_cycle=1,
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [calm, urgent],
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    cycles = maker.refresh_quotes()

    assert len(cycles) == 1
    assert cycles[0].market_id == "urgent-mkt"


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
    # Resolvable, safely-future gameStartTime -- see the identical comment
    # in test_multi_market_maker_orphaned_positions_take_priority_over_new_candidates_for_budget.
    safe_future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    cycles = maker.refresh_quotes(extra_raw_by_slug={"orphan-slug": {"gameStartTime": safe_future}})

    assert len(cycles) == 2
    assert cycles[0].market_id == "orphan-slug"
    assert cycles[1].market_id == "m1"
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
# Issue 2: an unknown position list must disable the whole quote cycle
# ---------------------------------------------------------------------

def test_multi_market_maker_fails_closed_when_positions_unknown(monkeypatch, isolated_ledger, caplog):
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
    with caplog.at_level("ERROR"):
        cycles = maker.refresh_quotes()

    client.cancel_order.assert_not_called()
    client.create_order.assert_not_called()
    assert cycles == []
    assert any("hidden inventory" in r.message for r in caplog.records)


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


def test_multi_market_maker_propagates_emergency_safeguard_failure(monkeypatch, isolated_ledger):
    # Unlike an ordinary crash (the test above), an EmergencySafeguardFailedError
    # means the account-wide emergency cancel-all could not be confirmed to
    # have actually left this market clean -- unconfirmed live exposure
    # after the LAST line of defense. This must NOT be swallowed and turned
    # into "no cycle this turn" like a routine per-market failure; it has to
    # propagate out of MultiMarketMaker.refresh_quotes() itself so it can
    # reach run_forever()'s top-level handler and stop the process.
    client = _client()
    read_client = _read_client()
    settings = config.LiveTradingSettings(order_shares_min=15.0, order_shares_max=20.0)
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [_scored("m1")],
    )
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.MarketMaker.refresh_quotes",
        Mock(side_effect=EmergencySafeguardFailedError("could not confirm a clean slate")),
    )

    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    with pytest.raises(EmergencySafeguardFailedError):
        maker.refresh_quotes()


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

    def test_widens_edge_without_shrinking_order_shares_when_in_cooldown(self):
        settings = config.LiveTradingSettings(
            min_edge_cents=0.5, order_shares_min=15.0, order_shares_max=20.0,
            toxicity_min_edge_multiplier=2.0, toxicity_size_multiplier=0.5,
        )
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        effective = maker._effective_settings_for(settings, in_cooldown=True)
        assert effective.min_edge_cents == pytest.approx(1.0)
        # order_shares is no longer touched here -- see
        # _increasing_order_shares_for, which discounts only the increasing
        # side once net_position is known, never a shared reducing/exit size.
        assert effective.order_shares_min == pytest.approx(15.0)
        assert effective.order_shares_max == pytest.approx(20.0)

    def test_composes_on_top_of_existing_settings_override(self):
        settings = config.LiveTradingSettings(
            min_edge_cents=0.5, order_shares_min=15.0, order_shares_max=20.0,
            toxicity_min_edge_multiplier=2.0, toxicity_size_multiplier=0.5,
        )
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        # An equity-protection-style override already halved size once.
        equity_override = dataclasses.replace(settings, order_shares_min=7.5, order_shares_max=10.0)
        effective = maker._effective_settings_for(equity_override, in_cooldown=True)
        # order_shares stays exactly what the override already set --
        # toxicity no longer shrinks it here at all.
        assert effective.order_shares_min == pytest.approx(7.5)
        assert effective.order_shares_max == pytest.approx(10.0)
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
        # Both multipliers apply to min_edge_cents (0.5 * 2.0 * 2.0); neither
        # touches order_shares_min/max here anymore.
        assert effective.min_edge_cents == pytest.approx(2.0)
        assert effective.order_shares_min == pytest.approx(15.0)
        assert effective.order_shares_max == pytest.approx(20.0)


class TestIncreasingOrderSharesFor:
    def test_none_when_neither_condition_active(self):
        settings = config.LiveTradingSettings(order_shares_min=15.0, order_shares_max=20.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        assert maker._increasing_order_shares_for(
            settings, in_cooldown=False, event_exposure_warn=False,
        ) is None

    def test_discounts_for_toxicity_cooldown(self):
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=20.0, toxicity_size_multiplier=0.5,
        )
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        result = maker._increasing_order_shares_for(
            settings, in_cooldown=True, event_exposure_warn=False,
        )
        assert result == pytest.approx(8.75)  # (15 + 20) / 2 * 0.5

    def test_discounts_for_event_exposure_warn(self):
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=20.0,
            event_exposure_warn_size_multiplier=0.5,
        )
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        result = maker._increasing_order_shares_for(
            settings, in_cooldown=False, event_exposure_warn=True,
        )
        assert result == pytest.approx(8.75)  # (15 + 20) / 2 * 0.5

    def test_stacks_toxicity_and_event_warn(self):
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=20.0,
            toxicity_size_multiplier=0.5, event_exposure_warn_size_multiplier=0.5,
        )
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        result = maker._increasing_order_shares_for(
            settings, in_cooldown=True, event_exposure_warn=True,
        )
        assert result == pytest.approx(4.375)  # 17.5 * 0.5 * 0.5

    def test_reads_from_base_settings_not_self_settings(self):
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=20.0, toxicity_size_multiplier=0.5,
        )
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        # An equity-protection-style override already halved size once.
        equity_override = dataclasses.replace(settings, order_shares_min=7.5, order_shares_max=10.0)
        result = maker._increasing_order_shares_for(
            equity_override, in_cooldown=True, event_exposure_warn=False,
        )
        assert result == pytest.approx(4.375)  # (7.5 + 10) / 2 * 0.5


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
        # -30h guarantees crossing at least one UTC midnight boundary
        # regardless of when this test runs, isolating "event already
        # started" from the new same-day/unknown-timing check below.
        settings = config.LiveTradingSettings(max_started_event_hours=0.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        _in_cooldown, reason, _warn, _over_cap = maker._event_and_toxicity_gating(
            "some-slug", self._raw(-30.0), {},
        )
        assert reason == "event already started"

    def test_reduce_only_reason_none_when_not_started_and_no_other_trigger(self):
        # 48h out is both not-started and not same-day, so this genuinely
        # has no trigger at all.
        settings = config.LiveTradingSettings(max_started_event_hours=0.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        _in_cooldown, reason, _warn, _over_cap = maker._event_and_toxicity_gating(
            "some-slug", self._raw(48.0), {},
        )
        assert reason is None


class TestExcludedMarketFamilyGating:
    def _settings(self, **overrides):
        defaults = dict(
            exclude_categories=("climate",),
            exclude_question_keywords=("temperature",),
            exclude_slug_prefixes=("tc-temp",),
        )
        defaults.update(overrides)
        return config.LiveTradingSettings(**defaults)

    def _safe_future(self):
        # 3 days out -- clear of same-day/unknown-timing so these tests
        # isolate the excluded-market-family mechanism specifically.
        return (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()

    def test_reduce_only_reason_includes_excluded_market_family_via_category(self):
        maker = MultiMarketMaker(client=_client(), settings=self._settings(), read_client=_read_client())
        _in_cooldown, reason, _warn, _over_cap = maker._event_and_toxicity_gating(
            "some-weather-slug", {"category": "climate", "gameStartTime": self._safe_future()}, {},
        )
        assert reason == "excluded market family"

    def test_reduce_only_reason_includes_excluded_market_family_via_slug_prefix(self):
        # raw=None deliberately -- proves slug-prefix exclusion works with
        # zero raw data. Since raw is also missing entirely, the new
        # same-day/unknown-timing check (fail-closed on no data) correctly
        # fires too -- both reasons are expected here, not a conflict.
        maker = MultiMarketMaker(client=_client(), settings=self._settings(), read_client=_read_client())
        _in_cooldown, reason, _warn, _over_cap = maker._event_and_toxicity_gating(
            "tc-temp-laxhigh-2026-07-07-gte73lt74f", None, {},
        )
        assert reason == "excluded market family + event start imminent or unknown timing"

    def test_reduce_only_reason_none_when_not_excluded(self):
        maker = MultiMarketMaker(client=_client(), settings=self._settings(), read_client=_read_client())
        _in_cooldown, reason, _warn, _over_cap = maker._event_and_toxicity_gating(
            "some-other-slug", {"category": "sports", "gameStartTime": self._safe_future()}, {},
        )
        assert reason is None

    def test_combined_reason_with_event_started(self):
        settings = self._settings(max_started_event_hours=0.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        started = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _in_cooldown, reason, _warn, _over_cap = maker._event_and_toxicity_gating(
            "tc-temp-laxhigh-2026-07-07-gte73lt74f", {"gameStartTime": started}, {},
        )
        # started is necessarily today too, so the new same-day check
        # legitimately also fires alongside the other two.
        assert reason == "event already started + excluded market family"


def test_refresh_quotes_blocks_orphaned_position_in_excluded_market_family_via_extra_raw_by_slug(
    monkeypatch, isolated_ledger,
):
    # Mirrors test_refresh_quotes_blocks_orphaned_position_via_extra_raw_by_slug_once_event_started
    # -- an orphaned weather-market position, reached only via extra_raw_by_slug
    # (not this cycle's ranked candidates), gets reduce-only'd.
    client = _client()
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
        exclude_categories=("climate",), exclude_question_keywords=("temperature",),
        exclude_slug_prefixes=("tc-temp",),
    )
    slug = "tc-temp-laxhigh-2026-07-07-gte73lt74f"
    client.get_all_positions.return_value = {
        slug: {"netPositionDecimal": "10", "cost": {"value": "5.0"}, "cashValue": {"value": "5.0"}},
    }
    client.get_position.return_value = {
        "netPositionDecimal": "10", "cost": {"value": "5.0"}, "cashValue": {"value": "5.0"},
    }
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [],
    )
    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    weather_raw = {"category": "climate", "question": "Highest temperature in Los Angeles on July 7?"}

    cycles = maker.refresh_quotes(candidates=[], extra_raw_by_slug={slug: weather_raw})

    assert len(cycles) == 1
    assert cycles[0].ask.order_id is not None
    assert cycles[0].bid.order_id is None
    assert "excluded market family" in cycles[0].bid.error


class TestEsportsExclusionGating:
    def _settings(self, **overrides):
        defaults = dict(
            exclude_categories=(), exclude_question_keywords=(), exclude_slug_prefixes=(),
            exclude_sports_market_types=("esports_match_winner",),
        )
        defaults.update(overrides)
        return config.LiveTradingSettings(**defaults)

    def _safe_future(self):
        return (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()

    def test_reduce_only_reason_includes_excluded_market_family_for_esports(self):
        maker = MultiMarketMaker(client=_client(), settings=self._settings(), read_client=_read_client())
        raw = {
            "category": "sports", "marketType": "drawable_outcome",
            "sportsMarketType": "esports_match_winner", "gameStartTime": self._safe_future(),
        }
        _in_cooldown, reason, _warn, _over_cap = maker._event_and_toxicity_gating(
            "atc-dota2-liquid-lvlup-2026-07-08-liquid", raw, {},
        )
        assert reason == "excluded market family"

    def test_reduce_only_reason_none_for_traditional_sports_with_same_atc_prefix(self):
        # Same "atc-" org-code prefix as the Dota example above, but a real
        # traditional-sports sportsMarketType -- proves exclusion comes from
        # sportsMarketType specifically, not the shared slug prefix.
        maker = MultiMarketMaker(client=_client(), settings=self._settings(), read_client=_read_client())
        raw = {
            "category": "sports", "marketType": "moneyline",
            "sportsMarketType": "soccer_team_full_time_winner", "gameStartTime": self._safe_future(),
        }
        _in_cooldown, reason, _warn, _over_cap = maker._event_and_toxicity_gating(
            "atc-fwc-fra-mar-2026-07-09-fra", raw, {},
        )
        assert reason is None


def test_refresh_quotes_blocks_orphaned_position_in_esports_market_via_extra_raw_by_slug(
    monkeypatch, isolated_ledger,
):
    # Mirrors the weather orphaned-position integration test above. Uses
    # "aec-lol-" -- one of the confirmed non-exclusive esports slug prefixes
    # (also used by traditional sports like tennis/MMA) -- to prove the
    # exclusion comes from sportsMarketType, not the slug.
    client = _client()
    read_client = _read_client()
    settings = config.LiveTradingSettings(
        order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
        exclude_categories=(), exclude_question_keywords=(), exclude_slug_prefixes=(),
        exclude_sports_market_types=("esports_match_winner",),
    )
    slug = "aec-lol-teamA-teamB-2026-07-08-teamA"
    client.get_all_positions.return_value = {
        slug: {"netPositionDecimal": "10", "cost": {"value": "5.0"}, "cashValue": {"value": "5.0"}},
    }
    client.get_position.return_value = {
        "netPositionDecimal": "10", "cost": {"value": "5.0"}, "cashValue": {"value": "5.0"},
    }
    monkeypatch.setattr(
        "polymarket_bot.live.multi_market_maker.select_target_markets",
        lambda settings, raw_by_slug_out=None: [],
    )
    maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
    safe_future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    esports_raw = {"category": "sports", "sportsMarketType": "esports_match_winner", "gameStartTime": safe_future}

    cycles = maker.refresh_quotes(candidates=[], extra_raw_by_slug={slug: esports_raw})

    assert len(cycles) == 1
    assert cycles[0].ask.order_id is not None
    assert cycles[0].bid.order_id is None
    assert "excluded market family" in cycles[0].bid.error


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


class TestTimingUnclearOrImminentGating:
    def test_true_when_raw_missing(self):
        # Fail CLOSED -- deliberately the opposite of _is_event_started/
        # _is_near_resolution's fail-open convention: zero information about
        # a held position's market means it shouldn't get more exposure.
        maker = MultiMarketMaker(client=_client(), settings=config.LiveTradingSettings(), read_client=_read_client())
        assert maker._is_timing_unclear_or_imminent(None) is True

    def test_true_when_raw_empty(self):
        maker = MultiMarketMaker(client=_client(), settings=config.LiveTradingSettings(), read_client=_read_client())
        assert maker._is_timing_unclear_or_imminent({}) is True

    def test_true_when_raw_present_but_no_timing_fields(self):
        maker = MultiMarketMaker(client=_client(), settings=config.LiveTradingSettings(), read_client=_read_client())
        assert maker._is_timing_unclear_or_imminent({"category": "sports"}) is True

    def test_true_when_timing_unparseable(self):
        maker = MultiMarketMaker(client=_client(), settings=config.LiveTradingSettings(), read_client=_read_client())
        assert maker._is_timing_unclear_or_imminent({"gameStartTime": "not-a-date"}) is True

    def test_false_for_known_start_outside_buffer_even_on_same_day(self):
        settings = config.LiveTradingSettings(pre_event_reduce_only_minutes=60.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        event_time = datetime.now(timezone.utc) + timedelta(hours=2)
        assert maker._is_timing_unclear_or_imminent(
            {"gameStartTime": event_time.isoformat()}
        ) is False

    def test_true_for_known_start_inside_buffer(self):
        settings = config.LiveTradingSettings(pre_event_reduce_only_minutes=60.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        event_time = datetime.now(timezone.utc) + timedelta(minutes=30)
        assert maker._is_timing_unclear_or_imminent(
            {"gameStartTime": event_time.isoformat()}
        ) is True

    def test_true_after_known_start(self):
        settings = config.LiveTradingSettings(pre_event_reduce_only_minutes=60.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        event_time = datetime.now(timezone.utc) - timedelta(minutes=1)
        assert maker._is_timing_unclear_or_imminent(
            {"gameStartTime": event_time.isoformat()}
        ) is True

    def test_reduce_only_reason_string(self):
        imminent = datetime.now(timezone.utc) + timedelta(minutes=30)
        settings = config.LiveTradingSettings(pre_event_reduce_only_minutes=60.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        _in_cooldown, reason, _warn, _over_cap = maker._event_and_toxicity_gating(
            "some-slug", {"gameStartTime": imminent.isoformat()}, {},
        )
        assert reason == "event start imminent or unknown timing"

    def test_combined_reason_with_event_started(self):
        # An event that started earlier today necessarily reachable both
        # checks -- already-started implies same-UTC-day when the grace
        # period is 0. Both reasons legitimately fire together.
        started = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        settings = config.LiveTradingSettings(max_started_event_hours=0.0)
        maker = MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())
        _in_cooldown, reason, _warn, _over_cap = maker._event_and_toxicity_gating(
            "some-slug", {"gameStartTime": started}, {},
        )
        assert reason == "event already started"


def test_pilot_reopens_at_start_then_cuts_entries_and_flattens_at_boundaries():
    settings = config.LiveTradingSettings(
        pilot_mode=True,
        max_started_event_hours=3.0,
        pre_event_reduce_only_minutes=60.0,
        observation_controlled_entry_cutoff_minutes=30.0,
        hard_flatten_minutes_before_event=0.0,
    )
    maker = MultiMarketMaker(
        client=_client(), settings=settings, read_client=_read_client(),
    )
    now = datetime.now(timezone.utc)

    assert maker._is_timing_unclear_or_imminent({
        "gameStartTime": (now - timedelta(seconds=1)).isoformat(),
    }) is False
    assert maker._pilot_entry_cutoff_due({
        "gameStartTime": (now - timedelta(hours=2, minutes=30)).isoformat(),
    }) is True
    assert maker._hard_flatten_due({
        "gameStartTime": (now - timedelta(hours=3)).isoformat(),
    }) is True


def test_pilot_round_trip_counter_allows_two_then_reaches_limit():
    settings = config.LiveTradingSettings(
        pilot_mode=True,
        one_round_trip_per_market_per_session=False,
        pilot_max_round_trips_per_market=2,
    )
    maker = MultiMarketMaker(
        client=_client(), settings=settings, read_client=_read_client(),
    )
    flat = {"m1": {"netPositionDecimal": "0"}}
    held = {"m1": {"netPositionDecimal": "1"}}
    maker._observe_position_transitions(flat)

    for _ in range(2):
        maker.note_fills({"m1"})
        maker._observe_position_transitions(held)
        maker.note_fills({"m1"})
        maker._observe_position_transitions(flat)

    assert maker._completed_round_trip_counts["m1"] == 2
    assert maker._round_trip_limit() == 2


class TestHardFlattenDeadline:
    def _maker(self, minutes: float = 90.0):
        settings = config.LiveTradingSettings(
            hard_flatten_minutes_before_event=minutes,
        )
        return MultiMarketMaker(client=_client(), settings=settings, read_client=_read_client())

    def _raw(self, minutes_from_now: float):
        when = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
        return {"gameStartTime": when.isoformat()}

    def test_false_outside_deadline(self):
        assert self._maker()._hard_flatten_due(self._raw(120)) is False

    def test_true_inside_deadline(self):
        assert self._maker()._hard_flatten_due(self._raw(60)) is True

    def test_true_after_event_started(self):
        assert self._maker()._hard_flatten_due(self._raw(-1)) is True

    def test_true_when_timing_unknown(self):
        assert self._maker()._hard_flatten_due(None) is True

    def test_nonpositive_setting_disables_deadline(self):
        assert self._maker(minutes=0)._hard_flatten_due(None) is False


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
    # settings.order_shares_min is no longer shrunk for the cooldown market --
    # only increasing_order_shares carries the toxicity discount now, applied
    # by MarketMaker solely to whichever leg turns out to be increasing.
    assert by_slug["m1"]["settings"].order_shares_min == pytest.approx(16.0)
    assert by_slug["m1"]["increasing_order_shares"] == pytest.approx(8.0)
    assert by_slug["m2"]["reduce_only_reason"] is None
    assert by_slug["m2"]["settings"].order_shares_min == pytest.approx(16.0)
    assert by_slug["m2"]["increasing_order_shares"] is None


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
    # 2026-01-01 found). gameStartTime a safe 3 days out -- clear of the
    # same-day/unknown-timing reduce-only check, so these tests isolate
    # event-exposure behavior specifically.
    safe_future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    market = Market(
        market_id=f"typ-{bucket_suffix}-{market_id}",
        event_id=f"e-{market_id}",
        question=f"Will {market_id} happen?",
        category="sports",
        token_ids=[f"t-{market_id}"],
        spread=0.04,
        raw={"orderPriceMinTickSize": 0.01, "marketType": "props", "gameStartTime": safe_future},
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

        # The new flat candidate is blocked. The already-held sibling may
        # produce a reducing orphan-management cycle now that all makers use
        # the same reconciled position snapshot.
        assert not any(c.market_id == new_candidate.market.market_id for c in cycles)

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
            slug: {"netPositionDecimal": "100", "cost": {"value": "25.0"}, "cashValue": {"value": "25.0"}},
        }
        client.get_position.return_value = {
            "netPositionDecimal": "100", "cost": {"value": "25.0"}, "cashValue": {"value": "25.0"},
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
        in_cooldown, reason, _warn, _over_cap = maker._event_and_toxicity_gating(
            slug, scored.market.raw, exposure_by_bucket,
        )
        assert in_cooldown is True
        assert reason == "toxicity cooldown + event exposure at or above cap"
        assert _over_cap is True

    def test_warn_tier_widens_edge_and_shrinks_size_without_reduce_only(self, monkeypatch, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
            max_event_exposure_pct=0.20, warn_event_exposure_pct=0.10,
            # Pinned explicitly (not left to the ambient .env default, which
            # is 0.10 as of the risk-tightening batch) -- this test is about
            # the warn tier specifically; _scored_in_bucket's candidate is a
            # stat prop, so an unpinned lower stat-prop cap would incorrectly
            # push it into the hard-cap branch instead of the warn tier.
            stat_prop_max_event_exposure_pct=0.20,
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
        # Warn-tier shrinks base size to 16.0*0.75=12.0 first. Headroom is
        # $20 cap - $12 existing settled exposure = $8. Flat candidate means
        # BOTH bid and ask are increasing, but each is clipped against the
        # FULL $8 headroom independently (a filled bid and filled ask on
        # the same token can't both be simultaneously realized risk -- see
        # live/RUNBOOK.md's most recent section): 8/0.49=16.3.. comfortably
        # covers the already-shrunk 12.0 base size, so neither leg is
        # clipped further by the event cap at all.
        candidate_cycle = next(c for c in cycles if c.market_id == new_candidate.market.market_id)
        assert candidate_cycle.bid.order_id is not None
        assert candidate_cycle.ask.order_id is not None
        assert candidate_cycle.bid.size == pytest.approx(12.0)
        assert candidate_cycle.ask.size == pytest.approx(12.0)

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

        assert not any(c.market_id == new_candidate.market.market_id for c in cycles)


class TestReduceUrgent:
    """reduce_urgent = over_cap OR near_resolution OR breaker_risk, threaded
    into each MarketMaker this cycle. See live/RUNBOOK.md's most recent
    section."""

    def _captured_reduce_urgent(self, monkeypatch, client, read_client, settings, candidate, **refresh_kwargs) -> bool:
        captured: dict = {}
        real_market_maker = MarketMaker

        def spy(*args, **kwargs):
            captured["reduce_urgent"] = kwargs["reduce_urgent"]
            return real_market_maker(*args, **kwargs)

        monkeypatch.setattr("polymarket_bot.live.multi_market_maker.MarketMaker", spy)
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
        maker.refresh_quotes(candidates=[candidate], **refresh_kwargs)
        return captured["reduce_urgent"]

    def test_false_when_no_trigger_applies(self, monkeypatch, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(order_shares_min=15.0, order_shares_max=20.0)
        candidate = _scored("m1")
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: [candidate],
        )

        reduce_urgent = self._captured_reduce_urgent(monkeypatch, client, read_client, settings, candidate)

        assert reduce_urgent is False

    def test_true_when_near_resolution(self, monkeypatch, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=20.0, near_resolution_hours_threshold=24.0,
        )
        candidate = _scored("m1")
        soon = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        candidate.market.raw["gameStartTime"] = soon
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: [candidate],
        )

        reduce_urgent = self._captured_reduce_urgent(monkeypatch, client, read_client, settings, candidate)

        assert reduce_urgent is True

    def test_true_when_breaker_risk_passed(self, monkeypatch, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(order_shares_min=15.0, order_shares_max=20.0)
        candidate = _scored("m1")
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: [candidate],
        )

        reduce_urgent = self._captured_reduce_urgent(
            monkeypatch, client, read_client, settings, candidate, breaker_risk=True,
        )

        assert reduce_urgent is True

    def test_true_when_over_event_exposure_cap(self, monkeypatch, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10,
            max_event_exposure_pct=0.20,
        )
        candidate = _scored_in_bucket("over")
        slug = candidate.market.market_id
        client.get_all_positions.return_value = {
            slug: {"netPositionDecimal": "10", "cost": {"value": "25.0"}, "cashValue": {"value": "25.0"}},
        }
        client.get_position.return_value = {
            "netPositionDecimal": "10", "cost": {"value": "2.5"}, "cashValue": {"value": "2.5"},
        }
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: [candidate],
        )

        reduce_urgent = self._captured_reduce_urgent(monkeypatch, client, read_client, settings, candidate)

        assert reduce_urgent is True


class TestSameCycleEventExposureSizing:
    """The exposure-cap overshoot fix's headroom-sizing mechanism -- see
    live/RUNBOOK.md's most recent section and the real 2026-07-09
    Argentina-Switzerland incident it closes."""

    def test_2026_07_09_incident_replay_third_sibling_starved_total_stays_under_cap(
        self, monkeypatch, isolated_ledger,
    ):
        # GATING TEST for the sum -> max change to each market's OWN two
        # legs (see live/RUNBOOK.md's most recent section): the real
        # 2026-07-09 incident was multiple sibling stat-prop markets on the
        # SAME game (Argentina-Switzerland corners/cards/goals-style
        # sub-markets), all flat, all quoted within one cycle, where a
        # STATIC settled-position snapshot never accounted for (a) this
        # cycle's other resting orders in the same bucket, or (b) the size
        # of the very order about to be placed -- so later siblings in the
        # loop had no idea earlier ones had already committed capital.
        # This is a DIFFERENT mechanism than the single-market bid/ask
        # question the sum->max change addresses: it's the cross-market
        # running tally (multi_market_maker.py::_record_provisional_
        # exposure / _event_cap_remaining_usd), which stays fully additive
        # ACROSS markets before and after this change -- only each
        # market's OWN contribution (max of its two legs, not their sum)
        # changed. This test proves that cross-market protection still
        # works under the new per-market math: with a $10 cap and three
        # $4.90-per-market siblings (10 shares @ 0.49/0.51 book, matching
        # the real bucket's stat-prop sizing), the third sibling -- unable
        # to fit after the first two consume $9.80 -- must be starved
        # (not silently let through), keeping total worst-case exposure at
        # or under the cap. If this fails, the sum->max change is not safe
        # to ship as-is.
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=10.0, order_shares_max=10.0, max_orders_per_cycle=10,
            stat_prop_max_event_exposure_pct=0.10,  # $10 cap on $100 capital
        )
        siblings = [_scored_in_bucket(f"sib{i}") for i in range(3)]
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: siblings,
        )
        maker = MultiMarketMaker(
            client=client, settings=settings, read_client=read_client,
            equity_protection_settings=config.EquityProtectionSettings(starting_capital_usd=100.0),
        )

        cycles = maker.refresh_quotes(candidates=siblings)

        assert len(cycles) == 3
        # First two siblings fit under $10 unclipped (10*0.49=$4.90 each,
        # cumulative $9.80). The third has only $0.20 remaining --
        # 0.20/0.49=0.41 shares, below any usable minimum -- so it must be
        # starved (both legs skipped), not let through at any size.
        assert cycles[0].bid.size == pytest.approx(10.0)
        assert cycles[0].ask.size == pytest.approx(10.0)
        assert cycles[1].bid.size == pytest.approx(10.0)
        assert cycles[1].ask.size == pytest.approx(10.0)
        assert cycles[2].bid.order_id is None
        assert cycles[2].ask.order_id is None
        # The actual invariant the cap exists to guarantee: total worst-
        # case exposure (each market's own max-of-two-legs) never exceeds
        # the bucket cap, regardless of how many siblings shared it.
        total_worst_case_usd = sum(
            max(
                c.bid.size * c.bid.price if c.bid.order_id else 0.0,
                c.ask.size * (1.0 - c.ask.price) if c.ask.order_id else 0.0,
            )
            for c in cycles
        )
        assert total_worst_case_usd <= 10.0 + 1e-6

    def test_second_sibling_market_sees_first_siblings_headroom_consumed(self, monkeypatch, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=10.0, order_shares_max=10.0, max_orders_per_cycle=10,
            stat_prop_max_event_exposure_pct=0.08,  # $8 cap on $100 capital
        )
        first = _scored_in_bucket("first")
        second = _scored_in_bucket("second")
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: [first, second],
        )
        maker = MultiMarketMaker(
            client=client, settings=settings, read_client=read_client,
            equity_protection_settings=config.EquityProtectionSettings(starting_capital_usd=100.0),
        )

        cycles = maker.refresh_quotes(candidates=[first, second])

        # Both flat, both in the same bucket. A filled bid and filled ask
        # on the SAME token can't both be simultaneously realized risk (if
        # both fill, they net to a captured spread) -- so each market's own
        # contribution to the running bucket tally is the MAX of its two
        # legs (10*0.49=$4.90), not their sum. See live/RUNBOOK.md's most
        # recent section. The first fits alone under the $8 cap (4.90/0.49
        # per-share would need 10 shares max at full headroom -- unclipped)
        # and commits $4.90. The second then sees only $3.10 remaining
        # (8.0 - 4.90) for EACH of its own legs independently:
        # 3.10/0.49=6.32.. floors to 6.0. Different markets in the same
        # bucket stay fully additive -- that cross-market case is the real
        # 2026-07-09 incident this mechanism exists to prevent.
        assert cycles[0].bid.size == pytest.approx(10.0)
        assert cycles[0].ask.size == pytest.approx(10.0)
        assert cycles[1].bid.size == pytest.approx(6.0)
        assert cycles[1].ask.size == pytest.approx(6.0)
        # Each market's own worst-case contribution (max of its two legs,
        # not their sum) stays within the shared cap in total.
        total_worst_case_usd = sum(
            max(c.bid.size * c.bid.price, c.ask.size * (1.0 - c.ask.price)) for c in cycles
        )
        assert total_worst_case_usd <= 8.0 + 1e-6

    def test_oversized_solo_order_sized_down_not_fully_blocked(self, monkeypatch, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=10.0, order_shares_max=10.0, max_orders_per_cycle=10,
            stat_prop_max_event_exposure_pct=0.03,  # $3 cap on $100 capital
        )
        candidate = _scored_in_bucket("solo")
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: [candidate],
        )
        maker = MultiMarketMaker(
            client=client, settings=settings, read_client=read_client,
            equity_protection_settings=config.EquityProtectionSettings(starting_capital_usd=100.0),
        )

        cycles = maker.refresh_quotes(candidates=[candidate])

        # Start-of-cycle exposure is 0 (flat, no siblings) -- under cap, so
        # reduce_only_reason is never set and neither leg is fully blocked.
        # Flat means BOTH legs are increasing, and each is clipped against
        # the FULL $3 cap independently (a filled bid and filled ask on the
        # same token can't both be simultaneously realized risk -- see
        # live/RUNBOOK.md's most recent section): 3.00/0.49=6.12.. floors
        # to 6.0 each -- sized down from the base 10.0, not fully blocked.
        assert cycles[0].bid.order_id is not None
        assert cycles[0].ask.order_id is not None
        assert cycles[0].bid.size == pytest.approx(6.0)
        assert cycles[0].ask.size == pytest.approx(6.0)
        # Each leg independently respects the cap -- their SUM is
        # deliberately allowed to exceed it, since they can't both be
        # simultaneously realized risk on the same token.
        assert cycles[0].bid.size * cycles[0].bid.price <= 3.0 + 1e-6
        assert cycles[0].ask.size * (1.0 - cycles[0].ask.price) <= 3.0 + 1e-6


class TestPnlAttributionWiring:
    """P&L attribution ('Strategy B') is wired into refresh_quotes right
    after positions are fetched -- see live/settlements.py. These tests
    cover the wiring itself (gating, best-effort failure isolation), not
    the underlying settlement-detection logic (covered in
    test_settlements.py/test_lot_accounting.py)."""

    def test_records_position_snapshots_when_positions_known(self, isolated_ledger):
        client = _client()
        client.get_all_positions.return_value = {
            "m1": {"netPositionDecimal": "5", "cost": {"value": "2.0"}, "cashValue": {"value": "2.0"}},
        }
        read_client = _read_client()
        settings = config.LiveTradingSettings(order_shares_min=15.0, order_shares_max=20.0)
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

        maker.refresh_quotes(candidates=[])

        snapshots = settlements_module.get_all_position_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0]["market_slug"] == "m1"

    def test_does_not_record_snapshots_when_positions_unknown(self, isolated_ledger):
        client = _client()
        client.get_all_positions.side_effect = UsApiError("boom")
        read_client = _read_client()
        settings = config.LiveTradingSettings(order_shares_min=15.0, order_shares_max=20.0)
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

        maker.refresh_quotes(candidates=[])

        assert settlements_module.get_all_position_snapshots() == []

    def test_does_not_record_snapshots_when_disabled(self, isolated_ledger):
        client = _client()
        client.get_all_positions.return_value = {
            "m1": {"netPositionDecimal": "5", "cost": {"value": "2.0"}, "cashValue": {"value": "2.0"}},
        }
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=20.0, position_snapshot_enabled=False,
        )
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

        maker.refresh_quotes(candidates=[])

        assert settlements_module.get_all_position_snapshots() == []

    def test_settlement_detection_skipped_when_disabled(self, isolated_ledger):
        client = _client()
        client.get_all_positions.return_value = {}
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=20.0, settlement_detection_enabled=False,
        )
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
        maker._detect_and_record_settlements = Mock()

        maker.refresh_quotes(candidates=[])

        maker._detect_and_record_settlements.assert_not_called()

    def test_settlement_detection_runs_when_enabled(self, isolated_ledger):
        client = _client()
        client.get_all_positions.return_value = {}
        read_client = _read_client()
        settings = config.LiveTradingSettings(order_shares_min=15.0, order_shares_max=20.0)
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
        maker._detect_and_record_settlements = Mock()

        maker.refresh_quotes(candidates=[])

        maker._detect_and_record_settlements.assert_called_once_with({})

    def test_settlement_detection_failure_does_not_crash_the_cycle(self, monkeypatch, isolated_ledger):
        client = _client()
        client.get_all_positions.return_value = {}
        read_client = _read_client()
        settings = config.LiveTradingSettings(order_shares_min=15.0, order_shares_max=20.0)
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.get_all_fills",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

        cycles = maker.refresh_quotes(candidates=[])  # must not raise


class TestExecutionBackfill:
    """A private-WS gap can silently and permanently drop a fill --
    reconnection only reconciles positions/open orders, never executions.
    _backfill_missed_executions() uses GET /v1/order/{orderId} (the only
    execution-history-adjacent endpoint this platform has) to close that
    gap for orders the ledger knows about but lost track of."""

    def test_backfills_a_filled_order_the_ws_never_reported(self, isolated_ledger):
        _record_known_order("o1", market_id="m1")
        client = _client()
        client.get_order.return_value = {
            "id": "o1", "state": "ORDER_STATE_FILLED", "cumQuantity": "5.0",
            "avgPx": {"value": "0.45", "currency": "USD"},
            "commissionNotionalTotalCollected": {"value": "0.02", "currency": "USD"},
            "marketSlug": "m1", "side": "ORDER_SIDE_BUY",
        }
        maker = MultiMarketMaker(client=client, settings=config.LiveTradingSettings())

        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])

        client.get_order.assert_called_once_with("o1")
        fills = fills_module.get_all_fills()
        assert len(fills) == 1
        assert fills[0]["order_id"] == "o1"
        assert fills[0]["market_slug"] == "m1"
        assert fills[0]["shares"] == pytest.approx(5.0)
        assert fills[0]["price"] == pytest.approx(0.45)

    def test_does_not_check_an_order_still_currently_open(self, isolated_ledger):
        _record_known_order("o1", market_id="m1")
        client = _client()
        maker = MultiMarketMaker(client=client, settings=config.LiveTradingSettings())

        maker._backfill_missed_executions(
            scope_slugs={"m1"}, open_orders=[{"id": "o1", "marketSlug": "m1"}],
        )

        client.get_order.assert_not_called()

    def test_does_not_check_an_order_that_already_has_a_recorded_fill(self, isolated_ledger):
        _record_known_order("o1", market_id="m1")
        client = _client()
        maker = MultiMarketMaker(client=client, settings=config.LiveTradingSettings())
        fills_module.record_fill(FillRecord(
            fill_id="f1", order_id="o1", market_slug="m1", side="BUY", price=0.45, shares=5.0,
            outcome="YES", commission_usd=0.02, execution_type="EXECUTION_TYPE_FILL",
            transact_time="2026-07-06T14:00:00Z", quoted_price=0.45, current_mid_at_detection=None,
            edge_vs_current_mid_cents=None, fill_quality=None, markout_1m_cents=None,
            markout_1m_computed_at=None, markout_5m_cents=None, markout_5m_computed_at=None,
            markout_15m_cents=None, markout_15m_computed_at=None, detected_at="2026-07-06T14:00:01Z",
            raw_execution={},
        ))

        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])

        client.get_order.assert_not_called()

    def test_ignores_orders_on_markets_outside_scope_this_cycle(self, isolated_ledger):
        _record_known_order("o1", market_id="m2")  # not in scope_slugs below
        client = _client()
        maker = MultiMarketMaker(client=client, settings=config.LiveTradingSettings())

        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])

        client.get_order.assert_not_called()

    def test_cancelled_order_records_no_fill_but_is_cached_as_resolved(self, isolated_ledger):
        _record_known_order("o1", market_id="m1")
        client = _client()
        client.get_order.return_value = {"id": "o1", "state": "ORDER_STATE_CANCELED", "cumQuantity": "0"}
        maker = MultiMarketMaker(client=client, settings=config.LiveTradingSettings())

        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])
        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])  # second cycle

        assert fills_module.get_all_fills() == []
        client.get_order.assert_called_once_with("o1")  # not re-checked the second time
        assert fills_module.get_backfill_resolved_order_ids() == {"o1"}

    def test_cached_as_resolved_survives_a_fresh_instance(self, isolated_ledger):
        # Simulates a bot restart: a fresh MultiMarketMaker (no shared
        # in-memory state with the one that made the original determination)
        # must still skip re-checking an order the persisted file already
        # confirmed has nothing to backfill.
        _record_known_order("o1", market_id="m1")
        client = _client()
        client.get_order.return_value = {"id": "o1", "state": "ORDER_STATE_CANCELED", "cumQuantity": "0"}
        first_maker = MultiMarketMaker(client=client, settings=config.LiveTradingSettings())
        first_maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])
        client.get_order.reset_mock()

        second_maker = MultiMarketMaker(client=client, settings=config.LiveTradingSettings())
        second_maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])

        client.get_order.assert_not_called()

    def test_exchange_has_no_record_of_the_order_is_cached_as_resolved(self, isolated_ledger):
        _record_known_order("o1", market_id="m1")
        client = _client()
        client.get_order.return_value = None
        maker = MultiMarketMaker(client=client, settings=config.LiveTradingSettings())

        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])
        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])

        assert fills_module.get_all_fills() == []
        client.get_order.assert_called_once_with("o1")
        assert fills_module.get_backfill_resolved_order_ids() == {"o1"}

    def test_partially_filled_then_cancelled_order_is_still_backfilled(self, isolated_ledger):
        # The bug this closes: an order that filled PARTIALLY before its
        # unfilled remainder was cancelled (plausible for this bot's own IOC
        # orders, which auto-cancel any unfilled remainder) must not be
        # silently treated as "nothing to backfill" just because it never
        # reached ORDER_STATE_FILLED.
        _record_known_order("o1", market_id="m1")
        client = _client()
        client.get_order.return_value = {
            "id": "o1", "state": "ORDER_STATE_CANCELED", "cumQuantity": "3.0",
            "avgPx": {"value": "0.40", "currency": "USD"},
            "marketSlug": "m1", "side": "ORDER_SIDE_BUY",
        }
        maker = MultiMarketMaker(client=client, settings=config.LiveTradingSettings())

        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])

        fills = fills_module.get_all_fills()
        assert len(fills) == 1
        assert fills[0]["order_id"] == "o1"
        assert fills[0]["shares"] == pytest.approx(3.0)
        assert fills[0]["price"] == pytest.approx(0.40)
        # Not double-tracked -- the fill's own order_id already excludes it
        # from future candidacy.
        assert fills_module.get_backfill_resolved_order_ids() == set()

    def test_per_cycle_lookup_count_is_capped(self, isolated_ledger):
        for i in range(8):
            _record_known_order(f"o{i}", market_id="m1")
        client = _client()
        client.get_order.return_value = {"id": "x", "state": "ORDER_STATE_CANCELED", "cumQuantity": "0"}
        settings = config.LiveTradingSettings(
            execution_backfill_max_lookups_per_cycle=3, execution_backfill_min_interval_seconds=0.0,
        )
        maker = MultiMarketMaker(client=client, settings=settings)

        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])

        assert client.get_order.call_count == 3

    def test_capped_candidates_are_picked_up_on_a_later_cycle(self, isolated_ledger):
        for i in range(5):
            _record_known_order(f"o{i}", market_id="m1")
        client = _client()
        client.get_order.return_value = {"id": "x", "state": "ORDER_STATE_CANCELED", "cumQuantity": "0"}
        settings = config.LiveTradingSettings(
            execution_backfill_max_lookups_per_cycle=3, execution_backfill_min_interval_seconds=0.0,
        )
        maker = MultiMarketMaker(client=client, settings=settings)

        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])
        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])

        # 5 candidates, capped at 3/cycle -> all 5 resolved by the second cycle
        # (3 + the remaining 2), not re-checking the first 3 again.
        assert client.get_order.call_count == 5
        assert fills_module.get_backfill_resolved_order_ids() == {f"o{i}" for i in range(5)}

    def test_lookups_are_rate_limited_within_a_cycle(self, isolated_ledger, monkeypatch):
        for i in range(3):
            _record_known_order(f"o{i}", market_id="m1")
        client = _client()
        client.get_order.return_value = {"id": "x", "state": "ORDER_STATE_CANCELED", "cumQuantity": "0"}
        settings = config.LiveTradingSettings(
            execution_backfill_max_lookups_per_cycle=10,
            execution_backfill_min_interval_seconds=0.05,
        )
        sleeps: list[float] = []
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.time.sleep", lambda s: sleeps.append(s)
        )
        maker = MultiMarketMaker(client=client, settings=settings)

        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])

        # 3 candidates -> 2 gaps between them, not a sleep before the first.
        assert sleeps == [0.05, 0.05]

    def test_zero_interval_setting_never_sleeps(self, isolated_ledger, monkeypatch):
        for i in range(3):
            _record_known_order(f"o{i}", market_id="m1")
        client = _client()
        client.get_order.return_value = {"id": "x", "state": "ORDER_STATE_CANCELED", "cumQuantity": "0"}
        settings = config.LiveTradingSettings(
            execution_backfill_max_lookups_per_cycle=10,
            execution_backfill_min_interval_seconds=0.0,
        )
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.time.sleep",
            lambda s: (_ for _ in ()).throw(AssertionError("must not sleep when interval is 0")),
        )
        maker = MultiMarketMaker(client=client, settings=settings)

        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])  # must not raise

    def test_lookup_failure_is_not_cached_and_retries_next_cycle(self, isolated_ledger):
        _record_known_order("o1", market_id="m1")
        client = _client()
        client.get_order.side_effect = UsApiError("network error")
        maker = MultiMarketMaker(client=client, settings=config.LiveTradingSettings())

        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])  # must not raise
        maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])

        assert client.get_order.call_count == 2  # retried, not silently given up on

    def test_no_scope_slugs_skips_the_whole_check(self, isolated_ledger):
        _record_known_order("o1", market_id="m1")
        client = _client()
        maker = MultiMarketMaker(client=client, settings=config.LiveTradingSettings())

        maker._backfill_missed_executions(scope_slugs=set(), open_orders=[])

        client.get_order.assert_not_called()

    def test_wired_into_refresh_quotes_end_to_end(self, monkeypatch, isolated_ledger):
        # Confirms the real entry point, not just a direct unit call:
        # refresh_quotes() itself reaches _backfill_missed_executions after
        # managing m1 this cycle.
        _record_known_order("m1-old", market_id="m1")
        client = _client(
            get_open_orders=Mock(return_value=[]),  # m1-old no longer resting
        )
        client.get_order.return_value = {
            "id": "m1-old", "state": "ORDER_STATE_FILLED", "cumQuantity": "16.0",
            "avgPx": {"value": "0.50", "currency": "USD"}, "marketSlug": "m1", "side": "ORDER_SIDE_BUY",
        }
        read_client = _read_client()
        settings = config.LiveTradingSettings(order_shares_min=16.0, order_shares_max=16.0, max_orders_per_cycle=10)
        monkeypatch.setattr(
            "polymarket_bot.live.multi_market_maker.select_target_markets",
            lambda settings, raw_by_slug_out=None: [_scored("m1")],
        )
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

        maker.refresh_quotes()

        client.get_order.assert_called_once_with("m1-old")
        fills = fills_module.get_all_fills()
        assert any(f["order_id"] == "m1-old" for f in fills)

    def test_partial_fill_during_unwind_race_is_recoverable_via_backfill(self, isolated_ledger):
        # The gap this closes: before the fix, MarketMaker._unwind_
        # unpaired_entry() erased the survivor's order_id before
        # record_cycle() ran, so a partial fill that happened in the brief
        # submit-to-cancel race window -- and that the private WebSocket
        # missed -- had no order_id left anywhere in the ledger for
        # execution-backfill to ever look up, permanently losing the fill.
        # Drives a real unwind through MarketMaker.refresh_quotes() (not a
        # hand-seeded ledger row) to prove the two previously-disconnected
        # pieces are actually wired together now.
        entry_client = _client()
        entry_client.create_order.side_effect = [{"id": "bid-1"}, _api_error(400)]
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=20.0, require_both_entry_legs=True,
        )
        maker = MarketMaker(
            client=entry_client, market_slug="m1", tick_size=0.01,
            settings=settings, read_client=read_client,
        )

        cycle = maker.refresh_quotes(open_orders=[])

        assert cycle is not None
        assert cycle.bid.order_id == "bid-1"
        assert cycle.bid.is_resting is False

        # Now simulate the exact race this is meant to catch: "bid-1"
        # actually filled 3 of its 17 shares before this bot's own cancel
        # landed, and the private WS never delivered that execution.
        backfill_client = _client()
        backfill_client.get_order.return_value = {
            "id": "bid-1", "state": "ORDER_STATE_CANCELED", "cumQuantity": "3.0",
            "avgPx": {"value": "0.49", "currency": "USD"}, "marketSlug": "m1", "side": "ORDER_SIDE_BUY",
        }
        multi_maker = MultiMarketMaker(client=backfill_client, settings=config.LiveTradingSettings())

        multi_maker._backfill_missed_executions(scope_slugs={"m1"}, open_orders=[])

        backfill_client.get_order.assert_called_once_with("bid-1")
        fills = fills_module.get_all_fills()
        assert len(fills) == 1
        assert fills[0]["order_id"] == "bid-1"
        assert fills[0]["shares"] == pytest.approx(3.0)


def test_count_placed_orders_does_not_count_an_unwound_leg():
    # Same reasoning as TestRunOneMarketPrivateStoreWiring below: order_id
    # alone can no longer mean "resting" now that an unwound leg's order_id
    # is deliberately preserved. Counting it here would inflate the order-
    # budget tally and wrongly sticky-pin a market that has nothing resting.
    cycle = LiveQuoteCycle(
        cycle_id="c1", market_id="m1", reference_price=0.5, tick_size=0.01,
        bid=PostedLeg(side="BUY", price=0.49, size=0.0, order_id="bid-1", error="cancelled: ..."),
        ask=PostedLeg(side="SELL", price=0.51, size=0.0, error="rejected: ..."),
        timestamp="2026-01-01T00:00:00+00:00",
    )

    assert _count_placed_orders(cycle) == 0


class TestRunOneMarketPrivateStoreWiring:
    def test_unwound_leg_is_removed_from_private_store_and_not_reupserted(self, isolated_ledger):
        # The other half of the ledger-preservation fix: _unwind_unpaired_
        # entry() deliberately keeps the cancelled leg's order_id (see
        # TestExecutionBackfill above), so _run_one_market's "is this leg
        # actually resting" checks can no longer use bare order_id
        # truthiness -- PostedLeg.is_resting (driven by size, not order_id)
        # is what has to gate both the removal-is-real check here AND the
        # upsert_local_orders step, or the removal that last_cancelled_
        # order_ids triggers would be immediately undone by re-inserting
        # the same order as ORDER_STATE_NEW right below it.
        client = _client()
        client.create_order.side_effect = [{"id": "bid-1"}, _api_error(400)]
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=20.0, require_both_entry_legs=True,
        )
        private_store = Mock()
        multi_maker = MultiMarketMaker(
            client=client, settings=settings, read_client=read_client, private_store=private_store,
        )
        market_maker = MarketMaker(
            client=client, market_slug="m1", tick_size=0.01, settings=settings, read_client=read_client,
        )

        cycle, ran_ok = multi_maker._run_one_market(market_maker, remaining=10, open_orders=[])

        assert ran_ok is True
        assert cycle.bid.order_id == "bid-1"
        assert cycle.bid.is_resting is False
        private_store.remove_orders.assert_called_once_with(["bid-1"])
        private_store.upsert_local_orders.assert_called_once_with([])


def _scored_near_resolution(market_id: str) -> ScoredMarket:
    """Like _scored(), but with gameStartTime 12 hours out -- inside the
    default 24h near_resolution_hours_threshold, so _is_near_resolution()
    returns True for this market."""
    imminent = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    market = Market(
        market_id=market_id,
        event_id=f"e-{market_id}",
        question=f"Will {market_id} happen?",
        category="politics",
        token_ids=[f"t-{market_id}"],
        spread=0.04,
        raw={"orderPriceMinTickSize": 0.01, "gameStartTime": imminent},
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


class TestStickyMarketAllocation:
    """A confirmed real run (2026-07-20) re-decided which markets won the
    shared order budget completely fresh every ~10s cycle -- 20 cycles, 62
    posted legs, 0 fills, orders resting only 10-20s each. Sticky
    allocation (self._pinned_markets) keeps a flat candidate that wins the
    budget preferred over fresh ranking until it becomes unsafe, its hold
    window expires, or it stops posting legs on its own merit."""

    def test_pin_retention_beats_fresh_incoming_order(self, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=15.0, max_orders_per_cycle=2,
        )
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

        cycle1 = maker.refresh_quotes(candidates=[_scored("m1"), _scored("m2")])
        assert len(cycle1) == 1
        assert cycle1[0].market_id == "m1"
        assert "m1" in maker._pinned_markets

        # m2 now first in incoming order -- without stickiness this would
        # flip the winner every cycle, exactly the confirmed real failure.
        cycle2 = maker.refresh_quotes(candidates=[_scored("m2"), _scored("m1")])

        assert len(cycle2) == 1
        assert cycle2[0].market_id == "m1"

    def test_pinned_market_survives_one_zero_leg_cycle(self, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=15.0, max_orders_per_cycle=2,
        )
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
        maker.refresh_quotes(candidates=[_scored("m1")])
        assert "m1" in maker._pinned_markets

        # Book too thin this cycle -- MarketMaker itself declines to quote
        # (insufficient edge/depth), a normal 0-leg outcome.
        read_client.get_market_book.return_value = {
            "bids": [{"price": 0.48, "quantity": 0.1}],
            "asks": [{"price": 0.52, "quantity": 0.1}],
        }
        cycles = maker.refresh_quotes(candidates=[_scored("m1")])

        assert cycles == []
        assert "m1" in maker._pinned_markets  # NOT evicted on a single bad cycle

    def test_near_resolution_does_not_release_or_churn_flat_pin(self, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=15.0, max_orders_per_cycle=2,
        )
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

        cycle1 = maker.refresh_quotes(candidates=[_scored("m1"), _scored("m2")])
        assert cycle1[0].market_id == "m1"
        assert "m1" in maker._pinned_markets
        client.get_open_orders.return_value = [
            {
                "id": cycle1[0].bid.order_id, "marketSlug": "m1", "side": "BUY",
                "price": {"value": str(cycle1[0].bid.price)},
                "leavesQuantity": str(cycle1[0].bid.size),
            },
            {
                "id": cycle1[0].ask.order_id, "marketSlug": "m1", "side": "SELL",
                "price": {"value": str(cycle1[0].ask.price)},
                "leavesQuantity": str(cycle1[0].ask.size),
            },
        ]
        client.reset_mock()

        # The broad near-resolution window widens m1's required edge but is
        # not itself a reason to discard queue time.
        cycle2 = maker.refresh_quotes(
            candidates=[_scored("m2"), _scored_near_resolution("m1")],
        )

        assert cycle2[0].market_id == "m1"
        assert "m1" in maker._pinned_markets
        client.cancel_order.assert_not_called()
        client.create_order.assert_not_called()

    def test_over_cap_release_cancels_before_replacement_posts(
        self, isolated_ledger, monkeypatch,
    ):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=15.0, max_orders_per_cycle=2,
        )
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)
        cycle1 = maker.refresh_quotes(candidates=[_scored("m1"), _scored("m2")])
        client.get_open_orders.return_value = [
            {"id": cycle1[0].bid.order_id, "marketSlug": "m1"},
            {"id": cycle1[0].ask.order_id, "marketSlug": "m1"},
        ]
        original_gating = maker._event_and_toxicity_gating

        def gating(slug, raw, exposures):
            if slug == "m1":
                return False, "event exposure cap", False, True
            return original_gating(slug, raw, exposures)

        monkeypatch.setattr(maker, "_event_and_toxicity_gating", gating)
        client.reset_mock()

        cycle2 = maker.refresh_quotes(candidates=[_scored("m2"), _scored("m1")])

        assert cycle2[0].market_id == "m2"
        assert "m1" not in maker._pinned_markets
        cancel_indices = [i for i, c in enumerate(client.mock_calls) if c[0] == "cancel_order"]
        create_indices = [i for i, c in enumerate(client.mock_calls) if c[0] == "create_order"]
        assert cancel_indices and create_indices
        assert cancel_indices[0] < create_indices[0]

    def test_hold_window_expiry_demotes_without_proactive_cancel(self, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=15.0, max_orders_per_cycle=2,
            sticky_market_hold_seconds=60.0,
        )
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

        cycle1 = maker.refresh_quotes(candidates=[_scored("m1"), _scored("m2")])
        assert cycle1[0].market_id == "m1"
        assert "m1" in maker._pinned_markets
        client.get_open_orders.return_value = [
            {"id": cycle1[0].bid.order_id, "marketSlug": "m1"},
            {"id": cycle1[0].ask.order_id, "marketSlug": "m1"},
        ]
        # Simulate the hold window elapsing without a real sleep.
        maker._pinned_markets["m1"] = time.monotonic() - 61.0
        client.reset_mock()

        cycle2 = maker.refresh_quotes(candidates=[_scored("m2"), _scored("m1")])

        assert cycle2[0].market_id == "m2"  # pin expired -- lost the tie to incoming order
        assert "m1" not in maker._pinned_markets
        cancel_indices = [i for i, c in enumerate(client.mock_calls) if c[0] == "cancel_order"]
        create_indices = [i for i, c in enumerate(client.mock_calls) if c[0] == "create_order"]
        assert cancel_indices and create_indices
        # Opposite of the unsafe-release case: m1's stale order is only
        # cleaned up by the ordinary post-hoc sweep, AFTER m2's replacement
        # legs already posted -- no proactive cancel for a routine expiry.
        assert create_indices[0] < cancel_indices[0]

    def test_kill_switch_disables_stickiness(self, isolated_ledger):
        client = _client()
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=15.0, max_orders_per_cycle=2,
            sticky_market_allocation_enabled=False,
        )
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

        cycle1 = maker.refresh_quotes(candidates=[_scored("m1"), _scored("m2")])
        assert cycle1[0].market_id == "m1"
        assert maker._pinned_markets == {}

        cycle2 = maker.refresh_quotes(candidates=[_scored("m2"), _scored("m1")])

        assert cycle2[0].market_id == "m2"  # no stickiness -- identical to pre-change behavior

    def test_backfill_runs_before_placement_scoped_to_candidates_and_orphans(self, isolated_ledger):
        _record_known_order("cand-order", market_id="m1")
        _record_known_order("orphan-order", market_id="m2")
        client = _client()
        client.get_all_positions.return_value = {
            "m2": {"netPositionDecimal": "5", "cost": {"value": "2.5"}, "cashValue": {"value": "2.5"}},
        }
        client.get_open_orders.return_value = []
        client.get_order.return_value = {
            "id": "x", "state": "ORDER_STATE_FILLED", "cumQuantity": "1.0",
            "avgPx": {"value": "0.50", "currency": "USD"},
        }
        read_client = _read_client()
        settings = config.LiveTradingSettings(
            order_shares_min=15.0, order_shares_max=15.0, max_orders_per_cycle=4,
        )
        maker = MultiMarketMaker(client=client, settings=settings, read_client=read_client)

        maker.refresh_quotes(candidates=[_scored("m1")])

        checked_ids = {c.args[0] for c in client.get_order.call_args_list}
        assert checked_ids == {"cand-order", "orphan-order"}
        get_order_indices = [i for i, c in enumerate(client.mock_calls) if c[0] == "get_order"]
        create_order_indices = [i for i, c in enumerate(client.mock_calls) if c[0] == "create_order"]
        assert create_order_indices
        assert max(get_order_indices) < min(create_order_indices)

    def test_backfill_uses_private_terminal_zero_fill_without_rest_lookup(self, isolated_ledger):
        _record_known_order("closed-zero", market_id="m1")
        private_store = PrivateStateStore()
        private_store.handle_message({"orderSubscriptionUpdate": {"execution": {"order": {
            "id": "closed-zero", "marketSlug": "m1",
            "state": "ORDER_STATE_CANCELED", "cumQuantity": "0",
        }}}})
        client = _client()
        maker = MultiMarketMaker(
            client=client,
            settings=config.LiveTradingSettings(max_orders_per_cycle=0),
            read_client=_read_client(),
            private_store=private_store,
        )

        maker.refresh_quotes(candidates=[_scored("m1")])

        client.get_order.assert_not_called()
        assert "closed-zero" in fills_module.get_backfill_resolved_order_ids()
