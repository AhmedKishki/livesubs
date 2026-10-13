"""The transcript assembler: provider events in, the text of each segment out.

The failure it exists to prevent was found in real recordings, not invented. A segment
cut in the middle of a Deepgram chunk was followed by a cumulative interim that began with
the words already sent, so cues repeated their own opening ("I'm also an English" /
"I'm also an English language examiner" / ...), and a chunk that settled between two cuts
was overwritten by the next chunk's interim, so words such as a surname vanished.
"""

from __future__ import annotations

import random

import pytest

from livesubs.asr.base import AsrEvent
from livesubs.events import WordTiming
from livesubs.transcript import TranscriptAssembler


def words(spec: str, start_ms: int = 0, step_ms: int = 300) -> list[WordTiming]:
    """``"a b c"`` as timed words, ``step_ms`` apart, each lasting 250 ms."""

    out = []
    for index, text in enumerate(spec.split()):
        begin = start_ms + index * step_ms
        out.append(WordTiming(text=text, start_ms=begin, end_ms=begin + 250))
    return out


def partial(spec: str, start_ms: int = 0, *, timed: bool = True) -> AsrEvent:
    return AsrEvent(
        kind="partial",
        text=spec,
        start_ms=start_ms,
        end_ms=start_ms + 300 * len(spec.split()),
        words=words(spec, start_ms) if timed else None,
    )


def final(spec: str, start_ms: int = 0, *, timed: bool = True, endpoint: bool = False) -> AsrEvent:
    return AsrEvent(
        kind="final",
        text=spec,
        start_ms=start_ms,
        end_ms=start_ms + 300 * len(spec.split()),
        words=words(spec, start_ms) if timed else None,
        speech_final=endpoint,
    )


# --- the basics ----------------------------------------------------------------------


def test_nothing_is_pending_before_any_event() -> None:
    assembler = TranscriptAssembler()

    assert not assembler.pending
    assert assembler.text == ""
    assert assembler.commit() == ""


def test_a_later_partial_replaces_an_earlier_one() -> None:
    assembler = TranscriptAssembler()

    assembler.feed(partial("the oscil"))
    assembler.feed(partial("the oscillation can"))

    assert assembler.text == "the oscillation can"


def test_feed_reports_whether_the_text_changed() -> None:
    assembler = TranscriptAssembler()

    assert assembler.feed(partial("the oscillation")) is True
    assert assembler.feed(partial("the oscillation")) is False
    assert assembler.feed(partial("the oscillation can")) is True


def test_commit_returns_the_text_and_empties_the_assembler() -> None:
    assembler = TranscriptAssembler()
    assembler.feed(partial("the oscillation can"))

    assert assembler.commit() == "the oscillation can"
    assert not assembler.pending
    assert assembler.commit() == ""


# --- settled chunks accumulate -------------------------------------------------------


def test_settled_chunks_are_joined_not_overwritten() -> None:
    """Deepgram's guide: concatenate the is_final results until the speaker stops."""

    assembler = TranscriptAssembler()

    assembler.feed(final("yeah so my credit card number is", 0))
    assembler.feed(partial("two two", 2000))
    assert assembler.text == "yeah so my credit card number is two two"

    assembler.feed(final("two two three three", 2000, endpoint=True))
    assert assembler.commit() == "yeah so my credit card number is two two three three"


def test_a_chunk_that_settles_between_two_cuts_is_not_lost() -> None:
    """The words a surname was dropped with: settled, then overwritten by a new interim."""

    assembler = TranscriptAssembler()
    assembler.feed(partial("my name is", 0))
    assert assembler.commit() == "my name is"

    # The next chunk settles ...
    assembler.feed(final("Nick Leonard and I teach", 900))
    # ... and the following chunk's interim begins before the next cut.
    assembler.feed(partial("English as a", 2700))

    assert assembler.commit() == "Nick Leonard and I teach English as a"


# --- a cut in the middle of a chunk --------------------------------------------------


def test_a_cumulative_interim_after_a_cut_does_not_repeat_what_was_sent() -> None:
    """The cue that repeated its own opening: 'I'm also an English' / '... language'."""

    assembler = TranscriptAssembler()
    assembler.feed(partial("I'm also an English", 0))
    assert assembler.commit() == "I'm also an English"

    assembler.feed(partial("I'm also an English language examiner", 0))
    assert assembler.text == "language examiner"

    assembler.feed(partial("I'm also an English language examiner at the", 0))
    assert assembler.commit() == "language examiner at the"


def test_the_settled_text_after_a_cut_still_skips_what_was_sent() -> None:
    assembler = TranscriptAssembler()
    assembler.feed(partial("the oscillation can be", 0))
    assert assembler.commit() == "the oscillation can be"

    assembler.feed(final("the oscillation can be described by", 0, endpoint=True))

    assert assembler.commit() == "described by"


def test_words_recognised_late_belong_to_the_next_segment() -> None:
    """Latency: the last words before a cut are not recognised yet, and are not lost."""

    assembler = TranscriptAssembler()
    assembler.feed(partial("a b c", 0))
    assert assembler.commit() == "a b c"

    # The recogniser catches up with words spoken before the cut.
    assembler.feed(partial("a b c d e", 0))

    assert assembler.commit() == "d e"


def test_a_revision_of_text_already_sent_is_ignored() -> None:
    """What went to the translator cannot be taken back, so a revision cannot be applied."""

    assembler = TranscriptAssembler()
    assembler.feed(partial("their oscillation can", 0))
    assembler.commit()

    assembler.feed(partial("the oscillation can be", 0))

    assert assembler.commit() == "be"


def test_a_word_whose_timing_shifts_slightly_is_not_repeated() -> None:
    assembler = TranscriptAssembler()
    assembler.feed(partial("alpha beta", 0))
    assembler.commit()

    shifted = partial("alpha beta gamma", 0)
    assert shifted.words is not None
    shifted.words[0] = WordTiming(text="alpha", start_ms=40, end_ms=330)
    shifted.words[1] = WordTiming(text="beta", start_ms=340, end_ms=640)  # was 300-550

    assembler.feed(shifted)

    assert assembler.commit() == "gamma"


def test_a_new_segment_after_a_pause_starts_clean() -> None:
    assembler = TranscriptAssembler()
    assembler.feed(final("first sentence here", 0, endpoint=True))
    assert assembler.commit() == "first sentence here"

    assembler.feed(partial("second", 5000))
    assembler.feed(final("second sentence", 5000, endpoint=True))

    assert assembler.commit() == "second sentence"


# --- providers without word timings ---------------------------------------------------


def test_untimed_cumulative_partials_are_cut_by_word_count() -> None:
    assembler = TranscriptAssembler()
    assembler.feed(partial("a b c d", timed=False))
    assert assembler.commit() == "a b c d"

    assembler.feed(partial("a b c d e", timed=False))
    assert assembler.text == "e"

    assembler.feed(final("a b c d e f", timed=False, endpoint=True))
    assert assembler.commit() == "e f"


def test_an_untimed_span_that_closes_starts_the_next_one_from_zero() -> None:
    assembler = TranscriptAssembler()
    assembler.feed(partial("a b", timed=False))
    assembler.commit()
    assembler.feed(final("a b c", timed=False, endpoint=True))
    assert assembler.commit() == "c"

    # A new utterance: nothing of it has been sent.
    assembler.feed(partial("x y", timed=False))
    assert assembler.commit() == "x y"


def test_whole_phrase_finals_pass_straight_through() -> None:
    """What StubAsr and a timed-transcript replay emit: one final per utterance."""

    assembler = TranscriptAssembler()

    assembler.feed(final("This is one cue.", timed=False, endpoint=True))
    assert assembler.commit() == "This is one cue."

    assembler.feed(final("And this is the next.", timed=False, endpoint=True))
    assert assembler.commit() == "And this is the next."


# --- restarting ----------------------------------------------------------------------


def test_reset_forgets_everything_including_the_cut() -> None:
    """A new stream restarts its clock at zero, so an old cut would swallow its words."""

    assembler = TranscriptAssembler()
    assembler.feed(partial("late words", 90_000))
    assembler.commit()

    assembler.reset()
    assembler.feed(partial("fresh stream", 0))

    assert assembler.commit() == "fresh stream"


def test_empty_events_change_nothing() -> None:
    assembler = TranscriptAssembler()
    assembler.feed(partial("hello there", 0))

    assert assembler.feed(AsrEvent(kind="final", text="", start_ms=0, end_ms=0, speech_final=True)) is False
    assert assembler.text == "hello there"


# --- the property that matters --------------------------------------------------------


def deepgram_like_stream(
    truth: list[str], rng: random.Random
) -> tuple[list[tuple[int, AsrEvent]], list[WordTiming]]:
    """Events for ``truth`` as a chunking provider sends them, with their arrival times.

    Interim results grow cumulatively from the start of the current chunk; a chunk
    settles into a ``final`` that carries only its own words; the next chunk starts from
    empty. Chunk lengths and recognition latency are random.
    """

    timed = [
        WordTiming(text=word, start_ms=index * 320, end_ms=index * 320 + 280)
        for index, word in enumerate(truth)
    ]
    events: list[tuple[int, AsrEvent]] = []
    index = 0
    while index < len(timed):
        size = rng.randint(2, 12)
        chunk = timed[index : index + size]
        latency = rng.randint(150, 600)
        for upto in range(1, len(chunk) + 1):
            heard = chunk[:upto]
            events.append(
                (
                    heard[-1].end_ms + latency,
                    AsrEvent(
                        kind="partial",
                        text=" ".join(w.text for w in heard),
                        start_ms=heard[0].start_ms,
                        end_ms=heard[-1].end_ms,
                        words=list(heard),
                    ),
                )
            )
        last = index + size >= len(timed)
        events.append(
            (
                chunk[-1].end_ms + latency + 20,
                AsrEvent(
                    kind="final",
                    text=" ".join(w.text for w in chunk),
                    start_ms=chunk[0].start_ms,
                    end_ms=chunk[-1].end_ms,
                    words=list(chunk),
                    speech_final=last or rng.random() < 0.3,
                ),
            )
        )
        index += size
    events.sort(key=lambda pair: pair[0])
    return events, timed


@pytest.mark.parametrize("seed", range(40))
def test_every_word_is_sent_exactly_once_whatever_the_cuts(seed: int) -> None:
    """Cut at arbitrary moments, as the 2 s cap and the pauses do: no loss, no repeats."""

    rng = random.Random(seed)
    truth = [f"w{n:03d}" for n in range(rng.randint(15, 90))]
    events, _ = deepgram_like_stream(truth, rng)

    assembler = TranscriptAssembler()
    sent: list[str] = []
    next_cut = rng.randint(300, 2500)
    for arrival, event in events:
        while arrival >= next_cut:
            sent.extend(assembler.commit().split())
            next_cut += rng.randint(300, 2500)
        assembler.feed(event)
    sent.extend(assembler.commit().split())

    assert sent == truth
