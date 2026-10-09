# Extensions

- Extensions need an implementation and the matching factory or registry entry. The delivery layer can continue using the same publisher protocol.

## Provider extensions

| Route | For | Where |
| --- | --- | --- |
| config file | a provider this repository does not ship, or a different configuration of a built-in one | `providers:` in `livesubs.yaml` |
| registry | a provider that should ship with the repository | one entry in `src/livesubs/keystore.py` |

### The config-file route

- For a deployment-only provider, declare its `role`, key environment variable and adapter options under `providers:`.
- Keep the provider `planned` until its implementation exists. A declaration alone does not implement a new wire protocol.
- Config-only providers use their declared environment variable for credentials. The current key CLI lists and accepts names from the built-in registry, not the per-configuration provider merge.

### The registry route

- For a provider that ships with the repository, add a built-in registry entry:

```python
PROVIDERS["acme"] = Provider(
    name="acme",
    env_var="ACME_API_KEY",
    label="ACME (streaming ASR, English)",
    role="asr",
    hint="where to get a key",
    options={"model": "acme-1", "url": "wss://api.acme.test"},
    invariants=(
        ("mode", "streaming", "batch mode adds seconds of latency"),
    ),
)
```

- A built-in entry adds its name to `uv run livesubs --keys` and `uv run livesubs --set-key acme`.
- Settings can resolve its key and apply options such as `LIVESUBS_PROVIDER_ACME_MODEL`.
- Startup checks its role and declared invariants when the provider is active.

### Adapter construction

- The registry entry alone does not build the adapter.
- ASR: implement `AsrProvider` (`src/livesubs/asr/base.py`) under `src/livesubs/asr/`, and add a dispatch branch in `build_asr` (`src/livesubs/approaches.py`). `build_asr` constructs `DeepgramAsr` for the name `deepgram` and raises `NotImplementedError` for any other name, so a new registry entry without that branch still cannot run.
- MT: an OpenAI-compatible endpoint needs no new adapter. Declare the provider with `options.adapter: openai_compat`, and `build_mt` uses `OpenAiCompatMt`. A local endpoint also sets `requires_key: false`. A provider declaration alone does not implement a different wire protocol.

### Provider status

- `status: planned` excludes the provider from required-role and invariant checks. A stored key does not make it eligible for selection.
- Make the provider active only after the factory can construct its adapter. Key presence alone does not prove adapter availability.

## Approach registration

- An approach supplies a complete subtitle publisher. The implemented registrations are `sim`, `live-sim`, `live` and `live-text`.
- Implement the publisher, then register its builder in `src/livesubs/approaches.py`. `LocalPipeline` in this example is an implementation to supply, not a shipped class.

```python
def _build_local(settings, options):
    from livesubs.local import LocalPipeline        # lazy, to avoid import cycles
    return LocalPipeline(model=options.get("model", "small"))

register(
    Approach(
        name="local",
        label="Fully local",
        summary="Local recognition and translation, with no network and no API keys.",
        build=_build_local,
    )
)
```

- `--approach local` builds it, and `--approaches` lists it.
- It does not become the default automatically. `_default_approach` prefers `live`, then `sim`, then the first available approach, so a new plugin is selected by name or when neither `live` nor `sim` is available.
- The publisher must implement `SegmentPublisher.run`:

```python
async def run(self, hub: Hub, *, session_id: str, stop: asyncio.Event) -> None: ...
```

- `live-sim` shares `pipeline.py` with `live`. A new builder can reuse that pipeline with different adapters rather than reimplementing delivery.

## Capture backends

- Implement `AudioSource` to deliver 16 kHz mono PCM frames:

```python
def frames(self) -> AsyncIterator[AudioFrame]: ...
async def close(self) -> None: ...
@property
def alive(self) -> bool: ...
```

- Populate `AudioFrame.pcm`, `start_ms` and `duration_ms`, with prompt delivery and monotonic audio timestamps.

- Use existing PipeWire, WAV and synthetic sources as examples.
- Speech-like synthetic input exercises pause boundaries. A steady tone also exercises duration caps during continuous input.
- Add a lazy factory branch in `audio/factory.py` for the new `--audio` syntax. File replay must remain importable without PipeWire.

## Delivery clients

- Consume `hello`, `segment`, `status` and `error` messages from the existing WebSocket.
- Handle snapshots and reconnects, and pass publishing through the hub's ordering guard rather than writing competing revisions directly.

## Bilingual viewer extension

- Add a separate opt-in role and its client control, then test both role-specific message filters. Keep `ClientRole.VIEWER` unchanged.
