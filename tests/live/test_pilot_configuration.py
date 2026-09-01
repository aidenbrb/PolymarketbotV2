from polymarket_bot import config
from polymarket_bot.main import (
    JULY5_PILOT_CONFIRMATION_PHRASE,
    _guarded_july5_pilot_settings,
    _guarded_pilot_settings,
)


def test_guarded_pilot_settings_override_a_loose_live_environment():
    base = config.LiveTradingSettings(
        observation_only_mode=True,
        use_websocket=False,
        enable_private_websocket=False,
        order_shares_min=99.0,
        order_shares_max=99.0,
        max_orders_per_cycle=100,
        max_spread=0.99,
        max_started_event_hours=12.0,
        max_markets_per_event=20,
        require_both_entry_legs=False,
        flat_first_inventory_enabled=False,
        liquidation_max_holding_hours=10.0,
        pilot_entry_hours=20.0,
        pilot_drain_minutes=1.0,
        pilot_max_round_trips_per_market=50,
        fast_reprice_enabled=False,
    )

    pilot = _guarded_pilot_settings(base)

    assert pilot.observation_only_mode is False
    assert pilot.pilot_mode is True
    assert pilot.use_websocket is True
    assert pilot.enable_private_websocket is True
    assert (pilot.order_shares_min, pilot.order_shares_max) == (1.0, 1.0)
    assert pilot.max_orders_per_cycle == 10
    assert pilot.max_spread == 0.50
    assert pilot.max_started_event_hours == 3.0
    assert pilot.max_markets_per_event == 3
    assert pilot.require_both_entry_legs is True
    assert pilot.flat_first_inventory_enabled is True
    assert pilot.liquidation_max_holding_hours == 1.0
    assert pilot.pilot_entry_hours == 3.5
    assert pilot.pilot_drain_minutes == 30.0
    assert pilot.pilot_max_round_trips_per_market == 2
    assert pilot.fast_reprice_enabled is True


def test_july5_pilot_widens_only_the_negotiated_opportunity_set():
    base = config.LiveTradingSettings(
        observation_only_mode=True,
        observation_gate_enabled=True,
        activity_tracking_enabled=False,
        activity_rerank_enabled=True,
        unattended_mode=True,
        rank_by_expected_value=True,
        max_spread=0.01,
        max_started_event_hours=0.0,
        pre_event_reduce_only_minutes=999.0,
        extreme_price_low_threshold=-1.0,
        extreme_price_high_threshold=2.0,
        extreme_price_min_edge_cents=0.0,
        max_payoff_loss_to_capture_ratio=1_000_000.0,
        fast_reprice_enabled=False,
    )

    pilot = _guarded_july5_pilot_settings(base)

    assert pilot.observation_only_mode is False
    assert pilot.observation_gate_enabled is False
    assert pilot.pilot_mode is True
    assert pilot.pilot_qualification_bypassed is True
    assert pilot.pilot_strategy_profile == "july5_style"
    assert pilot.max_spread == 0.98
    assert pilot.max_started_event_hours == 6.0
    assert pilot.pre_event_reduce_only_minutes == 0.0
    assert pilot.rank_by_expected_value is False
    assert pilot.activity_tracking_enabled is True
    assert pilot.activity_rerank_enabled is False
    assert pilot.unattended_mode is False
    assert pilot.confirmation_phrase == JULY5_PILOT_CONFIRMATION_PHRASE
    assert (pilot.order_shares_min, pilot.order_shares_max) == (1.0, 1.0)
    assert pilot.max_orders_per_cycle == 10
    assert pilot.max_markets_per_event == 3
    assert pilot.require_both_entry_legs is True
    assert pilot.flat_first_inventory_enabled is True
    assert pilot.liquidation_max_holding_hours == 1.0
    assert pilot.pilot_entry_hours == 3.5
    assert pilot.pilot_drain_minutes == 30.0
    assert pilot.pilot_max_round_trips_per_market == 2
    assert pilot.extreme_price_low_threshold == 0.15
    assert pilot.extreme_price_high_threshold == 0.85
    assert pilot.extreme_price_min_edge_cents == 4.0
    assert pilot.max_payoff_loss_to_capture_ratio == 20.0
    assert pilot.fast_reprice_enabled is True
