"""Provider-neutral speaker choices, independent of subtitle target languages."""

import re
from dataclasses import dataclass

MULTILINGUAL_SOURCE = "multilingual"


@dataclass(frozen=True, slots=True)
class SpeakerOption:
    code: str
    name: str


DEFAULT_SPEAKER_OPTIONS = (
    SpeakerOption("auto", "Automatic (English/German)"),
    SpeakerOption("en", "English"),
    SpeakerOption("de", "German"),
    SpeakerOption("ar", "Arabic"),
)


def normalize_speaker(value: str) -> str:
    """Canonicalise supported-code syntax; providers validate actual coverage."""
    value = value.strip()
    if value.lower() in {"auto", "en+de"}:
        return "auto"
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value):
        raise ValueError(f"invalid speaker language {value!r}; use auto or a language code")
    parts = value.split("-")
    result = [parts[0].lower()]
    for part in parts[1:]:
        result.append(part.title() if len(part) == 4 and part.isalpha()
                      else part.upper() if len(part) == 2 and part.isalpha() else part.lower())
    return "-".join(result)


def routing_language(code: str) -> str:
    """Collapse target locales only; preserve other explicit language evidence."""
    normalized = code.strip().lower().replace("_", "-")
    base = normalized.split("-", 1)[0]
    return base if base in {"en", "de", "ar"} else normalized
