"""Conservative, exact-device ALSA Capture gain control (never digital volume)."""

from __future__ import annotations

import json
import math
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass


class GainUnavailable(RuntimeError):
    """The selected microphone cannot safely use hardware gain adjustment."""


class _CommandNotStarted(GainUnavailable):
    """Cancellation/deadline prevented issuing a command."""


@dataclass(frozen=True)
class GainSetting:
    values: tuple[int, ...]
    minimum: int
    maximum: int
    label: str

    def describe(self) -> str:
        return f"{self.label}: raw {','.join(map(str, self.values))} ({self.minimum}-{self.maximum})"


@dataclass(frozen=True)
class GainAdjustment:
    before: GainSetting
    after: GainSetting


def _run(
    args: list[str], *, timeout: float = 3,
    deadline: float | None = None, stop_event: threading.Event | None = None,
) -> str:
    if stop_event is not None and stop_event.is_set():
        raise _CommandNotStarted("Hardware gain operation cancelled; no further changes permitted.")
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise _CommandNotStarted("Hardware gain operation deadline expired.")
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise GainUnavailable(f"{args[0]} is missing; install it to adjust ALSA Capture gain.") from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise GainUnavailable(f"{args[0]} failed or timed out; check microphone access: {exc}") from exc
    if result.returncode:
        raise GainUnavailable(
            f"{args[0]} failed (exit {result.returncode}); check device permissions: "
            f"{result.stderr.strip() or 'no diagnostic output'}"
        )
    return result.stdout


def _card_for_target(
    target: str, *, deadline: float | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    if not isinstance(target, str) or not target or "monitor" in target.lower():
        raise GainUnavailable("Select an exact non-monitor ALSA microphone node.name.")
    try:
        nodes = json.loads(_run(["pw-dump"], timeout=5, deadline=deadline, stop_event=stop_event))
        if not isinstance(nodes, list):
            raise TypeError("expected a node list")
        matches = []
        sources = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            info = node.get("info")
            props = info.get("props", {}) if isinstance(info, dict) else {}
            if isinstance(props, dict) and props.get("node.name") == target:
                if node.get("type") != "PipeWire:Interface:Node":
                    raise GainUnavailable("Selected microphone is not a PipeWire Node interface.")
                matches.append(props)
            if (
                node.get("type") == "PipeWire:Interface:Node"
                and isinstance(props, dict)
                and props.get("media.class") == "Audio/Source"
            ):
                sources.append(props)
    except (ValueError, TypeError) as exc:
        raise GainUnavailable(f"Cannot parse pw-dump microphone identity: {exc}") from exc
    if len(matches) != 1:
        raise GainUnavailable(f"Microphone {target!r} is missing or ambiguous in pw-dump.")
    props = matches[0]
    if props.get("media.class") != "Audio/Source" or props.get("device.api") not in (None, "alsa"):
        raise GainUnavailable(f"{target!r} is not an ALSA Audio/Source microphone.")
    if props.get("stream.capture.sink") in (True, "true") or props.get("device.class") == "monitor":
        raise GainUnavailable("Monitor sources cannot use hardware Capture gain.")
    card = props.get("alsa.card")
    if isinstance(card, bool) or not isinstance(card, (str, int)) or not re.fullmatch(r"[0-9]+", str(card)):
        raise GainUnavailable(f"{target!r} has no valid numeric alsa.card; no default-device fallback.")
    same_card = [source for source in sources
                 if not isinstance(source.get("alsa.card"), bool)
                 and re.fullmatch(r"[0-9]+", str(source.get("alsa.card")))
                 and int(source["alsa.card"]) == int(card)]
    if len(same_card) != 1:
        raise GainUnavailable("Multiple Audio/Source nodes share this ALSA card; Capture ownership is ambiguous.")
    return int(card)


def _parse_capture(output: str, card: int) -> GainSetting:
    headers = re.findall(r"^Simple mixer control '([^']+)',(\d+)\s*$", output, re.MULTILINE)
    if headers != [("Capture", "0")]:
        raise GainUnavailable("Expected only ALSA simple 'Capture',0; unsupported mixer response.")
    capabilities = re.search(r"^\s*Capabilities:\s*(.*?)\s*$", output, re.MULTILINE)
    if not capabilities or "cvolume" not in capabilities.group(1).split():
        raise GainUnavailable("ALSA Capture has no supported capture-volume control.")
    if any(word in capabilities.group(1).split() for word in ("pvolume", "pswitch")):
        raise GainUnavailable("Combined playback/Capture controls are not safe to adjust.")
    limits = re.findall(r"^\s*Limits: Capture (\d+) - (\d+)\s*$", output, re.MULTILINE)
    if len(limits) != 1:
        raise GainUnavailable("ALSA Capture limits are missing or unparseable.")
    minimum, maximum = map(int, limits[0])
    if maximum <= minimum:
        raise GainUnavailable("ALSA Capture has unsupported raw limits.")
    channel_line = re.search(r"^\s*Capture channels:\s*(.*?)\s*$", output, re.MULTILINE)
    if not channel_line or channel_line.group(1) not in ("Mono", "Front Left - Front Right"):
        raise GainUnavailable("ALSA Capture supports only known mono/stereo channel layouts.")
    channels = ("Mono",) if channel_line.group(1) == "Mono" else ("Front Left", "Front Right")
    rows = re.findall(r"^[ \t]*([^:\n]+): Capture (.*?)[ \t]*$", output, re.MULTILINE)
    rows = [(name, row) for name, row in rows if name != "Limits"]
    if tuple(name for name, _ in rows) != channels:
        raise GainUnavailable("ALSA Capture channel values are missing or unsupported.")
    values = []
    for name, row in rows:
        match = re.fullmatch(r"(\d+) \[\d+%\](?: \[-?\d+(?:\.\d+)?dB\])?(?: \[(on|off)\])?", row)
        if not match or match.group(2) == "off":
            raise GainUnavailable(f"ALSA Capture {name} is muted or unparseable; adjust it manually.")
        if "cswitch" in capabilities.group(1).split() and match.group(2) != "on":
            raise GainUnavailable("ALSA Capture switch state is unknown; refusing blind changes.")
        value = int(match.group(1))
        if not minimum <= value <= maximum:
            raise GainUnavailable("ALSA Capture value is outside its raw limits.")
        values.append(value)
    return GainSetting(tuple(values), minimum, maximum, f"ALSA card {card} Capture")


class AlsaCaptureGain:
    def __init__(self, target: str, card: int):
        if isinstance(card, bool) or not isinstance(card, int) or card < 0:
            raise GainUnavailable("ALSA card must be a non-negative integer.")
        self.target = target
        self.card = card
        self._last_applied: GainSetting | None = None
        self._deadline: float | None = None
        self._stop_event: threading.Event | None = None
        self._operation_depth = 0
        self._cleaning = False

    def set_operation_limits(
        self, *, deadline: float | None = None, stop_event: threading.Event | None = None,
    ) -> None:
        """Set an absolute time.monotonic budget and cooperative cancellation flag."""
        self._deadline = deadline
        self._stop_event = stop_event

    @contextmanager
    def _operation(self):
        old_deadline = self._deadline
        if self._operation_depth == 0:
            self._deadline = min(old_deadline, time.monotonic() + 15) if old_deadline is not None else time.monotonic() + 15
        self._operation_depth += 1
        try:
            yield
        finally:
            self._operation_depth -= 1
            self._deadline = old_deadline

    @contextmanager
    def _cleanup(self):
        # Shutdown must join this bounded cleanup too. Nested recovery cannot renew its budget.
        if self._cleaning:
            yield
            return
        deadline, stop_event = self._deadline, self._stop_event
        self._deadline, self._stop_event = time.monotonic() + 3, None
        self._cleaning = True
        try:
            yield
        finally:
            self._deadline, self._stop_event = deadline, stop_event
            self._cleaning = False

    def _run(self, args: list[str]) -> str:
        return _run(args, deadline=self._deadline, stop_event=self._stop_event)

    def _identity(self) -> None:
        if _card_for_target(self.target, deadline=self._deadline, stop_event=self._stop_event) != self.card:
            raise GainUnavailable("Microphone ALSA card changed; refusing to modify another device.")

    def read(self) -> GainSetting:
        with self._operation():
            self._identity()
            return _parse_capture(self._run(["amixer", "-c", str(self.card), "sget", "Capture"]), self.card)

    def _set(self, setting: GainSetting) -> None:
        try:
            self._identity()
        except GainUnavailable as exc:
            raise _CommandNotStarted(str(exc)) from exc
        # Bare integer values are ALSA raw values, not percentages or dB. No switch command.
        self._run(["amixer", "-c", str(self.card), "sset", "Capture", ",".join(map(str, setting.values))])

    def _apply(self, before: GainSetting, desired: GainSetting) -> None:
        if self.read() != before:
            raise GainUnavailable("Capture gain changed externally; refusing to overwrite it.")
        setter_completed = False
        try:
            self._set(desired)
            setter_completed = True
            actual = self.read()
            if actual != desired:
                raise GainUnavailable("ALSA Capture readback differs from requested gain; adjustment failed.")
        except GainUnavailable as exc:
            if isinstance(exc, _CommandNotStarted) and not setter_completed:
                raise
            # Only undo our exact requested setting. Unknown/partial/external changes stay untouched.
            try:
                with self._cleanup():
                    actual = self.read()
                    if actual == desired:
                        if self.read() != desired:
                            raise GainUnavailable("Capture changed externally during rollback.")
                        self._set(before)
                        if self.read() != before:
                            raise GainUnavailable("Original Capture gain could not be verified after rollback.")
                    elif actual != before:
                        raise GainUnavailable("Unexpected Capture gain; unsafe to restore automatically.")
            except GainUnavailable as rollback:
                raise GainUnavailable(f"{exc} Recovery not verified: {rollback}") from exc
            raise GainUnavailable(f"{exc} Original Capture gain verified.") from exc
        self._last_applied = desired

    def reduce(self, *, expected: GainSetting | None = None) -> GainAdjustment | None:
        with self._operation():
            return self._reduce(expected=expected)

    def _reduce(self, *, expected: GainSetting | None) -> GainAdjustment | None:
        before = self.read()
        if expected is not None and before != expected:
            raise GainUnavailable("Capture gain changed externally from expected setting; refusing mutation.")
        if self._last_applied is not None and before != self._last_applied:
            raise GainUnavailable("Capture gain changed externally; stop calibration and check it manually.")
        step = max(1, math.ceil((before.maximum - before.minimum) * 0.12))
        values = tuple(max(before.minimum, value - step) for value in before.values)
        if values == before.values:
            return None
        after = GainSetting(values, before.minimum, before.maximum, before.label)
        self._apply(before, after)
        return GainAdjustment(before, after)

    def restore(self, setting: GainSetting) -> None:
        with self._cleanup(), self._operation():
            self._restore(setting)

    def _restore(self, setting: GainSetting) -> None:
        current = self.read()
        if self._last_applied is None or current != self._last_applied:
            raise GainUnavailable("No matching last-applied Capture gain; refusing to overwrite an external change.")
        if (
            setting.label != current.label
            or (setting.minimum, setting.maximum) != (current.minimum, current.maximum)
            or len(setting.values) != len(current.values)
            or any(type(value) is not int or not setting.minimum <= value <= setting.maximum for value in setting.values)
        ):
            raise GainUnavailable("Restore setting does not match this Capture control.")
        self._apply(current, setting)


def discover_gain_control(
    target: str, *, deadline: float | None = None,
    stop_event: threading.Event | None = None,
) -> AlsaCaptureGain:
    budget = min(deadline, time.monotonic() + 15) if deadline is not None else time.monotonic() + 15
    control = AlsaCaptureGain(target, _card_for_target(target, deadline=budget, stop_event=stop_event))
    control.set_operation_limits(deadline=budget, stop_event=stop_event)
    control.read()
    control.set_operation_limits(deadline=deadline, stop_event=stop_event)
    return control
