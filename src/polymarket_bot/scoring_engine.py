"""Transparent, rule-based 0-100 market quality scoring.

No black-box models or predictions of market outcomes. This scores the
QUALITY of a market as a research/paper-trading candidate only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import config
from .logger import get_logger
from .models import Market, ScoredMarket

logger = get_logger("scoring_engine")


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


def _scale(value: float, low: float, high: float, max_points: float) -> float:
    """Linearly scale value in [low, high] to [0, max_points], clamped."""
    if high <= low:
        return 0.0
    fraction = (value - low) / (high - low)
    fraction = max(0.0, min(1.0, fraction))
    return round(fraction * max_points, 2)


class ScoringEngine:
    def __init__(self, settings: Optional[config.ScoringSettings] = None):
        self.settings = settings or config.load_settings().scoring

    def score(self, market: Market) -> ScoredMarket:
        s = self.settings
        components: dict[str, float] = {}
        explanation: list[str] = []

        # Liquidity: 0 at $0, full points at $50k+
        components["liquidity"] = _scale(market.liquidity, 0, 50_000, s.liquidity_max)
        explanation.append(
            f"Liquidity ${market.liquidity:,.0f} -> {components['liquidity']}/{s.liquidity_max}"
        )

        # Spread: full points at spread 0, zero points at spread >= 0.10
        if market.spread is None:
            components["spread"] = 0.0
            explanation.append("No spread data available -> 0 points")
        else:
            components["spread"] = _scale(0.10 - market.spread, 0, 0.10, s.spread_max)
            explanation.append(
                f"Spread {market.spread:.4f} -> {components['spread']}/{s.spread_max}"
            )

        # Volume: 0 at $0, full points at $20k+ 24h volume
        components["volume"] = _scale(market.volume_24h, 0, 20_000, s.volume_max)
        explanation.append(
            f"24h volume ${market.volume_24h:,.0f} -> {components['volume']}/{s.volume_max}"
        )

        # Time to resolution: best around 3-30 days out; too soon or too far is worse.
        components["time_to_resolution"] = self._score_time_to_resolution(market, s)
        explanation.append(
            f"Time to resolution -> {components['time_to_resolution']}/{s.time_to_resolution_max}"
        )

        # Price stability: prefer prices not sitting at the extremes (more genuinely open).
        components["price_stability"] = self._score_price_stability(market, s)
        explanation.append(
            f"Price positioning -> {components['price_stability']}/{s.price_stability_max}"
        )

        # Market clarity: penalize very short or very long/ambiguous questions.
        components["clarity"] = self._score_clarity(market, s)
        explanation.append(f"Question clarity -> {components['clarity']}/{s.clarity_max}")

        # Category risk: flatly reduce points for higher-volatility/harder-to-model categories.
        components["category_risk"] = self._score_category_risk(market, s)
        explanation.append(
            f"Category risk ({market.category}) -> {components['category_risk']}/{s.category_risk_max}"
        )

        # Risk/reward quality: combination of liquidity depth and tight spread.
        components["risk_reward"] = self._score_risk_reward(market, s)
        explanation.append(
            f"Risk/reward quality -> {components['risk_reward']}/{s.risk_reward_max}"
        )

        total = round(sum(components.values()), 2)
        recommendation = self._recommendation(total, s)

        return ScoredMarket(
            market_id=market.market_id,
            question=market.question,
            total_score=total,
            component_scores=components,
            explanation=explanation,
            recommendation=recommendation,
            market=market,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _score_time_to_resolution(market: Market, s: config.ScoringSettings) -> float:
        end_dt = _parse_end_date(market.end_date)
        if end_dt is None:
            return 0.0
        days = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        if days <= 0:
            return 0.0
        if days <= 30:
            return _scale(days, 0, 30, s.time_to_resolution_max)
        if days <= 90:
            return s.time_to_resolution_max
        # decay after 90 days out to 365
        return _scale(365 - days, 0, 365 - 90, s.time_to_resolution_max)

    @staticmethod
    def _score_price_stability(market: Market, s: config.ScoringSettings) -> float:
        if not market.outcome_prices:
            return 0.0
        primary = market.outcome_prices[0]
        distance_from_extreme = min(primary, 1 - primary)
        return _scale(distance_from_extreme, 0, 0.5, s.price_stability_max)

    @staticmethod
    def _score_clarity(market: Market, s: config.ScoringSettings) -> float:
        length = len(market.question)
        if length < 10:
            return 0.0
        if 20 <= length <= 140:
            return float(s.clarity_max)
        if length < 20:
            return _scale(length, 10, 20, s.clarity_max)
        return _scale(280 - length, 0, 280 - 140, s.clarity_max)

    @staticmethod
    def _score_category_risk(market: Market, s: config.ScoringSettings) -> float:
        category = (market.category or "").lower()
        if category in s.high_risk_categories:
            return 0.0
        return float(s.category_risk_max)

    @staticmethod
    def _score_risk_reward(market: Market, s: config.ScoringSettings) -> float:
        liquidity_component = _scale(market.liquidity, 0, 50_000, s.risk_reward_max / 2)
        if market.spread is None:
            spread_component = 0.0
        else:
            spread_component = _scale(0.10 - market.spread, 0, 0.10, s.risk_reward_max / 2)
        return round(liquidity_component + spread_component, 2)

    @staticmethod
    def _recommendation(total: float, s: config.ScoringSettings) -> str:
        if total < s.reject_below:
            return "REJECT"
        if total < s.watch_below:
            return "WATCH"
        if total < s.strong_watch_below:
            return "STRONG_WATCH"
        return "PAPER_CANDIDATE"
