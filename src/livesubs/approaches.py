"""Registry of whole-pipeline approaches.

An *approach* is a complete way of producing subtitles. Today there are two on the
drawing board: the simulated replay, and the live API pipeline. The ones this
design anticipates but does not build are local/self-hosted models, a one-call
speech-translation service, and a hybrid with a repair pass.

The server, the WebSocket layer and the overlay are indifferent to which is
running, because every approach implements
:class:`~livesubs.hub.SegmentPublisher`. So selecting an approach is a registry
lookup, and **adding one must not require editing the CLI, the server or the
hub** - only adding a registration below and an adapter behind it.

This is the extension point for "different approaches". See
``docs/11-extending.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from livesubs.config import Settings
from livesubs.hub import SegmentPublisher

if TYPE_CHECKING:
    # Annotations only. The implementations are imported inside the builders that need
    # them, so that listing the approaches cannot fail on a module a deployment lacks -
    # a machine without PipeWire still lists what is available.
    from livesubs.asr.base import AsrProvider
    from livesubs.audio.base import AudioSource
    from livesubs.mt.base import MtProvider
    from livesubs.pipeline import PipelineConfig

__all__ = [
    "APPROACHES",
    "Approach",
    "ApproachStatus",
    "artefacts",
    "build_approach",
    "build_asr",
    "build_mt",
    "pipeline_config",
    "register",
    "required_roles",
    "requires_credentials",
    "resolve_audio_spec",
]

ApproachStatus = Literal["ready", "planned", "retired"]

#: Options handed to an approach's builder: command-line and config values that are
#: relevant only to that approach. Keeping it a plain mapping is what lets a new
#: approach take its own options without the CLI learning about them.
ApproachOptions = Mapping[str, str]

_BUILD = Callable[[Settings, ApproachOptions], SegmentPublisher]


@dataclass(frozen=True, slots=True)
class Approach:
    """One way of producing subtitles."""

    name: str
    label: str
    summary: str
    status: ApproachStatus = "ready"
    #: ``None`` for an approach that is registered but not yet implemented, so the
    #: CLI can list it and give an honest error rather than a confusing one.
    build: _BUILD | None = None
    #: Why it is not available, shown when someone selects it.
    unavailable_reason: str | None = None
    #: Which provider roles this approach cannot work without. Empty for one that calls
    #: no vendor. A tuple rather than a bool because ``live-text`` needs a translator and
    #: no recogniser, and demanding an ASR key for it would block a run that works.
    required_roles: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.status == "ready" and self.build is not None

    @property
    def requires_credentials(self) -> bool:
        return bool(self.required_roles)


APPROACHES: dict[str, Approach] = {}


def register(approach: Approach) -> Approach:
    """Add an approach. Re-registering the same name replaces it, which tests rely on."""

    APPROACHES[approach.name] = approach
    return approach


def requires_credentials(name: str) -> bool:
    """Whether the named approach cannot work without a usable provider.

    An unknown name answers ``True``, because assuming an unidentifiable approach needs
    keys is the safe direction: the other way round starts a deployment that silently
    produces nothing.
    """

    approach = APPROACHES.get(name)
    return True if approach is None else approach.requires_credentials


def required_roles(name: str) -> tuple[str, ...]:
    """Which provider roles the named approach needs. Everything, if it is unknown."""

    approach = APPROACHES.get(name)
    return ("asr", "mt") if approach is None else approach.required_roles


def resolve_audio_spec(options: ApproachOptions, settings: Settings) -> str:
    """Which audio source an approach should use.

    In order of preference:

    1. an explicit ``--audio``, which is what a one-off run wants;
    2. the device named by ``capture.target``, because that is where ``--preflight``
       and the documentation tell an operator to set it, and a configured deployment
       should not quietly ignore its own microphone;
    3. synthetic audio, only when nothing at all is configured, so that a fresh
       checkout still does something.

    Step 2 exists because the alternative was worse: the docs said to set
    ``capture.target`` while the pipeline demanded ``--audio`` every run, so the
    setting appeared to work and did nothing.
    """

    spec = options.get("audio", "").strip()
    if spec:
        return spec

    target = settings.capture_target.strip()
    return f"pw:{target}" if target else "synth"


def build_approach(
    name: str,
    settings: Settings,
    options: ApproachOptions | None = None,
) -> SegmentPublisher:
    """Instantiate an approach by name.

    Raises ``KeyError`` for an unknown name and ``NotImplementedError`` for a
    registered-but-unbuilt one, so an operator gets a clear message rather than a
    half-started server.
    """

    try:
        approach = APPROACHES[name]
    except KeyError:
        known = ", ".join(sorted(APPROACHES)) or "none"
        raise KeyError(f"unknown approach {name!r}; registered: {known}") from None

    if not approach.available:
        reason = approach.unavailable_reason or "not implemented yet"
        raise NotImplementedError(f"approach {name!r} is {approach.status}: {reason}")

    assert approach.build is not None          # implied by `available`
    return approach.build(settings, options or {})


# --- the settings a live pipeline actually runs with --------------------------


def pipeline_config(settings: Settings) -> PipelineConfig:
    """The tunables for a live pipeline, taken from the config file.

    This exists because it was missing, and the omission was silent: the ``segmentation:``
    block in ``livesubs.yaml`` was validated, printed by ``--check`` and then ignored, so an
    operator tuning ``max_segment_ms`` to fix the pacing would have measured no change and
    concluded the pacing policy did not work. The pacing policy is the product, and a
    setting that appears to work while doing nothing is worse than one that is absent.
    """

    from livesubs.asr.base import AsrConfig
    from livesubs.ingest import IngestConfig
    from livesubs.pipeline import PipelineConfig
    from livesubs.segmenter import SegmenterConfig

    return PipelineConfig(
        segmenter=SegmenterConfig(
            endpoint_silence_ms=settings.segmentation.endpoint_silence_ms,
            max_segment_ms=settings.segmentation.max_segment_ms,
            min_segment_ms=settings.segmentation.min_segment_ms,
            context_segments=settings.segmentation.context_segments,
            trust_provider_endpointing=settings.segmentation.trust_provider_endpointing,
        ),
        ingest=IngestConfig(sample_rate=settings.sample_rate),
        # The source language and any vocabulary hints belong to the recogniser, so they
        # come from the provider registry rather than from here: input is English by
        # design, and the glossary that would fill ``keyterms`` is the M4 item.
        asr=AsrConfig(sample_rate=settings.sample_rate, channels=settings.channels),
    )


def capture_source(settings: Settings, options: ApproachOptions) -> AudioSource:
    """Use the same calibration policy for every approach with a real microphone."""
    from livesubs.audio.calibration import CalibrationConfig
    from livesubs.audio.factory import build_source

    return build_source(
        resolve_audio_spec(options, settings), sample_rate=settings.sample_rate,
        calibration=CalibrationConfig(
            enabled=settings.capture.auto_calibrate,
            runtime=settings.capture.runtime_calibration,
        ),
    )


def artefacts(
    settings: Settings, source: AudioSource
) -> tuple[AudioSource, Callable[[object], None] | None]:
    """Wrap a capture source with the session's recording and subtitle files.

    Shared by every approach that captures audio, so a change to how a session is archived
    cannot apply to one approach and not another. One stem for every artefact, so the
    recording and the subtitle files that describe it pair up by name.
    """

    from livesubs.audio.archive import ArchivedSource, recording_stem
    from livesubs.subtitles import SubtitleRecorder

    stem = recording_stem(settings.session_id)

    if settings.archive_audio:
        # Wrapped rather than wired into the pipeline: archiving is an optional extra and
        # must not be able to complicate the path that produces subtitles.
        source = ArchivedSource(source, directory=settings.artifacts_dir, stem=stem)

    recorder = None
    if settings.write_subtitles:
        recorder = SubtitleRecorder(
            directory=settings.artifacts_dir,
            stem=stem,
            source_code="en",
            target_code=settings.language,
        )

    return source, recorder


# --- adapters, chosen by name -------------------------------------------------


def _credentials(settings: Settings, name: str) -> str | None:
    """A provider's key, or ``None``. Never logged and never part of an error message."""

    secret = settings.provider_key(name)
    return secret.get_secret_value() if secret is not None else None


def _glossary(settings: Settings) -> tuple[str, ...]:
    """Domain terms for the frozen system prompt.

    Empty until a glossary exists in the config file, which is the M4 item in
    ``docs/10-roadmap.md``. The plumbing is here so that adding one is a settings change
    rather than an adapter change.
    """

    return ()


def build_asr(settings: Settings) -> AsrProvider:
    """The recogniser for whichever provider is active for the ASR role.

    A provider with no adapter raises rather than falling back to the stub. A silent
    substitution would let a deployment believe it was recognising speech while it was
    inventing words.
    """

    from livesubs.asr.deepgram import DEFAULT_URL, DeepgramAsr

    provider = settings.active_provider("asr")
    if provider is None:
        raise ValueError(
            "no ASR provider is usable; run `livesubs --set-key deepgram` or declare one in "
            "livesubs.yaml (see docs/11-extending.md)"
        )

    if provider.name != "deepgram":
        raise NotImplementedError(
            f"provider {provider.name!r} is active for ASR but no adapter exists for it; "
            "see docs/11-extending.md"
        )

    key = _credentials(settings, provider.name)
    if key is None:
        raise ValueError(
            f"provider {provider.name!r} is active for ASR but has no key; "
            f"run `livesubs --set-key {provider.name}`"
        )

    return DeepgramAsr(
        api_key=key,
        url=settings.provider_option(provider.name, "url") or DEFAULT_URL,
        model=settings.provider_option(provider.name, "model") or "nova-3",
        language=settings.provider_option(provider.name, "language") or "en",
        sample_rate=settings.sample_rate,
        channels=settings.channels,
        keyterms=_glossary(settings),
    )


def build_mt(settings: Settings) -> MtProvider:
    """The translator for whichever provider is active for the MT role.

    Any OpenAI-compatible endpoint is accepted, which is what lets a deployment point the
    translator at a model on its own machine. A declared provider must say so with
    ``options.adapter: openai_compat``: without it, the provider is refused rather than
    guessed at, because the cost of guessing is sending text to the wrong wire format.
    """

    from livesubs.mt.openai_compat import OpenAiCompatMt

    provider = settings.active_provider("mt")
    if provider is None:
        raise ValueError(
            "no MT provider is usable; run `livesubs --set-key deepseek` or declare one in "
            "livesubs.yaml (see docs/11-extending.md)"
        )

    kind = settings.provider_option(provider.name, "adapter")
    if provider.name != "deepseek" and kind != "openai_compat":
        raise NotImplementedError(
            f"provider {provider.name!r} is active for translation but no adapter is declared "
            "for it; set `options.adapter: openai_compat` if it speaks the OpenAI-compatible "
            "API. See docs/11-extending.md"
        )

    return OpenAiCompatMt(
        model=settings.provider_option(provider.name, "model") or "deepseek-flash",
        base_url=settings.provider_option(provider.name, "base_url"),
        api_key=_credentials(settings, provider.name),
        glossary=_glossary(settings),
        thinking=settings.provider_option(provider.name, "thinking"),
        # A hosted model answers in a second; a model on this machine's CPU can take
        # twenty before its first token, so the timeout is the deployment's to set. The
        # default is a hosted model's, because that is what the shipped providers are.
        timeout_s=float(settings.provider_option(provider.name, "timeout_s") or 20.0),
    )


# --- registrations -------------------------------------------------------------
#
# The plugin list. Adding an approach is one entry here plus its adapter; nothing
# else in the program needs to change.


def _build_sim(settings: Settings, options: ApproachOptions) -> SegmentPublisher:
    # Imported lazily so this module stays importable without pulling in every
    # approach's dependencies, and so an approach cannot create an import cycle.
    from livesubs.sim import SimPublisher

    # Looping is on for serving, and that is not a detail. The default script lasts
    # twelve seconds, so a one-shot replay has already finished by the time anyone has
    # walked to the phone and scanned the code; the viewer then sits on a page that
    # never receives a subtitle, which reads as a broken QR code. `--once` restores the
    # finite behaviour, which is what the tests and `--replay` want.
    loop = options.get("loop", "true").strip().lower() not in {"false", "0", "no"}

    return SimPublisher(
        script=options.get("script", "default"),
        speed=float(options.get("speed", "1.0")),
        loop=loop,
    )


register(
    Approach(
        name="sim",
        label="Simulated replay",
        summary=(
            "Replays a canned subtitle script with realistic timings and revisions. "
            "No credentials, no microphone, no network."
        ),
        build=_build_sim,
    )
)


def _build_live_sim(settings: Settings, options: ApproachOptions) -> SegmentPublisher:
    """The live pipeline's real structure, on an audio source of your choosing.

    Capture, conditioning, segmentation, publishing and the hub are all the real
    implementations. Only the words are stubbed, because no ASR adapter is built yet, so
    the audio path can be exercised with no credentials - and, via ``--audio``, with no
    hardware either: a synthetic source, a WAV recording, or a live device.

    It also writes the session's artefacts: the audio, and timestamped subtitle files in
    both languages whose cues refer to that audio's clock. See ``livesubs.subtitles``.

    It is a separate approach from ``live`` rather than a fallback within it, because a
    silent substitution would make a broken live pipeline look like a working one.
    """

    from livesubs.asr.stub import StubAsr
    from livesubs.mt.passthrough import PassthroughMt
    from livesubs.pipeline import LivePipeline

    capture, recorder = artefacts(settings, capture_source(settings, options))

    return LivePipeline(
        source=capture,
        asr=StubAsr(),
        mt=PassthroughMt(),
        config=pipeline_config(settings),
        on_segment=recorder,
    )


def _build_live(settings: Settings, options: ApproachOptions) -> SegmentPublisher:
    """The pipeline this repository exists for: speech to English to German to a screen.

    Nothing is stubbed. Both adapters are chosen from the provider chain the config file
    declares, so switching vendors is a config change, and an unusable chain raises here
    with instructions rather than producing invented words.
    """

    from livesubs.pipeline import LivePipeline

    capture, recorder = artefacts(settings, capture_source(settings, options))

    # The providers are constructed here so a missing key or adapter still fails at
    # startup rather than when the first viewer arrives. The audience gate means they
    # are opened (and the translator warmed) only once a viewer connects, and the
    # factory builds a fresh recogniser for each later audience session so no closed
    # adapter's send queue is replayed.
    initial_asr = build_asr(settings)
    initial_mt = build_mt(settings)

    return LivePipeline(
        source=capture,
        asr=initial_asr,
        asr_factory=lambda: build_asr(settings),
        mt=initial_mt,
        config=pipeline_config(settings),
        on_segment=recorder,
        require_audience=True,
    )


def _build_live_text(settings: Settings, options: ApproachOptions) -> SegmentPublisher:
    """The real translator, with the words supplied as a timed transcript.

    This is how the translation half of the pipeline is evaluated on its own, and how a
    deployment can watch the overlay and the German before it has an ASR key. The words come
    from ``--cues``, so they are whatever that file says: it is a rehearsal of everything
    except acoustic recognition, and it is named so that nobody forgets which half is real.
    """

    from livesubs.asr.transcript import TranscriptAsr
    from livesubs.corpus import read_cues
    from livesubs.pipeline import LivePipeline

    cues_path = options.get("cues", "").strip()
    if not cues_path:
        raise ValueError(
            "--cues PATH is required: this approach translates a timed transcript instead of "
            "recognising speech. Build one with tools/align_reference.py, or use --approach live "
            "to recognise speech."
        )

    cues = read_cues(cues_path)
    if not cues:
        raise ValueError(f"{cues_path} contains no cues")

    capture, recorder = artefacts(settings, capture_source(settings, options))

    # The translator is billed, so this approach is gated like ``live``. The
    # transcript input is rebuilt per audience session and realigned to the running
    # audio clock, so a resumed run does not replay cues from inside the pause.
    return LivePipeline(
        source=capture,
        asr=TranscriptAsr(cues),
        asr_factory=lambda: TranscriptAsr(cues),
        mt=build_mt(settings),
        config=pipeline_config(settings),
        on_segment=recorder,
        require_audience=True,
    )


register(
    Approach(
        name="live-sim",
        label="Live pipeline on a chosen audio source",
        summary=(
            "The real capture, conditioning, segmentation and delivery path, with "
            "credential-free adapters. Choose the audio with --audio: synth, a WAV "
            "recording, or a live device."
        ),
        build=_build_live_sim,
    )
)

register(
    Approach(
        name="live",
        label="Live API pipeline",
        summary=(
            "PipeWire capture to streaming speech recognition to LLM translation, fanned "
            "out to viewers. The approach this repository exists for."
        ),
        build=_build_live,
        required_roles=("asr", "mt"),
    )
)

register(
    Approach(
        name="live-text",
        label="Real translator on a timed transcript",
        summary=(
            "The real capture, segmentation, translation and delivery path, with the words "
            "read from a timed transcript instead of recognised from the audio. Use it to "
            "evaluate the German and the overlay without an ASR key; --cues PATH is required."
        ),
        build=_build_live_text,
        #: A translator and no recogniser, so demanding an ASR key for this approach would
        #: block a run that works.
        required_roles=("mt",),
    )
)
