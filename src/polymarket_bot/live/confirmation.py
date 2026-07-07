"""The go-live confirmation gate.

Isolated from argparse so it's directly unit-testable. live-start and
live-reset-breaker must pass through here before any credential is ever
loaded or any order is ever placed. There is no --yes/--force CLI flag
anywhere that could satisfy this -- see main.py's argparse wiring and
tests/test_no_live_orders.py, which asserts no such flag is ever added.

The ONE exception is `settings.unattended_mode` (env var
LIVE_UNATTENDED_MODE), which the user explicitly requested to enable
autostart-on-boot. It is off by default and deliberately NOT meant to be set
in the shared .env -- it's set only in the environment of the specific
autostart wrapper script (scripts/run_live_autostart.ps1), so a manual,
interactive `live-start` run from a terminal still requires the typed
phrase every time. Every unattended start is still logged as a warning so
there's an audit trail in logs/bot.log.
"""

from __future__ import annotations

from typing import Callable

from .. import config
from ..logger import get_logger

logger = get_logger("live.confirmation")


class LiveTradingNotConfirmed(RuntimeError):
    pass


def require_live_confirmation(
    settings: config.LiveTradingSettings, input_fn: Callable[[str], str] = input
) -> None:
    """Fails closed immediately if LIVE_TRADING_ENABLED is not true -- no
    prompt, no credential-related import happens past this point. Otherwise,
    skips the interactive prompt only if unattended_mode is explicitly set
    (autostart path); every other caller must type the exact phrase."""
    if not settings.enabled:
        raise LiveTradingNotConfirmed(
            "LIVE_TRADING_ENABLED is not 'true' in your .env. Live trading stays off "
            "by default -- set it explicitly in your own local .env when you're ready."
        )

    if settings.unattended_mode:
        logger.warning(
            "LIVE_UNATTENDED_MODE=true -- skipping interactive confirmation for an "
            "autostart/unattended launch. Real orders may be placed with no human "
            "supervision this session."
        )
        return

    print()
    print("=" * 70)
    print("WARNING: this will place REAL orders with REAL money on Polymarket US.")
    print("This code has never been run against the live exchange before.")
    print("=" * 70)
    print(f"Type exactly: {settings.confirmation_phrase}")
    typed = input_fn("> ").strip()
    if typed != settings.confirmation_phrase:
        raise LiveTradingNotConfirmed("Confirmation phrase did not match. Aborting.")

    logger.warning("Live trading confirmed by user. Proceeding to place real orders.")
