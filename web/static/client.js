// WebSocket transport: connect, dispatch, reconnect.
//
// Reconnection is not optional. A phone that sleeps, a WiFi blip, or a server
// restart must all recover without the attendee reloading the page. On
// reconnect the server seeds a snapshot, so the screen repopulates immediately
// rather than sitting blank until the next segment.

const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 10_000;

export class ViewerClient {
  constructor({
    sessionId,
    role = "viewer",
    overlay,
    pill = null,
    onHello = null,
    onStatus = null,
    onError = null,
  }) {
    this.sessionId = sessionId;
    this.role = role;
    this.overlay = overlay;
    this.pill = pill;
    this.onHello = onHello;
    this.onStatus = onStatus;
    this.onError = onError;

    this._socket = null;
    this._backoff = RECONNECT_MIN_MS;
    this._stopped = false;
  }

  get url() {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    return `${scheme}://${window.location.host}/ws/${encodeURIComponent(this.sessionId)}?role=${this.role}`;
  }

  start() {
    if (this._socket) return;
    this._open();
  }

  stop() {
    this._stopped = true;
    this._socket?.close();
    this._socket = null;
  }

  send(payload) {
    if (this._socket?.readyState === WebSocket.OPEN) {
      this._socket.send(JSON.stringify(payload));
    }
  }

  _open() {
    const socket = new WebSocket(this.url);
    this._socket = socket;

    socket.addEventListener("open", () => {
      this._backoff = RECONNECT_MIN_MS;
    });

    socket.addEventListener("message", (event) => this._dispatch(event));

    // No error handling beyond this: a WebSocket error is always followed by a
    // close, which is where reconnection belongs.
    socket.addEventListener("close", () => this._scheduleReconnect());
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

  _scheduleReconnect() {
    this._socket = null;
    if (this._stopped) return;

    const delay = this._backoff;
    this._backoff = Math.min(RECONNECT_MAX_MS, this._backoff * 2);
    window.setTimeout(() => this._open(), delay);
  }
}
