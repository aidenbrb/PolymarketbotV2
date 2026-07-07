"""Simulated portfolio bookkeeping: cash, positions, P/L, win rate, drawdown.

All figures are derived from the fake trade ledger written by paper_trader.py.
No real money, wallet, or exchange account is involved.
"""

from __future__ import annotations

from typing import Optional

from . import config, storage
from .logger import get_logger
from .models import PortfolioSnapshot, utcnow_iso
from .paper_trader import PaperTrader

logger = get_logger("portfolio_tracker")

EQUITY_HISTORY_FILE = config.PAPER_TRADES_DIR / "equity_history.json"
SNAPSHOTS_FILE = config.REPORTS_DIR / "portfolio_snapshots.json"


class PortfolioTracker:
    def __init__(
        self,
        paper_trader: Optional[PaperTrader] = None,
        settings: Optional[config.PaperTradingSettings] = None,
    ):
        self.paper_trader = paper_trader or PaperTrader()
        self.settings = settings or config.load_settings().paper

    # ------------------------------------------------------------------
    def get_cash(self) -> float:
        trades = self.paper_trader.get_all_trades()
        spent = sum(t["size_usd"] for t in trades)
        proceeds = sum(
            t["shares"] * t["exit_price"] for t in trades if t.get("status") == "CLOSED"
        )
        return round(self.settings.starting_cash - spent + proceeds, 4)

    def get_exposure(self) -> float:
        return round(
            sum(t["size_usd"] for t in self.paper_trader.get_open_trades()), 4
        )

    def open_positions(self) -> list[dict]:
        return self.paper_trader.get_open_trades()

    def closed_positions(self) -> list[dict]:
        return self.paper_trader.get_closed_trades()

    # ------------------------------------------------------------------
    def compute_metrics(
        self, current_prices: Optional[dict[str, float]] = None
    ) -> PortfolioSnapshot:
        open_trades = self.open_positions()
        closed_trades = self.closed_positions()
        current_prices = current_prices or {}

        realized_pnl = round(
            sum(t.get("realized_pnl") or 0 for t in closed_trades), 4
        )

        open_positions_value = 0.0
        unrealized_pnl = 0.0
        for t in open_trades:
            mark = current_prices.get(t.get("token_id"), t["entry_price"])
            value = mark * t["shares"]
            open_positions_value += value
            unrealized_pnl += value - t["size_usd"]

        cash = self.get_cash()
        total_equity = round(cash + open_positions_value, 4)

        wins = [t for t in closed_trades if (t.get("realized_pnl") or 0) > 0]
        win_rate = round(len(wins) / len(closed_trades) * 100, 2) if closed_trades else 0.0

        returns = [
            (t["realized_pnl"] / t["size_usd"]) * 100
            for t in closed_trades
            if t.get("size_usd")
        ]
        avg_return_pct = round(sum(returns) / len(returns), 2) if returns else 0.0

        max_drawdown_pct = self._max_drawdown_pct(total_equity)

        return PortfolioSnapshot(
            timestamp=utcnow_iso(),
            cash=cash,
            open_positions_value=round(open_positions_value, 4),
            total_equity=total_equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=round(unrealized_pnl, 4),
            win_rate=win_rate,
            avg_return_pct=avg_return_pct,
            max_drawdown_pct=max_drawdown_pct,
            num_open_positions=len(open_trades),
            num_closed_trades=len(closed_trades),
        )

    def record_snapshot(
        self, current_prices: Optional[dict[str, float]] = None
    ) -> PortfolioSnapshot:
        """Compute current metrics, append to equity history, and persist a snapshot."""
        snapshot = self.compute_metrics(current_prices)

        history = storage.load_json(EQUITY_HISTORY_FILE, default=[])
        history.append(snapshot.total_equity)
        storage.save_json(EQUITY_HISTORY_FILE, history)

        snapshots = storage.load_json(SNAPSHOTS_FILE, default=[])
        snapshots.append(snapshot.to_dict())
        storage.save_json(SNAPSHOTS_FILE, snapshots)

        logger.info(
            "Recorded portfolio snapshot: equity=$%.2f realized_pnl=$%.2f win_rate=%.1f%%",
            snapshot.total_equity, snapshot.realized_pnl, snapshot.win_rate,
        )
        return snapshot

    # ------------------------------------------------------------------
    @staticmethod
    def _max_drawdown_pct(current_equity: float) -> float:
        history = storage.load_json(EQUITY_HISTORY_FILE, default=[])
        series = history + [current_equity]
        if not series:
            return 0.0

        peak = series[0]
        max_dd = 0.0
        for value in series:
            peak = max(peak, value)
            if peak > 0:
                drawdown = (peak - value) / peak * 100
                max_dd = max(max_dd, drawdown)
        return round(max_dd, 2)
