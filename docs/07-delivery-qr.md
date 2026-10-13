# QR delivery

## URL scheme

| Path | Audience | Purpose |
| --- | --- | --- |
| `/` | attendees | landing page with the QR code, join instructions and session name |
| `/s/{session_id}` | attendees | the subtitle page in the session's default language, and the QR target |
| `/s/{session_id}?lang=ar` | attendees requesting another language | the subtitle page in that language, here Arabic |
| `/admin` | operator | the speaker's words and every target language, final-line history, status, connection count and local render duration |
| `/ws/{session_id}` | viewer and operator pages | WebSocket for `segment`, `status`, `hello` and `error` messages |
| `/qr.png` | landing page | rendered QR for the current session |

- The QR encodes the resolved viewer URL, for example `http://192.168.1.42:8000/s/7f3a`.
- The default server uses plain HTTP. The viewer needs no microphone permission because capture happens server-side.
- The default WebSocket role is `viewer`, which receives the session's first target language. Another language uses `viewer_<code>`, for example `viewer_ar`. No viewer role receives the speaker's own words or another language. A session that cannot produce the requested language accepts the handshake and then closes with code `4403`, and an unknown session closes with `4404`. The handshake is accepted first because a close sent before it reaches browsers as code 1006, which the viewer treats as a network drop and retries.
- The viewer page asks `/api/session` for the offered languages and does not open a socket for one the session does not offer. The server refusal covers a page built from a stale description.
- Roles are a content filter, not access control. The service is unauthenticated, so any device on the network can request `admin` or another language. Keep it on a trusted LAN.

### Address selection

1. Use `LIVESUBS_PUBLIC_URL` when it is set.
2. Otherwise preserve the request host unless it is loopback, unspecified or link-local.
3. For those filtered hosts, use the machine's LAN address when one is available.
4. Fall back to the request host if LAN discovery fails.

- A loopback host is replaced by the LAN address, because `localhost` on a phone resolves to the phone itself.

| Page opened at | QR encodes |
| --- | --- |
| `http://localhost:8000/` | the LAN address, for example `http://192.168.1.42:8000/s/demo` |
| `http://127.0.0.1:8000/` | the LAN address |
| `http://192.168.1.42:8000/` | unchanged |
| `http://subs.example.org/` | unchanged, but the phone must resolve the hostname |

- The URL filter also replaces bind addresses (`0.0.0.0`, `::`) and link-local addresses (`169.254.*`, `fe80::*`).
- A hostname requires working DNS or mDNS on the phone. Use a numeric LAN address or a configured `public_url` when name resolution fails.
- If LAN discovery fails, the request host remains in the URL.
- A numeric LAN address avoids hostname resolution, but firewall and routing checks still apply.

### Secure context

- Browser microphone access requires a secure context, so `getUserMedia` would be blocked on plain HTTP.
- Server-side capture avoids that browser microphone requirement.

### WiFi client isolation

- Many campus and guest WiFi networks enable AP client isolation, which blocks device-to-device traffic.
- Isolation can block the phone-to-server path, especially when both devices are wireless. Wireless-to-wired access may still work.
- Test the actual path by loading the viewer on a phone with mobile data off.
- Test phone-to-server access whether the server uses WiFi or Ethernet.
- `uv run livesubs --preflight` probes subnet hosts from the server's neighbour table.

#### Reading the preflight result

- A successful probe does not establish phone access. The responding peer may be wired.
- A failed probe does not establish isolation. The peer may be asleep or behind a firewall.
- The neighbour probe runs only while this machine is itself a wireless client.

### Local firewall

- A default-deny host firewall refuses the phone in the same way isolation does.
- `uv run livesubs --preflight` checks for a local firewall and reports a possible remedy.

### Fallbacks when isolation is on

- Use a dedicated travel router and have attendees join its SSID.
- A phone hotspot is another option, with the laptop joining it and sharing the uplink with viewer traffic.
- A laptop-hosted AP needs a compatible adapter.
- Ask network administrators for a client-to-server exception for a permanent installation.
- A fallback network must also provide outbound HTTPS for live providers.

## Bandwidth and concurrency

- At an assumed 1 KB per revision and one revision every 2 s, traffic is about 500 B/s per viewer, or 50 KB/s (0.4 Mbit/s) for 100 viewers.
- Streamed draft revisions add messages, and long segments add bytes. The estimate is not a measured concurrency limit.
- The fan-out cost is per-message JSON serialisation. Serialise once per revision and write the same bytes to every socket.
- Laptop WiFi power saving can add latency jitter. Disable it during lectures, and prefer the wired USB Ethernet adapter for the uplink if the room provides a port.

## Late joiners and reconnects

- The server keeps the current segment in memory.
- On connect, the server sends a `hello` message with the protocol version and session metadata.
- The snapshot then sends the current `status`, followed by the current segment when one exists.
- A join before the first segment has no text to display yet.
- After a disconnect, the viewer retries with backoff and receives another snapshot when the connection succeeds.
- Idle sockets receive a heartbeat every 10 seconds. Clients reply with `pong`; a client silent for 30 seconds is disconnected and no longer counts toward provider demand.

## Operator console

- `/admin` shows the speaker's recognised words and one line per target language for comparison. A history row collects the translations of one segment as they arrive.
- It shows the segment history of committed lines.
- It shows the pipeline status.
- It polls the viewer count about once a second. Operator connections do not count as viewers or keep paid processing active.
- `/api/session` and `/healthz` report `viewers` separately from total `connections`.
- Its render duration measures handling in the operator's own browser, not a viewer's end-to-end latency.
- A dropped-count field exists, but viewer acknowledgements are not routed to it and it remains unpopulated.
- The landing page does not link to `/admin`. The operator role still has no authentication.

## Viewer acknowledgements

- The overlay acknowledges each final line and each drop. It does not acknowledge draft revisions, which the next revision replaces within moments.
- The ack carries `seq`, `rev`, `rendered_in_ms` and `dropped`.
- `rendered_in_ms` is measured from when the viewer accepted the segment, so it needs no clock synchronisation with the server.
- The wire field `timing.render_ms` stays unused until an offset estimate between the two clocks exists.
- The server drains acknowledgement messages but does not aggregate `rendered_in_ms` or drop counts into operator metrics.

## Landing page content

- The landing page shows the QR code, a join URL and the session title.
- It speaks the session's default language, which is the language the QR code opens.
- It says the subtitles are live, that no app or login is needed, and that only display settings are stored on the device. It adds that the audio is recorded when the running publisher archives audio.

## Viewer data

- Viewer devices do not capture audio. They make page requests, send acknowledgement fields and save display preferences in `localStorage`.
- The landing page no longer claims that nothing is transmitted. It does not disclose provider processing: the audio goes to the recognition provider and the text to the translation provider. The operator must give that disclosure separately before a lecture.
