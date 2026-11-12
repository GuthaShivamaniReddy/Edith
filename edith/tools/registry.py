"""Tool registry: loads tools, validates them against the permission policy, and
exposes them to the brain as in-process MCP servers.

The service refuses to start if any in-process tool has no explicit tier in
config/permissions.yaml (PLAN.md section 6).
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import McpServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

from edith.observability.logging import get_logger
from edith.safety.policy import Permissions
from edith.tools import activity, files, notes, timers
from edith.tools.base import ToolContext, ToolError, ToolSpec
from edith.tools.system import tools as system_tools

log = get_logger(__name__)

MAX_RESULT_CHARS = 30_000

# How built-in tool calls are described in confirmations.
_BUILTIN_ACTIONS = {
    "web.search": lambda a: f"Search the web for: {a.get('query', '')!r}",
    "web.fetch": lambda a: f"Fetch the web page {a.get('url', '')}",
}


class RegistryError(RuntimeError):
    pass


def default_specs() -> list[ToolSpec]:
    return [
        *system_tools.specs(),
        *files.specs(),
        *notes.specs(),
        *timers.specs(),
        *activity.specs(),
    ]


def load_external_servers(path: Path) -> dict[str, McpServerConfig]:
    """External MCP servers from config/mcp_servers.yaml (stdio/http configs, passed through)."""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    servers = raw.get("servers") or {}
    if not isinstance(servers, dict):
        raise RegistryError(f"'servers' in {path} must be a mapping")
    return {str(k): v for k, v in servers.items()}


class ToolRegistry:
    def __init__(
        self,
        ctx: ToolContext,
        permissions: Permissions,
        specs: list[ToolSpec] | None = None,
        external: dict[str, McpServerConfig] | None = None,
    ) -> None:
        self.ctx = ctx
        self.permissions = permissions
        all_specs = default_specs() if specs is None else specs
        self.specs: dict[str, ToolSpec] = {
            s.policy_name: s for s in all_specs if sys.platform in s.platforms
        }
        self.external = external or {}
        self._validate()

    def _validate(self) -> None:
        missing = [n for n in self.specs if self.permissions.explicit_tier(n) is None]
        if missing:
            raise RegistryError(
                "These tools have no tier in config/permissions.yaml, so EDITH will not "
                f"start: {', '.join(sorted(missing))}"
            )
        clash = set(self.external) & {s.group for s in self.specs.values()}
        if clash:
            raise RegistryError(f"External MCP server names clash with built-in groups: {clash}")

    def get(self, name: str) -> ToolSpec | None:
        return self.specs.get(name)

    def action(self, name: str, args: dict[str, Any]) -> str:
        """Human description of the exact action, for confirmations and the UI."""
        spec = self.specs.get(name)
        if spec is not None:
            return spec.action(args)
        if name in _BUILTIN_ACTIONS:
            return _BUILTIN_ACTIONS[name](args)
        return f"{name} {json.dumps(args, ensure_ascii=False)[:300]}"

    async def call(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        """Run a tool. Never raises: returns (ok, text-for-the-model)."""
        spec = self.specs.get(name)
        if spec is None:
            return False, f"Unknown tool {name}."
        try:
            text = await asyncio.wait_for(spec.handler(args, self.ctx), spec.timeout_s)
        except ToolError as e:
            return False, str(e)
        except TimeoutError:
            return False, f"{name} timed out after {spec.timeout_s:.0f}s."
        except Exception as e:
            log.exception("tool_crashed", tool=name)
            return False, f"{name} failed unexpectedly ({type(e).__name__})."
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n... [truncated]"
        return True, text

    def _sdk_tool(self, spec: ToolSpec) -> SdkMcpTool[Any]:
        registry = self

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            ok, text = await registry.call(spec.policy_name, args)
            return {"content": [{"type": "text", "text": text}], "is_error": not ok}

        return tool(spec.name, spec.description, spec.input_schema)(handler)

    def mcp_servers(self) -> dict[str, McpServerConfig]:
        groups: dict[str, list[ToolSpec]] = defaultdict(list)
        for spec in self.specs.values():
            groups[spec.group].append(spec)
        servers: dict[str, McpServerConfig] = {
            group: create_sdk_mcp_server(group, tools=[self._sdk_tool(s) for s in specs])
            for group, specs in groups.items()
        }
        servers.update(self.external)
        return servers
