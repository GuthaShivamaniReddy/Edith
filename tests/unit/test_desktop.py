from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from edith.config import EdithConfig
from edith.interfaces import desktop
from edith.interfaces.cli import app
from edith.interfaces.desktop import Controller, DesktopError, ServerThread
from edith.interfaces.server import create_app
from edith.safety.policy import Permissions
from edith.tools.system.base import AdapterError
from edith.tools.system.fake import FakeAdapter
from tests.fakes import fake_orchestrator, text_turn


class FakeWindow:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def show(self) -> None:
        self.calls.append("show")

    def hide(self) -> None:
        self.calls.append("hide")

    def restore(self) -> None:
        self.calls.append("restore")

    def destroy(self) -> None:
        self.calls.append("destroy")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_close_hides_to_tray_and_hints_once() -> None:
    notes: list[str] = []
    win = FakeWindow()
    ctl = Controller(notify=notes.append, window=win)
    assert ctl.on_closing() is False
    assert ctl.on_closing() is False
    assert win.calls == ["hide", "hide"]
    assert len(notes) == 1


def test_quit_really_closes() -> None:
    win = FakeWindow()
    ctl = Controller(window=win)
    ctl.quit()
    assert win.calls == ["destroy"]
    assert ctl.on_closing() is True  # the close that destroy() triggers goes through
    ctl.show()
    assert win.calls == ["destroy"]  # no showing a window that is going away


def test_show_restores_window() -> None:
    win = FakeWindow()
    Controller(window=win).show()
    assert win.calls == ["show", "restore"]


def test_icon_is_drawn_and_saved_as_ico(tmp_path: Path) -> None:
    img = desktop.make_icon(32)
    assert img.size == (32, 32) and img.mode == "RGBA"
    assert img.getpixel((16, 16)) != (0, 0, 0, 0)  # something drawn in the middle
    path = desktop.write_icon(tmp_path / "x" / "edith.ico")
    with Image.open(path) as ico:
        assert ico.format == "ICO"


def test_base_url(cfg: EdithConfig) -> None:
    assert desktop.base_url(cfg) == f"http://127.0.0.1:{cfg.server.port}"


def test_probe_refuses_non_loopback() -> None:
    with pytest.raises(DesktopError):
        desktop.probe("http://example.com:8765")


def test_probe_nothing_listening() -> None:
    port = free_port()
    assert desktop.probe(f"http://127.0.0.1:{port}", timeout=0.5) is None
    assert desktop.port_in_use("127.0.0.1", port) is False


def test_server_thread_starts_answers_and_stops(
    tool_cfg: EdithConfig, permissions: Permissions
) -> None:
    cfg = tool_cfg.model_copy(
        update={"server": tool_cfg.server.model_copy(update={"port": free_port()})}
    )
    orch, _ = fake_orchestrator(cfg, permissions, text_turn("hi", 0.0), adapter=FakeAdapter())
    server = ServerThread(cfg, create_app(cfg, orch))
    server.start(timeout=15)
    try:
        url = desktop.base_url(cfg)
        assert desktop.probe(url) is not None
        assert desktop.port_in_use("127.0.0.1", cfg.server.port)
    finally:
        server.stop()
    assert desktop.probe(desktop.base_url(cfg), timeout=0.5) is None


def test_launcher_runs_desktop_module() -> None:
    target, args = desktop.launcher()
    assert target.parent == Path(sys.executable).parent
    assert args == ["-m", "edith.interfaces.desktop"]


def test_fake_adapter_shortcuts() -> None:
    a = FakeAdapter()
    link = a.install_shortcut("EDITH", Path("py.exe"), ["-m", "x"], Path("."), None)
    assert link.name == "EDITH.lnk" and "EDITH" in a.shortcuts
    assert a.remove_shortcut("EDITH") == link
    assert a.remove_shortcut("EDITH") is None


@pytest.mark.parametrize("name", ["..\\evil", "a/b", "C:x", "", ".hidden", "x" * 60])
def test_shortcut_names_are_plain(name: str) -> None:
    with pytest.raises(AdapterError):
        FakeAdapter().install_shortcut(name, Path("py.exe"), [], Path("."), None)


def test_cli_shortcut_install_and_remove(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EDITH_CONFIG_DIR", str(config_file.parent))
    monkeypatch.setenv("EDITH_FAKE_SYSTEM", "1")
    fake = FakeAdapter()
    monkeypatch.setattr("edith.tools.system.get_adapter", lambda: fake)
    runner = CliRunner()
    res = runner.invoke(app, ["app", "--shortcut"])
    assert res.exit_code == 0, res.output
    assert fake.shortcuts["EDITH"]["args"] == ["-m", "edith.interfaces.desktop"]
    assert Path(fake.shortcuts["EDITH"]["icon"]).exists()
    res = runner.invoke(app, ["app", "--remove-shortcut"])
    assert res.exit_code == 0 and "Removed" in res.output
    assert fake.shortcuts == {}
