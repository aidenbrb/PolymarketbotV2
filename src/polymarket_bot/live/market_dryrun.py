"""Risk-free dry-run: runs the existing shadow-simulation engine
(MarketObservationTracker) against the real public market-data WebSocket
feed, with genuine one-share sizing and a fixed evidence target/deadline,
producing a PASS/FAIL/INSUFFICIENT verdict -- with no order-placement or
account-access capability reachable anywhere in this module.

Deliberately constructs only: WebSocketAuthSigner (public-WS auth headers
only -- never LiveUsClient), StreamingMarketDataStore/
LiveMarketWebSocketClient (public market data), PolymarketClient/
MarketScanner (read-only REST scanning), StreamingReadClient (read-only,
rate-limited REST book fallback), and MarketObservationTracker (the shadow
simulation engine already used this way by the real pilot commands). Never
imports live/us_client.LiveUsClient, live/multi_market_maker.py,
live/market_maker.py, or anything from live/ws_private.py -- see
tests/live/test_market_dryrun.py's structural-guarantee test.

Lifecycle: COLLECTING -> GRACE -> FINALIZING -> COMPLETE (see
MarketObservationTracker's "Dry-run lifecycle" methods, which persist the
phase and re-enforce the entries-frozen state on every restart). Evidence
target: 20 round trips, 5 distinct events, AND 20 matured entry markouts,
all together -- reaching only two of the three does not stop collection,
since a 5-minute markout needs 5 minutes to mature. Once met (or the
healthy-feed/wall-clock deadline arrives), entries freeze and a 5-minute
grace period lets any still-maturing markouts resolve before final
liquidation and verdict.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .. import config, storage
from ..logger import get_logger
from ..polymarket_client import PolymarketClient
from .credentials import ApiCredentials
from .instance_lock import InstanceLock
from .market_observation import (
    DRY_RUN_PHASE_COLLECTING,
    DRY_RUN_PHASE_COMPLETE,
    DRY_RUN_PHASE_FINALIZING,
    DRY_RUN_PHASE_GRACE,
    DRYRUN_OBSERVATION_FILE,
    OBSERVATION_PROFILES,
    MarketObservationTracker,
    PROFILE_JULY5_STYLE,
    classify_settlement_lookup,
)
from .market_selection import event_or_close_datetime, select_target_markets
from .ws_auth_signer import WebSocketAuthSigner
from .ws_market_data import LiveMarketWebSocketClient, StreamingMarketDataStore, StreamingReadClient

logger = get_logger("live.market_dryrun")

DRYRUN_LOCK_FILE = config.LIVE_TRADES_DIR / "dry_run.lock"


class DryRunPolicyMismatchError(RuntimeError):
    """The frozen policy in an existing dry-run archive doesn't match the
    policy this process would use -- refuses to resume rather than silently
    grading already-collected evidence against different thresholds."""


class DryRunFeedStalledError(RuntimeError):
    """The candidate universe stayed empty, or the market-data WebSocket
    produced no heartbeat/message even after recovery reconnects. Stopping
    rather than silently running toward the wall-clock deadline collecting
    no real evidence."""


@dataclasses.dataclass(frozen=True)
class DryRunPolicy:
    """Every threshold the dry-run's verdict is graded against, frozen and
    hash-checked against the archive at startup (see
    enforce_dry_run_policy_locked below) so a later config/code change can
    never retroactively alter an in-progress or already-completed run's
    verdict."""

    profile: str = PROFILE_JULY5_STYLE
    order_shares: float = 1.0
    min_round_trips: int = 20
    min_distinct_events: int = 5
    min_entry_markout_samples: int = 20
    min_profit_factor: float = 1.20
    min_avg_markout_5m_cents: float = 0.0
    max_drawdown_usd: float = -3.0
    max_event_profit_concentration: float = 0.50
    max_settlement_or_forced_exit_rate: float = 0.20
    grace_period_seconds: float = 300.0
    min_healthy_feed_hours: float = 48.0
    max_wall_clock_hours: float = 96.0
    # Bounded wait for FINALIZING to resolve every open shadow position to
    # flat (via a live book, or settlement for a genuinely resolved
    # market) before giving up and forcing INSUFFICIENT rather than
    # retrying forever against a position that permanently lacks a
    # two-sided book.
    max_finalizing_wait_seconds: float = 1800.0
    # A settlement status can't change faster than real-world event
    # resolution -- querying it on every FINALIZING cycle (which can run
    # as often as refresh_interval_seconds, seconds under a live-tuned
    # .env) would hammer the REST settlement/metadata endpoints for every
    # still-stuck slug on every tick.
    settlement_lookup_interval_seconds: float = 60.0

    def policy_hash(self) -> str:
        payload = json.dumps(dataclasses.asdict(self), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


def enforce_dry_run_policy(tracker: MarketObservationTracker, policy: DryRunPolicy) -> None:
    """First run against a fresh archive: freezes the policy hash in.
    Every later run: fails closed on any mismatch rather than grading
    existing evidence against different thresholds."""
    with tracker._lock:
        existing = tracker._state.get("dry_run_policy_hash")
        current = policy.policy_hash()
        if existing is None:
            tracker._state["dry_run_policy_hash"] = current
            tracker._maybe_persist_locked(time.time(), force=True)
            return
        if existing != current:
            raise DryRunPolicyMismatchError(
                "The dry-run archive was started with a different policy "
                f"(hash {existing}) than this process would use (hash "
                f"{current}). Refusing to resume and silently re-grade "
                "already-collected evidence -- start a fresh archive/path "
                "if the policy change was intentional."
            )


def dry_run_evidence_target_met(summary: dict[str, Any], policy: DryRunPolicy) -> bool:
    """All three sample floors together -- not just round trips/events,
    which could be satisfied before entry markouts have had 5 minutes to
    mature."""
    return (
        int(summary.get("completed_round_trips") or 0) >= policy.min_round_trips
        and int(summary.get("distinct_event_count") or 0) >= policy.min_distinct_events
        and int(summary.get("entry_markout_5m_sample_count") or 0)
        >= policy.min_entry_markout_samples
    )


def dry_run_deadline_reached(
    summary: dict[str, Any], policy: DryRunPolicy, wall_clock_elapsed_hours: float,
) -> bool:
    healthy_feed_hours = float(summary.get("healthy_feed_hours") or 0.0)
    return (
        healthy_feed_hours >= policy.min_healthy_feed_hours
        or wall_clock_elapsed_hours >= policy.max_wall_clock_hours
    )


def equity_curve_has_incomplete_valuation(
    tracker: MarketObservationTracker, profile: str,
) -> bool:
    """True if ANY point in this profile's equity curve was ever recorded
    with valuation_incomplete=True -- e.g. a WS book gap left an open
    position unpriced for a bucket. _maximum_drawdown_from_curve() SKIPS
    such points rather than including them, so a real dip during one is
    invisible to maximum_drawdown_usd; compute_dry_run_verdict() uses this
    to force INSUFFICIENT instead of risking a falsely optimistic
    PASS/FAIL over an unknowable portion of the run's drawdown history."""
    with tracker._lock:
        curve = (
            tracker._state.get("profiles", {}).get(profile, {}).get("equity_curve", [])
        )
        return any(
            isinstance(point, dict) and point.get("valuation_incomplete")
            for point in curve
        )


def compute_dry_run_verdict(
    summary: dict[str, Any], policy: DryRunPolicy, any_valuation_incomplete: bool,
) -> dict[str, Any]:
    """Pure. Must only be called with a summary taken AFTER
    finalize_dry_run_evaluation() has confirmed zero open inventory
    (result["complete"] is True) -- by construction this means the FINAL
    total_pnl_usd has no unpriced open-position component. That alone does
    NOT make maximum_drawdown_usd trustworthy, though: it's computed over
    the ENTIRE historical equity curve, and _maximum_drawdown_from_curve()
    silently SKIPS any point with valuation_incomplete=True rather than
    including it -- a real dip that happened while a position was
    temporarily unpriced (e.g. a WS book gap mid-run) would be invisible
    to that number, understating the true worst-case drawdown. Callers
    must scan `profiles[profile]["equity_curve"]` themselves and pass
    whether ANY point was ever incomplete; this floor check forces
    INSUFFICIENT rather than risking a falsely optimistic PASS/FAIL."""
    round_trips = int(summary.get("completed_round_trips") or 0)
    distinct_events = int(summary.get("distinct_event_count") or 0)
    entry_markout_samples = int(summary.get("entry_markout_5m_sample_count") or 0)
    open_inventory_count = len(summary.get("open_inventory") or [])

    floor_checks = {
        "round_trips": round_trips >= policy.min_round_trips,
        "distinct_events": distinct_events >= policy.min_distinct_events,
        "entry_markout_samples": entry_markout_samples >= policy.min_entry_markout_samples,
        "zero_open_inventory": open_inventory_count == 0,
        "no_incomplete_valuation": not any_valuation_incomplete,
    }
    detail: dict[str, Any] = {
        "round_trips": round_trips,
        "distinct_events": distinct_events,
        "entry_markout_samples": entry_markout_samples,
        "open_inventory_count": open_inventory_count,
        "any_valuation_incomplete": any_valuation_incomplete,
        "floor_checks": floor_checks,
    }
    if not all(floor_checks.values()):
        return {"verdict": "INSUFFICIENT", "detail": detail}

    total_pnl = summary.get("total_pnl_usd")
    profit_factor = summary.get("profit_factor")
    avg_markout = summary.get("avg_markout_5m_cents")
    drawdown = summary.get("maximum_drawdown_usd")
    concentration = summary.get("event_profit_concentration")
    exit_rate = (
        (int(summary.get("forced_exit_count") or 0) + int(summary.get("settlement_exit_count") or 0))
        / round_trips
        if round_trips > 0 else 0.0
    )
    pass_checks = {
        "positive_total_pnl": total_pnl is not None and total_pnl > 0,
        "profit_factor": profit_factor is not None and profit_factor >= policy.min_profit_factor,
        "avg_markout_nonnegative": (
            avg_markout is not None and avg_markout >= policy.min_avg_markout_5m_cents
        ),
        "event_concentration": (
            concentration is not None and concentration <= policy.max_event_profit_concentration
        ),
        "settlement_or_forced_exit_rate": exit_rate <= policy.max_settlement_or_forced_exit_rate,
        "drawdown": drawdown is not None and drawdown >= policy.max_drawdown_usd,
    }
    detail.update({
        "total_pnl_usd": total_pnl,
        "profit_factor": profit_factor,
        "avg_markout_5m_cents": avg_markout,
        "maximum_drawdown_usd": drawdown,
        "event_profit_concentration": concentration,
        "settlement_or_forced_exit_rate": exit_rate,
        "pass_checks": pass_checks,
    })
    return {"verdict": "PASS" if all(pass_checks.values()) else "FAIL", "detail": detail}


def dry_run_settings(
    base_settings: config.LiveTradingSettings, policy: DryRunPolicy,
) -> config.LiveTradingSettings:
    """Genuine one-share simulation via the same order-sizing setting the
    real july5 profile uses -- not post-hoc normalization of a
    larger-sized fill -- plus a matching evaluation-hours cap so
    profile_summary()'s own healthy_feed_hours tracks policy.
    min_healthy_feed_hours directly."""
    return dataclasses.replace(
        base_settings,
        observation_july5_order_shares=policy.order_shares,
        observation_evaluation_hours=policy.min_healthy_feed_hours,
    )


def dry_run_status(path: Optional[Path] = None) -> dict[str, Any]:
    """Genuinely read-only: never constructs a MarketObservationTracker
    (whose __init__ has real side effects -- archives aside an
    incompatible schema file, resets state). Reads the exact same raw
    storage primitive MarketObservationTracker itself uses internally, and
    displays only the one canonical snapshot the running process persists
    -- never recomputes verdict logic itself, so it structurally cannot
    show a premature PASS/FAIL before grace and finalization complete."""
    state = storage.load_json(path or DRYRUN_OBSERVATION_FILE, default={})
    if not isinstance(state, dict) or not state:
        return {"phase": None, "verdict": "NOT_STARTED"}
    snapshot = state.get("dry_run_snapshot")
    if not isinstance(snapshot, dict):
        return {"phase": state.get("dry_run_phase", DRY_RUN_PHASE_COLLECTING), "verdict": "PROVISIONAL"}
    return snapshot


class DryRunRunner:
    """Orchestrates one dry-run session. Composes only read-only/public-WS
    capable objects -- see the module docstring's structural guarantee."""

    def __init__(
        self,
        credentials: ApiCredentials,
        settings: config.LiveTradingSettings,
        policy: Optional[DryRunPolicy] = None,
        tracker: Optional[MarketObservationTracker] = None,
        store: Optional[StreamingMarketDataStore] = None,
        market_ws: Optional[LiveMarketWebSocketClient] = None,
        read_client: Optional[StreamingReadClient] = None,
        settlement_client: Optional[PolymarketClient] = None,
        path: Optional[Path] = None,
    ):
        self.policy = policy or DryRunPolicy()
        self.settings = dry_run_settings(settings, self.policy)
        self.path = path or DRYRUN_OBSERVATION_FILE
        self.tracker = tracker or MarketObservationTracker(self.settings, path=self.path)
        enforce_dry_run_policy(self.tracker, self.policy)
        # Every OTHER profile (legacy/controlled) shares the same tracker
        # instance and would otherwise rank/quote/accumulate its own
        # shadow inventory off the identical broad WS feed, independent
        # of policy.profile -- open_inventory_slugs()/
        # finalize_dry_run_evaluation() operate across ALL profiles, so
        # unrelated inventory could delay FINALIZING or eat into the
        # bounded wait for a position this dry-run was never grading.
        # Pinning them to an empty allocation (the same mechanism that
        # would otherwise pin policy.profile itself) keeps them
        # permanently inert -- _refresh_allocations() re-applies this pin
        # every cycle for as long as this process runs, and _pinned_
        # allocation is in-memory only (never persisted), so a restart
        # re-establishes it here in __init__ before any WS traffic can
        # possibly reach record_book()/record_trade().
        for other_profile in OBSERVATION_PROFILES:
            if other_profile != self.policy.profile:
                self.tracker.override_profile_allocation(other_profile, [])
        self._signer = WebSocketAuthSigner(credentials)
        self.store = store or StreamingMarketDataStore(
            self.settings.websocket_stale_after_seconds,
            activity_window_seconds=self.settings.activity_window_seconds,
            observation_tracker=self.tracker,
        )
        self.market_ws = market_ws or LiveMarketWebSocketClient(
            settings=self.settings,
            signed_headers=self._signer.websocket_headers,
            store=self.store,
        )
        self.read_client = read_client or StreamingReadClient(self.store)
        # Read-only REST lookups only (classify_settlement_lookup) -- used
        # to resolve a shadow position on a market that has genuinely
        # settled and will never produce a live book again, so FINALIZING
        # doesn't wait out its full bounded timeout for every such market.
        self.settlement_client = settlement_client or PolymarketClient()
        self._stop_event_set = False
        self._ws_thread = None
        self._last_candidate_refresh_attempt = 0.0
        self._last_candidate_refresh_failed = False
        self._last_settlement_attempt_epoch = 0.0
        self._feed_recovery_attempts = 0
        self._feed_recovery_started_epoch: Optional[float] = None
        self._feed_activity_before_recovery = 0.0

    def _candidate_refresh_due(self, now: float) -> bool:
        """Same due-check ws_runner.py's _maybe_refresh_candidates() uses:
        the normal ~900s cadence, or a shorter ~60s retry after a failed
        scan -- never every run_one_cycle() tick (which can be as short as
        10s for live quoting's own refresh_interval_seconds)."""
        min_gap = (
            self.settings.websocket_candidate_refresh_retry_seconds
            if self._last_candidate_refresh_failed
            else self.settings.websocket_candidate_refresh_seconds
        )
        return now - self._last_candidate_refresh_attempt >= min_gap

    def _refresh_candidates(self, now: float) -> None:
        if not self._candidate_refresh_due(now):
            return
        self._last_candidate_refresh_attempt = now
        observation_markets: list = []
        try:
            select_target_markets(
                settings=self.settings, observation_markets_out=observation_markets,
            )
        except Exception as exc:  # noqa: BLE001 -- a scan failure must not crash the dry-run
            logger.warning("Dry-run candidate scan failed this cycle: %s", exc)
            self._last_candidate_refresh_failed = True
            return
        self._last_candidate_refresh_failed = False

        # observation_markets_out is the BROAD, non-trading observation
        # universe (ignores spread/liquidity/paired-entry/cutoff filters --
        # see is_observation_eligible) -- using select_target_markets' own
        # RETURN value here would silently restrict the dry-run to
        # whatever live quoting would trade, which is the wrong universe
        # for evidence collection.
        capacity = max(
            1,
            min(self.settings.websocket_subscription_limit, self.settings.observation_universe_size),
        )
        inventory_slugs = sorted(self.tracker.open_inventory_slugs())
        by_slug = {
            item.market.market_id: item
            for item in observation_markets if item.market is not None
        }
        broad_slugs = [slug for slug in by_slug if slug not in inventory_slugs]
        # Inventory owns subscription priority -- an event aging out of the
        # broad universe must never orphan an already-open shadow exit.
        slugs = list(dict.fromkeys([*inventory_slugs, *broad_slugs]))[:capacity]

        for slug in slugs:
            item = by_slug.get(slug)
            if item is None or item.market is None:
                # An inventory slug that fell out of this cycle's broad
                # scan keeps whatever metadata it was registered with
                # earlier -- it must already have real metadata from the
                # cycle that first made it a shadow candidate.
                continue
            raw = item.market.raw or {}
            try:
                tick_size = float(raw.get("orderPriceMinTickSize") or 0.01)
            except (TypeError, ValueError):
                tick_size = 0.01
            event_dt = event_or_close_datetime(item.market)
            self.tracker.register_market(
                slug,
                tick_size=tick_size,
                question=item.question,
                event_id=str(item.market.event_id or ""),
                event_or_close_epoch=(
                    event_dt.timestamp() if event_dt is not None else None
                ),
                raw=raw,
            )

        # Deliberately NOT override_profile_allocation() for policy.profile:
        # that pins the active set to exactly this caller-ordered list,
        # bypassing _refresh_allocations()'s own per-profile ranking
        # entirely. It exists to keep a real pilot's shadow comparison
        # matching whatever the real bot is actually quoting -- there is
        # no real bot here, so the correct thing is to feed the broad pool
        # via set_live_candidate_slugs() and let policy.profile rank it
        # itself (widest-spread first, for july5_style) exactly like a
        # real, unpinned run would. Using the pin here would silently
        # replace "widest spread" with "most recent/deepest scan order,"
        # the wrong universe for what this profile is meant to measure.
        self.tracker.set_live_candidate_slugs(slugs)
        self.market_ws.set_market_slugs(slugs)

    def _abort_if_feed_stalled(self, now: float) -> None:
        """Mirrors ws_runner.py's _abort_if_observation_feed_stalled(): an
        empty candidate universe, or a market-data WebSocket producing no
        heartbeat/message, must not let the dry-run keep running toward
        its wall-clock deadline collecting no real evidence. Up to 2
        reconnect attempts before failing closed."""
        health = self.tracker.feed_health(now)
        if health.get("reason") == "empty_candidate_pool":
            if health["stalled"]:
                raise DryRunFeedStalledError(
                    "Dry-run candidate universe remained empty for "
                    f"{float(health['age_seconds']):.0f}s. Stopping instead of "
                    "silently running without any markets to observe."
                )
            return
        latest_activity = float(health.get("latest_activity_epoch") or 0.0)
        if self._feed_recovery_started_epoch is not None:
            if latest_activity > self._feed_activity_before_recovery:
                logger.warning("Dry-run market WebSocket feed recovered after watchdog reconnect.")
                self._feed_recovery_attempts = 0
                self._feed_recovery_started_epoch = None
                self._feed_activity_before_recovery = 0.0
            else:
                grace = max(1.0, self.settings.observation_feed_stale_after_seconds)
                if now - self._feed_recovery_started_epoch <= grace:
                    return
                self._feed_recovery_started_epoch = None
        if not health["stalled"]:
            return
        if self._feed_recovery_attempts < 2:
            self._feed_recovery_attempts += 1
            self._feed_recovery_started_epoch = now
            self._feed_activity_before_recovery = latest_activity
            logger.warning(
                "Dry-run market WebSocket produced no heartbeat or market "
                "message for %.0fs; requesting recovery reconnect %d/2 "
                "before failing closed.",
                float(health["age_seconds"]), self._feed_recovery_attempts,
            )
            self.market_ws.force_reconnect()
            return
        raise DryRunFeedStalledError(
            "Dry-run market-data feed produced no heartbeat or market message "
            f"for {float(health['age_seconds']):.0f}s after "
            f"{self._feed_recovery_attempts} reconnect attempts."
        )

    def _compute_snapshot(self, now: float) -> dict[str, Any]:
        summary = self.tracker.profile_summary(self.policy.profile)
        started = float(self.tracker._state.get("started_at_epoch") or now)
        wall_clock_elapsed_hours = max(0.0, (now - started) / 3600)
        phase = self.tracker.dry_run_phase()
        snapshot: dict[str, Any] = {
            "phase": phase,
            "computed_at_epoch": now,
            "round_trips": summary.get("completed_round_trips"),
            "distinct_events": summary.get("distinct_event_count"),
            "entry_markout_samples": summary.get("entry_markout_5m_sample_count"),
            "healthy_feed_hours_elapsed": summary.get("healthy_feed_hours"),
            "wall_clock_hours_elapsed": wall_clock_elapsed_hours,
        }
        if phase != DRY_RUN_PHASE_COMPLETE:
            snapshot["verdict"] = "PROVISIONAL"
            return snapshot
        verdict = self.tracker._state.get("dry_run_verdict") or {"verdict": "INSUFFICIENT", "detail": {}}
        snapshot.update(verdict)
        return snapshot

    def run_one_cycle(self) -> None:
        now = time.time()
        self._abort_if_feed_stalled(now)
        phase = self.tracker.dry_run_phase()

        if phase == DRY_RUN_PHASE_COLLECTING:
            self._refresh_candidates(now)
            summary = self.tracker.profile_summary(self.policy.profile)
            started = float(self.tracker._state.get("started_at_epoch") or now)
            wall_clock_elapsed_hours = max(0.0, (now - started) / 3600)
            if dry_run_evidence_target_met(summary, self.policy) or dry_run_deadline_reached(
                summary, self.policy, wall_clock_elapsed_hours,
            ):
                self.tracker.advance_dry_run_to_grace(now, self.policy.grace_period_seconds)
        elif phase == DRY_RUN_PHASE_GRACE:
            deadline = self.tracker.dry_run_grace_deadline_epoch()
            if deadline is not None and now >= deadline:
                self.tracker.advance_dry_run_to_finalizing(now)
        elif phase == DRY_RUN_PHASE_FINALIZING:
            # A single attempt per cycle, no in-process retry sleep: the
            # outer run_one_cycle()/refresh_interval_seconds loop already
            # provides the retry cadence, so blocking here too would only
            # add redundant latency without changing the outcome.
            result = self.tracker.finalize_dry_run_evaluation(
                self.read_client.get_market_book,
                max_book_attempts=1, retry_seconds=0.0,
            )
            complete = bool(result.get("complete"))
            if not complete and not result.get("already_finalized"):
                # A live book that never returns a two-sided quote (e.g.
                # the market has actually resolved) would otherwise make
                # finalize_dry_run_evaluation() retry forever -- attempt
                # settlement for whatever's still stuck before falling
                # back to the bounded-wait timeout below.
                self._attempt_settlement_for_stuck_slugs(now)
                complete = not self.tracker.open_inventory_slugs()
            finalizing_started = self.tracker.dry_run_finalizing_started_epoch()
            finalizing_elapsed = (
                now - finalizing_started if finalizing_started is not None else 0.0
            )
            timed_out = finalizing_elapsed >= self.policy.max_finalizing_wait_seconds
            if complete or timed_out:
                summary = self.tracker.profile_summary(self.policy.profile)
                any_incomplete = equity_curve_has_incomplete_valuation(
                    self.tracker, self.policy.profile,
                )
                verdict = compute_dry_run_verdict(summary, self.policy, any_incomplete)
                self.tracker.complete_dry_run(verdict, now)

        self.tracker.record_dry_run_snapshot(self._compute_snapshot(now), now)

    def _attempt_settlement_for_stuck_slugs(self, now: float) -> None:
        stuck_slugs = sorted(self.tracker.open_inventory_slugs())
        if not stuck_slugs:
            return
        # Settlement status can't change faster than real-world event
        # resolution -- re-querying every still-stuck slug on every
        # FINALIZING cycle (which can run every refresh_interval_seconds,
        # as short as a few seconds under a live-tuned .env) would hammer
        # the REST settlement/metadata endpoints for no benefit.
        if now - self._last_settlement_attempt_epoch < self.policy.settlement_lookup_interval_seconds:
            return
        self._last_settlement_attempt_epoch = now
        lookup_results = []
        for slug in stuck_slugs:
            try:
                lookup_results.append(
                    classify_settlement_lookup(self.settlement_client, slug)
                )
            except Exception as exc:  # noqa: BLE001 -- a lookup failure must not crash the dry-run
                logger.warning(
                    "Dry-run settlement lookup failed for %s: %s", slug, exc,
                )
        if not lookup_results:
            return
        self.tracker.apply_settlement_batch(lookup_results, now)
        self.tracker.flush()

    def _start_ws_thread(self) -> None:
        self._ws_thread = threading.Thread(
            target=self.market_ws.run_forever, name="polymarket-dryrun-ws", daemon=True,
        )
        self._ws_thread.start()

    def run_forever(self) -> None:
        """Does NOT acquire the archive lock -- see run_dry_run() below,
        the only correct entry point for a real process. Tracker
        construction and policy enforcement already happened in __init__,
        by which point the lock must already be held."""
        self._start_ws_thread()
        logger.warning(
            "DRY-RUN STARTED: profile=%s order_shares=%.2f -- no real order or "
            "account capability exists in this process.",
            self.policy.profile, self.policy.order_shares,
        )
        try:
            while self.tracker.dry_run_phase() != DRY_RUN_PHASE_COMPLETE:
                self.run_one_cycle()
                time.sleep(self.settings.refresh_interval_seconds)
        finally:
            self.market_ws.stop()
        logger.warning(
            "DRY-RUN COMPLETE: %s", self.tracker._state.get("dry_run_verdict"),
        )


def run_dry_run(
    credentials: ApiCredentials,
    settings: config.LiveTradingSettings,
    policy: Optional[DryRunPolicy] = None,
) -> None:
    """The only correct way to start a real dry-run process. Acquires the
    exclusive archive lock BEFORE constructing DryRunRunner -- its
    __init__ reads/writes the archive (tracker construction) and writes
    the frozen policy hash (enforce_dry_run_policy), so acquiring the lock
    only inside run_forever() (as an earlier version did) left a window
    where two concurrent startups could each mutate the archive before
    either one reached the lock meant to prevent exactly that."""
    with InstanceLock(lock_path=DRYRUN_LOCK_FILE):
        runner = DryRunRunner(credentials=credentials, settings=settings, policy=policy)
        runner.run_forever()
