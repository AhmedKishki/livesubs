"""What the operator can be told about a running session.

Three things are measured here, none of them on the path a subtitle takes: how the
audience is being served (:class:`DeliveryMetrics`, owned by the hub), how the host is doing
(:class:`LoopMonitor` and :func:`process_stats`), and the rolling window that both use to
report a latency as something better than a single number.

Everything is in memory and bounded, so a day-long session costs no more than a minute-long
one. Nothing here reads lecture text, and nothing is written anywhere: the operator console
asks for a snapshot and the snapshot is a plain dictionary.
"""

from __future__ import annotations

import asyncio
import math
import os
import resource
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from livesubs.hub import ClientMailbox, Hub

__all__ = [
    "DeliveryMetrics",
    "LoopMonitor",
    "Window",
    "process_stats",
    "raise_open_file_limit",
]

#: How many recent samples a window keeps. Large enough for a stable 95th percentile,
#: small enough that the figure follows what is happening now rather than the whole lecture.
WINDOW_SIZE = 256


class Window:
    """The most recent samples of one measurement, summarised on demand.

    ``count`` is every sample ever added, so the console can show how many requests a
    figure is based on, while the percentiles describe only the recent window.
    """

    __slots__ = ("_values", "count")

    def __init__(self, size: int = WINDOW_SIZE) -> None:
        self._values: deque[float] = deque(maxlen=size)
        self.count = 0

    def add(self, value: float) -> None:
        self._values.append(float(value))
        self.count += 1

    def summary(self) -> dict[str, float | int | None]:
        """``count``, the last sample, and the median, 95th percentile and maximum.

        Percentiles are nearest-rank over the window, which is exact enough for a
        console and needs no interpolation. Every figure but ``count`` is ``None`` before
        the first sample, so a page can say "no data" instead of showing a zero that looks
        like a measurement.
        """

        if not self._values:
            return {"count": self.count, "last": None, "p50": None, "p95": None, "max": None}
        ordered = sorted(self._values)
        return {
            "count": self.count,
            "last": round(self._values[-1], 1),
            "p50": round(_rank(ordered, 0.50), 1),
            "p95": round(_rank(ordered, 0.95), 1),
            "max": round(ordered[-1], 1),
        }


def _rank(ordered: list[float], fraction: float) -> float:
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


class DeliveryMetrics:
    """How the audience is being served: who is connected, what they were sent, how they fare.

    The hub owns one and the socket handlers feed it. Counters that belong to a connection
    are folded into totals when it closes, so a phone that came and went still counts.
    """

    def __init__(self, targets: tuple[str, ...] = ()) -> None:
        self.started = time.monotonic()
        self.connects = 0
        self.disconnects = 0
        self.peak_viewers = 0
        self.peak_by_language: dict[str, int] = {}
        self.messages_sent = 0
        self.bytes_sent = 0
        #: Wall time to write one client's batch to its socket. The slow tail here is a
        #: phone on a weak signal, which the server can only notice, not fix.
        self.send_ms = Window()
        #: What viewers report: how long a line took from arriving to being on screen.
        self.render_ms: dict[str, Window] = {code: Window() for code in targets}
        #: Cumulative lines each connected viewer says it dropped (the latest report).
        self._dropped_live: dict[str, int] = {}
        self._dropped_closed = 0
        #: Lines and drafts a viewer was never sent because a newer one replaced them.
        self._skipped_lines_closed = 0
        self._skipped_drafts_closed = 0

    # --- fed by the hub and the socket handlers ------------------------------------

    def note_connect(self, viewers: int, by_language: dict[str, int]) -> None:
        self.connects += 1
        self.peak_viewers = max(self.peak_viewers, viewers)
        for code, count in by_language.items():
            self.peak_by_language[code] = max(self.peak_by_language.get(code, 0), count)

    def note_disconnect(self, connection: str, mailbox: ClientMailbox) -> None:
        self.disconnects += 1
        self._dropped_closed += self._dropped_live.pop(connection, 0)
        self._skipped_lines_closed += mailbox.skipped_lines
        self._skipped_drafts_closed += mailbox.skipped_drafts

    def note_sent(self, messages: int, size: int, elapsed_ms: float) -> None:
        self.messages_sent += messages
        self.bytes_sent += size
        self.send_ms.add(elapsed_ms)

    def note_ack(
        self, connection: str, language: str | None, rendered_ms: int | None, dropped: int | None
    ) -> None:
        # A viewer is untrusted input to a figure the operator relies on, so it can only
        # report values a clock could produce.
        if rendered_ms is not None and language is not None:
            self.render_ms.setdefault(language, Window()).add(max(0, rendered_ms))
        if dropped is not None:
            self._dropped_live[connection] = max(0, dropped)

    # --- reading --------------------------------------------------------------------

    def snapshot(self, hub: Hub) -> dict[str, Any]:
        live = list(hub.mailboxes())
        return {
            "viewers": hub.viewer_count,
            "viewers_by_language": hub.viewers_by_language,
            "operators": hub.subscriber_count - hub.viewer_count,
            "connections": hub.subscriber_count,
            "peak_viewers": self.peak_viewers,
            "peak_by_language": dict(self.peak_by_language),
            "connects": self.connects,
            "disconnects": self.disconnects,
            "messages_sent": self.messages_sent,
            "bytes_sent": self.bytes_sent,
            "send_ms": self.send_ms.summary(),
            "render_ms": {code: window.summary() for code, window in self.render_ms.items()},
            "dropped_lines": self._dropped_closed + sum(self._dropped_live.values()),
            "skipped_lines": self._skipped_lines_closed + sum(m.skipped_lines for m in live),
            "skipped_drafts": self._skipped_drafts_closed + sum(m.skipped_drafts for m in live),
        }


class LoopMonitor:
    """How late the event loop runs, and how much processor the process uses.

    The loop is what delivers every subtitle to every phone, so its lateness is the one
    figure that says whether the server is keeping up: a loop that wakes a few milliseconds
    late is idle, one that wakes hundreds late is saturated whatever the audience size.
    Measured by sleeping a fixed interval and seeing how much longer it took.
    """

    INTERVAL_S = 0.25

    def __init__(self) -> None:
        self.lag_ms = Window()
        self.cpu_percent = 0.0
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="livesubs:loop-monitor")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        cpu_at, wall_at = time.process_time(), loop.time()
        while True:
            before = loop.time()
            await asyncio.sleep(self.INTERVAL_S)
            now = loop.time()
            self.lag_ms.add(max(0.0, (now - before - self.INTERVAL_S) * 1000.0))
            # Over about a second, so a single busy tick does not make the figure jump.
            if now - wall_at >= 1.0:
                cpu_now = time.process_time()
                self.cpu_percent = 100.0 * (cpu_now - cpu_at) / (now - wall_at)
                cpu_at, wall_at = cpu_now, now

    def snapshot(self) -> dict[str, Any]:
        return {
            "loop_lag_ms": self.lag_ms.summary(),
            "cpu_percent": round(self.cpu_percent, 1),
            **process_stats(),
        }


def process_stats() -> dict[str, Any]:
    """Memory, open files and threads of this process, and the limit that matters.

    The open-file limit is reported because every connected phone is a file descriptor, and
    a default limit of 1024 is reached long before the machine is busy.
    """

    stats: dict[str, Any] = {
        "cpu_seconds": round(time.process_time(), 1),
        "threads": threading.active_count(),
    }
    try:
        with open("/proc/self/statm", encoding="ascii") as statm:
            pages = int(statm.read().split()[1])
        stats["rss_mb"] = round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 1)
    except (OSError, ValueError, IndexError):
        stats["rss_mb"] = None
    try:
        stats["open_files"] = len(os.listdir("/proc/self/fd"))
    except OSError:
        stats["open_files"] = None
    try:
        stats["open_files_limit"] = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (OSError, ValueError):
        stats["open_files_limit"] = None
    return stats


#: Open files a server asks for. Every connected phone is one, so the usual default of 1024
#: refuses phone number 1000 however idle the machine is.
WANTED_OPEN_FILES = 16_384


def raise_open_file_limit(wanted: int = WANTED_OPEN_FILES) -> int | None:
    """Raise the soft open-file limit towards ``wanted``, as far as the hard limit allows.

    Returns the soft limit in force afterwards, or ``None`` when it cannot be read or set.
    A limit already high enough is left alone, and the hard limit is never touched, so this
    asks for nothing an unprivileged process is not already allowed.
    """

    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft >= wanted:
            return soft
        target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
        if target > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        return max(soft, target)
    except (OSError, ValueError):
        return None
