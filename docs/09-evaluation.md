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

- `evaluation.chrf_sentence` and `evaluation.chrf_corpus` compute character n-gram agreement. Character matching preserves partial overlap between compounds and inflected forms, but valid rephrasing can still lower a score.
- **Numeral adherence**, implemented as `evaluation.number_preservation`. It counts source numerals that survive into the German, without a German reference.
- **Source-echo fraction**, implemented as `evaluation.identical_to_source_fraction`, to catch a translator that returns its input unchanged.
- **Length ratio**, implemented as `evaluation.length_ratio`, in German characters per English word, to check the overlay's line-length assumptions in [06-overlay-spec.md](06-overlay-spec.md).
- Entity preservation beyond numerals is planned.
- Fragment grammaticality would count German outputs missing a finite verb. The metric is planned for [segment-length comparisons](03-segmentation.md).
- **Terminology adherence** is planned. The glossary it depends on is not built.

### Scoring tool

- `tools/verify_against_reference.py` scores a session's own files against a reference. It makes no provider call, so it can score an old recording.
- It reports the reference-free checks from the session's own English and German, and chrF once a German reference or timed windows exist.
- `--self-test` corrupts the reference with a known error rate and fails when the metric does not reproduce it.
- A generated transcript is the pipeline's own output, a hypothesis rather than ground truth.

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

## Acceptance targets

- These are targets, not passing results.
- Keep p95 perceived latency under 2.5 s over a full lecture, without upward drift.
- Exceed 95% terminology adherence on the glossary subset.
- Preserve every source number in translation.
- Replacement of a settled line by a newer sequence respects `MIN_VISIBLE_MS` (800 ms) from first display. Test draft replacement, revisions and retirement separately.
- Test the [runbook failure cases](08-operations.md) during rehearsal and compare the reports with documented behaviour.
