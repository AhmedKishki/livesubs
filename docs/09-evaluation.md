# Evaluation

## Per-segment record

- Segment timings, request durations and viewer acknowledgements use different clocks and units.

| Field | Meaning | Status |
| --- | --- | --- |
| `audio_ms` | end of the audio span that produced the text | implemented, on `segment.timing` |
| `emit_ms` | the server pushed the revision to viewers | implemented, on `segment.timing` |
| `rendered_in_ms` | local handling duration before acknowledgement, clock-free | implemented, in the viewer `ack` |
| `dropped` | overlay drop count so far | implemented, in the viewer `ack` |
| MT time to first token | duration from request start to first text delta | implemented as `last_ttft_ms` and a per-call log |
| MT total duration | duration from request start to completion | implemented in the per-call log |
| `mt_first_token_ms` | absolute first-token timestamp | planned |
| `asr_partial_ms` | first partial from the provider | planned |
| `asr_final_ms` | final transcript for the span | planned |
| `mt_done_ms` | translation complete | planned |
| `render_ms` | absolute overlay render time | planned, needs clock alignment |

- Perceived latency measures time from the end of the audio span to visible text.
- Lag drift measures the change in perceived latency over a lecture.

## Perceived latency and clock alignment

- The server clock and the browser clock are not synchronised, so subtracting `audio_ms` from a browser timestamp is not a valid end-to-end figure.
- A true end-to-end figure needs an estimate of the offset between the two time origins.
- Until that estimate exists, the viewer reports `rendered_in_ms`, measured from when it accepted the segment, and the wire field `render_ms` stays unused.
- Percentiles over cue durations are pacing, not latency. `evaluation.percentiles` computes them from the session's own subtitle timings.

## Golden set

- Build a golden set from archived hall audio with reviewed references.
- Collect 20–30 minutes from at least two lectures, covering the following conditions.

| Coverage | Requirement |
| --- | --- |
| Terminology | Include a lecture with domain names, acronyms, units and formulas. |
| Critical tokens | Annotate numbers, dates, units and proper nouns. |
| Delivery | Include fast speech and a speaker trailing off. |
| Room conditions | Include audience noise and a question from the floor when available. |
- Store the set as `tests/fixtures/` references plus a small, versioned transcript file.
- Do not commit the audio. It is gitignored and may be sensitive.

## Implemented metrics

### ASR (English)

- `evaluation.wer` computes overall word error rate.
- `evaluation.wer_on_numbers` computes the numeral slice.
- The named-entity slice is planned.

### MT (English to German)

- The corpus, references and metrics in this document are English-to-German. German-to-English and Arabic output have not been evaluated.

- `evaluation.chrf_sentence` and `evaluation.chrf_corpus` compute character n-gram agreement. Character matching preserves partial overlap between compounds and inflected forms, but valid rephrasing can still lower a score.
- **Numeral adherence**, implemented as `evaluation.number_preservation`. It counts source numerals that survive into the German, without a German reference.
- **Source-echo fraction**, implemented as `evaluation.identical_to_source_fraction`, to catch a translator that returns its input unchanged.
- **Length ratio**, implemented as `evaluation.length_ratio`, in German characters per English word, to check the overlay's line-length assumptions in [06-overlay-spec.md](06-overlay-spec.md).
- Entity preservation beyond numerals is planned.
- Fragment grammaticality would count German outputs missing a finite verb. The metric is planned for [segment-length comparisons](03-segmentation.md).
- **Terminology adherence** is planned. The glossary it depends on is not built.

### Scoring tool

- `tools/verify_against_reference.py` scores a session's own files against a reference. It makes no provider call, so it can score an old recording.
- Use the raw `.source.srt` as the recognition hypothesis, not an English reader file that may contain translated text. The tool reports reference-free source/German checks and chrF once a German reference or timed windows exist.
- `--self-test` corrupts the reference with a known error rate and fails when the metric does not reproduce it.
- A generated transcript is the pipeline's own output, a hypothesis rather than ground truth.

### Conversation (English and German)

- `tools/splice_conversation.py` cuts windows from English and German recordings, joins them in the given order with silence between them, and writes a 16 kHz mono WAV and a turn manifest. The manifest gives each turn's language and span on the session's audio clock. The tool needs `gst-launch-1.0`.
- The WAV starts with 5 s of silence so that viewers can connect before the first turn. Without a viewer the live pipeline drops the audio.
- Replay it with `livesubs --serve --approach live --speaker auto --audio file:PATH`, with a viewer connected for each language to check. A scripted viewer must answer heartbeats and acknowledge finals as the viewer page does ([07-delivery-qr](07-delivery-qr.md)). Otherwise the server drops it after 30 s, and recognition stops for lack of an audience.
- `tools/check_conversation.py --turns MANIFEST STEM` checks target SRT files against their own language and the raw `.source.srt` against the dominant overlapping turn. It lists wrong-language lines and turns without output. Add `--source-code en+de` explicitly for legacy conversation recordings; never treat an English reader file as the raw mixed transcript.
- `evaluation.guess_language` reads a line's language from English and German function words, and Arabic from its script. A line with no function word, or with as many of each, counts as too short to tell.
- Delay is `emit_ms - audio_ms` from the segment timings, on the server clock. It covers recognition, segmentation and translation, not the network or the phone.
- Spliced clips have no replies to each other and no shared room sound, so they test language switching and not a real conversation's acoustics or overlapping speech.

```bash
uv run python tools/splice_conversation.py \
  --clip en:clips/en.mp3@60-80 --clip de:clips/de.mp3@60-66 --clip en:clips/en.mp3@80-84 \
  --out samples/conversation-16k-mono.wav
uv run python tools/check_conversation.py \
  --turns samples/conversation-16k-mono.turns.json --source-code en+de recordings/demo-20261023-101500
```

#### Recorded run, 2026-10-23

- This run predates word-language routing: every target used MT. It does not measure the redesigned transcription path or manual recognition handover.
- Audio: 118.6 s of ten alternating turns, 3 to 20 s long, cut from part 1 of two public-domain LibriVox readings. English came from *The Metamorphosis* (`metamorphosis_1401_librivox`, 60–114 s) and German from *Die Verwandlung* (`verwandlung_mw_0811_librivox`, 60–113 s).
- Providers: Deepgram Nova-3 `multi` with `endpointing=100`, and DeepSeek `deepseek-flash`, with English, German and Arabic viewers connected throughout.
- Every turn produced at least one line in every file.
- None of the 55 lines in each target file was in the wrong language. Between 8 and 13 lines per file were too short to tell.
- Two of the 55 transcript lines were in the wrong language, both at a switch. One segment spanned the end of an English turn and the start of a German one. In the other, English speech just after a German turn was recognised as the German words "hing das Bild".
- Same-language speech reached its readers close to the transcript, recognition errors included. German readers saw "ne, flimmbatten ihm hilf" where the English translation read "no, flickered before his eyes".
- Server-side delay over 58 segments per language: first words p50 0.93–0.96 s and p95 1.47–1.54 s; finals p50 1.04–1.08 s, p95 1.55–1.64 s and at most 1.79 s.

### Planned metrics

- BLEU and COMET would add translation-comparison scores.
- Endpointing evaluation would count disagreements between provider endpoints and segment boundaries.

## Comparison programme

- Run candidates on the same golden set, noting which comparisons need an adapter or a metric first.

| Question | Candidates |
| --- | --- |
| Streaming ASR | Deepgram Nova-3, `flux-general-en`, OpenAI `gpt-live-transcribe` at `delay=low` (no adapter yet) |
| MT alternatives | `deepseek-flash` and DeepL (no adapter yet) |
| Does context carry help? | `context_segments` 0 vs 2 |
| Is 2 s the right cap? | `max_segment_ms` 1500, 2000 and 3000 against fragment grammaticality |
| Is one-hop competitive? | `gpt-realtime-translate` (no adapter yet) against the two-hop pipeline |

- Compare translation quality as well as time to first token, completion time and billing.

## Bilingual human review

- Watch a real lecture with a German speaker and have them mark every line that was wrong, confusing, too fast to read, or too slow.
- Review meaning, grammar and readability that the implemented automated metrics do not capture.

## Development without credentials

- Use `sim` for viewer layout and pacing checks without provider calls.
- Replay cases must include long German compounds and revisions that replace words, not just append them.
- Use accelerated replay for layout checks and real-time replay for pacing checks.
- The `fast_speaker` case uses a 700ms cadence to test the replacement floor.
- `live-sim` checks the audio-processing and delivery paths using invented English text and a passthrough echo. Its sidecars are not suitable for recognition or German-quality scores.
- `live-text` runs the real translator over a timed transcript, which evaluates the German and the overlay without an ASR key.
- Unit/local-browser tests validate routing and controls without establishing provider recognition quality. A new paid rehearsal is required to measure current accuracy, latency and handover gaps; no such run is claimed by the redesign.

## Acceptance targets

- These are targets, not passing results.
- Keep p95 perceived latency under 2.5 s over a full lecture, without upward drift.
- Exceed 95% terminology adherence on the glossary subset.
- Preserve every source number in translation.
- Replacement of a settled line by a newer sequence respects `MIN_VISIBLE_MS` (800 ms) from first display. Test draft replacement, revisions and retirement separately.
- Test the [runbook failure cases](08-operations.md) during rehearsal and compare the reports with documented behaviour.
