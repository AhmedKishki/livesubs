"""Tests for the segmentation policy.

These pin the behaviour that decides whether the product is usable: where segment
boundaries fall. See ``docs/03-segmentation.md``. Display pacing is the viewer's, and is
pinned by the shared fixtures in ``spec/pacing_vectors.json``.
"""

from __future__ import annotations

from livesubs.segmenter import (
    Decision,
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


# --- recognition activity as evidence of speech ----------------------------------------


def test_recognition_activity_keeps_a_run_open_when_the_vad_hears_nothing() -> None:
    """A quiet voice below the energy threshold is still being recognised.

    The VAD alone reads this as silence, so the run used to close at the floor with a
    fragment, or never open at all.
    """

    buf = _buffer()
    for now in range(100, 1000, 100):
        assert buf.observe(now, voice=False, activity=True) is Decision.HOLD


def test_a_run_closes_after_recognition_has_gone_quiet() -> None:
    buf = _buffer()
    buf.observe(1000, voice=False, activity=True)

    assert buf.observe(1200, voice=False) is Decision.HOLD
    assert buf.observe(1400, voice=False) is Decision.EMIT


def test_late_recognition_delays_the_pause_cut_until_it_catches_up() -> None:
    """The words just before a pause are recognised a moment after it.

    Cutting at the pause would leave them for the next segment as a stray fragment.
    """

    buf = _buffer()
    buf.observe(1000, voice=True)
    # Speech stopped at 1000. Words spoken just before it arrive at 1250.
    assert buf.observe(1250, voice=False, activity=True) is Decision.HOLD
    # 350 ms after the VAD went quiet would have been 1350; recognition moved it on.
    assert buf.observe(1400, voice=False) is Decision.HOLD
    assert buf.observe(1600, voice=False) is Decision.EMIT


def test_cue_end_is_the_last_voiced_frame_not_the_last_recognition() -> None:
    """A cue that ran on to the late result would sit on screen through the pause."""

    buf = _buffer()
    buf.observe(1000, voice=True)
    buf.observe(1250, voice=False, activity=True)

    assert buf.end_ms == 1000


def test_cue_end_falls_back_to_recognition_when_the_vad_never_heard_anything() -> None:
    buf = _buffer()
    buf.reset(500)
    buf.observe(900, voice=False, activity=True)
    buf.observe(1300, voice=False, activity=True)

    assert buf.end_ms == 1300


def test_reset_clears_what_the_last_run_heard() -> None:
    buf = _buffer()
    buf.observe(1000, voice=True, activity=True)
    buf.reset(1100)

    assert buf.last_activity_ms == 1100
    assert buf.voiced is False
    assert buf.observe(1500, voice=False) is Decision.EMIT
