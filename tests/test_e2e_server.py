"""The server over a real socket.

Starlette's TestClient talks to the ASGI app directly, which hides everything the
transport does: it reported a close code that no browser ever received, because a close
sent before the handshake is answered as an HTTP 403. These tests run uvicorn on a free
port and connect with an independent WebSocket client, so what they observe is what a
phone observes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed

from livesubs.config import Settings
from livesubs.events import SegmentState, SubtitleSegment, Timing
from livesubs.hub import Hub
from livesubs.server import create_app


class OneLinePublisher:
    """Publishes one German line with its English source, then waits."""

    async def run(self, hub: Hub, *, session_id: str, stop: asyncio.Event) -> None:
        hub.publish_segment(
            SubtitleSegment(
                seq=0,
                rev=0,
                state=SegmentState.FINAL,
                de="Hallo zusammen.",
                src="Hello everyone.",
                timing=Timing(audio_ms=1000, emit_ms=1400),
            )
        )
        await stop.wait()


@asynccontextmanager
async def serving(publisher) -> AsyncIterator[str]:
    app = create_app(Settings(_env_file=None), publisher)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def close_code_of(url: str) -> int:
    """Connect, and return the close code the client is given."""

    async with websockets.connect(url, open_timeout=5) as ws:
        with pytest.raises(ConnectionClosed):
            while True:
                await asyncio.wait_for(ws.recv(), timeout=5)
        assert ws.close_code is not None
        return ws.close_code


async def messages_of(url: str, count: int) -> list[dict]:
    async with websockets.connect(url, open_timeout=5) as ws:
        return [json.loads(await asyncio.wait_for(ws.recv(), timeout=5)) for _ in range(count)]


async def test_an_unknown_session_is_refused_with_a_code_the_client_receives() -> None:
    async with serving(OneLinePublisher()) as base:
        assert await close_code_of(f"{base}/ws/nope") == 4404


async def test_an_unsupported_language_is_refused_with_a_code_the_client_receives() -> None:
    """The viewer stops retrying only if this code reaches it."""

    async with serving(OneLinePublisher()) as base:
        assert await close_code_of(f"{base}/ws/demo?role=viewer_ar") == 4403


async def test_a_refused_socket_creates_no_demand() -> None:
    async with serving(OneLinePublisher()) as base:
        await close_code_of(f"{base}/ws/demo?role=viewer_ar")
        await asyncio.sleep(0.05)
        # Asked over the same server, with an async client: a blocking call would stall
        # the loop that is also running the server.
        async with httpx.AsyncClient(base_url=base.replace("ws://", "http://")) as client:
            payload = (await client.get("/healthz", timeout=5)).json()
        assert payload["viewers"] == 0
        assert payload["connections"] == 0


async def test_a_viewer_gets_the_line_without_the_source() -> None:
    async with serving(OneLinePublisher()) as base:
        messages = await messages_of(f"{base}/ws/demo", 3)

    assert [m["type"] for m in messages] == ["hello", "status", "segment"]
    assert messages[2]["data"]["de"] == "Hallo zusammen."
    assert messages[2]["data"]["src"] is None


async def test_a_heartbeat_reaches_an_idle_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    from livesubs.server import ws as websocket_routes

    monkeypatch.setattr(websocket_routes, "HEARTBEAT_INTERVAL_S", 0.05)

    class Idle:
        async def run(self, hub, *, session_id, stop):
            await stop.wait()

    async with (
        serving(Idle()) as base,
        websockets.connect(f"{base}/ws/demo", open_timeout=5) as ws,
    ):
        kinds = []
        for _ in range(4):
            kinds.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=5))["type"])

    assert kinds[:2] == ["hello", "status"]
    assert "ping" in kinds[2:]
