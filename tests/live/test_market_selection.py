from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live.market_selection import (
    _captured_spread_cents,
    _diversify_by_event,
    _expected_value_score,
    _fill_confidence,
    is_eligible,
    select_target_market,
    select_target_markets,
)
from polymarket_bot.models import Market, ScoredMarket


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
        raw=raw or {},
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
    )
    defaults.update(overrides)
    return config.LiveTradingSettings(**defaults)


def test_is_eligible_accepts_top_tier_market():
    assert is_eligible(_scored(), _settings())


def test_is_eligible_rejects_below_min_tier():
    assert not is_eligible(_scored(recommendation="STRONG_WATCH"), _settings())


def test_is_eligible_accepts_above_min_tier_when_min_lowered():
    settings = _settings(min_score_recommendation="STRONG_WATCH")
    assert is_eligible(_scored(recommendation="PAPER_CANDIDATE"), settings)
    assert is_eligible(_scored(recommendation="STRONG_WATCH"), settings)


def test_is_eligible_rejects_excluded_category():
    assert not is_eligible(_scored(category="sports"), _settings())


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
    filters.apply.return_value = ([eligible.market], [filtered_out.market])
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
