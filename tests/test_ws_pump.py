"""The per-client writer must never lose a batch the mailbox has already emptied."""

from __future__ import annotations

import asyncio
import json

import anyio
import pytest

from livesubs.events import SegmentMessage, SegmentState, SubtitleSegment, Timing
from livesubs.hub import ClientRole, Hub
from livesubs.server import ws as websocket_routes


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def segment(seq: int) -> SegmentMessage:
    return SegmentMessage(
        data=SubtitleSegment(
            seq=seq,
            rev=0,
            state=SegmentState.FINAL,
            de=f"Zeile {seq}",
            timing=Timing(audio_ms=seq, emit_ms=seq),
        )
    )


async def test_a_message_landing_on_the_heartbeat_deadline_is_still_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the writer treated `deadline fired` as `nothing arrived`.

    A message offered in the same loop iteration as the heartbeat deadline made
    `receive()` return a real batch while the cancel scope still reported itself
    cancelled. The writer then sent a ping and carried on, discarding the batch the
    mailbox had just handed over.

    The window is about a millisecond wide, so rather than hope to land in it, each
    heartbeat scope the writer opens gets a message scheduled half a millisecond before
    its own deadline, which is the iteration that fires the deadline.
    """

    monkeypatch.setattr(websocket_routes, "HEARTBEAT_INTERVAL_S", 0.01)

    hub = Hub()
    subscriber = hub.subscribe(ClientRole.VIEWER)
    socket = RecordingSocket()
    offered: list[int] = []
    real_move_on_after = anyio.move_on_after

    def racing_move_on_after(delay):
        scope = real_move_on_after(delay)
        if len(offered) < 12:
            seq = len(offered)
            offered.append(seq)
            asyncio.get_running_loop().call_at(
                scope.deadline - 0.0005, hub.publish, segment(seq)
            )
        return scope

    monkeypatch.setattr(websocket_routes.anyio, "move_on_after", racing_move_on_after)

    writer = asyncio.create_task(websocket_routes._pump(socket, hub, subscriber))  # type: ignore[arg-type]
    try:
        while len(offered) < 12:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
    finally:
        subscriber.mailbox.close()
        await asyncio.wait_for(writer, timeout=2)

    delivered = {m["data"]["seq"] for m in socket.sent if m["type"] == "segment"}
    assert delivered == set(offered), f"lost: {sorted(set(offered) - delivered)}"
    assert any(m["type"] == "ping" for m in socket.sent), "the heartbeat itself must still run"
