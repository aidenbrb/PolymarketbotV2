"""Rejects unsafe or low-quality markets. Makes no scoring or trading decisions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import config
from .logger import get_logger
from .models import FilterResult, Market

logger = get_logger("filters")


def _parse_end_date(end_date: Optional[str]) -> Optional[datetime]:
    if not end_date:
        return None
    try:
        value = end_date.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


class MarketFilters:
    def __init__(self, settings: Optional[config.FilterSettings] = None):
        self.settings = settings or config.load_settings().filters

    def apply(self, markets: list[Market]) -> tuple[list[Market], list[FilterResult]]:
        accepted: list[Market] = []
        results: list[FilterResult] = []

        for market in markets:
            reasons = self._reject_reasons(market)
            is_accepted = not reasons
            results.append(
                FilterResult(
                    market_id=market.market_id,
                    question=market.question,
                    accepted=is_accepted,
                    reasons=reasons,
                )
            )
            if is_accepted:
                accepted.append(market)

        logger.info(
            "Filters applied: %d accepted, %d rejected out of %d",
            len(accepted), len(markets) - len(accepted), len(markets),
        )
        return accepted, results

    def _reject_reasons(self, market: Market) -> list[str]:
        s = self.settings
        reasons: list[str] = []

        if market.closed:
            reasons.append("Market is closed")
        if not market.active:
            reasons.append("Market is not active")

        if s.require_order_book and not market.has_order_book:
            reasons.append("No order book available")

        if market.liquidity < s.min_liquidity:
            reasons.append(
                f"Liquidity {market.liquidity:.2f} below minimum {s.min_liquidity:.2f}"
            )

        if market.volume_24h < s.min_volume_24h:
            reasons.append(
                f"24h volume {market.volume_24h:.2f} below minimum {s.min_volume_24h:.2f}"
            )

        if market.spread is not None and market.spread > s.max_spread:
            reasons.append(
                f"Spread {market.spread:.4f} exceeds maximum {s.max_spread:.4f}"
            )

        end_dt = _parse_end_date(market.end_date)
        if end_dt is not None:
            now = datetime.now(timezone.utc)
            hours_to_close = (end_dt - now).total_seconds() / 3600
            days_to_close = hours_to_close / 24
            if hours_to_close < s.min_hours_to_close:
                reasons.append(
                    f"Closes in {hours_to_close:.1f}h, below minimum {s.min_hours_to_close}h"
                )
            if days_to_close > s.max_days_to_close:
                reasons.append(
                    f"Closes in {days_to_close:.1f} days, above maximum {s.max_days_to_close} days"
                )

        if s.allowed_categories and market.category not in s.allowed_categories:
            reasons.append(f"Category '{market.category}' not in allowed list")

        question_lower = market.question.lower()
        for keyword in s.unclear_keywords:
            if keyword in question_lower:
                reasons.append(f"Question contains unclear-wording keyword '{keyword}'")

        for price in market.outcome_prices:
            if price < s.min_price or price > s.max_price:
                reasons.append(
                    f"Outcome price {price:.3f} outside allowed range "
                    f"[{s.min_price}, {s.max_price}]"
                )
                break

        return reasons
