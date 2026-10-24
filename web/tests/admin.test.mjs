import assert from "node:assert/strict";
import test from "node:test";

test("raw live and history sources use automatic bidi independently of generic source metadata", async () => {
  const nodes = new Map();
  const doc = {
    createElement(tag) {
      return { tagName: tag, children: [], ownerDocument: doc, handlers: {},
        append(...children) { this.children.push(...children); },
        replaceChildren(...children) { this.children = children; },
        addEventListener(type, callback) { this.handlers[type] = callback; },
      };
    },
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, this.createElement("div"));
      return nodes.get(id);
    },
  };
  globalThis.document = doc;
  globalThis.window = { location: { protocol: "http:", host: "host" } };
  let socket;
  globalThis.WebSocket = class {
    constructor() { socket = this; this.handlers = {}; }
    addEventListener(type, callback) { this.handlers[type] = callback; }
  };
  globalThis.fetch = async (url) => url === "/api/session"
    ? { ok: true, json: async () => ({ id: "demo", source: "multilingual",
      languages: ["en", "de", "ar"], targets: [
        {code:"en", name:"English"}, {code:"de", name:"German"}, {code:"ar", name:"Arabic", dir:"rtl"},
      ], speaker: "ar", default_language: "en", revision: 0,
      recognition: { requested:"ar", active:"ar", state:"active" } }) }
    : new Promise(() => {});
  await import("../static/admin.js");
  await new Promise(resolve => setImmediate(resolve));
  for (const [seq, source] of [[1, "مرحبا بالجميع"], [2, "Hello everyone"]]) {
    socket.handlers.message({ data: JSON.stringify({ type:"segment", data: {
      seq, rev:0, state:"final", language:"en", text:"Subtitles", src:source,
    } }) });
    const live = nodes.get("live-src");
    assert.equal(live.textContent, source);
    assert.equal(live.dir, "auto");
    const historySource = nodes.get("history").children[0].children.at(-1);
    assert.equal(historySource.textContent, source);
    assert.equal(historySource.dir, "auto");
  }
});
