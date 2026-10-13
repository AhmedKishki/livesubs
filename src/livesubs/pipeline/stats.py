"""What the live pipeline reports about itself.

Kept apart from the pipeline's logic so that adding a figure is an edit here and a one-line
call where it happens. Nothing in this module is read by the pipeline to decide anything: it
is only ever written to and read for display, so a wrong figure can mislead an operator but
cannot change what a viewer sees.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from livesubs.ingest import FRAME_MS, FrameStats
from livesubs.metrics import Window

__all__ = ["LanguageStats", "PipelineStats"]


@dataclass(slots=True)
class LanguageStats:
    """One target language's translator, as the operator sees it."""

    #: Segments translated and published as final.
    completed: int = 0
    #: Segments given up on: the request failed, ran past its deadline or returned nothing.
    failed: int = 0
    #: Time from the end of the spoken words to the final translation going out. The figure
    #: a viewer experiences, including the wait for the segment to close and any queueing.
    latency_ms: Window = field(default_factory=Window)
    #: Time from the request starting to the first words of the translation arriving.
    first_word_ms: Window = field(default_factory=Window)
    #: Time the whole request took.
    translate_ms: Window = field(default_factory=Window)


@dataclass(slots=True)
class PipelineStats:
    """The shared half: capture, recognition and the segments handed to translators."""

    started: float = field(default_factory=time.monotonic)
    #: Segments committed from the recogniser's text, before any language translates them.
    committed: int = 0
    #: Times a recognition stream was lost while it was wanted.
    recogniser_losses: int = 0
    #: The latest levels, once the first frame has been seen.
    audio: FrameStats | None = None
    #: Seconds of audio the pipeline has consumed. The audio clock, not the wall clock.
    audio_s: float = 0.0

    def silence_s(self) -> float | None:
        """How long the input has been silent, from the frame counter. ``None`` before audio."""

        if self.audio is None:
            return None
        return round(self.audio.silence_streak * FRAME_MS / 1000.0, 1)
