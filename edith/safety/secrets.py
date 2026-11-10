"""Secrets live in the OS keychain (Windows Credential Manager, macOS Keychain,
Secret Service on Linux) via `keyring`. Never in code, config, logs, prompts or the repo.

Every secret value read through this module is registered with the log redactor,
so even an accidental `log.info(..., key=value)` writes "***" instead.
"""

from __future__ import annotations

import keyring
import keyring.errors

SERVICE = "edith"

# Names EDITH knows about. Anything else is rejected to avoid typos creating
# orphaned keychain entries.
KNOWN_SECRETS: dict[str, str] = {
    "anthropic_api_key": "Claude Console API key (optional; otherwise your Claude login is used)",
}

_seen_values: set[str] = set()


class SecretError(RuntimeError):
    pass


def _check_name(name: str) -> None:
    if name not in KNOWN_SECRETS:
        known = ", ".join(sorted(KNOWN_SECRETS))
        raise SecretError(f"Unknown secret '{name}'. Known secrets: {known}")


def get_secret(name: str) -> str | None:
    _check_name(name)
    try:
        value = keyring.get_password(SERVICE, name)
    except keyring.errors.KeyringError as e:
        raise SecretError(f"Could not read '{name}' from the OS keychain: {e}") from e
    if value:
        _seen_values.add(value)
    return value or None


def set_secret(name: str, value: str) -> None:
    _check_name(name)
    value = value.strip()
    if not value:
        raise SecretError("Refusing to store an empty secret.")
    try:
        keyring.set_password(SERVICE, name, value)
    except keyring.errors.KeyringError as e:
        raise SecretError(f"Could not write '{name}' to the OS keychain: {e}") from e
    _seen_values.add(value)


def delete_secret(name: str) -> bool:
    """Delete a secret. Returns False if it was not set."""
    _check_name(name)
    try:
        keyring.delete_password(SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        return False
    except keyring.errors.KeyringError as e:
        raise SecretError(f"Could not delete '{name}' from the OS keychain: {e}") from e
    return True


def known_secret_values() -> frozenset[str]:
    """Secret values seen this process, for the log redactor. Never log this."""
    return frozenset(_seen_values)


def mask(value: str) -> str:
    """Show only enough of a secret to recognise it, e.g. 'sk-a…9f2c'."""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"
