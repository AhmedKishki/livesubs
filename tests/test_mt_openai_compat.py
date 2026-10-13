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

from livesubs.mt.base import MtContext, MtError, MtProvider
from livesubs.mt.openai_compat import OpenAiCompatMt, build_system_prompt

# --- the frozen prefix -------------------------------------------------------


def test_the_system_prompt_asks_for_the_target_and_nothing_else() -> None:
    prompt = build_system_prompt(source="en", target="de")

    assert "spoken English into German" in prompt
    assert "Output only the German translation" in prompt
    assert "Preserve numbers" in prompt


def test_a_glossary_is_baked_into_the_prompt() -> None:
    """It has to live in the frozen prefix, or the cache misses and the cost triples."""

    prompt = build_system_prompt(["capture=Aufnahme", "overlay=Overlay"], source="en", target="de")

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
    kwargs.setdefault("source_language", "en")
    kwargs.setdefault("target_language", "de")
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
        OpenAiCompatMt(
            model="m", thinking="enabled", source_language="en", target_language="de"
        )


async def test_a_failed_call_raises_mt_error_and_marks_the_provider_unhealthy() -> None:
    """The failure reaches the caller, which alone knows what a viewer has been shown."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = AsyncOpenAI(
        api_key="test",
        base_url="http://test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = OpenAiCompatMt(
        model="m", client=client, source_language="en", target_language="de"
    )

    with pytest.raises(MtError) as error:
        [chunk async for chunk in adapter.translate("Hello", MtContext())]

    assert adapter.healthy is False
    assert adapter.last_error is not None
    assert error.value.__cause__ is None, "no vendor exception may travel with the error"
    assert type(error.value) is MtError


async def test_a_stream_that_dies_part_way_raises_after_the_deltas_it_delivered() -> None:
    """Ending normally here published a truncated line as if it were complete."""

    class DyingStream:
        def __init__(self) -> None:
            self.items = ["Die Schwingung ", "lässt sich"]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.items:
                from types import SimpleNamespace

                text = self.items.pop(0)
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])
            raise ConnectionResetError("peer reset the stream")

        async def close(self) -> None:
            pass

    class Completions:
        async def create(self, **kwargs: Any) -> DyingStream:
            return DyingStream()

    class Client:
        def __init__(self) -> None:
            from types import SimpleNamespace

            self.chat = SimpleNamespace(completions=Completions())

    adapter = OpenAiCompatMt(
        model="m", client=Client(), source_language="en", target_language="de"
    )
    received: list[str] = []

    with pytest.raises(MtError):
        async for chunk in adapter.translate("The oscillation can be described", MtContext()):
            received.append(chunk.text_delta)

    assert received == ["Die Schwingung ", "lässt sich"]
    assert adapter.healthy is False


def test_the_sdk_is_not_left_to_retry_on_its_own() -> None:
    adapter = OpenAiCompatMt(
        model="m", api_key="k", base_url="http://127.0.0.1:1",
        source_language="en", target_language="de",
    )

    assert adapter._client.max_retries == 0


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
    adapter = OpenAiCompatMt(
        model="m", client=client, source_language="en", target_language="de"
    )

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


# --- any pairing of languages ----------------------------------------------------------


def _fixture(name: str) -> str:
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "prompts_v1.json"
    return json.loads(path.read_text(encoding="utf-8"))[name]


def test_the_english_to_german_prompt_is_byte_identical_to_the_one_before_modes() -> None:
    """The provider's cache hits only on an identical prefix, and a lecture is mid-flight."""

    assert build_system_prompt(source="en", target="de") == _fixture("en>de")
    assert build_system_prompt(
        ["capture=Aufnahme", "overlay=Overlay"], source="en", target="de"
    ) == _fixture("en>de+glossary")


def test_the_english_to_arabic_prompt_is_byte_identical_to_the_one_before_modes() -> None:
    assert build_system_prompt(source="en", target="ar") == _fixture("en>ar")


@pytest.mark.parametrize(
    ("source", "target", "speaks", "into", "audience"),
    [
        ("de", "en", "German", "English", "an English-speaking audience"),
        ("de", "ar", "German", "Arabic", "an Arabic-speaking audience"),
        ("en", "de", "English", "German", "a German-speaking audience"),
    ],
)
def test_a_prompt_names_the_speaker_and_the_audience(
    source: str, target: str, speaks: str, into: str, audience: str
) -> None:
    prompt = build_system_prompt(source=source, target=target)

    assert f"translate spoken {speaks} into {into}" in prompt
    assert f"Output only the {into} translation" in prompt
    assert f", no {speaks}," in prompt, "the speaker's own language is the one to keep out"
    assert audience in prompt


def test_every_pairing_gets_its_own_cacheable_prompt() -> None:
    prompts = {
        (source, target): build_system_prompt(source=source, target=target)
        for source, target in (("en", "de"), ("en", "ar"), ("de", "en"), ("de", "ar"))
    }

    assert len(set(prompts.values())) == 4
    assert build_system_prompt(source="de", target="en") == prompts[("de", "en")]


def test_translating_a_language_into_itself_is_refused() -> None:
    with pytest.raises(ValueError, match="into itself"):
        build_system_prompt(source="de", target="de")
    with pytest.raises(ValueError, match="into itself"):
        OpenAiCompatMt(model="m", client=object(), source_language="en", target_language="en")


def test_an_unknown_language_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown language"):
        OpenAiCompatMt(model="m", client=object(), source_language="en", target_language="xx")


def test_adapters_for_different_targets_are_distinguishable() -> None:
    pairs = [("en", "de"), ("en", "ar"), ("de", "en"), ("de", "ar")]
    adapters = [
        OpenAiCompatMt(model="m", client=object(), source_language=s, target_language=t)
        for s, t in pairs
    ]

    assert len({a.prompt_fingerprint for a in adapters}) == 4
    assert [a.system_prompt for a in adapters] == [
        build_system_prompt(source=s, target=t) for s, t in pairs
    ]


async def test_the_user_message_names_the_target_and_the_warm_up_is_in_the_speakers_language() -> None:
    captured: list = []
    adapter = _adapter(captured=captured, source_language="de", target_language="en")

    await adapter.warm()
    [chunk async for chunk in adapter.translate("Guten Tag", MtContext())]

    warm_message = captured[0]["messages"][1]["content"]
    real_message = captured[1]["messages"][1]["content"]
    assert warm_message == "Translate this into English:\nHallo."
    assert real_message.endswith("Translate this into English:\nGuten Tag")
