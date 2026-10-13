# Operations

## Deployment shape

- The service runs as one process on one port.
- A systemd user service with `Restart=always` can restart a crashed process.
- Start the installed command interactively to choose a microphone and complete setup.

```bash
livesubs
```

```bash
livesubs --serve --approach live --audio pw:TARGET
```

- A direct `--serve` invocation skips the interactive picker and startup probes.
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
| 4 | Language pairing sane | speak 10 s of test English | German appears, roughly correct |
| 5 | Latency acceptable | per-segment latency lines during rehearsal | no growing lag |
| 6 | QR reachable | one phone, mobile data off | the page loads |
| 7 | Providers verified | the launcher probe, or `uv run livesubs --set-key NAME --verify` | the provider accepts the key |
| 8 | Requested target ready | connect its viewer and inspect output | The target warms on demand, not while there are no viewers. |
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

## Failure behaviour

- An exception escaping the publisher sets the session to `stopped` with the generic detail `publisher failed`.
- A normal end of the audio source, including a capture failure that returns normally, can leave the status `live` with no new output.
- Runtime provider failover is not implemented. Provider failures can leave gaps while the server continues running.
- A stall watchdog is not implemented, so a silent capture or a dead ASR socket does not by itself raise `stopped`.
- Watch the terminal and `/admin` for unexpected gaps while the presenter is speaking.

| Condition | Current report |
| --- | --- |
| At least 4 finals wait for translation (`lag_queue_limit`). | The pipeline publishes `delayed`. |
| Startup calibration is active or fails. | The reporter publishes `delayed`, with a capture error on failure. A startup failure then stops the publisher. |
| Runtime gain protection gives up. | The operator console shows a capture error, the original gain is restored when it can be verified as safe, and capture continues with the protection off. |
| ASR events raise in the collector. | The pipeline publishes an ASR error, without switching providers. |
| MT raises or returns no text. | The segment is dropped with an MT error, and later segments can continue. |

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
| Subtitles frozen but the status is live | ASR socket died, and no watchdog or failover is active | restart the session |
| No German for a segment | the translation returned nothing | the pipeline drops the segment and publishes an error. Check the MT key and the provider log |
| Growing lag over minutes | translation backlog | inspect terminal timings, provider response times and host CPU load |
| No viewer can connect | host firewall, wrong URL or WiFi isolation | test the printed LAN address, check the firewall, then try a network without isolation |
| Some viewers connect, some not | weak WiFi or power saving | use the wired uplink and disable WiFi power saving |
| Requested language starts slowly | cache cold or provider response time | Inspect request timings after connecting that language's viewer. |
| German is grammatically odd | segments too short | raise `max_segment_ms` and check context carry |
| German is over-condensed | segments too long | lower `max_segment_ms` |

## Rehearsal

- Run at least one full 90-minute rehearsal in the actual hall, with the actual network and the actual audio setup.
- Have a bilingual reviewer read the German output.
- Unit tests do not establish acoustic quality or network reachability in the deployment room.

### Rehearsal tool

- `uv run python tools/rehearse.py` runs the sample corpus live. It defaults to `--approach live` and the file `samples/sample-16k-mono.wav` at real time.
- `uv run python tools/rehearse.py --approach live-text --cues samples/reference.en.timed.srt` translates a timed transcript instead.
- The tool starts the server, waits for its readiness log, then starts the player. This reduces manual timing error after provider startup, but is not sample-accurate audio synchronisation.
- Ctrl-C stops the player and the server together. The server sets `PR_SET_PDEATHSIG`, so it dies with the tool. The player does not, so a player can survive a `SIGKILL` of the tool, and it exits at the end of the file.
- `--no-play` runs without sound.
- `--player` takes any command containing `{file}`.
- The default player is the first available of `gst-play-1.0`, `pw-play` and `aplay`, so one of them must be installed for sound.

## Probing the socket

- `uv run python tools/ws_probe.py --count 6` connects to the WebSocket the way a phone does and prints what arrives.
- Its defaults are host `127.0.0.1`, port `8000`, session `demo` and role `viewer`, so it matches a default `--serve` run.
- Pass `--session` when the running session id differs, and `--host` when the server is elsewhere.
- It distinguishes three causes of a blank page: the server is not running, the session id is wrong, or the socket connects but nothing is published.
