"""Streaming ASR adapter interface.

Defined in ``docs/04-adapters.md``. The rule: provider-specific code lives behind
this Protocol and owns nothing else. No provider type, exception or identifier
may leak past an adapter, so that swapping providers is a config change and can
happen mid-lecture if a provider degrades.

Not implemented yet (M2). This module exists so the interface is fixed before
any concrete adapter is written.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from livesubs.events import WordTiming

__all__ = ["AsrConfig", "AsrEvent", "AsrProvider"]


@dataclass(slots=True)
class AsrConfig:
    """Everything an adapter needs to open a stream.

    ``language`` is the SOURCE language, which is English here. The German side
    is the MT adapter's business, not this one's.
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

    ``partial`` events supersede each other for the same span; ``final`` closes
    it. The segmenter treats ``speech_final`` as a HINT, never a requirement -
    see ``docs/03-segmentation.md`` on double-endpointing.
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
        """Yield transcription updates as they arrive."""
        ...

    async def close(self) -> None:
        """Close the upstream stream and release resources."""
        ...

    @property
    def healthy(self) -> bool:
        """Whether the upstream connection is currently usable.

        Must reflect the CONNECTION, not the last response, so that a silently
        dead socket is detectable. Drives failover per ``docs/04-adapters.md``.
        """
        ...
