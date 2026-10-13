"""Tests for the Deepgram adapter.

Two layers, and the split matters: the message parser is tested directly, because it is
pure and it decides what the segmenter sees, while the transport is tested against a local
fake that speaks the same protocol. What these tests cannot show is whether the *real*
endpoint's message shapes match what is parsed here - that needs a key, and the first live
run is where it will be confirmed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from livesubs.asr.base import AsrConfig, AsrEvent
from livesubs.asr.deepgram import DeepgramAsr, _parse_message

# --- the parser --------------------------------------------------------------


def _results(**overrides: object) -> str:
    payload = {
        "type": "Results",
        "is_final": False,
        "speech_final": False,
        "start": 1.0,
        "duration": 0.5,
        "channel": {
            "alternatives": [{"transcript": "hello there", "confidence": 0.94, "words": []}]
        },
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_a_partial_results_message_becomes_a_partial_event() -> None:
    event = _parse_message(_results())

    assert event is not None
    assert event.kind == "partial"
    assert event.text == "hello there"
    assert event.confidence == pytest.approx(0.94)


def test_a_settled_chunk_is_final_but_is_not_an_endpoint() -> None:
    """``final`` means the span is settled; ``speech_final`` is the separate endpoint hint.

    Deepgram marks each chunk ``is_final`` as it settles, usually several times inside
    one utterance. Treating that as an endpoint cut segments at every chunk, which was a
    real bug. Dropping it altogether lost the one piece of information that tells the
    transcript assembler a span will not change and the next event starts a new one, so
    both facts are carried, separately.
    """

    event = _parse_message(_results(is_final=True, speech_final=False))

    assert event is not None
    assert event.kind == "final"
    assert event.speech_final is False


def test_an_interim_result_is_a_partial() -> None:
    event = _parse_message(_results(is_final=False, speech_final=False))

    assert event is not None
    assert event.kind == "partial"


def test_speech_final_is_carried_through_as_the_endpointing_hint() -> None:
    """The segmenter treats it as a hint, and it implies the span is settled."""

    event = _parse_message(_results(is_final=True, speech_final=True))

    assert event is not None
    assert event.kind == "final"
    assert event.speech_final is True

    # Provider documentation shows speech_final on a result that is not yet is_final.
    # An endpoint settles the span whichever flag arrived.
    odd = _parse_message(_results(is_final=False, speech_final=True))
    assert odd is not None and odd.kind == "final"


def test_words_keep_their_punctuation_and_capitals() -> None:
    """Segment text is rebuilt from the words, so they must carry what the viewer reads."""

    raw = _results(
        is_final=True,
        channel={
            "alternatives": [
                {
                    "transcript": "Hello, there.",
                    "words": [
                        {"word": "hello", "punctuated_word": "Hello,", "start": 0.0, "end": 0.4},
                        {"word": "there", "punctuated_word": "there.", "start": 0.4, "end": 0.8},
                    ],
                }
            ]
        },
    )

    event = _parse_message(raw)

    assert event is not None
    assert [word.text for word in event.words or []] == ["Hello,", "there."]


def test_word_timings_are_preferred_over_the_result_window() -> None:
    """``start``/``duration`` covers the provider's padded window, the words do not.

    A cue built from the window appears on screen before the first word is spoken, which
    is the kind of offset nobody can name but everybody notices.
    """

    raw = _results(
        start=10.0,
        duration=2.0,
        is_final=True,
        channel={
            "alternatives": [
                {
                    "transcript": "hello there",
                    "words": [
                        {"word": "hello", "start": 10.4, "end": 10.8, "confidence": 0.9},
                        {"word": "there", "start": 10.8, "end": 11.2, "confidence": 0.8},
                    ],
                }
            ]
        },
    )

    event = _parse_message(raw)

    assert event is not None
    assert (event.start_ms, event.end_ms) == (10_400, 11_200)
    assert [word.text for word in event.words or []] == ["hello", "there"]


def test_seconds_are_converted_to_milliseconds_without_the_window_being_used() -> None:
    event = _parse_message(_results(start=1.5, duration=0.25))

    assert event is not None
    assert (event.start_ms, event.end_ms) == (1500, 1750)


def test_an_empty_transcript_is_not_an_event() -> None:
    """Silence produces empty results, and an empty string must not reach the segmenter."""

    assert _parse_message(_results(channel={"alternatives": [{"transcript": ""}]})) is None


def test_metadata_and_keepalives_are_not_events() -> None:
    for kind in ("Metadata", "KeepAlive", "SpeechStarted", "UtteranceEnd"):
        assert _parse_message(json.dumps({"type": kind})) is None


def test_utterance_end_does_not_close_an_utterance() -> None:
    """Using it as well as ``speech_final`` would double-endpoint.

    ``docs/03-segmentation.md`` exists because two endpointing signals produce two
    fragments where there should be one sentence.
    """

    assert _parse_message(json.dumps({"type": "UtteranceEnd", "last_word_end": 12.0})) is None


def test_malformed_json_is_ignored_rather_than_fatal() -> None:
    for raw in ("not json", "", "[]", "null"):
        assert _parse_message(raw) is None


def test_bytes_are_decoded() -> None:
    event = _parse_message(_results().encode())

    assert event is not None and event.text == "hello there"


# --- the wire ----------------------------------------------------------------


def _config() -> AsrConfig:
    return AsrConfig(language="en", sample_rate=16_000, channels=1)


def test_the_uri_declares_the_format_the_audio_is_in() -> None:
    """Without ``encoding``/``sample_rate``/``channels`` the transcript is garbage."""

    uri = DeepgramAsr(api_key="k", keyterms=("Rote Kreuz",))._uri(_config())

    assert "model=nova-3" in uri
    assert "encoding=linear16" in uri
    assert "sample_rate=16000" in uri
    assert "channels=1" in uri
    assert "interim_results=true" in uri
    assert "keyterm=Rote%20Kreuz" in uri


def test_an_empty_key_is_refused_before_any_socket_is_opened() -> None:
    with pytest.raises(ValueError):
        DeepgramAsr(api_key="")


def test_the_adapter_satisfies_the_asr_protocol() -> None:
    from livesubs.asr.base import AsrProvider

    assert isinstance(DeepgramAsr(api_key="k"), AsrProvider)


# --- transport, against a local fake -----------------------------------------


def _spoken(transcript: str, *, final: bool, speech_final: bool) -> str:
    return json.dumps(
        {
            "type": "Results",
            "is_final": final,
            "speech_final": speech_final,
            "start": 0.0,
            "duration": 1.0,
            "channel": {"alternatives": [{"transcript": transcript, "confidence": 0.9}]},
        }
    )


@asynccontextmanager
async def _fake_deepgram(handler) -> AsyncIterator[tuple[str, list[tuple[str, object]]]]:
    """A local endpoint that speaks enough of the protocol for the adapter to talk to it."""

    from websockets.asyncio.server import serve

    seen: list[tuple[str, object]] = []

    async def wrapper(connection) -> None:
        seen.append(("path", connection.request.path))
        seen.append(("auth", connection.request.headers.get("Authorization")))
        await handler(connection, seen)

    async with serve(wrapper, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}", seen


async def _answers_after_two_frames(connection, seen) -> None:
    frames = 0
    async for message in connection:
        if isinstance(message, bytes):
            frames += 1
            seen.append(("frame", len(message)))
            if frames == 1:
                await connection.send(_spoken("hello", final=False, speech_final=False))
            else:
                await connection.send(_spoken("hello there", final=True, speech_final=True))
        elif isinstance(message, str) and "CloseStream" in message:
            await connection.send(_spoken("hello there.", final=True, speech_final=True))
            return


async def test_audio_goes_in_and_transcripts_come_out() -> None:
    async with _fake_deepgram(_answers_after_two_frames) as (uri, seen):
        asr = DeepgramAsr(api_key="secret", url=uri, connect_timeout_s=2)

        await asr.open(_config())
        assert asr.healthy

        events: list[AsrEvent] = []

        async def collect() -> None:
            async for event in asr.events():
                events.append(event)

        collector = asyncio.create_task(collect())

        await asr.push(b"\x00" * 640)
        await asyncio.sleep(0.1)
        await asr.push(b"\x01" * 640)
        await asyncio.sleep(0.1)

        await asr.close()
        await asyncio.wait_for(collector, timeout=2)

    assert [event.kind for event in events] == ["partial", "final", "final"]
    assert events[-1].text == "hello there."
    assert events[-1].speech_final is True
    assert ("auth", "Token secret") in seen
    assert "/v1/listen?" in str(seen[0][1])


async def test_close_waits_for_the_final_so_the_last_line_is_not_lost() -> None:
    """The lecture stops mid-sentence: that sentence still has to reach the screen."""

    async def never_speaks(connection, seen) -> None:
        async for message in connection:
            if isinstance(message, str) and "CloseStream" in message:
                await connection.send(_spoken("the last words", final=True, speech_final=True))
                return

    async with _fake_deepgram(never_speaks) as (uri, _seen):
        asr = DeepgramAsr(api_key="k", url=uri)
        await asr.open(_config())

        events: list[AsrEvent] = []

        async def collect() -> None:
            async for event in asr.events():
                events.append(event)

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        await asr.close()
        await asyncio.wait_for(collector, timeout=2)

    assert [event.text for event in events] == ["the last words"]


async def test_a_socket_that_dies_is_reported_unhealthy() -> None:
    """The failure a viewer cannot distinguish from working: text stops, the page does not."""

    async def hang_up(connection, seen) -> None:
        return

    async with _fake_deepgram(hang_up) as (uri, _seen):
        asr = DeepgramAsr(api_key="k", url=uri)
        await asr.open(_config())
        assert asr.healthy

        await asyncio.sleep(0.2)

        assert not asr.healthy
        assert asr.last_error is not None


async def test_a_rejected_key_fails_at_open_rather_than_mid_lecture() -> None:
    """docs/04-adapters.md promises the misconfiguration is found during setup."""

    async def unauthorised(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(2048)
        writer.write(b"HTTP/1.1 401 Unauthorized\r\ncontent-length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(unauthorised, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    try:
        asr = DeepgramAsr(api_key="wrong", url=f"ws://127.0.0.1:{port}", connect_timeout_s=2)
        with pytest.raises(RuntimeError, match="could not open"):
            await asr.open(_config())
    finally:
        server.close()
        await server.wait_closed()


async def test_push_never_blocks_and_counts_dropped_audio() -> None:
    """A replay faster than real time must drop audio, not stall the capture loop.

    The sender task is deliberately not running here, which is what a stalled socket looks
    like from ``push``'s point of view. Audio is the one thing that must not be allowed to
    hold up the pipeline.
    """

    asr = DeepgramAsr(api_key="k", queue_frames=4)
    asr._open = True  # the point of the test is the queue, not the socket

    for _ in range(10):
        await asr.push(b"\x00" * 8)

    assert asr.dropped_frames == 6
    assert asr._queue.qsize() == 4


# --- a transport that dies must be visible to the pipeline --------------------------


async def _hangs_up_after_one_frame(connection, seen) -> None:
    async for message in connection:
        if isinstance(message, bytes):
            await connection.send(_spoken("hello", final=True, speech_final=True))
            await connection.close(code=1011, reason="internal error")
            return


async def _collect_until_end(asr: DeepgramAsr, timeout: float) -> list[AsrEvent]:
    events: list[AsrEvent] = []

    async def collect() -> None:
        async for event in asr.events():
            events.append(event)

    await asyncio.wait_for(collect(), timeout=timeout)
    return events


async def test_the_event_stream_ends_when_the_provider_hangs_up() -> None:
    """The pipeline learns a recogniser died only because its stream ends.

    The adapter used to mark itself unhealthy and then wait for ever, so a dropped socket
    mid-lecture silently stopped the subtitles: nothing ended the stream, and nothing
    else read ``healthy``.
    """

    async with _fake_deepgram(_hangs_up_after_one_frame) as (uri, _seen):
        asr = DeepgramAsr(api_key="k", url=uri)
        await asr.open(_config())
        await asr.push(b"\x00" * 640)

        events = await _collect_until_end(asr, timeout=3)

        assert [event.text for event in events] == ["hello"], "what arrived is still delivered"
        assert not asr.healthy
        await asr.close()


async def test_the_event_stream_ends_when_the_socket_goes_silent(monkeypatch) -> None:
    from livesubs.asr import deepgram

    monkeypatch.setattr(deepgram, "_STALE_AFTER_S", 0.05)

    async def says_nothing(connection, seen) -> None:
        async for _ in connection:
            pass

    async with _fake_deepgram(says_nothing) as (uri, _seen):
        asr = DeepgramAsr(api_key="k", url=uri)
        await asr.open(_config())

        await _collect_until_end(asr, timeout=3)

        assert not asr.healthy
        await asr.close()


async def test_audio_pushed_to_a_dead_adapter_is_counted_not_queued() -> None:
    async with _fake_deepgram(_hangs_up_after_one_frame) as (uri, _seen):
        asr = DeepgramAsr(api_key="k", url=uri, queue_frames=4)
        await asr.open(_config())
        await asr.push(b"\x00" * 640)
        await _collect_until_end(asr, timeout=3)

        for _ in range(20):
            await asr.push(b"\x00" * 640)

        assert asr._queue.qsize() < 4, "a dead socket must not be fed"
        assert asr.dropped_frames >= 20
        await asr.close()


async def test_close_completes_when_the_send_queue_is_full() -> None:
    """A dead socket leaves the queue full, and closing used to raise ``QueueFull``.

    That skipped the rest of the clean-up, so the tasks were never cancelled and the
    event stream was never ended.
    """

    asr = DeepgramAsr(api_key="k", queue_frames=2)
    asr._open = True
    for _ in range(4):
        await asr.push(b"\x00" * 8)
    assert asr._queue.full()

    await asr.close()

    assert asr._closed.is_set()
    assert await asyncio.wait_for(_drain(asr), timeout=1) == []


async def _drain(asr: DeepgramAsr) -> list[AsrEvent]:
    return [event async for event in asr.events()]


def test_the_stream_is_opened_for_the_language_the_speaker_uses() -> None:
    english = DeepgramAsr(api_key="k")._uri(AsrConfig(language="en"))
    german = DeepgramAsr(api_key="k")._uri(AsrConfig(language="de"))

    assert "language=en" in english
    assert "language=de" in german
    assert "model=nova-3" in german


def test_an_explicit_language_option_overrides_the_mode() -> None:
    uri = DeepgramAsr(api_key="k", language="de-CH")._uri(AsrConfig(language="de"))

    assert "language=de-CH" in uri
