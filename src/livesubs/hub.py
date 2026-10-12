"""Fan-out from the pipeline to viewers, with latest-wins mailboxes.

One rule shapes this module: **``Hub.publish`` must never block.** It is called
from the pipeline's hot path, so a viewer on bad hall WiFi must not be able to
stall transcription for everyone else. That single constraint rules out the
obvious design (a bounded queue per client) because a queue either blocks, drops
arbitrarily, or grows without limit.

Instead each client gets a **latest-wins mailbox** holding at most one message
per channel. This is not a compromise; it is the correct semantics for
subtitles:

* a newer revision of the same segment is a *complete replacement* for the older
  one, so superseding loses nothing;
* a stale subtitle is worth less than a current one, so collapsing is desirable;
* memory per client is constant, whatever the client does.

Ordering is still guaranteed, because the mailbox refuses anything at or below
the highest ``(seq, rev)`` already delivered. A client can never see ``final``
and then ``draft``.

Serialisation is shared: encoded payloads are cached by ``(seq, rev)``, so a
hundred viewers cost one JSON encoding per revision rather than a hundred. See
``docs/07-delivery-qr.md`` for why the fan-out cost is per-message encoding
rather than bandwidth.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from livesubs.events import (
    ErrorMessage,
    ErrorPayload,
    HelloMessage,
    HelloPayload,
    PipelineStatus,
    SegmentMessage,
    Stage,
    StatusMessage,
    StatusPayload,
    SubtitleSegment,
    WireMessage,
    dumps,
)

__all__ = [
    "ClientMailbox",
    "ClientRole",
    "Hub",
    "SegmentPublisher",
    "Subscriber",
]

#: How many encoded segment payloads to cache. A 90-minute lecture produces a few
#: thousand revisions, and only recent ones can still be pending delivery, so a
#: few hundred entries covers the live window.
_ENCODE_CACHE_SIZE = 512


class ClientRole(str, Enum):
    """What a connected client is, which decides how much it may see."""

    VIEWER = "viewer"
    """Attendee's phone or laptop. German only; never receives ``src``."""

    ADMIN = "admin"
    """Operator console. Also receives the English source and errors."""


@dataclass(slots=True)
class Subscriber:
    """A registered client connection."""

    id: str
    role: ClientRole
    mailbox: ClientMailbox


class ClientMailbox:
    """Holds at most one pending message per channel for one client.

    Written to only by :meth:`Hub.publish` (synchronous), read from only by the
    client's writer task. Because the drain in :meth:`receive` contains no
    ``await``, it is atomic with respect to the event loop, so a message offered
    mid-drain cannot be lost.
    """

    #: Errors are kept in a small bounded deque so a burst cannot grow memory,
    #: and only admins see them at all.
    MAX_ERRORS = 4

    def __init__(self, role: ClientRole) -> None:
        self.role = role
        self._hello: HelloPayload | None = None
        self._status: StatusPayload | None = None
        self._errors: deque[ErrorPayload] = deque(maxlen=self.MAX_ERRORS)
        self._segment: SubtitleSegment | None = None
        self._highest: tuple[int, int] | None = None
        self._event = asyncio.Event()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def has_pending(self) -> bool:
        return any(
            (
                self._hello is not None,
                self._status is not None,
                bool(self._errors),
                self._segment is not None,
            )
        )

    def offer(self, message: WireMessage) -> None:
        """Non-blocking, never raises. Called from the pipeline's hot path."""

        if self._closed:
            return

        match message:
            case HelloMessage(data=payload):
                self._hello = payload

            case StatusMessage(data=payload):
                self._status = payload

            case ErrorMessage(data=payload):
                if self.role is not ClientRole.ADMIN:
                    return
                self._errors.append(payload)

            case SegmentMessage(data=segment):
                # Ordering guard: refuse anything at or below what we already
                # hold, so a client can never see `final` and then `draft`.
                if self._highest is not None and (segment.seq, segment.rev) <= self._highest:
                    return
                self._highest = (segment.seq, segment.rev)

                # The German-only requirement is enforced HERE, on the server,
                # rather than trusted to the viewer page. A viewer's device never
                # receives the English source at all.
                self._segment = (
                    segment
                    if self.role is ClientRole.ADMIN
                    else segment.model_copy(update={"src": None})
                )

            case _:  # pragma: no cover - discriminated union makes this unreachable
                return

        self._event.set()

    async def receive(self) -> list[WireMessage]:
        """Wait for something to send, then return everything pending.

        Returns an empty list once the mailbox is closed, which is the caller's
        signal to stop.
        """

        await self._event.wait()
        self._event.clear()

        if self._closed:
            return []

        out: list[WireMessage] = []

        # Drain order deliberately gives a late joiner a sensible snapshot:
        # identify the session, then report health, then show the current line.
        if self._hello is not None:
            out.append(HelloMessage(data=self._hello))
            self._hello = None

        while self._errors:
            out.append(ErrorMessage(data=self._errors.popleft()))

        if self._status is not None:
            out.append(StatusMessage(data=self._status))
            self._status = None

        if self._segment is not None:
            out.append(SegmentMessage(data=self._segment))
            self._segment = None

        return out

    def close(self) -> None:
        self._closed = True
        self._event.set()


class Hub:
    """Fan-out to every client connected to one session.

    ``publish`` is synchronous and never blocks. See the module docstring for why
    that constraint drives the whole design.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, Subscriber] = {}
        self._ids = itertools.count(1)
        self._encode_cache: OrderedDict[tuple[int, int, bool], str] = OrderedDict()
        # Snapshot state, so a late joiner is fully oriented on connect: who the
        # session is, how healthy it is, and what is currently on screen.
        self._latest_hello: HelloPayload | None = None
        self._latest_segment: SubtitleSegment | None = None
        self._latest_status: StatusPayload | None = None

        #: Whether a pipeline is driving paid work from this hub's audience. Off by
        #: default so a non-gated replay keeps its cached line across a reconnect.
        self._audience_gating = False
        #: Presence, not count: the generation advances only when the audience crosses
        #: between none and some, so a second viewer is not mistaken for a fresh one.
        self._demand_present = False
        self._demand_generation = 0
        self._demand_event = asyncio.Event()

    def enable_audience_gating(self) -> None:
        """Mark this hub as driving paid work.

        With gating on, the cached line is dropped the instant the last viewer leaves,
        so a fast reconnect cannot be seeded with a stale or unfinished line. A
        non-gated replay leaves it off and keeps its snapshot behaviour.
        """

        self._audience_gating = True

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def viewer_count(self) -> int:
        """Connected *viewing* devices, which is what creates paid provider demand.

        Admins are deliberately excluded. An operator console is not an audience, and
        an admin connection must never by itself make the pipeline open or keep open a
        billed ASR stream or warm a billed translator on an empty room.
        """

        return sum(
            1 for subscriber in self._subscribers.values()
            if subscriber.role is ClientRole.VIEWER
        )

    @property
    def has_viewers(self) -> bool:
        return self.viewer_count > 0

    @property
    def demand_generation(self) -> int:
        """A revision that advances only when the audience crosses zero.

        A consumer records the value when it starts paid work and can tell, with no
        dependence on audio frames, whether the audience it opened for is still the
        current one. Going to zero and back produces a new value, so a fast reconnect
        invalidates any attempt that belonged to the previous occupancy.
        """

        return self._demand_generation

    async def wait_for_demand_change(
        self, generation: int, timeout: float | None = None
    ) -> int:
        """Wait until the audience generation differs from ``generation``.

        Wakes on the actual connect/disconnect, not on the next captured frame, which
        is what lets paid work be cancelled even when capture has stalled. Returns the
        current generation, or the same one after ``timeout`` if it did not change.
        """

        while self._demand_generation == generation:
            event = self._demand_event
            if self._demand_generation != generation:
                break
            if timeout is None:
                await event.wait()
            else:
                try:
                    await asyncio.wait_for(event.wait(), timeout=timeout)
                except TimeoutError:
                    break
        return self._demand_generation

    def forget_segment_snapshot(self) -> None:
        """Drop the cached current line.

        Called when the audience leaves: the cached line belongs to a lecture that is
        now paused, and seeding the next viewer with it would replay stale or
        unfinished text that no longer describes what is on screen.
        """

        self._latest_segment = None

    def _note_demand(self) -> None:
        """Advance the generation and wake waiters when audience presence flips.

        Called synchronously from :meth:`subscribe` and :meth:`unsubscribe`, both of
        which run on the event loop. Only a presence change counts, so a second viewer
        joining does not look like a new occupancy to an in-flight open.
        """

        present = self.has_viewers
        if present == self._demand_present:
            return
        self._demand_present = present
        self._demand_generation += 1
        previous, self._demand_event = self._demand_event, asyncio.Event()
        previous.set()

    def subscribe(self, role: ClientRole) -> Subscriber:
        """Register a client and seed its snapshot immediately.

        Seeding matters: a late joiner must never be staring at a blank screen
        while everyone around them is already reading a line.
        """

        subscriber = Subscriber(
            id=f"c{next(self._ids)}",
            role=role,
            mailbox=ClientMailbox(role),
        )
        self._subscribers[subscriber.id] = subscriber

        # Seed the snapshot in the same order the mailbox drains, so a joiner's
        # first three messages are always identity, health, current line.
        if self._latest_hello is not None:
            subscriber.mailbox.offer(HelloMessage(data=self._latest_hello))
        if self._latest_status is not None:
            subscriber.mailbox.offer(StatusMessage(data=self._latest_status))
        if self._latest_segment is not None:
            subscriber.mailbox.offer(SegmentMessage(data=self._latest_segment))

        if role is ClientRole.VIEWER:
            self._note_demand()

        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.pop(subscriber.id, None)
        subscriber.mailbox.close()

        if subscriber.role is not ClientRole.VIEWER:
            return

        # Clear synchronously, before the event loop can run a reconnect handler:
        # otherwise a viewer that leaves and returns within one frame would be seeded
        # with the previous line the moment it subscribes.
        if self._audience_gating and not self.has_viewers:
            self._latest_segment = None

        self._note_demand()

    def publish(self, message: WireMessage) -> None:
        """Synchronous, non-blocking, safe to call from the pipeline's hot path."""

        if isinstance(message, SegmentMessage):
            # Never cache a line that arrived after the audience left: a late
            # translation must not seed the next viewer with text nobody watched.
            if not (self._audience_gating and not self.has_viewers):
                self._latest_segment = message.data
            self.encode(message)          # pay for one encoding, not one per viewer
        elif isinstance(message, StatusMessage):
            self._latest_status = message.data
        elif isinstance(message, HelloMessage):
            # Cached, not just broadcast: Session publishes hello once at startup,
            # so without this a viewer joining mid-lecture would never learn the
            # session identity or the protocol version.
            self._latest_hello = message.data

        for subscriber in self._subscribers.values():
            subscriber.mailbox.offer(message)

    def publish_segment(self, segment: SubtitleSegment) -> None:
        self.publish(SegmentMessage(data=segment))

    def publish_status(self, status: PipelineStatus, detail: str | None = None) -> None:
        self.publish(StatusMessage(data=StatusPayload(status=status, detail=detail)))

    def publish_error(self, message: str, stage: Stage) -> None:
        self.publish(ErrorMessage(data=ErrorPayload(message=message, stage=stage)))

    def encode(self, message: WireMessage) -> str:
        """Serialise, caching segment payloads so every viewer shares one encoding.

        The cache key includes whether ``src`` was present, because viewers get a
        stripped copy and admins do not. Without that flag the two collide and a
        viewer could be handed the English source.
        """

        if not isinstance(message, SegmentMessage):
            return dumps(message)

        key = (message.data.seq, message.data.rev, message.data.src is not None)
        cached = self._encode_cache.get(key)
        if cached is not None:
            self._encode_cache.move_to_end(key)
            return cached

        encoded = dumps(message)
        self._encode_cache[key] = encoded
        if len(self._encode_cache) > _ENCODE_CACHE_SIZE:
            self._encode_cache.popitem(last=False)
        return encoded


@runtime_checkable
class SegmentPublisher(Protocol):
    """Anything able to drive a session's subtitles.

    Implemented by the live pipeline and by the simulation replay. That shared
    interface is what lets the overlay be built and tuned with no credentials and
    no microphone, and means the overlay cannot behave differently between a
    demo and a real lecture.
    """

    async def run(self, hub: Hub, *, session_id: str, stop: asyncio.Event) -> None:
        """Publish until ``stop`` is set, then return."""
        ...
