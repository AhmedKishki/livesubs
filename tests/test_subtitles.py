"""Tests for the timestamped subtitle files.

Format correctness matters here in a way that is easy to underestimate: a malformed
subtitle file does not look slightly wrong, it fails to load at all, and the failure
surfaces in a video editor rather than in a log. So the tests parse the output back
with a deliberately strict reader and refuse anything malformed.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from livesubs.pipeline import Committed
from livesubs.subtitles import (
    Cue,
    SubtitleRecorder,
    SubtitleWriter,
    format_cue,
    normalise_text,
    srt_timestamp,
    vtt_timestamp,
)


def parse_srt(text: str) -> list[tuple[int, int, int, str]]:
    """A strict SubRip reader, used to prove the output is well formed.

    Returns ``(index, start_ms, end_ms, text)``. Anything malformed raises rather
    than being tolerated, because tolerance here would hide the very bugs the tests
    exist to catch.
    """

    cues: list[tuple[int, int, int, str]] = []

    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue

        lines = block.split("\n")
        assert len(lines) >= 3, f"a cue needs an index, a timing line and text: {block!r}"

        index = int(lines[0])
        start_text, arrow, end_text = lines[1].partition(" --> ")
        assert arrow == " --> ", f"malformed timing line: {lines[1]!r}"

        cues.append(
            (
                index,
                parse_srt_timestamp(start_text),
                parse_srt_timestamp(end_text),
                "\n".join(lines[2:]),
            )
        )

    return cues


def parse_srt_timestamp(text: str) -> int:
    hours, minutes, rest = text.split(":")
    seconds, millis = rest.split(",")
    return (
        int(hours) * 3_600_000 + int(minutes) * 60_000 + int(seconds) * 1000 + int(millis)
    )


# --- timestamps -----------------------------------------------------------------


@pytest.mark.parametrize(
    "ms,expected",
    [
        (0, "00:00:00,000"),
        (1, "00:00:00,001"),
        (999, "00:00:00,999"),
        (1000, "00:00:01,000"),
        (61_234, "00:01:01,234"),
        (3_600_000, "01:00:00,000"),
        (5_400_000, "01:30:00,000"),          # a 90 minute lecture
    ],
)
def test_srt_timestamps(ms: int, expected: str) -> None:
    assert srt_timestamp(ms) == expected


def test_vtt_timestamps_use_a_dot() -> None:
    """The one character that differs, and the one that breaks a parser if wrong."""

    assert vtt_timestamp(61_234) == "00:01:01.234"


def test_negative_times_are_clamped() -> None:
    """A clock that went backwards must not produce an unparseable cue."""

    assert srt_timestamp(-5) == "00:00:00,000"


# --- text safety ----------------------------------------------------------------


def test_newlines_are_collapsed() -> None:
    """The load-bearing one.

    A blank line terminates a cue, so a translation containing one would not look
    merely wrong: every following cue would become part of this one and the file
    would not parse at all.
    """

    assert normalise_text("a\n\nb") == "a b"
    assert normalise_text("a\r\nb\tc   d") == "a b c d"
    assert normalise_text("  padded  ") == "padded"


def test_a_cue_containing_a_blank_line_stays_one_cue() -> None:
    cue = Cue(start_ms=0, end_ms=1000, text="first\n\nsecond")

    rendered = format_cue(1, cue, "srt")

    assert rendered.count("\r\n\r\n") == 1, "a blank line leaked into the cue body"
    assert parse_srt(rendered)[0][3] == "first second"


def test_vtt_escapes_markup_characters() -> None:
    """A browser refuses to play a VTT cue containing a stray angle bracket."""

    rendered = format_cue(1, Cue(start_ms=0, end_ms=1000, text="a < b & c > d"), "vtt")

    assert "&lt;" in rendered and "&gt;" in rendered and "&amp;" in rendered


def test_srt_is_left_as_plain_text() -> None:
    """SubRip has no escaping, so escaping there would show literally."""

    rendered = format_cue(1, Cue(start_ms=0, end_ms=1000, text="a & b"), "srt")

    assert "a & b" in rendered


# --- one cue --------------------------------------------------------------------


def test_an_srt_cue_has_the_shape_subrip_expects() -> None:
    rendered = format_cue(7, Cue(start_ms=1500, end_ms=3250, text="Hallo Welt"), "srt")

    assert rendered == "7\r\n00:00:01,500 --> 00:00:03,250\r\nHallo Welt\r\n\r\n"


def test_a_vtt_cue_uses_line_feeds() -> None:
    rendered = format_cue(1, Cue(start_ms=0, end_ms=1000, text="Hallo"), "vtt")

    assert "\r" not in rendered
    assert rendered == "1\n00:00:00.000 --> 00:00:01.000\nHallo\n\n"


@pytest.mark.parametrize("fmt", ["srt", "vtt"])
def test_undisplayable_cues_render_as_nothing(fmt: str) -> None:
    """Empty text, or a span of zero length, cannot be shown and must be skipped."""

    assert format_cue(1, Cue(start_ms=0, end_ms=1000, text="   "), fmt) == ""
    assert format_cue(1, Cue(start_ms=500, end_ms=500, text="hi"), fmt) == ""
    assert format_cue(1, Cue(start_ms=900, end_ms=100, text="hi"), fmt) == ""


# --- the writer -----------------------------------------------------------------


def test_no_file_is_created_until_there_is_something_to_write(tmp_path: Path) -> None:
    """An empty file would look like a session that produced nothing."""

    writer = SubtitleWriter(tmp_path / "empty.srt")
    writer.close()

    assert not writer.path.exists()
    assert writer.cue_count == 0


def test_the_file_is_readable_while_still_being_written(tmp_path: Path) -> None:
    """Cues are flushed as they arrive, so a killed session keeps its transcript."""

    writer = SubtitleWriter(tmp_path / "live.srt")

    writer.add(Cue(start_ms=0, end_ms=1000, text="one"))
    writer.add(Cue(start_ms=1000, end_ms=2000, text="two"))

    assert parse_srt(writer.path.read_text(encoding="utf-8")) == [
        (1, 0, 1000, "one"),
        (2, 1000, 2000, "two"),
    ]

    writer.close()


def test_skipped_cues_leave_no_gap_in_the_numbering(tmp_path: Path) -> None:
    """Some players tolerate gaps and some do not, so the writer owns the numbering."""

    writer = SubtitleWriter(tmp_path / "gaps.srt")

    writer.add(Cue(start_ms=0, end_ms=1000, text="kept"))
    writer.add(Cue(start_ms=1000, end_ms=1000, text="zero length, dropped"))
    writer.add(Cue(start_ms=1000, end_ms=2000, text="also kept"))

    writer.close()

    indices = [cue[0] for cue in parse_srt(writer.path.read_text(encoding="utf-8"))]
    assert indices == [1, 2]


def test_a_vtt_file_starts_with_its_header(tmp_path: Path) -> None:
    writer = SubtitleWriter(tmp_path / "out.vtt", fmt="vtt")
    writer.add(Cue(start_ms=0, end_ms=1000, text="Hallo"))
    writer.close()

    text = writer.path.read_text(encoding="utf-8")
    assert text.startswith("WEBVTT\n\n")


def test_a_file_starting_with_a_WEBVTT_line_is_still_on_disk_for_srt(tmp_path: Path) -> None:
    """SRT has no header, so adding one would make the first cue unparseable."""

    writer = SubtitleWriter(tmp_path / "out.srt", fmt="srt")
    writer.add(Cue(start_ms=0, end_ms=1000, text="Hallo"))
    writer.close()

    assert not writer.path.read_text(encoding="utf-8").startswith("WEBVTT")


def test_an_unwritable_path_is_recorded_and_not_raised(tmp_path: Path) -> None:
    """A transcript that cannot be written must not be able to stop a lecture."""

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("I am a file, so mkdir will fail")

    writer = SubtitleWriter(blocker / "out.srt")

    assert writer.add(Cue(start_ms=0, end_ms=1000, text="one")) is False
    assert writer.error is not None
    assert writer.cue_count == 0

    writer.close()


def test_close_is_idempotent(tmp_path: Path) -> None:
    writer = SubtitleWriter(tmp_path / "out.srt")
    writer.add(Cue(start_ms=0, end_ms=1000, text="one"))

    writer.close()
    writer.close()

    assert writer.cue_count == 1

# --- the recorder ---------------------------------------------------------------

STEM = "demo-20260101-120000"


def read_cues(directory: Path, code: str, fmt: str = "srt") -> list[tuple[int, int, int, str]]:
    text = (directory / f"{STEM}.{code}.{fmt}").read_text(encoding="utf-8")
    return parse_srt(text.replace("WEBVTT\n\n", "")) if fmt == "vtt" else parse_srt(text)


def segment(
    seq: int, start: int, end: int, src: str, text: str, language: str = "de"
) -> Committed:
    return Committed(
        seq=seq, start_ms=start, end_ms=end, src=src, text=text, language=language
    )


def record(
    tmp_path: Path,
    *segments: Committed,
    source_code: str = "en",
    target_codes: tuple[str, ...] = ("de",),
) -> tuple[SubtitleRecorder, Path]:
    directory = tmp_path / "artifacts"
    recorder = SubtitleRecorder(
        directory=directory, stem=STEM, source_code=source_code, target_codes=target_codes
    )

    for item in segments:
        recorder(item)
    recorder.close()

    return recorder, directory


def test_the_recorder_is_callable(tmp_path: Path) -> None:
    """It is handed to the pipeline as ``on_segment``, so it has to be."""

    recorder = SubtitleRecorder(
        directory=tmp_path, stem=STEM, source_code="en", target_codes=("de",)
    )

    assert callable(recorder)


def test_both_languages_are_written_in_both_formats(tmp_path: Path) -> None:
    """Four small text files, because the two formats are used by different tools."""

    recorder, _ = record(tmp_path, segment(0, 0, 1500, "hello there", "hallo"))

    assert sorted(path.name for path in recorder.paths) == [
        f"{STEM}.de.srt",
        f"{STEM}.de.vtt",
        f"{STEM}.en.srt",
        f"{STEM}.en.vtt",
    ]
    assert all(path.exists() for path in recorder.paths)
    assert recorder.errors == {}


def test_each_language_file_holds_its_own_text(tmp_path: Path) -> None:
    _, directory = record(tmp_path, segment(0, 0, 1500, "hello there", "hallo"))

    assert read_cues(directory, "de")[0][3] == "hallo"
    assert read_cues(directory, "en")[0][3] == "hello there"


def test_both_languages_share_identical_timings(tmp_path: Path) -> None:
    """They describe the same speech, so any drift between them would be a bug."""

    _, directory = record(
        tmp_path,
        segment(0, 0, 1500, "one", "eins"),
        segment(1, 2200, 3800, "two", "zwei"),
    )

    german = read_cues(directory, "de")
    source = read_cues(directory, "en")

    assert [cue[1:3] for cue in german] == [cue[1:3] for cue in source]


def test_cue_timings_are_the_audio_span(tmp_path: Path) -> None:
    """The timings come from the audio clock, which is what lines up with the WAV."""

    _, directory = record(tmp_path, segment(0, 1234, 5678, "a", "b"))

    _, start, end, _ = read_cues(directory, "de")[0]
    assert (start, end) == (1234, 5678)


def test_cues_are_ordered_and_do_not_overlap(tmp_path: Path) -> None:
    """Overlap is legal but makes a video overlay flicker, so it is worth pinning."""

    _, directory = record(
        tmp_path,
        segment(0, 0, 1000, "a", "a"),
        segment(1, 1500, 2500, "b", "b"),
        segment(2, 2600, 3000, "c", "c"),
    )

    cues = read_cues(directory, "de")

    assert [cue[1] for cue in cues] == [0, 1500, 2600]
    for earlier, later in itertools.pairwise(cues):
        assert earlier[2] <= later[1]


def test_the_vtt_files_are_well_formed_too(tmp_path: Path) -> None:
    _, directory = record(tmp_path, segment(0, 0, 1500, "hello", "hallo"))

    for code in ("en", "de"):
        text = (directory / f"{STEM}.{code}.vtt").read_text(encoding="utf-8")
        assert text.startswith("WEBVTT\n\n")
        assert " --> " in text
        assert "," not in text.split("\n")[2], "VTT timings use a dot, not a comma"


def test_the_stem_pairs_the_files_with_the_audio(tmp_path: Path) -> None:
    """The whole point of a shared stem: the folder is browsable by name."""

    recorder, directory = record(tmp_path, segment(0, 0, 1500, "a", "b"))

    assert {path.stem.split(".")[0] for path in recorder.paths} == {STEM}
    assert len(list(directory.glob(f"{STEM}.*"))) == 4


def test_the_file_names_follow_the_languages_in_use(tmp_path: Path) -> None:
    recorder, _ = record(
        tmp_path,
        segment(0, 0, 1500, "a", "b", language="fr"),
        target_codes=("fr",),
    )

    assert any(path.name == f"{STEM}.fr.srt" for path in recorder.paths)
    assert not any(path.name == f"{STEM}.de.srt" for path in recorder.paths)


# --- several target languages, and either speaker ------------------------------------


def test_every_target_language_gets_its_own_files(tmp_path: Path) -> None:
    recorder, directory = record(
        tmp_path,
        segment(0, 0, 1000, "hello there", "hallo", "de"),
        segment(0, 0, 1000, "hello there", "مرحبا", "ar"),
        target_codes=("de", "ar"),
    )

    names = sorted(path.name for path in recorder.paths)
    assert names == [f"{STEM}.{code}.{fmt}" for code in ("ar", "de", "en") for fmt in ("srt", "vtt")]
    assert read_cues(directory, "de")[0][3] == "hallo"
    assert read_cues(directory, "ar")[0][3] == "مرحبا"


def test_the_speakers_words_are_written_once_however_many_languages_translated_them(
    tmp_path: Path,
) -> None:
    _, directory = record(
        tmp_path,
        segment(0, 0, 1000, "hello there", "hallo", "de"),
        segment(0, 0, 1000, "hello there", "مرحبا", "ar"),
        target_codes=("de", "ar"),
    )

    assert [cue[3] for cue in read_cues(directory, "en")] == ["hello there"]


def test_a_room_that_read_only_one_language_still_gets_a_transcript(tmp_path: Path) -> None:
    """The speaker's words used to be written only by the German stream."""

    _, directory = record(
        tmp_path,
        segment(0, 0, 1000, "hello there", "مرحبا", "ar"),
        target_codes=("de", "ar"),
    )

    assert [cue[3] for cue in read_cues(directory, "en")] == ["hello there"]
    assert [cue[3] for cue in read_cues(directory, "ar")] == ["مرحبا"]


def test_a_german_speaker_is_recorded_as_german_with_english_and_arabic_files(
    tmp_path: Path,
) -> None:
    recorder, directory = record(
        tmp_path,
        segment(0, 0, 1500, "Guten Tag zusammen", "good day everyone", "en"),
        segment(0, 0, 1500, "Guten Tag zusammen", "طاب يومكم", "ar"),
        source_code="de",
        target_codes=("en", "ar"),
    )

    assert {path.name.split(".")[1] for path in recorder.paths} == {"de", "en", "ar"}
    assert read_cues(directory, "de")[0][3] == "Guten Tag zusammen"
    assert read_cues(directory, "en")[0][3] == "good day everyone"


def test_a_segment_for_a_language_with_no_file_is_ignored(tmp_path: Path) -> None:
    recorder, directory = record(tmp_path, segment(0, 0, 1000, "a", "b", "fr"))

    assert not (directory / f"{STEM}.fr.srt").exists()
    assert recorder.errors == {}
