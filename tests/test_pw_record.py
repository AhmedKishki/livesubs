"""Tests for the PipeWire capture backend.

The device is faked, because a test suite that needs a working microphone is a test
suite that fails on a laptop with the lid closed. What is tested here is everything
around the device: framing, end-of-stream, process death, shutdown and the
diagnostics an operator needs when capture fails. That is where the bugs are.

The real `pw-record` is exercised separately; see the note at the end of this file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from livesubs.audio import pw_record
from livesubs.audio.base import AudioFrame
from livesubs.audio.calibration import (
    CalibrationConfig,
    CalibrationError,
    CalibrationNotice,
    CaptureMeasurement,
)
from livesubs.audio.pw_record import PwRecordSource, pipewire_sources

FRAME_BYTES = 640


def fake_recorder(tmp_path: Path, script: str) -> str:
    """Write an executable stand-in for ``pw-record``."""

    path = tmp_path / "fake-pw-record"
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(0o755)
    return str(path)


def emitting(tmp_path: Path, payload: bytes) -> PwRecordSource:
    """A source whose "device" emits exactly ``payload`` and then stops."""

    raw = tmp_path / "payload.raw"
    raw.write_bytes(payload)
    binary = fake_recorder(tmp_path, f"cat '{raw}'")

    return PwRecordSource("fake", binary=binary, validate_target=False)


async def drain(source: PwRecordSource) -> list[AudioFrame]:
    return [frame async for frame in source.frames()]


async def collect(source: PwRecordSource, *, timeout: float = 5.0) -> list[AudioFrame]:
    """Drain with a timeout, so a hang fails one test instead of the whole suite."""

    return await asyncio.wait_for(drain(source), timeout=timeout)


# --- framing --------------------------------------------------------------------


async def test_frames_arrive_at_the_agreed_size_with_a_sample_derived_clock(
    tmp_path: Path,
) -> None:
    payload = bytes(range(256)) * 5                      # 1280 bytes, two frames
    source = emitting(tmp_path, payload)

    frames = await collect(source)

    assert [frame.start_ms for frame in frames] == [0, 20]
    assert all(len(frame.pcm) == FRAME_BYTES for frame in frames)
    assert b"".join(frame.pcm for frame in frames) == payload


async def test_bytes_are_delivered_unchanged(tmp_path: Path) -> None:
    """No resampling, no reinterpretation: the pipe is passed straight through.

    Full-scale alternating extremes, because a path that decoded and re-encoded the
    samples would mangle exactly these bytes while leaving quiet audio looking
    perfectly fine.
    """

    payload = b"\x00\x80\xff\x7f" * 320                  # 1280 bytes, two frames
    source = emitting(tmp_path, payload)

    assert b"".join(frame.pcm for frame in await collect(source)) == payload


async def test_a_short_read_is_reassembled(tmp_path: Path) -> None:
    """A pipe read may return short, so one read per frame is not enough.

    The fake writes a byte at a time, which forces the reassembly loop to matter.
    """

    payload = bytes(range(256)) * 5
    raw = tmp_path / "payload.raw"
    raw.write_bytes(payload)
    # `dd` with a one-byte block size produces one write per byte.
    binary = fake_recorder(tmp_path, f"dd if='{raw}' bs=1 count=1280 2>/dev/null")

    source = PwRecordSource("fake", binary=binary, validate_target=False)
    frames = await collect(source)

    assert b"".join(frame.pcm for frame in frames) == payload


async def test_a_trailing_partial_frame_is_discarded(tmp_path: Path) -> None:
    """Every frame the pipeline sees is exactly one frame long.

    The provider adapters are entitled to rely on that, so a fragment at the end of
    a stream is dropped rather than forwarded short.
    """

    source = emitting(tmp_path, bytes(700))              # one frame plus 60 bytes

    frames = await collect(source)

    assert len(frames) == 1
    assert len(frames[0].pcm) == FRAME_BYTES


# --- lifecycle ------------------------------------------------------------------


async def test_alive_is_true_while_the_process_runs_and_false_after(tmp_path: Path) -> None:
    """`alive` reflects the process, not the last frame delivered.

    A muted device still produces frames, so it must stay True; that case is caught
    by the ingest layer's silence check instead. Here the fake delivers one frame
    and then holds the pipe open, which is the shape of a device that is running but
    has nothing to say.
    """

    raw = tmp_path / "one.raw"
    raw.write_bytes(bytes(FRAME_BYTES))
    binary = fake_recorder(tmp_path, f"cat '{raw}'; exec sleep 30")

    source = PwRecordSource("fake", binary=binary, validate_target=False)

    assert source.alive is False, "not started yet"

    frames: list[AudioFrame] = []
    async for frame in source.frames():
        frames.append(frame)
        assert source.alive is True
        await source.close()

    assert len(frames) == 1
    assert source.alive is False


async def test_close_ends_a_running_capture(tmp_path: Path) -> None:
    """Shutdown must not hang, even with a read in flight."""

    binary = fake_recorder(tmp_path, "exec sleep 60")
    source = PwRecordSource("fake", binary=binary, validate_target=False)

    task = asyncio.create_task(drain(source))
    await asyncio.sleep(0.2)                             # let it spawn and block
    assert source.alive is True

    await source.close()

    assert await asyncio.wait_for(task, timeout=5.0) == []
    assert source.alive is False


async def test_close_is_idempotent(tmp_path: Path) -> None:
    source = emitting(tmp_path, bytes(FRAME_BYTES))

    await source.close()
    await source.close()

    assert source.alive is False


async def test_a_dead_device_ends_the_stream_rather_than_hanging(tmp_path: Path) -> None:
    """Capture ending must stop the pipeline, not leave it waiting forever.

    Silently restarting would keep serving subtitles that no longer match the
    speaker, with the operator seeing a working system.
    """

    binary = fake_recorder(tmp_path, "exit 1")
    source = PwRecordSource("fake", binary=binary, validate_target=False)

    frames = await asyncio.wait_for(collect(source), timeout=5.0)

    assert frames == []
    assert source.alive is False


# --- diagnostics ----------------------------------------------------------------


async def test_diagnostics_survive_the_stream_ending(tmp_path: Path) -> None:
    """The evidence is needed after the failure, not before it."""

    binary = fake_recorder(tmp_path, "echo 'no such target' >&2; exit 1")
    source = PwRecordSource("fake", binary=binary, validate_target=False)

    await collect(source)

    assert "no such target" in source.diagnostics()


def test_diagnostics_is_empty_before_anything_runs(tmp_path: Path) -> None:
    source = PwRecordSource("fake", binary=str(tmp_path / "unused"), validate_target=False)
    assert source.diagnostics() == ""


# --- target validation ----------------------------------------------------------


def test_an_unknown_target_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale target is the most likely way capture fails, so it fails early."""

    monkeypatch.setattr(pw_record, "pipewire_sources", lambda: [("good_mic", "A Mic")])

    with pytest.raises(ValueError, match="no capture source named 'bad_mic'"):
        PwRecordSource("bad_mic")


def test_a_known_target_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pw_record, "pipewire_sources", lambda: [("good_mic", "A Mic")])

    assert PwRecordSource("good_mic").target == "good_mic"


def test_being_unable_to_enumerate_does_not_block_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pw-dump missing is our problem, not the device's."""

    def boom() -> list[tuple[str, str]]:
        raise RuntimeError("pw-dump is not installed")

    monkeypatch.setattr(pw_record, "pipewire_sources", boom)

    assert PwRecordSource("anything").target == "anything"


def test_validation_can_be_skipped_for_injected_binaries(tmp_path: Path) -> None:
    source = PwRecordSource("whatever", binary="/bin/true", validate_target=False)
    assert source.target == "whatever"


# --- enumeration ----------------------------------------------------------------


def test_pipewire_sources_returns_only_capture_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dump = [
        {
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Audio/Source",
                    "node.name": "mic",
                    "node.description": "A Microphone",
                }
            },
        },
        {
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Audio/Sink",
                    "node.name": "speakers",
                    "node.description": "Speakers",
                }
            },
        },
        {"type": "PipeWire:Interface:Client", "info": {"props": {}}},
    ]

    def fake_run(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, json.dumps(dump), "")

    monkeypatch.setattr(pw_record.subprocess, "run", fake_run)

    assert pipewire_sources() == [("mic", "A Microphone")]


def test_pipewire_sources_raises_when_pw_dump_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "PipeWire is not running")

    monkeypatch.setattr(pw_record.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="pw-dump failed"):
        pipewire_sources()


# NOTE: `pipewire_sources()` against the real PipeWire, and `PwRecordSource` against
# a real device, are deliberately not tested here. Both depend on the machine having
# a microphone and a running PipeWire, so they belong in `livesubs --preflight`
# rather than in a suite that has to pass everywhere.


# --- what is logged, and what must not be ----------------------------------------


async def test_a_clean_close_is_not_reported_as_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A normal shutdown closes the stream too, and must not look like a fault.

    Logging it at ERROR puts a false alarm in the log every time an operator stops a
    session, which trains them to ignore the entry that would matter.
    """

    binary = fake_recorder(tmp_path, "exec sleep 60")
    source = PwRecordSource("fake", binary=binary, validate_target=False)

    task = asyncio.create_task(drain(source))
    await asyncio.sleep(0.2)

    with caplog.at_level(logging.ERROR, logger="livesubs.audio.pw_record"):
        await source.close()
        await asyncio.wait_for(task, timeout=5.0)

    assert "capture stopped" not in caplog.text


async def test_an_unexpected_death_is_still_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The one that matters must survive the fix for the one that did not."""

    binary = fake_recorder(tmp_path, "exit 1")
    source = PwRecordSource("fake", binary=binary, validate_target=False)

    with caplog.at_level(logging.ERROR, logger="livesubs.audio.pw_record"):
        await drain(source)

    assert "capture stopped" in caplog.text


async def test_an_unexpected_death_still_marks_the_source_dead(tmp_path: Path) -> None:
    """`alive` reflects the device, so a died process must not report healthy."""

    binary = fake_recorder(tmp_path, "exit 1")
    source = PwRecordSource("fake", binary=binary, validate_target=False)

    await drain(source)

    assert source.alive is False


# --- the invocation, which no test was looking at --------------------------------


def option_value(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


async def test_the_command_requests_the_format_pw_record_understands(
    tmp_path: Path,
) -> None:
    """The guard that was missing, and its absence shipped a real bug.

    ``-P '{ rate=16000, channels=1, format=s16 }'`` sets *node properties*, which
    pw-record does not negotiate the stream format from. It is silently ignored, and the
    device falls back to its defaults of 48 kHz stereo, so a 16 kHz mono pipeline
    received six times the expected data rate: all timings compressed, and two
    interleaved channels read as one.

    Every other test passed while that was true, because the fake recorder ignores its
    arguments. This one reads them.
    """

    payload = bytes(FRAME_BYTES)
    raw = tmp_path / "payload.raw"
    raw.write_bytes(payload)
    argv_path = tmp_path / "argv.txt"

    binary = fake_recorder(
        tmp_path, f"printf '%s\\n' \"$@\" > '{argv_path}'\ncat '{raw}'"
    )
    source = PwRecordSource("mic", binary=binary, validate_target=False)

    await collect(source)

    argv = argv_path.read_text(encoding="utf-8").split()
    assert option_value(argv, "--rate") == "16000"
    assert option_value(argv, "--channels") == "1"
    assert option_value(argv, "--format") == "s16"
    assert option_value(argv, "--target") == "mic"

    assert "-P" not in argv, (
        "-P sets node properties, which pw-record does not negotiate the format from, "
        "so the request would be ignored and the device would use 48 kHz stereo"
    )


# --- calibration: only injected measurements and gain controls ------------------


def measurement(*, clipped: bool = True, rms: float = -3) -> CaptureMeasurement:
    return CaptureMeasurement(32000, rms, -0.1, 0.1 if clipped else 0.0, -90)


@dataclass(frozen=True)
class FakeSetting:
    value: int

    def describe(self) -> str:
        return str(self.value)


class FakeGain:
    def __init__(self) -> None:
        self.setting = FakeSetting(10)
        self.reductions = 0
        self.restored: list[FakeSetting] = []
        self.limits = []
        self.last_applied = None

    def set_operation_limits(self, *, deadline=None, stop_event=None):
        self.limits.append((deadline, stop_event))

    def read(self) -> FakeSetting:
        return self.setting

    def reduce(self, *, expected=None):
        from types import SimpleNamespace
        before = self.setting
        if expected is not None and expected != before:
            raise RuntimeError('external setting changed before setter')
        self.setting = FakeSetting(before.value - 1)
        self.last_applied = self.setting
        self.reductions += 1
        return SimpleNamespace(before=before, after=self.setting)

    def restore(self, setting: FakeSetting) -> None:
        if self.last_applied is None or self.setting != self.last_applied:
            raise RuntimeError('external setting changed; unsafe restore')
        self.restored.append(setting)
        self.setting = setting


async def test_prepare_is_once_and_precedes_spawn(tmp_path, monkeypatch):
    source = emitting(tmp_path, bytes(FRAME_BYTES))
    source.calibration = CalibrationConfig(runtime=False)
    notices = []
    calls = []
    source.set_calibration_reporter(notices.append)

    def calibrate(target, sample_rate, *, config, report, stop_event):
        assert source._proc is None
        assert threading.current_thread() is not threading.main_thread()
        calls.append((target, sample_rate))
        report(CalibrationNotice('ready', 'verified'))
        return measurement(clipped=False)

    monkeypatch.setattr(pw_record, 'calibrate_capture', calibrate)
    await asyncio.gather(source.prepare(), source.prepare())
    frames = await collect(source)
    assert len(frames) == 1
    assert calls == [('fake', 16000)]
    assert [notice.state for notice in notices] == ['ready']


async def test_disabled_calibration_never_queries_gain(tmp_path, monkeypatch):
    source = emitting(tmp_path, b'\x00\x80\xff\x7f' * 32000)
    source.calibration = CalibrationConfig(enabled=False)

    def forbidden(*args, **kwargs):
        pytest.fail('disabled calibration queried hardware')

    monkeypatch.setattr(pw_record, 'calibrate_capture', forbidden)
    monkeypatch.setattr(pw_record, 'discover_gain_control', forbidden)
    assert len(await collect(source)) == 200


async def runtime_source(monkeypatch, values, *, config=None):
    source = PwRecordSource('exact-selected-target', validate_target=False,
                            calibration=config or CalibrationConfig())
    gain = FakeGain()
    measurements = iter(values)
    targets = []
    monkeypatch.setattr(pw_record, 'measure_pcm', lambda pcm: next(measurements))

    def discover(target, **kwargs):
        assert threading.current_thread() is not threading.main_thread()
        targets.append(target)
        return gain

    monkeypatch.setattr(pw_record, 'discover_gain_control', discover)
    return source, gain, targets


async def test_runtime_requires_sustained_clipping_and_verifies(monkeypatch):
    source, gain, targets = await runtime_source(monkeypatch, [
        measurement(), measurement(clipped=False), measurement(), measurement(),
        measurement(clipped=False, rms=-8),
    ])
    notices = []
    source.set_calibration_reporter(notices.append)
    for _ in range(3):
        await source._runtime_window(b'raw')
    assert targets == []
    await source._runtime_window(b'raw')
    assert targets == ['exact-selected-target']
    assert gain.reductions == 1
    await source._runtime_window(b'raw')
    assert [n.state for n in notices] == ['calibrating', 'adjusting', 'ready']
    await source.close()


async def test_ineffective_runtime_reduction_restores_and_fails(monkeypatch):
    source, gain, _ = await runtime_source(monkeypatch, [measurement()] * 3)
    notices = []
    source.set_calibration_reporter(notices.append)
    await source._runtime_window(b'raw')
    await source._runtime_window(b'raw')
    with pytest.raises(CalibrationError, match='did not improve'):
        await source._runtime_window(b'raw')
    assert gain.restored == [FakeSetting(10)]
    assert notices[-1].state == 'failed'
    await source.close()


async def test_runtime_cooldown_and_budget(monkeypatch):
    source, gain, _ = await runtime_source(monkeypatch, [
        measurement(), measurement(), measurement(clipped=False),
        measurement(), measurement(), measurement(),
    ], config=CalibrationConfig(max_adjustments=1))
    for _ in range(5):
        await source._runtime_window(b'raw')
    assert gain.reductions == 1
    source._last_adjustment -= 5
    with pytest.raises(CalibrationError, match='budget'):
        await source._runtime_window(b'raw')
    await source.close()


async def test_prepare_cancellation_joins_hardware_worker(monkeypatch):
    source = PwRecordSource('fake', validate_target=False, calibration=CalibrationConfig())
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def calibrate(*args, **kwargs):
        started.set()
        release.wait(3)
        finished.set()
        return measurement(clipped=False)

    monkeypatch.setattr(pw_record, 'calibrate_capture', calibrate)
    task = asyncio.create_task(source.prepare())
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert source._closed
    assert source._proc is None


@pytest.mark.parametrize('failure', ['unsupported', 'floor', 'external'])
async def test_runtime_control_failures_are_actionable(monkeypatch, failure):
    source, gain, _ = await runtime_source(monkeypatch, [measurement()] * 3)
    notices = []
    source.set_calibration_reporter(notices.append)
    if failure == 'unsupported':
        monkeypatch.setattr(pw_record, 'discover_gain_control', lambda target, **kw: None)
    elif failure == 'floor':
        monkeypatch.setattr(gain, 'reduce', lambda **kw: None)
    await source._runtime_window(b'raw')
    if failure == 'external':
        await source._runtime_window(b'raw')
        gain.setting = FakeSetting(99)
    with pytest.raises(CalibrationError):
        await source._runtime_window(b'raw')
    assert notices[-1].state == 'failed'
    assert notices[-1].message
    await source.close()


async def test_runtime_silence_cannot_verify_adjustment(monkeypatch):
    source, gain, _ = await runtime_source(monkeypatch, [
        measurement(), measurement(), measurement(clipped=False, rms=-90),
        measurement(clipped=False, rms=-90),
    ])
    for _ in range(4):
        await source._runtime_window(b'raw')
    assert gain.restored == []  # A normal four-second pause is not failure.
    source._verification_deadline = time.monotonic() - 1
    with pytest.raises(CalibrationError, match='speech budget'):
        await source._runtime_window(b'raw')
    await source.close()
    assert gain.restored == [FakeSetting(10)]


def phased_runtime_source(tmp_path, suffix: bytes):
    prefix = bytes(128000)  # two two-second clipping windows
    first, last = tmp_path / 'first.raw', tmp_path / 'last.raw'
    applied = tmp_path / 'adjustment-applied'
    first.write_bytes(prefix)
    last.write_bytes(suffix)
    binary = fake_recorder(
        tmp_path,
        f"cat '{first}'; i=0; "
        f"while [ ! -e '{applied}' ] && [ \"$i\" -lt 200 ]; do "
        "i=$((i+1)); sleep 0.01; done; "
        f"sleep 0.30; cat '{last}'",
    )
    source = PwRecordSource('fake', binary=binary, validate_target=False,
                            calibration=CalibrationConfig())

    def report(notice):
        if notice.state == 'adjusting':
            applied.touch()

    source.set_calibration_reporter(report)
    return source, prefix + suffix


async def test_runtime_frames_keep_bytes_and_clock(tmp_path, monkeypatch):
    suffix = b'\x00\x80\xff\x7f' * 16160  # one straddling frame plus a full window
    source, payload = phased_runtime_source(tmp_path, suffix)
    gain = FakeGain()
    measured = []
    values = iter([measurement(), measurement(), measurement(clipped=False, rms=-8)])
    monkeypatch.setattr(pw_record, 'calibrate_capture', lambda *a, **kw: measurement())
    monkeypatch.setattr(pw_record, 'discover_gain_control', lambda target, **kw: gain)

    def measure(pcm):
        measured.append(pcm)
        return next(values)

    monkeypatch.setattr(pw_record, 'measure_pcm', measure)
    frames = await collect(source)
    assert b''.join(f.pcm for f in frames) == payload
    assert [f.start_ms for f in frames] == list(range(0, 6020, 20))
    assert [len(pcm) for pcm in measured] == [64000] * 3
    assert b''.join(measured) == payload[:128000] + suffix[640:]
    assert gain.reductions == 1


async def test_runtime_failure_closes_and_reaps_recorder(tmp_path, monkeypatch):
    source, _ = phased_runtime_source(tmp_path, bytes(64640))
    gain = FakeGain()
    monkeypatch.setattr(pw_record, 'calibrate_capture', lambda *a, **kw: measurement())
    monkeypatch.setattr(pw_record, 'discover_gain_control', lambda target, **kw: gain)
    monkeypatch.setattr(pw_record, 'measure_pcm', lambda pcm: measurement())
    with pytest.raises(CalibrationError, match='did not improve'):
        await collect(source)
    assert source._closed
    assert source._proc.poll() is not None
    assert source._proc.stdout.closed


async def test_runtime_disabled_does_not_measure_or_discover(tmp_path, monkeypatch):
    source = emitting(tmp_path, bytes(192000))
    source.calibration = CalibrationConfig(runtime=False)
    monkeypatch.setattr(pw_record, 'calibrate_capture', lambda *a, **kw: measurement())

    def forbidden(*args):
        pytest.fail('runtime disabled but inspected capture')

    monkeypatch.setattr(pw_record, 'measure_pcm', forbidden)
    monkeypatch.setattr(pw_record, 'discover_gain_control', forbidden)
    assert len(await collect(source)) == 300


async def test_startup_failure_is_cached_and_never_spawns(tmp_path, monkeypatch):
    source = emitting(tmp_path, bytes(FRAME_BYTES))
    source.calibration = CalibrationConfig()
    notices = []
    source.set_calibration_reporter(notices.append)

    def calibrate(*args, **kwargs):
        kwargs['report'](CalibrationNotice('failed', 'Check microphone boost.'))
        raise CalibrationError('Check microphone boost.')

    monkeypatch.setattr(pw_record, 'calibrate_capture', calibrate)
    for _ in range(2):
        with pytest.raises(CalibrationError, match='boost'):
            await source.prepare()
    assert [notice.state for notice in notices] == ['failed']
    with pytest.raises(CalibrationError):
        await collect(source)
    assert source._closed
    assert source._proc is None


async def test_close_cooperatively_stops_and_joins_startup(monkeypatch):
    source = PwRecordSource('fake', validate_target=False, calibration=CalibrationConfig())
    started, finished = threading.Event(), threading.Event()

    def calibrate(*args, stop_event, **kwargs):
        started.set()
        assert stop_event.wait(2)
        finished.set()
        return measurement(clipped=False)

    monkeypatch.setattr(pw_record, 'calibrate_capture', calibrate)
    prepare = asyncio.create_task(source.prepare())
    assert await asyncio.to_thread(started.wait, 1)
    await source.close()
    await prepare
    assert finished.is_set()
    assert not source._workers
    assert source._proc is None


async def test_capture_cancellation_reaps_process_and_reader(tmp_path):
    source = PwRecordSource('fake', validate_target=False,
                            binary=fake_recorder(tmp_path, 'exec sleep 60'))
    task = asyncio.create_task(drain(source))
    await asyncio.sleep(0.05)
    assert source.alive
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source._closed
    assert source._proc.poll() is not None
    assert source._read_task is None


async def test_pump_drains_while_gain_worker_blocked_and_rejects_queued_pcm(tmp_path, monkeypatch):
    payload = bytes(192000)
    source = emitting(tmp_path, payload)
    source.calibration = CalibrationConfig()
    gain = FakeGain()
    entered, release = threading.Event(), threading.Event()
    reduce = gain.reduce
    measured = []
    notices = []
    forwarded = []
    source.set_calibration_reporter(notices.append)

    def blocked_reduce(**kwargs):
        adjustment = reduce(**kwargs)
        entered.set()
        assert release.wait(2)
        return adjustment

    def measure(pcm):
        measured.append(pcm)
        return measurement()

    monkeypatch.setattr(gain, 'reduce', blocked_reduce)
    monkeypatch.setattr(pw_record, 'calibrate_capture', lambda *a, **kw: measurement())
    monkeypatch.setattr(pw_record, 'discover_gain_control', lambda target, **kw: gain)
    monkeypatch.setattr(pw_record, 'measure_pcm', measure)

    async def consume():
        async for frame in source.frames():
            forwarded.append(frame)

    task = asyncio.create_task(consume())
    assert await asyncio.to_thread(entered.wait, 1)
    # Pump reaches EOF while the setter's caller is still blocked. All remaining
    # two seconds are queued with PRE-readback timestamps, not verifier evidence.
    await asyncio.wait_for(asyncio.shield(source._pump_task), 1)
    assert source._queue.qsize() == 101
    assert len(measured) == 2
    release.set()
    with pytest.raises(CalibrationError, match='before gain reduction could be verified'):
        await task
    assert b''.join(frame.pcm for frame in forwarded) == payload
    assert [frame.start_ms for frame in forwarded] == list(range(0, 6000, 20))
    assert len(measured) == 2
    assert not any(n.state == 'ready' for n in notices)
    assert gain.restored == [FakeSetting(10)]


@pytest.mark.parametrize('shutdown', ['close', 'cancel'])
async def test_shutdown_during_setter_joins_then_restores_original(monkeypatch, shutdown):
    source, gain, _ = await runtime_source(monkeypatch, [measurement()] * 2)
    entered, release = threading.Event(), threading.Event()
    reduce = gain.reduce

    def blocked_reduce(**kwargs):
        adjustment = reduce(**kwargs)
        entered.set()
        assert release.wait(2)
        return adjustment

    monkeypatch.setattr(gain, 'reduce', blocked_reduce)
    await source._runtime_window(b'raw')

    async def adjust():
        try:
            await source._runtime_window(b'raw')
        finally:
            await source.close()

    task = asyncio.create_task(adjust())
    assert await asyncio.to_thread(entered.wait, 1)
    assert gain.setting == FakeSetting(9)
    assert source._pending_original == FakeSetting(10)
    assert source._verification is None  # Setter has not returned/read back yet.
    close = None
    if shutdown == 'cancel':
        task.cancel()
    else:
        close = asyncio.create_task(source.close())
    await asyncio.sleep(0)
    assert not task.done()
    if close is not None:
        assert not close.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    if close is not None:
        await close
    assert gain.setting == FakeSetting(10)
    assert gain.restored == [FakeSetting(10)]
    assert source._pending_original is None


async def test_still_clipped_reduction_stays_adjusting_until_clean_speech(monkeypatch):
    source, gain, _ = await runtime_source(monkeypatch, [
        measurement(), measurement(), measurement(rms=-5),
        measurement(clipped=False, rms=-90), measurement(clipped=False, rms=-8),
    ])
    notices = []
    source.set_calibration_reporter(notices.append)
    for _ in range(3):
        await source._runtime_window(b'raw')
    assert notices[-1].state == 'adjusting'
    assert source._pending_original is None  # Hardware effect verified, though still degraded.
    await source._runtime_window(b'raw')
    assert notices[-1].state == 'adjusting'  # Silence does not imply clean speech.
    await source._runtime_window(b'raw')
    assert notices[-1].state == 'ready'
    await source.close()
    assert gain.restored == []  # Keep a verified setting, not a blind rollback.


async def test_settling_frames_are_excluded_from_verification(monkeypatch):
    source, gain, _ = await runtime_source(monkeypatch, [
        measurement(), measurement(), measurement(clipped=False, rms=-8),
    ])
    await source._runtime_window(b'raw')
    await source._runtime_window(b'raw')
    cutoff = source._verification_not_before
    await source._runtime_window(b'old raw', received_at=cutoff - .001)
    assert source._verification is not None
    assert source._verification_windows == 0
    await source._runtime_window(b'new raw', received_at=cutoff)
    assert source._verification is None
    await source.close()
    assert gain.restored == []


async def test_runtime_transaction_and_verification_budgets_are_capped(monkeypatch):
    source, gain, _ = await runtime_source(monkeypatch, [measurement()] * 2,
                                         config=CalibrationConfig(max_duration_seconds=60))
    await source._runtime_window(b'raw')
    await source._runtime_window(b'raw')
    deadline, event = gain.limits[0]
    assert 0 < deadline - time.monotonic() <= 8
    assert event is source._stop_calibration
    assert source._verification_not_before - source._last_adjustment == pytest.approx(.25)
    assert source._verification_deadline - source._last_adjustment == pytest.approx(25)
    await source.close()
    assert gain.restored == [FakeSetting(10)]


async def test_capture_queue_overflow_fails_instead_of_dropping_frames(tmp_path):
    source = emitting(tmp_path, bytes(640 * 650))
    frames = source.frames()
    first = await anext(frames)
    assert first.start_ms == 0
    await asyncio.wait_for(asyncio.shield(source._pump_task), 2)
    assert source._queue.qsize() == source._queue.maxsize
    with pytest.raises(CalibrationError, match='overflowed'):
        await anext(frames)
    assert source._closed
    assert source._proc.poll() is not None


async def test_normal_eof_with_exactly_full_queue_does_not_hang(tmp_path):
    payload = bytes(FRAME_BYTES * 5)
    source = emitting(tmp_path, payload)
    source._queue = asyncio.Queue(maxsize=4)
    frames = source.frames()
    first = await anext(frames)
    await asyncio.wait_for(asyncio.shield(source._pump_task), 1)
    assert source._queue.full()
    assert source._pump_error is None
    assert source._pump_done.is_set()

    async def remainder():
        return [frame async for frame in frames]

    received = [first, *await asyncio.wait_for(remainder(), 1)]
    assert b''.join(frame.pcm for frame in received) == payload
    assert [frame.start_ms for frame in received] == [0, 20, 40, 60, 80]
    assert source._closed
    assert source._proc.poll() is not None


async def test_read_timeout_rolls_back_unverified_gain(tmp_path, monkeypatch):
    prefix = tmp_path / 'prefix.raw'
    prefix.write_bytes(bytes(128000))
    binary = fake_recorder(tmp_path, f"cat '{prefix}'; exec sleep 60")
    source = PwRecordSource('fake', validate_target=False, binary=binary,
                            calibration=CalibrationConfig())
    gain = FakeGain()
    monkeypatch.setattr(pw_record, 'calibrate_capture', lambda *a, **kw: measurement())
    monkeypatch.setattr(pw_record, 'discover_gain_control', lambda target, **kw: gain)
    monkeypatch.setattr(pw_record, 'measure_pcm', lambda pcm: measurement())
    with pytest.raises(CalibrationError, match='stalled'):
        await collect(source, timeout=8)
    assert gain.restored == [FakeSetting(10)]
    assert source._pump_task is None
    assert source._read_task is None


async def test_manual_change_between_original_read_and_setter_is_not_overwritten(tmp_path, monkeypatch):
    source = emitting(tmp_path, bytes(128000))
    source.calibration = CalibrationConfig()
    gain = FakeGain()
    reduce = gain.reduce

    def manually_changed_reduce(**kwargs):
        gain.setting = FakeSetting(99)
        return reduce(**kwargs)

    monkeypatch.setattr(gain, 'reduce', manually_changed_reduce)
    monkeypatch.setattr(pw_record, 'calibrate_capture', lambda *a, **kw: measurement())
    monkeypatch.setattr(pw_record, 'discover_gain_control', lambda target, **kw: gain)
    monkeypatch.setattr(pw_record, 'measure_pcm', lambda pcm: measurement())
    with pytest.raises(CalibrationError, match='external setting changed before setter'):
        await collect(source)
    assert gain.reductions == 0
    assert gain.setting == FakeSetting(99)
    assert gain.restored == []


async def test_reader_exception_still_joins_and_rolls_back(tmp_path, monkeypatch):
    prefix = tmp_path / 'prefix.raw'
    prefix.write_bytes(bytes(128000))
    source = PwRecordSource('fake', validate_target=False, calibration=CalibrationConfig(),
                            binary=fake_recorder(tmp_path, f"cat '{prefix}'; exec sleep 60"))
    gain = FakeGain()
    changed = threading.Event()
    reduce, read = gain.reduce, pw_record._read_exactly
    reads = 0

    def faulting_read(stream, count):
        nonlocal reads
        reads += 1
        if reads == 201:
            assert changed.wait(2)
            raise RuntimeError('injected reader failure')
        return read(stream, count)

    def signal_reduce(**kwargs):
        adjustment = reduce(**kwargs)
        changed.set()
        return adjustment

    monkeypatch.setattr(gain, 'reduce', signal_reduce)
    monkeypatch.setattr(pw_record, '_read_exactly', faulting_read)
    monkeypatch.setattr(pw_record, 'calibrate_capture', lambda *a, **kw: measurement())
    monkeypatch.setattr(pw_record, 'discover_gain_control', lambda target, **kw: gain)
    monkeypatch.setattr(pw_record, 'measure_pcm', lambda pcm: measurement())
    with pytest.raises((CalibrationError, RuntimeError), match='injected reader failure'):
        await collect(source)
    assert gain.restored == [FakeSetting(10)]
    assert source._proc.poll() is not None
    assert source._read_task is None
