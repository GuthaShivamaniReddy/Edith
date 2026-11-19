"""Windows system control: pycaw for audio, ShellExecute for allowlisted apps."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from edith.tools.system.base import AdapterError, SystemAdapter, validate_shortcut_name

T = TypeVar("T")


@contextmanager
def _com() -> Iterator[None]:
    """COM must be initialised on each worker thread that touches audio."""
    import comtypes

    comtypes.CoInitialize()
    try:
        yield
    finally:
        comtypes.CoUninitialize()


def _endpoint() -> Any:
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetSpeakers().EndpointVolume


class WindowsAdapter(SystemAdapter):
    name = "windows"

    def _audio(self, fn: Callable[[Any], T]) -> T:
        try:
            with _com():
                return fn(_endpoint())
        except OSError as e:
            raise AdapterError(f"Could not reach the audio device: {e}") from e

    def get_volume(self) -> int:
        return self._audio(lambda ev: round(float(ev.GetMasterVolumeLevelScalar()) * 100))

    def set_volume(self, level: int) -> None:
        self._audio(lambda ev: ev.SetMasterVolumeLevelScalar(level / 100, None))

    def set_mute(self, muted: bool) -> None:
        self._audio(lambda ev: ev.SetMute(1 if muted else 0, None))

    def open_app(self, target: str) -> None:
        try:
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606 - allowlisted target
        except OSError as e:
            raise AdapterError(f"Could not open {target}: {e}") from e

    def install_shortcut(
        self, name: str, target: Path, args: list[str], workdir: Path, icon: Path | None
    ) -> Path:
        link = _start_menu() / f"{validate_shortcut_name(name)}.lnk"
        try:
            import comtypes.client

            with _com():
                shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
                sc = shell.CreateShortcut(str(link))
                sc.TargetPath = str(target)
                sc.Arguments = subprocess.list2cmdline(args)
                sc.WorkingDirectory = str(workdir)
                if icon is not None:
                    sc.IconLocation = f"{icon},0"
                sc.Description = "EDITH - your personal AI assistant"
                sc.Save()
        except OSError as e:
            raise AdapterError(f"Could not create the shortcut: {e}") from e
        return link

    def remove_shortcut(self, name: str) -> Path | None:
        link = _start_menu() / f"{validate_shortcut_name(name)}.lnk"
        if not link.exists():
            return None
        link.unlink()  # our own .lnk file, never anything else
        return link


def _start_menu() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise AdapterError("APPDATA is not set; cannot find the Start menu.")
    folder = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder
