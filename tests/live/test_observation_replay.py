import pytest

from polymarket_bot import storage
from polymarket_bot.live import observation_replay as replay
from polymarket_bot.live.market_observation import _estimated_commission


def _fill(
    *, side, price, quantity, epoch, role, strategy="improve_both",
    cohort_key="controlled|props|in_play|10-25c|normal",
    closure_type=None, exit_reason=None, admissible=True, commission_usd=0.0,
):
    fill = {
        "side": side, "price": price, "quantity": quantity,
        "observed_at_epoch": epoch, "role": role, "strategy": strategy,
        "admissible": admissible, "cohort_key": cohort_key,
        "commission_usd": commission_usd,
    }
    if closure_type is not None:
        fill["closure_type"] = closure_type
        if closure_type == "settlement":
            fill["liquidity_role"] = "settlement"
    if exit_reason is not None:
        fill["exit_reason"] = exit_reason
    return fill


def _trade(*, price, quantity, maker_side, epoch):
    return {"price": price, "quantity": quantity, "maker_side": maker_side, "observed_at_epoch": epoch}


def _archive_state(
    *, controlled_markets=None, legacy_markets=None, july5_markets=None,
    qualification_config=None, feed_minute_buckets=None,
    qualification_policy=None, controlled_spec=None, legacy_spec=None,
    july5_spec=None, evaluation_finalization=None,
    evaluation_healthy_feed_target_seconds=None,
):
    profiles = {
        "legacy": {"markets": legacy_markets or {}},
        "controlled": {"markets": controlled_markets or {}},
        "july5_style": {"markets": july5_markets or {}},
    }
    if controlled_spec is not None:
        profiles["controlled"]["spec"] = controlled_spec
    if legacy_spec is not None:
        profiles["legacy"]["spec"] = legacy_spec
    if july5_spec is not None:
        profiles["july5_style"]["spec"] = july5_spec
    state = {
        "qualification_config": qualification_config or {},
        "feed_minute_buckets": feed_minute_buckets or {},
        "profiles": profiles,
    }
    if qualification_policy is not None:
        state["qualification_policy"] = qualification_policy
    if evaluation_finalization is not None:
        state["evaluation_finalization"] = evaluation_finalization
    if evaluation_healthy_feed_target_seconds is not None:
        state["evaluation_healthy_feed_target_seconds"] = (
            evaluation_healthy_feed_target_seconds
        )
    return state


def _escalation_kwargs(**overrides):
    kwargs = dict(
        profile="controlled",
        event_epoch=None,
        max_started_event_hours=3.0,
        controlled_max_holding_hours=1.0,
        controlled_entry_cutoff_minutes=30.0,
        last_observed_activity_epoch=None,
        feed_minute_buckets={},
        maker_fee_theta=-0.0125,
    )
    kwargs.update(overrides)
    return kwargs


def _hard_rule_kwargs(**overrides):
    kwargs = dict(last_observed_activity_epoch=None, feed_minute_buckets={})
    kwargs.update(overrides)
    return kwargs


class TestFindEscalatedExit:
    def test_finds_a_trade_matching_the_strict_first_tier(self):
        trades = [_trade(price=0.55, quantity=5.0, maker_side="ORDER_SIDE_SELL", epoch=1010.0)]

        result = replay._find_escalated_exit(1000.0, "SELL", 0.50, trades, not_after_epoch=2000.0)

        assert result is not None
        assert result["exit_price"] == 0.55
        assert result["cents_given_up"] == 0.0

    def test_escalates_to_a_later_tier_when_no_early_trade_qualifies(self):
        trades = [_trade(price=0.49, quantity=5.0, maker_side="ORDER_SIDE_SELL", epoch=1350.0)]

        result = replay._find_escalated_exit(1000.0, "SELL", 0.50, trades, not_after_epoch=2000.0)

        assert result is not None
        assert result["cents_given_up"] == 2.0

    def test_returns_none_when_nothing_in_the_tape_ever_qualifies(self):
        trades = [_trade(price=0.10, quantity=5.0, maker_side="ORDER_SIDE_BUY", epoch=1010.0)]

        result = replay._find_escalated_exit(1000.0, "SELL", 0.50, trades, not_after_epoch=2000.0)

        assert result is None

    def test_ignores_trades_before_entry_or_after_the_deadline(self):
        trades = [
            _trade(price=0.99, quantity=5.0, maker_side="ORDER_SIDE_SELL", epoch=999.0),
            _trade(price=0.99, quantity=5.0, maker_side="ORDER_SIDE_SELL", epoch=3000.0),
        ]

        result = replay._find_escalated_exit(1000.0, "SELL", 0.50, trades, not_after_epoch=2000.0)

        assert result is None

    def test_buy_side_exit_escalates_upward(self):
        trades = [_trade(price=0.52, quantity=5.0, maker_side="ORDER_SIDE_BUY", epoch=1350.0)]

        result = replay._find_escalated_exit(1000.0, "BUY", 0.50, trades, not_after_epoch=2000.0)

        assert result is not None
        assert result["cents_given_up"] == 2.0


class TestRiskDeadlineEpoch:
    def test_legacy_with_known_kickoff_caps_at_six_hours_post_kickoff(self):
        kickoff = 10000.0
        deadline = replay._risk_deadline_epoch(
            "legacy", entry_epoch=9000.0, opened_at_epoch=9000.0,
            event_epoch=kickoff, max_started_event_hours=6.0,
        )
        assert deadline == pytest.approx(kickoff + 6 * 3600.0)

    def test_legacy_with_unknown_kickoff_is_unbounded(self):
        deadline = replay._risk_deadline_epoch(
            "legacy", entry_epoch=9000.0, opened_at_epoch=9000.0,
            event_epoch=None, max_started_event_hours=6.0,
        )
        assert deadline is None

    def test_july5_style_with_known_kickoff_caps_at_its_own_started_event_hours(self):
        # july5_style shares legacy's structural branch (no max-holding cap)
        # but has its own configured max_started_event_hours.
        kickoff = 10000.0
        deadline = replay._risk_deadline_epoch(
            "july5_style", entry_epoch=9000.0, opened_at_epoch=9000.0,
            event_epoch=kickoff, max_started_event_hours=8.0,
        )
        assert deadline == pytest.approx(kickoff + 8 * 3600.0)

    def test_july5_style_with_unknown_kickoff_is_unbounded(self):
        deadline = replay._risk_deadline_epoch(
            "july5_style", entry_epoch=9000.0, opened_at_epoch=9000.0,
            event_epoch=None, max_started_event_hours=6.0,
        )
        assert deadline is None

    def test_controlled_pregame_entry_far_from_kickoff_caps_at_one_hour_holding(self):
        kickoff = 10000.0
        entry = kickoff - 7200.0  # 2h before kickoff
        deadline = replay._risk_deadline_epoch(
            "controlled", entry_epoch=entry, opened_at_epoch=entry,
            event_epoch=kickoff, max_started_event_hours=3.0,
            controlled_max_holding_hours=1.0, controlled_entry_cutoff_minutes=30.0,
        )
        assert deadline == pytest.approx(entry + 3600.0)

    def test_controlled_pregame_entry_near_kickoff_caps_at_kickoff_itself(self):
        kickoff = 10000.0
        entry = kickoff - 1800.0  # 30 min before kickoff -- 1h holding cap would exceed kickoff
        deadline = replay._risk_deadline_epoch(
            "controlled", entry_epoch=entry, opened_at_epoch=entry,
            event_epoch=kickoff, max_started_event_hours=3.0,
            controlled_max_holding_hours=1.0, controlled_entry_cutoff_minutes=30.0,
        )
        assert deadline == pytest.approx(kickoff)

    def test_controlled_in_play_entry_shortly_after_kickoff_caps_at_one_hour_holding(self):
        kickoff = 10000.0
        entry = kickoff + 100.0  # near-event deadline (kickoff+2.5h) is still far off
        deadline = replay._risk_deadline_epoch(
            "controlled", entry_epoch=entry, opened_at_epoch=entry,
            event_epoch=kickoff, max_started_event_hours=3.0,
            controlled_max_holding_hours=1.0, controlled_entry_cutoff_minutes=30.0,
        )
        assert deadline == pytest.approx(entry + 3600.0)

    def test_controlled_in_play_entry_late_caps_at_two_point_five_hours_post_kickoff(self):
        kickoff = 10000.0
        entry = kickoff + 6000.0  # 100 min post-kickoff -- near-event (kickoff+2.5h) binds before entry+1h
        deadline = replay._risk_deadline_epoch(
            "controlled", entry_epoch=entry, opened_at_epoch=entry,
            event_epoch=kickoff, max_started_event_hours=3.0,
            controlled_max_holding_hours=1.0, controlled_entry_cutoff_minutes=30.0,
        )
        assert deadline == pytest.approx(kickoff + 2.5 * 3600.0)

    def test_controlled_unknown_kickoff_still_uses_one_hour_holding_cap(self):
        entry = 5000.0
        deadline = replay._risk_deadline_epoch(
            "controlled", entry_epoch=entry, opened_at_epoch=entry,
            event_epoch=None, max_started_event_hours=3.0,
            controlled_max_holding_hours=1.0, controlled_entry_cutoff_minutes=30.0,
        )
        assert deadline == pytest.approx(entry + 3600.0)

    def test_controlled_would_differ_under_the_wrong_no_cap_formula(self):
        # Regression guard for the three-way-branch fix: confirm the
        # controlled formula's 1h holding cap actually binds, i.e. it is
        # NOT simply reusing legacy/july5_style's uncapped formula.
        kickoff = 10000.0
        entry = kickoff + 100.0
        controlled_deadline = replay._risk_deadline_epoch(
            "controlled", entry_epoch=entry, opened_at_epoch=entry,
            event_epoch=kickoff, max_started_event_hours=3.0,
            controlled_max_holding_hours=1.0, controlled_entry_cutoff_minutes=30.0,
        )
        legacy_style_deadline = replay._risk_deadline_epoch(
            "legacy", entry_epoch=entry, opened_at_epoch=entry,
            event_epoch=kickoff, max_started_event_hours=3.0,
        )
        assert controlled_deadline != legacy_style_deadline


class TestFeedCoverage:
    def test_required_feed_minute_keys_matches_production_bucket_format(self):
        keys = replay._required_feed_minute_keys(65.0, 185.0)
        assert keys == ["60", "120", "180"]

    def test_fully_covered_when_every_required_minute_present(self):
        buckets = {key: True for key in replay._required_feed_minute_keys(0.0, 180.0)}
        coverage = replay._feed_coverage(buckets, 0.0, 180.0)
        assert coverage["fully_covered"] is True
        assert coverage["missing_feed_minute_count"] == 0

    def test_detects_a_gap_in_the_middle(self):
        buckets = {key: True for key in replay._required_feed_minute_keys(0.0, 240.0)}
        del buckets["120"]
        coverage = replay._feed_coverage(buckets, 0.0, 240.0)
        assert coverage["fully_covered"] is False
        assert coverage["missing_feed_minute_count"] == 1


class TestReplayExitEscalation:
    def test_only_processes_settlement_and_forced_exit_trips(self):
        ordinary_trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": 1.0,
            "closure_type": None, "forced_exit": False,
        }
        settlement_trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -2.0,
            "closure_type": "settlement", "forced_exit": True,
            "opened_at_epoch": 1000.0,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 10.0, "observed_at_epoch": 1000.0},
        }
        trades = [_trade(price=0.45, quantity=10.0, maker_side="ORDER_SIDE_SELL", epoch=1050.0)]

        rows = replay.replay_exit_escalation(
            [ordinary_trip, settlement_trip], trades,
            **_escalation_kwargs(last_observed_activity_epoch=2000.0),
        )

        assert len(rows) == 1
        assert rows[0]["real_closure_type"] == "settlement"

    def test_finds_a_crossing_and_computes_optimistic_pnl_net_of_commissions(self):
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -2.0,
            "closure_type": "settlement", "forced_exit": True,
            "opened_at_epoch": 1000.0,
            "entry_fill": {
                "side": "BUY", "price": 0.40, "quantity": 10.0,
                "observed_at_epoch": 1000.0, "commission_usd": 0.05,
            },
        }
        trades = [_trade(price=0.45, quantity=10.0, maker_side="ORDER_SIDE_SELL", epoch=1050.0)]

        rows = replay.replay_exit_escalation(
            [trip], trades,
            **_escalation_kwargs(last_observed_activity_epoch=2000.0, maker_fee_theta=-0.0125),
        )

        row = rows[0]
        assert row["status"] == replay.STATUS_CROSS_OBSERVED
        gross = (0.45 - 0.40) * 10.0
        exit_commission = _estimated_commission(0.45, 10.0, -0.0125)
        expected = gross - 0.05 - exit_commission
        assert row["optimistic_full_fill_pnl_usd"] == pytest.approx(expected)
        assert row["optimistic_pnl_delta_usd"] == pytest.approx(expected - (-2.0))
        assert row["taker_exit_feasibility"] == "UNKNOWN"


class TestReplayExitEscalationCoverageClassification:
    def test_no_anchor_at_all_is_unknown(self):
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -1.0,
            "closure_type": "settlement", "forced_exit": True,
            "opened_at_epoch": 1000.0,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 5.0, "observed_at_epoch": 1000.0},
        }
        rows = replay.replay_exit_escalation(
            [trip], [], **_escalation_kwargs(last_observed_activity_epoch=None),
        )
        assert rows[0]["status"] == replay.STATUS_UNKNOWN_NO_ANCHOR

    def test_anchor_before_entry_is_unknown(self):
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -1.0,
            "closure_type": "settlement", "forced_exit": True,
            "opened_at_epoch": 1000.0,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 5.0, "observed_at_epoch": 1000.0},
        }
        rows = replay.replay_exit_escalation(
            [trip], [], **_escalation_kwargs(last_observed_activity_epoch=900.0),
        )
        assert rows[0]["status"] == replay.STATUS_UNKNOWN_NO_ANCHOR

    def test_activity_ending_before_the_risk_deadline_with_no_crossing_is_incomplete(self):
        # controlled, unknown kickoff -> deadline = entry + 1h = 4600
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -1.0,
            "closure_type": "settlement", "forced_exit": True,
            "opened_at_epoch": 1000.0,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 5.0, "observed_at_epoch": 1000.0},
        }
        rows = replay.replay_exit_escalation(
            [trip], [], **_escalation_kwargs(last_observed_activity_epoch=2000.0),
        )
        assert rows[0]["status"] == replay.STATUS_UNKNOWN_INCOMPLETE_WINDOW

    def test_activity_continuing_beyond_deadline_with_full_coverage_and_no_crossing_is_honest_negative(self):
        entry_epoch = 1000.0
        deadline = entry_epoch + 3600.0
        last_observed = deadline + 500.0
        buckets = {key: True for key in replay._required_feed_minute_keys(entry_epoch, deadline)}
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -1.0,
            "closure_type": "settlement", "forced_exit": True,
            "opened_at_epoch": entry_epoch,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 5.0, "observed_at_epoch": entry_epoch},
        }
        rows = replay.replay_exit_escalation(
            [trip], [],
            **_escalation_kwargs(last_observed_activity_epoch=last_observed, feed_minute_buckets=buckets),
        )
        assert rows[0]["status"] == replay.STATUS_CROSS_NOT_OBSERVED

    def test_missing_middle_minute_forces_incomplete_even_though_endpoint_reached(self):
        entry_epoch = 1000.0
        deadline = entry_epoch + 3600.0
        last_observed = deadline + 500.0
        buckets = {key: True for key in replay._required_feed_minute_keys(entry_epoch, deadline)}
        keys = sorted(buckets)
        del buckets[keys[len(keys) // 2]]
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -1.0,
            "closure_type": "settlement", "forced_exit": True,
            "opened_at_epoch": entry_epoch,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 5.0, "observed_at_epoch": entry_epoch},
        }
        rows = replay.replay_exit_escalation(
            [trip], [],
            **_escalation_kwargs(last_observed_activity_epoch=last_observed, feed_minute_buckets=buckets),
        )
        assert rows[0]["status"] == replay.STATUS_UNKNOWN_INCOMPLETE_WINDOW
        assert rows[0]["missing_feed_minute_count"] >= 1

    def test_crossing_found_before_a_gap_is_still_valid(self):
        entry_epoch = 1000.0
        deadline = entry_epoch + 3600.0
        last_observed = deadline + 500.0
        buckets = {key: True for key in replay._required_feed_minute_keys(entry_epoch, deadline)}
        keys = sorted(buckets)
        del buckets[keys[-1]]
        trades = [_trade(price=0.45, quantity=5.0, maker_side="ORDER_SIDE_SELL", epoch=entry_epoch + 30.0)]
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -1.0,
            "closure_type": "settlement", "forced_exit": True,
            "opened_at_epoch": entry_epoch,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 5.0, "observed_at_epoch": entry_epoch},
        }
        rows = replay.replay_exit_escalation(
            [trip], trades,
            **_escalation_kwargs(last_observed_activity_epoch=last_observed, feed_minute_buckets=buckets),
        )
        assert rows[0]["status"] == replay.STATUS_CROSS_OBSERVED

    def test_multi_entry_trip_is_unknown_multifill(self):
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -1.0,
            "closure_type": "settlement", "forced_exit": True,
            "opened_at_epoch": 1000.0, "is_multi_entry": True,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 5.0, "observed_at_epoch": 1000.0},
        }
        rows = replay.replay_exit_escalation(
            [trip], [], **_escalation_kwargs(last_observed_activity_epoch=5000.0),
        )
        assert rows[0]["status"] == replay.STATUS_UNKNOWN_MULTIFILL

    def test_legacy_unknown_kickoff_with_no_crossing_is_always_incomplete(self):
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -1.0,
            "closure_type": "settlement", "forced_exit": True,
            "opened_at_epoch": 1000.0,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 5.0, "observed_at_epoch": 1000.0},
        }
        rows = replay.replay_exit_escalation(
            [trip], [],
            **_escalation_kwargs(profile="legacy", event_epoch=None, last_observed_activity_epoch=50000.0),
        )
        assert rows[0]["status"] == replay.STATUS_UNKNOWN_INCOMPLETE_WINDOW


class TestOptimisticCommissionNetting:
    def test_negative_theta_rebate_increases_optimistic_pnl_above_gross(self):
        entry_epoch = 1000.0
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -1.0,
            "closure_type": "settlement", "forced_exit": True,
            "opened_at_epoch": entry_epoch,
            "entry_fill": {
                "side": "BUY", "price": 0.40, "quantity": 10.0,
                "observed_at_epoch": entry_epoch, "commission_usd": 0.0,
            },
        }
        trades = [_trade(price=0.45, quantity=10.0, maker_side="ORDER_SIDE_SELL", epoch=entry_epoch + 30.0)]

        rows = replay.replay_exit_escalation(
            [trip], trades,
            **_escalation_kwargs(last_observed_activity_epoch=5000.0, maker_fee_theta=-0.0125),
        )

        gross = (0.45 - 0.40) * 10.0
        assert rows[0]["optimistic_full_fill_pnl_usd"] > gross

    def test_positive_theta_fee_can_turn_a_small_gross_profit_negative(self):
        entry_epoch = 1000.0
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -1.0,
            "closure_type": "settlement", "forced_exit": True,
            "opened_at_epoch": entry_epoch,
            "entry_fill": {
                "side": "BUY", "price": 0.40, "quantity": 10.0,
                "observed_at_epoch": entry_epoch, "commission_usd": 0.0,
            },
        }
        trades = [_trade(price=0.405, quantity=10.0, maker_side="ORDER_SIDE_SELL", epoch=entry_epoch + 30.0)]

        rows = replay.replay_exit_escalation(
            [trip], trades,
            **_escalation_kwargs(last_observed_activity_epoch=5000.0, maker_fee_theta=0.5),
        )

        gross = (0.405 - 0.40) * 10.0
        assert gross > 0
        assert rows[0]["optimistic_full_fill_pnl_usd"] < 0


class TestReplayHardPreSettlementExit:
    def test_crossing_found_within_window(self):
        anchor = 10000.0
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -2.0,
            "closure_type": "settlement", "opened_at_epoch": 1000.0,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 10.0, "observed_at_epoch": 1000.0},
        }
        trades = [_trade(price=0.42, quantity=5.0, maker_side="ORDER_SIDE_SELL", epoch=anchor - 100.0)]

        rows = replay.replay_hard_pre_settlement_exit(
            [trip], trades, **_hard_rule_kwargs(last_observed_activity_epoch=anchor),
        )

        assert rows[0]["status"] == replay.STATUS_CROSS_OBSERVED
        assert rows[0]["replayed_exit_epoch"] == anchor - 100.0

    def test_no_anchor_is_unknown(self):
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -2.0,
            "closure_type": "settlement", "opened_at_epoch": 1000.0,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 10.0, "observed_at_epoch": 1000.0},
        }
        rows = replay.replay_hard_pre_settlement_exit([trip], [], **_hard_rule_kwargs())
        assert rows[0]["status"] == replay.STATUS_UNKNOWN_NO_ANCHOR

    def test_anchor_before_entry_is_unknown(self):
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -2.0,
            "closure_type": "settlement", "opened_at_epoch": 1000.0,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 10.0, "observed_at_epoch": 1000.0},
        }
        rows = replay.replay_hard_pre_settlement_exit(
            [trip], [], **_hard_rule_kwargs(last_observed_activity_epoch=900.0),
        )
        assert rows[0]["status"] == replay.STATUS_UNKNOWN_NO_ANCHOR

    def test_no_crossing_with_full_coverage_is_honest_negative(self):
        anchor = 10000.0
        window_start = anchor - replay.HARD_EXIT_WINDOW_SECONDS
        buckets = {key: True for key in replay._required_feed_minute_keys(window_start, anchor)}
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -2.0,
            "closure_type": "settlement", "opened_at_epoch": window_start - 1000.0,
            "entry_fill": {
                "side": "BUY", "price": 0.40, "quantity": 10.0,
                "observed_at_epoch": window_start - 1000.0,
            },
        }
        rows = replay.replay_hard_pre_settlement_exit(
            [trip], [], **_hard_rule_kwargs(last_observed_activity_epoch=anchor, feed_minute_buckets=buckets),
        )
        assert rows[0]["status"] == replay.STATUS_CROSS_NOT_OBSERVED

    def test_missing_minute_in_window_is_incomplete(self):
        anchor = 10000.0
        window_start = anchor - replay.HARD_EXIT_WINDOW_SECONDS
        buckets = {key: True for key in replay._required_feed_minute_keys(window_start, anchor)}
        keys = sorted(buckets)
        del buckets[keys[len(keys) // 2]]
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -2.0,
            "closure_type": "settlement", "opened_at_epoch": window_start - 1000.0,
            "entry_fill": {
                "side": "BUY", "price": 0.40, "quantity": 10.0,
                "observed_at_epoch": window_start - 1000.0,
            },
        }
        rows = replay.replay_hard_pre_settlement_exit(
            [trip], [], **_hard_rule_kwargs(last_observed_activity_epoch=anchor, feed_minute_buckets=buckets),
        )
        assert rows[0]["status"] == replay.STATUS_UNKNOWN_INCOMPLETE_WINDOW

    def test_window_is_clipped_to_entry_so_a_pre_entry_gap_is_irrelevant(self):
        anchor = 10000.0
        entry_epoch = anchor - 600.0  # position opened only 10 minutes before the anchor
        buckets = {key: True for key in replay._required_feed_minute_keys(entry_epoch, anchor)}
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -2.0,
            "closure_type": "settlement", "opened_at_epoch": entry_epoch,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 10.0, "observed_at_epoch": entry_epoch},
        }

        rows = replay.replay_hard_pre_settlement_exit(
            [trip], [], **_hard_rule_kwargs(last_observed_activity_epoch=anchor, feed_minute_buckets=buckets),
        )

        assert rows[0]["status"] == replay.STATUS_CROSS_NOT_OBSERVED
        assert rows[0]["searched_from_epoch"] == pytest.approx(entry_epoch)

    def test_ignores_trips_that_did_not_close_via_settlement(self):
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": 1.0,
            "closure_type": None, "opened_at_epoch": 1000.0,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 10.0, "observed_at_epoch": 1000.0},
        }
        rows = replay.replay_hard_pre_settlement_exit([trip], [], **_hard_rule_kwargs())
        assert rows == []

    def test_multi_entry_trip_is_unknown_multifill(self):
        trip = {
            "market_slug": "m1", "event_id": "e1", "pnl_usd": -1.0,
            "closure_type": "settlement", "opened_at_epoch": 1000.0, "is_multi_entry": True,
            "entry_fill": {"side": "BUY", "price": 0.40, "quantity": 5.0, "observed_at_epoch": 1000.0},
        }
        rows = replay.replay_hard_pre_settlement_exit(
            [trip], [], **_hard_rule_kwargs(last_observed_activity_epoch=5000.0),
        )
        assert rows[0]["status"] == replay.STATUS_UNKNOWN_MULTIFILL


class TestSweepEntryFilters:
    def test_cartesian_grid_produces_full_product(self):
        trip = {
            "pnl_usd": 1.0, "closed_at_epoch": 1500.0,
            "_entry_hours_remaining": 2.0, "same_market_trailing_trade_count": 10,
        }
        rows = replay.sweep_entry_filters([trip], hours_grid=(0.0, 1.0), trade_count_grid=(0, 5))
        assert len(rows) == 4
        combos = {(row["min_hours_remaining"], row["min_same_market_trailing_trade_count"]) for row in rows}
        assert combos == {(0.0, 0), (0.0, 5), (1.0, 0), (1.0, 5)}

    def test_rejects_when_hours_remaining_below_threshold(self):
        trip = {
            "pnl_usd": 1.0, "closed_at_epoch": 1500.0,
            "_entry_hours_remaining": 0.1, "same_market_trailing_trade_count": 10,
        }
        rows = replay.sweep_entry_filters([trip], hours_grid=(1.0,), trade_count_grid=(0,))
        assert rows[0]["entries_rejected"] == 1
        assert rows[0]["entries_kept"] == 0

    def test_rejects_when_trailing_activity_below_threshold(self):
        trip = {
            "pnl_usd": 1.0, "closed_at_epoch": 1500.0,
            "_entry_hours_remaining": 2.0, "same_market_trailing_trade_count": 0,
        }
        rows = replay.sweep_entry_filters([trip], hours_grid=(0.0,), trade_count_grid=(5,))
        assert rows[0]["entries_rejected"] == 1

    def test_trips_without_precomputed_fields_are_never_rejected(self):
        trip = {"pnl_usd": 1.0, "closed_at_epoch": 1500.0}
        rows = replay.sweep_entry_filters([trip], hours_grid=(5.0,), trade_count_grid=(100,))
        assert rows[0]["entries_kept"] == 1


class TestRevisedCohortRows:
    def test_positive_pnl_cohort_can_still_fail_on_profit_factor(self):
        trips = [
            {"cohort_key": "c1", "event_id": f"e{i}", "pnl_usd": pnl, "closed_at_epoch": float(i)}
            for i, pnl in enumerate([5.0, -4.9, 0.1, 0.1, 0.1])
        ]
        rows = replay.revised_cohort_rows(trips)
        row = rows[0]
        assert row["profit_factor"] < replay.COHORT_MIN_PROFIT_FACTOR
        assert row["revised_eligible"] is False

    def test_high_settlement_exit_rate_disqualifies_an_otherwise_healthy_cohort(self):
        trips = [
            {
                "cohort_key": "c1", "event_id": f"e{i}", "pnl_usd": 2.0,
                "closed_at_epoch": float(i), "closure_type": "settlement",
            }
            for i in range(4)
        ] + [
            {"cohort_key": "c1", "event_id": "e4", "pnl_usd": 2.0, "closed_at_epoch": 10.0, "closure_type": None},
        ]
        rows = replay.revised_cohort_rows(trips)
        row = rows[0]
        assert row["settlement_exit_rate"] == pytest.approx(0.8)
        assert row["revised_eligible"] is False

    def test_healthy_cohort_with_enough_trips_and_events_qualifies(self):
        trips = [
            {
                "cohort_key": "c1", "event_id": f"e{i}", "pnl_usd": 1.0,
                "closed_at_epoch": float(i), "closure_type": None,
            }
            for i in range(5)
        ]
        rows = replay.revised_cohort_rows(trips)
        row = rows[0]
        assert row["revised_eligible"] is True

    def test_large_drawdown_disqualifies_a_net_positive_cohort(self):
        trips = [
            {
                "cohort_key": "c1", "event_id": f"e{i}", "pnl_usd": pnl,
                "closed_at_epoch": float(i), "closure_type": None,
            }
            for i, pnl in enumerate([10.0, -9.0, 0.5, 0.5, 0.5])
        ]
        rows = replay.revised_cohort_rows(trips)
        row = rows[0]
        assert row["max_drawdown_usd"] == pytest.approx(-9.0)
        assert row["revised_eligible"] is False

    def test_fewer_than_five_round_trips_disqualifies_even_with_good_stats(self):
        trips = [
            {
                "cohort_key": "c1", "event_id": f"e{i}", "pnl_usd": 5.0,
                "closed_at_epoch": float(i), "closure_type": None,
            }
            for i in range(4)
        ]
        rows = replay.revised_cohort_rows(trips)
        row = rows[0]
        assert row["round_trips"] == 4
        assert row["revised_eligible"] is False

    def test_fewer_than_two_distinct_events_disqualifies_even_with_good_stats(self):
        trips = [
            {"cohort_key": "c1", "event_id": "e0", "pnl_usd": 5.0, "closed_at_epoch": float(i), "closure_type": None}
            for i in range(6)
        ]
        rows = replay.revised_cohort_rows(trips)
        row = rows[0]
        assert row["distinct_event_count"] == 1
        assert row["revised_eligible"] is False


class TestVerdictBlock:
    def test_not_applicable_when_no_escalation_rows(self):
        verdict = replay._verdict_block({"net_pnl_usd": -5.0}, [])
        assert verdict["passive_replay_status"] == "NOT_APPLICABLE"
        assert verdict["baseline_shadow_status"] == "NEGATIVE"
        assert verdict["taker_replay_status"] == "UNKNOWN"
        assert verdict["pilot_unlock_authorized"] is False

    def test_rejected_when_optimistic_total_nonpositive_and_no_unknowns(self):
        rows = [
            {"status": replay.STATUS_CROSS_OBSERVED, "optimistic_pnl_delta_usd": 1.0},
            {"status": replay.STATUS_CROSS_NOT_OBSERVED, "optimistic_pnl_delta_usd": None},
        ]
        verdict = replay._verdict_block({"net_pnl_usd": -5.0}, rows)
        assert verdict["optimistic_passive_total_usd"] == pytest.approx(-4.0)
        assert verdict["passive_replay_status"] == "REJECTED"

    def test_inconclusive_when_optimistic_total_positive(self):
        rows = [{"status": replay.STATUS_CROSS_OBSERVED, "optimistic_pnl_delta_usd": 10.0}]
        verdict = replay._verdict_block({"net_pnl_usd": -5.0}, rows)
        assert verdict["optimistic_passive_total_usd"] == pytest.approx(5.0)
        assert verdict["passive_replay_status"] == "INCONCLUSIVE"

    def test_any_unknown_status_forces_inconclusive_even_with_a_nonpositive_total(self):
        rows = [
            {"status": replay.STATUS_CROSS_NOT_OBSERVED, "optimistic_pnl_delta_usd": None},
            {"status": replay.STATUS_UNKNOWN_MULTIFILL, "optimistic_pnl_delta_usd": None},
        ]
        verdict = replay._verdict_block({"net_pnl_usd": -5.0}, rows)
        assert verdict["passive_replay_status"] == "INCONCLUSIVE"

    def test_positive_baseline_is_labeled(self):
        verdict = replay._verdict_block({"net_pnl_usd": 3.0}, [])
        assert verdict["baseline_shadow_status"] == "POSITIVE"


class TestRoundTripsWithContextEntryMatching:
    def test_attaches_the_first_entry_fill_when_position_built_in_two_pieces(self):
        fills = [
            _fill(side="BUY", price=0.40, quantity=5.0, epoch=1000.0, role="entry"),
            _fill(side="BUY", price=0.42, quantity=5.0, epoch=1010.0, role="entry"),
            _fill(side="SELL", price=0.50, quantity=10.0, epoch=1500.0, role="exit"),
        ]

        trips = replay._round_trips_with_context(fills, slug="m1", event_id="e1")

        assert len(trips) == 1
        assert trips[0]["entry_fill"]["observed_at_epoch"] == 1000.0
        assert trips[0]["entry_fill"]["price"] == 0.40
        assert trips[0]["is_multi_entry"] is True

    def test_multiple_round_trips_each_get_their_own_entry(self):
        fills = [
            _fill(side="BUY", price=0.40, quantity=5.0, epoch=1000.0, role="entry"),
            _fill(side="SELL", price=0.45, quantity=5.0, epoch=1100.0, role="exit"),
            _fill(side="BUY", price=0.30, quantity=5.0, epoch=1200.0, role="entry"),
            _fill(side="SELL", price=0.35, quantity=5.0, epoch=1300.0, role="exit"),
        ]

        trips = replay._round_trips_with_context(fills, slug="m1", event_id="e1")

        assert len(trips) == 2
        assert trips[0]["entry_fill"]["observed_at_epoch"] == 1000.0
        assert trips[1]["entry_fill"]["observed_at_epoch"] == 1200.0
        assert trips[0]["is_multi_entry"] is False
        assert trips[1]["is_multi_entry"] is False

    def test_single_entry_trip_is_not_flagged_multi_entry(self):
        fills = [
            _fill(side="BUY", price=0.40, quantity=5.0, epoch=1000.0, role="entry"),
            _fill(side="SELL", price=0.45, quantity=5.0, epoch=1100.0, role="exit"),
        ]
        trips = replay._round_trips_with_context(fills, slug="m1", event_id="e1")
        assert trips[0]["is_multi_entry"] is False


class TestRunReplayEndToEnd:
    def test_never_mutates_the_archive_file(self, tmp_path):
        path = tmp_path / "archive.json"
        market = {
            "event_id": "e1", "event_or_close_epoch": 10000.0,
            "last_observed_at_epoch": 9950.0,
            "hypothetical_fills": [
                _fill(side="BUY", price=0.40, quantity=5.0, epoch=9800.0, role="entry"),
                _fill(
                    side="SELL", price=0.0, quantity=5.0, epoch=50000.0, role="exit",
                    closure_type="settlement", exit_reason="market_resolved",
                ),
            ],
            "trades": [],
        }
        state = _archive_state(controlled_markets={"m1": market})
        storage.save_json(path, state)
        before = path.read_bytes()

        report = replay.run_replay(path)

        assert path.read_bytes() == before
        assert "controlled" in report["profiles"]
        assert "verdict" in report["profiles"]["controlled"]["improve_both"]

    def test_kickoff_as_deadline_bug_no_longer_suppresses_in_play_crossings(self, tmp_path):
        path = tmp_path / "archive.json"
        kickoff = 10000.0
        entry_epoch = kickoff + 100.0  # in-play entry, after kickoff
        trades = [
            {
                "price": 0.30, "quantity": 5.0, "maker_side": "ORDER_SIDE_SELL",
                "observed_at_epoch": entry_epoch + 20.0,
            },
        ]
        market = {
            "event_id": "e1", "event_or_close_epoch": kickoff,
            "last_observed_at_epoch": entry_epoch + 3000.0,
            "hypothetical_fills": [
                _fill(side="BUY", price=0.25, quantity=5.0, epoch=entry_epoch, role="entry"),
                _fill(
                    side="SELL", price=1.0, quantity=5.0, epoch=entry_epoch + 5000.0, role="exit",
                    closure_type="settlement", exit_reason="market_resolved",
                ),
            ],
            "trades": trades,
        }
        state = _archive_state(controlled_markets={"m1": market})
        storage.save_json(path, state)

        report = replay.run_replay(path)

        rows = report["profiles"]["controlled"]["improve_both"]["escalation_replay"]
        assert len(rows) == 1
        assert rows[0]["status"] == replay.STATUS_CROSS_OBSERVED

    def test_sweep_never_lets_one_markets_activity_leak_into_another(self, tmp_path):
        path = tmp_path / "archive.json"
        entry_epoch = 1000.0
        busy_market = {
            "event_id": "e1", "event_or_close_epoch": None,
            "hypothetical_fills": [
                _fill(side="BUY", price=0.40, quantity=5.0, epoch=entry_epoch, role="entry"),
                _fill(side="SELL", price=0.45, quantity=5.0, epoch=entry_epoch + 100.0, role="exit"),
            ],
            "trades": [
                {
                    "price": 0.40, "quantity": 1.0, "maker_side": "ORDER_SIDE_BUY",
                    "observed_at_epoch": entry_epoch - 60.0 - i,
                }
                for i in range(5)
            ],
        }
        quiet_market = {
            "event_id": "e2", "event_or_close_epoch": None,
            "hypothetical_fills": [
                _fill(side="BUY", price=0.40, quantity=5.0, epoch=entry_epoch, role="entry"),
                _fill(side="SELL", price=0.45, quantity=5.0, epoch=entry_epoch + 100.0, role="exit"),
            ],
            "trades": [],
        }
        state = _archive_state(controlled_markets={"busy": busy_market, "quiet": quiet_market})
        storage.save_json(path, state)

        report = replay.run_replay(path)

        sweep = report["profiles"]["controlled"]["improve_both"]["entry_filter_sweep"]
        row = next(
            r for r in sweep
            if r["min_hours_remaining"] == 0.0 and r["min_same_market_trailing_trade_count"] == 5
        )
        assert row["entries_kept"] == 1

    def test_not_applicable_when_a_strategy_has_no_forced_or_settlement_trips(self, tmp_path):
        path = tmp_path / "archive.json"
        market = {
            "event_id": "e1", "event_or_close_epoch": None,
            "hypothetical_fills": [
                _fill(side="BUY", price=0.40, quantity=5.0, epoch=1000.0, role="entry"),
                _fill(side="SELL", price=0.35, quantity=5.0, epoch=1100.0, role="exit"),
            ],
            "trades": [],
        }
        state = _archive_state(controlled_markets={"m1": market})
        storage.save_json(path, state)

        report = replay.run_replay(path)

        verdict = report["profiles"]["controlled"]["improve_both"]["verdict"]
        assert verdict["passive_replay_status"] == "NOT_APPLICABLE"

    def test_reads_persisted_qualification_config_for_controlled_deadline(self, tmp_path):
        path = tmp_path / "archive.json"
        kickoff = 10000.0
        entry_epoch = kickoff + 10000.0
        crossing_epoch = kickoff + 11000.0
        trades = [
            {"price": 0.30, "quantity": 5.0, "maker_side": "ORDER_SIDE_SELL", "observed_at_epoch": crossing_epoch},
        ]
        market = {
            "event_id": "e1", "event_or_close_epoch": kickoff,
            "last_observed_at_epoch": kickoff + 13000.0,
            "hypothetical_fills": [
                _fill(side="BUY", price=0.25, quantity=5.0, epoch=entry_epoch, role="entry"),
                _fill(
                    side="SELL", price=1.0, quantity=5.0, epoch=entry_epoch + 20000.0, role="exit",
                    closure_type="settlement", exit_reason="market_resolved",
                ),
            ],
            "trades": trades,
        }
        state = _archive_state(
            controlled_markets={"m1": market},
            qualification_config={"controlled_max_started_event_hours": 4.0},
        )
        storage.save_json(path, state)

        report = replay.run_replay(path)

        rows = report["profiles"]["controlled"]["improve_both"]["escalation_replay"]
        assert rows[0]["status"] == replay.STATUS_CROSS_OBSERVED


class TestReportSchemaHonesty:
    def test_rows_and_verdict_never_use_bare_boolean_opportunity_framing(self, tmp_path):
        path = tmp_path / "archive.json"
        market = {
            "event_id": "e1", "event_or_close_epoch": None,
            "last_observed_at_epoch": 5000.0,
            "hypothetical_fills": [
                _fill(side="BUY", price=0.40, quantity=5.0, epoch=1000.0, role="entry"),
                _fill(
                    side="SELL", price=0.0, quantity=5.0, epoch=4000.0, role="exit",
                    closure_type="settlement", exit_reason="market_resolved",
                ),
            ],
            "trades": [],
        }
        state = _archive_state(controlled_markets={"m1": market})
        storage.save_json(path, state)

        report = replay.run_replay(path)

        for strategy, strategy_data in report["profiles"]["controlled"].items():
            if strategy == "_completion":
                continue
            for row in strategy_data["escalation_replay"] + strategy_data["hard_pre_settlement_exit"]:
                assert "replay_found_exit" not in row
                assert "exit_opportunity_existed" not in row
                assert row["taker_exit_feasibility"] == "UNKNOWN"
            verdict = strategy_data["verdict"]
            for key in (
                "baseline_shadow_status", "passive_replay_status",
                "taker_replay_status", "pilot_unlock_authorized",
            ):
                assert key in verdict
            assert verdict["pilot_unlock_authorized"] is False


class TestSpecField:
    def test_reads_a_present_spec_field(self):
        state = {"profiles": {"july5_style": {"spec": {"max_spread": 0.98}}}}
        assert replay._spec_field(state, "july5_style", "max_spread", 0.5) == 0.98

    def test_falls_back_when_spec_absent(self):
        state = {"profiles": {"july5_style": {}}}
        assert replay._spec_field(state, "july5_style", "max_spread", 0.5) == 0.5

    def test_falls_back_when_field_absent_from_spec(self):
        state = {"profiles": {"july5_style": {"spec": {}}}}
        assert replay._spec_field(state, "july5_style", "max_spread", 0.5) == 0.5

    def test_falls_back_when_profile_entirely_absent(self):
        state = {"profiles": {}}
        assert replay._spec_field(state, "july5_style", "max_spread", 0.5) == 0.5


class TestRevisedCohortRowsNormalization:
    def test_raw_drawdown_over_threshold_but_normalized_drawdown_within_it_is_eligible(self):
        # 17.5-share cohort: raw drawdown -$5.25 fails the raw $3 bar, but
        # -$5.25 / 17.5 = -$0.30 comfortably clears it.
        trips = [
            {"cohort_key": "c1", "event_id": f"e{i}", "pnl_usd": pnl, "closed_at_epoch": float(i)}
            for i, pnl in enumerate([1.0, 1.0, 1.0, 1.0, 1.0])
        ]
        # Inject a drawdown-producing sequence within the same cohort.
        trips = [
            {"cohort_key": "c1", "event_id": "e0", "pnl_usd": 8.75, "closed_at_epoch": 0.0},
            {"cohort_key": "c1", "event_id": "e1", "pnl_usd": -5.25, "closed_at_epoch": 1.0},
            {"cohort_key": "c1", "event_id": "e2", "pnl_usd": 1.0, "closed_at_epoch": 2.0},
            {"cohort_key": "c1", "event_id": "e3", "pnl_usd": 1.0, "closed_at_epoch": 3.0},
            {"cohort_key": "c1", "event_id": "e4", "pnl_usd": 1.0, "closed_at_epoch": 4.0},
        ]
        rows = replay.revised_cohort_rows(trips, order_shares_max=17.5)
        row = rows[0]
        assert row["max_drawdown_usd"] == pytest.approx(-5.25)
        assert row["max_drawdown_one_share_equivalent_usd"] == pytest.approx(-0.30)
        assert row["revised_eligible"] is True

    def test_controlled_profile_is_unaffected_by_normalization_no_op(self):
        trips = [
            {"cohort_key": "c1", "event_id": "e0", "pnl_usd": 5.0, "closed_at_epoch": 0.0},
            {"cohort_key": "c1", "event_id": "e1", "pnl_usd": -4.9, "closed_at_epoch": 1.0},
            {"cohort_key": "c1", "event_id": "e2", "pnl_usd": 0.1, "closed_at_epoch": 2.0},
            {"cohort_key": "c1", "event_id": "e3", "pnl_usd": 0.1, "closed_at_epoch": 3.0},
            {"cohort_key": "c1", "event_id": "e4", "pnl_usd": 0.1, "closed_at_epoch": 4.0},
        ]
        default_rows = replay.revised_cohort_rows(trips)
        explicit_one_share_rows = replay.revised_cohort_rows(trips, order_shares_max=1.0)
        assert default_rows[0]["revised_eligible"] == explicit_one_share_rows[0]["revised_eligible"]
        assert default_rows[0]["max_drawdown_one_share_equivalent_usd"] == pytest.approx(
            default_rows[0]["max_drawdown_usd"]
        )

    def test_policy_thresholds_can_be_overridden(self):
        trips = [
            {"cohort_key": "c1", "event_id": f"e{i}", "pnl_usd": 1.0, "closed_at_epoch": float(i)}
            for i in range(5)
        ]
        # profit_factor is inf (no losses) so raising cohort_min_profit_factor
        # shouldn't disqualify it -- but raising cohort_min_round_trips
        # above 5 should.
        rows = replay.revised_cohort_rows(trips, cohort_min_round_trips=6)
        assert rows[0]["revised_eligible"] is False


class TestProfileCompletionSummary:
    def test_reads_healthy_feed_hours_directly_from_archive(self):
        state = _archive_state(
            feed_minute_buckets={str(i * 60): True for i in range(120)},
            evaluation_healthy_feed_target_seconds=48 * 3600.0,
        )
        completion = replay._profile_completion_summary(state, "controlled")
        assert completion["healthy_feed_hours"] == pytest.approx(2.0)
        assert completion["healthy_feed_target_hours"] == pytest.approx(48.0)
        assert completion["remaining_healthy_feed_hours"] == pytest.approx(46.0)

    def test_open_inventory_and_shadow_positions_counted_independently(self):
        market = {
            "hypothetical_fills": [
                _fill(side="BUY", price=0.40, quantity=5.0, epoch=1000.0, role="entry", strategy="improve_both"),
                _fill(side="BUY", price=0.40, quantity=5.0, epoch=1000.0, role="entry", strategy="join_both"),
            ],
        }
        state = _archive_state(controlled_markets={"m1": market})
        completion = replay._profile_completion_summary(state, "controlled")
        assert completion["open_inventory_count"] == 1  # primary strategy (improve_both) only
        assert completion["open_shadow_strategy_positions"] == 2  # improve_both + join_both

    def test_no_open_inventory_when_flat(self):
        market = {
            "hypothetical_fills": [
                _fill(side="BUY", price=0.40, quantity=5.0, epoch=1000.0, role="entry"),
                _fill(side="SELL", price=0.45, quantity=5.0, epoch=1100.0, role="exit"),
            ],
        }
        state = _archive_state(controlled_markets={"m1": market})
        completion = replay._profile_completion_summary(state, "controlled")
        assert completion["open_inventory_count"] == 0
        assert completion["open_shadow_strategy_positions"] == 0

    def test_evaluation_finalization_complete_flag(self):
        state = _archive_state(evaluation_finalization={"complete": True})
        assert replay._profile_completion_summary(state, "controlled")[
            "evaluation_finalization_complete"
        ] is True
        state2 = _archive_state()
        assert replay._profile_completion_summary(state2, "controlled")[
            "evaluation_finalization_complete"
        ] is False


def _base_policy(**overrides):
    policy = dict(replay._FALLBACK_QUALIFICATION_POLICY)
    policy.update(overrides)
    return policy


def _base_completion(**overrides):
    completion = dict(
        healthy_feed_hours=48.0,
        healthy_feed_target_hours=48.0,
        remaining_healthy_feed_hours=0.0,
        open_inventory_count=0,
        open_shadow_strategy_positions=0,
        avg_markout_5m_cents=1.0,
        evaluation_finalization_complete=True,
    )
    completion.update(overrides)
    return completion


def _qualifying_trips(count=20, events=5, pnl=1.0):
    return [
        {
            "cohort_key": "c1", "event_id": f"e{i % events}",
            "pnl_usd": pnl, "closed_at_epoch": float(i),
        }
        for i in range(count)
    ]


class TestProfileFollowUpStatus:
    def _call(self, trips, **overrides):
        baseline = replay._portfolio_stats(trips)
        cohorts = overrides.pop(
            "cohorts", replay.revised_cohort_rows(trips, order_shares_max=1.0),
        )
        verdict = overrides.pop("verdict", {"passive_replay_status": "INCONCLUSIVE"})
        completion = overrides.pop("completion", _base_completion())
        policy = overrides.pop("policy", _base_policy())
        order_shares_max = overrides.pop("order_shares_max", 1.0)
        return replay.profile_follow_up_status(
            trips=trips, baseline=baseline, cohorts=cohorts, verdict=verdict,
            completion=completion, order_shares_max=order_shares_max, policy=policy,
        )

    def test_meeting_every_condition_returns_follow_up_candidate(self):
        trips = _qualifying_trips()
        result = self._call(trips)
        assert result["status"] == replay.FOLLOW_UP_CANDIDATE
        assert result["blocked_reasons"] == []

    def test_incomplete_finalization_blocks(self):
        trips = _qualifying_trips()
        result = self._call(trips, completion=_base_completion(evaluation_finalization_complete=False))
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("finalized" in reason for reason in result["blocked_reasons"])

    def test_insufficient_feed_coverage_blocks(self):
        trips = _qualifying_trips()
        result = self._call(
            trips,
            completion=_base_completion(healthy_feed_hours=40.0, remaining_healthy_feed_hours=8.0),
        )
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("feed coverage" in reason for reason in result["blocked_reasons"])

    def test_too_few_round_trips_blocks(self):
        trips = _qualifying_trips(count=10)
        result = self._call(trips)
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("round trips" in reason for reason in result["blocked_reasons"])

    def test_too_few_distinct_events_blocks(self):
        trips = _qualifying_trips(count=20, events=2)
        result = self._call(trips)
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("distinct events" in reason for reason in result["blocked_reasons"])

    def test_nonpositive_one_share_equivalent_pnl_blocks(self):
        trips = _qualifying_trips(pnl=-1.0)
        result = self._call(trips)
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("net P&L" in reason for reason in result["blocked_reasons"])

    def test_normalized_drawdown_exceeding_the_policy_bar_blocks(self):
        trips = _qualifying_trips()
        result = self._call(trips, order_shares_max=0.01)  # inflates normalized drawdown
        # net pnl per trip is +1.0, so there's no drawdown at all here --
        # construct a trip sequence with a real drawdown instead.
        drawdown_trips = [
            {"cohort_key": "c1", "event_id": f"e{i%5}", "pnl_usd": 10.0, "closed_at_epoch": float(i)}
            for i in range(19)
        ] + [{"cohort_key": "c1", "event_id": "e0", "pnl_usd": -50.0, "closed_at_epoch": 19.0}]
        result = self._call(drawdown_trips)
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("drawdown" in reason for reason in result["blocked_reasons"])

    def test_no_qualifying_cohort_blocks(self):
        trips = _qualifying_trips()
        result = self._call(trips, cohorts=[{"revised_eligible": False}])
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("cohort" in reason for reason in result["blocked_reasons"])

    def test_negative_markout_blocks(self):
        trips = _qualifying_trips()
        result = self._call(trips, completion=_base_completion(avg_markout_5m_cents=-0.5))
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("markout" in reason for reason in result["blocked_reasons"])

    def test_missing_markout_sample_blocks(self):
        trips = _qualifying_trips()
        result = self._call(trips, completion=_base_completion(avg_markout_5m_cents=None))
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("markout" in reason for reason in result["blocked_reasons"])

    def test_open_primary_inventory_blocks_independently_of_shadow_positions(self):
        trips = _qualifying_trips()
        result = self._call(
            trips,
            completion=_base_completion(open_inventory_count=1, open_shadow_strategy_positions=0),
        )
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("primary portfolio" in reason for reason in result["blocked_reasons"])

    def test_open_shadow_variant_inventory_blocks_independently_of_primary(self):
        trips = _qualifying_trips()
        result = self._call(
            trips,
            completion=_base_completion(open_inventory_count=0, open_shadow_strategy_positions=1),
        )
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("shadow strategy variant" in reason for reason in result["blocked_reasons"])

    def test_high_settlement_exit_rate_blocks(self):
        trips = [
            {
                "cohort_key": "c1", "event_id": f"e{i % 5}", "pnl_usd": 1.0,
                "closed_at_epoch": float(i), "closure_type": "settlement",
            }
            for i in range(20)
        ]
        result = self._call(trips)
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("settlement-exit rate" in reason for reason in result["blocked_reasons"])

    def test_rejected_passive_replay_status_blocks(self):
        trips = _qualifying_trips()
        result = self._call(trips, verdict={"passive_replay_status": "REJECTED"})
        assert result["status"] == replay.NOT_YET_QUALIFIED
        assert any("REJECTED" in reason for reason in result["blocked_reasons"])

    def test_result_reports_the_policys_primary_strategy(self):
        trips = _qualifying_trips()
        result = self._call(trips, policy=_base_policy(primary_strategy="improve_both"))
        assert result["primary_strategy"] == "improve_both"


class TestRunReplaySpecAndPolicyDriven:
    def test_july5_style_uses_its_own_persisted_spread_and_size_from_spec(self, tmp_path):
        path = tmp_path / "archive.json"
        market = {
            "event_id": "e1", "event_or_close_epoch": None,
            "hypothetical_fills": [
                _fill(side="BUY", price=0.40, quantity=17.5, epoch=1000.0, role="entry"),
                _fill(side="SELL", price=0.45, quantity=17.5, epoch=1100.0, role="exit"),
            ],
            "trades": [],
        }
        state = _archive_state(
            july5_markets={"m1": market},
            july5_spec={"order_shares_max": 17.5, "max_started_event_hours": 6.0},
        )
        storage.save_json(path, state)

        report = replay.run_replay(path)

        baseline = report["profiles"]["july5_style"]["improve_both"]["baseline"]
        assert baseline["round_trips"] == 1
        expected_one_share = baseline["net_pnl_usd"] / 17.5
        assert baseline["net_pnl_one_share_equivalent_usd"] == pytest.approx(expected_one_share)

    def test_follow_up_only_appears_for_the_policys_primary_strategy(self, tmp_path):
        path = tmp_path / "archive.json"
        market = {
            "event_id": "e1", "event_or_close_epoch": None,
            "hypothetical_fills": [
                _fill(
                    side="BUY", price=0.40, quantity=1.0, epoch=1000.0, role="entry",
                    strategy="join_both",
                ),
                _fill(
                    side="SELL", price=0.45, quantity=1.0, epoch=1100.0, role="exit",
                    strategy="join_both",
                ),
            ],
            "trades": [],
        }
        state = _archive_state(
            controlled_markets={"m1": market},
            qualification_policy=replay._FALLBACK_QUALIFICATION_POLICY,
        )
        storage.save_json(path, state)

        report = replay.run_replay(path)

        controlled = report["profiles"]["controlled"]
        assert "follow_up" not in controlled["join_both"]
        assert "follow_up" in controlled["improve_both"]

    def test_completion_block_present_per_profile(self, tmp_path):
        path = tmp_path / "archive.json"
        state = _archive_state(feed_minute_buckets={"60": True})
        storage.save_json(path, state)

        report = replay.run_replay(path)

        for profile in ("legacy", "controlled", "july5_style"):
            assert "_completion" in report["profiles"][profile]
            assert "healthy_feed_hours" in report["profiles"][profile]["_completion"]


class TestReplayIsArchiveDriven:
    def test_module_never_imports_market_observation_tracker_class(self):
        # observation_replay.py must never construct the live, stateful
        # tracker -- only ever read an already-loaded archive dict. This is
        # a static guard against that ever creeping back in.
        assert not hasattr(replay, "MarketObservationTracker")

    def test_run_replay_only_ever_calls_storage_load_json(self, tmp_path, monkeypatch):
        path = tmp_path / "archive.json"
        storage.save_json(path, _archive_state())

        calls = []
        original_load_json = storage.load_json

        def _tracking_load_json(*args, **kwargs):
            calls.append(args)
            return original_load_json(*args, **kwargs)

        monkeypatch.setattr(replay.storage, "load_json", _tracking_load_json)

        replay.run_replay(path)

        assert len(calls) == 1
