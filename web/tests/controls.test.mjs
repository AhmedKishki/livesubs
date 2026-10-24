import assert from "node:assert/strict";
import test from "node:test";
import { bindSessionControls } from "../static/controls.js";

function element() {
  return { value: "", textContent: "", disabled: false, handlers: {},
    addEventListener(type, fn) { this.handlers[type] = fn; } };
}

test("operator controls reconcile server state without claiming activation", async () => {
  const elements = Object.fromEntries(["speaker-language", "default-language", "recognition-state", "settings-error"].map(id => [id, element()]));
  let finish;
  let sent;
  const fetcher = (url, options) => {
    sent = { url, ...options };
    return new Promise(resolve => { finish = resolve; });
  };
  const controls = bindSessionControls("a/b", id => elements[id], fetcher);
  const state = { revision: 0, speaker: "auto", default_language: "en",
    recognition: { requested: "auto", active: null, state: "paused", detected: {seq: 4, language: "mixed"} } };
  controls.reconcile(state);
  assert.match(elements["recognition-state"].textContent, /paused.*requested: auto.*active: none.*mixed/);
  elements["speaker-language"].value = "de";
  const pending = elements["speaker-language"].handlers.change();
  assert.equal(elements["speaker-language"].disabled, true);
  assert.doesNotMatch(elements["recognition-state"].textContent, /active: de/);
  assert.equal(sent.url, "/api/session/a%2Fb/settings");
  assert.deepEqual(JSON.parse(sent.body), { speaker: "de" });
  assert.equal(sent.headers["Content-Type"], "application/json");
  finish({ ok: true, json: async () => ({ ...state, revision: 1, speaker: "de",
    recognition: {requested: "de", active: "auto", state: "applying"} }) });
  await pending;
  assert.match(elements["recognition-state"].textContent, /applying.*requested: de.*active: auto/);
  controls.reconcile(state);
  assert.equal(elements["speaker-language"].value, "de", "stale poll cannot undo acceptance");
});

test("failed mutations restore last confirmed controls and expose a safe error", async () => {
  const elements = Object.fromEntries(["speaker-language", "default-language", "recognition-state", "settings-error"].map(id => [id, element()]));
  const controls = bindSessionControls("demo", id => elements[id], async () => ({ok:false}));
  controls.reconcile({ revision: 0, speaker: "auto", default_language: "en", recognition: {state:"paused"} });
  elements["default-language"].value = "ar";
  await elements["default-language"].handlers.change();
  assert.equal(elements["default-language"].value, "en");
  assert.match(elements["settings-error"].textContent, /not applied/);
  assert.equal(elements["default-language"].disabled, false);
});
