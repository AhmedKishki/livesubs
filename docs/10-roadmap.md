# Roadmap

## Operator console and visibility

- Wire `on_stats` to a capture level meter.
- Aggregate viewer acknowledgement durations and drop counts. The operator's dropped-count field currently has no incoming data.
- Implement clock alignment before reporting end-to-end latency percentiles. `rendered_in_ms` measures only client-side handling, and `timing.render_ms` is unused.
- Add session start, stop and restart controls, with re-warming after a glossary change.
- Localise server status details before passing them to the German viewer.

## Failure handling

- Reconnect ASR with backoff when the socket drops.
- Switch to the next configured adapter when `healthy` reports failure.
- Add an explicit MT outage fallback that emits no text rather than substituting English.
- Monitor `AudioSource.alive` and stalled reads so a dead microphone changes the session status instead of leaving it `live`.

## Quality

- Add per-course glossary settings, reload support and adherence scoring. The prompt supports terms, but the settings group is absent.
- Add per-stage timestamps to distinguish recognition, translation and delivery delays.
- Compare provider quality and latency on the same corpus.
- Measure fragment grammaticality when tuning `max_segment_ms`.
- Generate a corrected transcript from archived audio with a post-lecture recognition pass.
- Add a bilingual viewer mode through a separate opt-in role. No English viewer toggle exists today.
- Evaluate stable-prefix translation using the LocalAgreement idea from `ufal/whisper_streaming` and `SimulStreaming`. The current pipeline starts MT after source commitment, not after prefix agreement.

## Language support

- Add German speech recognition with English subtitle output.
- Add German speech recognition with Arabic subtitle output.

## Deployment

- Rehearse a full session in the deployment hall with its network and a bilingual reviewer.
- Exclude the unmatched podcast intro from both the scored audio span and its reference so WER does not count unreferenced speech as recognition errors.

## Registered but not implemented

- Implement OpenAI ASR and then mark it active. It is registered as planned, so it holds a key and satisfies no requirement.

## Deferred, with reasons

| Item | Why |
| --- | --- |
| DeepL text MT | No token streaming. Per-character billing also needs evaluation if speculative retranslations are added. |
| DeepL Voice realtime | one call for recognition and translation, EU-hosted. A separate product from the text-MT decision |
| Multi-microphone beamforming | independent microphones cannot provide synchronised channels |
| A local ASR adapter | No local recogniser was selected for the reference machine without a GPU. Local MT can already use `adapter: openai_compat` and `requires_key: false`. |
| Projector overlay | A browser cannot draw over another application. OBS can instead use the viewer page as a browser source. |
| Diarization, audience Q&A, German TTS | need a second audio source and per-speaker handling |
| Scrolling transcript, word-level karaoke | The viewer renders one current subtitle. English timings do not directly align German word order. |
