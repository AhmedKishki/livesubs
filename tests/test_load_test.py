"""The load tool's arithmetic, on arrivals made up for the purpose.

The tool itself needs a running server and a few hundred sockets, so what is pinned here is
the part that can mislead: how arrivals become a spread, and what counts as a delivered line.
A tool that reports a clean run for a bad server is worse than none.
"""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "load_test", Path(__file__).resolve().parent.parent / "tools" / "load_test.py"
)
assert SPEC is not None and SPEC.loader is not None
load_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(load_test)


def result(arrivals: dict, **overrides) -> dict:
    base = {
        "arrivals": arrivals,
        "connect_ms": [1.0, 2.0],
        "connected": 2,
        "finished": 2,
        "failed": {},
        "dropped": {},
    }
    return {**base, **overrides}


def test_percentiles_are_nearest_rank() -> None:
    values = list(range(1, 101))

    assert load_test._percentile(values, 0.5) == 50
    assert load_test._percentile(values, 0.95) == 95
    assert load_test._percentile([7.0], 0.95) == 7.0


def test_arrivals_from_every_process_are_merged_per_message() -> None:
    key = ("de", 1, 1, "final")
    merged = load_test._merge(
        [
            result({key: [3, 10.0, 10.5]}),
            result({key: [2, 9.9, 10.2]}, connected=5, failed={"OSError": 1}),
        ]
    )

    assert merged["arrivals"][key] == [5, 9.9, 10.5]
    assert merged["connected"] == 7
    assert merged["failed"] == Counter({"OSError": 1})


def report(arrivals: dict, wanted: dict, steady_from: float = 0.0) -> dict:
    merged = load_test._merge([result(arrivals)])
    return load_test._report(merged, Counter(wanted), {}, steady_from)


def test_the_spread_is_first_phone_to_last_in_milliseconds() -> None:
    out = report({("de", 1, 1, "final"): [10, 5.000, 5.040]}, {"de": 10})

    assert out["fanout_spread_ms_max"] == 40.0
    assert out["fanout_spread_ms_p50"] == 40.0


def test_a_final_that_reached_everyone_is_delivered() -> None:
    out = report(
        {("de", 1, 1, "final"): [8, 1.0, 1.1], ("ar", 1, 1, "final"): [2, 1.0, 1.1]},
        {"de": 8, "ar": 2},
    )

    assert out["final_delivery_share"] == 1.0
    assert out["finals_fully_delivered"] == 2


def test_a_final_that_missed_phones_lowers_the_share() -> None:
    out = report({("de", 1, 1, "final"): [6, 1.0, 1.1]}, {"de": 8})

    assert out["final_delivery_share"] == 0.75
    assert out["finals_fully_delivered"] == 0


def test_drafts_are_not_expected_to_reach_everyone() -> None:
    """A draft can be replaced before it is sent, which is the design, not a loss."""

    out = report(
        {("de", 1, 0, "draft"): [1, 1.0, 1.0], ("de", 1, 1, "final"): [8, 1.0, 1.1]},
        {"de": 8},
    )

    assert out["final_delivery_share"] == 1.0
    assert out["finals_published"] == 1


def test_lines_published_while_phones_were_still_joining_are_not_judged() -> None:
    early = {("de", 0, 1, "final"): [2, 1.0, 1.1]}
    steady = {("de", 5, 1, "final"): [8, 9.0, 9.1]}

    out = report({**early, **steady}, {"de": 8}, steady_from=5.0)

    assert out["messages_published"] == 1
    assert out["final_delivery_share"] == 1.0


def test_a_run_with_no_steady_lines_says_nothing_rather_than_everything_is_fine() -> None:
    out = report({("de", 0, 1, "final"): [2, 1.0, 1.1]}, {"de": 8}, steady_from=50.0)

    assert out["final_delivery_share"] is None
    assert out["fanout_spread_ms_max"] is None


@pytest.mark.parametrize("phones", [1, 7])
def test_more_deliveries_than_phones_cannot_push_the_share_over_one(phones: int) -> None:
    out = report({("de", 1, 1, "final"): [phones + 3, 1.0, 1.1]}, {"de": phones})

    assert out["final_delivery_share"] == 1.0
