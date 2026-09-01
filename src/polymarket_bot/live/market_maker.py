"""One live market-making refresh cycle: fetch the real order book, join/
improve the best bid and best ask by one tick each (see
live/pricing.py::compute_book_aware_quote), cancel this market's existing
resting orders, post the new pair, and record the cycle to the ledger.

If the real spread doesn't leave enough edge after improving both sides
(see LIVE_MIN_EDGE_CENTS), the cycle deliberately posts nothing -- forcing a
quote with no edge is how a market maker guarantees losing money, not making
it. Existing resting orders are still cancelled in that case so the bot
isn't left holding a stale quote.

Position-aware, on top of that: each cycle receives a reconciled position
snapshot (or fetches one in standalone use) and:
  - bounds voluntary loss on the REDUCING side using position age, urgency,
    a per-share allowance, and a total-dollar ceiling -- see
    live/pricing.py::apply_liquidation_limit.
  - stops adding to the position once its value hits LIVE_MAX_POSITION_USD.
This avoids both unbounded loss-taking and the old failure mode where an
absolute historical-cost floor could trap inventory until settlement.

NOTE (see live/RUNBOOK.md): this assumes a binary (two-outcome) market and
quotes using OUTCOME_SIDE_YES for both legs (buying at the bid, selling at
the ask). Whether OUTCOME_SIDE_YES/NO applies cleanly to non-binary markets
(e.g. sports team moneylines, which dominate Polymarket US's listings) is
UNVERIFIED -- the first live cycle should target a clearly binary Yes/No
market, not a sports moneyline, until this is confirmed. It's also
unverified whether a SELL leg can rest at all before the bot holds any
inventory -- watch for that in logs/bot.log.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from .. import config
from ..logger import get_logger
from ..models import utcnow_iso
from ..polymarket_client import PolymarketClient
from .fills import parse_transact_time
from .cancel_safeguard import EmergencySafeguardFailedError
from .ledger import record_cycle
from .models import LiveQuoteCycle, PostedLeg
from .pricing import (
    apply_liquidation_limit,
    book_has_enough_depth,
    compute_book_aware_quote,
    floor_to_quantity_step,
)
from .us_client import LiveUsClient, UsApiError, is_client_rejection
from .volatility_filter import VolatilityTracker

logger = get_logger("live.market_maker")


class UnsafeOrderStateError(RuntimeError):
    """A placement may exist on-exchange but cannot be tracked safely."""


class MarketMaker:
    def __init__(
        self,
        client: LiveUsClient,
        market_slug: str,
        tick_size: float,
        min_trade_qty: float = 1.0,
        settings: Optional[config.LiveTradingSettings] = None,
        read_client: Optional[PolymarketClient] = None,
        volatility_tracker: Optional[VolatilityTracker] = None,
        reduce_only_reason: Optional[str] = None,
        reduce_urgent: bool = False,
        in_cooldown: bool = False,
        event_cap_remaining_usd: Optional[float] = None,
        increasing_order_shares: Optional[float] = None,
        position_snapshot: Optional[dict] = None,
        position_snapshot_known: bool = False,
        position_age_hours: Optional[float] = None,
        force_flatten: bool = False,
        bot_order_ids: Optional[set[str]] = None,
    ):
        self.client = client
        self.market_slug = market_slug
        self.tick_size = tick_size
        self.min_trade_qty = min_trade_qty
        self.settings = settings or config.load_settings().live
        self.read_client = read_client or PolymarketClient()
        # Defaults to a private, never-shared tracker if the caller doesn't
        # pass one -- fine for standalone/test use, but a real multi-cycle
        # bot (see MultiMarketMaker) must pass the SAME instance across
        # cycles for the rolling window to mean anything, since a fresh
        # MarketMaker is constructed every cycle.
        self.volatility_tracker = volatility_tracker or VolatilityTracker(
            self.settings.volatility_window_seconds
        )
        # Set by MultiMarketMaker when this market is either in a
        # live/toxicity_tracker.py adverse-markout cooldown OR its
        # live/event_exposure.py correlated-event bucket is at/above its
        # exposure cap -- blocks the increasing side entirely (see
        # _resolve_leg_price), regardless of the position cap. None means
        # not reduce-only; any non-empty string IS the reason and is used
        # verbatim in the skip message (multiple simultaneous reasons are
        # joined with " + " by the caller). At net_position == 0 (flat),
        # BOTH BUY and SELL count as "increasing" (nothing to reduce from
        # flat), so this blocks both legs and the market simply stops being
        # quoted until the reason clears -- deliberate: "sit out with no
        # position." See live/RUNBOOK.md's "-9."/most recent event-exposure
        # sections.
        self.reduce_only_reason = reduce_only_reason
        # Set by MultiMarketMaker alongside reduce_only_reason: reduce_urgent
        # is True when this market is over its event-exposure cap, near
        # resolution, or the daily/session circuit breaker is approaching its
        # loss limit -- an urgent exit still gets the aggressive inventory-skew
        # nudge. When not urgent, in_cooldown (this market's toxicity
        # cooldown state) is used as a stability gate: not urgent AND in
        # cooldown skips reducing entirely this cycle instead of exiting into
        # what may be a fast adverse move. See live/RUNBOOK.md's most recent
        # section.
        self.reduce_urgent = reduce_urgent
        self.in_cooldown = in_cooldown
        # Set by MultiMarketMaker: USD headroom remaining in this market's
        # event-exposure bucket before its cap is hit, given everything
        # already committed this cycle (including sibling markets in the
        # same bucket processed earlier in the same cycle -- see
        # MultiMarketMaker._event_cap_remaining_usd). None means no active
        # constraint. Unlike reduce_only_reason (a binary block once
        # already at/over cap), this SIZES DOWN the increasing leg to fit
        # remaining headroom -- the protection for a market comfortably
        # under cap today that could otherwise post a full-size order that
        # alone jumps its bucket over. See live/RUNBOOK.md's most recent
        # section.
        self.event_cap_remaining_usd = event_cap_remaining_usd
        # Set by MultiMarketMaker: the toxicity-cooldown/event-exposure-warn
        # discounted order-shares figure, applied ONLY to whichever leg turns
        # out to be increasing once net_position is known (see base_order_
        # shares below). None means no discount, i.e. both legs use the
        # ordinary order_shares_min/max average, unchanged. A shared,
        # side-blind shrink here previously landed on the REDUCING/exit leg
        # instead (reduce_only_reason already fully blocks the increasing
        # leg during the same cooldown), which is backwards -- see
        # live/RUNBOOK.md's most recent section.
        self.increasing_order_shares = increasing_order_shares
        self.position_snapshot = position_snapshot
        self.position_snapshot_known = position_snapshot_known
        self.position_age_hours = position_age_hours
        # Mandatory inventory exit state, set by MultiMarketMaker at the
        # pre-event deadline (or when event timing is unknowable). This path
        # uses a marketable limit and bypasses entry-only quote filters and
        # voluntary cost-basis loss bounds.
        self.force_flatten = force_flatten
        self.bot_order_ids = {str(order_id) for order_id in (bot_order_ids or set())}
        self.last_cancelled_order_ids: list[str] = []

    def refresh_quotes(
        self, max_orders: Optional[int] = None, open_orders: Optional[list[dict]] = None
    ) -> Optional[LiveQuoteCycle]:
        """`open_orders`, if provided, is used instead of calling
        client.get_open_orders() again -- that endpoint returns the whole
        account's orders, not just this market's, so a caller managing many
        markets in one cycle (see MultiMarketMaker) should fetch it once and
        pass it to every MarketMaker rather than each one re-fetching the
        identical account-wide list (this was hitting real rate limits)."""
        self.last_cancelled_order_ids = []
        if max_orders is not None and max_orders <= 0:
            logger.info("Skipping %s this cycle -- live order budget is exhausted.", self.market_slug)
            return None

        book = self._get_book()
        # The exchange book can include this account's own resting quotes.
        # Treating our 1-2 share improved quote as external top-of-book made
        # the next cycle (a) fail the 10-share top-depth guard or (b) improve
        # our own price by another tick.  Both paths caused needless churn
        # and forfeited queue time.  Remove only ledger-recognized bot orders
        # from aggregated levels; manual account orders remain untouched.
        if book is not None and open_orders is not None and self.bot_order_ids:
            book = self._book_without_bot_orders(book, open_orders)
        # Inventory at/inside the hard deadline is an exit task, not a
        # market-making opportunity. Establish position first and use any
        # available BBO even if full L2 depth is thin/stale/unavailable.
        # This prevents entry guards from trapping a position through
        # settlement, the exact real-account failure this path was added for.
        if self.force_flatten:
            try:
                net_position, _avg_cost, _exposure_usd = self._get_position_summary()
            except UsApiError as exc:
                logger.error(
                    "Could not establish hard-flatten position for %s: %s",
                    self.market_slug, exc,
                )
                self._cancel_existing_orders(open_orders)
                return None
            if net_position != 0:
                bbo = _bbo_from_book(book)
                if bbo is None:
                    bbo = self.read_client.get_market_bbo(
                        self.market_slug,
                        priority=self.force_flatten or self.reduce_urgent,
                    )
                return self._refresh_forced_flatten(
                    net_position, bbo, max_orders=max_orders, open_orders=open_orders,
                )

        if self.settings.require_l2_depth:
            # Safe default: no real, fresh L2 book means no quoting at all --
            # never silently fall back to pricing off a bare BBO number with
            # no visibility into actual depth.
            if book is None:
                logger.info(
                    "Skipping %s this cycle -- L2 order book unavailable and "
                    "LIVE_REQUIRE_L2_DEPTH is on; refusing to quote from "
                    "BBO-only data.",
                    self.market_slug,
                )
                self._cancel_existing_orders(open_orders)
                return None
            if not self._book_has_enough_depth(book):
                logger.info(
                    "Skipping %s this cycle -- visible book depth is too thin for safe quoting.",
                    self.market_slug,
                )
                self._cancel_existing_orders(open_orders)
                return None
            bbo = _bbo_from_book(book)
        else:
            if book is not None and not self._book_has_enough_depth(book):
                logger.info(
                    "Skipping %s this cycle -- visible book depth is too thin for safe quoting.",
                    self.market_slug,
                )
                self._cancel_existing_orders(open_orders)
                return None
            bbo = (
                _bbo_from_book(book) if book is not None
                else self.read_client.get_market_bbo(
                    self.market_slug,
                    priority=self.force_flatten or self.reduce_urgent,
                )
            )

        best_bid, best_ask = self._pick_book(bbo)
        if best_bid is None or best_ask is None:
            logger.warning(
                "No usable bid/ask for %s; cancelling its stale orders and skipping.",
                self.market_slug,
            )
            self._cancel_existing_orders(open_orders)
            return None
        reference_price = round((best_bid + best_ask) / 2, 6)

        self.volatility_tracker.record(self.market_slug, reference_price)
        if self.settings.volatility_filter_enabled:
            recent_move = self.volatility_tracker.recent_move_cents(self.market_slug)
            if recent_move is not None and recent_move > self.settings.max_recent_move_cents:
                logger.info(
                    "Skipping %s this cycle -- recent price move %.2fc exceeds "
                    "the %.2fc volatility guard.",
                    self.market_slug, recent_move, self.settings.max_recent_move_cents,
                )
                self._cancel_existing_orders(open_orders)
                return None

        quote = compute_book_aware_quote(
            best_bid, best_ask, self.tick_size, self.settings.min_edge_cents
        )
        if quote is None:
            logger.info(
                "Skipping %s this cycle -- no edge left after improving the book "
                "by 1 tick (best_bid=%.4f, best_ask=%.4f, tick=%.4f, min_edge=%.2fc).",
                self.market_slug, best_bid, best_ask, self.tick_size, self.settings.min_edge_cents,
            )
            self._cancel_existing_orders(open_orders)
            return None

        captured_spread_cents = (quote.ask - quote.bid) * 100

        try:
            net_position, avg_cost, exposure_usd = self._get_position_summary()
        except UsApiError as exc:
            logger.error(
                "Could not establish position for %s; cancelling stale orders and "
                "skipping instead of assuming flat: %s",
                self.market_slug, exc,
            )
            self._cancel_existing_orders(open_orders)
            return None
        bid_candidate = self._apply_inventory_price_skew("BUY", quote.bid, quote.ask, net_position, exposure_usd)
        ask_candidate = self._apply_inventory_price_skew("SELL", quote.ask, quote.bid, net_position, exposure_usd)

        bid_price, bid_skip = self._resolve_leg_price(
            "BUY", bid_candidate, net_position, avg_cost, exposure_usd, captured_spread_cents,
        )
        ask_price, ask_skip = self._resolve_leg_price(
            "SELL", ask_candidate, net_position, avg_cost, exposure_usd, captured_spread_cents,
        )

        if bid_price is None and ask_price is None:
            logger.info(
                "Skipping %s this cycle -- both legs blocked by position guards "
                "(bid: %s, ask: %s).", self.market_slug, bid_skip, ask_skip,
            )
            self._cancel_existing_orders(open_orders)
            return None

        if max_orders is not None:
            bid_price, bid_skip, ask_price, ask_skip = _apply_order_budget(
                max_orders, bid_price, bid_skip, ask_price, ask_skip
            )
            if bid_price is None and ask_price is None:
                logger.info(
                    "Skipping %s this cycle -- live order budget is exhausted.",
                    self.market_slug,
                )
                return None

        # increasing_order_shares (toxicity/event-warn discount) only ever
        # applies to whichever side is increasing -- the reducing/exit side
        # always uses the full, undiscounted average, so a held position can
        # always be offered in one order regardless of cooldown state (see
        # increasing_order_shares' own comment in __init__).
        reducing_base_shares = (self.settings.order_shares_min + self.settings.order_shares_max) / 2
        increasing_base_shares = (
            self.increasing_order_shares
            if self.increasing_order_shares is not None
            else reducing_base_shares
        )
        bid_base_shares = increasing_base_shares if net_position >= 0 else reducing_base_shares
        ask_base_shares = increasing_base_shares if net_position <= 0 else reducing_base_shares
        bid_shares = self._apply_inventory_size_skew("BUY", bid_base_shares, net_position, exposure_usd)
        ask_shares = self._apply_inventory_size_skew("SELL", ask_base_shares, net_position, exposure_usd)

        # A reducing quote must never cross through flat and create a new
        # position in the opposite direction.  This happened on the real
        # account when a -3 share short was given the normal 6-share BUY
        # size: the fill closed the short and opened +3 shares long.  Cap
        # the reducing leg to the position that actually exists; the next
        # cycle can reassess from a fresh position update.
        if net_position < 0 and bid_price is not None:
            bid_shares = min(bid_shares, abs(net_position))
        if net_position > 0 and ask_price is not None:
            ask_shares = min(ask_shares, abs(net_position))

        # Event-exposure cap: size the increasing leg(s) down to fit
        # remaining headroom, rather than only a binary block once already
        # at/over cap (that binary block is reduce_only_reason, enforced
        # above in _resolve_leg_price). This is the protection for a market
        # comfortably UNDER cap today that could otherwise post a full-size
        # order that alone jumps its bucket over -- the exact gap behind the
        # real 2026-07-09 Argentina-Switzerland overshoot. See
        # live/RUNBOOK.md's most recent section.
        #
        # At net_position == 0 (flat), BOTH bid and ask are increasing
        # simultaneously, but a filled bid and filled ask on the SAME
        # token can't both be simultaneously realized risk -- if both
        # fill, they net to a captured spread, not additive exposure. Each
        # increasing leg is therefore clipped against the FULL shared
        # budget independently, not a proportional split of it (reverting
        # an earlier, since-reconsidered fix that split it -- see
        # live/RUNBOOK.md's most recent section for why, and the
        # regression test replaying the real 2026-07-09 incident that
        # gates this change). The cross-market running tally
        # (multi_market_maker.py::_record_provisional_exposure) is what
        # actually protects against the real incident (sibling markets in
        # the same bucket filling within one cycle) and is untouched by
        # this -- it stays fully additive across markets.
        if self.event_cap_remaining_usd is not None:
            bid_increasing = net_position >= 0 and bid_price is not None and bid_price > 0
            ask_increasing = net_position <= 0 and ask_price is not None and ask_price > 0
            short_risk_per_share = (1.0 - ask_price) if ask_increasing else 0.0

            if bid_increasing:
                max_bid_shares = self.event_cap_remaining_usd / bid_price
                if bid_shares > max_bid_shares:
                    if _align_order_shares(max_bid_shares, self.min_trade_qty) <= 0:
                        bid_price, bid_skip = None, "event exposure cap: no headroom remaining"
                    else:
                        bid_shares = max_bid_shares
            if ask_increasing and short_risk_per_share > 0:
                max_ask_shares = self.event_cap_remaining_usd / short_risk_per_share
                if ask_shares > max_ask_shares:
                    if _align_order_shares(max_ask_shares, self.min_trade_qty) <= 0:
                        ask_price, ask_skip = None, "event exposure cap: no headroom remaining"
                    else:
                        ask_shares = max_ask_shares

        # Opening only one side from flat is a directional bet, not market
        # making. A guard can reject one leg asymmetrically (as happened in
        # the 2026-07-12 live test), and a one-slot order budget can do the
        # same. Pair the entry after every price/budget/exposure constraint
        # has run. Once inventory exists, flat-first/reduce-only logic still
        # permits the single reducing leg as intended.
        paired_entry_blocked = False
        if (
            self.settings.require_both_entry_legs
            and net_position == 0
            and ((bid_price is None) != (ask_price is None))
        ):
            paired_entry_blocked = True
            opposite_reason = ask_skip if ask_price is None else bid_skip
            paired_reason = f"paired-entry requirement: opposite leg blocked ({opposite_reason})"
            if bid_price is not None:
                bid_price, bid_skip = None, paired_reason
            if ask_price is not None:
                ask_price, ask_skip = None, paired_reason

        if paired_entry_blocked:
            logger.info(
                "Skipping %s this cycle -- no complete safe entry pair remains "
                "after position, budget, and exposure guards (bid: %s, ask: %s).",
                self.market_slug, bid_skip, ask_skip,
            )
            self._cancel_existing_orders(open_orders)
            return None

        kept_orders, reconciled = self._reconcile_existing_orders(
            open_orders,
            bid_price=bid_price,
            bid_shares=bid_shares,
            ask_price=ask_price,
            ask_shares=ask_shares,
        )
        if not reconciled:
            logger.error(
                "At least one existing order for %s could not be cancelled; "
                "skipping replacement quotes to avoid duplicate exposure.",
                self.market_slug,
            )
            return None

        bid_leg: Optional[PostedLeg] = None
        ask_leg: Optional[PostedLeg] = None
        try:
            # Maker-only: ordinary GTC entries (and non-urgent reducing
            # orders, which share this same call site) rest passively and
            # get rejected rather than take liquidity if the book moves
            # between observation and submission -- confirmed real via
            # docs.polymarket.us. force_flatten's IOC path (below) must NOT
            # set this -- a mandatory liquidation has to be allowed to take
            # liquidity. See live/RUNBOOK.md's most recent section.
            bid_leg = self._keep_post_or_skip(
                "BUY", bid_price, quote.bid, bid_shares, bid_skip, kept_orders.get("BUY"),
                participate_dont_initiate=True,
            )
            ask_leg = self._keep_post_or_skip(
                "SELL", ask_price, quote.ask, ask_shares, ask_skip, kept_orders.get("SELL"),
                participate_dont_initiate=True,
            )
            if (
                self.settings.require_both_entry_legs
                and net_position == 0
                and bool(bid_leg.order_id) != bool(ask_leg.order_id)
            ):
                # A maker-only rejection is discovered only AFTER both legs
                # are attempted -- the paired_entry_blocked check above
                # can't see it, since it only knows about OUR OWN pre-
                # placement skip reasons, not an exchange-side rejection.
                # Exactly one leg resting (posted or kept -- both already
                # normalize to PostedLeg.order_id, so this needs no origin
                # distinction) is exactly the naked, unpaired directional
                # bet require_both_entry_legs exists to prevent.
                bid_leg, ask_leg = self._unwind_unpaired_entry(bid_leg, ask_leg)
        except UnsafeOrderStateError as exc:
            logger.error("%s Sending account-wide cancel safeguard.", exc)
            known_legs = [leg for leg in (bid_leg, ask_leg) if leg and leg.order_id]
            for leg in known_legs:
                try:
                    self.client.cancel_order(leg.order_id, self.market_slug)
                except UsApiError as cancel_exc:
                    logger.error("Could not cancel known sibling order %s: %s", leg.order_id, cancel_exc)
            try:
                self.client.cancel_all()
            except Exception as cancel_exc:  # noqa: BLE001 -- verified below regardless of outcome
                logger.error("Account-wide cancel safeguard raised: %s", cancel_exc)
            if not self._verify_clean_slate():
                raise EmergencySafeguardFailedError(
                    f"Could not confirm {self.market_slug} has no resting orders after "
                    f"the emergency cancel safeguard for: {exc}"
                ) from exc
            return None

        cycle = LiveQuoteCycle(
            cycle_id=uuid4().hex,
            market_id=self.market_slug,
            reference_price=reference_price,
            tick_size=self.tick_size,
            bid=bid_leg,
            ask=ask_leg,
            timestamp=utcnow_iso(),
        )
        try:
            record_cycle(cycle)
        except Exception:
            # Without the ledger row these orders are no longer provably
            # bot-owned, so compensate immediately while their ids are in
            # hand instead of leaving invisible GTC exposure behind.
            for leg in (bid_leg, ask_leg):
                if not leg.is_resting:
                    # Not order_id truthiness alone: an unwound leg's
                    # order_id is deliberately preserved (see
                    # _unwind_unpaired_entry) even though it's already
                    # cancelled and has nothing left to compensate for.
                    continue
                try:
                    self.client.cancel_order(leg.order_id, self.market_slug)
                except UsApiError as exc:
                    logger.error(
                        "Failed to compensate order %s after ledger write failure: %s",
                        leg.order_id, exc,
                    )
            raise
        logger.info(
            "Refresh cycle complete for %s: book=[%.4f, %.4f] bid=%s ask=%s "
            "position=%.4f avg_cost=%s",
            self.market_slug, best_bid, best_ask, bid_price, ask_price,
            net_position, avg_cost,
        )
        return cycle

    @staticmethod
    def _pick_book(bbo: Optional[dict]) -> tuple[Optional[float], Optional[float]]:
        if not bbo:
            return None, None
        return bbo.get("best_bid"), bbo.get("best_ask")

    def _get_book(self) -> Optional[dict]:
        get_market_book = getattr(self.read_client, "get_market_book", None)
        if get_market_book is None:
            return None
        book = get_market_book(
            self.market_slug,
            priority=self.force_flatten or self.reduce_urgent,
        )
        return book if isinstance(book, dict) else None

    def _book_has_enough_depth(self, book: dict) -> bool:
        return book_has_enough_depth(book, self.settings)

    def _book_without_bot_orders(self, book: dict, open_orders: list[dict]) -> dict:
        return book_without_bot_orders(
            book,
            open_orders,
            market_slug=self.market_slug,
            bot_order_ids=self.bot_order_ids,
        )

    # ------------------------------------------------------------------
    # Position awareness
    # ------------------------------------------------------------------
    def _get_position_summary(self) -> tuple[float, Optional[float], float]:
        """Returns (net_position_shares, avg_cost_or_None, current_value_usd).
        A confirmed absent position is flat; an unavailable position read
        raises so the caller can fail closed."""
        if self.position_snapshot_known:
            position = self.position_snapshot
        else:
            position = self.client.get_position(self.market_slug)

        if not position:
            return 0.0, None, 0.0

        try:
            net_position = float(position["netPositionDecimal"])
        except (KeyError, TypeError, ValueError) as exc:
            raise UsApiError(
                f"Position for {self.market_slug} has no valid netPositionDecimal"
            ) from exc
        if net_position == 0:
            return 0.0, None, 0.0
        try:
            cost = float(_nested(position, "cost", "value"))
            raw_cash_value = float(_nested(position, "cashValue", "value"))
        except (TypeError, ValueError) as exc:
            raise UsApiError(
                f"Non-flat position for {self.market_slug} is missing cost/cashValue"
            ) from exc
        # The exchange reports long cost as premium paid, but short cost as
        # complementary collateral (confirmed on the real account: a short
        # opened around 0.22 reported roughly 0.78/share cost). Liquidation
        # compares against the original YES trade price, so invert collateral
        # for shorts.
        cost_per_share = abs(cost / net_position) if net_position != 0 else None
        avg_cost = (
            cost_per_share
            if net_position > 0
            else 1.0 - cost_per_share if net_position < 0 and cost_per_share is not None
            else None
        )
        if avg_cost is not None:
            avg_cost = round(avg_cost, 10)
        if avg_cost is not None and not 0 < avg_cost < 1:
            logger.warning(
                "Ignoring invalid average cost %.4f for %s; liquidation bound disabled this cycle.",
                avg_cost, self.market_slug,
            )
            avg_cost = None
        cash_value = abs(raw_cash_value)
        # Never let a losing mark shrink the risk number and reopen room to
        # add more. Cost basis is the floor; profitable appreciation may
        # still increase the inventory-skew/cap signal.
        exposure_usd = max(abs(cost), cash_value)
        return net_position, avg_cost, exposure_usd

    def _resolve_leg_price(
        self,
        side: str,
        price: float,
        net_position: float,
        avg_cost: Optional[float],
        exposure_usd: float,
        captured_spread_cents: float,
    ) -> tuple[Optional[float], Optional[str]]:
        """Returns (effective_price_or_None, skip_reason_or_None) for one leg,
        applying the time/risk liquidation bound to the reducing side and the inventory
        cap, extreme-price edge requirement, and payoff-ratio screen to the
        increasing side. Flat (net_position == 0) applies neither of the
        reducing-side checks."""
        is_reducing = (side == "SELL" and net_position > 0) or (side == "BUY" and net_position < 0)
        is_increasing = (side == "BUY" and net_position >= 0) or (side == "SELL" and net_position <= 0)

        if is_reducing:
            if not self.reduce_urgent and self.in_cooldown:
                return None, "reducing patience (toxicity cooldown, not urgent)"
            if avg_cost is None:
                return None, "cost basis unavailable for bounded liquidation"
            allowed_loss_cents = self._allowed_liquidation_loss_cents()
            adjusted = apply_liquidation_limit(
                price,
                net_position,
                avg_cost,
                self.tick_size,
                allowed_loss_cents,
                self.settings.liquidation_max_loss_usd,
            )
            if adjusted is None:
                return None, "outside bounded liquidation loss limit"
            return adjusted, None

        if (
            is_increasing
            and net_position != 0
            and self.settings.flat_first_inventory_enabled
        ):
            return None, "flat-first inventory mode: outstanding position must be reduced first"

        if is_increasing and self.reduce_only_reason:
            return None, f"reduce-only ({self.reduce_only_reason})"

        if is_increasing and abs(exposure_usd) >= self.settings.max_position_usd:
            return None, (
                f"position already at cap (${abs(exposure_usd):.2f} >= "
                f"${self.settings.max_position_usd:.2f})"
            )

        if is_increasing and self._is_extreme_price(price) and captured_spread_cents < self.settings.extreme_price_min_edge_cents:
            return None, (
                f"extreme price ({price:.4f}) with insufficient edge "
                f"({captured_spread_cents:.2f}c < {self.settings.extreme_price_min_edge_cents:.2f}c required)"
            )

        if is_increasing and captured_spread_cents > 0:
            max_loss_per_share_cents = (price if side == "BUY" else (1 - price)) * 100
            ratio = max_loss_per_share_cents / captured_spread_cents
            if ratio > self.settings.max_payoff_loss_to_capture_ratio:
                return None, (
                    f"payoff ratio too extreme ({ratio:.1f}x max-loss-to-captured-spread, "
                    f"cap {self.settings.max_payoff_loss_to_capture_ratio:.1f}x)"
                )

        return price, None

    def _refresh_forced_flatten(
        self,
        net_position: float,
        bbo: Optional[dict],
        *,
        max_orders: Optional[int],
        open_orders: Optional[list[dict]],
    ) -> Optional[LiveQuoteCycle]:
        """Post exactly one marketable reducing LIMIT for all inventory.

        Long inventory sells at the visible best bid; short inventory buys
        at the visible best ask. The limit price bounds execution while
        making the order immediately executable against displayed liquidity.
        Normal depth, volatility, edge, payoff-ratio, and cost-basis loss
        checks are entry/patient-exit controls and intentionally do not run.
        """
        best_bid, best_ask = self._pick_book(bbo)
        exit_price = best_bid if net_position > 0 else best_ask
        if exit_price is None:
            # Preserve any already-resting reducing order, but remove the
            # inventory-increasing side immediately. With no price, posting
            # a new marketable limit would require guessing and is unsafe.
            cancelled = self._cancel_increasing_orders(open_orders, net_position)
            logger.error(
                "Hard flatten for %s has no usable BBO; %s increasing orders "
                "and preserving any existing reducing order.",
                self.market_slug,
                "cancelled" if cancelled else "could not cancel all",
            )
            return None

        reference_price = round(
            (best_bid + best_ask) / 2
            if best_bid is not None and best_ask is not None
            else exit_price,
            6,
        )
        shares = abs(net_position)
        if net_position > 0:
            bid_price, bid_skip, bid_shares = None, "hard flatten: long inventory", 0.0
            ask_price, ask_skip, ask_shares = best_bid, None, shares
        else:
            bid_price, bid_skip, bid_shares = best_ask, None, shares
            ask_price, ask_skip, ask_shares = None, "hard flatten: short inventory", 0.0

        if max_orders is not None and max_orders < 1:
            logger.error("Hard flatten for %s has no order-budget slot.", self.market_slug)
            return None

        kept_orders, reconciled = self._reconcile_existing_orders(
            open_orders,
            bid_price=bid_price,
            bid_shares=bid_shares,
            ask_price=ask_price,
            ask_shares=ask_shares,
        )
        if not reconciled:
            logger.error(
                "Hard flatten could not cancel stale orders for %s; refusing a duplicate exit.",
                self.market_slug,
            )
            return None

        bid_leg: Optional[PostedLeg] = None
        try:
            bid_leg = self._keep_post_or_skip(
                "BUY", bid_price, best_ask, bid_shares, bid_skip, kept_orders.get("BUY"),
                tif="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            )
            ask_leg = self._keep_post_or_skip(
                "SELL", ask_price, best_bid, ask_shares, ask_skip, kept_orders.get("SELL"),
                tif="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            )
        except UnsafeOrderStateError as exc:
            logger.error("%s Sending account-wide cancel safeguard.", exc)
            for leg in (bid_leg,):
                if not leg or not leg.order_id:
                    continue
                try:
                    self.client.cancel_order(leg.order_id, self.market_slug)
                except UsApiError as cancel_exc:
                    logger.error("Could not cancel hard-flatten sibling %s: %s", leg.order_id, cancel_exc)
            self.client.cancel_all()
            return None

        cycle = LiveQuoteCycle(
            cycle_id=uuid4().hex,
            market_id=self.market_slug,
            reference_price=reference_price,
            tick_size=self.tick_size,
            bid=bid_leg,
            ask=ask_leg,
            timestamp=utcnow_iso(),
        )
        try:
            record_cycle(cycle)
        except Exception:
            for leg in (bid_leg, ask_leg):
                if leg.order_id:
                    try:
                        self.client.cancel_order(leg.order_id, self.market_slug)
                    except UsApiError as exc:
                        logger.error("Failed to cancel hard-flatten order %s: %s", leg.order_id, exc)
            raise
        logger.warning(
            "Hard flatten active for %s: position=%.4f bid=%s ask=%s book=[%s, %s]",
            self.market_slug, net_position, bid_price, ask_price, best_bid, best_ask,
        )
        return cycle

    def _allowed_liquidation_loss_cents(self) -> float:
        if self.reduce_urgent:
            return max(0.0, self.settings.liquidation_urgent_max_loss_cents)
        max_hours = self.settings.liquidation_max_holding_hours
        if self.position_age_hours is None or max_hours <= 0:
            return 0.0
        age_fraction = max(0.0, min(1.0, self.position_age_hours / max_hours))
        return max(0.0, self.settings.liquidation_max_loss_cents) * age_fraction

    def _is_extreme_price(self, price: float) -> bool:
        return (
            price <= self.settings.extreme_price_low_threshold
            or price >= self.settings.extreme_price_high_threshold
        )

    def _apply_inventory_price_skew(
        self,
        side: str,
        price: float,
        other_side_price: float,
        net_position: float,
        exposure_usd: float,
    ) -> float:
        if (
            not self.settings.inventory_skew_enabled
            or abs(exposure_usd) < self.settings.inventory_skew_threshold_usd
            or net_position == 0
        ):
            return price

        is_reducing = (side == "SELL" and net_position > 0) or (side == "BUY" and net_position < 0)
        if is_reducing and not self.reduce_urgent:
            return price
        min_price = self.tick_size
        max_price = 1 - self.tick_size

        if is_reducing and side == "SELL":
            return max(other_side_price + self.tick_size, price - self.tick_size, min_price)
        if is_reducing and side == "BUY":
            return min(other_side_price - self.tick_size, price + self.tick_size, max_price)
        if not is_reducing and side == "BUY":
            return max(price - self.tick_size, min_price)
        if not is_reducing and side == "SELL":
            return min(price + self.tick_size, max_price)
        return price

    def _apply_inventory_size_skew(
        self,
        side: str,
        base_shares: float,
        net_position: float,
        exposure_usd: float,
    ) -> float:
        if (
            not self.settings.inventory_skew_enabled
            or abs(exposure_usd) < self.settings.inventory_skew_threshold_usd
            or net_position == 0
        ):
            return base_shares

        is_reducing = (side == "SELL" and net_position > 0) or (side == "BUY" and net_position < 0)
        multiplier = (
            self.settings.inventory_reducing_size_multiplier
            if is_reducing
            else self.settings.inventory_increasing_size_multiplier
        )
        return base_shares * max(0.0, multiplier)

    # ------------------------------------------------------------------
    def _keep_post_or_skip(
        self,
        action: str,
        effective_price: Optional[float],
        original_price: float,
        order_shares: float,
        skip_reason: Optional[str],
        kept_order: Optional[dict],
        *,
        tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        participate_dont_initiate: bool = False,
    ) -> PostedLeg:
        if kept_order is None:
            return self._post_or_skip(
                action, effective_price, original_price, order_shares, skip_reason, tif=tif,
                participate_dont_initiate=participate_dont_initiate,
            )
        order_id = str(kept_order.get("id") or kept_order.get("orderId"))
        remaining = _order_remaining_quantity(kept_order)
        logger.info(
            "Keeping unchanged %s order for %s: price=%.4f remaining=%.4f id=%s",
            action, self.market_slug, effective_price, remaining, order_id,
        )
        return PostedLeg(
            side=action,
            price=float(effective_price),
            size=remaining,
            order_id=order_id,
        )

    def _post_or_skip(
        self,
        action: str,
        effective_price: Optional[float],
        original_price: float,
        order_shares: float,
        skip_reason: Optional[str],
        *,
        tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        participate_dont_initiate: bool = False,
    ) -> PostedLeg:
        if effective_price is None:
            logger.info("Skipping %s leg for %s: %s", action, self.market_slug, skip_reason)
            return PostedLeg(side=action, price=original_price, size=0.0, error=f"skipped: {skip_reason}")
        aligned_shares = _align_order_shares(order_shares, self.min_trade_qty)
        if aligned_shares <= 0:
            logger.info("Skipping %s leg for %s: order share size must be positive", action, self.market_slug)
            return PostedLeg(
                side=action,
                price=original_price,
                size=0.0,
                error="skipped: order share size must be positive",
            )
        return self._post_leg(
            action, effective_price, aligned_shares, tif=tif,
            participate_dont_initiate=participate_dont_initiate,
        )

    def _post_leg(
        self,
        action: str,
        price: float,
        quantity: float,
        *,
        tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        participate_dont_initiate: bool = False,
    ) -> PostedLeg:
        api_action = "ORDER_ACTION_BUY" if action == "BUY" else "ORDER_ACTION_SELL"
        try:
            response = self.client.create_order(
                market_slug=self.market_slug,
                outcome_side="OUTCOME_SIDE_YES",
                action=api_action,
                price=price,
                quantity=quantity,
                tif=tif,
                participate_dont_initiate=participate_dont_initiate,
            )
            order_id = _extract_order_id(response)
            if not order_id:
                raise UnsafeOrderStateError(
                    f"Exchange accepted {action} on {self.market_slug} but returned no order id."
                )
            logger.info(
                "Posted %s order for %s: price=%.4f qty=%.4f id=%s",
                action, self.market_slug, price, quantity, order_id,
            )
            return PostedLeg(side=action, price=price, size=quantity, order_id=order_id)
        except UsApiError as exc:
            if is_client_rejection(exc):
                # A 4xx means the exchange synchronously validated and
                # rejected this specific request -- nothing was created, no
                # uncertainty about whether it succeeded (unlike a 5xx/
                # timeout/connection failure below, which keeps the nuclear
                # account-wide-cancel safeguard). Most relevant for
                # participate_dont_initiate orders (the exchange rejects
                # rather than crossing the book -- an expected, frequent
                # outcome once maker-only entries are enabled), but true
                # for any definite rejection. See live/RUNBOOK.md's most
                # recent section.
                logger.info(
                    "%s leg for %s rejected by the exchange (not placed): %s",
                    action, self.market_slug, exc,
                )
                return PostedLeg(side=action, price=price, size=0.0, error=f"rejected: {exc}")
            raise UnsafeOrderStateError(
                f"Placement result for {action} on {self.market_slug} is uncertain: {exc}"
            ) from exc

    def _verify_clean_slate(self) -> bool:
        """After the account-wide cancel-all safeguard runs, confirm no
        order for THIS market is actually still open on the exchange.
        cancel_all() is best-effort and never raises on an enumeration or
        per-order cancel failure (see us_client.py), so its mere return
        proves nothing -- this is the only thing that can. Fails closed: if
        the verification fetch itself fails, that's ALSO treated as "not
        verified" rather than assumed clean."""
        try:
            open_orders = self.client.get_open_orders()
        except UsApiError as exc:
            logger.error("Could not verify a clean slate for %s: %s", self.market_slug, exc)
            return False
        remaining = [
            o for o in open_orders
            if (o.get("marketSlug") or o.get("market_slug")) == self.market_slug
        ]
        if remaining:
            logger.error(
                "%d order(s) for %s still open after the emergency cancel safeguard: %s",
                len(remaining), self.market_slug, remaining,
            )
            return False
        return True

    def _unwind_unpaired_entry(self, bid_leg: PostedLeg, ask_leg: PostedLeg) -> tuple[PostedLeg, PostedLeg]:
        """Called when exactly one of bid_leg/ask_leg ended up resting
        (order_id set) while the other was rejected/skipped, from a flat
        position under require_both_entry_legs -- a naked, unpaired
        directional bet, not market making. Cancels the surviving leg
        immediately and returns both legs updated to reflect that (the
        survivor zeroed out with an explanatory error, the already-rejected
        one unchanged).

        The survivor's order_id is deliberately PRESERVED (not nulled) so
        record_cycle() below still writes it to the ledger: if it partially
        filled during the brief submit-to-cancel race and the private
        WebSocket missed that execution, execution-backfill
        (multi_market_maker.py::_backfill_missed_executions) can only ever
        discover it by looking up an order_id it already knows about.
        size=0.0 is what now signals "not resting" instead -- see
        PostedLeg.is_resting, which every caller that cares about "is this
        leg actually live on the exchange" must use instead of a bare
        order_id truthiness check now that the two can diverge.

        Also appended to self.last_cancelled_order_ids (the same list
        _cancel_existing_orders/_cancel_increasing_orders append to) so the
        caller's existing last_cancelled_order_ids -> private_store.
        remove_orders(...) wiring (multi_market_maker.py::_run_one_market)
        removes it from the private open-order store immediately, same as
        any other cancellation.

        Fails closed: if the cancellation itself can't be confirmed, raises
        UnsafeOrderStateError rather than silently leaving the surviving
        leg resting -- the caller's existing except UnsafeOrderStateError
        block (in refresh_quotes) then runs the same account-wide cancel
        safeguard already used for any other uncertain placement state."""
        if bid_leg.order_id:
            surviving, surviving_is_bid = bid_leg, True
        else:
            surviving, surviving_is_bid = ask_leg, False
        try:
            self.client.cancel_order(surviving.order_id, self.market_slug)
        except UsApiError as exc:
            raise UnsafeOrderStateError(
                f"Could not cancel unpaired {surviving.side} leg {surviving.order_id} for "
                f"{self.market_slug} after its sibling was rejected -- state is uncertain: {exc}"
            ) from exc
        logger.warning(
            "Cancelled unpaired %s leg %s for %s after its sibling was rejected -- "
            "a flat position requires both legs or neither.",
            surviving.side, surviving.order_id, self.market_slug,
        )
        self.last_cancelled_order_ids.append(str(surviving.order_id))
        cancelled = PostedLeg(
            side=surviving.side, price=surviving.price, size=0.0, order_id=surviving.order_id,
            error="cancelled: sibling leg rejected, unpaired entry not allowed from flat",
        )
        return (cancelled, ask_leg) if surviving_is_bid else (bid_leg, cancelled)

    def _cancel_existing_orders(self, open_orders: Optional[list[dict]] = None) -> bool:
        """Cancels only this market's resting orders -- not the whole
        account's book -- so the bot doesn't touch unrelated activity.
        Fetches the account's open orders itself only if the caller didn't
        already pass in a (shared, account-wide) list."""
        if open_orders is None:
            open_orders = self.client.get_open_orders()
        matching = [
            o for o in open_orders
            if (o.get("marketSlug") or o.get("market_slug")) == self.market_slug
        ]
        all_cancelled = True
        for order in matching:
            order_id = order.get("id") or order.get("orderId")
            if order_id:
                try:
                    self.client.cancel_order(order_id, self.market_slug)
                    self.last_cancelled_order_ids.append(str(order_id))
                except UsApiError as exc:
                    all_cancelled = False
                    logger.warning("Failed to cancel order %s: %s", order_id, exc)
        return all_cancelled

    def _cancel_increasing_orders(
        self, open_orders: Optional[list[dict]], net_position: float,
    ) -> bool:
        """Cancel only orders that could add risk when an emergency exit
        lacks a trustworthy BBO. Existing reducing orders are preserved so
        a temporary market-data outage cannot remove the only exit already
        resting on the exchange.
        """
        if open_orders is None:
            open_orders = self.client.get_open_orders()
        increasing_side = "BUY" if net_position > 0 else "SELL"
        matching = [
            order for order in open_orders
            if (order.get("marketSlug") or order.get("market_slug")) == self.market_slug
            and _order_side(order) == increasing_side
        ]
        all_cancelled = True
        for order in matching:
            order_id = order.get("id") or order.get("orderId")
            if not order_id:
                continue
            try:
                self.client.cancel_order(order_id, self.market_slug)
                self.last_cancelled_order_ids.append(str(order_id))
            except UsApiError as exc:
                all_cancelled = False
                logger.warning("Failed to cancel increasing order %s: %s", order_id, exc)
        return all_cancelled

    def _reconcile_existing_orders(
        self,
        open_orders: Optional[list[dict]],
        *,
        bid_price: Optional[float],
        bid_shares: float,
        ask_price: Optional[float],
        ask_shares: float,
    ) -> tuple[dict[str, dict], bool]:
        """Keep one safe, unchanged order per desired side and cancel only
        stale/excess orders.

        The previous cancel-before-every-post loop forfeited queue priority
        once per minute even when price and size had not changed.  A
        partially-filled order is also safe to keep when its remaining
        quantity is no larger than the newly desired quantity.  Anything
        larger, differently priced, duplicated, or no longer desired is
        cancelled before a replacement can be posted.
        """
        if open_orders is None:
            open_orders = self.client.get_open_orders()
        matching = [
            order for order in open_orders
            if (order.get("marketSlug") or order.get("market_slug")) == self.market_slug
        ]
        desired = {
            "BUY": (bid_price, _align_order_shares(bid_shares, self.min_trade_qty)),
            "SELL": (ask_price, _align_order_shares(ask_shares, self.min_trade_qty)),
        }
        kept: dict[str, dict] = {}
        stale: list[dict] = []
        now = datetime.now(timezone.utc)
        for order in matching:
            side = _order_side(order)
            price = _order_price(order)
            remaining = _order_remaining_quantity(order)
            desired_price, desired_size = desired.get(side, (None, 0.0))
            size_is_safe = remaining > 0 and remaining <= desired_size + 1e-9
            if desired_price is None or price is None:
                price_matches = False
            elif self.force_flatten or self.reduce_urgent:
                # Risk exits always override hysteresis and min resting
                # time below and reprice to the current target immediately.
                price_matches = abs(price - desired_price) <= max(1e-9, self.tick_size / 1000)
            else:
                close_enough = (
                    abs(price - desired_price) <= self.tick_size * self.settings.price_hysteresis_ticks + 1e-9
                )
                if close_enough:
                    price_matches = True
                else:
                    # Outside the hysteresis band, direction matters. A BUY
                    # now priced ABOVE the new target, or a SELL now priced
                    # BELOW it, has drifted to the MORE aggressive side --
                    # adverse-selection risk (a stale high bid would keep
                    # buying above fair value if filled; a stale low ask
                    # would keep selling below it) -- and must reprice
                    # immediately regardless of age. Only an order that
                    # drifted to the LESS aggressive side (safe, just less
                    # likely to fill) can still be protected by
                    # min_resting_seconds below.
                    aggressive_side = (
                        (side == "BUY" and price > desired_price)
                        or (side == "SELL" and price < desired_price)
                    )
                    if aggressive_side:
                        price_matches = False
                    else:
                        age_seconds = _order_age_seconds(order, now)
                        price_matches = age_seconds is not None and age_seconds < self.settings.min_resting_seconds
            if side not in kept and price_matches and size_is_safe:
                kept[side] = order
            else:
                stale.append(order)

        all_cancelled = True
        for order in stale:
            order_id = order.get("id") or order.get("orderId")
            if not order_id:
                continue
            try:
                self.client.cancel_order(order_id, self.market_slug)
                self.last_cancelled_order_ids.append(str(order_id))
            except UsApiError as exc:
                all_cancelled = False
                logger.warning("Failed to cancel order %s: %s", order_id, exc)
        return kept, all_cancelled


def _extract_order_id(response: dict) -> Optional[str]:
    if isinstance(response, dict):
        return response.get("id") or response.get("orderId")
    return None


def book_without_bot_orders(
    book: dict,
    open_orders: list[dict],
    *,
    market_slug: str,
    bot_order_ids: set[str],
) -> dict:
    """Return an L2 book with recognized bot liquidity subtracted.

    The ordinary quote cycle and the pilot's five-second fast-repricing
    check must use the same external-book view.  Otherwise the bot's own
    resting quote can remain the public best bid/ask and mask the external
    move the fast check is supposed to detect.
    """
    own_by_side_price: dict[tuple[str, float], float] = {}
    recognized_ids = {str(order_id) for order_id in bot_order_ids}
    for order in open_orders:
        order_id = order.get("id") or order.get("orderId") or order.get("order_id")
        if not order_id or str(order_id) not in recognized_ids:
            continue
        if (order.get("marketSlug") or order.get("market_slug")) != market_slug:
            continue
        side = _order_side(order)
        price = _order_price(order)
        quantity = _order_remaining_quantity(order)
        if side and price is not None and quantity > 0:
            key = (side, round(price, 9))
            own_by_side_price[key] = own_by_side_price.get(key, 0.0) + quantity

    def external_levels(levels: list[dict], side: str) -> list[dict]:
        cleaned: list[dict] = []
        remaining_own = dict(own_by_side_price)
        for level in levels:
            price = _level_price(level)
            if price is None:
                continue
            key = (side, round(price, 9))
            quantity = max(0.0, _level_qty(level) - remaining_own.pop(key, 0.0))
            if quantity > 1e-9:
                cleaned.append({**level, "quantity": quantity})
        return cleaned

    return {
        "bids": external_levels(book.get("bids") or [], "BUY"),
        "asks": external_levels(book.get("asks") or [], "SELL"),
    }


def _order_side(order: dict) -> Optional[str]:
    value = str(order.get("side") or order.get("action") or "").upper()
    if value.endswith("BUY"):
        return "BUY"
    if value.endswith("SELL"):
        return "SELL"
    return None


def _order_price(order: dict) -> Optional[float]:
    value = order.get("price")
    if isinstance(value, dict):
        value = value.get("value")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _order_age_seconds(order: dict, now: datetime) -> Optional[float]:
    """How long this resting order has been alive, from the exchange's own
    open-order timestamps (no new local tracking needed) -- used by
    _reconcile_existing_orders' min_resting_seconds gate. Prefers
    insertTime/createTime (the ORIGINAL post time) over lastTransactTime --
    the latter updates on any transaction event including a partial fill,
    which would otherwise make a long-resting, since-partially-filled order
    look freshly posted and wrongly re-earn min-resting-time protection.
    lastTransactTime is only a fallback for a record missing both.
    None (unknown age) fails toward allowing a reprice rather than toward
    extra, unverifiable stickiness -- only the price-hysteresis check still
    protects an order in that case."""
    timestamp = order.get("insertTime") or order.get("createTime") or order.get("lastTransactTime")
    parsed = parse_transact_time(timestamp)
    return None if parsed is None else (now - parsed).total_seconds()


def _order_remaining_quantity(order: dict) -> float:
    value = order.get("leavesQuantity")
    if value is None:
        value = order.get("leaves_quantity")
    if value is None:
        value = order.get("quantity")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


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


def _bbo_from_book(book: Optional[dict]) -> Optional[dict]:
    if not book:
        return None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids and not asks:
        return None
    return {
        "best_bid": _level_price(bids[0]) if bids else None,
        "best_ask": _level_price(asks[0]) if asks else None,
    }


def _level_price(level: dict) -> Optional[float]:
    try:
        return float(level.get("price"))
    except (AttributeError, TypeError, ValueError):
        return None


def _level_qty(level: dict) -> float:
    try:
        return float(level.get("quantity"))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _align_order_shares(order_shares: float, min_trade_qty: float) -> float:
    try:
        aligned = floor_to_quantity_step(order_shares, min_trade_qty)
    except ValueError:
        return 0.0
    if aligned < min_trade_qty:
        return 0.0
    return round(aligned, 6)


def _apply_order_budget(
    max_orders: int,
    bid_price: Optional[float],
    bid_skip: Optional[str],
    ask_price: Optional[float],
    ask_skip: Optional[str],
) -> tuple[Optional[float], Optional[str], Optional[float], Optional[str]]:
    remaining = max_orders
    if bid_price is not None:
        remaining -= 1
    if ask_price is not None:
        if remaining > 0:
            remaining -= 1
        else:
            ask_price = None
            ask_skip = "live order budget reached"
    return bid_price, bid_skip, ask_price, ask_skip
