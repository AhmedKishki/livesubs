"""A credential-free machine-translation adapter.

Implements :class:`~livesubs.mt.base.MtProvider` and streams, because streaming is
required rather than optional: yielding deltas word by word is what exercises the
overlay's in-place revision path.

It does not translate. It marks the text as German so that nothing downstream can
mistake it for a real translation, and it is word-for-word identical to its input,
which makes it genuinely useful: an English word appearing in the output of a
pipeline that claims German-only is immediately visible.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from livesubs.mt.base import MtChunk, MtContext

__all__ = ["PassthroughMt"]

#: Prefixed to every output line. Deliberately not a German word, so that a
#: passthrough line can never be mistaken for a translation in a screenshot.
MARKER = "[passthrough]"


class PassthroughMt:
    """Echoes its input, streamed word by word."""

    def __init__(self, *, word_delay_ms: int = 0) -> None:
        #: Zero by default: tests want speed, and a demo wants to see the real
        #: latency of the rest of the pipeline rather than an invented one.
        self.word_delay_ms = word_delay_ms
        self._warmed = False

    @property
    def healthy(self) -> bool:
        return self._warmed

    async def warm(self) -> None:
        """No cache to build. Still awaited by the pipeline, so the call order of a
        real provider is exercised rather than skipped."""

        self._warmed = True

    async def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
        """Yield the source text in word-sized deltas."""

        words = src.split()
        if not words:
            yield MtChunk(text_delta="", done=True)
            return

        for index, word in enumerate(words):
            # The marker rides in the stream, not just in render(), so the text a
            # viewer actually receives carries it too.
            delta = f"{MARKER} {word}" if index == 0 else f" {word}"
            yield MtChunk(text_delta=delta, done=False)
            if self.word_delay_ms:
                await asyncio.sleep(self.word_delay_ms / 1000.0)

        yield MtChunk(text_delta="", done=True)

    def render(self, src: str) -> str:
        """The whole line at once, for callers that do not stream."""

        return f"{MARKER} {src}".strip()
