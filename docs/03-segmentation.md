# Speech segmentation

## Requirement

- Queue speech for translation at a pause or the configured duration cap. Very short runs remain open until the minimum duration is reached.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `endpoint_silence_ms` | 350 | Close after this much trailing silence, once the minimum duration is met. |
| `max_segment_ms` | 2000 | Close at this duration even during unbroken speech. |
| `min_segment_ms` | 400 | Hold a shorter run despite a pause or endpoint. |
| `context_segments` | 2 | Include this many previous source and target-language segments in MT context. |
| `trust_provider_endpointing` | true | Allow a provider endpoint to close a run. VAD remains a backstop. |

## Why 2 s

- At 150–210 spoken words per minute, a 2 s segment contains about 5–7 words of English speech. At an assumed reading rate of 200 wpm, those words take about 1.5–2 s to read.
- A larger cap gives the translator more of a clause but delays the request. A smaller cap produces shorter fragments and more requests.
- German subordinate clauses can put verbs at the end, so a short fragment may omit information needed for its translation.
- The 2 s default is a starting point, not a measured optimum. Compare 1500, 2000 and 3000 ms against real speech and the readability of each target language.

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

- After the source segment closes, MT output is published as `draft` revisions while target-language text accumulates.
- Each update retains the same `seq` and increases `rev`.
- A completed translation becomes `final`. Later ASR updates do not revise that segment.

## Context carry

- Each MT request includes up to `context_segments` previous source segments and translations for that target language. Each target language keeps its own context.
- Previous context can help with references and terminology, but it cannot supply words that the speaker has not said yet.
- Rolling context travels in the user message. The system prompt stays fixed during a session.

## Assembling the segment text

- A streaming recogniser describes speech in spans. While a span is open each event replaces the last hypothesis. When the span settles its text is complete, and the next event starts a new span that does not repeat it.
- Deepgram settles a chunk every few seconds, several times inside one utterance. An interim result is cumulative from the start of its chunk and knows nothing about the pipeline's cuts.
- The assembler (`src/livesubs/transcript.py`) keeps the settled text, the open hypothesis and how far the text has been sent. A segment gets only what has not been sent.
- How far text has been sent is measured on the recogniser's own stream clock from word timings, by the middle of each word, so a hypothesis that revises or re-times earlier words cannot make them repeat. A recogniser without timings is handled by counting the words of the open span.
- Words recognised after a cut belong to the next segment. A chunk that settles between two cuts is kept until it is sent.
- Without the assembler, a cut through a chunk was followed by an interim that began with the words already sent, and a chunk that settled between two cuts was overwritten by the next one. Real recordings showed both.

## Recognition activity

- A run opens when the energy VAD hears speech or the recogniser produces text. A voice below the VAD threshold is still recognised.
- New recognised text counts as speech when deciding that the speaker has paused. The last words before a pause are recognised a moment after it, and closing at the VAD's pause would leave them to the next segment as a fragment.
- A cue still ends at the last frame the VAD called voiced. Only when the VAD heard nothing in a run does the recogniser's last result end the cue.

## Interaction with ASR endpointing

- Deepgram performs its own end-of-speech detection and reports `speech_final`.
- Deepgram's settled chunks (`is_final`) are not speech endpoints. The adapter reports them as `final` events and reports `speech_final` separately, and only `speech_final` is the pipeline's endpoint hint.
- The cap is checked first. Below `min_segment_ms`, an endpoint or pause does not close the run.
- With `trust_provider_endpointing: true`, a provider endpoint can close a run after the minimum duration.
- Energy-VAD silence remains a backstop in both modes. Setting `trust_provider_endpointing: false` ignores provider hints, not the cap or VAD.
- Resetting the segment buffer after an emit prevents the same observed boundary from closing it twice.
