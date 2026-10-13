"""Live capture from a PipeWire source, via ``pw-record``.

**Why a subprocess rather than a PipeWire binding.** ``pw-record`` is already
installed and already the tool the pre-flight check uses, so shelling out adds no
dependency and keeps one source of truth about how capture is invoked. It also puts
a process boundary between a PipeWire misbehaviour and this program: a client
library segfault would take the lecture down with it, whereas a dead subprocess is
detectable and reportable.

**Reads are blocking, so they run on a thread.** ``pw-record`` paces itself in real
time, which means a blocking read of one frame is exactly the pacing this backend
needs. Doing it on the event loop would freeze the server; ``asyncio.to_thread``
moves it off, at a cost of fifty thread handoffs a second, which is nothing.
An independent, bounded pump keeps draining while hardware gain transactions run;
reception timestamps keep queued pre-adjustment audio out of gain verification.

**No automatic restart, deliberately.** A backend that silently respawned
``pw-record`` on failure would keep serving subtitles that stopped matching the
speaker, and the operator would see a working system. Instead the stream ends, the
pipeline stops, and viewers see ``stopped``: a visible failure in a lecture is
recoverable in seconds, whereas an invisible one is not. Restart policy belongs to
the watchdog, which can escalate to a human.

**stderr goes to a file, not a pipe.** A pipe nobody drains fills at 64 KiB and then
blocks the writer, which would hang capture during a long lecture. A temporary file
has no such limit and still lets :meth:`PwRecordSource.diagnostics` explain a
failure in the operator's own terms.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import IO

from livesubs.audio.base import TARGET_RATE, AudioFrame
from livesubs.audio.calibration import (
    CalibrationConfig,
    CalibrationError,
    CalibrationNotice,
    calibrate_capture,
    measure_pcm,
)
from livesubs.audio.hardware_gain import discover_gain_control

__all__ = ["GainProtectionError", "PwRecordSource", "pipewire_sources"]

logger = logging.getLogger(__name__)


class GainProtectionError(CalibrationError):
    """The optional runtime gain protection gave up.

    Distinct from a capture failure on purpose. Protecting the microphone from clipping
    is an extra, and when it cannot be done - the device has no adjustable control, a
    correction could not be verified, the budget is spent - the lecture is still being
    captured. Only the extra stops, and the operator is told. A capture that stalls or
    overflows is a different failure, and still ends the stream.
    """


def pipewire_sources() -> list[tuple[str, str]]:
    """Return ``(node.name, node.description)`` for every capture source.

    The single place that asks PipeWire what is available. Both this backend and the
    pre-flight check use it, so the two cannot disagree about what exists.
    """

    result = subprocess.run(
        ["pw-dump"], capture_output=True, text=True, timeout=15, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"pw-dump failed: {result.stderr.strip() or 'no output'}")

    sources: list[tuple[str, str]] = []
    for node in json.loads(result.stdout):
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        props = node.get("info", {}).get("props", {})
        if props.get("media.class") != "Audio/Source":
            continue
        sources.append((props.get("node.name", ""), props.get("node.description", "")))

    return sources

class PwRecordSource:
    """An :class:`~livesubs.audio.base.AudioSource` reading from a live device.

    Always requests mono. ``pw-record`` downmixes in C when asked, so there is no
    reason to expose a knob whose only correct value is one.
    """

    def __init__(
        self,
        target: str,
        *,
        sample_rate: int = TARGET_RATE,
        frame_ms: int = 20,
        binary: str = "pw-record",
        validate_target: bool = True,
        calibration: CalibrationConfig | None = None,
    ) -> None:
        self.target = target
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.binary = binary
        self.calibration = calibration
        self._reporter: Callable[[CalibrationNotice], None] | None = None
        self._last_notice: tuple[str, str] | None = None
        self._prepare_task: asyncio.Task | None = None
        self._stop_calibration = threading.Event()
        self._workers: set[asyncio.Task] = set()
        self._read_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        # Enough room for a bounded gain transaction, not an unbounded recording.
        self._queue: asyncio.Queue[tuple[bytes, float] | None] = asyncio.Queue(
            maxsize=max(2, 12000 // max(1, frame_ms))
        )
        self._pump_error: Exception | None = None
        self._pump_done = asyncio.Event()
        self._window = bytearray()
        self._clipped_windows = 0
        self._adjustments = 0
        self._last_adjustment = float('-inf')
        self._gain = None
        self._expected_gain = None
        self._verification = None
        self._verification_windows = 0
        self._verification_not_before = float('-inf')
        self._verification_deadline = float('inf')
        self._pending_original = None
        self._degraded = False
        #: Set once gain protection has given up, so it is not attempted again.
        self._protection_off = False

        self._frame_samples = sample_rate * frame_ms // 1000
        if self._frame_samples <= 0:
            raise ValueError(f"frame_ms={frame_ms} is too small for {sample_rate} Hz")

        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr: IO[bytes] | None = None
        self._diagnostics: str | None = None
        self._closed = False
        self._died = False
        self._samples_emitted = 0

        if validate_target:
            self._check_target()

    def _check_target(self) -> None:
        """Refuse to start on a target that does not exist.

        A wrong or stale target is the most likely way capture fails in practice,
        and one subprocess call before the lecture is a cheap price for finding out
        while there is still time to fix it.
        """

        try:
            available = pipewire_sources()
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            # Not being able to enumerate is our problem, not the device's. Refusing
            # to capture because pw-dump is missing would be perverse.
            logger.warning("cannot list PipeWire sources, skipping target check: %s", exc)
            return

        names = sorted(name for name, _ in available if name)
        if self.target not in names:
            raise ValueError(
                f"no capture source named {self.target!r}; available: "
                f"{', '.join(names) or 'none'}"
            )

    def set_calibration_reporter(
        self, callback: Callable[[CalibrationNotice], None] | None,
    ) -> None:
        self._reporter = callback

    def _notice(self, state: str, message: str) -> None:
        if self._last_notice == (state, message):
            return
        self._last_notice = (state, message)
        if self._reporter is not None:
            self._reporter(CalibrationNotice(state, message))

    async def _blocking(self, function, *args, **kwargs):
        """Shield and join bounded hardware work, including on cancellation."""
        if self._closed:
            raise asyncio.CancelledError
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._workers.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._stop_calibration.set()
            # A thread cannot be killed safely while it changes a device setting.
            # Keep joining even if shutdown is cancelled again.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 -- cooperative stop may fail the joined worker
                    break
            if not task.cancelled():
                task.exception()
            raise
        finally:
            self._workers.discard(task)

    async def prepare(self) -> None:
        """Calibrate once, before opening the long-lived recording stream."""
        if self._closed:
            return
        if self._prepare_task is None:
            async def run() -> None:
                config = self.calibration
                if config is not None and config.enabled:
                    loop = asyncio.get_running_loop()
                    def report(notice: CalibrationNotice) -> None:
                        loop.call_soon_threadsafe(self._notice, notice.state, notice.message)
                    await self._blocking(
                        calibrate_capture, self.target, self.sample_rate,
                        config=config, report=report, stop_event=self._stop_calibration,
                    )
            self._prepare_task = asyncio.create_task(run())
        try:
            await asyncio.shield(self._prepare_task)
        except asyncio.CancelledError:
            await self.close()
            raise

    async def _runtime_window(self, pcm: bytes, *, received_at: float | None = None) -> None:
        config = self.calibration
        if config is None or not config.enabled or not config.runtime:
            return
        if (
            self._verification is not None and received_at is not None
            and received_at < self._verification_not_before
        ):
            return
        self._check_verification_deadline()
        measurement = await self._blocking(measure_pcm, pcm)
        self._ensure_runtime_active()
        clipped = measurement.clipped(config.clipping_limit)
        if self._verification is not None:
            before, original = self._verification
            self._verification_windows += 1
            await self._blocking(
                self._gain.set_operation_limits,
                deadline=time.monotonic() + min(8.0, config.max_duration_seconds),
                stop_event=self._stop_calibration,
            )
            current = await self._blocking(self._gain.read)
            self._ensure_runtime_active()
            if current != self._expected_gain:
                self._abandon("Capture gain changed externally; stop and check the selected microphone.")
            # Silence cannot establish whether the gain correction worked.
            if measurement.rms_dbfs > -60:
                if not clipped:
                    self._verification = None
                    self._pending_original = None
                    self._degraded = False
                    self._clipped_windows = 0
                    self._notice('ready', 'Capture gain reduction verified on raw audio.')
                    return
                if measurement.rms_dbfs <= before.rms_dbfs - 1:
                    self._verification = None
                    self._pending_original = None
                    self._degraded = True
                    self._clipped_windows = 0
                    self._notice(
                        'adjusting',
                        'Gain reduction affected raw audio, but clipping persists; '
                        'waiting for the next bounded correction.',
                    )
                    return
                await self._rollback_pending()
                self._abandon('Gain reduction did not improve capture; original gain restored. Check microphone boost or input routing.')
            return
        if not clipped and measurement.rms_dbfs > -60 and self._degraded:
            self._degraded = False
            self._notice('ready', 'Post-adjustment speech is no longer clipped.')
        self._clipped_windows = self._clipped_windows + 1 if clipped else 0
        if self._clipped_windows < max(2, config.sustained_windows):
            return
        now = asyncio.get_running_loop().time()
        if now - self._last_adjustment < max(5.0, config.cooldown_seconds):
            return
        if self._adjustments >= min(4, config.max_adjustments):
            self._abandon('Capture remains clipped after the runtime adjustment budget; lower microphone boost manually.')
        self._notice('calibrating', 'Sustained raw capture clipping detected; checking selected microphone gain.')
        deadline = time.monotonic() + min(8.0, config.max_duration_seconds)
        if self._gain is None:
            self._gain = await self._blocking(
                discover_gain_control, self.target,
                deadline=deadline, stop_event=self._stop_calibration,
            )
        if self._gain is None:
            self._abandon('Selected microphone has no supported gain control; lower its hardware gain manually.')
        await self._blocking(
            self._gain.set_operation_limits, deadline=deadline, stop_event=self._stop_calibration,
        )
        original = await self._blocking(self._gain.read)
        self._ensure_runtime_active()
        if self._expected_gain is not None and original != self._expected_gain:
            self._abandon('Capture gain changed externally; check microphone settings before restarting.')
        # Record rollback ownership BEFORE entering a worker that may mutate gain.
        self._pending_original = original
        adjustment = await self._blocking(self._gain.reduce, expected=original)
        self._ensure_runtime_active()
        if adjustment is None:
            self._pending_original = None
            self._abandon('Microphone gain is at its floor; check microphone boost or move away from the microphone.')
        if adjustment.before != original:
            self._abandon('Capture gain changed during adjustment; check microphone settings before restarting.')
        readback = await self._blocking(self._gain.read)
        self._ensure_runtime_active()
        if readback != adjustment.after:
            self._abandon('Microphone did not retain the reduced gain; check hardware controls before restarting.')
        self._expected_gain = adjustment.after
        self._verification = (measurement, original)
        applied_at = time.monotonic()
        self._verification_not_before = applied_at + 0.250
        self._verification_deadline = applied_at + min(25.0, config.max_duration_seconds)
        self._window.clear()
        self._verification_windows = 0
        self._adjustments += 1
        self._last_adjustment = applied_at
        self._notice('adjusting', f'Capture gain reduced: {adjustment.before.describe()} → {adjustment.after.describe()}; verifying raw audio.')

    def _check_verification_deadline(self) -> None:
        if self._verification is not None and time.monotonic() >= self._verification_deadline:
            self._abandon('Gain correction could not be verified within the speech budget; stop and check the microphone.')

    def _ensure_runtime_active(self) -> None:
        # A pump failure/close can race a successful hardware worker. It must
        # not turn an unverified transaction into a retained or ready setting.
        if self._closed:
            raise asyncio.CancelledError
        if self._pump_error is not None:
            raise self._pump_error
        self._check_verification_deadline()

    async def _rollback_pending(self) -> None:
        """Independent, bounded compare-and-restore, also permitted after close."""
        original = self._pending_original
        if original is None or self._gain is None:
            return

        def restore() -> None:
            # Backend restore owns an independent <=3s cleanup deadline and
            # ignores the already-set stop event. It compares with its last
            # applied value; never overwrite an operator's newer setting.
            self._gain.restore(original)

        task = asyncio.create_task(asyncio.to_thread(restore))
        self._workers.add(task)
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            self._pending_original = None
            self._verification = None
        finally:
            self._workers.discard(task)

    def _fail(self, message: str) -> None:
        self._notice('failed', message)
        raise CalibrationError(message)

    def _abandon(self, message: str) -> None:
        """Give up on gain protection without giving up on the lecture."""

        self._notice('degraded', message)
        raise GainProtectionError(message)

    @property
    def alive(self) -> bool:
        """Whether capture could still deliver audio.

        Reflects the *process*, not the last frame delivered. A muted device still
        produces frames, so it correctly stays ``True``; a vanished device or a
        crashed ``pw-record`` does not.
        """

        if self._closed or self._died:
            return False

        proc = self._proc
        return proc is not None and proc.poll() is None

    def diagnostics(self) -> str:
        """Whatever ``pw-record`` wrote to stderr, for an operator-facing message.

        Read *after* the process is reaped and cached, because the moment this is
        most needed is the moment the file is about to be closed. Returning an
        empty string then would make the one useful piece of evidence disappear
        exactly when it mattered.
        """

        if self._diagnostics is not None:
            return self._diagnostics
        return self._read_stderr()

    def _read_stderr(self) -> str:
        handle = self._stderr
        if handle is None:
            return ""

        try:
            handle.seek(0)
            return handle.read().decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            return ""

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """Yield frames as the device produces them.

        There is no sleep here, and that is deliberate: ``pw-record`` runs in real
        time, so a blocking read of one frame *is* the pacing. Adding a sleep would
        double-count it and make capture fall progressively further behind.
        """

        try:
            await self.prepare()
            if self._closed:
                return
            if self._proc is None:
                self._spawn()
            assert self._proc is not None and self._proc.stdout is not None
            self._pump_task = asyncio.create_task(self._pump())
            while not self._closed:
                if self._pump_error is not None:
                    raise self._pump_error
                if self._queue.empty() and self._pump_done.is_set():
                    item = None
                else:
                    item = await self._queue.get()
                if self._pump_error is not None:
                    raise self._pump_error
                if self._closed:
                    return

                if item is None:
                    if self._pending_original is not None:
                        self._fail('Capture ended before gain reduction could be verified; restoring original gain.')
                    # Only an *unexpected* end is a failure. A clean shutdown closes the
                    # stream too, and logging that at ERROR would put a false alarm in
                    # the log every time an operator pressed Ctrl-C, which trains them
                    # to ignore the one entry that matters.
                    if not self._closed:
                        self._died = True
                        logger.error(
                            "capture stopped: %s", self.diagnostics() or "no diagnostics"
                        )
                    return

                chunk, received_at = item
                config = self.calibration
                if (
                    config is not None and config.enabled and config.runtime
                    and not self._protection_off
                ):
                    try:
                        await self._protect(chunk, received_at, config)
                    except Exception as exc:
                        if exc is self._pump_error:
                            raise
                        await self._stand_down(exc)
                yield AudioFrame(
                    pcm=chunk,
                    start_ms=self._samples_emitted * 1000 // self.sample_rate,
                    duration_ms=self.frame_ms,
                )
                self._samples_emitted += self._frame_samples
        finally:
            await self.close()

    async def _protect(self, chunk: bytes, received_at: float, config: CalibrationConfig) -> None:
        """Feed one frame to the runtime gain protection."""

        self._check_verification_deadline()
        # Queued pre-change PCM and the settling interval are still
        # forwarded verbatim, but cannot prove a hardware change.
        if self._verification is not None and received_at < self._verification_not_before:
            self._window.clear()
        else:
            self._window.extend(chunk)
        window_bytes = max(1, int(self.sample_rate * max(2.0, config.measure_seconds))) * 2
        while len(self._window) >= window_bytes:
            pcm = bytes(self._window[:window_bytes])
            del self._window[:window_bytes]
            try:
                await self._runtime_window(pcm, received_at=received_at)
            except CalibrationError:
                raise
            except Exception as exc:  # noqa: BLE001 -- any hardware error ends the protection
                self._abandon(f'Cannot correct capture gain: {exc}. Check microphone controls.')

    async def _stand_down(self, error: Exception) -> None:
        """Turn the gain protection off for the rest of the session and keep capturing.

        The protection is an extra. Two seconds of clipping on a microphone with no
        adjustable control used to raise out of the capture stream, which ended the
        lecture to avoid the risk of it sounding worse. Now the extra stops, an
        unverified correction is put back, the operator is told what to do by hand, and
        the audio keeps flowing.
        """

        self._protection_off = True
        self._window.clear()
        message = str(error) or type(error).__name__
        logger.error("gain protection is off for the rest of this session: %s", message)
        if not isinstance(error, GainProtectionError):
            self._notice('degraded', f'Gain protection stopped: {message}')
        try:
            await self._rollback_pending()
        except Exception as exc:  # noqa: BLE001 -- reported, never fatal here
            self._notice(
                'degraded',
                f'Cannot safely restore unverified microphone gain: {exc}. '
                'Check the hardware gain by hand.',
            )
            logger.error("could not restore the original gain: %s", exc)
        # One attempt only. If someone else changed the setting meanwhile, trying again
        # (or trying at close) would overwrite what they chose.
        self._pending_original = None
        self._verification = None

    async def _pump(self) -> None:
        """Drain independently of gain workers; overflow is a visible failure."""
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while not self._closed:
                # Conservatively classify a read that straddles the adjustment
                # as pre-change, even if its last bytes arrive after readback.
                received_at = time.monotonic()
                self._read_task = asyncio.create_task(
                    asyncio.to_thread(_read_exactly, stdout, self._frame_samples * 2)
                )
                try:
                    chunk = await asyncio.wait_for(
                        asyncio.shield(self._read_task),
                        timeout=max(5.0, self.frame_ms / 1000 * 3),
                    )
                except TimeoutError as exc:
                    raise CalibrationError('Microphone capture stalled; check the selected input and PipeWire.') from exc
                if self._closed or chunk is None:
                    return
                try:
                    self._queue.put_nowait((chunk, received_at))
                except asyncio.QueueFull as exc:
                    raise CalibrationError('Capture queue overflowed during processing; stop and check capture latency. No frames were silently dropped.') from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- forward pump errors to the owning iterator
            self._died = True
            self._pump_error = exc
            self._stop_calibration.set()
            if not self._closed:
                self._notice('failed', str(exc))
        finally:
            self._pump_done.set()
            # No blocking put: a full queue already wakes its consumer, which
            # checks _pump_error before forwarding another frame and _pump_done
            # after draining it. Normal EOF cannot lose its terminal state.
            if not self._queue.full():
                self._queue.put_nowait(None)

    def _spawn(self) -> None:
        """Start ``pw-record`` writing raw PCM to a pipe.

        The format is requested with ``--rate/--channels/--format``, and deliberately
        **not** with ``-P '{ rate=16000, ... }'``. ``-P`` sets *node properties*, which
        pw-record does not negotiate the stream format from, so that form is silently
        ignored and the device defaults of 48 kHz stereo are used instead. Feeding that
        to a 16 kHz mono pipeline delivers six times the expected data rate: every
        timing is compressed, cue timestamps end up far out of step with the recording,
        and two interleaved channels are read as one. Measured against the real device,
        because no test was looking at the command line.
        """

        # Not a `with` block: this file is the subprocess's stderr for its whole
        # life, so it has to outlive this method. A context manager would close it
        # before `pw-record` had written anything.
        self._stderr = tempfile.TemporaryFile()      # noqa: SIM115
        self._diagnostics = None

        self._proc = subprocess.Popen(
            [
                self.binary,
                "--target",
                self.target,
                "--rate",
                str(self.sample_rate),
                "--channels",
                "1",
                "--format",
                "s16",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=self._stderr,
        )

    async def close(self) -> None:
        """Terminate the subprocess and release the pipe. Safe to call twice."""
        self._closed = True
        self._stop_calibration.set()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._finish_close())
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                continue
        self._close_task.result()

    async def _finish_close(self) -> None:
        # Kill/reap first, so a pending pipe read cannot hold up hardware rollback.
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 3.0)
            except subprocess.TimeoutExpired:
                # It ignored SIGTERM, which a wedged audio client sometimes does.
                proc.kill()
                await asyncio.to_thread(proc.wait)

        pump_task = self._pump_task
        if pump_task is not None:
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
        read_task = self._read_task
        if read_task is not None:
            try:
                await asyncio.shield(read_task)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 -- pump reported the error; still roll back gain
                pass
            self._read_task = None
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass

        # A successful setter may have completed while its awaiting coroutine
        # was cancelled. Original ownership was recorded before that setter.
        pending = list(self._workers)
        if self._prepare_task is not None and not self._prepare_task.done():
            pending.append(self._prepare_task)
        for task in pending:
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 -- failed workers still need cleanup
                pass
        try:
            await self._rollback_pending()
        except Exception as exc:  # noqa: BLE001 -- report unsafe rollback without losing process cleanup
            message = f'Cannot safely restore unverified microphone gain: {exc}. Check hardware gain before restarting.'
            self._notice('failed', message)
            logger.error(message)

        if self._stderr is not None:
            # Cache first, then close: see `diagnostics`.
            self._diagnostics = self._read_stderr()
            try:
                self._stderr.close()
            except OSError:
                pass
            self._stderr = None


def _read_exactly(stream: IO[bytes], count: int) -> bytes | None:
    """Read exactly ``count`` bytes, or ``None`` at end of stream.

    A pipe read may return short, so one read is not enough. A closed stream raises
    rather than returning empty, which happens when shutdown races a read already in
    flight, so that is treated as end of stream too.

    A trailing partial frame is discarded rather than returned. Every frame the
    pipeline sees is therefore exactly one frame long, which is an invariant the
    provider adapters are entitled to rely on.
    """

    buf = bytearray()

    while len(buf) < count:
        try:
            chunk = stream.read(count - len(buf))
        except (ValueError, OSError):
            return None

        if not chunk:
            return None

        buf.extend(chunk)

    return bytes(buf)
