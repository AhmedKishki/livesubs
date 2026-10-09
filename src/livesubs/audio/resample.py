"""Streaming linear-interpolation resampling.

Needed because a real recording will not be 16 kHz mono. The capture contract
demands that format, and neither ``ffmpeg`` nor ``sox`` is installed here by
design, so the conversion has to happen in Python or a lecture recording is
unusable.

**Streaming, not whole-file.** A ninety-minute stereo 48 kHz recording is about a
gigabyte of PCM. Resampling it in one call would mean holding both it and the
result in memory, so the resampler instead accepts source blocks and returns
whatever output has become derivable, keeping one fractional sample of phase
between calls.

Linear interpolation is deliberately chosen over something better. This is a
convenience path for replaying recordings and for the golden set, not a production
resampler: it is transparent above roughly 4 kHz at these ratios, and its failure
mode for speech is a mild high-frequency loss rather than anything audible as
distortion. A polyphase filter would be more code and more dependencies for no
change in recognition accuracy.
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["LinearResampler"]

#: Source samples buffered before the resampler gives up and asks for more. Two
#: samples are the minimum for one interpolation; the rest bounds how much phase
#: can accumulate while the caller feeds small blocks.
_MIN_TAIL = 4


class LinearResampler:
    """Converts a stream from one sample rate to another.

    Usage::

        resampler = LinearResampler(src_rate=48_000, dst_rate=16_000)
        for block in source_blocks:
            out = resampler.push(block)
        out = resampler.flush()          # once the source is exhausted
    """

    def __init__(self, *, src_rate: int, dst_rate: int) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")

        #: Source samples advanced per output sample. Greater than one means
        #: decimation, less than one means interpolation.
        self.step = src_rate / dst_rate
        self.src_rate = src_rate
        self.dst_rate = dst_rate

        self._tail = np.empty(0, dtype=np.float64)
        #: Fractional position of the *next* output sample, relative to ``_tail[0]``.
        self._phase = 0.0

    @property
    def pending(self) -> int:
        """Source samples currently held back."""

        return int(self._tail.size)

    def push(self, samples: np.ndarray) -> np.ndarray:
        """Feed source samples; return whatever output is now derivable.

        Output is delayed by one source sample, because linear interpolation needs
        the sample *after* the one being produced. That costs 20 microseconds at
        48 kHz and is why the final call must be :meth:`flush`.
        """

        data = np.concatenate((self._tail, samples)) if self._tail.size else samples
        size = int(data.size)

        count = self._count(size)
        if count <= 0:
            self._tail = data
            return np.empty(0, dtype=np.float64)

        positions = self._phase + np.arange(count, dtype=np.float64) * self.step
        out = np.interp(positions, np.arange(size, dtype=np.float64), data)

        # The next output sample sits `step` further on. Dropping the whole samples
        # before it and carrying the fraction is what makes consecutive pushes
        # agree with a single-pass conversion.
        consumed = float(positions[-1]) + self.step
        drop = min(size, math.floor(consumed))
        self._phase = consumed - drop
        self._tail = data[drop:]

        return out

    def flush(self) -> np.ndarray:
        """Produce the last output samples, which need no further source data.

        Without this the final sample of a recording is dropped, which is the
        difference between a finished word and a truncated one at the end of a file.
        The sample *after* the last position does not exist, and ``np.interp``
        clamps it to the final available one, which is exactly the right
        substitution: hold the last known value rather than invent a slope.
        """

        size = int(self._tail.size)
        if size == 0:
            self._phase = 0.0
            return np.empty(0, dtype=np.float64)

        last = (size - 1 - self._phase) / self.step
        count = math.floor(last) + 1
        if count <= 0:
            self._tail = np.empty(0, dtype=np.float64)
            self._phase = 0.0
            return np.empty(0, dtype=np.float64)

        positions = self._phase + np.arange(count, dtype=np.float64) * self.step
        out = np.interp(positions, np.arange(size, dtype=np.float64), self._tail)

        self._tail = np.empty(0, dtype=np.float64)
        self._phase = 0.0
        return out

    def _count(self, size: int) -> int:
        """How many output samples can be produced from ``size`` source samples.

        The bound is chosen so that the sample *after* the last output position
        exists: ``positions[-1] + 1 <= size - 1``. Being one short here is
        harmless, because the next push corrects it, whereas being one long reads
        past the end of the buffer.
        """

        if size < _MIN_TAIL:
            return 0

        room = (size - 2 - self._phase) / self.step
        if room < 0:
            return 0

        return math.floor(room) + 1
