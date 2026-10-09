"""Audio conditioning and voice-activity detection.

Sits between a capture backend and the ASR provider. Its jobs:

* **DC offset removal**, so the noise-floor estimate does not drift.
* **An adaptive noise floor.** HVAC, projector fans and traffic are constant, so
  a fixed threshold will not survive a change of room. The floor tracks a rolling
  minimum and is deliberately allowed to fall fast and rise slowly.
* **Voice activity detection** as a margin above that floor.
* **Metering and clipping detection**, so the operator can see at a glance
  whether capture is working without reading logs. Hardware gain calibration belongs
  to the capture backend; this layer never scales already-clipped PCM.

It also *is the pipeline's clock*. Frame arrival drives the segment cap and the
silence detector, so the clock is derived from samples consumed rather than from
wall time. That keeps behaviour identical whether audio arrives from a live device
in real time or from a file replayed faster, which is what makes the audio path
testable at all.

Nothing here performs I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "FRAME_MS",
    "FrameStats",
    "Ingest",
    "IngestConfig",
]

#: Frame size handed downstream. 20 ms at 16 kHz is 320 samples: short enough to
#: behave like a clock, long enough that per-frame overhead is irrelevant.
FRAME_MS = 20

_INT16_SCALE = 32768.0
#: Anything at or above this counts as clipped. One LSB of headroom, because a
#: signal that sits exactly at full scale is already distorting.
_CLIP_LEVEL = 32760


@dataclass(slots=True)
class IngestConfig:
    """Tunable conditioning. Defaults suit a quiet lecture hall."""

    sample_rate: int = 16_000
    #: How fast the noise floor is allowed to *rise* once warmed up. Slow: a long
    #: pause in a quiet room must not let the floor climb into speech.
    floor_rise_rate: float = 0.002
    #: Length of the warm-up, during which the floor follows the signal up quickly.
    #: A room louder than the initial guess needs to be characterised in a fraction
    #: of a second, not after ten seconds of phantom speech.
    warmup_ms: int = 500
    #: Rise rate during warm-up.
    warmup_rise_rate: float = 0.25
    #: dB above the floor that counts as speech.
    speech_margin_db: float = 9.0
    #: Absolute level below which input is never speech, whatever the floor says.
    #:
    #: Without this, a very quiet but non-zero input lets the floor settle onto the
    #: noise itself, and the relative margin then measures noise *fluctuation* rather
    #: than speech: a room with nothing happening in it reads as continuous talking,
    #: which on the live pipeline is an endless bill as well as endless subtitles.
    #:
    #: -60 dBFS is deliberately just below the "no signal worth trying" level the
    #: pre-flight check uses, so a signal quiet enough to fail this was already being
    #: reported as unusable. ``tests/test_ingest.py`` pins both directions: a very
    #: quiet room is never speech, and a quiet but genuine voice still is.
    min_speech_dbfs: float = -60.0
    #: The floor is never allowed above this, so a very quiet recording cannot
    #: make ambient noise look like speech.
    max_floor_dbfs: float = -45.0
    #: Start optimistic: assume a quiet room until told otherwise.
    initial_floor_dbfs: float = -70.0


@dataclass(slots=True)
class FrameStats:
    """What one frame told us. All levels are dBFS, so 0.0 is full scale."""

    now_ms: int
    """Audio-derived time, not wall time. This is the pipeline's clock."""

    rms_dbfs: float
    peak_dbfs: float
    noise_floor_dbfs: float
    speech: bool
    clipping_fraction: float
    #: Consecutive frames with no speech. Drives the "is the mic dead?" alert.
    silence_streak: int

    @property
    def silent(self) -> bool:
        return not self.speech

    @property
    def clipping(self) -> bool:
        """Any sustained clipping, not one stray sample."""
        return self.clipping_fraction >= 0.001


def _to_dbfs(amplitude: float) -> float:
    """Linear amplitude to dBFS, floored to avoid log(0)."""

    if amplitude <= 0.0:
        return -120.0
    return 20.0 * float(np.log10(amplitude / _INT16_SCALE))


class Ingest:
    """Stateful conditioning. Feed it PCM, read back levels and a VAD decision.

    Single-threaded by design: one producer feeds it, so no locking is needed and
    the noise floor cannot be observed mid-update.
    """

    def __init__(self, config: IngestConfig | None = None) -> None:
        self.config = config or IngestConfig()
        self._floor = self.config.initial_floor_dbfs
        self._samples_seen = 0
        self._silence_streak = 0
        self._warmup_samples = self.config.sample_rate * self.config.warmup_ms // 1000

    def process(self, pcm: bytes) -> FrameStats:
        """Condition one frame and decide whether it carries speech."""

        samples = _as_float(pcm)

        if samples.size == 0:
            return FrameStats(
                now_ms=self.total_ms,
                rms_dbfs=-120.0,
                peak_dbfs=-120.0,
                noise_floor_dbfs=self._floor,
                speech=False,
                clipping_fraction=0.0,
                silence_streak=self._silence_streak + 1,
            )

        # DC offset removal, once per frame. Subtracting each frame's own mean
        # removes a static device bias within 20 ms rather than over ~20 s, which a
        # slow running average would have taken. It costs nothing: a 220 Hz tone
        # completes four full cycles inside a frame, so its mean is already near
        # zero and no part of the signal leaves with the offset.
        centred = samples - float(samples.mean())

        peak = float(np.abs(centred).max())
        rms = float(np.sqrt(np.mean(np.square(centred))))
        clipped = float(np.count_nonzero(np.abs(samples) >= _CLIP_LEVEL)) / samples.size

        rms_dbfs = _to_dbfs(rms)

        self._samples_seen += int(samples.size)
        self._update_floor(rms_dbfs)

        # Two independent gates, and both must be satisfied. The relative one separates
        # the speaker from the room; the absolute one stops a very quiet room from being
        # its own worst enemy when the floor has settled onto the noise.
        threshold = max(
            self._floor + self.config.speech_margin_db, self.config.min_speech_dbfs
        )
        speech = rms_dbfs >= threshold
        self._silence_streak = 0 if speech else self._silence_streak + 1

        return FrameStats(
            now_ms=self.total_ms,
            rms_dbfs=rms_dbfs,
            peak_dbfs=_to_dbfs(peak),
            noise_floor_dbfs=self._floor,
            speech=speech,
            clipping_fraction=clipped,
            silence_streak=self._silence_streak,
        )

    def _update_floor(self, rms_dbfs: float) -> None:
        """Fall fast, rise slowly, never above the cap.

        The asymmetry is the whole point: a quiet moment is real information about
        the room, whereas a loud moment is usually speech and must not be learned
        as the floor.
        """

        if rms_dbfs < self._floor:
            self._floor = rms_dbfs
        elif self._samples_seen < self._warmup_samples:
            # During warm-up, follow the signal up quickly. Otherwise a room louder
            # than the initial guess would spend ten seconds reading as speech
            # while the floor crept toward it, and every segment in that window
            # would be spurious.
            self._floor += self.config.warmup_rise_rate * (rms_dbfs - self._floor)
        else:
            self._floor += self.config.floor_rise_rate * (rms_dbfs - self._floor)

        self._floor = min(self._floor, self.config.max_floor_dbfs)

    @property
    def noise_floor_dbfs(self) -> float:
        return self._floor

    @property
    def total_ms(self) -> int:
        """Audio-derived elapsed time. The pipeline's clock."""

        return self._samples_seen * 1000 // self.config.sample_rate

    @property
    def total_samples(self) -> int:
        return self._samples_seen

    def reset(self) -> None:
        """Forget everything. For a new session, or between tests."""

        self._floor = self.config.initial_floor_dbfs
        self._samples_seen = 0
        self._silence_streak = 0


def _as_float(pcm: bytes) -> np.ndarray:
    """s16le bytes to float samples, tolerating an odd trailing byte.

    A truncated read is normal at the end of a device stream, so it must not
    raise; dropping the final byte is the right response.
    """

    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return np.empty(0, dtype=np.float64)
    return np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float64)
