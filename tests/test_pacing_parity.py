"""The reading-time contract must not drift between Python and JavaScript.

``hold_ms`` exists twice because the overlay cannot ask the server how long to
show a line - reading speed and font size are viewer-local. Duplication with a
promise is how implementations diverge, so the shared vectors are checked from
both languages.
"""

from __future__ import annotations

import json
from pathlib import Path

from livesubs.segmenter import hold_ms

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "spec" / "pacing_vectors.json"
JS_PACING = ROOT / "web" / "static" / "pacing.js"
NODE_TEST = ROOT / "web" / "tests" / "pacing.test.mjs"


def _vectors() -> dict:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def test_vectors_file_exists() -> None:
    assert VECTORS.is_file(), "run: uv run python tools/gen_pacing_vectors.py"


def test_python_matches_the_committed_vectors() -> None:
    data = _vectors()
    for raw, expected in zip(data["inputs"], data["expected"], strict=True):
        assert hold_ms(raw) == expected, (
            f"hold_ms({raw}) is {hold_ms(raw)}, vectors say {expected}. "
            "Regenerate with: uv run python tools/gen_pacing_vectors.py"
        )


def test_vectors_exercise_every_branch() -> None:
    """A vector set that only samples the linear region proves little."""

    data = _vectors()
    expected = data["expected"]
    constants = data["constants"]

    assert constants["MIN_HOLD_MS"] in expected, "floor never reached"
    assert constants["MAX_HOLD_MS"] in expected, "ceiling never reached"
    assert len(set(expected)) >= 10, "linear region barely sampled"


def test_constants_in_javascript_match_the_vectors() -> None:
    """Cheap guard: the JS constants must equal the generated ones.

    Catches an edit to one side that forgot the other, without needing node.
    """

    source = JS_PACING.read_text(encoding="utf-8")
    for name, value in _vectors()["constants"].items():
        assert f"export const {name} = {value};" in source, f"{name} drifted in pacing.js"


def test_javascript_actually_reads_the_shared_vectors() -> None:
    """The node test must consume the file, not hardcode its own expectations."""

    assert NODE_TEST.is_file(), "web/tests/pacing.test.mjs is missing"
    assert "pacing_vectors.json" in NODE_TEST.read_text(encoding="utf-8")
