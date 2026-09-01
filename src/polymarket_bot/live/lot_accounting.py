"""Pure FIFO lot-matching engine -- no I/O, no network calls.

Fill-based, not weighted-average: each open increment keeps its own price
and timestamp, required for capital-hours and entry-time-band attribution
(see live/pnl_attribution.py) -- weighted-average would blend multiple
opens into one running cost basis and destroy that per-lot identity.

Position identity is (market_slug, outcome), NOT market_slug alone --
confirmed real fills exist on both the Yes-token and No-token of the same
market_slug (see position_key). Netting them together would silently
misrepresent two economically distinct instruments as one.

compute_lots() is deliberately NOT persisted anywhere (no lots.json) --
it's cheap enough to recompute fresh from fills.json + settlements.json on
every call, same "pure, recomputed on demand" pattern already used by
live/family_performance.py::compute_family_performance and
live/event_exposure.py::compute_event_exposures.

See live/RUNBOOK.md's most recent section.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from .event_exposure import derive_event_bucket_key
from .fills import parse_transact_time

# Empty: no order.intent value has actually been confirmed to reliably mean
# "this fill reduces a pre-existing position." ORDER_INTENT_SELL_LONG was
# assumed to mean that, but real fill history disproved it: EVERY one of
# the 110 fills this produced as "unmatched" carried that exact intent, and
# 58 of them were the first-ever fill recorded for their (market_slug,
# outcome) -- i.e. selling to OPEN a fresh short (completely normal
# two-sided market-making), not reducing anything. This exchange appears to
# use SELL_LONG to mean "selling the long-side (YES) token," not "closing a
# position you hold." Treating those as opens instead recovered $7+ of real,
# previously-hidden realized P&L (see live/RUNBOOK.md's most recent
# section). Deliberately NOT guessing at a replacement signal -- an
# empty-deque fill with no recognized reducing intent is treated as a
# normal new-open (see _process_fill's rule 3), which is now unconditional
# until a genuinely reliable intent value is found.
_REDUCING_INTENTS: frozenset[str] = frozenset()


@dataclass
class ClosedLot:
    lot_id: str
    market_slug: str
    outcome: Optional[str]
    event_bucket_key: Optional[str]
    open_fill_id: str
    open_order_id: Optional[str]
    open_side: str  # "BUY" (long) or "SELL" (short)
    open_price: float
    open_time: Optional[str]
    close_fill_id: Optional[str]  # None when closed_via == "settlement"
    close_order_id: Optional[str]
    close_price: Optional[float]  # None when closed_via == "settlement"
    close_time: Optional[str]
    closed_via: str  # "fill" | "settlement"
    shares_matched: float
    gross_pnl_usd: float
    open_commission_usd: float
    close_commission_usd: float
    net_pnl_usd: float
    holding_seconds: float
    settlement_confidence: Optional[str] = None
    # Only populated for closed_via == "settlement" -- the true resolution
    # moment lies somewhere between last_seen_at and detected_at, so
    # holding_seconds above is the midpoint POINT ESTIMATE, and these two
    # preserve the real bound rather than presenting false precision.
    holding_seconds_lower: Optional[float] = None
    holding_seconds_upper: Optional[float] = None


@dataclass
class OpenLot:
    market_slug: str
    outcome: Optional[str]
    event_bucket_key: Optional[str]
    open_fill_id: str
    open_order_id: Optional[str]
    open_side: str
    open_price: float
    open_time: Optional[str]
    shares_open: float
    open_commission_usd: float
    # True once a settlement WAS detected for this (market_slug, outcome)
    # but proceeds couldn't be determined (confidence == "inferred_low") --
    # structurally still "open" (no number to close it with) but must be
    # reported separately from genuinely live risk.
    presumed_settled: bool = False
    settlement_id: Optional[str] = None


@dataclass
class LotAccountingResult:
    closed_lots: list[ClosedLot] = field(default_factory=list)
    open_lots: list[OpenLot] = field(default_factory=list)
    # Fills that reduce a pre-existing position this bot's own fill history
    # never recorded the opening of (predates tracking, or a gap) -- never
    # silently fabricated into a lot; kept here for visibility instead.
    unmatched_closing_fills: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def position_key(fill: dict[str, Any]) -> tuple[str, Optional[str]]:
    """(market_slug, outcome) -- market_slug ALONE is not a safe position
    identity: confirmed real fills exist on both outcome tokens of the same
    slug. See module docstring."""
    return fill.get("market_slug") or "", fill.get("outcome")


class _OpenSlice:
    """Internal mutable working state for one still-open increment within a
    (market_slug, outcome) FIFO deque -- promoted to an OpenLot only once
    every fill has been processed and any remainder is known."""

    __slots__ = (
        "market_slug", "outcome", "event_bucket_key", "side", "price", "time",
        "fill_id", "order_id", "commission_usd", "shares_total", "shares_remaining",
    )

    def __init__(self, fill: dict[str, Any], shares: float):
        self.market_slug = fill.get("market_slug") or ""
        self.outcome = fill.get("outcome")
        self.event_bucket_key = _event_bucket_key_from_fill(fill)
        self.side = fill.get("side")
        self.price = fill.get("price")
        self.time = fill.get("transact_time")
        self.fill_id = fill.get("fill_id")
        self.order_id = fill.get("order_id")
        self.commission_usd = fill.get("commission_usd") or 0.0
        self.shares_total = fill.get("shares") or 0.0
        self.shares_remaining = shares


def _event_bucket_key_from_fill(fill: dict[str, Any]) -> Optional[str]:
    """Real eventSlug ground truth, when present, lives at
    raw_execution["order"]["marketMetadata"]["eventSlug"] on a fill -- NOT
    at the top level the way live/event_exposure.py's derive_event_bucket_key
    normally expects a market-listing raw dict. marketMetadata's own key
    names (eventSlug/eventId) happen to match that function's lookup
    exactly, so it's passed straight through as the "raw" argument. This is
    strictly better ground truth than a live refresh cycle gets (which
    falls back to the slug heuristic outside a live cycle's own raw scan)."""
    market_slug = fill.get("market_slug")
    if not market_slug:
        return None
    order = (fill.get("raw_execution") or {}).get("order")
    market_metadata = order.get("marketMetadata") if isinstance(order, dict) else None
    return derive_event_bucket_key(
        market_slug, market_metadata if isinstance(market_metadata, dict) else None,
    )


def _reduces_untracked_position(fill: dict[str, Any]) -> bool:
    order = (fill.get("raw_execution") or {}).get("order")
    intent = order.get("intent") if isinstance(order, dict) else None
    return intent in _REDUCING_INTENTS


def _predates_known_snapshot(
    fill: dict[str, Any],
    earliest_snapshot_by_slug: dict[str, dict[str, Any]],
    slug_first_fill_time: dict[str, str],
) -> bool:
    """True when the earliest position_snapshots.json row recorded for this
    fill's market already shows a nonzero position at or before this fill's
    own transact_time -- real (if partial) evidence of inventory this bot's
    fill history doesn't explain, not just an absence of evidence. Position
    snapshots are per-market_slug only (no outcome breakdown -- see
    us_client.py::get_all_positions), so this can't tell which outcome held
    the pre-existing inventory; it conservatively applies to any fill on
    that slug.

    Only checked for a slug's OWN chronologically-first fill across every
    outcome (slug_first_fill_time, computed once in compute_lots from the
    full fills list) -- NOT every later empty-deque fill. A real, confirmed
    false positive without this restriction: a position can legitimately
    flip through flat and reopen (a full round trip fills.json itself
    fully explains -- SELL 6.5, BUY 6.5 closing it, SELL 6.0 reopening),
    and a stale snapshot from BEFORE that round trip closed would otherwise
    wrongly flag the reopen as ambiguous even though fills.json already
    accounts for everything. Only the slug's true first-ever fill has no
    such fills.json-internal explanation available for an empty deque.

    Coverage-limited: position_snapshots.json only has history from when
    that feature was built, so a slug with no snapshot at all (the common
    case for older fills) is NOT flagged -- absence of a snapshot is not
    evidence of absence of prior inventory, it's simply unknown, and
    _REDUCING_INTENTS being empty already accepts that residual risk
    deliberately (see this module's docstring)."""
    slug = fill.get("market_slug")
    if not slug:
        return False
    if fill.get("transact_time") != slug_first_fill_time.get(slug):
        return False
    snapshot = earliest_snapshot_by_slug.get(slug)
    if not snapshot:
        return False
    try:
        net_position = float(snapshot.get("net_position_shares") or 0.0)
    except (TypeError, ValueError):
        return False
    if net_position == 0.0:
        return False
    fill_dt = parse_transact_time(fill.get("transact_time"))
    snapshot_dt = parse_transact_time(snapshot.get("timestamp"))
    if fill_dt is None or snapshot_dt is None:
        # Can't confirm the snapshot predates this fill, but a nonzero
        # snapshot for this slug existing at all is still real evidence --
        # never silently discard it just because timing is unparseable.
        return True
    return snapshot_dt <= fill_dt


def _allocate_commission(fill_commission_usd: Optional[float], matched: float, fill_total_shares: float) -> float:
    if fill_commission_usd is None or not fill_total_shares:
        return 0.0
    return fill_commission_usd * (matched / fill_total_shares)


def _sort_key(fill: dict[str, Any]) -> tuple:
    parsed = parse_transact_time(fill.get("transact_time"))
    # Missing/unparseable transact_time sorts first (conservative: treated
    # as the earliest, oldest data) -- compute_lots() records a warning
    # rather than silently mis-ordering without a trace.
    return (parsed is None, parsed or "", fill.get("fill_id") or "")


def compute_lots(
    fills: list[dict[str, Any]],
    settlements: Optional[list[dict[str, Any]]] = None,
    earliest_snapshot_by_slug: Optional[dict[str, dict[str, Any]]] = None,
) -> LotAccountingResult:
    """Pure -- no I/O. `fills`/`settlements` are plain dicts shaped like
    FillRecord.to_dict() / one settlements.json row (e.g. straight from
    fills.py::get_all_fills() / settlements.py::get_all_settlements()).
    `earliest_snapshot_by_slug`, if provided, is
    settlements.get_earliest_snapshot_by_slug()'s output -- the
    pre-tracking-inventory safety net described in rule 3 below. Omitting
    it (default None) is safe and reproduces the exact prior behavior;
    every existing caller that doesn't pass it is unaffected.

    Processes fills in transact_time order (ascending, tie-broken by
    fill_id) through one FIFO deque per (market_slug, outcome):
    1. Opposite-side fill against a non-empty deque -> closes the oldest
       slice(s) first. If the closing fill's shares exceed total open
       inventory, the excess becomes a NEW opposite-side open slice -- a
       normal flip-through-flat for a two-sided market maker, not an error.
    2. Same-side fill -> a new open slice (position growing).
    3. Fill against an EMPTY deque: if order.intent indicates it reduces a
       pre-existing position this bot's own fill history never recorded
       opening (predates tracking, or a gap), OR the earliest recorded
       position snapshot for this market already shows nonzero inventory
       at or before this fill (see _predates_known_snapshot), the whole
       fill is routed to unmatched_closing_fills rather than fabricated
       into a lot. Otherwise it's a genuine new opening slice.

    `settlements`, if provided, closes out remaining open inventory for any
    (market_slug, outcome) a settlement event covers -- see
    _apply_settlements."""
    result = LotAccountingResult()
    deques: dict[tuple[str, Optional[str]], "deque[_OpenSlice]"] = {}
    earliest_snapshot_by_slug = earliest_snapshot_by_slug or {}

    usable_fills = []
    for fill in fills:
        if (
            not fill.get("market_slug")
            or fill.get("side") not in ("BUY", "SELL")
            or fill.get("price") is None
            or not fill.get("shares")
        ):
            result.warnings.append(
                f"Fill {fill.get('fill_id')} missing market_slug/side/price/shares -- "
                "excluded from lot accounting."
            )
            continue
        usable_fills.append(fill)

    sorted_fills = sorted(usable_fills, key=_sort_key)
    # First (chronologically) fill per market_slug across BOTH outcomes --
    # see _predates_known_snapshot for why the snapshot safety net is
    # restricted to only this fill, not every empty-deque fill.
    slug_first_fill_time: dict[str, str] = {}
    for fill in sorted_fills:
        slug = fill.get("market_slug")
        if slug and slug not in slug_first_fill_time:
            slug_first_fill_time[slug] = fill.get("transact_time")

    for fill in sorted_fills:
        if parse_transact_time(fill.get("transact_time")) is None:
            result.warnings.append(
                f"Fill {fill.get('fill_id')} has no parseable transact_time -- "
                "sorted first; FIFO ordering for its position may be wrong."
            )
        key = position_key(fill)
        dq = deques.setdefault(key, deque())
        _process_fill(fill, dq, result, earliest_snapshot_by_slug, slug_first_fill_time)

    for dq in deques.values():
        for sl in dq:
            if sl.shares_remaining > 0:
                result.open_lots.append(_to_open_lot(sl))

    if settlements:
        _apply_settlements(result, settlements)

    return result


def _process_fill(
    fill: dict[str, Any],
    dq: "deque[_OpenSlice]",
    result: LotAccountingResult,
    earliest_snapshot_by_slug: dict[str, dict[str, Any]],
    slug_first_fill_time: dict[str, str],
) -> None:
    side = fill["side"]
    shares_remaining = fill["shares"]

    if not dq:
        if _reduces_untracked_position(fill) or _predates_known_snapshot(
            fill, earliest_snapshot_by_slug, slug_first_fill_time,
        ):
            result.unmatched_closing_fills.append(fill)
            return
        dq.append(_OpenSlice(fill, shares_remaining))
        return

    if side == dq[0].side:
        dq.append(_OpenSlice(fill, shares_remaining))
        return

    while shares_remaining > 0 and dq and dq[0].side != side:
        oldest = dq[0]
        matched = min(shares_remaining, oldest.shares_remaining)
        result.closed_lots.append(_close_match(oldest, fill, matched))
        oldest.shares_remaining -= matched
        shares_remaining -= matched
        if oldest.shares_remaining <= 0:
            dq.popleft()

    if shares_remaining > 0:
        # Excess beyond total prior open inventory -- flip-through-flat.
        dq.append(_OpenSlice(fill, shares_remaining))


def _close_match(oldest: _OpenSlice, closing_fill: dict[str, Any], matched: float) -> ClosedLot:
    open_price = oldest.price
    close_price = closing_fill["price"]
    if oldest.side == "BUY":  # long lot
        gross_pnl = (close_price - open_price) * matched
    else:  # short lot
        gross_pnl = (open_price - close_price) * matched

    open_commission = _allocate_commission(oldest.commission_usd, matched, oldest.shares_total)
    close_commission = _allocate_commission(
        closing_fill.get("commission_usd"), matched, closing_fill["shares"],
    )
    # Polymarket's source field is commissionNotionalCollected: positive is
    # money collected from the account (a fee), negative is money returned
    # to it (a rebate). Subtract the signed amount. The former addition
    # inverted both fees and rebates and made the 2026-07-12 losing session
    # appear profitable in local attribution.
    net_pnl = gross_pnl - open_commission - close_commission

    open_dt = parse_transact_time(oldest.time)
    close_dt = parse_transact_time(closing_fill.get("transact_time"))
    holding_seconds = (
        (close_dt - open_dt).total_seconds()
        if open_dt is not None and close_dt is not None else 0.0
    )

    return ClosedLot(
        lot_id=f"{oldest.fill_id}:{closing_fill.get('fill_id')}:{matched}",
        market_slug=oldest.market_slug,
        outcome=oldest.outcome,
        event_bucket_key=oldest.event_bucket_key,
        open_fill_id=oldest.fill_id,
        open_order_id=oldest.order_id,
        open_side=oldest.side,
        open_price=open_price,
        open_time=oldest.time,
        close_fill_id=closing_fill.get("fill_id"),
        close_order_id=closing_fill.get("order_id"),
        close_price=close_price,
        close_time=closing_fill.get("transact_time"),
        closed_via="fill",
        shares_matched=matched,
        gross_pnl_usd=gross_pnl,
        open_commission_usd=open_commission,
        close_commission_usd=close_commission,
        net_pnl_usd=net_pnl,
        holding_seconds=holding_seconds,
    )


def _to_open_lot(sl: _OpenSlice) -> OpenLot:
    return OpenLot(
        market_slug=sl.market_slug,
        outcome=sl.outcome,
        event_bucket_key=sl.event_bucket_key,
        open_fill_id=sl.fill_id,
        open_order_id=sl.order_id,
        open_side=sl.side,
        open_price=sl.price,
        open_time=sl.time,
        shares_open=sl.shares_remaining,
        open_commission_usd=_allocate_commission(sl.commission_usd, sl.shares_remaining, sl.shares_total),
    )


def _settlement_holding_bounds(
    open_time: Optional[str], last_seen_at: Optional[str], detected_at: Optional[str],
) -> tuple[Optional[float], Optional[float], float]:
    open_dt = parse_transact_time(open_time)
    if open_dt is None:
        return None, None, 0.0
    last_seen_dt = parse_transact_time(last_seen_at)
    detected_dt = parse_transact_time(detected_at)
    lower = (last_seen_dt - open_dt).total_seconds() if last_seen_dt is not None else None
    upper = (detected_dt - open_dt).total_seconds() if detected_dt is not None else None
    if lower is not None and upper is not None:
        point = (lower + upper) / 2.0
    elif upper is not None:
        point = upper
    elif lower is not None:
        point = lower
    else:
        point = 0.0
    return lower, upper, point


def _apply_settlements(result: LotAccountingResult, settlements: list[dict[str, Any]]) -> None:
    """Closes out remaining open inventory for every (market_slug, outcome)
    a settlement event covers -- see compute_lots' docstring. A settlement
    with confidence "inferred_low" (or missing a payout estimate) leaves
    the open lot as-is except for presumed_settled=True -- never a
    fabricated realized number."""
    by_key: dict[tuple[str, Optional[str]], dict[str, Any]] = {
        (s.get("market_slug") or "", s.get("outcome")): s for s in settlements
    }

    remaining_open: list[OpenLot] = []
    for open_lot in result.open_lots:
        settlement = by_key.get((open_lot.market_slug, open_lot.outcome))
        if settlement is None:
            remaining_open.append(open_lot)
            continue

        confidence = settlement.get("confidence")
        payout = settlement.get("inferred_settlement_value_per_share")
        if confidence == "inferred_low" or payout is None:
            open_lot.presumed_settled = True
            open_lot.settlement_id = settlement.get("settlement_id")
            remaining_open.append(open_lot)
            continue

        if open_lot.open_side == "BUY":  # long lot
            gross_pnl = (payout - open_lot.open_price) * open_lot.shares_open
        else:  # short lot
            gross_pnl = (open_lot.open_price - payout) * open_lot.shares_open

        holding_lower, holding_upper, holding_point = _settlement_holding_bounds(
            open_lot.open_time, settlement.get("last_seen_at"), settlement.get("detected_at"),
        )

        result.closed_lots.append(ClosedLot(
            lot_id=f"{open_lot.open_fill_id}:{settlement.get('settlement_id')}:{open_lot.shares_open}",
            market_slug=open_lot.market_slug,
            outcome=open_lot.outcome,
            event_bucket_key=open_lot.event_bucket_key,
            open_fill_id=open_lot.open_fill_id,
            open_order_id=open_lot.open_order_id,
            open_side=open_lot.open_side,
            open_price=open_lot.open_price,
            open_time=open_lot.open_time,
            close_fill_id=None,
            close_order_id=None,
            close_price=None,
            close_time=settlement.get("detected_at"),
            closed_via="settlement",
            shares_matched=open_lot.shares_open,
            gross_pnl_usd=gross_pnl,
            open_commission_usd=open_lot.open_commission_usd,
            close_commission_usd=0.0,
            net_pnl_usd=gross_pnl - open_lot.open_commission_usd,
            holding_seconds=holding_point,
            settlement_confidence=confidence,
            holding_seconds_lower=holding_lower,
            holding_seconds_upper=holding_upper,
        ))

    result.open_lots = remaining_open
