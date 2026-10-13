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
from livesubs.transcript import TranscriptAssembler

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

    ``language`` says which target stream produced this record. The German stream
    fills ``de``; the Arabic stream fills ``ar`` and leaves ``de`` empty. ``src`` is
    the English source either way, which is what a subtitle recorder needs to pair a
    translation with what was actually said.
    """

    seq: int
    start_ms: int
    end_ms: int
    src: str
    de: str = ""
    ar: str | None = None
    language: str = "de"


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


@dataclass(slots=True)
class _Target:
    """Per-language translation state.

    One of these per target language keeps the shared commit path and the two
    independent translators from growing a second copy of this module. The German
    target borrows the pipeline's own deques and signal, so its behaviour is
    unchanged; the Arabic target owns its own, and is only ever active while an
    Arabic viewer is present.
    """

    language: str
    mt: MtProvider | None
    mt_factory: Callable[[], MtProvider] | None
    queue: deque[tuple[int, str, int, int]]
    signal: asyncio.Queue[None]
    #: ``rev`` starts at 0 for a new ``seq`` and only increases, per language.
    rev: int = 0
    published_seq: int = -1
    recent_src: list[str] = field(default_factory=list)
    recent_target: list[str] = field(default_factory=list)
    #: Whether this language's translator has been warmed for the current run.
    warmed: bool = False
    #: The per-language demand generation the active worker belongs to.
    epoch: int | None = None
    task: asyncio.Task[None] | None = None
    #: Whether the worker is running and this language should receive segments.
    active: bool = False
    lagging: bool = False


class LivePipeline:
    """Capture to hub. Implements :class:`~livesubs.hub.SegmentPublisher`."""

    #: The target languages this pipeline can produce. A publisher without an Arabic
    #: translator declares German only, and the server then refuses the Arabic role
    #: rather than letting it create demand for text that will never arrive.
    @property
    def supported_languages(self) -> tuple[str, ...]:
        return ("de", "ar") if self._ar_enabled else ("de",)

    def __init__(
        self,
        *,
        source: AudioSource,
        asr: AsrProvider | None = None,
        mt: MtProvider,
        config: PipelineConfig | None = None,
        on_stats: Callable[[FrameStats], None] | None = None,
        on_segment: Callable[[Committed], None] | None = None,
        asr_factory: Callable[[], AsrProvider] | None = None,
        require_audience: bool = False,
        mt_ar: MtProvider | None = None,
        mt_ar_factory: Callable[[], MtProvider] | None = None,
    ) -> None:
        if asr is None and asr_factory is None:
            raise ValueError("LivePipeline needs an `asr` provider or an `asr_factory`")

        self.source = source
        #: The recogniser consumed by the first open attempt. With ``asr_factory`` it is
        #: used exactly once, before any provider has been opened, so a failed or closed
        #: adapter is never reused and its queue can never be replayed.
        self.asr = asr
        self._asr_factory = asr_factory
        #: When set, the paid providers open only while at least one viewer is
        #: connected. Capture keeps running; the frames are dropped, not buffered.
        self._require_audience = require_audience
        self.mt = mt
        #: The Arabic translator, and the factory that rebuilds one. Both are optional:
        #: without them the pipeline is German-only. The factory is only called while an
        #: Arabic viewer is present, so a German-only lecture never constructs or warms
        #: an Arabic provider.
        self._mt_ar = mt_ar
        self._mt_ar_factory = mt_ar_factory
        self._ar_enabled = mt_ar is not None or mt_ar_factory is not None
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
        #: The only queue into the German publisher. Popping from it is the only thing
        #: that advances German ``seq``, which is what makes ordering monotonic by
        #: construction. Each entry is ``(seq, source text, audio start ms, audio end
        #: ms)``.
        self._finals: deque[tuple[int, str, int, int]] = deque()
        #: Semaphore for the German publisher, so it blocks instead of polling.
        self._signal: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._shutdown = asyncio.Event()

        self._hub: Hub | None = None
        self._stop: asyncio.Event | None = None
        self._t0 = 0.0
        self._audio_ms = 0
        #: The recogniser's text that has not been sent to translation yet.
        self._assembler = TranscriptAssembler()
        self._speech_final = False
        self._seq = 0
        self._lagging = False
        self._clipping_reported = False
        self._calibrating = False
        self._capture_detail: str | None = None

        #: Rolling translation context for German. Kept as attributes because callers
        #: and tests know them; the German target shares the same lists.
        self._recent_src: list[str] = []
        self._recent_de: list[str] = []

        #: Per-language translation state. The German target borrows the deques and
        #: signal created above, so its behaviour and ordering are unchanged.
        self._de = _Target(
            language="de",
            mt=mt,
            mt_factory=None,
            queue=self._finals,
            signal=self._signal,
        )
        self._de.recent_src = self._recent_src
        self._de.recent_target = self._recent_de
        self._ar = _Target(
            language="ar",
            mt=mt_ar,
            mt_factory=mt_ar_factory,
            queue=deque(),
            signal=asyncio.Queue(maxsize=1),
        )

        # --- audience-gated provider lifecycle ---
        #: The recogniser whose stream is currently open, if any.
        self._asr_active: AsrProvider | None = None
        #: The initial ``asr`` has been handed out, so later attempts must rebuild.
        self._initial_asr_consumed = False
        self._collector: asyncio.Task[None] | None = None
        self._publisher: asyncio.Task[None] | None = None
        #: Whether the shared recogniser is currently open for the present audience.
        self._audience_open = False
        #: The demand generation the open providers belong to, so a fast 0->1
        #: reconnect that the watcher never saw as zero still invalidates them.
        self._audience_epoch: int | None = None
        self._mt_warmed = False
        #: The demand watcher, the Arabic watcher, and in-flight opens, cancelled on
        #: shutdown.
        self._watcher: asyncio.Task[None] | None = None
        self._arabic_watcher: asyncio.Task[None] | None = None
        self._opening: asyncio.Task[None] | None = None
        self._german_opening: asyncio.Task[None] | None = None
        self._arabic_opening: asyncio.Task[None] | None = None
        #: A provider ended on its own, so the watcher should tear down and retry.
        self._provider_failed = False
        #: Wakes the watcher for internal events, not just audience changes.
        self._wake_event = asyncio.Event()
        #: Set once ``run`` is finishing, so background tasks stop cleanly.
        self._closing = False
        #: Set while the final Arabic segment is being drained at shutdown.
        self._ar_draining = False
        self._session_id = "session"

    async def run(self, hub: Hub, *, session_id: str, stop: asyncio.Event) -> None:
        """Publish until the source ends or ``stop`` is set, then drain and return.

        Capture runs continuously, but when ``require_audience`` is set the paid
        providers open only while a viewer is connected. That keeps the audio clock
        and the archive continuous across a pause, and means a lecture nobody is
        watching costs nothing in API calls. The frames captured while gated are
        dropped, never buffered, so no old audio is ever replayed into a recogniser
        when the audience returns.
        """

        self._hub = hub
        self._stop = stop
        self._session_id = session_id
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

        # Live runs use the demand watcher. Ungated tests and simulations open now.
        if not self._require_audience:
            try:
                await self._open_providers()
            except BaseException:
                await self.source.close()
                await self._teardown_paid()
                raise
        else:
            hub.enable_audience_gating()
            self._watcher = asyncio.create_task(
                self._watch_audience(), name=f"livesubs:watch:{session_id}"
            )
            # The Arabic watcher is separate on purpose: starting or stopping Arabic
            # work must never tear down the shared recogniser or the German stream.
            if self._ar_enabled:
                self._arabic_watcher = asyncio.create_task(
                    self._watch_arabic(), name=f"livesubs:watch:ar:{session_id}"
                )

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

                if self._capture_is_live():
                    asr = self._asr_active
                    assert asr is not None  # implied by the guard above

                    # Push before deciding anything: the provider's own endpointing is
                    # worth more the sooner it sees the audio, and `push` is required
                    # not to block for longer than a frame.
                    await asr.push(frame.pcm)

                    # Hand the event loop one turn, so the collector can move whatever
                    # the provider just produced into the inbox. Without this the capture
                    # loop never yields between pushing audio and reading events, so the
                    # inbox is always a frame stale - and on the frame where a segment
                    # closes, "a frame stale" means committing that segment with the
                    # previous partial's text instead of the provider's final one.
                    await asyncio.sleep(0)
                    activity = self._drain_events()

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
                        #
                        # Speech is the VAD hearing it OR the recogniser producing text. A
                        # voice below the VAD threshold is still recognised, and a run that
                        # only the VAD could open left that voice without subtitles while
                        # the recogniser billed on.
                        opens = stats.speech or self._assembler.pending
                        buffer.reset(frame.start_ms if stats.speech else stats.now_ms)
                        awaiting_resume = not opens

                    decision = buffer.observe(
                        stats.now_ms,
                        voice=stats.speech,
                        speech_final=self._speech_final,
                        activity=activity,
                    )
                    self._speech_final = False

                    if decision is Decision.EMIT:
                        # What the recogniser has said that has not been sent yet. Taken from
                        # the assembler, not from the newest event: that event describes the
                        # recogniser's chunk, not this segment.
                        text = self._assembler.commit()
                        if text.strip():
                            # The span is taken from the buffer rather than from the
                            # current frame: speech began at `started_ms` and stopped at
                            # `end_ms`, and the gap since then is the pause that closed the
                            # segment. A cue covering the pause too would sit on screen
                            # through the one moment a viewer looks up.
                            #
                            # One committed source segment is handed to every active
                            # target, each of which assigns and translates independently.
                            self._dispatch(
                                (
                                    self._seq,
                                    text,
                                    buffer.started_ms,
                                    max(buffer.end_ms, buffer.started_ms),
                                )
                            )
                            self._seq += 1
                        # The clock resets even for an empty run, so the next segment
                        # starts when speech resumes rather than when this one ended.
                        buffer.reset(stats.now_ms)
                        awaiting_resume = True
                        self._check_lag()
                else:
                    # No viewer: drop the audio instead of buffering it, and keep the
                    # segmenter closed, so the next line begins with the speech that
                    # follows the audience's return rather than with stale audio.
                    awaiting_resume = True
                    self._assembler.reset()
                    self._speech_final = False
                    self._inbox.clear()

                if stats.now_ms - last_telemetry_ms >= self.config.telemetry_interval_ms:
                    last_telemetry_ms = stats.now_ms
                    self._report(stats)
        finally:
            await self._finish(buffer)

    def _capture_is_live(self) -> bool:
        """Whether captured frames should reach the recogniser right now.

        Gated on the *shared* audience, not on German viewers specifically: an
        Arabic-only audience still needs the recogniser, because Arabic subtitles are
        translated from the same English source.
        """

        if not self._require_audience:
            return True
        return bool(
            self._audience_open
            and self._asr_active is not None
            and self._demand_is_current()
        )

    def _demand_is_current(self) -> bool:
        """Whether the shared recogniser still matches the current audience.

        The audience is any viewer, German or Arabic. Adding an Arabic viewer to a room
        that already has a German one leaves this true, so the recogniser is not torn
        down and rebuilt for the new language.
        """

        if not self._require_audience:
            return True
        hub = self._hub
        if hub is None:
            return False
        return hub.has_viewers and self._audience_epoch == hub.demand_generation

    def _german_demand_is_current(self) -> bool:
        """Whether German translation should be running: a German viewer, same epoch."""

        if not self._require_audience:
            return True
        hub = self._hub
        if hub is None:
            return False
        return (
            hub.viewer_count_for("de") > 0
            and self._de.epoch == hub.demand_generation_for("de")
        )

    def _arabic_demand_is_current(self) -> bool:
        """Whether Arabic translation should be running: an Arabic viewer, same epoch."""

        if not self._require_audience:
            return True
        hub = self._hub
        if hub is None:
            return False
        return (
            hub.viewer_count_for("ar") > 0
            and self._ar.epoch == hub.demand_generation_for("ar")
        )

    def _dispatch(self, committed: tuple[int, str, int, int]) -> None:
        """Hand one committed source segment to every target that has real demand.

        Both targets get the same English source and the same ``seq``; each keeps its
        own ``rev`` counter, so the two streams never share a revision timeline.

        A target's ``active`` flag alone is not enough: a worker can have retired
        without its flag being cleared yet, and a stale epoch means no viewer is
        actually waiting. Checking demand here stops a queue growing for a language
        nobody is watching.
        """

        for target in (self._de, self._ar):
            if not target.active:
                continue
            if not self._target_demand_is_current(target):
                continue
            target.queue.append(committed)
            self._wake_target(target)

    async def _watch_audience(self) -> None:
        """Open paid providers while viewers are present, close them when none are.

        A task of its own rather than a step in the capture loop, so audience changes
        take effect even when capture stalls or delivers frames sparsely. It waits on
        the hub's demand revision and cancels an in-flight open the moment the audience
        leaves, so no billed warm or ASR open runs on for an empty room. Consecutive
        open failures are retried with a bounded backoff while viewers remain; once the
        budget is spent it waits for a demand change rather than hammering the provider.
        """

        hub = self._hub
        assert hub is not None
        failures = 0

        try:
            while not self._closing:
                current = hub.demand_generation
                if not hub.has_viewers:
                    failures = 0
                    if self._audience_open or self._opening is not None:
                        await self._teardown_paid()
                    await self._wait_for_watch_event(current)
                    continue

                if self._provider_failed:
                    self._provider_failed = False
                    await self._teardown_paid()
                    failures += 1
                    if failures <= _OPEN_RETRY_LIMIT:
                        await self._sleep_until_demand_change(_backoff_seconds(failures))
                    else:
                        await self._wait_for_watch_event(current)
                        failures = 0
                    continue

                if self._audience_open and self._audience_epoch == current:
                    failures = 0
                    # The recogniser matches this occupancy. Bring German work up or
                    # down to match its own viewers - an Arabic-only room that gains a
                    # German viewer must start German without restarting the ASR.
                    await self._sync_german_target()
                    await self._wait_for_watch_event(current)
                    continue

                if self._audience_open:
                    # The occupancy changed without the watcher seeing the empty
                    # moment (a fast leave-and-return). The open providers belong to
                    # the old generation, so rebuild for the new one.
                    await self._teardown_paid()

                outcome = await self._attempt_open(current)
                if outcome == _OPENED or outcome == _DEMAND_LOST:
                    failures = 0
                else:                   # _FAILED
                    failures += 1
                    if failures <= _OPEN_RETRY_LIMIT:
                        await self._sleep_until_demand_change(_backoff_seconds(failures))
                    else:
                        await self._wait_for_watch_event(hub.demand_generation)
                        failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("audience watcher stopped unexpectedly")

    async def _attempt_open(self, epoch: int) -> str:
        """Run one open attempt, aborting it if the audience changes underneath it."""

        hub = self._hub
        assert hub is not None
        task = asyncio.create_task(
            self._open_sequence(epoch), name=f"livesubs:open:{self._session_id}"
        )
        self._opening = task
        try:
            while not task.done():
                if not hub.has_viewers:
                    break
                changed = asyncio.create_task(hub.wait_for_demand_change(epoch))
                wake = asyncio.create_task(self._wake_event.wait())
                try:
                    await asyncio.wait(
                        {task, changed, wake}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for pending in (changed, wake):
                        if not pending.done():
                            pending.cancel()
                    await asyncio.gather(changed, wake, return_exceptions=True)
                if wake.done() and not wake.cancelled():
                    self._wake_event.clear()
                    break
                if changed.done() and not changed.cancelled():
                    epoch = hub.demand_generation
                    if not hub.has_viewers:
                        break
            if not task.done():
                await _cancel_bounded(task)
        finally:
            if not task.done():
                await _cancel_bounded(task)
            self._opening = None

        if task.cancelled():
            return _DEMAND_LOST
        exc = task.exception()
        if exc is None:
            return _OPENED
        if isinstance(exc, _DemandChanged):
            return _DEMAND_LOST
        logger.error("could not open providers for a connected viewer", exc_info=exc)
        hub.publish_error("provider failed to open for a viewer", Stage.ASR)
        return _FAILED

    async def _open_sequence(self, epoch: int) -> None:
        """Open the shared recogniser for one audience epoch, cleaning up on failure.

        Only the recogniser is opened here. German translation is warmed and started
        separately, by :meth:`_sync_german_target`, which races the warm against the
        German demand alone. That separation is what lets a German viewer who leaves
        while Arabic viewers remain cancel a billed German warm without touching the
        shared recogniser or the Arabic stream.

        Every await is followed by a recheck, so a disconnect during the billed ASR
        open tears the half-open provider down instead of completing an open for an
        audience that has gone.
        """

        hub = self._hub
        assert hub is not None

        try:
            if hub.demand_generation != epoch or not hub.has_viewers:
                raise _DemandChanged

            asr = self._next_asr()
            # Published before open so a cancellation or failure can close a
            # half-open provider rather than leaking its socket.
            self._asr_active = asr
            await asr.open(self.config.asr)

            # Inside the cleanup guard on purpose: a seek that raises must close the
            # recogniser rather than leave it open and unused.
            seek = getattr(asr, "seek", None)
            if seek is not None:
                seek(self._audio_ms)

            if hub.demand_generation != epoch or not hub.has_viewers:
                raise _DemandChanged

            self._collector = asyncio.create_task(
                self._collect_events(asr), name=f"livesubs:asr:{self._session_id}"
            )
            self._audience_open = True
            self._audience_epoch = epoch
            logger.info("recogniser opened for a viewer on session %s", self._session_id)
        except BaseException:
            await self._teardown_paid()
            raise

    def _start_german_worker(self) -> None:
        """Start the German publisher for the current German occupancy."""

        hub = self._hub
        self._de.epoch = hub.demand_generation_for("de") if hub is not None else None
        self._de.active = True
        self._de.rev = 0
        self._de.published_seq = -1
        if self._publisher is None or self._publisher.done():
            self._publisher = asyncio.create_task(
                self._publish_loop(), name=f"livesubs:pub:{self._session_id}"
            )

    async def _stop_german_worker(self) -> None:
        """Stop German paid work, leaving the shared recogniser and Arabic untouched.

        Reached when the last German viewer leaves while Arabic viewers remain: the
        German translator stops, but the recogniser keeps streaming for Arabic and no
        German context is carried across the gap.
        """

        self._de.active = False
        self._de.epoch = None
        publisher, self._publisher = self._publisher, None
        self._finals.clear()
        self._drain_signal()
        self._de.recent_src.clear()
        self._de.recent_target.clear()
        if publisher is not None:
            await _cancel_bounded(publisher)

    async def _sync_german_target(self) -> None:
        """Bring German work up or down to match the German audience.

        Called while the recogniser is already open. Starting German warms the
        translator first, cancellably, so a German viewer who leaves mid-warm does not
        leave a billed warm running or a worker translating for nobody.

        The German demand *generation* is compared, not just the viewer count. A fast
        German leave-and-return that never drops the shared occupancy to zero (because
        Arabic viewers remain) bumps only the German generation; without that check the
        stale worker would look active and German would never restart.
        """

        hub = self._hub
        assert hub is not None
        want = hub.viewer_count_for("de") > 0
        generation = hub.demand_generation_for("de")

        if want:
            if self._de.active and self._de.epoch != generation:
                await self._stop_german_worker()
            if not self._de.active:
                await self._run_german_sync(generation)
        elif self._de.active:
            await self._stop_german_worker()

    async def _run_german_sync(self, de_generation: int) -> None:
        """Warm and start German work, aborting if the German audience changes."""

        task = asyncio.create_task(
            self._warm_and_start_german(de_generation),
            name=f"livesubs:warm:de:{self._session_id}",
        )
        self._german_opening = task
        try:
            while not task.done():
                hub = self._hub
                if hub is None or hub.viewer_count_for("de") == 0:
                    break
                if hub.demand_generation_for("de") != de_generation:
                    break
                changed = asyncio.create_task(
                    hub.wait_for_demand_change_for("de", de_generation)
                )
                wake = asyncio.create_task(self._wake_event.wait())
                try:
                    await asyncio.wait(
                        {task, changed, wake}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for pending in (changed, wake):
                        if not pending.done():
                            pending.cancel()
                    await asyncio.gather(changed, wake, return_exceptions=True)
                if wake.done() and not wake.cancelled():
                    self._wake_event.clear()
                    break
            if not task.done():
                await _cancel_bounded(task)
            elif not task.cancelled() and task.exception() is not None:
                exc = task.exception()
                if not isinstance(exc, _DemandChanged):
                    logger.error("could not warm German translation", exc_info=exc)
                    if self._hub is not None:
                        self._hub.publish_error("translation failed to start", Stage.MT)
        finally:
            if not task.done():
                await _cancel_bounded(task)
            self._german_opening = None

    async def _warm_and_start_german(self, de_generation: int) -> None:
        hub = self._hub
        assert hub is not None

        if not self._de.warmed:
            await self.mt.warm()
            if not self._german_demand_current_for(de_generation):
                raise _DemandChanged
            self._mt_warmed = True
            self._de.warmed = True

        if not self._german_demand_current_for(de_generation):
            raise _DemandChanged

        self._start_german_worker()

    def _german_demand_current_for(self, de_generation: int) -> bool:
        hub = self._hub
        return bool(
            hub is not None
            and hub.viewer_count_for("de") > 0
            and hub.demand_generation_for("de") == de_generation
        )

    async def _wait_for_watch_event(self, generation: int) -> int:
        """Block until the audience changes or a provider event needs attention.

        Wakes on a shared-occupancy change *or* a German-language change, so a German
        viewer joining an Arabic-only room starts German work without waiting for a
        frame, and a German viewer leaving stops it the same way.
        """

        hub = self._hub
        assert hub is not None
        de_generation = hub.demand_generation_for("de")
        demand = asyncio.create_task(hub.wait_for_demand_change(generation))
        german = asyncio.create_task(
            hub.wait_for_demand_change_for("de", de_generation)
        )
        wake = asyncio.create_task(self._wake_event.wait())
        try:
            await asyncio.wait(
                {demand, german, wake}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (demand, german, wake):
                if not task.done():
                    task.cancel()
            await asyncio.gather(demand, german, wake, return_exceptions=True)
        if wake.done() and not wake.cancelled():
            self._wake_event.clear()
        if demand.done() and not demand.cancelled():
            return demand.result()
        return hub.demand_generation

    async def _sleep_until_demand_change(self, seconds: float) -> None:
        """A bounded backoff delay that a demand change cuts short."""

        hub = self._hub
        if hub is None:
            return
        await hub.wait_for_demand_change(hub.demand_generation, timeout=seconds)

    def _next_asr(self) -> AsrProvider:
        """Pick the recogniser for the next open attempt.

        The startup-built ``asr`` is consumed exactly once, before any open. Every
        attempt after that rebuilds from the factory, so a failed or closed adapter is
        never reused and a queued sentinel can never be replayed.
        """

        if self._asr_factory is None:
            assert self.asr is not None
            return self.asr
        if self.asr is not None and not self._initial_asr_consumed:
            self._initial_asr_consumed = True
            return self.asr
        return self._asr_factory()

    async def _teardown_paid(self) -> None:
        """Stop all paid work and discard anything queued for the paused audience.

        ``_audience_open`` is cleared first, synchronously, so capture stops handing
        audio to a provider that is being closed. The publisher is cancelled before
        the recogniser so no translation request outlives the audience, and the queues
        are dropped rather than replayed when a viewer returns.
        """

        self._audience_open = False
        self._audience_epoch = None
        self._de.active = False
        self._de.epoch = None
        publisher, self._publisher = self._publisher, None
        asr, self._asr_active = self._asr_active, None
        collector, self._collector = self._collector, None

        self._finals.clear()
        self._drain_signal()
        self._inbox.clear()
        # A new stream restarts its clock at zero, so what was sent is forgotten with it.
        self._assembler.reset()
        self._speech_final = False
        # Context from before the pause must not colour the first line after it.
        self._recent_src.clear()
        self._recent_de.clear()

        if publisher is not None:
            await _cancel_bounded(publisher)
        if asr is not None:
            await _close_bounded(asr)
        if collector is not None:
            await _cancel_bounded(collector)

        if self._hub is not None:
            # Every language's cached line is dropped: the lecture is paused, so no
            # viewer of any language should be seeded with what was on screen before.
            self._hub.forget_segment_snapshot()

    async def _stop_watcher(self) -> None:
        """Stop the demand watchers and any in-flight open.

        Waits a bounded time for each watcher to notice ``_closing`` and exit at a
        safe point, and only force-cancels if it does not. That ordering matters:
        cancelling mid-teardown could leave a provider half-closed.
        """

        self._closing = True
        self._wake_event.set()

        for attribute in ("_watcher", "_arabic_watcher"):
            watcher = getattr(self, attribute)
            setattr(self, attribute, None)
            if watcher is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(watcher), timeout=_WATCHER_STOP_GRACE_S
                    )
                except TimeoutError:
                    logger.warning("%s did not stop in time; cancelling", attribute)
                    await _cancel_bounded(watcher)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("%s failed during shutdown", attribute)

        for attribute in ("_opening", "_german_opening", "_arabic_opening"):
            opening = getattr(self, attribute)
            setattr(self, attribute, None)
            if opening is not None:
                await _cancel_bounded(opening)

    # --- Arabic work, kept independent of the shared recogniser ---------------

    def _next_ar_mt(self) -> MtProvider:
        """The Arabic translator, built on first use from the factory.

        Only ever called while an Arabic viewer is present, so a German-only lecture
        never constructs an Arabic provider at all.
        """

        if self._ar.mt is not None:
            return self._ar.mt
        if self._ar.mt_factory is None:
            raise ValueError("Arabic subtitles are not configured for this pipeline")
        self._ar.mt = self._ar.mt_factory()
        return self._ar.mt

    def _arabic_demand_current_for(self, generation: int) -> bool:
        hub = self._hub
        return bool(
            hub is not None
            and hub.viewer_count_for("ar") > 0
            and hub.demand_generation_for("ar") == generation
        )

    async def _watch_arabic(self) -> None:
        """Start Arabic translation while Arabic viewers are present.

        A task of its own, independent of the shared recogniser and of German work.
        The last Arabic viewer leaving cancels the warm, the in-flight request and the
        queued segments, and clears the Arabic cached line - without closing the
        recogniser or disturbing the German stream. Consecutive start failures are
        retried with a bounded backoff while Arabic viewers remain.
        """

        hub = self._hub
        assert hub is not None
        failures = 0

        try:
            while not self._closing:
                generation = hub.demand_generation_for("ar")
                if hub.viewer_count_for("ar") == 0:
                    failures = 0
                    if self._ar.active or self._arabic_opening is not None:
                        await self._teardown_arabic()
                    await self._wait_for_arabic_event(generation)
                    continue

                if self._ar.active and self._ar.epoch == generation:
                    failures = 0
                    await self._wait_for_arabic_event(generation)
                    continue

                if self._ar.active:
                    await self._teardown_arabic()

                outcome = await self._attempt_arabic_open(generation)
                if outcome in (_OPENED, _DEMAND_LOST):
                    failures = 0
                else:                       # _FAILED
                    failures += 1
                    if failures <= _OPEN_RETRY_LIMIT:
                        await hub.wait_for_demand_change_for(
                            "ar", generation, timeout=_backoff_seconds(failures)
                        )
                    else:
                        await self._wait_for_arabic_event(hub.demand_generation_for("ar"))
                        failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Arabic watcher stopped unexpectedly")

    async def _wait_for_arabic_event(self, generation: int) -> None:
        """Block until the Arabic audience changes. No frame is needed to wake."""

        hub = self._hub
        assert hub is not None
        await hub.wait_for_demand_change_for("ar", generation)

    async def _attempt_arabic_open(self, epoch: int) -> str:
        """Run one Arabic start attempt, aborting it if the Arabic audience changes."""

        task = asyncio.create_task(
            self._open_arabic_sequence(epoch),
            name=f"livesubs:open:ar:{self._session_id}",
        )
        self._arabic_opening = task
        try:
            while not task.done():
                hub = self._hub
                if hub is None or hub.viewer_count_for("ar") == 0:
                    break
                changed = asyncio.create_task(
                    hub.wait_for_demand_change_for("ar", epoch)
                )
                try:
                    await asyncio.wait(
                        {task, changed}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    if not changed.done():
                        changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
            if not task.done():
                await _cancel_bounded(task)
        finally:
            if not task.done():
                await _cancel_bounded(task)
            self._arabic_opening = None

        if task.cancelled():
            return _DEMAND_LOST
        exc = task.exception()
        if exc is None:
            return _OPENED
        if isinstance(exc, _DemandChanged):
            return _DEMAND_LOST
        logger.error("could not start Arabic translation", exc_info=exc)
        if self._hub is not None:
            self._hub.publish_error("translation failed to start", Stage.MT)
        return _FAILED

    async def _open_arabic_sequence(self, epoch: int) -> None:
        """Warm the Arabic translator and start its worker for one Arabic occupancy.

        Every await is followed by a recheck, so a disconnect during the billed warm
        leaves no warm running and starts no worker for an audience that has gone. A
        fresh occupancy also drops any stale queue and rolling context, so a reconnect
        never replays the previous Arabic line or the previous English context.
        """

        hub = self._hub
        assert hub is not None

        try:
            mt = self._next_ar_mt()
            if not self._ar.warmed:
                await mt.warm()
                if not self._arabic_demand_current_for(epoch):
                    raise _DemandChanged
                self._ar.warmed = True

            if not self._arabic_demand_current_for(epoch):
                raise _DemandChanged

            self._reset_arabic_state(epoch)
            self._ar.active = True
            self._ar.task = asyncio.create_task(
                self._translate_loop(self._ar), name=f"livesubs:pub:ar:{self._session_id}"
            )
            logger.info("Arabic translation started on session %s", self._session_id)
        except BaseException:
            await self._teardown_arabic()
            raise

    def _reset_arabic_state(self, epoch: int) -> None:
        self._ar.queue.clear()
        _drain_queue(self._ar.signal)
        self._ar.recent_src.clear()
        self._ar.recent_target.clear()
        self._ar.rev = 0
        self._ar.published_seq = -1
        self._ar.lagging = False
        self._ar.epoch = epoch

    async def _teardown_arabic(self) -> None:
        """Stop Arabic paid work and forget its state, leaving everything else alone.

        The shared recogniser, the English source and the German stream are untouched:
        an Arabic viewer leaving must not interrupt German subtitles or reset the
        German ordering.
        """

        self._ar.active = False
        self._ar.epoch = None
        task, self._ar.task = self._ar.task, None
        self._ar.queue.clear()
        _drain_queue(self._ar.signal)
        self._ar.recent_src.clear()
        self._ar.recent_target.clear()
        if task is not None:
            await _cancel_bounded(task)
        if self._hub is not None:
            # Only the Arabic cached line is dropped, so a German viewer who stayed
            # keeps reading the current German line.
            self._hub.forget_segment_snapshot("ar")

    async def _open_providers(self) -> None:
        """Open the providers for a non-gated run and keep them open.

        The simulation and direct pipeline tests have no viewers and must simply run;
        this is the pre-audience-gate behaviour, unchanged.
        """

        if not self._mt_warmed:
            await self.mt.warm()
            self._mt_warmed = True
            self._de.warmed = True

        asr = self._next_asr()
        self._asr_active = asr
        try:
            await asr.open(self.config.asr)
            seek = getattr(asr, "seek", None)
            if seek is not None:
                seek(self._audio_ms)
        except BaseException:
            await self._teardown_paid()
            raise

        self._collector = asyncio.create_task(
            self._collect_events(asr), name=f"livesubs:asr:{self._session_id}"
        )
        # An ungated run has no hub audience to read language counts from; it exists
        # for the German simulation and direct pipeline tests, so German is active.
        self._de.active = True
        self._publisher = asyncio.create_task(
            self._publish_loop(), name=f"livesubs:pub:{self._session_id}"
        )
        self._audience_open = True

    async def _stop_publisher(self) -> None:
        publisher, self._publisher = self._publisher, None
        if publisher is not None:
            await _cancel_bounded(publisher)

    async def _cancel_collector(self) -> None:
        collector, self._collector = self._collector, None
        if collector is not None:
            await _cancel_bounded(collector)

    def _drain_signal(self) -> None:
        """Forget a wake token left over from a cancelled publisher.

        Without this a publisher started after a pause would consume the stale token
        and busy-spin, draining an empty ``_finals`` and rechecking forever.
        """

        while not self._signal.empty():
            try:
                self._signal.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - only under a race
                break

    async def _collect_events(self, asr: AsrProvider) -> None:
        """Move provider events into the inbox. The only awaiting reader."""

        try:
            async for event in asr.events():
                self._inbox.append(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failure while we are deliberately closing, or after the audience has
            # gone, is expected teardown noise from a socket being torn down. It must
            # not be reported to the operator or make the watcher rebuild a provider
            # for a session that is ending.
            if self._closing or not self._audience_open:
                logger.debug("ASR event stream ended during teardown")
                return
            logger.exception("ASR event stream failed")
            self._provider_failed = True
            self._wake_event.set()
            if self._hub is not None:
                self._hub.publish_error("provider event stream failed", Stage.ASR)
        else:
            # The provider ended on its own - unhealthy, or hung up. Stop paid work and
            # let the watcher build a fresh provider, unless we are the ones closing.
            if self._audience_open and not self._closing:
                self._provider_failed = True
                self._wake_event.set()

    async def _finish(self, buffer: SegmentBuffer) -> None:
        """Shut down in the one order that does not lose the last subtitle.

        Closing the provider comes **first**, and that is the whole point of the
        order. A provider flushes the utterance still open when its stream closes,
        and that flush is the last line of the lecture. Waiting for the publisher
        before closing the provider would leave the final draft of the session
        never committed, so the overlay would hold a line it can never finalise.

        Every stage is bounded, and the bounds add up to less than the session's own
        3 s shutdown budget, so this always finishes before the session loses
        patience and cancels ``run`` outright.

        Tolerant of the audience-gated state: when there was no viewer at the end,
        there is no provider, collector or publisher, and there is nothing to flush.
        The demand watcher is stopped first, so no background task can reopen a paid
        provider while this shutdown is draining the last subtitle.
        """

        # 0. Stop the demand watchers and any in-flight open.
        await self._stop_watcher()
        if not self._demand_is_current():
            await self._teardown_paid()
            # The shared audience is gone, so Arabic paid work is cancelled too. When
            # Arabic viewers remain, the Arabic watcher already stopped, so its worker
            # is drained below instead.
            await self._teardown_arabic()

        # 1. Close the provider: its close ends the event stream, after flushing any
        #    utterance that was still open.
        asr = self._asr_active
        self._asr_active = None
        if asr is not None:
            try:
                await asr.close()
            except Exception:
                logger.exception("ASR provider failed to close")

        # 2. Wait for the collector to move that flush into the inbox, rather than
        #    hoping a single event-loop turn is enough.
        collector = self._collector
        self._collector = None
        if collector is not None:
            try:
                await asyncio.wait_for(collector, timeout=_FLUSH_GRACE_S)
            except TimeoutError:
                await _cancel(collector)
            except Exception:
                logger.exception("event collector failed during shutdown")

        self._drain_events()

        # 3. Commit the utterance that was still open, so nothing is left as a
        #    draft forever. Text the assembler still holds was never sent, so this
        #    cannot duplicate a final. Every active target gets it.
        text = self._assembler.commit()
        if text.strip():
            self._dispatch(
                (
                    self._seq,
                    text,
                    buffer.started_ms,
                    max(buffer.end_ms, buffer.started_ms),
                )
            )

        # 4. Now let the publishers drain everything, that last segment included.
        self._shutdown.set()
        self._wake()

        publisher = self._publisher
        self._publisher = None
        if publisher is not None:
            try:
                await asyncio.wait_for(publisher, timeout=_SHUTDOWN_GRACE_S)
            except TimeoutError:
                logger.warning(
                    "publisher did not drain within %.1fs; cancelling", _SHUTDOWN_GRACE_S
                )
                await _cancel(publisher)
            except Exception:
                logger.exception("publisher failed during shutdown")

        await self._drain_arabic()

        if collector is not None:
            await _cancel(collector)

        try:
            await self.source.close()
        except Exception:
            logger.exception("audio source failed to close")

    async def _drain_arabic(self) -> None:
        """Let the Arabic worker finish its last segment, then stop it.

        Bounded like the German drain: the final Arabic line is worth waiting for, but
        not past the session's shutdown budget. If Arabic viewers had already left, the
        worker is gone and there is nothing to drain.
        """

        task = self._ar.task
        if task is None:
            return

        self._ar_draining = True
        self._wake_target(self._ar)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_SHUTDOWN_GRACE_S)
        except TimeoutError:
            logger.warning(
                "Arabic translation did not drain within %.1fs; cancelling",
                _SHUTDOWN_GRACE_S,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Arabic translator failed during shutdown")

        self._ar.task = None
        self._ar.active = False
        await _cancel_bounded(task)

    async def _publish_loop(self) -> None:
        """The German publisher, kept under its original name for existing callers.

        The only place German ``seq`` is consumed, so German ordering needs no
        argument: the translation of sequence *n* is always finished before sequence
        *n + 1* is looked at.
        """

        await self._translate_loop(self._de)

    async def _translate_loop(self, target: _Target) -> None:
        """Drain one target's queue, one segment at a time.

        German and Arabic each get their own loop, so a slow Arabic request cannot
        stall the next German segment. Each loop is the only consumer of its own
        queue, which is what makes ``rev`` monotonic per language.

        The epoch this worker was started for is captured once, so a worker that ends
        on a stale demand can tell whether it was already replaced and avoid clearing a
        replacement's state.
        """

        epoch = target.epoch

        while True:
            if target.queue:
                if not self._target_demand_is_current(target):
                    self._retire_target(target, epoch)
                    return
                seq, src, start_ms, end_ms = target.queue.popleft()
                await self._translate_target(target, seq, src, start_ms, end_ms)
                self._check_lag_target(target)
                continue

            if target is self._de and self._shutdown.is_set():
                return
            if target is self._ar and self._ar_draining:
                return

            await target.signal.get()

    def _target_demand_is_current(self, target: _Target) -> bool:
        if target.language == "de":
            return self._german_demand_is_current()
        return self._arabic_demand_is_current()

    def _retire_target(self, target: _Target, epoch: int | None) -> None:
        """Mark a worker that ended itself as no longer active.

        A worker leaves on a stale or absent demand without the watcher necessarily
        having observed it. Clearing the flag is what lets the watcher restart German
        work for the current occupancy; the wake is what makes it look promptly rather
        than at the next frame.

        The epoch guard matters: if a replacement worker has already been started for a
        newer German epoch, this stale worker must not clear the replacement's state, or
        German would freeze.
        """

        if target.epoch != epoch:
            return
        target.active = False
        target.epoch = None
        if target is self._de:
            self._wake_event.set()

    async def _translate_target(
        self, target: _Target, seq: int, src: str, start_ms: int, end_ms: int
    ) -> None:
        """Translate one segment for one language, publishing revisions as text arrives.

        Streaming the translation is the point: the first words reach the screen long
        before the sentence finishes, which is one of the three things that make the
        pipeline feel instant. A failure here is bounded to this language - the other
        language's loop is untouched - and never falls back to English.
        """

        if not self._target_demand_is_current(target):
            return

        mt = target.mt
        if mt is None:
            logger.error("%s translation has no provider", target.language)
            if self._hub is not None:
                self._hub.publish_error("translation failed", Stage.MT)
            return

        context = MtContext(
            source_context=list(target.recent_src),
            target_context=list(target.recent_target),
        )

        chunks: list[str] = []
        translated = ""

        try:
            async for chunk in mt.translate(src, context):
                if chunk.text_delta:
                    chunks.append(chunk.text_delta)
                    translated = "".join(chunks)
                    self._publish_target(
                        target, seq, SegmentState.DRAFT, translated, src=src, audio_ms=end_ms
                    )
                if chunk.done:
                    break
        except Exception:
            logger.exception("translation of segment %d failed for %s", seq, target.language)
            if self._hub is not None:
                self._hub.publish_error("translation failed", Stage.MT)
            return

        if not translated.strip():
            # Falling back to the English source would put English on the overlay,
            # which is worse than showing nothing at all.
            logger.warning(
                "segment %d produced no %s; dropping it", seq, target.language
            )
            if self._hub is not None:
                self._hub.publish_error("empty translation", Stage.MT)
            return

        self._publish_target(
            target, seq, SegmentState.FINAL, translated, src=src, audio_ms=end_ms
        )
        self._remember_target(target, src, translated)

        self._emit_committed(target, seq, start_ms, end_ms, src, translated)

    def _emit_committed(
        self,
        target: _Target,
        seq: int,
        start_ms: int,
        end_ms: int,
        src: str,
        text: str,
    ) -> None:
        """Hand the finished segment to the sink, if there is one.

        Wrapped for the same reason telemetry is: writing a transcript is an extra,
        and an extra must never be able to stop the lecture it is describing.
        """

        if self.on_segment is None:
            return

        try:
            if target.language == "de":
                committed = Committed(
                    seq=seq, start_ms=start_ms, end_ms=end_ms, src=src, de=text
                )
            else:
                committed = Committed(
                    seq=seq,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    src=src,
                    de="",
                    ar=text,
                    language="ar",
                )
            self.on_segment(committed)
        except Exception:
            logger.exception("the segment sink failed; the lecture continues")

    def _publish_target(
        self,
        target: _Target,
        seq: int,
        state: SegmentState,
        text: str,
        *,
        src: str | None = None,
        audio_ms: int | None = None,
    ) -> None:
        hub = self._hub
        if hub is None:
            return

        # ``rev`` starts at 0 for a new seq and only ever increases, which is the
        # contract the overlay's in-place update depends on. It is tracked per target
        # so the two languages never share a revision counter.
        if seq != target.published_seq:
            target.published_seq = seq
            target.rev = 0
        else:
            target.rev += 1

        if target.language == "de":
            segment = SubtitleSegment(
                seq=seq,
                rev=target.rev,
                state=state,
                language="de",
                de=text,
                src=src,
                timing=Timing(
                    audio_ms=self._audio_ms if audio_ms is None else audio_ms,
                    emit_ms=self._now_ms(),
                ),
            )
        else:
            segment = SubtitleSegment(
                seq=seq,
                rev=target.rev,
                state=state,
                language="ar",
                de="",
                ar=text,
                src=src,
                timing=Timing(
                    audio_ms=self._audio_ms if audio_ms is None else audio_ms,
                    emit_ms=self._now_ms(),
                ),
            )

        hub.publish_segment(segment)

    def _remember_target(self, target: _Target, src: str, text: str) -> None:
        """Keep the last few segments as translation context, per language.

        German is verb-final, so an isolated English fragment translates badly.
        Arabic has its own rolling target context, kept separate so a German line can
        never appear as Arabic context or vice versa. Context goes in the user message,
        never the system prompt - see ``docs/05-providers.md`` on cache prefixes.
        """

        limit = max(0, self.config.segmenter.context_segments)
        if not limit:
            return

        target.recent_src.append(src)
        target.recent_target.append(text)
        del target.recent_src[:-limit]
        del target.recent_target[:-limit]

    def _check_lag_target(self, target: _Target) -> None:
        """Surface backlog for one language, without disturbing the other's status.

        A backlog is a property of a language's own queue, so a slow Arabic request
        that leaves Arabic behind does not mark a healthy German stream as delayed.
        """

        behind = len(target.queue) >= self.config.lag_queue_limit
        if behind and not target.lagging:
            target.lagging = True
            logger.warning(
                "%s translation is %d segments behind", target.language, len(target.queue)
            )
        elif not behind and target.lagging:
            target.lagging = False

        # Only the German stream drives the shared status pill today; an Arabic
        # backlog must not mark a healthy German stream as delayed, and vice versa.
        if target.language == "de":
            self._check_lag()

    def _drain_events(self) -> bool:
        """Fold queued provider events into the unsent text, without awaiting.

        The assembler decides what each event means for the text of the next segment;
        an interim replaces the open hypothesis, a settled chunk is kept. A provider
        endpoint is recorded as a hint for the segmenter, never as a command. A settled
        chunk is not an endpoint.

        Returns whether the unsent text changed, which is the segmenter's evidence that
        the speaker is still talking.
        """

        changed = False
        inbox = self._inbox
        while inbox:
            event = inbox.popleft()
            if self._assembler.feed(event):
                changed = True
            if event.speech_final:
                self._speech_final = True
        return changed

    def _wake(self) -> None:
        """Signal the German publisher. Idempotent, because the signal is a token not a count."""

        self._wake_target(self._de)

    def _wake_target(self, target: _Target) -> None:
        """Signal one target's publisher. Idempotent, like the German signal."""

        if target.signal.empty():
            target.signal.put_nowait(None)

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

#: Open attempts retried, with a bounded backoff, while viewers remain but the
#: provider cannot be opened. After this many the watcher waits for a demand change
#: instead of hammering a failing provider.
_OPEN_RETRY_LIMIT = 5

#: Bounded backoff between open retries. No CLI option: this is a fixed guard
#: against a hot loop, not a deployment knob.
_OPEN_RETRY_INITIAL_S = 0.5
_OPEN_RETRY_MAX_S = 5.0

#: How long a background task (publisher, collector, watcher, opening) is given to
#: respond to cancellation before the pipeline gives up on it.
_TASK_CANCEL_GRACE_S = 2.0

#: How long a provider's ``close`` is allowed to take on a teardown.
_ASR_CLOSE_GRACE_S = 2.0

#: How long the watcher is given to notice shutdown before it is force-cancelled.
_WATCHER_STOP_GRACE_S = 2.0


def _backoff_seconds(failures: int) -> float:
    """Exponential backoff, capped, for consecutive open failures."""

    exponent = max(0, failures - 1)
    return min(_OPEN_RETRY_MAX_S, _OPEN_RETRY_INITIAL_S * (2 ** exponent))


def _drain_queue(signal: asyncio.Queue[None]) -> None:
    """Forget any wake token left over from a cancelled worker.

    Without this a worker started after a pause would consume the stale token and
    busy-spin, draining an empty queue and rechecking forever.
    """

    while not signal.empty():
        try:
            signal.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - only under a race
            break


class _DemandChanged(Exception):
    """Raised inside an open attempt when the audience it began for has gone."""


#: Open-attempt outcomes, returned by :meth:`LivePipeline._attempt_open`.
_OPENED = "opened"
_FAILED = "failed"
_DEMAND_LOST = "demand_lost"


def _consume(task: asyncio.Task[object]) -> None:
    """Retrieve a finished task's result, so a failure is not logged as unretrieved."""

    try:
        task.exception()
    except BaseException:  # noqa: BLE001
        # A cancelled task raises CancelledError here; there is nothing to record.
        logger.debug("cancelled task %s had no retrievable result", task.get_name())


async def _cancel_bounded(
    task: asyncio.Task[None], timeout: float = _TASK_CANCEL_GRACE_S
) -> None:
    """Cancel a task, waiting a bounded time, and never raise."""

    if task.done():
        _consume(task)
        return

    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        pass
    except Exception:
        logger.exception("task %s errored while being cancelled", task.get_name())


async def _close_bounded(asr: AsrProvider, timeout: float = _ASR_CLOSE_GRACE_S) -> None:
    """Close a provider, bounded so a wedged socket cannot stall teardown."""

    try:
        await asyncio.wait_for(asr.close(), timeout=timeout)
    except TimeoutError:
        logger.warning("ASR provider did not close within %.1fs", timeout)
    except Exception:
        logger.exception("ASR provider failed to close")


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
