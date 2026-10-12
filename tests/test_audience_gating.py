"""Tests for audience-gated paid work.

The requirement these pin: transcription and translation are billed, so they must run
only while a viewing device is connected. Capture continues across the pause, the
frames are dropped rather than buffered, and a resumed run must not replay stale audio
or segments nor reset ``seq``.

Only credential-free fakes are used. No test here opens a provider or touches a
network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from livesubs.asr.stub import StubAsr
from livesubs.audio.synthetic import SpeechLikeSource
from livesubs.events import SegmentMessage, WireMessage
from livesubs.hub import ClientRole, Hub
from livesubs.mt.base import MtChunk, MtContext
from livesubs.mt.passthrough import PassthroughMt
from livesubs.pipeline import LivePipeline, PipelineConfig
from livesubs.segmenter import SegmentBuffer


class RecordingHub(Hub):
    """A hub that keeps every published message, for observing a sequence."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[WireMessage] = []

    def publish(self, message: WireMessage) -> None:
        self.messages.append(message)
        super().publish(message)

    @property
    def segments(self) -> list:
        return [m.data for m in self.messages if isinstance(m, SegmentMessage)]


class LifecycleAsr:
    """A StubAsr wrapper that counts every lifecycle transition."""

    def __init__(self) -> None:
        self.inner = StubAsr()
        self.opens = 0
        self.closes = 0
        self.pushes = 0

    @property
    def healthy(self) -> bool:
        return self.inner.healthy

    async def open(self, cfg) -> None:
        self.opens += 1
        await self.inner.open(cfg)

    async def push(self, pcm: bytes) -> None:
        self.pushes += 1
        await self.inner.push(pcm)

    def events(self) -> AsyncIterator:
        return self.inner.events()

    async def close(self) -> None:
        self.closes += 1
        await self.inner.close()

    def seek(self, audio_ms: int) -> None:
        # Present so the pipeline's resume-alignment path is exercised.
        self.seeked_to = audio_ms


class AsrFactory:
    """Hands out fresh recognisers, recording each one for assertions."""

    def __init__(self) -> None:
        self.instances: list[LifecycleAsr] = []

    def __call__(self) -> LifecycleAsr:
        instance = LifecycleAsr()
        self.instances.append(instance)
        return instance


class BlockingMt(PassthroughMt):
    """A translator whose call never returns, to observe cancellation."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        yield MtChunk(text_delta="", done=True)  # pragma: no cover - unreachable


class CountingMt(PassthroughMt):
    def __init__(self) -> None:
        super().__init__()
        self.requests = 0

    async def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
        self.requests += 1
        async for chunk in super().translate(src, ctx):
            yield chunk


def gated_pipeline(mt: PassthroughMt | None = None) -> tuple[LivePipeline, AsrFactory]:
    factory = AsrFactory()
    first = factory()
    pipeline = LivePipeline(
        source=SpeechLikeSource(realtime=True),
        asr=first,
        asr_factory=factory,
        mt=mt if mt is not None else PassthroughMt(),
        config=PipelineConfig(telemetry_interval_ms=1000),
        require_audience=True,
    )
    return pipeline, factory


async def test_queued_mt_does_not_start_after_last_viewer_leaves() -> None:
    mt = CountingMt()
    pipeline, _ = gated_pipeline(mt)
    hub = RecordingHub()
    hub.enable_audience_gating()
    viewer = hub.subscribe(ClientRole.VIEWER)
    pipeline._hub = hub
    pipeline._audience_epoch = hub.demand_generation
    pipeline._audience_open = True
    pipeline._finals.append((0, "fixture", 0, 20))
    hub.unsubscribe(viewer)
    await pipeline._publish_loop()
    assert mt.requests == 0


async def test_shutdown_does_not_drain_mt_after_last_viewer_leaves() -> None:
    mt = CountingMt()
    pipeline, _ = gated_pipeline(mt)
    hub = RecordingHub()
    hub.enable_audience_gating()
    viewer = hub.subscribe(ClientRole.VIEWER)
    pipeline._hub = hub
    pipeline._audience_epoch = hub.demand_generation
    pipeline._audience_open = True
    pipeline._finals.append((0, "fixture", 0, 20))
    pipeline._publisher = asyncio.create_task(pipeline._publish_loop())
    hub.unsubscribe(viewer)
    buffer = SegmentBuffer(pipeline.config.segmenter, started_ms=0, last_voice_ms=0)
    await pipeline._finish(buffer)
    assert mt.requests == 0


async def wait_until(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not met before the timeout")
        await asyncio.sleep(0.01)


# --- no viewer means no paid work ----------------------------------------------


async def test_zero_audience_opens_and_warms_nothing() -> None:
    pipeline, factory = gated_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    try:
        await asyncio.sleep(0.25)
        assert factory.instances[0].opens == 0
        assert factory.instances[0].pushes == 0
        assert pipeline._mt_warmed is False
        assert pipeline.asr is not None and pipeline.asr.healthy is False
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def test_an_admin_connection_is_not_an_audience() -> None:
    """An operator console must never by itself create paid provider demand."""

    pipeline, factory = gated_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    admin = hub.subscribe(ClientRole.ADMIN)
    try:
        await asyncio.sleep(0.25)
        assert hub.viewer_count == 0
        assert factory.instances[0].opens == 0
        assert pipeline._mt_warmed is False
    finally:
        hub.unsubscribe(admin)
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def test_stop_while_paused_returns_promptly() -> None:
    """Waiting for a viewer must not deadlock shutdown."""

    pipeline, _ = gated_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    await asyncio.sleep(0.15)

    stop.set()
    await asyncio.wait_for(task, timeout=5.0)


# --- a viewer opens the providers, leaving closes them --------------------------


async def test_a_viewer_opens_providers_and_warms_translation() -> None:
    pipeline, factory = gated_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: factory.instances[0].opens == 1)
        # The recogniser opens first, then German is warmed by its own cancellable
        # step, so the warm is observed rather than assumed to precede the open.
        await wait_until(lambda: pipeline._mt_warmed is True)
        # Speech-like audio keeps flowing, so a segment should be produced.
        await wait_until(lambda: any(s.state == "final" for s in hub.segments))
    finally:
        hub.unsubscribe(viewer)
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def test_the_last_viewer_leaving_closes_the_provider() -> None:
    pipeline, factory = gated_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: factory.instances[0].opens == 1)
        hub.unsubscribe(viewer)
        await wait_until(lambda: factory.instances[0].closes >= 1)
        assert pipeline._audience_open is False
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def test_a_second_viewer_does_not_reopen_after_the_first_left() -> None:
    """Only the transition to and from zero viewers changes the provider state."""

    pipeline, factory = gated_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    first = hub.subscribe(ClientRole.VIEWER)
    second = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: factory.instances[0].opens == 1)
        hub.unsubscribe(first)
        await asyncio.sleep(0.15)
        assert factory.instances[0].closes == 0     # one viewer still present
        assert pipeline._audience_open is True
    finally:
        hub.unsubscribe(second)
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


# --- resume is fresh, ordered and continuous -----------------------------------


async def test_resume_uses_a_fresh_provider_and_keeps_seq_monotonic() -> None:
    pipeline, factory = gated_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    first = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: any(s.state == "final" for s in hub.segments))
        before = [s.seq for s in hub.segments if s.state == "final"]

        hub.unsubscribe(first)
        await wait_until(lambda: not pipeline._audience_open)

        # A reconnect must build a fresh recogniser, not reopen the closed one.
        second = hub.subscribe(ClientRole.VIEWER)
        await wait_until(lambda: len(factory.instances) >= 2)
        await wait_until(
            lambda: len([s for s in hub.segments if s.state == "final"]) > len(before)
        )
        hub.unsubscribe(second)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)

    keys = [(s.seq, s.rev) for s in hub.segments]
    assert keys == sorted(keys), "seq/rev regressed across the audience pause"
    finals = [s.seq for s in hub.segments if s.state == "final"]
    # The sequence continued where it left off rather than restarting at zero.
    assert finals[-1] >= 1


async def test_pausing_forgets_the_cached_line_so_no_stale_text_is_replayed() -> None:
    pipeline, _factory = gated_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: any(isinstance(m, SegmentMessage) for m in hub.messages))
        assert hub._latest_segment is not None
        hub.unsubscribe(viewer)
        await wait_until(lambda: not pipeline._audience_open)
        assert hub._latest_segment is None
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def test_in_flight_translation_is_cancelled_when_the_audience_leaves() -> None:
    """No translation request may outlive the last viewer."""

    mt = BlockingMt()
    pipeline, factory = gated_pipeline(mt=mt)
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: factory.instances[0].opens == 1)
        await wait_until(lambda: mt.started.is_set(), timeout=5.0)
        hub.unsubscribe(viewer)
        await wait_until(lambda: mt.cancelled.is_set(), timeout=5.0)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def test_the_audio_clock_keeps_running_across_a_pause() -> None:
    """Capture continues, so resumed cues refer to the same rising audio clock.

    Restarting capture would reset ``audio_ms`` and break the alignment between a
    cue and the archived recording.
    """

    pipeline, _factory = gated_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    first = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: any(s.state == "final" for s in hub.segments))
        before = max(s.timing.audio_ms for s in hub.segments if s.state == "final")

        hub.unsubscribe(first)
        await wait_until(lambda: not pipeline._audience_open)
        # Let a little real audio pass with no audience: the clock must still advance.
        await asyncio.sleep(0.4)

        second = hub.subscribe(ClientRole.VIEWER)
        await wait_until(
            lambda: any(s.state == "final" and s.timing.audio_ms > before for s in hub.segments),
            timeout=6.0,
        )
        hub.unsubscribe(second)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


# --- lifecycle races the frame-driven design could not handle -------------------


class BlockingWarmMt(PassthroughMt):
    """A translator whose warm call blocks until cancelled."""

    def __init__(self) -> None:
        super().__init__()
        self.warm_started = asyncio.Event()
        self.warm_cancelled = asyncio.Event()

    async def warm(self) -> None:
        self.warm_started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.warm_cancelled.set()
            raise
        self._warmed = True  # pragma: no cover - unreachable while blocking


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


def make_gated(source, *, asr, mt, asr_factory=None) -> LivePipeline:
    return LivePipeline(
        source=source,
        asr=asr,
        asr_factory=asr_factory,
        mt=mt,
        config=PipelineConfig(telemetry_interval_ms=1000),
        require_audience=True,
    )


async def test_disconnect_during_blocked_german_warm_cancels_the_warm() -> None:
    """A German viewer who leaves mid-warm must cancel the billed warm promptly.

    The shared recogniser is opened independently of the German warm, so with the
    audience now empty it is torn down too; the guarantees under test are that no
    German worker is started, no warm keeps running, and the warm latch is not set by
    the aborted call.
    """

    mt = BlockingWarmMt()
    asr = ControllableAsr()
    pipeline = make_gated(SpeechLikeSource(realtime=True), asr=asr, mt=mt)
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: mt.warm_started.is_set())

        hub.unsubscribe(viewer)
        await wait_until(lambda: mt.warm_cancelled.is_set())
        await wait_until(lambda: asr.closed, timeout=5.0)
        assert pipeline._de.active is False
        assert pipeline._mt_warmed is False
        assert pipeline._de.warmed is False
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=6.0)


async def test_disconnect_during_blocked_open_cancels_and_closes() -> None:
    asr = ControllableAsr(block_open=True)
    pipeline = make_gated(SpeechLikeSource(realtime=True), asr=asr, mt=PassthroughMt())
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: asr.open_calls == 1)
        hub.unsubscribe(viewer)
        await wait_until(lambda: asr.closed, timeout=5.0)
        assert pipeline._audience_open is False
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=6.0)


async def test_stalled_capture_disconnect_cancels_paid_work_without_a_frame() -> None:
    """The watcher acts on a disconnect even when no further frame ever arrives."""

    source = StallingSource(SpeechLikeSource(realtime=True), frames_before_stall=140)
    factory = AsrFactory()
    first = factory()
    mt = BlockingMt()
    pipeline = make_gated(source, asr=first, mt=mt, asr_factory=factory)
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: mt.started.is_set(), timeout=8.0)
        await wait_until(lambda: source.stalled, timeout=5.0)
        delivered = source.delivered

        hub.unsubscribe(viewer)
        await wait_until(lambda: mt.cancelled.is_set(), timeout=5.0)
        await wait_until(lambda: first.closes >= 1, timeout=5.0)
        # No frame was needed to trigger any of it.
        assert source.delivered == delivered
    finally:
        await source.close()
        stop.set()
        await asyncio.wait_for(task, timeout=6.0)


async def test_immediate_reconnect_has_no_stale_seed_and_a_fresh_provider() -> None:
    pipeline, factory = gated_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    first = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: any(s.state == "final" for s in hub.segments))
        assert hub._latest_segment is not None

        hub.unsubscribe(first)
        # Synchronous clear, before any reconnect handler can run.
        assert hub._latest_segment is None

        second = hub.subscribe(ClientRole.VIEWER)
        # The seeded mailbox cannot carry the old line.
        assert second.mailbox._segment is None

        await wait_until(lambda: len(factory.instances) >= 2, timeout=6.0)
        hub.unsubscribe(second)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=6.0)


async def test_stop_while_blocked_opening_returns_promptly() -> None:
    asr = ControllableAsr(block_open=True)
    pipeline = make_gated(SpeechLikeSource(realtime=True), asr=asr, mt=PassthroughMt())
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    hub.subscribe(ClientRole.VIEWER)
    await wait_until(lambda: asr.open_calls == 1)

    stop.set()
    await asyncio.wait_for(task, timeout=8.0)


async def test_seek_failure_closes_the_provider() -> None:
    """A raising seek is inside the open cleanup guard, so it must close the ASR."""

    factory = ControllableAsrFactory()
    first = ControllableAsr(seek_raises=True)
    pipeline = make_gated(
        SpeechLikeSource(realtime=True), asr=first, mt=PassthroughMt(), asr_factory=factory
    )
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: first.closed, timeout=6.0)
        # The failed attempt was cleaned up, and a fresh provider was retried.
        await wait_until(lambda: bool(factory.instances), timeout=6.0)
        assert factory.instances[0].opened is True
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=6.0)


async def test_first_open_failure_retries_with_a_fresh_provider() -> None:
    """A single open failure must not latch the pipeline while viewers remain."""

    factory = ControllableAsrFactory()
    first = ControllableAsr(fail_open_times=1)
    pipeline = make_gated(
        SpeechLikeSource(realtime=True), asr=first, mt=PassthroughMt(), asr_factory=factory
    )
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: pipeline._audience_open, timeout=8.0)
        assert factory.instances
        assert factory.instances[0].opened is True
    finally:
        hub.unsubscribe(viewer)
        stop.set()
        await asyncio.wait_for(task, timeout=6.0)



async def test_an_ended_provider_stream_stops_paid_work_and_retries() -> None:
    """An ASR that hangs up on its own must not leave a dead provider in place."""

    factory = ControllableAsrFactory()
    first = ControllableAsr(end_events_after=0.3)
    pipeline = make_gated(
        SpeechLikeSource(realtime=True), asr=first, mt=PassthroughMt(), asr_factory=factory
    )
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="gated", stop=stop))
    viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        # The first stream ends; the watcher tears it down and builds a fresh one.
        await wait_until(lambda: bool(factory.instances), timeout=8.0)
        assert first.closed is True
        assert factory.instances[0].opened is True
    finally:
        hub.unsubscribe(viewer)
        stop.set()
        await asyncio.wait_for(task, timeout=6.0)
