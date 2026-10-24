"""Documented /v1/listen monolingual capabilities, checked 2026-10-24.

Authority: https://developers.deepgram.com/docs/models-languages-overview.md
Model options: https://developers.deepgram.com/docs/model.md
Do not merge multilingual/hint coverage into these manual-language sets.
"""

from livesubs.speakers import SpeakerOption, normalize_speaker

SOURCE_URL = "https://developers.deepgram.com/docs/models-languages-overview.md"
CHECKED_DATE = "2026-10-24"

# One source row per documented language; regional codes retain their exact spelling.
_NOVA3 = (
    ("Afrikaans", "af af-ZA"),
    ("Arabic", "ar ar-AE ar-SA ar-QA ar-KW ar-SY ar-LB ar-PS ar-JO ar-EG ar-SD ar-TD ar-MA ar-DZ ar-TN ar-IQ ar-IR"),
    ("Armenian", "hy"), ("Assamese", "as as-IN"), ("Belarusian", "be"),
    ("Bengali", "bn"), ("Bosnian", "bs"), ("Bulgarian", "bg"), ("Catalan", "ca"),
    ("Chinese (Cantonese, Traditional)", "zh-HK"),
    ("Chinese (Mandarin, Simplified)", "zh zh-CN zh-Hans"),
    ("Chinese (Mandarin, Traditional)", "zh-TW zh-Hant"),
    ("Croatian", "hr"), ("Czech", "cs cs-CZ"), ("Danish", "da da-DK"),
    ("Dutch", "nl"), ("English", "en en-US en-AU en-GB en-IN en-NZ"),
    ("Estonian", "et"), ("Finnish", "fi"), ("Flemish", "nl-BE"),
    ("French", "fr fr-CA"), ("Georgian", "ka ka-GE"), ("German", "de"),
    ("German (Switzerland)", "de-CH"), ("Greek", "el"), ("Gujarati", "gu gu-IN"),
    ("Hebrew", "he"), ("Hindi", "hi"), ("Hungarian", "hu"), ("Indonesian", "id"),
    ("Italian", "it"), ("Japanese", "ja"), ("Kannada", "kn"), ("Kazakh", "kk kk-KZ"),
    ("Korean", "ko ko-KR"), ("Latvian", "lv"), ("Lithuanian", "lt"),
    ("Macedonian", "mk"), ("Malay", "ms"), ("Marathi", "mr"), ("Mongolian", "mn"),
    ("Nepali", "ne"), ("Norwegian", "no"), ("Pashto", "ps ps-AF"), ("Persian", "fa"),
    ("Polish", "pl"), ("Portuguese", "pt pt-BR pt-PT"), ("Punjabi", "pa pa-IN"),
    ("Romanian", "ro"), ("Russian", "ru"), ("Serbian", "sr"), ("Slovak", "sk"),
    ("Slovenian", "sl"), ("Spanish", "es es-419"), ("Swedish", "sv sv-SE"),
    ("Tagalog", "tl"), ("Tamil", "ta"), ("Telugu", "te"), ("Thai", "th th-TH"),
    ("Turkish", "tr tr-TR"), ("Ukrainian", "uk"), ("Urdu", "ur"), ("Vietnamese", "vi"),
)
_NOVA2_CODES = frozenset({
    "bg", "ca", "zh", "zh-CN", "zh-Hans", "zh-TW", "zh-Hant", "zh-HK", "cs",
    "da", "da-DK", "nl", "en", "en-US", "en-AU", "en-GB", "en-NZ", "en-IN",
    "et", "fi", "nl-BE", "fr", "fr-CA", "de", "de-CH", "el", "hi", "hu", "id",
    "it", "ja", "ko", "ko-KR", "lv", "lt", "ms", "no", "pl", "pt", "pt-BR",
    "pt-PT", "ro", "ru", "sk", "es", "es-419", "sv", "sv-SE", "th", "th-TH",
    "tr", "uk", "vi",
})


def _options(rows: tuple[tuple[str, str], ...]) -> tuple[SpeakerOption, ...]:
    return tuple(SpeakerOption(code, name if '-' not in code else f"{name} ({code})")
                 for name, codes in rows for code in codes.split())


NOVA3_MANUAL_OPTIONS = _options(_NOVA3)
NOVA2_MANUAL_OPTIONS = tuple(o for o in NOVA3_MANUAL_OPTIONS if o.code in _NOVA2_CODES)
_SPECIAL3 = _options((("English", "en en-US en-AU en-CA en-GB en-IE en-IN en-NZ"),))
_SPECIAL2 = _options((("English", "en en-US"),))
_MODELS = {
    "nova-3": NOVA3_MANUAL_OPTIONS, "nova-3-general": NOVA3_MANUAL_OPTIONS,
    "nova-2": NOVA2_MANUAL_OPTIONS, "nova-2-general": NOVA2_MANUAL_OPTIONS,
    "nova-3-medical": _SPECIAL3, "nova-3-pharma": _SPECIAL3,
    **{f"nova-2-{suffix}": _SPECIAL2 for suffix in (
        "meeting", "phonecall", "finance", "conversationalai", "voicemail", "video",
        "medical", "drivethru", "automotive", "atc")},
}
_AUTO_MODELS = {"nova-3", "nova-3-general"}


def validate_selection(model: str, speaker: str, language: str | None = None) -> str:
    """Return the exact effective provider code, rejecting incompatible overrides."""
    if model not in _MODELS:
        raise ValueError(f"unsupported Deepgram model {model!r}; no verified language catalogue")
    selected = normalize_speaker(speaker)
    if selected == "auto":
        if model not in _AUTO_MODELS:
            raise ValueError("Automatic recognition requires Deepgram nova-3 general")
        if language is not None and language != "multi":
            raise ValueError("Deepgram language override contradicts Automatic recognition")
        return "multi"
    supported = {option.code for option in _MODELS[model]}
    if selected not in supported:
        raise ValueError(f"Deepgram model {model!r} does not support manual language {selected!r}")
    if language is None:
        return selected
    override = normalize_speaker(language)
    if override not in supported:
        raise ValueError(f"unsupported Deepgram language override {language!r} for model {model!r}")
    if selected != override and not ('-' not in selected and override.startswith(selected + '-')):
        raise ValueError("Deepgram language override contradicts the selected recognition language")
    # zh is explicitly Mandarin Simplified: a base-code match must not erase Cantonese/script.
    if selected == "zh" and override not in {"zh", "zh-CN", "zh-Hans"}:
        raise ValueError("Deepgram language override contradicts Mandarin Simplified selection")
    return override


def speaker_options(model: str = "nova-3", language: str | None = None) -> tuple[SpeakerOption, ...]:
    """Available choices without keys/network, filtered by any forced provider language."""
    if model not in _MODELS:
        raise ValueError(f"unsupported Deepgram model {model!r}; no verified language catalogue")
    options = _MODELS[model]
    if model in _AUTO_MODELS:
        options = (SpeakerOption("auto", "Automatic (English/German)"), *options)
    if language is None:
        return options
    compatible = []
    for option in options:
        try:
            validate_selection(model, option.code, language)
        except ValueError:
            continue
        compatible.append(option)
    if not compatible:
        raise ValueError(f"Deepgram language override {language!r} has no compatible speaker choices")
    return tuple(compatible)
