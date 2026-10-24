"""Recognition intent is independent of subtitle capability and provider work."""
import asyncio

import pytest
from support import CountingMt, LifecycleAsr, RecordingHub, make_gated, running, wait_until

from livesubs.asr.base import AsrEvent
from livesubs.hub import ClientRole
from livesubs.pipeline import LivePipeline
from livesubs.pipeline.records import SourceSegment


def pipeline(**kwargs):
    return LivePipeline(source=object(), asr=object(), translators={"en": object()}, **kwargs)


def test_idle_intent_is_validated_revisioned_and_does_not_activate():
    live = pipeline(speaker="auto", source_language="en+de")
    assert live.speaker_snapshot() == {
        "requested": "auto", "active": None, "state": "paused", "revision": 0, "error": None
    }
    assert live.set_speaker("de")
    assert not live.set_speaker("de")
    assert live.speaker_snapshot()["revision"] == 1
    assert live.speaker_snapshot()["active"] is None
    with pytest.raises(ValueError):
        live.set_speaker("ar")
    assert live.speaker_snapshot()["requested"] == "de"


async def test_live_change_replaces_stream_without_replacing_workers_or_capture():
    live, factory = make_gated(translators={"de": CountingMt()})
    hub = RecordingHub(("de",))
    async with running(live, hub):
        reader = hub.subscribe(ClientRole.viewer("de"))
        await wait_until(lambda: live.speaker_snapshot()["state"] == "active")
        workers = dict(live._workers)
        clock = live._audio_ms
        live.set_speaker("de")
        losses = live.stats.recogniser_losses
        live._recogniser_ended(True)
        assert live.stats.recogniser_losses == losses
        await wait_until(lambda: live.speaker_snapshot()["active"] == "de")
        assert len(factory.instances) == 2
        assert factory.instances[0].closes == 1
        assert live._workers == workers
        assert live._audio_ms >= clock
        hub.unsubscribe(reader)


async def test_idle_updates_do_not_open_and_latest_setting_opens_on_demand():
    live, factory = make_gated(translators={"de": CountingMt()})
    hub = RecordingHub(("de",))
    async with running(live, hub):
        live.set_speaker("auto")
        live.set_speaker("de")
        assert factory.instances[0].opens == 0
        reader = hub.subscribe(ClientRole.viewer("de"))
        await wait_until(lambda: live.speaker_snapshot()["active"] == "de")
        assert live.speaker_snapshot()["revision"] == 2
        hub.unsubscribe(reader)


async def test_flush_keeps_old_pin_once_while_capture_and_latest_intent_advance():
    entered = asyncio.Event()
    release = asyncio.Event()

    class Retiring(LifecycleAsr):
        async def close(self):
            entered.set()
            await release.wait()
            await super().close()

    class Factory:
        def __init__(self):
            self.instances = []

        def __call__(self):
            item = Retiring() if not self.instances else LifecycleAsr()
            self.instances.append(item)
            return item

    factory = Factory()
    live, _ = make_gated(translators={"de": CountingMt()}, asr_factory=factory)
    sources = []
    live.on_source = sources.append
    hub = RecordingHub(("de",))
    async with running(live, hub):
        reader = hub.subscribe(ClientRole.viewer("de"))
        await wait_until(lambda: live.speaker_snapshot()["active"] == "en")
        live._assembler.feed(AsrEvent(kind="partial", text="pending", start_ms=0, end_ms=20))
        live.set_speaker("de")
        await entered.wait()
        clock = live._audio_ms
        await wait_until(lambda: live._audio_ms >= clock + 60)
        live.set_speaker("auto")
        release.set()
        await wait_until(lambda: live.speaker_snapshot()["active"] == "auto")
        assert [(item.seq, item.text, item.language) for item in sources] == [(0, "pending", "en")]
        assert len(factory.instances) == 2
        assert live._seq == 1
        hub.unsubscribe(reader)


def test_source_sink_runs_before_worker_offer_and_its_failure_is_isolated(monkeypatch):
    live = pipeline(speaker="auto")
    seen = []

    def sink(item):
        seen.append(("source", item))
        raise RuntimeError("broken recorder")

    live.on_source = sink
    worker = live._workers["en"]
    monkeypatch.setattr(worker, "offer", lambda item: seen.append(("worker", item)))
    item = SourceSegment(7, "text", 10, 20, "mixed")
    live._dispatch(item)
    assert seen == [("source", item), ("worker", item)]
    assert live.speaker_snapshot()["detected"] == {"seq": 7, "language": "mixed"}


async def test_superseded_open_is_cancelled_and_immutable_latest_config_activates():
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    configs = []

    class SlowFirst(LifecycleAsr):
        async def open(self, config):
            configs.append(config)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    class Next(LifecycleAsr):
        async def open(self, config):
            configs.append(config)
            await super().open(config)

    class Factory:
        def __init__(self):
            self.instances = []

        def __call__(self):
            item = SlowFirst() if not self.instances else Next()
            self.instances.append(item)
            return item

    factory = Factory()
    live, _ = make_gated(translators={"de": CountingMt()}, asr_factory=factory)
    hub = RecordingHub(("de",))
    async with running(live, hub):
        reader = hub.subscribe(ClientRole.viewer("de"))
        await entered.wait()
        live.set_speaker("auto")
        live.set_speaker("de")
        assert live.speaker_snapshot()["active"] is None
        await wait_until(lambda: live.speaker_snapshot()["active"] == "de")
        assert cancelled.is_set()
        assert [config.language for config in configs] == ["en", "de"]
        assert configs[0] is not configs[1]
        assert live.config.asr.language == "en"
        assert live.stats.recogniser_losses == 0
        hub.unsubscribe(reader)


async def test_failed_requested_setting_stays_failed_until_new_intent(monkeypatch):
    from livesubs.pipeline import lifecycle

    monkeypatch.setattr(lifecycle, "_BACKOFF_INITIAL_S", 0.001)
    monkeypatch.setattr(lifecycle, "_BACKOFF_MAX_S", 0.001)
    configs = []

    class RefusesEnglish(LifecycleAsr):
        async def open(self, config):
            configs.append(config.language)
            if config.language == "en":
                raise RuntimeError("provider input must remain private")
            await super().open(config)

    live, _ = make_gated(translators={"de": CountingMt()}, asr_factory=RefusesEnglish)
    hub = RecordingHub(("de",))
    async with running(live, hub):
        reader = hub.subscribe(ClientRole.viewer("de"))
        await wait_until(lambda: live.speaker_snapshot()["state"] == "failed")
        snapshot = live.speaker_snapshot()
        assert snapshot["active"] is None
        assert snapshot["requested"] == "en"
        assert snapshot["error"] == "recognition could not be started"
        assert set(configs) == {"en"}
        live.set_speaker("de")
        await wait_until(lambda: live.speaker_snapshot()["active"] == "de")
        assert live.speaker_snapshot()["error"] is None
        assert configs[-1] == "de"
        hub.unsubscribe(reader)


@pytest.mark.parametrize("transition", ["speaker", "disconnect"])
async def test_invalidated_resistant_open_is_eventually_closed_without_harming_latest(
    monkeypatch, transition
):
    from livesubs.pipeline import asr_session

    original_cancel = asr_session.cancel_bounded

    async def quick_cancel(task):
        await original_cancel(task, timeout=0.001)

    monkeypatch.setattr(asr_session, "cancel_bounded", quick_cancel)
    entered = asyncio.Event()
    release = asyncio.Event()

    class Resistant(LifecycleAsr):
        live = False

        async def open(self, cfg):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            self.live = True

        async def close(self):
            self.live = False
            await super().close()

    instances = []

    def factory():
        item = Resistant() if not instances else LifecycleAsr()
        instances.append(item)
        return item

    live, _ = make_gated(translators={"de": CountingMt()}, asr_factory=factory)
    hub = RecordingHub(("de",))
    async with running(live, hub):
        reader = hub.subscribe(ClientRole.viewer("de"))
        await entered.wait()
        if transition == "speaker":
            live.set_speaker("de")
            await wait_until(lambda: live.speaker_snapshot()["active"] == "de")
        else:
            hub.unsubscribe(reader)
            await wait_until(lambda: instances[0].closes == 1)
        clock = live._audio_ms
        await wait_until(lambda: live._audio_ms >= clock + 40)
        release.set()
        await wait_until(lambda: instances[0].closes == 2)
        assert not instances[0].live
        if transition == "speaker":
            assert len(instances) == 2 and instances[1].closes == 0
            assert live.speaker_snapshot()["active"] == "de"
            hub.unsubscribe(reader)
        else:
            assert len(instances) == 1 and not live._asr.is_open
