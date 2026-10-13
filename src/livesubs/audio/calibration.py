"""Bounded microphone checks and verified, downward-only hardware calibration.

No PCM is scaled: attenuation after an ADC has clipped cannot recover speech.
Gain discovery is lazy, so healthy microphones need neither ALSA nor mixer tools.
"""

from __future__ import annotations

import logging
import math
import os
import selectors
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from livesubs.audio.hardware_gain import GainUnavailable, discover_gain_control

logger = logging.getLogger(__name__)


class CalibrationError(RuntimeError):
    """Capture is unusable or automatic correction could not be verified."""


@dataclass(frozen=True)
class CalibrationConfig:
    enabled: bool = True
    runtime: bool = True
    measure_seconds: float = 2.0
    max_adjustments: int = 4
    clipping_limit: float = 0.001
    sustained_windows: int = 2
    cooldown_seconds: float = 5.0
    max_duration_seconds: float = 25.0

    def __post_init__(self) -> None:
        if not 0.05 <= self.measure_seconds <= 5:
            raise ValueError("calibration measure_seconds must be between 0.05 and 5")
        if not 1 <= self.max_adjustments <= 4:
            raise ValueError("calibration max_adjustments must be between 1 and 4")
        if not 0 < self.clipping_limit < 1:
            raise ValueError("calibration clipping_limit must be between 0 and 1")
        if not 1 <= self.sustained_windows <= 4:
            raise ValueError("calibration sustained_windows must be between 1 and 4")
        if self.cooldown_seconds < 0 or not math.isfinite(self.cooldown_seconds):
            raise ValueError("calibration cooldown_seconds must be finite and nonnegative")
        if not 1 <= self.max_duration_seconds <= 60:
            raise ValueError("calibration max_duration_seconds must be between 1 and 60")


@dataclass(frozen=True)
class CaptureMeasurement:
    samples: int
    rms_dbfs: float
    peak_dbfs: float
    clipping_fraction: float
    dc_dbfs: float

    def clipped(self, limit: float = 0.001) -> bool:
        return self.clipping_fraction >= limit


@dataclass(frozen=True)
class CalibrationNotice:
    """A capture-health report for the operator.

    ``state`` is one of ``calibrating`` and ``adjusting`` (work in progress, so the
    session shows as delayed), ``ready`` (done), ``failed`` (startup calibration could not
    finish, and capture will not start) and ``degraded`` (runtime gain protection has given
    up, but capture continues and the operator should act by hand).
    """

    state: str
    message: str


def _dbfs(amplitude: float) -> float:
    return 20 * math.log10(amplitude / 32768) if amplitude > 0 else -120.0


def measure_pcm(pcm: bytes) -> CaptureMeasurement:
    """Measure mono s16le; clipping uses the original, not DC-centred, samples."""
    if len(pcm) % 2:
        raise CalibrationError("CAPTURE_FORMAT: incomplete signed-16-bit sample")
    if not pcm:
        return CaptureMeasurement(0, -120.0, -120.0, 0.0, -120.0)
    values = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    dc = float(values.mean())
    centred = values - dc
    return CaptureMeasurement(
        samples=int(values.size),
        rms_dbfs=_dbfs(math.sqrt(float(np.mean(centred * centred)))),
        peak_dbfs=_dbfs(float(np.abs(values).max())),
        clipping_fraction=float(np.mean(np.abs(values) >= 32760)),
        dc_dbfs=_dbfs(abs(dc)),
    )


def measure_capture(
    target: str,
    sample_rate: int = 16_000,
    seconds: float = 2.0,
    *,
    binary: str = "pw-record",
    stop_event: threading.Event | None = None,
    deadline: float | None = None,
) -> CaptureMeasurement:
    """Read an exact PCM window with a real deadline, stderr drainage, and reaping."""
    if not target or not 0 < sample_rate <= 192_000 or not 0 < seconds <= 5:
        raise ValueError("capture measurement needs a target, supported rate and duration <=5s")
    if deadline is not None and not math.isfinite(deadline):
        raise ValueError("capture measurement deadline must be finite")
    end = time.monotonic() + seconds + 2.0
    if deadline is not None:
        end = min(end, deadline)
    expected = int(sample_rate * seconds) * 2
    if expected < 2:
        raise ValueError("capture measurement is shorter than one sample")
    if stop_event is not None and stop_event.is_set():
        raise CalibrationError("CAPTURE_CALIBRATION_CANCELLED: capture check stopped")
    try:
        proc = subprocess.Popen(
            [binary, "--target", target, "--rate", str(sample_rate),
             "--channels", "1", "--format", "s16", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
    except OSError as exc:
        raise CalibrationError(f"CAPTURE_START_FAILED: cannot run {binary}: {exc}") from exc

    data = bytearray()
    diagnostics = bytearray()
    try:
        assert proc.stdout is not None and proc.stderr is not None
        with selectors.DefaultSelector() as selector:
            for pipe, label in ((proc.stdout, "audio"), (proc.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, label)
            while len(data) < expected:
                if stop_event is not None and stop_event.is_set():
                    raise CalibrationError("CAPTURE_CALIBRATION_CANCELLED: capture check stopped")
                remaining = end - time.monotonic()
                if remaining <= 0:
                    detail = diagnostics.decode("utf-8", "replace").strip()
                    raise CalibrationError(
                        f"CAPTURE_TIMEOUT: {target!r} did not deliver {seconds:g}s of audio"
                        + (f"; recorder: {detail}" if detail else "; check the microphone/PipeWire")
                    )
                for key, _ in selector.select(min(remaining, 0.1)):
                    count = min(65536, expected - len(data)) if key.data == "audio" else 4096
                    try:
                        chunk = os.read(key.fileobj.fileno(), count)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if key.data == "audio" and len(data) < expected:
                            # Drain any final diagnostic without waiting for more audio.
                            try:
                                diagnostics.extend(os.read(proc.stderr.fileno(), 4096))
                            except BlockingIOError:
                                pass
                            detail = diagnostics[-4096:].decode("utf-8", "replace").strip()
                            raise CalibrationError(
                                f"CAPTURE_NO_AUDIO: recorder ended after {len(data) // 2} samples"
                                + (f"; {detail}" if detail else "; check the selected microphone")
                            )
                    elif key.data == "audio":
                        data.extend(chunk)
                    else:
                        diagnostics.extend(chunk)
                        del diagnostics[:-4096]
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()

    if bytes(data).startswith((b"RIFF", b"RIFX", b".snd", b"FORM", b"caff", b"PIPE:")):
        raise CalibrationError(
            "CAPTURE_FORMAT: recorder stdout contains a container/header or diagnostic text; "
            "headerless mono s16le PCM is required (do not enable pw-record --verbose)"
        )
    return measure_pcm(bytes(data))


def calibrate_capture(
    target: str,
    sample_rate: int = 16_000,
    *,
    config: CalibrationConfig,
    report: Callable[[CalibrationNotice], None] | None = None,
    stop_event: threading.Event | None = None,
) -> CaptureMeasurement:
    """Check signal, reduce verified hardware gain only for sustained clipping.

    Successful settings remain in place. A failed/cancelled attempt restores its
    starting setting only when the backend can prove nobody else changed it.
    Quiet input is never boosted and is not claimed to prove speech intelligibility.
    """
    end = time.monotonic() + config.max_duration_seconds
    gain = None
    original = None
    changed = False

    def notify(state: str, message: str) -> None:
        logger.info("microphone calibration: %s", message)
        if report is not None:
            try:
                report(CalibrationNotice(state, message))
            except Exception:
                logger.exception("calibration status reporter failed")

    def sample() -> CaptureMeasurement:
        if stop_event is not None and stop_event.is_set():
            raise CalibrationError("CAPTURE_CALIBRATION_CANCELLED: calibration stopped")
        if time.monotonic() >= end:
            raise CalibrationError("CAPTURE_CALIBRATION_TIMEOUT: calibration time budget exhausted")
        measurement = measure_capture(
            target, sample_rate, config.measure_seconds, stop_event=stop_event, deadline=end
        )
        if measurement.samples == 0 or measurement.rms_dbfs <= -75:
            raise CalibrationError(
                "CAPTURE_NO_SIGNAL: microphone is digitally silent/muted; "
                "check the selected input and mute switch (gain was not increased)"
            )
        return measurement

    def ready(measurement: CaptureMeasurement) -> CaptureMeasurement:
        quiet = "; quiet input: speak/play the sample to verify speech" \
            if measurement.rms_dbfs <= -55 else ""
        notify("ready", f"checked {measurement.rms_dbfs:.1f} dBFS, "
               f"{measurement.clipping_fraction * 100:.3f}% near-full-scale samples{quiet}; "
               "gain is held steady and never increased during silence")
        return measurement

    try:
        notify("calibrating", "speak or play a short sample at normal, steady volume; "
               "checking actual microphone PCM, not speaker volume")
        current = sample()
        if not config.enabled:
            return current
        clipped_windows = 1 if current.clipped(config.clipping_limit) else 0
        while clipped_windows and clipped_windows < config.sustained_windows:
            current = sample()
            clipped_windows = clipped_windows + 1 if current.clipped(config.clipping_limit) else 0
        if not clipped_windows:
            return ready(current)

        gain = discover_gain_control(target, deadline=end, stop_event=stop_event)
        original = gain.read()
        expected_gain = original
        for _ in range(config.max_adjustments):
            if time.monotonic() >= end:
                raise CalibrationError("CAPTURE_CALIBRATION_TIMEOUT: calibration time budget exhausted")
            if stop_event is not None and stop_event.is_set():
                raise CalibrationError("CAPTURE_CALIBRATION_CANCELLED: calibration stopped")
            adjustment = gain.reduce(expected=expected_gain)
            if adjustment is None:
                raise CalibrationError(
                    "CAPTURE_GAIN_LIMIT: microphone still clips at minimum capture gain; "
                    "reduce the sound level/microphone boost or move it farther from the speakers"
                )
            changed = True
            expected_gain = adjustment.after
            notify("adjusting", f"sustained clipping: reduced {adjustment.before.describe()} "
                   f"to {adjustment.after.describe()}; verifying the captured signal")
            previous = current
            current = sample()
            quiet_windows = 1
            while current.rms_dbfs <= -60:
                if quiet_windows >= 4:
                    raise CalibrationError(
                        "CAPTURE_GAIN_UNVERIFIED: no representative speech after gain reduction; "
                        "play/speak at normal volume and retry (silence does not verify a correction)"
                    )
                notify("calibrating", "input became quiet after the adjustment; "
                       "waiting for representative speech before confirming the gain")
                current = sample()
                quiet_windows += 1
            if not current.clipped(config.clipping_limit):
                return ready(current)
            if previous.rms_dbfs - current.rms_dbfs < 1.0:
                raise CalibrationError(
                    "CAPTURE_GAIN_INEFFECTIVE: mixer value changed but clipping persisted "
                    "without a measurable signal reduction; check hardware gain/boost and routing"
                )
        raise CalibrationError(
            f"CAPTURE_CALIBRATION_LIMIT: clipping persists after {config.max_adjustments} "
            "adjustments; reduce playback/source level and retry"
        )
    except (CalibrationError, GainUnavailable, OSError, subprocess.SubprocessError) as exc:
        detail = str(exc)
        if changed and gain is not None and original is not None:
            try:
                gain.restore(original)
                detail += "; original capture gain restored"
            except (GainUnavailable, OSError, subprocess.SubprocessError) as restore_error:
                detail += f"; original gain not restored: {restore_error}"
        notify("failed", detail)
        raise CalibrationError(detail) from exc
