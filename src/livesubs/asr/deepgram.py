"""Streaming speech recognition from Deepgram.

The adapter that turns the pipeline from a demonstration into the product. Everything
else already works; this is the stage that produces real text from real audio.

Transport is ``websockets`` directly rather than the vendor's SDK, because the design rule
in ``docs/04-adapters.md`` is that the adapter owns its transport and that no vendor type
or exception escapes it. What that buys is a PyPI dependency this project already has,
and a reconnect policy it controls.

Four decisions worth stating:

**``push`` never blocks.** The capture loop must not stall behind a slow socket, which is
the contract in ``asr/base.py``. Audio goes into a bounded queue drained by a sender task;
if the queue overflows, the *oldest* frame is dropped and counted rather than the capture
being held up. A replay faster than real time will do this deliberately, and the counter is
how the operator knows.

**Nothing is sent as base64.** Deepgram accepts raw binary frames, so the audio path stays
byte-for-byte what capture produced.

**``healthy`` reflects the socket, not the last transcript.** A TCP connection can die
without an error or a close frame, and a pipeline that keeps showing the last line while
the recogniser is gone is the one failure a viewer cannot distinguish from working. So a
silent socket is probed with a keepalive and then declared unhealthy.

**Only ``speech_final`` closes an utterance.** Deepgram also offers ``UtteranceEnd``, which
is a second, differently-timed endpointing signal; using both would double-endpoint, which
``docs/03-segmentation.md`` exists to prevent.

**A settled chunk and an endpoint are different facts, and both are reported.** Deepgram
sets ``is_final`` on every chunk as it settles, usually several times inside one utterance,
and the next result then starts from empty. That is ``kind="final"``. ``speech_final`` is the
speaker stopping, and travels separately. Collapsing the two either cut segments at every
chunk or, when ``is_final`` was ignored, left nothing to say that a span would not change.

**A conversation is one ``multi`` stream.** Nova-3 recognises speakers who switch
between some languages, English and German among them, in a single stream. Deepgram's
code-switching guide asks for 100 ms endpointing there, against a default of 10 ms, so a
conversation gets that unless the deployment chose a value. A conversation in a language the
model cannot switch into is refused before any socket opens.

**A transport that dies ends the event stream.** The pipeline learns a recogniser is gone
only because its events stop. Marking the adapter unhealthy and then waiting for ever left a
dropped socket silent for the rest of the lecture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from collections.abc import AsyncIterator, Sequence
from typing import Any

from livesubs.asr.base import AsrConfig, AsrEvent
from livesubs.asr.deepgram_languages import validate_selection
from livesubs.events import WordTiming
from livesubs.speakers import routing_language

__all__ = ["DEFAULT_URL", "DeepgramAsr"]

logger = logging.getLogger(__name__)

DEFAULT_URL = "wss://api.deepgram.com"

#: Frames buffered before audio is dropped. At 20 ms a frame this is four seconds: enough
#: to absorb a socket stall, small enough that a replay at speed cannot consume memory
#: without bound.
_QUEUE_FRAMES = 200

#: How long without a message before the socket is presumed dead. Deepgram sends a
#: keepalive every few seconds, so silence for this long is a symptom, not a lull.
_STALE_AFTER_S = 12.0

#: How long ``close`` waits for the provider's final transcript before giving up. Must stay
#: below the pipeline's own flush grace (``pipeline._FLUSH_GRACE_S``, 0.5 s), or the
#: utterance that was open when the audio stopped is lost.
_FLUSH_GRACE_S = 0.4

#: The endpointing Deepgram's code-switching guide recommends, in milliseconds.
_CODE_SWITCHING_ENDPOINTING_MS = 100


class DeepgramAsr:
    """An ``AsrProvider`` backed by Deepgram's streaming endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        url: str = DEFAULT_URL,
        model: str = "nova-3",
        language: str | None = None,
        sample_rate: int = 16_000,
        channels: int = 1,
        endpointing_ms: int | None = None,
        keyterms: Sequence[str] = (),
        connect_timeout_s: float = 10.0,
        queue_frames: int = _QUEUE_FRAMES,
    ) -> None:
        if not api_key:
            # Fail here rather than at the handshake: an empty key is a configuration
            # mistake, and the diagnosis belongs at startup where it is cheap.
            raise ValueError("Deepgram needs an API key; set DEEPGRAM_API_KEY or use --set-key")

        self.api_key = api_key
        self.url = url.rstrip("/")
        self.model = model
        #: An explicit override. Left unset, the stream is opened for the language the
        #: session's mode says the speaker uses, which arrives in the ``AsrConfig``.
        self.language = language
        self.sample_rate = sample_rate
        self.channels = channels
        self.endpointing_ms = endpointing_ms
        self.keyterms = tuple(keyterms)
        self.connect_timeout_s = connect_timeout_s

        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=queue_frames)
        self._events: asyncio.Queue[AsrEvent | None] = asyncio.Queue()
        self._socket: Any | None = None
        self._sender: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._open = False
        self._stale = False
        self._closed = asyncio.Event()
        self._last_error: str | None = None
        #: How many ``final`` events have arrived. A count rather than a flag, because
        #: ``close`` has to wait for a final that arrives *after* it asks for one: a flag
        #: already set by an earlier utterance would let it give up immediately and drop
        #: the flush it exists to collect.
        self._final_count = 0
        #: Whether the end-of-stream marker has been queued, so it is queued once.
        self._events_ended = False

        #: Audio frames discarded because the socket could not keep up. Surfaced rather
        #: than hidden: dropped audio is a real degradation, not a rounding error.
        self.dropped_frames = 0

    # --- the wire -------------------------------------------------------------

    def validate_config(self, cfg: AsrConfig) -> None:
        """Validate stream options at startup without opening a connection."""

        self._uri(cfg)

    def _uri(self, cfg: AsrConfig) -> str:
        """The listen URL, with every option the provider needs to decode correctly.

        ``encoding``, ``sample_rate`` and ``channels`` are not optional: without them
        Deepgram assumes a different format and the transcript is garbage that looks like a
        speech-recognition problem.
        """

        selected = validate_selection(self.model, cfg.language, self.language)
        conversation = selected == "multi"

        params = [
            ("model", self.model),
            ("language", selected),
            ("encoding", "linear16"),
            ("sample_rate", str(cfg.sample_rate)),
            ("channels", str(cfg.channels)),
            ("interim_results", "true"),
            ("punctuate", "true"),
            ("smart_format", "true"),
        ]

        endpointing = self.endpointing_ms
        if endpointing is None and conversation:
            endpointing = _CODE_SWITCHING_ENDPOINTING_MS
        if endpointing is not None:
            params.append(("endpointing", str(endpointing)))

        # Nova-3's vocabulary hints. Repeated rather than comma-joined, which is how the
        # API documents them.
        for term in self.keyterms or cfg.keyterms:
            params.append(("keyterm", term))

        query = "&".join(f"{key}={_quote(value)}" for key, value in params)
        return f"{self.url}/v1/listen?{query}"

    async def open(self, cfg: AsrConfig) -> None:
        """Connect. Raises on an auth failure so a bad key is found during setup."""

        import websockets

        # Built before connecting, so a conversation Deepgram cannot recognise is refused
        # as the configuration mistake it is rather than reported as a failed connection.
        uri = self._uri(cfg)

        try:
            self._socket = await websockets.connect(
                uri,
                additional_headers={"Authorization": f"Token {self.api_key}"},
                open_timeout=self.connect_timeout_s,
                max_size=None,
                ping_interval=20,
                ping_timeout=20,
            )
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            # Deliberately re-raised, unlike a mid-session failure: docs/04-adapters.md
            # promises that a misconfigured key fails fast at startup and never
            # mid-lecture.
            raise RuntimeError(
                f"could not open the Deepgram stream ({self.url}): {self._last_error}"
            ) from exc

        self._open = True
        self._stale = False
        self._closed.clear()
        self._sender = asyncio.create_task(self._send_loop(), name="livesubs:deepgram:tx")
        self._reader = asyncio.create_task(self._read_loop(), name="livesubs:deepgram:rx")
        logger.info("deepgram stream open (%s, %s)", self.model, self.url)

    async def push(self, pcm: bytes) -> None:
        """Queue audio. Never blocks, and never raises on a slow or dead socket."""

        if not self._open:
            raise RuntimeError("DeepgramAsr.push() before open()")

        if self._stale:
            # Nobody is sending. Queueing would only fill a queue that is never drained
            # and log a misleading "faster than the socket sends it" for every frame.
            self.dropped_frames += 1
            return

        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            # Drop the oldest frame, not the newest: the newest audio is the audio the
            # viewer is about to need. Counting the loss is the point; silently stalling
            # the capture loop would be worse.
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - only under a race
                pass
            self._queue.put_nowait(pcm)
            self.dropped_frames += 1
            if self.dropped_frames % 50 == 1:
                logger.warning(
                    "audio is arriving faster than the socket sends it: %d frames dropped",
                    self.dropped_frames,
                )

    def events(self) -> AsyncIterator[AsrEvent]:
        """Transcription updates, until :meth:`close`."""

        return self._event_stream()

    async def _event_stream(self) -> AsyncIterator[AsrEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    # --- the two tasks, one per direction ------------------------------------

    async def _send_loop(self) -> None:
        """Drain the audio queue into the socket.

        Separate from :meth:`push` so the capture loop never waits on the network, and
        separate from the reader so a stalled send cannot stop transcripts arriving.
        """

        socket = self._socket
        assert socket is not None  # set by open(); asserted for the type checker

        try:
            while True:
                chunk = await self._queue.get()
                if chunk is None:
                    return
                await socket.send(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._stale = True
            logger.warning("audio upload to Deepgram stopped: %s", self._last_error)
            # Audio can no longer leave, so nothing more will be recognised even if the
            # reader is still listening. Say so now rather than after its own timeout.
            self._end_events()

    async def _read_loop(self) -> None:
        """Turn provider messages into events, and notice a socket that has gone quiet."""

        socket = self._socket
        assert socket is not None

        misses = 0

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=_STALE_AFTER_S)
                    misses = 0
                except TimeoutError:
                    misses += 1
                    # A dead TCP connection produces neither data nor an error, so the
                    # probe is the only way to tell "quiet" from "gone".
                    if misses >= 2:
                        self._stale = True
                        logger.error(
                            "no message from Deepgram for %.0fs twice; treating the stream "
                            "as dead so the operator is told rather than left guessing",
                            _STALE_AFTER_S,
                        )
                        return
                    try:
                        await socket.send('{"type": "KeepAlive"}')
                    except Exception:  # noqa: BLE001
                        self._stale = True
                        return
                    continue

                event = _parse_message(raw)
                if event is not None:
                    if event.kind == "final":
                        self._final_count += 1
                    await self._events.put(event)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead socket is a state, not a crash
            self._last_error = f"{type(exc).__name__}: {exc}"
            # A close we asked for is not a fault, and logging it as a warning would train
            # the operator to ignore the one line that means the recogniser is gone.
            if self._open:
                self._stale = True
                logger.warning("Deepgram stream ended: %s", self._last_error)
            else:
                logger.debug("Deepgram stream closed: %s", self._last_error)
        finally:
            # However the reader stopped, no further transcript will arrive from this
            # socket. Everything it already delivered is ahead of the marker in the
            # queue, so a final result that landed just before the hang-up still counts.
            # `close()` ends the stream itself after collecting its flush, so a close we
            # asked for is left to it.
            if self._open:
                self._stale = True
                self._end_events()

    # --- lifecycle -----------------------------------------------------------

    @property
    def healthy(self) -> bool:
        """Whether the stream is usable right now.

        Reflects the connection rather than the last transcript, so a silently dead socket
        is detectable - which is what failover and the operator's status pill depend on.
        """

        return self._open and not self._stale and not self._closed.is_set()

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def close(self) -> None:
        """Ask for the final transcript, then end the event stream.

        The flush is not politeness. A lecture that stops mid-sentence would otherwise lose
        its last line, and the same omission in a real adapter would be invisible until it
        mattered. ``CloseStream`` is what makes Deepgram commit the open utterance.
        """

        if not self._open:
            return

        self._open = False
        socket, self._socket = self._socket, None

        try:
            if socket is not None:
                already_final = self._final_count
                try:
                    await socket.send('{"type": "CloseStream"}')
                    await asyncio.wait_for(
                        self._await_final(already_final), timeout=_FLUSH_GRACE_S
                    )
                except TimeoutError:
                    logger.info("no final transcript within %.1fs of CloseStream", _FLUSH_GRACE_S)
                except Exception as exc:  # noqa: BLE001 - a failed flush must not stop shutdown
                    logger.debug("Deepgram close was not clean: %s", exc)

                try:
                    await socket.close()
                except Exception as exc:  # noqa: BLE001 - closing a dead socket is fine
                    logger.debug("Deepgram socket close failed: %s", exc)
        finally:
            # Whatever went wrong above, the tasks are stopped and the stream is ended.
            # A dead socket leaves the send queue full, which used to make the sentinel
            # put raise and skip all of this.
            self._stop_sender()
            for task in (self._sender, self._reader):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (self._sender, self._reader) if task is not None),
                return_exceptions=True,
            )

            self._closed.set()
            self._end_events()

    def _stop_sender(self) -> None:
        """Wake the sender with its stop marker, making room if the queue is full."""

        while True:
            try:
                self._queue.put_nowait(None)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - only under a race
                    return

    def _end_events(self) -> None:
        """Queue the end-of-stream marker exactly once."""

        if self._events_ended:
            return
        self._events_ended = True
        self._events.put_nowait(None)

    async def _await_final(self, before: int) -> None:
        """Wait for a ``final`` that arrived after the flush was requested."""

        while True:
            if self._reader is not None and self._reader.done():
                return
            if self._final_count > before:
                return
            await asyncio.sleep(0.02)


def _quote(value: str) -> str:
    """Percent-encode a query value. Vocabulary terms contain spaces and punctuation."""

    return urllib.parse.quote(str(value), safe="")


def _parse_message(raw: str | bytes) -> AsrEvent | None:
    """One provider message to one event, or ``None`` when it carries no transcript.

    ``None`` covers more than malformed input: Deepgram sends ``Metadata``,
    ``SpeechStarted``, ``UtteranceEnd`` and keepalives on the same socket, and empty
    ``Results`` for silence. Turning those into events would put empty strings in front of
    the segmenter and, worse, let a stray endpointing signal close an utterance.
    """

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")

    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        logger.debug("ignoring a Deepgram message that is not JSON (%d bytes)", len(raw))
        return None

    if not isinstance(message, dict) or message.get("type") != "Results":
        return None

    alternatives = (message.get("channel") or {}).get("alternatives") or []
    alternative: dict[str, Any] = alternatives[0] if alternatives else {}

    text = (alternative.get("transcript") or "").strip()
    speech_final = bool(message.get("speech_final"))

    if not text and not speech_final:
        return None

    words = [
        WordTiming(
            # What the viewer reads: capitals and punctuation, not the bare word. Segment
            # text is rebuilt from these when a segment is cut part-way through a chunk.
            text=str(word.get("punctuated_word") or word.get("word", "")),
            start_ms=max(0, round(float(word.get("start", 0.0)) * 1000)),
            end_ms=max(0, round(float(word.get("end", 0.0)) * 1000)),
            confidence=word.get("confidence"),
            language=_word_language(word.get("language")),
        )
        for word in alternative.get("words") or []
        if word.get("word") and isinstance(word.get("start"), (int, float))
        and isinstance(word.get("end"), (int, float))
    ]

    if words:
        # The words' own span, not ``start``/``duration``: the latter covers the provider's
        # padded window, which would put a cue on screen before the first word.
        start_ms, end_ms = words[0].start_ms, words[-1].end_ms
    else:
        start_s = float(message.get("start") or 0.0)
        start_ms = max(0, round(start_s * 1000))
        end_ms = max(start_ms, round((start_s + float(message.get("duration") or 0.0)) * 1000))

    is_final = bool(message.get("is_final"))

    return AsrEvent(
        # ``final`` means this span is settled: its text will not change, and the next
        # result starts a new span instead of repeating it. Deepgram says so with
        # ``is_final``, which it sets on every chunk as it settles. An endpoint settles
        # the span too, whichever flag happened to arrive.
        #
        # That is deliberately NOT the same as ``speech_final``, which is the speaker
        # stopping and travels separately. Using ``is_final`` as the endpoint cut
        # segments at every chunk, one chunk's text at a time; using only
        # ``speech_final`` threw away the fact that earlier chunks of the same utterance
        # were settled and complete, so a segment cut after a later chunk lost them.
        kind="final" if (is_final or speech_final) else "partial",
        text=text,
        start_ms=start_ms,
        end_ms=end_ms,
        confidence=alternative.get("confidence"),
        words=words or None,
        speech_final=speech_final,
    )


def _word_language(value: Any) -> str | None:
    """Normalise provider tags without discarding unsupported explicit evidence."""

    if value is None:
        return None
    return routing_language(str(value))
