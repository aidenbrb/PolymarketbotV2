from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from polymarket_bot import config
from polymarket_bot.live import fills as fills_module
from polymarket_bot.live import instance_lock
from polymarket_bot.live.ws_runner import WebSocketLiveTradingBot
from polymarket_bot.models import Market, ScoredMarket


@pytest.fixture(autouse=True)
def _isolated_instance_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(instance_lock, "LOCK_FILE", tmp_path / "live_bot.lock")


@pytest.fixture(autouse=True)
def _isolated_fills_file(tmp_path, monkeypatch):
    # _compute_due_markouts/_persist_new_fills read/write fills.json via
    # fills.py's module-level FILLS_FILE -- must never touch the real
    # project's data/live_trades/fills.json from a test run.
    monkeypatch.setattr(fills_module, "FILLS_FILE", tmp_path / "fills.json")


def _scored(market_id="m1"):
    market = Market(
        market_id=market_id,
        event_id="e1",
        question="Will X happen?",
        category="politics",
        token_ids=["t1"],
        spread=0.03,
        raw={"orderPriceMinTickSize": 0.01},
    )
    return ScoredMarket(
        market_id=market_id,
        question=market.question,
        total_score=90.0,
        component_scores={},
        explanation=[],
        recommendation="PAPER_CANDIDATE",
        market=market,
    )


def _bot(monkeypatch):
    settings = config.LiveTradingSettings(
        refresh_interval_seconds=0.01, order_shares_min=16.0, order_shares_max=16.0,
    )
    client = Mock()
    client.cancel_all = Mock()
    market_ws = Mock()
    maker = Mock()
    breaker = Mock()
    breaker.evaluate.return_value = False
    equity_protection = Mock()
    equity_protection.evaluate.return_value = (False, 1.0)
    selector = Mock(return_value=[_scored("m1"), _scored("m2")])
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.estimate_daily_pnl_usd", lambda client: 0.0
    )
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.select_target_markets",
        selector,
    )
    bot = WebSocketLiveTradingBot(
        client=client,
        settings=settings,
        circuit_breaker=breaker,
        equity_protection=equity_protection,
        market_ws=market_ws,
        maker=maker,
    )
    return bot, client, market_ws, maker, breaker, selector, equity_protection


def test_refresh_candidates_subscribes_websocket(monkeypatch):
    bot, _client, market_ws, _maker, _breaker, selector, _equity_protection = _bot(monkeypatch)

    bot._refresh_candidates()

    selector.assert_called_once_with(settings=bot.settings, max_targets=10, raw_by_slug_out={})
    market_ws.set_market_slugs.assert_called_once_with(["m1", "m2"])


def test_refresh_candidates_populates_extra_raw_by_slug(monkeypatch):
    def _selector(settings, max_targets=None, raw_by_slug_out=None):
        if raw_by_slug_out is not None:
            raw_by_slug_out["m1"] = {"marketType": "futures"}
        return [_scored("m1"), _scored("m2")]

    bot, _client, _market_ws, _maker, _breaker, _selector_mock, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr("polymarket_bot.live.ws_runner.select_target_markets", _selector)

    bot._refresh_candidates()

    assert bot._get_extra_raw_by_slug() == {"m1": {"marketType": "futures"}}


def test_run_one_cycle_passes_extra_raw_by_slug_to_refresh_quotes(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()

    bot._run_one_cycle()

    extra_raw = maker.refresh_quotes.call_args.kwargs["extra_raw_by_slug"]
    assert extra_raw == bot._get_extra_raw_by_slug()


def test_run_one_cycle_uses_current_candidates(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    bot._refresh_candidates()

    bot._run_one_cycle()

    candidates = maker.refresh_quotes.call_args.kwargs["candidates"]
    assert [c.market_id for c in candidates] == ["m1", "m2"]


def test_run_one_cycle_skips_when_circuit_breaker_halted(monkeypatch):
    bot, _client, _market_ws, maker, breaker, _selector, _equity_protection = _bot(monkeypatch)
    breaker.evaluate.return_value = True

    bot._run_one_cycle()

    maker.refresh_quotes.assert_not_called()


def test_persist_new_fills_records_new_executions(monkeypatch, tmp_path):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.already_recorded_fill_ids", lambda: set()
    )
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.get_known_order_details", lambda: {}
    )
    recorded = []
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.record_fill", lambda fill: recorded.append(fill)
    )
    bot.private_store.handle_message({
        "orderSubscriptionUpdate": {"execution": {
            "id": "exec-1", "type": "EXECUTION_TYPE_FILL", "lastShares": "5.0", "lastPx": {"value": "0.4"},
        }}
    })

    bot._persist_new_fills()

    assert len(recorded) == 1
    assert recorded[0].fill_id == "exec-1"


def test_persist_new_fills_skips_non_fill_execution_types(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.already_recorded_fill_ids", lambda: set()
    )
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.get_known_order_details", lambda: {}
    )
    recorded = []
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.record_fill", lambda fill: recorded.append(fill)
    )
    for exec_type in ("EXECUTION_TYPE_NEW", "EXECUTION_TYPE_CANCELED", "EXECUTION_TYPE_REJECTED"):
        bot.private_store.handle_message({
            "orderSubscriptionUpdate": {"execution": {"id": f"exec-{exec_type}", "type": exec_type}}
        })

    bot._persist_new_fills()

    assert recorded == []


def test_persist_new_fills_skips_already_recorded(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.already_recorded_fill_ids", lambda: {"exec-1"}
    )
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.get_known_order_details", lambda: {}
    )
    recorded = []
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.record_fill", lambda fill: recorded.append(fill)
    )
    bot.private_store.handle_message({
        "orderSubscriptionUpdate": {"execution": {
            "id": "exec-1", "type": "EXECUTION_TYPE_FILL", "lastShares": "5.0", "lastPx": {"value": "0.4"},
        }}
    })

    bot._persist_new_fills()

    assert recorded == []


def test_persist_new_fills_never_raises_out_of_run_one_cycle(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.already_recorded_fill_ids",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    bot._run_one_cycle()  # must not raise

    maker.refresh_quotes.assert_called_once()


def test_persist_new_fills_fetches_bbo_once_per_market_slug(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.already_recorded_fill_ids", lambda: set()
    )
    monkeypatch.setattr(
        "polymarket_bot.live.ws_runner.get_known_order_details",
        lambda: {"order-1": {"market_id": "m1", "side": "BUY", "price": 0.4}},
    )
    monkeypatch.setattr("polymarket_bot.live.ws_runner.record_fill", lambda fill: None)
    maker.read_client.get_market_bbo.return_value = {"best_bid": 0.4, "best_ask": 0.42}
    bot.private_store.handle_message({
        "orderSubscriptionUpdate": {
            "execution": {
                "id": "exec-1", "type": "EXECUTION_TYPE_FILL", "orderId": "order-1",
                "lastShares": "5.0", "lastPx": {"value": "0.4"},
            }
        }
    })
    bot.private_store.handle_message({
        "orderSubscriptionUpdate": {
            "execution": {
                "id": "exec-2", "type": "EXECUTION_TYPE_FILL", "orderId": "order-1",
                "lastShares": "3.0", "lastPx": {"value": "0.41"},
            }
        }
    })

    bot._persist_new_fills()

    maker.read_client.get_market_bbo.assert_called_once_with("m1")


def test_run_one_cycle_skips_refresh_when_equity_protection_halted(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, equity_protection = _bot(monkeypatch)
    equity_protection.evaluate.return_value = (True, 1.0)

    bot._run_one_cycle()

    maker.refresh_quotes.assert_not_called()


def test_equity_protection_not_evaluated_when_circuit_breaker_already_halted(monkeypatch):
    bot, _client, _market_ws, maker, breaker, _selector, equity_protection = _bot(monkeypatch)
    breaker.evaluate.return_value = True

    bot._run_one_cycle()

    equity_protection.evaluate.assert_not_called()


def test_size_multiplier_produces_scaled_settings_override(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, equity_protection = _bot(monkeypatch)
    equity_protection.evaluate.return_value = (False, 0.5)

    bot._run_one_cycle()

    override = maker.refresh_quotes.call_args.kwargs["settings_override"]
    assert override.order_shares_min == 8.0
    assert override.order_shares_max == 8.0
    assert override.max_orders_per_cycle == bot.settings.max_orders_per_cycle


def test_size_multiplier_of_one_passes_no_override(monkeypatch):
    bot, _client, _market_ws, maker, _breaker, _selector, equity_protection = _bot(monkeypatch)
    equity_protection.evaluate.return_value = (False, 1.0)

    bot._run_one_cycle()

    assert maker.refresh_quotes.call_args.kwargs["settings_override"] is None


def _seed_fill(seconds_ago, **overrides):
    transact_time = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f000Z"
    )
    fill = {
        "fill_id": "f1",
        "market_slug": "m1",
        "side": "BUY",
        "price": 0.45,
        "transact_time": transact_time,
        "markout_1m_cents": None,
        "markout_1m_computed_at": None,
        "markout_5m_cents": None,
        "markout_5m_computed_at": None,
    }
    fill.update(overrides)
    return fill


class TestComputeDueMarkouts:
    def test_computes_1m_leaves_5m_alone_when_not_due(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([_seed_fill(seconds_ago=65)])
        maker.read_client.get_market_bbo.return_value = {"best_bid": 0.48, "best_ask": 0.50}

        bot._compute_due_markouts()

        fills = fills_module.get_all_fills()
        assert fills[0]["markout_1m_cents"] == pytest.approx(4.0)  # mid=0.49, fill=0.45, BUY -> +4c
        assert fills[0]["markout_1m_computed_at"] is not None
        assert fills[0]["markout_5m_cents"] is None
        assert fills[0]["markout_5m_computed_at"] is None

    def test_stale_window_resolves_to_none_with_computed_at_set(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([_seed_fill(seconds_ago=3600)])  # 1hr old, way past 15min staleness

        bot._compute_due_markouts()

        fills = fills_module.get_all_fills()
        assert fills[0]["markout_1m_cents"] is None
        assert fills[0]["markout_1m_computed_at"] is not None
        maker.read_client.get_market_bbo.assert_not_called()

    def test_bbo_unavailable_leaves_window_untouched_for_retry(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([_seed_fill(seconds_ago=65)])
        maker.read_client.get_market_bbo.return_value = None

        bot._compute_due_markouts()

        fills = fills_module.get_all_fills()
        assert fills[0]["markout_1m_cents"] is None
        assert fills[0]["markout_1m_computed_at"] is None  # not marked resolved -- retried next cycle

    def test_fetches_bbo_at_most_once_per_market_per_cycle(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([
            _seed_fill(seconds_ago=65, fill_id="f1", market_slug="m1"),
            _seed_fill(seconds_ago=65, fill_id="f2", market_slug="m1"),
        ])
        maker.read_client.get_market_bbo.return_value = {"best_bid": 0.48, "best_ask": 0.50}

        bot._compute_due_markouts()

        maker.read_client.get_market_bbo.assert_called_once_with("m1")

    def test_feeds_toxicity_tracker_once_per_new_1m_markout_not_5m(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([_seed_fill(
            seconds_ago=305, markout_1m_computed_at="2020-01-01T00:00:00+00:00",
        )])
        maker.read_client.get_market_bbo.return_value = {"best_bid": 0.48, "best_ask": 0.50}

        bot._compute_due_markouts()

        maker.toxicity_tracker.record_markout.assert_not_called()  # only 5m was due, not 1m

    def test_feeds_toxicity_tracker_for_newly_computed_1m_markout(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        fills_module.overwrite_fills([_seed_fill(seconds_ago=65)])
        maker.read_client.get_market_bbo.return_value = {"best_bid": 0.48, "best_ask": 0.50}

        bot._compute_due_markouts()

        maker.toxicity_tracker.record_markout.assert_called_once_with("m1", pytest.approx(4.0))

    def test_no_ops_when_markout_tracking_disabled(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = config.LiveTradingSettings(markout_tracking_enabled=False)
        fills_module.overwrite_fills([_seed_fill(seconds_ago=65)])

        bot._compute_due_markouts()

        maker.read_client.get_market_bbo.assert_not_called()
        assert fills_module.get_all_fills()[0]["markout_1m_computed_at"] is None

    def test_computes_but_does_not_feed_tracker_when_toxicity_disabled(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        bot.settings = config.LiveTradingSettings(toxicity_tracking_enabled=False)
        fills_module.overwrite_fills([_seed_fill(seconds_ago=65)])
        maker.read_client.get_market_bbo.return_value = {"best_bid": 0.48, "best_ask": 0.50}

        bot._compute_due_markouts()

        assert fills_module.get_all_fills()[0]["markout_1m_cents"] == pytest.approx(4.0)
        maker.toxicity_tracker.record_markout.assert_not_called()

    def test_never_raises_out_of_run_one_cycle(self, monkeypatch):
        bot, _client, _market_ws, maker, _breaker, _selector, _equity_protection = _bot(monkeypatch)
        monkeypatch.setattr(
            "polymarket_bot.live.ws_runner.get_all_fills",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        bot._run_one_cycle()  # must not raise

        maker.refresh_quotes.assert_called_once()
