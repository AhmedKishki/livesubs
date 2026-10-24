"""One recognition stream at a time, opened and closed on demand.

The pipeline feeds this every captured frame while it is open and reads whatever the
recogniser has said. Opening and closing are the expensive, billed parts and have rules of
their own:

* The recogniser consumed by the first open is the one built at startup, so a missing key or
  adapter fails before a viewer arrives. Every open after that is built afresh by the
  factory, so a failed or closed adapter is never reused and a queued end-of-stream marker
  can never be replayed.
* The provider is published *before* it is opened, so a cancellation during a slow connect
  can still close the half-open stream instead of leaking its socket.
* ``close`` clears the open flag first, synchronously, so capture stops handing audio to a
  provider that is being closed.
* Events are collected by one task and read by the capture loop without awaiting, so
  draining never yields.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable

from livesubs.asr.base import AsrConfig, AsrEvent, AsrProvider, Seekable
from livesubs.pipeline.lifecycle import cancel_bounded

__all__ = ["AsrSession"]

logger = logging.getLogger(__name__)

#: How long to wait for the provider's closing flush to reach the inbox.
FLUSH_GRACE_S = 0.5

#: How long a provider's ``close`` is allowed to take on a teardown.
CLOSE_GRACE_S = 2.0
OPEN_GRACE_S = 10.0


class AsrSession:
    """The recogniser, its event collector and the inbox between them and the pipeline."""

    def __init__(
        self,
        *,
        first: AsrProvider | None,
        factory: Callable[[], AsrProvider] | None,
        config: AsrConfig,
        on_ended: Callable[[bool], None],
    ) -> None:
        if first is None and factory is None:
            raise ValueError("an AsrSession needs a provider or a factory that builds one")

        self._first = first
        self._factory = factory
        self._config = config
        #: Called, with whether it was a failure, when a stream ends that nobody closed.
        self._on_ended = on_ended

        self._provider: AsrProvider | None = None
        self._collector: asyncio.Task[None] | None = None
        self._open = False
        self._first_used = False
        self._inbox: deque[AsrEvent] = deque()

        #: Streams opened, and the seconds they were open. A stream is billed by the time it
        #: is open, so the second figure is what the operator watches to see that paid work
        #: really does stop when the room empties.
        self.opens = 0
        self._closed_seconds = 0.0
        self._opened_at: float | None = None
        self.origin_ms: int | None = None
        self._collecting_provider: AsrProvider | None = None
        self._generation = 0
        self._collector_generation = 0
        # Failed adapters cannot be reused: an opening or closing coroutine may
        # outlive cancellation and otherwise interfere with a later use of that object.
        self._abandoned: dict[int, AsrProvider] = {}
        self._late_closes: set[asyncio.Task[None]] = set()

    # --- state ----------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def healthy(self) -> bool:
        provider = self._provider
        return provider is not None and provider.healthy

    @property
    def provider(self) -> AsrProvider | None:
        return self._provider

    @property
    def open_seconds(self) -> float:
        """Total time a stream has been open, including the one open now."""

        running = 0.0 if self._opened_at is None else time.monotonic() - self._opened_at
        return self._closed_seconds + running

    def _note_closed(self) -> None:
        if self._opened_at is not None:
            self._closed_seconds += time.monotonic() - self._opened_at
            self._opened_at = None

    # --- opening and closing --------------------------------------------------------

    def _next(self) -> AsrProvider:
        if self._factory is None:
            assert self._first is not None
            return self._first
        if self._first is not None and not self._first_used:
            self._first_used = True
            return self._first
        return self._factory()

    async def open(
        self, audio_ms: int, *, config: AsrConfig | None = None, defer_origin: bool = False
    ) -> None:
        """Open a stream. Leaves nothing behind if it raises or is cancelled.

        ``audio_ms`` is where the audio clock stands, for a provider that must stay
        aligned with it (``seek``).
        """

        provider = self._next()
        if id(provider) in self._abandoned:
            raise RuntimeError("cannot reopen an abandoned recognition provider")
        self._generation += 1
        generation = self._generation
        self._provider = provider
        opening = asyncio.create_task(provider.open(config or self._config))
        try:
            done, _ = await asyncio.wait({opening}, timeout=OPEN_GRACE_S)
            if not done:
                raise TimeoutError("recognition opening timed out")
            await opening
            if generation != self._generation or self._provider is not provider:
                raise asyncio.CancelledError
            # Inside the guard on purpose: a seek that raises must close the recogniser
            # rather than leave it open and unused.
            self.origin_ms = None if defer_origin else audio_ms
            if not defer_origin and isinstance(provider, Seekable):
                provider.seek(audio_ms)
            self._inbox.clear()
            self._open = True
            self.opens += 1
            self._opened_at = time.monotonic()
            self._collecting_provider = provider
            self._collector_generation += 1
            self._collector = asyncio.create_task(
                self._collect(provider, self._collector_generation), name="livesubs:asr:collect"
            )
        except BaseException:
            self._abandoned[id(provider)] = provider
            try:
                await cancel_bounded(opening)
            finally:
                if not opening.done():
                    # A close now cannot undo a connection established later. Keep
                    # this exact adapter quarantined and close it again on completion.
                    opening.add_done_callback(lambda _: self._schedule_late_close(provider))
            if generation == self._generation:
                await self.close()
            else:
                await _close_bounded(provider)
            raise

    def _schedule_late_close(self, provider: AsrProvider) -> None:
        task = asyncio.create_task(_close_bounded(provider), name="livesubs:asr:late-close")
        self._late_closes.add(task)
        task.add_done_callback(self._late_closes.discard)

    async def close(self) -> None:
        """Stop at once and discard anything unread."""

        self._open = False
        self._generation += 1
        self._note_closed()
        provider, self._provider = self._provider, None
        collector, self._collector = self._collector, None
        self._inbox.clear()
        self._collecting_provider = None
        self._collector_generation += 1

        if provider is not None:
            await _close_bounded(provider)
        if collector is not None:
            await cancel_bounded(collector)

    async def finish(self) -> None:
        """Close the provider and keep what it says on the way out.

        A provider flushes the utterance still open when its stream closes, and that flush
        is the last line of the lecture, so the provider is closed *first* and the
        collector is given time to move the flush into the inbox, rather than hoping one
        turn of the event loop is enough. Unlike :meth:`close` it does not discard
        anything.
        """

        self._open = False
        self._generation += 1
        self._note_closed()
        provider, self._provider = self._provider, None
        collector, self._collector = self._collector, None

        if provider is not None:
            await _close_bounded(provider)

        if collector is not None:
            try:
                done, _ = await asyncio.wait({collector}, timeout=FLUSH_GRACE_S)
                if done:
                    await collector
                else:
                    await cancel_bounded(collector)
            except Exception:
                logger.exception("event collector failed during shutdown")
        self._collecting_provider = None
        self._collector_generation += 1

    # --- audio in, events out -------------------------------------------------------

    async def push(self, pcm: bytes, *, audio_ms: int | None = None) -> None:
        provider = self._provider
        if provider is not None and self._open:
            try:
                if self.origin_ms is None and audio_ms is not None:
                    self.origin_ms = audio_ms
                    if isinstance(provider, Seekable):
                        provider.seek(audio_ms)
                await provider.push(pcm)
            except Exception:  # noqa: BLE001 - provider details must not enter control diagnostics
                logger.warning("recognition audio delivery failed")
                self._open = False
                self._on_ended(True)

    def drain(self) -> list[AsrEvent]:
        """Everything the recogniser has said since the last call. Never awaits."""

        events = list(self._inbox)
        self._inbox.clear()
        return events

    def discard(self) -> None:
        self._inbox.clear()

    async def _collect(self, provider: AsrProvider, generation: int) -> None:
        """Move provider events into the inbox. The only awaiting reader."""

        try:
            async for event in provider.events():
                if self._collecting_provider is provider and generation == self._collector_generation:
                    self._inbox.append(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failure while we are deliberately closing, or from a provider that has
            # already been replaced, is expected teardown noise from a socket being
            # torn down. It must not be reported to the operator or make the supervisor
            # rebuild a provider for a session that is ending.
            if not self._still_current(provider) or generation != self._collector_generation:
                logger.debug("ASR event stream ended during teardown")
                return
            logger.exception("ASR event stream failed")
            self._on_ended(True)
        else:
            # The provider ended on its own - unhealthy, or hung up. Say so, unless we
            # are the ones closing.
            if self._still_current(provider) and generation == self._collector_generation:
                self._on_ended(False)

    def _still_current(self, provider: AsrProvider) -> bool:
        return self._open and self._provider is provider


async def _close_bounded(asr: AsrProvider, timeout: float = CLOSE_GRACE_S) -> None:
    """Close a provider, bounded so a wedged socket cannot stall teardown."""

    closing = asyncio.create_task(asr.close())
    try:
        done, _ = await asyncio.wait({closing}, timeout=timeout)
        if done:
            await closing
        else:
            logger.warning("ASR provider did not close within %.1fs", timeout)
            await cancel_bounded(closing)
    except asyncio.CancelledError:
        await cancel_bounded(closing)
        raise
    except Exception:
        logger.exception("ASR provider failed to close")
