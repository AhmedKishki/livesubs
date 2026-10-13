"""Installed command for live startup, serving, configuration and replay."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from livesubs import __version__
from livesubs.approaches import (
    APPROACHES,
    build_approach,
    required_roles,
    requires_credentials,
)
from livesubs.audio.factory import SYNTAX
from livesubs.config import CONFIG_FILENAME, Settings, find_config_file, get_settings
from livesubs.keystore import (
    PROVIDERS,
    global_env_path,
    insecure_files,
    key_statuses,
    planned_providers,
    project_env_path,
    remove_env_var,
    upsert_env_var,
)
from livesubs.sim import SCRIPTS, replay

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="livesubs",
        description="Realtime English speech to German lecture subtitles.",
    )
    parser.add_argument("--version", action="version", version=f"livesubs {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and exit; non-zero if the environment is unusable",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="run the M0 pre-flight checks (capture target, signal, LAN, port)",
    )
    server_actions = parser.add_mutually_exclusive_group()
    server_actions.add_argument(
        "--start",
        action="store_true",
        help="choose a microphone, check keys and capture, then serve (default action)",
    )
    server_actions.add_argument(
        "--serve",
        action="store_true",
        help=(
            "serve an explicitly configured input without interactive setup; "
            "use --approach sim for a credential-free demo"
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help="bind address for --serve (default: LIVESUBS_HOST or 0.0.0.0)",
    )
    parser.add_argument(
        "--port", type=int, default=None, help="bind port for --serve (default: 8000)"
    )
    parser.add_argument(
        "--exit-on-failure",
        action="store_true",
        help=(
            "exit with status 1 if the pipeline dies, so a supervisor such as systemd "
            "restarts the process; without it the server keeps answering and shows 'stopped'"
        ),
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="session id, which appears in the viewer URL and the QR code",
    )
    parser.add_argument(
        "--session-name",
        default=None,
        help="session name shown on the landing page",
    )
    parser.add_argument(
        "--keys",
        action="store_true",
        help="show which API keys are set and where each one came from",
    )
    parser.add_argument(
        "--set-key",
        metavar="PROVIDER",
        choices=sorted(PROVIDERS),
        help="prompt for a provider's API key (input hidden) and optionally save it",
    )
    parser.add_argument(
        "--unset-key",
        metavar="PROVIDER",
        choices=sorted(PROVIDERS),
        help="remove a provider's API key from the selected env file",
    )
    parser.add_argument(
        "--scope",
        choices=("project", "global"),
        default="project",
        help=(
            "where --set-key/--unset-key writes: the project .env, or "
            "~/.config/livesubs/.env shared across checkouts (default: project)"
        ),
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="with --set-key: do not write to disk; print the export line for this session",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="with --set-key: probe the provider to confirm the key is accepted",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=(
            "use this config file instead of searching for livesubs.yaml "
            "(see docs/12-configuration.md)"
        ),
    )
    parser.add_argument(
        "--init-config",
        metavar="PATH",
        nargs="?",
        const=CONFIG_FILENAME,
        default=None,
        help=f"write a starter config file (default: ./{CONFIG_FILENAME}) and exit",
    )
    parser.add_argument(
        "--approach",
        choices=sorted(APPROACHES),
        default=None,
        help="how subtitles are produced with --serve (default: live); see --approaches",
    )
    parser.add_argument(
        "--approaches",
        action="store_true",
        help="list the registered approaches and whether each is available",
    )
    parser.add_argument(
        "--audio",
        metavar="SPEC",
        default=None,
        help=(
            "preferred pw:TARGET for startup, or input for --serve. "
            f"One of: {SYNTAX}"
        ),
    )
    parser.add_argument(
        "--script",
        choices=sorted(SCRIPTS),
        metavar="SCRIPT",
        help=f"script for the sim approach ({', '.join(sorted(SCRIPTS))})",
    )
    parser.add_argument(
        "--cues",
        metavar="PATH",
        help=(
            "a timed .srt or .vtt of what is being said, for --approach live-text "
            "(build one with tools/align_reference.py)"
        ),
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="sim playback speed; 0 runs flat out for layout work (default: 1.0)",
    )
    parser.add_argument(
        "--replay",
        choices=sorted(SCRIPTS),
        metavar="SCRIPT",
        help="print a script's frames as JSON to stdout, without serving anything",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="sim: play the script once instead of repeating it",
    )
    return parser


def _config_path(args: argparse.Namespace) -> Path | None:
    """The config file the CLI was pointed at, if any."""

    return Path(args.config).expanduser() if args.config else None


def _cmd_init_config(target: str) -> int:
    """Write a starter config file, copied from the shipped example."""

    import shutil

    from livesubs.config import checkout_root

    destination = Path(target).expanduser()
    if destination.exists():
        print(f"{destination} already exists; not overwriting it", file=sys.stderr)
        return 1

    root = checkout_root()
    example = (root or Path(__file__).resolve().parent) / f"{CONFIG_FILENAME}.example"
    if not example.is_file():
        example = Path(__file__).resolve().parent / f"{CONFIG_FILENAME}.example"
    if not example.is_file():
        print(f"no example config to copy from at {example}", file=sys.stderr)
        return 1

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, destination)
    print(f"wrote {destination}")
    print("Edit it, then run `livesubs --check`.")
    return 0


def _cmd_check(config_path: Path | None = None, *, approach_name: str | None = None) -> int:
    settings = get_settings(config_path)

    # The check has to reflect what will actually run. An approach that never calls a
    # vendor is perfectly usable without an API key, and telling a new user otherwise
    # sends them looking for a problem they do not have.
    name = approach_name or _default_approach()
    needs_keys = requires_credentials(name)
    problems = settings.validate_for_startup(
        require_credentials=needs_keys,
        roles=required_roles(name) if needs_keys else None,
    )

    resolved = find_config_file(config_path)

    if problems:
        print("configuration problems:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    print("configuration OK")
    print(
        f"  approach       : {name} "
        f"({'needs a usable provider' if needs_keys else 'needs no provider'})"
    )
    print(f"  config file    : {resolved or '(none; using built-in defaults)'}")
    print(f"  capture target : {settings.capture_target or '(not set; run --preflight)'}")
    startup_calibration = (
        "on (verified hardware reductions)" if settings.capture.auto_calibrate else "off"
    )
    print(f"  auto calibrate : {startup_calibration}")
    runtime_calibration = settings.capture.auto_calibrate and settings.capture.runtime_calibration
    print(f"  runtime gain   : {'on (bounded clipping protection)' if runtime_calibration else 'off'}")
    print(f"  viewer base URL: {settings.qr_base_url()}")

    for role in ("asr", "mt"):
        chain = [p.name for p in settings.providers_for(role)]
        selected = settings.active_provider(role)
        print(
            f"  {role.upper():<14} : "
            f"{selected.name if selected else '(none has a key)'}  "
            f"[order: {', '.join(chain) or 'none'}]"
        )

    print(
        "  segmentation   : "
        f"silence {settings.endpoint_silence_ms} ms, "
        f"cap {settings.max_segment_ms} ms"
    )
    return 0


def _env_path_for(scope: str) -> Path:
    return global_env_path() if scope == "global" else project_env_path()


def _warn_insecure_files() -> None:
    """Say so when a file holding API keys can be read by other users."""

    for path in insecure_files():
        print(
            f"warning: {path} can be read by other users and may hold API keys; "
            f"run: chmod 600 {path}",
            file=sys.stderr,
        )


def _cmd_keys() -> int:
    """Report which keys are set, and where each came from."""

    _warn_insecure_files()
    statuses = key_statuses()
    print("API keys\n")
    for status in statuses:
        print(status.render())
        print(f"          {status.provider.label}")
        print(f"          obtain: {status.provider.hint}")

    planned = planned_providers()
    if planned:
        names = ", ".join(provider.name for provider in planned)
        print(
            f"\nPlanned providers ({names}) are listed for future work and can hold a\n"
            "key, but no adapter exists yet, so they are not used and do not count\n"
            "towards the requirements below."
        )

    print(
        "\nPrecedence, highest first:\n"
        "  1. a real environment variable  (export DEEPGRAM_API_KEY=... - session only)\n"
        "  2. the project .env\n"
        "  3. ~/.config/livesubs/.env\n"
        "\nSet one with:  livesubs --set-key deepgram"
    )

    # Only ACTIVE providers can satisfy a requirement. A planned provider holding
    # a key must never make the setup look complete.
    def satisfied(role: str) -> bool:
        return any(
            status.set and status.provider.role == role
            for status in statuses
            if not status.provider.planned
        )

    missing = [role.upper() for role in ("asr", "mt") if not satisfied(role)]
    if missing:
        print("\nStill missing a usable provider for: " + ", ".join(missing))
        return 2
    return 0


def _cmd_set_key(name: str, scope: str, *, save: bool, verify: bool) -> int:
    """Prompt for a key without echoing it, then optionally persist it."""

    provider = PROVIDERS[name]
    print(f"{provider.label}")
    print(f"  environment variable: {provider.env_var}")
    print(f"  obtain a key from   : {provider.hint}\n")

    if provider.planned:
        print(
            "NOTE: this provider is planned, not integrated. The key will be stored\n"
            "and verified, but nothing reads it yet - the pipeline will keep using the\n"
            "active provider for this stage until an adapter is added.\n"
        )

    try:
        entered = getpass.getpass("Paste the key (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\naborted; nothing was written", file=sys.stderr)
        return 1

    if not entered:
        print("empty input; nothing was written", file=sys.stderr)
        return 1

    if any(character.isspace() for character in entered):
        print(
            "warning: the key contains whitespace, which is almost certainly a "
            "paste error. Continuing anyway.",
            file=sys.stderr,
        )

    if verify:
        from livesubs.keycheck import verify_key

        print("\nverifying...", end=" ", flush=True)
        ok, detail = verify_key(provider, entered)
        print(("OK: " if ok else "FAILED: ") + detail)
        if not ok:
            print(
                "\nThe key was NOT saved. Re-run --set-key to try again, or omit "
                "--verify to save it without checking.",
                file=sys.stderr,
            )
            return 2

    if not save:
        print(
            "\nNot written to disk. To use it for this shell session only:\n"
            f"  export {provider.env_var}='{entered}'\n"
            "\nNote that this lands in your shell history. Prefix the line with a\n"
            "space if HISTCONTROL=ignorespace is enabled.",
        )
        return 0

    path = _env_path_for(scope)
    upsert_env_var(path, provider.env_var, entered)
    print(f"\nsaved to {path} (owner-only permissions, 0600)")

    if scope == "project" and not _is_gitignored(path):
        print(
            "WARNING: that file does not appear to be gitignored. Check .gitignore "
            "before committing anything.",
            file=sys.stderr,
        )

    print("\nRun `livesubs --keys` to confirm, or `livesubs --preflight` to check the setup.")
    return 0


def _is_gitignored(path: Path) -> bool:
    """Best-effort check that a secrets file will not be committed."""

    import subprocess

    try:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", str(path)],
            cwd=str(path.parent),
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True                       # not a git checkout; nothing to leak into
    return result.returncode == 0


def _cmd_unset_key(name: str, scope: str) -> int:
    provider = PROVIDERS[name]
    path = _env_path_for(scope)

    if remove_env_var(path, provider.env_var):
        print(f"removed {provider.env_var} from {path}")
        return 0

    print(f"{provider.env_var} was not present in {path}")
    if scope == "project":
        print("(check --scope global, and note that an exported environment variable wins)")
    return 1


def _default_approach() -> str:
    """Pick the best available approach, preferring ``live`` over ``sim``.

    Deliberately not hardcoded: once the live pipeline becomes available it is
    selected automatically, with no edit here. The fallback keeps the replay
    working in the meantime.
    """

    for preferred in ("live", "sim"):
        approach = APPROACHES.get(preferred)
        if approach is not None and approach.available:
            return preferred

    for approach in APPROACHES.values():
        if approach.available:
            return approach.name

    raise RuntimeError("no approach is available")


def _cmd_approaches() -> int:
    print("Approaches\n")
    for approach in APPROACHES.values():
        state = "ready" if approach.available else approach.status
        print(f"  [{state:7}] {approach.name:<8} {approach.label}")
        print(f"              {approach.summary}")
        if not approach.available and approach.unavailable_reason:
            print(f"              -> {approach.unavailable_reason}")
        print()

    print("Select one with:  livesubs --serve --approach NAME")
    return 0


def _cmd_start(args: argparse.Namespace, settings: Settings) -> int:
    """Select a microphone and validate live startup before binding the server."""

    import os
    import shutil

    from livesubs.launcher import prepare_live_start
    from livesubs.preflight import _main as preflight_main

    if args.approach not in (None, "live") or args.script or args.cues or args.once:
        raise ValueError("use --serve for simulation, timed transcripts or replay options")
    if args.speed != 1.0:
        raise ValueError("--speed applies to replay with --serve, not microphone startup")
    if args.audio and (not args.audio.startswith("pw:") or not args.audio[3:].strip()):
        raise ValueError("startup accepts pw:TARGET; use --serve for a file or test audio")
    if args.port is not None and not 1 <= args.port <= 65535:
        raise ValueError("the server port must be between 1 and 65535")
    if sys.platform != "linux":
        raise ValueError("live microphone startup requires Linux with PipeWire")
    if os.geteuid() == 0:
        raise ValueError("run livesubs as your desktop user, not with sudo")
    _warn_insecure_files()

    missing = [binary for binary in ("pw-record", "pw-dump") if shutil.which(binary) is None]
    if missing:
        raise ValueError("missing system commands: " + ", ".join(missing))

    problems = settings.validate_for_startup(require_credentials=False)
    if problems:
        raise ValueError("; ".join(problems))

    settings = prepare_live_start(
        settings,
        preferred_target=args.audio[3:].strip() if args.audio else None,
        config_path=_config_path(args),
    )
    server_updates = {}
    if args.host is not None:
        server_updates["host"] = args.host
    if args.port is not None:
        server_updates["port"] = args.port
    if server_updates:
        settings = settings.model_copy(
            update={"server": settings.server.model_copy(update=server_updates)}
        )
    settings = settings.for_session(session_id=args.session_id, session_name=args.session_name)

    print(f"Microphone source: {settings.capture_target!r}", flush=True)
    print("Speak at normal volume while capture is checked.", flush=True)
    result = preflight_main(settings, approach_name="live")
    if result != 0:
        return result

    args.approach = "live"
    args.audio = f"pw:{settings.capture_target}"
    return _cmd_serve(args, settings)


def _cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    """Run the HTTP/WebSocket server with the selected approach.

    The approach is chosen here and injected into the app, which is the only place
    that decision is made. Adding one is a registration in ``livesubs.approaches``;
    nothing in this function, in the server, or in the overlay needs to change.
    """

    import uvicorn

    from livesubs.preflight import lan_ip
    from livesubs.server.app import create_app, log_config
    from livesubs.server.routes import viewer_url

    # Before anything is built from the settings. The flag has to reach the settings once
    # here, or the approach names a session's artefacts from the config file while the
    # server registers the id from the flag.
    settings = settings.for_session(
        session_id=args.session_id, session_name=args.session_name
    )

    name = args.approach or _default_approach()
    options = {
        "script": args.script or "default",
        "speed": str(args.speed),
        # Serving repeats the script so the demo is still running whenever someone
        # gets to the phone. `--once` restores a finite replay.
        "loop": "false" if args.once else "true",
        # Empty rather than a default value, so the builder can tell "the operator did
        # not choose" from "the operator chose synth" and fall back to the device named
        # in the config file in between. See `resolve_audio_spec`.
        "audio": args.audio or "",
        # The timed transcript `live-text` needs, and nothing to any other approach.
        "cues": args.cues or "",
    }

    try:
        publisher = build_approach(name, settings, options)
    except (KeyError, NotImplementedError) as exc:
        print(f"{exc}", file=sys.stderr)
        print("\nRun `livesubs --approaches` to see what is available.", file=sys.stderr)
        return 2
    except ValueError as exc:
        # An unusable --audio spec, or a capture target that does not exist. Both are
        # the operator's own typo, so the message is the whole point and a traceback
        # would only bury it.
        print(f"{exc}", file=sys.stderr)
        return 2

    if name != "live":
        # Say plainly which approach is running. A silent fallback to the replay
        # would be confusing, and worse, could look like a working live pipeline.
        print(
            f"APPROACH: {name} - {APPROACHES[name].label}\n"
            f"  {APPROACHES[name].summary}\n",
            file=sys.stderr,
        )
        if not APPROACHES[name].requires_credentials:
            print(
                "  This is not the live API pipeline; run `livesubs --approaches`\n"
                "  and see docs/10-roadmap.md for what remains.\n",
                file=sys.stderr,
            )

    host = args.host or settings.host
    port = args.port or settings.port

    failed = False

    def pipeline_died() -> None:
        nonlocal failed
        failed = True
        server.should_exit = True

    app = create_app(
        settings, publisher, on_failure=pipeline_died if args.exit_on_failure else None
    )

    # When bound to all interfaces, print the LAN address rather than 0.0.0.0, so the
    # URL shown is the one attendees can actually open. The bind address is named rather
    # than silently replaced, because it is what the operator typed a moment ago and a
    # browser on this machine accepts it: opening it works for them and gives every
    # phone ERR_CONNECTION_REFUSED, which reads as "the QR code does not work".
    if host in {"0.0.0.0", "::"}:
        address = lan_ip()
        display_host = address or "localhost"
        note = (
            f"   (open this, not the bind address {host})"
            if address is not None
            else "   (bound to every interface, but no LAN address was found)"
        )
    else:
        display_host = host
        note = ""

    base = f"http://{display_host}:{port}"

    # flush=True matters: without it these lines are block-buffered away whenever
    # stdout is a file or the journal, which is exactly how this runs under
    # systemd - and the URLs are the most useful thing the command prints.
    print(f"landing page : {base}/{note}", flush=True)
    print(f"viewer (QR)  : {viewer_url(settings, settings.session_id, base + '/')}", flush=True)
    print(f"operator     : {base}/admin", flush=True)
    print(flush=True)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=settings.log_level,
            # Without this, uvicorn's own config disables the loggers this package created
            # before it started, so a served run loses the pipeline's INFO lines.
            log_config=log_config(settings.log_level),
        )
    )
    server.run()
    return 1 if failed else 0


async def _cmd_replay(script: str, speed: float) -> int:
    """Print a script's frames as JSON, without serving anything.

    Useful for inspecting the wire contract by eye, and for diffing fixture changes.
    """

    from livesubs.events import dumps

    count = 0
    async for message in replay(script, speed=speed):
        print(dumps(message))
        count += 1

    print(f"\n{count} frames from script {script!r}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        return _dispatch(args)
    except (FileNotFoundError, ValueError) as exc:
        # A configuration problem must read as a message, not a traceback: the
        # person seeing it is setting up minutes before a lecture.
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nStartup cancelled.", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"could not access a required file or command: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    config_path = _config_path(args)

    if args.init_config:
        return _cmd_init_config(args.init_config)

    if args.keys:
        return _cmd_keys()

    if args.set_key:
        return _cmd_set_key(
            args.set_key, args.scope, save=not args.no_save, verify=args.verify
        )

    if args.unset_key:
        return _cmd_unset_key(args.unset_key, args.scope)

    if args.no_save or args.verify:
        print("--no-save and --verify only apply together with --set-key", file=sys.stderr)
        return 1

    if args.check:
        return _cmd_check(config_path, approach_name=args.approach)

    if args.preflight:
        from livesubs.preflight import _main as preflight_main

        return preflight_main(
            get_settings(config_path),
            approach_name=args.approach or _default_approach(),
        )

    if args.approaches:
        return _cmd_approaches()

    if args.serve:
        return _cmd_serve(args, get_settings(config_path))

    if args.replay:
        return asyncio.run(_cmd_replay(args.replay, args.speed))

    return _cmd_start(args, get_settings(config_path))


if __name__ == "__main__":
    raise SystemExit(main())
