from polymarket_bot.dashboard import render_event_exposure, render_pnl_attribution, render_pnl_reconciliation
from polymarket_bot.live.event_exposure import EventExposure
from polymarket_bot.live.lot_accounting import ClosedLot, LotAccountingResult
from polymarket_bot.live.pnl_attribution import compute_pnl_by_family, compute_pnl_summary
from polymarket_bot.live.reconciliation import PnlReconciliationReport


def test_render_event_exposure_shows_settled_and_projected_columns():
    exposures = [
        EventExposure(
            bucket_key="fwc-arg-sui-2026-07-11",
            market_count=2,
            cost_basis_usd=4.9,
            cash_value_usd=4.9,
            unrealized_pnl_usd=0.0,
            stat_prop_cost_basis_usd=4.9,
            pct_of_capital=0.049,
            stat_prop_pct_of_capital=0.049,
            resting_worst_case_usd=2.1,
            stat_prop_resting_worst_case_usd=2.1,
            projected_pct_of_capital=0.07,
            stat_prop_projected_pct_of_capital=0.07,
        ),
    ]

    output = render_event_exposure(exposures, capital_reference_usd=100.0)

    assert "fwc-arg-sui-2026-07-11" in output
    assert "Resting (worst-case)" in output
    assert "% of Capital (proj.)" in output
    assert "4.9%" in output  # settled %
    assert "7.0%" in output  # projected %


def test_render_event_exposure_empty_list():
    assert "No open positions" in render_event_exposure([], capital_reference_usd=100.0)


def test_render_event_exposure_sorts_by_projected_pct_descending():
    low = EventExposure(
        bucket_key="low", market_count=1, cost_basis_usd=1.0, cash_value_usd=1.0,
        unrealized_pnl_usd=0.0, stat_prop_cost_basis_usd=0.0, pct_of_capital=0.01,
        stat_prop_pct_of_capital=None, resting_worst_case_usd=0.0,
        stat_prop_resting_worst_case_usd=0.0, projected_pct_of_capital=0.01,
        stat_prop_projected_pct_of_capital=None,
    )
    high = EventExposure(
        bucket_key="high", market_count=1, cost_basis_usd=1.0, cash_value_usd=1.0,
        unrealized_pnl_usd=0.0, stat_prop_cost_basis_usd=0.0, pct_of_capital=0.01,
        stat_prop_pct_of_capital=None, resting_worst_case_usd=8.0,
        stat_prop_resting_worst_case_usd=0.0, projected_pct_of_capital=0.09,
        stat_prop_projected_pct_of_capital=None,
    )

    output = render_event_exposure([low, high], capital_reference_usd=100.0)

    assert output.index("high") < output.index("low")


def _closed_lot(market_slug="m1", net_pnl_usd=1.0):
    return ClosedLot(
        lot_id="lot1", market_slug=market_slug, outcome="YES", event_bucket_key="bucket1",
        open_fill_id="f1", open_order_id="o1", open_side="BUY", open_price=0.40,
        open_time="2026-07-06T14:00:00+00:00", close_fill_id="f2", close_order_id="o2",
        close_price=0.50, close_time="2026-07-06T15:00:00+00:00", closed_via="fill",
        shares_matched=10.0, gross_pnl_usd=net_pnl_usd, open_commission_usd=-0.02,
        close_commission_usd=-0.02, net_pnl_usd=net_pnl_usd, holding_seconds=3600.0,
    )


class TestRenderPnlAttribution:
    def test_no_lots_at_all_shows_empty_state(self):
        result = LotAccountingResult()
        output = render_pnl_attribution(compute_pnl_summary(result), {}, result)
        assert "No fills recorded yet" in output

    def test_shows_summary_and_breakdown_table(self):
        closed = [_closed_lot(net_pnl_usd=1.0)]
        result = LotAccountingResult(closed_lots=closed)
        summary = compute_pnl_summary(result)
        breakdowns = {"Family": compute_pnl_by_family(closed)}

        output = render_pnl_attribution(summary, breakdowns, result)

        assert "$1.00" in output  # total realized P&L
        assert "By Family" in output
        assert "Closed lots" in output

    def test_unmatched_closing_fills_noted(self):
        result = LotAccountingResult(
            closed_lots=[_closed_lot()],
            unmatched_closing_fills=[{"fill_id": "orphan1"}],
        )
        output = render_pnl_attribution(compute_pnl_summary(result), {}, result)
        assert "1 fill(s)" in output


class TestRenderPnlReconciliation:
    def test_empty_list(self):
        assert "nothing to reconcile" in render_pnl_reconciliation([])

    def test_shows_ok_and_mismatch_status(self):
        reports = [
            PnlReconciliationReport(
                market_slug="m1", strategy_a_realized_usd=1.0, strategy_b_realized_usd=1.02,
                delta_usd=-0.02, within_tolerance=True, lot_count=1, note=None,
            ),
            PnlReconciliationReport(
                market_slug="m2", strategy_a_realized_usd=1.0, strategy_b_realized_usd=10.0,
                delta_usd=-9.0, within_tolerance=False, lot_count=1, note=None,
            ),
        ]

        output = render_pnl_reconciliation(reports)

        assert "OK" in output
        assert "MISMATCH" in output
        assert "m1" in output
        assert "m2" in output

    def test_missing_snapshot_shows_note(self):
        reports = [
            PnlReconciliationReport(
                market_slug="m1", strategy_a_realized_usd=1.0, strategy_b_realized_usd=None,
                delta_usd=None, within_tolerance=False, lot_count=1,
                note="No position snapshot recorded for this market yet.",
            ),
        ]
        output = render_pnl_reconciliation(reports)
        assert "No position snapshot recorded" in output
