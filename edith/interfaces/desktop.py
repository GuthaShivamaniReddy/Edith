"""EDITH as a Windows app: the `edith serve` page in a native window, with a tray icon.

A thin face over the same local server (ADR 0007, ADR 0012). The window is WebView2
(through pywebview) pointed at http://127.0.0.1:<port>, so it passes the server's
same-origin and loopback-host checks unchanged. Closing the window hides it to the
tray; "Quit" in the tray menu stops EDITH. If `edith serve` is already running, the
window attaches to it instead of starting a second server.

The Tauri + React HUD in PLAN.md Phase 7 replaces this window; the server stays.

Run with `edith app`, or from the Start menu after `edith app --shortcut`.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageDraw

from edith import __version__
from edith.config import EdithConfig
from edith.observability.logging import get_logger

log = get_logger(__name__)

APP_NAME = "EDITH"
WINDOW_SIZE = (480, 760)
MIN_SIZE = (360, 480)
START_TIMEOUT_S = 30.0


class DesktopError(RuntimeError):
    pass


def base_url(cfg: EdithConfig) -> str:
    host = "[::1]" if cfg.server.host == "::1" else cfg.server.host
    return f"http://{host}:{cfg.server.port}"


def probe(url: str, timeout: float = 1.0) -> str | None:
    """The version of the EDITH server at url, or None if nothing (or not EDITH) answers."""
    if not url.startswith(("http://127.0.0.1:", "http://localhost:", "http://[::1]:")):
        raise DesktopError(f"Refusing to probe a non-loopback address: {url}")
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as resp:  # noqa: S310 - loopback only
            import json

            data = json.loads(resp.read(4096))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("status") == "ok":
        return str(data.get("version", "?"))
    return None


def port_in_use(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


class ServerThread:
    """uvicorn on a background thread, so the window can own the main thread."""

    def __init__(self, cfg: EdithConfig, app: Any | None = None) -> None:
        import uvicorn

        if app is None:
            from edith.interfaces.server import create_app

            app = create_app(cfg)
        config = uvicorn.Config(
            app, host=cfg.server.host, port=cfg.server.port, log_level="warning", lifespan="on"
        )
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self.server.run, name="edith-server", daemon=True)

    def start(self, timeout: float = START_TIMEOUT_S) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self._thread.is_alive():
                raise DesktopError("The EDITH server stopped while starting. See the log.")
            if time.monotonic() > deadline:
                raise DesktopError("The EDITH server did not start in time.")
            time.sleep(0.05)

    def stop(self, timeout: float = 10.0) -> None:
        self.server.should_exit = True  # lifespan shutdown closes the orchestrator
        self._thread.join(timeout)


def make_icon(size: int = 64) -> Image.Image:
    """The tray and shortcut icon, drawn in code so the repo carries no binary assets."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(1, size // 16)
    d.ellipse((pad, pad, size - pad, size - pad), fill=(17, 94, 89, 255))
    ring = max(1, size // 10)
    d.ellipse(
        (pad + ring, pad + ring, size - pad - ring, size - pad - ring),
        outline=(94, 234, 212, 255),
        width=max(1, size // 24),
    )
    # A plain "E" from rectangles: crisp at any size, no font files needed.
    w, h = size * 0.30, size * 0.40
    x0, y0 = (size - w) / 2, (size - h) / 2
    t = h / 5
    white = (240, 253, 250, 255)
    d.rectangle((x0, y0, x0 + t, y0 + h), fill=white)
    for y in (y0, y0 + 2 * t, y0 + h - t):
        d.rectangle((x0, y, x0 + w, y + t), fill=white)
    return img


def write_icon(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    make_icon(256).save(path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    return path


def launcher() -> tuple[Path, list[str]]:
    """How the shortcut starts the app: pythonw (no console window) running this module."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    target = windowless if windowless.exists() else exe
    return target, ["-m", "edith.interfaces.desktop"]


class Window(Protocol):
    def show(self) -> None: ...
    def hide(self) -> None: ...
    def restore(self) -> None: ...
    def destroy(self) -> None: ...


@dataclass
class Controller:
    """Window and tray behaviour, free of GUI imports so it can be unit-tested."""

    notify: Callable[[str], None] = lambda _msg: None
    window: Window | None = None
    quitting: bool = False
    _hinted: bool = field(default=False, repr=False)

    def on_closing(self) -> bool:
        """Window close button: hide to the tray. Returns False to cancel the close."""
        if self.quitting or self.window is None:
            return True
        self.window.hide()
        if not self._hinted:
            self._hinted = True
            self.notify("EDITH is still running. Use the tray icon to open it or quit.")
        return False

    def show(self) -> None:
        if self.window is not None and not self.quitting:
            self.window.show()
            self.window.restore()

    def quit(self) -> None:
        self.quitting = True
        if self.window is not None:
            self.window.destroy()


ERROR_PAGE = """<!doctype html><meta charset="utf-8"><title>EDITH</title>
<body style="font:15px system-ui;background:#0f172a;color:#e2e8f0;padding:24px">
<h2 style="color:#5eead4">EDITH could not start</h2><p>{message}</p>
<p>Run <code>uv run edith status</code> in a terminal for details.</p></body>"""


def run(cfg: EdithConfig) -> None:
    """Open the window; block until the user quits."""
    if sys.platform != "win32":
        raise DesktopError("The desktop app is Windows-only for now. Use `edith serve`.")
    import html

    import pystray
    import webview

    url = base_url(cfg)
    server: ServerThread | None = None
    error: str | None = None
    if probe(url) is not None:
        log.info("desktop_attached", url=url)  # `edith serve` (or another window) owns it
    elif port_in_use(cfg.server.host, cfg.server.port):
        error = f"Port {cfg.server.port} is taken by another program. Change server.port."
    else:
        try:
            server = ServerThread(cfg)
            server.start()
        except Exception as e:
            log.exception("desktop_server_failed")
            error = str(e) or type(e).__name__
            server = None

    ctl = Controller()
    if error is None:
        window = webview.create_window(
            APP_NAME, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=MIN_SIZE
        )
    else:
        window = webview.create_window(
            APP_NAME, html=ERROR_PAGE.format(message=html.escape(error)), width=480, height=320
        )
    if window is None:
        raise DesktopError("Could not create the window.")
    ctl.window = window
    window.events.closing += ctl.on_closing

    tray = pystray.Icon(
        "edith",
        make_icon(64),
        f"{APP_NAME} {__version__}",
        menu=pystray.Menu(
            pystray.MenuItem("Open EDITH", lambda: ctl.show(), default=True),
            pystray.MenuItem("Quit", lambda: ctl.quit()),
        ),
    )
    ctl.notify = lambda msg: tray.notify(msg, APP_NAME)
    tray.run_detached()
    log.info("desktop_started", url=url, owns_server=server is not None)
    try:
        # private_mode: no cookies or web storage kept on disk between runs.
        webview.start(gui="edgechromium", private_mode=True)
    finally:
        tray.stop()
        if server is not None:
            server.stop()
        log.info("desktop_stopped")


def main() -> None:
    """Entry point for the Start-menu shortcut (pythonw: no console to print to)."""
    from edith.config import ConfigError, load_config
    from edith.observability.logging import configure_logging

    try:
        cfg = load_config()
    except ConfigError:
        sys.exit(2)
    configure_logging(cfg.log_dir)
    try:
        run(cfg)
    except Exception:
        log.exception("desktop_crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()
