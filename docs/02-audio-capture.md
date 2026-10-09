# Audio capture

## Signal path

- In the reference setup, the presenter's microphone feeds a powered speaker, with no digital output available to the laptop.
- The laptop captures the room through its own microphone. Distance and reverberation limit recognition quality.
- A direct feed would avoid that room path, but its electrical level must match the capture input.

## Reference deployment

- Built and tested on a machine with no GPU, about 30 GB RAM (roughly 24 GB in use), PipeWire 1.0.5 and no `sudo`.
- The hardware and readings below describe that machine, not deployment requirements.

| Description | `node.name` | Measured | Notes |
| --- | --- | --- | --- |
| sof-hda-dsp Digital Microphone | `alsa_input.pci-0000_00_1f.3-...HiFi__hw_sofhdadsp_6__source` | -46 dBFS | the built-in DMIC array, and the only input here carrying a room level |
| sof-hda-dsp Headphones Stereo Microphone | `alsa_input.pci-0000_00_1f.3-...HiFi__hw_sofhdadsp__source` | -90 dBFS | the 3.5 mm combo jack, with nothing plugged in |
| ThinkPad USB-C Dock Audio Mono | `alsa_input.usb-Lenovo_ThinkPad_USB-C_Dock_Audio_...` | -120 dBFS | digital silence: muted or not connected |
| CORSAIR VOID ELITE Dongle Mono | `alsa_input.usb-...Corsair...` | -120 dBFS | was the system default source, and it was muted |

- Sustained zero samples indicate digital silence. Nonzero low-level noise is different, although it does not prove that speech will be captured clearly.
- Allow the recorder to settle before measuring. Initial zero samples during startup do not prove that the device is muted.

## Finding a capture device

```bash
uv run livesubs --preflight --approach live-sim
```

- Without a configured capture target, preflight lists the candidate sources on the current machine.
- A device identifier is valid only on the machine that produced it, so no default device ships.
- Target by `node.name`, never by numeric index. Numeric ids change across reboots and re-plugging, and a stale id either fails or silently selects a different device.
- The live capture backend accepts a device directly, without editing the config file.

```bash
uv run livesubs --serve --approach live-sim --audio 'pw:YOUR_PIPEWIRE_NODE_NAME'
```

- Replace `YOUR_PIPEWIRE_NODE_NAME` with a listed source name.
- An unknown name is rejected before the server binds a port, with available sources in the error.
- Do not rely on the system default microphone. It may be muted or disconnected, as the reference deployment's headset was.
- Calibration rejects digitally silent input. Quiet room noise is a different condition and needs a speech test.

## Requesting the capture format

- Request the stream format with `--rate`, `--channels` and `--format`, never with `-P`.
- `-P` sets node properties, which `pw-record` does not negotiate the stream format from, so the request is silently ignored and the device defaults to 48 kHz stereo.
- Reading 48 kHz stereo as 16 kHz mono would deliver six times the expected data rate and corrupt audio-clock timings.

## Capture backends

- Capture backends deliver 16 kHz mono PCM frames to the pipeline.
- `pw-record` is the backend for a real lecture.
- WAV replay accepts 16-bit PCM at any sample rate and channel count. It downmixes and resamples incrementally rather than loading the recording into memory.
- Other sample widths and unreadable WAV formats are rejected before replay starts.
- Synthetic sources provide silence, a steady tone and speech-like audio for tests without capture hardware.
- A backend is chosen with one `--audio` value: `synth`, `file:PATH[@SPEED]` or `pw:TARGET`.
- File replay uses `@1` for real time, `@2` for twice speed and `@0` for unpaced processing.

## The capture command

```bash
pw-record \
  --target 'YOUR_PIPEWIRE_NODE_NAME' \
  --rate 16000 \
  --channels 1 \
  --format s16 \
  -
```

- `pw-record` writes raw PCM to stdout, which ingest reads as a stream.
- The Deepgram adapter consumes 16 kHz mono signed-16-bit little-endian PCM, so this capture command needs no further resampling.
- Capture does not require `ffmpeg`, `sox` or `pactl`.

## Microphone calibration

- Calibration measures the actual PCM from the selected device.
- It never changes speaker volume, never boosts silence and never scales already-clipped PCM digitally.

| Setting | Default | Effect |
| --- | --- | --- |
| `capture.auto_calibrate` | `true` | enable the startup gain stage |
| `capture.runtime_calibration` | `true` | enable the runtime gain stage |

### Startup

- With automatic calibration enabled, the pipeline awaits the source's `prepare()` before opening providers or consuming pipeline frames.
- Startup works in 2 s measurement windows and has a 25 s budget (`max_duration_seconds`).
- Sustained clipping permits at most 4 adjustments (`max_adjustments`), each reducing hardware Capture gain on the exact ALSA device.
- Correction requires `amixer` and a standard ALSA `Capture` control for the exact source.
- Each reduction is read back and re-measured, so a change that the mixer accepted but the signal did not reflect is detected.
- A quiet room is reported as quiet and is not treated as proof that speech was tested.
- A healthy microphone needs no mixer command. Gain discovery is lazy, so ALSA and `amixer` are touched only when correction is necessary.

### Runtime

- Each 2 s window is measured against a clipping limit of 0.001 of samples.
- Two consecutive clipped windows trigger a correction (`sustained_windows`).
- At least 5 s must pass between adjustments (`cooldown_seconds`), and at most 4 runtime adjustments are made.
- Successful settings persist. A quiet or silent window cannot verify a correction, so verification waits for representative speech.

### Failure states

- Calibration rejects the following conditions with capture diagnostics rather than retrying indefinitely.

| Condition | Reason |
| --- | --- |
| Clipping persists after the budget or adjustment limit. | Further changes are not permitted. |
| The input is muted or digitally silent. | No representative signal is available. |
| The recorder stalls. | No new PCM arrives for verification. |
| Gain control is unsupported or ambiguous. | Monitor sources and shared ALSA cards cannot be adjusted safely. |
| A mixer adjustment does not reduce clipping. | The measured signal does not verify the change. |

- Launcher preflight failure prevents serving. In a direct served run, preparation or runtime calibration failure raises through the publisher and stops the session.

### Opting out

- `capture.runtime_calibration: false` holds gain fixed after startup.
- `capture.auto_calibrate: false`, or `LIVESUBS_AUTO_CALIBRATE=false`, disables both gain stages.
- Preflight still measures signal and clipping when both stages are off.

## Signal conditioning in ingest

- Ingest derives `now_ms` from consumed samples, not wall-clock time.

- **Adaptive noise floor.** A rolling minimum falls fast and rises slowly, so speech does not immediately become the new room-noise baseline.
- **Warm-up.** For the first 500 ms the floor tracks upward quickly, so a room louder than the initial guess is characterised in a fraction of a second. The cost is at most one 20 ms frame of phantom speech, which cannot produce a subtitle because `min_segment_ms` is twenty times longer.
- **Floor ceiling.** The floor is capped at -45 dBFS so sustained loud input continues to count as speech.
- **DC offset removal.** Each frame is centred independently rather than waiting for a running average to settle.
- **Level metering.** Reported to the pipeline through the `on_stats` callback. No console page consumes it yet.
- **Clipping detection.** Report only. Ingest never scales clipped PCM. Bounded, verified, downward-only hardware gain protection is the capture backend's job, not digital AGC.

## Deferred option: the 3.5 mm jack

- The machine exposes an analog capture input at the "Headphones Stereo Microphone" node above.
- If the powered speaker provides an aux or headphone output, a cable to the capture input avoids the room-acoustic path.
- It is a microphone-level input. A line-level source clips unless the speaker's output volume is reduced or a small attenuator is used.
- Calibrate the level before relying on it and inspect clipping diagnostics.

## Multi-microphone beamforming

- Independent USB or analog microphones each have their own clock and USB frame timing. Channels are neither sample-aligned nor drift-free.
- Crystal tolerance of ±10–100 ppm accumulates to up to about 0.5 s of relative slip over a 90-minute lecture.
- Delay-and-sum, MVDR and SRP-PHAT require sample-synchronised channels and known geometry. Independent microphones do not supply that synchronisation.
- Naive summing causes comb filtering. The same speech arrives at each mic at a different time, so summing creates frequency notches that vary with path length and degrade recognition.
- A multi-channel interface with a shared clock supplies synchronised capture channels.
- Hardware options include a USB conference microphone with built-in beamforming, such as Jabra, Poly or Sennheiser TeamConnect, or a multi-channel interface plus capsules with ODAS (MIT, introlab) for localisation and separation.
- A future independent-microphone implementation could select one channel rather than mix them: measure energy or VAD per source and route the cleanest channel to one ASR stream, re-evaluating every 1–2 s with hysteresis. That design avoids summed-channel comb filtering and keeps recognition at one stream, but is not implemented.

## Archiving raw audio

- An archive decorator tees any `AudioSource` into a WAV while forwarding frames to the pipeline.
- `capture.archive` enables audio saving. `capture.artifacts_dir` selects the directory for audio and subtitle files.
- The directory's environment alias is `LIVESUBS_ARCHIVE_DIR`.
- The WAV header is updated after each write, so a process killed between writes leaves the completed frames playable.
- An unwritable directory disables the archive with a logged error while frames keep flowing.

## Session artefacts

- A live-pipeline run with audio and subtitle saving enabled produces files with one session-and-timestamp stem.

```text
demo-20260922-150745.wav       16 kHz mono, the audio the timings refer to
demo-20260922-150745.de.srt    output from the selected MT adapter
demo-20260922-150745.en.srt    English source text from the selected mode
demo-20260922-150745.de.vtt    the same MT output, for a browser <track>
demo-20260922-150745.en.vtt
```

- `capture.subtitles` enables the subtitle writer.
- SRT and WebVTT are both produced because different tools consume them. Both come from one cue model, so they cannot disagree about timings.
| Mode | English sidecar | MT sidecar |
| --- | --- | --- |
| `live` | Contains recognition output. | Contains generated translation. |
| `live-text` | Contains the supplied timed transcript. | Contains generated translation. |
| `live-sim` | Contains invented stub phrases. | Contains a `[passthrough]` English echo. |

- The files preserve pipeline output, not a separate post-lecture correction pass.

### Cue clock

- Cue timings are the audio clock, not wall-clock time. The audio clock starts at zero on the first captured frame, and so does the archived WAV.
- A cue begins at the frame where speech resumed and ends at the last voiced frame, so no cue covers the pause after a sentence.
- Against a known fixture, cues land on the speech spans at 0.000, 2.200, 4.400 and so on, and the last one ends before the recording does.
- A session killed with `SIGKILL` leaves every cue up to the last one, because each is flushed as it is written.

```bash
# Burn the German subtitles into a video of the same event.
ffmpeg -i lecture.mp4 -vf "subtitles=recordings/demo-20260922-150745.de.srt" out.mp4
```

- The cue clock aligns to the archived WAV, which starts at zero. The video must therefore be aligned to that same start.
- A video started separately from the capture will not align, because its own start time is arbitrary.
- Subtitle burn-in needs a separate `ffmpeg` installation or a video editor. It is not part of live capture.
