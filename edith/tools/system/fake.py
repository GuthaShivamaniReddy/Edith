"""In-memory adapter for tests and evals: records what EDITH did, touches nothing real."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from edith.tools.system.base import SystemAdapter, validate_url


class FakeAdapter(SystemAdapter):
    name = "fake"

    def __init__(self) -> None:
        self.volume = 50
        self.muted = False
        self.opened_apps: list[str] = []
        self.opened_urls: list[str] = []
        self.clipboard = ""
        self.screenshots: list[Path] = []
        self.trashed: list[Path] = []

    def get_volume(self) -> int:
        return self.volume

    def set_volume(self, level: int) -> None:
        self.volume = level

    def set_mute(self, muted: bool) -> None:
        self.muted = muted

    def open_app(self, target: str) -> None:
        self.opened_apps.append(target)

    def open_url(self, url: str) -> None:
        self.opened_urls.append(validate_url(url))

    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG fake")
        self.screenshots.append(path)
        return path

    def trash(self, path: Path) -> None:
        """Deletes for real (tests/evals use temp folders) but never touches the Recycle Bin."""
        if path.is_dir():
            import shutil

            shutil.rmtree(path)
        else:
            path.unlink()
        self.trashed.append(path)

    def clipboard_read(self) -> str:
        return self.clipboard

    def clipboard_write(self, text: str) -> None:
        self.clipboard = text

    def stats(self) -> dict[str, Any]:
        return {
            "cpu_percent": 12.5,
            "memory_used_gb": 6.2,
            "memory_total_gb": 16.0,
            "memory_percent": 38.8,
            "disk_used_gb": 210.0,
            "disk_total_gb": 512.0,
            "disk_percent": 41.0,
            "battery_percent": 80,
            "battery_plugged_in": True,
        }
