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
