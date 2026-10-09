"""HTTP and WebSocket delivery.

The overlay is the product; this package serves it and pushes subtitles to it.
See ``docs/07-delivery-qr.md``.
"""

from livesubs.server.app import create_app
from livesubs.server.sessions import Session, SessionRegistry

__all__ = ["Session", "SessionRegistry", "create_app"]
