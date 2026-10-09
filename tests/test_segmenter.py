"""Tests for the segmentation and pacing policy.

These pin the two behaviours that decide whether the product is usable:
segment boundaries, and the backlog catch-up rule that stops lag from growing
without bound. See ``docs/03-segmentation.md``.
"""

from __future__ import annotations

from livesubs.segmenter import (
    Decision,
    Pacing,
    SegmentBuffer,
    SegmenterConfig,
    hold_ms,
)

# --- reading-time hold -------------------------------------------------------


def test_hold_is_clamped_at_both_ends() -> None:
    assert hold_ms(0) == 700          # floor
    assert hold_ms(-5) == 700         # defensive
    assert hold_ms(10_000) == 6000    # ceiling


def test_hold_grows_with_length() -> None:
    assert hold_ms(20) < hold_ms(60) < hold_ms(100)


def test_hold_is_monotonic_and_bounded_for_realistic_lengths() -> None:
    previous = 0
    for chars in range(0, 300, 10):
        value = hold_ms(chars)
        assert value >= previous
        assert 700 <= value <= 6000
        previous = value


# --- segment boundaries ------------------------------------------------------


def _buffer(cfg: SegmenterConfig | None = None) -> SegmentBuffer:
    return SegmentBuffer(cfg=cfg or SegmenterConfig(), started_ms=0, last_voice_ms=0)


def test_holds_while_speech_continues_below_the_cap() -> None:
    buf = _buffer()
    assert buf.observe(now_ms=1000, voice=True) is Decision.HOLD


def test_emits_on_trailing_silence() -> None:
    buf = _buffer()
    # Voice up to 1000 ms, then silence long enough to close the segment.
    buf.observe(now_ms=1000, voice=True)
    assert buf.observe(now_ms=1200, voice=False) is Decision.HOLD      # under endpoint
    assert buf.observe(now_ms=1400, voice=False) is Decision.EMIT      # 400 ms silence


def test_emits_at_the_cap_even_without_a_pause() -> None:
    """A speaker who never pauses must still get subtitles."""

    buf = _buffer(SegmenterConfig(max_segment_ms=2000))
    assert buf.observe(now_ms=1500, voice=True) is Decision.HOLD
    assert buf.observe(now_ms=2000, voice=True) is Decision.EMIT


def test_does_not_emit_a_too_short_fragment() -> None:
    """Fragments below min_segment_ms translate badly, so silence is ignored."""

    buf = _buffer(SegmenterConfig(min_segment_ms=400, endpoint_silence_ms=100))
    assert buf.observe(now_ms=100, voice=True) is Decision.HOLD
    assert buf.observe(now_ms=300, voice=False) is Decision.HOLD


def test_cap_wins_over_the_minimum() -> None:
    buf = _buffer(SegmenterConfig(min_segment_ms=400, max_segment_ms=500))
    assert buf.observe(now_ms=500, voice=True) is Decision.EMIT


def test_default_cap_is_two_seconds() -> None:
    """The tunable requirement: emit after ~2 s of continuous speech."""

    assert SegmenterConfig().max_segment_ms == 2000


# --- backlog catch-up --------------------------------------------------------


def test_advances_immediately_when_nothing_is_displayed() -> None:
    pacing = Pacing(cfg=SegmenterConfig())
    pacing.visible_ms = hold_ms(40)          # well past the early-advance point
    assert pacing.on_segment_ready(current_chars=40) is True
    assert pacing.dropped == 0


def test_drops_a_pending_segment_that_arrived_too_early() -> None:
    """Dropping is correct behaviour: a lost half-sentence beats growing lag."""

    pacing = Pacing(cfg=SegmenterConfig())
    pacing.visible_ms = 100                  # barely shown
    assert pacing.on_segment_ready(current_chars=80) is False
    assert pacing.dropped == 1


def test_early_advance_threshold_is_sixty_percent_of_hold() -> None:
    pacing = Pacing(cfg=SegmenterConfig())
    required = hold_ms(50)

    pacing.visible_ms = int(required * 0.59)
    assert pacing.should_advance(current_chars=50) is False

    pacing.visible_ms = int(required * 0.61)
    assert pacing.should_advance(current_chars=50) is True


def test_dropped_counter_accumulates_for_the_operator_console() -> None:
    """Pacing behaviour must be auditable during rehearsal, not inferred."""

    pacing = Pacing(cfg=SegmenterConfig())
    for _ in range(5):
        pacing.visible_ms = 0
        pacing.on_segment_ready(current_chars=80)
    assert pacing.dropped == 5


def test_a_fast_speaker_never_grows_the_queue() -> None:
    """Simulate a speaker outrunning reading speed.

    Whatever happens, at most one segment may be in flight, so lag cannot
    accumulate over a long lecture.
    """

    pacing = Pacing(cfg=SegmenterConfig())
    in_flight = 0

    for _ in range(200):
        pacing.visible_ms += 2000          # a new segment every 2 s
        in_flight += 1
        if pacing.on_segment_ready(current_chars=120):
            in_flight = 0

        assert in_flight <= 1
        assert pacing.dropped >= 0


# --- provider endpointing vs our own VAD -------------------------------------


def test_provider_endpointing_closes_a_segment_when_trusted() -> None:
    buf = _buffer()                                    # trusted by default
    assert buf.observe(1000, voice=True, speech_final=True) is Decision.EMIT


def test_provider_endpointing_is_ignored_when_not_trusted() -> None:
    buf = _buffer(SegmenterConfig(trust_provider_endpointing=False))
    assert buf.observe(1000, voice=True, speech_final=True) is Decision.HOLD


def test_vad_closes_a_segment_when_endpointing_is_untrusted() -> None:
    buf = _buffer(SegmenterConfig(trust_provider_endpointing=False))
    buf.observe(1000, voice=True)
    assert buf.observe(1400, voice=False) is Decision.EMIT


def test_vad_is_a_backstop_even_when_endpointing_is_trusted() -> None:
    """A provider that never reports an endpoint must not leave everything to the cap."""

    buf = _buffer()                                    # trusted
    buf.observe(1000, voice=True)
    assert buf.observe(1400, voice=False, speech_final=False) is Decision.EMIT


def test_endpointing_cannot_close_a_segment_below_the_floor() -> None:
    """A 100 ms utterance is too short to translate, whatever the provider says."""

    buf = _buffer()
    assert buf.observe(100, voice=True, speech_final=True) is Decision.HOLD


def test_cap_beats_the_floor_even_below_the_minimum() -> None:
    buf = _buffer(SegmenterConfig(min_segment_ms=1500, max_segment_ms=2000))
    assert buf.observe(2000, voice=True) is Decision.EMIT


def test_reset_restarts_both_clocks() -> None:
    buf = _buffer()
    buf.observe(1000, voice=True)
    buf.reset(1050)

    assert buf.started_ms == 1050
    assert buf.last_voice_ms == 1050
    # No history leaks across the boundary: a fresh run needs a fresh pause.
    assert buf.observe(1100, voice=False) is Decision.HOLD


def test_double_endpointing_cannot_emit_twice_for_one_run() -> None:
    """The failure mode docs/03-segmentation.md warns about.

    Once EMIT is acted on and the buffer reset, the same pause must not produce a
    second emission, or one sentence becomes two bad fragments.
    """

    buf = _buffer()
    buf.observe(1000, voice=True)

    assert buf.observe(1400, voice=False) is Decision.EMIT
    buf.reset(1400)

    assert buf.observe(1400, voice=False) is Decision.HOLD
    assert buf.observe(1500, voice=False) is Decision.HOLD      # still below the floor


# --- no duplicate wire type --------------------------------------------------


def test_segmenter_does_not_shadow_the_wire_segment_state() -> None:
    """``events.SegmentState`` must be the only ``SegmentState`` in the package.

    The segmenter previously defined its own unused ``SegmentState`` with
    different members (OPEN/HOLDING vs DRAFT/FINAL). Two meanings for one name in
    one package is a footgun, so the dead one was removed.
    """

    from livesubs import segmenter

    assert not hasattr(segmenter, "SegmentState")
    assert "SegmentState" not in segmenter.__all__
