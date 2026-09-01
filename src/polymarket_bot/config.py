"""Central configuration.

Only public, unauthenticated API settings and tunable thresholds live here.
No API keys, wallet addresses, or private keys are read or stored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
PAPER_TRADES_DIR = DATA_DIR / "paper_trades"
LIVE_TRADES_DIR = DATA_DIR / "live_trades"
REPORTS_DIR = DATA_DIR / "reports"
LOGS_DIR = PROJECT_ROOT / "logs"


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


def _env_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class APISettings:
    # Polymarket US's public, unauthenticated market-data API. Not to be
    # confused with the international polymarket.com Gamma/CLOB APIs, which
    # this project no longer targets (the user is restricted to .us).
    gateway_base_url: str = field(
        default_factory=lambda: _env_str(
            "GATEWAY_API_BASE", "https://gateway.polymarket.us"
        )
    )
    timeout_seconds: float = field(
        default_factory=lambda: _env_float("REQUEST_TIMEOUT_SECONDS", 10)
    )
    max_retries: int = field(
        default_factory=lambda: _env_int("REQUEST_MAX_RETRIES", 3)
    )
    backoff_base_seconds: float = 0.5
    page_limit: int = 100


@dataclass(frozen=True)
class FilterSettings:
    min_liquidity: float = 1000.0
    min_volume_24h: float = 500.0
    max_spread: float = 0.10
    min_hours_to_close: float = 2.0
    max_days_to_close: float = 365.0
    min_price: float = 0.05
    max_price: float = 0.95
    require_order_book: bool = True
    allowed_categories: tuple[str, ...] | None = None  # None = allow all
    unclear_keywords: tuple[str, ...] = (
        "tbd",
        "unclear",
        "test market",
        "do not trade",
    )


@dataclass(frozen=True)
class ScoringSettings:
    liquidity_max: int = 20
    spread_max: int = 20
    volume_max: int = 15
    time_to_resolution_max: int = 10
    price_stability_max: int = 10
    clarity_max: int = 10
    category_risk_max: int = 5
    risk_reward_max: int = 10

    reject_below: int = 40
    watch_below: int = 60
    strong_watch_below: int = 80
    # score >= strong_watch_below -> PAPER_CANDIDATE

    high_risk_categories: tuple[str, ...] = ("crypto", "weather", "meme")


@dataclass(frozen=True)
class RiskSettings:
    max_position_size_usd: float = 100.0
    max_open_positions: int = 10
    max_total_exposure_usd: float = 500.0
    max_position_pct_of_cash: float = 0.20


@dataclass(frozen=True)
class PaperTradingSettings:
    starting_cash: float = field(
        default_factory=lambda: _env_float("PAPER_STARTING_CASH", 1000.0)
    )
    default_mode: str = "manual_paper"  # never auto-paper
    fake_slippage_bps: float = 25.0  # 0.25%


@dataclass(frozen=True)
class LiveTradingSettings:
    """Non-secret tunables for live market-making on Polymarket US. No key
    material lives here or anywhere in this module — see
    live/credentials.py for credential loading."""

    enabled: bool = field(default_factory=lambda: _env_bool("LIVE_TRADING_ENABLED", False))
    # Polymarket US's authenticated trading API (Ed25519-signed requests).
    api_base_url: str = field(
        default_factory=lambda: _env_str("LIVE_API_BASE", "https://api.polymarket.us")
    )
    order_shares_min: float = field(
        default_factory=lambda: _env_float("LIVE_ORDER_SHARES_MIN", 15.0)
    )
    order_shares_max: float = field(
        default_factory=lambda: _env_float("LIVE_ORDER_SHARES_MAX", 20.0)
    )
    newest_market_scan_limit: int = field(
        default_factory=lambda: _env_int("LIVE_NEWEST_MARKET_SCAN_LIMIT", 500)
    )
    recent_market_scan_limit: int = field(
        default_factory=lambda: _env_int("LIVE_RECENT_MARKET_SCAN_LIMIT", 5000)
    )
    max_orders_per_cycle: int = field(
        default_factory=lambda: _env_int("LIVE_MAX_ORDERS_PER_CYCLE", 10)
    )
    use_websocket: bool = field(
        default_factory=lambda: _env_bool("LIVE_USE_WEBSOCKET", True)
    )
    websocket_responses_debounced: bool = field(
        default_factory=lambda: _env_bool("LIVE_WEBSOCKET_RESPONSES_DEBOUNCED", True)
    )
    websocket_stale_after_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_WEBSOCKET_STALE_AFTER_SECONDS", 10.0)
    )
    websocket_reconnect_initial_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_WEBSOCKET_RECONNECT_INITIAL_SECONDS", 1.0)
    )
    websocket_reconnect_max_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_WEBSOCKET_RECONNECT_MAX_SECONDS", 30.0)
    )
    websocket_candidate_refresh_seconds: int = field(
        default_factory=lambda: _env_int("LIVE_WEBSOCKET_CANDIDATE_REFRESH_SECONDS", 900)
    )
    # A failed candidate refresh (scan/network error) retries after this
    # much shorter interval instead of waiting the full
    # websocket_candidate_refresh_seconds window -- short enough to recover
    # promptly from a transient issue, long enough not to hammer the scan
    # API every single quote cycle during a sustained outage.
    websocket_candidate_refresh_retry_seconds: int = field(
        default_factory=lambda: _env_int("LIVE_WEBSOCKET_CANDIDATE_REFRESH_RETRY_SECONDS", 60)
    )
    websocket_subscription_limit: int = field(
        default_factory=lambda: _env_int("LIVE_WEBSOCKET_SUBSCRIPTION_LIMIT", 100)
    )
    # Number of ranked markets to watch over WebSocket.  This is
    # deliberately separate from max_orders_per_cycle: watching a broader
    # pool lets the runner skip thin/blocked markets and still find up to
    # the configured number of actionable orders.
    websocket_candidate_pool_size: int = field(
        default_factory=lambda: _env_int("LIVE_WEBSOCKET_CANDIDATE_POOL_SIZE", 20)
    )
    # REST retry/backoff for us_client.py's read-only calls (get_open_orders,
    # get_all_positions, get_position) -- NOT applied to order-placement/
    # cancellation, which stay single-attempt fail-fast (retrying a write
    # blindly risks a duplicate action if the first attempt actually
    # succeeded server-side but the response was lost). See
    # live/RUNBOOK.md's most recent section -- a real 429 on
    # GET /v1/orders/open prompted this.
    request_timeout_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_REQUEST_TIMEOUT_SECONDS", 10.0)
    )
    request_max_retries: int = field(
        default_factory=lambda: _env_int("LIVE_REQUEST_MAX_RETRIES", 3)
    )
    request_backoff_base_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_REQUEST_BACKOFF_BASE_SECONDS", 0.5)
    )
    # Multiplies the computed exponential backoff specifically for a 429 --
    # "back off harder" than a generic transient failure.
    rate_limit_backoff_multiplier: float = field(
        default_factory=lambda: _env_float("LIVE_RATE_LIMIT_BACKOFF_MULTIPLIER", 4.0)
    )
    # Scaffold only (see live/ws_private.py, live/RUNBOOK.md): streams
    # order/position/balance updates into an in-memory store for
    # visibility, but no trading decision reads from it yet. Escape hatch
    # in case it ever misbehaves -- doesn't affect trading logic either way.
    enable_private_websocket: bool = field(
        default_factory=lambda: _env_bool("LIVE_ENABLE_PRIVATE_WEBSOCKET", True)
    )
    private_order_state_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_PRIVATE_ORDER_STATE_ENABLED", True)
    )
    private_state_reconcile_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_PRIVATE_STATE_RECONCILE_SECONDS", 1200.0)
    )
    private_state_stale_after_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_PRIVATE_STATE_STALE_AFTER_SECONDS", 120.0)
    )
    private_state_degraded_cancel_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_PRIVATE_STATE_DEGRADED_CANCEL_SECONDS", 120.0)
    )
    max_days_to_close: float = field(
        default_factory=lambda: _env_float("LIVE_MAX_DAYS_TO_CLOSE", 14.0)
    )
    min_hours_to_close: float = field(
        default_factory=lambda: _env_float("LIVE_MIN_HOURS_TO_CLOSE", 2.0)
    )
    max_started_event_hours: float = field(
        default_factory=lambda: _env_float("LIVE_MAX_STARTED_EVENT_HOURS", 0.0)
    )
    # New exposure is blocked this many minutes before a known event start.
    # Unlike the old same-UTC-day rule, this still permits market-making
    # earlier on game day while avoiding the highest-risk pregame window.
    pre_event_reduce_only_minutes: float = field(
        default_factory=lambda: _env_float("LIVE_PRE_EVENT_REDUCE_ONLY_MINUTES", 60.0)
    )
    min_liquidity: float = field(
        default_factory=lambda: _env_float("LIVE_MIN_LIQUIDITY", 100.0)
    )
    min_volume_24h: float = field(
        default_factory=lambda: _env_float("LIVE_MIN_VOLUME_24H", 0.0)
    )
    # A market rejected ONLY for liquidity/volume (every other quality-bar
    # check already passed) gets one more chance via a real L2 order-book
    # depth lookup, since volume/volume24hr can go missing upstream (a
    # confirmed real incident -- see live/RUNBOOK.md's most recent section)
    # without the market itself actually being illiquid. Bounded: the
    # account-wide rate limit is 20 req/s (docs.polymarket.us), so doing
    # this across an entire multi-thousand-market scan is not viable. 0
    # disables the fallback entirely.
    # Default raised 200 -> 500 (2026-07-20): the upstream liquidity/volume
    # outage above is still ongoing days later with no local fix available,
    # so a bigger fallback lookup budget is the only actionable lever for
    # candidate diversity -- see live/RUNBOOK.md's most recent section. Only
    # costs time once per websocket_candidate_refresh_seconds (900s default)
    # candidate refresh, not per quoting cycle.
    liquidity_fallback_max_lookups: int = field(
        default_factory=lambda: _env_int("LIVE_LIQUIDITY_FALLBACK_MAX_LOOKUPS", 500)
    )
    # Sticky market allocation (see live/multi_market_maker.py's
    # _pinned_markets/_release_pins_before_cycle/_update_sticky_pins): a flat
    # (no held position) candidate that wins the shared order budget stays
    # preferred over fresh ranking for this long, instead of the budget
    # being re-decided from scratch every refresh_interval_seconds cycle --
    # a confirmed real run alternated the winning markets on nearly every
    # ~10s cycle, so orders never rested long enough to fill (20 cycles, 62
    # posted legs, 0 fills). Kept independent of the enabled flag so it can
    # be retuned without also having to toggle the flag.
    sticky_market_allocation_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_STICKY_MARKET_ALLOCATION_ENABLED", True)
    )
    sticky_market_hold_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_STICKY_MARKET_HOLD_SECONDS", 300.0)
    )
    # A resting order within this many ticks of the freshly computed target
    # price is kept (no cancel+repost) instead of requiring an exact-match
    # price -- confirmed real: even with sticky market selection keeping the
    # same markets selected, orders were still being replaced roughly every
    # ~50s because ordinary tick-to-tick book movement almost never matches
    # the old effectively-exact tolerance. See live/RUNBOOK.md's most
    # recent section.
    price_hysteresis_ticks: float = field(
        default_factory=lambda: _env_float("LIVE_PRICE_HYSTERESIS_TICKS", 1.0)
    )
    # A resting order younger than this is kept even if its price has moved
    # beyond the hysteresis band above -- discretionary repricing waits.
    # force_flatten/reduce_urgent (risk exits) always override both this and
    # price_hysteresis_ticks and reprice immediately regardless of age.
    min_resting_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_MIN_RESTING_SECONDS", 90.0)
    )
    # Kill switch for startup_recovery.py::recover_from_prior_crash(),
    # called once at the start of run_forever() (both runner.py and
    # ws_runner.py) -- detects a session left "running" by a process that
    # never reached clean shutdown (a confirmed real incident: PID 13880
    # died abruptly, sessions.json stayed stuck "running" forever, and its
    # resting orders were left unmanaged) and cancels bot-recognized
    # leftover orders before this run places any new ones.
    startup_crash_recovery_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_STARTUP_CRASH_RECOVERY_ENABLED", True)
    )
    # Execution-backfill (GET /v1/order/{orderId} checks for a fill the
    # private WebSocket never delivered -- see live/RUNBOOK.md's most recent
    # section) is bounded and paced the same way: capped per cycle so a long
    # outage with many stale orders can't make an unbounded number of calls
    # in one pass, and paced so it doesn't crowd out the same cycle's order
    # placement/cancellation/reconciliation calls against the shared 20 req/s
    # account-wide limit (docs.polymarket.us).
    execution_backfill_max_lookups_per_cycle: int = field(
        default_factory=lambda: _env_int("LIVE_EXECUTION_BACKFILL_MAX_LOOKUPS_PER_CYCLE", 5)
    )
    execution_backfill_min_interval_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_EXECUTION_BACKFILL_MIN_INTERVAL_SECONDS", 0.1)
    )
    max_spread: float = field(
        default_factory=lambda: _env_float("LIVE_MAX_SPREAD", 0.98)
    )
    exclude_market_types: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple("LIVE_EXCLUDE_MARKET_TYPES", ())
    )
    exclude_question_keywords: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple(
            "LIVE_EXCLUDE_QUESTION_KEYWORDS",
            ("champion", "championship", "mvp", "cy young", "pennant", "temperature"),
        )
    )
    # Slug-prefix hard exclusion -- e.g. "tc-temp" for intraday weather
    # threshold markets, where the edge depends on external meteorological
    # truth the bot doesn't model. Checked via market_id.startswith(prefix),
    # so it works even without raw market data (see
    # market_selection.py::is_excluded_market_family).
    exclude_slug_prefixes: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple("LIVE_EXCLUDE_SLUG_PREFIXES", ("tc-temp",))
    )
    # Hard exclusion by raw["sportsMarketType"] directly -- deliberately NOT
    # the same mechanism as exclude_market_types above, which reads
    # marketType-or-sportsMarketType via _market_type()'s fallback chain and
    # therefore can never reach sportsMarketType for esports records
    # (marketType is always truthy there -- "drawable_outcome"/"moneyline").
    # Confirmed 100% exclusive to esports (dota2/lol/valorant/cs2) across a
    # full 5000-record raw snapshot; category ("sports") and marketType are
    # both shared with traditional sports, so neither existing check can
    # target esports cleanly. See market_selection.py::is_excluded_market_family.
    exclude_sports_market_types: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple(
            "LIVE_EXCLUDE_SPORTS_MARKET_TYPES", ("esports_match_winner",)
        )
    )
    min_top_depth_shares: float = field(
        default_factory=lambda: _env_float("LIVE_MIN_TOP_DEPTH_SHARES", 10.0)
    )
    min_total_depth_shares: float = field(
        default_factory=lambda: _env_float("LIVE_MIN_TOTAL_DEPTH_SHARES", 25.0)
    )
    depth_levels_to_check: int = field(
        default_factory=lambda: _env_int("LIVE_DEPTH_LEVELS_TO_CHECK", 3)
    )
    # Safe default is True: require a real, fresh L2 order book before
    # quoting at all. Without this, a stale/unavailable book silently fell
    # back to BBO-only pricing -- quoting from a single top-of-book number
    # with no visibility into real depth. Only disable this if you have a
    # specific, understood reason to accept BBO-only quoting.
    require_l2_depth: bool = field(
        default_factory=lambda: _env_bool("LIVE_REQUIRE_L2_DEPTH", True)
    )
    # Minimum spread (in cents) that must remain after improving both the
    # real best bid and best ask by one tick each, before the bot will
    # bother quoting a market at all. Below this, there's no real edge to
    # capture -- see live/pricing.py::compute_book_aware_quote. Replaces an
    # earlier design that quoted a fixed 3c spread around the midpoint
    # regardless of the real book, which left the bot's orders resting
    # behind the market on essentially every market tested (see
    # live/RUNBOOK.md's profitability section).
    min_edge_cents: float = field(
        default_factory=lambda: _env_float("LIVE_MIN_EDGE_CENTS", 0.5)
    )
    # Selection ranking defaults to a heuristic expected-value proxy:
    # captured spread (after improving both sides by one tick) times a
    # volume/liquidity-based fill-confidence score. This is deliberately
    # uncalibrated until the live ledger has real fill history; set false to
    # return to raw-spread-first ranking.
    rank_by_expected_value: bool = field(
        default_factory=lambda: _env_bool("LIVE_RANK_BY_EXPECTED_VALUE", True)
    )
    # _depth_proxy value at which fill confidence saturates to 1.0. Starting
    # heuristic only; revisit once real fills are available to calibrate.
    fill_confidence_reference_depth: float = field(
        default_factory=lambda: _env_float("LIVE_FILL_CONFIDENCE_REFERENCE_DEPTH", 5000.0)
    )
    # Small floor so brand-new zero-volume markets with genuine spread edge
    # are heavily discounted but not erased entirely from ranking.
    min_fill_confidence: float = field(
        default_factory=lambda: _env_float("LIVE_MIN_FILL_CONFIDENCE", 0.1)
    )
    # Maximum position value (current mark-to-market, from the positions
    # API) before the bot stops adding to it -- it will still quote the
    # reducing side (subject to the cost-basis floor above) but skips the
    # side that would increase the position further. ~2x one order's worth
    # by default, so it can absorb one extra fill beyond a single cycle's
    # target size before it stops adding more.
    max_position_usd: float = field(
        default_factory=lambda: _env_float("LIVE_MAX_POSITION_USD", 40.0)
    )
    inventory_skew_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_INVENTORY_SKEW_ENABLED", True)
    )
    inventory_skew_threshold_usd: float = field(
        default_factory=lambda: _env_float("LIVE_INVENTORY_SKEW_THRESHOLD_USD", 10.0)
    )
    inventory_reducing_size_multiplier: float = field(
        default_factory=lambda: _env_float("LIVE_INVENTORY_REDUCING_SIZE_MULTIPLIER", 1.5)
    )
    inventory_increasing_size_multiplier: float = field(
        default_factory=lambda: _env_float("LIVE_INVENTORY_INCREASING_SIZE_MULTIPLIER", 0.5)
    )
    # Adverse-selection guard: skip quoting a market that just moved a lot
    # (the bot's 60s-ish refresh cadence can't react fast enough to be safe
    # quoting into a market that's actively repricing).
    volatility_filter_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_VOLATILITY_FILTER_ENABLED", True)
    )
    max_recent_move_cents: float = field(
        default_factory=lambda: _env_float("LIVE_MAX_RECENT_MOVE_CENTS", 3.0)
    )
    volatility_window_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_VOLATILITY_WINDOW_SECONDS", 300.0)
    )
    # A resting order can otherwise sit unrefreshed for up to
    # refresh_interval_seconds -- long enough for a fast in-play market to
    # move well past max_recent_move_cents and fill before the volatility
    # guard above ever gets a chance to react. Off by default (ordinary
    # live-start is unaffected); the guarded pilot settings-builders in
    # main.py turn it on explicitly.
    fast_reprice_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_FAST_REPRICE_ENABLED", False)
    )
    fast_reprice_check_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_FAST_REPRICE_CHECK_SECONDS", 5.0)
    )
    # Real-activity ranking (see live/ws_market_data.py's rolling-window
    # trade/sharesTraded/book-update tracking, fed into
    # market_selection.py::select_target_markets' activity_scores param).
    # Only ever informs re-ranking of markets already being watched over
    # the WebSocket -- the REST market-list scan itself has no activity
    # fields (confirmed live against the real API), so a market's first-
    # ever selection still relies on the static spread/depth formula.
    activity_tracking_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_ACTIVITY_TRACKING_ENABLED", True)
    )
    # Keep collecting the trade tape even when a fixed strategy profile wants
    # to preserve the selector's original ordering.  The July 5 pilot uses
    # the tape for observation/attribution, but deliberately disables only
    # the per-cycle activity re-rank.
    activity_rerank_enabled: bool = True
    activity_window_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_ACTIVITY_WINDOW_SECONDS", 300.0)
    )
    # Observation-first entry qualification. Observation-only mode is
    # deliberately passive after startup recovery: it scans and records
    # market data but never enters the maker/risk/cancel loop.
    observation_only_mode: bool = field(
        default_factory=lambda: _env_bool("LIVE_OBSERVATION_ONLY_MODE", False)
    )
    observation_gate_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_OBSERVATION_GATE_ENABLED", True)
    )
    observation_universe_size: int = field(
        default_factory=lambda: _env_int("LIVE_OBSERVATION_UNIVERSE_SIZE", 100)
    )
    observation_retained_active_slots: int = field(
        default_factory=lambda: _env_int("LIVE_OBSERVATION_RETAINED_ACTIVE_SLOTS", 70)
    )
    observation_active_hold_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_OBSERVATION_ACTIVE_HOLD_SECONDS", 7200.0)
    )
    observation_evidence_window_hours: float = field(
        default_factory=lambda: _env_float("LIVE_OBSERVATION_EVIDENCE_WINDOW_HOURS", 72.0)
    )
    observation_min_observed_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_OBSERVATION_MIN_OBSERVED_SECONDS", 7200.0)
    )
    observation_min_trades: int = field(
        default_factory=lambda: _env_int("LIVE_OBSERVATION_MIN_TRADES", 20)
    )
    observation_min_hypothetical_fills: int = field(
        default_factory=lambda: _env_int("LIVE_OBSERVATION_MIN_HYPOTHETICAL_FILLS", 5)
    )
    observation_min_fill_rate: float = field(
        default_factory=lambda: _env_float("LIVE_OBSERVATION_MIN_FILL_RATE", 0.05)
    )
    observation_min_markout_samples: int = field(
        default_factory=lambda: _env_int("LIVE_OBSERVATION_MIN_MARKOUT_SAMPLES", 5)
    )
    observation_min_avg_markout_cents: float = field(
        default_factory=lambda: _env_float("LIVE_OBSERVATION_MIN_AVG_MARKOUT_CENTS", 0.25)
    )
    observation_min_avg_markout_5m_cents: float = field(
        default_factory=lambda: _env_float("LIVE_OBSERVATION_MIN_AVG_MARKOUT_5M_CENTS", 0.0)
    )
    observation_min_distinct_fill_episodes: int = field(
        default_factory=lambda: _env_int("LIVE_OBSERVATION_MIN_DISTINCT_FILL_EPISODES", 3)
    )
    observation_fill_episode_gap_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_OBSERVATION_FILL_EPISODE_GAP_SECONDS", 300.0)
    )
    observation_min_paper_round_trips: int = field(
        default_factory=lambda: _env_int("LIVE_OBSERVATION_MIN_PAPER_ROUND_TRIPS", 3)
    )
    observation_min_paper_pnl_usd: float = field(
        default_factory=lambda: _env_float("LIVE_OBSERVATION_MIN_PAPER_PNL_USD", 0.0)
    )
    # Signed Polymarket US fee coefficients used by the shadow execution
    # model: commission = theta * shares * price * (1-price). A negative
    # maker value is a rebate; a positive taker value is a fee. Keeping
    # these configurable prevents a fee-schedule change from silently
    # invalidating paper P/L.
    observation_maker_fee_theta: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_MAKER_FEE_THETA", -0.0125,
        )
    )
    observation_taker_fee_theta: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_TAKER_FEE_THETA", 0.06,
        )
    )
    observation_persist_interval_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_OBSERVATION_PERSIST_INTERVAL_SECONDS", 10.0)
    )
    # Observation must prove that its market-data feed was alive for most of
    # the fixed evaluation window.  A process that keeps scanning REST while
    # its L2/tape subscription is silent is not valid evidence.
    observation_feed_stale_after_seconds: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_FEED_STALE_AFTER_SECONDS", 300.0,
        )
    )
    observation_min_feed_coverage_ratio: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_MIN_FEED_COVERAGE_RATIO", 0.90,
        )
    )
    # Schema-v4 portfolio shadow comparison.  These settings are deliberately
    # independent of the ordinary live selector/risk envelope: observation is
    # measuring an opportunity set, not granting permission to trade it.
    observation_evaluation_hours: float = field(
        default_factory=lambda: _env_float("LIVE_OBSERVATION_EVALUATION_HOURS", 48.0)
    )
    observation_legacy_max_started_event_hours: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_LEGACY_MAX_STARTED_EVENT_HOURS", 6.0
        )
    )
    observation_legacy_max_spread: float = field(
        default_factory=lambda: _env_float("LIVE_OBSERVATION_LEGACY_MAX_SPREAD", 0.98)
    )
    observation_legacy_order_shares: float = field(
        default_factory=lambda: _env_float("LIVE_OBSERVATION_LEGACY_ORDER_SHARES", 17.5)
    )
    observation_controlled_max_started_event_hours: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_CONTROLLED_MAX_STARTED_EVENT_HOURS", 3.0
        )
    )
    # July 5 style: matches the real 2026-07-05/06 account activity
    # (data/reports/july5_old_bot_reconstruction.md) -- wide spread, no
    # pregame pause, 17.5-share GTC orders -- but unlike the `legacy`
    # profile above, deliberately does NOT disable the extreme-price/
    # payoff-shape guards (see _july5_settings()). See live/RUNBOOK.md 44.
    observation_july5_max_started_event_hours: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_JULY5_MAX_STARTED_EVENT_HOURS", 6.0
        )
    )
    observation_july5_max_spread: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_JULY5_MAX_SPREAD", 0.98
        )
    )
    observation_july5_order_shares: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_JULY5_ORDER_SHARES", 17.5
        )
    )
    observation_controlled_max_spread: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_CONTROLLED_MAX_SPREAD", 0.50
        )
    )
    observation_controlled_order_shares: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_CONTROLLED_ORDER_SHARES", 1.0
        )
    )
    observation_profile_refresh_seconds: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_PROFILE_REFRESH_SECONDS", 60.0
        )
    )
    observation_profile_max_markets: int = field(
        default_factory=lambda: _env_int("LIVE_OBSERVATION_PROFILE_MAX_MARKETS", 5)
    )
    observation_controlled_max_markets_per_event: int = field(
        default_factory=lambda: _env_int(
            "LIVE_OBSERVATION_CONTROLLED_MAX_MARKETS_PER_EVENT", 3
        )
    )
    observation_controlled_pregame_pause_minutes: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_CONTROLLED_PREGAME_PAUSE_MINUTES", 60.0
        )
    )
    observation_controlled_entry_cutoff_minutes: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_CONTROLLED_ENTRY_CUTOFF_MINUTES", 30.0
        )
    )
    observation_controlled_max_holding_hours: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_CONTROLLED_MAX_HOLDING_HOURS", 1.0
        )
    )
    observation_controlled_max_round_trips_per_market: int = field(
        default_factory=lambda: _env_int(
            "LIVE_OBSERVATION_CONTROLLED_MAX_ROUND_TRIPS_PER_MARKET", 2
        )
    )
    observation_controlled_min_round_trips: int = field(
        default_factory=lambda: _env_int(
            "LIVE_OBSERVATION_CONTROLLED_MIN_ROUND_TRIPS", 20
        )
    )
    observation_controlled_min_distinct_events: int = field(
        default_factory=lambda: _env_int(
            "LIVE_OBSERVATION_CONTROLLED_MIN_DISTINCT_EVENTS", 5
        )
    )
    observation_cohort_min_round_trips: int = field(
        default_factory=lambda: _env_int(
            "LIVE_OBSERVATION_COHORT_MIN_ROUND_TRIPS", 5
        )
    )
    observation_cohort_min_distinct_events: int = field(
        default_factory=lambda: _env_int(
            "LIVE_OBSERVATION_COHORT_MIN_DISTINCT_EVENTS", 2
        )
    )
    observation_controlled_min_profit_factor: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_CONTROLLED_MIN_PROFIT_FACTOR", 1.20
        )
    )
    observation_controlled_max_drawdown_usd: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_CONTROLLED_MAX_DRAWDOWN_USD", 3.0
        )
    )
    observation_controlled_max_event_profit_concentration: float = field(
        default_factory=lambda: _env_float(
            "LIVE_OBSERVATION_CONTROLLED_MAX_EVENT_PROFIT_CONCENTRATION", 0.50
        )
    )
    # The pilot flag is set by the dedicated command, never by live-start.
    # Its defaults are intentionally fixed and do not scale after success.
    pilot_mode: bool = False
    # Audit-only markers set by the command-specific settings builders.  They
    # are intentionally not environment variables: an .env edit must never
    # turn an ordinary live-start into an unqualified pilot.
    pilot_qualification_bypassed: bool = False
    pilot_strategy_profile: str = "controlled"
    pilot_entry_hours: float = field(
        default_factory=lambda: _env_float("LIVE_PILOT_ENTRY_HOURS", 3.5)
    )
    pilot_drain_minutes: float = field(
        default_factory=lambda: _env_float("LIVE_PILOT_DRAIN_MINUTES", 30.0)
    )
    pilot_max_round_trips_per_market: int = field(
        default_factory=lambda: _env_int("LIVE_PILOT_MAX_ROUND_TRIPS_PER_MARKET", 2)
    )
    refresh_interval_seconds: int = field(
        default_factory=lambda: _env_int("LIVE_REFRESH_INTERVAL_SECONDS", 60)
    )
    # After submitting any order on a market, do not act on that market
    # again until this delay has elapsed and full REST order/position state
    # has been reconciled. Private order and position updates are not atomic;
    # without this barrier a fill can remove an order before the position
    # update arrives, causing the bot to repeat an exit and reverse through
    # flat. Set <= 0 only for isolated tests.
    order_settlement_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_ORDER_SETTLEMENT_SECONDS", 15.0)
    )
    # Do not repeatedly churn the same market after completing a round trip
    # during one live-start process. The bot has no directional fair-value
    # model, and repeated re-entry after fills amplified adverse selection
    # and taker fees in the 2026-07-12 session.
    one_round_trip_per_market_per_session: bool = field(
        default_factory=lambda: _env_bool(
            "LIVE_ONE_ROUND_TRIP_PER_MARKET_PER_SESSION", True
        )
    )
    # Default is the lowest non-rejected tier, not PAPER_CANDIDATE. The real
    # quality floor (liquidity, volume, spread ceiling, price range,
    # time-to-close) is already enforced by MarketFilters before scoring
    # ever happens -- this tier is an additional, graduated score on top of
    # that, and it REWARDS a tight spread (good for a researcher/taker).
    # Requiring the top tier here would fight the live selection's own
    # widest-spread-first ranking, filtering out exactly the markets with
    # the most room to profitably improve on both sides.
    min_score_recommendation: str = field(
        default_factory=lambda: _env_str("LIVE_MIN_RECOMMENDATION", "WATCH")
    )
    # Unlike polymarket.com, it's NOT confirmed whether .us sports markets
    # force-cancel resting orders at game start -- see live/RUNBOOK.md.
    # "sports" is deliberately NOT in the default exclusion list pending that
    # verification, since sports markets are a large share of what's
    # actually listed on .us. "climate" (Polymarket's real tag for intraday
    # weather-threshold markets, e.g. "tc-temp-*" slugs -- confirmed via raw
    # data, there's no "weather" category) IS excluded by default: the edge
    # there depends on external meteorological truth the bot doesn't model.
    exclude_categories: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple("LIVE_EXCLUDE_CATEGORIES", ("climate",))
    )
    # Off by default: live/market_maker.py always uses OUTCOME_SIDE_YES for
    # both legs, which is only verified correct for a literal two-outcome
    # Yes/No market. Most of what's listed on .us is sports moneylines
    # (team names, not "Yes"/"No"), where this mapping is unverified -- see
    # live/RUNBOOK.md. Only set this true once that mapping is confirmed.
    allow_non_binary_markets: bool = field(
        default_factory=lambda: _env_bool("LIVE_ALLOW_NON_BINARY_MARKETS", False)
    )
    incentive_categories: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple(
            "LIVE_INCENTIVE_CATEGORIES",
            ("politics", "macro", "culture", "climate", "sports"),
        )
    )
    confirmation_phrase: str = field(
        default_factory=lambda: _env_str(
            "LIVE_CONFIRMATION_PHRASE",
            "I UNDERSTAND THIS PLACES REAL ORDERS WITH REAL MONEY",
        )
    )
    # Off by default, and NOT meant to be set in the shared .env -- only the
    # autostart wrapper script (scripts/run_live_autostart.ps1) sets this in
    # its own process environment, so manual/interactive runs of `live-start`
    # from a terminal still require the typed confirmation phrase. See
    # live/confirmation.py and live/RUNBOOK.md section 8.
    unattended_mode: bool = field(
        default_factory=lambda: _env_bool("LIVE_UNATTENDED_MODE", False)
    )
    # 1-minute/5-minute realized-spread markout tracking (live/fills.py) --
    # a real delayed measurement, separate from build_fill_record's own
    # single-snapshot edge_vs_current_mid_cents. See live/RUNBOOK.md's "-9."
    # section.
    markout_tracking_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_MARKOUT_TRACKING_ENABLED", True)
    )
    # If a fill's 60s/300s mark is overdue by more than this, resolve it to
    # None instead of computing a number against today's BBO -- prevents a
    # meaningless markout (and a garbage toxicity-EWMA input) for a fill
    # from hours/days ago, e.g. right after this feature is deployed or the
    # bot restarts after downtime.
    markout_max_staleness_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_MARKOUT_MAX_STALENESS_SECONDS", 900.0)
    )
    # A third, longer markout window -- 1m/5m missed a real slow-grind loss
    # (a fill that looked fine at 1m/5m kept drifting against the bot for
    # much longer). See live/RUNBOOK.md's most recent section.
    markout_long_window_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_MARKOUT_LONG_WINDOW_SECONDS", 900.0)
    )
    # Per-market toxicity-aware quote widening (live/toxicity_tracker.py):
    # tracks an EWMA of each market's 1-minute markout: crossing
    # toxicity_adverse_threshold_cents triggers a cooldown for that market
    # (wider min_edge_cents, halved size, reduce-only) -- see
    # live/market_maker.py's reduce_only param and
    # live/multi_market_maker.py's _effective_settings_for.
    toxicity_tracking_enabled: bool = field(
        default_factory=lambda: _env_bool("TOXICITY_TRACKING_ENABLED", True)
    )
    toxicity_ewma_alpha: float = field(
        default_factory=lambda: _env_float("TOXICITY_EWMA_ALPHA", 0.3)
    )
    toxicity_adverse_threshold_cents: float = field(
        default_factory=lambda: _env_float("TOXICITY_ADVERSE_THRESHOLD_CENTS", -1.0)
    )
    toxicity_cooldown_seconds: float = field(
        default_factory=lambda: _env_float("TOXICITY_COOLDOWN_SECONDS", 600.0)
    )
    toxicity_min_edge_multiplier: float = field(
        default_factory=lambda: _env_float("TOXICITY_MIN_EDGE_MULTIPLIER", 2.0)
    )
    toxicity_size_multiplier: float = field(
        default_factory=lambda: _env_float("TOXICITY_SIZE_MULTIPLIER", 0.5)
    )
    # Event-level correlation-aware capital allocation (live/event_exposure.py):
    # markets sharing the same underlying real-world event (see
    # derive_event_bucket_key) are capped as ONE risk bucket, not treated as
    # independently diversified positions. See live/RUNBOOK.md's most recent
    # event-exposure section.
    #
    # NOTE: these use FRACTION semantics (0.20 = 20%), unlike
    # EquityProtectionSettings.drawdown_from_peak_pct's PERCENT-NUMBER
    # semantics (20.0), despite the shared "_pct" suffix -- deliberate, do
    # not "fix" this into percent-number form, it would silently change cap
    # math by 100x.
    max_event_exposure_pct: float = field(
        default_factory=lambda: _env_float("LIVE_MAX_EVENT_EXPOSURE_PCT", 0.15)
    )
    warn_event_exposure_pct: float = field(
        default_factory=lambda: _env_float("LIVE_WARN_EVENT_EXPOSURE_PCT", 0.10)
    )
    max_markets_per_event: int = field(
        default_factory=lambda: _env_int("LIVE_MAX_MARKETS_PER_EVENT", 2)
    )
    # Tighter cap for sports stat props specifically (marketType=="props"),
    # since these are the exact market family behind the real over-
    # concentration incident that prompted this feature. Deliberately equal
    # to warn_event_exposure_pct today -- stat props go straight from
    # "under cap" to "fully reduce-only" with no soft warn-tier step, since
    # they're the highest-risk family this whole risk-tightening batch
    # targets. Not a bug -- see live/RUNBOOK.md's risk-tightening section.
    stat_prop_max_event_exposure_pct: float = field(
        default_factory=lambda: _env_float("LIVE_STAT_PROP_MAX_EVENT_EXPOSURE_PCT", 0.10)
    )
    # Warn-tier multipliers are deliberately gentler than toxicity's 2x/0.5x
    # and on their OWN config surface rather than reusing toxicity's -- this
    # is a preventive/soft signal with no adverse evidence behind it (unlike
    # toxicity, which fires on evidence of actually-bad recent fills), and
    # reusing toxicity's knobs would silently couple two unrelated concerns.
    event_exposure_warn_edge_multiplier: float = field(
        default_factory=lambda: _env_float("LIVE_EVENT_EXPOSURE_WARN_EDGE_MULTIPLIER", 1.25)
    )
    event_exposure_warn_size_multiplier: float = field(
        default_factory=lambda: _env_float("LIVE_EVENT_EXPOSURE_WARN_SIZE_MULTIPLIER", 0.75)
    )

    # Require extra edge before opening (never reducing) a position priced
    # near an extreme -- the payoff there is lopsided (a small premium
    # against a large potential loss), so the same 0.5c default min_edge_cents
    # isn't enough justification to take it on. See live/market_maker.py::
    # _resolve_leg_price.
    extreme_price_low_threshold: float = field(
        default_factory=lambda: _env_float("LIVE_EXTREME_PRICE_LOW_THRESHOLD", 0.15)
    )
    extreme_price_high_threshold: float = field(
        default_factory=lambda: _env_float("LIVE_EXTREME_PRICE_HIGH_THRESHOLD", 0.85)
    )
    extreme_price_min_edge_cents: float = field(
        default_factory=lambda: _env_float("LIVE_EXTREME_PRICE_MIN_EDGE_CENTS", 4.0)
    )

    # Reject opening a position whose worst-case loss per share dwarfs the
    # spread actually being captured -- a single wrong resolution could wipe
    # out many trades' worth of captured edge. Static threshold only, no
    # automatic per-family expectancy override yet (see live/RUNBOOK.md and
    # live-family-performance). See live/market_maker.py::_resolve_leg_price.
    max_payoff_loss_to_capture_ratio: float = field(
        default_factory=lambda: _env_float("LIVE_MAX_PAYOFF_LOSS_TO_CAPTURE_RATIO", 20.0)
    )
    # From flat, never post a single directional entry merely because the
    # opposite side failed a risk guard or the shared order budget ran out.
    # Both entry legs must qualify together; inventory-reducing operation is
    # unaffected once a fill creates a position.
    require_both_entry_legs: bool = field(
        default_factory=lambda: _env_bool("LIVE_REQUIRE_BOTH_ENTRY_LEGS", True)
    )

    # Once either side fills, stop quoting the inventory-increasing side
    # until the position is flat again. This makes a one-sided fill a
    # temporary liquidation task instead of the start of a directional bet.
    flat_first_inventory_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_FLAT_FIRST_INVENTORY_ENABLED", True)
    )

    # At/inside this known time-to-event window, inventory is force-flattened
    # with a marketable LIMIT at the visible opposing best price. Entry-only
    # depth/volatility/edge and cost-basis loss guards do not block it.
    hard_flatten_minutes_before_event: float = field(
        default_factory=lambda: _env_float("LIVE_HARD_FLATTEN_MINUTES_BEFORE_EVENT", 90.0)
    )
    hard_flatten_on_max_holding_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_HARD_FLATTEN_ON_MAX_HOLDING_ENABLED", True)
    )

    # Require wider edge as a market nears its own resolution/close time --
    # every fill gets more likely to be informed the closer you are to
    # settlement. Edge-only (no size multiplier), unlike toxicity/event-warn.
    near_resolution_hours_threshold: float = field(
        default_factory=lambda: _env_float("LIVE_NEAR_RESOLUTION_HOURS_THRESHOLD", 24.0)
    )
    near_resolution_min_edge_multiplier: float = field(
        default_factory=lambda: _env_float("LIVE_NEAR_RESOLUTION_MIN_EDGE_MULTIPLIER", 2.0)
    )

    # Reduce-only exit urgency: a reducing leg exits patiently (no
    # inventory-skew aggressive nudge, skipped entirely while a market is
    # in a toxicity cooldown) UNLESS it's urgent -- over the event-exposure
    # cap, near its own resolution, or the daily/session circuit breaker is
    # approaching its loss limit. This fraction is the "approaching" cutoff
    # for the breaker-risk trigger specifically (checked in ws_runner.py
    # against CircuitBreakerSettings.daily_loss_limit_usd /
    # SessionCircuitBreakerSettings.loss_limit_usd). See
    # live/RUNBOOK.md's most recent section -- prompted by a real reducing
    # exit that got picked off by a fast move it wasn't urgent enough to
    # need to chase.
    breaker_risk_warning_fraction: float = field(
        default_factory=lambda: _env_float("LIVE_BREAKER_RISK_WARNING_FRACTION", 0.75)
    )

    # Reducing orders use a bounded loss allowance instead of a permanent
    # historical-cost floor. The allowance ramps from zero to
    # liquidation_max_loss_cents over liquidation_max_holding_hours, while
    # liquidation_max_loss_usd caps the total voluntary loss on the whole
    # position. Urgent risk states may use the separate, still-bounded cap.
    liquidation_max_holding_hours: float = field(
        default_factory=lambda: _env_float("LIVE_LIQUIDATION_MAX_HOLDING_HOURS", 1.0)
    )
    liquidation_max_loss_cents: float = field(
        default_factory=lambda: _env_float("LIVE_LIQUIDATION_MAX_LOSS_CENTS", 5.0)
    )
    liquidation_urgent_max_loss_cents: float = field(
        default_factory=lambda: _env_float("LIVE_LIQUIDATION_URGENT_MAX_LOSS_CENTS", 10.0)
    )
    liquidation_max_loss_usd: float = field(
        default_factory=lambda: _env_float("LIVE_LIQUIDATION_MAX_LOSS_USD", 2.0)
    )

    # P&L attribution (live/lot_accounting.py + live/settlements.py +
    # live/pnl_attribution.py): position-snapshot capture and settlement
    # detection ("Strategy B") run every cycle alongside the fill-based
    # FIFO lot matching ("Strategy A") -- both kill-switches default True,
    # mirroring markout_tracking_enabled's existing pattern, in case the
    # extra per-cycle write/detection work ever needs disabling without a
    # code change. See live/RUNBOOK.md's most recent section.
    position_snapshot_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_POSITION_SNAPSHOT_ENABLED", True)
    )
    settlement_detection_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_SETTLEMENT_DETECTION_ENABLED", True)
    )
    # How far apart live/reconciliation.py::reconcile_realized_pnl allows
    # Strategy A (fill-based) and Strategy B (exchange realized-field
    # snapshot) to disagree before flagging it. Kept generous by default --
    # whether the exchange's own `realized` field is net or gross of
    # commission is unverified against real data yet.
    pnl_reconcile_tolerance_usd: float = field(
        default_factory=lambda: _env_float("LIVE_PNL_RECONCILE_TOLERANCE_USD", 0.05)
    )


@dataclass(frozen=True)
class CircuitBreakerSettings:
    enabled: bool = field(default_factory=lambda: _env_bool("CIRCUIT_BREAKER_ENABLED", True))
    daily_loss_limit_usd: float = field(
        default_factory=lambda: _env_float("CIRCUIT_BREAKER_DAILY_LOSS_LIMIT_USD", 20.0)
    )


@dataclass(frozen=True)
class SessionCircuitBreakerSettings:
    """Separate from CircuitBreakerSettings's daily (UTC-midnight-resetting)
    limit -- this one measures P/L since the current live-start process
    began, in-memory only (see live/circuit_breaker.py::SessionCircuitBreaker).
    Resets automatically on every restart; there is no reset CLI command for
    it, by design -- restarting live-start IS the reset."""
    enabled: bool = field(
        default_factory=lambda: _env_bool("SESSION_CIRCUIT_BREAKER_ENABLED", True)
    )
    loss_limit_usd: float = field(
        default_factory=lambda: _env_float("SESSION_CIRCUIT_BREAKER_LOSS_LIMIT_USD", 8.0)
    )


@dataclass(frozen=True)
class EquityProtectionSettings:
    enabled: bool = field(default_factory=lambda: _env_bool("EQUITY_PROTECTION_ENABLED", True))
    # Not derivable from any Polymarket balance field -- both buyingPower and
    # currentBalance are documented as unreliable for money math elsewhere in
    # this codebase (see live/ledger.py's module docstring). Left at 0.0, the
    # drawdown-from-peak check below is inactive (logged once); only the
    # same-day profit-lock sizing check is active regardless of this value.
    starting_capital_usd: float = field(
        default_factory=lambda: _env_float("EQUITY_PROTECTION_STARTING_CAPITAL_USD", 0.0)
    )
    # Stop trading and cancel all resting orders once account value
    # (starting_capital_usd + lifetime position P/L) falls this many percent
    # below its highest point ever seen (persisted across restarts).
    drawdown_from_peak_pct: float = field(
        default_factory=lambda: _env_float("EQUITY_PROTECTION_DRAWDOWN_PCT", 5.0)
    )
    # Same-day profit lock: once today's P/L crosses this, order size is
    # halved (see profit_lock_size_multiplier) for the rest of the UTC day,
    # even if P/L later dips back below this threshold (a ratchet).
    profit_lock_daily_usd: float = field(
        default_factory=lambda: _env_float("EQUITY_PROTECTION_PROFIT_LOCK_USD", 40.0)
    )
    profit_lock_size_multiplier: float = field(
        default_factory=lambda: _env_float("EQUITY_PROTECTION_SIZE_MULTIPLIER", 0.5)
    )


@dataclass(frozen=True)
class Settings:
    api: APISettings = field(default_factory=APISettings)
    filters: FilterSettings = field(default_factory=FilterSettings)
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    paper: PaperTradingSettings = field(default_factory=PaperTradingSettings)
    live: LiveTradingSettings = field(default_factory=LiveTradingSettings)
    circuit_breaker: CircuitBreakerSettings = field(default_factory=CircuitBreakerSettings)
    session_circuit_breaker: SessionCircuitBreakerSettings = field(
        default_factory=SessionCircuitBreakerSettings
    )
    equity_protection: EquityProtectionSettings = field(default_factory=EquityProtectionSettings)
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO"))
    # bot.log rotation (see logger.py) -- a confirmed real incident left a
    # crashed process's console output essentially undiagnosable; rotation
    # keeps the file bounded regardless of run length.
    log_max_bytes: int = field(default_factory=lambda: _env_int("LOG_MAX_BYTES", 20_000_000))
    log_backup_count: int = field(default_factory=lambda: _env_int("LOG_BACKUP_COUNT", 5))


def load_settings() -> Settings:
    return Settings()


def ensure_data_dirs() -> None:
    for directory in (
        RAW_DIR, PROCESSED_DIR, PAPER_TRADES_DIR, LIVE_TRADES_DIR, REPORTS_DIR, LOGS_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
