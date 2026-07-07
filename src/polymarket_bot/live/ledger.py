"""Record of real live quote cycles -- separate from the fake paper_trader
ledger in data/paper_trades/. Also computes a best-effort daily P/L for the
circuit breaker.

Deliberately NOT based on any account-balance field. Two were tried and
both proved unsafe for this: `buyingPower` (free/tradeable cash) drops every
time the bot posts new resting orders -- posting orders reserves margin,
which looks identical to a loss from a pure balance-diff perspective. This
tripped the breaker for real on 2026-07-05 reporting a "$-24.13 loss" while
every currently held position was, in aggregate, within $0.18 of flat
(confirmed against the real account) -- the multi-market rewrite made this
easy to hit, since it can have far more resting orders live at once than
the original single-market design ever did. `currentBalance` was tried
first (see the old strategy_profitability_concern history) and has its own
unexplained ~$75 gap from the account's real value.

Daily P/L is instead computed directly from position data (see
estimate_daily_pnl_usd): unrealized P/L (cashValue - cost) plus realized P/L,
summed across every currently held position. This only reflects what was
actually paid vs. what's actually held/realized -- it isn't affected by how
much capital is currently reserved for resting orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import config, storage
from ..logger import get_logger
from .models import LiveQuoteCycle
from .us_client import LiveUsClient, UsApiError

logger = get_logger("live.ledger")

LEDGER_FILE = config.LIVE_TRADES_DIR / "orders.json"
# New filename (not daily_balance_baseline.json) is deliberate: that file may
# still hold today's date with an old buyingPower-based baseline from before
# this metric changed. Reusing its path would compare today's position-based
# total against a stale balance-based baseline -- a meaningless number far
# larger in magnitude than any real P/L. A fresh path guarantees a fresh
# baseline gets recorded on the first call under the new metric.
DAILY_BALANCE_FILE = config.LIVE_TRADES_DIR / "daily_pnl_baseline.json"


def get_all_cycles() -> list[dict]:
    return storage.load_json(LEDGER_FILE, default=[])


def record_cycle(cycle: LiveQuoteCycle) -> None:
    cycles = get_all_cycles()
    cycles.append(cycle.to_dict())
    storage.save_json(LEDGER_FILE, cycles)
    logger.info(
        "Recorded live quote cycle %s for market %s", cycle.cycle_id, cycle.market_id
    )


def get_cycles_since(since_iso: str) -> list[dict]:
    return [c for c in get_all_cycles() if c.get("timestamp", "") >= since_iso]


def get_known_order_ids() -> set[str]:
    """Every order id this bot has ever recorded posting, across all
    recorded live quote cycles (not just recent ones -- an order posted
    many cycles ago may still be resting if it never filled or got
    cancelled). Used to tell a bot-owned resting order apart from anything
    else on the account (a manual trade, or a different strategy sharing
    the same account) before a stale-order sweep cancels anything.

    Fails closed: if the ledger can't be read/parsed at all, this logs
    loudly and returns an EMPTY set rather than raising. An empty set
    means every order looks "unrecognized" to the ownership check in
    multi_market_maker.py, which makes it refuse to cancel anything --
    exactly the safe degradation wanted here (a crash reading local state
    must never be allowed to kill an entire refresh cycle or, worse, cause
    a cancellation decision based on incomplete data)."""
    return set(get_known_order_id_markets())


def get_known_order_id_markets() -> dict[str, str]:
    """Same data as get_known_order_ids(), but mapped to the market slug
    (cycle.market_id) each order was posted for -- needed by
    live/reconciliation.py to report which market a stale/unrecognized order
    belongs to. Same fail-closed contract as get_known_order_ids(): returns
    {} (not a partial result) on any read/parse failure. Dict order follows
    ledger file order (oldest cycle first) -- a caller wanting "most
    recently posted" can just take the tail of this dict's items()."""
    return {order_id: details["market_id"] for order_id, details in get_known_order_details().items()}


def get_known_order_details() -> dict[str, dict]:
    """Every order id this bot has ever recorded posting, mapped to
    {"market_id", "side", "price"} -- the market slug, BUY/SELL, and the
    bot's own intended quote price at post time, for that order. One walk
    of get_all_cycles() underlies get_known_order_id_markets()/
    get_known_order_ids() too (rebuilt on top of this), and live/fills.py
    uses it to enrich a detected fill with the side/market/intended-price it
    can't reliably get from the private-WS execution message alone (see
    live/RUNBOOK.md's "-9." section).

    Same fail-closed contract as get_known_order_ids(): returns {} (not a
    partial result) on any read/parse failure, logging loudly rather than
    raising -- a crash reading local state must never be allowed to kill an
    entire refresh cycle."""
    try:
        order_details: dict[str, dict] = {}
        for cycle in get_all_cycles():
            market_id = cycle.get("market_id")
            for leg_key in ("bid", "ask"):
                leg = cycle.get(leg_key) or {}
                if not isinstance(leg, dict):
                    continue  # one malformed record shouldn't taint the rest
                order_id = leg.get("order_id")
                if order_id:
                    order_details[order_id] = {
                        "market_id": market_id,
                        "side": leg.get("side"),
                        "price": leg.get("price"),
                    }
        return order_details
    except Exception as exc:  # noqa: BLE001 -- local-state read must never crash a cycle
        logger.error(
            "Could not read the live ledger to determine known order ids -- "
            "treating ownership as fully unknown this cycle (no stale/"
            "unmanaged order will be cancelled) rather than crashing: %s", exc,
        )
        return {}


def get_total_position_pnl_usd(client: LiveUsClient) -> Optional[float]:
    """Cumulative, non-resetting P/L (unrealized + realized, summed across
    every currently held position) right now. Unlike estimate_daily_pnl_usd,
    this is NOT diffed against a UTC-midnight baseline -- live/
    equity_protection.py's lifetime/session peak-account-value check needs a
    figure that doesn't reset every day the way the daily-loss circuit
    breaker deliberately does. Returns None if positions can't be fetched.

    Known gap (shared with estimate_daily_pnl_usd, which this now backs): a
    position that fully closes to flat may drop out of get_all_positions()
    before its final realized P/L is captured this way -- a known, smaller
    gap than the balance-field noise this replaced."""
    try:
        positions = client.get_all_positions()
    except UsApiError as exc:
        logger.warning("Could not fetch positions for P/L estimate: %s", exc)
        return None
    return round(sum_position_pnl(positions), 4)


def estimate_daily_pnl_usd(client: LiveUsClient) -> Optional[float]:
    """Best-effort daily P/L: get_total_position_pnl_usd() right now minus
    the same figure recorded at the start of today. Returns None if
    positions can't be fetched."""
    total_now = get_total_position_pnl_usd(client)
    if total_now is None:
        return None

    today = datetime.now(timezone.utc).date().isoformat()
    state = storage.load_json(DAILY_BALANCE_FILE, default={})
    if state.get("date") != today:
        state = {"date": today, "baseline": total_now}
        storage.save_json(DAILY_BALANCE_FILE, state)

    return round(total_now - state["baseline"], 4)


def sum_position_pnl(positions: dict) -> float:
    total = 0.0
    for position in positions.values():
        if not isinstance(position, dict):
            continue
        cost = _to_float(_nested(position, "cost", "value"))
        cash_value = _to_float(_nested(position, "cashValue", "value"))
        realized = _to_float(_nested(position, "realized", "value"))
        total += (cash_value - cost) + realized
    return total


def _nested(d: dict, *keys: str):
    for key in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
