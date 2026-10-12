"""Canonical wire contract between the pipeline and the subtitle overlay.

This module is the single source of truth for the message schema described in
``docs/04-adapters.md``. If the two ever disagree, this file wins and the
document is wrong.

Rules that the overlay relies on:

* ``seq`` is monotonic within a session.
* ``rev`` starts at 0 for a new ``seq`` and only ever increases.
* A viewer REPLACES the displayed line when ``seq`` increases, and UPDATES it in
  place when ``rev`` increases on the same ``seq``.
* ``state == FINAL`` means no further revisions will arrive for that ``seq``, so
  the overlay may start its reading-time hold timer.
* ``src`` is the English source. The viewer page does NOT render it (German-only
  requirement); the operator console does, because a mistranslation cannot be
  diagnosed without it.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

EVENT_VERSION = 1


class SegmentState(str, Enum):
    """Lifecycle of a subtitle line."""

    DRAFT = "draft"
    """Machine output that may still be revised."""

    FINAL = "final"
    """Committed by the segmentation policy; no further revisions."""


class PipelineStatus(str, Enum):
    """Health of the pipeline, surfaced to viewers as a status pill."""

    LIVE = "live"
    DELAYED = "delayed"
    STOPPED = "stopped"


class Stage(str, Enum):
    """Which part of the pipeline an error came from."""

    CAPTURE = "capture"
    ASR = "asr"
    MT = "mt"
    HUB = "hub"
    SERVER = "server"


class WordTiming(BaseModel):
    """Word-level timing, where the ASR provider supplies it.

    Deepgram supplies these; OpenAI's live transcription models do not, so
    consumers must treat ``words`` as optional everywhere.
    """

    text: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Timing(BaseModel):
    """Monotonic millisecond timestamps used for latency accounting.

    ``render_ms`` is filled in by the overlay and echoed back, so that true
    end-to-end latency can be measured from the client's perspective rather than
    the server's. See ``docs/09-evaluation.md``.
    """

    audio_ms: int = Field(ge=0, description="End of the audio span that produced this text")
    emit_ms: int = Field(ge=0, description="Server pushed this revision")
    render_ms: int | None = Field(default=None, ge=0, description="Client rendered it")


class SubtitleSegment(BaseModel):
    """One subtitle line, at one revision.

    A segment belongs to exactly one target language. A German segment carries
    ``language="de"`` and its text in ``de``; an Arabic segment carries
    ``language="ar"`` and its text in ``ar``, with ``de`` left empty. The two
    streams are ordered independently and a subscriber only ever receives the
    languages its role is allowed to see, so the fields are never mixed on the
    wire.

    ``ar`` is ``None`` on a German segment, which keeps the added field additive:
    an older client that ignores ``language``/``ar`` still renders German.
    """

    seq: int = Field(ge=0)
    rev: int = Field(default=0, ge=0)
    state: SegmentState = SegmentState.DRAFT
    language: Literal["de", "ar"] = Field(
        default="de", description="Which target stream this segment belongs to"
    )
    de: str = Field(
        default="", description="German text; empty on an Arabic-only segment"
    )
    ar: str | None = Field(
        default=None, description="Arabic text; only present when language is 'ar'"
    )
    src: str | None = Field(default=None, description="English source; operator console only")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    words: list[WordTiming] | None = None
    timing: Timing


class HelloPayload(BaseModel):
    """Sent once on connect so a late joiner is never looking at a blank screen."""

    session_id: str
    session_name: str
    language: str = "de"
    version: int = EVENT_VERSION


class StatusPayload(BaseModel):
    status: PipelineStatus
    detail: str | None = None


class ErrorPayload(BaseModel):
    message: str
    stage: Stage


class HelloMessage(BaseModel):
    v: int = EVENT_VERSION
    type: Literal["hello"] = "hello"
    data: HelloPayload


class SegmentMessage(BaseModel):
    v: int = EVENT_VERSION
    type: Literal["segment"] = "segment"
    data: SubtitleSegment


class StatusMessage(BaseModel):
    v: int = EVENT_VERSION
    type: Literal["status"] = "status"
    data: StatusPayload


class ErrorMessage(BaseModel):
    v: int = EVENT_VERSION
    type: Literal["error"] = "error"
    data: ErrorPayload


WireMessage = Annotated[
    HelloMessage | SegmentMessage | StatusMessage | ErrorMessage,
    Field(discriminator="type"),
]
"""Any message the server may send to a viewer, discriminated on ``type``."""


def dumps(message: BaseModel) -> str:
    """Serialise an envelope once, to be written verbatim to every socket.

    Fan-out is deliberately serialise-once: payloads are tiny text and the cost
    is per-message JSON encoding, not throughput. See ``docs/07-delivery-qr.md``.
    """

    return message.model_dump_json(exclude_none=False)
