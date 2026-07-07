"""Selects markets for live market-making.

Reuses the existing MarketScanner / MarketFilters / ScoringEngine pipeline
exactly as `main.py::cmd_scan` does for paper trading for the quality bar
(liquidity, volume, not-expired, overall tier) -- no separate discovery
logic lives here. No live-order code lives in this module.

Ranking is deliberately NOT by ScoringEngine's total_score. That score
rewards a TIGHT spread (good for a researcher/taker who wants easy
execution) -- exactly backwards for a market maker, who only has an edge to
capture if the REAL spread is wide enough to profitably improve on both
sides (see live/pricing.py::compute_book_aware_quote). So among markets that
clear the general quality bar, this ranks by a heuristic expected-value
proxy: captured spread after one-tick improvement times a volume/liquidity
fill-confidence proxy. This is not a calibrated fill model yet; it is a
smarter pre-fetch ranking heuristic using only market-list data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import config
from ..filters import MarketFilters
from ..logger import get_logger
from ..market_scanner import MarketScanner
from ..models import ScoredMarket
from ..scoring_engine import ScoringEngine
from .event_exposure import derive_event_bucket_key

logger = get_logger("live.market_selection")

_RECOMMENDATION_TIERS = ["REJECT", "WATCH", "STRONG_WATCH", "PAPER_CANDIDATE"]


def select_target_market(
    scanner: Optional[MarketScanner] = None,
    filters: Optional[MarketFilters] = None,
    scoring: Optional[ScoringEngine] = None,
    settings: Optional[config.LiveTradingSettings] = None,
) -> Optional[ScoredMarket]:
    """Pick the single highest-ranked eligible market with enough edge to be
    worth quoting at all. This strategy only ever quotes one instrument at a
    time, re-selected once per bot run."""
    # Resolved here (not just left to select_target_markets' own internal
    # fallback) so the log line below always has a real settings object --
    # select_target_markets reassigning its OWN local `settings` doesn't
    # propagate back to this function's variable of the same name.
    settings = settings or config.load_settings().live
    targets = select_target_markets(
        scanner=scanner, filters=filters, scoring=scoring, settings=settings, max_targets=1
    )
    chosen = targets[0] if targets else None
    if chosen is None:
        return None
    if settings.rank_by_expected_value:
        logger.info(
            "Selected market for live quoting: %s (score=%.1f, ev_proxy=%.4f, real_spread=%.2fc)",
            chosen.question, chosen.total_score, _expected_value_score(chosen, settings),
            (chosen.market.spread or 0) * 100,
        )
    else:
        # ev_proxy would be misleading here -- raw spread, not the EV score,
        # actually drove ranking while the escape hatch is active.
        logger.info(
            "Selected market for live quoting: %s (score=%.1f, real_spread=%.2fc)",
            chosen.question, chosen.total_score, (chosen.market.spread or 0) * 100,
        )
    return chosen


def select_target_markets(
    scanner: Optional[MarketScanner] = None,
    filters: Optional[MarketFilters] = None,
    scoring: Optional[ScoringEngine] = None,
    settings: Optional[config.LiveTradingSettings] = None,
    max_targets: Optional[int] = None,
    raw_by_slug_out: Optional[dict[str, dict]] = None,
) -> list[ScoredMarket]:
    """Pick eligible markets from the newest scan window, ranked by a
    heuristic EV proxy by default. The caller decides how many orders to
    place; this function only ranks opportunities.

    If raw_by_slug_out is provided, it's populated (in place) with every
    scanned market's raw data -- BEFORE eligibility filtering -- at zero
    extra scan cost. This lets a caller (multi_market_maker.py, via
    ws_runner.py) look up raw market data (gameStartTime/endDate/marketType)
    for a held position whose market fell out of candidacy, which otherwise
    has no raw data available anywhere (see live/RUNBOOK.md's risk-tightening
    section)."""
    settings = settings or config.load_settings().live
    scanner = scanner or MarketScanner()
    filters = filters or _live_filters(settings)
    scoring = scoring or ScoringEngine()

    page_limit = config.load_settings().api.page_limit
    scan_limit = max(
        1,
        settings.newest_market_scan_limit,
        settings.recent_market_scan_limit,
    )
    max_pages = max(1, (scan_limit + page_limit - 1) // page_limit)

    markets = scanner.scan(max_pages=max_pages)[:scan_limit]
    if raw_by_slug_out is not None:
        raw_by_slug_out.update({m.market_id: m.raw for m in markets})
    markets = _newest_first(markets)
    accepted, _ = filters.apply(markets)
    scored = [scoring.score(m) for m in accepted]

    eligible = [s for s in scored if is_eligible(s, settings)]
    if not eligible:
        logger.warning("No eligible markets found for live market-making.")
        return []

    tradable = [s for s in eligible if _has_minimum_edge(s, settings)]
    if not tradable:
        logger.warning(
            "No eligible market has a real spread wide enough to quote "
            "profitably (need >= %.2fc after improving both sides by a "
            "tick) -- skipping this selection rather than forcing a trade "
            "with no edge.",
            settings.min_edge_cents,
        )
        return []

    tradable.sort(
        key=lambda s: (
            _ranking_primary(s, settings),
            -_days_to_event_or_close(s.market),
            _incentive_priority(s, settings),
            _depth_proxy(s),
            _created_timestamp(s.market.raw if s.market else {}),
        ),
        reverse=True,
    )
    # With EV ranking, the primary key is a near-continuous float product, so
    # the secondary incentive/depth/recency tiebreakers should fire rarely.
    tradable = _diversify_by_event(tradable, max_targets, settings.max_markets_per_event)
    logger.info(
        "Selected %d live quote candidates from %d scanned markets.",
        len(tradable), scan_limit,
    )
    return tradable


def _diversify_by_event(
    tradable: list[ScoredMarket], max_targets: Optional[int], max_per_event: int
) -> list[ScoredMarket]:
    """Replaces the old plain `tradable[:max_targets]` slice: walks the
    already-rank-sorted list left to right, capping how many candidates from
    any one correlated-event bucket (see live/event_exposure.py) can be
    selected. Since the input is already correctly rank-ordered (EV-score
    descending), this naturally prefers globally-better-ranked markets first
    while capping any one event's share of the candidate list -- no
    re-ranking needed. Count-based only (not pct-of-capital): this module
    has zero dependency on live account state today (a deliberate
    architectural boundary), so the pct-based caps live in
    multi_market_maker.py instead, where position/capital data actually is."""
    selected: list[ScoredMarket] = []
    per_bucket: dict[str, int] = {}
    for scored in tradable:
        if max_targets is not None and len(selected) >= max_targets:
            break
        raw = scored.market.raw if scored.market else None
        slug = scored.market.market_id if scored.market else ""
        bucket = derive_event_bucket_key(slug, raw)
        if per_bucket.get(bucket, 0) >= max_per_event:
            continue
        selected.append(scored)
        per_bucket[bucket] = per_bucket.get(bucket, 0) + 1
    return selected


def _live_filters(settings: config.LiveTradingSettings) -> MarketFilters:
    return MarketFilters(
        config.FilterSettings(
            min_liquidity=settings.min_liquidity,
            min_volume_24h=settings.min_volume_24h,
            max_spread=settings.max_spread,
            min_hours_to_close=settings.min_hours_to_close,
            max_days_to_close=365.0,
            require_order_book=True,
        )
    )


def _has_minimum_edge(scored: ScoredMarket, settings: config.LiveTradingSettings) -> bool:
    """Raw market.spread overstates what's actually capturable: the bot
    joins/improves both the best bid and best ask by one tick each before
    quoting (see live/pricing.py::compute_book_aware_quote), so the real
    captured spread is the raw spread minus two ticks, not the raw spread
    itself. A 2c-wide book with a 1c tick has 0c left after improving both
    sides -- selection must reject that, not just the order placer."""
    market = scored.market
    if market is None or market.spread is None:
        return False
    return _captured_spread_cents(market) >= settings.min_edge_cents


def _captured_spread_cents(market, tick_size: Optional[float] = None) -> float:
    if market is None or market.spread is None:
        return 0.0
    tick_size = _get_tick_size(market) if tick_size is None else tick_size
    return (market.spread - (2 * tick_size)) * 100


def _get_tick_size(market, default: float = 0.01) -> float:
    try:
        return float(market.raw.get("orderPriceMinTickSize"))
    except (AttributeError, TypeError, ValueError):
        return default


def _is_verified_binary_yes_no(market) -> bool:
    """Conservative, explicit check: only a market with exactly two
    outcomes/token_ids that literally read "Yes"/"No" is treated as verified
    for live quoting. live/market_maker.py always uses OUTCOME_SIDE_YES for
    both legs -- that's only confirmed correct for a literal binary Yes/No
    market, not for team-name-labeled sports moneylines or multi-way
    markets (see live/RUNBOOK.md). Ambiguous metadata is rejected rather
    than guessed at."""
    if market is None:
        return False
    if len(market.token_ids) != 2:
        return False
    outcomes = [str(o).strip().lower() for o in (market.outcomes or [])]
    if len(outcomes) != 2:
        return False
    return set(outcomes) == {"yes", "no"}


def is_eligible(scored: ScoredMarket, settings: config.LiveTradingSettings) -> bool:
    market = scored.market
    if market is None or not market.token_ids:
        return False

    if not settings.allow_non_binary_markets and not _is_verified_binary_yes_no(market):
        return False

    try:
        min_tier_index = _RECOMMENDATION_TIERS.index(settings.min_score_recommendation)
        scored_tier_index = _RECOMMENDATION_TIERS.index(scored.recommendation)
    except ValueError:
        return False
    if scored_tier_index < min_tier_index:
        return False

    category = (market.category or "").lower()
    if category in settings.exclude_categories:
        return False

    hours_to_event = hours_to_event_or_close(market)
    if hours_to_event > settings.max_days_to_close * 24:
        return False
    if hours_to_event < -settings.max_started_event_hours:
        return False

    market_type = _market_type(market)
    if market_type in settings.exclude_market_types:
        return False

    question_lower = market.question.lower()
    if any(keyword in question_lower for keyword in settings.exclude_question_keywords):
        return False

    return True


def _incentive_priority(scored: ScoredMarket, settings: config.LiveTradingSettings) -> int:
    category = ((scored.market.category if scored.market else None) or "").lower()
    return int(bool(category and category in settings.incentive_categories))


def _ranking_primary(scored: ScoredMarket, settings: config.LiveTradingSettings) -> float:
    if settings.rank_by_expected_value:
        return _expected_value_score(scored, settings)
    market = scored.market
    if market is None:
        return 0.0
    return market.spread or 0.0


def _expected_value_score(scored: ScoredMarket, settings: config.LiveTradingSettings) -> float:
    market = scored.market
    if market is None:
        return 0.0
    return _captured_spread_cents(market) * _fill_confidence(scored, settings)


def _fill_confidence(scored: ScoredMarket, settings: config.LiveTradingSettings) -> float:
    """Volume/liquidity-based confidence proxy, not a calibrated fill
    probability. The actual order placer still validates live book depth
    immediately before quoting."""
    confidence = _normalize_confidence(
        _depth_proxy(scored), settings.fill_confidence_reference_depth
    )
    floor = max(0.0, min(1.0, settings.min_fill_confidence))
    return max(floor, confidence)


def _normalize_confidence(value: float, reference: float) -> float:
    if reference <= 0:
        return 1.0 if value > 0 else 0.0
    return max(0.0, min(1.0, value / reference))


def _depth_proxy(scored: ScoredMarket) -> float:
    """Cheap volume/liquidity proxy from market-list data. It is not live
    order-book depth; the actual order placer validates L2 depth immediately
    before quoting."""
    market = scored.market
    if market is None:
        return 0.0
    return max(market.volume_24h, 0.0) + max(market.liquidity, 0.0) * 0.1


def _days_to_close(market) -> float:
    return _days_from_raw_or_end_date(market, ("endDate",))


def _market_type(market) -> str:
    raw = getattr(market, "raw", {}) or {}
    return str(raw.get("marketType") or raw.get("sportsMarketType") or "").lower()


def _days_to_event_or_close(market) -> float:
    if _market_type(market) == "futures":
        # Season-long futures (division winner, etc.) never resolve when the
        # underlying team's next game starts -- Polymarket still populates
        # gameStartTime with that next game's time, which would make a
        # market months from resolution look like it closes tomorrow.
        return _days_from_raw_or_end_date(market, ("endDate",))
    return _days_from_raw_or_end_date(market, ("gameStartTime", "endDate"))


def hours_to_event_or_close(market) -> float:
    """Not underscore-prefixed -- multi_market_maker.py reuses this directly
    (see live/RUNBOOK.md's risk-tightening section) rather than
    reimplementing time-to-event math."""
    return _days_to_event_or_close(market) * 24


def _days_from_raw_or_end_date(market, raw_keys: tuple[str, ...]) -> float:
    for key in raw_keys:
        raw = getattr(market, "raw", {}) or {}
        value = raw.get(key)
        if value:
            return _days_from_iso(value)
    end_date = getattr(market, "end_date", None)
    if not end_date:
        return 0.0
    return _days_from_iso(end_date)


def _days_from_iso(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed - datetime.now(timezone.utc)).total_seconds() / 86400


def _newest_first(markets):
    return sorted(markets, key=lambda m: _created_timestamp(getattr(m, "raw", {})), reverse=True)


def _created_timestamp(raw: dict) -> float:
    value = raw.get("createdAt") or raw.get("startDate") or raw.get("updatedAt")
    if not isinstance(value, str):
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
