"""The input stage with the recogniser replaced by a transcript.

This is **not** an ASR and must never be mistaken for one: the words come from a transcript
someone already wrote, not from the audio. It exists because it splits one hard problem
into two measurable ones - and because it is the only way to watch the real translator, the
real segmentation and the real overlay working on real speech before an ASR key exists.

What is real: the audio clock, the conditioning, the voice detection, the segmentation, the
translation, the hub, the overlay, the artefacts. What is not: the words. ``live`` is the
approach that recognises speech, and this one is named so that nobody confuses them.

Timings come from the **audio clock**, accumulated from the length of the frames pushed, for
the same reason ``pipeline.py`` uses it: a file replayed faster than real time must behave
exactly like a live device. The cue's own end time is carried for information; the pipeline
re-segments the audio on its own voice detection, so what decides when a line is displayed
is the real audio, not this file.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from livesubs.asr.base import AsrConfig, AsrEvent
from livesubs.subtitles import Cue

__all__ = ["TranscriptAsr"]


class TranscriptAsr:
    """Emits each cue's text when the audio clock reaches the cue."""

    def __init__(self, cues: Sequence[Cue], *, sample_rate: int = 16_000) -> None:
        self.cues = sorted(cues, key=lambda cue: cue.start_ms)
        self.sample_rate = sample_rate

        self._open = False
        self._consumed_ms = 0
        self._next = 0
        #: A queue, not a list, and the ``None`` sentinel is what ends the event stream -
        #: the same contract ``StubAsr`` keeps, which the pipeline's collector relies on.
        self._queue: asyncio.Queue[AsrEvent | None] = asyncio.Queue()

    @property
    def healthy(self) -> bool:
        return self._open

    @property
    def now_ms(self) -> int:
        """The audio clock this adapter has consumed."""

        return self._consumed_ms

    @property
    def finished(self) -> bool:
        """Whether every cue has been emitted, which is when a session has nothing left."""

        return self._next >= len(self.cues)

    async def open(self, cfg: AsrConfig) -> None:
        self._open = True
        self.sample_rate = cfg.sample_rate or self.sample_rate
        self._consumed_ms = 0
        self._next = 0
        self._queue = asyncio.Queue()

    async def push(self, pcm: bytes) -> None:
        """Advance the clock and queue whatever the transcript says by now.

        Deliberately free of numpy and of a voice detector: this adapter is not looking for
        speech, it is looking up a table, and the pipeline runs its own detector on the same
        audio.
        """

        if not self._open:
            raise RuntimeError("TranscriptAsr.push() before open()")

        samples = len(pcm) // 2
        self._consumed_ms += round(samples * 1000 / self.sample_rate)

        while self._next < len(self.cues) and self.cues[self._next].start_ms <= self._consumed_ms:
            cue = self.cues[self._next]
            self._next += 1
            if not cue.text.strip():
                continue
            self._queue.put_nowait(
                AsrEvent(
                    kind="final",
                    text=cue.text,
                    start_ms=max(0, cue.start_ms),
                    end_ms=max(cue.start_ms, cue.end_ms),
                    confidence=None,
                    words=None,
                    # The transcript is a finished transcript, so every cue is an end of
                    # utterance by construction.
                    speech_final=True,
                )
            )

    async def events(self) -> AsyncIterator[AsrEvent]:
        """Yield cues as the clock reaches them, until :meth:`close`."""

        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        """Flush nothing: unlike a recogniser, there is no open utterance to commit."""

        if not self._open:
            return

        self._open = False
        self._queue.put_nowait(None)
