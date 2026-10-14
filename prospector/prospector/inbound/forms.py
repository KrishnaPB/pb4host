"""Form definitions, payload validation and the shared processing pipeline for inbound leads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..compliance import audit
from ..compliance.suppression import is_suppressed, organization_is_suppressed
from ..config import Settings, get_settings
from ..core.facts import Fact, apply_facts
from ..core.normalize import is_freemail_domain, normalize_email, normalize_phone, normalize_url
from ..core.organizations import ensure_website, resolve_organization
from ..models import ConsentRecord, InboundSubmission, Organization, Person
from ..util import utcnow


class SubmissionError(ValueError):
    """The payload is not acceptable (missing consent, bad e-mail, unknown form …)."""


@dataclass(frozen=True)
class FormField:
    name: str
    label: str
    kind: str = "text"  # text | select | textarea
    options: tuple[str, ...] = ()
    required: bool = False


@dataclass(frozen=True)
class FormDefinition:
    key: str
    title: str
    intro: str
    purpose: str  # consent purpose stored on the consent record
    track: str
    consent_text: str
    extra_fields: tuple[FormField, ...] = ()


FORMS: dict[str, FormDefinition] = {
    "speed_evaluation": FormDefinition(
        key="speed_evaluation",
        title="Request a free WordPress speed evaluation",
        intro="Tell us where your site lives and we will send you a short, human-written evaluation of its "
        "mobile performance, hosting and WordPress health — no automated spam, no obligation.",
        purpose="speed_evaluation_contact",
        track="smb",
        consent_text=(
            "I agree that PB4HOST may store the details I entered and contact me by e-mail (or the phone number "
            "I gave) about this evaluation. I can withdraw this at any time by replying to any message or writing "
            "to the privacy contact in the notice."
        ),
        extra_fields=(
            FormField("current_host", "Where is the site hosted today? (optional)"),
            FormField("main_problem", "What bothers you most about the site? (optional)", kind="select",
                      options=("", "slow pages", "errors or downtime", "security worries", "updates and maintenance", "not sure")),
        ),
    ),
    "agency_partner": FormDefinition(
        key="agency_partner",
        title="Agency partner enquiry",
        intro="For web design and development agencies that want a hosting and maintenance partner for their "
        "WordPress clients.",
        purpose="agency_partner_contact",
        track="agency",
        consent_text=(
            "I agree that PB4HOST may store the details I entered and contact me about a partnership. "
            "I can withdraw this at any time."
        ),
        extra_fields=(
            FormField("team_size", "Team size", kind="select", options=("", "1", "2-5", "6-20", "21+")),
            FormField("client_sites", "Roughly how many client WordPress sites do you look after?"),
            FormField("services", "Main services (optional)"),
        ),
    ),
}


@dataclass
class Identity:
    """Verified identity from an OpenID Connect sign-in (LinkedIn). Only these fields exist."""

    provider: str
    sub: str
    name: str | None = None
    email: str | None = None
    email_verified: bool = False
    picture: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "sub": self.sub, "name": self.name, "email": self.email,
                "email_verified": self.email_verified, "picture": self.picture}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Identity | None:
        if not data or not data.get("sub"):
            return None
        return cls(provider=str(data.get("provider") or "linkedin"), sub=str(data["sub"]), name=data.get("name"),
                   email=data.get("email"), email_verified=bool(data.get("email_verified")), picture=data.get("picture"))


@dataclass
class Submission:
    form_key: str
    name: str
    email: str
    company: str
    website: str | None = None
    phone: str | None = None
    message: str | None = None
    city: str | None = None
    country: str | None = None
    consent: bool = False
    consent_version: str | None = None
    source_url: str | None = None
    submitted_at: datetime | None = None
    identity: Identity | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def form(self) -> FormDefinition:
        return FORMS[self.form_key]

    def canonical(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k not in {"identity", "submitted_at"}}
        data["submitted_at"] = self.submitted_at.isoformat() if self.submitted_at else None
        data["identity"] = self.identity.as_dict() if self.identity else None
        return data


def _text(data: Mapping[str, Any], key: str, limit: int = 255) -> str:
    value = data.get(key)
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "granted"}


def parse_payload(form_key: str, data: Mapping[str, Any], *, identity: Identity | None = None,
                  settings: Settings | None = None) -> Submission:
    """Validate a raw payload (JSON webhook body or HTML form) into a `Submission`."""
    settings = settings or get_settings()
    form = FORMS.get(form_key)
    if form is None:
        raise SubmissionError(f"unknown form {form_key!r}")
    consent_block = data.get("consent")
    if isinstance(consent_block, Mapping):
        consent = _truthy(consent_block.get("granted", consent_block.get("given", False)))
        consent_version = _text(consent_block, "text_version", 40) or _text(consent_block, "version", 40)
    else:
        consent = _truthy(consent_block)
        consent_version = _text(data, "consent_version", 40)
    if not consent:
        raise SubmissionError("consent is required: the prospect must tick the consent statement")
    name = _text(data, "name", 200) or (identity.name if identity else "")
    email_raw = _text(data, "email") or (identity.email if identity else "")
    email = normalize_email(email_raw) if email_raw else None
    if not name:
        raise SubmissionError("name is required")
    if not email:
        raise SubmissionError("a valid e-mail address is required")
    company = _text(data, "company", 200) or _text(data, "organisation", 200) or _text(data, "organization", 200)
    website = _text(data, "website", 500) or None
    if website and not website.lower().startswith(("http://", "https://")):
        website = "https://" + website
    if website and normalize_url(website) is None:
        raise SubmissionError("website is not a valid URL")
    if not company and not website:
        raise SubmissionError("company name or website is required")
    phone = _text(data, "phone", 40) or None
    submitted_at = None
    raw_ts = data.get("submitted_at")
    if raw_ts:
        try:
            submitted_at = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError as exc:
            raise SubmissionError("submitted_at must be ISO-8601") from exc
    extra = {f.name: _text(data, f.name, 500) for f in form.extra_fields if _text(data, f.name, 500)}
    return Submission(
        form_key=form_key,
        name=name,
        email=email,
        company=company or "",
        website=website,
        phone=phone,
        message=_text(data, "message", 2000) or None,
        city=_text(data, "city", 120) or None,
        country=(_text(data, "country", 2).upper() or None),
        consent=True,
        consent_version=consent_version or settings.inbound_consent_version,
        source_url=_text(data, "source_url", 500) or None,
        submitted_at=submitted_at,
        identity=identity,
        extra=extra,
    )


def payload_hash(sub: Submission) -> str:
    return hashlib.sha256(json.dumps(sub.canonical(), sort_keys=True, default=str).encode()).hexdigest()


def hash_ip(ip: str | None, settings: Settings) -> str | None:
    if not ip:
        return None
    return hashlib.sha256(f"{settings.salt}:ip:{ip}".encode()).hexdigest()


@dataclass
class Processed:
    submission: InboundSubmission
    organization: Organization | None
    person: Person | None
    created_organization: bool = False
    duplicate: bool = False
    previously_suppressed: bool = False


def process_submission(
    session: Session,
    sub: Submission,
    *,
    captured_via: str,
    idempotency_key: str,
    ip: str | None = None,
    proof_ref: str | None = None,
    settings: Settings | None = None,
) -> Processed:
    """Create/refresh organisation + person + consent record for one consented submission.

    Idempotent on `idempotency_key`; the caller enqueues enrichment and CRM jobs (kept out of
    here so the pipeline stays testable without the queue)."""
    settings = settings or get_settings()
    existing = session.scalar(select(InboundSubmission).where(InboundSubmission.idempotency_key == idempotency_key))
    if existing is not None:
        org = session.get(Organization, existing.organization_id) if existing.organization_id else None
        person = session.get(Person, existing.person_id) if existing.person_id else None
        return Processed(existing, org, person, duplicate=True)

    now = utcnow()
    form = sub.form
    ref = f"inbound://{form.key}/{idempotency_key[:24]}"
    record = InboundSubmission(
        form_key=form.key, idempotency_key=idempotency_key, captured_via=captured_via,
        payload_hash=payload_hash(sub), ip_hash=hash_ip(ip, settings), message=sub.message, received_at=now,
    )
    session.add(record)

    # --- organisation ------------------------------------------------------------------------
    website = sub.website
    email_domain = sub.email.rsplit("@", 1)[-1].lower()
    if not website and not is_freemail_domain(sub.email):
        website = f"https://{email_domain}/"  # a business mailbox implies the business's own site
    resolved = resolve_organization(session, website=website, name=sub.company or None, city=sub.city,
                                    country=sub.country or settings.default_country, track=form.track, lead_source="inbound")
    org = resolved.organization
    previously_suppressed = organization_is_suppressed(session, org) or is_suppressed(session, "email", sub.email)
    site = ensure_website(session, org, website) if website and not resolved.profile_url else None
    facts: list[Fact] = []
    if sub.company:
        facts.append(Fact("organization.display_name", sub.company, "inbound_form", ref, 0.9, now))
    if site is not None:
        facts.append(Fact("organization.website_url", site.url, "inbound_form", ref, 0.95, now))
    if resolved.profile_url:
        facts.append(Fact("organization.social", f"{resolved.profile_network}={resolved.profile_url}", "inbound_form", ref, 0.9, now))
    if sub.city:
        facts.append(Fact("organization.city", sub.city, "inbound_form", ref, 0.8, now))
    if sub.country:
        facts.append(Fact("organization.country", sub.country, "inbound_form", ref, 0.8, now))
    if sub.phone and normalize_phone(sub.phone, sub.country or settings.default_phone_region):
        facts.append(Fact("organization.phone", sub.phone, "inbound_form", ref, 0.9, now))
    if form.track in {"smb", "agency"} and org.track == "unknown":
        facts.append(Fact("organization.track", form.track, "inbound_form", ref, 0.8, now))
    apply_facts(session, org, facts, site)
    org.lead_source = org.lead_source or "inbound"
    org.inbound_at = org.inbound_at or now
    org.last_engaged_at = now
    if org.status in {"new", "disqualified"}:
        org.status = "reviewing"
    stamp = now.strftime("%Y-%m-%d %H:%M")
    note_bits = [f"[{stamp} inbound:{form.key}] {sub.name} <{sub.email}>"]
    if sub.extra:
        note_bits.append("; ".join(f"{k}={v}" for k, v in sub.extra.items()))
    if sub.message:
        note_bits.append(f"message: {sub.message}")
    if previously_suppressed:
        note_bits.append("NOTE: an identifier of this lead is on the suppression list; they re-consented via the form — "
                         "review before contacting and clear the suppression deliberately.")
    org.notes = "\n".join(filter(None, [org.notes or "", " ".join(note_bits)])).strip()

    # --- person + consent (exactly one per submission) -----------------------------------------
    person = None
    if sub.identity and sub.identity.sub:
        person = session.scalar(select(Person).where(Person.external_id == sub.identity.sub, Person.organization_id == org.id))
    if person is None:
        person = session.scalar(select(Person).where(Person.email == sub.email, Person.organization_id == org.id))
    source = "linkedin_oidc" if sub.identity else "inbound_form"
    if person is None:
        person = Person(organization=org, full_name=sub.name[:200], email=sub.email, source=source,
                        lawful_basis="consent", notice_status="sent", notice_sent_at=now)
        session.add(person)
    else:
        person.full_name = sub.name[:200] or person.full_name
        person.lawful_basis = "consent"
        person.notice_status = "sent"
        person.notice_sent_at = person.notice_sent_at or now
        person.do_not_contact = False  # a fresh, explicit consent supersedes an older flag
    if sub.phone:
        person.phone_e164 = normalize_phone(sub.phone, sub.country or settings.default_phone_region) or person.phone_e164
    if sub.identity:
        person.external_id = sub.identity.sub
        person.picture_url = sub.identity.picture
        person.source = "linkedin_oidc"
    meta = dict(person.meta or {})
    meta.update({"form": form.key, "email_verified": bool(sub.identity and sub.identity.email_verified), **sub.extra})
    person.meta = meta
    session.flush()
    consent = ConsentRecord(
        person_id=person.id, purpose=form.purpose, text_version=sub.consent_version or settings.inbound_consent_version,
        captured_via=captured_via, captured_at=sub.submitted_at or now, ip_hash=record.ip_hash,
        proof_ref=proof_ref or sub.source_url or ref,
    )
    session.add(consent)
    session.flush()
    person.consent_id = consent.id
    record.organization_id = org.id
    record.person_id = person.id
    record.consent_id = consent.id
    record.status = "processed"
    session.flush()
    audit.log(session, "inbound.submission", actor_label=f"form:{form.key}", entity_type="organization", entity_id=org.id,
              person_id=person.id, captured_via=captured_via, created=resolved.created, previously_suppressed=previously_suppressed)
    return Processed(record, org, person, created_organization=resolved.created, previously_suppressed=previously_suppressed)
