"""CLI rendering for scan results, watchlist, rejections, and portfolio."""

from __future__ import annotations

from tabulate import tabulate

from .live.event_exposure import EventExposure
from .live.family_performance import FamilyPerformance
from .live.reconciliation import ReconciliationReport
from .models import FilterResult, PortfolioSnapshot, ScoredMarket

# Ledger-known orders no longer open can realistically number in the
# thousands over a long-running bot (the ledger is append-only, never
# pruned) -- only ever show the most recently posted handful, never dump
# the full list to the terminal.
_STALE_LEDGER_SAMPLE_SIZE = 10

# Same rationale as _STALE_LEDGER_SAMPLE_SIZE -- fills.json is append-only.
_RECENT_FILLS_SAMPLE_SIZE = 20


def _fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def render_watchlist(scored_markets: list[ScoredMarket], top_n: int = 20) -> str:
    ranked = sorted(scored_markets, key=lambda m: m.total_score, reverse=True)[:top_n]
    if not ranked:
        return "No markets to show. Run `scan` first."

    rows = []
    for m in ranked:
        market = m.market
        rows.append([
            m.question[:60],
            f"{m.total_score:.1f}",
            m.recommendation,
            _fmt_money(market.liquidity) if market else "-",
            f"{market.spread:.4f}" if market and market.spread is not None else "-",
            _fmt_money(market.volume_24h) if market else "-",
            (market.end_date or "-")[:10] if market else "-",
            (market.category or "-") if market else "-",
        ])

    headers = ["Question", "Score", "Recommendation", "Liquidity", "Spread", "24h Volume", "Ends", "Category"]
    return tabulate(rows, headers=headers, tablefmt="simple")


def render_rejections(results: list[FilterResult]) -> str:
    rejected = [r for r in results if not r.accepted]
    if not rejected:
        return "No rejected markets."

    rows = [[r.question[:60], "; ".join(r.reasons)] for r in rejected]
    return tabulate(rows, headers=["Question", "Rejection Reasons"], tablefmt="simple")


def render_open_positions(open_trades: list[dict]) -> str:
    if not open_trades:
        return "No open paper positions."

    rows = [
        [
            t["trade_id"][:8],
            t["market_id"],
            t["outcome"],
            f"{t['entry_price']:.4f}",
            _fmt_money(t["size_usd"]),
            f"{t['shares']:.4f}",
            t["entry_time"][:19],
            t["reason_entry"][:40],
        ]
        for t in open_trades
    ]
    headers = ["Trade ID", "Market", "Outcome", "Entry Price", "Size", "Shares", "Opened", "Reason"]
    return tabulate(rows, headers=headers, tablefmt="simple")


def render_closed_trades(closed_trades: list[dict]) -> str:
    if not closed_trades:
        return "No closed paper trades yet."

    rows = [
        [
            t["trade_id"][:8],
            t["market_id"],
            t["outcome"],
            f"{t['entry_price']:.4f}",
            f"{t.get('exit_price', 0):.4f}",
            _fmt_money(t.get("realized_pnl") or 0),
            (t.get("reason_exit") or "")[:30],
        ]
        for t in closed_trades
    ]
    headers = ["Trade ID", "Market", "Outcome", "Entry", "Exit", "Realized P/L", "Exit Reason"]
    return tabulate(rows, headers=headers, tablefmt="simple")


def render_live_quote_preview(
    question: str,
    market_id: str,
    reference_price: float,
    tick_size: float,
    bid: float,
    ask: float,
    order_shares_min: float,
    order_shares_max: float,
) -> str:
    rows = [
        ["Market", question[:70]],
        ["Market slug", market_id],
        ["Reference price (book mid)", f"{reference_price:.4f}"],
        ["Tick size", tick_size],
        ["Would quote BID", f"{bid:.4f}"],
        ["Would quote ASK", f"{ask:.4f}"],
        ["Captured edge", f"{(ask - bid) * 100:.2f}c per share"],
        ["Order size range", f"{order_shares_min:,.2f} - {order_shares_max:,.2f} shares per side"],
    ]
    return tabulate(rows, headers=["Field", "Value"], tablefmt="simple")


def render_reconciliation(report: ReconciliationReport) -> str:
    sections = [tabulate(
        [
            ["Open orders on exchange", report.open_order_count],
            ["  bot-owned", len(report.bot_owned_orders)],
            ["  unknown/manual", len(report.unknown_orders)],
            ["  missing an id field (unclassifiable)", report.orders_missing_id_count],
            ["Known bot order ids (lifetime)", report.known_order_id_count],
            ["Ledger orders no longer open (filled/cancelled)", len(report.stale_ledger_orders)],
            ["Positions considered", report.position_count],
            ["Unmanaged positions (no bot-owned order there)", len(report.unmanaged_positions)],
        ],
        headers=["Metric", "Value"], tablefmt="simple",
    )]

    if report.ledger_appears_empty:
        sections.append(
            "WARNING: the local ledger reports zero known order ids while the "
            "exchange has open orders. This may mean the ledger file failed to "
            "read (see live/ledger.py::get_known_order_ids) rather than the bot "
            "genuinely having placed nothing -- every 'unknown' order and "
            "'unmanaged' position below could actually be bot-owned. Check "
            "logs/bot.log for a ledger read error before trusting this report."
        )

    if report.unknown_orders:
        rows = [
            [o.get("id") or o.get("orderId") or "-", o.get("marketSlug") or o.get("market_slug") or "-"]
            for o in report.unknown_orders
        ]
        sections.append("Unknown/manual open orders:\n" + tabulate(
            rows, headers=["Order ID", "Market"], tablefmt="simple"
        ))

    if report.unmanaged_positions:
        rows = [
            [p.get("market_slug", "-"), p.get("netPositionDecimal", "-")]
            for p in report.unmanaged_positions
        ]
        sections.append("Unmanaged positions:\n" + tabulate(
            rows, headers=["Market", "Net Position"], tablefmt="simple"
        ))

    if report.stale_ledger_orders:
        sample = report.stale_ledger_orders[-_STALE_LEDGER_SAMPLE_SIZE:]
        rows = [[s["order_id"], s["market_slug"] or "-"] for s in sample]
        header = (
            f"Showing {len(sample)} most recently posted of "
            f"{len(report.stale_ledger_orders)} ledger order(s) no longer open:"
        )
        sections.append(header + "\n" + tabulate(
            rows, headers=["Order ID", "Market (at post time)"], tablefmt="simple"
        ))

    return "\n\n".join(sections)


def render_fills(fills: list[dict]) -> str:
    if not fills:
        return (
            "No fills recorded yet. Requires live-start in WebSocket mode "
            "with LIVE_ENABLE_PRIVATE_WEBSOCKET=true -- the REST-polling "
            "runner never populates this."
        )

    sample = fills[-_RECENT_FILLS_SAMPLE_SIZE:]
    rows = [
        [
            f.get("detected_at", "-"),
            f.get("market_slug") or "-",
            f.get("side") or "-",
            f.get("price") if f.get("price") is not None else "-",
            f.get("shares") if f.get("shares") is not None else "-",
            f.get("fill_quality") or "-",
            f.get("markout_1m_cents") if f.get("markout_1m_cents") is not None else "-",
            f.get("markout_5m_cents") if f.get("markout_5m_cents") is not None else "-",
        ]
        for f in reversed(sample)
    ]
    header = (
        f"Showing {len(sample)} most recent of {len(fills)} recorded fill(s) "
        "(detected_at is when the bot noticed the fill, not necessarily "
        "when it executed; markout columns are blank until the fill's "
        "1-minute/5-minute mark has actually passed -- see "
        "live/RUNBOOK.md's \"-9.\" section):"
    )
    return header + "\n" + tabulate(
        rows,
        headers=["Detected At", "Market", "Side", "Price", "Shares", "Quality", "Markout 1m", "Markout 5m"],
        tablefmt="simple",
    )


def render_event_exposure(exposures: list[EventExposure], capital_reference_usd: float | None) -> str:
    if not exposures:
        return "No open positions -- nothing to group by event."

    if capital_reference_usd is None:
        header = (
            "Could not resolve a capital reference (set "
            "EQUITY_PROTECTION_STARTING_CAPITAL_USD, or hold at least one "
            "position, to see % of capital) -- showing raw dollar figures "
            "only:"
        )
    else:
        header = f"Capital reference: ${capital_reference_usd:,.2f}. "

    # Reporting-only calls (this CLI command, outside a live refresh cycle)
    # have no Market.raw available for any of these slugs, so every bucket
    # key here always came from the slug heuristic, never eventSlug/eventId
    # -- see live/event_exposure.py::derive_event_bucket_key.
    header += (
        "Bucket keys are inferred from market slugs (see "
        "live/RUNBOOK.md's most recent event-exposure section) -- most "
        "concentrated first:"
    )

    ordered = sorted(
        exposures,
        key=lambda e: (e.pct_of_capital is not None, e.pct_of_capital or e.cost_basis_usd),
        reverse=True,
    )
    rows = [
        [
            e.bucket_key,
            e.market_count,
            _fmt_money(e.cost_basis_usd),
            _fmt_money(e.cash_value_usd),
            _fmt_money(e.unrealized_pnl_usd),
            f"{e.pct_of_capital * 100:.1f}%" if e.pct_of_capital is not None else "-",
        ]
        for e in ordered
    ]
    return header + "\n" + tabulate(
        rows,
        headers=["Event Bucket", "Markets", "Cost Basis", "Cash Value", "Unrealized P&L", "% of Capital"],
        tablefmt="simple",
    )


def render_family_performance(performances: list[FamilyPerformance]) -> str:
    if not performances:
        return (
            "No fills recorded yet. Requires live-start in WebSocket mode "
            "with LIVE_ENABLE_PRIVATE_WEBSOCKET=true -- the REST-polling "
            "runner never populates fills.json."
        )

    header = (
        "Family is a coarse, best-effort heuristic derived from each "
        "market's slug (see live/RUNBOOK.md's most recent risk-tightening "
        "section) -- not a guaranteed taxonomy. Markout (mark-to-market at "
        "a fixed delay after the fill), NOT realized P&L -- this codebase "
        "has no per-fill realized P&L attribution. Worst average 1-minute "
        "markout first:"
    )

    ordered = sorted(
        performances,
        key=lambda p: (
            p.avg_markout_1m_cents is None,
            p.avg_markout_1m_cents if p.avg_markout_1m_cents is not None else 0.0,
        ),
    )
    rows = [
        [
            p.family,
            p.fill_count,
            f"{p.avg_markout_1m_cents:.2f}c" if p.avg_markout_1m_cents is not None else "-",
            f"{p.median_markout_1m_cents:.2f}c" if p.median_markout_1m_cents is not None else "-",
            f"{p.avg_markout_5m_cents:.2f}c" if p.avg_markout_5m_cents is not None else "-",
            f"{p.median_markout_5m_cents:.2f}c" if p.median_markout_5m_cents is not None else "-",
            p.favorable_count,
            p.adverse_count,
            p.neutral_count,
            p.unresolved_count,
        ]
        for p in ordered
    ]
    return header + "\n" + tabulate(
        rows,
        headers=[
            "Family", "Fills", "Avg 1m", "Median 1m", "Avg 5m", "Median 5m",
            "Favorable", "Adverse", "Neutral", "Unresolved",
        ],
        tablefmt="simple",
    )


def render_portfolio_summary(snapshot: PortfolioSnapshot) -> str:
    rows = [
        ["Cash", _fmt_money(snapshot.cash)],
        ["Open Positions Value", _fmt_money(snapshot.open_positions_value)],
        ["Total Equity", _fmt_money(snapshot.total_equity)],
        ["Realized P/L", _fmt_money(snapshot.realized_pnl)],
        ["Unrealized P/L", _fmt_money(snapshot.unrealized_pnl)],
        ["Win Rate", f"{snapshot.win_rate:.1f}%"],
        ["Avg Return per Trade", f"{snapshot.avg_return_pct:.2f}%"],
        ["Max Drawdown", f"{snapshot.max_drawdown_pct:.2f}%"],
        ["Open Positions", snapshot.num_open_positions],
        ["Closed Trades", snapshot.num_closed_trades],
    ]
    return tabulate(rows, headers=["Metric", "Value"], tablefmt="simple")
