from unittest.mock import Mock

import pytest

from polymarket_bot import storage
from polymarket_bot.live import ledger as ledger_module
from polymarket_bot.live.ledger import (
    estimate_daily_pnl_usd,
    get_all_cycles,
    get_cycles_since,
    get_known_order_details,
    get_known_order_id_markets,
    get_known_order_ids,
    get_total_position_pnl_usd,
    record_cycle,
)
from polymarket_bot.live.models import LiveQuoteCycle, PostedLeg
from polymarket_bot.live.us_client import UsApiError


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, "LEDGER_FILE", tmp_path / "orders.json")
    monkeypatch.setattr(ledger_module, "DAILY_BALANCE_FILE", tmp_path / "daily_pnl_baseline.json")


def _position(cost, cash_value, realized=0.0):
    return {
        "cost": {"value": str(cost), "currency": "USD"},
        "cashValue": {"value": str(cash_value), "currency": "USD"},
        "realized": {"value": str(realized), "currency": "USD"},
    }


def _cycle(cycle_id="c1", timestamp="2026-07-04T00:00:00+00:00"):
    return LiveQuoteCycle(
        cycle_id=cycle_id,
        market_id="m1",
        reference_price=0.5,
        tick_size=0.01,
        bid=PostedLeg(side="BUY", price=0.49, size=100.0, order_id="b1"),
        ask=PostedLeg(side="SELL", price=0.52, size=100.0, order_id="a1"),
        timestamp=timestamp,
    )


def test_record_and_read_back_cycle(isolated_ledger):
    record_cycle(_cycle())
    cycles = get_all_cycles()
    assert len(cycles) == 1
    assert cycles[0]["cycle_id"] == "c1"
    assert cycles[0]["bid"]["order_id"] == "b1"


def test_multiple_cycles_append(isolated_ledger):
    record_cycle(_cycle(cycle_id="c1", timestamp="2026-07-04T00:00:00+00:00"))
    record_cycle(_cycle(cycle_id="c2", timestamp="2026-07-04T01:00:00+00:00"))
    assert len(get_all_cycles()) == 2


def test_get_known_order_ids_collects_bid_and_ask_across_all_cycles(isolated_ledger):
    record_cycle(_cycle(cycle_id="c1"))  # bid="b1", ask="a1"
    record_cycle(_cycle(cycle_id="c2"))  # same ids again -- must dedupe via a set

    assert get_known_order_ids() == {"b1", "a1"}


def test_get_known_order_ids_ignores_legs_with_no_order_id(isolated_ledger):
    cycle = LiveQuoteCycle(
        cycle_id="c1",
        market_id="m1",
        reference_price=0.5,
        tick_size=0.01,
        bid=PostedLeg(side="BUY", price=0.49, size=100.0, order_id=None, error="skipped: no edge"),
        ask=PostedLeg(side="SELL", price=0.52, size=100.0, order_id="a1"),
        timestamp="2026-07-04T00:00:00+00:00",
    )
    record_cycle(cycle)

    assert get_known_order_ids() == {"a1"}


def test_get_known_order_ids_empty_when_no_cycles_recorded(isolated_ledger):
    assert get_known_order_ids() == set()


def test_get_known_order_ids_skips_malformed_leg_but_keeps_valid_ones(isolated_ledger):
    # A single corrupted record (e.g. a non-dict leg value) must not lose
    # the rest of the ledger's legitimate, already-recorded order ids.
    storage.save_json(ledger_module.LEDGER_FILE, [
        {"bid": "not-a-dict", "ask": {"order_id": "a1"}},
    ])

    assert get_known_order_ids() == {"a1"}


def test_get_known_order_ids_fails_closed_when_ledger_is_unreadable(isolated_ledger, caplog):
    # Simulate a genuinely corrupted/truncated ledger file (invalid JSON) --
    # this must never raise out of get_known_order_ids(). It must log
    # loudly and return an EMPTY set, which is the safe degradation: an
    # empty set makes every order look "unrecognized" to the ownership
    # check, so nothing gets cancelled based on incomplete data.
    ledger_module.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    ledger_module.LEDGER_FILE.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level("ERROR"):
        order_ids = get_known_order_ids()

    assert order_ids == set()
    assert any("ledger" in r.message.lower() for r in caplog.records)


def test_get_known_order_id_markets_collects_bid_and_ask_across_all_cycles(isolated_ledger):
    record_cycle(_cycle(cycle_id="c1"))  # bid="b1", ask="a1", market="m1"
    record_cycle(_cycle(cycle_id="c2"))  # same ids again -- must dedupe like a set would

    assert get_known_order_id_markets() == {"b1": "m1", "a1": "m1"}


def test_get_known_order_id_markets_ignores_legs_with_no_order_id(isolated_ledger):
    cycle = LiveQuoteCycle(
        cycle_id="c1",
        market_id="m1",
        reference_price=0.5,
        tick_size=0.01,
        bid=PostedLeg(side="BUY", price=0.49, size=100.0, order_id=None, error="skipped: no edge"),
        ask=PostedLeg(side="SELL", price=0.52, size=100.0, order_id="a1"),
        timestamp="2026-07-04T00:00:00+00:00",
    )
    record_cycle(cycle)

    assert get_known_order_id_markets() == {"a1": "m1"}


def test_get_known_order_id_markets_empty_when_no_cycles_recorded(isolated_ledger):
    assert get_known_order_id_markets() == {}


def test_get_known_order_id_markets_skips_malformed_leg_but_keeps_valid_ones(isolated_ledger):
    storage.save_json(ledger_module.LEDGER_FILE, [
        {"market_id": "m1", "bid": "not-a-dict", "ask": {"order_id": "a1"}},
    ])

    assert get_known_order_id_markets() == {"a1": "m1"}


def test_get_known_order_id_markets_fails_closed_when_ledger_is_unreadable(isolated_ledger, caplog):
    ledger_module.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    ledger_module.LEDGER_FILE.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level("ERROR"):
        order_markets = get_known_order_id_markets()

    assert order_markets == {}
    assert any("ledger" in r.message.lower() for r in caplog.records)


def test_get_known_order_id_markets_maps_two_cycles_same_market_correctly(isolated_ledger):
    # Two distinct quote cycles for the same market -- different order ids,
    # both must map back to that one market slug.
    cycle_1 = LiveQuoteCycle(
        cycle_id="c1",
        market_id="m1",
        reference_price=0.5,
        tick_size=0.01,
        bid=PostedLeg(side="BUY", price=0.49, size=100.0, order_id="b1"),
        ask=PostedLeg(side="SELL", price=0.52, size=100.0, order_id="a1"),
        timestamp="2026-07-04T00:00:00+00:00",
    )
    cycle_2 = LiveQuoteCycle(
        cycle_id="c2",
        market_id="m1",
        reference_price=0.5,
        tick_size=0.01,
        bid=PostedLeg(side="BUY", price=0.48, size=100.0, order_id="b2"),
        ask=PostedLeg(side="SELL", price=0.53, size=100.0, order_id="a2"),
        timestamp="2026-07-04T01:00:00+00:00",
    )
    record_cycle(cycle_1)
    record_cycle(cycle_2)

    assert get_known_order_id_markets() == {"b1": "m1", "a1": "m1", "b2": "m1", "a2": "m1"}


def test_get_known_order_details_collects_bid_and_ask_across_all_cycles(isolated_ledger):
    record_cycle(_cycle(cycle_id="c1"))  # bid="b1"@0.49 BUY, ask="a1"@0.52 SELL, market="m1"
    record_cycle(_cycle(cycle_id="c2"))  # same ids again -- must dedupe like a set would

    details = get_known_order_details()
    assert details == {
        "b1": {"market_id": "m1", "side": "BUY", "price": 0.49},
        "a1": {"market_id": "m1", "side": "SELL", "price": 0.52},
    }


def test_get_known_order_details_ignores_legs_with_no_order_id(isolated_ledger):
    cycle = LiveQuoteCycle(
        cycle_id="c1",
        market_id="m1",
        reference_price=0.5,
        tick_size=0.01,
        bid=PostedLeg(side="BUY", price=0.49, size=100.0, order_id=None, error="skipped: no edge"),
        ask=PostedLeg(side="SELL", price=0.52, size=100.0, order_id="a1"),
        timestamp="2026-07-04T00:00:00+00:00",
    )
    record_cycle(cycle)

    assert get_known_order_details() == {"a1": {"market_id": "m1", "side": "SELL", "price": 0.52}}


def test_get_known_order_details_empty_when_no_cycles_recorded(isolated_ledger):
    assert get_known_order_details() == {}


def test_get_known_order_details_skips_malformed_leg_but_keeps_valid_ones(isolated_ledger):
    storage.save_json(ledger_module.LEDGER_FILE, [
        {"market_id": "m1", "bid": "not-a-dict", "ask": {"order_id": "a1", "side": "SELL", "price": 0.52}},
    ])

    assert get_known_order_details() == {"a1": {"market_id": "m1", "side": "SELL", "price": 0.52}}


def test_get_known_order_details_fails_closed_when_ledger_is_unreadable(isolated_ledger, caplog):
    ledger_module.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    ledger_module.LEDGER_FILE.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level("ERROR"):
        details = get_known_order_details()

    assert details == {}
    assert any("ledger" in r.message.lower() for r in caplog.records)


def test_get_known_order_details_maps_two_cycles_same_market_correctly(isolated_ledger):
    cycle_1 = LiveQuoteCycle(
        cycle_id="c1",
        market_id="m1",
        reference_price=0.5,
        tick_size=0.01,
        bid=PostedLeg(side="BUY", price=0.49, size=100.0, order_id="b1"),
        ask=PostedLeg(side="SELL", price=0.52, size=100.0, order_id="a1"),
        timestamp="2026-07-04T00:00:00+00:00",
    )
    cycle_2 = LiveQuoteCycle(
        cycle_id="c2",
        market_id="m1",
        reference_price=0.5,
        tick_size=0.01,
        bid=PostedLeg(side="BUY", price=0.48, size=100.0, order_id="b2"),
        ask=PostedLeg(side="SELL", price=0.53, size=100.0, order_id="a2"),
        timestamp="2026-07-04T01:00:00+00:00",
    )
    record_cycle(cycle_1)
    record_cycle(cycle_2)

    details = get_known_order_details()
    assert details["b1"] == {"market_id": "m1", "side": "BUY", "price": 0.49}
    assert details["a1"] == {"market_id": "m1", "side": "SELL", "price": 0.52}
    assert details["b2"] == {"market_id": "m1", "side": "BUY", "price": 0.48}
    assert details["a2"] == {"market_id": "m1", "side": "SELL", "price": 0.53}


def test_get_known_order_id_markets_built_from_order_details(isolated_ledger):
    record_cycle(_cycle(cycle_id="c1"))
    assert get_known_order_id_markets() == {"b1": "m1", "a1": "m1"}


def test_get_cycles_since_filters_by_timestamp(isolated_ledger):
    record_cycle(_cycle(cycle_id="old", timestamp="2026-07-04T00:00:00+00:00"))
    record_cycle(_cycle(cycle_id="new", timestamp="2026-07-04T12:00:00+00:00"))
    recent = get_cycles_since("2026-07-04T06:00:00+00:00")
    assert [c["cycle_id"] for c in recent] == ["new"]


def test_get_total_position_pnl_usd_sums_unrealized_and_realized(isolated_ledger):
    client = Mock()
    client.get_all_positions.return_value = {
        "m1": _position(cost=10.0, cash_value=10.5, realized=1.0),
        "m2": _position(cost=5.0, cash_value=4.5),
    }
    assert get_total_position_pnl_usd(client) == pytest.approx(1.0)


def test_get_total_position_pnl_usd_returns_none_when_fetch_fails(isolated_ledger):
    client = Mock()
    client.get_all_positions.side_effect = UsApiError("network error")
    assert get_total_position_pnl_usd(client) is None


def test_get_total_position_pnl_usd_does_not_reset_across_days(isolated_ledger):
    # Unlike estimate_daily_pnl_usd, this must NOT diff against any baseline
    # -- it's the raw cumulative figure equity_protection.py needs for a
    # multi-day peak-account-value check.
    client = Mock()
    client.get_all_positions.return_value = {"m1": _position(cost=10.0, cash_value=15.0)}
    first = get_total_position_pnl_usd(client)
    second = get_total_position_pnl_usd(client)
    assert first == second == pytest.approx(5.0)


def test_estimate_daily_pnl_first_call_establishes_baseline(isolated_ledger):
    client = Mock()
    client.get_all_positions.return_value = {"m1": _position(cost=10.0, cash_value=9.0)}
    pnl = estimate_daily_pnl_usd(client)
    assert pnl == 0.0


def test_estimate_daily_pnl_reflects_position_value_change_from_baseline(isolated_ledger):
    client = Mock()
    client.get_all_positions.return_value = {"m1": _position(cost=10.0, cash_value=10.0)}
    estimate_daily_pnl_usd(client)  # establishes baseline P/L of 0

    client.get_all_positions.return_value = {"m1": _position(cost=10.0, cash_value=8.0)}
    pnl = estimate_daily_pnl_usd(client)
    assert pnl == pytest.approx(-2.0)


def test_estimate_daily_pnl_sums_unrealized_across_multiple_positions(isolated_ledger):
    # This is the exact scenario that mattered in practice: many resting
    # orders/positions across many markets shouldn't look like one big loss
    # just because there are several of them -- only their actual P/L counts.
    client = Mock()
    client.get_all_positions.return_value = {
        "m1": _position(cost=10.0, cash_value=10.2),
        "m2": _position(cost=5.0, cash_value=4.8),
    }
    pnl = estimate_daily_pnl_usd(client)
    assert pnl == 0.0  # baseline call

    client.get_all_positions.return_value = {
        "m1": _position(cost=10.0, cash_value=10.5),
        "m2": _position(cost=5.0, cash_value=4.5),
    }
    pnl = estimate_daily_pnl_usd(client)
    # m1 improved by 0.3, m2 worsened by 0.3 -- net unchanged.
    assert pnl == pytest.approx(0.0)


def test_estimate_daily_pnl_includes_realized_pnl(isolated_ledger):
    client = Mock()
    client.get_all_positions.return_value = {"m1": _position(cost=10.0, cash_value=10.0, realized=0.0)}
    estimate_daily_pnl_usd(client)  # baseline

    client.get_all_positions.return_value = {"m1": _position(cost=10.0, cash_value=10.0, realized=-5.0)}
    pnl = estimate_daily_pnl_usd(client)
    assert pnl == pytest.approx(-5.0)


def test_estimate_daily_pnl_is_not_affected_by_reserved_margin_alone(isolated_ledger):
    # Regression test for the real 2026-07-05 false trip: posting more
    # resting orders reserves buying power/margin but must NOT look like a
    # loss when no position's actual cost-vs-value has changed.
    client = Mock()
    client.get_all_positions.return_value = {"m1": _position(cost=10.0, cash_value=10.0)}
    estimate_daily_pnl_usd(client)

    # Same position, unchanged -- as if 9 more markets' worth of resting
    # orders were posted elsewhere, reserving margin, with nothing realized.
    client.get_all_positions.return_value = {"m1": _position(cost=10.0, cash_value=10.0)}
    pnl = estimate_daily_pnl_usd(client)
    assert pnl == 0.0


def test_estimate_daily_pnl_returns_none_when_fetch_fails(isolated_ledger):
    client = Mock()
    client.get_all_positions.side_effect = UsApiError("network error")
    assert estimate_daily_pnl_usd(client) is None
