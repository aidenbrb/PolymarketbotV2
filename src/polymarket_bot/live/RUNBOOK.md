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

## -36. Observation starvation: authoritative L2 selection and stateful exit simulation (2026-07-29)

The multi-day observation experiment reached a useful stopping point rather
than an elapsed-time target. The rolling window repeatedly returned to zero:
the final 15-hour run added no primary fills, no round trips, and no eligible
market. Two selection defects and one observation-model defect explained why
more runtime under the same code was not useful.

**L2 recovery now recovers the current quote too.** During the continuing
REST liquidity/volume outage, `_l2_depth_liquidity_fallback()` fetched a live
book and used its total depth to recover a market, but left the market's REST
`best_bid`/`best_ask`/`spread` untouched. A real 2026-07-29 example was
0.26/0.27 in the listing versus 0.26/0.41 in the fetched L2 book: the old
selector discarded 13 cents of capturable spread after the same lookup had
already proved it existed. The fallback now hydrates BBO, midpoint, and
spread from the authoritative L2 snapshot. Its bounded lookup budget is also
ordered toward hard-safety-eligible, near-term markets before long-dated
records, rather than spending scarce outage lookups in listing order.

**An outage-recovered market no longer gets vetoed by the incompatible
research tier.** `ScoringEngine` is intentionally a research/taker score: it
rewards tight spreads and depends heavily on REST volume. That is already
documented as backwards for market-maker ranking, but `is_eligible()` still
used its recommendation as a hard gate. The real 0.26/0.41 opportunity above
passed L2 depth, the live spread ceiling, binary/family/time checks, minimum
captured edge, and both payoff-ratio checks, yet became `REJECT` specifically
because its spread was wide and REST volume was missing. Only markets that
have passed the configured real-L2 fallback threshold receive an in-memory
outage-recovery marker that bypasses this corrupted recommendation tier.
Ordinary markets retain the existing tier gate, and all actual live safety,
edge, and payoff checks remain in force. A read-only replay of the last real
scan changed the result from zero candidates to the exact L2-verified
0.26/0.41 candidate.

**Observation now follows live L2 opportunities, not a 15-minute REST
snapshot.** The broad WebSocket universe was already collecting books and
trades, but evidence was admissible only for the small set whose REST BBO
happened to pass at scan time. Statically live-eligible markets in the
observation universe can now produce evidence whenever their fresh L2 book
passes the exact depth, edge, extreme-price, paired-payoff, near-resolution,
and pre-event checks. Selector winners remain included unconditionally.

**Shadow execution is now inventory-aware.** Schema v2 kept reposting both
sides after a hypothetical fill and could stack repeated same-direction
inventory. It did not model `LIVE_FLAT_FIRST_INVENTORY_ENABLED`, the patient
reducing quote, or the configured one-hour/near-event marketable exit. As a
result, the gate's required paper round trips did not test the strategy that
would actually run live. Schema v3 now:

- removes the inventory-increasing shadow leg immediately after an entry;
- caps a reducing fill at the open shadow position, so it cannot reverse
  through flat;
- applies the same time-ramped liquidation price bound to the reducing leg;
- records a visible-BBO taker exit at the one-hour or near-event hard
  deadline; and
- computes net paper P/L with the configured signed US fee formula
  (`theta=-0.0125` maker rebate, `theta=0.06` taker fee), so a forced close
  cannot look profitable merely because commission was omitted.

The evidence window is 72 hours instead of 24. This prevents useful results
from disappearing at the next morning's review while leaving every
qualitative gate unchanged. Schema v2 evidence is retained only as an audit
archive; it is not mixed with schema v3 because its exit/P&L semantics are
not comparable.

Focused selection/observation/runner tests cover L2 quote hydration, bounded
lookup priority, the narrow outage-only tier bypass, broad live-L2 evidence,
payoff rejection, flat-first behavior, profitable passive round trips, and
fee-adjusted forced-exit losses. Observation-only mode and all real-order
paths remain unchanged: this section does not authorize live trading.

## -35. Review of `-34.`: observation mode could no longer see -- let alone protect -- an existing position (2026-07-24)

`-34.` made observation-only mode fully passive by skipping `_run_one_cycle()`
entirely (no `MultiMarketMaker`, no private WebSocket, no shutdown
cancel-all) whenever `observation_only_mode` is set. That's the only code
path that manages a held position (reduce-only exits, cost-basis floor,
force_flatten near resolution, circuit-breaker P/L). Skipping it
unconditionally meant a position open when observation starts -- or one
that appears mid-run from any other source sharing the account -- would sit
completely unmonitored for the whole window, with no visibility at all
(the private WebSocket wasn't even connected). `recover_from_prior_crash`
doesn't cover this gap: it only cancels *orders* the local ledger already
recognizes as its own, and has no concept of position risk. The account
happened to be flat both times this ran, so it never bit in practice, but
the gap was real and unbounded by session length.

Closed with three additions, all specific to `observation_only_mode` and
verified not to touch the normal live-trading path:

- `startup_recovery.verify_flat_for_observation()`: fetches open orders and
  positions once and raises `StartupRecoveryError` (refusing to start,
  same fail-closed posture as ordinary crash recovery) if either is
  non-empty. Confirmed via revert-and-confirm-failure that this actually
  blocks startup, not just logs.
- The private WebSocket now starts in observation mode too (previously
  explicitly excluded), seeded from the exact (confirmed-empty) REST
  snapshot the flatness check just fetched -- no second, possibly
  inconsistent REST round trip. `observation_only_mode` now additionally
  requires `LIVE_ENABLE_PRIVATE_WEBSOCKET=true`, refusing to start
  otherwise, so the integrity check below can never be silently absent.
  This does not reintroduce the REST-polling pressure `-34.` removed: the
  WS is a push feed, not a poll loop, and `MultiMarketMaker`'s own REST
  snapshot fetches stay unreachable exactly as before.
- Every refresh cycle, `_abort_if_unexpected_activity_during_observation()`
  reads the private store's own in-memory snapshot (no REST call) and
  raises `ObservationIntegrityError` -- left uncaught, reaching
  `run_forever()`'s top-level handler exactly like `EmergencySafeguardFailedError`
  does for live trading -- the instant an order or position appears.
  Deliberately does not attempt to cancel or manage what it finds: only
  stops. Verified via revert-and-confirm-failure -- removing the call
  turned the corresponding tests into infinite hangs rather than failures,
  about as convincing a proof of "this line is load-bearing" as this
  project has produced.

Also replaced the `-34.` test that was supposed to prove the maker/cancel-all
path is unreachable in observation mode: it pre-set `_stop_event` before
calling `run_forever()`, so the while loop body never executed and the
assertion passed regardless of whether the skip logic was even present
(confirmed empirically -- reverting the skip guard entirely left the old
test green). The replacement lets a real loop iteration run via a
side_effect on the method the observation branch actually calls each
cycle, the same pattern `test_runner.py` already used for the REST-only
runner.

Verified via full suite (924 tests, up from 912) + `compileall`, and each
new safeguard via revert-and-confirm-failure -- done carefully this time
without touching the live process's own data file or lock, since a real
observation-only run was already active against the funded account while
this fix was made (confirmed flat throughout: 0 positions, 0 open orders).

## -34. Passive observation and queue-aware shadow execution (2026-07-24)

The 2026-07-23 observation process ran for 10h03m. Its rolling file contained
1,322 trade prints but only six old-style hypothetical fills; the current
session itself contributed 723 prints and one fill. The old gate marked one
MLS market ready from four prints clustered into two timestamps over twenty
seconds. That was not enough independent evidence to authorize live money.

Observation-only mode is now strictly passive after fail-closed startup
recovery verifies prior bot orders are clean. It does not start the private
WebSocket, run `MultiMarketMaker`, evaluate account P/L, poll open orders, or
issue shutdown/emergency cancel-all calls. This removes the completed run's
658 HTTP-429 log lines, 203 degraded-private-state cycles, and unnecessary
account-wide cancel safeguard. The normal live path is unchanged.

Observation schema v2 replaces the price-cross-only detector with a
queue-aware shadow book:

- only a market currently accepted by the real candidate selector and
  passing the exact shared L2-depth guard can produce gate-admissible data;
- joined BBO quotes carry displayed quantity ahead, which real trade volume
  must exhaust before a shadow fill is recorded;
- a shadow order has the configured live order size and cannot be filled
  repeatedly inside one live refresh interval;
- the current improve-both quote is compared with join-both and the two
  improve-one-side variants;
- both 1m and 5m markouts, distinct fill episodes, paper inventory,
  completed paper round trips, and realized paper cashflow are persisted.

The observation allocation now pins recently trading markets for two hours
(up to 70 slots) while retaining discovery capacity. Gate defaults require
two eligible hours, 20 qualifying trades, five queue-adjusted fills, three
time-separated fill episodes, five 1m and 5m marks, positive markout margin,
three paper round trips, and positive paper P/L. The previous v1 file is
archived because its fills are not comparable to v2 evidence.

## -33. Observation-first redesign after another six-hour zero-fill run (2026-07-22)

The first run after `-32.` was decisive: 2026-07-22 10:23–16:27 local,
2,792 quote cycles, zero fills, and the persisted fill count remained 293.
The duplicate WebSocket request-id fault was gone and many orders were kept
unchanged, so this was no longer plausibly just a cancellation/repost bug.
The passive strategy had no demonstrated fillable edge in the markets it
was discovering.

The live `.env` is now deliberately set to
`LIVE_OBSERVATION_ONLY_MODE=true`. In this mode the WebSocket runner scans,
subscribes, and persists evidence without entering any private-account or
order-management path. Do not turn this flag off just because a clock
elapsed; inspect the evidence first.

The former "100 candidate pool" was not actually a 100-market observation
pool: strict liquidity/spread/edge/recommendation/paired-entry filters ran
first, leaving only 2–13 subscriptions through most of the real run. The new
`observation_markets_out` path deliberately separates these concerns. It
keeps hard safety eligibility (literal binary Yes/No, allowed family, useful
event horizon) but ignores quote-time liquidity, spread, recommendation,
edge, and payoff filters. The runner reserves space for current quote-eligible
markets, fills the remaining subscription capacity from the broad universe,
and rotates that broad window on each candidate refresh. The exchange's
100-market subscription capacity is therefore used for discovery rather
than being silently stranded behind the trading filters.

`market_observation.py` persists a rolling 24-hour record to
`data/live_trades/market_observations.json`:

- actual trade count, shares, maker side, price, and time;
- true observed time, accumulated only while fresh book updates arrive;
- the same one-tick-improved hypothetical bid/ask the maker would use;
- a conservative hypothetical fill only when the real trade direction and
  price would have interacted with that improved quote; and
- 1-minute and 5-minute midpoint markouts for each hypothetical fill.

Once observation-only mode is eventually disabled, the separate
`LIVE_OBSERVATION_GATE_ENABLED=true` gate still fails closed. A flat market
must have, inside the rolling evidence window: at least 30 observed minutes,
5 actual trades, 2 hypothetical fills, a 5% hypothetical fill rate, 2
one-minute markout samples, and non-negative average one-minute markout.
Held positions bypass this entry-candidate gate by becoming the existing
orphan-position management path; evidence can block a new bet, never an
exit.

Run observation with the normal command:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.main live-start
```

The startup banner must say `observation-only mode` and `No new positions
will be opened`. After a full day, stop with Ctrl+C and inspect:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.main live-observation-report --top 30
```

Only markets marked `evidence_ready=True` have cleared the empirical gate.
If none clear after a representative observation window, retire this passive
market-making strategy rather than forcing fills by crossing the spread.

## -32. Zero-fill run: self-book removal, reliable trade subscriptions, execution-first ranking, and lower REST load (2026-07-22)

The 2026-07-21 17:47–2026-07-22 00:07 local run finished exactly flat with
zero fills despite 2,089 quote cycles and 2,889 submitted order ids. The
problem was not the six-leg budget: 317 cycles used all 6/6 legs. Orders
were being cancelled too quickly (median lifetime 39s; 89.7% under 90s),
the log contained 3,901 thin-depth skips, 392 near-resolution pin releases,
26 market-WebSocket `request id already exists` errors, and 281 HTTP 429
warnings.

Four concrete causes are fixed:

- The L2 book is now cleaned of ledger-recognized bot orders before depth
  and target-price calculation. Previously the bot could treat its own
  small improved quote as the external top, then either fail its own
  top-depth threshold or improve against itself on the next cycle.
- Market-data and trade subscriptions now use UUID request ids. An
  asynchronous subscription rejection invalidates local subscription state
  and reconnects for a clean retry. The old two back-to-back requests could
  share the same millisecond id, which explains the run's duplicate-id
  errors and prevented dependable trade-activity data.
- Candidate order is now refreshed every quote cycle using actual observed
  trades and executed share quantity. Book-update frequency no longer counts
  as activity because cancellations can make a book busy without producing
  fills. The watched pool in the real `.env` is 100 (the documented socket
  limit), up from 20, while the live order budget remains six.
- Near-resolution still widens required edge and makes reductions urgent,
  but no longer releases a flat sticky pin by itself. The broad 24-hour
  caution window was causing cancel/re-pin churn. A genuine event-exposure
  cap breach still releases and proactively cancels the pin. Private-WS
  terminal cancel/reject/expiry updates with explicit zero cumulative fill
  now resolve backfill locally, avoiding redundant `GET /order` calls and
  reducing pressure on the shared rate limit.

The strategy remains maker-only. These changes improve the chance of a
profitable fill by preserving queue time and choosing markets where trades
are actually occurring; they do not cross the spread merely to manufacture
account activity.

## -31. Review of `-30.`: the emergency safeguard wasn't actually fail-closed, and the unwound leg's order_id was erased before the ledger could record it (2026-07-21)

Two real gaps in `-30.`'s fix, caught before ever going live:

**The emergency safeguard could silently no-op.** `-30.`'s `except UnsafeOrderStateError` handler (in `refresh_quotes()`) calls `self.client.cancel_all()` as the last-resort cleanup when a placement's state is uncertain. `LiveUsClient.cancel_all()` (`us_client.py`) is deliberately best-effort: if it can't even enumerate open orders, or a per-order cancel fails, it logs and returns normally -- it never raises. The handler wrapped that call in its own `try/except Exception` (swallowing anything it DID raise) and then unconditionally `return None`ed. The `-30.` test for this (`test_paired_entry_unwind_failure_triggers_account_wide_cancel_safeguard`) only asserted `cancel_all.assert_called_once()` -- proof the call was *made*, not that it *worked*. Net effect: if the emergency cancel genuinely failed to clear a naked resting order, the bot logged an error and carried on to the next cycle as if nothing were wrong, with unconfirmed live exposure on the exchange.

Fixed with `MarketMaker._verify_clean_slate()`: after `cancel_all()` runs (regardless of whether it raised), fetch `get_open_orders()` and confirm nothing for `self.market_slug` is still open. If verification fails -- including if the verification fetch itself fails, fail-closed rather than assumed-clean -- `refresh_quotes()` now raises a new `EmergencySafeguardFailedError` instead of returning `None`. This is deliberately NOT caught like an ordinary refresh failure: `multi_market_maker.py::_run_one_market()`, `ws_runner.py::_run_one_cycle()`, and `runner.py::_run_one_cycle()` each gained an `except EmergencySafeguardFailedError: raise` ahead of their existing generic `except Exception` (which exists specifically so one bad market/cycle doesn't take down the whole bot -- the wrong behavior here, since unconfirmed exposure after the LAST line of defense has to stop everything, not just this one market). Left uncaught, it propagates all the way to `run_forever()`'s existing top-level `except Exception: logger.exception(...); raise` (same pattern `-28.`'s `StartupRecoveryError` already established for "refuse to continue"), which `main.py::cmd_live_start` doesn't catch either -- the process exits. New tests cover both outcomes: verification failing (raises, `pytest.raises(EmergencySafeguardFailedError)`) and verification succeeding (returns `None` as before), plus propagation tests at each of the three bypassed layers, each verified by reverting the specific piece and confirming it reproduces the exact silent-continue outcome being fixed.

**The unwound leg's order_id was erased before the ledger could record it.** `_unwind_unpaired_entry()` (`-30.`) rebuilt the cancelled survivor as `PostedLeg(..., order_id=None, ...)` before `record_cycle()` ran. If that order partially filled during the brief window between posting and this bot's own cancel landing -- a real race for a resting limit order -- and the private WebSocket missed that execution (the exact failure mode `-24./-25.`'s execution-backfill mechanism exists to catch), backfill would have no order_id to look up at all: `ledger.get_known_order_details()` is built entirely from what `record_cycle()` persisted, and a `None` id is invisible to it.

Fixed by preserving the survivor's real order_id on the returned/recorded leg instead of nulling it (`_unwind_unpaired_entry()`), so `record_cycle()` (called normally, right after, unchanged) writes it to the ledger exactly like any other posted order -- `_backfill_missed_executions()` can now find and check it. `size=0.0` is what signals "not resting" now that order_id can no longer be trusted alone for that; added `PostedLeg.is_resting` (`models.py`, `bool(order_id) and size > 0`) as the one place that logic lives, and switched every caller that was using bare `order_id` truthiness to mean "is this leg actually live on the exchange" over to it: `multi_market_maker.py::_count_placed_orders()` (order-budget accounting and sticky-pin eligibility -- would otherwise have double-counted a cancelled leg as "placed") and `_run_one_market()`'s `private_store.upsert_local_orders()` filter (would otherwise have immediately re-inserted the very order `last_cancelled_order_ids` had just told the private store to remove, since `_unwind_unpaired_entry()` now also appends to that list so the existing removal wiring picks it up). `market_maker.py`'s record-cycle-write-failure compensation loop was similarly switched to `is_resting`, avoiding a confusing redundant cancel attempt on an already-cancelled order. Confirmed `event_exposure.py` needed no change -- its worst-case-USD projection only ever iterates the exchange's *real* open orders, never trusts the ledger's order_id presence alone, so an already-cancelled order recorded there is never even considered. New tests: a direct ledger-traceability check at the `MarketMaker` level, an end-to-end test driving a real unwind through `refresh_quotes()` and then a genuine `_backfill_missed_executions()` call with a mocked partial-fill response (proving the two previously-disconnected pieces are now actually wired together), a `_count_placed_orders` unit test, and a `_run_one_market` private-store wiring test -- each verified by reverting the specific piece and confirming it reproduces the exact traceability-loss or double-counting bug being fixed.

Verified via full suite (892 tests, up from 884) + `compileall`, and each new safety behavior via revert-and-confirm-failure, same discipline as `-21.` through `-30.`.

## -30. Review of `-29.`: paired-entry unwind on a one-sided maker-only rejection, narrowed 4xx handling (2026-07-21)

A real, serious safety gap in `-29.`'s maker-only change, caught before ever going live: `market_maker.py::refresh_quotes()` posts BUY and SELL sequentially (line ~390). With `participate_dont_initiate=True`, either leg can now come back a clean, benign "rejected -- would have crossed the book" skip instead of raising -- so if BUY succeeds and SELL is rejected (or vice versa), the successful leg was left resting ALONE, even from a flat position under `require_both_entry_legs`, which exists specifically to prevent exactly this: a naked, unpaired directional bet. The existing pre-placement `paired_entry_blocked` check couldn't catch this -- it only sees the bot's OWN skip reasons, computed before either leg is ever sent to the exchange; a maker-only rejection is only knowable after both legs are attempted. Confirmed the review's exact claim: the `-29.` test `test_client_rejection_is_a_benign_skip_not_a_cancel_safeguard` asserted the unsafe outcome (`cycle.bid.order_id == "bid-1"` while the sibling was rejected) using the test file's default `require_both_entry_legs=False` -- not representative of the production default (`True`), so the test was accidentally validating a scenario where a single resting leg is actually fine, while remaining silent about the dangerous one.

Fixed with a new post-placement check, inside the same `try:` block as the two `_keep_post_or_skip` calls: if `require_both_entry_legs` and flat and exactly one of `bid_leg`/`ask_leg` ended up with an `order_id` (posted OR kept -- both already normalize to the same `PostedLeg` shape, so no origin distinction is needed to satisfy "must cover both newly placed and previously kept orders"), `_unwind_unpaired_entry()` cancels the survivor immediately. If THAT cancellation itself can't be confirmed, it raises `UnsafeOrderStateError` rather than silently leaving the leg resting -- caught by the same `except UnsafeOrderStateError` block already handling any other uncertain placement state, reusing its existing account-wide `cancel_all()` safeguard (widened `known_legs` to check both `bid_leg`/`ask_leg`, not just `bid_leg`, since this new path can reach the except block with both legs already validly assigned). New tests cover both directions (BUY accepted/SELL rejected and the mirror), the kept-order case, and the fail-closed cancellation-failure case -- each verified by reverting it and confirming it reproduces the exact unsafe/silent-failure outcome being fixed.

Also narrowed `is_client_rejection()`'s 4xx-is-safe assumption per the review: confirmed via `docs.polymarket.us`'s error-handling reference that a 409 on order creation can mean "duplicate order with the same ClOrdID" -- genuinely ambiguous about whether an earlier attempt actually succeeded (the docs' own guidance is to verify order state via the API, not assume nothing happened) -- unlike a clean 400/422/etc validation rejection. 409 is now excluded from the "safe, benign skip" bucket and treated as uncertain, same as a 5xx, keeping the account-wide cancel safeguard for that specific case.

The reviewer's remaining verdict on `-29.`: `LIVE_MAX_SPREAD=0.20` and the six-order/600s-hold breadth increase both confirmed reasonable; activity tracking confirmed correctly wired to the real documented channels, with the caveat (already documented in `-29.`) that it's necessarily scoped to the currently-watched candidate pool, not global market knowledge; maker-only itself confirmed appropriate and correctly protective by design.

Verified via full suite (884 tests) + `compileall`, and each new safety behavior via revert-and-confirm-failure, same discipline as `-21.` through `-29.`.

## -29. Tighter spread cap, real-activity ranking, modest breadth increase, maker-only entries (2026-07-21)

Four operational changes, following up on `-27.`/`-28.`. Two are `.env` tuning edits (`LIVE_MAX_SPREAD` 0.98 -> 0.20, `LIVE_MAX_ORDERS_PER_CYCLE` 4 -> 6, `LIVE_STICKY_MARKET_HOLD_SECONDS` added at 600) directly in the real `.env`, not just `.env.example`/code defaults. Two required code changes, both verified against the real Polymarket US API/docs (not the international platform's docs, a repeat source of confusion earlier this session):

- **Real-activity ranking.** Confirmed live against the running bot's own client: the market-list REST scan and the L2 book REST endpoint carry zero activity fields (no `sharesTraded`/`openInterest`/trade data at all). Those fields ARE real, but only over the WebSocket -- confirmed via `docs.polymarket.us`: `marketData`'s `stats` object (and `marketDataLite`) already carry `sharesTraded`/`openInterest` (previously received but never read past `currentPx`/`lastTradePx`), and a completely separate, never-subscribed channel, `SUBSCRIPTION_TYPE_TRADE = 3`, streams real trade events. `ws_market_data.py::StreamingMarketDataStore` now tracks all three in rolling windows (`activity_window_seconds`, default 300s, same idiom as `VolatilityTracker`) and subscribes to the trade channel alongside market data. `market_selection.py::select_target_markets()` gained an optional `activity_scores` param -- when a real, WS-observed activity score exists for a market (only ever true for one already being watched >=1 cycle), it replaces the static volume/liquidity fill-confidence proxy entirely (a real signal beats a proxy that's often zero right now due to the ongoing upstream volume/liquidity field outage). `ws_runner.py::_compute_activity_scores()` blends recent-trade presence, sharesTraded growth, and book-update frequency into that score with a fixed, documented formula (not more tunables). The REST-only `runner.py` path and a market's first-ever selection are unaffected -- there's no activity data to rank by until a market has been watched at least once.
- **Maker-only entries, with a required companion safety fix.** `docs.polymarket.us`'s create-order schema confirms `participateDontInitiate` (boolean; "order will be rejected if it would immediately match") is real and exact. `LiveUsClient.create_order()` gained `participate_dont_initiate`, set `True` only at the ordinary GTC entry/reducing call site (`market_maker.py`) -- NOT on force_flatten's IOC legs, which must be allowed to take liquidity for a mandatory exit. This surfaced a real, pre-existing bug that would have made the feature dangerous as a bare flag: `_post_leg` treated ANY `create_order()` failure as `UnsafeOrderStateError`, triggering an account-wide `cancel_all()` safeguard -- correct for a genuinely uncertain placement (timeout, 5xx) but wrong for a maker-only rejection, which is a clean, synchronous, definite "nothing was placed" outcome that would become frequent once this ships. Added `us_client.is_client_rejection()` (same `__cause__`-introspection pattern as the existing `_is_not_found()`): a 4xx now resolves to a benign skipped leg (`error="rejected: ..."`, cycle continues normally), while a 5xx/timeout/connection failure keeps the existing nuclear cancel-all exactly as before. Verified the distinction actually matters by reverting it and confirming a 4xx incorrectly triggered a full account-wide cancel, exactly as it would have in production.

Every piece verified by reverting it individually and confirming its dedicated test reproduces the exact failure mode being fixed, then restoring -- same discipline as `-21.` through `-28.`. Full suite (875 tests) and `compileall` green.

**Unrelated but worth recording: `-28.`'s crash recovery confirmed working in production during this work.** A real live session (PID 8680, `data/live_trades/live_bot.lock`) ran 17:37:50-20:06:06 UTC today, entirely on the pre-existing `-27.`/`-28.` code and `.env` (confirmed via file mtimes -- every edit for this section landed at 20:30 UTC or later, well after that session had already ended). On startup it correctly found the 2026-07-20 session still `"status": "running"` (the original stuck-session incident `-27.`/`-28.` fixed) and marked it `"crashed"`, then ran its own session to completion (1334 quote cycles, 0 fills, clean Ctrl+C shutdown, `"status": "stopped"`). None of this section's changes were live during that session.

## -28. Review of `-27.`: directional min-resting-time safety, and fail-closed startup recovery (2026-07-21)

Two real safety gaps found in `-27.` before it ever ran live:

- **The min-resting-time gate was direction-blind.** `market_maker.py::_reconcile_existing_orders` protected a young order from repricing regardless of which way the price had moved -- so a resting BUY at $0.49 would stay live for up to 90s even if the newly computed target fell sharply to, say, $0.29 (adverse selection: the bot would keep buying above fair value if that stale bid got filled). Fixed: outside the hysteresis band, direction now matters. A BUY priced ABOVE the new target, or a SELL priced BELOW it, has drifted to the MORE aggressive side and is cancelled immediately regardless of age -- `min_resting_seconds` only protects an order that drifted to the LESS aggressive side (safe, just less likely to fill). Also fixed `_order_age_seconds` to prefer `insertTime`/`createTime` (the order's true original post time) over `lastTransactTime`, which updates on any transaction event including a partial fill -- preferring it would make a long-resting, since-partially-filled order look freshly posted and wrongly re-earn protection. New tests cover both the BUY-above/SELL-below adverse cases and the partial-fill-shouldn't-reset-the-clock case; each was verified by reverting it and confirming the corresponding test reproduces the exact unsafe scenario.
- **Startup recovery failed open.** `startup_recovery.py` caught an open-orders enumeration failure or a cancel failure, logged it, and returned as if nothing needed cleanup; both runners also caught any exception from the whole recovery call and continued starting regardless. A 429 (or any transient API error) during recovery could silently let the bot proceed to place new orders while a prior crashed session's resting orders stayed unmanaged -- defeating the entire point of `-27.`'s fix. Rewired to fail closed: a new `StartupRecoveryError` aborts startup if open orders can't be enumerated, if any recognized leftover order fails to cancel, or if a post-cancellation re-fetch still shows a bot-recognized order resting (a `cancel_order()` call not raising doesn't guarantee the exchange actually removed the order). Both runners now let this propagate out of `run_forever()` (logged via `logger.exception` first, so it lands in the rotating `bot.log`) instead of catching and continuing -- session metrics, candidate scanning, and quote placement never start if a clean slate can't be established. Session-status bookkeeping (`mark_stale_running_sessions_crashed`) stays best-effort/non-fatal -- it's a historical record, not a safety-critical action. Verified each abort path (enumeration failure, cancel failure, post-cancel verification failure, and the runner-level wiring itself) by reverting it and confirming the matching test fails -- the runner-level wiring check was caught by an actual test hang (the reverted code let the main loop start with no stop condition configured), a strong confirmation the wiring is genuinely load-bearing.

## -27. Background candidate scanning, price hysteresis/min resting time, and crash recovery (2026-07-21)

A ~9h40m live run right after `-26.` shipped (2026-07-20 18:52 UTC -> 2026-07-21 04:32 UTC) was profitable but tiny (+$0.0174, one round trip) and surfaced three real operational gaps:

- **Candidate scanning blocked the quote loop.** `ws_runner.py::run_forever()`'s main loop called `_maybe_refresh_candidates()` (a full ~5000-market scan plus up to `liquidity_fallback_max_lookups` -- 500 since `-26.` -- sequential, unpaced L2 lookups) synchronously, on the same thread as `refresh_quotes()`. Every `websocket_candidate_refresh_seconds` (900s) window fully stalled quote management -- 38 gaps, ~81 min total, ~14% of the run, matching 34,800s / 900s almost exactly. Fixed: `_refresh_candidates()` now runs on a short-lived background daemon thread (`_refresh_candidates_in_background`), guarded against overlapping refreshes, using the same "build fully, then swap under `self._candidates_lock`" pattern the code already used for `self._candidates` -- no new synchronization design needed.
- **Orders still churned despite `-26.`'s sticky market selection**, roughly every ~50s. `market_maker.py::_reconcile_existing_orders` only kept a resting order when its price matched the new target to within `tick_size/1000` (effectively exact) -- any ordinary tick-to-tick book movement forced a full cancel+repost. Added price hysteresis (`LIVE_PRICE_HYSTERESIS_TICKS`, default 1 tick -- keep an order within this band of the target) and a minimum resting time (`LIVE_MIN_RESTING_SECONDS`, default 90s -- don't discretionarily reprice a younger order even outside the hysteresis band), both read from the exchange's own order timestamps (`insertTime`/`createTime`, reusing `fills.py::parse_transact_time` -- no new tracking needed). `force_flatten`/`reduce_urgent` (risk exits) always bypass both and reprice immediately.
- **No crash recovery.** Confirmed real: `live_bot.lock` named a PID with no matching process; the session's `sessions.json` record was permanently stuck `"status": "running"`, `"ended_at": null`. Traced the actual mechanism: `bot.log` shows completely normal activity up to 00:32:42 UTC and then just stops -- an abrupt external termination, not a Python exception (an ordinary exception already triggers `run_forever()`'s existing `finally:` cleanup today; that was never the gap). Added `live/startup_recovery.py::recover_from_prior_crash()`, called once at the start of both runners' `run_forever()` (behind `LIVE_STARTUP_CRASH_RECOVERY_ENABLED`, default on): successfully acquiring `InstanceLock()` is itself proof no other bot process is alive, so any `sessions.json` record still `"running"` at that point is marked `"crashed"` (new `session_metrics.mark_stale_running_sessions_crashed()`), and any resting order the local ledger recognizes as bot-owned gets cancelled before this run places anything new. Also: `logger.py`'s `bot.log` handler is now a `RotatingFileHandler` (`LOG_MAX_BYTES`/`LOG_BACKUP_COUNT`, default 20MB x 5) instead of unbounded, and both runners now log (`logger.exception`, so the traceback lands in that rotating log) and re-raise on an unexpected exception escaping the main loop -- diagnoses the narrower case of an actual Python-exception crash specifically; startup recovery above is what closes the abrupt-kill case the real incident actually was.

**Real production-data incident caught and fixed during this work, not before it:** `tests/live/test_runner.py` never needed to isolate `session_metrics.SESSIONS_FILE` before, since `runner.py`'s `LiveTradingBot` never touched session tracking -- but the new `recover_from_prior_crash()` call does (unconditionally, regardless of which runner calls it), and that test file's `run_forever()`-based tests weren't isolating it. Running the full suite repeatedly while iterating on this work actually flipped the real `data/live_trades/sessions.json` record for the 2026-07-20 crashed session from `"running"` to `"crashed"` with a fabricated `ended_at` timestamp -- caught by noticing the real file's content had changed mid-session, root-caused to the missing fixture isolation (now added, verified via file-mtime-unchanged across a full suite run), and the corrupted record's three affected fields were manually restored to their original values (all other fields, including the real PnL/fill-count history, were untouched by the bug and needed no repair). A reminder that a new code path reading/writing shared state needs its test isolation audited across *every* file that exercises it, not just the ones already known to touch that state.

## -26. Sticky market allocation: stopped rotating markets every refresh cycle (2026-07-20)

A live run right after `-25.` shipped (13:25-13:29, `LIVE_MAX_ORDERS_PER_CYCLE=4`, `LIVE_REFRESH_INTERVAL_SECONDS=10`) confirmed the backfill fix works (no freeze, exactly 5 lookups/cycle x 20 cycles = 100) but exposed a separate, real trading problem: **20 refresh cycles, 62 newly posted order legs, ZERO fills, $0.00 session P&L.**

Root cause, confirmed against real `logs/bot.log`: `select_target_markets()` ran ONCE (13:25:27, one scan), producing a stable set of 9 candidates the WS runner cached for its whole 900s candidate-refresh window. Yet `multi_market_maker.py::refresh_quotes()`'s `prioritized_candidates` sort had zero memory of which markets were managed last cycle -- for flat (no held position) candidates its sort key was identical for everyone, so which 2-of-9 markets won the 4-leg budget was decided fresh, from scratch, every single ~11s cycle. The actual log showed the winning pair alternating almost every cycle (e.g. `astatc-valorant-jdg-te` map1+map2 -> `astatc-valorant-edg-nova` map1+map2 -> back to jdg-te -> ...). Orders rested only ~10-20s each -- nowhere near long enough to fill. `market_maker.py::_keep_post_or_skip` already avoided needless cancel/replace for a market that KEPT its slot with an unchanged price -- the only missing piece was keeping the same markets selected across cycles in the first place.

**Fix -- sticky market allocation.** `MultiMarketMaker` now tracks `self._pinned_markets: dict[str, float]` (market_id -> when first pinned). A flat candidate that wins the shared order budget and posts/keeps >=1 leg stays preferred over fresh ranking (`_apply_sticky_priority` extends the existing force_flatten/held-position sort with a third, strictly-lower tier) until:
- **The hold window expires** (`LIVE_STICKY_MARKET_HOLD_SECONDS`, default 300s -- "several minutes," ~30 cycles at the 10s refresh interval used in the incident run). Demoted with NO proactive cancel -- if it's still the best market it re-wins on its own merit and keeps its resting order completely untouched; if not, the existing post-hoc unmanaged-candidate sweep cleans it up, same as any ordinary lost-budget-race candidate.
- **It becomes unsafe** (event-cap over, or near-resolution -- the same two checks the placement loop already computes per-candidate, deliberately NOT including toxicity cooldown or account-wide breaker_risk, both already handled safely at the per-market level without needing the pin revoked). This releases the pin **and** proactively cancels its resting order **before** the placement loop runs, so a deliberate swap never even briefly exceeds the leg budget -- the fix for "enforce four legs globally, including pending cancellations," scoped to the one case where the overlap is actually urgent.
- **It stops posting legs on its own merit** (insufficient edge, thin book). No new handling needed -- every 0-leg early-return path in `market_maker.py::refresh_quotes` already cancels its own stale orders. The pin itself is deliberately NOT evicted on a single bad cycle (would recreate the exact churn this feature fixes) -- it keeps its sort-tier advantage for more attempts until the hold window itself times it out.

Also relocated `_backfill_missed_executions` to run **before** either placement loop (previously at the very end of the cycle), scoped to `candidate_slugs | orphaned_slugs` (was `managed_slugs` -- a superset now, closing a related small gap where a ranked-but-never-turned candidate wasn't checked). This made the old `cycles`-based just-placed-order exclusion structurally dead (nothing is placed yet at call time) and it was removed along with the `cycles` parameter. Confirmed while reverting this piece for verification: without the relocation, this cycle's own freshly-created orders get treated as "missed execution" candidates and backfilled with fabricated fills -- the exact false-positive class `-24.` originally guarded against, now eliminated structurally rather than by exclusion-list bookkeeping.

**Liquidity/volume data gap -- investigated, not a local bug.** Re-confirmed via fresh log evidence: all 5000 scanned markets still report zero `liquidity`/`volume`/`volume24hr`, the same upstream Polymarket outage diagnosed in `-23.`/`-24.`, now 2+ days ongoing. No alternate field exists to parse locally (`docs.polymarket.us`'s real schema was already checked). The only locally-actionable lever, `liquidity_fallback_max_lookups`, was bumped from 200 to 500 (`LIVE_LIQUIDITY_FALLBACK_MAX_LOOKUPS`) for more candidate diversity -- a mitigation, not a fix; costs ~15s extra once per 900s candidate refresh, not per quoting cycle.

Verified each of the 5 pieces (pin-priority sort, unsafe-release proactive cancel, hold-expiry no-proactive-cancel, 0-leg-cycle pin survival, backfill relocation) by temporarily reverting it and confirming its dedicated new test in `tests/live/test_multi_market_maker.py::TestStickyMarketAllocation` fails, then restoring -- same discipline as `-21.` through `-25.`. Full suite (818 tests) and `compileall` green after restoring all five.

## -25. Hardened the execution-backfill mechanism: rate limit, per-cycle bound, cross-restart persistence, partial-fill-then-cancel detection (2026-07-20)

`-24.`'s `_backfill_missed_executions()`/`_backfill_one_order()` closed the
gap where a private-WS outage silently drops a fill, but had four remaining
robustness/correctness gaps, all fixed together:

- **Not rate-limited.** `get_order()` calls happened back-to-back in a tight
  loop, sharing the account-wide 20 req/s limit (`docs.polymarket.us`) with
  the same cycle's order placement/cancellation/reconciliation calls. New
  `LIVE_EXECUTION_BACKFILL_MIN_INTERVAL_SECONDS` (default 0.1) paces
  successive lookups within one cycle.
- **Not bounded per cycle.** Every qualifying candidate in `managed_slugs`
  got checked in one pass -- after a long outage with many stale orders, one
  cycle could make an unbounded number of calls. New
  `LIVE_EXECUTION_BACKFILL_MAX_LOOKUPS_PER_CYCLE` (default 5) caps it;
  candidates beyond the cap are simply left for a later cycle (no special
  tracking needed -- anything unresolved still qualifies as a candidate
  next time).
- **Not persisted across restarts.** The "confirmed nothing to backfill"
  determination lived only in an in-memory `self._backfill_resolved_order_ids`
  set, lost on every restart, so a restart re-queried the same long-resolved
  orders forever. Replaced with a new persisted file
  (`data/live_trades/backfill_resolved_orders.json`, via new
  `fills.get_backfill_resolved_order_ids()`/`record_backfill_resolved()`),
  read fresh each cycle -- same "recompute from persisted files" pattern
  already used by `fills.already_recorded_fill_ids()` elsewhere in this
  module. The in-memory set was removed entirely rather than kept in sync
  with the file. A "backfilled a fill" outcome doesn't get its own resolved-
  marker -- it's already implicitly excluded from future candidacy via the
  fill's own `order_id` in `fills.json`.
- **Missed partially-filled-then-cancelled orders.** `_backfill_one_order`
  only backfilled when `state == "ORDER_STATE_FILLED"`. An order that filled
  PARTIALLY before its unfilled remainder was cancelled (state
  `ORDER_STATE_CANCELED`/`EXPIRED` with `cumQuantity > 0` -- plausible for
  this bot's own IOC orders, which auto-cancel any unfilled remainder) fell
  into the `else` branch and was permanently marked resolved with nothing
  backfilled, silently losing those shares' fill/P&L forever. Fixed by
  backfilling on any nonzero `cumQuantity`, regardless of terminal state.
  Sampled 51 real order_ids spread across this bot's full order history via
  `get_order()` specifically looking for this shape -- found none in the
  sample checked, so this wasn't observed causing an actual loss historically.
  The fix stands regardless: the logic gap itself is unambiguous from
  Polymarket's schema (a nonzero `cumQuantity` isn't exclusive to
  `ORDER_STATE_FILLED`), and confirmed by temporarily reverting it and
  watching the new regression test reproduce exactly this failure (a
  partially-filled cancelled order silently dropped with 0 fills recorded).

Verified each of the four fixes individually by temporarily reverting just
that piece and confirming the corresponding new test in
`tests/live/test_multi_market_maker.py::TestExecutionBackfill` fails, then
restoring it -- same discipline as `-21.` through `-24.`. Full suite (812
tests) and `compileall` both green after restoring all four.

## -24. Response to an external review: accounting robustness, execution backfill, candidate-data fallback, retry timing, exposure model (2026-07-19)

A review of `-21.`/`-22.`/`-23.`'s changes raised 5 concerns. Each was
verified against real data (fill history, live API scans, and
Polymarket's actual current documentation) before responding -- two of the
five didn't hold as literally stated, but the underlying instincts were
reasonable and led to real improvements regardless.

**Confirmed and fixed:**

- **`lot_accounting.py`'s empty-`_REDUCING_INTENTS` fix (`-22.`) was
  directionally correct but had no defense against genuine pre-tracking
  inventory.** Checked all 58 affected fills directly: every one is on a
  single-game prop market whose slug has that game's date baked in, and
  the fill's own timestamp matches -- these markets provably can't have
  pre-tracking history, and Polymarket's CTF mint/merge settlement paths
  confirm a SELL can legitimately open a fresh short with zero prior
  holdings. But the heuristic only works because of THIS bot's actual
  market mix, not as a general guarantee. `compute_lots()` now takes an
  optional `earliest_snapshot_by_slug` (new
  `settlements.get_earliest_snapshot_by_slug()`): if a slug's earliest
  recorded `position_snapshots.json` row already shows nonzero inventory
  at or before a fill, AND that fill is the slug's own chronologically
  first (the restriction matters: a real false positive was caught mid-
  implementation where a legitimate flip-through-flat-and-reopen sequence
  -- SELL 6.5, BUY 6.5 closing it, SELL 6.0 reopening -- was wrongly
  flagged by a snapshot taken while the FIRST short was still open,
  because it predated the reopen fill in wall-clock time even though
  fills.json's own history already fully explained the empty deque at
  that point), the fill routes to `unmatched_closing_fills` instead of
  fabricating an open. Verified against real `fills.json`: recovers to
  essentially the same $24.37-ish total as `-22.` (one additional, clearly
  legitimate ambiguous case now correctly flagged: a BUY whose slug's
  earliest snapshot already showed a *short* position before it).

- **No execution-history recovery after a WS gap -- confirmed accurate.**
  `us_client.py` had no order/execution-history endpoint. Polymarket US
  has no bulk history endpoint either, but `GET /v1/order/{orderId}`
  **does** work for terminal orders and returns `cumQuantity`/`avgPx`/
  commission fields. Added `LiveUsClient.get_order()` (confirmed against a
  real filled order that the response is wrapped in a `{"order": {...}}`
  envelope, unwrapped accordingly -- the docs summary didn't make this
  obvious). `MultiMarketMaker._backfill_missed_executions()`, scoped to
  markets actually managed this cycle (bounded, not a full ledger crawl):
  any ledger-known order_id neither currently open nor recorded as filled
  gets looked up; a `ORDER_STATE_FILLED` result is reconstructed into a
  synthetic `FillRecord` (necessarily an aggregate -- this endpoint has no
  per-execution granularity) and persisted via the normal `record_fill()`
  path. A real false positive was caught and fixed during testing: orders
  placed THIS SAME cycle aren't yet reflected in the start-of-cycle
  `open_orders` snapshot, so every fresh order looked like a "missed
  execution" candidate on every single cycle until `cycles`' own
  just-placed order ids were explicitly excluded.

- **Failed candidate scans waited the full 900s to retry -- confirmed
  precisely.** `_refresh_candidates()` now returns success/failure;
  `_maybe_refresh_candidates()` only advances on success, retrying failures
  after the new, much shorter `LIVE_WEBSOCKET_CANDIDATE_REFRESH_RETRY_SECONDS`
  (default 60s) instead. A real bug was caught and fixed while implementing
  this: a naive "due if EITHER the normal OR retry interval elapsed, each
  checked against its own baseline" design let the normal-interval check
  (measured from `_last_candidate_refresh`, which stays frozen at its
  initial `0.0` while failures persist) stay perpetually true forever once
  a single failure occurred -- `time.monotonic()` has an arbitrary epoch
  (often system boot, not process start), so `now - 0.0` is already far
  past 900s on the very next tick, completely bypassing the retry throttle
  for as long as refreshes kept failing. Fixed with a single due-check
  against one shared baseline (`_last_candidate_refresh_attempt`), using a
  shorter interval specifically when the last attempt failed.

- **Liquidity fallback: the reviewer's specific field-name claim was
  refuted, but the underlying instinct led to a real fix.** The cited
  "Markets API" documentation is `docs.polymarket.com` (the international
  platform this project doesn't use). Fetched the actual
  `docs.polymarket.us` schema: only `volume`/`volume24hr`/etc. are
  documented for this platform -- there is no `liquidity`/`liquidityNum`
  field to switch to. `-23.`'s root-cause finding (an upstream regression
  dropped these fields starting ~2026-07-18) stands, confirmed still
  broken via a fresh live scan. Built the fallback recommended anyway,
  correctly targeted: `market_selection.py::_l2_depth_liquidity_fallback`
  -- a market rejected ONLY for liquidity/volume (every other quality-bar
  check already passed) gets a real L2 order-book depth lookup via the
  already-existing `PolymarketClient.get_market_book`, bounded to
  `LIVE_LIQUIDITY_FALLBACK_MAX_LOOKUPS` (default 200) to respect the 20
  req/s account-wide rate limit. **Verified live against the still-broken
  real API**: 199 of 200 checked markets recovered real, substantial book
  depth despite `liquidity`/`volume` reading 0, and `select_target_markets()`
  went from 0 candidates to 4 -- the bot can trade again without waiting
  for Polymarket to fix the upstream field.

- **Event-exposure sum vs max: correct math, pre-existing design, shipped
  gated on an incident replay.** The reviewer's `max(bid_risk, ask_risk)`
  reasoning is right for a single market's own bid+ask in isolation (a
  filled bid and filled ask on the same token can't both be simultaneously
  realized risk -- if both fill, they net to a captured spread). But "sum
  both sides at flat" was deliberate, pre-existing (predates this session's
  own `-21.` fix, which only made it consistently applied across
  `market_maker.py`/`multi_market_maker.py`/`event_exposure.py`, not
  invented it), and was built specifically to close the real 2026-07-09
  Argentina-Switzerland incident: sibling markets in one bucket, same
  cycle, a static snapshot with no awareness of each other. That's a
  DIFFERENT mechanism (cross-market, same-cycle running tally) from the
  single-market bid/ask question, so proving max() is safe for one doesn't
  automatically prove it for the other. Changed all three sites
  (`market_maker.py`'s headroom clipping, `multi_market_maker.py`'s
  `_record_provisional_exposure`, `event_exposure.py`'s
  `_resting_worst_case_by_bucket`) to take the MAX of a single market's
  own two legs while keeping cross-market aggregation fully additive, then
  added a gating regression test that reconstructs the real incident shape
  (three sibling markets, $10 shared cap, each wanting $4.90) and confirms
  the third sibling is still correctly starved, keeping total worst-case
  exposure at or under the cap. Verified the test has real teeth by
  temporarily disabling the cross-market tally and confirming it fails
  exactly as the real incident did (all three post unclipped, $14.70
  total against the $10 cap) before restoring the fix.

## -23. Zero-trade day root-caused to an upstream data outage; two crash-safety gaps closed (2026-07-19)

Reviewed 2026-07-18: the bot placed **zero orders across two sessions and
~9.5 hours of runtime** -- confirmed 2039 of 2039 quote cycles had
`candidates=0`. Root cause is external, not a strategy or config problem:
Polymarket US's market-listing API stopped including the `volume`/
`volume24hr`/`volume1wk`/`volume1mo`/`volume1yr` fields on every market
record sometime between 2026-07-18 01:39 EDT (present, 3289/5000 markets
with real volume) and 16:46 EDT (absent from all 5000). `market_scanner.py`
uses `volume` as its liquidity proxy (`liquidity=_to_float(raw.get(
"volume"))`); with the field gone, every market normalizes to
`liquidity=0.0`, failing `LIVE_MIN_LIQUIDITY` on 100% of the scan. Confirmed
still broken as of 2026-07-19 13:51 EDT via a live scan against real
`LiveTradingSettings` (0 of 5000 accepted). Nothing to fix in our trading
logic -- this was the bot correctly, safely doing nothing with no usable
data, not a bug.

The session also stopped writing to `bot.log` abruptly at 2026-07-19
02:22:19 EDT mid-cycle, with no error/warning/shutdown line and no
traceback recoverable from any log file (stdout/stderr weren't redirected
for this run). Two related gaps closed regardless of whether either was the
actual trigger:

- **`_refresh_candidates()`'s call to `select_target_markets()` had no
  exception handling**, unlike the `set_market_slugs()` call right next to
  it in the same function (fixed in `-21.`'s finding #4). Since this
  function is called directly from `run_forever()`'s main loop (which only
  catches `KeyboardInterrupt`), a transient scan failure -- exactly the
  kind of thing an upstream API actively misbehaving would produce -- could
  silently kill the whole bot process. Now caught and logged; the previous
  cycle's candidates/raw data are left untouched (not cleared) so a
  still-tradeable market isn't dropped over one failed refresh, and the
  next refresh retries.
- **No distinct signal when the scan data itself looks broken.** A scan
  that comes back with ~100% zero liquidity looked identical in the logs to
  an ordinary quiet trading day -- the only way to notice was the multi-hour
  manual investigation that found this incident. `select_target_markets()`
  now warns loudly and distinctly (`_warn_if_liquidity_data_looks_broken`,
  95% zero-liquidity threshold -- conservative enough that real illiquid-
  market noise shouldn't trigger it) whenever this happens, separate from
  the routine "no eligible markets" warning.

## -22. Performance review of the 2026-07-17 session; ORDER_INTENT_SELL_LONG lot-accounting fix (2026-07-18)

Reviewed a 12.2-hour live session (2026-07-17 17:35 -> 2026-07-18 05:48 UTC)
against the P&L/logging infrastructure built in `-18.`/`-21.`. No crashes, no
circuit-breaker or equity-protection halts; two ordinary WS reconnects,
both handled cleanly (the `-21.` stale-WS-snapshot fix visibly fired at
shutdown, exactly as designed).

**Real bug found and fixed: `lot_accounting.py`'s `_REDUCING_INTENTS` was
wrong, and was hiding real realized P&L.** It assumed
`ORDER_INTENT_SELL_LONG` reliably meant "this fill reduces a pre-existing
position," so an empty-deque fill carrying it was discarded into
`unmatched_closing_fills` instead of opening a new lot. Checked all 110
fills that assumption had produced historically: **100% carried that exact
intent, and 58 were the first-ever fill recorded for their
(market_slug, outcome)** -- i.e. selling to OPEN a fresh short (completely
normal two-sided market-making), not reducing anything. This exchange
appears to use `SELL_LONG` to mean "selling the long-side (YES) token," not
"closing a position you hold." `_REDUCING_INTENTS` is now empty (no
confirmed replacement signal exists -- deliberately not guessing at one);
every empty-deque fill is a normal new-open, matching the safety net's own
existing documented fallback for any unrecognized intent.

Impact, verified against real fills.json: total realized P&L went from
**$16.09 to $24.37** (closed lots 100 -> 157, presumed-settled/unresolved
open lots 67 -> 49) -- the earlier figure was a genuine ~34% understatement,
not a rounding difference.

This was also the ROOT CAUSE of a previously-patched, separate incident:
`multi_market_maker.py`'s constructor comment (~line 96) documents that
during the 2026-07-13/14 live run, this exact mislabeling left every fresh
short with no tracked opening lot, so the unknown-age fallback immediately
force-flattened six brand-new positions as taker orders. That was worked
around with an independent REST-snapshot-transition position-age tracker
(`_observed_position_opened_at`) rather than fixed at the source, because
the source wasn't found at the time. Left that workaround in place
deliberately (defense in depth, not redundant dead code) -- not part of
this fix's scope.

**Today's actual loss (-$0.235, fill-corrected) was 5 real trades, not
noise.** All 5 were forced exits at almost exactly the
`LIVE_LIQUIDATION_MAX_HOLDING_HOURS=1` mark, not natural exits, each losing
1 tick of spread (5c on one) plus a 2c taker fee on the forced close -- the
loss ties out exactly to the sum of those five. 3 of the 5 were
`team_to_score_prop`, which -- across its full history -- shows a clean
bimodal split: fast round trips (under ~12 minutes) are net-positive-ish
(21 lots, but see below), while every one of its six historical ~1-hour
forced exits has lost the same tick+fee combo, 0-for-6. The candidate pool
was also thin most of the day (mode of 3 ranked candidates per cycle, 352
of 2354 cycles had zero); 508 of the resulting skip messages were the same
3 recurring candidates repeatedly hitting the payoff-ratio safety cap
(20x cap, observed ratios up to 40x).

## -21. Post-Codex review fixes: exposure cap, order-version tracking, WS crash guard, force_flatten priority, settlement cross-contamination, stale-WS cancel-all (2026-07-17)

Codex made substantial changes to this codebase over roughly a week without
review. A deep multi-angle review (parallel agent passes plus direct
verification, 12 findings total) turned up real bugs on both sides -- some
mine, some pre-existing, some introduced by Codex's own changes. Fixed so
far, most-severe first:

- **Event-exposure cap could still be overshot at a flat position.**
  `market_maker.py`'s per-leg headroom clipping and
  `multi_market_maker.py`'s `_record_provisional_exposure` both gave each
  side (bid and ask) the *full* shared per-bucket budget independently,
  instead of splitting it, whenever `net_position == 0` (both sides count as
  "increasing" simultaneously at flat -- see `event_exposure.py`'s own
  documented convention). Both now compute each increasing leg's desired USD
  commitment, sum them, and scale both down proportionally if the sum
  exceeds the shared budget. This is my own bug from the exposure-cap-fix
  work in `-18.`, and Codex's own week of subsequent changes never caught
  it either -- the existing tests only ever asserted one leg's size in
  isolation, never the combined total.
- **`compute_book_aware_quote()`'s ask clamp was one-sided.** The bid had a
  two-sided clamp to `[min_price, max_price]`; the ask only clamped its
  floor, so a book near the top of the range could produce an ask above the
  absolute exchange maximum. Now symmetric with the bid.
- **`PrivateStateStore` optimistic-concurrency version tracking had gaps.**
  `upsert_local_orders()` never bumped `_order_version` at all;
  `replace_market_orders()` only bumped it when the replacement list was
  non-empty, missing the cancel-with-no-replacement case. Either gap let a
  racing, already-stale REST reconciliation silently overwrite a
  just-placed or just-removed local order. Both now bump the version
  whenever they actually mutate state.
- **An ordinary WS reconnect race could crash the whole bot process.**
  `ws_runner.py::_refresh_candidates()` called `market_ws.set_market_slugs()`
  with no exception handling, and `run_forever()`'s only handler catches
  `KeyboardInterrupt` -- an expected, ordinary send failure during a
  connection-drop/reconnect window used to propagate all the way out and
  kill the process. Now caught and logged; the next refresh retries.
- **`force_flatten` had no priority in the shared per-cycle order budget.**
  A position past its hard-flatten deadline is a mandatory-liquidation
  task, not a routine re-quote, but both the orphaned-position loop and the
  ranked-candidates loop processed slugs in arbitrary iteration order --
  a force_flatten-due position could be starved of a turn entirely by
  other, non-urgent positions ahead of it, reproducing the exact
  "inventory trapped through settlement" failure force_flatten exists to
  close. Both loops now sort force_flatten-due slugs first via a new shared
  `_force_flatten_status()` helper.
- **Settlement payout back-solving could cross-contaminate between
  outcomes on the same slug.** `get_all_positions()` (`us_client.py`) is
  keyed by `market_slug` only, with no outcome-level breakdown. When a slug
  has more than one outcome (confirmed against real fills: a two-sided
  market maker can hold both YES and NO inventory on the same slug) still
  showing open lots, `detect_settlements()` was applying that single shared
  realized/cashValue signal independently to *each* outcome's back-solve --
  attributing the same payout event to both. `settlements.py` now falls
  back to `inferred_low` (no fabricated payout) for every outcome on a slug
  whenever more than one outcome still has open lots; the ordinary
  single-outcome case is unaffected.
- **Shutdown cancel-all trusted a possibly-stale WS snapshot with no health
  check.** Both cancel-all call sites in `ws_runner.py` (process shutdown,
  and the P/L-unavailable safeguard) passed
  `private_store.open_orders_snapshot()` straight to `cancel_all()`
  unconditionally -- if the private WS was never connected or had gone
  quiet, a resting order it didn't know about could survive the cancel-all
  entirely. New `_cancel_all_resting_orders()` helper only trusts the WS
  snapshot when `PrivateStateStore.is_healthy()` says so; otherwise it lets
  `cancel_all()` fetch its own fresh REST snapshot.

- **Daily `CircuitBreaker` was missing the fill-based P&L accuracy fix the
  session breaker already had.** Codex's own prior audit (`-20.`) fixed the
  session breaker's loss check to fall back to exact signed fill cashflow
  (`session_metrics.py`'s flat-round-trip correction) whenever the
  position-endpoint delta overstated profitability -- documented as having
  actually happened in the last two live sessions -- but never applied the
  same fix to the daily breaker, which is the primary, cross-process,
  file-persisted safety net. `flat_round_trip_fill_pnl()` (renamed public
  from `_flat_round_trip_fill_pnl`) is now reused by a new
  `_daily_flat_round_trip_fill_pnl_usd()` in `ws_runner.py`, windowed by
  each fill's UTC date instead of a since-session-start fill count (the
  daily breaker's window can span multiple live-start processes, so it has
  no session-scoped baseline to slice from). `_compute_breaker_risk()`'s
  daily component now uses the same corrected figure for consistency with
  what the breaker itself just evaluated. `EquityProtection` deliberately
  left untouched -- its use of the daily figure is a profit-lock (size
  *down* once profit is hit), so an overstated figure there errs toward
  caution, not risk.

- **A fast private-WS reconnect could leave stale pre-disconnect state
  trusted for up to `private_state_reconcile_seconds` longer.**
  `PrivateStateStore.is_healthy()` reports healthy again the instant the
  socket reopens (`mark_connected()` resets `_last_message_monotonic`
  immediately), with no confirmation that the exchange replayed whatever
  deltas were missed while disconnected -- private WS protocols typically
  only stream new events going forward, not backfill. A reconnect that
  resolves faster than the bot's own refresh cadence could complete
  entirely between two quote cycles, so `_get_account_state()`
  (`multi_market_maker.py`) never observes an unhealthy sample and has no
  other trigger (periodic timer not yet due) to force a REST cross-check.
  `PrivateStateStore` now tracks the connect/reconnect transition itself
  (`reconnect_pending()`, set on every False->True `_connected` transition
  in `mark_connected()`) and `_get_account_state()`'s `reconcile_due` check
  now also forces a REST reconciliation whenever it's set, clearing it only
  once both orders and positions have been freshly, successfully
  re-fetched (a failed/partial attempt keeps retrying every cycle rather
  than silently dropping the confirmation).

- **The REST-fallback runner (`runner.py`, used when `LIVE_USE_WEBSOCKET=
  false`) had no `SessionCircuitBreaker` at all.** `SessionCircuitBreaker`
  is enabled by default and is the WS-driven runner's only in-process,
  since-this-restart loss check -- the REST-fallback path silently ran
  without it, with only the daily (UTC-midnight) breaker and equity
  protection active. `LiveTradingBot` now takes and evaluates one, mirroring
  `ws_runner.py`'s `_estimate_pnl_figures()` shape (`get_total_position_pnl_
  usd()` fetched once, both the daily-diffed and session-diffed figures
  derived from it). While in there, `EquityProtection.evaluate()` is now
  also given the already-fetched `lifetime_pnl_usd`, removing a redundant
  second positions fetch it previously made internally every cycle whenever
  `starting_capital_usd` is configured.

- **`ORDER_STATE_*` literals in `ws_private.py` are unverified against real
  data.** This can't be fixed blindly -- guessing different literal values
  without real account data would just trade one unverified guess for
  another. `_TERMINAL_ORDER_STATES` already fails safe in the ambiguous
  direction (an unrecognized value is kept as open, never dropped), but the
  opposite risk is real and silent: a genuine terminal state this set
  doesn't happen to name would leave a filled/cancelled order stuck open
  indefinitely. `_handle_order_update` now logs a one-time-per-distinct-
  value warning whenever a truly unrecognized state is seen (neither the
  terminal set nor the one non-terminal state, `ORDER_STATE_NEW`, actually
  tested so far), so a live session's `bot.log` becomes the way to actually
  verify this -- **still needs a real live session to confirm**, not
  resolved by this change alone.
- **Stale docstring in `models.py`.** `FillRecord`'s class docstring still
  claimed the private-WS execution schema was "unverified against a real
  fill," directly contradicted by its own field comments two lines below
  (commission sign convention, outcome field -- both explicitly noted as
  real-account-verified). Corrected.

## -20. Whole-bot safety and accounting audit (2026-07-10)

The post-implementation audit fixed several cross-module defects that unit
tests had not previously exercised:

- JSON state writes are atomic and concurrent in-process appends cannot lose
  rows. A ledger-write failure after posting now immediately cancels the new
  orders.
- A failed cancellation blocks replacement quotes. Missing order ids and
  ambiguous placement failures trigger a cancel safeguard instead of leaving
  untracked GTC orders.
- Position/P&L reads, malformed position schemas, and incomplete pagination
  now fail closed. Both positions and open orders paginate to completion.
- Private-state REST reconciliation cannot overwrite a newer WebSocket delta;
  successful local cancels are reflected in the cache immediately.
- BUY risk uses `price`; SELL/short risk uses `1-price` everywhere exposure
  is projected or clipped. Losing marks can no longer shrink position risk
  and reopen room to add.
- Short liquidation basis now correctly converts the exchange's complementary
  collateral cost to the original YES sale price. Short settlement payout
  back-solving uses the corresponding short equation.
- Breaker P/L retains tracked results from fully closed markets, so realizing
  a loss cannot make it disappear when the position leaves the API. Daily and
  equity peak baselines are metric-versioned to avoid mixing formulas.
- L2 and BBO freshness are separate; lightweight updates cannot keep an old
  depth snapshot tradable. Removed subscriptions force a clean reconnect,
  and reconnect backoff resets after a successful connection.

## -19. WebSocket account state + bounded liquidation (2026-07-10)

`PrivateStateStore` is now trading state, not diagnostic scaffolding. At
startup, REST bootstraps complete open-order and position snapshots. Private
order/position messages update those snapshots between reconciliations;
REST runs every `LIVE_PRIVATE_STATE_RECONCILE_SECONDS` (default 300s) or
whenever the stream is unhealthy. A healthy stream can carry the bot across
a failed periodic REST reconciliation without polling again every quote
cycle. If neither source can establish trustworthy state, quoting fails
closed; after `LIVE_PRIVATE_STATE_DEGRADED_CANCEL_SECONDS` (default 120s),
one cancel-all safeguard is sent. `MultiMarketMaker` also passes the shared
position snapshot into every per-market maker, removing duplicate
`get_position()` calls.

The absolute cost-basis exit floor has been replaced in the active quoting
path by `pricing.apply_liquidation_limit()`. Fresh positions initially keep
the same zero-loss allowance. That allowance grows linearly with the oldest
open FIFO lot's age up to `LIVE_LIQUIDATION_MAX_LOSS_CENTS` at
`LIVE_LIQUIDATION_MAX_HOLDING_HOURS`; reaching that age makes the market
reduce-only and urgent. Existing over-cap, near-resolution, and breaker-risk
states use `LIVE_LIQUIDATION_URGENT_MAX_LOSS_CENTS`. In every case,
`LIVE_LIQUIDATION_MAX_LOSS_USD` also caps total voluntary loss, so a large
position cannot consume the full per-share allowance unchecked.

## -18. Exposure-cap overshoot fix + P&L attribution system (2026-07-10)

**Why:** an external strategy review made six recommendations; two were
selected for this pass -- one a real, currently-active bug, the other the
user's own stated top priority. (The other four -- calibrated EV ranking,
replacing the cost-basis exit floor, WS-authoritative order state, shadow
evaluation -- were deliberately deferred; see the plan file for the
reasoning, preserved in conversation history rather than duplicated here.)

**Item 1: exposure-cap overshoot.** Confirmed directly in production logs:
on 2026-07-09 17:40:10 the bot posted a SELL on the Argentina-Switzerland
event bucket (`fwc-arg-sui-2026-07-11`) while the bucket was still under
its 10% stat-prop cap; it filled 17 seconds later; only the *next* cycle
did `get_all_positions()` reflect the fill and flip the bucket to
reduce-only. Root cause: the cap check compared only *settled* positions,
computed once at the top of the cycle -- it never accounted for other
resting orders in the same bucket, or the size of the order about to be
placed, and stayed static even while multiple sibling markets in the same
bucket got orders placed later in the *same* cycle.

Fix: `event_exposure.py::compute_event_exposures()` gained
`open_orders`/`order_details` params (both optional, backward compatible)
that fold each bucket's still-open, increasing-side resting orders' worst-
case USD into new `resting_worst_case_usd`/`projected_pct_of_capital`
fields. Resting-order side/price/size are resolved via the bot's own
ledger (`ledger.py::get_known_order_details()`, extended with `size`) --
deliberately NOT parsed from the exchange's own open-order response, which
has never been verified beyond `id`/`marketSlug` anywhere in this
codebase. `multi_market_maker.py` maintains a new per-cycle running tally
(`provisional_committed_usd`), seeded from the projection and incremented
as each market's increasing leg actually posts, so a later sibling in the
same bucket sees an already-reduced headroom within the same cycle --
closing the exact gap the real incident exposed. `market_maker.py` gained
`event_cap_remaining_usd`: the increasing leg is now sized DOWN to fit
remaining headroom (`market_maker.py::refresh_quotes`, right after
inventory-size-skew), not just binary-blocked once already at/over cap
(that existing `reduce_only_reason` path is unchanged and still fires once
a bucket is fully at cap). `live-event-exposure` now shows both settled
and projected figures side by side. Verified against the real account:
the Argentina-Switzerland bucket showed **11.8%** projected exposure --
the exact figure from the original incident report.

**Item 2: P&L attribution (`live-pnl`).** `family_performance.py`'s own
docstring already admitted the gap: markout is a proxy, not realized P&L,
and would need "new FIFO/weighted-average cost-lot tracking that doesn't
exist anywhere in this codebase." Built as a hybrid: fill-based FIFO lot
matching (new `live/lot_accounting.py`, "Strategy A") is the source of
truth for per-trade attribution; periodic position-snapshot diffing of the
exchange's own `realized` field (new `live/settlements.py`, "Strategy B")
is the only mechanism that can close out a position that settles rather
than being closed by an offsetting fill, since nothing in this codebase
tracks resolution outcomes and `us_client.py` exposes no payout endpoint.
The settlement detector cross-checks against Strategy A's own FIFO state
first -- a vanished position is only logged as a genuine settlement if
Strategy A, given every known fill, *still* shows open inventory.

Real-data correction that changed the schema: position identity is
`(market_slug, outcome)`, not `market_slug` alone -- confirmed real fills
exist on both the Yes-token and No-token of the same slug
(`tec-pga-genescot-2026-07-12-w-scosch`). `FillRecord` gained `outcome`
and `commission_usd` (both extracted from `raw_execution`, confirmed
present on 100% of real fills, verified to match the documented fee
formula exactly). FIFO (not weighted-average) is used specifically because
weighted-average would destroy individual open timestamps, which
capital-hours and entry-time-band attribution both need per-lot.
Settlement payout is back-solved with **three-tier confidence**: `observed`
(a clean `realized`-field jump algebraically brackets the payout, bounded
to `[0,1]`), `inferred_high` (mark-to-market proximity to 0/1, long
positions only -- `cashValue`'s sign convention for a short is ambiguous
in this API), `inferred_low` (no usable signal -- proceeds genuinely
unknown, never fabricated). New `settlements.json`/`position_snapshots.json`
files (the latter deduped to only write on an actual change, to avoid
growing an order of magnitude faster than `fills.json` already does). No
`lots.json` is persisted -- `compute_lots()` is pure and cheap enough to
recompute fresh from `fills.json` + `settlements.json` on every `live-pnl`
call, matching `compute_family_performance`/`compute_event_exposures`'s
existing "recompute on demand" pattern. `reconciliation.py` gained
`reconcile_realized_pnl()`, comparing Strategy A against Strategy B per
market with a documented open caveat: whether the exchange's `realized`
field is net or gross of commission is unverified against real data.

**Real bug caught during manual verification, before trusting the
feature:** the real `fills.json`'s 202 records predated this session's
`outcome`/`commission_usd` fields, so `live-pnl`'s first run showed
`$0.00` commission everywhere and every fill defaulting to
`outcome=None` -- not a bug in the extraction logic (which was correct),
but a missing operational step. Fixed by running the existing
`live-migrate-fills` command (already designed to re-derive fields from
each record's own preserved `raw_execution` -- this was additive to it,
not a new migration path), which backfilled both fields onto all 202 real
fills from their preserved raw data. Total realized P&L dropped from
$18.48 (commission-blind) to the correct **$17.16** (net of $-1.32
commission) -- the $1.32 difference between the two runs matches the
commission total exactly, confirming the netting logic.

**Real, honest finding, not a bug:** 81 of 202 real fills (mostly
`ORDER_INTENT_SELL_LONG`) route to `unmatched_closing_fills` rather than
being matched into a lot. Root-caused: `fills.json`'s own history starts
2026-07-06T14:20 (when the fill-persistence feature shipped -- see "-9."
section), but the bot had already been live-trading since 2026-07-04 (see
memory: real order placement confirmed 2026-07-04). Positions opened in
that ~2-day gap have no recorded opening fill in `fills.json` at all --
only their later closing SELL (once it happened after 2026-07-06) was ever
captured. This is the lot-matching engine behaving exactly as designed
("never silently misrepresent a closed position as freshly-opened risk"),
not a defect -- but it means the current $17.16 figure is real and
correctly computed, yet UNDERSTATES total realized P&L since inception by
excluding every pre-tracking-era position close. This will not recur for
new positions going forward; it's a one-time historical-depth limitation
that resolves itself as more full open-to-close cycles accumulate within
the tracked window.

**Day-1 settlement bootstrap, run manually against the real account:**
per the plan, most of the 49 real market slugs (short-lived sports props
from 2026-07-06/07) had almost certainly already resolved with zero
settlement tracking to have caught it. Confirmed: of 53 open lots per
Strategy A, only 2 still showed a real nonzero position on the exchange:
the other 51 (23 distinct `(market_slug, outcome)` keys) had vanished.
Bootstrapped all 23 as `inferred_low` settlements (proceeds genuinely
unknowable retroactively -- no snapshot history existed to back-solve
from) rather than leaving them looking like live open risk. `live-pnl`
now correctly shows `presumed_settled, unresolved proceeds: 51`.

**`live-pnl-reconcile` verified against the real account:** for the one
market with both a fresh position snapshot and closed lots
(`astatc-fwc-arg-sui-2026-07-11-g-fwcliomes-gte1`), Strategy A and
Strategy B disagreed by $-0.24 -- a real, plausible first data point for
the documented net-vs-gross-commission open caveat above, not investigated
further yet. Every other market correctly reported "no position snapshot
recorded" rather than a fabricated comparison.

650 tests passing (up from 638 at the end of the prior "-17." round --
`test_lot_accounting.py`/`test_settlements.py`/`test_pnl_attribution.py`
new, `test_event_exposure.py`/`test_multi_market_maker.py`/
`test_market_maker.py`/`test_fills.py`/`test_reconciliation.py`/
`test_dashboard.py` extended), `compileall` clean.

**Real test-isolation bug caught and fixed mid-implementation, same
pattern as the historical `fills.json`/`bot.log` contamination incidents
(see "-9. follow-up"/"-11." sections):** the first run of
`test_multi_market_maker.py` after wiring `record_position_snapshots()`/
`detect_settlements()` into `refresh_quotes()` actually created real
`position_snapshots.json`/`settlements.json` files in the live
`data/live_trades/` directory, populated entirely with test-fixture slugs
(`"orphan-slug"`, etc.) -- `config.py::load_dotenv()` loads the real
`.env` at import time, so `LIVE_POSITION_SNAPSHOT_ENABLED=true` was active
by default even in directly-constructed test settings. Caught immediately
by checking file timestamps before/after a test run; both files were
brand new (didn't exist before this session) and 100% test-derived, so
safe to delete outright. Fixed with an autouse fixture in
`test_multi_market_maker.py` isolating `fills.FILLS_FILE`/
`settlements.POSITION_SNAPSHOTS_FILE`/`settlements.SETTLEMENTS_FILE` to
`tmp_path`, mirroring `test_ws_runner.py`'s existing `_isolated_fills_file`
pattern. Lesson for future sessions, now stated three times across this
RUNBOOK: any new test exercising a real `MultiMarketMaker`/`ws_runner.py`
code path that touches a new `config.LIVE_TRADES_DIR` file needs that file
isolated from day one, not discovered after the fact.

**Restart required** for any of this to take effect -- code edits don't
affect an already-running process.

## -17. WS subscription idempotency, 429 backoff, reduce-only exit patience (2026-07-09)

**Why:** three operational/risk items from reviewing real logs and a real
incident.

1. **WebSocket subscription noise.** Logs showed repeated "slug already
   subscribed" warnings from the exchange's WS server -- `set_market_slugs()`
   resent the FULL desired list on every ~300s refresh, including slugs that
   stayed candidates and were already subscribed. Not directly costing
   money, but real API/WS churn.
2. **429 on `GET /v1/orders/open`.** The bot correctly skipped posting blind
   (existing safe behavior), but there was no retry/backoff at all -- one
   attempt, then give up for the whole cycle.
3. **Reduce-only exit patience.** A real incident: a reducing BUY (covering
   a short) on a COL/LAD market exited aggressively into what turned out to
   be a fast adverse move, looking bad ~15 minutes later. Every reducing leg
   always got the same one-tick-more-aggressive inventory-skew nudge,
   regardless of whether the exit was actually urgent.

(UFC/fight-market exposure at 13.1%, raised in the same review, is already
correctly handled by the existing event-exposure warn tier -- no code change
needed there.)

**What changed:**

- `live/ws_market_data.py`: `LiveMarketWebSocketClient` now tracks
  `_subscribed_slugs`. `set_market_slugs()` only sends the diff (slugs not
  already subscribed); a fresh connection (`_on_open`) resets the tracked
  set and does a full resend, since the server has no memory of prior
  subscriptions after a reconnect. A failed send doesn't mark slugs as
  subscribed, so the next call retries them.
- `live/us_client.py`: `_request()` gained `retryable: bool = False`. The
  three read-only methods (`get_open_orders`, `get_all_positions`,
  `get_position`) pass `retryable=True` and retry up to
  `LIVE_REQUEST_MAX_RETRIES` times with exponential backoff
  (`LIVE_REQUEST_BACKOFF_BASE_SECONDS`), multiplied by
  `LIVE_RATE_LIMIT_BACKOFF_MULTIPLIER` specifically on a 429 (honoring a
  `Retry-After` header if present). Order placement/cancellation stay
  single-attempt, fail-fast, unchanged -- retrying a write blindly risks a
  duplicate action if the first attempt actually succeeded server-side but
  the response was lost. Signed headers are regenerated fresh on **every**
  attempt -- Ed25519 signing requires the timestamp within 30s of server
  time, which a reused signature would violate after a backoff sleep.
- `live/multi_market_maker.py` / `live/market_maker.py` / `live/ws_runner.py`:
  new `reduce_urgent` flag, computed once per market per cycle as
  `over_cap OR near_resolution OR breaker_risk`. `breaker_risk` is a new
  cycle-global boolean (`WebSocketLiveTradingBot._compute_breaker_risk`,
  computed in `_run_one_cycle` right before `refresh_quotes`), true once
  today's daily P&L or the current session's P&L crosses
  `LIVE_BREAKER_RISK_WARNING_FRACTION` (default 0.75) of its respective
  circuit breaker's loss limit -- ahead of the breaker actually tripping.
  `MarketMaker` now takes `reduce_urgent` and `in_cooldown` (this market's
  toxicity-cooldown state, already tracked): the aggressive one-tick
  inventory-skew nudge on the reducing leg only applies when
  `reduce_urgent` is true; when not urgent AND in a toxicity cooldown, the
  reducing leg is skipped entirely for the cycle ("reducing patience")
  instead of exiting into what may be a fast adverse move -- directly
  targeting the COL/LAD incident. An urgent exit (over cap, near
  resolution, or breaker risk) still reduces aggressively even in cooldown.

**New settings (`.env`):** `LIVE_REQUEST_TIMEOUT_SECONDS`,
`LIVE_REQUEST_MAX_RETRIES`, `LIVE_REQUEST_BACKOFF_BASE_SECONDS`,
`LIVE_RATE_LIMIT_BACKOFF_MULTIPLIER`, `LIVE_BREAKER_RISK_WARNING_FRACTION`.

**Restart required** for any of this to take effect -- code edits don't
affect an already-running process.

## -16. Exclude esports, same-day/unknown-timing reduce-only fallback (2026-07-08)

> **Timing rule superseded 2026-07-11:** the blanket same-UTC-day portion
> described below was too broad and left the bot idle across an entire
> game-day slate. Unknown/unparseable timing still fails closed, but a known
> future event now switches to reduce-only only within
> `LIVE_PRE_EVENT_REDUCE_ONLY_MINUTES` (default 60) of its start. Already-
> started events remain reduce-only independently. The historical text below
> is retained to explain why the original guard existed.

**Why:** two follow-up guardrails after the circuit-breaker trip. (1)
Esports/Dota markets have the same "edge depends on external truth the bot
doesn't model" shape as the weather markets already excluded. (2)
Investigating the trip surfaced a real gap: when a market's raw data has no
`gameStartTime`/`endDate` at all, the time-math silently falls back to
`0.0` hours-to-event -- which incidentally made the market look "not
started" (eligible) and "near resolution" (edge widened, not reduce-only)
purely as a side effect of the fallback number landing in-range, not by
design. Unknown timing defaulted to "assume it's fine," the wrong direction.

**1. Exclude esports/Dota markets entirely.** Real raw data (full 5000-record
snapshot) confirmed this isn't just Dota -- League of Legends, Valorant, and
CS2 all show up too, spread across two org-code slug prefixes (`atc-`/
`aec-`) that are ALSO heavily used by traditional sports (soccer, baseball,
tennis, MMA), so slug-based exclusion would over-exclude. `category` is
useless too (all esports records are tagged `"sports"`, identical to real
sports). The one clean, 100%-exclusive signal confirmed against the full
snapshot: `raw["sportsMarketType"] == "esports_match_winner"`.

New `exclude_sports_market_types` setting (`LIVE_EXCLUDE_SPORTS_MARKET_TYPES`,
default `("esports_match_winner",)`) -- deliberately NOT reusing the
existing `exclude_market_types` setting, since that drives
`_market_type()`'s `marketType`-first fallback chain, and `marketType` is
always truthy for esports records (`"drawable_outcome"`/`"moneyline"`,
shared with non-esports markets), so it never reaches `sportsMarketType` at
all. Extended `market_selection.py::is_excluded_market_family()` (the
shared predicate built for weather exclusion) with a direct
`raw["sportsMarketType"]` check -- wires into both `is_eligible()` and
`multi_market_maker.py::_event_and_toxicity_gating` automatically, since
both already call the shared function.

**2. Same-day/unknown-timing reduce-only fallback.** Refactored
`market_selection.py`'s datetime parsing (`_days_from_iso` ->
`_parse_iso`, `_days_from_raw_or_end_date` ->
`_resolve_datetime_from_raw_or_end_date`, both behavior-preserving) to
distinguish "no timing data available" (returns `None`) from "computes to
exactly 0.0 hours" (a real value that happens to be zero) -- the original
float-only path couldn't tell these apart. New public
`event_or_close_datetime(market) -> Optional[datetime]`.

New `multi_market_maker.py::_is_timing_unclear_or_same_day(raw)`: `True`
(reduce-only) when raw is missing/empty, when timing can't be parsed, OR
when the event/close time falls on today's UTC calendar date. **Deliberately
fails CLOSED** on missing data -- the opposite of `_is_event_started`/
`_is_near_resolution`'s fail-open convention: if the bot has zero
information about a held position's market, it shouldn't be adding to it.
Wired into `_event_and_toxicity_gating` as a new `"same-day or unknown
timing"` reduce-only reason, reduce-only only (not wired into
`is_eligible()`, which already owns its own hard time-window cutoffs for
new candidacy).

**Real behavioral consequence, broader than expected:** flipping the
fail-open default to fail-closed for "no timing data" affected far more
tests than anticipated -- most shared test fixtures across
`test_multi_market_maker.py` used minimal `raw` dicts with no
`gameStartTime` at all (fine before, since nothing read it), so nearly
every fixture now looked like "unknown timing" and went reduce-only. Fixed
by giving the shared `_scored()`/`_scored_in_bucket()` helpers a realistic
future `gameStartTime` by default, and fixing two orphaned-position
budget tests (which specifically relied on a no-raw-data orphan consuming
its full order-budget share) to pass a resolvable timing via
`extra_raw_by_slug`. Also surfaced and fixed one unrelated latent test bug
found along the way: a warn-tier event-exposure test didn't pin
`stat_prop_max_event_exposure_pct` explicitly and was silently inheriting
the real `.env`'s value (`0.10`, lowered in an earlier risk-tightening
pass), pushing its stat-prop candidate over the cap instead of into the
warn tier it was meant to test.

**Verification:** 518 tests passing (was 502), including new coverage for
`is_excluded_market_family`'s esports signal (positive/negative, proof the
shared `atc-`/`aec-` slug prefix doesn't already catch it by accident),
`_is_timing_unclear_or_same_day` (missing/empty raw, present-but-no-timing,
unparseable, same-day, and the key test proving same-day is a real calendar
check rather than a renamed hours threshold -- false on a different day
even within a widened near-resolution window), and orphaned-position
integration tests for both features via `extra_raw_by_slug`. `compileall`
clean. Manually verified against real settings: a real Dota raw shape is
excluded, a real soccer market sharing the same `atc-` slug prefix is not.

## -15. Post-trip hardening: exclude weather, session breaker, idle-while-halted, 15m markout (2026-07-07)

**Why:** the circuit breaker tripped for real (daily P/L -$10.88 crossing
the -$8.00 limit set the day before). Investigation traced ~$6.45 of the
loss to one market: an LA-area intraday temperature-threshold market
(`tc-temp-laxhigh-2026-07-07-gte73lt74f`), where the bot kept selling YES
at a steadily rising price (0.28→0.32→0.35→0.39→0.42) over ~20 minutes as
the real-world reported temperature apparently trended toward that
bracket -- informed flow the bot has no way to price. The 1m/5m markout
windows missed this (a slow grind, not a sharp reversal), and the daily
breaker's UTC-midnight baseline made the trip's true cause (giveback of an
earlier +$7.13, not pure same-session loss) confusing to read from the log
alone.

**1. Exclude weather/temperature markets entirely.** Real raw data
confirmed (30/30 `tc-temp-*` records): `category="climate"` (not
"weather" -- Polymarket's actual tag, 100% weather-specific in this data),
`marketType="futures"` (NOT selective alone -- shared with legitimate
non-weather futures), `question` literally reads `"Highest temperature in
{City} on {Date}?"`, slug prefix `tc-temp-`. **Bug found along the way:**
`LiveTradingSettings.exclude_categories` was a bare `()` literal, never
wired through `_env_tuple` like its two siblings -- not configurable via
`.env` at all until now. Fixed, default `("climate",)`.

Three redundant layers: `exclude_categories` (fixed, `climate`),
`"temperature"` added to `LIVE_EXCLUDE_QUESTION_KEYWORDS` (the *existing*
substring-match mechanism already fully catches all 30 real records via
their literal question text -- config-only, no code change for this
layer), and a new `exclude_slug_prefixes` setting (`LIVE_EXCLUDE_SLUG_PREFIXES`,
default `("tc-temp",)` -- the one genuinely new mechanism, since
`is_eligible()` never read the slug before).

New shared predicate, `market_selection.py::is_excluded_market_family(market_slug,
raw, settings)` -- reads straight off a raw dict so it works both for a
full `ScoredMarket` (`is_eligible()`) and a bare raw dict for an orphaned
position (`multi_market_maker.py::_event_and_toxicity_gating`, a new
`"excluded market family"` reduce-only reason joining the existing
`"event already started"` mechanism). This means an *existing* held
position in an excluded market also can't get more exposure -- reduce-only,
not a hard block, it can still be exited.

**2. Session-scoped circuit breaker, separate from the daily one.** New
`circuit_breaker.py::SessionCircuitBreaker` -- measures P/L since THIS
`live-start` process began, **in-memory only, deliberately no state file**:
a baseline captured lazily on first use already gives "resets every
restart" for free. Loss limit `SESSION_CIRCUIT_BREAKER_LOSS_LIMIT_USD`
(default `$8`, matching the daily limit). Since there's no state file, the
existing `live-reset-breaker` CLI (which works by rewriting a shared file a
separate process can read) **cannot** reset this breaker -- restarting
`live-start` is the only reset path, confirmed intentional.

Avoided a redundant position-fetch: extracted `ledger.py::diff_against_baseline(total_now,
baseline_file)` out of `estimate_daily_pnl_usd` (behavior-preserving --
every existing `test_ledger.py` test passes unmodified). `ws_runner.py::_estimate_pnl_figures`
now fetches `get_total_position_pnl_usd()` **once** per cycle and derives
both the daily-diffed and session-diffed figures from it, instead of two
independent fetches.

**3. Idle the candidate-refresh loop while any breaker is halted.**
`run_forever()`'s market-scan/WS-resubscription call used to run on its own
independent 300s timer, oblivious to halt state -- noisy and wasteful once
halted, though not dangerous (quoting itself was already correctly gated).
New `_maybe_refresh_candidates()`/`_any_breaker_halted()`: skips the scan
entirely while any of the three breakers (daily, session, equity
protection) is halted, and forces an immediate refresh on the cycle a halt
clears (rather than waiting up to 5 more minutes for the timer). `_run_one_cycle()`'s
own per-cycle work (fill persistence, markout computation, all `.evaluate()`
calls) is untouched -- those must keep running regardless of halt state, per
their own existing "must never interrupt" comments.

**Test-infra fix required, easy to miss:** `test_ws_runner.py`'s mock
breakers never configured `is_halted.return_value` -- an unconfigured
`Mock().is_halted()` returns a truthy `Mock`, so every fixture-built bot
would have looked permanently halted once this gating existed. Fixed in
the shared `_bot()` fixture.

**4. Added a 15-minute markout window** (uniform, not family-aware, not
until-settlement -- both explicitly deferred as bigger, separate scope).
`fills.py::_MARKOUT_WINDOWS` gained a `("15m", 900.0)` entry;
`find_due_markout_windows` gained an optional `windows=` override
(`ws_runner.py` is the one caller that overrides the duration from a new
`LIVE_MARKOUT_LONG_WINDOW_SECONDS` setting, default 900.0). Required
parallel edits (the schema is hardcoded to named field pairs, not a
generic dict): `models.py::FillRecord`, `fills.py::build_fill_record`/`migrate_legacy_fills`,
`family_performance.py`'s `FamilyPerformance` + aggregation, `dashboard.py`'s
two render functions. `toxicity_tracker.py` needed zero changes -- it only
ever consumes the `"1m"` window via an explicit gate in `ws_runner.py`.

**5. Lower `LIVE_MAX_ORDERS_PER_CYCLE` during testing -- already done**
(`=4` in the real `.env`, set the day before). No changes needed.

**Verification:** 502 tests passing (was 466), including new coverage for
`is_excluded_market_family` (all three layers, fail-open behavior, a
regression test using the real incident's slug/category/question),
`SessionCircuitBreaker` (mirrors `CircuitBreaker`'s test shape plus
`diff()`-specific cases), `_maybe_refresh_candidates`/`_any_breaker_halted`
(each halt source, forced-refresh-on-resume, timestamp-only-updates-on-actual-refresh),
and the 15m markout window end to end. `compileall` clean. Manually ran
`live-status` (shows the new session-breaker caveat line) and
`live-family-performance` (shows new 15m columns, "-" for all rows since no
fill has aged 15 minutes since this feature shipped) against the real
account data without crashing.

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

**6. Private WebSocket -- historical scaffold note (superseded 2026-07-10).**
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

`live-reset-breaker` is an explicit real-money risk override. It records the
raw daily P/L at the trip as today's reset baseline, then applies one fresh
configured loss allowance to subsequent movement. Without that baseline a
reset performed while raw UTC-day P/L remained below the limit would simply
re-trip on the next cycle. The baseline is ignored automatically after the
UTC date changes. Repeated resets therefore require repeated confirmation and
never silently erase the historical P/L shown in reporting.

## 7. Rate limits

20 requests/second per API key (authenticated) and per IP (public); a
"5-second stopgap" on new orders/modifications that isn't a hard limit but
can return a similar-looking error. The default 15-minute refresh interval
is far under any of these, so this shouldn't bind in normal operation --
just don't lower `LIVE_REFRESH_INTERVAL_SECONDS` drastically without
checking.

## 8. Inventory-risk hardening (2026-07-12)

The bot no longer treats a one-sided fill as permission to continue making
both sides of that market. With `LIVE_FLAT_FIRST_INVENTORY_ENABLED=true`, a
nonzero position blocks the inventory-increasing leg until the reducing leg
fills and the account is flat again.

Inventory becomes a mandatory exit task at the earlier of:

- `LIVE_LIQUIDATION_MAX_HOLDING_HOURS` (currently 1 hour), when
  `LIVE_HARD_FLATTEN_ON_MAX_HOLDING_ENABLED=true`; or
- `LIVE_HARD_FLATTEN_MINUTES_BEFORE_EVENT` (currently 90 minutes before the
  known event/close time). Unknown timing fails closed and is due now.

The mandatory exit is a marketable LIMIT at the visible best opposing price:
long positions sell at the best bid and short positions buy at the best ask.
It deliberately bypasses quote-entry depth, volatility, edge, payoff-ratio,
and historical-cost loss guards. Those controls must not trap inventory until
binary settlement. If the required opposing price is unavailable, the bot
cancels inventory-increasing orders, preserves an existing reducing order,
and logs an error instead of inventing an execution price.

Held positions, including held markets still present in the ranked candidate
list, receive order-budget priority over flat markets. New entries face a 20x
maximum worst-payoff-loss-to-captured-spread ratio and, from flat, both sides
must pass together: the bot will not turn an asymmetric guard failure or a
one-slot remaining order budget into a directional opening order. The selector
uses the same static paired-entry check so blocked markets do not monopolize
the WebSocket candidate pool. The daily and session loss stops are both $3 and
the persistent peak-equity drawdown stop is 5%.

### Fill/position settlement race hardening (2026-07-12/13)

The 2026-07-12 live run exposed a non-atomic private-state race. A filled
marketable reducing order disappeared from the private open-order snapshot
before the corresponding position update arrived. Ten seconds later the bot
still saw the old position, repeated the full reducing order, crossed through
flat, and opened the opposite position. Across the two affected sessions,
fills 230:252 produced exact flat-round-trip cashflow of -$1.5908 even though
the position-endpoint session journal incorrectly reported +$0.9218.

Controls added from that evidence:

- Every new submission starts `LIVE_ORDER_SETTLEMENT_SECONDS` (15 seconds).
  The market cannot act again until both open orders and positions have been
  refreshed authoritatively through REST. Reconciliation failure extends the
  pause; it never fails open to another order.
- Observed fills force that REST reconciliation immediately.
- Mandatory marketable liquidation limits are IOC, so unfilled remainders
  expire instead of resting.
- `LIVE_ONE_ROUND_TRIP_PER_MARKET_PER_SESSION=true` prevents repeated churn in
  the same market after fills return its position to flat. This is a session
  cooldown, not a permanent market blacklist.
- Probation sizing is 1-2 shares and two paired markets (four order legs) at
  a time. The log calls these `placed_order_legs` so `4/4` is not mistaken
  for four independently selected markets.

Accounting was also corrected: the exchange field
`commissionNotionalCollected` is positive when the exchange collects a fee
and negative for a rebate. Net P/L therefore subtracts the signed field. The
old code added it and inverted both fees and rebates. Session metrics now use
exact signed fill cashflow whenever the session's fill deltas are flat, label
their source, and feed the more conservative result into the session breaker
instead of allowing the position-endpoint proxy to mask a realized loss.

### Fresh-position age correction (2026-07-13/14)

The 2026-07-13 20:20 UTC through 2026-07-14 04:04 UTC session completed six
flat round trips and lost exactly $0.23. Every passive SELL entry was followed
2-75 seconds later by an IOC BUY at the same or a worse price. All six passive
entries had favorable one-minute markout (average +4.0 cents/share), so the
loss was caused by exit handling rather than those entry selections.

The exchange labeled each flat-to-short execution `ORDER_INTENT_SELL_LONG`.
FIFO P/L accounting conservatively treats that intent as a possible close of
inventory whose opening predates fill tracking. The live holding-age fallback
then saw a nonzero exchange position with no FIFO opening lot, treated it as
maximum-age inventory, and force-flattened it immediately as a taker.

Live liquidation age is now independent of that P/L-classification ambiguity.
The multi-market runner observes trustworthy account position transitions:

- The first snapshot is only a baseline. Inventory already present when the
  process starts remains unknown-age unless FIFO history can date it, so the
  conservative startup behavior is unchanged.
- A later flat-to-nonflat transition, or a position sign flip, records a known
  monotonic in-session opening time regardless of the exchange intent label.
- Partial reductions or additions on the same side do not reset the clock.
- When both FIFO history and the transition tracker know the age, the older
  age wins so this correction can never extend a real holding deadline.

A regression test runs the original flat -> passive SELL -> authoritative
short-position sequence with max-holding flattening enabled and requires the
next reducing BUY to remain GTC. A separate existing test requires genuinely
unknown startup inventory to remain IOC-flattened.

## 9. Autostart on login (unattended mode) — set up 2026-07-04

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

## 35. Schema-v4 portfolio observation and guarded pilot (2026-07-30)

Observation now writes `data/live_trades/market_observations_v4.json`.
Existing v1-v3 files remain diagnostic archives and are never counted toward
qualification. An incompatible file encountered at the v4 path is copied to
a schema-labelled diagnostic archive before fresh evidence starts; a newer
unknown schema fails closed instead of resetting evidence.

The same 100-market L2/trade feed drives two independent shadow portfolios:

- `legacy`: widest spread, five markets/10 legs, 17.5 shares, spread up to
  98c, and six hours in-play. It is permanently shadow-only.
- `controlled`: activity-adjusted spread, five markets/10 legs, one share,
  spread up to 50c, three markets per event, and three hours in-play. Entries
  pause for the final pregame hour, reopen at kickoff, stop in the last
  30 minutes, and stop after two round trips per market. Inventory is forced
  out after one hour or at the three-hour in-play deadline.

Both profiles keep separate quotes, queue-ahead state, fills, inventory,
fees/rebates, and P/L. Qualification is portfolio- and cohort-level, includes
open-inventory marks, forced exits, drawdown and event concentration, and
ends after a fixed 48 hours. It never starts real trading.

Reports:

```powershell
python -m polymarket_bot.main live-observation-report --profile controlled
python -m polymarket_bot.main live-observation-report --profile legacy --json
```

Only a controlled `PASS` with fresh, configuration-matched evidence unlocks:

```powershell
python -m polymarket_bot.main live-pilot-start
```

The pilot still requires the ordinary typed confirmation and credentials,
crash recovery, a verified-flat account, both WebSockets, maker-only paired
entries, and all fail-closed execution safeguards. It is fixed at one share,
10 legs/five markets, qualified cohorts only, two round trips per market,
and $3 daily and session loss limits. New entries stop after 3.5 hours,
followed by a 30-minute passive drain and mandatory force-flat verification.
The result is written to `pilot_results.json`; acceptance never scales or
starts another run automatically.

## 36. V4 model revision 2: resting-quote lifetime and event identity (2026-07-31)

The first schema-v4 production observation ran for nine hours and received
9,250 L2 snapshots plus 67 real tape trades, but classified every tape trade
as outside an admissible quote. Two model defects made that evidence
incompatible with the intended strategy:

- Shadow quotes inherited `LIVE_WEBSOCKET_STALE_AFTER_SECONDS=10`, although
  both portfolio profiles refresh every 60 seconds and a kept exchange order
  remains resting during a quiet-book interval. At tape time the observer now
  re-evaluates the portfolio allocation and models an unchanged scheduled
  refresh as keep-in-place: quote liveness is renewed without resetting its
  queue priority. A newly allocated quote joins the currently displayed
  queue; an inactive quote cannot consume queue or create pseudo-fills.
- The US market-list response currently omits `eventId`. Observation now uses
  `derive_event_bucket_key()` when that happens, the same tested event-slug
  inference used by live exposure controls. Sibling props therefore share
  the three-markets-per-event cap, distinct-event count, and concentration
  calculation.

Tape records now persist quote presence, age, active/depth flags, prices, and
queue-ahead at each trade. Portfolio reports include total books, tape trades,
qualifying trades, eligible quote-hours, and rejection-funnel counts.

This is observation model revision 2. Revision-1/untagged v4 evidence is
archived as diagnostic-only and never mixed into qualification. The next
`live-start` creates a fresh 48-hour deadline; yesterday's nine hours do not
count.

## 37. V4 model revision 3: restart-safe inventory and feed integrity (2026-08-03)

The revision-2 evaluation recorded useful controlled evidence before its
market feed stopped advancing: 60 completed one-share round trips across
eight events, three qualifying cohorts, +$4.09 realized P/L, +$0.79 including
open-inventory marks, and a -$1.53 maximum drawdown. It was not a valid PASS
or strategy FAIL. After restart, REST candidate scans continued, but the L2
and tape evidence stayed frozen while 14 controlled shadow positions remained
open. The legacy comparison made the consequence especially visible: its
realized gains were overwhelmed by hundreds of dollars of open-inventory
losses.

The root cause was subscription ownership. Once an event aged beyond the
profile's entry window, its markets fell out of the broad observation
universe even when a shadow portfolio still held inventory there. The process
could therefore scan indefinitely without receiving the books required to
model its mandatory exits.

Revision 3 closes that lifecycle gap:

- Every slug with inventory in any legacy/controlled shadow variant takes
  WebSocket subscription priority until all of its shadow positions are flat.
- A non-empty observation universe that produces no valid L2 book for
  `LIVE_OBSERVATION_FEED_STALE_AFTER_SECONDS` (300 seconds by default) aborts
  visibly instead of continuing REST scans without queue/tape evidence.
- Valid L2 minutes are persisted across restarts. At the fixed deadline,
  healthy feed coverage must be at least
  `LIVE_OBSERVATION_MIN_FEED_COVERAGE_RATIO` (90% by default), and the feed
  must be current. Interrupted or stale evidence is `INSUFFICIENT`, never a
  strategy `FAIL` and never a pilot unlock.
- Before qualification, the runner explicitly finalizes both portfolios by
  sweeping available bounded-age L2 depth for every shadow strategy. Missing
  books and unresolved inventory are recorded in the report. Qualification
  cannot PASS unless finalization ran and every primary and variant shadow
  position is closed.

The interrupted revision-2 file remains diagnostic-only. These changes alter
the simulated lifecycle and qualification evidence, so revision-2 evidence is
not mixed with revision 3; the next `live-start` begins a fresh 48-hour clock.

## 38. V4 model revision 4: current-market ordering and fail-fast safety (2026-08-03)

The next observation run repeatedly scanned 5,000 records, recovered L2 data
for 500 through the fallback, and still watched zero markets. This was not a
quiet market or another strategy failure. The gateway's default market order
was returning old, long-dated listings first, so a finite 5,000-record scan
never reached the newly-created sports listings that form the usable intraday
opportunity set.

Direct read-only API comparison identified the exact contract mismatch:
`orderBy=createdAt&orderDirection=desc` (the spelling shown in the API docs)
did not change the old-first response, while the backend's snake_case
`orderBy=created_at&orderDirection=desc` returned the newest listings first.
`PolymarketClient.get_markets()` now sends that working ordering explicitly.
The default scan size and three-day observation horizon were not widened to
paper over the bug.

Revision 4 also closes the ways this failure could waste another day or be
mistaken for evidence:

- An empty observation candidate universe is now unhealthy. If it remains
  empty for `LIVE_OBSERVATION_FEED_STALE_AFTER_SECONDS` (300 seconds by
  default), the observer exits with a visible error. Repeated empty refreshes
  cannot reset that timer.
- The listing-order contract is part of the persisted qualification config.
  Revision-3 evidence is archived diagnostic-only and cannot unlock a pilot.
- Controlled shadow and pilot execution now use the same pure lifecycle rule
  for the exact pregame pause, kickoff reopening, final-30-minute cutoff, and
  three-hour in-play deadline boundaries.
- Deadline shadow liquidation retries a missing/failed L2 snapshot up to three
  times before persisting unresolved inventory as `INSUFFICIENT`.
- Daily, session, and equity breaker cancellation now re-fetches open orders.
  A remaining order or failed verification raises
  `EmergencySafeguardFailedError` after the halted state is persisted, so the
  process exits fail-closed rather than trusting best-effort `cancel_all()`.
- The guarded pilot settings and acceptance cross-checks have integration
  coverage: ordinary environment tuning cannot widen its one-share/10-leg
  envelope, and acceptance still requires real fills, a completed round trip,
  compatible five-minute markouts, no breaker breach, and verified-flat
  shutdown. Automatic scaling remains forbidden.

The observer process already running when these edits were made has the old
revision-3 code loaded in memory and receives none of these changes. Stop it
normally with Ctrl+C, then start once more to begin revision-4 evidence. A
revision-4 process that again finds no usable markets will terminate in about
five minutes instead of running silently for days.

## 39. Quiet-book false stop: heartbeat health and bounded recovery (2026-08-04)

The first revision-4 run successfully watched 100 markets for about 13 hours,
recording 1,911 tape trades and eight controlled round trips across six
events. At 00:35 it refreshed the candidate subscriptions and received an
initial book, then no further book change for 307 seconds. Revision 4 treated
that alone as a dead feed and stopped at 00:40, even though an event-driven
book can legitimately remain unchanged while the socket continues exchanging
heartbeats. This was an over-strict health check, not a strategy failure.

Polymarket's WebSocket documentation explicitly defines periodic heartbeat
messages as connection liveness and recommends monitoring them/reconnecting
when they stop. Schema-v4 minor revision 1 therefore separates two concepts:

- Feed health and coverage advance from a market-socket heartbeat or other
  non-error market message. A quiet unchanged book no longer causes a false
  crash.
- L2 freshness remains strict and separate. Heartbeats never update a
  market's `last_book_epoch`, cannot make an old book tradable, and cannot
  satisfy the bounded-book requirement for deadline inventory liquidation.
- If both heartbeats and market messages really stop for five minutes, the
  runner requests a clean socket reconnect and full resubscription. It makes
  two bounded recovery attempts, allowing five minutes after each. Only then
  does it raise `ObservationFeedStalledError` and stop fail-closed.
- Heartbeat coverage is persisted at most once per new minute, avoiding a
  large observation-file write on every heartbeat.

This is an additive, evidence-preserving migration within model revision 4:
every historical minute containing a valid book was necessarily a healthy
connection minute. The eight existing controlled round trips, original start
time, and August 5 11:31 ET evaluation deadline remain valid and are resumed
on the next `live-start`; no 48-hour reset is required.

## 40. One-time continuation for interrupted revision-4 evidence (2026-08-06)

The revision-4 window expired with 15.53 healthy feed hours (32.36% of the
fixed 48-hour target), eight controlled round trips across six events, and an
incomplete deadline sweep because fresh L2 books were unavailable for every
remaining inventory slug. Restarting after the expired deadline correctly
stopped immediately as `INSUFFICIENT`, but requiring a brand-new uninterrupted
48-hour run would discard compatible evidence and repeat time already spent.

`live-observation-continue --hours 30` now provides one bounded exception for
this evidence set:

- It preserves all revision-4 books, trades, fill queues, inventory, P&L,
  markouts, event counts, and healthy-minute buckets. It does not create a new
  schema/model revision and does not weaken any pilot gate.
- The original 48-hour coverage denominator remains fixed. Extending the
  operational deadline does not make the percentage requirement easier or
  harder; the observer still needs at least 43.2 healthy hours (90% of 48).
- Arming does not start the 30-hour clock. The deadline is moved only after a
  subsequently started observer has a non-empty candidate universe and
  receives confirmed market-WebSocket activity. A failed startup consumes no
  continuation time.
- The failed deadline-finalization record is cleared only upon activation, so
  the extended deadline performs a fresh inventory sweep. A prior partial
  sweep is rejected because resuming a partly finalized portfolio would mix
  incompatible lifecycle evidence.
- The operation can be armed only once, only after an expired
  `INSUFFICIENT` window, and only while the exclusive live-instance lock is
  available. Restarting an active continuation resumes its existing deadline;
  it never adds another 30 hours.

The controlled profile must still reach 20 completed round trips, five events,
one qualifying cohort, positive net P&L, profit factor 1.20, nonnegative
five-minute markout, drawdown no worse than $3, event concentration at most
50%, complete inventory finalization, and the original feed-coverage gate.
The legacy profile remains shadow-only and no pilot starts automatically.

## 41. Healthy-feed completion and fresh-book finalization (2026-08-07)

The 30-hour continuation ended `INSUFFICIENT`, but the controlled strategy did
not fail its quality tests. It reached 14 completed round trips across nine
events, one eligible cohort, $1.5628 realized shadow P&L, +2.803c average
five-minute markout, a -$0.7101 maximum drawdown, and 38.3% event profit
concentration. It remained mechanically incomplete because only 28.33 of the
required 43.2 healthy feed hours were collected and the process was stopped
for a large part of the wall-clock extension.

The result also exposed a second sequencing bug. When `live-start` was invoked
after the expired deadline, the main loop tried to finalize immediately while
the asynchronous 5,000-market scan was still running. The persisted inventory
slugs had not yet received subscription priority or fresh initial L2 books, so
the sweep necessarily recorded every remaining position as unresolved.

`live-observation-complete` repairs both mechanics without resetting evidence:

- It preserves the existing 28.33 healthy hours, 14 round trips, fills,
  independent shadow portfolios, cohorts, P&L, markouts, and inventory.
- Completion is based on the unchanged 43.2-hour healthy-feed requirement
  (90% of the original 48-hour target). Only unique minutes containing a
  confirmed heartbeat or market message count. Stopping the process or losing
  the feed pauses progress instead of consuming a wall-clock allowance.
- The observer still stops at the fixed healthy-time target even if it has not
  reached 20 round trips or profitable quality thresholds. It cannot wait for
  a favorable result and does not weaken any pilot requirement.
- At the target, all new legacy and controlled shadow entries and queued quotes
  are frozen. Existing lifecycle exits may still consume fresh books, but no
  additional positions can be opened after the measurement endpoint.
- Finalization waits up to 10 minutes for the background scan to subscribe all
  persisted inventory markets, then requests one clean market-WebSocket
  reconnect so quiet markets deliver fresh initial books. It sweeps only after
  every open-inventory market has a bounded-age, two-sided L2 book. If books
  are still unavailable after the bound, it records an incomplete result
  rather than using stale marks.
- The completion mode is one-time and restart-safe. Re-running the maintenance
  command cannot add more evidence allowance, while ordinary process restarts
  resume the same remaining healthy-minute count.

The guarded pilot remains locked until finalization is complete and every
original controlled gate passes. Nothing transitions to live trading
automatically.

## 42. Settlement-based finalization for markets no book can ever revisit (2026-08-09)

The revision-4 healthy-feed window that finished around 2026-08-08 23:20
local passed every controlled sample and quality gate on its own terms
(43.37 healthy hours, 22 completed round trips, 16 distinct events, one
qualifying cohort) but left 17 primary shadow positions (39 slugs counting
every strategy variant, across both profiles) unfinalized: at the
measurement endpoint none of them had an obtainable order book, real or
REST. The displayed +$3.11 total P&L included stale marks on that open
inventory and was not trustworthy.

`finalize_evaluation()`'s book-based sweep (`_force_exit()`) cannot close
these no matter how much retry/reconnect logic exists (`38.`-`41.` already
hardened every book-*freshness* failure mode) -- these specific markets'
underlying events had already happened, so there is no book left to fetch,
ever. The fix uses a different, authoritative signal instead: the market's
actual settlement outcome.

Two public, unauthenticated endpoints, always checked together:

- `GET /v1/markets/{slug}/settlement` -> the payout, a decimal in `[0,1]`
  (not always binary). A 404 means genuinely not settled yet -- this is
  never retried, since retrying a 404 can't produce a different answer.
- `GET /v1/market/slug/{slug}` -> nested `market.closed`/`market.status`,
  confirming resolution (`closed=true`, `status="MARKET_STATUS_RESOLVED"`).
  `active` is deliberately ignored -- resolved markets still report
  `active=true`.

Every slug's lookup lands in exactly one of three buckets (both endpoints
are always checked, even when settlement itself 404s, since the verdict is
a function of both together): `SETTLED` (both agree), `UNRESOLVED` (a
clean, expected "not yet," settlement 404s and metadata confirms it's
still open), or `ERROR` (anything else, including the two endpoints
disagreeing with each other, or metadata itself 404ing/malformed). A batch
containing any `ERROR` aborts entirely before any mutation, real or
projected -- one ambiguous slug must never produce a partially-corrected
archive.

Closing a resolved position needed its own exit path (`_settle_at_resolution`),
distinct from `_force_exit`: no book depth to walk, the whole remaining
size of every `SHADOW_STRATEGIES` variant closes in one synthetic fill at
the settlement price, tagged `liquidity_role="settlement"` and a new
`closure_type="settlement"` field (distinct from the pre-existing
`exit_reason`/`forced_exit` machinery, which now explicitly excludes a
settlement close -- it's an authoritative real-world resolution, not a
defensive book-based liquidation, and conflating the two would misreport a
strategy-quality problem that isn't there). `commission_usd=0.0` is a
deliberate, documented assumption (no order executes at settlement under
the published fee model), recorded as such in the settlement audit
provenance, not just asserted silently in code.

This surfaced a real, separate accounting bug along the way: three
independent fill-price gates (`_paper_position_state`, `_round_trip_records`,
`_paper_round_trip_stats`) all rejected a fill priced at exactly `$0`/`$1`
-- correct for an ordinary trade, wrong for a settlement fill, whose whole
point is representing that exact payout. Naively appending a settlement fill
would have been silently dropped by at least one of the three, leaving the
position looking permanently open. Centralized into one shared
`_fill_price_is_valid()` (ordinary fills keep the strict `0 < price < 1`;
a `liquidity_role="settlement"` fill gets the inclusive `0 <= price <= 1`
instead) rather than patching three independent copies -- the same class of
drift `38.` already had to close once for the entry-window calculation.

`live-observation-settle` (new CLI command) is the standalone way to run
this without restarting observation or resetting any clock -- following the
`live-observation-continue`/`live-observation-complete` precedent (`40.`/
`41.`) of maintenance commands that operate on already-persisted evidence.
No credentials needed (both lookup endpoints are public). Holds the
instance lock for the full run. Resolves every stuck slug before mutating
anything. Dry-run by default, printing the full projected qualification
verdict and settlement-attributable P&L; `--apply` is required to persist,
which writes a timestamped backup first (aborting if the backup itself
fails) and then exactly one atomic write. The no-op decision compares the
full projected state against what's currently persisted, excluding only the
settlement audit log's own always-fresh timestamps -- a rerun that settles
nothing new but still needs to correct stale blocker-list bookkeeping is a
real, backed-up write; a rerun where genuinely nothing changed writes
nothing at all.

Verified via full suite (1038 tests, up from 1029) + `compileall`, and the
highest-stakes pieces (the ERROR-aborts-before-any-mutation guarantee, and
the settlement price-bound exception) via revert-and-confirm-failure. Also
caught and fixed one real bug during testing: the "preserve the original
book-based missing-slugs list" logic used `or` against a possibly-empty
list, which silently re-derived and drifted that field on every rerun
instead of keeping it fixed after the first write -- fixed to check key
presence explicitly.

Run against the real archive: dry-run first, hand-verify the settlement
values for a sample of the stuck slugs against their known real-world
outcomes, then `--apply` once satisfied. See the report afterward for the
corrected controlled verdict.

**Corrected result**: `status=FAIL`, `total_pnl=-$0.8368`. The apparent
+$3.11 was entirely an artifact of stale marks on the 39 unfinalized
positions. Broken down: +$1.3725 from previously completed trades, then
-$2.2093 from the 17 primary positions closing through settlement instead
of a live exit. The strategy captures small maker edge on individual fills
but gives it back on inventory it couldn't exit before the underlying event
resolved -- a real, structural loss mechanism, not noise. The pilot remains
locked. See `43.` for the follow-up attribution/replay pass this motivated.

## 43. Offline attribution/replay over the settled archive -- corrected, and inconclusive (2026-08-09)

`42.`'s corrected result diagnosed the failure mechanism: small maker edge
captured on individual fills, given back on inventory that couldn't exit
before the underlying event resolved. This pass replays revised rules
against evidence already on disk to test whether a different exit/entry
policy would have changed that outcome -- no new observation window, no
network calls, and the module (`live/observation_replay.py`) never mutates
the archive it reads.

**A first version of this section, published earlier the same day, was
wrong and has been fully replaced.** It concluded "replay-negative -- no
variant qualifies" from a search deadline that was actually kickoff, not
the deadline production ever enforced; an entry-filter sweep that pooled
every market's trade tape together; and language ("no counterparty at any
price") that the data cannot support. All three are fixed below; the
corrected result is materially different and considerably more honest about
what can and can't be concluded.

**Framing, corrected**: every fill this module replays against -- including
every number in `42.`'s -$0.8368 baseline -- is a **persisted hypothetical
shadow fill**, including synthetic closes tied to authoritative settlement
payouts. None of it is a real exchange execution. The only genuinely real
data here is the trade tape itself (actual observed market prints). A
printed trade that would have crossed a resting order is an **optimistic
upper bound** -- there is no historical queue position in this archive, so
a crossing print does not prove the bot's full order size would have filled
ahead of other resting interest, and it says nothing about whether an
aggressive taker order could have crossed existing liquidity without
producing a new print. Aggressive-taker feasibility is **always reported as
`UNKNOWN`** -- never inferred, never assumed negative.

### What was actually wrong

1. **The search deadline was kickoff, not the real risk deadline.** The
   archived `event_or_close_epoch` is genuinely kickoff (confirmed by
   tracing `market_selection.event_or_close_datetime()`, which prioritizes
   `gameStartTime`). But production's actual forced-exit deadline is
   profile- and phase-specific, reconstructed from
   `MarketObservationTracker._record_due_forced_exits()` and its per-profile
   settings overrides: legacy has no per-position deadline mechanism at all
   except an outer book-based flatten at **kickoff + 6h**; controlled caps a
   pregame entry at `min(entry + 1h, kickoff)`, and an in-play entry at
   `min(entry + 1h, kickoff + 2.5h)` (masking logic in `record_book()`
   suppresses the near-event trigger until that 2.5h in-play entry-cutoff
   boundary). For any in-play entry -- which dominates the controlled
   cohorts -- the old `not_after_epoch=kickoff` was already at or before the
   entry itself, an impossible search window before a single trade could
   ever qualify. This alone produced the old `29/30`/`17/17` "no
   opportunity" counts.
2. **The entry-filter sweep pooled every market's trades and closing time
   together.** A trip's "trailing activity" could be counted from an
   unrelated market, and "hours remaining" measured against the
   latest-closing trip in the whole portfolio. Fixed by precomputing both
   fields per trip from that trip's own market only, before the sweep ever
   runs.
3. **Reaching an activity endpoint didn't rule out a gap in between.** The
   observation run's overall healthy-feed coverage was ~90%. Even after
   fixing (1), concluding "no crossing was observed" from an endpoint being
   reached doesn't rule out a real crossing having happened inside an
   unobserved connection gap. Every "no crossing" conclusion now
   additionally checks the persisted global `feed_minute_buckets` for gaps
   across the full required window before calling it complete.
4. **Multi-entry trips were silently mispriced.** A trip whose position was
   built from more than one entry fill only ever had the *first* fill's
   price/quantity attached. Real, non-degenerate legacy trips are built this
   way. Those trips are now excluded from priced counterfactuals entirely
   (`unknown_multifill`) rather than mispriced.

### What the corrected replay actually does

Every row from the escalating-exit replay (rule 1) and the hard
pre-settlement-exit check (rule 3) now resolves to one of four states, never
a bare boolean:
- `passive_trade_cross_observed` -- a real trade printed that would have
  crossed our resting order (an optimistic bound, not a guaranteed fill).
- `passive_trade_cross_not_observed` -- no such print, **and** the archive's
  coverage of the full required window (endpoint reached, no feed-minute
  gaps) is complete. The only state that is an honest, complete negative.
- `unknown_incomplete_window` -- no print found, but the evidence window was
  cut short (observation ended before the real deadline, or a feed gap
  exists inside the window). Absence proves nothing here.
- `unknown_no_post_entry_anchor` / `unknown_multifill` -- no post-entry
  activity signal exists at all for that market, or the trip can't be
  honestly priced (see above).

A report-level verdict block separates what can and can't be concluded per
(profile, strategy):
- `passive_replay_status`: **`REJECTED`** only when every relevant trip's
  optimistic full-fill total (baseline P&L plus every observed crossing's
  fee-netted optimistic delta) is non-positive **and no row is in any
  unknown state**. **`INCONCLUSIVE`** otherwise -- including when the
  optimistic total is positive, since that doesn't establish the policy
  works either (no size/queue guarantee). **`NOT_APPLICABLE`** when a
  (profile, strategy) had no forced-exit or settlement trip to test the
  policy against at all, so a negative baseline is never misread as "this
  policy was tried and rejected."
- `taker_replay_status`: always the constant `UNKNOWN`.
- `pilot_unlock_authorized`: always the constant `false`. This is a
  read-only replay; it does not own the pilot gate. Actual pilot status is
  sourced from `live-observation-report`, not from here.

Revised cohort qualification (rule 4) now also enforces the **same
5-round-trip/2-distinct-event sample floor the live qualification gate
itself requires** (`observation_cohort_min_round_trips`/
`observation_cohort_min_distinct_events`), on top of the existing
profit-factor/drawdown/settlement-exit-rate bars -- a single-round-trip
cohort can no longer read as "eligible." The entry-filter sweep grid is now
bounded to each profile's actual achievable runway (controlled: 0-1h,
matching its 1h max-holding cap; legacy: 0-4h) and swept as a full Cartesian
product of time x same-market trailing-trade-count, rather than paired
tuples that confound the two.

Verified via 63 observation-replay-related tests (59 module-level, 4 CLI --
up from 25), revert-and-confirm-failure on all four fixes above (each one
individually disabled, confirmed the corresponding new test fails, restored),
and a real-archive run with the archive's SHA-256 checksum confirmed
unchanged before and after. Full suite: 1101 passing, up from 1063.

### Corrected findings against the real, settlement-finalized archive

Controlled profile, primary `improve_both` strategy (the one actually
eligible for a pilot): baseline unchanged at 39 round trips, net P&L
-$0.8368, profit factor 0.794 -- matching `42.` exactly, as expected (the
bug was in the replay, not the baseline).

The escalating-exit replay's 30 examined trips (every forced/settlement
close) now break down as **7 `passive_trade_cross_observed`, 7
`passive_trade_cross_not_observed`, 14 `unknown_incomplete_window`, 2
`unknown_no_post_entry_anchor`** -- a completely different picture from the
old `29/30 no opportunity`. The hard pre-settlement-exit check, over the 17
settlement-closed trips, found **9 `passive_trade_cross_observed`, 6
`passive_trade_cross_not_observed`, 2 unknown** -- directly contradicting
the old claim of "17/17, no counterparty at any price." The optimistic
full-fill total across controlled/`improve_both` is -$0.5187 (versus the
-$0.8368 baseline) -- still negative, but **`passive_replay_status` is
`INCONCLUSIVE`**, not `REJECTED`, because unknown-state rows are present.
Every other (profile, strategy) combination is `INCONCLUSIVE` too --
`passive_replay_status` never reaches `REJECTED` anywhere in the archive,
because incomplete-coverage rows exist in every single one.

Two findings worth flagging as directional, not dispositive: the
market-scoped, corrected entry-filter sweep shows a much larger swing than
the old (buggy) one did -- requiring even 1 trailing same-market trade
before entry flips controlled/`improve_both` from -$0.84/PF 0.794 (39 kept)
to +$0.53/PF 1.49 (17 kept), and tighter thresholds keep improving from
there (5 trailing trades: +$0.83/PF 3.71, 10 kept). This is a real, honestly
computed correlation in the existing data, but it is a small, filtered
sample chosen after the fact along one dimension -- not independent
confirmation, and not something this replay pass is positioned to validate
further. Separately, the previously-"qualifying" cohort
(`controlled|props|in_play|10-25c|normal`) still fails the revised bar (10
round trips, 9 events, net P&L -$0.6674, PF 0.651, 50% settlement-exit
rate), while the *closest* cohort to qualifying
(`controlled|props|in_play|25-50c|normal`: 11 trips, 9 events, PF 1.198,
just under the 1.20 bar) fails specifically on a 63.6% settlement-exit rate
well above the 20% cap -- consistent with `42.`'s diagnosis that settlement
exposure, not raw edge, is the recurring problem.

### Verdict

**Corrected, not replay-negative -- inconclusive.** The prior "no variant
qualifies" conclusion was itself an artifact of the same class of bug this
whole investigation exists to catch: an evidence window that looked
complete but wasn't. With the deadline, market-scoping, and coverage bugs
fixed, no (profile, strategy) combination reaches a clean `REJECTED` verdict
-- but none reaches anything resembling "qualifies" either.
**`INCONCLUSIVE` is not a green light.** Per the standing criterion for this
work, only a *replay-positive* revised profile would warrant another bounded
shadow test, and nothing here is replay-positive: every strategy's honest
result is either a negative optimistic bound or an unresolved evidence gap,
and aggressive-taker feasibility remains permanently `UNKNOWN` from this
archive regardless of outcome. The pilot remains locked
(`pilot_unlock_authorized=false` throughout); no fill parameters were
loosened; observation mode was not restarted. The entry-filter-sweep signal
above is worth keeping in mind for a *future*, properly-designed test --
not acted on from this pass alone.

## 44. Third observation profile: July 5 real-activity style, with risk guards active (2026-08-09)

`43.`'s replay-negative-turned-inconclusive result closed the door on tuning
the *current* strategy's exit/entry rules further. Separately, "reconstruct
the profitable July 5 bot from historical account orders and fills" turned
out to have already been investigated: `data/reports/july5_old_bot_reconstruction.md`
(2026-07-30), built from a full, read-only `GET /v1/portfolio/activities`
export (`data/reports/account_activity_2026-07-01_to_2026-07-11.json`, via
`scripts/export_account_activity.py`) covering 2026-07-01 through 07-11 --
the only way to see 07-04-07-06 at all, since local `fills.json` genuinely
has no data before 2026-07-06T14:19 UTC (confirmed against the raw
pre-migration backup). That document's own finding undercuts the
"profitable" premise: July 5-6's real combined net result was **+$0.33**
(bot-automatic fills +$87.46, offset by -$87.13 from *manual* liquidation
of bot-accumulated inventory -- the same stranded-inventory pattern `42.`/
`43.` re-diagnosed in the current bot), heavily concentrated in one lucky
trade (+$52.33 on a single fast-moving market). Its own conclusion
explicitly rejected a blind rollback: "first observe and simulate the old
in-play, wide-spread, 10-leg universe, while assigning every eventual
liquidation and settlement back to the bot-created inventory. Only after
that full-lifecycle result is positive should the same entry behavior be
tested live." This section is that simulation -- reusing the observation/
replay infrastructure `35.`-`43.` already built and just fixed, not new
historical-reconstruction work (the account-activity export has already
been mined as far as it can go; the one thing it can never recover is L2
depth/queue history, which stays out of scope here per `43.`'s own
taker-feasibility disclosure).

**What July 5's real style was**, per the existing analysis: wide-spread
(39 of 170 quoting cycles exceeded 30 cents, including the standout
profitable market -- a 30-cent cap would exclude exactly the regime that
mattered), in-play entries allowed, no pregame pause, 5 markets/10 legs per
refresh batch, GTC limit orders sized 17-17.5 shares (median 17.5), 97.6%
passive maker fills.

**The deliberate correction versus a blind rollback**: the existing
`legacy` profile already matches July 5 on spread/size/timing, but
`_legacy_settings()` also hardcodes `extreme_price_low_threshold=-1.0`,
`extreme_price_high_threshold=2.0`, `max_payoff_loss_to_capture_ratio=1_000_000.0`
-- disabling the extreme-price/payoff-shape guards entirely (confirmed
directly in `_flat_entry_prices()`: with those overrides, its `_allowed()`
check never rejects a leg). `43.`'s replay of the real archive showed
`legacy` losing heavily (-$193 to -$217 across 3 of 4 strategies),
dominated by catastrophic tail losses in a `drawable_outcome|pregame`
cohort -- exactly the unguarded territory those overrides leave open,
territory July 5's real sample never validated. The new profile,
`july5_style`, keeps those guards **active** at normal base-config values
(0.15/0.85/4.0c/20.0) -- isolating whether July 5's broad market selection
and wide spread ceiling worked on their own, without also restoring all of
`legacy`'s unbounded inventory risk.

**Verified limit of what the guards actually bound**: they cap *relative*
risk (max loss versus captured spread), not absolute price extremity. At
`july5_style`'s 0.98 spread ceiling, a market quoted around 1c/98c still
passes both legs' check -- the ~97c captured spread swamps both the
extreme-price-min-edge test and the payoff-ratio test (confirmed by direct
computation and by `TestJuly5GuardsRemainActive::test_wide_extreme_price_market_still_passes_the_guards`).
**The guards reduce tail risk here; they do not eliminate it.**

### What was built

1. **One canonical `ObservationProfileSpec` per profile** (`market_observation.py`)
   -- everything that defines a profile's behavior (size, spread, timing,
   ranking method, entry guards, exit rules, fee coefficients, the shared
   5-market allocation pool, and the profile's identity), built once from
   that profile's settings and persisted with a SHA-256 hash into
   `state["profiles"][profile]["spec"]`/`"spec_hash"`. On every
   restart, the spec is rebuilt fresh and compared by hash against what's
   persisted; any mismatch raises `ObservationSpecMismatchError` **before
   any other state mutation** -- evidence collected under different trading
   parameters can never be silently mixed into one archive. Applied to all
   three profiles, not just the new one.
2. **A companion `QualificationPolicy`**, persisted once at archive creation
   (`state["qualification_policy"]`/`"_hash"`) -- every threshold the
   follow-up gate below checks, plus `primary_strategy`, frozen at the
   moment the archive was created. A later change to the live
   `observation_controlled_*`/cohort constants can never retroactively
   change the verdict already-collected evidence produces; a policy drift
   on restart logs a warning (it doesn't affect what was actually traded)
   rather than raising, unlike a spec drift.
3. **Model revision 5, a fresh archive, and a cumulative, restart-safe
   clock.** `OBSERVATION_FILE` is now `market_observations_v5.json`;
   `market_observations_v4.json` is never touched, read, or moved by this
   change and remains exactly where it is as `42.`/`43.`'s completed
   diagnostic record. A fresh archive now initializes directly in
   `evaluation_completion_mode="healthy_feed_target"` (48 *confirmed*
   healthy-feed hours, not the wall-clock deadline that previously burned
   down even while the process was stopped) rather than the old default
   `wall_clock` mode -- reusing the tracker's own pre-existing
   `healthy_feed_target` completion check (`_evaluation_complete_locked()`),
   previously reachable only retroactively via `arm_healthy_feed_completion()`.
   Restart-safety verified directly: accumulated `feed_minute_buckets`
   survive a restart with 10+ hours of elapsed wall-clock downtime in
   between, un-consumed.
4. **`PROFILE_JULY5_STYLE`** (`"july5_style"`), added to `OBSERVATION_PROFILES`.
   New settings `observation_july5_max_started_event_hours` (6.0, matching
   `legacy`), `observation_july5_max_spread` (**0.98**, matching `legacy`'s
   ceiling -- not a tighter ~20-30c cap, which would have excluded the
   regime that actually mattered), `observation_july5_order_shares` (17.5,
   matching the real median). New `_july5_settings()` builder: a
   near-verbatim copy of `_legacy_settings()`'s risk-timing overrides, with
   **no** extreme-price/payoff-ratio overrides at all -- the one deliberate
   difference from `legacy`.
5. **Every legacy-vs-controlled binary branch made explicit three-way**,
   no implicit fallback: `record_book()`'s event-epoch masking (`july5_style`
   pops the field like `legacy`, never masked like `controlled`),
   `observation_replay.py::_risk_deadline_epoch()` (the single most
   consequential fix -- `july5_style` uses the no-holding-cap formula, not
   controlled's 1h-cap formula), and the `hours_grid` selection. Spread
   ceiling and in-play-cutoff-hours lookups collapsed from three-way
   conditionals into a single `self._profile_specs[profile].<field>`
   lookup, now that the spec is the shared source of truth.
6. **One-share-equivalent reporting, honestly labeled.** `july5_style`
   trades in uniform 17.5-share lots, unlike `controlled`'s 1 share.
   `revised_cohort_rows()` and `run_replay()`'s baseline now report both the
   raw dollar figures and a `_one_share_equivalent_usd` pair (linear
   scaling by the profile's own `order_shares_max`, sourced from its
   persisted spec) -- explicitly named and documented as a **linear
   estimate, not proof of actual 1-share execution behavior** (queue
   position, partial fills, and available depth can differ at a smaller
   clip size, and this archive can't verify that). Cohort eligibility
   itself now checks the *normalized* drawdown against the $3.00 bar,
   fixing a real bug in an earlier draft of this change that would have
   compared `july5_style`'s raw 17.5x-scaled drawdown against a threshold
   calibrated for 1-share economics, wrongly disqualifying nearly every
   cohort.
7. **A strengthened follow-up gate**, `profile_follow_up_status()`, bound
   to the archive's frozen `policy["primary_strategy"]` only -- never
   scanned across all four `SHADOW_STRATEGIES` and reported for whichever
   looks best, which would be post-hoc strategy selection layered on top of
   the exact "one lucky trade" concentration problem the real July 5 data
   already showed. Requires, all at once: complete finalization, adequate
   healthy-feed coverage, >=20 round trips across >=5 events, positive
   one-share-equivalent net P&L, profit factor >=1.20, one-share-equivalent
   drawdown within $3.00, event concentration <=50%, at least one
   `revised_eligible` cohort, a nonnegative 5-minute markout sample, **zero
   open inventory in both the primary portfolio and every shadow variant**
   (checked independently), portfolio-wide settlement-exit rate <=20%, and
   `passive_replay_status != REJECTED`. Reports `FOLLOW_UP_CANDIDATE` or
   `NOT_YET_QUALIFIED` with every unmet reason listed -- never "eligible,"
   never anything readable as trading permission.
8. **Offline replay stays purely archive-driven.** `observation_replay.py`
   still never imports or constructs `MarketObservationTracker` and never
   calls `config.load_settings()`. Since `profile_follow_up_status()` needs
   summary-style fields (open inventory, healthy-feed hours, markout
   samples) that previously only existed on the live tracker's
   `profile_summary()` method, a new, small, purpose-built pure function --
   `_profile_completion_summary(state, profile)` -- computes them directly
   from the loaded archive dict instead (not a refactor of the large,
   real-money-adjacent `profile_summary()` method, a deliberately
   lower-risk choice).
9. **Permanently shadow-only, unchanged.** `profile_summary()`'s existing
   controlled-only branch already covers any new profile automatically --
   `july5_style` is `SHADOW_ONLY`/`pilot_unlocked=False` by construction,
   and `live-pilot-start` remains entirely independent of
   `OBSERVATION_PROFILES`. No code change was needed for this.
10. **CLI**: `live-observation-report --profile july5_style` now valid;
    its deadline line is conditional on `evaluation_completion_mode` and
    never presents the wall-clock deadline as controlling completion in
    `healthy_feed_target` mode. `live-observation-replay` now prints raw and
    one-share-equivalent figures, the follow-up verdict and every blocked
    reason, and remaining healthy-feed hours, in both text and JSON.

### Verification

51 net new tests plus updates to existing ones for the new default
completion mode and signatures (1152 total, up from 1101), including:
per-profile
spec/policy construction and hashing; fail-closed mismatch (tampered
settings on restart raise `ObservationSpecMismatchError` before any
mutation, confirmed via checksum); restart-safety across simulated downtime;
the extreme-price/payoff-guard contrast between `july5_style` and `legacy`,
including the wide-extreme-price (1c/98c) case; normalized cohort
eligibility (a case whose raw drawdown fails the $3 bar but whose
one-share-equivalent drawdown passes it, and the reverse); every
`profile_follow_up_status()` condition tested independently, including that
it's computed only for the frozen primary strategy even when a non-primary
variant would "look better"; and that `observation_replay.py` genuinely
never constructs a tracker or reads live settings. Revert-and-confirm-failure
on the two most consequential structural fixes: the event-epoch-masking
branch (verified via a spy on the child tracker's `record_book()` call,
since `july5_style` sharing `legacy`'s `hard_flatten_on_max_holding_enabled=False`
means this bug has no forced-exit-based side effect to observe -- the only
way to catch it is inspecting state during the call itself) and
`_risk_deadline_epoch`'s three-way branch. `compileall` + full suite clean.
`data/live_trades/` files (including `market_observations_v4.json`)
checksummed identical before and after the complete test run.

### Explicitly not done in this change

No observation run was started against real market/account data --
`market_observations_v5.json` does not exist outside test fixtures. No
modification to `_guarded_pilot_settings`, `cmd_live_pilot_start`, or
`live/confirmation.py`. No widening of `legacy`'s or `controlled`'s existing
settings. The bounded L2 recorder for taker-exit-feasibility remains
deferred. Starting the actual 48-confirmed-healthy-feed-hour `july5_style`
observation window against real market data is a separate, later,
explicitly-confirmed action.

## 45. Separate unqualified July 5-style one-share live pilot (2026-08-09)

The dedicated command is:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.main live-pilot-start-july5
```

This is a **REAL-MONEY, qualification-bypassing command**. It is separate
from `live-pilot-start`; the original command remains controlled-profile and
qualification-gated. `live-pilot-start-july5` prints both the controlled gate
and the `july5_style` replay follow-up as informational evidence, then
continues even if they are missing or negative. It deliberately sets both
`observation_gate_enabled=False` and `pilot_qualification_bypassed=True`, so
neither the startup qualification decision nor the per-cycle
`entry_eligible()` filter can quietly turn it back into the controlled pilot.

The command is a **hybrid opportunity-set test, not a reconstruction of the
lost July 5 bot and not evidence of profitability**. Its fixed opportunity
set is: widest raw-spread ranking, up to a 98-cent spread, no pregame entry
pause, up to six hours in-play, five markets/10 legs, and no activity-based
re-ranking. WebSocket trade activity remains subscribed and recorded for
attribution; disabling re-ranking does not disable the tape. The normal
extreme-price/payoff guards remain fixed at 15c/85c, 4c minimum extreme edge,
and 20x maximum loss-to-captured-spread ratio. These are relative-payoff
guards, not a 15c-85c absolute entry band: a sufficiently wide market around
1c/98c can still pass.

The retained safety envelope is: verified-flat startup, crash recovery,
private WebSocket state, maker-only paired entries with unpaired-leg unwind,
flat-first inventory, one share per leg, at most three markets per event,
at most two round trips per market, one-hour maximum holding, and the
existing execution backfill/fail-closed safeguards. Both daily and session
loss breakers are forced to $3. Those are **reactive thresholds, not a hard
maximum-loss guarantee**: fills, repricing, liquidity disappearance, fees,
and force-flat execution can carry realized loss beyond $3 before the bot is
flat. No worst-case dollar loss is promised.

The pilot stops new entries after 3.5 hours, spends up to 30 minutes draining
inventory, then force-flattens and verifies that no position or resting order
remains. A failure to verify flat raises `PilotFlatnessError`. No result ever
changes order size or unlocks scaling automatically.

This command has its own exact confirmation phrase and always forces
`unattended_mode=False`, even if the ordinary environment enables unattended
startup. It defines no `--yes` or `--force` flag. A held daily breaker, dirty
startup account, missing private feed, failed crash recovery, or failed
flatness verification still blocks or terminates it.

Pilot attribution is session-scoped. The fill-index and wall-clock markers
default to `None` and are assigned only after candidate and WebSocket setup,
immediately before the run loop. A setup failure before then is recorded as
`pilot_status=NOT_STARTED` and never falls back to fill index zero or epoch
zero. A started pilot compares only its new real fills with primary-strategy
shadow markouts from `pilot_strategy_profile` (`july5_style` for this command)
at or after the same session marker. Synthetic settlement fills are excluded.
If either side has no five-minute samples, the comparison is `UNAVAILABLE`,
not a match. Every result persists `qualification_bypassed`, the pilot and
shadow profile names, comparison sample counts/status, verified-flat state,
breaker state, and `automatic_scaling_permitted=false`.

Implementation verification added focused coverage for the fixed settings,
qualification bypass, mandatory distinct confirmation, halted-breaker stop,
fully mocked command startup, trade-tape-without-rerank behavior, per-cycle
gate bypass, session/profile-scoped markouts, missing-sample handling, and
pre-marker `NOT_STARTED` attribution. No real pilot was launched by the
implementation or its tests.

## 46. Four fixes from the first real `live-pilot-start-july5` run (2026-08-10)

The first real run of section 45's pilot completed mechanically cleanly (950
quote cycles, verified-flat shutdown, fresh reconciliation confirming zero
open orders/positions, no breaker breach), but surfaced one adverse round
trip and three operational problems. This section documents both what was
found and the four fixes made in response -- none of which change the
pilot's fixed opportunity set, size, timing, or loss limits from section 45.

**1. Fast in-play repricing.** A resting $0.40 bid filled immediately before
its market moved roughly 42 cents within one 60-second refresh cycle; the
existing `VolatilityTracker` guard only checks price movement once per
market per `refresh_interval_seconds`, so it could only react at the next
cycle boundary, after the fill. Category-based exclusion (e.g. blocking
tennis while in-play) was considered and rejected as not currently buildable
-- section "-16. Exclude esports, same-day/unknown-timing reduce-only
fallback" already found that neither slug prefix nor `category` cleanly
isolates one sport from another (both esports and tennis share the same
`atc-`/`aec-` slug prefixes and `category="sports"`), and no equivalent
clean signal exists for tennis specifically; building one would require a
fresh raw-snapshot investigation, not something to do speculatively inside a
fix. Instead, `WebSocketLiveTradingBot` now runs a second, much
cheaper check between full quote-refresh cycles: `_check_fast_repricing()`,
invoked via `_wait_with_fast_repricing()` at `fast_reprice_check_seconds`
(default 5.0s) instead of the full `refresh_interval_seconds` (still 60s,
unchanged) whenever `fast_reprice_enabled` is on. It only ever cancels a
resting order whose quoted price has drifted beyond `max_recent_move_cents`
from the current BBO -- it never places a new order -- and reads only
in-memory WS-fed state (`private_store.open_orders_snapshot()`,
`store.get_market_bbo()`), so it makes no additional REST calls. Both pilot
settings-builders set `fast_reprice_enabled=True`; it defaults `False` and is
untouched for ordinary `live-start`.

**2. Toxicity-cooldown sizing was shrinking the exit leg, not the entry
leg.** After the adverse fill above, the bot offered only 0.5 shares to exit
a 1-share position. Root cause: `MultiMarketMaker._effective_settings_for()`
shrank `order_shares_min`/`order_shares_max` uniformly by
`toxicity_size_multiplier` during a cooldown, and that shared, side-blind
pair became `base_order_shares` for *both* legs in `MarketMaker.
refresh_quotes()`. Since `reduce_only_reason` already fully blocks the
increasing leg during the same cooldown, the shrink's only real effect
landed on the reducing/exit leg -- backwards. Fixed by making the discount
side-aware: `_effective_settings_for()` no longer touches `order_shares_min`/
`order_shares_max` for `in_cooldown`/`event_exposure_warn` (only
`min_edge_cents` widening remains there); a new `_increasing_order_shares_
for()` computes the discounted figure separately and both `MarketMaker(...)`
construction sites pass it through a new `increasing_order_shares`
constructor parameter. `MarketMaker.refresh_quotes()` now computes a
separate `reducing_base_shares` (always the full, undiscounted
`order_shares_min`/`max` average) and `increasing_base_shares` (the override
if set, else the same full average), and picks per BUY/SELL leg using the
same `is_increasing` condition already used elsewhere
(`_resolve_leg_price`). The existing `min(shares, abs(net_position))`
reducing-side cap now binds against the true position size again, matching
how the hard-flatten path already sized exits at `shares = abs(net_position)`
with no multiplier at all.

**3. Candidate scanning wasn't stopping during the pilot's drain phase.**
`run_forever()`'s loop called `_maybe_refresh_candidates()` unconditionally
every iteration, before `drain_only` was even computed later in the same
iteration. Each firing can issue up to `liquidity_fallback_max_lookups`
(default 500) L2-orderbook REST calls on a background thread, competing with
shutdown's `verify_flat_for_observation()` reconciliation calls for the same
account-wide rate-limit budget -- contributing to real 429s during the
actual run's shutdown. `_run_one_cycle(drain_only=True)` was already correctly
cheap (`candidates=[]` is passed explicitly, so no scan happens there); the
new `_pilot_in_drain()` method now guards the loop's
`_maybe_refresh_candidates()` call directly, skipping it entirely once the
pilot's entry window has elapsed. No other retry/backoff logic changed --
`LiveUsClient._request()`'s existing 429-aware backoff and
`_finish_pilot_flat()`'s own 3-attempt outer retry were already reasonable;
the fix removes the contention, not the handling of it.

**4. The shadow-comparison sample was empty because of an allocation
mismatch, not order size.** The real run's `comparison_status` came back
`UNAVAILABLE`. The original hypothesis was that the `july5_style` shadow
profile's 17.5-share `observation_july5_order_shares` book-depth check was
blocking hypothetical fills a 1-share real order would have cleared -- this
was checked directly against `book_has_enough_depth()` and is **wrong**:
depth admissibility uses fixed absolute thresholds
(`min_top_depth_shares`/`min_total_depth_shares`), identical across the real
pilot and every shadow profile; the 17.5-share figure only affects recorded
fill *quantity* bookkeeping, never admissibility. The actual cause:
`MarketObservationTracker._refresh_allocations()` independently selects each
profile's own active-market subset (capped at `observation_profile_max_
markets`, default 5) from the broader shared candidate pool -- not
necessarily the same markets the real pilot's live ranking is actually
quoting that cycle. Combined with `session_shadow_markouts()` requiring a
fill's `markout_5m_cents` to already be backfilled (at least 5 minutes old),
and only 2 real fills in the whole session, a real/shadow overlap was
already a narrow ask independent of any bug. Fixed with a new
`MarketObservationTracker.override_profile_allocation(profile, slugs)`
method that pins a profile's active allocation to an exact set, bypassing
`_refresh_allocations()`'s own ranking for that profile only (the pin check
runs unconditionally, ahead of the normal due-for-refresh gate, and is
re-applied every time `_refresh_allocations()` runs so it can't be
overwritten by a later, unrelated `record_book()` call). `_run_one_cycle()`
now calls it every cycle outside drain, with the real bot's current
candidate list, whenever `pilot_mode` is on -- covering both pilot commands
via `pilot_strategy_profile`, not just `july5_style`. Wrapped in its own
try/except (diagnostics/evidence-quality only, must never block trading).
**This does not guarantee a MATCH/MISMATCH verdict on the next run** --
with typically very few real fills in one bounded 4-hour/1-share session,
`UNAVAILABLE` may still be common; that is a sample-size reality of a
deliberately bounded pilot, not something the allocation fix alone resolves,
and running longer to collect more samples would contradict the
one-more-bounded-run recommendation below.

**Recommendation after these four fixes**: one more bounded 4-hour, 1-share
`live-pilot-start-july5` run -- not a return to multi-day observation, and no
increase in size. The first run validated execution and shutdown plumbing;
it did not, and still cannot after these fixes alone, provide positive
strategy evidence on its own.

New/changed tests: `_check_fast_repricing()`/`_wait_with_fast_repricing()`
cancel-trigger and dual-cadence behavior; `_increasing_order_shares_for()`
and the now-unshrunk `_effective_settings_for()` behavior, plus an
end-to-end regression test reproducing the exact 1-share/0.5-offer incident
(with revert-and-confirm-failure against the pre-fix code); `_pilot_in_drain()`
and a real `run_forever()`-loop test proving `_maybe_refresh_candidates()`
runs normally before drain and is skipped once draining (also
revert-and-confirm-failure verified); `override_profile_allocation()`
pinning/capping/persistence and `_run_one_cycle()`'s pilot-mode wiring,
including that a pin failure never blocks `refresh_quotes()` (also
revert-and-confirm-failure verified). No real pilot was started by this
work or its tests.

## 47. Two of section 46's four fixes were themselves wrong, and corrected (2026-08-10)

A review of section 46's implementation, before any real pilot was started
again, found the fast-repricing and shadow-allocation fixes both had real
bugs of their own. No money was exposed -- the flawed code was never run for
real -- but neither is fit to approve for another pilot until corrected.
Both are now fixed and independently revert-and-confirm-failure verified.

**1. Fast-repricing compared the wrong price to the wrong price.**
`_check_fast_repricing()` compared a resting order's own price against the
current book's *midpoint* -- wrong for a market maker resting near one edge
of a wide book, which is exactly what this pilot's `max_spread=0.98`
opportunity set allows. A completely unchanged 39c-bid/85c-ask book has a
62c midpoint; a 40c resting bid would look like it "moved" 22 cents and get
cancelled on a fully static market. It also examined every account order
with no ownership check at all -- a manual order, or one from a different
strategy sharing the account, could have been cancelled. Fixed two ways:
(a) every order is now looked up in `get_known_order_details()` (the same
ledger-backed ownership source `multi_market_maker.py`'s own stale-order
sweep already uses) before being touched at all -- unrecognized orders are
left alone, fail-closed; (b) the comparison is now directional and
side-specific -- a BUY order's price is compared against the current
`best_bid`, a SELL against `best_ask`, never the midpoint. Cancels are also
now grouped per market: if any bot-owned leg on a market needs cancelling,
every bot-owned leg resting on that market is cancelled together, so a
paired entry can never be left one-sided by a fast cancel that only touched
the drifted side.

**2. The shadow-allocation fix still didn't reflect what the real maker
did.** Two compounding problems. First, `_run_one_cycle()` pinned the
observation tracker's allocation from the *pre-execution candidate list*
(`_get_candidates()`'s output), not the outcome of `refresh_quotes()` --
`refresh_quotes()` can skip a candidate entirely (depth, edge, budget,
reduce-only) or post an order for an orphaned held position that was never
in the candidate list at all, so the candidate list is neither a superset
nor a reliable proxy for what actually got quoted. Fixed by capturing
`refresh_quotes()`'s own `list[LiveQuoteCycle]` return value and pinning
only `cycle.market_id` where `cycle.bid.is_resting or cycle.ask.is_resting`
-- `PostedLeg.is_resting` (`order_id` set and `size > 0`) is the existing,
authoritative "does this leg actually have live exposure right now" check,
already used elsewhere in the codebase. Second,
`MarketObservationTracker.override_profile_allocation()` converted its
input to a `set` before capping at `observation_profile_max_markets` (5) --
when more than 5 markets were pinned, *which* 5 survived depended on
Python's hash-based set iteration order, not the caller's priority order,
so the shadow tracker could silently end up watching different markets than
the real maker actually posted to even with the source fixed. Fixed by
storing an order-preserving, de-duplicated tuple instead, and slicing it
directly for the cap -- deterministic, and faithful to whatever order the
caller provided.

**Separately, but related:** both pilot commands' dedicated observation
trackers (`MarketObservationTracker(pilot)` in `main.py`, for both
`cmd_live_pilot_start` and `cmd_live_pilot_start_july5`) were still pointing
at the shared `OBSERVATION_FILE` -- the same long-running, multi-day
archive used for controlled-profile qualification evidence. A pilot run's
session-scoped shadow evidence was being interleaved into that canonical
archive rather than isolated to its own session. Each pilot command now
constructs its tracker with its own dedicated `path`
(`PILOT_OBSERVATION_FILE` / `JULY5_PILOT_OBSERVATION_FILE`, both in
`market_observation.py`, both distinct from `OBSERVATION_FILE` and from
each other). The one-time, purely informational `pilot_start_eligible()`
gate check at the top of each command still reads the ordinary
`OBSERVATION_FILE` (it's checking the long-running qualification evidence,
not creating a pilot session) -- only the tracker actually passed into
`WebSocketLiveTradingBot` uses the dedicated file.

New/changed tests, all revert-and-confirm-failure verified against the
pre-fix code: `_check_fast_repricing()` regression tests for a wide,
completely unchanged book (the exact false-positive scenario), an
unrecognized/manual order being left untouched, and a paired entry being
cancelled together rather than left one-sided; `_run_one_cycle()` pinning
from `refresh_quotes()`'s actual returned cycles rather than pre-execution
candidates, including a market that was never a candidate at all (an
orphaned position); `override_profile_allocation()`'s cap following the
caller's priority order (same five markets, reversed order, produces a
different surviving pair) rather than arbitrary set order, plus
de-duplication; and a command-level test confirming
`cmd_live_pilot_start_july5` constructs its bot's tracker with
`JULY5_PILOT_OBSERVATION_FILE`, not `OBSERVATION_FILE`. No real pilot was
started by this work or its tests.

**Recommendation, unchanged from section 46**: once these two blockers are
corrected and tested (done here), the appropriate next step is one more
bounded four-hour, one-share July 5-style pilot -- no observation run, no
increase in size.

## 48. Final fast-repricing safety correction before the second pilot (2026-08-10)

A final review found that section 47's fast check still read the public BBO
without removing this bot's own resting liquidity. That made the guard look
correct in a synthetic book where the bot's quote was absent, but ineffective
in the important real shape: if the bot's 40c bid remained the public top
level while the next external bid collapsed to 19c, it compared 40c against
itself and detected no move. The ordinary 60-second quote cycle had already
removed ledger-recognized bot quantities before pricing. That logic is now a
shared `book_without_bot_orders()` helper, and the five-second check requires
the same fresh full-L2 external-book view. A lite BBO is deliberately
insufficient because it cannot reveal what sits behind the bot's top level.

The trigger is now adverse-direction-only rather than an absolute distance:
a BUY cancels only when its price is more than the configured guard above the
current external bid, and a SELL only when it is more than the guard below the
current external ask. Favorable movement leaves a less-aggressive resting
order alone instead of creating avoidable cancel/repost churn.

Finally, cancelling two paired legs with sequential HTTP requests is not
atomic. After any drift-triggered cancellation group, the runner performs one
authoritative open-order fetch. If a leg survived, or verification itself is
unavailable, it invokes the existing account-wide `cancel_all_and_verify()`
safeguard. Failure to prove a clean slate raises
`EmergencySafeguardFailedError` through the runner and stops the process.
The private in-memory order store is only cleared after exchange verification.
Thus the normal five-second check remains REST-free; REST is used only after a
real cancellation trigger, where proving both legs are gone is load-bearing.

Regression tests cover bot-owned top-level masking, favorable movement, and a
partial paired-cancel failure that remains visible even after the emergency
safeguard. No live command was started and no live-trade data was modified.

## 49. Section 48's verification poll itself crashed a real pilot (2026-08-10)

Section 48's "REST is used only after a real cancellation trigger" claim was
too broad: it fired on *every* drift-triggered cancellation group, regardless
of whether every individual `cancel_order()` call in that group had already
succeeded. The second real `live-pilot-start-july5` run confirmed this is a
real problem, not a theoretical one. From `logs/bot.log`:

```
21:33:37 WARNING - Fast repricing: cancelled bot-owned resting order BSW337R00BAE for astatc-mlb-tb-ath-...-gte4 (side-specific target moved beyond the 3.00c guard).
21:33:38 WARNING - Fast repricing: cancelled bot-owned resting order BSW3043S2BAJ for astatc-mlb-tb-ath-...-gte4 (side-specific target moved beyond the 3.00c guard).
21:33:52 ERROR   - Could not verify fast repricing cancellation for astatc-mlb-tb-ath-...-gte4: GET /v1/orders/open failed: 429 Too Many Requests; using the fail-closed account-wide safeguard.
21:34:06 ERROR   - Live trading process crashed unexpectedly: fast repricing verification for astatc-mlb-tb-ath-...-gte4: could not verify account-wide cancellation: GET /v1/orders/open failed: 429 Too Many Requests
```

Both legs' own `cancel_order()` calls succeeded cleanly -- no exception, no
doubt. The unconditional post-cancel `get_open_orders()` poll still fired,
hit a 429 (the account was already under load from a concurrent
settlement-barrier reconciliation retry a minute earlier), and escalated to
`cancel_all_and_verify()` -- whose own mandatory verification also hit a
429, raising `EmergencySafeguardFailedError` uncaught and crashing
`run_forever()` roughly 19 minutes into a planned 4-hour session. Nothing
was left exposed: the `finally:` block's own shutdown-flat verification
fought through the same backlog and confirmed flat before exiting. The
fail-closed posture worked; the verification step that triggered it was
pure overhead in this specific incident, re-checking something that was
never in doubt.

Fix: the post-cancel REST verification (and its escalation) now only fires
when at least one leg's own `cancel_order()` call actually raised. If every
leg in the group cancelled cleanly, the runner trusts it -- removes those
order ids from `private_store` locally and moves on -- exactly the same
trust signal `market_maker.py::_cancel_existing_orders()` already relies on
elsewhere with no follow-up REST check at all. This is consistent with
Polymarket's own API documentation: the single-order cancel endpoint
returns `200 {}` on success, the Orders API overview recommends tracking
order state via the private WebSocket rather than REST polling, and the
account is subject to a global 20 req/s limit shared across every endpoint.

Note also (verified against `us_client.py` while investigating this):
`cancel_order()` does not retry. `LiveUsClient._request(..., retryable:
bool = False)` defaults to a single attempt; only the three explicit read
methods (`get_open_orders`, position lookups, order-by-id lookup) pass
`retryable=True` and get the 3-attempt/429-aware backoff described in
section "-17. WS subscription idempotency, 429 backoff, reduce-only exit
patience". A `cancel_order()` call that raises is therefore already a
concrete, immediate reason for doubt -- exactly the condition this fix now
gates on -- not something that needs its own additional retry logic.

Section 48's own guarantees are otherwise unchanged and still hold: the
external-book-aware, adverse-direction-only, bot-owned-only comparison, the
paired-group cancellation, the `cancel_all_and_verify` escalation mechanics,
and `EmergencySafeguardFailedError` propagation on genuine, unresolved doubt
are all untouched. A cancellation that itself fails still takes the full
verify-and-escalate path and can still stop the process if a clean state
can't be proven -- that remains intentional, correct fail-closed behavior.

`test_check_fast_repricing_cancels_both_legs_of_a_paired_entry_together` is
now also the direct regression test for this incident: both legs cancel
cleanly, and the test asserts `get_open_orders`/`cancel_all` were never
called and the local order store ends up empty. Revert-and-confirm-failure
verified: with the old unconditional-verification code restored, this test
fails (`get_open_orders` called once when it shouldn't be).
`test_check_fast_repricing_fails_closed_when_one_paired_cancel_survives` is
unmodified in behavior and now explicitly asserts `get_open_orders` was
called exactly twice (once for the initial verification, once inside
`cancel_all_and_verify`), keeping proof that a genuine per-leg failure still
takes the full path. 1199 tests passing; no live command was started or
restarted as part of this fix.
