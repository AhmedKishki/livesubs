"""Splice English and German clips into one test conversation.

A conversation session is tested by replaying a file in which the language changes, and
no recording of a real one exists here. This cuts windows from any recordings GStreamer
can decode, joins them in the order given with silence between them, and writes the
16 kHz mono WAV that ``--audio file:`` replays, plus a manifest of the turns: which
language each stretch is in, on the same audio clock as the session's subtitle files.
``tools/check_conversation.py`` reads that manifest.

Usage::

    uv run python tools/splice_conversation.py \\
        --clip en:clips/en.mp3@60-80 \\
        --clip de:clips/de.mp3@60-66 \\
        --clip en:clips/en.mp3@80-84 \\
        --out samples/conversation-16k-mono.wav

The output starts with ``--lead-ms`` of silence, so a viewer can connect before the
first word. Without a viewer the live pipeline drops the audio.

Needs ``gst-launch-1.0`` (GStreamer), like ``tools/prepare_corpus.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from livesubs.evaluation import Turn
from livesubs.languages import language

__all__ = ["Clip", "cut", "main", "parse_clip", "splice"]

SAMPLE_RATE = 16_000
#: Bytes in one millisecond of 16 kHz mono signed-16 audio.
_BYTES_PER_MS = SAMPLE_RATE * 2 // 1000

_WINDOW = re.compile(r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$")


@dataclass(frozen=True, slots=True)
class Clip:
    """One turn to cut: its spoken language, the recording, and the seconds to keep."""

    language: str
    path: Path
    start_s: float = 0.0
    end_s: float | None = None


def parse_clip(spec: str) -> Clip:
    """``LANG:PATH`` or ``LANG:PATH@START-END``, with the window in seconds."""

    code, separator, rest = spec.partition(":")
    if not separator or not rest:
        raise ValueError(f"{spec!r} is not LANG:PATH[@START-END]")
    language(code)

    path, start, end = rest, 0.0, None
    head, at, tail = rest.rpartition("@")
    window = _WINDOW.match(tail) if at else None
    if window:
        path, start, end = head, float(window.group(1)), float(window.group(2))
        if end <= start:
            raise ValueError(f"{spec!r} ends before it starts")
    return Clip(code, Path(path), start, end)


def cut(pcm: bytes, start_s: float, end_s: float | None) -> bytes:
    """The window of 16 kHz mono signed-16 audio, on whole samples."""

    start = round(start_s * 1000) * _BYTES_PER_MS
    end = len(pcm) if end_s is None else round(end_s * 1000) * _BYTES_PER_MS
    return pcm[start:end]


def _end(clip: Clip) -> str:
    return "end" if clip.end_s is None else f"{clip.end_s:g}"


def splice(
    parts: Sequence[tuple[str, bytes]], *, gap_ms: int, lead_ms: int, tail_ms: int
) -> tuple[bytes, list[Turn]]:
    """Join ``(language, audio)`` turns with silence, and say where each one landed."""

    audio = bytearray(lead_ms * _BYTES_PER_MS)
    turns: list[Turn] = []
    for index, (code, pcm) in enumerate(parts):
        if index:
            audio += bytes(gap_ms * _BYTES_PER_MS)
        start_ms = len(audio) // _BYTES_PER_MS
        audio += pcm
        turns.append(Turn(code, start_ms, len(audio) // _BYTES_PER_MS))
    audio += bytes(tail_ms * _BYTES_PER_MS)
    return bytes(audio), turns


def _read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (
            SAMPLE_RATE,
            1,
            2,
        ):
            raise SystemExit(f"{path} did not decode to 16 kHz mono signed-16 audio")
        return reader.readframes(reader.getnframes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--clip", action="append", required=True, type=parse_clip,
        help="LANG:PATH[@START-END], one per turn, in the order they are spoken",
    )
    parser.add_argument("--out", required=True, type=Path, help="the WAV to write")
    parser.add_argument("--gap-ms", type=int, default=400, help="silence between turns")
    parser.add_argument("--lead-ms", type=int, default=5_000, help="silence before the first")
    parser.add_argument("--tail-ms", type=int, default=3_000, help="silence after the last")
    args = parser.parse_args(argv)

    # Imported here so the functions above load without GStreamer or the tools directory
    # on the path, which is how the tests load them.
    from prepare_corpus import build_wav, require

    gst = require("gst-launch-1.0")
    decoded: dict[Path, bytes] = {}
    parts: list[tuple[str, bytes]] = []
    with tempfile.TemporaryDirectory() as scratch:
        for clip in args.clip:
            if clip.path not in decoded:
                wav = Path(scratch) / f"{len(decoded)}.wav"
                build_wav(clip.path, wav, gst=gst)
                decoded[clip.path] = _read_pcm(wav)
            parts.append((clip.language, cut(decoded[clip.path], clip.start_s, clip.end_s)))

    pcm, turns = splice(parts, gap_ms=args.gap_ms, lead_ms=args.lead_ms, tail_ms=args.tail_ms)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(args.out), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(pcm)

    manifest = args.out.with_suffix(".turns.json")
    manifest.write_text(
        json.dumps(
            {
                "audio": args.out.name,
                "turns": [
                    {
                        "language": turn.language,
                        "start_ms": turn.start_ms,
                        "end_ms": turn.end_ms,
                        "source": f"{clip.path.name}@{clip.start_s:g}-{_end(clip)}",
                    }
                    for turn, clip in zip(turns, args.clip, strict=True)
                ],
            },
            indent=2,
        )
        + "\n"
    )

    print(f"wrote {args.out} ({len(pcm) // _BYTES_PER_MS / 1000:.1f} s, {len(turns)} turns)")
    print(f"wrote {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
