"""SystemAdapter: the only place OS-specific system control lives (CLAUDE.md hard rule).

Methods are synchronous; tools call them through asyncio.to_thread. Every
command is a fixed template with validated arguments - never a shell string.
"""

from __future__ import annotations

import time
import webbrowser
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import mss
import mss.tools
import psutil
import pyperclip


class AdapterError(RuntimeError):
    pass


def validate_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise AdapterError("Only http:// and https:// web addresses can be opened.")
    return parsed.geturl()


class SystemAdapter(ABC):
    name = "base"

    @abstractmethod
    def get_volume(self) -> int: ...

    @abstractmethod
    def set_volume(self, level: int) -> None: ...

    @abstractmethod
    def set_mute(self, muted: bool) -> None: ...

    @abstractmethod
    def open_app(self, target: str) -> None:
        """Launch an app from the configured allowlist (target is the configured value)."""

    def open_url(self, url: str) -> None:
        if not webbrowser.open(validate_url(url)):
            raise AdapterError("No web browser could be opened.")

    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[0])  # all monitors
            mss.tools.to_png(shot.rgb, shot.size, output=str(path))
        return path

    def clipboard_read(self) -> str:
        return str(pyperclip.paste())

    def clipboard_write(self, text: str) -> None:
        pyperclip.copy(text)

    def stats(self) -> dict[str, Any]:
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(str(Path.home().anchor or "/"))
        out: dict[str, Any] = {
            "cpu_percent": psutil.cpu_percent(interval=0.3),
            "cpu_cores": psutil.cpu_count(logical=True),
            "memory_used_gb": round(vm.used / 1e9, 1),
            "memory_total_gb": round(vm.total / 1e9, 1),
            "memory_percent": vm.percent,
            "disk_used_gb": round(disk.used / 1e9, 1),
            "disk_total_gb": round(disk.total / 1e9, 1),
            "disk_percent": disk.percent,
            "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        }
        battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if battery is not None:
            out["battery_percent"] = round(battery.percent)
            out["battery_plugged_in"] = battery.power_plugged
        return out
