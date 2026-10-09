// Operator console.
//
// It consumes the same WebSocket and the same wire contract as the viewer page,
// but with role=admin, which is what makes the English source available. Nothing
// else differs, so the console cannot drift from what attendees actually see.

import { ViewerClient } from "./client.js";

const MAX_HISTORY = 30;

function makeAdminView() {
  const statusEl = document.getElementById("stat-status");
  const viewersEl = document.getElementById("stat-viewers");
  const droppedEl = document.getElementById("stat-dropped");
  const seqEl = document.getElementById("stat-seq");
  const renderEl = document.getElementById("stat-render");
  const deEl = document.getElementById("live-de");
  const srcEl = document.getElementById("live-src");
  const historyEl = document.getElementById("history");

  const history = [];

  function renderHistory() {
    historyEl.replaceChildren(
      ...history.map((entry) => {
        const item = document.createElement("li");
        const de = document.createElement("span");
        de.lang = "de";
        de.className = "admin-de";
        de.textContent = entry.de;
        const src = document.createElement("span");
        src.className = "admin-src";
        src.textContent = entry.src ?? "–";
        item.append(de, src);
        return item;
      }),
    );
  }

  return {
    offer(segment, receivedAt) {
      deEl.textContent = segment.de || "–";
      srcEl.textContent = segment.src || "(Quelle nicht verfügbar)";
      seqEl.textContent = `${segment.seq}.${segment.rev} ${segment.state}`;
      renderEl.textContent = `${Math.round(performance.now() - receivedAt)} ms`;

      // Only committed lines enter the history; drafts would just flicker.
      if (segment.state === "final") {
        history.unshift({ seq: segment.seq, de: segment.de, src: segment.src });
        history.length = Math.min(history.length, MAX_HISTORY);
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

async function sessionId() {
  const response = await fetch("/api/session");
  if (!response.ok) throw new Error(`no session: ${response.status}`);
  return (await response.json()).id;
}

async function main() {
  const view = makeAdminView();
  let id;

  try {
    id = await sessionId();
  } catch (error) {
    view.setStatus("stopped", String(error));
    return;
  }

  const client = new ViewerClient({
    sessionId: id,
    role: "admin",
    overlay: view,
    onError: (payload) => view.setStatus("delayed", `${payload.stage}: ${payload.message}`),
  });
  client.start();

  // Viewer count is not pushed on the wire; poll it. Once a second is cheap and
  // keeps the console honest about how many people are actually watching.
  window.setInterval(async () => {
    try {
      const response = await fetch(`/api/session/${encodeURIComponent(id)}`);
      if (!response.ok) return;
      view.setViewers((await response.json()).viewers);
    } catch {
      /* transient; the next tick will retry */
    }
  }, 1000);
}

main();
