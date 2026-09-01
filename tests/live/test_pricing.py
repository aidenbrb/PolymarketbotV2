import pytest

from polymarket_bot.live.pricing import (
    _ceil_to_tick,
    _floor_to_tick,
    apply_cost_basis_floor,
    apply_liquidation_limit,
    compute_book_aware_quote,
    compute_quote,
    floor_to_quantity_step,
    round_to_tick,
    usd_to_shares,
)


def test_liquidation_limit_allows_bounded_loss_for_long():
    assert apply_liquidation_limit(
        0.45, net_position=10, avg_cost=0.50, tick_size=0.01,
        allowed_loss_cents=3.0, max_total_loss_usd=2.0,
    ) == pytest.approx(0.47)


def test_liquidation_limit_total_dollar_cap_tightens_large_position():
    assert apply_liquidation_limit(
        0.40, net_position=100, avg_cost=0.50, tick_size=0.01,
        allowed_loss_cents=5.0, max_total_loss_usd=2.0,
    ) == pytest.approx(0.48)


def test_liquidation_limit_allows_bounded_loss_for_short():
    assert apply_liquidation_limit(
        0.60, net_position=-10, avg_cost=0.50, tick_size=0.01,
        allowed_loss_cents=4.0, max_total_loss_usd=2.0,
    ) == pytest.approx(0.54)


@pytest.mark.parametrize(
    "price,tick_size,expected",
    [
        (0.503, 0.01, 0.50),
        (0.505, 0.01, 0.51),  # ROUND_HALF_UP
        (0.1234, 0.001, 0.123),
        (0.30000000000000004, 0.01, 0.30),
        (0.0623, 0.0025, 0.0625),
    ],
)
def test_round_to_tick(price, tick_size, expected):
    assert round_to_tick(price, tick_size) == pytest.approx(expected)


def test_round_to_tick_rejects_non_positive_tick():
    with pytest.raises(ValueError):
        round_to_tick(0.5, 0)


def test_compute_quote_splits_evenly_around_midpoint():
    # raw bid/ask (0.485/0.515) land exactly halfway between ticks, so
    # ROUND_HALF_UP pushes both up a tick: 0.49 / 0.52 (spread stays 0.03).
    quote = compute_quote(reference_price=0.50, tick_size=0.01, spread_cents=3.0)
    assert quote.bid == pytest.approx(0.49)
    assert quote.ask == pytest.approx(0.52)
    assert quote.ask - quote.bid == pytest.approx(0.03)


def test_compute_quote_clamps_near_upper_edge():
    quote = compute_quote(reference_price=0.99, tick_size=0.01, spread_cents=3.0)
    assert quote.ask <= 0.99
    assert quote.bid < quote.ask


def test_compute_quote_clamps_near_lower_edge():
    quote = compute_quote(reference_price=0.02, tick_size=0.01, spread_cents=3.0)
    assert quote.bid >= 0.01
    assert quote.bid < quote.ask


def test_compute_quote_forces_minimum_tick_separation():
    # Wide tick size relative to spread can collapse bid/ask onto the same tick.
    quote = compute_quote(reference_price=0.50, tick_size=0.1, spread_cents=3.0)
    assert quote.bid < quote.ask


def test_compute_quote_rejects_out_of_range_reference_price():
    with pytest.raises(ValueError):
        compute_quote(reference_price=1.0, tick_size=0.01)
    with pytest.raises(ValueError):
        compute_quote(reference_price=0.0, tick_size=0.01)


def test_book_aware_quote_improves_both_sides_by_one_tick():
    quote = compute_book_aware_quote(best_bid=0.100, best_ask=0.117, tick_size=0.001)
    assert quote is not None
    assert quote.bid == pytest.approx(0.101)
    assert quote.ask == pytest.approx(0.116)


def test_book_aware_quote_returns_none_when_book_is_only_one_tick_wide():
    # best_bid/best_ask adjacent -- improving both sides crosses, no edge at all.
    quote = compute_book_aware_quote(best_bid=0.100, best_ask=0.101, tick_size=0.001)
    assert quote is None


def test_book_aware_quote_returns_none_when_remaining_edge_below_minimum():
    # 2-tick-wide book: improving both sides leaves exactly 0 captured spread.
    quote = compute_book_aware_quote(
        best_bid=0.100, best_ask=0.102, tick_size=0.001, min_edge_cents=0.05
    )
    assert quote is None


def test_book_aware_quote_respects_custom_min_edge():
    # 1.7c-wide book (matches a real market seen in testing) with a 0.1c tick.
    quote = compute_book_aware_quote(
        best_bid=0.1210, best_ask=0.1380, tick_size=0.001, min_edge_cents=1.0
    )
    assert quote is not None
    assert quote.bid == pytest.approx(0.122)
    assert quote.ask == pytest.approx(0.137)
    assert (quote.ask - quote.bid) * 100 >= 1.0


def test_book_aware_quote_never_quotes_below_absolute_exchange_minimum():
    quote = compute_book_aware_quote(best_bid=0.008, best_ask=0.025, tick_size=0.001)
    assert quote is not None
    assert quote.bid >= 0.01


def test_book_aware_quote_never_quotes_above_absolute_exchange_maximum():
    # Mirror of the bid-side minimum test above -- the ask must be clamped
    # to the same 0.99 ceiling the bid is clamped to on the low side.
    # best_ask - tick_size (0.997) would otherwise exceed 0.99 uncapped.
    quote = compute_book_aware_quote(best_bid=0.90, best_ask=0.998, tick_size=0.001)
    assert quote is not None
    assert quote.ask <= 0.99


def test_book_aware_quote_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        compute_book_aware_quote(best_bid=0.5, best_ask=0.4, tick_size=0.001)
    with pytest.raises(ValueError):
        compute_book_aware_quote(best_bid=0, best_ask=0.5, tick_size=0.001)
    with pytest.raises(ValueError):
        compute_book_aware_quote(best_bid=0.4, best_ask=0.5, tick_size=0)


def test_usd_to_shares():
    assert usd_to_shares(100.0, 0.50) == pytest.approx(200.0)


def test_usd_to_shares_rejects_non_positive_inputs():
    with pytest.raises(ValueError):
        usd_to_shares(0, 0.5)
    with pytest.raises(ValueError):
        usd_to_shares(100, 0)


def test_floor_to_quantity_step_rounds_down_to_market_minimum():
    assert floor_to_quantity_step(17.5, 1.0) == pytest.approx(17.0)
    assert floor_to_quantity_step(17.5, 0.01) == pytest.approx(17.5)


def test_floor_to_quantity_step_rejects_non_positive_inputs():
    with pytest.raises(ValueError):
        floor_to_quantity_step(10, 0)
    with pytest.raises(ValueError):
        floor_to_quantity_step(0, 1)


def test_ceil_to_tick_rounds_up():
    assert _ceil_to_tick(0.503, 0.01) == pytest.approx(0.51)


def test_floor_to_tick_rounds_down():
    assert _floor_to_tick(0.503, 0.01) == pytest.approx(0.50)


def test_apply_cost_basis_floor_no_position_returns_price_unchanged():
    assert apply_cost_basis_floor(0.51, net_position=0, avg_cost=None, tick_size=0.01) == 0.51


def test_apply_cost_basis_floor_no_avg_cost_returns_price_unchanged():
    # Position API reachable but avg cost unknown -- don't guess, behave as before.
    assert apply_cost_basis_floor(0.51, net_position=100, avg_cost=None, tick_size=0.01) == 0.51


def test_apply_cost_basis_floor_long_raises_price_that_would_lock_in_a_loss():
    # Long at 0.55 avg cost; book-implied ask (0.51) would sell at a loss.
    adjusted = apply_cost_basis_floor(0.51, net_position=100, avg_cost=0.55, tick_size=0.01)
    assert adjusted == pytest.approx(0.55)


def test_apply_cost_basis_floor_long_leaves_profitable_price_untouched():
    # Book-implied ask (0.60) is already above avg cost -- no adjustment needed.
    adjusted = apply_cost_basis_floor(0.60, net_position=100, avg_cost=0.55, tick_size=0.01)
    assert adjusted == pytest.approx(0.60)


def test_apply_cost_basis_floor_long_returns_none_when_floor_is_out_of_bounds():
    # Cost basis above the highest tradeable price -- no valid exit exists this cycle.
    adjusted = apply_cost_basis_floor(0.50, net_position=100, avg_cost=0.999, tick_size=0.01)
    assert adjusted is None


def test_apply_cost_basis_floor_short_caps_price_that_would_lock_in_a_loss():
    # Short at 0.45 avg cost; book-implied bid (0.49) would buy back at a loss.
    adjusted = apply_cost_basis_floor(0.49, net_position=-100, avg_cost=0.45, tick_size=0.01)
    assert adjusted == pytest.approx(0.45)


def test_apply_cost_basis_floor_short_leaves_profitable_price_untouched():
    adjusted = apply_cost_basis_floor(0.40, net_position=-100, avg_cost=0.45, tick_size=0.01)
    assert adjusted == pytest.approx(0.40)


def test_apply_cost_basis_floor_short_returns_none_when_cap_is_out_of_bounds():
    adjusted = apply_cost_basis_floor(0.50, net_position=-100, avg_cost=0.001, tick_size=0.01)
    assert adjusted is None
