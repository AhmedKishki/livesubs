"""Calibration uses deterministic PCM/fake mixers, never the machine's microphone."""

from __future__ import annotations

import math
import sys
import threading
import time
from collections import deque
from types import SimpleNamespace

import numpy as np
import pytest

from livesubs.audio import calibration
from livesubs.audio.calibration import (
    CalibrationConfig,
    CalibrationError,
    CaptureMeasurement,
    calibrate_capture,
    measure_capture,
    measure_pcm,
)
from livesubs.audio.hardware_gain import GainAdjustment, GainSetting, GainUnavailable


def reading(rms=-10.0, clipping=0.0):
    return CaptureMeasurement(32000, rms, -0.1, clipping, -100.0)


class Mixer:
    def __init__(self):
        self.current = GainSetting((47, 47), 0, 63, "selected microphone Capture")
        self.reductions = 0
        self.restored = []

    def read(self):
        return self.current

    def reduce(self, *, expected=None):
        before = self.current
        if expected is not None and expected != before:
            raise GainUnavailable("external gain change before reduction")
        self.current = GainSetting(
            tuple(max(0, value - 8) for value in before.values), 0, 63, before.label
        )
        self.reductions += 1
        return GainAdjustment(before, self.current)

    def restore(self, setting):
        self.restored.append(setting)
        self.current = setting


def stage(monkeypatch, readings):
    measurements = deque(readings)
    mixer = Mixer()
    targets = []

    def sample(target, rate, seconds, **kwargs):
        assert target == "the-physical-microphone"
        assert rate == 16000
        assert kwargs["deadline"] > 0
        return measurements.popleft()

    def discover(target, **kwargs):
        targets.append(target)
        return mixer

    monkeypatch.setattr(calibration, "measure_capture", sample)
    monkeypatch.setattr(calibration, "discover_gain_control", discover)
    return mixer, targets, measurements


def run(config=None, **kwargs):
    return calibrate_capture(
        "the-physical-microphone", config=config or CalibrationConfig(), **kwargs
    )


@pytest.mark.parametrize("pcm", [b"", bytes(640), np.full(320, 12000, dtype="<i2").tobytes()])
def test_empty_silent_and_dc_only_audio_are_not_speech(pcm):
    assert measure_pcm(pcm).rms_dbfs == -120


def test_signal_rms_and_original_rails_are_separate():
    pcm = np.array([32767, 32767, 0, 0], dtype="<i2").tobytes()
    measured = measure_pcm(pcm)
    assert measured.clipping_fraction == 0.5
    assert measured.rms_dbfs == pytest.approx(20 * math.log10(16383.5 / 32768))
    assert measured.dc_dbfs > -7
    assert measured.peak_dbfs > -0.001


def test_negative_full_scale_is_counted_without_integer_overflow():
    measured = measure_pcm(np.array([-32768, 32767], dtype="<i2").tobytes())
    assert measured.clipping_fraction == 1
    assert measured.peak_dbfs == 0


def test_an_incomplete_sample_is_a_format_error():
    with pytest.raises(CalibrationError, match="CAPTURE_FORMAT"):
        measure_pcm(b"x")


@pytest.mark.parametrize("kwargs", [
    {"measure_seconds": 0}, {"measure_seconds": float("nan")},
    {"max_adjustments": 0}, {"max_adjustments": 9}, {"clipping_limit": 0},
    {"sustained_windows": 0}, {"cooldown_seconds": -1},
    {"cooldown_seconds": float("inf")}, {"max_duration_seconds": 0},
])
def test_calibration_configuration_is_bounded(kwargs):
    with pytest.raises(ValueError):
        CalibrationConfig(**kwargs)


def test_healthy_input_needs_no_hardware_tools(monkeypatch):
    mixer, targets, _ = stage(monkeypatch, [reading()])
    notices = []
    assert run(report=notices.append).clipping_fraction == 0
    assert targets == [] and mixer.reductions == 0
    assert [n.state for n in notices] == ["calibrating", "ready"]


def test_quiet_input_is_not_boosted_or_claimed_to_be_tested_speech(monkeypatch):
    mixer, targets, _ = stage(monkeypatch, [reading(-60)])
    notices = []
    run(report=notices.append)
    assert mixer.reductions == 0 and targets == []
    assert "quiet" in notices[-1].message and "verify speech" in notices[-1].message


@pytest.mark.parametrize("rms", [-75, -120])
def test_digital_silence_fails_without_blind_gain_changes(monkeypatch, rms):
    mixer, targets, _ = stage(monkeypatch, [reading(rms)])
    with pytest.raises(CalibrationError, match="CAPTURE_NO_SIGNAL"):
        run()
    assert mixer.reductions == 0 and targets == []


def test_one_clipped_window_is_not_sustained(monkeypatch):
    mixer, targets, _ = stage(monkeypatch, [reading(-3, .1), reading()])
    run()
    assert mixer.reductions == 0 and targets == []


def test_real_signal_improvement_is_required_after_hardware_change(monkeypatch):
    mixer, targets, _ = stage(monkeypatch, [reading(-3, .1)] * 2 + [reading(-15)])
    notices = []
    run(report=notices.append)
    assert targets == ["the-physical-microphone"]
    assert mixer.current.values == (39, 39)
    assert not mixer.restored
    assert [n.state for n in notices] == ["calibrating", "adjusting", "ready"]


def test_effective_but_still_clipped_audio_keeps_calibrating(monkeypatch):
    mixer, _, _ = stage(monkeypatch, [reading(-3, .1)] * 2 + [reading(-9, .03), reading(-15)])
    notices = []
    run(report=notices.append)
    assert mixer.reductions == 2
    assert [n.state for n in notices].count("ready") == 1
    assert notices[-1].state == "ready"


def test_ineffective_change_restores_and_fails_instead_of_looping(monkeypatch):
    mixer, _, _ = stage(monkeypatch, [reading(-3, .1)] * 3)
    with pytest.raises(CalibrationError, match="CAPTURE_GAIN_INEFFECTIVE"):
        run()
    assert mixer.reductions == 1
    assert mixer.current.values == (47, 47)


def test_silence_after_reduction_does_not_verify_correction(monkeypatch):
    mixer, _, _ = stage(monkeypatch, [reading(-3, .1)] * 2 + [reading(-120)])
    with pytest.raises(CalibrationError, match="CAPTURE_NO_SIGNAL"):
        run()
    assert mixer.current.values == (47, 47)


def test_attempt_budget_is_finite_and_restores_original_gain(monkeypatch):
    mixer, _, _ = stage(monkeypatch, [reading(-3, .1)] * 2 + [reading(-9, .03)])
    with pytest.raises(CalibrationError, match="CAPTURE_CALIBRATION_LIMIT"):
        run(CalibrationConfig(max_adjustments=1))
    assert mixer.reductions == 1 and mixer.current.values == (47, 47)


def test_floor_is_an_actionable_failure(monkeypatch):
    mixer, _, _ = stage(monkeypatch, [reading(-3, .1)] * 2)
    monkeypatch.setattr(mixer, "reduce", lambda **kwargs: None)
    with pytest.raises(CalibrationError, match="CAPTURE_GAIN_LIMIT"):
        run()


def test_unsupported_hardware_never_falls_back_to_digital_volume(monkeypatch):
    stage(monkeypatch, [reading(-3, .1)] * 2)

    def unavailable(target, **kwargs):
        raise GainUnavailable("No standard Capture control; adjust input manually")

    monkeypatch.setattr(calibration, "discover_gain_control", unavailable)
    with pytest.raises(CalibrationError, match="adjust input manually"):
        run()


def test_manual_external_change_is_not_overwritten_during_restore(monkeypatch):
    mixer, _, _ = stage(monkeypatch, [reading(-3, .1)] * 3)

    def refuse(setting):
        raise GainUnavailable("external change; refusing overwrite")

    monkeypatch.setattr(mixer, "restore", refuse)
    with pytest.raises(CalibrationError, match="original gain not restored.*external change"):
        run()


def test_cancellation_after_adjustment_restores_and_stops(monkeypatch):
    mixer, _, remaining = stage(monkeypatch, [reading(-3, .1)] * 2 + [reading(-15)])
    stop = threading.Event()
    reduce = mixer.reduce

    def cancel_after_change(**kwargs):
        result = reduce(**kwargs)
        stop.set()
        return result

    monkeypatch.setattr(mixer, "reduce", cancel_after_change)
    with pytest.raises(CalibrationError, match="CANCELLED"):
        run(stop_event=stop)
    assert len(remaining) == 1 and mixer.current.values == (47, 47)


def test_disabled_calibration_measures_but_never_changes_gain(monkeypatch):
    mixer, targets, _ = stage(monkeypatch, [reading(-3, .1)])
    assert run(CalibrationConfig(enabled=False)).clipped()
    assert not targets and mixer.reductions == 0


def test_quiet_audio_after_mutation_never_announces_verified_ready(monkeypatch):
    mixer, _, _ = stage(monkeypatch, [reading(-3, .1)] * 2 + [reading(-70)] * 4)
    notices = []
    with pytest.raises(CalibrationError, match="CAPTURE_GAIN_UNVERIFIED"):
        run(report=notices.append)
    assert not any(n.state == "ready" for n in notices)
    assert mixer.current.values == (47, 47)


def test_waits_for_representative_audio_instead_of_confirming_quiet(monkeypatch):
    mixer, _, _ = stage(monkeypatch, [reading(-3, .1)] * 2 + [reading(-70), reading(-15)])
    notices = []
    assert run(report=notices.append).rms_dbfs == -15
    assert notices[-1].state == "ready" and mixer.reductions == 1


def test_external_change_before_first_adjustment_is_not_mutated_or_restored(monkeypatch):
    mixer, _, _ = stage(monkeypatch, [reading(-3, .1)] * 2)
    reduce = mixer.reduce

    def external_change(**kwargs):
        mixer.current = GainSetting((50, 50), 0, 63, mixer.current.label)
        return reduce(**kwargs)

    monkeypatch.setattr(mixer, "reduce", external_change)
    with pytest.raises(CalibrationError, match="external gain change"):
        run()
    assert mixer.reductions == 0 and mixer.current.values == (50, 50)
    assert mixer.restored == []


def test_reporter_failure_does_not_interrupt_safe_calibration(monkeypatch):
    mixer, _, _ = stage(monkeypatch, [reading(-3, .1)] * 2 + [reading(-15)])

    def broken(notice):
        raise RuntimeError("display disconnected")

    assert run(report=broken).clipping_fraction == 0
    assert mixer.reductions == 1


def fake_binary(tmp_path, body):
    path = tmp_path / "fake-recorder"
    path.write_text(f"#!{sys.executable}\n{body}\n")
    path.chmod(0o755)
    return str(path)


def test_exact_silent_pcm_window_is_measured_honestly(tmp_path):
    binary = fake_binary(tmp_path, "import sys; sys.stdout.buffer.write(bytes(1600))")
    measurement = measure_capture("fake", seconds=.05, binary=binary)
    assert measurement.samples == 800
    assert measurement.rms_dbfs == -120


def test_recorder_stall_has_a_real_deadline_and_is_reaped(tmp_path, monkeypatch):
    binary = fake_binary(tmp_path, "import time; time.sleep(60)")
    processes = []
    popen = calibration.subprocess.Popen

    def spawn(*args, **kwargs):
        proc = popen(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(calibration.subprocess, "Popen", spawn)
    started = time.monotonic()
    with pytest.raises(CalibrationError, match="CAPTURE_TIMEOUT"):
        measure_capture("fake", seconds=.05, binary=binary, deadline=started + .1)
    assert time.monotonic() - started < 2
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_stderr_is_drained_while_recording(tmp_path):
    binary = fake_binary(
        tmp_path,
        "import sys; sys.stderr.buffer.write(b'x'*200000); "
        "sys.stderr.flush(); sys.stdout.buffer.write(bytes(1600))",
    )
    assert measure_capture("fake", seconds=.05, binary=binary).samples == 800


def test_short_or_failed_capture_reports_recorder_reason(tmp_path):
    binary = fake_binary(
        tmp_path, "import sys; sys.stderr.write('device unplugged'); "
        "sys.stdout.buffer.write(bytes(100)); sys.exit(1)",
    )
    with pytest.raises(CalibrationError, match="CAPTURE_NO_AUDIO.*device unplugged"):
        measure_capture("fake", seconds=.05, binary=binary)


@pytest.mark.parametrize("header", [b"RIFF", b".snd", b"FORM", b"caff", b"PIPE:"])
def test_headers_and_verbose_stdout_are_not_mistaken_for_pcm(tmp_path, header):
    binary = fake_binary(
        tmp_path, f"import sys; sys.stdout.buffer.write({header!r} + bytes({1600-len(header)}))",
    )
    with pytest.raises(CalibrationError, match="CAPTURE_FORMAT"):
        measure_capture("fake", seconds=.05, binary=binary)


def test_cancelled_check_never_starts_a_recorder(monkeypatch):
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(calibration.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(CalibrationError, match="CANCELLED"):
        measure_capture("fake", stop_event=stop)


def test_startup_budget_expires_without_another_capture_or_gain_change(monkeypatch):
    mixer, targets, remaining = stage(monkeypatch, [reading(-3, .1)] * 2)
    ticks = iter([0.0, 26.0])
    monkeypatch.setattr(calibration, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(CalibrationError, match="CAPTURE_CALIBRATION_TIMEOUT"):
        run()
    assert len(remaining) == 2 and not targets and mixer.reductions == 0


@pytest.mark.parametrize("kwargs", [
    {"seconds": 60}, {"seconds": float("nan")}, {"deadline": float("inf")},
    {"sample_rate": 0}, {"sample_rate": 1_000_000},
])
def test_sampler_rejects_unbounded_parameters_before_spawn(monkeypatch, kwargs):
    monkeypatch.setattr(calibration.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(ValueError):
        measure_capture("fake", **kwargs)
