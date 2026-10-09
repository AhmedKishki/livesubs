"""Microphone selection and per-user credentials for installed live startup."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TextIO

from livesubs import keystore
from livesubs.audio import pw_record
from livesubs.config import Settings

__all__ = ["prepare_live_start"]

# Custom MT endpoints must not be probed with another vendor's URL.
_PROBED_PROVIDERS = frozenset({"deepgram", "deepseek"})

_AUTH_REJECTION = re.compile(r"returned (401|403)\b")


def prepare_live_start(
    settings: Settings,
    *,
    preferred_target: str | None = None,
    config_path: Path | None = None,
    interactive: bool | None = None,
) -> Settings:
    """Choose a microphone and ensure the live providers are usable.

    Returns a settings clone whose ``capture.target`` is the chosen source. Any
    credential written during the run triggers a fresh
    :func:`livesubs.config.load_settings` so the returned settings carry the new
    value; the chosen target is reapplied afterwards. The CLI applies its own
    host/session overrides to the returned settings.
    """

    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stderr.isatty()

    target = _choose_target(
        settings, preferred_target, interactive=interactive, output_stream=sys.stderr
    )

    for role in ("asr", "mt"):
        _candidate(settings, role)

    ensured: dict[str, tuple[str, str | None]] = {}
    wrote = False
    for role in ("asr", "mt"):
        name, role_wrote, verified_key = _ensure_role(
            settings, role, interactive=interactive, config_path=config_path
        )
        ensured[role] = (name, verified_key)
        wrote = wrote or role_wrote

    current = settings
    if wrote:
        current = _reload_settings(config_path)
        _confirm_providers_unchanged(current, ensured)

    return _with_target(current, target)


# --- source selection ----------------------------------------------------------


def _enumerate_sources() -> list[tuple[str, str]]:
    """Enumerate PipeWire sources, dropping entries with an empty name.

    Enumeration trouble is the operator's to fix and is reported as a plain
    message; the live path never substitutes synthetic audio for a missing device.
    """

    try:
        raw = pw_record.pipewire_sources()
    except Exception as exc:  # noqa: BLE001 -- any discovery failure is fatal, not a fallback
        raise ValueError(f"Cannot list PipeWire microphones: {exc}") from None
    return [(name, description) for name, description in raw if name]


def _choose_target(
    settings: Settings,
    preferred_target: str | None,
    *,
    interactive: bool,
    output_stream: TextIO,
) -> str:
    """Return the chosen source, or raise with an operator-facing message."""

    sources = _enumerate_sources()
    if not sources:
        raise ValueError(
            "No PipeWire microphone source found. Check that PipeWire is running."
        )

    names = [name for name, _ in sources]
    name_set = set(names)
    explicit = (preferred_target or "").strip() or None
    configured = settings.capture_target.strip() or None

    if interactive:
        # A preference never bypasses confirmation. Missing preferences warn and
        # leave the operator to choose from the actual source list.
        if explicit is not None and explicit not in name_set:
            print(
                f"warning: microphone preference {explicit!r} is not available; "
                "choose a microphone.",
                file=output_stream,
            )
            preselect = None
        elif explicit is not None:
            preselect = explicit
        elif configured is not None and configured in name_set:
            preselect = configured
        else:
            preselect = None
            if configured is not None:
                print(
                    f"warning: configured capture target {configured!r} is not available; "
                    "choose a microphone.",
                    file=output_stream,
                )
        return _select_microphone(sources, preselect)

    _print_sources(sources, output_stream=output_stream)

    if explicit is not None:
        if explicit not in name_set:
            raise ValueError(
                f"no microphone named {explicit!r} is available; choose one of: "
                f"{', '.join(names)}"
            )
        return explicit

    if configured is not None and configured in name_set:
        return configured

    if configured is not None:
        raise ValueError(
            f"the configured capture target {configured!r} is not available; "
            f"choose one of: {', '.join(names)}"
        )

    raise ValueError(
        "no microphone selected in a non-interactive run; pass a target, set "
        "capture.target, or run in a terminal. Available: " + ", ".join(names)
    )


def _print_sources(sources: list[tuple[str, str]], *, output_stream: TextIO) -> None:
    print("Available PipeWire microphone sources:", file=output_stream)
    for name, description in sources:
        name = "".join(char if char.isprintable() else "?" for char in name)
        description = "".join(char if char.isprintable() else "?" for char in description)
        detail = f"  ({description})" if description else ""
        print(f"  {name}{detail}", file=output_stream)


def _select_microphone(
    sources: list[tuple[str, str]], preferred: str | None
) -> str:
    """Thin wrapper so the picker can be imported lazily and mocked in tests."""

    from livesubs.audio.selection import select_microphone

    return select_microphone(sources, preferred=preferred)


# --- provider bootstrap --------------------------------------------------------


def _ensure_role(
    settings: Settings,
    role: str,
    *,
    interactive: bool,
    config_path: Path | None,
) -> tuple[str, bool, str | None]:
    """Return the provider, whether a key was written, and its verified value."""

    provider = _candidate(settings, role)
    active = settings.active_provider(role)

    if not provider.requires_key:
        return provider.name, False, None

    if active is not None:
        secret = settings.provider_key(provider.name)
        if secret is None:
            raise ValueError(f"no key is available for {provider.name}")
        written_key = _verify_saved(settings, provider, interactive=interactive)
        return provider.name, written_key is not None, written_key or secret.get_secret_value()

    if not interactive:
        raise ValueError(
            f"no {role.upper()} provider is usable: {provider.name} has no key. "
            f"Run in a terminal to enter it, or set {provider.env_var}. "
            "Nothing was read from stdin."
        )

    entered = _prompt_key(provider)
    _verify_and_store_new(provider, entered)
    return provider.name, True, entered


def _candidate(settings: Settings, role: str) -> keystore.Provider:
    """Reject missing or unsupported adapters before prompting for any key."""

    chain = settings.providers_for(role)
    if not chain:
        raise ValueError(
            f"no active provider is registered for the {role.upper()} stage; add one to "
            "livesubs.yaml (see docs/11-extending.md)"
        )

    active = settings.active_provider(role)
    provider = active if active is not None else chain[0]

    # Refuse an unsupported adapter before prompting for anything or touching the
    # network, so a mis-selection is not discovered only after a key was entered.
    if not _supports_adapter(settings, provider, role):
        raise ValueError(_unsupported_message(provider, role))

    return provider


def _supports_adapter(settings: Settings, provider: keystore.Provider, role: str) -> bool:
    if role == "asr":
        # Only Deepgram has an ASR adapter today.
        return provider.name == "deepgram"

    kind = settings.provider_option(provider.name, "adapter")
    return provider.name == "deepseek" or (kind or "").strip() == "openai_compat"


def _unsupported_message(provider: keystore.Provider, role: str) -> str:
    if role == "asr":
        return (
            f"provider {provider.name!r} is active for ASR but no adapter exists for it; "
            "only Deepgram is implemented (see docs/11-extending.md)"
        )
    return (
        f"provider {provider.name!r} is active for translation but no adapter is declared "
        "for it; set `options.adapter: openai_compat` if it speaks the OpenAI-compatible "
        "API (see docs/11-extending.md)"
    )


def _verify_saved(
    settings: Settings,
    provider: keystore.Provider,
    *,
    interactive: bool,
) -> str | None:
    """Return a replacement key if the saved value was rejected and replaced."""

    if provider.name not in _PROBED_PROVIDERS:
        # Unknown custom endpoint: store/require the key, but let the adapter validate
        # it at open rather than probing the wrong vendor URL.
        return None

    secret = settings.provider_key(provider.name)
    if secret is None:
        return None

    ok, detail = _verify_key(provider, secret.get_secret_value())
    if ok:
        return None

    if not _AUTH_REJECTION.search(detail):
        # Network or provider trouble: abort without prompting for a new key.
        raise ValueError(
            f"could not verify the {provider.name} key: {detail}. Aborting without "
            "changing anything."
        )

    source = settings.provider_source(provider.name)
    if source == "environment":
        # An exported variable outranks any file, so writing cannot fix it.
        raise ValueError(
            f"{provider.name} rejected the exported {provider.env_var}. Replace or unset "
            "that environment variable and rerun."
        )

    if not interactive:
        raise ValueError(
            f"{provider.name} rejected its saved API key. Run in a terminal to replace it."
        )

    replacement = _prompt_key(provider)
    ok, detail = _verify_key(provider, replacement)
    if not ok:
        raise ValueError(
            f"{provider.name} rejected the replacement key: {detail}. Nothing was saved."
        )

    # Replace the file the rejected value actually came from: the project .env when
    # that is the resolved source, otherwise the machine-wide file.
    path = keystore.project_env_path() if source == ".env" else keystore.global_env_path()
    keystore.upsert_env_var(path, provider.env_var, replacement)
    return replacement


def _verify_and_store_new(provider: keystore.Provider, entered: str) -> None:
    """Verify a freshly entered key (when probeable) and store it globally."""

    if provider.name in _PROBED_PROVIDERS:
        ok, detail = _verify_key(provider, entered)
        if not ok:
            raise ValueError(
                f"{provider.name} did not accept the key: {detail}. Nothing was saved; "
                "run again to try a different key."
            )

    keystore.upsert_env_var(keystore.global_env_path(), provider.env_var, entered)


def _prompt_key(provider: keystore.Provider) -> str:
    """Read a hidden key, rejecting empty or whitespace-bearing input."""

    print(f"{provider.label}\nObtain a key: {provider.hint}", file=sys.stderr)
    entered = _read_secret(f"Paste the {provider.name} API key (input hidden): ")
    if not entered or any(
        character.isspace() or not character.isprintable() for character in entered
    ):
        raise ValueError("empty or malformed key; nothing was written")
    return entered


def _read_secret(prompt: str) -> str:
    """Hidden input wrapper, kept separate so tests never touch a real terminal."""

    import getpass

    return getpass.getpass(prompt)


def _verify_key(provider: keystore.Provider, key: str) -> tuple[bool, str]:
    """Wrapper around the existing probe helper only; no new transport here."""

    from livesubs.keycheck import verify_key

    return verify_key(provider, key)


def _reload_settings(config_path: Path | None) -> Settings:
    """Reload from disk so a written key is reflected, never the cached accessor."""

    from livesubs.config import load_settings

    return load_settings(config_path)


# --- result assembly -----------------------------------------------------------


def _confirm_providers_unchanged(
    settings: Settings, ensured: dict[str, tuple[str, str | None]]
) -> None:
    """Fail loudly if a write did not actually settle the selected provider.

    A higher-precedence override (for example a project ``.env``) could still win
    after a global write. This is a bounded single check, not a retry loop: if the
    selected provider is no longer active, the operator has a file conflict to
    resolve rather than another prompt to answer.
    """

    for role, (name, verified_key) in ensured.items():
        active = settings.active_provider(role)
        secret = settings.provider_key(name) if active is not None and active.name == name else None
        if (
            active is None
            or active.name != name
            or (
                verified_key is not None
                and (secret is None or secret.get_secret_value() != verified_key)
            )
        ):
            raise ValueError(
                f"after saving the {role.upper()} key, {name!r} is still not the active "
                "provider; a higher-precedence override is likely interfering. Check the "
                "environment and .env files."
            )


def _with_target(settings: Settings, target: str) -> Settings:
    """Return a clone carrying the chosen capture target, preserving everything else."""

    return settings.model_copy(
        update={"capture": settings.capture.model_copy(update={"target": target})}
    )
