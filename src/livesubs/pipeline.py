"""The live pipeline: microphone to screen.

This module owns the only interesting concurrency in the program, so its rules are
written down here rather than left to be rediscovered.

**1. Audio frames are the clock.** Frame arrival drives the segment cap and the
silence detector. There is no separate timer task, so nothing can drift against
the audio. It also means the pipeline behaves identically whether frames arrive
from a device in real time or from a file replayed faster, which is the only
reason the audio path is testable at all.

**2. One publisher task, one ordering.** Segment publishing happens *only* inside
:meth:`LivePipeline._publish_loop`. Capture never publishes, so ``seq`` cannot
invert and a ``final`` can never be followed by a ``draft``. Capture hands work
over through a single queue and returns immediately, so a slow provider applies
backpressure to nothing.

**3. Nothing English ever reaches ``de``.** Only translated text is ever published
as subtitles. The obvious-looking shortcut of echoing the ASR partial into ``de``
so something appears sooner puts English on a German-only overlay, so it is not
done: the English source rides in ``src``, which the hub strips for viewers.

That has a latency consequence worth stating plainly: the screen shows nothing new
until the first German word of the next segment arrives, roughly one endpoint
delay after the speaker pauses. That is the correct trade. A placeholder would be
worse than briefly holding the previous line, and translating every partial would
multiply the translation bill by about eight for no reading-time gain, because
reading speed is the binding constraint anyway.

**4. Lag is made visible, not absorbed.** If translations fall behind, the queue
crosses a threshold and the pipeline reports ``DELAYED``. Queueing without bound
would look healthy while the subtitles drifted further and further behind the
speaker, which is the one failure a viewer cannot forgive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from livesubs.asr.base import AsrConfig, AsrEvent, AsrProvider
from livesubs.audio.base import AudioSource
from livesubs.audio.calibration import CalibrationNotice
from livesubs.events import (
    PipelineStatus,
    SegmentState,
    Stage,
    SubtitleSegment,
    Timing,
)
from livesubs.hub import Hub
from livesubs.ingest import FrameStats, Ingest, IngestConfig
from livesubs.mt.base import MtContext, MtProvider
from livesubs.segmenter import Decision, SegmentBuffer, SegmenterConfig

__all__ = ["Committed", "LivePipeline", "PipelineConfig"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Committed:
    """A finalised segment, with the audio span it came from.

    Both texts and both timings, because that is everything needed to write a
    transcript or a subtitle file, and the audio clock is the only clock that lines
    up with the archived recording.

    ``start_ms`` is where speech resumed and ``end_ms`` is the last voiced frame, not
    the moment the segment closed. The segmenter closes a segment a few hundred
    milliseconds after speech stops, and a cue running to the close would hold the
    previous line on screen through the pause - exactly the gap a viewer uses to look
    up from the screen.
    """

    seq: int
    start_ms: int
    end_ms: int
    src: str
    de: str


@dataclass(slots=True)
class PipelineConfig:
    """Everything tunable about the live path."""

    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    #: How often levels are reported to the operator, in audio time.
    telemetry_interval_ms: int = 1000
    #: Queued finals tolerated before the pipeline admits it is behind.
    lag_queue_limit: int = 4


class LivePipeline:
    """Capture to hub. Implements :class:`~livesubs.hub.SegmentPublisher`."""

    def __init__(
        self,
        *,
        source: AudioSource,
        asr: AsrProvider,
        mt: MtProvider,
        config: PipelineConfig | None = None,
        on_stats: Callable[[FrameStats], None] | None = None,
        on_segment: Callable[[Committed], None] | None = None,
    ) -> None:
        self.source = source
        self.asr = asr
        self.mt = mt
        self.config = config or PipelineConfig()
        #: Called at most once per ``telemetry_interval_ms`` with the latest frame
        #: statistics. Deliberately a callback rather than a wire message: the
        #: operator console is phase P5, and the pipeline should not have to
        #: change when it arrives.
        self.on_stats = on_stats
        #: Called once per committed segment, with both texts and the audio span.
        #: This is what writes a transcript or a subtitle file. A failure in it must
        #: not be able to stop a lecture, exactly as for ``on_stats``.
        self.on_segment = on_segment

        self.ingest = Ingest(self.config.ingest)

        #: Filled by the event collector, drained by capture. A deque, not a
        #: queue: draining must never await.
        self._inbox: deque[AsrEvent] = deque()
        #: The only queue into the publisher. Popping from it is the only thing that
        #: advances ``seq``, which is what makes ordering monotonic by construction.
        #: Each entry is ``(seq, source text, audio start ms, audio end ms)``.
        self._finals: deque[tuple[int, str, int, int]] = deque()
        #: Semaphore for the publisher, so it blocks instead of polling.
        self._signal: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._shutdown = asyncio.Event()

        self._hub: Hub | None = None
        self._stop: asyncio.Event | None = None
        self._t0 = 0.0
        self._audio_ms = 0
        self._src = ""
        self._speech_final = False
        self._seq = 0
        self._published_seq = -1
        self._rev = 0
        self._lagging = False
        self._clipping_reported = False
        self._calibrating = False
        self._capture_detail: str | None = None
        self._recent_src: list[str] = []
        self._recent_de: list[str] = []

    async def run(self, hub: Hub, *, session_id: str, stop: asyncio.Event) -> None:
        """Publish until the source ends or ``stop`` is set, then drain and return."""

        self._hub = hub
        self._stop = stop
        self._t0 = time.monotonic()

        reporter = getattr(self.source, "set_calibration_reporter", None)
        if reporter is not None:
            reporter(self._capture_calibration)
        prepare = getattr(self.source, "prepare", None)
        if stop.is_set():
            await self.source.close()
            return
        if prepare is not None:
            preparation = asyncio.create_task(prepare())
            stopped = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait((preparation, stopped), return_when=asyncio.FIRST_COMPLETED)
                if stop.is_set():
                    await self.source.close()
                    preparation.cancel()
                    await asyncio.gather(preparation, return_exceptions=True)
                    return
                await preparation
            except BaseException:
                await self.source.close()
                preparation.cancel()
                await asyncio.gather(preparation, return_exceptions=True)
                raise
            finally:
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)

        if stop.is_set():
            await self.source.close()
            return

        # Warm before the first segment, never during it: cache construction takes
        # seconds and doing it in front of an audience is the difference between a
        # working demo and an apology.
        try:
            await self.mt.warm()
            if stop.is_set():
                await self.mt.close()
                await self.source.close()
                return
            await self.asr.open(self.config.asr)
        except BaseException:
            await self.source.close()
            await self.asr.close()
            await self.mt.close()
            raise

        collector = asyncio.create_task(self._collect_events(), name=f"livesubs:asr:{session_id}")
        publisher = asyncio.create_task(self._publish_loop(), name=f"livesubs:pub:{session_id}")

        buffer = SegmentBuffer(self.config.segmenter, started_ms=0, last_voice_ms=0)
        self._seq = 0
        last_telemetry_ms = 0
        #: True while no speech run is open: from the session start, and again during
        #: the pause that ended each segment. Starts True so that leading silence does
        #: not become part of the first cue.
        awaiting_resume = True

        try:
            async for frame in self.source.frames():
                if stop.is_set():
                    break

                stats = self.ingest.process(frame.pcm)
                self._audio_ms = stats.now_ms

                # Push before deciding anything: the provider's own endpointing is
                # worth more the sooner it sees the audio, and `push` is required
                # not to block for longer than a frame.
                await self.asr.push(frame.pcm)

                # Hand the event loop one turn, so the collector can move whatever
                # the provider just produced into the inbox. Without this the capture
                # loop never yields between pushing audio and reading events, so the
                # inbox is always a frame stale - and on the frame where a segment
                # closes, "a frame stale" means committing that segment with the
                # previous partial's text instead of the provider's final one.
                await asyncio.sleep(0)
                self._drain_events()

                if awaiting_resume:
                    # No speech run is open: either the session has just started, or a
                    # segment has closed and its trailing pause is still running.
                    # Either way the next run must begin when speech begins, not at the
                    # previous EMIT and not at the session start. Otherwise the 2 s
                    # budget would be measured from the middle of a pause - so the cap
                    # would fire early and split an utterance - and a cue would appear
                    # before the speaker said anything.
                    #
                    # The run starts at `frame.start_ms`, not `now_ms`: now_ms is the
                    # END of the frame that first carried speech, so using it would put
                    # every cue one frame (20 ms) late. Small, and exactly the kind of
                    # offset that makes a video overlay look subtly out of sync.
                    buffer.reset(frame.start_ms if stats.speech else stats.now_ms)
                    awaiting_resume = not stats.speech

                decision = buffer.observe(
                    stats.now_ms,
                    voice=stats.speech,
                    speech_final=self._speech_final,
                )
                self._speech_final = False

                if decision is Decision.EMIT:
                    if self._src:
                        # The span is taken from the buffer rather than from the
                        # current frame: speech began at `started_ms` and stopped at
                        # `last_voice_ms`, and the gap since then is the pause that
                        # closed the segment. A cue covering the pause too would sit
                        # on screen through the one moment a viewer looks up.
                        self._finals.append(
                            (
                                self._seq,
                                self._src,
                                buffer.started_ms,
                                max(buffer.last_voice_ms, buffer.started_ms),
                            )
                        )
                        self._seq += 1
                        self._wake()
                    # The clock resets even for an empty run, so the next segment
                    # starts when speech resumes rather than when this one ended.
                    self._src = ""
                    buffer.reset(stats.now_ms)
                    awaiting_resume = True
                    self._check_lag()

                if stats.now_ms - last_telemetry_ms >= self.config.telemetry_interval_ms:
                    last_telemetry_ms = stats.now_ms
                    self._report(stats)
        finally:
            await self._finish(publisher, collector, buffer)

    async def _collect_events(self) -> None:
        """Move provider events into the inbox. The only awaiting reader."""

        try:
            async for event in self.asr.events():
                self._inbox.append(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ASR event stream failed")
            if self._hub is not None:
                self._hub.publish_error("provider event stream failed", Stage.ASR)

    async def _finish(
        self,
        publisher: asyncio.Task[None],
        collector: asyncio.Task[None],
        buffer: SegmentBuffer,
    ) -> None:
        """Shut down in the one order that does not lose the last subtitle.

        Closing the provider comes **first**, and that is the whole point of the
        order. A provider flushes the utterance still open when its stream closes,
        and that flush is the last line of the lecture. Waiting for the publisher
        before closing the provider would leave the final draft of the session
        never committed, so the overlay would hold a line it can never finalise.

        Every stage is bounded, and the bounds add up to less than the session's own
        3 s shutdown budget, so this always finishes before the session loses
        patience and cancels ``run`` outright.
        """

        # 1. Close the provider: its close ends the event stream, after flushing any
        #    utterance that was still open.
        try:
            await self.asr.close()
        except Exception:
            logger.exception("ASR provider failed to close")

        # 2. Wait for the collector to move that flush into the inbox, rather than
        #    hoping a single event-loop turn is enough.
        try:
            await asyncio.wait_for(collector, timeout=_FLUSH_GRACE_S)
        except TimeoutError:
            await _cancel(collector)
        except Exception:
            logger.exception("event collector failed during shutdown")

        self._drain_events()

        # 3. Commit the utterance that was still open, so nothing is left as a
        #    draft forever. ``_src`` still being set means no EMIT happened for this
        #    sequence, so this cannot duplicate a final.
        if self._src:
            self._finals.append(
                (
                    self._seq,
                    self._src,
                    buffer.started_ms,
                    max(buffer.last_voice_ms, buffer.started_ms),
                )
            )

        # 4. Now let the publisher drain everything, that last segment included.
        self._shutdown.set()
        self._wake()

        try:
            await asyncio.wait_for(publisher, timeout=_SHUTDOWN_GRACE_S)
        except TimeoutError:
            logger.warning("publisher did not drain within %.1fs; cancelling", _SHUTDOWN_GRACE_S)
            await _cancel(publisher)
        except Exception:
            logger.exception("publisher failed during shutdown")

        await _cancel(collector)

        try:
            await self.source.close()
        except Exception:
            logger.exception("audio source failed to close")

    async def _publish_loop(self) -> None:
        """The only publisher, and the only place ``seq`` is advanced.

        One queue, drained in order, so ordering needs no argument: the translation
        of sequence *n* is always finished before sequence *n + 1* is looked at.
        """

        while True:
            if self._finals:
                seq, src, start_ms, end_ms = self._finals.popleft()
                await self._translate_and_publish(seq, src, start_ms, end_ms)
                self._check_lag()
                continue

            if self._shutdown.is_set():
                return

            await self._signal.get()

    async def _translate_and_publish(
        self, seq: int, src: str, start_ms: int, end_ms: int
    ) -> None:
        """Translate one segment, publishing revisions as German arrives.

        Streaming the translation is the point: the first German words reach the
        screen long before the sentence finishes, which is one of the three things
        that make the pipeline feel instant.
        """

        context = MtContext(
            source_context=list(self._recent_src),
            target_context=list(self._recent_de),
        )

        chunks: list[str] = []
        translated = ""

        try:
            async for chunk in self.mt.translate(src, context):
                if chunk.text_delta:
                    chunks.append(chunk.text_delta)
                    translated = "".join(chunks)
                    self._publish(
                        seq, SegmentState.DRAFT, translated, src=src, audio_ms=end_ms
                    )
                if chunk.done:
                    break
        except Exception:
            logger.exception("translation of segment %d failed", seq)
            if self._hub is not None:
                self._hub.publish_error("translation failed", Stage.MT)
            return

        if not translated.strip():
            # Falling back to the English source would put English on a
            # German-only overlay, which is worse than showing nothing at all.
            logger.warning("segment %d produced no German; dropping it", seq)
            if self._hub is not None:
                self._hub.publish_error("empty translation", Stage.MT)
            return

        self._publish(seq, SegmentState.FINAL, translated, src=src, audio_ms=end_ms)
        self._remember(src, translated)

        self._emit_committed(seq, start_ms, end_ms, src, translated)

    def _emit_committed(self, seq: int, start_ms: int, end_ms: int, src: str, de: str) -> None:
        """Hand the finished segment to the sink, if there is one.

        Wrapped for the same reason telemetry is: writing a transcript is an extra,
        and an extra must never be able to stop the lecture it is describing.
        """

        if self.on_segment is None:
            return

        try:
            self.on_segment(
                Committed(seq=seq, start_ms=start_ms, end_ms=end_ms, src=src, de=de)
            )
        except Exception:
            logger.exception("the segment sink failed; the lecture continues")

    def _publish(
        self,
        seq: int,
        state: SegmentState,
        de: str,
        *,
        src: str | None = None,
        audio_ms: int | None = None,
    ) -> None:
        hub = self._hub
        if hub is None:
            return

        # ``rev`` starts at 0 for a new seq and only ever increases, which is the
        # contract the overlay's in-place update depends on.
        if seq != self._published_seq:
            self._published_seq = seq
            self._rev = 0
        else:
            self._rev += 1

        hub.publish_segment(
            SubtitleSegment(
                seq=seq,
                rev=self._rev,
                state=state,
                de=de,
                src=src,
                timing=Timing(
                    audio_ms=self._audio_ms if audio_ms is None else audio_ms,
                    emit_ms=self._now_ms(),
                ),
            )
        )

    def _remember(self, src: str, de: str) -> None:
        """Keep the last few segments as translation context.

        German is verb-final, so an isolated English fragment translates badly.
        Context goes in the user message, never the system prompt - see
        ``docs/05-providers.md`` on cache prefixes.
        """

        limit = max(0, self.config.segmenter.context_segments)
        if not limit:
            return

        self._recent_src.append(src)
        self._recent_de.append(de)
        del self._recent_src[:-limit]
        del self._recent_de[:-limit]

    def _drain_events(self) -> None:
        """Fold queued provider events into the open utterance, without awaiting.

        Later events supersede earlier ones for the same span, so only the last
        text survives. A provider ``final`` is recorded as a hint for the
        segmenter, never as a command.
        """

        inbox = self._inbox
        while inbox:
            event = inbox.popleft()
            if event.text:
                self._src = event.text
            if event.kind == "final":
                self._speech_final = True

    def _wake(self) -> None:
        """Signal the publisher. Idempotent, because the signal is a token not a count."""

        if self._signal.empty():
            self._signal.put_nowait(None)

    def _check_lag(self) -> None:
        """Surface backlog as a status transition, rather than hiding it in a queue.

        An unbounded queue looks perfectly healthy while the subtitles drift
        further behind the speaker, which is the one failure a viewer cannot
        forgive: the words are correct and useless.
        """

        hub = self._hub
        if hub is None:
            return

        behind = len(self._finals) >= self.config.lag_queue_limit

        if behind and not self._lagging:
            self._lagging = True
            logger.warning("translation is %d segments behind", len(self._finals))
            hub.publish_status(PipelineStatus.DELAYED, detail="translation is behind")
        elif not behind and self._lagging:
            self._lagging = False
            hub.publish_status(
                PipelineStatus.DELAYED if self._calibrating else PipelineStatus.LIVE,
                detail=self._capture_detail,
            )

    def _capture_calibration(self, notice: CalibrationNotice) -> None:
        """Expose capture adjustments separately from translation backlog."""
        hub = self._hub
        if hub is None or (self._stop is not None and self._stop.is_set()):
            return
        self._capture_detail = f"microphone: {notice.message}"
        self._calibrating = notice.state in {"calibrating", "adjusting", "failed"}
        if notice.state == "failed":
            hub.publish_error(notice.message, Stage.CAPTURE)
        hub.publish_status(
            PipelineStatus.DELAYED if self._calibrating or self._lagging else PipelineStatus.LIVE,
            detail=self._capture_detail,
        )

    def _report(self, stats: FrameStats) -> None:
        """Hand the operator a periodic view of the signal.

        The capture backend performs bounded, verified hardware reductions. Here
        levels are observed, never used to scale already-clipped PCM digitally.
        """

        if self.on_stats is not None:
            try:
                self.on_stats(stats)
            except Exception:
                logger.exception("on_stats callback failed")

        if stats.clipping and not self._clipping_reported:
            self._clipping_reported = True
            logger.warning(
                "input is clipping (%.1f%% of one frame); lower the capture level",
                stats.clipping_fraction * 100.0,
            )
        elif not stats.clipping:
            self._clipping_reported = False

    def _now_ms(self) -> int:
        """Wall-clock milliseconds since this pipeline started.

        Distinct from the audio clock on purpose: the gap between the two is the
        pipeline's own latency, which is exactly what ``Timing`` is for.
        """

        return int((time.monotonic() - self._t0) * 1000)


#: How long to wait for the provider's closing flush to reach the inbox.
_FLUSH_GRACE_S = 0.5

#: How long the publisher is given to finish translations already queued.
#: Together with ``_FLUSH_GRACE_S`` this must stay below the session's own 3 s
#: shutdown budget, so that draining completes before the session loses patience
#: and cancels ``run`` outright.
_SHUTDOWN_GRACE_S = 1.5


async def _cancel(task: asyncio.Task[None]) -> None:
    """Cancel a task and swallow the cancellation, but not a real failure."""

    if task.done():
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("task %s errored while being cancelled", task.get_name())
