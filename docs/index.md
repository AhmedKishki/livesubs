# Documentation

| File | Scope | When to read |
| --- | --- | --- |
| [README](../README.md) | Ubuntu/Debian installation and the first live microphone subtitle session. | Getting started. |
| [AGENTS](../AGENTS.md) | Development checks and constraints for code and documentation changes. | Before changing the repository. |
| [00-overview](00-overview.md) | Application scope, requirements, and non-goals. | Assessing whether the application fits a deployment. |
| [01-architecture](01-architecture.md) | Pipeline data flow and latency targets. | Changing stage coordination or investigating latency. |
| [02-audio-capture](02-audio-capture.md) | Device selection, calibration, signal conditioning, and archive mechanics. | Setting up audio or changing capture. |
| [03-segmentation](03-segmentation.md) | Speech boundaries, context carry, and endpointing. | Tuning or changing speech segmentation. |
| [04-adapters](04-adapters.md) | Recognition and translation interfaces and the WebSocket message contract. | Implementing an adapter or changing messages. |
| [05-providers](05-providers.md) | Provider capabilities, recorded prices, comparisons, and data residency. | Choosing or evaluating a provider. |
| [06-overlay-spec](06-overlay-spec.md) | Subtitle layout, display pacing, controls, accessibility, and browser checks. | Changing or testing the viewer. |
| [07-delivery-qr](07-delivery-qr.md) | URL selection, QR delivery, and network diagnostic limits. | Investigating phone connectivity. |
| [08-operations](08-operations.md) | Deployment, the lecture runbook, rehearsal, and retention policy. | Preparing a lecture or diagnosing a running session. |
| [09-evaluation](09-evaluation.md) | Evaluation methods, measurements, and acceptance targets. | Comparing output or checking performance. |
| [10-roadmap](10-roadmap.md) | Unimplemented and deferred work. | Choosing development work. |
| [11-extending](11-extending.md) | Provider, pipeline, capture, and delivery extension procedures. | Adding an implementation. |
| [12-configuration](12-configuration.md) | Configuration resolution, settings, credentials, and the command reference. | Changing settings or using less common commands. |
| [Sample corpus](../samples/README.md) | Corpus preparation, scoring, timed-reference alignment, and reference limitations. | Replaying or evaluating sample audio. |

## Source and input files

| File | Scope | When to read |
| --- | --- | --- |
| [livesubs.yaml.example](../livesubs.yaml.example) | Annotated configuration template. | Creating or editing deployment settings. |
| [pyproject.toml](../pyproject.toml) | Package dependencies, entry points, and development-tool settings. | Changing packaging or tooling. |
| [__main__.py](../src/livesubs/__main__.py) | Installed command dispatch, startup checks, and server entry points. | Changing CLI behaviour. |
| [launcher.py](../src/livesubs/launcher.py) | Microphone choice and credential setup before serving. | Changing live startup. |
| [audio/selection.py](../src/livesubs/audio/selection.py) | Arrow-key selection and terminal restoration. | Changing terminal input. |
| [languages.py](../src/livesubs/languages.py) | The language registry and the speaker-language modes. | Adding a language or a mode. |
| [language.js](../web/static/language.js) | Viewer language selection, role mapping, interface strings and target text filtering. | Changing viewer language behaviour. |
| [pipeline/](../src/livesubs/pipeline/) | The live pipeline: on-demand lifecycle, recogniser session, per-language translation workers and health reporting. | Changing how paid work starts, stops or reports problems. |
| [transcript.py](../src/livesubs/transcript.py) | Assembles recognition events into the text of each segment. | Changing how segment text is built. |
| [events.py](../src/livesubs/events.py) | Authoritative message models and protocol version. | Changing the wire schema. |
| [asr/base.py](../src/livesubs/asr/base.py) and [mt/base.py](../src/livesubs/mt/base.py) | Adapter protocols and context types. | Implementing recognition or translation. |
| [audio/base.py](../src/livesubs/audio/base.py) | Audio source protocol and frame types. | Implementing a capture backend. |
| [pacing_vectors.json](../spec/pacing_vectors.json) | Shared Python and JavaScript pacing fixtures. | Checking pacing parity. |
| [livesubs.service](../deploy/livesubs.service) | Example systemd user unit. | Deploying a supervised process. |
| `livesubs.yaml` and `.env` | Local deployment settings and credentials. | Configuring a checkout. |
| `samples/*.mp3`, `samples/*.pdf`, and input `samples/*.srt` | Externally supplied corpus media and transcripts. | Preparing the sample corpus. |
| `samples/*-16k-mono.wav` | Derived replay audio. | Running a corpus replay. |
| `samples/reference.en.txt` and `samples/reference.de.txt` | Derived reference text. | Scoring recognition or translation. |
| `samples/reference.en.timed.srt` | Aligned English reference cues. | Running timed-transcript translation. |
| `samples/corpus-manifest.json` | Corpus digests, duration, and text counts. | Checking corpus inputs and measurements. |
| [LICENSE](../LICENSE) | Repository license terms. | Redistributing the project. |
