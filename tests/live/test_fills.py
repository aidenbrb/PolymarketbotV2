from datetime import datetime, timedelta, timezone

import pytest

from polymarket_bot import storage
from polymarket_bot.live import fills as fills_module
from polymarket_bot.live.fills import (
    already_recorded_fill_ids,
    build_fill_record,
    compute_markout_cents,
    find_due_markout_windows,
    get_all_fills,
    is_actual_fill,
    migrate_legacy_fills,
    parse_transact_time,
    record_fill,
    resolve_order_id_and_market_slug,
)
from polymarket_bot.live.models import FillRecord


@pytest.fixture
def isolated_fills(tmp_path, monkeypatch):
    monkeypatch.setattr(fills_module, "FILLS_FILE", tmp_path / "fills.json")


def _fill(fill_id="f1", market_slug="m1"):
    return FillRecord(
        fill_id=fill_id,
        order_id="o1",
        market_slug=market_slug,
        side="BUY",
        price=0.45,
        shares=10.0,
        execution_type="EXECUTION_TYPE_FILL",
        transact_time="2026-07-06T00:00:00.000000000Z",
        quoted_price=0.45,
        current_mid_at_detection=0.46,
        edge_vs_current_mid_cents=1.0,
        fill_quality="favorable",
        markout_1m_cents=None,
        markout_1m_computed_at=None,
        markout_5m_cents=None,
        markout_5m_computed_at=None,
        detected_at="2026-07-06T00:00:00+00:00",
        raw_execution={"id": fill_id},
    )


def _execution(**overrides):
    execution = {
        "id": "exec-1",
        "lastShares": "10.0",
        "lastPx": {"value": "0.45", "currency": "USD"},
        "type": "EXECUTION_TYPE_FILL",
        "tradeId": "trade-1",
    }
    execution.update(overrides)
    return execution


def _real_execution(**overrides):
    """Shaped like a real production fill -- order_id/market_slug/side/
    quoted_price nested under execution["order"], confirmed against actual
    fills.json data on 2026-07-06."""
    execution = {
        "id": "B32HEA83J5YV",
        "tradeId": "B32HEA83E5YV",
        "type": "EXECUTION_TYPE_PARTIAL_FILL",
        "lastPx": {"currency": "USD", "value": "0.6400"},
        "lastShares": "2.0000",
        "transactTime": "2026-07-06T14:20:32.560425954Z",
        "order": {
            "id": "B308RJTX05Z9",
            "marketSlug": "aqc-fifa-wc-2026-07-07-quarterq-colbia",
            "side": "ORDER_SIDE_BUY",
            "action": "ORDER_ACTION_BUY",
            "price": {"currency": "USD", "value": "0.64"},
            "quantity": 17.5,
        },
    }
    execution.update(overrides)
    return execution


class TestFillsPersistence:
    def test_record_and_read_back_fill(self, isolated_fills):
        record_fill(_fill())
        fills = get_all_fills()
        assert len(fills) == 1
        assert fills[0]["fill_id"] == "f1"
        assert fills[0]["market_slug"] == "m1"

    def test_multiple_appends_accumulate(self, isolated_fills):
        record_fill(_fill(fill_id="f1"))
        record_fill(_fill(fill_id="f2"))
        assert [f["fill_id"] for f in get_all_fills()] == ["f1", "f2"]

    def test_already_recorded_fill_ids_empty_when_no_file(self, isolated_fills):
        assert already_recorded_fill_ids() == set()

    def test_already_recorded_fill_ids_reflects_recorded_fills(self, isolated_fills):
        record_fill(_fill(fill_id="f1"))
        record_fill(_fill(fill_id="f2"))
        assert already_recorded_fill_ids() == {"f1", "f2"}

    def test_already_recorded_fill_ids_fails_closed_on_corrupt_file(self, isolated_fills, caplog):
        fills_module.FILLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        fills_module.FILLS_FILE.write_text("{not valid json", encoding="utf-8")

        with caplog.at_level("ERROR"):
            ids = already_recorded_fill_ids()

        assert ids == set()
        assert any("fills" in r.message.lower() for r in caplog.records)

    def test_get_all_fills_default_empty_when_file_missing(self, isolated_fills):
        assert get_all_fills() == []


class TestBuildFillRecord:
    def test_extracts_core_fields_from_confirmed_fixture_shape(self):
        record = build_fill_record(_execution(), order_details={}, current_bbo=None)
        assert record.fill_id == "exec-1"
        assert record.price == pytest.approx(0.45)
        assert record.shares == pytest.approx(10.0)
        assert record.execution_type == "EXECUTION_TYPE_FILL"
        assert record.raw_execution == _execution()

    def test_falls_back_to_trade_id_when_id_absent(self):
        execution = _execution()
        del execution["id"]
        record = build_fill_record(execution, order_details={}, current_bbo=None)
        assert record.fill_id == "trade-1"

    def test_order_id_none_when_neither_field_present(self):
        record = build_fill_record(_execution(), order_details={}, current_bbo=None)
        assert record.order_id is None
        assert record.market_slug is None
        assert record.side is None
        assert record.quoted_price is None

    def test_resolves_market_slug_side_and_quoted_price_when_order_id_matches(self):
        execution = _execution(orderId="order-1")
        order_details = {"order-1": {"market_id": "m1", "side": "BUY", "price": 0.44}}
        record = build_fill_record(execution, order_details=order_details, current_bbo=None)
        assert record.order_id == "order-1"
        assert record.market_slug == "m1"
        assert record.side == "BUY"
        assert record.quoted_price == pytest.approx(0.44)

    def test_client_order_id_used_when_order_id_absent(self):
        execution = _execution(clientOrderId="order-2")
        order_details = {"order-2": {"market_id": "m2", "side": "SELL", "price": 0.60}}
        record = build_fill_record(execution, order_details=order_details, current_bbo=None)
        assert record.order_id == "order-2"
        assert record.market_slug == "m2"

    def test_stays_unresolved_when_order_id_not_in_details(self):
        execution = _execution(orderId="unknown-order")
        record = build_fill_record(execution, order_details={"other": {"market_id": "m1", "side": "BUY", "price": 0.1}}, current_bbo=None)
        assert record.market_slug is None
        assert record.side is None
        assert record.quoted_price is None

    def test_favorable_fill_quality_for_buy_below_current_mid(self):
        execution = _execution(orderId="order-1", lastPx={"value": "0.40", "currency": "USD"})
        order_details = {"order-1": {"market_id": "m1", "side": "BUY", "price": 0.40}}
        current_bbo = {"best_bid": 0.44, "best_ask": 0.46}  # mid=0.45, fill 0.40 -> favorable
        record = build_fill_record(execution, order_details, current_bbo)
        assert record.fill_quality == "favorable"
        assert record.edge_vs_current_mid_cents > 0

    def test_adverse_fill_quality_for_buy_above_current_mid(self):
        execution = _execution(orderId="order-1", lastPx={"value": "0.50", "currency": "USD"})
        order_details = {"order-1": {"market_id": "m1", "side": "BUY", "price": 0.50}}
        current_bbo = {"best_bid": 0.44, "best_ask": 0.46}  # mid=0.45, fill 0.50 -> adverse
        record = build_fill_record(execution, order_details, current_bbo)
        assert record.fill_quality == "adverse"
        assert record.edge_vs_current_mid_cents < 0

    def test_favorable_fill_quality_for_sell_above_current_mid(self):
        execution = _execution(orderId="order-1", lastPx={"value": "0.50", "currency": "USD"})
        order_details = {"order-1": {"market_id": "m1", "side": "SELL", "price": 0.50}}
        current_bbo = {"best_bid": 0.44, "best_ask": 0.46}  # mid=0.45, fill 0.50 -> favorable for a SELL
        record = build_fill_record(execution, order_details, current_bbo)
        assert record.fill_quality == "favorable"

    def test_adverse_fill_quality_for_sell_below_current_mid(self):
        execution = _execution(orderId="order-1", lastPx={"value": "0.40", "currency": "USD"})
        order_details = {"order-1": {"market_id": "m1", "side": "SELL", "price": 0.40}}
        current_bbo = {"best_bid": 0.44, "best_ask": 0.46}  # mid=0.45, fill 0.40 -> adverse for a SELL
        record = build_fill_record(execution, order_details, current_bbo)
        assert record.fill_quality == "adverse"

    def test_neutral_fill_quality_within_band(self):
        execution = _execution(orderId="order-1", lastPx={"value": "0.45", "currency": "USD"})
        order_details = {"order-1": {"market_id": "m1", "side": "BUY", "price": 0.45}}
        current_bbo = {"best_bid": 0.44, "best_ask": 0.46}  # mid=0.45, fill 0.45 -> neutral
        record = build_fill_record(execution, order_details, current_bbo)
        assert record.fill_quality == "neutral"

    def test_skips_edge_and_quality_when_current_bbo_is_none(self):
        execution = _execution(orderId="order-1")
        order_details = {"order-1": {"market_id": "m1", "side": "BUY", "price": 0.45}}
        record = build_fill_record(execution, order_details, current_bbo=None)
        assert record.current_mid_at_detection is None
        assert record.edge_vs_current_mid_cents is None
        assert record.fill_quality is None

    def test_skips_edge_and_quality_when_market_slug_unresolved_even_with_bbo(self):
        execution = _execution()  # no orderId -> market_slug stays None
        record = build_fill_record(execution, order_details={}, current_bbo={"best_bid": 0.44, "best_ask": 0.46})
        assert record.fill_quality is None

    def test_coerces_string_shares_and_price(self):
        record = build_fill_record(_execution(lastShares="12.5", lastPx={"value": "0.33"}), order_details={}, current_bbo=None)
        assert record.shares == pytest.approx(12.5)
        assert record.price == pytest.approx(0.33)

    def test_malformed_price_degrades_to_none_not_raise(self):
        execution = _execution(lastPx={"value": "not-a-number"})
        record = build_fill_record(execution, order_details={}, current_bbo=None)
        assert record.price is None
        assert record.raw_execution == execution

    def test_missing_last_px_degrades_to_none_not_raise(self):
        execution = _execution()
        del execution["lastPx"]
        record = build_fill_record(execution, order_details={}, current_bbo=None)
        assert record.price is None
        assert record.shares == pytest.approx(10.0)

    def test_detected_at_is_always_populated(self):
        record = build_fill_record(_execution(), order_details={}, current_bbo=None)
        assert record.detected_at

    def test_extracts_order_id_market_slug_side_price_from_real_nested_schema(self):
        record = build_fill_record(_real_execution(), order_details={}, current_bbo=None)
        assert record.order_id == "B308RJTX05Z9"
        assert record.market_slug == "aqc-fifa-wc-2026-07-07-quarterq-colbia"
        assert record.side == "BUY"
        assert record.quoted_price == pytest.approx(0.64)
        assert record.transact_time == "2026-07-06T14:20:32.560425954Z"

    def test_nested_order_side_sell_normalizes_correctly(self):
        execution = _real_execution(order={**_real_execution()["order"], "side": "ORDER_SIDE_SELL"})
        record = build_fill_record(execution, order_details={}, current_bbo=None)
        assert record.side == "SELL"

    def test_unrecognized_order_side_value_is_none_not_misattributed(self):
        execution = _real_execution(order={**_real_execution()["order"], "side": "ORDER_SIDE_UNKNOWN"})
        record = build_fill_record(execution, order_details={}, current_bbo=None)
        assert record.side is None

    def test_unrecognized_side_keeps_fill_quality_none_even_with_bbo(self):
        execution = _real_execution(order={**_real_execution()["order"], "side": "ORDER_SIDE_UNKNOWN"})
        record = build_fill_record(execution, order_details={}, current_bbo={"best_bid": 0.60, "best_ask": 0.62})
        assert record.fill_quality is None

    def test_nested_order_price_takes_precedence_over_ledger_fallback(self):
        order_details = {"B308RJTX05Z9": {"market_id": "other-market", "side": "SELL", "price": 0.10}}
        record = build_fill_record(_real_execution(), order_details=order_details, current_bbo=None)
        assert record.quoted_price == pytest.approx(0.64)  # nested order.price wins, not the ledger's 0.10

    def test_falls_back_to_ledger_when_order_missing(self):
        execution = _execution(orderId="order-1")  # no nested "order" dict at all
        order_details = {"order-1": {"market_id": "m1", "side": "BUY", "price": 0.44}}
        record = build_fill_record(execution, order_details, current_bbo=None)
        assert record.market_slug == "m1"
        assert record.side == "BUY"
        assert record.quoted_price == pytest.approx(0.44)


class TestIsActualFill:
    def test_true_for_fill_and_partial_fill(self):
        assert is_actual_fill({"type": "EXECUTION_TYPE_FILL"}) is True
        assert is_actual_fill({"type": "EXECUTION_TYPE_PARTIAL_FILL"}) is True

    def test_false_for_lifecycle_noise(self):
        assert is_actual_fill({"type": "EXECUTION_TYPE_NEW"}) is False
        assert is_actual_fill({"type": "EXECUTION_TYPE_CANCELED"}) is False
        assert is_actual_fill({"type": "EXECUTION_TYPE_REJECTED"}) is False

    def test_false_when_type_missing(self):
        assert is_actual_fill({}) is False


class TestResolveOrderIdAndMarketSlug:
    def test_prefers_nested_order_over_ledger(self):
        order_details = {"B308RJTX05Z9": {"market_id": "other", "side": "SELL", "price": 0.1}}
        order_id, market_slug = resolve_order_id_and_market_slug(_real_execution(), order_details)
        assert order_id == "B308RJTX05Z9"
        assert market_slug == "aqc-fifa-wc-2026-07-07-quarterq-colbia"

    def test_falls_back_to_top_level_and_ledger_when_no_nested_order(self):
        execution = _execution(orderId="order-1")
        order_details = {"order-1": {"market_id": "m1", "side": "BUY", "price": 0.44}}
        order_id, market_slug = resolve_order_id_and_market_slug(execution, order_details)
        assert order_id == "order-1"
        assert market_slug == "m1"


class TestMigrateLegacyFills:
    def test_drops_non_fill_noise_and_keeps_real_fills(self):
        existing = [
            {"raw_execution": {"type": "EXECUTION_TYPE_NEW"}},
            {"raw_execution": {"type": "EXECUTION_TYPE_CANCELED"}},
            {"raw_execution": _real_execution()},
        ]
        migrated, stats = migrate_legacy_fills(existing, order_details={})
        assert stats == {"input": 3, "dropped_non_fill": 2, "migrated": 1}
        assert len(migrated) == 1
        assert migrated[0]["market_slug"] == "aqc-fifa-wc-2026-07-07-quarterq-colbia"
        assert migrated[0]["side"] == "BUY"

    def test_preserves_original_detected_at(self):
        existing = [{"raw_execution": _real_execution(), "detected_at": "2020-01-01T00:00:00+00:00"}]
        migrated, _stats = migrate_legacy_fills(existing, order_details={})
        assert migrated[0]["detected_at"] == "2020-01-01T00:00:00+00:00"

    def test_leaves_bbo_dependent_fields_none(self):
        existing = [{"raw_execution": _real_execution()}]
        migrated, _stats = migrate_legacy_fills(existing, order_details={})
        assert migrated[0]["current_mid_at_detection"] is None
        assert migrated[0]["edge_vs_current_mid_cents"] is None
        assert migrated[0]["fill_quality"] is None

    def test_preserves_existing_markout_fields(self):
        existing = [{
            "raw_execution": _real_execution(),
            "markout_1m_cents": 2.5,
            "markout_1m_computed_at": "2026-07-06T14:21:32+00:00",
        }]
        migrated, _stats = migrate_legacy_fills(existing, order_details={})
        assert migrated[0]["markout_1m_cents"] == 2.5
        assert migrated[0]["markout_1m_computed_at"] == "2026-07-06T14:21:32+00:00"
        assert migrated[0]["markout_5m_cents"] is None

    def test_skips_records_with_missing_or_malformed_raw_execution(self):
        existing = [{"raw_execution": "not-a-dict"}, {}]
        migrated, stats = migrate_legacy_fills(existing, order_details={})
        assert migrated == []
        assert stats["dropped_non_fill"] == 2

    def test_end_to_end_against_realistic_shape(self):
        # 3 noise + 1 real fill, mirroring the real file's proportions.
        existing = (
            [{"raw_execution": {"type": t}} for t in (
                "EXECUTION_TYPE_NEW", "EXECUTION_TYPE_CANCELED", "EXECUTION_TYPE_REJECTED",
            )]
            + [{"raw_execution": _real_execution()}]
        )
        migrated, stats = migrate_legacy_fills(existing, order_details={})
        assert stats["input"] == 4
        assert stats["dropped_non_fill"] == 3
        assert stats["migrated"] == 1


class TestParseTransactTime:
    def test_parses_real_nine_digit_nanosecond_format(self):
        parsed = parse_transact_time("2026-07-06T14:20:32.560425954Z")
        assert parsed == datetime(2026, 7, 6, 14, 20, 32, 560425, tzinfo=timezone.utc)

    def test_none_for_missing_value(self):
        assert parse_transact_time(None) is None
        assert parse_transact_time("") is None

    def test_none_for_garbage_value(self):
        assert parse_transact_time("not-a-timestamp") is None


class TestComputeMarkoutCents:
    def test_buy_favorable_when_mid_above_fill(self):
        assert compute_markout_cents("BUY", fill_price=0.40, mid=0.45) == pytest.approx(5.0)

    def test_buy_adverse_when_mid_below_fill(self):
        assert compute_markout_cents("BUY", fill_price=0.50, mid=0.45) == pytest.approx(-5.0)

    def test_sell_favorable_when_mid_below_fill(self):
        assert compute_markout_cents("SELL", fill_price=0.50, mid=0.45) == pytest.approx(5.0)

    def test_sell_adverse_when_mid_above_fill(self):
        assert compute_markout_cents("SELL", fill_price=0.40, mid=0.45) == pytest.approx(-5.0)

    def test_matches_build_fill_record_convention(self):
        # Same inputs used in build_fill_record's own favorable/adverse
        # tests above -- confirms one shared formula, not two that could
        # drift apart.
        assert compute_markout_cents("BUY", 0.40, 0.45) > 0
        assert compute_markout_cents("BUY", 0.50, 0.45) < 0


def _due_fill(**overrides):
    fill = {
        "market_slug": "m1",
        "side": "BUY",
        "price": 0.45,
        "transact_time": "2026-07-06T12:00:00.000000000Z",
        "markout_1m_computed_at": None,
        "markout_5m_computed_at": None,
    }
    fill.update(overrides)
    return fill


class TestFindDueMarkoutWindows:
    def _now(self, **kwargs):
        return datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc) + timedelta(**kwargs)

    def test_not_yet_due(self):
        due = find_due_markout_windows([_due_fill()], now=self._now(seconds=30), max_staleness_seconds=900.0)
        assert due == []

    def test_due_and_computable_1m(self):
        due = find_due_markout_windows([_due_fill()], now=self._now(seconds=65), max_staleness_seconds=900.0)
        windows = {d["window"]: d for d in due}
        assert windows["1m"]["status"] == "compute"
        assert "5m" not in windows  # not due yet at only 65s

    def test_due_and_computable_5m(self):
        due = find_due_markout_windows(
            [_due_fill(markout_1m_computed_at="2026-07-06T12:01:05+00:00")],
            now=self._now(seconds=305), max_staleness_seconds=900.0,
        )
        windows = {d["window"]: d for d in due}
        assert windows["5m"]["status"] == "compute"
        assert "1m" not in windows  # already resolved

    def test_due_and_stale(self):
        due = find_due_markout_windows([_due_fill()], now=self._now(hours=2), max_staleness_seconds=900.0)
        windows = {d["window"]: d for d in due}
        assert windows["1m"]["status"] == "stale"
        assert windows["5m"]["status"] == "stale"

    def test_already_resolved_window_skipped(self):
        fill = _due_fill(markout_1m_computed_at="2026-07-06T12:01:05+00:00", markout_5m_computed_at="2026-07-06T12:05:05+00:00")
        due = find_due_markout_windows([fill], now=self._now(hours=2), max_staleness_seconds=900.0)
        assert due == []

    def test_skips_fill_missing_transact_time(self):
        due = find_due_markout_windows([_due_fill(transact_time=None)], now=self._now(hours=1), max_staleness_seconds=900.0)
        assert due == []

    def test_skips_fill_missing_market_slug_or_side_or_price(self):
        for override in ({"market_slug": None}, {"side": None}, {"price": None}):
            due = find_due_markout_windows([_due_fill(**override)], now=self._now(hours=1), max_staleness_seconds=900.0)
            assert due == []

    def test_1m_and_5m_tracked_independently_in_same_result(self):
        due = find_due_markout_windows([_due_fill()], now=self._now(seconds=305), max_staleness_seconds=900.0)
        assert {d["window"] for d in due} == {"1m", "5m"}

    def test_index_matches_position_in_input_list(self):
        fills = [_due_fill(market_slug="m1"), _due_fill(market_slug="m2")]
        due = find_due_markout_windows(fills, now=self._now(seconds=65), max_staleness_seconds=900.0)
        assert {d["index"] for d in due} == {0, 1}
