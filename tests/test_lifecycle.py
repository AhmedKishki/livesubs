"""The on-demand rules, once, for anything the pipeline starts only while viewers are there.

Everything here uses a fake resource and a real :class:`~livesubs.hub.Presence`, so the rules
the recogniser and every translator rely on are pinned apart from either of them.
"""

from __future__ import annotations

import asyncio

import pytest
from support import wait_until

from livesubs.hub import Presence
from livesubs.pipeline import lifecycle
from livesubs.pipeline.lifecycle import DemandChanged, Lifecycle, backoff_seconds


class Resource:
    """Counts starts and stops, and can be told to open slowly or fail."""

    def __init__(self) -> None:
        self.starts: list[int] = []
        self.stops = 0
        self.fail_next = 0
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.cancelled = False
        self.raise_demand_changed = False

    async def start(self, generation: int) -> None:
        self.starts.append(generation)
        self.entered.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.raise_demand_changed:
            raise DemandChanged
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("refused")

    async def stop(self) -> None:
        self.stops += 1


def make(resource: Resource, *, retry_limit: int = 5):
    audience = Presence()
    trouble: list[str] = []
    ok: list[int] = []
    life = Lifecycle(
        name="thing",
        audience=audience,
        start=resource.start,
        stop=resource.stop,
        on_trouble=trouble.append,
        on_ok=lambda: ok.append(1),
        retry_limit=retry_limit,
    )
    life.spawn()
    return life, audience, trouble, ok


@pytest.fixture
def quick_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "_BACKOFF_INITIAL_S", 0.01)
    monkeypatch.setattr(lifecycle, "_BACKOFF_MAX_S", 0.04)


def test_the_back_off_doubles_and_is_capped() -> None:
    assert [backoff_seconds(n) for n in (0, 1, 2, 3, 4)] == [0.5, 0.5, 1.0, 2.0, 4.0]
    assert backoff_seconds(10) == 5.0


async def test_nothing_starts_without_an_audience() -> None:
    resource = Resource()
    life, _, _, _ = make(resource)

    await asyncio.sleep(0.05)

    assert resource.starts == [] and not life.running
    await life.close()


async def test_the_first_viewer_starts_it_and_the_last_to_leave_stops_it() -> None:
    resource = Resource()
    life, audience, _, _ = make(resource)

    audience.update(1)
    await wait_until(lambda: life.running)
    assert resource.starts == [audience.generation]
    assert life.current

    audience.update(2)
    await asyncio.sleep(0.05)
    assert resource.starts == [1], "a second viewer is not a new audience"

    audience.update(0)
    await wait_until(lambda: not life.running)
    assert resource.stops == 1 and not life.current
    await life.close()


async def test_leaving_and_returning_before_the_loop_looks_starts_it_afresh() -> None:
    """Going to zero and back advances the generation, so the old resource is not reused."""

    resource = Resource()
    life, audience, _, _ = make(resource)
    audience.update(1)
    await wait_until(lambda: life.running)

    audience.update(0)
    audience.update(1)

    await wait_until(lambda: resource.starts == [1, 3])
    assert resource.stops == 1
    assert life.current
    await life.close()


async def test_a_start_is_cancelled_when_the_audience_leaves_while_it_is_opening() -> None:
    resource = Resource()
    resource.gate = asyncio.Event()
    life, audience, _, _ = make(resource)

    audience.update(1)
    await resource.entered.wait()
    audience.update(0)

    await wait_until(lambda: resource.cancelled)
    assert not life.running
    await life.close()


async def test_a_start_that_finishes_as_the_audience_leaves_is_stopped() -> None:
    """Opened just as everyone left: it must not be left running for nobody."""

    resource = Resource()
    resource.gate = asyncio.Event()
    life, audience, _, _ = make(resource)
    audience.update(1)
    await resource.entered.wait()

    audience.update(0)
    resource.gate.set()

    await wait_until(lambda: resource.stops >= 1 or resource.cancelled)
    await asyncio.sleep(0.05)
    assert not life.running
    await life.close()


async def test_a_start_that_gives_up_because_demand_changed_is_not_a_failure() -> None:
    resource = Resource()
    resource.raise_demand_changed = True
    life, audience, trouble, _ = make(resource)

    audience.update(1)
    await wait_until(lambda: resource.starts)
    audience.update(0)
    await asyncio.sleep(0.05)

    assert trouble == [] and not life.running
    await life.close()


@pytest.mark.usefixtures("quick_backoff")
async def test_a_failed_start_is_retried_and_then_recovers() -> None:
    resource = Resource()
    resource.fail_next = 2
    life, audience, trouble, ok = make(resource)

    audience.update(1)
    await wait_until(lambda: life.running)

    assert len(resource.starts) == 3
    assert trouble == ["thing could not be started"] * 2
    assert ok, "recovery is reported so the status can return to live"
    await life.close()


@pytest.mark.usefixtures("quick_backoff")
async def test_a_resource_that_dies_is_stopped_and_started_again() -> None:
    resource = Resource()
    life, audience, trouble, _ = make(resource)
    audience.update(1)
    await wait_until(lambda: life.running)

    life.fail()

    await wait_until(lambda: len(resource.starts) == 2 and life.running)
    assert resource.stops == 1
    assert trouble == ["thing stopped unexpectedly"]
    await life.close()


async def test_failing_something_not_running_does_nothing() -> None:
    resource = Resource()
    life, _, trouble, _ = make(resource)

    life.fail()
    await asyncio.sleep(0.05)

    assert resource.starts == [] and trouble == []
    await life.close()


@pytest.mark.usefixtures("quick_backoff")
async def test_after_the_retry_limit_it_waits_for_the_audience_instead_of_hammering() -> None:
    resource = Resource()
    resource.fail_next = 10_000
    life, audience, trouble, _ = make(resource, retry_limit=2)

    audience.update(1)
    await wait_until(lambda: len(trouble) >= 3)
    attempts = len(resource.starts)
    await asyncio.sleep(0.2)

    assert len(resource.starts) == attempts, "it stopped trying while the audience was unchanged"

    resource.fail_next = 0
    audience.update(0)
    audience.update(1)
    await wait_until(lambda: life.running)
    await life.close()


@pytest.mark.usefixtures("quick_backoff")
async def test_nudge_makes_it_look_again_now() -> None:
    resource = Resource()
    life, audience, _, _ = make(resource)
    audience.update(1)
    await wait_until(lambda: life.running)
    life._failed = True  # as a stalled owner would find out by itself

    life.nudge()

    await wait_until(lambda: len(resource.starts) == 2)
    await life.close()


async def test_closing_returns_promptly_and_leaves_the_resource_to_its_owner() -> None:
    resource = Resource()
    life, audience, _, _ = make(resource)
    audience.update(1)
    await wait_until(lambda: life.running)

    await asyncio.wait_for(life.close(), timeout=1)

    assert resource.stops == 0, "the owner stops it, in the order it needs"
    assert life.running


async def test_closing_a_supervisor_that_never_started_is_harmless() -> None:
    life = Lifecycle(
        name="idle",
        audience=Presence(),
        start=Resource().start,
        stop=Resource().stop,
    )

    await life.close()
