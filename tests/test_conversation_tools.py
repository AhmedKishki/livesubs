"""The two tools that test a conversation: one splices clips, one checks the subtitles."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from livesubs.evaluation import Turn

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first, because a dataclass looks its module up while it is defined.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


splice_conversation = _load("splice_conversation")
check_conversation = _load("check_conversation")

#: Bytes in one millisecond of 16 kHz mono signed-16 audio.
MS = 32


def test_a_clip_names_its_language_its_file_and_optionally_a_window() -> None:
    clip = splice_conversation.parse_clip("de:clips/die verwandlung.mp3@60-66.5")

    assert clip.language == "de"
    assert clip.path == Path("clips/die verwandlung.mp3")
    assert (clip.start_s, clip.end_s) == (60.0, 66.5)
    assert splice_conversation.parse_clip("en:a@b.mp3").path == Path("a@b.mp3")


@pytest.mark.parametrize("spec", ["clips/en.mp3", "xx:clips/en.mp3", "en:clips/en.mp3@9-3"])
def test_a_clip_without_a_known_language_or_with_a_backwards_window_is_refused(
    spec: str,
) -> None:
    with pytest.raises(ValueError):
        splice_conversation.parse_clip(spec)


def test_a_window_is_cut_on_whole_samples() -> None:
    pcm = bytes(range(200)) * 160  # one second

    window = splice_conversation.cut(pcm, 0.25, 0.5)

    assert len(window) == 250 * MS
    assert window == pcm[250 * MS : 500 * MS]


def test_turns_are_spliced_with_silence_and_recorded_on_the_audio_clock() -> None:
    english, german = b"\x01\x00" * 16_000, b"\x02\x00" * 8_000

    pcm, turns = splice_conversation.splice(
        [("en", english), ("de", german)], gap_ms=500, lead_ms=2_000, tail_ms=1_000
    )

    assert turns == [Turn("en", 2_000, 3_000), Turn("de", 3_500, 4_000)]
    assert len(pcm) == 5_000 * MS
    assert pcm[: 2_000 * MS] == bytes(2_000 * MS)
    assert pcm[2_000 * MS : 3_000 * MS] == english
    assert pcm[4_000 * MS :] == bytes(1_000 * MS)


def test_the_checker_holds_targets_to_their_language_and_the_transcript_to_the_turns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    turns = tmp_path / "talk.turns.json"
    turns.write_text(
        json.dumps(
            {
                "turns": [
                    {"language": "en", "start_ms": 0, "end_ms": 4_000},
                    {"language": "de", "start_ms": 4_500, "end_ms": 9_000},
                ]
            }
        )
    )
    stem = tmp_path / "demo-1"
    (tmp_path / "demo-1.en+de.srt").write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nHe woke up and he was not a man.\n\n"
        "2\n00:00:05,000 --> 00:00:07,000\nEr wachte auf und er war nicht mehr ein Mensch.\n"
    )
    (tmp_path / "demo-1.en.srt").write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nHe woke up and he was not a man.\n\n"
        "2\n00:00:05,000 --> 00:00:07,000\nEr wachte auf und er war nicht mehr ein Mensch.\n"
    )

    assert check_conversation.main(["--turns", str(turns), str(stem)]) == 0

    report = capsys.readouterr().out
    assert "en+de: 2 lines, 0 in the wrong language" in report
    assert "en: 2 lines, 1 in the wrong language" in report
    assert "expected en, read de" in report


def test_the_checker_refuses_a_stem_with_no_subtitle_files(tmp_path: Path) -> None:
    turns = tmp_path / "talk.turns.json"
    turns.write_text(json.dumps({"turns": []}))

    with pytest.raises(SystemExit):
        check_conversation.main(["--turns", str(turns), str(tmp_path / "nothing")])
