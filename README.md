# Polymarket Bot (Polymarket US)

> **⚠️ Not financial advice. Not a guarantee of profit.**
>
> This targets **Polymarket US** (`polymarket.us`) -- a separate,
> CFTC-regulated, USD-settled exchange operated by QCX LLC. It is a
> completely different platform from the international `polymarket.com`
> (crypto-settled, geoblocked for US persons); this project does not use
> that platform at all.
>
> This repo has two clearly separated parts:
> - **Everything outside `src/polymarket_bot/live/`** is a read-only research
>   tool plus a fully simulated paper-trading ledger. It has no credentials,
>   no signing, and no order-placement code, and never will --
>   [`tests/test_no_live_orders.py`](tests/test_no_live_orders.py) fails the
>   build if that ever changes.
> - **`src/polymarket_bot/live/`** is an opt-in live market-making module that
>   places **real orders with real money**. It is off by default
>   (`LIVE_TRADING_ENABLED=false`), requires your own Polymarket US API
>   credentials in your own local `.env`, and requires typing an exact
>   confirmation phrase every time you start it -- see
>   **[Live trading](#live-trading-real-money)** below and
>   [`src/polymarket_bot/live/RUNBOOK.md`](src/polymarket_bot/live/RUNBOOK.md)
>   before ever touching it.

## What the paper-trading side does

1. **Scans** public Polymarket US markets (Gateway API, no API key required).
2. **Filters** out low-quality/illiquid/unclear markets with documented reasons.
3. **Scores** the remaining markets 0–100 using a fully transparent, auditable
   rule-based formula (no black-box model).
4. Lets you record **manual paper trades** against a simulated portfolio and
   tracks fake P/L, win rate, and drawdown over time.
5. Shows everything in a simple CLI dashboard.

## What it does NOT do (outside live/)

- Does not require, store, or accept an API key ID, secret key, or any credential.
- Does not place, sign, or cancel real orders.
- Does not execute trades automatically. Paper trades are manual, opt-in
  CLI actions only (`paper-buy` / `paper-close`).
- Is not investment advice.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env         # optional, defaults already work
```

## Usage (paper trading -- always safe)

```bash
python -m polymarket_bot.main scan
python -m polymarket_bot.main show-watchlist
python -m polymarket_bot.main paper-buy
python -m polymarket_bot.main show-portfolio
python -m polymarket_bot.main show-rejections
```

## Live trading (REAL MONEY)

`src/polymarket_bot/live/` runs a market-making strategy: quote a bid and ask
simultaneously (3¢ apart by default), refreshing every ~15 minutes, using
real funds via Polymarket US's authenticated REST API (Ed25519-signed
requests, via the `cryptography` package -- installed separately, see below).

**Read [`src/polymarket_bot/live/RUNBOOK.md`](src/polymarket_bot/live/RUNBOOK.md)
in full before running any `live-*` command.** It covers credential setup and
several things that genuinely cannot be verified without a funded live
account -- notably whether the order-side semantics (`OUTCOME_SIDE_YES`)
apply cleanly to the sports moneyline markets that dominate Polymarket US's
current listings, versus a literal Yes/No market.

```bash
pip install cryptography       # optional dependency, only needed for live/

python -m polymarket_bot.main live-preview        # read-only, no credentials needed
python -m polymarket_bot.main live-status
python -m polymarket_bot.main live-cancel-all
python -m polymarket_bot.main live-reset-breaker
python -m polymarket_bot.main live-start           # places real orders
```

Safety design, all overridable via your own local `.env` (never
`.env.example`/`.env.live.example`, which are tracked templates):

- **Off by default.** `LIVE_TRADING_ENABLED=false` until you explicitly flip it.
- **No `--yes`/`--force` flag exists anywhere.** `live-start` and
  `live-reset-breaker` require typing an exact confirmation phrase every time
  (`live/confirmation.py`) -- enforced by a test that inspects the CLI parser.
- **Daily-loss circuit breaker, on by default**
  (`CIRCUIT_BREAKER_ENABLED=true`, `CIRCUIT_BREAKER_DAILY_LOSS_LIMIT_USD=100`).
  Cancels all resting orders and halts new quoting if today's account balance
  drops by more than the limit vs. the start of the day. This is a
  recommended safety net, not a hard requirement -- set
  `CIRCUIT_BREAKER_ENABLED=false` if you want it off. Assumes no external
  deposits/withdrawals happen mid-session (see RUNBOOK item 5).
- **Never asks for your API secret key in chat or anywhere but your local
  `.env`.** You already have Key ID + Secret Key issued via Polymarket US's
  developer portal -- type them directly into your own `.env`.
- **Never touches unrelated orders on your account.** Each refresh cycle
  cancels only its own market's resting orders, not the whole account's book
  (`live-cancel-all` is the explicit, separate command for clearing everything).

### Autostart on login

`scripts/run_live_autostart.ps1` + a Windows Scheduled Task
(`PolymarketBotLiveAutostart`) can launch `live-start` automatically at
login, with real money and **no interactive confirmation** — this bypasses
the typed-confirmation gate for that one launch path only (manual runs from
a terminal still require it). This was explicitly requested and understood
to remove that safety net. See
[`src/polymarket_bot/live/RUNBOOK.md`](src/polymarket_bot/live/RUNBOOK.md)
section 8 for what's registered, how to check on it, and how to disable it
(`Disable-ScheduledTask -TaskName "PolymarketBotLiveAutostart"` or
`Unregister-ScheduledTask` to remove it entirely).

## Running tests

```bash
pytest
```

## Project layout

```
src/polymarket_bot/
  config.py            settings, thresholds, base URLs (no secrets, ever)
  polymarket_client.py  public read-only Polymarket US Gateway API client
  market_scanner.py     fetches + normalizes market data
  filters.py            rejects low-quality markets with reasons
  scoring_engine.py     transparent 0-100 rule-based scoring
  risk_manager.py       position-size / exposure guardrails for paper trades
  paper_trader.py       simulated (fake) trade execution
  portfolio_tracker.py  simulated portfolio, P/L, win rate, drawdown
  logger.py             console + file logging
  storage.py            JSON/CSV persistence helpers
  models.py             shared dataclasses (no credential fields)
  dashboard.py          CLI rendering
  main.py               CLI entry point
  live/                 REAL MONEY -- isolated live-trading subpackage
    credentials.py         loads Key ID + Secret Key from env, never logs them
    us_client.py            the only file with order placement/cancellation
    market_selection.py    reuses the scanner/filter/scoring pipeline
    pricing.py             pure tick-aware bid/ask math
    market_maker.py        one refresh cycle (cancel then replace)
    circuit_breaker.py     daily-loss halt, on by default
    ledger.py              real trade/order record + balance-diff P/L
    confirmation.py        the go-live gate (env flag + typed phrase)
    runner.py              ties the refresh loop together
    RUNBOOK.md              manual pre-go-live checklist
scripts/
  run_live_autostart.ps1  REAL MONEY, unattended -- see RUNBOOK.md section 8
```

## Disclaimer

This software is provided for educational and research purposes only. It does
not constitute financial advice, and past market scores or simulated paper
trading results are not indicative of future real-world performance. Use of
any information from this tool for real trading decisions is entirely at your
own risk.
