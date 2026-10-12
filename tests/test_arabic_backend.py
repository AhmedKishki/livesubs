"""Tests for demand-driven parallel German and Arabic subtitles.

The requirement these pin: one shared English recogniser feeds two independent,
ordered translation streams. Arabic is opt-in, so a German-only lecture never
constructs or warms an Arabic translator; an Arabic viewer joining while German
viewers are present must not restart the recogniser or interrupt German; and the
last Arabic viewer leaving cancels only Arabic work.

Only credential-free fakes are used. No test opens a provider or touches a network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from livesubs.asr.stub import StubAsr
from livesubs.audio.synthetic import SpeechLikeSource
from livesubs.config import Settings
from livesubs.events import (
    SegmentMessage,
    SubtitleSegment,
    Timing,
    WireMessage,
)
from livesubs.hub import ClientRole, Hub
from livesubs.mt.base import MtChunk, MtContext
from livesubs.mt.passthrough import PassthroughMt
from livesubs.pipeline import Committed, LivePipeline, PipelineConfig
from livesubs.server import create_app
from livesubs.subtitles import SubtitleRecorder


class RecordingHub(Hub):
    """A hub that keeps every published message, for observing a sequence."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[WireMessage] = []

    def publish(self, message: WireMessage) -> None:
        self.messages.append(message)
        super().publish(message)

    @property
    def segments(self) -> list[SubtitleSegment]:
        return [m.data for m in self.messages if isinstance(m, SegmentMessage)]


class LifecycleAsr:
    """A StubAsr wrapper that counts every lifecycle transition."""

    def __init__(self) -> None:
        self.inner = StubAsr()
        self.opens = 0
        self.closes = 0

    @property
    def healthy(self) -> bool:
        return self.inner.healthy

    async def open(self, cfg) -> None:
        self.opens += 1
        await self.inner.open(cfg)

    async def push(self, pcm: bytes) -> None:
        await self.inner.push(pcm)

    def events(self) -> AsyncIterator:
        return self.inner.events()

    async def close(self) -> None:
        self.closes += 1
        await self.inner.close()

    def seek(self, audio_ms: int) -> None:
        pass


class AsrFactory:
    def __init__(self) -> None:
        self.instances: list[LifecycleAsr] = []

    def __call__(self) -> LifecycleAsr:
        instance = LifecycleAsr()
        self.instances.append(instance)
        return instance


class CountingMt(PassthroughMt):
    """Counts constructions, warms and translate calls."""

    def __init__(self) -> None:
        super().__init__()
        self.warms = 0
        self.requests = 0

    async def warm(self) -> None:
        self.warms += 1
        await super().warm()

    async def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
        self.requests += 1
        async for chunk in super().translate(src, ctx):
            yield chunk


class BlockingMt(PassthroughMt):
    """A translator whose translate call blocks until cancelled."""

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
        yield MtChunk(text_delta="", done=True)  # pragma: no cover


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
        self._warmed = True  # pragma: no cover


class CountingArFactory:
    """Builds Arabic translators lazily, recording every construction."""

    def __init__(self, make: type[PassthroughMt] = CountingMt) -> None:
        self._make = make
        self.instances: list[PassthroughMt] = []

    def __call__(self) -> PassthroughMt:
        instance = self._make()
        self.instances.append(instance)
        return instance


def make_pipeline(
    *,
    de_mt: PassthroughMt | None = None,
    ar_factory: CountingArFactory | None = None,
    realtime: bool = False,
) -> tuple[LivePipeline, AsrFactory]:
    factory = AsrFactory()
    first = factory()
    pipeline = LivePipeline(
        source=SpeechLikeSource(realtime=realtime),
        asr=first,
        asr_factory=factory,
        mt=de_mt if de_mt is not None else PassthroughMt(),
        mt_ar_factory=ar_factory if ar_factory is not None else CountingArFactory(),
        config=PipelineConfig(telemetry_interval_ms=1000),
        require_audience=True,
    )
    return pipeline, factory


async def wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not met before the timeout")
        await asyncio.sleep(0.005)


def finals(hub: RecordingHub, language: str | None = None) -> list[SubtitleSegment]:
    return [
        s
        for s in hub.segments
        if s.state == "final" and (language is None or s.language == language)
    ]


# --- Arabic is off until requested --------------------------------------------


async def test_arabic_provider_is_never_built_or_warmed_without_an_arabic_viewer() -> None:
    ar_factory = CountingArFactory()
    de_mt = CountingMt()
    pipeline, factory = make_pipeline(de_mt=de_mt, ar_factory=ar_factory)
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="ar", stop=stop))
    try:
        await asyncio.sleep(0.2)
        assert ar_factory.instances == []          # never constructed
        assert pipeline._ar.warmed is False        # never warmed
        assert factory.instances[0].opens == 0
        assert de_mt.warms == 0
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


def test_supported_languages_reports_arabic_only_when_configured() -> None:
    assert make_pipeline()[0].supported_languages == ("de", "ar")
    no_ar = LivePipeline(
        source=SpeechLikeSource(realtime=False),
        asr=StubAsr(),
        mt=PassthroughMt(),
    )
    assert no_ar.supported_languages == ("de",)


async def test_only_an_arabic_viewer_activates_asr_and_arabic_but_not_german() -> None:
    ar_factory = CountingArFactory()
    de_mt = CountingMt()
    pipeline, factory = make_pipeline(de_mt=de_mt, ar_factory=ar_factory)
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="ar", stop=stop))
    viewer = hub.subscribe(ClientRole.VIEWER_AR)
    try:
        await wait_until(lambda: factory.instances[0].opens == 1)
        await wait_until(lambda: bool(finals(hub, "ar")))
        assert de_mt.warms == 0
        assert de_mt.requests == 0
        assert pipeline._mt_warmed is False
        assert not finals(hub, "de")
    finally:
        hub.unsubscribe(viewer)
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


# --- Arabic joins and leaves without disturbing German -------------------------


async def test_arabic_joins_while_german_present_without_restarting_asr() -> None:
    ar_factory = CountingArFactory()
    pipeline, factory = make_pipeline(ar_factory=ar_factory)
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="ar", stop=stop))
    de_viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: bool(finals(hub, "de")))
        opens_before = factory.instances[0].opens
        assert opens_before == 1

        ar_viewer = hub.subscribe(ClientRole.VIEWER_AR)
        await wait_until(lambda: bool(finals(hub, "ar")))
        # The shared recogniser was not rebuilt for the new language.
        assert factory.instances[0].opens == 1
        assert len(factory.instances) == 1
        hub.unsubscribe(ar_viewer)
    finally:
        hub.unsubscribe(de_viewer)
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def test_slow_arabic_does_not_stall_german_segments() -> None:
    ar_mt = BlockingMt()
    ar_factory = CountingArFactory(make=lambda: ar_mt)  # type: ignore[arg-type]

    de_mt = CountingMt()
    pipeline, _ = make_pipeline(de_mt=de_mt, ar_factory=ar_factory)
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="ar", stop=stop))
    de_viewer = hub.subscribe(ClientRole.VIEWER)
    ar_viewer = hub.subscribe(ClientRole.VIEWER_AR)
    try:
        await wait_until(lambda: ar_mt.started.is_set())
        # German keeps finishing while Arabic is stuck in one blocked request.
        await wait_until(lambda: len(finals(hub, "de")) >= 2)
        assert not finals(hub, "ar")            # the blocked Arabic line never finished
    finally:
        hub.unsubscribe(ar_viewer)
        hub.unsubscribe(de_viewer)
        stop.set()
        await asyncio.wait_for(task, timeout=8.0)


async def test_last_arabic_viewer_cancels_only_arabic_work() -> None:
    ar_mt = BlockingMt()
    ar_factory = CountingArFactory(make=lambda: ar_mt)  # type: ignore[arg-type]

    pipeline, factory = make_pipeline(ar_factory=ar_factory)
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="ar", stop=stop))
    de_viewer = hub.subscribe(ClientRole.VIEWER)
    ar_viewer = hub.subscribe(ClientRole.VIEWER_AR)
    try:
        await wait_until(lambda: bool(finals(hub, "de")))
        de_finals_before = len(finals(hub, "de"))
        await wait_until(lambda: ar_mt.started.is_set())

        hub.unsubscribe(ar_viewer)
        await wait_until(lambda: ar_mt.cancelled.is_set(), timeout=5.0)
        # The recogniser and German stream survive, and German keeps flowing.
        assert factory.instances[0].closes == 0
        assert pipeline._audience_open is True
        await wait_until(lambda: len(finals(hub, "de")) > de_finals_before)
        assert hub._latest_segments.get("ar") is None
    finally:
        hub.unsubscribe(de_viewer)
        stop.set()
        await asyncio.wait_for(task, timeout=8.0)


async def test_arabic_disconnect_during_blocked_warm_cancels_the_warm() -> None:
    ar_mt = BlockingWarmMt()
    ar_factory = CountingArFactory(make=lambda: ar_mt)  # type: ignore[arg-type]

    pipeline, _ = make_pipeline(ar_factory=ar_factory)
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="ar", stop=stop))
    ar_viewer = hub.subscribe(ClientRole.VIEWER_AR)
    try:
        await wait_until(lambda: ar_mt.warm_started.is_set())
        hub.unsubscribe(ar_viewer)
        await wait_until(lambda: ar_mt.warm_cancelled.is_set(), timeout=5.0)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=6.0)


async def test_arabic_reconnect_is_fresh_and_does_not_churn_german_or_asr() -> None:
    pipeline, factory = make_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="ar", stop=stop))
    de_viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: bool(finals(hub, "de")))
        assert pipeline._mt_warmed is True

        ar_first = hub.subscribe(ClientRole.VIEWER_AR)
        await wait_until(lambda: bool(finals(hub, "ar")))
        first_ar_seq = finals(hub, "ar")[-1].seq

        hub.unsubscribe(ar_first)
        await wait_until(lambda: pipeline._ar.task is None)
        # The stale Arabic line was dropped, so a reconnect is not seeded with it.
        assert hub._latest_segments.get("ar") is None

        ar_second = hub.subscribe(ClientRole.VIEWER_AR)
        assert ar_second.mailbox._segment is None
        await wait_until(
            lambda: any(s.seq > first_ar_seq for s in finals(hub, "ar"))
        )
        # No recogniser restart, and German was not re-warmed.
        assert factory.instances[0].opens == 1
        assert len(factory.instances) == 1
        assert pipeline._mt_warmed is True
        hub.unsubscribe(ar_second)
    finally:
        hub.unsubscribe(de_viewer)
        stop.set()
        await asyncio.wait_for(task, timeout=8.0)


async def test_zero_overall_cancels_everything_and_clears_both_cached_lines() -> None:
    pipeline, factory = make_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="ar", stop=stop))
    de_viewer = hub.subscribe(ClientRole.VIEWER)
    ar_viewer = hub.subscribe(ClientRole.VIEWER_AR)
    try:
        await wait_until(lambda: bool(finals(hub, "de")) and bool(finals(hub, "ar")))
        assert hub._latest_segments.get("de") is not None
        assert hub._latest_segments.get("ar") is not None

        hub.unsubscribe(de_viewer)
        hub.unsubscribe(ar_viewer)
        await wait_until(lambda: factory.instances[0].closes >= 1)
        assert pipeline._audience_open is False
        assert hub._latest_segments == {}
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=6.0)


# --- ordering and revision guards are per language ----------------------------


def _seg(language: str, seq: int, rev: int, text: str, *, src: str | None = None) -> SubtitleSegment:
    if language == "ar":
        return SubtitleSegment(
            seq=seq, rev=rev, state="final", language="ar", de="", ar=text, src=src,
            timing=Timing(audio_ms=seq * 1000, emit_ms=seq * 1000 + 10),
        )
    return SubtitleSegment(
        seq=seq, rev=rev, state="final", language="de", de=text, ar=None, src=src,
        timing=Timing(audio_ms=seq * 1000, emit_ms=seq * 1000 + 10),
    )


async def test_role_filter_never_mixes_languages_or_leaks_the_source() -> None:
    hub = Hub()
    de_viewer = hub.subscribe(ClientRole.VIEWER)
    ar_viewer = hub.subscribe(ClientRole.VIEWER_AR)
    admin = hub.subscribe(ClientRole.ADMIN)

    hub.publish_segment(_seg("de", 0, 0, "Hallo", src="Hello there"))
    hub.publish_segment(_seg("ar", 0, 0, "مرحبا", src="Hello there"))

    de_messages = await de_viewer.mailbox.receive()
    ar_messages = await ar_viewer.mailbox.receive()
    admin_messages = await admin.mailbox.receive()

    de_segments = [m.data for m in de_messages if isinstance(m, SegmentMessage)]
    ar_segments = [m.data for m in ar_messages if isinstance(m, SegmentMessage)]
    admin_segments = [m.data for m in admin_messages if isinstance(m, SegmentMessage)]

    # The German role gets German only, with no Arabic, no source and no English words.
    assert [s.language for s in de_segments] == ["de"]
    assert de_segments[0].de == "Hallo"
    assert de_segments[0].ar is None
    assert de_segments[0].src is None
    assert "Hello" not in de_segments[0].de

    # The Arabic role gets Arabic only, with no German and no source.
    assert [s.language for s in ar_segments] == ["ar"]
    assert ar_segments[0].ar == "مرحبا"
    assert ar_segments[0].de == ""
    assert ar_segments[0].src is None

    # The operator sees both streams, each under its own language label, with source.
    assert sorted(s.language for s in admin_segments) == ["ar", "de"]
    assert all(s.src == "Hello there" for s in admin_segments)


def test_ordering_and_final_guards_are_per_language() -> None:
    hub = Hub()
    viewer = hub.subscribe(ClientRole.VIEWER)
    ar_viewer = hub.subscribe(ClientRole.VIEWER_AR)

    # A shared seq/rev pair in each language is accepted independently.
    hub.publish_segment(_seg("de", 7, 2, "de-final"))
    hub.publish_segment(_seg("ar", 7, 2, "ar-final"))
    # A stale German revision is refused without touching the Arabic guard.
    hub.publish_segment(_seg("de", 7, 1, "de-stale"))
    hub.publish_segment(_seg("ar", 8, 0, "ar-next"))

    assert viewer.mailbox._segment is not None
    assert viewer.mailbox._segment.de == "de-final"
    assert hub._latest_segments["de"].de == "de-final"
    assert hub._latest_segments["ar"].ar == "ar-next"
    # The Arabic viewer holds the Arabic line only, never the German one.
    assert ar_viewer.mailbox._segment is None


def test_encode_cache_does_not_collide_across_languages() -> None:
    hub = Hub()
    admin = hub.subscribe(ClientRole.ADMIN)
    hub.publish_segment(_seg("de", 1, 0, "Hallo", src="Hello"))
    hub.publish_segment(_seg("ar", 1, 0, "مرحبا", src="Hello"))

    de_admin = next(
        m for m in hub._subscribers[admin.id].mailbox._segments.values() if m is not None
    )
    # Same (seq, rev), different language: the encodings must differ and not collide.
    de_encoded = hub.encode(SegmentMessage(data=de_admin))
    ar_encoded = hub.encode(SegmentMessage(data=_seg("ar", 1, 0, "مرحبا", src="Hello")))
    assert de_encoded != ar_encoded
    assert "Hallo" in de_encoded
    assert "مرحبا" in ar_encoded


# --- hub language accounting --------------------------------------------------


def test_adding_arabic_does_not_advance_the_shared_occupancy() -> None:
    hub = Hub()
    de_viewer = hub.subscribe(ClientRole.VIEWER)
    shared = hub.demand_generation

    ar_viewer = hub.subscribe(ClientRole.VIEWER_AR)
    assert hub.demand_generation == shared          # recogniser not disturbed
    assert hub.viewer_count == 2
    assert hub.viewer_count_for("de") == 1
    assert hub.viewer_count_for("ar") == 1
    assert hub.viewers_by_language == {"de": 1, "ar": 1}

    hub.unsubscribe(ar_viewer)
    assert hub.demand_generation == shared
    assert hub.viewer_count_for("ar") == 0
    hub.unsubscribe(de_viewer)
    assert hub.demand_generation == shared + 1       # crossed back to none


def test_per_language_demand_generation_tracks_each_language() -> None:
    hub = Hub()
    assert hub.demand_generation_for("de") == 0
    assert hub.demand_generation_for("ar") == 0

    de = hub.subscribe(ClientRole.VIEWER)
    assert hub.demand_generation_for("de") == 1
    assert hub.demand_generation_for("ar") == 0

    ar = hub.subscribe(ClientRole.VIEWER_AR)
    assert hub.demand_generation_for("ar") == 1
    assert hub.demand_generation_for("de") == 1

    hub.unsubscribe(ar)
    assert hub.demand_generation_for("ar") == 2
    hub.unsubscribe(de)
    assert hub.demand_generation_for("de") == 2


def test_snapshot_and_forget_are_per_language() -> None:
    hub = Hub()
    hub.publish_segment(_seg("de", 1, 0, "Hallo"))
    hub.publish_segment(_seg("ar", 1, 0, "مرحبا"))

    hub.forget_segment_snapshot("ar")
    assert hub._latest_segments.get("ar") is None
    assert hub._latest_segments.get("de") is not None

    de_viewer = hub.subscribe(ClientRole.VIEWER)
    ar_viewer = hub.subscribe(ClientRole.VIEWER_AR)
    assert de_viewer.mailbox._segment is not None
    assert ar_viewer.mailbox._segment is None


# --- prompt locale ------------------------------------------------------------


def test_german_and_arabic_prompts_are_distinct_and_cacheable() -> None:
    from livesubs.mt.openai_compat import OpenAiCompatMt, build_system_prompt

    de = build_system_prompt()
    ar = build_system_prompt(target_language="ar")
    assert de != ar
    assert "German" in de and "Arabic" in ar

    de_adapter = OpenAiCompatMt(model="m", client=object(), target_language="de")
    ar_adapter = OpenAiCompatMt(model="m", client=object(), target_language="ar")
    assert de_adapter.prompt_fingerprint != ar_adapter.prompt_fingerprint
    # The frozen prefix does not change between calls.
    assert de_adapter.system_prompt == build_system_prompt()
    assert ar_adapter.system_prompt == build_system_prompt(target_language="ar")


# --- sidecar files ------------------------------------------------------------


def test_recorder_writes_an_arabic_sidecar_without_duplicating_english(tmp_path) -> None:
    recorder = SubtitleRecorder(
        directory=tmp_path,
        stem="demo",
        source_code="en",
        target_code="de",
        extra_target_codes=("ar",),
    )

    recorder(
        Committed(seq=0, start_ms=0, end_ms=1000, src="hello there", de="hallo", language="de")
    )
    recorder(
        Committed(
            seq=0,
            start_ms=0,
            end_ms=1000,
            src="hello there",
            de="",
            ar="مرحبا",
            language="ar",
        )
    )
    recorder.close()

    names = sorted(p.name for p in recorder.paths)
    assert "demo.ar.srt" in names
    assert "demo.de.srt" in names
    assert "demo.en.srt" in names
    assert "مرحبا" in (tmp_path / "demo.ar.srt").read_text(encoding="utf-8")
    # Exactly one English cue, written by the German stream.
    english = (tmp_path / "demo.en.srt").read_text(encoding="utf-8")
    assert english.count("hello there") == 1


# --- the server exposes and enforces the supported languages ------------------


class _IdlePublisher:
    """A publisher that declares Arabic support and then does nothing."""

    supported_languages = ("de", "ar")

    async def run(self, hub, *, session_id, stop) -> None:
        await stop.wait()


class _GermanOnlyPublisher:
    async def run(self, hub, *, session_id, stop) -> None:
        await stop.wait()


def test_session_api_reports_languages_and_viewers_by_language() -> None:
    settings = Settings(_env_file=None)
    with TestClient(create_app(settings, _IdlePublisher())) as client:
        payload = client.get("/api/session").json()
        assert payload["languages"] == ["de", "ar"]
        assert payload["viewers_by_language"] == {"de": 0, "ar": 0}


def test_arabic_role_is_accepted_when_the_publisher_supports_it() -> None:
    settings = Settings(_env_file=None)
    with (
        TestClient(create_app(settings, _IdlePublisher())) as client,
        client.websocket_connect("/ws/demo?role=viewer_ar") as ws,
    ):
        hello = ws.receive_json()
        assert hello["type"] == "hello"


def test_arabic_role_is_refused_before_accept_when_not_supported() -> None:
    settings = Settings(_env_file=None)
    with (
        TestClient(create_app(settings, _GermanOnlyPublisher())) as client,
        pytest.raises(WebSocketDisconnect) as error,
        client.websocket_connect("/ws/demo?role=viewer_ar") as ws,
    ):
        ws.receive_json()
    assert error.value.code == 4403


async def test_german_joining_an_arabic_only_room_starts_german_without_asr_restart() -> None:
    de_mt = CountingMt()
    pipeline, factory = make_pipeline(de_mt=de_mt)
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="ar", stop=stop))
    ar_viewer = hub.subscribe(ClientRole.VIEWER_AR)
    try:
        await wait_until(lambda: bool(finals(hub, "ar")))
        assert de_mt.warms == 0
        assert pipeline._mt_warmed is False

        de_viewer = hub.subscribe(ClientRole.VIEWER)
        await wait_until(lambda: de_mt.warms == 1)
        await wait_until(lambda: bool(finals(hub, "de")))
        # The recogniser was already open for Arabic and was not rebuilt.
        assert factory.instances[0].opens == 1
        assert len(factory.instances) == 1
        assert pipeline._mt_warmed is True
        hub.unsubscribe(de_viewer)
    finally:
        hub.unsubscribe(ar_viewer)
        stop.set()
        await asyncio.wait_for(task, timeout=8.0)


async def test_fast_german_reconnect_while_arabic_keeps_occupancy_restarts_german() -> None:
    """A German leave-and-return that never zeroes the shared occupancy must restart.

    Arabic viewers keep the recogniser open, so the global generation does not change;
    only the German generation advances. German must start again rather than freeze on
    a stale worker, and the recogniser must not be rebuilt.
    """

    pipeline, factory = make_pipeline()
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="ar", stop=stop))
    ar_viewer = hub.subscribe(ClientRole.VIEWER_AR)
    de_viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: bool(finals(hub, "de")))
        first_seq = finals(hub, "de")[-1].seq
        shared_before = hub.demand_generation
        de_generation_before = hub.demand_generation_for("de")
        opens_before = factory.instances[0].opens

        # Leave and rejoin synchronously, before the loop yields.
        hub.unsubscribe(de_viewer)
        de_viewer = hub.subscribe(ClientRole.VIEWER)
        assert hub.demand_generation == shared_before
        assert hub.demand_generation_for("de") == de_generation_before + 2

        await wait_until(
            lambda: any(s.seq > first_seq for s in finals(hub, "de")), timeout=6.0
        )
        assert factory.instances[0].opens == opens_before
        assert len(factory.instances) == 1
        assert pipeline._de.active is True
        assert pipeline._de.epoch == hub.demand_generation_for("de")
    finally:
        hub.unsubscribe(de_viewer)
        hub.unsubscribe(ar_viewer)
        stop.set()
        await asyncio.wait_for(task, timeout=8.0)


async def test_blocked_german_warm_is_cancelled_when_only_german_leaves() -> None:
    """German leaves during its warm while Arabic keeps the recogniser alive.

    The German warm must be cancelled promptly and not latch as warm, while Arabic
    subtitles keep flowing and the shared recogniser is not closed or restarted.
    """

    de_mt = BlockingWarmMt()
    pipeline, factory = make_pipeline(de_mt=de_mt)
    hub = RecordingHub()
    stop = asyncio.Event()

    task = asyncio.create_task(pipeline.run(hub, session_id="ar", stop=stop))
    ar_viewer = hub.subscribe(ClientRole.VIEWER_AR)
    de_viewer = hub.subscribe(ClientRole.VIEWER)
    try:
        await wait_until(lambda: de_mt.warm_started.is_set())
        await wait_until(lambda: bool(finals(hub, "ar")))
        opens_before = factory.instances[0].opens

        hub.unsubscribe(de_viewer)
        await wait_until(lambda: de_mt.warm_cancelled.is_set(), timeout=5.0)
        assert pipeline._mt_warmed is False
        assert pipeline._de.warmed is False
        assert pipeline._de.active is False
        # Arabic keeps running on the same recogniser.
        assert factory.instances[0].closes == 0
        assert factory.instances[0].opens == opens_before
        await wait_until(lambda: bool(finals(hub, "ar")))
    finally:
        hub.unsubscribe(ar_viewer)
        stop.set()
        await asyncio.wait_for(task, timeout=8.0)


class _RaisingEventsAsr:
    """An ASR whose event stream fails immediately, to test teardown noise."""

    def events(self):
        async def stream():
            raise RuntimeError("socket closed")
            yield  # pragma: no cover - makes this an async generator

        return stream()


async def test_collector_does_not_report_a_failure_during_teardown() -> None:
    from livesubs.events import ErrorMessage

    pipeline, _ = make_pipeline()
    hub = RecordingHub()
    pipeline._hub = hub

    # Deliberate close: the socket error is expected teardown noise.
    pipeline._closing = True
    pipeline._audience_open = True
    await pipeline._collect_events(_RaisingEventsAsr())
    assert pipeline._provider_failed is False
    assert not [m for m in hub.messages if isinstance(m, ErrorMessage)]

    # Audience already gone: also expected, and must not resurrect a provider.
    pipeline._closing = False
    pipeline._audience_open = False
    await pipeline._collect_events(_RaisingEventsAsr())
    assert pipeline._provider_failed is False
    assert not [m for m in hub.messages if isinstance(m, ErrorMessage)]


async def test_collector_still_reports_a_real_failure_while_live() -> None:
    from livesubs.events import ErrorMessage

    pipeline, _ = make_pipeline()
    hub = RecordingHub()
    pipeline._hub = hub
    pipeline._closing = False
    pipeline._audience_open = True

    await pipeline._collect_events(_RaisingEventsAsr())

    assert pipeline._provider_failed is True
    assert [m for m in hub.messages if isinstance(m, ErrorMessage)]
