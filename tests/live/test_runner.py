import dataclasses
from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live import instance_lock
from polymarket_bot.live import ledger as ledger_module
from polymarket_bot.live import session_metrics as session_metrics_module
from polymarket_bot.live.market_maker import EmergencySafeguardFailedError
from polymarket_bot.live.runner import LiveTradingBot


@pytest.fixture(autouse=True)
def _isolated_instance_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(instance_lock, "LOCK_FILE", tmp_path / "live_bot.lock")


@pytest.fixture(autouse=True)
def _isolated_daily_balance_file(tmp_path, monkeypatch):
    # _estimate_pnl_figures -> ledger_module.diff_against_baseline reads/
    # writes DAILY_BALANCE_FILE -- must never touch the real project's
    # data/live_trades/daily_pnl_baseline.json from a test run.
    monkeypatch.setattr(ledger_module, "DAILY_BALANCE_FILE", tmp_path / "daily_pnl_baseline.json")
    # run_forever() now calls startup_recovery.recover_from_prior_crash(),
    # which reads ledger_module.get_known_order_ids() and (via
    # mark_stale_running_sessions_crashed) reads/writes SESSIONS_FILE --
    # must never touch the real project's data/live_trades/orders.json or
    # sessions.json from a test run.
    monkeypatch.setattr(ledger_module, "LEDGER_FILE", tmp_path / "orders.json")
    monkeypatch.setattr(session_metrics_module, "SESSIONS_FILE", tmp_path / "sessions.json")


def _bot(refresh_interval=0.02):
    client = Mock()
    client.get_open_orders.return_value = []
    market_maker = Mock()
    settings = config.LiveTradingSettings(
        refresh_interval_seconds=refresh_interval, order_shares_min=16.0, order_shares_max=16.0,
    )
    breaker = Mock()
    breaker.evaluate.return_value = False
    session_breaker = Mock()
    session_breaker.evaluate.return_value = False
    session_breaker.diff.return_value = 0.0
    equity_protection = Mock()
    equity_protection.evaluate.return_value = (False, 1.0)
    bot = LiveTradingBot(
        client=client,
        market_maker=market_maker,
        circuit_breaker=breaker,
        session_circuit_breaker=session_breaker,
        equity_protection=equity_protection,
        settings=settings,
    )
    return bot, client, market_maker, breaker, session_breaker, equity_protection


def _stub_pnl(monkeypatch, total_now):
    monkeypatch.setattr(
        "polymarket_bot.live.runner.get_total_position_pnl_usd", lambda client: total_now
    )


def test_run_one_cycle_skips_refresh_when_halted(monkeypatch):
    bot, client, market_maker, breaker, _session_breaker, _equity_protection = _bot()
    _stub_pnl(monkeypatch, 0.0)
    breaker.evaluate.return_value = True
    bot._run_one_cycle()
    market_maker.refresh_quotes.assert_not_called()


def test_run_one_cycle_refreshes_when_not_halted(monkeypatch):
    bot, client, market_maker, breaker, _session_breaker, _equity_protection = _bot()
    _stub_pnl(monkeypatch, 0.0)
    bot._run_one_cycle()
    market_maker.refresh_quotes.assert_called_once()


def test_run_one_cycle_survives_refresh_failure(monkeypatch):
    bot, client, market_maker, breaker, _session_breaker, _equity_protection = _bot()
    _stub_pnl(monkeypatch, 0.0)
    market_maker.refresh_quotes.side_effect = RuntimeError("boom")
    bot._run_one_cycle()  # must not raise


def test_run_one_cycle_propagates_emergency_safeguard_failure(monkeypatch):
    # Unlike an ordinary refresh failure (the test above), an
    # EmergencySafeguardFailedError means the account-wide emergency
    # cancel-all could not be confirmed to have worked -- unconfirmed live
    # exposure after the last line of defense. This must NOT be swallowed
    # like a routine per-cycle failure; it has to reach run_forever()'s
    # top-level handler and stop the process.
    bot, client, market_maker, breaker, _session_breaker, _equity_protection = _bot()
    _stub_pnl(monkeypatch, 0.0)
    market_maker.refresh_quotes.side_effect = EmergencySafeguardFailedError("not confirmed clean")
    with pytest.raises(EmergencySafeguardFailedError):
        bot._run_one_cycle()


def test_run_forever_stops_and_cancels_all_on_stop_event(monkeypatch):
    bot, client, market_maker, breaker, _session_breaker, _equity_protection = _bot(refresh_interval=0.02)
    _stub_pnl(monkeypatch, 0.0)

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
    bot, client, market_maker, breaker, _session_breaker, _equity_protection = _bot(refresh_interval=0.02)
    _stub_pnl(monkeypatch, 0.0)
    market_maker.refresh_quotes.side_effect = KeyboardInterrupt()

    bot.run_forever()  # must not raise


def test_run_forever_calls_startup_crash_recovery(monkeypatch):
    bot, client, market_maker, _breaker, _session_breaker, _equity_protection = _bot(refresh_interval=0.02)
    _stub_pnl(monkeypatch, 0.0)
    recover = Mock()
    monkeypatch.setattr("polymarket_bot.live.runner.recover_from_prior_crash", recover)
    market_maker.refresh_quotes.side_effect = KeyboardInterrupt()

    bot.run_forever()

    recover.assert_called_once_with(client)


def test_run_forever_skips_startup_crash_recovery_when_disabled(monkeypatch):
    bot, client, market_maker, _breaker, _session_breaker, _equity_protection = _bot(refresh_interval=0.02)
    bot.settings = dataclasses.replace(bot.settings, startup_crash_recovery_enabled=False)
    _stub_pnl(monkeypatch, 0.0)
    recover = Mock()
    monkeypatch.setattr("polymarket_bot.live.runner.recover_from_prior_crash", recover)
    market_maker.refresh_quotes.side_effect = KeyboardInterrupt()

    bot.run_forever()

    recover.assert_not_called()


def test_run_forever_startup_crash_recovery_failure_blocks_startup(monkeypatch, caplog):
    # A failed recovery (can't enumerate/cancel/verify leftover orders)
    # must refuse to start rather than place new orders on top of unknown
    # prior state -- the main loop must never run at all.
    bot, client, market_maker, _breaker, _session_breaker, _equity_protection = _bot(refresh_interval=0.02)
    _stub_pnl(monkeypatch, 0.0)
    monkeypatch.setattr(
        "polymarket_bot.live.runner.recover_from_prior_crash",
        Mock(side_effect=RuntimeError("boom")),
    )

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="boom"):
            bot.run_forever()

    market_maker.refresh_quotes.assert_not_called()
    assert any("refusing to start" in r.message for r in caplog.records)


def test_run_forever_logs_and_reraises_an_unexpected_exception(monkeypatch, caplog):
    # _run_one_cycle() already catches an ordinary refresh_quotes()
    # failure internally (see test_run_one_cycle_survives_refresh_failure)
    # -- this exercises a failure OUTSIDE that try/except, at the
    # run_forever() level itself, to prove the new except Exception clause
    # there actually fires. Re-raising alone isn't distinguishing (Python
    # would propagate an uncaught exception through finally: regardless) --
    # the actual point of this clause is getting the traceback into the
    # rotating bot.log even on a manual/non-autostart launch, so assert on
    # that specifically via caplog, not just on the exception/cleanup.
    bot, client, _market_maker, _breaker, _session_breaker, _equity_protection = _bot(refresh_interval=0.02)
    bot._run_one_cycle = Mock(side_effect=RuntimeError("boom"))

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="boom"):
            bot.run_forever()

    # finally:'s cleanup still ran despite the crash.
    client.cancel_all.assert_called_once()
    assert any("crashed unexpectedly" in r.message for r in caplog.records)
    assert any(r.exc_info is not None for r in caplog.records)

    client.cancel_all.assert_called_once()


def test_estimate_pnl_figures_stays_unknown_when_unavailable(monkeypatch):
    bot, client, market_maker, breaker, _session_breaker, _equity_protection = _bot()
    _stub_pnl(monkeypatch, None)
    assert bot._estimate_pnl_figures() == (None, 0.0, 0.0)


def test_run_one_cycle_fails_closed_when_pnl_is_unknown(monkeypatch):
    bot, client, market_maker, _breaker, _session_breaker, _equity_protection = _bot()
    _stub_pnl(monkeypatch, None)

    bot._run_one_cycle()

    market_maker.refresh_quotes.assert_not_called()
    client.cancel_all.assert_called_once()


def test_estimate_pnl_figures_returns_real_daily_value(monkeypatch):
    bot, client, market_maker, breaker, _session_breaker, _equity_protection = _bot()
    _stub_pnl(monkeypatch, -42.0)
    total_now, daily_pnl, _session_pnl = bot._estimate_pnl_figures()
    assert total_now == -42.0
    # First call against a fresh baseline file bootstraps to 0.0, matching
    # diff_against_baseline's own documented bootstrap behavior.
    assert daily_pnl == 0.0


def test_run_one_cycle_skips_refresh_when_session_breaker_halted(monkeypatch):
    # The REST-fallback runner previously had no SessionCircuitBreaker at
    # all -- a real gap, since it's enabled by default for the WS-driven
    # runner. Confirms it's now actually wired in and can halt a cycle.
    bot, client, market_maker, breaker, session_breaker, _equity_protection = _bot()
    _stub_pnl(monkeypatch, 0.0)
    session_breaker.evaluate.return_value = True

    bot._run_one_cycle()

    market_maker.refresh_quotes.assert_not_called()


def test_session_breaker_receives_session_diffed_pnl(monkeypatch):
    bot, client, market_maker, breaker, session_breaker, _equity_protection = _bot()
    _stub_pnl(monkeypatch, 5.0)
    session_breaker.diff.return_value = 3.5

    bot._run_one_cycle()

    session_breaker.diff.assert_called_once_with(5.0)
    assert session_breaker.evaluate.call_args.kwargs["total_pnl_usd"] == 3.5


def test_session_breaker_not_evaluated_when_daily_breaker_already_halted(monkeypatch):
    bot, client, market_maker, breaker, session_breaker, _equity_protection = _bot()
    _stub_pnl(monkeypatch, 0.0)
    breaker.evaluate.return_value = True

    bot._run_one_cycle()

    session_breaker.evaluate.assert_not_called()


def test_run_one_cycle_skips_refresh_when_equity_protection_halted(monkeypatch):
    bot, client, market_maker, breaker, _session_breaker, equity_protection = _bot()
    _stub_pnl(monkeypatch, 0.0)
    equity_protection.evaluate.return_value = (True, 1.0)

    bot._run_one_cycle()

    market_maker.refresh_quotes.assert_not_called()


def test_equity_protection_receives_lifetime_pnl_without_a_second_fetch(monkeypatch):
    # Previously lifetime_pnl_usd was never passed, so equity_protection
    # fell back to its own internal get_total_position_pnl_usd() call -- a
    # redundant second positions fetch every cycle.
    bot, client, market_maker, breaker, _session_breaker, equity_protection = _bot()
    _stub_pnl(monkeypatch, 7.25)

    bot._run_one_cycle()

    assert equity_protection.evaluate.call_args.kwargs["lifetime_pnl_usd"] == 7.25


def test_equity_protection_not_evaluated_when_circuit_breaker_already_halted(monkeypatch):
    bot, client, market_maker, breaker, _session_breaker, equity_protection = _bot()
    _stub_pnl(monkeypatch, 0.0)
    breaker.evaluate.return_value = True

    bot._run_one_cycle()

    equity_protection.evaluate.assert_not_called()


def test_size_multiplier_produces_scaled_settings_override(monkeypatch):
    bot, client, market_maker, breaker, _session_breaker, equity_protection = _bot()
    _stub_pnl(monkeypatch, 0.0)
    equity_protection.evaluate.return_value = (False, 0.5)

    bot._run_one_cycle()

    override = market_maker.refresh_quotes.call_args.kwargs["settings_override"]
    assert override.order_shares_min == 8.0
    assert override.order_shares_max == 8.0
    assert override.max_orders_per_cycle == bot.settings.max_orders_per_cycle


def test_size_multiplier_of_one_passes_no_override(monkeypatch):
    bot, client, market_maker, breaker, _session_breaker, equity_protection = _bot()
    _stub_pnl(monkeypatch, 0.0)
    equity_protection.evaluate.return_value = (False, 1.0)

    bot._run_one_cycle()

    assert market_maker.refresh_quotes.call_args.kwargs["settings_override"] is None
