"""Timestamped subtitle files, so a recording can be overlaid on a video.

Every source commitment is written once to the raw source file, independently of successful
target output. All the files carry the same cue timings, and those timings
come from the **audio clock** rather than from wall time.

That is the whole point of the design. The audio clock starts at zero on the first
captured frame, and so does the archived WAV, so a cue's timestamps and the
recording's timeline agree by construction. A file that used wall-clock time would
be off by however long the machine took to start, which is exactly the problem that
makes hand-synced subtitles so tedious.

Two formats are written from one cue model, so they cannot disagree:

* **SRT**, because that is what video tools and ffmpeg's ``subtitles`` filter take.
* **WebVTT**, because that is what a browser ``<track>`` element wants.

Text is normalised and, for WebVTT, escaped. That is not cosmetic: a blank line
*terminates* a cue in both formats, so a stray newline in a translation would not
merely look wrong, it would make every following cue part of the previous one and
the file would fail to parse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from livesubs.pipeline.records import SourceSegment

__all__ = [
    "Cue",
    "CueSource",
    "SubtitleRecorder",
    "SubtitleWriter",
    "format_cue",
    "normalise_text",
    "srt_timestamp",
    "vtt_timestamp",
]

CueFormat = Literal["srt", "vtt"]

logger = logging.getLogger(__name__)

#: SRT numbers its cues; WebVTT identifiers are optional but harmless, and having them
#: makes a hand-edited file easier to read.
DEFAULT_FORMATS: tuple[CueFormat, ...] = ("srt", "vtt")


@dataclass(frozen=True, slots=True)
class Cue:
    """One subtitle line, with the audio span it covers.

    The index is deliberately absent. The writer assigns it, so a cue that cannot be
    displayed is skipped without leaving a gap in the numbering, which some players
    tolerate and some do not.
    """

    start_ms: int
    end_ms: int
    text: str


@runtime_checkable
class CueSource(Protocol):
    """Anything carrying a committed segment's audio span, its source and its translation.

    Structural rather than imported, so this module does not depend on the pipeline
    that produces them.
    """

    seq: int
    start_ms: int
    end_ms: int
    src: str
    text: str
    language: str


def normalise_text(text: str) -> str:
    """Collapse all whitespace runs to single spaces.

    Load-bearing rather than tidy: see the module docstring on blank lines ending a
    cue.
    """

    return " ".join(text.split())


def srt_timestamp(ms: int) -> str:
    """``HH:MM:SS,mmm``, which is what SubRip uses (comma, not dot)."""

    return _timestamp(ms, separator=",")


def vtt_timestamp(ms: int) -> str:
    """``HH:MM:SS.mmm``, which is what WebVTT uses."""

    return _timestamp(ms, separator=".")


def _timestamp(ms: int, *, separator: str) -> str:
    clamped = max(0, int(ms))
    hours, rest = divmod(clamped, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def format_cue(index: int, cue: Cue, fmt: CueFormat = "srt") -> str:
    """Render one cue, or an empty string if there is nothing displayable in it."""

    text = normalise_text(cue.text)
    if not text or cue.end_ms <= cue.start_ms:
        return ""

    if fmt == "vtt":
        text = _escape_vtt(text)

    stamp = srt_timestamp if fmt == "srt" else vtt_timestamp
    eol = _eol(fmt)

    return (
        f"{index}{eol}{stamp(cue.start_ms)} --> {stamp(cue.end_ms)}{eol}{text}{eol}{eol}"
    )


def _eol(fmt: CueFormat) -> str:
    """SubRip conventionally uses CRLF, WebVTT uses LF.

    Both are tolerated almost everywhere, but writing the conventional one costs
    nothing and avoids a stray ``^M`` question in one direction and a spec complaint
    in the other.
    """

    return "\r\n" if fmt == "srt" else "\n"


def _escape_vtt(text: str) -> str:
    """Escape the characters WebVTT treats as markup.

    ASR output is plain speech and realistically contains none of these. It is
    handled anyway because the cost is one line and the failure would be a file a
    browser refuses to play. A literal ``-->`` in cue text is the one case left
    unhandled: it cannot come from speech, and replacing it with something invisible
    would be a worse lie than the limitation.
    """

    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

class SubtitleWriter:
    """Appends cues to one subtitle file as they are committed.

    Written incrementally and flushed per cue, for the same reason the audio archive
    is: the moment these files matter most is the moment the process died. A session
    that is killed leaves every cue up to the last one, and the last cue is the only
    thing that can be lost.

    Never raises. A text file that cannot be written must not be able to stop a
    lecture, so a failure is recorded on ``error`` and reported once.
    """

    def __init__(self, path: str | Path, *, fmt: CueFormat = "srt") -> None:
        self.path = Path(path)
        self.fmt = fmt

        #: Why writing stopped, or ``None``.
        self.error: str | None = None

        self._handle = None
        self._written = 0

    @property
    def cue_count(self) -> int:
        return self._written

    def add(self, cue: Cue) -> bool:
        """Append one cue. Returns whether anything was actually written."""

        if self.error is not None:
            return False

        try:
            if self._handle is None:
                self._open()

            assert self._handle is not None
            rendered = format_cue(self._written + 1, cue, self.fmt)
            if not rendered:
                # Nothing displayable: an empty line, or a span of zero length. It is
                # skipped rather than written, so the numbering has no gaps.
                return False

            self._handle.write(rendered)
            self._handle.flush()
            self._written += 1
            return True

        except (OSError, ValueError) as exc:
            self._fail(exc)
            return False

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # newline="" is required, not cosmetic: the default translates "\n" to the
        # platform separator, which would turn the CRLF already written for SubRip
        # into CRCRLF on Windows.
        self._handle = self.path.open("w", encoding="utf-8", newline="")

        if self.fmt == "vtt":
            self._handle.write("WEBVTT\n\n")
            self._handle.flush()

    def _fail(self, exc: BaseException) -> None:
        self.error = f"{type(exc).__name__}: {exc}"
        logger.error("subtitle file %s could not be written: %s", self.path, self.error)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return

        try:
            handle.close()
        except OSError as exc:
            self._fail(exc)


class SubtitleRecorder:
    """Writes raw commitments separately from successful target output.

    Callable, so it can be handed to the pipeline directly as its segment sink. All
    the files share the recording stem, so they pair with the archived audio by name:

    .. code-block:: text

        demo-20260922-145157.wav        the audio the timings refer to
        demo-20260922-145157.de.srt     what a German viewer read
        demo-20260922-145157.ar.srt     what an Arabic viewer read
        demo-20260922-145157.source.srt what was actually said
        demo-20260922-145157.de.vtt     the same, for a browser <track>

    Both formats for every language, because the two are used by different tools and
    neither is a superset of the other in practice. The files are small text; the
    duplication is cheaper than a decision about which one someone will need.

    ``record_source`` receives ordered source commitments. The callable receives only
    successful target output. Target failures cannot erase or reorder the raw transcript.
    """

    def __init__(
        self,
        *,
        directory: str | Path,
        stem: str,
        source_code: str,
        target_codes: tuple[str, ...],
        formats: tuple[CueFormat, ...] = DEFAULT_FORMATS,
    ) -> None:
        self.directory = Path(directory)
        self.stem = stem
        self.source_code = source_code
        self.target_codes = tuple(target_codes)

        codes = dict.fromkeys(("source", *self.target_codes))
        self.writers: dict[tuple[str, CueFormat], SubtitleWriter] = {
            (code, fmt): SubtitleWriter(
                self.directory / f"{stem}.{code}.{fmt}", fmt=fmt
            )
            for code in codes
            for fmt in formats
        }
        self._source_written: set[int] = set()
        self._target_written: set[tuple[str, int]] = set()

    @property
    def paths(self) -> list[Path]:
        """Every file this recorder will write, whether or not it has yet."""

        return [writer.path for writer in self.writers.values()]

    @property
    def errors(self) -> dict[str, str]:
        """Files that could not be written, by name."""

        return {
            writer.path.name: writer.error
            for writer in self.writers.values()
            if writer.error is not None
        }

    def __call__(self, segment: CueSource) -> None:
        """Record one committed segment. Matches ``LivePipeline.on_segment``."""

        key = (segment.language, segment.seq)
        if key in self._target_written or segment.language not in self.target_codes:
            return
        self._target_written.add(key)
        self._add(segment.language, segment, segment.text)

    def record_source(self, segment: SourceSegment) -> None:
        """Record an ordered source commitment, independently of target success."""
        if segment.seq in self._source_written:
            return
        self._source_written.add(segment.seq)
        self._add("source", segment, segment.text)

    def _add(self, code: str, segment: CueSource | SourceSegment, text: str) -> None:
        if not text:
            return
        cue = Cue(start_ms=segment.start_ms, end_ms=segment.end_ms, text=text)
        for (writer_code, _fmt), writer in self.writers.items():
            if writer_code == code:
                writer.add(cue)

    def close(self) -> None:
        for writer in self.writers.values():
            writer.close()
