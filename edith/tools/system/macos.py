"""macOS system control via osascript and `open`, with fixed argument templates."""

from __future__ import annotations

import subprocess

from edith.tools.system.base import AdapterError, SystemAdapter


def _run(args: list[str]) -> str:
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=10, check=True)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as e:
        raise AdapterError(f"{args[0]} failed: {e}") from e
    return res.stdout.strip()


class MacAdapter(SystemAdapter):
    name = "macos"

    def get_volume(self) -> int:
        return int(_run(["osascript", "-e", "output volume of (get volume settings)"]) or 0)

    def set_volume(self, level: int) -> None:
        _run(["osascript", "-e", f"set volume output volume {int(level)}"])

    def set_mute(self, muted: bool) -> None:
        _run(["osascript", "-e", f"set volume output muted {'true' if muted else 'false'}"])

    def open_app(self, target: str) -> None:
        _run(["open", "-a", target])
