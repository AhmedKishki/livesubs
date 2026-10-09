"""Capture backend interface.

Capture is deliberately kept out of the pipeline proper. The pipeline consumes
**16 kHz mono signed-16-bit little-endian PCM frames** and does not care where
they come from, so a backend can be swapped without touching segmentation, ASR,
translation or delivery. See ``docs/11-extending.md``.

Anticipated backends beyond the PipeWire one: a WAV file (for deterministic tests
and golden-set work), a silent source (for watchdog testing), and browser capture
over WebSocket. Linux is the only supported platform; see
``docs/12-configuration.md``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["TARGET_CHANNELS", "TARGET_RATE", "AudioFrame", "AudioSource"]

#: What every backend must deliver, because it is what the ASR providers expect.
TARGET_RATE = 16_000
TARGET_CHANNELS = 1


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """One block of captured audio.

    ``start_ms`` is monotonic from the start of the session and is the pipeline's
    only clock: frame arrival drives the segment cap and the silence detector, so
    there is no separate timer task to drift against the audio.
    """

    pcm: bytes
    start_ms: int
    duration_ms: int


@runtime_checkable
class AudioSource(Protocol):
    """A stream of capture frames."""

    def frames(self) -> AsyncIterator[AudioFrame]:
        """Yield frames as they arrive.

        Must be prompt and monotonic. A backend that stalls causes the segmenter
        to see silence and close segments early, which is worse than an error,
        because it looks like the speaker paused.
        """
        ...

    async def close(self) -> None:
        """Release the device or process. Must be safe to call twice."""
        ...

    @property
    def alive(self) -> bool:
        """Whether the source is still producing.

        Must reflect the *device*, not the last frame delivered, so that a
        silently dead capture is detectable. This is what the watchdog polls, and
        why it can tell a muted microphone from a quiet room.
        """
        ...
