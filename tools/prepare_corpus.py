"""Build a scorable corpus from what a sample actually arrives as.

A sample turns up as an audio file and a transcript, and neither is in the format the
pipeline wants: audio is MP3 or a video container rather than 16 kHz mono WAV, and a
transcript is a PDF full of footnotes rather than plain words. This tool does that
conversion once, writes a **manifest**, and gets out of the way.

Optional tooling, in the same sense as ``check_overlay_layout.py`` needing node and
Chrome: this needs ``pdftotext`` (poppler) and ``gst-launch-1.0`` (GStreamer). Neither
is a runtime dependency, and the pipeline never calls this. ``ffmpeg`` is not
required, which matters because it is absent from the reference machine by design.

Usage::

    uv run python tools/prepare_corpus.py \\
        --audio 'samples/Episode 0.mp3' \\
        --transcript 'samples/Episode 0.pdf' \\
        --out-dir samples

Writes, next to each other so they cannot be mixed up::

    samples/<stem>-16k-mono.wav    what the pipeline can replay
    samples/reference.en.txt       the words that were actually spoken
    samples/reference.de.txt       the same words in German, if one was supplied
    samples/corpus-manifest.json   hashes, duration, word counts, and what was removed

The manifest is the point of the exercise. A WER or chrF figure is only meaningful
next to the exact bytes it was computed from, so the digests of every source file are
recorded alongside the numbers.

A German reference is what makes translation quality measurable at all, and it is
worth being precise about what it is. A translation supplied by the publisher or a
translation service is a *reference*, not ground truth: chrF against it measures
agreement with that translator, which is exactly the right question for comparing
providers and the wrong one for deciding whether the German is correct.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import wave
from pathlib import Path

from livesubs.corpus import clean_worksheet_transcript, read_reference
from livesubs.evaluation import tokenise

__all__ = ["build_wav", "extract_transcript", "main", "wav_format"]

#: The format the capture contract requires (``audio/base.py``). Fixed here rather
#: than configurable, because a corpus that is not in this format cannot be replayed
#: without the resampler, and the resampler would hide a conversion mistake.
TARGET = "audio/x-raw,format=S16LE,channels=1,rate=16000"


def sha256(path: Path) -> str:
    """Digest a file in chunks, because a sample can be a few hundred megabytes."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require(tool: str) -> str:
    found = shutil.which(tool)
    if found is None:
        raise SystemExit(
            f"needs {tool!r}, which is not installed.\n"
            "  audio: apt install gstreamer1.0-libav gstreamer1.0-plugins-good\n"
            "  pdf:   apt install poppler-utils"
        )
    return found


def build_wav(source: Path, destination: Path, *, gst: str = "gst-launch-1.0") -> None:
    """Decode any container GStreamer can read into 16 kHz mono signed-16 PCM.

    ``decodebin`` rather than a container-specific demuxer, so one command covers MP3,
    MP4, MKV and WebM and a new sample needs no new code.
    """

    pipeline = [
        gst, "-q",
        "filesrc", f"location={source}",
        "!", "decodebin",
        "!", "audioconvert",
        "!", "audioresample",
        "!", TARGET,
        "!", "wavenc",
        "!", "filesink", f"location={destination}",
    ]

    result = subprocess.run(pipeline, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not destination.is_file():
        raise SystemExit(
            f"could not decode {source.name}:\n{result.stderr.strip() or '(no output)'}"
        )


def wav_format(path: Path) -> dict[str, float | int]:
    """Rate, channels, width and duration, read back so the conversion is checked."""

    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
        return {
            "sample_rate": rate,
            "channels": handle.getnchannels(),
            "sample_width": handle.getsampwidth(),
            "frames": frames,
            "duration_s": round(frames / rate, 3) if rate else 0.0,
        }
def extract_transcript(source: Path) -> tuple[str, str]:
    """The spoken words from a transcript, whatever form it arrived in.

    Returns the text and a note describing what was done to it, because "cleaned" is
    not a description: a reader of the manifest needs to know that a summary and ten
    footnotes were removed, or they will wonder where two hundred words went.
    """

    suffix = source.suffix.lower()

    if suffix == ".pdf":
        pdftotext = require("pdftotext")
        result = subprocess.run(
            [pdftotext, "-layout", str(source), "-"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SystemExit(f"pdftotext failed on {source.name}: {result.stderr.strip()}")

        cleaned = clean_worksheet_transcript(result.stdout)
        note = "pdftotext -layout, then worksheet cleaning (summary, footnotes, line wraps)"
        return cleaned, note

    if suffix in (".srt", ".vtt"):
        return read_reference(source), f"{suffix[1:].upper()} cue texts, joined in order"

    if suffix in (".txt", ".md"):
        return read_reference(source), "read as plain text"

    raise SystemExit(f"unsupported transcript format: {suffix!r} (pdf, srt, vtt or txt)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audio", required=True, type=Path, help="audio or video file")
    parser.add_argument("--transcript", required=True, type=Path, help="pdf, srt, vtt or txt")
    parser.add_argument(
        "--transcript-de",
        type=Path,
        default=None,
        help="the same transcript in German, so translation quality becomes measurable",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("samples"))
    parser.add_argument("--stem", default=None, help="output name (default: the audio's)")
    parser.add_argument(
        "--audio-out",
        default=None,
        help="use an existing WAV instead of decoding one (for a sample already prepared)",
    )
    args = parser.parse_args(argv)

    for path in (args.audio, args.transcript):
        if not path.is_file():
            raise SystemExit(f"no such file: {path}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.stem or args.audio.stem

    wav_path = Path(args.audio_out) if args.audio_out else args.out_dir / f"{stem}-16k-mono.wav"
    if not args.audio_out:
        require("gst-launch-1.0")
        build_wav(args.audio, wav_path)

    fmt = wav_format(wav_path)
    if fmt["sample_rate"] != 16_000 or fmt["channels"] != 1 or fmt["sample_width"] != 2:
        raise SystemExit(f"{wav_path.name} is not 16 kHz mono s16: {fmt}")

    text, note = extract_transcript(args.transcript)
    reference_path = args.out_dir / "reference.en.txt"
    reference_path.write_text(text + "\n", encoding="utf-8")

    words = tokenise(text)

    # A German reference is optional, and it is what turns "the German appears" into
    # "the German agrees with a translator to this degree". Read through the same
    # extractor, because a German worksheet carries the same footnotes in a different
    # shape: capitalised terms, and a full stop after the footnote number.
    target_reference: dict[str, object] | None = None
    if args.transcript_de is not None:
        if not args.transcript_de.is_file():
            raise SystemExit(f"no such German transcript: {args.transcript_de}")

        german_text, german_note = extract_transcript(args.transcript_de)
        german_path = args.out_dir / "reference.de.txt"
        german_path.write_text(german_text + "\n", encoding="utf-8")
        german_words = tokenise(german_text)

        target_reference = {
            "source": str(args.transcript_de),
            "sha256": sha256(args.transcript_de),
            "reference": str(german_path),
            "note": german_note,
            "words": len(german_words),
            "unique_words": len(set(german_words)),
        }

    duration_min = (fmt["duration_s"] or 0) / 60
    manifest = {
        "audio": {
            "source": str(args.audio),
            "sha256": sha256(args.audio),
            "wav": str(wav_path),
            **fmt,
        },
        "transcript": {
            "source": str(args.transcript),
            "sha256": sha256(args.transcript),
            "reference": str(reference_path),
            "note": note,
            "words": len(words),
            "unique_words": len(set(words)),
        },
        "derived": {
            "words_per_minute": round(len(words) / duration_min, 1) if duration_min else None,
        },
        "language": {"source": "en", "target": "de"},
        "reference_target": target_reference,
    }

    manifest_path = args.out_dir / "corpus-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"audio      : {wav_path}  ({fmt['duration_s']} s, 16 kHz mono s16)")
    print(f"reference  : {reference_path}  ({len(words)} words, {len(set(words))} unique)")
    if target_reference is not None:
        print(
            f"german ref : {target_reference['reference']}  "
            f"({target_reference['words']} words, {target_reference['unique_words']} unique)"
        )
    print(f"manifest   : {manifest_path}")
    print(f"transcript : {note}")
    print(f"rate       : {manifest['derived']['words_per_minute']} words per minute")
    print()
    print("Replay it with:")
    print(f"  uv run livesubs --serve --approach live-sim --audio file:{wav_path}@0")
    print()
    print("Score a session with:")
    print(
        "  uv run python tools/verify_against_reference.py"
        f" --reference {reference_path} --hypothesis recordings/<stem>.en.srt"
        " --hypothesis-de recordings/<stem>.de.srt"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
