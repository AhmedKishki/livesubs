# Multilingual implementation plan

## Authority and scope

Implement the [approved specification](13-multilingual-design.md). Use failing tests before changing each behaviour. This plan chooses implementation details left open by the specification; it does not expand spoken-language support or authorise paid provider calls.

Keep changes on `main`. Do not create worktrees. Preserve the existing pacing and token-cut algorithm. Run all required checks before commits, and keep independently revertible changes in separate commits.

## Shared interfaces

| Boundary | Contract |
| --- | --- |
| ASR words | Add optional `WordTiming.language`. Normalise language tags inside adapters; unsupported explicit labels remain evidence, not missing data. |
| Untimed ASR | Add optional `AsrEvent.language`, meaning a guarantee that the entire event text is homogeneous. A dominant alternative language is not that guarantee. |
| Transcript commitment | Add immutable `TranscriptCommit(text, language)` and `TranscriptAssembler.commit_source(pin=None)`. Keep `commit()` returning text for existing callers. Both methods consume the same unsent tokens once. |
| Source queue item | Add immutable `SourceSegment(seq, text, start_ms, end_ms, language="unknown")` in pipeline records. `Item` names that record; migrate queue producers and consumers together. |
| Source recording | Add `LivePipeline(on_source=...)`, invoked once at source commitment, independently of target output. The recorder exposes `record_source(SourceSegment)`; its existing target callback writes targets only. |
| Recognition setting | `LivePipeline(..., speaker=None)` takes the resolved `auto`, `en` or `de` setting. If omitted by low-level callers, derive the pin from the existing ASR configuration for compatibility. Application construction passes it explicitly. |
| Runtime publisher control | `set_speaker(value) -> bool` validates and records intent synchronously; return whether requested state changed. `speaker_snapshot() -> dict` reports `requested`, `active`, `state`, `revision`, `error` and optional `detected: {seq, language}`. Implement it on live and simulated publishers. |
| Stable MT source | Build all target translators for English/German subtitle rendering (`en+de`), independent of the recognition pin. Never pass `auto` to language-name lookup. |
| Session controls | The session owns `default_language` and a settings revision. It delegates speaker intent to the publisher and exposes recognition state without pretending acceptance means activation. |

`AsrConfig.language` remains adapter-neutral: `en+de` for Automatic, `en` or `de` for a pin. Each stream receives an immutable configuration snapshot. Session/hello source capability remains `en+de`; the setting and last detected classification are separate fields.

## Limits and compatibility choices

Retain existing queue limits, translation deadlines and lifecycle retry/backoff defaults. Bound ASR open to 10 seconds. Use a 0.5-second collector flush grace and a 2-second provider-close bound for a live replacement; capture continues independently throughout. Cancellation remains bounded by the existing 2-second task grace. Tests use shorter injected or monkeypatched limits rather than wall-clock sleeps.

An unchanged requested speaker setting is a no-op: neither recognition configuration revision nor stream count advances. Every effective settings mutation advances the session control revision once; fallback-only changes do not advance recognition revision. Selecting a different setting invalidates older opening/retry work immediately. The latest accepted update wins.

Use the first `lang` query parameter, matching existing URL parsing. Trim and lowercase a two- or three-letter primary code. A missing, empty or malformed value requests automatic selection; a syntactically valid unsupported value displays unavailable. Normalise browser preference tags separately by primary language, preserving preference order. Manual links always use one canonical `lang` parameter.

Prefer `.source` recordings in the conversation checker. Add an explicit `--source-code en+de` option for old source filenames; never guess a source from a target file. Preserve existing archives without renaming them.

## Settings endpoint

Add `POST /api/session/{session_id}/settings` accepting a nonempty JSON object with `speaker`, `default_language`, or both. Values must be strings from the supported sets; reject unknown fields, invalid values and partial validation failures before mutating either setting.

Return HTTP 200 with `session_id`, `revision`, `speaker`, `default_language` and `recognition` from `speaker_snapshot()`. This acknowledges desired state only. Use 404 for an unknown session, 415 for a non-JSON content type, 422 for invalid JSON/settings, and 403 for a foreign browser Origin. Do not expose exception text containing provider input or headers.

Accept a missing Origin for trusted-LAN non-browser clients. When Origin is present, compare its normalised scheme/host/port with the request origin and reject a mismatch, including `null`. Require `application/json`; do not add CORS headers. Expose the same control fields in `/api/session` and operator snapshots. Update both settings atomically before yielding to the event loop.

## 1. Establish the baseline

Install development dependencies, then run Python tests, Ruff and Node tests. Record actual outcomes rather than inheriting the handoff's counts. Commit and push the approved specification, this plan and the updated development constraint only after those checks pass.

## 2. Retain language evidence

1. Add failing adapter and assembler tests for English, German, regional labels, unsupported labels, absent labels, metadata-only updates, mixed cuts, already-sent words and reset.
2. Add optional word/event language fields without changing `EVENT_VERSION = 2`.
3. Add token language retention and structured commitment. Apply the specification's classification precedence and manual-pin contradiction rules at commitment.
4. Keep `commit()` compatibility and existing cut behaviour. Exercise the shutdown and text-only cases without introducing a text-language classifier.
5. Reject Deepgram model/language overrides that silently defeat Automatic or a manual pin. Validate without making a provider request.

## 3. Add ordered transcription output and recording

1. Add failing worker tests for final-only matching output, zero MT construction/warm-up, mixed/unknown translation, slow-MT ordering, context and independent MT failure state.
2. Start target output workers on demand without warming translators. Build/warm MT inside the bounded request path only when needed; contain construction, warm-up and request failures to that item.
3. Introduce the source queue record and route both output methods through one worker per target. Preserve demand-generation checks, queue shedding and terminal finals.
4. Add a source-commit sink and collision-free raw/target recorder tests. Record source before offering target work.
5. Update checker compatibility and scripted inputs. Make simulation cover pure, mixed and unknown source commitments truthfully; do not mislabel stub English as German.

## 4. Decouple configuration and add live recognition changes

1. Add failing registry/configuration tests for fixed targets, Automatic default, `en+de` migration, full-set compatibility, rejected narrowing and independent fallback.
2. Keep recognition setting independent of fixed target availability and stable MT source. Validate live adapters/credentials at startup without warming MT.
3. Add supervisor invalidation distinct from failure. Fence opens, events and callbacks with recognition configuration revision as well as audience generation.
4. Add failing lifecycle/pipeline tests for changes during opening, speech, silence, flush, retry, exhaustion and shutdown; rapid requests; no audience; old events; and uncooperative closes.
5. Coordinate live closing flush with source commitment without blocking capture. Reset stream/segmentation state, rebase the clock to first-fed audio and retain workers, queues and session sequence.
6. Expose requested, active and transition states. Report replacement failures without silent fallback. Keep existing no-viewer and per-target-demand regression coverage.
7. Add startup speaker/fallback selection and preserve choices across credential reloads. Skip the speaker picker for an explicit flag and all setup under `--serve`.

## 5. Add session controls and attendee selection

1. Add failing session/route tests for fixed publisher capability, fallback validation, atomic settings changes, no-op revisions, foreign Origin and non-JSON requests.
2. Construct sessions with a distinct fallback and stable source capability. Resolve legacy plain roles at connection time; keep concrete subscriptions pinned.
3. Add failing JavaScript tests for explicit-link precedence, ordered regional preferences, unavailable codes, explicit roles, reconnects and canonical manual URLs.
4. Move language selection out of Aa into three visible native-name buttons. Preserve display preferences, navigation cleanup, language filtering and Arabic direction.
5. Add operator controls using the JSON endpoint and server-reconciled state. A failed request must show an error and cannot optimistically label a stream active. Separate live desired/active state from detected source classification.

## 6. Integrate, document and verify

Update the operative documentation through its existing owners, including changed language/filter constraints, configuration migration, viewer/QR behaviour, recording identities, runtime controls and deferred work. Keep the approved specification distinct from shipped documentation until corresponding behaviour passes its checks. Update example configuration without touching a local deployment or keys.

Run the complete required checks after integration:

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
node --test web/tests/*.test.mjs
```

Use credential-free served simulation for real Chromium checks at phone and laptop sizes. Check all three attendee languages, visible/keyboard-operable buttons, Arabic direction, operator controls, requested/active feedback, fallback changes and reconnection language retention. Reuse the existing overlay layout tool and extend checks only where controls require it. Save browser artifacts outside the repository.

Do not run a paid rehearsal. Report mocked/local integration evidence separately from unmeasured provider accuracy, latency and live handover duration. Do not regenerate pacing vectors unless integration intentionally changes pacing.

Before each implementation commit, inspect status, intended diff and recent history. Stage only intended source/tests/docs; do not stage keys, recordings, local deployment settings or generated browser artifacts. Split the independently compatible language-evidence change from dependent pipeline/control work where the verified interfaces permit it, then push the verified commits to `main`.
