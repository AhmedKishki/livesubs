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
from collections.abc import Callable
from typing import Any

from livesubs.events import HelloMessage, HelloPayload, PipelineStatus
from livesubs.hub import Hub, SegmentPublisher
from livesubs.languages import Mode
from livesubs.metrics import LoopMonitor, process_stats

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
        mode: Mode,
        default_language: str = "en",
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.mode = mode
        #: Stable source capabilities, independent of recognition intent.
        self.source = "en+de"
        if default_language not in {"en", "de", "ar"}:
            raise ValueError("default_language must be en, de or ar")
        self.default_language = default_language
        self.revision = 0
        #: All three targets are required; fallback is not derived from their order.
        self.targets = _targets(mode, publisher)
        self.hub = Hub(self.targets)
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

    def snapshot(self, loop_monitor: LoopMonitor | None = None) -> dict[str, Any]:
        """Everything the operator console shows, as one JSON-ready dictionary.

        The audience comes from the hub, the signal, recogniser and translators from the
        publisher when it can describe itself, and the host from the loop monitor. A
        publisher that cannot describe itself simply contributes nothing.
        """

        describe = getattr(self.publisher, "snapshot", None)
        return {
            "session": {
                "id": self.id,
                "name": self.name,
                "source": self.source,
                "default": self.default_language,
                "languages": list(self.targets),
                "running": self.running,
                "failed": self.failed,
                "uptime_s": round(self.uptime_s, 1),
                "recording": bool(getattr(self.publisher, "records_audio", False)),
                "status": self.hub.current_status(),
                **self.controls(),
            },
            "audience": self.hub.metrics.snapshot(self.hub),
            "pipeline": describe() if callable(describe) else None,
            "server": loop_monitor.snapshot() if loop_monitor is not None else process_stats(),
        }

    def controls(self) -> dict[str, Any]:
        describe = getattr(self.publisher, "speaker_snapshot", None)
        recognition = (
            describe() if callable(describe) else {
                "requested": self.mode.source if self.mode.source in {"en", "de"} else "auto",
                "active": None,
                "state": "paused",
                "revision": 0,
                "error": None,
            }
        )
        return {
            "session_id": self.id,
            "revision": self.revision,
            "speaker": recognition["requested"],
            "default_language": self.default_language,
            "recognition": recognition,
        }

    def update_settings(self, values: Any) -> dict[str, Any]:
        """Validate the entire update before recording either choice, without yielding."""
        allowed = {"speaker": {"auto", "en", "de"}, "default_language": {"en", "de", "ar"}}
        if not isinstance(values, dict) or not values or values.keys() - allowed.keys():
            raise ValueError("expected speaker and/or default_language")
        if any(
            not isinstance(value, str) or value not in allowed[key]
            for key, value in values.items()
        ):
            raise ValueError("unsupported session setting")
        current = self.controls()
        changed = any(current[key] != value for key, value in values.items())
        if "speaker" in values and values["speaker"] != current["speaker"]:
            setter = getattr(self.publisher, "set_speaker", None)
            if not callable(setter):
                raise ValueError("publisher does not support speaker controls")
            setter(values["speaker"])
        if "default_language" in values:
            self.default_language = values["default_language"]
        if changed:
            self.revision += 1
        return self.controls()

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
                    source=self.source,
                    targets=list(self.targets),
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


def _targets(mode: Mode, publisher: SegmentPublisher) -> tuple[str, ...]:
    """Reject an incapable publisher rather than silently narrowing the fixed offer."""

    declared = getattr(publisher, "supported_languages", None) or ()
    offered = ("en", "de", "ar")
    if not set(offered).issubset(declared):
        raise ValueError("publisher must support all subtitle languages: en, de, ar")
    return offered


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
