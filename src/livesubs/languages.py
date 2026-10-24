"""Language metadata and recognition modes with a fixed English/German/Arabic offer.

``auto`` is a recognition setting, never a language code. Its adapter recognition
language is ``en+de``. Manual pins change recognition, not subtitle availability.
The session's fallback is configured independently of target order.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from dataclasses import dataclass

from livesubs.speakers import normalize_speaker

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

    named = [LANGUAGES[code].name if code in LANGUAGES else code for code in codes]
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
        """Accept a legacy full offer, but reject narrowing or duplicate entries."""

        chosen = tuple(targets)
        if not chosen:
            return self
        if len(chosen) != 3 or set(chosen) != {"en", "de", "ar"}:
            raise ValueError(
                "remove session.targets: every session offers en, de, ar; "
                "subsets, duplicates and unsupported targets are not supported"
            )
        return self


#: Common recognition settings; other syntactically valid manual codes are built on demand.
MODES: dict[str, Mode] = {
    "auto": Mode(source="en+de", targets=("en", "de", "ar")),
    "en": Mode(source="en", targets=("en", "de", "ar")),
    "de": Mode(source="de", targets=("en", "de", "ar")),
    "ar": Mode(source="ar", targets=("en", "de", "ar")),
    "en+de": Mode(source="en+de", targets=("en", "de", "ar")),
}


def mode_for(speaker: str, targets: Sequence[str] = ()) -> Mode:
    """The mode for a speaker language, optionally narrowed to some of its targets."""

    if speaker.strip().lower() == "en+de":
        warnings.warn("session.speaker en+de is deprecated; use auto instead", stacklevel=2)
        speaker = "auto"
    speaker = normalize_speaker(speaker)
    mode = MODES.get(speaker, Mode(source=speaker, targets=("en", "de", "ar")))
    return mode.narrowed(targets)
