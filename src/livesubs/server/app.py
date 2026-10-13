"""FastAPI application factory.

The publisher is injected rather than chosen here. The CLI decides between the
live pipeline and the simulation replay; the app does not need to know which, and
neither does the WebSocket layer or the overlay. That is the hub seam made
concrete, and it is why the overlay cannot behave differently in a demo and in a
real lecture.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from livesubs.config import Settings
from livesubs.hub import SegmentPublisher
from livesubs.metrics import LoopMonitor
from livesubs.server.routes import build_router, web_dir
from livesubs.server.sessions import Session, SessionRegistry
from livesubs.server.ws import build_router as build_ws_router

__all__ = ["create_app", "log_config"]

logger = logging.getLogger(__name__)


def log_config(level: str) -> dict:
    """uvicorn's logging config, with this package's loggers left enabled.

    uvicorn applies its own config with ``disable_existing_loggers`` set, which silences
    every logger created before it starts - so the pipeline's own INFO lines, including the
    per-stage latency measurement (``docs/09-evaluation.md``), were emitted and thrown away
    in a served run. Adding the package to the config is what keeps them.

    A copy is returned rather than uvicorn's dict mutated, because that dict is module
    state shared with every other caller in the process.
    """

    import copy

    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config["loggers"]["livesubs"] = {
        "handlers": ["default"],
        "level": level.upper(),
        # Handler already carries the formatting uvicorn uses; propagating as well would
        # print each line twice.
        "propagate": False,
    }
    return config


def create_app(
    settings: Settings,
    publisher: SegmentPublisher,
    *,
    session_id: str | None = None,
    session_name: str | None = None,
    on_failure: Callable[[], None] | None = None,
) -> FastAPI:
    """Build the application around one session and one publisher."""

    resolved_id = session_id or settings.session_id
    registry = SessionRegistry()
    loop_monitor = LoopMonitor()
    registry.add(
        Session(
            id=resolved_id,
            name=session_name or settings.session_name,
            publisher=publisher,
            mode=settings.mode,
            on_failure=on_failure,
        )
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("starting session %s", resolved_id)
        loop_monitor.start()
        for session in registry:
            await session.start()
        try:
            yield
        finally:
            await registry.shutdown()
            await loop_monitor.stop()
            logger.info("session %s stopped", resolved_id)

    app = FastAPI(
        title="livesubs",
        description="Realtime translated lecture subtitles.",
        lifespan=lifespan,
        # No public API docs: this runs unauthenticated on a lecture LAN, and
        # exposing an interactive schema adds nothing but surface area.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Assigned before startup so handlers and tests work in either order.
    app.state.sessions = registry
    app.state.loop_monitor = loop_monitor
    app.state.settings = settings

    app.include_router(build_router(settings))
    app.include_router(build_ws_router(settings))

    static_dir = web_dir(settings) / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    else:
        logger.warning("no static assets at %s; the pages will not load", static_dir)

    return app
