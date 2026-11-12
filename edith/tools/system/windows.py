"""Windows system control: pycaw for audio, ShellExecute for allowlisted apps."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from edith.tools.system.base import AdapterError, SystemAdapter

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
