// Landing page: fill in the session name, the QR image and the plain URL.
//
// Server-side templating is deliberately avoided; the session metadata comes
// from /api/session so these pages can be static files served straight off disk.

async function main() {
  const nameEl = document.getElementById("session-name");
  const qrEl = document.getElementById("qr");
  const urlEl = document.getElementById("join-url");
  const errorEl = document.getElementById("qr-error");

  qrEl.addEventListener("error", () => {
    qrEl.hidden = true;
    errorEl.hidden = false;
  });
  qrEl.src = "/qr.png";

  try {
    const [session, qrUrl] = await Promise.all([
      fetch("/api/session").then((response) => response.json()),
      fetch("/api/qr-url").then((response) => response.json()),
    ]);

    if (session?.name) nameEl.textContent = session.name;
    if (qrUrl?.url) {
      urlEl.href = qrUrl.url;
      urlEl.textContent = qrUrl.url;
    }
    if (session?.viewers != null) {
      document.title = `${session.name} – ${session.viewers} verbunden`;
    }
  } catch {
    errorEl.hidden = false;
  }
}

main();
