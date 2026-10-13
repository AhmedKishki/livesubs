// Languages, roles, and the words the pages use in each language.
//
// The server decides which languages exist and which of them a session offers, and
// publishes that list with each language's name and direction at /api/session. This
// module owns the two things the server cannot know: the interface strings, and the
// rules that turn a URL plus a session into a role.
//
// The listener's language is a choice made in the URL, never inferred and never cached.
// A remembered choice is how an attendee ends up reading a language they only tried once,
// and the URL is the one thing that a QR code, a bookmark and a manual navigation all
// agree on. Without `?lang=`, the viewer gets the session's default language, which is
// what the plain QR code reaches. This module touches neither localStorage nor the
// overlay's saved display preferences.

//: The strings used when a language has no pack of its own, and the pack a page speaks
//: before it knows the listener's language.
export const FALLBACK_LANGUAGE = "en";

const PACKS = {
  en: {
    dir: "ltr",
    title: "Subtitles",
    connecting: "Connecting…",
    noSession: "No session in the address.",
    settings: "Settings",
    language: "Language",
    fontSize: "Text size",
    position: "Position",
    background: "Background",
    fullscreen: "Full screen",
    positions: { bottom: "bottom", top: "top" },
    backdrops: { box: "Box", shadow: "Shadow", outline: "Outline" },
    status: {
      delayed: "Subtitles delayed",
      stopped: "Subtitles unavailable",
      unavailable: "These subtitles are not available",
    },
    join: {
      title: "Subtitles – Join",
      qrAlt: "QR code to scan",
      qrError: "The QR code could not be loaded. Please use the address below.",
      live: "Live subtitles",
      noAccount: "No app, no login",
      stored: "Only your display settings are stored on your device",
      recorded: "The audio of this session is recorded",
      connected: "connected",
    },
  },
  de: {
    dir: "ltr",
    title: "Untertitel",
    connecting: "Verbinde …",
    noSession: "Keine Sitzung in der Adresse.",
    settings: "Einstellungen",
    language: "Sprache",
    fontSize: "Schriftgröße",
    position: "Position",
    background: "Hintergrund",
    fullscreen: "Vollbild",
    positions: { bottom: "unten", top: "oben" },
    backdrops: { box: "Kasten", shadow: "Schatten", outline: "Kontur" },
    status: {
      delayed: "Untertitel verzögert",
      stopped: "Untertitel nicht verfügbar",
      unavailable: "Diese Untertitel sind nicht verfügbar",
    },
    join: {
      title: "Untertitel – Beitreten",
      qrAlt: "QR-Code zum Scannen",
      qrError: "Der QR-Code konnte nicht geladen werden. Bitte die Adresse unten verwenden.",
      live: "Live-Untertitel",
      noAccount: "Keine App, kein Login",
      stored: "Auf Ihrem Gerät werden nur Ihre Anzeigeeinstellungen gespeichert",
      recorded: "Der Ton dieser Sitzung wird aufgezeichnet",
      connected: "verbunden",
    },
  },
  ar: {
    dir: "rtl",
    title: "الترجمة",
    connecting: "جارٍ الاتصال…",
    noSession: "لا توجد جلسة في العنوان.",
    settings: "الإعدادات",
    language: "اللغة",
    fontSize: "حجم الخط",
    position: "الموضع",
    background: "الخلفية",
    fullscreen: "ملء الشاشة",
    positions: { bottom: "أسفل", top: "أعلى" },
    backdrops: { box: "مربع", shadow: "ظل", outline: "حدود" },
    status: {
      delayed: "الترجمة متأخرة",
      stopped: "الترجمة غير متوفرة",
      unavailable: "هذه الترجمة غير متوفرة",
    },
    join: {
      title: "الترجمة – الانضمام",
      qrAlt: "رمز QR للمسح",
      qrError: "تعذر تحميل رمز QR. يرجى استخدام العنوان أدناه.",
      live: "ترجمة نصية مباشرة",
      noAccount: "بلا تطبيق وبلا تسجيل دخول",
      stored: "تُحفظ على جهازك إعدادات العرض فقط",
      recorded: "يتم تسجيل صوت هذه الجلسة",
      connected: "متصل",
    },
  },
};

/** The interface strings for a language, in English when there is no pack for it. */
export function stringsFor(language) {
  return PACKS[language] ?? PACKS[FALLBACK_LANGUAGE];
}

/** Which way a language's text runs: what the server says, else what its pack says. */
export function directionOf(language, session = null) {
  const described = session?.targets?.find((target) => target.code === language);
  return described?.dir ?? PACKS[language]?.dir ?? "ltr";
}

/** A language's name in itself, for the picker. */
export function nativeNameOf(language, session = null) {
  const described = session?.targets?.find((target) => target.code === language);
  return described?.native ?? language;
}

/**
 * The language a URL asks for, or `null` when it asks for none.
 *
 * `null` means "whatever this session's default is", which is what a plain QR scan wants.
 * Only the shape of the code is checked here, because which languages exist is the
 * server's to say: a code the session does not offer is refused there.
 */
export function languageFromSearch(search) {
  const value = new URLSearchParams(search ?? "").get("lang");
  const code = typeof value === "string" ? value.trim().toLowerCase() : "";
  return /^[a-z]{2,3}$/.test(code) ? code : null;
}

/** The language this page will show: the one asked for, else the session's default. */
export function chooseLanguage(requested, session) {
  return requested ?? session.default;
}

/** Whether the session can produce `language` at all. */
export function isOffered(language, session) {
  return Array.isArray(session?.languages) && session.languages.includes(language);
}

/**
 * The WebSocket role for `language`.
 *
 * The session's default language is the plain `viewer` role. Every other language is
 * named explicitly, which is the only way a device receives anything but the default.
 */
export function roleFor(language, session) {
  return language === session.default ? "viewer" : `viewer_${language}`;
}

/** Whether a segment is written in `language`. */
export function acceptsSegment(segment, language) {
  return typeof language === "string" && segment?.language === language;
}

/**
 * The text to render for this overlay's language, or `null` when it cannot.
 *
 * There is no cross-language fallback. Showing the wrong language to a listener is
 * exactly the misuse the per-language filter exists to prevent, so a segment of another
 * language renders nothing rather than the wrong text.
 */
export function segmentText(segment, language) {
  if (!acceptsSegment(segment, language)) return null;
  return typeof segment.text === "string" ? segment.text : null;
}

/**
 * Rewrite a viewer URL to an explicit language choice.
 *
 * The default language strips the parameter, so the default URL stays exactly what the
 * QR code already encodes. Any other language is explicit. Other query parameters and
 * the fragment are preserved.
 */
export function viewerUrlWithLanguage(href, language, defaultLanguage) {
  const url = new URL(href, "http://localhost");
  if (language === defaultLanguage) {
    url.searchParams.delete("lang");
  } else {
    url.searchParams.set("lang", language);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}
