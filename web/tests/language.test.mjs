// Language selection and per-language rendering, for either speaker.
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
  chooseLanguage,
  directionOf,
  isOffered,
  languageFromSearch,
  nativeNameOf,
  roleFor,
  segmentText,
  stringsFor,
  viewerUrlWithLanguage,
} from "../static/language.js";

// What /api/session says for each mode. The client has no list of languages of its own, so
// everything below is driven by these descriptions.
const TARGETS = {
  de: { code: "de", name: "German", native: "Deutsch", dir: "ltr" },
  en: { code: "en", name: "English", native: "English", dir: "ltr" },
  ar: { code: "ar", name: "Arabic", native: "العربية", dir: "rtl" },
};

function session(source, ...languages) {
  return {
    id: "demo",
    source,
    default: languages[0],
    languages,
    targets: languages.map((code) => TARGETS[code]),
  };
}

const ENGLISH_SPEAKER = session("en", "de", "ar");
const GERMAN_SPEAKER = session("de", "en", "ar");
const MODES = [
  ["an English speaker", ENGLISH_SPEAKER, "de"],
  ["a German speaker", GERMAN_SPEAKER, "en"],
];

// --- URL language and roles -------------------------------------------------

test("a URL asks for a language by its code, and for none by saying nothing", () => {
  assert.equal(languageFromSearch(""), null);
  assert.equal(languageFromSearch("?x=1"), null);
  assert.equal(languageFromSearch("?lang=ar"), "ar");
  assert.equal(languageFromSearch("?lang=AR"), "ar");
  assert.equal(languageFromSearch("?lang=de"), "de");
  // Only the shape is checked here. Which languages exist is for the server to say.
  assert.equal(languageFromSearch("?lang=fr"), "fr");
  assert.equal(languageFromSearch("?lang="), null);
  assert.equal(languageFromSearch("?lang=../../etc"), null);
  assert.equal(languageFromSearch("?lang=deutsch"), null);
});

for (const [name, mode, fallback] of MODES) {
  test(`for ${name}, no request reaches the first target and a plain viewer role`, () => {
    assert.equal(chooseLanguage(null, mode), fallback);
    assert.equal(roleFor(chooseLanguage(null, mode), mode), "viewer");
  });

  test(`for ${name}, Arabic is the explicit viewer_ar role`, () => {
    assert.equal(chooseLanguage("ar", mode), "ar");
    assert.equal(roleFor("ar", mode), "viewer_ar");
    assert.equal(isOffered("ar", mode), true);
  });

  test(`for ${name}, asking for the default by name is still the plain viewer role`, () => {
    assert.equal(roleFor(fallback, mode), "viewer");
  });
}

test("a language the session does not produce is not offered", () => {
  assert.equal(isOffered("en", ENGLISH_SPEAKER), false, "the speaker's own language");
  assert.equal(isOffered("de", GERMAN_SPEAKER), false, "the speaker's own language");
  assert.equal(isOffered("fr", ENGLISH_SPEAKER), false);
  assert.equal(isOffered("ar", session("en", "de")), false, "a publisher without Arabic");
  assert.equal(isOffered("de", null), false);
});

test("the picker reads each language's name and direction from the session", () => {
  assert.equal(nativeNameOf("ar", ENGLISH_SPEAKER), "العربية");
  assert.equal(nativeNameOf("de", ENGLISH_SPEAKER), "Deutsch");
  assert.equal(directionOf("ar", ENGLISH_SPEAKER), "rtl");
  assert.equal(directionOf("en", GERMAN_SPEAKER), "ltr");
  // A language the session lacks falls back to what the string pack knows.
  assert.equal(directionOf("ar", session("en", "de")), "rtl");
  assert.equal(directionOf("xx", ENGLISH_SPEAKER), "ltr");
  assert.equal(nativeNameOf("xx", ENGLISH_SPEAKER), "xx");
});

test("every language has the same interface strings, and an unknown one speaks English", () => {
  const shape = (value) =>
    typeof value === "object"
      ? Object.fromEntries(Object.entries(value).map(([k, v]) => [k, shape(v)]))
      : typeof value;
  const english = shape(stringsFor("en"));
  for (const code of ["de", "ar"]) {
    assert.deepEqual(shape(stringsFor(code)), english, `${code} lacks or adds a string`);
  }
  assert.equal(stringsFor("xx"), stringsFor("en"));
  assert.equal(stringsFor(null), stringsFor("en"));
});

test("the viewer URL keeps the default clean and makes any other language explicit", () => {
  assert.equal(
    viewerUrlWithLanguage("http://host/s/abc?lang=ar&x=1#frag", "de", "de"),
    "/s/abc?x=1#frag",
  );
  assert.equal(viewerUrlWithLanguage("http://host/s/abc", "ar", "de"), "/s/abc?lang=ar");
  assert.equal(viewerUrlWithLanguage("http://host/s/abc?lang=de", "ar", "de"), "/s/abc?lang=ar");
  // The default depends on the session, not on the language.
  assert.equal(viewerUrlWithLanguage("http://host/s/abc?lang=en", "en", "en"), "/s/abc");
  assert.equal(viewerUrlWithLanguage("http://host/s/abc", "en", "de"), "/s/abc?lang=en");
  // Idempotent: choosing a language twice never doubles the parameter.
  assert.equal(viewerUrlWithLanguage("http://host/s/abc?lang=ar", "ar", "de"), "/s/abc?lang=ar");
});

test("the WebSocket URL carries the role that matches the language", () => {
  globalThis.window = {
    location: { protocol: "http:", host: "host:8000" },
  };
  for (const [language, role] of [["de", "viewer"], ["ar", "viewer_ar"]]) {
    assert.equal(
      new ViewerClient({ sessionId: "sess", role: roleFor(language, ENGLISH_SPEAKER), overlay: {} }).url,
      `ws://host:8000/ws/sess?role=${role}`,
    );
  }
  assert.equal(
    new ViewerClient({ sessionId: "sess", role: roleFor("en", GERMAN_SPEAKER), overlay: {} }).url,
    "ws://host:8000/ws/sess?role=viewer",
  );
});

// --- segment acceptance and text selection ----------------------------------

test("a segment belongs to the one language it is written in", () => {
  assert.equal(acceptsSegment({ language: "de", text: "Hallo" }, "de"), true);
  assert.equal(acceptsSegment({ language: "de", text: "Hallo" }, "ar"), false);
  // No language, no acceptance: a segment that does not say what it is renders nowhere.
  assert.equal(acceptsSegment({ text: "Hallo" }, "de"), false);
  assert.equal(acceptsSegment({ language: "de" }, null), false);
});

test("text is taken from the segment's own language and never from another", () => {
  assert.equal(segmentText({ language: "de", text: "Hallo" }, "de"), "Hallo");
  assert.equal(segmentText({ language: "ar", text: "مرحبا" }, "ar"), "مرحبا");
  assert.equal(segmentText({ language: "en", text: "Hello" }, "en"), "Hello");
  // Wrong language, and a right-language segment without its text: both null.
  assert.equal(segmentText({ language: "de", text: "Hallo" }, "ar"), null);
  assert.equal(segmentText({ language: "ar" }, "ar"), null);
  assert.equal(segmentText({ language: "ar", text: 5 }, "ar"), null);
});

// --- heartbeat ping/pong for both roles -------------------------------------

for (const role of ["viewer", "viewer_ar", "admin"]) {
  test(`a heartbeat is answered for role ${role}`, () => {
    const replies = [];
    const client = new ViewerClient({
      sessionId: "test",
      role,
      overlay: { offer: () => assert.fail("heartbeat must not render") },
    });
    client.send = (payload) => replies.push(payload);
    client._dispatch({ data: JSON.stringify({ v: 2, type: "ping" }) });
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

const PREFS = { fontScale: 1, position: "bottom", backdrop: "box" };

function overlayFor(language, extra = {}) {
  installOverlayDom();
  return new SubtitleOverlay(new FakeElement("div"), {
    language,
    dir: directionOf(language, ENGLISH_SPEAKER),
    prefs: PREFS,
    ...extra,
  });
}

test("an overlay renders its own language's text and ignores another's", () => {
  const overlay = overlayFor("de");

  assert.equal(
    overlay.offer({ seq: 0, rev: 0, state: "final", language: "de", text: "Hallo" }, 1000),
    Action.REPLACE,
  );
  assert.equal(overlay.current.seq, 0);
  assert.equal(textOf(overlay).textContent, "Hallo");

  // Another language is ignored before pacing: no text, seq unchanged, no drop.
  const dropped = overlay.dropped;
  assert.equal(
    overlay.offer({ seq: 1, rev: 0, state: "final", language: "ar", text: "مرحبا" }, 2000),
    Action.IGNORE,
  );
  assert.equal(textOf(overlay).textContent, "Hallo");
  assert.equal(overlay.current.seq, 0);
  assert.equal(overlay.dropped, dropped);
});

test("an English overlay, for a German speaker, works the same way", () => {
  const overlay = overlayFor("en");

  assert.equal(
    overlay.offer({ seq: 0, rev: 0, state: "final", language: "en", text: "Hello" }, 1000),
    Action.REPLACE,
  );
  assert.equal(textOf(overlay).textContent, "Hello");
  assert.equal(
    overlay.offer({ seq: 1, rev: 0, state: "final", language: "de", text: "Hallo" }, 2000),
    Action.IGNORE,
  );
});

test("an Arabic overlay renders right-to-left and ignores everything else", () => {
  const overlay = overlayFor("ar");

  const span = textOf(overlay);
  assert.equal(span.attrs.lang, "ar");
  assert.equal(span.attrs.dir, "rtl");

  for (const other of ["de", "en"]) {
    assert.equal(
      overlay.offer({ seq: 0, rev: 0, state: "final", language: other, text: "x" }, 1000),
      Action.IGNORE,
    );
  }
  assert.equal(overlay.current, null, "another language must not initialise the sequence");
  assert.equal(span.textContent, "");

  assert.equal(
    overlay.offer({ seq: 0, rev: 0, state: "final", language: "ar", text: "مرحبا" }, 2000),
    Action.REPLACE,
  );
  // The swap moves the active layer, so re-read it rather than the earlier span.
  assert.equal(textOf(overlay).textContent, "مرحبا");
});

test("an overlay built without a language renders nothing", () => {
  const overlay = overlayFor(undefined);

  assert.equal(
    overlay.offer({ seq: 0, rev: 0, state: "final", language: "de", text: "Hallo" }, 1000),
    Action.IGNORE,
  );
  assert.equal(overlay.current, null);
});

for (const [language, delayed, stopped] of [
  ["de", "Untertitel verzögert", "Untertitel nicht verfügbar"],
  ["en", "Subtitles delayed", "Subtitles unavailable"],
  ["ar", "الترجمة متأخرة", "الترجمة غير متوفرة"],
]) {
  test(`the ${language} overlay labels a stalled status in ${language}, not raw server detail`, () => {
    const overlay = overlayFor(language);
    const pill = new FakeElement("div");

    overlay.setStatus("delayed", "translation is behind", pill);
    assert.equal(pill.textContent, delayed);
    assert.equal(pill.classList.contains("is-visible"), true);
    assert.equal(pill.classList.contains("is-stopped"), false);

    overlay.setStatus("stopped", "provider unreachable", pill);
    assert.equal(pill.textContent, stopped);
    assert.equal(pill.classList.contains("is-stopped"), true);

    overlay.setStatus("live", null, pill);
    assert.equal(pill.textContent, "");
    assert.equal(pill.classList.contains("is-visible"), false);
  });
}

test("a language the session does not offer is reported as unavailable, in that language", () => {
  const overlay = overlayFor("ar");
  const pill = new FakeElement("div");

  overlay.setStatus("unavailable", null, pill);
  assert.equal(pill.textContent, "هذه الترجمة غير متوفرة");
  assert.equal(pill.classList.contains("is-stopped"), true);
});

// --- acknowledgements ---------------------------------------------------------------

test("only finals and drops are acknowledged, not every draft revision", () => {
  const acks = [];
  const overlay = overlayFor("de", { onAck: (payload) => acks.push(payload) });
  const line = (seq, rev, state, text) => ({ seq, rev, state, language: "de", text });

  overlay.offer(line(0, 0, "draft", "Hal"), 1000);
  overlay.offer(line(0, 1, "draft", "Hallo"), 1100);
  assert.equal(acks.length, 0, "drafts are replaced within moments; nothing reads their acks");

  overlay.offer(line(0, 2, "final", "Hallo Welt"), 1200);
  assert.deepEqual(acks.map((ack) => [ack.seq, ack.rev]), [[0, 2]]);

  // A new line arriving inside the replacement floor is dropped, and that is reported
  // even for a draft, because the drop count is the point of the acknowledgement.
  overlay.offer(line(1, 0, "draft", "Zu frueh"), 1300);
  assert.equal(acks.length, 2);
  assert.equal(acks[1].dropped, 1);
});
