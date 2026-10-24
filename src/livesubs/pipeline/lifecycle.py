"""A resource that exists while somebody needs it.

Paid work - a recogniser stream, a warmed translator - must run only while viewers are
there, start when the first one arrives, stop when the last one leaves, survive a failure
by starting again, and never be left running for an audience that went away while it was
being set up. The same rules hold for the recogniser and for every target language's
translator, so they are written once, here, and each of those is an instance.

The cost of getting them wrong is billed work with nobody watching, or a lecture that
silently stops, so the rules are explicit:

* A start is raced against the audience. If everyone leaves, or the audience changes, while
  the resource is still opening, the open is cancelled and whatever it began is closed.
* The audience's *generation* is recorded at start. Going to zero and back advances it
  even if this loop never saw the empty moment, so a resource that belongs to the previous
  audience is stopped and started afresh instead of being mistaken for the current one.
* A resource that dies is stopped and started again after a bounded back-off, up to a
  limit, after which it waits for the audience to change instead of hammering a provider.
* Every wait is also woken by :meth:`Lifecycle.close`, so shutdown never has to wait out a
  timeout for a supervisor that is asleep.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from livesubs.hub import Presence

__all__ = ["DemandChanged", "Lifecycle", "backoff_seconds", "cancel_bounded"]

logger = logging.getLogger(__name__)

#: Start attempts retried, with a bounded back-off, while viewers remain but the resource
#: cannot be started. After this many it waits for the audience to change.
RETRY_LIMIT = 5
_BACKOFF_INITIAL_S = 0.5
_BACKOFF_MAX_S = 5.0

#: How long a task is given to respond to cancellation before the pipeline gives up on it.
TASK_CANCEL_GRACE_S = 2.0

_OPENED, _FAILED, _DEMAND_LOST = "opened", "failed", "demand_lost"


class DemandChanged(Exception):
    """Raised inside a start when the audience it began for has gone."""


def backoff_seconds(failures: int) -> float:
    """Exponential back-off, capped, for consecutive failures."""

    return min(_BACKOFF_MAX_S, _BACKOFF_INITIAL_S * (2 ** max(0, failures - 1)))


async def cancel_bounded(task: asyncio.Task, timeout: float = TASK_CANCEL_GRACE_S) -> None:
    """Cancel a task, waiting a bounded time, and never raise."""

    if task.done():
        _consume(task)
        return

    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.add_done_callback(_consume)
        return
    try:
        await task
    except (TimeoutError, asyncio.CancelledError):
        pass
    except Exception:
        logger.exception("task %s errored while being cancelled", task.get_name())


def _consume(task: asyncio.Task) -> None:
    """Retrieve a finished task's result, so a failure is not logged as unretrieved."""

    try:
        task.exception()
    except BaseException:  # noqa: BLE001 - a cancelled task raises here; nothing to record
        logger.debug("cancelled task %s had no retrievable result", task.get_name())


class Lifecycle:
    """Starts a resource while its audience is present and stops it when the audience goes.

    ``start(generation)`` brings the resource up for the audience of that generation. It may
    raise :class:`DemandChanged` after an ``await`` if the audience has gone, and it must
    leave nothing running if it raises or is cancelled. ``stop()`` takes it down.
    """

    def __init__(
        self,
        *,
        name: str,
        audience: Presence,
        start: Callable[[int], Awaitable[None]],
        stop: Callable[[], Awaitable[None]],
        on_trouble: Callable[[str], None] | None = None,
        on_ok: Callable[[], None] | None = None,
        retry_limit: int = RETRY_LIMIT,
    ) -> None:
        self.name = name
        self._audience = audience
        self._start = start
        self._stop = stop
        self._on_trouble = on_trouble
        self._on_ok = on_ok
        self._retry_limit = retry_limit

        self._wake = asyncio.Event()
        self._closing = False
        self._failed = False
        self._running = False
        self._generation: int | None = None
        self._failures = 0
        self.revision = 0
        self._active_revision = -1
        self._task: asyncio.Task[None] | None = None

    # --- what the owner can ask -----------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether a resource has been started and not stopped."""

        return self._running

    @property
    def exhausted(self) -> bool:
        return self._failures > self._retry_limit

    @property
    def current(self) -> bool:
        """Whether the running resource belongs to the audience that is here now."""

        return (
            self._running
            and self._audience.present
            and self._generation == self._audience.generation
            and self._active_revision == self.revision
        )

    def invalidate(self) -> None:
        """Replace configuration without treating the intentional change as failure."""
        self.revision += 1
        self._failures = 0
        self._wake.set()

    def fail(self) -> None:
        """The running resource has died: stop it and start it again."""

        if self._running:
            self._failed = True
            self._wake.set()

    def nudge(self) -> None:
        """Look at the situation again now."""

        self._wake.set()

    def spawn(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name=f"livesubs:life:{self.name}")

    def adopt(self) -> None:
        """Supervise a resource the owner has already opened synchronously."""
        self._running = True
        self._generation = self._audience.generation
        self._active_revision = self.revision

    async def close(self, grace: float = TASK_CANCEL_GRACE_S) -> None:
        """Stop supervising. The owner stops the resource itself, in the order it needs."""

        self._closing = True
        self._wake.set()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace)
        except TimeoutError:
            logger.warning("%s supervisor did not stop in time; cancelling", self.name)
            await cancel_bounded(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s supervisor failed during shutdown", self.name)

    # --- the loop -------------------------------------------------------------------

    async def _supervise(self) -> None:
        audience = self._audience
        try:
            while not self._closing:
                generation = audience.generation

                if not audience.present:
                    self._failures = 0
                    await self._release()
                    self._ok()
                    await self._pause(generation)
                    continue

                if self.current and not self._failed:
                    self._failures = 0
                    await self._pause(generation)
                    continue

                if self._running:
                    # Either it died, or it belongs to an audience that has since left and
                    # returned. Both mean a fresh start; only the first is a failure.
                    died = (
                        self._failed and self._active_revision == self.revision
                        and self._generation == generation
                    )
                    await self._release()
                    if died:
                        self._failures += 1
                        self._trouble(f"{self.name} stopped unexpectedly")
                        await self._back_off(generation)
                        continue

                outcome = await self._attempt(generation)
                if outcome == _OPENED:
                    self._failures = 0
                    self._ok()
                elif outcome == _FAILED:
                    self._failures += 1
                    self._trouble(f"{self.name} could not be started")
                    await self._back_off(generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s supervisor stopped unexpectedly", self.name)

    async def _back_off(self, generation: int) -> None:
        """Wait before the next start, cut short by a change of audience or by closing."""

        if self._failures <= self._retry_limit:
            await self._pause(generation, backoff_seconds(self._failures))
        else:
            # Out of retries. Wait for the audience to change instead of hammering a
            # provider that has refused this many times.
            await self._pause(generation)
            self._failures = 0

    async def _pause(self, generation: int, timeout: float | None = None) -> None:
        """Block until the audience changes, the owner nudges, or ``timeout`` passes."""

        changed = asyncio.create_task(self._audience.wait_change(generation, timeout))
        woke = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait({changed, woke}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (changed, woke):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(changed, woke, return_exceptions=True)
        if woke.done() and not woke.cancelled():
            self._wake.clear()

    async def _release(self) -> None:
        was_running = self._running
        self._running = False
        self._failed = False
        if was_running:
            await self._stop()

    async def _attempt(self, generation: int) -> str:
        """Run one start, abandoning it if the audience changes underneath it."""

        audience = self._audience
        revision = self.revision
        task = asyncio.create_task(self._start(generation), name=f"livesubs:start:{self.name}")
        try:
            while not task.done():
                if (
                    not audience.present or audience.generation != generation or self._closing
                    or revision != self.revision
                ):
                    break
                changed = asyncio.create_task(audience.wait_change(generation))
                woke = asyncio.create_task(self._wake.wait())
                try:
                    await asyncio.wait({task, changed, woke}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for pending in (changed, woke):
                        if not pending.done():
                            pending.cancel()
                    await asyncio.gather(changed, woke, return_exceptions=True)
                if woke.done() and not woke.cancelled():
                    self._wake.clear()
            if not task.done():
                await cancel_bounded(task)
        finally:
            if not task.done():
                await cancel_bounded(task)

        if task.cancelled():
            return _DEMAND_LOST
        if not task.done():
            return _DEMAND_LOST
        error = task.exception()
        if error is not None and revision != self.revision:
            return _DEMAND_LOST
        if error is None:
            if (
                self._closing or not audience.present or audience.generation != generation
                or revision != self.revision
            ):
                # Opened just as the audience left. It must not be left running for nobody.
                await self._stop()
                return _DEMAND_LOST
            self._running = True
            self._generation = generation
            self._active_revision = revision
            return _OPENED
        if isinstance(error, DemandChanged):
            return _DEMAND_LOST
        logger.error("%s could not be started", self.name, exc_info=error)
        return _FAILED

    # --- reporting ------------------------------------------------------------------

    def _trouble(self, detail: str) -> None:
        if self._on_trouble is not None:
            self._on_trouble(detail)

    def _ok(self) -> None:
        if self._on_ok is not None:
            self._on_ok()
