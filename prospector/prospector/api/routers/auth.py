from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...compliance import audit
from ...models import User
from ...util import utcnow
from ..deps import csrf_protect, flash, get_db, redirect, render
from ..security import verify_password, verify_totp

router = APIRouter(tags=["auth"])


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    if request.session.get("user_id"):
        return redirect(next or "/")
    return render(request, "login.html", {"next": next})


@router.post("/login", dependencies=[Depends(csrf_protect)])
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    totp: str = Form(""),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    ok = user is not None and user.is_active and verify_password(password, user.password_hash)
    if ok and user.totp_secret:
        ok = verify_totp(user.totp_secret, totp)
    if not ok:
        audit.log(db, "auth.login_failed", actor_label=email.strip().lower()[:120])
        flash(request, "Invalid credentials.", "error")
        return render(request, "login.html", {"next": next}, status_code=401)
    request.session.clear()
    request.session["user_id"] = user.id
    user.last_login_at = utcnow()
    audit.log(db, "auth.login", actor=user)
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    return redirect(next)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request):
    request.session.clear()
    return redirect("/login")
