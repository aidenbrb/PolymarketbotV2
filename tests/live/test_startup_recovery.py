from unittest.mock import Mock

import pytest

from polymarket_bot.live import ledger as ledger_module
from polymarket_bot.live import session_metrics as session_metrics_module
from polymarket_bot.live.ledger import record_cycle
from polymarket_bot.live.models import LiveQuoteCycle, PostedLeg
from polymarket_bot.live.startup_recovery import (
    StartupRecoveryError,
    recover_from_prior_crash,
    verify_flat_for_observation,
)
from polymarket_bot.live.us_client import UsApiError


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, "LEDGER_FILE", tmp_path / "orders.json")
    monkeypatch.setattr(session_metrics_module, "SESSIONS_FILE", tmp_path / "sessions.json")


def _record_known_order(order_id: str, market_id: str = "m1") -> None:
    record_cycle(LiveQuoteCycle(
        cycle_id=f"seed-{order_id}",
        market_id=market_id,
        reference_price=0.5,
        tick_size=0.01,
        bid=PostedLeg(side="BUY", price=0.5, size=1.0, order_id=order_id),
        ask=PostedLeg(side="SELL", price=0.5, size=1.0, order_id=None),
        timestamp="2026-01-01T00:00:00+00:00",
    ))


def _client(**overrides):
    client = Mock()
    client.get_open_orders.return_value = []
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


class TestRecoverFromPriorCrash:
    def test_marks_stale_session_and_cancels_recognized_leftover_orders(self, isolated):
        session_metrics_module.SessionMetricsRecorder().start()
        _record_known_order("o1", market_id="m1")
        client = _client(get_open_orders=Mock(side_effect=[
            [{"id": "o1", "marketSlug": "m1"}],  # initial enumeration
            [],  # post-cancel verification -- confirmed gone
        ]))

        recover_from_prior_crash(client)  # must not raise

        client.cancel_order.assert_called_once_with("o1", "m1")
        assert client.get_open_orders.call_count == 2
        records = session_metrics_module._load_records(session_metrics_module.SESSIONS_FILE)
        assert records[0]["status"] == "crashed"

    def test_leaves_an_unrecognized_order_alone(self, isolated):
        # Not seeded via _record_known_order -- the ledger doesn't
        # recognize it as bot-owned (a manual trade, or a different
        # strategy sharing the same account).
        client = _client(get_open_orders=Mock(return_value=[{"id": "not-mine", "marketSlug": "m1"}]))

        recover_from_prior_crash(client)  # must not raise

        client.cancel_order.assert_not_called()
        # No recognized orders means no verification re-fetch is needed --
        # only the one initial enumeration call.
        assert client.get_open_orders.call_count == 1

    def test_no_open_orders_and_no_stale_session_is_a_clean_no_op(self, isolated):
        client = _client()

        recover_from_prior_crash(client)  # must not raise

        client.cancel_order.assert_not_called()

    def test_enumeration_failure_aborts_startup(self, isolated):
        client = _client(get_open_orders=Mock(side_effect=UsApiError("network error")))

        with pytest.raises(StartupRecoveryError):
            recover_from_prior_crash(client)

    def test_cancel_failure_aborts_immediately_without_trying_the_rest(self, isolated):
        _record_known_order("o1", market_id="m1")
        _record_known_order("o2", market_id="m1")
        client = _client(get_open_orders=Mock(return_value=[
            {"id": "o1", "marketSlug": "m1"}, {"id": "o2", "marketSlug": "m1"},
        ]))
        client.cancel_order.side_effect = UsApiError("rejected")

        with pytest.raises(StartupRecoveryError):
            recover_from_prior_crash(client)

        # Aborted on the first failure -- the second order was never attempted.
        assert client.cancel_order.call_count == 1

    def test_aborts_if_a_recognized_order_is_still_open_after_cancellation(self, isolated):
        # cancel_order() not raising doesn't guarantee the exchange
        # actually removed the order (eventual consistency, or a silent
        # no-op) -- the post-cancel verification re-fetch must catch this.
        _record_known_order("o1", market_id="m1")
        client = _client(get_open_orders=Mock(return_value=[{"id": "o1", "marketSlug": "m1"}]))
        # cancel_order() "succeeds" (no exception) but the order is still
        # listed as open on the verification re-fetch (return_value is
        # static -- every call sees the same still-open order).

        with pytest.raises(StartupRecoveryError, match="still resting"):
            recover_from_prior_crash(client)

        client.cancel_order.assert_called_once_with("o1", "m1")

    def test_verification_enumeration_failure_aborts_startup(self, isolated):
        _record_known_order("o1", market_id="m1")
        client = _client(get_open_orders=Mock(side_effect=[
            [{"id": "o1", "marketSlug": "m1"}],
            UsApiError("network error"),  # fails on the verification re-fetch
        ]))

        with pytest.raises(StartupRecoveryError):
            recover_from_prior_crash(client)

        client.cancel_order.assert_called_once_with("o1", "m1")

    def test_running_session_is_marked_crashed_even_with_no_leftover_orders(self, isolated):
        session_metrics_module.SessionMetricsRecorder().start()
        client = _client()

        recover_from_prior_crash(client)  # must not raise

        records = session_metrics_module._load_records(session_metrics_module.SESSIONS_FILE)
        assert records[0]["status"] == "crashed"

    def test_session_marking_failure_does_not_block_order_recovery(self, isolated, monkeypatch):
        # Session-status bookkeeping is diagnostic, not safety-critical --
        # unlike the order-cancellation path, a failure there must not
        # abort startup.
        monkeypatch.setattr(
            "polymarket_bot.live.startup_recovery.mark_stale_running_sessions_crashed",
            Mock(side_effect=RuntimeError("disk full")),
        )
        client = _client()

        recover_from_prior_crash(client)  # must not raise


class TestVerifyFlatForObservation:
    def test_flat_account_returns_the_fetched_state(self):
        client = _client(get_all_positions=Mock(return_value={}))

        open_orders, positions = verify_flat_for_observation(client)

        assert open_orders == []
        assert positions == {}

    def test_an_explicit_zero_position_is_still_flat(self):
        client = _client(get_all_positions=Mock(
            return_value={"m1": {"netPositionDecimal": "0"}},
        ))

        open_orders, positions = verify_flat_for_observation(client)

        assert positions == {"m1": {"netPositionDecimal": "0"}}

    def test_aborts_if_any_open_order_exists(self):
        client = _client(get_open_orders=Mock(
            return_value=[{"id": "o1", "marketSlug": "m1"}],
        ))

        with pytest.raises(StartupRecoveryError, match="open order"):
            verify_flat_for_observation(client)

    def test_aborts_if_any_position_is_non_flat(self):
        client = _client(get_all_positions=Mock(
            return_value={"m1": {"netPositionDecimal": "5"}},
        ))

        with pytest.raises(StartupRecoveryError, match="open position"):
            verify_flat_for_observation(client)

    def test_aborts_if_a_position_value_is_malformed(self):
        # Fails closed -- an unparseable value is treated as NOT flat
        # rather than assumed safe.
        client = _client(get_all_positions=Mock(
            return_value={"m1": {"netPositionDecimal": "not-a-number"}},
        ))

        with pytest.raises(StartupRecoveryError, match="open position"):
            verify_flat_for_observation(client)

    def test_order_enumeration_failure_aborts(self):
        client = _client(get_open_orders=Mock(side_effect=UsApiError("network error")))

        with pytest.raises(StartupRecoveryError):
            verify_flat_for_observation(client)

    def test_position_enumeration_failure_aborts(self):
        client = _client(get_all_positions=Mock(side_effect=UsApiError("network error")))

        with pytest.raises(StartupRecoveryError):
            verify_flat_for_observation(client)
