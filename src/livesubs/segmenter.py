"""The segmentation and pacing policy - the heart of the application.

Specified in ``docs/03-segmentation.md`` and mirrored by the overlay's
``web/static/pacing.js``. Model quality sets the ceiling; this policy decides
whether the result is READABLE, so the rules here are deliberately explicit and
unit-tested rather than buried in a websocket handler.

Nothing in this module performs I/O. It is pure decision logic.

``hold_ms`` exists in two places, here and in the overlay, because the server
cannot know a viewer's reading speed or font size. A pytest generates
``spec/pacing_vectors.json`` from this implementation and a ``node --test`` file
asserts the JavaScript reproduces it, so the two cannot silently diverge.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "Decision",
    "SegmentBuffer",
    "SegmenterConfig",
    "hold_ms",
]


class Decision(str, Enum):
    """What the segmenter wants done with the buffered audio."""

    HOLD = "hold"
    """Keep accumulating; the current speech run is still open."""

    EMIT = "emit"
    """Close the segment now and translate it."""


@dataclass(slots=True)
class SegmenterConfig:
    """Tunable segmentation policy. Defaults are the recommended starting point.

    See ``docs/03-segmentation.md`` for why ``max_segment_ms`` defaults to 2000:
    a 2 s segment is roughly 1.5-2 s of reading at 200 wpm, so segment cadence
    and pipeline latency are matched and lag stays STABLE rather than growing.
    Lowering it damages German, because clause-final verbs need the clause.
    """

    #: Trailing silence that closes a segment.
    endpoint_silence_ms: int = 350
    #: Hard ceiling during unbroken speech. The tunable "2 seconds of talking".
    max_segment_ms: int = 2000
    #: Never emit fragments shorter than this; they translate badly.
    min_segment_ms: int = 400
    #: Previous source sentences sent to MT as context, for German word order.
    context_segments: int = 2
    #: Prefer the ASR provider's own endpointing over our VAD for pauses.
    #:
    #: ``True`` (default) trusts the provider, which has the acoustics and the
    #: language model. ``False`` ignores it and drives pauses purely from our own
    #: energy VAD, for when a provider's endpointing proves too slow or too
    #: twitchy. See the double-endpointing note on ``SegmentBuffer.observe``.
    trust_provider_endpointing: bool = True


def hold_ms(chars: int) -> int:
    """Minimum time a segment with ``chars`` characters should stay on screen.

    Reading speed, not generation speed, is the binding constraint. Mirrored by
    ``holdMs`` in ``web/static/pacing.js``.

    Non-integer input is truncated toward zero, matching JavaScript's
    ``Math.trunc``, so the two implementations agree for every input rather than
    only for the integers we happen to pass in Python. ``spec/pacing_vectors.json``
    pins this.
    """

    count = max(0, int(chars))
    return max(700, min(6000, 55 * count + 400))


@dataclass(slots=True)
class SegmentBuffer:
    """Accumulates one speech run and decides when it is finished.

    Three signals can close a segment, and they are reconciled here in one place
    so that double-endpointing is impossible by construction rather than by luck
    (see ``docs/03-segmentation.md``):

    1. **the hard cap** (``max_segment_ms``) - always wins, so a speaker who never
       pauses still gets subtitles;
    2. **the provider's endpointing** (``speech_final``) - preferred when
       ``trust_provider_endpointing``, because the provider has both the acoustics
       and a language model;
    3. **our own VAD silence** - the fallback, and the safety net when a provider's
       endpointing is absent or twitchy.

    The caller must call :meth:`reset` after acting on ``EMIT``, and must only
    publish a segment when text was actually accumulated.
    """

    cfg: SegmenterConfig
    started_ms: int
    last_voice_ms: int
    #: When the recogniser last produced new text. Evidence that someone is speaking
    #: that does not depend on the energy VAD, which a quiet voice can fall below.
    last_activity_ms: int = 0
    #: Whether the energy VAD heard speech during this run.
    voiced: bool = False

    def observe(
        self,
        now_ms: int,
        *,
        voice: bool,
        speech_final: bool = False,
        activity: bool = False,
    ) -> Decision:
        """Feed the current VAD state and endpointing hint; decide whether to emit.

        ``voice`` is our energy VAD for this frame. ``speech_final`` is the provider's
        own end-of-utterance signal, where it supplies one. ``activity`` says the
        recogniser produced new text during this frame.

        Activity counts as speech when deciding that the speaker has paused, for two
        reasons. A voice below the VAD threshold is still recognised, and the VAD alone
        read it as silence. And the last words before a pause are recognised a moment
        after it, so closing at the VAD's pause left them to the next segment as a stray
        fragment.
        """

        if voice:
            self.last_voice_ms = now_ms
            self.voiced = True
        if activity:
            self.last_activity_ms = now_ms

        elapsed = now_ms - self.started_ms

        # 1. Hard ceiling first: an unbroken monologue must still produce output,
        #    even if the result is shorter than the normal floor.
        if elapsed >= self.cfg.max_segment_ms:
            return Decision.EMIT

        # 2. Floor: fragments below min_segment_ms translate badly, so a pause
        #    alone is not enough to close a very short run. It will be closed by
        #    the cap, or merged with the words that follow.
        if elapsed < self.cfg.min_segment_ms:
            return Decision.HOLD

        # 3. Provider endpointing, when trusted.
        if self.cfg.trust_provider_endpointing and speech_final:
            return Decision.EMIT

        # 4. Our own VAD is always the backstop, whether or not we trust the
        #    provider. Without it, a provider that never reports an endpoint would
        #    leave every segment to the cap.
        quiet_since = max(self.last_voice_ms, self.last_activity_ms)
        if now_ms - quiet_since >= self.cfg.endpoint_silence_ms:
            return Decision.EMIT

        return Decision.HOLD

    @property
    def end_ms(self) -> int:
        """Where the speech in this run ended, for the cue that describes it.

        The last frame the VAD called voiced, so a cue does not run on through the
        pause and hold the previous line up while a viewer looks away. When the VAD
        never heard anything in the run, the recogniser's last result is the only
        evidence there is.
        """

        if self.voiced:
            return max(self.last_voice_ms, self.started_ms)
        return max(self.last_activity_ms, self.started_ms)

    def reset(self, now_ms: int) -> None:
        """Start a new speech run from ``now_ms``.

        Called by the caller after acting on ``EMIT``, and after a long silence so
        that the next run's clock starts when speech resumes rather than when the
        previous segment ended. Every clock restarts, so silence is measured from
        now rather than from the previous run's end.
        """

        self.started_ms = now_ms
        self.last_voice_ms = now_ms
        self.last_activity_ms = now_ms
        self.voiced = False
