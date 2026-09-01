"""Offline attribution and replay over already-collected schema-v4
observation evidence (see market_observation.py). Tests whether revised
entry/exit/qualification rules would have produced a profitable result,
using ONLY data already on disk -- no new observation window, no network
calls, and this module never mutates the archive it reads.

The archive's `hypothetical_fills` are persisted HYPOTHETICAL shadow fills,
including synthetic closes tied to authoritative settlement payouts -- not
real exchange executions. Every P&L figure this module reports (baseline or
replayed) is trustworthy only within that shadow-fill model, never as real
account P&L. The only genuinely real data replayed against is the external
trade tape (`market["trades"]`) -- actual observed market prints, not the
bot's own activity.

Data-availability constraints, worth stating up front:

- `record_book()` (market_observation.py) only ever keeps the LATEST L2
  snapshot per market -- overwritten on every call, never a time series --
  so there is no way to reconstruct what depth was visible at an arbitrary
  past moment, and no historical queue position. Every rule below that
  needs a liquidity signal uses the trade tape as an explicit proxy for "was
  there a real opportunity to trade" -- never true book depth or queue
  priority. A printed trade that would have crossed our resting order is an
  OPTIMISTIC upper bound (full-size fill assumed), not a guaranteed fill,
  and says nothing about whether an aggressive taker order could have
  crossed existing resting liquidity -- that remains permanently UNKNOWN
  from this archive.
- `last_book_epoch`/`last_trade_at_epoch`/`last_observed_at_epoch` record
  when THE BOT last saw activity for a market -- not when the market
  actually closed. That can reflect the market going quiet, or equally a
  subscription drop, re-ranking out of the watched pool, or a process
  restart. Framed everywhere as "the final observed minutes," never "before
  market close."
- The observation run's overall healthy-feed coverage was only ~90%, so
  reaching an activity endpoint does not prove the connection was live
  throughout -- a crossing print could fall inside an unobserved gap. Every
  rule below that claims a complete negative ("no crossing observed")
  cross-checks the persisted global `feed_minute_buckets` for gaps first.
- `_round_trips_with_context()` cannot honestly replay a trip whose position
  was built from more than one entry fill (a real, non-degenerate case in
  the legacy profile) -- those are excluded from priced counterfactuals
  entirely, tagged `unknown_multifill`, rather than mispriced against only
  the first entry fill.

This is a read-only analysis. It does not own the pilot gate and can never
authorize a pilot unlock; actual pilot status is sourced from
`live-observation-report`.

The four things this replays, matching the diagnosis that the controlled
strategy captures small maker edge but gives it back on stranded inventory:

1. Escalating exits: would a resting exit order that gives up more edge the
   longer it waits have been crossed by a real observed trade, sooner than
   what actually happened (a book-based forced exit or a settlement)? See
   `replay_exit_escalation()`.
2. Entry rejection: would refusing to enter when too little time remains
   before the position's real risk deadline, or when trailing real
   trade-tape activity in that same market was too thin (the depth proxy),
   have avoided the positions that ended up stranded? See
   `sweep_entry_filters()`.
3. A hard "never intentionally hold through settlement" rule: was there any
   real trade to cross in the final observed window before a settled
   position's evidence ran out? See `replay_hard_pre_settlement_exit()`.
4. Revised cohort qualification: net P&L, profit factor, drawdown,
   settlement-exit rate, AND the original 5-round-trip/2-event sample floor
   at the cohort level. See `revised_cohort_rows()`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .. import storage
from ..logger import get_logger
from . import market_observation
from .market_observation import (
    OBSERVATION_PROFILES,
    PRIMARY_STRATEGY,
    PROFILE_CONTROLLED,
    PROFILE_JULY5_STYLE,
    PROFILE_LEGACY,
    SHADOW_STRATEGIES,
    _estimated_commission,
    _paper_position_state,
    _positive_float,
    _round_trip_records,
    _trade_hits_side,
)

logger = get_logger("live.observation_replay")


@dataclass(frozen=True)
class EscalationStep:
    after_seconds: float
    give_up_cents: float


# Tier 0 requires at least breakeven-or-better (the entry price itself, not
# the strategy's actual historical exit quote, which wasn't recorded at
# arbitrary past times -- see the module docstring). Later tiers give up
# more edge in exchange for actually getting out. The last tier's
# give_up_cents=100 means "accept any price on the correct side."
DEFAULT_EXIT_ESCALATION: tuple[EscalationStep, ...] = (
    EscalationStep(after_seconds=0.0, give_up_cents=0.0),
    EscalationStep(after_seconds=300.0, give_up_cents=2.0),
    EscalationStep(after_seconds=900.0, give_up_cents=6.0),
    EscalationStep(after_seconds=1800.0, give_up_cents=100.0),
)

# A single-tier "accept anything on the correct side immediately" policy,
# used by the hard-pre-settlement-exit rule -- it isn't testing whether a
# *patient* escalation would eventually find a fill, it's testing whether
# any real crossing existed at all in the final observed window.
IMMEDIATE_EXIT_ESCALATION: tuple[EscalationStep, ...] = (
    EscalationStep(after_seconds=0.0, give_up_cents=100.0),
)

TRAILING_ACTIVITY_WINDOW_SECONDS = 600.0

# Rule 3's fixed pre-close window.
HARD_EXIT_WINDOW_SECONDS = 1800.0

# Fallback risk-deadline constants -- used ONLY when replaying a pre-v5
# archive that has no persisted ObservationProfileSpec/QualificationPolicy
# to read from. Every v5+ archive persists its own actual values (see
# market_observation.ObservationProfileSpec/QualificationPolicy); these
# mirror the current-code defaults as of this writing, purely as a
# best-effort fallback for older data, and are never used to grade a v5+
# archive's evidence.
LEGACY_MAX_STARTED_EVENT_HOURS = 6.0          # observation_legacy_max_started_event_hours default
CONTROLLED_MAX_HOLDING_HOURS = 1.0            # observation_controlled_max_holding_hours default
CONTROLLED_ENTRY_CUTOFF_MINUTES = 30.0        # observation_controlled_entry_cutoff_minutes default
CONTROLLED_MAX_STARTED_EVENT_HOURS_DEFAULT = 3.0  # fallback only if archive lacks a persisted spec/qualification_config
MAKER_FEE_THETA_DEFAULT = -0.0125             # observation_maker_fee_theta default (a rebate, not a fee)

COHORT_MIN_ROUND_TRIPS = 5        # observation_cohort_min_round_trips default
COHORT_MIN_DISTINCT_EVENTS = 2    # observation_cohort_min_distinct_events default
COHORT_MIN_PROFIT_FACTOR = 1.20
COHORT_MAX_DRAWDOWN_USD = 3.0
COHORT_MAX_SETTLEMENT_EXIT_RATE = 0.20
MIN_AVG_MARKOUT_5M_CENTS = 0.0

# Entry-filter sweep grids, Cartesian (time x activity), per profile --
# controlled can never legitimately have more than its own 1h max-holding
# cap of runway, so its grid is bounded there; legacy and july5_style's
# real cap is 6h, so they share the wider grid.
CONTROLLED_HOURS_GRID: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)
LEGACY_HOURS_GRID: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 4.0)
TRAILING_TRADE_COUNT_GRID: tuple[int, ...] = (0, 1, 2, 3, 5)

# Follow-up gate result labels -- never "eligible," to avoid any reading as
# trading permission (see profile_follow_up_status()).
FOLLOW_UP_CANDIDATE = "FOLLOW_UP_CANDIDATE"
NOT_YET_QUALIFIED = "NOT_YET_QUALIFIED"

# Row status values shared by both replay rules. A crossing print is an
# optimistic upper bound (see module docstring), never proof of a
# guaranteed or aggressive-taker fill.
STATUS_CROSS_OBSERVED = "passive_trade_cross_observed"
STATUS_CROSS_NOT_OBSERVED = "passive_trade_cross_not_observed"
STATUS_UNKNOWN_NO_ANCHOR = "unknown_no_post_entry_anchor"
STATUS_UNKNOWN_INCOMPLETE_WINDOW = "unknown_incomplete_window"
STATUS_UNKNOWN_MULTIFILL = "unknown_multifill"

_UNKNOWN_STATUSES = frozenset({
    STATUS_UNKNOWN_NO_ANCHOR, STATUS_UNKNOWN_INCOMPLETE_WINDOW, STATUS_UNKNOWN_MULTIFILL,
})

FEED_COVERAGE_BASIS = "global_feed_minutes_plus_inventory_priority"


def load_archive(path: Optional[Path] = None) -> dict[str, Any]:
    return storage.load_json(path or market_observation.OBSERVATION_FILE, default={})


def _max_optional(*values: Optional[float]) -> Optional[float]:
    present = [float(v) for v in values if v is not None]
    return max(present) if present else None


def _exit_side_for(entry_side: str) -> str:
    return "SELL" if entry_side == "BUY" else "BUY"


def _spec_field(state: dict[str, Any], profile: str, field: str, default: Any) -> Any:
    """Read one field from a profile's persisted ObservationProfileSpec
    (market_observation.py, v5+ archives only), falling back to a supplied
    default for a pre-v5 archive that never persisted one."""
    spec = ((state.get("profiles") or {}).get(profile) or {}).get("spec")
    if isinstance(spec, dict) and field in spec:
        return spec[field]
    return default


def _risk_deadline_epoch(
    profile: str,
    entry_epoch: float,
    opened_at_epoch: float,
    event_epoch: Optional[float],
    *,
    max_started_event_hours: float,
    controlled_max_holding_hours: float = CONTROLLED_MAX_HOLDING_HOURS,
    controlled_entry_cutoff_minutes: float = CONTROLLED_ENTRY_CUTOFF_MINUTES,
) -> Optional[float]:
    """The real forced-exit deadline production enforced for this trip --
    NOT simply kickoff. Reconstructed from MarketObservationTracker's
    per-profile settings overrides and record_book()'s kickoff
    masking/restore dance (see module docstring and RUNBOOK 43/44 for the
    full derivation). `max_started_event_hours` is this profile's OWN value
    (legacy/july5_style: their own outer in-play deadline; controlled: its
    3h outer fallback); `controlled_max_holding_hours`/
    `controlled_entry_cutoff_minutes` only matter for the controlled
    branch's formula. Returns None only when no finite deadline is
    derivable at all (legacy/july5_style, with kickoff unknown for that
    market)."""
    if profile in (PROFILE_LEGACY, PROFILE_JULY5_STYLE):
        if event_epoch is None:
            return None
        return event_epoch + max_started_event_hours * 3600.0
    # controlled: the 1h max-holding cap applies unconditionally, even when
    # kickoff is unknown for this market -- only the near-event component
    # depends on kickoff.
    max_holding_deadline = opened_at_epoch + controlled_max_holding_hours * 3600.0
    if event_epoch is None:
        return max_holding_deadline
    outer_fallback = event_epoch + max_started_event_hours * 3600.0
    if entry_epoch < event_epoch:
        near_event_deadline = event_epoch  # pregame: capped at kickoff itself
    else:
        # In-play: masking suppresses the near-event trigger until the
        # controlled_lifecycle entry-cutoff boundary, at which point the
        # child sees the true (already-past) kickoff and near-event fires
        # immediately. Mirrors strategy_lifecycle.controlled_lifecycle's
        # entry_cutoff_seconds computation.
        entry_cutoff_seconds = (
            max_started_event_hours * 3600.0
            - controlled_entry_cutoff_minutes * 60.0
        )
        near_event_deadline = event_epoch + entry_cutoff_seconds
    return min(max_holding_deadline, near_event_deadline, outer_fallback)


def _required_feed_minute_keys(start_epoch: float, end_epoch: float) -> list[str]:
    """Mirrors the minute-bucket key format MarketObservationTracker uses
    to populate the persisted global state["feed_minute_buckets"]
    (market_observation.py, str(int(epoch // 60) * 60))."""
    start_bucket = int(start_epoch // 60) * 60
    end_bucket = int(end_epoch // 60) * 60
    return [str(bucket) for bucket in range(start_bucket, end_bucket + 60, 60)]


def _feed_coverage(
    feed_minute_buckets: dict[str, Any], start_epoch: float, end_epoch: float,
) -> dict[str, Any]:
    """Whether the persisted GLOBAL feed-minute record (not independently
    persisted per-market subscription history) shows every minute in
    [start_epoch, end_epoch] as having some observed activity. Reasonable
    as a per-market proxy because shadow inventory receives subscription
    priority once a position is open, but it is still global-feed evidence,
    not proof this specific market was watched every minute -- disclosed
    via FEED_COVERAGE_BASIS on every row that uses it."""
    required = _required_feed_minute_keys(start_epoch, end_epoch)
    missing = [bucket for bucket in required if bucket not in feed_minute_buckets]
    return {
        "required_feed_minute_count": len(required),
        "observed_feed_minute_count": len(required) - len(missing),
        "missing_feed_minute_count": len(missing),
        "fully_covered": not missing,
    }


def _find_escalated_exit(
    entry_epoch: float,
    exit_side: str,
    original_exit_price: float,
    trades: list[dict[str, Any]],
    not_after_epoch: float,
    *,
    not_before_epoch: float = 0.0,
    escalation: tuple[EscalationStep, ...] = DEFAULT_EXIT_ESCALATION,
) -> Optional[dict[str, Any]]:
    """First real trade after entry (and after not_before_epoch, up to
    not_after_epoch) that would have CROSSED an escalating resting exit
    order, per `escalation`'s (elapsed_seconds, cents_given_up) schedule.
    This finds an optimistic upper bound -- a crossing print, not a
    guaranteed fill (see module docstring) -- or None if no trade in the
    window ever qualifies."""
    candidates = sorted(
        (
            trade for trade in trades
            if entry_epoch < float(trade.get("observed_at_epoch", 0.0)) <= not_after_epoch
            and float(trade.get("observed_at_epoch", 0.0)) >= not_before_epoch
        ),
        key=lambda trade: float(trade["observed_at_epoch"]),
    )
    for trade in candidates:
        elapsed = float(trade["observed_at_epoch"]) - entry_epoch
        give_up = 0.0
        for step in escalation:
            if elapsed >= step.after_seconds:
                give_up = step.give_up_cents
        target = (
            original_exit_price - give_up / 100.0 if exit_side == "SELL"
            else original_exit_price + give_up / 100.0
        )
        target = max(0.0, min(1.0, target))
        hit = _trade_hits_side(
            str(trade.get("maker_side", "")),
            float(trade["price"]),
            bid=target if exit_side == "BUY" else None,
            ask=target if exit_side == "SELL" else None,
        )
        if hit == exit_side:
            return {
                "exit_epoch": float(trade["observed_at_epoch"]),
                "exit_price": float(trade["price"]),
                "elapsed_seconds": elapsed,
                "cents_given_up": give_up,
            }
    return None


def _round_trips_with_context(
    fills: list[dict[str, Any]], *, slug: str, event_id: str,
) -> list[dict[str, Any]]:
    """_round_trip_records() output, with each record's originating entry
    fill attached (needed to replay against the trade tape, which
    _round_trip_records() itself has no reason to know about), and flagged
    when the position was built from more than one entry fill before it
    closed.

    Matched by exact timestamp against `opened_at_epoch` (_round_trip_records
    sets this from the first fill's own observed_at_epoch when a position
    opens from flat), not by zipping trips against entry-role fills
    positionally: a position can open across more than one entry fill
    before it eventually closes, which would silently misalign a naive zip.
    `is_multi_entry` is True whenever more than one role=="entry" fill falls
    within [opened_at_epoch, closed_at_epoch) -- both replay rules refuse to
    price a counterfactual for those trips (see module docstring), since
    only the first entry fill's price/quantity is attached here and
    reconstructing what a different policy would have done after a
    multi-fill entry would require modeling changed downstream quote/fill
    behavior this archive can't support honestly."""
    trips = _round_trip_records(fills, slug=slug, event_id=event_id)
    entry_fills = sorted(
        (fill for fill in fills if fill.get("role") == "entry"),
        key=lambda fill: float(fill.get("observed_at_epoch", 0.0)),
    )
    entries_by_epoch: dict[float, dict[str, Any]] = {}
    for fill in entry_fills:
        entries_by_epoch.setdefault(float(fill.get("observed_at_epoch", 0.0)), fill)
    for trip in trips:
        opened_at = float(trip.get("opened_at_epoch", -1.0))
        closed_at = float(trip.get("closed_at_epoch", -1.0))
        trip["entry_fill"] = entries_by_epoch.get(opened_at)
        entry_count = sum(
            1 for fill in entry_fills
            if opened_at <= float(fill.get("observed_at_epoch", 0.0)) < closed_at
        )
        trip["is_multi_entry"] = entry_count > 1
    return trips


def _replay_row(
    *,
    market_slug: str,
    event_id: str,
    real_pnl_usd: float,
    real_closure_type: str,
    status: str,
    searched_from_epoch: Optional[float] = None,
    searched_until_epoch: Optional[float] = None,
    optimistic_full_fill_pnl_usd: Optional[float] = None,
    optimistic_pnl_delta_usd: Optional[float] = None,
    replayed_exit_epoch: Optional[float] = None,
    replayed_elapsed_seconds: Optional[float] = None,
    replayed_cents_given_up: Optional[float] = None,
    required_feed_minute_count: Optional[int] = None,
    observed_feed_minute_count: Optional[int] = None,
    missing_feed_minute_count: Optional[int] = None,
    coverage_basis: Optional[str] = None,
) -> dict[str, Any]:
    """Uniform row schema for both replay rules, so every row -- whatever
    branch produced it -- carries the same inspectable keys, and the
    taker-feasibility disclosure can never be silently dropped."""
    return {
        "market_slug": market_slug,
        "event_id": event_id,
        "real_pnl_usd": real_pnl_usd,
        "real_closure_type": real_closure_type,
        "status": status,
        "taker_exit_feasibility": "UNKNOWN",
        "searched_from_epoch": searched_from_epoch,
        "searched_until_epoch": searched_until_epoch,
        "optimistic_full_fill_pnl_usd": optimistic_full_fill_pnl_usd,
        "optimistic_pnl_delta_usd": optimistic_pnl_delta_usd,
        "replayed_exit_epoch": replayed_exit_epoch,
        "replayed_elapsed_seconds": replayed_elapsed_seconds,
        "replayed_cents_given_up": replayed_cents_given_up,
        "required_feed_minute_count": required_feed_minute_count,
        "observed_feed_minute_count": observed_feed_minute_count,
        "missing_feed_minute_count": missing_feed_minute_count,
        "coverage_basis": coverage_basis,
    }


def replay_exit_escalation(
    trips: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    *,
    profile: str,
    event_epoch: Optional[float],
    max_started_event_hours: float,
    controlled_max_holding_hours: float,
    controlled_entry_cutoff_minutes: float,
    last_observed_activity_epoch: Optional[float],
    feed_minute_buckets: dict[str, Any],
    maker_fee_theta: float,
    escalation: tuple[EscalationStep, ...] = DEFAULT_EXIT_ESCALATION,
) -> list[dict[str, Any]]:
    """For every round trip whose real closing fill was a settlement or a
    forced book-based exit, replay an escalating exit against the real
    trade tape, searched only up to that trip's own reconstructed risk
    deadline (see _risk_deadline_epoch) and only as far as the archive
    actually has coverage for. Returns one row per such trip via
    _replay_row(); see STATUS_* constants for what each status means."""
    rows = []
    for trip in trips:
        if trip.get("closure_type") != "settlement" and not trip.get("forced_exit"):
            continue
        entry = trip.get("entry_fill")
        if not entry:
            continue
        entry_epoch = float(entry.get("observed_at_epoch", 0.0))
        entry_price = float(entry.get("price", 0.0))
        exit_side = _exit_side_for(str(entry.get("side")))
        quantity = float(entry.get("quantity", 0.0))
        opened_at_epoch = float(trip.get("opened_at_epoch", entry_epoch))
        base = dict(
            market_slug=trip["market_slug"],
            event_id=trip["event_id"],
            real_pnl_usd=float(trip["pnl_usd"]),
            real_closure_type=trip.get("closure_type") or "forced_exit",
        )

        if trip.get("is_multi_entry"):
            rows.append(_replay_row(**base, status=STATUS_UNKNOWN_MULTIFILL))
            continue
        if (
            last_observed_activity_epoch is None
            or last_observed_activity_epoch <= max(entry_epoch, opened_at_epoch)
        ):
            rows.append(_replay_row(**base, status=STATUS_UNKNOWN_NO_ANCHOR))
            continue

        risk_deadline = _risk_deadline_epoch(
            profile, entry_epoch, opened_at_epoch, event_epoch,
            max_started_event_hours=max_started_event_hours,
            controlled_max_holding_hours=controlled_max_holding_hours,
            controlled_entry_cutoff_minutes=controlled_entry_cutoff_minutes,
        )
        search_until = (
            last_observed_activity_epoch if risk_deadline is None
            else min(risk_deadline, last_observed_activity_epoch)
        )
        replayed = _find_escalated_exit(
            entry_epoch, exit_side, entry_price, trades, search_until,
            escalation=escalation,
        )
        if replayed is not None:
            sign = 1.0 if exit_side == "SELL" else -1.0
            exit_price = replayed["exit_price"]
            optimistic_full_fill_pnl = (
                sign * (exit_price - entry_price) * quantity
                - float(entry.get("commission_usd") or 0.0)
                - _estimated_commission(exit_price, quantity, maker_fee_theta)
            )
            rows.append(_replay_row(
                **base, status=STATUS_CROSS_OBSERVED,
                searched_from_epoch=entry_epoch, searched_until_epoch=search_until,
                optimistic_full_fill_pnl_usd=optimistic_full_fill_pnl,
                optimistic_pnl_delta_usd=optimistic_full_fill_pnl - float(trip["pnl_usd"]),
                replayed_exit_epoch=replayed["exit_epoch"],
                replayed_elapsed_seconds=replayed["elapsed_seconds"],
                replayed_cents_given_up=replayed["cents_given_up"],
            ))
            continue

        if risk_deadline is None:
            # Legacy, unknown kickoff: never provably complete without a
            # finite deadline to check feed coverage through.
            rows.append(_replay_row(
                **base, status=STATUS_UNKNOWN_INCOMPLETE_WINDOW,
                searched_from_epoch=entry_epoch, searched_until_epoch=search_until,
            ))
            continue

        coverage = _feed_coverage(feed_minute_buckets, entry_epoch, risk_deadline)
        endpoint_reached = last_observed_activity_epoch >= risk_deadline
        status = (
            STATUS_CROSS_NOT_OBSERVED if endpoint_reached and coverage["fully_covered"]
            else STATUS_UNKNOWN_INCOMPLETE_WINDOW
        )
        rows.append(_replay_row(
            **base, status=status,
            searched_from_epoch=entry_epoch, searched_until_epoch=search_until,
            coverage_basis=FEED_COVERAGE_BASIS,
            required_feed_minute_count=coverage["required_feed_minute_count"],
            observed_feed_minute_count=coverage["observed_feed_minute_count"],
            missing_feed_minute_count=coverage["missing_feed_minute_count"],
        ))
    return rows


def replay_hard_pre_settlement_exit(
    trips: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    *,
    last_observed_activity_epoch: Optional[float],
    feed_minute_buckets: dict[str, Any],
    window_seconds: float = HARD_EXIT_WINDOW_SECONDS,
) -> list[dict[str, Any]]:
    """For every round trip that actually closed via settlement, check
    whether any real trade on the correct side existed in the final
    observed window before evidence for that market ran out -- clipped to
    never require coverage before the position's own entry. A market with
    zero qualifying trades AND complete feed coverage in that window could
    not have been saved by any passive exit-timing rule; that's a genuine
    liquidity finding. Missing coverage instead yields an honest unknown,
    never a negative claim."""
    rows = []
    for trip in trips:
        if trip.get("closure_type") != "settlement":
            continue
        entry = trip.get("entry_fill")
        if not entry:
            continue
        entry_epoch = float(entry.get("observed_at_epoch", 0.0))
        entry_price = float(entry.get("price", 0.0))
        exit_side = _exit_side_for(str(entry.get("side")))
        opened_at_epoch = float(trip.get("opened_at_epoch", entry_epoch))
        base = dict(
            market_slug=trip["market_slug"],
            event_id=trip["event_id"],
            real_pnl_usd=float(trip["pnl_usd"]),
            real_closure_type=trip.get("closure_type") or "forced_exit",
        )

        if trip.get("is_multi_entry"):
            rows.append(_replay_row(**base, status=STATUS_UNKNOWN_MULTIFILL))
            continue
        anchor = last_observed_activity_epoch
        if anchor is None or anchor <= max(entry_epoch, opened_at_epoch):
            rows.append(_replay_row(**base, status=STATUS_UNKNOWN_NO_ANCHOR))
            continue

        searched_from = max(anchor - window_seconds, entry_epoch, opened_at_epoch)
        searched_until = anchor
        replayed = _find_escalated_exit(
            entry_epoch, exit_side, entry_price, trades, searched_until,
            not_before_epoch=searched_from, escalation=IMMEDIATE_EXIT_ESCALATION,
        )
        if replayed is not None:
            rows.append(_replay_row(
                **base, status=STATUS_CROSS_OBSERVED,
                searched_from_epoch=searched_from, searched_until_epoch=searched_until,
                replayed_exit_epoch=replayed["exit_epoch"],
            ))
            continue

        coverage = _feed_coverage(feed_minute_buckets, searched_from, searched_until)
        status = STATUS_CROSS_NOT_OBSERVED if coverage["fully_covered"] else STATUS_UNKNOWN_INCOMPLETE_WINDOW
        rows.append(_replay_row(
            **base, status=status,
            searched_from_epoch=searched_from, searched_until_epoch=searched_until,
            coverage_basis=FEED_COVERAGE_BASIS,
            required_feed_minute_count=coverage["required_feed_minute_count"],
            observed_feed_minute_count=coverage["observed_feed_minute_count"],
            missing_feed_minute_count=coverage["missing_feed_minute_count"],
        ))
    return rows


def trailing_trade_activity(
    trades: list[dict[str, Any]], before_epoch: float,
    window_seconds: float = TRAILING_ACTIVITY_WINDOW_SECONDS,
) -> int:
    window_start = before_epoch - window_seconds
    return sum(
        1 for trade in trades
        if window_start <= float(trade.get("observed_at_epoch", 0.0)) < before_epoch
    )


def sweep_entry_filters(
    trips: list[dict[str, Any]],
    *,
    hours_grid: tuple[float, ...],
    trade_count_grid: tuple[int, ...] = TRAILING_TRADE_COUNT_GRID,
) -> list[dict[str, Any]]:
    """Full Cartesian grid (time-remaining x same-market trailing-trade
    activity) of which real entries would have been rejected, and the
    resulting portfolio P&L/profit-factor with those trips excluded.
    Reads only per-trip fields precomputed by run_replay from that trip's
    OWN market's tape and OWN reconstructed risk deadline
    (`_entry_hours_remaining`, `same_market_trailing_trade_count`) -- no
    trade list or close epoch is accepted here, so one market's activity
    can never leak into another's rejection decision."""
    rows = []
    for min_hours in hours_grid:
        for min_trades in trade_count_grid:
            kept = []
            rejected = 0
            for trip in trips:
                hours_remaining = trip.get("_entry_hours_remaining")
                activity = trip.get("same_market_trailing_trade_count")
                if hours_remaining is None or activity is None:
                    kept.append(trip)
                    continue
                if hours_remaining < min_hours or activity < min_trades:
                    rejected += 1
                    continue
                kept.append(trip)
            rows.append({
                "min_hours_remaining": min_hours,
                "min_same_market_trailing_trade_count": min_trades,
                "entries_rejected": rejected,
                "entries_kept": len(kept),
                **_portfolio_stats(kept),
            })
    return rows


def _portfolio_stats(trips: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(trip["pnl_usd"]) for trip in trips]
    profit = sum(p for p in pnls if p > 0)
    loss = sum(-p for p in pnls if p < 0)
    profit_factor = profit / loss if loss > 1e-12 else (math.inf if profit > 0 else 0.0)
    return {
        "round_trips": len(trips),
        "net_pnl_usd": sum(pnls),
        "profit_factor": profit_factor,
        "max_drawdown_usd": _max_drawdown(trips),
    }


def _max_drawdown(trips: list[dict[str, Any]]) -> float:
    ordered = sorted(trips, key=lambda trip: float(trip.get("closed_at_epoch", 0.0)))
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for trip in ordered:
        cumulative += float(trip["pnl_usd"])
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst


def revised_cohort_rows(
    trips: list[dict[str, Any]],
    *,
    order_shares_max: float = 1.0,
    cohort_min_round_trips: int = COHORT_MIN_ROUND_TRIPS,
    cohort_min_distinct_events: int = COHORT_MIN_DISTINCT_EVENTS,
    cohort_min_profit_factor: float = COHORT_MIN_PROFIT_FACTOR,
    cohort_max_drawdown_usd: float = COHORT_MAX_DRAWDOWN_USD,
    max_settlement_exit_rate: float = COHORT_MAX_SETTLEMENT_EXIT_RATE,
) -> list[dict[str, Any]]:
    """Cohort qualification using complete lifecycle P&L, profit factor,
    drawdown, and settlement-exit rate, gated first by the SAME 5-round-trip/
    2-distinct-event sample floor the live qualification gate itself
    requires (market_observation.py::_cohort_rows) -- not merely positive
    net P&L and average markout the way the live gate's cohort check does,
    and not a single-round-trip cohort mistaken for a repeatable edge.

    `order_shares_max` (a profile's uniform lot size, from its persisted
    ObservationProfileSpec -- 1.0 is a no-op for controlled) is used ONLY to
    normalize the drawdown bar for eligibility: COHORT_MAX_DRAWDOWN_USD was
    calibrated for 1-share economics, so a 17.5-share profile's raw
    drawdown must be divided down to the same per-share basis before
    comparing against it, or almost every cohort would be wrongly
    disqualified. Every row still reports both the raw dollar figures and a
    ONE-SHARE-EQUIVALENT pair -- a linear scaling estimate, not empirical
    proof of how a real 1-share order would have performed (queue
    position, partial fills, and available depth can behave differently at
    a smaller clip size, and this archive can't verify that)."""
    keys = {str(trip.get("cohort_key")) for trip in trips if trip.get("cohort_key")}
    rows = []
    for key in keys:
        cohort_trips = [trip for trip in trips if trip.get("cohort_key") == key]
        stats = _portfolio_stats(cohort_trips)
        divisor = order_shares_max if order_shares_max > 1e-9 else 1.0
        net_pnl_one_share_equivalent_usd = stats["net_pnl_usd"] / divisor
        max_drawdown_one_share_equivalent_usd = stats["max_drawdown_usd"] / divisor
        distinct_events = len({
            trip.get("event_id") for trip in cohort_trips if trip.get("event_id")
        })
        settlement_trips = sum(1 for trip in cohort_trips if trip.get("closure_type") == "settlement")
        settlement_exit_rate = settlement_trips / len(cohort_trips) if cohort_trips else 0.0
        eligible = (
            stats["round_trips"] >= cohort_min_round_trips
            and distinct_events >= cohort_min_distinct_events
            and stats["net_pnl_usd"] > 0
            and stats["profit_factor"] >= cohort_min_profit_factor
            and max_drawdown_one_share_equivalent_usd >= -cohort_max_drawdown_usd
            and settlement_exit_rate <= max_settlement_exit_rate
        )
        rows.append({
            "cohort_key": key,
            "distinct_event_count": distinct_events,
            "settlement_exit_rate": settlement_exit_rate,
            "revised_eligible": eligible,
            "net_pnl_one_share_equivalent_usd": net_pnl_one_share_equivalent_usd,
            "max_drawdown_one_share_equivalent_usd": max_drawdown_one_share_equivalent_usd,
            **stats,
        })
    return sorted(
        rows, key=lambda row: (row["revised_eligible"], row["net_pnl_usd"]), reverse=True,
    )


def _verdict_block(baseline: dict[str, Any], escalation_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Separates what this replay can and cannot conclude. See module
    docstring: the strongest supportable claim is ever "this specific
    passive-escalation policy remains negative under optimistic full-fill
    assumptions over a complete evidence window" -- never "no exit policy
    could work," and never anything about the pilot gate."""
    baseline_shadow_status = "NEGATIVE" if baseline["net_pnl_usd"] <= 0 else "POSITIVE"
    status_counts: dict[str, int] = {}
    for row in escalation_rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1

    if not escalation_rows:
        # No forced-exit or settlement trip existed for this (profile,
        # strategy) at all -- the passive-escalation policy was never
        # exercised, so a negative baseline must not be misread as this
        # policy having been tried and rejected.
        passive_replay_status = "NOT_APPLICABLE"
        optimistic_passive_total = None
    else:
        optimistic_passive_total = baseline["net_pnl_usd"] + sum(
            row["optimistic_pnl_delta_usd"] for row in escalation_rows
            if row["status"] == STATUS_CROSS_OBSERVED
        )
        if any(row["status"] in _UNKNOWN_STATUSES for row in escalation_rows):
            passive_replay_status = "INCONCLUSIVE"
        elif optimistic_passive_total <= 0:
            passive_replay_status = "REJECTED"
        else:
            passive_replay_status = "INCONCLUSIVE"

    return {
        "baseline_shadow_status": baseline_shadow_status,
        "passive_replay_status": passive_replay_status,
        "taker_replay_status": "UNKNOWN",
        "pilot_unlock_authorized": False,
        "optimistic_passive_total_usd": optimistic_passive_total,
        "escalation_status_counts": status_counts,
    }


# Fallback QualificationPolicy fields for a pre-v5 archive that never
# persisted one -- mirrors market_observation.py's own defaults
# (observation_controlled_min_round_trips=20 etc.) as of this writing. A
# v5+ archive always has its own persisted policy and never uses this.
_FALLBACK_QUALIFICATION_POLICY: dict[str, Any] = {
    "primary_strategy": PRIMARY_STRATEGY,
    "min_round_trips": 20,
    "min_distinct_events": 5,
    "min_profit_factor": 1.20,
    "max_drawdown_usd": 3.0,
    "max_event_profit_concentration": 0.50,
    "cohort_min_round_trips": COHORT_MIN_ROUND_TRIPS,
    "cohort_min_distinct_events": COHORT_MIN_DISTINCT_EVENTS,
    "cohort_min_profit_factor": COHORT_MIN_PROFIT_FACTOR,
    "cohort_max_drawdown_usd": COHORT_MAX_DRAWDOWN_USD,
    "max_settlement_exit_rate": COHORT_MAX_SETTLEMENT_EXIT_RATE,
    "min_avg_markout_5m_cents": MIN_AVG_MARKOUT_5M_CENTS,
}


def _event_profit_concentration(trips: list[dict[str, Any]]) -> float:
    event_profit: dict[str, float] = {}
    gross_profit = 0.0
    for trip in trips:
        pnl = float(trip["pnl_usd"])
        if pnl > 0:
            gross_profit += pnl
            event_id = trip.get("event_id")
            event_profit[event_id] = event_profit.get(event_id, 0.0) + pnl
    if gross_profit <= 1e-12 or not event_profit:
        return 0.0
    return max(event_profit.values()) / gross_profit


def _profile_completion_summary(state: dict[str, Any], profile: str) -> dict[str, Any]:
    """Pure, read-only computation of the completion/inventory fields
    profile_follow_up_status() needs, sourced directly from the loaded
    archive. Deliberately independent of
    MarketObservationTracker.profile_summary() (a method on the live,
    stateful tracker) -- offline replay must never construct that
    tracker, so this reimplements only the handful of fields actually
    needed, reading the same underlying persisted data."""
    markets = ((state.get("profiles") or {}).get(profile) or {}).get("markets") or {}
    feed_minute_buckets = state.get("feed_minute_buckets") or {}
    healthy_feed_seconds = len(feed_minute_buckets) * 60.0
    target_seconds = float(state.get("evaluation_healthy_feed_target_seconds") or 0.0)

    open_inventory_count = 0
    open_shadow_strategy_positions = 0
    markouts_5m: list[float] = []
    for market in markets.values():
        fills = market.get("hypothetical_fills", [])
        primary_fills = [
            fill for fill in fills
            if fill.get("strategy") == PRIMARY_STRATEGY and fill.get("admissible")
        ]
        if abs(float(_paper_position_state(primary_fills)["position"])) > 1e-9:
            open_inventory_count += 1
        for strategy in SHADOW_STRATEGIES:
            strategy_fills = [
                fill for fill in fills
                if fill.get("strategy") == strategy and fill.get("admissible")
            ]
            if abs(float(_paper_position_state(strategy_fills)["position"])) > 1e-9:
                open_shadow_strategy_positions += 1
        markouts_5m.extend(
            float(fill["markout_5m_cents"])
            for fill in primary_fills
            if fill.get("markout_5m_cents") is not None
            and fill.get("liquidity_role") != "settlement"
        )

    finalization = state.get("evaluation_finalization") or {}
    return {
        "healthy_feed_hours": healthy_feed_seconds / 3600.0,
        "healthy_feed_target_hours": target_seconds / 3600.0,
        "remaining_healthy_feed_hours": (
            max(0.0, target_seconds - healthy_feed_seconds) / 3600.0
        ),
        "open_inventory_count": open_inventory_count,
        "open_shadow_strategy_positions": open_shadow_strategy_positions,
        "avg_markout_5m_cents": (
            sum(markouts_5m) / len(markouts_5m) if markouts_5m else None
        ),
        "evaluation_finalization_complete": bool(finalization.get("complete")),
    }


def profile_follow_up_status(
    *,
    trips: list[dict[str, Any]],
    baseline: dict[str, Any],
    cohorts: list[dict[str, Any]],
    verdict: dict[str, Any],
    completion: dict[str, Any],
    order_shares_max: float,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """The strengthened follow-up gate -- call this ONLY with the archive's
    frozen policy["primary_strategy"]'s own trips/baseline/cohorts/verdict
    (never scanned across all four SHADOW_STRATEGIES and reported for
    whichever looks best, which would be post-hoc strategy selection).
    Every threshold is read from the persisted QualificationPolicy dict,
    never from current module/config constants, so a later change to those
    can never retroactively change the verdict already-collected evidence
    produces.

    Returns FOLLOW_UP_CANDIDATE or NOT_YET_QUALIFIED with every unmet
    reason listed -- never "eligible," and never anything readable as
    trading permission. This profile is permanently SHADOW_ONLY/
    pilot_unlocked=False by construction in market_observation.py; this
    function has no bearing on that gate either way."""
    divisor = order_shares_max if order_shares_max > 1e-9 else 1.0
    net_pnl_one_share_equivalent_usd = baseline["net_pnl_usd"] / divisor
    max_drawdown_one_share_equivalent_usd = baseline["max_drawdown_usd"] / divisor
    distinct_events = len({
        trip.get("event_id") for trip in trips if trip.get("event_id")
    })
    settlement_trips = sum(1 for trip in trips if trip.get("closure_type") == "settlement")
    settlement_exit_rate = settlement_trips / len(trips) if trips else 0.0
    concentration = _event_profit_concentration(trips)
    avg_markout = completion.get("avg_markout_5m_cents")

    reasons: list[str] = []
    if not completion["evaluation_finalization_complete"]:
        reasons.append("evaluation not finalized")
    if completion["healthy_feed_hours"] + 1e-9 < completion["healthy_feed_target_hours"]:
        reasons.append("insufficient healthy feed coverage")
    if baseline["round_trips"] < policy["min_round_trips"]:
        reasons.append(f"fewer than {policy['min_round_trips']} completed round trips")
    if distinct_events < policy["min_distinct_events"]:
        reasons.append(f"fewer than {policy['min_distinct_events']} distinct events")
    if net_pnl_one_share_equivalent_usd <= 0:
        reasons.append("one-share-equivalent net P&L is not positive")
    if baseline["profit_factor"] < policy["min_profit_factor"]:
        reasons.append(f"profit factor below {policy['min_profit_factor']}")
    if max_drawdown_one_share_equivalent_usd < -policy["max_drawdown_usd"]:
        reasons.append(
            f"one-share-equivalent drawdown exceeds ${policy['max_drawdown_usd']:.2f}"
        )
    if concentration > policy["max_event_profit_concentration"]:
        reasons.append(
            f"event profit concentration exceeds "
            f"{policy['max_event_profit_concentration']:.0%}"
        )
    if not any(row.get("revised_eligible") for row in cohorts):
        reasons.append("no revised-eligible cohort")
    if avg_markout is None or avg_markout < policy["min_avg_markout_5m_cents"]:
        reasons.append("5-minute markout sample is missing or negative")
    if completion["open_inventory_count"] != 0:
        reasons.append("primary portfolio still has open inventory")
    if completion["open_shadow_strategy_positions"] != 0:
        reasons.append("a shadow strategy variant still has open inventory")
    if settlement_exit_rate > policy["max_settlement_exit_rate"]:
        reasons.append(
            f"portfolio settlement-exit rate exceeds "
            f"{policy['max_settlement_exit_rate']:.0%}"
        )
    if verdict.get("passive_replay_status") == "REJECTED":
        reasons.append("passive replay status is REJECTED")

    return {
        "status": NOT_YET_QUALIFIED if reasons else FOLLOW_UP_CANDIDATE,
        "blocked_reasons": reasons,
        "primary_strategy": policy["primary_strategy"],
        "net_pnl_one_share_equivalent_usd": net_pnl_one_share_equivalent_usd,
        "max_drawdown_one_share_equivalent_usd": max_drawdown_one_share_equivalent_usd,
        "distinct_event_count": distinct_events,
        "settlement_exit_rate": settlement_exit_rate,
        "event_profit_concentration": concentration,
    }


def _fallback_max_started_event_hours(
    profile: str, qualification_config: dict[str, Any],
) -> float:
    """Only used for a pre-v5 archive with no persisted spec for this
    profile."""
    if profile == PROFILE_LEGACY:
        return LEGACY_MAX_STARTED_EVENT_HOURS
    if profile == PROFILE_CONTROLLED:
        value = qualification_config.get("controlled_max_started_event_hours")
        return (
            float(value) if value is not None
            else CONTROLLED_MAX_STARTED_EVENT_HOURS_DEFAULT
        )
    return CONTROLLED_MAX_STARTED_EVENT_HOURS_DEFAULT


def run_replay(path: Optional[Path] = None) -> dict[str, Any]:
    """Top-level orchestration: read-only, never mutates the archive.
    Returns a report combining the escalation replay, the hard-exit-rule
    check, the entry-filter sweep, revised cohort qualification, the
    verdict block, and (for the frozen primary strategy only) the
    follow-up gate, per (profile, strategy)."""
    state = load_archive(path)
    profiles = state.get("profiles") or {}
    qualification_config = state.get("qualification_config") or {}
    feed_minute_buckets = state.get("feed_minute_buckets") or {}
    policy: dict[str, Any] = state.get("qualification_policy") or _FALLBACK_QUALIFICATION_POLICY
    primary_strategy = policy.get("primary_strategy", PRIMARY_STRATEGY)

    # Only _risk_deadline_epoch's controlled-formula branch ever reads
    # these two, regardless of which profile is currently being evaluated.
    controlled_max_holding_hours = _spec_field(
        state, PROFILE_CONTROLLED, "liquidation_max_holding_hours",
        CONTROLLED_MAX_HOLDING_HOURS,
    )
    controlled_entry_cutoff_minutes = _spec_field(
        state, PROFILE_CONTROLLED, "entry_cutoff_minutes",
        CONTROLLED_ENTRY_CUTOFF_MINUTES,
    )
    config_theta = qualification_config.get("maker_fee_theta")
    maker_fee_theta_fallback = (
        float(config_theta) if config_theta is not None else MAKER_FEE_THETA_DEFAULT
    )

    report: dict[str, Any] = {"profiles": {}}
    for profile in OBSERVATION_PROFILES:
        markets = (profiles.get(profile) or {}).get("markets") or {}
        max_started_event_hours = _spec_field(
            state, profile, "max_started_event_hours",
            _fallback_max_started_event_hours(profile, qualification_config),
        )
        maker_fee_theta = _spec_field(
            state, profile, "maker_fee_theta", maker_fee_theta_fallback,
        )
        # 1.0 fallback is deliberate for a pre-v5 archive: it reproduces
        # exactly the un-normalized figures already reported before this
        # feature existed, rather than silently re-scaling past results.
        order_shares_max = _spec_field(state, profile, "order_shares_max", 1.0)
        hours_grid = (
            CONTROLLED_HOURS_GRID if profile == PROFILE_CONTROLLED else LEGACY_HOURS_GRID
        )
        completion = _profile_completion_summary(state, profile)

        strategy_report: dict[str, Any] = {}
        for strategy in SHADOW_STRATEGIES:
            all_trips: list[dict[str, Any]] = []
            escalation_rows: list[dict[str, Any]] = []
            hard_rule_rows: list[dict[str, Any]] = []
            for slug, market in markets.items():
                fills = [
                    fill for fill in market.get("hypothetical_fills", [])
                    if fill.get("strategy") == strategy and fill.get("admissible")
                ]
                if not fills:
                    continue
                trades = market.get("trades", [])
                event_id = str(market.get("event_id") or f"unknown:{slug}")
                event_epoch = _positive_float(market.get("event_or_close_epoch"))
                last_observed_activity_epoch = _max_optional(
                    market.get("last_book_epoch"),
                    market.get("last_trade_at_epoch"),
                    market.get("last_observed_at_epoch"),
                )

                trips = _round_trips_with_context(fills, slug=slug, event_id=event_id)
                for trip in trips:
                    trip["market_slug"] = slug
                    entry = trip.get("entry_fill")
                    if entry is None:
                        continue
                    entry_epoch = float(entry.get("observed_at_epoch", 0.0))
                    opened_at_epoch = float(trip.get("opened_at_epoch", entry_epoch))
                    deadline = _risk_deadline_epoch(
                        profile, entry_epoch, opened_at_epoch, event_epoch,
                        max_started_event_hours=max_started_event_hours,
                        controlled_max_holding_hours=controlled_max_holding_hours,
                        controlled_entry_cutoff_minutes=controlled_entry_cutoff_minutes,
                    )
                    trip["_entry_hours_remaining"] = (
                        math.inf if deadline is None
                        else max(0.0, (deadline - entry_epoch) / 3600.0)
                    )
                    trip["same_market_trailing_trade_count"] = trailing_trade_activity(
                        trades, entry_epoch,
                    )
                all_trips.extend(trips)

                escalation_rows.extend(replay_exit_escalation(
                    trips, trades,
                    profile=profile,
                    event_epoch=event_epoch,
                    max_started_event_hours=max_started_event_hours,
                    controlled_max_holding_hours=controlled_max_holding_hours,
                    controlled_entry_cutoff_minutes=controlled_entry_cutoff_minutes,
                    last_observed_activity_epoch=last_observed_activity_epoch,
                    feed_minute_buckets=feed_minute_buckets,
                    maker_fee_theta=maker_fee_theta,
                ))
                hard_rule_rows.extend(replay_hard_pre_settlement_exit(
                    trips, trades,
                    last_observed_activity_epoch=last_observed_activity_epoch,
                    feed_minute_buckets=feed_minute_buckets,
                ))

            baseline = _portfolio_stats(all_trips)
            divisor = order_shares_max if order_shares_max > 1e-9 else 1.0
            baseline["net_pnl_one_share_equivalent_usd"] = (
                baseline["net_pnl_usd"] / divisor
            )
            baseline["max_drawdown_one_share_equivalent_usd"] = (
                baseline["max_drawdown_usd"] / divisor
            )
            cohorts = revised_cohort_rows(
                all_trips,
                order_shares_max=order_shares_max,
                cohort_min_round_trips=policy.get(
                    "cohort_min_round_trips", COHORT_MIN_ROUND_TRIPS,
                ),
                cohort_min_distinct_events=policy.get(
                    "cohort_min_distinct_events", COHORT_MIN_DISTINCT_EVENTS,
                ),
                cohort_min_profit_factor=policy.get(
                    "cohort_min_profit_factor", COHORT_MIN_PROFIT_FACTOR,
                ),
                cohort_max_drawdown_usd=policy.get(
                    "cohort_max_drawdown_usd", COHORT_MAX_DRAWDOWN_USD,
                ),
                max_settlement_exit_rate=policy.get(
                    "max_settlement_exit_rate", COHORT_MAX_SETTLEMENT_EXIT_RATE,
                ),
            )
            verdict = _verdict_block(baseline, escalation_rows)
            strategy_report[strategy] = {
                "baseline": baseline,
                "escalation_replay": escalation_rows,
                "hard_pre_settlement_exit": hard_rule_rows,
                "entry_filter_sweep": sweep_entry_filters(all_trips, hours_grid=hours_grid),
                "revised_cohorts": cohorts,
                "verdict": verdict,
            }
            if strategy == primary_strategy:
                strategy_report[strategy]["follow_up"] = profile_follow_up_status(
                    trips=all_trips,
                    baseline=baseline,
                    cohorts=cohorts,
                    verdict=verdict,
                    completion=completion,
                    order_shares_max=order_shares_max,
                    policy=policy,
                )
        # "_completion" (leading underscore, never a real strategy name) --
        # profile-level, sibling to the per-strategy entries: remaining
        # healthy-feed hours and open-inventory counts, per the plan's
        # "surface clearly in both text and JSON" requirement.
        strategy_report["_completion"] = completion
        report["profiles"][profile] = strategy_report
    return report
