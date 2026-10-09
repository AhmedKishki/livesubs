# Speech segmentation

## Requirement

- Queue speech for translation at a pause or the configured duration cap. Very short runs remain open until the minimum duration is reached.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `endpoint_silence_ms` | 350 | Close after this much trailing silence, once the minimum duration is met. |
| `max_segment_ms` | 2000 | Close at this duration even during unbroken speech. |
| `min_segment_ms` | 400 | Hold a shorter run despite a pause or endpoint. |
| `context_segments` | 2 | Include this many previous English and German segments in MT context. |
| `trust_provider_endpointing` | true | Allow a provider endpoint to close a run. VAD remains a backstop. |

## Why 2 s

- At 150–210 spoken words per minute, a 2 s segment contains about 5–7 English words. At an assumed reading rate of 200 wpm, those words take about 1.5–2 s to read.
- A larger cap gives the translator more of a clause but delays the request. A smaller cap produces shorter fragments and more requests.
- German subordinate clauses can put verbs at the end, so a short fragment may omit information needed for its translation.
- The 2 s default is a starting point, not a measured optimum. Compare 1500, 2000 and 3000 ms against real speech and German readability.

## State machine

```text
        speech detected
              |
              v
       +--------------+   silence >= endpoint_silence_ms   +-----------+
       |   OPEN       | ---------------------------------> |  COMMIT   |
       | accumulating |                                    +-----------+
       +--------------+                                          |
              |  elapsed >= max_segment_ms                        |
              +---------------------------------------------------+
                                                                  |
                                                     +------------v-----------+
                                                     |  EMIT -> translate     |
                                                     |  draft, then revisions |
                                                     +------------+-----------+
                                                                  |
                                                                  v
                                                     idle -> next segment
```

- The duration cap can close a segment while the speaker is still talking.

## Draft and revision

- After the source segment closes, MT output is published as `draft` revisions while German text accumulates.
- Each update retains the same `seq` and increases `rev`.
- A completed translation becomes `final`. Later ASR updates do not revise that segment.

## Context carry

- Each MT request includes up to `context_segments` previous English segments and their German translations.
- Previous context can help with references and terminology, but it cannot supply words that the speaker has not said yet.
- Rolling context travels in the user message. The system prompt stays fixed during a session.

## Interaction with ASR endpointing

- Deepgram performs its own end-of-speech detection and reports `speech_final`.
- Deepgram's settled chunks (`is_final`) are not necessarily speech endpoints. The adapter uses `speech_final` when mapping events to the pipeline's endpoint hint.
- The cap is checked first. Below `min_segment_ms`, an endpoint or pause does not close the run.
- With `trust_provider_endpointing: true`, a provider endpoint can close a run after the minimum duration.
- Energy-VAD silence remains a backstop in both modes. Setting `trust_provider_endpointing: false` ignores provider hints, not the cap or VAD.
- Resetting the segment buffer after an emit prevents the same observed boundary from closing it twice.
