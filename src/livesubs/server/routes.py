"""HTTP routes: landing page, viewer page, operator console, QR code, session API.

The viewer URL is plain HTTP on purpose. A certificate warning would destroy the
"just scan it" premise, and we do not need a secure context because audio capture
happens server-side. See ``docs/07-delivery-qr.md``.
"""

from __future__ import annotations

import io
import ipaddress
from pathlib import Path
from urllib.parse import urlparse

import qrcode
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from livesubs.config import Settings, checkout_root
from livesubs.events import EVENT_VERSION
from livesubs.languages import LANGUAGES
from livesubs.preflight import lan_ip

__all__ = ["build_router", "viewer_url", "web_dir"]


def web_dir(settings: Settings) -> Path:
    """Where the no-build frontend lives."""

    if settings.web_dir is not None:
        return settings.web_dir
    root = checkout_root()
    if root is not None and (root / "web").is_dir():
        return root / "web"
    # An installed wheel carries the frontend beside the package.
    return Path(__file__).resolve().parent.parent / "web"


def _is_local_only(host: str) -> bool:
    """Whether a host cannot address this machine from anywhere else.

    Three kinds of host fail that test, and all three have been seen in practice:

    - **loopback** — ``localhost``, ``127.0.0.1``, ``::1``. It means "this machine" to
      whoever reads it, so a phone resolves it to the phone itself;
    - **unspecified** — ``0.0.0.0``, ``::``. These are *bind* addresses, not
      destinations, which is what makes them dangerous: a browser on the machine running
      the server accepts one, so it works for the operator and is refused for everybody
      else. The operations doc says to bind ``0.0.0.0``, which puts the pointless
      address directly in front of the operator to type next;
    - **link-local** — ``169.254.*``, ``fe80::*``. Handed out when DHCP failed and
      reachable only across one wire segment.

    Anything else is left alone: reaching the page by a name means that name resolves,
    at least for whoever is looking at it.
    """

    name = host.strip().strip("[]").lower()
    if name in {"localhost", "localhost.localdomain", "ip6-localhost"}:
        return True

    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False

    return address.is_loopback or address.is_unspecified or address.is_link_local


def viewer_url(settings: Settings, session_id: str, base_url: str | None = None) -> str:
    """The URL the QR code points at.

    Resolution order:

    1. an explicit ``LIVESUBS_PUBLIC_URL``;
    2. the host the request arrived on, **unless that host cannot address this machine
       from elsewhere** — a loopback, bind or link-local address;
    3. this machine's LAN address;
    4. the request host after all, as a last resort when no LAN address is found.

    Deriving from the request matters, because otherwise the QR always encodes
    ``localhost`` and a phone scans itself. But deriving *blindly* fails the same way
    from the other direction, and did, twice, from two different addresses:

    - opening the landing page at ``http://localhost:8000`` — the obvious thing to type
      on the machine running the server, and what the README tells the operator to do —
      produced a QR encoding ``localhost``;
    - opening it at ``http://0.0.0.0:8000`` — the address the operations doc tells the
      operator to *bind*, and so the address in front of them — produced a QR encoding
      ``0.0.0.0``. That one opens in a browser on this machine and is refused outright
      on a phone: `ERR_CONNECTION_REFUSED`.

    So any host that cannot address this machine from elsewhere is replaced by the LAN
    address. :func:`_is_local_only` decides, and covers loopback, a bind address and
    link-local, because all three have now been reached in practice.
    """

    if settings.public_url:
        return f"{settings.public_url.rstrip('/')}/s/{session_id}"

    if base_url:
        parsed = urlparse(base_url)
        if parsed.hostname and not _is_local_only(parsed.hostname):
            return f"{base_url.rstrip('/')}/s/{session_id}"

    address = lan_ip()
    if address is not None:
        scheme = urlparse(base_url).scheme if base_url else "http"
        port = (urlparse(base_url).port if base_url else None) or settings.port
        return f"{scheme or 'http'}://{address}:{port}/s/{session_id}"

    if base_url:
        return f"{base_url.rstrip('/')}/s/{session_id}"

    return f"http://localhost:{settings.port}/s/{session_id}"


def language_json(code: str) -> dict[str, str]:
    """What a page needs to know about one language to offer it and to lay it out."""

    info = LANGUAGES[code]
    return {"code": code, "name": info.name, "native": info.native, "dir": info.direction}


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter()
    root = web_dir(settings)

    def page(name: str) -> FileResponse:
        path = root / name
        if not path.is_file():
            raise HTTPException(status_code=500, detail=f"missing web asset: {name}")
        return FileResponse(path)

    def session_json(request: Request, session_id: str) -> JSONResponse:
        session = request.app.state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        return JSONResponse(
            {
                "id": session.id,
                "name": session.name,
                #: Who is speaking, and what listeners can choose. The viewer page builds
                #: its language picker from this, so it offers only what can be produced.
                "source": session.source,
                "default": session.default_language,
                "languages": list(session.targets),
                "targets": [language_json(code) for code in session.targets],
                "version": EVENT_VERSION,
                "viewers": session.hub.viewer_count,
                "viewers_by_language": session.hub.viewers_by_language,
                "connections": session.hub.subscriber_count,
                "running": session.running,
                "uptime_s": round(session.uptime_s, 1),
                "ws_url": f"/ws/{session.id}",
                #: Whether this run keeps the audio, so the page can say so truthfully. The
                #: publisher is asked rather than the settings, because a simulation that
                #: captures nothing keeps nothing whatever the settings say.
                "recording": bool(getattr(session.publisher, "records_audio", False)),
            }
        )

    @router.get("/", include_in_schema=False)
    async def landing() -> FileResponse:
        return page("index.html")

    @router.get("/s/{session_id}", include_in_schema=False)
    async def viewer(session_id: str, request: Request) -> FileResponse:
        """The QR target. An unknown session must 404, never silently serve."""
        if request.app.state.sessions.get(session_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        return page("viewer.html")

    @router.get("/admin", include_in_schema=False)
    async def admin() -> FileResponse:
        return page("admin.html")

    @router.get("/qr.png", include_in_schema=False)
    async def qr(request: Request) -> Response:
        url = viewer_url(settings, settings.session_id, str(request.base_url))
        buffer = io.BytesIO()
        qrcode.make(url).save(buffer, format="PNG")
        return Response(
            content=buffer.getvalue(),
            media_type="image/png",
            # The URL can change between runs, so never let a phone cache it.
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/session/{session_id}")
    async def session_detail(session_id: str, request: Request) -> JSONResponse:
        return session_json(request, session_id)

    @router.get("/api/session")
    async def current_session(request: Request) -> JSONResponse:
        """For the landing page, which does not know the session id up front."""
        return session_json(request, settings.session_id)

    @router.get("/api/qr-url")
    async def qr_url(request: Request) -> JSONResponse:
        """The plain-text URL, shown on the landing page for anyone who cannot scan."""
        return JSONResponse(
            {"url": viewer_url(settings, settings.session_id, str(request.base_url))}
        )

    @router.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        """Liveness probe for a supervisor.

        503 when the pipeline is not running. The server being up says nothing about
        whether anyone is getting subtitles, and a probe that answers 200 either way
        cannot be used to restart a dead one.
        """
        session = request.app.state.sessions.get(settings.session_id)
        running = bool(session and session.running)
        return JSONResponse(
            {
                "ok": running,
                "session": settings.session_id,
                "viewers": session.hub.viewer_count if session else 0,
                "viewers_by_language": session.hub.viewers_by_language if session else {},
                "connections": session.hub.subscriber_count if session else 0,
            },
            status_code=200 if running else 503,
        )

    return router
