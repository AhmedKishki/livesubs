"""Tests for the credential-free replay harness.

The harness is what lets the overlay and pacing be developed with no API keys and
no microphone, so it needs to be trustworthy: the sequence must be deterministic,
and shutdown must not hang.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from livesubs.approaches import build_approach
from livesubs.config import Settings
from livesubs.events import HelloMessage, SegmentMessage, StatusMessage
from livesubs.hub import ClientRole, Hub, SegmentPublisher
from livesubs.languages import mode_for
from livesubs.segmenter import hold_ms
from livesubs.sim import SCRIPTS, SimPublisher, replay


def test_canned_speaker_options_and_atomic_rejection():
    publisher = SimPublisher(speaker="ar")
    assert [option.code for option in publisher.speaker_options] == ["auto", "en", "de", "ar"]
    before = publisher.speaker_snapshot()
    for code in ("fr", "en-US", "de-CH", "ar-SA"):
        with pytest.raises(ValueError):
            publisher.set_speaker(code)
        assert publisher.speaker_snapshot() == before
        with pytest.raises(ValueError):
            SimPublisher(speaker=code)
    assert publisher.set_speaker("en+de")
    assert publisher.speaker_snapshot()["requested"] == "auto"


async def test_manual_arabic_replay_is_truthful_and_terminal_across_loops():
    stream = replay(speaker="ar", speed=0, loop=True)
    rows = []
    async for message in stream:
        if isinstance(message, HelloMessage):
            assert message.data.source == "multilingual"
        if isinstance(message, SegmentMessage):
            rows.append(message.data)
            if message.data.seq == 7 and message.data.language == "de" and message.data.state == "final":
                break
    await stream.aclose()
    for seq in range(8):
        own = [row for row in rows if row.seq == seq]
        expected = SCRIPTS["default"].segments[seq % len(SCRIPTS["default"].segments)].final
        assert all(row.src == expected["ar"] for row in own)
        for code in ("en", "de", "ar"):
            target = [row for row in own if row.language == code]
            assert [(row.rev, row.state) for row in target] == (
                [(0, "final")] if code == "ar" else [(0, "draft"), (1, "final")]
            )
            assert target[-1].text == expected[code]
    assert [row.seq for row in rows] == sorted(row.seq for row in rows)


@pytest.mark.parametrize("source_text", [None, "نص عربي صريح"])
async def test_auto_arabic_fixture_is_unknown_without_corrupting_source(monkeypatch, source_text):
    from livesubs.sim import SimScript, SimSegment

    final = {"en": "Hello", "de": "Hallo", "ar": "مرحبا"}
    fixture = SimSegment(final=final, source_language="ar", source_text=source_text)
    monkeypatch.setitem(SCRIPTS, "arabic-evidence", SimScript(segments=[fixture]))
    detected = []
    messages = [m async for m in replay("arabic-evidence", speed=0,
                                       on_source=lambda seq, code: detected.append((seq, code)))]
    assert detected == [(0, "unknown")]
    rows = [m.data for m in messages if isinstance(m, SegmentMessage)]
    assert all(row.src == (source_text or final["ar"]) for row in rows)
    for code in final:
        assert [(row.rev, row.state) for row in rows if row.language == code] == [(0, "draft"), (1, "final")]

#: One target language, so that a segment is a draft and a final and nothing else. The
#: tests that are about several languages say so.
GERMAN_ONLY = mode_for("en")
VIEWER = ClientRole.viewer("de")


async def test_replay_sequence_is_deterministic() -> None:
    """speed=0 runs flat out, making the whole frame sequence observable."""

    messages = [message async for message in replay("default", mode=GERMAN_ONLY, speed=0)]

    assert messages[0].type == "hello"
    assert messages[1].type == "status"
    assert messages[1].data.status == "live"

    # Terminates with a terminal status, so the client can show "stopped".
    assert messages[-1].type == "status"
    assert messages[-1].data.status == "stopped"

    segments = [m for m in messages if isinstance(m, SegmentMessage)]
    assert segments[0].data.seq == 0
    assert segments[0].data.state == "final"
    assert segments[1].data.seq == 0
    assert segments[1].data.state == "draft"


async def test_every_script_segment_is_emitted_as_draft_then_final() -> None:
    script = "default"
    count = len(SCRIPTS[script].segments)
    messages = [message async for message in replay(script, mode=GERMAN_ONLY, speed=0)]
    segments = [m.data for m in messages if isinstance(m, SegmentMessage)]

    assert {segment.seq for segment in segments} == set(range(count))
    for seq in range(count):
        for code in ("en", "de", "ar"):
            own = [s for s in segments if s.seq == seq and s.language == code]
            assert own[-1].state == "final"
            assert [s.rev for s in own] == list(range(len(own)))


async def test_revisions_rewrite_rather_than_append() -> None:
    """The hard case for the overlay, so it must be present in the fixture."""

    messages = [message async for message in replay("default", mode=GERMAN_ONLY, speed=0)]
    segments = [m.data for m in messages if isinstance(m, SegmentMessage) and m.data.language == "ar"]

    revised = [
        (segments[i].text, segments[i + 1].text)
        for i in range(0, len(segments), 2)
        if segments[i].text != segments[i + 1].text
    ]
    assert revised, "at least one revision must actually change the text"


async def test_fast_speaker_genuinely_outruns_reading_speed() -> None:
    """Otherwise the display catch-up rule is dead code that will rot.

    The cadence must be comfortably shorter than the reading time of the German,
    or the overlay's drop rule can never trigger and would go untested.
    """

    script = SCRIPTS["fast_speaker"]
    longest = max(len(segment.final["de"]) for segment in script.segments)

    assert script.cadence_ms < hold_ms(longest) * 0.6
    assert script.cadence_ms < SCRIPTS["default"].cadence_ms


async def test_all_scripts_are_declared() -> None:
    assert set(SCRIPTS) == {"default", "fast_speaker", "lengthy"}
    for script in SCRIPTS.values():
        assert script.segments, "a script with no lines is not useful"
        assert script.cadence_ms > 0


async def test_lengthy_script_is_longer_than_default() -> None:
    assert len(SCRIPTS["lengthy"].segments) > len(SCRIPTS["default"].segments)


async def test_unknown_script_is_rejected() -> None:
    try:
        [message async for message in replay("nope", speed=0)]
    except KeyError as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected KeyError for an unknown script")


# --- publishing into the hub ---------------------------------------------------


def test_sim_publisher_satisfies_the_publisher_protocol() -> None:
    """It must be interchangeable with the live pipeline at the hub seam."""

    assert isinstance(SimPublisher(), SegmentPublisher)


async def test_sim_publisher_feeds_the_hub_and_terminates() -> None:
    hub = Hub()
    subscriber = hub.subscribe(VIEWER)
    seen: list[str] = []

    async def collect() -> None:
        while True:
            batch = await subscriber.mailbox.receive()
            seen.extend(message.type for message in batch)
            if any(
                message.type == "status" and message.data.status == "stopped"
                for message in batch
            ):
                return

    collector = asyncio.create_task(collect())

    await SimPublisher(script="default", speed=0, mode=GERMAN_ONLY).run(
        hub, session_id="test", stop=asyncio.Event()
    )
    await asyncio.wait_for(collector, timeout=2.0)

    assert seen[0] == "hello"
    assert "segment" in seen
    assert seen[-1] == "status"


async def test_viewer_never_sees_the_source_through_the_hub() -> None:
    """Same guarantee as the hub tests, exercised via the sim path."""

    hub = Hub()
    subscriber = hub.subscribe(VIEWER)

    await SimPublisher(script="default", speed=0, mode=GERMAN_ONLY).run(
        hub, session_id="test", stop=asyncio.Event()
    )

    batch = await asyncio.wait_for(subscriber.mailbox.receive(), timeout=1.0)
    segments = [m.data for m in batch if isinstance(m, SegmentMessage)]
    assert segments
    assert all(segment.src is None for segment in segments)


# --- shutdown ------------------------------------------------------------------


async def test_stop_cuts_a_long_replay_short() -> None:
    """Shutting down must not wait out a two-second inter-segment pause.

    The lengthy script sleeps ~2 s between segments; without racing the sleep
    against the stop event this would take many seconds to cancel.
    """

    stop = asyncio.Event()
    hub = Hub()
    publisher = SimPublisher(script="lengthy", speed=1.0)

    task = asyncio.create_task(publisher.run(hub, session_id="test", stop=stop))
    await asyncio.sleep(0.05)                    # let it get into a sleep
    stop.set()

    await asyncio.wait_for(task, timeout=1.0)


async def test_stop_already_set_returns_immediately() -> None:
    hub = Hub()
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(
        SimPublisher(script="lengthy", speed=1.0).run(hub, session_id="test", stop=stop),
        timeout=1.0,
    )



# --- looping ---------------------------------------------------------------------
#
# Serving repeats the script, and the reason is a bug found by using the thing rather
# than by testing it. The default script lasts twelve seconds, so the session had
# already stopped by the time anyone had walked to the phone and scanned the code. The
# viewer loaded a page that never received a subtitle, which is indistinguishable from
# a broken QR code - and was diagnosed as one.


async def _take_frames(
    messages: AsyncIterator[HelloMessage | SegmentMessage | StatusMessage],
    count: int,
) -> list[HelloMessage | SegmentMessage | StatusMessage]:
    """Pull exactly ``count`` frames and close the generator.

    A looping replay has no end, so the only way to observe one is to stop asking.
    """

    taken: list[HelloMessage | SegmentMessage | StatusMessage] = []
    async for message in messages:
        taken.append(message)
        if len(taken) >= count:
            break
    return taken


#: A replay opens with a hello and a live status before the first subtitle frame, so
#: frame counts and segment counts differ by this much.
_OPENING_FRAMES = 2


def _frames_for(segments: int) -> int:
    return _OPENING_FRAMES + segments


async def test_a_looping_replay_never_reports_a_terminal_status() -> None:
    """A demo that still has to run must not tell the client it has stopped."""

    frames = len(SCRIPTS["default"].segments) * 2       # a draft and a revision each

    messages = await _take_frames(
        replay("default", mode=GERMAN_ONLY, speed=0, loop=True), _frames_for(frames * 3)
    )

    statuses = [m.data for m in messages if m.type == "status"]
    assert statuses, "the viewer must be told the session is live"
    assert all(status.status == "live" for status in statuses)


async def test_a_looping_replay_repeats_its_content() -> None:
    frames = 32

    messages = await _take_frames(
        replay("default", mode=GERMAN_ONLY, speed=0, loop=True), _frames_for(frames + 1)
    )

    segments = [m.data for m in messages if isinstance(m, SegmentMessage)]
    first_pass, second_pass = segments[0], segments[frames]
    assert second_pass.seq == len(SCRIPTS["default"].segments)
    assert second_pass.text == first_pass.text


async def test_a_looping_replay_keeps_the_sequence_rising() -> None:
    """A restarted sequence would reach nobody: the hub refuses anything at or below
    what a client already holds, so pass two would be dropped as stale."""

    lines = len(SCRIPTS["default"].segments)      # one sequence number per line
    frames = 32                                  # four routing paths per pass

    messages = await _take_frames(
        replay("default", mode=GERMAN_ONLY, speed=0, loop=True), _frames_for(frames * 2)
    )

    segments = [m.data for m in messages if isinstance(m, SegmentMessage)]
    assert len(segments) == frames * 2

    assert [s.seq for s in segments] == sorted(s.seq for s in segments)
    assert max(s.seq for s in segments) == 2 * lines - 1
    final_keys = set()
    revisions = {}
    for segment in segments:
        key = (segment.seq, segment.language)
        assert key not in final_keys
        assert segment.rev > revisions.get(key, -1)
        revisions[key] = segment.rev
        if segment.state == "final":
            final_keys.add(key)


async def test_a_viewer_watching_across_a_pass_boundary_sees_both_passes() -> None:
    """The end-to-end version of the above, through the hub's own staleness rule."""

    lines = len(SCRIPTS["default"].segments)      # one sequence number per line

    hub = Hub()
    subscriber = hub.subscribe(VIEWER)
    stop = asyncio.Event()
    publisher = SimPublisher(script="default", speed=0, loop=True, mode=GERMAN_ONLY)
    task = asyncio.create_task(publisher.run(hub, session_id="test", stop=stop))

    seqs: list[int] = []
    try:
        # Keep reading until a number from the second pass has arrived.
        while len(set(seqs)) <= lines:
            batch = await asyncio.wait_for(subscriber.mailbox.receive(), timeout=2.0)
            seqs.extend(m.data.seq for m in batch if isinstance(m, SegmentMessage))
    finally:
        # A looping publisher outlives the test, so it must be stopped even when an
        # assertion above has already failed.
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert max(seqs) >= lines, "the second pass never reached the viewer"


def test_serving_the_simulation_loops_by_default() -> None:
    """What `--serve` gets, and what `--once` turns off."""

    settings = Settings(_env_file=None)

    publisher = build_approach("sim", settings, {})
    assert isinstance(publisher, SimPublisher)
    assert publisher.loop

    once = build_approach("sim", settings, {"loop": "false"})
    assert isinstance(once, SimPublisher)
    assert not once.loop


# --- who is speaking ---------------------------------------------------------------------


async def test_an_english_speaker_has_german_and_arabic_streams() -> None:
    messages = [m async for m in replay("default", mode=mode_for("en"), speed=0)]
    segments = [m.data for m in messages if isinstance(m, SegmentMessage)]
    hello = messages[0].data

    assert (hello.source, hello.targets) == ("multilingual", ["en", "de", "ar"])
    assert {s.language for s in segments} == {"en", "de", "ar"}
    first = [s for s in segments if s.seq == 0 and s.state == "final"]
    assert {s.language: s.text for s in first}["de"] == "Heute betrachten wir gedämpfte Schwingungen."
    assert all(s.src == "Today we will look at damped oscillations." for s in first)


async def test_a_german_speaker_has_english_and_arabic_streams() -> None:
    messages = [m async for m in replay("default", speaker="de", speed=0)]
    segments = [m.data for m in messages if isinstance(m, SegmentMessage)]
    hello = messages[0].data

    assert (hello.source, hello.targets) == ("multilingual", ["en", "de", "ar"])
    assert {s.language for s in segments} == {"en", "de", "ar"}
    first = {s.language: s for s in segments if s.seq == 0 and s.state == "final"}
    assert first["en"].text == "Today we will look at damped oscillations."
    assert first["en"].src == "Heute betrachten wir gedämpfte Schwingungen."


async def test_every_target_has_a_draft_and_a_final_for_every_line() -> None:
    mode = mode_for("de")
    messages = [m async for m in replay("default", mode=mode, speed=0)]
    segments = [m.data for m in messages if isinstance(m, SegmentMessage)]
    lines = len(SCRIPTS["default"].segments)

    for language in ("ar",):
        mine = [s for s in segments if s.language == language]
        assert len(mine) == 2 * lines
        assert [(s.seq, s.rev) for s in mine] == [(n, r) for n in range(lines) for r in (0, 1)]


def test_every_line_of_every_script_exists_in_every_language() -> None:
    """A mode must never meet a line it cannot show."""

    for name, script in SCRIPTS.items():
        for segment in script.segments:
            assert set(segment.final) >= {"en", "de", "ar"}, name


def test_the_publisher_declares_the_languages_it_can_produce() -> None:
    assert SimPublisher(mode=mode_for("en")).supported_languages == ("en", "de", "ar")
    assert SimPublisher(mode=mode_for("de")).supported_languages == ("en", "de", "ar")
    assert SimPublisher(mode=GERMAN_ONLY).supported_languages == ("en", "de", "ar")


async def test_a_sessions_own_announcement_is_not_overwritten_by_the_replays() -> None:
    from livesubs.events import HelloPayload

    hub = Hub()
    hub.publish(
        HelloMessage(
            data=HelloPayload(
                session_id="lecture", session_name="Physik I", source="en", targets=["de"]
            )
        )
    )

    await SimPublisher(script="default", speed=0, mode=GERMAN_ONLY).run(
        hub, session_id="lecture", stop=asyncio.Event()
    )

    assert hub.latest_hello is not None
    assert hub.latest_hello.session_name == "Physik I"


async def test_a_conversation_alternates_its_spoken_languages() -> None:
    mode = mode_for("en+de")
    messages = [m async for m in replay("default", mode=mode, speed=0)]
    segments = [m.data for m in messages if isinstance(m, SegmentMessage)]
    hello = messages[0].data

    assert (hello.source, hello.targets) == ("multilingual", ["en", "de", "ar"])
    assert {s.language for s in segments} == {"en", "de", "ar"}
    finals = {(s.seq, s.language): s for s in segments if s.state == "final"}
    assert finals[(0, "de")].src == "Today we will look at damped oscillations."
    assert finals[(1, "en")].src == "Die Amplitude nimmt mit der Zeit exponentiell ab."
    assert finals[(1, "en")].text == "The amplitude decreases exponentially over time."
