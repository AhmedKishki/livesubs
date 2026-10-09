"""Capture backends. The `AudioSource` protocol is fixed; backends are pluggable."""

from livesubs.audio.base import TARGET_CHANNELS, TARGET_RATE, AudioFrame, AudioSource

__all__ = ["TARGET_CHANNELS", "TARGET_RATE", "AudioFrame", "AudioSource"]
