"""End-to-end tests for the live pipeline.

These run the real path — synthetic capture, real conditioning, real segmentation,
real hub and real mailbox logic — with only the words stubbed. That is the point:
the wiring is exactly the part that a provider outage would otherwise hide, so it
needs a regression net that does not depend on any vendor being reachable.

The properties asserted here are the ones the overlay's correctness rests on, so
they are invariants rather than examples: monotonic ``seq``, ``rev`` restarting at
zero per sequence, no ``final`` ever followed by a ``draft``, and viewers never
receiving the English source.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from livesubs.asr.stub import StubAsr
from livesubs.audio.calibration import CalibrationError, CalibrationNotice
from livesubs.audio.synthetic import SpeechLikeSource
from livesubs.events import (
    ErrorMessage,
    SegmentMessage,
    StatusMessage,
    SubtitleSegment,
    WireMessage,
)
from livesubs.hub import ClientRole, Hub, SegmentPublisher
from livesubs.ingest import FrameStats
from livesubs.mt.base import MtChunk, MtContext, MtProvider
from livesubs.mt.passthrough import MARKER, PassthroughMt
from livesubs.pipeline import Committed, LivePipeline, PipelineConfig

PHRASES = (
    "alpha beta gamma delta epsilon",
    "zeta eta theta iota kappa lambda",
)


async def test_failed_calibration_prevents_provider_calls(monkeypatch):
    pipeline = make_pipeline(duration_ms=40)
    pipeline.source.prepare = AsyncMock(side_effect=CalibrationError("cannot verify gain"))
    pipeline.source.close = AsyncMock()
    pipeline.mt.warm = AsyncMock()
    pipeline.asr.open = AsyncMock()
    with pytest.raises(CalibrationError):
        await run_to_completion(pipeline)
    pipeline.source.close.assert_awaited_once()
    pipeline.mt.warm.assert_not_awaited()
    pipeline.asr.open.assert_not_awaited()


async def test_stop_during_calibration_closes_source_without_opening_providers():
    pipeline = make_pipeline(duration_ms=40)
    entered, released, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def prepare():
        entered.set()
        await released.wait()

    async def close():
        released.set()

    pipeline.source.prepare = prepare
    pipeline.source.close = AsyncMock(side_effect=close)
    pipeline.mt.warm = AsyncMock()
    pipeline.asr.open = AsyncMock()
    task = asyncio.create_task(pipeline.run(RecordingHub(), session_id="test", stop=stop))
    await asyncio.wait_for(entered.wait(), 1)
    stop.set()
    await asyncio.wait_for(task, 1)
    pipeline.source.close.assert_awaited()
    pipeline.mt.warm.assert_not_awaited()
    pipeline.asr.open.assert_not_awaited()


def test_calibration_notices_are_visible_and_do_not_clear_translation_lag():
    pipeline = make_pipeline(duration_ms=40)
    hub = RecordingHub()
    pipeline._hub = hub
    pipeline._capture_calibration(CalibrationNotice("adjusting", "verifying microphone"))
    assert hub.statuses[-1] == "delayed"
    assert "verifying microphone" in hub.messages[-1].data.detail
    pipeline._lagging = True
    pipeline._capture_calibration(CalibrationNotice("ready", "capture verified"))
    assert hub.statuses[-1] == "delayed"
    pipeline._lagging = False
    pipeline._capture_calibration(CalibrationNotice("ready", "capture verified"))
    assert hub.statuses[-1] == "live"


def test_failed_calibration_notice_reports_capture_stage():
    pipeline = make_pipeline(duration_ms=40)
    hub = RecordingHub()
    pipeline._hub = hub
    pipeline._capture_calibration(CalibrationNotice("failed", "gain ineffective"))
    errors = [m for m in hub.messages if isinstance(m, ErrorMessage)]
    assert errors[-1].data.stage.value == "capture"


def test_degraded_gain_protection_tells_the_operator_without_delaying_viewers():
    """The extra gave up and capture goes on: an error for the console, no delayed pill."""

    pipeline = make_pipeline(duration_ms=40)
    hub = RecordingHub()
    pipeline._hub = hub
    pipeline._capture_calibration(CalibrationNotice("adjusting", "reducing gain"))
    pipeline._capture_calibration(CalibrationNotice("degraded", "no supported gain control"))

    errors = [m for m in hub.messages if isinstance(m, ErrorMessage)]
    assert errors[-1].data.stage.value == "capture"
    assert hub.statuses[-1] == "live"


class RecordingHub(Hub):
    """A hub that keeps every published message.

    A mailbox deliberately does not: it is latest-wins per channel, which is the
    right design for delivery and the wrong place to observe a sequence from.
    Recording at the hub also means the tests exercise the real ``Hub.publish``,
    its encoding cache and its ordering guard.
    """

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[WireMessage] = []

    def publish(self, message: WireMessage) -> None:
        self.messages.append(message)
        super().publish(message)

    @property
    def segments(self) -> list[SubtitleSegment]:
        return [m.data for m in self.messages if isinstance(m, SegmentMessage)]

    @property
    def statuses(self) -> list[str]:
        return [m.data.status.value for m in self.messages if isinstance(m, StatusMessage)]

class RecordingMt(PassthroughMt):
    """A translator that remembers what context it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[MtContext] = []
        self.sources: list[str] = []

    async def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
        self.sources.append(src)
        self.contexts.append(ctx)
        async for chunk in super().translate(src, ctx):
            yield chunk


class FlakyMt(PassthroughMt):
    """Fails its first ``failures`` calls, then behaves."""

    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self.remaining = failures

    async def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("provider is down")
        async for chunk in super().translate(src, ctx):
            yield chunk


def make_pipeline(
    *,
    duration_ms: int = 8000,
    mt: MtProvider | None = None,
    realtime: bool = False,
) -> LivePipeline:
    return LivePipeline(
        source=SpeechLikeSource(
            realtime=realtime,
            duration_ms=None if realtime else duration_ms,
        ),
        asr=StubAsr(phrases=PHRASES),
        mt=mt if mt is not None else PassthroughMt(),
        config=PipelineConfig(telemetry_interval_ms=1000),
    )


async def run_to_completion(
    pipeline: LivePipeline,
    hub: RecordingHub | None = None,
) -> RecordingHub:
    """Drive a pipeline to the end of its audio and return what it published."""

    hub = hub if hub is not None else RecordingHub()
    await pipeline.run(hub, session_id="test", stop=asyncio.Event())
    return hub

# --- the shape of a healthy run -------------------------------------------------


async def test_speech_bursts_become_translated_finals() -> None:
    hub = await run_to_completion(make_pipeline())

    finals = [s for s in hub.segments if s.state == "final"]
    assert finals, "no final segment was published"
    assert all(s.de.startswith(MARKER) for s in finals)


async def test_the_english_source_rides_alongside_the_german() -> None:
    """The operator console needs it, to diagnose a mistranslation."""

    hub = await run_to_completion(make_pipeline())

    finals = [s for s in hub.segments if s.state == "final"]
    assert all(s.src in PHRASES for s in finals)


async def test_a_healthy_run_never_reports_delayed() -> None:
    hub = await run_to_completion(make_pipeline())
    assert "delayed" not in hub.statuses


async def test_timing_carries_two_independent_clocks() -> None:
    """``audio_ms`` is audio-derived; ``emit_ms`` is wall-clock.

    The gap between them *is* the pipeline's latency, so they must not be the same
    number. A fast replay makes the distinction vivid: eight seconds of audio is
    processed in a fraction of a second, so the wall clock falls far behind the
    audio clock. In real time the two advance together and that gap becomes the
    latency that actually matters to a viewer.
    """

    hub = await run_to_completion(make_pipeline())
    finals = [s for s in hub.segments if s.state == "final"]

    assert finals
    assert all(s.timing.audio_ms > 0 for s in finals)
    assert all(s.timing.render_ms is None for s in finals)
    assert all(s.timing.emit_ms < s.timing.audio_ms for s in finals)


async def test_no_published_subtitle_text_is_ever_the_english_source() -> None:
    """Regression test for the most dangerous shortcut available here.

    Echoing the ASR partial straight into ``de`` would make text appear sooner, and
    it would put English on a German-only overlay. Unless the translator has run,
    there is nothing German to publish, so nothing is published.
    """

    hub = await run_to_completion(make_pipeline())

    assert hub.segments
    for segment in hub.segments:
        assert segment.de.startswith(MARKER), (
            f"seq {segment.seq} rev {segment.rev} published untranslated text: {segment.de!r}"
        )


# --- ordering invariants --------------------------------------------------------


async def test_seq_never_regresses() -> None:
    """An inverted seq would let the overlay show a stale line over a fresh one."""

    hub = await run_to_completion(make_pipeline())
    keys = [(s.seq, s.rev) for s in hub.segments]

    assert len(keys) > 1
    assert keys == sorted(keys)


async def test_rev_starts_at_zero_for_every_seq_and_only_increases() -> None:
    hub = await run_to_completion(make_pipeline())

    revs: dict[int, list[int]] = {}
    for segment in hub.segments:
        revs.setdefault(segment.seq, []).append(segment.rev)

    assert revs
    for seq, seen in revs.items():
        assert seen[0] == 0, f"seq {seq} started at rev {seen[0]}"
        assert seen == sorted(seen)
        assert len(set(seen)) == len(seen), f"seq {seq} repeated a revision"


async def test_a_final_is_never_followed_by_a_draft() -> None:
    """The one ordering rule a viewer would notice immediately."""

    hub = await run_to_completion(make_pipeline())

    finished: set[int] = set()
    for segment in hub.segments:
        if segment.state == "final":
            finished.add(segment.seq)
        else:
            assert segment.seq not in finished, f"draft after final for seq {segment.seq}"


async def test_every_drafted_seq_also_finishes() -> None:
    """A seq left at draft would leave the overlay holding a line forever.

    This is not hypothetical: without the shutdown flush, the utterance still open
    when the audio stops is published as a draft and then never committed.
    """

    hub = await run_to_completion(make_pipeline())

    drafted = {s.seq for s in hub.segments if s.state == "draft"}
    finalised = {s.seq for s in hub.segments if s.state == "final"}

    assert drafted
    assert drafted <= finalised


async def test_seqs_are_contiguous_from_zero() -> None:
    hub = await run_to_completion(make_pipeline())
    seqs = sorted({s.seq for s in hub.segments})

    assert seqs == list(range(len(seqs)))

# --- pacing and throttling ------------------------------------------------------


async def test_drafts_are_throttled_rather_than_published_per_audio_frame() -> None:
    """One draft per audio frame would be 50 messages a second per viewer."""

    hub = await run_to_completion(make_pipeline())

    per_seq: dict[int, int] = {}
    for segment in hub.segments:
        if segment.state == "draft":
            per_seq[segment.seq] = per_seq.get(segment.seq, 0) + 1

    assert per_seq
    # A two second segment spans about a hundred audio frames, so this only holds
    # because drafts come from translation deltas rather than from the audio.
    assert max(per_seq.values()) < 20


async def test_the_audio_path_is_the_clock_not_wall_time() -> None:
    """Audio-derived timing must match the audio that was played, exactly.

    This is what lets a file replayed faster than real time behave identically to a
    live device, and therefore what makes any of this testable.
    """

    hub = await run_to_completion(make_pipeline(duration_ms=4000))
    finals = [s for s in hub.segments if s.state == "final"]

    assert finals
    assert all(s.timing.audio_ms <= 4000 for s in finals)


# --- adapters -------------------------------------------------------------------


async def test_translation_context_carries_forward() -> None:
    """German is verb-final, so an isolated English fragment translates badly.

    Context must arrive in the user message, never the system prompt, because the
    provider's cache only hits on a stable prefix.
    """

    mt = RecordingMt()
    await run_to_completion(make_pipeline(mt=mt))

    assert len(mt.contexts) >= 2
    assert mt.contexts[0].source_context == []
    assert mt.contexts[1].source_context == [mt.sources[0]]
    assert mt.contexts[1].target_context  # the previous German output


async def test_a_failing_translation_does_not_end_the_lecture() -> None:
    """One bad segment is a lost line, not a lost lecture."""

    hub = await run_to_completion(make_pipeline(mt=FlakyMt(failures=2)))

    errors = [
        m.data
        for m in hub.messages
        if isinstance(m, ErrorMessage) and m.data.stage == "mt"
    ]
    assert errors
    assert any(s.state == "final" for s in hub.segments)
    assert 0 not in {s.seq for s in hub.segments}, "the segment that failed twice is dropped"


async def test_a_quick_failure_is_retried_once_before_the_segment_is_given_up() -> None:
    """A dropped connection costs milliseconds to retry and a whole line not to."""

    hub = await run_to_completion(make_pipeline(mt=FlakyMt(failures=1)))

    assert not [m for m in hub.messages if isinstance(m, ErrorMessage)]
    assert any(s.seq == 0 and s.state == "final" for s in hub.segments)


async def test_a_translation_that_dies_part_way_is_never_shown_as_complete() -> None:
    """The truncated line used to be published as a final, and remembered as context."""

    class DiesOnce(RecordingMt):
        async def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
            self.sources.append(src)
            self.contexts.append(ctx)
            if len(self.sources) <= 2:  # the first segment, and its retry
                yield MtChunk(text_delta="Die Schwingung")
                yield MtChunk(text_delta=" lässt sich")
                raise RuntimeError("connection reset")
            async for chunk in PassthroughMt.translate(self, src, ctx):
                yield chunk

    mt = DiesOnce()
    committed: list[Committed] = []
    pipeline = make_pipeline(mt=mt)
    pipeline.on_segment = committed.append

    hub = await run_to_completion(pipeline)

    first = [s for s in hub.segments if s.seq == 0]
    assert first[-1].state == "final", "a line that stops must still finish, or it hangs for ever"
    assert first[-1].de.endswith("\u2026"), "and it says it was cut short"
    assert 0 not in {c.seq for c in committed}, "a cut-off line is not a translation to record"
    later = [ctx for ctx in mt.contexts[2:]]
    assert later and all("Die Schwingung" not in " ".join(c.target_context) for c in later)
    assert any(isinstance(m, ErrorMessage) and m.data.stage == "mt" for m in hub.messages)


async def test_a_translation_past_its_deadline_is_abandoned_and_not_retried() -> None:
    class Stalls(PassthroughMt):
        def __init__(self) -> None:
            super().__init__()
            self.calls: dict[str, int] = {}

        async def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
            self.calls[src] = self.calls.get(src, 0) + 1
            await asyncio.sleep(30)
            yield MtChunk(text_delta="never", done=True)

    mt = Stalls()
    pipeline = make_pipeline(mt=mt, duration_ms=1000)
    pipeline.config.mt_deadline_s = 0.05

    hub = await asyncio.wait_for(run_to_completion(pipeline), timeout=10)

    assert mt.calls and set(mt.calls.values()) == {1}, "a timeout is never retried"
    assert not [s for s in hub.segments if s.state == "final"]
    assert any(isinstance(m, ErrorMessage) and m.data.stage == "mt" for m in hub.messages)


async def test_drafts_are_spaced_but_the_first_words_and_the_final_always_go_out() -> None:
    class Chatty(PassthroughMt):
        async def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
            for index in range(40):
                yield MtChunk(text_delta=f"w{index} ")
            yield MtChunk(text_delta="", done=True)

    pipeline = make_pipeline(mt=Chatty(), duration_ms=1000)
    pipeline.config.draft_interval_ms = 10_000  # nothing after the first may follow

    hub = await run_to_completion(pipeline)

    first = [s for s in hub.segments if s.seq == 0]
    assert [s.state.value for s in first] == ["draft", "final"]
    assert first[0].de == "w0 ", "the first words are published at once"
    assert first[-1].de.startswith("w0 w1 ") and first[-1].de.rstrip().endswith("w39")


def test_a_translator_that_cannot_keep_up_loses_the_oldest_segments_not_the_newest() -> None:
    pipeline = make_pipeline(duration_ms=40)
    pipeline.config.max_backlog = 3
    pipeline._de.active = True

    for seq in range(10):
        pipeline._dispatch((seq, f"line {seq}", seq * 1000, seq * 1000 + 900))

    assert [item[0] for item in pipeline._de.queue] == [7, 8, 9]
    assert pipeline._de.shed == 7


# --- shutdown -------------------------------------------------------------------


async def test_the_utterance_open_when_the_audio_stops_is_still_committed() -> None:
    """A lecture that stops mid-sentence must still show that sentence.

    1000 ms is deliberately shorter than ``max_segment_ms``, so the segmenter never
    gets a chance to emit. Everything here therefore depends on the shutdown flush,
    which is also why the provider is closed before the publisher is drained.
    """

    hub = await run_to_completion(make_pipeline(duration_ms=1000))
    finals = [s for s in hub.segments if s.state == "final"]

    assert finals
    assert finals[-1].src in PHRASES


async def test_stop_ends_the_run_promptly_and_releases_capture() -> None:
    """Shutdown must not hang: the session cancels after three seconds."""

    hub = RecordingHub()
    stop = asyncio.Event()
    stop.set()

    pipeline = make_pipeline(realtime=True)
    await asyncio.wait_for(pipeline.run(hub, session_id="test", stop=stop), timeout=5.0)

    assert pipeline.source.alive is False
    assert pipeline.asr.healthy is False


# --- telemetry ------------------------------------------------------------------


async def test_stats_are_reported_while_the_pipeline_runs() -> None:
    """Levels are what tell an operator that capture is working at all."""

    hub = RecordingHub()
    seen: list[FrameStats] = []

    pipeline = make_pipeline(duration_ms=8000)
    pipeline.on_stats = seen.append
    await pipeline.run(hub, session_id="test", stop=asyncio.Event())

    assert len(seen) == 8
    assert all(stats.now_ms % 1000 == 0 for stats in seen)
    assert all(stats.noise_floor_dbfs < 0 for stats in seen)


async def test_a_broken_stats_callback_cannot_stop_the_lecture() -> None:
    """Telemetry is observation, so it must never be able to break delivery."""

    hub = RecordingHub()

    def explode(_: FrameStats) -> None:
        raise RuntimeError("console is broken")

    pipeline = make_pipeline(duration_ms=2000)
    pipeline.on_stats = explode
    await pipeline.run(hub, session_id="test", stop=asyncio.Event())

    assert hub.segments


# --- the German-only requirement ------------------------------------------------


async def test_viewers_never_receive_the_english_source() -> None:
    """Enforced on the server, never trusted to the viewer page."""

    hub = RecordingHub()
    viewer = hub.subscribe(ClientRole.VIEWER)
    admin = hub.subscribe(ClientRole.ADMIN)

    pipeline = make_pipeline()
    await pipeline.run(hub, session_id="test", stop=asyncio.Event())

    assert any(isinstance(m, SegmentMessage) for m in hub.messages)

    viewer_messages = await viewer.mailbox.receive()
    admin_messages = await admin.mailbox.receive()

    viewer_segments = [m.data for m in viewer_messages if isinstance(m, SegmentMessage)]
    admin_segments = [m.data for m in admin_messages if isinstance(m, SegmentMessage)]

    assert admin_segments and admin_segments[-1].src
    assert viewer_segments and viewer_segments[-1].src is None
    assert viewer_segments[-1].de


async def test_the_pipeline_satisfies_the_publisher_protocol() -> None:
    """The hub does not know which approach is driving it, so this must hold."""

    assert isinstance(make_pipeline(), SegmentPublisher)

# --- committed segments, which become transcripts ---------------------------------


async def test_every_final_is_reported_to_the_segment_sink() -> None:
    seen: list[Committed] = []

    pipeline = make_pipeline()
    pipeline.on_segment = seen.append
    hub = await run_to_completion(pipeline)

    finals = [s for s in hub.segments if s.state == "final"]
    assert len(seen) == len(finals)
    assert [c.seq for c in seen] == [s.seq for s in finals]


async def test_committed_segments_carry_both_texts_and_a_usable_span() -> None:
    """This is everything a subtitle file needs, so nothing downstream must guess."""

    seen: list[Committed] = []

    pipeline = make_pipeline()
    pipeline.on_segment = seen.append
    await run_to_completion(pipeline)

    assert seen
    for committed in seen:
        assert committed.start_ms < committed.end_ms
        assert committed.src in PHRASES
        assert committed.de.startswith(MARKER)


async def test_committed_spans_stay_inside_the_audio_that_was_played() -> None:
    """A span outside the recording would put a cue in the wrong place in a video."""

    seen: list[Committed] = []

    pipeline = make_pipeline(duration_ms=4000)
    pipeline.on_segment = seen.append
    await run_to_completion(pipeline)

    assert seen
    assert all(0 <= committed.start_ms for committed in seen)
    assert all(committed.end_ms <= 4000 for committed in seen)


async def test_committed_spans_are_ordered_and_do_not_overlap() -> None:
    """Overlapping cues are legal in both formats and make an overlay flicker."""

    seen: list[Committed] = []

    pipeline = make_pipeline()
    pipeline.on_segment = seen.append
    await run_to_completion(pipeline)

    assert len(seen) >= 2
    assert [c.seq for c in seen] == sorted(c.seq for c in seen)

    for earlier, later in itertools.pairwise(seen):
        assert earlier.end_ms <= later.start_ms, (
            f"seq {earlier.seq} ends at {earlier.end_ms} but "
            f"seq {later.seq} starts at {later.start_ms}"
        )


async def test_a_cue_ends_when_speech_stops_not_when_the_segment_closes() -> None:
    """The gap between the two is the pause, and a cue covering it lingers.

    The segment closes 350 ms after the last voiced frame by design, so a span that
    reached the close would hold the previous line on screen through the one moment
    a viewer takes to look up.
    """

    seen: list[Committed] = []

    pipeline = make_pipeline(duration_ms=4000)
    pipeline.on_segment = seen.append
    await run_to_completion(pipeline)

    assert seen
    for committed in seen:
        # Speech runs for 1500 ms from a 2200 ms cycle, so a span that includes the
        # closing pause would be noticeably longer than the speech it describes.
        assert committed.end_ms - committed.start_ms <= 2000


async def test_a_broken_segment_sink_cannot_stop_the_lecture() -> None:
    """Writing a transcript is an extra, and an extra must never break the lecture."""

    def explode(_: Committed) -> None:
        raise RuntimeError("the transcript writer is broken")

    pipeline = make_pipeline()
    pipeline.on_segment = explode
    hub = await run_to_completion(pipeline)

    assert hub.segments
    assert any(s.state == "final" for s in hub.segments)
