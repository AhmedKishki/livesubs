"""Tests for fan-out, ordering and the language filter.

The mailbox is the component that protects the lecture: if a slow viewer could
block or grow memory, one bad phone on hall WiFi would degrade transcription for
everyone. A viewer reads exactly one language and never the speaker's own words, and
that is enforced here, on the server, for every language the same way.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from livesubs.events import (
    ErrorMessage,
    HelloMessage,
    HelloPayload,
    PipelineStatus,
    SegmentMessage,
    SegmentState,
    Stage,
    StatusMessage,
    SubtitleSegment,
    Timing,
    WordTiming,
)
from livesubs.hub import ClientRole, Hub, Presence

DE = ClientRole.viewer("de")
AR = ClientRole.viewer("ar")
EN = ClientRole.viewer("en")
ADMIN = ClientRole.admin()


def _seg(
    seq: int = 1,
    rev: int = 0,
    *,
    language: str = "de",
    state: SegmentState = SegmentState.DRAFT,
    src: str | None = "The amplitude decreases exponentially.",
    text: str = "Die Amplitude nimmt mit der Zeit exponentiell ab.",
    words: list[WordTiming] | None = None,
) -> SubtitleSegment:
    return SubtitleSegment(
        seq=seq,
        rev=rev,
        state=state,
        language=language,
        text=text,
        src=src,
        words=words,
        timing=Timing(audio_ms=seq * 2000, emit_ms=seq * 2000 + 400),
    )


async def _messages(mailbox):
    return await asyncio.wait_for(mailbox.receive(), timeout=1.0)


async def _segments(mailbox) -> list[SubtitleSegment]:
    return [m.data for m in await _messages(mailbox) if isinstance(m, SegmentMessage)]


# --- roles --------------------------------------------------------------------


def test_a_viewer_reads_one_language_and_the_operator_reads_all() -> None:
    assert DE.reads("de") and not DE.reads("ar") and not DE.reads("en")
    assert all(ADMIN.reads(language) for language in ("de", "ar", "en", "fr"))


def test_a_role_is_a_value() -> None:
    assert ClientRole.viewer("de") == DE
    assert {DE, ClientRole.viewer("de"), AR} == {DE, AR}
    assert DE.value == "viewer_de"
    assert ADMIN.value == "admin"


def test_a_viewer_must_name_a_language_and_the_operator_must_not() -> None:
    with pytest.raises(ValueError):
        ClientRole("viewer")
    with pytest.raises(ValueError):
        ClientRole("admin", "de")


# --- the filter, for every language the same way ---------------------------------


@pytest.mark.parametrize("language", ["de", "ar", "en"])
async def test_a_viewer_gets_its_own_language_and_nothing_of_the_speaker(language: str) -> None:
    hub = Hub(["de", "ar", "en"])
    viewer = hub.subscribe(ClientRole.viewer(language))
    words = [WordTiming(text="private source", start_ms=0, end_ms=100)]

    for other in ("de", "ar", "en"):
        hub.publish_segment(_seg(language=other, text=f"text-{other}", words=words, src="spoken"))

    delivered = await _segments(viewer.mailbox)

    assert [s.language for s in delivered] == [language]
    assert delivered[0].text == f"text-{language}"
    assert delivered[0].src is None
    assert delivered[0].words is None


async def test_the_operator_gets_every_language_with_the_source_in_target_order() -> None:
    hub = Hub(["de", "ar"])
    admin = hub.subscribe(ADMIN)
    words = [WordTiming(text="private source", start_ms=0, end_ms=100)]

    hub.publish_segment(_seg(language="ar", text="مرحبا", words=words, src="Hello"))
    hub.publish_segment(_seg(language="de", text="Hallo", words=words, src="Hello"))

    delivered = await _segments(admin.mailbox)

    assert [s.language for s in delivered] == ["de", "ar"], "the hub's own order, not arrival"
    assert all(s.src == "Hello" and s.words == words for s in delivered)


async def test_the_cache_never_hands_a_viewer_the_operators_encoding() -> None:
    """The key must say whether the private fields were present, and the language."""

    hub = Hub(["de", "ar"])
    admin = hub.subscribe(ADMIN)
    viewer = hub.subscribe(DE)
    arabic = hub.subscribe(AR)

    hub.publish_segment(_seg(language="de", text="Hallo", src="Hello there"))
    hub.publish_segment(_seg(language="ar", text="مرحبا", src="Hello there"))

    encodings = {}
    for name, subscriber in (("admin", admin), ("de", viewer), ("ar", arabic)):
        encodings[name] = [hub.encode(m) for m in await _messages(subscriber.mailbox)]

    assert all("Hello there" in e for e in encodings["admin"] if '"segment"' in e)
    assert all("Hello there" not in e for e in encodings["de"] + encodings["ar"])
    assert any("Hallo" in e for e in encodings["de"]) and not any("مرحبا" in e for e in encodings["de"])
    assert any("مرحبا" in e for e in encodings["ar"]) and not any("Hallo" in e for e in encodings["ar"])


async def test_encoding_the_operators_copy_first_does_not_poison_the_viewers() -> None:
    hub = Hub(["de"])
    admin = hub.subscribe(ADMIN)
    viewer = hub.subscribe(DE)
    hub.publish_segment(
        _seg(src=None, words=[WordTiming(text="spoken", start_ms=0, end_ms=100)])
    )

    for message in await _messages(admin.mailbox):
        hub.encode(message)
    delivered = json.loads(hub.encode((await _messages(viewer.mailbox))[0]))["data"]

    assert delivered["words"] is None
    assert delivered["src"] is None


def test_encode_reuses_one_serialisation_per_revision() -> None:
    """Fan-out cost is one encoding per revision, not one per viewer."""

    hub = Hub()
    message = SegmentMessage(data=_seg(seq=3, rev=1, src=None))
    hub.publish_segment(_seg(seq=3, rev=1))

    assert hub.encode(message) is hub.encode(message)


def test_lines_of_two_languages_with_the_same_numbers_do_not_collide() -> None:
    hub = Hub()
    german = SegmentMessage(data=_seg(1, 0, language="de", text="Hallo", src=None))
    arabic = SegmentMessage(data=_seg(1, 0, language="ar", text="مرحبا", src=None))

    assert "Hallo" in hub.encode(german)
    assert "مرحبا" in hub.encode(arabic)
    assert "Hallo" in hub.encode(german)


# --- the hot path must not block ---------------------------------------------


def test_publish_is_synchronous() -> None:
    """A slow viewer must never be able to stall the pipeline."""

    hub = Hub()
    assert not inspect.iscoroutinefunction(hub.publish)
    assert not inspect.iscoroutinefunction(hub.publish_segment)


async def test_many_offers_collapse_to_the_newest_segment() -> None:
    """Memory per client is constant however much is published."""

    hub = Hub()
    subscriber = hub.subscribe(DE)

    for seq in range(50):
        hub.publish_segment(_seg(seq=seq))

    segments = await _segments(subscriber.mailbox)
    assert len(segments) == 1
    assert segments[0].seq == 49


async def test_publishing_with_no_subscribers_is_harmless() -> None:
    hub = Hub()
    hub.publish_segment(_seg())
    assert hub.subscriber_count == 0


# --- ordering, per language ----------------------------------------------------


async def test_a_stale_revision_of_the_same_seq_is_refused() -> None:
    """`final` must never be followed by `draft` on the wire."""

    hub = Hub()
    subscriber = hub.subscribe(DE)

    hub.publish_segment(_seg(seq=7, rev=2, state=SegmentState.FINAL))
    hub.publish_segment(_seg(seq=7, rev=1, state=SegmentState.DRAFT))   # stale

    segments = await _segments(subscriber.mailbox)
    assert len(segments) == 1
    assert segments[0].rev == 2
    assert segments[0].state is SegmentState.FINAL


async def test_a_new_seq_is_accepted_after_a_high_revision() -> None:
    """Revision ordering must not block the next segment."""

    hub = Hub()
    subscriber = hub.subscribe(DE)

    hub.publish_segment(_seg(seq=3, rev=5))
    hub.publish_segment(_seg(seq=4, rev=0))

    segments = await _segments(subscriber.mailbox)
    assert [s.seq for s in segments] == [4]
    assert segments[0].rev == 0


async def test_revisions_advance_in_order() -> None:
    hub = Hub()
    subscriber = hub.subscribe(DE)

    hub.publish_segment(_seg(seq=1, rev=0))
    assert (await _segments(subscriber.mailbox))[0].rev == 0

    hub.publish_segment(_seg(seq=1, rev=1))
    assert (await _segments(subscriber.mailbox))[0].rev == 1


async def test_each_language_has_its_own_ordering_guard() -> None:
    hub = Hub(["de", "ar"])
    admin = hub.subscribe(ADMIN)

    hub.publish_segment(_seg(7, 2, language="de", text="de-final"))
    hub.publish_segment(_seg(7, 2, language="ar", text="ar-final"))
    # A stale German revision is refused without touching the Arabic guard.
    hub.publish_segment(_seg(7, 1, language="de", text="de-stale"))
    hub.publish_segment(_seg(8, 0, language="ar", text="ar-next"))

    delivered = {s.language: s.text for s in await _segments(admin.mailbox)}

    assert delivered == {"de": "de-final", "ar": "ar-next"}


# --- late joiners and disconnects ---------------------------------------------


async def test_late_joiner_receives_a_snapshot() -> None:
    """Nobody should ever be staring at a blank screen mid-lecture."""

    hub = Hub()
    hub.publish_status(PipelineStatus.LIVE)
    hub.publish_segment(_seg(seq=12, rev=3, state=SegmentState.FINAL))

    subscriber = hub.subscribe(DE)          # joins afterwards
    messages = await _messages(subscriber.mailbox)

    assert [m.type for m in messages] == ["status", "segment"]      # defined drain order
    assert messages[1].data.seq == 12


async def test_a_late_joiner_is_seeded_only_with_its_own_language() -> None:
    hub = Hub(["de", "ar"])
    hub.publish_segment(_seg(language="de", text="Hallo"))
    hub.publish_segment(_seg(language="ar", text="مرحبا"))

    german = hub.subscribe(DE)
    arabic = hub.subscribe(AR)

    assert [s.text for s in await _segments(german.mailbox)] == ["Hallo"]
    assert [s.text for s in await _segments(arabic.mailbox)] == ["مرحبا"]


async def test_hello_tells_each_connection_the_language_it_receives() -> None:
    hub = Hub(["de", "ar"])
    hub.publish(
        HelloMessage(
            data=HelloPayload(
                session_id="s", session_name="n", source="en", targets=["de", "ar"]
            )
        )
    )
    german = hub.subscribe(DE)
    admin = hub.subscribe(ADMIN)

    german_hello = (await _messages(german.mailbox))[0]
    admin_hello = (await _messages(admin.mailbox))[0]

    assert german_hello.data.language == "de"
    assert admin_hello.data.language is None
    assert german_hello.data.targets == ["de", "ar"]


async def test_close_wakes_the_reader_and_returns_empty() -> None:
    """The writer task's exit condition."""

    hub = Hub()
    subscriber = hub.subscribe(DE)

    subscriber.mailbox.close()

    assert await _messages(subscriber.mailbox) == []
    assert subscriber.mailbox.closed


async def test_offers_after_close_are_ignored() -> None:
    hub = Hub()
    subscriber = hub.subscribe(DE)
    subscriber.mailbox.close()

    hub.publish_segment(_seg())
    assert subscriber.mailbox.has_pending is False


async def test_unsubscribe_stops_delivery() -> None:
    hub = Hub()
    subscriber = hub.subscribe(DE)

    hub.unsubscribe(subscriber)
    hub.publish_segment(_seg())

    assert hub.subscriber_count == 0
    assert subscriber.mailbox.closed


# --- errors are for the operator only -----------------------------------------


async def test_errors_reach_admins_but_not_viewers() -> None:
    hub = Hub()
    viewer = hub.subscribe(DE)
    admin = hub.subscribe(ADMIN)

    hub.publish_error("asr socket closed", Stage.ASR)

    assert any(isinstance(m, ErrorMessage) for m in await _messages(admin.mailbox))
    assert viewer.mailbox.has_pending is False


# --- status, which may concern one language ---------------------------------------


async def test_a_status_about_one_language_reaches_only_that_languages_readers() -> None:
    hub = Hub(["de", "ar"])
    german = hub.subscribe(DE)
    arabic = hub.subscribe(AR)
    admin = hub.subscribe(ADMIN)

    hub.publish_status(PipelineStatus.DELAYED, "translation unavailable", language="ar")

    assert german.mailbox.has_pending is False
    assert (await _messages(arabic.mailbox))[0].data.status is PipelineStatus.DELAYED
    assert (await _messages(admin.mailbox))[0].data.language == "ar"


async def test_a_client_is_told_the_worst_of_everything_that_concerns_it() -> None:
    hub = Hub(["de", "ar"])
    german = hub.subscribe(DE)

    hub.publish_status(PipelineStatus.DELAYED, "recognition reconnecting")
    hub.publish_status(PipelineStatus.DELAYED, "translation is behind", language="de")
    hub.publish_status(PipelineStatus.STOPPED, "capture lost", language=None)
    hub.publish_status(PipelineStatus.LIVE, None, language="de")

    status = [m for m in await _messages(german.mailbox) if isinstance(m, StatusMessage)]

    assert len(status) == 1, "one status on the wire, however many scopes there are"
    assert status[0].data.status is PipelineStatus.STOPPED


async def test_clearing_a_scope_clears_it_for_its_readers() -> None:
    hub = Hub(["de", "ar"])
    german = hub.subscribe(DE)

    hub.publish_status(PipelineStatus.DELAYED, "translation is behind", language="de")
    await _messages(german.mailbox)
    hub.publish_status(PipelineStatus.LIVE, None, language="de")

    status = [m for m in await _messages(german.mailbox) if isinstance(m, StatusMessage)]
    assert status[0].data.status is PipelineStatus.LIVE


async def test_a_late_joiner_is_seeded_with_the_status_that_concerns_it() -> None:
    hub = Hub(["de", "ar"])
    hub.publish_status(PipelineStatus.DELAYED, "translation unavailable", language="ar")

    german = hub.subscribe(DE)
    arabic = hub.subscribe(AR)

    assert german.mailbox.has_pending is False
    assert (await _messages(arabic.mailbox))[0].data.detail == "translation unavailable"


# --- audience accounting, which decides paid work ------------------------------


def test_viewer_count_excludes_admins() -> None:
    """An operator console is not an audience and must not create provider demand."""

    hub = Hub()
    hub.subscribe(ADMIN)
    hub.subscribe(ADMIN)

    assert hub.viewer_count == 0
    assert hub.has_viewers is False

    viewer = hub.subscribe(DE)
    assert hub.viewer_count == 1
    assert hub.has_viewers is True

    hub.unsubscribe(viewer)
    assert hub.viewer_count == 0
    assert hub.has_viewers is False


def test_viewers_are_counted_by_language() -> None:
    hub = Hub(["de", "ar"])
    hub.subscribe(DE)
    hub.subscribe(DE)
    hub.subscribe(AR)

    assert hub.viewer_count == 3
    assert hub.viewer_count_for("de") == 2
    assert hub.viewer_count_for("ar") == 1
    assert hub.viewer_count_for("en") == 0
    assert hub.viewers_by_language == {"de": 2, "ar": 1}


def test_the_shared_audience_advances_only_when_it_crosses_zero() -> None:
    """A second viewer is not a new occupancy; an admin is not demand at all."""

    hub = Hub(["de", "ar"])
    shared = hub.audience()
    assert shared.generation == 0

    first = hub.subscribe(DE)
    assert shared.generation == 1

    second = hub.subscribe(DE)
    assert shared.generation == 1          # still just "some viewers"

    hub.subscribe(ADMIN)
    assert shared.generation == 1          # admin adds no demand

    hub.unsubscribe(first)
    assert shared.generation == 1          # second viewer still present

    hub.unsubscribe(second)
    assert shared.generation == 2          # crossed back to none

    hub.subscribe(DE)
    assert shared.generation == 3          # a fresh occupancy


def test_adding_another_language_does_not_disturb_the_shared_audience() -> None:
    hub = Hub(["de", "ar"])
    german = hub.subscribe(DE)
    shared = hub.audience().generation

    arabic = hub.subscribe(AR)
    assert hub.audience().generation == shared          # recogniser not disturbed

    hub.unsubscribe(arabic)
    assert hub.audience().generation == shared
    hub.unsubscribe(german)
    assert hub.audience().generation == shared + 1


def test_each_language_has_its_own_audience() -> None:
    hub = Hub(["de", "ar"])
    assert hub.audience("de").generation == 0
    assert hub.audience("ar").generation == 0

    german = hub.subscribe(DE)
    assert hub.audience("de").generation == 1
    assert hub.audience("ar").generation == 0

    arabic = hub.subscribe(AR)
    assert hub.audience("ar").generation == 1
    assert hub.audience("de").generation == 1

    hub.unsubscribe(arabic)
    assert hub.audience("ar").generation == 2
    assert not hub.audience("ar").present
    hub.unsubscribe(german)
    assert hub.audience("de").generation == 2


def test_a_language_nobody_declared_still_gets_an_audience() -> None:
    hub = Hub()
    hub.subscribe(EN)

    assert hub.audience("en").present
    assert hub.viewers_by_language == {"en": 1}


async def test_waiting_for_a_change_wakes_on_the_connect_itself() -> None:
    """The supervisor is notified by the connect, not by the next frame."""

    hub = Hub(["de"])
    audience = hub.audience()
    waiter = asyncio.create_task(audience.wait_change(audience.generation))
    await asyncio.sleep(0)
    assert not waiter.done()

    hub.subscribe(DE)
    assert await asyncio.wait_for(waiter, timeout=1.0) == 1


async def test_waiting_for_a_generation_that_already_passed_returns_at_once() -> None:
    presence = Presence()
    presence.update(1)

    assert await asyncio.wait_for(presence.wait_change(0), timeout=1.0) == 1


async def test_a_wait_can_time_out_without_a_change() -> None:
    presence = Presence()

    assert await presence.wait_change(0, timeout=0.01) == 0


# --- the cached line, which must not outlive its audience ---------------------------


def test_forget_segment_snapshot_drops_the_cached_line() -> None:
    """A paused lecture must not seed the next viewer with its stale last line."""

    hub = Hub()
    hub.publish_segment(_seg(seq=5, rev=0))
    hub.forget_segment_snapshot()

    # A joiner after the reset is not handed the old line.
    subscriber = hub.subscribe(DE)
    assert subscriber.mailbox.has_pending is False


def test_forgetting_one_language_keeps_the_others() -> None:
    hub = Hub(["de", "ar"])
    hub.publish_segment(_seg(language="de", text="Hallo"))
    hub.publish_segment(_seg(language="ar", text="مرحبا"))

    hub.forget_segment_snapshot("ar")

    assert hub.subscribe(DE).mailbox.has_pending is True
    assert hub.subscribe(AR).mailbox.has_pending is False


def test_gated_hub_does_not_cache_a_segment_arriving_with_no_viewers() -> None:
    """A late translation must not seed the next viewer with text nobody watched."""

    hub = Hub()
    hub.enable_audience_gating()
    hub.publish_segment(_seg(seq=3))

    assert hub.subscribe(DE).mailbox.has_pending is False


def test_a_gated_hub_caches_for_a_language_that_has_a_viewer() -> None:
    hub = Hub(["de", "ar"])
    hub.enable_audience_gating()
    hub.subscribe(DE)
    hub.publish_segment(_seg(language="de", text="Hallo"))
    hub.publish_segment(_seg(language="ar", text="مرحبا"))

    assert hub.subscribe(DE).mailbox.has_pending is True
    assert hub.subscribe(AR).mailbox.has_pending is False, "nobody was reading Arabic"


def test_ungated_hub_still_caches_with_no_viewers() -> None:
    """The non-gated replay keeps its snapshot behaviour across a reconnect."""

    hub = Hub()
    hub.publish_segment(_seg(seq=3))

    assert hub.subscribe(DE).mailbox.has_pending is True


def test_the_last_viewer_of_a_language_leaving_clears_that_languages_line() -> None:
    hub = Hub(["de", "ar"])
    hub.enable_audience_gating()
    german = hub.subscribe(DE)
    arabic = hub.subscribe(AR)
    hub.publish_segment(_seg(language="de", text="Hallo"))
    hub.publish_segment(_seg(language="ar", text="مرحبا"))

    hub.unsubscribe(arabic)

    assert hub.subscribe(AR).mailbox.has_pending is False
    assert hub.subscribe(DE).mailbox.has_pending is True
    hub.unsubscribe(german)
