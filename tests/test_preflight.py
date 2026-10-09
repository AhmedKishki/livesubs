"""Tests for the pre-flight checks.

Two of these checks exist because their failure is indistinguishable from something
else, and both were wrong before: a default-deny firewall refuses a phone in exactly the
way WiFi client isolation does, and a check that reports the wrong verdict is worse than
no check at all, because it either sends someone chasing a problem they do not have or,
more expensively, reassures them about one they do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from livesubs import preflight
from livesubs.audio.calibration import CalibrationError, CalibrationNotice, CaptureMeasurement
from livesubs.config import Settings

# --- the local firewall, which mimics client isolation ----------------------------


def test_a_disabled_ufw_is_not_a_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ufw's *service* is routinely active while doing nothing, so the config decides."""

    conf = tmp_path / "ufw.conf"
    conf.write_text("ENABLED=no\nLOGLEVEL=low\n", encoding="utf-8")
    monkeypatch.setattr(preflight, "_service_active", lambda name: False)

    ok, detail = preflight._firewall_verdict(8000, ufw_conf=conf)

    assert ok is True
    assert "nothing local" in detail


def test_an_enabled_ufw_is_reported_with_the_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = tmp_path / "ufw.conf"
    conf.write_text("ENABLED=yes\n", encoding="utf-8")
    monkeypatch.setattr(preflight, "_service_active", lambda name: False)

    ok, detail = preflight._firewall_verdict(8000, ufw_conf=conf)

    assert ok is False
    assert "ufw allow 8000/tcp" in detail


def test_a_running_firewalld_is_reported_with_the_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight, "_service_active", lambda name: name == "firewalld")

    ok, detail = preflight._firewall_verdict(9000, ufw_conf=tmp_path / "absent")

    assert ok is False
    assert "firewall-cmd --add-port=9000/tcp" in detail


def test_an_unreadable_firewall_config_is_unknown_not_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never report the port as clear when the state could not be determined."""

    unreadable = tmp_path / "ufw.conf"
    unreadable.mkdir()                       # read_text on a directory raises OSError
    monkeypatch.setattr(preflight, "_service_active", lambda name: False)

    ok, detail = preflight._firewall_verdict(8000, ufw_conf=unreadable)

    assert ok is None
    assert "could not be read" in detail


# --- client isolation -------------------------------------------------------------


def stage_network(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ip: str = "192.168.110.190",
    gateway: str = "192.168.110.1",
    peers: tuple[str, ...] = (),
    reachable: tuple[str, ...] = (),
) -> None:
    monkeypatch.setattr(preflight, "lan_ip", lambda: ip)
    monkeypatch.setattr(preflight, "_default_gateway", lambda: gateway)
    monkeypatch.setattr(preflight, "_neighbours", lambda: list(peers))
    monkeypatch.setattr(
        preflight, "_reaches", lambda host, timeout=2.0: host in reachable
    )


def test_reachable_peers_show_that_client_to_client_traffic_flows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the whole value of the check: it answers without a phone."""

    stage_network(
        monkeypatch, peers=("10.0.0.5", "10.0.0.6"), reachable=("10.0.0.5",)
    )

    ok, detail = preflight._client_isolation_verdict()

    assert ok is True
    assert "1 of 2" in detail


def test_silent_peers_are_inconclusive_rather_than_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not proof of isolation: a peer may be wired, asleep, or behind its own firewall."""

    stage_network(monkeypatch, peers=("10.0.0.5", "10.0.0.6"))

    ok, detail = preflight._client_isolation_verdict()

    assert ok is None
    assert "one phone" in detail


def test_no_known_peers_is_inconclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    stage_network(monkeypatch)

    ok, detail = preflight._client_isolation_verdict()

    assert ok is None
    assert "one phone" in detail


def test_the_gateway_is_not_counted_as_a_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The router answers even with isolation on, so reaching it proves nothing."""

    stage_network(
        monkeypatch,
        ip="10.0.0.9",
        gateway="10.0.0.1",
        peers=("10.0.0.1", "10.0.0.9"),
    )

    ok, detail = preflight._client_isolation_verdict()

    assert ok is None
    assert "no other hosts known" in detail


def test_the_number_of_hosts_probed_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-flight must not crawl: a couple of seconds per host is the whole budget."""

    stage_network(monkeypatch, peers=tuple(f"10.0.0.{n}" for n in range(2, 40)))

    probed: list[str] = []

    def record(host: str, timeout: float = 2.0) -> bool:
        probed.append(host)
        return False

    monkeypatch.setattr(preflight, "_reaches", record)

    preflight._client_isolation_verdict()

    assert len(probed) == 6


def test_an_absent_lan_address_is_reported_rather_than_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight, "lan_ip", lambda: None)

    ok, detail = preflight._client_isolation_verdict()

    assert ok is None
    assert "no LAN address" in detail


# --- parsing, which decides what gets probed --------------------------------------


def test_neighbours_skips_unresolved_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """An INCOMPLETE or FAILED entry is not a host, so it must not be pinged."""

    output = (
        "10.0.0.1 dev wlan0 lladdr 00:11:22:33:44:55 REACHABLE\n"
        "10.0.0.2 dev wlan0 lladdr aa:bb:cc:dd:ee:ff STALE\n"
        "10.0.0.3 dev wlan0  INCOMPLETE\n"
        "10.0.0.4 dev wlan0 lladdr 11:22:33:44:55:66 FAILED\n"
    )
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, output, ""),
    )

    assert preflight._neighbours() == ["10.0.0.1", "10.0.0.2"]


@pytest.mark.parametrize(
    "line,expected",
    [
        ("default via 10.0.0.1 dev wlp2s0 proto dhcp src 10.0.0.9 metric 600", "10.0.0.1"),
        ("default dev ppp0 scope link", None),
        ("", None),
    ],
)
def test_the_default_gateway_is_read_from_the_route(
    monkeypatch: pytest.MonkeyPatch, line: str, expected: str | None
) -> None:
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, line, ""),
    )

    assert preflight._default_gateway() == expected


def test_the_default_interface_is_read_from_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    line = "default via 10.0.0.1 dev wlp2s0 proto dhcp src 10.0.0.9 metric 600"
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, line, ""),
    )

    assert preflight._default_interface() == "wlp2s0"


def test_missing_system_tools_degrade_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ip` or `ping` may not exist on a given machine, and pre-flight must survive."""

    def missing(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("no such command")

    monkeypatch.setattr(preflight.subprocess, "run", missing)

    assert preflight._neighbours() == []
    assert preflight._default_gateway() is None
    assert preflight._default_interface() is None
    assert preflight._reaches("10.0.0.1") is False


@pytest.fixture
def capture_checks(monkeypatch):
    monkeypatch.setattr(preflight, "pipewire_sources", lambda: [("mic", "Physical microphone")])
    monkeypatch.setattr(preflight, "lan_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(preflight, "_port_free", lambda *args: True)
    monkeypatch.setattr(preflight, "_firewall_verdict", lambda port: (True, "test"))
    monkeypatch.setattr(preflight, "_default_interface", lambda: "eth0")
    return Settings(capture={"target": "mic"})


def test_capture_failure_is_fatal_not_a_warning(capture_checks, monkeypatch):
    def fail(*args, **kwargs):
        raise CalibrationError("CAPTURE_NO_SIGNAL: muted microphone")

    monkeypatch.setattr(preflight, "calibrate_capture", fail)
    checks = preflight.run_preflight(capture_checks, require_credentials=False)
    signal = next(c for c in checks if c.label == "capture signal")
    assert signal.ok is False and "CAPTURE_NO_SIGNAL" in signal.detail


def test_empty_source_list_cannot_pass_preflight(capture_checks, monkeypatch):
    monkeypatch.setattr(preflight, "pipewire_sources", list)
    checks = preflight.run_preflight(capture_checks, require_credentials=False)
    assert any(c.label == "capture target" and c.ok is False for c in checks)


def test_preflight_uses_resolved_calibration_config(capture_checks, monkeypatch):
    capture_checks.capture.auto_calibrate = False
    capture_checks.capture.runtime_calibration = False

    def measured(target, rate, *, config, report):
        assert target == "mic" and rate == 16000
        assert not config.enabled and not config.runtime
        return CaptureMeasurement(48000, -20, -8, 0, -100)

    monkeypatch.setattr(preflight, "calibrate_capture", measured)
    checks = preflight.run_preflight(capture_checks, require_credentials=False)
    assert next(c for c in checks if c.label == "clipping").ok is True


def test_preflight_explains_calibration_before_finishing(capture_checks, monkeypatch, capsys):
    def measured(*args, report, **kwargs):
        report(CalibrationNotice("calibrating", "play the sample at steady volume"))
        report(CalibrationNotice("ready", "verified capture"))
        return CaptureMeasurement(48000, -20, -8, 0, -100)

    monkeypatch.setattr(preflight, "calibrate_capture", measured)
    assert preflight._main(capture_checks, approach_name="sim") == 0
    text = capsys.readouterr().out
    assert "[CALIBRATION CALIBRATING] play the sample" in text
    assert "[CALIBRATION READY] verified capture" in text


def test_disabled_auto_gain_does_not_hide_clipping(capture_checks, monkeypatch):
    capture_checks.capture.auto_calibrate = False
    monkeypatch.setattr(preflight, "calibrate_capture", lambda *a, **k:
                        CaptureMeasurement(48000, -3, 0, .1, -100))
    checks = preflight.run_preflight(capture_checks, require_credentials=False)
    assert next(c for c in checks if c.label == "clipping").ok is False
