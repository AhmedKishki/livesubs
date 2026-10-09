"""Score a session against a reference transcript, or against itself.

The measurement this repository was missing: ``docs/09-evaluation.md`` says what to
measure, and until now nothing computed it. This reads a reference (a plain
transcript, or a subtitle file with timings) and the session's own output, and reports
the numbers in one place.

Two things it deliberately does not do. It does not call a provider, so it can score a
recording from last year. And it does not need a **German** reference to say something
useful about the German: numbers that vanished, output that is not actually
translated, and the German-to-English length ratio all work from the session's own
files.

Usage::

    # ASR accuracy, from a session's own English subtitle file
    uv run python tools/verify_against_reference.py \\
        --reference samples/reference.en.txt \\
        --hypothesis recordings/demo-20260923-101500.en.srt

    # German quality, once a German reference exists
    uv run python tools/verify_against_reference.py \\
        --reference samples/reference.en.txt \\
        --hypothesis recordings/demo-20260923-101500.en.srt \\
        --hypothesis-de recordings/demo-20260923-101500.de.srt \\
        --reference-de samples/reference.de.txt

    # Prove the arithmetic on the real corpus before trusting any number it prints
    uv run python tools/verify_against_reference.py \\
        --reference samples/reference.en.txt --self-test

``--self-test`` is the important one. A harness that is wrong about WER is worse than
no harness, so it derives corrupted copies of the reference with a known error rate and
fails loudly if the metric does not reproduce it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from livesubs.corpus import align_by_order, group_by_window, read_cues, read_reference
from livesubs.evaluation import (
    SessionReport,
    chrf_corpus,
    identical_to_source_fraction,
    length_ratio,
    number_preservation,
    percentiles,
    tokenise,
    wer,
    wer_on_numbers,
)

__all__ = ["main", "score", "self_test"]

#: Every tenth word, so a corruption's error rate is exactly known: deleting or
#: substituting one reference word in ten is a WER of 0.1 by construction.
_SELF_TEST_STRIDE = 10


def _source_target_pairs(hypothesis: Path, hypothesis_de: Path) -> list[tuple[str, str]]:
    """Our English paired with our German.

    These are the pairs the reference-free checks need: "did the number survive into
    the German" is a question about what the translator was given, which is our ASR
    output, not the reference transcript. Scoring that against the reference would
    count the reference translator's own omissions against us.
    """

    timed = (hypothesis.suffix.lower(), hypothesis_de.suffix.lower())
    if timed[0] in (".srt", ".vtt") and timed[1] in (".srt", ".vtt"):
        return align_by_order(read_cues(hypothesis), read_cues(hypothesis_de))

    return [(read_reference(hypothesis), read_reference(hypothesis_de))]


def _reference_pairs(
    *, reference: Path, reference_de: Path | None, hypothesis_de: Path, hypothesis: Path
) -> list[tuple[str, str]]:
    """Reference German paired with the German we produced for it.

    Three situations, and mixing them up would produce a number that looks perfectly
    reasonable and means nothing:

    * a **timed German reference** - its cues are the windows, and our fragments are
      reassembled into them, so the 2 s fragment policy is not scored as an error;
    * an **untimed German reference** - one whole-text pair. Coarser, and honest;
    * **no German reference** - the English cues become the windows. That is what makes
      the translation stage measurable on its own: the reference transcript goes
      through the translator and nothing else is involved.

    With neither a German reference nor any timings there is no sane pairing, and none
    is invented: comparing our German to the English source would score a correct
    translation as total disagreement.
    """

    our_german = read_reference(hypothesis_de)

    for candidate in (reference_de, reference):
        if candidate is not None and candidate.suffix.lower() in (".srt", ".vtt"):
            windows = read_cues(candidate)

            if hypothesis_de.suffix.lower() not in (".srt", ".vtt"):
                return [(" ".join(window.text for window in windows), our_german)]

            groups, _unmatched = group_by_window(windows, read_cues(hypothesis_de))
            return [
                (window.text, " ".join(cue.text for cue in group))
                for window, group in zip(windows, groups, strict=False)
                if group
            ]

    if reference_de is not None:
        return [(read_reference(reference_de), our_german)]

    return []


def self_test(reference_path: Path) -> int:
    """Check the arithmetic on the real corpus, with error rates known in advance.

    Deleting every tenth reference word is a WER of exactly 0.1. Substituting every
    tenth is a WER of exactly 0.1 with a substitution count of one in ten rather than
    a deletion count. If those do not come back, every number this tool prints about a
    real session is suspect, so this fails rather than warns.
    """

    tokens = tokenise(read_reference(reference_path))
    if len(tokens) < 100:
        print(f"reference is only {len(tokens)} words; too short to calibrate")
        return 1

    failures = 0
    total = len(tokens)

    identical = wer(tokens, tokens)
    ok = identical.wer == 0.0
    failures += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] identical hypothesis -> WER 0.0  (got {identical.wer})")

    # The expectation is the arithmetic, not a rounded 0.1: with a reference that is
    # not a multiple of ten words, one in ten removed is 120 of 1205, and asserting
    # "0.1 exactly" would fail a metric that is behaving perfectly.
    kept = [token for index, token in enumerate(tokens) if (index + 1) % _SELF_TEST_STRIDE]
    removed = total - len(kept)
    deleted = wer(tokens, kept)
    ok = (
        deleted.deletions == removed
        and deleted.substitutions == 0
        and deleted.insertions == 0
        and deleted.wer == removed / total
    )
    failures += not ok
    print(
        f"[{'PASS' if ok else 'FAIL'}] every {_SELF_TEST_STRIDE}th word deleted -> "
        f"{removed}/{total} counted as deletions "
        f"(got {deleted.wer:.4f}, {deleted.deletions} deletions, "
        f"{deleted.substitutions} substitutions)"
    )

    substituted = [
        "zzz" if (index + 1) % _SELF_TEST_STRIDE == 0 else token
        for index, token in enumerate(tokens)
    ]
    sub = wer(tokens, substituted)
    ok = (
        sub.substitutions == removed
        and sub.deletions == 0
        and sub.insertions == 0
        and sub.wer == removed / total
    )
    failures += not ok
    print(
        f"[{'PASS' if ok else 'FAIL'}] every {_SELF_TEST_STRIDE}th word substituted -> "
        f"{removed}/{total} counted as substitutions "
        f"(got {sub.wer:.4f}, {sub.substitutions} substitutions, "
        f"{sub.deletions} deletions)"
    )

    numbers = wer_on_numbers(read_reference(reference_path), read_reference(reference_path))
    ok = numbers is None or numbers.wer == 0.0
    failures += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] numeral slice of the reference against itself")

    print()
    print(
        f"{'calibrated' if not failures else 'NOT CALIBRATED'}: "
        f"{4 - failures}/4 checks passed against {reference_path.name}"
    )
    return 1 if failures else 0

    numbers = wer_on_numbers(read_reference(reference_path), read_reference(reference_path))
    ok = numbers is None or numbers.wer == 0.0
    failures += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] numeral slice of the reference against itself")

    print()
    print(
        f"{'calibrated' if not failures else 'NOT CALIBRATED'}: "
        f"{4 - failures}/4 checks passed against {reference_path.name}"
    )
    return 1 if failures else 0


def score(
    *,
    reference: Path,
    hypothesis: Path,
    hypothesis_de: Path | None = None,
    reference_de: Path | None = None,
) -> SessionReport:
    """Build a report from files on disk. No provider, no network."""

    reference_text = read_reference(reference)
    hypothesis_text = read_reference(hypothesis)

    report = SessionReport(
        reference_words=len(tokenise(reference_text)),
        asr=wer(tokenise(reference_text), tokenise(hypothesis_text)),
        asr_numbers=wer_on_numbers(reference_text, hypothesis_text),
    )

    # Pacing is measured from the session's own cue timings, and it is the one thing
    # measurable without any reference at all: how long each line was on screen.
    if hypothesis.suffix.lower() in (".srt", ".vtt"):
        cues = read_cues(hypothesis)
        report.segments = len(cues)
        if cues:
            report.cue_ms = percentiles(cue.end_ms - cue.start_ms for cue in cues)

    if hypothesis_de is None:
        report.notes.append(
            "no German output given, so only the English was scored against the reference"
        )
        return report

    pairs = _source_target_pairs(hypothesis, hypothesis_de)
    report.numbers = number_preservation(pairs)
    report.untranslated = identical_to_source_fraction(pairs)
    report.length_ratio = length_ratio(pairs)

    german_reference_pairs = _reference_pairs(
        reference=reference,
        reference_de=reference_de,
        hypothesis=hypothesis,
        hypothesis_de=hypothesis_de,
    )
    report.chrf = chrf_corpus(german_reference_pairs)

    if report.chrf is None:
        report.notes.append(
            "chrF needs a German reference or timed cue windows; none were available"
        )

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reference", required=True, type=Path, help="the words spoken")
    parser.add_argument(
        "--hypothesis",
        type=Path,
        help="a session's own English output (.srt, .vtt or .txt)",
    )
    parser.add_argument(
        "--hypothesis-de",
        type=Path,
        help="the same session's German output, for the translation checks",
    )
    parser.add_argument(
        "--reference-de",
        type=Path,
        help="a German reference translation, timed, to score the German against",
    )
    parser.add_argument("--json", type=Path, default=None, help="write the report as JSON")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="calibrate the metric against the reference instead of scoring a session",
    )
    args = parser.parse_args(argv)

    if not args.reference.is_file():
        raise SystemExit(f"no such reference: {args.reference}")

    if args.self_test:
        return self_test(args.reference)

    if args.hypothesis is None:
        raise SystemExit("--hypothesis is required (or use --self-test)")
    if not args.hypothesis.is_file():
        raise SystemExit(f"no such hypothesis: {args.hypothesis}")

    report = score(
        reference=args.reference,
        hypothesis=args.hypothesis,
        hypothesis_de=args.hypothesis_de,
        reference_de=args.reference_de,
    )

    print(f"reference  : {args.reference}")
    print(f"hypothesis : {args.hypothesis}")
    if args.hypothesis_de is not None:
        print(f"german     : {args.hypothesis_de}")
    if args.reference_de is not None:
        print(f"german ref : {args.reference_de}")
    print()
    print(report.format_text())
    print()

    if report.asr is not None:
        print(
            "A word error rate measures the words, not the lecture: read the numeral "
            "line and the missed numbers above before believing the percentage."
        )

    if args.json is not None:
        payload = report.as_dict() | {
            "reference": str(args.reference),
            "hypothesis": str(args.hypothesis),
            "hypothesis_de": str(args.hypothesis_de) if args.hypothesis_de else None,
            "reference_de": str(args.reference_de) if args.reference_de else None,
        }
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"report written to {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
