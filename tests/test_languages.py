"""Languages and modes: the one place that knows which pairings exist."""

from __future__ import annotations

import pytest

from livesubs.languages import LANGUAGES, MODES, language, mode_for


def test_an_english_speaker_reaches_german_by_default_and_arabic_on_request() -> None:
    mode = mode_for("en")

    assert mode.source == "en"
    assert mode.targets == ("de", "ar")
    assert mode.default_target == "de"


def test_a_german_speaker_reaches_english_by_default_and_arabic_on_request() -> None:
    mode = mode_for("de")

    assert mode.source == "de"
    assert mode.targets == ("en", "ar")
    assert mode.default_target == "en"


def test_a_speaker_is_never_offered_their_own_language() -> None:
    for mode in MODES.values():
        assert mode.source not in mode.targets


def test_every_language_a_mode_mentions_is_registered() -> None:
    for mode in MODES.values():
        for code in (mode.source, *mode.targets):
            assert code in LANGUAGES


def test_a_mode_can_be_narrowed_to_some_of_its_targets() -> None:
    assert mode_for("en", ["de"]).targets == ("de",)
    assert mode_for("en", ["ar", "de"]).targets == ("ar", "de")
    assert mode_for("en", ["ar", "de"]).default_target == "ar"


def test_narrowing_to_nothing_leaves_the_mode_alone() -> None:
    assert mode_for("en", []) == MODES["en"]


def test_narrowing_ignores_a_repeated_target() -> None:
    assert mode_for("en", ["de", "de"]).targets == ("de",)


def test_a_mode_cannot_be_widened() -> None:
    """No pairing is supported that the mode does not list."""

    with pytest.raises(ValueError, match="cannot be a target"):
        mode_for("en", ["de", "en"])
    with pytest.raises(ValueError, match="cannot be a target"):
        mode_for("de", ["de"])


def test_an_unsupported_speaker_language_is_refused_with_the_alternatives() -> None:
    with pytest.raises(ValueError, match="de, en"):
        mode_for("fr")


def test_an_unknown_language_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown language"):
        language("xx")


def test_arabic_is_right_to_left_and_the_others_are_not() -> None:
    assert language("ar").direction == "rtl"
    assert language("de").direction == "ltr"
    assert language("en").direction == "ltr"


def test_a_label_names_both_sides() -> None:
    assert mode_for("en").label == "English speaker, German or Arabic subtitles"
    assert mode_for("de").label == "German speaker, English or Arabic subtitles"


def test_the_picker_shows_each_language_in_itself() -> None:
    assert language("de").native == "Deutsch"
    assert language("ar").native == "العربية"
