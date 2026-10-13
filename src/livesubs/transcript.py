"""From recognition events to the text of a segment.

A streaming recogniser does not describe a lecture one segment at a time. It describes it
in *spans*: while a span is open, each event is a fresh hypothesis of the whole span and
replaces the last; when the span settles, its text is complete and the next event starts a
new span that does not repeat it. Deepgram settles a chunk every few seconds, several times
inside one utterance, and an interim result is cumulative from the start of its chunk.

The pipeline cuts segments on its own schedule: at a pause, at the provider's endpoint, or
at a duration cap in the middle of a word. Nothing about that schedule is known to the
provider, so the two disagree in both directions, and reading "the newest event's text" as
"the segment" is wrong both ways:

* a cut part-way through a span is followed by an interim that still begins with the words
  just sent, so the next segment repeats them. Real recordings showed cues such as "I'm also
  an English" / "I'm also an English language examiner" / "... at the" in succession;
* a span that settles between two cuts is overwritten by the next span's interim, so its
  words never reach any segment. A surname was lost this way.

This module owns that bookkeeping and nothing else. It keeps the settled text, the current
hypothesis, and how far the text has been sent, and hands over only what has not been sent.
It performs no I/O and does not decide when to cut: that is the segmenter's job.

How far the text has been sent is measured on the provider's own stream clock, from word
timings, when the provider has them. That is robust to a hypothesis revising earlier words,
which interim results do all the time. A provider without timings is handled by counting
words in the open span, which is exact for the whole-phrase output of the simulated and
transcript recognisers and best-effort otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

from livesubs.asr.base import AsrEvent

__all__ = ["Token", "TranscriptAssembler"]


@dataclass(frozen=True, slots=True)
class Token:
    """One word of a hypothesis, with its timing when the provider supplied one."""

    text: str
    start_ms: int | None = None
    end_ms: int | None = None

    @property
    def timed(self) -> bool:
        return self.start_ms is not None and self.end_ms is not None

    @property
    def midpoint_ms(self) -> float:
        assert self.start_ms is not None and self.end_ms is not None
        return (self.start_ms + self.end_ms) / 2


class TranscriptAssembler:
    """Accumulates recognition events and releases only text that has not been sent."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forget everything. A new stream restarts its clock, so nothing carries over."""

        #: Tokens of spans that settled and have not been sent.
        self._settled: list[Token] = []
        #: Tokens of the open span's latest hypothesis that have not been sent.
        self._tail: list[Token] = []
        #: Stream time up to which words have been sent, once timings have been seen.
        self._cut_ms: int | None = None
        #: Words of the open span already sent, for providers without timings.
        self._skip = 0

    # --- reading ------------------------------------------------------------------

    @property
    def pending(self) -> bool:
        """Whether there is text that has not been sent."""

        return bool(self._settled or self._tail)

    @property
    def text(self) -> str:
        """The text that has not been sent, without sending it."""

        return " ".join(token.text for token in (*self._settled, *self._tail))

    # --- feeding ------------------------------------------------------------------

    def feed(self, event: AsrEvent) -> bool:
        """Fold one event in. Returns whether the unsent text changed."""

        before = self.text
        received = _tokens_of(event)

        if not received:
            # An endpoint with no words in it (silence, or a settled-empty chunk) carries
            # no text to fold in, and must not erase the hypothesis that is still unsent.
            # It does close a span, so the next one still starts counting from nothing.
            if event.kind == "final":
                self._skip = 0
            return False

        tokens = self._unsent(received)

        if event.kind == "final":
            self._settled.extend(tokens)
            self._tail = []
            # The span is closed, so the next one starts counting from nothing.
            self._skip = 0
        else:
            self._tail = tokens

        return self.text != before

    def _unsent(self, tokens: list[Token]) -> list[Token]:
        """Drop the words of a hypothesis that have already been sent."""

        if not tokens:
            return tokens

        if self._cut_ms is not None and all(token.timed for token in tokens):
            # By the middle of the word, not its end: the same word is re-timed by a few
            # milliseconds between an interim and its final, and an end-based test would
            # let it through a second time.
            return [token for token in tokens if token.midpoint_ms > self._cut_ms]

        return tokens[self._skip :]

    # --- sending ------------------------------------------------------------------

    def commit(self) -> str:
        """Release the unsent text, and remember that it has been sent."""

        tokens = [*self._settled, *self._tail]
        if not tokens:
            return ""

        ends = [token.end_ms for token in tokens if token.end_ms is not None]
        if ends:
            latest = max(ends)
            self._cut_ms = latest if self._cut_ms is None else max(self._cut_ms, latest)

        # Only the open span's words are still being re-described by later events.
        self._skip += len(self._tail)

        text = self.text
        self._settled = []
        self._tail = []
        return text


def _tokens_of(event: AsrEvent) -> list[Token]:
    if event.words:
        return [
            Token(word.text, word.start_ms, word.end_ms) for word in event.words if word.text
        ]
    return [Token(word) for word in event.text.split()]
