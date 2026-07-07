from polymarket_bot import config
from polymarket_bot.risk_manager import RiskManager


def _settings() -> config.RiskSettings:
    return config.RiskSettings(
        max_position_size_usd=100.0,
        max_open_positions=3,
        max_total_exposure_usd=250.0,
        max_position_pct_of_cash=0.5,
    )


def test_allows_reasonable_trade():
    manager = RiskManager(_settings())
    result = manager.check_new_position(
        size_usd=50, current_open_positions=0, current_exposure_usd=0, current_cash=1000
    )
    assert result.allowed
    assert result.reasons == []


def test_blocks_oversized_position():
    manager = RiskManager(_settings())
    result = manager.check_new_position(
        size_usd=500, current_open_positions=0, current_exposure_usd=0, current_cash=1000
    )
    assert not result.allowed
    assert any("exceeds max position size" in r for r in result.reasons)


def test_blocks_when_at_max_open_positions():
    manager = RiskManager(_settings())
    result = manager.check_new_position(
        size_usd=50, current_open_positions=3, current_exposure_usd=0, current_cash=1000
    )
    assert not result.allowed
    assert any("max open positions" in r for r in result.reasons)


def test_blocks_when_exceeding_total_exposure():
    manager = RiskManager(_settings())
    result = manager.check_new_position(
        size_usd=50, current_open_positions=0, current_exposure_usd=220, current_cash=1000
    )
    assert not result.allowed
    assert any("total exposure" in r for r in result.reasons)


def test_blocks_when_exceeding_cash_percentage():
    manager = RiskManager(_settings())
    result = manager.check_new_position(
        size_usd=90, current_open_positions=0, current_exposure_usd=0, current_cash=100
    )
    assert not result.allowed
    assert any("% of current cash" in r for r in result.reasons)


def test_blocks_when_exceeding_available_cash():
    manager = RiskManager(_settings())
    result = manager.check_new_position(
        size_usd=60, current_open_positions=0, current_exposure_usd=0, current_cash=50
    )
    assert not result.allowed
    assert any("available paper cash" in r for r in result.reasons)


def test_blocks_non_positive_size():
    manager = RiskManager(_settings())
    result = manager.check_new_position(
        size_usd=0, current_open_positions=0, current_exposure_usd=0, current_cash=1000
    )
    assert not result.allowed
