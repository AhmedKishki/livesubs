"""Tests for the transcript-fed input stage.

It is not an ASR, and the tests pin what it actually promises: cues appear when the *audio
clock* reaches them, and the event stream ends when it is closed. The clock matters because it
is what keeps the subtitles in step with the recording.
"""

from __future__ import annotations

import asyncio

import pytest

from livesubs.asr.base import AsrConfig, AsrEvent, AsrProvider
from livesubs.asr.transcript import TranscriptAsr
from livesubs.subtitles import Cue

FRAME_MS = 20
SAMPLES = 16_000 * FRAME_MS // 1000
FRAME = b"\x00" * (SAMPLES * 2)


async def _drain(adapter: TranscriptAsr) -> list[AsrEvent]:
    return [event async for event in adapter.events()]


def test_a_cue_appears_only_when_the_clock_reaches_it() -> None:
    """The timing comes from the audio, so a cue must not arrive early."""

    adapter = TranscriptAsr([Cue(1000, 2000, "later"), Cue(0, 500, "first")])

    async def run() -> list[AsrEvent]:
        await adapter.open(AsrConfig())
        events: list[AsrEvent] = []
        collector = asyncio.create_task(_drain_collect(adapter, events))

        await adapter.push(FRAME)                       # 20 ms
        assert events == []

        for _ in range(49):                             # 1000 ms in total
            await adapter.push(FRAME)
        await asyncio.sleep(0.01)

        await adapter.close()
        await asyncio.wait_for(collector, timeout=2)
        return events

    events = asyncio.run(run())

    assert [event.text for event in events] == ["first", "later"]
    assert all(event.kind == "final" for event in events)
    assert all(event.speech_final for event in events)


async def _drain_collect(adapter: TranscriptAsr, sink: list[AsrEvent]) -> None:
    async for event in adapter.events():
        sink.append(event)


def test_cues_are_emitted_in_audio_order_whatever_order_they_arrived_in() -> None:
    adapter = TranscriptAsr([Cue(400, 800, "second"), Cue(0, 400, "first")])

    assert [cue.text for cue in adapter.cues] == ["first", "second"]


def test_the_clock_advances_with_the_audio_it_is_given() -> None:
    adapter = TranscriptAsr([])

    async def run() -> int:
        await adapter.open(AsrConfig())
        for _ in range(50):
            await adapter.push(FRAME)
        return adapter.now_ms

    assert asyncio.run(run()) == 1000


def test_pushing_before_open_is_refused() -> None:
    with pytest.raises(RuntimeError):
        asyncio.run(TranscriptAsr([]).push(FRAME))


def test_the_event_stream_ends_when_the_adapter_is_closed() -> None:
    """The pipeline's collector relies on the stream ending, or shutdown hangs."""

    adapter = TranscriptAsr([])

    async def run() -> int:
        await adapter.open(AsrConfig())
        collector = asyncio.create_task(_drain(adapter))
        await adapter.close()
        return len(await asyncio.wait_for(collector, timeout=2))

    assert asyncio.run(run()) == 0


def test_an_empty_cue_is_skipped_rather_than_published_blank() -> None:
    adapter = TranscriptAsr([Cue(0, 500, "   "), Cue(500, 900, "real")])

    async def run() -> list[AsrEvent]:
        await adapter.open(AsrConfig())
        events: list[AsrEvent] = []
        collector = asyncio.create_task(_drain_collect(adapter, events))
        for _ in range(30):
            await adapter.push(FRAME)
        await adapter.close()
        await asyncio.wait_for(collector, timeout=2)
        return events

    assert [event.text for event in asyncio.run(run())] == ["real"]


def test_the_adapter_satisfies_the_asr_protocol() -> None:
    assert isinstance(TranscriptAsr([]), AsrProvider)
