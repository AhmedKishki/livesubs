"""Tests for the approach registry.

The guarantee: adding an approach does not require editing the CLI, the server or
the hub. That is the whole reason this indirection exists, so it is pinned rather
than assumed.
"""

from __future__ import annotations

import asyncio

import pytest

from livesubs.approaches import (
    APPROACHES,
    Approach,
    build_approach,
    register,
    requires_credentials,
    resolve_audio_spec,
)
from livesubs.config import Settings
from livesubs.hub import Hub, SegmentPublisher


class _NoopPublisher:
    """Minimal publisher, to prove the registry needs nothing from the CLI."""

    def __init__(self, **options: str) -> None:
        self.options = options

    async def run(self, hub: Hub, *, session_id: str, stop: asyncio.Event) -> None:
        return


def _settings(capture_target: str = "") -> Settings:
    # The capture target is set through the init rather than left to the environment,
    # so a developer's own shell cannot change what these tests assert.
    return Settings(_env_file=None, capture={"target": capture_target})


# --- what ships ---------------------------------------------------------------


def test_sim_is_registered_and_available() -> None:
    approach = APPROACHES["sim"]
    assert approach.available
    assert approach.status == "ready"


def test_live_is_registered_and_available() -> None:
    """It was listed honestly as planned until the adapters existed; now they do.

    The exit criterion of M2 is that this approach is buildable, so the test that used to
    assert its absence asserts its presence instead - and asserts which roles it needs, so
    that a machine without an ASR key is told about the ASR key.
    """

    approach = APPROACHES["live"]

    assert approach.available
    assert approach.status == "ready"
    assert approach.required_roles == ("asr", "mt")


def test_live_text_needs_a_translator_and_not_a_recogniser() -> None:
    """``live-text`` translates a transcript, so an ASR key must not be demanded for it.

    Validation is the thing at stake: demanding the wrong role would refuse to start a run
    that works.
    """

    approach = APPROACHES["live-text"]

    assert approach.available
    assert approach.required_roles == ("mt",)
    assert approach.requires_credentials


def test_every_registered_approach_has_a_summary_and_label() -> None:
    for approach in APPROACHES.values():
        assert approach.label
        assert approach.summary
        assert len(approach.summary) > 20


# --- building -----------------------------------------------------------------


def test_sim_builds_a_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    publisher = build_approach("sim", _settings(), {"script": "default", "speed": "0"})
    assert isinstance(publisher, SegmentPublisher)


def test_unknown_approach_raises_with_the_available_names() -> None:
    with pytest.raises(KeyError) as excinfo:
        build_approach("nope", _settings(), {})

    assert "nope" in str(excinfo.value)
    assert "sim" in str(excinfo.value)


def test_unavailable_approach_explains_itself() -> None:
    """A half-started server would be worse than a clear refusal.

    Registered inside the test because every shipped approach is now buildable, and the
    machinery for an unbuildable one still has to work: it is how a future approach is
    listed before its adapter exists.
    """

    register(
        Approach(
            name="not-yet",
            label="Registered, not built",
            summary="Exercises the refusal path.",
            status="planned",
            unavailable_reason="the adapter is a later phase; see docs/10-roadmap.md",
        )
    )

    with pytest.raises(NotImplementedError) as excinfo:
        build_approach("not-yet", _settings(), {})

    assert "roadmap" in str(excinfo.value)


# --- extensibility ------------------------------------------------------------


def test_a_new_approach_needs_no_cli_or_server_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One registration is enough to be selectable and buildable.

    Nothing here touches the CLI, the server, the hub or the overlay, which is the
    property that keeps alternative approaches cheap to add later: local models, a
    one-call speech-translation service, or a hybrid.
    """

    monkeypatch.setitem(
        APPROACHES,
        "custom",
        Approach(
            name="custom",
            label="Custom approach",
            summary="Stand-in for a future approach, e.g. local models.",
            build=lambda settings, options: _NoopPublisher(**options),
        ),
    )

    publisher = build_approach("custom", _settings(), {"anything": "goes"})

    assert isinstance(publisher, SegmentPublisher)
    assert publisher.options == {"anything": "goes"}       # type: ignore[attr-defined]


def test_register_returns_and_replaces() -> None:
    approach = Approach(name="temp", label="Temp", summary="A temporary approach for testing.")

    register(approach)
    assert APPROACHES["temp"] is approach
    try:
        register(
            Approach(name="temp", label="Temp 2", summary="Replacement for the same name.")
        )
        assert APPROACHES["temp"].label == "Temp 2"
    finally:
        APPROACHES.pop("temp", None)


def test_a_planned_approach_without_a_reason_still_reports_cleanly() -> None:
    APPROACHES["bare"] = Approach(name="bare", label="Bare", summary="x", status="planned")
    try:
        with pytest.raises(NotImplementedError):
            build_approach("bare", _settings(), {})
    finally:
        APPROACHES.pop("bare", None)


def test_an_approach_marked_ready_but_without_a_builder_is_not_available() -> None:
    """Defensive: inconsistent registration must not crash the server later."""

    approach = Approach(name="broken", label="Broken", summary="ready but no builder")
    assert not approach.available


# --- which approaches need credentials -------------------------------------------


def test_only_the_vendor_calling_approach_requires_credentials() -> None:
    """The validation commands read this, so it decides what a new user is told."""

    assert requires_credentials("live") is True
    assert requires_credentials("sim") is False
    assert requires_credentials("live-sim") is False


def test_an_unknown_approach_is_assumed_to_need_credentials() -> None:
    """Assume the safe direction.

    The other way round approves a deployment whose approach cannot be identified, and
    that deployment would start and silently produce nothing.
    """

    assert requires_credentials("not-registered-at-all") is True


# --- which audio a live approach uses --------------------------------------------


def test_an_explicit_audio_option_wins() -> None:
    """A one-off run should not have to edit the config to override it."""

    settings = _settings(capture_target="mic")

    assert resolve_audio_spec({"audio": "file:x.wav"}, settings) == "file:x.wav"


def test_the_configured_capture_device_is_used_when_nothing_is_chosen() -> None:
    """Otherwise `capture.target` would appear to work and do nothing.

    Pre-flight and the documentation both tell an operator to set it, so the pipeline
    honouring it is the difference between a setting and a decoy.
    """

    assert resolve_audio_spec({}, _settings(capture_target="mic")) == "pw:mic"


def test_synthetic_audio_is_the_last_resort() -> None:
    """A fresh checkout with nothing configured must still do something."""

    assert resolve_audio_spec({}, _settings(capture_target="")) == "synth"


def test_an_empty_audio_option_falls_through_rather_than_choosing_silence() -> None:
    """The CLI passes an empty string for "not chosen", which must not become a spec."""

    assert resolve_audio_spec({"audio": "  "}, _settings(capture_target="mic")) == "pw:mic"


# --- how long a translation may take ---------------------------------------------------


def test_a_hosted_translator_gets_a_short_limit_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from livesubs.approaches import DEFAULT_MT_TIMEOUT_S, mt_timeout_s, pipeline_config

    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.delenv("LIVESUBS_PROVIDER_DEEPSEEK_TIMEOUT_S", raising=False)
    settings = _settings()

    assert mt_timeout_s(settings) == DEFAULT_MT_TIMEOUT_S == 8.0
    assert pipeline_config(settings).mt_deadline_s == 8.0


def test_a_slow_local_model_can_be_given_longer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pipeline's limit follows the provider's, or it would cut a local model off."""

    from livesubs.approaches import pipeline_config

    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("LIVESUBS_PROVIDER_DEEPSEEK_TIMEOUT_S", "180")

    assert pipeline_config(_settings()).mt_deadline_s == 180.0


def test_without_a_translator_the_default_limit_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    from livesubs.approaches import mt_timeout_s

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("livesubs.config.Settings.provider_key", lambda self, name: None)

    assert mt_timeout_s(_settings()) == 8.0
