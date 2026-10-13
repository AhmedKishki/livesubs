"""Run a live session with the audio playing alongside it, in sync.

A rehearsal has three parts that have to happen at once: the pipeline reading its audio in
real time, the same audio audible to the room, and a phone showing the subtitles. Doing that by
hand needs a stopwatch, because the two sides start counting from different moments.

They do not have to. ``LivePipeline.run`` warms the translator and opens the recogniser
*before* it reads the first frame, so the pipeline's audio clock starts when the providers are
ready rather than when the process starts. Waiting for that moment in the log and starting the
player then puts the two within a fraction of a second of each other, whatever the machine's
startup time, and it stays correct on a slow network.

The output of the server is passed through, so the URLs it prints - and the per-segment latency
lines - appear here as they happen.

Usage::

    uv run python tools/rehearse.py                       # the sample corpus, live
    uv run python tools/rehearse.py --approach live-text --cues samples/reference.en.timed.srt
    uv run python tools/rehearse.py --audio file:lecture.wav --no-play   # watch without sound

Ctrl-C stops the player and the server together. The player exits when the audio ends; the
server keeps serving until you stop it.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

__all__ = ["default_player", "main", "wait_for_ready"]

#: The line each approach prints once its providers are connected. Waiting for it is what makes
#: the audio and the subtitles start from the same moment.
READY = {
    "live": re.compile(r"stream open"),
    "live-text": re.compile(r"provider warm"),
}

#: Players in the order they are tried. `gst-play` handles the WAV and is what the corpus
#: notes use; `pw-play` is the PipeWire-native equivalent.
PLAYERS = (
    ("gst-play-1.0", "gst-play-1.0 --no-interactive {file}"),
    ("pw-play", "pw-play {file}"),
    ("aplay", "aplay -q {file}"),
)


def _die_with_parent() -> None:
    """Ask the kernel to signal this child if its parent dies.

    The graceful path above is not enough on its own. `uv run` sits between the terminal and
    this process, so a Ctrl-C does not always reach the handler here, and a tool that is killed
    outright cannot run a handler at all. Either way the server survives, holding the port, with
    no subtitles appearing and nothing to say why.

    ``PR_SET_PDEATHSIG`` closes that hole: the kernel signals the child itself when the parent
    goes, whatever killed it. Linux-only, and only reachable from this tool.
    """

    import ctypes
    import signal as _signal

    PR_SET_PDEATHSIG = 1
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl(PR_SET_PDEATHSIG, _signal.SIGTERM, 0, 0, 0)


def port_in_use(host: str, port: int) -> bool:
    """Whether something is already listening, so the server is not started to fail.

    `livesubs` reports a bind failure correctly, but by then the tool has already promised a
    session, and a server that died from ``EADDRINUSE`` looks exactly like one that finished:
    both exit, both leave the caller nothing to show. Checking first keeps the two apart.
    """

    import socket

    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("" if host in {"0.0.0.0", "::"} else host, port))
        except OSError:
            return True
    return False


def default_player() -> tuple[str, str] | None:
    """The first available player, as ``(binary, command template)``."""

    from shutil import which

    for binary, template in PLAYERS:
        if which(binary):
            return binary, template
    return None


def wait_for_ready(stream, pattern: re.Pattern[str], timeout_s: float) -> bool:
    """Print the server's output until it says its providers are connected.

    Every line is echoed, so the operator sees what the pipeline says - including which
    provider each stage resolved to, which is the thing worth noticing before an audience.
    """

    ready = threading.Event()
    deadline = time.monotonic() + timeout_s

    def pump() -> None:
        for line in stream:
            print(line, end="", flush=True)
            if pattern.search(line):
                ready.set()
        ready.set()  # the process ended; stop waiting rather than hang

    pump_thread = threading.Thread(target=pump, daemon=True)
    pump_thread.start()

    while not ready.is_set():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _terminate(process: subprocess.Popen | None) -> None:
    """Stop a child and everything it started.

    `uv run` re-executes Python, so signalling only the parent leaves the server alive holding
    the port. Both children are started in their own process group so the group can be signalled.
    """

    if process is None or process.poll() is not None:
        return

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--approach", default="live", choices=sorted(READY))
    parser.add_argument("--audio", default="file:samples/sample-16k-mono.wav@1")
    parser.add_argument("--cues", default=None, help="timed transcript, for --approach live-text")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default="8000")
    parser.add_argument("--session-id", default=None)
    parser.add_argument(
        "--speaker",
        default=None,
        help="the language the audio is in, en or de; the configured speaker when omitted",
    )
    parser.add_argument(
        "--player",
        default=None,
        help="command template with {file}; default picks gst-play, pw-play or aplay",
    )
    parser.add_argument("--no-play", action="store_true", help="run the pipeline without sound")
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=45.0,
        help="seconds to wait for the providers before giving up (default: 45)",
    )
    args = parser.parse_args(argv)

    audio = args.audio.split(":", 1)[1].split("@", 1)[0] if args.audio.startswith("file:") else None
    if audio is not None and not Path(audio).is_file():
        raise SystemExit(f"no such audio file: {audio}")

    player = args.player
    if player is None and not args.no_play:
        found = default_player()
        if found is None:
            raise SystemExit("no audio player found; install gst-plugins-base or pass --no-play")
        print(f"player     : {found[0]}")

    server_command = [
        sys.executable, "-m", "livesubs",
        "--serve",
        "--approach", args.approach,
        "--audio", args.audio,
        "--host", args.host,
        "--port", args.port,
    ]
    if args.cues:
        server_command += ["--cues", args.cues]
    if args.session_id:
        server_command += ["--session-id", args.session_id]
    if args.speaker:
        server_command += ["--speaker", args.speaker]

    print(f"starting   : {' '.join(shlex.quote(part) for part in server_command[2:])}")
    print(f"listening  : {args.host}:{args.port}  (open the landing page printed below and scan)")
    print()

    if port_in_use(args.host, int(args.port)):
        raise SystemExit(
            f"port {args.port} is already in use; stop the other server or choose another "
            "with --port"
        )

    # `preexec_fn` is unsafe in a threaded process: between fork and exec the child can inherit a
    # lock another thread was holding. It is safe at exactly this point, because the reader thread
    # that would be that other thread is started inside `wait_for_ready` below, after this call.
    server = subprocess.Popen(
        server_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
        preexec_fn=_die_with_parent,  # noqa: PLW1509 - no thread exists yet, see above
    )
    assert server.stdout is not None

    pattern = READY[args.approach]
    play: subprocess.Popen | None = None

    # A `kill` is how a script or a systemd stop ends a rehearsal, and it should take the server
    # with it rather than needing a second command.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))

    try:
        if not wait_for_ready(server.stdout, pattern, args.ready_timeout):
            print(
                f"\nthe providers were not ready within {args.ready_timeout:.0f}s; "
                "not starting playback",
                file=sys.stderr,
            )
            return 1

        # A server that failed to start has already exited, and its output above says why. Play
        # the audio anyway and the operator hears a lecture with no subtitles and no reason to
        # think anything is wrong.
        if server.poll() is not None:
            print(
                f"\nthe server exited before its providers were ready "
                f"(code {server.returncode}); not starting playback",
                file=sys.stderr,
            )
            return 1

        if player is not None and audio is not None:
            print(f"\n--- providers ready; playing {audio} ---\n", flush=True)
            # No `_die_with_parent` here: the reader thread is running by now, so a pre-exec hook
            # would be a real deadlock risk. A stray player is also harmless - it plays to the end
            # of the file and exits - where a stray *server* holds the port and serves nothing.
            play = subprocess.Popen(
                shlex.split(player.format(file=shlex.quote(audio))),
                start_new_session=True,
            )
            play.wait()
            print("\n--- audio finished; the server is still serving, Ctrl-C to stop ---")
        else:
            print("\n--- providers ready; no playback (--no-play) ---")

        code = server.wait()

        # Negative is a signal: Ctrl-C on the child is a normal way to end a rehearsal.
        if code > 0:
            print(f"\nthe server exited with code {code}", file=sys.stderr)
            return code

    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        _terminate(play)
        _terminate(server)

    return 0


if __name__ == "__main__":
    sys.exit(main())
