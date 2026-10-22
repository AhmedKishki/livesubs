"""Check a conversation session's subtitle files against the turns it was replayed from.

Every target file should be in its own language throughout, whoever was speaking, and
the transcript should follow the speakers from turn to turn. This reports the lines that
do not, and the turns that produced no line at all, which is where words were lost
around a switch. The turns come from ``tools/splice_conversation.py``.

Usage::

    uv run python tools/check_conversation.py \\
        --turns samples/conversation-16k-mono.turns.json \\
        recordings/demo-20261023-101500

The stem is the session's recording name without a language or extension. Every
``STEM.*.srt`` beside it is checked. The transcript is the file named after the
conversation's languages, such as ``.en+de.srt``.

A line's language is guessed from its function words, so a line of one or two words is
counted as too short to tell rather than as right or wrong.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from livesubs.corpus import read_cues
from livesubs.evaluation import Turn, check_languages

__all__ = ["main"]


def _turns(path: Path) -> list[Turn]:
    entries = json.loads(path.read_text(encoding="utf-8"))["turns"]
    return [Turn(entry["language"], entry["start_ms"], entry["end_ms"]) for entry in entries]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--turns", required=True, type=Path, help="the splice manifest")
    parser.add_argument("stem", type=Path, help="the session's subtitle files, without .LANG.srt")
    args = parser.parse_args(argv)

    turns = _turns(args.turns)
    files = sorted(args.stem.parent.glob(f"{args.stem.name}.*.srt"))
    if not files:
        raise SystemExit(f"no subtitle files match {args.stem}.*.srt")

    for path in files:
        code = path.suffixes[-2].lstrip(".")
        # The transcript is held to each turn's language; a target to its own.
        expected = None if "+" in code else code
        check = check_languages(code, read_cues(path), turns, expected=expected)
        print(check.format_text(turns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
