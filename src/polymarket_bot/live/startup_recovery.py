"""Runs once at process startup, right after the instance lock is acquired
-- successfully acquiring it (see live/instance_lock.py, an OS-level
exclusive lock) is itself proof no other bot process is alive, so it's also
the earliest point a prior unclean shutdown (an abruptly killed process,
not an ordinary Python exception -- see live/RUNBOOK.md's most recent
section for the confirmed real incident this closes) can be safely
detected and recovered from, before this run places any new orders.

Deliberately FAIL-CLOSED for the order-cancellation half: a leftover
resting order from a crashed session that this run doesn't know about is a
real, unmanaged risk, so an inability to enumerate/cancel/verify it must
abort startup (raise StartupRecoveryError, left uncaught by
runner.py/ws_runner.py's run_forever()) rather than silently proceeding to
place new orders on top of an unknown prior state. Session-status
bookkeeping (mark_stale_running_sessions_crashed) stays best-effort --
purely a historical record, not a safety-critical action.
"""

from __future__ import annotations

from ..logger import get_logger
from .ledger import get_known_order_ids
from .session_metrics import mark_stale_running_sessions_crashed
from .us_client import LiveUsClient, UsApiError

logger = get_logger("live.startup_recovery")


class StartupRecoveryError(RuntimeError):
    """Raised when a clean slate (no unmanaged bot-owned resting orders)
    can't be established before this run places any new orders."""


def verify_flat_for_observation(client: LiveUsClient) -> tuple[list[dict], dict[str, dict]]:
    """Observation-only mode (see ws_runner.py's run_forever) never manages
    or cancels anything for the entire run -- if an order or position
    already exists when it starts, nothing would ever act on it. Refusing
    to start with non-flat state is the only way to keep that a safe,
    honest tradeoff rather than a silent gap.

    Returns the fetched (open_orders, positions) so the caller can seed
    PrivateStateStore with a confirmed-empty baseline instead of a second,
    possibly-inconsistent REST round trip."""
    try:
        open_orders = client.get_open_orders()
    except UsApiError as exc:
        raise StartupRecoveryError(
            f"Could not enumerate open orders before starting observation-only mode: {exc}"
        ) from exc
    if open_orders:
        ids = [order.get("id") or order.get("orderId") for order in open_orders]
        raise StartupRecoveryError(
            f"{len(open_orders)} open order(s) exist -- refusing to start "
            f"observation-only mode with non-flat state (nothing would manage "
            f"them for the whole run): {ids}"
        )

    try:
        positions = client.get_all_positions()
    except UsApiError as exc:
        raise StartupRecoveryError(
            f"Could not enumerate positions before starting observation-only mode: {exc}"
        ) from exc
    non_flat = [slug for slug, position in positions.items() if not _position_is_flat(position)]
    if non_flat:
        raise StartupRecoveryError(
            f"{len(non_flat)} open position(s) exist -- refusing to start "
            f"observation-only mode with non-flat state (nothing would manage "
            f"them for the whole run): {sorted(non_flat)}"
        )
    return open_orders, positions


def _position_is_flat(position: dict) -> bool:
    value = position.get("netPositionDecimal")
    if value is None:
        value = position.get("net_position")
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def recover_from_prior_crash(client: LiveUsClient) -> None:
    try:
        stale_sessions = mark_stale_running_sessions_crashed()
        if stale_sessions:
            logger.warning(
                "Found %d session(s) still marked 'running' from a prior process "
                "that never reached clean shutdown -- marked crashed: %s",
                len(stale_sessions), stale_sessions,
            )
    except Exception as exc:  # noqa: BLE001 -- session bookkeeping is diagnostic, not safety-critical
        logger.warning("Could not mark stale sessions crashed: %s", exc)

    _cancel_and_verify_recognized_leftover_orders(client)


def _cancel_and_verify_recognized_leftover_orders(client: LiveUsClient) -> None:
    """Only cancels orders this bot's local ledger recognizes as its own
    (get_known_order_ids()) -- the same "bot-owned, not just anything on
    the account" filter multi_market_maker.py's stale-order sweep already
    uses -- rather than the blunter cancel_all(), which cancels everything
    on the account regardless of origin.

    A cancel_order() call not raising doesn't guarantee the exchange
    actually removed the order (eventual consistency, or a silent no-op),
    so after cancelling, this re-fetches open orders and confirms no
    bot-recognized order is still resting before returning."""
    try:
        open_orders = client.get_open_orders()
    except UsApiError as exc:
        raise StartupRecoveryError(
            f"Could not enumerate open orders for startup crash recovery: {exc}"
        ) from exc

    known_ids = get_known_order_ids()
    to_cancel = [
        (order_id, market_slug)
        for order in open_orders
        if (order_id := order.get("id") or order.get("orderId"))
        and (market_slug := order.get("marketSlug") or order.get("market_slug"))
        and str(order_id) in known_ids
    ]
    if not to_cancel:
        return

    for order_id, market_slug in to_cancel:
        try:
            client.cancel_order(order_id, market_slug)
        except UsApiError as exc:
            raise StartupRecoveryError(
                f"Could not cancel leftover order {order_id} during startup crash recovery: {exc}"
            ) from exc

    logger.warning(
        "Cancelled %d resting order(s) left over from a prior crashed "
        "session before allowing new placements this run.", len(to_cancel),
    )

    try:
        remaining_open = client.get_open_orders()
    except UsApiError as exc:
        raise StartupRecoveryError(
            f"Could not verify leftover-order cleanup: {exc}"
        ) from exc
    still_present = [
        order_id for order in remaining_open
        if (order_id := order.get("id") or order.get("orderId")) and str(order_id) in known_ids
    ]
    if still_present:
        raise StartupRecoveryError(
            f"{len(still_present)} bot-recognized order(s) still resting after "
            f"the cancellation attempt: {still_present}"
        )
