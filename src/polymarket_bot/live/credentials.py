"""Loads Polymarket US API credentials from environment variables.

This is the only module that ever reads the Ed25519 secret key. It never
logs the raw secret and never persists it anywhere.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from ..logger import get_logger

logger = get_logger("live.credentials")

_REDACTED = "***REDACTED***"


class MissingCredentialsError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApiCredentials:
    key_id: str  # public identifier, safe to log
    secret_key: str  # base64-encoded Ed25519 private key seed -- never logged

    def __repr__(self) -> str:
        return f"ApiCredentials(key_id={self.key_id}, secret_key={_REDACTED})"

    __str__ = __repr__


def load_api_credentials() -> ApiCredentials:
    key_id = os.getenv("POLYMARKET_US_KEY_ID", "").strip()
    secret_key = os.getenv("POLYMARKET_US_SECRET_KEY", "").strip()

    if not key_id:
        raise MissingCredentialsError(
            "POLYMARKET_US_KEY_ID is not set. Add it to your own local .env "
            "(never to .env.example or .env.live.example, which are templates only)."
        )
    if not secret_key:
        raise MissingCredentialsError(
            "POLYMARKET_US_SECRET_KEY is not set. Add it to your local .env."
        )
    _validate_secret_key_format(secret_key)

    logger.info("Loaded Polymarket US API credentials for key_id %s", key_id)
    return ApiCredentials(key_id=key_id, secret_key=secret_key)


def _validate_secret_key_format(secret_key: str) -> None:
    try:
        decoded = base64.b64decode(secret_key, validate=True)
    except Exception as exc:
        raise MissingCredentialsError(
            "POLYMARKET_US_SECRET_KEY does not look like valid base64 data. "
            "The value itself is never logged or shown."
        ) from exc
    if len(decoded) < 32:
        raise MissingCredentialsError(
            "POLYMARKET_US_SECRET_KEY decodes to fewer than 32 bytes -- too short "
            "to be a valid Ed25519 private key seed. The value itself is never "
            "logged or shown."
        )
