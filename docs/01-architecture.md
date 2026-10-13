# Architecture

## Pipeline

```text
  microphone (capture backend, 16 kHz mono PCM)
        |
        v
  [ INGEST ]   DC offset, adaptive noise floor, energy VAD, metering, clipping report
        |      this layer is the pipeline clock: time is derived from samples consumed
        |
        |------ every frame, continuously ------> [ ASR ]  streaming adapter
        |                                          partials, finals, word timings
        v
  [ SEGMENTER ]  silence >= endpoint_silence_ms OR elapsed >= max_segment_ms
        |        commits the recognised text since the last cut as one segment
        v
  [ MT <lang> ]  one ordered stream per target language, active only while someone reads it
  [ MT <lang> ]  for example German and Arabic, or English and Arabic
        |
        v
  [ HUB ]        WebSocket fan-out, seq/rev revision protocol, src stripped for viewers
        |
        v
  [ OVERLAY ]    subtitles in the attendee's browser
```

## Concurrency and ordering

- Startup prepares capture. Paid recognition and translation start with the first viewer and pause with the last disconnect. Capture and its archive clock continue while idle; those frames are not buffered for later recognition.
- Capture reads frames and feeds ingest. It never publishes.
- One ASR stream and segmenter feed independent ordered translation workers, one per target language. Each language has its own context, queue and revision counter, so a slow request in one language does not block another.
- Capture pushes each frame to ASR before updating the segmenter, without waiting for recognition responses.
- Ingest uses a sample-derived clock. File acceleration changes wall-clock pacing, not the underlying audio timestamps.

## Stage roles

| Stage | Input | Output |
| --- | --- | --- |
| Ingest | PCM frames | Speech flag, levels, audio clock |
| Segmenter | Speech flag, provider endpointing, cap | Emit decision and the committed source text |
| ASR | Every PCM frame | Partial and final source-language text, word timings |
| MT | One committed source segment plus target-specific context | Streaming text in one target language |
| Hub | Revisions | One JSON envelope per message per viewer |

- A segment's source text is what the recogniser has produced since the previous cut, assembled from settled chunks and the open hypothesis. Text already sent is never sent again, and later ASR updates do not change it.
- MT starts after source commitment and publishes draft revisions as target text accumulates. A target without viewers is not warmed or called.

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
