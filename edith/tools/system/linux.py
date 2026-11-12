"""Linux system control via pactl and launching allowlisted executables, fixed templates."""

from __future__ import annotations

import re
import shutil
import subprocess

from edith.tools.system.base import AdapterError, SystemAdapter


def _run(args: list[str]) -> str:
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=10, check=True)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as e:
        raise AdapterError(f"{args[0]} failed: {e}") from e
    return res.stdout.strip()


class LinuxAdapter(SystemAdapter):
    name = "linux"

    def get_volume(self) -> int:
        out = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
        m = re.search(r"(\d+)%", out)
        return int(m.group(1)) if m else 0

    def set_volume(self, level: int) -> None:
        _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{int(level)}%"])

    def set_mute(self, muted: bool) -> None:
        _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"])

    def open_app(self, target: str) -> None:
        exe = shutil.which(target)
        if exe is None:
            raise AdapterError(f"{target} is not installed.")
        subprocess.Popen([exe], start_new_session=True)  # noqa: S603
