"""Position-snapshot persistence + settlement detection -- the "Strategy B"
half of the P&L-attribution hybrid (see live/lot_accounting.py for
"Strategy A", the fill-based FIFO half).

Neither strategy alone can fully account for realized P&L: fills never
record a market's RESOLUTION (nothing in this codebase tracks
payout/winning-outcome, and us_client.py exposes no such endpoint), and a
raw snapshot diff of the exchange's own `realized` position field can't be
attributed back to individual fills/families/price-bands
(get_all_positions() is keyed by market_slug only, and multiple fills
routinely land inside one refresh cycle's snapshot interval). This module
supplies the settlement side only: when a position with still-open lots
(per lot_accounting.compute_lots(), which already fully accounts for every
known fill) disappears from the exchange's own position list, back-solve
what it must have paid out per share from the change in the exchange's own
`realized` field, if a clean enough signal exists -- confidence-tiered
rather than guessed with false certainty. See live/RUNBOOK.md's most
recent section.
"""

from __future__ import annotations

from typing import Any, Optional

from .. import config, storage
from ..logger import get_logger
from ..models import utcnow_iso
from .fills import parse_transact_time
from .lot_accounting import OpenLot

logger = get_logger("live.settlements")

POSITION_SNAPSHOTS_FILE = config.LIVE_TRADES_DIR / "position_snapshots.json"
SETTLEMENTS_FILE = config.LIVE_TRADES_DIR / "settlements.json"


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _net_position(position: dict[str, Any]) -> float:
    try:
        return float(position.get("netPositionDecimal", 0))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _position_figures(position: dict[str, Any]) -> tuple[float, float, float]:
    """(cost_usd, cash_value_usd, realized_usd)."""
    cost = _to_float((position.get("cost") or {}).get("value"))
    cash_value = _to_float((position.get("cashValue") or {}).get("value"))
    realized = _to_float((position.get("realized") or {}).get("value"))
    return cost, cash_value, realized


# ------------------------------------------------------------------
# Position snapshots
# ------------------------------------------------------------------
def get_all_position_snapshots() -> list[dict]:
    return storage.load_json(POSITION_SNAPSHOTS_FILE, default=[])


def get_latest_snapshot_by_slug() -> dict[str, dict]:
    """Last (by list order -- rows are appended chronologically) persisted
    row per market_slug."""
    latest: dict[str, dict] = {}
    for row in get_all_position_snapshots():
        slug = row.get("market_slug")
        if slug:
            latest[slug] = row
    return latest


def get_earliest_snapshot_by_slug() -> dict[str, dict]:
    """First (by list order -- rows are appended chronologically) persisted
    row per market_slug -- used by lot_accounting.py's pre-tracking-
    inventory safety net: a nonzero position already present the first
    time a slug was ever snapshotted is real (if partial) evidence of
    inventory this bot's fill history doesn't explain, distinct from a
    slug we've simply never snapshotted at all."""
    earliest: dict[str, dict] = {}
    for row in get_all_position_snapshots():
        slug = row.get("market_slug")
        if slug and slug not in earliest:
            earliest[slug] = row
    return earliest


def build_position_snapshot_rows(
    positions: dict[str, dict[str, Any]],
    timestamp: str,
    previous_by_slug: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pure. Only emits a row for a slug when cost/cashValue/realized/
    net_position actually changed since previous_by_slug's entry for it (or
    there's no previous entry at all) -- an undeduped every-cycle snapshot
    would grow the file an order of magnitude faster than fills.json
    already does (storage.py rewrites the whole file on every write)."""
    rows = []
    for slug, position in positions.items():
        if not isinstance(position, dict):
            continue
        net_position = _net_position(position)
        cost, cash_value, realized = _position_figures(position)
        previous = previous_by_slug.get(slug)
        if (
            previous is not None
            and previous.get("net_position_shares") == net_position
            and previous.get("cost_usd") == cost
            and previous.get("cash_value_usd") == cash_value
            and previous.get("realized_usd") == realized
        ):
            continue
        rows.append({
            "snapshot_id": f"{slug}:{timestamp}",
            "market_slug": slug,
            "timestamp": timestamp,
            "net_position_shares": net_position,
            "cost_usd": cost,
            "cash_value_usd": cash_value,
            "realized_usd": realized,
            "raw_position": position,
        })
    return rows


def record_position_snapshots(positions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """I/O wrapper -- reads the latest persisted snapshot per slug, builds
    only-if-changed rows (see build_position_snapshot_rows), appends them.
    Best-effort: a read/write failure is logged, never allowed to
    interrupt the caller's refresh cycle."""
    try:
        previous_by_slug = get_latest_snapshot_by_slug()
        rows = build_position_snapshot_rows(positions, utcnow_iso(), previous_by_slug)
        for row in rows:
            storage.append_json_list(POSITION_SNAPSHOTS_FILE, row)
        return rows
    except Exception as exc:  # noqa: BLE001 -- must never interrupt the refresh cycle
        logger.error("Could not record position snapshot(s) this cycle: %s", exc)
        return []


# ------------------------------------------------------------------
# Settlement detection
# ------------------------------------------------------------------
def get_all_settlements() -> list[dict]:
    return storage.load_json(SETTLEMENTS_FILE, default=[])


def get_detected_settlement_keys() -> set[tuple[str, Optional[str]]]:
    return {(s.get("market_slug"), s.get("outcome")) for s in get_all_settlements()}


def record_settlements(settlements: list[dict[str, Any]]) -> None:
    """Every settlement passed in is assumed already new (detect_settlements
    itself skips already-detected keys) -- no further dedup here."""
    for settlement in settlements:
        storage.append_json_list(SETTLEMENTS_FILE, settlement)


def infer_settlement_payout_per_share(
    last_snapshot: dict[str, Any],
    baseline_realized_usd: float,
    shares_open: float,
    cost_open_usd: float,
) -> tuple[Optional[float], str]:
    """Three-tier confidence back-solve for what a vanished position must
    have paid out per share. See module docstring / live/RUNBOOK.md.

    observed: the exchange's own `realized` field moved since the
    baseline (the earliest snapshot on record for this slug) -- a clean
    enough signal to back-solve algebraically:
        long payout = (realized_delta_usd + entry_cost_usd) / shares_open
        short payout = (entry_credit_usd - realized_delta_usd) / shares_open
    A $1/$0 binary resolution pays the same per share regardless of entry
    price; applying this uniformly and summing across every still-open lot
    reconstructs realized_delta_usd exactly. Sanity-bounded to [0, 1] (with
    small float-noise tolerance) -- anything further outside is a loud
    anomaly, not a quiet clamp, and is deliberately downgraded rather than
    trusted.

    inferred_high: no realized-field movement, but the last snapshot shows
    a clear mark-to-market proximity to 0 or 1. Restricted to LONG
    positions only -- cashValue's sign convention for a SHORT position is
    genuinely ambiguous in this API (see ledger.py's own documented
    uncertainty about "cost"'s sign) and isn't worth guessing at for this,
    the weakest confidence tier.

    inferred_low: neither signal usable -- payout genuinely unknown."""
    if shares_open <= 0:
        return None, "inferred_low"

    realized_delta = last_snapshot.get("realized_usd", 0.0) - baseline_realized_usd
    if abs(realized_delta) > 1e-9:
        net_position = last_snapshot.get("net_position_shares", 0.0)
        payout = (
            (cost_open_usd - realized_delta) / shares_open
            if net_position < 0
            else (realized_delta + cost_open_usd) / shares_open
        )
        if -0.01 <= payout <= 1.01:
            return max(0.0, min(1.0, payout)), "observed"
        logger.warning(
            "Back-solved settlement payout %.4f/share is outside [0,1] -- "
            "treating as inferred_low instead of trusting an obviously-wrong number.",
            payout,
        )
        return None, "inferred_low"

    net_position = last_snapshot.get("net_position_shares", 0.0)
    cash_value = last_snapshot.get("cash_value_usd", 0.0)
    if net_position > 0:
        mark_price = cash_value / net_position
        if mark_price >= 0.9:
            return 1.0, "inferred_high"
        if mark_price <= 0.1:
            return 0.0, "inferred_high"

    return None, "inferred_low"


def _gap_seconds(last_seen_at: Optional[str], detected_at: Optional[str]) -> Optional[float]:
    last_dt = parse_transact_time(last_seen_at)
    detected_dt = parse_transact_time(detected_at)
    if last_dt is None or detected_dt is None:
        return None
    return (detected_dt - last_dt).total_seconds()


def _group_open_lots(open_lots: list[OpenLot]) -> dict[tuple[str, Optional[str]], list[OpenLot]]:
    grouped: dict[tuple[str, Optional[str]], list[OpenLot]] = {}
    for lot in open_lots:
        grouped.setdefault((lot.market_slug, lot.outcome), []).append(lot)
    return grouped


def detect_settlements(
    all_snapshots: list[dict[str, Any]],
    current_positions: dict[str, dict[str, Any]],
    open_lots: list[OpenLot],
    already_detected_keys: Optional[set[tuple[str, Optional[str]]]] = None,
    now: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Pure. Only considers a (market_slug, outcome) that Strategy A
    (live/lot_accounting.py::compute_lots) STILL shows as open inventory
    after every known fill is applied -- a disappearance Strategy A
    already fully explains via a real fill never reaches this function at
    all (it simply has no OpenLot for that key). `already_detected_keys`
    (e.g. settlements.get_detected_settlement_keys()) skips re-detecting a
    pair already flagged once -- otherwise an inferred_low settlement
    (which doesn't remove the open lot, unlike a confident one) would be
    re-logged every cycle indefinitely."""
    now = now or utcnow_iso()
    already_detected_keys = already_detected_keys or set()

    grouped = _group_open_lots(open_lots)
    if not grouped:
        return []

    outcomes_by_slug: dict[str, set] = {}
    for slug, outcome in grouped:
        outcomes_by_slug.setdefault(slug, set()).add(outcome)

    slugs = set(outcomes_by_slug)
    snapshots_by_slug: dict[str, list[dict]] = {}
    for row in all_snapshots:
        slug = row.get("market_slug")
        if slug in slugs:
            snapshots_by_slug.setdefault(slug, []).append(row)

    settlements = []
    for slug, outcomes in outcomes_by_slug.items():
        current = current_positions.get(slug)
        if current is not None and _net_position(current) != 0:
            continue  # still open per the exchange -- not a settlement candidate

        history = snapshots_by_slug.get(slug)
        if not history:
            continue  # never captured a snapshot for this slug -- nothing to back-solve from

        history_sorted = sorted(history, key=lambda r: r.get("timestamp") or "")
        baseline_realized_usd = history_sorted[0].get("realized_usd", 0.0)
        last_seen = history_sorted[-1]
        last_seen_at = last_seen.get("timestamp")

        # get_all_positions() (us_client.py) is keyed by market_slug only --
        # one combined realized/cashValue snapshot per slug, with no
        # outcome-level breakdown. When more than one outcome on the same
        # slug still has open lots, that single shared signal can't be
        # disambiguated between them: applying the full realized_delta (or
        # mark-price proximity) independently to EACH outcome's back-solve
        # would attribute the same payout event to both, cross-contaminating
        # whichever one actually settled. Never fabricate a per-outcome
        # number from a signal that's ambiguous across outcomes -- same
        # never-guess posture as the sanity-bound downgrade in
        # infer_settlement_payout_per_share.
        multi_outcome = len(outcomes) > 1

        for outcome in outcomes:
            if (slug, outcome) in already_detected_keys:
                continue
            lots = grouped[(slug, outcome)]
            shares_open = sum(lot.shares_open for lot in lots)
            cost_open_usd = sum(lot.shares_open * lot.open_price for lot in lots)
            if multi_outcome:
                payout, confidence = None, "inferred_low"
            else:
                payout, confidence = infer_settlement_payout_per_share(
                    last_seen, baseline_realized_usd, shares_open, cost_open_usd,
                )
            settlements.append({
                "settlement_id": f"{slug}:{outcome}:{now}",
                "market_slug": slug,
                "outcome": outcome,
                "detected_at": now,
                "last_seen_snapshot": last_seen,
                "last_seen_at": last_seen_at,
                "gap_seconds": _gap_seconds(last_seen_at, now),
                "resolution_realized_delta_usd": last_seen.get("realized_usd", 0.0) - baseline_realized_usd,
                "inferred_settlement_value_per_share": payout,
                "confidence": confidence,
            })
    return settlements
