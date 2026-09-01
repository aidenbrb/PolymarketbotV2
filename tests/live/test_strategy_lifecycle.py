import pytest

from polymarket_bot.live.strategy_lifecycle import controlled_lifecycle


@pytest.mark.parametrize(
    ("now", "entry_open", "pregame_paused", "cutoff", "deadline"),
    [
        (-3601.0, True, False, False, False),
        (-3600.0, False, True, False, False),
        (0.0, True, False, False, False),
        (8999.999, True, False, False, False),
        (9000.0, False, False, True, False),
        (10800.0, False, False, True, True),
    ],
)
def test_controlled_lifecycle_exact_boundaries(
    now, entry_open, pregame_paused, cutoff, deadline,
):
    state = controlled_lifecycle(
        event_start_epoch=0.0,
        now_epoch=now,
        pregame_pause_minutes=60.0,
        max_started_event_hours=3.0,
        entry_cutoff_minutes=30.0,
    )

    assert state.entry_open is entry_open
    assert state.pregame_paused is pregame_paused
    assert state.entry_cutoff_due is cutoff
    assert state.inplay_deadline_due is deadline
