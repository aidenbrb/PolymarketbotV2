"""Pure lifecycle rules shared by controlled shadow and live-pilot trading."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ControlledLifecycle:
    seconds_from_event_start: float
    pregame_paused: bool
    entry_cutoff_due: bool
    inplay_deadline_due: bool
    entry_open: bool


def controlled_lifecycle(
    *,
    event_start_epoch: float,
    now_epoch: float,
    pregame_pause_minutes: float,
    max_started_event_hours: float,
    entry_cutoff_minutes: float,
) -> ControlledLifecycle:
    """Return controlled-strategy state with exact, reusable boundaries.

    The last pregame hour is closed, kickoff reopens entries, the final
    ``entry_cutoff_minutes`` of the in-play window is closed, and the full
    in-play deadline requires liquidation.
    """
    elapsed = float(now_epoch) - float(event_start_epoch)
    pregame_pause_seconds = max(0.0, pregame_pause_minutes) * 60.0
    inplay_deadline_seconds = max(0.0, max_started_event_hours) * 3600.0
    entry_cutoff_seconds = max(
        0.0,
        inplay_deadline_seconds - max(0.0, entry_cutoff_minutes) * 60.0,
    )
    pregame_paused = elapsed < 0 and -elapsed <= pregame_pause_seconds
    entry_cutoff_due = elapsed >= entry_cutoff_seconds
    inplay_deadline_due = elapsed >= inplay_deadline_seconds
    return ControlledLifecycle(
        seconds_from_event_start=elapsed,
        pregame_paused=pregame_paused,
        entry_cutoff_due=entry_cutoff_due,
        inplay_deadline_due=inplay_deadline_due,
        entry_open=not pregame_paused and not entry_cutoff_due,
    )
