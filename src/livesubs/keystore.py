"""API key management: set from the terminal, use for a session, optionally save.

Motivated by the ergonomics of the deployment box: keys should not require hand
editing a file, but they should also not be forced to persist.

Precedence, highest first (mirroring ``pydantic-settings`` and standard Unix
convention):

1. a real environment variable, e.g. ``export DEEPGRAM_API_KEY=...`` - this is
   what makes a key usable "for this session" with no disk write at all;
2. the project ``.env`` next to ``pyproject.toml``;
3. the machine-wide ``~/.config/livesubs/.env``.

Files written here are made owner-only (``0600``) because they contain secrets.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = [
    "PROVIDERS",
    "KeyStatus",
    "Provider",
    "active_providers",
    "global_env_path",
    "insecure_files",
    "key_statuses",
    "mask",
    "planned_providers",
    "project_env_path",
    "read_env_file",
    "remove_env_var",
    "resolve_key",
    "upsert_env_var",
]

_KeyRole = Literal["asr", "mt"]

#: Whether a provider is wired into the design or is a documented future option.
#:
#: * ``active``   - part of the design; keys for it are used and are required.
#: * ``planned``  - known, documented, and its key may be stored, but **no
#:   adapter exists yet**, so it must never satisfy a startup requirement.
#:
#: The distinction exists so that a provider can be listed for future work
#: without ``--keys`` implying it works, and without startup validation passing
#: with no functioning stage behind it.
_ProviderStatus = Literal["active", "planned"]


@dataclass(frozen=True, slots=True)
class Provider:
    """Everything known about one provider, in one place.

    This is the single registry entry for a provider: its credential, its role, its
    tuning, and any value that must hold for realtime use. Adding a provider here
    makes it settable via ``--set-key``, listable via ``--keys``, selectable by an
    adapter, and counted by startup validation - with no changes to ``config.py``
    or to any other provider's code.
    """

    name: str
    env_var: str
    label: str
    role: _KeyRole
    hint: str
    status: _ProviderStatus = "active"
    #: Whether this provider needs a credential at all. ``False`` for an endpoint that is
    #: already running on this machine - a local model server, for instance - which
    #: genuinely has no key to collect, so asking for a placeholder one would be a lie the
    #: validation code then has to be told about.
    requires_key: bool = True
    #: Provider-specific tuning, e.g. ``{"model": "nova-3"}``. Overridable per
    #: deployment through ``LIVESUBS_PROVIDER_<NAME>_<KEY>``, so a deployment can
    #: change a model without a code change.
    options: dict[str, str] = field(default_factory=dict)
    #: Values that must hold for realtime use, expressed as data so that
    #: validation never needs to know a provider by name.
    #: Each entry is ``(option, required_value, why_it_matters)``.
    invariants: tuple[tuple[str, str, str], ...] = ()

    @property
    def planned(self) -> bool:
        return self.status == "planned"

    def option_env_var(self, key: str) -> str:
        """The environment variable that overrides one of this provider's options."""

        return f"LIVESUBS_PROVIDER_{self.name.upper()}_{key.upper()}"


#: Registry of provider credentials. Adding an entry here is all that is needed
#: for the ``--set-key`` workflow to accept it.
#:
#: An entry does not imply a working adapter: ``status="planned"`` marks a
#: documented future option whose key can be stored and verified, but which is
#: never counted as satisfying a requirement. Only ``status="active"`` providers
#: are wired into the pipeline.
PROVIDERS: dict[str, Provider] = {
    "deepgram": Provider(
        name="deepgram",
        env_var="DEEPGRAM_API_KEY",
        label="Deepgram (streaming speech recognition)",
        role="asr",
        hint="console.deepgram.com -> API Keys",
        options={
            "url": "wss://api.deepgram.com",
            "model": "nova-3",
            # No `language` here: the recogniser is told the speaker's language from the
            # session's mode. Set `language` under `options` to override it deliberately.
        },
    ),
    "openai": Provider(
        name="openai",
        env_var="OPENAI_API_KEY",
        label="OpenAI (streaming ASR)",
        role="asr",
        hint="platform.openai.com -> API keys",
        # No adapter exists. Registered as active it satisfied the recognition requirement
        # in `--check` and failed only when the session started.
        status="planned",
        options={
            "model": "gpt-live-transcribe",
            # Tunable latency/accuracy trade-off: minimal|low|medium|high|xhigh.
            "delay": "low",
        },
    ),
    "deepseek": Provider(
        name="deepseek",
        env_var="DEEPSEEK_API_KEY",
        label="DeepSeek (translation)",
        role="mt",
        hint="platform.deepseek.com -> API keys",
        options={
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-flash",
            "thinking": "disabled",
        },
        invariants=(
            (
                "thinking",
                "disabled",
                (
                    "reasoning adds seconds of latency per segment, which the "
                    "subtitle latency budget cannot absorb"
                ),
            ),
        ),
    ),
    "deepl": Provider(
        name="deepl",
        env_var="DEEPL_API_KEY",
        label="DeepL (translation, EU-hosted)",
        role="mt",
        hint="deepl.com -> Your account -> Authentication Key",
        status="planned",
    ),
}

#: Providers a fresh setup actually needs, in the order they should be set.
REQUIRED_ROLES: tuple[_KeyRole, ...] = ("asr", "mt")


def active_providers(role: _KeyRole | None = None) -> list[Provider]:
    """Providers wired into the pipeline, optionally filtered by role.

    These are the only ones that can satisfy a startup requirement.
    """

    return [
        provider
        for provider in PROVIDERS.values()
        if not provider.planned and (role is None or provider.role == role)
    ]


def planned_providers(role: _KeyRole | None = None) -> list[Provider]:
    """Documented future options with no adapter yet."""

    return [
        provider
        for provider in PROVIDERS.values()
        if provider.planned and (role is None or provider.role == role)
    ]

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def project_env_path(start: Path | None = None) -> Path:
    """The project ``.env``: beside this project's ``pyproject.toml``, else the working directory.

    Only a ``pyproject.toml`` that declares this project counts. Walking up to the first
    one found would pick up whatever other Python project the operator happened to be
    inside, and read - or write, with ``--set-key`` - that project's ``.env``.
    """

    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if _declares_livesubs(candidate / "pyproject.toml"):
            return candidate / ".env"
    return here / ".env"


def _declares_livesubs(pyproject: Path) -> bool:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("project", {}).get("name") == "livesubs"


def global_env_path() -> Path:
    """The machine-wide env file, for keys shared across checkouts."""

    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "livesubs" / ".env"


def insecure_files(paths: list[Path] | None = None) -> list[Path]:
    """Existing credential files that other users can read or change.

    Files written here are owner-only, but a file made by hand or by an editor takes the
    umask, which is usually group-readable. Nothing else checks, and a key in such a file is
    exactly as exposed as the file.
    """

    candidates = paths if paths is not None else [project_env_path(), global_env_path()]
    found = []
    for path in candidates:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        if mode & 0o077 and path.is_file():
            found.append(path)
    return found


def mask(value: str) -> str:
    """Render a secret for display. Never returns more than the last four."""

    if not value:
        return "(empty)"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{'*' * 8}...{value[-4:]}  ({len(value)} chars)"


def _unquote(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    # Strip a trailing inline comment only when the value is unquoted.
    return value.split(" #", 1)[0].strip()


def read_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` pairs from an env file. Missing files read as empty."""

    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if match:
            values[match.group(1)] = _unquote(match.group(2))
    return values


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` atomically, owner-only, without a partial-write window.

    Atomic because a half-written env file would be a broken credential store,
    and this runs immediately before a lecture.
    """

    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def upsert_env_var(path: Path, key: str, value: str) -> Path:
    """Set ``key`` in ``path``, preserving comments, ordering and blank lines.

    An existing assignment is edited in place, including a commented-out
    ``# KEY=`` placeholder, so a file seeded from ``.env.example`` stays tidy.
    Otherwise the assignment is appended.

    Returns the path written.
    """

    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    assignment = f"{key}={value}"

    for index, line in enumerate(lines):
        match = _LINE_RE.match(line)
        if match and match.group(1) == key:
            lines[index] = assignment
            break
        # Reuse a commented placeholder rather than leaving a stale one behind.
        if line.lstrip().startswith("#") and _LINE_RE.match(line.lstrip().lstrip("#").strip()):
            inner = _LINE_RE.match(line.lstrip().lstrip("#").strip())
            if inner and inner.group(1) == key:
                lines[index] = assignment
                break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(assignment)

    _write_atomic(path, "\n".join(lines) + "\n")
    return path


def remove_env_var(path: Path, key: str) -> bool:
    """Remove ``key`` from ``path``. Returns whether anything was removed."""

    if not path.is_file():
        return False

    lines = path.read_text(encoding="utf-8").splitlines()

    def assigned_key(line: str) -> str | None:
        match = _LINE_RE.match(line)
        return match.group(1) if match else None

    kept = [line for line in lines if assigned_key(line) != key]
    if len(kept) == len(lines):
        return False

    remaining = "\n".join(kept).strip()
    _write_atomic(path, f"{remaining}\n" if remaining else "")
    return True


def resolve_key(provider: Provider, env_path: Path | None = None) -> tuple[str | None, str]:
    """Find a credential and report where it came from.

    Returns ``(value, source)`` where source is one of ``"environment"``,
    ``".env"``, ``"~/.config/livesubs/.env"`` or ``"unset"``.
    """

    from_environment = os.environ.get(provider.env_var)
    if from_environment:
        return from_environment, "environment"

    project = read_env_file(env_path or project_env_path())
    if project.get(provider.env_var):
        return project[provider.env_var], ".env"

    shared = read_env_file(global_env_path())
    if shared.get(provider.env_var):
        return shared[provider.env_var], "~/.config/livesubs/.env"

    return None, "unset"


@dataclass(slots=True)
class KeyStatus:
    """One row of the ``--keys`` report."""

    provider: Provider
    value: str | None
    source: str

    @property
    def set(self) -> bool:
        return bool(self.value)

    def render(self) -> str:
        if self.set:
            state = f"[set] {self.provider.name:<9} {mask(self.value or '')}  via {self.source}"
        else:
            state = f"[   ] {self.provider.name:<9} not set"

        if self.provider.planned:
            state += "  (planned: no adapter yet, unused)"
        return "  " + state


def key_statuses(env_path: Path | None = None) -> list[KeyStatus]:
    """Status for every registered provider, in registry order."""

    statuses: list[KeyStatus] = []
    for provider in PROVIDERS.values():
        value, source = resolve_key(provider, env_path)
        statuses.append(KeyStatus(provider=provider, value=value, source=source))
    return statuses
