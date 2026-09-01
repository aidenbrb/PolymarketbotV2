"""Fail-closed account-wide order cancellation.

``LiveUsClient.cancel_all`` is intentionally best-effort: it logs individual
failures and returns so every known order gets an attempt.  Safety callers must
therefore verify the exchange state afterwards rather than treating that
return as proof that no exposure remains.
"""

from __future__ import annotations

from typing import Any, Optional

from ..logger import get_logger
from .us_client import LiveUsClient

logger = get_logger("live.cancel_safeguard")


class EmergencySafeguardFailedError(RuntimeError):
    """An emergency cancel-all could not be verified on the exchange."""


def cancel_all_and_verify(
    client: LiveUsClient,
    *,
    open_orders: Optional[list[dict[str, Any]]] = None,
    context: str,
) -> None:
    """Attempt account-wide cancellation and prove no order remains open.

    The supplied snapshot only avoids an initial REST enumeration.  The final
    REST fetch is mandatory because the snapshot may be stale and cancellation
    responses may be silent no-ops or only partially successful.
    """
    client.cancel_all(open_orders=open_orders)
    try:
        remaining = client.get_open_orders()
    except Exception as exc:  # noqa: BLE001 -- inability to verify is itself unsafe
        raise EmergencySafeguardFailedError(
            f"{context}: could not verify account-wide cancellation: {exc}"
        ) from exc
    if not isinstance(remaining, list):
        raise EmergencySafeguardFailedError(
            f"{context}: open-orders verification returned invalid state"
        )
    if remaining:
        ids = [order.get("id") or order.get("orderId") for order in remaining]
        raise EmergencySafeguardFailedError(
            f"{context}: {len(remaining)} order(s) remain open after cancel-all: {ids}"
        )
    logger.warning("%s: account-wide cancellation verified clean.", context)
