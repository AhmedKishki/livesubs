"""M0 pre-flight checks.

Implements the checklist in ``docs/08-operations.md`` as something executable,
because the failure modes that matter are properties of the room and the network
rather than of the code, and none of them are visible in a unit test.

Two checks here can invalidate the whole design, which is why the tool exists:

* the pinned capture target must exist and actually carry signal - the system
  default source on this machine is a **muted** headset mic, so a misconfigured
  server looks broken rather than misconfigured;
* the LAN address used by the QR code must be reachable, which depends on WiFi
  client isolation being off.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from livesubs.audio.calibration import (
    CalibrationConfig,
    CalibrationNotice,
    calibrate_capture,
    measure_capture,
)
from livesubs.audio.pw_record import pipewire_sources
from livesubs.config import Settings

__all__ = ["Check", "lan_ip", "run_preflight"]

#: Below this RMS the capture is treated as having no signal worth trying.
#: A working room mic in a quiet hall sits around -60 to -25 dBFS.
SILENCE_DBFS = -55.0

#: Below this RMS the capture looks *digitally* silent, which means the target
#: is muted or dead rather than merely in a quiet room. The distinction matters:
#: "quiet room" is a warning to re-run while speaking, whereas "digital silence"
#: is the muted-default-mic trap described in docs/02-audio-capture.md.
DEAD_DBFS = -75.0

_RECORD_SECONDS = 3.0


@dataclass(slots=True)
class Check:
    """One pre-flight result."""

    label: str
    ok: bool | None          # None means "could not determine"
    detail: str

    def render(self) -> str:
        mark = "PASS" if self.ok else ("WARN" if self.ok is None else "FAIL")
        return f"[{mark}] {self.label:<20} {self.detail}"


def lan_ip() -> str | None:
    """Best-effort LAN address. Sends no traffic; the socket is never used.

    Used both by the pre-flight check and to print a scannable URL when the
    server binds to ``0.0.0.0``.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))      # TEST-NET-1, unroutable by design
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _measure_capture(
    target: str, sample_rate: int, seconds: float = _RECORD_SECONDS
) -> tuple[float, float]:
    """Compatibility helper backed by the bounded, correctly decoded sampler."""
    measurement = measure_capture(target, sample_rate, seconds)
    return measurement.rms_dbfs, measurement.clipping_fraction


def _service_active(name: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() == "active"


def _firewall_verdict(
    port: int,
    *,
    ufw_conf: Path | None = None,
) -> tuple[bool | None, str]:
    """Whether a local firewall is likely to refuse the viewer port.

    Worth checking before the manual step, because its failure is indistinguishable
    from the one being tested: a default-deny firewall refuses the phone in exactly the
    way WiFi client isolation does, so the operator concludes the network is at fault
    and buys a travel router for a problem that a single `ufw allow` would have fixed.

    ``ufw_conf`` is injectable so the check can be tested without depending on the
    machine it runs on.
    """

    findings: list[str] = []
    unreadable = False

    # ufw is routinely "active" as a *service* while doing nothing at all, so the
    # config file is authoritative and the service state is not.
    conf = ufw_conf if ufw_conf is not None else Path("/etc/ufw/ufw.conf")
    if conf.exists():
        try:
            if "ENABLED=yes" in conf.read_text(encoding="utf-8", errors="replace"):
                findings.append(f"ufw is enabled; run `sudo ufw allow {port}/tcp`")
        except OSError:
            unreadable = True

    if _service_active("firewalld"):
        findings.append(
            f"firewalld is running; run `sudo firewall-cmd --add-port={port}/tcp`"
        )

    if findings:
        return False, "; ".join(findings)
    if unreadable:
        return None, "the firewall state could not be read"
    return True, "nothing local should refuse the port"


def _neighbours() -> list[str]:
    """Hosts this machine already knows about, from its own neighbour table.

    Deliberately not a sweep: the entries are hosts that have already exchanged
    traffic with this machine, so confirming they are reachable touches nobody new.
    """

    try:
        result = subprocess.run(
            ["ip", "neigh", "show"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if result.returncode != 0:
        return []

    known: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[-1] in {"FAILED", "INCOMPLETE"}:
            continue
        known.append(parts[0])

    return known


def _default_gateway() -> str | None:
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    parts = result.stdout.split()
    if "via" in parts:
        return parts[parts.index("via") + 1]
    return None


def _default_interface() -> str | None:
    """The interface carrying default traffic, which is the one that matters here."""

    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    parts = result.stdout.split()
    if "dev" in parts:
        return parts[parts.index("dev") + 1]
    return None


def _reaches(host: str, timeout: float = 2.0) -> bool:
    """One ICMP echo. Returns False on any doubt, so a failure is never a false alarm."""

    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(timeout)), host],
            capture_output=True,
            text=True,
            timeout=timeout + 3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _client_isolation_verdict(limit: int = 6) -> tuple[bool | None, str]:
    """Whether this machine can reach other hosts on its own subnet.

    WiFi client isolation refuses traffic between two clients of the same access point,
    which is precisely the path an attendee's phone takes to this machine. Reaching
    another client from here tests that path from the inside, and needs no phone.

    A positive answer is strong but not proof: the peers that reply might be wired, and
    WiFi-to-wired traffic is normally forwarded even when isolation is on. A negative
    answer is not proof either, because a peer may be asleep or behind its own firewall.
    So this narrows the question rather than replacing it - and a negative result is
    what should send someone to find a phone.
    """

    ip = lan_ip()
    if ip is None:
        return None, "no LAN address; check the network first"

    gateway = _default_gateway()
    peers = [host for host in _neighbours() if host != ip and host != gateway][:limit]

    if not peers:
        return None, "no other hosts known on this subnet; confirm with one phone"

    reachable = [host for host in peers if _reaches(host)]
    if reachable:
        return True, (
            f"{len(reachable)} of {len(peers)} other hosts here answered, so this network "
            "forwards client-to-client traffic"
        )

    return None, (
        f"none of {len(peers)} known hosts answered; they may be wired, asleep or "
        "firewalled, so confirm with one phone"
    )


def run_preflight(
    settings: Settings,
    *,
    record_seconds: float = _RECORD_SECONDS,
    require_credentials: bool = True,
    roles: tuple[str, ...] | None = None,
    calibration_report: Callable[[CalibrationNotice], None] | None = None,
) -> list[Check]:
    """Run every check that can be run without API keys."""

    checks: list[Check] = []
    target_found = False

    try:
        sources = pipewire_sources()
        names = {name for name, _ in sources}
    except Exception as exc:                                  # noqa: BLE001
        sources = []
        names = set()
        checks.append(Check("capture target", None, f"could not query PipeWire: {exc}"))

    if names and not settings.capture_target:
        # Not an error: it just has not been chosen yet. On a fresh machine this
        # check is the discovery tool, so print the candidates to paste in.
        listed = "\n".join(
            f"           {name}   ({description})" for name, description in sorted(sources)
        )
        checks.append(
            Check(
                "capture target",
                None,
                "not configured. Set LIVESUBS_CAPTURE_TARGET to one of:\n" + listed,
            )
        )
    elif names:
        if settings.capture_target in names:
            target_found = True
            checks.append(
                Check("capture target", True, f"found: {dict(sources)[settings.capture_target]}")
            )

        else:
            available = ", ".join(sorted(names)) or "none"
            checks.append(
                Check(
                    "capture target",
                    False,
                    f"NOT FOUND: {settings.capture_target}\n"
                    f"           available: {available}",
                )
            )

    if not names:
        checks.append(Check("capture target", False, "no microphone source found; check PipeWire"))

    if target_found:
        try:
            measurement = calibrate_capture(
                settings.capture_target, settings.sample_rate,
                config=CalibrationConfig(
                    enabled=settings.capture.auto_calibrate,
                    runtime=settings.capture.runtime_calibration,
                    measure_seconds=record_seconds,
                ),
                report=calibration_report,
            )
            dbfs, clipping = measurement.rms_dbfs, measurement.clipping_fraction
            if dbfs <= DEAD_DBFS:
                verdict: bool | None = False
                note = "  <-- DIGITAL SILENCE: the target is muted or dead"
            elif dbfs <= SILENCE_DBFS:
                verdict = None
                note = (
                    f"  <-- quiet: no speech during the {record_seconds:.0f}s check. "
                    "Re-run while talking."
                )
            else:
                verdict = True
                note = ""
            checks.append(
                Check("capture signal", verdict, f"{dbfs:.1f} dBFS{note}")
            )
            clipped = clipping >= 0.001
            checks.append(
                Check(
                    "clipping",
                    not clipped,
                    f"{clipping * 100:.3f}% of samples at full scale"
                    + ("  <-- clipping: lower the source level" if clipped else ""),
                )
            )
        except Exception as exc:                              # noqa: BLE001
            checks.append(Check("capture signal", False, str(exc)))
    else:
        checks.append(Check("capture signal", None, "skipped: no valid target"))
        checks.append(Check("clipping", None, "skipped: no valid target"))

    ip = lan_ip()
    checks.append(
        Check(
            "LAN address",
            bool(ip),
            (
                f"{ip} - QR code points at http://{ip}:{settings.port}"
                if ip
                else "no LAN address; the QR code has nothing to point at"
            ),
        )
    )

    free = _port_free(settings.host, settings.port)
    checks.append(
        Check(
            "port",
            free,
            (
                f"{settings.host}:{settings.port} available"
                if free
                else f"{settings.host}:{settings.port} already in use"
            ),
        )
    )

    verdict, detail = _firewall_verdict(settings.port)
    checks.append(Check("local firewall", verdict, detail))

    # Only meaningful while this machine is itself a wireless client. Otherwise the
    # isolation question belongs to the attendees' network and nothing here answers it.
    # `wl*` is the systemd/udev naming convention for WLAN interfaces.
    interface = _default_interface()
    if interface is not None and interface.startswith("wl"):
        verdict, detail = _client_isolation_verdict()
    else:
        verdict, detail = None, (
            f"this machine is not on WiFi ({interface or 'no default route'}); "
            "confirm from one phone"
        )
    checks.append(Check("client isolation", verdict, detail))

    problems = settings.validate_for_startup(
        require_credentials=require_credentials, roles=roles
    )

    detail = "; ".join(problems) if problems else "ok"
    if not problems and not require_credentials:
        detail = "ok (the selected approach needs no API keys)"

    checks.append(Check("configuration", not problems, detail))

    return checks


def _main(settings: Settings, *, approach_name: str | None = None) -> int:
    """Run every check. ``approach_name`` decides whether API keys are required.

    Without it, keys are treated as required, which is the safe default for a caller
    that has not said which approach it is checking.
    """

    from livesubs.approaches import required_roles, requires_credentials

    needs_keys = requires_credentials(approach_name) if approach_name else True
    roles = required_roles(approach_name) if approach_name and needs_keys else None
    print("livesubs pre-flight\n", flush=True)
    checks = run_preflight(
        settings, require_credentials=needs_keys, roles=roles,
        calibration_report=lambda notice: print(
            f"[CALIBRATION {notice.state.upper()}] {notice.message}", flush=True
        ),
    )
    for check in checks:
        print(check.render())

    print(
        "\nMANUAL checks this tool cannot perform:\n"
        "  * The definitive isolation test: load the landing page on ONE phone with\n"
        "    mobile data OFF. Two phones are only needed if this machine is on Ethernet,\n"
        "    because a phone reaching a WiFi server is already the client-to-client path\n"
        "    that isolation blocks. If it fails, no attendee can reach the server and\n"
        "    you need the travel-router fallback (docs/07-delivery-qr.md). This is the\n"
        "    check most likely to invalidate the design.\n"
        "  * Microphone aimed at the presenter.\n"
        "  * Mains power connected and suspend disabled."
    )

    return 0 if all(check.ok is not False for check in checks) else 2


if __name__ == "__main__":                                    # pragma: no cover
    from livesubs.config import get_settings

    sys.exit(_main(get_settings()))
