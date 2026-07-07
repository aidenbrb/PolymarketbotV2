import time

import pytest

from polymarket_bot.live.toxicity_tracker import ToxicityTracker


def test_first_observation_seeds_ewma_directly():
    tracker = ToxicityTracker(alpha=0.3)
    tracker.record_markout("m1", -5.0)
    assert tracker.get_ewma("m1") == pytest.approx(-5.0)


def test_subsequent_update_matches_ewma_formula():
    tracker = ToxicityTracker(alpha=0.3)
    tracker.record_markout("m1", 10.0)
    tracker.record_markout("m1", -10.0)
    # alpha*new + (1-alpha)*old = 0.3*(-10) + 0.7*10 = 4.0
    assert tracker.get_ewma("m1") == pytest.approx(4.0)


def test_per_market_isolation():
    tracker = ToxicityTracker(alpha=0.3)
    tracker.record_markout("m1", -5.0)
    tracker.record_markout("m2", 5.0)
    assert tracker.get_ewma("m1") == pytest.approx(-5.0)
    assert tracker.get_ewma("m2") == pytest.approx(5.0)


def test_get_ewma_none_with_no_observations():
    tracker = ToxicityTracker()
    assert tracker.get_ewma("m1") is None


def test_is_in_cooldown_false_before_any_adverse_observation():
    tracker = ToxicityTracker(adverse_threshold_cents=-1.0)
    tracker.record_markout("m1", 5.0)
    assert tracker.is_in_cooldown("m1") is False


def test_is_in_cooldown_true_once_threshold_crossed():
    tracker = ToxicityTracker(adverse_threshold_cents=-1.0)
    tracker.record_markout("m1", -5.0)
    assert tracker.is_in_cooldown("m1") is True


def test_is_in_cooldown_false_for_market_with_no_observations():
    tracker = ToxicityTracker()
    assert tracker.is_in_cooldown("m1") is False


def test_cooldown_expires_after_cooldown_seconds():
    tracker = ToxicityTracker(adverse_threshold_cents=-1.0, cooldown_seconds=0.05)
    tracker.record_markout("m1", -5.0)
    assert tracker.is_in_cooldown("m1") is True
    time.sleep(0.1)
    assert tracker.is_in_cooldown("m1") is False


def test_renewed_adverse_observation_during_cooldown_extends_it():
    tracker = ToxicityTracker(alpha=1.0, adverse_threshold_cents=-1.0, cooldown_seconds=0.1)
    tracker.record_markout("m1", -5.0)
    time.sleep(0.05)  # still within the original cooldown
    tracker.record_markout("m1", -5.0)  # renews cooldown for another 0.1s from now
    time.sleep(0.07)  # total elapsed since first record: 0.12s (past original), but renewed
    assert tracker.is_in_cooldown("m1") is True


def test_favorable_observation_during_cooldown_does_not_shorten_it():
    tracker = ToxicityTracker(alpha=0.3, adverse_threshold_cents=-1.0, cooldown_seconds=0.2)
    tracker.record_markout("m1", -10.0)
    assert tracker.is_in_cooldown("m1") is True
    tracker.record_markout("m1", 10.0)  # favorable, but EWMA may still be adverse via smoothing
    # Still within the ORIGINAL cooldown window -- must not have been cancelled early.
    assert tracker.is_in_cooldown("m1") is True
