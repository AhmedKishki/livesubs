// What the operator console makes of /api/stats: formatting, and the list of things to look at.
//
// Pure functions, no DOM, so they run in node exactly as in the browser. The server sends
// figures and the console decides which of them mean trouble; the thresholds are here, named,
// so changing what counts as trouble is a one-line edit with a test beside it.

export const THRESHOLDS = {
  //: Silence this long while people are watching is worth a look: a dead microphone and a
  //: long pause sound the same to the pipeline.
  silenceWarnS: 20,
  //: The event loop being this late (95th percentile) means the server is struggling.
  loopLagWarnMs: 100,
  loopLagCriticalMs: 250,
  //: Share of the open-file limit at which a new phone may be refused.
  openFilesWarnShare: 0.8,
  //: Viewers reporting this long from arrival to display means their device is struggling.
  renderWarnMs: 1500,
  //: The lowest level drawn on the meter, in dBFS.
  meterFloorDbfs: -80,
};

/** "1h 02m", "3m 12s", "45s", or "–" when there is nothing to show. */
export function fmtDuration(seconds) {
  if (!Number.isFinite(seconds)) return "–";
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(rest).padStart(2, "0")}s`;
  return `${rest}s`;
}

/** Bytes as "512 B", "3.4 KB", "1.2 MB". */
export function fmtBytes(bytes) {
  if (!Number.isFinite(bytes)) return "–";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Milliseconds as "420 ms" or "1.8 s", and "–" for no data. */
export function fmtMs(value) {
  if (!Number.isFinite(value)) return "–";
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;
}

/** A window summary as "median / 95th percentile", or "–" before the first sample. */
export function fmtSpread(summary) {
  if (!summary || summary.p50 == null) return "–";
  return `${fmtMs(summary.p50)} / ${fmtMs(summary.p95)}`;
}

/** Where a level sits on the meter, as a percentage of its width. */
export function levelPercent(dbfs) {
  if (!Number.isFinite(dbfs)) return 0;
  const floor = THRESHOLDS.meterFloorDbfs;
  return Math.round(Math.min(1, Math.max(0, (dbfs - floor) / -floor)) * 100);
}

const SEVERITY_ORDER = { critical: 0, warning: 1, info: 2 };

/**
 * The things the operator should look at, worst first.
 *
 * Each is `{ severity, text }`. Nothing here is a verdict about the lecture, only about the
 * service: a long silence is reported as silence, and the operator knows whether the speaker
 * is thinking or the microphone is unplugged.
 */
export function deriveAlerts(stats) {
  const alerts = [];
  const add = (severity, text) => alerts.push({ severity, text });
  if (!stats) return alerts;

  const { session, audience, pipeline, server } = stats;

  if (session?.failed) add("critical", "The pipeline stopped after an error. Restart the service.");

  for (const [scope, status] of Object.entries(session?.status ?? {})) {
    if (status.status === "live") continue;
    const where = scope === "all" ? "Subtitles" : `${scope.toUpperCase()} subtitles`;
    const detail = status.detail ? `: ${status.detail}` : "";
    add(status.status === "stopped" ? "critical" : "warning", `${where} ${status.status}${detail}`);
  }

  const audio = pipeline?.audio;
  if (audio) {
    if (audio.capturing === false) add("critical", "The microphone is no longer delivering audio.");
    if (audio.clipping) add("warning", "The input is clipping. Lower the capture level.");
    if (
      pipeline.recognition?.open &&
      Number.isFinite(audio.silence_s) &&
      audio.silence_s >= THRESHOLDS.silenceWarnS
    ) {
      add(
        "warning",
        `No speech for ${fmtDuration(audio.silence_s)} while people are watching. Check the microphone.`,
      );
    }
  }

  if (pipeline?.recognition?.losses > 0) {
    add("info", `Recognition dropped ${pipeline.recognition.losses} time(s) and was restarted.`);
  }

  for (const [code, language] of Object.entries(pipeline?.languages ?? {})) {
    const name = code.toUpperCase();
    if (language.failed > 0) {
      add("warning", `${name} translation failed for ${language.failed} line(s).`);
    }
    if (language.shed > 0) {
      add("warning", `${name} translation fell behind and dropped ${language.shed} line(s).`);
    }
  }

  const lag = server?.loop_lag_ms?.p95;
  if (Number.isFinite(lag) && lag >= THRESHOLDS.loopLagWarnMs) {
    add(
      lag >= THRESHOLDS.loopLagCriticalMs ? "critical" : "warning",
      `The server is falling behind: its event loop runs ${fmtMs(lag)} late.`,
    );
  }

  if (
    Number.isFinite(server?.open_files) &&
    Number.isFinite(server?.open_files_limit) &&
    server.open_files >= server.open_files_limit * THRESHOLDS.openFilesWarnShare
  ) {
    add(
      "warning",
      `${server.open_files} of ${server.open_files_limit} files are open. New phones will be refused at the limit.`,
    );
  }

  if (audience?.skipped_lines > 0) {
    add("info", `${audience.skipped_lines} line(s) were skipped for phones that could not keep up.`);
  }

  for (const [code, render] of Object.entries(audience?.render_ms ?? {})) {
    if (Number.isFinite(render.p95) && render.p95 >= THRESHOLDS.renderWarnMs) {
      add("warning", `Phones take ${fmtMs(render.p95)} to show a ${code.toUpperCase()} line.`);
    }
  }

  return alerts.sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);
}

/** The worst status among the scopes, or `live` when there are none. */
export function overallStatus(session) {
  const order = { live: 0, delayed: 1, stopped: 2 };
  let worst = session?.failed ? "stopped" : "live";
  for (const { status } of Object.values(session?.status ?? {})) {
    if ((order[status] ?? 0) > order[worst]) worst = status;
  }
  return worst;
}
