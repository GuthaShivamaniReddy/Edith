"""Tool specs and the context tools run with.

A tool is plain async Python: handler(args, ctx) -> text. The registry turns
specs into in-process MCP servers. Safety properties (tier, access scope,
data-flow labels) are NOT declared here: they live in config/permissions.yaml
so a tool cannot grant itself privileges.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from edith.config import EdithConfig
from edith.safety.audit import AuditLog
from edith.safety.scope import FileScope

if TYPE_CHECKING:
    from edith.tools.system.base import SystemAdapter
    from edith.tools.timers import TimerService

ALL_PLATFORMS = frozenset({"win32", "darwin", "linux"})


class ToolError(Exception):
    """A failure to report to the model as the tool's result (message is user-safe)."""


@dataclass
class ToolContext:
    cfg: EdithConfig
    adapter: SystemAdapter
    scope: FileScope
    timers: TimerService
    audit: AuditLog


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[str]]
Describer = Callable[[dict[str, Any]], str]


def schema(
    properties: dict[str, dict[str, Any]], required: list[str] | None = None
) -> dict[str, Any]:
    """A strict JSON Schema object for tool input."""
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class ToolSpec:
    group: str
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    describe: Describer | None = None  # the exact action, shown in confirmations
    platforms: frozenset[str] = field(default=ALL_PLATFORMS)
    needs_network: bool = False
    timeout_s: float = 15.0

    @property
    def policy_name(self) -> str:
        return f"{self.group}.{self.name}"

    def action(self, args: dict[str, Any]) -> str:
        if self.describe is not None:
            try:
                return self.describe(args)
            except (KeyError, TypeError, ValueError):
                pass
        return f"{self.policy_name} {json.dumps(args, ensure_ascii=False)[:300]}"
