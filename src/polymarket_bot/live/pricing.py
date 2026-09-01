"""Pure bid/ask pricing math. No network calls, no client, no side effects."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Optional


def round_to_tick(price: float, tick_size: float) -> float:
    """Round price to the nearest exchange tick using exact decimal arithmetic
    (plain float rounding produces artifacts like 0.30000000000000004)."""
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    price_d = Decimal(str(price))
    tick_d = Decimal(str(tick_size))
    ticks = (price_d / tick_d).to_integral_value(rounding=ROUND_HALF_UP)
    return float(ticks * tick_d)


def _ceil_to_tick(price: float, tick_size: float) -> float:
    """Round UP to the nearest tick -- used where rounding down would let a
    price slip below a hard floor (e.g. cost basis)."""
    price_d = Decimal(str(price))
    tick_d = Decimal(str(tick_size))
    ticks = (price_d / tick_d).to_integral_value(rounding=ROUND_CEILING)
    return float(ticks * tick_d)


def _floor_to_tick(price: float, tick_size: float) -> float:
    """Round DOWN to the nearest tick -- used where rounding up would let a
    price slip above a hard cap (e.g. cost basis on a short)."""
    price_d = Decimal(str(price))
    tick_d = Decimal(str(tick_size))
    ticks = (price_d / tick_d).to_integral_value(rounding=ROUND_FLOOR)
    return float(ticks * tick_d)


@dataclass
class QuoteSides:
    bid: float
    ask: float


def compute_quote(
    reference_price: float, tick_size: float, spread_cents: float = 3.0
) -> QuoteSides:
    """Offset reference_price by half the spread each side, round to tick,
    and clamp inside (0, 1) -- guaranteeing at least one tick of separation."""
    if not 0 < reference_price < 1:
        raise ValueError("reference_price must be strictly between 0 and 1")
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")

    half_spread = (spread_cents / 100) / 2
    min_price = max(tick_size, 0.01)
    max_price = min(1 - tick_size, 0.99)

    raw_bid = max(min_price, min(reference_price - half_spread, max_price))
    raw_ask = max(min_price, min(reference_price + half_spread, max_price))

    bid = round_to_tick(raw_bid, tick_size)
    ask = round_to_tick(raw_ask, tick_size)

    if bid >= ask:
        # Collapsed onto the same tick (tight tick size vs. spread, or clamped
        # near an edge) -- force a minimum 1-tick separation within bounds.
        if ask + tick_size <= max_price:
            ask = round_to_tick(ask + tick_size, tick_size)
        elif bid - tick_size >= min_price:
            bid = round_to_tick(bid - tick_size, tick_size)
        else:
            raise ValueError(
                "Cannot compute a valid bid/ask pair at this tick size near price bounds"
            )

    return QuoteSides(bid=bid, ask=ask)


def compute_book_aware_quote(
    best_bid: float,
    best_ask: float,
    tick_size: float,
    min_edge_cents: float = 0.5,
) -> QuoteSides | None:
    """Join/improve the REAL order book by one tick on each side, instead of
    quoting an arbitrary offset from the midpoint.

    A market maker only gets filled -- and only captures the spread rather
    than being adversely selected -- if its resting orders are competitive
    with (at or better than) what's already on the book. Quoting behind the
    book (e.g. a fixed spread around the mid, wider than the real market)
    means either nobody ever trades with you, or you only get filled when
    the market has already moved through your price -- the wrong side of
    every fill.

    Returns None if there isn't enough real spread left to bother quoting
    after improving both sides by one tick (e.g. the book is already only
    one tick wide) -- callers should skip the cycle entirely rather than
    force a trade with no edge.
    """
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        raise ValueError("best_bid must be positive and less than best_ask")

    min_price = max(tick_size, 0.01)
    max_price = min(1 - tick_size, 0.99)

    new_bid = round_to_tick(max(min(best_bid + tick_size, max_price), min_price), tick_size)
    new_ask = round_to_tick(max(min(best_ask - tick_size, max_price), min_price), tick_size)

    captured_spread = new_ask - new_bid
    min_edge = min_edge_cents / 100
    if captured_spread < min_edge:
        return None

    return QuoteSides(bid=new_bid, ask=new_ask)


def book_has_enough_depth(book: dict, settings) -> bool:
    """Apply the exact live L2 depth guard to a normalized book.

    Observation uses this same helper so a hypothetical fill from a book
    the real maker would reject cannot qualify a market for live entry.
    """
    levels = max(1, settings.depth_levels_to_check)
    bids = list(book.get("bids") or [])
    asks = list(book.get("asks") or [])
    if not bids or not asks:
        return False

    def quantity(level: dict) -> float:
        try:
            value = level.get("quantity", 0.0)
            if isinstance(value, dict):
                value = value.get("value", 0.0)
            return max(0.0, float(value))
        except (AttributeError, TypeError, ValueError):
            return 0.0

    if quantity(bids[0]) < settings.min_top_depth_shares:
        return False
    if quantity(asks[0]) < settings.min_top_depth_shares:
        return False
    return (
        sum(quantity(level) for level in bids[:levels])
        >= settings.min_total_depth_shares
        and sum(quantity(level) for level in asks[:levels])
        >= settings.min_total_depth_shares
    )


def apply_cost_basis_floor(
    exit_price: float,
    net_position: float,
    avg_cost: Optional[float],
    tick_size: float,
) -> Optional[float]:
    """Adjusts the price for the position-REDUCING side only (selling if
    long, buying-to-cover if short) so the bot never voluntarily realizes a
    loss against its own average cost. Returns None if no valid price
    satisfies this (the market has moved too far away -- skip that leg this
    cycle rather than lock in a loss).

    This protects only against the bot's OWN resting order realizing a
    loss. It does NOT protect against the position simply resolving
    unfavorably when the market settles -- that risk is unavoidable for any
    held position and this function cannot address it.
    """
    if avg_cost is None or net_position == 0:
        return exit_price

    if net_position > 0:
        adjusted = max(exit_price, _ceil_to_tick(avg_cost, tick_size))
    else:
        adjusted = min(exit_price, _floor_to_tick(avg_cost, tick_size))

    if not (max(tick_size, 0.01) <= adjusted <= min(1 - tick_size, 0.99)):
        return None
    return adjusted


def apply_liquidation_limit(
    exit_price: float,
    net_position: float,
    avg_cost: Optional[float],
    tick_size: float,
    allowed_loss_cents: float,
    max_total_loss_usd: float,
) -> Optional[float]:
    """Bound, but do not prohibit, a reducing order's realized loss.

    ``allowed_loss_cents`` is supplied by the time/risk policy in
    ``MarketMaker``. The total-dollar cap prevents a large position from
    consuming the same per-share allowance as a small one. Rounding is
    conservative: a long's minimum exit rounds up and a short's maximum
    cover price rounds down.
    """
    if avg_cost is None or net_position == 0:
        return exit_price

    per_share_limit = max(0.0, allowed_loss_cents) / 100.0
    if max_total_loss_usd >= 0:
        per_share_limit = min(
            per_share_limit,
            max_total_loss_usd / abs(net_position) if net_position else 0.0,
        )

    if net_position > 0:
        minimum_exit = _ceil_to_tick(avg_cost - per_share_limit, tick_size)
        adjusted = max(exit_price, minimum_exit)
    else:
        maximum_exit = _floor_to_tick(avg_cost + per_share_limit, tick_size)
        adjusted = min(exit_price, maximum_exit)

    if not (max(tick_size, 0.01) <= adjusted <= min(1 - tick_size, 0.99)):
        return None
    return adjusted


def usd_to_shares(size_usd: float, price: float) -> float:
    if price <= 0:
        raise ValueError("price must be positive")
    if size_usd <= 0:
        raise ValueError("size_usd must be positive")
    return round(size_usd / price, 6)


def floor_to_quantity_step(quantity: float, min_quantity: float) -> float:
    """Align quantity to the market's minimum trade quantity by rounding down."""
    if min_quantity <= 0:
        raise ValueError("min_quantity must be positive")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    quantity_d = Decimal(str(quantity))
    step_d = Decimal(str(min_quantity))
    steps = (quantity_d / step_d).to_integral_value(rounding=ROUND_FLOOR)
    aligned = steps * step_d
    if aligned <= 0:
        return 0.0
    return float(aligned)
