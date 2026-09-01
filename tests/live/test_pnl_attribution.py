import pytest

from polymarket_bot.live.lot_accounting import ClosedLot, LotAccountingResult, OpenLot
from polymarket_bot.live.pnl_attribution import (
    capital_hours,
    compute_pnl_by_entry_date,
    compute_pnl_by_entry_time_band,
    compute_pnl_by_event,
    compute_pnl_by_family,
    compute_pnl_by_price_band,
    compute_pnl_breakdown,
    compute_pnl_summary,
    entry_date_for,
    entry_time_band_for,
    price_band_for,
)


def _closed_lot(
    market_slug="astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5",
    event_bucket_key="fwc-por-esp-2026-07-06",
    open_price=0.40,
    net_pnl_usd=1.0,
    gross_pnl_usd=None,
    shares_matched=10.0,
    open_time="2026-07-06T14:00:00+00:00",
    holding_seconds=3600.0,
    closed_via="fill",
    open_commission_usd=-0.02,
    close_commission_usd=-0.02,
    **overrides,
):
    fields = dict(
        lot_id="lot1",
        market_slug=market_slug,
        outcome="YES",
        event_bucket_key=event_bucket_key,
        open_fill_id="f1",
        open_order_id="o1",
        open_side="BUY",
        open_price=open_price,
        open_time=open_time,
        close_fill_id="f2",
        close_order_id="o2",
        close_price=0.50,
        close_time="2026-07-06T15:00:00+00:00",
        closed_via=closed_via,
        shares_matched=shares_matched,
        gross_pnl_usd=gross_pnl_usd if gross_pnl_usd is not None else net_pnl_usd,
        open_commission_usd=open_commission_usd,
        close_commission_usd=close_commission_usd,
        net_pnl_usd=net_pnl_usd,
        holding_seconds=holding_seconds,
    )
    fields.update(overrides)
    return ClosedLot(**fields)


def _open_lot(market_slug="m1", open_price=0.40, shares_open=10.0, open_time="2026-07-06T14:00:00+00:00", **overrides):
    fields = dict(
        market_slug=market_slug, outcome="YES", event_bucket_key="bucket1",
        open_fill_id="f1", open_order_id="o1", open_side="BUY",
        open_price=open_price, open_time=open_time, shares_open=shares_open,
        open_commission_usd=-0.02,
    )
    fields.update(overrides)
    return OpenLot(**fields)


class TestCapitalHours:
    def test_closed_lot_uses_stored_holding_seconds(self):
        lot = _closed_lot(open_price=0.40, shares_matched=10.0, holding_seconds=3600.0)
        # 10 shares * 0.40 * 1 hour = 4.0 share-dollar-hours
        assert capital_hours(lot) == pytest.approx(4.0)

    def test_open_lot_recomputes_against_now(self):
        lot = _open_lot(open_price=0.40, shares_open=10.0, open_time="2026-07-06T14:00:00+00:00")
        result = capital_hours(lot, now="2026-07-06T16:00:00+00:00")  # 2 hours later
        assert result == pytest.approx(10.0 * 0.40 * 2.0)

    def test_open_lot_with_unparseable_open_time_is_zero(self):
        lot = _open_lot(open_time=None)
        assert capital_hours(lot, now="2026-07-06T16:00:00+00:00") == 0.0


class TestPriceBandFor:
    @pytest.mark.parametrize("price,expected", [
        (0.05, "0-10c (deep longshot)"),
        (0.10, "10-25c (longshot)"),
        (0.24, "10-25c (longshot)"),
        (0.40, "40-60c (coinflip)"),
        (0.59, "40-60c (coinflip)"),
        (0.75, "75-90c (favorite)"),
        (0.90, "90-100c (deep favorite)"),
        (1.00, "90-100c (deep favorite)"),
    ])
    def test_bands(self, price, expected):
        assert price_band_for(price) == expected


class TestEntryTimeBandFor:
    def test_bands_by_utc_hour(self):
        assert entry_time_band_for("2026-07-06T02:00:00+00:00") == "00-06 UTC"
        assert entry_time_band_for("2026-07-06T08:00:00+00:00") == "06-12 UTC"
        assert entry_time_band_for("2026-07-06T14:00:00+00:00") == "12-18 UTC"
        assert entry_time_band_for("2026-07-06T20:00:00+00:00") == "18-24 UTC"

    def test_unparseable_time_is_unknown(self):
        assert entry_time_band_for(None) == "unknown"
        assert entry_time_band_for("not-a-date") == "unknown"


class TestEntryDateFor:
    def test_extracts_calendar_date(self):
        assert entry_date_for("2026-07-06T14:00:00+00:00") == "2026-07-06"

    def test_unparseable_time_is_unknown(self):
        assert entry_date_for(None) == "unknown"


class TestComputePnlBreakdown:
    def test_groups_by_key_fn_and_sums_correctly(self):
        lots = [
            _closed_lot(market_slug="a", net_pnl_usd=1.0, open_commission_usd=-0.01, close_commission_usd=-0.01),
            _closed_lot(market_slug="a", net_pnl_usd=-0.5, open_commission_usd=-0.01, close_commission_usd=-0.01),
            _closed_lot(market_slug="b", net_pnl_usd=2.0, open_commission_usd=-0.01, close_commission_usd=-0.01),
        ]
        breakdowns = compute_pnl_breakdown(lots, key_fn=lambda lot: lot.market_slug)
        by_bucket = {b.bucket: b for b in breakdowns}

        assert by_bucket["a"].lot_count == 2
        assert by_bucket["a"].realized_pnl_usd == pytest.approx(0.5)
        assert by_bucket["a"].win_count == 1
        assert by_bucket["a"].loss_count == 1
        assert by_bucket["a"].win_rate == pytest.approx(0.5)
        assert by_bucket["a"].commission_usd == pytest.approx(-0.04)

        assert by_bucket["b"].lot_count == 1
        assert by_bucket["b"].realized_pnl_usd == pytest.approx(2.0)
        assert by_bucket["b"].win_rate == pytest.approx(1.0)

    def test_most_profitable_bucket_sorted_first(self):
        lots = [
            _closed_lot(market_slug="low", net_pnl_usd=0.5),
            _closed_lot(market_slug="high", net_pnl_usd=5.0),
        ]
        breakdowns = compute_pnl_breakdown(lots, key_fn=lambda lot: lot.market_slug)
        assert breakdowns[0].bucket == "high"

    def test_win_rate_none_when_no_decided_lots(self):
        lots = [_closed_lot(net_pnl_usd=0.0)]  # exactly breakeven -- neither win nor loss
        breakdowns = compute_pnl_breakdown(lots, key_fn=lambda lot: "x")
        assert breakdowns[0].win_rate is None

    def test_empty_input_returns_empty_list(self):
        assert compute_pnl_breakdown([], key_fn=lambda lot: "x") == []


class TestBreakdownWrappers:
    def test_by_family_reuses_classify_family(self):
        lots = [_closed_lot(market_slug="astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5", net_pnl_usd=1.0)]
        breakdowns = compute_pnl_by_family(lots)
        assert len(breakdowns) == 1
        assert breakdowns[0].bucket  # some real family label, not blank

    def test_by_event_uses_stored_event_bucket_key(self):
        lots = [_closed_lot(event_bucket_key="fwc-por-esp-2026-07-06", net_pnl_usd=1.0)]
        breakdowns = compute_pnl_by_event(lots)
        assert breakdowns[0].bucket == "fwc-por-esp-2026-07-06"

    def test_by_event_falls_back_to_unknown_when_none(self):
        lots = [_closed_lot(event_bucket_key=None, net_pnl_usd=1.0)]
        breakdowns = compute_pnl_by_event(lots)
        assert breakdowns[0].bucket == "unknown"

    def test_by_price_band_uses_open_price_not_close_price(self):
        lots = [_closed_lot(open_price=0.05, net_pnl_usd=1.0)]  # deep longshot entry
        breakdowns = compute_pnl_by_price_band(lots)
        assert breakdowns[0].bucket == "0-10c (deep longshot)"

    def test_by_entry_time_band(self):
        lots = [_closed_lot(open_time="2026-07-06T20:00:00+00:00", net_pnl_usd=1.0)]
        breakdowns = compute_pnl_by_entry_time_band(lots)
        assert breakdowns[0].bucket == "18-24 UTC"

    def test_by_entry_date(self):
        lots = [_closed_lot(open_time="2026-07-06T20:00:00+00:00", net_pnl_usd=1.0)]
        breakdowns = compute_pnl_by_entry_date(lots)
        assert breakdowns[0].bucket == "2026-07-06"


class TestComputePnlSummary:
    def test_totals_reconcile_against_breakdown_sum(self):
        closed = [
            _closed_lot(market_slug="a", net_pnl_usd=1.0, closed_via="fill"),
            _closed_lot(market_slug="b", net_pnl_usd=2.0, closed_via="settlement"),
        ]
        result = LotAccountingResult(closed_lots=closed, open_lots=[])
        summary = compute_pnl_summary(result)
        breakdown_total = sum(b.realized_pnl_usd for b in compute_pnl_by_family(closed))

        assert summary.total_realized_pnl_usd == pytest.approx(3.0)
        assert summary.total_realized_pnl_usd == pytest.approx(breakdown_total)
        assert summary.closed_lot_count == 2
        assert summary.closed_via_fill_count == 1
        assert summary.closed_via_settlement_count == 1

    def test_presumed_settled_unresolved_counted_separately_from_open(self):
        open_lots = [
            _open_lot(market_slug="a", presumed_settled=True),
            _open_lot(market_slug="b", presumed_settled=False),
        ]
        result = LotAccountingResult(closed_lots=[], open_lots=open_lots)
        summary = compute_pnl_summary(result)

        assert summary.open_lot_count == 2
        assert summary.presumed_settled_unresolved_count == 1

    def test_overall_pnl_per_capital_hour_excludes_open_capital_hours(self):
        # One closed lot with real P&L, one open lot with a lot of
        # capital-hours but nothing realized yet -- open capital-hours must
        # NOT dilute the closed-lot-based efficiency metric.
        closed = [_closed_lot(open_price=0.50, shares_matched=10.0, holding_seconds=3600.0, net_pnl_usd=5.0)]
        # capital_hours = 10*0.50*1 = 5.0 -> pnl_per_capital_hour = 5.0/5.0 = 1.0
        open_lots = [_open_lot(open_price=0.50, shares_open=1000.0, open_time="2020-01-01T00:00:00+00:00")]
        result = LotAccountingResult(closed_lots=closed, open_lots=open_lots)

        summary = compute_pnl_summary(result, now="2026-07-06T15:00:00+00:00")

        assert summary.overall_pnl_per_capital_hour == pytest.approx(1.0)
        assert summary.open_capital_hours > summary.closed_capital_hours  # sanity: open dwarfs closed here

    def test_no_closed_lots_pnl_per_capital_hour_is_none(self):
        result = LotAccountingResult(closed_lots=[], open_lots=[_open_lot()])
        summary = compute_pnl_summary(result)
        assert summary.overall_pnl_per_capital_hour is None
