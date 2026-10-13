# Adapters and messages

## ASR adapter

```python
class WordTiming(BaseModel):
    text: str
    start_ms: int
    end_ms: int
    confidence: float | None = None

@dataclass(slots=True)
class AsrEvent:
    kind: Literal["partial", "final"]
    text: str
    start_ms: int
    end_ms: int
    confidence: float | None = None
    words: list[WordTiming] | None = None
    speech_final: bool = False

class AsrProvider(Protocol):
    async def open(self, cfg: AsrConfig) -> None: ...
    async def push(self, pcm: bytes) -> None: ...
    def events(self) -> AsyncIterator[AsrEvent]: ...
    async def close(self) -> None: ...
    @property
    def healthy(self) -> bool: ...
```

- `push` takes raw 16 kHz mono s16le PCM. Adapters handle their own framing and transport.
- `words` may be `None`. Deepgram supplies word timings. OpenAI's live transcription models do not, and must not be assumed to.
- Only Deepgram has a built ASR adapter. An OpenAI ASR provider can be declared and hold a key, but `build_asr` accepts only `deepgram` and raises for anything else.
- `partial` events replace each other for the current span. `final` settles the span: its text will not change, and the next event starts a new span instead of repeating it. A chunked provider such as Deepgram settles several spans inside one utterance.
- `speech_final` is the provider's own endpointing signal, independent of `kind`. The segmenter treats it as a hint, never as a requirement.
- `events()` must end or raise when the upstream stream dies. The pipeline learns that a recogniser is gone only that way. Deepgram ends the stream when its socket closes, errors, or stays silent for two probe intervals, and `healthy` then reports false.

## MT adapter

```python
@dataclass(slots=True)
class MtChunk:
    text_delta: str
    done: bool = False

@dataclass(slots=True)
class MtContext:
    source_context: list[str]      # previous source sentences
    target_context: list[str]      # previous German output, for terminology consistency

class MtProvider(Protocol):
    async def warm(self) -> None: ...
    def translate(
        self, src: str, ctx: MtContext
    ) -> AsyncIterator[MtChunk]: ...
    @property
    def healthy(self) -> bool: ...
```

- Each target translator calls `warm()` only when its language has viewer demand, before that target's first translation. Shared recognition does not wait for an unrelated target to warm. Cache warm-up is best-effort, not a latency guarantee.
- `translate` yields text deltas. The publisher accumulates them into draft revisions, then publishes a final translation. Drafts are spaced at least 120 ms apart after the first words, which go out at once.
- `translate` raises `MtError` when the request fails, including after some deltas. It never ends normally on a partial translation, because only the pipeline knows what a viewer has already been shown.
- Any OpenAI-compatible endpoint is accepted for MT when the provider declares `options.adapter: openai_compat`. Without that declaration the provider is refused rather than guessed at.

## Wire contract

- One JSON envelope per WebSocket message.
- Segment messages contain text and metadata, not audio.

```json
{
  "v": 1,
  "type": "segment",
  "data": {
    "seq": 412,
    "rev": 2,
    "state": "draft",
    "language": "de",
    "de": "…die Schwingung lässt sich durch…",
    "src": "…the oscillation can be described by…",
    "confidence": 0.94,
    "timing": { "audio_ms": 812345, "emit_ms": 813100 }
  }
}
```

| `type` | Purpose |
| --- | --- |
| `hello` | session metadata on connect: version, language, session id |
| `segment` | a subtitle line, new or revised |
| `status` | pipeline health: `live`, `delayed` or `stopped` |
| `error` | a stage diagnostic shown to the operator |

### Field rules

- `seq` identifies a source segment. Each target language has its own monotonic `rev` sequence, starting at 0 for a new `seq`.
- A newer `seq` is offered to display pacing, which can replace or drop it. A newer `rev` on the same `seq` updates the line in place.
- `state` is `draft` or `final`. `final` means no further revisions for that `(language, seq)`, so the overlay may start its hold timer.
- `language` defaults to `de`; German text is in `de`, Arabic text in `ar`. Mailbox ordering, snapshots and encoding caches distinguish languages.
- German viewers receive only German segments, and `viewer_ar` receives only Arabic segments. The server removes English source text, source word timings and the other target's text from both viewer roles. Operators retain source text.
- `timing` carries monotonic millisecond timestamps. `audio_ms` is the end of the audio span, which is on the audio clock.
- The admin console records a local `render` time from the moment a message arrived. A true client end-to-end `render_ms` round trip is not implemented, because server and client clocks are not synchronised.

## Current error behaviour

| Condition | Implemented behaviour |
| --- | --- |
| transient socket close | The adapter stops its reader. With viewer demand, the pipeline retries with a fresh recogniser and bounded backoff. |
| repeated provider failure | a stage exception publishes `error` with that stage. The pipeline does not switch adapters |
| translation returns empty | publish `error` for stage MT and drop the segment. English is never shown as a substitute |
| translation fails quickly | The adapter raises `MtError`. The pipeline retries once. |
| translation fails after words were shown | The line is finished with a trailing `…` so its hold timer runs. It is not kept as context and not written to the subtitle files. |
| translation passes its deadline | The segment is abandoned and not retried. The deadline is the provider's `timeout_s`, 8 s by default. |
| translator slower than the speaker | Past `max_backlog` queued segments (6) the oldest are discarded. At `lag_queue_limit` (3) the status is `delayed`. |
| provider cannot be built | a missing key or missing adapter raises at startup. There is no automatic stub fallback from `live` |
| initial ASR auth failure | Raises during the connection attempt. No transcription is produced. |
