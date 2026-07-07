"""Guardrails for simulated (paper) position sizing and exposure.

This has no connection to real money. It exists so the paper-trading
simulation practices the same discipline a live system would need, without
ever touching a wallet or exchange account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import config
from .logger import get_logger

logger = get_logger("risk_manager")


@dataclass
class RiskCheckResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self, settings: Optional[config.RiskSettings] = None):
        self.settings = settings or config.load_settings().risk

    def check_new_position(
        self,
        size_usd: float,
        current_open_positions: int,
        current_exposure_usd: float,
        current_cash: float,
    ) -> RiskCheckResult:
        s = self.settings
        reasons: list[str] = []

        if size_usd <= 0:
            reasons.append("Position size must be positive")

        if size_usd > s.max_position_size_usd:
            reasons.append(
                f"Position size ${size_usd:,.2f} exceeds max position size "
                f"${s.max_position_size_usd:,.2f}"
            )

        if current_open_positions >= s.max_open_positions:
            reasons.append(
                f"Already at max open positions ({current_open_positions}/"
                f"{s.max_open_positions})"
            )

        if current_exposure_usd + size_usd > s.max_total_exposure_usd:
            reasons.append(
                f"New exposure ${current_exposure_usd + size_usd:,.2f} would exceed "
                f"max total exposure ${s.max_total_exposure_usd:,.2f}"
            )

        if current_cash > 0 and size_usd > current_cash * s.max_position_pct_of_cash:
            reasons.append(
                f"Position size ${size_usd:,.2f} exceeds "
                f"{s.max_position_pct_of_cash * 100:.0f}% of current cash "
                f"(${current_cash:,.2f})"
            )

        if size_usd > current_cash:
            reasons.append(
                f"Position size ${size_usd:,.2f} exceeds available paper cash "
                f"${current_cash:,.2f}"
            )

        allowed = not reasons
        if not allowed:
            logger.warning("Risk check blocked trade: %s", "; ".join(reasons))

        return RiskCheckResult(allowed=allowed, reasons=reasons)
