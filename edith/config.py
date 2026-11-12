"""Load and validate config/edith.yaml.

Config holds settings only, never secrets (those live in the OS keychain, see
edith.safety.secrets). Unknown keys are rejected so typos fail loudly at startup
instead of silently falling back to a default.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UserConfig(_Strict):
    name: str
    address: str = "sir"
    timezone: str = "UTC"
    city: str = ""


class ModelsConfig(_Strict):
    main: str
    fast: str
    deep: str
    local: str
    effort: EffortLevel | None = None


class AgentConfig(_Strict):
    max_turns: int = Field(default=25, ge=1, le=200)


class BudgetsConfig(_Strict):
    per_request_usd: float = Field(default=0.50, gt=0)
    per_day_usd: float = Field(default=3.00, gt=0)


class VoiceConfig(_Strict):
    wake_word: str = "hey_edith"
    stt_model: str = "small"
    tts: str = "kokoro"
    tts_voice: str = "bf_emma"
    follow_up_seconds: int = 8


class FilesConfig(_Strict):
    # Folders EDITH may *ask* to read. Nothing is readable until you approve it.
    allowed_roots: list[str] = Field(default_factory=list)
    # Folders EDITH can never touch, even if you approve (e.g. your work folder).
    blocked_roots: list[str] = Field(default_factory=list)

    def resolved_allowed(self) -> list[Path]:
        return [_resolve_dir(p) for p in self.allowed_roots]

    def resolved_blocked(self) -> list[Path]:
        return [_resolve_dir(p) for p in self.blocked_roots]


def _resolve_dir(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser().resolve()


class ServerConfig(_Strict):
    host: Literal["127.0.0.1", "localhost", "::1"] = "127.0.0.1"  # loopback only, by design
    port: int = Field(default=8765, ge=1024, le=65535)


class SafetyConfig(_Strict):
    confirm_timeout_s: float = Field(default=120, gt=0)  # silence is a no


class PrivacyConfig(_Strict):
    # Turn off Claude Code's telemetry, error reporting and other non-essential traffic.
    disable_cli_telemetry: bool = True


class PathsConfig(_Strict):
    data_dir: Path = Path("~/.edith")

    @field_validator("data_dir", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


class EdithConfig(_Strict):
    user: UserConfig
    models: ModelsConfig
    agent: AgentConfig = AgentConfig()
    budgets: BudgetsConfig = BudgetsConfig()
    voice: VoiceConfig = VoiceConfig()
    files: FilesConfig = FilesConfig()
    # App name -> launch target. EDITH can open only these (fixed templates, no shell).
    apps: dict[str, str] = Field(default_factory=dict)
    server: ServerConfig = ServerConfig()
    safety: SafetyConfig = SafetyConfig()
    privacy: PrivacyConfig = PrivacyConfig()
    quiet_hours: tuple[str, str] = ("23:00", "07:00")
    paths: PathsConfig = PathsConfig()

    @property
    def data_dir(self) -> Path:
        return self.paths.data_dir

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def notes_dir(self) -> Path:
        return self.data_dir / "notes"


class ConfigError(RuntimeError):
    """Raised when the config file is missing or invalid."""


def config_dir() -> Path:
    override = os.environ.get("EDITH_CONFIG_DIR")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_DIR


def load_config(path: Path | None = None) -> EdithConfig:
    """Load edith.yaml. EDITH_DATA_DIR, if set, overrides paths.data_dir."""
    path = path or config_dir() / "edith.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as e:
        raise ConfigError(f"Config file not found: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"Config file is not valid YAML: {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file must contain a mapping: {path}")

    data_dir_override = os.environ.get("EDITH_DATA_DIR")
    if data_dir_override:
        raw.setdefault("paths", {})["data_dir"] = data_dir_override

    try:
        return EdithConfig.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"Invalid config in {path}:\n{e}") from e
