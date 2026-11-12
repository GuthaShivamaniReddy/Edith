"""The local API: one WebSocket that every face talks to, plus the minimal web page.

Loopback only (config enforces it). Browsers are held to same-origin and the Host
header must be a loopback name, so other web sites (and DNS rebinding) cannot drive
EDITH through your browser.

Client -> server messages (JSON):
  {"type": "request", "text": "..."}
  {"type": "confirm", "id": "...", "approved": true, "scope": "once" | "session"}
  {"type": "answer",  "id": "...", "answers": {"question": "label"} | null}
  {"type": "stop"}
  {"type": "grants"}  /  {"type": "revoke"}
Server -> client: every Event as a dict (see edith/core/events.py), plus
  {"type": "hello", ...}, {"type": "grants", "grants": [...]}.

Each connection is its own session: its access grants end when it disconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from edith import __version__
from edith.config import EdithConfig
from edith.core import fastpath
from edith.core.events import ErrorEvent, Event, Request, event_to_dict
from edith.core.orchestrator import Orchestrator
from edith.observability.logging import get_logger
from edith.safety.confirm import GrantScope

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent / "web"
LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "::1", "[::1]"]
MAX_TEXT = 20_000


def allowed_origins(cfg: EdithConfig) -> set[str]:
    port = cfg.server.port
    return {f"http://{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")}


def create_app(
    cfg: EdithConfig,
    orchestrator: Orchestrator | None = None,
    extra_hosts: list[str] | None = None,
) -> FastAPI:
    state: dict[str, Orchestrator] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        orch = orchestrator or Orchestrator(cfg)
        state["orch"] = orch
        log.info("server_started", host=cfg.server.host, port=cfg.server.port)
        try:
            yield
        finally:
            await orch.close()

    app = FastAPI(title="EDITH", version=__version__, lifespan=lifespan, docs_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*LOOPBACK_HOSTS, *(extra_hosts or [])])
    origins = allowed_origins(cfg)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if origin is not None and origin not in origins:
            log.warning("ws_origin_rejected", origin=origin)
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await Connection(state["orch"], websocket).serve()

    return app


class Connection:
    """One client: one session, at most one request in flight."""

    def __init__(self, orch: Orchestrator, websocket: WebSocket) -> None:
        self.orch = orch
        self.ws = websocket
        self.session_id = f"ws-{uuid.uuid4().hex[:8]}"
        self._send_lock = asyncio.Lock()
        self._request: asyncio.Task[None] | None = None

    async def send(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_json(payload)

    async def serve(self) -> None:
        notes = asyncio.create_task(self._forward_notifications())
        await self.send({"type": "hello", "version": __version__, "session_id": self.session_id})
        try:
            while True:
                msg = await self.ws.receive_json()
                if isinstance(msg, dict):
                    await self._dispatch(msg)
                else:
                    await self.send(event_to_dict(ErrorEvent("Messages must be JSON objects.")))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            notes.cancel()
            if self._busy:
                await self.orch.stop(self.session_id)
            if self._request is not None:
                self._request.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._request
            await self.orch.end_session(self.session_id)  # grants end with the connection

    @property
    def _busy(self) -> bool:
        return self._request is not None and not self._request.done()

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "request":
            text = str(msg.get("text") or "").strip()[:MAX_TEXT]
            if not text:
                return
            if self._busy:
                cmd = fastpath.match(text)
                if cmd is not None and cmd.kind == "stop":
                    await self.orch.stop(self.session_id)
                else:
                    await self.send(
                        event_to_dict(ErrorEvent("Still working on the last request. Say stop."))
                    )
                return
            req = Request(text=text, source="desktop", session_id=self.session_id)
            self._request = asyncio.create_task(self._run(req))
        elif kind == "confirm":
            scope: GrantScope = "session" if msg.get("scope") == "session" else "once"
            self.orch.confirm(str(msg.get("id")), bool(msg.get("approved")), scope)
        elif kind == "answer":
            answers = msg.get("answers")
            self.orch.answer(
                str(msg.get("id")),
                {str(k): str(v) for k, v in answers.items()} if isinstance(answers, dict) else None,
            )
        elif kind == "stop":
            await self.orch.stop(self.session_id)
        elif kind == "grants":
            await self.send({"type": "grants", "grants": self.orch.grants(self.session_id)})
        elif kind == "revoke":
            self.orch.revoke_grants(self.session_id)
            await self.send({"type": "grants", "grants": []})
        else:
            await self.send(event_to_dict(ErrorEvent(f"Unknown message type {kind!r}.")))

    async def _run(self, req: Request) -> None:
        try:
            async for ev in self.orch.handle(req):
                await self.send(event_to_dict(ev))
        except Exception as e:  # report, keep the connection alive
            log.exception("request_failed")
            with contextlib.suppress(Exception):
                await self.send(event_to_dict(ErrorEvent(f"Internal error: {type(e).__name__}")))

    async def _forward_notifications(self) -> None:
        q: asyncio.Queue[Event] = self.orch.subscribe()
        try:
            while True:
                ev = await q.get()
                await self.send(event_to_dict(ev))
        finally:
            self.orch.unsubscribe(q)
