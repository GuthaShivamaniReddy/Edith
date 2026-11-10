from __future__ import annotations

import pytest

from edith.safety import secrets
from tests.conftest import MemoryKeyring


def test_set_get_delete(memory_keyring: MemoryKeyring) -> None:
    assert secrets.get_secret("anthropic_api_key") is None
    secrets.set_secret("anthropic_api_key", "  sk-ant-test-1234567890  ")
    assert memory_keyring.store[("edith", "anthropic_api_key")] == "sk-ant-test-1234567890"
    assert secrets.get_secret("anthropic_api_key") == "sk-ant-test-1234567890"
    assert secrets.delete_secret("anthropic_api_key") is True
    assert secrets.delete_secret("anthropic_api_key") is False
    assert secrets.get_secret("anthropic_api_key") is None


def test_unknown_name_rejected() -> None:
    with pytest.raises(secrets.SecretError, match="Unknown secret"):
        secrets.set_secret("anthropc_api_key", "x")


def test_empty_value_rejected() -> None:
    with pytest.raises(secrets.SecretError, match="empty"):
        secrets.set_secret("anthropic_api_key", "   ")


def test_values_are_registered_for_redaction() -> None:
    secrets.set_secret("anthropic_api_key", "sk-ant-abcdef123456")
    assert "sk-ant-abcdef123456" in secrets.known_secret_values()


def test_mask() -> None:
    assert secrets.mask("sk-ant-abcdef123456") == "sk-a…3456"
    assert secrets.mask("short") == "***"
