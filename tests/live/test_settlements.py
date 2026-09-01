import pytest

from polymarket_bot.live import settlements as settlements_module
from polymarket_bot.live.lot_accounting import OpenLot
from polymarket_bot.live.settlements import (
    build_position_snapshot_rows,
    detect_settlements,
    get_all_position_snapshots,
    infer_settlement_payout_per_share,
    record_position_snapshots,
)


@pytest.fixture
def isolated_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(settlements_module, "POSITION_SNAPSHOTS_FILE", tmp_path / "position_snapshots.json")
    monkeypatch.setattr(settlements_module, "SETTLEMENTS_FILE", tmp_path / "settlements.json")


def _position(net_position=10.0, cost=4.0, cash_value=4.5, realized=0.0):
    return {
        "netPositionDecimal": str(net_position),
        "cost": {"value": str(cost), "currency": "USD"},
        "cashValue": {"value": str(cash_value), "currency": "USD"},
        "realized": {"value": str(realized), "currency": "USD"},
    }


def _open_lot(market_slug="m1", outcome="YES", open_price=0.40, shares_open=10.0):
    return OpenLot(
        market_slug=market_slug, outcome=outcome, event_bucket_key=None,
        open_fill_id="f1", open_order_id="o1", open_side="BUY",
        open_price=open_price, open_time="2026-07-06T14:00:00+00:00",
        shares_open=shares_open, open_commission_usd=-0.05,
    )


class TestBuildPositionSnapshotRows:
    def test_emits_row_when_no_previous_entry(self):
        positions = {"m1": _position()}
        rows = build_position_snapshot_rows(positions, "2026-07-06T14:00:00+00:00", previous_by_slug={})
        assert len(rows) == 1
        assert rows[0]["market_slug"] == "m1"
        assert rows[0]["net_position_shares"] == 10.0
        assert rows[0]["cost_usd"] == 4.0
        assert rows[0]["cash_value_usd"] == 4.5
        assert rows[0]["realized_usd"] == 0.0

    def test_skips_row_when_nothing_changed(self):
        positions = {"m1": _position()}
        previous = {"m1": {
            "net_position_shares": 10.0, "cost_usd": 4.0, "cash_value_usd": 4.5, "realized_usd": 0.0,
        }}
        rows = build_position_snapshot_rows(positions, "2026-07-06T14:01:00+00:00", previous_by_slug=previous)
        assert rows == []

    def test_emits_row_when_realized_changed(self):
        positions = {"m1": _position(realized=1.0)}
        previous = {"m1": {
            "net_position_shares": 10.0, "cost_usd": 4.0, "cash_value_usd": 4.5, "realized_usd": 0.0,
        }}
        rows = build_position_snapshot_rows(positions, "2026-07-06T14:01:00+00:00", previous_by_slug=previous)
        assert len(rows) == 1
        assert rows[0]["realized_usd"] == 1.0

    def test_non_dict_position_skipped(self):
        rows = build_position_snapshot_rows({"weird": "not-a-dict"}, "t", previous_by_slug={})
        assert rows == []


class TestRecordPositionSnapshots:
    def test_records_and_reads_back(self, isolated_snapshots):
        record_position_snapshots({"m1": _position()})
        snapshots = get_all_position_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0]["market_slug"] == "m1"

    def test_second_call_with_unchanged_position_writes_nothing_new(self, isolated_snapshots):
        record_position_snapshots({"m1": _position()})
        record_position_snapshots({"m1": _position()})
        assert len(get_all_position_snapshots()) == 1

    def test_second_call_with_changed_position_appends(self, isolated_snapshots):
        record_position_snapshots({"m1": _position(realized=0.0)})
        record_position_snapshots({"m1": _position(realized=1.5)})
        snapshots = get_all_position_snapshots()
        assert len(snapshots) == 2
        assert snapshots[-1]["realized_usd"] == 1.5

    def test_never_raises_on_write_failure(self, isolated_snapshots, monkeypatch):
        monkeypatch.setattr(
            settlements_module.storage, "append_json_list",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("disk full")),
        )
        result = record_position_snapshots({"m1": _position()})  # must not raise
        assert result == []


class TestGetEarliestSnapshotBySlug:
    def test_returns_first_recorded_row_per_slug_not_last(self, isolated_snapshots):
        record_position_snapshots({"m1": _position(realized=0.0)})
        record_position_snapshots({"m1": _position(realized=1.5)})

        earliest = settlements_module.get_earliest_snapshot_by_slug()

        assert earliest["m1"]["realized_usd"] == 0.0

    def test_empty_history_returns_empty_dict(self, isolated_snapshots):
        assert settlements_module.get_earliest_snapshot_by_slug() == {}

    def test_tracks_each_slug_independently(self, isolated_snapshots):
        record_position_snapshots({"m1": _position(realized=0.0), "m2": _position(realized=9.0)})
        record_position_snapshots({"m1": _position(realized=1.0)})

        earliest = settlements_module.get_earliest_snapshot_by_slug()

        assert earliest["m1"]["realized_usd"] == 0.0
        assert earliest["m2"]["realized_usd"] == 9.0


class TestInferSettlementPayoutPerShare:
    def test_observed_tier_backsolves_payout_at_one(self):
        # 10 shares opened @ 0.40 (cost_open=4.0). realized moved from 0
        # to 6.0 -- payout = (6.0 + 4.0) / 10 = 1.0 (a full win).
        last_snapshot = {"realized_usd": 6.0, "net_position_shares": 0.0, "cash_value_usd": 0.0}
        payout, confidence = infer_settlement_payout_per_share(
            last_snapshot, baseline_realized_usd=0.0, shares_open=10.0, cost_open_usd=4.0,
        )
        assert payout == pytest.approx(1.0)
        assert confidence == "observed"

    def test_observed_tier_backsolves_payout_at_zero(self):
        # Full loss: realized moved from 0 to -4.0 -- payout = (-4.0+4.0)/10 = 0.0.
        last_snapshot = {"realized_usd": -4.0, "net_position_shares": 0.0, "cash_value_usd": 0.0}
        payout, confidence = infer_settlement_payout_per_share(
            last_snapshot, baseline_realized_usd=0.0, shares_open=10.0, cost_open_usd=4.0,
        )
        assert payout == pytest.approx(0.0)
        assert confidence == "observed"

    def test_observed_tier_backsolves_short_winner_payout_at_zero(self):
        snapshot = {
            "realized_usd": 2.0,
            "net_position_shares": -10.0,
            "cash_value_usd": 8.0,
        }
        payout, confidence = infer_settlement_payout_per_share(
            snapshot, baseline_realized_usd=0.0, shares_open=10.0, cost_open_usd=2.0,
        )
        assert payout == pytest.approx(0.0)
        assert confidence == "observed"

    def test_observed_tier_backsolves_short_loser_payout_at_one(self):
        snapshot = {
            "realized_usd": -8.0,
            "net_position_shares": -10.0,
            "cash_value_usd": 0.0,
        }
        payout, confidence = infer_settlement_payout_per_share(
            snapshot, baseline_realized_usd=0.0, shares_open=10.0, cost_open_usd=2.0,
        )
        assert payout == pytest.approx(1.0)
        assert confidence == "observed"

    def test_observed_tier_out_of_bounds_falls_back_to_inferred_low(self):
        # realized moved by an implausibly large amount -- payout would be
        # way outside [0,1]; must not be trusted as a real number.
        last_snapshot = {"realized_usd": 1000.0, "net_position_shares": 0.0, "cash_value_usd": 0.0}
        payout, confidence = infer_settlement_payout_per_share(
            last_snapshot, baseline_realized_usd=0.0, shares_open=10.0, cost_open_usd=4.0,
        )
        assert payout is None
        assert confidence == "inferred_low"

    def test_inferred_high_tier_when_mark_price_near_one(self):
        last_snapshot = {"realized_usd": 0.0, "net_position_shares": 10.0, "cash_value_usd": 9.5}
        payout, confidence = infer_settlement_payout_per_share(
            last_snapshot, baseline_realized_usd=0.0, shares_open=10.0, cost_open_usd=4.0,
        )
        assert payout == pytest.approx(1.0)
        assert confidence == "inferred_high"

    def test_inferred_high_tier_when_mark_price_near_zero(self):
        last_snapshot = {"realized_usd": 0.0, "net_position_shares": 10.0, "cash_value_usd": 0.5}
        payout, confidence = infer_settlement_payout_per_share(
            last_snapshot, baseline_realized_usd=0.0, shares_open=10.0, cost_open_usd=4.0,
        )
        assert payout == pytest.approx(0.0)
        assert confidence == "inferred_high"

    def test_inferred_low_when_no_signal_at_all(self):
        last_snapshot = {"realized_usd": 0.0, "net_position_shares": 10.0, "cash_value_usd": 5.0}  # mid-range
        payout, confidence = infer_settlement_payout_per_share(
            last_snapshot, baseline_realized_usd=0.0, shares_open=10.0, cost_open_usd=4.0,
        )
        assert payout is None
        assert confidence == "inferred_low"

    def test_inferred_low_for_short_position_even_near_boundary(self):
        # Short position -- cashValue sign convention is ambiguous, so this
        # must never guess even when the ratio looks extreme.
        last_snapshot = {"realized_usd": 0.0, "net_position_shares": -10.0, "cash_value_usd": -9.5}
        payout, confidence = infer_settlement_payout_per_share(
            last_snapshot, baseline_realized_usd=0.0, shares_open=10.0, cost_open_usd=4.0,
        )
        assert payout is None
        assert confidence == "inferred_low"

    def test_zero_shares_open_is_inferred_low(self):
        payout, confidence = infer_settlement_payout_per_share(
            {"realized_usd": 5.0}, baseline_realized_usd=0.0, shares_open=0.0, cost_open_usd=0.0,
        )
        assert payout is None
        assert confidence == "inferred_low"


class TestDetectSettlements:
    def test_no_settlement_when_strategy_a_already_explains_disappearance(self):
        # No open lots at all for this slug (Strategy A already closed it
        # via a real fill) -- must never be flagged.
        settlements = detect_settlements(
            all_snapshots=[{"market_slug": "m1", "timestamp": "t1", "realized_usd": 0.0}],
            current_positions={},
            open_lots=[],
        )
        assert settlements == []

    def test_no_settlement_when_still_open_per_exchange(self):
        settlements = detect_settlements(
            all_snapshots=[{"market_slug": "m1", "timestamp": "t1", "realized_usd": 0.0}],
            current_positions={"m1": _position(net_position=5.0)},  # still nonzero
            open_lots=[_open_lot()],
        )
        assert settlements == []

    def test_no_settlement_when_no_snapshot_history(self):
        settlements = detect_settlements(
            all_snapshots=[],  # nothing to back-solve from
            current_positions={},
            open_lots=[_open_lot()],
        )
        assert settlements == []

    def test_genuine_settlement_detected_and_shaped_correctly(self):
        snapshots = [
            {"market_slug": "m1", "timestamp": "2026-07-06T14:00:00+00:00", "realized_usd": 0.0,
             "net_position_shares": 10.0, "cash_value_usd": 4.0},
            {"market_slug": "m1", "timestamp": "2026-07-06T15:00:00+00:00", "realized_usd": 6.0,
             "net_position_shares": 0.0, "cash_value_usd": 0.0},
        ]
        settlements = detect_settlements(
            all_snapshots=snapshots,
            current_positions={},  # m1 is gone -- vanished
            open_lots=[_open_lot(open_price=0.40, shares_open=10.0)],
            now="2026-07-06T16:00:00+00:00",
        )
        assert len(settlements) == 1
        s = settlements[0]
        assert s["market_slug"] == "m1"
        assert s["outcome"] == "YES"
        assert s["confidence"] == "observed"
        assert s["inferred_settlement_value_per_share"] == pytest.approx(1.0)
        assert s["detected_at"] == "2026-07-06T16:00:00+00:00"
        assert s["last_seen_at"] == "2026-07-06T15:00:00+00:00"
        assert s["gap_seconds"] == pytest.approx(3600.0)

    def test_already_detected_key_skipped(self):
        snapshots = [
            {"market_slug": "m1", "timestamp": "t1", "realized_usd": 0.0},
            {"market_slug": "m1", "timestamp": "t2", "realized_usd": 6.0},
        ]
        settlements = detect_settlements(
            all_snapshots=snapshots,
            current_positions={},
            open_lots=[_open_lot()],
            already_detected_keys={("m1", "YES")},
        )
        assert settlements == []

    def test_multiple_outcomes_on_same_slug_each_get_own_settlement_row(self):
        snapshots = [
            {"market_slug": "m1", "timestamp": "t1", "realized_usd": 0.0},
            {"market_slug": "m1", "timestamp": "t2", "realized_usd": 6.0},
        ]
        settlements = detect_settlements(
            all_snapshots=snapshots,
            current_positions={},
            open_lots=[_open_lot(outcome="YES"), _open_lot(outcome="NO")],
        )
        assert {s["outcome"] for s in settlements} == {"YES", "NO"}
        assert len(settlements) == 2
        # get_all_positions() is keyed by market_slug only -- one shared
        # realized_usd signal can't be attributed to either outcome
        # individually while both still have open lots, so neither gets a
        # fabricated payout (see the cross-contamination test below for why
        # blindly back-solving both from the same shared delta is wrong).
        assert {s["confidence"] for s in settlements} == {"inferred_low"}
        assert all(s["inferred_settlement_value_per_share"] is None for s in settlements)

    def test_multi_outcome_settlement_does_not_cross_contaminate_payout_between_outcomes(self):
        # Real scenario: fills confirm a two-sided market maker can end up
        # holding BOTH YES and NO inventory on the same slug, and both can
        # resolve inside the same detection window. get_all_positions()
        # only exposes one combined realized/cashValue snapshot per slug
        # (us_client.py) -- there's no way to tell how much of a shared
        # realized delta belongs to YES vs NO. Before the fix,
        # infer_settlement_payout_per_share() was called independently per
        # outcome using EACH outcome's own shares_open/cost_open_usd against
        # the SAME shared realized_delta: 10 YES shares @ 0.40 (cost 4.0)
        # and 5 NO shares @ 0.30 (cost 1.5) against one shared delta of 6.0
        # back-solved to two DIFFERENT "observed"-confidence numbers
        # (YES=(6.0+4.0)/10=1.0, NO=(6.0+1.5)/5=1.5 clamped to 1.0) despite
        # the shared signal being unable to distinguish which one actually
        # won. Both must now come back inferred_low instead.
        snapshots = [
            {"market_slug": "m1", "timestamp": "t1", "realized_usd": 0.0},
            {"market_slug": "m1", "timestamp": "t2", "realized_usd": 6.0},
        ]
        settlements = detect_settlements(
            all_snapshots=snapshots,
            current_positions={},
            open_lots=[
                _open_lot(outcome="YES", open_price=0.40, shares_open=10.0),
                _open_lot(outcome="NO", open_price=0.30, shares_open=5.0),
            ],
        )
        assert len(settlements) == 2
        for s in settlements:
            assert s["confidence"] == "inferred_low"
            assert s["inferred_settlement_value_per_share"] is None

    def test_single_outcome_settlement_still_backsolves_normally(self):
        # Sanity check: the multi-outcome guard must not affect the
        # overwhelmingly common single-outcome case.
        snapshots = [
            {"market_slug": "m1", "timestamp": "t1", "realized_usd": 0.0},
            {"market_slug": "m1", "timestamp": "t2", "realized_usd": 6.0,
             "net_position_shares": 0.0, "cash_value_usd": 0.0},
        ]
        settlements = detect_settlements(
            all_snapshots=snapshots,
            current_positions={},
            open_lots=[_open_lot(outcome="YES", open_price=0.40, shares_open=10.0)],
        )
        assert len(settlements) == 1
        assert settlements[0]["confidence"] == "observed"
        assert settlements[0]["inferred_settlement_value_per_share"] == pytest.approx(1.0)
