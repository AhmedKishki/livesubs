"""Probe a running server's WebSocket, the way an attendee's phone would.

Worth having as a tool rather than a throwaway snippet: "the page is blank" is
the most common report, and this distinguishes the three causes immediately -
the server is not running, the session id is wrong, or the socket connects but no
segments are flowing.

    uv run python tools/ws_probe.py --session demo --role viewer --count 6
    uv run python tools/ws_probe.py --session demo --role viewer_ar --count 6

The role is ``viewer`` for the session's default language, ``viewer_<code>`` for another
language the session offers, or ``admin`` for every language and the speaker's own words.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import websockets

__all__ = ["main", "probe"]


async def probe(url: str, *, count: int, timeout: float) -> int:
    try:
        async with websockets.connect(url, open_timeout=timeout) as socket:
            print(f"connected: {url}", flush=True)
            for _ in range(count):
                raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
                payload = json.loads(raw)
                kind = payload.get("type")
                data = payload.get("data", {})

                if kind == "segment":
                    src = data.get("src")
                    print(
                        f"segment seq={data.get('seq')} rev={data.get('rev')} "
                        f"state={data.get('state')} language={data.get('language')} "
                        f"text={data.get('text')!r} "
                        f"src={'present' if src else 'stripped'}",
                        flush=True,
                    )
                else:
                    print(f"{kind}: {json.dumps(data, ensure_ascii=False)}", flush=True)
        return 0
    except TimeoutError:
        print(f"timed out waiting for messages from {url}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"could not connect to {url}: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe a livesubs WebSocket.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--session", default="demo")
    parser.add_argument(
        "--role",
        default="viewer",
        help="viewer, viewer_<code> or admin (default: viewer)",
    )
    parser.add_argument("--count", type=int, default=6, help="messages to read")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    url = f"ws://{args.host}:{args.port}/ws/{args.session}?role={args.role}"
    return asyncio.run(probe(url, count=args.count, timeout=args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
