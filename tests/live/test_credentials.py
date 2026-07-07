import base64
import logging

import pytest

from polymarket_bot.live.credentials import MissingCredentialsError, load_api_credentials

VALID_SECRET = base64.b64encode(b"a" * 32).decode()
VALID_KEY_ID = "11111111-1111-1111-1111-111111111111"


def test_missing_key_id_raises(monkeypatch):
    monkeypatch.delenv("POLYMARKET_US_KEY_ID", raising=False)
    monkeypatch.setenv("POLYMARKET_US_SECRET_KEY", VALID_SECRET)
    with pytest.raises(MissingCredentialsError):
        load_api_credentials()


def test_missing_secret_key_raises(monkeypatch):
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", VALID_KEY_ID)
    monkeypatch.delenv("POLYMARKET_US_SECRET_KEY", raising=False)
    with pytest.raises(MissingCredentialsError):
        load_api_credentials()


def test_malformed_secret_key_raises(monkeypatch):
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", VALID_KEY_ID)
    monkeypatch.setenv("POLYMARKET_US_SECRET_KEY", "not-valid-base64!!!")
    with pytest.raises(MissingCredentialsError):
        load_api_credentials()


def test_too_short_secret_key_raises(monkeypatch):
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", VALID_KEY_ID)
    monkeypatch.setenv("POLYMARKET_US_SECRET_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(MissingCredentialsError):
        load_api_credentials()


def test_valid_credentials_load_successfully(monkeypatch):
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", VALID_KEY_ID)
    monkeypatch.setenv("POLYMARKET_US_SECRET_KEY", VALID_SECRET)
    creds = load_api_credentials()
    assert creds.key_id == VALID_KEY_ID
    assert creds.secret_key == VALID_SECRET


def test_repr_and_str_never_contain_raw_secret(monkeypatch):
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", VALID_KEY_ID)
    monkeypatch.setenv("POLYMARKET_US_SECRET_KEY", VALID_SECRET)
    creds = load_api_credentials()
    assert VALID_SECRET not in repr(creds)
    assert VALID_SECRET not in str(creds)
    assert "REDACTED" in repr(creds)
    # key_id is a public identifier -- fine to show
    assert VALID_KEY_ID in repr(creds)


def test_load_api_credentials_never_logs_raw_secret(monkeypatch, caplog):
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", VALID_KEY_ID)
    monkeypatch.setenv("POLYMARKET_US_SECRET_KEY", VALID_SECRET)
    with caplog.at_level(logging.DEBUG):
        load_api_credentials()
    for record in caplog.records:
        assert VALID_SECRET not in record.getMessage()
