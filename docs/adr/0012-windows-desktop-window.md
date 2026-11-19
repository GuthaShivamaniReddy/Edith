# 12. A light Windows window before the Phase 7 HUD

Status: accepted · 2026-11-19

## Context
The user asked for a Windows application during Phase 1. PLAN.md puts the desktop HUD
(Tauri + React) in Phase 7, and Tauri needs a Rust toolchain that this machine does not
have. The `edith serve` web page already covers chat, confirmations, questions and stop.

## Decisions
1. **pywebview (WebView2) around the existing page.** `edith app` starts the same FastAPI
   server on a background thread and opens a native window on `http://127.0.0.1:<port>`.
   No new client protocol, and the server's same-origin and loopback-host checks apply
   unchanged. `gui="edgechromium"` is forced so a missing WebView2 fails loudly instead of
   falling back to the old IE engine. `private_mode=True`: no web storage kept on disk.
2. **Tray icon (pystray).** Closing the window hides it to the tray; Quit in the tray menu
   stops the server (the lifespan shutdown closes the orchestrator). If `edith serve` is
   already running, the window attaches to it instead of starting a second server.
3. **Start-menu shortcut.** `edith app --shortcut` writes `EDITH.lnk` that runs
   `pythonw -m edith.interfaces.desktop` (no console window). The .lnk is written through
   `SystemAdapter.install_shortcut` (COM `WScript.Shell` in windows.py) per the OS-code rule;
   names are validated as plain file names. The icon is drawn in code with Pillow.
4. **Windows only.** pywebview and pystray are Windows-only dependencies so Linux CI needs
   no GTK; Pillow is installed everywhere. macOS and Linux keep `edith serve`.
5. **Not a model tool.** Nothing here is exposed to the model, so no permissions.yaml tier
   or eval scenarios; behaviour is covered by unit tests (Controller, ServerThread, CLI).

## Consequences
- Phase 7 replaces the window with the Tauri HUD; the server and protocol stay.
- Not packaged: the shortcut points into the repo's `.venv`. A standalone installer
  (PyInstaller or Tauri) is a later decision.
