// Operator console.
//
// Two feeds, deliberately separate. The WebSocket is the same one the viewers use, with
// role=admin, which is what makes the speaker's own words and every language of the session
// available: it drives the live lines and the history. The figures come from /api/stats once
// a second: audience, signal, recognition, translators and the host. Neither needs the
// other, so a console whose socket has dropped still shows the numbers, and the reverse.

import { ViewerClient } from "./client.js";
import { bindSessionControls } from "./controls.js";
import { directionOf } from "./language.js";
import {
  deriveAlerts,
  fmtBytes,
  fmtDuration,
  fmtSpread,
  levelPercent,
  overallStatus,
} from "./stats.js";

const MAX_HISTORY = 30;
const MAX_ERRORS = 8;
const POLL_MS = 1000;

function line(session, language, text, className) {
  const element = document.createElement("p");
  element.className = className;
  element.lang = language;
  element.dir = directionOf(language, session);
  element.textContent = text;
  return element;
}

/** Replace a definition list's rows. A row is `[label, value]` or `[label, value, tone]`. */
function setRows(list, rows) {
  list.replaceChildren(
    ...rows.map(([label, value, tone]) => {
      const row = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = label;
      const detail = document.createElement("dd");
      detail.textContent = String(value);
      if (tone) detail.dataset.tone = tone;
      row.append(term, detail);
      return row;
    }),
  );
}

function cell(text, tone) {
  const element = document.createElement("td");
  element.textContent = String(text);
  if (tone) element.dataset.tone = tone;
  return element;
}

function makeAdminView(session) {
  const $ = (id) => document.getElementById(id);
  const liveEl = $("live-lines");
  const srcEl = $("live-src");
  const historyEl = $("history");
  const errorsEl = $("errors");
  const controls = bindSessionControls(session.id, $);
  controls.reconcile(session);

  const names = Object.fromEntries((session.targets ?? []).map((t) => [t.code, t.name]));
  const nameOf = (code) => names[code] ?? code.toUpperCase();

  // A conversation's source names every language spoken, joined by "+".
  const source = (session.source ?? "").toUpperCase();
  $("mode").textContent =
    `${source} ${source.includes("+") ? "conversation" : "speaker"} → ` +
    session.languages.map((code) => nameOf(code)).join(", ");

  // One row per language, in the order the session offers them, each labelled with the
  // language's own name so the operator can see which line is which.
  const liveLines = new Map();
  for (const language of session.languages) {
    const label = document.createElement("span");
    label.className = "admin-lang";
    label.textContent = nameOf(language);
    const text = line(session, language, "–", "admin-line");
    liveEl.append(label, text);
    liveLines.set(language, text);
  }

  // A line is committed in every language separately, so a history row is keyed by the
  // segment's `seq` and collects whichever translations have arrived for it.
  const history = new Map();
  const errors = [];

  function renderHistory() {
    const rows = [...history.values()].sort((a, b) => b.seq - a.seq);
    historyEl.replaceChildren(
      ...rows.map((entry) => {
        const item = document.createElement("li");
        for (const language of session.languages) {
          item.append(line(session, language, entry.lines[language] ?? "…", "admin-line"));
        }
        const src = document.createElement("span");
        src.className = "admin-src";
        src.textContent = entry.src ?? "–";
        item.append(src);
        return item;
      }),
    );
  }

  function renderErrors() {
    errorsEl.replaceChildren(
      ...errors.map((entry) => {
        const item = document.createElement("li");
        item.className = "admin-error";
        item.textContent = `${entry.at}  ${entry.stage}: ${entry.message}`;
        return item;
      }),
    );
  }

  function renderAlerts(stats) {
    const alerts = deriveAlerts(stats);
    $("alerts").replaceChildren(
      ...alerts.map(({ severity, text }) => {
        const item = document.createElement("p");
        item.className = "admin-alert";
        item.dataset.severity = severity;
        item.textContent = text;
        return item;
      }),
    );
  }

  function renderAudience(audience) {
    const perLanguage = session.languages
      .map((code) => `${nameOf(code)} ${audience.viewers_by_language?.[code] ?? 0}`)
      .join(" · ");
    setRows($("audience"), [
      ["Viewers now", audience.viewers],
      ["By language", perLanguage],
      ["Peak", audience.peak_viewers],
      ["Operators", audience.operators],
      ["Joined / left", `${audience.connects} / ${audience.disconnects}`],
      ["Lines dropped by phones", audience.dropped_lines, audience.dropped_lines ? "warn" : null],
      ["Lines skipped for slow phones", audience.skipped_lines, audience.skipped_lines ? "warn" : null],
    ]);
  }

  function renderAudio(pipeline) {
    const level = $("meter-level");
    const peak = $("meter-peak");
    const floor = $("meter-floor");
    const audio = pipeline?.audio;

    if (!audio) {
      level.style.width = "0%";
      peak.style.left = "0%";
      floor.style.left = "0%";
      setRows($("audio"), [
        [
          "Input",
          pipeline ? "waiting for audio" : "none (simulation)",
          pipeline ? null : "muted",
        ],
      ]);
      return;
    }

    level.style.width = `${levelPercent(audio.rms_dbfs)}%`;
    level.dataset.state = audio.clipping ? "clip" : audio.speech ? "speech" : "quiet";
    peak.style.left = `${levelPercent(audio.peak_dbfs)}%`;
    floor.style.left = `${levelPercent(audio.noise_floor_dbfs)}%`;

    setRows($("audio"), [
      ["Level", `${audio.rms_dbfs} dBFS (peak ${audio.peak_dbfs})`],
      ["Noise floor", `${audio.noise_floor_dbfs} dBFS`],
      ["Voice", audio.speech ? "speech" : `silent for ${fmtDuration(audio.silence_s)}`],
      ["Clipping", audio.clipping ? "yes" : "no", audio.clipping ? "bad" : null],
      ["Capture", audio.capturing ? "running" : "stopped", audio.capturing ? null : "bad"],
      ["Audio clock", fmtDuration(audio.audio_s)],
    ]);
  }

  function renderRecognition(pipeline, viewers) {
    const recognition = pipeline?.recognition;
    if (!recognition) {
      setRows($("recognition"), [["State", pipeline ? "–" : "none (simulation)", "muted"]]);
      return;
    }
    const state = recognition.open
      ? recognition.healthy === false
        ? "open, not healthy"
        : "open"
      : viewers > 0
        ? "starting"
        : "paused (nobody watching)";
    setRows($("recognition"), [
      ["State", state, recognition.open ? null : "muted"],
      ["Paid transcription time", fmtDuration(recognition.open_s)],
      ["Streams opened", recognition.streams_opened],
      ["Times lost", recognition.losses, recognition.losses ? "warn" : null],
      ["Lines produced", pipeline.segments.committed],
    ]);
  }

  function renderServer(server, audience) {
    const limit = server.open_files_limit;
    setRows($("server"), [
      ["Event loop lateness", fmtSpread(server.loop_lag_ms)],
      ["Processor", `${server.cpu_percent}%`],
      ["Memory", server.rss_mb == null ? "–" : `${server.rss_mb} MB`],
      ["Open files", limit ? `${server.open_files} of ${limit}` : server.open_files ?? "–"],
      ["Sent", `${audience.messages_sent} messages, ${fmtBytes(audience.bytes_sent)}`],
      ["Time to write a batch", fmtSpread(audience.send_ms)],
    ]);
  }

  function renderLanguages(stats) {
    const body = document.querySelector("#languages tbody");
    const details = stats.pipeline?.languages;

    body.replaceChildren(
      ...session.languages.map((code) => {
        const row = document.createElement("tr");
        const render = stats.audience.render_ms?.[code];
        const readers = stats.audience.viewers_by_language?.[code] ?? 0;
        const scope = stats.session.status?.[code];
        const trouble = scope && scope.status !== "live";

        if (!details) {
          row.append(cell(nameOf(code)), cell(readers), cell("–", "muted"));
          row.append(...Array.from({ length: 6 }, () => cell("–", "muted")));
          row.append(cell(fmtSpread(render)));
          return row;
        }

        const detail = details[code];
        const state = trouble ? scope.status : detail.active ? "running" : "idle";
        row.append(
          cell(nameOf(code)),
          cell(detail.readers),
          cell(state, trouble ? "bad" : detail.active ? null : "muted"),
          cell(detail.queue, detail.queue >= 3 ? "warn" : null),
          cell(detail.completed),
          cell(detail.failed, detail.failed ? "bad" : null),
          cell(fmtSpread(detail.latency_ms)),
          cell(fmtSpread(detail.first_word_ms)),
          cell(fmtSpread(detail.translate_ms)),
          cell(fmtSpread(render)),
        );
        return row;
      }),
    );
  }

  return {
    offer(segment) {
      const target = liveLines.get(segment.language);
      if (target === undefined) return;

      target.textContent = segment.text || "–";
      if (segment.src) srcEl.textContent = segment.src;

      // Only committed lines enter the history; drafts would just flicker.
      if (segment.state === "final") {
        const entry = history.get(segment.seq) ?? { seq: segment.seq, src: null, lines: {} };
        entry.lines[segment.language] = segment.text;
        entry.src ??= segment.src;
        history.set(segment.seq, entry);
        for (const old of [...history.keys()].sort((a, b) => b - a).slice(MAX_HISTORY)) {
          history.delete(old);
        }
        renderHistory();
      }
    },

    // Health arrives with the figures, per language, so the socket's own copy of it is not
    // used. The console is the one place that reads it from /api/stats instead.
    setStatus() {},

    addError(payload) {
      errors.unshift({
        at: new Date().toLocaleTimeString([], { hour12: false }),
        stage: payload.stage,
        message: payload.message,
      });
      errors.length = Math.min(errors.length, MAX_ERRORS);
      renderErrors();
    },

    renderStats(stats) {
      controls.reconcile(stats.session);
      const status = overallStatus(stats.session);
      const overall = $("overall");
      overall.dataset.status = status;
      overall.textContent = `${status} · up ${fmtDuration(stats.session.uptime_s)}${
        stats.session.recording ? " · recording" : ""
      }`;

      renderAlerts(stats);
      renderAudience(stats.audience);
      renderAudio(stats.pipeline);
      renderRecognition(stats.pipeline, stats.audience.viewers);
      renderServer(stats.server, stats.audience);
      renderLanguages(stats);
    },

    unreachable() {
      const overall = $("overall");
      overall.dataset.status = "stopped";
      overall.textContent = "server unreachable";
    },
  };
}

async function loadSession() {
  const response = await fetch("/api/session");
  if (!response.ok) throw new Error(`no session: ${response.status}`);
  return response.json();
}

async function main() {
  let session;

  try {
    session = await loadSession();
  } catch (error) {
    document.getElementById("overall").textContent = `stopped – ${error}`;
    return;
  }

  const view = makeAdminView(session);
  const client = new ViewerClient({
    sessionId: session.id,
    role: "admin",
    overlay: view,
    onError: (payload) => view.addError(payload),
  });
  client.start();

  // Once a second is cheap, and keeps the console honest about who is watching and how the
  // service is coping. A failed poll says so rather than leaving the last figures looking
  // current.
  async function poll() {
    try {
      const response = await fetch("/api/stats", { cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      view.renderStats(await response.json());
    } catch {
      view.unreachable();
    }
  }
  await poll();
  window.setInterval(poll, POLL_MS);
}

main();
