"""Measure the overlay in a real browser, at the sizes attendees actually use.

The overlay's layout faults do not show up by reading the CSS, and they do not show up in
the Python test suite either, because they are properties of a *render*: text can be
deleted by a line clamp, or laid out past the bottom edge of the screen, while every class
name and every number in the DOM looks perfectly correct. Both of those shipped, and a
phone found them.

So this renders the real stylesheet and the real overlay module in headless Chrome, feeds
them the lines the simulated lecture actually sends, and fails if any line is clipped or
off screen. It also checks that the block's bottom edge sits at the overlay's anchor,
because "off the bottom of the display" and "correctly placed" differ by one CSS property
that is easy to lose again.

Needs `node` and a Chrome or Chromium binary, so it is optional tooling rather than part
of the test suite:

    uv run python tools/check_overlay_layout.py
    uv run python tools/check_overlay_layout.py --shot /tmp/phone.png
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

__all__ = ["VIEWPORTS", "main"]

#: The render also depends on viewport height, and a phone is the case that exposed the
#: faults, so the sizes cover one small phone, one typical phone and one laptop.
VIEWPORTS: tuple[tuple[str, int, int], ...] = (
    ("small phone", 360, 640),
    ("phone", 390, 844),
    ("laptop", 1440, 900),
)

#: Where the overlay root sits, from `inset: auto 0 8vh 0` in styles.css. The block's
#: bottom edge must be exactly here rather than at the viewport edge.
ANCHOR_VH = 8.0

#: Slack allowed on the geometric checks, in pixels. A line box is a fractional height, and
#: `getBoundingClientRect` rounds it, so a text edge can land 1px outside its box without
#: anything being wrong. Without this the box-mode padding hides the rounding and the
#: shadow mode exposes it, which is a difference between backdrops rather than a fault.
TOLERANCE_PX = 1

#: Browsers to try, in order. The viewport is set over the DevTools protocol rather than
#: with `--window-size`, because the Chrome builds this was written against ignore that
#: flag entirely and always report 500x701.
CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)

#: The page under test. It declares the same viewport meta as the viewer page, because
#: without it mobile emulation falls back to a 980px layout viewport and the measurement
#: describes a tablet instead of the phone the report came from.
HARNESS_HTML = """<!doctype html>
<html lang="de">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <title>overlay layout check</title>
    <link rel="stylesheet" href="./static/styles.css" />
  </head>
  <body class="__BODY_CLASS__">
    <div id="root" class="overlay-root"></div>
    <pre id="out">pending</pre>
    <script type="module">
      import { SubtitleOverlay } from "./static/overlay.js";

      const LINES = __LINES__;

      const overlay = new SubtitleOverlay(document.getElementById("root"), {
        prefs: { fontScale: 1, position: "__POSITION__", backdrop: "__BACKDROP__" },
      });

      const rows = LINES.map((text, index) => {
        // Far enough apart that the pacing policy accepts every line as new.
        overlay.offer(
          { seq: index, rev: 0, state: "final", de: text, src: null },
          performance.now() + index * 60_000,
        );

        const layer = overlay.activeLayer;
        const box = layer.querySelector(".subtitle");
        const span = box.querySelector(".subtitle-text");
        const boxRect = box.getBoundingClientRect();
        const range = document.createRange();
        range.selectNodeContents(span);
        const textRect = range.getBoundingClientRect();

        return {
          chars: text.length,
          fontPx: Math.round(parseFloat(getComputedStyle(box).fontSize) * 10) / 10,
          lines: span.getClientRects().length,
          boxHeightPx: Math.round(boxRect.height),
          boxTopPx: Math.round(boxRect.top),
          boxBottomPx: Math.round(boxRect.bottom),
          // Signed, so overflow shows as a positive number rather than being hidden by a
          // clamp on the way out.
          bottomSlackPx: Math.round(boxRect.bottom - textRect.bottom),
          rightSlackPx: Math.round(boxRect.right - textRect.right),
          // Against the screen, which is what the reader experiences. Negative means the
          // text is off the bottom of the display, the failure a box-relative number
          // cannot see at all.
          viewportSlackPx: Math.round(window.innerHeight - textRect.bottom),
          clippedByOverflow: box.scrollHeight > box.clientHeight + 1,
          textPresent: span.textContent.length === text.length,
        };
      });

      // Reported once the page has settled rather than immediately, because anything that
      // blanks a line *after* it is swapped in - a stale fade-out callback, say - is
      // invisible to the per-row measurements above and would otherwise show up only in
      // a screenshot, as an empty frame that looks like a rendering quirk.
      window.setTimeout(() => {
        const layers = [...document.querySelectorAll(".subtitle-layer")];
        const visible = layers.filter((layer) => layer.classList.contains("is-visible"));

        document.getElementById("out").textContent = JSON.stringify({
          viewport: `${window.innerWidth}x${window.innerHeight}`,
          opacity: getComputedStyle(overlay.activeLayer).opacity,
          settled: {
            visibleLayers: visible.length,
            visibleTextLengths: visible.map(
              (layer) => layer.querySelector(".subtitle-text").textContent.length,
            ),
            lastLineLength: LINES[LINES.length - 1].length,
          },
          rows,
        });
      }, 1000);
    </script>
  </body>
</html>
"""


#: The DevTools-protocol driver. `--window-size` is ignored by the Chrome builds this was
#: written against, so the viewport is set with an emulation override instead.
MEASURE_JS = """\
import { writeFileSync } from "node:fs";

const [, , widthArg, heightArg, url, shotPath, clipArg] = process.argv;
const debugPort = process.env.LIVESUBS_CHROME_PORT ?? "9222";

const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
const page = targets.find((target) => target.type === "page");
if (!page) throw new Error("no page target on the debugging port");

const socket = new WebSocket(page.webSocketDebuggerUrl);
let nextId = 1;
const pending = new Map();

function send(method, params = {}) {
  const id = nextId++;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

socket.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result);
  }
});

await new Promise((resolve) => socket.addEventListener("open", resolve));

await send("Emulation.setDeviceMetricsOverride", {
  width: Number(widthArg),
  height: Number(heightArg),
  deviceScaleFactor: 1,
  // Deliberately not mobile emulation. With `mobile: true` this Chrome build paints
  // nothing anchored to the bottom of the viewport - not even a plain control square -
  // while the layout viewport still reports its full size, which turns a layout check
  // into a check of the screenshot method. The page's viewport meta fixes the layout
  // width either way, so the layout under test is identical.
  mobile: false,
});
// Headless does not advance the fade-in transition, so every layer would stay at opacity
// 0 and a screenshot would be blank while the DOM said otherwise.
await send("Emulation.setEmulatedMedia", {
  features: [{ name: "prefers-reduced-motion", value: "reduce" }],
});
await send("Page.enable");
await send("Page.navigate", { url });
// Generous: the page is three modules deep and the measurement must not race the fit.
await new Promise((resolve) => setTimeout(resolve, 1500));

const measured = await send("Runtime.evaluate", {
  expression: 'document.getElementById("out")?.textContent ?? "no #out element"',
  returnByValue: true,
});
console.log(measured.result.value);

if (shotPath && shotPath !== "-") {
  const clip = clipArg
    ? (() => {
        const [x, y, width, height, scale] = clipArg.split(",").map(Number);
        return { x, y, width, height, scale: scale || 1 };
      })()
    : undefined;
  const shot = await send("Page.captureScreenshot", { format: "png", clip });
  writeFileSync(shotPath, Buffer.from(shot.data, "base64"));
}

socket.close();
process.exit(0);
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for(url: str, timeout: float) -> bool:
    """Chrome takes a moment to accept connections on its debugging port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _lines_under_test() -> list[str]:
    """The lines the simulated lecture actually sends, so the check tracks the content.

    Reads `livesubs.sim` rather than copying the strings, because a stale copy is how a
    layout check quietly stops testing anything.
    """

    from livesubs.sim import SCRIPTS

    script = SCRIPTS["default"]
    return [segment.de_final for segment in script.segments]


def _write_harness(directory: Path, *, position: str, backdrop: str) -> Path:
    body_class = "is-top-host" if position == "top" else ""
    html = (
        HARNESS_HTML.replace("__LINES__", json.dumps(_lines_under_test(), ensure_ascii=False))
        .replace("__POSITION__", position)
        .replace("__BACKDROP__", backdrop)
        .replace("__BODY_CLASS__", body_class)
    )
    (directory / "index.html").write_text(html, encoding="utf-8")
    script = directory / "measure.mjs"
    script.write_text(MEASURE_JS, encoding="utf-8")
    return script


def _measure(
    *,
    node: str,
    script: Path,
    url: str,
    width: int,
    height: int,
    chrome_port: int,
    shot: Path | None,
    clip: str | None,
) -> dict:
    import os

    environment = {**os.environ, "LIVESUBS_CHROME_PORT": str(chrome_port)}
    completed = subprocess.run(
        [node, str(script), str(width), str(height), url, str(shot or "-"), clip or ""],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        check=False,
    )
    if "{" not in completed.stdout:
        raise RuntimeError(
            f"the browser driver returned nothing usable:\n"
            f"{completed.stdout}\n{completed.stderr}".strip()
        )
    return json.loads(completed.stdout[completed.stdout.index("{") :])


def _problems(measured: dict, *, height: int, position: str) -> list[str]:
    """Every way the render can be wrong, as sentences rather than booleans."""

    if measured.get("opacity") != "1":
        # Nothing else means anything if the overlay never became visible.
        return [
            (
                f"the overlay layer is at opacity {measured.get('opacity')}, not 1, "
                "so nothing below was actually measured"
            )
        ]

    if position == "bottom":
        anchor = round(height * (1 - ANCHOR_VH / 100))
        edge_key, side = "boxBottomPx", "bottom"
    else:
        anchor = round(height * ANCHOR_VH / 100)
        edge_key, side = "boxTopPx", "top"

    problems: list[str] = []

    settled = measured.get("settled") or {}
    if settled.get("visibleLayers") != 1:
        problems.append(
            f"{settled.get('visibleLayers')} layers are visible once the page settles; "
            "exactly one should be"
        )
    elif settled.get("lastLineLength") not in settled.get("visibleTextLengths", []):
        problems.append(
            "the line on screen was emptied after it appeared: the visible layer holds "
            f"{settled.get('visibleTextLengths')} characters, not "
            f"{settled.get('lastLineLength')}"
        )

    for index, row in enumerate(measured["rows"]):
        where = f"line {index + 1} ({row['chars']} chars, {row['fontPx']}px, {row['lines']} lines)"

        if not row["textPresent"]:
            problems.append(f"{where}: the DOM does not hold the whole line")
        if row["clippedByOverflow"]:
            problems.append(f"{where}: the box is clipping its own content")
        if row["bottomSlackPx"] < -TOLERANCE_PX:
            problems.append(f"{where}: {-row['bottomSlackPx']}px of text below its own box")
        if row["rightSlackPx"] < -TOLERANCE_PX:
            problems.append(f"{where}: {-row['rightSlackPx']}px of text past the right edge")
        if row["viewportSlackPx"] < -TOLERANCE_PX:
            problems.append(f"{where}: {-row['viewportSlackPx']}px of text off the screen")
        if abs(row[edge_key] - anchor) > TOLERANCE_PX:
            problems.append(
                f"{where}: block {side} edge at {row[edge_key]}px, but the anchor is "
                f"{anchor}px ({ANCHOR_VH}vh from the {side} edge)"
            )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--top",
        action="store_true",
        help="check the top placement instead of the bottom (the viewer can choose either)",
    )
    parser.add_argument(
        "--shot",
        metavar="PATH",
        help="also write a screenshot of the phone viewport to PATH, to look at",
    )
    parser.add_argument(
        "--clip",
        metavar="X,Y,W,H[,SCALE]",
        help="capture only this region of the screenshot, optionally scaled up",
    )
    parser.add_argument(
        "--backdrop",
        choices=("box", "shadow", "outline"),
        default="box",
        help=(
            "backdrop mode to measure (default: box, which is also the viewer default). "
            "Headless Chrome does not paint the box mode's backdrop-filter, so a "
            "screenshot needs --backdrop shadow; the measurements are unaffected"
        ),
    )
    args = parser.parse_args(argv)

    node = shutil.which("node")
    chrome = next(
        (found for name in CHROME_CANDIDATES if (found := shutil.which(name))), None
    )
    if node is None or chrome is None:
        missing = "node" if node is None else "a Chrome or Chromium binary"
        print(f"{missing} is not installed, so the overlay cannot be rendered here.")
        print("This check is optional: the test suite never needs a browser.")
        return 2

    repo = Path(__file__).resolve().parent.parent
    static = repo / "web" / "static"
    if not static.is_dir():
        print(f"no frontend at {static}", file=sys.stderr)
        return 2

    position = "top" if args.top else "bottom"
    if args.shot and args.backdrop == "box":
        print(
            "note: the box backdrop uses backdrop-filter, which headless Chrome does not\n"
            "      paint, so the screenshot will be empty even though the numbers are\n"
            "      right. Pass --backdrop shadow for a picture.",
            file=sys.stderr,
        )
    failures = 0

    with tempfile.TemporaryDirectory(prefix="livesubs-overlay-") as raw:
        work = Path(raw)
        (work / "static").symlink_to(static)
        script = _write_harness(work, position=position, backdrop=args.backdrop)

        http_port, chrome_port = _free_port(), _free_port()
        server = subprocess.Popen(
            [
                sys.executable, "-m", "http.server", str(http_port),
                "--bind", "127.0.0.1", "--directory", str(work),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        browser = subprocess.Popen(
            [
                chrome,
                "--headless",
                "--no-sandbox",
                # No --hide-scrollbars, and no --disable-gpu either: this build leaves
                # the screenshot blank for anything anchored to the bottom of the
                # viewport when they are passed. The measurements are unaffected, because
                # they come from the DOM, so the symptom is a correct report next to an
                # empty picture - a good way to spend an hour doubting a layout that is
                # fine.
                "--enable-unsafe-swiftshader",
                f"--remote-debugging-port={chrome_port}",
                f"--user-data-dir={work / 'profile'}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            time.sleep(1.0)
            if not _wait_for(f"http://127.0.0.1:{chrome_port}", timeout=20):
                print("Chrome never opened its debugging port", file=sys.stderr)
                return 2

            print(f"overlay layout, {position} placement", flush=True)
            print("  viewport       size        font          lines   verdict")
            for name, width, height in VIEWPORTS:
                shot = Path(args.shot) if args.shot and name == "phone" else None
                measured = _measure(
                    node=node,
                    script=script,
                    url=f"http://127.0.0.1:{http_port}/index.html",
                    width=width,
                    height=height,
                    chrome_port=chrome_port,
                    shot=shot,
                    clip=args.clip,
                )
                problems = _problems(measured, height=height, position=position)
                failures += len(problems)

                fonts = sorted({row["fontPx"] for row in measured["rows"]})
                counts = sorted({row["lines"] for row in measured["rows"]})
                size = f"{width}x{height}"
                font = f"{fonts[0]:g}-{fonts[-1]:g}px"
                lines = f"{counts[0]}-{counts[-1]}"
                verdict = "ok" if not problems else f"{len(problems)} problem(s)"
                print(
                    f"  {name:<14} {size:<11} {font:<13} {lines:<7} {verdict}",
                    flush=True,
                )
                for problem in problems:
                    print(f"      - {problem}")
        finally:
            browser.terminate()
            server.terminate()
            browser.wait(timeout=10)
            server.wait(timeout=10)

    if failures:
        print(f"\n{failures} layout problem(s).", file=sys.stderr)
        return 1

    print("\nthe overlay shows every line, in full, inside the screen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
