"""Replays a WAV file as if it were live capture.

Two jobs, and they are different jobs:

* **Deterministic tests.** The same recording through the same pipeline twice gives
  the same segments, which is the only way to compare two segmentation policies or
  two ASR providers without the room changing underneath you.
* **Replaying real material.** A recording made in the actual hall is the golden set
  seed, and it is the only way to tune the VAD against room reverberation before
  standing in front of an audience.

**Any sample rate and channel count are accepted.** The capture contract wants
16 kHz mono, and neither ``ffmpeg`` nor ``sox`` is installed here, so a backend that
demanded that format would make every real recording unusable. Blocks are read,
downmixed and resampled incrementally rather than loaded whole, because a
ninety-minute 48 kHz stereo recording is about a gigabyte of PCM.

Samples are read from the file, never written to it. The only writes anywhere in
this project are the archive tee and the keystore.
"""

from __future__ import annotations

import asyncio
import wave
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import numpy as np

from livesubs.audio.base import TARGET_CHANNELS, TARGET_RATE, AudioFrame
from livesubs.audio.resample import LinearResampler

__all__ = ["WavFileSource"]

#: Source frames read per block. Roughly 170 ms at 48 kHz: enough that per-block
#: overhead is irrelevant, small enough that memory stays flat.
_READ_FRAMES = 8192

#: Only 16-bit PCM is supported. 24-bit audio is read at the wrong stride by
#: ``wave`` and would decode as loud noise rather than as an error, so it is
#: rejected loudly instead.
_SUPPORTED_WIDTH = 2


class WavFileSource:
    """An :class:`~livesubs.audio.base.AudioSource` backed by a WAV file."""

    def __init__(
        self,
        path: str | Path,
        *,
        sample_rate: int = TARGET_RATE,
        channels: int = TARGET_CHANNELS,
        frame_ms: int = 20,
        speed: float = 1.0,
        loop: bool = False,
    ) -> None:
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        #: ``1.0`` is real time and ``0.0`` runs flat out. Negative is rejected
        #: rather than treated as "flat out", because a negative sleep silently
        #: becomes no sleep and would hide a typo.
        self.speed = speed
        self.loop = loop

        if speed < 0:
            raise ValueError("speed cannot be negative; use 0 for as fast as possible")

        if not self.path.is_file():
            raise FileNotFoundError(f"no such audio file: {self.path}")

        self._frame_samples = sample_rate * frame_ms // 1000
        if self._frame_samples <= 0:
            raise ValueError(f"frame_ms={frame_ms} is too small for {sample_rate} Hz")

        self.source_rate, self.source_channels, self.source_width, total_frames = (
            self._probe()
        )
        self.source_frames = total_frames

        self._closed = False
        self._exhausted = False
        self._samples_emitted = 0

    def _probe(self) -> tuple[int, int, int, int]:
        """Read the format up front, so a bad file fails before the server starts.

        Deferring this to the first frame would mean discovering a 24-bit recording
        after the lecture has begun.
        """

        try:
            with wave.open(str(self.path), "rb") as wav:
                rate = wav.getframerate()
                channels = wav.getnchannels()
                width = wav.getsampwidth()
                total = wav.getnframes()
        except wave.Error as exc:
            raise ValueError(f"{self.path.name} is not a readable WAV file: {exc}") from exc

        if width != _SUPPORTED_WIDTH:
            raise ValueError(
                f"{self.path.name} is {width * 8}-bit, but only 16-bit PCM is "
                "supported. A wider sample would be read at the wrong stride and "
                "decoded as noise, so convert it first."
            )
        if rate <= 0:
            raise ValueError(f"{self.path.name} declares a sample rate of {rate}")

        return rate, channels, width, total

    @property
    def duration_ms(self) -> int:
        """Length of the file in audio time, ignoring ``speed``."""

        return self.source_frames * 1000 // self.source_rate

    @property
    def alive(self) -> bool:
        """Whether the source could still produce audio.

        Reaches ``False`` when the file runs out, not merely when frames stop
        arriving, so a watchdog can tell "finished" from "stalled".
        """

        return not self._closed and not self._exhausted

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """Yield frames until the file ends, or for ever if looping."""

        # Paced against a deadline counted from the first frame, not by a fixed sleep
        # after each one. A microphone delivers audio in real time whatever the pipeline
        # is doing, and the pipeline's latency is the wall clock minus the audio clock. A
        # fixed sleep added the consumer's own time to every frame, so a replay fell
        # behind the wall clock and the latency it reported grew with its length. A
        # consumer that falls behind now gets the overdue frames at once, as it would
        # from a microphone's buffer.
        clock = asyncio.get_running_loop()
        started: float | None = None
        played = 0

        while not self._closed:
            with wave.open(str(self.path), "rb") as wav:
                for pcm in self._pcm_frames(wav):
                    if self._closed:
                        return

                    if started is None:
                        started = clock.time()
                    yield AudioFrame(
                        pcm=pcm,
                        start_ms=self._samples_emitted * 1000 // self.sample_rate,
                        duration_ms=self.frame_ms,
                    )
                    self._samples_emitted += self._frame_samples
                    played += self._frame_samples

                    if self.speed > 0:
                        due = started + played / self.sample_rate / self.speed
                        await asyncio.sleep(max(0.0, due - clock.time()))
                    else:
                        await asyncio.sleep(0)

            if not self.loop or self._closed:
                self._exhausted = True
                return

            # A loop restarts the clock, so `start_ms` stays honest for the pass
            # actually being played rather than growing without bound.
            self._samples_emitted = 0

    def _pcm_frames(self, wav: wave.Wave_read) -> Iterator[bytes]:
        """Convert the open file into exactly ``frame_samples`` at a time."""

        resampler = LinearResampler(src_rate=wav.getframerate(), dst_rate=self.sample_rate)
        source_channels = wav.getnchannels()
        want = self._frame_samples
        pending = np.empty(0, dtype=np.float64)
        finished = False

        while True:
            if pending.size < want and not finished:
                raw = wav.readframes(_READ_FRAMES)
                if raw:
                    pending = np.concatenate(
                        (pending, resampler.push(_to_mono(raw, source_channels)))
                    )
                else:
                    finished = True
                    pending = np.concatenate((pending, resampler.flush()))

            if pending.size < want:
                if finished:
                    if pending.size:
                        # Zero-pad the trailing fragment rather than dropping it.
                        # It is real audio, and silence is the honest way to fill a
                        # partial frame; discarding it loses the end of a word.
                        yield _to_pcm(pending, want)
                    return
                continue
                # `continue` cannot spin here: the branch is only reachable when
                # the resampler is holding fewer samples than a frame, and the next
                # iteration either reads more or marks the source finished.

            yield _to_pcm(pending[:want], want)
            pending = pending[want:]

    async def close(self) -> None:
        """Stop producing. Safe to call twice, and safe to call mid-stream."""

        self._closed = True


def _to_mono(raw: bytes, channels: int) -> np.ndarray:
    """s16le bytes to mono float samples, averaging channels if there are several."""

    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)

    if channels <= 1:
        return samples

    usable = (samples.size // channels) * channels
    return samples[:usable].reshape(-1, channels).mean(axis=1)


def _to_pcm(values: np.ndarray, want: int) -> bytes:
    """Float samples to s16le, zero-padded to exactly ``want`` samples."""

    out = np.zeros(want, dtype=np.float64)
    out[: values.size] = values
    return np.clip(np.round(out), -32768, 32767).astype("<i2").tobytes()
