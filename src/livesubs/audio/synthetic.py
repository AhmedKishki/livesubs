"""Synthetic audio sources.

Not throwaway test doubles. The watchdog's job is to tell a **dead microphone**
from a **quiet room**, and those two cases can only be tested against a source
that is deliberately one or the other. They are also how the ingest layer is
exercised without a device.

Both implement :class:`~livesubs.audio.base.AudioSource` and emit 16 kHz mono
s16le frames, with ``start_ms`` derived from samples emitted rather than from wall
time, so a fast replay behaves exactly like a real-time one.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator

from livesubs.audio.base import TARGET_CHANNELS, TARGET_RATE, AudioFrame

__all__ = [
    "SilenceSource",
    "SpeechLikeSource",
    "ToneSource",
    "rms_amplitude_for_dbfs",
    "sine_peak_for_dbfs",
]


def rms_amplitude_for_dbfs(dbfs: float) -> float:
    """Linear RMS amplitude, in the s16 range, for a level in dBFS.

    For a square wave the RMS *is* the amplitude, so this is the whole conversion.
    For a sine, use :func:`sine_peak_for_dbfs` instead.
    """

    return (10.0 ** (dbfs / 20.0)) * 32768.0


def sine_peak_for_dbfs(dbfs: float) -> float:
    """Sine PEAK amplitude for a signal whose RMS measures ``dbfs``.

    A sine's RMS is 1/sqrt(2) of its peak, so the factor is baked in here rather
    than left for each caller to remember. Getting this wrong is not cosmetic: a
    signal 3 dB quieter than requested would quietly invalidate every threshold in
    the ingest tests and make a broken VAD look correct.
    """

    return rms_amplitude_for_dbfs(dbfs) * math.sqrt(2.0)


def _tone_pcm(
    *,
    offset_samples: int,
    count: int,
    sample_rate: int,
    dbfs: float,
    frequency: float,
) -> bytes:
    """A sine whose RMS level is ``dbfs``, clipped to the s16 range.

    Clipping rather than wrapping matters: a level above 0 dBFS must sound like
    distortion, because that is what the ingest layer is there to detect.
    """

    amplitude = sine_peak_for_dbfs(dbfs)
    samples = bytearray()

    for n in range(count):
        t = (offset_samples + n) / sample_rate
        value = int(amplitude * math.sin(2.0 * math.pi * frequency * t))
        samples += max(-32768, min(32767, value)).to_bytes(2, "little", signed=True)

    return bytes(samples)


class _SyntheticSource:
    """Shared frame pacing, level generation and shutdown."""

    def __init__(
        self,
        *,
        sample_rate: int = TARGET_RATE,
        channels: int = TARGET_CHANNELS,
        frame_ms: int = 20,
        duration_ms: int | None = None,
        realtime: bool = True,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.duration_ms = duration_ms
        #: ``False`` runs flat out, for tests and for file replay at speed.
        self.realtime = realtime

        self._frame_samples = sample_rate * frame_ms // 1000
        self._samples_emitted = 0
        self._closed = False
        self._exhausted = False

    @property
    def alive(self) -> bool:
        """Whether the source could still produce audio.

        A finite source reaches ``False`` when its duration runs out, not merely when
        frames stop arriving, so a watchdog can tell "finished" from "stalled". The
        file backend does the same, and the two must agree or the contract is only
        true for some backends.
        """

        return not self._closed and not self._exhausted

    def _pcm_for(self, index: int) -> bytes:
        raise NotImplementedError

    async def frames(self) -> AsyncIterator[AudioFrame]:
        interval = self.frame_ms / 1000.0 if self.realtime else 0.0
        index = 0

        while not self._closed:
            start_ms = self._samples_emitted * 1000 // self.sample_rate

            if self.duration_ms is not None and start_ms >= self.duration_ms:
                self._exhausted = True
                return

            yield AudioFrame(
                pcm=self._pcm_for(index),
                start_ms=start_ms,
                duration_ms=self.frame_ms,
            )

            self._samples_emitted += self._frame_samples
            index += 1

            if interval:
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(0)

    async def close(self) -> None:
        self._closed = True


class SilenceSource(_SyntheticSource):
    """Digital silence: every sample zero.

    Models a muted or dead device. A watchdog must treat this as an error, which
    is why it is distinct from a *quiet* source rather than just a small tone.
    """

    def _pcm_for(self, index: int) -> bytes:
        return b"\x00\x00" * self._frame_samples


class ToneSource(_SyntheticSource):
    """A steady tone at a given level.

    Models a room with constant noise: not speech, but definitely not silence.
    That is exactly the case the noise floor has to learn, and the case a naive
    "is the level zero?" check would get wrong.
    """

    def __init__(
        self,
        *,
        dbfs: float = -60.0,
        frequency: float = 220.0,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.dbfs = dbfs
        self.frequency = frequency

    def _pcm_for(self, index: int) -> bytes:
        return _tone_pcm(
            offset_samples=index * self._frame_samples,
            count=self._frame_samples,
            sample_rate=self.sample_rate,
            dbfs=self.dbfs,
            frequency=self.frequency,
        )


class SpeechLikeSource(_SyntheticSource):
    """Alternates tone bursts with near-silence.

    Models a speaker who pauses, which is the only input that exercises the
    segmenter, the provider's endpointing or the pacing policy at all: a steady
    tone never ends a segment, so a test built on one would keep passing while the
    segmentation was completely broken.

    This is also what makes the live pipeline demonstrable with no microphone and
    no credentials. The audio path is real; only the words are missing.
    """

    def __init__(
        self,
        *,
        speech_ms: int = 1500,
        pause_ms: int = 700,
        speech_dbfs: float = -20.0,
        pause_dbfs: float = -75.0,
        frequency: float = 180.0,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.speech_ms = speech_ms
        self.pause_ms = pause_ms
        self.speech_dbfs = speech_dbfs
        self.pause_dbfs = pause_dbfs
        self.frequency = frequency

    @property
    def cycle_ms(self) -> int:
        return self.speech_ms + self.pause_ms

    def _pcm_for(self, index: int) -> bytes:
        # A pause is deliberately NOT digital silence. A real room is never that
        # quiet, and testing against true zeros would hide a noise floor that had
        # stopped adapting.
        phase = (index * self.frame_ms) % self.cycle_ms
        speaking = phase < self.speech_ms

        return _tone_pcm(
            offset_samples=index * self._frame_samples,
            count=self._frame_samples,
            sample_rate=self.sample_rate,
            dbfs=self.speech_dbfs if speaking else self.pause_dbfs,
            frequency=self.frequency,
        )
