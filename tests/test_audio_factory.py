"""Tests for the audio-source factory.

The factory is the only thing standing between a typo on the command line and a
demonstration that looks like it is working. Silently substituting a source would be
the worst possible behaviour here, so most of these tests are about refusing
correctly.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from livesubs.audio import pw_record
from livesubs.audio.base import AudioSource
from livesubs.audio.calibration import CalibrationConfig
from livesubs.audio.factory import SYNTAX, build_source


def write_wav(path: Path, samples: np.ndarray, *, rate: int = 16_000) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.astype("<i2").tobytes())
    return path


# --- the three kinds ------------------------------------------------------------


def test_synth_is_the_credential_and_hardware_free_default() -> None:
    source = build_source("synth")

    assert isinstance(source, AudioSource)
    assert source.alive is True


def test_a_file_spec_builds_a_replay_source(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "tone.wav", np.arange(1600, dtype="<i2"))
    source = build_source(f"file:{path}")

    assert isinstance(source, AudioSource)
    assert source.duration_ms == 100  # type: ignore[attr-defined]
    assert source.speed == 1.0        # type: ignore[attr-defined]


@pytest.mark.parametrize("text,expected", [("0", 0.0), ("2", 2.0), ("0.5", 0.5)])
def test_a_file_speed_suffix_is_honoured(tmp_path: Path, text: str, expected: float) -> None:
    """Flat out for a test, real time for a rehearsal, from the same code path."""

    path = write_wav(tmp_path / "tone.wav", np.arange(1600, dtype="<i2"))
    source = build_source(f"file:{path}@{text}")

    assert source.speed == expected  # type: ignore[attr-defined]


def test_a_pipewire_spec_builds_a_capture_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pw_record, "pipewire_sources", lambda: [("mic", "A Mic")])

    source = build_source("pw:mic")

    assert isinstance(source, AudioSource)
    assert source.target == "mic"   # type: ignore[attr-defined]
    assert source.calibration.enabled and source.calibration.runtime


def test_factory_preserves_explicit_calibration_opt_out(monkeypatch):
    monkeypatch.setattr(pw_record, "pipewire_sources", lambda: [("mic", "A Mic")])
    source = build_source("pw:mic", calibration=CalibrationConfig(enabled=False))
    assert source.calibration.enabled is False


def test_approach_capture_uses_resolved_calibration_settings(monkeypatch):
    from livesubs.approaches import capture_source
    from livesubs.config import Settings

    monkeypatch.setattr(pw_record, "pipewire_sources", lambda: [("mic", "A Mic")])
    settings = Settings(capture={"target": "mic", "runtime_calibration": False})
    source = capture_source(settings, {})
    assert source.calibration.enabled is True and source.calibration.runtime is False


def test_a_path_containing_a_colon_survives(tmp_path: Path) -> None:
    """Only the first colon separates the kind, so the rest is the path."""

    directory = tmp_path / "odd:name"
    directory.mkdir()
    path = write_wav(directory / "tone.wav", np.arange(1600, dtype="<i2"))

    assert build_source(f"file:{path}").path == path   # type: ignore[attr-defined]


# --- refusing correctly ---------------------------------------------------------


def test_an_unknown_kind_is_rejected_with_the_syntax() -> None:
    with pytest.raises(ValueError, match="unknown audio source") as caught:
        build_source("microphone")

    assert SYNTAX in str(caught.value)


def test_synth_rejects_an_argument() -> None:
    with pytest.raises(ValueError, match="synth takes no argument"):
        build_source("synth:loud")


def test_file_requires_a_path() -> None:
    with pytest.raises(ValueError, match="file: needs a path"):
        build_source("file:")


def test_pw_requires_a_target() -> None:
    with pytest.raises(ValueError, match="pw: needs a node.name"):
        build_source("pw:")


def test_a_non_numeric_speed_is_rejected(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "tone.wav", np.arange(1600, dtype="<i2"))

    with pytest.raises(ValueError, match="speed must be a number"):
        build_source(f"file:{path}@fast")


def test_a_missing_recording_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_source(f"file:{tmp_path / 'absent.wav'}")


def test_an_unknown_capture_target_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail before the server binds a port, not on the first frame."""

    monkeypatch.setattr(pw_record, "pipewire_sources", lambda: [("good", "A Mic")])

    with pytest.raises(ValueError, match="no capture source named 'stale'"):
        build_source("pw:stale")


# --- laziness -------------------------------------------------------------------


def test_backends_are_imported_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine without PipeWire must still be able to replay a file.

    Importing every backend eagerly would make one backend's failure a failure of
    all of them, which is exactly what the factory exists to avoid.
    """

    import builtins

    real_import = builtins.__import__
    imported: list[str] = []

    def tracking_import(name: str, *args: object, **kwargs: object) -> object:
        imported.append(name)
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", tracking_import)

    build_source("synth")

    assert not any("pw_record" in name for name in imported)
    assert not any("file_source" in name for name in imported)
