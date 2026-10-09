// Parity and behaviour tests for the overlay's pacing decisions.
//
// Run with:  cd web && node --test tests/
//
// Node is a DEVELOPMENT-ONLY dependency. The browser runs these ES modules
// directly; nothing is bundled, built or installed for deployment.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

import * as pacing from "../static/pacing.js";

const { Action, MIN_VISIBLE_MS, decideSegment, holdMs, shouldRetire } = pacing;

const here = dirname(fileURLToPath(import.meta.url));
const vectors = JSON.parse(
  readFileSync(join(here, "..", "..", "spec", "pacing_vectors.json"), "utf8"),
);

// --- parity with the Python implementation ---------------------------------

test("holdMs matches every shared vector", () => {
  vectors.inputs.forEach((raw, index) => {
    assert.equal(holdMs(raw), vectors.expected[index], `holdMs(${raw})`);
  });
});

test("constants match the shared vectors", () => {
  for (const [name, value] of Object.entries(vectors.constants)) {
    assert.equal(pacing[name], value, name);
  }
});

test("truncation agrees with Python for non-integer input", () => {
  // 6.7 appears in the vectors; both sides must truncate toward zero, not round.
  const index = vectors.inputs.indexOf(6.7);
  assert.ok(index >= 0, "the vectors should include a non-integer input");
  assert.equal(holdMs(6.7), vectors.expected[index]);
  assert.equal(holdMs(6.7), holdMs(6));
});

// --- replacement, updates and drops ----------------------------------------

test("a first segment replaces an empty screen", () => {
  assert.equal(decideSegment(null, { seq: 0, rev: 0 }, 1000), Action.REPLACE);
});

test("a newer revision updates the current line in place", () => {
  const current = { seq: 4, rev: 0, chars: 40, shownAt: 0, finalAt: null };
  assert.equal(decideSegment(current, { seq: 4, rev: 1 }, 50), Action.UPDATE);
});

test("a stale revision of the same seq is ignored", () => {
  const current = { seq: 4, rev: 2, chars: 40, shownAt: 0, finalAt: 100 };
  assert.equal(decideSegment(current, { seq: 4, rev: 1 }, 200), Action.IGNORE);
});

test("an older seq is ignored, so final can never be followed by draft", () => {
  const current = { seq: 9, rev: 0, chars: 40, shownAt: 0, finalAt: 100 };
  assert.equal(decideSegment(current, { seq: 3, rev: 0 }, 200), Action.IGNORE);
});

test("a new line replaces once the anti-flicker floor has elapsed", () => {
  const current = { seq: 1, rev: 1, chars: 60, shownAt: 0, finalAt: 600 };
  assert.equal(decideSegment(current, { seq: 2, rev: 0 }, MIN_VISIBLE_MS), Action.REPLACE);
});

test("a new line arriving inside the floor is dropped, not queued", () => {
  const current = { seq: 1, rev: 1, chars: 60, shownAt: 0, finalAt: 100 };
  assert.equal(
    decideSegment(current, { seq: 2, rev: 0 }, MIN_VISIBLE_MS - 1),
    Action.DROP,
  );
});

test("a line that never settled is not held hostage to its hold timer", () => {
  const current = { seq: 1, rev: 0, chars: 60, shownAt: 0, finalAt: null };
  assert.equal(decideSegment(current, { seq: 2, rev: 0 }, 10), Action.REPLACE);
});

// --- regression guard for the corrected pacing policy ----------------------

test("the 2 s segment cap can always keep up: the default cadence never drops", () => {
  // The earlier policy used 60% of the READING hold as its replace threshold,
  // which for a 60-character line is ~2.2 s - longer than the 2 s cadence. That
  // would have discarded nearly every line. This test pins the fix.
  const current = { seq: 1, rev: 1, chars: 60, shownAt: 0, finalAt: 600 };
  assert.equal(decideSegment(current, { seq: 2, rev: 0 }, 2000), Action.REPLACE);
});

test("a sub-second cadence does drop, so the rule is reachable", () => {
  const current = { seq: 1, rev: 1, chars: 60, shownAt: 0, finalAt: 600 };
  assert.equal(decideSegment(current, { seq: 2, rev: 0 }, 700), Action.DROP);
});

// --- retirement -------------------------------------------------------------

test("a line retires only after its reading hold, and only once final", () => {
  const chars = 60;
  const current = { seq: 1, rev: 1, chars, shownAt: 0, finalAt: 1000 };

  assert.equal(shouldRetire(current, 1000 + holdMs(chars) - 1), false);
  assert.equal(shouldRetire(current, 1000 + holdMs(chars)), true);
  assert.equal(shouldRetire({ ...current, finalAt: null }, 999_999), false);
  assert.equal(shouldRetire(null, 999_999), false);
});
