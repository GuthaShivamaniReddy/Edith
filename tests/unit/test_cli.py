from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from edith import __version__
from edith.interfaces.cli import app
from tests.conftest import MemoryKeyring

runner = CliRunner()


@pytest.fixture(autouse=True)
def use_test_config(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITH_CONFIG_DIR", str(config_file.parent))


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_costs_empty() -> None:
    result = runner.invoke(app, ["costs"])
    assert result.exit_code == 0, result.output
    assert "Today: $0.0000" in result.output


def test_status_does_not_call_model() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Credentials: Claude Code login" in result.output


def test_secrets_set_list_delete(memory_keyring: MemoryKeyring) -> None:
    result = runner.invoke(
        app, ["secrets", "set", "anthropic_api_key"], input="sk-ant-cli-12345678\n"
    )
    assert result.exit_code == 0, result.output
    assert "sk-ant-cli-12345678" not in result.output  # never echoed
    assert memory_keyring.store[("edith", "anthropic_api_key")] == "sk-ant-cli-12345678"

    listed = runner.invoke(app, ["secrets", "list"])
    assert "anthropic_api_key: set" in listed.output
    assert "sk-ant-cli-12345678" not in listed.output

    deleted = runner.invoke(app, ["secrets", "delete", "anthropic_api_key"])
    assert "Removed" in deleted.output


def test_bad_config_exits_cleanly(config_file: Path) -> None:
    config_file.write_text("user: [", encoding="utf-8")
    result = runner.invoke(app, ["costs"])
    assert result.exit_code == 2
