import assert from "node:assert/strict";
import test from "node:test";

import { bindPageLifecycle, ViewerClient } from "../static/client.js";

test("a heartbeat is answered without rendering a subtitle", () => {
  const replies = [];
  const client = new ViewerClient({
    sessionId: "test",
    overlay: { offer: () => assert.fail("heartbeat must not render") },
  });
  client.send = (payload) => replies.push(payload);
  client._dispatch({ data: JSON.stringify({ v: 1, type: "ping" }) });
  assert.deepEqual(replies, [{ type: "pong" }]);
});

test("a viewer restored from browser history reconnects after pagehide", () => {
  const handlers = new Map();
  const calls = [];
  const unbind = bindPageLifecycle({
    stop: () => calls.push("stop"),
    start: () => calls.push("start"),
  }, {
    addEventListener: (name, handler) => handlers.set(name, handler),
    removeEventListener: (name) => handlers.delete(name),
  });
  handlers.get("pageshow")({ persisted: false });
  assert.deepEqual(calls, []);
  handlers.get("pagehide")();
  handlers.get("pageshow")({ persisted: true });
  assert.deepEqual(calls, ["stop", "start"]);
  unbind();
  assert.equal(handlers.size, 0);
});

// --- the real client, not a stand-in -----------------------------------------------------
//
// The test above proves the page hooks call `start` and `stop`. It cannot prove that
// `start` after `stop` works, because it replaces the client. These use the real class
// with a fake WebSocket, so the whole restore path runs.

function installTransport() {
  const sockets = [];
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
    static OPEN = 1;
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.closed = false;
      sockets.push(this);
    }
    addEventListener() {}
    close() {
      this.closed = true;
    }
    send() {}
  };
  return { sockets, timers };
}

test("a page restored from the back/forward cache opens a new socket", () => {
  const { sockets } = installTransport();
  const client = new ViewerClient({ sessionId: "demo", overlay: {} });
  const handlers = new Map();
  bindPageLifecycle(client, {
    addEventListener: (name, handler) => handlers.set(name, handler),
    removeEventListener: () => {},
  });

  client.start();
  assert.equal(sockets.length, 1);

  handlers.get("pagehide")();
  assert.equal(sockets[0].closed, true, "leaving the page must close the socket");

  handlers.get("pageshow")({ persisted: true });
  assert.equal(sockets.length, 2, "coming back must reconnect");
  assert.equal(sockets[1].closed, false);
});

test("start() is idempotent while a socket is open", () => {
  const { sockets } = installTransport();
  const client = new ViewerClient({ sessionId: "demo", overlay: {} });
  client.start();
  client.start();
  assert.equal(sockets.length, 1);
});

test("start() during a pending reconnect does not leave two connections", () => {
  const { sockets, timers } = installTransport();
  const client = new ViewerClient({ sessionId: "demo", overlay: {} });
  client._scheduleReconnect();
  assert.equal(timers.size, 1);

  client.start();

  assert.equal(timers.size, 0, "the pending timer is replaced by the immediate open");
  assert.equal(sockets.length, 1);
});

test("a restart begins from the shortest backoff again", () => {
  installTransport();
  const client = new ViewerClient({ sessionId: "demo", overlay: {} });
  client._backoff = 10_000;
  client.stop();
  client.start();
  assert.equal(client._backoff, 500);
});
