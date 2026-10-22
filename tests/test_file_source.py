"""Tests for the WAV replay backend.

This backend is what makes "run the real pipeline on a real recording" possible,
and it is also the golden-set path, so it has to be exact rather than merely
plausible. The strongest test here asserts a byte-for-byte round trip: at 1:1 there
is no resampling, so any difference at all is a bug.
"""

from __future__ import annotations

import asyncio
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from livesubs.audio.file_source import WavFileSource

FRAME_SAMPLES = 320
FRAME_BYTES = FRAME_SAMPLES * 2

#: Exactly four frames' worth, so a round trip can be compared for equality. A
#: length that is not a whole number of frames is padded, which the padding test
#: covers separately.
#: The values are exactly representable as both int16 and float64, so any
#: difference is a bug rather than rounding.
STAIRCASE = np.resize(
    np.array([-32767, -16384, -1, 0, 1, 16384, 32766], dtype="<i2"), 4 * FRAME_SAMPLES
)


def write_wav(
    path: Path,
    pcm: np.ndarray,
    *,
    rate: int = 16_000,
    channels: int = 1,
) -> Path:
    """Write int16 samples as a WAV file, duplicating them across channels."""

    frames = pcm
    if channels > 1:
        frames = np.repeat(pcm[:, None], channels, axis=1).reshape(-1)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames.astype("<i2").tobytes())

    return path


async def collect(source: WavFileSource) -> bytes:
    return b"".join([frame.pcm async for frame in source.frames()])


# --- exactness ------------------------------------------------------------------


async def test_a_matching_format_round_trips_byte_for_byte(tmp_path: Path) -> None:
    """16 kHz mono in, 16 kHz mono out, with nothing touched in between.

    Any difference at all is a bug, so this is an equality rather than a tolerance.
    """

    path = write_wav(tmp_path / "tone.wav", STAIRCASE)
    source = WavFileSource(path, speed=0.0)

    assert await collect(source) == STAIRCASE.tobytes()


async def test_frames_have_the_agreed_size_and_a_sample_derived_clock(
    tmp_path: Path,
) -> None:
    path = write_wav(tmp_path / "tone.wav", STAIRCASE)
    source = WavFileSource(path, speed=0.0)

    frames = [frame async for frame in source.frames()]

    assert len(frames) == 4
    assert all(len(frame.pcm) == FRAME_BYTES for frame in frames)
    assert [frame.start_ms for frame in frames] == [0, 20, 40, 60]
    assert all(frame.duration_ms == 20 for frame in frames)


async def test_a_trailing_fragment_is_padded_not_dropped(tmp_path: Path) -> None:
    """Dropping it would lose the end of a word; silence is the honest filler."""

    pcm = np.arange(400, dtype="<i2") * 10
    path = write_wav(tmp_path / "short.wav", pcm)
    source = WavFileSource(path, speed=0.0)

    data = await collect(source)

    assert len(data) == 2 * FRAME_BYTES
    assert data[: 400 * 2] == pcm.tobytes()
    assert data[400 * 2 :] == b"\x00\x00" * (640 - 400)


# --- conversion -----------------------------------------------------------------


async def test_a_48k_stereo_file_is_downmixed_and_resampled(tmp_path: Path) -> None:
    """The realistic case: a recording is almost never 16 kHz mono."""

    samples = 48_000                                       # one second
    tone = np.sin(2 * np.pi * 440.0 * np.arange(samples) / 48_000)
    pcm = np.round(tone * 20000).astype("<i2")
    path = write_wav(tmp_path / "stereo48.wav", pcm, rate=48_000, channels=2)

    source = WavFileSource(path, speed=0.0)
    data = await collect(source)

    assert source.source_rate == 48_000
    assert source.source_channels == 2
    assert source.duration_ms == 1000

    got_samples = len(data) // 2
    assert abs(got_samples - 16_000) <= 3, f"expected about 16000 samples, got {got_samples}"

    # Level survives the conversion, so nothing is being decoded at the wrong stride.
    floats = np.frombuffer(data, dtype="<i2").astype(np.float64)
    rms = float(np.sqrt(np.mean(floats**2)))
    assert 20000 / np.sqrt(2) * 0.98 < rms < 20000 / np.sqrt(2) * 1.02


async def test_a_zero_length_file_produces_no_frames(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "empty.wav", np.empty(0, dtype="<i2"))
    source = WavFileSource(path, speed=0.0)

    assert [frame async for frame in source.frames()] == []


# --- lifecycle ------------------------------------------------------------------


async def test_alive_goes_false_once_the_file_is_exhausted(tmp_path: Path) -> None:
    """`alive` must mean "could still produce audio", so the watchdog can tell
    finished from stalled."""

    path = write_wav(tmp_path / "tone.wav", STAIRCASE)
    source = WavFileSource(path, speed=0.0)

    assert source.alive is True
    await collect(source)
    assert source.alive is False


async def test_close_stops_the_stream(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "tone.wav", STAIRCASE)
    source = WavFileSource(path, speed=0.0)

    await source.close()

    assert source.alive is False
    assert [frame async for frame in source.frames()] == []


async def test_close_is_idempotent(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "tone.wav", STAIRCASE)
    source = WavFileSource(path, speed=0.0)

    await source.close()
    await source.close()

    assert source.alive is False


async def test_looping_restarts_the_clock(tmp_path: Path) -> None:
    """`start_ms` must stay honest for the pass being played, not grow for ever.

    Two frames per pass, so the third frame is the first of the second pass and its
    clock has to be back at zero.
    """

    path = write_wav(tmp_path / "tone.wav", np.arange(2 * FRAME_SAMPLES, dtype="<i2"))
    source = WavFileSource(path, speed=0.0, loop=True)

    frames = []
    async for frame in source.frames():
        frames.append(frame)
        if len(frames) == 3:
            await source.close()

    assert [frame.start_ms for frame in frames] == [0, 20, 0]


async def test_realtime_pacing_is_optional_but_real(tmp_path: Path) -> None:
    """Real time matters for a demo; a test cannot afford it."""

    path = write_wav(tmp_path / "tone.wav", np.arange(1600, dtype="<i2"))

    fast = WavFileSource(path, speed=0.0)
    instant = await asyncio.wait_for(collect(fast), timeout=2.0)
    assert instant

    paced = WavFileSource(path, speed=1.0)
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(collect(paced), timeout=5.0)
    elapsed = asyncio.get_running_loop().time() - started

    assert 0.05 < elapsed < 2.0, f"five frames should take about 0.1 s, took {elapsed:.3f} s"


async def test_a_real_time_replay_keeps_to_the_clock_behind_a_slow_consumer(
    tmp_path: Path,
) -> None:
    """A microphone does not slow down for the pipeline, and a replay must not either.

    Pacing each frame by a fixed sleep added the consumer's own time to every frame, so a
    replay fell behind the wall clock and the latency it reported grew by about 3 s in two
    minutes of audio with no real delay behind it.
    """

    path = write_wav(tmp_path / "second.wav", np.zeros(16_000, dtype="<i2"))
    source = WavFileSource(path, speed=1.0)

    started = asyncio.get_running_loop().time()
    async for _ in source.frames():
        # Ten milliseconds of work per frame: half of each frame's real-time budget.
        time.sleep(0.010)  # noqa: ASYNC251 - blocking on purpose, as CPU work would
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.2, f"one second of audio took {elapsed:.2f} s to replay"


# --- failing fast ---------------------------------------------------------------


def test_a_missing_file_is_rejected_at_construction(tmp_path: Path) -> None:
    """Better to refuse to start than to discover it once the lecture has begun."""

    with pytest.raises(FileNotFoundError):
        WavFileSource(tmp_path / "nope.wav")


def test_a_non_16_bit_file_is_rejected(tmp_path: Path) -> None:
    """8-bit would be read at the wrong stride and decoded as noise, not as an error."""

    path = tmp_path / "eight_bit.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(16_000)
        wav.writeframes(bytes(1600))

    with pytest.raises(ValueError, match="only 16-bit PCM"):
        WavFileSource(path)


def test_a_non_wav_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "not_wav.wav"
    path.write_bytes(b"this is not a RIFF file at all")

    with pytest.raises(ValueError, match="not a readable WAV"):
        WavFileSource(path)


def test_negative_speed_is_rejected(tmp_path: Path) -> None:
    """A negative sleep silently becomes no sleep, so it would hide a typo."""

    path = write_wav(tmp_path / "tone.wav", STAIRCASE)

    with pytest.raises(ValueError, match="speed cannot be negative"):
        WavFileSource(path, speed=-1.0)
