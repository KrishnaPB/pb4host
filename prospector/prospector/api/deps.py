"""FastAPI dependencies: DB session, authentication, roles, CSRF, flash messages, templates."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import __version__
from ..config import get_settings
from ..db import session_factory
from ..models import User

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fmt_dt(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not value:
        return "—"
    try:
        return value.strftime(fmt)
    except AttributeError:
        return str(value)


def _fmt_ms(value) -> str:
    if value is None:
        return "—"
    return f"{value / 1000:.1f}s" if value >= 1000 else f"{int(value)}ms"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["ms"] = _fmt_ms
templates.env.globals["band_class"] = lambda band: {"hot": "band-hot", "warm": "band-warm"}.get(band or "", "band-cold")


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


def get_db() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def flash(request: Request, message: str, level: str = "info") -> None:
    messages = request.session.setdefault("_flash", [])
    messages.append({"message": message, "level": level})
    request.session["_flash"] = messages


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("_flash", []) if "session" in request.scope else []


def csrf_token(request: Request) -> str:
    token = request.session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["_csrf"] = token
    return token


async def csrf_protect(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if request.headers.get("x-api-key"):
        return  # token-authenticated API clients are not browser sessions
    expected = request.session.get("_csrf")
    supplied = request.headers.get("x-csrf-token")
    if supplied is None:
        content_type = request.headers.get("content-type", "")
        if "form" in content_type:
            form = await request.form()
            supplied = form.get("csrf_token")  # type: ignore[assignment]
    if not expected or not supplied or not secrets.compare_digest(str(supplied), expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")


def _user_from_api_key(request: Request, db: Session) -> User | None:
    settings = get_settings()
    key = request.headers.get("x-api-key")
    if key and settings.api_token and secrets.compare_digest(key, settings.api_token):
        return User(id=0, email="api-token", name="API token", role="analyst", password_hash="", is_active=True)
    return None


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    api_user = _user_from_api_key(request, db)
    if api_user is not None:
        return api_user
    user_id = request.session.get("user_id") if "session" in request.scope else None
    if not user_id:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        request.session.pop("user_id", None)
        return None
    return user


def current_user(request: Request, user: User | None = Depends(optional_user)) -> User:
    if user is None:
        if request.url.path.startswith("/api/"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
        raise LoginRequired(next_url=str(request.url.path))
    return user


ROLE_RANK = {"viewer": 0, "analyst": 1, "owner": 2}


def require_role(minimum: str):
    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_RANK.get(user.role, -1) < ROLE_RANK[minimum]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role {minimum}")
        return user

    return dep


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {
        "settings": get_settings(),
        "version": __version__,
        "flashes": pop_flashes(request),
        "csrf_token": csrf_token(request) if "session" in request.scope else "",
        "user": getattr(request.state, "user", None),
        "path": request.url.path,
    }
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str, status_code: int = status.HTTP_303_SEE_OTHER) -> RedirectResponse:
    return RedirectResponse(url, status_code=status_code)
