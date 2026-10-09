"""Tests for audio conditioning, the noise floor and the voice-activity decision.

``Ingest`` is pure, and it is also the pipeline's clock, so it is the one place
where a mistake is both easy to make and expensive. A floor that never adapts
transcribes the room's hum for the whole lecture; one that adapts too eagerly lets
a speaker raise the floor behind themselves and gets cut off mid-sentence.
"""

from __future__ import annotations

import numpy as np

from livesubs.audio.synthetic import rms_amplitude_for_dbfs
from livesubs.ingest import FRAME_MS, Ingest, IngestConfig

SR = 16_000
FRAME_SAMPLES = SR * FRAME_MS // 1000


def pcm_of(dbfs: float, samples: int = FRAME_SAMPLES, *, dc: float = 0.0) -> bytes:
    """An alternating square wave whose RMS is exactly ``dbfs``.

    A square wave rather than a sine, so the RMS is simply the amplitude and the
    assertions can be about the dBFS conversion instead of about a sine's crest
    factor.
    """

    level = rms_amplitude_for_dbfs(dbfs)
    pattern = np.array([level, -level], dtype=np.float64)
    values = np.resize(pattern, samples) + dc
    return np.clip(values, -32768, 32767).astype("<i2").tobytes()


def characterise_room(dbfs: float = -55.0, frames: int = 50) -> Ingest:
    """An ingest that has already learned a room at ``dbfs``."""

    ingest = Ingest()
    for _ in range(frames):
        ingest.process(pcm_of(dbfs))
    return ingest

# --- the clock -----------------------------------------------------------------


def test_the_clock_comes_from_samples_not_wall_time() -> None:
    """Frame arrival is the pipeline's only clock, so it must be deterministic.

    This is what makes a file replayed at 10x behave exactly like a live device,
    and therefore what makes the audio path testable at all.
    """

    ingest = Ingest()
    for index in range(1, 51):
        stats = ingest.process(pcm_of(-70.0))
        assert stats.now_ms == index * FRAME_MS


def test_empty_input_does_not_advance_the_clock() -> None:
    ingest = Ingest()
    stats = ingest.process(b"")
    assert stats.now_ms == 0
    assert stats.speech is False


def test_a_truncated_frame_is_tolerated() -> None:
    """A short read is normal at the end of a device stream, so it must not raise."""

    ingest = Ingest()
    stats = ingest.process(pcm_of(-60.0)[:-1])       # 319 samples, not 320
    assert stats.now_ms == 19


# --- the noise floor -----------------------------------------------------------


def test_a_quiet_room_stops_reading_as_speech_once_it_is_characterised() -> None:
    """The floor is the only thing separating "the room" from "the speaker"."""

    ingest = characterise_room(-55.0)
    assert -60.0 < ingest.noise_floor_dbfs < -50.0
    assert ingest.process(pcm_of(-55.0)).speech is False


def test_speech_above_the_floor_is_detected() -> None:
    ingest = characterise_room(-55.0)
    assert ingest.process(pcm_of(-20.0)).speech is True


def test_the_floor_falls_fast_and_rises_slowly() -> None:
    """The asymmetry is the whole point: a loud moment is speech, not room tone.

    If the floor learned speech as quickly as it learns silence, a speaker would
    raise it behind themselves and be cut off after a sentence or two.
    """

    ingest = characterise_room(-60.0)
    before = ingest.noise_floor_dbfs

    ingest.process(pcm_of(0.0))                      # one loud frame
    assert ingest.noise_floor_dbfs - before < 1.0


def test_a_quiet_moment_is_learned_immediately() -> None:
    """The other half of the asymmetry, and why a room change is survivable."""

    ingest = characterise_room(-40.0)
    ingest.process(pcm_of(-80.0))
    assert ingest.noise_floor_dbfs <= -80.0


def test_the_floor_is_capped_so_a_loud_room_cannot_swallow_speech() -> None:
    """Sustained loud input keeps reading as speech, which is the safe direction.

    A spurious segment costs a glance at the screen; a missed one costs the whole
    lecture, so the failure modes are not symmetric and the cap leans towards
    producing text.
    """

    ingest = Ingest()
    stats = None
    for _ in range(200):
        stats = ingest.process(pcm_of(-30.0))

    assert stats is not None
    assert stats.noise_floor_dbfs == IngestConfig().max_floor_dbfs
    assert stats.speech is True

# --- conditioning --------------------------------------------------------------


def test_a_dc_offset_is_removed_within_one_frame() -> None:
    """A cheap USB input with a static bias must not read as continuous speech."""

    ingest = Ingest()
    stats = ingest.process(pcm_of(-80.0, dc=8000.0))

    # Without DC removal this measures about -12 dBFS, which would read as speech
    # on every frame for the entire lecture.
    assert stats.rms_dbfs < -70.0


def test_clipping_is_detected() -> None:
    ingest = Ingest()
    stats = ingest.process(pcm_of(6.0))              # above full scale

    assert stats.clipping is True
    assert stats.clipping_fraction > 0.9


def test_a_normal_level_is_not_reported_as_clipping() -> None:
    ingest = Ingest()
    assert ingest.process(pcm_of(-20.0)).clipping is False


def test_dc_removal_does_not_hide_original_sample_clipping() -> None:
    samples = np.resize(np.array([32767, 20000], dtype="<i2"), FRAME_SAMPLES)
    stats = Ingest().process(samples.tobytes())
    assert stats.clipping is True
    assert stats.clipping_fraction == 0.5


def test_the_silence_streak_counts_up_and_resets() -> None:
    """This is what the watchdog polls to tell a dead microphone from a quiet room."""

    ingest = characterise_room(-55.0)

    first = ingest.process(pcm_of(-75.0))
    second = ingest.process(pcm_of(-75.0))
    assert second.silence_streak == first.silence_streak + 1

    loud = ingest.process(pcm_of(-20.0))
    assert loud.speech is True
    assert loud.silence_streak == 0


def test_warm_up_costs_at_most_one_frame_of_phantom_speech() -> None:
    """The floor starts optimistic, so the first frame of a loud room reads as speech.

    That is bounded rather than eliminated, and it is safe because a single 20 ms
    frame cannot produce a subtitle: ``min_segment_ms`` is twenty times longer. A
    real fix would mean adding a pre-roll buffer, which is not worth the latency.
    """

    ingest = Ingest()
    speech_frames = sum(1 for _ in range(100) if ingest.process(pcm_of(-55.0)).speech)

    assert speech_frames <= 1


def test_reset_forgets_everything() -> None:
    ingest = characterise_room(-55.0)
    ingest.reset()

    assert ingest.total_ms == 0
    assert ingest.noise_floor_dbfs == IngestConfig().initial_floor_dbfs


# --- the absolute speech gate ----------------------------------------------------


def test_a_very_quiet_room_never_reads_as_speech() -> None:
    """The reason the absolute gate exists.

    An input can be live and still sit tens of dB below anything worth transcribing. With
    only a relative margin the floor settles onto that noise, and the margin then measures
    noise *fluctuation* rather than speech, so a room with nothing happening in it reads
    as continuous talking — and on the live pipeline that is an endless bill as well as
    endless subtitles.
    """

    ingest = Ingest()
    stats = None
    for _ in range(500):                              # ten seconds of a very quiet room
        stats = ingest.process(pcm_of(-96.0))

    assert stats is not None
    assert stats.speech is False
    assert stats.silence_streak == 500


def test_the_absolute_gate_admits_a_quiet_but_genuine_voice() -> None:
    """It must not be so cautious that it rejects real speech in a quiet room."""

    ingest = characterise_room(-80.0)

    assert ingest.process(pcm_of(-40.0)).speech is True


def test_the_gate_is_absolute_rather_than_a_higher_margin() -> None:
    """Raising the relative margin could not have fixed it: the floor tracks the noise.

    However large the margin, a floor that has settled onto the noise leaves the margin
    measuring that noise's own swing. Only a floor-independent threshold helps.
    """

    ingest = Ingest()
    for _ in range(500):
        ingest.process(pcm_of(-96.0))

    # The noise is far below the threshold even though the floor sits right on it.
    assert ingest.noise_floor_dbfs < -90.0
    assert ingest.process(pcm_of(-96.0)).speech is False
