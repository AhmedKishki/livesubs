// Viewer page composition: build the DOM, wire the controls, start the client.
//
// Deliberately no framework and no build step. The page is small, deployment is
// one laptop, and adding a bundler to a lecture deployment is real operational
// risk for little gain.

import { ViewerClient } from "./client.js";
import { BACKDROPS, FONT_STEPS, POSITIONS, SubtitleOverlay } from "./overlay.js";

const STEP_LABELS = { 0.75: "A-", 0.9: "A", 1: "A+", 1.2: "A++", 1.45: "A+++" };
const POSITION_LABELS = { bottom: "unten", top: "oben" };
const BACKDROP_LABELS = { box: "Kasten", shadow: "Schatten", outline: "Kontur" };

function sessionIdFromPath() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  return parts[0] === "s" && parts[1] ? decodeURIComponent(parts[1]) : null;
}

function buildControls(overlay) {
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

  panel.append(
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

  const root = document.createElement("div");
  root.className = "overlay-root";

  const pill = document.createElement("div");
  pill.className = "status-pill";
  pill.setAttribute("role", "status");
  pill.setAttribute("aria-live", "polite");

  let client = null;
  const overlay = new SubtitleOverlay(root, {
    onAck: (payload) => client?.send(payload),
  });

  document.body.append(root, pill, buildControls(overlay));

  client = new ViewerClient({
    sessionId,
    overlay,
    pill,
    onHello: (hello) => {
      document.title = `${hello.session_name} | Untertitel`;
    },
  });

  client.start();
}

main();
