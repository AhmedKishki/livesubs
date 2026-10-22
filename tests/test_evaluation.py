"""Tests for the scoring metrics.

These are the numbers a provider comparison is decided on, so the assertions are
mostly about the two ways a metric lies: counting an error that did not happen, and
reporting a perfect score for an empty measurement.
"""

from __future__ import annotations

import pytest

from livesubs.evaluation import (
    SessionReport,
    Turn,
    check_languages,
    chrf_corpus,
    chrf_sentence,
    guess_language,
    identical_to_source_fraction,
    length_ratio,
    normalise,
    number_preservation,
    numbers,
    percentiles,
    tokenise,
    wer,
    wer_on_numbers,
)
from livesubs.subtitles import Cue

# --- normalisation and tokenising -------------------------------------------


def test_normalise_folds_case_and_punctuation() -> None:
    assert normalise("Hello,  World!") == "hello world"


def test_normalise_folds_the_typographic_variants_a_pdf_inserts() -> None:
    """A PDF and a model disagree about apostrophes; that must not be an error."""

    assert normalise("I\u2019m here \u2014 now") == normalise("I'm here - now")


def test_normalise_keeps_digits() -> None:
    """Numbers are the error class that matters most, so they must stay visible."""

    assert normalise("40 to 50%") == "40 to 50"


def test_tokenise_keeps_an_internal_apostrophe_or_hyphen_intact() -> None:
    tokens = tokenise("Don't stop the 10-minute talk.")

    assert tokens == ["don't", "stop", "the", "10-minute", "talk"]


def test_numbers_finds_the_awkward_shapes() -> None:
    assert numbers("3.5 kg, 1,200 people, 40-50%, at 12:30") == [
        "3.5",
        "1,200",
        "40-50",
        "12:30",
    ]


# --- WER --------------------------------------------------------------------


def test_identical_text_scores_zero_errors() -> None:
    alignment = wer(tokenise("one two three"), tokenise("one two three"))

    assert alignment.wer == 0.0
    assert alignment.accuracy == 1.0


def test_a_substitution_is_counted_as_one_error() -> None:
    alignment = wer(tokenise("one two three"), tokenise("one X three"))

    assert (alignment.substitutions, alignment.deletions, alignment.insertions) == (1, 0, 0)
    assert alignment.wer == pytest.approx(1 / 3)


def test_a_missing_word_is_a_deletion_and_an_extra_word_is_an_insertion() -> None:
    deleted = wer(tokenise("one two three"), tokenise("one three"))
    inserted = wer(tokenise("one two three"), tokenise("one two three four"))

    assert (deleted.deletions, deleted.insertions) == (1, 0)
    assert (inserted.deletions, inserted.insertions) == (0, 1)


def test_case_and_punctuation_do_not_count_as_errors() -> None:
    """Otherwise a transcriber's style would be scored as an ASR mistake."""

    assert wer(tokenise("Hello, world."), tokenise("hello world")).wer == 0.0


def test_wer_measures_whole_words_not_characters() -> None:
    alignment = wer(tokenise("a b c d"), tokenise("a x c y"))

    assert alignment.wer == pytest.approx(0.5)


def test_a_completely_wrong_hypothesis_scores_one_hundred_percent() -> None:
    """The stub adapter's realistic worst case: no words in common."""

    alignment = wer(tokenise("one two three"), tokenise("alpha beta gamma"))

    assert alignment.wer == 1.0
    assert alignment.hits == 0


def test_an_empty_hypothesis_is_all_deletions() -> None:
    alignment = wer(tokenise("one two three"), [])

    assert (alignment.deletions, alignment.wer) == (3, 1.0)


def test_an_empty_reference_raises_rather_than_scoring_perfectly() -> None:
    """A zero-scoring empty reference would hide a corpus that failed to load."""

    with pytest.raises(ValueError):
        wer([], tokenise("anything"))


def test_alignment_counts_always_add_up() -> None:
    reference = tokenise("the quick brown fox jumps over the lazy dog")
    hypothesis = tokenise("the quick brown fox jumped over a lazy dog")

    alignment = wer(reference, hypothesis)

    assert alignment.hits + alignment.substitutions + alignment.deletions == alignment.reference
    assert alignment.errors == (
        alignment.substitutions + alignment.deletions + alignment.insertions
    )


def test_wer_on_numbers_scores_only_the_numerals() -> None:
    """The words can be right and the numbers wrong, which is what a viewer notices."""

    alignment = wer_on_numbers(
        "It is 40 to 50% of the 90 minutes.",
        "Es sind 40 bis 50% der 60 Minuten.",
    )

    assert alignment is not None
    assert (alignment.reference, alignment.substitutions) == (3, 1)


def test_wer_on_numbers_is_none_when_the_reference_has_no_numbers() -> None:
    assert wer_on_numbers("no digits here", "keine Ziffern") is None


# --- chrF -------------------------------------------------------------------


def test_chrf_is_one_for_an_identical_sentence() -> None:
    assert chrf_sentence("Der Mann liest ein Buch.", "Der Mann liest ein Buch.") == 1.0


def test_chrf_is_zero_for_an_empty_translation() -> None:
    assert chrf_sentence("Der Mann liest ein Buch.", "") == 0.0


def test_chrf_is_between_zero_and_one_and_higher_for_closer_text() -> None:
    reference = "Der Mann liest ein Buch."
    close = chrf_sentence(reference, "Der Mann liest ein gutes Buch.")
    far = chrf_sentence(reference, "Ganz andere Worte stehen hier.")

    assert 0.0 < far < close < 1.0


def test_chrf_preserves_case_because_german_capitalises_nouns() -> None:
    """Lowercasing would score a correct noun and a wrong one identically."""

    right = chrf_sentence("Der Mann liest ein Buch.", "Der Mann liest ein Buch.")
    wrong_case = chrf_sentence("Der Mann liest ein Buch.", "der mann liest ein buch.")

    assert right > wrong_case


def test_chrf_corpus_averages_sentences_and_reports_none_when_empty() -> None:
    assert chrf_corpus([]) is None

    score = chrf_corpus([("Hallo.", "Hallo."), ("Welt.", "Welt.")])
    assert score == 1.0


# --- reference-free checks --------------------------------------------------


def test_number_preservation_counts_a_dropped_number() -> None:
    report = number_preservation([("It rose 40% in 1999.", "Er stieg im Jahr 1999.")])

    assert report.total == 2
    assert report.kept == 1
    assert report.adherence == pytest.approx(0.5)
    assert report.misses[0][0] == "40"


def test_number_preservation_accepts_the_german_number_formats() -> None:
    """``40 %`` and ``1.000`` are the same numbers as ``40%`` and ``1,000``."""

    report = number_preservation([("40 to 50% of 1,000 people", "40 bis 50 % von 1.000 Menschen")])

    assert report.total == 3
    assert report.kept == 3


def test_number_preservation_still_catches_a_two_thousand_becoming_one() -> None:
    """The thousands separator must not make a wrong number look right.

    ``1,200`` is twelve hundred and ``1.000`` is one thousand, so a translation
    that writes one for the other has changed the number.
    """

    report = number_preservation([("1,200 people", "1.000 Menschen")])

    assert report.kept == 0


def test_number_preservation_does_not_match_digits_across_two_numbers() -> None:
    """Otherwise "45" passes against a target containing "4 apples and 5"."""

    report = number_preservation([("Look at 45 now", "Sieh dir 4 Äpfel und 5 an")])

    assert report.kept == 0
    assert report.misses[0][0] == "45"


def test_number_report_of_no_numbers_is_perfect_not_undefined() -> None:
    report = number_preservation([("no digits", "keine Ziffern")])

    assert report.total == 0
    assert report.adherence == 1.0


def test_identical_to_source_fraction_catches_a_passthrough() -> None:
    """The failure that looks like success: confident text in the wrong language."""

    pairs = [("Hello there.", "Hello there."), ("How are you?", "Wie geht es dir?")]

    assert identical_to_source_fraction(pairs) == pytest.approx(0.5)


def test_identical_to_source_fraction_is_none_without_any_text() -> None:
    assert identical_to_source_fraction([("", "")]) is None


def test_length_ratio_counts_german_characters_per_english_word() -> None:
    """The number that decides whether the overlay's line assumptions hold."""

    assert length_ratio([("the man", "der Mann")]) == pytest.approx(len("dermann") / 2)


# --- latency ----------------------------------------------------------------


def test_percentiles_use_nearest_rank_rather_than_interpolating() -> None:
    """Interpolating invents a latency that was never observed."""

    values = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]

    assert percentiles(values)["p50"] == 500
    assert percentiles(values)["p95"] == 1000


def test_percentiles_of_nothing_is_empty_not_zero() -> None:
    assert percentiles([]) == {}


def test_percentiles_always_report_the_maximum() -> None:
    """One eight-second freeze matters more than the mean."""

    assert percentiles([10, 10, 8000])["max"] == 8000


# --- the report itself ------------------------------------------------------


def test_report_distinguishes_missing_from_zero() -> None:
    """A run with no ASR numbers must not look like a run with perfect ones."""

    report = SessionReport(reference_words=100)

    assert report.as_dict()["asr"] is None
    assert report.as_dict()["chrf"] is None
    assert "asr wer" not in report.format_text()


def test_report_renders_the_numbers_it_was_given() -> None:
    report = SessionReport(
        reference_words=3,
        asr=wer(tokenise("one two three"), tokenise("one X three")),
        numbers=number_preservation([("It rose 40%.", "Er stieg.")]),
        segments=2,
    )
    text = report.format_text()

    assert "asr wer : 33.3%" in text
    assert "numbers : 0.0% kept (0/1)" in text
    assert "segments : 2" in text
    assert "lost '40'" in text


# --- a conversation: which language each line is in ---------------------------------

def test_english_german_and_arabic_lines_are_told_apart() -> None:
    assert guess_language("The amplitude decreases over time, and that is not small.") == "en"
    assert guess_language("Die Amplitude nimmt mit der Zeit ab, und das ist nicht wenig.") == "de"
    assert guess_language("تتناقص السعة مع مرور الوقت.") == "ar"


def test_a_line_too_short_to_tell_is_not_guessed() -> None:
    assert guess_language("Okay.") is None
    assert guess_language("Gregor Samsa") is None


TURNS = [Turn("en", 0, 10_000), Turn("de", 10_400, 20_000), Turn("en", 20_400, 24_000)]


def test_a_target_line_in_the_wrong_language_is_reported() -> None:
    cues = [
        Cue(1_000, 3_000, "He woke up and he was not a man."),
        Cue(11_000, 13_000, "Er wachte auf und er war nicht mehr ein Mensch."),
        Cue(21_000, 23_000, "It was not a dream, and he knew that."),
    ]

    check = check_languages("en", cues, TURNS, expected="en")

    assert check.cues == 3
    assert [(cue.start_ms, guessed) for cue, _, guessed in check.wrong] == [(11_000, "de")]
    assert check.silent_turns == []


def test_a_transcript_line_is_held_to_the_language_of_its_turn() -> None:
    """The transcript of a conversation is right when it follows the speakers."""

    cues = [
        Cue(1_000, 3_000, "He woke up and he was not a man."),
        Cue(11_000, 13_000, "He woke up and it was not a dream."),
    ]

    check = check_languages("en+de", cues, TURNS, expected=None)

    assert [(cue.start_ms, expected) for cue, expected, _ in check.wrong] == [(11_000, "de")]


def test_a_turn_with_no_subtitle_is_reported() -> None:
    cues = [Cue(1_000, 3_000, "He woke up and he was not a man.")]

    check = check_languages("en", cues, TURNS, expected="en")

    assert check.silent_turns == [1, 2]
