"""Tests for the streaming resampler.

The resampler is small and easy to get subtly wrong, and a subtle error here is
a silent one: the audio still plays, it is just wrong. So the tests pin the two
properties that matter, that a streamed conversion matches a single-pass one and
that a known waveform survives unchanged in frequency and level.
"""

from __future__ import annotations

import numpy as np
import pytest

from livesubs.audio.resample import LinearResampler


def sine(rate: int, frequency: float, seconds: float) -> np.ndarray:
    t = np.arange(int(rate * seconds), dtype=np.float64) / rate
    return np.sin(2.0 * np.pi * frequency * t)


def stream(
    signal: np.ndarray, *, src_rate: int, dst_rate: int, block: int
) -> np.ndarray:
    """Convert ``signal`` in fixed-size blocks, the way a file source would."""

    resampler = LinearResampler(src_rate=src_rate, dst_rate=dst_rate)
    out: list[np.ndarray] = []

    for start in range(0, signal.size, block):
        out.append(resampler.push(signal[start : start + block]))

    out.append(resampler.flush())
    return np.concatenate(out) if out else np.empty(0)


# --- the property that matters --------------------------------------------------


@pytest.mark.parametrize("block", [1, 2, 3, 5, 7, 64, 320, 4096])
def test_streaming_matches_a_single_pass(block: int) -> None:
    """Block size must not change the result.

    This is the test that catches phase bookkeeping errors. A wrong phase gives
    continuously plausible output that drifts, so it is invisible without a
    comparison like this one.
    """

    signal = sine(48_000, 440.0, 0.05)

    reference = stream(signal, src_rate=48_000, dst_rate=16_000, block=signal.size)
    streamed = stream(signal, src_rate=48_000, dst_rate=16_000, block=block)

    assert abs(streamed.size - reference.size) <= 1
    shared = min(streamed.size, reference.size)
    assert np.allclose(streamed[:shared], reference[:shared], atol=1e-9)


def test_a_known_sine_survives_with_its_frequency_intact() -> None:
    """48 kHz to 16 kHz is an exact 3:1 decimation, so the reference is analytic."""

    out = stream(sine(48_000, 1000.0, 0.05), src_rate=48_000, dst_rate=16_000, block=512)

    expected = sine(16_000, 1000.0, out.size / 16_000)
    assert out.size >= 790
    assert np.allclose(out[: expected.size], expected, atol=0.02)


def test_level_is_preserved() -> None:
    """Linear interpolation attenuates slightly, but it must not attenuate much."""

    source = sine(44_100, 500.0, 0.2)
    out = stream(source, src_rate=44_100, dst_rate=16_000, block=1024)

    src_rms = float(np.sqrt(np.mean(source**2)))
    out_rms = float(np.sqrt(np.mean(out**2)))

    assert abs(out_rms - src_rms) < 0.02


def test_a_non_integer_ratio_still_converts() -> None:
    """44.1 kHz to 16 kHz is 2.75625:1, which is the awkward real-world case."""

    out = stream(sine(44_100, 500.0, 0.2), src_rate=44_100, dst_rate=16_000, block=333)

    expected = int(44_100 * 0.2 * 16_000 / 44_100)
    assert abs(out.size - expected) <= 2


# --- edge cases -----------------------------------------------------------------


def test_matching_rates_are_a_passthrough() -> None:
    signal = sine(16_000, 300.0, 0.02)
    out = stream(signal, src_rate=16_000, dst_rate=16_000, block=100)

    assert out.size == signal.size
    assert np.allclose(out, signal, atol=1e-9)


def test_flush_recovers_the_final_samples() -> None:
    """Without a flush the last sample of a recording is dropped.

    Inaudible in isolation, and it is the difference between a finished word and a
    truncated one at the end of a file, so it is worth pinning exactly.
    """

    signal = sine(16_000, 300.0, 0.05)               # 800 samples, exactly
    resampler = LinearResampler(src_rate=16_000, dst_rate=16_000)

    pushed = resampler.push(signal)
    flushed = resampler.flush()

    assert pushed.size + flushed.size == signal.size
    assert np.allclose(np.concatenate((pushed, flushed)), signal, atol=1e-9)


def test_pushing_nothing_is_harmless() -> None:
    resampler = LinearResampler(src_rate=48_000, dst_rate=16_000)
    assert resampler.push(np.empty(0, dtype=np.float64)).size == 0
    assert resampler.flush().size == 0


def test_flush_is_idempotent() -> None:
    resampler = LinearResampler(src_rate=48_000, dst_rate=16_000)
    resampler.push(sine(48_000, 440.0, 0.01))

    resampler.flush()
    assert resampler.flush().size == 0


def test_zero_or_negative_rates_are_rejected() -> None:
    with pytest.raises(ValueError):
        LinearResampler(src_rate=0, dst_rate=16_000)
    with pytest.raises(ValueError):
        LinearResampler(src_rate=48_000, dst_rate=-1)
