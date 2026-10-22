"""Streaming ASR adapter interface.

Defined in ``docs/04-adapters.md``. The rule: provider-specific code lives behind
this Protocol and owns nothing else. No provider type, exception or identifier
may leak past an adapter, so that swapping providers is a config change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from livesubs.events import WordTiming

__all__ = ["AsrConfig", "AsrEvent", "AsrProvider", "Seekable"]


@dataclass(slots=True)
class AsrConfig:
    """Everything an adapter needs to open a stream.

    ``language`` is the SOURCE language, the one the speaker uses, or every language of a
    conversation joined by ``+`` (``en+de``) when speakers switch between them. An adapter
    that cannot recognise a conversation in one stream refuses it at ``open``. Whatever the
    listeners read is the MT adapter's business, not this one's.
    """

    language: str = "en"
    sample_rate: int = 16_000
    channels: int = 1
    #: Domain vocabulary hints, e.g. a course glossary. Both Deepgram (keyterms)
    #: and OpenAI (keyword hints) accept a form of this.
    keyterms: tuple[str, ...] = ()
    #: Free-text context (lecture title, abstract). Decisively improves accuracy
    #: on proper nouns, and is cheap.
    context: str | None = None


@dataclass(slots=True)
class AsrEvent:
    """One transcription update.

    A *span* is the stretch of audio one hypothesis describes. ``partial`` events
    supersede each other for the current span. ``final`` settles it: the text is
    complete, it will not change, and the next event starts a new span that does not
    repeat it. A provider that streams in chunks settles several spans inside one
    utterance, so a ``final`` is not the speaker stopping.

    ``speech_final`` is that, the provider's own end-of-utterance signal, and it is
    independent of ``kind``. The segmenter treats it as a HINT, never a requirement -
    see ``docs/03-segmentation.md`` on double-endpointing - and the transcript
    assembler (``livesubs.transcript``) is what turns spans into the text of a segment.

    ``words`` carries per-word timings on the provider's own stream clock, which is
    what lets a segment cut in the middle of a span avoid repeating what it already
    sent. Providers without timings leave it ``None``.
    """

    kind: Literal["partial", "final"]
    text: str
    start_ms: int
    end_ms: int
    confidence: float | None = None
    words: list[WordTiming] | None = None
    speech_final: bool = False


@runtime_checkable
class AsrProvider(Protocol):
    """A streaming speech-to-text stream.

    ``push`` takes raw 16 kHz mono signed-16-bit little-endian PCM. Framing,
    base64 encoding and transport are the adapter's business.
    """

    async def open(self, cfg: AsrConfig) -> None:
        """Open the upstream stream. Raises on auth failure rather than retrying."""
        ...

    async def push(self, pcm: bytes) -> None:
        """Feed audio. Must not block the caller for longer than a frame."""
        ...

    def events(self) -> AsyncIterator[AsrEvent]:
        """Yield transcription updates as they arrive.

        The iterator MUST end, or raise, when the upstream stream dies. The pipeline
        learns a recogniser is gone only that way, so an adapter that goes quiet and
        waits for ever leaves the lecture silently without subtitles.
        """
        ...

    async def close(self) -> None:
        """Close the upstream stream and release resources."""
        ...

    @property
    def healthy(self) -> bool:
        """Whether the upstream connection is currently usable.

        Must reflect the CONNECTION, not the last response, so that a silently
        dead socket is detectable. The pipeline polls it as a second line of defence
        behind the event stream ending.
        """
        ...


@runtime_checkable
class Seekable(Protocol):
    """An optional capability: a recogniser that must stay aligned with the audio clock.

    The pipeline keeps capturing while no viewer is watching, so a recogniser opened later
    would otherwise start from zero. One that replays a timed transcript needs to be told
    where the audio is, or it would replay lines the room has already heard. Providers that
    recognise live audio have no use for it and leave it out.
    """

    def seek(self, audio_ms: int) -> None:
        """Skip everything that begins at or before ``audio_ms``."""
        ...
