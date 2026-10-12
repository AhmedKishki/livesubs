// Language selection and per-language rendering.
//
// Run with:  node --test web/tests/*.test.mjs
//
// No npm dependency and no real DOM: a small fake document/window covers the
// handful of APIs the overlay uses, so the language decisions can be tested in
// node exactly as the browser runs them.

import assert from "node:assert/strict";
import test from "node:test";

import { ViewerClient } from "../static/client.js";
import { SubtitleOverlay } from "../static/overlay.js";
import { Action } from "../static/pacing.js";
import {
  acceptsSegment,
  fieldForLanguage,
  languageFromSearch,
  roleForLanguage,
  segmentLanguage,
  segmentText,
  viewerUrlWithLanguage,
} from "../static/language.js";

// --- URL language and roles -------------------------------------------------

test("a missing or unknown lang is German; lang=ar is Arabic", () => {
  assert.equal(languageFromSearch(""), "de");
  assert.equal(languageFromSearch("?lang=de"), "de");
  assert.equal(languageFromSearch("?lang=ar"), "ar");
  assert.equal(languageFromSearch("?lang=AR"), "ar");
  assert.equal(languageFromSearch("?lang=fr"), "de");
});

test("Arabic maps to the exact viewer_ar role and ar field; German to viewer/de", () => {
  assert.equal(roleForLanguage("ar"), "viewer_ar");
  assert.equal(fieldForLanguage("ar"), "ar");
  assert.equal(roleForLanguage("de"), "viewer");
  assert.equal(fieldForLanguage("de"), "de");
  assert.equal(roleForLanguage(undefined), "viewer");
});

test("the viewer URL keeps German clean and makes Arabic explicit", () => {
  assert.equal(
    viewerUrlWithLanguage("http://host/s/abc?lang=ar&x=1#frag", "de"),
    "/s/abc?x=1#frag",
  );
  assert.equal(viewerUrlWithLanguage("http://host/s/abc", "ar"), "/s/abc?lang=ar");
  assert.equal(
    viewerUrlWithLanguage("http://host/s/abc?lang=de", "ar"),
    "/s/abc?lang=ar",
  );
  // Idempotent: choosing Arabic twice never doubles the parameter.
  assert.equal(
    viewerUrlWithLanguage("http://host/s/abc?lang=ar", "ar"),
    "/s/abc?lang=ar",
  );
});

test("the WebSocket URL carries the role that matches the language", () => {
  globalThis.window = {
    location: { protocol: "http:", host: "host:8000" },
  };
  assert.equal(
    new ViewerClient({ sessionId: "sess", role: roleForLanguage("de"), overlay: {} }).url,
    "ws://host:8000/ws/sess?role=viewer",
  );
  assert.equal(
    new ViewerClient({ sessionId: "sess", role: roleForLanguage("ar"), overlay: {} }).url,
    "ws://host:8000/ws/sess?role=viewer_ar",
  );
});

// --- segment acceptance and field selection ---------------------------------

test("a segment without a language field is German, for older servers", () => {
  assert.equal(segmentLanguage({ de: "Hallo" }), "de");
  assert.equal(acceptsSegment({ de: "Hallo" }, "de"), true);
  assert.equal(acceptsSegment({ de: "Hallo" }, "ar"), false);
});

test("the Arabic target field is never filled from German", () => {
  assert.equal(segmentText({ language: "de", de: "Hallo" }, "de"), "Hallo");
  assert.equal(segmentText({ language: "ar", ar: "مرحبا" }, "ar"), "مرحبا");
  // Wrong language, and a right-language segment missing its field: both null.
  assert.equal(segmentText({ language: "de", de: "Hallo" }, "ar"), null);
  assert.equal(segmentText({ language: "ar", de: "Hallo" }, "ar"), null);
  assert.equal(segmentText({ language: "ar" }, "ar"), null);
});

// --- heartbeat ping/pong for both roles -------------------------------------

for (const role of ["viewer", "viewer_ar"]) {
  test(`a heartbeat is answered for role ${role}`, () => {
    const replies = [];
    const client = new ViewerClient({
      sessionId: "test",
      role,
      overlay: { offer: () => assert.fail("heartbeat must not render") },
    });
    client.send = (payload) => replies.push(payload);
    client._dispatch({ data: JSON.stringify({ v: 1, type: "ping" }) });
    assert.deepEqual(replies, [{ type: "pong" }]);
  });
}

test("stopping a client cancels a pending reconnect timer", () => {
  const timers = new Map();
  let nextTimer = 1;
  globalThis.window = {
    location: { protocol: "http:", host: "host:8000" },
    setTimeout: (fn) => {
      const id = nextTimer++;
      timers.set(id, fn);
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
  };
  globalThis.WebSocket = class {
    constructor() {
      this.readyState = 0;
    }
    addEventListener() {}
    close() {}
  };

  const client = new ViewerClient({ sessionId: "s", overlay: {} });
  client._scheduleReconnect();
  assert.equal(timers.size, 1, "a reconnect should be pending");
  client.stop();
  assert.equal(timers.size, 0, "stop() must clear the pending reconnect");
});

test("a refused language close stops instead of reconnecting forever", () => {
  const timers = new Map();
  let nextTimer = 1;
  globalThis.window = {
    location: { protocol: "http:", host: "host:8000" },
    setTimeout: (fn) => {
      const id = nextTimer++;
      timers.set(id, fn);
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
  };

  const closes = [];
  const client = new ViewerClient({
    sessionId: "s",
    role: "viewer_ar",
    overlay: {},
    onClose: (code) => closes.push(code),
  });
  client._onClose({ code: 4403 });
  assert.deepEqual(closes, [4403]);
  assert.equal(timers.size, 0, "4403 must not schedule a reconnect");
});

test("an ordinary disconnect still schedules a reconnect", () => {
  const timers = new Map();
  let nextTimer = 1;
  globalThis.window = {
    location: { protocol: "http:", host: "host:8000" },
    setTimeout: (fn) => {
      const id = nextTimer++;
      timers.set(id, fn);
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
  };

  const client = new ViewerClient({ sessionId: "s", overlay: {} });
  client._onClose({ code: 1006 });
  assert.equal(timers.size, 1, "a network drop should keep retrying");
});

// --- per-language rendering in the overlay ----------------------------------

class FakeElement {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attrs = {};
    this.style = { setProperty() {}, fontSize: "" };
    this._classes = new Set();
    this._text = "";
    this.offsetWidth = 10;
    this.scrollHeight = 40;
    this.clientHeight = 40;
    this.classList = {
      add: (...names) => names.forEach((name) => this._classes.add(name)),
      remove: (...names) => names.forEach((name) => this._classes.delete(name)),
      contains: (name) => this._classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !this._classes.has(name) : Boolean(force);
        if (on) this._classes.add(name);
        else this._classes.delete(name);
        return on;
      },
    };
  }

  set className(value) {
    this._classes = new Set(String(value).split(/\s+/).filter(Boolean));
  }

  get className() {
    return [...this._classes].join(" ");
  }

  set textContent(value) {
    this._text = String(value);
  }

  get textContent() {
    return this._text;
  }

  setAttribute(name, value) {
    this.attrs[name] = String(value);
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  querySelector(selector) {
    const cls = selector.replace(/^\./, "");
    if (this._classes.has(cls)) return this;
    for (const child of this.children) {
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }
}

function installOverlayDom() {
  globalThis.document = {
    documentElement: new FakeElement("html"),
    createElement: (tag) => new FakeElement(tag),
  };
  globalThis.window = {
    localStorage: { getItem: () => null, setItem: () => {} },
    addEventListener: () => {},
    getComputedStyle: (element) => ({ fontSize: element.style.fontSize || "32px" }),
    innerHeight: 800,
    // Never fire: pacing/retirement is tested by the pacing suite, and letting a
    // fade-out run here would blank the layer the assertions are reading.
    setTimeout: () => 1,
    clearTimeout: () => {},
  };
}

function textOf(overlay) {
  return overlay.activeLayer.querySelector(".subtitle-text");
}

test("a German overlay renders the de field and ignores an Arabic segment", () => {
  installOverlayDom();
  const overlay = new SubtitleOverlay(new FakeElement("div"), {
    prefs: { fontScale: 1, position: "bottom", backdrop: "box" },
  });

  assert.equal(overlay.offer({ seq: 0, rev: 0, state: "final", de: "Hallo" }, 1000), Action.REPLACE);
  assert.equal(overlay.current.seq, 0);
  assert.equal(textOf(overlay).textContent, "Hallo");

  // An Arabic segment is ignored before pacing: no text, seq unchanged, no drop.
  const dropped = overlay.dropped;
  assert.equal(
    overlay.offer({ seq: 1, rev: 0, state: "final", language: "ar", ar: "مرحبا" }, 2000),
    Action.IGNORE,
  );
  assert.equal(textOf(overlay).textContent, "Hallo");
  assert.equal(overlay.current.seq, 0);
  assert.equal(overlay.dropped, dropped);
});

test("an Arabic overlay renders the ar field, RTL, and ignores German", () => {
  installOverlayDom();
  const overlay = new SubtitleOverlay(new FakeElement("div"), {
    language: "ar",
    prefs: { fontScale: 1, position: "bottom", backdrop: "box" },
  });

  const span = textOf(overlay);
  assert.equal(span.attrs.lang, "ar");
  assert.equal(span.attrs.dir, "rtl");

  assert.equal(
    overlay.offer({ seq: 0, rev: 0, state: "final", language: "de", de: "Hallo" }, 1000),
    Action.IGNORE,
  );
  assert.equal(overlay.current, null, "German must not initialise the Arabic sequence");
  assert.equal(span.textContent, "");

  assert.equal(
    overlay.offer({ seq: 0, rev: 0, state: "final", language: "ar", ar: "مرحبا" }, 2000),
    Action.REPLACE,
  );
  // The swap moves the active layer, so re-read it rather than the earlier span.
  assert.equal(textOf(overlay).textContent, "مرحبا");
});

test("the German overlay labels a stalled status in German, not raw server detail", () => {
  installOverlayDom();
  const overlay = new SubtitleOverlay(new FakeElement("div"), {
    prefs: { fontScale: 1, position: "bottom", backdrop: "box" },
  });
  const pill = new FakeElement("div");

  overlay.setStatus("delayed", "translation is behind", pill);
  assert.equal(pill.textContent, "Untertitel verzögert");
  assert.equal(pill.classList.contains("is-visible"), true);
});

test("the Arabic overlay labels a stalled status in Arabic, not raw server detail", () => {
  installOverlayDom();
  const overlay = new SubtitleOverlay(new FakeElement("div"), {
    language: "ar",
    prefs: { fontScale: 1, position: "bottom", backdrop: "box" },
  });
  const pill = new FakeElement("div");

  overlay.setStatus("stopped", "provider unreachable", pill);
  assert.equal(pill.textContent, "الترجمة غير متوفرة");
  assert.equal(pill.classList.contains("is-stopped"), true);
});
