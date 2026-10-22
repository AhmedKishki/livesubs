"""Languages, and the modes that pair them.

A *mode* says who is speaking and who may listen in what: an English speaker with German
or Arabic listeners, a German speaker with English or Arabic listeners, or a conversation
whose speakers switch between English and German, with English, German or Arabic listeners.
Everything that used to assume "English in, German out" asks the session's mode instead, so
the pipeline, the hub, the wire format and the viewer carry no language of their own.

A conversation's ``source`` names every language spoken, joined by ``+``: ``en+de``. Each
listener still reads one language throughout, whoever is speaking, so every segment goes to
every listened-to language's translator, including the one it was spoken in.

Language metadata lives here and nowhere else: the name a translation prompt uses, the
name a viewer sees in its picker, the writing direction. The first target of a mode is the
one a plain QR scan reaches; every other target needs its explicit role, so a listener
never lands in a language by accident.

Adding a language is an entry in ``LANGUAGES``. Adding a mode is an entry in ``MODES``.
Neither needs an edit anywhere else, except the viewer's interface strings for a new
language in ``web/static/language.js``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "LANGUAGES",
    "MODES",
    "Language",
    "Mode",
    "language",
    "mode_for",
    "names",
    "spoken",
]


@dataclass(frozen=True, slots=True)
class Language:
    """Everything the program needs to know about one language."""

    code: str
    #: The English name, used in translation prompts and in log lines.
    name: str
    #: The name in that language, which is what a viewer's language picker shows.
    native: str
    #: A phrase naming the people who read it, for the translation prompt.
    audience: str
    #: What a translator should write: "the plain, spoken ``style`` a lecturer would use".
    style: str
    #: A short sentence in that language, for the throwaway request that warms a translator.
    greeting: str = "Hello."
    rtl: bool = False

    @property
    def direction(self) -> str:
        return "rtl" if self.rtl else "ltr"


LANGUAGES: dict[str, Language] = {
    "en": Language(
        code="en",
        name="English",
        native="English",
        audience="an English-speaking audience",
        style="English",
        greeting="Hello.",
    ),
    "de": Language(
        code="de",
        name="German",
        native="Deutsch",
        audience="a German-speaking audience",
        style="German",
        greeting="Hallo.",
    ),
    "ar": Language(
        code="ar",
        name="Arabic",
        native="العربية",
        audience="an Arabic-speaking audience",
        style="Modern Standard Arabic",
        greeting="مرحبا.",
        rtl=True,
    ),
}


def language(code: str) -> Language:
    """The language registered under ``code``. Raises ``ValueError`` for an unknown one."""

    try:
        return LANGUAGES[code]
    except KeyError:
        known = ", ".join(sorted(LANGUAGES))
        raise ValueError(f"unknown language {code!r}; known: {known}") from None


def spoken(source: str) -> tuple[str, ...]:
    """The languages a mode's ``source`` names: one speaker's, or a conversation's."""

    return tuple(source.split("+"))


def names(codes: Sequence[str]) -> str:
    """Languages by their English names: "English", "English and German"."""

    named = [language(code).name for code in codes]
    if len(named) < 2:
        return "".join(named)
    return f"{', '.join(named[:-1])} and {named[-1]}"


@dataclass(frozen=True, slots=True)
class Mode:
    """Who speaks, and the languages their listeners can read."""

    #: What the recogniser is told to expect: the speaker's language, or every language of
    #: a conversation joined by ``+``.
    source: str
    #: What listeners can choose. The first is the default: what a plain QR scan gets.
    targets: tuple[str, ...]

    @property
    def default_target(self) -> str:
        return self.targets[0]

    @property
    def spoken(self) -> tuple[str, ...]:
        return spoken(self.source)

    @property
    def is_conversation(self) -> bool:
        """Whether speakers switch languages, so a segment may be read in the one it was said in."""

        return len(self.spoken) > 1

    @property
    def label(self) -> str:
        speakers = f"{names(self.spoken)} {'conversation' if self.is_conversation else 'speaker'}"
        listeners = [language(code).name for code in self.targets]
        if len(listeners) > 1:
            listeners = [", ".join(listeners[:-1]), listeners[-1]]
        return f"{speakers}, {' or '.join(listeners)} subtitles"

    def narrowed(self, targets: Sequence[str]) -> Mode:
        """The same speaker with fewer listeners' languages, in the order given.

        For a deployment that wants, say, German only. Every entry must already be a
        target of the mode: a mode cannot be widened, because no pairing of languages is
        supported that it does not list.
        """

        chosen = tuple(dict.fromkeys(targets))
        if not chosen:
            return self
        extra = [code for code in chosen if code not in self.targets]
        if extra:
            raise ValueError(
                f"{', '.join(extra)} cannot be a target when the speakers use "
                f"{names(self.spoken)}; this mode offers {', '.join(self.targets)}"
            )
        return Mode(self.source, chosen)


#: The supported pairings, keyed by the language the speaker uses, or by a conversation's
#: languages. The recogniser switches between English and German in one stream but cannot
#: add Arabic, so Arabic is only ever read.
MODES: dict[str, Mode] = {
    "en": Mode(source="en", targets=("de", "ar")),
    "de": Mode(source="de", targets=("en", "ar")),
    "en+de": Mode(source="en+de", targets=("en", "de", "ar")),
}


def mode_for(speaker: str, targets: Sequence[str] = ()) -> Mode:
    """The mode for a speaker language, optionally narrowed to some of its targets."""

    try:
        mode = MODES[speaker]
    except KeyError:
        known = ", ".join(sorted(MODES))
        raise ValueError(
            f"no mode for a {speaker!r} speaker; supported speaker languages: {known}"
        ) from None
    return mode.narrowed(targets)
