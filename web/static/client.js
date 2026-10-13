// WebSocket transport: connect, dispatch, reconnect.
//
// Reconnection is not optional. A phone that sleeps, a WiFi blip, or a server
// restart must all recover without the attendee reloading the page. On
// reconnect the server seeds a snapshot, so the screen repopulates immediately
// rather than sitting blank until the next segment.

const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 10_000;

//: Application close codes the server uses for a client it will never serve:
//: 4404 is an unknown session, 4403 is a language this session cannot produce.
//: Both are permanent for this connection, so retrying only spins. Matches
//: `WS_UNKNOWN_SESSION` and `WS_UNSUPPORTED_LANGUAGE` in src/livesubs/server/ws.py.
const FATAL_CLOSE_CODES = new Set([4403, 4404]);

export function bindPageLifecycle(client, page = window) {
  const hide = () => client.stop();
  const show = (event) => { if (event.persisted) client.start(); };
  page.addEventListener("pagehide", hide);
  page.addEventListener("pageshow", show);
  return () => {
    page.removeEventListener("pagehide", hide);
    page.removeEventListener("pageshow", show);
  };
}

export class ViewerClient {
  constructor({
    sessionId,
    role = "viewer",
    overlay,
    pill = null,
    onHello = null,
    onStatus = null,
    onError = null,
    onClose = null,
  }) {
    this.sessionId = sessionId;
    this.role = role;
    this.overlay = overlay;
    this.pill = pill;
    this.onHello = onHello;
    this.onStatus = onStatus;
    this.onError = onError;
    //: Called with the close code when the server refuses permanently, so the
    //: page can explain the refusal instead of retrying forever.
    this.onClose = onClose;

    this._socket = null;
    this._backoff = RECONNECT_MIN_MS;
    this._stopped = false;
    //: Pending reconnect timer. Kept so `stop()` can cancel it; otherwise a
    //: stopped client would still open a socket when the timer fired, which is
    //: a leaked connection that outlives the page state that wanted it.
    this._reconnectTimer = null;
  }

  get url() {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    return `${scheme}://${window.location.host}/ws/${encodeURIComponent(this.sessionId)}?role=${this.role}`;
  }

  start() {
    if (this._socket) return;
    // `stop()` leaves the client stopped, so a restart has to say it is wanted again.
    // Without this a page restored from the back/forward cache called `start()` and
    // `_open()` returned at once, leaving a viewer with no socket and no way to notice.
    this._stopped = false;
    this._backoff = RECONNECT_MIN_MS;
    // A pending retry would otherwise fire later and open a second connection.
    if (this._reconnectTimer !== null) {
      window.clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this._open();
  }

  stop() {
    this._stopped = true;
    if (this._reconnectTimer !== null) {
      window.clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this._socket?.close();
    this._socket = null;
  }

  send(payload) {
    if (this._socket?.readyState === WebSocket.OPEN) {
      this._socket.send(JSON.stringify(payload));
    }
  }

  _open() {
    if (this._stopped) return;

    const socket = new WebSocket(this.url);
    this._socket = socket;

    socket.addEventListener("open", () => {
      this._backoff = RECONNECT_MIN_MS;
    });

    socket.addEventListener("message", (event) => this._dispatch(event));

    // No error handling beyond this: a WebSocket error is always followed by a
    // close, which is where reconnection belongs.
    socket.addEventListener("close", (event) => this._onClose(event));
  }

  _dispatch(event) {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }

    // Captured before any rendering work, so `rendered_in_ms` measures what the
    // viewer experienced rather than how long our own dispatch took.
    const receivedAt = performance.now();

    switch (message.type) {
      case "ping":
        this.send({ type: "pong" });
        break;

      case "hello":
        this.onHello?.(message.data);
        break;

      case "segment":
        this.overlay.offer(message.data, receivedAt);
        break;

      case "status":
        this.overlay.setStatus(message.data.status, message.data.detail, this.pill);
        this.onStatus?.(message.data);
        break;

      case "error":
        this.onError?.(message.data);
        break;

      default:
        break;
    }
  }

  _onClose(event) {
    this._socket = null;
    if (this._stopped) return;

    // A refusal is not a disconnect. Reconnecting to a session the server has
    // already rejected would spin the backoff loop forever and, for 4403, keep
    // announcing demand for a language that can never arrive.
    if (FATAL_CLOSE_CODES.has(event?.code)) {
      this._stopped = true;
      this.onClose?.(event.code);
      return;
    }

    this._scheduleReconnect();
  }

  _scheduleReconnect() {
    this._socket = null;
    if (this._stopped) return;

    const delay = this._backoff;
    this._backoff = Math.min(RECONNECT_MAX_MS, this._backoff * 2);
    this._reconnectTimer = window.setTimeout(() => {
      this._reconnectTimer = null;
      this._open();
    }, delay);
  }
}
