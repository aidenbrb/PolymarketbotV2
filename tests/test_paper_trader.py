import pytest

from polymarket_bot import config, paper_trader as paper_trader_module
from polymarket_bot.paper_trader import PaperTrader


@pytest.fixture
def isolated_trades_file(tmp_path, monkeypatch):
    trades_file = tmp_path / "trades.json"
    monkeypatch.setattr(paper_trader_module, "TRADES_FILE", trades_file)
    return trades_file


def _trader(slippage_bps=0.0):
    settings = config.PaperTradingSettings(
        starting_cash=1000.0, default_mode="manual_paper", fake_slippage_bps=slippage_bps
    )
    return PaperTrader(settings=settings)


def test_open_trade_records_fake_trade(isolated_trades_file):
    trader = _trader()
    trade = trader.open_trade(
        market_id="m1",
        token_id="t1",
        outcome="Yes",
        requested_price=0.50,
        size_usd=100.0,
        reason_entry="test entry",
    )
    assert trade.status == "OPEN"
    assert trade.entry_price == 0.50
    assert trade.shares == 200.0

    open_trades = trader.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["market_id"] == "m1"


def test_open_trade_applies_slippage(isolated_trades_file):
    trader = _trader(slippage_bps=100)  # 1%
    trade = trader.open_trade(
        market_id="m1", token_id="t1", outcome="Yes",
        requested_price=0.50, size_usd=100.0, reason_entry="slippage test",
    )
    assert trade.entry_price == pytest.approx(0.505, rel=1e-6)


def test_close_trade_computes_realized_pnl(isolated_trades_file):
    trader = _trader()
    trade = trader.open_trade(
        market_id="m1", token_id="t1", outcome="Yes",
        requested_price=0.50, size_usd=100.0, reason_entry="entry",
    )
    closed = trader.close_trade(trade.trade_id, requested_exit_price=0.60, reason_exit="target hit")

    assert closed.status == "CLOSED"
    assert closed.exit_price == 0.60
    expected_pnl = (0.60 - 0.50) * 200.0
    assert closed.realized_pnl == pytest.approx(expected_pnl, rel=1e-6)

    assert trader.get_open_trades() == []
    assert len(trader.get_closed_trades()) == 1


def test_close_unknown_trade_raises(isolated_trades_file):
    trader = _trader()
    with pytest.raises(ValueError):
        trader.close_trade("does-not-exist", 0.5, "n/a")


def test_open_trade_rejects_non_positive_inputs(isolated_trades_file):
    trader = _trader()
    with pytest.raises(ValueError):
        trader.open_trade("m1", "t1", "Yes", requested_price=0, size_usd=10, reason_entry="x")
    with pytest.raises(ValueError):
        trader.open_trade("m1", "t1", "Yes", requested_price=0.5, size_usd=0, reason_entry="x")
