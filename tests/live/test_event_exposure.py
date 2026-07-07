import pytest

from polymarket_bot.live.event_exposure import (
    EventExposure,
    compute_event_exposures,
    derive_event_bucket_key,
    is_stat_prop_market,
    resolve_capital_reference_usd,
)


class TestDeriveEventBucketKey:
    def test_portugal_spain_corner_props_share_a_bucket(self):
        a = derive_event_bucket_key("astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5")
        b = derive_event_bucket_key("astatc-fwc-por-esp-2026-07-06-cor-team-esp-gt4pt5")
        c = derive_event_bucket_key("atc-fwc-por-esp-2026-07-06-exact-score-other")
        assert a == b == c == "fwc-por-esp-2026-07-06"

    def test_usa_belgium_is_a_distinct_bucket(self):
        assert derive_event_bucket_key(
            "astatc-fwc-usa-bel-2026-07-06-cor-team-usa-gt5pt5"
        ) == "fwc-usa-bel-2026-07-06"

    def test_pga_tournament_groups_different_golfers_together(self):
        a = derive_event_bucket_key("tec-pga-genescot-2026-07-12-w-scosch")
        b = derive_event_bucket_key("tec-pga-genescot-2026-07-12-w-rormci")
        assert a == b == "pga-genescot-2026-07-12"

    def test_mlb_division_futures(self):
        assert derive_event_bucket_key("tec-mlb-alwest-2026-09-27-w-tex") == "mlb-alwest-2026-09-27"

    def test_fifa_quarterfinal_single_team_slugs_coarsely_share_a_bucket(self):
        # Documented, deliberate coarsening: no opponent token means the
        # string can't recover match-pairing, so same-stage/same-date
        # single-team props collapse together. Over-grouping, not a bug.
        a = derive_event_bucket_key("aqc-fifa-wc-2026-07-07-quarterq-colbia")
        b = derive_event_bucket_key("aqc-fifa-wc-2026-07-07-quarterq-arg")
        assert a == b == "fifa-wc-2026-07-07"

    def test_no_date_triplet_falls_back_to_remainder(self):
        assert derive_event_bucket_key("no-hyphens-nodate") == "hyphens-nodate"

    def test_false_partial_date_match_falls_back_to_remainder(self):
        assert derive_event_bucket_key("astatc-2026-notadate-06-cor") == "2026-notadate-06-cor"

    def test_fewer_than_two_tokens_returns_whole_slug(self):
        assert derive_event_bucket_key("single") == "single"

    def test_empty_string_does_not_crash(self):
        assert derive_event_bucket_key("") == ""

    def test_event_slug_in_raw_takes_priority_over_heuristic(self):
        assert derive_event_bucket_key("astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5", raw={
            "eventSlug": "real-event-slug",
        }) == "real-event-slug"

    def test_event_id_used_when_event_slug_absent(self):
        assert derive_event_bucket_key("astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5", raw={
            "eventId": "12345",
        }) == "12345"

    def test_empty_raw_dict_falls_through_to_heuristic(self):
        assert derive_event_bucket_key("astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5", raw={}) == "fwc-por-esp-2026-07-06"


class TestIsStatPropMarket:
    def test_true_for_props_market_type(self):
        assert is_stat_prop_market({"marketType": "props"}) is True

    def test_false_for_futures_market_type(self):
        assert is_stat_prop_market({"marketType": "futures"}) is False

    def test_false_for_missing_market_type(self):
        assert is_stat_prop_market({}) is False

    def test_false_for_none_raw(self):
        assert is_stat_prop_market(None) is False


class TestResolveCapitalReferenceUsd:
    def test_uses_starting_capital_plus_pnl_when_configured(self):
        ref = resolve_capital_reference_usd(
            starting_capital_usd=107.0, total_position_pnl_usd=18.46, positions={},
        )
        assert ref == pytest.approx(125.46)

    def test_falls_back_to_deployed_cost_basis_when_unconfigured(self):
        positions = {
            "m1": {"netPositionDecimal": "10", "cost": {"value": "5.0"}},
            "m2": {"netPositionDecimal": "-5", "cost": {"value": "3.0"}},
            "m3": {"netPositionDecimal": "0", "cost": {"value": "100.0"}},  # flat, excluded
        }
        ref = resolve_capital_reference_usd(0.0, 0.0, positions)
        assert ref == pytest.approx(8.0)

    def test_none_when_both_unconfigured_and_nothing_deployed(self):
        assert resolve_capital_reference_usd(0.0, 0.0, {}) is None

    def test_none_when_positions_all_flat_and_unconfigured(self):
        positions = {"m1": {"netPositionDecimal": "0", "cost": {"value": "5.0"}}}
        assert resolve_capital_reference_usd(0.0, 0.0, positions) is None


class TestComputeEventExposures:
    def test_aggregates_multiple_markets_into_one_bucket(self):
        positions = {
            "astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5": {
                "netPositionDecimal": "-0.5", "cost": {"value": "0.26"}, "cashValue": {"value": "0.17"},
            },
            "astatc-fwc-por-esp-2026-07-06-cor-team-esp-gt4pt5": {
                "netPositionDecimal": "-35.01", "cost": {"value": "11.63"}, "cashValue": {"value": "0.35"},
            },
        }
        exposures = compute_event_exposures(positions, capital_reference_usd=100.0)
        assert len(exposures) == 1
        e = exposures[0]
        assert e.bucket_key == "fwc-por-esp-2026-07-06"
        assert e.market_count == 2
        assert e.cost_basis_usd == pytest.approx(11.89)
        assert e.cash_value_usd == pytest.approx(0.52)
        assert e.unrealized_pnl_usd == pytest.approx(0.52 - 11.89)
        assert e.pct_of_capital == pytest.approx(0.1189)

    def test_distinct_buckets_kept_separate(self):
        positions = {
            "astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5": {
                "netPositionDecimal": "5", "cost": {"value": "1.0"}, "cashValue": {"value": "1.0"},
            },
            "astatc-fwc-usa-bel-2026-07-06-cor-team-usa-gt5pt5": {
                "netPositionDecimal": "5", "cost": {"value": "2.0"}, "cashValue": {"value": "2.0"},
            },
        }
        exposures = compute_event_exposures(positions, capital_reference_usd=100.0)
        assert {e.bucket_key for e in exposures} == {"fwc-por-esp-2026-07-06", "fwc-usa-bel-2026-07-06"}

    def test_zero_net_positions_excluded(self):
        positions = {
            "astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5": {
                "netPositionDecimal": "0", "cost": {"value": "1.0"}, "cashValue": {"value": "1.0"},
            },
        }
        assert compute_event_exposures(positions, capital_reference_usd=100.0) == []

    def test_pct_of_capital_none_when_capital_reference_is_none(self):
        positions = {
            "astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5": {
                "netPositionDecimal": "5", "cost": {"value": "1.0"}, "cashValue": {"value": "1.0"},
            },
        }
        exposures = compute_event_exposures(positions, capital_reference_usd=None)
        assert exposures[0].pct_of_capital is None
        assert exposures[0].stat_prop_pct_of_capital is None

    def test_stat_prop_sub_total_isolated_via_raw_by_slug(self):
        positions = {
            "astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5": {
                "netPositionDecimal": "5", "cost": {"value": "3.0"}, "cashValue": {"value": "3.0"},
            },
            "atc-fwc-por-esp-2026-07-06-exact-score-other": {
                "netPositionDecimal": "5", "cost": {"value": "2.0"}, "cashValue": {"value": "2.0"},
            },
        }
        raw_by_slug = {
            "astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5": {"marketType": "props"},
            "atc-fwc-por-esp-2026-07-06-exact-score-other": {"marketType": "futures"},
        }
        exposures = compute_event_exposures(positions, capital_reference_usd=100.0, raw_by_slug=raw_by_slug)
        assert len(exposures) == 1
        e = exposures[0]
        assert e.cost_basis_usd == pytest.approx(5.0)
        assert e.stat_prop_cost_basis_usd == pytest.approx(3.0)
        assert e.stat_prop_pct_of_capital == pytest.approx(0.03)

    def test_empty_positions_returns_empty_list(self):
        assert compute_event_exposures({}, capital_reference_usd=100.0) == []

    def test_non_dict_position_value_skipped_not_crashed(self):
        positions = {"weird-slug": "not-a-dict"}
        assert compute_event_exposures(positions, capital_reference_usd=100.0) == []
