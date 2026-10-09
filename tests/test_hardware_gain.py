import json
import subprocess
import threading
import time

import pytest

from livesubs.audio.hardware_gain import GainUnavailable, discover_gain_control

TARGET = "alsa_input.laptop"


def mixer(values=(47, 43), minimum=0, maximum=63, switch="on"):
    channels = ("Mono",) if len(values) == 1 else ("Front Left", "Front Right")
    return (
        "Simple mixer control 'Capture',0\n"
        "  Capabilities: cvolume cswitch\n"
        f"  Capture channels: {' - '.join(channels)}\n"
        f"  Limits: Capture {minimum} - {maximum}\n"
        + "\n".join(f"  {name}: Capture {value} [75%] [18.00dB] [{switch}]"
                    for name, value in zip(channels, values))
    )


class FakeCommands:
    def __init__(self):
        self.props = {"node.name": TARGET, "media.class": "Audio/Source", "alsa.card": "1", "device.api": "alsa"}
        self.nodes = [{"type": "PipeWire:Interface:Node", "info": {"props": self.props}},
                      {"type": "PipeWire:Interface:Node", "info": {"props": {
            "node.name": "alsa_input.other", "media.class": "Audio/Source", "alsa.card": "0"}}}]
        self.values = (47, 43)
        self.calls = []
        self.output = None
        self.error = None
        self.ineffective = False
        self.fail_set = False

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        assert "shell" not in kwargs
        assert kwargs["timeout"] <= (5 if args[0] == "pw-dump" else 3)
        if args[0] == "pw-dump":
            return subprocess.CompletedProcess(args, 0, json.dumps(self.nodes), "")
        assert args[:3] == ["amixer", "-c", "1"]
        if self.error:
            raise self.error
        if args[3] == "sset":
            assert args[4] == "Capture"
            if not self.ineffective:
                self.values = tuple(map(int, args[5].split(",")))
            if self.fail_set:
                self.fail_set = False
                return subprocess.CompletedProcess(args, 1, "", "permission denied")
        return subprocess.CompletedProcess(args, 0, self.output or mixer(self.values), "")


@pytest.fixture
def commands(monkeypatch):
    fake = FakeCommands()
    monkeypatch.setattr(subprocess, "run", fake.run)
    return fake


def test_exact_card_reduce_restore_preserves_imbalance(commands):
    control = discover_gain_control(TARGET)
    original = control.read()
    adjustment = control.reduce()
    assert adjustment.before == original
    assert adjustment.after.values == (39, 35)
    assert "ALSA card 1 Capture" in original.describe()
    control.reduce()
    control.restore(original)
    assert commands.values == (47, 43)
    assert all(call[0][4] == "Capture" for call in commands.calls if call[0][0] == "amixer")


@pytest.mark.parametrize("target", ["default", "alsa_input.absent", "alsa_input.monitor", ""])
def test_unsupported_target(commands, target):
    with pytest.raises(GainUnavailable):
        discover_gain_control(target)
    assert not any(args[0] == "amixer" for args, _ in commands.calls)


@pytest.mark.parametrize("key,value", [
    ("media.class", "Audio/Sink"), ("device.api", "bluez5"), ("alsa.card", None),
    ("alsa.card", "1;evil"), ("alsa.card", True), ("alsa.card", -1),
    ("stream.capture.sink", True), ("device.class", "monitor"),
])
def test_invalid_identity(commands, key, value):
    commands.props[key] = value
    with pytest.raises(GainUnavailable):
        discover_gain_control(TARGET)


def test_ambiguous(commands):
    commands.nodes.append({"type": "PipeWire:Interface:Node", "info": {"props": dict(commands.props)}})
    with pytest.raises(GainUnavailable, match="ambiguous"):
        discover_gain_control(TARGET)


@pytest.mark.parametrize("output", [
    mixer(switch="off"), mixer().replace("Capture',0", "Mic Boost',0"),
    mixer().replace("cvolume cswitch", "pvolume pswitch"),
    mixer().replace("cvolume cswitch", "cvolume pvolume cswitch"),
    mixer().replace("Limits: Capture", "Limits: Playback"),
    mixer().replace("Front Left", "Rear Left"), mixer().replace("47 [75%]", "oops [75%]"),
    mixer().replace(" [on]", ""), mixer(values=(99, 43)), mixer(maximum=0),
])
def test_refuse_unsafe_control_syntax(commands, output):
    commands.output = output
    with pytest.raises(GainUnavailable):
        discover_gain_control(TARGET)


def test_mono_and_floor(commands):
    commands.values = (3,)
    control = discover_gain_control(TARGET)
    assert control.reduce().after.values == (0,)
    count = len(commands.calls)
    assert control.reduce() is None
    assert not any(args[3] == "sset" for args, _ in commands.calls[count:] if args[0] == "amixer")


def test_stereo_one_channel_at_floor(commands):
    commands.values = (0, 9)
    assert discover_gain_control(TARGET).reduce().after.values == (0, 1)


@pytest.mark.parametrize("error", [FileNotFoundError("amixer"), subprocess.TimeoutExpired("amixer", 3), PermissionError("denied")])
def test_command_errors(commands, error):
    commands.error = error
    with pytest.raises(GainUnavailable):
        discover_gain_control(TARGET)


def test_ineffective_readback(commands):
    control = discover_gain_control(TARGET)
    commands.ineffective = True
    with pytest.raises(GainUnavailable, match="readback"):
        control.reduce()
    assert commands.values == (47, 43)


def test_setter_failure_restores_when_safe(commands):
    control = discover_gain_control(TARGET)
    commands.fail_set = True
    with pytest.raises(GainUnavailable, match="permission denied"):
        control.reduce()
    assert commands.values == (47, 43)


def test_external_change_not_overwritten(commands):
    control = discover_gain_control(TARGET)
    original = control.read()
    control.reduce()
    commands.values = (20, 20)
    with pytest.raises(GainUnavailable, match="external change"):
        control.restore(original)
    with pytest.raises(GainUnavailable, match="externally"):
        control.reduce()
    assert commands.values == (20, 20)


def test_identity_changed_before_mutation(commands):
    control = discover_gain_control(TARGET)
    commands.props["alsa.card"] = "2"
    with pytest.raises(GainUnavailable, match="card changed"):
        control.reduce()
    assert not any(args[3] == "sset" for args, _ in commands.calls if args[0] == "amixer")


def test_unexpected_readback_not_blindly_restored(commands, monkeypatch):
    control = discover_gain_control(TARGET)
    run = commands.run

    def external_change(args, **kwargs):
        result = run(args, **kwargs)
        if args[0] == "amixer" and args[3] == "sset":
            commands.values = (22, 23)
        return result

    monkeypatch.setattr(subprocess, "run", external_change)
    with pytest.raises(GainUnavailable, match="unsafe to restore"):
        control.reduce()
    assert commands.values == (22, 23)
    assert sum(args[0] == "amixer" and args[3] == "sset" for args, _ in commands.calls) == 1


def test_change_between_compare_reads_not_overwritten(commands, monkeypatch):
    control = discover_gain_control(TARGET)
    read = control.read
    count = 0

    def changed_read():
        nonlocal count
        count += 1
        if count == 2:
            commands.values = (22, 23)
        return read()

    monkeypatch.setattr(control, "read", changed_read)
    with pytest.raises(GainUnavailable, match="externally"):
        control.reduce()
    assert commands.values == (22, 23)
    assert not any(args[3] == "sset" for args, _ in commands.calls if args[0] == "amixer")


@pytest.mark.parametrize("stdout", ["invalid json", "{}"])
def test_bad_pw_dump(commands, monkeypatch, stdout):
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 0, stdout, ""))
    with pytest.raises(GainUnavailable, match="parse pw-dump"):
        discover_gain_control(TARGET)


def test_pw_dump_timeout(commands, monkeypatch):
    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(GainUnavailable, match="timed out"):
        discover_gain_control(TARGET)


def test_volume_only_no_db_parser_and_minimum_step(commands, monkeypatch):
    commands.output = mixer((6,), minimum=5, maximum=9).replace(
        "cvolume cswitch", "cvolume"
    ).replace(" [18.00dB] [on]", "")
    control = discover_gain_control(TARGET)
    assert control.read().values == (6,)
    commands.output = None
    # Keep a narrow-range device response for both read and set readback.
    run = commands.run

    def narrow_range(args, **kwargs):
        result = run(args, **kwargs)
        if args[0] == "amixer":
            result.stdout = mixer(commands.values, minimum=5, maximum=9)
        return result

    commands.values = (6,)
    monkeypatch.setattr(subprocess, "run", narrow_range)
    assert control.reduce().after.values == (5,)
    assert control.reduce() is None


def test_restore_requires_last_applied(commands):
    control = discover_gain_control(TARGET)
    with pytest.raises(GainUnavailable, match="last-applied"):
        control.restore(control.read())


def test_expected_rejects_manual_change_before_first_reduction(commands):
    control = discover_gain_control(TARGET)
    original = control.read()
    commands.values = (25, 26)
    with pytest.raises(GainUnavailable, match="expected"):
        control.reduce(expected=original)
    assert commands.values == (25, 26)
    assert not any(args[3] == "sset" for args, _ in commands.calls if args[0] == "amixer")


def test_expected_matching_setting(commands):
    control = discover_gain_control(TARGET)
    assert control.reduce(expected=control.read()).after.values == (39, 35)


def test_same_card_source_is_ambiguous(commands):
    commands.nodes[1]["info"]["props"]["alsa.card"] = 1
    with pytest.raises(GainUnavailable, match="share this ALSA card"):
        discover_gain_control(TARGET)
    assert not any(args[0] == "amixer" for args, _ in commands.calls)


def test_target_must_be_node_interface(commands):
    commands.nodes[0]["type"] = "PipeWire:Interface:Device"
    with pytest.raises(GainUnavailable, match="Node interface"):
        discover_gain_control(TARGET)


def test_deadline_caps_every_command(commands, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    run = commands.run

    def advancing(args, **kwargs):
        assert 0 < kwargs["timeout"] <= 102.0 - now[0]
        now[0] += 0.1
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", advancing)
    control = discover_gain_control(TARGET, deadline=102.0)
    control.reduce(expected=control.read())
    now[0] = 102.0
    count = len(commands.calls)
    with pytest.raises(GainUnavailable, match="deadline"):
        control.read()
    assert len(commands.calls) == count


def test_discovery_cancelled_before_any_command(commands):
    stop = threading.Event()
    stop.set()
    with pytest.raises(GainUnavailable, match="cancelled"):
        discover_gain_control(TARGET, stop_event=stop)
    assert commands.calls == []


def test_cancellation_after_identity_before_setter(commands, monkeypatch):
    stop = threading.Event()
    control = discover_gain_control(TARGET, stop_event=stop)
    original = control.read()
    identity = control._identity
    calls = 0

    def cancel_after_identity():
        nonlocal calls
        identity()
        calls += 1
        if calls == 3:  # reduce read, compare read, setter identity
            stop.set()

    monkeypatch.setattr(control, "_identity", cancel_after_identity)
    with pytest.raises(GainUnavailable, match="cancelled"):
        control.reduce(expected=original)
    assert commands.values == original.values
    assert not any(args[3] == "sset" for args, _ in commands.calls if args[0] == "amixer")


def test_restore_has_independent_cleanup_budget_and_ignores_stop(commands, monkeypatch):
    control = discover_gain_control(TARGET)
    original = control.read()
    control.reduce(expected=original)
    stop = threading.Event()
    stop.set()
    control.set_operation_limits(deadline=time.monotonic() - 1, stop_event=stop)
    run = commands.run

    def cleanup_commands(args, **kwargs):
        assert 0 < kwargs["timeout"] <= 3
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", cleanup_commands)
    control.restore(original)
    assert commands.values == original.values
    with pytest.raises(GainUnavailable, match="cancelled"):
        control.read()


def test_cancel_after_setter_rolls_back_safely(commands, monkeypatch):
    stop = threading.Event()
    control = discover_gain_control(TARGET, stop_event=stop)
    original = control.read()
    run = commands.run

    def cancel_after_set(args, **kwargs):
        result = run(args, **kwargs)
        if args[0] == "amixer" and args[3] == "sset":
            stop.set()
        return result

    monkeypatch.setattr(subprocess, "run", cancel_after_set)
    with pytest.raises(GainUnavailable, match="Original Capture gain verified"):
        control.reduce(expected=original)
    assert commands.values == original.values


def test_cleanup_budget_is_shared_not_renewed_per_command(commands, monkeypatch):
    control = discover_gain_control(TARGET)
    original = control.read()
    control.reduce(expected=original)
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    run = commands.run

    def consume_cleanup(args, **kwargs):
        assert 0 < kwargs["timeout"] <= 103.0 - now[0]
        result = run(args, **kwargs)
        now[0] += 1.0
        return result

    monkeypatch.setattr(subprocess, "run", consume_cleanup)
    with pytest.raises(GainUnavailable, match="deadline"):
        control.restore(original)
    assert now[0] == 103.0
    assert commands.values == (39, 35)  # No time remained to safely issue a restore.


def test_same_card_ambiguity_revalidated_before_mutation(commands):
    control = discover_gain_control(TARGET)
    original = control.read()
    commands.nodes[1]["info"]["props"]["alsa.card"] = "01"
    with pytest.raises(GainUnavailable, match="ambiguous"):
        control.reduce(expected=original)
    assert commands.values == original.values
