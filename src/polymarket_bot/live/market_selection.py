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
from ..models import FilterResult, Market, ScoredMarket
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
    activity_scores: Optional[dict[str, float]] = None,
    observation_markets_out: Optional[list[ScoredMarket]] = None,
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
    section).

    activity_scores, if provided, is a market_id -> real-activity-confidence
    (0..1) map -- see _activity_confidence -- used ONLY to override the
    static volume/liquidity fill-confidence proxy for markets it covers.
    The REST market-list scan itself has no activity fields (confirmed live
    against the real API -- see live/RUNBOOK.md's most recent section), so
    this can only ever come from a caller with its own WebSocket-observed
    history (ws_runner.py); the REST-only runner.py path never passes it."""
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
    _warn_if_liquidity_data_looks_broken(markets)
    markets = _newest_first(markets)
    if observation_markets_out is not None:
        all_scored = [scoring.score(market) for market in markets]
        observation_markets = [
            item for item in all_scored if is_observation_eligible(item, settings)
        ]
        observation_markets.sort(
            key=lambda item: (
                _created_timestamp(item.market.raw if item.market else {}),
                _depth_proxy(item),
            ),
            reverse=True,
        )
        observation_markets_out.extend(observation_markets)
    accepted, rejected = filters.apply(markets)
    accepted += _l2_depth_liquidity_fallback(scanner, markets, rejected, settings)
    # Re-score accepted markets after the L2 fallback, which can populate a
    # previously-missing liquidity value used by ScoringEngine.
    scored = [scoring.score(market) for market in accepted]

    eligible = [s for s in scored if is_eligible(s, settings)]
    if not eligible:
        logger.warning("No eligible markets found for live market-making.")
        return []

    tradable = [
        s for s in eligible
        if _has_minimum_edge(s, settings) and _passes_static_paired_entry_guard(s, settings)
    ]
    if not tradable:
        logger.warning(
            "No eligible market has both enough capturable spread and a "
            "safe paired entry (need >= %.2fc after improving both sides; "
            "payoff ratio cap %.1fx) -- skipping rather than forcing a trade.",
            settings.min_edge_cents, settings.max_payoff_loss_to_capture_ratio,
        )
        return []

    tradable.sort(
        key=lambda s: (
            _ranking_primary(s, settings, activity_scores),
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


# A scanned market that's genuinely quiet is expected occasionally; the
# whole scan showing zero liquidity is not -- that's the signature of a
# missing/renamed upstream API field (confirmed real: Polymarket US's
# market-listing response stopped including volume/volume24hr entirely for
# ~36+ hours starting 2026-07-18, silently zeroing every market's liquidity
# proxy and starving candidate selection with no distinct signal in the
# logs -- see live/RUNBOOK.md's most recent section). Conservative
# threshold: real illiquid-market noise shouldn't plausibly hit 95% of an
# entire scan at once.
_BROKEN_LIQUIDITY_DATA_FRACTION = 0.95


def _warn_if_liquidity_data_looks_broken(markets: list) -> None:
    if not markets:
        return
    # Purely diagnostic -- must never be able to crash real selection on an
    # unexpected market shape. getattr, not m.liquidity: a market missing
    # the attribute entirely is itself zero-signal, not an error here.
    zero_liquidity_count = sum(1 for m in markets if getattr(m, "liquidity", 0.0) <= 0)
    zero_fraction = zero_liquidity_count / len(markets)
    if zero_fraction >= _BROKEN_LIQUIDITY_DATA_FRACTION:
        logger.warning(
            "%d of %d scanned markets (%.0f%%) show zero liquidity -- this "
            "looks like a market-data problem (a missing/renamed upstream "
            "API field), not genuinely quiet markets. Every candidate will "
            "likely be rejected on the liquidity filter until this "
            "resolves. See live/RUNBOOK.md's most recent section.",
            zero_liquidity_count, len(markets), zero_fraction * 100,
        )


_LIQUIDITY_ONLY_REJECTION_PREFIXES = ("Liquidity ", "24h volume ")
_L2_RECOVERED_MARKER = "_live_l2_liquidity_recovered"


def _l2_depth_liquidity_fallback(
    scanner: MarketScanner,
    markets: list[Market],
    rejected: list[FilterResult],
    settings: config.LiveTradingSettings,
) -> list[Market]:
    """A market rejected ONLY for liquidity/volume (every other quality-bar
    check in filters.py already passed) gets one more chance via a real L2
    order-book depth lookup -- volume/volume24hr can go missing upstream
    entirely (a confirmed real incident -- see live/RUNBOOK.md's most
    recent section) without the market itself actually being illiquid.
    Bounded to liquidity_fallback_max_lookups lookups (0 disables this
    entirely) -- the account-wide rate limit is 20 req/s
    (docs.polymarket.us), so doing this across an entire multi-thousand-
    market scan is not viable. Recovered markets have `.liquidity`
    overwritten with the L2-derived share-count depth (same unit
    volume/liquidity are documented in) so downstream scoring/eligibility
    sees a real number instead of the missing upstream one."""
    limit = settings.liquidity_fallback_max_lookups
    if limit <= 0 or not rejected:
        return []

    candidates = [
        r for r in rejected
        if r.reasons and all(reason.startswith(_LIQUIDITY_ONLY_REJECTION_PREFIXES) for reason in r.reasons)
    ]
    if not candidates:
        return []

    markets_by_id = {m.market_id: m for m in markets}
    # The upstream liquidity outage can leave more than a thousand otherwise
    # valid markets competing for a bounded number of L2 lookups.  The old
    # order was merely the market listing's creation order, so long-dated
    # futures could consume all 500 lookups before today's actually
    # entry-relevant markets were checked.  Put the same broad, hard-safety
    # observation universe first, then prefer the nearest upcoming event.
    # sort() is stable, so markets with identical priority keep their scan
    # order and the fallback remains deterministic.
    candidates.sort(
        key=lambda result: _l2_fallback_priority(
            markets_by_id.get(result.market_id), settings,
        ),
        reverse=True,
    )
    recovered: list[Market] = []
    for result in candidates[:limit]:
        market = markets_by_id.get(result.market_id)
        if market is None:
            continue
        book = scanner.client.get_market_book(market.market_id)
        if not book:
            continue
        depth_shares = sum(level.get("quantity", 0.0) for level in book.get("bids", [])) + sum(
            level.get("quantity", 0.0) for level in book.get("asks", [])
        )
        if depth_shares >= settings.min_liquidity:
            market.liquidity = depth_shares
            _hydrate_market_quote_from_l2(market, book)
            # The generic research score depends on the missing REST volume
            # fields and rewards tight spreads, so its recommendation tier
            # is not meaningful for an outage-recovered market-maker
            # opportunity. is_eligible() recognizes this in-memory marker
            # only after real L2 depth has passed the configured threshold.
            market.raw[_L2_RECOVERED_MARKER] = True
            recovered.append(market)

    if recovered:
        logger.info(
            "L2-depth liquidity fallback recovered %d market(s) rejected only for "
            "missing/zero volume -- their real order books show usable depth.",
            len(recovered),
        )
    return recovered


def _l2_fallback_priority(
    market: Optional[Market], settings: config.LiveTradingSettings,
) -> tuple[int, float, float]:
    """Prioritize scarce fallback lookups toward plausible near-term entries.

    This is only lookup ordering, never an eligibility bypass.  Every
    recovered market still passes the normal filter, score, static safety,
    edge, and paired-entry checks below.
    """
    if market is None:
        return (0, float("-inf"), 0.0)
    broadly_eligible = _is_observation_market_eligible(market, settings)
    hours = hours_to_event_or_close(market)
    # A smaller positive time-to-event is more useful for the current
    # three-day live horizon, hence the negative value under reverse sort.
    upcoming_priority = -hours if hours >= 0 else float("-inf")
    return (
        int(broadly_eligible),
        upcoming_priority,
        _created_timestamp(market.raw or {}),
    )


def _hydrate_market_quote_from_l2(
    market: Market, book: dict,
) -> None:
    """Make the L2 fallback authoritative for both depth *and* current BBO.

    The prior fallback replaced only ``market.liquidity``. During the real
    upstream outage the REST listing's quote could be stale too (for
    example 0.26/0.27 while the fetched L2 book was 0.26/0.41), causing the
    recovered market to be rejected using the very snapshot the fallback
    had just disproved.
    """
    bids = [
        float(level["price"])
        for level in book.get("bids", [])
        if isinstance(level, dict) and level.get("price") is not None
    ]
    asks = [
        float(level["price"])
        for level in book.get("asks", [])
        if isinstance(level, dict) and level.get("price") is not None
    ]
    if not bids or not asks:
        return
    best_bid = max(bids)
    best_ask = min(asks)
    if not (0 < best_bid < best_ask < 1):
        return
    market.best_bid = best_bid
    market.best_ask = best_ask
    market.midpoint = round((best_bid + best_ask) / 2, 6)
    market.spread = round(best_ask - best_bid, 6)


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


def _passes_static_paired_entry_guard(
    scored: ScoredMarket, settings: config.LiveTradingSettings
) -> bool:
    """Pre-filter obviously unactionable flat-entry candidates.

    The live L2 book remains authoritative and MarketMaker repeats the guard
    before every order. This list-data check only prevents the WebSocket pool
    from spending an entire refresh interval tracking markets that its own
    configured paired-entry payoff rule is guaranteed to reject. Missing BBO
    metadata fails open here because the downstream live-book check fails
    closed before trading.
    """
    if not settings.require_both_entry_legs:
        return True
    market = scored.market
    if market is None or market.best_bid is None or market.best_ask is None:
        return True
    captured_spread_cents = _captured_spread_cents(market)
    if captured_spread_cents <= 0:
        return False
    tick_size = _get_tick_size(market)
    bid_price = market.best_bid + tick_size
    ask_price = market.best_ask - tick_size
    bid_ratio = bid_price * 100 / captured_spread_cents
    ask_ratio = (1 - ask_price) * 100 / captured_spread_cents
    cap = settings.max_payoff_loss_to_capture_ratio
    return bid_ratio <= cap and ask_ratio <= cap


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


def is_observation_eligible(
    scored: ScoredMarket, settings: config.LiveTradingSettings,
) -> bool:
    """Broad, non-trading universe used only to collect evidence.

    It deliberately ignores liquidity, spread, recommendation, and paired-
    entry filters.  Those decide whether to place an order *now*; applying
    them before observation was the architectural mistake that left most of
    the 100-market WebSocket capacity unused and made activity unknowable.
    """
    market = scored.market
    return is_observation_market_eligible(market, settings)


def is_observation_market_eligible(
    market: Optional[Market], settings: config.LiveTradingSettings,
) -> bool:
    """Market-only form shared with the bounded L2 fallback prioritizer."""
    if market is None or not market.token_ids:
        return False
    if not settings.allow_non_binary_markets and not _is_verified_binary_yes_no(market):
        return False
    if is_excluded_market_family(market.market_id, market.raw, settings):
        return False
    if _market_type(market) in settings.exclude_market_types:
        return False
    hours = hours_to_event_or_close(market)
    # Observation measures the broad legacy opportunity set.  It must not
    # inherit LIVE_MAX_STARTED_EVENT_HOURS (commonly 0), live spread limits,
    # pregame reduce-only rules, or live event-allocation caps.
    return (
        -settings.observation_legacy_max_started_event_hours
        <= hours
        <= settings.max_days_to_close * 24
    )


# Kept for downstream extensions that imported the former private names.
_is_observation_eligible = is_observation_eligible
_is_observation_market_eligible = is_observation_market_eligible


def is_eligible(scored: ScoredMarket, settings: config.LiveTradingSettings) -> bool:
    market = scored.market
    if market is None or not market.token_ids:
        return False

    if not settings.allow_non_binary_markets and not _is_verified_binary_yes_no(market):
        return False

    l2_recovered = bool((market.raw or {}).get(_L2_RECOVERED_MARKER))
    if not l2_recovered:
        try:
            min_tier_index = _RECOMMENDATION_TIERS.index(settings.min_score_recommendation)
            scored_tier_index = _RECOMMENDATION_TIERS.index(scored.recommendation)
        except ValueError:
            return False
        if scored_tier_index < min_tier_index:
            return False

    if is_excluded_market_family(market.market_id, market.raw, settings):
        return False

    hours_to_event = hours_to_event_or_close(market)
    if hours_to_event > settings.max_days_to_close * 24:
        return False
    if hours_to_event < -settings.max_started_event_hours:
        return False

    market_type = _market_type(market)
    if market_type in settings.exclude_market_types:
        return False

    return True


def is_excluded_market_family(
    market_slug: str, raw: Optional[dict], settings: config.LiveTradingSettings
) -> bool:
    """True if this market's category, question text, slug prefix, or
    sports-market-type matches a configured exclusion -- e.g. intraday
    weather-threshold markets (category "climate", question containing
    "temperature", slug prefix "tc-temp") or esports markets
    (sportsMarketType "esports_match_winner"), both cases where the edge
    depends on external truth the bot doesn't model. Reusable from both
    is_eligible() (full candidacy exclusion) and
    multi_market_maker.py::_event_and_toxicity_gating (a reduce-only trigger
    for an orphaned position already in an excluded market).

    Works with just a slug (the slug-prefix check needs no raw data); the
    category/question/sports-market-type checks fail open (not excluded)
    when raw is None/empty, same direction as the event-started check's
    fail-open."""
    slug_lower = (market_slug or "").lower()
    if any(slug_lower.startswith(prefix.lower()) for prefix in settings.exclude_slug_prefixes):
        return True

    raw = raw or {}
    category = str(raw.get("category") or "").lower()
    if category in settings.exclude_categories:
        return True

    # Checked directly against raw["sportsMarketType"], NOT via
    # _market_type()'s marketType-first fallback chain -- for esports
    # records raw["marketType"] is always truthy ("drawable_outcome"/
    # "moneyline", shared with many non-esports markets), so _market_type()
    # never falls through to sportsMarketType at all.
    sports_market_type = str(raw.get("sportsMarketType") or "").lower()
    if sports_market_type in settings.exclude_sports_market_types:
        return True

    question = str(raw.get("question") or raw.get("title") or "").lower()
    if any(keyword in question for keyword in settings.exclude_question_keywords):
        return True

    return False


def _incentive_priority(scored: ScoredMarket, settings: config.LiveTradingSettings) -> int:
    category = ((scored.market.category if scored.market else None) or "").lower()
    return int(bool(category and category in settings.incentive_categories))


def _ranking_primary(
    scored: ScoredMarket,
    settings: config.LiveTradingSettings,
    activity_scores: Optional[dict[str, float]] = None,
) -> float:
    if settings.rank_by_expected_value:
        return _expected_value_score(scored, settings, activity_scores)
    market = scored.market
    if market is None:
        return 0.0
    return market.spread or 0.0


def _expected_value_score(
    scored: ScoredMarket,
    settings: config.LiveTradingSettings,
    activity_scores: Optional[dict[str, float]] = None,
) -> float:
    market = scored.market
    if market is None:
        return 0.0
    return _captured_spread_cents(market) * _activity_confidence(scored, settings, activity_scores)


def _activity_confidence(
    scored: ScoredMarket,
    settings: config.LiveTradingSettings,
    activity_scores: Optional[dict[str, float]],
) -> float:
    """Prefers a real, WS-observed activity signal (recent trades, growth
    in sharesTraded, book-update frequency -- see
    ws_market_data.py::StreamingMarketDataStore's rolling-window tracking,
    blended into activity_scores by ws_runner.py::_refresh_candidates)
    over the static volume/liquidity proxy below whenever one is available
    for this market -- a real observed signal strictly beats a proxy
    that's often zero right now due to the ongoing upstream volume/
    liquidity field outage (see live/RUNBOOK.md's most recent section).
    Only ever available for a market already being watched over the
    WebSocket for at least one cycle -- activity_scores is None/empty for
    the REST-only runner.py path and for a market's first-ever selection,
    both of which fall back to _fill_confidence exactly as before this."""
    market = scored.market
    if activity_scores and market is not None and market.market_id in activity_scores:
        return max(0.0, min(1.0, activity_scores[market.market_id]))
    return _fill_confidence(scored, settings)


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


def event_or_close_datetime(market) -> Optional[datetime]:
    """The actual parsed event/close datetime, or None if it can't be
    determined at all (no usable gameStartTime/endDate field, or an
    unparseable value) -- unlike hours_to_event_or_close()/
    _days_to_event_or_close(), which both fold that "unknown" case into a
    misleading 0.0 (indistinguishable from a market that genuinely starts
    right now). Not underscore-prefixed -- multi_market_maker.py reuses this
    directly for the imminent/unknown-timing reduce-only check (see
    live/RUNBOOK.md's most recent section)."""
    if _market_type(market) == "futures":
        return _resolve_datetime_from_raw_or_end_date(market, ("endDate",))
    return _resolve_datetime_from_raw_or_end_date(market, ("gameStartTime", "endDate"))


def _resolve_datetime_from_raw_or_end_date(
    market, raw_keys: tuple[str, ...]
) -> Optional[datetime]:
    for key in raw_keys:
        raw = getattr(market, "raw", {}) or {}
        value = raw.get(key)
        if value:
            return _parse_iso(value)
    end_date = getattr(market, "end_date", None)
    if not end_date:
        return None
    return _parse_iso(end_date)


def _days_from_raw_or_end_date(market, raw_keys: tuple[str, ...]) -> float:
    parsed = _resolve_datetime_from_raw_or_end_date(market, raw_keys)
    if parsed is None:
        return 0.0
    return (parsed - datetime.now(timezone.utc)).total_seconds() / 86400


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _days_from_iso(value: str) -> float:
    parsed = _parse_iso(value)
    if parsed is None:
        return 0.0
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
