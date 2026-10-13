"""The recogniser's open, close and event inbox, against fakes that can misbehave."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from support import wait_until

from livesubs.asr.base import AsrConfig, AsrEvent
from livesubs.pipeline.asr_session import AsrSession
from livesubs.pipeline.asr_session import _close_bounded as close_bounded

CONFIG = AsrConfig(language="en")


def event(text: str, kind: str = "final") -> AsrEvent:
    return AsrEvent(kind=kind, text=text, start_ms=0, end_ms=500)  # type: ignore[arg-type]


class Recogniser:
    """A recogniser that says what it is told to and counts what is done to it."""

    def __init__(self, *, fail_open: bool = False, seekable: bool = False) -> None:
        self.fail_open = fail_open
        self.opens = 0
        self.closes = 0
        self.pushed: list[bytes] = []
        self.healthy = True
        self.sought: list[int] = []
        self.flush_on_close: AsrEvent | None = None
        self.crash: BaseException | None = None
        self._queue: asyncio.Queue[AsrEvent | None] = asyncio.Queue()
        if seekable:
            self.seek = self.sought.append  # type: ignore[assignment]

    async def open(self, cfg: AsrConfig) -> None:
        self.opens += 1
        if self.fail_open:
            raise ConnectionError("refused")

    async def push(self, pcm: bytes) -> None:
        self.pushed.append(pcm)

    async def events(self) -> AsyncIterator[AsrEvent]:
        while True:
            item = await self._queue.get()
            if self.crash is not None:
                raise self.crash
            if item is None:
                return
            yield item

    def say(self, *events: AsrEvent) -> None:
        for item in events:
            self._queue.put_nowait(item)

    async def close(self) -> None:
        self.closes += 1
        if self.flush_on_close is not None:
            self._queue.put_nowait(self.flush_on_close)
        self._queue.put_nowait(None)


def session(first=None, factory=None, ended=None) -> AsrSession:
    return AsrSession(
        first=first,
        factory=factory,
        config=CONFIG,
        on_ended=(lambda failed: ended.append(failed)) if ended is not None else (lambda _: None),
    )


def test_a_session_needs_a_provider_or_a_factory() -> None:
    with pytest.raises(ValueError):
        session()


async def test_the_first_open_uses_the_provider_built_at_startup() -> None:
    first = Recogniser()
    built: list[Recogniser] = []

    def factory() -> Recogniser:
        built.append(Recogniser())
        return built[-1]

    asr = session(first, factory)
    await asr.open(0)
    assert asr.provider is first and first.opens == 1 and built == []

    await asr.close()
    await asr.open(0)
    # Never the same adapter twice: a closed one may hold a replayed end-of-stream marker.
    assert asr.provider is built[0] and first.opens == 1 and built[0].opens == 1
    await asr.close()


async def test_a_provider_without_a_factory_is_the_only_one() -> None:
    only = Recogniser()
    asr = session(only)

    await asr.open(0)
    await asr.close()

    assert asr.provider is None and only.closes == 1


async def test_a_failed_open_closes_what_it_began_and_raises() -> None:
    broken = Recogniser(fail_open=True)
    asr = session(broken)

    with pytest.raises(ConnectionError):
        await asr.open(0)

    assert not asr.is_open
    assert broken.closes == 1
    assert asr.provider is None


async def test_a_cancelled_open_closes_the_half_open_stream() -> None:
    class Slow(Recogniser):
        async def open(self, cfg: AsrConfig) -> None:
            self.opens += 1
            await asyncio.sleep(3600)

    slow = Slow()
    asr = session(slow)
    task = asyncio.create_task(asr.open(0))
    await wait_until(lambda: slow.opens == 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert slow.closes == 1 and not asr.is_open


async def test_a_provider_that_must_stay_aligned_is_told_where_the_audio_clock_is() -> None:
    seekable = Recogniser(seekable=True)
    asr = session(seekable)

    await asr.open(12_345)

    assert seekable.sought == [12_345]
    await asr.close()


async def test_a_provider_that_cannot_seek_opens_normally() -> None:
    asr = session(Recogniser())

    await asr.open(12_345)

    assert asr.is_open
    await asr.close()


async def test_audio_goes_to_the_provider_only_while_open() -> None:
    provider = Recogniser()
    asr = session(provider)

    await asr.push(b"before")
    await asr.open(0)
    await asr.push(b"during")
    await asr.close()
    await asr.push(b"after")

    assert provider.pushed == [b"during"]


async def test_what_the_provider_says_is_drained_in_order_and_once() -> None:
    provider = Recogniser()
    asr = session(provider)
    await asr.open(0)

    provider.say(event("one"), event("two", "partial"))
    await wait_until(lambda: len(asr._inbox) == 2)

    assert [e.text for e in asr.drain()] == ["one", "two"]
    assert asr.drain() == []
    await asr.close()


async def test_close_discards_whatever_was_not_read() -> None:
    provider = Recogniser()
    asr = session(provider)
    await asr.open(0)
    provider.say(event("unread"))
    await wait_until(lambda: len(asr._inbox) == 1)

    await asr.close()

    assert asr.drain() == []
    assert not asr.is_open


async def test_finish_keeps_the_flush_a_provider_sends_as_it_closes() -> None:
    """The last line of the lecture arrives while the stream is closing."""

    provider = Recogniser()
    provider.flush_on_close = event("the last words")
    asr = session(provider)
    await asr.open(0)

    await asr.finish()

    assert [e.text for e in asr.drain()] == ["the last words"]
    assert not asr.is_open


async def test_open_clears_events_left_from_a_previous_stream() -> None:
    first, second = Recogniser(), Recogniser()
    asr = session(first, lambda: second)
    await asr.open(0)
    first.say(event("stale"))
    await wait_until(lambda: len(asr._inbox) == 1)
    asr._open = False  # as the supervisor does on its way to a reopen
    await asr.close()

    await asr.open(0)

    assert asr.drain() == []
    await asr.close()


async def test_a_stream_that_ends_by_itself_is_reported_as_not_a_failure() -> None:
    provider = Recogniser()
    ended: list[bool] = []
    asr = session(provider, ended=ended)
    await asr.open(0)

    provider._queue.put_nowait(None)
    await wait_until(lambda: ended == [False])

    await asr.close()


async def test_a_stream_that_raises_is_reported_as_a_failure() -> None:
    provider = Recogniser()
    ended: list[bool] = []
    asr = session(provider, ended=ended)
    await asr.open(0)

    provider.crash = RuntimeError("socket died")
    provider.say(event("trigger"))
    await wait_until(lambda: ended == [True])

    await asr.close()


async def test_closing_it_ourselves_reports_nothing() -> None:
    """Teardown noise must not make the supervisor rebuild a provider for a session that is ending."""

    provider = Recogniser()
    ended: list[bool] = []
    asr = session(provider, ended=ended)
    await asr.open(0)

    await asr.close()
    await asyncio.sleep(0.05)

    assert ended == []


async def test_a_wedged_close_does_not_stall_teardown() -> None:
    class Wedged(Recogniser):
        async def close(self) -> None:
            self.closes += 1
            await asyncio.sleep(3600)

    provider = Wedged()

    await asyncio.wait_for(close_bounded(provider, timeout=0.05), timeout=2)

    assert provider.closes == 1
