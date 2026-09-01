"""Coordinates live quote attempts across multiple markets per refresh cycle."""

from __future__ import annotations

import dataclasses
import time
from datetime import datetime, timezone
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
from .fills import (
    build_fill_record,
    get_all_fills,
    get_backfill_resolved_order_ids,
    parse_transact_time,
    record_backfill_resolved,
    record_fill,
)
from .ledger import get_known_order_details, get_known_order_ids, strategy_total_pnl_usd
from .lot_accounting import compute_lots
from .market_maker import EmergencySafeguardFailedError, MarketMaker
from .market_selection import (
    event_or_close_datetime,
    hours_to_event_or_close,
    is_excluded_market_family,
    select_target_markets,
)
from .models import LiveQuoteCycle
from .settlements import (
    detect_settlements,
    get_all_position_snapshots,
    get_all_settlements,
    get_detected_settlement_keys,
    get_earliest_snapshot_by_slug,
    record_position_snapshots,
    record_settlements,
)
from .strategy_lifecycle import ControlledLifecycle, controlled_lifecycle
from .toxicity_tracker import ToxicityTracker
from .us_client import LiveUsClient, UsApiError
from .volatility_filter import VolatilityTracker
from .ws_private import PrivateStateStore

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
        private_store: Optional[PrivateStateStore] = None,
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
        self.private_store = private_store
        self._last_private_state_reconcile = 0.0
        self._state_degraded_since: Optional[float] = None
        self._degraded_cancel_sent = False
        # A newly submitted order can fill before the private position
        # subscription reflects its effect. Until the deadline expires and
        # both orders/positions are refreshed from REST, this market is not
        # allowed another action. This prevents a repeated reducing order
        # from crossing flat and reversing the position.
        self._settlement_not_before: dict[str, float] = {}
        self._markets_with_fills: set[str] = set()
        self._completed_round_trip_markets: set[str] = set()
        self._completed_round_trip_counts: dict[str, int] = {}
        # Position age used for live liquidation must not depend solely on
        # FIFO P&L attribution. The US API labels a real flat-to-short fill
        # ORDER_INTENT_SELL_LONG; lot accounting conservatively treats that
        # intent as a possible reduction of pre-tracking inventory. During
        # the 2026-07-13/14 live run that left every new short without a
        # tracked opening lot, so the unknown-age fallback immediately
        # force-flattened six fresh positions as taker orders. Observe
        # authoritative account transitions separately: a position that was
        # flat in a prior trustworthy snapshot and is non-flat now has a
        # known in-session opening time regardless of API intent labels.
        self._last_known_net_positions: Optional[dict[str, float]] = None
        self._observed_position_opened_at: dict[str, float] = {}
        # Sticky market allocation: a flat (no held position) candidate that
        # wins the shared order budget and successfully posts/keeps >=1 leg
        # stays preferred over fresh ranking for subsequent cycles, instead
        # of the budget being re-contested from scratch every refresh cycle.
        # A confirmed real run (2026-07-20) alternated the winning markets
        # on nearly every ~10s cycle -- 20 cycles, 62 posted legs, 0 fills,
        # orders resting only 10-20s each. See live/RUNBOOK.md's most recent
        # section. Maps market_id -> time.monotonic() the pin was first
        # established (NOT last renewed) -- see _update_sticky_pins -- so
        # settings.sticky_market_hold_seconds bounds total pin lifetime, not
        # time-since-last-renewal, forcing periodic re-contestability even
        # for a market that would keep winning forever on raw merit.
        self._pinned_markets: dict[str, float] = {}

    def note_fills(self, market_slugs: set[str]) -> None:
        """Require immediate REST reconciliation after observed executions."""
        now = time.monotonic()
        for slug in market_slugs:
            if not slug:
                continue
            self._markets_with_fills.add(slug)
            # The next refresh must reconcile now, even if a normal
            # post-submission delay had not elapsed yet.
            self._settlement_not_before[slug] = min(
                self._settlement_not_before.get(slug, now), now,
            )

    def refresh_quotes(
        self, candidates=None, settings_override=None, extra_raw_by_slug: Optional[dict[str, dict]] = None,
        breaker_risk: bool = False, drain_only: bool = False,
        force_flatten_all: bool = False,
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
        see the raw_by_slug construction below.

        `breaker_risk`, if True (computed once per cycle by ws_runner.py --
        see WebSocketLiveTradingBot._compute_breaker_risk), means the daily
        or session circuit breaker is approaching its loss limit -- one of
        three reduce-only exit urgency triggers alongside per-market
        over_cap/near_resolution (see reduce_urgent below)."""
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

        account_state = self._get_account_state()
        if account_state is None:
            return []
        open_orders, positions, positions_known = account_state
        open_orders, positions, positions_known, settling_slugs = (
            self._reconcile_settlement_barriers(open_orders, positions, positions_known)
        )
        if positions_known:
            self._observe_position_transitions(positions)

        # P&L attribution ("Strategy B" -- see live/settlements.py): best-
        # effort, only when positions are actually known (never snapshot a
        # known-failed fetch as if it were real state). Both independently
        # kill-switched; neither is allowed to interrupt this refresh cycle.
        if positions_known and self.settings.position_snapshot_enabled:
            record_position_snapshots(positions)
        if positions_known and self.settings.settlement_detection_enabled:
            self._detect_and_record_settlements(positions)

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
        # Extended (this cycle: also carries "size" per leg) to resolve each
        # still-open resting order's side/price/size for the event-exposure
        # projection below, without ever needing to parse the exchange's own
        # open-order response schema (never verified beyond id/marketSlug
        # anywhere in this codebase). See live/event_exposure.py.
        order_details = get_known_order_details()

        # Runs here, before either placement loop below has posted anything
        # this cycle, so scope_slugs (candidates + orphaned positions, both
        # already known at this point) can never contain a just-placed
        # order id -- unlike the old end-of-cycle call site, there is
        # structurally nothing to exclude. See live/RUNBOOK.md's most
        # recent section.
        self._backfill_missed_executions(candidate_slugs | set(orphaned_slugs), open_orders)

        if self.settings.one_round_trip_per_market_per_session:
            for slug in self._markets_with_fills:
                if slug not in settling_slugs and _net_position_for(positions, slug) == 0:
                    self._completed_round_trip_markets.add(slug)

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
            total_pnl = strategy_total_pnl_usd(positions)
            if total_pnl is None:
                logger.error(
                    "Could not establish strategy P/L for capital-reference risk; "
                    "skipping quote cycle."
                )
                return []
            capital_reference_usd = resolve_capital_reference_usd(
                self.equity_protection_settings.starting_capital_usd, total_pnl, positions,
            )
            exposure_by_bucket = {
                e.bucket_key: e
                for e in compute_event_exposures(
                    positions, capital_reference_usd, raw_by_slug,
                    open_orders=open_orders, order_details=order_details,
                )
            }

        # Running per-cycle tally of USD committed to each event bucket,
        # seeded from the start-of-cycle projection (settled + resting
        # worst-case) and incremented as each market's increasing leg
        # actually posts below -- used ONLY to size DOWN a new increasing
        # order to fit remaining headroom (_event_cap_remaining_usd), kept
        # separate from the static-snapshot over_cap binary block in
        # _event_and_toxicity_gating. Closes the real gap where two sibling
        # markets in the same event bucket, same cycle, could each post a
        # full-size order with neither aware of the other's contribution --
        # the exact mechanism of the 2026-07-09 Argentina-Switzerland
        # overshoot. See live/RUNBOOK.md's most recent section.
        provisional_committed_usd: dict[str, float] = {
            bucket_key: exposure.cost_basis_usd + exposure.resting_worst_case_usd
            for bucket_key, exposure in exposure_by_bucket.items()
        }

        # Release any sticky pin (see __init__/self._pinned_markets) that's
        # become unsafe BEFORE this cycle's placement loops run, and cancel
        # its resting order right now -- not after this cycle's replacement
        # market has already posted its own new order. A pin that merely
        # expired its hold window is released without a proactive cancel
        # (see _release_pins_before_cycle's docstring for why).
        unsafe_released = self._release_pins_before_cycle(raw_by_slug, exposure_by_bucket)
        if unsafe_released:
            logger.info(
                "Releasing unsafe sticky pin(s) before this cycle's placement "
                "loop -- cancelling their resting orders now: %s",
                sorted(unsafe_released),
            )
            self._cancel_orders_on_slugs(
                open_orders, unsafe_released, known_order_ids, context="pin-released-unsafe",
            )

        position_age_hours = self._position_age_hours_by_slug()

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
        #
        # Within that, a position whose force_flatten deadline is already
        # due must itself outrank every other orphaned position -- it's a
        # mandatory-liquidation task, not a routine re-quote, and the
        # arbitrary iteration order the position API/private-store returns
        # slugs in has no relationship to urgency. Without this, a
        # force_flatten position could be starved of a turn entirely by
        # other, non-urgent orphaned positions ahead of it in that order,
        # reproducing the exact "inventory trapped through settlement"
        # failure force_flatten exists to close -- a real, since-fixed bug.
        # See live/RUNBOOK.md's most recent section.
        orphan_status = {
            slug: self._force_flatten_status(slug, raw_by_slug.get(slug), positions, position_age_hours)
            for slug in orphaned_slugs
        }
        orphaned_slugs = sorted(orphaned_slugs, key=lambda slug: orphan_status[slug][0], reverse=True)
        for slug in orphaned_slugs:
            if slug in settling_slugs:
                logger.info(
                    "Settlement barrier active for held market %s; preserving its "
                    "orders and waiting for authoritative REST state.",
                    slug,
                )
                managed_slugs.add(slug)
                continue
            if placed_orders >= order_budget:
                logger.warning(
                    "Order budget exhausted before managing orphaned position "
                    "on %s -- it carries over unmanaged to the next cycle.", slug,
                )
                break
            remaining = order_budget - placed_orders
            orphan_raw = raw_by_slug.get(slug)
            in_cooldown, reduce_only_reason, event_warn, over_cap = self._event_and_toxicity_gating(
                slug, orphan_raw, exposure_by_bucket,
            )
            near_resolution = self._is_near_resolution(orphan_raw)
            force_flatten, age_hours, max_holding_reached = orphan_status[slug]
            if drain_only:
                reduce_only_reason = _join_reason(
                    reduce_only_reason, "pilot passive drain",
                )
            if force_flatten_all:
                force_flatten = True
            if max_holding_reached:
                reduce_only_reason = _join_reason(reduce_only_reason, "maximum holding time")
            if force_flatten:
                reduce_only_reason = _join_reason(reduce_only_reason, "hard flatten deadline")
            reduce_urgent = over_cap or near_resolution or breaker_risk or max_holding_reached or force_flatten
            event_cap_remaining_usd = self._event_cap_remaining_usd(
                slug, orphan_raw, capital_reference_usd, provisional_committed_usd,
            )
            increasing_order_shares = self._increasing_order_shares_for(
                effective_settings, in_cooldown, event_warn,
            )
            orphan_market_view = SimpleNamespace(raw=orphan_raw or {})
            maker = MarketMaker(
                client=self.client,
                market_slug=slug,
                tick_size=_get_tick_size(orphan_market_view),
                min_trade_qty=_get_min_trade_qty(orphan_market_view),
                settings=self._effective_settings_for(effective_settings, in_cooldown, event_warn, near_resolution),
                read_client=self.read_client,
                volatility_tracker=self.volatility_tracker,
                reduce_only_reason=reduce_only_reason,
                reduce_urgent=reduce_urgent,
                in_cooldown=in_cooldown,
                event_cap_remaining_usd=event_cap_remaining_usd,
                increasing_order_shares=increasing_order_shares,
                position_snapshot=positions.get(slug),
                position_snapshot_known=positions_known,
                position_age_hours=age_hours,
                force_flatten=force_flatten,
                bot_order_ids=known_order_ids,
            )
            cycle, ran_ok = self._run_one_market(maker, remaining, open_orders)
            if ran_ok:
                managed_slugs.add(slug)
            self._record_provisional_exposure(
                slug, orphan_raw, _net_position_for(positions, slug), cycle, provisional_committed_usd,
            )
            if cycle is None:
                continue
            cycles.append(cycle)
            placed_orders += _count_placed_orders(cycle)

        # A held position that remains in the ranked candidate set is still
        # real inventory and must outrank every flat opportunity. Previously
        # only *orphaned* positions got first priority; an in-candidate held
        # market could be starved when earlier flat candidates consumed the
        # order budget. Stable sorting preserves rank within each group.
        #
        # Within that, a force_flatten-due candidate outranks every other
        # held candidate too, for the same reason it outranks other orphaned
        # positions above -- a mandatory-liquidation task must not lose the
        # shared budget race to a merely-held, non-urgent position just
        # because of where select_target_markets' own EV ranking happened
        # to place it.
        candidate_status = {
            scored.market.market_id: self._force_flatten_status(
                scored.market.market_id, scored.market.raw, positions, position_age_hours,
            )
            for scored in candidates if scored.market is not None
        }
        prioritized_candidates = self._apply_sticky_priority(candidates, positions, candidate_status)
        for scored in prioritized_candidates:
            if placed_orders >= order_budget:
                break
            if scored.market is None:
                continue

            remaining = order_budget - placed_orders
            market = scored.market
            round_trip_limit = self._round_trip_limit()
            completed_round_trips = self._completed_round_trip_counts.get(
                market.market_id, 0,
            )
            legacy_limit_reached = (
                self.settings.one_round_trip_per_market_per_session
                and market.market_id in self._completed_round_trip_markets
            )
            if (
                (legacy_limit_reached or (
                    round_trip_limit > 0
                    and completed_round_trips >= round_trip_limit
                ))
                and _net_position_for(positions, market.market_id) == 0
            ):
                logger.info(
                    "Skipping %s for the rest of this session -- its position "
                    "already completed %d round trip(s).",
                    market.market_id, max(1, completed_round_trips),
                )
                self._cancel_orders_on_slugs(
                    open_orders,
                    {market.market_id},
                    known_order_ids,
                    context="completed-round-trip",
                )
                managed_slugs.add(market.market_id)
                continue
            if market.market_id in settling_slugs:
                logger.info(
                    "Settlement barrier active for candidate %s; skipping it until "
                    "authoritative REST order/position reconciliation completes.",
                    market.market_id,
                )
                managed_slugs.add(market.market_id)
                continue
            in_cooldown, reduce_only_reason, event_warn, over_cap = self._event_and_toxicity_gating(
                market.market_id, market.raw, exposure_by_bucket,
            )
            near_resolution = self._is_near_resolution(market.raw)
            force_flatten, age_hours, max_holding_reached = candidate_status[market.market_id]
            if max_holding_reached:
                reduce_only_reason = _join_reason(reduce_only_reason, "maximum holding time")
            if force_flatten:
                reduce_only_reason = _join_reason(reduce_only_reason, "hard flatten deadline")
            reduce_urgent = over_cap or near_resolution or breaker_risk or max_holding_reached or force_flatten
            event_cap_remaining_usd = self._event_cap_remaining_usd(
                market.market_id, market.raw, capital_reference_usd, provisional_committed_usd,
            )
            increasing_order_shares = self._increasing_order_shares_for(
                effective_settings, in_cooldown, event_warn,
            )
            maker = MarketMaker(
                client=self.client,
                market_slug=market.market_id,
                tick_size=_get_tick_size(market),
                min_trade_qty=_get_min_trade_qty(market),
                settings=self._effective_settings_for(effective_settings, in_cooldown, event_warn, near_resolution),
                read_client=self.read_client,
                volatility_tracker=self.volatility_tracker,
                reduce_only_reason=reduce_only_reason,
                reduce_urgent=reduce_urgent,
                in_cooldown=in_cooldown,
                event_cap_remaining_usd=event_cap_remaining_usd,
                increasing_order_shares=increasing_order_shares,
                position_snapshot=positions.get(market.market_id),
                position_snapshot_known=positions_known,
                position_age_hours=age_hours,
                force_flatten=force_flatten,
                bot_order_ids=known_order_ids,
            )
            cycle, ran_ok = self._run_one_market(maker, remaining, open_orders)
            if ran_ok:
                managed_slugs.add(market.market_id)
            self._record_provisional_exposure(
                market.market_id, market.raw, _net_position_for(positions, market.market_id),
                cycle, provisional_committed_usd,
            )
            if cycle is None:
                continue
            cycles.append(cycle)
            placed_orders += _count_placed_orders(cycle)

        self._update_sticky_pins(cycles, candidate_slugs)

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
            "Multi-market refresh complete: candidates=%d orphaned=%d "
            "quoted_markets=%d placed_order_legs=%d/%d",
            len(candidates), len(orphaned_slugs), len(cycles), placed_orders, order_budget,
        )
        return cycles

    def _release_pins_before_cycle(
        self, raw_by_slug: dict[str, dict], exposure_by_bucket: dict[str, EventExposure],
    ) -> set[str]:
        """Called once per cycle, before either placement loop runs. Two
        distinct release paths, deliberately handled differently:

        - Hold window expired (settings.sticky_market_hold_seconds since the
          pin was first established -- see __init__): dropped from
          self._pinned_markets with NO proactive cancel. It re-enters this
          cycle's ranked walk as an ordinary candidate -- if it's still the
          best available market it wins again normally and
          market_maker.py::_keep_post_or_skip keeps its resting order
          completely untouched (the common case, since nothing displaced
          it). If something else now outranks it, it loses and is cleaned
          up by the existing post-hoc unmanaged-candidate sweep -- the same
          brief, self-correcting overshoot window that already exists today
          for any ordinary lost-budget-race candidate, just now also
          covering this one low-frequency case. Proactively cancelling on
          every interval boundary regardless would force a needless
          cancel-then-repost-at-the-same-price on a market that was about
          to keep winning anyway.
        - Unsafe (over_cap from the existing _event_and_toxicity_gating):
          dropped from self._pinned_markets AND returned,
          so the caller can cancel its resting order before this cycle's
          replacement market posts its own new order -- a market becoming
          unsafe should never even briefly overlap with its replacement.
          Deliberately narrow: toxicity cooldown / account-wide
          breaker_risk are excluded because MarketMaker's own
          in_cooldown/reduce_only gating already handles those safely at
          the per-market level without needing the pin revoked.

        A pinned market that simply posts 0 legs this cycle for its own
        reasons (insufficient edge, thin book, etc.) needs no handling
        here at all -- every 0-leg early-return path inside
        market_maker.py::refresh_quotes already cancels its own stale
        orders before returning. The pin itself is deliberately NOT
        evicted on a single bad cycle either (see _update_sticky_pins) --
        immediate eviction would recreate the exact churn this feature
        exists to fix."""
        if not self.settings.sticky_market_allocation_enabled:
            return set()
        now = time.monotonic()
        unsafe: set[str] = set()
        for slug in list(self._pinned_markets):
            if now - self._pinned_markets[slug] >= self.settings.sticky_market_hold_seconds:
                del self._pinned_markets[slug]
                continue
            raw = raw_by_slug.get(slug)
            _, _, _, over_cap = self._event_and_toxicity_gating(slug, raw, exposure_by_bucket)
            # Near-resolution is already handled by wider entry edge and
            # urgent reduction inside MarketMaker.  Releasing the pin every
            # cycle solely because a market was inside the broad 24-hour
            # caution window caused cancel/re-pin churn while flat.  Only a
            # real exposure-cap violation makes the pin itself unsafe.
            if over_cap:
                del self._pinned_markets[slug]
                unsafe.add(slug)
        return unsafe

    def _apply_sticky_priority(
        self,
        candidates: list,
        positions: dict[str, dict],
        candidate_status: dict[str, tuple],
    ) -> list:
        """Extends the EXISTING (force_flatten, held) 2-tuple priority sort
        (see refresh_quotes) with a third, strictly LOWER-priority tier:
        currently-pinned sticky-allocation slugs (self._pinned_markets --
        see __init__, _release_pins_before_cycle, _update_sticky_pins). A
        held position or a force_flatten-due market still always outranks a
        pinned-but-flat market, exactly as before this tier existed -- pins
        only ever break ties among flat, non-urgent candidates. Stable sort
        preserves candidates' original EV-ranked order within each tier,
        same guarantee the existing two-tier sort already relied on."""
        return sorted(
            candidates,
            key=lambda scored: (
                candidate_status[scored.market.market_id][0] if scored.market is not None else False,
                bool(
                    scored.market is not None
                    and _net_position_for(positions, scored.market.market_id) != 0
                ),
                bool(
                    scored.market is not None
                    and self.settings.sticky_market_allocation_enabled
                    and scored.market.market_id in self._pinned_markets
                ),
            ),
            reverse=True,
        )

    def _update_sticky_pins(self, cycles: list[LiveQuoteCycle], candidate_slugs: set[str]) -> None:
        """Called once, at the end of a cycle's candidate placement loop.
        pinned_since is set ONLY when a slug newly enters the pinned dict
        (first win, or a re-win after a prior release) -- it is NOT reset
        on a cycle where an already-pinned slug simply keeps winning, so
        settings.sticky_market_hold_seconds bounds the total pin lifetime,
        not the time since last renewal. This is what forces periodic
        re-contestability even for a market that would keep winning
        forever on raw merit.

        Restricting to candidate_slugs specifically (not just any cycle in
        `cycles`) is what keeps this a no-op for the orphaned-position
        loop's cycles: orphaned_slugs and candidate_slugs are always
        disjoint (_find_orphaned_position_slugs skips anything already in
        candidate_slugs), so an orphan's cycle can never match here.

        Also prunes any pinned slug that fell out of ranked candidacy
        entirely this cycle -- it can never win a turn again to naturally
        trigger release, and its resting orders are already handled
        unconditionally by the existing end-of-cycle stale-order sweep."""
        if not self.settings.sticky_market_allocation_enabled:
            self._pinned_markets.clear()
            return
        now = time.monotonic()
        won_this_cycle = {
            cycle.market_id for cycle in cycles
            if cycle.market_id in candidate_slugs and _count_placed_orders(cycle) > 0
        }
        for slug in won_this_cycle:
            if slug not in self._pinned_markets:
                self._pinned_markets[slug] = now
        for slug in list(self._pinned_markets):
            if slug not in candidate_slugs:
                del self._pinned_markets[slug]

    def _backfill_missed_executions(self, scope_slugs: set[str], open_orders: list[dict]) -> None:
        """A private-WS gap can silently and permanently drop a fill --
        reconnection only reconciles positions/open orders, never
        executions (see live/RUNBOOK.md's most recent section). Scoped to
        markets in scope_slugs (bounded -- not a blind crawl over this
        bot's entire order history; the caller passes this cycle's
        candidate + orphaned-position slugs): for any order the ledger
        recorded posting on one of those markets that is neither currently
        open nor has a recorded fill, ask the exchange directly
        (GET /v1/order/{orderId} -- the only execution-history-adjacent
        endpoint this platform has) whether it actually filled. Best-effort,
        never allowed to interrupt the refresh cycle.

        Called by refresh_quotes() BEFORE either placement loop runs, so no
        order placed this cycle can exist in `open_orders` (fetched at the
        start of this refresh) yet -- unlike an end-of-cycle call site,
        there's structurally nothing to exclude here.

        Bounded to settings.execution_backfill_max_lookups_per_cycle
        candidates per cycle (extras are simply left for a later cycle --
        anything not yet resolved still qualifies as a candidate then), and
        paced settings.execution_backfill_min_interval_seconds apart so this
        mechanism's own GET /v1/order/{orderId} calls don't crowd out the
        same cycle's order placement/cancellation/reconciliation calls
        against the shared account-wide rate limit (docs.polymarket.us)."""
        if not scope_slugs:
            return
        try:
            order_details = get_known_order_details()
            fill_order_ids = {f.get("order_id") for f in get_all_fills() if f.get("order_id")}
            resolved_order_ids = get_backfill_resolved_order_ids()
        except Exception as exc:  # noqa: BLE001 -- diagnostics must not stop the refresh cycle
            logger.warning("Could not prepare execution-backfill candidates: %s", exc)
            return

        terminal_no_fill_ids: set[str] = set()
        terminal_ids_getter = getattr(self.private_store, "terminal_no_fill_order_ids", None)
        if callable(terminal_ids_getter):
            observed = terminal_ids_getter()
            if isinstance(observed, (set, list, tuple)):
                terminal_no_fill_ids = {str(order_id) for order_id in observed}
        newly_resolved = [
            order_id for order_id in terminal_no_fill_ids
            if order_id in order_details
            and order_details[order_id].get("market_id") in scope_slugs
            and order_id not in fill_order_ids
            and order_id not in resolved_order_ids
        ]
        for order_id in newly_resolved:
            record_backfill_resolved(order_id, "private_ws_no_fill")
            resolved_order_ids.add(order_id)
        if newly_resolved:
            logger.info(
                "Resolved %d closed zero-fill order(s) from private WebSocket state; "
                "skipping unnecessary REST backfill lookups.",
                len(newly_resolved),
            )

        open_order_ids = {
            str(order_id) for o in open_orders
            if (order_id := o.get("id") or o.get("orderId"))
        }

        candidates = [
            order_id
            for order_id, details in order_details.items()
            if details.get("market_id") in scope_slugs
            and order_id not in open_order_ids
            and order_id not in fill_order_ids
            and order_id not in resolved_order_ids
        ]

        min_interval = self.settings.execution_backfill_min_interval_seconds
        max_lookups = self.settings.execution_backfill_max_lookups_per_cycle
        for i, order_id in enumerate(candidates[:max_lookups]):
            if i > 0 and min_interval > 0:
                time.sleep(min_interval)
            self._backfill_one_order(order_id, order_details)

    def _backfill_one_order(self, order_id: str, order_details: dict[str, dict]) -> None:
        try:
            order = self.client.get_order(order_id)
        except UsApiError as exc:
            logger.warning("Could not check order %s for a missed execution: %s", order_id, exc)
            return  # not persisted as resolved -- retried next cycle

        if order is None:
            logger.warning(
                "Order %s has no recorded fill and isn't currently open, but the "
                "exchange also has no record of it -- treating as resolved (nothing "
                "to backfill).", order_id,
            )
            record_backfill_resolved(order_id, "not_found")
            return

        state = order.get("state")
        cum_quantity = _to_float(order.get("cumQuantity"))
        if cum_quantity and cum_quantity > 0:
            # Nonzero cumQuantity means shares filled regardless of which
            # terminal state the order ended in -- an order that filled
            # PARTIALLY before its remainder was cancelled (state
            # ORDER_STATE_CANCELED, plausible for this bot's own IOC orders,
            # which auto-cancel any unfilled remainder) still needs its
            # fill backfilled, not just one that reached ORDER_STATE_FILLED.
            try:
                fill = build_fill_record(_synthetic_execution_from_order(order_id, order), order_details, None)
                record_fill(fill)
                logger.warning(
                    "Backfilled a fill for order %s that the private WebSocket never "
                    "delivered (state=%s, cumQuantity=%s, avgPx=%s).",
                    order_id, state, cum_quantity, order.get("avgPx"),
                )
            except Exception as exc:  # noqa: BLE001 -- diagnostics must not stop the refresh cycle
                logger.error("Could not persist backfilled fill for order %s: %s", order_id, exc)
                return  # not persisted as resolved -- retried next cycle
            # Not separately recorded as backfill-resolved: the fill's own
            # order_id in fills.json already excludes it from future
            # candidacy (fill_order_ids above), so this state needs no
            # duplicate tracking.
        else:
            logger.info("Order %s resolved with nothing to backfill (state=%s).", order_id, state)
            record_backfill_resolved(order_id, "no_fill")

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

        Deliberately does NOT shrink order_shares_min/order_shares_max here
        for in_cooldown/event_exposure_warn -- that would shrink both BUY
        and SELL uniformly, including whichever side turns out to be the
        reducing/exit leg once MarketMaker resolves net_position, which is
        backwards: a market exiting a toxic or over-concentrated position
        should be able to do so at normal size. See _increasing_order_shares_
        for, called separately by both call sites and passed into
        MarketMaker's own increasing_order_shares parameter, which only ever
        discounts whichever side is increasing.

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
            )
        if event_exposure_warn:
            settings = dataclasses.replace(
                settings,
                min_edge_cents=settings.min_edge_cents * self.settings.event_exposure_warn_edge_multiplier,
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

    def _increasing_order_shares_for(
        self,
        base_settings: config.LiveTradingSettings,
        in_cooldown: bool,
        event_exposure_warn: bool,
    ) -> Optional[float]:
        """The increasing-side-only order-shares figure toxicity/event-warn
        should discount -- never the reducing/exit side (see
        _effective_settings_for's docstring). None means no override, so the
        caller (MarketMaker) uses its own unshrunk order_shares_min/max
        average for both sides, exactly as when neither condition is active.
        Multipliers compose the same way _effective_settings_for's
        min_edge_cents widening does, and read from base_settings so a
        prior settings_override (e.g. equity-protection's profit-lock
        sizing) is still respected."""
        if not in_cooldown and not event_exposure_warn:
            return None
        shares = (base_settings.order_shares_min + base_settings.order_shares_max) / 2
        if in_cooldown:
            shares *= self.settings.toxicity_size_multiplier
        if event_exposure_warn:
            shares *= self.settings.event_exposure_warn_size_multiplier
        return shares

    def _is_event_started(self, raw: Optional[dict]) -> bool:
        if not raw:
            return False  # fail-open: cannot evaluate without raw data
        if self.settings.pilot_mode:
            lifecycle = self._controlled_lifecycle(raw)
            return bool(lifecycle and lifecycle.inplay_deadline_due)
        view = SimpleNamespace(raw=raw, end_date=raw.get("endDate"))
        return hours_to_event_or_close(view) < -self.settings.max_started_event_hours

    def _controlled_lifecycle(
        self, raw: Optional[dict],
    ) -> Optional[ControlledLifecycle]:
        if not raw:
            return None
        view = SimpleNamespace(raw=raw, end_date=raw.get("endDate"))
        event_dt = event_or_close_datetime(view)
        if event_dt is None:
            return None
        return controlled_lifecycle(
            event_start_epoch=event_dt.timestamp(),
            now_epoch=datetime.now(timezone.utc).timestamp(),
            pregame_pause_minutes=self.settings.pre_event_reduce_only_minutes,
            max_started_event_hours=self.settings.max_started_event_hours,
            entry_cutoff_minutes=(
                self.settings.observation_controlled_entry_cutoff_minutes
            ),
        )

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

    def _is_timing_unclear_or_imminent(self, raw: Optional[dict]) -> bool:
        """Reduce-only when timing is unknown or a known start is imminent.

        The former same-UTC-day rule blocked every new order in the day's
        sports slate, even many hours before the games.  A bounded pre-event
        buffer preserves the informed-flow protection without disabling
        game-day market-making altogether.  Missing/unparseable timing still
        FAILS CLOSED: without a trustworthy start time, exposure may only be
        reduced.
        """
        if not raw:
            return True
        view = SimpleNamespace(raw=raw, end_date=raw.get("endDate"))
        event_dt = event_or_close_datetime(view)
        if event_dt is None:
            return True
        minutes_remaining = (
            event_dt.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds() / 60.0
        if self.settings.pilot_mode:
            # Controlled pilot explicitly reopens at kickoff.
            lifecycle = self._controlled_lifecycle(raw)
            return True if lifecycle is None else lifecycle.pregame_paused
        return minutes_remaining <= max(
            0.0, self.settings.pre_event_reduce_only_minutes
        )

    def _pilot_entry_cutoff_due(self, raw: Optional[dict]) -> bool:
        if not self.settings.pilot_mode:
            return False
        lifecycle = self._controlled_lifecycle(raw)
        return True if lifecycle is None else lifecycle.entry_cutoff_due

    def _hard_flatten_due(self, raw: Optional[dict]) -> bool:
        """True once inventory may no longer be carried safely.

        Unknown timing fails closed: a position with no trustworthy event
        time cannot be allowed to drift toward an unseen settlement. Known
        markets become mandatory-liquidation tasks at the configured
        deadline and remain so after the event starts.
        """
        if self.settings.pilot_mode:
            lifecycle = self._controlled_lifecycle(raw)
            return True if lifecycle is None else lifecycle.inplay_deadline_due
        deadline_minutes = self.settings.hard_flatten_minutes_before_event
        if deadline_minutes <= 0:
            return False
        if not raw:
            return True
        view = SimpleNamespace(raw=raw, end_date=raw.get("endDate"))
        event_dt = event_or_close_datetime(view)
        if event_dt is None:
            return True
        minutes_remaining = (
            event_dt.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds() / 60.0
        return minutes_remaining <= deadline_minutes

    def _get_account_state(self) -> Optional[tuple[list[dict], dict[str, dict], bool]]:
        """Return trustworthy order/position snapshots or fail closed.

        With private state enabled, REST bootstraps and periodically
        reconciles full snapshots. A healthy private WebSocket makes its
        deltas authoritative between those reconciliations. If neither a
        healthy stream nor a successful REST read can vouch for state, no
        new quotes are placed; a prolonged outage triggers one cancel-all.
        """
        store = self.private_store
        if not self.settings.private_order_state_enabled or store is None:
            try:
                open_orders = self.client.get_open_orders()
            except UsApiError as exc:
                logger.error("Could not fetch open orders -- skipping to avoid trading blind: %s", exc)
                return None
            try:
                positions = self.client.get_all_positions()
                if not _positions_are_complete(positions):
                    logger.error("Positions response is malformed; refusing partial risk state.")
                    return None
                return open_orders, positions, True
            except UsApiError as exc:
                logger.error(
                    "Could not fetch complete positions; skipping to avoid opening "
                    "against hidden inventory: %s", exc,
                )
                return None

        now = time.monotonic()
        ws_healthy = store.is_healthy(self.settings.private_state_stale_after_seconds)
        reconcile_due = (
            self._last_private_state_reconcile == 0.0
            or now - self._last_private_state_reconcile
            >= self.settings.private_state_reconcile_seconds
            or not ws_healthy
            # A reconnect that resolves faster than this loop's own cadence
            # can leave ws_healthy continuously True without ever being
            # observed False -- forcing a REST cross-check here is the only
            # way to confirm the reconnect didn't miss anything. See
            # PrivateStateStore.mark_connected()'s docstring.
            or store.reconnect_pending()
        )
        orders_rest_ok = False
        positions_rest_ok = False
        if reconcile_due:
            order_version = store.order_version()
            try:
                orders = self.client.get_open_orders()
                if not store.reconcile_open_orders(orders, order_version):
                    logger.info("Preserved newer private order delta over racing REST snapshot.")
                orders_rest_ok = True
            except UsApiError as exc:
                logger.warning("Open-order reconciliation failed: %s", exc)
            position_version = store.position_version()
            try:
                positions_result = self.client.get_all_positions()
                if not store.reconcile_positions(positions_result, position_version):
                    logger.info("Preserved newer private position delta over racing REST snapshot.")
                positions_rest_ok = True
            except UsApiError as exc:
                logger.warning("Position reconciliation failed: %s", exc)
            if orders_rest_ok and positions_rest_ok:
                # Only a fresh, successful fetch of BOTH actually confirms
                # the reconnect didn't miss anything -- clearing this on a
                # failed/partial attempt would let a real gap go unconfirmed
                # until the next periodic reconciliation, up to
                # private_state_reconcile_seconds later.
                store.clear_reconnect_pending()
            # Once both caches have been bootstrapped, a failed periodic
            # reconciliation must not turn into a REST retry storm every
            # quote cycle. The healthy WS remains authoritative until the
            # next normal interval. Before bootstrap, leave the timestamp at
            # zero so the next cycle retries promptly while staying halted.
            if (
                orders_rest_ok and positions_rest_ok
                or store.open_orders_snapshot() is not None
                and store.positions_snapshot() is not None
            ):
                self._last_private_state_reconcile = now

        open_orders = store.open_orders_snapshot()
        positions = store.positions_snapshot()
        trustworthy = (
            open_orders is not None
            and positions is not None
            and (ws_healthy or (orders_rest_ok and positions_rest_ok))
            and _positions_are_complete(positions)
        )
        if not trustworthy:
            self._handle_degraded_private_state(now)
            logger.error(
                "Private account state is not trustworthy -- skipping quote cycle "
                "(ws_healthy=%s orders_initialized=%s positions_initialized=%s).",
                ws_healthy, open_orders is not None, positions is not None,
            )
            return None

        self._state_degraded_since = None
        self._degraded_cancel_sent = False
        return open_orders, positions, True

    def _handle_degraded_private_state(self, now: float) -> None:
        if self._state_degraded_since is None:
            self._state_degraded_since = now
            return
        timeout = self.settings.private_state_degraded_cancel_seconds
        if self._degraded_cancel_sent or now - self._state_degraded_since < timeout:
            return
        try:
            snapshot = self.private_store.open_orders_snapshot() if self.private_store else None
            self.client.cancel_all(open_orders=snapshot)
            self._degraded_cancel_sent = True
            logger.error(
                "Private account state remained degraded for %.1fs; sent cancel-all safeguard.",
                now - self._state_degraded_since,
            )
        except Exception as exc:  # noqa: BLE001 -- remain fail-closed and retry next cycle
            logger.error("Cancel-all safeguard failed while private state was degraded: %s", exc)

    def _position_age_hours_by_slug(self) -> dict[str, float]:
        try:
            lots = compute_lots(
                get_all_fills(), get_all_settlements(), get_earliest_snapshot_by_slug(),
            ).open_lots
        except Exception as exc:  # noqa: BLE001 -- unknown age means no age-based relaxation
            logger.warning("Could not compute open-position ages: %s", exc)
            return {}
        now = datetime.now(timezone.utc)
        ages: dict[str, float] = {}
        for lot in lots:
            if lot.presumed_settled:
                continue
            opened = parse_transact_time(lot.open_time)
            if opened is None:
                continue
            age = max(0.0, (now - opened).total_seconds() / 3600.0)
            ages[lot.market_slug] = max(age, ages.get(lot.market_slug, 0.0))
        monotonic_now = time.monotonic()
        for slug, opened_at in self._observed_position_opened_at.items():
            observed_age = max(0.0, (monotonic_now - opened_at) / 3600.0)
            # When both sources know the opening, keep the older age. This
            # remains conservative and prevents an observed transition from
            # extending an accurately tracked lot's holding deadline.
            ages[slug] = max(observed_age, ages.get(slug, 0.0))
        return ages

    def _observe_position_transitions(self, positions: dict[str, dict]) -> None:
        """Track trustworthy in-session flat-to-position transitions.

        The first snapshot only establishes a baseline: an already-held
        position at process startup still has unknown age unless FIFO lot
        history can date it. Later flat->non-flat or sign-flip transitions
        are known new inventory and get a monotonic opening timestamp.
        Partial reductions/additions on the same side never reset the clock.
        """
        current = {
            slug: _net_position_for(positions, slug)
            for slug in positions
        }
        previous = self._last_known_net_positions
        if previous is None:
            self._last_known_net_positions = current
            return

        observed_at = time.monotonic()
        for slug in set(previous) | set(current):
            prior_net = previous.get(slug, 0.0)
            current_net = current.get(slug, 0.0)
            if current_net == 0:
                if prior_net != 0 and slug in self._markets_with_fills:
                    self._completed_round_trip_counts[slug] = (
                        self._completed_round_trip_counts.get(slug, 0) + 1
                    )
                    self._completed_round_trip_markets.add(slug)
                    self._markets_with_fills.discard(slug)
                self._observed_position_opened_at.pop(slug, None)
                continue
            if prior_net == 0 or (prior_net > 0) != (current_net > 0):
                self._observed_position_opened_at[slug] = observed_at

        self._last_known_net_positions = current

    def _round_trip_limit(self) -> int:
        if self.settings.pilot_mode:
            return max(0, self.settings.pilot_max_round_trips_per_market)
        return 1 if self.settings.one_round_trip_per_market_per_session else 0

    def _max_holding_reached(self, age_hours: Optional[float]) -> bool:
        limit = self.settings.liquidation_max_holding_hours
        return age_hours is not None and limit >= 0 and age_hours >= limit

    def _position_age_hours(
        self,
        slug: str,
        positions: dict[str, dict],
        known_ages: dict[str, float],
    ) -> Optional[float]:
        age = known_ages.get(slug)
        if age is not None:
            return age
        if _net_position_for(positions, slug) == 0:
            return None
        # A held position that predates fill tracking has unknown age. Treat
        # it as maximally old so the new liquidation policy cannot recreate
        # the old permanent cost-basis trap merely because history is absent.
        logger.warning(
            "Held position on %s has no tracked opening lot; treating it as "
            "maximum-age reduce-only inventory.",
            slug,
        )
        return max(0.0, self.settings.liquidation_max_holding_hours)

    def _force_flatten_status(
        self,
        slug: str,
        raw: Optional[dict],
        positions: dict[str, dict],
        known_ages: dict[str, float],
    ) -> tuple[bool, Optional[float], bool]:
        """(force_flatten, age_hours, max_holding_reached) -- the exact
        force_flatten derivation both the orphaned-position and ranked-
        candidate loops need, factored out so it can be computed ONCE per
        slug ahead of either loop (for budget-priority sorting -- see
        refresh_quotes) instead of twice (which would also double-log
        _position_age_hours' unknown-age warning)."""
        age_hours = self._position_age_hours(slug, positions, known_ages)
        max_holding_reached = self._max_holding_reached(age_hours)
        force_flatten = self._hard_flatten_due(raw) or (
            max_holding_reached and self.settings.hard_flatten_on_max_holding_enabled
        )
        return force_flatten, age_hours, max_holding_reached

    def _event_and_toxicity_gating(
        self,
        slug: str,
        raw: Optional[dict],
        exposure_by_bucket: dict[str, EventExposure],
    ) -> tuple[bool, Optional[str], bool, bool]:
        """Returns (in_cooldown, reduce_only_reason, event_exposure_warn,
        over_cap) for one market. The cap check is PER-BUCKET, not
        per-market: a brand-new candidate with zero position of its own, in
        a bucket already over-exposed via OTHER markets, still gets
        reduce_only_reason set -- and since a flat market treats both BUY
        and SELL as "increasing" (see market_maker.py), that correctly
        blocks it from opening at all. This is the actual mechanism that
        stops an Nth same-event slice from quoting.

        over_cap is also returned on its own (not just folded into
        reduce_only_reason) since it's one of the three reduce-only exit
        urgency triggers (see refresh_quotes' reduce_urgent computation)."""
        in_cooldown = self.settings.toxicity_tracking_enabled and self.toxicity_tracker.is_in_cooldown(slug)

        bucket_key = derive_event_bucket_key(slug, raw)
        exposure = exposure_by_bucket.get(bucket_key)
        over_cap = False
        near_warn = False
        if exposure is not None and exposure.projected_pct_of_capital is not None:
            cap_pct = (
                self.settings.stat_prop_max_event_exposure_pct
                if is_stat_prop_market(raw) else self.settings.max_event_exposure_pct
            )
            # projected_pct_of_capital (settled + still-open resting orders'
            # worst case), not the settled-only pct_of_capital -- this is
            # the start-of-cycle half of the exposure-overshoot fix; the
            # other half (same-cycle sibling markets) is
            # provisional_committed_usd/_event_cap_remaining_usd in
            # refresh_quotes, which additionally sizes down rather than
            # only binary-blocking. See live/RUNBOOK.md's most recent
            # section.
            over_cap = exposure.projected_pct_of_capital >= cap_pct
            near_warn = not over_cap and exposure.projected_pct_of_capital >= self.settings.warn_event_exposure_pct

        reasons = []
        if in_cooldown:
            reasons.append("toxicity cooldown")
        if over_cap:
            reasons.append("event exposure at or above cap")
        event_started = self._is_event_started(raw)
        if event_started:
            reasons.append("event already started")
        if is_excluded_market_family(slug, raw, self.settings):
            reasons.append("excluded market family")
        if not event_started and self._is_timing_unclear_or_imminent(raw):
            reasons.append("event start imminent or unknown timing")
        if not event_started and self._pilot_entry_cutoff_due(raw):
            reasons.append("pilot final in-play entry cutoff")
        reduce_only_reason = " + ".join(reasons) or None

        return in_cooldown, reduce_only_reason, near_warn, over_cap

    def _event_cap_remaining_usd(
        self,
        slug: str,
        raw: Optional[dict],
        capital_reference_usd: Optional[float],
        provisional_committed_usd: dict[str, float],
    ) -> Optional[float]:
        """USD headroom remaining in this market's event bucket before the
        (stat-prop-aware) cap is hit, given everything already committed
        this cycle -- including sibling markets in the same bucket already
        processed earlier in THIS cycle's loop (provisional_committed_usd
        is updated live by _record_provisional_exposure as each market
        posts). None means no active constraint (capital reference
        unavailable this cycle), matching this codebase's existing
        none-means-inactive convention (e.g. capital_reference_usd itself).
        Threaded into MarketMaker(event_cap_remaining_usd=...) to size the
        increasing leg DOWN to fit, rather than only a binary block once
        already at/over cap (see _event_and_toxicity_gating's over_cap)."""
        if capital_reference_usd is None:
            return None
        cap_pct = (
            self.settings.stat_prop_max_event_exposure_pct
            if is_stat_prop_market(raw) else self.settings.max_event_exposure_pct
        )
        bucket_key = derive_event_bucket_key(slug, raw)
        committed = provisional_committed_usd.get(bucket_key, 0.0)
        return max(0.0, cap_pct * capital_reference_usd - committed)

    def _record_provisional_exposure(
        self,
        slug: str,
        raw: Optional[dict],
        net_position: float,
        cycle: Optional[LiveQuoteCycle],
        provisional_committed_usd: dict[str, float],
    ) -> None:
        """After a market's cycle posts, computes THIS market's own
        worst-case committed USD (price*size for an increasing BUY,
        (1-price)*size for an increasing SELL) and adds it to the running
        per-cycle bucket tally, so the NEXT market in the same event
        bucket -- later in this same cycle's loop -- sees reduced headroom
        via _event_cap_remaining_usd (the exact gap behind the real
        2026-07-09 Argentina-Switzerland overshoot: sibling markets in one
        bucket, same cycle, neither aware of the other -- see
        live/RUNBOOK.md's most recent sections).

        At net_position == 0 (flat), BOTH bid and ask are increasing
        simultaneously, but a filled bid and filled ask on the SAME token
        can't both be simultaneously realized risk -- if both fill, they
        net to a captured spread, not additive exposure. This market's OWN
        contribution to the bucket tally is therefore the MAX of its two
        increasing legs' committed USD, not their sum (reverting an
        earlier, since-reconsidered fix that summed them -- see
        live/RUNBOOK.md's most recent section for why, and the regression
        test replaying the real 2026-07-09 incident that gates this
        change). DIFFERENT markets in the same bucket remain fully
        additive below -- that cross-market case is the real incident this
        mechanism exists to prevent, and is untouched by this. No-op per
        leg if it wasn't actually posted (order_id is None --
        reduce-only-blocked, no edge, budget exhausted, etc.) -- nothing
        was actually committed in that case."""
        if cycle is None:
            return
        bid_increasing = net_position >= 0
        ask_increasing = net_position <= 0
        bid_committed_usd = (
            cycle.bid.price * cycle.bid.size
            if bid_increasing and cycle.bid.order_id is not None else 0.0
        )
        ask_committed_usd = (
            (1.0 - cycle.ask.price) * cycle.ask.size
            if ask_increasing and cycle.ask.order_id is not None else 0.0
        )
        committed_usd = max(bid_committed_usd, ask_committed_usd)
        if committed_usd <= 0:
            return
        bucket_key = derive_event_bucket_key(slug, raw)
        provisional_committed_usd[bucket_key] = (
            provisional_committed_usd.get(bucket_key, 0.0) + committed_usd
        )

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
            existing_order_ids = {
                str(order_id)
                for order in open_orders
                if (order_id := order.get("id") or order.get("orderId"))
            }
            cycle = maker.refresh_quotes(max_orders=remaining, open_orders=open_orders)
            if self.private_store is not None and maker.last_cancelled_order_ids:
                self.private_store.remove_orders(maker.last_cancelled_order_ids)
            if cycle is not None and self.private_store is not None:
                self.private_store.upsert_local_orders([
                    {
                        "id": leg.order_id,
                        "marketSlug": cycle.market_id,
                        "side": f"ORDER_SIDE_{leg.side}",
                        "price": {"value": str(leg.price), "currency": "USD"},
                        "quantity": leg.size,
                        "state": "ORDER_STATE_NEW",
                    }
                    # is_resting, not bare order_id: an unwound leg's
                    # order_id is deliberately preserved for ledger/backfill
                    # purposes even though it was already removed above via
                    # last_cancelled_order_ids -- re-upserting it here as
                    # ORDER_STATE_NEW would immediately undo that removal.
                    for leg in (cycle.bid, cycle.ask) if leg.is_resting
                ])
            if cycle is not None and self.settings.order_settlement_seconds > 0:
                submitted_new_order = any(
                    leg.order_id and str(leg.order_id) not in existing_order_ids
                    for leg in (cycle.bid, cycle.ask)
                )
                if submitted_new_order:
                    self._settlement_not_before[cycle.market_id] = (
                        time.monotonic() + self.settings.order_settlement_seconds
                    )
            return cycle, True
        except EmergencySafeguardFailedError:
            # Deliberately NOT caught here like an ordinary refresh
            # failure -- this means the account-wide emergency cancel
            # safeguard itself couldn't be confirmed to have worked, which
            # must stop the whole process, not just skip this one market.
            # Left to propagate all the way to run_forever()'s own
            # top-level `except Exception: raise`. See
            # EmergencySafeguardFailedError's docstring.
            raise
        except Exception as exc:  # noqa: BLE001 -- try the next market
            logger.error("Refresh failed for candidate %s: %s", maker.market_slug, exc)
            return None, False

    def _reconcile_settlement_barriers(
        self,
        open_orders: list[dict],
        positions: dict[str, dict],
        positions_known: bool,
    ) -> tuple[list[dict], dict[str, dict], bool, set[str]]:
        """Refresh account state before a submitted market may act again.

        Order and position WebSocket messages are individually timely but
        not atomic. A terminal order update can therefore remove a filled
        order while positions still show the pre-fill quantity. REST is the
        authority at the barrier boundary; any failure extends the pause.
        """
        if not self._settlement_not_before:
            return open_orders, positions, positions_known, set()

        now = time.monotonic()
        expired = {
            slug for slug, deadline in self._settlement_not_before.items()
            if now >= deadline
        }
        if expired:
            try:
                rest_orders = self.client.get_open_orders()
                rest_positions = self.client.get_all_positions()
                if not isinstance(rest_orders, list) or not isinstance(rest_positions, dict):
                    raise UsApiError("REST settlement reconciliation returned invalid state")
            except UsApiError as exc:
                retry_at = now + max(1.0, self.settings.order_settlement_seconds)
                for slug in expired:
                    self._settlement_not_before[slug] = retry_at
                logger.error(
                    "Settlement-barrier REST reconciliation failed for %s; "
                    "extending the trading pause: %s",
                    sorted(expired), exc,
                )
            else:
                open_orders = rest_orders
                positions = rest_positions
                positions_known = True
                if self.private_store is not None:
                    self.private_store.seed_open_orders(rest_orders)
                    self.private_store.seed_positions(rest_positions)
                for slug in expired:
                    self._settlement_not_before.pop(slug, None)
                logger.info(
                    "Settlement barrier cleared after authoritative REST "
                    "reconciliation for %s.",
                    sorted(expired),
                )

        return (
            open_orders,
            positions,
            positions_known,
            set(self._settlement_not_before),
        )

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
                if self.private_store is not None:
                    self.private_store.remove_orders([str(order_id)])
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

    def _detect_and_record_settlements(self, positions: dict[str, dict]) -> None:
        """Best-effort P&L-attribution bookkeeping, never allowed to
        interrupt the refresh cycle: re-derives Strategy A's (live/
        lot_accounting.py) current open lots from every known fill, then
        checks whether any of them have vanished from the exchange's own
        position list (Strategy B -- live/settlements.py) since last seen.
        Newly detected settlement events are persisted; already-detected
        ones are skipped (see settlements.detect_settlements)."""
        try:
            fills = get_all_fills()
            existing_settlements = get_all_settlements()
            lot_result = compute_lots(fills, existing_settlements, get_earliest_snapshot_by_slug())
            new_settlements = detect_settlements(
                get_all_position_snapshots(), positions, lot_result.open_lots,
                get_detected_settlement_keys(),
            )
            if new_settlements:
                record_settlements(new_settlements)
                logger.info(
                    "Detected %d new settlement event(s): %s", len(new_settlements),
                    [(s["market_slug"], s["outcome"], s["confidence"]) for s in new_settlements],
                )
        except Exception as exc:  # noqa: BLE001 -- must never interrupt the refresh cycle
            logger.error("Could not run settlement detection this cycle: %s", exc)


def _to_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _synthetic_execution_from_order(order_id: str, order: dict) -> dict:
    """Reshapes GET /v1/order/{orderId}'s response (the bare order object)
    into the execution-envelope shape fills.py::build_fill_record()
    expects (mirroring the private-WS execution schema: an "order"
    sub-dict plus execution-level lastShares/lastPx/transactTime/
    commission fields). Necessarily an AGGREGATE reconstruction, not a
    genuine execution: this endpoint only exposes cumulative totals
    (cumQuantity/avgPx across the order's whole fill history), never
    per-execution granularity, so an order filled across multiple actual
    trades collapses into ONE synthetic fill representing the aggregate --
    a real loss of granularity versus what the WS stream would have
    delivered, but strictly better than losing the fill (and its P&L)
    entirely."""
    return {
        "id": f"backfill-{order_id}",
        "tradeId": f"backfill-{order_id}",
        "order": order,
        "lastShares": order.get("cumQuantity"),
        "lastPx": order.get("avgPx"),
        "type": "EXECUTION_TYPE_FILL",
        "transactTime": order.get("lastTransactTime") or order.get("insertTime") or order.get("createTime"),
        "commissionNotionalCollected": order.get("commissionNotionalTotalCollected"),
    }


def _count_placed_orders(cycle: LiveQuoteCycle) -> int:
    # is_resting, not bare order_id truthiness: an unwound leg's order_id
    # is deliberately preserved for ledger/backfill purposes even though
    # nothing is actually resting -- see PostedLeg.is_resting.
    return int(cycle.bid.is_resting) + int(cycle.ask.is_resting)


def _join_reason(existing: Optional[str], reason: str) -> str:
    return f"{existing} + {reason}" if existing else reason


def _net_position_for(positions: dict, slug: str) -> float:
    position = positions.get(slug) or {}
    try:
        return float(position.get("netPositionDecimal", 0))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _positions_are_complete(positions: Optional[dict[str, dict]]) -> bool:
    if positions is None:
        return False
    for position in positions.values():
        if not isinstance(position, dict):
            return False
        try:
            net = float(position["netPositionDecimal"])
        except (KeyError, TypeError, ValueError):
            return False
        if net == 0:
            continue
        try:
            float((position.get("cost") or {})["value"])
            float((position.get("cashValue") or {})["value"])
        except (KeyError, TypeError, ValueError):
            return False
    return True


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
