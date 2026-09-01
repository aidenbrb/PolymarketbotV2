"""Persists fills/executions detected on the private WebSocket
(live/ws_private.py::PrivateStateStore) so they survive past its in-memory,
capped ring buffer. See live/RUNBOOK.md's "-9." section.

CONFIRMED schema, against real production fills (2026-07-06): an execution
dict's order_id/market_slug/side/quoted_price live NESTED under
execution["order"] (order.id, order.marketSlug, order.side -- a prefixed
enum string "ORDER_SIDE_BUY"/"ORDER_SIDE_SELL", normalized below to this
codebase's own "BUY"/"SELL" -- and order.price.value), NOT at the top level
of the execution dict (an earlier, wrong assumption when this module was
first written, before any real fill existed to check against). The raw
execution dict is still always preserved (raw_execution) so nothing is lost
even where a field-extraction guess is wrong or the real schema turns out
to differ further.

Also confirmed: PrivateStateStore.recent_executions() holds EVERY execution
type (order lifecycle noise -- NEW/CANCELED/REJECTED -- vastly outnumbers
real fills in practice), so is_actual_fill() filters to FILL/PARTIAL_FILL
before anything is persisted.

This is fed from live/ws_runner.py only -- the REST-polling fallback runner
(live/runner.py::LiveTradingBot) has no private-WS connection at all and
never populates this file.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional

from .. import config, storage
from ..logger import get_logger
from ..models import utcnow_iso
from .models import FillRecord

logger = get_logger("live.fills")

FILLS_FILE = config.LIVE_TRADES_DIR / "fills.json"
BACKFILL_RESOLVED_FILE = config.LIVE_TRADES_DIR / "backfill_resolved_orders.json"

FILL_EXECUTION_TYPES = frozenset({"EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"})
_ORDER_SIDE_TO_CANONICAL = {"ORDER_SIDE_BUY": "BUY", "ORDER_SIDE_SELL": "SELL"}
# order.outcomeSide -> canonical "YES"/"NO". Confirmed to agree with
# order.marketMetadata.outcome (the string fallback below) on 100% of real
# fills checked -- see live/lot_accounting.py for why outcome is part of
# true position identity.
_OUTCOME_SIDE_TO_CANONICAL = {"OUTCOME_SIDE_YES": "YES", "OUTCOME_SIDE_NO": "NO"}
_MARKOUT_WINDOWS = (("1m", 60.0), ("5m", 300.0), ("15m", 900.0))


def get_all_fills() -> list[dict]:
    return storage.load_json(FILLS_FILE, default=[])


def record_fill(fill: FillRecord) -> None:
    storage.append_json_list(FILLS_FILE, fill.to_dict())
    logger.info("Recorded fill %s for market %s", fill.fill_id, fill.market_slug)


def overwrite_fills(fills: list[dict]) -> None:
    """The one place outside record_fill()/append_json_list allowed to
    rewrite the whole file -- used by migrate_legacy_fills() and by
    ws_runner.py's markout backfill, so this module stays the sole owner of
    FILLS_FILE's write mechanics."""
    storage.save_json(FILLS_FILE, fills)


def already_recorded_fill_ids() -> set[str]:
    """Fails closed to an empty set on a corrupt/unreadable fills file --
    mirrors ledger.py::get_known_order_details()'s fail-closed contract
    (better to risk a duplicate re-record than to crash the refresh cycle)."""
    try:
        return {f["fill_id"] for f in get_all_fills() if f.get("fill_id")}
    except Exception as exc:  # noqa: BLE001 -- local-state read must never crash a cycle
        logger.error(
            "Could not read the fills file to determine already-recorded "
            "fill ids -- treating as empty (a fill may be re-recorded this "
            "cycle) rather than crashing: %s", exc,
        )
        return set()


def get_backfill_resolved_order_ids() -> set[str]:
    """The execution-backfill mechanism's "confirmed nothing to backfill"
    determinations (multi_market_maker.py::_backfill_one_order), persisted
    so a bot restart doesn't re-query the same long-resolved orders forever
    -- read fresh each cycle, same fail-closed contract as
    already_recorded_fill_ids()."""
    try:
        return {
            r["order_id"] for r in storage.load_json(BACKFILL_RESOLVED_FILE, default=[])
            if r.get("order_id")
        }
    except Exception as exc:  # noqa: BLE001 -- local-state read must never crash a cycle
        logger.error(
            "Could not read the backfill-resolved file to determine "
            "already-resolved order ids -- treating as empty (an order may "
            "be re-checked this cycle) rather than crashing: %s", exc,
        )
        return set()


def record_backfill_resolved(order_id: str, reason: str) -> None:
    """Persist why no future execution-backfill lookup is needed.

    Reasons currently include ``no_fill`` (REST), ``not_found`` (REST),
    and ``private_ws_no_fill`` (terminal private-stream update with an
    explicit zero cumulative quantity).
    """
    storage.append_json_list(
        BACKFILL_RESOLVED_FILE,
        {"order_id": order_id, "reason": reason, "resolved_at": utcnow_iso()},
    )


def is_actual_fill(execution: dict[str, Any]) -> bool:
    """True only for an execution type where shares actually changed hands.
    NEW/CANCELED/REJECTED/etc are order-lifecycle noise, not fills, and must
    never reach fills.json."""
    return execution.get("type") in FILL_EXECUTION_TYPES


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _nested(d: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _normalize_side(raw_side: Any) -> Optional[str]:
    return _ORDER_SIDE_TO_CANONICAL.get(raw_side)


def _normalize_outcome(order: dict[str, Any]) -> Optional[str]:
    canonical = _OUTCOME_SIDE_TO_CANONICAL.get(order.get("outcomeSide"))
    if canonical is not None:
        return canonical
    fallback = _nested(order, "marketMetadata", "outcome")
    return str(fallback).upper() if fallback else None


def resolve_order_id_and_market_slug(
    execution: dict[str, Any], order_details: dict[str, dict]
) -> tuple[Optional[str], Optional[str]]:
    """Shared by build_fill_record and ws_runner.py::_persist_new_fills
    (which needs market_slug ahead of build_fill_record running, to prefetch
    that market's BBO), so this resolution logic lives in exactly one
    place. Prefers the execution's own embedded order.id/order.marketSlug
    (confirmed reliable against real fills) over ledger correlation, which
    only helps when this bot's own local ledger successfully recorded that
    exact order_id (see live/RUNBOOK.md's "-6."/"-8." sections on
    ledger-based ownership being fragile)."""
    order = execution.get("order")
    order = order if isinstance(order, dict) else {}
    order_id = order.get("id") or execution.get("orderId") or execution.get("clientOrderId")
    details = order_details.get(order_id) if order_id else None
    market_slug = order.get("marketSlug") or (details.get("market_id") if details else None)
    return order_id, market_slug


def build_fill_record(
    execution: dict[str, Any],
    order_details: dict[str, dict],
    current_bbo: Optional[dict[str, float]],
) -> FillRecord:
    """Pure -- no I/O. `order_details` is ledger.py::get_known_order_details()'s
    output (order_id -> {"market_id", "side", "price"}), pre-fetched by the
    caller. `current_bbo` is a best-effort {"best_bid", "best_ask"} snapshot
    fetched by the caller at the moment this fill was drained from the
    ring buffer -- 0 to one refresh interval (~60s by default) after the
    real fill, an uncontrolled/variable delay, NOT a deliberate fixed-window
    academic realized-spread measurement (see markout_1m_cents/
    markout_5m_cents for that). `detected_at` records exactly when this
    snapshot was taken so that delay is auditable later.
    """
    fill_id = execution.get("id") or execution.get("tradeId") or ""
    order = execution.get("order")
    order = order if isinstance(order, dict) else {}
    order_id, market_slug = resolve_order_id_and_market_slug(execution, order_details)
    details = order_details.get(order_id) if order_id else None

    side = _normalize_side(order.get("side")) or (details.get("side") if details else None)
    quoted_price = _to_float(_nested(order, "price", "value"))
    if quoted_price is None and details is not None:
        quoted_price = details.get("price")

    price = _to_float((execution.get("lastPx") or {}).get("value"))
    shares = _to_float(execution.get("lastShares"))
    transact_time = execution.get("transactTime")
    outcome = _normalize_outcome(order)
    commission_usd = _to_float(_nested(execution, "commissionNotionalCollected", "value"))

    current_mid_at_detection: Optional[float] = None
    edge_vs_current_mid_cents: Optional[float] = None
    fill_quality: Optional[str] = None
    if market_slug is not None and current_bbo is not None and side in ("BUY", "SELL") and price is not None:
        best_bid = current_bbo.get("best_bid")
        best_ask = current_bbo.get("best_ask")
        if best_bid is not None and best_ask is not None:
            current_mid_at_detection = (best_bid + best_ask) / 2
            edge_vs_current_mid_cents = compute_markout_cents(side, price, current_mid_at_detection)
            if edge_vs_current_mid_cents > 0.1:
                fill_quality = "favorable"
            elif edge_vs_current_mid_cents < -0.1:
                fill_quality = "adverse"
            else:
                fill_quality = "neutral"

    return FillRecord(
        fill_id=fill_id,
        order_id=order_id,
        market_slug=market_slug,
        side=side,
        price=price,
        shares=shares,
        outcome=outcome,
        commission_usd=commission_usd,
        execution_type=execution.get("type", "UNKNOWN"),
        transact_time=transact_time,
        quoted_price=quoted_price,
        current_mid_at_detection=current_mid_at_detection,
        edge_vs_current_mid_cents=edge_vs_current_mid_cents,
        fill_quality=fill_quality,
        markout_1m_cents=None,
        markout_1m_computed_at=None,
        markout_5m_cents=None,
        markout_5m_computed_at=None,
        markout_15m_cents=None,
        markout_15m_computed_at=None,
        detected_at=utcnow_iso(),
        raw_execution=dict(execution),
        executable_markout_1m_cents=None,
        executable_markout_5m_cents=None,
        executable_markout_15m_cents=None,
    )


def migrate_legacy_fills(
    existing_fills: list[dict], order_details: dict[str, dict]
) -> tuple[list[dict], dict[str, int]]:
    """One-time cleanup for fills.json written before build_fill_record
    learned the real execution["order"] schema and before is_actual_fill()
    existed to filter out order-lifecycle noise. Pure -- no I/O; the caller
    owns backing up and overwriting FILLS_FILE.

    Re-derives order_id/market_slug/side/quoted_price/transact_time for
    surviving records from their own already-stored raw_execution. Leaves
    current_mid_at_detection/edge_vs_current_mid_cents/fill_quality as None
    rather than backfilling them from today's BBO -- a stale,
    unrelated-in-time snapshot would misrepresent what those fields mean.
    PRESERVES any markout_*_cents/markout_*_computed_at already present on
    the input record instead of resetting them to None -- safe to re-run
    even after real markouts have already been computed."""
    stats = {"input": len(existing_fills), "dropped_non_fill": 0, "migrated": 0}
    migrated: list[dict] = []
    for raw_fill in existing_fills:
        execution = raw_fill.get("raw_execution")
        if not isinstance(execution, dict) or not is_actual_fill(execution):
            stats["dropped_non_fill"] += 1
            continue
        record = build_fill_record(execution, order_details, current_bbo=None)
        record.detected_at = raw_fill.get("detected_at", record.detected_at)
        for field_name in (
            "markout_1m_cents", "markout_1m_computed_at",
            "markout_5m_cents", "markout_5m_computed_at",
            "markout_15m_cents", "markout_15m_computed_at",
            "executable_markout_1m_cents",
            "executable_markout_5m_cents",
            "executable_markout_15m_cents",
        ):
            if raw_fill.get(field_name) is not None:
                setattr(record, field_name, raw_fill[field_name])
        migrated.append(record.to_dict())
        stats["migrated"] += 1
    return migrated, stats


def parse_transact_time(value: Optional[str]) -> Optional[datetime]:
    """execution["transactTime"] arrives as e.g.
    "2026-07-06T14:20:32.560425954Z" -- 9 fractional digits (nanoseconds).
    datetime.fromisoformat's leniency for >6 fractional digits is
    version-specific, so the fraction is defensively truncated to 6
    (microseconds) before parsing rather than relying on it, since this
    project supports Python 3.10+."""
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    normalized = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6], normalized)
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def compute_markout_cents(side: str, fill_price: float, mid: float) -> float:
    """Signed edge of fill_price vs. mid, in cents, by side: positive means
    the fill was favorable (BUY below mid / SELL above mid)."""
    signed = (mid - fill_price) if side == "BUY" else (fill_price - mid)
    return round(signed * 100, 4)


def compute_executable_markout_cents(
    side: str, fill_price: float, best_bid: float, best_ask: float,
) -> float:
    """Signed edge of fill_price vs. the price actually achievable to close
    the position right now, in cents: BUY marks against best_bid (what a
    sell-to-close would fetch), SELL marks against best_ask (what a
    buy-to-close would cost). Unlike compute_markout_cents's midpoint, this
    is a price the position could actually transact at."""
    reference = best_bid if side == "BUY" else best_ask
    signed = (reference - fill_price) if side == "BUY" else (fill_price - reference)
    return round(signed * 100, 4)


def find_due_markout_windows(
    fills: list[dict],
    now: datetime,
    max_staleness_seconds: float,
    windows: tuple[tuple[str, float], ...] = _MARKOUT_WINDOWS,
) -> list[dict[str, Any]]:
    """Pure. One entry per (fill, window) pair not yet resolved whose mark
    has passed -- status "compute" (needs a fresh BBO fetch) or "stale"
    (passed too long ago; resolve to None rather than a misleading number
    computed against an unrelated-in-time market snapshot). Skips fills
    missing transact_time/market_slug/side/price, and windows already
    resolved (their *_computed_at is not None). `windows` defaults to the
    fixed module constant; ws_runner.py overrides the "15m" duration from a
    settings value, everything else (including every test) uses the default."""
    due: list[dict[str, Any]] = []
    for index, fill in enumerate(fills):
        transact_time = parse_transact_time(fill.get("transact_time"))
        if (
            transact_time is None
            or fill.get("market_slug") is None
            or fill.get("side") not in ("BUY", "SELL")
            or fill.get("price") is None
        ):
            continue
        for window, seconds in windows:
            if fill.get(f"markout_{window}_computed_at") is not None:
                continue
            due_at = transact_time + timedelta(seconds=seconds)
            if now < due_at:
                continue
            status = "stale" if (now - due_at) > timedelta(seconds=max_staleness_seconds) else "compute"
            due.append({
                "index": index,
                "window": window,
                "status": status,
                "market_slug": fill["market_slug"],
                "side": fill["side"],
                "price": fill["price"],
            })
    return due
