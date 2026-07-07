from polymarket_bot.live.reconciliation import reconcile


def _order(order_id=None, order_id_alt=None, slug=None, slug_alt=None):
    order = {}
    if order_id is not None:
        order["id"] = order_id
    if order_id_alt is not None:
        order["orderId"] = order_id_alt
    if slug is not None:
        order["marketSlug"] = slug
    if slug_alt is not None:
        order["market_slug"] = slug_alt
    return order


def _position(net):
    return {"netPositionDecimal": net}


def test_bot_owned_order_recognized_by_id():
    order = _order(order_id="b1", slug="m1")
    report = reconcile([order], {"b1": "m1"}, {})

    assert report.bot_owned_orders == [order]
    assert report.unknown_orders == []


def test_order_id_only_field_is_recognized():
    order = _order(order_id_alt="b1", slug="m1")
    report = reconcile([order], {"b1": "m1"}, {})

    assert report.bot_owned_orders == [order]


def test_market_slug_only_field_is_used_for_unmanaged_check():
    order = _order(order_id="b1", slug_alt="m1")
    report = reconcile([order], {"b1": "m1"}, {"m1": _position(5)})

    assert report.bot_owned_orders == [order]
    assert report.unmanaged_positions == []  # m1 has a bot-owned order via market_slug


def test_unknown_order_not_in_known_ids():
    order = _order(order_id="x1", slug="m1")
    report = reconcile([order], {}, {})

    assert report.unknown_orders == [order]
    assert report.bot_owned_orders == []


def test_order_missing_both_id_fields_is_counted_not_misclassified():
    order = _order(slug="m1")
    report = reconcile([order], {}, {})

    assert report.orders_missing_id_count == 1
    assert report.unknown_orders == []
    assert report.bot_owned_orders == []


def test_stale_ledger_orders_carry_market_slug():
    report = reconcile([], {"b1": "m1"}, {})

    assert report.stale_ledger_orders == [{"order_id": "b1", "market_slug": "m1"}]


def test_stale_ledger_orders_handle_missing_slug():
    report = reconcile([], {"b1": None}, {})

    assert report.stale_ledger_orders == [{"order_id": "b1", "market_slug": None}]


def test_stale_ledger_orders_preserve_insertion_order():
    known = {"b1": "m1", "b2": "m2", "b3": "m3"}
    report = reconcile([], known, {})

    assert [s["order_id"] for s in report.stale_ledger_orders] == ["b1", "b2", "b3"]


def test_known_order_still_open_is_not_stale():
    order = _order(order_id="b1", slug="m1")
    report = reconcile([order], {"b1": "m1"}, {})

    assert report.stale_ledger_orders == []


def test_unmanaged_position_detected_when_no_bot_owned_order():
    report = reconcile([], {}, {"m1": _position(10)})

    assert report.unmanaged_positions == [{"netPositionDecimal": 10, "market_slug": "m1"}]


def test_position_managed_by_bot_owned_order_not_flagged():
    order = _order(order_id="b1", slug="m1")
    report = reconcile([order], {"b1": "m1"}, {"m1": _position(10)})

    assert report.unmanaged_positions == []


def test_position_still_unmanaged_despite_unknown_order_on_same_slug():
    # An unrelated manual/unrecognized order sharing the slug must not hide
    # a genuinely unmanaged position -- only a BOT-OWNED order counts.
    order = _order(order_id="unknown-1", slug="m1")
    report = reconcile([order], {}, {"m1": _position(10)})

    assert report.unmanaged_positions == [{"netPositionDecimal": 10, "market_slug": "m1"}]


def test_zero_net_position_never_flagged():
    report = reconcile([], {}, {"m1": _position(0)})

    assert report.unmanaged_positions == []


def test_malformed_net_position_treated_as_zero_not_crashed():
    report = reconcile([], {}, {"m1": {"netPositionDecimal": "not-a-number"}})

    assert report.unmanaged_positions == []


def test_ledger_appears_empty_true_when_no_known_ids_but_open_orders_exist():
    order = _order(order_id="x1", slug="m1")
    report = reconcile([order], {}, {})

    assert report.ledger_appears_empty is True


def test_ledger_appears_empty_false_when_fresh_bot_has_no_open_orders_either():
    report = reconcile([], {}, {})

    assert report.ledger_appears_empty is False


def test_ledger_appears_empty_false_when_known_ids_present():
    order = _order(order_id="b1", slug="m1")
    report = reconcile([order], {"b1": "m1"}, {})

    assert report.ledger_appears_empty is False


def test_fully_empty_inputs_produce_empty_report():
    report = reconcile([], {}, {})

    assert report.bot_owned_orders == []
    assert report.unknown_orders == []
    assert report.orders_missing_id_count == 0
    assert report.stale_ledger_orders == []
    assert report.unmanaged_positions == []
    assert report.known_order_id_count == 0
    assert report.open_order_count == 0
    assert report.position_count == 0
    assert report.ledger_appears_empty is False


def test_count_fields_reflect_input_sizes():
    orders = [_order(order_id="b1", slug="m1"), _order(order_id="x1", slug="m2")]
    known = {"b1": "m1", "stale1": "m3"}
    positions = {"m1": _position(5), "m2": _position(0)}
    report = reconcile(orders, known, positions)

    assert report.open_order_count == 2
    assert report.known_order_id_count == 2
    assert report.position_count == 2


def test_combined_scenario_exercises_all_report_facets():
    bot_order = _order(order_id="b1", slug="m1")
    unknown_order = _order(order_id="x1", slug="m2")
    no_id_order = _order(slug="m3")

    known = {"b1": "m1", "stale1": "m4"}  # stale1 no longer open
    positions = {
        "m1": _position(10),   # managed by bot_order
        "m2": _position(5),    # unmanaged: only an unknown order rests there
        "m5": _position(0),    # zero net, never flagged
    }

    report = reconcile([bot_order, unknown_order, no_id_order], known, positions)

    assert report.bot_owned_orders == [bot_order]
    assert report.unknown_orders == [unknown_order]
    assert report.orders_missing_id_count == 1
    assert report.stale_ledger_orders == [{"order_id": "stale1", "market_slug": "m4"}]
    assert report.unmanaged_positions == [{"netPositionDecimal": 5, "market_slug": "m2"}]
    assert report.known_order_id_count == 2
    assert report.open_order_count == 3
    assert report.position_count == 3
    assert report.ledger_appears_empty is False
