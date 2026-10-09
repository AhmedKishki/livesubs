// Display-side pacing: how long a line stays up, and what to do when the next
// one arrives before the reader is finished.
//
// holdMs() MIRRORS hold_ms() in src/livesubs/segmenter.py. The two must agree;
// spec/pacing_vectors.json is generated from the Python implementation and
// tests/test_pacing_parity.py + web/tests/pacing.test.mjs both check against it,
// so they cannot silently drift.
//
// CORRECTION to an earlier version of this policy (docs/06-overlay-spec.md):
// the replacement rule previously used 60% of the full reading-time hold as its
// threshold. That is wrong, and would have discarded almost every line, because
// for a 60-character German line the hold is ~3.7 s and 60% of it (~2.2 s)
// exceeds the 2 s segment cadence. Two separate concepts were being conflated:
//
//   * holdMs()        - how long to KEEP a line when nothing else is ready.
//                       This is a reading-comfort target, not a gate.
//   * MIN_VISIBLE_MS  - how long a line must stay before it may be REPLACED.
//                       This is an anti-flicker floor, and it is small.
//
// So a line is replaced as soon as it has been up for MIN_VISIBLE_MS, and only
// retired on the hold when the speaker stops. At the 2 s segment cap the display
// can always keep up, so the DROP action is normally dormant; it exists for
// bursts (after a stall or a reconnect flush) and for sub-second cadences.

export const BASE_HOLD_MS = 400;
export const PER_CHAR_MS = 55;
export const MIN_HOLD_MS = 700;
export const MAX_HOLD_MS = 6000;

/** How long a line should stay on screen when nothing else is ready. */
export function holdMs(chars) {
  const count = Number.isFinite(chars) ? Math.max(0, Math.trunc(chars)) : 0;
  return Math.max(MIN_HOLD_MS, Math.min(MAX_HOLD_MS, PER_CHAR_MS * count + BASE_HOLD_MS));
}

/**
 * Anti-flicker floor: the shortest time a line may be on screen before a new one
 * is allowed to replace it. Roughly the point where a swap stops looking broken.
 */
export const MIN_VISIBLE_MS = 800;

export const Action = Object.freeze({
  IGNORE: "ignore",   // stale revision, or a segment we already have
  UPDATE: "update",   // same seq, newer rev: replace the text in place
  REPLACE: "replace", // a new line may take the screen now
  DROP: "drop",       // a new line arrived too early; discard it rather than queue
});

/**
 * What to do with an incoming segment.
 *
 * `current` is `{ seq, rev, chars, shownAt, finalAt }` or null when nothing is
 * displayed. Pure: no timers, no DOM, so it is unit-testable in node.
 */
export function decideSegment(current, incoming, now) {
  if (incoming == null) return Action.IGNORE;
  if (current == null) return Action.REPLACE;

  if (incoming.seq === current.seq) {
    return incoming.rev > current.rev ? Action.UPDATE : Action.IGNORE;
  }
  if (incoming.seq < current.seq) return Action.IGNORE;

  // A genuinely new line. If the current one never settled, do not hold it
  // hostage to a hold timer that will not start until it does.
  if (current.finalAt === null) return Action.REPLACE;

  return now - current.shownAt >= MIN_VISIBLE_MS ? Action.REPLACE : Action.DROP;
}

/** Whether a displayed line has now been up long enough to retire. */
export function shouldRetire(current, now) {
  if (current == null || current.finalAt === null) return false;
  return now - current.finalAt >= holdMs(current.chars);
}
