"""Reference material for scoring: transcripts, subtitle files, and transcripts from PDFs.

``subtitles.py`` writes cues; this module reads them, so that a session can be scored
against a reference that was produced by someone else. The cue model is shared
rather than redefined, because two cue models that disagree about a timestamp are
worse than one.

Three kinds of reference turn up, and they are not interchangeable:

* **A subtitle file** (``.srt``/``.vtt``) carries text *and* timings, so it can be
  compared cue by cue and per-window.
* **A plain transcript** carries text only. That is enough for ASR scoring - WER is
  computed over the whole recording rather than per cue - and it is the common case
  for a podcast or a lecture, where the transcript is a document rather than a
  subtitled video.
* **A worksheet PDF** is a fourth case: a transcript with a summary paragraph,
  footnotes and soft line wraps glued in, which is what ``clean_worksheet_transcript``
  exists to remove.

Reading is deliberately forgiving about encodings and tags, because reference files
are produced by other people's tools: Windows-1252 subtitle files, byte-order marks,
CRLF, and ``<i>``/``{\\an8}`` markup all occur in the wild, and a harness that
refuses them measures nothing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from livesubs.subtitles import Cue, normalise_text

__all__ = [
    "align_by_order",
    "clean_worksheet_transcript",
    "decode_text",
    "distribute_words",
    "group_by_window",
    "parse_srt",
    "parse_timestamp",
    "parse_vtt",
    "read_cues",
    "read_reference",
    "read_text",
    "strip_markup",
]

#: Reference files arrive from subtitle sites and office suites, so they arrive in
#: whatever encoding those chose. Windows-1252 covers the German umlauts that a
#: UTF-8 decode failure would otherwise mangle into replacement characters.
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

#: ``00:01:02,345``, ``0:01:02.345``, and the ``MM:SS.mmm`` form WebVTT allows.
_TIMESTAMP_RE = re.compile(
    r"(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)

#: Inline markup a player interprets: SubRip's ``<i>``, WebVTT voice/class spans, and
#: the ASS override blocks that leak into ``.srt`` files converted from ``.ass``.
_MARKUP_RE = re.compile(r"</?[a-zA-Z][^>]*>|\{\\[^}]*\}")

#: A worksheet footnote marker: digits stuck to the end of a word. This is
#: deliberately anchored to a letter before the digits and a non-word character
#: after them, because "englishin10minutes.com" is not a footnote and neither is
#: the "10" in "English in 10 Minutes".
_FOOTNOTE_MARKER_RE = re.compile(r"(?<=[A-Za-z])\d{1,2}(?![A-Za-z0-9])")

#: A footnote *definition* at the foot of a worksheet page: ``3 brainpower: ...``, and
#: also ``8 subject matter: ...``, ``1 Prüfer: ...`` and ``3. Intelligenz: ...``.
#:
#: Three shapes have to be accepted, and each was found in a real document rather than
#: imagined: a term of more than one word (``subject matter``), a capitalised term
#: (every German noun), and a full stop after the number. A pattern that missed any of
#: them left the footnote in the transcript, where it is scored as speech nobody gave.
_FOOTNOTE_DEFINITION_RE = re.compile(r"^\d{1,2}[.)]?\s+\w[\w '\-]*:")

#: What the spoken part of a worksheet is headed, in the languages that have turned up.
#: Matched against a whole line, never as a substring: ``Transkript`` appears inside
#: ordinary sentences in both languages ("die Transkripte werden erst im Nachhinein
#: erstellt"), and cutting at the first of those would discard half the transcript.
_TRANSCRIPT_HEADINGS = frozenset({"transcript", "transkript", "transkription", "transcription"})


def decode_text(raw: bytes) -> str:
    """Decode reference bytes, trying the encodings these files actually use."""

    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_text(path: str | Path) -> str:
    """Read a text file as text, newlines normalised so ``\\r\\n`` cannot hide a blank line."""

    text = decode_text(Path(path).read_bytes())
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_markup(text: str) -> str:
    """Remove inline player markup, keeping the words it wrapped."""

    return normalise_text(_MARKUP_RE.sub("", text))


def parse_timestamp(value: str) -> int:
    """``HH:MM:SS,mmm`` (or ``MM:SS.mmm``) to milliseconds."""

    match = _TIMESTAMP_RE.search(value)
    if match is None:
        raise ValueError(f"not a subtitle timestamp: {value!r}")

    hours, minutes, seconds, fraction = match.groups()
    millis = int(fraction.ljust(3, "0")[:3])
    return (
        int(hours or 0) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1000
        + millis
    )



def _parse_cue_blocks(text: str) -> list[Cue]:
    """The shared body of the SubRip and WebVTT parsers.

    One implementation for both, because the two formats differ only in the
    timestamp separator and in what may appear before the timing line. Blocks
    without a timing line are skipped rather than treated as errors: WebVTT's
    ``NOTE``, ``STYLE`` and ``REGION`` blocks are legal and common, and a reference
    file that fails to parse is a dead end.
    """

    cues: list[Cue] = []

    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue

        start_text, _, end_text = lines[timing_index].partition("-->")
        # Everything after the end timestamp is cue settings ("align:start
        # position:10%"), not text.
        end_text = end_text.strip().split(" ")[0]

        try:
            start_ms = parse_timestamp(start_text)
            end_ms = parse_timestamp(end_text)
        except ValueError:
            continue

        body = strip_markup(" ".join(lines[timing_index + 1 :]))
        if not body:
            continue

        cues.append(Cue(start_ms=start_ms, end_ms=max(end_ms, start_ms), text=body))

    return cues


def parse_srt(text: str) -> list[Cue]:
    """Parse SubRip. Index lines and unnumbered cues are both accepted."""

    return _parse_cue_blocks(text)


def parse_vtt(text: str) -> list[Cue]:
    """Parse WebVTT. The ``WEBVTT`` header line is not a cue and is skipped."""

    return _parse_cue_blocks(text)


def read_cues(path: str | Path) -> list[Cue]:
    """Read timed cues from ``.srt`` or ``.vtt``, chosen by extension."""

    target = Path(path)
    suffix = target.suffix.lower()
    text = read_text(target)

    if suffix == ".srt":
        return parse_srt(text)
    if suffix == ".vtt":
        return parse_vtt(text)
    raise ValueError(f"not a subtitle file: {target.name} (expected .srt or .vtt)")


def read_reference(path: str | Path) -> str:
    """Read a reference as plain text, whether it is timed or not.

    For a subtitle file the cue texts are joined in order, because whole-recording
    WER does not care where a sentence was on the timeline - only which words were
    said, and in what order.
    """

    target = Path(path)
    if target.suffix.lower() in (".srt", ".vtt"):
        return " ".join(cue.text for cue in read_cues(target))
    return read_text(target)


def strip_footnote_markers(text: str) -> str:
    """Remove worksheet footnote markers, tidying the space they leave behind.

    ``drills4 .`` becomes ``drills.``: leaving the gap would insert a spurious word
    boundary and cost a hit in WER against every footnote-bearing sentence.
    """

    stripped = _FOOTNOTE_MARKER_RE.sub("", text)
    stripped = re.sub(r"\s+([.,;:!?])", r"\1", stripped)
    return stripped


def _is_footnote_start(line: str) -> bool:
    """Whether a line begins the footnote apparatus at the foot of a page.

    Two shapes: a definition (``3 brainpower: ...``, ``8 subject matter: ...``) and a
    number left on a line of its own by the two-column footnote layout.
    """

    return bool(
        _FOOTNOTE_DEFINITION_RE.match(line) or re.fullmatch(r"\d{1,2}[.)]?", line)
    )


def _after_transcript_heading(lines: Sequence[str]) -> int:
    """Index of the first spoken line, so a caller can drop the front matter.

    Zero when there is no heading, which keeps a document's text rather than emptying
    it: an empty reference scores a perfect word error rate for a session that
    produced nothing at all.
    """

    for index, line in enumerate(lines):
        if line.strip().lower() in _TRANSCRIPT_HEADINGS:
            return index + 1
    return 0


def clean_worksheet_transcript(text: str) -> str:
    """Turn a worksheet PDF's text layer into the words that were actually spoken.

    Shaped by the "English in 10 Minutes" worksheets, and by worksheet PDFs
    generally: a title, a **Summary** paragraph that is *not* spoken - and would
    otherwise be scored as speech the speaker never gave - a **Transcript** heading,
    and footnotes that define vocabulary the speaker used.

    The rule is **per page, and positional**: a page's footnotes start at its first
    footnote line and run to the foot of that page. That is what the layout
    guarantees, and it is the only rule that survives these documents' real shape,
    which defeated three cleverer ones:

    * a footnote's text is soft-wrapped *with a blank line inside it*, so a footnote
      arrives as two blocks and the second one carries no marker at all;
    * a page break can fall between a footnote and the body sentence that resumes
      overleaf, so treating a page break as a space splices a footnote definition onto
      the front of a spoken sentence - which is how a whole sentence came to be
      deleted as if it were a footnote;
    * a definition is not always first in its block, because the wrapped line before it
      belongs to the previous footnote.

    Body text that follows a footnote *on the same page* would be lost by this rule.
    That cannot happen in a footnote layout, and it is the one assumption worth
    checking against a new sample - ``tools/prepare_corpus.py`` prints the word counts
    so a loss is visible rather than silent.

    Two things are deliberately left in. Footnote **markers** that a document sets off
    with spaces rather than gluing to a word (``Prüfer für Englisch 1 beim``) are kept,
    because a marker there is indistinguishable from a spoken numeral - stripping
    ``10`` would destroy "10 Minuten", which the speaker says repeatedly - and a stray
    `` 1 `` moves chrF by a fraction of a percent. Document **headers and footers** are
    removed by the heading cut rather than by pattern, because the machine-translated
    worksheet's watermark ("Abonnieren Sie DeepL Pro ...") shares its wording with a
    sentence the speaker actually says.
    """

    normalised = text.replace("\r\n", "\n").replace("\r", "\n")

    # Everything before the Transcript heading is front matter: the title, the summary
    # that was never spoken, and any watermark a translation service added.
    lines = normalised.split("\n")
    body = "\n".join(lines[_after_transcript_heading(lines) :])

    paragraphs: list[str] = []

    for page in body.split("\f"):
        page_lines = page.split("\n")

        cut = next(
            (
                index
                for index, line in enumerate(page_lines)
                if _is_footnote_start(line.strip())
            ),
            None,
        )
        if cut is not None:
            page_lines = page_lines[:cut]

        for block in re.split(r"\n\s*\n", "\n".join(page_lines)):
            joined = " ".join(line.strip() for line in block.split("\n") if line.strip())
            if joined:
                paragraphs.append(joined)

    cleaned = strip_footnote_markers("\n\n".join(paragraphs))
    return "\n\n".join(
        normalise_text(paragraph) for paragraph in cleaned.split("\n\n") if paragraph.strip()
    )


def distribute_words(words: Sequence[str], cues: Sequence[Cue]) -> list[Cue]:
    """Lay an untimed transcript onto timed spans, in proportion to their length.

    The bridge between the two things a corpus actually provides: a transcript with words
    and no timings, and a recording with timings and no words. The spans come from the audio
    itself - any approach run over the recording writes cues whose boundaries are real
    speech pauses on the audio clock - so a word lands in the *right utterance*.
    Its position within that utterance is approximate, which is enough to rehearse the
    German and the overlay against audio somebody can hear, and not enough to score word
    timings. Callers are expected to say so.

    Leftover words from rounding go to the spans with the largest fractional share, so the
    last words of the transcript are not quietly dropped by a series of floor divisions.
    """

    if not cues:
        raise ValueError("no spans to lay the words onto")
    if not words:
        return []

    lengths = [max(1, cue.end_ms - cue.start_ms) for cue in cues]
    total_ms = sum(lengths)

    shares = [length / total_ms * len(words) for length in lengths]
    counts = [int(share) for share in shares]

    leftover = len(words) - sum(counts)
    if leftover:
        order = sorted(range(len(cues)), key=lambda index: shares[index] - counts[index], reverse=True)
        for index in order[:leftover]:
            counts[index] += 1

    out: list[Cue] = []
    position = 0

    for cue, count in zip(cues, counts, strict=False):
        text = " ".join(words[position : position + count])
        position += count
        if text:
            out.append(Cue(start_ms=cue.start_ms, end_ms=cue.end_ms, text=text))

    return out


def align_by_order(left: Sequence[Cue], right: Sequence[Cue]) -> list[tuple[str, str]]:
    """Pair two subtitle tracks by position, not by timestamp.

    Reference tracks for the same material are frequently timed differently - two
    translations of one film, or a transcript prepared against a different edit - so
    pairing on timestamps silently matches nothing and pairs on order gets it right.
    The shorter track decides the length; the surplus is dropped rather than paired
    with a blank, because an unmatched cue would score as a total mistranslation.
    """

    return [(a.text, b.text) for a, b in zip(left, right, strict=False)]


def group_by_window(
    windows: Sequence[Cue], fragments: Sequence[Cue], *, slack_ms: int = 200
) -> tuple[list[list[Cue]], list[Cue]]:
    """Assign each fragment to the reference window it overlaps most.

    This is the comparison that makes a real-time run scorable against subtitles.
    The system emits two-second clause fragments and a reference track carries
    whole sentences, so comparing fragment to cue would punish the fragmentation
    that ``docs/03-segmentation.md`` chose on purpose. Reassembling our fragments
    into the reference's own windows removes that bias; what is left is translation
    quality.

    ``slack_ms`` absorbs the small disagreement between our audio clock and the
    reference's timings, which is systematic rather than random and would otherwise
    push boundary fragments into the wrong window.

    Returns the groups in window order, plus the fragments that fell outside every
    window so that coverage is visible rather than assumed.
    """

    groups: list[list[Cue]] = [[] for _ in windows]
    unmatched: list[Cue] = []

    for fragment in fragments:
        best_index: int | None = None
        best_overlap = 0

        for index, window in enumerate(windows):
            overlap = min(fragment.end_ms, window.end_ms + slack_ms) - max(
                fragment.start_ms, window.start_ms - slack_ms
            )
            if overlap > best_overlap:
                best_overlap = overlap
                best_index = index

        if best_index is None:
            unmatched.append(fragment)
        else:
            groups[best_index].append(fragment)

    return groups, unmatched
