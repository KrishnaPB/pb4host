"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__
from ..config import Settings, get_settings
from ..connectors.base import ConnectorError
from ..db import init_engine
from ..logging_setup import configure_logging
from .deps import LoginRequired, flash, render
from .routers import (
    admin,
    api_v1,
    auth,
    capture,
    dashboard,
    exports,
    health,
    imports,
    organizations,
    runs,
    suppressions,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    init_engine(settings)
    app = FastAPI(title="PB4HOST Prospector", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="prospector_session",
        same_site="lax",
        https_only=settings.session_https_only,
        max_age=12 * 3600,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse(f"/login?next={exc.next_url}", status_code=303)

    @app.exception_handler(ConnectorError)
    async def _connector_error(request: Request, exc: ConnectorError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc)}, status_code=409)
        flash(request, str(exc), "error")
        return render(request, "error.html", {"message": str(exc)}, status_code=409)

    for router in (auth, dashboard, runs, organizations, imports, capture, suppressions, exports, admin, health, api_v1):
        app.include_router(router.router)
    return app
