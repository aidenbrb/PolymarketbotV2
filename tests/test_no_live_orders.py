"""Fails the build if any live-execution code path leaks outside live/.

Every module under src/polymarket_bot EXCEPT the live/ subpackage must
remain a read-only research tool plus a fully simulated paper-trading
ledger: it must never gain the ability to place, sign, or cancel a real
order, and must never require a wallet or API secret to run. live/ is the
one deliberate, isolated exception -- see live/__init__.py.
"""

from __future__ import annotations

from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "polymarket_bot"
LIVE_ROOT = SRC_ROOT / "live"

FORBIDDEN_SUBSTRINGS = [
    "place_order",
    "create_order",
    "post_order",
    "sign_order",
    "cancel_order",
    "private_key",
    "secret_key",
    "live_trader",
    "seed_phrase",
    "mnemonic",
    "wallet_address",
]

# fake_slippage_bps is a paper-trading simulation parameter, not live execution.
ALLOWED_CONTEXT_SUBSTRINGS = ["fake_slippage_bps"]


def _all_source_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _paper_source_files() -> list[Path]:
    """Every source file EXCEPT the live/ subpackage."""
    return sorted(p for p in SRC_ROOT.rglob("*.py") if LIVE_ROOT not in p.parents)


def _live_source_files() -> list[Path]:
    return sorted(LIVE_ROOT.rglob("*.py")) if LIVE_ROOT.exists() else []


def _scan_for_forbidden(paths: list[Path]) -> list[str]:
    violations = []
    for path in paths:
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in FORBIDDEN_SUBSTRINGS:
            if forbidden in text:
                violations.append(f"{path.relative_to(SRC_ROOT)}: contains '{forbidden}'")
    return violations


def test_source_files_exist_to_scan():
    files = _all_source_files()
    assert len(files) > 0


def test_no_forbidden_live_execution_patterns_outside_live_package():
    """The original guarantee, preserved exactly: paper-trading code (every
    file except live/) can never gain live-order capability."""
    violations = _scan_for_forbidden(_paper_source_files())
    assert not violations, "Live-execution code path detected outside live/:\n" + "\n".join(
        violations
    )


def test_live_subpackage_exists_and_genuinely_contains_live_code():
    """Sanity check that live/ isn't an accidental no-op: it must actually
    contain the order-placement/wallet code it claims to isolate. If this
    ever fails, the live wrapper isn't wired up the way it should be."""
    live_files = _live_source_files()
    assert live_files, "live/ subpackage is missing or empty"
    violations = _scan_for_forbidden(live_files)
    assert violations, "live/ does not contain any of the expected live-order/wallet terms"


def test_no_api_key_or_secret_required_env_vars():
    """.env.example (tracked, copied by every new setup) must never carry
    secret-adjacent values. Live wallet variable NAMES live only in the
    separate .env.live.example, checked below."""
    env_example = SRC_ROOT.parents[1] / ".env.example"
    if not env_example.exists():
        return
    text = env_example.read_text(encoding="utf-8").lower()
    for forbidden in ("private_key", "secret_key", "wallet_address", "seed_phrase", "mnemonic"):
        assert forbidden not in text, f".env.example must not reference '{forbidden}'"


def test_env_example_never_enables_unattended_mode():
    """LIVE_UNATTENDED_MODE=true skips the typed confirmation phrase entirely
    (see live/confirmation.py) -- it must only ever be set in the autostart
    wrapper script's own process environment, never in the shared, tracked
    .env.example that every setup copies from."""
    env_example = SRC_ROOT.parents[1] / ".env.example"
    if not env_example.exists():
        return
    text = env_example.read_text(encoding="utf-8")
    assert "LIVE_UNATTENDED_MODE=true" not in text


def test_env_live_example_documents_names_only_never_real_values():
    env_live_example = SRC_ROOT.parents[1] / ".env.live.example"
    assert env_live_example.exists(), ".env.live.example should exist to document credential var names"

    text = env_live_example.read_text(encoding="utf-8")
    assert "POLYMARKET_US_SECRET_KEY=" in text
    # Only the bare "NAME=" template line should appear -- no value after the
    # equals sign on that line (a real secret key is far longer).
    for line in text.splitlines():
        if line.startswith("POLYMARKET_US_SECRET_KEY="):
            assert line.strip() == "POLYMARKET_US_SECRET_KEY=", (
                ".env.live.example must not contain a real secret key value"
            )


def test_env_example_and_env_live_example_are_distinct_files():
    root = SRC_ROOT.parents[1]
    assert (root / ".env.example").resolve() != (root / ".env.live.example").resolve()


def test_polymarket_client_has_no_order_methods():
    from polymarket_bot.polymarket_client import PolymarketClient

    banned_method_prefixes = ("place_", "create_order", "post_order", "sign_", "cancel_")
    for attr_name in dir(PolymarketClient):
        assert not any(attr_name.startswith(p) for p in banned_method_prefixes), (
            f"PolymarketClient must not define '{attr_name}'"
        )


def test_live_start_has_no_yes_or_force_flag():
    """live-start must never be scriptable past its confirmation gate with a
    bare flag -- the typed phrase in live/confirmation.py is the only path."""
    from polymarket_bot.main import build_parser

    parser = build_parser()
    live_start_subparser = None
    for action in parser._subparsers._group_actions:
        for name, subparser in action.choices.items():
            if name == "live-start":
                live_start_subparser = subparser
    assert live_start_subparser is not None, "live-start command not found"

    for action in live_start_subparser._actions:
        option_strings = " ".join(action.option_strings).lower()
        assert "yes" not in option_strings and "force" not in option_strings, (
            f"live-start must not define a --yes/--force-style flag, found: {action.option_strings}"
        )


def test_july5_pilot_has_no_yes_or_force_flag():
    """The qualification bypass is not a confirmation bypass."""
    from polymarket_bot.main import build_parser

    parser = build_parser()
    pilot_subparser = None
    for action in parser._subparsers._group_actions:
        pilot_subparser = action.choices.get("live-pilot-start-july5")
    assert pilot_subparser is not None, "live-pilot-start-july5 command not found"

    for action in pilot_subparser._actions:
        option_strings = " ".join(action.option_strings).lower()
        assert "yes" not in option_strings and "force" not in option_strings, (
            "live-pilot-start-july5 must not define a --yes/--force-style "
            f"flag, found: {action.option_strings}"
        )


def test_observation_continuation_defaults_to_thirty_hours():
    from polymarket_bot.main import build_parser

    args = build_parser().parse_args(["live-observation-continue"])

    assert args.command == "live-observation-continue"
    assert args.hours == 30.0


def test_observation_healthy_completion_command_is_explicit():
    from polymarket_bot.main import build_parser

    args = build_parser().parse_args(["live-observation-complete"])

    assert args.command == "live-observation-complete"
