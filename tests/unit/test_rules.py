"""Fast path, router, policy loading, scope and redaction."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from edith.config import EdithConfig
from edith.core import fastpath
from edith.core.events import Request
from edith.core.router import route
from edith.safety.policy import Permissions, PolicyError, policy_name
from edith.safety.redact import REDACTED, redact
from edith.safety.scope import FileScope, ScopeError

# ---------------------------------------------------------------- fast path


@pytest.mark.parametrize("text", ["stop", "Edith, stop!", "hey edith cancel that", "never mind"])
def test_stop_words(text: str) -> None:
    cmd = fastpath.match(text)
    assert cmd is not None and cmd.kind == "stop"


def test_stop_word_inside_sentence_is_not_a_stop() -> None:
    assert fastpath.match("how do I stop my sink leaking") is None


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("set volume to 30", "system.set_volume", {"level": 30}),
        ("Edith, volume 150%", "system.set_volume", {"level": 100}),
        ("turn the volume up", "system.set_volume", {"delta": 10}),
        ("turn it down please", "system.set_volume", {"delta": -10}),
        ("mute", "system.mute", {"muted": True}),
        ("unmute the sound", "system.mute", {"muted": False}),
        ("set a timer for 10 minutes", "timers.set", {"seconds": 600, "label": "10 minutes timer"}),
        ("5 minute timer for the eggs", "timers.set", {"seconds": 300, "label": "eggs"}),
    ],
)
def test_fast_tools(text: str, tool: str, args: dict[str, object]) -> None:
    cmd = fastpath.match(text)
    assert cmd is not None and cmd.kind == "tool"
    assert (cmd.tool, cmd.args) == (tool, args)


def test_time_and_date_replies() -> None:
    now = datetime(2026, 11, 12, 9, 5)
    t = fastpath.match("what time is it?", now=now)
    d = fastpath.match("what's the date today", now=now)
    assert t is not None and t.reply == "It's 9:05 AM."
    assert d is not None and d.reply == "It's Thursday, November 12, 2026."


def test_open_requests_go_to_model() -> None:
    assert fastpath.match("set the volume so I can hear the podcast") is None


# ---------------------------------------------------------------- router


@pytest.mark.parametrize(
    ("text", "kind", "model"),
    [
        ("hi", "quick_answer", "claude-haiku-5-5"),
        ("what is the capital of France", "quick_answer", "claude-haiku-5-5"),
        ("open notepad", "task", "claude-sonnet-5-5"),
        ("what's the latest news on the election", "task", "claude-sonnet-5-5"),
        ("read C:\\notes\\a.txt", "task", "claude-sonnet-5-5"),
        ("compare these two laptops", "deep_research", "claude-sonnet-5-5"),
    ],
)
def test_router(cfg: EdithConfig, text: str, kind: str, model: str) -> None:
    r = route(Request(text=text), cfg)
    assert (r.kind, r.model) == (kind, model)


def test_router_long_request_uses_main_model(cfg: EdithConfig) -> None:
    r = route(Request(text="tell me a story about a " + "very " * 30 + "brave cat"), cfg)
    assert r.model == cfg.models.main


# ---------------------------------------------------------------- policy


def test_policy_names() -> None:
    assert policy_name("mcp__files__read") == "files.read"
    assert policy_name("WebSearch") == "web.search"
    assert policy_name("Bash") == "builtin.Bash"


def test_unknown_builtins_are_tier2(permissions: Permissions) -> None:
    for name in ("Bash", "Write", "Edit", "Read"):
        assert permissions.tier_for(policy_name(name)) == 2


def test_wildcards_take_most_restrictive(permissions: Permissions) -> None:
    assert permissions.tier_for("payments.charge") == 3
    assert permissions.tier_for("shell.run") == 2


@pytest.mark.parametrize(
    "raw",
    [{"default_tier": 0}, {"default_tier": 1}, {"tools": {"x.y": 7}}, {"access": {"a.b": {}}}],
)
def test_policy_rejects_unsafe_files(raw: dict[str, object]) -> None:
    with pytest.raises(PolicyError):
        Permissions.from_dict(raw)


def test_missing_policy_file(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        Permissions.load(tmp_path / "nope.yaml")


# ---------------------------------------------------------------- scope


def test_scope(tool_cfg: EdithConfig, sandbox: Path, tmp_path: Path) -> None:
    scope = FileScope.from_config(tool_cfg)
    assert (
        scope.resolve(str(sandbox / "docs" / "todo.txt"))
        == (sandbox / "docs" / "todo.txt").resolve()
    )
    for bad in [
        "",
        str(sandbox / "secret"),
        str(sandbox / "docs" / ".." / "secret" / "x"),
        str(tmp_path),
    ]:
        with pytest.raises(ScopeError):
            scope.resolve(bad)


def test_empty_scope_allows_nothing(sandbox: Path) -> None:
    with pytest.raises(ScopeError, match="none configured"):
        FileScope((), ()).resolve(str(sandbox))


# ---------------------------------------------------------------- redaction


def test_redact_masks_secret_keys_not_token_counts() -> None:
    out = redact(
        {"api_key": "x", "nested": {"password": "p"}, "input_tokens": 5, "s": "a" * 50}, max_str=10
    )
    assert out["api_key"] == REDACTED and out["nested"]["password"] == REDACTED
    assert out["input_tokens"] == 5
    assert out["s"].startswith("a" * 10) and "more chars" in out["s"]
