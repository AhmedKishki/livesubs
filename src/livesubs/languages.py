"""Languages, and the modes that pair them.

A *mode* says who is speaking and who may listen in what: an English speaker with German
or Arabic listeners, or a German speaker with English or Arabic listeners. Everything that
used to assume "English in, German out" asks the session's mode instead, so the pipeline,
the hub, the wire format and the viewer carry no language of their own.

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


@dataclass(frozen=True, slots=True)
class Mode:
    """Who speaks, and the languages their listeners can read."""

    #: The language the speaker uses, which the recogniser is told to expect.
    source: str
    #: What listeners can choose. The first is the default: what a plain QR scan gets.
    targets: tuple[str, ...]

    @property
    def default_target(self) -> str:
        return self.targets[0]

    @property
    def label(self) -> str:
        speaker = language(self.source).name
        listeners = " or ".join(language(code).name for code in self.targets)
        return f"{speaker} speaker, {listeners} subtitles"

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
                f"{', '.join(extra)} cannot be a target when the speaker uses "
                f"{language(self.source).name}; this mode offers {', '.join(self.targets)}"
            )
        return Mode(self.source, chosen)


#: The supported pairings, keyed by the language the speaker uses.
MODES: dict[str, Mode] = {
    "en": Mode(source="en", targets=("de", "ar")),
    "de": Mode(source="de", targets=("en", "ar")),
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
