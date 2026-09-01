import pytest

from polymarket_bot.live import session_metrics


def test_session_metrics_records_pnl_and_activity_deltas(tmp_path, monkeypatch):
    counts = {"fills": 10, "cycles": 20}
    monkeypatch.setattr(session_metrics, "get_all_fills", lambda: [None] * counts["fills"])
    monkeypatch.setattr(session_metrics, "get_all_cycles", lambda: [None] * counts["cycles"])
    recorder = session_metrics.SessionMetricsRecorder(tmp_path / "sessions.json")

    started = recorder.start()
    recorder.update(5.0)
    counts.update(fills=13, cycles=27)
    updated = recorder.update(5.75)
    finished = recorder.finish(5.75)

    assert started["starting_fill_count"] == 10
    assert updated["session_pnl_usd"] == 0.75
    assert updated["new_fill_count"] == 3
    assert updated["new_quote_cycle_count"] == 7
    assert finished["status"] == "stopped"
    assert finished["ended_at"] is not None


def test_update_starts_record_if_needed(tmp_path, monkeypatch):
    monkeypatch.setattr(session_metrics, "get_all_fills", lambda: [])
    monkeypatch.setattr(session_metrics, "get_all_cycles", lambda: [])
    recorder = session_metrics.SessionMetricsRecorder(tmp_path / "sessions.json")

    record = recorder.update(-2.0)

    assert record["starting_total_pnl_usd"] == -2.0
    assert record["session_pnl_usd"] == 0.0


def test_flat_round_trip_fill_cashflow_overrides_misleading_position_pnl_delta(
    tmp_path, monkeypatch,
):
    fills = []
    monkeypatch.setattr(session_metrics, "get_all_fills", lambda: list(fills))
    monkeypatch.setattr(session_metrics, "get_all_cycles", lambda: [])
    recorder = session_metrics.SessionMetricsRecorder(tmp_path / "sessions.json")
    recorder.start()
    recorder.update(10.0)
    fills.extend([
        {
            "market_slug": "m1", "outcome": "YES", "side": "BUY",
            "shares": 10.0, "price": 0.20, "commission_usd": 0.10,
        },
        {
            "market_slug": "m1", "outcome": "YES", "side": "SELL",
            "shares": 10.0, "price": 0.22, "commission_usd": 0.20,
        },
    ])

    record = recorder.update(11.0)

    assert record["account_position_pnl_delta_usd"] == 1.0
    assert record["flat_round_trip_fill_pnl_usd"] == pytest.approx(-0.10)
    assert record["session_pnl_usd"] == pytest.approx(-0.10)
    assert record["session_pnl_source"] == "flat_round_trip_fills"


class TestMarkStaleRunningSessionsCrashed:
    """Startup crash recovery (live/startup_recovery.py) relies on this to
    detect a session left "running" by a process that never reached
    .finish() -- a confirmed real incident (see live/RUNBOOK.md's most
    recent section)."""

    def test_marks_a_running_session_crashed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session_metrics, "get_all_fills", lambda: [])
        monkeypatch.setattr(session_metrics, "get_all_cycles", lambda: [])
        path = tmp_path / "sessions.json"
        recorder = session_metrics.SessionMetricsRecorder(path)
        started = recorder.start()

        marked = session_metrics.mark_stale_running_sessions_crashed(path)

        assert marked == [started["session_id"]]
        records = session_metrics._load_records(path)
        record = records[0]
        assert record["status"] == "crashed"
        assert record["ended_at"] is not None

    def test_leaves_a_cleanly_stopped_session_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session_metrics, "get_all_fills", lambda: [])
        monkeypatch.setattr(session_metrics, "get_all_cycles", lambda: [])
        path = tmp_path / "sessions.json"
        recorder = session_metrics.SessionMetricsRecorder(path)
        recorder.start()
        recorder.finish(0.0)

        marked = session_metrics.mark_stale_running_sessions_crashed(path)

        assert marked == []
        records = session_metrics._load_records(path)
        assert records[0]["status"] == "stopped"

    def test_empty_when_no_file(self, tmp_path):
        assert session_metrics.mark_stale_running_sessions_crashed(tmp_path / "sessions.json") == []
