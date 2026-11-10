from __future__ import annotations

from pathlib import Path

import pytest

from edith.config import DEFAULT_CONFIG_DIR, ConfigError, EdithConfig, load_config


def test_repo_config_is_valid() -> None:
    cfg = load_config(DEFAULT_CONFIG_DIR / "edith.yaml")
    assert cfg.models.main.startswith("claude-")
    assert cfg.budgets.per_day_usd > 0


def test_loads_minimal_config(cfg: EdithConfig, tmp_path: Path) -> None:
    assert cfg.user.name == "Tester"
    assert cfg.agent.max_turns == 25  # default
    assert cfg.budgets.per_day_usd == 1.00
    assert cfg.data_dir == tmp_path / "data"  # EDITH_DATA_DIR override from conftest


def test_unknown_keys_are_rejected(config_file: Path) -> None:
    config_file.write_text(config_file.read_text() + "\nmodles: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="modles"):
        load_config(config_file)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("user: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)


def test_bad_effort_rejected(config_file: Path) -> None:
    config_file.write_text(
        config_file.read_text().replace("effort: medium", "effort: turbo"), encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_config(config_file)


def test_config_contains_no_secrets() -> None:
    """Secrets live in the keychain; config files must never carry them."""
    for path in DEFAULT_CONFIG_DIR.glob("*.yaml"):
        text = path.read_text(encoding="utf-8").lower()
        assert "sk-ant-" not in text, path
        assert "api_key:" not in text, path
