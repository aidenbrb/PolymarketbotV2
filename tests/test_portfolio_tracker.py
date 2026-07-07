import pytest

from polymarket_bot import config, paper_trader as paper_trader_module
from polymarket_bot import portfolio_tracker as portfolio_tracker_module
from polymarket_bot.paper_trader import PaperTrader
from polymarket_bot.portfolio_tracker import PortfolioTracker


@pytest.fixture
def isolated_paper_files(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trader_module, "TRADES_FILE", tmp_path / "trades.json")
    monkeypatch.setattr(portfolio_tracker_module, "EQUITY_HISTORY_FILE", tmp_path / "equity_history.json")
    monkeypatch.setattr(portfolio_tracker_module, "SNAPSHOTS_FILE", tmp_path / "snapshots.json")


def _trader_and_tracker(starting_cash=1000.0):
    settings = config.PaperTradingSettings(
        starting_cash=starting_cash, default_mode="manual_paper", fake_slippage_bps=0.0
    )
    trader = PaperTrader(settings=settings)
    tracker = PortfolioTracker(paper_trader=trader, settings=settings)
    return trader, tracker


def test_starting_cash_with_no_trades(isolated_paper_files):
    _, tracker = _trader_and_tracker(starting_cash=1000.0)
    assert tracker.get_cash() == 1000.0
    snapshot = tracker.compute_metrics()
    assert snapshot.total_equity == 1000.0
    assert snapshot.num_open_positions == 0
    assert snapshot.num_closed_trades == 0


def test_cash_decreases_on_open_and_recovers_on_close(isolated_paper_files):
    trader, tracker = _trader_and_tracker(starting_cash=1000.0)
    trade = trader.open_trade("m1", "t1", "Yes", requested_price=0.5, size_usd=100.0, reason_entry="e")
    assert tracker.get_cash() == 900.0

    trader.close_trade(trade.trade_id, requested_exit_price=0.6, reason_exit="x")
    # proceeds = shares(200) * exit_price(0.6) = 120
    assert tracker.get_cash() == pytest.approx(1020.0)


def test_realized_pnl_and_win_rate(isolated_paper_files):
    trader, tracker = _trader_and_tracker()
    winner = trader.open_trade("m1", "t1", "Yes", requested_price=0.5, size_usd=100.0, reason_entry="e")
    trader.close_trade(winner.trade_id, requested_exit_price=0.7, reason_exit="win")

    loser = trader.open_trade("m2", "t2", "No", requested_price=0.5, size_usd=100.0, reason_entry="e")
    trader.close_trade(loser.trade_id, requested_exit_price=0.4, reason_exit="loss")

    snapshot = tracker.compute_metrics()
    assert snapshot.num_closed_trades == 2
    assert snapshot.win_rate == 50.0
    assert snapshot.realized_pnl == pytest.approx(40.0 - 20.0)


def test_unrealized_pnl_uses_current_prices(isolated_paper_files):
    trader, tracker = _trader_and_tracker()
    trade = trader.open_trade("m1", "t1", "Yes", requested_price=0.5, size_usd=100.0, reason_entry="e")
    snapshot = tracker.compute_metrics(current_prices={"t1": 0.6})
    # shares = 200, mark value = 120, cost basis = 100 -> unrealized pnl = 20
    assert snapshot.unrealized_pnl == pytest.approx(20.0)
    assert snapshot.num_open_positions == 1


def test_record_snapshot_persists_equity_history(isolated_paper_files):
    _, tracker = _trader_and_tracker()
    tracker.record_snapshot()
    tracker.record_snapshot()
    from polymarket_bot import storage
    history = storage.load_json(portfolio_tracker_module.EQUITY_HISTORY_FILE, default=[])
    assert len(history) == 2
