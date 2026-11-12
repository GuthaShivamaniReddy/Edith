from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from edith.config import EdithConfig
from edith.core.orchestrator import Orchestrator
from edith.interfaces.server import create_app
from edith.safety.policy import Permissions
from edith.tools.system.fake import FakeAdapter
from tests.fakes import FakeFactory, ToolCall, fake_orchestrator, text_turn


def until(ws: Any, kind: str) -> list[dict[str, Any]]:
    """Read messages until one of `kind` arrives; return them all."""
    seen: list[dict[str, Any]] = []
    while True:
        msg = ws.receive_json()
        seen.append(msg)
        if msg["type"] == kind:
            return seen


@pytest.fixture
def setup(
    tool_cfg: EdithConfig, permissions: Permissions, sandbox: Path
) -> tuple[Orchestrator, FakeFactory]:
    todo = sandbox / "docs" / "todo.txt"
    return fake_orchestrator(
        tool_cfg,
        permissions,
        [ToolCall("mcp__files__read", {"path": str(todo)}), *text_turn("milk", 0.001)],
        adapter=FakeAdapter(),
    )


@pytest.fixture
def client(tool_cfg: EdithConfig, setup: tuple[Orchestrator, FakeFactory]) -> Iterator[TestClient]:
    app = create_app(tool_cfg, setup[0], extra_hosts=["testserver"])
    with TestClient(app) as c:
        yield c


def test_page_and_health(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "<title>EDITH</title>" in page.text
    assert "http" not in page.text.split("<script>")[1].replace("http://", "")  # no remote URLs
    assert client.get("/health").json()["status"] == "ok"


def test_request_with_confirmation(
    client: TestClient, setup: tuple[Orchestrator, FakeFactory]
) -> None:
    orch, _ = setup
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        ws.send_json({"type": "request", "text": "read my todo"})
        confirm = until(ws, "confirm_request")[-1]
        assert confirm["kind"] == "access" and "todo.txt" in confirm["action"]
        ws.send_json({"type": "confirm", "id": confirm["id"], "approved": True, "scope": "session"})
        msgs = until(ws, "final")
        assert any(m["type"] == "tool_end" and m["ok"] for m in msgs)
        assert msgs[-1]["text"] == "milk"
        ws.send_json({"type": "grants"})
        grants = until(ws, "grants")[-1]["grants"]
        assert len(grants) == 1 and grants[0].startswith("files: ")
    # grants end with the connection
    assert hello["session_id"] not in orch._sessions


def test_fast_path_and_unknown_message(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "request", "text": "mute"})
        assert until(ws, "final")[-1]["text"] == "Muted."
        ws.send_json({"type": "dance"})
        assert "Unknown message" in until(ws, "error")[-1]["message"]
        ws.send_json({"type": "stop"})  # nothing running: harmless
        ws.send_json({"type": "request", "text": "what time is it"})
        assert until(ws, "final")[-1]["model"] == "fast-path"


def test_foreign_origin_rejected(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws,
    ):
        ws.receive_json()


def test_same_origin_accepted(client: TestClient, tool_cfg: EdithConfig) -> None:
    origin = f"http://127.0.0.1:{tool_cfg.server.port}"
    with client.websocket_connect("/ws", headers={"origin": origin}) as ws:
        assert ws.receive_json()["type"] == "hello"


def test_foreign_host_rejected(
    tool_cfg: EdithConfig, setup: tuple[Orchestrator, FakeFactory]
) -> None:
    """DNS rebinding defence: only loopback Host headers are served."""
    with TestClient(create_app(tool_cfg, setup[0]), base_url="http://evil.example") as c:
        assert c.get("/").status_code == 400
