from polymarket_bot.live.family_performance import classify_family, compute_family_performance


def test_classify_family_composite_half_exact_score_takes_priority_over_plain_exact():
    assert classify_family("atc-fwc-arg-egy-2026-07-07-fh-exact-score-1-0") == "half_exact_score_prop"
    assert classify_family("atc-fwc-sui-col-2026-07-07-fh-exact-score-0-0") == "half_exact_score_prop"


def test_classify_family_plain_exact_score_without_half_qualifier():
    assert classify_family("atc-fwc-por-esp-2026-07-06-exact-score-other") == "exact_score_prop"


def test_classify_family_corners():
    assert classify_family("astatc-fwc-por-esp-2026-07-06-cor-all-gt10pt5") == "corners"
    assert classify_family("astatc-fwc-por-esp-2026-07-06-cor-team-esp-gt4pt5") == "corners"


def test_classify_family_home_run_prop():
    assert classify_family("astatc-mlb-az-sd-2026-07-06-hr-ketmar-gte1") == "home_run_prop"


def test_classify_family_first_inning_run_prop():
    assert classify_family("astatc-mlb-tor-sf-2026-07-06-yrfi") == "first_inning_run_prop"
    assert classify_family("astatc-mlb-tor-sf-2026-07-06-nrfi") == "first_inning_run_prop"


def test_classify_family_first_five_innings():
    assert classify_family("atc-mlb-az-sd-2026-07-06-f5-az") == "first_five_innings"


def test_classify_family_time_to_goal_prop():
    assert classify_family("tsc-fwc-arg-egy-2026-07-07-ttg-fh-arg-1pt5") == "time_to_goal_prop"
    assert classify_family("tsc-fwc-mex-eng-2026-07-05-ttg-eng-3pt5") == "time_to_goal_prop"


def test_classify_family_team_to_score_prop():
    assert classify_family("astatc-fwc-por-esp-2026-07-06-fh-btts") == "team_to_score_prop"
    assert classify_family("astatc-fwc-mex-eng-2026-07-05-sh-btts") == "team_to_score_prop"
    assert classify_family("astatc-fwc-por-esp-2026-07-06-fh-ftts-esp") == "team_to_score_prop"


def test_classify_family_weather():
    assert classify_family("tc-temp-miahigh-2026-07-06-gte91lt92f") == "weather"


def test_classify_family_award_and_championship_futures():
    assert classify_family("tec-mlb-al-2026-11-27-cy-dylcea") == "award_futures"
    assert classify_family("tec-mlb-nl-2026-11-27-mvp-shooht") == "award_futures"
    assert classify_family("tec-mlb-champ-2026-09-27-nyy") == "championship_futures"


def test_classify_family_qualifier():
    assert classify_family("aqc-fifa-wc-2026-07-07-quarterq-colbia") == "qualifier"
    assert classify_family("aqc-fifa-wc-2026-07-19-stgelim-col-sf") == "qualifier"


def test_classify_family_futures_winner():
    assert classify_family("tec-pga-genescot-2026-07-12-w-rormci") == "futures_winner"
    assert classify_family("tec-mlb-alwest-2026-09-27-w-tex") == "futures_winner"


def test_classify_family_falls_back_to_prefix_token_when_no_keyword_matches():
    assert classify_family("atc-dota2-lgd-vp-2026-07-07-lgd") == "atc"
    assert classify_family("atc-ucl-var-kps-2026-07-07-var") == "atc"


def test_classify_family_empty_or_unparseable_slug_falls_back_to_other():
    assert classify_family("") == "other"
    assert classify_family(None) == "other"


def _fill(slug, markout_1m=None, markout_5m=None, markout_15m=None, quality=None):
    return {
        "market_slug": slug,
        "markout_1m_cents": markout_1m,
        "markout_5m_cents": markout_5m,
        "markout_15m_cents": markout_15m,
        "fill_quality": quality,
    }


def test_compute_family_performance_empty_input():
    assert compute_family_performance([]) == []


def test_compute_family_performance_groups_and_aggregates():
    fills = [
        _fill(
            "astatc-fwc-por-esp-2026-07-06-cor-all-gt10pt5",
            markout_1m=1.0, markout_5m=2.0, markout_15m=3.0, quality="favorable",
        ),
        _fill(
            "astatc-fwc-por-esp-2026-07-06-cor-all-gt11pt5",
            markout_1m=-3.0, markout_5m=-1.0, markout_15m=-5.0, quality="adverse",
        ),
        _fill("astatc-mlb-az-sd-2026-07-06-hr-ketmar-gte1", markout_1m=0.0, markout_5m=0.0, quality="neutral"),
    ]

    performances = {p.family: p for p in compute_family_performance(fills)}

    corners = performances["corners"]
    assert corners.fill_count == 2
    assert corners.avg_markout_1m_cents == -1.0  # mean(1.0, -3.0)
    assert corners.median_markout_1m_cents == -1.0
    assert corners.avg_markout_5m_cents == 0.5  # mean(2.0, -1.0)
    assert corners.avg_markout_15m_cents == -1.0  # mean(3.0, -5.0)
    assert corners.median_markout_15m_cents == -1.0
    assert corners.favorable_count == 1
    assert corners.adverse_count == 1
    assert corners.neutral_count == 0
    assert corners.unresolved_count == 0

    hr = performances["home_run_prop"]
    assert hr.fill_count == 1
    assert hr.neutral_count == 1
    assert hr.avg_markout_15m_cents is None  # never provided for this fill


def test_compute_family_performance_excludes_none_markout_from_average_but_counts_fill():
    fills = [
        _fill("astatc-mlb-az-sd-2026-07-06-hr-a-gte1", markout_1m=None, markout_5m=None, quality=None),
        _fill(
            "astatc-mlb-az-sd-2026-07-06-hr-b-gte1",
            markout_1m=4.0, markout_5m=None, markout_15m=6.0, quality="favorable",
        ),
    ]

    performances = {p.family: p for p in compute_family_performance(fills)}
    hr = performances["home_run_prop"]

    assert hr.fill_count == 2
    assert hr.avg_markout_1m_cents == 4.0  # only the non-None value counted
    assert hr.avg_markout_5m_cents is None  # both were None
    assert hr.avg_markout_15m_cents == 6.0  # only the non-None value counted
    assert hr.unresolved_count == 1  # the fill with quality=None
    assert hr.favorable_count == 1


def test_quality_counts_come_from_fixed_markout_not_legacy_fill_quality():
    fills = [
        _fill("astatc-mlb-az-sd-2026-07-06-hr-a-gte1", markout_1m=2.0, quality=None),
        _fill("astatc-mlb-az-sd-2026-07-06-hr-b-gte1", markout_1m=-2.0, quality="favorable"),
    ]

    performance = compute_family_performance(fills)[0]

    assert performance.favorable_count == 1
    assert performance.adverse_count == 1
    assert performance.unresolved_count == 0
