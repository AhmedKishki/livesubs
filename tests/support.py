"""Fakes and helpers shared by the pipeline tests.

Only credential-free fakes. No test opens a provider or touches a network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager

from livesubs.asr.stub import StubAsr
from livesubs.audio.synthetic import SpeechLikeSource
from livesubs.events import (
    ErrorMessage,
    SegmentMessage,
    StatusMessage,
    SubtitleSegment,
    WireMessage,
)
from livesubs.hub import Hub
from livesubs.mt.base import MtChunk, MtContext, MtProvider
from livesubs.mt.passthrough import PassthroughMt
from livesubs.pipeline import LivePipeline, PipelineConfig

PHRASES = (
    "alpha beta gamma delta epsilon",
    "zeta eta theta iota kappa lambda",
)


class RecordingHub(Hub):
    """A hub that keeps every published message.

    A mailbox deliberately does not: it is latest-wins per channel, which is the right
    design for delivery and the wrong place to observe a sequence from. Recording at the hub
    also means the tests exercise the real ``Hub.publish``, its encoding cache and its
    ordering guard.
    """

    def __init__(self, targets: tuple[str, ...] = ()) -> None:
        super().__init__(targets)
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

    @property
    def status_events(self) -> list[tuple[str, str | None, str | None]]:
        """``(status, detail, language)`` in the order published."""

        return [
            (m.data.status.value, m.data.detail, m.data.language)
            for m in self.messages
            if isinstance(m, StatusMessage)
        ]

    @property
    def errors(self) -> list[ErrorMessage]:
        return [m for m in self.messages if isinstance(m, ErrorMessage)]


def finals(hub: RecordingHub, language: str | None = None) -> list[SubtitleSegment]:
    return [
        s
        for s in hub.segments
        if s.state == "final" and (language is None or s.language == language)
    ]


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not met before the timeout")
        await asyncio.sleep(0.005)


# --- recognisers ---------------------------------------------------------------------


class LifecycleAsr:
    """A StubAsr wrapper that counts every lifecycle transition."""

    def __init__(self) -> None:
        self.inner = StubAsr()
        self.opens = 0
        self.closes = 0
        self.pushes = 0
        self.seeked_to: int | None = None

    @property
    def opened(self) -> bool:
        return self.opens > 0

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


# --- translators ----------------------------------------------------------------------


class CountingMt(PassthroughMt):
    """Counts warms and translate calls."""

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


class RecordingMt(PassthroughMt):
    """A translator that remembers what it was asked and with what context."""

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
        yield MtChunk(text_delta="", done=True)  # pragma: no cover - unreachable


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
        self._warmed = True  # pragma: no cover - unreachable


class Factory:
    """Builds translators lazily and records every construction."""

    def __init__(self, make: Callable[[], MtProvider] = CountingMt) -> None:
        self._make = make
        self.instances: list[MtProvider] = []

    def __call__(self) -> MtProvider:
        instance = self._make()
        self.instances.append(instance)
        return instance


# --- pipelines -----------------------------------------------------------------------


def make_pipeline(
    *,
    duration_ms: int = 8000,
    mt: MtProvider | None = None,
    realtime: bool = False,
    translators: Mapping[str, MtProvider | Callable[[], MtProvider]] | None = None,
    source_language: str = "en",
    on_segment=None,
) -> LivePipeline:
    """An ungated pipeline over synthetic speech.

    With no ``translators`` it has one target, German, translated by ``mt``.
    """

    return LivePipeline(
        source=SpeechLikeSource(
            realtime=realtime,
            duration_ms=None if realtime else duration_ms,
        ),
        asr=StubAsr(phrases=PHRASES),
        translators=translators
        if translators is not None
        else {"de": mt if mt is not None else PassthroughMt()},
        source_language=source_language,
        config=PipelineConfig(telemetry_interval_ms=1000),
        on_segment=on_segment,
    )


def make_gated(
    *,
    translators: Mapping[str, MtProvider | Callable[[], MtProvider]],
    source_language: str = "en",
    realtime: bool = True,
    asr_factory: AsrFactory | None = None,
    source=None,
    config: PipelineConfig | None = None,
) -> tuple[LivePipeline, AsrFactory]:
    """A pipeline that follows its audience, with a recogniser factory to inspect."""

    factory = asr_factory if asr_factory is not None else AsrFactory()
    first = factory()
    pipeline = LivePipeline(
        source=source if source is not None else SpeechLikeSource(realtime=realtime),
        asr=first,
        asr_factory=factory,
        translators=translators,
        source_language=source_language,
        config=config or PipelineConfig(telemetry_interval_ms=1000),
        require_audience=True,
    )
    return pipeline, factory


async def run_to_completion(
    pipeline: LivePipeline,
    hub: RecordingHub | None = None,
) -> RecordingHub:
    """Drive a pipeline to the end of its audio and return what it published."""

    hub = hub if hub is not None else RecordingHub()
    await pipeline.run(hub, session_id="test", stop=asyncio.Event())
    return hub


@asynccontextmanager
async def running(pipeline: LivePipeline, hub: Hub, *, timeout: float = 6.0):
    """Run a pipeline for the duration of a block, and stop it, however the block ends."""

    stop = asyncio.Event()
    task = asyncio.create_task(pipeline.run(hub, session_id="t", stop=stop))
    try:
        yield stop
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=timeout)


#: The two modes the pipeline serves, as ``(speaker, targets)``. Behaviour that does not
#: depend on the language is tested under both, so nothing is true only for German.
MODES = [("en", ("de", "ar")), ("de", ("en", "ar")), ("en+de", ("en", "de"))]
MODE_IDS = ["english-speaker", "german-speaker", "conversation"]
