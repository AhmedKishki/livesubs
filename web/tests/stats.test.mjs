// What the operator console makes of the server's figures.
//
// Run with:  node --test web/tests/*.test.mjs

import assert from "node:assert/strict";
import test from "node:test";

import {
  THRESHOLDS,
  deriveAlerts,
  fmtBytes,
  fmtDuration,
  fmtMs,
  fmtSpread,
  levelPercent,
  overallStatus,
} from "../static/stats.js";

// --- formatting ---------------------------------------------------------------------------

test("durations read as the largest sensible units", () => {
  assert.equal(fmtDuration(0), "0s");
  assert.equal(fmtDuration(45.4), "45s");
  assert.equal(fmtDuration(192), "3m 12s");
  assert.equal(fmtDuration(3725), "1h 02m");
  assert.equal(fmtDuration(-5), "0s");
});

test("missing figures read as a dash, never as a zero that looks like a measurement", () => {
  assert.equal(fmtDuration(null), "–");
  assert.equal(fmtDuration(undefined), "–");
  assert.equal(fmtBytes(null), "–");
  assert.equal(fmtMs(null), "–");
  assert.equal(fmtSpread({ p50: null, p95: null }), "–");
  assert.equal(fmtSpread(undefined), "–");
});

test("bytes and milliseconds pick a readable unit", () => {
  assert.equal(fmtBytes(512), "512 B");
  assert.equal(fmtBytes(3500), "3.4 KB");
  assert.equal(fmtBytes(5 * 1024 * 1024), "5.0 MB");
  assert.equal(fmtMs(420), "420 ms");
  assert.equal(fmtMs(1800), "1.8 s");
  assert.equal(fmtSpread({ p50: 420, p95: 1800 }), "420 ms / 1.8 s");
});

test("the meter maps the usable range of dBFS onto its width", () => {
  assert.equal(levelPercent(0), 100);
  assert.equal(levelPercent(THRESHOLDS.meterFloorDbfs), 0);
  assert.equal(levelPercent(-40), 50);
  assert.equal(levelPercent(-200), 0, "below the floor stays on the scale");
  assert.equal(levelPercent(5), 100, "above full scale stays on the scale");
  assert.equal(levelPercent(null), 0);
});

// --- what to look at ----------------------------------------------------------------------

function healthy() {
  return {
    session: { failed: false, status: { all: { status: "live", detail: null } } },
    audience: { skipped_lines: 0, render_ms: { de: { p95: 80 } } },
    pipeline: {
      audio: { capturing: true, clipping: false, silence_s: 0 },
      recognition: { open: true, losses: 0 },
      languages: { de: { failed: 0, shed: 0 }, ar: { failed: 0, shed: 0 } },
    },
    server: { loop_lag_ms: { p95: 4 }, open_files: 40, open_files_limit: 1024 },
  };
}

const texts = (stats) => deriveAlerts(stats).map((alert) => alert.text);

test("a healthy session has nothing to look at", () => {
  assert.deepEqual(deriveAlerts(healthy()), []);
  assert.deepEqual(deriveAlerts(null), []);
  assert.deepEqual(deriveAlerts({}), []);
});

test("a simulation, which has no pipeline, is not an alert", () => {
  const stats = healthy();
  stats.pipeline = { kind: "simulation" };

  assert.deepEqual(deriveAlerts(stats), []);
});

test("a failed pipeline is critical and comes first", () => {
  const stats = healthy();
  stats.session.failed = true;
  stats.pipeline.audio.clipping = true;

  const alerts = deriveAlerts(stats);

  assert.equal(alerts[0].severity, "critical");
  assert.match(alerts[0].text, /pipeline stopped/i);
  assert.equal(alerts.at(-1).severity, "warning");
});

test("a language that is delayed is named, with the server's detail", () => {
  const stats = healthy();
  stats.session.status.ar = { status: "delayed", detail: "translation is behind" };

  assert.deepEqual(texts(stats), ["AR subtitles delayed: translation is behind"]);
});

test("a stopped status is critical and a delayed one is a warning", () => {
  const stats = healthy();
  stats.session.status.all = { status: "stopped", detail: "publisher failed" };
  stats.session.status.de = { status: "delayed", detail: null };

  const alerts = deriveAlerts(stats);

  assert.deepEqual(
    alerts.map((alert) => alert.severity),
    ["critical", "warning"],
  );
});

test("capture stopping, clipping and a long silence are each reported", () => {
  const stats = healthy();
  stats.pipeline.audio = { capturing: false, clipping: true, silence_s: THRESHOLDS.silenceWarnS };

  const found = texts(stats).join("\n");

  assert.match(found, /no longer delivering audio/);
  assert.match(found, /clipping/);
  assert.match(found, /No speech for 20s/);
});

test("silence is only worth reporting while recognition is running", () => {
  const stats = healthy();
  stats.pipeline.audio.silence_s = 600;
  stats.pipeline.recognition.open = false;

  assert.deepEqual(deriveAlerts(stats), [], "paused with an empty room, the silence is expected");
});

test("a short silence is not reported", () => {
  const stats = healthy();
  stats.pipeline.audio.silence_s = THRESHOLDS.silenceWarnS - 1;

  assert.deepEqual(deriveAlerts(stats), []);
});

test("translation failures and shed lines are reported per language", () => {
  const stats = healthy();
  stats.pipeline.languages.ar = { failed: 3, shed: 2 };

  assert.deepEqual(texts(stats), [
    "AR translation failed for 3 line(s).",
    "AR translation fell behind and dropped 2 line(s).",
  ]);
});

test("the event loop running late is a warning, then critical", () => {
  const stats = healthy();

  stats.server.loop_lag_ms.p95 = THRESHOLDS.loopLagWarnMs;
  assert.equal(deriveAlerts(stats)[0].severity, "warning");

  stats.server.loop_lag_ms.p95 = THRESHOLDS.loopLagCriticalMs;
  assert.equal(deriveAlerts(stats)[0].severity, "critical");

  stats.server.loop_lag_ms.p95 = THRESHOLDS.loopLagWarnMs - 1;
  assert.deepEqual(deriveAlerts(stats), []);
});

test("nearing the open-file limit warns before phones are refused", () => {
  const stats = healthy();
  stats.server.open_files = 820;

  assert.match(texts(stats)[0], /820 of 1024 files are open/);

  stats.server.open_files = 800;
  assert.deepEqual(deriveAlerts(stats), [], "under the share, nothing to report");
});

test("phones that cannot keep up, and phones that are slow to draw, are reported", () => {
  const stats = healthy();
  stats.audience.skipped_lines = 4;
  stats.audience.render_ms.de.p95 = THRESHOLDS.renderWarnMs;

  assert.deepEqual(texts(stats), [
    "Phones take 1.5 s to show a DE line.",
    "4 line(s) were skipped for phones that could not keep up.",
  ]);
});

test("recognition having dropped is information, not an alarm", () => {
  const stats = healthy();
  stats.pipeline.recognition.losses = 2;

  const [alert] = deriveAlerts(stats);

  assert.equal(alert.severity, "info");
});

test("the overall status is the worst of the scopes", () => {
  assert.equal(overallStatus(undefined), "live");
  assert.equal(overallStatus({ status: { all: { status: "live" } } }), "live");
  assert.equal(
    overallStatus({ status: { all: { status: "live" }, ar: { status: "delayed" } } }),
    "delayed",
  );
  assert.equal(
    overallStatus({ status: { de: { status: "delayed" }, all: { status: "stopped" } } }),
    "stopped",
  );
  assert.equal(overallStatus({ failed: true, status: {} }), "stopped");
});
