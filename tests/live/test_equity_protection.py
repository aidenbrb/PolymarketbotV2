from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live import equity_protection as ep_module
from polymarket_bot.live.equity_protection import EquityProtection


def _client(open_orders=None):
    client = Mock()
    client.get_open_orders.return_value = [] if open_orders is None else open_orders
    return client


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ep_module, "STATE_FILE", tmp_path / "equity_protection_state.json")


def _settings(
    enabled=True,
    starting_capital_usd=0.0,
    drawdown_from_peak_pct=20.0,
    profit_lock_daily_usd=40.0,
    profit_lock_size_multiplier=0.5,
):
    return config.EquityProtectionSettings(
        enabled=enabled,
        starting_capital_usd=starting_capital_usd,
        drawdown_from_peak_pct=drawdown_from_peak_pct,
        profit_lock_daily_usd=profit_lock_daily_usd,
        profit_lock_size_multiplier=profit_lock_size_multiplier,
    )


def _mock_total_pnl(monkeypatch, value):
    monkeypatch.setattr(ep_module, "get_total_position_pnl_usd", Mock(return_value=value))


class TestDisabledAndHalted:
    def test_disabled_never_halts_no_size_change_no_client_calls(self, isolated_state):
        protection = EquityProtection(_settings(enabled=False))
        client = _client()
        halted, multiplier = protection.evaluate(total_pnl_usd=-1000, client=client)
        assert (halted, multiplier) == (False, 1.0)
        client.get_all_positions.assert_not_called()
        client.cancel_all.assert_not_called()

    def test_already_halted_reasserts_cancel_safeguard(self, isolated_state, monkeypatch):
        protection = EquityProtection(_settings(starting_capital_usd=100.0))
        _mock_total_pnl(monkeypatch, -1000.0)
        protection.evaluate(total_pnl_usd=0.0, client=_client())  # trips and halts
        assert protection.is_halted()

        client = _client()
        halted, multiplier = protection.evaluate(total_pnl_usd=500.0, client=client)
        assert (halted, multiplier) == (True, 1.0)
        client.get_all_positions.assert_not_called()
        client.cancel_all.assert_called_once()


class TestStartingCapitalUnset:
    def test_skips_drawdown_check_when_starting_capital_not_set(self, isolated_state, monkeypatch):
        protection = EquityProtection(_settings(starting_capital_usd=0.0))
        pnl_mock = Mock(return_value=-999999.0)
        monkeypatch.setattr(ep_module, "get_total_position_pnl_usd", pnl_mock)

        halted, _ = protection.evaluate(total_pnl_usd=0.0, client=_client())

        assert halted is False
        pnl_mock.assert_not_called()

    def test_still_applies_size_multiplier_when_starting_capital_unset(self, isolated_state):
        protection = EquityProtection(_settings(starting_capital_usd=0.0, profit_lock_daily_usd=40.0))
        _halted, multiplier = protection.evaluate(total_pnl_usd=50.0, client=_client())
        assert multiplier == 0.5

    def test_warns_only_once_across_multiple_calls(self, isolated_state, caplog):
        protection = EquityProtection(_settings(starting_capital_usd=0.0))
        with caplog.at_level("WARNING"):
            protection.evaluate(total_pnl_usd=0.0, client=_client())
            protection.evaluate(total_pnl_usd=0.0, client=_client())
        warnings = [r for r in caplog.records if "INACTIVE" in r.message]
        assert len(warnings) == 1


class TestDrawdownFromPeak:
    def test_first_call_bootstraps_peak_without_tripping(self, isolated_state, monkeypatch):
        protection = EquityProtection(_settings(starting_capital_usd=100.0))
        _mock_total_pnl(monkeypatch, 0.0)  # account_value = 100
        halted, _ = protection.evaluate(total_pnl_usd=0.0, client=_client())
        assert halted is False

    def test_peak_monotonically_increases(self, isolated_state, monkeypatch):
        protection = EquityProtection(_settings(starting_capital_usd=100.0))
        _mock_total_pnl(monkeypatch, 50.0)  # account_value = 150 (new peak)
        protection.evaluate(total_pnl_usd=0.0, client=_client())
        # A drop to 125 is a ~16.7% drawdown from the 150 peak -- under the
        # 20% threshold, so must NOT trip. If the peak had incorrectly been
        # overwritten down to a lower running value instead of staying at
        # its historical max, this would look like a smaller/no drawdown at
        # all, which would also pass -- the real proof is the persisted
        # peak value asserted below.
        _mock_total_pnl(monkeypatch, 25.0)  # account_value = 125
        halted, _ = protection.evaluate(total_pnl_usd=0.0, client=_client())
        assert halted is False
        state = ep_module.storage.load_json(ep_module.STATE_FILE)
        assert state["peak_account_value_usd"] == pytest.approx(150.0)

    def test_trips_exactly_at_threshold_and_cancels_all(self, isolated_state, monkeypatch):
        protection = EquityProtection(_settings(starting_capital_usd=100.0, drawdown_from_peak_pct=20.0))
        _mock_total_pnl(monkeypatch, 0.0)  # peak = 100
        protection.evaluate(total_pnl_usd=0.0, client=_client())
        _mock_total_pnl(monkeypatch, -20.0)  # account_value = 80 = 100 * (1 - 0.20)
        client = _client()
        halted, multiplier = protection.evaluate(total_pnl_usd=0.0, client=client)
        assert (halted, multiplier) == (True, 1.0)
        client.cancel_all.assert_called_once()

    def test_does_not_trip_just_above_threshold(self, isolated_state, monkeypatch):
        protection = EquityProtection(_settings(starting_capital_usd=100.0, drawdown_from_peak_pct=20.0))
        _mock_total_pnl(monkeypatch, 0.0)  # peak = 100
        protection.evaluate(total_pnl_usd=0.0, client=_client())
        _mock_total_pnl(monkeypatch, -19.0)  # account_value = 81, above the 80 threshold
        client = _client()
        halted, _ = protection.evaluate(total_pnl_usd=0.0, client=client)
        assert halted is False
        client.cancel_all.assert_not_called()

    def test_trips_immediately_when_account_value_non_positive(self, isolated_state, monkeypatch):
        protection = EquityProtection(_settings(starting_capital_usd=100.0, drawdown_from_peak_pct=20.0))
        _mock_total_pnl(monkeypatch, -150.0)  # account_value = -50
        client = _client()
        halted, _ = protection.evaluate(total_pnl_usd=0.0, client=client)
        assert halted is True
        client.cancel_all.assert_called_once()

    def test_stays_halted_across_new_instances_until_reset(self, isolated_state, monkeypatch):
        protection = EquityProtection(_settings(starting_capital_usd=100.0))
        _mock_total_pnl(monkeypatch, -1000.0)
        protection.evaluate(total_pnl_usd=0.0, client=_client())

        fresh = EquityProtection(_settings(starting_capital_usd=100.0))
        assert fresh.is_halted()
        halted, _ = fresh.evaluate(total_pnl_usd=999.0, client=_client())
        assert halted is True

    def test_reset_clears_halt_but_preserves_peak(self, isolated_state, monkeypatch):
        protection = EquityProtection(_settings(starting_capital_usd=100.0))
        _mock_total_pnl(monkeypatch, 50.0)  # peak = 150
        protection.evaluate(total_pnl_usd=0.0, client=_client())
        _mock_total_pnl(monkeypatch, -1000.0)  # trips
        protection.evaluate(total_pnl_usd=0.0, client=_client())
        assert protection.is_halted()

        protection.reset()
        assert not protection.is_halted()

        # A subsequent evaluate() must measure drawdown against the
        # PRESERVED peak (150), not re-bootstrap from whatever comes next.
        _mock_total_pnl(monkeypatch, -1000.0)  # still deep underwater vs peak=150
        halted, _ = protection.evaluate(total_pnl_usd=0.0, client=_client())
        assert halted is True

    def test_positions_fetch_failure_fails_closed(self, isolated_state, monkeypatch, caplog):
        protection = EquityProtection(_settings(starting_capital_usd=100.0))
        _mock_total_pnl(monkeypatch, None)  # simulates UsApiError inside get_total_position_pnl_usd
        client = _client()
        with caplog.at_level("ERROR"):
            halted, _ = protection.evaluate(total_pnl_usd=0.0, client=client)
        assert halted is True
        client.cancel_all.assert_called_once()
        assert any("drawdown check" in r.message for r in caplog.records)

    def test_supplied_lifetime_pnl_avoids_duplicate_position_fetch(self, isolated_state, monkeypatch):
        protection = EquityProtection(_settings(starting_capital_usd=100.0))
        pnl_fetch = Mock(side_effect=AssertionError("duplicate REST fetch"))
        monkeypatch.setattr(ep_module, "get_total_position_pnl_usd", pnl_fetch)

        halted, _ = protection.evaluate(
            total_pnl_usd=0.0,
            lifetime_pnl_usd=5.0,
            client=_client(),
        )

        assert halted is False
        pnl_fetch.assert_not_called()


class TestProfitLockRatchet:
    def test_engages_at_or_above_threshold(self, isolated_state):
        protection = EquityProtection(_settings(profit_lock_daily_usd=40.0))
        _halted, multiplier = protection.evaluate(total_pnl_usd=40.0, client=_client())
        assert multiplier == 0.5

    def test_not_engaged_below_threshold(self, isolated_state):
        protection = EquityProtection(_settings(profit_lock_daily_usd=40.0))
        _halted, multiplier = protection.evaluate(total_pnl_usd=39.99, client=_client())
        assert multiplier == 1.0

    def test_stays_engaged_for_rest_of_day_even_if_pnl_drops_back(self, isolated_state):
        protection = EquityProtection(_settings(profit_lock_daily_usd=40.0))
        protection.evaluate(total_pnl_usd=45.0, client=_client())  # engages
        _halted, multiplier = protection.evaluate(total_pnl_usd=5.0, client=_client())  # dips back down
        assert multiplier == 0.5  # still engaged -- ratchet, not reactive

    def test_resets_on_a_new_utc_day(self, isolated_state, monkeypatch):
        protection = EquityProtection(_settings(profit_lock_daily_usd=40.0))
        protection.evaluate(total_pnl_usd=45.0, client=_client())  # engages "today"

        # Simulate a new UTC day by monkeypatching datetime inside the module.
        import datetime as real_datetime

        class _FakeDatetime(real_datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime.datetime(2099, 1, 1, tzinfo=tz)

        monkeypatch.setattr(ep_module, "datetime", _FakeDatetime)
        _halted, multiplier = protection.evaluate(total_pnl_usd=5.0, client=_client())
        assert multiplier == 1.0


class TestDayVsLifetimeSplit:
    def test_size_check_uses_daily_pnl_even_when_lifetime_pnl_differs(self, isolated_state, monkeypatch):
        # Daily P/L (total_pnl_usd) is high enough to engage the ratchet,
        # while the lifetime figure (get_total_position_pnl_usd) differs and
        # is deliberately kept from tripping the (separate) drawdown check --
        # the size check must key off the daily figure only.
        protection = EquityProtection(_settings(starting_capital_usd=100.0, profit_lock_daily_usd=40.0))
        _mock_total_pnl(monkeypatch, 0.0)  # lifetime P/L differs from the daily 45.0 below
        halted, multiplier = protection.evaluate(total_pnl_usd=45.0, client=_client())
        assert halted is False
        assert multiplier == 0.5

    def test_drawdown_check_uses_lifetime_pnl_even_when_daily_pnl_differs(self, isolated_state, monkeypatch):
        # Daily P/L looks fine (small positive), but the lifetime figure
        # shows a real drawdown from a prior peak -- the drawdown check must
        # key off the lifetime figure, not the daily one.
        protection = EquityProtection(_settings(starting_capital_usd=100.0, drawdown_from_peak_pct=20.0))
        _mock_total_pnl(monkeypatch, 100.0)  # peak account_value = 200
        protection.evaluate(total_pnl_usd=5.0, client=_client())
        _mock_total_pnl(monkeypatch, -50.0)  # account_value = 50, well under 200*0.8=160
        halted, _ = protection.evaluate(total_pnl_usd=5.0, client=_client())
        assert halted is True
