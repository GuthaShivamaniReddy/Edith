from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest

from edith.config import (
    DEFAULT_CONFIG_DIR,
    EdithConfig,
    FilesConfig,
    SafetyConfig,
    load_config,
)
from edith.safety import secrets
from edith.safety.policy import Permissions

MINIMAL_CONFIG = """
user:
  name: "Tester"
  address: "boss"
  timezone: "America/New_York"
models:
  main: claude-sonnet-5-5
  fast: claude-haiku-5-5
  deep: claude-opus-5-5
  local: gemma4:12b
  effort: medium
budgets:
  per_day_usd: 1.00
"""


class MemoryKeyring(keyring.backend.KeyringBackend):
    """In-memory keyring so tests never touch the real OS keychain."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError("not set")
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    secrets._seen_values.clear()
    yield backend
    keyring.set_keyring(previous)
    secrets._seen_values.clear()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the developer's real data dir or config overrides during tests."""
    monkeypatch.setenv("EDITH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("EDITH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("EDITH_LOG_LEVEL", raising=False)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config" / "edith.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")
    return path


@pytest.fixture
def cfg(config_file: Path) -> EdithConfig:
    return load_config(config_file)


@pytest.fixture
def permissions() -> Permissions:
    """The real policy file from the repo: tests check the policy the user approved."""
    return Permissions.load(DEFAULT_CONFIG_DIR / "permissions.yaml")


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A folder EDITH may ask to access (never the developer's real home)."""
    root = tmp_path / "sandbox"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "todo.txt").write_text("buy milk\ncall mom\n", encoding="utf-8")
    (root / "secret").mkdir()
    (root / "secret" / "salary.txt").write_text("classified", encoding="utf-8")
    return root


@pytest.fixture
def tool_cfg(cfg: EdithConfig, sandbox: Path) -> EdithConfig:
    """Config with file scope pointed at the sandbox, a blocked subfolder and two apps."""
    return cfg.model_copy(
        update={
            "files": FilesConfig(
                allowed_roots=[str(sandbox)], blocked_roots=[str(sandbox / "secret")]
            ),
            "apps": {"notepad": "notepad.exe", "calculator": "calc.exe"},
            "safety": SafetyConfig(confirm_timeout_s=2),
        }
    )
