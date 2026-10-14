"""Rights-request fulfilment: erasure, objection and consent withdrawal in one transaction (plan §7, Phase 2).

Acceptance (plan §11): an erasure request removes the person and anonymises organisation history in one workflow.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ConsentRecord, CrmLink, InboundSubmission, Organization, Person, RightsRequest, User
from ..util import utcnow
from . import audit
from .suppression import add_suppression, anonymise_organization, erase_person, find_subjects, suppress_organization


@dataclass
class FulfilmentSummary:
    persons_erased: int = 0
    persons_objected: int = 0
    organizations_anonymised: int = 0
    organizations_suppressed: int = 0
    consents_withdrawn: int = 0
    submissions_scrubbed: int = 0
    crm_deletes_queued: int = 0
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        parts = []
        if self.persons_erased:
            parts.append(f"{self.persons_erased} contact(s) erased")
        if self.persons_objected:
            parts.append(f"{self.persons_objected} contact(s) marked do-not-contact")
        if self.organizations_anonymised:
            parts.append(f"{self.organizations_anonymised} organisation(s) anonymised")
        if self.organizations_suppressed:
            parts.append(f"{self.organizations_suppressed} organisation(s) suppressed")
        if self.consents_withdrawn:
            parts.append(f"{self.consents_withdrawn} consent(s) withdrawn")
        if self.submissions_scrubbed:
            parts.append(f"{self.submissions_scrubbed} inbound submission(s) scrubbed")
        if self.crm_deletes_queued:
            parts.append(f"{self.crm_deletes_queued} CRM deletion(s) queued")
        return "; ".join(parts) or "nothing matched"


def _subjects(session: Session, req: RightsRequest) -> tuple[list[Organization], list[Person]]:
    matches = find_subjects(session, req.subject)
    orgs = {o.id: o for o in matches.organizations}
    persons = {p.id: p for p in matches.persons}
    if req.organization_id and (org := session.get(Organization, req.organization_id)) is not None:
        orgs.setdefault(org.id, org)
    if req.person_id and (person := session.get(Person, req.person_id)) is not None:
        persons.setdefault(person.id, person)
    return list(orgs.values()), list(persons.values())


def _queue_crm_delete(session: Session, entity_type: str, entity_id: int, summary: FulfilmentSummary) -> None:
    from ..jobs.queue import enqueue

    links = session.scalars(select(CrmLink).where(CrmLink.entity_type == entity_type, CrmLink.entity_id == entity_id)).all()
    for link in links:
        enqueue(session, "crm_delete", {"target": link.target, "remote_entity": "Account" if entity_type == "organization" else "Contact",
                                        "remote_id": link.remote_id}, idempotency_key=f"crm_delete:{link.target}:{link.remote_id}")
        session.delete(link)
        summary.crm_deletes_queued += 1


def _scrub_submissions(session: Session, *, org_ids: set[int], person_ids: set[int], summary: FulfilmentSummary) -> None:
    rows = session.scalars(select(InboundSubmission).where(
        InboundSubmission.organization_id.in_(org_ids or {-1}) | InboundSubmission.person_id.in_(person_ids or {-1}))).all()
    for sub in rows:
        sub.message = None
        sub.error = None
        sub.ip_hash = None
        sub.status = "erased"
        summary.submissions_scrubbed += 1


def _withdraw_consents(session: Session, person_ids: set[int], summary: FulfilmentSummary) -> None:
    if not person_ids:
        return
    for consent in session.scalars(select(ConsentRecord).where(ConsentRecord.person_id.in_(person_ids), ConsentRecord.withdrawn_at.is_(None))):
        consent.withdrawn_at = utcnow()
        consent.ip_hash = None
        summary.consents_withdrawn += 1


def _close(session: Session, req: RightsRequest, actor: User | None, summary: FulfilmentSummary, action: str) -> None:
    stamp = utcnow().strftime("%Y-%m-%d %H:%M")
    who = actor.email if actor else "system"
    req.resolution = f"{req.resolution or ''}\n[{stamp} {who}] {action}: {summary.text()}".strip()
    req.status = "completed"
    req.completed_at = utcnow()
    req.completed_by = actor.id if actor else None
    # keep only a hash of the identifier on the closed request; the suppression list keeps the salted hash for matching
    req.subject = "sha256:" + hashlib.sha256(req.subject.strip().lower().encode()).hexdigest()[:24]
    audit.log(session, f"rights.{action}", actor=actor, entity_type="rights_request", entity_id=req.id, **{
        k: v for k, v in summary.__dict__.items() if k != "notes"})


def fulfil_erasure(session: Session, req: RightsRequest, *, actor: User | None = None, include_person_orgs: bool = False) -> FulfilmentSummary:
    """Erase every matching person, anonymise every matching organisation, suppress identifiers, close the request.

    ``include_person_orgs`` also anonymises the organisations the matched people belong to (sole traders, freelancers,
    "the business is the person"); otherwise business facts from the company's own website stay and only the person,
    their submissions, consents and CRM contact go.
    """
    summary = FulfilmentSummary()
    reason = f"rights request #{req.id} ({req.kind})"
    orgs, persons = _subjects(session, req)
    if include_person_orgs:
        seen = {o.id for o in orgs}
        for person in persons:
            if person.organization is not None and person.organization.id not in seen:
                orgs.append(person.organization)
                seen.add(person.organization.id)
    org_ids = {o.id for o in orgs}
    person_ids = {p.id for p in persons}
    # persons that belong to the matched organisations go too (anonymise_organization deletes them)
    for org in orgs:
        person_ids.update(p.id for p in org.persons)
    _withdraw_consents(session, person_ids, summary)
    _scrub_submissions(session, org_ids=org_ids, person_ids=person_ids, summary=summary)
    for person in persons:
        if person.organization_id in org_ids:
            continue  # handled with the organisation below
        if person.linkedin_url:
            summary.notes.append(f"linkedin:{person.linkedin_url}")
        org = person.organization
        if org is not None and org.notes:
            for needle in filter(None, (person.full_name, person.email, person.phone_e164)):
                org.notes = org.notes.replace(needle, "[erased]")
        _queue_crm_delete(session, "person", person.id, summary)
        erase_person(session, person, reason=reason, actor=actor)
        summary.persons_erased += 1
    for org in orgs:
        for person in list(org.persons):
            _queue_crm_delete(session, "person", person.id, summary)
            if person.email:
                add_suppression(session, "email", person.email, reason=reason, actor=actor)
            if person.phone_e164:
                add_suppression(session, "phone", person.phone_e164, reason=reason, actor=actor)
            audit.log(session, "person.erase", actor=actor, entity_type="person", entity_id=person.id, organization_id=org.id, reason=reason)
            summary.persons_erased += 1
        _queue_crm_delete(session, "organization", org.id, summary)
        anonymise_organization(session, org, reason=reason, actor=actor)
        org.tracked = False
        org.next_recheck_at = None
        summary.organizations_anonymised += 1
    # the identifier itself is suppressed even when nothing matched, so it can never be re-added
    kind = "email" if "@" in req.subject else ("phone" if any(ch.isdigit() for ch in req.subject) else "domain")
    add_suppression(session, kind, req.subject.strip(), reason=reason, actor=actor)
    session.flush()
    _close(session, req, actor, summary, "erasure")
    return summary


def fulfil_objection(session: Session, req: RightsRequest, *, actor: User | None = None) -> FulfilmentSummary:
    """Stop all contact: suppress identifiers, mark contacts objected/do-not-contact, keep records for the audit trail."""
    summary = FulfilmentSummary()
    reason = f"rights request #{req.id} ({req.kind})"
    orgs, persons = _subjects(session, req)
    for person in persons:
        person.do_not_contact = True
        person.notice_status = "objected"
        if person.email:
            add_suppression(session, "email", person.email, reason=reason, actor=actor)
        if person.phone_e164:
            add_suppression(session, "phone", person.phone_e164, reason=reason, actor=actor)
        summary.persons_objected += 1
    if req.kind == "withdraw_consent":
        _withdraw_consents(session, {p.id for p in persons}, summary)
        for person in list(persons):
            if person.lawful_basis == "consent":  # nothing else justifies keeping the record
                _queue_crm_delete(session, "person", person.id, summary)
                erase_person(session, person, reason=reason, actor=actor)
                summary.persons_erased += 1
                summary.persons_objected -= 1
    for org in orgs:
        if org.status not in {"suppressed", "anonymised", "merged"}:
            suppress_organization(session, org, reason=reason, actor=actor)
            org.tracked = False
            org.next_recheck_at = None
            summary.organizations_suppressed += 1
        for person in org.persons:
            if not person.do_not_contact:
                person.do_not_contact = True
                person.notice_status = "objected"
                summary.persons_objected += 1
    kind = "email" if "@" in req.subject else ("phone" if any(ch.isdigit() for ch in req.subject) else "domain")
    add_suppression(session, kind, req.subject.strip(), reason=reason, actor=actor)
    session.flush()
    _close(session, req, actor, summary, "objection")
    return summary
