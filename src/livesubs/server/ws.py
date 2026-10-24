"""WebSocket endpoint: one writer task per client, plus a reader for acks.

Concurrency shape: exactly one task writes to a socket, so sends can never
interleave. The reader exists for two reasons - it detects a disconnect promptly
(the writer would otherwise only find out on its next send), and it consumes the
render-timing echoes that become metrics input in P5. Draining also stops a
chatty client filling the receive buffer.
"""

from __future__ import annotations

import logging
import re
import time

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from livesubs.config import Settings
from livesubs.events import AckMessage, ClientMessage, WireMessage
from livesubs.hub import ClientRole, Hub, Subscriber
from livesubs.languages import LANGUAGES
from livesubs.server.sessions import Session

__all__ = ["UnsupportedLanguage", "build_router", "role_from"]

logger = logging.getLogger(__name__)

#: Application-defined close codes.
WS_UNKNOWN_SESSION = 4404
#: The client asked for a language this session cannot produce. Refused at the
#: socket, before any demand is recorded or a billed provider is touched.
WS_UNSUPPORTED_LANGUAGE = 4403
HEARTBEAT_INTERVAL_S = 10.0
CLIENT_IDLE_TIMEOUT_S = 30.0

#: Explicit language roles: ``viewer_de``, ``viewer_ar``. A plain ``viewer`` is the default.
_LANGUAGE_ROLE = re.compile(r"viewer_([a-z]{2,3})")
_CLIENT_MESSAGE = TypeAdapter(ClientMessage)


class UnsupportedLanguage(Exception):
    """A viewer asked for a language this session cannot produce."""

    def __init__(self, language: str) -> None:
        super().__init__(language)
        self.language = language


async def _refuse(websocket: WebSocket, code: int, reason: str) -> None:
    """Close a socket the server will never serve, in a way the browser can read.

    The handshake is accepted first, on purpose. A close sent before ``accept`` is
    answered as an HTTP 403 and the browser reports close code 1006 whatever code was
    given, so the viewer's "this refusal is permanent" handling never ran and it
    retried for ever. Nothing is subscribed here, so a refused socket creates no demand
    for paid work.
    """

    await websocket.accept()
    await websocket.close(code=code, reason=reason)


def role_from(value: str | None, session: Session) -> ClientRole:
    """Map a ``role`` query value to a client role by exact match.

    ``admin`` is the operator console. ``viewer_<code>`` asks for one language, and is
    refused (:class:`UnsupportedLanguage`) unless this session produces it. Every other
    value, including a missing one and a typo, is an ordinary viewer of the session's
    default language, so a misspelling can never widen what a device receives or put it
    in a language nobody chose.

    The legacy default role resolves the fallback now. The resulting concrete role
    remains pinned even if the operator changes that fallback later.
    """

    if value == "admin":
        return ClientRole.admin()

    match = _LANGUAGE_ROLE.fullmatch(value or "")
    if match is not None:
        language = match.group(1)
        if language not in session.targets:
            raise UnsupportedLanguage(language)
        return ClientRole.viewer(language)

    return ClientRole.viewer(session.default_language)


async def _pump(websocket: WebSocket, hub: Hub, subscriber: Subscriber) -> None:
    """Send whatever the mailbox considers current, for as long as it is open."""

    while True:
        batch: list[WireMessage] | None = None
        with anyio.move_on_after(HEARTBEAT_INTERVAL_S):
            batch = await subscriber.mailbox.receive()
        # Decided by whether `receive` returned, not by whether the deadline fired. A
        # message offered in the same loop iteration as the deadline makes `receive`
        # return a real batch while the scope still reports itself cancelled, and
        # treating that as a timeout threw the batch away after the mailbox had
        # already emptied itself into it.
        if batch is None:
            await websocket.send_text(hub.ping())
            continue
        if not batch:
            return

        started = time.monotonic()
        size = 0
        for message in batch:
            text = hub.encode(message)
            await websocket.send_text(text)
            size += len(text.encode("utf-8"))
        hub.metrics.note_sent(len(batch), size, (time.monotonic() - started) * 1000.0)


async def _read_acks(
    websocket: WebSocket, session_id: str, hub: Hub, subscriber: Subscriber
) -> None:
    """Consume client messages until the socket closes, keeping what viewers report."""

    while True:
        try:
            with anyio.fail_after(CLIENT_IDLE_TIMEOUT_S):
                raw = await websocket.receive_text()
        except TimeoutError:
            await websocket.close(code=1001, reason="heartbeat timeout")
            return

        try:
            message = _CLIENT_MESSAGE.validate_json(raw)
        except ValidationError:
            continue

        if isinstance(message, AckMessage):
            hub.metrics.note_ack(
                subscriber.id, subscriber.role.language, message.rendered_in_ms, message.dropped
            )
            logger.debug(
                "render ack session=%s seq=%s rev=%s rendered_in_ms=%s dropped=%s",
                session_id,
                message.seq,
                message.rev,
                message.rendered_in_ms,
                message.dropped,
            )


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/{session_id}")
    async def subtitle_stream(websocket: WebSocket, session_id: str) -> None:
        registry = websocket.app.state.sessions
        session = registry.get(session_id)

        if session is None:
            await _refuse(websocket, WS_UNKNOWN_SESSION, f"unknown session: {session_id}")
            return

        # Refuse a language the publisher cannot serve *before* subscribing, so no demand is
        # recorded and no billed provider is opened for a language that would never
        # produce a line.
        try:
            role = role_from(websocket.query_params.get("role"), session)
        except UnsupportedLanguage as unsupported:
            info = LANGUAGES.get(unsupported.language)
            name = info.name if info is not None else unsupported.language
            await _refuse(
                websocket,
                WS_UNSUPPORTED_LANGUAGE,
                f"{name} subtitles are not available for this session",
            )
            return

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
                        await _read_acks(websocket, session_id, session.hub, subscriber)
                    finally:
                        group.cancel_scope.cancel()

                group.start_soon(send)
                group.start_soon(receive)
        except* (WebSocketDisconnect, ConnectionError):
            pass
        except* RuntimeError as errors:
            # Starlette raises this when the socket is already closing. It is a broad
            # type, so what was swallowed is recorded rather than lost.
            logger.debug("socket closed under a send: %s", errors.exceptions)
        finally:
            session.hub.unsubscribe(subscriber)
            logger.info(
                "client disconnected session=%s role=%s remaining=%d",
                session_id,
                role.value,
                session.hub.subscriber_count,
            )

    return router
