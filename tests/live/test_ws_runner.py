import dataclasses
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live import fills as fills_module
from polymarket_bot.live import instance_lock
from polymarket_bot.live import ledger as ledger_module
from polymarket_bot.live import market_observation as observation_module
from polymarket_bot.live import session_metrics as session_metrics_module
from polymarket_bot.live import settlements as settlements_module
from polymarket_bot.live import ws_runner as ws_runner_module
from polymarket_bot.live.market_maker import EmergencySafeguardFailedError
from polymarket_bot.live.startup_recovery import StartupRecoveryError
from polymarket_bot.live.ws_runner import (
    ObservationFeedStalledError,
    ObservationIntegrityError,
    PilotFlatnessError,
    WebSocketLiveTradingBot,
)
from polymarket_bot.live.models import LiveQuoteCycle, PostedLeg
from polymarket_bot.models import Market, ScoredMarket


@pytest.fixture(autouse=True)
def _isolated_instance_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(instance_lock, "LOCK_FILE", tmp_path / "live_bot.lock")


@pytest.fixture(autouse=True)
def _isolated_fills_file(tmp_path, monkeypatch):
    # _compute_due_markouts/_persist_new_fills read/write fills.json via
    # fills.py's module-level FILLS_FILE -- must never touch the real
    # project's data/live_trades/fills.json from a test run.
    monkeypatch.setattr(fills_module, "FILLS_FILE", tmp_path / "fills.json")


@pytest.fixture(autouse=True)
def _isolated_daily_balance_file(tmp_path, monkeypatch):
    # _estimate_pnl_figures -> ledger_module.diff_against_baseline reads/
    # writes DAILY_BALANCE_FILE -- must never touch the real project's
    # data/live_trades/daily_pnl_baseline.json from a test run.
    monkeypatch.setattr(ledger_module, "DAILY_BALANCE_FILE", tmp_path / "daily_pnl_baseline.json")
    monkeypatch.setattr(settlements_module, "SETTLEMENTS_FILE", tmp_path / "settlements.json")
    # run_forever() now calls startup_recovery.recover_from_prior_crash()
    # (reads LEDGER_FILE) and already used SessionMetricsRecorder (reads/
    # writes SESSIONS_FILE) -- must never touch the real project's
    # data/live_trades/orders.json or sessions.json from a test run.
    monkeypatch.setattr(ledger_module, "LEDGER_FILE", tmp_path / "orders.json")
    monkeypatch.setattr(session_metrics_module, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(observation_module, "OBSERVATION_FILE", tmp_path / "market_observations.json")


def _scored(market_id="m1"):
    market = Market(
        market_id=market_id,
        event_id="e1",
        question="Will X happen?",
        category="politics",
        token_ids=["t1"],
        spread=0.03,
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


def _bot(monkeypatch):
    settings = config.LiveTradingSettings(
        refresh_interval_seconds=0.01, order_shares_min=16.0, order_shares_max=16.0,
        max_orders_per_cycle=10, observation_only_mode=False, observation_gate_enabled=False,
    )
    client = Mock()
    client.cancel_all = Mock()
    client.get_open_orders.return_value = []
    client.get_all_positions.return_value = {}
    market_ws = Mock()
    maker = Mock()
    breaker = Mock()
    breaker.evaluate.return_value = False
    breaker.is_halted.return_value = False
    breaker.settings = config.CircuitBreakerSettings(daily_loss_limit_usd=8.0)
    session_breaker = Mock()
    session_breaker.evaluate.return_value = False
    session_breaker.is_halted.return_value = False
    session_breaker.settings = config.SessionCircuitBreakerSettings(loss_limit_usd=8.0)
    session_breaker.diff.return_value = 0.0
    equity_protection = Mock()
    equity_protection.evaluate.return_value = (False, 1.0)
    equity_protection.is_halted.return_value = False
    selector = Mock(return_value=[_scored("m1"), _scored("m2")])
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.get_total_position_pnl_usd", lambda client: 0.0
    )
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.select_target_markets",
        selector,
    )
    bot = WebSocketLiveTradingBot(
        client=client,
        settings=settings,
        circuit_breaker=breaker,
        session_circuit_breaker=session_breaker,
        equity_protection=equity_protection,
        market_ws=market_ws,
        maker=maker,
    )
    return bot, client, market_ws, maker, breaker, selector, equity_protection


def _maybe_refresh_and_join(bot, timeout: float = 2.0) -> None:
    """_maybe_refresh_candidates() now spawns _refresh_candidates() on a
    background thread instead of running it inline (see
    ws_runner.py::_refresh_candidates_in_background) -- tests that need to
    assert on its effects (_get_candidates()/_last_candidate_refresh*)
    must wait for that thread to finish first, or the assertion races a
    still-running background thread."""
    bot._maybe_refresh_candidates()
    if bot._candidate_refresh_thread is not None:
        bot._candidate_refresh_thread.join(timeout=timeout)


def _set_fast_reprice_book(
    bot,
    *,
    slug: str = "m1",
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
) -> None:
    """Seed the real L2 shape used by fast repricing.

    A lite BBO cannot prove whether its top level is the bot's own quote, so
    the production check deliberately requires the full book.
    """
    bot.store.update_market_data({
        "marketSlug": slug,
        "bids": [
            {"price": price, "quantity": quantity}
            for price, quantity in bids
        ],
        "offers": [
            {"price": price, "quantity": quantity}
            for price, quantity in asks
        ],
    })


def test_check_fast_repricing_cancels_bot_owned_order_when_its_side_moved(monkeypatch):
    bot, client, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, max_recent_move_cents=3.0)
    bot.private_store.seed_open_orders([
        {"marketSlug": "m1", "price": 0.40, "orderId": "o1"},
    ])
    _set_fast_reprice_book(
        bot, bids=[(0.19, 10.0)], asks=[(0.21, 10.0)],
    )
    monkeypatch.setattr(
        ws_runner_module, "get_known_order_details",
        lambda: {"o1": {"market_id": "m1", "side": "BUY", "price": 0.40, "size": 1.0}},
    )

    bot._check_fast_repricing()

    client.cancel_order.assert_called_once_with("o1", "m1")


def test_check_fast_repricing_does_not_cancel_when_its_own_side_is_within_the_guard(monkeypatch):
    bot, client, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, max_recent_move_cents=3.0)
    bot.private_store.seed_open_orders([
        {"marketSlug": "m1", "price": 0.40, "orderId": "o1"},
    ])
    _set_fast_reprice_book(
        bot, bids=[(0.395, 10.0)], asks=[(0.405, 10.0)],
    )
    monkeypatch.setattr(
        ws_runner_module, "get_known_order_details",
        lambda: {"o1": {"market_id": "m1", "side": "BUY", "price": 0.40, "size": 1.0}},
    )

    bot._check_fast_repricing()

    client.cancel_order.assert_not_called()


def test_check_fast_repricing_ignores_orders_the_ledger_does_not_recognize(monkeypatch):
    # A manual order, or one from a different strategy sharing the account
    # -- not something this bot posted -- must never be touched, even if it
    # looks wildly "moved" relative to the current book.
    bot, client, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, max_recent_move_cents=3.0)
    bot.private_store.seed_open_orders([
        {"marketSlug": "m1", "price": 0.40, "orderId": "manual-order"},
    ])
    _set_fast_reprice_book(
        bot, bids=[(0.19, 10.0)], asks=[(0.21, 10.0)],
    )
    monkeypatch.setattr(ws_runner_module, "get_known_order_details", lambda: {})

    bot._check_fast_repricing()

    client.cancel_order.assert_not_called()


def test_check_fast_repricing_does_not_false_positive_on_a_wide_unchanged_book(monkeypatch):
    """Regression test: a 39c bid / 85c ask book (a 46c spread, well inside
    this pilot's max_spread=0.98 opportunity set) is completely unchanged,
    but its MIDPOINT (62c) sits far from a 40c resting bid. Comparing
    against the midpoint (the original bug) would cancel this immediately on
    a fully static market; comparing same-side (bid vs best_bid) must not."""
    bot, client, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, max_recent_move_cents=3.0)
    bot.private_store.seed_open_orders([
        {"marketSlug": "m1", "price": 0.40, "orderId": "o1"},
    ])
    _set_fast_reprice_book(
        bot,
        # Public L2 includes the bot's 40c/1-share bid. Once removed,
        # the unchanged external best bid is the original 39c level.
        bids=[(0.40, 1.0), (0.39, 10.0)],
        asks=[(0.85, 10.0)],
    )
    monkeypatch.setattr(
        ws_runner_module, "get_known_order_details",
        lambda: {"o1": {"market_id": "m1", "side": "BUY", "price": 0.40, "size": 1.0}},
    )

    bot._check_fast_repricing()

    client.cancel_order.assert_not_called()


def test_check_fast_repricing_removes_own_top_level_before_detecting_external_move(
    monkeypatch,
):
    """The public best bid is still the bot's own 40c order while the next
    external bid has collapsed to 19c. Comparing against raw BBO would
    compare the order to itself and miss the exact adverse move this guard
    exists to catch."""
    bot, client, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, max_recent_move_cents=3.0)
    bot.private_store.seed_open_orders([{
        "marketSlug": "m1", "side": "BUY", "price": 0.40,
        "leavesQuantity": 1.0, "orderId": "o1",
    }])
    _set_fast_reprice_book(
        bot,
        bids=[(0.40, 1.0), (0.19, 10.0)],
        asks=[(0.85, 10.0)],
    )
    monkeypatch.setattr(
        ws_runner_module, "get_known_order_details",
        lambda: {"o1": {
            "market_id": "m1", "side": "BUY", "price": 0.40, "size": 1.0,
        }},
    )

    bot._check_fast_repricing()

    client.cancel_order.assert_called_once_with("o1", "m1")


def test_check_fast_repricing_does_not_cancel_a_buy_after_only_favorable_movement(
    monkeypatch,
):
    bot, client, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, max_recent_move_cents=3.0)
    bot.private_store.seed_open_orders([{
        "marketSlug": "m1", "side": "BUY", "price": 0.40,
        "leavesQuantity": 1.0, "orderId": "o1",
    }])
    _set_fast_reprice_book(
        bot, bids=[(0.50, 10.0)], asks=[(0.85, 10.0)],
    )
    monkeypatch.setattr(
        ws_runner_module, "get_known_order_details",
        lambda: {"o1": {
            "market_id": "m1", "side": "BUY", "price": 0.40, "size": 1.0,
        }},
    )

    bot._check_fast_repricing()

    client.cancel_order.assert_not_called()


def test_check_fast_repricing_cancels_both_legs_of_a_paired_entry_together(monkeypatch):
    """Regression test: only the bid drifted, but both bid and ask are
    bot-owned resting legs on the same market. Cancelling only the drifted
    leg would leave the other resting alone -- a paired entry becoming an
    accidental one-sided directional bet.

    Also the direct regression test for the real 2026-08-10 21:33-21:34
    incident: both legs' cancel_order() calls succeed cleanly here (no
    side_effect exception), matching what actually happened that night.
    Previously this fired an unconditional REST get_open_orders() poll
    afterward regardless -- that poll hit a 429 (the account was already
    under load from a concurrent reconciliation retry), which escalated to
    an account-wide cancel_all_and_verify, whose own verification also hit
    a 429, raising EmergencySafeguardFailedError uncaught and crashing the
    pilot 19 minutes into a planned 4-hour run. With both cancels already
    confirmed successful, no REST re-verification should ever fire."""
    bot, client, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, max_recent_move_cents=3.0)
    bot.private_store.seed_open_orders([
        {"marketSlug": "m1", "price": 0.40, "orderId": "bid-1"},
        {"marketSlug": "m1", "price": 0.60, "orderId": "ask-1"},
    ])
    _set_fast_reprice_book(
        bot,
        bids=[(0.40, 1.0), (0.19, 10.0)],
        asks=[(0.60, 1.0), (0.61, 10.0)],
    )
    monkeypatch.setattr(
        ws_runner_module, "get_known_order_details",
        lambda: {
            "bid-1": {"market_id": "m1", "side": "BUY", "price": 0.40, "size": 1.0},
            "ask-1": {"market_id": "m1", "side": "SELL", "price": 0.60, "size": 1.0},
        },
    )

    bot._check_fast_repricing()

    cancelled = {call.args[0] for call in client.cancel_order.call_args_list}
    assert cancelled == {"bid-1", "ask-1"}
    client.get_open_orders.assert_not_called()
    client.cancel_all.assert_not_called()
    assert bot.private_store.open_orders_snapshot() == []


def test_check_fast_repricing_fails_closed_when_one_paired_cancel_survives(
    monkeypatch,
):
    """Genuine doubt (one leg's own cancel_order() call raises) must still
    take the full verify-and-escalate path, unlike the all-succeeded case
    above: (1) the raised cancellation enters REST verification, (2) a
    surviving sibling in that verification triggers a verified account-wide
    cancel-all, (3) inability to establish a clean state even after that
    still raises EmergencySafeguardFailedError rather than resuming with
    unconfirmed exposure."""
    bot, client, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, max_recent_move_cents=3.0)
    resting = [
        {
            "marketSlug": "m1", "side": "BUY", "price": 0.40,
            "leavesQuantity": 1.0, "orderId": "bid-1",
        },
        {
            "marketSlug": "m1", "side": "SELL", "price": 0.60,
            "leavesQuantity": 1.0, "orderId": "ask-1",
        },
    ]
    bot.private_store.seed_open_orders(resting)
    _set_fast_reprice_book(
        bot,
        bids=[(0.40, 1.0), (0.19, 10.0)],
        asks=[(0.60, 1.0), (0.61, 10.0)],
    )
    monkeypatch.setattr(
        ws_runner_module, "get_known_order_details",
        lambda: {
            "bid-1": {"market_id": "m1", "side": "BUY", "price": 0.40, "size": 1.0},
            "ask-1": {"market_id": "m1", "side": "SELL", "price": 0.60, "size": 1.0},
        },
    )
    client.cancel_order.side_effect = [{}, RuntimeError("second cancel failed")]
    surviving = [resting[1]]
    # First fetch detects the surviving sibling. The verified cancel-all's
    # mandatory second fetch still sees it, so the emergency safeguard must
    # raise and stop the bot rather than resume with unconfirmed exposure.
    client.get_open_orders.side_effect = [surviving, surviving]

    with pytest.raises(EmergencySafeguardFailedError):
        bot._check_fast_repricing()

    # The one raised cancellation -- not the mere fact that a cancellation
    # was attempted -- is what makes the group enter REST verification.
    assert client.get_open_orders.call_count == 2
    client.cancel_all.assert_called_once_with(open_orders=surviving)


def test_check_fast_repricing_skips_orders_with_no_current_bbo(monkeypatch):
    bot, client, *_rest = _bot(monkeypatch)
    bot.private_store.seed_open_orders([
        {"marketSlug": "unwatched", "price": 0.40, "orderId": "o1"},
    ])
    monkeypatch.setattr(
        ws_runner_module, "get_known_order_details",
        lambda: {"o1": {"market_id": "unwatched", "side": "BUY", "price": 0.40, "size": 1.0}},
    )

    bot._check_fast_repricing()

    client.cancel_order.assert_not_called()


def test_wait_with_fast_repricing_disabled_is_a_plain_single_wait(monkeypatch):
    bot, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, fast_reprice_enabled=False)
    bot._check_fast_repricing = Mock()
    bot._stop_event = Mock()
    bot._stop_event.is_set.return_value = False

    bot._wait_with_fast_repricing(0.05)

    bot._check_fast_repricing.assert_not_called()
    bot._stop_event.wait.assert_called_once_with(timeout=0.05)


def test_wait_with_fast_repricing_enabled_checks_more_often_than_the_full_interval(monkeypatch):
    bot, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(
        bot.settings, fast_reprice_enabled=True, fast_reprice_check_seconds=0.02,
    )
    bot._check_fast_repricing = Mock()

    bot._wait_with_fast_repricing(0.08)

    # The full quote-refresh cadence (refresh_interval_seconds) is untouched
    # -- only this cheap, cancel-only check runs more often, roughly every
    # fast_reprice_check_seconds within the requested total wait.
    assert bot._check_fast_repricing.call_count >= 2


def test_pilot_in_drain_false_when_not_pilot_mode(monkeypatch):
    bot, *_rest = _bot(monkeypatch)
    assert bot._pilot_in_drain() is False


def test_pilot_in_drain_false_before_entry_window_elapses(monkeypatch):
    bot, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, pilot_mode=True, pilot_entry_hours=1.0)
    bot._pilot_started_monotonic = time.monotonic()
    assert bot._pilot_in_drain() is False


def test_pilot_in_drain_true_once_entry_window_elapses(monkeypatch):
    bot, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, pilot_mode=True, pilot_entry_hours=0.0)
    bot._pilot_started_monotonic = time.monotonic() - 1.0
    assert bot._pilot_in_drain() is True


def _run_pilot_loop_for_n_cycles(bot, client, n: int, monkeypatch) -> Mock:
    """Runs bot.run_forever() for real, stopping deterministically after `n`
    real _run_one_cycle iterations -- the established pattern in this file
    for exercising the actual while loop instead of only its extracted
    pieces. Returns the Mock installed over _maybe_refresh_candidates so
    call_count can be asserted."""
    client.get_open_orders.return_value = []
    client.get_all_positions.return_value = {}
    monkeypatch.setattr("polymarket_bot.live.ws_runner.recover_from_prior_crash", Mock())
    monkeypatch.setattr(bot, "_start_ws_thread", Mock())
    monkeypatch.setattr(bot, "_start_private_ws_thread", Mock())
    monkeypatch.setattr(bot, "_finish_pilot_flat", Mock())
    monkeypatch.setattr(bot, "_record_pilot_acceptance", Mock())
    refresh_mock = Mock()
    monkeypatch.setattr(bot, "_maybe_refresh_candidates", refresh_mock)

    call_count = {"n": 0}
    original_run_one_cycle = bot._run_one_cycle

    def _counted(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= n:
            bot._stop_event.set()
        return original_run_one_cycle(*args, **kwargs)

    monkeypatch.setattr(bot, "_run_one_cycle", _counted)

    bot.run_forever()

    assert call_count["n"] >= n
    return refresh_mock


def test_maybe_refresh_candidates_runs_each_iteration_before_drain(monkeypatch):
    bot, client, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(
        bot.settings,
        pilot_mode=True,
        enable_private_websocket=True,
        pilot_entry_hours=10.0,  # nowhere near draining
        pilot_drain_minutes=30.0,
        refresh_interval_seconds=0.01,
    )

    refresh_mock = _run_pilot_loop_for_n_cycles(bot, client, 2, monkeypatch)

    # One call before the loop starts, plus one per loop iteration while not
    # draining.
    assert refresh_mock.call_count >= 3


def test_maybe_refresh_candidates_is_skipped_once_pilot_is_draining(monkeypatch):
    bot, client, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(
        bot.settings,
        pilot_mode=True,
        enable_private_websocket=True,
        pilot_entry_hours=0.0,  # already in drain from the first loop iteration
        pilot_drain_minutes=30.0,
        refresh_interval_seconds=0.01,
    )

    refresh_mock = _run_pilot_loop_for_n_cycles(bot, client, 2, monkeypatch)

    # Exactly the one pre-loop call (before pilot markers even exist) --
    # every in-loop call is skipped because the pilot is already draining,
    # so shutdown-critical reconciliation isn't competing with a scan for
    # rate-limit budget.
    assert refresh_mock.call_count == 1


def _cycle(market_id: str, bid_resting: bool = True, ask_resting: bool = True) -> LiveQuoteCycle:
    bid = PostedLeg(
        side="BUY", price=0.49, size=1.0 if bid_resting else 0.0,
        order_id="bid-1" if bid_resting else None,
    )
    ask = PostedLeg(
        side="SELL", price=0.51, size=1.0 if ask_resting else 0.0,
        order_id="ask-1" if ask_resting else None,
    )
    return LiveQuoteCycle(
        cycle_id="c1", market_id=market_id, reference_price=0.5, tick_size=0.01,
        bid=bid, ask=ask, timestamp="2026-08-10T00:00:00+00:00",
    )


def test_run_one_cycle_pins_pilot_shadow_allocation_to_actually_posted_markets(monkeypatch):
    bot, _client, _market_ws, maker, *_rest = _bot(monkeypatch)
    bot._refresh_candidates()
    bot.settings = dataclasses.replace(
        bot.settings, pilot_mode=True, pilot_strategy_profile="july5_style",
    )
    # refresh_quotes() returns the real outcome: m1 got a resting order, m2's
    # candidate was entirely skipped (e.g. depth/edge/budget), and m3 -- an
    # orphaned held position that was never in the input candidate list at
    # all -- got a reducing order posted.
    maker.refresh_quotes.return_value = [
        _cycle("m1", bid_resting=True, ask_resting=False),
        _cycle("m2", bid_resting=False, ask_resting=False),
        _cycle("m3", bid_resting=False, ask_resting=True),
    ]

    bot._run_one_cycle()

    # Reflects what was ACTUALLY posted/retained (m1, m3) -- not the
    # pre-execution candidate list (m1, m2) refresh_quotes() was called
    # with, and not blind to m3, which was never a candidate at all.
    assert bot.observation_tracker._active["july5_style"] == {"m1", "m3"}


def test_run_one_cycle_does_not_pin_shadow_allocation_during_drain(monkeypatch):
    bot, *_rest = _bot(monkeypatch)
    bot._refresh_candidates()
    bot.settings = dataclasses.replace(
        bot.settings, pilot_mode=True, pilot_strategy_profile="july5_style",
    )
    bot.observation_tracker.override_profile_allocation = Mock()

    bot._run_one_cycle(drain_only=True)

    bot.observation_tracker.override_profile_allocation.assert_not_called()


def test_run_one_cycle_does_not_pin_shadow_allocation_outside_pilot_mode(monkeypatch):
    bot, *_rest = _bot(monkeypatch)
    bot._refresh_candidates()
    bot.observation_tracker.override_profile_allocation = Mock()

    bot._run_one_cycle()

    bot.observation_tracker.override_profile_allocation.assert_not_called()


def test_run_one_cycle_pin_failure_does_not_block_trading(monkeypatch):
    bot, _client, _market_ws, maker, *_rest = _bot(monkeypatch)
    bot._refresh_candidates()
    bot.settings = dataclasses.replace(
        bot.settings, pilot_mode=True, pilot_strategy_profile="july5_style",
    )
    maker.refresh_quotes.return_value = [_cycle("m1")]
    bot.observation_tracker.override_profile_allocation = Mock(
        side_effect=RuntimeError("boom"),
    )

    bot._run_one_cycle()  # must not raise

    maker.refresh_quotes.assert_called_once()
    bot.observation_tracker.override_profile_allocation.assert_called_once_with(
        "july5_style", ["m1"],
    )


def test_pilot_force_flat_shutdown_uses_reducing_only_and_verifies_flat(monkeypatch):
    bot, client, _market_ws, maker, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, pilot_mode=True)
    client.get_open_orders.return_value = []
    client.get_all_positions.return_value = {}
    monkeypatch.setattr("polymarket_bot.live.ws_runner.time.sleep", lambda _seconds: None)
    bot._persist_new_fills = Mock()
    bot._compute_due_markouts = Mock()

    bot._finish_pilot_flat()

    maker.refresh_quotes.assert_called_once()
    kwargs = maker.refresh_quotes.call_args.kwargs
    assert kwargs["candidates"] == []
    assert kwargs["drain_only"] is True
    assert kwargs["force_flatten_all"] is True


def test_pilot_shutdown_fails_closed_when_position_remains(monkeypatch):
    bot, client, _market_ws, maker, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(bot.settings, pilot_mode=True)
    client.get_open_orders.return_value = []
    client.get_all_positions.return_value = {
        "m1": {"netPositionDecimal": "1"},
    }
    monkeypatch.setattr("polymarket_bot.live.ws_runner.time.sleep", lambda _seconds: None)

    with pytest.raises(PilotFlatnessError, match="could not be verified flat"):
        bot._finish_pilot_flat()

    assert maker.refresh_quotes.call_count == 3


def test_pilot_acceptance_cross_checks_real_round_trip_and_shadow_markout(
    tmp_path, monkeypatch,
):
    bot, _client, _market_ws, _maker, breaker, *_rest = _bot(monkeypatch)
    bot._pilot_start_fill_count = 0
    bot._pilot_start_epoch = 1000.0
    bot.session_circuit_breaker.is_halted.return_value = False
    breaker.is_halted.return_value = False
    bot.observation_tracker.session_shadow_markouts = Mock(return_value={
        "avg_markout_5m_cents": 1.5, "sample_count": 2,
    })
    fills = [
        {
            "market_slug": "m1", "outcome": "YES", "side": "BUY",
            "shares": 1.0, "markout_5m_cents": 1.0,
        },
        {
            "market_slug": "m1", "outcome": "YES", "side": "SELL",
            "shares": 1.0, "markout_5m_cents": 2.0,
        },
    ]
    monkeypatch.setattr(ws_runner_module, "get_all_fills", lambda: fills)
    monkeypatch.setattr(ws_runner_module, "flat_round_trip_fill_pnl", lambda _fills: 0.02)
    monkeypatch.setattr(
        ws_runner_module, "PILOT_RESULTS_FILE", tmp_path / "pilot_results.json",
    )

    bot._record_pilot_acceptance(verified_flat=True)

    records = ws_runner_module.storage.load_json(
        ws_runner_module.PILOT_RESULTS_FILE, default=[],
    )
    assert records[-1]["status"] == "ACCEPTED"
    assert records[-1]["completed_real_round_trip_count"] == 1
    assert records[-1]["markouts_reasonably_match"] is True
    assert records[-1]["comparison_status"] == "MATCH"
    assert records[-1]["shadow_comparison_profile"] == "controlled"
    assert records[-1]["automatic_scaling_permitted"] is False
    bot.observation_tracker.session_shadow_markouts.assert_called_once_with(
        "controlled", 1000.0,
    )


def test_pilot_acceptance_mechanically_valid_but_not_accepted_when_unprofitable(
    tmp_path, monkeypatch,
):
    """Regression test: a session that's mechanically clean (real fills, a
    completed round trip, verified-flat shutdown, no breaker breach,
    matching markouts) but not profitable must not be reported as
    ACCEPTED -- matching the first real pilot's actual -$0.10 result,
    which the old accepted=(...) check never gated on fill_pnl > 0."""
    bot, _client, _market_ws, _maker, breaker, *_rest = _bot(monkeypatch)
    bot._pilot_start_fill_count = 0
    bot._pilot_start_epoch = 1000.0
    bot.session_circuit_breaker.is_halted.return_value = False
    breaker.is_halted.return_value = False
    bot.observation_tracker.session_shadow_markouts = Mock(return_value={
        "avg_markout_5m_cents": 1.5, "sample_count": 2,
    })
    fills = [
        {
            "market_slug": "m1", "outcome": "YES", "side": "BUY",
            "shares": 1.0, "markout_5m_cents": 1.0,
        },
        {
            "market_slug": "m1", "outcome": "YES", "side": "SELL",
            "shares": 1.0, "markout_5m_cents": 2.0,
        },
    ]
    monkeypatch.setattr(ws_runner_module, "get_all_fills", lambda: fills)
    monkeypatch.setattr(ws_runner_module, "flat_round_trip_fill_pnl", lambda _fills: -0.10)
    monkeypatch.setattr(
        ws_runner_module, "PILOT_RESULTS_FILE", tmp_path / "pilot_results.json",
    )

    bot._record_pilot_acceptance(verified_flat=True)

    records = ws_runner_module.storage.load_json(
        ws_runner_module.PILOT_RESULTS_FILE, default=[],
    )
    assert records[-1]["status"] == "MECHANICALLY_VALID"
    assert records[-1]["flat_round_trip_fill_pnl_usd"] == pytest.approx(-0.10)


def test_pilot_acceptance_requires_verified_flat_shutdown(tmp_path, monkeypatch):
    bot, _client, _market_ws, _maker, breaker, *_rest = _bot(monkeypatch)
    bot._pilot_start_fill_count = 0
    bot._pilot_start_epoch = 1000.0
    bot.session_circuit_breaker.is_halted.return_value = False
    breaker.is_halted.return_value = False
    bot.observation_tracker.session_shadow_markouts = Mock(return_value={
        "avg_markout_5m_cents": 1.0, "sample_count": 2,
    })
    fills = [
        {"market_slug": "m1", "outcome": "YES", "side": "BUY", "shares": 1.0, "markout_5m_cents": 1.0},
        {"market_slug": "m1", "outcome": "YES", "side": "SELL", "shares": 1.0, "markout_5m_cents": 1.0},
    ]
    monkeypatch.setattr(ws_runner_module, "get_all_fills", lambda: fills)
    monkeypatch.setattr(ws_runner_module, "flat_round_trip_fill_pnl", lambda _fills: 0.02)
    monkeypatch.setattr(
        ws_runner_module, "PILOT_RESULTS_FILE", tmp_path / "pilot_results.json",
    )

    bot._record_pilot_acceptance(verified_flat=False)

    records = ws_runner_module.storage.load_json(
        ws_runner_module.PILOT_RESULTS_FILE, default=[],
    )
    assert records[-1]["status"] == "REVIEW_REQUIRED"
    assert records[-1]["verified_flat_shutdown"] is False


def test_pilot_acceptance_uses_july5_session_shadow_not_lifetime_controlled(
    tmp_path, monkeypatch,
):
    bot, _client, _market_ws, _maker, breaker, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(
        bot.settings,
        pilot_strategy_profile="july5_style",
        pilot_qualification_bypassed=True,
    )
    bot._pilot_start_fill_count = 1
    bot._pilot_start_epoch = 5000.0
    bot.session_circuit_breaker.is_halted.return_value = False
    breaker.is_halted.return_value = False
    bot.observation_tracker.session_shadow_markouts = Mock(return_value={
        "avg_markout_5m_cents": 1.0, "sample_count": 2,
    })
    historical = {
        "market_slug": "old", "outcome": "YES", "side": "BUY",
        "shares": 99.0, "markout_5m_cents": -99.0,
    }
    session_fills = [
        {"market_slug": "m1", "outcome": "YES", "side": "BUY", "shares": 1.0, "markout_5m_cents": 1.0},
        {"market_slug": "m1", "outcome": "YES", "side": "SELL", "shares": 1.0, "markout_5m_cents": 1.0},
    ]
    monkeypatch.setattr(
        ws_runner_module, "get_all_fills", lambda: [historical, *session_fills],
    )
    monkeypatch.setattr(ws_runner_module, "flat_round_trip_fill_pnl", lambda fills: 0.01 if fills == session_fills else None)
    monkeypatch.setattr(
        ws_runner_module, "PILOT_RESULTS_FILE", tmp_path / "pilot_results.json",
    )

    bot._record_pilot_acceptance(verified_flat=True)

    result = ws_runner_module.storage.load_json(
        ws_runner_module.PILOT_RESULTS_FILE, default=[],
    )[-1]
    assert result["status"] == "ACCEPTED"
    assert result["real_fill_count"] == 2
    assert result["qualification_bypassed"] is True
    assert result["shadow_comparison_profile"] == "july5_style"
    bot.observation_tracker.session_shadow_markouts.assert_called_once_with(
        "july5_style", 5000.0,
    )


def test_pilot_acceptance_is_not_started_without_session_markers(
    tmp_path, monkeypatch,
):
    bot, _client, _market_ws, _maker, _breaker, *_rest = _bot(monkeypatch)
    bot.settings = dataclasses.replace(
        bot.settings,
        pilot_mode=True,
        pilot_strategy_profile="july5_style",
        pilot_qualification_bypassed=True,
    )
    historical_lookup = Mock(return_value=[{
        "market_slug": "old", "side": "BUY", "shares": 50.0,
    }])
    monkeypatch.setattr(ws_runner_module, "get_all_fills", historical_lookup)
    bot.observation_tracker.session_shadow_markouts = Mock()
    monkeypatch.setattr(
        ws_runner_module, "PILOT_RESULTS_FILE", tmp_path / "pilot_results.json",
    )

    bot._record_pilot_acceptance(verified_flat=True)

    result = ws_runner_module.storage.load_json(
        ws_runner_module.PILOT_RESULTS_FILE, default=[],
    )[-1]
    assert result["pilot_status"] == "NOT_STARTED"
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["real_fill_count"] == 0
    assert result["comparison_status"] == "UNAVAILABLE"
    historical_lookup.assert_not_called()
    bot.observation_tracker.session_shadow_markouts.assert_not_called()


def test_pilot_acceptance_marks_comparison_unavailable_without_session_shadow(
    tmp_path, monkeypatch,
):
    bot, _client, _market_ws, _maker, breaker, *_rest = _bot(monkeypatch)
    bot._pilot_start_fill_count = 0
    bot._pilot_start_epoch = 1000.0
    breaker.is_halted.return_value = False
    bot.session_circuit_breaker.is_halted.return_value = False
    bot.observation_tracker.session_shadow_markouts = Mock(return_value={
        "avg_markout_5m_cents": None, "sample_count": 0,
    })
    fills = [
        {"market_slug": "m1", "outcome": "YES", "side": "BUY", "shares": 1.0, "markout_5m_cents": 1.0},
        {"market_slug": "m1", "outcome": "YES", "side": "SELL", "shares": 1.0, "markout_5m_cents": 1.0},
    ]
    monkeypatch.setattr(ws_runner_module, "get_all_fills", lambda: fills)
    monkeypatch.setattr(ws_runner_module, "flat_round_trip_fill_pnl", lambda _fills: 0.01)
    monkeypatch.setattr(
        ws_runner_module, "PILOT_RESULTS_FILE", tmp_path / "pilot_results.json",
    )

    bot._record_pilot_acceptance(verified_flat=True)

    result = ws_runner_module.storage.load_json(
        ws_runner_module.PILOT_RESULTS_FILE, default=[],
    )[-1]
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["comparison_status"] == "UNAVAILABLE"
    assert result["markouts_reasonably_match"] is False


def test_compute_activity_scores_only_covers_currently_watched_candidates(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()  # populates _candidates with m1/m2 (the default selector mock)

    scores = bot._compute_activity_scores()

    assert set(scores.keys()) == {"m1", "m2"}


def test_compute_activity_scores_blends_trades_growth_and_frequency(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()
    # m1: 3+ real trades (saturates the trade component), positive
    # sharesTraded growth, and frequent book updates -- should score near 1.0.
    for _ in range(3):
        bot.store.update_message({"trade": {
            "marketSlug": "m1", "price": {"value": "0.5"}, "quantity": {"value": "1"},
        }})
    bot.store.update_message({"marketData": {"marketSlug": "m1", "stats": {"sharesTraded": "100"}}})
    bot.store.update_message({"marketData": {"marketSlug": "m1", "stats": {"sharesTraded": "150"}}})
    for _ in range(5):
        bot.store.update_message({"marketData": {
            "marketSlug": "m1",
            "bids": [{"px": {"value": "0.49"}, "qty": "10"}],
            "offers": [{"px": {"value": "0.51"}, "qty": "10"}],
        }})
    # m2: no activity observed at all.

    scores = bot._compute_activity_scores()

    assert scores["m1"] > scores["m2"]
    assert scores["m2"] == 0.0


def test_compute_activity_scores_empty_when_tracking_disabled(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()
    bot.settings = dataclasses.replace(bot.settings, activity_tracking_enabled=False)
    bot.store.update_message({"trade": {
        "marketSlug": "m1", "price": {"value": "0.5"}, "quantity": {"value": "1"},
    }})

    assert bot._compute_activity_scores() == {}


def test_get_candidates_immediately_prioritizes_observed_trade_activity(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()  # selector order is m1, then m2
    bot.store.update_message({"trade": {
        "marketSlug": "m2", "price": {"value": "0.5"}, "quantity": {"value": "4"},
    }})

    candidates = bot._get_candidates()

    assert candidates[0].market.market_id == "m2"


def test_activity_tape_remains_on_when_activity_rerank_is_disabled(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()  # selector order is m1, then m2
    bot.settings = dataclasses.replace(
        bot.settings,
        activity_tracking_enabled=True,
        activity_rerank_enabled=False,
    )
    bot.store.update_message({"trade": {
        "marketSlug": "m2", "price": {"value": "0.5"},
        "quantity": {"value": "4"},
    }})

    candidates = bot._get_candidates()

    assert [item.market.market_id for item in candidates] == ["m1", "m2"]
    assert bot.store.recent_trade_count("m2") == 1


def test_observation_gate_filters_flat_entry_candidates(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()
    bot.settings = dataclasses.replace(bot.settings, observation_gate_enabled=True)
    bot.observation_tracker.entry_eligible = Mock(
        side_effect=lambda slug: (slug == "m2", [] if slug == "m2" else ["no evidence"]),
    )

    candidates = bot._get_candidates()

    assert [item.market.market_id for item in candidates] == ["m2"]


def test_disabled_observation_gate_never_calls_entry_qualification(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()
    bot.settings = dataclasses.replace(
        bot.settings,
        observation_only_mode=False,
        observation_gate_enabled=False,
    )
    bot.observation_tracker.entry_eligible = Mock(return_value=(False, ["blocked"]))

    candidates = bot._get_candidates()

    assert [item.market.market_id for item in candidates] == ["m1", "m2"]
    bot.observation_tracker.entry_eligible.assert_not_called()


def test_observation_only_mode_returns_no_new_entry_candidates(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()
    bot.settings = dataclasses.replace(bot.settings, observation_only_mode=True)

    assert bot._get_candidates() == []


def test_refresh_candidates_threads_activity_scores_into_selector(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()  # populates _candidates with m1/m2
    bot.store.update_message({"trade": {
        "marketSlug": "m1", "price": {"value": "0.5"}, "quantity": {"value": "1"},
    }})
    selector.reset_mock()

    bot._refresh_candidates()

    activity_scores = selector.call_args.kwargs["activity_scores"]
    assert activity_scores["m1"] > 0.0


def test_refresh_candidates_does_not_rerank_when_only_tracking_is_enabled(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()
    bot.store.update_message({"trade": {
        "marketSlug": "m1", "price": {"value": "0.5"},
        "quantity": {"value": "1"},
    }})
    bot.settings = dataclasses.replace(
        bot.settings,
        activity_tracking_enabled=True,
        activity_rerank_enabled=False,
    )
    selector.reset_mock()

    bot._refresh_candidates()

    assert selector.call_args.kwargs["activity_scores"] == {}
    assert bot.store.recent_trade_count("m1") == 1


def test_refresh_candidates_subscribes_websocket(monkeypatch):
    bot, _client, market_ws, _maker, _breaker, selector, _equity_protection = _bot(monkeypatch)

    bot._refresh_candidates()

    selector.assert_called_once_with(
        settings=bot.settings,
        max_targets=bot.settings.websocket_candidate_pool_size,
        raw_by_slug_out={},
        activity_scores={},
        observation_markets_out=[],
    )
    market_ws.set_market_slugs.assert_called_once_with(["m1", "m2"])


def test_refresh_candidates_survives_set_market_slugs_failure(monkeypatch):
    # An ordinary WS reconnect race (the connection drops between reading
    # self._ws and calling ws.send(...)) makes set_market_slugs() raise by
    # design, so a later call retries the failed slugs. This must never
    # propagate out of _refresh_candidates() -- it's called directly from
    # run_forever()'s main loop, which has no handler for anything but
    # KeyboardInterrupt, so an uncaught exception here used to crash the
    # whole bot process on what is an ordinary, expected event.
    bot, _client, market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    market_ws.set_market_slugs.side_effect = RuntimeError("WebSocketConnectionClosedException")

    bot._refresh_candidates()  # must not raise

    # Candidates are still updated even though the WS subscription failed --
    # only the subscription send itself is best-effort here.
    assert [c.market_id for c in bot._get_candidates()] == ["m1", "m2"]


def test_maybe_refresh_candidates_survives_set_market_slugs_failure(monkeypatch):
    # Same failure, exercised through the actual call path run_forever()
    # uses (_maybe_refresh_candidates -> _refresh_candidates), to confirm
    # the fix covers the real entry point, not just a direct unit call.
    bot, _client, market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    market_ws.set_market_slugs.side_effect = RuntimeError("WebSocketConnectionClosedException")

    _maybe_refresh_and_join(bot)  # must not raise


def test_refresh_candidates_survives_select_target_markets_failure(monkeypatch):
    # select_target_markets() sits right next to set_market_slugs() in this
    # same function -- a transient market-scan failure (bad API response,
    # network error, a parsing edge case in one of hundreds of scanned
    # markets) used to be just as capable of crashing the whole bot process,
    # since _refresh_candidates() is called directly from run_forever()'s
    # main loop, which has no handler for anything but KeyboardInterrupt.
    bot, _client, market_ws, _maker, _breaker, selector, _equity_protection = _bot(monkeypatch)
    # Seed a real prior candidate list so the "left untouched on failure"
    # behavior below is actually exercised, not just trivially true of an
    # empty default.
    bot._refresh_candidates()
    assert [c.market_id for c in bot._get_candidates()] == ["m1", "m2"]
    selector.side_effect = RuntimeError("500 Server Error")

    bot._refresh_candidates()  # must not raise

    # Previous cycle's candidates are preserved, not cleared, so a still-
    # tradeable market isn't dropped just because this one refresh failed.
    assert [c.market_id for c in bot._get_candidates()] == ["m1", "m2"]
    market_ws.set_market_slugs.assert_called_once()  # only from the FIRST, successful refresh


def test_maybe_refresh_candidates_survives_select_target_markets_failure(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, selector, _equity_protection = _bot(monkeypatch)
    selector.side_effect = RuntimeError("500 Server Error")

    _maybe_refresh_and_join(bot)  # must not raise


def test_refresh_candidates_pool_is_independent_of_order_budget(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, selector, _equity_protection = _bot(monkeypatch)
    bot.settings = dataclasses.replace(
        bot.settings,
        max_orders_per_cycle=4,
        websocket_candidate_pool_size=20,
        websocket_subscription_limit=100,
    )

    bot._refresh_candidates()

    selector.assert_called_once_with(
        settings=bot.settings, max_targets=20, raw_by_slug_out={}, activity_scores={},
        observation_markets_out=[],
    )


def test_refresh_candidates_uses_broad_rotating_observation_universe(monkeypatch):
    def selector(
        settings, max_targets=None, raw_by_slug_out=None, activity_scores=None,
        observation_markets_out=None,
    ):
        observation_markets_out.extend([_scored(f"m{i}") for i in range(1, 7)])
        return [_scored("m1")]

    bot, _client, market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr("polymarket_bot.live.ws_runner.select_target_markets", selector)
    bot.settings = dataclasses.replace(bot.settings, observation_universe_size=3)

    bot._refresh_candidates()
    bot._refresh_candidates()

    first, second = [call.args[0] for call in market_ws.set_market_slugs.call_args_list]
    assert first == ["m1", "m2", "m3"]
    assert second == ["m1", "m4", "m5"]


def test_refresh_candidates_prioritizes_aged_out_shadow_inventory(monkeypatch):
    def selector(
        settings, max_targets=None, raw_by_slug_out=None, activity_scores=None,
        observation_markets_out=None,
    ):
        observation_markets_out.extend([_scored(f"m{i}") for i in range(1, 5)])
        return []

    bot, _client, market_ws, _maker, _breaker, _selector, _equity = _bot(monkeypatch)
    monkeypatch.setattr("polymarket_bot.live.ws_runner.select_target_markets", selector)
    bot.settings = dataclasses.replace(
        bot.settings,
        observation_only_mode=True,
        observation_universe_size=3,
    )
    bot.observation_tracker.open_inventory_slugs = Mock(
        return_value={"held-old"},
    )

    assert bot._refresh_candidates() is True

    market_ws.set_market_slugs.assert_called_once_with(
        ["held-old", "m1", "m2"],
    )


def test_observation_feed_stall_reconnects_twice_then_stops(monkeypatch):
    bot, *_rest = _bot(monkeypatch)
    bot.observation_tracker.feed_health = Mock(return_value={
        "required": True,
        "stalled": True,
        "reason": "silent_feed",
        "age_seconds": 301.0,
        "candidate_count": 100,
        "latest_activity_epoch": 1000.0,
    })
    now = {"value": 2000.0}
    monkeypatch.setattr(ws_runner_module.time, "time", lambda: now["value"])

    bot._abort_if_observation_feed_stalled()
    assert bot.market_ws.force_reconnect.call_count == 1

    now["value"] = 2301.0
    bot._abort_if_observation_feed_stalled()
    assert bot.market_ws.force_reconnect.call_count == 2

    now["value"] = 2602.0
    with pytest.raises(ObservationFeedStalledError, match="Two clean reconnect"):
        bot._abort_if_observation_feed_stalled()


def test_observation_feed_activity_resets_watchdog_recovery(monkeypatch):
    bot, *_rest = _bot(monkeypatch)
    health = {
        "required": True,
        "stalled": True,
        "reason": "silent_feed",
        "age_seconds": 301.0,
        "candidate_count": 100,
        "latest_activity_epoch": 1000.0,
    }
    bot.observation_tracker.feed_health = Mock(side_effect=lambda: dict(health))
    monkeypatch.setattr(ws_runner_module.time, "time", lambda: 2000.0)

    bot._abort_if_observation_feed_stalled()
    health.update(stalled=False, age_seconds=1.0, latest_activity_epoch=2001.0)
    bot._abort_if_observation_feed_stalled()

    assert bot._market_feed_recovery_attempts == 0
    assert bot._market_feed_recovery_started_epoch is None


def test_empty_observation_universe_stops_instead_of_running_for_days(monkeypatch):
    bot, *_rest = _bot(monkeypatch)
    bot.observation_tracker.feed_health = Mock(return_value={
        "required": True,
        "stalled": True,
        "reason": "empty_candidate_pool",
        "age_seconds": 301.0,
        "candidate_count": 0,
    })

    with pytest.raises(ObservationFeedStalledError, match="universe remained empty"):
        bot._abort_if_observation_feed_stalled()


def test_refresh_candidates_allows_live_l2_evidence_for_static_broad_market(
    monkeypatch,
):
    broad = _scored("m2")
    broad.market.token_ids = ["yes", "no"]
    broad.market.outcomes = ["Yes", "No"]

    def selector(
        settings, max_targets=None, raw_by_slug_out=None, activity_scores=None,
        observation_markets_out=None,
    ):
        observation_markets_out.extend([_scored("m1"), broad])
        return [_scored("m1")]

    bot, *_rest = _bot(monkeypatch)
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.select_target_markets", selector,
    )
    bot.observation_tracker.set_live_candidate_slugs = Mock()

    bot._refresh_candidates()

    bot.observation_tracker.set_live_candidate_slugs.assert_called_once_with(
        ["m1", "m2"],
    )


def test_refresh_candidates_retains_recently_active_observation_market(monkeypatch):
    def selector(
        settings, max_targets=None, raw_by_slug_out=None, activity_scores=None,
        observation_markets_out=None,
    ):
        observation_markets_out.extend([_scored(f"m{i}") for i in range(1, 7)])
        return [_scored("m1")]

    bot, _client, market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr("polymarket_bot.live.ws_runner.select_target_markets", selector)
    bot.settings = dataclasses.replace(
        bot.settings,
        observation_universe_size=3,
        observation_retained_active_slots=1,
        observation_active_hold_seconds=7200,
    )

    bot._refresh_candidates()
    bot.store.update_message({"trade": {
        "marketSlug": "m2",
        "price": {"value": "0.5"},
        "quantity": {"value": "3"},
    }})
    bot._refresh_candidates()

    second = market_ws.set_market_slugs.call_args_list[1].args[0]
    assert second[:2] == ["m1", "m2"]


def test_refresh_candidates_populates_extra_raw_by_slug(monkeypatch):
    def _selector(
        settings, max_targets=None, raw_by_slug_out=None, activity_scores=None,
        observation_markets_out=None,
    ):
        if raw_by_slug_out is not None:
            raw_by_slug_out["m1"] = {"marketType": "futures"}
        return [_scored("m1"), _scored("m2")]

    bot, _client, _market_ws, _maker, _breaker, _selector_mock, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr("polymarket_bot.live.ws_runner.select_target_markets", _selector)

    bot._refresh_candidates()

    assert bot._get_extra_raw_by_slug() == {"m1": {"marketType": "futures"}}


def test_run_one_cycle_passes_extra_raw_by_slug_to_refresh_quotes(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()

    bot._run_one_cycle()

    extra_raw = maker.refresh_quotes.call_args.kwargs["extra_raw_by_slug"]
    assert extra_raw == bot._get_extra_raw_by_slug()


def test_run_one_cycle_uses_current_candidates(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()

    bot._run_one_cycle()

    candidates = maker.refresh_quotes.call_args.kwargs["candidates"]
    assert [c.market_id for c in candidates] == ["m1", "m2"]


def test_run_one_cycle_observation_only_is_strictly_passive(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()
    bot.settings = dataclasses.replace(bot.settings, observation_only_mode=True)

    bot._run_one_cycle()

    maker.refresh_quotes.assert_not_called()
    _client.cancel_all.assert_not_called()


def test_run_one_cycle_skips_when_circuit_breaker_halted(monkeypatch):
    bot, _client, _market_ws, maker, breaker, _selector, _equity_protection = _bot(monkeypatch)
    breaker.evaluate.return_value = True

    bot._run_one_cycle()

    maker.refresh_quotes.assert_not_called()


def test_daily_circuit_breaker_uses_fill_corrected_pnl_when_it_disagrees_with_raw(
    monkeypatch,
):
    # The daily circuit breaker gets the same accuracy correction the
    # session breaker already had: the position-endpoint delta can
    # overstate profitability, so once today's fills round-trip flat, the
    # exact signed fill cashflow (a loss here) must be what actually
    # decides the breaker, not the misleadingly-profitable raw figure.
    bot, _client, _market_ws, maker, breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(ledger_module, "diff_against_baseline", lambda total_now, path: 10.0)
    fills_module.overwrite_fills([
        _seed_fill(seconds_ago=120, side="BUY", shares=10.0, price=0.20, commission_usd=0.10),
        _seed_fill(seconds_ago=60, side="SELL", shares=10.0, price=0.22, commission_usd=0.20),
    ])

    bot._run_one_cycle()

    assert breaker.evaluate.call_args.kwargs["total_pnl_usd"] == pytest.approx(-0.10)
    maker.refresh_quotes.assert_called_once()


class TestDailyFlatRoundTripFillPnl:
    def test_computes_exact_cashflow_for_todays_flat_round_trip(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([
            _seed_fill(seconds_ago=120, side="BUY", shares=10.0, price=0.20, commission_usd=0.10),
            _seed_fill(seconds_ago=60, side="SELL", shares=10.0, price=0.22, commission_usd=0.20),
        ])

        assert bot._daily_flat_round_trip_fill_pnl_usd() == pytest.approx(-0.10)

    def test_returns_none_when_todays_inventory_is_not_flat(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([
            _seed_fill(seconds_ago=60, side="BUY", shares=10.0, price=0.20, commission_usd=0.10),
        ])

        assert bot._daily_flat_round_trip_fill_pnl_usd() is None

    def test_excludes_fills_from_a_different_utc_day(self, monkeypatch):
        # A non-flat fill from yesterday must not suppress today's
        # correction (nor be mistaken for today's activity) -- the window
        # is UTC-date-scoped, not since-session-start.
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([
            _seed_fill(seconds_ago=90000, side="BUY", shares=10.0, price=0.20, commission_usd=0.10),
        ])

        assert bot._daily_flat_round_trip_fill_pnl_usd() == pytest.approx(0.0)


def test_persist_new_fills_records_new_executions(monkeypatch, tmp_path):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.already_recorded_fill_ids", lambda: set()
    )
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.get_known_order_details", lambda: {}
    )
    recorded = []
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.record_fill", lambda fill: recorded.append(fill)
    )
    bot.private_store.handle_message({
        "orderSubscriptionUpdate": {"execution": {
            "id": "exec-1", "type": "EXECUTION_TYPE_FILL", "lastShares": "5.0", "lastPx": {"value": "0.4"},
        }}
    })

    bot._persist_new_fills()

    assert len(recorded) == 1
    assert recorded[0].fill_id == "exec-1"


def test_persist_new_fills_skips_non_fill_execution_types(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.already_recorded_fill_ids", lambda: set()
    )
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.get_known_order_details", lambda: {}
    )
    recorded = []
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.record_fill", lambda fill: recorded.append(fill)
    )
    for exec_type in ("EXECUTION_TYPE_NEW", "EXECUTION_TYPE_CANCELED", "EXECUTION_TYPE_REJECTED"):
        bot.private_store.handle_message({
            "orderSubscriptionUpdate": {"execution": {"id": f"exec-{exec_type}", "type": exec_type}}
        })

    bot._persist_new_fills()

    assert recorded == []


def test_persist_new_fills_skips_already_recorded(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.already_recorded_fill_ids", lambda: {"exec-1"}
    )
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.get_known_order_details", lambda: {}
    )
    recorded = []
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.record_fill", lambda fill: recorded.append(fill)
    )
    bot.private_store.handle_message({
        "orderSubscriptionUpdate": {"execution": {
            "id": "exec-1", "type": "EXECUTION_TYPE_FILL", "lastShares": "5.0", "lastPx": {"value": "0.4"},
        }}
    })

    bot._persist_new_fills()

    assert recorded == []


def test_persist_new_fills_never_raises_out_of_run_one_cycle(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.already_recorded_fill_ids",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    bot._run_one_cycle()  # must not raise

    maker.refresh_quotes.assert_called_once()


def test_run_one_cycle_fails_closed_when_pnl_is_unavailable(monkeypatch):
    bot, client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.get_total_position_pnl_usd", lambda client: None,
    )

    bot._run_one_cycle()

    maker.refresh_quotes.assert_not_called()
    client.cancel_all.assert_called_once()


def test_cancel_all_resting_orders_falls_back_to_rest_when_private_ws_unhealthy(monkeypatch):
    # An emergency/shutdown cancel-all is often the bot's LAST action -- a
    # stale private-WS snapshot (never connected, or gone quiet past
    # private_state_stale_after_seconds) must not be trusted for it, or a
    # resting order the WS never learned about could survive the process
    # exit unmanaged. The store here is never marked connected, so
    # is_healthy() is False and cancel_all() must be called with no
    # override -- letting it fetch its own fresh REST snapshot -- instead
    # of the stale WS snapshot.
    bot, client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot.private_store.seed_open_orders([{"id": "stale-1", "marketSlug": "m1"}])

    bot._cancel_all_resting_orders(context="shutdown")

    client.cancel_all.assert_called_once_with()


def test_cancel_all_resting_orders_uses_ws_snapshot_when_healthy(monkeypatch):
    # The common case: a healthy private WS's snapshot is trustworthy and
    # is used directly, without an extra REST round-trip.
    bot, client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot.private_store.seed_open_orders([{"id": "resting-1", "marketSlug": "m1"}])
    bot.private_store.mark_connected()

    bot._cancel_all_resting_orders(context="shutdown")

    client.cancel_all.assert_called_once_with(
        open_orders=[{"id": "resting-1", "marketSlug": "m1"}]
    )


def test_pnl_uses_healthy_private_position_snapshot_without_rest(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    rest_pnl = Mock(side_effect=AssertionError("REST should not be used"))
    monkeypatch.setattr("polymarket_bot.live.ws_runner.get_total_position_pnl_usd", rest_pnl)
    bot.private_store.seed_positions({
        "m1": {
            "cost": {"value": "5.0"},
            "cashValue": {"value": "6.0"},
            "realized": {"value": "0.5"},
        }
    })
    bot.private_store.mark_connected()

    total_now, _daily, _session = bot._estimate_pnl_figures()

    assert total_now == pytest.approx(1.5)
    rest_pnl.assert_not_called()


def test_persist_new_fills_fetches_bbo_once_per_market_slug(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.already_recorded_fill_ids", lambda: set()
    )
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.get_known_order_details",
        lambda: {"order-1": {"market_id": "m1", "side": "BUY", "price": 0.4}},
    )
    monkeypatch.setattr("polymarket_bot.live.ws_runner.record_fill", lambda fill: None)
    maker.read_client.get_market_bbo.return_value = {"best_bid": 0.4, "best_ask": 0.42}
    bot.private_store.handle_message({
        "orderSubscriptionUpdate": {
            "execution": {
                "id": "exec-1", "type": "EXECUTION_TYPE_FILL", "orderId": "order-1",
                "lastShares": "5.0", "lastPx": {"value": "0.4"},
            }
        }
    })
    bot.private_store.handle_message({
        "orderSubscriptionUpdate": {
            "execution": {
                "id": "exec-2", "type": "EXECUTION_TYPE_FILL", "orderId": "order-1",
                "lastShares": "3.0", "lastPx": {"value": "0.41"},
            }
        }
    })

    bot._persist_new_fills()

    maker.read_client.get_market_bbo.assert_called_once_with("m1")


def test_run_one_cycle_skips_refresh_when_equity_protection_halted(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, equity_protection = _bot(monkeypatch)
    equity_protection.evaluate.return_value = (True, 1.0)

    bot._run_one_cycle()

    maker.refresh_quotes.assert_not_called()


def test_equity_protection_not_evaluated_when_circuit_breaker_already_halted(monkeypatch):
    bot, _client, _market_ws, maker, breaker, _selector, equity_protection = _bot(monkeypatch)
    breaker.evaluate.return_value = True

    bot._run_one_cycle()

    equity_protection.evaluate.assert_not_called()


def test_run_one_cycle_skips_when_session_circuit_breaker_halted(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot.session_circuit_breaker.evaluate.return_value = True

    bot._run_one_cycle()

    maker.refresh_quotes.assert_not_called()


def test_session_circuit_breaker_not_evaluated_when_daily_circuit_breaker_already_halted(monkeypatch):
    bot, _client, _market_ws, maker, breaker, _selector, _equity_protection = _bot(monkeypatch)
    breaker.evaluate.return_value = True

    bot._run_one_cycle()

    bot.session_circuit_breaker.evaluate.assert_not_called()


def test_equity_protection_not_evaluated_when_session_circuit_breaker_halted(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, equity_protection = _bot(monkeypatch)
    bot.session_circuit_breaker.evaluate.return_value = True

    bot._run_one_cycle()

    equity_protection.evaluate.assert_not_called()


def test_run_one_cycle_fetches_position_pnl_at_most_once_per_cycle(monkeypatch):
    bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    fetch = Mock(return_value=5.0)
    monkeypatch.setattr("polymarket_bot.live.ws_runner.get_total_position_pnl_usd", fetch)

    bot._run_one_cycle()

    fetch.assert_called_once()
    # Both breakers and equity protection must derive their figures from
    # that single fetch, not re-fetch independently.
    bot.circuit_breaker.evaluate.assert_called_once()
    bot.session_circuit_breaker.evaluate.assert_called_once()


class TestComputeBreakerRisk:
    def test_false_when_both_pnls_well_within_limits(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)

        assert bot._compute_breaker_risk(daily_pnl=-1.0, session_pnl=-1.0) is False

    def test_true_when_daily_pnl_crosses_warning_fraction(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        # breaker.settings.daily_loss_limit_usd is 8.0 (see _bot()); warning
        # fraction defaults to 0.75 -> threshold is -6.0.
        assert bot._compute_breaker_risk(daily_pnl=-6.5, session_pnl=0.0) is True

    def test_true_when_session_pnl_crosses_warning_fraction(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        # session_breaker.settings.loss_limit_usd is 8.0 -> threshold is -6.0.
        assert bot._compute_breaker_risk(daily_pnl=0.0, session_pnl=-6.5) is True

    def test_false_just_below_the_warning_threshold(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)

        assert bot._compute_breaker_risk(daily_pnl=-5.9, session_pnl=-5.9) is False


def test_run_one_cycle_passes_breaker_risk_to_refresh_quotes(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(bot, "_compute_breaker_risk", lambda daily_pnl, session_pnl: True)

    bot._run_one_cycle()

    assert maker.refresh_quotes.call_args.kwargs["breaker_risk"] is True


class TestMaybeRefreshCandidates:
    def test_refresh_runs_on_a_background_thread_and_does_not_block(self, monkeypatch):
        # A confirmed real incident: this synchronous call (a full ~5000-
        # market scan plus up to hundreds of L2 lookups) blocked the whole
        # quote loop for a median of 138s per occurrence, 38 times in one
        # run. Confirms the fix at the actual entry point: a slow
        # _refresh_candidates() must not make _maybe_refresh_candidates()
        # itself slow.
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot._refresh_candidates = Mock(side_effect=lambda: time.sleep(0.3) or True)

        started = time.monotonic()
        bot._maybe_refresh_candidates()
        elapsed = time.monotonic() - started

        assert elapsed < 0.1
        assert isinstance(bot._candidate_refresh_thread, threading.Thread)
        bot._candidate_refresh_thread.join(timeout=2)
        bot._refresh_candidates.assert_called_once()

    def test_does_not_spawn_a_second_thread_while_one_is_already_in_progress(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        release = threading.Event()
        bot._refresh_candidates = Mock(side_effect=lambda: release.wait(timeout=2) or True)
        bot._maybe_refresh_candidates()  # kicks off the (still-blocked) background thread
        first_thread = bot._candidate_refresh_thread

        bot._maybe_refresh_candidates()  # due again, but a refresh is already in progress

        assert bot._candidate_refresh_thread is first_thread
        assert bot._refresh_candidates.call_count == 1
        release.set()
        first_thread.join(timeout=2)

    def test_runs_on_first_call_when_not_halted(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot._refresh_candidates = Mock()

        _maybe_refresh_and_join(bot)

        bot._refresh_candidates.assert_called_once()

    def test_skips_before_timer_elapses(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot._refresh_candidates = Mock()
        _maybe_refresh_and_join(bot)  # first call, refreshes and sets the timestamp
        bot._refresh_candidates.reset_mock()

        _maybe_refresh_and_join(bot)  # immediately again -- timer not due

        bot._refresh_candidates.assert_not_called()

    def test_skips_when_daily_circuit_breaker_halted(self, monkeypatch):
        bot, _client, _market_ws, _maker, breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot._refresh_candidates = Mock()
        breaker.is_halted.return_value = True

        _maybe_refresh_and_join(bot)

        bot._refresh_candidates.assert_not_called()

    def test_skips_when_equity_protection_halted(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, equity_protection = _bot(monkeypatch)
        bot._refresh_candidates = Mock()
        equity_protection.is_halted.return_value = True

        _maybe_refresh_and_join(bot)

        bot._refresh_candidates.assert_not_called()

    def test_skips_when_session_circuit_breaker_halted(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot._refresh_candidates = Mock()
        bot.session_circuit_breaker.is_halted.return_value = True

        _maybe_refresh_and_join(bot)

        bot._refresh_candidates.assert_not_called()

    def test_forces_immediate_refresh_on_resume_even_if_timer_not_due(self, monkeypatch):
        bot, _client, _market_ws, _maker, breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot._refresh_candidates = Mock()
        breaker.is_halted.return_value = True
        _maybe_refresh_and_join(bot)  # halted -- skipped, _was_halted becomes True
        bot._refresh_candidates.assert_not_called()

        breaker.is_halted.return_value = False
        _maybe_refresh_and_join(bot)  # resumed -- forced refresh despite timer

        bot._refresh_candidates.assert_called_once()

    def test_updates_last_refresh_timestamp_only_when_it_actually_refreshes(self, monkeypatch):
        bot, _client, _market_ws, _maker, breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot._refresh_candidates = Mock()
        breaker.is_halted.return_value = True

        _maybe_refresh_and_join(bot)

        assert bot._last_candidate_refresh == 0.0  # never updated -- refresh was skipped

    def test_failed_refresh_does_not_advance_last_candidate_refresh(self, monkeypatch):
        # Previously _last_candidate_refresh advanced unconditionally, so a
        # caught scan failure still meant waiting the full
        # websocket_candidate_refresh_seconds (900s by default) before
        # retrying.
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot._refresh_candidates = Mock(return_value=False)

        _maybe_refresh_and_join(bot)

        assert bot._last_candidate_refresh == 0.0
        assert bot._last_candidate_refresh_failed is True

    def test_failed_refresh_does_not_retry_before_the_retry_interval_elapses(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot._refresh_candidates = Mock(return_value=False)
        _maybe_refresh_and_join(bot)  # first attempt, fails
        bot._refresh_candidates.reset_mock()

        _maybe_refresh_and_join(bot)  # immediately again -- neither interval elapsed

        bot._refresh_candidates.assert_not_called()

    def test_failed_refresh_retries_after_the_shorter_retry_interval_not_the_full_one(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = dataclasses.replace(
            bot.settings,
            websocket_candidate_refresh_seconds=900,
            websocket_candidate_refresh_retry_seconds=60,
        )
        bot._refresh_candidates = Mock(return_value=False)
        _maybe_refresh_and_join(bot)  # first attempt, fails
        bot._refresh_candidates.reset_mock()
        # Simulate 61s elapsed -- past the short retry interval, nowhere
        # near the full 900s one.
        bot._last_candidate_refresh_attempt -= 61.0

        _maybe_refresh_and_join(bot)

        bot._refresh_candidates.assert_called_once()

    def test_successful_retry_after_a_failure_clears_the_failed_flag_and_advances(self, monkeypatch):
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = dataclasses.replace(
            bot.settings,
            websocket_candidate_refresh_seconds=900,
            websocket_candidate_refresh_retry_seconds=60,
        )
        bot._refresh_candidates = Mock(return_value=False)
        _maybe_refresh_and_join(bot)  # first attempt, fails
        bot._last_candidate_refresh_attempt -= 61.0
        bot._refresh_candidates.return_value = True

        _maybe_refresh_and_join(bot)  # retries, succeeds

        assert bot._last_candidate_refresh_failed is False
        assert bot._last_candidate_refresh > 0.0


def test_size_multiplier_produces_scaled_settings_override(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, equity_protection = _bot(monkeypatch)
    equity_protection.evaluate.return_value = (False, 0.5)

    bot._run_one_cycle()

    override = maker.refresh_quotes.call_args.kwargs["settings_override"]
    assert override.order_shares_min == 8.0
    assert override.order_shares_max == 8.0
    assert override.max_orders_per_cycle == bot.settings.max_orders_per_cycle


def test_size_multiplier_of_one_passes_no_override(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, equity_protection = _bot(monkeypatch)
    equity_protection.evaluate.return_value = (False, 1.0)

    bot._run_one_cycle()

    assert maker.refresh_quotes.call_args.kwargs["settings_override"] is None


def _seed_fill(seconds_ago, **overrides):
    transact_time = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f000Z"
    )
    fill = {
        "fill_id": "f1",
        "market_slug": "m1",
        "side": "BUY",
        "price": 0.45,
        "transact_time": transact_time,
        "markout_1m_cents": None,
        "markout_1m_computed_at": None,
        "markout_5m_cents": None,
        "markout_5m_computed_at": None,
    }
    fill.update(overrides)
    return fill


class TestComputeDueMarkouts:
    def test_computes_1m_leaves_5m_alone_when_not_due(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([_seed_fill(seconds_ago=65)])
        maker.read_client.get_market_bbo.return_value = {"best_bid": 0.48, "best_ask": 0.50}

        bot._compute_due_markouts()

        fills = fills_module.get_all_fills()
        assert fills[0]["markout_1m_cents"] == pytest.approx(4.0)  # mid=0.49, fill=0.45, BUY -> +4c
        assert fills[0]["markout_1m_computed_at"] is not None
        assert fills[0]["markout_5m_cents"] is None
        assert fills[0]["markout_5m_computed_at"] is None

    def test_computes_15m_once_due_and_does_not_feed_toxicity_tracker(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([_seed_fill(
            seconds_ago=905,
            markout_1m_cents=1.0, markout_1m_computed_at="2026-01-01T00:00:00+00:00",
            markout_5m_cents=1.0, markout_5m_computed_at="2026-01-01T00:00:00+00:00",
        )])
        maker.read_client.get_market_bbo.return_value = {"best_bid": 0.48, "best_ask": 0.50}
        maker.toxicity_tracker.record_markout.reset_mock()

        bot._compute_due_markouts()

        fills = fills_module.get_all_fills()
        assert fills[0]["markout_15m_cents"] == pytest.approx(4.0)
        assert fills[0]["markout_15m_computed_at"] is not None
        maker.toxicity_tracker.record_markout.assert_not_called()

    def test_stale_window_resolves_to_none_with_computed_at_set(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([_seed_fill(seconds_ago=3600)])  # 1hr old, way past 15min staleness

        bot._compute_due_markouts()

        fills = fills_module.get_all_fills()
        assert fills[0]["markout_1m_cents"] is None
        assert fills[0]["markout_1m_computed_at"] is not None
        maker.read_client.get_market_bbo.assert_not_called()

    def test_bbo_unavailable_leaves_window_untouched_for_retry(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([_seed_fill(seconds_ago=65)])
        maker.read_client.get_market_bbo.return_value = None

        bot._compute_due_markouts()

        fills = fills_module.get_all_fills()
        assert fills[0]["markout_1m_cents"] is None
        assert fills[0]["markout_1m_computed_at"] is None  # not marked resolved -- retried next cycle

    def test_fetches_bbo_at_most_once_per_market_per_cycle(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([
            _seed_fill(seconds_ago=65, fill_id="f1", market_slug="m1"),
            _seed_fill(seconds_ago=65, fill_id="f2", market_slug="m1"),
        ])
        maker.read_client.get_market_bbo.return_value = {"best_bid": 0.48, "best_ask": 0.50}

        bot._compute_due_markouts()

        maker.read_client.get_market_bbo.assert_called_once_with("m1")

    def test_feeds_toxicity_tracker_once_per_new_1m_markout_not_5m(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([_seed_fill(
            seconds_ago=305, markout_1m_computed_at="2020-01-01T00:00:00+00:00",
        )])
        maker.read_client.get_market_bbo.return_value = {"best_bid": 0.48, "best_ask": 0.50}

        bot._compute_due_markouts()

        maker.toxicity_tracker.record_markout.assert_not_called()  # only 5m was due, not 1m

    def test_feeds_toxicity_tracker_for_newly_computed_1m_markout(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([_seed_fill(seconds_ago=65)])
        maker.read_client.get_market_bbo.return_value = {"best_bid": 0.48, "best_ask": 0.50}

        bot._compute_due_markouts()

        maker.toxicity_tracker.record_markout.assert_called_once_with("m1", pytest.approx(4.0))

    def test_no_ops_when_markout_tracking_disabled(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = config.LiveTradingSettings(markout_tracking_enabled=False)
        fills_module.overwrite_fills([_seed_fill(seconds_ago=65)])

        bot._compute_due_markouts()

        maker.read_client.get_market_bbo.assert_not_called()
        assert fills_module.get_all_fills()[0]["markout_1m_computed_at"] is None

    def test_computes_but_does_not_feed_tracker_when_toxicity_disabled(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = config.LiveTradingSettings(toxicity_tracking_enabled=False)
        fills_module.overwrite_fills([_seed_fill(seconds_ago=65)])
        maker.read_client.get_market_bbo.return_value = {"best_bid": 0.48, "best_ask": 0.50}

        bot._compute_due_markouts()

        assert fills_module.get_all_fills()[0]["markout_1m_cents"] == pytest.approx(4.0)
        maker.toxicity_tracker.record_markout.assert_not_called()

    def test_never_raises_out_of_run_one_cycle(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        monkeypatch.setattr(
            "polymarket_bot.live.ws_runner.get_all_fills",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        bot._run_one_cycle()  # must not raise

        maker.refresh_quotes.assert_called_once()

    def test_run_one_cycle_propagates_emergency_safeguard_failure(self, monkeypatch):
        # Unlike an ordinary refresh failure (the test above), an
        # EmergencySafeguardFailedError means the account-wide emergency
        # cancel-all could not be confirmed to have worked -- unconfirmed
        # live exposure after the last line of defense. This must NOT be
        # swallowed like a routine per-cycle failure; it has to reach
        # run_forever()'s top-level handler and stop the process.
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        maker.refresh_quotes.side_effect = EmergencySafeguardFailedError("not confirmed clean")

        with pytest.raises(EmergencySafeguardFailedError):
            bot._run_one_cycle()


class TestRunForeverStartupRecovery:
    def test_pilot_session_markers_are_set_only_after_runtime_setup(self, monkeypatch):
        bot, _client, _market_ws, _maker, *_rest = _bot(monkeypatch)
        bot.settings = dataclasses.replace(bot.settings, pilot_mode=True)
        monkeypatch.setattr(
            "polymarket_bot.live.ws_runner.recover_from_prior_crash", Mock(),
        )
        monkeypatch.setattr(bot, "_maybe_refresh_candidates", Mock())
        monkeypatch.setattr(bot, "_start_ws_thread", Mock())
        monkeypatch.setattr(bot, "_start_private_ws_thread", Mock())
        fill_lookup = Mock(return_value=[{"id": "historical"}])
        monkeypatch.setattr(ws_runner_module, "get_all_fills", fill_lookup)
        bot._run_one_cycle = Mock(side_effect=KeyboardInterrupt())
        bot._finish_pilot_flat = Mock()
        recorded = []
        bot._record_pilot_acceptance = Mock(
            side_effect=lambda flat: recorded.append((
                flat, bot._pilot_start_fill_count, bot._pilot_start_epoch,
            )),
        )

        bot.run_forever()

        assert fill_lookup.call_count == 1
        assert recorded and recorded[0][0] is True
        assert recorded[0][1] == 1
        assert isinstance(recorded[0][2], float)

    def test_pilot_setup_failure_records_not_started_without_history(
        self, tmp_path, monkeypatch,
    ):
        bot, _client, _market_ws, _maker, *_rest = _bot(monkeypatch)
        bot.settings = dataclasses.replace(
            bot.settings,
            pilot_mode=True,
            pilot_strategy_profile="july5_style",
            pilot_qualification_bypassed=True,
        )
        monkeypatch.setattr(
            "polymarket_bot.live.ws_runner.recover_from_prior_crash", Mock(),
        )
        monkeypatch.setattr(bot, "_maybe_refresh_candidates", Mock())
        monkeypatch.setattr(
            bot, "_start_ws_thread", Mock(side_effect=RuntimeError("setup failed")),
        )
        fill_lookup = Mock(return_value=[{"id": "historical"}])
        monkeypatch.setattr(ws_runner_module, "get_all_fills", fill_lookup)
        monkeypatch.setattr(
            ws_runner_module, "PILOT_RESULTS_FILE", tmp_path / "pilot_results.json",
        )
        bot._finish_pilot_flat = Mock()

        with pytest.raises(RuntimeError, match="setup failed"):
            bot.run_forever()

        result = ws_runner_module.storage.load_json(
            ws_runner_module.PILOT_RESULTS_FILE, default=[],
        )[-1]
        assert result["pilot_status"] == "NOT_STARTED"
        assert result["real_fill_count"] == 0
        assert result["shadow_comparison_profile"] == "july5_style"
        fill_lookup.assert_not_called()

    def test_observation_finalization_waits_for_fresh_inventory_books(
        self, monkeypatch,
    ):
        bot, _client, market_ws, _maker, *_rest = _bot(monkeypatch)
        bot.observation_tracker.open_inventory_slugs = Mock(return_value={"m1"})
        bot.observation_tracker.finalize_evaluation = Mock(
            return_value={"attempted": True, "complete": True}
        )
        bot.store.get_market_book_snapshot = Mock(return_value=None)

        assert bot._finalize_observation_when_ready() is None
        market_ws.force_reconnect.assert_not_called()
        bot.observation_tracker.finalize_evaluation.assert_not_called()

        bot._watched_slugs = ["m1"]
        assert bot._finalize_observation_when_ready() is None
        market_ws.force_reconnect.assert_called_once_with()
        bot.observation_tracker.finalize_evaluation.assert_not_called()

        bot.store.get_market_book_snapshot.return_value = {
            "bids": [{"price": 0.48, "quantity": 5.0}],
            "asks": [{"price": 0.52, "quantity": 5.0}],
        }
        result = bot._finalize_observation_when_ready()

        assert result == {"attempted": True, "complete": True}
        bot.observation_tracker.finalize_evaluation.assert_called_once()

    def test_observation_deadline_finalizes_before_qualification(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity = _bot(monkeypatch)
        bot.settings = dataclasses.replace(bot.settings, observation_only_mode=True)
        monkeypatch.setattr(
            "polymarket_bot.live.ws_runner.recover_from_prior_crash", Mock(),
        )
        monkeypatch.setattr(bot, "_maybe_refresh_candidates", Mock())
        monkeypatch.setattr(bot, "_start_ws_thread", Mock())
        monkeypatch.setattr(bot, "_start_private_ws_thread", Mock())
        bot.observation_tracker.evaluation_complete = Mock(return_value=True)
        order = []

        def _finalize(_provider):
            order.append("finalize")
            return {"attempted": True, "complete": True}

        def _qualify():
            order.append("qualify")
            return {
                "status": "PASS",
                "completed_round_trips": 20,
                "distinct_event_count": 5,
                "total_pnl_usd": 1.0,
            }

        bot.observation_tracker.finalize_evaluation = Mock(side_effect=_finalize)
        bot.observation_tracker.controlled_qualification = Mock(side_effect=_qualify)

        bot.run_forever()

        assert order == ["finalize", "qualify"]
        maker.refresh_quotes.assert_not_called()

    def test_observation_only_never_starts_maker_or_shutdown_cancel_all(self, monkeypatch):
        # Deliberately does NOT pre-set bot._stop_event -- doing that would
        # make the while loop body (including the branch under test) never
        # execute at all, letting this pass regardless of whether the
        # observation-only skip logic is even present. Instead, a
        # side_effect on the exact method the observation branch calls each
        # cycle lets one real iteration run before stopping.
        bot, client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = dataclasses.replace(bot.settings, observation_only_mode=True)
        monkeypatch.setattr(
            "polymarket_bot.live.ws_runner.recover_from_prior_crash", Mock(),
        )
        call_count = {"n": 0}
        original_check = bot._abort_if_unexpected_activity_during_observation

        def _checked():
            call_count["n"] += 1
            original_check()
            if call_count["n"] >= 2:
                bot._stop_event.set()

        monkeypatch.setattr(bot, "_abort_if_unexpected_activity_during_observation", _checked)

        bot.run_forever()

        assert call_count["n"] >= 2
        assert bot._private_ws_thread is not None
        assert bot.private_store.open_orders_snapshot() == []
        assert bot.private_store.positions_snapshot() == {}
        maker.refresh_quotes.assert_not_called()
        client.cancel_all.assert_not_called()

    def test_observation_only_mode_aborts_startup_if_open_orders_exist(self, monkeypatch):
        bot, client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = dataclasses.replace(bot.settings, observation_only_mode=True)
        monkeypatch.setattr(
            "polymarket_bot.live.ws_runner.recover_from_prior_crash", Mock(),
        )
        client.get_open_orders.return_value = [{"id": "o1", "marketSlug": "m1"}]

        with pytest.raises(StartupRecoveryError):
            bot.run_forever()

        maker.refresh_quotes.assert_not_called()
        assert bot._private_ws_thread is None

    def test_observation_only_mode_aborts_startup_if_position_exists(self, monkeypatch):
        bot, client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = dataclasses.replace(bot.settings, observation_only_mode=True)
        monkeypatch.setattr(
            "polymarket_bot.live.ws_runner.recover_from_prior_crash", Mock(),
        )
        client.get_all_positions.return_value = {"m1": {"netPositionDecimal": "3"}}

        with pytest.raises(StartupRecoveryError):
            bot.run_forever()

        maker.refresh_quotes.assert_not_called()
        assert bot._private_ws_thread is None

    def test_observation_only_mode_requires_private_websocket_enabled(self, monkeypatch):
        bot, client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = dataclasses.replace(
            bot.settings, observation_only_mode=True, enable_private_websocket=False,
        )
        monkeypatch.setattr(
            "polymarket_bot.live.ws_runner.recover_from_prior_crash", Mock(),
        )

        with pytest.raises(StartupRecoveryError):
            bot.run_forever()

        maker.refresh_quotes.assert_not_called()

    def test_observation_only_mode_aborts_if_order_appears_mid_run(self, monkeypatch):
        # The private WS running read-only must catch state appearing
        # DURING the run, not just a dirty state at startup -- and must
        # only stop, never attempt to cancel what it finds.
        bot, client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = dataclasses.replace(bot.settings, observation_only_mode=True)
        monkeypatch.setattr(
            "polymarket_bot.live.ws_runner.recover_from_prior_crash", Mock(),
        )
        original_refresh = bot._maybe_refresh_candidates

        def _inject_then_refresh():
            original_refresh()
            bot.private_store.handle_message({"orderSubscriptionUpdate": {"execution": {"order": {
                "id": "surprise-1", "marketSlug": "m1", "state": "ORDER_STATE_NEW",
            }}}})

        monkeypatch.setattr(bot, "_maybe_refresh_candidates", _inject_then_refresh)

        with pytest.raises(ObservationIntegrityError):
            bot.run_forever()

        client.cancel_all.assert_not_called()
        client.cancel_order.assert_not_called()
        maker.refresh_quotes.assert_not_called()

    def test_observation_only_mode_aborts_if_position_appears_mid_run(self, monkeypatch):
        bot, client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = dataclasses.replace(bot.settings, observation_only_mode=True)
        monkeypatch.setattr(
            "polymarket_bot.live.ws_runner.recover_from_prior_crash", Mock(),
        )
        original_refresh = bot._maybe_refresh_candidates

        def _inject_then_refresh():
            original_refresh()
            bot.private_store.handle_message({"positionSubscription": {"afterPosition": {
                "marketSlug": "m1", "netPositionDecimal": "5",
            }}})

        monkeypatch.setattr(bot, "_maybe_refresh_candidates", _inject_then_refresh)

        with pytest.raises(ObservationIntegrityError):
            bot.run_forever()

        client.cancel_all.assert_not_called()
        client.cancel_order.assert_not_called()
        maker.refresh_quotes.assert_not_called()

    def test_calls_startup_crash_recovery(self, monkeypatch):
        bot, client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        recover = Mock()
        monkeypatch.setattr("polymarket_bot.live.ws_runner.recover_from_prior_crash", recover)
        maker.refresh_quotes.side_effect = KeyboardInterrupt()

        bot.run_forever()

        recover.assert_called_once_with(client)

    def test_skips_startup_crash_recovery_when_disabled(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = dataclasses.replace(bot.settings, startup_crash_recovery_enabled=False)
        recover = Mock()
        monkeypatch.setattr("polymarket_bot.live.ws_runner.recover_from_prior_crash", recover)
        maker.refresh_quotes.side_effect = KeyboardInterrupt()

        bot.run_forever()

        recover.assert_not_called()

    def test_startup_crash_recovery_failure_blocks_startup(self, monkeypatch, caplog):
        # A failed recovery (can't enumerate/cancel/verify leftover
        # orders) must refuse to start rather than place new orders on top
        # of unknown prior state -- the main loop must never run at all.
        bot, client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        monkeypatch.setattr(
            "polymarket_bot.live.ws_runner.recover_from_prior_crash",
            Mock(side_effect=RuntimeError("boom")),
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError, match="boom"):
                bot.run_forever()

        maker.refresh_quotes.assert_not_called()
        assert any("refusing to start" in r.message for r in caplog.records)

    def test_logs_and_reraises_an_unexpected_exception(self, monkeypatch, caplog):
        # _run_one_cycle() already catches an ordinary failure internally
        # (see test_never_raises_out_of_run_one_cycle) -- this exercises a
        # failure OUTSIDE that try/except, at the run_forever() level
        # itself, to prove the new except Exception clause there fires.
        # Re-raising alone isn't distinguishing (Python would propagate an
        # uncaught exception through finally: regardless) -- the actual
        # point is getting the traceback into the rotating bot.log, so
        # assert on that specifically via caplog.
        bot, _client, _market_ws, _maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot._run_one_cycle = Mock(side_effect=RuntimeError("boom"))

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError, match="boom"):
                bot.run_forever()

        assert any("crashed unexpectedly" in r.message for r in caplog.records)
        assert any(r.exc_info is not None for r in caplog.records)
