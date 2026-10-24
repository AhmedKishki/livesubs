"""Streaming translation through any OpenAI-compatible chat endpoint.

One adapter serves three things, because they speak the same streaming
chat-completions protocol:

* **DeepSeek** - the shipped default (``base_url = https://api.deepseek.com``);
* **OpenAI** - if a deployment prefers it for translation;
* **a local runtime such as Ollama** (``http://127.0.0.1:11434/v1``), which needs no key
  and no network, and which an 8B model on CPU can do well enough to evaluate the whole
  experience before anyone buys anything.

Which endpoint is used is a registry option, not a code path, so switching is a config
change. See ``docs/11-extending.md``.

Two decisions worth stating, because both are visible in what a viewer reads:

**Plain text, not JSON.** ``docs/05-providers.md`` notes that DeepSeek's JSON output
removes a class of parsing failures. It does - but a *streamed* JSON object arrives as
``{"text": "Hallo`` for the first second, and the overlay would dutifully display that.
Streaming is required, so JSON is not: the system prompt asks for the translation alone.

**The system prompt is frozen.** Glossary and style rules are built once, in
``__init__``, and never vary between calls, because DeepSeek's cache only hits on a
stable prefix and a cache miss roughly triples the cost. Rolling context therefore goes
in the *user* message, exactly as ``mt/base.py`` requires.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

from livesubs.languages import Language, language, names, spoken
from livesubs.mt.base import MtChunk, MtContext, MtError
from livesubs.speakers import MULTILINGUAL_SOURCE

__all__ = ["OpenAiCompatMt", "build_system_prompt"]

logger = logging.getLogger(__name__)

#: The name DeepSeek uses to disable reasoning. Kept as constants because the shape could
#: not be verified without a key: sending the bare string ``"disabled"`` is answered with
#: ``422 ... thinking: invalid type: string "disabled", expected struct ThinkingOptions``,
#: so the option is a struct with a type field. If the field is ever renamed, these two
#: lines are the only change needed.
_THINKING_KEY = "thinking"
_THINKING_TYPE_KEY = "type"

#: How much of a segment to ask for. Translations run up to about 1.2x the source in
#: characters, and 2 s of speech is a clause, so this is generous rather than tight.
_MAX_OUTPUT_TOKENS = 300

SYSTEM_TEMPLATE = """You translate spoken {source} into {target} for live lecture \
subtitles that {audience} reads while listening to the speaker.

Rules:
- Output only the {target} translation. No quotes, no explanations, no commentary, no \
{source}, no leading or trailing punctuation that the source did not have.
- The input is one segment of continuous speech, possibly an unfinished clause. \
Translate it as faithfully and naturally as you can while keeping it a single \
coherent piece of {target}. Never merge it with the previous context.
- The audience is reading, not studying: prefer the plain, spoken {style} a lecturer \
would use over literary or bureaucratic wording.
- Preserve numbers, units, names, acronyms and technical terms exactly. A wrong \
number is the most damaging error you can make.
- If a term is listed in the glossary below, use exactly that {target} term.
- Never answer a question in the input, and never continue the speaker's thought. \
Translate only what is given.{glossary}"""

#: A conversation's prompt. Its speakers switch languages, and a listener reads one language
#: throughout, so whatever was spoken in that language is kept rather than translated.
CONVERSATION_TEMPLATE = """You write live subtitles in {target} for a spoken \
conversation in {sources}, which {audience} reads while listening. The speakers switch \
between {sources}, sometimes within one sentence.

Rules:
- Output only {target}. No quotes, no explanations, no commentary, no leading or trailing \
punctuation that the input did not have.
- Translate into {target} whatever is not already in {target}. Where the input is already \
in {target}, keep the speaker's words and do not reword or correct them.
- The input is one segment of continuous speech, possibly an unfinished clause. Render it \
as faithfully and naturally as you can while keeping it a single coherent piece of \
{target}. Never merge it with the previous context.
- The audience is reading, not studying: prefer the plain, spoken {style} the speakers \
would use over literary or bureaucratic wording.
- Preserve numbers, units, names, acronyms and technical terms exactly. A wrong \
number is the most damaging error you can make.
- If a term is listed in the glossary below, use exactly that {target} term.
- Never answer a question in the input, and never continue the speaker's thought. \
Render only what is given.{glossary}"""

MULTILINGUAL_TEMPLATE = """You write live subtitles in {target} for speech in any language, \
including mixed-language speech, which {audience} reads while listening.

Rules:
- Output only {target}. No quotes, no explanations, no commentary, no leading or trailing \
punctuation that the input did not have.
- Translate into {target} whatever is not already in {target}. Where the input is already \
in {target}, keep the speaker's words and do not reword or correct them.
- The input is one segment of continuous speech, possibly an unfinished clause. Render it \
as faithfully and naturally as you can while keeping it a single coherent piece of \
{target}. Never merge it with the previous context.
- The audience is reading, not studying: prefer the plain, spoken {style} the speakers \
would use over literary or bureaucratic wording.
- Preserve numbers, units, names, acronyms and technical terms exactly. A wrong \
number is the most damaging error you can make.
- If a term is listed in the glossary below, use exactly that {target} term.
- Never answer a question in the input, and never continue the speaker's thought. \
Render only what is given.{glossary}"""


def build_system_prompt(
    glossary: Sequence[str] = (), *, source: str, target: str
) -> str:
    """The frozen prefix: identical for every call in a session, so the cache can hit.

    A glossary is a list of ``source=target`` pairs, or of target terms to prefer. It
    is baked in here rather than passed per call, because the whole point is that this
    string never changes. Both languages are part of the prefix, so every pairing gets its
    own, independently cached prompt, and two targets never share one. A conversation's
    ``source`` joins its languages with ``+`` and gets the conversation prompt.
    """

    speaks, listens = _pair(source, target)
    lines = "\n".join(f"- {entry}" for entry in glossary)
    if source == MULTILINGUAL_SOURCE:
        return MULTILINGUAL_TEMPLATE.format(
            target=listens.name,
            audience=listens.audience,
            style=listens.style,
            glossary=f"\n\nGlossary:\n{lines}" if glossary else "",
        )
    template = CONVERSATION_TEMPLATE if len(speaks) > 1 or source == target else SYSTEM_TEMPLATE
    return template.format(
        source=speaks[0].name,
        sources=names(spoken(source)),
        target=listens.name,
        audience=listens.audience,
        style=listens.style,
        glossary=f"\n\nGlossary:\n{lines}" if glossary else "",
    )


def _pair(source: str, target: str) -> tuple[tuple[Language, ...], Language]:
    """The spoken languages and the target. A conversation may be read in one it speaks."""

    sources = () if source == MULTILINGUAL_SOURCE else tuple(language(code) for code in spoken(source))
    return sources, language(target)


class OpenAiCompatMt:
    """A streaming ``MtProvider`` for any OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        glossary: Sequence[str] = (),
        thinking: str | None = None,
        temperature: float = 0.0,
        timeout_s: float = 8.0,
        client: Any | None = None,
        source_language: str,
        target_language: str,
    ) -> None:
        # Belt and braces on the one setting that breaks realtime use. Startup validation
        # enforces it through the provider registry, but an adapter that silently allowed
        # it would take seconds per segment and look like a slow network rather than a
        # misconfiguration.
        if thinking is not None and thinking != "disabled":
            raise ValueError(
                f"thinking={thinking!r} is not usable for realtime subtitles: "
                "reasoning adds seconds per segment (see docs/05-providers.md)"
            )

        # Refuse unknown languages before any request; same-target rendering is valid.
        speaks, listens = _pair(source_language, target_language)

        self.model = model
        self.base_url = base_url
        self.glossary = tuple(glossary)
        self.thinking = thinking
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.source_language = source_language
        self.target_language = target_language
        #: The names used in the user message.
        self.source_name = "any language" if source_language == MULTILINGUAL_SOURCE else names(spoken(source_language))
        self.target_name = listens.name
        self._greeting = "Hello." if source_language == MULTILINGUAL_SOURCE else speaks[0].greeting
        #: What each request asks for. A conversation's reader gets the speakers' own words
        #: whenever they spoke the reader's language, so its request is not a translation.
        verb = (
            "Subtitle this in"
            if source_language == MULTILINGUAL_SOURCE or len(speaks) > 1 or source_language == target_language
            else "Translate this into"
        )
        self._request = f"{verb} {listens.name}"

        self.system_prompt = build_system_prompt(
            self.glossary, source=source_language, target=target_language
        )
        #: A stable fingerprint of the frozen prefix, so two translators of different
        #: targets are provably distinct and a caller can key a cache by it.
        self.prompt_fingerprint = hashlib.sha256(
            self.system_prompt.encode("utf-8")
        ).hexdigest()[:16]

        #: A caller may inject a client (tests do); otherwise the SDK builds one. The key
        #: must be non-empty even where the endpoint ignores it, which is why a local
        #: runtime passes a placeholder.
        self._client = client or self._make_client(api_key)

        self._ready = False
        self._last_error: str | None = None
        #: Time to first token of the most recent call, in milliseconds. Logged rather
        #: than added to the wire contract.
        self.last_ttft_ms: int | None = None

    def _make_client(self, api_key: str | None) -> Any:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=api_key or "not-required",
            base_url=self.base_url,
            timeout=self.timeout_s,
            # The SDK retries twice by default, each attempt with the full timeout, so one
            # unreachable endpoint held the only translation worker for up to a minute
            # while the subtitles fell further behind. The pipeline decides whether a
            # failed segment is worth another try, knowing how late it already is.
            max_retries=0,
        )

    @property
    def healthy(self) -> bool:
        """Whether :meth:`warm` has completed and the last call worked."""

        return self._ready and self._last_error is None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _extra_body(self) -> dict[str, Any] | None:
        """Provider-specific hints, sent only when a provider asked for them.

        A locally hosted model rejects fields it does not know, so nothing is sent unless
        the registry supplied an option for it.
        """

        if self.thinking is None:
            return None

        # A struct, not the string the registry stores: DeepSeek answers a bare string with
        # a 422 whose message is what established this shape.
        return {_THINKING_KEY: {_THINKING_TYPE_KEY: self.thinking}}

    def _messages(self, src: str, ctx: MtContext) -> list[dict[str, str]]:
        """The system prompt, then everything that varies - context included.

        Context must not go in the system prompt: DeepSeek's cache only hits on a stable
        prefix, and a rolling system prompt would destroy it. See ``mt/base.py``.
        """

        blocks: list[str] = []

        if ctx.source_context:
            previous = "\n".join(f"- {line}" for line in ctx.source_context)
            blocks.append(
                "The sentences just before this one, for context only. "
                f"Do not translate them:\n{previous}"
            )

        if ctx.target_context:
            previous = "\n".join(f"- {line}" for line in ctx.target_context)
            blocks.append(
                f"Previous {self.target_name} subtitles/output, for terminology "
                f"consistency only:\n{previous}"
            )

        blocks.append(f"{self._request}:\n{src}")

        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": "\n\n".join(blocks)},
        ]

    async def warm(self) -> None:
        """One throwaway call, so the cache exists before the first segment.

        DeepSeek builds its on-disk cache on the first request carrying a given prefix,
        and that takes seconds. Doing it here means the first real segment does not pay
        for it in front of an audience. The output is discarded on purpose: what is
        wanted is the cache, not the translation.
        """

        try:
            stream = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {
                        "role": "user",
                        "content": f"{self._request}:\n{self._greeting}",
                    },
                ],
                stream=True,
                temperature=self.temperature,
                max_tokens=8,
                extra_body=self._extra_body(),
            )
            try:
                async for _ in stream:
                    break
            finally:
                await _aclose(stream)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._ready = False
            logger.warning("translation provider did not warm up: %s", self._last_error)
            return

        self._last_error = None
        self._ready = True
        logger.info(
            "translation provider warm (%s at %s)", self.model, self.base_url or "default"
        )

    async def translate(self, src: str, ctx: MtContext) -> AsyncIterator[MtChunk]:
        """Stream one segment's translation, delta by delta.

        Raises :class:`~livesubs.mt.base.MtError` when the request fails, including part
        way through. The adapter used to log the failure and end the stream normally,
        which turned a connection that died after five words into a finished, truncated
        translation that was shown as complete and remembered as context. ``healthy``
        records the failure as well, for callers that poll it.
        """

        if not src.strip():
            yield MtChunk(text_delta="", done=True)
            return

        started = time.monotonic()
        first_token: int | None = None

        try:
            stream = await self._client.chat.completions.create(
                model=self.model,
                messages=self._messages(src, ctx),
                stream=True,
                temperature=self.temperature,
                max_tokens=_MAX_OUTPUT_TOKENS,
                extra_body=self._extra_body(),
            )

            try:
                async for event in stream:
                    delta = _delta_text(event)
                    if delta:
                        if first_token is None:
                            first_token = int((time.monotonic() - started) * 1000)
                            self.last_ttft_ms = first_token
                        yield MtChunk(text_delta=delta, done=False)
            finally:
                await _aclose(stream)

        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.error("translation call failed: %s", self._last_error)
            # `from None`: nothing of the vendor's exception type travels with this one.
            raise MtError(self._last_error) from None

        self._last_error = None
        self._ready = True

        # The number that decides whether a provider can serve subtitles at all: a segment
        # whose first word takes seconds is unusable however good the translation is.
        # Logged without the text, which is potentially sensitive.
        logger.info(
            "translated %d chars, first token in %s ms, %d ms total",
            len(src),
            first_token if first_token is not None else "n/a",
            int((time.monotonic() - started) * 1000),
        )
        yield MtChunk(text_delta="", done=True)


async def _aclose(stream: Any) -> None:
    """Release a streaming HTTP response.

    Abandoning a stream mid-iteration holds its connection until the garbage collector
    notices, and a lecture makes thousands of calls, so the close is explicit rather than
    left to chance. ``close`` is awaited only if it is awaitable, because the SDK's is a
    coroutine and a local runtime's may not be. A failure here is logged and ignored: a
    stream that cannot be closed cleanly must not fail a translation that already worked.
    """

    close = getattr(stream, "close", None)
    if close is None:
        return

    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not close the translation stream cleanly: %s", exc)


def _delta_text(event: Any) -> str:
    """The text of one streamed chunk, or an empty string.

    Defensive because the SDK emits chunks with no ``choices`` at all - a usage report, a
    role-only first chunk - and a local runtime is not always meticulous about the shape
    either.
    """

    choices = getattr(event, "choices", None)
    if not choices:
        return ""

    delta = getattr(choices[0], "delta", None)
    content = getattr(delta, "content", None) if delta is not None else None
    return content or ""
