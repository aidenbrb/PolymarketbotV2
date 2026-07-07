from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live import instance_lock
from polymarket_bot.live.runner import LiveTradingBot


@pytest.fixture(autouse=True)
def _isolated_instance_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(instance_lock, "LOCK_FILE", tmp_path / "live_bot.lock")


def _bot(refresh_interval=0.02):
    client = Mock()
    market_maker = Mock()
    settings = config.LiveTradingSettings(
        refresh_interval_seconds=refresh_interval, order_shares_min=16.0, order_shares_max=16.0,
    )
    breaker = Mock()
    breaker.evaluate.return_value = False
    equity_protection = Mock()
    equity_protection.evaluate.return_value = (False, 1.0)
    bot = LiveTradingBot(
        client=client,
        market_maker=market_maker,
        circuit_breaker=breaker,
        equity_protection=equity_protection,
        settings=settings,
    )
    return bot, client, market_maker, breaker, equity_protection


def test_run_one_cycle_skips_refresh_when_halted(monkeypatch):
    bot, client, market_maker, breaker, _equity_protection = _bot()
    monkeypatch.setattr(
        "polymarket_bot.live.runner.estimate_daily_pnl_usd", lambda client: 0.0
    )
    breaker.evaluate.return_value = True
    bot._run_one_cycle()
    market_maker.refresh_quotes.assert_not_called()


def test_run_one_cycle_refreshes_when_not_halted(monkeypatch):
    bot, client, market_maker, breaker, _equity_protection = _bot()
    monkeypatch.setattr(
        "polymarket_bot.live.runner.estimate_daily_pnl_usd", lambda client: 0.0
    )
    bot._run_one_cycle()
    market_maker.refresh_quotes.assert_called_once()


def test_run_one_cycle_survives_refresh_failure(monkeypatch):
    bot, client, market_maker, breaker, _equity_protection = _bot()
    monkeypatch.setattr(
        "polymarket_bot.live.runner.estimate_daily_pnl_usd", lambda client: 0.0
    )
    market_maker.refresh_quotes.side_effect = RuntimeError("boom")
    bot._run_one_cycle()  # must not raise


def test_run_forever_stops_and_cancels_all_on_stop_event(monkeypatch):
    bot, client, market_maker, breaker, _equity_protection = _bot(refresh_interval=0.02)
    monkeypatch.setattr(
        "polymarket_bot.live.runner.estimate_daily_pnl_usd", lambda client: 0.0
    )

    call_count = {"n": 0}

    def _refresh(**_kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            bot._stop_event.set()

    market_maker.refresh_quotes.side_effect = _refresh
    bot.run_forever()

    assert call_count["n"] >= 2
    client.cancel_all.assert_called_once()


def test_run_forever_handles_keyboard_interrupt(monkeypatch):
    bot, client, market_maker, breaker, _equity_protection = _bot(refresh_interval=0.02)
    monkeypatch.setattr(
        "polymarket_bot.live.runner.estimate_daily_pnl_usd", lambda client: 0.0
    )
    market_maker.refresh_quotes.side_effect = KeyboardInterrupt()

    bot.run_forever()  # must not raise

    client.cancel_all.assert_called_once()


def test_estimate_daily_pnl_falls_back_to_zero_when_unknown(monkeypatch):
    bot, client, market_maker, breaker, _equity_protection = _bot()
    monkeypatch.setattr(
        "polymarket_bot.live.runner.estimate_daily_pnl_usd", lambda client: None
    )
    assert bot._estimate_daily_pnl() == 0.0


def test_estimate_daily_pnl_returns_real_value(monkeypatch):
    bot, client, market_maker, breaker, _equity_protection = _bot()
    monkeypatch.setattr(
        "polymarket_bot.live.runner.estimate_daily_pnl_usd", lambda client: -42.0
    )
    assert bot._estimate_daily_pnl() == -42.0


def test_run_one_cycle_skips_refresh_when_equity_protection_halted(monkeypatch):
    bot, client, market_maker, breaker, equity_protection = _bot()
    monkeypatch.setattr(
        "polymarket_bot.live.runner.estimate_daily_pnl_usd", lambda client: 0.0
    )
    equity_protection.evaluate.return_value = (True, 1.0)

    bot._run_one_cycle()

    market_maker.refresh_quotes.assert_not_called()


def test_equity_protection_not_evaluated_when_circuit_breaker_already_halted(monkeypatch):
    bot, client, market_maker, breaker, equity_protection = _bot()
    monkeypatch.setattr(
        "polymarket_bot.live.runner.estimate_daily_pnl_usd", lambda client: 0.0
    )
    breaker.evaluate.return_value = True

    bot._run_one_cycle()

    equity_protection.evaluate.assert_not_called()


def test_size_multiplier_produces_scaled_settings_override(monkeypatch):
    bot, client, market_maker, breaker, equity_protection = _bot()
    monkeypatch.setattr(
        "polymarket_bot.live.runner.estimate_daily_pnl_usd", lambda client: 0.0
    )
    equity_protection.evaluate.return_value = (False, 0.5)

    bot._run_one_cycle()

    override = market_maker.refresh_quotes.call_args.kwargs["settings_override"]
    assert override.order_shares_min == 8.0
    assert override.order_shares_max == 8.0
    assert override.max_orders_per_cycle == bot.settings.max_orders_per_cycle


def test_size_multiplier_of_one_passes_no_override(monkeypatch):
    bot, client, market_maker, breaker, equity_protection = _bot()
    monkeypatch.setattr(
        "polymarket_bot.live.runner.estimate_daily_pnl_usd", lambda client: 0.0
    )
    equity_protection.evaluate.return_value = (False, 1.0)

    bot._run_one_cycle()

    assert market_maker.refresh_quotes.call_args.kwargs["settings_override"] is None
