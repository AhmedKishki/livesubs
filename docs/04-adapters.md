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
- Only Deepgram has a built ASR adapter. OpenAI is registered as planned: it can hold a key and satisfies nothing, so a key for it alone does not pass `--check`.
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
    target_context: list[str]      # previous output in the target language, for terminology consistency

class MtProvider(Protocol):
    async def warm(self) -> None: ...
    def translate(
        self, src: str, ctx: MtContext
    ) -> AsyncIterator[MtChunk]: ...
    @property
    def healthy(self) -> bool: ...
```

- A translator is built for one target language. The pipeline builds it, calls `warm()` and starts its worker only when that language has viewer demand, before that target's first translation. Shared recognition does not wait for an unrelated target to warm. Cache warm-up is best-effort, not a latency guarantee.
- `translate` yields text deltas. The publisher accumulates them into draft revisions, then publishes a final translation. Drafts are spaced at least 120 ms apart after the first words, which go out at once.
- `translate` raises `MtError` when the request fails, including after some deltas. It never ends normally on a partial translation, because only the pipeline knows what a viewer has already been shown.
- Any OpenAI-compatible endpoint is accepted for MT when the provider declares `options.adapter: openai_compat`. Without that declaration the provider is refused rather than guessed at.
- The OpenAI-compatible adapter builds its prompt from the speaker's language and the target language. The language names come from [languages.py](../src/livesubs/languages.py), and the German target keeps the exact prompt text recorded in `tests/fixtures/prompts_v1.json`, so its provider cache prefix does not change.

## Session modes

- The speaker language selects the mode. An English speaker has the targets `de` and `ar`, and a German speaker has the targets `en` and `ar`. The first target is the default.
- `session.targets` can narrow a mode to some of its targets, in order. A target outside the mode is rejected at startup.
- A publisher declares the languages it can produce with `supported_languages`. The session offers the mode's targets that the publisher can produce. A publisher that declares nothing is offered the default target only.
- The live pipeline takes one translator per target as a mapping from language code to provider or provider factory. A factory is called only when that language first has viewer demand.

## Wire contract

- One JSON envelope per WebSocket message.
- Segment messages contain text and metadata, not audio.

```json
{
  "v": 2,
  "type": "segment",
  "data": {
    "seq": 412,
    "rev": 2,
    "state": "draft",
    "language": "de",
    "text": "…die Schwingung lässt sich durch…",
    "src": "…the oscillation can be described by…",
    "confidence": 0.94,
    "timing": { "audio_ms": 812345, "emit_ms": 813100 }
  }
}
```

| `type` | Purpose |
| --- | --- |
| `hello` | session metadata on connect: version, session id and name, the speaker's `source` language, the offered `targets`, and this connection's own `language` |
| `segment` | a subtitle line, new or revised |
| `status` | pipeline health: `live`, `delayed` or `stopped`, for everyone or for one `language` |
| `error` | a stage diagnostic shown to the operator |

### Field rules

- `seq` identifies a source segment. Each target language has its own monotonic `rev` sequence, starting at 0 for a new `seq`.
- A newer `seq` is offered to display pacing, which can replace or drop it. A newer `rev` on the same `seq` updates the line in place.
- `state` is `draft` or `final`. `final` means no further revisions for that `(language, seq)`, so the overlay may start its hold timer.
- Every segment names its `language` and carries its words in `text`. No field is specific to one language, so a new language changes no message. Mailbox ordering, snapshots and encoding caches distinguish languages.
- A viewer receives one language. The plain `viewer` role receives the mode's first target, and `viewer_<code>` receives that target if the session offers it. The server removes the speaker's source text, source word timings and every other language from both. Operators retain source text and receive every language.
- A status with a `language` concerns that language's readers only, so a translator that is down is shown to its own viewers. A client with several scopes shows the worst. Status without a `language` concerns everyone.
- The `hello` to a viewer carries the connection's `language`, which a page compares with what it asked for.
- The role is a content filter for a trusted network, not access control. Anyone who can reach the server can request `admin`.
- `timing` carries monotonic millisecond timestamps. `audio_ms` is the end of the audio span, which is on the audio clock.
- The admin console records a local `render` time from the moment a message arrived. A true client end-to-end `render_ms` round trip is not implemented, because server and client clocks are not synchronised.

## Current error behaviour

| Condition | Implemented behaviour |
| --- | --- |
| transient socket close | The adapter stops its reader. With viewer demand, the pipeline retries with a fresh recogniser and bounded backoff. |
| repeated provider failure | a stage exception publishes `error` with that stage. The pipeline does not switch adapters |
| translation returns empty | publish `error` for stage MT and drop the segment. The speaker's language is never shown as a substitute |
| translation fails quickly | The adapter raises `MtError`. The pipeline retries once. |
| translation fails after words were shown | The line is finished with a trailing `…` so its hold timer runs. It is not kept as context and not written to the subtitle files. |
| translation passes its deadline | The segment is abandoned and not retried. The deadline is the provider's `timeout_s`, 8 s by default. |
| translator slower than the speaker | Past `max_backlog` queued segments (6) the oldest are discarded. At `lag_queue_limit` (3) the status is `delayed`. |
| provider cannot be built | a missing key or missing adapter raises at startup. There is no automatic stub fallback from `live` |
| initial ASR auth failure | Raises during the connection attempt. No transcription is produced. |
