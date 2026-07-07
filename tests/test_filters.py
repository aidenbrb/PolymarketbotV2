from datetime import datetime, timedelta, timezone

from polymarket_bot import config
from polymarket_bot.filters import MarketFilters
from polymarket_bot.models import Market


def _make_market(**overrides) -> Market:
    defaults = dict(
        market_id="m1",
        event_id="e1",
        question="Will this clearly-worded event happen by the deadline?",
        category="politics",
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        token_ids=["t1"],
        volume_24h=5000.0,
        liquidity=10000.0,
        end_date=(datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
        closed=False,
        active=True,
        has_order_book=True,
        spread=0.02,
    )
    defaults.update(overrides)
    return Market(**defaults)


def _settings() -> config.FilterSettings:
    return config.FilterSettings()


def test_accepts_healthy_market():
    market = _make_market()
    accepted, results = MarketFilters(_settings()).apply([market])
    assert len(accepted) == 1
    assert results[0].accepted


def test_rejects_low_liquidity():
    market = _make_market(liquidity=100.0)
    accepted, results = MarketFilters(_settings()).apply([market])
    assert accepted == []
    assert any("Liquidity" in r for r in results[0].reasons)


def test_rejects_wide_spread():
    market = _make_market(spread=0.5)
    accepted, results = MarketFilters(_settings()).apply([market])
    assert accepted == []
    assert any("Spread" in r for r in results[0].reasons)


def test_rejects_near_expired_market():
    market = _make_market(
        end_date=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    )
    accepted, results = MarketFilters(_settings()).apply([market])
    assert accepted == []
    assert any("Closes in" in r for r in results[0].reasons)


def test_rejects_closed_market():
    market = _make_market(closed=True)
    accepted, results = MarketFilters(_settings()).apply([market])
    assert accepted == []
    assert "Market is closed" in results[0].reasons


def test_rejects_missing_order_book():
    market = _make_market(has_order_book=False)
    accepted, results = MarketFilters(_settings()).apply([market])
    assert accepted == []
    assert any("order book" in r for r in results[0].reasons)


def test_rejects_extreme_price():
    market = _make_market(outcome_prices=[0.99, 0.01])
    accepted, results = MarketFilters(_settings()).apply([market])
    assert accepted == []
    assert any("Outcome price" in r for r in results[0].reasons)


def test_rejects_unclear_wording():
    market = _make_market(question="TBD market for testing")
    accepted, results = MarketFilters(_settings()).apply([market])
    assert accepted == []
    assert any("unclear-wording" in r for r in results[0].reasons)
