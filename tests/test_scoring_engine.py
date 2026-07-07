from datetime import datetime, timedelta, timezone

from polymarket_bot.models import Market
from polymarket_bot.scoring_engine import ScoringEngine


def _make_market(**overrides) -> Market:
    defaults = dict(
        market_id="m1",
        event_id="e1",
        question="Will this clearly-worded event happen by the deadline?",
        category="politics",
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        token_ids=["t1"],
        volume_24h=15000.0,
        liquidity=40000.0,
        end_date=(datetime.now(timezone.utc) + timedelta(days=20)).isoformat(),
        closed=False,
        active=True,
        has_order_book=True,
        spread=0.01,
    )
    defaults.update(overrides)
    return Market(**defaults)


def test_score_is_within_0_to_100():
    market = _make_market()
    scored = ScoringEngine().score(market)
    assert 0 <= scored.total_score <= 100


def test_high_quality_market_is_paper_candidate():
    market = _make_market()
    scored = ScoringEngine().score(market)
    assert scored.recommendation == "PAPER_CANDIDATE"


def test_low_quality_market_is_rejected():
    market = _make_market(
        liquidity=0,
        volume_24h=0,
        spread=None,
        outcome_prices=[0.99, 0.01],
        end_date=None,
        category="crypto",
    )
    scored = ScoringEngine().score(market)
    assert scored.recommendation == "REJECT"
    assert scored.total_score < 40


def test_component_scores_sum_to_total():
    market = _make_market()
    scored = ScoringEngine().score(market)
    assert round(sum(scored.component_scores.values()), 2) == scored.total_score


def test_explanation_is_human_readable_list():
    market = _make_market()
    scored = ScoringEngine().score(market)
    assert isinstance(scored.explanation, list)
    assert all(isinstance(line, str) for line in scored.explanation)
    assert len(scored.explanation) == len(scored.component_scores)
