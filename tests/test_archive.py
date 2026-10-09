"""Tests for the audio archive.

Archiving is an optional extra wrapped around the audio path, which makes two
properties worth pinning hard: it must not alter the audio it is passing through, and
it must not be able to stop a lecture. The rest is about the file being genuinely
recoverable, because the moment it matters most is the moment the process died.
"""

from __future__ import annotations

import wave
from pathlib import Path

from livesubs.audio.archive import ArchivedSource
from livesubs.audio.base import AudioFrame
from livesubs.audio.calibration import CalibrationNotice
from livesubs.audio.synthetic import SilenceSource

FRAME_BYTES = 640
FRAME_MS = 20

#: A WAV "frame" is one sample per channel, not one 20 ms block. Keeping the two
#: apart matters: conflating them produces assertions that are wrong by 320x, which
#: is exactly the sort of error a test is supposed to catch rather than contain.
SAMPLES_PER_FRAME = 320


def silent_frames(count: int) -> SilenceSource:
    return SilenceSource(realtime=False, duration_ms=count * FRAME_MS)


def archived(tmp_path: Path, count: int = 4, **kwargs: object) -> ArchivedSource:
    settings: dict[str, object] = {"directory": tmp_path / "rec", "label": "lecture"}
    settings.update(kwargs)

    return ArchivedSource(silent_frames(count), **settings)  # type: ignore[arg-type]


async def drain(source: ArchivedSource) -> list[AudioFrame]:
    return [frame async for frame in source.frames()]


async def test_calibration_is_forwarded_without_recording_private_setup_audio(tmp_path):
    class PreparedSource(SilenceSource):
        def set_calibration_reporter(self, reporter):
            self.reporter = reporter

        async def prepare(self):
            self.reporter(CalibrationNotice("ready", "microphone checked"))

    source = ArchivedSource(PreparedSource(realtime=False, duration_ms=40), directory=tmp_path)
    notices = []
    source.set_calibration_reporter(notices.append)
    await source.prepare()
    assert notices[0].message == "microphone checked"
    assert source.path is None
    assert list(tmp_path.iterdir()) == []
    await source.close()


# --- passing through ------------------------------------------------------------


async def test_frames_are_passed_through_unchanged(tmp_path: Path) -> None:
    """The tee must not alter the audio the pipeline consumes."""

    source = archived(tmp_path)
    frames = await drain(source)

    assert len(frames) == 4
    assert all(len(frame.pcm) == FRAME_BYTES for frame in frames)
    assert [frame.start_ms for frame in frames] == [0, 20, 40, 60]


async def test_alive_is_delegated(tmp_path: Path) -> None:
    """The archive has no opinion about whether capture is working."""

    source = archived(tmp_path)
    assert source.alive is True

    await drain(source)
    assert source.alive is False


# --- the file -------------------------------------------------------------------


async def test_the_archive_contains_exactly_what_went_through_it(tmp_path: Path) -> None:
    source = archived(tmp_path)
    frames = await drain(source)

    assert source.path is not None
    assert source.path.exists()

    with wave.open(str(source.path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16_000
        assert wav.getnframes() == 4 * SAMPLES_PER_FRAME
        written = wav.readframes(4 * SAMPLES_PER_FRAME)

    assert written == b"".join(frame.pcm for frame in frames)


async def test_the_directory_is_created(tmp_path: Path) -> None:
    """A first run should not require the operator to mkdir anything."""

    target = tmp_path / "deep" / "nested" / "rec"
    source = ArchivedSource(silent_frames(1), directory=target)

    await drain(source)

    assert target.is_dir()
    assert source.path is not None and source.path.parent == target


async def test_the_filename_identifies_the_recording(tmp_path: Path) -> None:
    source = archived(tmp_path, count=1, label="demo")
    await drain(source)

    assert source.path is not None
    assert source.path.name.startswith("demo-")
    assert source.path.suffix == ".wav"


async def test_two_runs_do_not_collide(tmp_path: Path) -> None:
    """Timestamps are to the second, so the second recording must still be writable."""

    first = archived(tmp_path, count=1)
    await drain(first)

    second = archived(tmp_path, count=1)
    await drain(second)

    # Same directory, so a collision would have shown up as a truncation instead.
    assert first.path is not None and second.path is not None
    assert first.path.exists() and second.path.exists()


async def test_nothing_is_written_if_no_frames_arrive(tmp_path: Path) -> None:
    """An empty file is worse than no file: it looks like a recording."""

    source = archived(tmp_path, count=0)
    await drain(source)

    assert source.path is None


# --- crash resilience -----------------------------------------------------------


async def test_the_file_is_playable_before_it_is_closed(tmp_path: Path) -> None:
    """A killed process must still leave audio, and that holds with no help from us.

    ``wave`` rewrites the header sizes after every write, so the file on disk is
    always current and a hard kill loses at most the frame in flight. The test is
    here because that property is the whole reason the archive is worth anything when
    a lecture goes wrong, and it should fail loudly if a refactor moves the writing
    somewhere that loses it. An earlier version of the module reimplemented this on a
    thirty-second timer; the test outlived that code, deliberately.
    """

    source = archived(tmp_path, count=6)
    frames: list[AudioFrame] = []

    async for frame in source.frames():
        frames.append(frame)
        if len(frames) == 4:
            # Deliberately NOT closed: this is the state a crash would leave.
            assert source.path is not None
            with wave.open(str(source.path), "rb") as wav:
                available = wav.getnframes()
                data = wav.readframes(available)

            assert available == len(frames) * SAMPLES_PER_FRAME
            assert data == b"".join(f.pcm for f in frames)


async def test_a_clean_close_finalises_the_header(tmp_path: Path) -> None:
    source = archived(tmp_path, count=3)
    await drain(source)

    assert source.path is not None
    with wave.open(str(source.path), "rb") as wav:
        assert wav.getnframes() == 3 * SAMPLES_PER_FRAME


# --- failing safely -------------------------------------------------------------


async def test_an_unwritable_directory_does_not_stop_the_audio(tmp_path: Path) -> None:
    """Refusing to serve subtitles because a file could not be opened would trade the
    whole point of the system for its optional extra."""

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("I am a file, so mkdir will fail")

    source = ArchivedSource(silent_frames(4), directory=blocker / "rec")
    frames = await drain(source)

    assert len(frames) == 4, "frames must keep flowing"
    assert source.error is not None
    assert source.recording is False


async def test_a_failure_is_reported_once_rather_than_per_frame(tmp_path: Path) -> None:
    """One error, not one per frame for ninety minutes."""

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file")

    source = ArchivedSource(silent_frames(10), directory=blocker / "rec")
    await drain(source)

    assert source.error is not None
    assert source.path is None


async def test_close_is_idempotent_and_closes_the_inner_source(tmp_path: Path) -> None:
    source = archived(tmp_path, count=2)
    await drain(source)

    await source.close()
    await source.close()

    assert source.source.alive is False


async def test_closing_early_still_produces_a_readable_file(tmp_path: Path) -> None:
    """The pipeline may break out of the frame loop, for instance on a stop request."""

    source = archived(tmp_path, count=10)

    seen = 0
    async for _frame in source.frames():
        seen += 1
        if seen == 2:
            break

    await source.close()

    assert source.path is not None
    with wave.open(str(source.path), "rb") as wav:
        assert wav.getnframes() == 2 * SAMPLES_PER_FRAME


async def test_the_archive_satisfies_the_source_protocol(tmp_path: Path) -> None:
    from livesubs.audio.base import AudioSource

    assert isinstance(archived(tmp_path, count=1), AudioSource)
