from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...compliance import audit
from ...connectors.base import REGISTRY, ensure_connector_states
from ...jobs.queue import enqueue
from ...models import ROLES, ActivityLog, ConnectorState, Job, User
from ...util import utcnow
from ..deps import csrf_protect, current_user, flash, get_db, redirect, render, require_role
from ..security import hash_password, new_totp_secret, totp_uri

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/connectors")
def connectors(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    ensure_connector_states(db)
    rows = db.scalars(select(ConnectorState).order_by(ConnectorState.name)).all()
    return render(request, "admin/connectors.html", {"rows": rows, "registry": REGISTRY, "now": utcnow()})


@router.post("/connectors/{name}", dependencies=[Depends(csrf_protect)])
def update_connector(name: str, request: Request, action: str = Form(...), daily_cap: int = Form(0),
                     db: Session = Depends(get_db), user: User = Depends(require_role("owner"))):
    row = db.scalar(select(ConnectorState).where(ConnectorState.name == name))
    if row is None:
        raise HTTPException(404)
    if action == "enable":
        row.enabled = True
    elif action == "disable":
        row.enabled = False
    elif action == "review":
        row.terms_reviewed_at = utcnow()
        row.terms_reviewed_by = user.email
    elif action == "cap" and daily_cap > 0:
        row.daily_cap = daily_cap
    else:
        raise HTTPException(400, "unknown action")
    audit.log(db, "connector.update", actor=user, entity_type="connector", entity_id=row.id, connector=name, change=action, daily_cap=daily_cap or None)
    flash(request, f"Connector {name}: {action} applied.", "success")
    return redirect("/admin/connectors")


@router.get("/jobs")
def jobs(request: Request, status: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    counts = {k: v for k, v in db.execute(select(Job.status, func.count()).group_by(Job.status)).all()}
    stmt = select(Job).order_by(Job.id.desc()).limit(100)
    if status:
        stmt = stmt.where(Job.status == status)
    rows = db.scalars(stmt).all()
    return render(request, "admin/jobs.html", {"rows": rows, "counts": counts, "status": status or ""})


@router.post("/jobs/{job_id}/retry", dependencies=[Depends(csrf_protect)])
def retry_job(job_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404)
    job.status = "queued"
    job.attempts = 0
    job.run_after = utcnow()
    job.last_error = None
    audit.log(db, "job.retry", actor=user, entity_type="job", entity_id=job.id)
    return redirect("/admin/jobs")


@router.post("/retention/run", dependencies=[Depends(csrf_protect)])
def run_retention_now(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("owner"))):
    enqueue(db, "retention_purge", {}, idempotency_key=f"retention:manual:{utcnow():%Y%m%d%H%M%S}")
    audit.log(db, "retention.enqueue", actor=user)
    flash(request, "Retention job queued.", "success")
    return redirect("/admin/jobs")


@router.get("/audit")
def audit_log(request: Request, action: str | None = None, db: Session = Depends(get_db), user: User = Depends(require_role("owner"))):
    request.state.user = user
    stmt = select(ActivityLog).order_by(ActivityLog.id.desc()).limit(300)
    if action:
        stmt = stmt.where(ActivityLog.action.like(f"{action}%"))
    rows = db.scalars(stmt).all()
    return render(request, "admin/audit.html", {"rows": rows, "action": action or ""})


@router.get("/users")
def users(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("owner"))):
    request.state.user = user
    rows = db.scalars(select(User).order_by(User.id)).all()
    return render(request, "admin/users.html", {"rows": rows, "roles": ROLES, "totp_uri": request.session.pop("_totp_uri", None)})


@router.post("/users", dependencies=[Depends(csrf_protect)])
def create_user(request: Request, email: str = Form(...), name: str = Form(""), role: str = Form("analyst"), password: str = Form(...),
                db: Session = Depends(get_db), user: User = Depends(require_role("owner"))):
    if role not in ROLES or len(password) < 12:
        flash(request, "Role invalid or password shorter than 12 characters.", "error")
        return redirect("/admin/users")
    if db.scalar(select(User).where(User.email == email.strip().lower())):
        flash(request, "A user with that e-mail exists.", "error")
        return redirect("/admin/users")
    row = User(email=email.strip().lower(), name=name.strip(), role=role, password_hash=hash_password(password))
    db.add(row)
    db.flush()
    audit.log(db, "user.create", actor=user, entity_type="user", entity_id=row.id, role=role)
    flash(request, f"User {row.email} created.", "success")
    return redirect("/admin/users")


@router.post("/users/{user_id}/totp", dependencies=[Depends(csrf_protect)])
def enable_totp(user_id: int, request: Request, action: str = Form("enable"), db: Session = Depends(get_db), user: User = Depends(require_role("owner"))):
    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(404)
    if action == "disable":
        row.totp_secret = None
        flash(request, f"2FA disabled for {row.email}.", "info")
    else:
        row.totp_secret = new_totp_secret()
        request.session["_totp_uri"] = totp_uri(row.totp_secret, row.email)
        flash(request, f"2FA enabled for {row.email}. Add the secret below to an authenticator app now; it is shown once.", "success")
    audit.log(db, "user.totp", actor=user, entity_type="user", entity_id=row.id, change=action)
    return redirect("/admin/users")


@router.post("/users/{user_id}/toggle", dependencies=[Depends(csrf_protect)])
def toggle_user(user_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("owner"))):
    row = db.get(User, user_id)
    if row is None or row.id == user.id:
        raise HTTPException(400)
    row.is_active = not row.is_active
    audit.log(db, "user.toggle", actor=user, entity_type="user", entity_id=row.id, active=row.is_active)
    return redirect("/admin/users")
