"""Machine translation adapter interface.

Defined in ``docs/04-adapters.md`` and ``docs/05-providers.md``.

Streaming is REQUIRED, not optional: the first German words must reach the screen
before the sentence finishes, which is one of the three techniques that make the
pipeline feel instant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = ["MtChunk", "MtContext", "MtError", "MtProvider"]


class MtError(RuntimeError):
    """A translation request failed.

    Raised by an adapter from ``translate`` in place of whatever its transport raised, so
    no provider type crosses the protocol. It is raised, not swallowed: a stream that dies
    half way has produced a truncated line, and the only party that can decide what a
    viewer should see then is the one that knows what has already been shown.
    """


@dataclass(slots=True)
class MtContext:
    """Context carried alongside a segment to be translated.

    ``source_context`` exists because German is verb-final: translating an
    isolated English fragment produces grammatically wrong German. Previous
    source sentences let the model resolve word order and pronoun gender.

    IMPORTANT: context goes in the USER message, never the system prompt.
    DeepSeek's on-disk context cache only hits on a stable prefix, so putting
    rolling context in the system prompt would destroy the cache and roughly
    triple the cost. See ``docs/05-providers.md``.
    """

    source_context: list[str] = field(default_factory=list)
    #: Previous German output, for terminology consistency across a lecture.
    target_context: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MtChunk:
    """One streamed piece of a translation."""

    text_delta: str
    done: bool = False


@runtime_checkable
class MtProvider(Protocol):
    """A streaming English-to-German translator."""

    async def warm(self) -> None:
        """Build the provider's cache BEFORE the lecture starts.

        Caching is best-effort and has no latency guarantee, and cache
        construction takes seconds; without warming, the first real segment pays
        that cost in front of an audience. Mandatory, not an optimisation.
        """
        ...

    def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
        """Translate one segment, yielding deltas as they are generated.

        Raises :class:`MtError` if the request fails, including after some deltas have
        been yielded. Never ends normally on a partial translation.
        """
        ...

    @property
    def healthy(self) -> bool:
        """Whether the provider is currently usable. Drives failover."""
        ...
