"""The safety gate's decision: allow, ask, or deny one tool call.

Order of checks (first match wins):
1. Tier 3                                   -> deny
2. Needs an access grant the user hasn't given -> ask (access)      [privacy]
   ... or the target is out of scope        -> deny
3. Outbound tool after local data was read  -> ask (outbound)       [privacy: no leaks]
4. Tier 2                                   -> ask (consequential)
5. Tier 1 after untrusted content was read  -> ask (untrusted)      [prompt injection]
6. Otherwise                                -> allow
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

import yaml

from edith.safety.scope import FileScope, ScopeError

Verdict = Literal["allow", "ask", "deny"]
AskKind = Literal["access", "outbound", "consequential", "untrusted"]

# Built-in Claude Code tools EDITH enables, mapped to policy names. Any other built-in
# that ever shows up is unknown and therefore tier 2.
BUILTIN_NAMES = {
    "WebSearch": "web.search",
    "WebFetch": "web.fetch",
    "AskUserQuestion": "question.ask",
}


def policy_name(sdk_name: str) -> str:
    """'mcp__files__read' -> 'files.read'; 'WebSearch' -> 'web.search'."""
    if sdk_name in BUILTIN_NAMES:
        return BUILTIN_NAMES[sdk_name]
    if sdk_name.startswith("mcp__"):
        parts = sdk_name.split("__", 2)
        if len(parts) == 3:
            return f"{parts[1]}.{parts[2]}"
    return f"builtin.{sdk_name}"


@dataclass(frozen=True)
class AccessRule:
    scope: str  # files | clipboard | screen | system_info
    session: bool  # may the user grant it for the whole session?


@dataclass(frozen=True)
class GrantRequest:
    scope: str
    target: str  # folder for files; "" for clipboard/screen/system_info
    allow_session: bool


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    tier: int
    reason: str
    kind: AskKind | None = None
    grant: GrantRequest | None = None


class PolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Permissions:
    default_tier: int
    tiers: dict[str, int]
    access: dict[str, AccessRule]
    local_data: frozenset[str]
    outbound: frozenset[str]
    untrusted_output: frozenset[str]

    @classmethod
    def load(cls, path: Path) -> Permissions:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise PolicyError(f"Cannot load permissions from {path}: {e}") from e
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Permissions:
        try:
            default = int(raw.get("default_tier", 2))
            tiers = {str(k): int(v) for k, v in (raw.get("tools") or {}).items()}
            access = {
                str(k): AccessRule(scope=str(v["scope"]), session=bool(v.get("session", False)))
                for k, v in (raw.get("access") or {}).items()
            }
        except (TypeError, ValueError, KeyError) as e:
            raise PolicyError(f"Invalid permissions file: {e}") from e
        for name, tier in [*tiers.items(), ("default_tier", default)]:
            if tier not in (0, 1, 2, 3):
                raise PolicyError(f"Tier for {name} must be 0-3, got {tier}")
        if default < 2:
            raise PolicyError("default_tier must be 2 or 3: unknown tools always ask")
        return cls(
            default_tier=default,
            tiers=tiers,
            access=access,
            local_data=frozenset(raw.get("local_data") or []),
            outbound=frozenset(raw.get("outbound") or []),
            untrusted_output=frozenset(raw.get("untrusted_output") or []),
        )

    def explicit_tier(self, name: str) -> int | None:
        if name in self.tiers:
            return self.tiers[name]
        matches = [t for pat, t in self.tiers.items() if "*" in pat and fnmatchcase(name, pat)]
        return max(matches) if matches else None  # most restrictive wildcard wins

    def tier_for(self, name: str) -> int:
        tier = self.explicit_tier(name)
        return self.default_tier if tier is None else tier


@dataclass
class RequestSafety:
    """Data-flow state for one request."""

    local_data_read: bool = False
    untrusted_read: bool = False

    def note_ran(self, name: str, perms: Permissions) -> None:
        if name in perms.local_data:
            self.local_data_read = True
        if name in perms.untrusted_output:
            self.untrusted_read = True


@dataclass
class AccessGrants:
    """What the user has allowed for this session. Starts empty; never written to disk."""

    _granted: set[tuple[str, str]] = field(default_factory=set)

    def covers(self, scope: str, target: str) -> bool:
        if scope == "files":
            path = Path(target)
            return any(
                s == "files" and (path == Path(t) or path.is_relative_to(Path(t)))
                for s, t in self._granted
            )
        return (scope, target) in self._granted

    def grant(self, scope: str, target: str) -> None:
        self._granted.add((scope, target))

    def revoke_all(self) -> None:
        self._granted.clear()

    def describe(self) -> list[str]:
        return sorted(f"{s}: {t}" if t else s for s, t in self._granted)


def access_target(name: str, args: dict[str, Any], perms: Permissions, scope: FileScope) -> str:
    """The resolved thing a tool call wants to access ('' for non-file scopes).

    Raises ScopeError when a file path is out of scope.
    """
    rule = perms.access.get(name)
    if rule is None or rule.scope != "files":
        return ""
    raw = args.get("path") or args.get("folder") or ""
    if not isinstance(raw, str) or not raw:
        raise ScopeError("This tool needs a 'path' inside an allowed folder.")
    return str(scope.resolve(raw))


def decide(
    name: str,
    args: dict[str, Any],
    perms: Permissions,
    safety: RequestSafety,
    grants: AccessGrants,
    scope: FileScope,
) -> Decision:
    tier = perms.tier_for(name)

    if tier >= 3:
        return Decision("deny", tier, f"{name} is tier 3 (forbidden): EDITH never does this.")

    rule = perms.access.get(name)
    if rule is not None:
        try:
            target = access_target(name, args, perms, scope)
        except ScopeError as e:
            return Decision("deny", tier, str(e))
        if not grants.covers(rule.scope, target):
            grant_target = (
                str(scope.folder_for(Path(target))) if rule.scope == "files" and target else ""
            )
            return Decision(
                "ask",
                tier,
                f"{name} needs your permission to access {target or rule.scope}.",
                kind="access",
                grant=GrantRequest(rule.scope, grant_target, rule.session),
            )

    if name in perms.outbound and safety.local_data_read:
        return Decision(
            "ask",
            tier,
            "This request has read data from your computer; check exactly what would be sent "
            "to the internet before allowing it.",
            kind="outbound",
        )

    if tier == 2:
        return Decision("ask", tier, f"{name} is tier 2 (consequential).", kind="consequential")

    if tier == 1 and safety.untrusted_read:
        return Decision(
            "ask",
            tier,
            "Raised to tier 2: this request has read untrusted content (web, files or clipboard).",
            kind="untrusted",
        )

    return Decision("allow", tier, f"tier {tier}")
