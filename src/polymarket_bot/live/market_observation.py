"""Queue-aware, non-trading evidence for live-entry qualification.

The tracker consumes the same normalized L2 snapshots and trade tape as the
live maker. It never places or cancels an order. Only fills produced while a
market is in the live candidate set, passes the shared L2 depth guard, and has
the primary live quote available are admissible for the entry gate.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import shutil
import threading
import time
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Optional

from .. import config, storage
from ..logger import get_logger
from ..polymarket_client import PolymarketClient, PolymarketClientError
from .event_exposure import derive_event_bucket_key
from .fills import compute_executable_markout_cents
from .pricing import (
    apply_liquidation_limit,
    book_has_enough_depth,
    compute_book_aware_quote,
)
from .strategy_lifecycle import controlled_lifecycle

logger = get_logger("live.market_observation")

LEGACY_OBSERVATION_FILE = config.LIVE_TRADES_DIR / "market_observations.json"
OBSERVATION_FILE = config.LIVE_TRADES_DIR / "market_observations_v5.json"
# Each real pilot command gets its own dedicated archive, isolated from both
# OBSERVATION_FILE (the long-running multi-day observation archive) and from
# each other -- a pilot's session-scoped shadow evidence must never be
# interleaved with (or interleave into) the canonical qualification archive.
PILOT_OBSERVATION_FILE = config.LIVE_TRADES_DIR / "pilot_shadow_observations.json"
JULY5_PILOT_OBSERVATION_FILE = config.LIVE_TRADES_DIR / "pilot_shadow_observations_july5.json"
# The risk-free dry-run command's own dedicated archive -- isolated from
# every archive above. See MarketObservationTracker's "Dry-run lifecycle"
# section for the phases below.
DRYRUN_OBSERVATION_FILE = config.LIVE_TRADES_DIR / "dry_run_shadow_observations.json"
DRY_RUN_PHASE_COLLECTING = "COLLECTING"
DRY_RUN_PHASE_GRACE = "GRACE"
DRY_RUN_PHASE_FINALIZING = "FINALIZING"
DRY_RUN_PHASE_COMPLETE = "COMPLETE"
SCHEMA_VERSION = 4
OBSERVATION_MODEL_REVISION = 6
PRIMARY_STRATEGY = "improve_both"
SHADOW_STRATEGIES = (
    PRIMARY_STRATEGY,
    "improve_bid_join_ask",
    "join_bid_improve_ask",
    "join_both",
)
_ONE_MINUTE = 60.0
_FIVE_MINUTES = 300.0


class _SingleProfileObservationTracker:
    def __init__(
        self,
        settings: config.LiveTradingSettings,
        path: Optional[Path] = None,
    ):
        self.settings = settings
        self.path = path or OBSERVATION_FILE
        self._lock = threading.RLock()
        self._last_book_epoch: dict[str, float] = {}
        self._shadow_quotes: dict[str, dict[str, dict[str, Any]]] = {}
        self._cooldowns: dict[tuple[str, str, str], float] = {}
        self._live_candidate_slugs: set[str] = set()
        self._last_persist_epoch = 0.0
        loaded = storage.load_json(self.path, default={})
        if isinstance(loaded, dict) and loaded.get("version") == 3:
            self._state = loaded
        else:
            if loaded:
                logger.warning(
                    "Ignoring legacy market-observation schema; version %d "
                    "requires fresh queue-aware evidence.",
                    3,
                )
            self._state = {"version": 3, "markets": {}}
        self._state.setdefault("markets", {})

    def set_live_candidate_slugs(self, slugs: list[str]) -> None:
        with self._lock:
            self._live_candidate_slugs = {str(slug) for slug in slugs if slug}

    def register_market(
        self,
        slug: str,
        *,
        tick_size: float,
        question: str = "",
        event_id: str = "",
        event_or_close_epoch: Optional[float] = None,
    ) -> None:
        with self._lock:
            market = self._market(slug)
            market["tick_size"] = float(tick_size)
            market["question"] = question
            market["event_id"] = event_id
            if event_or_close_epoch is not None:
                market["event_or_close_epoch"] = float(event_or_close_epoch)

    def record_book(self, slug: str, book: dict[str, Any]) -> None:
        now = time.time()
        bids = list(book.get("bids") or [])
        asks = list(book.get("asks") or [])
        if not bids or not asks:
            return
        bid_prices = [
            value for level in bids
            if (value := _level_float(level, "price")) is not None
        ]
        ask_prices = [
            value for level in asks
            if (value := _level_float(level, "price")) is not None
        ]
        best_bid = max(bid_prices) if bid_prices else None
        best_ask = min(ask_prices) if ask_prices else None
        if best_bid is None or best_ask is None or best_bid >= best_ask:
            return

        changed = False
        with self._lock:
            market = self._market(slug)
            market["last_observed_at_epoch"] = now
            market["book_sample_count"] = int(market.get("book_sample_count", 0)) + 1
            previous = self._last_book_epoch.get(slug)
            self._last_book_epoch[slug] = now
            previously_eligible = bool(
                market.get("last_depth_ok")
                and market.get("last_live_candidate")
                and market.get("last_primary_quote_available")
            )

            tick = _positive_float(market.get("tick_size")) or 0.01
            depth_ok = book_has_enough_depth(book, self.settings)
            live_candidate = slug in self._live_candidate_slugs
            effective_min_edge = _effective_min_edge_cents(
                market, now, self.settings,
            )
            base_plans = _build_quote_plans(
                best_bid,
                best_ask,
                tick,
                effective_min_edge,
                self.settings.max_spread,
            ) if depth_ok else {}
            if self._record_due_forced_exits(
                slug, market, bids, asks, best_bid, best_ask, now,
            ):
                changed = True
            plans, managed_strategies = _position_aware_quote_plans(
                market,
                base_plans,
                tick,
                now,
                self.settings,
            )
            self._update_shadow_quotes(
                slug,
                plans,
                bids,
                asks,
                now,
                depth_ok=depth_ok,
                live_candidate=live_candidate,
                managed_strategies=managed_strategies,
            )

            if previous is not None:
                delta = max(
                    0.0,
                    min(
                        now - previous,
                        max(30.0, self.settings.websocket_stale_after_seconds * 2),
                    ),
                )
                bucket = str(int(now // 60) * 60)
                buckets = market.setdefault("observed_second_buckets", {})
                buckets[bucket] = min(60.0, float(buckets.get(bucket, 0.0)) + delta)
                if (
                    previously_eligible
                    and live_candidate
                    and depth_ok
                    and PRIMARY_STRATEGY in plans
                ):
                    eligible = market.setdefault("eligible_observed_second_buckets", {})
                    eligible[bucket] = min(
                        60.0,
                        float(eligible.get(bucket, 0.0)) + delta,
                    )

            market["last_depth_ok"] = depth_ok
            market["last_live_candidate"] = bool(
                live_candidate or PRIMARY_STRATEGY in managed_strategies
            )
            market["last_primary_quote_available"] = PRIMARY_STRATEGY in plans

            for fill in market.setdefault("hypothetical_fills", []):
                age = now - float(fill.get("observed_at_epoch", now))
                if age >= _ONE_MINUTE and "markout_1m_cents" not in fill:
                    fill["markout_1m_cents"] = compute_executable_markout_cents(
                        fill.get("side"), fill.get("price"), best_bid, best_ask,
                    )
                    changed = True
                if age >= _FIVE_MINUTES and "markout_5m_cents" not in fill:
                    fill["markout_5m_cents"] = compute_executable_markout_cents(
                        fill.get("side"), fill.get("price"), best_bid, best_ask,
                    )
                    changed = True
            self._prune_market(market, now)
            self._maybe_persist_locked(now, force=changed)

    def record_trade(
        self,
        slug: str,
        *,
        price: float,
        quantity: float,
        maker_side: str = "",
        trade_time: str = "",
    ) -> None:
        now = time.time()
        maker_side = str(maker_side or "").upper()
        key = f"{trade_time}|{price:.9f}|{quantity:.9f}|{maker_side}"
        with self._lock:
            market = self._market(slug)
            trades = market.setdefault("trades", [])
            if any(item.get("key") == key for item in trades):
                return

            raw_primary = self._shadow_quotes.get(slug, {}).get(
                PRIMARY_STRATEGY
            )
            primary = self._fresh_quote(slug, PRIMARY_STRATEGY, now)
            primary_admissible = bool(
                primary
                and primary.get("depth_ok")
                and primary.get("live_candidate")
            )
            trades.append({
                "key": key,
                "observed_at_epoch": now,
                "trade_time": trade_time,
                "price": float(price),
                "quantity": float(quantity),
                "maker_side": maker_side,
                "primary_quote_admissible": primary_admissible,
                "primary_quote_present": raw_primary is not None,
                "primary_quote_age_seconds": (
                    max(
                        0.0,
                        now - float(raw_primary.get("observed_at_epoch", now)),
                    )
                    if raw_primary is not None else None
                ),
                "primary_quote_age_before_scheduled_refresh_seconds": (
                    raw_primary.get("age_before_scheduled_refresh_seconds")
                    if raw_primary else None
                ),
                "primary_quote_depth_ok": bool(
                    raw_primary and raw_primary.get("depth_ok")
                ),
                "primary_quote_live_candidate": bool(
                    raw_primary and raw_primary.get("live_candidate")
                ),
                "primary_quote_bid": (
                    raw_primary.get("bid") if raw_primary else None
                ),
                "primary_quote_ask": (
                    raw_primary.get("ask") if raw_primary else None
                ),
                "primary_queue_bid": (
                    raw_primary.get("queue_bid") if raw_primary else None
                ),
                "primary_queue_ask": (
                    raw_primary.get("queue_ask") if raw_primary else None
                ),
            })
            market["last_trade_at_epoch"] = now

            for strategy in SHADOW_STRATEGIES:
                quote = self._fresh_quote(slug, strategy, now)
                if quote is None:
                    continue
                if not (
                    quote.get("depth_ok") and quote.get("live_candidate")
                ):
                    # An inactive diagnostic quote was never actually
                    # resting, so its displayed queue must not be depleted
                    # or turned into an inadmissible pseudo-fill.
                    continue
                side = _trade_hits_side(
                    maker_side,
                    float(price),
                    (
                        float(quote["bid"])
                        if quote.get("bid") is not None else None
                    ),
                    (
                        float(quote["ask"])
                        if quote.get("ask") is not None else None
                    ),
                )
                if side is None:
                    continue
                cooldown_key = (slug, strategy, side)
                if now < self._cooldowns.get(cooldown_key, 0.0):
                    continue
                repost_field = (
                    "repost_bid_after" if side == "BUY"
                    else "repost_ask_after"
                )
                if quote.get(repost_field) is not None:
                    # A filled shadow leg cannot magically reappear at the
                    # front of the queue. Wait for a post-cooldown book
                    # snapshot, which resets its displayed queue ahead.
                    continue

                queue_field = "queue_bid" if side == "BUY" else "queue_ask"
                queue_ahead = max(0.0, float(quote.get(queue_field, 0.0)))
                executable = max(0.0, float(quantity) - queue_ahead)
                quote[queue_field] = max(0.0, queue_ahead - float(quantity))
                if executable <= 1e-9:
                    continue

                fill_price = float(quote["bid"] if side == "BUY" else quote["ask"])
                fill_key = f"{key}|{strategy}|{side}|{fill_price:.9f}"
                fills = market.setdefault("hypothetical_fills", [])
                if any(item.get("key") == fill_key for item in fills):
                    continue
                strategy_fills = [
                    item for item in fills
                    if item.get("strategy") == strategy and item.get("admissible")
                ]
                paper_before = _paper_position_state(strategy_fills)
                position_before = float(paper_before["position"])
                reducing = (
                    (side == "SELL" and position_before > 0)
                    or (side == "BUY" and position_before < 0)
                )
                quantity_cap = (
                    abs(position_before)
                    if reducing
                    else max(0.0001, float(self.settings.order_shares_min))
                )
                fill_quantity = min(
                    executable,
                    quantity_cap,
                )
                if fill_quantity <= 1e-9:
                    continue
                fills.append({
                    "key": fill_key,
                    "observed_at_epoch": now,
                    "side": side,
                    "price": fill_price,
                    "quantity": fill_quantity,
                    "trade_price": float(price),
                    "trade_quantity": float(quantity),
                    "strategy": strategy,
                    "depth_ok": bool(quote.get("depth_ok")),
                    "live_candidate": bool(quote.get("live_candidate")),
                    "admissible": bool(
                        quote.get("depth_ok") and quote.get("live_candidate")
                    ),
                    "queue_ahead_before": queue_ahead,
                    "position_before": position_before,
                    "role": "exit" if reducing else "entry",
                    "liquidity_role": "maker",
                    "commission_usd": _estimated_commission(
                        fill_price,
                        fill_quantity,
                        self.settings.observation_maker_fee_theta,
                    ),
                })
                cooldown_until = now + max(
                    1.0,
                    float(self.settings.refresh_interval_seconds),
                )
                self._cooldowns[cooldown_key] = cooldown_until
                quote[
                    "repost_bid_after" if side == "BUY"
                    else "repost_ask_after"
                ] = cooldown_until
                paper_after = _paper_position_state([
                    item for item in fills
                    if item.get("strategy") == strategy and item.get("admissible")
                ])
                if (
                    self.settings.flat_first_inventory_enabled
                    and float(paper_after["position"]) > 0
                ):
                    # Flat-first: after a BUY entry only the reducing SELL
                    # remains available until the shadow inventory is flat.
                    quote["bid"] = None
                    quote["queue_bid"] = 0.0
                elif (
                    self.settings.flat_first_inventory_enabled
                    and float(paper_after["position"]) < 0
                ):
                    quote["ask"] = None
                    quote["queue_ask"] = 0.0

            self._prune_market(market, now)
            self._maybe_persist_locked(now, force=True)

    def _record_due_forced_exits(
        self,
        slug: str,
        market: dict[str, Any],
        bids: list[dict[str, Any]],
        asks: list[dict[str, Any]],
        best_bid: float,
        best_ask: float,
        now: float,
    ) -> bool:
        """Mirror the live bot's mandatory inventory deadline in shadow data.

        Without this, observation measured passive entries but never the
        configured one-hour/near-event IOC exit that determined the real
        strategy's P/L.  Each strategy is derived from its persisted fills,
        so this remains restart-safe and cannot repeatedly flatten inventory
        that is already flat.
        """
        changed = False
        all_fills = market.setdefault("hypothetical_fills", [])
        event_epoch = _positive_float(market.get("event_or_close_epoch"))
        near_event = bool(
            event_epoch is not None
            and event_epoch - now
            <= max(0.0, self.settings.hard_flatten_minutes_before_event) * 60
        )
        for strategy in SHADOW_STRATEGIES:
            strategy_fills = [
                item for item in all_fills
                if item.get("strategy") == strategy and item.get("admissible")
            ]
            state = _paper_position_state(strategy_fills)
            position = float(state["position"])
            opened_at = state["opened_at_epoch"]
            if position == 0 or opened_at is None:
                continue
            max_holding_due = bool(
                self.settings.hard_flatten_on_max_holding_enabled
                and self.settings.liquidation_max_holding_hours > 0
                and now - float(opened_at)
                >= self.settings.liquidation_max_holding_hours * 3600
            )
            if not (near_event or max_holding_due):
                continue

            side = "SELL" if position > 0 else "BUY"
            exit_price = best_bid if side == "SELL" else best_ask
            levels = bids if side == "SELL" else asks
            visible = _quantity_at_price(levels, exit_price)
            quantity = min(abs(position), visible)
            if quantity <= 1e-9:
                continue
            reason = "near_event" if near_event else "max_holding"
            key = (
                f"forced|{strategy}|{side}|{float(opened_at):.6f}|"
                f"{exit_price:.9f}|{quantity:.9f}|{reason}"
            )
            if any(item.get("key") == key for item in all_fills):
                continue
            all_fills.append({
                "key": key,
                "observed_at_epoch": now,
                "side": side,
                "price": exit_price,
                "quantity": quantity,
                "trade_price": exit_price,
                "trade_quantity": visible,
                "strategy": strategy,
                "depth_ok": True,
                "live_candidate": True,
                "admissible": True,
                "queue_ahead_before": 0.0,
                "position_before": position,
                "role": "exit",
                "liquidity_role": "taker",
                "exit_reason": reason,
                "commission_usd": _estimated_commission(
                    exit_price,
                    quantity,
                    self.settings.observation_taker_fee_theta,
                ),
            })
            changed = True
            logger.info(
                "Recorded shadow %s forced exit for %s/%s: side=%s "
                "price=%.4f qty=%.4f.",
                reason, slug, strategy, side, exit_price, quantity,
            )
        return changed

    def entry_eligible(self, slug: str) -> tuple[bool, list[str]]:
        if not self.settings.observation_gate_enabled:
            return (
                not self.settings.observation_only_mode,
                ["observation-only mode is enabled"]
                if self.settings.observation_only_mode else [],
            )
        reasons: list[str] = []
        if self.settings.observation_only_mode:
            reasons.append("observation-only mode is enabled")
        with self._lock:
            market = self._state["markets"].get(slug)
            if not isinstance(market, dict):
                return False, reasons + ["no queue-aware observation history"]
            self._prune_market(market, time.time())
            stats = self._stats(market)

        checks = (
            (
                stats["eligible_observed_seconds"]
                >= self.settings.observation_min_observed_seconds,
                f"eligible observation {stats['eligible_observed_seconds']:.0f}s "
                f"< {self.settings.observation_min_observed_seconds:.0f}s",
            ),
            (
                stats["qualifying_trade_count"] >= self.settings.observation_min_trades,
                f"qualifying trades {stats['qualifying_trade_count']} "
                f"< {self.settings.observation_min_trades}",
            ),
            (
                stats["hypothetical_fill_count"]
                >= self.settings.observation_min_hypothetical_fills,
                f"queue-adjusted fills {stats['hypothetical_fill_count']} "
                f"< {self.settings.observation_min_hypothetical_fills}",
            ),
            (
                stats["hypothetical_fill_rate"]
                >= self.settings.observation_min_fill_rate,
                f"fill rate {stats['hypothetical_fill_rate']:.1%} "
                f"< {self.settings.observation_min_fill_rate:.1%}",
            ),
            (
                stats["distinct_fill_episodes"]
                >= self.settings.observation_min_distinct_fill_episodes,
                f"distinct fill episodes {stats['distinct_fill_episodes']} "
                f"< {self.settings.observation_min_distinct_fill_episodes}",
            ),
            (
                stats["markout_1m_sample_count"]
                >= self.settings.observation_min_markout_samples,
                f"1m markout samples {stats['markout_1m_sample_count']} "
                f"< {self.settings.observation_min_markout_samples}",
            ),
            (
                stats["markout_5m_sample_count"]
                >= self.settings.observation_min_markout_samples,
                f"5m markout samples {stats['markout_5m_sample_count']} "
                f"< {self.settings.observation_min_markout_samples}",
            ),
            (
                stats["avg_markout_1m_cents"] is not None
                and stats["avg_markout_1m_cents"]
                >= self.settings.observation_min_avg_markout_cents,
                f"avg 1m markout {stats['avg_markout_1m_cents']} "
                f"< {self.settings.observation_min_avg_markout_cents:.2f}c",
            ),
            (
                stats["avg_markout_5m_cents"] is not None
                and stats["avg_markout_5m_cents"]
                >= self.settings.observation_min_avg_markout_5m_cents,
                f"avg 5m markout {stats['avg_markout_5m_cents']} "
                f"< {self.settings.observation_min_avg_markout_5m_cents:.2f}c",
            ),
            (
                stats["paper_round_trip_count"]
                >= self.settings.observation_min_paper_round_trips,
                f"paper round trips {stats['paper_round_trip_count']} "
                f"< {self.settings.observation_min_paper_round_trips}",
            ),
            (
                stats["paper_realized_pnl_usd"]
                > self.settings.observation_min_paper_pnl_usd,
                f"paper realized P/L {stats['paper_realized_pnl_usd']:.4f} "
                f"<= {self.settings.observation_min_paper_pnl_usd:.4f}",
            ),
        )
        reasons.extend(reason for passed, reason in checks if not passed)
        return not reasons, reasons

    def report(self) -> list[dict[str, Any]]:
        with self._lock:
            now = time.time()
            rows = []
            for slug, market in self._state["markets"].items():
                self._prune_market(market, now)
                eligible, reasons = self.entry_eligible(slug)
                rows.append({
                    "market_slug": slug,
                    "question": market.get("question", ""),
                    **self._stats(market),
                    "entry_eligible": eligible,
                    "evidence_ready": not [
                        reason for reason in reasons
                        if reason != "observation-only mode is enabled"
                    ],
                    "blocked_reasons": reasons,
                })
            return sorted(
                rows,
                key=lambda row: (
                    row["evidence_ready"],
                    row["paper_realized_pnl_usd"],
                    row["hypothetical_fill_count"],
                    row["qualifying_trade_count"],
                ),
                reverse=True,
            )

    def flush(self) -> None:
        with self._lock:
            self._persist_locked(time.time())

    def _market(self, slug: str) -> dict[str, Any]:
        market = self._state["markets"].setdefault(slug, {})
        market.setdefault("trades", [])
        market.setdefault("hypothetical_fills", [])
        market.setdefault("observed_second_buckets", {})
        market.setdefault("eligible_observed_second_buckets", {})
        return market

    def _fresh_quote(
        self, slug: str, strategy: str, now: float,
    ) -> Optional[dict[str, Any]]:
        quote = self._shadow_quotes.get(slug, {}).get(strategy)
        if quote is None:
            return None
        if now - float(quote.get("observed_at_epoch", 0.0)) > self.settings.websocket_stale_after_seconds:
            return None
        return quote

    def _update_shadow_quotes(
        self,
        slug: str,
        plans: dict[str, tuple[Optional[float], Optional[float]]],
        bids: list[dict[str, Any]],
        asks: list[dict[str, Any]],
        now: float,
        *,
        depth_ok: bool,
        live_candidate: bool,
        managed_strategies: set[str],
    ) -> None:
        states = self._shadow_quotes.setdefault(slug, {})
        for strategy in list(states):
            if strategy not in plans:
                states.pop(strategy, None)
        for strategy, (bid, ask) in plans.items():
            bid_queue = _quantity_at_price(bids, bid)
            ask_queue = _quantity_at_price(asks, ask)
            prior = states.get(strategy)
            if (
                prior is not None
                and _same_optional_price(prior.get("bid"), bid)
                and _same_optional_price(prior.get("ask"), ask)
            ):
                for queue_field, displayed, repost_field in (
                    ("queue_bid", bid_queue, "repost_bid_after"),
                    ("queue_ask", ask_queue, "repost_ask_after"),
                ):
                    repost_after = prior.get(repost_field)
                    if repost_after is not None and now >= float(repost_after):
                        prior[queue_field] = displayed
                        prior.pop(repost_field, None)
                    elif repost_after is None:
                        prior[queue_field] = min(
                            max(0.0, float(prior.get(queue_field, 0.0))),
                            displayed,
                        )
                prior["observed_at_epoch"] = now
                prior["depth_ok"] = depth_ok
                prior["displayed_bid_quantity"] = bid_queue
                prior["displayed_ask_quantity"] = ask_queue
                prior["live_candidate"] = bool(
                    live_candidate or strategy in managed_strategies
                )
            else:
                states[strategy] = {
                    "bid": bid,
                    "ask": ask,
                    "queue_bid": bid_queue,
                    "queue_ask": ask_queue,
                    "displayed_bid_quantity": bid_queue,
                    "displayed_ask_quantity": ask_queue,
                    "created_at_epoch": now,
                    "observed_at_epoch": now,
                    "depth_ok": depth_ok,
                    "live_candidate": bool(
                        live_candidate or strategy in managed_strategies
                    ),
                }

    def _prune_market(self, market: dict[str, Any], now: float) -> None:
        cutoff = now - max(
            1.0,
            self.settings.observation_evidence_window_hours * 3600,
        )
        market["trades"] = [
            item for item in market.get("trades", [])
            if float(item.get("observed_at_epoch", 0)) >= cutoff
        ][-5000:]
        market["hypothetical_fills"] = [
            item for item in market.get("hypothetical_fills", [])
            if float(item.get("observed_at_epoch", 0)) >= cutoff
        ][-5000:]
        for field in (
            "observed_second_buckets",
            "eligible_observed_second_buckets",
        ):
            market[field] = {
                key: value for key, value in market.get(field, {}).items()
                if float(key) >= cutoff
            }

    def _stats(self, market: dict[str, Any]) -> dict[str, Any]:
        trades = list(market.get("trades", []))
        qualifying_trades = [
            item for item in trades if item.get("primary_quote_admissible")
        ]
        fills = [
            item for item in market.get("hypothetical_fills", [])
            if item.get("strategy") == PRIMARY_STRATEGY
            and item.get("admissible")
        ]
        markouts_1m = [
            float(item["markout_1m_cents"])
            for item in fills if item.get("markout_1m_cents") is not None
        ]
        markouts_5m = [
            float(item["markout_5m_cents"])
            for item in fills if item.get("markout_5m_cents") is not None
        ]
        paper = _paper_round_trip_stats(fills)
        variant_stats = {}
        for strategy in SHADOW_STRATEGIES:
            strategy_fills = [
                item for item in market.get("hypothetical_fills", [])
                if item.get("strategy") == strategy and item.get("admissible")
            ]
            variant_stats[strategy] = {
                "fill_count": len(strategy_fills),
                **_paper_round_trip_stats(strategy_fills),
            }
        return {
            "observed_seconds": sum(
                float(value)
                for value in market.get("observed_second_buckets", {}).values()
            ),
            "eligible_observed_seconds": sum(
                float(value)
                for value in market.get(
                    "eligible_observed_second_buckets", {}
                ).values()
            ),
            "trade_count": len(trades),
            "qualifying_trade_count": len(qualifying_trades),
            "traded_shares": sum(
                float(item.get("quantity", 0)) for item in trades
            ),
            "hypothetical_fill_count": len(fills),
            "hypothetical_fill_rate": (
                len(fills) / len(qualifying_trades)
                if qualifying_trades else 0.0
            ),
            "distinct_fill_episodes": _distinct_fill_episodes(
                fills,
                self.settings.observation_fill_episode_gap_seconds,
            ),
            "markout_1m_sample_count": len(markouts_1m),
            "avg_markout_1m_cents": (
                mean(markouts_1m) if markouts_1m else None
            ),
            "markout_5m_sample_count": len(markouts_5m),
            "avg_markout_5m_cents": (
                mean(markouts_5m) if markouts_5m else None
            ),
            **paper,
            "variant_stats": variant_stats,
        }

    def _maybe_persist_locked(self, now: float, *, force: bool = False) -> None:
        if (
            force
            or now - self._last_persist_epoch
            >= self.settings.observation_persist_interval_seconds
        ):
            self._persist_locked(now)

    def _persist_locked(self, now: float) -> None:
        storage.save_json(self.path, self._state)
        self._last_persist_epoch = now


PROFILE_LEGACY = "legacy"
PROFILE_CONTROLLED = "controlled"
PROFILE_JULY5_STYLE = "july5_style"
OBSERVATION_PROFILES = (PROFILE_LEGACY, PROFILE_CONTROLLED, PROFILE_JULY5_STYLE)
QUALIFICATION_PASS = "PASS"
QUALIFICATION_FAIL = "FAIL"
QUALIFICATION_INSUFFICIENT = "INSUFFICIENT"

# Defaults for the cohort-level portion of QualificationPolicy that have no
# existing config field (unlike the portfolio-level observation_controlled_*
# thresholds and observation_cohort_min_round_trips/min_distinct_events,
# which do). Mirror observation_replay.py's own COHORT_MIN_PROFIT_FACTOR /
# COHORT_MAX_DRAWDOWN_USD / COHORT_MAX_SETTLEMENT_EXIT_RATE fallback
# constants -- kept as separate literals rather than a shared import, since
# observation_replay.py already imports from this module and importing the
# other way would be circular. Only used as the INITIAL value frozen into a
# fresh archive's QualificationPolicy; every archive after that reads its
# own persisted policy, so a drift between these two default locations can
# only affect a brand-new archive's starting point, never re-grade already
# -collected evidence.
_POLICY_DEFAULT_COHORT_MIN_PROFIT_FACTOR = 1.20
_POLICY_DEFAULT_COHORT_MAX_DRAWDOWN_USD = 3.0
_POLICY_DEFAULT_MAX_SETTLEMENT_EXIT_RATE = 0.20
_POLICY_DEFAULT_MIN_AVG_MARKOUT_5M_CENTS = 0.0


class ObservationContinuationError(RuntimeError):
    """The fixed observation gate cannot safely be continued as requested."""


class ObservationCoverageCompletionError(RuntimeError):
    """Compatible evidence cannot safely enter healthy-feed completion mode."""


class ObservationSpecMismatchError(RuntimeError):
    """A profile's persisted ObservationProfileSpec no longer matches the
    settings that would be built for it now -- refuses to continue rather
    than silently mixing evidence collected under different parameters."""


@dataclasses.dataclass(frozen=True)
class ObservationProfileSpec:
    """The complete, canonical specification of one shadow profile's
    behavior -- everything that controls what it trades and how, in one
    place, persisted and hashed so runtime dispatch and offline replay can
    never silently disagree about what was actually in effect."""

    profile: str
    order_shares_min: float
    order_shares_max: float
    max_spread: float
    max_started_event_hours: float
    pregame_pause_minutes: float
    entry_cutoff_minutes: float
    hard_flatten_minutes_before_event: float
    hard_flatten_on_max_holding_enabled: bool
    liquidation_max_holding_hours: float
    flat_first_inventory_enabled: bool
    require_both_entry_legs: bool
    max_round_trips_per_market: Optional[int]
    max_markets_per_event: Optional[int]
    ranking_method: str
    refresh_interval_seconds: float
    max_markets_allocated: int
    extreme_price_low_threshold: float
    extreme_price_high_threshold: float
    extreme_price_min_edge_cents: float
    max_payoff_loss_to_capture_ratio: float
    maker_fee_theta: float
    taker_fee_theta: float
    model_revision: int

    def spec_hash(self) -> str:
        payload = json.dumps(dataclasses.asdict(self), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclasses.dataclass(frozen=True)
class QualificationPolicy:
    """The complete set of thresholds evidence is graded against, frozen
    into an archive at creation time so a later change to the live
    module/config constants can never retroactively change the verdict
    already-collected evidence produces."""

    primary_strategy: str
    min_round_trips: int
    min_distinct_events: int
    min_profit_factor: float
    max_drawdown_usd: float
    max_event_profit_concentration: float
    cohort_min_round_trips: int
    cohort_min_distinct_events: int
    cohort_min_profit_factor: float
    cohort_max_drawdown_usd: float
    max_settlement_exit_rate: float
    min_avg_markout_5m_cents: float

    def policy_hash(self) -> str:
        payload = json.dumps(dataclasses.asdict(self), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


class MarketObservationTracker:
    """Schema-v4 portfolio observer.

    Two isolated single-profile engines consume the same L2 snapshots and
    trade tape.  The wrapper owns allocation, lifecycle deadlines, cohort
    aggregation, portfolio accounting, and the fixed 48-hour gate.  The old
    schema-v3 file is intentionally never loaded here: it remains available
    as diagnostic history but is not compatible qualification evidence.
    """

    def __init__(
        self,
        settings: config.LiveTradingSettings,
        path: Optional[Path] = None,
    ):
        self.settings = settings
        self.path = path or OBSERVATION_FILE
        self._lock = threading.RLock()
        self._last_persist_epoch = 0.0
        self._candidate_pool: set[str] = set()
        self._candidate_pool_started_epoch: Optional[float] = None
        self._empty_candidate_pool_started_epoch: Optional[float] = time.time()
        self._active: dict[str, set[str]] = {
            profile: set() for profile in OBSERVATION_PROFILES
        }
        self._last_allocation_epoch: dict[str, float] = {
            profile: 0.0 for profile in OBSERVATION_PROFILES
        }
        # None (the default for every profile) means _refresh_allocations'
        # own ranking picks that profile's active markets as usual. A caller
        # can pin a profile to an exact, ORDERED sequence via
        # override_profile_allocation -- used by the real pilot to keep its
        # shadow-comparison profile's allocation mirroring the real bot's
        # own current candidates each cycle, instead of drifting to whatever
        # unrelated subset the ranking would otherwise pick from the broader
        # shared pool. Deliberately a tuple, not a set: when there are more
        # pinned markets than observation_profile_max_markets, the cap below
        # must take a deterministic slice in the caller's own priority
        # order, not an arbitrary hash-ordered subset.
        self._pinned_allocation: dict[str, Optional[tuple[str, ...]]] = {
            profile: None for profile in OBSERVATION_PROFILES
        }
        loaded = storage.load_json(self.path, default={})
        loaded_version = loaded.get("version") if isinstance(loaded, dict) else None
        loaded_model_revision = (
            loaded.get("model_revision") if isinstance(loaded, dict) else None
        )
        if (
            loaded_version == SCHEMA_VERSION
            and loaded_model_revision == OBSERVATION_MODEL_REVISION
        ):
            self._state = loaded
        else:
            if loaded:
                version = loaded_version if loaded_version is not None else "unknown"
                if isinstance(version, int) and version > SCHEMA_VERSION:
                    raise ValueError(
                        f"Observation schema {version} is newer than supported "
                        f"schema {SCHEMA_VERSION}; refusing to reset evidence."
                    )
                if (
                    version == SCHEMA_VERSION
                    and isinstance(loaded_model_revision, int)
                    and loaded_model_revision > OBSERVATION_MODEL_REVISION
                ):
                    raise ValueError(
                        "Observation model revision "
                        f"{loaded_model_revision} is newer than supported "
                        f"revision {OBSERVATION_MODEL_REVISION}; refusing to "
                        "reset evidence."
                    )
                archive = self.path.with_name(
                    f"{self.path.stem}.schema-{version}-model-"
                    f"{loaded_model_revision or 'legacy'}-diagnostic"
                    f"{self.path.suffix}"
                )
                if archive.exists():
                    archive = self.path.with_name(
                        f"{self.path.stem}.schema-{version}-model-"
                        f"{loaded_model_revision or 'legacy'}-diagnostic-"
                        f"{int(time.time())}{self.path.suffix}"
                    )
                if self.path.exists():
                    shutil.copy2(self.path, archive)
                logger.warning(
                    "Ignoring incompatible market-observation schema %s at %s; "
                    "preserved it at %s and started fresh schema-v4 evidence.",
                    version, self.path, archive,
                )
            now = time.time()
            self._state = {
                "version": SCHEMA_VERSION,
                "schema_minor": 3,
                "model_revision": OBSERVATION_MODEL_REVISION,
                "started_at_epoch": now,
                "original_evaluation_deadline_epoch": (
                    now + max(0.0, settings.observation_evaluation_hours) * 3600
                ),
                "evaluation_deadline_epoch": (
                    now + max(0.0, settings.observation_evaluation_hours) * 3600
                ),
                "coverage_target_seconds": (
                    max(0.0, settings.observation_evaluation_hours) * 3600
                ),
                # Model revision 5+ starts directly in cumulative,
                # restart-safe healthy-feed-hours mode rather than
                # wall-clock mode: the 48-hour budget is confirmed healthy
                # feed minutes, accumulated across however many restarts it
                # takes, never consumed by downtime or a degraded feed. The
                # full observation_evaluation_hours is the target -- not
                # arm_healthy_feed_completion()'s own 90%-scaled target,
                # which serves a different, narrower retroactive-fix
                # purpose for older wall-clock archives. See RUNBOOK 44.
                "evaluation_completion_mode": "healthy_feed_target",
                "evaluation_healthy_feed_target_seconds": (
                    max(0.0, settings.observation_evaluation_hours) * 3600
                ),
                "profiles": {
                    profile: {"markets": {}} for profile in OBSERVATION_PROFILES
                },
                "feed_minute_buckets": {},
                "last_feed_book_epoch": 0.0,
                "last_feed_activity_epoch": 0.0,
                "qualification_config": self._qualification_config(),
            }
        self._migrate_compatible_v4()

        profile_settings = {
            PROFILE_LEGACY: self._legacy_settings(),
            PROFILE_CONTROLLED: self._controlled_settings(),
            PROFILE_JULY5_STYLE: self._july5_settings(),
        }
        self._profile_specs: dict[str, ObservationProfileSpec] = {
            profile: self._build_profile_spec(profile, settings_obj)
            for profile, settings_obj in profile_settings.items()
        }
        self._qualification_policy = self._build_qualification_policy()
        self._enforce_spec_and_policy()

        self._trackers = {
            profile: self._new_profile_tracker(profile, settings_obj)
            for profile, settings_obj in profile_settings.items()
        }

    def _enforce_spec_and_policy(self) -> None:
        """Persist each profile's ObservationProfileSpec and the shared
        QualificationPolicy the first time an archive is created; on every
        later load, verify the specs still match exactly what would be
        built now and refuse to continue on any mismatch (fail-closed) --
        a spec drift would mean evidence collected under different trading
        parameters is about to be silently mixed into one archive. A
        QualificationPolicy drift only affects how evidence is later
        graded, not what was actually traded, so it's logged rather than
        treated as fatal."""
        profiles_state = self._state.setdefault("profiles", {})
        for profile, spec in self._profile_specs.items():
            profile_state = profiles_state.setdefault(profile, {"markets": {}})
            persisted_hash = profile_state.get("spec_hash")
            fresh_hash = spec.spec_hash()
            if persisted_hash is None:
                profile_state["spec"] = dataclasses.asdict(spec)
                profile_state["spec_hash"] = fresh_hash
            elif persisted_hash != fresh_hash:
                raise ObservationSpecMismatchError(
                    f"Profile {profile!r}'s persisted specification no longer "
                    "matches the settings that would be built for it now "
                    f"(archive: {self.path}). Refusing to continue this "
                    "archive with drifted trading parameters -- start a new "
                    "model revision instead if this change is intentional."
                )

        persisted_policy_hash = self._state.get("qualification_policy_hash")
        fresh_policy_hash = self._qualification_policy.policy_hash()
        if persisted_policy_hash is None:
            self._state["qualification_policy"] = dataclasses.asdict(
                self._qualification_policy
            )
            self._state["qualification_policy_hash"] = fresh_policy_hash
        elif persisted_policy_hash != fresh_policy_hash:
            logger.warning(
                "Qualification policy for %s no longer matches the current "
                "defaults; offline replay will keep grading this archive's "
                "evidence against the ORIGINAL persisted policy, not the "
                "current one.",
                self.path,
            )

    def _migrate_compatible_v4(self) -> None:
        """Additive v4 migrations preserve evidence instead of resetting it."""
        try:
            prior_minor = int(self._state.get("schema_minor", 0))
        except (TypeError, ValueError):
            prior_minor = 0
        self._state["schema_minor"] = max(3, prior_minor)
        self._state.setdefault("model_revision", OBSERVATION_MODEL_REVISION)
        self._state.setdefault("started_at_epoch", time.time())
        self._state.setdefault(
            "evaluation_deadline_epoch",
            float(self._state["started_at_epoch"])
            + max(0.0, self.settings.observation_evaluation_hours) * 3600,
        )
        self._state.setdefault(
            "original_evaluation_deadline_epoch",
            float(self._state["evaluation_deadline_epoch"]),
        )
        self._state.setdefault(
            "coverage_target_seconds",
            max(
                0.0,
                float(self._state["original_evaluation_deadline_epoch"])
                - float(self._state["started_at_epoch"]),
            ),
        )
        # V4 windows retain their fixed wall-clock behavior until the explicit
        # one-time coverage-completion command is armed for compatible
        # interrupted evidence.
        self._state.setdefault("evaluation_completion_mode", "wall_clock")
        minimum_coverage = max(
            0.0,
            min(
                1.0,
                float(
                    (self._state.get("qualification_config") or {}).get(
                        "min_feed_coverage_ratio",
                        self.settings.observation_min_feed_coverage_ratio,
                    )
                ),
            ),
        )
        self._state.setdefault(
            "evaluation_healthy_feed_target_seconds",
            float(self._state["coverage_target_seconds"]) * minimum_coverage,
        )
        profiles = self._state.setdefault("profiles", {})
        for profile in OBSERVATION_PROFILES:
            profile_state = profiles.setdefault(profile, {})
            profile_state.setdefault("markets", {})
            profile_state.setdefault("equity_curve", [])
        self._state.setdefault("feed_minute_buckets", {})
        self._state.setdefault("last_feed_book_epoch", 0.0)
        self._state.setdefault(
            "last_feed_activity_epoch",
            float(self._state.get("last_feed_book_epoch") or 0.0),
        )
        qualification = self._state.setdefault(
            "qualification_config", {"legacy": "unknown"},
        )
        if isinstance(qualification, dict) and "legacy" not in qualification:
            # Additive revision-4 migration: every historical book minute is
            # already proof that the socket was alive, so it remains valid
            # under the broader book-or-heartbeat health definition.
            qualification.setdefault(
                "feed_health_basis", "book-or-heartbeat-v1",
            )

    def _qualification_config(self) -> dict[str, Any]:
        return {
            "evaluation_hours": self.settings.observation_evaluation_hours,
            "controlled_max_started_event_hours": (
                self.settings.observation_controlled_max_started_event_hours
            ),
            "controlled_max_spread": (
                self.settings.observation_controlled_max_spread
            ),
            "controlled_order_shares": (
                self.settings.observation_controlled_order_shares
            ),
            "controlled_max_markets_per_event": (
                self.settings.observation_controlled_max_markets_per_event
            ),
            "controlled_max_round_trips_per_market": (
                self.settings.observation_controlled_max_round_trips_per_market
            ),
            "maker_fee_theta": self.settings.observation_maker_fee_theta,
            "taker_fee_theta": self.settings.observation_taker_fee_theta,
            "quote_refresh_seconds": (
                self.settings.observation_profile_refresh_seconds
            ),
            "feed_stale_after_seconds": (
                self.settings.observation_feed_stale_after_seconds
            ),
            "min_feed_coverage_ratio": (
                self.settings.observation_min_feed_coverage_ratio
            ),
            "event_grouping": "event-id-or-slug-bucket-v1",
            "market_listing_order": "created-at-desc-v1",
            "feed_health_basis": "book-or-heartbeat-v1",
            "model_revision": OBSERVATION_MODEL_REVISION,
        }

    def evaluation_complete(self) -> bool:
        with self._lock:
            return self._evaluation_complete_locked(time.time())

    def _evaluation_complete_locked(self, now: float) -> bool:
        if self._state.get("evaluation_completion_mode") == "healthy_feed_target":
            healthy_seconds = len(
                self._state.get("feed_minute_buckets", {})
            ) * 60.0
            target_seconds = max(
                0.0,
                float(
                    self._state.get("evaluation_healthy_feed_target_seconds")
                    or 0.0
                ),
            )
            return healthy_seconds + 1e-9 >= target_seconds
        continuation = self._state.get("observation_continuation") or {}
        if continuation.get("status") == "armed":
            # An armed continuation starts on the first proven market-feed
            # activity, not while the process is stopped or still starting.
            return False
        return now >= float(self._state["evaluation_deadline_epoch"])

    def arm_healthy_feed_completion(self) -> dict[str, Any]:
        """Preserve compatible evidence and finish its missing healthy time.

        Unlike a wall-clock extension, stopped processes and feed outages do
        not consume the remaining allowance. The observer ends at the fixed
        90%-of-48-hours evidence target whether or not the other sample gates
        pass, so this cannot wait indefinitely for a profitable result.
        """
        with self._lock:
            now = time.time()
            if self._state.get("evaluation_completion_mode") == "healthy_feed_target":
                raise ObservationCoverageCompletionError(
                    "This evidence set already uses healthy-feed completion mode."
                )
            prior = self._state.get("observation_coverage_completion")
            if isinstance(prior, dict) and prior:
                raise ObservationCoverageCompletionError(
                    "This evidence set has already used its one-time healthy-feed "
                    "completion."
                )
            summary = self.profile_summary(PROFILE_CONTROLLED)
            if summary["status"] != QUALIFICATION_INSUFFICIENT:
                raise ObservationCoverageCompletionError(
                    "Only an INSUFFICIENT controlled observation may complete "
                    f"missing healthy feed time; status is {summary['status']}."
                )
            finalization = self._state.get("evaluation_finalization") or {}
            if finalization.get("attempted") and not finalization.get("complete"):
                missing = set(finalization.get("missing_book_slugs") or [])
                unresolved = set(
                    finalization.get("unresolved_inventory_slugs") or []
                )
                if missing != unresolved:
                    raise ObservationCoverageCompletionError(
                        "The prior deadline partially finalized shadow inventory; "
                        "resuming that mixed portfolio would invalidate evidence."
                    )

            target_seconds = max(
                0.0,
                float(self._state["coverage_target_seconds"])
                * max(
                    0.0,
                    min(1.0, self.settings.observation_min_feed_coverage_ratio),
                ),
            )
            healthy_seconds = len(
                self._state.get("feed_minute_buckets", {})
            ) * 60.0
            if healthy_seconds + 1e-9 >= target_seconds:
                raise ObservationCoverageCompletionError(
                    "The healthy-feed target is already satisfied; only fresh-book "
                    "finalization remains."
                )
            completion = {
                "status": "active",
                "armed_at_epoch": now,
                "healthy_feed_hours_at_arm": healthy_seconds / 3600,
                "target_healthy_feed_hours": target_seconds / 3600,
                "remaining_healthy_feed_hours_at_arm": (
                    target_seconds - healthy_seconds
                ) / 3600,
            }
            self._state["evaluation_completion_mode"] = "healthy_feed_target"
            self._state["evaluation_healthy_feed_target_seconds"] = target_seconds
            self._state["observation_coverage_completion"] = completion
            self._state.pop("evaluation_completed_at_epoch", None)
            self._state.pop("evaluation_entries_frozen_at_epoch", None)
            self._state.pop("evaluation_finalization", None)
            self._maybe_persist_locked(now, force=True)
            return dict(completion)

    def arm_continuation(self, hours: float = 30.0) -> dict[str, Any]:
        """Arm the one permitted continuation without starting its clock.

        Existing books, fills, queues, inventory, P&L, and healthy-feed
        buckets are retained. The operational deadline is moved only when a
        subsequently started observer receives real market-feed activity.
        """
        with self._lock:
            now = time.time()
            try:
                continuation_hours = float(hours)
            except (TypeError, ValueError) as exc:
                raise ObservationContinuationError(
                    "Continuation hours must be a finite positive number."
                ) from exc
            if (
                not math.isfinite(continuation_hours)
                or continuation_hours <= 0
                or continuation_hours > 72
            ):
                raise ObservationContinuationError(
                    "Continuation hours must be greater than 0 and no more than 72."
                )
            deadline = float(self._state["evaluation_deadline_epoch"])
            if now < deadline:
                raise ObservationContinuationError(
                    "The current observation window has not expired; no continuation "
                    "is needed."
                )
            prior = self._state.get("observation_continuation")
            if isinstance(prior, dict) and prior:
                raise ObservationContinuationError(
                    "This evidence set has already used or armed its one-time "
                    "continuation."
                )

            summary = self.profile_summary(PROFILE_CONTROLLED)
            if summary["status"] != QUALIFICATION_INSUFFICIENT:
                raise ObservationContinuationError(
                    "Only an expired INSUFFICIENT observation may be continued; "
                    f"the controlled status is {summary['status']}."
                )

            # Continuing after a partial liquidation would mix a finalized
            # portfolio with a resumed one. The known outage case is safe:
            # every unresolved inventory slug lacked a book, so no deadline
            # liquidation was applied at all.
            finalization = self._state.get("evaluation_finalization") or {}
            if finalization.get("attempted") and not finalization.get("complete"):
                missing = set(finalization.get("missing_book_slugs") or [])
                unresolved = set(
                    finalization.get("unresolved_inventory_slugs") or []
                )
                if missing != unresolved:
                    raise ObservationContinuationError(
                        "The prior deadline partially finalized shadow inventory; "
                        "continuing that mixed portfolio would invalidate evidence."
                    )

            target_seconds = max(
                0.0, float(self._state.get("coverage_target_seconds") or 0.0)
            )
            healthy_seconds = min(
                target_seconds,
                len(self._state.get("feed_minute_buckets", {})) * 60.0,
            )
            continuation = {
                "status": "armed",
                "armed_at_epoch": now,
                "requested_hours": continuation_hours,
                "prior_deadline_epoch": deadline,
                "healthy_feed_hours_at_arm": healthy_seconds / 3600,
            }
            self._state["observation_continuation"] = continuation
            self._maybe_persist_locked(now, force=True)
            return dict(continuation)

    def activate_armed_continuation(
        self, now: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Start an armed continuation exactly once and preserve its evidence."""
        with self._lock:
            activation_epoch = time.time() if now is None else float(now)
            return self._activate_armed_continuation_locked(activation_epoch)

    def _activate_armed_continuation_locked(
        self, now: float,
    ) -> Optional[dict[str, Any]]:
        continuation = self._state.get("observation_continuation") or {}
        if continuation.get("status") != "armed":
            return None
        hours = float(continuation["requested_hours"])
        new_deadline = now + hours * 3600
        continuation.update({
            "status": "active",
            "activated_at_epoch": now,
            "evaluation_deadline_epoch": new_deadline,
        })
        self._state["observation_continuation"] = continuation
        self._state["evaluation_deadline_epoch"] = new_deadline
        self._state.pop("evaluation_finalization", None)
        self._maybe_persist_locked(now, force=True)
        logger.warning(
            "Activated the one-time %.2f-hour observation continuation after "
            "confirmed market-feed activity; prior evidence and the original "
            "%.2f-hour coverage target are unchanged.",
            hours,
            float(self._state["coverage_target_seconds"]) / 3600,
        )
        return dict(continuation)

    def _new_profile_tracker(
        self,
        profile: str,
        profile_settings: config.LiveTradingSettings,
    ) -> _SingleProfileObservationTracker:
        # Construct without reading or writing a second file.  Both children
        # point directly at their profile's persisted market dictionary.
        child = _SingleProfileObservationTracker.__new__(
            _SingleProfileObservationTracker
        )
        child.settings = profile_settings
        child.path = self.path
        child._lock = self._lock
        child._last_book_epoch = {}
        child._shadow_quotes = {}
        child._cooldowns = {}
        child._live_candidate_slugs = set()
        child._last_persist_epoch = 0.0
        child._state = {
            "version": 3,
            "markets": self._state["profiles"][profile]["markets"],
        }
        # The wrapper is the sole persistence owner.
        child._maybe_persist_locked = lambda *_args, **_kwargs: None
        child._persist_locked = lambda *_args, **_kwargs: None
        return child

    def _legacy_settings(self) -> config.LiveTradingSettings:
        return dataclasses.replace(
            self.settings,
            max_spread=self.settings.observation_legacy_max_spread,
            order_shares_min=self.settings.observation_legacy_order_shares,
            order_shares_max=self.settings.observation_legacy_order_shares,
            refresh_interval_seconds=max(
                1, int(self.settings.observation_profile_refresh_seconds)
            ),
            websocket_stale_after_seconds=max(
                self.settings.websocket_stale_after_seconds,
                self.settings.observation_profile_refresh_seconds * 2,
            ),
            flat_first_inventory_enabled=False,
            require_both_entry_legs=True,
            hard_flatten_minutes_before_event=0.0,
            hard_flatten_on_max_holding_enabled=False,
            pre_event_reduce_only_minutes=0.0,
            max_payoff_loss_to_capture_ratio=1_000_000.0,
            extreme_price_low_threshold=-1.0,
            extreme_price_high_threshold=2.0,
            observation_evidence_window_hours=max(
                self.settings.observation_evidence_window_hours,
                self.settings.observation_evaluation_hours + 1.0,
            ),
        )

    def _controlled_settings(self) -> config.LiveTradingSettings:
        return dataclasses.replace(
            self.settings,
            max_spread=self.settings.observation_controlled_max_spread,
            order_shares_min=self.settings.observation_controlled_order_shares,
            order_shares_max=self.settings.observation_controlled_order_shares,
            refresh_interval_seconds=max(
                1, int(self.settings.observation_profile_refresh_seconds)
            ),
            websocket_stale_after_seconds=max(
                self.settings.websocket_stale_after_seconds,
                self.settings.observation_profile_refresh_seconds * 2,
            ),
            flat_first_inventory_enabled=True,
            require_both_entry_legs=True,
            pre_event_reduce_only_minutes=(
                self.settings.observation_controlled_pregame_pause_minutes
            ),
            hard_flatten_minutes_before_event=0.0,
            hard_flatten_on_max_holding_enabled=True,
            liquidation_max_holding_hours=(
                self.settings.observation_controlled_max_holding_hours
            ),
            observation_evidence_window_hours=max(
                self.settings.observation_evidence_window_hours,
                self.settings.observation_evaluation_hours + 1.0,
            ),
        )

    def _july5_settings(self) -> config.LiveTradingSettings:
        return dataclasses.replace(
            self.settings,
            max_spread=self.settings.observation_july5_max_spread,
            order_shares_min=self.settings.observation_july5_order_shares,
            order_shares_max=self.settings.observation_july5_order_shares,
            refresh_interval_seconds=max(
                1, int(self.settings.observation_profile_refresh_seconds)
            ),
            websocket_stale_after_seconds=max(
                self.settings.websocket_stale_after_seconds,
                self.settings.observation_profile_refresh_seconds * 2,
            ),
            flat_first_inventory_enabled=False,
            require_both_entry_legs=True,
            hard_flatten_minutes_before_event=0.0,
            hard_flatten_on_max_holding_enabled=False,
            pre_event_reduce_only_minutes=0.0,
            # Deliberately NOT overriding extreme_price_low_threshold /
            # extreme_price_high_threshold / extreme_price_min_edge_cents /
            # max_payoff_loss_to_capture_ratio -- this profile exists
            # specifically to test July 5's real spread/size/timing WITH
            # those guards active, unlike _legacy_settings() above, which
            # disables them. See live/RUNBOOK.md 44 and
            # data/reports/july5_old_bot_reconstruction.md.
            observation_evidence_window_hours=max(
                self.settings.observation_evidence_window_hours,
                self.settings.observation_evaluation_hours + 1.0,
            ),
        )

    def _build_profile_spec(
        self, profile: str, profile_settings: config.LiveTradingSettings,
    ) -> ObservationProfileSpec:
        if profile == PROFILE_LEGACY:
            max_started_event_hours = (
                self.settings.observation_legacy_max_started_event_hours
            )
        elif profile == PROFILE_CONTROLLED:
            max_started_event_hours = (
                self.settings.observation_controlled_max_started_event_hours
            )
        elif profile == PROFILE_JULY5_STYLE:
            max_started_event_hours = (
                self.settings.observation_july5_max_started_event_hours
            )
        else:
            raise ValueError(f"unknown observation profile: {profile}")
        return ObservationProfileSpec(
            profile=profile,
            order_shares_min=profile_settings.order_shares_min,
            order_shares_max=profile_settings.order_shares_max,
            max_spread=profile_settings.max_spread,
            max_started_event_hours=max_started_event_hours,
            pregame_pause_minutes=(
                self.settings.observation_controlled_pregame_pause_minutes
                if profile == PROFILE_CONTROLLED else 0.0
            ),
            entry_cutoff_minutes=(
                self.settings.observation_controlled_entry_cutoff_minutes
                if profile == PROFILE_CONTROLLED else 0.0
            ),
            hard_flatten_minutes_before_event=(
                profile_settings.hard_flatten_minutes_before_event
            ),
            hard_flatten_on_max_holding_enabled=(
                profile_settings.hard_flatten_on_max_holding_enabled
            ),
            liquidation_max_holding_hours=(
                profile_settings.liquidation_max_holding_hours
            ),
            flat_first_inventory_enabled=(
                profile_settings.flat_first_inventory_enabled
            ),
            require_both_entry_legs=profile_settings.require_both_entry_legs,
            max_round_trips_per_market=(
                self.settings.observation_controlled_max_round_trips_per_market
                if profile == PROFILE_CONTROLLED else None
            ),
            max_markets_per_event=(
                self.settings.observation_controlled_max_markets_per_event
                if profile == PROFILE_CONTROLLED else None
            ),
            ranking_method=(
                "widest_spread_recent_quantity_weighted"
                if profile == PROFILE_CONTROLLED else "widest_spread_first"
            ),
            refresh_interval_seconds=(
                self.settings.observation_profile_refresh_seconds
            ),
            max_markets_allocated=self.settings.observation_profile_max_markets,
            extreme_price_low_threshold=(
                profile_settings.extreme_price_low_threshold
            ),
            extreme_price_high_threshold=(
                profile_settings.extreme_price_high_threshold
            ),
            extreme_price_min_edge_cents=(
                profile_settings.extreme_price_min_edge_cents
            ),
            max_payoff_loss_to_capture_ratio=(
                profile_settings.max_payoff_loss_to_capture_ratio
            ),
            maker_fee_theta=self.settings.observation_maker_fee_theta,
            taker_fee_theta=self.settings.observation_taker_fee_theta,
            model_revision=OBSERVATION_MODEL_REVISION,
        )

    def _build_qualification_policy(self) -> QualificationPolicy:
        return QualificationPolicy(
            primary_strategy=PRIMARY_STRATEGY,
            min_round_trips=self.settings.observation_controlled_min_round_trips,
            min_distinct_events=(
                self.settings.observation_controlled_min_distinct_events
            ),
            min_profit_factor=(
                self.settings.observation_controlled_min_profit_factor
            ),
            max_drawdown_usd=self.settings.observation_controlled_max_drawdown_usd,
            max_event_profit_concentration=(
                self.settings.observation_controlled_max_event_profit_concentration
            ),
            cohort_min_round_trips=self.settings.observation_cohort_min_round_trips,
            cohort_min_distinct_events=(
                self.settings.observation_cohort_min_distinct_events
            ),
            cohort_min_profit_factor=_POLICY_DEFAULT_COHORT_MIN_PROFIT_FACTOR,
            cohort_max_drawdown_usd=_POLICY_DEFAULT_COHORT_MAX_DRAWDOWN_USD,
            max_settlement_exit_rate=_POLICY_DEFAULT_MAX_SETTLEMENT_EXIT_RATE,
            min_avg_markout_5m_cents=_POLICY_DEFAULT_MIN_AVG_MARKOUT_5M_CENTS,
        )

    def set_live_candidate_slugs(self, slugs: list[str]) -> None:
        """Set the broad observable pool, not a live-trading permission set."""
        with self._lock:
            now = time.time()
            new_pool = {str(slug) for slug in slugs if slug}
            if new_pool and not self._candidate_pool:
                self._candidate_pool_started_epoch = now
                self._empty_candidate_pool_started_epoch = None
            elif not new_pool:
                self._candidate_pool_started_epoch = None
                # Repeated empty refreshes must not postpone the alarm.  Only
                # start a new grace period when a formerly non-empty universe
                # actually becomes empty.
                if self._candidate_pool or self._empty_candidate_pool_started_epoch is None:
                    self._empty_candidate_pool_started_epoch = now
            self._candidate_pool = new_pool
            self._refresh_allocations(now, force=True)

    def override_profile_allocation(self, profile: str, slugs: list[str]) -> None:
        """Pin `profile`'s active-market allocation to exactly `slugs`
        (capped at observation_profile_max_markets), bypassing
        _refresh_allocations' own independent ranking for this profile only
        until the next call. Intended for a real pilot's own dedicated
        tracker instance, to keep its shadow-comparison profile tracking the
        same markets the real bot is actually quoting each cycle -- without
        this, the profile's own ranking can pick an unrelated subset of the
        broader shared candidate pool, so a real fill and a comparable
        shadow fill may never land on the same market. Does not affect any
        other profile, and has no effect on the long-running multi-day
        observation archive unless something explicitly calls it against
        that tracker too."""
        if profile not in OBSERVATION_PROFILES:
            raise ValueError(f"Unknown observation profile: {profile}")
        with self._lock:
            # Order-preserving de-duplication, NOT a set -- when the caller
            # passes more markets than observation_profile_max_markets, the
            # cap below takes a plain slice of this sequence. Collapsing to
            # a set here first would make which markets survive the cap
            # depend on Python's hash-based set iteration order rather than
            # the caller's own priority order, silently selecting different
            # markets than the real maker actually posted to.
            seen: set[str] = set()
            ordered: list[str] = []
            for slug in slugs:
                slug = str(slug) if slug else ""
                if slug and slug not in seen:
                    seen.add(slug)
                    ordered.append(slug)
            self._pinned_allocation[profile] = tuple(ordered)
            # Not force=True: the pin check above is unconditional (ahead of
            # the due-for-refresh gate) for the pinned profile itself, so it
            # takes effect immediately regardless; omitting force leaves
            # every OTHER profile's own refresh schedule completely
            # untouched, matching this method's "no effect on any other
            # profile" contract.
            self._refresh_allocations(time.time())

    def open_inventory_slugs(self) -> set[str]:
        """Return every market with inventory in any shadow strategy.

        These slugs remain WebSocket subscription requirements even after an
        event ages out of the entry universe.  Dropping them would strand the
        simulated inventory and invalidate deadline P/L.
        """
        with self._lock:
            result: set[str] = set()
            for child in self._trackers.values():
                for slug, market in child._state["markets"].items():
                    if self._market_has_shadow_inventory(market):
                        result.add(slug)
            return result

    def feed_health(self, now: Optional[float] = None) -> dict[str, Any]:
        """Describe whether observation has no usable candidate/L2 feed."""
        with self._lock:
            current = time.time() if now is None else float(now)
            limit = max(1.0, self.settings.observation_feed_stale_after_seconds)
            if not self._candidate_pool:
                reference = self._empty_candidate_pool_started_epoch
                if reference is None:
                    reference = current
                    self._empty_candidate_pool_started_epoch = reference
                age = max(0.0, current - reference)
                return {
                    "required": True,
                    "stalled": age > limit,
                    "reason": "empty_candidate_pool",
                    "age_seconds": age,
                    "candidate_count": 0,
                }
            if self._candidate_pool_started_epoch is None:
                self._candidate_pool_started_epoch = current
            latest = float(
                self._state.get("last_feed_activity_epoch")
                or self._state.get("last_feed_book_epoch")
                or 0.0
            )
            reference = self._candidate_pool_started_epoch
            if latest >= self._candidate_pool_started_epoch:
                reference = latest
            age = max(0.0, current - reference)
            return {
                "required": True,
                "stalled": age > limit,
                "reason": "silent_feed",
                "age_seconds": age,
                "candidate_count": len(self._candidate_pool),
                "latest_activity_epoch": latest,
            }

    def record_feed_activity(self) -> None:
        """Record a live market-socket heartbeat or non-error message.

        Polymarket's stream is event-driven: a quiet but healthy order book
        need not change every five minutes.  Heartbeats prove connection
        liveness without pretending that an old L2 snapshot is fresh; actual
        fills and deadline exits retain their separate bounded-book checks.
        """
        now = time.time()
        with self._lock:
            if not self._candidate_pool:
                return
            new_minute = self._record_feed_activity_locked(now)
            if new_minute:
                # One write per new heartbeat minute is enough for crash-safe
                # coverage and avoids serializing a large state every pulse.
                self._maybe_persist_locked(now)

    def register_market(
        self,
        slug: str,
        *,
        tick_size: float,
        question: str = "",
        event_id: str = "",
        event_or_close_epoch: Optional[float] = None,
        sports_market_family: str = "",
        raw: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            family = sports_market_family or _market_family(slug, raw)
            resolved_event_id = event_id or derive_event_bucket_key(slug, raw)
            for child in self._trackers.values():
                child.register_market(
                    slug,
                    tick_size=tick_size,
                    question=question,
                    event_id=resolved_event_id,
                    event_or_close_epoch=event_or_close_epoch,
                )
                market = child._market(slug)
                market["sports_market_family"] = family
                if raw:
                    market["raw"] = dict(raw)

    def record_book(self, slug: str, book: dict[str, Any]) -> None:
        now = time.time()
        bid, ask = _best_prices(book)
        if bid is None or ask is None:
            return
        with self._lock:
            self._record_feed_book_locked(now)
            for profile, child in self._trackers.items():
                market = child._market(slug)
                market["last_best_bid"] = bid
                market["last_best_ask"] = ask
                market["last_mid"] = (bid + ask) / 2
                market["last_spread"] = ask - bid
                market["last_book_epoch"] = now
                market["last_profile_window_eligible"] = self._in_profile_window(
                    profile, market, now,
                )

            self._refresh_allocations(now)
            for profile, child in self._trackers.items():
                market = child._market(slug)
                original_event_epoch = market.get("event_or_close_epoch")
                event_elapsed = self._event_elapsed_seconds(market, now)
                cutoff_hours = self._profile_specs[profile].max_started_event_hours
                if (
                    event_elapsed is not None
                    and event_elapsed >= cutoff_hours * 3600
                ):
                    self._force_exit(
                        profile, slug, book, now, reason="in_play_deadline",
                    )

                active = (
                    slug in self._active[profile]
                    and self._in_profile_window(profile, market, now)
                )
                if (
                    profile == PROFILE_CONTROLLED
                    and self._controlled_round_trip_limit_reached(market)
                ):
                    state = _paper_position_state(self._primary_fills(market))
                    if float(state["position"]) == 0:
                        active = False
                child.set_live_candidate_slugs([slug] if active else [])

                # The schema-v3 state machine treated every negative
                # event-time delta as permanently pregame.  V4 explicitly
                # reopens controlled entries at kickoff, then closes them in
                # the final 30 minutes of the three-hour window. legacy and
                # july5_style both trade in-play immediately with no
                # pregame-entry gating at all, so neither ever masks the
                # true event timestamp -- only controlled does.
                if profile in (PROFILE_LEGACY, PROFILE_JULY5_STYLE):
                    market.pop("event_or_close_epoch", None)
                elif profile == PROFILE_CONTROLLED and self._controlled_entry_open(market, now):
                    market["event_or_close_epoch"] = (
                        now
                        + max(
                            61.0,
                            self.settings.observation_controlled_pregame_pause_minutes
                            * 60
                            + 1.0,
                        )
                    )
                child.record_book(slug, book)
                if (
                    profile == PROFILE_CONTROLLED
                    and self._controlled_round_trip_limit_reached(market)
                    and float(
                        _paper_position_state(self._primary_fills(market))[
                            "position"
                        ]
                    ) == 0
                ):
                    child._shadow_quotes.pop(slug, None)
                if original_event_epoch is None:
                    market.pop("event_or_close_epoch", None)
                else:
                    market["event_or_close_epoch"] = original_event_epoch
                self._tag_profile_fills(profile, slug)
            self._record_equity_points(now)
            self._maybe_persist_locked(now)

    def _record_feed_book_locked(self, now: float) -> None:
        self._record_feed_activity_locked(now)
        self._state["last_feed_book_epoch"] = now

    def _record_feed_activity_locked(self, now: float) -> bool:
        self._activate_armed_continuation_locked(now)
        started = float(self._state["started_at_epoch"])
        deadline = float(self._state["evaluation_deadline_epoch"])
        healthy_mode = (
            self._state.get("evaluation_completion_mode")
            == "healthy_feed_target"
        )
        if now < started or (not healthy_mode and now > deadline):
            return False
        bucket = str(int(now // 60) * 60)
        buckets = self._state.setdefault("feed_minute_buckets", {})
        is_new = bucket not in buckets
        buckets[bucket] = True
        self._state["last_feed_activity_epoch"] = now
        if (
            healthy_mode
            and self._evaluation_complete_locked(now)
            and not self._state.get("evaluation_completed_at_epoch")
        ):
            self._state["evaluation_completed_at_epoch"] = now
            self._freeze_entries_locked(now)
        return is_new

    def record_trade(
        self,
        slug: str,
        *,
        price: float,
        quantity: float,
        maker_side: str = "",
        trade_time: str = "",
    ) -> None:
        now = time.time()
        with self._lock:
            # A real resting order survives quiet periods between book
            # messages. Re-evaluate the 60-second portfolio allocation at
            # tape time and model the scheduled refresh as a keep-in-place
            # when its target price has not changed. Queue-ahead is retained;
            # only liveness/time is renewed.
            self._refresh_allocations(now)
            for profile, child in self._trackers.items():
                self._refresh_quote_liveness_for_trade(
                    profile, child, slug, now,
                )
                child.record_trade(
                    slug,
                    price=price,
                    quantity=quantity,
                    maker_side=maker_side,
                    trade_time=trade_time,
                )
                self._tag_profile_fills(profile, slug)
            self._record_equity_points(now, force=True)
            self._maybe_persist_locked(now, force=True)

    def _refresh_quote_liveness_for_trade(
        self,
        profile: str,
        child: _SingleProfileObservationTracker,
        slug: str,
        now: float,
    ) -> None:
        market = child._market(slug)
        profile_active = (
            slug in self._active[profile]
            and self._in_profile_window(profile, market, now)
        )
        if (
            profile == PROFILE_CONTROLLED
            and not self._controlled_entry_open(market, now)
        ):
            profile_active = False
        if (
            profile == PROFILE_CONTROLLED
            and self._controlled_round_trip_limit_reached(market)
        ):
            profile_active = False

        fills = market.get("hypothetical_fills", [])
        for strategy, quote in child._shadow_quotes.get(slug, {}).items():
            strategy_fills = [
                fill for fill in fills
                if fill.get("strategy") == strategy and fill.get("admissible")
            ]
            held = abs(float(
                _paper_position_state(strategy_fills)["position"]
            )) > 1e-9
            live = profile_active or held
            was_live = bool(quote.get("live_candidate"))
            quote["live_candidate"] = live
            if live:
                if not was_live:
                    # A newly allocated quote joins the currently displayed
                    # queue; it cannot inherit priority from an earlier
                    # inactive diagnostic plan.
                    quote["queue_bid"] = max(
                        0.0,
                        float(quote.get("displayed_bid_quantity", 0.0)),
                    )
                    quote["queue_ask"] = max(
                        0.0,
                        float(quote.get("displayed_ask_quantity", 0.0)),
                    )
                quote["age_before_scheduled_refresh_seconds"] = max(
                    0.0,
                    now - float(quote.get("observed_at_epoch", now)),
                )
                quote["scheduled_refresh_at_epoch"] = now
                quote["observed_at_epoch"] = now

    def _record_equity_points(self, now: float, *, force: bool = False) -> None:
        bucket = int(now // 60) * 60
        for profile in OBSERVATION_PROFILES:
            curve = self._state["profiles"][profile].setdefault(
                "equity_curve", [],
            )
            if (
                curve
                and int(curve[-1].get("bucket_epoch", -1)) == bucket
                and not force
            ):
                continue
            pnl, any_open_market_unpriced = self._profile_total_pnl_locked(profile)
            point = {
                "bucket_epoch": bucket,
                "observed_at_epoch": now,
                "total_pnl_usd": None if any_open_market_unpriced else pnl,
                "valuation_incomplete": any_open_market_unpriced,
            }
            if curve and int(curve[-1].get("bucket_epoch", -1)) == bucket:
                curve[-1] = point
            else:
                curve.append(point)
            del curve[:-5000]

    def _profile_total_pnl_locked(self, profile: str) -> tuple[float, bool]:
        """Returns (pnl, any_open_market_unpriced). `pnl` keeps the lenient
        fallback of treating an unpriced open market as contributing 0.0 to
        open_mtm -- profile_summary()'s existing numeric contract (which
        only reads this element) is unchanged by the None-propagation
        added to _open_inventory_mtm. Callers that need an honest signal
        for an incomplete valuation (_record_equity_points) must inspect
        the second element themselves."""
        child = self._trackers[profile]
        realized = 0.0
        open_mtm = 0.0
        any_open_market_unpriced = False
        for market in child._state["markets"].values():
            fills = self._primary_fills(market)
            realized += float(
                _paper_round_trip_stats(fills)["paper_realized_pnl_usd"]
            )
            if abs(float(_paper_position_state(fills)["position"])) > 1e-9:
                mtm = _open_inventory_mtm(
                    fills,
                    _positive_float(market.get("last_best_bid")),
                    _positive_float(market.get("last_best_ask")),
                    self.settings.observation_taker_fee_theta,
                )
                if mtm is None:
                    any_open_market_unpriced = True
                    mtm = 0.0
                open_mtm += mtm
        return realized + open_mtm, any_open_market_unpriced

    def _refresh_allocations(self, now: float, *, force: bool = False) -> None:
        if self._state.get("evaluation_entries_frozen_at_epoch"):
            self._freeze_entries_locked(now, persist_marker=False)
            return
        for profile, child in self._trackers.items():
            pinned = self._pinned_allocation.get(profile)
            if pinned is not None:
                # pinned is already an order-preserving, de-duplicated tuple
                # (see override_profile_allocation) -- slicing it directly
                # keeps the cap deterministic and faithful to the caller's
                # own priority order, instead of an arbitrary hash-ordered
                # subset that could keep different markets than the real
                # maker actually posted to.
                capped = set(pinned[: self.settings.observation_profile_max_markets])
                self._active[profile] = capped
                self._last_allocation_epoch[profile] = now
                continue
            active_count = len(self._active[profile])
            due = (
                force
                or active_count < self.settings.observation_profile_max_markets
                or now - self._last_allocation_epoch[profile]
                >= self.settings.observation_profile_refresh_seconds
            )
            if not due:
                continue
            ranked = []
            for slug in self._candidate_pool:
                market = child._state["markets"].get(slug)
                if not isinstance(market, dict):
                    continue
                if not self._in_profile_window(profile, market, now):
                    continue
                spread = _positive_float(market.get("last_spread"))
                if spread is None:
                    continue
                ceiling = self._profile_specs[profile].max_spread
                if spread > ceiling + 1e-9:
                    continue
                score = spread
                if profile == PROFILE_CONTROLLED:
                    recent_quantity = sum(
                        float(item.get("quantity", 0.0))
                        for item in market.get("trades", [])
                        if now - float(item.get("observed_at_epoch", 0.0)) <= 300
                    )
                    score *= 1.0 + math.log1p(max(0.0, recent_quantity))
                ranked.append((score, slug, market))
            ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
            selected: list[str] = []
            per_event: dict[str, int] = {}
            for _score, slug, market in ranked:
                if len(selected) >= self.settings.observation_profile_max_markets:
                    break
                if profile == PROFILE_CONTROLLED:
                    event_id = str(market.get("event_id") or f"unknown:{slug}")
                    if (
                        per_event.get(event_id, 0)
                        >= self.settings.observation_controlled_max_markets_per_event
                    ):
                        continue
                    per_event[event_id] = per_event.get(event_id, 0) + 1
                selected.append(slug)
            self._active[profile] = set(selected)
            self._last_allocation_epoch[profile] = now

    def _freeze_entries_locked(
        self, now: float, *, persist_marker: bool = True,
    ) -> None:
        if persist_marker:
            self._state.setdefault("evaluation_entries_frozen_at_epoch", now)
        for profile, child in self._trackers.items():
            self._active[profile] = set()
            child.set_live_candidate_slugs([])
            child._shadow_quotes.clear()

    def _in_profile_window(
        self, profile: str, market: dict[str, Any], now: float,
    ) -> bool:
        elapsed = self._event_elapsed_seconds(market, now)
        if elapsed is None or elapsed < 0:
            return True
        max_hours = self._profile_specs[profile].max_started_event_hours
        return elapsed <= max(0.0, max_hours) * 3600

    @staticmethod
    def _event_elapsed_seconds(
        market: dict[str, Any], now: float,
    ) -> Optional[float]:
        event_epoch = _positive_float(market.get("event_or_close_epoch"))
        return None if event_epoch is None else now - event_epoch

    def _controlled_entry_open(
        self, market: dict[str, Any], now: float,
    ) -> bool:
        event_epoch = _positive_float(market.get("event_or_close_epoch"))
        if event_epoch is None:
            return True
        return controlled_lifecycle(
            event_start_epoch=event_epoch,
            now_epoch=now,
            pregame_pause_minutes=(
                self.settings.observation_controlled_pregame_pause_minutes
            ),
            max_started_event_hours=(
                self.settings.observation_controlled_max_started_event_hours
            ),
            entry_cutoff_minutes=(
                self.settings.observation_controlled_entry_cutoff_minutes
            ),
        ).entry_open

    def _controlled_round_trip_limit_reached(
        self, market: dict[str, Any],
    ) -> bool:
        stats = _paper_round_trip_stats(self._primary_fills(market))
        return int(stats["paper_round_trip_count"]) >= max(
            0, self.settings.observation_controlled_max_round_trips_per_market
        )

    @staticmethod
    def _primary_fills(market: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            fill for fill in market.get("hypothetical_fills", [])
            if fill.get("strategy") == PRIMARY_STRATEGY
            and fill.get("admissible")
        ]

    def session_shadow_markouts(
        self,
        profile: str,
        since_epoch: float,
    ) -> dict[str, Any]:
        """Return primary-strategy 5m markouts created in this pilot session.

        This is deliberately time- and profile-scoped.  A pilot audit must
        never compare today's real fills with a lifetime controlled summary,
        nor count a synthetic settlement close as an observed shadow fill.
        """
        if profile not in OBSERVATION_PROFILES:
            raise ValueError(f"Unknown observation profile: {profile}")
        markouts: list[float] = []
        with self._lock:
            markets = (
                self._state.get("profiles", {})
                .get(profile, {})
                .get("markets", {})
            )
            for market in markets.values():
                for fill in self._primary_fills(market):
                    if fill.get("liquidity_role") == "settlement":
                        continue
                    try:
                        observed_at = float(fill.get("observed_at_epoch") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    if observed_at + 1e-9 < since_epoch:
                        continue
                    value = fill.get("markout_5m_cents")
                    if value is None:
                        continue
                    try:
                        markouts.append(float(value))
                    except (TypeError, ValueError):
                        continue
        return {
            "profile": profile,
            "strategy": PRIMARY_STRATEGY,
            "since_epoch": since_epoch,
            "sample_count": len(markouts),
            "avg_markout_5m_cents": mean(markouts) if markouts else None,
        }

    def session_shadow_drawdown_usd(
        self,
        profile: str,
        since_epoch: float,
    ) -> Optional[dict[str, Any]]:
        """Maximum drawdown measured only over this session's own
        equity-curve points, not profile_summary()'s lifetime archive.

        Baseline is the last curve point at or before since_epoch (not the
        first point after it) -- otherwise a loss between the true session
        start and the first recorded point after it would be missed.
        `incomplete` reflects only that baseline point's own
        valuation_incomplete flag plus any session-window point's flag, not
        every point scanned while searching for the baseline. Returns None
        when there are no points strictly after since_epoch -- a session
        with no equity history yet has no drawdown to report, not a
        fabricated zero.
        """
        if profile not in OBSERVATION_PROFILES:
            raise ValueError(f"Unknown observation profile: {profile}")
        with self._lock:
            curve = (
                self._state.get("profiles", {})
                .get(profile, {})
                .get("equity_curve", [])
            )
            baseline_point: Optional[dict[str, Any]] = None
            session_points: list[dict[str, Any]] = []
            for point in curve:
                if not isinstance(point, dict):
                    continue
                try:
                    bucket_epoch = float(point.get("bucket_epoch", 0.0))
                except (TypeError, ValueError):
                    continue
                if bucket_epoch <= since_epoch:
                    baseline_point = point
                else:
                    session_points.append(point)
        if not session_points:
            return None
        incomplete = bool(
            baseline_point and baseline_point.get("valuation_incomplete")
        ) or any(point.get("valuation_incomplete") for point in session_points)
        baseline_value = baseline_point.get("total_pnl_usd") if baseline_point else 0.0
        raw_values = [baseline_value] + [
            point.get("total_pnl_usd") for point in session_points
        ]
        numeric_values = [float(v) for v in raw_values if v is not None]
        if not numeric_values:
            return {
                "profile": profile,
                "since_epoch": since_epoch,
                "drawdown_usd": None,
                "incomplete": True,
                "sample_count": len(session_points),
            }
        peak = numeric_values[0]
        drawdown = 0.0
        for value in numeric_values:
            peak = max(peak, value)
            drawdown = min(drawdown, value - peak)
        return {
            "profile": profile,
            "since_epoch": since_epoch,
            "drawdown_usd": drawdown,
            "incomplete": incomplete,
            "sample_count": len(session_points),
        }

    @staticmethod
    def _market_has_shadow_inventory(market: dict[str, Any]) -> bool:
        fills = market.get("hypothetical_fills", [])
        for strategy in SHADOW_STRATEGIES:
            strategy_fills = [
                fill for fill in fills
                if fill.get("strategy") == strategy and fill.get("admissible")
            ]
            if abs(float(
                _paper_position_state(strategy_fills)["position"]
            )) > 1e-9:
                return True
        return False

    def _sweep_inventory_to_flat_locked(
        self,
        book_provider: Callable[[str], Optional[dict[str, Any]]],
        *,
        max_book_attempts: int,
        retry_seconds: float,
        reason: str,
        now: float,
    ) -> dict[str, Any]:
        """Sweep every open shadow position to flat via executable-price
        _force_exit(). Must be called with self._lock already held. Shared
        by finalize_evaluation() (gated on the archive's own qualification
        deadline) and finalize_dry_run_evaluation() (gated on the dry-run's
        own phase instead) -- both need the identical per-slug sweep,
        triggered by different conditions."""
        inventory_slugs = sorted(self.open_inventory_slugs())
        missing_books: list[str] = []
        book_lookup_attempts: dict[str, int] = {}
        for slug in inventory_slugs:
            book: Optional[dict[str, Any]] = None
            bid: Optional[float] = None
            ask: Optional[float] = None
            attempts = max(1, int(max_book_attempts))
            for attempt in range(1, attempts + 1):
                book_lookup_attempts[slug] = attempt
                try:
                    book = book_provider(slug)
                except Exception as exc:  # noqa: BLE001 -- bounded retry, then persisted diagnostic
                    logger.warning(
                        "Could not fetch deadline shadow-exit book for %s "
                        "(attempt %d/%d): %s",
                        slug, attempt, attempts, exc,
                    )
                    book = None
                bid, ask = _best_prices(book or {})
                if bid is not None and ask is not None:
                    break
                if attempt < attempts and retry_seconds > 0:
                    time.sleep(retry_seconds)
            if bid is None or ask is None:
                missing_books.append(slug)
                continue
            for profile in OBSERVATION_PROFILES:
                market = self._trackers[profile]._state["markets"].get(slug)
                if not isinstance(market, dict) or not self._market_has_shadow_inventory(market):
                    continue
                self._force_exit(
                    profile,
                    slug,
                    book or {},
                    now,
                    reason=reason,
                )
                self._tag_profile_fills(profile, slug)

        unresolved = sorted(self.open_inventory_slugs())
        return {
            "attempted": True,
            "attempted_at_epoch": now,
            "complete": not unresolved,
            "missing_book_slugs": missing_books,
            "unresolved_inventory_slugs": unresolved,
            "book_lookup_attempts": book_lookup_attempts,
        }

    def finalize_evaluation(
        self,
        book_provider: Callable[[str], Optional[dict[str, Any]]],
        *,
        max_book_attempts: int = 3,
        retry_seconds: float = 1.0,
    ) -> dict[str, Any]:
        """Sweep every shadow position at the fixed evaluation deadline.

        This is called by the observation runner immediately before it
        computes the final gate.  Missing books are persisted explicitly so
        an interrupted feed becomes INSUFFICIENT evidence rather than a
        strategy loss or an apparently clean PASS.
        """
        with self._lock:
            now = time.time()
            if not self._evaluation_complete_locked(now):
                return {
                    "attempted": False,
                    "complete": False,
                    "missing_book_slugs": [],
                    "unresolved_inventory_slugs": sorted(
                        self.open_inventory_slugs()
                    ),
                }

            finalization_epoch = now
            result = self._sweep_inventory_to_flat_locked(
                book_provider,
                max_book_attempts=max_book_attempts,
                retry_seconds=retry_seconds,
                reason="evaluation_deadline",
                now=finalization_epoch,
            )
            self._state["evaluation_finalization"] = result
            continuation = self._state.get("observation_continuation") or {}
            if continuation.get("status") == "active":
                continuation.update({
                    "status": (
                        "completed" if result["complete"]
                        else "ended_finalization_incomplete"
                    ),
                    "ended_at_epoch": now,
                })
                self._state["observation_continuation"] = continuation
            coverage_completion = (
                self._state.get("observation_coverage_completion") or {}
            )
            if coverage_completion.get("status") == "active":
                coverage_completion.update({
                    "status": (
                        "completed" if result["complete"]
                        else "ended_finalization_incomplete"
                    ),
                    "ended_at_epoch": now,
                })
                self._state["observation_coverage_completion"] = (
                    coverage_completion
                )
            self._record_equity_points(finalization_epoch, force=True)
            self._maybe_persist_locked(now, force=True)
            return result

    # ------------------------------------------------------------------
    # Dry-run lifecycle (COLLECTING -> GRACE -> FINALIZING -> COMPLETE).
    # Deliberately independent of the archive's own qualification deadline
    # (_evaluation_complete_locked/evaluation_deadline_epoch) -- a dry-run
    # must be able to finalize as soon as its own evidence target or
    # deadline is reached, which finalize_evaluation() above cannot do
    # (it refuses to run before the archive's own deadline).
    # ------------------------------------------------------------------

    def dry_run_phase(self) -> str:
        with self._lock:
            return self._state.get("dry_run_phase", DRY_RUN_PHASE_COLLECTING)

    def dry_run_grace_deadline_epoch(self) -> Optional[float]:
        with self._lock:
            value = self._state.get("dry_run_grace_deadline_epoch")
            return float(value) if value is not None else None

    def advance_dry_run_to_grace(self, now: float, grace_seconds: float) -> bool:
        """Freezes new shadow entries and transitions COLLECTING -> GRACE.

        Reuses _freeze_entries_locked(), the same mechanism the archive's
        own evaluation deadline uses: once evaluation_entries_frozen_at_
        epoch is set, _refresh_allocations() keeps re-enforcing it on every
        subsequent record_book()/record_trade() call -- including after a
        restart, since it's persisted state -- so entries stay frozen with
        no further intervention needed here. Forward-only/idempotent: a
        call once GRACE has already been entered is a no-op.
        """
        with self._lock:
            if self.dry_run_phase() != DRY_RUN_PHASE_COLLECTING:
                return False
            self._freeze_entries_locked(now)
            self._state["dry_run_phase"] = DRY_RUN_PHASE_GRACE
            self._state["dry_run_grace_deadline_epoch"] = now + max(0.0, grace_seconds)
            self._maybe_persist_locked(now, force=True)
            return True

    def advance_dry_run_to_finalizing(self, now: float) -> bool:
        """Forward-only/idempotent: a call once FINALIZING (or later) has
        already been entered is a no-op, so a restart mid-GRACE with an
        already-passed deadline can safely call this every cycle."""
        with self._lock:
            if self.dry_run_phase() != DRY_RUN_PHASE_GRACE:
                return False
            self._state["dry_run_phase"] = DRY_RUN_PHASE_FINALIZING
            self._state["dry_run_finalizing_started_epoch"] = now
            self._maybe_persist_locked(now, force=True)
            return True

    def dry_run_finalizing_started_epoch(self) -> Optional[float]:
        with self._lock:
            value = self._state.get("dry_run_finalizing_started_epoch")
            return float(value) if value is not None else None

    def finalize_dry_run_evaluation(
        self,
        book_provider: Callable[[str], Optional[dict[str, Any]]],
        *,
        max_book_attempts: int = 3,
        retry_seconds: float = 1.0,
    ) -> dict[str, Any]:
        """Sweep every open shadow position to flat, independent of the
        archive's own qualification deadline. Safe to call repeatedly (a
        restart mid-FINALIZING just re-sweeps) -- _force_exit()'s
        deterministic per-fill key means an already-flat slug is a no-op.
        Idempotent once a verdict has been recorded: returns immediately
        without re-sweeping."""
        with self._lock:
            existing_verdict = self._state.get("dry_run_verdict")
            if existing_verdict is not None:
                return {
                    "attempted": False,
                    "complete": True,
                    "already_finalized": True,
                    "verdict": existing_verdict,
                }
            now = time.time()
            result = self._sweep_inventory_to_flat_locked(
                book_provider,
                max_book_attempts=max_book_attempts,
                retry_seconds=retry_seconds,
                reason="dry_run_deadline",
                now=now,
            )
            self._state["dry_run_finalization"] = result
            self._record_equity_points(now, force=True)
            self._maybe_persist_locked(now, force=True)
            return result

    def complete_dry_run(self, verdict: dict[str, Any], now: float) -> bool:
        """Records the final verdict and transitions FINALIZING -> COMPLETE
        in the same persisted write, so the two can never desync across a
        crash. Idempotent: a verdict, once recorded, is never overwritten
        or recomputed by a repeated call."""
        with self._lock:
            if self.dry_run_phase() != DRY_RUN_PHASE_FINALIZING:
                return False
            if self._state.get("dry_run_verdict") is not None:
                return False
            self._state["dry_run_verdict"] = verdict
            self._state["dry_run_phase"] = DRY_RUN_PHASE_COMPLETE
            self._maybe_persist_locked(now, force=True)
            return True

    def record_dry_run_snapshot(self, snapshot: dict[str, Any], now: float) -> None:
        """The one canonical progress/verdict snapshot a running dry-run
        persists once per cycle -- live-shadow-dryrun-status reads this
        verbatim rather than recomputing verdict logic itself."""
        with self._lock:
            self._state["dry_run_snapshot"] = snapshot
            self._maybe_persist_locked(now, force=True)

    def _force_exit(
        self,
        profile: str,
        slug: str,
        book: dict[str, Any],
        now: float,
        *,
        reason: str,
    ) -> None:
        child = self._trackers[profile]
        market = child._market(slug)
        bids = list(book.get("bids") or [])
        asks = list(book.get("asks") or [])
        best_bid, best_ask = _best_prices(book)
        if best_bid is None or best_ask is None:
            return
        all_fills = market.setdefault("hypothetical_fills", [])
        for strategy in SHADOW_STRATEGIES:
            strategy_fills = [
                item for item in all_fills
                if item.get("strategy") == strategy and item.get("admissible")
            ]
            state = _paper_position_state(strategy_fills)
            position = float(state["position"])
            if abs(position) <= 1e-9:
                continue
            side = "SELL" if position > 0 else "BUY"
            levels = bids if side == "SELL" else asks
            parsed_levels = []
            for level in levels:
                price = _level_float(level, "price")
                quantity = _level_float(level, "quantity")
                if (
                    price is not None and 0 < price < 1
                    and quantity is not None and quantity > 0
                ):
                    parsed_levels.append((price, quantity))
            parsed_levels.sort(key=lambda item: item[0], reverse=side == "SELL")
            remaining = abs(position)
            position_before = position
            for exit_price, visible in parsed_levels:
                exit_quantity = min(remaining, visible)
                if exit_quantity <= 1e-9:
                    continue
                key = (
                    f"forced|{profile}|{strategy}|{side}|{exit_price:.9f}|"
                    f"{exit_quantity:.9f}|{position_before:.9f}|{reason}"
                )
                if any(item.get("key") == key for item in all_fills):
                    continue
                all_fills.append({
                    "key": key,
                    "observed_at_epoch": now,
                    "side": side,
                    "price": exit_price,
                    "quantity": exit_quantity,
                    "trade_price": exit_price,
                    "trade_quantity": visible,
                    "strategy": strategy,
                    "depth_ok": True,
                    "live_candidate": True,
                    "admissible": True,
                    "queue_ahead_before": 0.0,
                    "position_before": position_before,
                    "role": "exit",
                    "liquidity_role": "taker",
                    "exit_reason": reason,
                    "commission_usd": _estimated_commission(
                        exit_price,
                        exit_quantity,
                        self.settings.observation_taker_fee_theta,
                    ),
                })
                remaining -= exit_quantity
                position_before += exit_quantity if side == "BUY" else -exit_quantity
                if remaining <= 1e-9:
                    break

    def _settle_at_resolution(
        self,
        profile: str,
        slug: str,
        settlement_price: float,
        now: float,
        *,
        retrieved_at_epoch: float,
        metadata_synced_at: Optional[str],
    ) -> list[dict[str, Any]]:
        """Close every SHADOW_STRATEGIES position remaining on this slug in
        this profile at the market's actual settlement payout -- used only
        when a market has genuinely resolved and _force_exit's book-based
        approach can never work again (a resolved market has no book).
        Unlike _force_exit there's no depth to walk: the whole remaining
        position closes in one synthetic fill per strategy, since the
        settlement price applies to the full size at once.

        commission_usd=0.0 is a deliberate, documented assumption, not an
        oversight: no order executes at settlement under the published fee
        model (fees apply to executed trades; settlement is a resolution
        payout). retrieved_at_epoch/metadata_synced_at are both recorded
        for provenance, honestly labeled -- metadata_synced_at is whatever
        the API's own updatedAt/ep3SyncedAt field says, never asserted to
        mean "this is when the market resolved" since that isn't
        documented.

        Returns one summary dict per strategy actually settled (empty if
        this slug/profile was already flat), for the caller's audit
        record. Idempotent: a strategy already at zero position is
        skipped, and the fill key is stable, so calling this again for an
        already-settled slug is a safe no-op."""
        child = self._trackers[profile]
        market = child._market(slug)
        all_fills = market.setdefault("hypothetical_fills", [])
        settled: list[dict[str, Any]] = []
        for strategy in SHADOW_STRATEGIES:
            strategy_fills = [
                item for item in all_fills
                if item.get("strategy") == strategy and item.get("admissible")
            ]
            state = _paper_position_state(strategy_fills)
            position = float(state["position"])
            if abs(position) <= 1e-9:
                continue
            side = "SELL" if position > 0 else "BUY"
            quantity = abs(position)
            key = f"settlement|{profile}|{strategy}|{slug}|{settlement_price:.9f}"
            if any(item.get("key") == key for item in all_fills):
                continue
            all_fills.append({
                "key": key,
                "observed_at_epoch": now,
                "side": side,
                "price": settlement_price,
                "quantity": quantity,
                "strategy": strategy,
                "depth_ok": True,
                "live_candidate": True,
                "admissible": True,
                "queue_ahead_before": 0.0,
                "position_before": position,
                "role": "exit",
                "liquidity_role": "settlement",
                "closure_type": "settlement",
                "exit_reason": "market_resolved",
                "commission_usd": 0.0,
                "settlement_retrieved_at_epoch": retrieved_at_epoch,
                "settlement_metadata_synced_at": metadata_synced_at,
            })
            settled.append({
                "profile": profile,
                "slug": slug,
                "strategy": strategy,
                "side": side,
                "quantity": quantity,
                "settlement_price": settlement_price,
            })
        return settled

    def _strategy_round_trips(self, profile: str, strategy: str) -> list[dict[str, Any]]:
        """Same FIFO round-trip accounting profile_summary() already uses
        for the primary strategy (_profile_round_trips), generalized to any
        SHADOW_STRATEGIES member -- needed only for the settlement audit's
        per-strategy P&L breakdown, since the main report stays scoped to
        the primary strategy exactly as before."""
        trips: list[dict[str, Any]] = []
        markets = self._state["profiles"][profile]["markets"]
        for slug, market in markets.items():
            fills = [
                fill for fill in market.get("hypothetical_fills", [])
                if fill.get("strategy") == strategy and fill.get("admissible")
            ]
            trips.extend(
                _round_trip_records(
                    fills,
                    slug=slug,
                    event_id=str(market.get("event_id") or f"unknown:{slug}"),
                )
            )
        return sorted(trips, key=lambda item: item["closed_at_epoch"])

    def apply_settlement_batch(
        self, lookup_results: list[dict[str, Any]], now: float,
    ) -> dict[str, Any]:
        """Apply every SETTLED lookup result (see classify_settlement_lookup)
        to the corresponding shadow positions, in every profile, and
        recompute finalization bookkeeping. Does no network I/O and never
        persists to disk itself (no _maybe_persist_locked/_persist_locked
        call) -- callers must have already resolved every stuck slug and
        confirmed none came back ERROR, and are responsible for deciding
        whether/how to persist the result (this is what makes a pure,
        side-effect-free dry-run preview possible: call this against an
        in-memory tracker and just don't persist)."""
        with self._lock:
            settled_events: list[dict[str, Any]] = []
            touched_profile_strategies: set[tuple[str, str]] = set()
            for result in lookup_results:
                if result.get("status") != "SETTLED":
                    continue
                slug = result["slug"]
                price = float(result["settlement_price"])
                for profile in OBSERVATION_PROFILES:
                    settled = self._settle_at_resolution(
                        profile, slug, price, now,
                        retrieved_at_epoch=float(result["retrieved_at_epoch"]),
                        metadata_synced_at=result.get("metadata_synced_at"),
                    )
                    if not settled:
                        continue
                    self._tag_profile_fills(profile, slug)
                    settled_events.extend(settled)
                    for item in settled:
                        touched_profile_strategies.add((item["profile"], item["strategy"]))

            breakdown = []
            for profile, strategy in sorted(touched_profile_strategies):
                trips = self._strategy_round_trips(profile, strategy)
                settlement_trips = [
                    trip for trip in trips if trip.get("closure_type") == "settlement"
                ]
                breakdown.append({
                    "profile": profile,
                    "strategy": strategy,
                    "settlement_exit_count": len(settlement_trips),
                    "settlement_pnl_usd": sum(
                        float(trip["pnl_usd"]) for trip in settlement_trips
                    ),
                })

            # A slug only leaves the blocker lists once every profile's
            # shadow inventory for it is genuinely flat -- re-derived from
            # actual current state, not assumed from which slugs this
            # batch attempted.
            remaining = sorted(self.open_inventory_slugs())
            complete = not remaining
            prior_finalization = dict(self._state.get("evaluation_finalization") or {})
            # Key presence, not truthiness -- once this is set (even to an
            # empty list, meaning finalize_evaluation() never found any
            # missing books in the first place), it must stay put on every
            # later call. `or` here would wrongly treat that legitimate
            # empty list as "not set" and re-derive it from the CURRENT
            # missing_book_slugs on the next call, drifting the "original"
            # value every time this runs.
            if "original_missing_book_slugs" in prior_finalization:
                original_missing = prior_finalization["original_missing_book_slugs"]
            else:
                original_missing = prior_finalization.get("missing_book_slugs") or []
            finalization = dict(prior_finalization)
            finalization["original_missing_book_slugs"] = original_missing
            finalization["missing_book_slugs"] = remaining
            finalization["unresolved_inventory_slugs"] = remaining
            finalization["complete"] = complete
            finalization["settlement_pass"] = {
                "attempted_at_epoch": now,
                "lookups": lookup_results,
                "settled_events": settled_events,
                "breakdown_by_profile_strategy": breakdown,
            }
            self._state["evaluation_finalization"] = finalization

            if complete:
                coverage = dict(self._state.get("observation_coverage_completion") or {})
                if coverage:
                    coverage["status"] = "completed"
                    coverage["ended_at_epoch"] = now
                    self._state["observation_coverage_completion"] = coverage

            if settled_events:
                self._record_equity_points(now, force=True)

            return {
                "settled_slugs": sorted({event["slug"] for event in settled_events}),
                "remaining_unresolved_slugs": remaining,
                "complete": complete,
                "breakdown_by_profile_strategy": breakdown,
            }

    def _tag_profile_fills(self, profile: str, slug: str) -> None:
        market = self._trackers[profile]._market(slug)
        fills = sorted(
            market.get("hypothetical_fills", []),
            key=lambda item: float(item.get("observed_at_epoch", 0.0)),
        )
        active_cohort: Optional[str] = None
        position = 0.0
        for fill in fills:
            fill["profile"] = profile
            side = fill.get("side")
            quantity = max(0.0, float(fill.get("quantity", 0.0)))
            signed = quantity if side == "BUY" else -quantity
            if abs(position) <= 1e-9 or position * signed > 0:
                if abs(position) <= 1e-9:
                    active_cohort = fill.get("cohort_key") or self._cohort_key(
                        profile, market, fill,
                    )
                fill.setdefault("cohort_key", active_cohort)
            else:
                fill.setdefault(
                    "cohort_key",
                    active_cohort or self._cohort_key(profile, market, fill),
                )
            prior = position
            position += signed
            if abs(position) <= 1e-9:
                position = 0.0
                active_cohort = None
            elif prior != 0 and prior * position < 0:
                active_cohort = self._cohort_key(profile, market, fill)

    def _cohort_key(
        self,
        profile: str,
        market: dict[str, Any],
        fill: dict[str, Any],
    ) -> str:
        timestamp = float(fill.get("observed_at_epoch", time.time()))
        event_epoch = _positive_float(market.get("event_or_close_epoch"))
        phase = (
            "in_play"
            if event_epoch is not None and timestamp >= event_epoch
            else "pregame"
        )
        spread = float(market.get("last_spread") or 0.0)
        if spread <= 0.10:
            spread_band = "0-10c"
        elif spread <= 0.25:
            spread_band = "10-25c"
        elif spread <= 0.50:
            spread_band = "25-50c"
        else:
            spread_band = "50c+"
        price = float(fill.get("price") or 0.0)
        price_band = "extreme" if price <= 0.10 or price >= 0.90 else "normal"
        family = str(market.get("sports_market_family") or "unknown")
        return "|".join((profile, family, phase, spread_band, price_band))

    def _profile_round_trips(self, profile: str) -> list[dict[str, Any]]:
        trips: list[dict[str, Any]] = []
        markets = self._state["profiles"][profile]["markets"]
        for slug, market in markets.items():
            fills = self._primary_fills(market)
            trips.extend(
                _round_trip_records(
                    fills,
                    slug=slug,
                    event_id=str(market.get("event_id") or f"unknown:{slug}"),
                )
            )
        return sorted(trips, key=lambda item: item["closed_at_epoch"])

    def profile_summary(
        self, profile: str = PROFILE_CONTROLLED,
    ) -> dict[str, Any]:
        if profile not in OBSERVATION_PROFILES:
            raise ValueError(f"unknown observation profile: {profile}")
        with self._lock:
            now = time.time()
            child = self._trackers[profile]
            trips = self._profile_round_trips(profile)
            realized = sum(float(item["pnl_usd"]) for item in trips)
            open_inventory = []
            open_mtm = 0.0
            forced_exits = 0
            open_shadow_strategy_positions = 0
            fills_for_markout = []
            all_trades: list[dict[str, Any]] = []
            total_book_samples = 0
            total_observed_seconds = 0.0
            total_eligible_observed_seconds = 0.0
            latest_observation_epoch = 0.0
            for slug, market in child._state["markets"].items():
                all_trades.extend(market.get("trades", []))
                total_book_samples += int(market.get("book_sample_count", 0))
                total_observed_seconds += sum(
                    float(value) for value in market.get(
                        "observed_second_buckets", {}
                    ).values()
                )
                total_eligible_observed_seconds += sum(
                    float(value) for value in market.get(
                        "eligible_observed_second_buckets", {}
                    ).values()
                )
                latest_observation_epoch = max(
                    latest_observation_epoch,
                    float(market.get("last_observed_at_epoch") or 0.0),
                    float(market.get("last_book_epoch") or 0.0),
                )
                fills = self._primary_fills(market)
                # Settlement fills are excluded from trade-fill/markout
                # sample sizes below -- they're an authoritative accounting
                # close, not a market-activity-driven fill, and have no
                # meaningful markout (no future mid-price exists once a
                # market has resolved). Round-trip/P&L accounting (trips,
                # below) still includes them in full.
                fills_for_markout.extend(
                    fill for fill in fills if fill.get("liquidity_role") != "settlement"
                )
                forced_exits += sum(
                    1 for fill in fills
                    if fill.get("exit_reason") and fill.get("closure_type") != "settlement"
                )
                for strategy in SHADOW_STRATEGIES:
                    strategy_fills = [
                        fill for fill in market.get("hypothetical_fills", [])
                        if fill.get("strategy") == strategy and fill.get("admissible")
                    ]
                    if abs(float(
                        _paper_position_state(strategy_fills)["position"]
                    )) > 1e-9:
                        open_shadow_strategy_positions += 1
                state = _paper_position_state(fills)
                position = float(state["position"])
                if abs(position) <= 1e-9:
                    continue
                best_bid = _positive_float(market.get("last_best_bid"))
                best_ask = _positive_float(market.get("last_best_ask"))
                mark_price = best_bid if position > 0 else best_ask
                mtm = _open_inventory_mtm(
                    fills, best_bid, best_ask,
                    self.settings.observation_taker_fee_theta,
                )
                if mtm is None:
                    mtm = 0.0
                open_mtm += mtm
                open_inventory.append({
                    "market_slug": slug,
                    "event_id": str(market.get("event_id") or f"unknown:{slug}"),
                    "shares": position,
                    "average_price": state["average_price"],
                    "mark_price": mark_price,
                    "mark_to_market_pnl_usd": mtm,
                    "opened_at_epoch": state["opened_at_epoch"],
                })
            profits = [float(item["pnl_usd"]) for item in trips if item["pnl_usd"] > 0]
            losses = [-float(item["pnl_usd"]) for item in trips if item["pnl_usd"] < 0]
            gross_profit = sum(profits)
            gross_loss = sum(losses)
            profit_factor = (
                gross_profit / gross_loss
                if gross_loss > 1e-12 else (
                    math.inf if gross_profit > 0 else 0.0
                )
            )
            event_profit: dict[str, float] = {}
            for trip in trips:
                if trip["pnl_usd"] > 0:
                    event_profit[trip["event_id"]] = (
                        event_profit.get(trip["event_id"], 0.0)
                        + float(trip["pnl_usd"])
                    )
            concentration = (
                max(event_profit.values()) / gross_profit
                if gross_profit > 1e-12 and event_profit else 0.0
            )
            markouts_5m = [
                float(fill["markout_5m_cents"])
                for fill in fills_for_markout
                if fill.get("markout_5m_cents") is not None
            ]
            total_pnl = realized + open_mtm
            drawdown = _maximum_drawdown_from_curve(
                self._state["profiles"][profile].get("equity_curve", []),
                total_pnl,
            )
            forced_exit_pnl = sum(
                float(item["pnl_usd"])
                for item in trips if item.get("forced_exit")
            )
            settlement_trips = [
                item for item in trips if item.get("closure_type") == "settlement"
            ]
            settlement_exit_count = len(settlement_trips)
            settlement_pnl_usd = sum(
                float(item["pnl_usd"]) for item in settlement_trips
            )
            cohorts = self._cohort_rows(profile, trips, fills_for_markout)
            started = float(self._state["started_at_epoch"])
            deadline = float(self._state["evaluation_deadline_epoch"])
            expected_feed_seconds = max(
                0.0,
                float(
                    self._state.get("coverage_target_seconds")
                    or (deadline - started)
                ),
            )
            feed_bucket_count = len(self._state.get("feed_minute_buckets", {}))
            healthy_feed_seconds = min(
                expected_feed_seconds,
                feed_bucket_count * 60.0,
            )
            feed_coverage_ratio = (
                healthy_feed_seconds / expected_feed_seconds
                if expected_feed_seconds > 0 else 1.0
            )
            latest_feed_book_epoch = float(
                self._state.get("last_feed_book_epoch") or 0.0
            )
            latest_feed_activity_epoch = float(
                self._state.get("last_feed_activity_epoch")
                or latest_feed_book_epoch
            )
            evaluation_endpoint = float(
                self._state.get("evaluation_completed_at_epoch") or deadline
            )
            feed_stale_at_deadline_seconds = (
                max(0.0, evaluation_endpoint - latest_feed_activity_epoch)
                if latest_feed_activity_epoch > 0 else math.inf
            )
            finalization = dict(
                self._state.get("evaluation_finalization") or {}
            )
            summary = {
                "schema_version": SCHEMA_VERSION,
                "profile": profile,
                "started_at_epoch": float(self._state["started_at_epoch"]),
                "original_evaluation_deadline_epoch": float(
                    self._state.get("original_evaluation_deadline_epoch")
                    or self._state["evaluation_deadline_epoch"]
                ),
                "evaluation_deadline_epoch": float(
                    self._state["evaluation_deadline_epoch"]
                ),
                "coverage_target_hours": expected_feed_seconds / 3600,
                "evaluation_completion_mode": self._state.get(
                    "evaluation_completion_mode", "wall_clock"
                ),
                "healthy_feed_target_hours": float(
                    self._state.get("evaluation_healthy_feed_target_seconds")
                    or 0.0
                ) / 3600,
                "remaining_healthy_feed_hours": max(
                    0.0,
                    float(
                        self._state.get("evaluation_healthy_feed_target_seconds")
                        or 0.0
                    ) - healthy_feed_seconds,
                ) / 3600,
                "evaluation_completed_at_epoch": self._state.get(
                    "evaluation_completed_at_epoch"
                ),
                "observation_continuation": dict(
                    self._state.get("observation_continuation") or {}
                ),
                "observation_coverage_completion": dict(
                    self._state.get("observation_coverage_completion") or {}
                ),
                "evaluated_at_epoch": now,
                "latest_observation_epoch": latest_observation_epoch,
                "latest_feed_book_epoch": latest_feed_book_epoch,
                "latest_feed_activity_epoch": latest_feed_activity_epoch,
                "healthy_feed_hours": healthy_feed_seconds / 3600,
                "feed_coverage_ratio": feed_coverage_ratio,
                "feed_stale_at_deadline_seconds": (
                    feed_stale_at_deadline_seconds
                ),
                "qualification_config_matches": (
                    self._state.get("qualification_config")
                    == self._qualification_config()
                ),
                "market_count": len(child._state["markets"]),
                "book_sample_count": total_book_samples,
                "observed_market_hours": total_observed_seconds / 3600,
                "eligible_quote_hours": (
                    total_eligible_observed_seconds / 3600
                ),
                "tape_trade_count": len(all_trades),
                "tape_traded_shares": sum(
                    float(trade.get("quantity", 0.0)) for trade in all_trades
                ),
                "qualifying_tape_trade_count": sum(
                    1 for trade in all_trades
                    if trade.get("primary_quote_admissible")
                ),
                "tape_trade_without_quote_count": sum(
                    1 for trade in all_trades
                    if not trade.get("primary_quote_present")
                ),
                "tape_trade_inactive_quote_count": sum(
                    1 for trade in all_trades
                    if trade.get("primary_quote_present")
                    and not trade.get("primary_quote_live_candidate")
                ),
                "tape_trade_depth_rejected_count": sum(
                    1 for trade in all_trades
                    if trade.get("primary_quote_present")
                    and trade.get("primary_quote_live_candidate")
                    and not trade.get("primary_quote_depth_ok")
                ),
                "hypothetical_fill_count": len(fills_for_markout),
                "completed_round_trips": len(trips),
                "distinct_event_count": len({
                    trip["event_id"] for trip in trips
                }),
                "realized_pnl_usd": realized,
                "open_inventory_mark_to_market_usd": open_mtm,
                "total_pnl_usd": total_pnl,
                "gross_positive_pnl_usd": gross_profit,
                "gross_loss_usd": gross_loss,
                "profit_factor": profit_factor,
                "avg_markout_5m_cents": (
                    mean(markouts_5m) if markouts_5m else None
                ),
                "markout_5m_sample_count": len(markouts_5m),
                # Entry-only subset of markout_5m_sample_count above --
                # excludes exit/forced-exit/settlement fills. The dry-run
                # verdict's "matured entry markouts" sample requirement
                # needs this specific count, not the combined entry+exit
                # figure above.
                "entry_markout_5m_sample_count": sum(
                    1 for fill in fills_for_markout
                    if fill.get("role") == "entry"
                    and fill.get("markout_5m_cents") is not None
                ),
                "maximum_drawdown_usd": drawdown,
                "event_profit_concentration": concentration,
                "forced_exit_count": forced_exits,
                "forced_exit_pnl_usd": forced_exit_pnl,
                "forced_exit_loss_usd": min(0.0, forced_exit_pnl),
                "settlement_exit_count": settlement_exit_count,
                "settlement_pnl_usd": settlement_pnl_usd,
                "open_inventory": open_inventory,
                "open_shadow_strategy_positions": (
                    open_shadow_strategy_positions
                ),
                "evaluation_finalization": finalization,
                "cohorts": cohorts,
                "qualifying_cohort_count": sum(
                    1 for row in cohorts if row["live_eligible"]
                ),
            }
            if profile == PROFILE_CONTROLLED:
                summary.update(self._controlled_status(summary))
            else:
                summary.update({
                    "status": "SHADOW_ONLY",
                    "pilot_unlocked": False,
                    "blocked_reasons": [
                        f"{profile} profile is permanently shadow-only"
                    ],
                })
            return summary

    def _cohort_rows(
        self,
        profile: str,
        trips: list[dict[str, Any]],
        fills: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        keys = {
            str(item.get("cohort_key"))
            for item in trips + fills if item.get("cohort_key")
        }
        rows = []
        for key in keys:
            cohort_trips = [item for item in trips if item.get("cohort_key") == key]
            cohort_fills = [item for item in fills if item.get("cohort_key") == key]
            markouts = [
                float(item["markout_5m_cents"])
                for item in cohort_fills
                if item.get("markout_5m_cents") is not None
            ]
            pnl = sum(float(item["pnl_usd"]) for item in cohort_trips)
            distinct_events = len({item["event_id"] for item in cohort_trips})
            avg_markout = mean(markouts) if markouts else None
            eligible = (
                profile == PROFILE_CONTROLLED
                and len(cohort_trips)
                >= self.settings.observation_cohort_min_round_trips
                and distinct_events
                >= self.settings.observation_cohort_min_distinct_events
                and pnl > 0
                and avg_markout is not None
                and avg_markout >= 0
            )
            rows.append({
                "cohort_key": key,
                "completed_round_trips": len(cohort_trips),
                "distinct_event_count": distinct_events,
                "net_pnl_usd": pnl,
                "avg_markout_5m_cents": avg_markout,
                "markout_5m_sample_count": len(markouts),
                "live_eligible": eligible,
            })
        return sorted(
            rows,
            key=lambda row: (
                row["live_eligible"],
                row["net_pnl_usd"],
                row["completed_round_trips"],
            ),
            reverse=True,
        )

    def _controlled_status(self, summary: dict[str, Any]) -> dict[str, Any]:
        sample_reasons = []
        if not summary["qualification_config_matches"]:
            sample_reasons.append(
                "observation configuration does not match the current controlled profile"
            )
        if (
            summary["completed_round_trips"]
            < self.settings.observation_controlled_min_round_trips
        ):
            sample_reasons.append("fewer than required completed round trips")
        if (
            summary["distinct_event_count"]
            < self.settings.observation_controlled_min_distinct_events
        ):
            sample_reasons.append("fewer than required distinct events")
        if summary["qualifying_cohort_count"] < 1:
            sample_reasons.append("no controlled-live eligible cohort")
        if summary["avg_markout_5m_cents"] is None:
            sample_reasons.append("no five-minute markout samples")

        deadline_reached = self._evaluation_complete_locked(time.time())
        # Full schema-v4 summaries contain feed coverage.  Direct unit-level
        # callers that supply the older minimal summary intentionally retain
        # the pre-existing gate behavior.
        if deadline_reached and "feed_coverage_ratio" in summary:
            coverage = float(summary.get("feed_coverage_ratio") or 0.0)
            minimum = max(
                0.0,
                min(1.0, self.settings.observation_min_feed_coverage_ratio),
            )
            if coverage + 1e-12 < minimum:
                sample_reasons.append(
                    "healthy market-data coverage "
                    f"{coverage:.1%} < required {minimum:.1%}"
                )
            stale_seconds = float(
                summary.get("feed_stale_at_deadline_seconds") or 0.0
            )
            if stale_seconds > max(
                1.0, self.settings.observation_feed_stale_after_seconds,
            ):
                sample_reasons.append(
                    "market-data feed was stale at the evaluation deadline"
                )
            finalization = summary.get("evaluation_finalization") or {}
            if not finalization.get("attempted"):
                sample_reasons.append(
                    "deadline shadow-inventory finalization was not performed"
                )
            elif finalization.get("missing_book_slugs"):
                sample_reasons.append(
                    "deadline finalization lacked a fresh L2 book for shadow inventory"
                )

        quality_reasons = []
        if summary["open_inventory"]:
            quality_reasons.append("open inventory remains at evaluation")
        if int(summary.get("open_shadow_strategy_positions") or 0) > 0:
            quality_reasons.append(
                "open shadow-variant inventory remains at evaluation"
            )
        if summary["total_pnl_usd"] <= 0:
            quality_reasons.append("total P&L is not positive after fees and marks")
        if (
            summary["profit_factor"]
            < self.settings.observation_controlled_min_profit_factor
        ):
            quality_reasons.append("profit factor is below the required minimum")
        if (
            summary["avg_markout_5m_cents"] is not None
            and summary["avg_markout_5m_cents"] < 0
        ):
            quality_reasons.append("average five-minute markout is negative")
        if (
            summary["maximum_drawdown_usd"]
            < -self.settings.observation_controlled_max_drawdown_usd
        ):
            quality_reasons.append("maximum drawdown exceeds the one-share limit")
        if (
            summary["event_profit_concentration"]
            > self.settings.observation_controlled_max_event_profit_concentration
        ):
            quality_reasons.append("one event contributes over the allowed profit share")

        if not deadline_reached or sample_reasons:
            status = QUALIFICATION_INSUFFICIENT
        elif quality_reasons:
            status = QUALIFICATION_FAIL
        else:
            status = QUALIFICATION_PASS
        return {
            "status": status,
            "pilot_unlocked": status == QUALIFICATION_PASS,
            "deadline_reached": deadline_reached,
            "blocked_reasons": sample_reasons + quality_reasons,
        }

    def qualifying_controlled_cohorts(self) -> set[str]:
        return {
            row["cohort_key"]
            for row in self.profile_summary(PROFILE_CONTROLLED)["cohorts"]
            if row["live_eligible"]
        }

    def controlled_qualification(self) -> dict[str, Any]:
        return self.profile_summary(PROFILE_CONTROLLED)

    def pilot_start_eligible(self) -> tuple[bool, list[str]]:
        summary = self.controlled_qualification()
        reasons = list(summary["blocked_reasons"])
        if summary["status"] != QUALIFICATION_PASS and not reasons:
            reasons.append("controlled profile has not passed")
        latest = float(summary.get("latest_observation_epoch") or 0.0)
        max_age = max(
            300.0,
            self.settings.observation_profile_refresh_seconds * 2,
        )
        if latest <= 0 or time.time() - latest > max_age:
            reasons.append("controlled observation evidence is stale")
        return not reasons and summary["status"] == QUALIFICATION_PASS, reasons

    def entry_eligible(self, slug: str) -> tuple[bool, list[str]]:
        if self.settings.observation_only_mode:
            return False, ["observation-only mode is enabled"]
        if not self.settings.observation_gate_enabled:
            return True, []
        summary = self.controlled_qualification()
        if summary["status"] != QUALIFICATION_PASS:
            return False, list(summary["blocked_reasons"]) or [
                "controlled portfolio has not passed its 48-hour gate"
            ]
        market = self._state["profiles"][PROFILE_CONTROLLED]["markets"].get(slug)
        if not isinstance(market, dict):
            return False, ["market has no controlled-profile evidence"]
        cohort = self._current_cohort_key(PROFILE_CONTROLLED, market)
        if cohort not in self.qualifying_controlled_cohorts():
            return False, ["market's current cohort did not qualify"]
        return True, []

    def _current_cohort_key(
        self, profile: str, market: dict[str, Any],
    ) -> str:
        price = _positive_float(market.get("last_mid")) or 0.5
        return self._cohort_key(
            profile,
            market,
            {"observed_at_epoch": time.time(), "price": price},
        )

    def report(
        self, profile: str = PROFILE_CONTROLLED,
    ) -> list[dict[str, Any]]:
        child = self._trackers[profile]
        rows = child.report()
        allowed_cohorts = (
            self.qualifying_controlled_cohorts()
            if profile == PROFILE_CONTROLLED else set()
        )
        for row in rows:
            market = child._state["markets"].get(row["market_slug"], {})
            row["profile"] = profile
            row["event_id"] = market.get("event_id", "")
            row["cohort_key"] = self._current_cohort_key(profile, market)
            row["entry_eligible"] = (
                profile == PROFILE_CONTROLLED
                and self.controlled_qualification()["status"] == QUALIFICATION_PASS
                and row["cohort_key"] in allowed_cohorts
            )
        return rows

    def flush(self) -> None:
        with self._lock:
            storage.save_json(self.path, self._state)
            self._last_persist_epoch = time.time()

    def _maybe_persist_locked(
        self, now: float, *, force: bool = False,
    ) -> None:
        if (
            force
            or now - self._last_persist_epoch
            >= self.settings.observation_persist_interval_seconds
        ):
            storage.save_json(self.path, self._state)
            self._last_persist_epoch = now


def load_observation_report(
    settings: Optional[config.LiveTradingSettings] = None,
    path: Optional[Path] = None,
    profile: str = PROFILE_CONTROLLED,
) -> list[dict[str, Any]]:
    return MarketObservationTracker(
        settings or config.load_settings().live,
        path=path,
    ).report(profile)


def load_observation_summary(
    settings: Optional[config.LiveTradingSettings] = None,
    path: Optional[Path] = None,
    profile: str = PROFILE_CONTROLLED,
) -> dict[str, Any]:
    return MarketObservationTracker(
        settings or config.load_settings().live,
        path=path,
    ).profile_summary(profile)


def classify_settlement_lookup(client: PolymarketClient, slug: str) -> dict[str, Any]:
    """One settlement lookup for one slug, combining both REST endpoints
    into a single SETTLED/UNRESOLVED/ERROR verdict. Both endpoints are
    always checked, even when settlement 404s -- the verdict is a function
    of the two responses together, never either one alone:

    | settlement          | metadata                  | result       |
    |----------------------|----------------------------|--------------|
    | value, slug matches  | closed=true, RESOLVED      | SETTLED      |
    | 404                  | confirms NOT resolved      | UNRESOLVED   |
    | 404                  | confirms resolved          | ERROR (disagreement) |
    | value                | does not confirm resolved  | ERROR (disagreement) |
    | (either lookup 404s on metadata, is malformed, or exhausts retries) | | ERROR |

    A settlement 404 is itself never retried (see PolymarketClientNotFound)
    -- it's only ever ambiguous in combination with a disagreeing metadata
    response, which is an ERROR, not something to retry into a different
    answer. Never raises: a network/parsing failure becomes an ERROR
    result so a caller resolving many slugs can always finish the whole
    batch and report every outcome together, rather than the batch dying
    partway through on the first bad slug."""
    retrieved_at_epoch = time.time()
    try:
        settlement = client.get_market_settlement(slug)
    except PolymarketClientError as exc:
        return {
            "slug": slug, "status": "ERROR",
            "reason": f"settlement lookup failed: {exc}",
            "retrieved_at_epoch": retrieved_at_epoch,
        }
    try:
        metadata = client.get_market_metadata(slug)
    except PolymarketClientError as exc:
        return {
            "slug": slug, "status": "ERROR",
            "reason": f"metadata lookup failed: {exc}",
            "retrieved_at_epoch": retrieved_at_epoch,
        }

    metadata_missing = metadata is None
    resolved = (
        not metadata_missing
        and metadata.get("closed") is True
        and metadata.get("status") == "MARKET_STATUS_RESOLVED"
    )
    if settlement is not None and resolved:
        return {
            "slug": slug,
            "status": "SETTLED",
            "settlement_price": settlement["settlement"],
            "metadata_synced_at": metadata.get("ep3SyncedAt") or metadata.get("updatedAt"),
            "retrieved_at_epoch": retrieved_at_epoch,
        }
    if settlement is None and not metadata_missing and not resolved:
        return {"slug": slug, "status": "UNRESOLVED", "retrieved_at_epoch": retrieved_at_epoch}
    return {
        "slug": slug,
        "status": "ERROR",
        "reason": (
            f"settlement/metadata disagreement for {slug}: "
            f"settlement={'present' if settlement is not None else '404'}, "
            f"metadata={'missing' if metadata_missing else ('resolved' if resolved else 'not resolved')}"
        ),
        "retrieved_at_epoch": retrieved_at_epoch,
    }


def _build_quote_plans(
    best_bid: float,
    best_ask: float,
    tick: float,
    min_edge_cents: float,
    max_spread: float,
) -> dict[str, tuple[float, float]]:
    if best_ask - best_bid > max_spread + 1e-9:
        return {}
    plans: dict[str, tuple[float, float]] = {}
    primary = compute_book_aware_quote(
        best_bid, best_ask, tick, min_edge_cents,
    )
    if primary is not None:
        plans[PRIMARY_STRATEGY] = (primary.bid, primary.ask)
    candidates = {
        "improve_bid_join_ask": (best_bid + tick, best_ask),
        "join_bid_improve_ask": (best_bid, best_ask - tick),
        "join_both": (best_bid, best_ask),
    }
    min_edge = min_edge_cents / 100
    for strategy, (bid, ask) in candidates.items():
        bid = round(bid / tick) * tick
        ask = round(ask / tick) * tick
        if 0 < bid < ask < 1 and ask - bid >= min_edge - 1e-9:
            plans[strategy] = (round(bid, 9), round(ask, 9))
    return plans


def _effective_min_edge_cents(
    market: dict[str, Any],
    now: float,
    settings: config.LiveTradingSettings,
) -> float:
    """Mirror the live near-resolution entry-edge multiplier."""
    minimum = float(settings.min_edge_cents)
    event_epoch = _positive_float(market.get("event_or_close_epoch"))
    if event_epoch is None:
        return minimum
    hours = (event_epoch - now) / 3600
    if hours <= settings.near_resolution_hours_threshold:
        minimum *= max(1.0, settings.near_resolution_min_edge_multiplier)
    return minimum


def _position_aware_quote_plans(
    market: dict[str, Any],
    base_plans: dict[str, tuple[float, float]],
    tick: float,
    now: float,
    settings: config.LiveTradingSettings,
) -> tuple[
    dict[str, tuple[Optional[float], Optional[float]]],
    set[str],
]:
    """Turn flat paired quotes into the live bot's flat-first state machine."""
    plans: dict[str, tuple[Optional[float], Optional[float]]] = {}
    managed_strategies: set[str] = set()
    all_fills = market.get("hypothetical_fills", [])
    event_epoch = _positive_float(market.get("event_or_close_epoch"))
    entry_time_safe = not (
        event_epoch is not None
        and event_epoch - now
        <= max(0.0, settings.pre_event_reduce_only_minutes) * 60
    )

    for strategy, (base_bid, base_ask) in base_plans.items():
        strategy_fills = [
            item for item in all_fills
            if item.get("strategy") == strategy and item.get("admissible")
        ]
        state = _paper_position_state(strategy_fills)
        position = float(state["position"])
        if position == 0:
            if not entry_time_safe:
                continue
            bid, ask = _flat_entry_prices(
                base_bid, base_ask, settings,
            )
            if bid is not None or ask is not None:
                plans[strategy] = (bid, ask)
            continue

        managed_strategies.add(strategy)
        if not settings.flat_first_inventory_enabled:
            plans[strategy] = (base_bid, base_ask)
            continue

        opened_at = state["opened_at_epoch"]
        age_hours = (
            max(0.0, now - float(opened_at)) / 3600
            if opened_at is not None else 0.0
        )
        max_hours = float(settings.liquidation_max_holding_hours)
        age_fraction = (
            max(0.0, min(1.0, age_hours / max_hours))
            if max_hours > 0 else 0.0
        )
        allowed_loss_cents = (
            max(0.0, settings.liquidation_max_loss_cents) * age_fraction
        )
        average_price = state["average_price"]
        if position > 0:
            reducing_ask = apply_liquidation_limit(
                base_ask,
                position,
                average_price,
                tick,
                allowed_loss_cents,
                settings.liquidation_max_loss_usd,
            )
            if reducing_ask is not None:
                plans[strategy] = (None, reducing_ask)
        else:
            reducing_bid = apply_liquidation_limit(
                base_bid,
                position,
                average_price,
                tick,
                allowed_loss_cents,
                settings.liquidation_max_loss_usd,
            )
            if reducing_bid is not None:
                plans[strategy] = (reducing_bid, None)
    return plans, managed_strategies


def _flat_entry_prices(
    bid: float,
    ask: float,
    settings: config.LiveTradingSettings,
) -> tuple[Optional[float], Optional[float]]:
    """Apply the same flat-position extreme-price/payoff screens per leg."""
    captured_spread_cents = (ask - bid) * 100

    def _allowed(side: str, price: float) -> bool:
        extreme = (
            price <= settings.extreme_price_low_threshold
            or price >= settings.extreme_price_high_threshold
        )
        if extreme and captured_spread_cents < settings.extreme_price_min_edge_cents:
            return False
        if captured_spread_cents <= 0:
            return False
        max_loss_cents = (price if side == "BUY" else 1 - price) * 100
        return (
            max_loss_cents / captured_spread_cents
            <= settings.max_payoff_loss_to_capture_ratio
        )

    safe_bid = bid if _allowed("BUY", bid) else None
    safe_ask = ask if _allowed("SELL", ask) else None
    if settings.require_both_entry_legs and (
        safe_bid is None or safe_ask is None
    ):
        return None, None
    return safe_bid, safe_ask


def _fill_price_is_valid(fill: dict[str, Any], price: float) -> bool:
    """A real trade fill's price can never legitimately be exactly $0 or
    $1 -- that bound catches bad data. A settlement-based synthetic exit
    (see MarketObservationTracker._settle_at_resolution) is different: its
    whole point is representing the market's actual $0/$1 (or any
    fractional [0,1]) resolution payout, so it needs the inclusive bound
    instead. Every fill-price gate in this module must use this one
    function rather than re-deriving the bound locally, or the three
    independent copies drift the way RUNBOOK.md's `38.` already had to fix
    once for the entry-window calculation."""
    if fill.get("liquidity_role") == "settlement":
        return 0.0 <= price <= 1.0
    return 0.0 < price < 1.0


def _paper_position_state(
    fills: list[dict[str, Any]],
) -> dict[str, Optional[float]]:
    """Current paper inventory, average price, and opening time."""
    position = 0.0
    average_price = 0.0
    opened_at: Optional[float] = None
    for fill in sorted(
        fills,
        key=lambda item: float(item.get("observed_at_epoch", 0.0)),
    ):
        quantity = max(0.0, float(fill.get("quantity", 0.0)))
        price = float(fill.get("price", 0.0))
        side = fill.get("side")
        timestamp = float(fill.get("observed_at_epoch", 0.0))
        if quantity <= 0 or not _fill_price_is_valid(fill, price) or side not in {"BUY", "SELL"}:
            continue
        signed = quantity if side == "BUY" else -quantity
        if position == 0 or position * signed > 0:
            total = abs(position) + quantity
            average_price = (
                (abs(position) * average_price + quantity * price) / total
            )
            if position == 0:
                opened_at = timestamp
            position += signed
            continue

        prior_position = position
        position += signed
        if abs(position) <= 1e-9:
            position = 0.0
            average_price = 0.0
            opened_at = None
        elif position * prior_position < 0:
            average_price = price
            opened_at = timestamp

    return {
        "position": position,
        "average_price": average_price if position != 0 else None,
        "opened_at_epoch": opened_at,
    }


def _trade_hits_side(
    maker_side: str,
    trade_price: float,
    bid: Optional[float],
    ask: Optional[float],
) -> Optional[str]:
    if (
        bid is not None
        and maker_side.endswith("BUY")
        and trade_price <= bid + 1e-9
    ):
        return "BUY"
    if (
        ask is not None
        and maker_side.endswith("SELL")
        and trade_price >= ask - 1e-9
    ):
        return "SELL"
    return None


def _quantity_at_price(
    levels: list[dict[str, Any]], price: Optional[float],
) -> float:
    if price is None:
        return 0.0
    total = 0.0
    for level in levels:
        level_price = _level_float(level, "price")
        if level_price is not None and abs(level_price - price) <= 1e-9:
            total += _level_float(level, "quantity") or 0.0
    return max(0.0, total)


def _same_optional_price(left: Any, right: Optional[float]) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except (TypeError, ValueError):
        return False


def _distinct_fill_episodes(
    fills: list[dict[str, Any]], gap_seconds: float,
) -> int:
    times = sorted(float(item.get("observed_at_epoch", 0.0)) for item in fills)
    if not times:
        return 0
    count = 1
    last = times[0]
    for timestamp in times[1:]:
        if timestamp - last >= max(0.0, gap_seconds):
            count += 1
        last = timestamp
    return count


def _paper_round_trip_stats(
    fills: list[dict[str, Any]],
) -> dict[str, Any]:
    position = 0.0
    average_price = 0.0
    average_open_commission_per_share = 0.0
    realized = 0.0
    round_trips = 0
    for fill in sorted(
        fills,
        key=lambda item: float(item.get("observed_at_epoch", 0.0)),
    ):
        quantity = max(0.0, float(fill.get("quantity", 0.0)))
        price = float(fill.get("price", 0.0))
        side = fill.get("side")
        if quantity <= 0 or not _fill_price_is_valid(fill, price):
            continue
        commission_per_share = (
            float(fill.get("commission_usd") or 0.0) / quantity
        )
        signed = quantity if side == "BUY" else -quantity
        if position == 0 or position * signed > 0:
            total = abs(position) + quantity
            average_price = (
                (abs(position) * average_price + quantity * price) / total
            )
            average_open_commission_per_share = (
                (
                    abs(position) * average_open_commission_per_share
                    + quantity * commission_per_share
                )
                / total
            )
            position += signed
            continue

        position_before = abs(position)
        closing = min(position_before, quantity)
        if position > 0:
            gross = (price - average_price) * closing
        else:
            gross = (average_price - price) * closing
        realized += gross - (
            average_open_commission_per_share + commission_per_share
        ) * closing
        if closing >= position_before - 1e-9:
            round_trips += 1
        prior_sign = 1.0 if position > 0 else -1.0
        position += signed
        if abs(position) <= 1e-9:
            position = 0.0
            average_price = 0.0
            average_open_commission_per_share = 0.0
        elif position * prior_sign < 0:
            average_price = price
            average_open_commission_per_share = commission_per_share
    return {
        "paper_round_trip_count": round_trips,
        "paper_realized_pnl_usd": realized,
        "paper_open_position_shares": position,
    }


def _estimated_commission(
    price: float,
    quantity: float,
    theta: float,
) -> float:
    """Signed Polymarket US commission estimate for a shadow fill."""
    return (
        float(theta)
        * max(0.0, float(quantity))
        * float(price)
        * (1.0 - float(price))
    )


def _level_float(level: Any, field: str) -> Optional[float]:
    if not isinstance(level, dict):
        return None
    value = level.get(field)
    if isinstance(value, dict):
        value = value.get("value")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _best_prices(
    book: dict[str, Any],
) -> tuple[Optional[float], Optional[float]]:
    bids = [
        value for level in list(book.get("bids") or [])
        if (value := _level_float(level, "price")) is not None
    ]
    asks = [
        value for level in list(book.get("asks") or [])
        if (value := _level_float(level, "price")) is not None
    ]
    best_bid = max(bids) if bids else None
    best_ask = min(asks) if asks else None
    if (
        best_bid is None
        or best_ask is None
        or best_bid <= 0
        or best_ask >= 1
        or best_bid >= best_ask
    ):
        return None, None
    return best_bid, best_ask


def _market_family(slug: str, raw: Optional[dict[str, Any]]) -> str:
    raw = raw or {}
    for key in ("marketType", "market_type", "sportsMarketType", "category"):
        value = raw.get(key)
        if value:
            return str(value).strip().lower()
    parts = [part for part in str(slug).lower().split("-") if part]
    return "-".join(parts[:2]) if parts else "unknown"


def _round_trip_records(
    fills: list[dict[str, Any]],
    *,
    slug: str,
    event_id: str,
) -> list[dict[str, Any]]:
    """FIFO-like one-position accounting with a record for every flat close."""
    records: list[dict[str, Any]] = []
    position = 0.0
    average_price = 0.0
    average_open_commission = 0.0
    trip_pnl = 0.0
    opened_at: Optional[float] = None
    cohort_key: Optional[str] = None
    forced = False
    closure_type: Optional[str] = None
    for fill in sorted(
        fills,
        key=lambda item: float(item.get("observed_at_epoch", 0.0)),
    ):
        quantity = max(0.0, float(fill.get("quantity", 0.0)))
        price = float(fill.get("price", 0.0))
        side = fill.get("side")
        if quantity <= 0 or not _fill_price_is_valid(fill, price) or side not in {"BUY", "SELL"}:
            continue
        commission_per_share = float(fill.get("commission_usd") or 0.0) / quantity
        signed = quantity if side == "BUY" else -quantity
        if position == 0 or position * signed > 0:
            total = abs(position) + quantity
            average_price = (
                abs(position) * average_price + quantity * price
            ) / total
            average_open_commission = (
                abs(position) * average_open_commission
                + quantity * commission_per_share
            ) / total
            if position == 0:
                opened_at = float(fill.get("observed_at_epoch", 0.0))
                cohort_key = fill.get("cohort_key")
                trip_pnl = 0.0
                forced = False
                closure_type = None
            position += signed
            continue

        prior_abs = abs(position)
        closing = min(prior_abs, quantity)
        gross = (
            (price - average_price) * closing
            if position > 0
            else (average_price - price) * closing
        )
        trip_pnl += gross - (
            average_open_commission + commission_per_share
        ) * closing
        # A settlement close is an authoritative real-world resolution, not
        # a defensive/approximate liquidation -- it must not be counted as
        # a "forced exit" (see profile_summary()'s forced_exit_count/_pnl,
        # which use this to flag book-based deadline liquidations as a
        # quality signal). closure_type reflects whichever closing fill
        # most recently reduced this trip -- normally just one fill, since
        # both _force_exit() and _settle_at_resolution() always close a
        # trip's entire remaining size in a single fill.
        closure_type = fill.get("closure_type")
        forced = forced or (bool(fill.get("exit_reason")) and closure_type != "settlement")
        prior_sign = 1.0 if position > 0 else -1.0
        position += signed
        if abs(position) <= 1e-9:
            records.append({
                "market_slug": slug,
                "event_id": event_id,
                "cohort_key": cohort_key,
                "opened_at_epoch": opened_at,
                "closed_at_epoch": float(
                    fill.get("observed_at_epoch", 0.0)
                ),
                "pnl_usd": trip_pnl,
                "forced_exit": forced,
                "closure_type": closure_type,
            })
            position = 0.0
            average_price = 0.0
            average_open_commission = 0.0
            opened_at = None
            cohort_key = None
            trip_pnl = 0.0
            forced = False
            closure_type = None
        elif position * prior_sign < 0:
            # A reversal is a closed trip plus a new inventory lot. The
            # controlled profile prevents this; handling it keeps the legacy
            # benchmark's accounting complete.
            records.append({
                "market_slug": slug,
                "event_id": event_id,
                "cohort_key": cohort_key,
                "opened_at_epoch": opened_at,
                "closed_at_epoch": float(
                    fill.get("observed_at_epoch", 0.0)
                ),
                "pnl_usd": trip_pnl,
                "forced_exit": forced,
            })
            average_price = price
            average_open_commission = commission_per_share
            opened_at = float(fill.get("observed_at_epoch", 0.0))
            cohort_key = fill.get("cohort_key")
            trip_pnl = 0.0
            forced = False
    return records


def _open_inventory_mtm(
    fills: list[dict[str, Any]],
    best_bid: Optional[float],
    best_ask: Optional[float],
    taker_fee_theta: float,
) -> Optional[float]:
    """Net liquidation mark for the currently open lot, including already-
    paid entry commissions and the prospective exit commission. Marks a
    long against best_bid (what a sell-to-close would fetch right now) and
    a short against best_ask (what a buy-to-close would cost) -- the price
    actually achievable to close, not an untradeable single mark_price.
    Returns None (never 0.0) when the relevant side's executable price is
    unavailable -- an unpriced position is unknown exposure, not worthless."""
    position = 0.0
    cash = 0.0
    for fill in sorted(
        fills,
        key=lambda item: float(item.get("observed_at_epoch", 0.0)),
    ):
        quantity = max(0.0, float(fill.get("quantity", 0.0)))
        if quantity <= 0:
            continue
        price = float(fill.get("price", 0.0))
        commission = float(fill.get("commission_usd") or 0.0)
        if fill.get("side") == "BUY":
            position += quantity
            cash -= quantity * price + commission
        elif fill.get("side") == "SELL":
            position -= quantity
            cash += quantity * price - commission
        if abs(position) <= 1e-9:
            position = 0.0
            cash = 0.0
    if abs(position) <= 1e-9:
        return 0.0
    mark_price = best_bid if position > 0 else best_ask
    if mark_price is None:
        return None
    exit_commission = _estimated_commission(mark_price, abs(position), taker_fee_theta)
    return cash + position * mark_price - exit_commission


def _maximum_drawdown_usd(
    trips: list[dict[str, Any]], open_mtm: float,
) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum_drawdown = 0.0
    for trip in trips:
        cumulative += float(trip["pnl_usd"])
        peak = max(peak, cumulative)
        maximum_drawdown = min(maximum_drawdown, cumulative - peak)
    cumulative += open_mtm
    maximum_drawdown = min(maximum_drawdown, cumulative - peak)
    return maximum_drawdown


def _maximum_drawdown_from_curve(
    curve: list[dict[str, Any]], final_pnl: float,
) -> float:
    # Points with total_pnl_usd is None (valuation_incomplete -- an open
    # position couldn't be priced when the point was recorded) are skipped
    # rather than crashing on float(None) or coercing to 0.0, which would
    # fabricate a recovery-to-zero that never happened.
    values = [
        float(point["total_pnl_usd"])
        for point in curve
        if isinstance(point, dict) and point.get("total_pnl_usd") is not None
    ]
    values.append(float(final_pnl))
    peak = 0.0
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, value - peak)
    return drawdown
