"""Startup dispatch with fake capture, credentials and server edges."""

from __future__ import annotations

import os
import shutil

import pytest

from livesubs import __main__ as cli
from livesubs import launcher, preflight
from livesubs.config import Settings


@pytest.fixture
def startup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake-deepgram")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek")
    monkeypatch.delenv("LIVESUBS_CONFIG", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    settings = Settings(_env_file=None)
    monkeypatch.setattr(cli, "get_settings", lambda path: settings)
    calls = {"prepare": [], "preflight": [], "serve": []}

    def prepare(settings, **kwargs):
        calls["prepare"].append(kwargs)
        return settings.model_copy(
            update={"capture": settings.capture.model_copy(update={"target": "chosen-mic"})}
        )

    def check(settings, *, approach_name):
        calls["preflight"].append((settings, approach_name))
        return 0

    def serve(args, settings):
        calls["serve"].append((args, settings))
        return 0

    monkeypatch.setattr(launcher, "prepare_live_start", prepare)
    monkeypatch.setattr(preflight, "_main", check)
    monkeypatch.setattr(cli, "_cmd_serve", serve)
    return calls


@pytest.mark.parametrize("arguments", [[], ["--start"]])
def test_default_and_explicit_start_choose_before_preflight_and_serve(startup, arguments):
    assert cli.main(arguments) == 0
    assert startup["prepare"] == [{"preferred_target": None, "config_path": None}]
    assert startup["preflight"][0][1] == "live"
    args, settings = startup["serve"][0]
    assert args.audio == "pw:chosen-mic"
    assert args.approach == "live"
    assert settings.capture_target == "chosen-mic"


def test_audio_preselects_but_does_not_skip_setup(startup):
    assert cli.main(["--audio", "pw:preferred-mic"]) == 0
    assert startup["prepare"][0]["preferred_target"] == "preferred-mic"
    assert startup["serve"][0][0].audio == "pw:chosen-mic"


def test_config_path_reaches_bootstrap(startup, tmp_path):
    path = tmp_path / "deployment.yaml"
    assert cli.main(["--start", "--config", str(path)]) == 0
    assert startup["prepare"][0]["config_path"] == path


def test_host_port_and_session_are_checked_as_they_will_be_served(startup):
    assert cli.main(
        ["--host", "127.0.0.1", "--port", "8123", "--session-id", "lecture"]
    ) == 0
    settings, _ = startup["preflight"][0]
    assert settings.host == "127.0.0.1"
    assert settings.port == 8123
    assert settings.session_id == "lecture"
    assert startup["serve"][0][1] is settings


def test_preflight_failure_does_not_open_the_server(startup, monkeypatch):
    monkeypatch.setattr(preflight, "_main", lambda settings, **kwargs: 2)
    assert cli.main([]) == 2
    assert startup["prepare"]
    assert not startup["serve"]


def test_explicit_serve_skips_interactive_setup(startup):
    assert cli.main(["--serve", "--approach", "sim"]) == 0
    assert not startup["prepare"]
    assert not startup["preflight"]
    assert startup["serve"][0][0].approach == "sim"


def test_diagnostics_do_not_select_a_microphone(startup):
    assert cli.main(["--preflight", "--approach", "live-sim"]) == 0
    assert not startup["prepare"]
    assert not startup["serve"]


@pytest.mark.parametrize("error", [KeyboardInterrupt(), EOFError()])
def test_cancel_is_handled_without_traceback(startup, monkeypatch, capsys, error):
    def cancel(*args, **kwargs):
        raise error

    monkeypatch.setattr(launcher, "prepare_live_start", cancel)
    assert cli.main([]) == 130
    assert "cancelled" in capsys.readouterr().err
    assert not startup["serve"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--approach", "sim"],
        ["--audio", "synth"],
        ["--audio", "file:lecture.wav@1"],
        ["--audio", "pw:"],
        ["--cues", "reference.srt"],
        ["--once"],
        ["--speed", "2"],
        ["--port", "0"],
        ["--port", "65536"],
    ],
)
def test_invalid_startup_arguments_fail_before_setup(startup, arguments):
    assert cli.main(arguments) == 2
    assert not startup["prepare"]
    assert not startup["serve"]


def test_root_startup_is_rejected_before_capture(startup, monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert cli.main([]) == 2
    assert "not with sudo" in capsys.readouterr().err
    assert not startup["prepare"]


def test_missing_capture_tools_are_reported_before_setup(startup, monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert cli.main([]) == 2
    assert "pw-record, pw-dump" in capsys.readouterr().err
    assert not startup["prepare"]


def test_non_linux_startup_is_rejected_before_capture(startup, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    assert cli.main([]) == 2
    assert "requires Linux" in capsys.readouterr().err
    assert not startup["prepare"]


def test_start_and_serve_cannot_be_combined():
    with pytest.raises(SystemExit) as exc:
        cli.main(["--start", "--serve"])
    assert exc.value.code == 2
