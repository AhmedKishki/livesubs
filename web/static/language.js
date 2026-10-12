// Language roles and per-language wire metadata for the Arabic viewer feature.
//
// German stays the default. Arabic is an explicit opt-in that must be chosen in
// the URL, never inferred and never cached: a remembered choice is how a German
// attendee ends up reading Arabic after one experiment, and the URL is the only
// thing a QR code, a bookmark and a manual navigation all agree on.
//
// This module deliberately touches neither localStorage nor the overlay's saved
// preferences. `livesubs.viewer.prefs` holds display choices; language is not one
// of them.

export const DEFAULT_LANGUAGE = "de";

export const LANGUAGES = {
  de: {
    code: "de",
    //: The wire role that makes the server send German only.
    role: "viewer",
    //: The segment field this language renders.
    field: "de",
    htmlLang: "de",
    dir: "ltr",
    label: "Deutsch",
    status: {
      delayed: "Untertitel verzögert",
      stopped: "Untertitel nicht verfügbar",
    },
  },
  ar: {
    code: "ar",
    //: Exact role string agreed with the backend; the server filters Arabic to it.
    role: "viewer_ar",
    field: "ar",
    htmlLang: "ar",
    dir: "rtl",
    label: "العربية",
    status: {
      delayed: "الترجمة متأخرة",
      stopped: "الترجمة غير متوفرة",
    },
  },
};

/** Anything that is not explicitly Arabic is German, including a missing value. */
export function normalizeLanguage(value) {
  return typeof value === "string" && value.trim().toLowerCase() === "ar" ? "ar" : "de";
}

/** The viewer language carried by a URL query string; German when absent. */
export function languageFromSearch(search) {
  const params = new URLSearchParams(search ?? "");
  return normalizeLanguage(params.get("lang"));
}

export function languageConfig(language) {
  return LANGUAGES[normalizeLanguage(language)];
}

export function roleForLanguage(language) {
  return languageConfig(language).role;
}

export function fieldForLanguage(language) {
  return languageConfig(language).field;
}

/** The language a segment carries. Older servers omit it, and that means German. */
export function segmentLanguage(segment) {
  return normalizeLanguage(segment?.language);
}

/**
 * Whether this overlay may render the segment at all.
 *
 * A single-language viewer gets one language from the server, but a mixed feed
 * (a future admin view, or a test) must not let the other language reach the
 * screen. The filter runs before pacing, so an Arabic segment cannot advance a
 * German viewer's `seq`/`rev` state or corrupt its drop count either.
 */
export function acceptsSegment(segment, language) {
  return segmentLanguage(segment) === normalizeLanguage(language);
}

/**
 * The text to render for this overlay's language, or `null` when it cannot.
 *
 * There is no cross-language fallback. Showing German to an Arabic viewer is
 * exactly the misuse the split wire fields exist to prevent, so a missing field
 * renders nothing rather than the wrong language.
 */
export function segmentText(segment, language) {
  if (!acceptsSegment(segment, language)) return null;
  const value = segment?.[fieldForLanguage(language)];
  return typeof value === "string" ? value : null;
}

/**
 * Rewrite a viewer URL to an explicit language choice.
 *
 * German strips the parameter, so the default URL stays exactly what the QR
 * code already encodes. Arabic is explicit. Any other query parameters and the
 * fragment are preserved.
 */
export function viewerUrlWithLanguage(href, language) {
  const url = new URL(href, "http://localhost");
  if (normalizeLanguage(language) === DEFAULT_LANGUAGE) {
    url.searchParams.delete("lang");
  } else {
    url.searchParams.set("lang", normalizeLanguage(language));
  }
  return `${url.pathname}${url.search}${url.hash}`;
}
