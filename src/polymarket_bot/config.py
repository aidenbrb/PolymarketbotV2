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
        default_factory=lambda: _env_int("LIVE_WEBSOCKET_CANDIDATE_REFRESH_SECONDS", 300)
    )
    websocket_subscription_limit: int = field(
        default_factory=lambda: _env_int("LIVE_WEBSOCKET_SUBSCRIPTION_LIMIT", 100)
    )
    # Scaffold only (see live/ws_private.py, live/RUNBOOK.md): streams
    # order/position/balance updates into an in-memory store for
    # visibility, but no trading decision reads from it yet. Escape hatch
    # in case it ever misbehaves -- doesn't affect trading logic either way.
    enable_private_websocket: bool = field(
        default_factory=lambda: _env_bool("LIVE_ENABLE_PRIVATE_WEBSOCKET", True)
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
    min_liquidity: float = field(
        default_factory=lambda: _env_float("LIVE_MIN_LIQUIDITY", 100.0)
    )
    min_volume_24h: float = field(
        default_factory=lambda: _env_float("LIVE_MIN_VOLUME_24H", 0.0)
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
            ("champion", "championship", "mvp", "cy young", "pennant"),
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
    refresh_interval_seconds: int = field(
        default_factory=lambda: _env_int("LIVE_REFRESH_INTERVAL_SECONDS", 60)
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
    # Left empty (no exclusions) by default pending that verification, since
    # sports markets are a large share of what's actually listed on .us.
    exclude_categories: tuple[str, ...] = ()
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
        default_factory=lambda: _env_float("LIVE_MAX_PAYOFF_LOSS_TO_CAPTURE_RATIO", 30.0)
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


@dataclass(frozen=True)
class CircuitBreakerSettings:
    enabled: bool = field(default_factory=lambda: _env_bool("CIRCUIT_BREAKER_ENABLED", True))
    daily_loss_limit_usd: float = field(
        default_factory=lambda: _env_float("CIRCUIT_BREAKER_DAILY_LOSS_LIMIT_USD", 20.0)
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
        default_factory=lambda: _env_float("EQUITY_PROTECTION_DRAWDOWN_PCT", 20.0)
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
    equity_protection: EquityProtectionSettings = field(default_factory=EquityProtectionSettings)
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO"))


def load_settings() -> Settings:
    return Settings()


def ensure_data_dirs() -> None:
    for directory in (
        RAW_DIR, PROCESSED_DIR, PAPER_TRADES_DIR, LIVE_TRADES_DIR, REPORTS_DIR, LOGS_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
