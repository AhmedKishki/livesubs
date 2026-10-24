"""Tests for the wire contract.

The overlay's revision handling is load-bearing (see ``docs/04-adapters.md``), so
these tests pin the semantics rather than the serialisation details.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from livesubs.events import (
    EVENT_VERSION,
    AckMessage,
    ClientMessage,
    ErrorMessage,
    HelloMessage,
    HelloPayload,
    PingMessage,
    PongMessage,
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
CLIENT_ADAPTER = TypeAdapter(ClientMessage)


def _segment(seq: int = 1, rev: int = 0, state: SegmentState = SegmentState.DRAFT) -> SubtitleSegment:
    return SubtitleSegment(
        seq=seq,
        rev=rev,
        state=state,
        language="de",
        text="Die Amplitude nimmt mit der Zeit exponentiell ab.",
        src="The amplitude decreases exponentially over time.",
        timing=Timing(audio_ms=2000, emit_ms=2400),
    )


def test_the_version_marks_the_language_neutral_format() -> None:
    """Version 2 replaced the `de` and `ar` fields with `language` and `text`."""

    assert EVENT_VERSION == 2


def test_a_segment_carries_its_text_and_the_language_it_is_in() -> None:
    payload = _segment()
    assert payload.language == "de"
    assert payload.text.startswith("Die Amplitude")
    # The speaker's own words must survive serialisation: the operator console needs
    # them to diagnose a bad translation, even though the viewer never shows them.
    assert payload.src is not None


def test_no_field_belongs_to_one_language() -> None:
    """Adding a target language must change no message."""

    fields = set(SubtitleSegment.model_fields)

    assert fields.isdisjoint({"de", "ar", "en"})
    assert {"language", "text"} <= fields


def test_a_segment_must_name_its_language() -> None:
    with pytest.raises(ValidationError):
        SubtitleSegment(seq=1, text="x", timing=Timing(audio_ms=0, emit_ms=1))  # type: ignore[call-arg]


def test_src_is_optional() -> None:
    payload = SubtitleSegment(
        seq=1, language="en", text="Only the translation", timing=Timing(audio_ms=0, emit_ms=1)
    )
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
        SubtitleSegment(
            seq=0, language="de", text="x", confidence=1.5, timing=Timing(audio_ms=0, emit_ms=0)
        )


def test_word_timings_are_optional_because_providers_differ() -> None:
    """Deepgram supplies word timings; OpenAI live transcription does not."""

    payload = _segment()
    assert payload.words is None

    with_words = payload.model_copy(
        update={"words": [WordTiming(text="Die", start_ms=0, end_ms=120, confidence=0.9)]}
    )
    assert with_words.words is not None
    assert with_words.words[0].text == "Die"


def test_hello_names_the_speaker_and_the_languages_on_offer() -> None:
    hello = HelloMessage(
        data=HelloPayload(
            session_id="s1", session_name="Physik I", source="en", targets=["de", "ar"]
        )
    )

    assert hello.data.source == "en"
    assert hello.data.targets == ["de", "ar"]
    assert hello.data.version == EVENT_VERSION
    # Filled in per connection, so the shared copy does not claim a viewer's language.
    assert hello.data.language is None


def test_a_status_may_be_about_one_language_only() -> None:
    everyone = StatusMessage.model_validate({"type": "status", "data": {"status": "delayed"}})
    german = StatusMessage.model_validate(
        {"type": "status", "data": {"status": "delayed", "language": "de"}}
    )

    assert everyone.data.language is None
    assert german.data.language == "de"


def test_discriminated_union_parses_each_type() -> None:
    messages = [
        HelloMessage(
            data=HelloPayload(session_id="s1", session_name="n", source="en", targets=["de"])
        ),
        SegmentMessage(data=_segment()),
        PingMessage(),
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


def test_the_heartbeat_is_part_of_the_contract() -> None:
    assert dumps(PingMessage()).replace(" ", "") == f'{{"v":{EVENT_VERSION},"type":"ping"}}'


def test_what_a_viewer_may_send_is_part_of_the_contract() -> None:
    assert isinstance(CLIENT_ADAPTER.validate_python({"type": "pong"}), PongMessage)

    ack = CLIENT_ADAPTER.validate_python(
        {"type": "ack", "seq": 4, "rev": 2, "rendered_in_ms": 9, "dropped": 1}
    )
    assert isinstance(ack, AckMessage)
    assert (ack.seq, ack.rev, ack.dropped) == (4, 2, 1)

    with pytest.raises(ValidationError):
        CLIENT_ADAPTER.validate_python({"type": "hello"})
def test_optional_word_language_preserves_evidence_without_version_bump():
    from livesubs.events import EVENT_VERSION, WordTiming

    assert EVENT_VERSION == 2
    word = WordTiming(text="hi", start_ms=0, end_ms=1, language="fr")
    assert word.model_dump()["language"] == "fr"
    assert WordTiming(text="hi", start_ms=0, end_ms=1).language is None
