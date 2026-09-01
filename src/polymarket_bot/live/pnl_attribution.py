"""Multi-dimensional realized-P&L reporting built on top of
live/lot_accounting.py's FIFO matching output -- the same relationship
live/family_performance.py has to live/fills.py. Pure -- no I/O; callers
fetch fills/settlements and run them through lot_accounting.compute_lots()
first (see main.py::cmd_live_pnl). See live/RUNBOOK.md's most recent
section.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Union

from .family_performance import classify_family
from .fills import parse_transact_time
from .lot_accounting import ClosedLot, LotAccountingResult, OpenLot

PRICE_BANDS = (
    (0.00, 0.10, "0-10c (deep longshot)"),
    (0.10, 0.25, "10-25c (longshot)"),
    (0.25, 0.40, "25-40c (lean-no)"),
    (0.40, 0.60, "40-60c (coinflip)"),
    (0.60, 0.75, "60-75c (lean-yes)"),
    (0.75, 0.90, "75-90c (favorite)"),
    (0.90, 1.01, "90-100c (deep favorite)"),  # upper bound inclusive of 1.00
)

ENTRY_TIME_BANDS_UTC = (
    (0, 6, "00-06 UTC"),
    (6, 12, "06-12 UTC"),
    (12, 18, "12-18 UTC"),
    (18, 24, "18-24 UTC"),
)


def capital_hours(lot: Union[ClosedLot, OpenLot], now: Optional[str] = None) -> float:
    """(shares * open_price * holding_seconds) / 3600.0. ClosedLot uses its
    own stored holding_seconds (exact for closed_via=="fill"; a midpoint
    point-estimate for closed_via=="settlement" -- see
    ClosedLot.holding_seconds_lower/_upper for the real bound).  OpenLot
    recomputes now - open_time at call time (not persisted -- an open
    position's capital-hours keeps growing) -- `now` defaults to the real
    current time, overridable for deterministic reporting/testing."""
    if isinstance(lot, ClosedLot):
        shares = lot.shares_matched
        holding_seconds = lot.holding_seconds
    else:
        shares = lot.shares_open
        open_dt = parse_transact_time(lot.open_time)
        now_dt = parse_transact_time(now) or datetime.now(timezone.utc)
        holding_seconds = (now_dt - open_dt).total_seconds() if open_dt is not None else 0.0
    return (shares * lot.open_price * holding_seconds) / 3600.0


def price_band_for(price: float) -> str:
    for low, high, label in PRICE_BANDS:
        if low <= price < high:
            return label
    return "unknown"


def entry_time_band_for(open_time: Optional[str]) -> str:
    dt = parse_transact_time(open_time)
    if dt is None:
        return "unknown"
    hour = dt.astimezone(timezone.utc).hour
    for low, high, label in ENTRY_TIME_BANDS_UTC:
        if low <= hour < high:
            return label
    return "unknown"


def entry_date_for(open_time: Optional[str]) -> str:
    """Calendar-date bucket -- a near-zero-cost companion to
    entry_time_band_for. With only a handful of days of real trading
    history, day-over-day trend is likely more informative than hour-of-
    day; costs nothing extra to add alongside it."""
    dt = parse_transact_time(open_time)
    if dt is None:
        return "unknown"
    return dt.astimezone(timezone.utc).date().isoformat()


@dataclass
class PnlBreakdown:
    bucket: str
    lot_count: int
    realized_pnl_usd: float
    commission_usd: float
    win_count: int
    loss_count: int
    win_rate: Optional[float]  # None when no lot has a nonzero net_pnl_usd
    avg_holding_hours: Optional[float]
    capital_hours: float
    pnl_per_capital_hour: Optional[float]


def compute_pnl_breakdown(
    closed_lots: list[ClosedLot], key_fn: Callable[[ClosedLot], str],
) -> list[PnlBreakdown]:
    """Generic reducer -- every breakdown dimension below is a thin wrapper
    around this with a different key_fn. Most-profitable bucket first."""
    buckets: dict[str, dict] = {}
    for lot in closed_lots:
        bucket_key = key_fn(lot)
        bucket = buckets.setdefault(bucket_key, {
            "lot_count": 0, "realized_pnl_usd": 0.0, "commission_usd": 0.0,
            "win_count": 0, "loss_count": 0, "holding_hours_sum": 0.0, "capital_hours": 0.0,
        })
        bucket["lot_count"] += 1
        bucket["realized_pnl_usd"] += lot.net_pnl_usd
        bucket["commission_usd"] += lot.open_commission_usd + lot.close_commission_usd
        if lot.net_pnl_usd > 0:
            bucket["win_count"] += 1
        elif lot.net_pnl_usd < 0:
            bucket["loss_count"] += 1
        bucket["holding_hours_sum"] += lot.holding_seconds / 3600.0
        bucket["capital_hours"] += capital_hours(lot)

    breakdowns = []
    for bucket_key, bucket in buckets.items():
        decided = bucket["win_count"] + bucket["loss_count"]
        breakdowns.append(PnlBreakdown(
            bucket=bucket_key,
            lot_count=bucket["lot_count"],
            realized_pnl_usd=bucket["realized_pnl_usd"],
            commission_usd=bucket["commission_usd"],
            win_count=bucket["win_count"],
            loss_count=bucket["loss_count"],
            win_rate=(bucket["win_count"] / decided) if decided > 0 else None,
            avg_holding_hours=(bucket["holding_hours_sum"] / bucket["lot_count"]) if bucket["lot_count"] > 0 else None,
            capital_hours=bucket["capital_hours"],
            pnl_per_capital_hour=(
                bucket["realized_pnl_usd"] / bucket["capital_hours"] if bucket["capital_hours"] > 0 else None
            ),
        ))
    return sorted(breakdowns, key=lambda b: b.realized_pnl_usd, reverse=True)


def compute_pnl_by_family(closed_lots: list[ClosedLot]) -> list[PnlBreakdown]:
    return compute_pnl_breakdown(closed_lots, lambda lot: classify_family(lot.market_slug))


def compute_pnl_by_event(closed_lots: list[ClosedLot]) -> list[PnlBreakdown]:
    return compute_pnl_breakdown(closed_lots, lambda lot: lot.event_bucket_key or "unknown")


def compute_pnl_by_price_band(closed_lots: list[ClosedLot]) -> list[PnlBreakdown]:
    return compute_pnl_breakdown(closed_lots, lambda lot: price_band_for(lot.open_price))


def compute_pnl_by_entry_time_band(closed_lots: list[ClosedLot]) -> list[PnlBreakdown]:
    return compute_pnl_breakdown(closed_lots, lambda lot: entry_time_band_for(lot.open_time))


def compute_pnl_by_entry_date(closed_lots: list[ClosedLot]) -> list[PnlBreakdown]:
    return compute_pnl_breakdown(closed_lots, lambda lot: entry_date_for(lot.open_time))


@dataclass
class PnlSummary:
    total_realized_pnl_usd: float
    total_commission_usd: float
    closed_lot_count: int
    open_lot_count: int
    closed_via_fill_count: int
    closed_via_settlement_count: int
    presumed_settled_unresolved_count: int
    closed_capital_hours: float
    open_capital_hours: float
    # realized P&L / closed_capital_hours ONLY -- deliberately excludes
    # open_capital_hours from the denominator, since an open lot's ongoing
    # capital-hours have no realized P&L yet to pair them with; blending
    # the two would artificially depress this figure early in the bot's
    # life whenever a lot of capital is still deployed in open positions.
    overall_pnl_per_capital_hour: Optional[float]


def compute_pnl_summary(result: LotAccountingResult, now: Optional[str] = None) -> PnlSummary:
    total_realized = sum(lot.net_pnl_usd for lot in result.closed_lots)
    total_commission = sum(lot.open_commission_usd + lot.close_commission_usd for lot in result.closed_lots)
    closed_capital_hours = sum(capital_hours(lot) for lot in result.closed_lots)
    open_capital_hours = sum(capital_hours(lot, now=now) for lot in result.open_lots)

    return PnlSummary(
        total_realized_pnl_usd=total_realized,
        total_commission_usd=total_commission,
        closed_lot_count=len(result.closed_lots),
        open_lot_count=len(result.open_lots),
        closed_via_fill_count=sum(1 for lot in result.closed_lots if lot.closed_via == "fill"),
        closed_via_settlement_count=sum(1 for lot in result.closed_lots if lot.closed_via == "settlement"),
        presumed_settled_unresolved_count=sum(1 for lot in result.open_lots if lot.presumed_settled),
        closed_capital_hours=closed_capital_hours,
        open_capital_hours=open_capital_hours,
        overall_pnl_per_capital_hour=(
            total_realized / closed_capital_hours if closed_capital_hours > 0 else None
        ),
    )
