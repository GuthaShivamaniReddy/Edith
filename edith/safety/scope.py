"""File scope: which paths EDITH may ever ask to touch.

A path is in scope only if it resolves (symlinks and '..' followed) inside an
allowed root and outside every blocked root. Being in scope is necessary but not
sufficient: the user must still grant access (see policy.AccessGrants).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from edith.config import EdithConfig


class ScopeError(PermissionError):
    pass


@dataclass(frozen=True)
class FileScope:
    allowed: tuple[Path, ...]
    blocked: tuple[Path, ...]

    @classmethod
    def from_config(cls, cfg: EdithConfig) -> FileScope:
        return cls(tuple(cfg.files.resolved_allowed()), tuple(cfg.files.resolved_blocked()))

    def resolve(self, raw: str) -> Path:
        """Resolve a user/model-supplied path and check it is in scope, or raise ScopeError."""
        if not raw or not raw.strip():
            raise ScopeError("No path given.")
        expanded = Path(os.path.expandvars(raw.strip().strip('"'))).expanduser()
        if not expanded.is_absolute():
            expanded = Path.home() / expanded
        path = expanded.resolve()
        for b in self.blocked:
            if path == b or path.is_relative_to(b):
                raise ScopeError(f"{path} is in a blocked folder. EDITH can never access it.")
        if not any(path == a or path.is_relative_to(a) for a in self.allowed):
            roots = ", ".join(str(a) for a in self.allowed) or "(none configured)"
            raise ScopeError(f"{path} is outside the folders EDITH may access: {roots}.")
        return path

    def folder_for(self, path: Path) -> Path:
        """The folder a 'for this session' grant would cover for this path."""
        return path if path.is_dir() else path.parent
