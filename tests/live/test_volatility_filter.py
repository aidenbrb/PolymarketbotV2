import time

import pytest

from polymarket_bot.live.volatility_filter import VolatilityTracker


def test_recent_move_cents_returns_none_with_fewer_than_two_observations():
    tracker = VolatilityTracker(window_seconds=300.0)
    assert tracker.recent_move_cents("m1") is None

    tracker.record("m1", 0.50)
    assert tracker.recent_move_cents("m1") is None


def test_recent_move_cents_computes_range_in_cents():
    tracker = VolatilityTracker(window_seconds=300.0)
    tracker.record("m1", 0.50)
    tracker.record("m1", 0.53)
    tracker.record("m1", 0.51)

    # Range is max - min = 0.53 - 0.50 = 0.03 -> 3.0 cents.
    assert tracker.recent_move_cents("m1") == pytest.approx(3.0)


def test_recent_move_cents_is_per_market():
    tracker = VolatilityTracker(window_seconds=300.0)
    tracker.record("m1", 0.50)
    tracker.record("m1", 0.60)
    tracker.record("m2", 0.20)
    tracker.record("m2", 0.201)

    assert tracker.recent_move_cents("m1") == pytest.approx(10.0)
    assert tracker.recent_move_cents("m2") is not None
    assert tracker.recent_move_cents("m2") < 1.0


def test_old_observations_fall_out_of_the_window():
    tracker = VolatilityTracker(window_seconds=0.05)
    tracker.record("m1", 0.50)
    time.sleep(0.1)  # older observation ages out of the 0.05s window
    tracker.record("m1", 0.80)

    # Only one observation remains within the window -- nothing to compare.
    assert tracker.recent_move_cents("m1") is None


def test_recording_a_third_market_does_not_affect_others():
    tracker = VolatilityTracker(window_seconds=300.0)
    tracker.record("m1", 0.50)
    tracker.record("m1", 0.50)
    tracker.record("m3", 0.10)

    assert tracker.recent_move_cents("m1") == 0.0
    assert tracker.recent_move_cents("m2") is None
