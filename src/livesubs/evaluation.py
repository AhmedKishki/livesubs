"""Scoring a session against a reference, and against itself.

Specified in ``docs/09-evaluation.md``. The point of that document is that the
obvious metrics are the wrong scoreboard, so the emphasis here is deliberate:

* **WER is reported in slices**, because "5% word error" hides the two error
  classes an attendee actually notices, a mangled name and a wrong number.
* **chrF rather than BLEU** for translation, because German compounds and
  inflection punish word-level matching, and because a reference translation
  legitimately rephrases.
* **Some checks need no reference at all** (numbers surviving into the German,
  output that is not actually translated, the length ratio). Those are what make a
  session measurable before anyone has written a reference translation by hand.

Everything here is pure: text in, numbers out. No I/O, no network, no provider, so
it is unit-tested rather than trusted, and it can score a recording years old.

Two honest limits, stated because a metric that overstates itself is worse than no
metric:

* The tokeniser is whitespace-and-punctuation based. It is not a linguistic WER
  implementation, and it deliberately does not normalise numerals to words.
* chrF is implemented from the published definition rather than imported from
  sacrebleu, because adding a dependency for one metric is not worth it. It agrees
  with sacrebleu's ``chrF`` (``char_order=6``, ``word_order=0``) by construction,
  but it has not been checked against it figure for figure.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "Alignment",
    "LanguageCheck",
    "NumberReport",
    "SessionReport",
    "Turn",
    "check_languages",
    "chrf_corpus",
    "chrf_sentence",
    "guess_language",
    "identical_to_source_fraction",
    "length_ratio",
    "normalise",
    "number_preservation",
    "numbers",
    "percentiles",
    "tokenise",
    "tokenise_numbers",
    "wer",
    "wer_on_numbers",
]

#: Characters that carry no signal for comparison but do carry a lot of variation
#: between a transcript and a machine transcript: the curly quotes and dashes that
#: a PDF or a word processor inserts versus the ASCII a model emits.
_QUOTES = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
    "\u00a0": " ",
}


def normalise(text: str) -> str:
    """Lowercase, fold the punctuation variants, and keep digits intact.

    Digits are preserved on purpose: a number is the error class that damages a
    lecture most, so the comparison must be able to see one, and ``numbers()``
    reads this output.
    """

    folded = unicodedata.normalize("NFKC", text)
    for source, target in _QUOTES.items():
        folded = folded.replace(source, target)

    out: list[str] = []
    for char in folded.lower():
        if char.isalnum():
            out.append(char)
        elif char in "'-":
            # Kept inside a word because "don't" and "10-minute" are single words
            # to a reader, and splitting them would inflate WER for no reason.
            out.append(char)
        else:
            out.append(" ")
    return " ".join("".join(out).split())


def tokenise(text: str) -> list[str]:
    """Words for comparison. ``"Don't."`` and ``"don't"`` are the same token."""

    return normalise(text).split()


#: A numeral, including decimals, thousands separators, ranges and times: ``3.5``,
#: ``1,200``, ``40-50``, ``12:30``. Deliberately greedy, because the cost of
#: missing a number is a wrong number on screen.
_NUMBER_RE = re.compile(r"\d[\d.,:/+\-]*\d|\d")


def numbers(text: str) -> list[str]:
    """Every numeral in ``text``, in order, punctuation stripped from the ends."""

    found = []
    for raw in _NUMBER_RE.findall(unicodedata.normalize("NFKC", text)):
        cleaned = raw.strip(".,:/-+")
        if cleaned:
            found.append(cleaned)
    return found


def tokenise_numbers(text: str) -> list[str]:
    """The numeral tokens only, as a sequence that can be aligned against another.

    Comparing the numeral *subsequences* rather than their positions is the point:
    a translation may move a number within the sentence ("40 to 50% of all
    language" -> "40 bis 50 % der Sprache") and that is not an error.
    """

    return numbers(text)


@dataclass(frozen=True, slots=True)
class Alignment:
    """The edit operations that turn ``reference`` into ``hypothesis``."""

    reference: int
    substitutions: int
    deletions: int
    insertions: int

    @property
    def hits(self) -> int:
        return self.reference - self.substitutions - self.deletions

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def wer(self) -> float:
        """Word error rate, as a fraction. ``1.0`` means one error per reference word."""

        return self.errors / self.reference

    @property
    def accuracy(self) -> float:
        return self.hits / self.reference

    def as_dict(self) -> dict[str, float | int]:
        return {
            "reference_words": self.reference,
            "substitutions": self.substitutions,
            "deletions": self.deletions,
            "insertions": self.insertions,
            "hits": self.hits,
            "errors": self.errors,
            "wer": round(self.wer, 4),
            "accuracy": round(self.accuracy, 4),
        }

    def format_text(self, label: str = "wer") -> str:
        return (
            f"{label}: {self.wer * 100:.1f}% "
            f"({self.errors}/{self.reference} words; "
            f"{self.substitutions} sub, {self.deletions} del, {self.insertions} ins)"
        )


def wer(reference: Sequence[str], hypothesis: Sequence[str]) -> Alignment:
    """Align two token sequences and count the edits.

    Standard Levenshtein over words, with a backtrace so the counts are reported
    rather than only the distance. Ties are broken deterministically - a match or
    substitution is preferred to a deletion, and a deletion to an insertion - so the
    same pair of inputs always produces the same counts and two runs can be
    compared.

    An empty reference is an error rather than a perfect score: a reference with no
    words means the corpus was not loaded, and reporting ``0.0`` for it would hide a
    broken measurement behind the best possible number.

    Quadratic in the longer sequence, which is fine for a lecture: 2,700 words is
    about seven million cell updates, a second or so of pure Python.
    """

    ref = list(reference)
    hyp = list(hypothesis)
    n, m = len(ref), len(hyp)

    if n == 0:
        raise ValueError("reference is empty; there is nothing to score against")

    # dp[i][j] is the edit distance between ref[:i] and hyp[:j]. Filled row by row
    # rather than with numpy, because the backtrace needs the full table anyway and
    # the table is the only allocation.
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i
    for j in range(1, m + 1):
        dp[0][j] = j

    for i in range(1, n + 1):
        ref_token = ref[i - 1]
        previous, current = dp[i - 1], dp[i]
        for j in range(1, m + 1):
            cost = 0 if ref_token == hyp[j - 1] else 1
            current[j] = min(
                previous[j] + 1,          # deletion
                current[j - 1] + 1,       # insertion
                previous[j - 1] + cost,   # match or substitution
            )

    i, j = n, m
    substitutions = deletions = insertions = 0

    while i > 0 or j > 0:
        if i > 0 and j > 0:
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            if dp[i][j] == dp[i - 1][j - 1] + cost:
                substitutions += cost
                i -= 1
                j -= 1
                continue
        if i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            deletions += 1
            i -= 1
            continue
        insertions += 1
        j -= 1

    return Alignment(
        reference=n,
        substitutions=substitutions,
        deletions=deletions,
        insertions=insertions,
    )


def _char_ngrams(text: str, order: int) -> Counter[str]:
    """Character n-grams with whitespace removed, case preserved.

    Case is preserved because German capitalises every noun: a translation that
    gets the noun right but the capitalisation wrong is a different, and worse,
    result than one that gets both right, and lowercasing would hide it.
    """

    chars = "".join(text.split())
    if len(chars) < order:
        return Counter({chars: 1}) if chars else Counter()
    return Counter(chars[i : i + order] for i in range(len(chars) - order + 1))


def chrf_sentence(reference: str, hypothesis: str, *, order: int = 6, beta: float = 2.0) -> float:
    """chrF for one sentence: averaged n-gram precision and recall, then F-beta.

    The published recipe: for every n from 1 to ``order`` take the character n-gram
    precision and recall, average each across n, then combine the two averages. Not
    an F-beta per n averaged afterwards, which is a subtly different and less
    standard quantity.
    """

    if not hypothesis.strip():
        return 0.0

    precisions: list[float] = []
    recalls: list[float] = []

    for n in range(1, order + 1):
        ref_grams = _char_ngrams(reference, n)
        hyp_grams = _char_ngrams(hypothesis, n)
        matches = sum(min(count, hyp_grams.get(gram, 0)) for gram, count in ref_grams.items())

        hyp_total = sum(hyp_grams.values())
        ref_total = sum(ref_grams.values())
        precisions.append(matches / hyp_total if hyp_total else 0.0)
        recalls.append(matches / ref_total if ref_total else 0.0)

    mean_precision = sum(precisions) / order
    mean_recall = sum(recalls) / order

    if mean_precision + mean_recall == 0.0:
        return 0.0

    beta_squared = beta * beta
    return (1 + beta_squared) * mean_precision * mean_recall / (
        beta_squared * mean_precision + mean_recall
    )


def chrf_corpus(
    pairs: Iterable[tuple[str, str]], *, order: int = 6, beta: float = 2.0
) -> float | None:
    """Mean sentence-level chrF over ``(reference, hypothesis)`` pairs.

    ``None`` for an empty corpus, so a caller can tell "scored zero" from "scored
    nothing", which is the distinction that keeps a broken run from looking like a
    bad one.
    """

    scores = [chrf_sentence(ref, hyp, order=order, beta=beta) for ref, hyp in pairs]
    if not scores:
        return None
    return sum(scores) / len(scores)


@dataclass(frozen=True, slots=True)
class NumberReport:
    """How many source numerals survived into the translation.

    Reference-free on purpose. It needs no German reference translation, only the
    English that went in and the German that came out, so it can be reported for
    every session from the first day rather than once a corpus exists.
    """

    total: int
    kept: int
    misses: tuple[tuple[str, str], ...] = ()

    @property
    def adherence(self) -> float:
        """Fraction of source numerals that appear in the target. ``1.0`` if none."""

        return 1.0 if self.total == 0 else self.kept / self.total

    def as_dict(self) -> dict[str, object]:
        return {
            "source_numbers": self.total,
            "kept": self.kept,
            "adherence": round(self.adherence, 4),
            "misses": [{"number": n, "in": src, "out": de} for n, src, de in self.misses],
        }


def number_preservation(
    pairs: Iterable[tuple[str, str]], *, max_misses: int = 5
) -> NumberReport:
    """Check each source numeral against the target text.

    Substring containment in the normalised target, rather than a token match:
    German writes ``40 %`` where English writes ``40%`` and ``1.000`` where English
    writes ``1,000``, and both are the number arriving correctly. The normaliser
    keeps digits and drops separators, so the comparison is on the digits.
    """

    total = kept = 0
    misses: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for src, de in pairs:
        # Compare against the target's own numerals rather than against every digit
        # in it. Concatenating all digits would let "45" match a target containing
        # "4 apples and 5", which is a false pass on the one error class that
        # matters most.
        target_numbers = {re.sub(r"\D", "", value) for value in numbers(de)}

        for number in numbers(src):
            digits = re.sub(r"\D", "", number)
            if not digits:
                continue
            total += 1
            if digits in target_numbers:
                kept += 1
            elif number not in seen and len(misses) < max_misses:
                seen.add(number)
                misses.append((number, src, de))

    return NumberReport(total=total, kept=kept, misses=tuple(misses))


def identical_to_source_fraction(pairs: Iterable[tuple[str, str]]) -> float | None:
    """Fraction of segments whose German is character-for-character the English.

    Catches the translator that returns its input unchanged, which otherwise looks
    like success: a full screen of confident text in the wrong language. It is a
    strict check and says nothing about a target that merely *resembles* the source,
    so an echo that adds a marker, like ``PassthroughMt`` deliberately does, is visible
    in the text rather than in this number.
    """

    total = same = 0
    for src, de in pairs:
        if not src.strip() or not de.strip():
            continue
        total += 1
        if normalise(src) == normalise(de):
            same += 1

    return None if total == 0 else same / total


def length_ratio(pairs: Iterable[tuple[str, str]]) -> float | None:
    """German characters per English word, as ``docs/09-evaluation.md`` specifies.

    The number that decides whether the overlay's line-length assumptions hold for
    German, which is reliably longer than English.
    """

    words = characters = 0
    for src, de in pairs:
        words += len(tokenise(src))
        characters += len("".join(normalise(de).split()))

    return None if words == 0 else characters / words


def percentiles(values: Iterable[float], ps: Sequence[float] = (50, 95)) -> dict[str, float]:
    """Nearest-rank percentiles, plus ``max`` and ``mean``.

    Nearest-rank rather than interpolated, deliberately: latency is measured, not
    modelled, and interpolating between two observed values invents a number that
    never happened. ``max`` is always reported because a single eight-second freeze
    destroys trust in a way an average hides completely.
    """

    data = sorted(float(value) for value in values)
    if not data:
        return {}

    out: dict[str, float] = {}
    for p in ps:
        rank = math.ceil(p / 100.0 * len(data))
        out[f"p{p:g}"] = data[min(max(rank, 1), len(data)) - 1]

    out["max"] = data[-1]
    out["mean"] = sum(data) / len(data)
    out["count"] = float(len(data))
    return out


@dataclass(slots=True)
class SessionReport:
    """Everything measurable about one scored session.

    A field is ``None`` when it could not be computed rather than when it was zero,
    so a report that is missing its ASR numbers is visibly missing them instead of
    claiming a perfect score.
    """

    reference_words: int
    asr: Alignment | None = None
    asr_numbers: Alignment | None = None
    chrf: float | None = None
    numbers: NumberReport | None = None
    untranslated: float | None = None
    length_ratio: float | None = None
    #: Percentiles of how long each cue was on screen. This is *pacing*, not latency:
    #: perceived latency is ``render_ms - audio_ms`` (``docs/09-evaluation.md``), and a
    #: subtitle file does not carry the emit or render timestamps, so a report built
    #: from one must not claim to have measured latency.
    cue_ms: dict[str, float] | None = None
    segments: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "reference_words": self.reference_words,
            "segments": self.segments,
            "asr": self.asr.as_dict() if self.asr else None,
            "asr_numbers": self.asr_numbers.as_dict() if self.asr_numbers else None,
            "chrf": None if self.chrf is None else round(self.chrf, 4),
            "numbers": self.numbers.as_dict() if self.numbers else None,
            "untranslated": None if self.untranslated is None else round(self.untranslated, 4),
            "length_ratio": (
                None if self.length_ratio is None else round(self.length_ratio, 3)
            ),
            "cue_ms": (
                None
                if self.cue_ms is None
                else {key: round(value, 1) for key, value in self.cue_ms.items()}
            ),
            "notes": list(self.notes),
        }

    def format_text(self) -> str:
        lines: list[str] = [f"reference: {self.reference_words} words"]

        if self.segments:
            lines.append(f"segments : {self.segments}")

        if self.asr is not None:
            lines.append(self.asr.format_text("asr wer "))
            lines.append(f"asr acc : {self.asr.accuracy * 100:.1f}%")

        if self.asr_numbers is not None:
            lines.append(self.asr_numbers.format_text("num wer "))

        if self.chrf is not None:
            lines.append(f"chrf    : {self.chrf:.3f}")

        if self.numbers is not None:
            lines.append(
                f"numbers : {self.numbers.adherence * 100:.1f}% kept "
                f"({self.numbers.kept}/{self.numbers.total})"
            )
            for number, src, de in self.numbers.misses:
                lines.append(f"          lost {number!r} in {src!r} -> {de!r}")

        if self.untranslated is not None:
            lines.append(f"verbatim: {self.untranslated * 100:.1f}% identical to the source")

        if self.length_ratio is not None:
            lines.append(f"length  : {self.length_ratio:.2f} German chars per English word")

        if self.cue_ms:
            rendered = ", ".join(
                f"{key} {value:.0f}" if key == "count" else f"{key} {value:.0f} ms"
                for key, value in self.cue_ms.items()
            )
            lines.append(f"pacing  : cue on screen {rendered}")

        lines.extend(f"note    : {note}" for note in self.notes)
        return "\n".join(lines)


def wer_on_numbers(reference: str, hypothesis: str) -> Alignment | None:
    """WER across the numerals only, or ``None`` when the reference has none.

    This is the slice that predicts complaints. A transcript can score 3% overall
    and be useless if the three errors in it were the three numbers.
    """

    ref_numbers = tokenise_numbers(reference)
    if not ref_numbers:
        return None
    return wer(ref_numbers, tokenise_numbers(hypothesis))


# --- a conversation: which language each line is in ---------------------------------
#
# A conversation's speakers switch between English and German, so the question for every
# subtitle line is whether it is in the language it should be. Function words answer it
# without a model: they are the most frequent words of each language, and a line of a few
# words almost always has some. Words both languages use, such as "in", "so" and "was",
# are in neither list.

_FUNCTION_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        (
            "the", "and", "is", "are", "of", "to", "it", "that", "this", "with",
            "for", "you", "he", "she", "we", "they", "not", "be", "have", "has",
            "had", "at", "on", "but", "his", "her", "from", "or", "as", "by", "would",
            "could", "there", "what", "which", "all", "been", "were", "my", "me",
            "him", "them", "their", "its", "do", "does", "did", "no", "yes",
        )
    ),
    "de": frozenset(
        (
            "der", "die", "das", "und", "ist", "sind", "nicht", "ich", "sie", "er",
            "es", "wir", "ihr", "ein", "eine", "einen", "einem", "einer", "zu", "mit",
            "auf", "für", "von", "den", "dem", "des", "sich", "auch", "als", "wie",
            "aber", "oder", "dass", "noch", "nur", "schon", "wenn", "hat", "haben",
            "wird", "werden", "mir", "mich", "ihm", "ihn", "sein", "seine", "seinen",
            "nach", "bei", "aus", "um", "ja", "nein",
        )
    ),
}


def guess_language(text: str) -> str | None:
    """``en``, ``de`` or ``ar`` for a subtitle line, or ``None`` when it is too short to tell."""

    letters = [char for char in text if char.isalpha()]
    if not letters:
        return None
    arabic = sum(1 for char in letters if "\u0600" <= char <= "\u06ff")
    if arabic * 2 > len(letters):
        return "ar"

    words = re.findall(r"[^\W\d_]+", text.lower())
    hits = {
        code: sum(word in vocabulary for word in words)
        for code, vocabulary in _FUNCTION_WORDS.items()
    }
    best = max(hits, key=hits.__getitem__)
    if hits[best] == 0 or list(hits.values()).count(hits[best]) > 1:
        return None
    return best


@dataclass(frozen=True, slots=True)
class Turn:
    """One stretch of a test conversation, in one spoken language, on the audio clock."""

    language: str
    start_ms: int
    end_ms: int


class _Line(Protocol):
    start_ms: int
    end_ms: int
    text: str


@dataclass(slots=True)
class LanguageCheck:
    """How one subtitle file of a conversation kept to its language."""

    name: str
    cues: int = 0
    #: Lines too short for :func:`guess_language` to tell.
    unknown: int = 0
    #: ``(cue, expected language, guessed language)`` for every line in the wrong one.
    wrong: list[tuple[_Line, str, str]] = field(default_factory=list)
    #: Indexes of the turns that produced no line in this file at all.
    silent_turns: list[int] = field(default_factory=list)

    def format_text(self, turns: Sequence[Turn]) -> str:
        summary = (
            f"{self.name}: {self.cues} lines, {len(self.wrong)} in the wrong language, "
            f"{self.unknown} too short to tell"
        )
        lines = [summary]
        for cue, expected, guessed in self.wrong:
            lines.append(
                f"  {_clock(cue.start_ms)} expected {expected}, read {guessed}: {cue.text}"
            )
        for index in self.silent_turns:
            turn = turns[index]
            span = f"{_clock(turn.start_ms)}-{_clock(turn.end_ms)}"
            lines.append(f"  turn {index} ({turn.language}, {span}) has no line")
        return "\n".join(lines)


def check_languages(
    name: str, cues: Sequence[_Line], turns: Sequence[Turn], *, expected: str | None
) -> LanguageCheck:
    """Check one subtitle file of a conversation against the turns it was made from.

    A target file has one ``expected`` language throughout. The transcript has none: each
    of its lines is held to the language of the turn it overlaps most, so a recogniser
    that wrote English words for German speech shows up here.
    """

    check = LanguageCheck(name=name, cues=len(cues))
    heard: set[int] = set()

    for cue in cues:
        index = _turn_of(cue, turns)
        if index is not None:
            heard.add(index)
        wanted = expected
        if wanted is None and index is not None:
            wanted = turns[index].language
        guessed = guess_language(cue.text)
        if guessed is None:
            check.unknown += 1
        elif wanted is not None and guessed != wanted:
            check.wrong.append((cue, wanted, guessed))

    check.silent_turns = [index for index in range(len(turns)) if index not in heard]
    return check


def _turn_of(cue: _Line, turns: Sequence[Turn]) -> int | None:
    """The turn a line overlaps most, or ``None`` for a line in a gap between turns."""

    best, overlap = None, 0
    for index, turn in enumerate(turns):
        shared = min(cue.end_ms, turn.end_ms) - max(cue.start_ms, turn.start_ms)
        if shared > overlap:
            best, overlap = index, shared
    return best


def _clock(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
