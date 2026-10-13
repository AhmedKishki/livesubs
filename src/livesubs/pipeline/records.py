"""The pipeline's settings and the record it hands to a subtitle sink."""

from __future__ import annotations

from dataclasses import dataclass, field

from livesubs.asr.base import AsrConfig
from livesubs.ingest import IngestConfig
from livesubs.segmenter import SegmenterConfig

__all__ = ["Committed", "PipelineConfig"]


@dataclass(frozen=True, slots=True)
class Committed:
    """A finalised segment in one target language, with the audio span it came from.

    The speaker's own words and the translation, and both timings, because that is
    everything needed to write a transcript or a subtitle file, and the audio clock is the
    only clock that lines up with the archived recording.

    ``start_ms`` is where speech resumed and ``end_ms`` is the last voiced frame, not
    the moment the segment closed. The segmenter closes a segment a few hundred
    milliseconds after speech stops, and a cue running to the close would hold the
    previous line on screen through the pause - exactly the gap a viewer uses to look
    up from the screen.

    One record is made per target language that translated the segment, all with the same
    ``seq`` and ``src``. A subtitle recorder pairs a translation with what was actually
    said by that, and uses ``seq`` to write the speaker's words once.
    """

    seq: int
    start_ms: int
    end_ms: int
    #: What the speaker said, in their own language.
    src: str
    #: The translation.
    text: str
    #: The language ``text`` is in.
    language: str


@dataclass(slots=True)
class PipelineConfig:
    """Everything tunable about the live path."""

    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    #: How often levels are reported to the operator, in audio time.
    telemetry_interval_ms: int = 1000
    #: How often the recogniser's own health is checked, in audio time. The event stream
    #: ending is the main signal that it died; this is the second line of defence.
    health_check_ms: int = 1000
    #: Queued finals at which the pipeline admits it is behind. Three queued segments is
    #: already several seconds of lag at the usual 2 s cadence.
    lag_queue_limit: int = 3
    #: Queued finals at which the oldest are discarded. A subtitle that is several segments
    #: late is worth less than the live one behind it, and a translator that is slower than
    #: the speaker would otherwise fall further behind for the rest of the lecture.
    max_backlog: int = 6
    #: Shortest gap between draft revisions of one line, in milliseconds. The first words
    #: are published at once and the final always is; the drafts between are coalesced,
    #: because a model streams a token at a time and every draft goes to every phone.
    draft_interval_ms: int = 120
    #: Whole-request limit for one segment's translation, in seconds. The adapter's own
    #: timeout covers a stalled connection; this covers a stream that trickles for ever.
    mt_deadline_s: float | None = 30.0
    #: Extra attempts after a translation fails quickly. A timeout is never retried: the
    #: segment is already too late to be worth a second wait.
    mt_retries: int = 1
    #: Translation failures in a row before the language's readers are told it is down.
    mt_failures_before_notice: int = 2
