# Architecture

## Pipeline

```text
  microphone (capture backend, 16 kHz mono PCM)
        |
        v
  [ INGEST ]   DC offset, adaptive noise floor, energy VAD, metering, clipping report
        |      this layer is the pipeline clock: time is derived from samples consumed
        |
        |------ frames while viewers exist -----> [ ASR ]  streaming adapter
        |                                          partials, finals, word languages
        v
  [ SEGMENTER ]  silence >= endpoint_silence_ms OR elapsed >= max_segment_ms
        |        commits text and its source-language classification
        |------ source commitment -------------> [ RAW SUBTITLES ] optional
        v
  [ OUTPUT ]     one ordered worker per target: English, German and Arabic
        |        matching transcription, otherwise lazy streaming MT
        |
        v
  [ HUB ]        WebSocket fan-out, seq/rev revision protocol, src stripped for viewers
        |
        v
  [ OVERLAY ]    subtitles in the attendee's browser
```

## Concurrency and ordering

- Startup prepares capture. Paid recognition starts with the first viewer and pauses with the last disconnect. Each target performs MT only with readers and an item that needs translation. Capture and its archive clock continue while idle; those frames are not buffered for later recognition.
- Capture reads frames and feeds ingest. It never publishes.
- One ASR stream and segmenter feed independent ordered output workers, one per target language. Transcription and MT use the same queue. Each language has its own context and revision counter, so a slow request in one language does not block another or get overtaken within its target.
- Capture pushes each frame to ASR before updating the segmenter, without waiting for recognition responses.
- Ingest uses a sample-derived clock. File acceleration changes wall-clock pacing, not the underlying audio timestamps.

## Stage roles

| Stage | Input | Output |
| --- | --- | --- |
| Ingest | PCM frames | Speech flag, levels, audio clock |
| Segmenter | Speech flag, provider endpointing, cap | Emit decision and committed source text/classification |
| ASR | PCM while viewers exist | Partial/final text, optional word timings and language evidence |
| Target output | One committed source record plus target-specific context | One final transcription or streamed MT output in that language |
| Hub | Revisions | One JSON envelope per message per viewer |

- A segment's source text is assembled from settled chunks and the open hypothesis. Language evidence follows surviving tokens through cuts. Later ASR updates cannot change a committed segment; the known retimed-word repetition defect remains on the roadmap.
- Source recording observes commitment once, before independent target work. Target output starts after commitment. A matching transcription needs no translator, while an MT-required item builds and warms its translator within the request deadline. A target without viewers is not warmed or called.

## Latency budget

- Target: under 3 s end-to-end, aiming for about 1.5 s, measured from the end of the audio span to the pixel on screen.

| Stage | Budget | Notes |
| --- | --- | --- |
| capture, resample, VAD | 20–50 ms | `pw-record` delivers 16 kHz mono directly |
| ASR partial to final | 300–800 ms | a recognition target, not a guaranteed provider response time |
| MT time to first token | 200–900 ms | depends on provider response time |
| MT completion of about 25 tokens | 150–500 ms | streamed, so the first words appear early |
| WebSocket fan-out | 20–100 ms | text payloads, not streams |
| client render | under 16 ms | one line swap per frame budget |

- The segment cap limits how long speech accumulates before a translation request is queued.
- Overall lag also depends on MT and delivery, so the cap alone does not bound it.
