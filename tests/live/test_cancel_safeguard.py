from unittest.mock import Mock

import pytest

from polymarket_bot.live.cancel_safeguard import (
    EmergencySafeguardFailedError,
    cancel_all_and_verify,
)
from polymarket_bot.live.us_client import UsApiError


def test_cancel_all_and_verify_accepts_only_a_confirmed_empty_account():
    client = Mock()
    client.get_open_orders.return_value = []

    cancel_all_and_verify(client, open_orders=[{"id": "o1"}], context="test")

    client.cancel_all.assert_called_once_with(open_orders=[{"id": "o1"}])
    client.get_open_orders.assert_called_once_with()


def test_cancel_all_and_verify_fails_closed_when_an_order_remains():
    client = Mock()
    client.get_open_orders.return_value = [{"id": "o1", "marketSlug": "m1"}]

    with pytest.raises(EmergencySafeguardFailedError, match="1 order.*remain open"):
        cancel_all_and_verify(client, context="test")


def test_cancel_all_and_verify_fails_closed_when_verification_is_unavailable():
    client = Mock()
    client.get_open_orders.side_effect = UsApiError("rate limited")

    with pytest.raises(EmergencySafeguardFailedError, match="could not verify"):
        cancel_all_and_verify(client, context="test")
