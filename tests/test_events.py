"""Tests for the wire contract.

The overlay's revision handling is load-bearing (see ``docs/04-adapters.md``), so
these tests pin the semantics rather than the serialisation details.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from livesubs.events import (
    EVENT_VERSION,
    ErrorMessage,
    HelloMessage,
    HelloPayload,
    SegmentMessage,
    SegmentState,
    StatusMessage,
    SubtitleSegment,
    Timing,
    WireMessage,
    WordTiming,
    dumps,
)

WIRE_ADAPTER = TypeAdapter(WireMessage)


def _segment(seq: int = 1, rev: int = 0, state: SegmentState = SegmentState.DRAFT) -> SubtitleSegment:
    return SubtitleSegment(
        seq=seq,
        rev=rev,
        state=state,
        de="Die Amplitude nimmt mit der Zeit exponentiell ab.",
        src="The amplitude decreases exponentially over time.",
        timing=Timing(audio_ms=2000, emit_ms=2400),
    )


def test_version_is_one() -> None:
    assert EVENT_VERSION == 1


def test_segment_serialises_both_languages() -> None:
    payload = _segment()
    assert payload.de.startswith("Die Amplitude")
    # The English source must survive serialisation: the operator console needs
    # it to diagnose a bad translation, even though the viewer never shows it.
    assert payload.src is not None


def test_src_is_optional() -> None:
    payload = SubtitleSegment(seq=1, de="Nur Deutsch", timing=Timing(audio_ms=0, emit_ms=1))
    assert payload.src is None
    assert payload.rev == 0
    assert payload.state is SegmentState.DRAFT


def test_revision_semantics_replace_vs_update() -> None:
    """A new seq replaces the line; a new rev updates it in place."""

    first = _segment(seq=1, rev=0)
    same_line_newer = _segment(seq=1, rev=1)
    next_line = _segment(seq=2, rev=0)

    assert same_line_newer.seq == first.seq and same_line_newer.rev > first.rev
    assert next_line.seq > first.seq and next_line.rev == 0


def test_final_hint_lets_the_overlay_start_its_hold_timer() -> None:
    assert _segment(state=SegmentState.FINAL).state is SegmentState.FINAL


def test_timing_render_ms_is_filled_by_the_client() -> None:
    timing = Timing(audio_ms=0, emit_ms=100)
    assert timing.render_ms is None
    echoed = Timing(audio_ms=0, emit_ms=100, render_ms=1750)
    assert echoed.render_ms == 1750


def test_timing_rejects_negative_timestamps() -> None:
    with pytest.raises(ValidationError):
        Timing(audio_ms=-1, emit_ms=0)


def test_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        SubtitleSegment(seq=0, de="x", confidence=1.5, timing=Timing(audio_ms=0, emit_ms=0))


def test_word_timings_are_optional_because_providers_differ() -> None:
    """Deepgram supplies word timings; OpenAI live transcription does not."""

    payload = _segment()
    assert payload.words is None

    with_words = payload.model_copy(
        update={"words": [WordTiming(text="Die", start_ms=0, end_ms=120, confidence=0.9)]}
    )
    assert with_words.words is not None
    assert with_words.words[0].text == "Die"


def test_hello_announces_german_only_default() -> None:
    hello = HelloMessage(data=HelloPayload(session_id="s1", session_name="Physik I"))
    assert hello.data.language == "de"
    assert hello.data.version == EVENT_VERSION


def test_discriminated_union_parses_each_type() -> None:
    messages = [
        HelloMessage(data=HelloPayload(session_id="s1", session_name="n")),
        SegmentMessage(data=_segment()),
        StatusMessage.model_validate({"type": "status", "data": {"status": "delayed"}}),
        ErrorMessage.model_validate({"type": "error", "data": {"message": "x", "stage": "asr"}}),
    ]

    for message in messages:
        parsed = WIRE_ADAPTER.validate_json(dumps(message))
        assert type(parsed) is type(message)


def test_unknown_type_is_rejected_rather_than_ignored() -> None:
    with pytest.raises(ValidationError):
        WIRE_ADAPTER.validate_json('{"type": "nonsense", "data": {}}')


def test_dumps_keeps_explicit_nulls() -> None:
    """The overlay can distinguish 'no confidence' from 'field absent'."""

    payload = _segment()
    assert '"confidence":null' in dumps(payload).replace(" ", "")
