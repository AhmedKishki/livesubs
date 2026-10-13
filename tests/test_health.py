"""Health tells each scope what is wrong for it, and only when that changes."""

from __future__ import annotations

from livesubs.events import PipelineStatus
from livesubs.pipeline.health import Health

LIVE, DELAYED = PipelineStatus.LIVE, PipelineStatus.DELAYED


def make() -> tuple[Health, list[tuple[PipelineStatus, str | None, str | None]]]:
    published: list[tuple[PipelineStatus, str | None, str | None]] = []
    return Health(lambda status, detail, scope: published.append((status, detail, scope))), published


def test_a_problem_makes_its_scope_delayed_with_its_detail() -> None:
    health, published = make()

    health.problem("capture", "the microphone stalled")

    assert published == [(DELAYED, "the microphone stalled", None)]
    assert health.is_open("capture")


def test_clearing_the_last_problem_makes_the_scope_live_again() -> None:
    health, published = make()
    health.problem("capture", "the microphone stalled")

    health.clear("capture")

    assert published[-1] == (LIVE, None, None)
    assert not health.is_open("capture")


def test_the_detail_of_the_first_open_problem_is_shown_until_it_closes() -> None:
    health, published = make()
    health.problem("capture", "the microphone stalled")
    health.problem("asr", "the recogniser dropped")

    # The second problem does not change what the scope says, so it says nothing new.
    assert published == [(DELAYED, "the microphone stalled", None)]

    health.clear("capture")
    assert published[-1] == (DELAYED, "the recogniser dropped", None)

    health.clear("asr")
    assert published[-1] == (LIVE, None, None)


def test_an_unchanged_state_is_not_published_again() -> None:
    health, published = make()

    health.problem("lag", "behind")
    health.problem("lag", "behind")
    health.clear("lag")
    health.clear("lag")
    health.clear("never-opened")

    assert published == [(DELAYED, "behind", None), (LIVE, None, None)]


def test_a_detail_that_changes_is_published() -> None:
    health, published = make()

    health.problem("lag", "3 segments behind")
    health.problem("lag", "5 segments behind")

    assert [detail for _, detail, _ in published] == ["3 segments behind", "5 segments behind"]


def test_a_languages_problem_is_news_for_that_language_only() -> None:
    health, published = make()

    health.problem("translator", "the translator is down", scope="ar")

    assert published == [(DELAYED, "the translator is down", "ar")]
    # The room as a whole, and the other language, are untouched.
    health.problem("capture", "the microphone stalled")
    health.clear("capture")
    assert (LIVE, None, None) in published
    assert not any(scope == "de" for _, _, scope in published)
    assert health.is_open("translator", scope="ar")
    assert not health.is_open("translator", scope="de")
    assert not health.is_open("translator")


def test_the_same_key_in_two_scopes_is_two_problems() -> None:
    health, published = make()
    health.problem("translator", "down", scope="de")
    health.problem("translator", "down", scope="ar")

    health.clear("translator", scope="de")

    assert health.is_open("translator", scope="ar")
    assert published[-1] == (LIVE, None, "de")


def test_clearing_a_scope_closes_everything_open_in_it() -> None:
    """A language whose last reader left must not keep reporting a problem to nobody."""

    health, published = make()
    health.problem("translator", "down", scope="ar")
    health.problem("lag", "behind", scope="ar")

    health.clear_scope("ar")

    assert published[-1] == (LIVE, None, "ar")
    assert not health.is_open("translator", scope="ar")
    assert not health.is_open("lag", scope="ar")


def test_clearing_a_scope_with_nothing_open_says_nothing() -> None:
    health, published = make()

    health.clear_scope("ar")

    assert published == []


def test_a_note_rides_on_the_live_status_that_follows_a_recovery() -> None:
    health, published = make()
    health.problem("asr", "the recogniser dropped")

    health.clear("asr", detail="recognition resumed")

    assert published[-1] == (LIVE, "recognition resumed", None)
