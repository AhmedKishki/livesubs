// Landing page: fill in the session name, the QR image and the plain URL.
//
// Server-side templating is deliberately avoided; the session metadata comes
// from /api/session so these pages can be static files served straight off disk.
// The page speaks the session's default language, because that is the language a
// scan of its QR code opens.

import { directionOf, stringsFor } from "./language.js";

function note(text) {
  const item = document.createElement("li");
  item.textContent = text;
  return item;
}

async function main() {
  const nameEl = document.getElementById("session-name");
  const qrEl = document.getElementById("qr");
  const urlEl = document.getElementById("join-url");
  const errorEl = document.getElementById("qr-error");
  const notesEl = document.getElementById("join-notes");

  let strings = stringsFor(null);
  const showError = () => {
    qrEl.hidden = true;
    errorEl.textContent = strings.join.qrError;
    errorEl.hidden = false;
  };

  qrEl.addEventListener("error", showError);
  qrEl.src = "/qr.png";

  try {
    const [session, qrUrl] = await Promise.all([
      fetch("/api/session").then((response) => response.json()),
      fetch("/api/qr-url").then((response) => response.json()),
    ]);

    const language = session.default;
    strings = stringsFor(language);
    document.documentElement.lang = language;
    document.documentElement.dir = directionOf(language, session);
    qrEl.alt = strings.join.qrAlt;

    document.title = strings.join.title;
    if (session.name) nameEl.textContent = session.name;
    if (qrUrl?.url) {
      urlEl.href = qrUrl.url;
      urlEl.textContent = qrUrl.url;
    }
    if (session.viewers != null) {
      document.title = `${session.name} – ${session.viewers} ${strings.join.connected}`;
    }

    // What the service does with a device, and nothing it does not do. The recording
    // line appears only when this run keeps the audio.
    notesEl.replaceChildren(
      note(strings.join.live),
      note(strings.join.noAccount),
      note(strings.join.stored),
      ...(session.recording ? [note(strings.join.recorded)] : []),
    );
  } catch {
    showError();
  }
}

main();
