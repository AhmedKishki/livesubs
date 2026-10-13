"""Tests for the config file.

Two things here are load-bearing rather than cosmetic:

* the config file must actually take effect. A nested group whose values are
  silently ignored would be the worst kind of failure, so it is pinned.
* the flat ``LIVESUBS_*`` environment surface must keep working after fields
  moved into groups. pydantic-settings does not descend into nested models for
  env vars, so this broke and had to be fixed with a custom source; the test
  exists so it cannot break again unnoticed.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from livesubs.config import (
    CONFIG_FILENAME,
    find_config_file,
    load_settings,
)

#: Every credential and setting the config layer knows about, cleared so that a
#: developer's own shell cannot change a result.
_CLEARED = (
    "DEEPGRAM_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DEEPL_API_KEY",
    "ACME_API_KEY",
    "LIVESUBS_CONFIG",
    "LIVESUBS_HOST",
    "LIVESUBS_PORT",
    "LIVESUBS_SESSION_ID",
    "LIVESUBS_SESSION_NAME",
    "LIVESUBS_CAPTURE_TARGET",
    "LIVESUBS_SAMPLE_RATE",
    "LIVESUBS_CHANNELS",
    "LIVESUBS_AUTO_CALIBRATE",
    "LIVESUBS_RUNTIME_CALIBRATION",
    "LIVESUBS_MAX_SEGMENT_MS",
    "LIVESUBS_MIN_SEGMENT_MS",
    "LIVESUBS_PROVIDER_DEEPGRAM_MODEL",
    "LIVESUBS_PROVIDER_DEEPSEEK_THINKING",
)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated working directory with no ambient LIVESUBS configuration."""

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_config(directory: Path, body: str, name: str = CONFIG_FILENAME) -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --- the file takes effect -----------------------------------------------------


def test_nested_groups_load_from_the_config_file(sandbox: Path) -> None:
    path = write_config(
        sandbox,
        """
        server:
          port: 9123
          log_level: debug
        session:
          id: physik-1
          name: Physik I
        capture:
          target: alsa_input.usb-example-00.analog-stereo
          archive: false
        segmentation:
          max_segment_ms: 1500
        """,
    )

    resolved = load_settings(path)

    # Grouped access...
    assert resolved.server.port == 9123
    assert resolved.session.id == "physik-1"
    assert resolved.capture.archive is False
    assert resolved.segmentation.max_segment_ms == 1500
    # ...and the flat accessors agree, so readers did not have to move.
    assert resolved.port == 9123
    assert resolved.session_id == "physik-1"
    assert resolved.archive_audio is False
    assert resolved.max_segment_ms == 1500


def test_unspecified_values_keep_their_defaults(sandbox: Path) -> None:
    resolved = load_settings(write_config(sandbox, "server:\n  port: 9123\n"))

    assert resolved.port == 9123
    assert resolved.session_id == "demo"
    assert resolved.min_segment_ms == 400
    assert resolved.capture.sample_rate == 16_000


def test_a_missing_config_file_is_normal(sandbox: Path) -> None:
    """Every setting has a default, so livesubs runs with no config file.

    Searched from ``sandbox`` rather than from the process, because the repository *expects*
    each deployment to keep its own gitignored ``livesubs.yaml``: a developer has one, this
    test would otherwise fail for them, and passing it must not depend on that file being
    absent.
    """

    assert find_config_file(start=sandbox) is None
    resolved = load_settings()
    assert resolved.port == 8000


def test_the_session_flag_overrides_the_config_file(sandbox: Path) -> None:
    """``--session-id`` has to reach everything that reads settings.

    It used to reach the session registry but not the settings, so an artefact took its
    name from the config file while the server answered questions about the flag's id.
    """

    write_config(
        sandbox,
        """
        session:
          id: from-config
          name: From config
        """,
    )
    settings = load_settings()

    assert settings.session_id == "from-config"

    overridden = settings.for_session(session_id="from-cli")

    assert overridden.session_id == "from-cli"
    assert overridden.session_name == "From config"
    # The override must not rebuild the deployment around it.
    assert overridden.port == settings.port
    assert overridden.segmentation.max_segment_ms == settings.segmentation.max_segment_ms
    assert set(overridden.registry) == set(settings.registry)


def test_overriding_nothing_leaves_the_settings_alone(sandbox: Path) -> None:
    settings = load_settings()

    assert settings.for_session() is settings


def test_an_explicitly_named_missing_file_is_an_error(sandbox: Path) -> None:
    """Naming a file is a deliberate act, so a typo must not pass silently."""

    with pytest.raises(FileNotFoundError):
        load_settings(sandbox / "nope.yaml")


def test_unknown_keys_are_ignored(sandbox: Path) -> None:
    """A stale key should not stop a lecture; extra="ignore" is deliberate."""

    resolved = load_settings(
        write_config(sandbox, "server:\n  port: 9123\nnonsense:\n  whatever: 1\n")
    )
    assert resolved.port == 9123


# --- precedence -----------------------------------------------------------------


def test_the_environment_overrides_the_config_file(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: nested fields used to be invisible to env vars.

    pydantic-settings does not descend into nested models when reading the
    environment, so moving a setting into a group silently killed its
    ``LIVESUBS_*`` override. The custom source in ``config.py`` fixes that.
    """

    path = write_config(sandbox, "server:\n  port: 9123\nsession:\n  id: from-file\n")
    monkeypatch.setenv("LIVESUBS_PORT", "9999")
    monkeypatch.setenv("LIVESUBS_SESSION_ID", "from-env")

    resolved = load_settings(path)

    assert resolved.port == 9999
    assert resolved.session_id == "from-env"


def test_the_env_file_overrides_the_config_file(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(sandbox, "server:\n  port: 9123\n")
    (sandbox / ".env").write_text("LIVESUBS_PORT=7777\n", encoding="utf-8")

    assert load_settings(path).port == 7777


def test_the_environment_beats_the_env_file(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(sandbox, "server:\n  port: 9123\n")
    (sandbox / ".env").write_text("LIVESUBS_PORT=7777\n", encoding="utf-8")
    monkeypatch.setenv("LIVESUBS_PORT", "5555")

    assert load_settings(path).port == 5555


def test_an_empty_env_value_does_not_override_the_file(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank line in .env means "unset", not "deliberately blank"."""

    path = write_config(sandbox, "server:\n  port: 9123\n")
    (sandbox / ".env").write_text("LIVESUBS_PORT=\n", encoding="utf-8")

    assert load_settings(path).port == 9123


def test_config_path_resolution_order(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = write_config(sandbox, "server:\n  port: 9123\n")
    assert find_config_file() == found

    elsewhere = sandbox / "other.yaml"
    elsewhere.write_text("server:\n  port: 1\n", encoding="utf-8")
    monkeypatch.setenv("LIVESUBS_CONFIG", str(elsewhere))
    assert find_config_file() == elsewhere

    # An explicit argument beats even LIVESUBS_CONFIG.
    assert find_config_file(explicit=found) == found


# --- providers -----------------------------------------------------------------


def test_provider_options_override_registry_defaults(sandbox: Path) -> None:
    resolved = load_settings(
        write_config(
            sandbox,
            """
            providers:
              deepgram:
                options:
                  model: nova-2
            """,
        )
    )

    assert resolved.provider_option("deepgram", "model") == "nova-2"
    # Options the file did not mention keep their registry default.
    assert resolved.provider_option("deepgram", "url") == "wss://api.deepgram.com"


def test_an_environment_option_beats_the_config_file(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        sandbox, "providers:\n  deepgram:\n    options:\n      model: nova-2\n"
    )
    monkeypatch.setenv("LIVESUBS_PROVIDER_DEEPGRAM_MODEL", "nova-3")

    assert load_settings(path).provider_option("deepgram", "model") == "nova-3"


def test_a_new_provider_can_be_declared_in_the_config_file(sandbox: Path) -> None:
    resolved = load_settings(
        write_config(
            sandbox,
            """
            providers:
              acme:
                role: asr
                env_var: ACME_API_KEY
                label: ACME streaming ASR
                options:
                  endpoint: wss://api.acme.test
            """,
        )
    )

    provider = resolved.registry["acme"]
    assert provider.role == "asr"
    assert provider.env_var == "ACME_API_KEY"
    assert provider.label == "ACME streaming ASR"
    assert resolved.provider_option("acme", "endpoint") == "wss://api.acme.test"

    # ...and it satisfies the ASR requirement once it has a key.
    assert "acme" in [p.name for p in resolved.providers_for("asr")]


def test_the_builtin_registry_is_not_mutated(sandbox: Path) -> None:
    """Config-declared providers must not leak into the module-level registry."""

    from livesubs.keystore import PROVIDERS

    load_settings(
        write_config(sandbox, "providers:\n  acme:\n    role: asr\n")
    )

    assert "acme" not in PROVIDERS


def test_a_new_provider_without_a_role_is_rejected(sandbox: Path) -> None:
    """Role cannot be guessed, so guessing would silently put it in the wrong stage."""

    with pytest.raises(ValueError, match="role"):
        load_settings(write_config(sandbox, "providers:\n  acme:\n    options:\n      a: b\n"))


def test_a_provider_can_be_marked_planned_in_the_file(sandbox: Path) -> None:
    resolved = load_settings(
        write_config(sandbox, "providers:\n  deepgram:\n    status: planned\n")
    )

    assert resolved.registry["deepgram"].planned
    # Planned providers hold a key and satisfy nothing.
    assert "deepgram" not in [p.name for p in resolved.providers_for("asr")]


# --- pipeline selection ---------------------------------------------------------


#: A second recogniser, declared in the file, so the ordering rules are tested with a provider
#: that is active by construction rather than one that happens to ship as active.
SECOND_ASR = "providers:\n  acme:\n    role: asr\n"


def test_pipeline_order_is_respected(sandbox: Path) -> None:
    resolved = load_settings(
        write_config(sandbox, SECOND_ASR + "pipeline:\n  asr: [acme, deepgram]\n")
    )

    assert [p.name for p in resolved.providers_for("asr")][:2] == ["acme", "deepgram"]


def test_unlisted_providers_still_work_after_the_listed_ones(sandbox: Path) -> None:
    resolved = load_settings(write_config(sandbox, SECOND_ASR + "pipeline:\n  asr: [acme]\n"))

    assert [p.name for p in resolved.providers_for("asr")] == ["acme", "deepgram"]


def test_active_provider_is_the_first_in_the_chain_with_a_key(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(sandbox, SECOND_ASR + "pipeline:\n  asr: [acme, deepgram]\n")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")          # only the second has a key
    monkeypatch.delenv("ACME_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")

    resolved = load_settings(path)

    assert resolved.active_provider("asr").name == "deepgram"
    assert resolved.validate_for_startup() == []


def test_a_recogniser_with_no_adapter_cannot_satisfy_the_requirement(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an OpenAI key set: `--check` used to pass and the session failed to start."""

    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-only")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")

    resolved = load_settings(write_config(sandbox, "pipeline:\n  asr: [openai, deepgram]\n"))

    assert resolved.active_provider("asr") is None
    assert any("ASR" in problem for problem in resolved.validate_for_startup())


def test_a_pipeline_entry_naming_an_unknown_provider_is_reported(sandbox: Path) -> None:
    """A typo would otherwise mean a stage silently running on the wrong provider."""

    resolved = load_settings(write_config(sandbox, "pipeline:\n  asr: [deegram]\n"))
    problems = resolved.validate_for_startup()

    assert any("deegram" in problem for problem in problems), problems


# --- cross-field validation -----------------------------------------------------


def test_capture_format_is_validated(sandbox: Path) -> None:
    """The ASR providers expect 16 kHz mono, so anything else is a hard error."""

    resolved = load_settings(
        write_config(sandbox, "capture:\n  sample_rate: 48000\n  channels: 2\n")
    )
    problems = resolved.validate_for_startup()

    assert any("sample_rate" in problem for problem in problems), problems
    assert any("channels" in problem for problem in problems), problems


def test_segmentation_bounds_are_validated(sandbox: Path) -> None:
    resolved = load_settings(
        write_config(sandbox, "segmentation:\n  min_segment_ms: 3000\n  max_segment_ms: 2000\n")
    )
    problems = resolved.validate_for_startup()

    assert any("min_segment_ms" in problem for problem in problems), problems


# --- repository hygiene ---------------------------------------------------------


def _git(*args: str, cwd: Path) -> int:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=False, timeout=15
    ).returncode


def test_the_deployment_config_is_gitignored() -> None:
    """A deployment's own config must not be committed by accident.

    It holds no secrets, but it describes one deployment: a capture device, a
    port, a session. Committing it would ship one machine's setup to everyone.
    """

    from livesubs.config import checkout_root

    root = checkout_root()
    if root is None or _git("rev-parse", "--git-dir", cwd=root) != 0:  # pragma: no cover
        pytest.skip("not a git checkout")

    assert _git("check-ignore", "--quiet", CONFIG_FILENAME, cwd=root) == 0


def test_the_example_config_is_not_gitignored() -> None:
    """The template must be tracked, or a new user has nothing to copy."""

    from livesubs.config import checkout_root

    root = checkout_root()
    if root is None or _git("rev-parse", "--git-dir", cwd=root) != 0:  # pragma: no cover
        pytest.skip("not a git checkout")

    assert _git("check-ignore", "--quiet", f"{CONFIG_FILENAME}.example", cwd=root) != 0

# --- credential requirement, which depends on the approach -----------------------


def test_an_approach_needing_no_keys_is_not_told_its_environment_is_broken(
    sandbox: Path,
) -> None:
    """A fresh checkout has no API keys and is perfectly usable for the replay.

    Reporting otherwise sends a new user hunting for a problem they do not have, on
    the very first command they are likely to run.
    """

    resolved = load_settings(write_config(sandbox, "server:\n  port: 8123\n"))

    assert resolved.validate_for_startup(require_credentials=True), (
        "the live pipeline does need keys, so this must not pass vacuously"
    )
    assert resolved.validate_for_startup(require_credentials=False) == []


def test_a_pipeline_typo_survives_the_credential_opt_out(sandbox: Path) -> None:
    """Skipping the key check must not also skip the operator's own mistake.

    A misspelled provider name means a stage quietly running on a different provider
    than intended, which is worth reporting whatever the approach.
    """

    resolved = load_settings(write_config(sandbox, "pipeline:\n  mt: [deespeek]\n"))

    problems = resolved.validate_for_startup(require_credentials=False)

    assert any("deespeek" in problem for problem in problems), problems


def test_structural_validation_survives_the_credential_opt_out(sandbox: Path) -> None:
    """The remaining checks are about the pipeline itself and apply regardless."""

    resolved = load_settings(
        write_config(sandbox, "segmentation:\n  min_segment_ms: 3000\n  max_segment_ms: 2000\n")
    )

    problems = resolved.validate_for_startup(require_credentials=False)

    assert any("min_segment_ms" in problem for problem in problems), problems


def test_calibration_defaults_are_on_and_yaml_can_disable_them(sandbox):
    resolved = load_settings(write_config(sandbox, "capture:\n  target: mic\n"))
    assert resolved.capture.auto_calibrate and resolved.capture.runtime_calibration
    resolved = load_settings(write_config(
        sandbox, "capture:\n  auto_calibrate: false\n  runtime_calibration: false\n"
    ))
    assert not resolved.capture.auto_calibrate and not resolved.capture.runtime_calibration


def test_calibration_environment_overrides_yaml(sandbox, monkeypatch):
    path = write_config(sandbox, "capture:\n  auto_calibrate: true\n")
    monkeypatch.setenv("LIVESUBS_AUTO_CALIBRATE", "false")
    monkeypatch.setenv("LIVESUBS_RUNTIME_CALIBRATION", "false")
    resolved = load_settings(path)
    assert not resolved.capture.auto_calibrate and not resolved.capture.runtime_calibration
