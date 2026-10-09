"""Build an audio source from a one-line specification.

The command line takes a single ``--audio`` value rather than a flag per backend, so
adding a backend does not mean adding a flag. The syntax is deliberately tiny:

    synth               synthetic speech, the default
    file:PATH[@SPEED]   a WAV recording, replayed at SPEED (1.0 by default, 0 for
                        as fast as possible)
    pw:TARGET           a live PipeWire source, by ``node.name``

Backends are imported lazily, so a machine without PipeWire can still replay a file
and a machine with no recording can still run the synthetic source. Importing
everything eagerly would make the failure mode of one backend a failure mode of all
of them.
"""

from __future__ import annotations

from livesubs.audio.base import TARGET_RATE, AudioSource
from livesubs.audio.calibration import CalibrationConfig

__all__ = ["SYNTAX", "build_source"]

#: Shown by ``--help`` and quoted in error messages, so the two cannot drift.
SYNTAX = "synth | file:PATH[@SPEED] | pw:TARGET"


def build_source(
    spec: str,
    *,
    sample_rate: int = TARGET_RATE,
    frame_ms: int = 20,
    calibration: CalibrationConfig | None = None,
) -> AudioSource:
    """Instantiate the source named by ``spec``.

    Raises ``ValueError`` for anything unrecognised, listing the alternatives. A
    silently substituted source would be the worst outcome here: a demonstration of
    the live pipeline that is quietly replaying synthetic audio looks exactly like a
    working one.
    """

    kind, _, rest = spec.partition(":")

    match kind:
        case "synth":
            if rest:
                raise ValueError(f"synth takes no argument, got {rest!r}; expected {SYNTAX}")
            return _synthetic(sample_rate=sample_rate, frame_ms=frame_ms)

        case "file":
            if not rest:
                raise ValueError(f"file: needs a path; expected {SYNTAX}")
            return _wav(rest, sample_rate=sample_rate, frame_ms=frame_ms)

        case "pw":
            if not rest:
                raise ValueError(f"pw: needs a node.name; expected {SYNTAX}")
            return _pipewire(
                rest, sample_rate=sample_rate, frame_ms=frame_ms,
                calibration=calibration if calibration is not None else CalibrationConfig(),
            )

        case _:
            raise ValueError(f"unknown audio source {spec!r}; expected {SYNTAX}")


def _synthetic(*, sample_rate: int, frame_ms: int) -> AudioSource:
    from livesubs.audio.synthetic import SpeechLikeSource

    # Real time, always. A synthetic source running flat out would emit thousands of
    # segments a second and pin a core, which is a bug in a demo even though it
    # would be convenient in a test.
    return SpeechLikeSource(realtime=True, sample_rate=sample_rate, frame_ms=frame_ms)


def _wav(rest: str, *, sample_rate: int, frame_ms: int) -> AudioSource:
    from livesubs.audio.file_source import WavFileSource

    path, _, speed_text = rest.partition("@")
    if not path:
        raise ValueError(f"file: needs a path; expected {SYNTAX}")

    try:
        speed = float(speed_text) if speed_text else 1.0
    except ValueError:
        raise ValueError(f"file: speed must be a number, got {speed_text!r}") from None

    return WavFileSource(path, sample_rate=sample_rate, frame_ms=frame_ms, speed=speed)


def _pipewire(
    rest: str, *, sample_rate: int, frame_ms: int, calibration: CalibrationConfig
) -> AudioSource:
    from livesubs.audio.pw_record import PwRecordSource

    # Target validation is left on: refusing to start on a stale node.name is the
    # whole point, and it happens before the server binds a port.
    return PwRecordSource(
        rest, sample_rate=sample_rate, frame_ms=frame_ms, calibration=calibration
    )
