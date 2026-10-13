"""Canned replay harness for developing the overlay without any credentials.

Specified in ``docs/09-evaluation.md``. This is how the largest and most
subjective workstream - the overlay and the pacing - gets built and tuned with
**zero API calls and zero API keys**.

Two things make it useful rather than decorative:

* revisions that REWRITE a clause rather than appending to it, because German
  clause reordering is the hard case for the overlay;
* a ``fast_speaker`` script that deliberately outruns reading speed, so the
  backlog catch-up rule is exercised instead of assumed.

No I/O, no network, no credentials.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from livesubs.events import (
    HelloMessage,
    HelloPayload,
    SegmentMessage,
    SegmentState,
    StatusMessage,
    StatusPayload,
    SubtitleSegment,
    Timing,
)
from livesubs.hub import Hub
from livesubs.languages import MODES, Mode
from livesubs.segmenter import hold_ms

__all__ = ["SCRIPTS", "SimPublisher", "SimScript", "SimSegment", "replay"]


@dataclass(slots=True)
class SimSegment:
    """One scripted line, in every language the simulation speaks.

    ``final`` is what each language's viewer ends up reading, and for the speaker's own
    language it is what was said. ``draft`` is deliberately a different, worse translation:
    real drafts are revised, and the overlay must be designed for that rather than for a
    clean append. A language with no draft shows its final text straight away.
    """

    final: dict[str, str]
    draft: dict[str, str] = field(default_factory=dict)

    def draft_for(self, language: str) -> str:
        return self.draft.get(language, self.final[language])


def _line(
    en: str, de: str, ar: str, *, en_draft: str = "", de_draft: str = "", ar_draft: str = ""
) -> SimSegment:
    drafts = {"en": en_draft, "de": de_draft, "ar": ar_draft}
    return SimSegment(
        final={"en": en, "de": de, "ar": ar},
        draft={code: text for code, text in drafts.items() if text},
    )


@dataclass(slots=True)
class SimScript:
    """A named replay, carrying its own cadence.

    Cadence belongs to the script rather than to the caller because it is what
    decides whether the display can keep up. ``fast_speaker`` needs a cadence
    shorter than a comfortable reading time, otherwise the catch-up rule is never
    exercised and is dead code in practice.
    """

    segments: list[SimSegment]
    cadence_ms: int = 2000


#: Physics-lecture content, chosen because it exercises long German compounds
#: and the hyphenation path in ``docs/06-overlay-spec.md``.
_DEFAULT_SEGMENTS: list[SimSegment] = [
    _line(
        en="Today we will look at damped oscillations.",
        de="Heute betrachten wir gedämpfte Schwingungen.",
        ar="اليوم سنتناول التذبذبات المخمّدة.",
    ),
    _line(
        en="The amplitude decreases exponentially over time.",
        de="Die Amplitude nimmt mit der Zeit exponentiell ab.",
        ar="تتناقص السعة أسّياً مع مرور الوقت.",
        en_draft="The amplitude falls off exponential with the time.",
        de_draft="Die Amplitude nimmt ab exponentiell mit der Zeit.",
        ar_draft="تتناقص السعة مع مرور الوقت بشكل أسّي.",
    ),
    _line(
        en="The damping constant determines how quickly energy is lost.",
        de="Die Dämpfungskonstante bestimmt, wie schnell Energie verloren geht.",
        ar="يحدد ثابت التخميد مدى سرعة فقدان الطاقة.",
        en_draft="The damping constant determines, how energy quickly lost goes.",
        de_draft="Die Dämpfungskonstante bestimmt, wie Energie schnell verloren geht.",
        ar_draft="ثابت التخميد يحدد كم بسرعة الطاقة تُفقد.",
    ),
    _line(
        en="Write this down, because it will appear on the exam.",
        de="Notieren Sie das, denn es wird in der Prüfung vorkommen.",
        ar="دوّنوا هذا، لأنه سيأتي في الامتحان.",
        en_draft="Write that on, because it in the exam appear will.",
        de_draft="Schreiben Sie das auf, weil es in der Prüfung erscheinen wird.",
        ar_draft="اكتبوا هذا لأنه سيظهر في الامتحان.",
    ),
    _line(
        en="Consider a mass on a spring in a viscous medium.",
        de="Betrachten Sie eine Masse an einer Feder in einem viskosen Medium.",
        ar="تأمّلوا كتلة معلّقة بنابض في وسط لزج.",
    ),
    _line(
        en="The resonance frequency shifts slightly.",
        de="Die Resonanzfrequenz verschiebt sich geringfügig.",
        ar="يتغير تردد الرنين بشكل طفيف.",
        en_draft="The resonance frequency moves a little.",
        de_draft="Die Resonanzfrequenz verschiebt sich leicht.",
        ar_draft="ينزاح تردد الرنين قليلاً.",
    ),
]

#: Short lines at a 700 ms cadence. Deliberately far faster than a comfortable
#: reading time for the translation, so this is the only script that genuinely
#: exercises the display catch-up rule - without it that rule would be dead code
#: and would rot. Cadence lives on the script for exactly this reason.
_FAST_SEGMENTS: list[SimSegment] = [
    _line(
        en=f"Point {n}: this follows immediately and matters for the exam.",
        de=f"Punkt {n}, dies folgt unmittelbar und ist prüfungsrelevant.",
        ar=f"النقطة {n}: هذا يلي مباشرة ومهم للامتحان.",
        en_draft=f"Point {n}.",
        de_draft=f"Punkt {n}.",
        ar_draft=f"النقطة {n}.",
    )
    for n in range(1, 12)
]

SCRIPTS: dict[str, SimScript] = {
    "default": SimScript(segments=_DEFAULT_SEGMENTS, cadence_ms=2000),
    "fast_speaker": SimScript(segments=_FAST_SEGMENTS, cadence_ms=700),
    #: A lecture-length run, for judging reading comfort over minutes rather than
    #: seconds. Cycles the default content with numbering so the current pass is
    #: always obvious.
    "lengthy": SimScript(
        segments=[
            SimSegment(
                final={
                    code: f"({cycle}) {text}" if code == "en" else text
                    for code, text in segment.final.items()
                },
                draft=dict(segment.draft),
            )
            for cycle in range(1, 4)
            for segment in _DEFAULT_SEGMENTS
        ],
        cadence_ms=2000,
    ),
}


async def replay(
    script: str = "default",
    *,
    mode: Mode | None = None,
    session_id: str = "sim",
    speed: float = 1.0,
    stop: asyncio.Event | None = None,
    loop: bool = False,
) -> AsyncIterator[HelloMessage | SegmentMessage | StatusMessage]:
    """Emit a scripted lecture as if it were live.

    ``speed=0.0`` yields every frame as fast as possible, for layout work.
    ``speed=1.0`` runs in real time, for pacing work. Anything in between scales
    the inter-frame delay. The script's own cadence decides the segment spacing.

    When ``stop`` is supplied, the replay abandons its waits as soon as the event
    is set and raises :class:`_Stopped`, so shutting the server down never has to
    wait out an inter-segment pause.

    ``loop=True`` repeats the script indefinitely and never emits a terminal status.
    That is what serving uses, and the reason is worth stating: the default script runs
    for twelve seconds, so a one-shot replay is over before anyone has finished walking
    to the phone and scanning the code. The viewer then shows a page that never receives
    a subtitle, which reads as "the QR code is broken" rather than as "the demo already
    finished". A demonstration that has to be caught in a twelve-second window is not a
    demonstration. ``loop=False`` stays the default so tests and ``--replay`` get the
    finite, inspectable sequence they depend on.

    ``mode`` says who is speaking and which languages listeners read: the speaker's lines
    are the source, and every target gets its own stream. Without one, an English speaker.
    """

    mode = mode or MODES["en"]
    if script not in SCRIPTS:
        raise KeyError(f"unknown script {script!r}; available: {sorted(SCRIPTS)}")

    chosen = SCRIPTS[script]
    segments = chosen.segments
    cadence = chosen.cadence_ms

    await _sleep(cadence * 0.3, speed, stop)
    yield HelloMessage(
        data=HelloPayload(
            session_id=session_id,
            session_name=f"sim:{script}",
            source=mode.source,
            targets=list(mode.targets),
        )
    )
    yield StatusMessage(data=StatusPayload(status="live"))

    # Sequence numbers keep counting across passes when looping. The hub refuses
    # anything at or below what a client already holds, so restarting at zero would be
    # rejected as stale and the second pass would never reach a viewer at all.
    completed = 0

    while True:
        for offset, seg in enumerate(segments):
            seq = completed + offset
            src = seg.final[mode.source]

            # Draft first: this is the state the attendee sees while MT is still
            # streaming, and the state the overlay must make look provisional.
            for target in mode.targets:
                yield _segment(seq, 0, SegmentState.DRAFT, target, seg.draft_for(target), src,
                               audio_ms=seq * cadence, emit_ms=seq * cadence + 400)

            # A revision that rewrites the clause, then commits. The pause before it
            # is scaled to a plausible MT latency so the pacing behaves realistically.
            await _sleep(600, speed, stop)
            for target in mode.targets:
                yield _segment(seq, 1, SegmentState.FINAL, target, seg.final[target], src,
                               audio_ms=seq * cadence, emit_ms=seq * cadence + 900)

            # Pace the next segment against the longest line's reading-time hold.
            longest = max(len(seg.final[target]) for target in mode.targets)
            await _sleep(max(cadence, hold_ms(longest)), speed, stop)

        completed += len(segments)

        if not loop:
            yield StatusMessage(
                data=StatusPayload(status="stopped", detail="simulation ended")
            )
            return


def _segment(
    seq: int, rev: int, state: SegmentState, language: str, text: str, src: str,
    *, audio_ms: int, emit_ms: int,
) -> SegmentMessage:
    return SegmentMessage(
        data=SubtitleSegment(
            seq=seq,
            rev=rev,
            state=state,
            language=language,
            text=text,
            src=src,
            timing=Timing(audio_ms=audio_ms, emit_ms=emit_ms),
        )
    )


class _Stopped(Exception):
    """Raised internally when a replay is cut short by the stop event."""


async def _sleep(ms: float, speed: float, stop: asyncio.Event | None = None) -> None:
    """Sleep unless running flat out, in which case just yield.

    Races the sleep against ``stop`` so shutdown is immediate rather than waiting
    out the current inter-segment pause.
    """

    if stop is not None and stop.is_set():
        raise _Stopped

    delay = 0.0 if speed <= 0 else ms / 1000.0 / speed

    if stop is None:
        await asyncio.sleep(delay)
        return

    if delay <= 0:
        await asyncio.sleep(0)
        if stop.is_set():
            raise _Stopped
        return

    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        return
    raise _Stopped


@dataclass(slots=True)
class SimPublisher:
    """A :class:`~livesubs.hub.SegmentPublisher` that replays a canned script.

    This is what lets the overlay, the pacing and the whole delivery path be
    developed and tuned with no credentials, no microphone and no network.
    """

    script: str = "default"
    speed: float = 1.0
    #: Serving sets this; see :func:`replay` for why a demonstration must not expire.
    loop: bool = False
    #: Who is speaking and which languages listeners read.
    mode: Mode = MODES["en"]

    @property
    def supported_languages(self) -> tuple[str, ...]:
        return self.mode.targets

    async def run(self, hub: Hub, *, session_id: str, stop: asyncio.Event) -> None:
        try:
            async for message in replay(
                self.script,
                mode=self.mode,
                session_id=session_id,
                speed=self.speed,
                stop=stop,
                loop=self.loop,
            ):
                # A session announces itself before it starts, under the name it was
                # configured with. The replay's own hello would replace that with the
                # script's name, so it is used only on a hub nobody has announced on.
                if isinstance(message, HelloMessage) and hub.latest_hello is not None:
                    continue
                hub.publish(message)
        except _Stopped:
            return
