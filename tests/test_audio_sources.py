"""Tests for the synthetic audio sources.

These sources are what make the audio path testable and demonstrable without a
microphone, so they have to be exact: a source with the wrong frame size, the
wrong clock or the wrong level would make the ingest tests pass for the wrong
reason.
"""

from __future__ import annotations

import numpy as np

from livesubs.audio.synthetic import (
    SilenceSource,
    SpeechLikeSource,
    ToneSource,
)

SR = 16_000
FRAME_BYTES = 640          # 20 ms of 16 kHz mono s16le


def dbfs_of(pcm: bytes) -> float:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    rms = float(np.sqrt(np.mean(np.square(samples))))
    return 20.0 * float(np.log10(rms / 32768.0)) if rms > 0 else -120.0


async def test_frames_have_the_agreed_size_and_a_sample_derived_clock() -> None:
    source = SilenceSource(realtime=False, duration_ms=100)
    frames = [frame async for frame in source.frames()]

    assert len(frames) == 5
    assert [frame.start_ms for frame in frames] == [0, 20, 40, 60, 80]
    assert all(len(frame.pcm) == FRAME_BYTES for frame in frames)
    assert all(frame.duration_ms == 20 for frame in frames)


async def test_silence_is_digital_silence() -> None:
    """Distinct from a quiet room on purpose: a watchdog must treat it as a fault."""

    source = SilenceSource(realtime=False, duration_ms=20)
    frame = await anext(source.frames())
    assert frame.pcm == b"\x00\x00" * 320


async def test_close_stops_the_stream() -> None:
    source = SilenceSource(realtime=False)

    assert source.alive is True
    await source.close()
    assert source.alive is False
    assert [frame async for frame in source.frames()] == []


async def test_tone_reaches_the_level_it_was_asked_for() -> None:
    source = ToneSource(dbfs=-20.0, realtime=False, duration_ms=20)
    frame = await anext(source.frames())

    assert abs(dbfs_of(frame.pcm) - (-20.0)) < 0.5
async def test_speech_like_source_alternates_loud_and_quiet() -> None:
    """A steady tone never ends a segment, so this is the fixture that matters."""

    source = SpeechLikeSource(
        speech_ms=100,
        pause_ms=100,
        speech_dbfs=-20.0,
        pause_dbfs=-75.0,
        realtime=False,
        duration_ms=200,
    )
    frames = [frame async for frame in source.frames()]

    assert len(frames) == 10
    assert source.cycle_ms == 200

    loud = [dbfs_of(frame.pcm) for frame in frames[:5]]
    quiet = [dbfs_of(frame.pcm) for frame in frames[5:]]

    assert all(-21.0 < level < -19.0 for level in loud)
    assert all(level < -70.0 for level in quiet)


async def test_a_pause_is_not_digital_silence() -> None:
    """A real room is never truly silent.

    Testing against true zeros would hide a noise floor that had stopped adapting,
    because the only signal that exposes it is a *quiet* one.
    """

    source = SpeechLikeSource(
        speech_ms=100,
        pause_ms=100,
        pause_dbfs=-75.0,
        realtime=False,
        duration_ms=200,
    )
    frames = [frame async for frame in source.frames()]
    pause = frames[6]

    assert pause.pcm != b"\x00\x00" * 320
    assert -80.0 < dbfs_of(pause.pcm) < -70.0


async def test_realtime_pacing_is_optional() -> None:
    """Real-time matters for a demo; a test cannot afford it."""

    fast = SilenceSource(realtime=False, duration_ms=2000)
    assert len([frame async for frame in fast.frames()]) == 100
