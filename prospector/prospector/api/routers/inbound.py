"""Inbound leads: signed webhook, hosted public forms, Sign In with LinkedIn, staff list."""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...compliance import audit
from ...config import get_settings
from ...connectors.base import ConnectorError
from ...connectors.inbound_webhook import InboundWebhookConnector, SignatureError, idempotency_key, verify_signature
from ...connectors.linkedin_oidc import LinkedInOidcConnector, StateError
from ...inbound.forms import FORMS, Identity, SubmissionError, parse_payload, process_submission
from ...jobs.tasks import enqueue_inbound_followups
from ...models import InboundSubmission, User
from ..deps import current_user, get_db, render

router = APIRouter(tags=["inbound"])

_FORM_TOKEN_MAX_AGE_S = 3600
_rate_lock = threading.Lock()
_rate_hits: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _rate_limited(ip: str | None, per_hour: int) -> bool:
    if not ip:
        return False
    now = time.time()
    with _rate_lock:
        hits = _rate_hits[ip]
        while hits and hits[0] < now - 3600:
            hits.popleft()
        if len(hits) >= per_hour:
            return True
        hits.append(now)
    return False


def _form_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="public-form-token")


def _form_token(form_key: str) -> str:
    return _form_serializer().dumps({"f": form_key, "n": secrets.token_urlsafe(8)})


def _check_form_token(form_key: str, token: str | None) -> bool:
    if not token:
        return False
    try:
        data = _form_serializer().loads(token, max_age=_FORM_TOKEN_MAX_AGE_S)
    except BadData:
        return False
    return data.get("f") == form_key


def _linkedin_available(db: Session) -> bool:
    settings = get_settings()
    if not settings.linkedin_enabled:
        return False
    try:
        LinkedInOidcConnector(settings).guard(db)
    except ConnectorError:
        return False
    return True


# --- signed webhook ------------------------------------------------------------------------------
@router.post("/inbound/forms/{form_key}")
async def webhook(form_key: str, request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if form_key not in FORMS:
        raise HTTPException(404, "unknown form")
    body = await request.body()
    try:
        verify_signature(settings.inbound_webhook_secret, request.headers.get("x-prospector-timestamp"),
                         request.headers.get("x-prospector-signature"), body, window_s=settings.inbound_replay_window_s)
    except SignatureError as exc:
        audit.log(db, "inbound.rejected", actor_label="webhook", form=form_key, reason=str(exc))
        raise HTTPException(401, str(exc)) from exc
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "body must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "body must be a JSON object")
    key = idempotency_key(request.headers.get("x-prospector-idempotency-key"), payload, body)
    connector = InboundWebhookConnector(settings)
    connector.reserve_calls(db, 1)
    try:
        sub = parse_payload(form_key, payload, settings=settings)
    except SubmissionError as exc:
        audit.log(db, "inbound.rejected", actor_label="webhook", form=form_key, reason=str(exc))
        return JSONResponse({"accepted": False, "error": str(exc)}, status_code=422)
    processed = process_submission(db, sub, captured_via="webhook", idempotency_key=key, ip=_client_ip(request),
                                   proof_ref=sub.source_url, settings=settings)
    if not processed.duplicate and processed.organization is not None:
        enqueue_inbound_followups(db, processed.organization)
    return JSONResponse({
        "accepted": True,
        "duplicate": processed.duplicate,
        "submission_id": processed.submission.id,
        "organization_id": processed.organization.id if processed.organization else None,
    }, status_code=200 if processed.duplicate else 201)


# --- hosted public forms -------------------------------------------------------------------------
def _render_form(request: Request, db: Session, form_key: str, *, errors: list[str] | None = None, values: dict | None = None,
                 status_code: int = 200):
    settings = get_settings()
    form = FORMS[form_key]
    identity = Identity.from_dict(request.session.get("li_identity")) if "session" in request.scope else None
    if identity is not None and request.session.get("li_identity", {}).get("form") not in {None, form_key}:
        identity = None
    return render(request, "public/form.html", {
        "form": form,
        "form_token": _form_token(form_key),
        "errors": errors or [],
        "values": values or {},
        "identity": identity,
        "linkedin_available": _linkedin_available(db),
        "consent_version": settings.inbound_consent_version,
        "privacy_url": settings.privacy_notice_url,
        "business_name": settings.business_name,
    }, status_code=status_code)


@router.get("/forms/{form_key}")
def hosted_form(form_key: str, request: Request, db: Session = Depends(get_db)):
    if form_key not in FORMS:
        raise HTTPException(404, "unknown form")
    return _render_form(request, db, form_key)


@router.post("/forms/{form_key}")
async def hosted_form_submit(form_key: str, request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if form_key not in FORMS:
        raise HTTPException(404, "unknown form")
    data = dict(await request.form())
    values = {k: str(v) for k, v in data.items() if k not in {"form_token", "website_confirm"}}
    if data.get("website_confirm"):  # honeypot: bots fill every field
        return render(request, "public/thanks.html", {"form": FORMS[form_key], "business_name": settings.business_name})
    if not _check_form_token(form_key, str(data.get("form_token") or "")):
        return _render_form(request, db, form_key, errors=["The form expired — please submit it again."], values=values, status_code=400)
    ip = _client_ip(request)
    if _rate_limited(ip, settings.inbound_rate_limit_per_hour):
        return _render_form(request, db, form_key, errors=["Too many submissions from this network; please try again later."],
                            values=values, status_code=429)
    identity = Identity.from_dict(request.session.get("li_identity")) if "session" in request.scope else None
    if identity is not None:
        # the verified identity wins over anything typed into the locked fields
        data["name"] = identity.name or data.get("name", "")
        if identity.email:
            data["email"] = identity.email
    try:
        sub = parse_payload(form_key, data, identity=identity, settings=settings)
    except SubmissionError as exc:
        return _render_form(request, db, form_key, errors=[str(exc)], values=values, status_code=422)
    InboundWebhookConnector(settings).reserve_calls(db, 1)
    key = f"hosted:{form_key}:{secrets.token_urlsafe(16)}"
    sub.source_url = sub.source_url or str(request.url)
    processed = process_submission(db, sub, captured_via="linkedin_oidc" if identity else "hosted_form", idempotency_key=key,
                                   ip=ip, proof_ref=str(request.url), settings=settings)
    if processed.organization is not None:
        enqueue_inbound_followups(db, processed.organization)
    if "session" in request.scope:
        request.session.pop("li_identity", None)
    return render(request, "public/thanks.html", {"form": FORMS[form_key], "business_name": settings.business_name,
                                                  "verified": identity is not None})


# --- Sign In with LinkedIn -----------------------------------------------------------------------
def _redirect_uri() -> str:
    return get_settings().base_url.rstrip("/") + "/auth/linkedin/callback"


@router.get("/forms/{form_key}/linkedin/start")
def linkedin_start(form_key: str, request: Request, db: Session = Depends(get_db)):
    if form_key not in FORMS:
        raise HTTPException(404, "unknown form")
    connector = LinkedInOidcConnector(get_settings())
    connector.guard(db)  # kill switch + terms review apply before any redirect
    state = connector.make_state(form_key, return_to=f"/forms/{form_key}")
    return RedirectResponse(connector.authorization_url(redirect_uri=_redirect_uri(), state=state), status_code=302)


@router.get("/auth/linkedin/callback")
def linkedin_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
                      error_description: str | None = None, db: Session = Depends(get_db)):
    connector = LinkedInOidcConnector(get_settings())
    try:
        parsed = connector.read_state(state)
    except StateError as exc:
        raise HTTPException(400, str(exc)) from exc
    form_key = parsed.form_key if parsed.form_key in FORMS else "speed_evaluation"
    if error or not code:
        return _render_form(request, db, form_key, errors=[f"LinkedIn sign-in was cancelled ({error_description or error or 'no code'}). "
                                                            "You can still fill the form by hand."], status_code=400)
    token = connector.exchange_code(db, code, redirect_uri=_redirect_uri())
    identity = connector.userinfo(db, token)
    del token  # never stored, never logged
    request.session["li_identity"] = {**identity.as_dict(), "form": form_key, "at": int(time.time())}
    audit.log(db, "inbound.linkedin_signin", actor_label="public", form=form_key, email_verified=identity.email_verified)
    return RedirectResponse(parsed.return_to or f"/forms/{form_key}", status_code=303)


# --- staff view ------------------------------------------------------------------------------------
@router.get("/inbound")
def list_submissions(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    rows = db.scalars(select(InboundSubmission).order_by(InboundSubmission.id.desc()).limit(100)).all()
    settings = get_settings()
    return render(request, "inbound/list.html", {
        "rows": rows,
        "forms": FORMS,
        "webhook_configured": bool(settings.inbound_webhook_secret),
        "linkedin_configured": settings.linkedin_enabled,
        "base_url": settings.base_url.rstrip("/"),
    })
