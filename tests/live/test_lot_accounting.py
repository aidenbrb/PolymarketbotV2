import pytest

from polymarket_bot.live.lot_accounting import (
    compute_lots,
    position_key,
)


def _fill(
    fill_id, side, price, shares, market_slug="m1", outcome="YES",
    transact_time="2026-07-06T14:00:00.000000Z", order_id=None,
    commission_usd=None, intent=None,
):
    order = {"marketSlug": market_slug}
    if intent is not None:
        order["intent"] = intent
    return {
        "fill_id": fill_id,
        "order_id": order_id or f"order-{fill_id}",
        "market_slug": market_slug,
        "side": side,
        "price": price,
        "shares": shares,
        "outcome": outcome,
        "commission_usd": commission_usd,
        "execution_type": "EXECUTION_TYPE_FILL",
        "transact_time": transact_time,
        "raw_execution": {"order": order},
    }


class TestPositionKey:
    def test_keys_on_market_slug_and_outcome(self):
        assert position_key(_fill("f1", "BUY", 0.4, 10, outcome="YES")) == ("m1", "YES")
        assert position_key(_fill("f1", "BUY", 0.4, 10, outcome="NO")) == ("m1", "NO")


class TestFifoMatching:
    def test_same_side_fills_accumulate_into_separate_open_slices(self):
        fills = [
            _fill("f1", "BUY", 0.40, 10.0, transact_time="2026-07-06T14:00:00Z"),
            _fill("f2", "BUY", 0.42, 5.0, transact_time="2026-07-06T14:01:00Z"),
        ]
        result = compute_lots(fills)
        assert result.closed_lots == []
        assert len(result.open_lots) == 2
        assert {lot.open_fill_id for lot in result.open_lots} == {"f1", "f2"}

    def test_opposite_side_fill_closes_oldest_first(self):
        fills = [
            _fill("f1", "BUY", 0.40, 10.0, transact_time="2026-07-06T14:00:00Z"),
            _fill("f2", "BUY", 0.45, 10.0, transact_time="2026-07-06T14:01:00Z"),
            _fill("f3", "SELL", 0.50, 15.0, transact_time="2026-07-06T14:02:00Z"),
        ]
        result = compute_lots(fills)

        assert len(result.closed_lots) == 2
        first, second = result.closed_lots
        assert first.open_fill_id == "f1"  # oldest closed first
        assert first.shares_matched == pytest.approx(10.0)  # fully closed
        assert second.open_fill_id == "f2"
        assert second.shares_matched == pytest.approx(5.0)  # partially closed

        assert len(result.open_lots) == 1
        assert result.open_lots[0].open_fill_id == "f2"
        assert result.open_lots[0].shares_open == pytest.approx(5.0)  # remainder

    def test_flip_through_flat_opens_new_opposite_side_slice(self):
        fills = [
            _fill("f1", "BUY", 0.40, 10.0, transact_time="2026-07-06T14:00:00Z"),
            _fill("f2", "SELL", 0.50, 15.0, transact_time="2026-07-06T14:01:00Z"),
        ]
        result = compute_lots(fills)

        assert len(result.closed_lots) == 1
        assert result.closed_lots[0].shares_matched == pytest.approx(10.0)

        assert len(result.open_lots) == 1
        flipped = result.open_lots[0]
        assert flipped.open_fill_id == "f2"
        assert flipped.open_side == "SELL"  # now short
        assert flipped.shares_open == pytest.approx(5.0)  # the excess

    def test_yes_and_no_outcomes_on_same_slug_never_netted(self):
        fills = [
            _fill("f1", "BUY", 0.40, 10.0, outcome="YES", transact_time="2026-07-06T14:00:00Z"),
            _fill("f2", "SELL", 0.30, 10.0, outcome="NO", transact_time="2026-07-06T14:01:00Z"),
        ]
        result = compute_lots(fills)

        # Opposite sides, but different outcomes -- must NOT close each
        # other out. Both remain independently open.
        assert result.closed_lots == []
        assert len(result.open_lots) == 2
        assert {lot.outcome for lot in result.open_lots} == {"YES", "NO"}


class TestEmptyDequeReducingFillSafetyNet:
    def test_sell_long_intent_is_no_longer_treated_as_reducing(self):
        # ORDER_INTENT_SELL_LONG was assumed to mean "this fill reduces a
        # pre-existing position" -- disproven against real fill history
        # (100% of the fills this routed to unmatched carried this exact
        # intent, and over half were the first-ever fill for their market,
        # i.e. selling to OPEN a fresh short). It must now behave exactly
        # like an unrecognized intent: a normal new open, not unmatched.
        fills = [_fill("f1", "SELL", 0.50, 10.0, intent="ORDER_INTENT_SELL_LONG")]
        result = compute_lots(fills)

        assert result.unmatched_closing_fills == []
        assert len(result.open_lots) == 1
        assert result.open_lots[0].open_side == "SELL"

    def test_unrecognized_intent_becomes_normal_new_open(self):
        # No intent at all -- treated as a genuine new (short) open, not
        # routed to unmatched -- matches the "don't guess" safety-net rule.
        fills = [_fill("f1", "SELL", 0.50, 10.0)]
        result = compute_lots(fills)

        assert result.unmatched_closing_fills == []
        assert len(result.open_lots) == 1
        assert result.open_lots[0].open_side == "SELL"

    def test_sell_long_open_then_buy_close_now_recovers_real_short_pnl(self):
        # The exact real-production shape this bug hid: a brand-new market
        # (no prior fills, so the deque starts empty) where the bot SELLS
        # first to open a short (labeled ORDER_INTENT_SELL_LONG by the
        # exchange) and BUYS later to close it. Before the fix, the SELL
        # was discarded as "unmatched" and the BUY became a phantom new
        # long open -- the real, profitable short round trip was invisible
        # to live-pnl entirely. Shape matches a real fill pair from
        # 2026-07-17 (astatc-fwc-fra-eng-...-fh-ftts-none): SELL 1.5@0.30,
        # BUY 1.5@0.31, $0.02 commission on the close.
        fills = [
            _fill(
                "f1", "SELL", 0.30, 1.5, transact_time="2026-07-17T23:27:59Z",
                intent="ORDER_INTENT_SELL_LONG", commission_usd=0.0,
            ),
            _fill(
                "f2", "BUY", 0.31, 1.5, transact_time="2026-07-18T00:28:04Z",
                intent="ORDER_INTENT_SELL_LONG", commission_usd=0.02,
            ),
        ]
        result = compute_lots(fills)

        assert result.unmatched_closing_fills == []
        assert result.open_lots == []
        assert len(result.closed_lots) == 1
        lot = result.closed_lots[0]
        assert lot.open_side == "SELL"
        assert lot.gross_pnl_usd == pytest.approx((0.30 - 0.31) * 1.5)
        assert lot.net_pnl_usd == pytest.approx((0.30 - 0.31) * 1.5 - 0.02)


def _snapshot(market_slug, net_position_shares, timestamp):
    return {"market_slug": market_slug, "net_position_shares": net_position_shares, "timestamp": timestamp}


class TestPreTrackingInventorySafetyNet:
    # _REDUCING_INTENTS being empty accepts a real residual risk: an
    # empty-deque fill might actually be closing genuine pre-tracking
    # inventory, not opening a fresh position. Where position_snapshots.json
    # actually has evidence of that (a nonzero position already existed the
    # first time we ever snapshotted this slug), don't ignore it.
    def test_snapshot_predating_fill_with_nonzero_position_routes_to_unmatched(self):
        fills = [_fill("f1", "SELL", 0.50, 10.0, market_slug="m1", transact_time="2026-07-06T15:00:00Z")]
        earliest = {"m1": _snapshot("m1", 5.0, "2026-07-06T14:00:00Z")}  # predates the fill, nonzero

        result = compute_lots(fills, earliest_snapshot_by_slug=earliest)

        assert result.open_lots == []
        assert len(result.unmatched_closing_fills) == 1
        assert result.unmatched_closing_fills[0]["fill_id"] == "f1"

    def test_snapshot_after_fill_does_not_flag_it(self):
        # The snapshot only proves inventory existed AT that later moment --
        # it says nothing about before the fill, so it must not be treated
        # as evidence against a fill that precedes it.
        fills = [_fill("f1", "SELL", 0.50, 10.0, market_slug="m1", transact_time="2026-07-06T14:00:00Z")]
        earliest = {"m1": _snapshot("m1", 5.0, "2026-07-06T15:00:00Z")}  # after the fill

        result = compute_lots(fills, earliest_snapshot_by_slug=earliest)

        assert result.unmatched_closing_fills == []
        assert len(result.open_lots) == 1

    def test_snapshot_with_zero_position_does_not_flag_it(self):
        # A flat snapshot is exactly what a genuinely fresh market looks
        # like before its first fill -- not evidence of prior inventory.
        fills = [_fill("f1", "SELL", 0.50, 10.0, market_slug="m1", transact_time="2026-07-06T15:00:00Z")]
        earliest = {"m1": _snapshot("m1", 0.0, "2026-07-06T14:00:00Z")}

        result = compute_lots(fills, earliest_snapshot_by_slug=earliest)

        assert result.unmatched_closing_fills == []
        assert len(result.open_lots) == 1

    def test_no_snapshot_for_slug_does_not_flag_it(self):
        # The common case for older fills: position_snapshots.json simply
        # has no coverage for this slug at all. Absence of a snapshot is
        # not evidence of absence of prior inventory -- it's unknown, and
        # that residual risk is already deliberately accepted (module
        # docstring). Must reproduce today's behavior exactly.
        fills = [_fill("f1", "SELL", 0.50, 10.0, market_slug="m1")]

        result = compute_lots(fills, earliest_snapshot_by_slug={})

        assert result.unmatched_closing_fills == []
        assert len(result.open_lots) == 1

    def test_omitting_the_parameter_entirely_reproduces_prior_behavior(self):
        fills = [_fill("f1", "SELL", 0.50, 10.0, market_slug="m1")]

        result = compute_lots(fills)  # no earliest_snapshot_by_slug at all

        assert result.unmatched_closing_fills == []
        assert len(result.open_lots) == 1

    def test_unparseable_snapshot_timestamp_flags_conservatively(self):
        fills = [_fill("f1", "SELL", 0.50, 10.0, market_slug="m1")]
        earliest = {"m1": _snapshot("m1", 5.0, "not-a-timestamp")}

        result = compute_lots(fills, earliest_snapshot_by_slug=earliest)

        assert result.open_lots == []
        assert len(result.unmatched_closing_fills) == 1

    def test_snapshot_on_a_different_slug_does_not_flag_this_fill(self):
        fills = [_fill("f1", "SELL", 0.50, 10.0, market_slug="m1", transact_time="2026-07-06T15:00:00Z")]
        earliest = {"m2": _snapshot("m2", 5.0, "2026-07-06T14:00:00Z")}

        result = compute_lots(fills, earliest_snapshot_by_slug=earliest)

        assert result.unmatched_closing_fills == []
        assert len(result.open_lots) == 1

    def test_real_2026_07_17_fill_shape_is_unaffected_by_the_safety_net(self):
        # The exact real-production round trip from the existing regression
        # test above, but now also given a REALISTIC earliest-snapshot
        # picture (flat before the fill, since this was a genuinely
        # brand-new single-game market) -- must still recover the real P&L,
        # not regress back to unmatched.
        fills = [
            _fill(
                "f1", "SELL", 0.30, 1.5, market_slug="astatc-fwc-fra-eng-2026-07-18-fh-ftts-none",
                transact_time="2026-07-17T23:27:59Z", intent="ORDER_INTENT_SELL_LONG", commission_usd=0.0,
            ),
            _fill(
                "f2", "BUY", 0.31, 1.5, market_slug="astatc-fwc-fra-eng-2026-07-18-fh-ftts-none",
                transact_time="2026-07-18T00:28:04Z", intent="ORDER_INTENT_SELL_LONG", commission_usd=0.02,
            ),
        ]
        earliest = {
            "astatc-fwc-fra-eng-2026-07-18-fh-ftts-none": _snapshot(
                "astatc-fwc-fra-eng-2026-07-18-fh-ftts-none", 0.0, "2026-07-17T23:00:00Z",
            ),
        }

        result = compute_lots(fills, earliest_snapshot_by_slug=earliest)

        assert result.unmatched_closing_fills == []
        assert len(result.closed_lots) == 1
        assert result.closed_lots[0].net_pnl_usd == pytest.approx((0.30 - 0.31) * 1.5 - 0.02)

    def test_flip_through_flat_reopen_is_not_flagged_by_a_stale_earlier_snapshot(self):
        # Real, confirmed false positive found while verifying this fix
        # against production data (astatc-fwc-arg-sui-2026-07-11-ga-
        # fwcliomes-gte2): SELL 6.5 opens a short; BUY 6.5 closes it exactly
        # (a full round trip fills.json itself completely explains); SELL
        # 6.0 legitimately reopens a fresh short afterward. A snapshot taken
        # WHILE the first short was still open (nonzero, predating the
        # reopen fill in wall-clock time) must not flag the reopen just
        # because it's chronologically earlier -- fills.json's own history
        # already fully explains the empty deque at that point, unlike the
        # slug's true first-ever fill, which has no such explanation
        # available. Only the slug's actual first fill should ever consult
        # the snapshot at all.
        fills = [
            _fill("f1", "SELL", 0.21, 6.5, market_slug="m1", transact_time="2026-07-09T21:48:15Z"),
            _fill("f2", "BUY", 0.23, 6.5, market_slug="m1", transact_time="2026-07-11T20:03:22Z"),
            _fill("f3", "SELL", 0.27, 6.0, market_slug="m1", transact_time="2026-07-11T22:22:23Z"),
        ]
        # Snapshot captured while the first short (f1) was still open --
        # predates f3 in wall-clock time, but does NOT predate f1 (the
        # slug's actual first fill), so must not apply to either.
        earliest = {"m1": _snapshot("m1", -6.5, "2026-07-10T21:32:02Z")}

        result = compute_lots(fills, earliest_snapshot_by_slug=earliest)

        assert result.unmatched_closing_fills == []
        assert len(result.closed_lots) == 1  # f1 closed by f2
        assert len(result.open_lots) == 1  # f3 still open
        assert result.open_lots[0].open_fill_id == "f3"


class TestPnlSign:
    def test_long_lot_profits_when_close_price_higher(self):
        fills = [
            _fill("f1", "BUY", 0.40, 10.0, transact_time="2026-07-06T14:00:00Z"),
            _fill("f2", "SELL", 0.50, 10.0, transact_time="2026-07-06T14:10:00Z"),
        ]
        result = compute_lots(fills)
        assert result.closed_lots[0].gross_pnl_usd == pytest.approx(1.0)  # (0.50-0.40)*10
        assert result.closed_lots[0].holding_seconds == pytest.approx(600.0)

    def test_short_lot_profits_when_close_price_lower(self):
        fills = [
            _fill("f1", "SELL", 0.50, 10.0, transact_time="2026-07-06T14:00:00Z"),  # opens short
            _fill("f2", "BUY", 0.40, 10.0, transact_time="2026-07-06T14:05:00Z"),  # covers
        ]
        result = compute_lots(fills)
        assert result.closed_lots[0].gross_pnl_usd == pytest.approx(1.0)  # (0.50-0.40)*10
        assert result.closed_lots[0].open_side == "SELL"


class TestCommissionAllocation:
    def test_allocated_proportionally_to_shares_matched(self):
        # Closing fill of 15 shares (commission -0.30) splits across two
        # open slices (10 and 5 shares) -- commission must split 10/15 and
        # 5/15, summing back to exactly -0.30, not doubled or dropped.
        fills = [
            _fill("f1", "BUY", 0.40, 10.0, commission_usd=-0.10, transact_time="2026-07-06T14:00:00Z"),
            _fill("f2", "BUY", 0.45, 5.0, commission_usd=-0.05, transact_time="2026-07-06T14:01:00Z"),
            _fill("f3", "SELL", 0.50, 15.0, commission_usd=-0.30, transact_time="2026-07-06T14:02:00Z"),
        ]
        result = compute_lots(fills)

        first, second = result.closed_lots
        assert first.open_commission_usd == pytest.approx(-0.10)  # whole open fill matched in one go
        assert first.close_commission_usd == pytest.approx(-0.30 * 10 / 15)
        assert second.open_commission_usd == pytest.approx(-0.05)
        assert second.close_commission_usd == pytest.approx(-0.30 * 5 / 15)

        total_close_commission = first.close_commission_usd + second.close_commission_usd
        assert total_close_commission == pytest.approx(-0.30)  # sums back exactly, no double count

    def test_none_commission_treated_as_zero(self):
        fills = [
            _fill("f1", "BUY", 0.40, 10.0, commission_usd=None, transact_time="2026-07-06T14:00:00Z"),
            _fill("f2", "SELL", 0.50, 10.0, commission_usd=None, transact_time="2026-07-06T14:01:00Z"),
        ]
        result = compute_lots(fills)
        assert result.closed_lots[0].open_commission_usd == 0.0
        assert result.closed_lots[0].close_commission_usd == 0.0

    def test_positive_collected_commission_reduces_net_and_negative_rebate_increases_it(self):
        fee_result = compute_lots([
            _fill("f1", "BUY", 0.40, 10.0, commission_usd=0.10),
            _fill("f2", "SELL", 0.50, 10.0, commission_usd=0.20),
        ])
        rebate_result = compute_lots([
            _fill("f3", "BUY", 0.40, 10.0, commission_usd=-0.10),
            _fill("f4", "SELL", 0.50, 10.0, commission_usd=-0.20),
        ])

        assert fee_result.closed_lots[0].gross_pnl_usd == pytest.approx(1.0)
        assert fee_result.closed_lots[0].net_pnl_usd == pytest.approx(0.70)
        assert rebate_result.closed_lots[0].net_pnl_usd == pytest.approx(1.30)


class TestMalformedAndEdgeInputs:
    def test_empty_fills_returns_empty_result(self):
        result = compute_lots([])
        assert result.closed_lots == []
        assert result.open_lots == []
        assert result.unmatched_closing_fills == []

    def test_fill_missing_market_slug_excluded_with_warning(self):
        fills = [_fill("f1", "BUY", 0.40, 10.0, market_slug=None)]
        result = compute_lots(fills)
        assert result.open_lots == []
        assert any("f1" in w for w in result.warnings)

    def test_fill_missing_shares_excluded_with_warning(self):
        fills = [_fill("f1", "BUY", 0.40, 0.0)]
        result = compute_lots(fills)
        assert result.open_lots == []
        assert any("f1" in w for w in result.warnings)


class TestSettlements:
    def _settlement(self, market_slug="m1", outcome="YES", confidence="observed", payout=1.0, **overrides):
        settlement = {
            "settlement_id": f"{market_slug}:{outcome}:s1",
            "market_slug": market_slug,
            "outcome": outcome,
            "detected_at": "2026-07-06T15:00:00Z",
            "last_seen_at": "2026-07-06T14:50:00Z",
            "confidence": confidence,
            "inferred_settlement_value_per_share": payout,
        }
        settlement.update(overrides)
        return settlement

    def test_observed_settlement_closes_remaining_long_inventory_at_payout(self):
        fills = [_fill("f1", "BUY", 0.40, 10.0, transact_time="2026-07-06T14:00:00Z")]
        settlements = [self._settlement(payout=1.0, confidence="observed")]

        result = compute_lots(fills, settlements)

        assert result.open_lots == []
        assert len(result.closed_lots) == 1
        closed = result.closed_lots[0]
        assert closed.closed_via == "settlement"
        assert closed.gross_pnl_usd == pytest.approx((1.0 - 0.40) * 10.0)
        assert closed.settlement_confidence == "observed"
        assert closed.close_price is None
        assert closed.close_fill_id is None

    def test_settlement_at_zero_payout_is_a_full_loss_for_long(self):
        fills = [_fill("f1", "BUY", 0.40, 10.0, transact_time="2026-07-06T14:00:00Z")]
        settlements = [self._settlement(payout=0.0, confidence="observed")]

        result = compute_lots(fills, settlements)

        assert result.closed_lots[0].gross_pnl_usd == pytest.approx((0.0 - 0.40) * 10.0)

    def test_inferred_low_confidence_marks_presumed_settled_not_closed(self):
        fills = [_fill("f1", "BUY", 0.40, 10.0, transact_time="2026-07-06T14:00:00Z")]
        settlements = [self._settlement(confidence="inferred_low", payout=None)]

        result = compute_lots(fills, settlements)

        assert result.closed_lots == []
        assert len(result.open_lots) == 1
        assert result.open_lots[0].presumed_settled is True
        assert result.open_lots[0].settlement_id == "m1:YES:s1"

    def test_settlement_for_unrelated_market_leaves_open_lot_untouched(self):
        fills = [_fill("f1", "BUY", 0.40, 10.0, market_slug="m1", transact_time="2026-07-06T14:00:00Z")]
        settlements = [self._settlement(market_slug="different-market")]

        result = compute_lots(fills, settlements)

        assert len(result.open_lots) == 1
        assert result.open_lots[0].presumed_settled is False

    def test_settlement_holding_seconds_uses_midpoint_of_bound(self):
        fills = [_fill("f1", "BUY", 0.40, 10.0, transact_time="2026-07-06T14:00:00Z")]
        # last_seen_at=14:50 -> 3000s since open; detected_at=15:00 -> 3600s.
        settlements = [self._settlement(
            payout=1.0, confidence="observed",
            last_seen_at="2026-07-06T14:50:00Z", detected_at="2026-07-06T15:00:00Z",
        )]

        result = compute_lots(fills, settlements)

        closed = result.closed_lots[0]
        assert closed.holding_seconds_lower == pytest.approx(3000.0)
        assert closed.holding_seconds_upper == pytest.approx(3600.0)
        assert closed.holding_seconds == pytest.approx(3300.0)  # midpoint
