# Live Trading Runbook (Polymarket US)

This is the manual checklist for going live on **Polymarket US** (a separate,
CFTC-regulated, USD-settled exchange operated by QCX LLC — not the
international polymarket.com). Everything here either requires your funded
real account or genuinely cannot be verified without one — that's exactly
why it's a checklist instead of code. Work through it in order, at small
size, before trusting the full $50–100/side sizing.

**I (the assistant) will walk through this with you rather than run it
myself.** I will never ask you to paste your API secret key into chat — add
it directly to your own local `.env` file yourself.

## -14. Risk-tightening batch: refuse bad-shaped bets, don't scale (2026-07-06)

**Why:** a review of real trading history and the bot's own settings found
seven concrete gaps where the bot still takes on informed-flow risk it can't
price -- quoting in-play stat props for up to 6 hours after the underlying
event starts, no extra caution near lopsided-payoff price extremes, no
screen against a single wrong resolution wiping out many trades' worth of
captured spread, and no visibility into which market families are actually
profitable. The framing: **don't scale, tighten first** -- profitability
comes more from refusing bad-shaped bets than from quoting more markets.

**Cross-cutting fix, required by two of the items below:** `raw_by_slug` in
`multi_market_maker.py::refresh_quotes` used to cover only ranked candidates
-- a held position whose market fell out of candidacy (an "orphaned
position") had literally no `gameStartTime`/`endDate`/`marketType` available
anywhere, blocking any time-based check for exactly the positions where it
matters most. Fix: `market_selection.py::select_target_markets` gained an
optional `raw_by_slug_out` out-parameter, populated from the full
pre-eligibility-filter scan (already happening every cycle, zero extra API
cost) rather than just the final ranked list. This had to be threaded
through **both** real call sites, not just `multi_market_maker.py`: the
default runner is `ws_runner.py`'s `WebSocketLiveTradingBot`, whose
`_refresh_candidates()` calls `select_target_markets()` directly and caches
the result (`self._extra_raw_by_slug`) -- `multi_market_maker.py`'s own
internal `candidates is None` fallback is dead code in that path, since
`_run_one_cycle` always passes an explicit candidates list. Only the unused
REST-polling fallback (`runner.py`) ever hit that branch. Any check using
this data fails open (treats "cannot evaluate" as "don't block") when raw
data still isn't available -- e.g. a fully delisted market -- matching this
codebase's established degrade-safely convention. Also renamed
`market_selection.py::_hours_to_event_or_close` -> `hours_to_event_or_close`
(dropped only the leading underscore, same minimal-rename precedent as
`ledger.py::_sum_position_pnl` -> `sum_position_pnl`) so
`multi_market_maker.py` can reuse it directly.

**1. Stop opening new exposure once an event has started.**
`LIVE_MAX_STARTED_EVENT_HOURS` default `6.0` -> `0.0` -- `is_eligible()`'s
existing hard cutoff now fully excludes any started-event market from new
candidacy with no grace period. For an *existing* position whose event has
since started (an orphaned position), a new `"event already started"` reason
was added to `multi_market_maker.py::_event_and_toxicity_gating` -- the same
shared function that already produces `reduce_only_reason` for toxicity
cooldown and event-exposure over-cap, so no changes to `market_maker.py`
were needed at all; it already fully blocks the increasing leg (both legs,
if flat) whenever `reduce_only_reason` is non-empty.

**2. "No ugly payoff" filters.** New settings
`LIVE_EXTREME_PRICE_LOW_THRESHOLD` (0.15), `LIVE_EXTREME_PRICE_HIGH_THRESHOLD`
(0.85), `LIVE_EXTREME_PRICE_MIN_EDGE_CENTS` (4.0). Enforced in
`market_maker.py::_resolve_leg_price`, which already had both the resolved
leg price and the increasing/reducing determination in scope -- it just
needed the overall captured-spread value threaded in as a new parameter
(computed once per cycle from the already-built `QuoteSides`). Only the
increasing leg is ever affected; reducing legs return early, before this
check is reached.

**3. Stricter reduce-only -- already true, confirmed, not new work.**
`_resolve_leg_price` already fully blocks the increasing leg whenever
`reduce_only_reason` is set, for both toxicity cooldown and event-exposure
over-cap. Item 1's new reason just joins the same mechanism.

**4. Lower event caps.** `LIVE_MAX_EVENT_EXPOSURE_PCT` 0.20 -> 0.15,
`LIVE_WARN_EVENT_EXPOSURE_PCT` 0.15 -> 0.10, `LIVE_STAT_PROP_MAX_EVENT_EXPOSURE_PCT`
0.15 -> 0.10. No code changes -- the existing gating already reads these
fields directly. **Known, intentional consequence:** warn (0.10) and the
stat-prop cap (0.10) are now equal, so stat-prop markets specifically skip
the soft warn-tier multiplier entirely and go straight from "under cap" to
"fully reduce-only" -- deliberate, since stat props are the highest-risk
family this whole batch targets.

**5. Expected-loss / payoff-ratio screening.** New setting
`LIVE_MAX_PAYOFF_LOSS_TO_CAPTURE_RATIO` (30.0). Enforced in
`_resolve_leg_price`, deliberately **without** folding in
`_fill_confidence` (market_selection.py's volume/liquidity proxy, floored
for low-volume markets) -- doing so would make the ratio swing on liquidity
rather than price/payoff economics and hard-block exactly the low-volume
candidates that floor was designed to protect. `max_loss_per_share_cents`
is `price` for a BUY-opening leg (worst case: YES resolves to 0) or
`(1 - price)` for a SELL-opening leg (worst case: YES resolves to 1),
compared against the already-available captured spread. Static threshold
only, per an explicit scoping decision -- no automatic per-family
expectancy override yet; that's real future work once item 6's reporting
has accumulated enough data to calibrate against safely. The 30x default is
a starting point (the existing at-the-money test fixture lands at ~24.5x)
-- watch real behavior via `live-family-performance` and retune.

**6. Family performance reporting (`live-family-performance`,
markout-based, per an explicit scoping decision -- not realized P&L).**
fills.json has no per-fill realized P&L field, and attributing one would
need new FIFO/weighted-average cost-lot tracking that doesn't exist
anywhere in this codebase; `markout_1m_cents`/`markout_5m_cents` (already
the bot's own trusted adverse-selection proxy, per toxicity_tracker.py) is
the signal used instead. New pure module `live/family_performance.py`:
`classify_family(slug)` is a coarse, best-effort heuristic (ordered
hyphen-token keyword matches -- corners, home-run props, first-half/
first-team-to-score props, first-5-innings, time-to-goal, weather, award/
championship/qualifier/winner futures -- falling back to the slug's
market-type-code prefix, or `"other"`), independently checked against every
real slug in the current `fills.json`/`orders.json` (144/117 records).
`compute_family_performance(fills)` aggregates fill count, avg/median 1m/5m
markout, and `fill_quality` counts per family. New CLI command
`live-family-performance` (zero-arg, no credentials, reads only
`fills.json`) follows the `live-fills` command's exact template; new
`dashboard.py::render_family_performance`, sorted worst-average-1m-markout
first. Run against the real account's data: already surfaces a real
signal (the `qualifier` family showing a negative average 5-minute markout)
worth further attention.

**7. Penalize resolution-soon markets.** New settings
`LIVE_NEAR_RESOLUTION_HOURS_THRESHOLD` (24.0),
`LIVE_NEAR_RESOLUTION_MIN_EDGE_MULTIPLIER` (2.0) -- edge-only, deliberately
no size multiplier (unlike toxicity/event-warn's edge+size pair).
`multi_market_maker.py::_effective_settings_for` gained a `near_resolution`
parameter, composing multiplicatively with the existing toxicity/event-warn
multipliers. Bounded at `>= 0` hours remaining deliberately: once a market
has already started (negative hours), item 1's reduce-only already applies,
and widening edge further at that point would also needlessly suppress the
*reducing* leg, which `reduce_only_reason` never touches. Relies on the
same cross-cutting `raw_by_slug`/`extra_raw_by_slug` fix for orphaned-
position coverage.

**Verification:** 466 tests passing (was 429 before this batch), including
new coverage for the `raw_by_slug_out` cross-cutting fix, event-started
reduce-only (fail-open, true/false cases, an end-to-end orphaned-position
test using `extra_raw_by_slug`), the near-resolution multiplier (fail-open,
in/out of threshold, suppressed once already-started), extreme-price and
payoff-ratio blocking/allowing (including proof that reducing legs are
never touched by either), and the full `family_performance.py` module.
`compileall` clean. Manually ran `live-family-performance` against the
real account's `fills.json` -- output shown above.

## -13. Futures markets bypassed the days-to-close cap via gameStartTime (2026-07-06)

**Why:** asked why the bot had quoted `tec-mlb-alwest-2026-09-27-w-tex` (an
AL West division-winner futures market) 18 times despite
`LIVE_MAX_DAYS_TO_CLOSE=3`. Pulling the real raw record for that slug showed
`endDate: 2026-10-11T23:00:00Z` (~97 days out, the market's actual
resolution) but `gameStartTime: 2026-07-08T04:20:00Z` (next calendar day) --
Polymarket populates `gameStartTime` on a season-long futures market with
the underlying team's *next scheduled game*, not the market's own
settlement date. `_days_to_event_or_close()` (`market_selection.py`) checked
`gameStartTime` before `endDate`, so a futures market always looked like it
closed tomorrow, permanently bypassing the days-to-close cap meant to keep
the bot on near-term markets. There's no trading advantage to holding a
market for months anyway: MM profit comes from recycling capital across
many spread captures, and a slow-moving, team-performance-driven price is
exactly the informed-flow risk a market maker wants to avoid -- consistent
with `LIVE_EXCLUDE_QUESTION_KEYWORDS` already excluding related futures
questions (champion/mvp/pennant); "division winner" just wasn't on that list.

**Fix:** `_days_to_event_or_close()` now checks `marketType`/`sportsMarketType`
(via a new small `_market_type()` helper, also reused by `is_eligible()`'s
existing exclude-market-types check) and uses `endDate` directly, skipping
`gameStartTime` entirely, whenever `marketType == "futures"`. Single-game
markets (moneyline, stat props) are unaffected -- `gameStartTime` is a real,
accurate signal for those. This also fixes the ranking tiebreaker sort in
`select_target_markets()`, which uses the same function.

**Verification:** new regression test
`test_is_eligible_rejects_long_dated_futures_market_despite_near_game_start_time`
reproduces the real scenario (futures market, `gameStartTime` tomorrow,
`endDate` 97 days out) and asserts rejection. Full suite: 429 passed (was
428). `compileall` clean.

## -12. Event-level correlation-aware capital allocation (2026-07-06)

**Why:** reviewing real trading history found that a bot that looks
diversified by position COUNT can be dangerously concentrated by actual
capital: 9 of 13 open positions (52%, $43.21 of $82.30) were different
threshold-slice corner-count props on ONE real-world soccer match
(Portugal-Spain), 2 more (29%) on a second match (USA-Belgium) -- 81% of
capital on two correlated events.

**Critical finding before any code was written:** the obvious approach --
bucket by `market.raw["eventSlug"]`/`["eventId"]` -- doesn't work. A dump of
the real, current 5000-record market-listing snapshot confirmed no key
containing "event" exists anywhere in that API's shape today
(`market_scanner.py`'s `Market.event_id=raw.get("eventId")` is dead code
against the real API, always `None`). Real `eventSlug` values DO exist, but
only in the *private* execution WebSocket schema
(`execution["order"]["marketMetadata"]["eventSlug"]`, see "-9." section),
fetched only *after* a fill -- never at candidate-selection time.

**The grouping signal exists anyway, encoded in the slug.** New
`live/event_exposure.py::derive_event_bucket_key(market_slug, raw=None)`:
tries `raw["eventSlug"]`/`raw["eventId"]` first (forward-compat only, always
absent today), then falls to a heuristic -- strip the first hyphen-token (a
short market-type code: `astatc`/`atc`/`aqc`/`tec`/`tsc`/...), then take
everything through the first `YYYY-MM-DD` date triplet found. Independently
re-verified against every real slug this bot has actually quoted (94 from
`fills.json`/`orders.json`): correct in every case, and an exact
character-for-character match to real `eventSlug` ground-truth (available
in `fills.json`'s private-execution data) in 3 of 4 comparable cases.
**Known, safe coarsening:** single-team-name slug families with no opponent
token (e.g. tournament-advancement props naming only one team) collapse to
one shared tournament-stage bucket even when plausibly different physical
fixtures -- over-groups (more conservative capping), never under-groups (a
missed real correlation).

**New settings** (`config.py::LiveTradingSettings`, all fraction semantics
`0.20`=20%, NOT percent-number like `EquityProtectionSettings
.drawdown_from_peak_pct`'s `20.0` -- deliberate, don't "fix" this into
percent-number form, it would change cap math by 100x):
`LIVE_MAX_EVENT_EXPOSURE_PCT` (0.20), `LIVE_WARN_EVENT_EXPOSURE_PCT` (0.15),
`LIVE_MAX_MARKETS_PER_EVENT` (2), `LIVE_STAT_PROP_MAX_EVENT_EXPOSURE_PCT`
(0.15, tighter cap specifically for `marketType=="props"` -- confirmed
present/reliable on 100% of 5000 real sampled records, not brittle),
`LIVE_EVENT_EXPOSURE_WARN_EDGE_MULTIPLIER` (1.25),
`LIVE_EVENT_EXPOSURE_WARN_SIZE_MULTIPLIER` (0.75) -- deliberately gentler
than toxicity's 2x/0.5x and on their OWN config surface (this is a
preventive signal with no adverse evidence behind it, unlike toxicity).

**Capital reference:** reuses the exact "account value" formula
`live/equity_protection.py` already established
(`starting_capital_usd + total_position_pnl`) when configured; otherwise
falls back to total deployed cost basis across held positions; both
unavailable -> the whole cap check is skipped for that cycle (mirrors
equity_protection's own inactive-when-unconfigured convention) rather than
dividing by zero or blocking everything.

**Enforcement is per-bucket, not per-market:** a brand-new candidate with
zero position of its own, in a bucket already over-exposed via OTHER
markets, still gets blocked -- and since a flat market treats both BUY and
SELL as "increasing" (already true for toxicity), that correctly stops it
from opening at all. This is the actual mechanism that would have stopped a
10th Portugal-Spain slice from quoting.

**`MarketMaker.reduce_only` (bool) renamed to `reduce_only_reason`
(`Optional[str]`)** -- generalizes the toxicity-only gate added in the "-9."
section to express either trigger (toxicity cooldown, event-cap breach, or
both, joined with `" + "`) with a distinguishable skip message. A hard
event-cap breach only sets `reduce_only_reason` -- it does NOT additionally
shrink size/widen edge (that would throttle the reducing side too, which
should be able to shed over-concentrated exposure at normal size). The
softer warn-tier (below the hard cap, above the warn threshold) does the
opposite: widens edge/shrinks size via `_effective_settings_for`, never sets
`reduce_only_reason`.

**Candidate-selection diversification** (`market_selection.py
::_diversify_by_event`): replaces the old plain `tradable[:max_targets]`
slice with a walk over the already-rank-sorted list, capping how many
candidates from one event bucket can be selected (`max_markets_per_event`,
default 2) -- naturally prefers globally-better-ranked markets first since
the input is already correctly sorted. Count-based only, not pct-of-capital:
`market_selection.py` has zero dependency on live account state today (a
deliberate architectural boundary), so the pct-based caps live in
`multi_market_maker.py` instead.

**New read-only CLI `live-event-exposure`** (mirrors `live-reconcile-orders`'s
shape) groups current positions by bucket and shows cost basis/cash value/
unrealized P&L/% of capital, sorted most-concentrated-first. Manually
verified against the real account: correctly showed the real USA-Belgium
cluster at 19.8% of capital (right at the edge of the cap) as the top row,
no crash.

**Also:** `ledger.py::_sum_position_pnl` renamed to `sum_position_pnl`
(public) -- gained a second real caller (`multi_market_maker.py`, computing
P&L from a positions dict it already has in hand, avoiding a redundant
`get_all_positions()` fetch). **Related, explicitly out of scope:**
`ws_runner.py::_run_one_cycle` still calls `get_all_positions()` up to 3x
per cycle (`_estimate_daily_pnl`, `equity_protection.evaluate`, and inside
`refresh_quotes`) -- this feature only avoided adding a 4th call; fully
consolidating the other 3 is a bigger, separate refactor.

428 tests passing (up from 391), `compileall` clean. Full design validated
by a Plan-agent pass that independently re-verified the slug heuristic
against real data and the `marketType=="props"` stat-prop classifier
feasibility before any code was written.

## -11. Test-suite log contamination (2026-07-06)

**Found while reviewing real trading history** for a "what's working/not
working" review: `logs/bot.log` was found to be roughly a third test-run
noise (6,770 of 20,370 lines matched obvious test-fixture patterns -- `m1`/
`m2`/`m3`, `"boom"`, `"network error"`, synthetic account values) mixed
permanently into real production log entries, with no field distinguishing
one from the other.

**Root cause:** `logger.py::setup_logging()` is guarded by a module-level
`_CONFIGURED` flag and only runs once per process -- but loggers throughout
this codebase are created as module-level singletons
(`logger = get_logger(...)` at the top of each file), so the FIRST call
happens at import time, for whichever module pytest happens to collect
first. That one call attached a real `logging.FileHandler` pointed at the
actual `data/../logs/bot.log` for the rest of the ENTIRE test session --
every subsequent test that exercised any logged code path (nearly all of
them) wrote real entries into the real file, permanently, with no way to
separate them from genuine trading activity after the fact.

**Fix:** `setup_logging()` now checks `"pytest" not in sys.modules` before
attaching the file handler -- `sys.modules` reliably contains `"pytest"` for
the whole duration of any pytest session (pytest imports itself before
collecting any test module), and never does in a real `live-start`/CLI
process. The console handler is unaffected either way. New regression test:
`tests/test_logger.py::test_setup_logging_never_attaches_a_file_handler_under_pytest`
(saves/restores the real root logger's handler list and `_CONFIGURED` flag
around a forced re-run of `setup_logging()`, so it doesn't disturb the rest
of the test session's own logging). Verified empirically: `logs/bot.log`'s
byte size was identical before and after a full 391-test run.

**Not done, lower priority:** the EXISTING contamination in `logs/bot.log`
itself was left as-is -- unlike the `fills.json` schema bug (which broke
real feature data other code depended on, per the "-9." section above), this
is a pure human-readability nuisance with no structured field cleanly
separating real from synthetic entries, and nothing in the codebase reads
`bot.log` programmatically. Worth a manual cleanup pass only if it's
actually getting in the way of a real investigation.

## -10. Single-instance guard (2026-07-06)

**Why:** during this same day's session, TWO separate `live-start` processes
were found running simultaneously against the same real account (one via
the project's `.venv` Python, one via a separate global Python install) --
started manually outside any session, not by the autostart scheduled task
(that task only fires at login and was in "Ready", not "Running", state).
Nothing went wrong that time, but two concurrent refresh loops racing on the
same account's orders/ledger/circuit-breaker/equity-protection state files
is a real, avoidable risk.

**How it works:** `live/instance_lock.py::InstanceLock` is an OS-level
exclusive file lock (`data/live_trades/live_bot.lock`) -- NOT a PID file
with a liveness check. A PID file can go stale if a process crashes without
cleanup, and cross-platform "is this PID still alive" checks are unreliable
(`os.kill(pid, 0)` on Windows actually calls `TerminateProcess` for
non-CTRL signals rather than just probing). An OS-level lock (`msvcrt
.locking()` on Windows, `fcntl.flock()` on POSIX) is released automatically
by the OS the instant the holding process exits for ANY reason -- normal
exit, crash, or `kill -9` -- so no stale-lock cleanup procedure is ever
needed. If you ever see an "already running" error with nothing actually
running, deleting the lock file is harmless, but should essentially never be
necessary.

**Where it's enforced:** inside each runner's `run_forever()`
(`live/runner.py`, `live/ws_runner.py`), wrapping the entire existing loop
body -- NOT per-CLI-command. `live-cancel-all`, `live-reset-breaker`,
`live-reset-equity-protection`, `live-status`, `live-preview`,
`live-reconcile-orders`, and `live-fills` all deliberately stay lock-free:
they're one-shot commands that run and exit in seconds, and
`circuit_breaker.py`'s own module docstring documents the OPPOSITE as the
intended design -- state files are read/written fresh on every check
specifically so a separate CLI invocation (a different OS process) can
reach in and clear a halt while `live-start` keeps running. Locking those
commands out would break that designed interaction pattern.

**Critical ordering, verified against the Python language reference:** the
`with InstanceLock():` wraps the runner's existing try/except-
KeyboardInterrupt/finally-cancel_all body from the OUTSIDE. If
`InstanceLock().__enter__()` raises `AlreadyRunningError` (a second instance
attempting to start), the exception propagates straight out of
`run_forever()` BEFORE the inner `finally: self.client.cancel_all()` block
ever runs -- `__enter__()` always executes before the `with` block's body,
and if it raises, the block body never runs at all. This matters
concretely: a failed second instance must never cancel the FIRST,
legitimately-running instance's real resting orders.

**A real bug caught during design, before any code was written:** the
original draft opened the lock file in append mode (`"a+b"`), which
positions the file pointer at EOF once the file already has content from a
previous run. `msvcrt.locking()` locks a byte range starting from the
CURRENT file position -- without an explicit `seek(0)` before locking,
every restart after the first would lock a different, non-overlapping byte
range and never actually contend with anything. Fixed by always seeking to
0 immediately before the lock call.

**Windows-specific nuance:** Windows file locking is mandatory, not
advisory -- even a plain read from a different handle overlapping the
locked byte range fails with a sharing violation while the lock is held
(confirmed in this feature's own test suite: reading the lock file's
content had to happen AFTER release, not while held). POSIX `flock()` is
advisory by contrast. This doesn't affect the guard's correctness, just
means "peek at the lock file's content while live-start is running" isn't a
reliable ad-hoc diagnostic on Windows.

## -9. Fill persistence + equity protection (2026-07-06)

Two follow-up features requested after the account "more than doubled":
persisting actual fills/executions (not just posted orders), and protection
against giving back gains.

**Fill persistence (`live-fills`, `live/fills.py`).** The private WebSocket
(`live/ws_private.py`) already streamed execution updates into
`PrivateStateStore`'s 50-item in-memory ring buffer, but nothing persisted
them -- a real fill was gone for good once it aged out. Each refresh cycle,
`ws_runner.py::_persist_new_fills()` now drains new executions into
`data/live_trades/fills.json`, enriched (best-effort) with the bot's own
ledger data via the order id.

**WS-only gap:** the REST-polling fallback runner (`live/runner.py`) has no
private-WS connection at all and never populates `fills.json`.

**`fill_quality` ("favorable"/"adverse"/"neutral") is a single-snapshot
proxy, not a rigorous realized spread.** It compares the fill price against
the current BBO fetched at the moment the fill is drained from the ring
buffer -- an uncontrolled 0-to-one-refresh-interval (~60s default) delay
after the real fill, not a deliberate fixed-window academic measurement.
`detected_at` is always recorded so that delay is auditable later. See the
"-9. follow-up" section below for the real 60s/300s markout that replaces
this as the primary adverse-selection signal.

### -9. follow-up, same day: schema fix, filtering, real markout, toxicity widening

Real production data (730 records in `fills.json`) revealed the schema
assumption above was WRONG, and exposed a second, separate bug: only 14 of
730 records were actual fills (`EXECUTION_TYPE_FILL`/`PARTIAL_FILL`) -- the
rest was order-lifecycle noise (`NEW`/`CANCELED`/`REJECTED`) that nothing
filtered out before persisting.

**Confirmed real schema** (verified directly against all 14 real fills):
`order_id`/`market_slug`/`side`/`quoted_price` live NESTED under
`execution["order"]` (`order.id`, `order.marketSlug`, `order.side` -- a
prefixed enum `"ORDER_SIDE_BUY"`/`"ORDER_SIDE_SELL"`, normalized to this
codebase's own `"BUY"`/`"SELL"` -- and `order.price.value`), never at the
top level. Fixed: `fills.py::resolve_order_id_and_market_slug()` and
`build_fill_record()` now read the nested `order` object first, falling
back to top-level `orderId`/`clientOrderId` and ledger correlation only
when `order` is missing/incomplete (fully backward-compatible with the old
assumption -- existing tests with no nested `order` still resolve via the
same fallbacks as before).

**Filtering:** `fills.py::is_actual_fill()` now gates
`ws_runner.py::_persist_new_fills()` -- only `EXECUTION_TYPE_FILL`/
`PARTIAL_FILL` are ever persisted, checked BEFORE any ledger lookup or BBO
fetch (this also fixes a real, previously-masked inefficiency: once
`market_slug` resolution actually worked, every `NEW`/`CANCELED` execution
would otherwise trigger a wasted BBO fetch).

**One-time migration:** `live-migrate-fills` (new CLI, `fills.py
::migrate_legacy_fills()`) drops non-fill noise and re-derives
`market_slug`/`side`/`quoted_price`/`transact_time` for surviving records
from their own already-stored `raw_execution` -- backs up the original file
first (`fills_pre_migration_<timestamp>.json`). Run once against the real
730-record file: 716 dropped, 14 real fills kept and correctly re-derived.
Deliberately does NOT backfill `current_mid_at_detection`/
`edge_vs_current_mid_cents`/`fill_quality` from today's BBO -- a fresh
snapshot now would misrepresent what those fields mean for an old fill.

**1-minute/5-minute markout tracking** (`fills.py::compute_markout_cents`,
`find_due_markout_windows`, `parse_transact_time`; `ws_runner.py
::_compute_due_markouts()`, called every cycle): the real, delayed
realized-spread measurement the single-snapshot `fill_quality` above always
lacked. Piggybacks on the existing ~60s refresh cadence (no new
threads/scheduler) -- scans `fills.json` each cycle for any fill whose real
exchange `transact_time` has crossed the 60s/300s mark and isn't resolved
yet, computes it against a fresh BBO. **Staleness cutoff is required, not
optional:** `LIVE_MARKOUT_MAX_STALENESS_SECONDS` (default 900s/15min) --
without it, deploying this feature (or restarting after downtime) would
compute a meaningless "1-minute markout" using TODAY's BBO for a fill from
hours ago, feeding Phase 3's toxicity EWMA with garbage. A too-stale window
resolves to `None` + a set `_computed_at` (not retried forever) instead of
computing a number. Exactly one `overwrite_fills()` write per cycle covers
every fill whose window came due, not one write per fill.

**Toxicity-aware quote widening** (new `live/toxicity_tracker.py
::ToxicityTracker`, mirrors `VolatilityTracker`'s in-memory/per-market/
no-cross-restart-persistence shape exactly): tracks an EWMA of each
market's 1-minute markout (fed from `_compute_due_markouts()`, never from
5-minute). Crossing `TOXICITY_ADVERSE_THRESHOLD_CENTS` (default -1.0)
starts a cooldown (`TOXICITY_COOLDOWN_SECONDS`, default 600s) for that
market -- a renewed adverse observation during cooldown extends it further;
a favorable observation does not shorten it early. Entering cooldown
triggers all three risk-off responses together, not three independently-
thresholded triggers (matches real market-maker practice, simpler to
reason about): wider `min_edge_cents` (`TOXICITY_MIN_EDGE_MULTIPLIER`,
default 2x), halved size (`TOXICITY_SIZE_MULTIPLIER`, default 0.5x) via
`MultiMarketMaker._effective_settings_for()` (composes with an existing
`settings_override`, e.g. equity-protection's profit-lock sizing, rather
than overwriting it), and `reduce_only=True` on the `MarketMaker` instance
(new constructor param, checked in `_resolve_leg_price` before the
position-cap check). **Deliberate consequence:** at `net_position == 0`
(flat), BOTH sides count as "increasing," so a flat toxic market simply
stops being quoted until cooldown ends -- "sit out of a toxic market with
no position." Wired into BOTH `MarketMaker(...)` construction sites in
`multi_market_maker.py` (orphaned-position loop AND ranked-candidate loop --
an orphaned position on a toxic market needs the caution too).

All three (schema fix, filtering, migration) validated by a Plan-agent pass
that independently read every real fill in the production `fills.json` file
directly, not just a pasted sample. 390 tests passing (up from 328),
`compileall` clean. Manually verified end-to-end: ran `live-migrate-fills`
against the real file (716 dropped / 14 migrated, matching the exact
predicted counts), confirmed `live-fills` now shows real `market_slug`/
`side` values instead of `-`. A real test-isolation bug was caught and
fixed during this work: an early test run of `test_ws_runner.py` (before
its `fills.py::FILLS_FILE` was monkeypatched to an isolated `tmp_path`)
actually wrote resolved-`None` markout fields into the REAL production
`fills.json` -- caught immediately, isolation fixture added, real file
restored. Worth remembering: any new `ws_runner.py` test touching
fill-related methods needs that isolation fixture, not just the
`instance_lock.LOCK_FILE` one.

**Equity protection (`live/equity_protection.py`), alongside the existing
daily-loss circuit breaker:**
- **Drawdown-from-peak stop:** cancels all resting orders and halts once
  account value (`starting_capital_usd` + lifetime position P/L) falls
  `EQUITY_PROTECTION_DRAWDOWN_PCT` (default 20%) below its highest point
  ever seen, persisted across restarts. Deliberately does NOT use any
  Polymarket balance field for "account value" -- `buyingPower` and
  `currentBalance` are both documented elsewhere in this file as unreliable
  for money math (both caused real false circuit-breaker trips). Instead,
  `starting_capital_usd` is a number the user sets manually (currently
  $107, the real account value given 2026-07-06) plus
  `ledger.get_total_position_pnl_usd()` -- a NON-resetting cumulative P/L
  figure split out of `estimate_daily_pnl_usd` specifically for this (the
  daily-resetting figure would cause a phantom ~17%+ "drawdown" at every UTC
  midnight boundary, the exact same class of false-trip bug as the -3.
  section's `buyingPower` incident -- caught by a Plan-agent review before
  any code was written, not discovered live).
- **Same-day profit-lock sizing:** once today's P/L crosses
  `EQUITY_PROTECTION_PROFIT_LOCK_USD` (default $40), order share size is
  scaled by `EQUITY_PROTECTION_SIZE_MULTIPLIER` (default 0.5) for the rest
  of the UTC day -- a **ratchet**, not reactive: it stays engaged even if
  P/L later dips back below the threshold, resetting only at the next UTC
  day boundary (the user's explicit choice, to avoid flip-flopping size
  every cycle if P/L oscillates near the threshold).
- Left at the default `starting_capital_usd=0.0`, only the profit-lock half
  is active -- the drawdown half is inert (logged once) until a real
  starting capital is set. `EQUITY_PROTECTION_ENABLED=true` by default means
  the profit-lock half is live immediately with the shipped example numbers
  ($40/50%), not that the whole feature does nothing until configured.
- `reset()` (via `live-reset-equity-protection`) clears the halt only --
  `peak_account_value_usd` is deliberately preserved, so the next drawdown
  check still measures against the real historical peak, not a
  falsely-lowered baseline re-bootstrapped from whatever the account is
  worth right after a manual resume.
- **Known limitation, accepted:** `get_total_position_pnl_usd` can dip when
  a profitable position fully closes/settles and rolls off
  `get_all_positions()` (the same gap already documented on
  `estimate_daily_pnl_usd`) -- which could cause a false drawdown-triggered
  halt. This fails in the SAFE direction (an over-cautious stop, not a
  missed real drawdown), so it's accepted, not fixed here.
- Wired into BOTH `runner.py` and `ws_runner.py` (no private-WS dependency,
  unlike fill persistence) via `MultiMarketMaker.refresh_quotes`'s new
  `settings_override` parameter -- a `dataclasses.replace()`'d copy of
  `LiveTradingSettings` with only `order_shares_min`/`order_shares_max`
  scaled, leaving order budget and everything else untouched.

## -8. Live order reconciliation: `live-reconcile-orders` (2026-07-05)

A **diagnostic, not a fix**, for the architectural risk named in the -6
section above ("Ledger-based order ownership is architecturally fragile...
add a reconciliation command comparing live open orders against the
ledger's known-id set"). The underlying fragility still exists after this;
this only makes a mismatch visible instead of silent.

**What it does:** `python -m polymarket_bot.main live-reconcile-orders` is a
fully read-only, credential-requiring command. It fetches the exchange's
current open orders and positions, cross-references them against the local
ledger's known order ids, and reports: bot-owned open orders, unknown/manual
open orders, ledger-known orders no longer open (filled/cancelled), and open
positions with no bot-owned order currently resting on that market. It
places no orders and cancels nothing.

**`ledger.py::get_known_order_id_markets()`:** a new function alongside
`get_known_order_ids()`, mapping each known order id to the market slug it
was posted for (`cycle.market_id`, already stored right alongside every
recorded leg — free to expose). `get_known_order_ids()` is now
`return set(get_known_order_id_markets())` — a behavior-preserving internal
refactor, same fail-closed-to-empty-on-read-failure contract, same existing
tests unchanged.

**"Unmanaged position" means no *bot-owned* open order on that market —
deliberately not "no open order at all."** An unrelated manual/unrecognized
order sharing the same slug must not hide a genuinely unmanaged position.
The more precise sidedness question (a resting BUY doesn't actually hedge a
long position the way a SELL would) needs unverified order `side`/`action`
field parsing and is deferred, not built here.

**Stale ledger orders are shown as a recent sample, not a full dump:** the
ledger is append-only and never pruned, so this list can grow unboundedly
over a long-running bot's lifetime. The dashboard renders only the last 10
(most recently *posted*, since the ledger's known-order-id map preserves
insertion order oldest-first — this is recency of posting, not recency of
closing) plus a "showing N of TOTAL" header.

**`ledger_appears_empty` heuristic, with two named blind spots:**
`known_order_markets` is empty while the exchange has open orders. Since
`get_known_order_ids()`'s empty-on-failure contract is already
safety-reviewed and must not change, this can't distinguish "ledger read
failed" from "bot genuinely has nothing" — it's a heuristic flag surfaced
for a human, not a certainty. Blind spot (a): if the ledger read fails AND
there happen to be zero open orders right now, this stays `False` —
harmless, since there's nothing to misclassify either way. Blind spot (b):
a genuinely fresh bot with a manual order already resting correctly shows
that order as "unknown" and this flag correctly stays `False` too — that's
the ledger's intended fail-safe behavior surfacing correctly, not a bug.

**Now a four-way duplicated pattern:** `order.get("id") or
order.get("orderId")` / `order.get("marketSlug") or order.get("market_slug")`
already existed in `us_client.py`, `market_maker.py`, and
`multi_market_maker.py`; `reconciliation.py` (and its caller in
`dashboard.py`, for the raw order dicts it renders) each duplicate it again
rather than importing a private helper across modules. Cleanup opportunity
noted alongside the -6 section's existing three-way cancel-loop dedup note,
not fixed here.

## -7. Expected-value-aware market selection heuristic (2026-07-05)

Live market selection no longer ranks candidates by raw spread alone.
Raw-spread-first ranking treated a wide spread on a dead/untraded market
like the same spread on a market with enough volume/liquidity to plausibly
get filled. That was safer than quoting tight markets with no edge, but it
was not smart about where the bot spends its limited order budget.

**What changed:** `live/market_selection.py::select_target_markets()` now
uses a heuristic expected-value proxy as the primary sort key by default:

`captured_spread_cents * fill_confidence`

`captured_spread_cents` is the real spread left after the bot improves both
sides of the book by one tick each. This is the same calculation used by
the minimum-edge eligibility floor, so selection and placement agree on
what spread is actually capturable.

`fill_confidence` is a volume/liquidity-based ranking proxy from the market
list data already available at selection time. It is NOT a calibrated fill
probability and it is NOT live book depth. Live L2 depth is still validated
later by `MarketMaker.refresh_quotes()` immediately before any order is
posted.

**New config knobs:**
- `LIVE_RANK_BY_EXPECTED_VALUE=true` (default): use the EV proxy as the
  primary ranking key. Set false to restore old raw-spread-first ranking.
- `LIVE_FILL_CONFIDENCE_REFERENCE_DEPTH=5000.0`: `_depth_proxy` value at
  which fill confidence saturates to 1.0. Starting heuristic only.
- `LIVE_MIN_FILL_CONFIDENCE=0.1`: floor used so a brand-new zero-volume
  market with a genuine spread edge is heavily discounted, not erased from
  ranking entirely.

**Important limitation:** this is not a validated EV model yet. There are
still no confirmed real fills in the ledger to calibrate fill rates. Once
real fill history exists, revisit `LIVE_FILL_CONFIDENCE_REFERENCE_DEPTH`
and `LIVE_MIN_FILL_CONFIDENCE` first.

**Deferred on purpose:**
- Maker rebates are not part of the ranking score. They apply roughly
  uniformly to candidates and change overall profitability more than
  relative ordering.
- Volatility is still handled as a binary skip-if-too-volatile guard inside
  `MarketMaker.refresh_quotes()`, not as a ranking penalty. Ranking new
  candidates by volatility needs shared pre-quote price history, which is
  separate plumbing.
- No real fill-rate calibration yet. The current score is an honest
  heuristic pending real account data.
- Worth keeping in mind (external review, not a code bug): `fill_confidence`
  is built from the same volume/liquidity fields `MarketFilters` already
  gates on, not an independent signal -- and for this bot's slow ~60s-refresh
  cadence, a high-volume market could just as plausibly mean more competition
  from faster market makers already capturing the spread as it could mean
  higher fill confidence. The ranking assumes the latter without evidence
  either way. Not fixed; just flagged for whoever calibrates this next.

**Review follow-up, same day:** an external review of this change found and
fixed two bugs in `select_target_market()`'s logging line (the single-market
wrapper, not `select_target_markets`): (1) it read its own `settings`
parameter (which defaults to `None`) instead of the value
`select_target_markets()` resolves internally -- calling `select_target_market()`
with no `settings` argument and successfully finding a market would crash on
`None.fill_confidence_reference_depth` right after the real work succeeded.
Fixed by resolving `settings = settings or config.load_settings().live` at
the top of `select_target_market()` itself. (2) The log line always printed
an `ev_proxy` value even when `LIVE_RANK_BY_EXPECTED_VALUE=false` (raw
spread, not the EV score, actually drove ranking) -- misleading exactly when
an operator flips that toggle to debug the new heuristic. Fixed: `ev_proxy`
is now only logged when it was actually the ranking key used.

## -6. Review follow-up: fail-closed ledger reads, managed-slug fix, and a documented assumption (2026-07-05)

An external review (see the -5 section below, reviewed after the fact) surfaced
several issues in the -5 batch's own sweep logic. Two were fixed immediately;
one was an accepted, documented tradeoff; the rest were deferred.

**Fixed: `ledger.py::get_known_order_ids()` now fails closed.** It used to
have no error handling at all -- a corrupted/malformed local ledger file
(`data/live_trades/orders.json`) would raise uncaught and crash the ENTIRE
refresh cycle (no market quoted, refreshed, or cleaned up that cycle), a
worse blast radius than the account-wide REST fetches in the same function,
which all degrade gracefully. It now: (a) skips an individual malformed
record (e.g. a non-dict `bid`/`ask` leg) without losing the rest of the
ledger's data, and (b) if the ledger genuinely can't be read/parsed at all,
logs loudly and returns an EMPTY set rather than raising. An empty
known-order-ids set makes the ownership check in `multi_market_maker.py`
treat every resting order as "unrecognized" -- i.e. cancel nothing -- which
is exactly the safe degradation wanted: a local-state read failure must
never crash a cycle, and it must never be used as grounds to cancel
anything it isn't sure about.

**Fixed: a market whose own turn crashed is no longer marked "managed."**
`_run_one_market()` used to return only `Optional[LiveQuoteCycle]`, so a
market that raised an exception before its own `MarketMaker.refresh_quotes()`
could reach its internal cancel-before-post step was indistinguishable from
one that ran fine and legitimately decided to post nothing -- both looked
like `cycle is None`. Since `managed_slugs.add(...)` ran unconditionally
right after the call, a persistently-erroring market got marked "managed"
anyway, permanently exempting it from the unmanaged-candidate cleanup sweep
even though its resting order was never actually touched. Fixed:
`_run_one_market()` now returns `(cycle, ran_without_crashing)`, and both
loops in `refresh_quotes()` only add to `managed_slugs` when
`ran_without_crashing` is `True` -- a crashed market now correctly falls
through to the unmanaged-candidate sweep instead of hiding from it.

**Accepted, documented assumption: `MarketMaker._cancel_existing_orders()`
has no order-ownership check.** Unlike the two `MultiMarketMaker`-level
sweeps (unmanaged-candidate, non-candidate-stale), the per-market
cancel-before-post path that runs every cycle for every currently-managed
market (a ranked candidate or an orphaned position) cancels every resting
order matching its market slug unconditionally, with no check against
`ledger.py::get_known_order_ids()`. **This is a deliberate choice, not an
oversight:** this bot is understood to be the only thing placing orders on
this account -- a dedicated-account assumption, not a shared one. If that
ever changes (a manual trade placed alongside the bot, or a second strategy
sharing the same account), a resting order on a market that's currently a
ranked candidate or orphaned position WOULD be cancelled by this path with
no ownership check, since the protection added in -5 only covers markets
outside the bot's active management set. Revisit this if the
dedicated-account assumption ever stops holding -- do not assume the -5
ownership fix protects this path too.

**Deferred to a later pass (not urgent, true but not blocking):**
- Ledger-based order ownership is architecturally fragile (a failed
  `record_cycle()` write after a real order posts, or an unverified
  API response field name in `_extract_order_id()`, can silently and
  permanently make a genuine bot order "unrecognized"). Best fix: record
  attempted order responses more defensively (raw response snippets) and
  add a reconciliation command comparing live open orders against the
  ledger's known-id set. Not done yet.
- Three-way duplicated cancel-loop logic across `us_client.py::cancel_all`,
  `market_maker.py::_cancel_existing_orders`, and
  `multi_market_maker.py::_cancel_orders_on_slugs`. Cleanup, not a safety
  issue.
- The orphan-position loop and ranked-candidate loop in `refresh_quotes()`
  duplicate their bookkeeping (run market, mark managed, check for `None`,
  append/increment) by hand, with nothing structurally keeping the two in
  sync. Cleanup, not a safety issue, though worth doing before the next
  time either loop's bookkeeping needs to change.
- `get_known_order_ids()` re-reads and re-parses the entire (unboundedly
  growing) ledger file every cycle. Efficiency, not a safety issue yet.

## -5. Remaining live-order safety gaps closed (2026-07-05)

Three gaps found in the -4 batch's own new sweep logic, fixed the same day
before any live run exercised them:

**1. Budget-starved candidates were left unmanaged.** The stale sweep
excluded ALL candidate slugs from cancellation, even ones that never
actually got a `MarketMaker` turn because the order budget ran out on an
earlier candidate (or an orphaned position). Their resting orders, if any,
were never refreshed or cancelled -- silently exempt from cleanup for no
good reason. Fixed: `MultiMarketMaker.refresh_quotes()` now tracks
`managed_slugs` (every slug that actually got a turn, orphan or candidate)
and treats `candidate_slugs - managed_slugs` as needing cleanup too, right
alongside the existing non-candidate stale sweep.

**2. A failed positions fetch could nuke orders on undiscovered
positions.** `_find_orphaned_position_slugs` used to return `[]` both when
there were genuinely no orphans AND when `get_all_positions()` failed --
indistinguishable to the caller. On failure, the stale sweep would then
treat every non-candidate open order as fair game, including one on a real
position this cycle simply failed to see. Fixed: it now returns `None` on
failure (distinct from `[]`), and the non-candidate stale sweep is skipped
entirely (with a warning) whenever positions are unknown. The
budget-starved-candidate sweep from fix 1 is unaffected by this -- it
doesn't depend on position data at all, so it still runs.

**3. The stale sweep could cancel an order this bot never placed.** If a
manual trade or a different strategy shared the account, its resting order
on a non-candidate market would get cancelled right alongside genuinely
stale bot orders -- the sweep had no concept of ownership. Fixed:
`ledger.py::get_known_order_ids()` returns every order id this bot has
ever recorded posting (scanned from all recorded live quote cycles,
bid+ask legs). Both sweeps (unmanaged-candidate and non-candidate-stale)
now only cancel an order if its id is in that set; an unrecognized order
is logged and left alone. This ownership check applies ONLY to the two
sweeps -- `MarketMaker`'s own per-market cancel-before-post (when a market
IS being actively managed this cycle) is unchanged, since that's about the
bot managing its own current quote, not a stale-cleanup judgment call.

211 tests passing (up from 203), `compileall` clean. Not yet run against
the real account since these landed -- same discipline as every other
change in this file: treat the next live cycle as a fresh checkpoint.

## -4. Strategy/safety upgrade batch (2026-07-05)

Six changes made together, all defaulting to the SAFE/conservative
behavior. None of this has been verified against the real account yet
(no live run since these landed) -- treat the first live cycle after this
as a fresh checkpoint, same as any other change here.

**1. Stale open-order cleanup across all markets.** `MultiMarketMaker`
fetches account-wide open orders once per cycle (as already established),
then now also cancels any resting order whose market is neither a ranked
candidate nor a held (orphaned) position this cycle. Previously, a market
that dropped out of BOTH sets (e.g. its position fully closed AND it fell
out of the spread ranking in the same window) had no mechanism to ever
cancel its stale resting order. Runs before the per-market loops, using
the candidate/orphan slug sets computed from the FULL candidate list up
front, so a candidate's own still-pending order is never mistaken for
stale before its `MarketMaker` turn runs. Cancel failures are logged
per-order and don't stop the sweep or the cycle.

**2. Fail closed when L2 depth is unavailable.** Previously, if
`get_market_book` returned `None` (unavailable/stale), `MarketMaker`
silently fell back to quoting from a bare BBO number with zero depth
visibility. New default (`LIVE_REQUIRE_L2_DEPTH=true`): no usable L2 book
means no quoting at all this cycle -- cancel existing orders and skip,
exactly like the existing thin-depth case. `LIVE_REQUIRE_L2_DEPTH=false`
restores the old BBO-fallback behavior as an explicit, non-default opt-out.

**3. Market selection is now tick-aware.** `_has_minimum_edge` previously
compared raw `market.spread` against `LIVE_MIN_EDGE_CENTS`, but the bot
actually improves both the best bid and best ask by one tick before
quoting (see `pricing.py::compute_book_aware_quote`) -- so the real
capturable spread is `market.spread - 2*tick_size`, not the raw spread.
A 2c-wide book with a 1c tick has 0c left after improving both sides;
under the old check, that market could still get selected and only get
rejected later, per-cycle, by the order placer. Now selection itself
requires `(market.spread - 2*tick_size) * 100 >= LIVE_MIN_EDGE_CENTS`,
with tick size read from `market.raw["orderPriceMinTickSize"]` (default
0.01) the same way the rest of the codebase already does.

**4. Live trading restricted to verified binary Yes/No markets.** New
eligibility guard in `is_eligible()`: a market must have exactly two
`token_ids` AND exactly two `outcomes` that literally read "yes"/"no"
(case/whitespace-insensitive) to be selected for live quoting. This
directly addresses the long-standing open question in this RUNBOOK about
whether `OUTCOME_SIDE_YES` (always used for both legs in
`market_maker.py`) maps correctly onto non-binary markets -- team-name
sports moneylines, multi-way markets, etc. Rather than guess, those are
now rejected outright for live trading. `LIVE_ALLOW_NON_BINARY_MARKETS`
(default `false`) is the escape hatch, for once that mapping is actually
confirmed -- do not set it true without real verification first.

**5. Volatility / adverse-selection filter.** New `live/volatility_filter.py`
(`VolatilityTracker`): a small in-memory rolling window of recently observed
reference prices per market. If the range within the window exceeds
`LIVE_MAX_RECENT_MOVE_CENTS` (default 3.0c over `LIVE_VOLATILITY_WINDOW_SECONDS`,
default 300s), the market is skipped for that cycle (cancel + skip, same
shape as the depth/edge checks). Toggle via `LIVE_VOLATILITY_FILTER_ENABLED`
(default `true`). One shared `VolatilityTracker` instance lives on
`MultiMarketMaker` (not on `MarketMaker`, which is reconstructed fresh every
cycle) so the rolling window actually persists across cycles.

**6. Private WebSocket -- SCAFFOLD ONLY, not wired into trading decisions.**
`live/ws_private.py` connects to `wss://api.polymarket.us/v1/ws/private`
(same Ed25519 handshake as the already-working public market-data
WebSocket) and subscribes to order/position/balance updates, parsing them
into an in-memory `PrivateStateStore`. `WebSocketLiveTradingBot` now starts
it as a second background thread (`LIVE_ENABLE_PRIVATE_WEBSOCKET`, default
`true`) purely for visibility. **Nothing in `market_maker.py` or
`multi_market_maker.py` reads from it.** Every real order-placement
decision still goes through the already-verified REST
`get_position()`/`get_all_positions()` path. Why not wire it in yet:
docs.polymarket.us documents this channel's message schema as "key fields
include..." rather than an exhaustive, guaranteed shape, and trusting an
unverified schema to drive real-money position/inventory decisions is
exactly the kind of thing this project has learned (the hard way, twice,
on 2026-07-05 alone) to verify against the real account before trusting.
**Follow-up task, not yet done:** watch `PrivateStateStore` against a real
fill, confirm the schema matches what's documented, and only then consider
having `MarketMaker._get_position_summary()` prefer it over the REST poll.

## -3. Circuit breaker false trip: buyingPower conflated reserved margin with loss (2026-07-05)

Right after the -2 fixes below went live, the breaker tripped again for
real: `daily P/L $-24.13 crossed -$20.00 limit`. Investigating with the
user's own real-time observation ("buying power went down because it
bought some stuff but there was no loss at all") found the true cause:
`estimate_daily_pnl_usd` tracked `buyingPower` (free/tradeable cash), which
drops every time the bot posts new resting orders -- posting an order
reserves margin against it, which looks identical to a loss from a pure
balance-diff perspective. Confirmed against the real account: every
currently held position, summed, was within $0.18 of flat (unrealized
cashValue-vs-cost across 4 real positions), while buyingPower had dropped
$24.13 purely from margin reserved for ~18 new resting orders across 9
markets in two cycles. The multi-market rewrite made this far easier to
hit than the original single-market design (2 resting orders max) ever
could.

`currentBalance` was considered as an alternative and rejected: the user
checked and confirmed it's *also* not their real balance right now (same
unexplained ~$75ish gap first seen 2026-07-04, still unexplained).

**Fix:** `live/ledger.py::estimate_daily_pnl_usd` no longer touches any
account-balance field at all. It sums `(cashValue - cost) + realized`
across every position from the new `LiveUsClient.get_all_positions()`,
compares against the same figure recorded at the start of the day (fresh
file `daily_pnl_baseline.json` -- deliberately a new path, not
`daily_balance_baseline.json`, so a stale same-day buyingPower-based
baseline can never get compared against the new metric). This only moves
when a position's actual cost-vs-value changes or a trade realizes P/L --
posting/cancelling resting orders no longer affects it at all.

**Known limitation:** a position that fully closes to flat intra-day may
drop out of `get_all_positions()` before its final realized P/L is
captured by the next snapshot -- smaller and rarer than the bug this
replaced, not eliminated. Verified against the real account: first call
after the fix correctly returned `0.0` baseline (matching the near-flat
$-0.18 aggregate position P/L), not the false `-$24`.

## -2. Multi-market rotation fixes: rate-limit cause and orphaned positions (2026-07-05)

After the multi-market/WebSocket rewrite (many candidate markets quoted per
cycle instead of one), the circuit breaker tripped for real on 2026-07-04 at
21:19:58 (`daily P/L $-25.05 crossed -$20.00 limit`) -- it did its job and
halted, but investigating it surfaced two real issues:

**1. Redundant `GET /v1/orders/open` calls were causing the recurring 429s.**
Each candidate market's `MarketMaker._cancel_existing_orders()` independently
called `get_open_orders()` -- an ACCOUNT-WIDE endpoint, not per-market -- so
with 10 candidates per cycle that endpoint was hit 10x per cycle just to
filter the identical response 10 different ways. Confirmed in the log:
6+ back-to-back 429s on that endpoint in the cycle right before the trip.
Fixed: `MultiMarketMaker` now fetches it once per cycle and passes the same
list into every `MarketMaker.refresh_quotes(open_orders=...)` call.

**2. Positions on markets that rotate out of the ranked candidate list were
being silently abandoned.** `select_target_markets` re-ranks from scratch
every `LIVE_WEBSOCKET_CANDIDATE_REFRESH_SECONDS` (default 300s); any market
not in the new top-N got no `MarketMaker` instance at all -- no cancel, no
re-price, no cost-basis protection -- until the whole bot process stops. This
wasn't theoretical: after the 21:19:58 halt, the account was left holding a
real short position (`astatc-fwc-bra-nor-...`) on a market that had already
fallen out of rotation, with nothing managing it. Fixed: `MultiMarketMaker`
now also calls `LiveUsClient.get_all_positions()` each cycle and gives any
held position outside the ranked candidates a `MarketMaker` turn too (after
ranked candidates, subject to the same `LIVE_MAX_ORDERS_PER_CYCLE` budget) --
using default tick_size=0.01/min_trade_qty=1.0 since it isn't in the current
scan.

**Follow-up same day:** the first version of this fix still processed
ranked candidates *before* orphaned positions, so orphaned positions only
got whatever budget was left over. In practice the ranked candidates
routinely filled the entire `LIVE_MAX_ORDERS_PER_CYCLE` budget on their own
(confirmed live: 5 candidates x 2 legs = 10/10), so the orphaned position
got deferred cycle after cycle with no guarantee it would ever actually be
reached. Fixed: orphaned positions are now processed FIRST, so they always
get first claim on the shared budget -- an existing position with real
money on the line outranks opening a brand-new speculative quote.

**Still true, unchanged:** this only protects against the bot's own orders
compounding a loss or sitting unmanaged -- it doesn't protect against a held
position simply losing value before it can be exited or before it settles.

## -1. Strategy fix and honest profitability assessment (2026-07-04)

**Bug found and fixed:** the original strategy quoted a fixed 3¢ spread
around the market midpoint, regardless of the real order book. Verified
against real data: this left the bot's orders resting *behind* the actual
market on 100% of markets sampled — meaning it would either never fill, or
only fill via adverse selection (the wrong side of every trade). This was
not a viable market-making strategy, it was structurally guaranteed to
either sit idle or lose.

**Fix:** `live/pricing.py::compute_book_aware_quote` now joins/improves the
*real* best bid and best ask by one tick each, and refuses to quote at all
(`live/market_maker.py` returns `None`, cancels stale orders, posts nothing)
unless the remaining captured spread clears `LIVE_MIN_EDGE_CENTS` (default
0.5¢). `live/market_selection.py` was also changed to rank candidate markets
by **real spread** (widest first, among markets that already clear the
quality/liquidity bar) instead of the general research score, which
actually rewards *tight* spread — backwards for a market maker. Verified
live: markets with only a 1-tick-wide book are now correctly skipped with
zero risk taken; a market with real edge gets a genuinely competitive quote
capturing real spread inside the book.

**Fee structure (confirmed via docs.polymarket.us/fees, effective
2026-07-01):** `Fee = Θ × contracts × price × (1 - price)`. Taker
Θ=0.06 (paid), **Maker Θ=-0.0125 (a rebate, i.e. the maker is PAID)**. Since
this strategy only ever posts resting limit orders inside the existing
spread (never crosses it), every fill should land on the maker side and
earn a small rebate on top of the captured spread — a genuine structural
tailwind, not a cost to net out.

**Follow-up fix, same day, from a CLAUDE.md-guided self-review:**
`LIVE_MIN_RECOMMENDATION` originally defaulted to `PAPER_CANDIDATE` (the
top tier on the general research `ScoringEngine`). That score rewards a
*tight* spread — the opposite of what live selection now ranks for — so
requiring the top tier fought the widest-spread-first ranking above,
potentially filtering out exactly the widest-spread (most profitable-if-
real) candidates once spreads get much above ~2¢. The real quality floor
(liquidity ≥ $1000, volume ≥ $500, spread ≤ 10¢, price range, time-to-close)
is already enforced by `MarketFilters` before scoring ever happens, so the
extra tier requirement was redundant on top of that. Lowered the default to
`WATCH` (the lowest non-rejected tier) so a market only needs to clear the
real filters and have genuine spread, not also win a tightness contest it
was never trying to win.

**The honest, sobering context (industry research, 2026):** market-making
returns for *professional* operators are cited around 8-20% annualized —
but that's with $1-10M pools and infrastructure far faster than this bot's
15-minute refresh; retail bots "capture only fragments" of that. Broader
Polymarket bot data: ~92% of wallets show zero or negative returns, only
0.51% clear $1,000 profit, and "fees and slippage absorb most edge below
$5K working capital." Pure arbitrage windows now average 2.7 seconds,
dominated by sub-100ms bots — completely inaccessible to this bot's
architecture. None of this is specific to a flaw in this codebase; it's the
general reality of a $70, 15-minute-refresh bot with no speed or
informational edge, competing in a market with professional participants.

**Verdict:** the fix removes the *guaranteed*-bad mechanic and gives the
strategy an honest, structurally sound shot (real edge required before
quoting, maker rebates on every fill, a $20 circuit breaker capping the
downside to ~28% of the account). It does **not** make profitability
likely. Realistic outcomes range from small, slow gains to a small, slow
loss to roughly flat, dominated by adverse selection during the 15-minute
gaps between refreshes on thin/wide-spread markets. Treat this as a small,
bounded experiment — not an income strategy — unless it's run for a real
stretch and shown to actually work.

## -0. Position-aware quoting: cost-basis floor + inventory cap (2026-07-04)

**Problem this closes:** before this, every refresh cycle re-quoted both
sides purely from the current book, with zero memory of any position the
bot already holds. Two failure modes followed directly from that: (1) if
the market moved against a held position, the bot's own next sell order
could re-quote *below* what it originally paid — voluntarily locking in a
loss it didn't have to take yet; (2) nothing stopped the bot from
repeatedly adding to a losing position every 15-minute cycle, growing
exposure without limit.

**Fix:** each cycle now calls `LiveUsClient.get_position(market_slug)`
(`GET /v1/portfolio/positions`) and applies two independent guards, one per
leg:

- **Cost-basis floor** (`live/pricing.py::apply_cost_basis_floor`) — on the
  side that would *reduce* the position (sell if long, buy-to-cover if
  short), the price is floored/capped at the position's average cost. If no
  valid price exists within tick bounds (the market moved too far away),
  that leg is skipped for the cycle rather than forced through at a loss.
- **Inventory cap** (`LIVE_MAX_POSITION_USD`, default $40) — on the side
  that would *increase* the position, the leg is skipped once the position's
  current mark-to-market value (`cashValue.value` from the positions API)
  is at or beyond this cap. The bot keeps quoting the reducing side; it just
  stops digging deeper on the increasing side.

Each leg is now independently postable/skippable — previously it was
all-or-nothing per cycle. A skipped leg still shows up in `live-status` and
the ledger as a `PostedLeg` with `order_id=None` and `error="skipped: <reason>"`,
distinguishing a deliberate skip from a real API failure.

**Explicit limitation — read this before assuming this makes the bot
"safe":** this protects only against the bot's *own resting order* locking
in a realized loss or piling on more exposure. It does **not** protect
against a held position simply resolving unfavorably at market
settlement — if the market moves against you, the value of what you're
already holding still falls; this feature just stops the bot from making
that specific loss *worse* through its own quoting. Settlement risk on an
existing position is unavoidable and this feature cannot address it.

**Verified 2026-07-04 against the real account:** `get_position()` returned
`None` (no position held, correctly, since no live fill has occurred yet).
This surfaced and fixed a real bug in the process — the first
implementation folded the `?market=slug` query string into the signed
`path`, and Polymarket US's signature check rejects that with a 401 (the
signed message covers only the bare route, `/v1/portfolio/positions`; query
parameters are sent separately, unsigned). Caught before this ever ran
live: with the bug, `get_position()` would have raised on every cycle,
which `market_maker.py` silently treats as "flat, no position data" and
falls back to pre-position-awareness behavior — meaning the feature would
have appeared to work (no errors surfaced to `live-status`) while actually
providing zero protection.

## 0. Credentials

You already have a Key ID + Secret Key issued through Polymarket US's
developer portal. Add them to your local `.env`:
```
POLYMARKET_US_KEY_ID=<your key id>
POLYMARKET_US_SECRET_KEY=<your secret key>
```
Unlike the international platform, there's no wallet, no private key export,
no on-chain allowance step, and no crypto involved at all — Polymarket US
settles in real USD through your linked broker/FCM.

## 1. First auth check: account balances -- ✅ CONFIRMED WORKING 2026-07-04

```
python -m polymarket_bot.main live-status
```

This calls `get_account_balances` and `get_open_orders` — both read-only, no
orders placed or cancelled. **Confirmed working against the real account**:
returned real balance data (`currentBalance`, `buyingPower`, etc. with
`currency: "USD"`) and an empty open-orders list. Ed25519 signing, the
`timestamp+method+path` message format, and the `[:32]`-byte secret slicing
are all confirmed correct.

Two things learned the hard way while getting here, already fixed in code:
- `GET /v1/whoami` returns **404** on `api.polymarket.us` (it's simply not
  deployed there, despite being documented) — `live-status` now treats it as
  best-effort only and doesn't let its failure block the rest of the check.
- The open-orders endpoint is `GET /v1/orders/open`, **not** `GET /v1/orders`
  (which returns 501) — fixed in `live/us_client.py::get_open_orders`.

If auth ever fails again, check:
- [ ] Your system clock is accurate (signatures must be within 30 seconds of
      server time) — confirmed fine via the server's own `Date` header.
- [ ] `POLYMARKET_US_SECRET_KEY` is the exact raw base64 value from the
      developer portal, pasted as a single line (a paste into `.env` can get
      line-wrapped by an editor/terminal, splitting the value across lines --
      this happened once already and was fixed).
- [x] The response shape of `get_account_balances` matches what
      `live/ledger.py::estimate_daily_pnl_usd` expects. **Update 2026-07-04:**
      `currentBalance` ($145.77) did NOT match the user's own knowledge of
      their account ($70, after a withdrawal + $20 deposit + $50 signup
      bonus) — confirmed via Polymarket US's own website, which shows $70.
      `buyingPower` ($70.01) matched exactly. `estimate_daily_pnl_usd` now
      tracks `buyingPower`, not `currentBalance`. The `currentBalance` field
      appears unreliable/stale on Polymarket US's side (plausibly a
      beta-product quirk) — worth reporting to their support, but not a
      blocker since the fix avoids relying on it entirely.

## 2. The biggest open question: does `OUTCOME_SIDE_YES` work for your first market? -- ✅ CONFIRMED for literal Yes/No markets, 2026-07-04

**Update:** placed a real test order — BUY 5 shares YES on
`tec-mlb-nlchamp-2026-09-27-atl` ("Will Atlanta win the NL Championship?")
at $0.02 (well below the $0.129 market, so it wouldn't fill). Confirmed:
- `create_order` succeeded, returned a real order ID, `executions: []`.
- `cancel_order` succeeded (empty response, per spec).
- No order left resting on the account afterward.
- **Also discovered: it turns out nearly every market on Polymarket US,
  including sports ones, decomposes into individual markets with literal
  `outcomes: ["Yes","No"]`** (e.g. "Will Atlanta win the NL Championship?" is
  its own Yes/No market, separate from other teams' markets for the same
  event) — not team-vs-team labels as originally assumed from the one NFL
  moneyline example seen early on. This may mean `OUTCOME_SIDE_YES` is safe
  more broadly than first thought, but this was only tested on one market
  shape (a futures/championship market) — a head-to-head **game** moneyline
  (e.g. the original NFL example with `outcomes: ["Chargers","Titans"]`) has
  NOT been tested and may behave differently. Treat that combination as
  still unverified.
- **New finding: `GET /v1/orders/open` has eventual-consistency lag** — right
  after creating the test order it showed 0 open orders (not yet indexed);
  right after cancelling, it showed 1 (the create had caught up, the cancel
  hadn't); a check ~1 minute later correctly showed 0. `market_maker.py`'s
  cancel-before-replace logic reads this endpoint once per cycle and could
  occasionally miss a just-placed order if timing is unlucky — worth
  watching during item 4 below, and possibly adding a short retry/delay if
  it causes real problems.

Original open question, now narrowed:

Polymarket US's order API supports two ways to specify a side: `intent`
(`BUY_LONG`/`SELL_LONG`/`BUY_SHORT`/`SELL_SHORT`) or `outcomeSide` (`YES`/`NO`)
+ `action` (`BUY`/`SELL`). `live/market_maker.py` always uses the
`outcomeSide`+`action` path with `OUTCOME_SIDE_YES`, which is the natural
choice for a literal Yes/No binary market — but **most of what's actually
listed on Polymarket US right now is sports moneyline markets** (team vs.
team), and it is **not verified** whether `OUTCOME_SIDE_YES` maps sensibly
onto those (vs. needing `intent`/`BUY_LONG` referencing a specific team side
instead).

- [ ] For your first live test, deliberately pick (or let `live-preview`
      show you) a market with literal Yes/No outcomes, not a sports
      moneyline, until this is confirmed.
- [ ] Watch the first `create_order` response closely (`logs/bot.log`) to
      confirm the order was accepted the way you expect, before trusting the
      bot to auto-select any eligible market (currently `exclude_categories`
      is empty by default — see `config.py`'s `LiveTradingSettings` — because
      excluding "sports" outright would rule out most of the platform without
      evidence it's actually necessary; revisit this once confirmed).

## 3. Confirm order IDs and open-order fields parse correctly

`get_open_orders` (now correctly hitting `GET /v1/orders/open`) is confirmed
reachable and returns the right shape for an empty list. What's still
unconfirmed is the field names on an actual **populated** order record --
`live/us_client.py`'s `_extract_order_id`/`cancel_all` parse `id`/`orderId`
and `marketSlug`/`market_slug` defensively, but only a real resting order
will confirm these are the right keys.

- [ ] After your first `create_order` call, check `logs/bot.log` for a
      populated order ID (not `None`) to confirm the field-name guess was
      right.
- [ ] After that order is resting, confirm `live-status`/`get_open_orders`
      actually shows it, and that `market_maker._cancel_existing_orders`
      correctly finds and cancels it on the next refresh cycle.

## 4. Confirm cancel-then-replace doesn't leave a gap

`MarketMaker._cancel_existing_orders()` cancels this market's resting orders
one at a time (there's no documented bulk cancel-all endpoint) before
`create_order` posts the new pair. This is only observable against the real
order book.

- [ ] Watch one refresh cycle happen live and confirm there's no window
      where stale and new orders are both resting, or where neither is.

## 5. Confirm the daily-P/L circuit breaker is actually working

`live/ledger.py::estimate_daily_pnl_usd` diffs the current
`GET /v1/account/balances` total against a baseline recorded at the start of
the UTC day — much simpler than the international platform's trade-fill
parsing, but it assumes **no external deposits or withdrawals** happen
during a live session (those would look like fake P/L swings).

- [ ] Don't deposit/withdraw funds while the bot is running a session.
- [ ] Watch `logs/bot.log` for `"Daily P/L could not be computed"` warnings —
      if you see them repeatedly, the circuit breaker isn't actually
      protecting you and the balances-endpoint response shape needs checking.

## 6. First real `live-cancel-all` and circuit-breaker reset

- [ ] Deliberately trigger `live-cancel-all` once and confirm the book is
      actually clear afterward.
- [ ] If you want to test the circuit breaker, temporarily lower
      `CIRCUIT_BREAKER_DAILY_LOSS_LIMIT_USD`, let it trip, confirm it cancels
      everything and halts, then run `live-reset-breaker` and confirm quoting
      resumes on the next cycle.

## 7. Rate limits

20 requests/second per API key (authenticated) and per IP (public); a
"5-second stopgap" on new orders/modifications that isn't a hard limit but
can return a similar-looking error. The default 15-minute refresh interval
is far under any of these, so this shouldn't bind in normal operation --
just don't lower `LIVE_REFRESH_INTERVAL_SECONDS` drastically without
checking.

## 8. Autostart on login (unattended mode) — set up 2026-07-04

**This runs the bot with real money, with no human confirmation, every time
you log into Windows.** This was explicitly requested, understanding it
removes the typed-confirmation safety net for this specific launch path.

**How it's scoped:** `LIVE_UNATTENDED_MODE=true` is set *only* inside
`scripts/run_live_autostart.ps1`'s own process environment — it is
deliberately NOT in the shared `.env`. This means:
- Running `python -m polymarket_bot.main live-start` yourself from a
  terminal **still requires typing the confirmation phrase**, exactly as
  before.
- Only a launch via the scheduled task below skips that prompt.

**What's registered:** a Windows Scheduled Task named
`PolymarketBotLiveAutostart`, triggered "at log on" for the `dougf` user,
running `scripts/run_live_autostart.ps1` hidden, with up to 3 automatic
restarts (5 minutes apart) if the process dies, and configured not to start
a second instance if one's already running.

**To check if it's running:**
```
Get-ScheduledTask -TaskName "PolymarketBotLiveAutostart" | Get-ScheduledTaskInfo
```
Also check `logs/autostart_stdout.log` (wrapper-level start/stop timestamps)
and `logs/bot.log` (the bot's normal structured logs, including the
"skipping interactive confirmation" warning logged on every unattended start).

**To disable/remove this later:**
```powershell
# Stop it from running at next login (keeps the task, just disables it):
Disable-ScheduledTask -TaskName "PolymarketBotLiveAutostart"

# Or remove it entirely:
Unregister-ScheduledTask -TaskName "PolymarketBotLiveAutostart" -Confirm:$false

# If the bot is currently running and you want to stop it right now:
python -m polymarket_bot.main live-cancel-all   # cancels resting orders first
Get-Process python | Where-Object { $_.Path -like "*Polymarket Bot*" } | Stop-Process
```
You can also just set `CIRCUIT_BREAKER_ENABLED`/lower the loss limit, or
flip `LIVE_TRADING_ENABLED=false` in `.env` — the wrapper script will still
launch on next login, but `require_live_confirmation` will fail closed
immediately (before any credential is loaded) since `enabled` is checked
before `unattended_mode`.
