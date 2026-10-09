"""Tee captured audio to a WAV file.

A lecture can only be captured once. The archive is what makes a later, slower,
better pass possible: a corrected transcript, a comparison of two ASR providers, an
honest evaluation set. Even a poor recording from a laptop microphone is worth
keeping, because it is the only evidence of what the room actually sounded like.

Three decisions worth stating.

**It decorates a source rather than living in the pipeline.** Archiving is not part
of producing subtitles, so it must not be able to make the pipeline harder to
reason about. Wrapping an :class:`~livesubs.audio.base.AudioSource` means it composes
with every backend, including ones not written yet.

**Archiving never breaks the lecture.** If the disk fills or the directory is not
writable, the failure is logged loudly and recording stops, but frames keep flowing.
The alternative - refusing to serve subtitles because a WAV file could not be
opened - trades the whole point of the system for its optional extra.

**A killed process still leaves playable audio**, and this module does not have to
arrange it. ``wave.Wave_write.writeframes`` rewrites the two header size fields after
every write, restoring the file position itself, so the header is never more than one
frame stale and the loss on a hard kill is bounded by that one frame.

An earlier version of this file patched the header on a thirty-second timer. It was
removed for two reasons: it duplicated the standard library, and a hand-rolled seek
into a file the writer already owns is a corrupt-a-recording risk taken for no
benefit. ``tests/test_archive.py`` still pins a readable-mid-write file, because the
property is worth pinning whoever provides it.
"""

from __future__ import annotations

import logging
import time
import wave
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from livesubs.audio.base import TARGET_CHANNELS, TARGET_RATE, AudioFrame, AudioSource
from livesubs.audio.calibration import CalibrationNotice

__all__ = ["ArchivedSource", "recording_stem"]

logger = logging.getLogger(__name__)


def recording_stem(label: str) -> str:
    """The name shared by every artefact of one recording.

    The audio and its subtitle files share this stem so that they pair up by name,
    which is what makes a folder of recordings browsable: the cues in ``x.de.srt``
    refer to the audio clock of ``x.wav``, and nothing else in the folder says so.
    """

    return f"{label}-{_timestamp()}"


class ArchivedSource:
    """An :class:`~livesubs.audio.base.AudioSource` that also writes a WAV file."""

    def __init__(
        self,
        source: AudioSource,
        *,
        directory: str | Path = "recordings",
        label: str = "lecture",
        stem: str | None = None,
        sample_rate: int = TARGET_RATE,
        channels: int = TARGET_CHANNELS,
    ) -> None:
        self.source = source
        self.directory = Path(directory)
        self.label = label
        #: Shared with the subtitle files written for the same session. Passed in
        #: rather than derived so that the audio and the transcripts cannot end up
        #: with different names, which would break the pairing.
        self.stem = stem or recording_stem(label)
        self.sample_rate = sample_rate
        self.channels = channels

        self._writer: wave.Wave_write | None = None
        self._path: Path | None = None
        self._data_bytes = 0
        self._finalised = False

        #: Why archiving stopped, or ``None``. Surfaced rather than swallowed, because
        #: not having a recording is something the operator needs to know about.
        self.error: str | None = None

    @property
    def path(self) -> Path | None:
        """The file being written, once there is one."""

        return self._path

    @property
    def recording(self) -> bool:
        """Whether frames are currently being archived."""

        return self._writer is not None and self.error is None

    @property
    def alive(self) -> bool:
        """Delegated: the archive has no opinion about whether capture is working."""

        return self.source.alive

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """Yield frames unchanged, writing each to disk on the way through."""

        try:
            async for frame in self.source.frames():
                self._write(frame.pcm)
                yield frame
        finally:
            self._finalise()

    async def prepare(self) -> None:
        """Check the underlying microphone without archiving calibration samples."""
        prepare = getattr(self.source, "prepare", None)
        if prepare is not None:
            await prepare()

    def set_calibration_reporter(self, reporter: Callable[[CalibrationNotice], None]) -> None:
        """Keep capture health visible even when audio archiving wraps the source."""
        setter = getattr(self.source, "set_calibration_reporter", None)
        if setter is not None:
            setter(reporter)

    def _write(self, pcm: bytes) -> None:
        """Append one frame. Never raises: see the module docstring."""

        if self.error is not None:
            return

        try:
            if self._writer is None:
                self._start()

            assert self._writer is not None
            self._writer.writeframes(pcm)
            self._data_bytes += len(pcm)

        except (OSError, wave.Error, ValueError) as exc:
            self._fail(exc)

    def _start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

        self._path = self.directory / f"{self.stem}.wav"

        # Not a `with` block: frames arrive over the whole session, so the writer has
        # to outlive this method. A context manager would close the file after the
        # first frame. `_finalise` is what closes it.
        self._writer = wave.open(str(self._path), "wb")      # noqa: SIM115
        self._writer.setnchannels(self.channels)
        self._writer.setsampwidth(2)
        self._writer.setframerate(self.sample_rate)

        logger.info("archiving audio to %s", self._path)

    def _finalise(self) -> None:
        """Close the file. Idempotent, and safe to call from a cancellation path."""

        if self._finalised:
            return
        self._finalised = True

        writer = self._writer
        self._writer = None

        if writer is None:
            return

        try:
            # `wave` writes the correct sizes here too, so its per-write updates only
            # ever matter for a process that did not get this far.
            writer.close()
        except (OSError, wave.Error) as exc:
            self._fail(exc)
            return

        if self._path is not None:
            logger.info("archived %d bytes of audio to %s", self._data_bytes, self._path)

    def _fail(self, exc: BaseException) -> None:
        """Stop archiving, say so once, and get out of the way."""

        self.error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "audio archiving stopped (%s); the lecture continues unrecorded", self.error
        )

        writer, self._writer = self._writer, None
        if writer is not None:
            try:
                writer.close()
            except (OSError, wave.Error):
                pass

    async def close(self) -> None:
        """Finalise the file, then close the wrapped source."""

        self._finalise()
        await self.source.close()


def _timestamp() -> str:
    """Local wall-clock stamp, because that is how a lecture is remembered."""

    return time.strftime("%Y%m%d-%H%M%S", time.localtime())
