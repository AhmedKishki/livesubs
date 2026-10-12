"""WebSocket endpoint: one writer task per client, plus a reader for acks.

Concurrency shape: exactly one task writes to a socket, so sends can never
interleave. The reader exists for two reasons - it detects a disconnect promptly
(the writer would otherwise only find out on its next send), and it consumes the
render-timing echoes that become metrics input in P5. Draining also stops a
chatty client filling the receive buffer.
"""

from __future__ import annotations

import json
import logging

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from livesubs.config import Settings
from livesubs.events import EVENT_VERSION
from livesubs.hub import ClientRole, Hub, Subscriber

__all__ = ["build_router"]

logger = logging.getLogger(__name__)

#: Application-defined close code for an unknown session.
WS_UNKNOWN_SESSION = 4404
HEARTBEAT_INTERVAL_S = 10.0
CLIENT_IDLE_TIMEOUT_S = 30.0


def _role_from(value: str | None) -> ClientRole:
    """Only ``admin`` gets the operator role; anything else is a viewer."""

    return ClientRole.ADMIN if value == "admin" else ClientRole.VIEWER


async def _pump(websocket: WebSocket, hub: Hub, subscriber: Subscriber) -> None:
    """Send whatever the mailbox considers current, for as long as it is open."""

    while True:
        with anyio.move_on_after(HEARTBEAT_INTERVAL_S) as heartbeat:
            batch = await subscriber.mailbox.receive()
        if heartbeat.cancel_called:
            await websocket.send_text(json.dumps({"v": EVENT_VERSION, "type": "ping"}))
            continue
        if not batch:
            return

        for message in batch:
            await websocket.send_text(hub.encode(message))


async def _read_acks(websocket: WebSocket, session_id: str) -> None:
    """Consume client messages until the socket closes."""

    while True:
        try:
            with anyio.fail_after(CLIENT_IDLE_TIMEOUT_S):
                raw = await websocket.receive_text()
        except TimeoutError:
            await websocket.close(code=1001, reason="heartbeat timeout")
            return

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        if isinstance(payload, dict) and payload.get("type") == "ack":
            logger.debug(
                "render ack session=%s seq=%s rev=%s render_ms=%s",
                session_id,
                payload.get("seq"),
                payload.get("rev"),
                payload.get("render_ms"),
            )


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/{session_id}")
    async def subtitle_stream(websocket: WebSocket, session_id: str) -> None:
        registry = websocket.app.state.sessions
        session = registry.get(session_id)

        if session is None:
            await websocket.close(
                code=WS_UNKNOWN_SESSION, reason=f"unknown session: {session_id}"
            )
            return

        role = _role_from(websocket.query_params.get("role"))
        await websocket.accept()

        # Subscribe *after* accept: the mailbox is seeded with a snapshot on
        # subscribe, and a late joiner must not see a blank screen.
        subscriber = session.hub.subscribe(role)

        try:
            async with anyio.create_task_group() as group:
                async def send() -> None:
                    try:
                        await _pump(websocket, session.hub, subscriber)
                    finally:
                        group.cancel_scope.cancel()

                async def receive() -> None:
                    try:
                        await _read_acks(websocket, session_id)
                    finally:
                        group.cancel_scope.cancel()

                group.start_soon(send)
                group.start_soon(receive)
        except* (WebSocketDisconnect, ConnectionError):
            pass
        except* RuntimeError:
            # Starlette raises this when the socket is already closing.
            pass
        finally:
            session.hub.unsubscribe(subscriber)
            logger.info(
                "client disconnected session=%s role=%s remaining=%d",
                session_id,
                role.value,
                session.hub.subscriber_count,
            )

    return router
