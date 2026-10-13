# Roadmap

## Operator console and visibility

- Spread viewer reconnect delays randomly, so phones that lost the network together do not retry together.
- Make the draft interval a setting, so an operator can trade subtitle smoothness for a lower message rate on a crowded network.
- Offer a final-only delivery mode, which would cut the message rate per phone to about a quarter for a crowded network.
- Keep figures over time. The console shows the last 256 samples of each timing and the current counters, and keeps no history of a lecture.
- Implement clock alignment before reporting end-to-end latency percentiles. `rendered_in_ms` measures only client-side handling, and `timing.render_ms` is unused.
- Add session start, stop and restart controls, with re-warming after a glossary change.
- Localise server status details before passing them to viewers.

## Failure handling

- Reconnect ASR with backoff when the socket drops.
- Switch to the next configured adapter when `healthy` reports failure.
- Add an explicit MT outage fallback that emits no text rather than substituting the speaker's language.
- Monitor `AudioSource.alive` and stalled reads so a dead microphone changes the session status instead of leaving it `live`.

## Quality

- Add per-course glossary settings, reload support and adherence scoring. The prompt supports terms, but the settings group is absent.
- Add per-stage timestamps to distinguish recognition, translation and delivery delays.
- Compare provider quality and latency on the same corpus.
- Measure fragment grammaticality when tuning `max_segment_ms`.
- Generate a corrected transcript from archived audio with a post-lecture recognition pass.
- Add a bilingual viewer mode through a separate opt-in role. No viewer shows the speaker's words beside the translation today.
- Evaluate stable-prefix translation using the LocalAgreement idea from `ufal/whisper_streaming` and `SimulStreaming`. The current pipeline starts MT after source commitment, not after prefix agreement.

## Language support

- Measure recognition, translation quality and cost for a German speaker and for Arabic subtitles. Only English-to-German has corpus measurements.
- Have a reader of Arabic review the Arabic prompt, the simulated Arabic lines and the Arabic viewer strings. No such review is recorded.

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
| Diarization, audience Q&A, speech synthesis | need a second audio source and per-speaker handling |
| Scrolling transcript, word-level karaoke | The viewer renders one current subtitle. Source timings do not directly align target word order. |
