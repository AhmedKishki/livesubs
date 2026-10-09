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


def test_a_settled_chunk_does_not_close_the_utterance() -> None:
    """Only ``speech_final`` ends a span, and mapping ``is_final`` to it was a real bug.

    Deepgram marks each chunk ``is_final`` as it settles, so treating that as the end of an
    utterance produced one subtitle line per chunk - each a growing prefix of the last -
    instead of one line growing in place. Running against the real API is the only place
    that could be found, and this pins it.
    """

    event = _parse_message(_results(is_final=True, speech_final=False))

    assert event is not None
    assert event.kind == "partial"
    assert event.speech_final is False


def test_speech_final_is_carried_through_as_the_endpointing_hint() -> None:
    """The segmenter treats it as a hint, and only this may close a span."""

    event = _parse_message(_results(is_final=True, speech_final=True))

    assert event is not None
    assert event.kind == "final"
    assert event.speech_final is True


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
