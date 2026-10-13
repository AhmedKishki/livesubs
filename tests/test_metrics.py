"""What the operator console is told: windows, delivery counters, host figures, the pipeline.

The figures only ever describe; nothing here is read back to decide anything. These tests pin
that they describe correctly, because an operator acts on them during a lecture.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from support import (
    Factory,
    FlakyMt,
    RecordingHub,
    finals,
    make_gated,
    running,
    wait_until,
)

from livesubs.events import SegmentMessage, SegmentState, SubtitleSegment, Timing
from livesubs.hub import ClientMailbox, ClientRole, Hub
from livesubs.metrics import DeliveryMetrics, LoopMonitor, Window, process_stats

# --- the rolling window -----------------------------------------------------------------


def test_an_empty_window_says_it_has_no_data_rather_than_zero() -> None:
    summary = Window().summary()

    assert summary == {"count": 0, "last": None, "p50": None, "p95": None, "max": None}


def test_percentiles_are_nearest_rank() -> None:
    window = Window()
    for value in range(1, 101):
        window.add(value)

    summary = window.summary()

    assert (summary["p50"], summary["p95"], summary["max"], summary["last"]) == (50, 95, 100, 100)
    assert summary["count"] == 100


def test_a_window_follows_recent_samples_but_counts_all_of_them() -> None:
    window = Window(size=10)
    for value in range(1000, 1010):
        window.add(value)
    for value in range(10):
        window.add(value)

    summary = window.summary()

    assert summary["count"] == 20
    assert summary["max"] == 9, "the old, large samples have left the window"


def test_a_single_sample_is_every_percentile() -> None:
    window = Window()
    window.add(42.26)

    assert window.summary() == {"count": 1, "last": 42.3, "p50": 42.3, "p95": 42.3, "max": 42.3}


# --- what a client was never sent -------------------------------------------------------


def line(seq: int, rev: int, language: str = "de") -> SegmentMessage:
    return SegmentMessage(
        data=SubtitleSegment(
            seq=seq,
            rev=rev,
            state=SegmentState.DRAFT,
            language=language,
            text="x",
            timing=Timing(audio_ms=0, emit_ms=0),
        )
    )


def test_a_replaced_draft_and_a_replaced_line_are_counted_apart() -> None:
    mailbox = ClientMailbox(ClientRole.viewer("de"), ("de",))

    mailbox.offer(line(0, 0))
    mailbox.offer(line(0, 1))  # a newer revision of the line still waiting: coalesced
    mailbox.offer(line(1, 0))  # a new line while the old one was never written: skipped

    assert (mailbox.skipped_drafts, mailbox.skipped_lines) == (1, 1)


async def test_a_client_that_keeps_up_skips_nothing() -> None:
    mailbox = ClientMailbox(ClientRole.viewer("de"), ("de",))

    for seq in range(5):
        mailbox.offer(line(seq, 0))
        await mailbox.receive()

    assert (mailbox.skipped_drafts, mailbox.skipped_lines) == (0, 0)


def test_a_message_the_client_may_not_read_is_not_a_skip() -> None:
    mailbox = ClientMailbox(ClientRole.viewer("de"), ("de", "ar"))

    mailbox.offer(line(0, 0, "ar"))
    mailbox.offer(line(1, 0, "ar"))

    assert (mailbox.skipped_drafts, mailbox.skipped_lines) == (0, 0)


# --- delivery ---------------------------------------------------------------------------


async def test_connections_peaks_and_totals_survive_a_viewer_leaving() -> None:
    hub = Hub(("de", "ar"))
    a = hub.subscribe(ClientRole.viewer("de"))
    b = hub.subscribe(ClientRole.viewer("de"))
    c = hub.subscribe(ClientRole.viewer("ar"))
    operator = hub.subscribe(ClientRole.admin())

    for subscriber in (a, b, c, operator):
        hub.unsubscribe(subscriber)

    snapshot = hub.metrics.snapshot(hub)
    assert snapshot["peak_viewers"] == 3
    assert snapshot["peak_by_language"] == {"de": 2, "ar": 1}
    assert (snapshot["connects"], snapshot["disconnects"]) == (3, 3), "an operator is not a viewer"
    assert snapshot["viewers"] == 0 and snapshot["connections"] == 0


async def test_operators_are_counted_apart_from_viewers() -> None:
    hub = Hub(("de",))
    hub.subscribe(ClientRole.viewer("de"))
    hub.subscribe(ClientRole.admin())

    snapshot = hub.metrics.snapshot(hub)

    assert (snapshot["viewers"], snapshot["operators"], snapshot["connections"]) == (1, 1, 2)


async def test_a_closed_connections_skips_stay_in_the_total() -> None:
    hub = Hub(("de",))
    slow = hub.subscribe(ClientRole.viewer("de"))
    for seq in range(4):
        hub.publish(line(seq, 0))

    assert hub.metrics.snapshot(hub)["skipped_lines"] == 3
    hub.unsubscribe(slow)

    assert hub.metrics.snapshot(hub)["skipped_lines"] == 3


def test_render_times_are_kept_per_language_and_dropped_lines_are_summed() -> None:
    metrics = DeliveryMetrics(("de", "ar"))

    metrics.note_ack("c1", "de", 40, 0)
    metrics.note_ack("c1", "de", 60, 2)
    metrics.note_ack("c2", "ar", 100, 1)

    hub = Hub(("de", "ar"))
    snapshot = metrics.snapshot(hub)
    assert snapshot["render_ms"]["de"]["count"] == 2
    assert snapshot["render_ms"]["ar"]["p50"] == 100
    # The latest report of each connection, not the sum of every report.
    assert snapshot["dropped_lines"] == 3


async def test_dropped_lines_of_a_viewer_who_left_still_count() -> None:
    hub = Hub(("de",))
    subscriber = hub.subscribe(ClientRole.viewer("de"))
    hub.metrics.note_ack(subscriber.id, "de", 10, 4)

    hub.unsubscribe(subscriber)

    assert hub.metrics.snapshot(hub)["dropped_lines"] == 4


def test_a_viewer_cannot_report_a_negative_time_or_count() -> None:
    metrics = DeliveryMetrics(("de",))

    metrics.note_ack("c1", "de", -500, -7)

    snapshot = metrics.snapshot(Hub(("de",)))
    assert snapshot["render_ms"]["de"]["last"] == 0
    assert snapshot["dropped_lines"] == 0


def test_an_ack_without_figures_changes_nothing() -> None:
    metrics = DeliveryMetrics(("de",))

    metrics.note_ack("c1", "de", None, None)

    snapshot = metrics.snapshot(Hub(("de",)))
    assert snapshot["render_ms"]["de"]["count"] == 0 and snapshot["dropped_lines"] == 0


def test_what_was_sent_is_counted() -> None:
    metrics = DeliveryMetrics(("de",))

    metrics.note_sent(3, 900, 1.5)
    metrics.note_sent(1, 100, 0.5)

    snapshot = metrics.snapshot(Hub(("de",)))
    assert (snapshot["messages_sent"], snapshot["bytes_sent"]) == (4, 1000)
    assert snapshot["send_ms"]["count"] == 2 and snapshot["send_ms"]["max"] == 1.5


def test_the_hub_reports_each_scopes_latest_health() -> None:
    from livesubs.events import PipelineStatus

    hub = Hub(("de", "ar"))
    hub.publish_status(PipelineStatus.LIVE)
    hub.publish_status(PipelineStatus.DELAYED, "translation is behind", "ar")

    assert hub.current_status() == {
        "all": {"status": "live", "detail": None},
        "ar": {"status": "delayed", "detail": "translation is behind"},
    }


# --- the host ---------------------------------------------------------------------------


async def test_the_loop_monitor_notices_a_blocked_loop() -> None:
    monitor = LoopMonitor()
    monitor.start()
    try:
        await asyncio.sleep(0.6)
        quiet = monitor.lag_ms.summary()["max"]
        # A handler that blocks the loop, which is what this exists to show. Blocked for half
        # a second against a 0.25 s tick, the lateness is at least 0.25 s wherever in the
        # tick the block lands.
        time.sleep(0.5)  # noqa: ASYNC251 - blocking the loop is the point of this test
        await asyncio.sleep(0.4)
    finally:
        await monitor.stop()

    assert quiet is not None and quiet < 100
    assert monitor.lag_ms.summary()["max"] >= 200


async def test_the_loop_monitor_stops_cleanly_and_can_be_stopped_twice() -> None:
    monitor = LoopMonitor()
    monitor.start()
    monitor.start()  # a second start must not leave a second task behind
    await monitor.stop()
    await monitor.stop()


def test_process_stats_describe_this_process() -> None:
    stats = process_stats()

    assert stats["open_files"] >= 3
    assert stats["open_files_limit"] >= stats["open_files"]
    assert stats["rss_mb"] > 10
    assert stats["threads"] >= 1


# --- the live pipeline ------------------------------------------------------------------


def viewer(language: str) -> ClientRole:
    return ClientRole.viewer(language)


async def test_a_language_nobody_reads_reports_no_work() -> None:
    pipeline, _ = make_gated(translators={"de": Factory(), "ar": Factory()})
    hub = RecordingHub(("de", "ar"))

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer("de"))
        await wait_until(lambda: bool(finals(hub, "de")))
        snapshot = pipeline.snapshot()
        hub.unsubscribe(reader)

    german, arabic = snapshot["languages"]["de"], snapshot["languages"]["ar"]
    assert german["active"] and german["readers"] == 1 and german["completed"] >= 1
    assert (arabic["active"], arabic["warmed"], arabic["readers"], arabic["completed"]) == (
        False,
        False,
        0,
        0,
    )


async def test_the_snapshot_reports_signal_recognition_and_latency() -> None:
    pipeline, _ = make_gated(translators={"de": Factory()})
    hub = RecordingHub(("de",))

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer("de"))
        await wait_until(lambda: bool(finals(hub, "de")))
        snapshot = pipeline.snapshot()
        hub.unsubscribe(reader)

    assert snapshot["kind"] == "live"
    assert snapshot["audio"]["capturing"] is True
    assert snapshot["audio"]["rms_dbfs"] < 0 and snapshot["audio"]["audio_s"] > 0
    assert snapshot["recognition"]["open"] is True
    assert snapshot["recognition"]["streams_opened"] == 1
    assert snapshot["recognition"]["open_s"] > 0
    assert snapshot["segments"]["committed"] >= 1
    german = snapshot["languages"]["de"]
    assert german["latency_ms"]["count"] >= 1 and german["latency_ms"]["p50"] >= 0
    assert german["first_word_ms"]["count"] >= 1
    assert german["translate_ms"]["count"] >= 1
    assert snapshot["problems"] == {}


async def test_billable_recognition_time_stops_when_the_room_empties() -> None:
    """The figure that shows paid work really does pause with the last viewer."""

    pipeline, _ = make_gated(translators={"de": Factory()})
    hub = RecordingHub(("de",))

    async with running(pipeline, hub):
        reader = hub.subscribe(viewer("de"))
        await wait_until(lambda: pipeline.snapshot()["recognition"]["open"])
        await asyncio.sleep(0.3)
        hub.unsubscribe(reader)
        await wait_until(lambda: not pipeline.snapshot()["recognition"]["open"])
        spent = pipeline.snapshot()["recognition"]["open_s"]
        await asyncio.sleep(0.4)

        assert spent >= 0.3
        assert pipeline.snapshot()["recognition"]["open_s"] == spent


async def test_a_failing_translator_is_counted_and_shown_for_its_language() -> None:
    pipeline, _ = make_gated(translators={"de": Factory(lambda: FlakyMt(failures=10_000))})
    hub = RecordingHub(("de",))

    async with running(pipeline, hub, timeout=8.0):
        reader = hub.subscribe(viewer("de"))
        await wait_until(lambda: pipeline.snapshot()["languages"]["de"]["failed"] >= 2, timeout=8.0)
        snapshot = pipeline.snapshot()
        hub.unsubscribe(reader)

    assert snapshot["languages"]["de"]["completed"] == 0
    assert snapshot["problems"].get("de"), "the operator is told which language is down"


def test_a_pipeline_that_has_not_run_describes_itself_without_error() -> None:
    pipeline, _ = make_gated(translators={"de": Factory()})

    snapshot = pipeline.snapshot()

    assert snapshot["audio"] is None
    assert snapshot["recognition"]["open"] is False
    assert snapshot["languages"]["de"]["readers"] == 0


@pytest.mark.parametrize("kind", ["sim"])
def test_a_simulation_says_that_it_is_one(kind: str) -> None:
    from livesubs.sim import SimPublisher

    assert SimPublisher().snapshot()["kind"] == "simulation"


# --- the open-file limit ----------------------------------------------------------------


class _Limits:
    """A stand-in for ``resource`` that remembers what was asked of it."""

    RLIMIT_NOFILE = 7
    RLIM_INFINITY = -1

    def __init__(self, soft: int, hard: int) -> None:
        self.soft, self.hard = soft, hard
        self.set_to: tuple[int, int] | None = None

    def getrlimit(self, which: int) -> tuple[int, int]:
        return self.soft, self.hard

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        self.set_to = limits
        self.soft = limits[0]


@pytest.mark.parametrize(
    ("soft", "hard", "result", "set_to"),
    [
        (1024, 524_288, 16_384, (16_384, 524_288)),
        (1024, 4096, 4096, (4096, 4096)),
        (1024, -1, 16_384, (16_384, -1)),
        (65_536, 524_288, 65_536, None),
        (4096, 4096, 4096, None),
    ],
)
def test_the_open_file_limit_is_raised_as_far_as_the_hard_limit_allows(
    monkeypatch: pytest.MonkeyPatch, soft: int, hard: int, result: int, set_to
) -> None:
    from livesubs import metrics

    fake = _Limits(soft, hard)
    monkeypatch.setattr(metrics, "resource", fake)

    assert metrics.raise_open_file_limit() == result
    assert fake.set_to == set_to


def test_an_unreadable_limit_is_reported_as_unknown_not_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from livesubs import metrics

    class Broken(_Limits):
        def getrlimit(self, which: int) -> tuple[int, int]:
            raise OSError("no")

    monkeypatch.setattr(metrics, "resource", Broken(1024, 1024))

    assert metrics.raise_open_file_limit() is None
