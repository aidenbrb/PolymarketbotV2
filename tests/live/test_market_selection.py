from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live.market_selection import (
    _activity_confidence,
    _captured_spread_cents,
    _diversify_by_event,
    _expected_value_score,
    _fill_confidence,
    _l2_depth_liquidity_fallback,
    _passes_static_paired_entry_guard,
    _warn_if_liquidity_data_looks_broken,
    is_eligible,
    is_observation_market_eligible,
    is_excluded_market_family,
    select_target_market,
    select_target_markets,
)
from polymarket_bot.models import FilterResult, Market, ScoredMarket


def _scored(
    recommendation="PAPER_CANDIDATE", category="politics", token_ids=("t1", "t2"), score=90.0,
    spread=0.02,
    question="Will X happen?",
    raw=None,
    end_date=None,
    outcomes=("Yes", "No"),
    market_id="m1",
    volume_24h=1000.0,
    liquidity=1000.0,
):
    raw = dict(raw or {})
    raw.setdefault("category", category)
    raw.setdefault("question", question)
    market = Market(
        market_id=market_id,
        event_id="e1",
        question=question,
        category=category,
        token_ids=list(token_ids),
        outcomes=list(outcomes),
        volume_24h=volume_24h,
        liquidity=liquidity,
        spread=spread,
        raw=raw,
        end_date=end_date,
    )
    return ScoredMarket(
        market_id=market_id,
        question=question,
        total_score=score,
        component_scores={},
        explanation=[],
        recommendation=recommendation,
        market=market,
    )


def _settings(**overrides):
    defaults = dict(
        min_score_recommendation="PAPER_CANDIDATE",
        exclude_categories=("sports",),
        min_edge_cents=0.5,
        max_days_to_close=3.0,
        max_started_event_hours=6.0,
        exclude_market_types=(),
        exclude_question_keywords=("champion", "championship", "mvp", "cy young", "pennant"),
        exclude_slug_prefixes=(),
        exclude_sports_market_types=(),
    )
    defaults.update(overrides)
    return config.LiveTradingSettings(**defaults)


def test_is_eligible_accepts_top_tier_market():
    assert is_eligible(_scored(), _settings())


def test_started_market_is_observed_when_live_started_allowance_is_zero():
    started = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    scored = _scored(raw={"gameStartTime": started})
    settings = _settings(
        max_started_event_hours=0.0,
        observation_legacy_max_started_event_hours=6.0,
    )

    assert is_eligible(scored, settings) is False
    assert is_observation_market_eligible(scored.market, settings) is True


def test_observation_universe_ignores_quote_filters_but_keeps_hard_safety_exclusions():
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    quiet = _scored(
        market_id="quiet", recommendation="REJECT", spread=0.0,
        liquidity=0.0, volume_24h=0.0,
        raw={"gameStartTime": future},
    )
    excluded = _scored(
        market_id="excluded", category="sports", recommendation="PAPER_CANDIDATE",
        raw={"gameStartTime": future},
    )
    scanner = Mock()
    scanner.scan.return_value = [quiet.market, excluded.market]
    filters = Mock()
    filters.apply.return_value = ([], [])
    scoring = Mock()
    scoring.score.side_effect = [quiet, excluded]
    observed = []

    selected = select_target_markets(
        scanner=scanner, filters=filters, scoring=scoring, settings=_settings(),
        observation_markets_out=observed,
    )

    assert selected == []
    assert [item.market_id for item in observed] == ["quiet"]


def test_static_paired_entry_guard_requires_both_sides_under_ratio_cap():
    safe = _scored(spread=0.05, raw={"orderPriceMinTickSize": "0.01"})
    safe.market.best_bid = 0.53
    safe.market.best_ask = 0.58
    asymmetric = _scored(spread=0.04, raw={"orderPriceMinTickSize": "0.01"})
    asymmetric.market.best_bid = 0.22
    asymmetric.market.best_ask = 0.26
    settings = _settings(
        max_payoff_loss_to_capture_ratio=20.0,
        require_both_entry_legs=True,
    )

    assert _passes_static_paired_entry_guard(safe, settings)
    assert not _passes_static_paired_entry_guard(asymmetric, settings)


def test_is_eligible_rejects_below_min_tier():
    assert not is_eligible(_scored(recommendation="STRONG_WATCH"), _settings())


def test_is_eligible_accepts_above_min_tier_when_min_lowered():
    settings = _settings(min_score_recommendation="STRONG_WATCH")
    assert is_eligible(_scored(recommendation="PAPER_CANDIDATE"), settings)
    assert is_eligible(_scored(recommendation="STRONG_WATCH"), settings)


def test_is_eligible_rejects_excluded_category():
    assert not is_eligible(_scored(category="sports"), _settings())


def _exclusion_settings(**overrides):
    defaults = dict(
        exclude_categories=("climate",),
        exclude_question_keywords=("temperature",),
        exclude_slug_prefixes=("tc-temp",),
        exclude_sports_market_types=("esports_match_winner",),
    )
    defaults.update(overrides)
    return _settings(**defaults)


def test_is_excluded_market_family_matches_category():
    assert is_excluded_market_family("m1", {"category": "climate"}, _exclusion_settings())


def test_is_excluded_market_family_matches_question_keyword():
    assert is_excluded_market_family(
        "m1", {"question": "Highest temperature in NYC on July 7?"}, _exclusion_settings()
    )


def test_is_excluded_market_family_matches_slug_prefix():
    assert is_excluded_market_family("tc-temp-laxhigh-2026-07-07-gte73lt74f", None, _exclusion_settings())
    assert is_excluded_market_family("tc-temp-laxhigh-2026-07-07-gte73lt74f", {}, _exclusion_settings())


def test_is_excluded_market_family_case_insensitive():
    assert is_excluded_market_family("m1", {"category": "CLIMATE"}, _exclusion_settings())
    assert is_excluded_market_family("m1", {"question": "TEMPERATURE check"}, _exclusion_settings())
    assert is_excluded_market_family("TC-TEMP-nychigh-2026-07-07", None, _exclusion_settings())


def test_is_excluded_market_family_fails_open_without_raw_except_slug_prefix():
    settings = _exclusion_settings()
    assert not is_excluded_market_family("some-other-slug", None, settings)
    assert not is_excluded_market_family("some-other-slug", {}, settings)


def test_is_excluded_market_family_false_when_nothing_matches():
    assert not is_excluded_market_family(
        "m1", {"category": "sports", "question": "Will the Yankees win?"}, _exclusion_settings()
    )


def test_is_excluded_market_family_matches_esports_via_sports_market_type():
    # Real raw shape confirmed against a live snapshot: category is "sports"
    # (shared with traditional sports, not exclusive), marketType is
    # "drawable_outcome" (also shared, not esports-specific) -- only
    # sportsMarketType cleanly isolates esports.
    raw = {
        "category": "sports",
        "marketType": "drawable_outcome",
        "sportsMarketType": "esports_match_winner",
        "question": "Will Team Liquid win the esports series against Level UP?",
    }
    assert is_excluded_market_family("atc-dota2-liquid-lvlup-2026-07-08-liquid", raw, _exclusion_settings())


def test_is_excluded_market_family_esports_case_insensitive():
    raw = {"sportsMarketType": "ESPORTS_MATCH_WINNER"}
    assert is_excluded_market_family("m1", raw, _exclusion_settings())


def test_is_excluded_market_family_esports_signal_is_independent_of_other_layers():
    # With the esports layer disabled, the same category/marketType alone
    # must NOT already exclude it -- proves this is a genuinely new signal,
    # not something the pre-existing category/keyword/slug checks already
    # caught by accident.
    raw = {
        "category": "sports",
        "marketType": "drawable_outcome",
        "sportsMarketType": "esports_match_winner",
    }
    settings = _exclusion_settings(exclude_sports_market_types=())
    assert not is_excluded_market_family("atc-dota2-liquid-lvlup-2026-07-08-liquid", raw, settings)


def test_is_excluded_market_family_traditional_sports_not_excluded_by_esports_layer():
    # Same "atc-" prefix as the Dota example above, but a real traditional-
    # sports sportsMarketType -- confirms the check is genuinely scoped to
    # sportsMarketType, not the shared org-code slug prefix.
    raw = {"category": "sports", "marketType": "moneyline", "sportsMarketType": "soccer_team_full_time_winner"}
    assert not is_excluded_market_family("atc-fwc-fra-mar-2026-07-09-fra", raw, _exclusion_settings())


def test_is_eligible_rejects_esports_market():
    raw = {
        "category": "sports",
        "marketType": "drawable_outcome",
        "sportsMarketType": "esports_match_winner",
        "question": "Will Team Liquid win the esports series against Level UP?",
    }
    scored = _scored(
        market_id="atc-dota2-liquid-lvlup-2026-07-08-liquid",
        category="sports", question=raw["question"], raw=raw,
    )
    assert not is_eligible(scored, _exclusion_settings())


def test_is_eligible_rejects_real_incident_weather_market_via_all_three_layers():
    # Regression case: the real market that caused today's circuit-breaker
    # trip -- verify each of the three exclusion layers independently
    # catches it, since any one alone should be sufficient.
    raw = {
        "category": "climate",
        "question": "Highest temperature in Los Angeles on July 7?",
        "marketType": "futures",
    }
    slug = "tc-temp-laxhigh-2026-07-07-gte73lt74f"

    by_category_only = _scored(
        market_id=slug, category="climate", question="irrelevant", raw={**raw, "question": "irrelevant"},
    )
    assert not is_eligible(
        by_category_only,
        _settings(exclude_categories=("climate",), exclude_question_keywords=(), exclude_slug_prefixes=()),
    )

    by_keyword_only = _scored(
        market_id=slug, category="other", question=raw["question"],
        raw={**raw, "category": "other"},
    )
    assert not is_eligible(
        by_keyword_only,
        _settings(exclude_categories=(), exclude_question_keywords=("temperature",), exclude_slug_prefixes=()),
    )

    by_slug_only = _scored(
        market_id=slug, category="other", question="irrelevant",
        raw={"category": "other", "question": "irrelevant", "marketType": "futures"},
    )
    assert not is_eligible(
        by_slug_only,
        _settings(exclude_categories=(), exclude_question_keywords=(), exclude_slug_prefixes=("tc-temp",)),
    )


def test_is_eligible_accepts_when_no_exclusion_layer_matches():
    assert is_eligible(_scored(category="politics", question="Will X happen?"), _exclusion_settings())


def test_is_eligible_rejects_missing_token_ids():
    assert not is_eligible(_scored(token_ids=()), _settings())


def test_is_eligible_rejects_long_dated_live_market():
    far_end = (datetime.now(timezone.utc) + timedelta(days=120)).isoformat()
    assert not is_eligible(_scored(end_date=far_end), _settings())


def test_is_eligible_uses_game_start_time_before_settlement_end_date():
    soon_start = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    settlement_end = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    scored = _scored(
        category="sports",
        end_date=settlement_end,
        raw={"gameStartTime": soon_start, "endDate": settlement_end},
    )

    assert is_eligible(scored, _settings(exclude_categories=()))


def test_is_eligible_rejects_long_dated_futures_market_despite_near_game_start_time():
    # Real-world case: a season-long futures market (division winner) has
    # gameStartTime set to the team's NEXT game (always near-term), but
    # doesn't itself resolve until the season ends -- endDate must govern
    # the time-window check for marketType == "futures", not gameStartTime.
    tomorrow_game = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    season_end = (datetime.now(timezone.utc) + timedelta(days=97)).isoformat()
    scored = _scored(
        category="sports",
        raw={"marketType": "futures", "gameStartTime": tomorrow_game, "endDate": season_end},
    )

    assert not is_eligible(scored, _settings(exclude_categories=()))


def test_is_eligible_rejects_stale_started_event():
    stale_start = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
    scored = _scored(raw={"gameStartTime": stale_start})

    assert not is_eligible(scored, _settings())


def test_is_eligible_rejects_futures_market_type():
    assert not is_eligible(
        _scored(raw={"marketType": "futures"}),
        _settings(exclude_market_types=("futures",)),
    )


def test_is_eligible_rejects_championship_keyword():
    assert not is_eligible(_scored(question="World Series Champion"), _settings())


def test_select_target_market_picks_widest_real_spread_among_eligible():
    # Ranking is by real spread now (the actual market-making opportunity),
    # not by the general research score -- narrow-spread and ineligible
    # candidates must lose out to the widest-spread eligible one.
    scored_narrow = _scored(score=95.0, spread=0.01, volume_24h=5000.0, liquidity=5000.0)
    scored_wide = _scored(score=85.0, spread=0.03, volume_24h=5000.0, liquidity=5000.0)
    scored_ineligible = _scored(recommendation="WATCH", score=99.0, spread=0.05)

    scanner = Mock()
    scanner.scan.return_value = ["m-narrow", "m-wide", "m-ineligible"]
    filters = Mock()
    filters.apply.return_value = (["m-narrow", "m-wide", "m-ineligible"], [])
    scoring = Mock()
    scoring.score.side_effect = [scored_narrow, scored_wide, scored_ineligible]

    chosen = select_target_market(
        scanner=scanner, filters=filters, scoring=scoring, settings=_settings()
    )
    assert chosen is scored_wide


def test_select_target_market_returns_none_when_nothing_eligible():
    scanner = Mock()
    scanner.scan.return_value = ["m1"]
    filters = Mock()
    filters.apply.return_value = (["m1"], [])
    scoring = Mock()
    scoring.score.return_value = _scored(recommendation="WATCH")

    chosen = select_target_market(
        scanner=scanner, filters=filters, scoring=scoring, settings=_settings()
    )
    assert chosen is None


def test_select_target_market_returns_none_when_no_market_has_enough_edge():
    # Eligible on quality/tier, but real spread is below the minimum edge
    # required to bother quoting -- must not force a trade with no edge.
    scanner = Mock()
    scanner.scan.return_value = ["m1"]
    filters = Mock()
    filters.apply.return_value = (["m1"], [])
    scoring = Mock()
    scoring.score.return_value = _scored(spread=0.001)  # 0.1c, below the 0.5c minimum

    chosen = select_target_market(
        scanner=scanner, filters=filters, scoring=scoring, settings=_settings()
    )
    assert chosen is None


def test_select_target_market_rejects_edge_that_vanishes_after_tick_improvement():
    # A 2c raw spread with a 1c tick leaves exactly 0c after improving both
    # the bid and the ask by one tick each (see
    # live/pricing.py::compute_book_aware_quote) -- below the 0.5c minimum,
    # even though the raw 2c spread alone would clear a naive check.
    scanner = Mock()
    scanner.scan.return_value = ["m1"]
    filters = Mock()
    filters.apply.return_value = (["m1"], [])
    scoring = Mock()
    scoring.score.return_value = _scored(spread=0.02, raw={"orderPriceMinTickSize": 0.01})

    chosen = select_target_market(
        scanner=scanner, filters=filters, scoring=scoring, settings=_settings(min_edge_cents=0.5)
    )
    assert chosen is None


def test_select_target_market_accepts_edge_that_survives_tick_improvement():
    # A 3c raw spread with a 1c tick leaves 1c captured after improving both
    # sides -- above the 0.5c minimum.
    scanner = Mock()
    scanner.scan.return_value = ["m1"]
    filters = Mock()
    filters.apply.return_value = (["m1"], [])
    scoring = Mock()
    scoring.score.return_value = _scored(spread=0.03, raw={"orderPriceMinTickSize": 0.01})

    chosen = select_target_market(
        scanner=scanner, filters=filters, scoring=scoring, settings=_settings(min_edge_cents=0.5)
    )
    assert chosen is not None


def test_is_eligible_accepts_verified_binary_yes_no_market():
    scored = _scored(token_ids=("t1", "t2"), outcomes=("Yes", "No"))
    assert is_eligible(scored, _settings())


def test_is_eligible_accepts_yes_no_ignoring_case_and_whitespace():
    scored = _scored(token_ids=("t1", "t2"), outcomes=(" yes ", "NO"))
    assert is_eligible(scored, _settings())


def test_is_eligible_rejects_non_binary_outcome_labels():
    # e.g. a team-name-labeled sports moneyline, not a literal Yes/No market
    # -- OUTCOME_SIDE_YES's mapping onto this is unverified (see
    # live/RUNBOOK.md), so it must be rejected for live trading rather than
    # guessed at.
    scored = _scored(token_ids=("t1", "t2"), outcomes=("Chargers", "Titans"))
    assert not is_eligible(scored, _settings())


def test_is_eligible_rejects_more_than_two_outcomes():
    scored = _scored(token_ids=("t1", "t2", "t3"), outcomes=("A", "B", "C"))
    assert not is_eligible(scored, _settings())


def test_is_eligible_rejects_missing_outcomes_metadata():
    scored = _scored(token_ids=("t1", "t2"), outcomes=())
    assert not is_eligible(scored, _settings())


def test_is_eligible_allows_non_binary_when_explicitly_overridden():
    scored = _scored(token_ids=("t1", "t2"), outcomes=("Chargers", "Titans"))
    settings = _settings(allow_non_binary_markets=True)
    assert is_eligible(scored, settings)


def test_select_target_markets_scans_deeper_recent_window_and_returns_ranked_candidates():
    scored_old_wide = _scored(score=85.0, spread=0.03, volume_24h=5000.0, liquidity=5000.0)
    scored_old_wide.market.raw = {"createdAt": "2026-01-01T00:00:00Z"}
    # 0.026 (not the usual default 0.02): with the tick-aware edge check,
    # a 2c spread and the default 1c tick leaves exactly 0c captured after
    # improving both sides -- below the 0.5c minimum. This market needs to
    # actually clear the edge floor for this test to be about ranking, not
    # edge filtering.
    scored_new_narrow = _scored(score=90.0, spread=0.026, volume_24h=5000.0, liquidity=5000.0)
    scored_new_narrow.market.market_id = "m2"
    scored_new_narrow.market_id = "m2"
    scored_new_narrow.market.raw = {"createdAt": "2026-07-01T00:00:00Z"}

    scanner = Mock()
    scanner.scan.return_value = [scored_old_wide.market, scored_new_narrow.market]
    filters = Mock()
    filters.apply.side_effect = lambda markets: (markets, [])
    scoring = Mock()
    scoring.score.side_effect = lambda market: (
        scored_old_wide if market.market_id == "m1" else scored_new_narrow
    )

    chosen = select_target_markets(
        scanner=scanner,
        filters=filters,
        scoring=scoring,
        settings=_settings(newest_market_scan_limit=500, recent_market_scan_limit=5000),
        max_targets=10,
    )

    scanner.scan.assert_called_once_with(max_pages=50)
    assert chosen == [scored_old_wide, scored_new_narrow]


def test_select_target_markets_populates_raw_by_slug_out_from_full_scan():
    eligible = _scored(market_id="m-eligible", score=90.0, spread=0.05)
    eligible.market.raw = {"orderPriceMinTickSize": 0.01, "marketType": "futures"}
    filtered_out = _scored(market_id="m-filtered", score=90.0, spread=0.05)
    filtered_out.market.raw = {"orderPriceMinTickSize": 0.01, "marketType": "props"}

    scanner = Mock()
    scanner.scan.return_value = [eligible.market, filtered_out.market]
    filters = Mock()
    # Only "eligible" passes the general quality bar -- "filtered_out" is
    # rejected by MarketFilters, never even reaching scoring/eligibility.
    # Real MarketFilters.apply()'s second element is FilterResult objects,
    # not Market objects -- irrelevant to this test (which only checks
    # raw_by_slug_out), so left empty rather than mismodeled.
    filters.apply.return_value = ([eligible.market], [])
    scoring = Mock()
    scoring.score.return_value = eligible

    raw_by_slug_out: dict = {}
    select_target_markets(
        scanner=scanner, filters=filters, scoring=scoring, settings=_settings(),
        raw_by_slug_out=raw_by_slug_out,
    )

    # Populated from the FULL pre-filter scan, including a market that never
    # became eligible -- this is what lets an orphaned position (whose
    # market fell out of candidacy) still get raw data for the
    # event-started/near-resolution checks in multi_market_maker.py.
    assert raw_by_slug_out == {
        "m-eligible": eligible.market.raw,
        "m-filtered": filtered_out.market.raw,
    }


def test_select_target_markets_omitting_raw_by_slug_out_changes_nothing():
    scanner = Mock()
    scanner.scan.return_value = ["m1"]
    filters = Mock()
    filters.apply.return_value = (["m1"], [])
    scoring = Mock()
    scoring.score.return_value = _scored(recommendation="WATCH")

    # Must not raise even though the scan result here is a plain string, not
    # a real Market object -- raw_by_slug_out defaults to None and the
    # population line is skipped entirely.
    chosen = select_target_markets(scanner=scanner, filters=filters, scoring=scoring, settings=_settings())
    assert chosen == []


class TestWarnIfLiquidityDataLooksBroken:
    # Real incident: Polymarket US's market-listing response stopped
    # including volume/volume24hr entirely for ~36+ hours starting
    # 2026-07-18, silently zeroing every market's liquidity proxy with no
    # distinct signal anywhere in the logs -- see live/RUNBOOK.md's most
    # recent section.
    def test_warns_when_scan_is_almost_entirely_zero_liquidity(self, caplog):
        markets = [SimpleNamespace(liquidity=0.0) for _ in range(19)] + [SimpleNamespace(liquidity=500.0)]
        with caplog.at_level("WARNING"):
            _warn_if_liquidity_data_looks_broken(markets)
        assert any("looks like a market-data problem" in r.message for r in caplog.records)

    def test_does_not_warn_when_liquidity_data_looks_normal(self, caplog):
        # A genuinely mixed/quiet scan (well under the 95% threshold) must
        # not trigger the diagnostic -- real illiquid markets exist.
        markets = [SimpleNamespace(liquidity=0.0) for _ in range(5)] + [
            SimpleNamespace(liquidity=500.0) for _ in range(5)
        ]
        with caplog.at_level("WARNING"):
            _warn_if_liquidity_data_looks_broken(markets)
        assert caplog.records == []

    def test_empty_scan_does_not_warn(self, caplog):
        with caplog.at_level("WARNING"):
            _warn_if_liquidity_data_looks_broken([])
        assert caplog.records == []

    def test_never_crashes_on_a_market_missing_the_liquidity_attribute(self):
        # Purely diagnostic -- must never be able to crash real selection
        # on an unexpected market shape (e.g. a test double, or a future
        # refactor that changes what scanner.scan() returns).
        _warn_if_liquidity_data_looks_broken(["not-a-market-object"])  # must not raise


def test_select_target_markets_warns_when_scan_liquidity_is_broken(caplog):
    markets = [_scored(market_id=f"m{i}", liquidity=0.0).market for i in range(20)]
    scanner = Mock()
    scanner.scan.return_value = markets
    filters = Mock()
    filters.apply.return_value = ([], [])  # every market rejected; FilterResult detail irrelevant here
    scoring = Mock()

    with caplog.at_level("WARNING"):
        result = select_target_markets(scanner=scanner, filters=filters, scoring=scoring, settings=_settings())

    assert result == []
    assert any("looks like a market-data problem" in r.message for r in caplog.records)


class TestL2DepthLiquidityFallback:
    def test_recovers_a_market_rejected_only_for_liquidity_with_real_book_depth(self):
        market = _scored(market_id="m1", liquidity=0.0).market
        rejected = [FilterResult(
            market_id="m1", question="q", accepted=False,
            reasons=["Liquidity 0.00 below minimum 100.00"],
        )]
        scanner = Mock()
        scanner.client.get_market_book.return_value = {
            "bids": [{"price": 0.47, "quantity": 80.0}],
            "asks": [{"price": 0.53, "quantity": 50.0}],
        }
        settings = _settings(min_liquidity=100.0, liquidity_fallback_max_lookups=10)

        recovered = _l2_depth_liquidity_fallback(scanner, [market], rejected, settings)

        assert recovered == [market]
        assert market.liquidity == pytest.approx(130.0)
        assert market.best_bid == pytest.approx(0.47)
        assert market.best_ask == pytest.approx(0.53)
        assert market.midpoint == pytest.approx(0.50)
        assert market.spread == pytest.approx(0.06)
        scanner.client.get_market_book.assert_called_once_with("m1")

    def test_prioritizes_near_term_live_universe_before_lookup_cap(self):
        far = _scored(
            market_id="far", liquidity=0.0,
            raw={"gameStartTime": (
                datetime.now(timezone.utc) + timedelta(days=30)
            ).isoformat()},
        ).market
        near = _scored(
            market_id="near", liquidity=0.0,
            raw={"gameStartTime": (
                datetime.now(timezone.utc) + timedelta(hours=6)
            ).isoformat()},
        ).market
        rejected = [
            FilterResult(
                market_id=market.market_id, question="q", accepted=False,
                reasons=["Liquidity 0.00 below minimum 100.00"],
            )
            for market in (far, near)
        ]
        scanner = Mock()
        scanner.client.get_market_book.return_value = {
            "bids": [{"price": 0.47, "quantity": 80.0}],
            "asks": [{"price": 0.53, "quantity": 50.0}],
        }
        settings = _settings(
            min_liquidity=100.0,
            liquidity_fallback_max_lookups=1,
            exclude_categories=(),
        )

        recovered = _l2_depth_liquidity_fallback(
            scanner, [far, near], rejected, settings,
        )

        assert recovered == [near]
        scanner.client.get_market_book.assert_called_once_with("near")

    def test_l2_recovery_bypasses_only_the_outage_corrupted_research_tier(self):
        scored = _scored(
            market_id="m1", recommendation="REJECT", liquidity=0.0,
            spread=0.06,
        )
        rejected = [FilterResult(
            market_id="m1", question="q", accepted=False,
            reasons=["Liquidity 0.00 below minimum 100.00"],
        )]
        scanner = Mock()
        scanner.client.get_market_book.return_value = {
            "bids": [{"price": 0.47, "quantity": 80.0}],
            "asks": [{"price": 0.53, "quantity": 50.0}],
        }
        settings = _settings(
            min_liquidity=100.0,
            liquidity_fallback_max_lookups=10,
        )

        assert is_eligible(scored, settings) is False
        _l2_depth_liquidity_fallback(
            scanner, [scored.market], rejected, settings,
        )

        assert is_eligible(scored, settings) is True

    def test_does_not_recover_when_book_depth_still_below_minimum(self):
        market = _scored(market_id="m1", liquidity=0.0).market
        rejected = [FilterResult(
            market_id="m1", question="q", accepted=False,
            reasons=["Liquidity 0.00 below minimum 100.00"],
        )]
        scanner = Mock()
        scanner.client.get_market_book.return_value = {
            "bids": [{"price": 0.48, "quantity": 5.0}],
            "asks": [{"price": 0.52, "quantity": 5.0}],
        }
        settings = _settings(min_liquidity=100.0, liquidity_fallback_max_lookups=10)

        recovered = _l2_depth_liquidity_fallback(scanner, [market], rejected, settings)

        assert recovered == []

    def test_no_book_data_available_does_not_recover(self):
        market = _scored(market_id="m1", liquidity=0.0).market
        rejected = [FilterResult(
            market_id="m1", question="q", accepted=False,
            reasons=["Liquidity 0.00 below minimum 100.00"],
        )]
        scanner = Mock()
        scanner.client.get_market_book.return_value = None
        settings = _settings(min_liquidity=100.0, liquidity_fallback_max_lookups=10)

        recovered = _l2_depth_liquidity_fallback(scanner, [market], rejected, settings)

        assert recovered == []

    def test_does_not_look_up_a_market_rejected_for_other_reasons_too(self):
        # Bounded to markets that failed ONLY on liquidity/volume -- a
        # market that also fails a real, non-data-outage check (spread,
        # category, etc.) isn't a "missing upstream field" case and
        # shouldn't spend a book lookup.
        market = _scored(market_id="m1", liquidity=0.0).market
        rejected = [FilterResult(
            market_id="m1", question="q", accepted=False,
            reasons=["Liquidity 0.00 below minimum 100.00", "Spread 0.9900 exceeds maximum 0.9800"],
        )]
        scanner = Mock()
        settings = _settings(min_liquidity=100.0, liquidity_fallback_max_lookups=10)

        recovered = _l2_depth_liquidity_fallback(scanner, [market], rejected, settings)

        assert recovered == []
        scanner.client.get_market_book.assert_not_called()

    def test_respects_the_lookup_cap(self):
        markets = [_scored(market_id=f"m{i}", liquidity=0.0).market for i in range(5)]
        rejected = [
            FilterResult(market_id=f"m{i}", question="q", accepted=False, reasons=["Liquidity 0.00 below minimum 100.00"])
            for i in range(5)
        ]
        scanner = Mock()
        scanner.client.get_market_book.return_value = {
            "bids": [{"price": 0.5, "quantity": 200.0}], "asks": [],
        }
        settings = _settings(min_liquidity=100.0, liquidity_fallback_max_lookups=2)

        recovered = _l2_depth_liquidity_fallback(scanner, markets, rejected, settings)

        assert len(recovered) == 2
        assert scanner.client.get_market_book.call_count == 2

    def test_disabled_via_zero_limit(self):
        market = _scored(market_id="m1", liquidity=0.0).market
        rejected = [FilterResult(
            market_id="m1", question="q", accepted=False,
            reasons=["Liquidity 0.00 below minimum 100.00"],
        )]
        scanner = Mock()
        settings = _settings(min_liquidity=100.0, liquidity_fallback_max_lookups=0)

        recovered = _l2_depth_liquidity_fallback(scanner, [market], rejected, settings)

        assert recovered == []
        scanner.client.get_market_book.assert_not_called()

    def test_empty_rejected_list_does_not_touch_the_scanner(self):
        scanner = Mock()
        settings = _settings(min_liquidity=100.0, liquidity_fallback_max_lookups=10)

        recovered = _l2_depth_liquidity_fallback(scanner, [], [], settings)

        assert recovered == []
        scanner.client.get_market_book.assert_not_called()

    def test_select_target_markets_includes_recovered_market_in_final_ranking(self):
        # End-to-end: a market rejected only for liquidity, then recovered
        # by real book depth, actually reaches the ranked candidate output.
        recovered_scored = _scored(market_id="m-recovered", score=90.0, spread=0.05, liquidity=0.0)
        recovered_scored.market.raw = {"orderPriceMinTickSize": 0.01}
        scanner = Mock()
        scanner.scan.return_value = [recovered_scored.market]
        scanner.client.get_market_book.return_value = {
            "bids": [{"price": 0.47, "quantity": 80.0}],
            "asks": [{"price": 0.53, "quantity": 50.0}],
        }
        filters = Mock()
        filters.apply.return_value = ([], [FilterResult(
            market_id="m-recovered", question="q", accepted=False,
            reasons=["Liquidity 0.00 below minimum 100.00"],
        )])
        scoring = Mock()
        scoring.score.return_value = recovered_scored

        result = select_target_markets(
            scanner=scanner, filters=filters, scoring=scoring,
            settings=_settings(min_liquidity=100.0, liquidity_fallback_max_lookups=10),
        )

        assert result == [recovered_scored]


def test_captured_spread_cents_matches_tick_improvement_formula():
    scored = _scored(spread=0.05)
    assert _captured_spread_cents(scored.market, tick_size=0.01) == pytest.approx(3.0)
    assert _captured_spread_cents(None, tick_size=0.01) == 0.0
    scored.market.spread = None
    assert _captured_spread_cents(scored.market, tick_size=0.01) == 0.0


def test_fill_confidence_floor_linear_and_saturated():
    settings = _settings(
        fill_confidence_reference_depth=5000.0,
        min_fill_confidence=0.1,
    )

    zero_depth = _scored(volume_24h=0.0, liquidity=0.0)
    mid_depth = _scored(volume_24h=2000.0, liquidity=5000.0)  # proxy 2500
    high_depth = _scored(volume_24h=10000.0, liquidity=10000.0)

    assert _fill_confidence(zero_depth, settings) == 0.1
    assert _fill_confidence(mid_depth, settings) == 0.5
    assert _fill_confidence(high_depth, settings) == 1.0
    assert _fill_confidence(zero_depth, _settings(min_fill_confidence=2.0)) == 1.0


def test_expected_value_score_uses_captured_spread_times_confidence():
    settings = _settings(
        fill_confidence_reference_depth=5000.0,
        min_fill_confidence=0.1,
    )
    scored = _scored(
        spread=0.05,  # 3c captured after two 1c improvements
        volume_24h=2000.0,
        liquidity=5000.0,  # proxy 2500 -> confidence 0.5
        raw={"orderPriceMinTickSize": 0.01},
    )

    assert _expected_value_score(scored, settings) == pytest.approx(1.5)


def test_activity_confidence_falls_back_to_fill_confidence_when_no_score_provided():
    settings = _settings(fill_confidence_reference_depth=5000.0, min_fill_confidence=0.1)
    scored = _scored(market_id="m1", volume_24h=2000.0, liquidity=5000.0)  # proxy 2500 -> 0.5

    assert _activity_confidence(scored, settings, None) == pytest.approx(0.5)
    assert _activity_confidence(scored, settings, {}) == pytest.approx(0.5)
    assert _activity_confidence(scored, settings, {"m2": 0.9}) == pytest.approx(0.5)  # different market


def test_activity_confidence_prefers_real_signal_when_available():
    settings = _settings(fill_confidence_reference_depth=5000.0, min_fill_confidence=0.1)
    # Static proxy would say confidence 0.5 (proxy 2500) -- a real observed
    # activity score of 0.9 for this exact market must win instead.
    scored = _scored(market_id="m1", volume_24h=2000.0, liquidity=5000.0)

    assert _activity_confidence(scored, settings, {"m1": 0.9}) == pytest.approx(0.9)


def test_activity_confidence_clamps_to_0_1_range():
    settings = _settings()
    scored = _scored(market_id="m1")

    assert _activity_confidence(scored, settings, {"m1": 1.5}) == 1.0
    assert _activity_confidence(scored, settings, {"m1": -0.5}) == 0.0


def test_expected_value_score_uses_activity_confidence_when_provided():
    settings = _settings(fill_confidence_reference_depth=5000.0, min_fill_confidence=0.1)
    scored = _scored(
        market_id="m1", spread=0.05, volume_24h=2000.0, liquidity=5000.0,
        raw={"orderPriceMinTickSize": 0.01},
    )

    # Static path: 3c captured spread * 0.5 confidence = 1.5 (already
    # covered above). With a real activity score of 1.0 instead: 3.0.
    assert _expected_value_score(scored, settings, {"m1": 1.0}) == pytest.approx(3.0)


def test_select_target_markets_threads_activity_scores_into_ranking(monkeypatch):
    # m1 has a weaker static proxy than m2, but a real activity score
    # strong enough to outrank it -- confirms activity_scores actually
    # reaches the sort, not just the standalone scoring functions. m2's
    # volume/liquidity is deliberately mid-range (NOT saturating its static
    # confidence to 1.0), so this is a clear win on the primary EV key, not
    # a tie falling through to a secondary depth-proxy tiebreaker.
    settings = _settings(fill_confidence_reference_depth=5000.0, min_fill_confidence=0.1)
    weak_static_strong_activity = _scored(
        market_id="m1", spread=0.05, volume_24h=0.0, liquidity=0.0,
        raw={"orderPriceMinTickSize": 0.01},
    )
    strong_static = _scored(
        market_id="m2", spread=0.05, volume_24h=2000.0, liquidity=3000.0,
        raw={"orderPriceMinTickSize": 0.01},
    )
    scanner = Mock()
    scanner.scan.return_value = [weak_static_strong_activity.market, strong_static.market]
    filters = Mock()
    filters.apply.return_value = ([weak_static_strong_activity.market, strong_static.market], [])
    scoring = Mock()
    scoring.score.side_effect = lambda m: (
        weak_static_strong_activity if m.market_id == "m1" else strong_static
    )

    result = select_target_markets(
        scanner=scanner, filters=filters, scoring=scoring, settings=settings,
        activity_scores={"m1": 1.0},
    )

    assert [s.market.market_id for s in result][0] == "m1"


def test_select_target_markets_expected_value_ranking_prefers_liquid_smaller_spread():
    wide_dead = _scored(
        market_id="wide-dead",
        spread=0.06,  # 4c captured, confidence floor 0.1 -> EV 0.4
        volume_24h=0.0,
        liquidity=0.0,
        raw={"createdAt": "2026-01-01T00:00:00Z", "orderPriceMinTickSize": 0.01},
    )
    liquid_smaller = _scored(
        market_id="liquid-smaller",
        spread=0.03,  # 1c captured, confidence 1.0 -> EV 1.0
        volume_24h=10000.0,
        liquidity=10000.0,
        raw={"createdAt": "2026-01-02T00:00:00Z", "orderPriceMinTickSize": 0.01},
    )

    scanner = Mock()
    scanner.scan.return_value = [wide_dead.market, liquid_smaller.market]
    filters = Mock()
    filters.apply.side_effect = lambda markets: (markets, [])
    scoring = Mock()
    scoring.score.side_effect = lambda market: (
        wide_dead if market.market_id == "wide-dead" else liquid_smaller
    )

    chosen = select_target_markets(
        scanner=scanner, filters=filters, scoring=scoring, settings=_settings(), max_targets=10
    )

    assert chosen == [liquid_smaller, wide_dead]


def test_select_target_markets_raw_spread_toggle_reproduces_old_order():
    wide_dead = _scored(
        market_id="wide-dead",
        spread=0.06,
        volume_24h=0.0,
        liquidity=0.0,
        raw={"createdAt": "2026-01-01T00:00:00Z", "orderPriceMinTickSize": 0.01},
    )
    liquid_smaller = _scored(
        market_id="liquid-smaller",
        spread=0.03,
        volume_24h=10000.0,
        liquidity=10000.0,
        raw={"createdAt": "2026-01-02T00:00:00Z", "orderPriceMinTickSize": 0.01},
    )

    scanner = Mock()
    scanner.scan.return_value = [wide_dead.market, liquid_smaller.market]
    filters = Mock()
    filters.apply.side_effect = lambda markets: (markets, [])
    scoring = Mock()
    scoring.score.side_effect = lambda market: (
        wide_dead if market.market_id == "wide-dead" else liquid_smaller
    )

    chosen = select_target_markets(
        scanner=scanner,
        filters=filters,
        scoring=scoring,
        settings=_settings(rank_by_expected_value=False),
        max_targets=10,
    )

    assert chosen == [wide_dead, liquid_smaller]


def test_select_target_markets_equal_ev_still_uses_existing_tiebreakers():
    older = _scored(
        market_id="older",
        spread=0.03,
        volume_24h=10000.0,
        liquidity=10000.0,
        raw={"createdAt": "2026-01-01T00:00:00Z", "orderPriceMinTickSize": 0.01},
    )
    newer = _scored(
        market_id="newer",
        spread=0.03,
        volume_24h=10000.0,
        liquidity=10000.0,
        raw={"createdAt": "2026-01-02T00:00:00Z", "orderPriceMinTickSize": 0.01},
    )

    scanner = Mock()
    scanner.scan.return_value = [older.market, newer.market]
    filters = Mock()
    filters.apply.side_effect = lambda markets: (markets, [])
    scoring = Mock()
    scoring.score.side_effect = lambda market: older if market.market_id == "older" else newer

    chosen = select_target_markets(
        scanner=scanner, filters=filters, scoring=scoring, settings=_settings(), max_targets=10
    )

    assert chosen == [newer, older]


# ---------------------------------------------------------------------
# select_target_market's log line: settings=None must not crash, and
# ev_proxy must not be logged when it wasn't the actual ranking driver.
# ---------------------------------------------------------------------

def _mock_pipeline(scored: ScoredMarket):
    scanner = Mock()
    scanner.scan.return_value = ["m1"]
    filters = Mock()
    filters.apply.return_value = (["m1"], [])
    scoring = Mock()
    scoring.score.return_value = scored
    return scanner, filters, scoring


def test_select_target_market_does_not_crash_when_settings_omitted(monkeypatch):
    # select_target_markets() resolving its OWN local `settings` fallback
    # does not propagate back to select_target_market's `settings` variable
    # -- select_target_market must resolve its own, or the log line below
    # crashes on None.fill_confidence_reference_depth right after a market
    # was already successfully chosen.
    monkeypatch.setattr(
        "polymarket_bot.config.load_settings",
        lambda: config.Settings(live=_settings()),
    )
    scored = _scored(spread=0.05, volume_24h=5000.0, liquidity=5000.0, raw={"orderPriceMinTickSize": 0.01})
    scanner, filters, scoring = _mock_pipeline(scored)

    chosen = select_target_market(scanner=scanner, filters=filters, scoring=scoring)  # no settings!

    assert chosen is scored


def test_select_target_market_log_omits_ev_proxy_when_ranking_toggle_off(caplog):
    scored = _scored(spread=0.05, volume_24h=5000.0, liquidity=5000.0, raw={"orderPriceMinTickSize": 0.01})
    scanner, filters, scoring = _mock_pipeline(scored)

    with caplog.at_level("INFO"):
        chosen = select_target_market(
            scanner=scanner, filters=filters, scoring=scoring,
            settings=_settings(rank_by_expected_value=False),
        )

    assert chosen is scored
    selected_logs = [r.message for r in caplog.records if "Selected market for live quoting" in r.message]
    assert len(selected_logs) == 1
    assert "ev_proxy" not in selected_logs[0]


def test_select_target_market_log_includes_ev_proxy_when_ranking_enabled(caplog):
    scored = _scored(spread=0.05, volume_24h=5000.0, liquidity=5000.0, raw={"orderPriceMinTickSize": 0.01})
    scanner, filters, scoring = _mock_pipeline(scored)

    with caplog.at_level("INFO"):
        chosen = select_target_market(
            scanner=scanner, filters=filters, scoring=scoring,
            settings=_settings(rank_by_expected_value=True),
        )

    assert chosen is scored
    selected_logs = [r.message for r in caplog.records if "Selected market for live quoting" in r.message]
    assert len(selected_logs) == 1
    assert "ev_proxy" in selected_logs[0]


class TestDiversifyByEvent:
    def test_caps_markets_per_bucket_preferring_best_ranked_first(self):
        # Already rank-sorted (best first), 3 in the same event bucket.
        high = _scored(market_id="typ-ev-2026-01-01-a")
        mid = _scored(market_id="typ-ev-2026-01-01-b")
        low = _scored(market_id="typ-ev-2026-01-01-c")
        other = _scored(market_id="other-ev-2026-02-02-x")
        tradable = [high, mid, low, other]

        result = _diversify_by_event(tradable, max_targets=None, max_per_event=2)

        assert result == [high, mid, other]  # low dropped, other (distinct bucket) kept

    def test_distinct_buckets_all_kept(self):
        a = _scored(market_id="typ-ev-2026-01-01-a")
        b = _scored(market_id="typ-ev-2026-02-02-b")
        c = _scored(market_id="typ-ev-2026-03-03-c")
        result = _diversify_by_event([a, b, c], max_targets=None, max_per_event=2)
        assert result == [a, b, c]

    def test_max_targets_still_applies_on_top_of_the_per_event_cap(self):
        a = _scored(market_id="typ-ev-2026-01-01-a")
        b = _scored(market_id="typ-ev-2026-02-02-b")
        c = _scored(market_id="typ-ev-2026-03-03-c")
        result = _diversify_by_event([a, b, c], max_targets=2, max_per_event=2)
        assert result == [a, b]

    def test_singleton_slug_fixtures_unaffected_by_default_cap(self):
        # Existing test fixtures throughout this file use plain slugs like
        # "m1"/"wide-dead"/"older"/"newer" with no hyphen-date pattern --
        # confirm the default max_markets_per_event=2 doesn't drop any of
        # them (each becomes its own singleton bucket).
        m1 = _scored(market_id="m1")
        wide_dead = _scored(market_id="wide-dead")
        older = _scored(market_id="older")
        newer = _scored(market_id="newer")
        result = _diversify_by_event([m1, wide_dead, older, newer], max_targets=None, max_per_event=2)
        assert result == [m1, wide_dead, older, newer]


def test_select_target_markets_limits_candidates_per_event_bucket():
    # 3 Portugal-Spain-style corner-prop markets (same event bucket) plus 1
    # distinct-event market -- only the top max_markets_per_event=2 from the
    # shared bucket should survive, best-ranked-first, plus the distinct one.
    high = _scored(market_id="astatc-fwc-por-esp-2026-07-06-cor-all-gt6pt5", score=95.0, spread=0.06, volume_24h=5000.0, liquidity=5000.0, raw={"orderPriceMinTickSize": 0.01})
    mid = _scored(market_id="astatc-fwc-por-esp-2026-07-06-cor-team-esp-gt4pt5", score=90.0, spread=0.06, volume_24h=5000.0, liquidity=5000.0, raw={"orderPriceMinTickSize": 0.01})
    low = _scored(market_id="atc-fwc-por-esp-2026-07-06-exact-score-other", score=80.0, spread=0.06, volume_24h=5000.0, liquidity=5000.0, raw={"orderPriceMinTickSize": 0.01})
    distinct = _scored(market_id="astatc-fwc-usa-bel-2026-07-06-cor-team-usa-gt5pt5", score=99.0, spread=0.06, volume_24h=5000.0, liquidity=5000.0, raw={"orderPriceMinTickSize": 0.01})

    scanner = Mock()
    scanner.scan.return_value = [high.market, mid.market, low.market, distinct.market]
    filters = Mock()
    filters.apply.side_effect = lambda markets: (markets, [])
    by_id = {s.market_id: s for s in (high, mid, low, distinct)}
    scoring = Mock()
    scoring.score.side_effect = lambda market: by_id[market.market_id]

    result = select_target_markets(
        scanner=scanner, filters=filters, scoring=scoring, settings=_settings(), max_targets=10,
    )

    assert distinct in result
    por_esp_kept = [s for s in result if s in (high, mid, low)]
    assert len(por_esp_kept) == 2
    assert low not in por_esp_kept  # lowest EV score in its bucket, correctly dropped
