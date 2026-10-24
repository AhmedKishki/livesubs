"""Paid work follows the audience.

A recogniser stream is billed per second and a translator per request, so neither may run
for an empty room. These pin the lifecycle: nothing opens until a viewer arrives, it opens
once however many arrive, it closes when the last leaves, it survives a failure by starting
again, and no cancelled open, warm or request outlives the audience it was for.

Only credential-free fakes are used. No test opens a provider or touches a network. The
behaviour is the same for either speaker, so these run under both modes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from support import (
    MODE_IDS,
    MODES,
    AsrFactory,
    BlockingMt,
    BlockingWarmMt,
    CountingMt,
    RecordingHub,
    finals,
    make_gated,
    running,
    wait_until,
)

from livesubs.asr.base import AsrConfig
from livesubs.audio.synthetic import SpeechLikeSource
from livesubs.events import SegmentMessage
from livesubs.hub import ClientRole
from livesubs.mt.base import MtChunk, MtContext
from livesubs.mt.passthrough import PassthroughMt
from livesubs.pipeline.records import PipelineConfig, SourceSegment


@pytest.fixture(params=MODES, ids=MODE_IDS)
def mode(request):
    """``(speaker, (default target, other target))``."""

    return request.param


def viewer(language: str) -> ClientRole:
    return ClientRole.viewer(language)


def translators(mode, make=CountingMt):
    """The default language's translator now, as in a real start, and the rest lazily."""

    _, (first, other) = mode
    return {first: make(), other: make}


def gated(mode, mt=None, **kwargs):
    speaker, (first, _) = mode
    table = translators(mode)
    if mt is not None:
        table[first] = mt
    # These tests exercise paid translation cancellation, not transcription routing.
    # The scripted recogniser emits English. A contradictory German pin must
    # conservatively require MT, even for an English target.
    kwargs.setdefault("config", PipelineConfig(asr=AsrConfig(language="de")))
    return make_gated(translators=table, source_language=speaker, **kwargs)


def hub_for(mode) -> RecordingHub:
    return RecordingHub(mode[1])


# --- no viewer means no paid work ----------------------------------------------


async def test_zero_audience_opens_and_warms_nothing(mode) -> None:
    mt = CountingMt()
    pipeline, factory = gated(mode, mt)
    hub = hub_for(mode)

    async with running(pipeline, hub):
        await asyncio.sleep(0.25)
        assert factory.instances[0].opens == 0
        assert factory.instances[0].pushes == 0
        assert mt.warms == 0
        assert not pipeline._asr.healthy


async def test_an_admin_connection_is_not_an_audience(mode) -> None:
    """An operator console must never by itself create paid provider demand."""

    mt = CountingMt()
    pipeline, factory = gated(mode, mt)
    hub = hub_for(mode)

    async with running(pipeline, hub):
        admin = hub.subscribe(ClientRole.admin())
        await asyncio.sleep(0.25)
        assert hub.viewer_count == 0
        assert factory.instances[0].opens == 0
        assert mt.warms == 0
        hub.unsubscribe(admin)


async def test_stop_while_paused_returns_promptly(mode) -> None:
    """Waiting for a viewer must not deadlock shutdown."""

    pipeline, _ = gated(mode)
    hub = hub_for(mode)
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    await asyncio.sleep(0.15)

    stop.set()
    await asyncio.wait_for(task, timeout=5.0)


async def test_shutdown_is_not_held_up_by_idle_supervisors(mode) -> None:
    """Every supervisor waits on its audience, and each must also wake for shutdown.

    One of them used to wait the full two-second grace for a wake-up that never came, on
    every stop, leaving a third of the session's shutdown budget for flushing the last line.
    """

    pipeline, _ = gated(mode)
    hub = hub_for(mode)
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    await asyncio.sleep(0.15)
    started = asyncio.get_running_loop().time()
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)

    assert asyncio.get_running_loop().time() - started < 0.5


# --- a viewer opens the providers, leaving closes them --------------------------


async def test_a_viewer_opens_the_recogniser_and_warms_their_language(mode) -> None:
    mt = CountingMt()
    pipeline, factory = gated(mode, mt)
    hub = hub_for(mode)
    first = mode[1][0]

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(first))
        await wait_until(lambda: factory.instances[0].opens == 1)
        await wait_until(lambda: mt.warms == 1)
        # Speech-like audio keeps flowing, so a segment should be produced.
        await wait_until(lambda: bool(finals(hub, first)))
        hub.unsubscribe(reader)


async def test_the_last_viewer_leaving_closes_the_provider(mode) -> None:
    pipeline, factory = gated(mode)
    hub = hub_for(mode)

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(mode[1][0]))
        await wait_until(lambda: factory.instances[0].opens == 1)
        hub.unsubscribe(reader)
        await wait_until(lambda: factory.instances[0].closes >= 1)
        assert not pipeline._asr.is_open


async def test_a_second_viewer_does_not_reopen_after_the_first_left(mode) -> None:
    """Only the transition to and from zero viewers changes the provider state."""

    pipeline, factory = gated(mode)
    hub = hub_for(mode)
    first_language = mode[1][0]

    async with running(pipeline, hub):
        first = hub.subscribe(viewer(first_language))
        second = hub.subscribe(viewer(first_language))
        await wait_until(lambda: factory.instances[0].opens == 1)
        hub.unsubscribe(first)
        await asyncio.sleep(0.15)
        assert factory.instances[0].closes == 0     # one viewer still present
        assert pipeline._asr.is_open
        hub.unsubscribe(second)


# --- no translation request outlives the audience it was for ---------------------------


async def test_queued_translations_do_not_start_after_the_last_viewer_leaves(mode) -> None:
    class Slow(CountingMt):
        async def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
            await asyncio.sleep(0.4)
            async for chunk in super().translate(src, ctx):
                yield chunk

    mt = Slow()
    pipeline, _ = gated(mode, mt)
    hub = hub_for(mode)

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(mode[1][0]))
        await wait_until(lambda: mt.warms == 1 and mt.requests >= 1, timeout=6.0)
        hub.unsubscribe(reader)
        started_before = mt.requests
        await asyncio.sleep(1.0)
        assert mt.requests == started_before, "a queued segment was translated for nobody"


async def test_shutdown_does_not_drain_translations_after_the_last_viewer_left(mode) -> None:
    mt = CountingMt()
    pipeline, _ = gated(mode, mt)
    hub = hub_for(mode)

    async with running(pipeline, hub) as stop:
        reader = hub.subscribe(viewer(mode[1][0]))
        await wait_until(lambda: mt.requests >= 1, timeout=6.0)
        hub.unsubscribe(reader)
        requests_at_leaving = mt.requests
        stop.set()
        await asyncio.sleep(0.2)

    assert mt.requests <= requests_at_leaving + 1


async def test_in_flight_translation_is_cancelled_when_the_audience_leaves(mode) -> None:
    """No translation request may outlive the last viewer."""

    mt = BlockingMt()
    pipeline, factory = gated(mode, mt)
    hub = hub_for(mode)

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(mode[1][0]))
        await wait_until(lambda: factory.instances[0].opens == 1)
        await wait_until(lambda: mt.started.is_set(), timeout=5.0)
        hub.unsubscribe(reader)
        await wait_until(lambda: mt.cancelled.is_set(), timeout=5.0)


# --- resume is fresh, ordered and continuous -----------------------------------


async def test_resume_uses_a_fresh_provider_and_keeps_seq_monotonic(mode) -> None:
    pipeline, factory = gated(mode)
    hub = hub_for(mode)
    language = mode[1][0]

    async with running(pipeline, hub):
        first = hub.subscribe(viewer(language))
        await wait_until(lambda: bool(finals(hub, language)))
        before = len(finals(hub, language))

        hub.unsubscribe(first)
        await wait_until(lambda: not pipeline._asr.is_open)

        # A reconnect must build a fresh recogniser, not reopen the closed one.
        second = hub.subscribe(viewer(language))
        await wait_until(lambda: len(factory.instances) >= 2)
        await wait_until(lambda: len(finals(hub, language)) > before)
        hub.unsubscribe(second)

    keys = [(s.seq, s.rev) for s in hub.segments if s.language == language]
    assert keys == sorted(keys), "seq/rev regressed across the audience pause"
    # The sequence continued where it left off rather than restarting at zero.
    assert finals(hub, language)[-1].seq >= 1


async def test_pausing_forgets_the_cached_line_so_no_stale_text_is_replayed(mode) -> None:
    pipeline, _ = gated(mode)
    hub = hub_for(mode)
    language = mode[1][0]

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(language))
        await wait_until(lambda: any(isinstance(m, SegmentMessage) for m in hub.messages))
        hub.unsubscribe(reader)
        await wait_until(lambda: not pipeline._asr.is_open)
        await wait_until(lambda: not pipeline._workers[language].active)

        assert not hub.subscribe(viewer(language)).mailbox.has_pending


async def test_the_audio_clock_keeps_running_across_a_pause(mode) -> None:
    """Capture continues, so resumed cues refer to the same rising audio clock.

    Restarting capture would reset ``audio_ms`` and break the alignment between a
    cue and the archived recording.
    """

    pipeline, _ = gated(mode)
    hub = hub_for(mode)
    language = mode[1][0]

    async with running(pipeline, hub):
        first = hub.subscribe(viewer(language))
        await wait_until(lambda: bool(finals(hub, language)))
        before = max(s.timing.audio_ms for s in finals(hub, language))

        hub.unsubscribe(first)
        await wait_until(lambda: not pipeline._asr.is_open)
        # Let a little real audio pass with no audience: the clock must still advance.
        await asyncio.sleep(0.4)

        second = hub.subscribe(viewer(language))
        await wait_until(
            lambda: any(s.timing.audio_ms > before for s in finals(hub, language)),
            timeout=6.0,
        )
        hub.unsubscribe(second)


async def test_immediate_reconnect_has_no_stale_seed_and_a_fresh_provider(mode) -> None:
    pipeline, factory = gated(mode)
    hub = hub_for(mode)
    language = mode[1][0]

    async with running(pipeline, hub):
        first = hub.subscribe(viewer(language))
        await wait_until(lambda: bool(finals(hub, language)))

        hub.unsubscribe(first)
        # Synchronous clear, before any reconnect handler can run: the seeded mailbox of
        # a viewer who returns at once cannot carry the old line.
        second = hub.subscribe(viewer(language))
        assert not second.mailbox.has_pending

        await wait_until(lambda: len(factory.instances) >= 2, timeout=6.0)
        hub.unsubscribe(second)


# --- lifecycle races the frame-driven design could not handle -------------------


class ControllableAsr:
    """An ASR whose open can block, fail, or whose seek can raise."""

    def __init__(
        self,
        *,
        block_open: bool = False,
        fail_open_times: int = 0,
        seek_raises: bool = False,
        end_events_after: float | None = None,
    ) -> None:
        self.block_open = block_open
        self.fail_open_times = fail_open_times
        self.seek_raises = seek_raises
        self.end_events_after = end_events_after
        self.open_calls = 0
        self.opened = False
        self.closed = False
        self.open_cancelled = asyncio.Event()
        self._release = asyncio.Event()

    @property
    def healthy(self) -> bool:
        return self.opened

    async def open(self, cfg) -> None:
        self.open_calls += 1
        if self.fail_open_times > 0:
            self.fail_open_times -= 1
            raise RuntimeError("open failed")
        if self.block_open:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.open_cancelled.set()
                raise
        self.opened = True

    def seek(self, audio_ms: int) -> None:
        if self.seek_raises:
            raise RuntimeError("seek failed")

    async def push(self, pcm: bytes) -> None:
        pass

    async def events(self) -> AsyncIterator:
        if self.end_events_after is not None:
            # The provider hangs up on its own after a moment.
            try:
                await asyncio.wait_for(self._release.wait(), timeout=self.end_events_after)
            except TimeoutError:
                return
        await self._release.wait()
        if False:  # pragma: no cover - makes this an async generator
            yield

    async def close(self) -> None:
        self.closed = True
        self._release.set()


class ControllableAsrFactory:
    """Hands out controllable recognisers, recording each one."""

    def __init__(self, **kwargs) -> None:
        self._kwargs = kwargs
        self.instances: list[ControllableAsr] = []

    def __call__(self) -> ControllableAsr:
        instance = ControllableAsr(**self._kwargs)
        self.instances.append(instance)
        return instance


class StallingSource:
    """Replays a fixed number of frames, then stalls until released."""

    def __init__(self, inner, *, frames_before_stall: int) -> None:
        self._inner = inner
        self._limit = frames_before_stall
        self._release = asyncio.Event()
        self._closed = False
        self.delivered = 0
        self.stalled = False

    async def frames(self) -> AsyncIterator:
        async for frame in self._inner.frames():
            yield frame
            self.delivered += 1
            if self.delivered >= self._limit:
                break
        self.stalled = True
        await self._release.wait()

    async def close(self) -> None:
        self._closed = True
        self._release.set()
        await self._inner.close()

    @property
    def alive(self) -> bool:
        return not self._closed


def with_recogniser(mode, asr, *, mt=None, factory=None, source=None):
    """A gated pipeline around a controllable recogniser."""

    from livesubs.pipeline import LivePipeline, PipelineConfig

    speaker, (first, other) = mode
    return LivePipeline(
        source=source if source is not None else SpeechLikeSource(realtime=True),
        asr=asr,
        asr_factory=factory,
        translators={first: mt if mt is not None else PassthroughMt(), other: PassthroughMt},
        source_language=speaker,
        config=PipelineConfig(telemetry_interval_ms=1000, asr=AsrConfig(language="de")),
        require_audience=True,
    )


async def test_disconnect_during_blocked_warm_cancels_the_warm(mode) -> None:
    """A viewer who leaves mid-warm must cancel the billed warm promptly.

    The worker starts without warming. Its first translation owns the cancellable warm;
    losing demand cancels that work and does not set the warmed latch.
    """

    mt = BlockingWarmMt()
    asr = ControllableAsr()
    pipeline = with_recogniser(mode, asr, mt=mt)
    hub = hub_for(mode)
    language = mode[1][0]

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(language))
        await wait_until(lambda: pipeline._workers[language].active)
        pipeline._dispatch(SourceSegment(0, "unlabelled speech", 0, 100))
        await wait_until(lambda: mt.warm_started.is_set())

        hub.unsubscribe(reader)
        await wait_until(lambda: mt.warm_cancelled.is_set())
        await wait_until(lambda: asr.closed, timeout=5.0)
        worker = pipeline._workers[language]
        assert worker.active is False
        assert worker.warmed is False


async def test_disconnect_during_blocked_open_cancels_and_closes(mode) -> None:
    asr = ControllableAsr(block_open=True)
    pipeline = with_recogniser(mode, asr)
    hub = hub_for(mode)

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(mode[1][0]))
        await wait_until(lambda: asr.open_calls == 1)
        hub.unsubscribe(reader)
        await wait_until(lambda: asr.closed, timeout=5.0)
        assert not pipeline._asr.is_open


async def test_stalled_capture_disconnect_cancels_paid_work_without_a_frame(mode) -> None:
    """The supervisor acts on a disconnect even when no further frame ever arrives."""

    source = StallingSource(SpeechLikeSource(realtime=True), frames_before_stall=140)
    factory = AsrFactory()
    first = factory()
    mt = BlockingMt()
    pipeline = with_recogniser(mode, first, mt=mt, factory=factory, source=source)
    hub = hub_for(mode)

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(mode[1][0]))
        try:
            await wait_until(lambda: mt.started.is_set(), timeout=8.0)
            await wait_until(lambda: source.stalled, timeout=5.0)
            delivered = source.delivered

            hub.unsubscribe(reader)
            await wait_until(lambda: mt.cancelled.is_set(), timeout=5.0)
            await wait_until(lambda: first.closes >= 1, timeout=5.0)
            # No frame was needed to trigger any of it.
            assert source.delivered == delivered
        finally:
            # A stalled capture only notices a stop request when it is released.
            await source.close()


async def test_stop_while_blocked_opening_returns_promptly(mode) -> None:
    asr = ControllableAsr(block_open=True)
    pipeline = with_recogniser(mode, asr)
    hub = hub_for(mode)
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    hub.subscribe(viewer(mode[1][0]))
    await wait_until(lambda: asr.open_calls == 1)

    stop.set()
    await asyncio.wait_for(task, timeout=8.0)


async def test_seek_failure_closes_the_provider(mode) -> None:
    """A raising seek is inside the open cleanup guard, so it must close the ASR."""

    factory = ControllableAsrFactory()
    first = ControllableAsr(seek_raises=True)
    pipeline = with_recogniser(mode, first, factory=factory)
    hub = hub_for(mode)

    async with running(pipeline, hub):
        hub.subscribe(viewer(mode[1][0]))
        await wait_until(lambda: first.closed, timeout=6.0)
        # The failed attempt was cleaned up, and a fresh provider was retried.
        await wait_until(lambda: bool(factory.instances), timeout=6.0)
        assert factory.instances[0].opened is True


async def test_first_open_failure_retries_with_a_fresh_provider(mode) -> None:
    """A single open failure must not latch the pipeline while viewers remain."""

    factory = ControllableAsrFactory()
    first = ControllableAsr(fail_open_times=1)
    pipeline = with_recogniser(mode, first, factory=factory)
    hub = hub_for(mode)

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(mode[1][0]))
        await wait_until(lambda: pipeline._asr.is_open, timeout=8.0)
        assert factory.instances
        assert factory.instances[0].opened is True
        hub.unsubscribe(reader)


async def test_an_ended_provider_stream_stops_paid_work_and_retries(mode) -> None:
    """An ASR that hangs up on its own must not leave a dead provider in place."""

    factory = ControllableAsrFactory()
    first = ControllableAsr(end_events_after=0.3)
    pipeline = with_recogniser(mode, first, factory=factory)
    hub = hub_for(mode)

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(mode[1][0]))
        # The first stream ends; the supervisor tears it down and builds a fresh one.
        await wait_until(lambda: bool(factory.instances), timeout=8.0)
        assert first.closed is True
        assert factory.instances[0].opened is True
        hub.unsubscribe(reader)


async def test_viewers_are_told_while_the_recogniser_is_down_and_when_it_is_back(mode) -> None:
    """A dropped recogniser used to leave the status at live for the rest of the lecture."""

    factory = ControllableAsrFactory()
    first = ControllableAsr(end_events_after=0.3)
    pipeline = with_recogniser(mode, first, factory=factory)
    hub = hub_for(mode)

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(mode[1][0]))
        await wait_until(
            lambda: ("delayed", "recognition is reconnecting", None) in hub.status_events,
            timeout=8.0,
        )
        await wait_until(lambda: hub.status_events[-1][0] == "live", timeout=8.0)
        hub.unsubscribe(reader)


async def test_a_recogniser_that_dies_without_ending_its_stream_is_replaced(mode) -> None:
    """Some failures never end the stream; ``healthy`` is the second line of defence."""

    class GoesQuiet(ControllableAsr):
        @property
        def healthy(self) -> bool:
            return self.opened and not self.closed and self.open_calls < 0

    factory = ControllableAsrFactory()
    first = GoesQuiet()
    pipeline = with_recogniser(mode, first, factory=factory)
    pipeline.config.health_check_ms = 100
    hub = hub_for(mode)

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(mode[1][0]))
        await wait_until(lambda: first.closed, timeout=8.0)
        await wait_until(lambda: bool(factory.instances), timeout=8.0)
        hub.unsubscribe(reader)
