"""Tests for the interactive terminal microphone picker.

The picker reads from a real file descriptor and puts the terminal into raw
mode, so these tests drive it through a pseudoterminal rather than a mock
stream. That is the only way to exercise arrow-key parsing, raw mode and its
restoration, and the Esc/Ctrl-C/EOF paths honestly.

No microphone, provider, credential or subprocess is involved: the picker is a
pure UI whose input is a supplied list of ``(node.name, description)`` pairs.
"""

from __future__ import annotations

import io
import os
import pty
import select
import termios
import threading
import time
from contextlib import contextmanager

import pytest

from livesubs.audio import selection
from livesubs.audio.selection import select_microphone

_HELP = b"Arrows: choose"
_CURSOR_VISIBLE = b"\x1b[?25h"

SOURCES = [("node0", "First Mic"), ("node1", "Second Mic"), ("node2", "Third Mic")]


class _Fd:
    """Minimal ``fileno``-only stand-in for a text stream over an fd."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


class _Session:
    """Run the picker against one pseudoterminal and drive it from the master."""

    def __init__(self, sources, *, preferred: str | None = None) -> None:
        self.master, self.slave = pty.openpty()
        self.termios_before = termios.tcgetattr(self.slave)
        self.result: str | None = None
        self.error: BaseException | None = None
        self.output = bytearray()
        self._thread = threading.Thread(
            target=self._run, args=(sources, preferred), daemon=True
        )
        self._thread.start()
        self._drain_until(_HELP)

    def _run(self, sources, preferred: str | None) -> None:
        try:
            self.result = select_microphone(
                sources,
                preferred=preferred,
                input_stream=_Fd(self.slave),
                output_stream=_Fd(self.slave),
            )
        except BaseException as exc:  # noqa: BLE001 -- surfaced to the test
            self.error = exc

    def _drain_until(self, marker: bytes, timeout: float = 3.0) -> bytes:
        end = time.monotonic() + timeout
        while marker not in self.output and time.monotonic() < end:
            readable, _, _ = select.select([self.master], [], [], 0.05)
            if not readable:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.output += chunk
        return bytes(self.output)

    def send(self, data: bytes, pause: float = 0.2) -> None:
        os.write(self.master, data)
        time.sleep(pause)

    def finish(self, timeout: float = 3.0) -> str | None:
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._interrupt()
        self._drain_until(_CURSOR_VISIBLE, timeout=0.5)
        return self.result

    def confirm(self) -> str | None:
        self.send(b"\r")
        return self.finish()

    def _interrupt(self) -> None:
        try:
            os.write(self.master, b"\x03")
        except OSError:
            pass
        self._thread.join(1.0)

    def close_master(self) -> None:
        try:
            os.close(self.master)
        except OSError:
            pass

    def shutdown(self) -> None:
        self._thread.join(2.0)
        if self._thread.is_alive():
            self._interrupt()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


@contextmanager
def pty_session(sources, *, preferred: str | None = None):
    session = _Session(sources, preferred=preferred)
    try:
        yield session
    finally:
        session.shutdown()


def _read_available(fd: int, timeout: float = 1.0) -> bytes:
    end = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < end:
        readable, _, _ = select.select([fd], [], [], 0.05)
        if not readable:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    return bytes(data)


def test_a_single_source_is_still_shown_and_confirmed() -> None:
    with pty_session([("only", "Only Mic")]) as session:
        rendered = bytes(session.output)
        assert b"Only Mic" in rendered
        assert b"\x1b[7m> Only Mic  (only)" in rendered
        assert session.confirm() == "only"


def test_the_preferred_node_is_highlighted() -> None:
    with pty_session(SOURCES, preferred="node1") as session:
        rendered = bytes(session.output)
        assert b"\x1b[7m> Second Mic  (node1)\x1b[0m" in rendered
        assert b"\x1b[7m> First Mic" not in rendered


def test_an_absent_preference_highlights_the_first_entry() -> None:
    with pty_session(SOURCES, preferred="missing") as session:
        assert b"\x1b[7m> First Mic  (node0)" in bytes(session.output)


def test_arrow_down_moves_the_selection() -> None:
    with pty_session(SOURCES) as session:
        session.send(b"\x1b[B")
        assert session.confirm() == "node1"


def test_arrow_up_wraps_from_the_first_entry_to_the_last() -> None:
    with pty_session(SOURCES) as session:
        session.send(b"\x1b[A")
        assert session.confirm() == "node2"


def test_arrow_down_wraps_from_the_last_entry_to_the_first() -> None:
    with pty_session(SOURCES, preferred="node2") as session:
        session.send(b"\x1b[B")
        assert session.confirm() == "node0"


def test_application_mode_arrow_keys_are_accepted() -> None:
    with pty_session(SOURCES) as session:
        session.send(b"\x1bOB")
        assert session.confirm() == "node1"


def test_application_mode_arrow_up_is_accepted() -> None:
    with pty_session(SOURCES, preferred="node1") as session:
        session.send(b"\x1bOA")
        assert session.confirm() == "node0"


@pytest.mark.parametrize("enter", [b"\r", b"\n"])
def test_enter_confirms_in_both_line_encodings(enter: bytes) -> None:
    with pty_session(SOURCES) as session:
        session.send(enter)
        assert session.finish() == "node0"


def test_the_node_is_returned_not_the_description() -> None:
    with pty_session([("exact.node.name", "Friendly Label")]) as session:
        result = session.confirm()
    assert result == "exact.node.name"


def test_escape_cancels_restores_the_terminal_and_shows_the_cursor() -> None:
    session = _Session(SOURCES)
    try:
        session.send(b"\x1b")
        session.finish()
        assert isinstance(session.error, KeyboardInterrupt)
        assert termios.tcgetattr(session.slave) == session.termios_before
        assert _CURSOR_VISIBLE in bytes(session.output)
    finally:
        session.shutdown()


def test_ctrl_c_cancels_restores_the_terminal_and_shows_the_cursor() -> None:
    session = _Session(SOURCES)
    try:
        session.send(b"\x03")
        session.finish()
        assert isinstance(session.error, KeyboardInterrupt)
        assert termios.tcgetattr(session.slave) == session.termios_before
        assert _CURSOR_VISIBLE in bytes(session.output)
    finally:
        session.shutdown()


def test_end_of_input_raises_eof() -> None:
    session = _Session(SOURCES)
    try:
        session.close_master()
        session._thread.join(3.0)
        assert isinstance(session.error, EOFError)
    finally:
        session.shutdown()


def test_terminal_is_restored_when_rendering_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(selection, "_render", boom)
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            select_microphone(
                SOURCES, input_stream=_Fd(slave), output_stream=_Fd(slave)
            )
        except BaseException as exc:  # noqa: BLE001 -- surfaced to the test
            errors.append(exc)

    try:
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(3.0)
        assert errors and isinstance(errors[0], RuntimeError)
        assert termios.tcgetattr(slave) == before
        assert _CURSOR_VISIBLE in _read_available(master)
    finally:
        os.close(master)
        os.close(slave)


def test_an_empty_source_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="no microphone sources"):
        select_microphone([])


def test_a_non_tty_input_is_rejected() -> None:
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, "rb", buffering=0) as stream, pytest.raises(
            ValueError, match="standard input"
        ):
            select_microphone([("node0", "First Mic")], input_stream=stream)
    finally:
        os.close(write_fd)


def test_a_stream_without_a_file_descriptor_is_rejected() -> None:
    with pytest.raises(ValueError, match="standard input"):
        select_microphone([("node0", "First Mic")], input_stream=io.StringIO())


def test_a_non_tty_output_is_rejected() -> None:
    master, slave = pty.openpty()
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, "rb", buffering=0) as stream, pytest.raises(
            ValueError, match="standard error"
        ):
            select_microphone(
                [("node0", "First Mic")],
                input_stream=_Fd(slave),
                output_stream=stream,
            )
    finally:
        os.close(master)
        os.close(slave)
        os.close(write_fd)


def test_control_characters_are_sanitised_but_the_name_is_returned_verbatim() -> None:
    name = "node\x01id"
    description = "\x1b[31mRed Node\x1b[0m"
    with pty_session([(name, description)]) as session:
        rendered = bytes(session.output)
        result = session.confirm()

    assert b"\x1b[31m" not in rendered
    assert b"Red Node" in rendered
    assert result == name


def test_source_values_are_never_executed(tmp_path) -> None:
    marker = tmp_path / "pwned"
    hostile = f"; touch {marker}; echo /etc/passwd"
    with pty_session([(hostile, "Hostile")]) as session:
        result = session.confirm()
    assert result == hostile
    assert not marker.exists()


def test_long_names_stay_on_one_row_without_changing_the_selection() -> None:
    name = "alsa_input.usb-" + "long-device-name" * 20
    with pty_session([(name, "A long microphone description " * 20)]) as session:
        rendered = bytes(session.output).decode("utf-8")
        assert "…" in rendered
        assert session.confirm() == name


def test_line_fitting_counts_wide_and_combining_characters() -> None:
    assert selection._fit_line("日本語 microphone", 7) == "日本語…"
    assert selection._fit_line("e\u0301", 1) == "e\u0301"


def test_crlf_confirmation_does_not_leak_a_newline_into_the_next_prompt() -> None:
    with pty_session(SOURCES) as session:
        session.send(b"\r\n")
        assert session.finish() == "node0"
        assert not select.select([session.slave], [], [], 0)[0]
