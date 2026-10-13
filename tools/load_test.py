"""Load a running server with simulated phones, and report whether it keeps up.

Phones are cheap to imagine and expensive to count, so this opens as many real WebSocket
connections as asked, behaves the way the viewer page does (answers every heartbeat, and
acknowledges every final line), and measures what a lecture hall would experience:

* **Fan-out spread.** For every message the server publishes, the time between the first
  phone receiving it and the last. It needs no clock synchronisation, because every
  generator process runs on this machine and shares one monotonic clock. A server that keeps
  up shows a spread of a few milliseconds however many phones are connected.
* **Completeness.** Whether every phone received every final line. A draft may be replaced
  by a newer one before it is sent, which is the design, but a final must arrive.
* **The server's own view.** Event-loop lateness, processor, memory and open files, taken
  from ``/api/stats`` while the load runs, because a server that is struggling says so there
  before phones notice.

The generator shares the machine with the server unless ``--host`` points elsewhere, so run
it from another machine for the figure that matters. It uses up to ``--processes`` cores.

Serve something that publishes at lecture pace first, for example::

    uv run livesubs --serve --approach sim --script fast_speaker --speaker en

then::

    uv run python tools/load_test.py --viewers 500 --duration 30

The first 500 phones read the default language, and ``--arabic`` of every hundred read Arabic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import multiprocessing
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

import websockets

__all__ = ["main"]


# --- the generator, one per process ---------------------------------------------------------


async def _phone(
    url: str,
    delay_s: float,
    until: float,
    ack: bool,
    arrivals: dict,
    outcome: dict,
) -> None:
    """One phone: connect, stay connected until ``until``, and record what arrives."""

    await asyncio.sleep(delay_s)
    began = time.monotonic()
    try:
        async with websockets.connect(url, open_timeout=30, max_size=None) as socket:
            outcome["connect_ms"].append((time.monotonic() - began) * 1000.0)
            outcome["connected"] += 1
            while time.monotonic() < until:
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=max(0.1, until - time.monotonic()))
                except TimeoutError:
                    break
                arrived = time.monotonic()
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "ping":
                    await socket.send('{"type":"pong"}')
                elif kind == "segment":
                    data = message["data"]
                    key = (data["language"], data["seq"], data["rev"], data["state"])
                    slot = arrivals.get(key)
                    if slot is None:
                        arrivals[key] = [1, arrived, arrived]
                    else:
                        slot[0] += 1
                        slot[1] = min(slot[1], arrived)
                        slot[2] = max(slot[2], arrived)
                    if ack and data["state"] == "final":
                        await socket.send(
                            json.dumps(
                                {
                                    "type": "ack",
                                    "seq": data["seq"],
                                    "rev": data["rev"],
                                    "rendered_in_ms": 25,
                                    "dropped": 0,
                                }
                            )
                        )
            outcome["finished"] += 1
    except websockets.ConnectionClosed as closed:
        outcome["dropped"][f"closed {closed.rcvd.code if closed.rcvd else '?'}"] += 1
    except (OSError, TimeoutError, websockets.WebSocketException) as error:
        outcome["failed"][type(error).__name__] += 1


async def _generate(urls: list[str], ramp_s: float, until: float, ack: bool) -> dict:
    arrivals: dict = {}
    outcome = {
        "connect_ms": [],
        "connected": 0,
        "finished": 0,
        "failed": Counter(),
        "dropped": Counter(),
    }
    step = ramp_s / max(1, len(urls))
    await asyncio.gather(
        *(_phone(url, index * step, until, ack, arrivals, outcome) for index, url in enumerate(urls))
    )
    return {"arrivals": arrivals, **outcome}


def _worker(urls: list[str], ramp_s: float, until: float, ack: bool, sink) -> None:
    result = asyncio.run(_generate(urls, ramp_s, until, ack))
    result["failed"] = dict(result["failed"])
    result["dropped"] = dict(result["dropped"])
    sink.put(result)


# --- watching the server ---------------------------------------------------------------------


class _Watcher(threading.Thread):
    """Polls ``/api/stats`` while the load runs, keeping the worst of each figure."""

    def __init__(self, base: str) -> None:
        super().__init__(daemon=True)
        self.base = base
        self.stop_event = threading.Event()
        self.samples: list[dict] = []

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                with urllib.request.urlopen(f"{self.base}/api/stats", timeout=5) as response:
                    self.samples.append(json.load(response))
            except (OSError, ValueError):
                self.samples.append({})
            self.stop_event.wait(2.0)

    def worst(self) -> dict:
        usable = [sample for sample in self.samples if sample]
        if not usable:
            return {}

        def over(path: tuple[str, ...], pick=max):
            values = []
            for sample in usable:
                value = sample
                for part in path:
                    value = value.get(part) if isinstance(value, dict) else None
                if isinstance(value, (int, float)):
                    values.append(value)
            return pick(values) if values else None

        return {
            "viewers": over(("audience", "viewers")),
            "loop_lag_p95_ms": over(("server", "loop_lag_ms", "p95")),
            "loop_lag_max_ms": over(("server", "loop_lag_ms", "max")),
            "cpu_percent": over(("server", "cpu_percent")),
            "rss_mb": over(("server", "rss_mb")),
            "open_files": over(("server", "open_files")),
            "open_files_limit": over(("server", "open_files_limit"), min),
            "send_ms_p95": over(("audience", "send_ms", "p95")),
            "send_ms_max": over(("audience", "send_ms", "max")),
            "skipped_lines": over(("audience", "skipped_lines")),
            "unreachable_polls": sum(1 for sample in self.samples if not sample),
        }


# --- reporting -------------------------------------------------------------------------------


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest rank, the same definition the server's own windows use."""

    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _merge(results: list[dict]) -> dict:
    arrivals: dict = {}
    connect_ms: list[float] = []
    connected = finished = 0
    failed: Counter = Counter()
    dropped: Counter = Counter()
    for result in results:
        for key, (count, first, last) in result["arrivals"].items():
            slot = arrivals.get(tuple(key))
            if slot is None:
                arrivals[tuple(key)] = [count, first, last]
            else:
                slot[0] += count
                slot[1] = min(slot[1], first)
                slot[2] = max(slot[2], last)
        connect_ms += result["connect_ms"]
        connected += result["connected"]
        finished += result["finished"]
        failed.update(result["failed"])
        dropped.update(result["dropped"])
    return {
        "arrivals": arrivals,
        "connect_ms": connect_ms,
        "connected": connected,
        "finished": finished,
        "failed": failed,
        "dropped": dropped,
    }


def _report(merged: dict, wanted: Counter, watcher: dict, steady_from: float) -> dict:
    arrivals = merged["arrivals"]
    # Phones join over a ramp, so only what was published once they had all had time to join
    # is judged. A phone that was slower than that is counted against delivery, which is fair.
    steady = {key: slot for key, slot in arrivals.items() if slot[1] >= steady_from}
    spreads = [(last - first) * 1000.0 for _, first, last in steady.values()]

    # A final is complete when every phone reading that language received it.
    finals = [(key, slot) for key, slot in steady.items() if key[3] == "final"]
    complete = [slot[0] >= wanted[key[0]] for key, slot in finals]
    delivered = sum(min(slot[0], wanted[key[0]]) for key, slot in finals)
    possible = sum(wanted[key[0]] for key, _ in finals)

    return {
        "phones_asked": sum(wanted.values()),
        "phones_connected": merged["connected"],
        "phones_stayed": merged["finished"],
        "connect_failures": dict(merged["failed"]),
        "unexpected_disconnects": dict(merged["dropped"]),
        "connect_ms_p50": round(statistics.median(merged["connect_ms"]), 1) if merged["connect_ms"] else None,
        "connect_ms_p95": round(_percentile(merged["connect_ms"], 0.95), 1) if merged["connect_ms"] else None,
        "messages_published": len(steady),
        "finals_published": len(finals),
        "finals_fully_delivered": sum(complete),
        "final_delivery_share": round(delivered / possible, 4) if possible else None,
        "fanout_spread_ms_p50": round(statistics.median(spreads), 1) if spreads else None,
        "fanout_spread_ms_p95": round(_percentile(spreads, 0.95), 1) if spreads else None,
        "fanout_spread_ms_max": round(max(spreads), 1) if spreads else None,
        "server": watcher,
    }


def _print(report: dict) -> None:
    server = report["server"]
    lines = [
        (
            f"phones           : {report['phones_connected']} of {report['phones_asked']} "
            f"connected, {report['phones_stayed']} stayed to the end"
        ),
        f"connect time     : median {report['connect_ms_p50']} ms, 95th {report['connect_ms_p95']} ms",
        (
            f"messages         : {report['messages_published']} published, "
            f"{report['finals_published']} final lines"
        ),
        (
            f"final delivery   : {report['final_delivery_share']} of phones received each line "
            f"({report['finals_fully_delivered']} lines reached everyone)"
        ),
        (
            f"fan-out spread   : median {report['fanout_spread_ms_p50']} ms, "
            f"95th {report['fanout_spread_ms_p95']} ms, worst {report['fanout_spread_ms_max']} ms "
            "(first phone to last, per message)"
        ),
    ]
    if report["connect_failures"] or report["unexpected_disconnects"]:
        lines.append(
            f"problems         : failed {report['connect_failures']}, "
            f"dropped {report['unexpected_disconnects']}"
        )
    if server:
        lines += [
            f"server loop lag  : 95th {server['loop_lag_p95_ms']} ms, worst {server['loop_lag_max_ms']} ms",
            f"server processor : up to {server['cpu_percent']}% of one core",
            f"server memory    : up to {server['rss_mb']} MB",
            f"server files     : up to {server['open_files']} open (limit {server['open_files_limit']})",
            f"server send time : 95th {server['send_ms_p95']} ms, worst {server['send_ms_max']} ms per batch",
            f"lines skipped    : {server['skipped_lines']} (phones that could not keep up)",
        ]
    print("\n".join(lines), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load a livesubs server with simulated phones.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--session", default=None, help="session id (default: ask the server)")
    parser.add_argument("--viewers", type=int, default=100, help="phones to connect")
    parser.add_argument("--arabic", type=int, default=20, help="percent of phones reading Arabic")
    parser.add_argument("--duration", type=float, default=30.0, help="seconds to stay connected")
    parser.add_argument("--ramp", type=float, default=None, help="seconds to connect them over")
    parser.add_argument("--processes", type=int, default=4, help="generator processes")
    parser.add_argument("--no-ack", action="store_true", help="do not acknowledge final lines")
    parser.add_argument("--json", default=None, help="also write the report to this file")
    args = parser.parse_args(argv)

    base = f"http://{args.host}:{args.port}"
    try:
        with urllib.request.urlopen(f"{base}/api/session", timeout=10) as response:
            session = json.load(response)
    except (OSError, ValueError) as error:
        print(f"could not reach {base}: {error}", file=sys.stderr)
        return 2

    session_id = args.session or session["id"]
    languages = session["languages"]
    second = languages[1] if len(languages) > 1 else languages[0]
    arabic = round(args.viewers * min(100, max(0, args.arabic)) / 100)
    roles = ["viewer"] * (args.viewers - arabic) + [f"viewer_{second}"] * arabic
    wanted = Counter({languages[0]: args.viewers - arabic, second: arabic})
    if second == languages[0]:
        wanted = Counter({languages[0]: args.viewers})

    ramp = args.ramp if args.ramp is not None else min(30.0, args.viewers / 100.0)
    urls = [f"ws://{args.host}:{args.port}/ws/{session_id}?role={role}" for role in roles]

    print(
        f"{args.viewers} phones ({wanted[languages[0]]} {languages[0]}, "
        f"{args.viewers - wanted[languages[0]]} {second}) over {ramp:.0f} s, "
        f"then {args.duration:.0f} s connected, from {args.processes} processes",
        flush=True,
    )

    watcher = _Watcher(base)
    watcher.start()

    context = multiprocessing.get_context("spawn")
    sink = context.Queue()
    started = time.monotonic()
    until = started + ramp + args.duration
    chunks = [urls[index :: args.processes] for index in range(args.processes)]
    processes = [
        context.Process(target=_worker, args=(chunk, ramp, until, not args.no_ack, sink))
        for chunk in chunks
        if chunk
    ]
    for process in processes:
        process.start()
    results = [sink.get(timeout=ramp + args.duration + 120) for _ in processes]
    for process in processes:
        process.join(timeout=30)

    watcher.stop_event.set()
    watcher.join(timeout=5)

    report = _report(_merge(results), wanted, watcher.worst(), started + ramp + 3.0)
    _print(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as out:
            json.dump(report, out, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
