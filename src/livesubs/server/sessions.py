"""Session registry.

One session is served today, but everything is keyed by session id from the
start, so multi-session is a change to this file rather than a refactor of the
APIs, the WebSocket path or the QR URLs.

A session owns a :class:`~livesubs.hub.Hub` and exactly one
:class:`~livesubs.hub.SegmentPublisher`. The publisher is either the live
pipeline or the simulation replay, and the session does not care which: that
indirection is what makes the overlay identical in a demo and in a lecture.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from livesubs.events import HelloMessage, HelloPayload, PipelineStatus
from livesubs.hub import Hub, SegmentPublisher

__all__ = ["Session", "SessionRegistry"]

logger = logging.getLogger(__name__)


class Session:
    """A running subtitle session: one hub, one publisher, one task."""

    def __init__(
        self,
        *,
        id: str,
        name: str,
        publisher: SegmentPublisher,
        language: str = "de",
        languages: Sequence[str] | None = None,
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.language = language
        #: The target languages this session can actually produce. A viewer that asks
        #: for a language not in this list is refused at the socket rather than being
        #: allowed to create demand for text that will never arrive.
        self.languages: tuple[str, ...] = (
            tuple(languages) if languages is not None else (language,)
        )
        self.hub = Hub()
        self.publisher = publisher
        #: Called when the publisher dies with an exception. A service uses it to exit, so
        #: its supervisor restarts the process: a server that is up but whose pipeline is
        #: dead looks healthy to everything except the people reading the subtitles.
        self.on_failure = on_failure
        #: Set when the publisher died unexpectedly rather than being asked to stop.
        self.failed = False

        self.stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started_at = time.monotonic()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def uptime_s(self) -> float:
        return time.monotonic() - self._started_at

    async def start(self) -> None:
        """Announce the session and start publishing.

        The hello and an initial status are published *before* the publisher runs,
        so a viewer connecting in the first milliseconds already has a session
        identity and a health value rather than an empty shell.
        """

        if self._task is not None:
            return

        self.hub.publish(
            HelloMessage(
                data=HelloPayload(
                    session_id=self.id,
                    session_name=self.name,
                    language=self.language,
                )
            )
        )
        self.hub.publish_status(PipelineStatus.LIVE)

        self._task = asyncio.create_task(self._run(), name=f"livesubs:{self.id}")

    async def _run(self) -> None:
        try:
            await self.publisher.run(self.hub, session_id=self.id, stop=self.stop)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("publisher for session %s failed", self.id)
            self.failed = True
            self.hub.publish_status(PipelineStatus.STOPPED, detail="publisher failed")
            if self.on_failure is not None:
                self.on_failure()

    async def stop_publishing(self) -> None:
        """Ask the publisher to finish, then wait for it, then cancel if needed."""

        self.stop.set()
        if self._task is None:
            return

        try:
            await asyncio.wait_for(self._task, timeout=3.0)
        except TimeoutError:
            logger.warning("publisher for session %s did not stop; cancelling", self.id)
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("publisher for session %s errored while cancelling", self.id)
        except Exception:
            logger.exception("publisher for session %s errored during shutdown", self.id)

        self._task = None


class SessionRegistry:
    """Lookup by session id. Unknown ids must not be silently served."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def add(self, session: Session) -> None:
        self._sessions[session.id] = session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def require(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def __len__(self) -> int:
        return len(self._sessions)

    def __iter__(self):
        return iter(self._sessions.values())

    async def shutdown(self) -> None:
        for session in self._sessions.values():
            await session.stop_publishing()
