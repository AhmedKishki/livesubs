"""Configuration: a committed YAML file, plus secrets and overrides from the env.

Four layers, highest precedence first:

1. **real environment variables** - ``export LIVESUBS_PORT=9000``, or for a
   provider key ``export DEEPGRAM_API_KEY=...``. Session-scoped, no disk write.
2. **``.env`` files** - ``./.env`` over ``~/.config/livesubs/.env``. For secrets
   and machine-local overrides; gitignored.
3. **the config file** - ``livesubs.yaml``. Committed, and describes the
   deployment: which providers, how the pipeline is wired, capture, segmentation,
   delivery.
4. built-in defaults.

The split of responsibility is deliberate: the config file carries **structure and
choices** and is safe to commit, while secrets live only in the environment or
``.env``. See ``livesubs.yaml.example`` and ``docs/12-configuration.md``.

Fields are grouped to match the file, with flat accessors kept as properties so
callers do not have to chase the nesting. No secret value is ever logged or
repr'd: API keys are ``SecretStr``.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from livesubs.keystore import (
    PROVIDERS,
    REQUIRED_ROLES,
    Provider,
    global_env_path,
    project_env_path,
    read_env_file,
    resolve_key,
)
from livesubs.languages import Mode, mode_for

__all__ = [
    "CONFIG_FILENAME",
    "CaptureSettings",
    "PipelineSettings",
    "ProviderOverride",
    "SegmentationSettings",
    "ServerSettings",
    "SessionSettings",
    "Settings",
    "checkout_root",
    "env_files",
    "find_config_file",
    "get_settings",
    "load_settings",
]

#: Name of the committed config file, looked for in the paths below.
CONFIG_FILENAME = "livesubs.yaml"


def checkout_root(start: Path | None = None) -> Path | None:
    """The source checkout this code is running from, or ``None`` when it is installed.

    Found by walking up from this file to ``pyproject.toml``. There is deliberately no
    fallback to the working directory: for an installed wheel the walk finds nothing, and
    answering "wherever the operator happened to run it" made the server serve a ``web``
    folder that belonged to some other project.
    """

    here = (start or Path(__file__)).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def find_config_file(explicit: Path | None = None, start: Path | None = None) -> Path | None:
    """Locate the config file, or ``None`` when there is not one.

    Search order: an explicit path, ``LIVESUBS_CONFIG``, the working directory,
    the repository root, then ``~/.config/livesubs/``.

    A missing file is a normal state, not an error: every setting has a working
    default, so livesubs runs with no config file at all.
    """

    if explicit is not None:
        return Path(explicit).expanduser()

    from_env = os.environ.get("LIVESUBS_CONFIG")
    if from_env:
        return Path(from_env).expanduser()

    root = checkout_root(start)
    candidates = (
        Path.cwd() / CONFIG_FILENAME,
        *((root / CONFIG_FILENAME,) if root is not None else ()),
        global_env_path().parent / CONFIG_FILENAME,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def env_files(start: Path | None = None) -> tuple[Path, Path]:
    """Env files in ascending precedence order, lowest first."""

    return (global_env_path(), project_env_path(start))


# --- config file groups --------------------------------------------------------
#
# Each group mirrors a top-level section of livesubs.yaml. Leaf fields keep the
# LIVESUBS_* validation alias they always had, so environment variables still
# override the file exactly as before.


class _Group(BaseModel):
    """Base for config-file groups.

    ``populate_by_name`` is essential here. Each leaf carries an environment
    alias, and without it a nested value would be accepted *only* under that
    alias - so ``server: {port: 9123}`` in the config file would be silently
    ignored while ``LIVESUBS_PORT`` kept working. A config file that does nothing
    and says nothing is the worst possible failure, so this is load-bearing and
    is pinned by a test.
    """

    model_config = ConfigDict(populate_by_name=True)


class ServerSettings(_Group):
    """The HTTP/WebSocket listener."""

    host: str = Field(default="0.0.0.0", validation_alias="LIVESUBS_HOST")
    port: int = Field(default=8000, validation_alias="LIVESUBS_PORT")
    #: Force the URL the QR code encodes. Unset means derive it from each request's
    #: Host header, which needs no configuration and works on any address.
    public_url: str | None = Field(default=None, validation_alias="LIVESUBS_PUBLIC_URL")
    log_level: str = Field(default="info", validation_alias="LIVESUBS_LOG_LEVEL")


class SessionSettings(_Group):
    """Session identity, who is speaking, and where the frontend is served from."""

    id: str = Field(default="demo", validation_alias="LIVESUBS_SESSION_ID")
    name: str = Field(default="Live subtitles", validation_alias="LIVESUBS_SESSION_NAME")
    #: Recognition intent, independent of the fixed three-language subtitle offer.
    speaker: str = Field(default="auto", validation_alias="LIVESUBS_SESSION_SPEAKER")
    default_language: str = Field(
        default="en", validation_alias="LIVESUBS_SESSION_DEFAULT_LANGUAGE"
    )
    #: Deprecated compatibility declaration; a nonempty value must contain all targets.
    targets: list[str] = Field(default_factory=list)
    web_dir: Path | None = Field(default=None, validation_alias="LIVESUBS_WEB_DIR")

    @field_validator("speaker")
    @classmethod
    def validate_speaker(cls, value: str) -> str:
        if value == "en+de":
            warnings.warn("session.speaker en+de is deprecated; use auto instead", stacklevel=2)
            return "auto"
        mode_for(value)
        return value

    @field_validator("default_language")
    @classmethod
    def validate_fallback(cls, value: str) -> str:
        if value not in ("en", "de", "ar"):
            raise ValueError("session.default_language must be en, de or ar")
        return value

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, value: list[str]) -> list[str]:
        if value:
            mode_for("auto", value)
            warnings.warn("session.targets is deprecated; remove session.targets", stacklevel=2)
            return ["en", "de", "ar"]
        return value


class CaptureSettings(_Group):
    """Audio capture, and the session artefacts kept alongside it."""

    mode: str = Field(default="pw_record", validation_alias="LIVESUBS_CAPTURE_MODE")
    #: The device to capture from. Deliberately empty: a device identifier is valid
    #: only on the machine that produced it, so a shipped default would be wrong
    #: everywhere else. ``livesubs --preflight`` lists the candidates here.
    target: str = Field(default="", validation_alias="LIVESUBS_CAPTURE_TARGET")
    sample_rate: int = Field(default=16_000, validation_alias="LIVESUBS_SAMPLE_RATE")
    channels: int = Field(default=1, validation_alias="LIVESUBS_CHANNELS")
    #: Verified hardware reductions, never digital scaling or silence-driven boosts.
    auto_calibrate: bool = Field(default=True, validation_alias="LIVESUBS_AUTO_CALIBRATE")
    runtime_calibration: bool = Field(
        default=True, validation_alias="LIVESUBS_RUNTIME_CALIBRATION"
    )
    #: Record the raw audio. On by decision rather than by accident: lecture audio is
    #: not considered sensitive here, and a recording can be deleted afterwards
    #: whereas a lecture can only be captured once. See docs/08-operations.md.
    archive: bool = Field(default=True, validation_alias="LIVESUBS_ARCHIVE_AUDIO")
    #: Where session artefacts go: the audio, and the subtitle files that pair with it
    #: by sharing its name. Gitignored.
    #:
    #: The environment alias stays `LIVESUBS_ARCHIVE_DIR` because this setting used to
    #: govern only the audio. Renaming the alias would break deployed `.env` files for
    #: no functional gain.
    artifacts_dir: str = Field(default="recordings", validation_alias="LIVESUBS_ARCHIVE_DIR")
    #: Write timestamped subtitle files alongside the audio, in the source language and in
    #: each target language that was read, so a recording can be overlaid on a video of the
    #: same event.
    subtitles: bool = Field(default=True, validation_alias="LIVESUBS_SUBTITLES")


class SegmentationSettings(_Group):
    """The pacing policy. See docs/03-segmentation.md for why 2000 ms."""

    endpoint_silence_ms: int = Field(default=350, validation_alias="LIVESUBS_ENDPOINT_SILENCE_MS")
    max_segment_ms: int = Field(default=2000, validation_alias="LIVESUBS_MAX_SEGMENT_MS")
    min_segment_ms: int = Field(default=400, validation_alias="LIVESUBS_MIN_SEGMENT_MS")
    context_segments: int = Field(default=2, validation_alias="LIVESUBS_CONTEXT_SEGMENTS")
    #: Prefer the ASR provider's endpointing over our own energy VAD.
    trust_provider_endpointing: bool = Field(
        default=True, validation_alias="LIVESUBS_TRUST_PROVIDER_ENDPOINTING"
    )


class PipelineSettings(_Group):
    """Which provider serves each stage, in preference order.

    The first entry with a usable key is chosen once, at startup. Nothing fails over
    while a session runs. An empty list means "any active provider for that role, in
    registry order".
    """

    asr: list[str] = Field(default_factory=list)
    mt: list[str] = Field(default_factory=list)


class ProviderOverride(_Group):
    """One entry under ``providers:`` in the config file.

    Overrides a built-in provider, or declares a new one. A new provider needs
    ``role``; the rest have defaults. Declaring one makes it visible, settable and
    configurable immediately, but it is only *usable* once an adapter exists for
    it - see docs/11-extending.md.
    """

    role: str | None = None
    env_var: str | None = None
    label: str | None = None
    hint: str | None = None
    #: "active" or "planned". A planned provider holds a key and satisfies nothing.
    status: str | None = None
    #: Set ``false`` for an endpoint that needs no credential, such as a local model.
    requires_key: bool | None = None
    options: dict[str, str] = Field(default_factory=dict)


def _group_alias_map(settings_cls: type[BaseSettings]) -> dict[str, tuple[str, str]]:
    """Map each flat env var name to its ``(group, field)`` path.

    Derived from the models rather than hand-written, so moving or adding a leaf
    field cannot leave a stale env name behind.
    """

    mapping: dict[str, tuple[str, str]] = {}

    for group_name, group_field in settings_cls.model_fields.items():
        model = group_field.annotation
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            continue
        for leaf_name, leaf in model.model_fields.items():
            alias = leaf.validation_alias
            if isinstance(alias, str):
                mapping[alias.upper()] = (group_name, leaf_name)

    return mapping


class _FlatEnvSource(PydanticBaseSettingsSource):
    """Read flat ``LIVESUBS_*`` names from the environment and ``.env`` files.

    pydantic-settings does not descend into nested models when looking up
    environment variables, so without this the whole ``LIVESUBS_*`` surface would
    be silently ignored the moment a setting moved into a config-file group: every
    exported override and every ``.env`` line quietly doing nothing.

    That failure mode was observed, not imagined, which is why this source exists
    and why a test pins the precedence.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._map = _group_alias_map(settings_cls)

    def get_field_value(
        self, field: Any, field_name: str
    ) -> tuple[Any, str, bool]:  # pragma: no cover - required by the base class
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        out: dict[str, Any] = {}

        # Layers applied lowest precedence first, so the last write wins:
        # environment > project .env > global .env.
        for layer in (
            read_env_file(global_env_path()),
            read_env_file(project_env_path()),
            dict(os.environ),
        ):
            for env_name, (group, leaf) in self._map.items():
                raw = layer.get(env_name)
                # An empty value means "unset", not "deliberately blank", so that
                # a commented-out line in .env does not override the config file.
                if raw:
                    out.setdefault(group, {})[leaf] = raw

        return out


class Settings(BaseSettings):
    """Runtime configuration for the whole pipeline."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Lets Settings be constructed with field names as well as env aliases,
        # which keeps tests and ad-hoc construction readable.
        populate_by_name=True,
    )

    #: Cache for the merged provider registry. A private attribute rather than a
    #: cached_property so that several Settings instances coexist cleanly.
    _registry_cache: dict[str, Provider] | None = PrivateAttr(default=None)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Source precedence, highest first.

        Explicit arguments, then ``LIVESUBS_*`` from the environment and ``.env``,
        then the config file, then defaults.

        Keeping the YAML source last matters: a secret never needs to live in a
        committed file, because exporting it overrides whatever the file says. The
        stock ``env_settings``/``dotenv_settings`` are kept after the flat source
        so that any future non-nested field still works the ordinary way.
        """

        return (
            init_settings,
            _FlatEnvSource(settings_cls),
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    # --- config file sections ----------------------------------------------
    # Grouped to match livesubs.yaml. Each has flat accessors below so callers
    # can keep writing settings.port rather than settings.server.port.
    server: ServerSettings = Field(default_factory=ServerSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    segmentation: SegmentationSettings = Field(default_factory=SegmentationSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    #: Per-provider overrides and new provider declarations, from the config file.
    providers: dict[str, ProviderOverride] = Field(default_factory=dict)

    # --- flat accessors ----------------------------------------------------
    # Kept so that moving a setting into a config-file group does not ripple
    # through every reader.

    @property
    def capture_mode(self) -> str:
        return self.capture.mode

    @property
    def capture_target(self) -> str:
        return self.capture.target

    @property
    def sample_rate(self) -> int:
        return self.capture.sample_rate

    @property
    def channels(self) -> int:
        return self.capture.channels

    @property
    def archive_audio(self) -> bool:
        return self.capture.archive

    @property
    def artifacts_dir(self) -> str:
        """Where session artefacts go: the audio and its subtitle files together."""

        return self.capture.artifacts_dir

    @property
    def write_subtitles(self) -> bool:
        return self.capture.subtitles

    @property
    def endpoint_silence_ms(self) -> int:
        return self.segmentation.endpoint_silence_ms

    @property
    def max_segment_ms(self) -> int:
        return self.segmentation.max_segment_ms

    @property
    def min_segment_ms(self) -> int:
        return self.segmentation.min_segment_ms

    @property
    def context_segments(self) -> int:
        return self.segmentation.context_segments

    @property
    def host(self) -> str:
        return self.server.host

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def public_url(self) -> str | None:
        return self.server.public_url

    @property
    def log_level(self) -> str:
        return self.server.log_level

    @property
    def session_id(self) -> str:
        return self.session.id

    @property
    def session_name(self) -> str:
        return self.session.name

    @property
    def mode(self) -> Mode:
        """Who speaks and which languages listeners can read.

        Raises ``ValueError`` for a speaker language no mode exists for, or a target the
        mode does not offer.
        """

        return mode_for(self.session.speaker, self.session.targets)

    @property
    def source_language(self) -> str:
        return self.mode.source

    @property
    def target_languages(self) -> tuple[str, ...]:
        return self.mode.targets

    @property
    def default_language(self) -> str:
        """The language a plain QR scan reaches."""

        return self.session.default_language

    @property
    def web_dir(self) -> Path | None:
        return self.session.web_dir

    # --- Providers ---------------------------------------------------------
    # Deliberately NOT one field per provider. Everything provider-specific -
    # credential, role, tuning defaults, realtime invariants - lives in the
    # registry in ``livesubs.keystore``, which the config file may override or
    # extend under ``providers:``. Adding a provider is therefore one registry
    # entry, or one config-file entry, and no edit to this class.
    #
    # Tuning is overridden per deployment with
    # ``LIVESUBS_PROVIDER_<NAME>_<KEY>``, e.g.
    # ``LIVESUBS_PROVIDER_DEEPGRAM_MODEL=nova-2``.

    @property
    def registry(self) -> dict[str, Provider]:
        """The providers for this deployment: built-ins merged with the config file.

        Computed per instance rather than mutating the module-level registry, so
        that several Settings in one process - which tests create constantly -
        cannot interfere with each other.
        """

        if self._registry_cache is None:
            self._registry_cache = self._build_registry()
        return self._registry_cache

    def _build_registry(self) -> dict[str, Provider]:
        merged = dict(PROVIDERS)

        for name, override in self.providers.items():
            base = merged.get(name)

            if base is None:
                if override.role is None:
                    raise ValueError(
                        f"provider {name!r} is declared in the config file but has no "
                        "'role', and it is not built in, so it cannot be registered"
                    )
                merged[name] = Provider(
                    name=name,
                    env_var=override.env_var or f"{name.upper()}_API_KEY",
                    label=override.label or name,
                    role=override.role,
                    hint=override.hint or "",
                    status=override.status or "active",
                    requires_key=(
                        True if override.requires_key is None else override.requires_key
                    ),
                    options=dict(override.options),
                )
                continue

            merged[name] = replace(
                base,
                label=override.label or base.label,
                status=override.status or base.status,
                requires_key=(
                    base.requires_key if override.requires_key is None else override.requires_key
                ),
                # Config-file options layer over the registry defaults.
                options={**base.options, **override.options},
            )

        return merged

    def validate_registry(self) -> None:
        """Build the merged provider registry, raising on a bad ``providers:`` entry.

        Called during load so a configuration error surfaces immediately, rather
        than the first time some unrelated code path happens to touch the registry.
        """

        _ = self.registry

    def providers_for(self, role: str, *, active_only: bool = True) -> list[Provider]:
        """Providers for a role, in the order the config file prefers.

        ``pipeline.asr``/``pipeline.mt`` is a preference *and* a failover order:
        listed providers come first, in the listed order, and anything else that
        is registered for the role follows so that an unlisted provider still works.
        """

        candidates = [
            provider
            for provider in self.registry.values()
            if provider.role == role and (not active_only or not provider.planned)
        ]

        preferred = getattr(self.pipeline, role, []) or []
        by_name = {provider.name: provider for provider in candidates}

        ordered = [by_name[name] for name in preferred if name in by_name]
        ordered.extend(provider for provider in candidates if provider.name not in preferred)
        return ordered

    def usable(self, provider: Provider) -> bool:
        """Whether a provider can be used right now.

        A provider that needs no key is usable by definition, which is what lets a local
        model sit in the same selection chain as a vendor without a placeholder credential.
        """

        return (not provider.requires_key) or bool(self.provider_key(provider.name))

    def active_provider(self, role: str) -> Provider | None:
        """The provider that will actually serve a role: first in the chain that is usable."""

        for provider in self.providers_for(role):
            if self.usable(provider):
                return provider
        return None

    def for_session(
        self,
        *,
        session_id: str | None = None,
        session_name: str | None = None,
        speaker: str | None = None,
        default_language: str | None = None,
    ) -> Settings:
        """A copy of these settings with the session overridden.

        Exists because ``--session-id`` used to reach the session registry but not the
        settings, so two things followed the config file instead of the flag: the artefact
        names (a run with ``--session-id evalslice`` wrote ``sample-<time>.srt``), and the
        status endpoints (``/healthz`` reported ``sample`` while ``/api/session`` answered
        ``unknown session: sample``). Overriding once, before anything is built from the
        settings, is what makes the flag mean one thing.

        Recognition and fallback overrides do not change subtitle availability.
        """

        if all(value is None for value in (session_id, session_name, speaker, default_language)):
            return self

        update: dict[str, object] = {}
        if session_id is not None:
            update["id"] = session_id
        if session_name is not None:
            update["name"] = session_name
        if speaker is not None and speaker != self.session.speaker:
            update["speaker"] = speaker
        if default_language is not None:
            update["default_language"] = default_language

        session = SessionSettings.model_validate({**self.session.model_dump(), **update})
        return self.model_copy(update={"session": session})

    def validate_for_startup(
        self,
        *,
        require_credentials: bool = True,
        roles: Sequence[str] | None = None,
    ) -> list[str]:
        """Configuration problems that must stop the service.

        Provider-agnostic on purpose: it asks the registry which providers are
        active for each required role, rather than naming any provider. A new
        provider - built in or declared in the config file - is picked up here
        automatically.

        A missing key is a configuration error and should stop everything; a
        *flaky* provider is an operational reality that must not. Network probes
        are therefore deliberately not performed here.

        ``require_credentials=False`` is for approaches that do not call a vendor,
        so that a fresh checkout running the replay or the synthetic-audio pipeline
        is not told its environment is unusable for want of an API key. ``roles``
        narrows that further, for an approach that needs *some* providers but not all:
        ``live-text`` translates a transcript, so demanding an ASR key for it would
        block a run that works. Either way a typo in ``pipeline.<role>`` is reported,
        because that is an operator error rather than a missing purchase.
        """

        problems: list[str] = []

        for role in REQUIRED_ROLES:
            chain = self.providers_for(role)
            needed = require_credentials and (roles is None or role in roles)

            if needed:
                if not chain:
                    problems.append(
                        f"no active provider is registered for the {role.upper()} stage"
                    )
                    continue

                if not any(self.usable(provider) for provider in chain):
                    how = " or ".join(
                        f"livesubs --set-key {p.name}"
                        for p in chain
                        if p.requires_key
                    )
                    detail = f": {how}" if how else ""
                    problems.append(
                        f"no {role.upper()} provider is usable: none of "
                        f"{', '.join(p.name for p in chain)} is configured{detail}"
                    )

            for name in getattr(self.pipeline, role, []) or []:
                if name not in self.registry:
                    known = ", ".join(sorted(self.registry)) or "none"
                    problems.append(
                        f"pipeline.{role} names unknown provider {name!r}; known: {known}"
                    )

        try:
            _ = self.mode
        except ValueError as exc:
            problems.append(f"session: {exc}")

        if self.segmentation.min_segment_ms >= self.segmentation.max_segment_ms:
            problems.append("segmentation.min_segment_ms must be below segmentation.max_segment_ms")

        if self.capture.channels != 1:
            problems.append("capture.channels must be 1: the ASR providers expect mono")

        if self.capture.sample_rate != 16_000:
            problems.append("capture.sample_rate must be 16000: the ASR providers expect 16 kHz")

        # Provider-declared invariants, expressed as data in the registry, so
        # validation never has to know a provider by name.
        for provider in self.all_active_providers():
            for option, required, why in provider.invariants:
                actual = self.provider_option(provider.name, option)
                if actual is not None and actual.lower() != required:
                    problems.append(
                        f"{provider.option_env_var(option)} must be {required!r}: {why}"
                    )

        return problems

    def all_active_providers(self) -> list[Provider]:
        """Every active provider across the required roles, without duplicates."""

        found: dict[str, Provider] = {}
        for role in REQUIRED_ROLES:
            for provider in self.providers_for(role):
                found.setdefault(provider.name, provider)
        return list(found.values())

    # --- provider access, driven entirely by the effective registry --------

    def provider_key(self, name: str) -> SecretStr | None:
        """Resolve one provider's credential, honouring the documented precedence."""

        value, _source = resolve_key(self.registry[name])
        return SecretStr(value) if value else None

    def provider_source(self, name: str) -> str:
        """Where a provider's key came from: ``environment``, a file, or ``unset``."""

        return resolve_key(self.registry[name])[1]

    def provider_option(self, name: str, key: str) -> str | None:
        """A provider's tuning value.

        Precedence: an environment override, then the value from the config file,
        then the registry default. Config-file options are already merged into the
        effective provider by :meth:`_build_registry`, so only the env override
        needs handling here.
        """

        provider = self.registry[name]
        env_name = provider.option_env_var(key)

        for candidate in (
            os.environ.get(env_name),
            read_env_file(project_env_path()).get(env_name),
            read_env_file(global_env_path()).get(env_name),
        ):
            if candidate:
                return candidate

        return provider.options.get(key)

    def providers_with_keys(self) -> dict[str, str]:
        """``{provider_name: source}`` for every provider that has a credential."""

        return {
            name: self.provider_source(name)
            for name in self.registry
            if self.provider_source(name) != "unset"
        }

    def qr_base_url(self) -> str:
        """The URL viewers are sent to by the QR code.

        Prefers an explicitly configured public URL; otherwise falls back to
        localhost. Request-derived resolution lives in
        ``livesubs.server.routes.viewer_url``. Note this is deliberately plain
        HTTP: a certificate warning would destroy the frictionless "just scan it"
        premise. See ``docs/07-delivery-qr.md``.
        """

        if self.server.public_url:
            return self.server.public_url.rstrip("/")
        return f"http://localhost:{self.server.port}"


def load_settings(config_path: Path | None = None) -> Settings:
    """Build Settings, reading the config file, ``.env`` files and the environment.

    The config path is injected by subclassing, because pydantic-settings takes it
    from ``model_config``. Precedence is set explicitly in
    ``Settings.settings_customise_sources``: environment, then ``.env``, then the
    config file, then defaults.
    """

    resolved = find_config_file(config_path)

    if resolved is None:
        loaded = Settings(_env_file=env_files())          # type: ignore[arg-type]
        loaded.validate_registry()
        return loaded

    if not resolved.is_file():
        raise FileNotFoundError(f"config file not found: {resolved}")

    configured = type(
        "ConfiguredSettings",
        (Settings,),
        {
            "model_config": SettingsConfigDict(
                **{**Settings.model_config, "yaml_file": str(resolved)}
            )
        },
    )
    loaded = configured(_env_file=env_files())             # type: ignore[arg-type]
    # Fail here on a malformed `providers:` section rather than later.
    loaded.validate_registry()
    return loaded


_settings_cache: dict[str, Settings] = {}


def get_settings(config_path: Path | None = None) -> Settings:
    """Cached accessor, keyed by config path.

    Cached rather than a single global so that a caller asking for a specific
    config file gets that one, instead of whatever was loaded first.
    """

    key = str(config_path) if config_path else ""
    if key not in _settings_cache:
        _settings_cache[key] = load_settings(config_path)
    return _settings_cache[key]
