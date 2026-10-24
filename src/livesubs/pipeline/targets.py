"""One target language's translation: its queue, its worker, its revision numbers.

Every language is an instance of the same class, so a slow Arabic request cannot stall the
next German segment and the two never share a counter. The class is the only consumer of its
own queue, which is what makes ``rev`` monotonic per language: the translation of segment
*n* has finished before segment *n + 1* is looked at.

What it guarantees to a viewer:

* **Streaming.** The first words reach the screen long before the sentence finishes, which
  is one of the three things that make the pipeline feel instant.
* **No other language.** A failure is bounded to this language and never falls back to the
  speaker's words, which would put the wrong language on the overlay.
* **A finished line is finished.** A failed translation that had already shown words is
  closed with a marker that it is cut short, so the viewer's hold timer runs; and it is
  neither remembered as context nor handed to a subtitle sink, because it is not a
  translation.
* **Behind is visible and bounded.** Past ``lag_queue_limit`` queued segments the language's
  readers are told it is delayed; past ``max_backlog`` the oldest are dropped, since a line
  that is many segments late is worth less than the live one behind it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from livesubs.events import SegmentState, Stage, SubtitleSegment, Timing
from livesubs.hub import Hub, Presence
from livesubs.languages import LANGUAGES
from livesubs.mt.base import MtContext, MtProvider
from livesubs.pipeline.health import Health
from livesubs.pipeline.lifecycle import DemandChanged, cancel_bounded
from livesubs.pipeline.records import Committed, PipelineConfig, SourceSegment
from livesubs.pipeline.stats import LanguageStats

__all__ = ["TargetWorker", "WorkerEnv"]

logger = logging.getLogger(__name__)

#: One immutable committed source segment, including its frozen language evidence.
Item = SourceSegment


@dataclass(slots=True)
class WorkerEnv:
    """What a worker needs from the pipeline that owns it, once the pipeline is running."""

    hub: Hub
    config: PipelineConfig
    health: Health
    now_ms: Callable[[], int]
    sink: Callable[[Committed], None] | None
    #: Whether paid work follows the audience. Ungated runs (simulations, tests) do not.
    gated: bool


@dataclass(slots=True)
class _Progress:
    """How far one segment's translation got, kept outside the request that may fail."""

    text: str = ""
    shown: bool = False


def _deadline(seconds: float | None):
    """``asyncio.timeout``, or nothing when no limit is set."""

    return asyncio.timeout(seconds) if seconds else contextlib.nullcontext()


class TargetWorker:
    """The translator for one target language and everything that is its alone."""

    def __init__(
        self,
        language: str,
        *,
        provider: MtProvider | None = None,
        factory: Callable[[], MtProvider] | None = None,
        source_language: str,
    ) -> None:
        if provider is None and factory is None:
            raise ValueError(f"no translator was given for {language!r}")
        self.language = language
        self.source_language = source_language
        self._provider = provider
        self._factory = factory

        self.queue: deque[Item] = deque()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._draining = False
        self._env: WorkerEnv | None = None
        self._presence: Presence | None = None

        #: ``rev`` starts at 0 for a new ``seq`` and only increases, per language.
        self.rev = 0
        self.published_seq = -1
        self.recent_src: list[str] = []
        self.recent_target: list[str] = []
        #: Whether this translator has been warmed for the current run.
        self.warmed = False
        #: The audience generation the running worker belongs to.
        self.epoch: int | None = None
        self.active = False
        #: Segments discarded because the translator could not keep up.
        self.shed = 0
        #: Translations that failed in a row.
        self.failures = 0
        #: Figures for the operator console. Written here, read nowhere else.
        self.stats = LanguageStats()

    # --- wiring ---------------------------------------------------------------------

    def bind(self, env: WorkerEnv, presence: Presence | None) -> None:
        self._env = env
        self._presence = presence

    @property
    def provider(self) -> MtProvider | None:
        return self._provider

    @property
    def name(self) -> str:
        return LANGUAGES[self.language].name if self.language in LANGUAGES else self.language

    def _build(self) -> MtProvider:
        """The translator, built on first use so an unwanted language costs nothing."""

        if self._provider is None:
            assert self._factory is not None
            self._provider = self._factory()
        return self._provider

    def demand_is_current(self) -> bool:
        """Whether a viewer of this language is here, and is the one this worker started for."""

        env = self._env
        if env is None:
            return False
        if not env.gated:
            return True
        presence = self._presence
        return presence is not None and presence.present and presence.generation == self.epoch

    # --- starting and stopping -------------------------------------------------------

    async def start(self, epoch: int | None) -> None:
        """Start ordered output without constructing or warming paid MT."""

        self._require_env()
        self.epoch = epoch
        try:
            self._check_demand()

            self._reset()
            self.active = True
            self._draining = False
            self._task = asyncio.create_task(
                self._loop(), name=f"livesubs:translate:{self.language}"
            )
            logger.info("%s translation started", self.language)
        except BaseException:
            self.active = False
            self.epoch = None
            raise

    def _check_demand(self) -> None:
        if not self.demand_is_current():
            raise DemandChanged

    async def stop(self) -> None:
        """Stop paid work and forget this language's state, leaving everything else alone."""

        self.active = False
        self.epoch = None
        task, self._task = self._task, None
        self._reset()
        if task is not None:
            await cancel_bounded(task)
        env = self._env
        if env is not None:
            # Only this language's cached line is dropped, so a viewer of another language
            # keeps reading the current line.
            env.hub.forget_segment_snapshot(self.language)
            env.health.clear_scope(self.language)

    def _reset(self) -> None:
        self.queue.clear()
        self._wake.clear()
        self.recent_src.clear()
        self.recent_target.clear()
        self.rev = 0
        self.published_seq = -1
        self.failures = 0

    async def drain(self, timeout: float) -> None:
        """Let the queue empty, then stop the worker. For shutdown: the last line is worth it."""

        task = self._task
        if task is None:
            return
        self._draining = True
        self._wake.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "%s translation did not drain within %.1fs; cancelling", self.language, timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s translator failed during shutdown", self.language)
        self.active = False
        self._task = None
        await cancel_bounded(task)

    # --- taking work ----------------------------------------------------------------

    def offer(self, item: Item | tuple[int, str, int, int]) -> None:
        """Queue a committed segment, if a viewer of this language is waiting for it.

        A worker's own flag is not enough: it can have retired without being cleared yet,
        and a stale audience means nobody is actually waiting. Checking here stops a queue
        growing for a language nobody is reading.
        """

        if not self.active or not self.demand_is_current():
            return
        # Low-level callers predating language evidence remain conservative MT callers.
        self.queue.append(item if isinstance(item, SourceSegment) else SourceSegment(*item))
        self._shed()
        self._wake.set()
        self.check_lag()

    def _shed(self) -> None:
        """Discard the oldest queued segments once this language is hopelessly behind.

        Only segments that have not started are discarded, so a line being translated is
        never cut off. The viewer misses a fragment instead of reading the whole lecture
        further and further behind the speaker, which is the failure it cannot recover from.
        """

        env = self._require_env()
        excess = len(self.queue) - env.config.max_backlog
        if excess <= 0:
            return
        for _ in range(excess):
            self.queue.popleft()
        self.shed += excess
        logger.warning(
            "%s translation is %d segments behind; dropped the %d oldest (%d in total)",
            self.language,
            env.config.max_backlog + excess,
            excess,
            self.shed,
        )

    def check_lag(self) -> None:
        """Tell this language's readers when it is behind, and when it has caught up."""

        env = self._require_env()
        if len(self.queue) >= env.config.lag_queue_limit:
            if not env.health.is_open("lag", scope=self.language):
                logger.warning(
                    "%s translation is %d segments behind", self.language, len(self.queue)
                )
            env.health.problem("lag", "translation is behind", scope=self.language)
        else:
            env.health.clear("lag", scope=self.language)

    # --- the worker -----------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            if self.queue:
                if not self.demand_is_current():
                    # The audience left, or was replaced, since this was queued. The
                    # supervisor will stop this worker; nothing is translated meanwhile.
                    self.queue.clear()
                    continue
                item = self.queue.popleft()
                if item.language in {"en", "de", "ar"} and item.language == self.language:
                    self._publish(
                        item.seq, SegmentState.FINAL, item.text,
                        src=item.text, audio_ms=item.end_ms,
                    )
                    self._remember(item.text, item.text)
                    self._emit(item.seq, item.start_ms, item.end_ms, item.text, item.text)
                else:
                    await self._translate(item.seq, item.text, item.start_ms, item.end_ms)
                self.check_lag()
                continue

            if self._draining:
                return

            # No await between the emptiness check and the clear, so an offer cannot be lost.
            self._wake.clear()
            await self._wake.wait()

    async def _translate(self, seq: int, src: str, start_ms: int, end_ms: int) -> None:
        """Translate one segment, publishing revisions as text arrives."""

        env = self._require_env()
        if not self.demand_is_current():
            return

        context = MtContext(
            source_context=list(self.recent_src),
            target_context=list(self.recent_target),
        )

        progress = _Progress()
        failed = False
        began = time.monotonic()

        for attempt in range(1 + max(0, env.config.mt_retries)):
            progress.text = ""
            try:
                self._check_demand()
                async with _deadline(env.config.mt_deadline_s):
                    provider = self._build()
                    if not self.warmed:
                        await provider.warm()
                        self._check_demand()
                        self.warmed = True
                    self._check_demand()
                    await self._stream(provider, seq, src, context, end_ms, progress)
                    self._check_demand()
                failed = False
                break
            except DemandChanged:
                return
            except TimeoutError:
                logger.warning(
                    "translation of segment %d for %s ran past its deadline", seq, self.language
                )
                failed = True
                break  # already too late to be worth waiting for again
            except Exception:
                logger.warning(
                    "translation of segment %d failed for %s (attempt %d)",
                    seq, self.language, attempt + 1, exc_info=True,
                )
                failed = True

        if failed or not progress.text.strip():
            # An empty result is a failure too. Falling back to the speaker's words would
            # put the wrong language on the overlay, which is worse than showing nothing.
            self.stats.failed += 1
            self._failed(seq, progress, end_ms)
            return

        self._succeeded()
        self._publish(seq, SegmentState.FINAL, progress.text, src=src, audio_ms=end_ms)
        self.stats.completed += 1
        self.stats.translate_ms.add((time.monotonic() - began) * 1000.0)
        self.stats.latency_ms.add(max(0, env.now_ms() - end_ms))
        self._remember(src, progress.text)
        self._emit(seq, start_ms, end_ms, src, progress.text)

    async def _stream(
        self,
        provider: MtProvider,
        seq: int,
        src: str,
        context: MtContext,
        end_ms: int,
        progress: _Progress,
    ) -> None:
        """Run one request, publishing drafts and recording how far it got."""

        env = self._require_env()
        chunks: list[str] = []
        interval = max(0, env.config.draft_interval_ms) / 1000.0
        last_draft = float("-inf")

        began = time.monotonic()
        async for chunk in provider.translate(src, context):
            self._check_demand()
            if chunk.text_delta:
                if not chunks:
                    self.stats.first_word_ms.add((time.monotonic() - began) * 1000.0)
                chunks.append(chunk.text_delta)
                progress.text = "".join(chunks)
                now = time.monotonic()
                # The first words go out at once - they are the latency a viewer feels -
                # and later drafts are spaced. Nothing is lost by skipping one: each
                # draft carries the whole text so far, and the final carries all of it.
                if not progress.shown or now - last_draft >= interval:
                    self._publish(
                        seq, SegmentState.DRAFT, progress.text, src=src, audio_ms=end_ms
                    )
                    last_draft = now
                    progress.shown = True
            if chunk.done:
                break

    def _failed(self, seq: int, progress: _Progress, end_ms: int) -> None:
        """Close the books on a segment that could not be translated.

        If words were already on screen the line is finished with a marker that it is cut
        short, so the viewer's hold timer runs and it retires; a draft that never finalises
        would sit there until something replaced it.
        """

        env = self._require_env()
        env.hub.publish_error(f"translation into {self.name} failed", Stage.MT)
        self.failures += 1
        if self.failures >= env.config.mt_failures_before_notice:
            env.health.problem("mt", "translation unavailable", scope=self.language)
        if progress.shown and progress.text.strip():
            self._publish(
                seq, SegmentState.FINAL, progress.text.rstrip() + " …", audio_ms=end_ms
            )

    def _succeeded(self) -> None:
        self.failures = 0
        self._require_env().health.clear("mt", scope=self.language)

    # --- publishing -----------------------------------------------------------------

    def _publish(
        self,
        seq: int,
        state: SegmentState,
        text: str,
        *,
        src: str | None = None,
        audio_ms: int,
    ) -> None:
        env = self._require_env()

        # ``rev`` starts at 0 for a new seq and only ever increases, which is the contract
        # the overlay's in-place update depends on. It is tracked per worker so two
        # languages never share a revision counter.
        if seq != self.published_seq:
            self.published_seq = seq
            self.rev = 0
        else:
            self.rev += 1

        env.hub.publish_segment(
            SubtitleSegment(
                seq=seq,
                rev=self.rev,
                state=state,
                language=self.language,
                text=text,
                src=src,
                timing=Timing(audio_ms=audio_ms, emit_ms=env.now_ms()),
            )
        )

    def _remember(self, src: str, text: str) -> None:
        """Keep the last few segments as translation context, for this language alone.

        A verb-final language is badly served by an isolated fragment, and a pronoun's
        gender can depend on the sentence before. Each language keeps its own target
        context, so one language's output can never appear as another's. Context goes in
        the user message, never the system prompt - see ``docs/05-providers.md`` on cache
        prefixes.
        """

        env = self._require_env()
        limit = max(0, env.config.segmenter.context_segments)
        if not limit:
            return

        self.recent_src.append(src)
        self.recent_target.append(text)
        del self.recent_src[:-limit]
        del self.recent_target[:-limit]

    def _emit(self, seq: int, start_ms: int, end_ms: int, src: str, text: str) -> None:
        """Hand the finished segment to the sink, if there is one.

        Wrapped for the same reason telemetry is: writing a transcript is an extra, and an
        extra must never be able to stop the lecture it is describing.
        """

        env = self._require_env()
        if env.sink is None:
            return
        try:
            env.sink(
                Committed(
                    seq=seq,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    src=src,
                    text=text,
                    language=self.language,
                )
            )
        except Exception:
            logger.exception("the segment sink failed; the lecture continues")

    def _require_env(self) -> WorkerEnv:
        if self._env is None:
            raise RuntimeError("the worker was used before the pipeline started")
        return self._env
