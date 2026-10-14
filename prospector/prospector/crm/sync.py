"""Push/pull logic on top of an adapter, with `CrmLink` rows for idempotent upserts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..compliance import audit
from ..compliance.suppression import organization_is_suppressed
from ..config import Settings, get_settings
from ..connectors.base import Connector, ConnectorError, register
from ..models import CrmLink, Organization
from ..util import utcnow
from .base import CrmAdapter, account_payload, contact_payload, payload_hash, person_is_notice_ready

PUSH_STATUSES = {"qualified", "exported", "contacted", "customer"}
FUNNEL_ORDER = ["new", "reviewing", "qualified", "exported", "contacted", "customer"]


@register
class CrmSyncConnector(Connector):
    """Registered so the kill switch and daily cap apply to CRM traffic; no external terms clock."""

    name = "crm_sync"
    terms_url = ""
    requires_terms_review = False
    default_daily_cap = 2000


def build_adapter(settings: Settings | None = None, client=None) -> CrmAdapter:
    settings = settings or get_settings()
    if settings.crm_target != "espocrm":
        raise ConnectorError(f"unsupported CRM target {settings.crm_target!r}")
    if not settings.crm_configured:
        raise ConnectorError("CRM is not configured (ESPOCRM_BASE_URL / ESPOCRM_API_KEY)")
    from .espocrm import EspoCrmAdapter

    return EspoCrmAdapter(settings.espocrm_base_url or "", settings.espocrm_api_key or "", client=client,
                          status_field=settings.crm_status_field)


def eligibility(session: Session, org: Organization) -> tuple[bool, str]:
    if org.status in {"suppressed", "anonymised", "merged", "disqualified"}:
        return False, f"status {org.status}"
    if organization_is_suppressed(session, org):
        return False, "an identifier is on the suppression list"
    if org.lead_source == "inbound" or org.status in PUSH_STATUSES:
        return True, "ok"
    return False, f"status {org.status} is not qualified (inbound leads and qualified+ organisations sync)"


@dataclass
class PushOutcome:
    pushed: bool
    reason: str = ""
    account: CrmLink | None = None
    contacts: list[CrmLink] = field(default_factory=list)
    skipped_contacts: int = 0


def _link(session: Session, target: str, entity_type: str, entity_id: int) -> CrmLink | None:
    return session.scalar(select(CrmLink).where(CrmLink.target == target, CrmLink.entity_type == entity_type, CrmLink.entity_id == entity_id))


def push_organization(session: Session, org: Organization, adapter: CrmAdapter, *, notice_text: str, settings: Settings | None = None,
                      force: bool = False, actor=None) -> PushOutcome:
    settings = settings or get_settings()
    ok, reason = eligibility(session, org)
    if not ok:
        return PushOutcome(False, reason)
    connector = CrmSyncConnector(settings)
    payload = account_payload(org, settings, notice_text)
    digest = payload_hash(payload)
    link = _link(session, adapter.target, "organization", org.id)
    if link is None:
        connector.reserve_calls(session, 1)
        remote = adapter.find_account(domain=org.domain, name=org.display_name)
        link = CrmLink(target=adapter.target, entity_type="organization", entity_id=org.id, organization_id=org.id,
                       remote_id=remote.remote_id if remote else "", remote_url=remote.url if remote else None)
        session.add(link)
        session.flush()
    if link.push_hash == digest and not force:
        outcome = PushOutcome(False, "unchanged since last push", account=link)
    else:
        connector.reserve_calls(session, 1)
        try:
            remote = adapter.upsert_account(payload, link.remote_id or None)
        except ConnectorError as exc:
            link.last_error = str(exc)[:1000]
            connector.record_error(session, str(exc))
            raise
        link.remote_id, link.remote_url, link.remote_status = remote.remote_id, remote.url, remote.status
        link.push_hash, link.last_pushed_at, link.last_error = digest, utcnow(), None
        outcome = PushOutcome(True, "pushed", account=link)
    # contacts: notice-ready only
    if settings.crm_push_persons and link.remote_id:
        for person in org.persons:
            if not person_is_notice_ready(person):
                outcome.skipped_contacts += 1
                continue
            cpayload = contact_payload(person, settings)
            cdigest = payload_hash(cpayload)
            clink = _link(session, adapter.target, "person", person.id)
            if clink is not None and clink.push_hash == cdigest and not force:
                continue
            connector.reserve_calls(session, 1)
            remote_c = adapter.upsert_contact(cpayload, link.remote_id, clink.remote_id if clink else None)
            if clink is None:
                clink = CrmLink(target=adapter.target, entity_type="person", entity_id=person.id, organization_id=org.id,
                                remote_id=remote_c.remote_id)
                session.add(clink)
            clink.remote_id, clink.remote_url, clink.push_hash, clink.last_pushed_at = remote_c.remote_id, remote_c.url, cdigest, utcnow()
            outcome.contacts.append(clink)
    if outcome.pushed or outcome.contacts:
        if org.status == "qualified":
            org.status = "exported"
        org.last_engaged_at = utcnow()
        audit.log(session, "crm.push", actor=actor, entity_type="organization", entity_id=org.id, target=adapter.target,
                  remote_id=link.remote_id, contacts=len(outcome.contacts), skipped_contacts=outcome.skipped_contacts)
    session.flush()
    return outcome


def status_map(settings: Settings) -> dict[str, str]:
    try:
        raw = json.loads(settings.crm_status_map or "{}")
    except json.JSONDecodeError:
        return {}
    return {str(k).lower(): str(v) for k, v in raw.items()}


def pull_status(session: Session, org: Organization, adapter: CrmAdapter, *, settings: Settings | None = None) -> str | None:
    """Read the CRM's status field and advance the organisation when the CRM is further along."""
    settings = settings or get_settings()
    link = _link(session, adapter.target, "organization", org.id)
    if link is None or not link.remote_id:
        return None
    CrmSyncConnector(settings).reserve_calls(session, 1)
    remote = adapter.get_account(link.remote_id)
    link.last_pulled_at = utcnow()
    if remote is None:
        link.last_error = "record no longer exists in the CRM"
        return None
    link.remote_status = remote.status
    mapped = status_map(settings).get((remote.status or "").lower())
    if not mapped:
        return None
    if mapped == "disqualified":
        if org.status not in {"disqualified", "customer"}:
            before, org.status, org.disqualify_reason = org.status, "disqualified", f"crm:{remote.status}"
            audit.log(session, "crm.pull_status", entity_type="organization", entity_id=org.id, before=before, after=org.status)
        return mapped
    if mapped in FUNNEL_ORDER and org.status in FUNNEL_ORDER and FUNNEL_ORDER.index(mapped) > FUNNEL_ORDER.index(org.status):
        before, org.status = org.status, mapped
        org.last_engaged_at = utcnow()
        audit.log(session, "crm.pull_status", entity_type="organization", entity_id=org.id, before=before, after=org.status)
    return mapped
