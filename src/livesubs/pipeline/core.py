"""The live pipeline: microphone to screen.

This module owns the capture loop and the one rule about ordering that everything else
depends on. The lifecycle of paid work lives in :mod:`livesubs.pipeline.lifecycle`, the
recogniser in :mod:`~livesubs.pipeline.asr_session` and each target language's translation
in :mod:`~livesubs.pipeline.targets`, so that this file is about the audio.

**1. Audio frames are the clock.** Frame arrival drives the segment cap and the
silence detector. There is no separate timer task, so nothing can drift against
the audio. It also means the pipeline behaves identically whether frames arrive
from a device in real time or from a file replayed faster, which is the only
reason the audio path is testable at all.

**2. One writer per language, one ordering.** A language's lines are published only by that
language's worker, which handles one segment at a time. Capture never publishes, so ``seq``
cannot invert and a ``final`` can never be followed by a ``draft``. Capture hands work over
through a queue and returns immediately, so a slow provider applies backpressure to nothing.

**3. Only translated text is ever published.** The obvious-looking shortcut of echoing the
recogniser's partial into a subtitle so something appears sooner puts the speaker's language
on a screen meant for another. The speaker's words ride in ``src``, which the hub strips for
viewers. That has a latency consequence worth stating plainly: the screen shows nothing
new until the first translated word of the next segment arrives, roughly one endpoint delay
after the speaker pauses. A placeholder would be worse than briefly holding the previous
line, and translating every partial would multiply the translation bill by about eight for
no reading-time gain, because reading speed is the binding constraint anyway.

**4. Trouble is made visible, not absorbed.** If a translator falls behind, the
language's readers are told it is delayed, and past a limit the oldest queued segments are
dropped. Queueing without bound would look healthy while the subtitles drifted further and
further behind the speaker, which is the one failure a viewer cannot forgive. A recogniser
that dies is reported and started again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping

from livesubs.asr.base import AsrProvider
from livesubs.audio.base import AudioSource, Preparable, ReportsCapture
from livesubs.audio.calibration import CalibrationNotice
from livesubs.events import PipelineStatus, Stage
from livesubs.hub import Hub
from livesubs.ingest import FrameStats, Ingest
from livesubs.mt.base import MtProvider
from livesubs.pipeline.asr_session import AsrSession
from livesubs.pipeline.health import Health
from livesubs.pipeline.lifecycle import DemandChanged, Lifecycle
from livesubs.pipeline.records import Committed, PipelineConfig
from livesubs.pipeline.targets import Item, TargetWorker, WorkerEnv
from livesubs.segmenter import Decision, SegmentBuffer
from livesubs.transcript import TranscriptAssembler

__all__ = ["Committed", "LivePipeline", "PipelineConfig"]

logger = logging.getLogger(__name__)

#: How long the workers are given to finish translations already queued, at shutdown.
#: Together with the recogniser's flush this must stay below the session's own 3 s
#: shutdown budget, so that draining completes before the session loses patience and
#: cancels ``run`` outright. All workers drain at once, so the budget does not grow with the
#: number of languages.
SHUTDOWN_GRACE_S = 1.5


class LivePipeline:
    """Capture to hub. Implements :class:`~livesubs.hub.SegmentPublisher`."""

    def __init__(
        self,
        *,
        source: AudioSource,
        asr: AsrProvider | None = None,
        asr_factory: Callable[[], AsrProvider] | None = None,
        translators: Mapping[str, MtProvider | Callable[[], MtProvider]],
        source_language: str = "en",
        config: PipelineConfig | None = None,
        on_stats: Callable[[FrameStats], None] | None = None,
        on_segment: Callable[[Committed], None] | None = None,
        require_audience: bool = False,
    ) -> None:
        """Wire a pipeline.

        ``translators`` maps each target language to its translator, or to a factory that
        builds one. A factory is called only while a viewer of that language is present,
        so a language nobody asks for is never constructed or warmed. The order is the
        order languages are listed in.

        With ``require_audience`` the paid providers open only while a viewer is
        connected. Capture keeps running; the frames are dropped, not buffered.
        """

        if not translators:
            raise ValueError("a pipeline needs a translator for at least one target language")
        if source_language in translators:
            raise ValueError(f"{source_language!r} is the speaker's language, not a target")

        self.source = source
        self.source_language = source_language
        self.config = config or PipelineConfig()
        #: Called at most once per ``telemetry_interval_ms`` with the latest frame
        #: statistics. Deliberately a callback rather than a wire message: the operator
        #: console should not need the pipeline to change.
        self.on_stats = on_stats
        #: Called once per committed segment and language, with both texts and the audio
        #: span. This is what writes a transcript or a subtitle file. A failure in it must
        #: not be able to stop a lecture, exactly as for ``on_stats``.
        self.on_segment = on_segment
        self._require_audience = require_audience

        self._asr = AsrSession(
            first=asr,
            factory=asr_factory,
            config=self.config.asr,
            on_ended=self._recogniser_ended,
        )
        self.ingest = Ingest(self.config.ingest)
        self._assembler = TranscriptAssembler()

        self._workers: dict[str, TargetWorker] = {}
        for language, translator in translators.items():
            is_instance = hasattr(translator, "translate")
            self._workers[language] = TargetWorker(
                language,
                provider=translator if is_instance else None,  # type: ignore[arg-type]
                factory=None if is_instance else translator,  # type: ignore[arg-type]
                source_language=source_language,
            )

        self._hub: Hub | None = None
        self._health: Health | None = None
        self._stop: asyncio.Event | None = None
        self._t0 = 0.0
        self._audio_ms = 0
        self._seq = 0
        self._speech_final = False
        self._clipping_reported = False
        #: The recogniser has been reported lost and not yet replaced, so it is said once.
        self._asr_lost = False
        self._capture_detail: str | None = None
        self._session_id = "session"

        self._asr_life: Lifecycle | None = None
        self._worker_lives: dict[str, Lifecycle] = {}

    @property
    def supported_languages(self) -> tuple[str, ...]:
        """The target languages this pipeline can produce, in the order they are offered."""

        return tuple(self._workers)

    # --- running --------------------------------------------------------------------

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
        self._health = Health(self._publish_health)

        env = WorkerEnv(
            hub=hub,
            config=self.config,
            health=self._health,
            now_ms=self._now_ms,
            sink=self.on_segment,
            gated=self._require_audience,
        )
        for language, worker in self._workers.items():
            worker.bind(env, hub.audience(language) if self._require_audience else None)

        if isinstance(self.source, ReportsCapture):
            self.source.set_calibration_reporter(self._capture_calibration)

        if stop.is_set():
            await self.source.close()
            return
        if isinstance(self.source, Preparable) and not await self._prepare(stop):
            return

        if stop.is_set():
            await self.source.close()
            return

        # Latency is measured from here: calibration is not part of it.
        self._t0 = time.monotonic()

        # Live runs follow the audience. Ungated tests and simulations open now.
        if not self._require_audience:
            try:
                await self._open_everything()
            except BaseException:
                await self.source.close()
                await self._close_everything()
                raise
        else:
            hub.enable_audience_gating()
            self._supervise(hub)

        buffer = SegmentBuffer(self.config.segmenter, started_ms=0, last_voice_ms=0)
        self._seq = 0
        last_telemetry_ms = 0
        last_health_ms = 0
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

                if self._recogniser_is_live():
                    # Push before deciding anything: the provider's own endpointing is
                    # worth more the sooner it sees the audio, and `push` is required
                    # not to block for longer than a frame.
                    await self._asr.push(frame.pcm)

                    # Hand the event loop one turn, so the collector can move whatever
                    # the provider just produced into the inbox. Without this the capture
                    # loop never yields between pushing audio and reading events, so the
                    # inbox is always a frame stale - and on the frame where a segment
                    # closes, "a frame stale" means committing that segment without the
                    # words the provider had just sent.
                    await asyncio.sleep(0)
                    activity = self._ingest_events()

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
                else:
                    # No viewer: drop the audio instead of buffering it, and keep the
                    # segmenter closed, so the next line begins with the speech that
                    # follows the audience's return rather than with stale audio.
                    awaiting_resume = True
                    self._assembler.reset()
                    self._speech_final = False
                    self._asr.discard()

                if stats.now_ms - last_health_ms >= self.config.health_check_ms:
                    last_health_ms = stats.now_ms
                    self._check_recogniser()

                if stats.now_ms - last_telemetry_ms >= self.config.telemetry_interval_ms:
                    last_telemetry_ms = stats.now_ms
                    self._report(stats)
        finally:
            await self._finish(buffer)

    async def _prepare(self, stop: asyncio.Event) -> bool:
        """Let the source finish its own set-up, stopping promptly if asked to.

        Returns whether to carry on. A source that is closed because ``stop`` was set
        returns ``False`` having been closed; one whose set-up raises is closed and the
        error is passed on.
        """

        assert isinstance(self.source, Preparable)
        preparation = asyncio.create_task(self.source.prepare())
        stopped = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait((preparation, stopped), return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                await self.source.close()
                preparation.cancel()
                await asyncio.gather(preparation, return_exceptions=True)
                return False
            await preparation
            return True
        except BaseException:
            await self.source.close()
            preparation.cancel()
            await asyncio.gather(preparation, return_exceptions=True)
            raise
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)

    # --- the recogniser -------------------------------------------------------------

    def _recogniser_is_live(self) -> bool:
        """Whether captured frames should reach the recogniser right now.

        Gated on the *shared* audience, not on any one language: a room that reads only one
        language still needs the recogniser, because every language is translated from the
        same source text.
        """

        if not self._asr.is_open:
            return False
        if not self._require_audience:
            return True
        life = self._asr_life
        return life is not None and life.current

    def _ingest_events(self) -> bool:
        """Fold the recogniser's new events into the unsent text, without awaiting.

        The assembler decides what each event means for the text of the next segment;
        an interim replaces the open hypothesis, a settled chunk is kept. A provider
        endpoint is recorded as a hint for the segmenter, never as a command. A settled
        chunk is not an endpoint.

        Returns whether the unsent text changed, which is the segmenter's evidence that the
        speaker is still talking.
        """

        changed = False
        for event in self._asr.drain():
            if self._assembler.feed(event):
                changed = True
            if event.speech_final:
                self._speech_final = True
        return changed

    def _recogniser_ended(self, failed: bool) -> None:
        """The stream ended and nobody closed it: the recogniser is gone."""

        hub = self._hub
        if hub is not None and failed:
            hub.publish_error("recognition stream failed", Stage.ASR)
        self._recogniser_lost("the recognition stream ended")

    def _check_recogniser(self) -> None:
        """A second line of defence: a stream that goes quiet without ending."""

        if self._asr.is_open and not self._asr.healthy:
            self._recogniser_lost("the recogniser reports itself unhealthy")

    def _recogniser_lost(self, reason: str) -> None:
        if self._asr_lost:
            return
        self._asr_lost = True
        logger.warning("%s; reconnecting if anyone is watching", reason)
        if self._health is not None:
            self._health.problem("asr", "recognition is reconnecting")
        if self._asr_life is not None:
            self._asr_life.fail()

    async def _start_recogniser(self, generation: int) -> None:
        audience = self._require_hub().audience()

        def current() -> bool:
            return audience.present and audience.generation == generation

        if not current():
            raise DemandChanged
        try:
            await self._asr.open(self._audio_ms)
            if not current():
                raise DemandChanged
        except BaseException:
            await self._asr.close()
            raise
        self._assembler.reset()
        self._speech_final = False
        self._asr_lost = False
        logger.info("recogniser opened for a viewer on session %s", self._session_id)

    async def _stop_recogniser(self) -> None:
        await self._asr.close()
        # A new stream restarts its clock at zero, so what was sent is forgotten with it.
        self._assembler.reset()
        self._speech_final = False

    # --- supervision ----------------------------------------------------------------

    def _supervise(self, hub: Hub) -> None:
        """Start the supervisors that open and close paid work with the audience.

        One for the shared recogniser and one per language, deliberately separate:
        starting or stopping one language's work must never tear down the recogniser or
        another language's stream.
        """

        health = self._require_health()
        self._asr_life = Lifecycle(
            name="recogniser",
            audience=hub.audience(),
            start=self._start_recogniser,
            stop=self._stop_recogniser,
            on_trouble=lambda detail: health.problem("asr", "recognition is reconnecting"),
            on_ok=lambda: health.clear("asr"),
        )
        self._asr_life.spawn()

        for language, worker in self._workers.items():
            life = Lifecycle(
                name=f"translator:{language}",
                audience=hub.audience(language),
                start=worker.start,
                stop=worker.stop,
                on_trouble=lambda detail, w=worker: self._require_health().problem(
                    "mt", "translation could not be started", scope=w.language
                ),
                on_ok=lambda w=worker: self._require_health().clear("mt", scope=w.language),
            )
            self._worker_lives[language] = life
            life.spawn()

    async def _open_everything(self) -> None:
        """Open every provider for a run that does not follow an audience.

        The simulation and direct pipeline tests have no viewers and must simply run.
        """

        await self._asr.open(self._audio_ms)
        for worker in self._workers.values():
            await worker.start(None)

    async def _close_everything(self) -> None:
        await self._asr.close()
        for worker in self._workers.values():
            await worker.stop()

    # --- handing segments to the languages -----------------------------------------

    def _dispatch(self, committed: Item) -> None:
        """Hand one committed source segment to every language that has a viewer.

        Every language gets the same source text and the same ``seq``; each keeps its own
        ``rev`` counter, so the streams never share a revision timeline.
        """

        for worker in self._workers.values():
            worker.offer(committed)

    # --- shutting down --------------------------------------------------------------

    async def _finish(self, buffer: SegmentBuffer) -> None:
        """Shut down in the one order that does not lose the last subtitle.

        Closing the provider comes **first**, and that is the whole point of the order. A
        provider flushes the utterance still open when its stream closes, and that flush is
        the last line of the lecture. Waiting for the workers before closing the provider
        would leave the final draft of the session never committed, so the overlay would
        hold a line it can never finalise.

        Every stage is bounded, and the bounds add up to less than the session's own 3 s
        shutdown budget, so this always finishes before the session loses patience and
        cancels ``run`` outright.

        Tolerant of the audience-gated state: when there was no viewer at the end, there is
        no provider or worker, and there is nothing to flush. The supervisors are stopped
        first, so none of them can reopen a paid provider while this shutdown drains the
        last subtitle.
        """

        # 0. Stop the supervisors, so nothing reopens or closes under this shutdown.
        await self._stop_supervisors()

        audience_here = (not self._require_audience) or self._require_hub().has_viewers
        if not audience_here:
            await self._close_everything()
        else:
            # 1. Close the provider: its close ends the event stream, after flushing any
            #    utterance that was still open. The collector is given time to move that
            #    flush into the inbox, rather than hoping a single event-loop turn is
            #    enough.
            await self._asr.finish()
            self._ingest_events()

            # 2. Commit the utterance that was still open, so nothing is left as a draft
            #    forever. Text the assembler still holds was never sent, so this cannot
            #    duplicate a final. Every active language gets it.
            text = self._assembler.commit()
            if text.strip():
                self._dispatch(
                    (self._seq, text, buffer.started_ms, max(buffer.end_ms, buffer.started_ms))
                )

            # 3. Now let the workers drain everything, that last segment included. All at
            #    once: the budget is for shutdown, not per language.
            await asyncio.gather(
                *(worker.drain(SHUTDOWN_GRACE_S) for worker in self._workers.values())
            )

        try:
            await self.source.close()
        except Exception:
            logger.exception("audio source failed to close")

    async def _stop_supervisors(self) -> None:
        lives = [life for life in (self._asr_life, *self._worker_lives.values()) if life]
        await asyncio.gather(*(life.close() for life in lives))

    # --- reporting ------------------------------------------------------------------

    def _publish_health(
        self, status: PipelineStatus, detail: str | None, language: str | None
    ) -> None:
        if self._hub is not None:
            self._hub.publish_status(status, detail, language=language)

    def _capture_calibration(self, notice: CalibrationNotice) -> None:
        """Expose capture adjustments separately from translation backlog."""

        hub, health = self._hub, self._health
        if hub is None or health is None or (self._stop is not None and self._stop.is_set()):
            return

        self._capture_detail = f"microphone: {notice.message}"
        if notice.state in {"failed", "degraded"}:
            # `degraded` is runtime gain protection giving up while capture carries on:
            # the operator needs to know, but the viewers' subtitles are not delayed.
            hub.publish_error(notice.message, Stage.CAPTURE)

        if notice.state in {"calibrating", "adjusting", "failed"}:
            health.problem("capture", self._capture_detail)
        else:
            health.clear("capture", detail=self._capture_detail)

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
        """Wall-clock milliseconds since the first frame was captured.

        Distinct from the audio clock on purpose: the gap between the two is the
        pipeline's own latency, which is exactly what ``Timing`` is for. Measured from the
        first frame, not from startup, so calibration is not counted as lag.
        """

        return int((time.monotonic() - self._t0) * 1000)

    def _require_hub(self) -> Hub:
        if self._hub is None:
            raise RuntimeError("the pipeline is not running")
        return self._hub

    def _require_health(self) -> Health:
        if self._health is None:
            raise RuntimeError("the pipeline is not running")
        return self._health

