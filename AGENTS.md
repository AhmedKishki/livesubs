# Development

- Read the [document map](docs/index.md) before changing a subsystem.

## Constraints

- Keep live microphone capture server-side on Linux with PipeWire. Do not move capture into the browser.
- Derive the target languages from the speaker language: an English speaker has German and Arabic listeners, and a German speaker has English and Arabic listeners. Keep the default viewer on the mode's first target only. Every other target requires its explicit `viewer_<code>` role. Filter other translations and the source text on the server for each viewer role.
- Keep paid transcription paused without viewers and each translation paused without demand for its target language.
- Keep provider transport, types, and exceptions behind the ASR and MT protocols.
- Reject unavailable live adapters or credentials instead of substituting simulated output.
- Preserve monotonic `seq` and `rev` values. Do not revise a final segment or send a draft after its final revision.
- Keep the frontend as directly served ES modules. Node is a development dependency, not a deployment requirement.
- Keep the unauthenticated service on a trusted LAN. Do not expose it to the internet.

## Local data

- Do not commit API keys, lecture recordings, or corpus media.
- Leave `livesubs.yaml` gitignored unless the user explicitly asks to version a deployment.
- Keep key prompts hidden and saved credential files owner-only (`0600`) with atomic writes.
- Do not log keys or full request headers. Preserve the raw-export warning for the explicit `--no-save` option.
- Do not log lecture text, in any language, at INFO level.

## Checks

- Install the development dependencies before running the Python checks.

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
node --test web/tests/*.test.mjs
```

- Use explicit `--approach sim` or `--approach live-sim` for credential-free runs. The default approach is `live`.
- Check layout changes in a real browser at phone and laptop sizes.
- Regenerate the shared fixtures after changing pacing behaviour, then run both test suites.

```bash
uv run python tools/gen_pacing_vectors.py
```

## Git workflow

- Commit all intended changes and push after each substantial, verified change. Keep independent work in separate commits so it can be reverted.

## Documentation changes

- Write README explanations as prose. Use bullets there only for lists or procedural steps.
- Keep README limited to getting started with live microphone subtitles and Ubuntu/Debian installation.
- Keep each prose paragraph on one physical line. Preserve line breaks in code blocks, tables and lists.
- Describe implemented behaviour separately from planned work.
- Preserve commands, conditions, exceptions, measurements, and source provenance when shortening text.
- Write short, concrete sentences in headings, complete-sentence bullets, tables, and command examples.
- Keep each rule in one owner and remove completed-development narratives.
