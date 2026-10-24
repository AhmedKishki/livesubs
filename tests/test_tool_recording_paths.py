"""Corpus preparation must score raw recognition, not a reader-language output."""

from __future__ import annotations

import importlib.util
import wave
from pathlib import Path


def test_rehearsal_readiness_does_not_require_translation_warmup() -> None:
    path = Path(__file__).parents[1] / "tools" / "rehearse.py"
    spec = importlib.util.spec_from_file_location("rehearsal_recording_paths", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for approach in ("live", "live-text"):
        assert module.READY[approach].search("recognition stream open")


def test_prepared_corpus_scores_the_source_recording(tmp_path, capsys) -> None:
    path = Path(__file__).parents[1] / "tools" / "prepare_corpus.py"
    spec = importlib.util.spec_from_file_location("prepare_recording_paths", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    audio = tmp_path / "speech.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(bytes(3200))
    transcript = tmp_path / "speech.txt"
    transcript.write_text("A short English reference.", encoding="utf-8")

    assert module.main([
        "--audio", str(audio), "--audio-out", str(audio),
        "--transcript", str(transcript), "--out-dir", str(tmp_path / "out"),
    ]) == 0
    output = capsys.readouterr().out
    assert "--hypothesis recordings/<stem>.source.srt" in output
    assert "--hypothesis recordings/<stem>.en.srt" not in output
