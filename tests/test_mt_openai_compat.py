"""Tests for the OpenAI-compatible translation adapter.

The endpoint is faked at the HTTP layer - an ``httpx.MockTransport`` inside the client the
adapter is given - so the real SDK's SSE parsing is exercised and the request payloads can
be captured. That matters because the two properties that cost money and quality are
properties of the *request*: the system prompt must be frozen, and the rolling context must
be in the user message rather than the system prompt.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from livesubs.mt.base import MtContext, MtProvider
from livesubs.mt.openai_compat import OpenAiCompatMt, build_system_prompt

# --- the frozen prefix -------------------------------------------------------


def test_the_system_prompt_asks_for_german_and_nothing_else() -> None:
    prompt = build_system_prompt()

    assert "German" in prompt
    assert "Output only the German translation" in prompt
    assert "Preserve numbers" in prompt


def test_a_glossary_is_baked_into_the_prompt() -> None:
    """It has to live in the frozen prefix, or the cache misses and the cost triples."""

    prompt = build_system_prompt(["capture=Aufnahme", "overlay=Overlay"])

    assert "- capture=Aufnahme" in prompt
    assert "- overlay=Overlay" in prompt


# --- request construction ----------------------------------------------------


def _sse(*texts: str) -> bytes:
    """A chat-completions SSE body that emits ``texts`` as deltas."""

    events: list[dict[str, Any]] = [
        {"id": "1", "object": "chat.completion.chunk", "choices": []},  # usage-only chunk
    ]
    for text in texts:
        events.append(
            {
                "id": "1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }
        )
    events.append(
        {"id": "1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}}]}
    )

    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return (body + "data: [DONE]\n\n").encode()


def _adapter(
    *, texts: tuple[str, ...] = ("Hallo", " Welt"), captured: list | None = None, **kwargs: Any
) -> OpenAiCompatMt:
    sent = captured if captured is not None else []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_sse(*texts)
        )

    client = AsyncOpenAI(
        api_key="test",
        base_url="http://test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return OpenAiCompatMt(model="test-model", client=client, **kwargs)


async def test_deltas_are_streamed_then_finished() -> None:
    adapter = _adapter()

    chunks = [chunk async for chunk in adapter.translate("Hello", MtContext())]

    assert "".join(chunk.text_delta for chunk in chunks) == "Hallo Welt"
    assert chunks[-1].done is True
    assert all(not chunk.done for chunk in chunks[:-1])


async def test_rolling_context_is_in_the_user_message_not_the_system_prompt() -> None:
    """DeepSeek's cache only hits on a stable prefix, so this is a cost decision."""

    captured: list = []
    adapter = _adapter(captured=captured)
    context = MtContext(
        source_context=["The previous sentence."], target_context=["Der vorherige Satz."]
    )

    async for _ in adapter.translate("This one.", context):
        pass

    system, user = captured[0]["messages"]
    assert system["role"] == "system"
    assert "The previous sentence." not in system["content"]
    assert "Der vorherige Satz." not in system["content"]
    assert user["role"] == "user"
    assert "The previous sentence." in user["content"]
    assert "Der vorherige Satz." in user["content"]
    assert "This one." in user["content"]


async def test_the_system_prompt_is_byte_identical_across_calls() -> None:
    """Anything that varies here would invalidate the cached prefix every segment."""

    captured: list = []
    adapter = _adapter(captured=captured)

    async for _ in adapter.translate("One.", MtContext(source_context=["Previous."])):
        pass
    async for _ in adapter.translate("Two.", MtContext(source_context=["Different."])):
        pass

    first = captured[0]["messages"][0]["content"]
    second = captured[1]["messages"][0]["content"]

    assert first == second
    assert captured[0]["messages"][1]["content"] != captured[1]["messages"][1]["content"]


async def test_streaming_is_requested_so_the_first_words_appear_early() -> None:
    captured: list = []
    adapter = _adapter(captured=captured)

    async for _ in adapter.translate("Hello", MtContext()):
        pass

    assert captured[0]["stream"] is True
    assert captured[0]["temperature"] == 0.0


async def test_the_thinking_option_is_sent_as_the_struct_deepseek_expects() -> None:
    """A bare string is refused with a 422, so the shape is part of the contract."""

    captured: list = []
    adapter = _adapter(captured=captured, thinking="disabled")

    async for _ in adapter.translate("Hello", MtContext()):
        pass

    assert captured[0]["thinking"] == {"type": "disabled"}


async def test_a_local_endpoint_gets_no_provider_specific_fields() -> None:
    """A locally hosted model rejects fields it does not know."""

    captured: list = []
    adapter = _adapter(captured=captured)

    async for _ in adapter.translate("Hello", MtContext()):
        pass

    assert "thinking" not in captured[0]


# --- health, failures, and the settings that break realtime -------------------


def test_reasoning_is_refused_at_construction() -> None:
    """Startup validation enforces it, and the adapter must not be the weak link."""

    with pytest.raises(ValueError, match="realtime"):
        OpenAiCompatMt(model="m", thinking="enabled")


async def test_a_failed_call_marks_the_provider_unhealthy_without_raising() -> None:
    """The failure is reported to the operator; the caller moves on to the next segment."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = AsyncOpenAI(
        api_key="test",
        base_url="http://test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = OpenAiCompatMt(model="m", client=client)

    chunks = [chunk async for chunk in adapter.translate("Hello", MtContext())]

    assert chunks[-1].done is True
    assert adapter.healthy is False
    assert adapter.last_error is not None


async def test_warm_marks_the_provider_ready() -> None:
    adapter = _adapter()

    assert adapter.healthy is False
    await adapter.warm()

    assert adapter.healthy is True


async def test_warm_reports_a_failure_instead_of_raising() -> None:
    """Startup continues; the status pill is what tells the operator."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    client = AsyncOpenAI(
        api_key="test",
        base_url="http://test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = OpenAiCompatMt(model="m", client=client)

    await adapter.warm()

    assert adapter.healthy is False
    assert "401" in (adapter.last_error or "")


async def test_time_to_first_token_is_recorded() -> None:
    """The number that decides whether a provider can serve subtitles at all."""

    adapter = _adapter(texts=("Hallo", " Welt"))

    async for _ in adapter.translate("Hello", MtContext()):
        pass

    assert adapter.last_ttft_ms is not None
    assert adapter.last_ttft_ms >= 0


async def test_an_empty_segment_is_not_sent_to_the_provider() -> None:
    captured: list = []
    adapter = _adapter(captured=captured)

    chunks = [chunk async for chunk in adapter.translate("   ", MtContext())]

    assert captured == []
    assert chunks[-1].done is True


def test_the_adapter_satisfies_the_mt_protocol() -> None:
    assert isinstance(_adapter(), MtProvider)
