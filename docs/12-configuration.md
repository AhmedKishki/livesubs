# Configuration

## Local settings

- Create `livesubs.yaml` with `--init-config`, then edit only the settings that differ from defaults.
- Local settings and credential files are gitignored. The tracked starter template is `livesubs.yaml.example`.
- `capture.artifacts_dir`, or `LIVESUBS_ARCHIVE_DIR`, sets the directory for audio and subtitle files.

## Commands

- Use the installed `livesubs` command. For development in a checkout, prefix it with `uv run`.

| Command | What it does |
| --- | --- |
| `livesubs` or `livesubs --start` | Choose a microphone, check credentials and capture, then start a live session. |
| `livesubs --audio pw:TARGET` | Preselect a microphone without skipping interactive confirmation. |
| `livesubs --serve` | Run the server without interactive startup. |
| `livesubs --serve --approach NAME` | Choose how subtitles are produced. |
| `livesubs --serve --audio SPEC` | Use `synth`, `file:PATH[@SPEED]` or `pw:TARGET` directly. |
| `livesubs --approaches` | List registered approaches and availability. |
| `livesubs --preflight` | Check capture, signal, clipping and network diagnostics. |
| `livesubs --check` | Validate configuration without probing providers. |
| `livesubs --init-config [PATH]` | Write a starter config, default `./livesubs.yaml`. |
| `livesubs --config PATH --check` | Validate a specific configuration. |
| `livesubs --keys` | Show masked key status and resolution sources. |
| `livesubs --set-key NAME` | Store a key through a hidden prompt. |
| `livesubs --unset-key NAME` | Remove a stored key. |
| `livesubs --replay SCRIPT` | Print replay frames as JSON without serving. |
| `livesubs --version` | Print the version. |

| Flag | Applies to |
| --- | --- |
| `--host`, `--port`, `--session-id`, `--session-name` | Startup or `--serve`. |
| `--speaker auto\|en\|de` | Startup, `--serve`, `--check` or `--preflight`. Overrides recognition intent; an explicit flag skips the startup speaker picker. Deprecated `en+de` means `auto`. |
| `--exit-on-failure` | Startup or `--serve`. Exit with status 1 if the pipeline dies, so a supervisor restarts the process. |
| `--script`, `--speed`, `--once` | the `sim` approach |
| `--audio` | A `pw:TARGET` preference for startup, or a direct input with `--serve`. |
| `--cues PATH` | `--approach live-text` |
| `--speed` | also sets the pace for `--replay` |
| `--verify`, `--scope project\|global`, `--no-save` | `--set-key` |
| `--scope project\|global` | `--unset-key` |

- Not every flag maps to a YAML setting, and not every YAML setting has a flag.
- `--init-config` refuses to overwrite an existing file.

### Default approach and keys

- With no action flag, the command runs live startup. `--serve` skips setup for explicit inputs, simulations and service integrations.
- Interactive startup always shows the microphone list. Non-interactive startup requires a configured or explicit source and existing credentials.
- Startup saves newly entered keys under the user's configuration directory, not the installed Python environment. Manual `--set-key` still defaults to project scope, so use `--scope global` for per-user storage.
- The default approach is `live`. Configuration checks therefore require usable providers unless another approach is selected.
- Select a credential-free mode explicitly for configuration checks or test replay:

```bash
livesubs --check --approach sim
livesubs --serve --approach live-sim --audio file:lecture.wav@0
```

## Where the config file is found

1. Use the path supplied through `--config`, if given.
2. Otherwise use `$LIVESUBS_CONFIG`, if set.
3. Search `./livesubs.yaml`.
4. Search `<repository>/livesubs.yaml`.
5. Search `~/.config/livesubs/livesubs.yaml`.

- An unsuccessful implicit search uses defaults. A nonexistent explicitly selected path fails when settings load.

```bash
uv run livesubs --init-config
uv run livesubs --check
uv run livesubs --config /srv/sub/livesubs.yaml --check
```

## Precedence

- Settings resolve in the order below, highest first. Run-specific flags such as `--host`, `--port` and `--audio` override their corresponding choices.

| # | Source | Use for |
| --- | --- | --- |
| 1 | real environment variable | a one-off run: `LIVESUBS_PORT=9000 uv run livesubs --check` |
| 2 | `./.env` | secrets and this machine's overrides |
| 3 | `~/.config/livesubs/.env` | secrets shared across checkouts |
| 4 | `livesubs.yaml` | the deployment's structure and choices |
| 5 | built-in defaults | everything else |

- Empty environment values count as unset. An exported nonempty value wins over saved values.
- `server.port` and `LIVESUBS_PORT` address the same setting.

## Recognition and fallback

- `session.speaker`, or `LIVESUBS_SESSION_SPEAKER`, is `auto` by default, `en` for an English pin, or `de` for a German pin. `--speaker` overrides it for one run. Deprecated `en+de` is normalised to `auto` with a migration warning; an explicit existing English setting remains English.
- Every setting offers English, German and Arabic subtitles in canonical order. The operator's fallback is `session.default_language`, or `LIVESUBS_SESSION_DEFAULT_LANGUAGE`, with values `en`, `de`, `ar` and built-in default `en`. It does not depend on target order.
- Remove `session.targets` from old deployments. A deprecated complete `en/de/ar` declaration is accepted and normalised; subsets, duplicates and unsupported codes fail validation instead of narrowing the offer. A speaker override does not hide an invalid target declaration.
- Interactive startup preselects resolved recognition/fallback values and lets the operator change them before microphone setup. No terminal means no picker; `--serve` skips interactive setup. `/admin` can change the current run without rewriting configuration, as described in [QR delivery](07-delivery-qr.md).
- Automatic opens Nova-3 `multi`; manual selection opens its single-language configuration. All translators use stable English/German subtitle-rendering prompts. Demand and committed source evidence decide whether any given item needs MT.

## Groups

- Each top-level key mirrors a settings group. Python also exposes read-only flat accessors:

```python
settings.server.port          # grouped
settings.port                 # flat accessor, same value
```

- Construct settings through the groups, not flat accessor names:

```python
Settings(server={"port": 9000})     # correct
Settings(port=9000)                 # silently ignored
```

## Providers

- Use `providers:` to override named options without replacing the provider's other defaults:

```yaml
providers:
  deepgram:
    options:
      model: nova-3
```

- Options resolve from `LIVESUBS_PROVIDER_<NAME>_<KEY>`, then the configuration, then the registry default.
- Automatic requires compatible Nova-3 configuration; model/language overrides are optional and must match the selected setting. Application construction validates recognition configuration without connecting, rejecting contradictory overrides rather than silently defeating the choice.
- A new provider requires an explicit `role`:

```yaml
providers:
  acme:
    role: asr
    env_var: ACME_API_KEY
    label: ACME streaming ASR
    status: planned
    options:
      endpoint: wss://api.acme.test
```

- `status: planned` excludes the provider from startup requirements and invariant checks. Make it active only after its adapter is implemented.
- The declaration merges per `Settings` instance without modifying the built-in registry.
- The key CLI currently uses the built-in registry, so config-only `acme` does not appear in `--keys` or become an accepted `--set-key` argument. Supply its declared environment variable instead.
- An OpenAI-compatible MT declaration can use the existing adapter with `options.adapter: openai_compat`. A local endpoint can set `requires_key: false`.
- A different wire protocol needs an [adapter implementation](11-extending.md).

## Pipeline

- `pipeline:` orders provider candidates at startup, not runtime failover:

```yaml
pipeline:
  asr: [deepgram]
  mt: [deepseek]
```

- The first active candidate with a nonempty key, or with `requires_key: false`, is selected.
- Unlisted active providers for the role follow the listed candidates. An empty list uses registry order.
- The selected candidate must still have an adapter. A rejected key or missing adapter does not cause a retry with the next candidate.
- Selection happens once at startup. Nothing fails over while a session runs.
- `--check` prints the candidate chain and selection. Unknown names are configuration errors.

## Validated at startup

- `--check` rejects the following configuration problems:
  - A required stage has no active provider with usable credentials or a no-key declaration.
  - `pipeline.<role>` names an unregistered provider.
  - `segmentation.min_segment_ms` is at or above `segmentation.max_segment_ms`.
  - `capture.sample_rate` is not 16000, or `capture.channels` is not 1.
  - An active provider's realtime invariant fails, such as DeepSeek `thinking` not being `disabled`.

- A malformed `providers:` section fails while settings load.
- The check does not authenticate keys, construct adapters or test provider reachability. Passing it is not proof that a live session will start.

## Keys

- `--keys`, `--start` and `--serve` warn when a file that may hold keys can be read by other users, and print the `chmod 600` that fixes it.
- `--set-key NAME` prompts with hidden input (`getpass`), and never accepts a key as a command-line argument, because that exposes it in the process list and shell history.
- It writes owner-only (`0600`) and atomically.
- `--scope project` writes the `.env` beside this project's `pyproject.toml`, or in the working directory when there is none. A `pyproject.toml` that belongs to another project is ignored. `--scope global` writes `~/.config/livesubs/.env`.
- `--verify` probes the provider at entry time and does not save a rejected key.
- `--keys` shows every provider masked to the last four characters, with the resolution source.

- `--set-key NAME --no-save` prints a raw export line without saving the key.
- The line is not applied to the parent shell, so the caller must export it.
- The whole key appears in the terminal and can land in shell history.
- In Bash, a leading space keeps the command out of history only when `HISTCONTROL=ignorespace` is enabled.

```bash
uv run livesubs --set-key deepgram
uv run livesubs --set-key deepseek --verify
uv run livesubs --set-key deepgram --scope global
uv run livesubs --set-key deepgram --no-save
uv run livesubs --unset-key deepgram --scope global
uv run livesubs --keys
```

- `deepgram` (ASR) and `deepseek` (MT) are active and implemented.
- `openai` is registered as a planned ASR provider. It can hold a key and satisfies no requirement, because no adapter exists.
- `deepl` is planned. Its key can be stored and verified, and no adapter exists, so it does not satisfy the MT requirement.

## Capture

- `--preflight` lists sources when no microphone is configured. `--serve --audio pw:TARGET` selects a PipeWire source directly.
- A direct ALSA capture backend is not implemented.

## Why YAML

- YAML permits comments and nested provider settings. Parsing adds the `pyyaml` dependency.
