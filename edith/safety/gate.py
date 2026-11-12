"""The safety gate: the only path from the brain to the tools (PLAN.md section 7).

Wired into the Agent SDK in two places:
- PreToolUse hook -> pre_tool(): runs before EVERY tool call. Allow, deny, or
  'ask' (the decision is stashed for the confirmation step).
- can_use_tool    -> confirm(): runs for 'ask' calls. Shows the exact action
  to the user and waits; silence is a no.
PostToolUse -> post_tool() records the outcome.

Every decision, confirmation, grant and outcome goes to the audit log.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from edith.core.events import ConfirmRequestEvent, Event, ToolEndEvent, ToolStartEvent
from edith.safety.audit import AuditLog
from edith.safety.confirm import ConfirmationBroker
from edith.safety.policy import AccessGrants, Decision, Permissions, RequestSafety, decide
from edith.safety.scope import FileScope

Emit = Callable[[Event], Awaitable[None]]

DECLINED_MSG = (
    "The user declined this action. Do not retry it or try another way to do the same "
    "thing; tell the user it was not done and ask what they would like instead."
)
TIMEOUT_MSG = "The user did not answer the confirmation, so the action was not done."


class ActionDescriber(Protocol):
    """Anything that can describe a tool call in plain words (the ToolRegistry)."""

    def action(self, name: str, args: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    message: str = ""


class SafetyGate:
    """One per session: holds that session's access grants."""

    def __init__(
        self,
        session_id: str,
        permissions: Permissions,
        scope: FileScope,
        audit: AuditLog,
        broker: ConfirmationBroker,
        describer: ActionDescriber,
    ) -> None:
        self.session_id = session_id
        self.permissions = permissions
        self.scope = scope
        self.audit = audit
        self.broker = broker
        self.describer = describer
        self.grants = AccessGrants()
        self._stash: dict[str, Decision] = {}

    @staticmethod
    def _key(name: str, args: dict[str, Any], tool_use_id: str) -> str:
        return tool_use_id or f"{name}:{json.dumps(args, sort_keys=True, default=str)}"

    def decide(self, name: str, args: dict[str, Any], safety: RequestSafety) -> Decision:
        return decide(name, args, self.permissions, safety, self.grants, self.scope)

    async def pre_tool(
        self,
        name: str,
        args: dict[str, Any],
        tool_use_id: str,
        safety: RequestSafety,
        emit: Emit,
        via: str = "model",
    ) -> Decision:
        decision = self.decide(name, args, safety)
        self.audit.decision(
            name, tool_use_id, args, decision.tier, decision.verdict, decision.reason, via=via
        )
        if decision.verdict == "allow":
            await self._started(name, args, tool_use_id, safety, emit)
        elif decision.verdict == "deny":
            await emit(ToolEndEvent(name, False, f"Refused: {decision.reason}", tool_use_id))
        else:
            self._stash[self._key(name, args, tool_use_id)] = decision
        return decision

    async def confirm(
        self,
        name: str,
        args: dict[str, Any],
        tool_use_id: str,
        safety: RequestSafety,
        emit: Emit,
    ) -> GateResult:
        decision = self._stash.pop(self._key(name, args, tool_use_id), None)
        if decision is None:
            # Not seen by pre_tool (should not happen): decide again, and never auto-allow here.
            decision = self.decide(name, args, safety)
            if decision.verdict == "deny":
                return GateResult(False, decision.reason)
        grant = decision.grant
        confirm_id = self.broker.new_id()
        await emit(
            ConfirmRequestEvent(
                id=confirm_id,
                tool=name,
                action=self.describer.action(name, args),
                reason=decision.reason,
                kind=decision.kind or "consequential",
                tier=decision.tier,
                allow_session=bool(grant and grant.allow_session),
                grant_label=_grant_label(grant.scope, grant.target) if grant else "",
            )
        )
        resp = await self.broker.wait_confirm(confirm_id, self.session_id)
        self.audit.confirmation(name, tool_use_id, resp.approved, resp.scope, resp.timed_out)
        if not resp.approved:
            msg = TIMEOUT_MSG if resp.timed_out else DECLINED_MSG
            note = "no answer" if resp.timed_out else "you declined"
            await emit(ToolEndEvent(name, False, f"Not done ({note})", tool_use_id))
            return GateResult(False, msg)
        if grant and grant.allow_session and resp.scope == "session":
            self.grants.grant(grant.scope, grant.target)
            self.audit.grant(grant.scope, grant.target)
        await self._started(name, args, tool_use_id, safety, emit)
        return GateResult(True)

    async def post_tool(
        self, name: str, tool_use_id: str, ok: bool, summary: str, emit: Emit
    ) -> None:
        self.audit.outcome(name, tool_use_id, ok, summary)
        await emit(ToolEndEvent(name, ok, summary, tool_use_id))

    async def _started(
        self,
        name: str,
        args: dict[str, Any],
        tool_use_id: str,
        safety: RequestSafety,
        emit: Emit,
    ) -> None:
        # Data-flow flags are set as soon as a tool is cleared to run, so every later
        # decision in this request sees them.
        safety.note_ran(name, self.permissions)
        await emit(ToolStartEvent(name, self.describer.action(name, args), tool_use_id))


def _grant_label(scope: str, target: str) -> str:
    labels = {
        "files": f"the folder {target}",
        "system_info": "system information",
        "clipboard": "your clipboard",
        "screen": "your screen",
    }
    return labels.get(scope, scope)
