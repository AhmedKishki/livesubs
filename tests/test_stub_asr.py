"""Tests for the credential-free ASR adapter.

The stub is a real adapter rather than a mock, so it has to behave like one:
partials that grow as the utterance proceeds, a final that closes it, endpointing
reported through ``speech_final``, and a flush on close so that a lecture which
stops mid-sentence does not lose its last line.
"""

from __future__ import annotations

import numpy as np
import pytest

from livesubs.asr.base import AsrConfig, AsrEvent
from livesubs.asr.stub import StubAsr
from livesubs.audio.synthetic import rms_amplitude_for_dbfs

FRAME_SAMPLES = 320
FRAME_MS = 20


def frame(dbfs: float) -> bytes:
    """One 20 ms frame of square wave at ``dbfs`` RMS."""

    level = rms_amplitude_for_dbfs(dbfs)
    values = np.resize(np.array([level, -level]), FRAME_SAMPLES)
    return np.clip(values, -32768, 32767).astype("<i2").tobytes()


async def feed(asr: StubAsr, dbfs: float, ms: int) -> None:
    for _ in range(ms // FRAME_MS):
        await asr.push(frame(dbfs))


async def drain(asr: StubAsr) -> list[AsrEvent]:
    """Collect every queued event. Only terminates after ``close``."""

    return [event async for event in asr.events()]

async def test_a_speech_burst_produces_growing_partials_then_a_final() -> None:
    phrase = "hello there world"
    asr = StubAsr(phrases=(phrase,))
    await asr.open(AsrConfig())

    await feed(asr, -20.0, 1000)          # speech
    await feed(asr, -75.0, 1000)          # a pause long enough to endpoint
    await asr.close()

    events = await drain(asr)
    assert events, "the stub produced nothing"

    partials = [event for event in events if event.kind == "partial"]
    final = events[-1]

    assert final.kind == "final"
    assert final.text == phrase
    assert final.speech_final is True

    # Partials are prefixes of the final, which is what the overlay's in-place
    # revision path is designed around.
    assert len(partials) >= 2
    assert all(phrase.startswith(event.text) for event in partials)
    assert partials[0].text != phrase


async def test_the_final_is_reported_after_the_pause_not_on_the_last_voiced_frame() -> None:
    """The endpoint belongs to the silence, so the pause must be observed first."""

    asr = StubAsr(phrases=("a sentence",))
    await asr.open(AsrConfig())

    await feed(asr, -20.0, 1000)
    await feed(asr, -75.0, 1000)
    await asr.close()

    events = await drain(asr)
    assert events[-1].start_ms < 1000 < events[-1].end_ms


async def test_close_flushes_an_open_utterance() -> None:
    """A lecture that stops mid-sentence must not lose its last line."""

    asr = StubAsr(phrases=("unfinished sentence",))
    await asr.open(AsrConfig())

    await feed(asr, -20.0, 500)           # speech, with no pause at all
    await asr.close()

    events = await drain(asr)
    assert events[-1].kind == "final"
    assert events[-1].text == "unfinished sentence"


async def test_silence_alone_produces_no_events() -> None:
    asr = StubAsr()
    await asr.open(AsrConfig())

    await feed(asr, -75.0, 2000)
    await asr.close()

    assert await drain(asr) == []


async def test_phrases_cycle_so_successive_segments_differ() -> None:
    asr = StubAsr(phrases=("first one", "second one"))
    await asr.open(AsrConfig())

    for _ in range(3):
        await feed(asr, -20.0, 600)
        await feed(asr, -75.0, 600)
    await asr.close()

    finals = [event.text for event in await drain(asr) if event.kind == "final"]
    assert finals == ["first one", "second one", "first one"]


async def test_healthy_reflects_the_open_state() -> None:
    """A real adapter's health reflects the connection, so this must too."""

    asr = StubAsr()
    assert asr.healthy is False

    await asr.open(AsrConfig())
    assert asr.healthy is True

    await asr.close()
    assert asr.healthy is False


async def test_pushing_before_open_is_an_error() -> None:
    asr = StubAsr()
    with pytest.raises(RuntimeError):
        await asr.push(frame(-20.0))


async def test_close_is_idempotent() -> None:
    """The pipeline closes the provider on a path that may already have closed it."""

    asr = StubAsr(phrases=("once",))
    await asr.open(AsrConfig())
    await feed(asr, -20.0, 200)

    await asr.close()
    await asr.close()

    finals = [event.text for event in await drain(asr) if event.kind == "final"]
    assert finals == ["once"]
