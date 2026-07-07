from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live import circuit_breaker as cb_module
from polymarket_bot.live.circuit_breaker import CircuitBreaker


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cb_module, "STATE_FILE", tmp_path / "circuit_breaker_state.json")


def _settings(enabled=True, limit=100.0):
    return config.CircuitBreakerSettings(enabled=enabled, daily_loss_limit_usd=limit)


def test_disabled_breaker_never_halts(isolated_state):
    breaker = CircuitBreaker(_settings(enabled=False))
    client = Mock()
    assert breaker.evaluate(total_pnl_usd=-1000, client=client) is False
    client.cancel_all.assert_not_called()


def test_no_trip_under_threshold(isolated_state):
    breaker = CircuitBreaker(_settings(limit=100.0))
    client = Mock()
    assert breaker.evaluate(total_pnl_usd=-50, client=client) is False
    client.cancel_all.assert_not_called()
    assert not breaker.is_halted()


def test_trips_at_threshold_and_cancels_all(isolated_state):
    breaker = CircuitBreaker(_settings(limit=100.0))
    client = Mock()
    assert breaker.evaluate(total_pnl_usd=-100, client=client) is True
    client.cancel_all.assert_called_once()
    assert breaker.is_halted()


def test_stays_halted_across_new_instances_until_reset(isolated_state):
    breaker = CircuitBreaker(_settings(limit=100.0))
    breaker.evaluate(total_pnl_usd=-200, client=Mock())

    fresh_breaker = CircuitBreaker(_settings(limit=100.0))
    assert fresh_breaker.is_halted()
    # Even a wildly positive P/L shouldn't un-halt without an explicit reset.
    assert fresh_breaker.evaluate(total_pnl_usd=500, client=Mock()) is True


def test_reset_clears_halt(isolated_state):
    breaker = CircuitBreaker(_settings(limit=100.0))
    breaker.evaluate(total_pnl_usd=-200, client=Mock())
    assert breaker.is_halted()

    breaker.reset()
    assert not breaker.is_halted()
    assert breaker.evaluate(total_pnl_usd=-50, client=Mock()) is False
