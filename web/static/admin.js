// Operator console.
//
// It consumes the same WebSocket and the same wire contract as the viewer page,
// but with role=admin, which is what makes the speaker's own words and every language
// of the session available. Nothing else differs, so the console cannot drift from
// what attendees actually see.

import { ViewerClient } from "./client.js";
import { directionOf, nativeNameOf } from "./language.js";

const MAX_HISTORY = 30;

function line(session, language, text, className) {
  const element = document.createElement("p");
  element.className = className;
  element.lang = language;
  element.dir = directionOf(language, session);
  element.textContent = text;
  return element;
}

function makeAdminView(session) {
  const statusEl = document.getElementById("stat-status");
  const viewersEl = document.getElementById("stat-viewers");
  const droppedEl = document.getElementById("stat-dropped");
  const seqEl = document.getElementById("stat-seq");
  const renderEl = document.getElementById("stat-render");
  const liveEl = document.getElementById("live-lines");
  const srcEl = document.getElementById("live-src");
  const historyEl = document.getElementById("history");

  // One row per language, in the order the session offers them, each labelled with the
  // language's own name so the operator can see which line is which.
  const liveLines = new Map();
  for (const language of session.languages) {
    const label = document.createElement("span");
    label.className = "admin-lang";
    label.textContent = nativeNameOf(language, session);
    const text = line(session, language, "–", "admin-line");
    liveEl.append(label, text);
    liveLines.set(language, text);
  }

  // A line is committed in every language separately, so a history row is keyed by the
  // segment's `seq` and collects whichever translations have arrived for it.
  const history = new Map();

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

  return {
    offer(segment, receivedAt) {
      const target = liveLines.get(segment.language);
      if (target === undefined) return;

      target.textContent = segment.text || "–";
      if (segment.src) srcEl.textContent = segment.src;
      seqEl.textContent = `${segment.seq}.${segment.rev} ${segment.state}`;
      renderEl.textContent = `${Math.round(performance.now() - receivedAt)} ms`;

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

    setStatus(status, detail) {
      statusEl.textContent = detail ? `${status} – ${detail}` : status;
      statusEl.dataset.status = status;
    },

    setViewers(count) {
      viewersEl.textContent = String(count);
    },

    setDropped(count) {
      droppedEl.textContent = String(count);
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
    document.getElementById("stat-status").textContent = `stopped – ${error}`;
    return;
  }

  const view = makeAdminView(session);
  const client = new ViewerClient({
    sessionId: session.id,
    role: "admin",
    overlay: view,
    onError: (payload) => view.setStatus("delayed", `${payload.stage}: ${payload.message}`),
  });
  client.start();

  // Viewer count is not pushed on the wire; poll it. Once a second is cheap and
  // keeps the console honest about how many people are actually watching.
  window.setInterval(async () => {
    try {
      const response = await fetch(`/api/session/${encodeURIComponent(session.id)}`);
      if (!response.ok) return;
      view.setViewers((await response.json()).viewers);
    } catch {
      /* transient; the next tick will retry */
    }
  }, 1000);
}

main();
