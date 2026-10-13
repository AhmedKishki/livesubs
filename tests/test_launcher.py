"""Tests for the installed-command startup bootstrap.

Every external edge is faked: PipeWire enumeration, the microphone picker, hidden
input, the provider probe, and settings reload. The only real integration is the
``keystore.upsert_env_var`` write in the global-save test, which uses a temporary
file. No microphone, network, credentials, or config file is touched.
"""

from __future__ import annotations

import os

import pytest

from livesubs import config as config_module
from livesubs import keystore, launcher
from livesubs.config import Settings


def make_settings(**overrides: object) -> Settings:
    """Settings isolated from the on-disk ``.env`` and the real environment."""

    return Settings(_env_file=None, **overrides)


def use_sources(monkeypatch, sources: list[tuple[str, str]]) -> None:
    monkeypatch.setattr(launcher.pw_record, "pipewire_sources", lambda: list(sources))


def use_picker(monkeypatch, target: str) -> list[tuple[list[tuple[str, str]], str | None]]:
    calls: list[tuple[list[tuple[str, str]], str | None]] = []

    def fake(
        sources: list[tuple[str, str]], preferred: str | None = None
    ) -> str:
        calls.append((list(sources), preferred))
        return target

    monkeypatch.setattr(launcher, "_select_microphone", fake)
    return calls


def accept_keys(monkeypatch) -> None:
    """An active provider chain whose probe always succeeds."""

    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake-deepgram")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek")
    monkeypatch.setattr(launcher, "_verify_key", lambda provider, key: (True, "accepted"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    missing_global = tmp_path / "missing-global.env"
    missing_project = tmp_path / "missing-project.env"
    monkeypatch.setattr(keystore, "global_env_path", lambda: missing_global)
    monkeypatch.setattr(keystore, "project_env_path", lambda start=None: missing_project)
    monkeypatch.setattr(config_module, "global_env_path", lambda: missing_global)
    monkeypatch.setattr(config_module, "project_env_path", lambda start=None: missing_project)

    for var in ("DEEPGRAM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "DEEPL_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    def forbidden(name: str):
        def _raise(*args: object, **kwargs: object) -> object:
            raise AssertionError(f"{name} must not be called")

        return _raise

    monkeypatch.setattr(launcher, "_read_secret", forbidden("_read_secret"))
    monkeypatch.setattr(launcher, "_select_microphone", forbidden("_select_microphone"))
    monkeypatch.setattr(launcher, "_verify_key", forbidden("_verify_key"))
    monkeypatch.setattr(launcher, "_reload_settings", forbidden("_reload_settings"))
    monkeypatch.setattr(launcher.pw_record, "pipewire_sources", forbidden("pipewire_sources"))
    return tmp_path


# --- source selection ----------------------------------------------------------


def test_tty_always_calls_picker_even_single_source(monkeypatch):
    use_sources(monkeypatch, [("mic0", "Built-in Mic")])
    accept_keys(monkeypatch)
    calls = use_picker(monkeypatch, "mic0")

    result = launcher.prepare_live_start(make_settings(), interactive=True)

    assert calls == [([("mic0", "Built-in Mic")], None)]
    assert result.capture.target == "mic0"


def test_tty_preselects_configured_target(monkeypatch):
    use_sources(monkeypatch, [("mic0", "Mic 0"), ("mic1", "Mic 1")])
    accept_keys(monkeypatch)
    calls = use_picker(monkeypatch, "mic1")

    result = launcher.prepare_live_start(
        make_settings(capture={"target": "mic1"}), interactive=True
    )

    assert calls[0][1] == "mic1"
    assert result.capture.target == "mic1"


def test_tty_explicit_preferred_beats_configured(monkeypatch):
    use_sources(monkeypatch, [("mic0", "Mic 0"), ("mic1", "Mic 1")])
    accept_keys(monkeypatch)
    calls = use_picker(monkeypatch, "mic0")

    launcher.prepare_live_start(
        make_settings(capture={"target": "mic1"}),
        preferred_target="mic0",
        interactive=True,
    )

    assert calls[0][1] == "mic0"


def test_tty_unknown_preference_still_shows_the_picker(monkeypatch, capsys):
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    accept_keys(monkeypatch)
    calls = use_picker(monkeypatch, "mic0")

    result = launcher.prepare_live_start(
        make_settings(), preferred_target="nope", interactive=True
    )
    assert calls == [([("mic0", "Mic 0")], None)]
    assert result.capture_target == "mic0"
    assert "preference" in capsys.readouterr().err


def test_tty_stale_configured_warns_and_requires_choice(monkeypatch, capsys):
    use_sources(monkeypatch, [("mic0", "Mic 0"), ("mic1", "Mic 1")])
    accept_keys(monkeypatch)
    calls = use_picker(monkeypatch, "mic0")

    result = launcher.prepare_live_start(
        make_settings(capture={"target": "vanished"}), interactive=True
    )

    assert calls[0][1] is None
    assert result.capture.target == "mic0"
    assert "not available" in capsys.readouterr().err


def test_non_tty_accepts_explicit_target_and_lists_sources(monkeypatch, capsys):
    use_sources(monkeypatch, [("mic0", "Mic 0"), ("mic1", "Mic 1")])
    accept_keys(monkeypatch)

    result = launcher.prepare_live_start(
        make_settings(), preferred_target="mic1", interactive=False
    )

    assert result.capture.target == "mic1"
    err = capsys.readouterr().err
    assert "mic0" in err and "mic1" in err


def test_non_tty_accepts_configured_target(monkeypatch):
    use_sources(monkeypatch, [("mic0", "Mic 0"), ("mic1", "Mic 1")])
    accept_keys(monkeypatch)

    result = launcher.prepare_live_start(
        make_settings(capture={"target": "mic0"}), interactive=False
    )

    assert result.capture.target == "mic0"


def test_non_tty_refuses_single_source_without_target(monkeypatch):
    use_sources(monkeypatch, [("only", "Only Mic")])
    accept_keys(monkeypatch)

    with pytest.raises(ValueError, match="non-interactive"):
        launcher.prepare_live_start(make_settings(), interactive=False)


def test_non_tty_refuses_stale_configured_target(monkeypatch):
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    accept_keys(monkeypatch)

    with pytest.raises(ValueError, match="vanished"):
        launcher.prepare_live_start(
            make_settings(capture={"target": "vanished"}), interactive=False
        )


def test_no_sources_is_fatal(monkeypatch):
    use_sources(monkeypatch, [])
    accept_keys(monkeypatch)

    with pytest.raises(ValueError, match="No PipeWire microphone"):
        launcher.prepare_live_start(make_settings(), interactive=True)


def test_enumeration_error_is_fatal(monkeypatch):
    def boom() -> list[tuple[str, str]]:
        raise RuntimeError("pw-dump failed: no PipeWire")

    monkeypatch.setattr(launcher.pw_record, "pipewire_sources", boom)
    accept_keys(monkeypatch)

    with pytest.raises(ValueError, match="Cannot list PipeWire microphones"):
        launcher.prepare_live_start(make_settings(), interactive=True)


def test_default_interactive_follows_tty(monkeypatch):
    class Tty:
        def __init__(self, value: bool) -> None:
            self._value = value

        def isatty(self) -> bool:
            return self._value

        def write(self, text: str) -> int:
            return len(text)

    use_sources(monkeypatch, [("mic0", "Mic 0")])
    accept_keys(monkeypatch)
    calls = use_picker(monkeypatch, "mic0")

    monkeypatch.setattr(launcher.sys, "stdin", Tty(True))
    monkeypatch.setattr(launcher.sys, "stderr", Tty(True))
    launcher.prepare_live_start(make_settings())
    assert calls, "a TTY default must reach the picker"

    calls.clear()
    monkeypatch.setattr(launcher.sys, "stdin", Tty(False))
    monkeypatch.setattr(launcher.sys, "stderr", Tty(True))
    with pytest.raises(ValueError, match="non-interactive"):
        launcher.prepare_live_start(make_settings())
    assert not calls, "a non-TTY default must never reach the picker"


# --- provider bootstrap --------------------------------------------------------


def test_new_key_saved_to_global_env_owner_only(monkeypatch, tmp_path):
    global_path = tmp_path / "livesubs" / ".env"
    monkeypatch.setattr(keystore, "global_env_path", lambda: global_path)
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    use_picker(monkeypatch, "mic0")
    monkeypatch.setattr(launcher, "_read_secret", lambda prompt: "fake-new-deepgram")
    monkeypatch.setattr(launcher, "_verify_key", lambda provider, key: (True, "accepted"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek")
    monkeypatch.setattr(launcher, "_reload_settings", lambda config_path: make_settings())

    result = launcher.prepare_live_start(make_settings(), interactive=True)

    assert global_path.read_text(encoding="utf-8").strip() == (
        "DEEPGRAM_API_KEY=fake-new-deepgram"
    )
    assert os.stat(global_path).st_mode & 0o777 == 0o600
    assert result.active_provider("asr").name == "deepgram"


def test_invalid_exported_key_aborts_without_prompt(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake-exported")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek")
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    use_picker(monkeypatch, "mic0")

    def reject(provider, key):
        if provider.name == "deepgram":
            return False, "Deepgram returned 403: forbidden"
        return True, "accepted"

    monkeypatch.setattr(launcher, "_verify_key", reject)

    with pytest.raises(ValueError, match="DEEPGRAM_API_KEY"):
        launcher.prepare_live_start(make_settings(), interactive=True)


def test_rejected_project_key_is_replaced_in_project_file(monkeypatch, tmp_path):
    project_path = tmp_path / "project" / ".env"
    project_path.parent.mkdir(parents=True)
    project_path.write_text("DEEPGRAM_API_KEY=old-fake\n", encoding="utf-8")
    global_path = tmp_path / "global.env"

    monkeypatch.setattr(keystore, "project_env_path", lambda start=None: project_path)
    monkeypatch.setattr(config_module, "project_env_path", lambda start=None: project_path)
    monkeypatch.setattr(keystore, "global_env_path", lambda: global_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek")
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    use_picker(monkeypatch, "mic0")
    monkeypatch.setattr(launcher, "_read_secret", lambda prompt: "new-fake-key")
    monkeypatch.setattr(launcher, "_reload_settings", lambda config_path: make_settings())

    probes = {"deepgram": 0}

    def verify(provider, key):
        if provider.name == "deepgram":
            probes["deepgram"] += 1
            if probes["deepgram"] == 1:
                return False, "Deepgram returned 401: unauthorized"
        return True, "accepted"

    monkeypatch.setattr(launcher, "_verify_key", verify)

    launcher.prepare_live_start(make_settings(), interactive=True)

    assert project_path.read_text(encoding="utf-8").strip() == (
        "DEEPGRAM_API_KEY=new-fake-key"
    )
    assert not global_path.exists()


def test_non_auth_verification_failure_aborts_without_prompt(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake-deepgram")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek")
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    use_picker(monkeypatch, "mic0")

    def network(provider, key):
        if provider.name == "deepgram":
            return False, "could not reach the provider to verify (ConnectError)"
        return True, "accepted"

    monkeypatch.setattr(launcher, "_verify_key", network)

    with pytest.raises(ValueError, match="Aborting"):
        launcher.prepare_live_start(make_settings(), interactive=True)


def test_invalid_new_key_is_not_written(monkeypatch, tmp_path):
    global_path = tmp_path / "livesubs" / ".env"
    monkeypatch.setattr(keystore, "global_env_path", lambda: global_path)
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    use_picker(monkeypatch, "mic0")
    monkeypatch.setattr(launcher, "_read_secret", lambda prompt: "bad-key")
    monkeypatch.setattr(launcher, "_verify_key", lambda provider, key: (False, "rejected 401"))

    with pytest.raises(ValueError, match="did not accept"):
        launcher.prepare_live_start(make_settings(), interactive=True)

    assert not global_path.exists()


def test_unsupported_active_asr_adapter_is_rejected_before_prompt(monkeypatch):
    # Only Deepgram has an ASR adapter; an active provider without one must be refused
    # before any key prompt or network call. The fixture forbids _read_secret. The
    # provider is declared in the settings: none that ships is active without an adapter.
    monkeypatch.setenv("ACME_API_KEY", "fake-acme")
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    use_picker(monkeypatch, "mic0")
    settings = make_settings(
        providers={"acme": {"role": "asr"}}, pipeline={"asr": ["acme"]}
    )

    with pytest.raises(ValueError, match="acme"):
        launcher.prepare_live_start(settings, interactive=True)


def test_keyless_local_mt_skips_probe(monkeypatch):
    probe_calls: list[str] = []

    def verify(provider, key):
        probe_calls.append(provider.name)
        return True, "accepted"

    monkeypatch.setattr(launcher, "_verify_key", verify)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake-deepgram")
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    use_picker(monkeypatch, "mic0")

    settings = make_settings(
        providers={
            "localmt": {
                "role": "mt",
                "requires_key": False,
                "status": "active",
                "options": {"adapter": "openai_compat"},
            }
        },
        pipeline={"asr": ["deepgram"], "mt": ["localmt"]},
    )

    result = launcher.prepare_live_start(settings, interactive=True)

    assert result.active_provider("mt").name == "localmt"
    assert probe_calls == ["deepgram"]


def test_custom_openai_compat_mt_key_is_not_probed(monkeypatch):
    probe_calls: list[str] = []

    def verify(provider, key):
        probe_calls.append(provider.name)
        return True, "accepted"

    monkeypatch.setattr(launcher, "_verify_key", verify)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake-deepgram")
    monkeypatch.setenv("LOCALMT_API_KEY", "fake-local")
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    use_picker(monkeypatch, "mic0")

    settings = make_settings(
        providers={
            "localmt": {
                "role": "mt",
                "env_var": "LOCALMT_API_KEY",
                "status": "active",
                "options": {"adapter": "openai_compat"},
            }
        },
        pipeline={"asr": ["deepgram"], "mt": ["localmt"]},
    )

    result = launcher.prepare_live_start(settings, interactive=True)

    assert result.active_provider("mt").name == "localmt"
    assert probe_calls == ["deepgram"]


def test_missing_key_headless_refuses_and_reads_no_stdin(monkeypatch):
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    # The fixture forbids _read_secret, proving nothing is read from stdin.

    with pytest.raises(ValueError, match="terminal"):
        launcher.prepare_live_start(
            make_settings(), preferred_target="mic0", interactive=False
        )


def test_reload_result_carries_chosen_target(monkeypatch, tmp_path):
    global_path = tmp_path / "livesubs" / ".env"
    monkeypatch.setattr(keystore, "global_env_path", lambda: global_path)
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    use_picker(monkeypatch, "mic0")
    monkeypatch.setattr(launcher, "_read_secret", lambda prompt: "fake-new-deepgram")
    monkeypatch.setattr(launcher, "_verify_key", lambda provider, key: (True, "accepted"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek")

    # The reload deliberately disagrees; the chosen target must win.
    monkeypatch.setattr(
        launcher,
        "_reload_settings",
        lambda config_path: make_settings(capture={"target": "from-config"}),
    )

    result = launcher.prepare_live_start(make_settings(), interactive=True)

    assert result.capture.target == "mic0"


def test_secret_never_appears_in_error_or_output(monkeypatch, capsys):
    secret = "super-secret-value-12345"
    monkeypatch.setenv("DEEPGRAM_API_KEY", secret)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek")
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    use_picker(monkeypatch, "mic0")

    def reject(provider, key):
        if provider.name == "deepgram":
            return False, "Deepgram returned 403: forbidden"
        return True, "accepted"

    monkeypatch.setattr(launcher, "_verify_key", reject)

    with pytest.raises(ValueError) as excinfo:
        launcher.prepare_live_start(make_settings(), interactive=True)

    captured = capsys.readouterr()
    assert secret not in str(excinfo.value)
    assert secret not in captured.out
    assert secret not in captured.err


def test_unsupported_mt_is_rejected_before_verifying_asr(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake-deepgram")
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    use_picker(monkeypatch, "mic0")
    settings = make_settings(
        providers={"unsupported": {"role": "mt", "requires_key": False}},
        pipeline={"mt": ["unsupported"]},
    )
    with pytest.raises(ValueError, match="no adapter is declared"):
        launcher.prepare_live_start(settings, interactive=True)


def test_reload_rejects_a_different_key_for_the_same_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(keystore, "global_env_path", lambda: tmp_path / "global.env")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek")
    use_sources(monkeypatch, [("mic0", "Mic 0")])
    use_picker(monkeypatch, "mic0")
    monkeypatch.setattr(launcher, "_read_secret", lambda prompt: "verified-fake-key")
    monkeypatch.setattr(launcher, "_verify_key", lambda provider, key: (True, "accepted"))

    def reload(config_path):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "different-fake-key")
        return make_settings()

    monkeypatch.setattr(launcher, "_reload_settings", reload)
    with pytest.raises(ValueError, match="higher-precedence override") as exc:
        launcher.prepare_live_start(make_settings(), interactive=True)
    assert "verified-fake-key" not in str(exc.value)
    assert "different-fake-key" not in str(exc.value)
