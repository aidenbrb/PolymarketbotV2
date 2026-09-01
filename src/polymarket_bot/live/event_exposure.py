"""Pure correlation-bucket math -- no network calls, no client, no state
mutation (a resting order's unattributable-to-ledger case logs a warning,
the one side effect in this file). See live/RUNBOOK.md's most recent
event-exposure section.

Groups markets that share the same underlying real-world event (e.g. many
"corner count over/under" props on one soccer match) into one risk bucket,
so capital concentration can be measured and capped per-event instead of
per-market -- a bot can look diversified by position COUNT while actually
having most of its capital riding on one or two correlated outcomes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from ..logger import get_logger

logger = get_logger("live.event_exposure")

_DATE_TOKEN_RE = re.compile(r"^\d{4}$")
_TWO_DIGIT_RE = re.compile(r"^\d{2}$")


def derive_event_bucket_key(market_slug: str, raw: Optional[dict[str, Any]] = None) -> str:
    """eventSlug -> eventId -> slug-heuristic fallback chain.

    The first two are forward-compat only: confirmed ABSENT from the real
    public market-listing API today (a dump of 5000 real records found no
    key containing "event" anywhere). Real eventSlug values do exist, but
    only in the private, authenticated execution WebSocket schema
    (execution["order"]["marketMetadata"]["eventSlug"]), fetched only after
    a fill -- never available at candidate-selection time. Kept here anyway
    so this function automatically starts using them for free if a future
    API revision ever adds them to market-listing too.

    The heuristic: split market_slug on "-", drop the first token (a short
    market-type code -- astatc/atc/aqc/tec/tsc/asc/... -- confirmed several
    distinct such prefixes exist across real data), then scan the remaining
    tokens for the first 3-token run matching \\d{4}, \\d{2}, \\d{2} (a
    YYYY-MM-DD date split across hyphens). The bucket key is every remaining
    token through the end of that date triplet, inclusive. If no date
    triplet is found, or fewer than 2 tokens remain to start with, the whole
    remainder (or the whole slug) becomes its own singleton bucket --
    degrades safely to "no grouping" for that one market, never raises.

    Independently re-verified against every real slug this bot has actually
    quoted (94 slugs from fills.json/orders.json): correct in every case,
    and where real eventSlug ground-truth also happens to be available (in
    fills.json's private-execution data), the heuristic's output is an
    exact character-for-character match in 3 of 4 comparable cases.

    Known coarsening, not a bug: single-team-name slug families with no
    opponent token (e.g. a tournament-advancement prop naming only one
    team) collapse to one shared tournament-stage bucket even when they're
    plausibly different physical fixtures -- the string alone can't recover
    match-pairing that isn't in it. This over-groups (more conservative
    capping), never under-groups (a missed real correlation), which is the
    safe failure direction.
    """
    if raw:
        event_slug = raw.get("eventSlug")
        if event_slug:
            return str(event_slug)
        event_id = raw.get("eventId")
        if event_id:
            return str(event_id)
    return _infer_event_bucket_from_slug(market_slug)


def _infer_event_bucket_from_slug(slug: str) -> str:
    tokens = slug.split("-") if slug else []
    if len(tokens) < 2:
        return slug
    rest = tokens[1:]
    date_end = _find_date_triplet_end(rest)
    if date_end is not None:
        return "-".join(rest[:date_end])
    return "-".join(rest)


def _find_date_triplet_end(tokens: list[str]) -> Optional[int]:
    for i in range(len(tokens) - 2):
        if (
            _DATE_TOKEN_RE.match(tokens[i])
            and _TWO_DIGIT_RE.match(tokens[i + 1])
            and _TWO_DIGIT_RE.match(tokens[i + 2])
        ):
            return i + 3
    return None


def is_stat_prop_market(raw: Optional[dict[str, Any]]) -> bool:
    """True iff raw["marketType"] == "props" -- confirmed present on 100% of
    5000 real sampled market records (an exhaustive enum, never missing),
    confirmed 1:1-identical to raw["sportsMarketTypeV2"] ==
    "SPORTS_MARKET_TYPE_PROP" by direct set-equality check against real
    data, and confirmed to correctly classify every one of this bot's real
    open stat-prop positions as True and every non-prop futures/moneyline
    slug as False."""
    if not raw:
        return False
    return str(raw.get("marketType") or "").lower() == "props"


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


def _position_cost(position: dict[str, Any]) -> float:
    return abs(_to_float((position.get("cost") or {}).get("value")))


def _position_cash_value(position: dict[str, Any]) -> float:
    return _to_float((position.get("cashValue") or {}).get("value"))


@dataclass
class EventExposure:
    bucket_key: str
    market_count: int
    cost_basis_usd: float
    cash_value_usd: float
    unrealized_pnl_usd: float
    stat_prop_cost_basis_usd: float
    pct_of_capital: Optional[float]  # None when capital_reference_usd is None
    stat_prop_pct_of_capital: Optional[float]
    # Resting-order worst-case: this bucket's still-open, increasing-side
    # orders' worst-case capital (BUY: shares*price, SELL:
    # shares*(1-price)), summed -- money not yet settled into a position
    # but already committed if those orders fill. Added to cost_basis_usd to
    # get a PROJECTED exposure, closing the real gap that let the
    # Argentina-Switzerland bucket overshoot its cap on 2026-07-09 (a
    # resting order filled and pushed the bucket over cap one cycle before
    # the settled-only snapshot could see it). See live/RUNBOOK.md's most
    # recent section.
    resting_worst_case_usd: float = 0.0
    stat_prop_resting_worst_case_usd: float = 0.0
    projected_pct_of_capital: Optional[float] = None
    stat_prop_projected_pct_of_capital: Optional[float] = None


def resolve_capital_reference_usd(
    starting_capital_usd: float,
    total_position_pnl_usd: float,
    positions: dict[str, dict[str, Any]],
) -> Optional[float]:
    """starting_capital_usd > 0 -> starting_capital_usd + total_position_pnl_usd
    (the exact "account value" formula live/equity_protection.py already
    established). Otherwise -> sum of deployed cost basis across every
    currently-held (nonzero net) position, as a conservative proxy. Both
    zero -> None, meaning "cannot evaluate this cycle" -- the caller must
    skip the event-exposure cap check entirely rather than divide by zero
    or block everything, mirroring equity_protection.py's own
    inactive-when-unconfigured convention."""
    if starting_capital_usd > 0:
        return starting_capital_usd + total_position_pnl_usd

    deployed = sum(
        _position_cost(position)
        for position in positions.values()
        if isinstance(position, dict) and _net_position(position) != 0
    )
    return deployed if deployed > 0 else None


def _order_id(order: dict[str, Any]) -> Optional[str]:
    return order.get("id") or order.get("orderId")


def _order_market_slug(order: dict[str, Any]) -> Optional[str]:
    return order.get("marketSlug") or order.get("market_slug")


def _resting_worst_case_by_bucket(
    positions: dict[str, dict[str, Any]],
    open_orders: list[dict[str, Any]],
    order_details: dict[str, dict[str, Any]],
    raw_by_slug: dict[str, dict[str, Any]],
) -> dict[str, dict[str, float]]:
    """Sums worst-case capital for still-open, increasing orders per event
    bucket, ACROSS markets -- a reducing-side resting order doesn't add
    bucket risk if it fills, so it's excluded. Resolves each order's
    side/price/size from the bot's OWN ledger (order_details, keyed by
    order_id), never from the exchange's open-order response directly --
    that schema has never been parsed for anything beyond id/marketSlug
    anywhere in this codebase, so trusting it for quantity/price would be
    an unverified-schema risk (see live/RUNBOOK.md's most recent section).
    An order with no matching ledger entry is skipped with a warning rather
    than guessed at -- should be rare given the documented
    single-strategy-account assumption (RUNBOOK "-6." section).

    Two-pass: within ONE market, a flat position (net_position == 0) has
    BOTH sides "increasing" simultaneously, but a filled bid and filled ask
    on the SAME token can't both be simultaneously realized risk -- if both
    fill, they net to a captured spread, not additive exposure. Pass 1
    takes the MAX of a single market's own increasing legs, not their sum
    (reverting an earlier, since-reconsidered fix that summed them here --
    see live/RUNBOOK.md's most recent section for why, and the regression
    test replaying the real 2026-07-09 incident that gates this change).
    Pass 2 sums each market's own worst-case ACROSS markets in the same
    bucket -- that stays fully additive, since DIFFERENT markets filling
    within the same cycle is the real incident's exact mechanism and is
    untouched by this."""
    per_market_worst_case: dict[str, float] = {}
    per_market_stat_prop: dict[str, bool] = {}
    for order in open_orders:
        if not isinstance(order, dict):
            continue
        order_id = _order_id(order)
        slug = _order_market_slug(order)
        if not order_id or not slug:
            continue
        details = order_details.get(order_id)
        if not details:
            logger.warning(
                "Open order %s on %s has no matching ledger entry -- "
                "excluded from projected event-exposure headroom (not "
                "necessarily wrong, but a blind spot).", order_id, slug,
            )
            continue
        side = details.get("side")
        price = details.get("price")
        size = details.get("size")
        if side not in ("BUY", "SELL") or price is None or size is None:
            continue
        net_position = _net_position(positions.get(slug, {}))
        is_increasing = (
            (side == "BUY" and net_position >= 0)
            or (side == "SELL" and net_position <= 0)
        )
        if not is_increasing:
            continue
        # BUY YES risks the premium paid; SELL YES (a short/NO-equivalent)
        # risks the complementary payout. Using price for both materially
        # understates high-priced shorts, the exact ugly-payoff exposure the
        # event cap is meant to constrain.
        worst_case_per_share = float(price) if side == "BUY" else 1.0 - float(price)
        worst_case = float(size) * max(0.0, worst_case_per_share)
        per_market_worst_case[slug] = max(per_market_worst_case.get(slug, 0.0), worst_case)
        if is_stat_prop_market(raw_by_slug.get(slug)):
            per_market_stat_prop[slug] = True

    result: dict[str, dict[str, float]] = {}
    for slug, worst_case in per_market_worst_case.items():
        bucket_key = derive_event_bucket_key(slug, raw_by_slug.get(slug))
        bucket = result.setdefault(bucket_key, {"resting_worst_case_usd": 0.0, "stat_prop_resting_worst_case_usd": 0.0})
        bucket["resting_worst_case_usd"] += worst_case
        if per_market_stat_prop.get(slug):
            bucket["stat_prop_resting_worst_case_usd"] += worst_case
    return result


def compute_event_exposures(
    positions: dict[str, dict[str, Any]],
    capital_reference_usd: Optional[float],
    raw_by_slug: Optional[dict[str, dict[str, Any]]] = None,
    open_orders: Optional[list[dict[str, Any]]] = None,
    order_details: Optional[dict[str, dict[str, Any]]] = None,
) -> list[EventExposure]:
    """Groups every nonzero-net position by derive_event_bucket_key, using
    raw_by_slug[slug] when available (e.g. a held position that's ALSO this
    cycle's ranked candidate, giving the eventSlug/eventId forward-compat
    chain a real shot) else None, falling straight to the slug heuristic.

    open_orders/order_details (both optional, default None) additionally
    fold in each bucket's still-open, increasing-side resting orders'
    worst-case USD into resting_worst_case_usd/projected_pct_of_capital --
    see _resting_worst_case_by_bucket. Omitting them reproduces the exact
    prior settled-only behavior (resting_worst_case_usd=0.0,
    projected_pct_of_capital == pct_of_capital), so every existing caller
    is unaffected."""
    raw_by_slug = raw_by_slug or {}
    buckets: dict[str, dict[str, Any]] = {}

    for slug, position in positions.items():
        if not isinstance(position, dict):
            continue
        if _net_position(position) == 0:
            continue

        bucket_key = derive_event_bucket_key(slug, raw_by_slug.get(slug))
        bucket = buckets.setdefault(bucket_key, {
            "market_count": 0,
            "cost_basis_usd": 0.0,
            "cash_value_usd": 0.0,
            "stat_prop_cost_basis_usd": 0.0,
        })
        cost = _position_cost(position)
        cash_value = _position_cash_value(position)
        bucket["market_count"] += 1
        bucket["cost_basis_usd"] += cost
        bucket["cash_value_usd"] += cash_value
        if is_stat_prop_market(raw_by_slug.get(slug)):
            bucket["stat_prop_cost_basis_usd"] += cost

    resting_by_bucket = _resting_worst_case_by_bucket(
        positions, open_orders or [], order_details or {}, raw_by_slug,
    )
    for bucket_key, resting in resting_by_bucket.items():
        buckets.setdefault(bucket_key, {
            "market_count": 0,
            "cost_basis_usd": 0.0,
            "cash_value_usd": 0.0,
            "stat_prop_cost_basis_usd": 0.0,
        })

    exposures = []
    for bucket_key, bucket in buckets.items():
        resting = resting_by_bucket.get(bucket_key, {})
        resting_worst_case = resting.get("resting_worst_case_usd", 0.0)
        stat_prop_resting_worst_case = resting.get("stat_prop_resting_worst_case_usd", 0.0)
        pct = (
            bucket["cost_basis_usd"] / capital_reference_usd
            if capital_reference_usd else None
        )
        stat_prop_pct = (
            bucket["stat_prop_cost_basis_usd"] / capital_reference_usd
            if capital_reference_usd else None
        )
        projected_pct = (
            (bucket["cost_basis_usd"] + resting_worst_case) / capital_reference_usd
            if capital_reference_usd else None
        )
        stat_prop_projected_pct = (
            (bucket["stat_prop_cost_basis_usd"] + stat_prop_resting_worst_case) / capital_reference_usd
            if capital_reference_usd else None
        )
        exposures.append(EventExposure(
            bucket_key=bucket_key,
            market_count=bucket["market_count"],
            cost_basis_usd=bucket["cost_basis_usd"],
            cash_value_usd=bucket["cash_value_usd"],
            unrealized_pnl_usd=bucket["cash_value_usd"] - bucket["cost_basis_usd"],
            stat_prop_cost_basis_usd=bucket["stat_prop_cost_basis_usd"],
            pct_of_capital=pct,
            stat_prop_pct_of_capital=stat_prop_pct,
            resting_worst_case_usd=resting_worst_case,
            stat_prop_resting_worst_case_usd=stat_prop_resting_worst_case,
            projected_pct_of_capital=projected_pct,
            stat_prop_projected_pct_of_capital=stat_prop_projected_pct,
        ))
    return exposures
