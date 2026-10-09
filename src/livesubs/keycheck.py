"""Credential probes that report status without reflecting secrets or headers."""

from __future__ import annotations

import httpx

from livesubs.keystore import Provider

__all__ = ["verify_key"]

_TIMEOUT = 10.0


def _deepgram(key: str) -> tuple[bool, str]:
    response = httpx.get(
        "https://api.deepgram.com/v1/projects",
        headers={"Authorization": f"Token {key}"},
        timeout=_TIMEOUT,
    )
    if response.status_code == 200:
        return True, "accepted by Deepgram"
    return False, f"Deepgram returned {response.status_code}"


def _openai_compatible(base_url: str, key: str, label: str) -> tuple[bool, str]:
    response = httpx.get(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=_TIMEOUT,
    )
    if response.status_code == 200:
        return True, f"accepted by {label}"
    return False, f"{label} returned {response.status_code}"


def _deepl(key: str) -> tuple[bool, str]:
    """DeepL keys are valid on either the free or the pro host, so try both.

    DeepL is a planned provider: no adapter exists yet, so a stored key is
    unused. Verification is still offered so the key can be pre-provisioned and
    checked before the adapter lands.
    """

    for host in ("https://api-free.deepl.com", "https://api.deepl.com"):
        try:
            response = httpx.post(
                f"{host}/v2/usage",
                headers={"Authorization": f"DeepL-Auth-Key {key}"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError:
            continue
        if response.status_code == 200:
            return True, f"accepted by DeepL ({host})"
        if response.status_code == 403:
            return False, f"DeepL rejected the key (403) at {host}"
    return False, "DeepL rejected the key on both the free and pro hosts"


def verify_key(provider: Provider, key: str) -> tuple[bool, str]:
    """Best-effort probe. Never raises: network trouble is reported, not fatal."""

    if not key or any(char.isspace() or not char.isprintable() for char in key):
        return False, "the key is empty or contains whitespace or control characters"

    try:
        match provider.name:
            case "deepgram":
                return _deepgram(key)
            case "deepseek":
                return _openai_compatible("https://api.deepseek.com", key, "DeepSeek")
            case "openai":
                return _openai_compatible("https://api.openai.com/v1", key, "OpenAI")
            case "deepl":
                return _deepl(key)
            case _:
                return True, f"no verification implemented for {provider.name!r}; skipped"
    except httpx.HTTPError as exc:
        return False, f"could not reach the provider to verify ({exc.__class__.__name__})"
    except Exception as exc:                                  # noqa: BLE001
        return False, f"verification failed unexpectedly ({exc.__class__.__name__})"
