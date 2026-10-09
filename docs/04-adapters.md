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
- `speech_final` is the provider's own endpointing signal. The segmenter treats it as a hint, never as a requirement.
- Deepgram marks the connection stale after two unanswered keepalive probes. `healthy` then reports false, but the pipeline does not consume it to reconnect or switch adapters.

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

- The pipeline calls `warm()` before recognition opens. A provider's cache warm-up is best-effort, not a latency guarantee.
- `translate` yields text deltas. The publisher accumulates them into draft revisions, then publishes a final translation.
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

- `seq` is monotonic per session. `rev` starts at 0 for a new `seq`.
- A newer `seq` is offered to display pacing, which can replace or drop it. A newer `rev` on the same `seq` updates the line in place.
- `state` is `draft` or `final`. `final` means no further revisions for that `seq`, so the overlay may start its hold timer.
- The hub removes English `src` from viewer messages. Operator messages retain it for comparison with the translation.
- `timing` carries monotonic millisecond timestamps. `audio_ms` is the end of the audio span, which is on the audio clock.
- The admin console records a local `render` time from the moment a message arrived. A true client end-to-end `render_ms` round trip is not implemented, because server and client clocks are not synchronised.

## Current error behaviour

| Condition | Implemented behaviour |
| --- | --- |
| transient socket close | the adapter stops its reader and marks itself unhealthy. It does not reconnect |
| repeated provider failure | a stage exception publishes `error` with that stage. The pipeline does not switch adapters |
| translation returns empty | publish `error` for stage MT and drop the segment. English is never shown as a substitute |
| provider cannot be built | a missing key or missing adapter raises at startup. There is no automatic stub fallback from `live` |
| initial ASR auth failure | raises when the stream opens, before the first pipeline frame |
