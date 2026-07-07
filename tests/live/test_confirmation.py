import pytest

from polymarket_bot import config
from polymarket_bot.live.confirmation import LiveTradingNotConfirmed, require_live_confirmation


def _settings(enabled=True, phrase="CONFIRM", unattended_mode=False):
    return config.LiveTradingSettings(
        enabled=enabled, confirmation_phrase=phrase, unattended_mode=unattended_mode
    )


def test_fails_closed_when_not_enabled():
    with pytest.raises(LiveTradingNotConfirmed):
        require_live_confirmation(_settings(enabled=False), input_fn=lambda _: "CONFIRM")


def test_fails_closed_without_prompting_when_disabled():
    def _explode(_prompt):
        raise AssertionError("input_fn should never be called when disabled")

    with pytest.raises(LiveTradingNotConfirmed):
        require_live_confirmation(_settings(enabled=False), input_fn=_explode)


def test_wrong_phrase_raises():
    with pytest.raises(LiveTradingNotConfirmed):
        require_live_confirmation(_settings(enabled=True), input_fn=lambda _: "nope")


def test_exact_phrase_passes():
    require_live_confirmation(_settings(enabled=True), input_fn=lambda _: "CONFIRM")


def test_phrase_is_case_and_whitespace_sensitive_but_strips_trailing_input():
    require_live_confirmation(_settings(enabled=True), input_fn=lambda _: "  CONFIRM  ")
    with pytest.raises(LiveTradingNotConfirmed):
        require_live_confirmation(_settings(enabled=True), input_fn=lambda _: "confirm")


def test_unattended_mode_skips_prompt_entirely():
    def _explode(_prompt):
        raise AssertionError("input_fn should never be called in unattended mode")

    require_live_confirmation(
        _settings(enabled=True, unattended_mode=True), input_fn=_explode
    )


def test_unattended_mode_still_fails_closed_when_not_enabled():
    with pytest.raises(LiveTradingNotConfirmed):
        require_live_confirmation(
            _settings(enabled=False, unattended_mode=True), input_fn=lambda _: "CONFIRM"
        )
