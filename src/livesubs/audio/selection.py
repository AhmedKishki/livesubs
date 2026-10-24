"""Interactive microphone picker for a Linux terminal.

**Pure UI, no discovery.** The caller enumerates PipeWire sources and passes
``(node.name, node.description)`` pairs in; this module never runs ``pw-dump``,
imports configuration or spawns anything. That keeps discovery in one place
(:func:`livesubs.audio.pw_record.pipewire_sources`) and makes the picker usable
from any front end that already has a candidate list.

**Raw terminal, real file descriptors.** Arrow keys arrive as byte sequences,
and the standard stream wrappers buffer a line at a time, so reading them
through ``input_stream.read`` would block until Enter. The picker therefore
reads the underlying descriptor with :func:`os.read` and waits on it with
:func:`select.select`. The passed streams are used only to obtain that fd and
are never read or written through.

**The returned value is the node, not the label.** Descriptions are for humans;
identity is ``node.name``. Rendering sanitises control characters and ANSI
escapes so a hostile or malformed description cannot repaint the terminal, but
the string returned is the caller's original ``node.name`` byte for byte.

**Restoration is unconditional.** On confirm, cancel or an unexpected error the
terminal mode is put back, the owned lines are cleared and the cursor is made
visible again. Nothing is auto-selected: a single source is still shown and
confirmed by the operator.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import tty
import unicodedata
from collections.abc import Sequence
from typing import TextIO

__all__ = ["select_microphone"]

_ESCAPE = b"\x1b"
_ENTER = (b"\r", b"\n")
_CTRL_C = b"\x03"
_ESCAPE_TIMEOUT = 0.1
_HELP = "Arrows: choose  Enter: confirm  Esc: cancel"


def select_microphone(
    sources: Sequence[tuple[str, str]],
    *,
    preferred: str | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> str:
    """Let the operator choose a source and return its exact ``node.name``.

    ``sources`` holds ``(node.name, node.description)`` pairs. A bounded window
    follows the selection in long lists. ``preferred`` highlights the matching
    node if it is listed, otherwise the first entry is highlighted.

    Raises :class:`ValueError` when there is nothing to choose from or when
    either stream is not an interactive terminal, :class:`EOFError` when input
    ends, and :class:`KeyboardInterrupt` when the operator presses Esc or
    Ctrl-C. Terminal state is restored in every case.
    """

    entries = list(sources)
    if not entries:
        raise ValueError("no microphone sources available to choose from")

    in_fd = _tty_fd(input_stream if input_stream is not None else sys.stdin, "standard input")
    out_fd = _tty_fd(output_stream if output_stream is not None else sys.stderr, "standard error")

    current = 0
    if preferred is not None:
        for index, (name, _) in enumerate(entries):
            if name == preferred:
                current = index
                break

    saved: list[object] | None = None
    drawn = 0
    try:
        saved = termios.tcgetattr(in_fd)
        tty.setraw(in_fd)
        os.write(out_fd, b"\x1b[?25l")
        drawn = _render(out_fd, entries, current, 0)
        while True:
            key = _read_key(in_fd)
            if key == "enter":
                return entries[current][0]
            if key == "escape":
                raise KeyboardInterrupt
            if key == "up":
                current = (current - 1) % len(entries)
                drawn = _render(out_fd, entries, current, drawn)
            elif key == "down":
                current = (current + 1) % len(entries)
                drawn = _render(out_fd, entries, current, drawn)
    finally:
        reset = bytearray()
        if drawn:
            reset += f"\x1b[{drawn}A\r\x1b[J".encode()
        if saved is not None:
            try:
                termios.tcsetattr(in_fd, termios.TCSAFLUSH, saved)
            except (OSError, termios.error):
                pass
        reset += b"\x1b[?25h"
        try:
            os.write(out_fd, bytes(reset))
        except OSError:
            pass


def _tty_fd(stream: object, role: str) -> int:
    """Return the stream's fd only when it is an interactive terminal."""

    try:
        fd = stream.fileno()  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        raise ValueError(
            f"microphone selection needs an interactive terminal, but {role} "
            "has no usable file descriptor"
        ) from None
    if fd < 0 or not os.isatty(fd):
        raise ValueError(
            f"microphone selection needs an interactive terminal, but {role} "
            "is not a TTY"
        )
    return fd


def _sanitize(text: str) -> str:
    """Replace control characters, including ESC, with a visible placeholder.

    ANSI escapes lose their ESC byte, so a description cannot issue terminal
    commands. The returned selection is never passed through this function.
    """

    return "".join(ch if ch.isprintable() else "?" for ch in text)


def _render(
    out_fd: int,
    entries: Sequence[tuple[str, str]],
    current: int,
    previous_lines: int,
) -> int:
    """Draw the list in place and return the number of owned lines."""

    size = os.get_terminal_size(out_fd)
    columns = size.columns or 80
    visible = min(len(entries), max(1, (size.lines or 24) - 3))
    start = max(0, min(current - visible // 2, len(entries) - visible))
    width = max(1, columns - 1)
    lines: list[str] = []
    if previous_lines:
        lines.append(f"\x1b[{previous_lines}A\r\x1b[J")
    for index in range(start, start + visible):
        name, description = entries[index]
        label = _sanitize(description) or _sanitize(name)
        detail = _sanitize(name)
        text = f"{label}  ({detail})"
        if index == current:
            lines.append("\r\x1b[K\x1b[7m" + _fit_line("> " + text, width) + "\x1b[0m\r\n")
        else:
            lines.append("\r\x1b[K" + _fit_line("  " + text, width) + "\r\n")
    help_text = _HELP
    if visible < len(entries):
        help_text += f"  {current + 1}/{len(entries)}"
    lines.append("\r\x1b[K" + _fit_line(help_text, width) + "\r\n")
    os.write(out_fd, "".join(lines).encode("utf-8", "replace"))
    return visible + 1


def _fit_line(text: str, width: int) -> str:
    """Keep a rendered entry on one physical terminal row."""

    widths = [
        0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        for char in text
    ]
    if sum(widths) <= width:
        return text
    used = 0
    result = []
    for char, cells in zip(text, widths, strict=True):
        if used + cells > width - 1:
            break
        result.append(char)
        used += cells
    return "".join(result) + "…"


def _read_byte(in_fd: int) -> bytes:
    try:
        ready, _, _ = select.select([in_fd], [], [], None)
    except OSError as exc:
        raise EOFError("microphone selection input ended") from exc
    if not ready:
        return _read_byte(in_fd)
    try:
        data = os.read(in_fd, 1)
    except OSError as exc:
        raise EOFError("microphone selection input ended") from exc
    if not data:
        raise EOFError("microphone selection input ended")
    return data


def _read_byte_timeout(in_fd: int, timeout: float) -> bytes | None:
    try:
        ready, _, _ = select.select([in_fd], [], [], timeout)
    except OSError:
        return b""
    if not ready:
        return None
    try:
        return os.read(in_fd, 1)
    except OSError:
        return b""


def _read_key(in_fd: int) -> str:
    """Translate one key press into ``enter``, ``up``, ``down``, ``escape`` or ``ignore``."""

    char = _read_byte(in_fd)
    if char in _ENTER:
        return "enter"
    if char == _CTRL_C:
        raise KeyboardInterrupt
    if char == _ESCAPE:
        second = _read_byte_timeout(in_fd, _ESCAPE_TIMEOUT)
        if second is None:
            return "escape"
        if second in (b"[", b"O"):
            third = _read_byte_timeout(in_fd, _ESCAPE_TIMEOUT)
            if third == b"A":
                return "up"
            if third == b"B":
                return "down"
        return "ignore"
    return "ignore"
