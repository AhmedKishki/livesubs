# Providers

- Prices below are recorded design estimates, not current quotes. Re-check vendor documentation before committing a budget.

## Streaming ASR

### Deepgram, the default

- Adapter status: the only implemented speech-recognition adapter.
- Registry default host: `wss://api.deepgram.com`, the United States endpoint.
- EU residency: the `api.eu.deepgram.com` endpoint exists as an opt-in option, not the default.
- Models: `nova-3` monolingual, `nova-3` multilingual, and `flux-general-en`.
- Source language: the adapter opens each stream with the session's speaker language, English or German. `nova-3` serves both. `flux-general-en` is English-only, so it cannot serve a German speaker.
- Price: Nova-3 streaming at **$0.0048/min**, about **$0.43** per 90-minute lecture.
- Vendor documentation recommends Nova-3 for far-field, noisy and multi-speaker audio.
- Capture format: 16 kHz mono `linear16`, the format the capture path already produces.
- Word timings: word-level start and end times, plus per-word confidence.
- Translation: none, so machine translation is a separate stage.
- `flux-general-en` adds model-native end-of-turn detection, which maps onto emitting after a pause. It is a candidate to compare against Nova-3.
- A recorded API run returned punctuated Deepgram finals with word timings and no dropped frames at real time. The run date and corpus were not recorded with that observation.

### OpenAI `gpt-live-transcribe`

- Adapter status: registered as an ASR provider with a key slot, but no adapter is implemented, so it cannot serve recognition.
- Price: **$0.017/min**, about **$1.53** per lecture, and about 3.5 times Deepgram.
- Latency: tunable through `audio.input.transcription.delay`, with values `minimal`, `low`, `medium`, `high` and `xhigh`.
- Extras: keyword hints, unstructured context, and multiple language hints.
- Missing: no word timestamps, no confidence, and no speaker labels.
- Endpoint: `POST /v1/realtime/transcription_sessions`, over WebSocket, with base64 24 kHz PCM16.

### Others considered

- Speechmatics: realtime translation with `enable_partials`, a custom dictionary of up to 20,000 items, an on-premise GPU container, 48-hour sessions, and an Audio filtering feature that removes background speech. It is not registered.
- AssemblyAI: not evaluated in depth.

## Machine translation

- A session translates from the speaker's language, English or German, into the targets of its mode: German and Arabic for an English speaker, English and Arabic for a German speaker.
- The recorded cost and quality comparisons below use English-to-German workloads. Arabic and German-to-English costs and quality have not been measured against the corpus.

### DeepSeek `deepseek-flash`, the default

- Protocol: OpenAI-compatible at `base_url = https://api.deepseek.com`, so the `openai` SDK client serves it.
- The recorded model names are `deepseek-flash` and `deepseek-v4-pro`. Vendor documentation at design time described `deepseek-chat` and `deepseek-reasoner` as retired aliases served by DeepSeek-V4.1-Flash and billed at Flash prices.
- Pricing per 1M tokens, off-peak and peak:

| | cache hit | cache miss | output |
| --- | --- | --- | --- |
| `deepseek-flash` | $0.003 / $0.006 | $0.15 / $0.30 | $0.6 / $1.2 |

- Recorded peak hours are 01:00-04:00 and 06:00-10:00 UTC on weekdays. Compare a lecture's UTC time with those windows rather than assuming every evening is off-peak.
- The cost summary uses a recorded translation budget of about $0.08 for a 90-minute lecture with about 2,700 segments. No complete token breakdown was recorded for that budget.
- At the off-peak output price, 18,000 output tokens cost about $0.011. That figure excludes input charges and is not directly comparable to the $0.08 budget.

#### Current adapter behaviour

- The adapter streams plain text. JSON would expose partially received markup such as `{"de": "Hallo` to the viewer.
- The adapter raises `MtError` on any failure and sets `max_retries=0` on the SDK. The default `timeout_s` is 8 s, and the pipeline applies the same limit to a whole segment. A model on the capture machine needs a larger `timeout_s`.
- DeepSeek's declared realtime invariant requires `thinking: disabled`.
- It keeps a frozen system prompt so a stable prefix can qualify for DeepSeek's cache-hit input price.
- The adapter places rolling context in the user message to retain a stable system-prompt prefix.
- It can bake a glossary into the frozen system prompt. No glossary is configured, so the list is empty.
- It does not use JSON output or Chat Prefix Completion.
- It calls `warm()` before the first segment. Cache construction can take seconds, but warming does not guarantee a cache hit or response time.
- Each target language has its own frozen prompt, built from the speaker's language and that target, with the same configured provider and key. A target is constructed and warmed only when someone reads it, and it stops making requests when its audience is empty.
- Recorded API observations put DeepSeek time to first token at 489–909 ms per segment. The run date and corpus were not recorded with those observations, so the range is not a performance guarantee.
- Vendor features not used by the adapter include Chat Prefix Completion (beta), which pre-fills an assistant reply, and JSON Output, which constrains a response to an object such as `{"de": "..."}`.

#### Hosting caveat

- DeepSeek is China-hosted.
- DeepSeek receives text, not audio. The ASR provider separately receives audio.
- Round-trip time from Europe and time to first token both affect latency. Evaluate data residency before sending student speech.
- Another OpenAI-compatible MT endpoint can use the same adapter through configuration.

### DeepL, planned with no adapter

- Status: registered as **planned**. A key can be stored and verified now, but no adapter exists, so it is unused and does not satisfy the translation requirement.
- Hosting: EU (Cologne), ISO 27001 and SOC 2 Type II, with no training on customer data.
- Modes and controls, from vendor documentation:
  - `model_type: latency_optimized` is an explicit latency-first mode.
  - Glossaries use `glossary_id`, and `glossary_ids` accepts up to 5. A glossary requires `source_lang` to be set.
  - `translation_memory_id` takes a match threshold, default 75%.
  - `context` is supported, as are `custom_instructions` of up to 10 items of 300 characters, with German explicitly supported.
- Trade-offs to settle before wiring it up:
  - **No token streaming.** `/v2/translate` returns a complete translation in one POST, so no German appears until the full segment translation returns. The latency trade-off for roughly 25-token segments is unmeasured.
  - **Per-character billing with no warm cache.** Retranslating growing ASR prefixes would bill each repeated character. A design estimate assumed 1.5 to 2 times the raw character count, or about 130,000 to 170,000 billed characters for 85,000 characters of speech. The current pipeline translates committed segments once, so that estimate applies only to a future speculative-retranslation policy.
  - The endpoint exposes `outline_detection`, `splitting_tags` and `non_splitting_tags`. Compare `context` and `custom_instructions` on clause fragments before selecting the provider.
- DeepL needs an MT adapter before corpus comparison.
- DeepL also ships a separate real-time **Voice API** at `POST /v3/voice/realtime` over WebSocket. It emits source transcriptions and translations as text, with vendor-managed tentative transcript segments, and it would replace both the recogniser and the translator in one call.

### Other comparators

- Azure Translator and Google Cloud Translation offer EU regions and complete-response text translation. Neither is registered. Evaluate their German-specific controls separately.
- One-call alternatives, including `gpt-realtime-translate`, Speechmatics and DeepL Voice, would replace both stages with one vendor rather than allowing independent stage selection.

### Hosted model prices

- Surveyed on **2026-09-22** against the providers' own pricing pages.

| Option | Input /1M tokens | Output /1M | Note |
| --- | --- | --- | --- |
| `deepseek-flash` direct, off-peak | $0.15 (cache hit $0.003) | $0.60 | 50x discount on a stable cached prefix |
| `deepseek-flash` direct, peak | $0.30 | $1.20 | peak is 01:00-04:00 and 06:00-10:00 UTC, weekdays |
| DeepSeek-V4.1-Flash on DeepInfra | $0.14 (cached $0.004) | $0.42 | the same model without the peak and off-peak difference |
| GLM-5.3-Flash on DeepInfra | $0.075 (cached $0.015) | $0.25 | |
| DeepSeek-V3.2 on DeepInfra | $0.26 (cached $0.13) | $0.38 | |
| Google Cloud Translation (NMT) | $20 per 1M characters | none | first 500,000 characters each month free |
| DeepL Pro | subscription from €7.49/month for 300,000 characters | none | billed per character |

- Ninety minutes of speech is about 100,000 characters and about 18,000 output tokens.
- Output-only estimates are about a cent for `deepseek-flash` off-peak and half a cent for GLM-5.3-Flash. Google NMT is about $2 for the character count, before its free allowance. DeepL also has a subscription charge.
- These billing units differ. A full comparison must include LLM input tokens, cache misses, and repeated requests as well as output.

#### What differs between hosts

- **Latency and streaming.** Measure time to first token and completion on lecture fragments. Groq and Cerebras were candidates for faster inference, but no price was captured from their client-rendered pricing pages.
- **Cache billing.** The listed DeepSeek cache-hit input rate is 50 times lower than its cache-miss rate. Actual cost depends on the cache-hit rate.
- **Data residency.** DeepSeek is China-hosted. DeepInfra advertises zero-retention endpoints. EU hosting options include Mistral, Nebius and Scaleway.
- **German quality and glossary control.** Compare fragment quality and support for required terminology. A dedicated NMT endpoint without a glossary or instruction parameter cannot accept those controls.

### Self-hosting

- Candidate models: a 77M-parameter Marian model (`opus-mt-en-de`), distilled NLLB-200, and Argos Translate.
- All three run on CPU without a GPU.
- Hosting a model on the capture machine avoids sending inputs to a translation vendor. Compute, setup and maintenance still contribute to cost.
- None is the default or has a shipped adapter. Compare speed, German fragment quality, and terminology/context support before choosing an offline implementation.

### The one-hop alternative

- OpenAI `gpt-realtime-translate`: `POST /v1/realtime/translations`, over WebSocket or WebRTC.
- It returns translated audio plus transcript deltas while the speaker is still talking.
- It is priced by audio duration at **$0.034/min**, about **$3.06** per lecture.
- Sessions stream continuously from audio and are not driven by `response.create`.
- The recorded price is about 6 times the two-hop budget. Recognition and translation use one provider, without a separate MT stage or glossary configuration.
- An adapter could support comparison with the two-hop pipeline or a future outage fallback. Neither is implemented.

## Cost summary, 90-minute lecture

| Configuration estimate | Cost |
| --- | --- |
| Deepgram Nova-3 + DeepSeek Flash | about $0.51 |
| OpenAI gpt-live-transcribe + DeepSeek Flash | about $1.61 |
| OpenAI gpt-realtime-translate (one-hop) | about $3.06 |
| Azure speech translation (up to 2 target languages) | about $2.50/hr list |

- These are budgeting estimates, not measured bills. OpenAI ASR and the one-hop alternatives need adapters before they can run here.
- Azure's row lists an hourly price rather than a 90-minute total.
