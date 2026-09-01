from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live import circuit_breaker as cb_module
from polymarket_bot.live.circuit_breaker import CircuitBreaker, SessionCircuitBreaker
from polymarket_bot.live.cancel_safeguard import EmergencySafeguardFailedError


def _client(open_orders=None):
    client = Mock()
    client.get_open_orders.return_value = [] if open_orders is None else open_orders
    return client


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cb_module, "STATE_FILE", tmp_path / "circuit_breaker_state.json")


def _settings(enabled=True, limit=100.0):
    return config.CircuitBreakerSettings(enabled=enabled, daily_loss_limit_usd=limit)


def test_disabled_breaker_never_halts(isolated_state):
    breaker = CircuitBreaker(_settings(enabled=False))
    client = _client()
    assert breaker.evaluate(total_pnl_usd=-1000, client=client) is False
    client.cancel_all.assert_not_called()


def test_no_trip_under_threshold(isolated_state):
    breaker = CircuitBreaker(_settings(limit=100.0))
    client = _client()
    assert breaker.evaluate(total_pnl_usd=-50, client=client) is False
    client.cancel_all.assert_not_called()
    assert not breaker.is_halted()


def test_trips_at_threshold_and_cancels_all(isolated_state):
    breaker = CircuitBreaker(_settings(limit=100.0))
    client = _client()
    assert breaker.evaluate(total_pnl_usd=-100, client=client) is True
    client.cancel_all.assert_called_once()
    assert breaker.is_halted()


def test_stays_halted_across_new_instances_until_reset(isolated_state):
    breaker = CircuitBreaker(_settings(limit=100.0))
    breaker.evaluate(total_pnl_usd=-200, client=_client())

    fresh_breaker = CircuitBreaker(_settings(limit=100.0))
    assert fresh_breaker.is_halted()
    # Even a wildly positive P/L shouldn't un-halt without an explicit reset.
    assert fresh_breaker.evaluate(total_pnl_usd=500, client=_client()) is True


def test_reset_clears_halt(isolated_state):
    breaker = CircuitBreaker(_settings(limit=100.0))
    breaker.evaluate(total_pnl_usd=-200, client=_client())
    assert breaker.is_halted()

    breaker.reset()
    assert not breaker.is_halted()
    assert breaker.evaluate(total_pnl_usd=-50, client=_client()) is False


def test_reset_grants_fresh_loss_allowance_from_trip_pnl(isolated_state):
    breaker = CircuitBreaker(_settings(limit=100.0))
    breaker.evaluate(total_pnl_usd=-200, client=_client())

    breaker.reset()

    assert breaker.evaluate(total_pnl_usd=-299.99, client=_client()) is False
    client = _client()
    assert breaker.evaluate(total_pnl_usd=-300.0, client=client) is True
    client.cancel_all.assert_called_once()


def test_reset_baseline_is_ignored_on_a_later_utc_day(isolated_state):
    cb_module.storage.save_json(
        cb_module.STATE_FILE,
        {
            "halted": False,
            "reset_baseline_date": "1900-01-01",
            "reset_baseline_pnl_usd": -1000.0,
        },
    )
    client = _client()

    assert CircuitBreaker(_settings(limit=100.0)).evaluate(-100.0, client) is True
    client.cancel_all.assert_called_once()


def test_reset_does_not_carry_an_old_trip_into_a_new_utc_day(isolated_state):
    cb_module.storage.save_json(
        cb_module.STATE_FILE,
        {
            "halted": True,
            "tripped_at": "1900-01-01T12:00:00+00:00",
            "pnl_at_trip_usd": -1000.0,
        },
    )
    breaker = CircuitBreaker(_settings(limit=100.0))

    breaker.reset()

    state = cb_module.storage.load_json(cb_module.STATE_FILE, default={})
    assert state["reset_baseline_pnl_usd"] == 0.0
    assert breaker.evaluate(-99.99, _client()) is False


def test_trip_persists_halt_but_raises_when_cancel_cannot_be_verified(isolated_state):
    breaker = CircuitBreaker(_settings(limit=100.0))
    client = _client([{"id": "still-open", "marketSlug": "m1"}])

    with pytest.raises(EmergencySafeguardFailedError, match="remain open"):
        breaker.evaluate(total_pnl_usd=-100.0, client=client)

    assert breaker.is_halted()
    client.cancel_all.assert_called_once()


def _session_settings(enabled=True, limit=100.0):
    return config.SessionCircuitBreakerSettings(enabled=enabled, loss_limit_usd=limit)


class TestSessionCircuitBreaker:
    # No isolated_state fixture needed -- deliberately in-memory only, no
    # state file, no cross-test contamination risk.

    def test_disabled_breaker_never_halts(self):
        breaker = SessionCircuitBreaker(_session_settings(enabled=False))
        client = _client()
        assert breaker.evaluate(total_pnl_usd=-1000, client=client) is False
        client.cancel_all.assert_not_called()

    def test_no_trip_under_threshold(self):
        breaker = SessionCircuitBreaker(_session_settings(limit=100.0))
        client = _client()
        assert breaker.evaluate(total_pnl_usd=-50, client=client) is False
        client.cancel_all.assert_not_called()
        assert not breaker.is_halted()

    def test_trips_at_threshold_and_cancels_all(self):
        breaker = SessionCircuitBreaker(_session_settings(limit=100.0))
        client = _client()
        assert breaker.evaluate(total_pnl_usd=-100, client=client) is True
        client.cancel_all.assert_called_once()
        assert breaker.is_halted()

    def test_stays_halted_until_reset(self):
        breaker = SessionCircuitBreaker(_session_settings(limit=100.0))
        breaker.evaluate(total_pnl_usd=-200, client=_client())
        assert breaker.is_halted()
        # Even a wildly positive P/L shouldn't un-halt without an explicit reset.
        assert breaker.evaluate(total_pnl_usd=500, client=_client()) is True

    def test_reset_clears_halt(self):
        breaker = SessionCircuitBreaker(_session_settings(limit=100.0))
        breaker.evaluate(total_pnl_usd=-200, client=_client())
        assert breaker.is_halted()

        breaker.reset()
        assert not breaker.is_halted()
        assert breaker.evaluate(total_pnl_usd=-50, client=_client()) is False

    def test_diff_first_call_captures_baseline_and_returns_zero(self):
        # Sign of the raw baseline doesn't matter -- only movement from here.
        assert SessionCircuitBreaker(_session_settings()).diff(37.5) == 0.0
        assert SessionCircuitBreaker(_session_settings()).diff(-1000.0) == 0.0

    def test_diff_later_calls_diff_against_captured_baseline(self):
        breaker = SessionCircuitBreaker(_session_settings())
        breaker.diff(10.0)  # baseline captured here
        assert breaker.diff(4.0) == -6.0
        assert breaker.diff(15.0) == 5.0

    def test_two_independent_instances_do_not_share_state(self):
        # No shared file -- confirms genuine in-memory isolation, unlike
        # CircuitBreaker's module-level STATE_FILE.
        first = SessionCircuitBreaker(_session_settings(limit=100.0))
        second = SessionCircuitBreaker(_session_settings(limit=100.0))
        first.evaluate(total_pnl_usd=-200, client=_client())
        assert first.is_halted()
        assert not second.is_halted()
