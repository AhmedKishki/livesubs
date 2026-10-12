import assert from "node:assert/strict";
import test from "node:test";

import { ViewerClient } from "../static/client.js";

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
