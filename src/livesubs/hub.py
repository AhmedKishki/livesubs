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
the highest ``(seq, rev)`` already delivered for that language. A client can never
see ``final`` and then ``draft``.

A channel is a target language. A viewer's role names the one language it may read and
the mailbox holds nothing else, so the filter is enforced on the server and not trusted to
the page. The operator console is the one role that reads every language.

Serialisation is shared: encoded payloads are cached by language and ``(seq, rev)``, so a
hundred viewers cost one JSON encoding per revision rather than a hundred. See
``docs/07-delivery-qr.md`` for why the fan-out cost is per-message encoding
rather than bandwidth.

The hub also counts who is watching, because that is what decides which paid work runs.
:class:`Presence` is that count with a generation number, and it is kept here rather than
in the pipeline because it is a fact about the connections, not about the pipeline.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import OrderedDict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from livesubs.events import (
    ErrorMessage,
    ErrorPayload,
    HelloMessage,
    HelloPayload,
    PingMessage,
    PipelineStatus,
    SegmentMessage,
    Stage,
    StatusMessage,
    StatusPayload,
    SubtitleSegment,
    WireMessage,
    dumps,
)
from livesubs.metrics import DeliveryMetrics

__all__ = [
    "ClientMailbox",
    "ClientRole",
    "Hub",
    "Presence",
    "SegmentPublisher",
    "Subscriber",
]

#: How many encoded segment payloads to cache. A 90-minute lecture produces a few
#: thousand revisions, and only recent ones can still be pending delivery, so a
#: few hundred entries covers the live window.
_ENCODE_CACHE_SIZE = 512

_SEVERITY = {PipelineStatus.LIVE: 0, PipelineStatus.DELAYED: 1, PipelineStatus.STOPPED: 2}


@dataclass(frozen=True, slots=True)
class ClientRole:
    """What a connected client is, which decides how much it may see.

    A viewer reads one target language and never receives the speaker's own words or any
    other language. The operator console receives everything.
    """

    kind: Literal["viewer", "admin"]
    language: str | None = None

    def __post_init__(self) -> None:
        if (self.kind == "viewer") != (self.language is not None):
            raise ValueError("a viewer reads one language, and the operator reads them all")

    @classmethod
    def viewer(cls, language: str) -> ClientRole:
        return cls("viewer", language)

    @classmethod
    def admin(cls) -> ClientRole:
        return cls("admin")

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"

    @property
    def is_viewer(self) -> bool:
        return self.kind == "viewer"

    @property
    def value(self) -> str:
        """A short name for logs: ``admin`` or ``viewer_<language>``."""

        return "admin" if self.is_admin else f"viewer_{self.language}"

    def reads(self, language: str) -> bool:
        """Whether this role may receive a line written in ``language``."""

        return self.is_admin or language == self.language


@dataclass(slots=True)
class Subscriber:
    """A registered client connection."""

    id: str
    role: ClientRole
    mailbox: ClientMailbox


class Presence:
    """Whether anyone is there, and a number that changes when that flips.

    The generation advances only when the audience crosses between none and some, so a
    second viewer is not mistaken for a fresh one. A consumer records the generation when
    it starts paid work and can tell, with no dependence on audio frames, whether the
    audience it started for is still the current one: going to zero and back produces a
    new value, so a fast reconnect invalidates anything that belonged to the old audience.
    """

    __slots__ = ("_event", "count", "generation")

    def __init__(self) -> None:
        self.count = 0
        self.generation = 0
        self._event = asyncio.Event()

    @property
    def present(self) -> bool:
        return self.count > 0

    def update(self, count: int) -> None:
        was_present = self.present
        self.count = count
        if self.present != was_present:
            self.generation += 1
            # Replaced rather than cleared, so a waiter that is about to look at the event
            # cannot miss the change by looking a moment too late.
            previous, self._event = self._event, asyncio.Event()
            previous.set()

    async def wait_change(self, generation: int, timeout: float | None = None) -> int:
        """Wait until the generation differs from ``generation``; return the current one.

        Wakes on the actual connect or disconnect, not on the next captured frame, which
        is what lets paid work be cancelled even when capture has stalled. After
        ``timeout`` it returns the same generation if nothing changed.
        """

        while self.generation == generation:
            event = self._event
            if timeout is None:
                await event.wait()
            else:
                try:
                    await asyncio.wait_for(event.wait(), timeout=timeout)
                except TimeoutError:
                    break
        return self.generation


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

    def __init__(self, role: ClientRole, order: Sequence[str] = ()) -> None:
        self.role = role
        #: The order languages are delivered in, for a role that reads several.
        self._order = tuple(order)
        self._hello: HelloPayload | None = None
        #: The latest status of each scope: ``None`` for everyone, or one language.
        self._status: dict[str | None, StatusPayload] = {}
        self._status_dirty = False
        self._errors: deque[ErrorPayload] = deque(maxlen=self.MAX_ERRORS)
        #: At most one pending segment per language, each with its own ordering guard, so
        #: one language's sequence cannot collide with another's.
        self._segments: dict[str, SubtitleSegment | None] = {}
        self._highest: dict[str, tuple[int, int] | None] = {}
        self._event = asyncio.Event()
        self._closed = False
        #: Lines this client was never sent because a newer line replaced them while it was
        #: still waiting to be written to, and revisions of one line that were coalesced.
        #: A skipped line is a client that cannot keep up; a skipped draft is ordinary.
        self.skipped_lines = 0
        self.skipped_drafts = 0

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def has_pending(self) -> bool:
        return any(
            (
                self._hello is not None,
                self._status_dirty,
                bool(self._errors),
                any(segment is not None for segment in self._segments.values()),
            )
        )

    def _for_role(self, segment: SubtitleSegment) -> SubtitleSegment:
        """The copy this role may hold, with everything it must not see removed."""

        if self.role.is_admin:
            return segment
        return segment.model_copy(update={"src": None, "words": None})

    def offer(self, message: WireMessage) -> None:
        """Non-blocking, never raises. Called from the pipeline's hot path."""

        if self._closed:
            return

        match message:
            case HelloMessage(data=payload):
                self._hello = payload

            case StatusMessage(data=payload):
                # A status about one language is for that language's readers alone.
                if payload.language is not None and not self.role.reads(payload.language):
                    return
                # Re-inserted, so the most recent status of equal severity comes last.
                self._status.pop(payload.language, None)
                self._status[payload.language] = payload
                self._status_dirty = True

            case ErrorMessage(data=payload):
                if not self.role.is_admin:
                    return
                self._errors.append(payload)

            case SegmentMessage(data=segment):
                language = segment.language
                # Server-side language filter: a role that may not read this stream
                # never receives it, rather than being trusted to ignore it.
                if not self.role.reads(language):
                    return

                # Ordering guard, per language: refuse anything at or below what we
                # already hold, so a client can never see `final` then `draft`.
                highest = self._highest.get(language)
                if highest is not None and (segment.seq, segment.rev) <= highest:
                    return
                self._highest[language] = (segment.seq, segment.rev)
                waiting = self._segments.get(language)
                if waiting is not None:
                    if waiting.seq == segment.seq:
                        self.skipped_drafts += 1
                    else:
                        self.skipped_lines += 1
                self._segments[language] = self._for_role(segment)

            case _:
                # A ping is written straight to the socket, never queued.
                return

        self._event.set()

    def _merged_status(self) -> StatusPayload | None:
        """One status for the wire: the worst of everything this client is told about."""

        if not self._status:
            return None
        # `max` keeps the first of equals, and the most recent should win a tie.
        newest_first = reversed(list(self._status.values()))
        return max(newest_first, key=lambda payload: _SEVERITY[payload.status])

    def _language_order(self) -> list[str]:
        known = [language for language in self._order if language in self._segments]
        known += sorted(language for language in self._segments if language not in known)
        return known

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
            out.append(
                HelloMessage(
                    data=self._hello.model_copy(update={"language": self.role.language})
                )
            )
            self._hello = None

        while self._errors:
            out.append(ErrorMessage(data=self._errors.popleft()))

        if self._status_dirty:
            merged = self._merged_status()
            if merged is not None:
                out.append(StatusMessage(data=merged))
            self._status_dirty = False

        for language in self._language_order():
            segment = self._segments.get(language)
            if segment is not None:
                out.append(SegmentMessage(data=segment))
                self._segments[language] = None

        return out

    def close(self) -> None:
        self._closed = True
        self._event.set()


class Hub:
    """Fan-out to every client connected to one session.

    ``publish`` is synchronous and never blocks. See the module docstring for why
    that constraint drives the whole design.
    """

    def __init__(self, targets: Sequence[str] = ()) -> None:
        #: The target languages in the order they are delivered to a role that reads several.
        self._targets = tuple(targets)
        self._subscribers: dict[str, Subscriber] = {}
        self._ids = itertools.count(1)
        self._encode_cache: OrderedDict[tuple[str, int, int, bool], str] = OrderedDict()
        # Snapshot state, so a late joiner is fully oriented on connect: who the
        # session is, how healthy it is, and what is currently on screen. One cached
        # line per target language, so a joiner is never shown another language's line.
        self._latest_hello: HelloPayload | None = None
        self._latest_segments: dict[str, SubtitleSegment] = {}
        #: The highest ``(seq, rev)`` cached per language, so a stale revision cannot
        #: overwrite the snapshot a late joiner is seeded with.
        self._latest_highest: dict[str, tuple[int, int] | None] = {}
        self._latest_status: dict[str | None, StatusPayload] = {}

        #: Whether a pipeline is driving paid work from this hub's audience. Off by
        #: default so a non-gated replay keeps its cached line across a reconnect.
        self._audience_gating = False
        #: Everyone watching in any language: what decides whether recognition runs.
        self._shared = Presence()
        #: Per language, so a translator runs only while someone reads its language.
        #: Adding a viewer of one language must not look like a new audience to the others.
        self._languages: dict[str, Presence] = {}
        #: Counters and windows for the operator console. Fed by the socket handlers.
        self.metrics = DeliveryMetrics(self._targets)

    def current_status(self) -> dict[str, dict[str, str | None]]:
        """The latest reported health of each scope, for the operator console.

        ``all`` is the scope that concerns everyone; any other key is a target language.
        """

        return {
            ("all" if scope is None else scope): {
                "status": payload.status.value,
                "detail": payload.detail,
            }
            for scope, payload in self._latest_status.items()
        }

    def mailboxes(self):
        """The mailbox of every connected client, for the console's totals."""

        return (subscriber.mailbox for subscriber in self._subscribers.values())

    def enable_audience_gating(self) -> None:
        """Mark this hub as driving paid work.

        With gating on, the cached line is dropped the instant the last viewer leaves,
        so a fast reconnect cannot be seeded with a stale or unfinished line. A
        non-gated replay leaves it off and keeps its snapshot behaviour.
        """

        self._audience_gating = True

    @property
    def targets(self) -> tuple[str, ...]:
        return self._targets

    @property
    def latest_hello(self) -> HelloPayload | None:
        """The session's announcement, once someone has made it."""

        return self._latest_hello

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def viewer_count(self) -> int:
        """Connected *viewing* devices, which is what creates paid provider demand.

        Admins are deliberately excluded. An operator console is not an audience, and
        an admin connection must never by itself make the pipeline open or keep open a
        billed ASR stream or warm a billed translator on an empty room. Viewers of every
        language count: any of them keeps recognition running.
        """

        return sum(1 for subscriber in self._subscribers.values() if subscriber.role.is_viewer)

    def viewer_count_for(self, language: str) -> int:
        """Connected viewers reading one target language."""

        return sum(
            1
            for subscriber in self._subscribers.values()
            if subscriber.role.is_viewer and subscriber.role.language == language
        )

    @property
    def viewers_by_language(self) -> dict[str, int]:
        languages = list(dict.fromkeys([*self._targets, *self._languages]))
        return {language: self.viewer_count_for(language) for language in languages}

    @property
    def has_viewers(self) -> bool:
        return self.viewer_count > 0

    def audience(self, language: str | None = None) -> Presence:
        """Who is watching: in one language, or, with ``None``, in any."""

        if language is None:
            return self._shared
        return self._languages.setdefault(language, Presence())

    def forget_segment_snapshot(self, language: str | None = None) -> None:
        """Drop the cached current line, for one language or for all.

        Called when an audience leaves: the cached line belongs to a lecture that is
        now paused, and seeding the next viewer with it would replay stale or
        unfinished text that no longer describes what is on screen.
        """

        if language is None:
            self._latest_segments.clear()
            self._latest_highest.clear()
        else:
            self._latest_segments.pop(language, None)
            self._latest_highest.pop(language, None)

    def _note_demand(self) -> None:
        """Update every audience count, advancing a generation where presence flipped.

        Called synchronously from :meth:`subscribe` and :meth:`unsubscribe`, both of
        which run on the event loop. Only a presence change advances a generation, so a
        second viewer joining does not look like a new occupancy to an in-flight open,
        and a viewer of one language joining a room that already has another does not
        look like a new shared occupancy either.
        """

        counts: dict[str, int] = {}
        for subscriber in self._subscribers.values():
            language = subscriber.role.language
            if language is not None:
                counts[language] = counts.get(language, 0) + 1

        self._shared.update(sum(counts.values()))
        for language in {*self._languages, *counts}:
            self.audience(language).update(counts.get(language, 0))

    def subscribe(self, role: ClientRole) -> Subscriber:
        """Register a client and seed its snapshot immediately.

        Seeding matters: a late joiner must never be staring at a blank screen
        while everyone around them is already reading a line.
        """

        subscriber = Subscriber(
            id=f"c{next(self._ids)}",
            role=role,
            mailbox=ClientMailbox(role, self._targets),
        )
        self._subscribers[subscriber.id] = subscriber

        # Seed the snapshot in the same order the mailbox drains, so a joiner's
        # first three messages are always identity, health, current line. Only the
        # languages the role may read are seeded.
        if self._latest_hello is not None:
            subscriber.mailbox.offer(HelloMessage(data=self._latest_hello))
        for status in self._latest_status.values():
            subscriber.mailbox.offer(StatusMessage(data=status))
        for cached in self._latest_segments.values():
            subscriber.mailbox.offer(SegmentMessage(data=cached))

        if role.is_viewer:
            self._note_demand()
            self.metrics.note_connect(self.viewer_count, self.viewers_by_language)

        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.pop(subscriber.id, None)
        subscriber.mailbox.close()

        if not subscriber.role.is_viewer:
            return

        self.metrics.note_disconnect(subscriber.id, subscriber.mailbox)

        # Clear synchronously, before the event loop can run a reconnect handler:
        # otherwise a viewer that leaves and returns within one frame would be seeded
        # with the previous line the moment it subscribes. Only this role's own
        # language is dropped, so one language's viewer leaving does not clear another
        # language's line while its viewers remain.
        language = subscriber.role.language
        if (
            self._audience_gating
            and language is not None
            and self.viewer_count_for(language) == 0
        ):
            self._latest_segments.pop(language, None)

        self._note_demand()

    def publish(self, message: WireMessage) -> None:
        """Synchronous, non-blocking, safe to call from the pipeline's hot path."""

        if isinstance(message, SegmentMessage):
            language = message.data.language
            highest = self._latest_highest.get(language)
            fresh = highest is None or (message.data.seq, message.data.rev) > highest
            # Never cache a line that arrived after the audience for that language
            # left: a late translation must not seed the next viewer with text nobody
            # watched. Never cache a stale revision either, so the snapshot obeys the
            # same ordering rule as a mailbox.
            if fresh:
                self._latest_highest[language] = (message.data.seq, message.data.rev)
                if not (self._audience_gating and self.viewer_count_for(language) == 0):
                    self._latest_segments[language] = message.data
            self.encode(message)          # pay for one encoding, not one per viewer
        elif isinstance(message, StatusMessage):
            self._latest_status[message.data.language] = message.data
        elif isinstance(message, HelloMessage):
            # Cached, not just broadcast: Session publishes hello once at startup,
            # so without this a viewer joining mid-lecture would never learn the
            # session identity or the protocol version.
            self._latest_hello = message.data

        for subscriber in self._subscribers.values():
            subscriber.mailbox.offer(message)

    def publish_segment(self, segment: SubtitleSegment) -> None:
        self.publish(SegmentMessage(data=segment))

    def publish_status(
        self, status: PipelineStatus, detail: str | None = None, language: str | None = None
    ) -> None:
        """Report health to everyone, or, with ``language``, to that language's readers."""

        self.publish(
            StatusMessage(data=StatusPayload(status=status, detail=detail, language=language))
        )

    def publish_error(self, message: str, stage: Stage) -> None:
        self.publish(ErrorMessage(data=ErrorPayload(message=message, stage=stage)))

    def encode(self, message: WireMessage) -> str:
        """Serialise, caching segment payloads so every viewer shares one encoding.

        The cache key includes the language and whether the private fields were
        present. It must: lines of two languages can share a ``(seq, rev)``, and viewers
        get a stripped copy while admins do not. Without both parts the entries collide
        and a viewer could be handed another language's text, or the speaker's own
        words, through a cache hit.
        """

        if not isinstance(message, SegmentMessage):
            return dumps(message)

        segment = message.data
        has_private_fields = segment.src is not None or segment.words is not None
        key = (segment.language, segment.seq, segment.rev, has_private_fields)
        cached = self._encode_cache.get(key)
        if cached is not None:
            self._encode_cache.move_to_end(key)
            return cached

        encoded = dumps(message)
        self._encode_cache[key] = encoded
        if len(self._encode_cache) > _ENCODE_CACHE_SIZE:
            self._encode_cache.popitem(last=False)
        return encoded

    @staticmethod
    def ping() -> str:
        """The heartbeat, which is written to a socket directly and never queued."""

        return dumps(PingMessage())


@runtime_checkable
class SegmentPublisher(Protocol):
    """Anything able to drive a session's subtitles.

    Implemented by the live pipeline and by the simulation replay. That shared
    interface is what lets the overlay be built and tuned with no credentials and
    no microphone, and means the overlay cannot behave differently between a
    demo and a real lecture.

    A publisher may also declare ``supported_languages``, the target languages it can
    produce; the session then refuses a viewer who asks for any other.
    """

    async def run(self, hub: Hub, *, session_id: str, stop: asyncio.Event) -> None:
        """Publish until ``stop`` is set, then return."""
        ...
