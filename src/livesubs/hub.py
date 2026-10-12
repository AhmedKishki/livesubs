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

    VIEWER_AR = "viewer_ar"
    """Attendee who explicitly opted into Arabic. Arabic only; never ``src``."""

    ADMIN = "admin"
    """Operator console. Also receives the English source, both target languages, errors."""


#: The target language a viewing role reads. Admins are deliberately absent: they
#: receive both streams.
VIEWER_LANGUAGE: dict[ClientRole, str] = {
    ClientRole.VIEWER: "de",
    ClientRole.VIEWER_AR: "ar",
}

#: Every target language the pipeline may produce, in drain order.
TARGET_LANGUAGES: tuple[str, ...] = ("de", "ar")


def visible_languages(role: ClientRole) -> tuple[str, ...]:
    """Which target streams a role may receive.

    Enforced on the server, never trusted to the client. An operator sees every
    stream; a viewing role sees its own language and nothing else.
    """

    if role is ClientRole.ADMIN:
        return TARGET_LANGUAGES
    language = VIEWER_LANGUAGE.get(role)
    return (language,) if language is not None else ()


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
        #: The target languages this role may receive. A German viewer never gets an
        #: Arabic segment at all: it is filtered here, on the server, rather than
        #: hidden by the page.
        self._allowed = visible_languages(role)
        self._hello: HelloPayload | None = None
        self._status: StatusPayload | None = None
        self._errors: deque[ErrorPayload] = deque(maxlen=self.MAX_ERRORS)
        #: At most one pending segment per target language, each with its own ordering
        #: guard, so a German seq cannot collide with an Arabic one.
        self._segments: dict[str, SubtitleSegment | None] = dict.fromkeys(self._allowed)
        self._highest: dict[str, tuple[int, int] | None] = dict.fromkeys(self._allowed)
        self._event = asyncio.Event()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def _segment(self) -> SubtitleSegment | None:
        """The pending German segment, kept for callers of the previous single-channel
        mailbox. The mailbox now keeps one pending segment per language."""

        return self._segments.get("de")

    @property
    def has_pending(self) -> bool:
        return any(
            (
                self._hello is not None,
                self._status is not None,
                bool(self._errors),
                any(segment is not None for segment in self._segments.values()),
            )
        )

    def _for_role(self, segment: SubtitleSegment) -> SubtitleSegment:
        """The copy this role may hold, with everything it must not see removed."""

        if self.role is ClientRole.ADMIN:
            return segment

        if segment.language == "ar":
            # An Arabic viewer reads Arabic and nothing else. The German text is
            # stripped rather than hidden, so a device never receives it.
            return segment.model_copy(update={"src": None, "de": "", "words": None})

        # A German viewer never receives Arabic text; ``ar`` is already None on a
        # German segment, and ``src`` is stripped.
        return segment.model_copy(update={"src": None, "ar": None, "words": None})

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
                language = segment.language
                # Server-side language filter: a role that may not read this stream
                # never receives it, rather than being trusted to ignore it.
                if language not in self._segments:
                    return

                # Ordering guard, per language: refuse anything at or below what we
                # already hold, so a client can never see `final` then `draft`.
                highest = self._highest[language]
                if highest is not None and (segment.seq, segment.rev) <= highest:
                    return
                self._highest[language] = (segment.seq, segment.rev)
                self._segments[language] = self._for_role(segment)

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

        for language in TARGET_LANGUAGES:
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

    def __init__(self) -> None:
        self._subscribers: dict[str, Subscriber] = {}
        self._ids = itertools.count(1)
        self._encode_cache: OrderedDict[tuple[str, int, int, bool], str] = OrderedDict()
        # Snapshot state, so a late joiner is fully oriented on connect: who the
        # session is, how healthy it is, and what is currently on screen. One
        # cached line per target language, so a German joiner is not shown the
        # Arabic line and vice versa.
        self._latest_hello: HelloPayload | None = None
        self._latest_segments: dict[str, SubtitleSegment] = {}
        #: The highest ``(seq, rev)`` cached per language, so a stale revision cannot
        #: overwrite the snapshot a late joiner is seeded with. Kept per language so a
        #: German seq cannot block an Arabic one.
        self._latest_highest: dict[str, tuple[int, int] | None] = dict.fromkeys(
            TARGET_LANGUAGES
        )
        self._latest_status: StatusPayload | None = None

        #: Whether a pipeline is driving paid work from this hub's audience. Off by
        #: default so a non-gated replay keeps its cached line across a reconnect.
        self._audience_gating = False
        #: Presence, not count: the generation advances only when the audience crosses
        #: between none and some, so a second viewer is not mistaken for a fresh one.
        #: This is the *shared* occupancy that decides whether the billed ASR runs.
        self._demand_present = False
        self._demand_generation = 0
        self._demand_event = asyncio.Event()
        #: Per-language presence and generation. Adding an Arabic viewer while a
        #: German one is present must NOT advance the shared occupancy, or the
        #: recogniser would be needlessly torn down and rebuilt. Each language gets
        #: its own revision so its translator can be started and stopped on its own.
        self._lang_present: dict[str, bool] = dict.fromkeys(TARGET_LANGUAGES, False)
        self._lang_generation: dict[str, int] = dict.fromkeys(TARGET_LANGUAGES, 0)
        self._lang_event: dict[str, asyncio.Event] = {
            language: asyncio.Event() for language in TARGET_LANGUAGES
        }

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
        billed ASR stream or warm a billed translator on an empty room. This is the
        shared count: German and Arabic viewers both keep the recogniser open.
        """

        return sum(
            1
            for subscriber in self._subscribers.values()
            if subscriber.role in VIEWER_LANGUAGE
        )

    def viewer_count_for(self, language: str) -> int:
        """Connected viewers reading one target language.

        Drives per-language paid work: German translation runs only while a German
        viewer is present, Arabic only while an Arabic viewer is, independent of each
        other.
        """

        role = ClientRole.VIEWER_AR if language == "ar" else ClientRole.VIEWER
        return sum(
            1 for subscriber in self._subscribers.values() if subscriber.role is role
        )

    @property
    def viewers_by_language(self) -> dict[str, int]:
        return {language: self.viewer_count_for(language) for language in TARGET_LANGUAGES}

    @property
    def has_viewers(self) -> bool:
        return self.viewer_count > 0

    @property
    def demand_generation(self) -> int:
        """A revision that advances only when the audience crosses zero.

        A consumer records the value when it starts paid work and can tell, with no
        dependence on audio frames, whether the audience it opened for is still the
        current one. Going to zero and back produces a new value, so a fast reconnect
        invalidates any attempt that belonged to the previous occupancy. Only the
        transition between no viewers and some viewers counts, so adding an Arabic
        viewer to a room that already has a German one leaves the recogniser alone.
        """

        return self._demand_generation

    def demand_generation_for(self, language: str) -> int:
        """A revision that advances only when one language's audience crosses zero."""

        return self._lang_generation.get(language, 0)

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

    async def wait_for_demand_change_for(
        self, language: str, generation: int, timeout: float | None = None
    ) -> int:
        """Wait until one language's audience generation differs from ``generation``."""

        event = self._lang_event.get(language)
        if event is None:
            return generation

        while self._lang_generation.get(language) == generation:
            event = self._lang_event.get(language)
            if event is None or self._lang_generation.get(language) != generation:
                break
            if timeout is None:
                await event.wait()
            else:
                try:
                    await asyncio.wait_for(event.wait(), timeout=timeout)
                except TimeoutError:
                    break
        return self._lang_generation.get(language, generation)

    @property
    def _latest_segment(self) -> SubtitleSegment | None:
        """The cached German line, for backward compatibility with older callers.

        The hub now caches one line per target language; this exposes the German one,
        which is all a German viewer or the previous single-language hub ever saw.
        """

        return self._latest_segments.get("de")

    def forget_segment_snapshot(self, language: str | None = None) -> None:
        """Drop the cached current line, for one language or for all.

        Called when an audience leaves: the cached line belongs to a lecture that is
        now paused, and seeding the next viewer with it would replay stale or
        unfinished text that no longer describes what is on screen.
        """

        if language is None:
            self._latest_segments.clear()
            self._latest_highest = dict.fromkeys(TARGET_LANGUAGES)
        else:
            self._latest_segments.pop(language, None)
            self._latest_highest[language] = None

    def _note_demand(self) -> None:
        """Advance the generations and wake waiters when audience presence flips.

        Called synchronously from :meth:`subscribe` and :meth:`unsubscribe`, both of
        which run on the event loop. Only a presence change counts, so a second viewer
        joining does not look like a new occupancy to an in-flight open, and an Arabic
        viewer joining a room that already has a German one does not look like a new
        shared occupancy either.
        """

        present = self.has_viewers
        if present != self._demand_present:
            self._demand_present = present
            self._demand_generation += 1
            previous, self._demand_event = self._demand_event, asyncio.Event()
            previous.set()

        for language in TARGET_LANGUAGES:
            language_present = self.viewer_count_for(language) > 0
            if language_present == self._lang_present[language]:
                continue
            self._lang_present[language] = language_present
            self._lang_generation[language] += 1
            previous, self._lang_event[language] = (
                self._lang_event[language],
                asyncio.Event(),
            )
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
        # first three messages are always identity, health, current line. Only the
        # languages the role may read are seeded.
        if self._latest_hello is not None:
            subscriber.mailbox.offer(HelloMessage(data=self._latest_hello))
        if self._latest_status is not None:
            subscriber.mailbox.offer(StatusMessage(data=self._latest_status))
        for language in visible_languages(role):
            cached = self._latest_segments.get(language)
            if cached is not None:
                subscriber.mailbox.offer(SegmentMessage(data=cached))

        if role in VIEWER_LANGUAGE:
            self._note_demand()

        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.pop(subscriber.id, None)
        subscriber.mailbox.close()

        if subscriber.role not in VIEWER_LANGUAGE:
            return

        # Clear synchronously, before the event loop can run a reconnect handler:
        # otherwise a viewer that leaves and returns within one frame would be seeded
        # with the previous line the moment it subscribes. Only this role's own
        # language is dropped, so a German viewer leaving does not clear the Arabic
        # line while Arabic viewers remain, and vice versa.
        if self._audience_gating:
            language = VIEWER_LANGUAGE[subscriber.role]
            if self.viewer_count_for(language) == 0:
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

        The cache key includes the target language and whether ``src`` was present.
        It must: a German segment and an Arabic segment can share a ``(seq, rev)``,
        and viewers get a stripped copy while admins do not. Without both parts the
        entries collide and a viewer could be handed another language's text or the
        English source through a cache hit.
        """

        if not isinstance(message, SegmentMessage):
            return dumps(message)

        segment = message.data
        has_private_fields = (
            segment.src is not None
            or segment.words is not None
            or (segment.ar is not None if segment.language == "de" else bool(segment.de))
        )
        key = (
            message.data.language,
            message.data.seq,
            message.data.rev,
            has_private_fields,
        )
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
