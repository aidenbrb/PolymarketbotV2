"""Coordinates live quote attempts across multiple markets per refresh cycle."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Optional

from .. import config
from ..logger import get_logger
from ..models import Market
from ..polymarket_client import PolymarketClient
from .event_exposure import (
    EventExposure,
    compute_event_exposures,
    derive_event_bucket_key,
    is_stat_prop_market,
    resolve_capital_reference_usd,
)
from .ledger import get_known_order_ids, sum_position_pnl
from .market_maker import MarketMaker
from .market_selection import hours_to_event_or_close, select_target_markets
from .models import LiveQuoteCycle
from .toxicity_tracker import ToxicityTracker
from .us_client import LiveUsClient, UsApiError
from .volatility_filter import VolatilityTracker

logger = get_logger("live.multi_market_maker")


class MultiMarketMaker:
    def __init__(
        self,
        client: LiveUsClient,
        settings: Optional[config.LiveTradingSettings] = None,
        read_client: Optional[PolymarketClient] = None,
        candidate_provider=None,
        toxicity_tracker: Optional[ToxicityTracker] = None,
        equity_protection_settings: Optional[config.EquityProtectionSettings] = None,
    ):
        self.client = client
        self.settings = settings or config.load_settings().live
        self.read_client = read_client or PolymarketClient()
        self.candidate_provider = candidate_provider
        # Only used to read starting_capital_usd for the event-exposure
        # capital-reference fallback chain (see resolve_capital_reference_usd)
        # -- the same "account value" figure live/equity_protection.py
        # already established, reused here rather than duplicated.
        self.equity_protection_settings = (
            equity_protection_settings or config.load_settings().equity_protection
        )
        # One shared tracker across cycles -- MultiMarketMaker itself lives
        # for the whole bot run, but it constructs a fresh MarketMaker per
        # market every cycle, so the rolling window has to live here instead.
        self.volatility_tracker = VolatilityTracker(self.settings.volatility_window_seconds)
        # Same "one shared instance across cycles" reasoning as
        # volatility_tracker -- fed by ws_runner.py's _compute_due_markouts,
        # consulted here per-market to widen/shrink/reduce-only a toxic
        # market's quoting. See live/toxicity_tracker.py.
        self.toxicity_tracker = toxicity_tracker or ToxicityTracker(
            alpha=self.settings.toxicity_ewma_alpha,
            adverse_threshold_cents=self.settings.toxicity_adverse_threshold_cents,
            cooldown_seconds=self.settings.toxicity_cooldown_seconds,
        )

    def refresh_quotes(
        self, candidates=None, settings_override=None, extra_raw_by_slug: Optional[dict[str, dict]] = None,
    ) -> list[LiveQuoteCycle]:
        """`settings_override`, if provided, is used instead of self.settings
        ONLY when constructing each MarketMaker below -- i.e. it can only
        affect per-market quoting (order share size, in practice, per
        live/equity_protection.py's profit-lock sizing), never the order
        budget or candidate selection, both of which always read
        self.settings directly.

        `extra_raw_by_slug`, if provided, is a broader slug->raw map (e.g.
        ws_runner.py's full pre-eligibility-filter scan) used to look up raw
        market data for held positions that fell out of ranked candidacy --
        see the raw_by_slug construction below."""
        effective_settings = settings_override or self.settings
        order_budget = max(0, self.settings.max_orders_per_cycle)
        if order_budget <= 0:
            logger.info(
                "Live max orders per cycle is 0; no new orders will be posted, "
                "but bot-owned stale resting orders will still be cleaned up."
            )

        if candidates is None:
            if self.candidate_provider:
                candidates = self.candidate_provider()
            else:
                internal_raw: dict[str, dict] = {}
                candidates = select_target_markets(settings=self.settings, raw_by_slug_out=internal_raw)
                extra_raw_by_slug = {**internal_raw, **(extra_raw_by_slug or {})}

        # Fetched ONCE per cycle and handed to every market -- this endpoint
        # returns the whole account's open orders regardless of market, so
        # calling it again per-candidate was pure repeated work and was
        # tripping the real account's rate limit on cycles with many
        # candidates. If it can't be fetched at all, skip the whole cycle
        # rather than post new orders without knowing what's already resting.
        try:
            open_orders = self.client.get_open_orders()
        except UsApiError as exc:
            logger.error("Could not fetch open orders this cycle -- skipping to avoid posting blind: %s", exc)
            return []

        # Also fetched ONCE per cycle (unconditionally -- NOT short-circuited
        # by the order-budget check above) and shared for orphaned-position
        # detection, event-exposure computation, AND the capital-reference
        # P/L figure below -- previously _find_orphaned_position_slugs did
        # its own separate fetch; this avoids a second one every cycle.
        try:
            positions = self.client.get_all_positions()
            positions_known = True
        except UsApiError as exc:
            logger.warning(
                "Could not fetch positions this cycle (treating as unknown, "
                "not zero): %s", exc,
            )
            positions = {}
            positions_known = False

        cycles: list[LiveQuoteCycle] = []
        placed_orders = 0
        managed_slugs: set[str] = set()

        candidate_slugs = {s.market.market_id for s in candidates if s.market is not None}
        # Unknown (not []) means "couldn't confirm the full position list" --
        # that must be treated as unknown, not as zero orphans, or a stale
        # sweep below could cancel a protective order on a position we
        # simply failed to discover.
        orphaned_slugs = self._find_orphaned_position_slugs(positions, candidate_slugs) if positions_known else []

        known_order_ids = get_known_order_ids()

        # Event-level exposure (see live/event_exposure.py) plus the
        # event-started/near-resolution checks below: raw_by_slug is seeded
        # from extra_raw_by_slug (a broader pre-eligibility-filter scan --
        # see ws_runner.py::_refresh_candidates -- covering orphaned
        # positions too, best-effort) and then overlaid with this cycle's
        # ranked candidates (always freshest, wins on overlap). A position
        # whose market has been fully delisted still falls through to the
        # slug-heuristic bucket key / fail-open checks, same as
        # reporting-only callers (main.py::cmd_live_event_exposure).
        raw_by_slug = dict(extra_raw_by_slug or {})
        raw_by_slug.update({s.market.market_id: s.market.raw for s in candidates if s.market is not None})
        capital_reference_usd = None
        exposure_by_bucket: dict[str, EventExposure] = {}
        if positions_known:
            total_pnl = sum_position_pnl(positions)
            capital_reference_usd = resolve_capital_reference_usd(
                self.equity_protection_settings.starting_capital_usd, total_pnl, positions,
            )
            exposure_by_bucket = {
                e.bucket_key: e
                for e in compute_event_exposures(positions, capital_reference_usd, raw_by_slug)
            }

        # Held positions on markets that fell out of this cycle's ranked
        # candidates (e.g. spread narrowed, a better opportunity took their
        # slot) would otherwise never be revisited -- no cancel, no re-price,
        # no cost-basis protection -- until the whole bot is stopped. These
        # go FIRST, ahead of ranked candidates: real money already at stake
        # in an existing position must outrank opening a brand-new
        # speculative quote for the shared order budget, or a held position
        # can get starved out indefinitely whenever there are enough ranked
        # candidates to fill the budget on their own (this happened for
        # real on 2026-07-05 -- see live/RUNBOOK.md).
        for slug in orphaned_slugs:
            if placed_orders >= order_budget:
                logger.warning(
                    "Order budget exhausted before managing orphaned position "
                    "on %s -- it carries over unmanaged to the next cycle.", slug,
                )
                break
            remaining = order_budget - placed_orders
            in_cooldown, reduce_only_reason, event_warn = self._event_and_toxicity_gating(
                slug, raw_by_slug.get(slug), exposure_by_bucket,
            )
            near_resolution = self._is_near_resolution(raw_by_slug.get(slug))
            maker = MarketMaker(
                client=self.client,
                market_slug=slug,
                tick_size=0.01,
                min_trade_qty=1.0,
                settings=self._effective_settings_for(effective_settings, in_cooldown, event_warn, near_resolution),
                read_client=self.read_client,
                volatility_tracker=self.volatility_tracker,
                reduce_only_reason=reduce_only_reason,
            )
            cycle, ran_ok = self._run_one_market(maker, remaining, open_orders)
            if ran_ok:
                managed_slugs.add(slug)
            if cycle is None:
                continue
            cycles.append(cycle)
            placed_orders += _count_placed_orders(cycle)

        for scored in candidates:
            if placed_orders >= order_budget:
                break
            if scored.market is None:
                continue

            remaining = order_budget - placed_orders
            market = scored.market
            in_cooldown, reduce_only_reason, event_warn = self._event_and_toxicity_gating(
                market.market_id, market.raw, exposure_by_bucket,
            )
            near_resolution = self._is_near_resolution(market.raw)
            maker = MarketMaker(
                client=self.client,
                market_slug=market.market_id,
                tick_size=_get_tick_size(market),
                min_trade_qty=_get_min_trade_qty(market),
                settings=self._effective_settings_for(effective_settings, in_cooldown, event_warn, near_resolution),
                read_client=self.read_client,
                volatility_tracker=self.volatility_tracker,
                reduce_only_reason=reduce_only_reason,
            )
            cycle, ran_ok = self._run_one_market(maker, remaining, open_orders)
            if ran_ok:
                managed_slugs.add(market.market_id)
            if cycle is None:
                continue
            cycles.append(cycle)
            placed_orders += _count_placed_orders(cycle)

        # Candidates that never got a MarketMaker turn this cycle because the
        # order budget ran out first -- their resting orders (if any) were
        # NOT refreshed, so they're just as stale as a market that fell out
        # of selection entirely. Safe regardless of whether positions could
        # be fetched: this only depends on the candidate list and what
        # actually ran, not on position data.
        unmanaged_candidate_slugs = candidate_slugs - managed_slugs
        if unmanaged_candidate_slugs:
            logger.warning(
                "Order budget exhausted before these candidates got a turn "
                "this cycle -- their resting orders were not refreshed: %s",
                sorted(unmanaged_candidate_slugs),
            )
            self._cancel_orders_on_slugs(
                open_orders, unmanaged_candidate_slugs, known_order_ids, context="unmanaged-candidate",
            )

        # Markets that are neither a ranked candidate nor a held position at
        # all. Only safe to sweep when we actually know the full position
        # list -- if get_all_positions() failed, a market could be an
        # undiscovered held position, and cancelling its resting order would
        # remove the one thing protecting it (cost-basis floor on the next
        # successful cycle aside, an order cancelled now is just gone).
        if positions_known:
            keep_slugs = candidate_slugs | set(orphaned_slugs)
            open_slugs = {(o.get("marketSlug") or o.get("market_slug")) for o in open_orders}
            stale_slugs = open_slugs - keep_slugs
            stale_slugs.discard(None)
            if stale_slugs:
                logger.warning(
                    "Cancelling stale resting order(s) on market(s) no "
                    "longer selected: %s", sorted(stale_slugs),
                )
                self._cancel_orders_on_slugs(open_orders, stale_slugs, known_order_ids, context="stale")
        else:
            logger.warning(
                "Skipping the stale-order sweep for non-candidate markets "
                "this cycle -- could not confirm the full set of held "
                "positions, so a resting order on an undiscovered position "
                "could be mistaken for stale."
            )

        logger.info(
            "Multi-market refresh complete: attempted=%d orphaned=%d cycles=%d placed_orders=%d/%d",
            len(candidates), len(orphaned_slugs), len(cycles), placed_orders, order_budget,
        )
        return cycles

    def _effective_settings_for(
        self,
        base_settings: config.LiveTradingSettings,
        in_cooldown: bool,
        event_exposure_warn: bool = False,
        near_resolution: bool = False,
    ) -> config.LiveTradingSettings:
        """base_settings unchanged unless in_cooldown and/or
        event_exposure_warn and/or near_resolution -- each independently
        widens min_edge_cents ON TOP OF whatever base_settings already is
        (composes with an existing settings_override, e.g.
        equity-protection's profit-lock sizing, rather than overwriting it;
        if more than one applies, the multipliers stack). All multipliers
        always come from self.settings (the same settings object that
        configured self.toxicity_tracker), never from
        base_settings/settings_override.

        Deliberately does NOT take an event-cap-BREACH (hard) signal here --
        a hard breach only ever sets reduce_only_reason (see
        _event_and_toxicity_gating), which already fully blocks the
        increasing side; additionally shrinking the REDUCING side's size
        here would be counterproductive, since a market actively shedding
        over-concentrated exposure should be able to do so at normal size."""
        settings = base_settings
        if in_cooldown:
            settings = dataclasses.replace(
                settings,
                min_edge_cents=settings.min_edge_cents * self.settings.toxicity_min_edge_multiplier,
                order_shares_min=settings.order_shares_min * self.settings.toxicity_size_multiplier,
                order_shares_max=settings.order_shares_max * self.settings.toxicity_size_multiplier,
            )
        if event_exposure_warn:
            settings = dataclasses.replace(
                settings,
                min_edge_cents=settings.min_edge_cents * self.settings.event_exposure_warn_edge_multiplier,
                order_shares_min=settings.order_shares_min * self.settings.event_exposure_warn_size_multiplier,
                order_shares_max=settings.order_shares_max * self.settings.event_exposure_warn_size_multiplier,
            )
        if near_resolution:
            # Edge-only, deliberately no size multiplier -- unlike toxicity/
            # event-warn, this is a pure time-based caution, not evidence of
            # a specific bad market or over-concentration.
            settings = dataclasses.replace(
                settings,
                min_edge_cents=settings.min_edge_cents * self.settings.near_resolution_min_edge_multiplier,
            )
        return settings

    def _is_event_started(self, raw: Optional[dict]) -> bool:
        if not raw:
            return False  # fail-open: cannot evaluate without raw data
        view = SimpleNamespace(raw=raw, end_date=raw.get("endDate"))
        return hours_to_event_or_close(view) < -self.settings.max_started_event_hours

    def _is_near_resolution(self, raw: Optional[dict]) -> bool:
        if not raw:
            return False  # fail-open: cannot evaluate without raw data
        view = SimpleNamespace(raw=raw, end_date=raw.get("endDate"))
        hours_remaining = hours_to_event_or_close(view)
        # Bounded at >= 0 deliberately: once a market has already started
        # (negative hours), _is_event_started's reduce-only already applies,
        # and widening edge further here would also needlessly suppress the
        # REDUCING leg, which reduce_only_reason never touches.
        return 0 <= hours_remaining <= self.settings.near_resolution_hours_threshold

    def _event_and_toxicity_gating(
        self,
        slug: str,
        raw: Optional[dict],
        exposure_by_bucket: dict[str, EventExposure],
    ) -> tuple[bool, Optional[str], bool]:
        """Returns (in_cooldown, reduce_only_reason, event_exposure_warn) for
        one market. The cap check is PER-BUCKET, not per-market: a brand-new
        candidate with zero position of its own, in a bucket already
        over-exposed via OTHER markets, still gets reduce_only_reason set --
        and since a flat market treats both BUY and SELL as "increasing"
        (see market_maker.py), that correctly blocks it from opening at all.
        This is the actual mechanism that stops an Nth same-event slice from
        quoting."""
        in_cooldown = self.settings.toxicity_tracking_enabled and self.toxicity_tracker.is_in_cooldown(slug)

        bucket_key = derive_event_bucket_key(slug, raw)
        exposure = exposure_by_bucket.get(bucket_key)
        over_cap = False
        near_warn = False
        if exposure is not None and exposure.pct_of_capital is not None:
            cap_pct = (
                self.settings.stat_prop_max_event_exposure_pct
                if is_stat_prop_market(raw) else self.settings.max_event_exposure_pct
            )
            over_cap = exposure.pct_of_capital >= cap_pct
            near_warn = not over_cap and exposure.pct_of_capital >= self.settings.warn_event_exposure_pct

        reasons = []
        if in_cooldown:
            reasons.append("toxicity cooldown")
        if over_cap:
            reasons.append("event exposure at or above cap")
        if self._is_event_started(raw):
            reasons.append("event already started")
        reduce_only_reason = " + ".join(reasons) or None

        return in_cooldown, reduce_only_reason, near_warn

    def _run_one_market(
        self, maker: MarketMaker, remaining: int, open_orders: list[dict]
    ) -> tuple[Optional[LiveQuoteCycle], bool]:
        """Returns (cycle, ran_without_crashing). The second value is what
        callers must use to decide whether this slug got a genuine turn --
        a market whose own refresh_quotes() raised (e.g. before it could
        reach its internal cancel-before-post step) must NOT be marked
        "managed," or it becomes permanently invisible to the
        unmanaged-candidate cleanup sweep if the same error keeps recurring.
        `cycle is None` alone can't tell "crashed" apart from "ran fine and
        deliberately posted nothing" (a normal, frequent outcome)."""
        try:
            cycle = maker.refresh_quotes(max_orders=remaining, open_orders=open_orders)
            return cycle, True
        except Exception as exc:  # noqa: BLE001 -- try the next market
            logger.error("Refresh failed for candidate %s: %s", maker.market_slug, exc)
            return None, False

    def _cancel_orders_on_slugs(
        self, open_orders: list[dict], target_slugs: set, known_order_ids: set, context: str,
    ) -> None:
        """Cancels resting orders whose market is in target_slugs -- but
        ONLY if the order id is recognized as one this bot placed (found in
        the live ledger, see ledger.py::get_known_order_ids). An order this
        bot didn't create -- a manual trade, or a different strategy sharing
        the same account -- is left alone and logged instead of cancelled.
        A single cancel failure is logged and does not stop the rest."""
        matching = [
            o for o in open_orders
            if (o.get("marketSlug") or o.get("market_slug")) in target_slugs
        ]
        for order in matching:
            order_id = order.get("id") or order.get("orderId")
            slug = order.get("marketSlug") or order.get("market_slug")
            if not order_id or not slug:
                continue
            if order_id not in known_order_ids:
                logger.info(
                    "Leaving order %s on %s alone (%s sweep) -- not "
                    "recognized as an order this bot placed.",
                    order_id, slug, context,
                )
                continue
            try:
                self.client.cancel_order(order_id, slug)
                logger.warning("Cancelled %s order %s on %s.", context, order_id, slug)
            except UsApiError as exc:
                logger.warning("Failed to cancel %s order %s on %s: %s", context, order_id, slug, exc)

    def _find_orphaned_position_slugs(self, positions: dict, candidate_slugs: set) -> list[str]:
        """Returns held-position market slugs outside the ranked candidates.
        Pure now -- positions are fetched once by the caller (refresh_quotes)
        and shared; the caller is responsible for treating a failed fetch as
        unknown (not zero orphans) by not calling this at all in that case,
        same fail-closed posture as before."""
        orphaned = []
        for slug, position in positions.items():
            if slug in candidate_slugs:
                continue
            try:
                net_position = float(position.get("netPositionDecimal", 0))
            except (AttributeError, TypeError, ValueError):
                net_position = 0.0
            if net_position != 0:
                orphaned.append(slug)

        if orphaned:
            logger.warning(
                "Held position(s) outside this cycle's ranked candidates -- "
                "managing them anyway so they aren't abandoned: %s", orphaned,
            )
        return orphaned


def _count_placed_orders(cycle: LiveQuoteCycle) -> int:
    return int(bool(cycle.bid.order_id)) + int(bool(cycle.ask.order_id))


def _get_tick_size(market: Market, default: float = 0.01) -> float:
    try:
        return float(market.raw.get("orderPriceMinTickSize"))
    except (AttributeError, TypeError, ValueError):
        return default


def _get_min_trade_qty(market: Market, default: float = 1.0) -> float:
    try:
        return float(market.raw.get("minimumTradeQty") or default)
    except (AttributeError, TypeError, ValueError):
        return default
