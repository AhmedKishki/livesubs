# Subtitle overlay

## Layout

```css
.overlay-root {
  position: fixed;
  inset: auto 0 8vh 0;          /* bottom-anchored, safe-area respected */
  display: flex;
  justify-content: center;
  pointer-events: none;
  z-index: 2147483647;          /* above everything in the page */
}

.subtitle-layer {
  position: absolute;
  bottom: 0;                    /* the block grows upward from here */
  inline-size: min(92vw, 1600px);
}

.subtitle {
  text-align: center;
  font-size: clamp(24px, min(4.2vh, 7vw), 72px);
  line-height: 1.28;
  font-weight: 600;
  text-wrap: balance;
  hyphens: auto;                /* needs the lang attribute on the element */
  overflow-wrap: break-word;
  min-height: 2.56em;           /* reserve two lines */
}
```

- `bottom: 0` makes the block grow upward from the anchor, 8vh above the bottom edge. Top placement uses `top: 0` and grows downward.
- The font-size clamp uses viewport height and width. On an 844px-high, 390px-wide phone, 4.2vh is about 35px, while the 7vw term limits the preferred size to about 27px.
- `_fit()` measures candidate sizes against browser layout and reduces the font to a 16px floor.
- At the floor, the block grows to fit the text instead of truncating it.
- The renderer reserves two lines of height and permits additional lines. Glyph widths and browser hyphenation determine line breaks.

## Typography

- Font stack: `Inter, "Source Sans 3", system-ui, sans-serif`.
- Avoid condensed and display faces for long compound nouns. The 15–20% character-growth estimate is a layout assumption, not a bound on translations or pixel width.
- Set `lang` to the viewer's language on the subtitle element and use `hyphens: auto`. German contains long compounds, and hyphenation silently does nothing if the language attribute is missing. The element also carries `dir` from the language, so Arabic lays out right-to-left.
- Use weight 600 with normal letter-spacing. Do not use all-caps.

## Visual states

- Draft, final and stopped states use different text opacity.

| State | Treatment | Meaning |
| --- | --- | --- |
| `draft` | text at `rgba(255,255,255,0.62)` | machine output that may still change |
| `final` | text at `#FFFFFF`, full | committed for this segment |
| `stopped` | text at 0.4 plus a prominent status pill | pipeline unavailable |

- State changes do not change the backdrop.

## Backdrop modes

- The viewer saves its selected backdrop in `localStorage`.

| Mode | Rendering |
| --- | --- |
| Box (default) | Uses `rgba(0,0,0,0.55)`, padding `0.35em 0.8em`, an 8px radius and `blur(2px)`. |
| Shadow | Uses `0 2px 6px rgba(0,0,0,0.9), 0 0 2px #000`, without a box. |
| Outline | Uses a `0.02em` black text stroke and a hard shadow. |

## Motion

- A new `seq` cross-fades between two layers, with a 140ms enter transition and a 200ms exit transition.
- The transitions change opacity, not position or scale.
- A revision on the same `seq` updates text in place. Finalisation changes draft opacity to full opacity.

## Pacing

```
holdMs(chars)  = clamp(700, 55 * chars + 400, 6000)   // how long to KEEP a line
MIN_VISIBLE_MS = 800                                   // when it may be REPLACED
```

- A new sequence replaces a settled line after at least `MIN_VISIBLE_MS` from its first display. A draft that has not settled can be replaced immediately.
- A final line retires once its reading hold has elapsed from finalisation. Replacement does not wait for the full reading hold.
- A newer sequence arriving before the replacement floor is dropped, not queued. Bursts after a stall or reconnect can trigger that rule.
- The `fast_speaker` script runs at a 700ms cadence to test dropping within the replacement floor.
- The overlay counts drops and includes the count in acknowledgements. The server does not aggregate those counts for the operator console yet.
- Python and JavaScript implementations share pacing fixtures. Parity checks compare both implementations with the generated vectors.
- Both `hold_ms` and `holdMs` truncate non-integer input toward zero.

## Viewer controls

- The control panel hides automatically. Its toggle accepts pointer events.

| Control | Values | Persisted |
| --- | --- | --- |
| font size | 5 steps | yes |
| position | bottom / top | yes |
| backdrop | box / shadow / outline | yes |
| language | any language the session offers | URL, not local storage |
| fullscreen | button | no |

- Defaults are the session's first target language with the box backdrop: German for an English speaker, English for a German speaker.
- Another language uses `?lang=<code>`, for example `?lang=ar`. The page reads the offered languages, their names and their direction from `/api/session`, and builds the picker from them. The picker is absent when the session offers one language.
- The overlay renders segments of its own language only and ignores the rest before applying pacing. A page whose `hello` names a different language than the page asked for reloads once, because the server restarted in another mode.
- A language the session does not offer shows an "unavailable" notice in that language and opens no socket. The picker stays available.
- Interface strings come from a pack per language in [language.js](../web/static/language.js). A language without a pack uses English strings and takes its name and direction from the session.

## Status handling

- The overlay renders three pipeline states from the `status` message.
- **live:** the status pill is hidden.
- **delayed:** the status pill is visible, without dimming the subtitle.
- **stopped:** the status pill is visible and the last line is dimmed.
- Viewers use status labels in their selected language, not untranslated server details. The operator can inspect those details separately.

## Accessibility

- Target WCAG 2.2 AA contrast against each backdrop. Verify contrast instead of treating the target as a measured result.
- Respect `prefers-reduced-motion` by skipping the cross-fade and using instant swaps.
- Set the page and text span language and direction to those of the selected viewer language.
- Convey no text by colour or position alone.
- Use JavaScript-driven layout only. Do not put meaningful text in CSS `content`.

## Verification

- Check clipping and off-screen layout in a browser render.
- The check needs Node and a Chrome or Chromium binary, so it is optional tooling.
- `uv run python tools/check_overlay_layout.py` measures a small phone, a typical phone and a laptop.
- `uv run python tools/check_overlay_layout.py --language ar` checks Arabic layout and right-to-left clipping at the same sizes. `--language en` checks English. Every language is measured on the simulated lecture's lines.
- `uv run python tools/check_overlay_layout.py --backdrop shadow --shot /tmp/overlay.png` also writes a screenshot. The box backdrop uses `backdrop-filter`, which headless Chrome does not paint, so a screenshot needs `--backdrop shadow`.
- The tool fails when a line is clipped, laid out past the bottom edge, or emptied after it appears.

## Unsupported features

- Source word timings do not provide target-language word alignment. Word-by-word karaoke is not implemented.
- The viewer has no speaker labels and assumes a single presenter.
- The viewer has no scrolling transcript. Final-line history is available in the operator page.
