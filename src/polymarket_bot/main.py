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
import sys

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
        print(
            "Starting WebSocket-driven live quoting across the newest "
            f"{settings.newest_market_scan_limit} markets, up to "
            f"{settings.max_orders_per_cycle} orders per cycle. "
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
        f"{settings.max_orders_per_cycle} orders per cycle. "
        "Press Ctrl+C to stop and cancel all resting orders."
    )
    try:
        bot.run_forever()
    except AlreadyRunningError as exc:
        print(str(exc))


def cmd_live_status(_args: argparse.Namespace) -> None:
    settings = config.load_settings().live
    from .live.circuit_breaker import CircuitBreaker
    from .live.equity_protection import EquityProtection
    from .live.ledger import get_all_cycles

    breaker = CircuitBreaker()
    print(f"Circuit breaker halted: {breaker.is_halted()}")

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
    See live/RUNBOOK.md's most recent event-exposure section."""
    settings = config.load_settings().live
    from .live.credentials import MissingCredentialsError, load_api_credentials
    from .live.event_exposure import compute_event_exposures, resolve_capital_reference_usd
    from .live.ledger import sum_position_pnl
    from .live.us_client import CryptographyDependencyMissing, LiveUsClient, UsApiError

    try:
        credentials = load_api_credentials()
        client = LiveUsClient(credentials=credentials, settings=settings)
    except (MissingCredentialsError, CryptographyDependencyMissing) as exc:
        print(f"Cannot compute event exposure: {exc}")
        return

    try:
        positions = client.get_all_positions()
    except UsApiError as exc:
        print(f"Could not fetch positions: {exc}")
        return

    equity_settings = config.load_settings().equity_protection
    total_pnl = sum_position_pnl(positions)
    capital_reference_usd = resolve_capital_reference_usd(
        equity_settings.starting_capital_usd, total_pnl, positions,
    )
    exposures = compute_event_exposures(positions, capital_reference_usd)
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
    print("Circuit breaker reset. Trading may resume on the next refresh cycle.")


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
    "live-status": cmd_live_status,
    "live-reconcile-orders": cmd_live_reconcile_orders,
    "live-event-exposure": cmd_live_event_exposure,
    "live-fills": cmd_live_fills,
    "live-family-performance": cmd_live_family_performance,
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
