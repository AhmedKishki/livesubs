"""The live pipeline against messages shaped like a chunking recogniser's.

The simulated recognisers emit one whole phrase per utterance, which is the one shape that
hides the problem this file exists for: Deepgram settles a chunk every few seconds, an
interim result is cumulative from the start of its chunk and knows nothing about the
pipeline's own cuts, and the real recordings showed cues repeating their opening and
words such as a surname missing.

Everything here goes through the real message parser and the real pipeline. Only the
network is replaced, by a script of Deepgram-format messages that arrive at chosen
moments on the audio clock.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator

import pytest

from livesubs.asr.base import AsrConfig, AsrEvent
from livesubs.asr.deepgram import _parse_message
from livesubs.audio.base import AudioFrame
from livesubs.hub import Hub
from livesubs.mt.base import MtChunk, MtContext
from livesubs.pipeline import LivePipeline, PipelineConfig

RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000


def tone_frame(rms_dbfs: float) -> bytes:
    amplitude = 32768 * 10 ** (rms_dbfs / 20) * math.sqrt(2)
    return b"".join(
        int(amplitude * math.sin(2 * math.pi * 220 * n / RATE)).to_bytes(2, "little", signed=True)
        for n in range(FRAME_SAMPLES)
    )


class SpeechIntervals:
    """Audio that is a tone while someone is speaking and silence otherwise."""

    def __init__(self, intervals: list[tuple[float, float]], total_s: float, level: float = -18) -> None:
        self.intervals = intervals
        self.total_s = total_s
        self.loud = tone_frame(level)
        self._closed = False

    @property
    def alive(self) -> bool:
        return not self._closed

    async def frames(self) -> AsyncIterator[AudioFrame]:
        quiet = b"\x00" * (FRAME_SAMPLES * 2)
        n = 0
        while n * FRAME_MS < self.total_s * 1000 and not self._closed:
            t = n * FRAME_MS / 1000
            speaking = any(a <= t < b for a, b in self.intervals)
            yield AudioFrame(pcm=self.loud if speaking else quiet, start_ms=n * FRAME_MS, duration_ms=FRAME_MS)
            n += 1
            await asyncio.sleep(0)

    async def close(self) -> None:
        self._closed = True


def message(words: list[tuple[str, float, float]], start: float, duration: float, *,
            is_final: bool, speech_final: bool, timed: bool) -> str:
    alternative: dict = {"transcript": " ".join(w[0] for w in words), "confidence": 0.95}
    if timed:
        alternative["words"] = [
            {"word": w[0], "punctuated_word": w[0], "start": w[1], "end": w[2], "confidence": 0.9}
            for w in words
        ]
    return json.dumps({
        "type": "Results", "is_final": is_final, "speech_final": speech_final,
        "start": start, "duration": duration,
        "channel": {"alternatives": [alternative]},
    })


def lecture(sentences: list[int], *, word_s: float, pause_s: float, chunk_s: float,
            latency_s: float, timed: bool, lead_s: float = 0.4):
    """A lecture and the Deepgram messages that describe it.

    Following Deepgram's documented behaviour: an interim result covers the current
    chunk only and grows; at a chunk boundary a settled result carries just that chunk;
    the next result starts from empty; the last chunk of a sentence carries the endpoint.
    """

    truth: list[str] = []
    intervals: list[tuple[float, float]] = []
    script: list[tuple[float, str]] = []
    t = lead_s
    for count in sentences:
        begin = t
        spoken = []
        for _ in range(count):
            truth.append(f"w{len(truth) + 1:03d}")
            spoken.append((truth[-1], t, t + word_s - 0.05))
            t += word_s
        end = spoken[-1][2]
        intervals.append((begin, end + 0.05))
        chunks = max(1, math.ceil((end - begin) / chunk_s))
        for index in range(chunks):
            first, last_edge = begin + index * chunk_s, min(end, begin + (index + 1) * chunk_s)
            is_last = index == chunks - 1
            inside = [w for w in spoken if first <= w[1] < last_edge + (1e-9 if is_last else 0)]
            at = first + 0.25
            while at < last_edge - 1e-9:
                heard = [w for w in inside if w[2] <= at]
                if heard:
                    script.append((at + latency_s, message(heard, first, at - first,
                                                           is_final=False, speech_final=False, timed=timed)))
                at += 0.25
            script.append((last_edge + latency_s + (0.1 if is_last else 0.0),
                           message(inside, first, last_edge - first,
                                   is_final=True, speech_final=is_last, timed=timed)))
        t += pause_s
    script.sort(key=lambda pair: pair[0])
    return truth, SpeechIntervals(intervals, t + 2.5), script


class ScriptedRecogniser:
    """A recogniser whose messages arrive at scripted moments of the audio clock."""

    def __init__(self, script: list[tuple[float, str]]) -> None:
        self._script = list(script)
        self._clock_ms = 0
        self._events: asyncio.Queue[AsrEvent | None] = asyncio.Queue()
        self._open = False

    @property
    def healthy(self) -> bool:
        return self._open

    async def open(self, cfg: AsrConfig) -> None:
        self._open = True

    async def push(self, pcm: bytes) -> None:
        self._clock_ms += len(pcm) // 2 * 1000 // RATE
        while self._script and self._script[0][0] * 1000 <= self._clock_ms:
            event = _parse_message(self._script.pop(0)[1])
            if event is not None:
                self._events.put_nowait(event)

    async def events(self) -> AsyncIterator[AsrEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        self._open = False
        self._events.put_nowait(None)


class RecordingTranslator:
    def __init__(self) -> None:
        self.sources: list[str] = []

    async def warm(self) -> None:
        pass

    @property
    def healthy(self) -> bool:
        return True

    async def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
        self.sources.append(src)
        yield MtChunk(text_delta=src, done=False)
        yield MtChunk(text_delta="", done=True)


async def run_pipeline(source: SpeechIntervals, script: list[tuple[float, str]]) -> list[str]:
    translator = RecordingTranslator()
    pipeline = LivePipeline(
        source=source,
        asr=ScriptedRecogniser(script),
        translators={"de": translator},
        config=PipelineConfig(),
    )
    await pipeline.run(Hub(("de",)), session_id="replay", stop=asyncio.Event())
    return translator.sources


def sent_words(sources: list[str]) -> list[str]:
    return [word for source in sources for word in source.split()]


# --- every word once, whatever the recogniser's chunking ---------------------------------


@pytest.mark.parametrize("timed", [True, False], ids=["word-timings", "text-only"])
@pytest.mark.parametrize("latency_s", [0.2, 0.5])
@pytest.mark.parametrize("chunk_s", [1.0, 1.7, 2.5, 4.0, 30.0])
async def test_a_monologue_reaches_translation_without_repeats_or_gaps(
    chunk_s: float, latency_s: float, timed: bool
) -> None:
    """The 2 s cap cuts through the recogniser's chunks; nothing may be repeated or lost."""

    truth, source, script = lecture(
        [26], word_s=0.35, pause_s=0.0, chunk_s=chunk_s, latency_s=latency_s, timed=timed
    )

    sources = await run_pipeline(source, script)

    assert sent_words(sources) == truth


@pytest.mark.parametrize("timed", [True, False], ids=["word-timings", "text-only"])
@pytest.mark.parametrize("chunk_s", [1.5, 2.5, 4.0])
async def test_sentences_with_pauses_reach_translation_without_repeats_or_gaps(
    chunk_s: float, timed: bool
) -> None:
    truth, source, script = lecture(
        [12, 9, 14, 10, 13, 8], word_s=0.4, pause_s=0.7, chunk_s=chunk_s, latency_s=0.3, timed=timed
    )

    sources = await run_pipeline(source, script)

    assert sent_words(sources) == truth


async def test_the_recorded_failure_cannot_come_back() -> None:
    """The sentence that a real recording turned into four growing, overlapping cues."""

    sentence = "I'm also an English language examiner at the International Committee of the Red Cross."
    spoken = [(w, 0.4 + i * 0.35, 0.4 + i * 0.35 + 0.3) for i, w in enumerate(sentence.split())]
    # One chunk for the whole sentence, as the recording showed: interims grow from its
    # start while the 2 s cap cuts through it.
    script = []
    for upto in range(2, len(spoken) + 1):
        heard = spoken[:upto]
        script.append((heard[-1][2] + 0.3, message(heard, 0.4, heard[-1][2] - 0.4,
                                                   is_final=False, speech_final=False, timed=True)))
    script.append((spoken[-1][2] + 0.4, message(spoken, 0.4, spoken[-1][2] - 0.4,
                                                is_final=True, speech_final=True, timed=True)))
    script.sort(key=lambda pair: pair[0])

    sources = await run_pipeline(SpeechIntervals([(0.4, spoken[-1][2] + 0.05)], 8.0), script)

    assert " ".join(sources) == sentence
    assert len(sources) >= 2, "the cap should have cut the sentence into more than one segment"


# --- a voice the energy detector cannot hear ----------------------------------------------


@pytest.mark.parametrize("level", [-18, -34, -42, -50])
async def test_a_quiet_voice_still_gets_subtitles(level: float) -> None:
    """The VAD's threshold is about -36 dBFS; the recogniser has no such limit.

    At -42 and -50 dBFS the segmenter used to see only silence, never opened a run, and
    discarded everything the recogniser said until shutdown.
    """

    truth, source, script = lecture(
        [10, 10, 10], word_s=0.4, pause_s=0.9, chunk_s=3.0, latency_s=0.3, timed=True
    )
    source.loud = tone_frame(level)

    sources = await run_pipeline(source, script)

    assert sent_words(sources) == truth
    assert len(sources) >= 3, "sentences should still be cut at their pauses"
