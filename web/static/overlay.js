// The movie-like subtitle renderer. This is the deliverable; everything
// upstream exists to feed it. Spec: docs/06-overlay-spec.md.
//
// Two layers are cross-faded rather than one element swapping its text, because
// fade-out-then-in leaves a visible ~340 ms gap on every line change.
//
// Note on revisions: a `final` line is never revised by protocol, so the only
// in-place edits happen while a line is still `draft`. That is why draft text is
// dim - the reader already expects it to change - and no special rewrite
// animation is needed. The protocol removes a whole class of visual problem.

import { Action, decideSegment, holdMs, shouldRetire } from "./pacing.js";
import { languageConfig, segmentText } from "./language.js";

//: Matches --fade-out-ms in styles.css. Duplicated because the JS needs it to
//: know when the outgoing layer may be cleared.
const FADE_OUT_MS = 200;

//: Share of the viewport height a subtitle block may occupy before the font shrinks.
//: The block sits 8vh off the bottom edge and the status pill takes the top strip, so
//: this keeps the two comfortably apart at any screen size.
const BLOCK_HEIGHT_RATIO = 0.42;

//: Where shrinking stops. Below this the text is no longer readable at arm's length, so
//: the block is allowed to grow instead. Nothing is cut either way, which is the point.
const MIN_FONT_PX = 16;

//: One shrink step. Converges from the largest preferred size down to the floor in about
//: fifteen measurements, which is nothing next to a frame.
const FIT_STEP = 0.94;

const PREFS_KEY = "livesubs.viewer.prefs";

const DEFAULT_PREFS = {
  fontScale: 1,
  position: "bottom",
  backdrop: "box",
};

// One knob per step, so the panel stays a couple of buttons wide.
const FONT_STEPS = [0.75, 0.9, 1, 1.2, 1.45];
const POSITIONS = ["bottom", "top"];
const BACKDROPS = ["box", "shadow", "outline"];

export function loadPrefs() {
  try {
    const raw = window.localStorage.getItem(PREFS_KEY);
    return raw ? { ...DEFAULT_PREFS, ...JSON.parse(raw) } : { ...DEFAULT_PREFS };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

export function savePrefs(prefs) {
  try {
    window.localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    /* private mode: preferences simply do not persist */
  }
}

export class SubtitleOverlay {
  constructor(root, { onAck, prefs = loadPrefs(), language = "de" } = {}) {
    this.root = root;
    this.onAck = onAck ?? (() => {});
    this.prefs = prefs;
    //: The one language this overlay renders. The default keeps every existing
    //: caller German, and the page's URL decides the rest.
    this.language = language;
    this.config = languageConfig(language);

    this.current = null;
    this.dropped = 0;

    this.layers = [this._makeLayer(), this._makeLayer()];
    for (const layer of this.layers) root.append(layer);
    this.activeIndex = 0;

    this._timer = null;
    this.applyPrefs(this.prefs);

    // A rotated phone or a resized window changes how much room the text has, so the
    // line on screen is re-fitted rather than left at the size that suited the old
    // viewport.
    window.addEventListener("resize", () => this._refit());
  }

  _makeLayer() {
    const layer = document.createElement("div");
    layer.className = "subtitle-layer";

    const box = document.createElement("div");
    box.className = "subtitle";

    const text = document.createElement("span");
    text.className = "subtitle-text";
    // `lang="de"` is what enables German hyphenation. Without it, `hyphens:
    // auto` silently does nothing and long compounds overflow the line. The
    // Arabic view carries `lang="ar"` and `dir="rtl"` instead.
    text.setAttribute("lang", this.config.htmlLang);
    text.setAttribute("dir", this.config.dir);

    box.append(text);
    layer.append(box);
    return layer;
  }

  get activeLayer() {
    return this.layers[this.activeIndex];
  }

  get idleLayer() {
    return this.layers[1 - this.activeIndex];
  }

  applyPrefs(prefs) {
    this.prefs = prefs;
    const scale = prefs.fontScale ?? 1;
    const root = document.documentElement;
    // The same rule as --subtitle-font-size in styles.css, scaled by the viewer's
    // preference. Both terms are scaled, so a phone keeps its width bound and a laptop
    // keeps its height bound at every step.
    root.style.setProperty(
      "--subtitle-font-size",
      `clamp(24px, min(${(4.2 * scale).toFixed(2)}vh, ${(7 * scale).toFixed(2)}vw), ${Math.round(72 * scale)}px)`,
    );
    this.root.classList.toggle("is-top", prefs.position === "top");
    this.root.classList.remove("backdrop-box", "backdrop-shadow", "backdrop-outline");
    this.root.classList.add(`backdrop-${prefs.backdrop ?? "box"}`);
    savePrefs(prefs);

    // The preferred size just changed, so the size the fit settled on is stale.
    this._refit();
  }

  /** Apply one segment from the wire, if the pacing policy says we may. */
  offer(segment, receivedAt = performance.now()) {
    // The selected language's field, or nothing. A segment of the other language
    // is ignored before pacing, so it can neither advance our `seq`/`rev` state
    // nor count as a drop; the server already filters, and this is the second
    // line of defence for a mixed feed.
    const text = segmentText(segment, this.language);
    if (text === null) return Action.IGNORE;

    const incoming = { seq: segment.seq, rev: segment.rev, chars: text.length };
    const action = decideSegment(this.current, incoming, receivedAt);

    if (action === Action.IGNORE) return action;

    if (action === Action.DROP) {
      // Correct behaviour, not a bug: a lost line beats lag that grows without
      // bound. Counted so rehearsal can see it happening.
      this.dropped += 1;
      this._ack(segment, receivedAt);
      return action;
    }

    if (action === Action.UPDATE) {
      this.current.rev = segment.rev;
      this.current.chars = incoming.chars;
      this.current.text = text;
      this._paint(this.activeLayer, text, segment.state);
      this._fit(this.activeLayer);
      this.current.finalAt ??= segment.state === "final" ? receivedAt : null;
      this._armRetire();
      this._ack(segment, receivedAt);
      return action;
    }

    this._swapIn(text, segment, receivedAt);
    this._ack(segment, receivedAt);
    return action;
  }

  // --- sizing ---------------------------------------------------------------

  /**
   * Size the text to the room it has, so that all of it is shown.
   *
   * The block used to be clamped to two lines. That is right for a caption on a
   * projected screen and wrong for the phone in an attendee's hand, where German at
   * that size routinely needs four: the clamp removed the rest of the sentence and gave
   * no sign that it had, so a lecture could be followed while quietly missing its
   * endings. The size gives way instead, down to a legibility floor - below that the
   * block grows rather than shrink further, because a taller block is readable and a
   * truncated one is not.
   *
   * Measured rather than estimated: the preferred size is read back from the stylesheet,
   * which is viewport-relative and already scaled by the viewer's own preference, and
   * the fit is a real layout rather than a character count, because where a line breaks
   * depends on the glyphs and on the hyphenation the browser is willing to apply.
   */
  _fit(layer) {
    const box = layer.querySelector(".subtitle");
    const text = box.querySelector(".subtitle-text");
    if (text.textContent === "") return;

    // Clearing the inline size restores the stylesheet's preferred size.
    box.style.fontSize = "";
    const preferred = parseFloat(window.getComputedStyle(box).fontSize);
    if (!Number.isFinite(preferred)) return;

    const ceiling = window.innerHeight * BLOCK_HEIGHT_RATIO;
    let size = preferred;

    for (;;) {
      box.style.fontSize = `${size}px`;
      if (box.scrollHeight <= ceiling) return;
      if (size <= MIN_FONT_PX) return;
      size = Math.max(MIN_FONT_PX, size * FIT_STEP);
    }
  }

  /** Re-fit the line on screen, if there is one. */
  _refit() {
    if (this.current !== null) this._fit(this.activeLayer);
  }

  // --- rendering ------------------------------------------------------------

  _paint(layer, text, state) {
    layer.querySelector(".subtitle-text").textContent = text;
    layer.classList.toggle("is-draft", state !== "final");
    layer.classList.toggle("is-final", state === "final");
  }

  _swapIn(text, segment, now) {
    const outgoing = this.activeLayer;
    const incoming = this.idleLayer;

    this._paint(incoming, text, segment.state);
    // Sized before it is shown: a layer that fades in at the wrong size and then
    // corrects itself is more distracting than the wrong size alone.
    this._fit(incoming);
    incoming.classList.remove("is-leaving");

    // Force a style recalculation so the browser treats this as a fresh
    // transition instead of coalescing it with the previous frame.
    void incoming.offsetWidth;
    incoming.classList.add("is-visible");

    if (outgoing.classList.contains("is-visible")) {
      outgoing.classList.add("is-leaving");
      outgoing.classList.remove("is-visible");
      window.setTimeout(() => {
        outgoing.classList.remove("is-leaving");
        // Only clear it if a later swap has not made it the visible layer again. Two
        // lines arriving less than a fade apart would otherwise let this callback
        // blank the line that is on screen, which is the one thing a subtitle must
        // never do. Segmenting normally spaces lines by seconds, so this is a latent
        // fault rather than a visible one - but a burst after a network stall is
        // exactly when the viewer needs the next line to appear.
        if (outgoing !== this.activeLayer) {
          outgoing.querySelector(".subtitle-text").textContent = "";
        }
      }, FADE_OUT_MS);
    }

    this.activeIndex = 1 - this.activeIndex;
    this.current = {
      seq: segment.seq,
      rev: segment.rev,
      chars: text.length,
      text,
      shownAt: now,
      finalAt: segment.state === "final" ? now : null,
    };

    this._armRetire();
  }

  // --- retirement -----------------------------------------------------------

  _armRetire() {
    if (this._timer !== null) {
      window.clearTimeout(this._timer);
      this._timer = null;
    }
    if (this.current === null || this.current.finalAt === null) return;

    const remaining = holdMs(this.current.chars) - (performance.now() - this.current.finalAt);
    if (remaining <= 0) return;

    this._timer = window.setTimeout(() => {
      this._timer = null;
      if (shouldRetire(this.current, performance.now())) this._retire();
    }, remaining);
  }

  _retire() {
    const layer = this.activeLayer;
    if (layer.classList.contains("is-visible")) {
      layer.classList.add("is-leaving");
      layer.classList.remove("is-visible");
      window.setTimeout(() => layer.classList.remove("is-leaving"), FADE_OUT_MS);
    }
    this.current = null;
  }

  // --- status ---------------------------------------------------------------

  setStatus(status, detail, pill) {
    const stalled = status !== "live";
    this.root.classList.toggle("is-stalled", status === "stopped");

    if (!pill) return;
    // The viewer shows its own language's label and never the server's raw
    // detail, which is currently English. The operator console reads `detail`
    // directly instead, so an untranslated diagnostic stays visible there.
    pill.textContent = stalled ? (this.config.status[status] ?? status) : "";
    pill.classList.toggle("is-visible", stalled);
    pill.classList.toggle("is-stopped", status === "stopped");
  }

  // --- telemetry ------------------------------------------------------------

  _ack(segment, receivedAt) {
    // `rendered_in_ms` is clock-free, so it is meaningful without any clock
    // synchronisation between server and viewer. A true end-to-end figure needs
    // an offset estimate between the two clocks, which is P5 metrics work; the
    // wire field `timing.render_ms` stays unused until then rather than being
    // filled with a value from a different clock.
    this.onAck({
      type: "ack",
      seq: segment.seq,
      rev: segment.rev,
      rendered_in_ms: Math.round(performance.now() - receivedAt),
      dropped: this.dropped,
    });
  }
}

export { FONT_STEPS, POSITIONS, BACKDROPS };
