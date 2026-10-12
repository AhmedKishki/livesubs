"""Tests for fan-out, ordering and the German-only guarantee.

The mailbox is the component that protects the lecture: if a slow viewer could
block or grow memory, one bad phone on hall WiFi would degrade transcription for
everyone.
"""

from __future__ import annotations

import asyncio
import inspect

from livesubs.events import (
    ErrorMessage,
    PipelineStatus,
    SegmentMessage,
    SegmentState,
    Stage,
    SubtitleSegment,
    Timing,
)
from livesubs.hub import ClientRole, Hub


def _seg(
    seq: int = 1,
    rev: int = 0,
    *,
    state: SegmentState = SegmentState.DRAFT,
    src: str | None = "The amplitude decreases exponentially.",
    de: str = "Die Amplitude nimmt mit der Zeit exponentiell ab.",
) -> SubtitleSegment:
    return SubtitleSegment(
        seq=seq,
        rev=rev,
        state=state,
        de=de,
        src=src,
        timing=Timing(audio_ms=seq * 2000, emit_ms=seq * 2000 + 400),
    )


async def _segments(mailbox) -> list[SubtitleSegment]:
    messages = await asyncio.wait_for(mailbox.receive(), timeout=1.0)
    return [m.data for m in messages if isinstance(m, SegmentMessage)]


# --- the hot path must not block ---------------------------------------------


def test_publish_is_synchronous() -> None:
    """A slow viewer must never be able to stall the pipeline."""

    hub = Hub()
    assert not inspect.iscoroutinefunction(hub.publish)
    assert not inspect.iscoroutinefunction(hub.publish_segment)


async def test_many_offers_collapse_to_the_newest_segment() -> None:
    """Memory per client is constant however much is published."""

    hub = Hub()
    subscriber = hub.subscribe(ClientRole.VIEWER)

    for seq in range(50):
        hub.publish_segment(_seg(seq=seq))

    segments = await _segments(subscriber.mailbox)
    assert len(segments) == 1
    assert segments[0].seq == 49


async def test_publishing_with_no_subscribers_is_harmless() -> None:
    hub = Hub()
    hub.publish_segment(_seg())
    assert hub.subscriber_count == 0


# --- ordering ------------------------------------------------------------------


async def test_a_stale_revision_of_the_same_seq_is_refused() -> None:
    """`final` must never be followed by `draft` on the wire."""

    hub = Hub()
    subscriber = hub.subscribe(ClientRole.VIEWER)

    hub.publish_segment(_seg(seq=7, rev=2, state=SegmentState.FINAL))
    hub.publish_segment(_seg(seq=7, rev=1, state=SegmentState.DRAFT))   # stale

    segments = await _segments(subscriber.mailbox)
    assert len(segments) == 1
    assert segments[0].rev == 2
    assert segments[0].state is SegmentState.FINAL


async def test_a_new_seq_is_accepted_after_a_high_revision() -> None:
    """Revision ordering must not block the next segment."""

    hub = Hub()
    subscriber = hub.subscribe(ClientRole.VIEWER)

    hub.publish_segment(_seg(seq=3, rev=5))
    hub.publish_segment(_seg(seq=4, rev=0))

    segments = await _segments(subscriber.mailbox)
    assert [s.seq for s in segments] == [4]
    assert segments[0].rev == 0


async def test_revisions_advance_in_order() -> None:
    hub = Hub()
    subscriber = hub.subscribe(ClientRole.VIEWER)

    hub.publish_segment(_seg(seq=1, rev=0))
    assert (await _segments(subscriber.mailbox))[0].rev == 0

    hub.publish_segment(_seg(seq=1, rev=1))
    assert (await _segments(subscriber.mailbox))[0].rev == 1


# --- the German-only guarantee -------------------------------------------------


async def test_viewer_never_receives_the_english_source() -> None:
    """Enforced on the server, not trusted to the viewer page."""

    hub = Hub()
    subscriber = hub.subscribe(ClientRole.VIEWER)
    hub.publish_segment(_seg(src="The amplitude decreases exponentially."))

    segment = (await _segments(subscriber.mailbox))[0]

    assert segment.src is None
    assert "Amplitude" in segment.de          # German still delivered


async def test_admin_receives_the_english_source() -> None:
    """The operator console needs it to diagnose a bad translation."""

    hub = Hub()
    subscriber = hub.subscribe(ClientRole.ADMIN)
    hub.publish_segment(_seg(src="The amplitude decreases exponentially."))

    assert (await _segments(subscriber.mailbox))[0].src is not None


async def test_encode_cache_does_not_leak_the_source_to_viewers() -> None:
    """The cache key must include whether ``src`` was present.

    Without that flag an admin's encoding would be handed to viewers, exposing
    the English source through a cache hit rather than through a logic error.
    """

    hub = Hub()
    admin = hub.subscribe(ClientRole.ADMIN)
    viewer = hub.subscribe(ClientRole.VIEWER)

    hub.publish_segment(_seg(seq=1, src="The amplitude decreases exponentially."))

    admin_messages = await asyncio.wait_for(admin.mailbox.receive(), timeout=1.0)
    viewer_messages = await asyncio.wait_for(viewer.mailbox.receive(), timeout=1.0)

    admin_encoded = hub.encode(
        next(m for m in admin_messages if isinstance(m, SegmentMessage))
    )
    viewer_encoded = hub.encode(
        next(m for m in viewer_messages if isinstance(m, SegmentMessage))
    )

    assert "The amplitude" in admin_encoded
    assert "The amplitude" not in viewer_encoded
    assert "Die Amplitude" in viewer_encoded


def test_encode_reuses_one_serialisation_per_revision() -> None:
    """Fan-out cost is one encoding per revision, not one per viewer."""

    hub = Hub()
    message = SegmentMessage(data=_seg(seq=3, rev=1, src=None))
    hub.publish_segment(_seg(seq=3, rev=1))

    assert hub.encode(message) is hub.encode(message)


# --- late joiners and disconnects ---------------------------------------------


async def test_late_joiner_receives_a_snapshot() -> None:
    """Nobody should ever be staring at a blank screen mid-lecture."""

    hub = Hub()
    hub.publish_status(PipelineStatus.LIVE)
    hub.publish_segment(_seg(seq=12, rev=3, state=SegmentState.FINAL))

    subscriber = hub.subscribe(ClientRole.VIEWER)          # joins afterwards
    messages = await asyncio.wait_for(subscriber.mailbox.receive(), timeout=1.0)

    kinds = [m.type for m in messages]
    assert kinds == ["status", "segment"]                  # defined drain order
    assert messages[1].data.seq == 12


async def test_close_wakes_the_reader_and_returns_empty() -> None:
    """The writer task's exit condition."""

    hub = Hub()
    subscriber = hub.subscribe(ClientRole.VIEWER)

    subscriber.mailbox.close()

    assert await asyncio.wait_for(subscriber.mailbox.receive(), timeout=1.0) == []
    assert subscriber.mailbox.closed


async def test_offers_after_close_are_ignored() -> None:
    hub = Hub()
    subscriber = hub.subscribe(ClientRole.VIEWER)
    subscriber.mailbox.close()

    hub.publish_segment(_seg())
    assert subscriber.mailbox.has_pending is False


async def test_unsubscribe_stops_delivery() -> None:
    hub = Hub()
    subscriber = hub.subscribe(ClientRole.VIEWER)

    hub.unsubscribe(subscriber)
    hub.publish_segment(_seg())

    assert hub.subscriber_count == 0
    assert subscriber.mailbox.closed


# --- errors are for the operator only -----------------------------------------


async def test_errors_reach_admins_but_not_viewers() -> None:
    hub = Hub()
    viewer = hub.subscribe(ClientRole.VIEWER)
    admin = hub.subscribe(ClientRole.ADMIN)

    hub.publish_error("asr socket closed", Stage.ASR)

    admin_messages = await asyncio.wait_for(admin.mailbox.receive(), timeout=1.0)
    assert any(isinstance(m, ErrorMessage) for m in admin_messages)
    assert viewer.mailbox.has_pending is False


# --- audience accounting, which decides paid work ------------------------------


def test_viewer_count_excludes_admins() -> None:
    """An operator console is not an audience and must not create provider demand."""

    hub = Hub()
    hub.subscribe(ClientRole.ADMIN)
    hub.subscribe(ClientRole.ADMIN)

    assert hub.viewer_count == 0
    assert hub.has_viewers is False

    viewer = hub.subscribe(ClientRole.VIEWER)
    assert hub.viewer_count == 1
    assert hub.has_viewers is True

    hub.unsubscribe(viewer)
    assert hub.viewer_count == 0
    assert hub.has_viewers is False


def test_forget_segment_snapshot_drops_the_cached_line() -> None:
    """A paused lecture must not seed the next viewer with its stale last line."""

    hub = Hub()
    hub.publish_segment(_seg(seq=5, rev=0))

    assert hub._latest_segment is not None
    hub.forget_segment_snapshot()
    assert hub._latest_segment is None

    # A joiner after the reset is not handed the old line.
    subscriber = hub.subscribe(ClientRole.VIEWER)
    assert subscriber.mailbox.has_pending is False



def test_gated_hub_does_not_cache_a_segment_arriving_with_no_viewers() -> None:
    """A late translation must not seed the next viewer with text nobody watched."""

    hub = Hub()
    hub.enable_audience_gating()
    hub.publish_segment(_seg(seq=3))
    assert hub._latest_segment is None


def test_ungated_hub_still_caches_with_no_viewers() -> None:
    """The non-gated replay keeps its snapshot behaviour across a reconnect."""

    hub = Hub()
    hub.publish_segment(_seg(seq=3))
    assert hub._latest_segment is not None


def test_demand_generation_advances_only_on_a_presence_change() -> None:
    """A second viewer is not a new occupancy; an admin is not demand at all."""

    hub = Hub()
    assert hub.demand_generation == 0

    first = hub.subscribe(ClientRole.VIEWER)
    assert hub.demand_generation == 1

    second = hub.subscribe(ClientRole.VIEWER)
    assert hub.demand_generation == 1          # still just "some viewers"

    hub.subscribe(ClientRole.ADMIN)
    assert hub.demand_generation == 1          # admin adds no demand

    hub.unsubscribe(first)
    assert hub.demand_generation == 1          # second viewer still present

    hub.unsubscribe(second)
    assert hub.demand_generation == 2          # crossed back to none

    hub.subscribe(ClientRole.VIEWER)
    assert hub.demand_generation == 3          # a fresh occupancy


async def test_wait_for_demand_change_wakes_on_a_viewer_connect() -> None:
    """The watcher is notified by the connect itself, not by the next frame."""

    hub = Hub()
    generation = hub.demand_generation
    waiter = asyncio.create_task(hub.wait_for_demand_change(generation))
    await asyncio.sleep(0)
    assert not waiter.done()

    hub.subscribe(ClientRole.VIEWER)
    assert await asyncio.wait_for(waiter, timeout=1.0) == generation + 1
