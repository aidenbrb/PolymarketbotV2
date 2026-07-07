"""Compares exchange open orders and positions against the local ledger's
known-order-id set. Pure computation, no I/O, no logging -- callers fetch
the real data (see main.py::cmd_live_reconcile_orders) and pass it in here.

This exists because ledger-based order ownership (see
live/ledger.py::get_known_order_ids) is fragile: a lost/corrupted ledger
file, a write failure right after a real order posts, or an API response
field-name mismatch can silently make a genuine bot order permanently
"unrecognized" -- and the stale-order sweeps in multi_market_maker.py
already refuse to cancel anything unrecognized, for safety. This module is
a DIAGNOSTIC for that risk, not a fix for it: it makes the gap visible, it
doesn't close it. See live/RUNBOOK.md's "-8." section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ReconciliationReport:
    bot_owned_orders: list[dict[str, Any]]
    unknown_orders: list[dict[str, Any]]
    orders_missing_id_count: int
    # Ledger insertion order (oldest first) -- see live/ledger.py's
    # get_known_order_id_markets docstring.
    stale_ledger_orders: list[dict[str, Optional[str]]]
    unmanaged_positions: list[dict[str, Any]]
    known_order_id_count: int
    open_order_count: int
    position_count: int
    ledger_appears_empty: bool


def reconcile(
    open_orders: list[dict[str, Any]],
    known_order_markets: dict[str, str],
    positions: dict[str, dict[str, Any]],
) -> ReconciliationReport:
    """Classifies already-fetched exchange/ledger state. Deliberately does
    not fetch anything itself, so it's testable without mocking a client."""
    bot_owned_orders: list[dict[str, Any]] = []
    unknown_orders: list[dict[str, Any]] = []
    orders_missing_id = 0
    bot_owned_slugs: set[str] = set()
    open_order_ids: set[str] = set()

    for order in open_orders:
        order_id = _order_id(order)
        if not order_id:
            orders_missing_id += 1
            continue
        open_order_ids.add(order_id)
        if order_id in known_order_markets:
            bot_owned_orders.append(order)
            slug = _order_slug(order)
            if slug:
                bot_owned_slugs.add(slug)
        else:
            unknown_orders.append(order)

    stale_ledger_orders = [
        {"order_id": order_id, "market_slug": market_slug}
        for order_id, market_slug in known_order_markets.items()
        if order_id not in open_order_ids
    ]

    # Deliberately "no BOT-OWNED open order on this market" -- not "no open
    # order at all." An unrelated manual/unrecognized order sharing the same
    # slug doesn't mean this position is actually being managed.
    unmanaged_positions: list[dict[str, Any]] = []
    for slug, position in positions.items():
        if not isinstance(position, dict):
            continue
        if _net_position(position) == 0:
            continue
        if slug in bot_owned_slugs:
            continue
        unmanaged_positions.append({**position, "market_slug": slug})

    # Heuristic, not a certainty: get_known_order_ids()/get_known_order_id_markets()
    # already fail closed to {} on a ledger read failure, and that contract
    # can't distinguish "ledger unreadable" from "bot genuinely has zero
    # known orders" from the caller's side -- by design, it must not, since
    # that ambiguity is what makes the cancellation-safety behavior safe.
    # This flag surfaces the ambiguity for a human instead of hiding it.
    # Known blind spots: (a) if the ledger fails to read AND there happen to
    # be zero open orders right now, this stays False -- harmless, since
    # there's nothing to misclassify either way; (b) a genuinely fresh bot
    # with a manual order already resting correctly shows that order as
    # "unknown" and this flag correctly stays False too -- that's the
    # ledger's intended fail-safe behavior surfacing correctly, not a bug.
    ledger_appears_empty = not known_order_markets and bool(open_orders)

    return ReconciliationReport(
        bot_owned_orders=bot_owned_orders,
        unknown_orders=unknown_orders,
        orders_missing_id_count=orders_missing_id,
        stale_ledger_orders=stale_ledger_orders,
        unmanaged_positions=unmanaged_positions,
        known_order_id_count=len(known_order_markets),
        open_order_count=len(open_orders),
        position_count=len(positions),
        ledger_appears_empty=ledger_appears_empty,
    )


def _order_id(order: dict[str, Any]) -> Optional[str]:
    return order.get("id") or order.get("orderId")


def _order_slug(order: dict[str, Any]) -> Optional[str]:
    return order.get("marketSlug") or order.get("market_slug")


def _net_position(position: dict[str, Any]) -> float:
    try:
        return float(position.get("netPositionDecimal", 0))
    except (AttributeError, TypeError, ValueError):
        return 0.0
