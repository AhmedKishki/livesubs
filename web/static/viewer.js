// Viewer page composition: build the DOM, wire the controls, start the client.
//
// Deliberately no framework and no build step. The page is small, deployment is
// one laptop, and adding a bundler to a lecture deployment is real operational
// risk for little gain.
//
// German is the default and the only language a plain QR scan reaches. Arabic is
// an opt-in carried entirely by `?lang=ar`, which selects the `viewer_ar` role so
// the server sends Arabic and nothing else. The URL is authoritative: no language
// is read from or written to localStorage, so a remembered experiment can never
// turn a German viewer into an Arabic one.

import { bindPageLifecycle, ViewerClient } from "./client.js";
import { BACKDROPS, FONT_STEPS, POSITIONS, SubtitleOverlay } from "./overlay.js";
import {
  LANGUAGES,
  languageConfig,
  languageFromSearch,
  roleForLanguage,
  viewerUrlWithLanguage,
} from "./language.js";

const STEP_LABELS = { 0.75: "A-", 0.9: "A", 1: "A+", 1.2: "A++", 1.45: "A+++" };
const POSITION_LABELS = { bottom: "unten", top: "oben" };
const BACKDROP_LABELS = { box: "Kasten", shadow: "Schatten", outline: "Kontur" };

function sessionIdFromPath() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  return parts[0] === "s" && parts[1] ? decodeURIComponent(parts[1]) : null;
}

function buildControls(overlay, language, onLanguage) {
  const wrap = document.createElement("div");
  wrap.className = "controls";

  const toggle = document.createElement("button");
  toggle.className = "controls-toggle";
  toggle.type = "button";
  toggle.title = "Einstellungen";
  toggle.setAttribute("aria-label", "Einstellungen");
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

  // The language picker. A plain select, labelled by language name in that
  // language, so it needs no translated chrome of its own.
  const languageFieldset = document.createElement("fieldset");
  const languageCaption = document.createElement("legend");
  languageCaption.textContent = "Sprache";
  const select = document.createElement("select");
  select.className = "language-select";
  select.setAttribute("aria-label", "Sprache");
  for (const [code, config] of Object.entries(LANGUAGES)) {
    const option = document.createElement("option");
    option.value = code;
    option.textContent = config.label;
    option.setAttribute("lang", config.htmlLang);
    select.append(option);
  }
  select.value = language;
  select.addEventListener("change", () => onLanguage(select.value));
  languageFieldset.append(languageCaption, select);

  panel.append(
    languageFieldset,
    group("Schriftgröße", FONT_STEPS, overlay.prefs.fontScale, (v) => STEP_LABELS[v] ?? String(v),
      (value) => ({ fontScale: value })),
    group("Position", POSITIONS, overlay.prefs.position, (v) => POSITION_LABELS[v] ?? v,
      (value) => ({ position: value })),
    group("Hintergrund", BACKDROPS, overlay.prefs.backdrop, (v) => BACKDROP_LABELS[v] ?? v,
      (value) => ({ backdrop: value })),
  );

  const fullscreen = document.createElement("button");
  fullscreen.type = "button";
  fullscreen.textContent = "Vollbild";
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

function main() {
  const sessionId = sessionIdFromPath();
  if (!sessionId) {
    document.body.textContent = "Keine Sitzung in der URL.";
    return;
  }

  const language = languageFromSearch(window.location.search);
  const config = languageConfig(language);
  document.documentElement.lang = config.htmlLang;
  document.documentElement.dir = config.dir;

  const root = document.createElement("div");
  root.className = "overlay-root";

  const pill = document.createElement("div");
  pill.className = "status-pill";
  pill.setAttribute("role", "status");
  pill.setAttribute("aria-live", "polite");

  let client = null;
  const overlay = new SubtitleOverlay(root, {
    language,
    onAck: (payload) => client?.send(payload),
  });

  // An explicit change navigates to the new URL rather than reconnecting in
  // place. A full navigation tears the old page, socket and timers down through
  // the normal unload path, so nothing is left reconnecting behind the new
  // view, and the browser history records the choice.
  const controls = buildControls(overlay, language, (next) => {
    if (next === language) return;
    window.location.assign(viewerUrlWithLanguage(window.location.href, next));
  });

  document.body.append(root, pill, controls);

  // Do not advertise a language the session cannot produce. The server refuses an
  // Arabic socket with close code 4403 anyway; this keeps the control honest so
  // that refusal path stays the exception rather than the normal way to learn.
  fetch(`/api/session/${encodeURIComponent(sessionId)}`)
    .then((response) => (response.ok ? response.json() : null))
    .then((data) => {
      if (!Array.isArray(data?.languages) || data.languages.includes("ar")) return;
      controls.querySelector('.language-select option[value="ar"]')?.remove();
    })
    .catch(() => {
      /* metadata is advisory; leave both options on any failure */
    });

  client = new ViewerClient({
    sessionId,
    role: roleForLanguage(language),
    overlay,
    pill,
    onHello: (hello) => {
      document.title = `${hello.session_name} | Untertitel`;
    },
    onClose: (code) => {
      if (code === 4403) overlay.setStatus("stopped", null, pill);
    },
  });

  // Leaving the page - a language change navigates, a phone closes the tab -
  // must close the socket and cancel any pending reconnect rather than leave a
  // timer that reopens a connection behind the next page.
  bindPageLifecycle(client);

  client.start();
}

main();
