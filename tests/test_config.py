"""Tests for provider resolution and startup validation.

The point of these is the modularity guarantee: the registry in
``livesubs.keystore`` is the single source of truth for providers, so adding one
is a single dict entry and no edit to ``config.py``, the CLI, or another
provider's code. That claim is easy to make and easy to break, so it is pinned
here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from livesubs.config import Settings
from livesubs.keystore import PROVIDERS, Provider

#: Every credential the registry knows about. Cleared before asserting anything
#: about *missing* providers, so a developer's own shell cannot change the result.
_ALL_KEY_VARS = [provider.env_var for provider in PROVIDERS.values()]


@pytest.fixture
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in _ALL_KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def settings() -> Settings:
    return Settings(_env_file=None)


# --- the registry drives everything ------------------------------------------


def test_adding_a_provider_needs_no_config_change(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new registry entry is immediately resolvable, validated and reported."""

    monkeypatch.setitem(
        PROVIDERS,
        "acme",
        Provider(
            name="acme",
            env_var="ACME_API_KEY",
            label="ACME (test ASR)",
            role="asr",
            hint="n/a",
        ),
    )
    monkeypatch.setenv("ACME_API_KEY", "acme-key")

    resolved = settings()

    # Resolves without any config.py field for it.
    key = resolved.provider_key("acme")
    assert key is not None
    assert key.get_secret_value() == "acme-key"

    # Reported without any CLI change.
    assert resolved.providers_with_keys()["acme"] == "environment"

    # Satisfies the ASR requirement, so validation picks it up automatically.
    problems = resolved.validate_for_startup()
    assert not any("ASR" in problem for problem in problems), problems
    # MT is still unsatisfied, so this is not passing vacuously.
    assert any("MT" in problem for problem in problems), problems


def test_validation_is_provider_agnostic(clean_env: None) -> None:
    """With no keys at all, both roles are reported by role name."""

    problems = settings().validate_for_startup()

    assert any("ASR" in problem for problem in problems), problems
    assert any("MT" in problem for problem in problems), problems


def test_every_role_has_at_least_one_active_provider() -> None:
    """Otherwise nothing could ever satisfy startup validation."""

    for role in ("asr", "mt"):
        assert [p for p in PROVIDERS.values() if p.role == role and not p.planned], role


# --- credentials --------------------------------------------------------------


def test_provider_key_resolves_from_the_environment(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "from-env")

    key = settings().provider_key("deepgram")

    assert key is not None
    assert key.get_secret_value() == "from-env"
    assert settings().provider_source("deepgram") == "environment"


def test_provider_key_is_none_when_unset(clean_env: None) -> None:
    assert settings().provider_key("deepgram") is None
    assert settings().provider_source("deepgram") == "unset"
    assert settings().providers_with_keys() == {}


def test_planned_providers_hold_keys_without_satisfying_validation(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DeepL case: stored, visible, and still not counted."""

    monkeypatch.setenv("DEEPL_API_KEY", "dl")

    resolved = settings()
    assert resolved.provider_key("deepl") is not None
    assert "deepl" in resolved.providers_with_keys()

    problems = resolved.validate_for_startup()
    assert any("MT" in problem for problem in problems), problems


# --- tuning options -----------------------------------------------------------


def test_provider_options_default_to_the_registry(clean_env: None) -> None:
    resolved = settings()

    assert resolved.provider_option("deepgram", "model") == "nova-3"
    assert resolved.provider_option("deepgram", "url") == "wss://api.deepgram.com"
    assert resolved.provider_option("deepseek", "base_url") == "https://api.deepseek.com"
    assert resolved.provider_option("openai", "delay") == "low"


def test_provider_options_are_overridable_by_environment(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment can retune a model without a code change."""

    monkeypatch.setenv("LIVESUBS_PROVIDER_DEEPGRAM_MODEL", "nova-2")

    assert settings().provider_option("deepgram", "model") == "nova-2"
    # Other options are untouched.
    assert settings().provider_option("deepgram", "url") == "wss://api.deepgram.com"
    # The recogniser's language comes from the session's mode unless deliberately set.
    assert settings().provider_option("deepgram", "language") is None


def test_unknown_option_has_no_value(clean_env: None) -> None:
    assert settings().provider_option("deepgram", "nonsense") is None


# --- declared invariants ------------------------------------------------------


def test_realtime_invariant_is_declared_by_the_provider(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thinking mode must stay off, and the rule lives in the registry.

    ``config.py`` never names DeepSeek: it iterates whatever invariants the active
    providers declare, so a future provider with its own realtime constraint gets
    the same treatment for free.
    """

    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("LIVESUBS_PROVIDER_DEEPSEEK_THINKING", "enabled")

    problems = settings().validate_for_startup()

    assert any("THINKING" in problem for problem in problems), problems
    assert any("latency" in problem for problem in problems), problems


def test_the_shipped_defaults_satisfy_every_declared_invariant(clean_env: None) -> None:
    """A fresh install must not fail its own invariants out of the box."""

    for provider in PROVIDERS.values():
        for option, required, _why in provider.invariants:
            assert settings().provider_option(provider.name, option) == required, (
                f"{provider.name}:{option}"
            )


def test_invariants_are_ignored_for_planned_providers(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planned provider cannot block startup: it is not wired in yet."""

    monkeypatch.setitem(
        PROVIDERS,
        "plannedx",
        Provider(
            name="plannedx",
            env_var="PLANNEDX_API_KEY",
            label="Planned X",
            role="mt",
            hint="n/a",
            status="planned",
            options={"x": "wrong"},
            invariants=(("x", "y", "must be y"),),
        ),
    )

    problems = settings().validate_for_startup()

    assert not any("PLANNEDX" in problem for problem in problems), problems


# --- who is speaking ---------------------------------------------------------------


def test_the_default_session_is_an_english_speaker(clean_env: None) -> None:
    resolved = settings()

    assert resolved.mode.source == "en"
    assert resolved.source_language == "en"
    assert resolved.target_languages == ("de", "ar")
    assert resolved.default_language == "de"


def test_a_german_speaker_is_offered_english_first(clean_env: None) -> None:
    resolved = Settings(_env_file=None, session={"speaker": "de"})

    assert resolved.source_language == "de"
    assert resolved.target_languages == ("en", "ar")
    assert resolved.default_language == "en"


def test_the_speaker_can_be_set_from_the_environment(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from livesubs.config import load_settings

    monkeypatch.setenv("LIVESUBS_SESSION_SPEAKER", "de")

    assert load_settings().source_language == "de"


def test_a_deployment_can_offer_fewer_languages(clean_env: None) -> None:
    resolved = Settings(_env_file=None, session={"speaker": "en", "targets": ["ar", "de"]})

    assert resolved.target_languages == ("ar", "de")
    assert resolved.default_language == "ar"


def test_startup_reports_an_unsupported_speaker(clean_env: None) -> None:
    resolved = Settings(_env_file=None, session={"speaker": "fr"})

    problems = resolved.validate_for_startup(require_credentials=False)

    assert any("fr" in problem and "speaker" in problem for problem in problems), problems


def test_startup_reports_a_target_the_mode_does_not_offer(clean_env: None) -> None:
    resolved = Settings(_env_file=None, session={"speaker": "de", "targets": ["de"]})

    problems = resolved.validate_for_startup(require_credentials=False)

    assert any("cannot be a target" in problem for problem in problems), problems
