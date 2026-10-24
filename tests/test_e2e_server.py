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

LINES = {
    "de": "Hallo zusammen.",
    "en": "Hello everyone.",
    "ar": "مرحبا بالجميع.",
}


class OneLinePublisher:
    """Publishes one line in each language it declares, with the speaker's words, then waits."""

    def __init__(self, *languages: str) -> None:
        self.supported_languages = ("en", "de", "ar")

    async def run(self, hub: Hub, *, session_id: str, stop: asyncio.Event) -> None:
        for language in self.supported_languages:
            hub.publish_segment(
                SubtitleSegment(
                    seq=0,
                    rev=0,
                    state=SegmentState.FINAL,
                    language=language,
                    text=LINES[language],
                    src="the speaker's own words",
                    timing=Timing(audio_ms=1000, emit_ms=1400),
                )
            )
        await stop.wait()


@asynccontextmanager
async def serving(publisher, speaker: str = "en") -> AsyncIterator[str]:
    app = create_app(Settings(_env_file=None, session={"speaker": speaker}), publisher)
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


#: ``(speaker, default language, the optional second language)``.
MODES = [("en", "en", "ar"), ("de", "en", "ar"), ("auto", "en", "de")]
MODE_IDS = ["english-speaker", "german-speaker", "conversation"]


async def test_an_unknown_session_is_refused_with_a_code_the_client_receives() -> None:
    async with serving(OneLinePublisher()) as base:
        assert await close_code_of(f"{base}/ws/nope") == 4404


@pytest.mark.parametrize(("speaker", "default", "optional"), MODES, ids=MODE_IDS)
async def test_a_language_the_publisher_cannot_produce_is_refused_with_a_code_the_client_receives(
    speaker: str, default: str, optional: str
) -> None:
    """The viewer stops retrying only if this code reaches it."""

    async with serving(OneLinePublisher(default), speaker) as base:
        assert await close_code_of(f"{base}/ws/demo?role=viewer_fr") == 4403


async def test_the_speakers_own_language_can_supply_filtered_subtitles() -> None:
    async with serving(OneLinePublisher("de", "ar"), "en") as base:
        messages = await messages_of(f"{base}/ws/demo?role=viewer_en", 3)
        assert messages[2]["data"]["text"] == LINES["en"]
        assert messages[2]["data"]["src"] is None


@pytest.mark.parametrize(("speaker", "default", "optional"), MODES, ids=MODE_IDS)
async def test_a_refused_socket_creates_no_demand(
    speaker: str, default: str, optional: str
) -> None:
    async with serving(OneLinePublisher(default), speaker) as base:
        await close_code_of(f"{base}/ws/demo?role=viewer_fr")
        await asyncio.sleep(0.05)
        # Asked over the same server, with an async client: a blocking call would stall
        # the loop that is also running the server.
        async with httpx.AsyncClient(base_url=base.replace("ws://", "http://")) as client:
            payload = (await client.get("/healthz", timeout=5)).json()
        assert payload["viewers"] == 0
        assert payload["connections"] == 0


@pytest.mark.parametrize(("speaker", "default", "optional"), MODES, ids=MODE_IDS)
async def test_a_plain_viewer_gets_the_default_language_without_the_source(
    speaker: str, default: str, optional: str
) -> None:
    async with serving(OneLinePublisher(default, optional), speaker) as base:
        messages = await messages_of(f"{base}/ws/demo", 3)

    assert [m["type"] for m in messages] == ["hello", "status", "segment"]
    assert messages[0]["data"]["source"] == "multilingual"
    assert messages[0]["data"]["targets"] == ["en", "de", "ar"]
    assert messages[0]["data"]["language"] == default
    assert messages[2]["data"]["language"] == default
    assert messages[2]["data"]["text"] == LINES[default]
    assert messages[2]["data"]["src"] is None


@pytest.mark.parametrize(("speaker", "default", "optional"), MODES, ids=MODE_IDS)
async def test_the_optional_language_reaches_only_a_viewer_who_asked_for_it(
    speaker: str, default: str, optional: str
) -> None:
    async with serving(OneLinePublisher(default, optional), speaker) as base:
        asked = await messages_of(f"{base}/ws/demo?role=viewer_{optional}", 3)
        async with websockets.connect(f"{base}/ws/demo", open_timeout=5) as plain:
            # A plain viewer sees its own language and nothing of the other one, however
            # long it listens.
            seen = [json.loads(await asyncio.wait_for(plain.recv(), timeout=5)) for _ in range(3)]
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(plain.recv(), timeout=0.3)

    assert asked[0]["data"]["language"] == optional
    assert asked[2]["data"]["language"] == optional
    assert asked[2]["data"]["text"] == LINES[optional]
    assert asked[2]["data"]["src"] is None
    assert {m["data"]["language"] for m in seen if m["type"] == "segment"} == {default}


async def test_a_mistyped_role_is_a_plain_viewer_and_never_widens_what_is_received() -> None:
    async with serving(OneLinePublisher("de", "ar")) as base:
        messages = await messages_of(f"{base}/ws/demo?role=viewer_AR", 3)

    assert messages[2]["data"]["language"] == "en"


async def test_a_heartbeat_reaches_an_idle_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    from livesubs.server import ws as websocket_routes

    monkeypatch.setattr(websocket_routes, "HEARTBEAT_INTERVAL_S", 0.05)

    class Idle:
        supported_languages = ("en", "de", "ar")

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


async def test_what_a_viewer_reports_reaches_the_operators_figures() -> None:
    """The acknowledgements the overlay sends are aggregated, not just drained."""

    async with serving(OneLinePublisher("de", "ar")) as base:
        async with websockets.connect(f"{base}/ws/demo?role=viewer_de", open_timeout=5) as ws:
            for _ in range(3):
                await asyncio.wait_for(ws.recv(), timeout=5)
            await ws.send(
                json.dumps({"type": "ack", "seq": 0, "rev": 0, "rendered_in_ms": 37, "dropped": 2})
            )
            async with httpx.AsyncClient(base_url=base.replace("ws://", "http://")) as client:
                for _ in range(100):
                    audience = (await client.get("/api/stats", timeout=5)).json()["audience"]
                    if audience["render_ms"]["de"]["count"]:
                        break
                    await asyncio.sleep(0.02)

        assert audience["render_ms"]["de"]["last"] == 37
        assert audience["render_ms"]["ar"]["count"] == 0, "a German viewer's time is German's"
        assert audience["dropped_lines"] == 2
        assert audience["messages_sent"] >= 3 and audience["peak_viewers"] == 1
