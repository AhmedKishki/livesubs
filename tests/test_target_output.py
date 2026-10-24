"""Ordered source output without paid providers."""

import asyncio
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest

from livesubs.hub import Presence
from livesubs.mt.base import MtChunk
from livesubs.pipeline.health import Health
from livesubs.pipeline.records import PipelineConfig, SourceSegment
from livesubs.pipeline.targets import TargetWorker, WorkerEnv


def worker(language="en", factory=None):
    hub = Mock()
    sink = Mock()
    health = Health(lambda *args: None)
    config = PipelineConfig(mt_retries=0, mt_failures_before_notice=1)
    target = TargetWorker(language, factory=factory or Mock(), source_language="en+de")
    target.bind(WorkerEnv(hub, config, health, lambda: 2000, sink, False), None)
    return target, hub, sink, health


def test_source_contract_is_frozen():
    item = SourceSegment(1, "Hello", 0, 100)
    assert item.language == "unknown"
    with pytest.raises(FrozenInstanceError):
        item.language = "en"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "de"])
async def test_transcription_never_builds_or_warms_mt(language):
    factory = Mock(side_effect=AssertionError("must not build"))
    target, hub, sink, _ = worker(language, factory)
    await target.start(None)
    target.offer(SourceSegment(1, "Words", 10, 100, language))
    await target.drain(1)
    factory.assert_not_called()
    segment = hub.publish_segment.call_args.args[0]
    assert (segment.seq, segment.rev, segment.state.value, segment.text) == (1, 0, "final", "Words")
    assert hub.publish_segment.call_count == 1
    assert target.recent_src == target.recent_target == ["Words"]
    assert sink.call_args.args[0].start_ms == 10
    assert target.stats.completed == 0


class Translator:
    def __init__(self):
        self.calls = []
        self.warms = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def warm(self):
        self.warms += 1

    async def translate(self, src, ctx):
        self.calls.append((src, ctx))
        self.entered.set()
        await self.release.wait()
        yield MtChunk("Rendered", done=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("source,target_language", [("mixed", "en"), ("unknown", "de"), ("en", "ar"), ("ar", "ar"), ("de", "en")])
async def test_conservative_routes_use_mt(source, target_language):
    mt = Translator()
    mt.release.set()
    target, _, sink, _ = worker(target_language, lambda: mt)
    await target.start(None)
    assert mt.warms == 0
    target.offer(SourceSegment(1, "Input", 0, 100, source))
    await target.drain(1)
    assert mt.warms == 1
    assert len(mt.calls) == 1
    assert sink.call_args.args[0].text == "Rendered"


@pytest.mark.asyncio
async def test_transcription_waits_for_translation_and_supplies_paired_context():
    mt = Translator()
    target, hub, _, _ = worker(factory=lambda: mt)
    await target.start(None)
    target.offer(SourceSegment(1, "German", 0, 100, "de"))
    await asyncio.wait_for(mt.entered.wait(), 1)
    target.offer(SourceSegment(2, "English", 100, 200, "en"))
    assert not hub.publish_segment.called
    mt.release.set()
    await target.drain(1)
    assert [c.args[0].seq for c in hub.publish_segment.call_args_list] == [1, 1, 2]
    assert target.recent_src == ["German", "English"]
    assert target.recent_target == ["Rendered", "English"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["build", "warm", "timeout"])
async def test_lazy_failures_leave_worker_alive_and_transcript_does_not_clear_outage(failure):
    mt = Translator()

    async def bad_warm():
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("warm failed")

    mt.warm = bad_warm

    def factory():
        if failure == "build":
            raise RuntimeError("build failed")
        return mt

    target, hub, _, health = worker(factory=factory)
    target._env.config.mt_deadline_s = 0.01
    await target.start(None)
    target.offer(SourceSegment(1, "Unknown", 0, 100))
    target.offer(SourceSegment(2, "English", 100, 200, "en"))
    await target.drain(1)
    assert health.is_open("mt", scope="en")
    assert target.failures == 1
    assert target.stats.failed == 1
    assert target.stats.completed == 0
    assert hub.publish_segment.call_args.args[0].text == "English"
    assert target.recent_src == ["English"]


@pytest.mark.asyncio
async def test_transcript_context_is_bounded_and_reaches_next_mt_request():
    mt = Translator()
    mt.release.set()
    target, _, _, _ = worker(factory=lambda: mt)
    target._env.config.segmenter.context_segments = 1
    await target.start(None)
    target.offer(SourceSegment(1, "Old", 0, 100, "en"))
    target.offer(SourceSegment(2, "Recent", 100, 200, "en"))
    target.offer(SourceSegment(3, "Neu", 200, 300, "de"))
    await target.drain(1)
    context = mt.calls[0][1]
    assert context.source_context == context.target_context == ["Recent"]
    assert target.recent_src == ["Neu"]
    assert target.recent_target == ["Rendered"]


@pytest.mark.asyncio
async def test_stopping_cancels_lazy_warm_and_discards_queued_transcript():
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    mt = Translator()

    async def warm():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    mt.warm = warm
    target, hub, sink, _ = worker(factory=lambda: mt)
    await target.start(None)
    target.offer(SourceSegment(1, "Neu", 0, 100, "de"))
    target.offer(SourceSegment(2, "English", 100, 200, "en"))
    await asyncio.wait_for(entered.wait(), 1)
    await target.stop()
    assert cancelled.is_set()
    assert not target.queue
    hub.publish_segment.assert_not_called()
    sink.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect", [False, True], ids=["last-reader-left", "generation-changed"])
async def test_failed_first_warm_does_not_retry_after_demand_invalidation(reconnect):
    presence = Presence()
    presence.update(1)
    mt = Translator()

    async def invalidating_warm():
        mt.warms += 1
        presence.update(0)
        if reconnect:
            presence.update(1)
        raise RuntimeError("first warm failed after audience changed")

    mt.warm = invalidating_warm
    factory = Mock(return_value=mt)
    target, hub, sink, _ = worker(factory=factory)
    target._env.gated = True
    target._env.config.mt_retries = 1
    target.bind(target._env, presence)
    await target.start(presence.generation)
    target.offer(SourceSegment(1, "Unknown", 0, 100))
    await target.drain(1)

    assert mt.warms == 1
    factory.assert_called_once_with()
    assert mt.calls == []
    assert not target.warmed
    assert target.stats.failed == target.stats.completed == 0
    hub.publish_segment.assert_not_called()
    sink.assert_not_called()
