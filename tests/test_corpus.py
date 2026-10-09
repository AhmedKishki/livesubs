"""Tests for reading and cleaning reference material.

The worksheet fixtures here are deliberately small but *shaped like the real thing*,
because every rule in ``clean_worksheet_transcript`` exists because of a specific
awkwardness in a real PDF: a footnote soft-wrapped with a blank line inside it, a
definition that is not first in its block, and a page break that falls between a
footnote and the spoken sentence that resumes overleaf. A tidy fixture would pass
against a cleaner that cannot read the real document, which is the failure mode these
tests exist to prevent.
"""

from __future__ import annotations

import pytest

from livesubs.corpus import (
    align_by_order,
    clean_worksheet_transcript,
    decode_text,
    distribute_words,
    group_by_window,
    parse_srt,
    parse_timestamp,
    parse_vtt,
    read_reference,
    strip_footnote_markers,
    strip_markup,
)
from livesubs.subtitles import Cue

# --- timestamps --------------------------------------------------------------


def test_srt_timestamps_use_a_comma() -> None:
    assert parse_timestamp("00:01:02,345") == 62_345


def test_webvtt_timestamps_use_a_dot() -> None:
    assert parse_timestamp("00:01:02.345") == 62_345


def test_hours_are_optional_and_milliseconds_are_padded() -> None:
    assert parse_timestamp("01:02.5") == 62_500
    assert parse_timestamp("01:02.05") == 62_050


def test_a_non_timestamp_is_an_error_not_a_zero() -> None:
    """A silent zero would place a cue at the start of the recording."""

    with pytest.raises(ValueError):
        parse_timestamp("not a time")


# --- subtitle parsing -------------------------------------------------------


def test_parse_srt_reads_index_timing_and_text() -> None:
    cues = parse_srt(
        "1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n"
        "2\n00:00:03,500 --> 00:00:05,000\nSecond line.\n"
    )
    assert [cue.text for cue in cues] == ["Hello there.", "Second line."]
    assert (cues[0].start_ms, cues[0].end_ms) == (1000, 3000)


def test_parse_srt_accepts_unnumbered_cues() -> None:
    cues = parse_srt("00:00:01,000 --> 00:00:02,000\nUnnumbered.\n")
    assert [cue.text for cue in cues] == ["Unnumbered."]


def test_parse_srt_joins_multiline_cue_text() -> None:
    cues = parse_srt("00:00:01,000 --> 00:00:02,000\nfirst half\nsecond half\n")
    assert cues[0].text == "first half second half"


def test_parse_srt_strips_inline_markup_but_keeps_words() -> None:
    cues = parse_srt(
        "00:00:01,000 --> 00:00:02,000\n<i>italic</i> and {\\an8}positioned\n"
    )
    assert cues[0].text == "italic and positioned"


def test_parse_srt_tolerates_a_byte_order_mark_and_crlf() -> None:
    """Files saved by Windows tools arrive with both, and both break naive parsing."""

    parsed = parse_srt("\ufeff1\r\n00:00:01,000 --> 00:00:02,000\r\nHallo.\r\n")

    assert [cue.text for cue in parsed] == ["Hallo."]


def test_read_reference_reads_a_plain_transcript(tmp_path) -> None:
    path = tmp_path / "reference.en.txt"
    path.write_text("One two three.\n", encoding="utf-8")

    assert read_reference(path) == "One two three.\n"


def test_read_reference_joins_cue_text_for_a_subtitle_file(tmp_path) -> None:
    path = tmp_path / "reference.en.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nOne.\n\n"
                    "2\n00:00:03,000 --> 00:00:04,000\nTwo.\n", encoding="utf-8")

    assert read_reference(path) == "One. Two."


def test_read_reference_reads_a_cp1252_subtitle_file(tmp_path) -> None:
    path = tmp_path / "reference.de.srt"
    path.write_bytes("1\n00:00:01,000 --> 00:00:02,000\nGrüße.\n".encode("cp1252"))

    assert read_reference(path) == "Grüße."


def test_parse_srt_ignores_a_zero_length_cue() -> None:
    """Playable files contain them; a cue with no text would score as a deletion."""

    cues = parse_srt("1\n00:00:01,000 --> 00:00:02,000\n\n\n2\n00:00:03,000 --> 00:00:04,000\nText.\n")
    assert [cue.text for cue in cues] == ["Text."]


def test_parse_vtt_skips_the_header_and_note_blocks() -> None:
    cues = parse_vtt(
        "WEBVTT\n\nNOTE this is a comment\nstill the comment\n\n"
        "00:00:01.000 --> 00:00:02.000 align:start position:10%\nHallo.\n"
    )
    assert [cue.text for cue in cues] == ["Hallo."]


def test_cue_settings_are_not_part_of_the_text() -> None:
    cues = parse_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nNur Text.\n")
    assert cues[0].text == "Nur Text."


# --- encodings --------------------------------------------------------------


def test_cp1252_umlauts_survive_decoding() -> None:
    """Subtitle files from Windows tools are routinely not UTF-8."""

    assert decode_text("Grüße".encode("cp1252")) == "Grüße"


def test_utf8_wins_when_it_is_valid() -> None:
    assert decode_text("Grüße".encode()) == "Grüße"


def test_strip_markup_leaves_a_bare_line_alone() -> None:
    assert strip_markup("Hallo Welt") == "Hallo Welt"


# --- worksheet transcripts ---------------------------------------------------

#: Shaped like a real worksheet: a summary that is not spoken, footnote markers stuck
#: to words, a footnote whose text is wrapped *with a blank line inside it*, a
#: definition that starts mid-block behind the previous footnote's wrapped line, a
#: bare footnote number on its own line, a URL full of digits that must survive, and a
#: page break placed between a footnote and the spoken sentence continuing overleaf.
WORKSHEET = (
    "Episode 0: Introduction\n"
    "Summary\n"
    "\n"
    "A summary that is not spoken and must never be scored as speech.\n"
    "\n"
    "Transcript\n"
    "\n"
    "Hello and welcome. My name is Nick Leonard1 and I teach.\n"
    "\n"
    "You know, 10 minutes is not long, and 40 to 50% of it is listening.\n"
    "\n"
    "\n"
    "1 examiner: a teacher who tests\n"
    "\n"
    "\n"
    "2 listening: what you do with your ears\n"
    "\f"
    "Today I will talk about the podcast2, and about real unscripted3 talk.\n"
    "\n"
    "See www.englishin10minutes.com for the worksheets.\n"
    "\n"
    "\n"
    "3 unscripted: without a script. The talk is\n"
    "\n"
    "natural and the notes are made afterwards.\n"
    "4 notes: written records, like\n"
    "\n"
    "a summary of what was said.\n"
    "\n"
    "\n"
    "5\n"
    "\f"
    "That is all, and thanks for listening.\n"
)


def test_worksheet_keeps_every_spoken_sentence() -> None:
    cleaned = clean_worksheet_transcript(WORKSHEET)

    assert "Hello and welcome. My name is Nick Leonard and I teach." in cleaned
    assert "You know, 10 minutes is not long, and 40 to 50% of it is listening." in cleaned
    assert "Today I will talk about the podcast, and about real unscripted talk." in cleaned
    assert "See www.englishin10minutes.com for the worksheets." in cleaned
    assert "That is all, and thanks for listening." in cleaned


def test_worksheet_drops_the_summary_which_was_never_spoken() -> None:
    cleaned = clean_worksheet_transcript(WORKSHEET)

    assert "A summary that is not spoken" not in cleaned
    assert "Episode 0: Introduction" not in cleaned


def test_worksheet_drops_every_footnote_definition() -> None:
    cleaned = clean_worksheet_transcript(WORKSHEET)

    for definition in (
        "examiner: a teacher who tests",
        "listening: what you do",
        "unscripted: without a script",
        "notes: written records",
        "a summary of what was said",
    ):
        assert definition not in cleaned


def test_a_capitalised_footnote_term_is_dropped_too() -> None:
    """The English worksheet writes lowercase terms; the German one cannot.

    Every German noun is capitalised, so a pattern accepting only lowercase terms left
    four of the ten German footnotes in the transcript, where they are scored as speech
    nobody gave. The risk on the other side - a spoken line shaped like a definition -
    is pinned by the test below.
    """

    cleaned = clean_worksheet_transcript(
        "Transcript\n\nSpoken sentence one.\n\n1 Examiner: a definition nobody spoke.\n"
    )

    assert "Spoken sentence one." in cleaned
    assert "a definition nobody spoke" not in cleaned


def test_worksheet_drops_a_footnote_continuation_with_no_marker_on_it() -> None:
    """The wrapped line after a blank line is a footnote, not a spoken sentence."""

    cleaned = clean_worksheet_transcript(WORKSHEET)

    assert "natural and the notes are made afterwards" not in cleaned


def test_worksheet_keeps_body_text_that_follows_a_page_break() -> None:
    """A page break after a footnote must not let the footnote eat the next sentence."""

    cleaned = clean_worksheet_transcript(WORKSHEET)

    assert "Today I will talk about the podcast" in cleaned
    assert "That is all, and thanks for listening." in cleaned


def test_worksheet_strips_markers_without_touching_numbers_or_urls() -> None:
    cleaned = clean_worksheet_transcript(WORKSHEET)

    assert "Leonard1" not in cleaned
    assert "podcast2" not in cleaned
    assert "unscripted3" not in cleaned
    assert "10 minutes" in cleaned
    assert "40 to 50%" in cleaned
    assert "englishin10minutes.com" in cleaned


def test_worksheet_without_a_transcript_heading_keeps_its_text() -> None:
    """Rather than emptying itself, which would score a perfect WER."""

    cleaned = clean_worksheet_transcript("Just a sentence1 with a marker.\n")

    assert "Just a sentence with a marker." in cleaned


# --- the German translation of the same worksheet ----------------------------

#: The German document differs from the English one in four ways that each broke a rule
#: the English one had established: a translation service's watermark above the title,
#: a ``Transkript`` heading rather than ``Transcript``, footnote terms capitalised the
#: way German nouns are, and a full stop after the footnote number (``3. Intelligenz:``).
GERMAN_WORKSHEET = (
    "            Abonnieren Sie DeepL Pro, um größere Dateien zu übersetzen.\n"
    "            Weitere Informationen finden Sie unter www.DeepL.com/pro\n"
    "\n"
    "\n"
    "              Folge 0: Einführung\n"
    "Zusammenfassung\n"
    "\n"
    "In dieser Einführungsfolge spricht Moderator Nick Leonard darüber, was die Hörer\n"
    "erwarten können.\n"
    "\n"
    "Transkript\n"
    "\n"
    "Hallo und herzlich willkommen. Mein Name ist Nick Leonard und ich unterrichte\n"
    "Englisch als Fremdsprache, außerdem bin ich Prüfer für Englisch 1.\n"
    "\n"
    "Wisst ihr, 10 Minuten sind keine lange Zeit, und 40 bis 50 % davon ist Zuhören.\n"
    "\n"
    "\n"
    "1 Prüfer: jemand, der Tests durchführt oder Prüfungen abnimmt\n"
    "\n"
    "\n"
    "2 Pendeln: der tägliche Weg zur Arbeit und zurück\n"
    "\f"
    "Ausspracheübungen 3. Nein, ich spreche ganz einfach von einer Sache: Zuhören.\n"
    "\n"
    "Die Adresse lautet www.englishin10minutes.com.\n"
    "\n"
    "\n"
    "3. Intelligenz: Einsatz des Verstandes,\n"
    "\n"
    "Denken\n"
    "\f"
    "Das war eine kurze Einführung, und viel Spaß beim Zuhören.\n"
)


def test_german_worksheet_keeps_every_spoken_sentence() -> None:
    cleaned = clean_worksheet_transcript(GERMAN_WORKSHEET)

    assert "Hallo und herzlich willkommen. Mein Name ist Nick Leonard" in cleaned
    assert "Prüfer für Englisch 1." in cleaned
    assert "Wisst ihr, 10 Minuten sind keine lange Zeit, und 40 bis 50 % davon ist Zuhören." in cleaned
    assert "Ausspracheübungen 3. Nein, ich spreche ganz einfach von einer Sache: Zuhören." in cleaned
    assert "Die Adresse lautet www.englishin10minutes.com." in cleaned
    assert "Das war eine kurze Einführung, und viel Spaß beim Zuhören." in cleaned


def test_german_worksheet_drops_the_translation_service_watermark() -> None:
    """Removed by the heading cut, not by pattern: the speaker says "Abonnieren Sie ..."
    himself later in the transcript, so a pattern on that wording would delete speech."""

    cleaned = clean_worksheet_transcript(GERMAN_WORKSHEET)

    assert "DeepL" not in cleaned
    assert "größere Dateien" not in cleaned
    assert "Folge 0" not in cleaned
    assert "In dieser Einführungsfolge" not in cleaned


def test_german_worksheet_drops_capitalised_and_full_stop_footnotes() -> None:
    """Every German noun is capitalised, and this document writes ``3.`` with a stop."""

    cleaned = clean_worksheet_transcript(GERMAN_WORKSHEET)

    for definition in ("Prüfer: jemand", "Pendeln: der tägliche", "Intelligenz:", "Denken"):
        assert definition not in cleaned


def test_a_spoken_heading_word_inside_a_sentence_is_not_a_heading() -> None:
    """``Transkript`` occurs inside ordinary sentences; only a whole line is a heading."""

    cleaned = clean_worksheet_transcript(
        "Transkript\n\nUnd natürlich verbessern sich die Transkripte beim Lesen.\n"
    )

    assert "Und natürlich verbessern sich die Transkripte beim Lesen." in cleaned


def test_a_number_at_the_start_of_a_sentence_is_not_a_footnote_without_a_colon() -> None:
    """The boundary of the positional rule, pinned so it cannot move unnoticed.

    A line beginning ``10 Minuten, ...`` is speech. A line beginning ``10 wort:`` would
    be read as a footnote and would truncate its page - the one residual risk of the
    rule, which no page of either real document triggers, and which the word counts
    printed by ``tools/prepare_corpus.py`` keep visible.
    """

    cleaned = clean_worksheet_transcript(
        "Transkript\n\n10 Minuten, 40 bis 50 % davon, sind keine lange Zeit.\n"
    )

    assert "10 Minuten, 40 bis 50 % davon, sind keine lange Zeit." in cleaned


def test_space_separated_footnote_markers_are_left_alone() -> None:
    """Deliberate: ``Prüfer für Englisch 1`` and ``10 Minuten`` are the same shape.

    Stripping the marker would delete the spoken number, and a stray ``1`` costs chrF a
    fraction of a percent, so the marker stays.
    """

    cleaned = clean_worksheet_transcript(
        "Transkript\n\nIch bin Prüfer für Englisch 1 und rede 10 Minuten lang.\n"
    )

    assert "Prüfer für Englisch 1 und rede 10 Minuten lang." in cleaned


def test_strip_footnote_markers_closes_the_gap_before_punctuation() -> None:
    assert strip_footnote_markers("pronunciation drills4 . No") == "pronunciation drills. No"


def test_strip_footnote_markers_leaves_a_digit_inside_a_word_alone() -> None:
    assert strip_footnote_markers("in10minutes") == "in10minutes"


# --- aligning two references -------------------------------------------------


def test_distribute_words_keeps_every_word_and_preserves_order() -> None:
    """Words are never dropped by a series of floor divisions, and never reordered."""

    spans = [Cue(0, 1000, ""), Cue(1000, 2000, ""), Cue(2000, 3000, "")]
    words = [f"w{index}" for index in range(10)]

    timed = distribute_words(words, spans)

    assert " ".join(cue.text for cue in timed).split() == words


def test_distribute_words_gives_a_longer_span_more_words() -> None:
    spans = [Cue(0, 1000, ""), Cue(1000, 5000, "")]
    words = [f"w{index}" for index in range(10)]

    timed = distribute_words(words, spans)

    assert len(timed[1].text.split()) > len(timed[0].text.split())


def test_distribute_words_keeps_the_spans_it_was_given() -> None:
    """The timings come from the audio, so they must survive untouched."""

    spans = [Cue(500, 1500, ""), Cue(2000, 4000, "")]

    timed = distribute_words(["a", "b", "c"], spans)

    assert [(cue.start_ms, cue.end_ms) for cue in timed] == [(500, 1500), (2000, 4000)]


def test_distribute_words_without_spans_is_an_error_not_a_silent_pass() -> None:
    with pytest.raises(ValueError):
        distribute_words(["a"], [])


def test_align_by_order_pairs_by_position_not_time() -> None:
    """Two tracks for one recording are rarely timed identically."""

    english = [Cue(0, 1000, "one"), Cue(5000, 6000, "two"), Cue(9000, 10_000, "three")]
    german = [Cue(0, 1200, "eins"), Cue(5100, 6200, "zwei")]

    assert align_by_order(english, german) == [("one", "eins"), ("two", "zwei")]


def test_group_by_window_reassembles_fragments_into_reference_sentences() -> None:
    """The point: a 2 s fragment policy is not penalised against sentence cues."""

    windows = [Cue(0, 4000, "a whole sentence"), Cue(4000, 8000, "another one")]
    fragments = [
        Cue(0, 2000, "a whole"),
        Cue(2000, 4000, "sentence"),
        Cue(4000, 6000, "another"),
    ]

    groups, unmatched = group_by_window(windows, fragments)

    assert [[c.text for c in group] for group in groups] == [["a whole", "sentence"], ["another"]]
    assert unmatched == []


def test_group_by_window_reports_fragments_outside_every_window() -> None:
    windows = [Cue(0, 2000, "only")]
    fragments = [Cue(0, 1000, "in"), Cue(60_000, 61_000, "far away")]

    groups, unmatched = group_by_window(windows, fragments)

    assert [[c.text for c in group] for group in groups] == [["in"]]
    assert [c.text for c in unmatched] == ["far away"]


def test_group_by_window_assigns_to_the_largest_overlap() -> None:
    """A fragment straddling two sentences belongs to the one it mostly covers."""

    windows = [Cue(0, 2000, "first"), Cue(2000, 4000, "second")]
    fragments = [Cue(1300, 2200, "straddles")]

    groups, _ = group_by_window(windows, fragments)

    assert [[c.text for c in group] for group in groups] == [["straddles"], []]
