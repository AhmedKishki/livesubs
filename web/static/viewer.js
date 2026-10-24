// Viewer page composition: build the DOM, wire the controls, start the client.
//
// Deliberately no framework and no build step. The page is small, deployment is
// one laptop, and adding a bundler to a lecture deployment is real operational
// risk for little gain.
//
// Resolve a language once from the URL, browser preferences or operator fallback.
// Its explicit role stays pinned across reconnects; no language enters localStorage.

import { bindPageLifecycle, ViewerClient } from "./client.js";
import {
  chooseLanguage,
  directionOf,
  isOffered,
  languageFromSearch,
  nativeNameOf,
  roleFor,
  stringsFor,
  viewerUrlWithLanguage,
} from "./language.js";
import { BACKDROPS, FONT_STEPS, POSITIONS, SubtitleOverlay } from "./overlay.js";

const STEP_LABELS = { 0.75: "A-", 0.9: "A", 1: "A+", 1.2: "A++", 1.45: "A+++" };

//: How long to wait before asking for the session again while the server is unreachable.
const SESSION_RETRY_MS = 1500;

//: The earliest a page may reload itself again after the server disagreed with it. A
//: server that keeps contradicting its own session description would otherwise reload
//: a phone in a loop.
const RELOAD_GUARD_MS = 10_000;
const RELOAD_GUARD_KEY = "livesubs.viewer.reloaded";

function sessionIdFromPath() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  return parts[0] === "s" && parts[1] ? decodeURIComponent(parts[1]) : null;
}

/**
 * The session's description, which says who is speaking and which languages exist.
 *
 * Retries for as long as the server cannot be reached, because the page is useless
 * without it and the page itself came from that server a moment ago. Resolves `null` for
 * a session the server has never heard of, which retrying cannot fix.
 */
async function loadSession(sessionId, onWaiting) {
  for (;;) {
    try {
      const response = await fetch(`/api/session/${encodeURIComponent(sessionId)}`);
      if (response.status === 404) return null;
      if (response.ok) return await response.json();
    } catch {
      /* unreachable for the moment: try again */
    }
    onWaiting();
    await new Promise((resolve) => window.setTimeout(resolve, SESSION_RETRY_MS));
  }
}

/** Reload once to pick up what the server now says, unless this page just did so. */
function reloadOnce() {
  try {
    const last = Number(window.sessionStorage.getItem(RELOAD_GUARD_KEY) ?? 0);
    if (Date.now() - last < RELOAD_GUARD_MS) return;
    window.sessionStorage.setItem(RELOAD_GUARD_KEY, String(Date.now()));
  } catch {
    /* no storage: reload anyway, the server is unlikely to disagree twice */
  }
  window.location.reload();
}

function buildControls(overlay, session, language, strings) {
  const wrap = document.createElement("div");
  wrap.className = "controls";

  const toggle = document.createElement("button");
  toggle.className = "controls-toggle";
  toggle.type = "button";
  toggle.title = strings.settings;
  toggle.setAttribute("aria-label", strings.settings);
  toggle.setAttribute("aria-expanded", "false");
  toggle.textContent = "Aa";

  const panel = document.createElement("div");
  panel.className = "controls-panel";

  function group(legend, values, current, label, onPick) {
    const fieldset = document.createElement("fieldset");
    const caption = document.createElement("legend");
    caption.textContent = legend;
    fieldset.append(caption);

    for (const value of values) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label(value);
      // Each label takes the direction of its own first letter, so the Latin "A+" is not
      // reordered into "+A" on a right-to-left page.
      button.dir = "auto";
      button.setAttribute("aria-pressed", String(value === current));
      button.addEventListener("click", () => {
        const next = { ...overlay.prefs, ...onPick(value) };
        overlay.applyPrefs(next);
        for (const sibling of fieldset.querySelectorAll("button")) {
          sibling.setAttribute("aria-pressed", String(sibling === button));
        }
      });
      fieldset.append(button);
    }
    return fieldset;
  }

  panel.append(
    group(strings.fontSize, FONT_STEPS, overlay.prefs.fontScale, (v) => STEP_LABELS[v] ?? String(v),
      (value) => ({ fontScale: value })),
    group(strings.position, POSITIONS, overlay.prefs.position, (v) => strings.positions[v] ?? v,
      (value) => ({ position: value })),
    group(strings.background, BACKDROPS, overlay.prefs.backdrop, (v) => strings.backdrops[v] ?? v,
      (value) => ({ backdrop: value })),
  );

  const fullscreen = document.createElement("button");
  fullscreen.type = "button";
  fullscreen.textContent = strings.fullscreen;
  fullscreen.addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen?.();
  });
  panel.append(fullscreen);

  toggle.addEventListener("click", () => {
    const open = panel.classList.toggle("is-open");
    toggle.setAttribute("aria-expanded", String(open));
  });

  wrap.append(toggle, panel);
  return wrap;
}

export function buildLanguageButtons(session, language, strings) {
  const group = document.createElement("nav");
  group.className = "viewer-languages";
  group.setAttribute("aria-label", strings.language);
  for (const code of ["en", "de", "ar"]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = nativeNameOf(code, session);
    button.lang = code;
    button.dir = "auto";
    button.setAttribute("data-language", code);
    button.setAttribute("aria-pressed", String(code === language));
    button.addEventListener("click", () => {
      window.location.assign(viewerUrlWithLanguage(window.location.href, code));
    });
    group.append(button);
  }
  return group;
}

async function main() {
  const requested = languageFromSearch(window.location.search);
  const waitingStrings = stringsFor(requested);

  const pill = document.createElement("div");
  pill.className = "status-pill";
  pill.setAttribute("role", "status");
  pill.setAttribute("aria-live", "polite");
  document.body.append(pill);

  const sessionId = sessionIdFromPath();
  if (!sessionId) {
    document.body.textContent = waitingStrings.noSession;
    return;
  }

  const session = await loadSession(sessionId, () => {
    pill.textContent = waitingStrings.connecting;
    pill.classList.add("is-visible");
  });
  if (session === null) {
    pill.remove();
    document.body.textContent = waitingStrings.noSession;
    return;
  }
  pill.classList.remove("is-visible");

  const language = chooseLanguage(requested, session, navigator);
  const strings = stringsFor(language);
  const dir = directionOf(language, session);
  document.documentElement.lang = language;
  document.documentElement.dir = dir;
  document.title = strings.title;

  const root = document.createElement("div");
  root.className = "overlay-root";

  let client = null;
  const overlay = new SubtitleOverlay(root, {
    language,
    dir,
    onAck: (payload) => client?.send(payload),
  });

  document.body.append(root, buildLanguageButtons(session, language, strings),
    buildControls(overlay, session, language, strings));

  // Do not open a socket for a language the session cannot produce: the server would
  // refuse it with close code 4403, and a refused socket is a way to learn what the
  // session description already said. The picker is still there to choose another.
  if (!isOffered(language, session)) {
    overlay.setStatus("unavailable", null, pill);
    return;
  }

  client = new ViewerClient({
    sessionId,
    role: roleFor(language, session),
    overlay,
    pill,
    onHello: (hello) => {
      document.title = `${hello.session_name} | ${strings.title}`;
      // The server restarted in another mode while this page was open, so the language
      // this page was built for is not the one the server now sends. Rendering would
      // show nothing, so start again from what the server says now.
      if (hello.language && hello.language !== language) reloadOnce();
    },
    onClose: (code) => {
      if (code === 4403) overlay.setStatus("unavailable", null, pill);
    },
  });

  // Leaving the page - a language change navigates, a phone closes the tab -
  // must close the socket and cancel any pending reconnect rather than leave a
  // timer that reopens a connection behind the next page.
  bindPageLifecycle(client);

  client.start();
}

if (typeof window !== "undefined" && typeof document !== "undefined") main();
