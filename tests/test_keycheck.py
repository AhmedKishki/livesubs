"""Credential probes must not echo keys from responses or exception messages."""

import httpx
import pytest

from livesubs import keycheck
from livesubs.keystore import PROVIDERS


@pytest.mark.parametrize("name", ["deepgram", "deepseek", "openai"])
def test_rejected_key_is_not_reflected_from_the_response(monkeypatch, name):
    secret = "fake-secret-value-for-tests"
    monkeypatch.setattr(
        keycheck.httpx,
        "get",
        lambda *args, **kwargs: httpx.Response(403, text=f"Rejected key: {secret}"),
    )
    accepted, detail = keycheck.verify_key(PROVIDERS[name], secret)
    assert not accepted
    assert "returned 403" in detail
    assert secret not in detail


def test_exception_does_not_expose_credential_or_headers(monkeypatch):
    secret = "fake-secret-value-for-tests"

    def fail(*args, **kwargs):
        raise ValueError(f"Invalid Authorization: Bearer {secret}")

    monkeypatch.setattr(keycheck.httpx, "get", fail)
    accepted, detail = keycheck.verify_key(PROVIDERS["deepseek"], secret)
    assert not accepted
    assert "ValueError" in detail
    assert secret not in detail
    assert "Authorization" not in detail


@pytest.mark.parametrize("secret", ["", "fake key", "fake\x1bkey", "fake\nkey"])
def test_malformed_key_is_rejected_before_network_access(monkeypatch, secret):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not make a request")

    monkeypatch.setattr(keycheck.httpx, "get", forbidden)
    accepted, detail = keycheck.verify_key(PROVIDERS["deepgram"], secret)
    assert not accepted
    assert "whitespace or control characters" in detail
