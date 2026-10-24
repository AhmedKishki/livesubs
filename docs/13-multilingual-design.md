# Automatic speaker languages

## Status and scope

Approved for implementation on 2026-10-24. The requirements below are not yet implemented. Approval covers the implementation plan, TDD, verification, commits and pushes; a paid rehearsal still requires separate authorisation.

The supported scenario is a meeting, conversation or event in which people take turns speaking English and German into one shared microphone. Every session offers English, German and Arabic subtitles, independently of the recognition setting. A lecture uses the same pipeline and controls as a conversation.

Spoken Arabic is outside this redesign's scope. Deepgram now documents Arabic support for Nova-3, so its exclusion is a product-scope decision, not a provider limitation. Overlapping speech, separate microphones, diarization, Flux evaluation, privacy-mode deployment and the known repeated-word boundary defect remain deferred.

## Recognition approach

Use one active recogniser. Automatic recognition opens Deepgram Nova-3 with `language=multi`; a manual English or German choice opens the corresponding single-language stream. Changing the choice replaces the recognition stream without replacing the running session.

Use provider word-language labels for automatic routing. A separate text-language classifier would introduce another decision that can disagree with recognition. Parallel recognisers would require transcript arbitration and additional paid streams without an established benefit for the shared-microphone scenario.

Automatic means that the application supports English/German speech; it does not mean Deepgram's multilingual model is restricted to those two languages. Unexpected language labels must prevent transcription passthrough rather than create an additional supported spoken language.

## Operator settings

| Setting | Values | Built-in default | Controls |
| --- | --- | --- | --- |
| Speaker language | `auto`, `en`, `de`. | `auto`. | Startup picker and a persistent control in the operator console. |
| Fallback subtitle language | `en`, `de`, `ar`. | `en`. | Startup selection and a live operator-console control. |

Persisted configuration supplies the initial selections. Keep `session.speaker`, its environment override and `--speaker` for recognition. Add `session.default_language` and its corresponding environment override for the fallback; it must not depend on target order.

Interactive startup presents Automatic, English and German with the resolved recognition choice preselected. It also makes the fallback easy to change. An explicit speaker flag bypasses the recognition picker. `--serve` skips interactive setup, and startup without a terminal uses resolved configuration without prompting. Preserve selected values if credential setup reloads settings.

Console changes apply to the current run only. They do not rewrite deployment configuration or credential files. Speaker and fallback changes are independent: neither changes which subtitle languages are offered.

The console shows the requested recognition setting separately from the active setting. It also shows the most recently committed source classification, including mixed and unknown, without presenting the requested setting as detected speech.

## Attendee language selection

Resolve the attendee's language once when the viewer page loads:

1. Honour an explicit supported `?lang=<code>` in the URL.
2. Otherwise inspect `navigator.languages` in preference order. Match each preference's primary language to `en`, `de` or `ar`, so `de-CH` selects German and `ar-EG` selects Arabic. If the preference list is absent or empty, inspect `navigator.language`.
3. If no browser preference matches, use the operator's current fallback.

A syntactically valid explicit code that is not offered displays an unavailable notice and opens no subtitle socket. Do not silently replace that explicit choice. Missing or malformed parameters follow automatic selection.

Show three always-visible buttons labelled English, Deutsch and العربية. Each alternative takes one tap. Indicate the current language accessibly and retain keyboard operation. Language is no longer hidden inside the Aa display-settings panel.

A manual selection always writes `?lang=<code>`, including a selection equal to the fallback. Preserve unrelated URL parameters and fragments. Reuse the existing page-navigation and socket-cleanup path rather than introducing in-place subscription mutation. Display preferences remain independent of language selection.

Do not save the language in local storage or cookies. A page retains its resolved language across socket reconnects; a fresh load of a plain session URL resolves preferences again. A live fallback change affects later page loads that have no matching browser preference, not existing readers.

The browser requests an explicit `viewer_<code>` role for every resolved language, including the fallback. The server pins each connection to that concrete language. The legacy plain `viewer` role resolves the fallback at connection time; a direct client using that alias must use an explicit role if it needs to retain its language across reconnects.

## Source-language evidence

Keep the recognition setting, observed language evidence and committed classification separate. `auto` is a setting, not a language label or an input to language-name lookup.

Add optional provider-neutral language evidence to recognised words and retain it with each transcript token. Evidence follows the same token through hypothesis replacement, settled spans and segment cuts. Classify only the tokens that remain in the commitment; a language belonging to an already-sent word must not affect a later segment.

Do not emit empty source commitments. In Automatic mode, classify nonempty commitments using the first matching row:

| Evidence in the committed text | Classification |
| --- | --- |
| Any token has an explicit unsupported language label. | `unknown`, including when English and German labels also occur. |
| Every spoken token has an English label. | `en`. |
| Every spoken token has a German label. | `de`. |
| Both English and German labels occur. | `mixed`, even if some tokens are unresolved. |
| Anything else, including an unresolved token. | `unknown`. |

Normalise regional labels to their supported primary language inside the adapter. Do not use majority voting, the previous segment's language, script heuristics or word-recognition confidence to fill missing language evidence. A dominant alternative-level language does not establish that every token is in that language.

In Automatic mode, text without word timings is eligible for passthrough only when the adapter supplies a provider-neutral guarantee that all of its textual tokens have one language. Text without that guarantee stays unknown.

A manual pin is a strict operator assertion about the speech. Tokens from a pinned stream inherit its selected language when the provider omits labels, including untimed text-only events without an independent language guarantee. Any explicit label contradicting the pin makes that commitment unknown; do not overwrite that evidence with the selected language. A pin cannot independently detect every actual language change: return to Automatic when speakers start switching again.

Metadata-only hypothesis updates replace stored language evidence without counting as new speech activity. Freeze classification when a source segment is committed. Later provider corrections must not revise that segment or any of its final subtitles. Reset language evidence with the assembler at a recognition-stream boundary.

## Ordered subtitle output

Offer every committed source segment to the workers for targets that currently have readers:

| Source classification | English target | German target | Arabic target |
| --- | --- | --- | --- |
| `en` | Transcription. | Translation. | Translation. |
| `de` | Translation. | Transcription. | Translation. |
| `mixed` or `unknown` | Translation. | Translation. | Translation. |

Both output paths run inside the existing ordered worker for that target. Never publish transcription directly from capture or source dispatch: it could overtake an earlier translation. Retain bounded queues, shedding, request deadlines and cancellation on loss of target demand. A transcription can wait behind an earlier translation until that request completes or times out.

Transcription publishes the committed text as one final revision, initially `rev=0`, without a translation request or translation warm-up. Build and warm the translator lazily on the first item that actually requires it. Validate required live credentials and adapter capabilities at startup; lazy execution must not allow an unusable deployment or simulated substitution.

Use a stable English/German subtitle-rendering prompt for each target, including requests whose text is already partly or wholly in the target language. This permits the conservative mixed/unknown route without rebuilding translators when recognition changes.

A successful transcription enters the same bounded, target-specific source/output context as a successful translation. Failed, cancelled, shed and undemanded items do not enter successful-output history. A working transcription path must not be counted as a successful MT request or clear an MT outage. An MT outage must not disable later eligible transcription output.

Recognition stays paused without viewers, and the operator console creates no viewer demand. Translation stays paused without readers for its target. Source sequence numbers remain session-wide; revisions are ordered within a target's sequence. A final is terminal: no later revision, draft or final may follow it.

## Live recognition changes

One recognition supervisor owns replacement. Each accepted speaker change advances a configuration revision. The latest accepted choice supersedes an older opening or pending change; stale callbacks, events and completed opens must not become active.

| State | Console meaning |
| --- | --- |
| Paused | No viewers; retain the requested setting without opening a paid stream. |
| Applying | The requested configuration is being applied; do not label it active yet. |
| Active | The recogniser has opened with the displayed setting. |
| Retrying | Opening or operation failed and the requested setting is awaiting another bounded attempt. |
| Failed | Attempts are exhausted or the setting cannot be applied; show the failure without claiming success. |

For an active stream, stop feeding new frames to the retiring recogniser, then attempt a bounded closing flush. Commit available text once under that stream's old configuration. Invalidate old event delivery after the boundary, reset recognition/segmentation state for the new stream, and open the latest requested configuration if viewers still exist.

Capture and its archive continue during this transition. Opening, closing and retry waits must not block microphone ingestion. The session identity, viewer sockets, archive clock, translation workers, committed queues and sequence counter remain intact.

This design does not add audio replay across the handover or promise lossless recognition. Frames captured while no stream is ready may be unrecognised; a failed or timed-out closing flush can lose an unfinished fragment. The console must show the interruption, and operator guidance should recommend manual changes during a pause. Automatic English/German turns do not require stream replacement.

Associate the replacement's timing origin with the first audio frame actually sent to it, not with the start of its connection attempt. Reset stream-relative watermarks and pending segmentation state while retaining the continuous capture clock. Already committed subtitles complete normally; a switch must not reset workers or reopen final segments.

Changing the setting with no viewers records intent only. Losing the last viewer during opening cancels the unnecessary stream. A new setting must wake retry or exhausted waits so the previous configuration cannot leave it stuck. On failure, report the requested setting as retrying or failed; never silently revert to Automatic or another provider.

## Control and message contracts

Add a session-scoped JSON mutation endpoint, `POST /api/session/{session_id}/settings`. It accepts `speaker`, `default_language`, or both. Reject invalid values and unknown fields without partially applying the request. Validate and serialise changes before recording intent. Repeating an unchanged speaker choice must not reopen recognition unnecessarily.

Return the current settings and a monotonically increasing control revision. A speaker-change acceptance means the request was recorded, not that recognition has already reopened. Expose requested and active settings, transition state, configuration revision and a safe error summary through the existing session/stats polling path. The console reconciles its controls from server state rather than assuming its last click succeeded.

Require JSON requests and reject cross-origin browser mutations. Do not enable permissive CORS. These checks do not add authentication: the service and its operator controls remain trusted-LAN-only, and roles remain content filters rather than access control.

Keep the segment envelope and `EVENT_VERSION = 2`. The segment's `language` still names its output language; it never becomes `auto`, `mixed` or `unknown`. Optional word-language metadata is additive. Keep a stable English/German source-capability descriptor in session metadata and expose the requested/active recognition setting separately rather than passing `auto` through language-registry operations.

Filter every viewer on the server. A same-language transcript is allowed in that viewer's `text` field, but raw `src`, word timings and other targets remain stripped. Browser filtering is an additional check, never the privacy or language-isolation boundary. Detection diagnostics must not add lecture text to stats or INFO logs.

## Recording and compatibility

When subtitle recording is enabled, write the raw committed transcript once, in source sequence order, to `STEM.source.srt` and `STEM.source.vtt`. Record it at source commitment, independently of target completion, so MT failures cannot erase or reorder the raw transcript.

Write successful reader output to `STEM.en`, `STEM.de` and `STEM.ar` subtitle files in the existing SRT/VTT formats. A same-language transcription appears once in its target file, not twice through source/target writer overlap. Preserve audio-clock cue spans, formatting, escaping, duration validation and recorder exception isolation. Failed or truncated translations remain excluded from successful-output exports.

Accept the existing `en+de` speaker value as a deprecated alias for `auto`, with an actionable migration warning. Keep existing `en` and `de` choices as manual pins. Do not silently rewrite configuration. The new built-in default is Automatic; an explicit old English setting remains English.

Retire target narrowing. An absent `session.targets`, or a deprecated declaration containing all three supported targets, resolves to the canonical offer `en, de, ar`. A subset, duplicate or unsupported entry fails validation with instructions to remove the setting. Publisher capability checks must reject a session that cannot produce all three rather than silently advertising fewer languages.

Update recording consumers for the `.source` identity, including the conversation-checking tool. Existing recordings retain their old filenames and are not renamed automatically. Compatibility behaviour and any explicit legacy-input option belong in the implementation plan; new recordings must never infer raw-source identity from a reader language.

Credential-free simulation must exercise English, German, mixed and unknown commitments with all three targets. Scripted recognition must describe the text it actually emits: it must not label English test phrases German merely because recognition was pinned to German.

## Acceptance checks

| Area | Required evidence |
| --- | --- |
| Language availability | Every speaker setting offers exactly English, German and Arabic. Narrowed or incapable deployments fail explicitly. |
| Recognition configuration | Automatic uses multilingual Nova-3; English/German pins use single-language streams. Provider overrides cannot silently defeat the selected setting. |
| Classification | Word labels survive hypothesis replacement and cuts. Missing, contradictory and unsupported evidence cannot take automatic-mode passthrough. Metadata-only updates do not extend speech activity. |
| Output routing | Matching homogeneous targets make no MT call or warm-up. Mixed/unknown text uses MT for every demanded target. Arabic never uses passthrough in this scope. |
| Ordering and finality | Slow translation followed by transcription stays ordered. Revisions increase within a sequence, and every final is terminal, including across demand and recognition changes. |
| Demand and failures | No viewers means no paid ASR; no target readers means no MT for it. Transcription works during an MT outage without falsely declaring MT recovery. |
| Live changes | Test speech, silence, opening, flush timeout, retry, exhaustion and shutdown; rapid competing changes; old events; and last-viewer departure. Only the latest requested configuration may activate. |
| Time and recording | Stream-relative clocks rebase to continuous capture time. Raw source is written once in commitment order, and each successful target output is written once without filename collision. |
| Attendee selection | Explicit links beat browser preferences; ordered regional matching and fallback work. Manual fallback-language choices stay explicit. A fallback change cannot change the viewer page's or any explicit-role client's language on reconnect; the legacy plain role resolves the current fallback. |
| Filtering and controls | Each viewer gets only its target text and no raw-source fields or word timings. Reject invalid JSON settings, partial mutations and cross-origin browser changes. |
| User interface | Check phone and laptop layouts in a real browser, including Arabic direction, keyboard operation, visible language buttons and requested-versus-active operator states. |
| Simulation | Credential-free runs cover all routing paths and preserve sequence/revision rules without claiming real provider availability. |

Implement through failing unit/integration tests before production changes. Before committing implementation, install development dependencies and run the repository checks:

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
node --test web/tests/*.test.mjs
```

Regenerate and check shared pacing fixtures only if pacing behaviour changes. This redesign does not intentionally change pacing or the token-cut algorithm.

A separately authorised paid rehearsal should replay alternating English/German turns, mixed boundaries and manual changes with responsive viewers for all three languages. Measure language correctness, final delay and recognition interruptions separately. Do not claim gap-free switching, new latency results or verified spoken-Arabic support from unit tests or provider documentation.

## Evidence and documentation owners

The approved design discussion establishes the product choices in this draft. Repository inspection establishes the existing single-stream lifecycle, ordered target workers and server-side role filtering; it does not establish that the proposed behaviour is implemented.

Provider documentation checked on 2026-10-24 confirms `language=multi`, per-word language labels, a 100 ms endpointing recommendation for code-switching, and Nova-3's broader language set: [Multilingual Codeswitching](https://developers.deepgram.com/docs/multilingual-code-switching) and [Models & Languages Overview](https://developers.deepgram.com/docs/models-languages-overview). [Finalize](https://developers.deepgram.com/docs/finalize) documents flushing and warns that a completion response is not guaranteed when little audio remains; a live handover therefore needs a timeout.

During implementation, update the operative constraints and documentation owners identified in the [document map](index.md). Keep README limited to installation and the first live session. Do not rewrite implemented-behaviour documentation to describe this proposal as shipped before its corresponding code and checks exist.
