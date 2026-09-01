import base64
from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live import market_dryrun as dryrun_module
from polymarket_bot.live import market_observation as observation_module
from polymarket_bot.live.credentials import ApiCredentials
from polymarket_bot.live.instance_lock import AlreadyRunningError, InstanceLock
from polymarket_bot.live.market_dryrun import (
    DryRunFeedStalledError,
    DryRunPolicy,
    DryRunPolicyMismatchError,
    DryRunRunner,
    compute_dry_run_verdict,
    dry_run_deadline_reached,
    dry_run_evidence_target_met,
    dry_run_settings,
    dry_run_status,
    enforce_dry_run_policy,
    run_dry_run,
)
from polymarket_bot.live.market_observation import (
    DRY_RUN_PHASE_COLLECTING,
    DRY_RUN_PHASE_COMPLETE,
    DRY_RUN_PHASE_FINALIZING,
    DRY_RUN_PHASE_GRACE,
    MarketObservationTracker,
    PROFILE_CONTROLLED,
    PROFILE_JULY5_STYLE,
    PROFILE_LEGACY,
)
from polymarket_bot.models import Market, ScoredMarket

VALID_SECRET = base64.b64encode(b"a" * 32).decode()
CREDS = ApiCredentials(key_id="key-123", secret_key=VALID_SECRET)


def _settings(**overrides):
    values = dict(
        observation_only_mode=False,
        observation_gate_enabled=True,
        refresh_interval_seconds=0,
    )
    values.update(overrides)
    return config.LiveTradingSettings(**values)


def _scored(market_id="m1"):
    market = Market(
        market_id=market_id, event_id="e1", question="Will X happen?",
        category="politics", token_ids=["t1"], spread=0.03,
        raw={"orderPriceMinTickSize": 0.01},
    )
    return ScoredMarket(
        market_id=market_id, question=market.question, total_score=90.0,
        component_scores={}, explanation=[], recommendation="PAPER_CANDIDATE",
        market=market,
    )


# ---------------------------------------------------------------------
# DryRunPolicy / policy hash / enforcement
# ---------------------------------------------------------------------

def test_policy_hash_is_stable_for_identical_policies():
    assert DryRunPolicy().policy_hash() == DryRunPolicy().policy_hash()


def test_policy_hash_changes_when_a_threshold_changes():
    assert DryRunPolicy().policy_hash() != DryRunPolicy(min_round_trips=21).policy_hash()


def test_enforce_dry_run_policy_freezes_hash_on_first_run(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "dryrun.json")
    policy = DryRunPolicy()

    enforce_dry_run_policy(tracker, policy)

    assert tracker._state["dry_run_policy_hash"] == policy.policy_hash()


def test_enforce_dry_run_policy_allows_resume_with_matching_policy(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "dryrun.json")
    policy = DryRunPolicy()
    enforce_dry_run_policy(tracker, policy)

    enforce_dry_run_policy(tracker, policy)  # must not raise


def test_enforce_dry_run_policy_fails_closed_on_mismatch(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "dryrun.json")
    enforce_dry_run_policy(tracker, DryRunPolicy())

    with pytest.raises(DryRunPolicyMismatchError):
        enforce_dry_run_policy(tracker, DryRunPolicy(min_round_trips=999))


# ---------------------------------------------------------------------
# Evidence target / deadline
# ---------------------------------------------------------------------

def test_evidence_target_requires_all_three_floors_together():
    policy = DryRunPolicy(min_round_trips=20, min_distinct_events=5, min_entry_markout_samples=20)
    # Round trips and events satisfied, markouts not yet matured -- must
    # NOT report the target met (this is the premature-stop regression).
    summary = {
        "completed_round_trips": 20, "distinct_event_count": 5,
        "entry_markout_5m_sample_count": 19,
    }
    assert dry_run_evidence_target_met(summary, policy) is False

    summary["entry_markout_5m_sample_count"] = 20
    assert dry_run_evidence_target_met(summary, policy) is True


def test_deadline_reached_by_healthy_feed_hours_or_wall_clock():
    policy = DryRunPolicy(min_healthy_feed_hours=48.0, max_wall_clock_hours=96.0)
    assert dry_run_deadline_reached({"healthy_feed_hours": 48.0}, policy, 10.0) is True
    assert dry_run_deadline_reached({"healthy_feed_hours": 10.0}, policy, 96.0) is True
    assert dry_run_deadline_reached({"healthy_feed_hours": 10.0}, policy, 10.0) is False


# ---------------------------------------------------------------------
# Verdict computation
# ---------------------------------------------------------------------

def _passing_summary(**overrides):
    values = dict(
        completed_round_trips=20, distinct_event_count=5,
        entry_markout_5m_sample_count=20, open_inventory=[],
        total_pnl_usd=5.0, profit_factor=1.5, avg_markout_5m_cents=1.0,
        maximum_drawdown_usd=-1.0, event_profit_concentration=0.3,
        forced_exit_count=1, settlement_exit_count=1,
    )
    values.update(overrides)
    return values


def _verdict(summary, policy=None, any_valuation_incomplete=False):
    return compute_dry_run_verdict(summary, policy or DryRunPolicy(), any_valuation_incomplete)


def test_verdict_insufficient_below_the_sample_floor():
    result = _verdict(_passing_summary(completed_round_trips=5))
    assert result["verdict"] == "INSUFFICIENT"


def test_verdict_insufficient_with_open_inventory_remaining():
    result = _verdict(_passing_summary(open_inventory=[{"market_slug": "m1"}]))
    assert result["verdict"] == "INSUFFICIENT"


def test_verdict_insufficient_when_any_equity_point_was_valuation_incomplete():
    """Regression test: an earlier version only guarded the FINAL
    total_pnl_usd (trustworthy once flat), but maximum_drawdown_usd scans
    the entire historical curve and silently SKIPS incomplete points --
    a real dip during a temporarily-unpriced position would be invisible
    to that number. A PASS/FAIL over an unknowable portion of the
    drawdown history must not be possible."""
    result = _verdict(_passing_summary(), any_valuation_incomplete=True)
    assert result["verdict"] == "INSUFFICIENT"
    assert result["detail"]["floor_checks"]["no_incomplete_valuation"] is False


def test_verdict_pass_when_every_threshold_clears():
    result = _verdict(_passing_summary())
    assert result["verdict"] == "PASS"


@pytest.mark.parametrize("overrides", [
    {"total_pnl_usd": -0.5},
    {"total_pnl_usd": 0.0},
    {"profit_factor": 1.0},
    {"avg_markout_5m_cents": -0.5},
    {"event_profit_concentration": 0.6},
    {"maximum_drawdown_usd": -3.5},
])
def test_verdict_fail_when_floor_met_but_one_threshold_misses(overrides):
    result = _verdict(_passing_summary(**overrides))
    assert result["verdict"] == "FAIL"


def test_verdict_fail_on_excessive_settlement_or_forced_exit_rate():
    # 5 of 20 round trips closed by forced/settlement exit = 25% > 20% cap.
    result = _verdict(_passing_summary(forced_exit_count=3, settlement_exit_count=2))
    assert result["verdict"] == "FAIL"
    assert result["detail"]["settlement_or_forced_exit_rate"] == pytest.approx(0.25)


def test_verdict_exit_rate_exactly_at_the_20_percent_boundary_passes():
    # 4 of 20 = exactly 20% -- must pass (<=, not <).
    result = _verdict(_passing_summary(forced_exit_count=2, settlement_exit_count=2))
    assert result["verdict"] == "PASS"


# ---------------------------------------------------------------------
# dry_run_settings
# ---------------------------------------------------------------------

def test_dry_run_settings_forces_one_share_sizing_and_matching_evaluation_hours():
    base = _settings(observation_july5_order_shares=17.5, observation_evaluation_hours=30.0)
    policy = DryRunPolicy(order_shares=1.0, min_healthy_feed_hours=48.0)

    result = dry_run_settings(base, policy)

    assert result.observation_july5_order_shares == 1.0
    assert result.observation_evaluation_hours == 48.0


# ---------------------------------------------------------------------
# dry_run_status -- read-only status command
# ---------------------------------------------------------------------

def test_dry_run_status_not_started_when_archive_absent(tmp_path):
    result = dry_run_status(tmp_path / "does_not_exist.json")
    assert result["verdict"] == "NOT_STARTED"


def test_dry_run_status_provisional_before_any_snapshot_is_written(tmp_path):
    path = tmp_path / "dryrun.json"
    tracker = MarketObservationTracker(_settings(), path=path)
    tracker.flush()

    result = dry_run_status(path)

    assert result["verdict"] == "PROVISIONAL"
    assert result["phase"] == DRY_RUN_PHASE_COLLECTING


def test_dry_run_status_returns_the_persisted_snapshot_verbatim(tmp_path):
    path = tmp_path / "dryrun.json"
    tracker = MarketObservationTracker(_settings(), path=path)
    tracker.record_dry_run_snapshot({"phase": DRY_RUN_PHASE_COMPLETE, "verdict": "PASS"}, 1000.0)

    result = dry_run_status(path)

    assert result == {"phase": DRY_RUN_PHASE_COMPLETE, "verdict": "PASS"}


def test_dry_run_status_never_constructs_a_tracker(tmp_path, monkeypatch):
    path = tmp_path / "dryrun.json"
    spy = Mock(wraps=MarketObservationTracker)
    monkeypatch.setattr(dryrun_module, "MarketObservationTracker", spy)

    dry_run_status(path)

    spy.assert_not_called()


# ---------------------------------------------------------------------
# DryRunRunner -- structural guarantee + cycle orchestration
# ---------------------------------------------------------------------

def test_market_dryrun_module_never_imports_order_capable_classes():
    """The structural guarantee: checks the module's actual bound names
    (only things an import/def introduces), not prose -- a docstring
    mentioning why LiveUsClient is deliberately absent shouldn't trip a
    naive text scan the way tests/test_no_live_orders.py's would."""
    banned_names = (
        "LiveUsClient", "MultiMarketMaker", "MarketMaker",
        "PrivateWebSocketClient", "PrivateStateStore",
    )
    for name in banned_names:
        assert name not in vars(dryrun_module), f"market_dryrun.py must not import '{name}'"


def _selector_populating(*market_ids):
    """select_target_markets' real contract: the broad observation
    universe is delivered via the observation_markets_out out-param, not
    the return value (which is the narrow, live-quoting-eligible set)."""
    def _side_effect(*_args, observation_markets_out=None, **_kwargs):
        if observation_markets_out is not None:
            observation_markets_out.extend(_scored(m) for m in market_ids)
        return []
    return Mock(side_effect=_side_effect)


def _runner(monkeypatch, tmp_path, tracker=None, selector=None, settings=None):
    monkeypatch.setattr(
        dryrun_module, "select_target_markets",
        selector or _selector_populating("m1"),
    )
    store = Mock()
    market_ws = Mock()
    read_client = Mock()
    read_client.get_market_book.return_value = {
        "bids": [{"price": 0.50, "quantity": 10.0}],
        "asks": [{"price": 0.52, "quantity": 10.0}],
    }
    settings = settings or _settings()
    runner = DryRunRunner(
        credentials=CREDS,
        settings=settings,
        policy=DryRunPolicy(grace_period_seconds=0.0),
        tracker=tracker or MarketObservationTracker(settings, path=tmp_path / "dryrun.json"),
        store=store,
        market_ws=market_ws,
        read_client=read_client,
        path=tmp_path / "dryrun.json",
    )
    return runner, store, market_ws, read_client


def test_refresh_candidates_feeds_the_pool_and_subscribes_without_pinning(tmp_path, monkeypatch):
    """Regression test: an earlier version called
    override_profile_allocation(policy.profile, slugs), which PINS the
    active set to exactly that caller-ordered list -- bypassing
    policy.profile's own widest-spread ranking entirely and silently
    substituting the scan's recency/depth order instead. The dry-run must
    feed the broad candidate pool and let the profile rank it itself."""
    runner, _store, market_ws, _read_client = _runner(tmp_path=tmp_path, monkeypatch=monkeypatch)

    runner.run_one_cycle()

    assert runner.tracker._pinned_allocation.get(PROFILE_JULY5_STYLE) is None
    assert "m1" in runner.tracker._candidate_pool
    market_ws.set_market_slugs.assert_called_once()
    assert "m1" in market_ws.set_market_slugs.call_args.args[0]


def test_refresh_candidates_selects_widest_spread_even_when_not_scanned_first(tmp_path, monkeypatch):
    """The user's exact regression scenario: the scan/observation-universe
    order is recency/depth, not spread. With the profile capped to fewer
    markets than there are candidates, a market later in that scan order
    but with a wider spread must still be the one that survives
    policy.profile's own ranking once real book data arrives -- proving
    the fix actually restores widest-spread selection, not just that
    pinning was removed."""
    now = {"value": 1_000_000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    settings = _settings(observation_profile_max_markets=1)
    tracker = MarketObservationTracker(settings, path=tmp_path / "dryrun.json")
    runner, _store, market_ws, _read_client = _runner(
        tmp_path=tmp_path, monkeypatch=monkeypatch, tracker=tracker, settings=settings,
        # Scan order: m1 first (narrow spread), m2 last (widest spread) --
        # if the old recency/depth-ordered pin were still in effect, m1
        # would be the one (and only) survivor of the 1-market cap.
        selector=_selector_populating("m1", "m2"),
    )

    runner.run_one_cycle()  # feeds the pool, no pinning

    narrow_book = {
        "bids": [{"price": 0.49, "quantity": 30.0}],
        "asks": [{"price": 0.51, "quantity": 30.0}],
    }
    wide_book = {
        "bids": [{"price": 0.20, "quantity": 30.0}],
        "asks": [{"price": 0.80, "quantity": 30.0}],
    }
    tracker.record_book("m1", narrow_book)
    # _refresh_allocations() throttles re-ranking once the profile is
    # already at its cap (observation_profile_refresh_seconds, 60s
    # default) -- advance past it so m2's wider spread actually gets a
    # chance to compete, rather than m1 winning merely by being ranked
    # first and then never being re-evaluated.
    now["value"] += 61.0
    tracker.record_book("m2", wide_book)

    assert tracker._active[PROFILE_JULY5_STYLE] == {"m2"}


def test_non_target_profiles_are_pinned_empty_and_cannot_create_inventory(tmp_path, monkeypatch):
    """Regression test: legacy/controlled share the same tracker instance
    and, unless isolated, independently rank/quote the identical broad WS
    feed -- open_inventory_slugs()/finalize_dry_run_evaluation() operate
    across ALL profiles, so unrelated shadow positions could delay
    FINALIZING or eat into the bounded wait for a position this dry-run
    was never grading."""
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "dryrun.json")
    runner, _store, _market_ws, _read_client = _runner(
        tmp_path=tmp_path, monkeypatch=monkeypatch, tracker=tracker,
    )

    assert tracker._pinned_allocation.get(PROFILE_LEGACY) == ()
    assert tracker._pinned_allocation.get(PROFILE_CONTROLLED) == ()

    runner.run_one_cycle()
    book = {
        "bids": [{"price": 0.20, "quantity": 30.0}],
        "asks": [{"price": 0.80, "quantity": 30.0}],
    }
    tracker.record_book("m1", book)
    tracker.record_book("m1", book)
    tracker.record_trade(
        "m1", price=0.30, quantity=5.0, maker_side="ORDER_SIDE_BUY", trade_time="trade-1",
    )

    assert tracker._active[PROFILE_LEGACY] == set()
    assert tracker._active[PROFILE_CONTROLLED] == set()
    for profile in (PROFILE_LEGACY, PROFILE_CONTROLLED):
        market = tracker._trackers[profile]._market("m1")
        assert market.get("hypothetical_fills", []) == []


def test_refresh_candidates_uses_the_broad_observation_universe_not_the_narrow_return_value(
    tmp_path, monkeypatch,
):
    """Regression test: select_target_markets' RETURN value is the narrow,
    live-quoting-eligible set (filtered by spread/cutoff/max-per-event --
    tuned for LIVE risk, not broad evidence collection). The dry-run must
    watch observation_markets_out instead, which deliberately ignores
    those filters."""
    def _side_effect(*_args, observation_markets_out=None, **_kwargs):
        if observation_markets_out is not None:
            observation_markets_out.extend([_scored("broad-only")])
        return [_scored("narrow-only")]  # must NOT end up watched

    selector = Mock(side_effect=_side_effect)
    runner, _store, market_ws, _read_client = _runner(
        tmp_path=tmp_path, monkeypatch=monkeypatch, selector=selector,
    )

    runner.run_one_cycle()

    watched = market_ws.set_market_slugs.call_args.args[0]
    assert "broad-only" in watched
    assert "narrow-only" not in watched


def test_refresh_candidates_registers_real_market_metadata(tmp_path, monkeypatch):
    """Regression test: an earlier version never called register_market(),
    leaving tick_size/event_id/event_or_close_epoch/raw all absent --
    different slugs from one game could incorrectly count as distinct
    events, and the in-play window couldn't be enforced."""
    market = Market(
        market_id="m1", event_id="event-42", question="Will X happen?",
        category="sports", token_ids=["t1"], spread=0.03,
        raw={"orderPriceMinTickSize": 0.02, "gameStartTime": "2026-08-23T18:00:00Z"},
    )
    scored = ScoredMarket(
        market_id="m1", question=market.question, total_score=90.0,
        component_scores={}, explanation=[], recommendation="PAPER_CANDIDATE",
        market=market,
    )

    def _side_effect(*_args, observation_markets_out=None, **_kwargs):
        if observation_markets_out is not None:
            observation_markets_out.append(scored)
        return []

    runner, _store, _market_ws, _read_client = _runner(
        tmp_path=tmp_path, monkeypatch=monkeypatch, selector=Mock(side_effect=_side_effect),
    )

    runner.run_one_cycle()

    registered = runner.tracker._trackers[PROFILE_JULY5_STYLE]._market("m1")
    assert registered["tick_size"] == pytest.approx(0.02)
    assert registered["event_id"] == "event-42"
    assert registered["event_or_close_epoch"] is not None
    assert registered["raw"]["gameStartTime"] == "2026-08-23T18:00:00Z"


def test_refresh_candidates_is_throttled_to_the_background_cadence(tmp_path, monkeypatch):
    """Regression test: an earlier version scanned ~5000 markets on every
    run_one_cycle() tick -- as often as every 10s under a live-tuned
    refresh_interval_seconds -- instead of the existing 900s background
    cadence (60s retry on failure)."""
    settings = _settings(
        websocket_candidate_refresh_seconds=900, websocket_candidate_refresh_retry_seconds=60,
    )
    now = {"value": 1_000_000.0}
    monkeypatch.setattr(dryrun_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "dryrun.json")
    selector = _selector_populating("m1")
    monkeypatch.setattr(dryrun_module, "select_target_markets", selector)
    store, market_ws, read_client = Mock(), Mock(), Mock()
    runner = DryRunRunner(
        credentials=CREDS, settings=settings, policy=DryRunPolicy(grace_period_seconds=0.0),
        tracker=tracker, store=store, market_ws=market_ws, read_client=read_client,
        path=tmp_path / "dryrun.json",
    )

    runner.run_one_cycle()
    assert selector.call_count == 1

    now["value"] += 10.0  # well under both the 900s and 60s thresholds
    runner.run_one_cycle()
    assert selector.call_count == 1

    now["value"] += 900.0
    runner.run_one_cycle()
    assert selector.call_count == 2


def test_run_one_cycle_advances_to_grace_once_evidence_target_met(tmp_path, monkeypatch):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "dryrun.json")
    monkeypatch.setattr(
        observation_module.MarketObservationTracker, "profile_summary",
        lambda self, profile=PROFILE_JULY5_STYLE: {
            "completed_round_trips": 20, "distinct_event_count": 5,
            "entry_markout_5m_sample_count": 20, "healthy_feed_hours": 1.0,
            "open_inventory": [],
        },
    )
    runner, _store, _market_ws, _read_client = _runner(
        tmp_path=tmp_path, monkeypatch=monkeypatch, tracker=tracker,
    )

    runner.run_one_cycle()

    assert tracker.dry_run_phase() == DRY_RUN_PHASE_GRACE


def test_run_one_cycle_advances_grace_to_finalizing_after_deadline(tmp_path, monkeypatch):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "dryrun.json")
    tracker.advance_dry_run_to_grace(0.0, grace_seconds=0.0)
    runner, _store, _market_ws, _read_client = _runner(
        tmp_path=tmp_path, monkeypatch=monkeypatch, tracker=tracker,
    )

    runner.run_one_cycle()

    assert tracker.dry_run_phase() == DRY_RUN_PHASE_FINALIZING


def test_run_one_cycle_finalizes_and_completes_when_sweep_resolves(tmp_path, monkeypatch):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "dryrun.json")
    tracker.advance_dry_run_to_grace(0.0, grace_seconds=0.0)
    tracker.advance_dry_run_to_finalizing(0.0)
    monkeypatch.setattr(
        observation_module.MarketObservationTracker, "profile_summary",
        lambda self, profile=PROFILE_JULY5_STYLE: {
            "completed_round_trips": 3, "distinct_event_count": 1,
            "entry_markout_5m_sample_count": 3, "open_inventory": [],
        },
    )
    runner, _store, _market_ws, _read_client = _runner(
        tmp_path=tmp_path, monkeypatch=monkeypatch, tracker=tracker,
    )

    runner.run_one_cycle()

    # No open inventory was ever created in this test, so the sweep
    # trivially completes without needing a book fetch -- covered with
    # actual open inventory by
    # test_finalize_dry_run_evaluation_sweeps_open_inventory_to_flat in
    # test_market_observation.py.
    assert tracker.dry_run_phase() == DRY_RUN_PHASE_COMPLETE
    assert tracker._state["dry_run_verdict"]["verdict"] == "INSUFFICIENT"


def _tracker_with_stuck_inventory(tmp_path):
    """A real open shadow position with no matching book -- the FINALIZING
    scenario a permanently unresolvable market produces."""
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "dryrun.json")
    tracker.register_market("m1", tick_size=0.01)
    tracker.set_live_candidate_slugs(["m1"])
    book = {
        "bids": [{"price": 0.48, "quantity": 30.0}],
        "asks": [{"price": 0.52, "quantity": 30.0}],
    }
    tracker.record_book("m1", book)  # establishes a resting hypothetical quote
    tracker.record_book("m1", book)
    tracker.record_trade(
        "m1", price=0.48, quantity=5.0, maker_side="ORDER_SIDE_BUY", trade_time="trade-1",
    )
    assert tracker.open_inventory_slugs() == {"m1"}
    # Must track whatever "now" run_one_cycle() will itself observe via
    # time.time() -- an earlier version of this fixture hardcoded 0.0,
    # which made dry_run_finalizing_started_epoch() look ~55 years in the
    # past against real wall-clock time, spuriously tripping the bounded-
    # wait timeout on the very first cycle.
    started = dryrun_module.time.time()
    tracker.advance_dry_run_to_grace(started, grace_seconds=0.0)
    tracker.advance_dry_run_to_finalizing(started)
    return tracker


def test_finalizing_attempts_settlement_for_stuck_slugs_after_a_failed_sweep(tmp_path, monkeypatch):
    """Regression test: an earlier version only retried the live-book
    sweep forever. A market that has actually resolved will never produce
    a two-sided book again -- settlement must be attempted instead of
    waiting out the full bounded timeout."""
    tracker = _tracker_with_stuck_inventory(tmp_path)
    settlement_lookup = Mock(return_value={
        "slug": "m1", "status": "UNRESOLVED", "retrieved_at_epoch": 0.0,
    })
    monkeypatch.setattr(dryrun_module, "classify_settlement_lookup", settlement_lookup)
    runner, _store, _market_ws, read_client = _runner(
        tmp_path=tmp_path, monkeypatch=monkeypatch, tracker=tracker,
    )
    read_client.get_market_book.return_value = None  # unresolvable

    runner.run_one_cycle()

    settlement_lookup.assert_called_once()
    assert settlement_lookup.call_args.args[1] == "m1"
    # UNRESOLVED and well within the bounded wait -- must stay in
    # FINALIZING, not give up early.
    assert tracker.dry_run_phase() == DRY_RUN_PHASE_FINALIZING


def test_finalizing_settles_a_resolved_market_via_settlement_lookup(tmp_path, monkeypatch):
    tracker = _tracker_with_stuck_inventory(tmp_path)
    settlement_lookup = Mock(return_value={
        "slug": "m1", "status": "SETTLED", "settlement_price": 1.0,
        "retrieved_at_epoch": 0.0, "metadata_synced_at": None,
    })
    monkeypatch.setattr(dryrun_module, "classify_settlement_lookup", settlement_lookup)
    monkeypatch.setattr(
        observation_module.MarketObservationTracker, "profile_summary",
        lambda self, profile=PROFILE_JULY5_STYLE: {
            "completed_round_trips": 1, "distinct_event_count": 1,
            "entry_markout_5m_sample_count": 1, "open_inventory": [],
        },
    )
    runner, _store, _market_ws, read_client = _runner(
        tmp_path=tmp_path, monkeypatch=monkeypatch, tracker=tracker,
    )
    read_client.get_market_book.return_value = None

    runner.run_one_cycle()

    assert tracker.open_inventory_slugs() == set()
    assert tracker.dry_run_phase() == DRY_RUN_PHASE_COMPLETE


def test_finalizing_forces_insufficient_after_the_bounded_wait_expires(tmp_path, monkeypatch):
    """Regression test: if inventory never resolves (no book, no genuine
    settlement), FINALIZING must not retry forever -- it must give up
    after policy.max_finalizing_wait_seconds and record an explicit
    INSUFFICIENT verdict rather than looping indefinitely."""
    now = {"value": 10_000.0}
    monkeypatch.setattr(dryrun_module.time, "time", lambda: now["value"])
    tracker = _tracker_with_stuck_inventory(tmp_path)
    monkeypatch.setattr(
        dryrun_module, "classify_settlement_lookup",
        Mock(return_value={"slug": "m1", "status": "UNRESOLVED", "retrieved_at_epoch": 0.0}),
    )
    runner, _store, _market_ws, read_client = _runner(
        tmp_path=tmp_path, monkeypatch=monkeypatch, tracker=tracker,
    )
    read_client.get_market_book.return_value = None
    runner.policy = DryRunPolicy(max_finalizing_wait_seconds=1800.0)

    now["value"] = 10_000.0 + 900.0  # well within the bound
    runner.run_one_cycle()
    assert tracker.dry_run_phase() == DRY_RUN_PHASE_FINALIZING

    now["value"] = 10_000.0 + 1800.0 + 1.0  # past the bound
    runner.run_one_cycle()

    assert tracker.dry_run_phase() == DRY_RUN_PHASE_COMPLETE
    assert tracker._state["dry_run_verdict"]["verdict"] == "INSUFFICIENT"
    assert tracker.open_inventory_slugs() == {"m1"}  # never actually resolved


def test_equity_curve_has_incomplete_valuation_detects_any_flagged_point(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "dryrun.json")
    tracker._state["profiles"][PROFILE_JULY5_STYLE]["equity_curve"] = [
        {"bucket_epoch": 0, "total_pnl_usd": 1.0, "valuation_incomplete": False},
    ]
    assert dryrun_module.equity_curve_has_incomplete_valuation(tracker, PROFILE_JULY5_STYLE) is False

    tracker._state["profiles"][PROFILE_JULY5_STYLE]["equity_curve"].append(
        {"bucket_epoch": 60, "total_pnl_usd": None, "valuation_incomplete": True},
    )
    assert dryrun_module.equity_curve_has_incomplete_valuation(tracker, PROFILE_JULY5_STYLE) is True


def test_run_one_cycle_records_a_snapshot_every_cycle(tmp_path, monkeypatch):
    runner, _store, _market_ws, _read_client = _runner(tmp_path=tmp_path, monkeypatch=monkeypatch)

    runner.run_one_cycle()

    assert runner.tracker._state["dry_run_snapshot"]["phase"] == DRY_RUN_PHASE_COLLECTING
    assert runner.tracker._state["dry_run_snapshot"]["verdict"] == "PROVISIONAL"


# ---------------------------------------------------------------------
# Isolation from every other archive -- structural + an actual run
# ---------------------------------------------------------------------

def test_market_dryrun_module_never_imports_other_archive_constants():
    """Structural guarantee backing the checksum test below: the module
    only ever references its own DRYRUN_OBSERVATION_FILE, never
    OBSERVATION_FILE/PILOT_OBSERVATION_FILE/JULY5_PILOT_OBSERVATION_FILE/
    FILLS_FILE/LEDGER_FILE -- so there is no code path that COULD touch
    them, regardless of what a given run does at runtime."""
    banned_names = (
        "OBSERVATION_FILE", "PILOT_OBSERVATION_FILE",
        "JULY5_PILOT_OBSERVATION_FILE", "FILLS_FILE", "LEDGER_FILE",
    )
    for name in banned_names:
        assert name not in vars(dryrun_module), (
            f"market_dryrun.py must not reference '{name}'"
        )


def test_full_dry_run_cycle_leaves_every_other_archive_byte_identical(tmp_path, monkeypatch):
    """Not just unit-level mocking of THIS module's own isolation -- drives
    an actual DryRunRunner through COLLECTING -> GRACE -> FINALIZING ->
    COMPLETE (using a real MarketObservationTracker against its own
    dedicated file) and confirms every other real archive file, pre-seeded
    with real content, is untouched byte-for-byte."""
    from polymarket_bot import storage
    from polymarket_bot.live import fills as fills_module
    from polymarket_bot.live import ledger as ledger_module
    from polymarket_bot.live import market_observation as obs_module

    observation_file = tmp_path / "market_observations_v5.json"
    pilot_file = tmp_path / "pilot_shadow_observations.json"
    july5_pilot_file = tmp_path / "pilot_shadow_observations_july5.json"
    fills_file = tmp_path / "fills.json"
    ledger_file = tmp_path / "orders.json"

    # Seed each with real, non-trivial content via the real tracker/storage
    # primitives -- not empty placeholders.
    MarketObservationTracker(_settings(), path=observation_file).flush()
    MarketObservationTracker(_settings(), path=pilot_file).flush()
    MarketObservationTracker(_settings(), path=july5_pilot_file).flush()
    storage.save_json(fills_file, [{"fill_id": "f1", "side": "BUY"}])
    storage.save_json(ledger_file, [{"order_id": "o1"}])

    monkeypatch.setattr(obs_module, "OBSERVATION_FILE", observation_file)
    monkeypatch.setattr(obs_module, "PILOT_OBSERVATION_FILE", pilot_file)
    monkeypatch.setattr(obs_module, "JULY5_PILOT_OBSERVATION_FILE", july5_pilot_file)
    monkeypatch.setattr(fills_module, "FILLS_FILE", fills_file)
    monkeypatch.setattr(ledger_module, "LEDGER_FILE", ledger_file)

    before = {
        path: path.read_bytes()
        for path in (observation_file, pilot_file, july5_pilot_file, fills_file, ledger_file)
    }

    dryrun_tracker = MarketObservationTracker(_settings(), path=tmp_path / "dryrun.json")
    runner, _store, _market_ws, _read_client = _runner(
        tmp_path=tmp_path, monkeypatch=monkeypatch, tracker=dryrun_tracker,
        selector=Mock(return_value=[]),
    )
    monkeypatch.setattr(
        observation_module.MarketObservationTracker, "profile_summary",
        lambda self, profile=PROFILE_JULY5_STYLE: {
            "completed_round_trips": 20, "distinct_event_count": 5,
            "entry_markout_5m_sample_count": 20, "healthy_feed_hours": 1.0,
            "open_inventory": [],
        },
    )

    for _ in range(4):  # COLLECTING -> GRACE -> FINALIZING -> COMPLETE
        runner.run_one_cycle()
    assert dryrun_tracker.dry_run_phase() == DRY_RUN_PHASE_COMPLETE

    for path, original_bytes in before.items():
        assert path.read_bytes() == original_bytes, f"{path.name} was modified by the dry-run"


# ---------------------------------------------------------------------
# run_dry_run -- lock must cover construction, not just run_forever
# ---------------------------------------------------------------------

def test_run_dry_run_rejects_a_second_start_without_touching_the_archive(tmp_path, monkeypatch):
    """Regression test: an earlier version acquired the lock only inside
    run_forever(), after DryRunRunner.__init__ had already constructed the
    tracker (reading/writing the archive) and frozen the policy hash --
    a second concurrent start could mutate the archive before either
    process reached the lock. run_dry_run() must reject a second start
    BEFORE constructing anything."""
    lock_path = tmp_path / "dry_run.lock"
    archive_path = tmp_path / "dryrun.json"
    monkeypatch.setattr(dryrun_module, "DRYRUN_LOCK_FILE", lock_path)
    monkeypatch.setattr(dryrun_module, "DRYRUN_OBSERVATION_FILE", archive_path)
    constructor = Mock(side_effect=AssertionError("DryRunRunner must not be constructed"))
    monkeypatch.setattr(dryrun_module, "DryRunRunner", constructor)

    with InstanceLock(lock_path=lock_path):  # simulates an already-running process
        with pytest.raises(AlreadyRunningError):
            run_dry_run(credentials=CREDS, settings=_settings())

    constructor.assert_not_called()
    assert not archive_path.exists()


def test_run_dry_run_acquires_lock_then_constructs_and_runs(tmp_path, monkeypatch):
    lock_path = tmp_path / "dry_run.lock"
    monkeypatch.setattr(dryrun_module, "DRYRUN_LOCK_FILE", lock_path)
    runner = Mock()
    constructor = Mock(return_value=runner)
    monkeypatch.setattr(dryrun_module, "DryRunRunner", constructor)

    run_dry_run(credentials=CREDS, settings=_settings())

    constructor.assert_called_once()
    assert constructor.call_args.kwargs["credentials"] == CREDS
    runner.run_forever.assert_called_once()
    # The lock is released once run_dry_run() returns -- a second call
    # must succeed rather than raising AlreadyRunningError.
    run_dry_run(credentials=CREDS, settings=_settings())
    assert constructor.call_count == 2


# ---------------------------------------------------------------------
# Feed-health watchdog
# ---------------------------------------------------------------------

def test_run_one_cycle_does_nothing_extra_when_feed_is_healthy(tmp_path, monkeypatch):
    runner, _store, market_ws, _read_client = _runner(tmp_path=tmp_path, monkeypatch=monkeypatch)
    runner.tracker.feed_health = Mock(return_value={
        "required": True, "stalled": False, "reason": "silent_feed",
        "age_seconds": 1.0, "candidate_count": 1, "latest_activity_epoch": 1.0,
    })

    runner.run_one_cycle()  # must not raise

    market_ws.force_reconnect.assert_not_called()


def test_abort_if_feed_stalled_raises_when_candidate_universe_stays_empty(tmp_path, monkeypatch):
    """Regression test: an earlier version had no feed watchdog at all --
    an empty candidate universe (e.g. every scan fails, or every market is
    filtered out) could run all the way to the wall-clock deadline
    collecting no evidence at all."""
    runner, _store, _market_ws, _read_client = _runner(tmp_path=tmp_path, monkeypatch=monkeypatch)
    runner.tracker.feed_health = Mock(return_value={
        "required": True, "stalled": True, "reason": "empty_candidate_pool",
        "age_seconds": 999.0, "candidate_count": 0,
    })

    with pytest.raises(DryRunFeedStalledError):
        runner.run_one_cycle()


def test_abort_if_feed_stalled_requests_reconnect_twice_before_failing_closed(tmp_path, monkeypatch):
    """Regression test: a genuinely dead market-data WebSocket (feed goes
    silent, candidate pool non-empty) must not let the runner keep looping
    toward its wall-clock deadline either -- attempts up to 2 recovery
    reconnects, then fails closed."""
    runner, _store, market_ws, _read_client = _runner(tmp_path=tmp_path, monkeypatch=monkeypatch)
    stalled_health = {
        "required": True, "stalled": True, "reason": "silent_feed",
        "age_seconds": 999.0, "candidate_count": 1, "latest_activity_epoch": 0.0,
    }
    runner.tracker.feed_health = Mock(return_value=stalled_health)

    runner.run_one_cycle()
    assert market_ws.force_reconnect.call_count == 1

    # Still within the recovery grace period -- must not reconnect again yet.
    runner.run_one_cycle()
    assert market_ws.force_reconnect.call_count == 1


def test_abort_if_feed_stalled_recovers_when_activity_resumes(tmp_path, monkeypatch):
    runner, _store, market_ws, _read_client = _runner(tmp_path=tmp_path, monkeypatch=monkeypatch)
    runner.tracker.feed_health = Mock(return_value={
        "required": True, "stalled": True, "reason": "silent_feed",
        "age_seconds": 999.0, "candidate_count": 1, "latest_activity_epoch": 0.0,
    })
    runner.run_one_cycle()
    assert market_ws.force_reconnect.call_count == 1

    runner.tracker.feed_health = Mock(return_value={
        "required": True, "stalled": False, "reason": "silent_feed",
        "age_seconds": 0.0, "candidate_count": 1, "latest_activity_epoch": 100.0,
    })
    runner.run_one_cycle()  # must not raise, must reset recovery state
    assert runner._feed_recovery_attempts == 0


# ---------------------------------------------------------------------
# Settlement lookups are rate-limited
# ---------------------------------------------------------------------

def test_settlement_lookups_are_rate_limited(tmp_path, monkeypatch):
    """Regression test: an earlier version queried classify_settlement_
    lookup for every stuck slug on every FINALIZING cycle -- as often as
    refresh_interval_seconds, seconds under a live-tuned .env -- hammering
    the REST settlement/metadata endpoints with no benefit, since
    settlement status can't change that fast."""
    now = {"value": 10_000.0}
    monkeypatch.setattr(dryrun_module.time, "time", lambda: now["value"])
    tracker = _tracker_with_stuck_inventory(tmp_path)
    settlement_lookup = Mock(return_value={
        "slug": "m1", "status": "UNRESOLVED", "retrieved_at_epoch": 0.0,
    })
    monkeypatch.setattr(dryrun_module, "classify_settlement_lookup", settlement_lookup)
    runner, _store, _market_ws, read_client = _runner(
        tmp_path=tmp_path, monkeypatch=monkeypatch, tracker=tracker,
    )
    read_client.get_market_book.return_value = None
    runner.policy = DryRunPolicy(
        max_finalizing_wait_seconds=1_000_000.0, settlement_lookup_interval_seconds=60.0,
    )

    runner.run_one_cycle()
    assert settlement_lookup.call_count == 1

    now["value"] += 10.0  # well under the 60s settlement interval
    runner.run_one_cycle()
    assert settlement_lookup.call_count == 1

    now["value"] += 60.0
    runner.run_one_cycle()
    assert settlement_lookup.call_count == 2
