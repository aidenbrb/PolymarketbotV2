"""One live market-making refresh cycle: fetch the real order book, join/
improve the best bid and best ask by one tick each (see
live/pricing.py::compute_book_aware_quote), cancel this market's existing
resting orders, post the new pair, and record the cycle to the ledger.

If the real spread doesn't leave enough edge after improving both sides
(see LIVE_MIN_EDGE_CENTS), the cycle deliberately posts nothing -- forcing a
quote with no edge is how a market maker guarantees losing money, not making
it. Existing resting orders are still cancelled in that case so the bot
isn't left holding a stale quote.

Position-aware, on top of that: the bot has no memory of prior cycles by
default, so a naive re-quote could sell an existing position below its own
cost basis, or keep adding to a position that's moving the wrong way. Each
cycle now fetches the real position (net shares, cost basis, current value)
and:
  - never lets its own resting order on the REDUCING side (sell if long, buy
    to cover if short) price below/above cost basis -- see
    live/pricing.py::apply_cost_basis_floor.
  - stops adding to the position once its value hits LIVE_MAX_POSITION_USD.
This protects only against the bot's OWN orders locking in a loss or
compounding exposure. It does NOT protect against a held position simply
resolving unfavorably at market settlement -- that risk is unavoidable for
any held position.

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

from typing import Optional
from uuid import uuid4

from .. import config
from ..logger import get_logger
from ..models import utcnow_iso
from ..polymarket_client import PolymarketClient
from .ledger import record_cycle
from .models import LiveQuoteCycle, PostedLeg
from .pricing import apply_cost_basis_floor, compute_book_aware_quote, floor_to_quantity_step
from .us_client import LiveUsClient, UsApiError
from .volatility_filter import VolatilityTracker

logger = get_logger("live.market_maker")


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

    def refresh_quotes(
        self, max_orders: Optional[int] = None, open_orders: Optional[list[dict]] = None
    ) -> Optional[LiveQuoteCycle]:
        """`open_orders`, if provided, is used instead of calling
        client.get_open_orders() again -- that endpoint returns the whole
        account's orders, not just this market's, so a caller managing many
        markets in one cycle (see MultiMarketMaker) should fetch it once and
        pass it to every MarketMaker rather than each one re-fetching the
        identical account-wide list (this was hitting real rate limits)."""
        if max_orders is not None and max_orders <= 0:
            logger.info("Skipping %s this cycle -- live order budget is exhausted.", self.market_slug)
            return None

        book = self._get_book()
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
            bbo = _bbo_from_book(book) if book is not None else self.read_client.get_market_bbo(self.market_slug)

        best_bid, best_ask = self._pick_book(bbo)
        if best_bid is None or best_ask is None:
            raise RuntimeError(
                f"No usable bid/ask for {self.market_slug}; cannot quote this cycle."
            )
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

        net_position, avg_cost, exposure_usd = self._get_position_summary()
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

        base_order_shares = (self.settings.order_shares_min + self.settings.order_shares_max) / 2
        bid_shares = self._apply_inventory_size_skew("BUY", base_order_shares, net_position, exposure_usd)
        ask_shares = self._apply_inventory_size_skew("SELL", base_order_shares, net_position, exposure_usd)

        self._cancel_existing_orders(open_orders)

        bid_leg = self._post_or_skip("BUY", bid_price, quote.bid, bid_shares, bid_skip)
        ask_leg = self._post_or_skip("SELL", ask_price, quote.ask, ask_shares, ask_skip)

        cycle = LiveQuoteCycle(
            cycle_id=uuid4().hex,
            market_id=self.market_slug,
            reference_price=reference_price,
            tick_size=self.tick_size,
            bid=bid_leg,
            ask=ask_leg,
            timestamp=utcnow_iso(),
        )
        record_cycle(cycle)
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
        book = get_market_book(self.market_slug)
        return book if isinstance(book, dict) else None

    def _book_has_enough_depth(self, book: dict) -> bool:
        levels = max(1, self.settings.depth_levels_to_check)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return False

        bid_top = _level_qty(bids[0])
        ask_top = _level_qty(asks[0])
        if bid_top < self.settings.min_top_depth_shares:
            return False
        if ask_top < self.settings.min_top_depth_shares:
            return False

        bid_total = sum(_level_qty(level) for level in bids[:levels])
        ask_total = sum(_level_qty(level) for level in asks[:levels])
        return (
            bid_total >= self.settings.min_total_depth_shares
            and ask_total >= self.settings.min_total_depth_shares
        )

    # ------------------------------------------------------------------
    # Position awareness
    # ------------------------------------------------------------------
    def _get_position_summary(self) -> tuple[float, Optional[float], float]:
        """Returns (net_position_shares, avg_cost_or_None, current_value_usd).
        Defaults to flat (0, None, 0) if there's no position or the position
        endpoint can't be reached -- the bot then behaves exactly as it did
        before position-awareness existed."""
        try:
            position = self.client.get_position(self.market_slug)
        except UsApiError as exc:
            logger.warning(
                "Could not fetch position for %s (assuming flat): %s", self.market_slug, exc
            )
            return 0.0, None, 0.0

        if not position:
            return 0.0, None, 0.0

        net_position = _to_float(position.get("netPositionDecimal"))
        cost = _to_float(_nested(position, "cost", "value"))
        # Average cost is a per-share price (0-1), always positive -- take
        # the magnitude regardless of whether the API signs "cost" by cash
        # flow direction (negative for a short) or by cost-basis magnitude.
        avg_cost = abs(cost / net_position) if net_position != 0 else None
        exposure_usd = _to_float(_nested(position, "cashValue", "value"))
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
        applying the cost-basis floor to the reducing side and the inventory
        cap, extreme-price edge requirement, and payoff-ratio screen to the
        increasing side. Flat (net_position == 0) applies neither of the
        reducing-side checks."""
        is_reducing = (side == "SELL" and net_position > 0) or (side == "BUY" and net_position < 0)
        is_increasing = (side == "BUY" and net_position >= 0) or (side == "SELL" and net_position <= 0)

        if is_reducing:
            adjusted = apply_cost_basis_floor(price, net_position, avg_cost, self.tick_size)
            if adjusted is None:
                return None, "would realize a loss below cost basis"
            return adjusted, None

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
    def _post_or_skip(
        self,
        action: str,
        effective_price: Optional[float],
        original_price: float,
        order_shares: float,
        skip_reason: Optional[str],
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
        return self._post_leg(action, effective_price, aligned_shares)

    def _post_leg(self, action: str, price: float, quantity: float) -> PostedLeg:
        api_action = "ORDER_ACTION_BUY" if action == "BUY" else "ORDER_ACTION_SELL"
        try:
            response = self.client.create_order(
                market_slug=self.market_slug,
                outcome_side="OUTCOME_SIDE_YES",
                action=api_action,
                price=price,
                quantity=quantity,
            )
            order_id = _extract_order_id(response)
            logger.info(
                "Posted %s order for %s: price=%.4f qty=%.4f id=%s",
                action, self.market_slug, price, quantity, order_id,
            )
            return PostedLeg(side=action, price=price, size=quantity, order_id=order_id)
        except UsApiError as exc:
            logger.warning("Failed to post %s leg for %s: %s", action, self.market_slug, exc)
            return PostedLeg(side=action, price=price, size=quantity, error=str(exc))

    def _cancel_existing_orders(self, open_orders: Optional[list[dict]] = None) -> None:
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
        for order in matching:
            order_id = order.get("id") or order.get("orderId")
            if order_id:
                try:
                    self.client.cancel_order(order_id, self.market_slug)
                except UsApiError as exc:
                    logger.warning("Failed to cancel order %s: %s", order_id, exc)


def _extract_order_id(response: dict) -> Optional[str]:
    if isinstance(response, dict):
        return response.get("id") or response.get("orderId")
    return None


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
    if not bids or not asks:
        return None
    return {
        "best_bid": _level_price(bids[0]),
        "best_ask": _level_price(asks[0]),
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
