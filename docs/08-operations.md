# Operations

## Deployment shape

- The service runs as one process on one port.
- A systemd user service with `Restart=always` can restart a crashed process. The shipped unit passes `--exit-on-failure`, so the process also exits non-zero when the pipeline dies, and systemd then restarts it. Without that flag the server keeps answering and shows `stopped`.
- `/healthz` answers 503 while the pipeline is not running, so any probe can tell a dead pipeline from a live server.
- Start the installed command interactively to choose a microphone and complete setup.

```bash
livesubs
```

```bash
livesubs --serve --approach live --audio pw:TARGET
```

- A direct `--serve` invocation skips the interactive picker and startup probes.
- Recognition defaults to Automatic for English/German turns. Startup offers an override and fallback selection; `/admin` changes both during the run. English, German and Arabic subtitles stay offered under every setting. Use a manual pin when the speech is known to remain in one language.

```bash
livesubs --speaker de
```

- In a terminal, startup always asks for confirmation from the microphone list. In a service, configure `capture.target` and store credentials first. An unspecified source is rejected even if there is only one microphone.
- The user unit runs `~/.local/bin/livesubs --start` from the user's home directory. If uv uses a custom executable directory, adjust `ExecStart`. Set an absolute `capture.artifacts_dir` if recordings should be saved elsewhere.

```bash
livesubs --init-config ~/.config/livesubs/livesubs.yaml
livesubs --set-key deepgram --scope global --verify
livesubs --set-key deepseek --scope global --verify
```

- Set `capture.target` in the configuration, then install and enable the unit as that user. Do not use `sudo` for the user service.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/livesubs.service ~/.config/systemd/user/livesubs.service
systemctl --user daemon-reload
systemctl --user enable --now livesubs.service
```

- Copying the unit uses the checkout, but the running service uses the installed application.
- Bind `0.0.0.0` so LAN clients can reach the service.
- The default server has no TLS or authentication. Restrict it to a trusted LAN.
- `0.0.0.0` is a bind address, not a destination for attendees.
- Open the LAN address the command prints rather than the bind address.

```
landing page : http://192.168.110.190:8000/   (open this, not the bind address 0.0.0.0)
```


## Firewall

- Inbound TCP 8000 must be allowed on the LAN interface.
- `--preflight` reports whether a local firewall is likely to refuse it and names the command.
- Making the change needs root, so it stays a manual step.

```bash
sudo ufw allow 8000/tcp comment 'livesubs'
```

- An active `ufw` service does not establish that firewall rules are enabled. On the reference machine, the service reports `active` while `/etc/ufw/ufw.conf` contains `ENABLED=no`.
- Both the check and any debugging read the config file rather than the service state.

## Pre-flight checklist

- Run these checks before each lecture.

| # | Check | How | Expected |
| --- | --- | --- | --- |
| 1 | Capture target present | `uv run livesubs --preflight` | the configured source exists |
| 2 | Capture carries signal | speak during preflight | a measured speech signal, not digital silence |
| 3 | Capture not clipping | the `--preflight` clipping line | no clipping |
| 4 | Language routing sane | Speak English/German test turns with a viewer in each target; include a mixed boundary. | Each target stays in its own language; matching homogeneous speech needs no MT. |
| 5 | Latency acceptable | per-segment latency lines during rehearsal | no growing lag |
| 6 | QR reachable | one phone, mobile data off | the page loads |
| 7 | Providers verified | the launcher probe, or `uv run livesubs --set-key NAME --verify` | the provider accepts the key |
| 8 | Requested target ready | Connect its viewer and inspect transcript and translated output. | MT warms only when a demanded item needs translation. |
| 9 | Archive recording | `recordings/` | a WAV file growing |
| 10 | Power | laptop on mains, sleep disabled | none |

- Preflight checks source presence, signal, clipping and configuration, then reports network diagnostics. It does not authenticate provider keys.
- Check 6 is the one preflight cannot finish.
- Installed startup verifies the selected Deepgram and DeepSeek keys before serving. New keys go into the per-user credential file. A rejected saved key is replaced in its existing source file when the operator confirms a replacement.
- `uv run livesubs --keys` reports key presence and origin, not whether a provider accepts it.

## Sleep, power and thermal

- Disable automatic suspend and display blanking.
- Run on mains power.
- Check CPU load and thermal throttling if capture or translation falls behind.
- `delayed` can report translation backlog or capture calibration. It does not prove text correctness or transcript completeness.

## Capacity

- Measured with `tools/load_test.py` against `livesubs --serve --approach sim --speed 4`, on a 16-core development laptop, with the load generator on the same machine and the loopback interface in between. Each row used a fresh server, 80% of phones reading the default language and 20% reading Arabic, and 30 s connected after a ramp of up to 30 s.
- These measurements predate automatic language routing and its transcription-only finals. They have not been rerun for the redesigned message mix.
- Each phone behaved like the viewer page: it answered every heartbeat and acknowledged every final line. The publisher sent each phone between 1.6 and 2 messages per second, which is a line every few seconds with a first draft and a final.

| Phones | Event-loop lateness (95th) | Processor | Memory | Final lines delivered to every phone |
| --- | --- | --- | --- | --- |
| 100 | 1 ms | 7% of one core | 86 MB | all |
| 500 | 2 ms | 14% | 127 MB | all |
| 1,000 | 17 ms | 21% | 178 MB | all |
| 1,500 | 26 ms | 31% | 229 MB | all |
| 2,500 | 62 ms | 41% | 331 MB | all |
| 3,500 | 113 ms | 55% | 434 MB | all |
| 5,000 | 228 ms | 95% | 587 MB | all |

- The lateness figures include the ramp, when handshakes are the heaviest work, so they bound the steady state from above. Every final line reached every phone in every run. A draft replaced before it was sent is the design, not a loss, and no run skipped more than four lines for a phone that could not keep up.
- One process uses one core. Each message delivered cost about 100 µs of it, acknowledgements and heartbeats included, so a server delivers roughly 9,000 messages a second before the loop is saturated.
- The practical limit follows from the message rate. At the rate measured, the loop stays under 100 ms late up to about 3,000 phones, and 5,000 pegs the core. At 3 messages per phone per second the same limits fall to about 1,800 and 3,000 phones. Lateness adds directly to the delay of every line, so the console warns at 100 ms.
- Recognition and translation do not scale with phones. Provider work depends on how long a stream is open and on how many languages someone reads.
- The message rate is set mostly by `PipelineConfig.draft_interval_ms`, the shortest gap between drafts of one line (120 ms). It is not a setting yet, so changing it means changing the code.
- The server raises its open-file limit to 16,384 at startup when the hard limit allows it, and warns when it cannot reach 4,096. Every phone is an open file, and the usual default of 1,024 would refuse phone number 1,000.
- Not measured: the room's WiFi, which is the likelier limit in a hall, TLS, a slower processor than the test laptop, and a generator on another machine. Check the figures on the real hardware with `tools/load_test.py --host SERVER_ADDRESS` from another computer, and read the operator console's event-loop lateness and open-file figures during the lecture.

### Network

- Measured with the same setup, a phone received 2.0 messages per second of about 250 bytes each, which is 500 B/s or about 4 kbit/s of payload, with 20% of phones reading Arabic. Adding WebSocket, TCP and IP headers, and the phone's acknowledgements of each final line and each packet, a phone costs about 5 kbit/s downstream and 1.5 kbit/s upstream.
- The server's link therefore carries about 5 Mbit/s for 1,000 phones and 12 Mbit/s for 2,500. A wired gigabit port is not the limit, so connect the server by Ethernet.
- With 2,000 phones connecting within 0.2 s, as after a WiFi blip, every phone connected. The median connect took 560 ms and the 95th percentile 1.5 s, while the server's core was saturated for those seconds.
- A phone that goes offline misses the lines published meanwhile. On reconnect it receives the current status and the current line, then follows live. It retries after 0.5 s and doubles the wait up to 10 s, with no random spread, so phones that lost the network together retry together.
- A phone that cannot keep up skips lines and never slows another phone. The server drops a connection that has been silent for 30 s.
- Not measured: the room's WiFi. Its limit is airtime, not bandwidth, because every message is a small frame to each phone and an acknowledgement back. This is an estimate from assumed frame costs, not a measurement: at 0.3 ms of airtime per exchange, 2 messages per second use 0.1% to 0.2% of a radio per phone, so one radio saturates at roughly 300 to 500 phones, and phones with a weak signal use several times more airtime each. Plan on 100 to 150 phones per access point radio until the room has been measured.
- The address pool is the next limit: a /24 network offers about 250 addresses. Access points also have their own client limits, and client isolation blocks the phone-to-server path altogether (see [07-delivery-qr](07-delivery-qr.md)).

## Failure behaviour

- An exception escaping the publisher sets the session to `stopped` with the generic detail `publisher failed`.
- A normal end of the audio source, including a capture failure that returns normally, can leave the status `live` with no new output.
- Runtime provider failover is not implemented. Provider failures can leave gaps while the server continues running.
- A stall watchdog is not implemented, so a silent capture does not by itself raise `stopped`.
- Watch the terminal and `/admin` for unexpected gaps while the presenter is speaking.

| Condition | Current report |
| --- | --- |
| At least 3 finals wait for one language's translation (`lag_queue_limit`). | The pipeline publishes `delayed` to that language's viewers only. |
| Startup calibration is active or fails. | The reporter publishes `delayed`, with a capture error on failure. A startup failure then stops the publisher. |
| Runtime gain protection gives up. | The operator console shows a capture error, the original gain is restored when it can be verified as safe, and capture continues with the protection off. |
| ASR events raise in the collector. | The pipeline publishes an ASR error, without switching providers. |
| MT raises or returns no text. | One quick retry. If it still fails, the segment is dropped with an MT error, or finished with a trailing `…` if words were already shown. Later segments continue. |
| MT is slower than the speaker. | `delayed` for that language at 3 queued segments. Past 6 the oldest queued segments are discarded. Other languages are unaffected. |
| A translator cannot be built or warmed. | The MT-required item fails within its bounded request/retry path. The target worker remains available for later eligible transcription; that success does not clear the MT outage. |
| A manual speaker change cannot open. | The console retains requested intent and shows retrying or failed. It does not silently revert. Change settings during a pause because reconnecting can leave recognition gaps. |

## Logging

- Served runs use uvicorn's formatter with package loggers enabled.
- Session startup and disconnect logs identify the session and connection role. MT logs input length, time to first token and total request duration without the lecture text.
- Wire timing fields and viewer drop counts are not a complete structured event log.

## Retention and privacy

- Audio and subtitle saving default to enabled, with no automatic expiration. The reference deployment treats its lecture audio as nonsensitive evaluation material.
- Set `capture.archive: false` for a session where that is not true, or `LIVESUBS_ARCHIVE_AUDIO=false` for one run. The files are gitignored and local.
- Transcripts and translations are written beside the audio whenever `capture.subtitles` is on, sharing its name. Set `capture.subtitles: false` or `LIVESUBS_SUBTITLES=false` to turn them off.
- The subtitle files are the pipeline's own output, not a ground-truth reference.
- Viewer devices do not record audio. They send acknowledgements and store display preferences, so the landing page's current no-storage/no-transmission wording is not an accurate disclosure.
- Audio goes to the ASR vendor and text goes to the MT vendor. Disclose this on the landing page or in course materials if the institution requires it.
- The Deepgram EU endpoint `api.eu.deepgram.com` is available for EU residency, with the United States host as the default. DeepSeek is China-hosted, which is the one to flag if student speech is involved.
- The non-sensitivity of lecture audio is a property of the reference deployment, not a general privacy claim.

## Runbook: common failures

| Symptom | Likely cause | Action |
| --- | --- | --- |
| No subtitles at all | capture target wrong or muted | check pre-flight 1 and 2. A muted default microphone is the usual cause |
| Subtitles frozen but the status is live | capture is delivering silence, or recognition stopped without its socket closing | check the microphone and the terminal; restart the session |
| No translation for a segment | the translation returned nothing | the pipeline drops the segment and publishes an error. Check the MT key and the provider log |
| Growing lag over minutes | translation backlog | inspect terminal timings, provider response times and host CPU load |
| No viewer can connect | host firewall, wrong URL or WiFi isolation | test the printed LAN address, check the firewall, then try a network without isolation |
| Some viewers connect, some not | weak WiFi or power saving | use the wired uplink and disable WiFi power saving |
| Requested language starts slowly | cache cold or provider response time | Inspect request timings after connecting that language's viewer. |
| Translation is grammatically odd | segments too short | raise `max_segment_ms` and check context carry |
| Translation is over-condensed | segments too long | lower `max_segment_ms` |

## Rehearsal

- Run at least one full 90-minute rehearsal in the actual hall, with the actual network and the actual audio setup.
- Have a reviewer who reads each target language read that language's output.
- Unit tests do not establish acoustic quality or network reachability in the deployment room.

### Rehearsal tool

- `uv run python tools/rehearse.py` runs the sample corpus live. It defaults to `--approach live` and the file `samples/sample-16k-mono.wav` at real time.
- `uv run python tools/rehearse.py --approach live-text --cues samples/reference.en.timed.srt` translates a timed transcript instead.
- The tool starts the server, waits for the provider-neutral recognition readiness log, then starts the player. Connect a viewer before its timeout. Readiness does not wait for MT warm-up, which transcription-only readers never need. Playback/capture alignment is still approximate, not sample-accurate.
- Ctrl-C stops the player and the server together. The server sets `PR_SET_PDEATHSIG`, so it dies with the tool. The player does not, so a player can survive a `SIGKILL` of the tool, and it exits at the end of the file.
- `--no-play` runs without sound.
- `--speaker de` pins German recognition; `--speaker auto` uses English/German automatic recognition. The old `en+de` spelling is a deprecated alias.
- `--player` takes any command containing `{file}`.
- The default player is the first available of `gst-play-1.0`, `pw-play` and `aplay`, so one of them must be installed for sound.

## Load testing

- `uv run python tools/load_test.py --viewers 500 --duration 30` connects that many simulated phones to a running server and prints the fan-out spread (first phone to last, per message), whether every phone received every final line, and the server's own figures taken from `/api/stats`.
- Serve something that publishes at lecture pace first, for example `livesubs --serve --approach sim --speed 4`. `--processes` sets how many generator processes share the connections, `--arabic` sets the share of phones reading the second language, and `--json PATH` saves the report.
- Run the generator from another machine for a figure that includes the network.

## Probing the socket

- `uv run python tools/ws_probe.py --count 6` connects to the WebSocket the way a phone does and prints what arrives.
- Its defaults are host `127.0.0.1`, port `8000`, session `demo` and role `viewer`, so it matches a default `--serve` run. `--role viewer_ar` probes another language and `--role admin` shows every language and the source.
- Pass `--session` when the running session id differs, and `--host` when the server is elsewhere.
- It distinguishes three causes of a blank page: the server is not running, the session id is wrong, or the socket connects but nothing is published.
