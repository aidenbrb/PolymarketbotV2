import argparse
import json
from unittest.mock import Mock

import pytest

from polymarket_bot import config, main
from polymarket_bot.live import instance_lock
from polymarket_bot.live import market_observation as observation_module
from polymarket_bot.live.market_observation import MarketObservationTracker


@pytest.fixture(autouse=True)
def _isolated_instance_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(instance_lock, "LOCK_FILE", tmp_path / "live_bot.lock")


@pytest.fixture(autouse=True)
def _isolated_observation_file(tmp_path, monkeypatch):
    monkeypatch.setattr(observation_module, "OBSERVATION_FILE", tmp_path / "market_observations_v4.json")


def _settings(**overrides):
    values = dict(
        observation_only_mode=False,
        observation_gate_enabled=True,
        observation_persist_interval_seconds=9999.0,
    )
    values.update(overrides)
    return config.LiveTradingSettings(**values)


def _seed_stuck_market(slug="m1", *, side="BUY", quantity=10.0, price=0.40):
    path = observation_module.OBSERVATION_FILE
    tracker = MarketObservationTracker(_settings(), path=path)
    tracker.register_market(slug, tick_size=0.01, event_id="event-1")
    market = tracker._trackers["controlled"]._market(slug)
    market["event_id"] = "event-1"
    fills = market.setdefault("hypothetical_fills", [])
    for strategy in observation_module.SHADOW_STRATEGIES:
        fills.append({
            "key": f"seed|controlled|{strategy}|{slug}",
            "observed_at_epoch": 1000.0,
            "side": side, "price": price, "quantity": quantity,
            "strategy": strategy, "admissible": True,
            "liquidity_role": "maker", "role": "entry",
            "commission_usd": 0.0, "position_before": 0.0,
        })
    tracker.flush()
    return path


def _args(apply=False):
    return argparse.Namespace(apply=apply)


def _pilot_loaded_settings():
    return config.Settings(
        live=config.LiveTradingSettings(
            enabled=True,
            use_websocket=True,
            enable_private_websocket=True,
            unattended_mode=True,
        ),
        circuit_breaker=config.CircuitBreakerSettings(
            enabled=False, daily_loss_limit_usd=99.0,
        ),
        session_circuit_breaker=config.SessionCircuitBreakerSettings(
            enabled=False, loss_limit_usd=99.0,
        ),
        equity_protection=config.EquityProtectionSettings(
            profit_lock_size_multiplier=0.25,
        ),
    )


class TestDryRunCommands:
    def test_status_reports_not_started_when_no_archive_exists(self, tmp_path, monkeypatch, capsys):
        from polymarket_bot.live import market_dryrun as dryrun_module

        monkeypatch.setattr(
            dryrun_module, "DRYRUN_OBSERVATION_FILE", tmp_path / "dryrun.json",
        )

        main.cmd_live_shadow_dryrun_status(argparse.Namespace(json=False))

        output = capsys.readouterr().out
        assert "NOT_STARTED" in output

    def test_status_json_emits_the_persisted_snapshot(self, tmp_path, monkeypatch, capsys):
        from polymarket_bot.live import market_dryrun as dryrun_module

        path = tmp_path / "dryrun.json"
        monkeypatch.setattr(dryrun_module, "DRYRUN_OBSERVATION_FILE", path)
        tracker = MarketObservationTracker(_settings(), path=path)
        tracker.record_dry_run_snapshot({"phase": "COMPLETE", "verdict": "PASS"}, 1000.0)

        main.cmd_live_shadow_dryrun_status(argparse.Namespace(json=True))

        output = capsys.readouterr().out
        assert json.loads(output) == {"phase": "COMPLETE", "verdict": "PASS"}

    def test_start_reports_missing_credentials_without_crashing(self, monkeypatch, capsys):
        from polymarket_bot.live import credentials as credentials_module
        from polymarket_bot.live.credentials import MissingCredentialsError

        monkeypatch.setattr(
            credentials_module, "load_api_credentials",
            Mock(side_effect=MissingCredentialsError("no key")),
        )

        main.cmd_live_shadow_dryrun_start(argparse.Namespace())

        output = capsys.readouterr().out
        assert "Cannot start dry-run" in output

    def test_start_reports_policy_mismatch_without_crashing(self, monkeypatch, capsys):
        from polymarket_bot.live import credentials as credentials_module
        from polymarket_bot.live import market_dryrun as dryrun_module
        from polymarket_bot.live.credentials import ApiCredentials
        from polymarket_bot.live.market_dryrun import DryRunPolicyMismatchError

        monkeypatch.setattr(
            credentials_module, "load_api_credentials",
            Mock(return_value=ApiCredentials(key_id="k", secret_key="s")),
        )
        monkeypatch.setattr(
            dryrun_module, "run_dry_run",
            Mock(side_effect=DryRunPolicyMismatchError("mismatch")),
        )

        main.cmd_live_shadow_dryrun_start(argparse.Namespace())

        output = capsys.readouterr().out
        assert "Cannot start dry-run" in output

    def test_start_happy_path_calls_run_dry_run(self, monkeypatch, capsys):
        from polymarket_bot.live import credentials as credentials_module
        from polymarket_bot.live import market_dryrun as dryrun_module
        from polymarket_bot.live.credentials import ApiCredentials

        creds = ApiCredentials(key_id="k", secret_key="s")
        monkeypatch.setattr(
            credentials_module, "load_api_credentials", Mock(return_value=creds),
        )
        run_dry_run = Mock()
        monkeypatch.setattr(dryrun_module, "run_dry_run", run_dry_run)

        main.cmd_live_shadow_dryrun_start(argparse.Namespace())

        run_dry_run.assert_called_once()
        assert run_dry_run.call_args.kwargs["credentials"] == creds
        output = capsys.readouterr().out
        assert "No real order or account capability" in output


class TestJuly5LivePilotCommand:
    def _mock_informational_gate(self, monkeypatch):
        tracker = Mock()
        tracker.pilot_start_eligible.return_value = (
            False, ["controlled evidence did not pass"],
        )
        constructor = Mock(return_value=tracker)
        monkeypatch.setattr(
            observation_module, "MarketObservationTracker", constructor,
        )
        return tracker, constructor

    def test_failed_qualification_still_reaches_distinct_confirmation(
        self, monkeypatch, capsys,
    ):
        from polymarket_bot.live import confirmation as confirmation_module
        from polymarket_bot.live import credentials as credentials_module
        from polymarket_bot.live.confirmation import LiveTradingNotConfirmed

        loaded = _pilot_loaded_settings()
        monkeypatch.setattr(config, "load_settings", Mock(return_value=loaded))
        tracker, _constructor = self._mock_informational_gate(monkeypatch)
        confirmation = Mock(side_effect=LiveTradingNotConfirmed("declined"))
        monkeypatch.setattr(
            confirmation_module, "require_live_confirmation", confirmation,
        )
        credential_lookup = Mock()
        monkeypatch.setattr(
            credentials_module, "load_api_credentials", credential_lookup,
        )

        main.cmd_live_pilot_start_july5(argparse.Namespace())

        output = capsys.readouterr().out
        assert "informational only" in output
        assert "deliberately bypasses" in output
        assert "reactive thresholds" in output
        tracker.pilot_start_eligible.assert_called_once_with()
        pilot = confirmation.call_args.args[0]
        assert pilot.observation_gate_enabled is False
        assert pilot.pilot_qualification_bypassed is True
        assert pilot.pilot_strategy_profile == "july5_style"
        assert pilot.unattended_mode is False
        assert pilot.confirmation_phrase == main.JULY5_PILOT_CONFIRMATION_PHRASE
        credential_lookup.assert_not_called()

    def test_halted_daily_breaker_blocks_before_bot_construction(
        self, monkeypatch, capsys,
    ):
        from polymarket_bot.live import circuit_breaker as breaker_module
        from polymarket_bot.live import confirmation as confirmation_module
        from polymarket_bot.live import credentials as credentials_module
        from polymarket_bot.live import us_client as client_module
        from polymarket_bot.live import ws_runner as runner_module

        loaded = _pilot_loaded_settings()
        monkeypatch.setattr(config, "load_settings", Mock(return_value=loaded))
        self._mock_informational_gate(monkeypatch)
        monkeypatch.setattr(
            confirmation_module, "require_live_confirmation", Mock(),
        )
        monkeypatch.setattr(
            credentials_module, "load_api_credentials", Mock(return_value=Mock()),
        )
        client_constructor = Mock(return_value=Mock())
        monkeypatch.setattr(client_module, "LiveUsClient", client_constructor)
        daily_breaker = Mock()
        daily_breaker.is_halted.return_value = True
        breaker_constructor = Mock(return_value=daily_breaker)
        monkeypatch.setattr(breaker_module, "CircuitBreaker", breaker_constructor)
        bot_constructor = Mock()
        monkeypatch.setattr(
            runner_module, "WebSocketLiveTradingBot", bot_constructor,
        )

        main.cmd_live_pilot_start_july5(argparse.Namespace())

        assert "already halted" in capsys.readouterr().out
        assert breaker_constructor.call_args.args[0].enabled is True
        assert breaker_constructor.call_args.args[0].daily_loss_limit_usd == 3.0
        assert client_constructor.call_args.kwargs["settings"].order_shares_max == 1.0
        bot_constructor.assert_not_called()

    def test_mocked_success_path_uses_three_dollar_breakers_and_never_scales(
        self, monkeypatch,
    ):
        from polymarket_bot.live import circuit_breaker as breaker_module
        from polymarket_bot.live import confirmation as confirmation_module
        from polymarket_bot.live import credentials as credentials_module
        from polymarket_bot.live import equity_protection as equity_module
        from polymarket_bot.live import us_client as client_module
        from polymarket_bot.live import ws_runner as runner_module

        loaded = _pilot_loaded_settings()
        monkeypatch.setattr(config, "load_settings", Mock(return_value=loaded))
        tracker, _constructor = self._mock_informational_gate(monkeypatch)
        monkeypatch.setattr(
            confirmation_module, "require_live_confirmation", Mock(),
        )
        monkeypatch.setattr(
            credentials_module, "load_api_credentials", Mock(return_value=Mock()),
        )
        monkeypatch.setattr(client_module, "LiveUsClient", Mock(return_value=Mock()))
        daily_breaker = Mock()
        daily_breaker.is_halted.return_value = False
        breaker_constructor = Mock(return_value=daily_breaker)
        session_constructor = Mock(return_value=Mock())
        monkeypatch.setattr(breaker_module, "CircuitBreaker", breaker_constructor)
        monkeypatch.setattr(
            breaker_module, "SessionCircuitBreaker", session_constructor,
        )
        equity_constructor = Mock(return_value=Mock())
        monkeypatch.setattr(equity_module, "EquityProtection", equity_constructor)
        bot = Mock()
        bot_constructor = Mock(return_value=bot)
        monkeypatch.setattr(
            runner_module, "WebSocketLiveTradingBot", bot_constructor,
        )

        main.cmd_live_pilot_start_july5(argparse.Namespace())

        pilot = bot_constructor.call_args.kwargs["settings"]
        assert pilot.max_spread == 0.98
        assert pilot.max_orders_per_cycle == 10
        assert pilot.pilot_qualification_bypassed is True
        assert bot_constructor.call_args.kwargs["observation_tracker"] is tracker
        assert breaker_constructor.call_args.args[0].daily_loss_limit_usd == 3.0
        assert session_constructor.call_args.args[0].loss_limit_usd == 3.0
        assert equity_constructor.call_args.args[0].profit_lock_size_multiplier == 1.0
        bot.run_forever.assert_called_once_with()

    def test_uses_a_shadow_archive_isolated_from_the_observation_file(
        self, monkeypatch,
    ):
        from polymarket_bot.live import circuit_breaker as breaker_module
        from polymarket_bot.live import confirmation as confirmation_module
        from polymarket_bot.live import credentials as credentials_module
        from polymarket_bot.live import equity_protection as equity_module
        from polymarket_bot.live import us_client as client_module
        from polymarket_bot.live import ws_runner as runner_module

        loaded = _pilot_loaded_settings()
        monkeypatch.setattr(config, "load_settings", Mock(return_value=loaded))
        tracker_constructor = Mock(return_value=Mock())
        monkeypatch.setattr(
            observation_module, "MarketObservationTracker", tracker_constructor,
        )
        monkeypatch.setattr(
            confirmation_module, "require_live_confirmation", Mock(),
        )
        monkeypatch.setattr(
            credentials_module, "load_api_credentials", Mock(return_value=Mock()),
        )
        monkeypatch.setattr(client_module, "LiveUsClient", Mock(return_value=Mock()))
        daily_breaker = Mock()
        daily_breaker.is_halted.return_value = False
        monkeypatch.setattr(breaker_module, "CircuitBreaker", Mock(return_value=daily_breaker))
        monkeypatch.setattr(
            breaker_module, "SessionCircuitBreaker", Mock(return_value=Mock()),
        )
        monkeypatch.setattr(equity_module, "EquityProtection", Mock(return_value=Mock()))
        monkeypatch.setattr(
            runner_module, "WebSocketLiveTradingBot", Mock(return_value=Mock()),
        )

        main.cmd_live_pilot_start_july5(argparse.Namespace())

        # The FIRST construction is the informational, non-blocking
        # controlled-gate check (_mock_informational_gate's own pattern),
        # reading the ordinary shared OBSERVATION_FILE by default -- only
        # the SECOND, the one actually passed into the bot, must use the
        # pilot's own dedicated, isolated archive.
        pilot_tracker_call = tracker_constructor.call_args_list[-1]
        assert pilot_tracker_call.kwargs["path"] == observation_module.JULY5_PILOT_OBSERVATION_FILE
        assert pilot_tracker_call.kwargs["path"] != observation_module.OBSERVATION_FILE


def _mock_client(status="SETTLED", price=1.0):
    client = Mock()
    if status == "SETTLED":
        client.get_market_settlement.return_value = {"slug": "m1", "settlement": price}
        client.get_market_metadata.return_value = {
            "slug": "m1", "closed": True, "status": "MARKET_STATUS_RESOLVED", "active": True,
        }
    elif status == "UNRESOLVED":
        client.get_market_settlement.return_value = None
        client.get_market_metadata.return_value = {
            "slug": "m1", "closed": False, "status": "MARKET_STATUS_OPEN", "active": True,
        }
    elif status == "ERROR":
        client.get_market_settlement.return_value = None
        client.get_market_metadata.return_value = None
    return client


class TestLiveObservationSettle:
    def test_nothing_stuck_is_a_clean_no_op(self, monkeypatch, capsys):
        # No market registered at all -- open_inventory_slugs() is empty.
        tracker = MarketObservationTracker(_settings(), path=observation_module.OBSERVATION_FILE)
        tracker.flush()
        monkeypatch.setattr(main, "PolymarketClient", Mock(return_value=_mock_client()))

        main.cmd_live_observation_settle(_args(apply=True))

        assert "nothing to settle" in capsys.readouterr().out

    def test_dry_run_writes_nothing(self, monkeypatch, capsys):
        path = _seed_stuck_market()
        before_bytes = path.read_bytes()
        monkeypatch.setattr(main, "PolymarketClient", Mock(return_value=_mock_client()))

        main.cmd_live_observation_settle(_args(apply=False))

        assert path.read_bytes() == before_bytes
        assert "Dry run only" in capsys.readouterr().out

    def test_apply_writes_a_timestamped_backup_then_the_real_file(self, monkeypatch, tmp_path, capsys):
        path = _seed_stuck_market()
        before_bytes = path.read_bytes()
        monkeypatch.setattr(main, "PolymarketClient", Mock(return_value=_mock_client()))

        main.cmd_live_observation_settle(_args(apply=True))

        backups = list(tmp_path.glob("market_observations_v4.pre-settlement-backup-*.json"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == before_bytes
        after = json.loads(path.read_text())
        assert after["evaluation_finalization"]["complete"] is True
        assert "Applied." in capsys.readouterr().out

    def test_error_lookup_aborts_before_any_mutation(self, monkeypatch, tmp_path, capsys):
        path = _seed_stuck_market()
        before_bytes = path.read_bytes()
        monkeypatch.setattr(main, "PolymarketClient", Mock(return_value=_mock_client(status="ERROR")))

        with pytest.raises(SystemExit):
            main.cmd_live_observation_settle(_args(apply=True))

        assert path.read_bytes() == before_bytes
        assert list(tmp_path.glob("*.pre-settlement-backup-*.json")) == []
        assert "Aborting" in capsys.readouterr().out

    def test_first_run_against_an_unresolved_market_still_writes_bookkeeping(
        self, monkeypatch, capsys,
    ):
        # Establishing finalization bookkeeping for the first time (even
        # when nothing actually settles) is a real state change per the
        # no-op rule -- "identical to the final projected state," not
        # "no new settlement fills."
        path = _seed_stuck_market()
        before_bytes = path.read_bytes()
        monkeypatch.setattr(main, "PolymarketClient", Mock(return_value=_mock_client(status="UNRESOLVED")))

        main.cmd_live_observation_settle(_args(apply=True))

        assert path.read_bytes() != before_bytes
        assert "Applied." in capsys.readouterr().out

    def test_unresolved_only_is_a_true_no_op_once_bookkeeping_already_matches(
        self, monkeypatch, capsys,
    ):
        path = _seed_stuck_market()
        monkeypatch.setattr(main, "PolymarketClient", Mock(return_value=_mock_client(status="UNRESOLVED")))
        main.cmd_live_observation_settle(_args(apply=True))  # establishes bookkeeping
        after_first_run = path.read_bytes()
        capsys.readouterr()

        main.cmd_live_observation_settle(_args(apply=True))  # same answer again

        assert path.read_bytes() == after_first_run
        assert "nothing to write" in capsys.readouterr().out

    def test_backup_failure_aborts_before_writing(self, monkeypatch, tmp_path, capsys):
        path = _seed_stuck_market()
        before_bytes = path.read_bytes()
        monkeypatch.setattr(main, "PolymarketClient", Mock(return_value=_mock_client()))
        monkeypatch.setattr(
            main.shutil, "copy2",
            Mock(side_effect=OSError("disk full")),
        )

        with pytest.raises(SystemExit):
            main.cmd_live_observation_settle(_args(apply=True))

        assert path.read_bytes() == before_bytes

    def test_refuses_to_run_while_instance_lock_is_held(self, monkeypatch, capsys):
        _seed_stuck_market()
        monkeypatch.setattr(main, "PolymarketClient", Mock(return_value=_mock_client()))

        with instance_lock.InstanceLock():
            with pytest.raises(SystemExit):
                main.cmd_live_observation_settle(_args(apply=True))

    def test_idempotent_rerun_after_apply_is_a_no_op(self, monkeypatch, capsys):
        path = _seed_stuck_market()
        monkeypatch.setattr(main, "PolymarketClient", Mock(return_value=_mock_client()))
        main.cmd_live_observation_settle(_args(apply=True))
        after_first_apply = path.read_bytes()
        capsys.readouterr()

        # Fully settled and flat now -- open_inventory_slugs() is empty, so
        # this exits via the earliest "nothing stuck" path rather than
        # reaching the lookup/apply machinery at all. Either way, no
        # further write happens.
        main.cmd_live_observation_settle(_args(apply=True))

        assert path.read_bytes() == after_first_apply
        assert "nothing to settle" in capsys.readouterr().out


def _replay_args(json=False):
    return argparse.Namespace(json=json)


class TestLiveObservationReplay:
    def test_never_mutates_the_archive_file(self, capsys):
        path = _seed_stuck_market()
        before_bytes = path.read_bytes()

        main.cmd_live_observation_replay(_replay_args())

        assert path.read_bytes() == before_bytes
        assert "profile=controlled" in capsys.readouterr().out

    def test_json_mode_emits_valid_json_with_all_profiles(self, capsys):
        _seed_stuck_market()

        main.cmd_live_observation_replay(_replay_args(json=True))

        report = json.loads(capsys.readouterr().out)
        assert set(report["profiles"]) == {"legacy", "controlled", "july5_style"}
        for strategy in observation_module.SHADOW_STRATEGIES:
            assert strategy in report["profiles"]["controlled"]

    def test_runs_cleanly_against_an_empty_archive(self, capsys):
        tracker = MarketObservationTracker(_settings(), path=observation_module.OBSERVATION_FILE)
        tracker.flush()

        main.cmd_live_observation_replay(_replay_args())

        assert "profile=legacy" in capsys.readouterr().out

    def test_json_mode_never_authorizes_a_pilot_unlock(self, capsys):
        _seed_stuck_market()

        main.cmd_live_observation_replay(_replay_args(json=True))

        report = json.loads(capsys.readouterr().out)
        for strategies in report["profiles"].values():
            for strategy, data in strategies.items():
                if strategy == "_completion":
                    continue
                verdict = data["verdict"]
                assert verdict["pilot_unlock_authorized"] is False
                assert verdict["taker_replay_status"] == "UNKNOWN"
                for row in data["escalation_replay"] + data["hard_pre_settlement_exit"]:
                    assert "replay_found_exit" not in row
                    assert "exit_opportunity_existed" not in row
