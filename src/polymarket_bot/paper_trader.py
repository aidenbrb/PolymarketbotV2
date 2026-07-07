"""Simulated ("paper") trade execution.

FAKE TRADING ONLY. No wallet, no private key, no live order placement of any
kind. See tests/test_no_live_orders.py, which fails the build if any
live-execution code path is introduced here or anywhere else in the package.
"""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

from . import config, storage
from .logger import get_logger
from .models import PaperTrade, utcnow_iso

logger = get_logger("paper_trader")

TRADES_FILE = config.PAPER_TRADES_DIR / "trades.json"


class PaperTrader:
    def __init__(self, settings: Optional[config.PaperTradingSettings] = None):
        self.settings = settings or config.load_settings().paper

    # ------------------------------------------------------------------
    def get_all_trades(self) -> list[dict]:
        return storage.load_json(TRADES_FILE, default=[])

    def get_open_trades(self) -> list[dict]:
        return [t for t in self.get_all_trades() if t.get("status") == "OPEN"]

    def get_closed_trades(self) -> list[dict]:
        return [t for t in self.get_all_trades() if t.get("status") == "CLOSED"]

    def get_trade(self, trade_id: str) -> Optional[dict]:
        for t in self.get_all_trades():
            if t.get("trade_id") == trade_id:
                return t
        return None

    # ------------------------------------------------------------------
    def open_trade(
        self,
        market_id: str,
        token_id: Optional[str],
        outcome: str,
        requested_price: float,
        size_usd: float,
        reason_entry: str,
    ) -> PaperTrade:
        """Record a fake fill. Applies fake slippage against the requested price."""
        if requested_price <= 0:
            raise ValueError("requested_price must be positive")
        if size_usd <= 0:
            raise ValueError("size_usd must be positive")

        slippage = self.settings.fake_slippage_bps / 10_000
        effective_price = round(requested_price * (1 + slippage), 6)
        shares = round(size_usd / effective_price, 6)

        trade = PaperTrade(
            trade_id=uuid4().hex,
            market_id=market_id,
            token_id=token_id,
            outcome=outcome,
            side="BUY",
            entry_price=effective_price,
            size_usd=size_usd,
            shares=shares,
            entry_time=utcnow_iso(),
            reason_entry=reason_entry,
        )

        trades = self.get_all_trades()
        trades.append(trade.to_dict())
        storage.save_json(TRADES_FILE, trades)
        logger.info(
            "Opened paper trade %s: %s (%s) size=$%.2f price=%.4f shares=%.4f",
            trade.trade_id, market_id, outcome, size_usd, effective_price, shares,
        )
        return trade

    def close_trade(
        self,
        trade_id: str,
        requested_exit_price: float,
        reason_exit: str,
    ) -> PaperTrade:
        if requested_exit_price <= 0:
            raise ValueError("requested_exit_price must be positive")

        trades = self.get_all_trades()
        for i, t in enumerate(trades):
            if t.get("trade_id") == trade_id and t.get("status") == "OPEN":
                slippage = self.settings.fake_slippage_bps / 10_000
                effective_exit = round(requested_exit_price * (1 - slippage), 6)
                realized_pnl = round((effective_exit - t["entry_price"]) * t["shares"], 4)

                t["exit_price"] = effective_exit
                t["exit_time"] = utcnow_iso()
                t["reason_exit"] = reason_exit
                t["status"] = "CLOSED"
                t["realized_pnl"] = realized_pnl
                trades[i] = t

                storage.save_json(TRADES_FILE, trades)
                logger.info(
                    "Closed paper trade %s: exit_price=%.4f realized_pnl=%.4f",
                    trade_id, effective_exit, realized_pnl,
                )
                return PaperTrade(**t)

        raise ValueError(f"No open trade found with id {trade_id}")
