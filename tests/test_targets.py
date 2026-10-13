"""One shared recogniser feeds independent, ordered translation streams, one per language.

The requirement these pin: a language nobody asks for is never built, warmed or paid for;
a viewer of one language joining a room that already has another must not restart the
recogniser or interrupt the other; and the last viewer of a language leaving cancels only
that language's work. They hold for either speaker, so they run under both modes, and
nothing in the pipeline knows how many languages there are, which the last tests show.

Only credential-free fakes are used. No test opens a provider or touches a network.
"""

from __future__ import annotations

import asyncio

import pytest
from support import (
    MODE_IDS,
    MODES,
    BlockingMt,
    BlockingWarmMt,
    CountingMt,
    Factory,
    RecordingHub,
    finals,
    make_gated,
    running,
    wait_until,
)

from livesubs.hub import ClientRole
from livesubs.mt.passthrough import PassthroughMt


@pytest.fixture(params=MODES, ids=MODE_IDS)
def mode(request):
    return request.param


def viewer(language: str) -> ClientRole:
    return ClientRole.viewer(language)


def arrives_to_nothing(hub: RecordingHub, language: str) -> bool:
    """Whether a viewer arriving now would be seeded with nothing from a past audience."""

    probe = hub.subscribe(viewer(language))
    try:
        return not probe.mailbox.has_pending
    finally:
        hub.unsubscribe(probe)


def pair(mode):
    speaker, (first, other) = mode
    return speaker, first, other


def room(mode, *, first_mt=None, other: Factory | None = None):
    """A gated pipeline for ``mode``, its recogniser factory, and the other language's factory."""

    speaker, first, second = pair(mode)
    first_mt = first_mt if first_mt is not None else CountingMt()
    other = other if other is not None else Factory()
    pipeline, asr = make_gated(
        translators={first: first_mt, second: other}, source_language=speaker
    )
    return pipeline, asr, first_mt, other, RecordingHub((first, second))


# --- a language is off until somebody reads it ---------------------------------------


async def test_a_language_nobody_reads_is_never_built_or_warmed(mode) -> None:
    _, _, second = pair(mode)
    pipeline, asr, first_mt, second_factory, hub = room(mode)

    async with running(pipeline, hub):
        await asyncio.sleep(0.2)
        assert second_factory.instances == []          # never constructed
        assert pipeline._workers[second].warmed is False
        assert asr.instances[0].opens == 0
        assert first_mt.warms == 0


async def test_a_viewer_of_the_second_language_alone_activates_that_language_only(mode) -> None:
    _, first, second = pair(mode)
    pipeline, asr, first_mt, second_factory, hub = room(mode)

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer(second))
        await wait_until(lambda: asr.instances[0].opens == 1)
        await wait_until(lambda: bool(finals(hub, second)))
        assert first_mt.warms == 0
        assert first_mt.requests == 0
        assert not finals(hub, first)
        assert len(second_factory.instances) == 1
        hub.unsubscribe(reader)


# --- one language joins and leaves without disturbing the other ----------------------


async def test_a_second_language_joining_does_not_restart_the_recogniser(mode) -> None:
    _, first, second = pair(mode)
    pipeline, asr, _, _, hub = room(mode)

    async with running(pipeline, hub):
        a = hub.subscribe(viewer(first))
        await wait_until(lambda: bool(finals(hub, first)))
        assert asr.instances[0].opens == 1

        b = hub.subscribe(viewer(second))
        await wait_until(lambda: bool(finals(hub, second)))
        # The shared recogniser was not rebuilt for the new language.
        assert asr.instances[0].opens == 1
        assert len(asr.instances) == 1
        hub.unsubscribe(b)
        hub.unsubscribe(a)


async def test_a_slow_language_does_not_stall_the_other(mode) -> None:
    _, first, second = pair(mode)
    stuck = BlockingMt()
    pipeline, _, _, _, hub = room(mode, other=Factory(lambda: stuck))

    async with running(pipeline, hub, timeout=8.0):
        a = hub.subscribe(viewer(first))
        b = hub.subscribe(viewer(second))
        await wait_until(lambda: stuck.started.is_set())
        # The first language keeps finishing while the second is stuck in one request.
        await wait_until(lambda: len(finals(hub, first)) >= 2)
        assert not finals(hub, second)
        hub.unsubscribe(b)
        hub.unsubscribe(a)


async def test_the_last_viewer_of_a_language_cancels_only_that_languages_work(mode) -> None:
    _, first, second = pair(mode)
    stuck = BlockingMt()
    pipeline, asr, _, _, hub = room(mode, other=Factory(lambda: stuck))

    async with running(pipeline, hub, timeout=8.0):
        a = hub.subscribe(viewer(first))
        b = hub.subscribe(viewer(second))
        await wait_until(lambda: bool(finals(hub, first)))
        before = len(finals(hub, first))
        await wait_until(lambda: stuck.started.is_set())

        hub.unsubscribe(b)
        await wait_until(lambda: stuck.cancelled.is_set(), timeout=5.0)
        # The recogniser and the first language survive, and the first keeps flowing.
        assert asr.instances[0].closes == 0
        assert pipeline._asr.is_open
        await wait_until(lambda: len(finals(hub, first)) > before)
        # Only the second language's cached line is dropped.
        assert arrives_to_nothing(hub, second)
        hub.unsubscribe(a)


async def test_leaving_during_a_blocked_warm_cancels_the_warm(mode) -> None:
    _, _, second = pair(mode)
    warming = BlockingWarmMt()
    pipeline, _, _, _, hub = room(mode, other=Factory(lambda: warming))

    async with running(pipeline, hub):
        b = hub.subscribe(viewer(second))
        await wait_until(lambda: warming.warm_started.is_set())
        hub.unsubscribe(b)
        await wait_until(lambda: warming.warm_cancelled.is_set(), timeout=5.0)
        assert pipeline._workers[second].active is False


async def test_a_languages_reconnect_is_fresh_and_does_not_churn_the_rest(mode) -> None:
    _, first, second = pair(mode)
    pipeline, asr, first_mt, _, hub = room(mode)

    async with running(pipeline, hub, timeout=8.0):
        a = hub.subscribe(viewer(first))
        await wait_until(lambda: bool(finals(hub, first)))
        assert first_mt.warms == 1

        b = hub.subscribe(viewer(second))
        await wait_until(lambda: bool(finals(hub, second)))
        last_seq = finals(hub, second)[-1].seq

        hub.unsubscribe(b)
        await wait_until(lambda: not pipeline._workers[second].active)
        # The stale line was dropped, so a reconnect is not seeded with it.
        again = hub.subscribe(viewer(second))
        assert not again.mailbox.has_pending
        await wait_until(lambda: any(s.seq > last_seq for s in finals(hub, second)))
        # No recogniser restart, and the first language was not warmed again.
        assert asr.instances[0].opens == 1
        assert len(asr.instances) == 1
        assert first_mt.warms == 1
        hub.unsubscribe(again)
        hub.unsubscribe(a)


async def test_with_everyone_gone_everything_is_cancelled_and_every_line_forgotten(mode) -> None:
    _, first, second = pair(mode)
    pipeline, asr, _, _, hub = room(mode)

    async with running(pipeline, hub):
        a = hub.subscribe(viewer(first))
        b = hub.subscribe(viewer(second))
        await wait_until(lambda: bool(finals(hub, first)) and bool(finals(hub, second)))

        hub.unsubscribe(a)
        hub.unsubscribe(b)
        await wait_until(lambda: asr.instances[0].closes >= 1)
        assert not pipeline._asr.is_open
        await wait_until(
            lambda: not pipeline._workers[first].active and not pipeline._workers[second].active
        )
        assert arrives_to_nothing(hub, first)
        assert arrives_to_nothing(hub, second)


# --- a language joining a room that already has the recogniser --------------------------


async def test_the_first_language_joining_a_room_that_reads_the_second_starts_without_a_restart(
    mode,
) -> None:
    _, first, second = pair(mode)
    pipeline, asr, first_mt, _, hub = room(mode)

    async with running(pipeline, hub, timeout=8.0):
        b = hub.subscribe(viewer(second))
        await wait_until(lambda: bool(finals(hub, second)))
        assert first_mt.warms == 0

        a = hub.subscribe(viewer(first))
        await wait_until(lambda: first_mt.warms == 1)
        await wait_until(lambda: bool(finals(hub, first)))
        # The recogniser was already open for the other language and was not rebuilt.
        assert asr.instances[0].opens == 1
        assert len(asr.instances) == 1
        hub.unsubscribe(a)
        hub.unsubscribe(b)


async def test_a_fast_leave_and_return_that_keeps_the_room_occupied_restarts_that_language(
    mode,
) -> None:
    """A leave-and-return that never zeroes the shared audience must still restart.

    The other language's viewers keep the recogniser open, so the shared generation does
    not change; only this language's does. It must start again rather than freeze on a
    stale worker, and the recogniser must not be rebuilt.
    """

    _, first, second = pair(mode)
    pipeline, asr, _, _, hub = room(mode)

    async with running(pipeline, hub, timeout=8.0):
        b = hub.subscribe(viewer(second))
        a = hub.subscribe(viewer(first))
        await wait_until(lambda: bool(finals(hub, first)))
        first_seq = finals(hub, first)[-1].seq
        shared_before = hub.audience().generation
        generation_before = hub.audience(first).generation

        # Leave and rejoin synchronously, before the loop yields.
        hub.unsubscribe(a)
        a = hub.subscribe(viewer(first))
        assert hub.audience().generation == shared_before
        assert hub.audience(first).generation == generation_before + 2

        await wait_until(lambda: any(s.seq > first_seq for s in finals(hub, first)), timeout=6.0)
        assert asr.instances[0].opens == 1
        assert len(asr.instances) == 1
        worker = pipeline._workers[first]
        assert worker.active and worker.epoch == hub.audience(first).generation
        hub.unsubscribe(a)
        hub.unsubscribe(b)


async def test_leaving_during_a_blocked_warm_keeps_the_other_language_running(mode) -> None:
    """One language leaves during its warm while the other keeps the recogniser alive."""

    _, first, second = pair(mode)
    warming = BlockingWarmMt()
    pipeline, asr, _, _, hub = room(mode, first_mt=warming)

    async with running(pipeline, hub, timeout=8.0):
        b = hub.subscribe(viewer(second))
        a = hub.subscribe(viewer(first))
        await wait_until(lambda: warming.warm_started.is_set())
        await wait_until(lambda: bool(finals(hub, second)))

        hub.unsubscribe(a)
        await wait_until(lambda: warming.warm_cancelled.is_set(), timeout=5.0)
        worker = pipeline._workers[first]
        assert worker.warmed is False and worker.active is False
        # The other language keeps running on the same recogniser.
        assert asr.instances[0].closes == 0
        seen = len(finals(hub, second))
        await wait_until(lambda: len(finals(hub, second)) > seen)
        hub.unsubscribe(b)


# --- each language is ordered on its own ---------------------------------------------------


async def test_each_language_has_its_own_revision_timeline(mode) -> None:
    _, first, second = pair(mode)
    pipeline, _, _, _, hub = room(mode)

    async with running(pipeline, hub, timeout=8.0):
        a = hub.subscribe(viewer(first))
        b = hub.subscribe(viewer(second))
        await wait_until(lambda: bool(finals(hub, first)) and bool(finals(hub, second)))
        hub.unsubscribe(a)
        hub.unsubscribe(b)

    for language in (first, second):
        mine = [s for s in hub.segments if s.language == language]
        keys = [(s.seq, s.rev) for s in mine]
        assert keys == sorted(keys)
        for seq in {s.seq for s in mine}:
            revs = [s.rev for s in mine if s.seq == seq]
            assert revs[0] == 0 and revs == sorted(revs)


async def test_both_languages_are_translated_from_the_same_source_text(mode) -> None:
    _, first, second = pair(mode)
    pipeline, _, _, _, hub = room(mode, other=Factory(PassthroughMt))

    async with running(pipeline, hub, timeout=8.0):
        a = hub.subscribe(viewer(first))
        b = hub.subscribe(viewer(second))
        await wait_until(lambda: bool(finals(hub, first)) and bool(finals(hub, second)))
        hub.unsubscribe(a)
        hub.unsubscribe(b)

    shared = {s.seq for s in finals(hub, first)} & {s.seq for s in finals(hub, second)}
    assert shared
    for seq in shared:
        sources = {s.src for s in finals(hub) if s.seq == seq}
        assert len(sources) == 1, f"seq {seq} was translated from two different texts"


# --- nothing is special about two, or about these languages --------------------------------


async def test_a_pipeline_serves_any_number_of_languages() -> None:
    languages = ("de", "ar", "fr")
    pipeline, asr = make_gated(translators={code: Factory(PassthroughMt) for code in languages})
    hub = RecordingHub(languages)

    async with running(pipeline, hub, timeout=8.0):
        readers = [hub.subscribe(viewer(code)) for code in languages]
        await wait_until(lambda: all(finals(hub, code) for code in languages))
        assert asr.instances[0].opens == 1
        for reader in readers:
            hub.unsubscribe(reader)

    assert pipeline.supported_languages == languages


async def test_a_language_the_pipeline_does_not_produce_creates_no_work() -> None:
    pipeline, _ = make_gated(translators={"de": Factory(PassthroughMt)})
    hub = RecordingHub(("de",))

    async with running(pipeline, hub):
        # A viewer of a language that has no translator: the shared audience exists, so the
        # recogniser opens, but no segment is ever produced for it.
        reader = hub.subscribe(viewer("ar"))
        await asyncio.sleep(0.5)
        assert not finals(hub, "ar")
        hub.unsubscribe(reader)
