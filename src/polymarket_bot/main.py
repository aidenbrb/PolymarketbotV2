"""CLI entry point.

Paper-trading (simulated, no real money -- always safe to run):
    python -m polymarket_bot.main scan
    python -m polymarket_bot.main show-watchlist
    python -m polymarket_bot.main paper-buy
    python -m polymarket_bot.main paper-close
    python -m polymarket_bot.main show-portfolio
    python -m polymarket_bot.main show-rejections

Live trading on Polymarket US (REAL MONEY -- see live/RUNBOOK.md first):
    python -m polymarket_bot.main live-preview        (read-only, no credentials)
    python -m polymarket_bot.main live-start          (places real orders)
    python -m polymarket_bot.main live-status
    python -m polymarket_bot.main live-reconcile-orders (read-only, needs credentials)
    python -m polymarket_bot.main live-event-exposure  (read-only, needs credentials)
    python -m polymarket_bot.main live-fills           (read-only, no credentials)
    python -m polymarket_bot.main live-family-performance (read-only, no credentials)
    python -m polymarket_bot.main live-observation-report (read-only, no credentials)
    python -m polymarket_bot.main live-observation-continue (one-time evidence continuation)
    python -m polymarket_bot.main live-observation-complete (finish missing healthy-feed evidence)
    python -m polymarket_bot.main live-observation-settle (settle shadow inventory on resolved markets)
    python -m polymarket_bot.main live-observation-replay (read-only, no credentials, no network)
    python -m polymarket_bot.main live-pilot-start     (guarded real-money pilot)
    python -m polymarket_bot.main live-pilot-start-july5 (unqualified July 5-style real-money pilot)
    python -m polymarket_bot.main live-pnl              (read-only, no credentials)
    python -m polymarket_bot.main live-pnl-reconcile     (read-only, needs credentials)
    python -m polymarket_bot.main live-migrate-fills   (one-time maintenance, no credentials)
    python -m polymarket_bot.main live-cancel-all
    python -m polymarket_bot.main live-reset-breaker
    python -m polymarket_bot.main live-reset-equity-protection

live-start requires LIVE_TRADING_ENABLED=true in your .env AND a typed
confirmation phrase -- there is no --yes/--force flag. See
live/credentials.py, live/confirmation.py, and live/RUNBOOK.md.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import math
import shutil
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from . import config, dashboard, storage
from .filters import MarketFilters
from .logger import get_logger
from .market_scanner import MarketScanner
from .models import Market, ScoredMarket
from .paper_trader import PaperTrader
from .polymarket_client import PolymarketClient
from .portfolio_tracker import PortfolioTracker
from .risk_manager import RiskManager
from .scoring_engine import ScoringEngine

logger = get_logger("main")

LATEST_MARKETS_FILE = config.PROCESSED_DIR / "latest.json"
LATEST_SCORES_FILE = config.PROCESSED_DIR / "latest_scores.json"
LATEST_REJECTIONS_FILE = config.PROCESSED_DIR / "latest_rejections.json"


def _get_tick_size(market: Market, default: float = 0.01) -> float:
    try:
        return float(market.raw.get("orderPriceMinTickSize"))
    except (TypeError, ValueError):
        return default


def _load_latest_markets() -> list[Market]:
    data = storage.load_json(LATEST_MARKETS_FILE, default=None)
    if not data:
        return []
    return [Market(**m) for m in data.get("markets", [])]


def cmd_scan(_args: argparse.Namespace) -> None:
    config.ensure_data_dirs()
    scanner = MarketScanner()
    markets = scanner.scan()

    filters = MarketFilters()
    accepted, filter_results = filters.apply(markets)
    storage.save_json(LATEST_REJECTIONS_FILE, [r.__dict__ for r in filter_results])

    engine = ScoringEngine()
    scored = [engine.score(m) for m in accepted]
    storage.save_json(
        LATEST_SCORES_FILE,
        [
            {
                "market_id": s.market_id,
                "question": s.question,
                "total_score": s.total_score,
                "component_scores": s.component_scores,
                "explanation": s.explanation,
                "recommendation": s.recommendation,
                "market": s.market.to_dict() if s.market else None,
            }
            for s in scored
        ],
    )

    print(f"Scanned {len(markets)} markets: {len(accepted)} accepted, "
          f"{len(markets) - len(accepted)} rejected.")
    print(f"Scored {len(scored)} markets. Run `show-watchlist` to see the ranked list.")


def _load_latest_scored() -> list[ScoredMarket]:
    data = storage.load_json(LATEST_SCORES_FILE, default=[])
    scored = []
    for item in data:
        market = Market(**item["market"]) if item.get("market") else None
        scored.append(
            ScoredMarket(
                market_id=item["market_id"],
                question=item["question"],
                total_score=item["total_score"],
                component_scores=item["component_scores"],
                explanation=item["explanation"],
                recommendation=item["recommendation"],
                market=market,
            )
        )
    return scored


def cmd_show_watchlist(args: argparse.Namespace) -> None:
    scored = _load_latest_scored()
    if not scored:
        print("No scan data found. Run `scan` first.")
        return
    print(dashboard.render_watchlist(scored, top_n=args.top))


def cmd_show_rejections(_args: argparse.Namespace) -> None:
    data = storage.load_json(LATEST_REJECTIONS_FILE, default=[])
    from .models import FilterResult
    results = [FilterResult(**r) for r in data]
    print(dashboard.render_rejections(results))


def cmd_paper_buy(_args: argparse.Namespace) -> None:
    scored = _load_latest_scored()
    if not scored:
        print("No scan data found. Run `scan` first, then `show-watchlist` to pick a market.")
        return

    print("Enter details for a manual paper (fake) trade. This places NO real order.")
    market_id = input("Market ID (from show-watchlist): ").strip()
    match = next((s for s in scored if s.market_id == market_id), None)
    if match is None or match.market is None:
        print("Market ID not found in the latest scan. Run `show-watchlist` for valid IDs.")
        return

    market = match.market
    print(f"Selected: {market.question}")
    if market.outcomes:
        print(f"Outcomes: {', '.join(market.outcomes)}")

    outcome = input("Outcome to buy: ").strip()
    token_id = market.token_ids[0] if market.token_ids else None

    default_price = market.midpoint or (market.outcome_prices[0] if market.outcome_prices else None)
    price_prompt = f"Entry price [{default_price}]: " if default_price else "Entry price: "
    price_input = input(price_prompt).strip()
    try:
        price = float(price_input) if price_input else float(default_price)
    except (TypeError, ValueError):
        print("Invalid price.")
        return

    size_input = input("Fake size in USD [10]: ").strip()
    try:
        size_usd = float(size_input) if size_input else 10.0
    except ValueError:
        print("Invalid size.")
        return

    reason = input("Reason for entry: ").strip() or "No reason given"

    portfolio = PortfolioTracker()
    risk = RiskManager()
    check = risk.check_new_position(
        size_usd=size_usd,
        current_open_positions=len(portfolio.open_positions()),
        current_exposure_usd=portfolio.get_exposure(),
        current_cash=portfolio.get_cash(),
    )
    if not check.allowed:
        print("Trade blocked by risk manager:")
        for reason_blocked in check.reasons:
            print(f"  - {reason_blocked}")
        return

    trader = PaperTrader()
    trade = trader.open_trade(
        market_id=market_id,
        token_id=token_id,
        outcome=outcome,
        requested_price=price,
        size_usd=size_usd,
        reason_entry=reason,
    )
    portfolio.record_snapshot()
    print(f"Opened paper trade {trade.trade_id} at {trade.entry_price:.4f} "
          f"({trade.shares:.4f} shares, ${trade.size_usd:.2f}).")


def cmd_paper_close(_args: argparse.Namespace) -> None:
    trader = PaperTrader()
    open_trades = trader.get_open_trades()
    if not open_trades:
        print("No open paper positions.")
        return

    print(dashboard.render_open_positions(open_trades))
    trade_id = input("Trade ID to close (first 8 chars ok): ").strip()
    match = next(
        (t for t in open_trades if t["trade_id"] == trade_id or t["trade_id"].startswith(trade_id)),
        None,
    )
    if match is None:
        print("Trade ID not found among open positions.")
        return

    price_input = input("Exit price: ").strip()
    try:
        exit_price = float(price_input)
    except ValueError:
        print("Invalid price.")
        return

    reason = input("Reason for exit: ").strip() or "No reason given"

    trade = trader.close_trade(match["trade_id"], exit_price, reason)
    PortfolioTracker().record_snapshot()
    print(f"Closed paper trade {trade.trade_id}: realized P/L = ${trade.realized_pnl:.2f}")


def cmd_show_portfolio(_args: argparse.Namespace) -> None:
    portfolio = PortfolioTracker()
    snapshot = portfolio.record_snapshot()
    print(dashboard.render_portfolio_summary(snapshot))
    print()
    print("Open positions:")
    print(dashboard.render_open_positions(portfolio.open_positions()))
    print()
    print("Closed trades:")
    print(dashboard.render_closed_trades(portfolio.closed_positions()))


def cmd_live_preview(_args: argparse.Namespace) -> None:
    """Fully read-only. No credentials, no order of any kind."""
    from .live.market_selection import select_target_market
    from .live.pricing import compute_book_aware_quote

    settings = config.load_settings().live
    chosen = select_target_market(settings=settings)
    if chosen is None or chosen.market is None:
        print("No eligible market found for live quoting right now (no candidate "
              "has enough real spread to be worth quoting -- see LIVE_MIN_EDGE_CENTS).")
        return

    market = chosen.market
    tick_size = _get_tick_size(market)

    client = PolymarketClient()
    bbo = client.get_market_bbo(market.market_id)
    best_bid = bbo.get("best_bid") if bbo else None
    best_ask = bbo.get("best_ask") if bbo else None
    if best_bid is None or best_ask is None:
        print(f"Selected '{market.question}' but no live best bid/ask is available "
              f"to preview a book-aware quote.")
        return

    reference_price = round((best_bid + best_ask) / 2, 6)
    quote = compute_book_aware_quote(best_bid, best_ask, tick_size, settings.min_edge_cents)
    if quote is None:
        print(f"Selected '{market.question}' (real spread "
              f"{(best_ask - best_bid) * 100:.2f}c) but there's not enough room to "
              f"improve both sides by a tick and still clear the {settings.min_edge_cents:.2f}c "
              f"minimum edge -- live-start would skip quoting this market this cycle.")
        return

    print(dashboard.render_live_quote_preview(
        question=market.question,
        market_id=market.market_id,
        reference_price=reference_price,
        tick_size=tick_size,
        bid=quote.bid,
        ask=quote.ask,
        order_shares_min=settings.order_shares_min,
        order_shares_max=settings.order_shares_max,
    ))
    print()
    print("This is a PREVIEW only -- no credentials were used, no order was placed.")


def cmd_live_start(_args: argparse.Namespace) -> None:
    from .live.confirmation import LiveTradingNotConfirmed, require_live_confirmation

    settings = config.load_settings().live
    if settings.observation_only_mode and not settings.use_websocket:
        print("Observation-only mode requires LIVE_USE_WEBSOCKET=true; bot not started.")
        return
    try:
        require_live_confirmation(settings)
    except LiveTradingNotConfirmed as exc:
        print(f"Live trading not started: {exc}")
        return

    # Only past confirmation do we ever touch anything credential-related.
    from .live.credentials import MissingCredentialsError, load_api_credentials
    from .live.instance_lock import AlreadyRunningError
    from .live.multi_market_maker import MultiMarketMaker
    from .live.runner import LiveTradingBot
    from .live.us_client import CryptographyDependencyMissing, LiveUsClient
    from .live.ws_market_data import WebSocketDependencyMissing
    from .live.ws_runner import WebSocketLiveTradingBot

    try:
        credentials = load_api_credentials()
    except MissingCredentialsError as exc:
        print(f"Cannot start live trading: {exc}")
        return

    try:
        client = LiveUsClient(credentials=credentials, settings=settings)
    except CryptographyDependencyMissing as exc:
        print(str(exc))
        return
    if settings.use_websocket:
        bot = WebSocketLiveTradingBot(client=client, settings=settings)
        if settings.observation_only_mode:
            print(
                "Starting WebSocket observation-only mode across up to "
                f"{settings.observation_universe_size} markets. No new positions "
                "will be opened. Press Ctrl+C to stop."
            )
        else:
            print(
                "Starting WebSocket-driven live quoting across the newest "
                f"{settings.newest_market_scan_limit} markets, up to "
                f"{settings.max_orders_per_cycle} order legs per cycle. "
                "Press Ctrl+C to stop and cancel all resting orders."
            )
        try:
            bot.run_forever()
        except (WebSocketDependencyMissing, AlreadyRunningError) as exc:
            print(str(exc))
        return

    maker = MultiMarketMaker(client=client, settings=settings)
    bot = LiveTradingBot(client=client, market_maker=maker, settings=settings)
    print(
        "Starting REST-polling live quoting across the newest "
        f"{settings.newest_market_scan_limit} markets, up to "
        f"{settings.max_orders_per_cycle} order legs per cycle. "
        "Press Ctrl+C to stop and cancel all resting orders."
    )
    try:
        bot.run_forever()
    except AlreadyRunningError as exc:
        print(str(exc))


def _guarded_pilot_settings(
    base: config.LiveTradingSettings,
) -> config.LiveTradingSettings:
    """Return the fixed pilot envelope; environment tuning cannot widen it."""
    return dataclasses.replace(
        base,
        observation_only_mode=False,
        observation_gate_enabled=True,
        pilot_mode=True,
        use_websocket=True,
        enable_private_websocket=True,
        order_shares_min=1.0,
        order_shares_max=1.0,
        max_orders_per_cycle=10,
        max_spread=0.50,
        max_started_event_hours=3.0,
        max_markets_per_event=3,
        one_round_trip_per_market_per_session=False,
        require_both_entry_legs=True,
        flat_first_inventory_enabled=True,
        pre_event_reduce_only_minutes=60.0,
        hard_flatten_minutes_before_event=0.0,
        hard_flatten_on_max_holding_enabled=True,
        liquidation_max_holding_hours=1.0,
        refresh_interval_seconds=60,
        pilot_entry_hours=3.5,
        pilot_drain_minutes=30.0,
        pilot_max_round_trips_per_market=2,
        fast_reprice_enabled=True,
    )


JULY5_PILOT_CONFIRMATION_PHRASE = (
    "I UNDERSTAND THIS BYPASSES THE OBSERVATION QUALIFICATION GATE "
    "AND PLACES REAL ORDERS WITH REAL MONEY"
)


def _guarded_july5_pilot_settings(
    base: config.LiveTradingSettings,
) -> config.LiveTradingSettings:
    """Return the fixed, unqualified July 5-style pilot envelope.

    This deliberately widens only the opportunity-set controls negotiated
    for this command.  It inherits the guarded pilot's one-share sizing,
    paired maker entries, holding limit, event cap, timed shutdown, and
    verified-flat behavior.
    """
    guarded = _guarded_pilot_settings(base)
    return dataclasses.replace(
        guarded,
        max_spread=0.98,
        pre_event_reduce_only_minutes=0.0,
        max_started_event_hours=6.0,
        rank_by_expected_value=False,
        observation_gate_enabled=False,
        activity_tracking_enabled=True,
        activity_rerank_enabled=False,
        extreme_price_low_threshold=0.15,
        extreme_price_high_threshold=0.85,
        extreme_price_min_edge_cents=4.0,
        max_payoff_loss_to_capture_ratio=20.0,
        unattended_mode=False,
        pilot_qualification_bypassed=True,
        pilot_strategy_profile="july5_style",
        confirmation_phrase=JULY5_PILOT_CONFIRMATION_PHRASE,
    )


def cmd_live_pilot_start(_args: argparse.Namespace) -> None:
    """Start the fixed one-share, four-hour controlled live pilot."""
    from .live.confirmation import LiveTradingNotConfirmed, require_live_confirmation
    from .live.market_observation import PILOT_OBSERVATION_FILE, MarketObservationTracker

    loaded = config.load_settings()
    base = loaded.live
    if not base.use_websocket or not base.enable_private_websocket:
        print(
            "Guarded live pilot is locked: LIVE_USE_WEBSOCKET and "
            "LIVE_ENABLE_PRIVATE_WEBSOCKET must both already be enabled."
        )
        return
    evidence = MarketObservationTracker(base)
    allowed, reasons = evidence.pilot_start_eligible()
    if not allowed:
        print("Guarded live pilot is locked.")
        for reason in reasons:
            print(f"  - {reason}")
        print("Run live-observation-report --profile controlled for the full gate.")
        return

    pilot = _guarded_pilot_settings(base)
    try:
        require_live_confirmation(pilot)
    except LiveTradingNotConfirmed as exc:
        print(f"Guarded live pilot not started: {exc}")
        return

    from .live.circuit_breaker import CircuitBreaker, SessionCircuitBreaker
    from .live.credentials import MissingCredentialsError, load_api_credentials
    from .live.equity_protection import EquityProtection
    from .live.instance_lock import AlreadyRunningError
    from .live.us_client import CryptographyDependencyMissing, LiveUsClient
    from .live.ws_market_data import WebSocketDependencyMissing
    from .live.ws_runner import PilotFlatnessError, WebSocketLiveTradingBot

    try:
        credentials = load_api_credentials()
        client = LiveUsClient(credentials=credentials, settings=pilot)
    except (MissingCredentialsError, CryptographyDependencyMissing) as exc:
        print(f"Cannot start guarded live pilot: {exc}")
        return

    daily_settings = dataclasses.replace(
        loaded.circuit_breaker,
        enabled=True,
        daily_loss_limit_usd=3.0,
    )
    session_settings = dataclasses.replace(
        loaded.session_circuit_breaker,
        enabled=True,
        loss_limit_usd=3.0,
    )
    equity_settings = dataclasses.replace(
        loaded.equity_protection,
        profit_lock_size_multiplier=1.0,
    )
    daily_breaker = CircuitBreaker(daily_settings)
    if daily_breaker.is_halted():
        print(
            "Guarded live pilot is locked because the daily circuit breaker "
            "is already halted. Review the loss before resetting it."
        )
        return
    bot = WebSocketLiveTradingBot(
        client=client,
        settings=pilot,
        circuit_breaker=daily_breaker,
        session_circuit_breaker=SessionCircuitBreaker(session_settings),
        equity_protection=EquityProtection(equity_settings),
        observation_tracker=MarketObservationTracker(pilot, path=PILOT_OBSERVATION_FILE),
    )
    print(
        "Starting the guarded four-hour pilot: one share per leg, up to "
        "10 legs/five markets, qualified cohorts only, $3 daily and session "
        "loss limits. New entries stop after 3.5 hours; shutdown must verify flat."
    )
    try:
        bot.run_forever()
    except (
        WebSocketDependencyMissing,
        AlreadyRunningError,
        PilotFlatnessError,
    ) as exc:
        print(str(exc))


def cmd_live_pilot_start_july5(_args: argparse.Namespace) -> None:
    """Start the fixed, unqualified July 5-style one-share live pilot."""
    from .live.confirmation import LiveTradingNotConfirmed, require_live_confirmation
    from .live.market_observation import (
        JULY5_PILOT_OBSERVATION_FILE,
        OBSERVATION_FILE,
        PRIMARY_STRATEGY,
        MarketObservationTracker,
    )

    loaded = config.load_settings()
    base = loaded.live
    if not base.use_websocket or not base.enable_private_websocket:
        print(
            "July 5-style live pilot is locked: LIVE_USE_WEBSOCKET and "
            "LIVE_ENABLE_PRIVATE_WEBSOCKET must both already be enabled."
        )
        return

    # Both evidence checks are informational for this explicitly unqualified
    # command.  Failures, missing evidence, and negative evidence are shown,
    # but none masquerades as a qualification pass or silently changes the
    # negotiated bypass into an ordinary controlled pilot.
    evidence = None
    try:
        evidence = MarketObservationTracker(base)
        controlled_allowed, controlled_reasons = evidence.pilot_start_eligible()
        print(
            "Controlled observation gate (informational only): "
            f"{'PASS' if controlled_allowed else 'NOT PASSED'}."
        )
        for reason in controlled_reasons:
            print(f"  - {reason}")
    except Exception as exc:  # noqa: BLE001 -- evidence is non-blocking here
        print(f"Controlled observation gate unavailable (non-blocking): {exc}")

    if OBSERVATION_FILE.exists():
        try:
            from .live.observation_replay import run_replay

            replay = run_replay(OBSERVATION_FILE)
            follow_up = (
                replay.get("profiles", {})
                .get("july5_style", {})
                .get(PRIMARY_STRATEGY, {})
                .get("follow_up", {})
            )
            status = follow_up.get("status", "UNAVAILABLE")
            print(f"July 5-style shadow follow-up (informational only): {status}.")
            for reason in follow_up.get("blocked_reasons", []):
                print(f"  - {reason}")
        except Exception as exc:  # noqa: BLE001 -- evidence is non-blocking here
            print(f"July 5-style shadow follow-up unavailable (non-blocking): {exc}")
    else:
        print("July 5-style shadow follow-up unavailable: no v5 archive exists.")

    print()
    print("=" * 78)
    print("UNQUALIFIED REAL-MONEY JULY 5-STYLE PILOT")
    print("This command deliberately bypasses the startup qualification gate and")
    print("the per-cycle observation entry filter. It is a hybrid opportunity set,")
    print("not a reconstruction or proof that the July 5 bot will be profitable.")
    print("It can quote spreads as wide as 98 cents and can admit extreme prices")
    print("such as roughly 1c/98c when the relative payoff guards still pass.")
    print("The $3 daily/session breakers are reactive thresholds, not a guaranteed")
    print("maximum loss; fills and exits can take realized loss beyond $3.")
    print("One-share sizing and all guarded shutdown protections remain enforced.")
    print("=" * 78)

    pilot = _guarded_july5_pilot_settings(base)
    try:
        require_live_confirmation(pilot)
    except LiveTradingNotConfirmed as exc:
        print(f"July 5-style live pilot not started: {exc}")
        return

    try:
        pilot_observation_tracker = MarketObservationTracker(
            pilot, path=JULY5_PILOT_OBSERVATION_FILE,
        )
    except Exception as exc:  # fail closed on a mismatched attribution spec
        print(
            "Cannot start July 5-style live pilot because its shadow "
            f"attribution profile could not be initialized: {exc}"
        )
        return

    from .live.circuit_breaker import CircuitBreaker, SessionCircuitBreaker
    from .live.credentials import MissingCredentialsError, load_api_credentials
    from .live.equity_protection import EquityProtection
    from .live.instance_lock import AlreadyRunningError
    from .live.us_client import CryptographyDependencyMissing, LiveUsClient
    from .live.ws_market_data import WebSocketDependencyMissing
    from .live.ws_runner import PilotFlatnessError, WebSocketLiveTradingBot

    try:
        credentials = load_api_credentials()
        client = LiveUsClient(credentials=credentials, settings=pilot)
    except (MissingCredentialsError, CryptographyDependencyMissing) as exc:
        print(f"Cannot start July 5-style live pilot: {exc}")
        return

    daily_settings = dataclasses.replace(
        loaded.circuit_breaker,
        enabled=True,
        daily_loss_limit_usd=3.0,
    )
    session_settings = dataclasses.replace(
        loaded.session_circuit_breaker,
        enabled=True,
        loss_limit_usd=3.0,
    )
    equity_settings = dataclasses.replace(
        loaded.equity_protection,
        profit_lock_size_multiplier=1.0,
    )
    daily_breaker = CircuitBreaker(daily_settings)
    if daily_breaker.is_halted():
        print(
            "July 5-style live pilot is locked because the daily circuit "
            "breaker is already halted. Review the loss before resetting it."
        )
        return
    bot = WebSocketLiveTradingBot(
        client=client,
        settings=pilot,
        circuit_breaker=daily_breaker,
        session_circuit_breaker=SessionCircuitBreaker(session_settings),
        equity_protection=EquityProtection(equity_settings),
        observation_tracker=pilot_observation_tracker,
    )
    print(
        "Starting the unqualified July 5-style four-hour pilot: one share per "
        "leg, up to 10 legs/five markets, widest-spread ordering, and $3 "
        "reactive daily/session breakers. New entries stop after 3.5 hours; "
        "shutdown must verify flat. No automatic scaling is permitted."
    )
    try:
        bot.run_forever()
    except (
        WebSocketDependencyMissing,
        AlreadyRunningError,
        PilotFlatnessError,
    ) as exc:
        print(str(exc))


def cmd_live_shadow_dryrun_start(_args: argparse.Namespace) -> None:
    """Risk-free: runs the shadow-simulation engine against the real
    public market-data feed with genuine one-share sizing, a fixed
    evidence target/deadline, and a PASS/FAIL/INSUFFICIENT verdict -- no
    order-placement or account-access capability exists anywhere in this
    path. Real API credentials are still required (the market-data
    WebSocket handshake itself must be signed), but only
    WebSocketAuthSigner is constructed from them, never LiveUsClient."""
    from .live.credentials import MissingCredentialsError, load_api_credentials
    from .live.instance_lock import AlreadyRunningError
    from .live.market_dryrun import (
        DryRunFeedStalledError,
        DryRunPolicy,
        DryRunPolicyMismatchError,
        run_dry_run,
    )
    from .live.ws_auth_signer import CryptographyDependencyMissing
    from .live.ws_market_data import WebSocketDependencyMissing

    settings = config.load_settings().live
    try:
        credentials = load_api_credentials()
    except MissingCredentialsError as exc:
        print(f"Cannot start dry-run: {exc}")
        return

    policy = DryRunPolicy()
    print(
        f"Starting risk-free dry-run: profile={policy.profile} "
        f"order_shares={policy.order_shares}. No real order or account "
        "capability exists in this process -- see live/market_dryrun.py."
    )
    try:
        run_dry_run(credentials=credentials, settings=settings, policy=policy)
    except (
        CryptographyDependencyMissing,
        DryRunPolicyMismatchError,
        WebSocketDependencyMissing,
        AlreadyRunningError,
        DryRunFeedStalledError,
    ) as exc:
        print(f"Cannot start dry-run: {exc}")


def cmd_live_shadow_dryrun_status(args: argparse.Namespace) -> None:
    """Genuinely read-only: never constructs a MarketObservationTracker.
    Displays the one canonical snapshot the running dry-run process
    persists -- never recomputes verdict logic itself."""
    from .live.market_dryrun import dry_run_status

    snapshot = dry_run_status()
    if args.json:
        def _json_safe(value):
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, dict):
                return {key: _json_safe(item) for key, item in value.items()}
            if isinstance(value, list):
                return [_json_safe(item) for item in value]
            return value

        print(json.dumps(_json_safe(snapshot), indent=2, allow_nan=False))
        return

    verdict = snapshot.get("verdict", "NOT_STARTED")
    print(f"Dry-run status: verdict={verdict} phase={snapshot.get('phase')}")
    if verdict in ("NOT_STARTED",):
        return
    if verdict == "PROVISIONAL":
        print(
            f"  round_trips={snapshot.get('round_trips')}  "
            f"distinct_events={snapshot.get('distinct_events')}  "
            f"entry_markout_samples={snapshot.get('entry_markout_samples')}  "
            f"healthy_feed_hours={snapshot.get('healthy_feed_hours_elapsed')}  "
            f"wall_clock_hours={snapshot.get('wall_clock_hours_elapsed')}"
        )
        return
    detail = snapshot.get("detail") or {}
    for key, value in detail.items():
        print(f"  {key}={value}")


def cmd_live_status(_args: argparse.Namespace) -> None:
    settings = config.load_settings().live
    from .live.circuit_breaker import CircuitBreaker
    from .live.equity_protection import EquityProtection
    from .live.ledger import get_all_cycles

    breaker = CircuitBreaker()
    print(f"Circuit breaker halted: {breaker.is_halted()}")
    print(
        "Session circuit breaker: in-memory only, not visible from this "
        "process -- check the running live-start log for a trip (only a "
        "live-start restart clears it)."
    )

    equity_protection = EquityProtection()
    print(f"Equity protection halted: {equity_protection.is_halted()}")

    cycles = get_all_cycles()
    print(f"Total recorded live quote cycles: {len(cycles)}")
    if cycles:
        last = cycles[-1]
        print(f"Last cycle: {last['timestamp']} market={last['market_id']}")
        print(f"  bid: price={last['bid']['price']} id={last['bid']['order_id']} error={last['bid']['error']}")
        print(f"  ask: price={last['ask']['price']} id={last['ask']['order_id']} error={last['ask']['error']}")

    if not settings.enabled:
        return

    from .live.credentials import MissingCredentialsError, load_api_credentials
    from .live.us_client import CryptographyDependencyMissing, LiveUsClient, UsApiError

    try:
        credentials = load_api_credentials()
        client = LiveUsClient(credentials=credentials, settings=settings)
    except (MissingCredentialsError, CryptographyDependencyMissing) as exc:
        print(f"(Could not build live client: {exc})")
        return

    # /v1/whoami is not available on every environment -- best-effort only,
    # never blocks the rest of the status check.
    try:
        who = client.whoami()
        print(f"Authenticated as: {who}")
    except UsApiError:
        pass

    try:
        balances = client.get_account_balances()
        print(f"Account balances: {balances}")
    except UsApiError as exc:
        print(f"(Could not fetch account balances: {exc})")

    try:
        open_orders = client.get_open_orders()
        print(f"Open orders on exchange: {len(open_orders)}")
    except UsApiError as exc:
        print(f"(Could not fetch open orders: {exc})")

    equity_settings = config.load_settings().equity_protection
    if equity_settings.starting_capital_usd > 0:
        from .live.ledger import get_total_position_pnl_usd

        total_pnl = get_total_position_pnl_usd(client)
        if total_pnl is not None:
            account_value = equity_settings.starting_capital_usd + total_pnl
            print(f"Equity-protection account value: ${account_value:.2f}")
        else:
            print("(Could not fetch positions for equity-protection account value)")
    else:
        print("Equity-protection drawdown check: inactive (EQUITY_PROTECTION_STARTING_CAPITAL_USD not set)")


def cmd_live_reconcile_orders(_args: argparse.Namespace) -> None:
    """Fully read-only diagnostic: compares exchange open orders/positions
    against the local ledger's known-order-id set. Places no orders,
    cancels nothing. See live/RUNBOOK.md's "-8." section."""
    settings = config.load_settings().live
    from .live.credentials import MissingCredentialsError, load_api_credentials
    from .live.ledger import get_known_order_id_markets
    from .live.reconciliation import reconcile
    from .live.us_client import CryptographyDependencyMissing, LiveUsClient, UsApiError

    try:
        credentials = load_api_credentials()
        client = LiveUsClient(credentials=credentials, settings=settings)
    except (MissingCredentialsError, CryptographyDependencyMissing) as exc:
        print(f"Cannot reconcile: {exc}")
        return

    try:
        open_orders = client.get_open_orders()
    except UsApiError as exc:
        print(f"Could not fetch open orders: {exc}")
        return

    try:
        positions = client.get_all_positions()
    except UsApiError as exc:
        print(f"Could not fetch positions: {exc}")
        return

    report = reconcile(open_orders, get_known_order_id_markets(), positions)
    print(dashboard.render_reconciliation(report))


def cmd_live_event_exposure(_args: argparse.Namespace) -> None:
    """Fully read-only diagnostic: groups open positions by correlated
    underlying event (see live/event_exposure.py) to surface capital
    concentration that position-count alone hides -- e.g. many corner-count
    props on one soccer match. No raw_by_slug available outside a live
    refresh cycle, so bucket keys here always come from the slug heuristic.
    Also folds in still-open resting orders' worst-case USD (via the local
    ledger, not the exchange's open-order schema) so the "projected"
    figures shown here match what a live refresh cycle's own cap
    enforcement actually sees, not just settled positions.
    See live/RUNBOOK.md's most recent event-exposure section."""
    settings = config.load_settings().live
    from .live.credentials import MissingCredentialsError, load_api_credentials
    from .live.event_exposure import compute_event_exposures, resolve_capital_reference_usd
    from .live.ledger import get_known_order_details, strategy_total_pnl_usd
    from .live.us_client import CryptographyDependencyMissing, LiveUsClient, UsApiError

    try:
        credentials = load_api_credentials()
        client = LiveUsClient(credentials=credentials, settings=settings)
    except (MissingCredentialsError, CryptographyDependencyMissing) as exc:
        print(f"Cannot compute event exposure: {exc}")
        return

    try:
        positions = client.get_all_positions()
        open_orders = client.get_open_orders()
    except UsApiError as exc:
        print(f"Could not fetch positions/open orders: {exc}")
        return

    equity_settings = config.load_settings().equity_protection
    total_pnl = strategy_total_pnl_usd(positions)
    if total_pnl is None:
        print("Could not compute complete strategy P/L; exposure report withheld.")
        return
    capital_reference_usd = resolve_capital_reference_usd(
        equity_settings.starting_capital_usd, total_pnl, positions,
    )
    order_details = get_known_order_details()
    exposures = compute_event_exposures(
        positions, capital_reference_usd, open_orders=open_orders, order_details=order_details,
    )
    print(dashboard.render_event_exposure(exposures, capital_reference_usd))


def cmd_live_fills(_args: argparse.Namespace) -> None:
    """Fully read-only: prints fills persisted locally by the WebSocket
    runner's private-WS execution stream (live/fills.py). No credentials
    needed -- only reads data/live_trades/fills.json. See
    live/RUNBOOK.md's "-9." section."""
    from .live.fills import get_all_fills

    print(dashboard.render_fills(get_all_fills()))


def cmd_live_family_performance(_args: argparse.Namespace) -> None:
    """Fully read-only: markout-based performance grouped by a coarse
    market-type family heuristic (live/family_performance.py), computed
    from fills.json. No credentials needed. See live/RUNBOOK.md's most
    recent risk-tightening section."""
    from .live.family_performance import compute_family_performance
    from .live.fills import get_all_fills

    print(dashboard.render_family_performance(compute_family_performance(get_all_fills())))


def cmd_live_observation_report(args: argparse.Namespace) -> None:
    """Read-only schema-v4 portfolio/cohort qualification report."""
    from .live.market_observation import (
        load_observation_report,
        load_observation_summary,
    )

    profile = args.profile
    summary = load_observation_summary(profile=profile)
    rows = load_observation_report(profile=profile)[: max(1, args.top)]
    if args.json:
        def _json_safe(value):
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, dict):
                return {key: _json_safe(item) for key, item in value.items()}
            if isinstance(value, list):
                return [_json_safe(item) for item in value]
            return value

        print(json.dumps(
            _json_safe({"summary": summary, "markets": rows}),
            indent=2,
            allow_nan=False,
        ))
        return
    print(
        f"Schema-v4 {profile} portfolio observation: "
        f"status={summary['status']}"
    )
    completion_mode = summary.get("evaluation_completion_mode", "wall_clock")
    # In healthy_feed_target mode the wall-clock deadline does not control
    # completion at all (see RUNBOOK 44) -- never present it as if it does.
    deadline_label = (
        "completion_mode=healthy_feed_target" if completion_mode == "healthy_feed_target"
        else f"deadline={summary['evaluation_deadline_epoch']:.0f}"
    )
    print(
        f"window_start={summary['started_at_epoch']:.0f}  "
        f"{deadline_label}  "
        f"round_trips={summary['completed_round_trips']}  "
        f"events={summary['distinct_event_count']}  "
        f"forced_exits={summary['forced_exit_count']}"
    )
    print(
        f"markets={summary['market_count']}  "
        f"book_samples={summary['book_sample_count']}  "
        f"observed_market_hours={summary['observed_market_hours']:.2f}  "
        f"eligible_quote_hours={summary['eligible_quote_hours']:.2f}  "
        f"tape_trades={summary['tape_trade_count']}  "
        f"qualifying_trades={summary['qualifying_tape_trade_count']}  "
        f"shadow_fills={summary['hypothetical_fill_count']}"
    )
    print(
        f"healthy_feed_hours={summary['healthy_feed_hours']:.2f}  "
        f"coverage_target_hours={summary['coverage_target_hours']:.2f}  "
        f"feed_coverage={summary['feed_coverage_ratio']:.1%}  "
        f"open_shadow_positions={summary['open_shadow_strategy_positions']}  "
        f"deadline_finalized={bool(summary['evaluation_finalization'].get('complete'))}"
    )
    continuation = summary.get("observation_continuation") or {}
    if continuation:
        print(
            "continuation="
            f"{continuation.get('status', 'unknown')}  "
            f"requested_hours={float(continuation.get('requested_hours') or 0):.2f}"
        )
    coverage_completion = summary.get("observation_coverage_completion") or {}
    if coverage_completion:
        print(
            "healthy_feed_completion="
            f"{coverage_completion.get('status', 'unknown')}  "
            f"target_hours={summary['healthy_feed_target_hours']:.2f}  "
            f"remaining_hours={summary['remaining_healthy_feed_hours']:.2f}"
        )
    elif completion_mode == "healthy_feed_target":
        # A v5+ archive starts directly in this mode (no retroactive-arm
        # record exists) -- still show what actually governs completion.
        print(
            "healthy_feed_completion=governs_completion  "
            f"target_hours={summary['healthy_feed_target_hours']:.2f}  "
            f"remaining_hours={summary['remaining_healthy_feed_hours']:.2f}"
        )
    avg_5m = summary["avg_markout_5m_cents"]
    avg_5m_text = "n/a" if avg_5m is None else f"{avg_5m:+.3f}c"
    profit_factor = summary["profit_factor"]
    pf_text = "inf" if profit_factor == float("inf") else f"{profit_factor:.3f}"
    print(
        f"realized=${summary['realized_pnl_usd']:+.4f}  "
        f"open_mtm=${summary['open_inventory_mark_to_market_usd']:+.4f}  "
        f"total=${summary['total_pnl_usd']:+.4f}  PF={pf_text}  "
        f"avg_5m={avg_5m_text}  "
        f"max_drawdown=${summary['maximum_drawdown_usd']:+.4f}  "
        f"event_concentration={summary['event_profit_concentration']:.1%}"
    )
    if summary["blocked_reasons"]:
        print(f"blocked: {'; '.join(summary['blocked_reasons'])}")
    if summary["open_inventory"]:
        print("Open inventory (included in total P&L; prevents qualification):")
        for item in summary["open_inventory"]:
            print(
                f"  {item['market_slug']} shares={item['shares']:+.4f} "
                f"mark={item['mark_price']} "
                f"mtm=${item['mark_to_market_pnl_usd']:+.4f}"
            )
    print("Cohorts:")
    if not summary["cohorts"]:
        print("  none")
    for cohort in summary["cohorts"]:
        markout = cohort["avg_markout_5m_cents"]
        markout_text = "n/a" if markout is None else f"{markout:+.3f}c"
        print(
            f"  eligible={cohort['live_eligible']} "
            f"round_trips={cohort['completed_round_trips']} "
            f"events={cohort['distinct_event_count']} "
            f"pnl=${cohort['net_pnl_usd']:+.4f} "
            f"avg_5m={markout_text} {cohort['cohort_key']}"
        )
    if not rows:
        print("No market-level observation evidence recorded yet.")
        return
    print("Market details")
    print(
        "evidence_ready  eligible/all_trades  fills  episodes  roundtrips  "
        "paper_pnl  avg_1m  avg_5m  eligible_min  market"
    )
    for row in rows:
        avg_1m = row["avg_markout_1m_cents"]
        avg_5m = row["avg_markout_5m_cents"]
        avg_1m_text = "n/a" if avg_1m is None else f"{avg_1m:+.2f}c"
        avg_5m_text = "n/a" if avg_5m is None else f"{avg_5m:+.2f}c"
        print(
            f"{str(row['evidence_ready']):>14}  "
            f"{row['qualifying_trade_count']:>8}/{row['trade_count']:<8}  "
            f"{row['hypothetical_fill_count']:>5}  "
            f"{row['distinct_fill_episodes']:>8}  "
            f"{row['paper_round_trip_count']:>10}  "
            f"{row['paper_realized_pnl_usd']:>+9.4f}  "
            f"{avg_1m_text:>7}  {avg_5m_text:>7}  "
            f"{row['eligible_observed_seconds'] / 60:>12.1f}  "
            f"{row['market_slug']}"
        )
        evidence_reasons = [
            reason for reason in row["blocked_reasons"]
            if reason != "observation-only mode is enabled"
        ]
        if evidence_reasons:
            print(f"                blocked: {'; '.join(evidence_reasons)}")


def cmd_live_observation_continue(args: argparse.Namespace) -> None:
    """Arm a one-time continuation of expired compatible v4 evidence."""
    from .live.instance_lock import AlreadyRunningError, InstanceLock
    from .live.market_observation import (
        MarketObservationTracker,
        ObservationContinuationError,
    )

    try:
        with InstanceLock():
            tracker = MarketObservationTracker(config.load_settings().live)
            result = tracker.arm_continuation(hours=args.hours)
    except (AlreadyRunningError, ObservationContinuationError) as exc:
        raise SystemExit(f"Observation continuation was not armed: {exc}") from exc

    print(
        "Armed the one-time observation continuation. Existing evidence was "
        "preserved and its clock has NOT started yet."
    )
    print(
        f"requested_hours={result['requested_hours']:.2f}  "
        f"healthy_feed_hours_preserved={result['healthy_feed_hours_at_arm']:.2f}"
    )
    print(
        f"The {result['requested_hours']:.2f}-hour clock starts on the first "
        "confirmed market-feed activity after the next successful "
        "observation-mode live-start."
    )


def cmd_live_observation_complete(_args: argparse.Namespace) -> None:
    """Arm healthy-feed completion for compatible insufficient v4 evidence."""
    from .live.instance_lock import AlreadyRunningError, InstanceLock
    from .live.market_observation import (
        MarketObservationTracker,
        ObservationCoverageCompletionError,
    )

    try:
        with InstanceLock():
            tracker = MarketObservationTracker(config.load_settings().live)
            result = tracker.arm_healthy_feed_completion()
    except (AlreadyRunningError, ObservationCoverageCompletionError) as exc:
        raise SystemExit(
            f"Healthy-feed completion was not armed: {exc}"
        ) from exc

    print(
        "Armed healthy-feed completion without resetting observation evidence."
    )
    print(
        f"preserved_healthy_hours={result['healthy_feed_hours_at_arm']:.2f}  "
        f"target_healthy_hours={result['target_healthy_feed_hours']:.2f}  "
        f"remaining_healthy_hours={result['remaining_healthy_feed_hours_at_arm']:.2f}"
    )


def _without_settlement_pass(state: dict) -> dict:
    finalization = state.get("evaluation_finalization")
    if not isinstance(finalization, dict) or "settlement_pass" not in finalization:
        return state
    trimmed = copy.deepcopy(state)
    trimmed["evaluation_finalization"].pop("settlement_pass", None)
    return trimmed


def cmd_live_observation_settle(args: argparse.Namespace) -> None:
    """One-time maintenance: settle shadow inventory on markets that have
    genuinely resolved. finalize_evaluation()'s book-based sweep can never
    close these -- a resolved market has no book left to fetch, no matter
    how long the process waits or reconnects -- so this queries the
    market's actual settlement outcome instead and closes the position at
    the real payout. Read-only against the exchange either way (both
    lookup endpoints are unauthenticated); dry-run by default, --apply
    required to persist. See live/RUNBOOK.md's most recent section."""
    from .live.instance_lock import AlreadyRunningError, InstanceLock
    from .live.market_observation import (
        MarketObservationTracker,
        classify_settlement_lookup,
    )

    try:
        with InstanceLock():
            tracker = MarketObservationTracker(config.load_settings().live)
            unresolved = sorted(tracker.open_inventory_slugs())
            if not unresolved:
                print("No shadow inventory is stuck -- nothing to settle.")
                return

            client = PolymarketClient()
            print(f"Resolving {len(unresolved)} slug(s) with open shadow inventory...")
            results = [classify_settlement_lookup(client, slug) for slug in unresolved]

            errors = [row for row in results if row["status"] == "ERROR"]
            if errors:
                print(
                    f"Aborting: {len(errors)} slug(s) returned an ambiguous or failed "
                    "lookup -- no changes made, real or projected."
                )
                for row in errors:
                    print(f"  {row['slug']}: {row.get('reason')}")
                raise SystemExit(1)

            settled_count = sum(1 for row in results if row["status"] == "SETTLED")
            unresolved_count = sum(1 for row in results if row["status"] == "UNRESOLVED")
            print(f"{settled_count} settled, {unresolved_count} still genuinely unresolved.")

            before_state = copy.deepcopy(tracker._state)
            now = time.time()
            batch_result = tracker.apply_settlement_batch(results, now)
            # The settlement_pass audit entry legitimately carries a fresh
            # wall-clock timestamp on every attempt, even when nothing else
            # changed (e.g. a market is still genuinely unresolved on a
            # rerun) -- comparing it verbatim would mean "identical
            # projected state" could never be true except on the very first
            # write. Exclude it specifically from the no-op comparison;
            # everything else (blocker lists, complete, coverage status,
            # every fill/position) still has to match exactly.
            is_noop = (
                _without_settlement_pass(tracker._state)
                == _without_settlement_pass(before_state)
            )

            summary = tracker.profile_summary("controlled")
            print(
                f"Projected controlled result: status={summary['status']}  "
                f"total_pnl=${summary['total_pnl_usd']:+.4f}  "
                f"settlement_exits={summary['settlement_exit_count']}  "
                f"settlement_pnl=${summary['settlement_pnl_usd']:+.4f}  "
                f"remaining_unresolved={len(batch_result['remaining_unresolved_slugs'])}  "
                f"finalization_complete={batch_result['complete']}"
            )

            if is_noop:
                print(
                    "Projected state is identical to what's already persisted -- "
                    "nothing to write."
                )
                return

            if not args.apply:
                print(
                    "Dry run only -- no changes written. Re-run with --apply to "
                    "persist this."
                )
                return

            backup_path = tracker.path.with_name(
                f"{tracker.path.stem}.pre-settlement-backup-{int(now)}{tracker.path.suffix}"
            )
            try:
                shutil.copy2(tracker.path, backup_path)
            except OSError as exc:
                raise SystemExit(
                    f"Could not create a backup at {backup_path} -- refusing to "
                    f"write without one: {exc}"
                ) from exc
            print(f"Backed up the current observation file to {backup_path}.")
            tracker.flush()
            print(
                f"Applied. Settled {len(batch_result['settled_slugs'])} slug(s); "
                f"wrote {tracker.path}."
            )
    except AlreadyRunningError as exc:
        raise SystemExit(f"Cannot run live-observation-settle: {exc}") from exc
    print(
        "Only confirmed feed minutes count. Stopping and restarting pauses "
        "progress instead of consuming a wall-clock deadline."
    )


def cmd_live_observation_replay(args: argparse.Namespace) -> None:
    """Read-only offline attribution/replay over already-collected schema-v4
    evidence. No network calls, never mutates the archive it reads."""
    from .live.observation_replay import run_replay

    report = run_replay()
    if args.json:
        def _json_safe(value):
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, dict):
                return {key: _json_safe(item) for key, item in value.items()}
            if isinstance(value, list):
                return [_json_safe(item) for item in value]
            return value

        print(json.dumps(_json_safe(report), indent=2, allow_nan=False))
        return

    def _pf(value: float) -> str:
        return "inf" if value == float("inf") else f"{value:.3f}"

    print(
        "NOTE: baseline figures are persisted hypothetical shadow fills "
        "(including synthetic settlement closes), not real exchange fills. "
        "A crossing print is an optimistic upper bound, never a guaranteed "
        "fill; aggressive taker-exit feasibility is always UNKNOWN -- no "
        "historical L2 was retained. This command cannot authorize a pilot "
        "unlock; see live-observation-report for actual pilot status."
    )
    for profile, strategies in report["profiles"].items():
        print(f"== profile={profile} ==")
        completion = strategies.get("_completion") or {}
        if completion:
            print(
                f"  healthy_feed_hours={completion['healthy_feed_hours']:.2f}  "
                f"healthy_feed_target_hours={completion['healthy_feed_target_hours']:.2f}  "
                f"remaining_healthy_feed_hours={completion['remaining_healthy_feed_hours']:.2f}  "
                f"open_inventory_count={completion['open_inventory_count']}  "
                f"open_shadow_strategy_positions={completion['open_shadow_strategy_positions']}  "
                f"evaluation_finalization_complete={completion['evaluation_finalization_complete']}"
            )
        for strategy, data in strategies.items():
            if strategy == "_completion":
                continue
            baseline = data["baseline"]
            verdict = data["verdict"]
            print(
                f"  strategy={strategy}  round_trips={baseline['round_trips']}  "
                f"net_pnl=${baseline['net_pnl_usd']:+.4f}  PF={_pf(baseline['profit_factor'])}  "
                f"max_drawdown=${baseline['max_drawdown_usd']:+.4f}"
            )
            print(
                "    one_share_equivalent (linear estimate, not proof of actual "
                f"1-share execution): net_pnl=${baseline['net_pnl_one_share_equivalent_usd']:+.4f}  "
                f"max_drawdown=${baseline['max_drawdown_one_share_equivalent_usd']:+.4f}"
            )
            optimistic_total = verdict["optimistic_passive_total_usd"]
            optimistic_text = "n/a" if optimistic_total is None else f"${optimistic_total:+.4f}"
            print(
                f"    verdict: baseline_shadow_status={verdict['baseline_shadow_status']}  "
                f"passive_replay_status={verdict['passive_replay_status']}  "
                f"taker_replay_status={verdict['taker_replay_status']}  "
                f"pilot_unlock_authorized={verdict['pilot_unlock_authorized']}  "
                f"optimistic_passive_total={optimistic_text}"
            )
            print(f"    escalation_replay status counts: {dict(verdict['escalation_status_counts'])}")
            print(
                "    hard_pre_settlement_exit rows: "
                f"{len(data['hard_pre_settlement_exit'])}"
            )
            print("    entry_filter_sweep (first 5 rows):")
            for row in data["entry_filter_sweep"][:5]:
                print(
                    f"      min_hours={row['min_hours_remaining']:.2f}  "
                    f"min_same_market_trailing_trades={row['min_same_market_trailing_trade_count']}  "
                    f"kept={row['entries_kept']}  rejected={row['entries_rejected']}  "
                    f"net_pnl=${row['net_pnl_usd']:+.4f}  PF={_pf(row['profit_factor'])}"
                )
            print("    revised_cohorts:")
            if not data["revised_cohorts"]:
                print("      none")
            for cohort in data["revised_cohorts"]:
                print(
                    f"      eligible={cohort['revised_eligible']}  "
                    f"round_trips={cohort['round_trips']}  "
                    f"distinct_events={cohort['distinct_event_count']}  "
                    f"net_pnl=${cohort['net_pnl_usd']:+.4f} "
                    f"(1sh_equiv=${cohort['net_pnl_one_share_equivalent_usd']:+.4f})  "
                    f"PF={_pf(cohort['profit_factor'])}  "
                    f"drawdown=${cohort['max_drawdown_usd']:+.4f} "
                    f"(1sh_equiv=${cohort['max_drawdown_one_share_equivalent_usd']:+.4f})  "
                    f"settlement_exit_rate={cohort['settlement_exit_rate']:.1%}  "
                    f"{cohort['cohort_key']}"
                )
            follow_up = data.get("follow_up")
            if follow_up is not None:
                print(
                    f"    follow_up_status={follow_up['status']}  "
                    f"(primary_strategy={follow_up['primary_strategy']})"
                )
                if follow_up["blocked_reasons"]:
                    for reason in follow_up["blocked_reasons"]:
                        print(f"      blocked: {reason}")


def cmd_live_pnl(_args: argparse.Namespace) -> None:
    """Fully read-only: realized P&L attribution -- FIFO lot matching
    (live/lot_accounting.py) over fills.json, closed out by any detected
    settlements (live/settlements.py), broken down by family/event/price-
    band/entry-time/entry-date (live/pnl_attribution.py). No credentials
    needed -- only reads local fills.json/settlements.json. Unlike
    live-family-performance's markout (a proxy), this is actual realized
    P&L. See live/RUNBOOK.md's most recent section."""
    from .live.fills import get_all_fills
    from .live.lot_accounting import compute_lots
    from .live.pnl_attribution import (
        compute_pnl_by_entry_date,
        compute_pnl_by_entry_time_band,
        compute_pnl_by_event,
        compute_pnl_by_family,
        compute_pnl_by_price_band,
        compute_pnl_summary,
    )
    from .live.settlements import get_all_settlements, get_earliest_snapshot_by_slug

    lot_result = compute_lots(get_all_fills(), get_all_settlements(), get_earliest_snapshot_by_slug())
    summary = compute_pnl_summary(lot_result)
    breakdowns = {
        "Family": compute_pnl_by_family(lot_result.closed_lots),
        "Event": compute_pnl_by_event(lot_result.closed_lots),
        "Price band (entry)": compute_pnl_by_price_band(lot_result.closed_lots),
        "Entry time (UTC)": compute_pnl_by_entry_time_band(lot_result.closed_lots),
        "Entry date": compute_pnl_by_entry_date(lot_result.closed_lots),
    }
    print(dashboard.render_pnl_attribution(summary, breakdowns, lot_result))


def cmd_live_pnl_reconcile(_args: argparse.Namespace) -> None:
    """Read-only diagnostic (no order placement/cancellation, needs
    credentials to fetch positions): cross-checks fill-based realized P&L
    (live/lot_accounting.py, "Strategy A") against the exchange's own
    latest realized-position-snapshot figure (live/settlements.py,
    "Strategy B") per market -- see
    live/reconciliation.py::reconcile_realized_pnl. Records a fresh
    position snapshot first so the comparison reflects right now, not just
    whatever the last live-start cycle happened to see."""
    settings = config.load_settings().live
    from .live.credentials import MissingCredentialsError, load_api_credentials
    from .live.fills import get_all_fills
    from .live.lot_accounting import compute_lots
    from .live.reconciliation import reconcile_realized_pnl
    from .live.settlements import (
        get_all_settlements,
        get_earliest_snapshot_by_slug,
        get_latest_snapshot_by_slug,
        record_position_snapshots,
    )
    from .live.us_client import CryptographyDependencyMissing, LiveUsClient, UsApiError

    try:
        credentials = load_api_credentials()
        client = LiveUsClient(credentials=credentials, settings=settings)
    except (MissingCredentialsError, CryptographyDependencyMissing) as exc:
        print(f"Cannot reconcile P&L: {exc}")
        return

    try:
        positions = client.get_all_positions()
    except UsApiError as exc:
        print(f"Could not fetch positions: {exc}")
        return

    record_position_snapshots(positions)
    lot_result = compute_lots(get_all_fills(), get_all_settlements(), get_earliest_snapshot_by_slug())
    reports = reconcile_realized_pnl(
        lot_result.closed_lots, get_latest_snapshot_by_slug(),
        tolerance_usd=settings.pnl_reconcile_tolerance_usd,
    )
    print(dashboard.render_pnl_reconciliation(reports))


def cmd_live_migrate_fills(_args: argparse.Namespace) -> None:
    """One-time maintenance: re-derives market_slug/side/quoted_price for
    existing fills.json records using the corrected schema, and drops
    order-lifecycle noise (NEW/CANCELED/REJECTED) that predates the
    is_actual_fill() filter. No credentials needed -- only reads/writes
    data/live_trades/fills.json. See live/RUNBOOK.md's "-9." section."""
    from datetime import datetime, timezone

    from .live.fills import FILLS_FILE, get_all_fills, migrate_legacy_fills, overwrite_fills
    from .live.ledger import get_known_order_details

    existing = get_all_fills()
    if not existing:
        print("No fills.json found (or it's empty) -- nothing to migrate.")
        return

    migrated, stats = migrate_legacy_fills(existing, get_known_order_details())

    # Not utcnow_iso() -- that contains ":" characters, invalid in a
    # Windows filename.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = FILLS_FILE.with_name(f"fills_pre_migration_{timestamp}.json")
    storage.save_json(backup_path, existing)
    overwrite_fills(migrated)

    print(
        f"Migrated fills.json: {stats['input']} input record(s), "
        f"{stats['dropped_non_fill']} non-fill noise record(s) dropped, "
        f"{stats['migrated']} real fill(s) kept and re-derived."
    )
    print(f"Original file backed up to {backup_path}")


def cmd_live_cancel_all(_args: argparse.Namespace) -> None:
    settings = config.load_settings().live
    if not settings.enabled:
        print("LIVE_TRADING_ENABLED is not 'true'. Nothing to cancel.")
        return

    confirm = input("Cancel ALL resting live orders on this account? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    from .live.credentials import MissingCredentialsError, load_api_credentials
    from .live.us_client import CryptographyDependencyMissing, LiveUsClient, UsApiError

    try:
        credentials = load_api_credentials()
        client = LiveUsClient(credentials=credentials, settings=settings)
        client.cancel_all()
        print("All resting live orders cancelled.")
    except (MissingCredentialsError, UsApiError, CryptographyDependencyMissing) as exc:
        print(f"Could not cancel orders: {exc}")


def cmd_live_reset_breaker(_args: argparse.Namespace) -> None:
    from .live.confirmation import LiveTradingNotConfirmed, require_live_confirmation

    settings = config.load_settings().live
    try:
        require_live_confirmation(settings)
    except LiveTradingNotConfirmed as exc:
        print(f"Not resetting: {exc}")
        return

    from .live.circuit_breaker import CircuitBreaker
    CircuitBreaker().reset()
    print(
        "Circuit breaker reset from the recorded trip P/L. A fresh configured "
        "loss allowance now applies until the next UTC day."
    )


def cmd_live_reset_equity_protection(_args: argparse.Namespace) -> None:
    from .live.confirmation import LiveTradingNotConfirmed, require_live_confirmation

    settings = config.load_settings().live
    try:
        require_live_confirmation(settings)
    except LiveTradingNotConfirmed as exc:
        print(f"Not resetting: {exc}")
        return

    from .live.equity_protection import EquityProtection
    EquityProtection().reset()
    print("Equity protection halt cleared (peak account value preserved). Trading may resume on the next refresh cycle.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymarket_bot",
        description="Polymarket paper-trading research assistant (no live trading).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("scan", help="Fetch, filter, and score public markets.")

    watchlist_parser = subparsers.add_parser("show-watchlist", help="Show ranked watchlist.")
    watchlist_parser.add_argument("--top", type=int, default=20, help="Number of markets to show.")

    subparsers.add_parser("show-rejections", help="Show rejected markets and reasons.")
    subparsers.add_parser("paper-buy", help="Record a manual fake paper trade.")
    subparsers.add_parser("paper-close", help="Close an open fake paper trade.")
    subparsers.add_parser("show-portfolio", help="Show simulated portfolio and P/L.")

    subparsers.add_parser(
        "live-preview", help="REAL DATA, no wallet: preview what live-start would quote."
    )
    # Deliberately no --yes/--force flag on live-start: going live requires the
    # interactive confirmation phrase in live/confirmation.py, every time.
    subparsers.add_parser(
        "live-start", help="REAL MONEY: start live market-making. See live/RUNBOOK.md first."
    )
    subparsers.add_parser(
        "live-pilot-start",
        help="REAL MONEY: start the qualified one-share guarded pilot.",
    )
    subparsers.add_parser(
        "live-pilot-start-july5",
        help=(
            "REAL MONEY: start the unqualified one-share July 5-style pilot; "
            "explicitly bypasses observation qualification."
        ),
    )
    subparsers.add_parser(
        "live-shadow-dryrun-start",
        help=(
            "RISK-FREE: run the shadow-simulation engine against the real "
            "public market-data feed with one-share sizing for a bounded "
            "evidence target/deadline, producing a PASS/FAIL/INSUFFICIENT "
            "verdict. No order-placement or account-access capability "
            "exists in this path."
        ),
    )
    dryrun_status_parser = subparsers.add_parser(
        "live-shadow-dryrun-status",
        help="Read-only: show the current risk-free dry-run's progress/verdict snapshot.",
    )
    dryrun_status_parser.add_argument(
        "--json", action="store_true", help="Emit the snapshot as JSON."
    )
    subparsers.add_parser("live-status", help="Show circuit breaker / live order status.")
    subparsers.add_parser(
        "live-reconcile-orders",
        help="Read-only: compare exchange open orders/positions against the local ledger.",
    )
    subparsers.add_parser(
        "live-event-exposure",
        help="Read-only: show capital concentration grouped by correlated underlying event.",
    )
    subparsers.add_parser(
        "live-fills", help="Read-only: show fills persisted from the private WebSocket stream."
    )
    subparsers.add_parser(
        "live-family-performance",
        help="Read-only: markout-based performance grouped by a coarse market-type family heuristic.",
    )
    observation_parser = subparsers.add_parser(
        "live-observation-report",
        help="Read-only: show real trade flow and hypothetical maker-fill evidence.",
    )
    observation_parser.add_argument("--top", type=int, default=20, help="Number of markets to show.")
    observation_parser.add_argument(
        "--profile",
        choices=("legacy", "controlled", "july5_style"),
        default="controlled",
        help="Shadow portfolio profile to report.",
    )
    observation_parser.add_argument(
        "--json", action="store_true", help="Emit the complete report as JSON."
    )
    continuation_parser = subparsers.add_parser(
        "live-observation-continue",
        help=(
            "One-time maintenance: preserve expired v4 evidence and arm a "
            "bounded continuation."
        ),
    )
    continuation_parser.add_argument(
        "--hours",
        type=float,
        default=30.0,
        help="Continuation duration after confirmed feed startup (default: 30).",
    )
    subparsers.add_parser(
        "live-observation-complete",
        help=(
            "One-time maintenance: preserve compatible evidence and finish "
            "the missing healthy-feed target."
        ),
    )
    settle_parser = subparsers.add_parser(
        "live-observation-settle",
        help=(
            "Maintenance: settle shadow inventory on markets that have "
            "genuinely resolved. Dry-run by default."
        ),
    )
    settle_parser.add_argument(
        "--apply", action="store_true",
        help="Persist the settlement (writes a timestamped backup first). Default: dry-run.",
    )
    replay_parser = subparsers.add_parser(
        "live-observation-replay",
        help=(
            "Read-only: offline attribution/replay over already-collected "
            "evidence -- escalating exits, entry rejection, a hard "
            "never-hold-through-settlement check, and revised cohort "
            "qualification. No network calls; never mutates the archive."
        ),
    )
    replay_parser.add_argument(
        "--json", action="store_true", help="Emit the complete report as JSON."
    )
    subparsers.add_parser(
        "live-pnl",
        help="Read-only: realized P&L attribution (FIFO lot matching), broken down multiple ways.",
    )
    subparsers.add_parser(
        "live-pnl-reconcile",
        help="Read-only: cross-check fill-based realized P&L against the exchange's own figures.",
    )
    subparsers.add_parser(
        "live-migrate-fills",
        help="One-time maintenance: re-derive fields and drop non-fill noise in fills.json.",
    )
    subparsers.add_parser("live-cancel-all", help="Cancel all resting live orders (real money).")
    subparsers.add_parser(
        "live-reset-breaker", help="Clear a tripped circuit-breaker halt (real money)."
    )
    subparsers.add_parser(
        "live-reset-equity-protection",
        help="Clear a tripped equity-protection halt (real money).",
    )

    return parser


COMMANDS = {
    "scan": cmd_scan,
    "show-watchlist": cmd_show_watchlist,
    "show-rejections": cmd_show_rejections,
    "paper-buy": cmd_paper_buy,
    "paper-close": cmd_paper_close,
    "show-portfolio": cmd_show_portfolio,
    "live-preview": cmd_live_preview,
    "live-start": cmd_live_start,
    "live-pilot-start": cmd_live_pilot_start,
    "live-pilot-start-july5": cmd_live_pilot_start_july5,
    "live-shadow-dryrun-start": cmd_live_shadow_dryrun_start,
    "live-shadow-dryrun-status": cmd_live_shadow_dryrun_status,
    "live-status": cmd_live_status,
    "live-reconcile-orders": cmd_live_reconcile_orders,
    "live-event-exposure": cmd_live_event_exposure,
    "live-fills": cmd_live_fills,
    "live-family-performance": cmd_live_family_performance,
    "live-observation-report": cmd_live_observation_report,
    "live-observation-continue": cmd_live_observation_continue,
    "live-observation-complete": cmd_live_observation_complete,
    "live-observation-settle": cmd_live_observation_settle,
    "live-observation-replay": cmd_live_observation_replay,
    "live-pnl": cmd_live_pnl,
    "live-pnl-reconcile": cmd_live_pnl_reconcile,
    "live-migrate-fills": cmd_live_migrate_fills,
    "live-cancel-all": cmd_live_cancel_all,
    "live-reset-breaker": cmd_live_reset_breaker,
    "live-reset-equity-protection": cmd_live_reset_equity_protection,
}


def main(argv: list[str] | None = None) -> int:
    config.ensure_data_dirs()
    parser = build_parser()
    args = parser.parse_args(argv)
    COMMANDS[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
