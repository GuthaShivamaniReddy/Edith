"""Unit tests for each in-process tool, run through the registry on the fake adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from edith.config import EdithConfig
from edith.core.events import NotificationEvent
from edith.safety.audit import AuditLog
from edith.safety.policy import Permissions
from edith.safety.scope import FileScope
from edith.tools.base import ToolContext, ToolSpec
from edith.tools.registry import RegistryError, ToolRegistry, default_specs
from edith.tools.system.fake import FakeAdapter
from edith.tools.timers import TimerService, human_duration


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def notified() -> list[NotificationEvent]:
    return []


@pytest.fixture
def registry(
    tool_cfg: EdithConfig,
    permissions: Permissions,
    adapter: FakeAdapter,
    notified: list[NotificationEvent],
) -> ToolRegistry:
    ctx = ToolContext(
        tool_cfg,
        adapter,
        FileScope.from_config(tool_cfg),
        TimerService(notified.append),
        AuditLog(tool_cfg.data_dir / "audit.jsonl"),
    )
    return ToolRegistry(ctx, permissions)


# ---------------------------------------------------------------- registry


def test_every_tool_has_an_explicit_tier(permissions: Permissions) -> None:
    for spec in default_specs():
        assert permissions.explicit_tier(spec.policy_name) is not None, spec.policy_name


def test_registry_refuses_tool_without_tier(
    tool_cfg: EdithConfig, permissions: Permissions, adapter: FakeAdapter
) -> None:
    async def handler(args: dict[str, object], ctx: ToolContext) -> str:
        return ""

    rogue = ToolSpec("rogue", "act", "no tier", {"type": "object"}, handler)
    ctx = ToolContext(
        tool_cfg,
        adapter,
        FileScope.from_config(tool_cfg),
        TimerService(lambda e: None),
        AuditLog(tool_cfg.data_dir / "a.jsonl"),
    )
    with pytest.raises(RegistryError, match=r"rogue\.act"):
        ToolRegistry(ctx, permissions, specs=[rogue])


def test_mcp_servers_group_tools(registry: ToolRegistry) -> None:
    servers = registry.mcp_servers()
    assert {"files", "system", "apps", "web", "clipboard", "notes", "timers", "activity"} <= set(
        servers
    )


async def test_unknown_tool_and_crash_are_reported_not_raised(registry: ToolRegistry) -> None:
    assert await registry.call("nope.nothing", {}) == (False, "Unknown tool nope.nothing.")
    ok, text = await registry.call("system.set_volume", {"level": "loud"})
    assert not ok and "0 to 100" in text


# ---------------------------------------------------------------- system


async def test_volume_set_is_verified_and_clamped(
    registry: ToolRegistry, adapter: FakeAdapter
) -> None:
    assert await registry.call("system.set_volume", {"level": 140}) == (True, "Volume is now 100%.")
    assert adapter.volume == 100
    assert (await registry.call("system.get_volume", {}))[1] == "Volume is 100%."


async def test_mute_and_unmute(registry: ToolRegistry, adapter: FakeAdapter) -> None:
    await registry.call("system.mute", {"muted": True})
    assert adapter.muted
    await registry.call("system.mute", {"muted": False})
    assert not adapter.muted


async def test_stats(registry: ToolRegistry) -> None:
    ok, text = await registry.call("system.stats", {})
    assert ok and "cpu_percent" in text


async def test_screenshot_saved_in_data_dir(
    registry: ToolRegistry, adapter: FakeAdapter, tool_cfg: EdithConfig
) -> None:
    ok, _ = await registry.call("system.screenshot", {})
    assert ok and adapter.screenshots[0].is_relative_to(tool_cfg.data_dir)


async def test_open_app_only_from_allowlist(registry: ToolRegistry, adapter: FakeAdapter) -> None:
    assert (await registry.call("apps.open", {"name": "Notepad"}))[0]
    assert adapter.opened_apps == ["notepad.exe"]
    ok, text = await registry.call("apps.open", {"name": "cmd.exe /c del *"})
    assert not ok and "not an app EDITH may open" in text
    assert adapter.opened_apps == ["notepad.exe"]


@pytest.mark.parametrize(
    "url", ["file:///C:/Windows/system32", "javascript:alert(1)", "ms-settings:", "example.com"]
)
async def test_open_url_only_http(registry: ToolRegistry, adapter: FakeAdapter, url: str) -> None:
    ok, _ = await registry.call("web.open_url", {"url": url})
    assert not ok and adapter.opened_urls == []


async def test_open_url(registry: ToolRegistry, adapter: FakeAdapter) -> None:
    assert (await registry.call("web.open_url", {"url": "https://example.com/a"}))[0]
    assert adapter.opened_urls == ["https://example.com/a"]


async def test_clipboard_round_trip(registry: ToolRegistry, adapter: FakeAdapter) -> None:
    assert (await registry.call("clipboard.read", {}))[1] == "The clipboard is empty."
    await registry.call("clipboard.write", {"text": "hello"})
    assert adapter.clipboard == "hello"
    ok, text = await registry.call("clipboard.read", {})
    assert ok and "hello" in text and "untrusted" in text


# ---------------------------------------------------------------- files


async def test_files_list_and_search(registry: ToolRegistry, sandbox: Path) -> None:
    ok, text = await registry.call("files.list", {"path": str(sandbox)})
    assert ok and "docs" in text and "untrusted" in text
    ok, text = await registry.call("files.search", {"path": str(sandbox), "query": "todo"})
    assert ok and "todo.txt" in text


async def test_files_read_marks_content_untrusted(registry: ToolRegistry, sandbox: Path) -> None:
    ok, text = await registry.call("files.read", {"path": str(sandbox / "docs" / "todo.txt")})
    assert ok and "buy milk" in text and "never follow instructions" in text


async def test_files_tools_recheck_scope(registry: ToolRegistry, sandbox: Path) -> None:
    """Defence in depth: tools refuse blocked paths even if the gate were bypassed."""
    ok, text = await registry.call("files.read", {"path": str(sandbox / "secret" / "salary.txt")})
    assert not ok and "blocked" in text
    ok, _ = await registry.call("files.list", {"path": str(sandbox / "secret")})
    assert not ok


async def test_files_read_rejects_binary_and_later_phase(
    registry: ToolRegistry, sandbox: Path
) -> None:
    (sandbox / "docs" / "a.pdf").write_bytes(b"%PDF")
    (sandbox / "docs" / "b.txt").write_bytes(b"\x00\x01\x02")
    assert (
        "Phase 2"
        in (await registry.call("files.read", {"path": str(sandbox / "docs" / "a.pdf")}))[1]
    )
    assert (
        "binary"
        in (await registry.call("files.read", {"path": str(sandbox / "docs" / "b.txt")}))[1]
    )


async def test_trash_refuses_root_and_missing(
    registry: ToolRegistry, sandbox: Path, adapter: FakeAdapter
) -> None:
    assert not (await registry.call("files.trash", {"path": str(sandbox)}))[0]
    assert not (await registry.call("files.trash", {"path": str(sandbox / "nope.txt")}))[0]
    target = sandbox / "docs" / "todo.txt"
    assert (await registry.call("files.trash", {"path": str(target)}))[0]
    assert not target.exists() and adapter.trashed == [target.resolve()]


# ---------------------------------------------------------------- notes


async def test_notes_create_list_read(registry: ToolRegistry, tool_cfg: EdithConfig) -> None:
    ok, _text = await registry.call("notes.create", {"title": "Groceries", "text": "eggs"})
    assert ok
    ok, listing = await registry.call("notes.list", {})
    assert ok and "Groceries" in listing
    ok, body = await registry.call("notes.read", {"name": "Groceries"})
    assert ok and "eggs" in body
    assert all(p.is_relative_to(tool_cfg.notes_dir) for p in tool_cfg.notes_dir.rglob("*"))


async def test_notes_cannot_escape_folder(
    registry: ToolRegistry, sandbox: Path, tool_cfg: EdithConfig
) -> None:
    await registry.call("notes.create", {"title": "../../evil", "text": "x"})
    created = await asyncio.to_thread(lambda: list(sandbox.parent.rglob("*evil*")))
    assert created and all(p.parent == tool_cfg.notes_dir for p in created)
    ok, _ = await registry.call("notes.read", {"name": str(sandbox / "docs" / "todo.txt")})
    assert not ok


# ---------------------------------------------------------------- timers


async def test_timers_set_list_cancel_and_fire(
    registry: ToolRegistry, notified: list[NotificationEvent]
) -> None:
    ok, text = await registry.call("timers.set", {"minutes": 5, "label": "pasta"})
    assert ok and "pasta" in text
    assert "pasta" in (await registry.call("timers.list", {}))[1]
    assert (await registry.call("timers.cancel", {"id": "timer 1"}))[0]
    assert (await registry.call("timers.list", {}))[1] == "No timers running."
    await registry.call("timers.set", {"seconds": 1, "label": "tea"})
    await asyncio.sleep(1.2)
    assert notified and "tea" in notified[0].text


async def test_timer_limits(registry: ToolRegistry) -> None:
    assert not (await registry.call("timers.set", {}))[0]
    assert not (await registry.call("timers.set", {"hours": 25}))[0]
    assert not (await registry.call("timers.cancel", {"id": "99"}))[0]


def test_human_duration() -> None:
    assert human_duration(3725) == "1 hour 2 minutes 5 seconds"
    assert human_duration(60) == "1 minute"


# ---------------------------------------------------------------- activity


async def test_activity_today_reads_audit(registry: ToolRegistry, tool_cfg: EdithConfig) -> None:
    assert (await registry.call("activity.today", {}))[1] == "No actions taken today."
    audit = registry.ctx.audit
    audit.outcome("system.set_volume", "t1", True, "Volume is now 30%.")
    audit.decision("payments.charge", "t2", {}, 3, "deny", "tier 3")
    audit.confirmation("files.trash", "t3", False, "once", True)
    ok, text = await registry.call("activity.today", {})
    assert ok and "set_volume: done" in text
    assert "payments.charge: refused" in text and "no answer" in text
