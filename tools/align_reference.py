"""Give an untimed transcript the timings of the audio it came from.

A publisher's transcript has words and no timings; the pipeline needs to know when each
piece of text is being said. The audio supplies the answer, and it is already written down:
running *any* approach over the recording produces a cue file whose boundaries are real
speech spans on the audio clock.

So this lays the transcript's words onto those spans, in proportion to how long each one
lasted. A word lands in the right utterance, because the boundaries are real pauses; its
position *within* an utterance is approximate. That is precise enough to rehearse the German
and the overlay against audio somebody can hear, and not precise enough to score word
timings - which is why the numbers are printed rather than hidden.

Usage::

    # 1. Teach the pipeline where the speech is. Any approach does; this one is quick.
    uv run livesubs --serve --approach live-sim --audio file:samples/sample-16k-mono.wav@0
    #    (its cues land in recordings/<stem>.en.srt)

    # 2. Lay the transcript onto those spans.
    uv run python tools/align_reference.py \\
        --reference samples/reference.en.txt \\
        --timing recordings/sample.en.srt \\
        --out samples/reference.en.timed.srt

    # 3. Translate it live, against the audio.
    uv run livesubs --serve --approach live-text \\
        --audio file:samples/sample-16k-mono.wav@1 --cues samples/reference.en.timed.srt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from livesubs.corpus import distribute_words, read_cues, read_reference
from livesubs.subtitles import Cue

__all__ = ["main"]


def _srt(cues: list[Cue]) -> str:
    """SubRip, with the same timestamp form the pipeline's own writer produces."""

    from livesubs.subtitles import srt_timestamp

    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n{srt_timestamp(cue.start_ms)} --> {srt_timestamp(cue.end_ms)}\n{cue.text}\n"
        )
    return "\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reference", required=True, type=Path, help="the untimed transcript")
    parser.add_argument(
        "--timing",
        required=True,
        type=Path,
        help="a timed .srt or .vtt of the same recording, to take the spans from",
    )
    parser.add_argument("--out", required=True, type=Path, help="the timed transcript to write")
    args = parser.parse_args(argv)

    for path in (args.reference, args.timing):
        if not path.is_file():
            raise SystemExit(f"no such file: {path}")

    words = read_reference(args.reference).split()
    spans = read_cues(args.timing)

    if not words:
        raise SystemExit(f"{args.reference} has no words")
    if not spans:
        raise SystemExit(
            f"{args.timing} has no cues. Produce one by running an approach over the "
            "recording, e.g. --approach live-sim --audio file:<recording>@0"
        )

    timed = distribute_words(words, spans)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(_srt(timed), encoding="utf-8")

    counts = [len(cue.text.split()) for cue in timed]
    covered = sum(cue.end_ms - cue.start_ms for cue in timed)

    print(f"transcript : {len(words)} words")
    print(f"spans      : {len(spans)} cues from {args.timing.name}")
    print(f"written    : {args.out}  ({len(timed)} cues)")
    print(f"per cue    : min {min(counts)} mean {sum(counts) / len(counts):.1f} max {max(counts)} words")
    print(f"on the clock: {covered / 1000:.0f} s covered")
    print()
    print(
        "Approximate: a word is placed in the correct utterance and at an approximate "
        "position inside it, so this is good enough to read along with the audio and not "
        "good enough to score word timings."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
