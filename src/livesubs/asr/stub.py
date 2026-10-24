"""A credential-free ASR adapter.

This is a **real adapter**, not a mock: it implements
:class:`~livesubs.asr.base.AsrProvider`, owns its own endpointing, and emits
partials and finals with timings taken from the audio it was fed. What it does not
do is recognise speech.

That is enough to exercise every downstream stage — the segmenter, the pacing
policy, the translation plumbing, the hub, the overlay and the viewer — with no
API key, no microphone and no network. So the pipeline's *structure* can be
developed and regression-tested before any vendor is chosen, and a bug in the
wiring cannot hide behind a provider outage.

Partial text grows through the phrase as the utterance proceeds, which is what a
real streaming provider does. It is deliberately spelled out rather than
plausible: nobody should mistake this output for a transcript.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from livesubs.asr.base import AsrConfig, AsrEvent
from livesubs.ingest import Ingest, IngestConfig

__all__ = ["DEFAULT_PHRASES", "StubAsr"]

#: Deliberately varied in length, so the two-line clamp and the reading-time
#: hold are exercised rather than merely compiled.
DEFAULT_PHRASES: tuple[str, ...] = (
    "This is a synthetic segment standing in for recognised speech.",
    "The capture, the segmentation and the overlay are real; only the words are invented.",
    (
        "A longer sentence is included on purpose, because the two line clamp and "
        "the reading time policy only misbehave on input that is long enough to "
        "need them, and short test strings never find those bugs."
    ),
    "No API key was used to produce this line.",
)


class StubAsr:
    """An ``AsrProvider`` that needs no network.

    Endpointing is its own, so it reports ``speech_final`` exactly as a real
    streaming provider would. That matters: it means the segmenter's
    "trust the provider" path is exercised by the tests rather than bypassed.
    """

    def __init__(
        self,
        *,
        phrases: Sequence[str] = DEFAULT_PHRASES,
        sample_rate: int = 16_000,
        endpoint_silence_ms: int = 350,
        #: How often a new partial supersedes the last one, while speech continues.
        partial_interval_ms: int = 300,
        #: Duration at which a partial has grown to the full phrase.
        partial_target_ms: int = 2000,
        source_language: str | None = None,
    ) -> None:
        self.phrases = tuple(phrases) or DEFAULT_PHRASES
        self.source_language = (
            "en" if self.phrases == DEFAULT_PHRASES else source_language
        )
        self.sample_rate = sample_rate
        self.endpoint_silence_ms = endpoint_silence_ms
        self.partial_interval_ms = partial_interval_ms
        self.partial_target_ms = partial_target_ms

        self._ingest = Ingest(IngestConfig(sample_rate=sample_rate))
        self._queue: asyncio.Queue[AsrEvent | None] = asyncio.Queue()
        self._open = False
        self._index = 0
        self._utterance_start_ms: int | None = None
        self._last_voice_ms = 0
        self._last_partial_ms = 0

    @property
    def healthy(self) -> bool:
        return self._open

    async def open(self, cfg: AsrConfig) -> None:
        """Record the negotiated config. There is nothing to connect to."""

        self._open = True
        self._ingest = Ingest(IngestConfig(sample_rate=cfg.sample_rate))
        self.sample_rate = cfg.sample_rate

    async def push(self, pcm: bytes) -> None:
        """Condition the frame, then emit partials and finals off its own VAD.

        Non-blocking: it never awaits anything, so a slow consumer of ``events()``
        cannot stall capture. Events queue in memory and the pipeline drains them
        on every frame, so the queue depth stays at one or two.
        """

        if not self._open:
            raise RuntimeError("StubAsr.push() before open()")

        stats = self._ingest.process(pcm)
        now = stats.now_ms

        if stats.speech:
            if self._utterance_start_ms is None:
                self._utterance_start_ms = now
                self._last_partial_ms = now

            self._last_voice_ms = now

            if now - self._last_partial_ms >= self.partial_interval_ms:
                self._last_partial_ms = now
                self._queue.put_nowait(
                    self._make_event(kind="partial", now_ms=now, speech_final=False)
                )
            return

        # Not speech. Close the utterance once the pause is long enough, and only
        # if one was open.
        if self._utterance_start_ms is None:
            return

        if now - self._last_voice_ms >= self.endpoint_silence_ms:
            self._queue.put_nowait(
                self._make_event(kind="final", now_ms=now, speech_final=True)
            )
            self._index += 1
            self._utterance_start_ms = None
            self._last_partial_ms = now

    def _make_event(self, *, kind: str, now_ms: int, speech_final: bool) -> AsrEvent:
        start_ms = self._utterance_start_ms if self._utterance_start_ms is not None else now_ms
        return AsrEvent(
            kind=kind,  # type: ignore[arg-type]
            text=self._text_for(now_ms=now_ms, final=kind == "final"),
            start_ms=start_ms,
            end_ms=now_ms,
            confidence=1.0,
            words=None,
            speech_final=speech_final,
            language=self.source_language,
        )

    def _text_for(self, *, now_ms: int, final: bool) -> str:
        """Grow a phrase to full length, or return it whole for a final.

        Cutting at a word boundary rather than mid-word keeps the partial plausible;
        cutting mid-word makes the overlay's cross-fade look broken and teaches
        nothing, because the real providers do not do it.
        """

        phrase = self.phrases[self._index % len(self.phrases)]

        if final:
            return phrase

        start_ms = self._utterance_start_ms
        elapsed = max(0, now_ms - (start_ms if start_ms is not None else now_ms))
        fraction = min(1.0, elapsed / self.partial_target_ms)
        head = phrase[: max(1, round(len(phrase) * fraction))]

        boundary = head.rsplit(" ", 1)[0]
        return boundary or head

    async def events(self) -> AsyncIterator[AsrEvent]:
        """Yield events until :meth:`close`. Never blocks the pusher."""

        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        """Flush any open utterance, then end the event stream.

        The flush is not politeness: a lecture that stops mid-sentence would
        otherwise lose its last line, and the same bug in a real adapter would be
        invisible until it mattered.
        """

        if not self._open:
            return

        self._open = False

        if self._utterance_start_ms is not None:
            self._queue.put_nowait(
                self._make_event(kind="final", now_ms=self._ingest.total_ms, speech_final=True)
            )
            self._utterance_start_ms = None

        self._queue.put_nowait(None)
