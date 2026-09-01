from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live import market_observation as observation_module
from polymarket_bot.live.market_observation import (
    DRY_RUN_PHASE_COLLECTING,
    DRY_RUN_PHASE_COMPLETE,
    DRY_RUN_PHASE_FINALIZING,
    DRY_RUN_PHASE_GRACE,
    MarketObservationTracker,
    ObservationContinuationError,
    ObservationCoverageCompletionError,
    ObservationSpecMismatchError,
    PROFILE_CONTROLLED,
    PROFILE_JULY5_STYLE,
    PROFILE_LEGACY,
    PRIMARY_STRATEGY,
    SHADOW_STRATEGIES,
    _flat_entry_prices,
    _paper_position_state,
    classify_settlement_lookup,
)
from polymarket_bot.polymarket_client import PolymarketClientError


def _settings(**overrides):
    values = dict(
        observation_only_mode=False,
        observation_gate_enabled=True,
        observation_evidence_window_hours=24.0,
        observation_min_observed_seconds=1.0,
        observation_min_trades=1,
        observation_min_hypothetical_fills=1,
        observation_min_fill_rate=0.5,
        observation_min_markout_samples=1,
        observation_min_avg_markout_cents=0.0,
        observation_min_avg_markout_5m_cents=0.0,
        observation_min_distinct_fill_episodes=1,
        observation_fill_episode_gap_seconds=300.0,
        observation_min_paper_round_trips=0,
        observation_min_paper_pnl_usd=-1.0,
        observation_persist_interval_seconds=9999.0,
        websocket_stale_after_seconds=10.0,
        min_edge_cents=0.5,
        max_payoff_loss_to_capture_ratio=30.0,
        require_both_entry_legs=True,
        observation_maker_fee_theta=-0.0125,
        observation_taker_fee_theta=0.06,
    )
    values.update(overrides)
    return config.LiveTradingSettings(**values)


def _book(bid=0.48, ask=0.52):
    return {
        "bids": [{"price": bid, "quantity": 30.0}],
        "asks": [{"price": ask, "quantity": 30.0}],
    }


def test_session_shadow_markouts_are_profile_and_session_scoped(tmp_path):
    tracker = MarketObservationTracker(
        _settings(), path=tmp_path / "observations.json",
    )
    tracker._state["profiles"][PROFILE_JULY5_STYLE]["markets"] = {
        "m1": {
            "hypothetical_fills": [
                {
                    "strategy": PRIMARY_STRATEGY, "admissible": True,
                    "observed_at_epoch": 199.0, "markout_5m_cents": 99.0,
                },
                {
                    "strategy": PRIMARY_STRATEGY, "admissible": True,
                    "observed_at_epoch": 200.0, "markout_5m_cents": 1.0,
                },
                {
                    "strategy": PRIMARY_STRATEGY, "admissible": True,
                    "observed_at_epoch": 201.0, "markout_5m_cents": 3.0,
                },
                {
                    "strategy": PRIMARY_STRATEGY, "admissible": True,
                    "observed_at_epoch": 202.0, "markout_5m_cents": 50.0,
                    "liquidity_role": "settlement",
                },
                {
                    "strategy": PRIMARY_STRATEGY, "admissible": True,
                    "observed_at_epoch": 203.0, "markout_5m_cents": None,
                },
                {
                    "strategy": "join_both", "admissible": True,
                    "observed_at_epoch": 204.0, "markout_5m_cents": 75.0,
                },
            ],
        },
    }
    tracker._state["profiles"][PROFILE_CONTROLLED]["markets"] = {
        "m1": {"hypothetical_fills": [{
            "strategy": PRIMARY_STRATEGY, "admissible": True,
            "observed_at_epoch": 205.0, "markout_5m_cents": -80.0,
        }]},
    }

    result = tracker.session_shadow_markouts(PROFILE_JULY5_STYLE, 200.0)

    assert result["profile"] == PROFILE_JULY5_STYLE
    assert result["strategy"] == PRIMARY_STRATEGY
    assert result["sample_count"] == 2
    assert result["avg_markout_5m_cents"] == pytest.approx(2.0)


def test_open_inventory_mtm_returns_none_when_relevant_side_unpriced():
    long_fills = [
        {"side": "BUY", "price": 0.40, "quantity": 5.0, "commission_usd": 0.01,
         "observed_at_epoch": 1.0},
    ]
    # Long position exits by selling -- needs best_bid. Missing it is
    # unpriced exposure, not worthless.
    assert observation_module._open_inventory_mtm(
        long_fills, None, 0.55, 0.06,
    ) is None
    # A short exits by buying -- needs best_ask, not best_bid.
    short_fills = [
        {"side": "SELL", "price": 0.60, "quantity": 5.0, "commission_usd": 0.01,
         "observed_at_epoch": 1.0},
    ]
    assert observation_module._open_inventory_mtm(
        short_fills, 0.45, None, 0.06,
    ) is None


def test_open_inventory_mtm_subtracts_prospective_exit_commission():
    fills = [
        {"side": "BUY", "price": 0.40, "quantity": 5.0, "commission_usd": 0.0,
         "observed_at_epoch": 1.0},
    ]
    theta = 0.06
    mtm = observation_module._open_inventory_mtm(fills, 0.50, 0.52, theta)
    # cash = -5*0.40 = -2.0; position=5 marked at best_bid=0.50 => 2.5;
    # minus prospective exit commission on selling 5 @ 0.50.
    expected_exit_commission = theta * 5.0 * 0.50 * (1 - 0.50)
    assert mtm == pytest.approx(-2.0 + 2.5 - expected_exit_commission)


def test_open_inventory_mtm_returns_zero_when_flat():
    fills = [
        {"side": "BUY", "price": 0.40, "quantity": 5.0, "commission_usd": 0.0,
         "observed_at_epoch": 1.0},
        {"side": "SELL", "price": 0.45, "quantity": 5.0, "commission_usd": 0.0,
         "observed_at_epoch": 2.0},
    ]
    assert observation_module._open_inventory_mtm(fills, None, None, 0.06) == 0.0


def test_record_equity_points_stores_incomplete_valuation_not_a_partial_number(tmp_path, monkeypatch):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker.register_market("m1", tick_size=0.01)
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=5.0,
        maker_side="ORDER_SIDE_BUY", trade_time="trade-1",
    )
    for profile in (PROFILE_LEGACY, PROFILE_CONTROLLED, PROFILE_JULY5_STYLE):
        child = tracker._trackers[profile]
        market = child._market("m1")
        # Position stays open (no matching SELL), and the book's bid gets
        # wiped so the open long can't be marked -- forces
        # any_open_market_unpriced.
        market["last_best_bid"] = None

    tracker._record_equity_points(now["value"], force=True)

    for profile in (PROFILE_LEGACY, PROFILE_CONTROLLED, PROFILE_JULY5_STYLE):
        curve = tracker._state["profiles"][profile]["equity_curve"]
        point = curve[-1]
        assert point["valuation_incomplete"] is True
        assert point["total_pnl_usd"] is None


def test_maximum_drawdown_from_curve_skips_none_points_instead_of_crashing():
    curve = [
        {"bucket_epoch": 0, "total_pnl_usd": 5.0},
        {"bucket_epoch": 60, "total_pnl_usd": None, "valuation_incomplete": True},
        {"bucket_epoch": 120, "total_pnl_usd": -3.0},
    ]
    # Must not raise TypeError on float(None); the None point is excluded
    # from the series rather than coerced to a fabricated recovery-to-zero.
    drawdown = observation_module._maximum_drawdown_from_curve(curve, final_pnl=-3.0)
    assert drawdown == pytest.approx(-8.0)


def test_session_shadow_drawdown_usd_uses_last_baseline_at_or_before_since_epoch(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker._state["profiles"][PROFILE_JULY5_STYLE]["equity_curve"] = [
        {"bucket_epoch": 100, "total_pnl_usd": 10.0},
        {"bucket_epoch": 200, "total_pnl_usd": 2.0},  # last point at/before since_epoch=250
        {"bucket_epoch": 300, "total_pnl_usd": 8.0},
        {"bucket_epoch": 400, "total_pnl_usd": -1.0},
    ]

    result = tracker.session_shadow_drawdown_usd(PROFILE_JULY5_STYLE, since_epoch=250.0)

    # Baseline is the 2.0 point (last at/before 250), not the 10.0 point
    # (the first one overall) or the 8.0 point (first strictly after 250).
    # Peak becomes 8.0, trough 2.0 -> -1.0 relative to that peak = -9.0.
    assert result["drawdown_usd"] == pytest.approx(-9.0)
    assert result["sample_count"] == 2
    assert result["incomplete"] is False


def test_session_shadow_drawdown_usd_returns_none_with_no_points_after_since_epoch(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker._state["profiles"][PROFILE_JULY5_STYLE]["equity_curve"] = [
        {"bucket_epoch": 100, "total_pnl_usd": 10.0},
    ]

    result = tracker.session_shadow_drawdown_usd(PROFILE_JULY5_STYLE, since_epoch=250.0)

    assert result is None


def test_session_shadow_drawdown_usd_incomplete_scoped_to_baseline_and_session_points(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker._state["profiles"][PROFILE_JULY5_STYLE]["equity_curve"] = [
        # This early point is incomplete, but it's neither the chosen
        # baseline nor in the session window -- must not taint the result.
        {"bucket_epoch": 50, "total_pnl_usd": None, "valuation_incomplete": True},
        {"bucket_epoch": 200, "total_pnl_usd": 2.0, "valuation_incomplete": False},
        {"bucket_epoch": 300, "total_pnl_usd": 8.0, "valuation_incomplete": False},
    ]

    result = tracker.session_shadow_drawdown_usd(PROFILE_JULY5_STYLE, since_epoch=250.0)

    assert result["incomplete"] is False


def test_session_shadow_drawdown_usd_rejects_unknown_profile(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")

    with pytest.raises(ValueError):
        tracker.session_shadow_drawdown_usd("not_a_real_profile", since_epoch=0.0)


def test_dry_run_phase_defaults_to_collecting(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    assert tracker.dry_run_phase() == DRY_RUN_PHASE_COLLECTING
    assert tracker.dry_run_grace_deadline_epoch() is None


def test_advance_dry_run_to_grace_freezes_entries_and_sets_deadline(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker.register_market("m1", tick_size=0.01)
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    assert tracker._active[PROFILE_JULY5_STYLE] == {"m1"}

    advanced = tracker.advance_dry_run_to_grace(1000.0, grace_seconds=300.0)

    assert advanced is True
    assert tracker.dry_run_phase() == DRY_RUN_PHASE_GRACE
    assert tracker.dry_run_grace_deadline_epoch() == pytest.approx(1300.0)
    # Entries are frozen the same way the archive's own evaluation deadline
    # freezes them -- immediately, across every profile.
    assert tracker._active[PROFILE_JULY5_STYLE] == set()

    # And the freeze must survive a later record_book() call -- never
    # reopening entries once GRACE has begun.
    tracker.record_book("m1", _book())
    assert tracker._active[PROFILE_JULY5_STYLE] == set()


def test_advance_dry_run_to_grace_is_forward_only(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    assert tracker.advance_dry_run_to_grace(1000.0, grace_seconds=300.0) is True
    # A second call once already in GRACE is a no-op -- must not reset the
    # deadline or re-run the freeze.
    assert tracker.advance_dry_run_to_grace(1200.0, grace_seconds=300.0) is False
    assert tracker.dry_run_grace_deadline_epoch() == pytest.approx(1300.0)


def test_advance_dry_run_to_finalizing_requires_grace_phase(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    # Still COLLECTING -- must refuse to skip straight to FINALIZING.
    assert tracker.advance_dry_run_to_finalizing(1000.0) is False
    assert tracker.dry_run_phase() == DRY_RUN_PHASE_COLLECTING

    tracker.advance_dry_run_to_grace(1000.0, grace_seconds=300.0)
    assert tracker.advance_dry_run_to_finalizing(1300.0) is True
    assert tracker.dry_run_phase() == DRY_RUN_PHASE_FINALIZING
    # Idempotent/forward-only: calling again (e.g. a restart mid-FINALIZING
    # re-checking the deadline) is a no-op, not an error.
    assert tracker.advance_dry_run_to_finalizing(1301.0) is False


def test_finalize_dry_run_evaluation_sweeps_open_inventory_to_flat(tmp_path, monkeypatch):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker.register_market("m1", tick_size=0.01)
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=5.0,
        maker_side="ORDER_SIDE_BUY", trade_time="trade-1",
    )
    assert tracker.open_inventory_slugs() == {"m1"}

    result = tracker.finalize_dry_run_evaluation(lambda slug: _book(bid=0.50, ask=0.52))

    assert result["attempted"] is True
    assert result["complete"] is True
    assert tracker.open_inventory_slugs() == set()


def test_finalize_dry_run_evaluation_is_idempotent_once_verdict_recorded(tmp_path, monkeypatch):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker.advance_dry_run_to_grace(now["value"], grace_seconds=0.0)
    tracker.advance_dry_run_to_finalizing(now["value"])
    tracker.complete_dry_run({"verdict": "PASS"}, now["value"])

    book_provider = Mock(return_value=_book())
    result = tracker.finalize_dry_run_evaluation(book_provider)

    assert result["already_finalized"] is True
    assert result["verdict"] == {"verdict": "PASS"}
    book_provider.assert_not_called()


def test_complete_dry_run_requires_finalizing_phase(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    # Still COLLECTING -- must refuse to record a verdict early.
    assert tracker.complete_dry_run({"verdict": "PASS"}, 1000.0) is False
    assert tracker.dry_run_phase() == DRY_RUN_PHASE_COLLECTING


def test_complete_dry_run_records_verdict_and_phase_together(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker.advance_dry_run_to_grace(1000.0, grace_seconds=0.0)
    tracker.advance_dry_run_to_finalizing(1000.0)

    completed = tracker.complete_dry_run({"verdict": "FAIL"}, 1001.0)

    assert completed is True
    assert tracker.dry_run_phase() == DRY_RUN_PHASE_COMPLETE
    assert tracker._state["dry_run_verdict"] == {"verdict": "FAIL"}

    # A verdict, once recorded, is never overwritten by a repeated call.
    assert tracker.complete_dry_run({"verdict": "PASS"}, 1002.0) is False
    assert tracker._state["dry_run_verdict"] == {"verdict": "FAIL"}


def test_record_dry_run_snapshot_persists_to_state(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    snapshot = {"phase": DRY_RUN_PHASE_COLLECTING, "verdict": "PROVISIONAL"}

    tracker.record_dry_run_snapshot(snapshot, 1000.0)

    assert tracker._state["dry_run_snapshot"] == snapshot


def test_override_profile_allocation_pins_the_exact_slugs(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker.set_live_candidate_slugs(["m1", "m2", "m3"])

    tracker.override_profile_allocation(PROFILE_JULY5_STYLE, ["m2"])

    assert tracker._active[PROFILE_JULY5_STYLE] == {"m2"}
    # No market has any recorded spread yet, so the untouched profiles'
    # own ranking naturally picks nothing -- confirms the override affects
    # only the pinned profile.
    assert tracker._active[PROFILE_CONTROLLED] == set()
    assert tracker._active[PROFILE_LEGACY] == set()


def test_override_profile_allocation_caps_at_observation_profile_max_markets(tmp_path):
    settings = _settings(observation_profile_max_markets=2)
    tracker = MarketObservationTracker(settings, path=tmp_path / "observations.json")

    tracker.override_profile_allocation(PROFILE_JULY5_STYLE, ["m1", "m2", "m3"])

    assert len(tracker._active[PROFILE_JULY5_STYLE]) == 2


def test_override_profile_allocation_cap_follows_caller_order_not_hash_order(tmp_path):
    """Regression test: converting the pinned markets to a set before
    capping made which markets survived a cap arbitrary (Python's hash-
    based set iteration order), independent of the caller's own priority
    order -- meaning the shadow tracker could end up watching different
    markets than the real maker actually posted to. The cap must instead
    keep exactly the first N in the caller's given order."""
    settings = _settings(observation_profile_max_markets=2)
    tracker = MarketObservationTracker(settings, path=tmp_path / "observations.json")

    tracker.override_profile_allocation(
        PROFILE_JULY5_STYLE, ["m5", "m4", "m3", "m2", "m1"],
    )
    assert tracker._active[PROFILE_JULY5_STYLE] == {"m5", "m4"}

    # Same five markets, reversed priority order -- must change which two
    # survive. A set-based cap would give the identical (arbitrary) result
    # both times, since {"m5",...,"m1"} == {"m1",...,"m5"} as sets.
    tracker.override_profile_allocation(
        PROFILE_JULY5_STYLE, ["m1", "m2", "m3", "m4", "m5"],
    )
    assert tracker._active[PROFILE_JULY5_STYLE] == {"m1", "m2"}


def test_override_profile_allocation_deduplicates_preserving_first_occurrence(tmp_path):
    settings = _settings(observation_profile_max_markets=2)
    tracker = MarketObservationTracker(settings, path=tmp_path / "observations.json")

    tracker.override_profile_allocation(PROFILE_JULY5_STYLE, ["m1", "m1", "m2", "m3"])

    assert tracker._active[PROFILE_JULY5_STYLE] == {"m1", "m2"}


def test_override_profile_allocation_rejects_unknown_profile(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")

    with pytest.raises(ValueError):
        tracker.override_profile_allocation("not_a_real_profile", ["m1"])


def test_override_profile_allocation_persists_across_later_refreshes(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker.register_market("m1", tick_size=0.01, question="Will X happen?")
    tracker.register_market("m2", tick_size=0.01, question="Will Y happen?")
    tracker.set_live_candidate_slugs(["m1", "m2"])
    tracker.override_profile_allocation(PROFILE_JULY5_STYLE, ["m1"])

    # A later book update for a DIFFERENT market -- one the natural ranking
    # would otherwise be free to pick -- must not dislodge the pin.
    tracker.record_book("m2", _book())

    assert tracker._active[PROFILE_JULY5_STYLE] == {"m1"}


def test_trade_through_hypothetical_improved_bid_records_fill_and_markout(tmp_path, monkeypatch):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker.register_market("m1", tick_size=0.01, question="Will X happen?")
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())  # hypothetical quote is 0.49 / 0.51

    now["value"] = 1002.0
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=5.0,
        maker_side="ORDER_SIDE_BUY", trade_time="trade-1",
    )
    now["value"] = 1303.0
    tracker.record_book("m1", _book(bid=0.49, ask=0.51))

    row = tracker.report()[0]
    assert row["trade_count"] == 1
    assert row["hypothetical_fill_count"] == 1
    # Shadow markouts are executable-price-based (BUY marks against
    # best_bid, the price actually achievable to sell out right now), not
    # midpoint-based. The fill executed at the resting bid (0.49, the
    # improved price), and the bid is still 0.49 at markout time, so
    # there's no realizable edge from closing now -- 0.0, not the
    # midpoint's illusory 1.0.
    assert row["avg_markout_1m_cents"] == pytest.approx(0.0)
    # Schema v4 no longer promotes one market after one apparent fill.  The
    # controlled portfolio must finish the fixed 48-hour/sample gate.
    assert row["entry_eligible"] is False


def test_wrong_trade_direction_does_not_count_as_hypothetical_fill(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker.register_market("m1", tick_size=0.01)
    tracker.record_book("m1", _book())

    tracker.record_trade(
        "m1", price=0.48, quantity=5.0,
        maker_side="ORDER_SIDE_SELL", trade_time="trade-1",
    )

    row = tracker.report()[0]
    assert row["trade_count"] == 1
    assert row["hypothetical_fill_count"] == 0


def test_observation_only_mode_blocks_entry_even_with_evidence(tmp_path):
    tracker = MarketObservationTracker(
        _settings(observation_only_mode=True, observation_min_observed_seconds=0),
        path=tmp_path / "observations.json",
    )
    tracker.register_market("m1", tick_size=0.01)

    allowed, reasons = tracker.entry_eligible("m1")

    assert allowed is False
    assert "observation-only mode is enabled" in reasons


def test_persisted_evidence_survives_restart(tmp_path):
    path = tmp_path / "observations.json"
    tracker = MarketObservationTracker(_settings(), path=path)
    tracker.register_market("m1", tick_size=0.01)
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=2.0,
        maker_side="ORDER_SIDE_BUY", trade_time="trade-1",
    )
    tracker.flush()

    reloaded = MarketObservationTracker(_settings(), path=path)

    assert reloaded.report()[0]["trade_count"] == 1
    assert reloaded.report()[0]["hypothetical_fill_count"] == 1


def test_nonempty_candidate_pool_reports_a_silent_feed_as_stalled(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(
        _settings(observation_feed_stale_after_seconds=300.0),
        path=tmp_path / "observations.json",
    )
    tracker.set_live_candidate_slugs(["m1"])

    now["value"] = 1301.0

    assert tracker.feed_health()["stalled"] is True
    tracker.register_market("m1", tick_size=0.01)
    tracker.record_book("m1", _book())
    assert tracker.feed_health()["stalled"] is False


def test_websocket_heartbeat_keeps_quiet_observation_feed_healthy_without_faking_a_book(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(
        _settings(observation_feed_stale_after_seconds=300.0),
        path=tmp_path / "observations.json",
    )
    tracker.set_live_candidate_slugs(["m1"])
    now["value"] = 1301.0
    assert tracker.feed_health()["stalled"] is True

    tracker.record_feed_activity()

    health = tracker.feed_health()
    assert health["stalled"] is False
    assert health["latest_activity_epoch"] == 1301.0
    assert tracker._state["last_feed_book_epoch"] == 0.0
    assert tracker._state["last_feed_activity_epoch"] == 1301.0


def test_empty_candidate_pool_reports_a_stalled_observer(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(
        _settings(observation_feed_stale_after_seconds=300.0),
        path=tmp_path / "observations.json",
    )
    tracker.set_live_candidate_slugs([])

    assert tracker.feed_health()["stalled"] is False
    now["value"] = 1301.0
    health = tracker.feed_health()

    assert health["required"] is True
    assert health["stalled"] is True
    assert health["reason"] == "empty_candidate_pool"


def test_join_bbo_shadow_variant_waits_for_queue_ahead(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker.register_market("m1", tick_size=0.01)
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())

    tracker.record_trade(
        "m1", price=0.48, quantity=20.0,
        maker_side="ORDER_SIDE_BUY", trade_time="trade-1",
    )
    tracker.record_trade(
        "m1", price=0.48, quantity=15.0,
        maker_side="ORDER_SIDE_BUY", trade_time="trade-2",
    )
    tracker.record_trade(
        "m1", price=0.48, quantity=50.0,
        maker_side="ORDER_SIDE_BUY", trade_time="trade-3",
    )

    row = tracker.report()[0]
    assert row["variant_stats"]["join_both"]["fill_count"] == 1


def test_non_candidate_trade_never_qualifies_for_entry_evidence(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "observations.json")
    tracker.register_market("m1", tick_size=0.01)
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=5.0,
        maker_side="ORDER_SIDE_BUY", trade_time="trade-1",
    )

    row = tracker.report()[0]
    assert row["trade_count"] == 1
    assert row["qualifying_trade_count"] == 0
    assert row["hypothetical_fill_count"] == 0


def test_shadow_strategy_becomes_reduce_only_and_completes_round_trip(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(
        _settings(order_shares_min=1.0, flat_first_inventory_enabled=True),
        path=tmp_path / "observations.json",
    )
    tracker.register_market("m1", tick_size=0.01)
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())

    tracker.record_trade(
        "m1", price=0.48, quantity=5.0,
        maker_side="ORDER_SIDE_BUY", trade_time="entry",
    )
    # The entry-side bid is removed immediately. A second same-direction
    # trade cannot stack more shadow inventory before the next book update.
    now["value"] = 1001.0
    tracker.record_trade(
        "m1", price=0.48, quantity=5.0,
        maker_side="ORDER_SIDE_BUY", trade_time="would-stack",
    )

    now["value"] = 1061.0
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.52, quantity=5.0,
        maker_side="ORDER_SIDE_SELL", trade_time="exit",
    )

    row = tracker.report()[0]
    assert row["hypothetical_fill_count"] == 2
    assert row["paper_round_trip_count"] == 1
    assert row["paper_open_position_shares"] == 0
    assert row["paper_realized_pnl_usd"] == pytest.approx(0.0262475)


def test_shadow_inventory_force_flattens_at_real_max_holding_deadline(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(
        _settings(
            order_shares_min=1.0,
            flat_first_inventory_enabled=True,
            hard_flatten_on_max_holding_enabled=True,
            liquidation_max_holding_hours=1.0,
        ),
        path=tmp_path / "observations.json",
    )
    tracker.register_market("m1", tick_size=0.01)
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=5.0,
        maker_side="ORDER_SIDE_BUY", trade_time="entry",
    )

    now["value"] = 4601.0
    tracker.record_book("m1", _book(bid=0.45, ask=0.49))

    row = tracker.report()[0]
    assert row["paper_round_trip_count"] == 1
    assert row["paper_open_position_shares"] == 0
    assert row["paper_realized_pnl_usd"] == pytest.approx(-0.05172625)
    fills = tracker._state["profiles"]["controlled"]["markets"]["m1"][
        "hypothetical_fills"
    ]
    forced = [
        fill for fill in fills
        if fill.get("strategy") == "improve_both"
        and fill.get("liquidity_role") == "taker"
    ]
    assert len(forced) == 1
    assert forced[0]["exit_reason"] == "max_holding"


def test_event_epoch_is_popped_not_masked_for_july5_style_during_record_book(
    tmp_path, monkeypatch,
):
    # Regression guard for the event-epoch-masking three-way branch fix
    # (record_book(), see RUNBOOK 44). Note: for july5_style specifically,
    # hard_flatten_on_max_holding_enabled=False (matching legacy) means
    # _record_due_forced_exits' near_event/max_holding synthetic-fill path
    # never fires either way, so this bug has no *forced-exit* side effect
    # to observe -- the only way to directly verify the branch is to
    # inspect market state during the child.record_book() call itself,
    # before the wrapper restores the true value afterward. Confirmed by
    # temporarily reverting the fix to the old two-way
    # `if profile == PROFILE_LEGACY: ... elif self._controlled_entry_open(...):
    # mask(...)` form: with that bug present, july5_style falls into the
    # controlled-only masking branch and this test fails.
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v5.json")
    kickoff = 5000.0
    tracker.register_market(
        "m1", tick_size=0.01, event_id="event-1", event_or_close_epoch=kickoff,
    )
    tracker.set_live_candidate_slugs(["m1"])

    observed = {}
    child = tracker._trackers[PROFILE_JULY5_STYLE]
    original_record_book = child.record_book

    def _spy(slug, book):
        observed["event_or_close_epoch"] = child._market(slug).get("event_or_close_epoch")
        return original_record_book(slug, book)

    monkeypatch.setattr(child, "record_book", _spy)

    tracker.record_book("m1", _book())

    # Popped (like legacy), not masked into the far future (like controlled
    # would be while its entry window is open).
    assert observed["event_or_close_epoch"] is None
    # And restored to the true value once record_book() returns.
    assert tracker._trackers[PROFILE_JULY5_STYLE]._market("m1")[
        "event_or_close_epoch"
    ] == kickoff


def test_shadow_inventory_market_remains_a_subscription_requirement_after_window(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    tracker.register_market(
        "m1", tick_size=0.01, event_id="event-1",
        event_or_close_epoch=1000.0,
    )
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=100,
        maker_side="ORDER_SIDE_BUY", trade_time="entry",
    )

    # The controlled entry window is long over, but the market must stay on
    # the subscription list until every shadow profile/variant is flat.
    now["value"] = 20000.0
    assert tracker.open_inventory_slugs() == {"m1"}


def test_deadline_finalization_sweeps_depth_and_closes_every_shadow_position(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    tracker.register_market("m1", tick_size=0.01, event_id="event-1")
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=100,
        maker_side="ORDER_SIDE_BUY", trade_time="entry",
    )
    tracker._state["evaluation_completion_mode"] = "wall_clock"
    tracker._state["evaluation_deadline_epoch"] = 1001.0
    now["value"] = 1002.0
    exit_book = {
        "bids": [
            {"price": 0.47, "quantity": 10.0},
            {"price": 0.46, "quantity": 20.0},
        ],
        "asks": [{"price": 0.53, "quantity": 30.0}],
    }

    result = tracker.finalize_evaluation(lambda _slug: exit_book)

    assert result["complete"] is True
    assert tracker.open_inventory_slugs() == set()
    assert tracker.profile_summary("controlled")["open_inventory"] == []
    assert tracker.profile_summary("legacy")["open_inventory"] == []
    legacy_primary = [
        fill for fill in _profile_fills(tracker, "legacy", "m1")
        if fill["strategy"] == "improve_both"
        and fill.get("exit_reason") == "evaluation_deadline"
    ]
    assert [fill["quantity"] for fill in legacy_primary] == pytest.approx(
        [10.0, 7.5]
    )


def test_deadline_finalization_retries_a_transient_book_error(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    tracker.register_market("m1", tick_size=0.01, event_id="event-1")
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=100,
        maker_side="ORDER_SIDE_BUY", trade_time="entry",
    )
    tracker._state["evaluation_completion_mode"] = "wall_clock"
    tracker._state["evaluation_deadline_epoch"] = 1001.0
    now["value"] = 1002.0
    calls = {"count": 0}

    def provider(_slug):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary snapshot race")
        return _book(bid=0.47, ask=0.53)

    result = tracker.finalize_evaluation(provider, retry_seconds=0)

    assert result["complete"] is True
    assert result["book_lookup_attempts"] == {"m1": 2}
    assert calls["count"] == 2


def test_interrupted_feed_is_insufficient_not_strategy_failure(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(
        _settings(observation_min_feed_coverage_ratio=0.90),
        path=tmp_path / "v4.json",
    )
    tracker.register_market("m1", tick_size=0.01, event_id="event-1")
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=10,
        maker_side="ORDER_SIDE_BUY", trade_time="entry",
    )
    tracker._state["evaluation_completion_mode"] = "wall_clock"
    tracker._state["evaluation_deadline_epoch"] = 1100.0
    now["value"] = 1200.0

    summary = tracker.profile_summary("controlled")

    assert summary["status"] == "INSUFFICIENT"
    assert any(
        "market-data coverage" in reason
        for reason in summary["blocked_reasons"]
    )
    assert "open inventory remains at evaluation" in summary["blocked_reasons"]


def test_one_time_continuation_arms_without_resetting_evidence_or_clock(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(
        _settings(observation_evaluation_hours=48.0),
        path=tmp_path / "v4.json",
    )
    tracker.register_market("m1", tick_size=0.01, event_id="event-1")
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    prior_deadline = tracker._state["evaluation_deadline_epoch"]
    prior_market_state = dict(
        tracker._state["profiles"]["controlled"]["markets"]["m1"]
    )
    prior_buckets = dict(tracker._state["feed_minute_buckets"])
    now["value"] = prior_deadline + 1

    result = tracker.arm_continuation(hours=30.0)

    assert result["status"] == "armed"
    assert tracker._state["evaluation_deadline_epoch"] == prior_deadline
    assert tracker._state["profiles"]["controlled"]["markets"]["m1"] == prior_market_state
    assert tracker._state["feed_minute_buckets"] == prior_buckets
    assert tracker.evaluation_complete() is False
    with pytest.raises(ObservationContinuationError, match="already used or armed"):
        tracker.arm_continuation(hours=30.0)


def test_continuation_starts_on_feed_and_keeps_original_coverage_denominator(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(
        _settings(observation_evaluation_hours=48.0),
        path=tmp_path / "v4.json",
    )
    original_deadline = tracker._state["evaluation_deadline_epoch"]
    tracker._state["feed_minute_buckets"] = {
        str(index * 60): True for index in range(600)
    }
    tracker._state["evaluation_finalization"] = {
        "attempted": True,
        "complete": False,
        "missing_book_slugs": ["m1"],
        "unresolved_inventory_slugs": ["m1"],
    }
    now["value"] = original_deadline + 1
    tracker.arm_continuation(hours=30.0)
    tracker.register_market("m1", tick_size=0.01, event_id="event-1")
    tracker.set_live_candidate_slugs(["m1"])
    activation_epoch = original_deadline + 5000
    now["value"] = activation_epoch

    tracker.record_feed_activity()

    continuation = tracker._state["observation_continuation"]
    extended_deadline = activation_epoch + 30 * 3600
    assert continuation["status"] == "active"
    assert tracker._state["evaluation_deadline_epoch"] == extended_deadline
    assert tracker._state["original_evaluation_deadline_epoch"] == original_deadline
    assert "evaluation_finalization" not in tracker._state
    summary = tracker.profile_summary("controlled")
    assert summary["coverage_target_hours"] == pytest.approx(48.0)
    assert summary["feed_coverage_ratio"] == pytest.approx(601 / (48 * 60))

    now["value"] += 1000
    assert tracker.activate_armed_continuation() is None
    assert tracker._state["evaluation_deadline_epoch"] == extended_deadline


def test_continuation_rejects_a_partially_finalized_portfolio(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(
        _settings(observation_evaluation_hours=48.0),
        path=tmp_path / "v4.json",
    )
    now["value"] = tracker._state["evaluation_deadline_epoch"] + 1
    tracker._state["evaluation_finalization"] = {
        "attempted": True,
        "complete": False,
        "missing_book_slugs": ["still-open"],
        "unresolved_inventory_slugs": ["still-open", "already-swept"],
    }

    with pytest.raises(ObservationContinuationError, match="partially finalized"):
        tracker.arm_continuation(hours=30.0)


def test_healthy_feed_completion_preserves_evidence_and_clears_failed_sweep(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(
        _settings(
            observation_evaluation_hours=48.0,
            observation_min_feed_coverage_ratio=0.90,
        ),
        path=tmp_path / "v4.json",
    )
    # Simulates loading an older, pre-v5 wall-clock archive -- a fresh v5
    # tracker now starts directly in healthy_feed_target mode (see
    # RUNBOOK 44), so arm_healthy_feed_completion()'s one-time retroactive
    # transition is only ever reachable from a wall_clock archive.
    tracker._state["evaluation_completion_mode"] = "wall_clock"
    tracker._state["feed_minute_buckets"] = {
        f"old-{index}": True for index in range(1700)
    }
    tracker._state["evaluation_finalization"] = {
        "attempted": True,
        "complete": False,
        "missing_book_slugs": ["m1"],
        "unresolved_inventory_slugs": ["m1"],
    }
    now["value"] = tracker._state["evaluation_deadline_epoch"] + 1

    result = tracker.arm_healthy_feed_completion()

    assert result["healthy_feed_hours_at_arm"] == pytest.approx(1700 / 60)
    assert result["target_healthy_feed_hours"] == pytest.approx(43.2)
    assert result["remaining_healthy_feed_hours_at_arm"] == pytest.approx(
        43.2 - 1700 / 60
    )
    assert tracker._state["evaluation_completion_mode"] == "healthy_feed_target"
    assert len(tracker._state["feed_minute_buckets"]) == 1700
    assert "evaluation_finalization" not in tracker._state
    assert tracker.evaluation_complete() is False
    with pytest.raises(
        ObservationCoverageCompletionError, match="already uses",
    ):
        tracker.arm_healthy_feed_completion()


def test_healthy_feed_completion_ignores_downtime_and_survives_restart(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    path = tmp_path / "v4.json"
    settings = _settings(
        observation_evaluation_hours=1.0,
        observation_min_feed_coverage_ratio=0.50,
        observation_persist_interval_seconds=0.0,
    )
    tracker = MarketObservationTracker(settings, path=path)
    # Simulates loading an older, pre-v5 wall-clock archive (see comment in
    # test_healthy_feed_completion_preserves_evidence_and_clears_failed_sweep).
    tracker._state["evaluation_completion_mode"] = "wall_clock"
    tracker._state["feed_minute_buckets"] = {
        f"old-{index}": True for index in range(10)
    }
    now["value"] = tracker._state["evaluation_deadline_epoch"] + 1
    tracker.arm_healthy_feed_completion()
    tracker.set_live_candidate_slugs(["m1"])

    # Twelve hours stopped consumes no healthy-time allowance.
    now["value"] += 12 * 3600
    assert tracker.profile_summary("controlled")[
        "remaining_healthy_feed_hours"
    ] == pytest.approx(20 / 60)
    assert tracker.evaluation_complete() is False

    for minute in range(20):
        now["value"] += 60
        tracker.record_feed_activity()

    assert tracker.evaluation_complete() is True
    assert tracker._state.get("evaluation_completed_at_epoch") == now["value"]
    assert tracker._state.get("evaluation_entries_frozen_at_epoch") == now["value"]
    tracker.flush()

    restarted = MarketObservationTracker(settings, path=path)
    assert restarted.evaluation_complete() is True
    assert restarted.profile_summary("controlled")[
        "remaining_healthy_feed_hours"
    ] == pytest.approx(0.0)


def test_dynamic_payoff_guard_blocks_shadow_evidence_live_would_reject(
    tmp_path,
):
    tracker = MarketObservationTracker(
        _settings(max_payoff_loss_to_capture_ratio=20.0),
        path=tmp_path / "observations.json",
    )
    tracker.register_market("m1", tick_size=0.01)
    tracker.set_live_candidate_slugs(["m1"])
    # Improved 0.49/0.51 captures 2c, but each side risks roughly 49c:
    # 24.5x exceeds the production 20x cap.
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=5.0,
        maker_side="ORDER_SIDE_BUY", trade_time="unsafe",
    )

    row = tracker.report()[0]
    assert row["qualifying_trade_count"] == 0
    assert row["hypothetical_fill_count"] == 0


def _profile_fills(tracker, profile, slug):
    return tracker._state["profiles"][profile]["markets"][slug][
        "hypothetical_fills"
    ]


def test_profiles_have_independent_queues_sizes_inventory_and_pnl(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    tracker.register_market("m1", tick_size=0.01, event_id="event-1")
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=100,
        maker_side="ORDER_SIDE_BUY", trade_time="shared-tape-1",
    )

    legacy = [
        fill for fill in _profile_fills(tracker, "legacy", "m1")
        if fill["strategy"] == "improve_both"
    ]
    controlled = [
        fill for fill in _profile_fills(tracker, "controlled", "m1")
        if fill["strategy"] == "improve_both"
    ]
    assert legacy[0]["quantity"] == pytest.approx(17.5)
    assert controlled[0]["quantity"] == pytest.approx(1.0)
    assert tracker._trackers["legacy"]._shadow_quotes is not (
        tracker._trackers["controlled"]._shadow_quotes
    )
    assert tracker.profile_summary("legacy")["open_inventory"][0]["shares"] == 17.5
    assert tracker.profile_summary("controlled")["open_inventory"][0]["shares"] == 1.0


def test_scheduled_refresh_keeps_quiet_resting_quote_live_past_global_stale_limit(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(
        _settings(websocket_stale_after_seconds=10.0),
        path=tmp_path / "v4.json",
    )
    tracker.register_market("m1", tick_size=0.01, event_id="event-1")
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())

    # No intervening book update. A real order refreshed/kept every 60s is
    # still resting; the unrelated 10s live-data guard must not erase it.
    now["value"] = 1301.0
    tracker.record_trade(
        "m1", price=0.48, quantity=10,
        maker_side="ORDER_SIDE_BUY", trade_time="quiet-market-fill",
    )

    controlled = [
        fill for fill in _profile_fills(tracker, "controlled", "m1")
        if fill["strategy"] == "improve_both" and fill["admissible"]
    ]
    trade = tracker._state["profiles"]["controlled"]["markets"]["m1"][
        "trades"
    ][0]
    assert len(controlled) == 1
    assert trade["primary_quote_admissible"] is True
    assert trade["primary_quote_age_before_scheduled_refresh_seconds"] == 301.0


def test_scheduled_refresh_preserves_join_queue_ahead(tmp_path, monkeypatch):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    tracker.register_market("m1", tick_size=0.01, event_id="event-1")
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())

    now["value"] = 1301.0
    tracker.record_trade(
        "m1", price=0.48, quantity=20,
        maker_side="ORDER_SIDE_BUY", trade_time="queue-depletion-1",
    )
    assert tracker.report()[0]["variant_stats"]["join_both"]["fill_count"] == 0
    now["value"] = 1362.0
    tracker.record_trade(
        "m1", price=0.48, quantity=15,
        maker_side="ORDER_SIDE_BUY", trade_time="queue-depletion-2",
    )
    assert tracker.report()[0]["variant_stats"]["join_both"]["fill_count"] == 1


def test_controlled_pauses_at_last_hour_and_reopens_at_kickoff(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    tracker.register_market(
        "m1", tick_size=0.01, event_id="event-1",
        event_or_close_epoch=4600.0,
    )
    tracker.set_live_candidate_slugs(["m1"])

    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=10,
        maker_side="ORDER_SIDE_BUY", trade_time="exactly-last-hour",
    )
    assert not _profile_fills(tracker, "controlled", "m1")

    now["value"] = 4600.0
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=10,
        maker_side="ORDER_SIDE_BUY", trade_time="kickoff",
    )
    assert len([
        fill for fill in _profile_fills(tracker, "controlled", "m1")
        if fill["strategy"] == "improve_both"
    ]) == 1


def test_controlled_entry_cutoff_and_inplay_forced_exit_exact_boundaries(
    tmp_path, monkeypatch,
):
    now = {"value": 1001.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    tracker.register_market(
        "m1", tick_size=0.01, event_id="event-1",
        event_or_close_epoch=1000.0,
    )
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=10,
        maker_side="ORDER_SIDE_BUY", trade_time="entry",
    )

    now["value"] = 11800.0  # exactly three hours after event start
    tracker.record_book("m1", _book(bid=0.45, ask=0.49))
    primary = [
        fill for fill in _profile_fills(tracker, "controlled", "m1")
        if fill["strategy"] == "improve_both"
    ]
    assert primary[-1]["exit_reason"] == "in_play_deadline"
    assert _paper_position(primary) == 0

    tracker.register_market(
        "m2", tick_size=0.01, event_id="event-2",
        event_or_close_epoch=1000.0,
    )
    tracker.set_live_candidate_slugs(["m2"])
    now["value"] = 10000.0  # exactly 2h30m: final-30m cutoff begins
    tracker.record_book("m2", _book())
    tracker.record_trade(
        "m2", price=0.48, quantity=10,
        maker_side="ORDER_SIDE_BUY", trade_time="cutoff",
    )
    assert not _profile_fills(tracker, "controlled", "m2")


def _paper_position(fills):
    return sum(
        float(fill["quantity"]) * (1 if fill["side"] == "BUY" else -1)
        for fill in fills
    )


def test_controlled_stops_after_two_round_trips_per_market(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    tracker.register_market("m1", tick_size=0.01, event_id="event-1")
    tracker.set_live_candidate_slugs(["m1"])

    for trip in range(2):
        tracker.record_book("m1", _book())
        tracker.record_trade(
            "m1", price=0.48, quantity=10,
            maker_side="ORDER_SIDE_BUY", trade_time=f"entry-{trip}",
        )
        now["value"] += 61
        tracker.record_book("m1", _book())
        tracker.record_trade(
            "m1", price=0.52, quantity=10,
            maker_side="ORDER_SIDE_SELL", trade_time=f"exit-{trip}",
        )
        now["value"] += 61

    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=10,
        maker_side="ORDER_SIDE_BUY", trade_time="third-entry",
    )
    primary = [
        fill for fill in _profile_fills(tracker, "controlled", "m1")
        if fill["strategy"] == "improve_both"
    ]
    assert len(primary) == 4
    assert tracker.report()[0]["paper_round_trip_count"] == 2


def test_controlled_short_round_trip_and_partial_fill(tmp_path, monkeypatch):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    tracker.register_market("m1", tick_size=0.01, event_id="event-1")
    tracker.set_live_candidate_slugs(["m1"])
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.52, quantity=0.4,
        maker_side="ORDER_SIDE_SELL", trade_time="partial-short",
    )
    now["value"] += 61
    tracker.record_book("m1", _book())
    tracker.record_trade(
        "m1", price=0.48, quantity=1.0,
        maker_side="ORDER_SIDE_BUY", trade_time="cover",
    )

    primary = [
        fill for fill in _profile_fills(tracker, "controlled", "m1")
        if fill["strategy"] == "improve_both"
    ]
    assert [fill["quantity"] for fill in primary] == pytest.approx([0.4, 0.4])
    assert _paper_position(primary) == 0
    assert tracker.report()[0]["paper_round_trip_count"] == 1


def test_controlled_allocation_is_five_markets_with_three_per_event(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    slugs = [f"m{i}" for i in range(7)]
    tracker.set_live_candidate_slugs(slugs)
    for index, slug in enumerate(slugs):
        event_id = "shared" if index < 5 else f"event-{index}"
        tracker.register_market(slug, tick_size=0.01, event_id=event_id)
        tracker.record_book(slug, _book(bid=0.40, ask=0.40 + 0.02 + index * 0.01))

    controlled = tracker._active["controlled"]
    assert len(controlled) == 5
    shared_count = sum(
        tracker._state["profiles"]["controlled"]["markets"][slug]["event_id"]
        == "shared"
        for slug in controlled
    )
    assert shared_count == 3
    assert len(tracker._active["legacy"]) == 5


def test_missing_api_event_id_uses_shared_slug_event_bucket(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    siblings = [
        "astatc-ufc-janbla-navsti-2026-08-01-rov-f2-r1",
        "astatc-ufc-janbla-navsti-2026-08-01-rov-f2-r2",
    ]
    for slug in siblings:
        tracker.register_market(
            slug,
            tick_size=0.01,
            event_id="",
            raw={"marketType": "props"},
        )

    ids = {
        tracker._state["profiles"]["controlled"]["markets"][slug]["event_id"]
        for slug in siblings
    }
    assert ids == {"ufc-janbla-navsti-2026-08-01"}


def test_cohort_requires_round_trips_from_two_distinct_events(tmp_path):
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    trips = [
        {
            "cohort_key": "controlled|moneyline|in_play|0-10c|normal",
            "event_id": "event-1" if index < 4 else "event-2",
            "pnl_usd": 0.02,
        }
        for index in range(5)
    ]
    fills = [
        {
            "cohort_key": trips[0]["cohort_key"],
            "markout_5m_cents": 0.1,
        }
    ]
    row = tracker._cohort_rows("controlled", trips, fills)[0]
    assert row["completed_round_trips"] == 5
    assert row["distinct_event_count"] == 2
    assert row["live_eligible"] is True


@pytest.mark.parametrize(
    ("change", "expected_status"),
    [
        ({"completed_round_trips": 19}, "INSUFFICIENT"),
        ({"distinct_event_count": 4}, "INSUFFICIENT"),
        ({"qualifying_cohort_count": 0}, "INSUFFICIENT"),
        ({"avg_markout_5m_cents": None}, "INSUFFICIENT"),
        ({"open_inventory": [{"market_slug": "m1"}]}, "FAIL"),
        ({"total_pnl_usd": 0.0}, "FAIL"),
        ({"profit_factor": 1.19}, "FAIL"),
        ({"avg_markout_5m_cents": -0.01}, "FAIL"),
        ({"maximum_drawdown_usd": -3.01}, "FAIL"),
        ({"event_profit_concentration": 0.51}, "FAIL"),
    ],
)
def test_controlled_gate_checks_each_failure_independently(
    tmp_path, monkeypatch, change, expected_status,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    tracker._state["evaluation_completion_mode"] = "wall_clock"
    tracker._state["evaluation_deadline_epoch"] = 999.0
    summary = {
        "completed_round_trips": 20,
        "distinct_event_count": 5,
        "qualifying_cohort_count": 1,
        "avg_markout_5m_cents": 0.1,
        "open_inventory": [],
        "total_pnl_usd": 1.0,
        "profit_factor": 1.20,
        "maximum_drawdown_usd": -3.0,
        "event_profit_concentration": 0.50,
        "qualification_config_matches": True,
    }
    summary.update(change)
    assert tracker._controlled_status(summary)["status"] == expected_status


def test_controlled_gate_requires_feed_coverage_and_deadline_finalization(
    tmp_path, monkeypatch,
):
    now = {"value": 1000.0}
    monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
    tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
    tracker._state["evaluation_completion_mode"] = "wall_clock"
    tracker._state["evaluation_deadline_epoch"] = 999.0
    summary = {
        "completed_round_trips": 20,
        "distinct_event_count": 5,
        "qualifying_cohort_count": 1,
        "avg_markout_5m_cents": 0.1,
        "open_inventory": [],
        "open_shadow_strategy_positions": 0,
        "total_pnl_usd": 1.0,
        "profit_factor": 1.20,
        "maximum_drawdown_usd": -3.0,
        "event_profit_concentration": 0.50,
        "qualification_config_matches": True,
        "feed_coverage_ratio": 1.0,
        "feed_stale_at_deadline_seconds": 0.0,
        "evaluation_finalization": {"attempted": True, "complete": True},
    }

    assert tracker._controlled_status(summary)["status"] == "PASS"

    summary["feed_coverage_ratio"] = 0.89
    assert tracker._controlled_status(summary)["status"] == "INSUFFICIENT"
    summary["feed_coverage_ratio"] = 1.0
    summary["evaluation_finalization"] = {
        "attempted": True,
        "complete": False,
        "missing_book_slugs": ["m1"],
    }
    assert tracker._controlled_status(summary)["status"] == "INSUFFICIENT"


def test_incompatible_archive_is_preserved_and_not_counted(tmp_path):
    path = tmp_path / "observations.json"
    observation_module.storage.save_json(
        path,
        {"version": 3, "markets": {"old": {"hypothetical_fills": [{}]}}},
    )
    tracker = MarketObservationTracker(_settings(), path=path)

    assert tracker.report() == []
    archives = list(
        tmp_path.glob("observations.schema-3-model-legacy-diagnostic*.json")
    )
    assert len(archives) == 1
    assert observation_module.storage.load_json(archives[0], {})["version"] == 3


def test_prior_v4_model_revision_is_archived_and_not_qualified(tmp_path):
    path = tmp_path / "observations.json"
    observation_module.storage.save_json(
        path,
        {
            "version": 4,
            "model_revision": 3,
            "profiles": {
                "controlled": {"markets": {"old": {"hypothetical_fills": [{}]}}},
                "legacy": {"markets": {}},
            },
        },
    )

    tracker = MarketObservationTracker(_settings(), path=path)

    assert tracker.report() == []
    assert tracker._state["model_revision"] == observation_module.OBSERVATION_MODEL_REVISION
    archives = list(
        tmp_path.glob("observations.schema-4-model-3-diagnostic*.json")
    )
    assert len(archives) == 1


# ---------------------------------------------------------------------
# Settlement-based finalization (for shadow positions on markets that
# have genuinely resolved -- no book will ever come back for these, so
# finalize_evaluation()'s book-based sweep can never close them).
# ---------------------------------------------------------------------

def _seed_open_position(
    tracker, profile, slug, *, side="BUY", quantity=10.0, price=0.40,
    strategies=SHADOW_STRATEGIES, event_id="event-1",
):
    market = tracker._trackers[profile]._market(slug)
    market["event_id"] = event_id
    fills = market.setdefault("hypothetical_fills", [])
    for strategy in strategies:
        fills.append({
            "key": f"seed|{profile}|{strategy}|{slug}",
            "observed_at_epoch": 1000.0,
            "side": side,
            "price": price,
            "quantity": quantity,
            "strategy": strategy,
            "admissible": True,
            "liquidity_role": "maker",
            "role": "entry",
            "commission_usd": 0.01,
            "position_before": 0.0,
            "cohort_key": f"{profile}|props|in_play|10-25c|normal",
        })


def _settled_lookup(slug, price, retrieved_at_epoch=2000.0, metadata_synced_at="2026-08-08T00:00:00Z"):
    return {
        "slug": slug, "status": "SETTLED", "settlement_price": price,
        "metadata_synced_at": metadata_synced_at,
        "retrieved_at_epoch": retrieved_at_epoch,
    }


class TestFillPriceValidation:
    def test_ordinary_fill_rejects_zero_and_one(self):
        assert observation_module._fill_price_is_valid({}, 0.0) is False
        assert observation_module._fill_price_is_valid({}, 1.0) is False
        assert observation_module._fill_price_is_valid({}, 0.5) is True

    def test_settlement_fill_accepts_zero_and_one(self):
        fill = {"liquidity_role": "settlement"}
        assert observation_module._fill_price_is_valid(fill, 0.0) is True
        assert observation_module._fill_price_is_valid(fill, 1.0) is True
        assert observation_module._fill_price_is_valid(fill, 0.37) is True

    def test_settlement_fill_still_rejects_out_of_range(self):
        fill = {"liquidity_role": "settlement"}
        assert observation_module._fill_price_is_valid(fill, -0.01) is False
        assert observation_module._fill_price_is_valid(fill, 1.01) is False


class TestSettleAtResolution:
    def test_long_position_settles_as_a_sell_at_settlement_price(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        _seed_open_position(tracker, "controlled", "m1", side="BUY", quantity=10.0, price=0.40)

        settled = tracker._settle_at_resolution(
            "controlled", "m1", 1.0, 2000.0,
            retrieved_at_epoch=2000.0, metadata_synced_at="2026-08-08T00:00:00Z",
        )

        assert len(settled) == len(SHADOW_STRATEGIES)
        assert all(item["side"] == "SELL" for item in settled)
        assert all(item["quantity"] == pytest.approx(10.0) for item in settled)
        state = observation_module._paper_position_state(
            [f for f in _profile_fills(tracker, "controlled", "m1") if f["strategy"] == "improve_both"]
        )
        assert state["position"] == pytest.approx(0.0)

    def test_short_position_settles_as_a_buy_at_settlement_price(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        _seed_open_position(tracker, "controlled", "m1", side="SELL", quantity=6.0, price=0.60)

        settled = tracker._settle_at_resolution(
            "controlled", "m1", 0.0, 2000.0,
            retrieved_at_epoch=2000.0, metadata_synced_at=None,
        )

        assert all(item["side"] == "BUY" for item in settled)
        state = observation_module._paper_position_state(
            [f for f in _profile_fills(tracker, "controlled", "m1") if f["strategy"] == "improve_both"]
        )
        assert state["position"] == pytest.approx(0.0)

    def test_already_flat_strategy_is_skipped(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        # No open position seeded at all.
        settled = tracker._settle_at_resolution(
            "controlled", "m1", 1.0, 2000.0,
            retrieved_at_epoch=2000.0, metadata_synced_at=None,
        )
        assert settled == []

    def test_settlement_fill_carries_no_fabricated_trade_or_depth_fields(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        _seed_open_position(tracker, "controlled", "m1", side="BUY", quantity=10.0, price=0.40)

        tracker._settle_at_resolution(
            "controlled", "m1", 1.0, 2000.0,
            retrieved_at_epoch=2000.0, metadata_synced_at="2026-08-08T00:00:00Z",
        )

        settlement_fills = [
            f for f in _profile_fills(tracker, "controlled", "m1")
            if f.get("liquidity_role") == "settlement"
        ]
        primary = [f for f in settlement_fills if f["strategy"] == "improve_both"][0]
        assert primary["commission_usd"] == 0.0
        assert primary["closure_type"] == "settlement"
        assert primary["exit_reason"] == "market_resolved"
        assert "trade_price" not in primary
        assert "trade_quantity" not in primary
        assert primary["settlement_retrieved_at_epoch"] == 2000.0
        assert primary["settlement_metadata_synced_at"] == "2026-08-08T00:00:00Z"


class TestApplySettlementBatch:
    def test_settles_both_profiles_and_reports_settlement_pnl(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        _seed_open_position(tracker, "controlled", "m1", side="BUY", quantity=10.0, price=0.40)
        _seed_open_position(tracker, "legacy", "m1", side="BUY", quantity=10.0, price=0.40)
        assert tracker.open_inventory_slugs() == {"m1"}

        result = tracker.apply_settlement_batch([_settled_lookup("m1", 1.0)], 2000.0)

        assert result["settled_slugs"] == ["m1"]
        assert result["complete"] is True
        assert tracker.open_inventory_slugs() == set()
        controlled_summary = tracker.profile_summary("controlled")
        # (1.0 - 0.40) * 10 = 6.0 realized on the primary strategy, entry
        # commission (0.01) still deducted, settlement leg itself fee-free.
        assert controlled_summary["settlement_exit_count"] == 1
        assert controlled_summary["settlement_pnl_usd"] == pytest.approx(6.0 - 0.01)
        assert controlled_summary["realized_pnl_usd"] == pytest.approx(6.0 - 0.01)

    def test_slug_only_leaves_blocker_lists_once_every_profile_is_flat(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        # Only controlled has inventory here -- legacy is already flat.
        _seed_open_position(tracker, "controlled", "m1", side="BUY", quantity=10.0, price=0.40)

        result = tracker.apply_settlement_batch([_settled_lookup("m1", 1.0)], 2000.0)

        assert result["complete"] is True
        assert result["remaining_unresolved_slugs"] == []

    def test_partial_batch_leaves_unresolved_slug_still_blocking(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        tracker.register_market("m2", tick_size=0.01, event_id="event-2")
        _seed_open_position(tracker, "controlled", "m1", side="BUY", quantity=10.0, price=0.40)
        _seed_open_position(tracker, "controlled", "m2", side="BUY", quantity=5.0, price=0.30)

        result = tracker.apply_settlement_batch(
            [
                _settled_lookup("m1", 1.0),
                {"slug": "m2", "status": "UNRESOLVED", "retrieved_at_epoch": 2000.0},
            ],
            2000.0,
        )

        assert result["settled_slugs"] == ["m1"]
        assert result["complete"] is False
        assert result["remaining_unresolved_slugs"] == ["m2"]

    def test_idempotent_rerun_does_not_double_book_fills_or_pnl(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        _seed_open_position(tracker, "controlled", "m1", side="BUY", quantity=10.0, price=0.40)

        tracker.apply_settlement_batch([_settled_lookup("m1", 1.0)], 2000.0)
        first_pnl = tracker.profile_summary("controlled")["realized_pnl_usd"]
        # Rerun against the now-already-flat position.
        result = tracker.apply_settlement_batch([_settled_lookup("m1", 1.0)], 2001.0)
        second_pnl = tracker.profile_summary("controlled")["realized_pnl_usd"]

        assert result["settled_slugs"] == []
        assert second_pnl == pytest.approx(first_pnl)

    def test_settlement_close_does_not_count_as_a_forced_exit(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        _seed_open_position(tracker, "controlled", "m1", side="BUY", quantity=10.0, price=0.40)

        tracker.apply_settlement_batch([_settled_lookup("m1", 1.0)], 2000.0)

        summary = tracker.profile_summary("controlled")
        assert summary["forced_exit_count"] == 0
        assert summary["forced_exit_pnl_usd"] == 0.0
        assert summary["settlement_exit_count"] == 1

    def test_settlement_fill_excluded_from_hypothetical_fill_count(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        _seed_open_position(
            tracker, "controlled", "m1", side="BUY", quantity=10.0, price=0.40,
            strategies=["improve_both"],
        )
        before = tracker.profile_summary("controlled")["hypothetical_fill_count"]

        tracker.apply_settlement_batch([_settled_lookup("m1", 1.0)], 2000.0)

        after = tracker.profile_summary("controlled")["hypothetical_fill_count"]
        # The entry fill still counts; the synthetic settlement exit must not.
        assert after == before

    def test_complete_marks_coverage_completion_status(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        _seed_open_position(tracker, "controlled", "m1", side="BUY", quantity=10.0, price=0.40)
        tracker._state["observation_coverage_completion"] = {
            "status": "ended_finalization_incomplete",
        }

        tracker.apply_settlement_batch([_settled_lookup("m1", 1.0)], 2000.0)

        assert (
            tracker._state["observation_coverage_completion"]["status"] == "completed"
        )

    def test_preserves_original_missing_book_slugs_in_audit_record(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v4.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        _seed_open_position(tracker, "controlled", "m1", side="BUY", quantity=10.0, price=0.40)
        tracker._state["evaluation_finalization"] = {
            "attempted": True, "complete": False,
            "missing_book_slugs": ["m1"], "unresolved_inventory_slugs": ["m1"],
        }

        tracker.apply_settlement_batch([_settled_lookup("m1", 1.0)], 2000.0)

        finalization = tracker._state["evaluation_finalization"]
        assert finalization["original_missing_book_slugs"] == ["m1"]
        assert finalization["missing_book_slugs"] == []
        assert finalization["settlement_pass"]["settled_events"]


class TestClassifySettlementLookup:
    def test_settled_when_both_endpoints_agree(self):
        client = Mock()
        client.get_market_settlement.return_value = {"slug": "m1", "settlement": 1.0}
        client.get_market_metadata.return_value = {
            "slug": "m1", "closed": True, "status": "MARKET_STATUS_RESOLVED", "active": True,
            "updatedAt": "2026-08-08T00:00:00Z",
        }

        result = classify_settlement_lookup(client, "m1")

        assert result["status"] == "SETTLED"
        assert result["settlement_price"] == 1.0
        assert result["metadata_synced_at"] == "2026-08-08T00:00:00Z"

    def test_unresolved_when_settlement_404s_and_metadata_confirms_open(self):
        client = Mock()
        client.get_market_settlement.return_value = None
        client.get_market_metadata.return_value = {
            "slug": "m1", "closed": False, "status": "MARKET_STATUS_OPEN", "active": True,
        }

        result = classify_settlement_lookup(client, "m1")

        assert result["status"] == "UNRESOLVED"

    def test_error_when_settlement_404s_but_metadata_says_resolved(self):
        client = Mock()
        client.get_market_settlement.return_value = None
        client.get_market_metadata.return_value = {
            "slug": "m1", "closed": True, "status": "MARKET_STATUS_RESOLVED", "active": True,
        }

        result = classify_settlement_lookup(client, "m1")

        assert result["status"] == "ERROR"

    def test_error_when_settlement_present_but_metadata_not_resolved(self):
        client = Mock()
        client.get_market_settlement.return_value = {"slug": "m1", "settlement": 1.0}
        client.get_market_metadata.return_value = {
            "slug": "m1", "closed": False, "status": "MARKET_STATUS_OPEN", "active": True,
        }

        result = classify_settlement_lookup(client, "m1")

        assert result["status"] == "ERROR"

    def test_error_when_metadata_is_missing_even_if_settlement_also_404s(self):
        client = Mock()
        client.get_market_settlement.return_value = None
        client.get_market_metadata.return_value = None

        result = classify_settlement_lookup(client, "m1")

        assert result["status"] == "ERROR"

    def test_error_on_settlement_lookup_failure(self):
        client = Mock()
        client.get_market_settlement.side_effect = PolymarketClientError("boom")

        result = classify_settlement_lookup(client, "m1")

        assert result["status"] == "ERROR"
        client.get_market_metadata.assert_not_called()

    def test_error_on_metadata_lookup_failure(self):
        client = Mock()
        client.get_market_settlement.return_value = {"slug": "m1", "settlement": 1.0}
        client.get_market_metadata.side_effect = PolymarketClientError("boom")

        result = classify_settlement_lookup(client, "m1")

        assert result["status"] == "ERROR"

    def test_both_endpoints_always_checked_even_on_settlement_404(self):
        client = Mock()
        client.get_market_settlement.return_value = None
        client.get_market_metadata.return_value = {
            "slug": "m1", "closed": False, "status": "MARKET_STATUS_OPEN", "active": True,
        }

        classify_settlement_lookup(client, "m1")

        client.get_market_metadata.assert_called_once_with("m1")


class TestObservationProfileSpec:
    def test_july5_style_spec_matches_its_own_settings_and_leaves_guards_active(
        self, tmp_path,
    ):
        settings = _settings()
        tracker = MarketObservationTracker(settings, path=tmp_path / "v5.json")
        spec = tracker._profile_specs[PROFILE_JULY5_STYLE]

        assert spec.profile == PROFILE_JULY5_STYLE
        assert spec.max_spread == settings.observation_july5_max_spread
        assert spec.order_shares_min == settings.observation_july5_order_shares
        assert spec.order_shares_max == settings.observation_july5_order_shares
        assert spec.max_started_event_hours == settings.observation_july5_max_started_event_hours
        assert spec.pregame_pause_minutes == 0.0
        assert spec.entry_cutoff_minutes == 0.0
        assert spec.hard_flatten_on_max_holding_enabled is False
        assert spec.flat_first_inventory_enabled is False
        assert spec.ranking_method == "widest_spread_first"
        # The one deliberate difference from legacy: guards are NOT
        # disabled, they fall through to the base settings values.
        assert spec.extreme_price_low_threshold == settings.extreme_price_low_threshold
        assert spec.extreme_price_high_threshold == settings.extreme_price_high_threshold
        assert spec.max_payoff_loss_to_capture_ratio == settings.max_payoff_loss_to_capture_ratio

    def test_legacy_spec_disables_the_extreme_price_and_payoff_guards(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v5.json")
        spec = tracker._profile_specs[PROFILE_LEGACY]

        assert spec.extreme_price_low_threshold == -1.0
        assert spec.extreme_price_high_threshold == 2.0
        assert spec.max_payoff_loss_to_capture_ratio == 1_000_000.0
        assert spec.ranking_method == "widest_spread_first"

    def test_controlled_spec_uses_the_quantity_weighted_ranking_method(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v5.json")
        spec = tracker._profile_specs[PROFILE_CONTROLLED]

        assert spec.ranking_method == "widest_spread_recent_quantity_weighted"
        assert spec.max_round_trips_per_market is not None
        assert spec.max_markets_per_event is not None

    def test_spec_hash_is_stable_for_unchanged_settings(self, tmp_path):
        settings = _settings()
        tracker = MarketObservationTracker(settings, path=tmp_path / "v5.json")
        spec = tracker._profile_specs[PROFILE_JULY5_STYLE]

        assert spec.spec_hash() == spec.spec_hash()
        rebuilt = tracker._build_profile_spec(
            PROFILE_JULY5_STYLE, tracker._july5_settings(),
        )
        assert rebuilt.spec_hash() == spec.spec_hash()

    def test_fresh_archive_persists_spec_and_hash_for_all_three_profiles(self, tmp_path):
        path = tmp_path / "v5.json"
        tracker = MarketObservationTracker(_settings(), path=path)
        tracker.flush()

        for profile in (PROFILE_LEGACY, PROFILE_CONTROLLED, PROFILE_JULY5_STYLE):
            profile_state = tracker._state["profiles"][profile]
            assert profile_state["spec"]["profile"] == profile
            assert profile_state["spec_hash"] == tracker._profile_specs[profile].spec_hash()

    def test_qualification_policy_persisted_and_matches_current_defaults(self, tmp_path):
        settings = _settings()
        tracker = MarketObservationTracker(settings, path=tmp_path / "v5.json")

        policy = tracker._state["qualification_policy"]
        assert policy["primary_strategy"] == observation_module.PRIMARY_STRATEGY
        assert policy["min_round_trips"] == settings.observation_controlled_min_round_trips
        assert policy["min_profit_factor"] == settings.observation_controlled_min_profit_factor
        assert tracker._state["qualification_policy_hash"] == (
            tracker._qualification_policy.policy_hash()
        )


class TestSpecFailClosed:
    def test_mismatched_settings_on_restart_raises_and_does_not_mutate(self, tmp_path):
        path = tmp_path / "v5.json"
        tracker = MarketObservationTracker(_settings(), path=path)
        tracker.flush()
        before = path.read_bytes()

        drifted_settings = _settings(
            observation_july5_max_spread=(
                _settings().observation_july5_max_spread + 0.1
            ),
        )
        with pytest.raises(ObservationSpecMismatchError):
            MarketObservationTracker(drifted_settings, path=path)

        assert path.read_bytes() == before

    def test_unchanged_settings_on_restart_load_cleanly(self, tmp_path):
        path = tmp_path / "v5.json"
        settings = _settings()
        tracker = MarketObservationTracker(settings, path=path)
        tracker.flush()
        original_hash = tracker._state["profiles"][PROFILE_JULY5_STYLE]["spec_hash"]

        restarted = MarketObservationTracker(settings, path=path)

        assert restarted._state["profiles"][PROFILE_JULY5_STYLE]["spec_hash"] == original_hash

    def test_qualification_policy_drift_does_not_raise(self, tmp_path):
        path = tmp_path / "v5.json"
        settings = _settings()
        tracker = MarketObservationTracker(settings, path=path)
        tracker.flush()

        drifted_settings = _settings(
            observation_controlled_min_profit_factor=(
                settings.observation_controlled_min_profit_factor + 1.0
            ),
        )
        # Must not raise -- a policy drift only affects how evidence is
        # later graded, not what was actually traded.
        MarketObservationTracker(drifted_settings, path=path)


class TestHealthyFeedTargetDefaultMode:
    def test_fresh_archive_starts_in_healthy_feed_target_mode(self, tmp_path):
        settings = _settings(observation_evaluation_hours=48.0)
        tracker = MarketObservationTracker(settings, path=tmp_path / "v5.json")

        assert tracker._state["evaluation_completion_mode"] == "healthy_feed_target"
        assert tracker._state["evaluation_healthy_feed_target_seconds"] == pytest.approx(
            48.0 * 3600.0
        )

    def test_restart_does_not_consume_the_healthy_feed_budget(self, tmp_path, monkeypatch):
        now = {"value": 1000.0}
        monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
        path = tmp_path / "v5.json"
        settings = _settings(
            observation_evaluation_hours=1.0,
            observation_persist_interval_seconds=0.0,
        )
        tracker = MarketObservationTracker(settings, path=path)
        tracker._state["feed_minute_buckets"] = {"1000": True, "1060": True}
        tracker.flush()

        # A long stop -- far past what a 1h wall-clock deadline would ever
        # have allowed -- must not itself complete or fail the evaluation.
        now["value"] += 10 * 3600
        restarted = MarketObservationTracker(settings, path=path)
        assert restarted.evaluation_complete() is False
        assert len(restarted._state["feed_minute_buckets"]) == 2


class TestJuly5GuardsRemainActive:
    def test_thin_edge_extreme_price_market_is_rejected_for_july5_style(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v5.json")
        july5_settings = tracker._july5_settings()

        # bid=0.05 is below the 0.15 extreme-price threshold, and the
        # captured spread (1c) is well under extreme_price_min_edge_cents
        # (4c), so the low leg should be rejected.
        safe_bid, safe_ask = _flat_entry_prices(0.05, 0.06, july5_settings)

        assert safe_bid is None

    def test_same_thin_edge_market_would_pass_under_legacys_disabled_guards(self, tmp_path):
        # Revert-and-confirm-failure counterfactual: proves the guard in
        # the test above is actually load-bearing, not incidental.
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v5.json")
        legacy_settings = tracker._legacy_settings()

        safe_bid, safe_ask = _flat_entry_prices(0.05, 0.06, legacy_settings)

        assert safe_bid is not None

    def test_wide_extreme_price_market_still_passes_the_guards(self, tmp_path):
        # The payoff/extreme-price guards bound RELATIVE risk (max loss vs.
        # captured spread), not absolute price extremity -- at a 98c
        # ceiling, a ~1c/98c-quoted market's enormous captured spread
        # swamps both the extreme-price-min-edge test and the payoff-ratio
        # test. This is a real, documented limitation (see RUNBOOK 44):
        # the guards reduce tail risk, they do not eliminate it.
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v5.json")
        july5_settings = tracker._july5_settings()

        safe_bid, safe_ask = _flat_entry_prices(0.01, 0.98, july5_settings)

        assert safe_bid is not None
        assert safe_ask is not None


class TestThreeProfileIndependence:
    def test_fills_inventory_and_equity_never_leak_between_profiles(self, tmp_path, monkeypatch):
        now = {"value": 1000.0}
        monkeypatch.setattr(observation_module.time, "time", lambda: now["value"])
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v5.json")
        tracker.register_market("m1", tick_size=0.01, event_id="event-1")
        tracker.set_live_candidate_slugs(["m1"])
        tracker.record_book("m1", _book())
        now["value"] = 1002.0
        tracker.record_book("m1", _book())
        tracker.record_trade(
            "m1", price=0.48, quantity=100,
            maker_side="ORDER_SIDE_BUY", trade_time="entry",
        )

        for profile in (PROFILE_LEGACY, PROFILE_CONTROLLED, PROFILE_JULY5_STYLE):
            market = tracker._trackers[profile]._market("m1")
            fills = [
                fill for fill in market.get("hypothetical_fills", [])
                if fill.get("strategy") == "improve_both" and fill.get("admissible")
            ]
            if not fills:
                continue
            state = _paper_position_state(fills)
            # Each profile's own fill quantity must match ITS OWN
            # configured order size, never another profile's.
            spec = tracker._profile_specs[profile]
            for fill in fills:
                assert fill["quantity"] <= spec.order_shares_max + 1e-9

        # Equity curves are independent per-profile lists, never shared.
        legacy_curve = tracker._state["profiles"][PROFILE_LEGACY]["equity_curve"]
        july5_curve = tracker._state["profiles"][PROFILE_JULY5_STYLE]["equity_curve"]
        assert legacy_curve is not july5_curve

    def test_finalization_state_is_not_shared_across_profiles(self, tmp_path):
        tracker = MarketObservationTracker(_settings(), path=tmp_path / "v5.json")
        # Each profile has its own markets dict, confirmed distinct objects.
        markets_by_profile = {
            profile: tracker._state["profiles"][profile]["markets"]
            for profile in (PROFILE_LEGACY, PROFILE_CONTROLLED, PROFILE_JULY5_STYLE)
        }
        assert len({id(m) for m in markets_by_profile.values()}) == 3
