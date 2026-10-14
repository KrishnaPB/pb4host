"""CSV imports: existing customers, manual lists and the owner's own LinkedIn connections export.

Persons are created only when the batch declares a lawful basis (R2); otherwise rows import as
organisations only. Every imported value gets evidence pointing at the batch and row.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..core.facts import Fact, apply_facts
from ..core.normalize import normalize_email, normalize_phone, normalize_url, registered_domain
from ..core.organizations import ensure_website, resolve_organization
from ..models import ConsentRecord, ImportBatch, Person, User
from ..util import utcnow
from .base import Connector, register

PRESETS: dict[str, dict[str, str]] = {
    "generic": {
        "company": "company", "website": "website", "email": "email", "phone": "phone",
        "city": "city", "country": "country", "first_name": "first_name", "last_name": "last_name",
        "role": "role", "person_email": "person_email", "profile_url": "linkedin_url",
    },
    "woocommerce_customers": {
        "company": "Company", "website": "Website", "email": "Email", "phone": "Phone",
        "city": "City", "country": "Country", "first_name": "First name", "last_name": "Last name",
    },
    "linkedin_connections": {
        "first_name": "First Name", "last_name": "Last Name", "profile_url": "URL",
        "person_email": "Email Address", "company": "Company", "role": "Position",
    },
}
FIELDS = tuple(PRESETS["generic"].keys())


@dataclass
class ImportSummary:
    rows: int = 0
    ok: int = 0
    errors: list[dict] = field(default_factory=list)
    organizations_created: int = 0
    organizations_matched: int = 0
    persons_created: int = 0
    websites_to_probe: list[int] = field(default_factory=list)  # organization ids


def read_csv(content: bytes | str) -> list[dict[str, str]]:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


@register
class CsvImportConnector(Connector):
    name = "csv_import"
    terms_url = ""
    requires_terms_review = False
    default_daily_cap = 100_000
    cost_per_call_usd = 0.0
    creates_persons = True
    person_route = "import"  # the only connector allowed to, and only with a declared lawful basis

    def import_rows(
        self,
        session: Session,
        batch: ImportBatch,
        rows: Iterable[dict[str, str]],
        mapping: dict[str, str],
        *,
        lawful_basis: str | None,
        notice_status: str = "pending",
        actor: User | None = None,
        default_country: str | None = None,
        on_website: Callable[[int, str], None] | None = None,
    ) -> ImportSummary:
        summary = ImportSummary()
        source = f"import:{batch.kind}"
        country = (default_country or self.settings.default_country or "").upper() or None
        for index, row in enumerate(rows, start=2):  # header is line 1
            summary.rows += 1
            def get(key: str, _row=row) -> str:
                return (_row.get(mapping.get(key, ""), "") or "").strip()

            company = get("company")
            website = normalize_url(get("website")) if get("website") else None
            org_email = normalize_email(get("email")) if get("email") else None
            person_email = normalize_email(get("person_email")) if get("person_email") else None
            first, last = get("first_name"), get("last_name")
            try:
              with session.begin_nested():
                  if not website and org_email and org_email.split("@")[1] not in {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "rediffmail.com"}:
                      website = "https://" + org_email.split("@")[1]
                  if not company and not website:
                      raise ValueError("row has neither company nor website")
                  phone = normalize_phone(get("phone"), get("country") or country or "IN") if get("phone") else None
                  resolved = resolve_organization(
                      session,
                      website=website,
                      name=company or None,
                      city=get("city") or None,
                      country=(get("country") or country),
                      phones=[phone] if phone else None,
                      track=batch.track,
                      lead_source="import",
                  )
                  org = resolved.organization
                  if resolved.created:
                      summary.organizations_created += 1
                  else:
                      summary.organizations_matched += 1
                  ref = f"import://batch/{batch.id}/row/{index}"
                  facts: list[Fact] = []
                  if resolved.profile_url:
                      website = None
                      facts.append(Fact("organization.social", f"{resolved.profile_network}={resolved.profile_url}", source, ref, 0.9))
                  if company and not org.display_name:
                      facts.append(Fact("organization.display_name", company, source, ref, 0.8))
                  if website:
                      facts.append(Fact("organization.website_url", website, source, ref, 0.9))
                  if org_email and org_email.split("@")[1] == (registered_domain(website) if website else None):
                      facts.append(Fact("organization.email", org_email, source, ref, 0.9))
                  elif org_email and not (first or last):
                      facts.append(Fact("organization.email", org_email, source, ref, 0.7))
                  if phone:
                      facts.append(Fact("organization.phone", phone, source, ref, 0.8))
                  if get("city") and not org.city:
                      facts.append(Fact("organization.city", get("city"), source, ref, 0.8))
                  if (get("country") or country) and not org.country:
                      facts.append(Fact("organization.country", (get("country") or country)[:2].upper(), source, ref, 0.8))
                  site = ensure_website(session, org, website) if website else None
                  apply_facts(session, org, self.check_facts(facts), site)
                  if site is not None and on_website is not None:
                      on_website(org.id, website)

                  if (first or last) and lawful_basis:
                      person = Person(
                          organization=org,
                          full_name=" ".join(p for p in (first, last) if p)[:200],
                          role_title=get("role") or None,
                          email=person_email or (org_email if (first or last) and org_email and org_email not in org.emails else None),
                          linkedin_url=get("profile_url") or None,
                          source=source,
                          lawful_basis=lawful_basis,
                          notice_status=notice_status,
                          created_by=actor.id if actor else None,
                      )
                      session.add(person)
                      session.flush()
                      if lawful_basis == "consent":
                          consent = ConsentRecord(
                              person_id=person.id,
                              purpose="b2b_contact",
                              text_version=f"import:{batch.kind}",
                              captured_via="import",
                              captured_at=utcnow(),
                              proof_ref=ref,
                          )
                          session.add(consent)
                          session.flush()
                          person.consent_id = consent.id
                      summary.persons_created += 1
                  summary.ok += 1
            except Exception as exc:  # row-level isolation; nothing personal in the error log
                summary.errors.append({"row": index, "error": str(exc)[:200]})
        batch.row_count = summary.rows
        batch.rows_ok = summary.ok
        batch.rows_error = len(summary.errors)
        batch.organizations_created = summary.organizations_created
        batch.organizations_matched = summary.organizations_matched
        batch.persons_created = summary.persons_created
        batch.errors = summary.errors[:200]
        session.flush()
        return summary
