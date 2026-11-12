"""System control behind one SystemAdapter interface (Windows, macOS, Linux, fake)."""

from __future__ import annotations

import os
import sys

from edith.tools.system.base import SystemAdapter


def get_adapter() -> SystemAdapter:
    """The adapter for this OS. EDITH_FAKE_SYSTEM=1 gives an in-memory fake (tests, evals)."""
    if os.environ.get("EDITH_FAKE_SYSTEM") == "1":
        from edith.tools.system.fake import FakeAdapter

        return FakeAdapter()
    if sys.platform == "win32":
        from edith.tools.system.windows import WindowsAdapter

        return WindowsAdapter()
    if sys.platform == "darwin":
        from edith.tools.system.macos import MacAdapter

        return MacAdapter()
    from edith.tools.system.linux import LinuxAdapter

    return LinuxAdapter()


__all__ = ["SystemAdapter", "get_adapter"]
